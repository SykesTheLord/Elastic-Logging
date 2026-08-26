#!/usr/bin/env python3
"""
Create the Fleet agent policies for the Proxmox host and the media VMs.

Idempotent: existing policies are left alone, so re-running after you have
tweaked something in the Fleet UI will not stomp your changes. Delete a policy
in Kibana and re-run to get the stock version back.

Deliberately not declared in kibana.yml as preconfigured policies — Kibana
reasserts those on every restart, which makes the UI read-only in practice.
These are ordinary API-created policies you can edit freely.
"""
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
AUTH = ("elastic", ENV["ELASTIC_PASSWORD"])

if KB.startswith("https"):
    CTX = ssl.create_default_context(cafile=CA)
    # The certificate is issued for the LAN IP and DNS name, not "localhost".
    CTX.check_hostname = False
else:
    CTX = None


def api(path, method="GET", body=None):
    req = urllib.request.Request(KB + path, method=method)
    req.add_header("kbn-xsrf", "true")
    req.add_header("Content-Type", "application/json")
    import base64
    req.add_header("Authorization", "Basic " + base64.b64encode(
        ("%s:%s" % AUTH).encode()).decode())
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data, context=CTX, timeout=120) as r:
            return json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        payload = e.read().decode()
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            pass
        raise RuntimeError("%s %s -> %s: %s" % (method, path, e.code, payload)) from None


def pkg_version(name):
    """Use whatever version Kibana actually installed, not a pinned guess."""
    return api("/api/fleet/epm/packages/%s" % name)["item"]["version"]


def ensure_agent_policy(pid, name, description):
    existing = api("/api/fleet/agent_policies?perPage=200")["items"]
    if any(p["id"] == pid for p in existing):
        print("   agent policy %-14s already exists, leaving alone" % pid)
        return False
    api("/api/fleet/agent_policies", "POST", {
        "id": pid, "name": name, "namespace": "default",
        "description": description,
        "monitoring_enabled": ["logs", "metrics"],
    })
    print("   agent policy %-14s created" % pid)
    return True


def add(name, policy_id, package, inputs):
    # Integration policy names are unique across the whole of Fleet, not just
    # within one agent policy, so every name carries its policy as a prefix.
    full = "%s-%s" % (policy_id, name)
    api("/api/fleet/package_policies", "POST", {
        "name": full, "policy_ids": [policy_id], "namespace": "default",
        "package": {"name": package, "version": pkg_version(package)},
        "inputs": inputs,
    })
    print("      + %s" % full)


# ── Reusable input fragments ────────────────────────────────────────────────

# Left to itself the filesystem metricset collects almost nothing worth having.
# With no filesystem.ignore_types set, Metricbeat builds the ignore list from
# every type marked `nodev` in /proc/filesystems — which is not just the pseudo
# filesystems it is aiming at, but **nfs, nfs4, cifs and zfs as well**. A host
# whose extra storage is a NAS mount or a ZFS pool therefore reports one
# filesystem, the root one, and nothing anywhere says so: the metricset is
# healthy, the agent is green, and the dashboard draws a correct-looking panel
# with a single row.
#
# So the list is given explicitly. It is the nodev set minus the types that are
# real storage — anything not named here is collected, which is why nfs4, cifs,
# zfs, fuse.mergerfs and friends now appear. Mount points under /sys, /proc,
# /dev, /etc, /lib and /snap are dropped separately by the package's own
# default processor, so this only has to catch the pseudo filesystems mounted
# somewhere else.
FS_IGNORE_TYPES = [
    "autofs", "binfmt_misc", "bpf", "cgroup", "cgroup2", "configfs", "debugfs",
    "devpts", "devtmpfs", "efivarfs", "fuse.gvfsd-fuse", "fuse.lxcfs",
    "fuse.portal", "fusectl", "hugetlbfs", "mqueue", "nfsd", "nsfs", "overlay",
    "pipefs", "proc", "pstore", "ramfs", "rpc_pipefs", "securityfs",
    "selinuxfs", "squashfs", "sysfs", "tmpfs", "tracefs",
]

# The System integration ships a journald input and a logfile input that both
# read syslog. On any systemd host that is the same data twice, so the file
# reader goes off. winlog is Windows-only and never matches here.
SYSTEM_INPUTS = {
    "system-logfile": {"enabled": False},
    "system-winlog": {"enabled": False},
    # Only the one stream is named, which leaves cpu, memory, network, diskio,
    # process and uptime at their package defaults rather than redeclaring them.
    "system-system/metrics": {
        "streams": {
            "system.filesystem": {
                "vars": {"filesystem.ignore_types": FS_IGNORE_TYPES},
            },
        },
    },
}

