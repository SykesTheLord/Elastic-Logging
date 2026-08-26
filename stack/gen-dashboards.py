#!/usr/bin/env python3
"""
Generate a Grafana dashboard for every host running an Elastic Agent, plus a
set of dashboards for the network-device receivers.

Hosts are discovered from Fleet rather than listed in this file, and the panels
each one gets are chosen from what its agent policy actually collects. Add the
Docker integration to a policy and that host's dashboard grows a container row
on the next run; point a new Custom Logs input somewhere and its dataset gets
its own panels. Nothing here needs editing when the estate changes.

    ./gen-dashboards.py              # write or refresh every dashboard
    ./gen-dashboards.py --list       # show what was discovered, write nothing
    ./gen-dashboards.py --dry-run    # show which files would change

Output goes to grafana/provisioning/dashboards/json/generated/, which the
Grafana file provisioner already watches, so dashboards appear within
updateIntervalSeconds (30s). No Grafana API call, no restart, no credentials
beyond the ones already in .env.

Files are rewritten only when their content actually changes, so a re-run does
not reset UI edits on dashboards nobody's data moved. Dashboards for hosts
Fleet no longer lists are removed. Only files carrying the "generated" tag are
ever written or deleted: hand-written dashboards in the parent directory —
homelab-overview.json included — are never touched.

Runs on the stack VM: it reaches Kibana and Elasticsearch on localhost and
reads ../stack/.env and ../stack/certs/.
"""
import base64
import hashlib
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ENVF = os.path.join(HERE, ".env")
CA = os.path.join(HERE, "certs", "ca", "ca.crt")
OUTDIR = os.path.join(HERE, "grafana", "provisioning", "dashboards", "json", "generated")

# Every dashboard this script writes carries this tag, and it is the only thing
# that makes a file eligible to be overwritten or pruned. Nothing else in the
# output directory is read or modified.
MARKER = "generated"

DS_LOGS = {"type": "elasticsearch", "uid": "es-logs"}
DS_METRICS = {"type": "elasticsearch", "uid": "es-metrics"}

ERROR_Q = "log.level:(error OR fatal OR critical)"


def load_env():
    if not os.path.exists(ENVF):
        sys.exit("!! %s not found. Copy .env.example to .env first." % ENVF)
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
ES = os.environ.get("ES_URL") or "https://localhost:%s" % ENV.get("ES_PORT", "9200")

if KB.startswith("https") or ES.startswith("https"):
    if not os.path.exists(CA):
        sys.exit("!! %s not found — run stack/setup-certs.sh first, or set "
                 "KIBANA_URL and ES_URL to plain http." % CA)
    CTX = ssl.create_default_context(cafile=CA)
    # The certificate is issued for the LAN IP and DNS name, not "localhost".
    CTX.check_hostname = False
else:
    CTX = None


def api(base, path, method="GET", body=None):
    req = urllib.request.Request(base + path, method=method)
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
    except urllib.error.URLError as e:
        raise RuntimeError("cannot reach %s: %s" % (base, e.reason))


# ── Discovery ───────────────────────────────────────────────────────────────

def fleet_agents():
    """Every enrolled agent, online or not.

    An agent that is merely offline still deserves its dashboard — that is
    exactly when you want to look at one. Only agents that have actually been
    unenrolled drop out, which is what makes pruning safe.
    """
    items = api(KB, "/api/fleet/agents?perPage=500&showInactive=true").get("items", [])
    agents = []
    for a in items:
        if a.get("status") == "unenrolled" or a.get("unenrolled_at"):
            continue
        meta = (a.get("local_metadata") or {}).get("host") or {}
        hostname = meta.get("hostname") or meta.get("name")
        if not hostname:
            continue
        agents.append({
            "hostname": hostname,
            "policy_id": a.get("policy_id") or "",
            "status": a.get("status") or "unknown",
            "version": (((a.get("local_metadata") or {}).get("elastic") or {})
                        .get("agent") or {}).get("version", "?"),
        })
    # Two agents on one hostname would otherwise produce two identical files.
    seen, unique = set(), []
    for a in sorted(agents, key=lambda x: x["hostname"].lower()):
        if a["hostname"].lower() in seen:
            continue
        seen.add(a["hostname"].lower())
        unique.append(a)
    return unique


def policy_capabilities():
    """What each agent policy collects, keyed by agent policy id.

    Read from the live integration policies rather than from setup-policies.py,
    which only ever seeds them — anything changed in the Fleet UI or by
    toggle-stream.py since then is reflected here and nowhere else.
    """
    caps = {}
    for pp in api(KB, "/api/fleet/package_policies?perPage=500").get("items", []):
        pkg = (pp.get("package") or {}).get("name", "")
        targets = pp.get("policy_ids") or ([pp["policy_id"]] if pp.get("policy_id") else [])
        for pid in targets:
            c = caps.setdefault(pid, {"packages": set(), "datasets": [], "receivers": {}})
            c["packages"].add(pkg)
            for inp in pp.get("inputs", []):
                if not inp.get("enabled", True):
                    continue
                for st in inp.get("streams", []):
                    if not st.get("enabled", True):
                        continue
                    v = st.get("vars") or {}
                    ds = (v.get("data_stream.dataset") or {}).get("value")
                    if pkg == "log" and ds and ds not in c["datasets"]:
                        c["datasets"].append(ds)
                    # Syslog and NetFlow listeners, so the dashboards can name
                    # the port the device is actually expected to send to.
                    port = (v.get("syslog_port") or v.get("port") or {}).get("value")
                    if pkg in ("qnap_nas", "cef", "iptables", "netflow") and port:
                        c["receivers"][pkg] = int(port)
    return caps


def es_host_names():
    """The host.name values Elasticsearch has actually seen.

    Fleet reports the agent's hostname; documents are queried by host.name.
    They are normally the same string, but not always — an FQDN or a case
    difference here is the difference between a dashboard and twelve empty
    panels, and it fails silently in exactly the way nothing else catches.
    """
    body = {
        "size": 0,
        "query": {"range": {"@timestamp": {"gte": "now-7d"}}},
        "aggs": {"h": {"terms": {"field": "host.name", "size": 200}}},
    }
    try:
        r = api(ES, "/logs-*,metrics-*/_search?ignore_unavailable=true&allow_no_indices=true",
                "POST", body)
    except RuntimeError as exc:
        print("   ! could not query Elasticsearch for host.name (%s)" % exc)
        return None
    return [b["key"] for b in (r.get("aggregations", {}).get("h", {}).get("buckets", []))]


def resolve_host_name(hostname, seen):
    """Map a Fleet hostname onto the host.name the documents really carry."""
    if seen is None:
        return hostname, None
    if hostname in seen:
        return hostname, None
    low = {s.lower(): s for s in seen}
    if hostname.lower() in low:
        return low[hostname.lower()], None
    # pve.lan in Fleet against pve in the documents, or the other way round.
    short = hostname.split(".")[0].lower()
    for s in seen:
        if s.split(".")[0].lower() == short:
            return s, "Fleet calls it %r, documents say %r — using the latter" % (hostname, s)
    return hostname, "no documents carry host.name:%r yet" % hostname


# ── Grafana panel construction ──────────────────────────────────────────────

def date_hist(mid="2"):
    return {"id": mid, "type": "date_histogram", "field": "@timestamp",
            "settings": {"interval": "auto", "min_doc_count": "0", "trimEdges": "0"}}


def terms(field, size=10, mid="3", order_by="_count", missing=None, order="desc"):
    """A terms bucket.

    `missing` matters more than it looks. A terms aggregation silently drops
    documents that do not have the field, so a table grouped by host.name shows
    nothing at all for the network devices — which do not set one — rather than
    showing them with the column blank. Setting it puts them on the page. In a
    nested bucket the effect is worse: the *outer* row disappears too, so a
    table can lose half its content while still looking populated.

    The value is parsed as the field's own type. A string sentinel on source.ip
    ("'(none)' is not an IP string literal") or on destination.port ("For input
    string") throws, and the panel then renders empty rather than erroring
    visibly — so keyword fields get a word, numeric fields get a number, and ip
    fields get no missing bucket at all.

    `order` matters only alongside order_by="_term", where the "desc" default
    lists a column of device names or unit names backwards.
    """
    settings = {"size": str(size), "order": order, "orderBy": order_by,
                "min_doc_count": "1"}
    if missing is not None:
        settings["missing"] = missing
    return {"id": mid, "type": "terms", "field": field, "settings": settings}


