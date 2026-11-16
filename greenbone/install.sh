#!/usr/bin/env bash
# Set up the Greenbone VM in one go: Docker, Greenbone, and the Elastic Agent
# that ships its findings to the stack. Runs on a fresh Ubuntu/Debian VM, as
# root. stack/connect-greenbone.sh on the stack VM prints the full command,
# token and CA included.
#
#   sudo ./install.sh --url https://192.168.1.50:8220 --token <token> --ca ./ca.crt \
#                     [--targets 192.168.1.0/24] [--tz Europe/London] [--foreground]
#
#   --targets     what to scan: hosts, ranges or CIDRs, comma-separated.
#                 Default: the subnet of this VM's default route.
#   --tz          timezone the scan schedule is in. Default: this VM's.
#   --foreground  wait for the feed load in this terminal (hours, the first
#                 time) instead of handing it to a background service.
#
# Without --url/--token/--ca it sets up Greenbone and skips the agent. Safe to
# re-run; an existing greenbone/.env is kept, apart from what flags change.
# Afterwards, greenbone/update.sh applies repo or .env changes.
set -euo pipefail
cd "$(dirname "$0")"
HERE=$PWD

URL="" TOKEN="" CA="" TARGETS="" TZ_NAME="" FOREGROUND=false
while [[ $# -gt 0 ]]; do
  case $1 in
    --url)        URL=$2;     shift 2 ;;
    --token)      TOKEN=$2;   shift 2 ;;
    --ca)         CA=$2;      shift 2 ;;
    --targets)    TARGETS=$2; shift 2 ;;
    --tz)         TZ_NAME=$2; shift 2 ;;
    --foreground) FOREGROUND=true; shift ;;
    # Accepted for symmetry with the agent installers. This VM generates the
    # scan key, so there is nothing to receive.
    --scan-key)   shift 2 ;;
    -h|--help)    sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "!! Run as root (sudo)."; exit 1; }
AGENT=false
if [[ -n "$URL$TOKEN$CA" ]]; then
  [[ -n $URL && -n $TOKEN ]] || { echo "!! --url and --token go together."; exit 1; }
  [[ -f ${CA:-} ]] || { echo "!! --ca must point at the ca.crt copied from stack/certs/ca/"; exit 1; }
  CA=$(readlink -f "$CA")
  AGENT=true
fi

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

# ── Host ─────────────────────────────────────────────────────────────────────
step "Checking the host"
# Subshells: os-release defines VERSION, among others (see CLAUDE.md).
OS_ID=$(. /etc/os-release && printf '%s' "${ID:-unknown}")
OS_CODENAME=$(. /etc/os-release && printf '%s' "${VERSION_CODENAME:-}")
echo "   $(. /etc/os-release && printf '%s' "${PRETTY_NAME:-unknown}"), $(dpkg --print-architecture)"
[[ $OS_ID == ubuntu || $OS_ID == debian ]] \
  || { echo "!! This installer is for Ubuntu or Debian. Install Docker yourself and run setup.sh."; exit 1; }

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git openssh-client python3 iproute2 >/dev/null

# ── Docker ───────────────────────────────────────────────────────────────────
step "Docker"
if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
  echo "   $(docker --version)"
else
  # Docker's own packages: Ubuntu's docker.io has no Compose v2.
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/${OS_ID}/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc]" \
       "https://download.docker.com/linux/${OS_ID} ${OS_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin >/dev/null
  systemctl enable --now docker >/dev/null
  echo "   installed $(docker --version)"
fi

# ── .env ─────────────────────────────────────────────────────────────────────
step "Configuration (greenbone/.env)"
# Replace KEY=... or append it. Values are written quoted where they need it.
set_env() {
  local tmp; tmp=$(mktemp .env.XXXXXX)
  KEY=$1 VAL=$2 awk '
    BEGIN { k = ENVIRON["KEY"]; line = k "=" ENVIRON["VAL"] }
    $0 ~ "^" k "=" { if (!done) print line; done = 1; next }
    { print }
    END { if (!done) print line }' .env > "$tmp"
  chmod 600 "$tmp"; mv "$tmp" .env
}

if [[ ! -f .env ]]; then
  install -m 600 .env.example .env
  # Alphanumeric: it goes through .env, a gvmd command line and the exporter's
  # config, and none of them needs to be taught about quoting.
  set_env GREENBONE_ADMIN_PASSWORD "$(python3 -c 'import secrets, string
