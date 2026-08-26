#!/usr/bin/env bash
# Install Elastic Agent on an Ubuntu or Debian VM running the media stack.
#
# Run on each VM, as root. Safe to re-run.
#
#   ./install-agent.sh --url https://192.168.1.50:8220 \
#                      --token <enrollment-token> \
#                      --ca ./ca.crt \
#                      --tags media,docker
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
# Read os-release in subshells rather than sourcing it into this one. It
# defines NAME, VERSION and ID, and sourcing it here would overwrite any
# variable of ours that shares a name — which is exactly how AGENT_VERSION
# used to become "24.04 LTS (Noble Numbat)" and the download URL malformed.
OS_PRETTY=$(. /etc/os-release && printf '%s' "${PRETTY_NAME:-unknown}")
OS_ID=$(. /etc/os-release && printf '%s' "${ID:-unknown}")
echo "   ${OS_PRETTY}"
[[ $OS_ID == ubuntu || $OS_ID == debian ]] || echo "   !! Untested on ${OS_ID}; continuing anyway."

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl ca-certificates >/dev/null

# The Docker integration reads /var/run/docker.sock. The agent runs as root, so
# this is only a warning about a missing daemon, not a permissions problem.
if [[ -S /var/run/docker.sock ]]; then
  echo "   docker socket present — the Docker integration will collect container logs and metrics"
else
  echo "   no docker socket found — remove the Docker integration from this VM's policy if it is not a Docker host"
fi

# ── Log paths ────────────────────────────────────────────────────────────────
# The media-vm policy tails these. Report which exist so a wrong bind-mount
# path is caught now rather than three days into an empty dashboard.
step "Looking for media application logs"
declare -A EXPECTED=(
  [sonarr]='/opt/appdata/sonarr/logs/*.txt'
  [radarr]='/opt/appdata/radarr/logs/*.txt'
  [lidarr]='/opt/appdata/lidarr/logs/*.txt'
  [readarr]='/opt/appdata/readarr/logs/*.txt'
  [prowlarr]='/opt/appdata/prowlarr/logs/*.txt'
  [bazarr]='/opt/appdata/bazarr/log/*.log'
  [jellyfin]='/opt/appdata/jellyfin/log/*.log'
  [emby]='/opt/appdata/emby/logs/*.txt'
  [kavita]='/opt/appdata/kavita/logs/*.log'
)
FOUND=0
for app in "${!EXPECTED[@]}"; do
  # shellcheck disable=SC2086
  if compgen -G ${EXPECTED[$app]} >/dev/null 2>&1; then
    echo "   found  ${app}: ${EXPECTED[$app]}"; FOUND=$((FOUND+1))
  fi
done
if [[ $FOUND -eq 0 ]]; then
  cat <<'EOF'
   !! None of the default paths matched. That is expected if your containers
      bind-mount their config somewhere other than /opt/appdata. Note the real
      paths now and correct them in Kibana → Fleet → media-vm policy → the
      relevant Custom Logs integration. Collection will otherwise be silent.
EOF
fi

# ── Elastic Agent ────────────────────────────────────────────────────────────
step "Installing Elastic Agent ${AGENT_VERSION}"
if command -v elastic-agent >/dev/null 2>&1; then
  echo "   removing the existing agent first"
  elastic-agent uninstall --force >/dev/null 2>&1 || true
fi

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
ARCH=$(dpkg --print-architecture)
PKG="elastic-agent-${AGENT_VERSION}-linux-$([[ $ARCH == arm64 ]] && echo arm64 || echo x86_64)"
DL="https://artifacts.elastic.co/downloads/beats/elastic-agent/${PKG}.tar.gz"
echo "   downloading ${PKG}.tar.gz"
curl -fsSL --retry 3 -o "$TMP/agent.tar.gz" "$DL" || {
  echo "!! Download failed: $DL"
  echo "   Check the version exists and that this host can reach artifacts.elastic.co."
  exit 1; }
# The 9.x agent tarball carries 80 directory entries whose mode is
# 0o20000000755 — Go's fs.ModeDir bit (1<<31) leaked into the tar header when
# the artifact was built. A mainline kernel masks the junk off (umode_t is 16
# bits) and GNU tar extracts it in silence; some kernels reject that chmod with
# EFAULT instead — QNAP's QTS, hosting this as an LXD container — and tar then
# reports "Cannot change mode ...: Bad address" for exactly those directories
# plus the top-level symlink, and exits non-zero. The download is fine. Retry
# with python, which masks the mode back to the permission bits and does not
# chmod symlinks at all.
extract_agent() {
  local archive=$1 dest=$2
  if tar -xzf "$archive" -C "$dest" 2>"$dest/tar.err"; then return 0; fi
  tail -3 "$dest/tar.err" | sed 's/^/   /'
  echo "   tar could not extract the archive — retrying with python3"
  command -v python3 >/dev/null 2>&1 || apt-get install -y -qq python3-minimal >/dev/null
  rm -rf "${dest:?}/${PKG:?}"
  # filter= landed in 3.12 (and 3.11.4); older versions extract fully-trusted.
  python3 -c 'import sys, tarfile
kw = {"filter": "fully_trusted"} if hasattr(tarfile, "fully_trusted_filter") else {}
with tarfile.open(sys.argv[1]) as tf:
    members = tf.getmembers()
    for m in members:
        m.mode &= 0o7777
    tf.extractall(sys.argv[2], members=members, **kw)' "$archive" "$dest"
}
extract_agent "$TMP/agent.tar.gz" "$TMP" || {
  echo "!! The downloaded archive could not be extracted."; exit 1; }
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
minute, running the "media-vm" policy.

  elastic-agent status
  journalctl -u elastic-agent -f
EOF