def count():
    return [{"id": "1", "type": "count"}]


def metric(kind, field, mid="1"):
    return [{"id": mid, "type": kind, "field": field}]


def rate(field):
    """Per-second rate of a monotonic counter.

    system.network.* and system.diskio.* are totals since boot, so charting the
    value itself just draws a line that goes up forever. The derivative is
    normalised to 1s by Elasticsearch, which also makes it independent of the
    bucket width Grafana picks for the time range.
    """
    return [{"id": "1", "type": "max", "field": field, "hide": True},
            {"id": "3", "type": "derivative", "field": "1", "pipelineAgg": "1",
             "settings": {"unit": "1s"}}]


def target(ref, query, metrics, buckets, alias="", ds=None):
    return {"datasource": ds or DS_LOGS, "query": query, "alias": alias, "refId": ref,
            "metrics": metrics, "bucketAggs": buckets, "timeField": "@timestamp"}


def thresholds(steps):
    return {"mode": "absolute", "steps": steps}


def stat(title, targets, unit="short", description="", steps=None, calc="sum",
         ds=None, text_mode="auto"):
    return {
        "type": "stat", "title": title, "description": description,
        "datasource": ds or DS_LOGS, "targets": targets,
        "options": {
            "reduceOptions": {"calcs": [calc], "fields": "", "values": False},
            "orientation": "auto", "textMode": text_mode, "colorMode": "value",
            "graphMode": "area", "justifyMode": "auto",
        },
        "fieldConfig": {"defaults": {
            "unit": unit, "color": {"mode": "thresholds"},
            "thresholds": thresholds(steps or [{"color": "text", "value": None}]),
        }, "overrides": []},
    }


