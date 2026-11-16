#!/usr/bin/env bash
# Print one paste-and-run command per agent-install use case: clone this repo
# into a hidden directory on the target host (so the enrollment scripts exist
# there without a manual copy), drop in the CA, and run the right installer
# with its enrollment token already filled in.
#
# Run on the stack VM, where stack/.env and stack/certs/ca/ca.crt live.
#
#   ./print-install-commands.sh              # every policy
#   ./print-install-commands.sh greenbone    # only the named ones
set -euo pipefail
ONLY="$*"
cd "$(dirname "$0")"

ENVF=../stack/.env
[[ -f $ENVF ]] || { echo "!! ../stack/.env not found."; exit 1; }
set -a
# .env is generated at deploy time, so there is nothing for shellcheck to follow.
# shellcheck disable=SC1090
. "$ENVF"
set +a

CA=../stack/certs/ca/ca.crt
[[ -f $CA ]] || { echo "!! $CA not found."; exit 1; }

REPO_URL=$(git -C .. remote get-url origin 2>/dev/null) || {
  echo "!! No git remote 'origin' on this checkout — a target host has nothing to clone."
  echo "   Push this repo somewhere reachable first, or set REPO_URL yourself and rerun."
  exit 1
}
case $REPO_URL in
  git@*) echo "!! origin is an SSH remote ($REPO_URL) — every target host needs a deploy key" \
              "that can read it, or the clone step below will hang on a host-key prompt or fail." ;;
esac

KB="https://localhost:${KIBANA_PORT}"
CA_B64=$(base64 -w0 "$CA")
# Left as a literal ~ on purpose — this string is only ever printed inside a
# command for the *target* host's shell to expand, never used as a path here.
# shellcheck disable=SC2088
HIDDEN='~/.elastic-logging'

# -k rather than --cacert: see enrollment-tokens.sh for why.
curl -fsS -k -u "elastic:${ELASTIC_PASSWORD}" -H 'kbn-xsrf: true' \
  "${KB}/api/fleet/enrollment_api_keys?perPage=100" \
| STACK_IP="$STACK_IP" FLEET_PORT="$FLEET_PORT" REPO_URL="$REPO_URL" \
  CA_B64="$CA_B64" HIDDEN="$HIDDEN" GREENBONE_SCAN_PUBKEY="${GREENBONE_SCAN_PUBKEY:-}" \
  ONLY="$ONLY" python3 -c '
import json, os, shlex, sys

url = "https://%s:%s" % (os.environ["STACK_IP"], os.environ["FLEET_PORT"])
repo_url = os.environ["REPO_URL"]
ca_b64 = os.environ["CA_B64"]
hidden = os.environ["HIDDEN"]

# One or more installers per policy — a policy that could plausibly run on a
# headless Arch box as well as Ubuntu/Debian lists both, since the installer
# is the only thing that differs; the target host, not this script, decides
# which one actually applies.
INSTALLERS = {
    "proxmox-host": [("Proxmox host",              "proxmox/install-agent.sh")],
    "media-vm":     [("Ubuntu/Debian media VM",    "vms/install-agent.sh")],
    "stack-vm":     [("Ubuntu/Debian stack VM",     "vms/install-agent.sh")],
    "baseline":     [("Ubuntu/Debian, headless",    "vms/install-agent.sh"),
                      ("Arch Linux, headless",       "vms/install-agent-arch.sh")],
    "minecraft":    [("Ubuntu/Debian",               "vms/install-agent.sh"),
                      ("Arch Linux, headless",        "vms/install-agent-arch.sh")],
    # Not just the agent: installs Docker and Greenbone, then enrols.
    "greenbone":    [("Ubuntu/Debian Greenbone VM", "greenbone/install.sh")],
}

# The public half of the key Greenbone logs in with (greenbone/setup.sh prints
# it). When set, every command also creates the scan account on the target.
scan_key = os.environ.get("GREENBONE_SCAN_PUBKEY", "").strip()
scan_arg = " --scan-key %s" % shlex.quote(scan_key) if scan_key else ""
only = os.environ.get("ONLY", "").split()

clone = "{h}/repo".format(h=hidden)
ca_path = "{h}/ca.crt".format(h=hidden)

for k in json.load(sys.stdin)["items"]:
    if not k.get("active"):
        continue
    pid = k["policy_id"]
    variants = INSTALLERS.get(pid)
    if not variants or (only and pid not in only):
        continue
    print("\n\033[1m%s\033[0m" % pid)
    for label, script in variants:
        print("  %s:" % label)
        cmd = (
            "mkdir -p {hidden} && echo {ca_b64} | base64 -d > {ca_path} && "
            "([ -d {clone}/.git ] && git -C {clone} pull --ff-only "
            "|| git clone --depth 1 {repo_url} {clone}) && "
            "sudo {clone}/{script} --url {url} --token {token} --ca {ca_path}{scan}"
        ).format(hidden=hidden, ca_b64=ca_b64, ca_path=ca_path, clone=clone,
                  repo_url=repo_url, script=script, url=url, token=k["api_key"],
                  # The Greenbone VM generates the key; it has nothing to receive.
                  scan="" if script.startswith("greenbone/") else scan_arg)
        print("    %s" % cmd)
'
echo
