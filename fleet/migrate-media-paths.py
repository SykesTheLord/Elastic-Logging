#!/usr/bin/env python3
"""
Push the media-app log paths and multiline patterns onto the live policies.

setup-policies.py only ever *creates*. Once media-vm exists, editing MEDIA_APPS
in that file changes nothing on a running stack, and neither does restarting it:
the policies live in Fleet's saved objects, not in a file the stack reads at
boot. This applies the current MEDIA_APPS to the policies that already exist.

It imports MEDIA_APPS from setup-policies.py rather than restating the paths, so
the two cannot drift apart.

    ./migrate-media-paths.py --check   # report what differs, change nothing
    ./migrate-media-paths.py           # apply

Both the path and the multiline pattern are compared, because a wrong pattern is
the more damaging of the two: with negate/after, a pattern that never matches
glues every line of the file into 200-line documents rather than collecting
nothing, and the agent stays Healthy throughout.

Agents pick the change up on their next check-in. Documents already indexed are
not rewritten — for Custom Logs there is no event.original to re-parse from.
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
ENVF = os.path.join(HERE, "..", "stack", ".env")
CA = os.path.join(HERE, "..", "stack", "certs", "ca", "ca.crt")


def load_generator():
    """Import setup-policies.py for MEDIA_APPS and custom_log().

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


def wanted():
    """dataset -> (path, custom-config block) exactly as setup-policies.py builds it."""
    out = {}
    for dataset, path, ml in GEN.MEDIA_APPS:
        inputs = GEN.custom_log(dataset, [path], ml)
        stream = inputs["logs-logfile"]["streams"]["log.logs"]["vars"]
        out[dataset] = (stream["paths"], stream["custom"])
    return out


def first_line(text):
    for line in (text or "").splitlines():
        if line.startswith("multiline.pattern:"):
            return line.strip()
    return "(no multiline.pattern)"


def main():
    check_only = bool(sys.argv[1:]) and sys.argv[1] in ("--check", "-n", "--dry-run")
    want = wanted()

    # Always say where the desired values came from and what they are. This
    # script compares the live policy against MEDIA_APPS in *its own directory*,
    # so running it out of a stale checkout reports a contented "ok" for paths
    # that are wrong. Printing them makes that obvious instead of invisible.
    print("\nMEDIA_APPS read from %s" % os.path.join(HERE, "setup-policies.py"))
    for dataset, (paths, _) in sorted(want.items()):
        print("   %-12s %s" % (dataset, paths[0]))

    pps = [p for p in api("/api/fleet/package_policies?perPage=500")["items"]
           if p["package"]["name"] == "log"]
    if not pps:
        print("No Custom Logs integration policies found. Is the agent on media-vm?")
        return 0

    todo, seen = [], []
    print("\nCustom Logs streams:")
    for p in sorted(pps, key=lambda x: x["name"]):
        for inp in p["inputs"]:
            for st in inp.get("streams", []):
                v = st.get("vars", {}) or {}
                ds = (v.get("data_stream.dataset", {}) or {}).get("value")
                if not ds:
                    continue
                seen.append(ds)
                if ds not in want:
                    print("   %-12s not in MEDIA_APPS — left alone" % ds)
                    continue
                new_paths, new_custom = want[ds]
                cur_paths = (v.get("paths", {}) or {}).get("value") or []
                cur_custom = (v.get("custom", {}) or {}).get("value") or ""
                dp = cur_paths != new_paths
                dc = cur_custom.strip() != new_custom.strip()
                if not (dp or dc):
                    print("   %-12s ok    %s" % (ds, cur_paths[0] if cur_paths else "(none)"))
                    continue
                print("   %-12s NEEDS UPDATING" % ds)
                if dp:
                    print("        paths     %s" % (cur_paths or "(none)"))
                    print("               -> %s" % new_paths)
                if dc:
                    print("        multiline %s" % first_line(cur_custom))
                    print("               -> %s" % first_line(new_custom))
                todo.append((p, st, new_paths, new_custom))

    missing = [d for d in want if d not in seen]
    if missing:
        print("\n   in MEDIA_APPS but not on any policy: %s" % ", ".join(sorted(missing)))
        print("   add those as Custom Logs integrations in Fleet, or re-create the policy.")

    if not todo:
        print("\nNothing to do.")
        return 0
    if check_only:
        print("\n--check given: nothing changed. Re-run without it to apply.")
        return 0

    print("\nApplying:")
    for p, st, new_paths, new_custom in todo:
        st.setdefault("vars", {})
        st["vars"].setdefault("paths", {"type": "text"})["value"] = new_paths
        st["vars"].setdefault("custom", {"type": "yaml"})["value"] = new_custom
    for pid in dict.fromkeys(p["id"] for p, _, _, _ in todo):
        p = next(x for x, _, _, _ in todo if x["id"] == pid)
        body = dict(p)
        for k in ("id", "revision", "created_at", "created_by", "updated_at",
                  "updated_by", "elasticsearch", "agents", "policy_id",
                  "secret_references", "spaceIds"):
            body.pop(k, None)
        api("/api/fleet/package_policies/%s" % pid, "PUT", body)
        print("   updated %s" % p["name"])

    print("\nDone. Agents apply this on their next check-in, usually within a"
          "\nminute. Confirm with a document count rather than the agent's status:"
          "\na path that matches nothing is not an error and the agent stays green.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as exc:
        print("!! %s" % exc, file=sys.stderr)
        sys.exit(1)