a = string.ascii_letters + string.digits
print("".join(secrets.choice(a) for _ in range(24)))')"
  echo "   created with a generated admin password"
  if [[ -z $TARGETS ]]; then
    # The subnet of the interface the default route leaves by.
    TARGETS=$(python3 - <<'EOF'
import ipaddress, json, subprocess
routes = json.loads(subprocess.run(["ip", "-j", "-4", "route", "show", "default"],
                                   capture_output=True, text=True).stdout or "[]")
dev = routes[0]["dev"] if routes else None
if dev:
    addrs = json.loads(subprocess.run(["ip", "-j", "-4", "addr", "show", "dev", dev],
                                      capture_output=True, text=True).stdout or "[]")
    for a in (addrs[0].get("addr_info", []) if addrs else []):
        if a.get("family") == "inet":
            print(ipaddress.ip_interface("%s/%s" % (a["local"], a["prefixlen"])).network)
            break
EOF
)
    [[ -n $TARGETS ]] || { echo "!! Could not work out this VM's subnet. Pass --targets."; exit 1; }
    echo "   scanning this VM's subnet, ${TARGETS} (--targets to change)"
  fi
  if [[ -z $TZ_NAME ]]; then
    TZ_NAME=$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || true)
    TZ_NAME=${TZ_NAME:-UTC}
  fi
else
  echo "   keeping the existing .env"
fi
[[ -n $TARGETS ]] && { set_env SCAN_TARGETS "$TARGETS"; echo "   SCAN_TARGETS=${TARGETS}"; }
[[ -n $TZ_NAME ]] && { set_env SCAN_TZ "$TZ_NAME";     echo "   SCAN_TZ=${TZ_NAME}"; }

# ── Greenbone ────────────────────────────────────────────────────────────────
# Everything short of the feed wait: key pair, containers, admin password,
# exporter. The wait comes after the agent, so the VM is enrolled and shipping
# its own metrics during the hours the feeds take.
"$HERE/setup.sh" --no-wait

# ── Agent ────────────────────────────────────────────────────────────────────
if $AGENT; then
  # With this VM's own scan key, so the scanner can log in to its own host.
  "$HERE/../vms/install-agent.sh" --url "$URL" --token "$TOKEN" --ca "$CA" \
    --scan-key "$(cat "$HERE/secrets/scan_key.pub")"
else
  step "Elastic Agent"
  echo "   skipped (no --url/--token/--ca). Run stack/connect-greenbone.sh on the"
  echo "   stack VM for the command that includes them."
fi

# ── Feeds and scan ───────────────────────────────────────────────────────────
step "Feed load and scan provisioning"
PROVISION_UNIT=greenbone-provision
if $FOREGROUND; then
  "$HERE/setup.sh"
elif systemctl is-active --quiet "$PROVISION_UNIT"; then
  echo "   already running: journalctl -fu ${PROVISION_UNIT}"
else
  systemctl reset-failed "$PROVISION_UNIT" >/dev/null 2>&1 || true
  # Transient: it exists only to see the first feed load through. If the VM
  # reboots first, greenbone/update.sh picks up where it left off.
  systemd-run --quiet --unit="$PROVISION_UNIT" --collect \
    --description="Wait for the Greenbone feeds, then provision the scan" \
    --property=Type=exec "$HERE/setup.sh"
  echo "   running in the background — the first feed load takes hours."
  echo "   Follow it:   journalctl -fu ${PROVISION_UNIT}"
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
GSA_PORT=$(sed -n 's/^GSA_PORT=//p' .env | tail -1)
cat <<EOF

────────────────────────────────────────────────────────────────────────────
 Greenbone is installed.

   Web UI     https://${IP:-<this VM>}:${GSA_PORT:-9392}    user admin
              password: GREENBONE_ADMIN_PASSWORD in ${HERE}/.env

 Last step — on the stack VM, from its checkout of this repo:

   ./stack/connect-greenbone.sh \\
     --pubkey "$(cat "$HERE/secrets/scan_key.pub")" \\
     --web-ui https://${IP:-<this VM>}:${GSA_PORT:-9392}

 That saves the scan key for future agent installs, links Grafana's
 vulnerabilities dashboard to the web UI, and prints the one command each
 already-enrolled host needs so the scanner can log in to it.
────────────────────────────────────────────────────────────────────────────
EOF
