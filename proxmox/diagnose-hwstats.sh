#!/usr/bin/env bash
# Why are there no temperatures or SMART stats in the dashboard?
#
# Run on the Proxmox host. Checks each layer between the sensors and the
# Elastic Agent, and stops being useful at the point something is wrong.
set -uo pipefail
OUT=${HWSTATS_OUT:-/var/log/elastic/hwstats.ndjson}
ok()   { printf '  \033[32mok\033[0m    %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$*"; }
step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

step "1. Tools the collector depends on"
for c in python3 sensors smartctl; do
  command -v "$c" >/dev/null && ok "$c present" || bad "$c MISSING — apt-get install -y python3 lm-sensors smartmontools"
done
if command -v sensors >/dev/null; then
  n=$(sensors -j 2>/dev/null | grep -c '_input' || echo 0)
  (( n > 0 )) && ok "sensors reports $n readings" \
               || bad "sensors reports nothing — modules not loaded. See /etc/modules-load.d/lm-sensors.conf"
fi
if command -v smartctl >/dev/null; then
  d=$(lsblk -dn -o NAME,TYPE 2>/dev/null | awk '$2=="disk"{c++} END{print c+0}')
  ok "$d physical disk(s) visible to lsblk"
  [[ $EUID -eq 0 ]] || warn "not root — smartctl needs root, so disk stats would be empty when run by hand"
fi

step "2. The collector itself"
systemctl is-enabled elastic-hwstats.timer >/dev/null 2>&1 && ok "timer enabled" || bad "timer NOT enabled — systemctl enable --now elastic-hwstats.timer"
systemctl is-active  elastic-hwstats.timer >/dev/null 2>&1 && ok "timer active"  || bad "timer NOT active"
# ExecMainStatus reads 0 for a unit that has never run at all, so check it
# actually started before believing the exit code.
STARTED=$(systemctl show -p ExecMainStartTimestamp --value elastic-hwstats.service 2>/dev/null)
LAST=$(systemctl show -p ExecMainStatus --value elastic-hwstats.service 2>/dev/null)
if [[ -z "${STARTED:-}" ]]; then
  bad "the collector has never run — systemctl start elastic-hwstats.service"
elif [[ "${LAST:-1}" == "0" ]]; then
  ok "last run exited 0 (${STARTED})"
else
  bad "last run exited ${LAST} — journalctl -u elastic-hwstats -n 30"
fi

step "3. The file the agent reads"
if [[ -f $OUT ]]; then
  lines=$(wc -l < "$OUT")
  age=$(( $(date +%s) - $(stat -c %Y "$OUT") ))
  ok "$OUT exists — $lines lines, last written ${age}s ago"
  (( age > 300 )) && warn "nothing written in ${age}s; the timer runs every 60s"
  (( lines == 0 )) && bad "file is empty"
  echo "     document kinds:"
  python3 - "$OUT" <<'PY' 2>/dev/null || echo "       (could not parse — is it valid NDJSON?)"
import json,sys,collections
c=collections.Counter()
for l in open(sys.argv[1]):
    try: c[json.loads(l).get("hw",{}).get("kind","?")]+=1
    except Exception: c["UNPARSEABLE"]+=1
for k,v in c.most_common(): print("       %-16s %d" % (k,v))
PY
else
  bad "$OUT does not exist — run: /usr/local/bin/hwstats.py && head -1 $OUT"
fi

step "4. The agent"
if command -v elastic-agent >/dev/null 2>&1; then
  elastic-agent status 2>/dev/null | head -3 | sed 's/^/     /'
  if elastic-agent inspect 2>/dev/null | grep -q "$OUT"; then
    ok "the running agent config includes $OUT"
  else
    bad "the agent config does NOT mention $OUT"
    echo "     → this host is probably on the wrong policy. It needs proxmox-host,"
    echo "       not media-vm. Check Kibana → Fleet → Agents → this host."
  fi
else
  bad "elastic-agent is not installed on this host"
fi

cat <<EOF

Next, from the stack VM:
  cd ~/elastic-logging/stack && source .env
  curl -s --cacert certs/ca/ca.crt -u "elastic:\$ELASTIC_PASSWORD" \\
    "https://\$STACK_IP:9200/logs-proxmox_hw-default/_count?pretty"

A count above zero means the data is arriving and the problem is the dashboard's
time range. Zero means it stops somewhere above.
EOF
