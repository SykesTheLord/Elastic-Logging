# QNAP and UniFi

Both integrate, and better than I expected going in: the QNAP has a
first-party Elastic integration, and UniFi's two log streams land on two
existing Elastic integrations — one of which has Ubiquiti-specific parsing
built into it.

Neither needs anything installed on the device. Both push to UDP listeners that
the stack VM's Elastic Agent already opens once `bootstrap.sh` has run.

| Source | Integration | Listener | What you get |
|---|---|---|---|
| QNAP NAS | `qnap_nas` (official) | UDP 9301 | Event and connection logs — logins, file access, disk and app events |
| UniFi activity | `cef` | UDP 9003 | Admin actions, device events, client connect/disconnect |
| UniFi firewall | `iptables` | UDP 9001 | Per-rule allow/block with the full 5-tuple |
| UniFi flows | `netflow` | UDP 2055 | NetFlow v5/v9/v10 — model-dependent, see below |

Ports are the integrations' own defaults, which are also what other SIEMs
document for UniFi, so the numbers you type into the devices match anything
else you read.

---

## QNAP

There is an official **QNAP NAS** integration. It expects BSD syslog and parses
QNAP's two log types into ECS — `user.name`, `source.address`, `event.action`,
`qnap.nas.application`, `qnap.nas.category`, `qnap.nas.file.path`.

**On the NAS:** QuLog Center → Log Sender → Send to Syslog Server → Add
Destination.

- Destination: the stack VM's IP
- Port: `9301`
- Log Type: **Event & Access Logs** (both — access logs are what give you the
  file-access trail)

If your QuLog build pins the port to 514, use 514 and change `syslog_port` in
Kibana → Fleet → `stack-vm` → `stack-vm-qnap-syslog` to match. Elastic Agent
runs as root, so a privileged port is fine.

Verified parse, from a real-shape event log line:

```
<14>Aug 21 14:30:22 qnap-nas kernel: event log: Users: admin, Source IP: 192.168.1.20,
Computer name: laptop, Content: [Storage & Snapshots] Disk 3 was removed.
```
```
@timestamp     2026-08-21T14:30:22.000Z
user.name      admin
source.address 192.168.1.20
host.name      qnap-nas
event.category ["configuration"]
message        [Storage & Snapshots] Disk 3 was removed.
```

> BSD syslog timestamps carry no timezone. The integration's `tz_offset` is set
> to `local`, meaning the agent's own timezone — correct when the NAS and the
> stack VM agree. If they don't, set it explicitly to the NAS's offset.

---

## UniFi

Verified against **Network 10.5.67 on UniFi OS 5.1.31** (August 2026), with a UDM
Pro and a UniFi Express 7 both reporting.

There is no UniFi integration and none is needed: UniFi splits its logging in
two, and each half is something Elastic already parses. The split is also the
single easiest thing to get wrong here — the two feeds are configured in two
different places in the UI, and pointing the CEF one at the iptables port
produces a wall of grok failures.

| Feed | Configured at | Format | Port | Integration |
|---|---|---|---|---|
| Activity / platform events | Settings → **Control Plane → Integrations** → Activity Logging (Syslog) | CEF | 9003 | `cef` |
| Firewall and IPS traffic | Settings → **CyberSecure → Traffic Logging** → Activity Logging (Syslog) | iptables | 9001 | `iptables` |
| Flow records | Settings → **Traffic Flows** | NetFlow/IPFIX | 2055 | `netflow` |

Neither syslog feed offers TLS, and there is no RFC selector — framing varies
by feed, so both pipelines here accept BSD-style and RFC 5424 alike.

### Activity logs → the CEF integration

**Settings → Control Plane → Integrations →** Activity Logging (Syslog) →
**SIEM Server** → the stack VM's IP, port `9003`.

Start with Admin Activity, Security, Triggers and Updates. Device and Client are
the volume hogs with the least security value; add them once you know what your
retention looks like. *Include Raw Logs* adds full message bodies — useful, and
the reason `max_message_size` is set explicitly (see below).

CEF decoding happens **in the agent**, not in an ingest pipeline — worth knowing,
because simulating a pipeline against a CEF line shows nothing and looks like a
failure.

Verified parse of a real *Threat Detected and Blocked* event, end to end through
`decode_cef` and `logs-cef.log@custom`:

```
@timestamp            2026-08-25T08:01:01.056Z   ← from UNIFIutcTime, not the syslog header
observer.name         Dream Machine Pro
observer.ip / .mac    10.0.0.1 / 74-AC-B9-1C-25-8D
event.kind            alert
event.code            201
event.category        [intrusion_detection, network]
event.type            [denied]
event.action          blocked
event.risk_score      50            ← from UNIFIrisk: low/medium/high/critical
rule.name             ET CINS Active Threat Intelligence Poor Reputation IP group 247
rule.id               2403546
rule.ruleset          CINS Army Reputation List
rule.category         IDS/IPS
source.ip / .port     185.16.215.140 : 31805      source.geo.country_iso_code  RU
destination.ip/.port  10.99.199.249 : 49597       observer.egress.zone         Dmz
network.direction     inbound      network.bytes 196    network.packets 3
```