def timeseries(title, targets, unit="short", description="", style="line",
               stack=False, ds=None, fill=0, soft_min=None, overrides=None):
    return {
        "type": "timeseries", "title": title, "description": description,
        "datasource": ds or DS_LOGS, "targets": targets,
        "options": {
            "legend": {"displayMode": "table", "placement": "bottom",
                       "showLegend": True, "calcs": []},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
        "fieldConfig": {"defaults": {
            "unit": unit,
            "custom": {
                "drawStyle": style, "lineWidth": 2,
                "fillOpacity": 100 if style == "bars" else fill,
                "gradientMode": "none", "showPoints": "never", "pointSize": 8,
                "spanNulls": False,
                "lineInterpolation": "linear" if style == "bars" else "smooth",
                "barAlignment": 0, "axisSoftMin": soft_min, "axisSoftMax": None,
                "stacking": {"mode": "normal" if stack else "none", "group": "A"},
                "axisPlacement": "auto", "axisLabel": "",
                "scaleDistribution": {"type": "linear"},
                "hideFrom": {"legend": False, "tooltip": False, "viz": False},
            },
            "color": {"mode": "palette-classic"},
        }, "overrides": overrides or []},
    }


def table(title, targets, description="", ds=None, overrides=None):
    return {
        "type": "table", "title": title, "description": description,
        "datasource": ds or DS_LOGS, "targets": targets,
        "options": {"showHeader": True, "cellHeight": "sm",
                    "footer": {"show": False, "reducer": ["sum"],
                               "countRows": False, "fields": ""}},
        "fieldConfig": {"defaults": {"custom": {
            "align": "auto", "cellOptions": {"type": "auto"}, "inspect": False,
        }}, "overrides": overrides or []},
    }


def logs_panel(title, query, description="", limit="300"):
    return {
        "type": "logs", "title": title, "description": description,
        "datasource": DS_LOGS, "maxDataPoints": 100,
        "targets": [target("A", query,
                           [{"id": "1", "type": "logs", "settings": {"limit": limit}}],
                           [date_hist()])],
        "options": {"showTime": True, "showLabels": False, "showCommonLabels": False,
                    "wrapLogMessage": True, "prettifyLogMessage": False,
                    "enableLogDetails": True, "dedupStrategy": "none",
                    "sortOrder": "Descending"},
        "fieldConfig": {"defaults": {}, "overrides": []},
    }


def rename(field, title, unit=None, steps=None):
    """A table column override: give a metric a human name, unit and colour."""
    props = [{"id": "displayName", "value": title}]
    if unit:
        props.append({"id": "unit", "value": unit})
    if steps:
        props.append({"id": "custom.cellOptions", "value": {"type": "color-text"}})
        props.append({"id": "thresholds", "value": thresholds(steps)})
    return {"matcher": {"id": "byName", "options": field}, "properties": props}


def last_seen(title, query, description=""):
    """How long ago this source last produced an event.

    The single most useful panel for anything that pushes rather than being
    polled: a silent QNAP and a healthy idle one look identical on a count.
    Read from the last non-empty bucket, so it is accurate to the bucket width
    and needs no data outside the dashboard's time range.
    """
    return stat(title, [target("A", query, metric("max", "@timestamp"), [date_hist()])],
                unit="dateTimeFromNow", description=description, calc="lastNotNull",
                steps=[{"color": "text", "value": None}], text_mode="value")


class Board:
    """A dashboard under construction, packing panels left to right."""

    def __init__(self, uid, title, description, tags, link_tag=None,
                 variables=None, time_from="now-24h", refresh="1m"):
        self.uid, self.title, self.description = uid, title, description
        self.tags = tags
        self.link_tag = link_tag
        self.variables = variables or []
        self.time_from, self.refresh = time_from, refresh
        self.panels = []
        self._y = self._x = self._rowh = self._id = 0

    def _nid(self):
        self._id += 1
        return self._id

    def _newline(self):
        if self._x:
            self._y += self._rowh
            self._x = self._rowh = 0

    def row(self, title):
        self._newline()
        self.panels.append({"type": "row", "title": title, "id": self._nid(),
                            "collapsed": False, "panels": [],
                            "gridPos": {"h": 1, "w": 24, "x": 0, "y": self._y}})
        self._y += 1
        return self

    def add(self, panel, w, h):
        if self._x + w > 24:
            self._newline()
        panel["id"] = self._nid()
        panel["gridPos"] = {"h": h, "w": w, "x": self._x, "y": self._y}
        self.panels.append(panel)
        self._x += w
        self._rowh = max(self._rowh, h)
        return self

    def to_dict(self):
        links = []
        if self.link_tag:
            links.append({"asDropdown": True, "icon": "external link",
                          "includeVars": False, "keepTime": True,
                          "tags": [self.link_tag], "targetBlank": False,
                          "title": self.link_tag.capitalize(), "tooltip": "",
                          "type": "dashboards", "url": ""})
        return {
            "uid": self.uid,
            "title": self.title,
            "description": self.description,
            "tags": sorted(set(self.tags) | {MARKER}),
            "timezone": "browser",
            "schemaVersion": 39,
            "editable": True,
            "graphTooltip": 1,
            "refresh": self.refresh,
            "time": {"from": self.time_from, "to": "now"},
            "timepicker": {},
            "templating": {"list": self.variables},
            "links": links,
            "version": 1,
            "panels": self.panels,
        }


# ── Per-host dashboards ─────────────────────────────────────────────────────

def host_dashboard(agent, host_name, caps, stem):
    """One dashboard for one host, built from what its policy collects."""
    pkgs = caps.get("packages", set())
    datasets = list(caps.get("datasets", []))
    has_hw = "proxmox_hw" in datasets
    if has_hw:
        datasets.remove("proxmox_hw")

    hq = 'host.name:"$host"'
    b = Board(
        uid="host-" + stem,
        title="Host — %s" % host_name,
        description=("Agent %s on policy %s. Generated by stack/gen-dashboards.py "
                     "from what that policy collects — edit the policy, not this file."
                     % (agent["version"], agent["policy_id"] or "unknown")),
        tags=["homelab", "elastic", "host", agent["policy_id"] or "unassigned"],
        link_tag="host",
        # A constant rather than a query variable: this dashboard is *for* this
        # host, and a picker that can silently point it at another one turns
        # every panel into a lie about its own title.
        variables=[{"name": "host", "label": "Host", "type": "constant",
                    "query": host_name, "current": {"text": host_name, "value": host_name},
                    "hide": 2, "skipUrlSync": False}],
    )

    pct = [{"color": "green", "value": None}, {"color": "orange", "value": 0.8},
           {"color": "red", "value": 0.92}]

    b.add(stat("CPU", [target("A", 'event.dataset:system.cpu AND %s' % hq,
                              metric("avg", "system.cpu.total.norm.pct"), [date_hist()],
                              ds=DS_METRICS)],
               unit="percentunit", calc="lastNotNull", steps=pct, ds=DS_METRICS,
               description="Normalised across every core, so 100% means the whole box."), 4, 4)
    b.add(stat("Memory", [target("A", 'event.dataset:system.memory AND %s' % hq,
                                 metric("avg", "system.memory.actual.used.pct"), [date_hist()],
                                 ds=DS_METRICS)],
               unit="percentunit", calc="lastNotNull", steps=pct, ds=DS_METRICS,
               description="Actual used — excludes cache and buffers, which Linux will "
                           "hand back under pressure."), 4, 4)
    b.add(stat("Root filesystem", [target("A",
               'event.dataset:system.filesystem AND %s AND system.filesystem.mount_point:"/"' % hq,
               metric("max", "system.filesystem.used.pct"), [date_hist()], ds=DS_METRICS)],
               unit="percentunit", calc="lastNotNull", ds=DS_METRICS,
               steps=[{"color": "green", "value": None}, {"color": "orange", "value": 0.8},
                      {"color": "red", "value": 0.9}]), 4, 4)
    b.add(stat("Load (1m)", [target("A", 'event.dataset:system.load AND %s' % hq,
                                    metric("avg", "system.load.norm.1"), [date_hist()],
                                    ds=DS_METRICS)],
               unit="short", calc="lastNotNull", ds=DS_METRICS,
               steps=[{"color": "green", "value": None}, {"color": "orange", "value": 1},
                      {"color": "red", "value": 2}],
               description="Normalised per core: 1.0 means fully committed, whatever the "
                           "core count."), 4, 4)
    b.add(stat("Uptime", [target("A", 'event.dataset:system.uptime AND %s' % hq,
                                 metric("max", "system.uptime.duration.ms"), [date_hist()],
                                 ds=DS_METRICS)],
               unit="dtdurationms", calc="lastNotNull", ds=DS_METRICS,
               text_mode="value"), 4, 4)
    b.add(stat("Errors", [target("A", "%s AND %s" % (hq, ERROR_Q), count(), [date_hist()])],
               steps=[{"color": "text", "value": None}, {"color": "orange", "value": 1},
                      {"color": "red", "value": 100}],
               description="Every dataset on this host, at error or worse."), 4, 4)

    # ── Resources ──
    b.row("Resources")
    b.add(timeseries("CPU by state",
                     [target(r, 'event.dataset:system.cpu AND %s' % hq,
                             metric("avg", "system.cpu.%s.norm.pct" % s), [date_hist()],
                             alias=s, ds=DS_METRICS)
                      for r, s in zip("ABCDE", ("user", "system", "iowait", "steal", "nice"))],
                     unit="percentunit", stack=True, style="line", fill=40, ds=DS_METRICS,
                     description="iowait means waiting on disk; steal means the hypervisor "
                                 "gave the CPU to someone else."), 12, 8)
    b.add(timeseries("Memory",
                     [target("A", 'event.dataset:system.memory AND %s' % hq,
                             metric("avg", "system.memory.actual.used.bytes"), [date_hist()],
                             alias="used", ds=DS_METRICS),
                      target("B", 'event.dataset:system.memory AND %s' % hq,
                             metric("avg", "system.memory.actual.free"), [date_hist()],
                             alias="free", ds=DS_METRICS),
                      target("C", 'event.dataset:system.memory AND %s' % hq,
                             metric("avg", "system.memory.swap.used.bytes"), [date_hist()],
                             alias="swap used", ds=DS_METRICS)],
                     unit="bytes", ds=DS_METRICS,
                     description="Swap climbing on a VM with ballooning off is worth "
                                 "chasing — see the README on balloon=0."), 12, 8)
    b.add(timeseries("Filesystem used",
                     [target("A", 'event.dataset:system.filesystem AND %s' % hq,
                             metric("max", "system.filesystem.used.pct"),
                             [terms("system.filesystem.mount_point", 12), date_hist()],
                             alias="{{term system.filesystem.mount_point}}", ds=DS_METRICS)],
                     unit="percentunit", ds=DS_METRICS), 8, 8)
    b.add(timeseries("Network throughput",
                     [target("A", 'event.dataset:system.network AND %s' % hq,
                             rate("system.network.in.bytes"),
                             [terms("system.network.name", 8), date_hist()],
                             alias="{{term system.network.name}} in", ds=DS_METRICS),
                      target("B", 'event.dataset:system.network AND %s' % hq,
                             rate("system.network.out.bytes"),
                             [terms("system.network.name", 8), date_hist()],
                             alias="{{term system.network.name}} out", ds=DS_METRICS)],
                     unit="Bps", ds=DS_METRICS, soft_min=0,
                     description="Per interface, per second. In and out are both bytes and "
                                 "share one axis. A reboot resets the underlying counters "
                                 "and shows as one negative spike."), 8, 8)
    b.add(timeseries("Disk I/O",
                     [target("A", 'event.dataset:system.diskio AND %s' % hq,
                             rate("system.diskio.read.bytes"),
                             [terms("system.diskio.name", 8), date_hist()],
                             alias="{{term system.diskio.name}} read", ds=DS_METRICS),
                      target("B", 'event.dataset:system.diskio AND %s' % hq,
                             rate("system.diskio.write.bytes"),
                             [terms("system.diskio.name", 8), date_hist()],
                             alias="{{term system.diskio.name}} write", ds=DS_METRICS)],
                     unit="Bps", ds=DS_METRICS, soft_min=0), 8, 8)

    # One row per mounted filesystem — the closest thing to a disk inventory
    # any agent here reports. The system integration collects *mounts*, so a
    # disk carrying no mounted filesystem never appears and a disk with several
    # partitions appears several times; the SMART table below is the other half
    # of the picture on hosts that have it.
    #
    # There is no free.pct field, so percent free is derived from used.pct in a
    # bucket_script rather than being left off the table. pipelineVariables
    # names a *metric id* — "4" here, not the field it reads.
    b.add(table("Disks and filesystems",
                [target("A", 'event.dataset:system.filesystem AND %s' % hq,
                        [{"id": "1", "type": "max", "field": "system.filesystem.total"},
                         {"id": "4", "type": "max", "field": "system.filesystem.used.pct"},
                         {"id": "5", "type": "bucket_script",
                          "pipelineVariables": [{"name": "used", "pipelineAgg": "4"}],
                          "settings": {"script": "1 - params.used"}}],
                        # Every one of these three is a keyword, and every one
                        # of them can be absent — a mount with no type reported
                        # would otherwise take its whole row off the table
                        # rather than showing a blank cell.
                        [terms("system.filesystem.device_name", 40, mid="3",
                               order_by="_term", order="asc", missing="(unknown)"),
                         terms("system.filesystem.mount_point", 10, mid="6",
                               order_by="_term", order="asc", missing="(unknown)"),
                         terms("system.filesystem.type", 5, mid="7",
                               order_by="_term", order="asc", missing="(unknown)")],
                        ds=DS_METRICS)],
                ds=DS_METRICS,
                description="Every filesystem this host has mounted: size, where it is "
                            "mounted, its type, and how full it is. Type distinguishes a "
                            "local filesystem from a network one (nfs4, cifs) — the "
                            "physical connection is not in any data collected here. "
                            "Usage is the peak over the selected range, not the value "
                            "right now, so a mount that filled and was cleared still "
                            "shows its high-water mark; free is 1 - that. Unmounted "
                            "disks do not appear at all.",
                overrides=[
                    rename("system.filesystem.device_name", "Disk"),
                    rename("system.filesystem.mount_point", "Mount point"),
                    rename("system.filesystem.type", "Type"),
                    rename("Max system.filesystem.total", "Size", "bytes"),
                    rename("Max system.filesystem.used.pct", "In use", "percentunit",
                           [{"color": "green", "value": None},
                            {"color": "orange", "value": 0.8},
                            {"color": "red", "value": 0.9}]),
                    # Grafana names a bucket_script column after the aggregation
                    # type, not after the script or the field it derives from.
                    rename("Bucket Script", "Free", "percentunit",
                           [{"color": "red", "value": None},
                            {"color": "orange", "value": 0.1},
                            {"color": "green", "value": 0.2}]),
                ]), 24, 8)

    # ── Hardware, when hwstats.py is feeding this host ──
    if has_hw:
        b.row("Hardware — temperatures, SMART and ZFS")
        hw = 'event.dataset:proxmox_hw AND %s' % hq
        b.add(timeseries("Temperatures",
                         [target("A", hw + " AND hw.kind:temperature",
                                 metric("avg", "hw.sensor.celsius"),
                                 [terms("hw.sensor.label", 12), date_hist()],
                                 alias="{{term hw.sensor.label}}")],
                         unit="celsius"), 12, 8)
        b.add(timeseries("Disk temperatures",
                         [target("A", hw + " AND hw.kind:disk",
                                 metric("max", "hw.disk.celsius"),
                                 [terms("hw.disk.device", 12), date_hist()],
                                 alias="{{term hw.disk.device}}")],
                         unit="celsius"), 12, 8)
        b.add(table("Disk health (SMART)",
                    [target("A", hw + " AND hw.kind:disk",
                            [{"id": "1", "type": "max", "field": "hw.disk.celsius"},
                             {"id": "4", "type": "max", "field": "hw.disk.power_on_hours"},
                             {"id": "5", "type": "max", "field": "hw.disk.reallocated_sectors"},
                             {"id": "6", "type": "max", "field": "hw.disk.pending_sectors"},
                             {"id": "7", "type": "max", "field": "hw.disk.percentage_used"}],
                            [terms("hw.disk.device", 20, order_by="_term")])],
                    description="Latest SMART summary per device. Any non-zero reallocated "
                                "or pending count wants attention.",
                    overrides=[
                        rename("hw.disk.device", "Device"),
                        rename("Max hw.disk.celsius", "Temp", "celsius",
                               [{"color": "green", "value": None},
                                {"color": "orange", "value": 45}, {"color": "red", "value": 55}]),
                        rename("Max hw.disk.power_on_hours", "Powered on (h)"),
                        rename("Max hw.disk.reallocated_sectors", "Reallocated", None,
                               [{"color": "green", "value": None}, {"color": "red", "value": 1}]),
                        rename("Max hw.disk.pending_sectors", "Pending", None,
                               [{"color": "green", "value": None}, {"color": "red", "value": 1}]),
                        rename("Max hw.disk.percentage_used", "SSD wear", "percent",
                               [{"color": "green", "value": None},
                                {"color": "orange", "value": 80}, {"color": "red", "value": 95}]),
                    ]), 12, 8)
        b.add(table("ZFS pools",
                    [target("A", hw + " AND hw.kind:zpool",
                            [{"id": "1", "type": "max", "field": "hw.zpool.capacity_pct"},
                             {"id": "4", "type": "max", "field": "hw.zpool.free_bytes"},
                             {"id": "5", "type": "max", "field": "hw.zpool.size_bytes"},
                             {"id": "6", "type": "max", "field": "hw.zpool.fragmentation_pct"}],
                            [terms("hw.zpool.name", 10, order_by="_term"),
                             terms("hw.zpool.health", 4, mid="8")])],
                    description="A pool past about 80% full slows down badly on ZFS, well "
                                "before it runs out.",
                    overrides=[
                        rename("hw.zpool.name", "Pool"),
                        rename("hw.zpool.health", "Health"),
                        rename("Max hw.zpool.capacity_pct", "Used", "percent",
                               [{"color": "green", "value": None},
                                {"color": "orange", "value": 80}, {"color": "red", "value": 90}]),
                        rename("Max hw.zpool.free_bytes", "Free", "bytes"),
                        rename("Max hw.zpool.size_bytes", "Size", "bytes"),
                        rename("Max hw.zpool.fragmentation_pct", "Fragmentation", "percent"),
                    ]), 12, 8)

    # ── systemd, when the linux integration is on ──
    if "linux" in pkgs:
        b.row("systemd units")
        # linux.service is a *metrics* data stream, so these two read es-metrics.
        # Pointed at es-logs they return nothing at all, and a count panel with
        # min_doc_count 0 draws a confident flat zero rather than looking broken.
        svc = 'event.dataset:linux.service AND %s' % hq
        b.add(stat("Failed units",
                   [target("A", '%s AND system.service.state:failed' % svc,
                           metric("cardinality", "system.service.name"), [date_hist()],
                           ds=DS_METRICS)],
                   calc="lastNotNull", ds=DS_METRICS,
                   steps=[{"color": "green", "value": None}, {"color": "red", "value": 1}]), 4, 6)
        b.add(table("Units not active",
                    [target("A", '%s AND NOT system.service.state:active' % svc, count(),
                            [terms("system.service.name", 40, order_by="_term"),
                             terms("system.service.sub_state", 6, mid="5")],
                            ds=DS_METRICS)],
                    ds=DS_METRICS,
                    description="Anything systemd is not currently holding active. A "
                                "one-shot that has finished shows here too and is fine. "
                                "The dataset filter matters: without it the negative "
                                "matches every metric document that has no unit state.",
                    overrides=[rename("system.service.name", "Unit"),
                               rename("system.service.sub_state", "State"),
                               rename("Count", "Samples")]), 20, 6)

    # ── Containers, when the docker integration is on ──
    if "docker" in pkgs:
        b.row("Containers")
        b.add(stat("Running containers",
                   [target("A", 'event.dataset:docker.container AND %s' % hq,
                           metric("cardinality", "container.name"), [date_hist()],
                           ds=DS_METRICS)],
                   calc="lastNotNull", ds=DS_METRICS), 4, 7)
        b.add(timeseries("Container CPU",
                         [target("A", 'event.dataset:docker.cpu AND %s' % hq,
                                 metric("avg", "docker.cpu.total.pct"),
                                 [terms("container.name", 15), date_hist()],
                                 alias="{{term container.name}}", ds=DS_METRICS)],
                         unit="percentunit", ds=DS_METRICS), 10, 7)
        b.add(timeseries("Container memory",
                         [target("A", 'event.dataset:docker.memory AND %s' % hq,
                                 metric("avg", "docker.memory.usage.total"),
                                 [terms("container.name", 15), date_hist()],
                                 alias="{{term container.name}}", ds=DS_METRICS)],
                         unit="bytes", ds=DS_METRICS), 10, 7)
        b.add(timeseries("Container log volume",
                         [target("A", 'event.dataset:docker.container_logs AND %s' % hq,
                                 count(), [terms("container.name", 15), date_hist()],
                                 alias="{{term container.name}}")],
                         style="bars", stack=True,
                         description="stdout and stderr of every container on this host."), 12, 7)
        b.add(timeseries("Container errors",
                         [target("A", 'event.dataset:docker.container_logs AND %s AND %s'
                                 % (hq, ERROR_Q), count(),
                                 [terms("container.name", 10), date_hist()],
                                 alias="{{term container.name}}")],
                         style="bars", stack=True), 12, 7)

    # ── Application logs, one panel set for whatever Custom Logs collects ──
    if datasets:
        b.row("Application logs")
        ds_filter = "event.dataset:(%s)" % " OR ".join(datasets)
        b.add(timeseries("Volume by dataset",
                         [target("A", "%s AND %s" % (hq, ds_filter), count(),
                                 [terms("event.dataset", max(len(datasets), 4)), date_hist()],
                                 alias="{{term event.dataset}}")],
                         style="bars", stack=True,
                         description="Custom Logs datasets configured on this host's policy: "
                                     + ", ".join(datasets)), 12, 8)
        b.add(timeseries("Errors by dataset",
                         [target("A", "%s AND %s AND %s" % (hq, ds_filter, ERROR_Q), count(),
                                 [terms("event.dataset", max(len(datasets), 4)), date_hist()],
                                 alias="{{term event.dataset}}")],
                         style="bars", stack=True,
                         description="Empty for a dataset with no logs-<dataset>@custom "
                                     "pipeline: without a parser nothing sets log.level."), 12, 8)
        b.add(logs_panel("Application errors",
                         "%s AND %s AND %s" % (hq, ds_filter, ERROR_Q)), 24, 10)

    # ── Everything else this host logs ──
    b.row("All logs")
    b.add(timeseries("Log volume by dataset",
                     [target("A", hq, count(), [terms("event.dataset", 15), date_hist()],
                             alias="{{term event.dataset}}")],
                     style="bars", stack=True), 16, 8)
    b.add(timeseries("Errors by dataset",
                     [target("A", "%s AND %s" % (hq, ERROR_Q), count(),
                             [terms("event.dataset", 8), date_hist()],
                             alias="{{term event.dataset}}")],
                     style="bars", stack=True), 8, 8)
    if "journald" in pkgs:
        b.add(logs_panel("Journal (error and above)",
                         '%s AND event.dataset:journal_errors' % hq,
                         description="The journald input is filtered to PRIORITY 0-3 at the "
                                     "agent, so everything here is already error or worse."),
              12, 10)
        b.add(logs_panel("Recent errors — everything else",
                         '%s AND %s AND NOT event.dataset:journal_errors' % (hq, ERROR_Q)),
              12, 10)
    else:
        b.add(logs_panel("Recent errors", "%s AND %s" % (hq, ERROR_Q)), 24, 10)

    return b


# ── Network-device dashboards ───────────────────────────────────────────────

NET_SOURCES = [
    ("qnap_nas", "qnap_nas.log", "QNAP NAS", 9301),
    ("cef", "cef.log", "UniFi activity (CEF)", 9003),
    ("iptables", "iptables.log", "UniFi firewall", 9001),
    ("netflow", "netflow.log", "UniFi flows (NetFlow)", 2055),
]


def network_overview(receivers):
    """Are the four push-based receivers actually receiving?

    Everything else in this stack polls or tails a file, and stops loudly. These
    four sit on a UDP socket waiting to be sent to, so a device that stops
    sending, a changed port, or a firewall rule looks exactly like a quiet
    night. That is the question this dashboard answers first.
    """
    b = Board(uid="net-overview", title="Network — receiver health",
              description="The four UDP receivers on the stack VM. Ports are read from the "
                          "live Fleet policy, so they match what the devices must be told.",
              tags=["homelab", "elastic", "network"], link_tag="network",
              time_from="now-24h")

    for pkg, dataset, label, default_port in NET_SOURCES:
        port = receivers.get(pkg, default_port)
        q = "event.dataset:%s" % dataset
        b.add(stat("%s — events" % label,
                   [target("A", q, count(), [date_hist()])],
                   description="UDP %d. Zero over the whole range means nothing arrived: "
                               "check the device's SIEM/syslog target, then the port, then "
                               "the stack VM's firewall." % port,
                   steps=[{"color": "red", "value": None}, {"color": "green", "value": 1}]),
              6, 4)

    for pkg, dataset, label, default_port in NET_SOURCES:
        port = receivers.get(pkg, default_port)
        b.add(last_seen("%s — last event" % label, "event.dataset:%s" % dataset,
                        description="Age of the most recent event on UDP %d." % port), 6, 4)

    b.row("Ingest")
    b.add(timeseries("Events by source",
                     [target("A", "event.dataset:(%s)" % " OR ".join(d for _, d, _, _ in NET_SOURCES),
                             count(), [terms("event.dataset", 6), date_hist()],
                             alias="{{term event.dataset}}")],
                     style="bars", stack=True,
                     description="A flat line at zero for one source while the others move "
                                 "is a device problem, not a stack problem."), 16, 8)
    b.add(timeseries("Events by sending address",
                     [target("A", "event.dataset:(%s)" % " OR ".join(d for _, d, _, _ in NET_SOURCES),
                             count(), [terms("log.source.address", 8), date_hist()],
                             alias="{{term log.source.address}}")],
                     style="bars", stack=True,
                     description="The UDP peer each packet came from, stamped by the syslog "
                                 "input itself rather than parsed out of the message — so it "
                                 "is right even when nothing else parses. NetFlow uses a "
                                 "different input and does not set it."), 8, 8)

    b.row("Everything arriving")
    b.add(logs_panel("Recent network-device events",
                     "event.dataset:(%s)" % " OR ".join(d for _, d, _, _ in NET_SOURCES),
                     description="Unfiltered, newest first — the quickest way to confirm a "
                                 "device you just reconfigured is landing."), 24, 12)
    return b


# Every spelling a block can have here, because there are three sources of the
# word and they disagree.
#
#   d, a, r     9.x labels [LAN_IN-2000-D], taken verbatim and lowercased
#   drop        iptables >=1.23.1 maps d -> drop and a -> accept in its own
#               pipeline, but has no entry for r
#   reject      logs-iptables.log@custom expands the leftover r
#   drop/accept 10.x lines, derived from the rule's DESCR text, which is the
#               only place the disposition appears in that format
#
# The bare letters stay in the list because documents indexed before
# pipelines/backfill-unifi-iptables.sh was run still carry them.
BLOCKED = "event.action:(d OR r OR drop OR reject OR deny)"


def qnap_dashboard(port):
    """QNAP event and access logs.

    The integration sets no log.level, so there is nothing to filter severity
    on here — the shape of the data is event.action and qnap.nas.category
    instead, and the panels follow it rather than pretending otherwise.
    """
    b = Board(uid="net-qnap", title="Network — QNAP NAS",
              description="Event and access logs from QuLog Center over UDP %d." % port,
              tags=["homelab", "elastic", "network"], link_tag="network")
    q = "event.dataset:qnap_nas.log"
    b.add(stat("Events", [target("A", q, count(), [date_hist()])],
               steps=[{"color": "red", "value": None}, {"color": "green", "value": 1}]), 6, 4)
    b.add(last_seen("Last event", q), 6, 4)
    b.add(stat("Distinct users",
               [target("A", q, metric("cardinality", "user.name"), [date_hist()])],
               calc="lastNotNull"), 6, 4)
    b.add(stat("Distinct source addresses",
               [target("A", q, metric("cardinality", "source.address"), [date_hist()])],
               calc="lastNotNull"), 6, 4)

    b.row("Activity")
    b.add(timeseries("Events by category",
                     [target("A", q, count(), [terms("qnap.nas.category", 8), date_hist()],
                             alias="{{term qnap.nas.category}}")],
                     style="bars", stack=True), 12, 8)
    b.add(timeseries("Events by action",
                     [target("A", q, count(), [terms("event.action", 12), date_hist()],
                             alias="{{term event.action}}")],
                     style="bars", stack=True,
                     description="login, logout, delete, and the rest — the QNAP parser "
                                 "normalises these out of the message text."), 12, 8)
    b.add(table("Top users",
                [target("A", q, count(), [terms("user.name", 20)])],
                overrides=[rename("user.name", "User"), rename("Count", "Events")]), 8, 8)
    b.add(table("Top source addresses",
                [target("A", q, count(), [terms("source.address", 20)])],
                description="A login from an address you do not recognise is the thing to "
                            "look for here.",
                overrides=[rename("source.address", "Source"), rename("Count", "Events")]), 8, 8)
    b.add(table("Applications",
                [target("A", q, count(), [terms("qnap.nas.application", 20)])],
                overrides=[rename("qnap.nas.application", "Application"),
                           rename("Count", "Events")]), 8, 8)

    b.row("File access")
    b.add(table("Most-touched paths",
                [target("A", q, count(),
                        [terms("qnap.nas.file.path", 25),
                         terms("event.action", 5, mid="5")])],
                description="Populated only if QuLog is sending Access Logs as well as "
                            "Event Logs — both have to be ticked on the Log Sender "
                            "destination.",
                overrides=[rename("qnap.nas.file.path", "Path"),
                           rename("event.action", "Action"), rename("Count", "Events")]), 12, 9)
    b.add(table("Connection types",
                [target("A", q, count(), [terms("qnap.nas.connection_type", 15)])],
                description="SMB, AFP, FTP, HTTP and friends.",
                overrides=[rename("qnap.nas.connection_type", "Protocol"),
                           rename("Count", "Events")]), 12, 9)

    b.row("Detail")
    b.add(logs_panel("All QNAP events", q), 24, 12)
    return b


def cef_dashboard(port):
    b = Board(uid="net-unifi-activity", title="Network — UniFi activity",
              description="UniFi admin actions, device events and client activity, decoded "
                          "from CEF over UDP %d. Decoding happens in the agent, not in an "
                          "ingest pipeline — simulating the pipeline against a CEF line "
                          "shows nothing and looks like a failure." % port,
              tags=["homelab", "elastic", "network"], link_tag="network")
    q = "event.dataset:cef.log"
    b.add(stat("Events", [target("A", q, count(), [date_hist()])],
               steps=[{"color": "red", "value": None}, {"color": "green", "value": 1}]), 6, 4)
    b.add(last_seen("Last event", q), 6, 4)
    b.add(stat("Event types",
               [target("A", q, metric("cardinality", "cef.name"), [date_hist()])],
               calc="lastNotNull"), 6, 4)
    b.add(stat("Products reporting",
               [target("A", q, metric("cardinality", "cef.device.product"), [date_hist()])],
               calc="lastNotNull",
               description="cef.device.product comes from the CEF header, so it is set on "
                           "every well-formed line whatever else fails to parse."), 6, 4)

    b.row("Activity")
    b.add(timeseries("Events by type",
                     [target("A", q, count(), [terms("cef.name", 12), date_hist()],
                             alias="{{term cef.name}}")],
                     style="bars", stack=True), 16, 8)
    b.add(timeseries("By CEF severity",
                     [target("A", q, count(), [terms("cef.severity", 8), date_hist()],
                             alias="sev {{term cef.severity}}")],
                     style="bars", stack=True,
                     description="CEF severity is 0-10, not a log level."), 8, 8)
    b.add(table("Event types",
                [target("A", q, count(),
                        [terms("cef.name", 25, missing="(unnamed)"),
                         terms("cef.device.event_class_id", 3, mid="5", missing="(none)")])],
                overrides=[rename("cef.name", "Event"),
                           rename("cef.device.event_class_id", "Class"),
                           rename("Count", "Count")]), 8, 8)
    b.add(table("Reporting devices",
                [target("A", q, count(),
                        [terms("cef.device.vendor", 10, missing="(none)"),
                         terms("cef.device.product", 10, mid="5", missing="(none)"),
                         terms("observer.name", 10, mid="6", missing="(unattributed)")])],
                description="observer.name is the device, resolved by "
                            "pipelines/unifi-cef-common.json from UNIFIdeviceName, UNIFIhost "
                            "or the syslog hostname in that order. Product is the "
                            "application, which is the same string on every device.",
                overrides=[rename("cef.device.vendor", "Vendor"),
                           rename("cef.device.product", "Product"),
                           rename("observer.name", "Device"),
                           rename("Count", "Events")]), 8, 8)
    b.add(table("Source addresses",
                [target("A", q, count(), [terms("source.ip", 20)])],
                description="Empty unless the events carry a src extension — plenty of "
                            "UniFi activity events are about the controller itself.",
                overrides=[rename("source.ip", "Source IP"), rename("Count", "Events")]), 8, 8)

    b.row("Detail")
    b.add(logs_panel("All UniFi activity", q), 24, 12)
    return b


def iptables_dashboard(port):
    b = Board(uid="net-unifi-firewall", title="Network — UniFi firewall",
              description="Per-rule allow and block decisions from the UniFi gateway over "
                          "UDP %d. Ubiquiti's rule labels are parsed into "
                          "iptables.ubiquiti.* by patterns built into the integration." % port,
              tags=["homelab", "elastic", "network"], link_tag="network")
    q = "event.dataset:iptables.log"
    drop = "%s AND %s" % (q, BLOCKED)
    b.add(stat("Decisions", [target("A", q, count(), [date_hist()])],
               steps=[{"color": "red", "value": None}, {"color": "green", "value": 1}]), 6, 4)
    b.add(last_seen("Last event", q), 6, 4)
    b.add(stat("Blocked", [target("A", drop, count(), [date_hist()])],
               steps=[{"color": "text", "value": None}],
               description="Blocks are constant and normal on a WAN-facing rule; the signal "
                           "is a change in shape, not the number. Counts action \"d\" and "
                           "\"r\" — UniFi's rule-label suffixes — as well as the "
                           "spelled-out values a generic iptables source would send."), 6, 4)
    b.add(stat("Distinct sources blocked",
               [target("A", drop, metric("cardinality", "source.ip"), [date_hist()])],
               calc="lastNotNull"), 6, 4)

    b.row("Traffic")
    b.add(timeseries("By action",
                     [target("A", q, count(), [terms("event.action", 6), date_hist()],
                             alias="{{term event.action}}")],
                     style="bars", stack=True,
                     description="Three spellings coexist. 9.x labels give the bare "
                                 "letters d/a/r; the integration maps d to drop and a to "
                                 "accept but not r; 10.x lines have no disposition in them "
                                 "at all and take it from the rule's DESCR text. Documents "
                                 "indexed before backfill-unifi-iptables.sh ran keep "
                                 "whichever spelling they arrived with."), 12, 8)
    b.add(timeseries("By rule set",
                     [target("A", q, count(),
                             [terms("iptables.ubiquiti.rule_set", 10), date_hist()],
                             alias="{{term iptables.ubiquiti.rule_set}}")],
                     style="bars", stack=True,
                     description="LAN_IN, WAN_IN and friends. Empty means the lines fell "
                                 "through to the generic iptables pattern — on Network 10.x "
                                 "that is the normal state until logs-iptables.log@custom "
                                 "has repaired them. See docs/network-devices.md."), 12, 8)
    b.add(table("Decisions by device",
                [target("A", q, count(),
                        [terms("observer.name", 12, missing="(unattributed)"),
                         terms("event.action", 4, mid="5", missing="(none)")])],
                description="Which gateway or AP reported the decision. (unattributed) "
                            "means the line's framing was not parsed — run "
                            "pipelines/backfill-unifi-iptables.sh.",
                overrides=[rename("observer.name", "Device"),
                           rename("event.action", "Action"),
                           rename("Count", "Decisions")]), 8, 8)
    b.add(table("Top blocked sources",
                [target("A", drop, count(), [terms("source.ip", 20)])],
                overrides=[rename("source.ip", "Source IP"), rename("Count", "Blocked")]), 8, 8)
    b.add(table("Top blocked destination ports",
                [target("A", drop, count(),
                        [terms("destination.port", 20, missing=0),
                         terms("network.transport", 3, mid="5", missing="(none)")])],
                overrides=[rename("destination.port", "Port"),
                           rename("network.transport", "Proto"),
                           rename("Count", "Blocked")]), 8, 8)
    b.add(table("Busiest rules",
                [target("A", q, count(),
                        [terms("iptables.ubiquiti.rule_set", 10, missing="(none)"),
                         terms("iptables.ubiquiti.rule_number", 10, mid="5", missing="(none)"),
                         terms("event.action", 3, mid="6", missing="(none)")])],
                overrides=[rename("iptables.ubiquiti.rule_set", "Rule set"),
                           rename("iptables.ubiquiti.rule_number", "Rule"),
                           rename("event.action", "Action"),
                           rename("Count", "Hits")]), 8, 8)
    b.add(timeseries("Blocked by transport",
                     [target("A", drop, count(), [terms("network.transport", 6), date_hist()],
                             alias="{{term network.transport}}")],
                     style="bars", stack=True), 12, 7)
    b.add(timeseries("Blocked by source country",
                     [target("A", drop, count(),
                             [terms("source.geo.country_iso_code", 8, missing="(no geo)"),
                              date_hist()],
                             alias="{{term source.geo.country_iso_code}}")],
                     style="bars", stack=True,
                     description="From the GeoIP lookup the integration does on the way in. "
                                 "This replaced a zone breakdown that could never populate: "
                                 "the integration splits zones out of the rule set name on a "
                                 "hyphen, and UniFi's chains use underscores — LAN_IN, "
                                 "WAN_LOCAL — so input_zone and output_zone are empty on "
                                 "every UniFi line, and the panel drew an authoritative "
                                 "blank."), 12, 7)

    b.row("Detail")
    b.add(logs_panel("Recent firewall decisions", q), 24, 12)
    return b


def unifi_dashboard(receivers):
    """Both UniFi feeds on one page, keyed on the device that reported.

    The two per-receiver dashboards answer "is this listener working". This one
    answers the security questions that span both: what got blocked, by which
    signature, from where, against which of my devices — and who logged into the
    management plane. It is built on the ECS fields that pipelines/
    unifi-cef-common.json and logs-iptables.log@custom.json normalise, so it
    stays correct across the 9.x and 10.x line formats where the raw
    integration fields do not.
    """
    cef_port = receivers.get("cef", 9003)
    ipt_port = receivers.get("iptables", 9001)
    both = "event.dataset:(cef.log OR iptables.log)"
    threats = "event.dataset:cef.log AND event.code:201"
    admin = "event.dataset:cef.log AND event.code:544"
    fw = "event.dataset:iptables.log"
    blocked = "%s AND %s" % (fw, BLOCKED)

    b = Board(uid="net-unifi", title="Network — UniFi security",
              description="UniFi CEF (UDP %d) and firewall (UDP %d) together, grouped by the "
                          "device that reported rather than by feed. Every adopted device "
                          "sends to the collector directly, so this is a whole-site view: "
                          "the UDM Pro, a UX7, each AP and switch. Panels key on the numeric "
                          "CEF Event Class ID, which Ubiquiti keeps stable, rather than on "
                          "the event name, which it rewords freely."
                          % (cef_port, ipt_port),
              tags=["homelab", "elastic", "network", "unifi"], link_tag="network")

    b.add(stat("Events", [target("A", both, count(), [date_hist()])],
               steps=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
               description="Both feeds. Zero over the whole range is a delivery problem, "
                           "not a quiet night — see Network — receiver health."), 6, 4)
    b.add(stat("Devices reporting",
               [target("A", both, metric("cardinality", "observer.name"), [date_hist()])],
               calc="lastNotNull",
               description="Distinct observer.name. If this is 1 on a site with several "
                           "adopted devices, the others are not reaching the collector — "
                           "check the per-VLAN firewall rule, not the UniFi settings."), 6, 4)
    b.add(stat("Threats blocked", [target("A", threats, count(), [date_hist()])],
               steps=[{"color": "text", "value": None}],
               description="CEF class 201. A steady background rate is normal on a "
                           "WAN-facing gateway; the signal is a change in shape."), 6, 4)
    b.add(stat("Admin logins", [target("A", admin, count(), [date_hist()])],
               steps=[{"color": "text", "value": None}],
               description="CEF class 544. Any of these you cannot account for is the "
                           "highest-value finding on this page."), 6, 4)

    b.row("Threats — IDS/IPS")
    b.add(timeseries("Threats by signature",
                     [target("A", threats, count(), [terms("rule.name", 10), date_hist()],
                             alias="{{term rule.name}}")],
                     style="bars", stack=True,
                     description="rule.name is the Suricata signature. On consoles migrated "
                                 "to Suricata 8 it comes from UNIFIthreatType instead; both "
                                 "spellings land in the same field."), 12, 8)
    b.add(timeseries("Threats by direction and device",
                     [target("A", threats, count(),
                             [terms("network.direction", 4, missing="(unknown)"),
                              terms("observer.name", 6, mid="5", missing="(unattributed)"),
                              date_hist()],
                             alias="{{term network.direction}} · {{term observer.name}}")],
                     style="bars", stack=True), 12, 8)
    b.add(table("Top signatures",
                [target("A", threats,
                        [{"id": "1", "type": "count"},
                         {"id": "4", "type": "max", "field": "event.risk_score"}],
                        [terms("rule.name", 20, missing="(unnamed)"),
                         terms("rule.ruleset", 5, mid="5", missing="(none)")])],
                description="Ruleset is the list the signature came from — CINS Army "
                            "Reputation List and friends. Risk is UNIFIrisk mapped to a "
                            "number so it can be sorted.",
                overrides=[rename("rule.name", "Signature"),
                           rename("rule.ruleset", "Ruleset"),
                           rename("Count", "Hits"),
                           rename("Max event.risk_score", "Risk",
                                  steps=[{"color": "text", "value": None},
                                         {"color": "orange", "value": 50},
                                         {"color": "red", "value": 75}])]), 12, 8)
    b.add(table("Where threats come from",
                [target("A", threats, count(),
                        [terms("source.ip", 20),
                         terms("source.geo.country_iso_code", 3, mid="5",
                               missing="(no geo)")])],
                description="Country is UNIFIsrcRegion where UniFi sent one, otherwise the "
                            "GeoIP lookup the integration does on the way in.",
                overrides=[rename("source.ip", "Source IP"),
                           rename("source.geo.country_iso_code", "Country"),
                           rename("Count", "Threats")]), 12, 8)

    b.row("Firewall")
    b.add(timeseries("Decisions by device",
                     [target("A", fw, count(),
                             [terms("observer.name", 8, missing="(unattributed)"),
                              date_hist()],
                             alias="{{term observer.name}}")],
                     style="bars", stack=True,
                     description="An (unattributed) series means firewall lines are landing "
                                 "that logs-iptables.log@custom has not repaired — run "
                                 "pipelines/backfill-unifi-iptables.sh."), 12, 8)
    b.add(timeseries("Blocked by rule",
                     [target("A", blocked, count(), [terms("rule.name", 10), date_hist()],
                             alias="{{term rule.name}}")],
                     style="bars", stack=True,
                     description="On 10.x rule.name is the rule's DESCR text, which is why "
                                 "the convention is to name rules 'Block ...' and "
                                 "'Allow ...'. On 9.x lines it falls back to the chain."), 12, 8)
    b.add(table("Busiest rules",
                [target("A", fw, count(),
                        [terms("rule.ruleset", 8, missing="(none)"),
                         terms("rule.name", 15, mid="5", missing="(unnamed)"),
                         terms("event.action", 4, mid="6", missing="(none)")])],
                description="An action of (none) is a 10.x line whose rule description does "
                            "not begin with Block or Allow — the only place the disposition "
                            "appears in that format.",
                overrides=[rename("rule.ruleset", "Chain"),
                           rename("rule.name", "Rule"),
                           rename("event.action", "Action"),
                           rename("Count", "Hits")]), 12, 8)
    b.add(table("Top blocked sources",
                [target("A", blocked, count(),
                        [terms("source.ip", 20),
                         terms("destination.port", 5, mid="5", missing=0),
                         terms("network.transport", 3, mid="6", missing="(none)")])],
                description="A destination port of 0 is the missing-value bucket, not a "
                            "real port — ICMP blocks carry no port at all.",
                overrides=[rename("source.ip", "Source IP"),
                           rename("destination.port", "Dst port"),
                           rename("network.transport", "Proto"),
                           rename("Count", "Blocked")]), 12, 8)

    b.row("Management plane")
    b.add(table("Admin activity",
                [target("A", admin, count(),
                        [terms("source.ip", 15),
                         terms("source.user.name", 10, mid="5", missing="(none)"),
                         terms("observer.name", 5, mid="6", missing="(unattributed)")])],
                description="Who reached the controller, from where, via which device. A "
                            "super-admin appearing from a client or guest VLAN is the "
                            "pattern that mattered after Ubiquiti's Bulletin 064.",
                overrides=[rename("source.ip", "Source IP"),
                           rename("source.user.name", "User"),
                           rename("observer.name", "Device"),
                           rename("Count", "Events")]), 12, 8)
    b.add(logs_panel("Admin and management events", admin,
                     description="CEF class 544 in full. Configuration changes you did not "
                                 "make — especially to firewall policy or to the syslog "
                                 "settings themselves — are the tamper canary here.",
                     limit="100"), 12, 8)

    b.row("Devices")
    b.add(table("Events by device",
                [target("A", both, count(),
                        [terms("observer.name", 20, missing="(unattributed)"),
                         terms("event.dataset", 3, mid="5", missing="(none)")])],
                description="The whole site, both feeds. A device you know is adopted and "
                            "not listed here is not reaching the collector.",
                overrides=[rename("observer.name", "Device"),
                           rename("event.dataset", "Feed"),
                           rename("Count", "Events")]), 12, 8)
    b.add(stat("Unattributed events",
               [target("A", "%s AND NOT _exists_:observer.name" % both, count(), [date_hist()])],
               steps=[{"color": "green", "value": None}, {"color": "orange", "value": 1}],
               description="Events no device could be resolved for. Green at zero is the "
                           "healthy answer; anything else means a feed is arriving in a "
                           "shape the pipelines do not recognise, and the per-device panels "
                           "above are quietly incomplete."), 12, 8)

    b.row("Detail")
    b.add(logs_panel("Everything from UniFi", both), 24, 12)
    return b


def netflow_dashboard(port):
    b = Board(uid="net-netflow", title="Network — flows (NetFlow)",
              description="NetFlow v5/v9/v10 records from the UniFi gateway on UDP %d. "
                          "Model-dependent: several gateways cannot export NetFlow at all, "
                          "and this dashboard stays empty on those." % port,
              tags=["homelab", "elastic", "network"], link_tag="network")
    q = "event.dataset:netflow.log"
    b.add(stat("Flows", [target("A", q, count(), [date_hist()])],
               steps=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
               description="Zero here while the other receivers are healthy usually means "
                           "the gateway model does not support NetFlow export."), 6, 4)
    b.add(last_seen("Last flow", q), 6, 4)
    b.add(stat("Bytes", [target("A", q, metric("sum", "network.bytes"), [date_hist()])],
               unit="bytes"), 6, 4)
    b.add(stat("Distinct hosts",
               [target("A", q, metric("cardinality", "source.ip"), [date_hist()])],
               calc="lastNotNull"), 6, 4)

    b.row("Volume")
    b.add(timeseries("Traffic by source",
                     [target("A", q, metric("sum", "network.bytes"),
                             [terms("source.ip", 12, order_by="1"), date_hist()],
                             alias="{{term source.ip}}")],
                     unit="bytes", style="bars", stack=True,
                     description="Ordered by bytes rather than flow count — one big "
                                 "transfer matters more than a thousand DNS lookups."), 12, 8)
    b.add(timeseries("Traffic by destination",
                     [target("A", q, metric("sum", "network.bytes"),
                             [terms("destination.ip", 12, order_by="1"), date_hist()],
                             alias="{{term destination.ip}}")],
                     unit="bytes", style="bars", stack=True), 12, 8)
    b.add(table("Top talkers",
                [target("A", q, [{"id": "1", "type": "sum", "field": "network.bytes"},
                                 {"id": "4", "type": "sum", "field": "network.packets"}],
                        [terms("source.ip", 20, order_by="1")])],
                overrides=[rename("source.ip", "Source IP"),
                           rename("Sum network.bytes", "Bytes", "bytes"),
                           rename("Sum network.packets", "Packets")]), 8, 8)
    b.add(table("Top conversations",
                [target("A", q, metric("sum", "network.bytes"),
                        [terms("source.ip", 10, order_by="1"),
                         terms("destination.ip", 5, mid="5", order_by="1")])],
                overrides=[rename("source.ip", "Source"),
                           rename("destination.ip", "Destination"),
                           rename("Sum network.bytes", "Bytes", "bytes")]), 8, 8)
    b.add(table("Top destination ports",
                [target("A", q, metric("sum", "network.bytes"),
                        [terms("destination.port", 20, order_by="1"),
                         terms("network.transport", 3, mid="5")])],
                overrides=[rename("destination.port", "Port"),
                           rename("network.transport", "Proto"),
                           rename("Sum network.bytes", "Bytes", "bytes")]), 8, 8)
    return b


NETWORK_BUILDERS = {
    "qnap_nas": ("network-qnap.json", qnap_dashboard),
    "cef": ("network-unifi-activity.json", cef_dashboard),
    "iptables": ("network-unifi-firewall.json", iptables_dashboard),
    "netflow": ("network-netflow.json", netflow_dashboard),
}


# ── Writing ─────────────────────────────────────────────────────────────────

def slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "host"
    return s[:32]


def unique_slugs(names):
    """Stable, collision-free filename and uid stems, keyed by host name.

    Two hosts whose names differ only past the truncation point would otherwise
    land on the same file, and one dashboard would go missing without a word
    being said about it. Sorted so the disambiguated one is the same every run.
    """
    out, used = {}, set()
    for name in sorted(names):
        stem = slug(name)
        if stem in used:
            stem = "%s-%s" % (stem[:26], hashlib.sha1(name.encode()).hexdigest()[:5])
        used.add(stem)
        out[name] = stem
    return out


def render(dash):
    return json.dumps(dash, indent=2, ensure_ascii=False) + "\n"


def is_generated(path):
    """True only for a file this script wrote — the guard on every write."""
    try:
        with open(path) as fh:
            return MARKER in (json.load(fh).get("tags") or [])
    except (OSError, ValueError):
        return False


def write_dashboard(outdir, filename, dash, dry_run):
    path = os.path.join(outdir, filename)
    body = render(dash)
    if os.path.exists(path):
        if not is_generated(path):
            print("   ! %s exists and is not generated — left alone" % filename)
            return "skipped"
        with open(path) as fh:
            if fh.read() == body:
                return "unchanged"
        action = "updated"
    else:
        action = "created"
    if not dry_run:
        # Grafana rescans this directory every 30s and would happily read a
        # half-written file, so the rename has to be the only thing it sees.
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(body)
        os.replace(tmp, path)
    return action


def prune(outdir, keep, dry_run):
    removed = []
    for name in sorted(os.listdir(outdir)):
        if not name.endswith(".json") or name in keep:
            continue
        path = os.path.join(outdir, name)
        if not is_generated(path):
            continue
        if not dry_run:
            os.remove(path)
        removed.append(name)
    return removed


# ── Entry point ─────────────────────────────────────────────────────────────

def main(argv):
    args = argv[1:]
    if "-h" in args or "--help" in args:
        print(__doc__.strip())
        return 0
    dry_run = "--dry-run" in args
    only_list = "--list" in args
    do_prune = "--no-prune" not in args
    outdir = OUTDIR
    if "--out" in args:
        i = args.index("--out") + 1
        if i >= len(args):
            print("!! --out needs a directory", file=sys.stderr)
            return 1
        outdir = args[i]

    print("Discovering agents from Fleet at %s" % KB)
    agents = fleet_agents()
    caps = policy_capabilities()
    seen = es_host_names()

    receivers = {}
    for c in caps.values():
        receivers.update(c.get("receivers", {}))

    print("   %d agent%s enrolled" % (len(agents), "" if len(agents) == 1 else "s"))
    resolved = []
    for a in agents:
        host_name, note = resolve_host_name(a["hostname"], seen)
        resolved.append((a, host_name))
        c = caps.get(a["policy_id"], {})
        bits = sorted(c.get("packages", set()))
        print("   %-24s policy=%-14s status=%-8s [%s]"
              % (host_name, a["policy_id"] or "-", a["status"], ", ".join(bits) or "no inputs"))
        if c.get("datasets"):
            print("       custom logs: %s" % ", ".join(sorted(c["datasets"])))
        if note:
            print("       ! %s" % note)

    if receivers:
        print("   network receivers: %s"
              % ", ".join("%s udp/%d" % (k, v) for k, v in sorted(receivers.items())))
    else:
        print("   no network receivers configured in any policy")

    if only_list:
        return 0

    if not os.path.isdir(outdir):
        os.makedirs(outdir)

    boards = []
    stems = unique_slugs([h for _, h in resolved])
    for a, host_name in resolved:
        stem = stems[host_name]
        b = host_dashboard(a, host_name, caps.get(a["policy_id"], {}), stem)
        boards.append(("host-%s.json" % stem, b.to_dict()))

    boards.append(("network-overview.json", network_overview(receivers).to_dict()))
    for pkg, (filename, builder) in sorted(NETWORK_BUILDERS.items()):
        if pkg not in receivers:
            continue
        boards.append((filename, builder(receivers[pkg]).to_dict()))
    # Spans both UniFi feeds, so it is worth having as soon as either listener
    # exists — one half being empty is itself the thing you want to see.
    if "cef" in receivers or "iptables" in receivers:
        boards.append(("network-unifi.json", unifi_dashboard(receivers).to_dict()))

    print("\nWriting to %s%s" % (outdir, "  (dry run)" if dry_run else ""))
    counts = {}
    for filename, dash in boards:
        action = write_dashboard(outdir, filename, dash, dry_run)
        counts[action] = counts.get(action, 0) + 1
        if action != "unchanged":
            print("   %-9s %s" % (action, filename))
    if counts.get("unchanged"):
        print("   unchanged %d" % counts["unchanged"])

    if do_prune:
        for name in prune(outdir, {f for f, _ in boards}, dry_run):
            print("   removed   %s  (no longer configured in Fleet)" % name)

    if not dry_run:
        print("\nGrafana rescans that directory every 30s — nothing to restart.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except RuntimeError as exc:
        print("!! %s" % exc, file=sys.stderr)
        sys.exit(1)