# UniFi pushes logging site-wide and every adopted device sends to the collector
# directly, so these listeners take a whole site's worth of traffic rather than
# one gateway's. Both defaults break under that, and both break silently:
# max_message_size is 10 KiB and the udp input truncates a larger datagram
# without logging anything (a 20,078-byte datagram arrives as exactly 10,240),
# while an undersized socket buffer drops bursts outright. read_buffer is capped
# by net.core.rmem_max — see fleet/migrate-unifi-inputs.py for the sysctl.
UDP_OPTIONS = "read_buffer: 8MiB\nmax_message_size: 64KiB\n"


def custom_log(dataset, paths, multiline_pattern, extra=""):
    """A Custom Logs input.

    No ingest pipeline is named here: the logs@custom dispatcher routes on
    event.dataset to logs-<dataset>@custom automatically. add_locale stamps the
    host's timezone so the date processors can resolve the offset-less
    timestamps these apps write.
    """
    # NOTE the syntax. The Custom Logs package compiles to the deprecated
    # `log` input, not `filestream`, and `parsers:` is a filestream-only
    # option — a `log` input silently ignores it and ships every line raw.
    # The `log` input takes these as top-level multiline.* / json.* keys.
    custom = "multiline.type: pattern\n" \
             "multiline.pattern: '%s'\n" \
             "multiline.negate: true\n" \
             "multiline.match: after\n" \
             "multiline.max_lines: 200\n" % multiline_pattern
    return {
        "logs-logfile": {
            "enabled": True,
            "streams": {
                "log.logs": {
                    "enabled": True,
                    "vars": {
                        "paths": paths,
                        "data_stream.dataset": dataset,
                        "processors": "- add_locale: ~\n",
                        "custom": custom + extra,
                    },
                }
            },
        }
    }


def journald_errors():
    """Everything the kernel or any unit logged at error or worse."""
    return {
        "logs-journald": {
            "enabled": True,
            "streams": {
                "journald.logs": {
                    "enabled": True,
                    "vars": {
                        # 0=emerg 1=alert 2=crit 3=err
                        "include_matches": ["PRIORITY=0", "PRIORITY=1",
                                            "PRIORITY=2", "PRIORITY=3"],
                        "data_stream.dataset": "journal_errors",
                    },
                }
            },
        }
    }


ARR_ML = r"^\d{4}-\d{2}-\d{2}"
SERILOG_ML = r"^\[\d{4}-\d{2}-\d{2}"
# Kavita prefixes every line with its own app name, so a new entry does not
# start with the timestamp bracket. Without this no line looks like a new
# entry, and negate/after then glues all 200 max_lines into one document.
KAVITA_ML = r"^\[Kavita\] \[\d{4}-\d{2}-\d{2}"

# Where each app's log directory is depends entirely on how it was installed,
# and there is no single right answer:
#
#   Linux service install   /var/lib/<app>          <- what this deployment uses
#   Linux default (native)  ~/.config/<App>         note the capital: .config/Radarr
#   Docker                  whatever the host maps /config to, e.g. /opt/appdata/<app>
#
# The agent runs on the host, so a containerised app needs the *host* side of
# the /config bind mount, never /config itself. Confirm with:
#
#   for a in sonarr radarr lidarr readarr prowlarr bazarr jellyfin kavita emby; do
#     sudo find / -xdev -type f \( -iname "$a.txt" -o -iname "$a.log" \) 2>/dev/null | head -1
#   done
ARR_APPDATA = "/var/lib"
# Kavita is not installed under /var/lib: it is extracted wherever the operator
# put it and keeps config/ beside its own binaries, so this is the one path here
# that is a property of the install rather than of the distribution. Elastic
# Agent runs as root, so reading it out of a user's home is not a problem.
KAVITA_HOME = "/home/ubuntu/Kavita"

