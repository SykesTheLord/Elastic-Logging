#!/usr/bin/env python3
"""
Create (or bring up to date) the scan Greenbone runs on a schedule.

    ./provision.py --ready      # exit 0 once gvmd has finished loading its feeds
    ./provision.py              # create or update credential, target, schedule, task
    ./provision.py --scan-now   # ...and start the task once, now

Called by setup.sh, which exports greenbone/.env into the environment first.
Every object is found by name before anything is created, so a re-run changes
only what .env changed: new SCAN_TARGETS update the target, a new SCAN_RRULE
the schedule, a rotated key the credential.

Runs on the Greenbone VM as root (for the gvmd socket). Standard library only.
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gmp import Gmp, GmpError, attr, created_id, x  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
KEY = os.path.join(HERE, "secrets", "scan_key")

NAME = "homelab-lan"
CRED_NAME = "homelab-ssh"

# Feed-provided objects. These ids are fixed across every Greenbone feed
# release; they arrive with the data-objects feed, which is why --ready waits
# for them rather than only for gvmd to answer.
FULL_AND_FAST = "daf66e28-d2f2-11e0-ab68-406186ea4fc5"
ALL_IANA_TCP = "33d0cd82-57c6-11e1-8ed1-406186ea4fc5"
OPENVAS_SCANNER = "08b69003-5fc2-4037-a479-93b440211c73"

# The scanner sits behind Docker's NAT, so an ARP ping cannot see the LAN —
# hosts that answer neither ICMP nor a TCP ACK are reported dead and skipped.
ALIVE_TEST = "ICMP & TCP-ACK Service Ping"


def env(name, default=None):
    v = os.environ.get(name, default)
    if v is None:
        sys.exit("!! %s is not set — run this through setup.sh, which reads .env" % name)
    return v


def hosts_list(value):
    return ",".join(h.strip() for h in value.split(",") if h.strip())


def connect():
    g = Gmp().__enter__()
    g.authenticate("admin", env("GREENBONE_ADMIN_PASSWORD"))
    return g


def ready():
    """True once a scan could actually be created and run.

    gvmd answers long before it is useful: the VT, SCAP and data-object feeds
    load for an hour or more on a first boot, and until the data objects are in
    there is no scan config or port list to point a task at.
    """
    try:
        g = connect()
    except (OSError, GmpError) as exc:
        print("gvmd not answering yet (%s)" % exc)
        return False
    try:
        syncing = [f.findtext("name") or f.findtext("type")
                   for f in g.command("<get_feeds/>").iter("feed")
                   if f.find("currently_syncing") is not None]
        if syncing:
            print("feeds still loading: %s" % ", ".join(syncing))
            return False
        missing = []
        for kind, oid in (("config", FULL_AND_FAST), ("port_list", ALL_IANA_TCP),
                          ("scanner", OPENVAS_SCANNER)):
            try:
                g.command("<get_%ss %s_id=%s/>" % (kind, kind, attr(oid)))
            except GmpError:
                missing.append(kind)
        if missing:
            print("waiting for feed objects: %s" % ", ".join(missing))
            return False
        # Full and fast exists from the moment the data objects load, but it
        # has no NVT families until the VT feed is in, and a scan with it then
        # finishes in seconds having checked nothing.
        cfg = g.command("<get_configs config_id=%s families=\"1\"/>" % attr(FULL_AND_FAST))
        nvts = int(cfg.findtext("config/nvt_count") or 0)
        if nvts < 1000:
            print("scan config has %d NVTs so far — VT feed still loading" % nvts)
            return False
        print("ready: %d NVTs" % nvts)
        return True
    finally:
        g.__exit__()


def ensure_credential(g):
    with open(KEY) as fh:
        private = fh.read()
    user = env("SCAN_SSH_USER")
    body = "<login>%s</login><key><private>%s</private></key>" % (x(user), x(private))
    cid = g.find("credential", CRED_NAME)
    if cid:
        # Unconditional: the private key cannot be read back to compare, and
        # setting it again is harmless. A rotated key in secrets/ takes effect.
        g.command("<modify_credential credential_id=%s>%s</modify_credential>"
                  % (attr(cid), body))
        print("   credential %-14s updated" % CRED_NAME)
    else:
        cid = created_id(g.command(
            "<create_credential><name>%s</name><type>usk</type>"
            "<comment>SSH key for authenticated scans. Public half is on every host as %s.</comment>"
            "%s</create_credential>" % (x(CRED_NAME), x(user), body)))
        print("   credential %-14s created" % CRED_NAME)
    return cid


def target_xml(name, cid):
    excl = hosts_list(env("SCAN_EXCLUDE", ""))
    return (
        "<name>%s</name><hosts>%s</hosts>%s"
        "<port_list id=%s/>"
        "<ssh_credential id=%s><port>%s</port></ssh_credential>"
        "<alive_tests>%s</alive_tests>"
        % (x(name), x(hosts_list(env("SCAN_TARGETS"))),
           "<exclude_hosts>%s</exclude_hosts>" % x(excl) if excl else "",
           attr(ALL_IANA_TCP), attr(cid), x(env("SCAN_SSH_PORT", "22")), x(ALIVE_TEST)))


def target_matches(el, cid):
    def norm(s):
        return sorted(h.strip() for h in (s or "").split(",") if h.strip())
    ssh = el.find("ssh_credential")
    return (norm(el.findtext("hosts")) == norm(hosts_list(env("SCAN_TARGETS")))
            and norm(el.findtext("exclude_hosts")) == norm(env("SCAN_EXCLUDE", ""))
            and (el.find("port_list").get("id") if el.find("port_list") is not None else None) == ALL_IANA_TCP
            and (ssh.get("id") if ssh is not None else None) == cid
            and (ssh.findtext("port") if ssh is not None else None) == env("SCAN_SSH_PORT", "22")
            and (el.findtext("alive_tests") or "") == ALIVE_TEST)


def ensure_target(g, cid, task_el):
    """The target the task should point at, created or updated as needed.

    gvmd refuses to change the hosts of a target any task uses — which is every
    target that has ever been scanned. So a changed SCAN_TARGETS becomes a new
    target with a dated name, the task is repointed at it, and the old one is
    deleted once nothing references it.
    """
    current = None
    if task_el is not None and task_el.find("target") is not None:
        current = task_el.find("target").get("id")
    if current is None:
        current = g.find("target", NAME)
    if current:
        el = g.command("<get_targets target_id=%s/>" % attr(current)).find("target")
        if target_matches(el, cid):
            print("   target     %-14s unchanged" % el.findtext("name"))
            return current, False
        try:
            g.command("<modify_target target_id=%s>%s</modify_target>"
                      % (attr(current), target_xml(el.findtext("name"), cid)))
            print("   target     %-14s updated" % el.findtext("name"))
            return current, False
        except GmpError as exc:
            print("   target     %-14s in use (%s) — replacing it" % (el.findtext("name"), exc.status))
        name = "%s %s" % (NAME, datetime.datetime.now().strftime("%Y-%m-%d %H%M%S"))
    else:
        name = NAME
    tid = created_id(g.command("<create_target>%s</create_target>" % target_xml(name, cid)))
    print("   target     %-14s created" % name)
    return tid, current


def icalendar():
    """A VCALENDAR for SCAN_START + SCAN_RRULE, anchored in UTC.

    gvmd interprets the recurrence in the schedule's <timezone>, so DTSTART is
    given as the UTC instant of the first local occurrence rather than with a
    TZID gvmd would need a VTIMEZONE block for.
    """
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(env("SCAN_TZ", "UTC"))
    hh, mm = (int(p) for p in env("SCAN_START", "02:00").split(":"))
    now = datetime.datetime.now(tz)
    start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if start <= now:
        start += datetime.timedelta(days=1)
    utc = start.astimezone(datetime.timezone.utc)
    return "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//homelab//elastic-logging//EN",
        "BEGIN:VEVENT",
        "UID:homelab-lan-scan@elastic-logging",
        "DTSTAMP:%s" % datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "DTSTART:%s" % utc.strftime("%Y%m%dT%H%M%SZ"),
        "RRULE:%s" % env("SCAN_RRULE"),
        "END:VEVENT", "END:VCALENDAR", ""])


def ensure_schedule(g):
    body = ("<icalendar>%s</icalendar><timezone>%s</timezone>"
            % (x(icalendar()), x(env("SCAN_TZ", "UTC"))))
    sid = g.find("schedule", NAME)
    if sid:
        el = g.command("<get_schedules schedule_id=%s/>" % attr(sid)).find("schedule")
        ical = el.findtext("icalendar") or ""
        if ("RRULE:%s" % env("SCAN_RRULE")) in ical and el.findtext("timezone") == env("SCAN_TZ", "UTC") \
                and _start_matches(ical):
            print("   schedule   %-14s unchanged" % NAME)
            return sid
        g.command("<modify_schedule schedule_id=%s><name>%s</name>%s</modify_schedule>"
                  % (attr(sid), x(NAME), body))
        print("   schedule   %-14s updated" % NAME)
        return sid
    sid = created_id(g.command("<create_schedule><name>%s</name>%s</create_schedule>"
                               % (x(NAME), body)))
    print("   schedule   %-14s created" % NAME)
    return sid


def _start_matches(ical):
    """Whether the stored DTSTART still lands on SCAN_START local time."""
    from zoneinfo import ZoneInfo
    for line in ical.splitlines():
        if line.startswith("DTSTART"):
            stamp = line.split(":", 1)[1].strip()
            try:
                t = datetime.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=datetime.timezone.utc)
            except ValueError:
                return False
            local = t.astimezone(ZoneInfo(env("SCAN_TZ", "UTC")))
            return local.strftime("%H:%M") == env("SCAN_START", "02:00")
    return False


def task_preferences():
    # assets_min_qod also filters what the exporter will see, so keep it with
    # MIN_QOD. in_assets feeds the host asset database the GSA UI shows.
    prefs = {"in_assets": "yes", "assets_min_qod": env("MIN_QOD", "70"),
             "assets_apply_overrides": "yes"}
    return "<preferences>%s</preferences>" % "".join(
        "<preference><scanner_name>%s</scanner_name><value>%s</value></preference>"
        % (x(k), x(v)) for k, v in prefs.items())


def ensure_task(g, task_id, task_el, target_id, schedule_id):
    if task_id:
        # gvmd rejects a <target> on a task that has run unless the task was
        # created alterable (below), and only sends it when it changed so an
        # older non-alterable task still accepts the schedule update.
        current = task_el.find("target").get("id") if task_el.find("target") is not None else None
        tgt = "<target id=%s/>" % attr(target_id) if target_id != current else ""
        g.command("<modify_task task_id=%s>%s<schedule id=%s/>%s</modify_task>"
                  % (attr(task_id), tgt, attr(schedule_id), task_preferences()))
        print("   task       %-14s updated" % NAME)
        return task_id
    # alterable: without it the target of a task with reports is frozen, and a
    # change to SCAN_TARGETS could only be applied by starting a new task.
    task_id = created_id(g.command(
        "<create_task><name>%s</name>"
        "<comment>Scheduled scan of SCAN_TARGETS. Results ship to Elasticsearch via gvm-export.py.</comment>"
        "<alterable>1</alterable>"
        "<config id=%s/><target id=%s/><scanner id=%s/><schedule id=%s/>%s"
        "</create_task>"
        % (x(NAME), attr(FULL_AND_FAST), attr(target_id), attr(OPENVAS_SCANNER),
           attr(schedule_id), task_preferences())))
    print("   task       %-14s created" % NAME)
    return task_id


def main(argv):
    if "--ready" in argv:
        return 0 if ready() else 1

    g = connect()
    try:
        task_id = g.find("task", NAME)
        task_el = None
        if task_id:
            task_el = g.command("<get_tasks task_id=%s/>" % attr(task_id)).find("task")

        cid = ensure_credential(g)
        target_id, old_target = ensure_target(g, cid, task_el)
        schedule_id = ensure_schedule(g)
        task_id = ensure_task(g, task_id, task_el, target_id, schedule_id)

        if old_target and old_target != target_id:
            try:
                g.command("<delete_target target_id=%s ultimate=\"1\"/>" % attr(old_target))
                print("   target     old one deleted")
            except GmpError as exc:
                print("   ! old target %s left in place: %s" % (old_target, exc))

        if "--scan-now" in argv:
            status = g.command("<get_tasks task_id=%s/>" % attr(task_id)).findtext("task/status")
            if status in ("Running", "Requested", "Queued"):
                print("   scan already %s" % status.lower())
            else:
                g.command("<start_task task_id=%s/>" % attr(task_id))
                print("   scan started")
    finally:
        g.__exit__()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except GmpError as exc:
        print("!! %s" % exc, file=sys.stderr)
        sys.exit(1)
