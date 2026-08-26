#!/usr/bin/env bash
# Print the enrollment token for each agent policy, with the exact install
# command to run on the target machine.
set -euo pipefail
cd "$(dirname "$0")"

ENVF=../stack/.env
[[ -f $ENVF ]] || { echo "!! ../stack/.env not found."; exit 1; }
set -a
# .env is generated at deploy time, so there is nothing for shellcheck to follow.
# shellcheck disable=SC1090
. "$ENVF"
set +a

KB="https://localhost:${KIBANA_PORT}"

# -k rather than --cacert: this runs on the stack VM against its own loopback,
# and the certificate's SANs cover STACK_IP and STACK_DNS, not "localhost", so
# verification would fail on the hostname even with the right CA.
curl -fsS -k -u "elastic:${ELASTIC_PASSWORD}" -H 'kbn-xsrf: true' \
  "${KB}/api/fleet/enrollment_api_keys?perPage=100" \
| STACK_IP="$STACK_IP" FLEET_PORT="$FLEET_PORT" python3 -c '
import json, os, sys
url = "https://%s:%s" % (os.environ["STACK_IP"], os.environ["FLEET_PORT"])
script = {"proxmox-host": "proxmox", "media-vm": "vms", "stack-vm": "vms"}
for k in json.load(sys.stdin)["items"]:
    if not k.get("active"):
        continue
    pid = k["policy_id"]
    print("\n\033[1m%s\033[0m" % pid)
    print("  token: %s" % k["api_key"])
    d = script.get(pid)
    if d:
        print("  run on the target host (as root), after copying stack/certs/ca/ca.crt to it:")
        print("    ./%s/install-agent.sh --url %s --token %s --ca ./ca.crt"
              % (d, url, k["api_key"]))
'
echo