# Name the current log file rather than globbing the directory. The *arr apps
# roll at 1 MB into <app>.0.txt ... <app>.49.txt, so `*.txt` re-ingests the
# whole retained history on first run — and silently picks up <app>.debug.txt
# and <app>.trace.txt the moment anyone raises the log level in the UI, which
# is a firehose. Rotation is a rename, and the log input follows the inode, so
# tailing only the current file loses nothing while the agent is up.
# dataset -> (log path, multiline pattern)
MEDIA_APPS = [
    ("sonarr",   "%s/sonarr/logs/sonarr.txt"     % ARR_APPDATA, ARR_ML),
    ("radarr",   "%s/radarr/logs/radarr.txt"     % ARR_APPDATA, ARR_ML),
    ("lidarr",   "%s/lidarr/logs/lidarr.txt"     % ARR_APPDATA, ARR_ML),
    ("readarr",  "%s/readarr/logs/readarr.txt"   % ARR_APPDATA, ARR_ML),
    ("prowlarr", "%s/prowlarr/logs/prowlarr.txt" % ARR_APPDATA, ARR_ML),
    # Not part of the .NET Servarr family, so it does not follow the same
    # layout — Bazarr is Python and keeps its log under a data/ subdirectory.
    ("bazarr",   "%s/bazarr/data/log/bazarr.log" % ARR_APPDATA, ARR_ML),
    # Jellyfin is the odd one out: its data lives in /var/lib/jellyfin but its
    # logs do not. The Linux default is /var/log/jellyfin, overridable with
    # JELLYFIN_LOG_DIR. Files are named log_YYYYMMDD.log, so this one is a glob.
    ("jellyfin", "/var/log/jellyfin/*.log",                     SERILOG_ML),
    # Kavita keeps config/ alongside its own install directory rather than in
    # /var/lib, and the directory name is capitalised.
    ("kavita",   "%s/config/logs/*.log"          % KAVITA_HOME, KAVITA_ML),
    # Emby runs on a different VM in this deployment. That is fine — every media
    # VM shares this one policy, and an input whose glob matches nothing on a
    # given host simply collects nothing there. It does cost a warning in that
    # agent's own diagnostics, which is why the stack VM gets its own policy
    # rather than reusing this one.
    # Current file only: Emby rotates into embyserver-<ticks>.txt, and a *.txt
    # glob would both replay that history and pick up ffmpeg-*.txt transcode
    # logs, which are not application logs at all.
    ("emby",     "%s/emby/logs/embyserver.txt"   % ARR_APPDATA, ARR_ML),
]


def proxmox_policy():
    print("\n   proxmox-host")
    # CPU, memory, filesystem, diskio, network, processes, uptime + journal.
    add("system", "proxmox-host", "system", dict(SYSTEM_INPUTS))

    # linux.service is the systemd unit-state metricset — this is what answers
    # "which services are running / have failed". It is off by default.
    add("linux-metrics", "proxmox-host", "linux", {
        "system-system/metrics": {
            "enabled": True,
            "streams": {
                "linux.service": {"enabled": True, "vars": {
                    "period": "30s",
                    "service.state_filter": ["active", "failed",
                                             "activating", "deactivating"],
                }},
                # linux.raid reads mdadm state from /proc/mdstat. A Proxmox
                # host on ZFS, on an HBA, or on plain disks has none, and the
                # metricset then errors on every collection interval. Enable it
                # only if this host actually runs Linux software RAID.
                "linux.raid": {"enabled": False},
                "linux.network_summary": {"enabled": True, "vars": {"period": "30s"}},
            },
        },
        "system-linux/metrics": {
            "enabled": True,
            "streams": {
                "linux.conntrack": {"enabled": True, "vars": {"period": "30s"}},
                "linux.memory": {"enabled": True, "vars": {"period": "30s"}},
                "linux.iostat": {"enabled": True, "vars": {"period": "30s"}},
            },
        },
    })

    add("journal-errors", "proxmox-host", "journald", journald_errors())

    # Temperatures, SMART and ZFS, produced by hwstats.py. Already NDJSON, so
    # it is parsed at the edge rather than with a grok pipeline.
    add("proxmox-hardware", "proxmox-host", "log", {
        "logs-logfile": {
            "enabled": True,
            "streams": {
                "log.logs": {
                    "enabled": True,
                    "vars": {
                        "paths": ["/var/log/elastic/hwstats.ndjson"],
                        "data_stream.dataset": "proxmox_hw",
                        # json.*, not `parsers: - ndjson:` — see the note in
                        # custom_log(). keys_under_root puts the decoded fields
                        # at the document root, which is what the dashboard
                        # queries (hw.kind, hw.sensor.celsius) depend on.
                        "custom": (
                            "json.keys_under_root: true\n"
                            "json.overwrite_keys: true\n"
                            "json.add_error_key: true\n"
                            "json.expand_keys: true\n"
                        ),
                    },
                }
            },
        }
    })


def media_policy():
    print("\n   media-vm")
    add("system", "media-vm", "system", dict(SYSTEM_INPUTS))
    # Container metrics plus stdout/stderr of every container on the host.
    add("docker", "media-vm", "docker", {})
    add("journal-errors", "media-vm", "journald", journald_errors())
    for dataset, path, ml in MEDIA_APPS:
        add("%s-logs" % dataset, "media-vm", "log", custom_log(dataset, [path], ml))


