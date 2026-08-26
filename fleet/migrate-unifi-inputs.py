#!/usr/bin/env python3
"""
Make the UniFi syslog listeners fit for a whole site, not one gateway.

UniFi pushes logging configuration site-wide and every adopted device sends to
the collector *directly* — the UDM Pro, a UX7, every AP and switch. Two package
defaults do not survive that:

  max_message_size  defaults to 10 KiB, and the udp input truncates a larger
                    datagram silently. Verified: a 20,078-byte datagram arrives
                    as exactly 10,240 bytes with no error logged anywhere. CEF
                    lines get long — long IPS signature names, and the Include
                    Raw Logs toggle adds full message bodies — so the tail of
                    the extension list is simply cut off, and what survives
                    still parses, which is why this is invisible.

  read_buffer       the socket receive buffer. Several devices logging a burst
                    at once overflow it and the kernel drops datagrams. UDP has
                    no retransmit, so those events are gone with no trace on
                    either side.

Also turns on preserve_original_event for the CEF stream, which is what lets
logs-cef.log@custom recover the sending device's hostname on the event classes
that carry no UNIFIdeviceName, and keeps the raw audit trail off the console.

setup-policies.py only ever *creates* policies, so editing it changes nothing on
a live stack. This edits the live ones.

    ./migrate-unifi-inputs.py --check    # report only
    ./migrate-unifi-inputs.py            # apply

Agents pick the change up on their next check-in. Nothing already indexed is
touched — see pipelines/migrate-unifi-cef.sh and backfill-unifi-iptables.sh.
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

# Asked for, not necessarily granted: the kernel caps SO_RCVBUF at
# net.core.rmem_max, which is 208 KiB on a stock Ubuntu. Raising it is a
# separate step and the script prints the command for it.
UDP_OPTIONS = "read_buffer: 8MiB\nmax_message_size: 64KiB\n"


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


def var(stream, name):
    return (stream.get("vars", {}) or {}).get(name, {}) or {}


def is_untouched(text):
    """True when udp_options is still the package default: comments only.

    A hand-written value is left alone and reported. Overwriting one would be a
    worse failure than the defaults this script exists to correct.
    """
    for line in (text or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return False
    return True


def plan_stream(pkg, stream):
    """Return a list of (var_name, type, new_value, note) for one stream."""
    todo = []

    cur = var(stream, "udp_options").get("value")
    if is_untouched(cur):
        todo.append(("udp_options", "yaml", UDP_OPTIONS,
                     "read_buffer 8MiB, max_message_size 64KiB"))
    elif "max_message_size" not in (cur or ""):
        todo.append((None, None, None,
                     "udp_options hand-edited and has no max_message_size — add it yourself"))

    if pkg == "cef":
        if var(stream, "preserve_original_event").get("value") is not True:
            todo.append(("preserve_original_event", "bool", True,
                         "keeps the raw line for device attribution and audit"))
        if var(stream, "ignore_empty_values").get("value") is not True:
            todo.append(("ignore_empty_values", "bool", True,
                         "UniFi sends empty extension values; without this they error"))
    return todo


def main():
    check_only = bool(sys.argv[1:]) and sys.argv[1] in ("--check", "-n", "--dry-run")

    pps = [p for p in api("/api/fleet/package_policies?perPage=500")["items"]
           if p["package"]["name"] in ("cef", "iptables")]
    if not pps:
        print("No cef or iptables integration policies found.")
        return 0

    changed = {}
    print("\nUniFi syslog listeners:")
    for p in sorted(pps, key=lambda x: x["name"]):
        pkg = p["package"]["name"]
        for inp in p["inputs"]:
            if not inp.get("enabled") or not inp.get("type", "").startswith("udp"):
                continue
            for st in inp.get("streams", []):
                if not st.get("enabled"):
                    continue
                host = var(st, "syslog_host").get("value")
                port = var(st, "syslog_port").get("value")
                print("\n   %-34s %s  %s:%s" % (p["name"], pkg, host, port))
                if host not in ("0.0.0.0", "::"):
                    print("      !! bound to %s — nothing on the network can reach it" % host)

                for name, vtype, value, note in plan_stream(pkg, st):
                    if name is None:
                        print("      .. %s" % note)
                        continue
                    print("      -> %-24s %s" % (name, note))
                    if not check_only:
                        st.setdefault("vars", {})
                        st["vars"].setdefault(name, {"type": vtype})
                        st["vars"][name]["value"] = value
                        changed[p["id"]] = p

    if not changed and not check_only:
        print("\nNothing to do.")
        return 0
    if check_only:
        print("\n--check given: nothing changed. Re-run without it to apply.")
        return 0

    print("\nApplying:")
    for pid, p in changed.items():
        body = dict(p)
        for k in ("id", "revision", "created_at", "created_by", "updated_at",
                  "updated_by", "elasticsearch", "agents", "policy_id",
                  "secret_references", "spaceIds"):
            body.pop(k, None)
        api("/api/fleet/package_policies/%s" % pid, "PUT", body)
        print("   updated %s" % p["name"])

    print("""
Done. Agents apply this on their next check-in.

Two things this script cannot do for you:

  1. Raise the kernel's receive-buffer ceiling on the stack VM, or read_buffer
     is silently capped at whatever net.core.rmem_max already is:

         echo 'net.core.rmem_max=8388608' | sudo tee /etc/sysctl.d/99-syslog.conf
         sudo sysctl --system

  2. Point UniFi's two syslog destinations at the right ports. They are
     configured on the device, in two different places, and sending the CEF one
     to the iptables port is what produces the grok failures this repo's
     pipelines/migrate-unifi-cef.sh exists to clean up:

         Control Plane -> Integrations -> Activity Logging (Syslog)   -> 9003
         CyberSecure   -> Traffic Logging -> Activity Logging (Syslog) -> 9001
""")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as exc:
        print("!! %s" % exc, file=sys.stderr)
        sys.exit(1)
