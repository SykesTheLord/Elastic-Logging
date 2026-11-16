#!/usr/bin/env bash
# Bring an installed Greenbone VM up to date with this repo and its .env.
# Run on the Greenbone VM, as root or with sudo, after editing greenbone/.env or
# whenever the repo has moved on.
#
#   sudo ./update.sh              # pull the repo, then re-apply everything
#   sudo ./update.sh --no-pull    # re-apply the checkout as it is (after an .env edit)
#   sudo ./update.sh --scan-now   # ...and start a scan once it is applied
#
# Re-applies: newer Greenbone images (containers recreated only when one
# changed), the admin password, the exporter's code, systemd units, logrotate
# and /etc/gvm-export.conf, and — once the feeds are loaded — the SSH
# credential, target, schedule and task from SCAN_* in .env. Nothing waits on
# the feeds: if they are still loading, the scan settings are applied by the
# next run.
#
# On the stack VM, the matching step is stack/connect-greenbone.sh: it re-PUTs
# the findings pipeline and regenerates the dashboards.
set -euo pipefail
cd "$(dirname "$0")"
HERE=$PWD

PULL=true SCAN_NOW=""
for a in "$@"; do
  case $a in
    --no-pull)  PULL=false ;;
    --scan-now) SCAN_NOW=--scan-now ;;
    -h|--help)  sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "Unknown option: $a"; exit 1 ;;
  esac
done

if [[ $EUID -eq 0 ]]; then SUDO=""; else SUDO="sudo"; fi
step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

[[ -f .env ]] || { echo "!! No greenbone/.env — this VM was never set up. Run install.sh first."; exit 1; }

# ── Repo ─────────────────────────────────────────────────────────────────────
if $PULL; then
  step "Updating the checkout"
  # Every git command runs as the checkout's owner, not root — even the first
  # rev-parse: git refuses to so much as read a repo owned by someone else
  # ("dubious ownership"), and a root-run pull would leave root-owned objects
  # in the owner's .git.
  OWNER=$(stat -c %U "$HERE")
  as_owner() {
    if [[ $(id -un) == "$OWNER" ]]; then "$@"
    elif [[ $EUID -eq 0 ]]; then runuser -u "$OWNER" -- "$@"
    else sudo -u "$OWNER" -- "$@"; fi
  }
  TOP=$(as_owner git -C "$HERE" rev-parse --show-toplevel 2>/dev/null) \
    || { echo "!! $HERE is not in a git checkout; use --no-pull."; exit 1; }
  BEFORE=$(as_owner git -C "$TOP" rev-parse HEAD)
  as_owner git -C "$TOP" pull --ff-only --quiet \
    || { echo "!! git pull failed (local commits or edits?). Fix it, or use --no-pull."; exit 1; }
  AFTER=$(as_owner git -C "$TOP" rev-parse HEAD)
  if [[ $BEFORE == "$AFTER" ]]; then
    echo "   already current at ${AFTER:0:7}"
  else
    echo "   ${BEFORE:0:7} → ${AFTER:0:7}"
    as_owner git -C "$TOP" --no-pager log --oneline "$BEFORE..$AFTER" -- greenbone vms | sed 's/^/     /'
  fi
fi

# ── .env keys added since install ────────────────────────────────────────────
# setup.sh has a default for every optional key, so a missing one is never an
# error — but say so, since the default may not be what this VM wants.
missing=$(comm -23 <(sed -n 's/^\([A-Z_][A-Z0-9_]*\)=.*/\1/p' .env.example | sort -u) \
                   <(sed -n 's/^\([A-Z_][A-Z0-9_]*\)=.*/\1/p' .env | sort -u))
if [[ -n $missing ]]; then
  echo "   .env lacks $(echo "$missing" | tr '\n' ' ')— using defaults (see .env.example)"
fi
if grep -qE '^SCAN_RRULE=[^"'\''].*;' .env; then
  echo "   note: SCAN_RRULE in .env is unquoted; setup.sh copes, but quote it for anything else that sources the file"
fi

# ── Re-apply ─────────────────────────────────────────────────────────────────
# A first feed load still running under install.sh's background unit already
# ends by provisioning, and two provisioners racing gvmd helps nobody.
if systemctl is-active --quiet greenbone-provision 2>/dev/null; then
  echo "   the first feed load is still running (journalctl -fu greenbone-provision);"
  echo "   applying everything except the scan settings, which it applies when done."
fi
# A fresh process, so an update.sh or setup.sh the pull just replaced is read
# from its new version rather than mid-way through the old one.
exec $SUDO "$HERE/setup.sh" --no-wait $SCAN_NOW
