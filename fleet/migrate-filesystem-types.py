#!/usr/bin/env python3
"""
Push filesystem.ignore_types onto the live System integration policies.

Without that var the filesystem metricset ignores every type marked `nodev` in
/proc/filesystems, and nfs, nfs4, cifs and zfs are all marked nodev. The result
is a host that reports its root filesystem and nothing else: no NAS mounts, no
ZFS datasets, no error, no warning, and a green agent throughout. Setting the
list explicitly — FS_IGNORE_TYPES in setup-policies.py — collects everything
that is not a pseudo filesystem instead.

setup-policies.py only ever *creates*, so editing that list changes nothing on
a stack whose policies already exist, and neither does restarting the stack.
This applies it to the policies that are already there.

    ./migrate-filesystem-types.py --check   # report what differs, change nothing
    ./migrate-filesystem-types.py           # apply

Agents pick the change up on their next check-in. Nothing is backfilled: the
mounts that were never collected have no history to recover, so the new rows
begin at the moment the agent reloads.
"""
import base64
import importlib.util
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CA = os.path.join(HERE, "..", "stack", "certs", "ca", "ca.crt")

# The stream this var lives on. The System integration compiles one input per
# metricset group; filesystem sits in the system/metrics one alongside cpu and
# memory, and every other stream there is left exactly as it was found.
INPUT_TYPE = "system/metrics"
DATASET = "system.filesystem"
VAR = "filesystem.ignore_types"


def load_generator():
    """Import setup-policies.py for FS_IGNORE_TYPES.

    The hyphen in the filename means this cannot be a plain import. Importing it
    reads ../stack/.env, which is the same file this script needs anyway, and
    makes no API calls until its main() runs.
    """
    path = os.path.join(HERE, "setup-policies.py")
    spec = importlib.util.spec_from_file_location("setup_policies", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GEN = load_generator()
ENV = GEN.ENV
KB = os.environ.get("KIBANA_URL") or "https://localhost:%s" % ENV.get("KIBANA_PORT", "5601")
CTX = None
if KB.startswith("https"):
    CTX = ssl.create_default_context(cafile=CA)
    # The certificate is issued for the LAN IP and DNS name, not "localhost".
    CTX.check_hostname = False


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


def filesystem_stream(policy):
    """The system.filesystem stream of one System integration policy, or None."""
    for inp in policy.get("inputs", []):
        if inp.get("type") != INPUT_TYPE:
            continue
        for st in inp.get("streams", []):
            if (st.get("data_stream") or {}).get("dataset") == DATASET:
                return inp, st
    return None, None


def summarise(types):
    """Say what a list *allows*, which is the half that matters here."""
    if not types:
        return "(unset — Metricbeat ignores every nodev type: nfs, cifs, zfs …)"
    real = [t for t in ("nfs", "nfs4", "cifs", "smbfs", "zfs", "ceph") if t in types]
    return "%d types, still ignoring %s" % (len(types), ", ".join(real)) if real \
        else "%d types, network and ZFS mounts collected" % len(types)


def main():
    check_only = bool(sys.argv[1:]) and sys.argv[1] in ("--check", "-n", "--dry-run")
    want = list(GEN.FS_IGNORE_TYPES)

    # Say where the desired value came from and what it is: run out of a stale
    # checkout and this script will otherwise report a contented "ok" for a list
    # that is not the one you think you are applying.
    print("\nFS_IGNORE_TYPES read from %s" % os.path.join(HERE, "setup-policies.py"))
    print("   %d types ignored: %s" % (len(want), ", ".join(want)))

    pps = [p for p in api("/api/fleet/package_policies?perPage=500")["items"]
           if p["package"]["name"] == "system"]
    if not pps:
        print("\nNo System integration policies found. Nothing collects filesystem "
              "metrics yet — run setup-policies.py first.")
        return 0

    todo = []
    print("\nSystem integration policies:")
    for p in sorted(pps, key=lambda x: x["name"]):
        inp, st = filesystem_stream(p)
        if st is None:
            print("   %-28s no %s stream — left alone" % (p["name"], DATASET))
            continue
        if not st.get("enabled", True):
            print("   %-28s stream disabled — left alone" % p["name"])
            continue
        cur = ((st.get("vars") or {}).get(VAR) or {}).get("value")
        if cur == want:
            print("   %-28s ok    %s" % (p["name"], summarise(cur)))
            continue
        print("   %-28s NEEDS UPDATING" % p["name"])
        print("        was  %s" % summarise(cur))
        print("        now  %s" % summarise(want))
        todo.append((p, st))

    if not todo:
        print("\nNothing to do.")
        return 0
    if check_only:
        print("\n--check given: nothing changed. Re-run without it to apply.")
        return 0

    print("\nApplying:")
    for p, st in todo:
        st.setdefault("vars", {})
        st["vars"].setdefault(VAR, {"type": "text"})["value"] = want
        body = dict(p)
        for k in ("id", "revision", "created_at", "created_by", "updated_at",
                  "updated_by", "elasticsearch", "agents", "policy_id",
                  "secret_references", "spaceIds"):
            body.pop(k, None)
        api("/api/fleet/package_policies/%s" % p["id"], "PUT", body)
        print("   updated %s" % p["name"])

    print("\nDone. Agents apply this on their next check-in, usually within a"
          "\nminute. Confirm with the data, not the agent's status — the mounts"
          "\nthat were missing were never an error:"
          "\n"
          "\n  GET metrics-system.filesystem-*/_search"
          "\n  {\"size\":0,\"aggs\":{\"t\":{\"terms\":{\"field\":\"system.filesystem.type\"}}}}"
          "\n"
          "\nA disk with no mounted filesystem — an LVM-thin pool, a ZFS zvol"
          "\nbacking a VM, an unformatted spare — still will not appear. There is"
          "\nno filesystem there to measure. ZFS pool capacity comes from"
          "\nhwstats.py instead, on the ZFS pools panel.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as exc:
        print("!! %s" % exc, file=sys.stderr)
        sys.exit(1)