> **Match on `event.code`, not on the event name.** Ubiquiti keeps the numeric
> Event Class IDs stable across releases and rewords the human-readable Name
> freely. The ones worth knowing: **201** threat detected/blocked, **400**/**401**
> WiFi client connected/disconnected, **544** admin accessed UniFi Network. 544 is
> the highest-value detection on this feed — a super-admin session from an
> unexpected source address is exactly the indicator that mattered after Security
> Bulletin 064.

> **`@timestamp` needs `UNIFIutcTime`.** The `cef` package parses the BSD syslog
> header, whose date carries no offset, with a `date` processor that has no
> `timezone` option and no `event.timezone` branch — so it reads your gateway's
> local wall clock as UTC and every event lands shifted by your offset. Adding
> `- add_locale: ~` does *not* help. `logs-cef.log@custom` overrides `@timestamp`
> from `UNIFIutcTime`, which is a real UTC instant.

### Firewall logs → the iptables integration

**Settings → CyberSecure → Traffic Logging →** Activity Logging (Syslog) →
**SIEM Server** → the stack VM's IP, port `9001`. Set Flow Logging to *All
Traffic* or *Blocked Traffic Only*, and leave debug logging off.

Then **Settings → Policy Engine → Policy Table**: enable *Syslog Logging* per
rule. Sort by the **Hits** column first and enable it only on rules that
actually fire — that is the difference between sustainable logging and eMMC
wear on the gateway.

A 10.x line looks like this:

```
<13>1 2026-08-25T00:59:08+01:00 Ipercube-UDM LAN_IN-RET-20000 - - [LAN_IN-RET-20000]
DESCR="Block LAN in Monitoring" IN=br0 OUT=eth8 MAC=... SRC=10.69.1.104
DST=142.251.209.10 LEN=60 TOS=00 PREC=0x00 TTL=64 ID=26089 DF PROTO=TCP
SPT=41113 DPT=443 SEQ=1 ACK=0 WINDOW=64240 SYN URGP=0 MARK=1a0000
```

> **This half-parses, and does so without erroring.** The `iptables` package was
> written for the 9.x BSD kernel framing (`<4>Aug 25 10:01:02 host kernel:
> [LAN_IN-2000-D]IN=...`). The 10.x line still matches the package's generic
> fallback pattern, so addresses and ports come through and the document indexes
> with `event.kind: event` — while `@timestamp` falls back to ingest time,
> `observer.name` is never set, `iptables.ubiquiti.rule_number` reads `RET`, and
> `event.action` is either absent or the numeric rule id. Nothing looks broken.
> `logs-iptables.log@custom` recovers all of it from the raw line.

> **Where the raw line actually is.** `event.original` exists during ingest but
> Fleet's `.fleet_final_pipeline-1` strips it before indexing unless the
> `preserve_original_event` tag is set — and it is mapped `index: false`, so you
> cannot query for its presence either. The pipelines here fall back to `message`,
> which holds the complete line on exactly the documents that need repairing:
> the package's header `on_failure` copies it there when the framing does not
> parse. That is what makes `backfill-unifi-iptables.sh` work on data indexed
> long before any of this was installed.

After that pipeline, the same line yields:

```
@timestamp                     2026-08-24T23:59:08.000Z   ← the header's own offset
observer.name                  Ipercube-UDM
rule.name                      Block LAN in Monitoring     ← from DESCR=
rule.ruleset                   LAN_IN                      ← the chain
rule.id                        20000
iptables.ubiquiti.rule_kind    RET
event.action / event.type      drop / [connection, denied]
source.ip / destination.ip     10.69.1.104 → 142.251.209.10
```

> **Name your rules `Block …` and `Allow …`.** On 10.x the disposition is not in
> the log line at all — the bracket label is `<chain>-<kind>-<id>`, with no
> disposition component. The rule description is the only place it appears, which
> is why every SIEM's UniFi guide tells you to prefix it.

### Several devices at once

UniFi pushes both syslog configurations **site-wide**, and every adopted device
sends to the collector **directly** — the gateway, every AP, every switch. Three
consequences:

- **Firewall policy.** Permit each device VLAN → collector, not just the gateway.
- **Attribution.** Both pipelines set `observer.name` — from `UNIFIdeviceName`,
  `UNIFIhost`, or the syslog hostname for CEF; from the RFC 5424 hostname for
  iptables — and append it to `related.hosts`. `related.ip` gets source,
  destination and the reporting device, so one query finds an address whether the
  UDM Pro or the UX7 saw it.
- **The listener has to cope.** Both inputs set `read_buffer: 8MiB` and
  `max_message_size: 64KiB`. The defaults lose data silently: a datagram over
  10 KiB is truncated with nothing logged, and a burst from several devices
  overflows the socket buffer and is dropped by the kernel. `read_buffer` is
  capped by `net.core.rmem_max`, so raise that on the stack VM too:

  ```bash
  echo 'net.core.rmem_max=8388608' | sudo tee /etc/sysctl.d/99-syslog.conf
  sudo sysctl --system
  ```

The UDM Pro is the primary log source; a **UX7 is a forwarder only** — no
NetFlow, no Traffic Flows retention, no storage, and nothing of your own can run
on it. Both feed these pipelines identically.

### The pipelines

| Pipeline | Does |
|---|---|
| `unifi-cef-common` | The shared CEF → ECS normalisation: timestamp, device identity, class-ID categorisation, rule and threat fields, risk score, `related.*` |
| `logs-cef.log@custom` | Fleet's hook into the `cef` data stream; calls the above, guarded on `observer.vendor == Ubiquiti` so another CEF source is unaffected |
| `logs-iptables.log@custom` | Recovers what 10.x framing costs the `iptables` package |
| `unifi-cef-rescue` | Parses a raw CEF line *inside* Elasticsearch, for migration only — `decode_cef` has no ingest-pipeline equivalent |

All installed by `pipelines/install.sh`. Both `@custom` pipelines are invoked
twice per document (Fleet's own chain, and this repo's `logs@custom` dispatcher)
and are written to be idempotent.

### Dashboards

`stack/gen-dashboards.py` writes three for UniFi, from what Fleet reports:

| Dashboard | Answers |
|---|---|
| **Network — UniFi security** (`net-unifi`) | Both feeds together, grouped by reporting device: threats by signature and origin, firewall decisions per device, and who reached the management plane |
| **Network — UniFi firewall** (`net-unifi-firewall`) | The iptables feed on its own — actions, rule sets, blocked sources and ports |
| **Network — UniFi activity** (`net-unifi-activity`) | The CEF feed on its own — event classes, severities, reporting products |

The security dashboard is built on the ECS fields the pipelines normalise rather
than on raw integration fields, so it reads the same whether a line arrived in
9.x or 10.x framing. Its **Unattributed events** panel is the one to watch: green
at zero, and anything else means events are arriving in a shape the pipelines do
not recognise and every per-device panel above it is quietly incomplete.

### If CEF events landed on the iptables port

The symptom is a `grok_message_44b8bbb5` failure naming a `CEF:0|Ubiquiti|...`
value. Fix the destination in the UniFi UI first, then clean up:

```bash
cd ~/elastic-logging

./fleet/migrate-unifi-inputs.py --check      # then without --check
./pipelines/install.sh
./pipelines/migrate-unifi-cef.sh --check     # then without --check, then --purge
./pipelines/backfill-unifi-iptables.sh --check
```

`migrate-unifi-cef.sh` reindexes the stranded documents into `logs-cef.log-*`
through `unifi-cef-rescue`; the result is field-for-field identical to an event
that arrived on 9003, tagged `unifi-cef-rescued`, and originals are kept until
you pass `--purge`. `backfill-unifi-iptables.sh` replays
`logs-iptables.log@custom` over firewall documents already indexed, recovering
the timestamp, device name, rule identity and disposition that 10.x framing cost
them.

### Flow records → the netflow integration

**Settings → Traffic Flows.** NetFlow v5/v9/v10 to the stack VM on port `2055`.
Ubiquiti describes the export as *sampled*, so do not read it as a complete flow
record.

Model-dependent, and the exclusion list is long: **UX7, Express, Express 7, UDR,
UDR7, UDR 5G Max, UDM, UCG-Ultra and UXG-Lite** cannot do it, and it needs
gateway firmware 4.1 or newer. If your gateway is on that list, skip it — the
listener sitting idle costs nothing.

Traffic Flows also requires internal SSD or added console storage, *even when the
logs are exported*. The UDM Pro's HDD bay is for Protect video and does not
count.

### Watch for silent forwarding failures

UniFi's log export is not a stable API, and forwarding regressions have shipped
in production builds — 10.5.6x fixed one where Firewall Blocked and
Policy-Based Routing events were not reaching remote SIEM servers at all. Run
**10.5.62+ / UniFi OS 5.1.20+** at minimum, and build absence-of-signal
alerting: a synthetic block rule that fires on a schedule, and an alert on
*silence* from it. Re-validate after every firmware bump.

---

## Optional: UniFi device and client metrics

Syslog tells you what happened; it does not tell you an AP's client count,
per-client signal strength, switch port PoE draw, or channel utilisation. Those
come from the controller's API, and the usual way to get at them is
[unpoller](https://github.com/unpoller/unpoller) — actively maintained, v4.0.0
released August 2026 — which exposes the controller API as a Prometheus
endpoint.

Elastic's **Prometheus** integration (`prometheus.collector`) then scrapes it.
Run unpoller as a container on the stack VM, point a Prometheus integration at
`http://unpoller:9130/metrics`, and the device metrics land alongside everything
else.

Genuinely optional. Add it when you want to answer "why is the WiFi bad in the
back bedroom", not before.

---

## Opening the ports

The listeners bind `0.0.0.0` inside the stack VM. If it runs a firewall:

```bash
sudo ufw allow from 192.168.1.0/24 to any port 9301 proto udp comment 'QNAP syslog'
sudo ufw allow from 192.168.1.0/24 to any port 9003 proto udp comment 'UniFi CEF'
sudo ufw allow from 192.168.1.0/24 to any port 9001 proto udp comment 'UniFi iptables'
sudo ufw allow from 192.168.1.0/24 to any port 2055 proto udp comment 'UniFi NetFlow'
```

Restrict to the LAN. This is plain UDP syslog: unauthenticated, unencrypted, and
lossy under load. That is the protocol, not the configuration — anything on the
network segment can write into these data streams, which is worth knowing before
you build alerts on them.

---

## Verify

```bash
cd ~/elastic-logging/stack && source .env
for ds in qnap_nas.log cef.log iptables.log netflow.log; do
  n=$(curl -s --cacert certs/ca/ca.crt -u "elastic:$ELASTIC_PASSWORD" \
      "https://$STACK_IP:9200/logs-$ds-default/_count" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("count","-"))' 2>/dev/null)
  printf '  %-16s %s\n' "$ds" "${n:-no data stream yet}"
done
```

Nothing arriving? Work outwards, in this order:

| Check | How |
|---|---|
| Is the agent listening? | `sudo ss -ulnp \| grep -E '9301\|9003\|9001\|2055'` on the stack VM |
| Is anything arriving? | `sudo tcpdump -ni any udp port 9001` while triggering a firewall rule |
| Is the agent unhappy? | Kibana → Fleet → the stack-vm agent → Logs |
| Did the device send? | UniFi: re-check the port. QNAP: QuLog has a *Send a Test Message* button |

The most common cause of silence is a listener bound to `localhost`. Every one
of these integrations defaults to that, and `setup-policies.py` overrides it to
`0.0.0.0` — if you rebuild a policy by hand in the UI, check that field first.

---

## What I could not confirm

- **Whether UniFi Traffic Logging needs a CyberSecure subscription.** The
  setting lives under a menu named CyberSecure, which is a paid tier, but
  Ubiquiti's own documentation blocks automated access and no third-party source
  states it either way. Look in your own UI — if Traffic Logging is greyed out,
  that is the answer, and the CEF activity stream and NetFlow are unaffected.
- **The exact menu paths on your firmware.** Ubiquiti moves these settings
  between releases — Activity Logging lived under Settings → System → Advanced
  before 8.5, and the Control Plane / CyberSecure split arrived in 9.x. The
  ports, formats and field names below are what matter; the menu path is what
  drifts.
- **Which `UNIFI*` extension keys appear on which event class.** Only class 201
  has been observed here in full. `logs-cef.log@custom` therefore sets each
  field only when its source key is present, and falls back through
  `UNIFIdeviceName` → `UNIFIhost` → the syslog hostname for device identity
  rather than assuming any one of them exists.
- **`UNIFIbytesSent` / `UNIFIbytesReceived` orientation.** They sum to
  `UNIFItotalBytes`, but nothing states which endpoint they are relative to, so
  only the totals are mapped to `network.bytes` / `network.packets`. The
  directional pair stays under `cef.extensions.*` rather than being guessed onto
  `source.bytes` / `destination.bytes`.
- **Suricata 8 field changes.** Network 10.5 on UniFi OS 5.1.20+ migrates the
  IPS engine from Suricata 6 to 8, which changes the EVE schema upstream. The
  CEF feed exposes `UNIFIthreatType` / `UNIFIthreatCategory` where 10.5 sends
  `UNIFIipsSignature` / `UNIFIpolicyType`; both spellings are mapped, but only
  the latter pair has been seen on real traffic here. EVE JSON itself is still
  not exposed through the UI at all.
- **Whether `read_buffer: 8MiB` is actually granted.** The agent reports the
  value it asked for, not what the kernel applied, and `SO_RCVBUF` is capped by
  `net.core.rmem_max` — 208 KiB on a stock Ubuntu. Raise the sysctl and treat
  the setting as a request. The `max_message_size: 64KiB` half *is* verified: a
  20,078-byte datagram arrives whole with it and as exactly 10,240 bytes without.