def network_receivers():
    """Syslog and NetFlow listeners for the QNAP and the UniFi network.

    These bind UDP ports on the stack VM and wait to be sent to; nothing is
    polled. Every one of them defaults to listening on localhost, which would
    silently receive nothing from the network — 0.0.0.0 is the whole point.

    Ports follow the convention the Elastic integrations themselves default to,
    which is also what other SIEMs document for UniFi, so the numbers you enter
    on the devices match anything else you read.

    Each of these packages ships several alternative inputs for the same data
    stream — a file reader, a TCP listener, a journald reader. Fleet enables
    every one it is not told about, using package defaults, so the unwanted
    ones must be turned off explicitly. Left alone you get a CEF file input
    with no path to read erroring on the agent, and stray TCP listeners on
    localhost that nothing will ever connect to.
    """
    print("\n   network receivers (on stack-vm)")

    # QNAP QuLog Center → Log Sender → Send to Syslog Server.
    add("qnap-syslog", "stack-vm", "qnap_nas", {
        "qnap-tcp": {"enabled": False},
        "qnap-udp": {
            "enabled": True,
            "streams": {"qnap_nas.log": {"enabled": True, "vars": {
                "syslog_host": "0.0.0.0", "syslog_port": 9301,
                # BSD syslog timestamps carry no offset. Without this the
                # events land shifted by whatever the agent's TZ happens to be.
                "tz_offset": "local",
                "tags": ["qnap-nas", "forwarded"],
            }}},
        }
    })

    # UniFi Settings → Control Plane → Integrations → Activity Logging (Syslog)
    # → SIEM Server. Admin actions, device events, client activity, as CEF.
    add("unifi-activity-cef", "stack-vm", "cef", {
        "cef-tcp": {"enabled": False},
        "cef-logfile": {"enabled": False},
        "cef-udp": {
            "enabled": True,
            "streams": {"cef.log": {"enabled": True, "vars": {
                "syslog_host": "0.0.0.0", "syslog_port": 9003,
                "tags": ["unifi", "cef", "forwarded"],
                # The raw line is the only device attribution available on the
                # event classes that carry no UNIFIdeviceName, and the cef
                # package drops event.original before logs-cef.log@custom runs
                # unless this is set.
                "preserve_original_event": True,
                # UniFi sends empty extension values; decode_cef errors on them
                # otherwise.
                "ignore_empty_values": True,
                "udp_options": UDP_OPTIONS,
            }}},
        }
    })

    # UniFi Settings → CyberSecure → Traffic Logging → SIEM Server.
    # Firewall and IPS events, in iptables format rather than CEF.
    add("unifi-traffic-iptables", "stack-vm", "iptables", {
        "iptables-logfile": {"enabled": False},
        # This one reads the *local* journal for iptables messages. The stack
        # VM is not a firewall; the data arrives over the wire from UniFi.
        "iptables-journald": {"enabled": False},
        "iptables-udp": {
            "enabled": True,
            "streams": {"iptables.log": {"enabled": True, "vars": {
                "syslog_host": "0.0.0.0", "syslog_port": 9001,
                "tags": ["unifi", "iptables", "forwarded"],
                "udp_options": UDP_OPTIONS,
            }}},
        }
    })

    # UniFi Settings → Traffic Flows. Model-dependent: several gateways,
    # including the plain UDM, cannot export NetFlow at all. Harmless if unused
    # — an idle UDP listener costs nothing.
    add("unifi-netflow", "stack-vm", "netflow", {
        "netflow-netflow": {
            "enabled": True,
            "streams": {"netflow.log": {"enabled": True, "vars": {
                "host": "0.0.0.0", "port": 2055,
                "tags": ["unifi", "netflow", "forwarded"],
            }}},
        }
    })


def stack_policy():
    """The VM the stack itself runs on.

    Deliberately not the media-vm policy: that one tails a dozen media log
    paths which do not exist here, and a Custom Logs input pointed at nothing
    is silent noise in the agent's own diagnostics.
    """
    print("\n   stack-vm")
    add("system", "stack-vm", "system", dict(SYSTEM_INPUTS))
    # Collects the stack's own container logs — Elasticsearch, Kibana,
    # Logstash and Grafana end up searchable in Elasticsearch. Useful, and
    # bounded: docker-compose.yml caps every container's json-file driver at
    # 20 MB x 5, so a component that starts erroring loudly cannot fill the
    # disk or flood the cluster it is complaining about.
    add("docker", "stack-vm", "docker", {})
    add("journal-errors", "stack-vm", "journald", journald_errors())
    network_receivers()


def main():
    fresh_pve = ensure_agent_policy(
        "proxmox-host", "proxmox-host", "Proxmox VE hypervisor")
    fresh_vm = ensure_agent_policy(
        "media-vm", "media-vm", "Ubuntu/Debian VMs running the media stack")
    fresh_stack = ensure_agent_policy(
        "stack-vm", "stack-vm", "The Ubuntu VM running Elasticsearch, Kibana, Logstash and Grafana")

    if fresh_pve:
        proxmox_policy()
    if fresh_vm:
        media_policy()
    if fresh_stack:
        stack_policy()

    if not (fresh_pve or fresh_vm or fresh_stack):
        print("   nothing to do")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as exc:
        print("!! %s" % exc, file=sys.stderr)
        sys.exit(1)
