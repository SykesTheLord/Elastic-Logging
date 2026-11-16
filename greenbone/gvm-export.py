#!/usr/bin/env python3
"""
Ship finished Greenbone reports to Elasticsearch, one document per finding.

Lists every report gvmd has marked Done, skips the ones already exported, and
appends each remaining report's results to an NDJSON file that the Elastic
Agent on this VM tails (policy `greenbone`, dataset greenbone.finding). Only
once a report is fully written is its id recorded, so a crash part-way through
re-exports that report rather than losing it — at worst some findings arrive
twice, and the dashboard counts on greenbone.finding_key so duplicates do not
inflate anything.

The scanned machine goes in greenbone.host.*, not host.*: the agent stamps its
own host — this VM — over host.*, and logs-greenbone.finding@custom swaps the
two back at ingest (scanner → observer.*, scanned asset → host.*).

    gvm-export.py              # export anything new (the systemd timer runs this)
    gvm-export.py --dry-run    # print what would be exported, record nothing
    gvm-export.py --reexport   # forget the state file and export every report again

Configuration comes from /etc/gvm-export.conf (KEY=value, written by setup.sh).
Standard library only.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gmp import Gmp, GmpError, attr  # noqa: E402

CONF = os.environ.get("GVM_EXPORT_CONF", "/etc/gvm-export.conf")
STATE = os.environ.get("GVM_EXPORT_STATE", "/var/lib/gvm-export/state.json")
OUT = os.environ.get("GVM_EXPORT_OUT", "/var/log/elastic/greenbone.ndjson")
SOCKET = os.environ.get("GVM_SOCKET", "/run/gvmd/gvmd.sock")


def load_conf():
    conf = {}
    try:
        with open(CONF) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    conf[k.strip()] = v.strip().strip('"').strip("'")
    except OSError as exc:
        sys.exit("!! cannot read %s: %s" % (CONF, exc))
    return conf


def load_state():
    try:
        with open(STATE) as fh:
            return set(json.load(fh).get("exported", []))
    except (OSError, ValueError):
        return set()


def save_state(done):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(STATE))
    with os.fdopen(fd, "w") as fh:
        json.dump({"exported": sorted(done)}, fh)
    os.replace(tmp, STATE)


def band(score):
    """CVSS v3 qualitative rating. Greenbone's own UI stops at High; Critical
    is split out here because it is the band that means 'today'."""
    if score >= 9.0:
        return "Critical"
    if score >= 7.0:
        return "High"
    if score >= 4.0:
        return "Medium"
    if score > 0.0:
        return "Low"
    return "Log"


def iso(stamp):
    """gvmd times look like 2026-10-29T02:14:07Z or carry an offset."""
    if not stamp:
        return None
    try:
        t = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def tags(nvt):
    """The NVT's tag string is pipe-separated key=value pairs:
    cvss_base_vector=...|summary=...|insight=...|affected=...|impact=..."""
    out = {}
    for part in (nvt.findtext("tags") or "").split("|"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def result_doc(r, report_id, task_name, ended):
    nvt = r.find("nvt")
    host_el = r.find("host")
    ip = (host_el.text or "").strip() if host_el is not None else ""
    hostname = (host_el.findtext("hostname") or "").strip() if host_el is not None else ""
    score = float(r.findtext("severity") or 0)
    oid = nvt.get("oid") if nvt is not None else ""
    port = r.findtext("port") or ""
    t = tags(nvt) if nvt is not None else {}

    cves, urls = [], []
    refs = nvt.find("refs") if nvt is not None else None
    if refs is not None:
        for ref in refs.findall("ref"):
            if ref.get("type", "").lower() == "cve":
                cves.append(ref.get("id"))
            elif ref.get("type", "").lower() == "url":
                urls.append(ref.get("id"))
    sol = nvt.find("solution") if nvt is not None else None
    severity = band(score)
    name = nvt.findtext("name") if nvt is not None else r.findtext("name")

    ghost = {"ip": ip}
    if hostname:
        ghost["name"] = hostname
    doc = {
        "@timestamp": ended,
        "event": {"kind": "state", "category": ["vulnerability"], "type": ["info"],
                  "module": "greenbone", "dataset": "greenbone.finding"},
        "message": "%s %.1f %s on %s%s" % (severity, score, name, hostname or ip,
                                           " (%s)" % port if port else ""),
        "vulnerability": {
            "id": cves,
            "enumeration": "CVE" if cves else None,
            "severity": severity,
            "score": {"base": score},
            "classification": "CVSS",
            "category": [nvt.findtext("family")] if nvt is not None and nvt.findtext("family") else [],
            "description": t.get("summary") or r.findtext("description") or "",
            "reference": urls[:10],
            "scanner": {"vendor": "Greenbone"},
            "report_id": report_id,
        },
        "greenbone": {
            "host": ghost,
            "port": port,
            "threat": r.findtext("threat") or "",
            "qod": int(r.findtext("qod/value") or 0),
            "nvt": {"oid": oid, "name": name or "",
                    "cvss_vector": t.get("cvss_base_vector", "")},
            "insight": t.get("insight", ""),
            "detection": (r.findtext("description") or "")[:4000],
            "solution": (sol.text or "").strip() if sol is not None else "",
            "solution_type": sol.get("type", "") if sol is not None else "",
            "report_id": report_id,
            "task": task_name,
            # One finding = one NVT on one port of one host. Counting distinct
            # values of this, rather than documents, keeps a host scanned
            # twice inside the dashboard's window from counting double.
            "finding_key": "%s|%s|%s" % (ip, oid, port),
        },
    }
    if not doc["vulnerability"]["enumeration"]:
        del doc["vulnerability"]["enumeration"]
    return doc


def scanned_hosts(report):
    """One summary document per host the report covered, findings or not.

    Without these a host that came back clean is indistinguishable on the
    dashboard from one that was never scanned.
    """
    for h in report.findall("host"):
        ip = (h.findtext("ip") or "").strip()
        if not ip:
            continue
        details = {}
        for d in h.findall("detail"):
            details[d.findtext("name") or ""] = d.findtext("value") or ""
        counts = h.find("result_count")
        yield ip, details.get("hostname", ""), {
            "os": details.get("best_os_txt", ""),
            # gvmd records the outcome of the credentialed login as a host
            # detail. A failure here means the scan of that host was network-
            # only, which the dashboard needs to say — its findings are then
            # a fraction of the real picture.
            "auth_ssh": any(k.startswith("Auth-SSH-Success") for k in details),
            "auth_ssh_failed": any(k.startswith("Auth-SSH-Failure") for k in details),
            "start": iso(h.findtext("start")),
            "end": iso(h.findtext("end")),
            "results": int(counts.findtext("page") or 0) if counts is not None else 0,
        }


def export_report(g, report_id, min_qod):
    resp = g.command(
        "<get_reports report_id=%s details=\"1\" ignore_pagination=\"1\" filter=%s/>"
        % (attr(report_id),
           attr("apply_overrides=1 min_qod=%s rows=-1 levels=chml sort-reverse=severity" % min_qod)))
    rep = resp.find("report")
    inner = rep.find("report") if rep is not None and rep.find("report") is not None else rep
    task_name = rep.findtext("task/name") or ""
    ended = iso(inner.findtext("scan_end")) or iso(rep.findtext("modification_time")) \
        or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    docs = []
    for r in inner.iter("result"):
        # Notes and overrides embed nested <result> stubs with no <nvt>.
        if r.find("nvt") is None or r.find("host") is None:
            continue
        docs.append(result_doc(r, report_id, task_name, ended))

    for ip, name, info in scanned_hosts(inner):
        ghost = {"ip": ip}
        if name:
            ghost["name"] = name
        docs.append({
            "@timestamp": ended,
            "event": {"kind": "state", "category": ["host"], "type": ["info"],
                      "module": "greenbone", "dataset": "greenbone.finding"},
            "message": "scanned %s%s" % (name or ip,
                                         " (SSH login ok)" if info["auth_ssh"] else
                                         " (SSH login FAILED)" if info["auth_ssh_failed"] else ""),
            "greenbone": {"host": ghost, "report_id": report_id, "task": task_name,
                          "scan": dict(info, kind="host")},
        })
    return docs


def main(argv):
    dry = "--dry-run" in argv
    conf = load_conf()
    done = set() if "--reexport" in argv else load_state()
    min_qod = conf.get("MIN_QOD", "70")

    with Gmp(SOCKET) as g:
        g.authenticate(conf.get("GMP_USER", "admin"), conf["GREENBONE_ADMIN_PASSWORD"])
        listing = g.command("<get_reports details=\"0\" ignore_pagination=\"1\" "
                            "filter=%s/>" % attr("rows=-1 sort=date"))
        pending = []
        for rep in listing.findall("report"):
            rid = rep.get("id")
            inner = rep.find("report")
            status = (inner.findtext("scan_run_status") if inner is not None else None) \
                or rep.findtext("scan_run_status")
            if status == "Done" and rid not in done:
                pending.append(rid)

        if not pending:
            print("nothing new (%d reports already exported)" % len(done))
            return 0

        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        for rid in pending:
            docs = export_report(g, rid, min_qod)
            findings = sum(1 for d in docs if "vulnerability" in d)
            print("report %s: %d findings, %d hosts" % (rid, findings, len(docs) - findings))
            if dry:
                for d in docs[:3]:
                    print("   " + json.dumps(d)[:300])
                continue
            with open(OUT, "a") as fh:
                for d in docs:
                    fh.write(json.dumps(d, separators=(",", ":")) + "\n")
            done.add(rid)
            save_state(done)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (GmpError, OSError) as exc:
        print("!! %s" % exc, file=sys.stderr)
        sys.exit(1)
