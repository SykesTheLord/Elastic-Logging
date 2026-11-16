#!/usr/bin/env bash
# One-shot bring-up for Greenbone on its own VM. Safe to re-run: every step is
# idempotent, and a re-run is how .env changes (targets, schedule) are applied.
#
#   ./setup.sh               # everything; waits for the feeds (hours on first run)
#   ./setup.sh --no-wait     # start the containers and stop; re-run later
#   ./setup.sh --scan-now    # also start a scan immediately once provisioned
#
# For a first install, install.sh wraps this (Docker, .env, agent); afterwards
# update.sh pulls the repo and re-runs it.
#
# Runs on the Greenbone VM, as root or with sudo. See docs/vulnerability-scanning.md.
set -euo pipefail
cd "$(dirname "$0")"
HERE=$PWD

WAIT=true SCAN_NOW=""
for a in "$@"; do
  case $a in
    --no-wait)  WAIT=false ;;
    --scan-now) SCAN_NOW=--scan-now ;;
    -h|--help)  sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "Unknown option: $a"; exit 1 ;;
  esac
done

[[ -f .env ]] || { echo "!! No .env found. Copy .env.example to .env and edit it first."; exit 1; }
set -a; . ./.env; set +a
# An RRULE is full of semicolons, and sourcing an unquoted
# SCAN_RRULE=FREQ=WEEKLY;BYDAY=SU runs "BYDAY=SU" as a second statement —
# leaving FREQ=WEEKLY, which schedules the scan on whatever weekday it was
# provisioned. Read the raw line instead, quoted or not.
RRULE_RAW=$(sed -n 's/^SCAN_RRULE=//p' .env | tail -1)
RRULE_RAW=${RRULE_RAW%\"}; RRULE_RAW=${RRULE_RAW#\"}
RRULE_RAW=${RRULE_RAW%\'}; RRULE_RAW=${RRULE_RAW#\'}
[[ -n $RRULE_RAW ]] && SCAN_RRULE=$RRULE_RAW
unset BYDAY BYMONTHDAY BYHOUR BYMINUTE INTERVAL COUNT UNTIL WKST
: "${GREENBONE_ADMIN_PASSWORD:?}" "${SCAN_TARGETS:?}" "${SCAN_SSH_USER:?}"

if [[ $EUID -eq 0 ]]; then SUDO=""; else SUDO="sudo"; fi
step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
# provision.py needs the .env values and root for the gvmd socket. sudo drops
# the environment, so pass it through explicitly.
prov() {
  $SUDO env GREENBONE_ADMIN_PASSWORD="$GREENBONE_ADMIN_PASSWORD" \
    SCAN_TARGETS="$SCAN_TARGETS" SCAN_EXCLUDE="${SCAN_EXCLUDE:-}" \
    SCAN_START="${SCAN_START:-02:00}" SCAN_RRULE="${SCAN_RRULE:-FREQ=WEEKLY;BYDAY=SU}" \
    SCAN_TZ="${SCAN_TZ:-UTC}" SCAN_SSH_USER="$SCAN_SSH_USER" \
    SCAN_SSH_PORT="${SCAN_SSH_PORT:-22}" MIN_QOD="${MIN_QOD:-70}" \
    python3 "$HERE/provision.py" "$@"
}

# ── Preflight ────────────────────────────────────────────────────────────────
step "Preflight"
command -v docker >/dev/null || { echo "!! docker is not installed."; exit 1; }
$SUDO docker compose version >/dev/null 2>&1 || {
  echo "!! The Docker Compose v2 plugin is missing."
  echo "   sudo apt-get install -y docker-compose-plugin"
  exit 1; }
command -v ssh-keygen >/dev/null || { echo "!! ssh-keygen is missing: sudo apt-get install -y openssh-client"; exit 1; }
[[ "$GREENBONE_ADMIN_PASSWORD" == changeme* ]] && { echo "!! GREENBONE_ADMIN_PASSWORD is still the placeholder. Edit .env first."; exit 1; }
python3 -c 'import zoneinfo, sys; zoneinfo.ZoneInfo(sys.argv[1])' "${SCAN_TZ:-UTC}" 2>/dev/null \
  || { echo "!! SCAN_TZ=${SCAN_TZ} is not a timezone name (e.g. Europe/London, America/New_York)."; exit 1; }

# The feeds, the Postgres database and the scanner's Redis together want
# about this much. Under it the first feed load is OOM-killed part-way and
# gvmd comes back with a half-imported SCAP database.
RAM_G=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
if (( RAM_G < 7 )); then
  echo "!! ${RAM_G} GB RAM. Greenbone needs 8 GB; give the VM more memory."
  exit 1
fi
# Images and feed volumes land under Docker's data root, not wherever this
# checkout happens to live.
DATA_ROOT=$($SUDO docker info -f '{{.DockerRootDir}}' 2>/dev/null || true)
[[ -d ${DATA_ROOT:-/nonexistent} ]] || DATA_ROOT=/
AVAIL_G=$(df -BG --output=avail "$DATA_ROOT" | tail -1 | tr -dc '0-9')
if [[ -n "$AVAIL_G" ]] && (( AVAIL_G < 40 )); then
  echo "!! Only ${AVAIL_G} GB free under ${DATA_ROOT}. The feeds alone are ~25 GB."
  exit 1
fi
echo "   memory: ${RAM_G} GB, disk: ${AVAIL_G:-?} GB free"

# ── SSH key for authenticated scans ──────────────────────────────────────────
step "Scan key"
install -d -m 0700 secrets
if [[ ! -f secrets/scan_key ]]; then
  # PEM, not the OpenSSH container format: gvmd parses the key with libssh
  # and stores it as given, and PEM is the form every libssh it ships with
  # reads. RSA because PEM has no ed25519 encoding.
  ssh-keygen -q -t rsa -b 4096 -m PEM -N '' -C "greenbone-scan@$(hostname -s)" -f secrets/scan_key
  echo "   generated secrets/scan_key"
else
  echo "   reusing secrets/scan_key"
fi
chmod 600 secrets/scan_key

# ── Containers ───────────────────────────────────────────────────────────────
step "Pulling and starting Greenbone (first pull is ~3 GB)"
$SUDO docker compose pull -q
$SUDO docker compose up -d

printf '   waiting for gvmd '
for i in $(seq 1 120); do
  [[ -S /run/gvmd/gvmd.sock ]] && { echo " up"; break; }
  printf '.'; sleep 5
  [[ $i -eq 120 ]] && { echo; echo "!! gvmd never opened its socket. Check: docker compose logs gvmd"; exit 1; }
done

step "Admin account"
# The gvmd image creates "admin" with a random password on first boot (or,
# on older images, none at all). Either way, set the one from .env.
gvmd_cli() { $SUDO docker compose exec -T -u gvmd gvmd gvmd "$@"; }
for i in $(seq 1 30); do
  if gvmd_cli --get-users 2>/dev/null | grep -qx admin; then break; fi
  [[ $i -eq 30 ]] && { gvmd_cli --create-user=admin --password="$GREENBONE_ADMIN_PASSWORD" >/dev/null 2>&1; break; }
  sleep 5
done
gvmd_cli --user=admin --new-password="$GREENBONE_ADMIN_PASSWORD" >/dev/null 2>&1
# Feed objects (scan configs, port lists) are only imported once some user
# owns them. The image normally sets this; make sure.
ADMIN_UUID=$(gvmd_cli --get-users --verbose 2>/dev/null | awk '$1=="admin"{print $2}')
[[ -n $ADMIN_UUID ]] && gvmd_cli --modify-setting 78eceaec-3385-11ea-b237-28d24461215b --value "$ADMIN_UUID" >/dev/null 2>&1
echo "   admin password set"

# ── Exporter ─────────────────────────────────────────────────────────────────
# Installed before the feeds finish, so the timer is already in place when the
# first scheduled scan completes even if this run is left at --no-wait.
step "Installing the Elasticsearch exporter"
$SUDO install -d -m 0755 /usr/local/lib/gvm-export
$SUDO install -m 0644 gmp.py                /usr/local/lib/gvm-export/gmp.py
$SUDO install -m 0755 gvm-export.py         /usr/local/lib/gvm-export/gvm-export.py
$SUDO install -d -m 0750 -o root -g adm /var/log/elastic
$SUDO install -d -m 0700 /var/lib/gvm-export
umask 077
printf 'GREENBONE_ADMIN_PASSWORD=%s\nMIN_QOD=%s\n' "$GREENBONE_ADMIN_PASSWORD" "${MIN_QOD:-70}" \
  | $SUDO tee /etc/gvm-export.conf >/dev/null
$SUDO chmod 600 /etc/gvm-export.conf
umask 022
$SUDO install -m 0644 systemd/elastic-greenbone-export.service /etc/systemd/system/
$SUDO install -m 0644 systemd/elastic-greenbone-export.timer   /etc/systemd/system/
$SUDO install -m 0644 systemd/elastic-greenbone-export.logrotate /etc/logrotate.d/elastic-greenbone-export
$SUDO systemctl daemon-reload
$SUDO systemctl enable --now elastic-greenbone-export.timer >/dev/null
echo "   timer enabled — writes /var/log/elastic/greenbone.ndjson"

# ── Feeds ────────────────────────────────────────────────────────────────────
step "Waiting for the feeds to load"
if ! prov --ready >/dev/null 2>&1; then
  if ! $WAIT; then
    echo "   not loaded yet. Re-run ./setup.sh (or ./update.sh) once they are; it picks up from here."
    echo "   Progress: docker compose logs -f gvmd"
    exit 0
  fi
  echo "   A first load takes one to several hours. Ctrl-C is safe — re-run later."
  last=""
  while ! msg=$(prov --ready 2>&1); do
    [[ $msg != "$last" ]] && { printf '\n   %s ' "$msg"; last=$msg; }
    printf '.'; sleep 60
  done
  echo
fi
prov --ready

# ── Scan ─────────────────────────────────────────────────────────────────────
step "Provisioning the scheduled scan"
prov $SCAN_NOW

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
cat <<EOF

────────────────────────────────────────────────────────────────────────────
 Greenbone is up.

   Web UI    https://${IP:-<this VM>}:${GSA_PORT:-9392}    admin / (see .env)
   Scans     ${SCAN_TARGETS}
             ${SCAN_RRULE} at ${SCAN_START:-02:00} ${SCAN_TZ:-UTC}

 Authenticated scans log in as '${SCAN_SSH_USER}' with this key. To hand it
 to the stack, and link Grafana's vulnerabilities dashboard to this web UI,
 run on the stack VM:

   ./stack/connect-greenbone.sh \\
     --pubkey "$(cat secrets/scan_key.pub)" \\
     --web-ui https://${IP:-<this VM>}:${GSA_PORT:-9392}

 This VM ships findings through its own Elastic Agent on the 'greenbone'
 policy; stack/connect-greenbone.sh also prints the command that enrols it.
 Later changes to the repo or to .env: sudo ./update.sh
────────────────────────────────────────────────────────────────────────────
EOF
