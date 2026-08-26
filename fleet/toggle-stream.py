#!/usr/bin/env python3
"""
Enable or disable one data stream inside an existing Fleet integration policy.

setup-policies.py only ever *creates* policies — it deliberately leaves
existing ones alone so it cannot stomp changes made in the Fleet UI. This is
the counterpart for changing one that already exists, without clicking through
Kibana and without touching anything else in the policy.

    ./toggle-stream.py --list
    ./toggle-stream.py proxmox-host-linux-metrics linux.raid off
    ./toggle-stream.py proxmox-host-linux-metrics linux.raid on

The agent picks the change up on its next check-in, usually within a minute.
Nothing needs restarting on the monitored host.
"""
import base64
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ENVF = os.path.join(HERE, "..", "stack", ".env")
CA = os.path.join(HERE, "..", "stack", "certs", "ca", "ca.crt")


def load_env():
    env = {}
    with open(ENVF) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


ENV = load_env()
KB = os.environ.get("KIBANA_URL") or "https://localhost:%s" % ENV.get("KIBANA_PORT", "5601")
if KB.startswith("https"):
    CTX = ssl.create_default_context(cafile=CA)
    CTX.check_hostname = False          # the cert is for the LAN IP, not localhost
else:
    CTX = None


def api(path, method="GET", body=None):
    req = urllib.request.Request(KB + path, method=method)
    req.add_header("kbn-xsrf", "true")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Basic " + base64.b64encode(
        ("elastic:%s" % ENV["ELASTIC_PASSWORD"]).encode()).decode())
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data, context=CTX, timeout=120) as r:
            return json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError("%s %s -> %s: %s" % (method, path, e.code, e.read().decode()[:400]))


def policies():
    return api("/api/fleet/package_policies?perPage=500")["items"]


def do_list():
    for p in sorted(policies(), key=lambda x: x["name"]):
        print("\n%s  (%s)" % (p["name"], p["package"]["name"]))
        for inp in p["inputs"]:
            for st in inp.get("streams", []):
                print("   [%s] %s" % ("x" if st.get("enabled") else " ",
                                      st["data_stream"]["dataset"]))


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__.strip()); return 0
    if args[0] == "--list":
        do_list(); return 0
    if len(args) != 3 or args[2] not in ("on", "off"):
        print("usage: toggle-stream.py <integration-policy> <dataset> on|off")
        print("       toggle-stream.py --list")
        return 1

    name, dataset, want = args[0], args[1], args[2] == "on"

    matches = [p for p in policies() if p["name"] == name]
    if not matches:
        print("!! No integration policy called %r. Try --list." % name); return 1
    pp = matches[0]
    pp_id = pp["id"]

    found = False
    for inp in pp["inputs"]:
        for st in inp.get("streams", []):
            if st["data_stream"]["dataset"] == dataset:
                found = True
                if st.get("enabled") == want:
                    print("   %s is already %s — nothing to do" % (dataset, "on" if want else "off"))
                    return 0
                st["enabled"] = want
    if not found:
        print("!! %r has no stream %r. Try --list." % (name, dataset)); return 1

    # Fleet rejects its own server-managed fields if they are sent back.
    for k in ("id", "revision", "created_at", "created_by", "updated_at",
              "updated_by", "elasticsearch", "agents", "policy_id",
              "secret_references", "spaceIds"):
        pp.pop(k, None)

    api("/api/fleet/package_policies/%s" % pp_id, "PUT", pp)
    print("   %s: %s -> %s" % (name, dataset, "enabled" if want else "disabled"))

    for ap in pp.get("policy_ids", []):
        full = api("/api/fleet/agent_policies/%s/full" % ap)["item"]
        live = sorted(s["data_stream"]["dataset"]
                      for i in full.get("inputs", []) for s in i.get("streams", []))
        print("   %s now collects: %s" % (ap, ", ".join(live)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as exc:
        print("!! %s" % exc, file=sys.stderr)
        sys.exit(1)
