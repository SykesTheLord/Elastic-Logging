#!/usr/bin/env bash
# Connect a Greenbone scanner to this stack and to Grafana. Run on the stack VM.
#
#   ./connect-greenbone.sh
#       Creates the 'greenbone' Fleet policy if it is missing, installs the
#       ingest pipelines, regenerates the Grafana dashboards, and prints the one
#       command that sets up the Greenbone VM (Docker, Greenbone, its agent).
#
#   ./connect-greenbone.sh --pubkey "ssh-rsa AAAA... greenbone-scan@gb" \
#                          --web-ui https://192.168.1.60:9392
#       The second half, with the two values greenbone/install.sh prints at the
#       end: saves them to .env, so every agent install command carries the
#       scan key and the vulnerabilities dashboard links to the web UI.
#
# Safe to re-run at any time, and re-running is how a repo update reaches the
# stack: the pipeline is re-PUT and the dashboards regenerated. Existing
# policies are never modified (see the CLAUDE.md note on setup-policies.py).
set -euo pipefail
cd "$(dirname "$0")"

PUBKEY="" WEB_UI=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --pubkey) PUBKEY=$2; shift 2 ;;
    --web-ui) WEB_UI=$2; shift 2 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

ENVF=.env
[[ -f $ENVF ]]             || { echo "!! stack/.env not found — deploy the stack first."; exit 1; }
[[ -f certs/ca/ca.crt ]]   || { echo "!! stack/certs/ca/ca.crt not found — run setup-certs.sh first."; exit 1; }
[[ -w $ENVF || -z "$PUBKEY$WEB_UI" ]] || { echo "!! stack/.env is not writable by $(id -un); re-run with sudo."; exit 1; }

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

# Replace KEY=... in .env, or append it. awk rather than sed: a public key is
# base64, so it is full of the slashes sed would need escaped.
set_env() {
  local key=$1 val=$2 tmp
  tmp=$(mktemp "$ENVF.XXXXXX")
  KEY=$key VAL=$val awk '
    BEGIN { k = ENVIRON["KEY"]; line = k "=" ENVIRON["VAL"] }
    $0 ~ "^" k "=" { if (!done) print line; done = 1; next }
    { print }
    END { if (!done) print line }' "$ENVF" > "$tmp"
  chmod --reference="$ENVF" "$tmp" 2>/dev/null || chmod 600 "$tmp"
  mv "$tmp" "$ENVF"
  echo "   $key saved to stack/.env"
}

if [[ -n $PUBKEY || -n $WEB_UI ]]; then
  step "Saving the Greenbone settings"
  if [[ -n $PUBKEY ]]; then
    # It lands in a file bash sources, inside double quotes: nothing in it may
    # end the quote or expand.
    [[ $PUBKEY =~ ^(ssh-rsa|ssh-ed25519|ecdsa-sha2-nistp[0-9]+)\ [A-Za-z0-9+/=]+(\ [^\"\$\`\\]*)?$ ]] \
      || { echo "!! --pubkey does not look like an OpenSSH public key line."; exit 1; }
    set_env GREENBONE_SCAN_PUBKEY "\"$PUBKEY\""
  fi
  if [[ -n $WEB_UI ]]; then
    [[ $WEB_UI =~ ^https?://[^/[:space:]\"]+(/[^[:space:]\"]*)?$ ]] \
      || { echo "!! --web-ui must be a URL such as https://192.168.1.60:9392"; exit 1; }
    set_env GREENBONE_WEB_UI "$WEB_UI"
  fi
fi

step "Fleet policy"
python3 ../fleet/setup-policies.py greenbone

step "Ingest pipelines"
../pipelines/install.sh

step "Grafana dashboards"
python3 ./gen-dashboards.py

# Read back after any edits above, so the summary reflects what is saved.
set -a
# .env is generated at deploy time, so there is nothing for shellcheck to follow.
# shellcheck disable=SC1090
. "./$ENVF"
set +a

if [[ -z ${GREENBONE_SCAN_PUBKEY:-} ]]; then
  step "Set up the Greenbone VM"
  cat <<'EOF'
   On a fresh Ubuntu 24.04 VM (4 cores, 8 GB, 80 GB — see
   docs/vulnerability-scanning.md), paste the command below. It installs
   Docker, starts Greenbone, enrols the VM's agent on the 'greenbone' policy,
   and leaves the feed load (hours, the first time) running in the background.

   Optional flags to append: --targets 192.168.1.0/24 (default: the VM's own
   subnet), --tz Europe/London (default: the VM's timezone).
EOF
  ../fleet/print-install-commands.sh greenbone
  echo "   When it finishes it prints the command to run back here."
else
  step "Connected"
  echo "   Scan key       saved — every command from fleet/print-install-commands.sh"
  echo "                  now creates the scan account on the host it enrols."
  echo "   Web UI link    ${GREENBONE_WEB_UI:-not set (--web-ui)}"
  echo "   Dashboard      Security — vulnerabilities (Greenbone), in Grafana at"
  echo "                  http://${STACK_IP}:${GRAFANA_PORT:-3000}; it fills after the first scan."
  echo
  echo "   Hosts enrolled before the key was saved need the scan account added."
  echo "   On each of them, from its clone of this repo:"
  echo
  # Plain double quotes are safe: --pubkey allows no quote, $, ` or backslash.
  printf '     sudo ./vms/install-host-tools.sh --scan-key "%s"\n' "$GREENBONE_SCAN_PUBKEY"
fi
