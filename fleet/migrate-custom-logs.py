#!/usr/bin/env python3
"""
Rewrite Custom Logs configurations that use filestream syntax on a log input.

The Custom Logs package compiles to the deprecated `log` input, not to
`filestream`. `parsers:` is a filestream-only option, so a `log` input ignores
it entirely and ships every line raw:

  * NDJSON is never decoded — hw.kind, hw.sensor.celsius and friends never
    exist, so the Proxmox temperature and SMART panels stay empty
  * multiline never joins — every stack trace line becomes its own document

The `log` input takes the same settings as top-level json.* and multiline.*
keys. This converts them in place on the live policies.

    ./migrate-custom-logs.py --check    # report only
    ./migrate-custom-logs.py            # convert

Agents pick the change up on their next check-in. Existing documents are not
rewritten — only data collected from now on is parsed correctly.
"""
import base64
import json
import os
import re
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
CTX = None
if KB.startswith("https"):
    CTX = ssl.create_default_context(cafile=CA)
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


def convert(custom):
    """filestream `parsers:` -> the log input's flat keys.

    Returns (new_text, note) or (None, reason) when the shape is not one this
    script generated — better to report it than to mangle a hand-edited config.
    """
    if "parsers:" not in custom:
        return None, "already converted"

    body = custom.split("parsers:", 1)[1]
    trailing = custom.split("parsers:", 1)[0].strip()

    if re.search(r'-\s*ndjson\s*:', body):
        opts = {"keys_under_root": "true"}   # from target: ""
        for k in ("overwrite_keys", "add_error_key", "expand_keys",
                  "ignore_decoding_error"):
            m = re.search(r'\b%s\s*:\s*(\S+)' % k, body)
            if m:
                opts[k] = m.group(1)
        if re.search(r'\btarget\s*:\s*(?!"")\S', body):
            return None, "ndjson uses a non-empty target — convert by hand"
        out = "".join("json.%s: %s\n" % (k, v) for k, v in opts.items())
        note = "ndjson -> json.*"
    elif re.search(r'-\s*multiline\s*:', body):
        opts = {}
        for k in ("type", "pattern", "negate", "match", "max_lines", "timeout"):
            m = re.search(r'\b%s\s*:\s*(.+)' % k, body)
            if m:
                opts[k] = m.group(1).strip()
        if "pattern" not in opts:
            return None, "multiline block has no pattern — convert by hand"
        out = "".join("multiline.%s: %s\n" % (k, v) for k, v in opts.items())
        note = "multiline -> multiline.*"
    else:
        return None, "unrecognised parsers block — convert by hand"

    if trailing:
        out = trailing + "\n" + out
    return out, note


def main():
    check_only = bool(sys.argv[1:]) and sys.argv[1] in ("--check", "-n", "--dry-run")

    pps = [p for p in api("/api/fleet/package_policies?perPage=500")["items"]
           if p["package"]["name"] == "log"]
    if not pps:
        print("No Custom Logs integration policies found.")
        return 0

    todo = []
    print("\nCustom Logs policies:")
    for p in sorted(pps, key=lambda x: x["name"]):
        for inp in p["inputs"]:
            for st in inp.get("streams", []):
                cur = (st.get("vars", {}).get("custom", {}) or {}).get("value") or ""
                new, note = convert(cur)
                ds = (st.get("vars", {}).get("data_stream.dataset", {}) or {}).get("value", "?")
                if new is None:
                    print("   %-34s %-16s %s" % (p["name"], ds, note))
                else:
                    print("   %-34s %-16s NEEDS FIXING (%s)" % (p["name"], ds, note))
                    todo.append((p, st, new))

    if not todo:
        print("\nNothing to do.")
        return 0
    if check_only:
        print("\n--check given: nothing changed. Re-run without it to apply.")
        return 0

    print("\nConverting:")
    for p, st, new in todo:
        st["vars"]["custom"]["value"] = new
    seen = set()
    for p, _, _ in todo:
        if p["id"] in seen:
            continue
        seen.add(p["id"])
        pid = p["id"]
        body = dict(p)
        for k in ("id", "revision", "created_at", "created_by", "updated_at",
                  "updated_by", "elasticsearch", "agents", "policy_id",
                  "secret_references", "spaceIds"):
            body.pop(k, None)
        api("/api/fleet/package_policies/%s" % pid, "PUT", body)
        print("   updated %s" % p["name"])

    print("\nDone. Agents apply this on their next check-in, usually within a"
          "\nminute. Documents already indexed are unchanged — only new data is"
          "\nparsed correctly.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as exc:
        print("!! %s" % exc, file=sys.stderr)
        sys.exit(1)
