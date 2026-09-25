#!/usr/bin/env bash
# Install Elastic Agent on a headless Arch Linux machine running the
# "baseline" policy — no Docker, no media apps, no GUI. System metrics and
# the journal only.
#
# Run on the machine, as root. Safe to re-run.
#
#   ./install-agent-arch.sh --url https://192.168.1.50:8220 \
#                           --token <enrollment-token> \
#                           --ca ./ca.crt
set -euo pipefail

AGENT_VERSION=9.5.1
URL="" TOKEN="" CA="" TAGS=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --url)     URL=$2;     shift 2 ;;
    --token)   TOKEN=$2;   shift 2 ;;
    --ca)      CA=$2;      shift 2 ;;
    --tags)    TAGS=$2;    shift 2 ;;
    --version) AGENT_VERSION=$2; shift 2 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "!! Run as root."; exit 1; }
[[ -n $URL ]]     || { echo "!! --url is required (e.g. https://192.168.1.50:8220)"; exit 1; }
[[ -n $TOKEN ]]   || { echo "!! --token is required"; exit 1; }
[[ -f ${CA:-} ]]  || { echo "!! --ca must point at the ca.crt copied from stack/certs/ca/"; exit 1; }

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

step "Checking the host"
# Read os-release in a subshell rather than sourcing it. It defines NAME,
# VERSION and ID, and sourcing it here would overwrite any variable of ours
# that shares a name — see the CLAUDE.md note on VERSION specifically.
OS_PRETTY=$(. /etc/os-release && printf '%s' "${PRETTY_NAME:-unknown}")
OS_ID=$(. /etc/os-release && printf '%s' "${ID:-unknown}")
echo "   ${OS_PRETTY}"
[[ $OS_ID == arch ]] || echo "   !! Untested on ${OS_ID}; continuing anyway."

pacman -Sy --noconfirm --needed curl ca-certificates >/dev/null

# This installer targets the "baseline" policy — system metrics and the
# journal, nothing app-specific. If this host does run Docker or one of the
# media apps, use install-agent.sh against the media-vm policy instead so
# those inputs actually get collected.
if command -v docker >/dev/null 2>&1 || [[ -S /var/run/docker.sock ]]; then
  echo "   !! docker found on this host — the baseline policy will not collect its containers."
  echo "      Consider enrolling into media-vm with install-agent.sh instead."
fi

# ── Elastic Agent ────────────────────────────────────────────────────────────
step "Installing Elastic Agent ${AGENT_VERSION}"
if command -v elastic-agent >/dev/null 2>&1; then
  echo "   removing the existing agent first"
  elastic-agent uninstall --force >/dev/null 2>&1 || true
fi

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
ARCH=$(uname -m)
case $ARCH in
  x86_64)  PKG_ARCH=x86_64 ;;
  aarch64) PKG_ARCH=arm64 ;;
  *) echo "!! Unsupported architecture: ${ARCH}"; exit 1 ;;
esac
PKG="elastic-agent-${AGENT_VERSION}-linux-${PKG_ARCH}"
DL="https://artifacts.elastic.co/downloads/beats/elastic-agent/${PKG}.tar.gz"
echo "   downloading ${PKG}.tar.gz"
curl -fsSL --retry 3 -o "$TMP/agent.tar.gz" "$DL" || {
  echo "!! Download failed: $DL"
  echo "   Check the version exists and that this host can reach artifacts.elastic.co."
  exit 1; }
tar -xzf "$TMP/agent.tar.gz" -C "$TMP"
[[ -x "$TMP/$PKG/elastic-agent" ]] || { echo "!! $PKG/elastic-agent missing from the archive."; exit 1; }

install -d -m 0755 /etc/elastic-agent
install -m 0644 "$CA" /etc/elastic-agent/ca.crt

INSTALL_ARGS=(
  --non-interactive
  # Required from 9.0: the default "basic" flavor omits the journald
  # dependencies, and the Journald integration then collects nothing at all.
  --install-servers
  --url="$URL"
  --enrollment-token="$TOKEN"
  --certificate-authorities=/etc/elastic-agent/ca.crt
)
[[ -n $TAGS ]] && INSTALL_ARGS+=( --tag="$TAGS" )

"$TMP/$PKG/elastic-agent" install "${INSTALL_ARGS[@]}"

step "Done"
elastic-agent status || true
cat <<EOF

The agent is enrolled and should appear in Kibana → Fleet → Agents within a
minute, running the "baseline" policy.

  elastic-agent status
  journalctl -u elastic-agent -f
EOF
