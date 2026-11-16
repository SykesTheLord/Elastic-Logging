#!/usr/bin/env bash
# Install the per-host extras that sit alongside Elastic Agent:
#
#   - the software-update checker (updates.py + a 6-hourly systemd timer),
#     whose output the agent ships as dataset software_updates;
#   - optionally, the account Greenbone logs in as for authenticated scans.
#
# The agent installers call this. Run it directly on a host that is already
# enrolled — it does not touch the agent. As root. Safe to re-run.
#
#   ./install-host-tools.sh
#   ./install-host-tools.sh --scan-key "ssh-rsa AAAA... greenbone-scan@greenbone"
#   ./install-host-tools.sh --scan-key ./scan_key.pub --scan-user greenbone-scan
#
# Works on Ubuntu, Debian, Proxmox VE and Arch.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)

SCAN_KEY="" SCAN_USER=greenbone-scan
while [[ $# -gt 0 ]]; do
  case $1 in
    --scan-key)  SCAN_KEY=$2;  shift 2 ;;
    --scan-user) SCAN_USER=$2; shift 2 ;;
    -h|--help)   sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "!! Run as root."; exit 1; }
step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

# ── Update checker ───────────────────────────────────────────────────────────
step "Installing the update checker"
command -v python3 >/dev/null || {
  if command -v apt-get >/dev/null; then apt-get install -y -qq python3-minimal >/dev/null
  else pacman -S --needed --noconfirm python >/dev/null; fi; }
if command -v pacman >/dev/null && ! { command -v checkupdates && command -v fakeroot; } >/dev/null; then
  # checkupdates syncs into a private copy of the package database. The
  # alternative, `pacman -Sy` from a timer, leaves the host one `pacman -S`
  # away from a partial upgrade. It runs that sync under fakeroot and fails
  # with "Cannot find the fakeroot binary" without it — and fakeroot is in
  # base-devel, which a headless box often does not have.
  pacman -S --needed --noconfirm pacman-contrib fakeroot >/dev/null \
    || echo "   !! could not install pacman-contrib/fakeroot — no package updates will be reported"
fi
install -d -m 0750 -o root -g adm /var/log/elastic 2>/dev/null \
  || install -d -m 0750 /var/log/elastic   # Arch has no adm group by default
install -m 0755 "$HERE/updates.py"              /usr/local/bin/elastic-updates.py
install -m 0644 "$HERE/elastic-updates.service" /etc/systemd/system/
install -m 0644 "$HERE/elastic-updates.timer"   /etc/systemd/system/
if [[ -d /etc/logrotate.d ]]; then
  install -m 0644 "$HERE/elastic-updates.logrotate" /etc/logrotate.d/elastic-updates
  getent group adm >/dev/null || sed -i 's/root adm/root root/' /etc/logrotate.d/elastic-updates
fi
systemctl daemon-reload
systemctl enable --now elastic-updates.timer >/dev/null
echo "   running a first check (refreshes the package lists — can take a minute)"
systemctl start elastic-updates.service || true
if [[ -s /var/log/elastic/updates.ndjson ]]; then
  tail -1 /var/log/elastic/updates.ndjson \
    | python3 -c 'import json,sys; print("   " + json.load(sys.stdin).get("message", ""))'
else
  echo "   !! the checker produced nothing — check: journalctl -u elastic-updates"
fi

# ── Greenbone scan account ───────────────────────────────────────────────────
if [[ -n $SCAN_KEY ]]; then
  step "Greenbone scan account '${SCAN_USER}'"
  [[ -f $SCAN_KEY ]] && SCAN_KEY=$(<"$SCAN_KEY")
  [[ $SCAN_KEY =~ ^(ssh-(rsa|ed25519)|ecdsa-sha2-) ]] \
    || { echo "!! --scan-key is not an SSH public key (or a file holding one)."; exit 1; }

  if ! id "$SCAN_USER" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "/var/lib/${SCAN_USER}" \
            --shell /bin/bash --comment "Greenbone authenticated scans" "$SCAN_USER"
    echo "   user created"
  fi
  # '*' rather than the '!' useradd leaves: both mean "no password login",
  # but sshd treats '!' as a locked account and refuses key logins too on
  # hosts running without PAM.
  usermod -p '*' "$SCAN_USER"
  HOME_DIR=$(getent passwd "$SCAN_USER" | cut -d: -f6)
  install -d -m 0700 -o "$SCAN_USER" -g "$(id -gn "$SCAN_USER")" "$HOME_DIR/.ssh"
  # restrict: no forwarding, no pty, no agent. Greenbone's checks run plain
  # commands over exec channels (dpkg -l, uname -a, reading /etc/*-release),
  # which is all this account is for. No sudo — the local security checks
  # read package state, which needs no privilege.
  printf 'restrict %s\n' "$SCAN_KEY" > "$HOME_DIR/.ssh/authorized_keys"
  chown "$SCAN_USER:$(id -gn "$SCAN_USER")" "$HOME_DIR/.ssh/authorized_keys"
  chmod 600 "$HOME_DIR/.ssh/authorized_keys"
  echo "   key installed"

  if ! { command -v sshd >/dev/null || [[ -x /usr/sbin/sshd ]]; }; then
    echo "   !! no SSH server on this host — Greenbone will scan it unauthenticated."
  elif ! { systemctl is-active --quiet ssh 2>/dev/null || systemctl is-active --quiet sshd 2>/dev/null; }; then
    echo "   !! the SSH server is not running — Greenbone will scan this host unauthenticated."
  fi
  # An AllowUsers / AllowGroups line keeps the new account out without a word.
  SSHD_CFG=$( (/usr/sbin/sshd -T 2>/dev/null || sshd -T 2>/dev/null) | grep -Ei '^(allowusers|allowgroups) ' || true)
  if [[ -n $SSHD_CFG ]] && ! grep -qw -- "$SCAN_USER" <<<"$SSHD_CFG"; then
    echo "   !! sshd restricts logins ($(tr '\n' ' ' <<<"$SSHD_CFG")) — add ${SCAN_USER} or the scan login will fail."
  fi
fi
