# Homelab logging stack

Elasticsearch + Logstash + Kibana + Grafana in one Ubuntu VM on Proxmox VE 9,
Elastic Agent on the hypervisor and on every guest, all managed centrally
through Fleet.

```
                       Proxmox VE 9 host
  ┌───────────────────────────────────────────────────────────────────┐
  │  hypervisor agent ──────────────┐                                 │
  │  temps · SMART · units · journal│                                 │
  │                                 ▼                                 │
  │  ┌───────────────┐   ┌────────────────────────────────────────┐   │
  │  │ media VMs     │──▶│  stack VM  (Ubuntu 24.04, Docker)      │   │
  │  │ *arr · Emby   │   │                                        │   │
  │  │ Jellyfin      │   │  Fleet Server :8220                    │   │
  │  │ Kavita        │   │  Logstash     :5000 / :5001 TLS        │   │
  │  └───────────────┘   │  Elasticsearch :9200                   │   │
  │                      │  Kibana :5601      Grafana :3000       │   │
  │  ┌───────────────┐   │                                        │   │
  │  │ other guests  │──▶│                                        │   │
  │  └───────────────┘   └────────────────────────────────────────┘   │
  └───────────────────────────────────────────────────────────────────┘
                                  ▲
              apps elsewhere ─────┘  TCP :5000
```

## Why a VM and not an LXC container

On Proxmox the tempting option is an LXC container — lighter, faster to start,
no kernel to boot. Do not.

Elasticsearch needs `vm.max_map_count=262144`, and LXC containers share the
host's kernel. Setting it for a container means setting it on the **Proxmox
host itself**, where it applies to the hypervisor and every other guest. That is
a real change to a machine whose job is running your VMs, made to satisfy one
service. Docker inside LXC then needs nesting and keyctl workarounds on top.

A VM has its own kernel, takes an ordinary `/etc/sysctl.d` drop-in that affects
nothing else, and snapshots as a unit before an upgrade.

## What you need

### The VM

| Setting | Value | Why |
|---|---|---|
| OS | Ubuntu Server 24.04 LTS | |
| Cores | 4 | |
| CPU type | `host` | Passes through the real CPU. Elasticsearch leans on modern vector instructions. |
| Memory | **12 GB, ballooning off** | Ballooning against a fixed JVM heap causes the guest to swap while the host thinks it has freed memory. |
| Disk | 250 GB, SCSI on **VirtIO SCSI single**, IO thread on | The default controller in PVE 9, and the fastest path for a write-heavy service. |
| Discard / SSD emulation | on, if the backing store is SSD | Lets deleted index segments actually return space to a thin pool. |
| Start at boot | yes, `startup order=1` | The stack should be up before the guests that log to it. |
| Guest agent | installed | `qemu-guest-agent` gives clean shutdowns, which Elasticsearch cares about. |

```bash
# On the Proxmox host, once the VM exists:
qm set <vmid> --cpu host --balloon 0 --onboot 1 --startup order=1 --agent enabled=1
```

### Storage

Put the VM disk on SSD-backed storage. Elasticsearch on spinning rust is
genuinely painful, and no amount of tuning fixes it.

If that storage is **ZFS**, budget for the ARC. The Proxmox host's ZFS cache
competes with your guests for the same RAM, and a host with 32 GB running a
12 GB stack VM can find ARC has quietly taken 8 GB of what is left. Either cap
it or size the host accordingly:

```bash
# /etc/modprobe.d/zfs.conf — 8 GB ARC ceiling, then update-initramfs -u
options zfs zfs_arc_max=8589934592
```

LVM-thin avoids the question entirely and is the simpler choice here.

### Network

A static IP, and outbound internet on first boot — Kibana downloads its
integration packages from `epr.elastic.co`. Everything after that is local.

### Software

Ubuntu's own `docker.io` package does **not** include Compose v2, which is what
these scripts use. Install Docker's own packages:

```bash
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
```

Everything else the scripts need — `python3`, `curl`, `openssl`, `ss` — is on a
stock Ubuntu Server install. `bootstrap.sh` checks for the ones that are not,
along with RAM against `ES_HEAP`, free disk, port conflicts, and whether the
clock is NTP-synchronised, before it starts anything.

Run the scripts as root or under `sudo`. They detect which and behave the same
either way.

8 GB RAM works if you drop `ES_HEAP` to `2g`, but leave headroom:
Elasticsearch wants roughly half the VM's RAM as heap and the other half as
filesystem cache.

> **Know what this cannot tell you.** The stack runs on the hypervisor it
> monitors. If the Proxmox host goes down, so does your view of why — the
> hypervisor's agent buffers to disk and backfills once the VM returns, so you
> lose no history, but you lose it live. Diagnose a dead hypervisor with
> `journalctl` on the host, the way you would have anyway. This stack is for
> history and correlation, not for watching its own floor give way.

## Install

### 1. The stack, on its VM

```bash
cd stack
cp .env.example .env
$EDITOR .env          # STACK_IP, STACK_DNS and every password
./setup-certs.sh      # private CA + per-service certificates
./bootstrap.sh        # brings everything up, creates users, policies, pipelines
```

`setup-certs.sh` bakes `STACK_IP` and `STACK_DNS` into the certificate SANs, so
get those right first. Changing them later means re-running it and re-enrolling
every agent.

`bootstrap.sh` is idempotent — re-run it after any change.

### 2. Agents

Copy `stack/certs/ca/ca.crt` to each machine, then get the enrollment tokens:

```bash
./fleet/enrollment-tokens.sh
```

It prints the exact command per policy. Three policies, three targets:

```bash
# On the Proxmox host — hardware, systemd units, journal
sudo ./proxmox/install-agent.sh --url https://<STACK_IP>:8220 --token <token> --ca ./ca.crt

# On each media VM
sudo ./vms/install-agent.sh --url https://<STACK_IP>:8220 --token <token> --ca ./ca.crt

# On the stack VM itself — its own health, plus the stack's container logs
sudo ./vms/install-agent.sh --url https://<STACK_IP>:8220 --token <token> --ca ./ca.crt
```

The stack VM gets the `stack-vm` policy, which is deliberately *not* the
media-vm one — that would point a dozen Custom Logs inputs at media paths which
do not exist there. It collects system metrics, the journal, and the stack's own
container logs, so Elasticsearch's complaints end up searchable in
Elasticsearch. That is bounded rather than circular: `docker-compose.yml` caps
every container's log driver at 20 MB × 5, so a component erroring loudly cannot
flood the cluster it is complaining about.

Agents appear in Kibana → Fleet → Agents within about a minute.

> Both installers pass `--install-servers`. From 9.0 the default "basic" agent
> flavor ships **without** the journald dependencies, and the Journald
> integration then silently collects nothing at all. This is the single most
> common way a 9.x agent install ends up half-working.

## What gets collected

### Proxmox host — policy `proxmox-host`

| Requirement | How |
|---|---|
| Temperatures | `hwstats.py` reads `sensors -j` every 60s → `logs-proxmox_hw` |
| Disk health | same script, `smartctl --json` per disk: SMART pass/fail, temperature, power-on hours, reallocated + pending sectors, NVMe wear and media errors |
| ZFS | `zpool list` / `zpool status -x` health and capacity |
| Services running | `linux.service` metricset — systemd unit state, filtered to `active`, `failed`, `activating`, `deactivating` |
| Errors | Journald input filtered to `PRIORITY 0-3`, plus the full syslog and auth streams |
| Network traffic | `system.network` per interface, `linux.network_summary`, `linux.conntrack` |
| Everything else | CPU, load, memory, filesystem, diskio, processes, uptime, RAID |

`hwstats.py` exists because Elastic Agent has no native lm-sensors or SMART
input. It writes NDJSON that the Custom Logs integration parses at the edge — no
grok, no ingest pipeline. It depends only on the Python 3 standard library and
degrades quietly if `sensors`, `smartctl` or `zpool` is missing.

### VMs — policy `media-vm`

| Requirement | How |
|---|---|
| Docker | Container metrics (cpu, memory, network, diskio, healthcheck, events) plus every container's stdout/stderr |
| Kavita, Emby, Jellyfin | Custom Logs, one dataset each, with the right multiline pattern so stack traces stay attached to their message |
| Arr stack | Sonarr, Radarr, Lidarr, Readarr, Prowlarr, Bazarr — one dataset each |
| General errors | Journald `PRIORITY 0-3` + syslog + auth |
| Host health | The full System integration |

**Check the log paths.** The policies default to `/opt/appdata/<app>/...`. The VM
installer tells you which of those actually exist on that machine; if yours are
elsewhere, fix them in Kibana → Fleet → `media-vm` → the relevant integration.
Wrong paths fail silently.

### The stack VM — policy `stack-vm`

System metrics, the journal at error and above, and the stack's own container
logs (`logs-docker.container_logs-default`). No media log paths.

It also opens the network-device receivers, since it is the one guest that is
always up and holds the cluster:

| Listener | Integration | Source |
|---|---|---|
| UDP 9301 | `qnap_nas` | QNAP event and access logs |
| UDP 9003 | `cef` | UniFi activity logs |
| UDP 9001 | `iptables` | UniFi firewall rules |
| UDP 2055 | `netflow` | UniFi flow records |

Nothing is installed on the QNAP or the UniFi gear — they push. See
[docs/network-devices.md](docs/network-devices.md) for the device-side settings
and the model caveats on NetFlow.

## Changing what a policy collects

> `setup-policies.py` creates policies; it never updates them. Once a policy
> exists, editing that file — or restarting the stack — changes nothing, because
> the policies live in Fleet's saved objects rather than in any file the stack
> reads at boot. The `fleet/migrate-*.py` scripts exist for exactly this:
> `migrate-media-paths.py` pushes the current `MEDIA_APPS` paths and multiline
> patterns onto the live policies, `migrate-unifi-inputs.py` does the same for
> the network listeners, and `migrate-filesystem-types.py` does it for
> `FS_IGNORE_TYPES`. All three take `--check`.


`fleet/setup-policies.py` only ever **creates** policies — it leaves existing
ones alone so it cannot stomp changes you make in Kibana. Editing that file and
re-running `bootstrap.sh` therefore changes nothing on a live stack; it is a
seed, not a source of truth.

To change a policy that already exists, either use Kibana → Fleet → Agent
policies, or:

```bash
cd fleet
./toggle-stream.py --list                                     # what exists, and what is on
./toggle-stream.py proxmox-host-linux-metrics linux.raid off  # turn one off
```

Agents pick the change up on their next check-in, usually within a minute.
Nothing needs restarting on the monitored host.

`linux.raid` is off by default: it reads mdadm state from `/proc/mdstat`, and a
Proxmox host on ZFS, on an HBA, or on plain disks has none — the metricset then
errors on every collection interval. Turn it on only if the host really does
run Linux software RAID.

### Disks that do not show up

The filesystem metricset has a default worth knowing about. With no
`filesystem.ignore_types` set it ignores every type marked `nodev` in
`/proc/filesystems` — which is meant to skip pseudo filesystems, but **`nfs`,
`nfs4`, `cifs` and `zfs` are all marked `nodev` too**. A Proxmox host on ZFS, or
any host with a NAS mount, then reports its root filesystem and nothing else.
Nothing anywhere says so: the metricset does not error, the agent stays green,
and the dashboard draws a healthy-looking panel with one row in it.

`FS_IGNORE_TYPES` in `setup-policies.py` therefore names the ignored types
explicitly — the pseudo filesystems, and nothing that is real storage. New
deployments get it; existing ones need:

```bash
cd fleet
./migrate-filesystem-types.py --check   # what differs
./migrate-filesystem-types.py           # apply
```

Verify with the data rather than the agent's status, because the missing mounts
were never an error:

```bash
curl -sk -u elastic:$ELASTIC_PASSWORD \
  "https://localhost:9200/metrics-system.filesystem-*/_search" \
  -H 'Content-Type: application/json' -d \
  '{"size":0,"aggs":{"t":{"terms":{"field":"system.filesystem.type"}}}}'
```

A disk with **no mounted filesystem** stays invisible whatever you set here —
an LVM-thin pool, a ZFS zvol backing a VM, an unformatted spare. There is no
filesystem on it to measure. ZFS pool capacity comes from `hwstats.py` instead,
on the ZFS pools panel; LVM-thin usage is not collected by anything here.

### Parsing

Raw log lines are turned into `@timestamp`, `log.level`, `log.logger` and a
clean `message` by ingest pipelines:

```
logs@default-pipeline           built in, Elastic-managed
  └─ logs@custom                this stack's dispatcher
       ├─ dot_expander          flat "log.level" → nested log.level
       ├─ backfill              event.dataset + data_stream.* from the index name
       ├─ logs-<dataset>@custom the per-app parser, if one exists
       │    ├─ logs-sonarr@custom   ─▶ arr-common
       │    ├─ logs-jellyfin@custom ─▶ serilog-common
       │    └─ logs-bazarr@custom   ─▶ bazarr-common
       └─ lowercase log.level   one casing across every producer
```

The three cross-cutting steps exist because producers disagree: the
`ecs-logging-*` libraries write flat dotted keys and uppercase severities where
Elastic Agent writes nested keys and lowercase, and an application writing
straight to `_bulk` sets neither `event.dataset` nor `data_stream.*`. Handling
that centrally means no application has to be patched to fit the dashboards.

Adding an app is: write `logs-<name>@custom`, point a Custom Logs integration at
it. Nothing else changes, and nothing here breaks on a stack upgrade because
`@custom` pipelines are the documented user hook that Elastic never overwrites.

Every pipeline degrades gracefully: a line that matches no pattern keeps its
original `message` rather than being dropped.

Bazarr is the one to double-check — it is Python where the rest of the *arr
family is .NET, and its format has changed between releases. Compare a real line
against `pipelines/bazarr-common.json` and adjust the grok if it does not match.

## Sending application logs

Newline-delimited, to `<STACK_IP>:5000` (plain) or `:5001` (TLS).

**If your app already emits ECS** — Spring Boot's `StructuredLogEncoder`, any
`ecs-logging-*` library — it is passed straight through to the document root
untouched, in either the nested or the flat-dotted spelling. The dataset comes
from `event.dataset`, else `service.name`. Nothing is re-wrapped, because the
document is already correct.

Otherwise JSON is detected and its fields promoted:

```bash
echo '{"dataset":"payments","level":"error","message":"charge failed","order_id":991}' \
  | nc 192.168.1.50 5000
```

| Field in your JSON | Becomes |
|---|---|
| `dataset` / `service` / `app` | the data stream — `logs-app.<name>-default` |
| `message` / `msg` / `log` | `message` |
| `level` / `severity` / `levelname` / `loglevel` | `log.level`, lowercased |
| `timestamp` / `time` / `ts` | `@timestamp` (ISO8601, epoch seconds or millis) |
| `host` / `hostname` | `host.name` |
| `logger`, `exception`, `trace_id` | `log.logger`, `error.stack_trace`, `trace.id` |
| anything else | kept under `app.*` |

Plain text works too — it lands in `logs-app.generic-default`, and a severity
word anywhere in the line is picked up as `log.level`.

For an app that would rather write to Elasticsearch directly, mint it a key
that can create documents in one data stream and do nothing else:

```bash
cd stack && ./create-app-credentials.sh myapp.component
```

Full detail, including which route to pick, is in
[docs/shipping-logs.md](docs/shipping-logs.md).

Dataset names are sanitised (lowercased, non-alphanumerics to `_`, truncated),
so `Payments-API` becomes `logs-app.payments_api-default` rather than being
rejected.

## Grafana

`http://<STACK_IP>:3000`, admin / `GRAFANA_ADMIN_PASSWORD`.

Two provisioned datasources (`logs-*` and `metrics-*`, read-only credentials)
and a **Homelab Overview** dashboard: log volume and errors by source, Proxmox
and per-disk temperatures, a SMART health table, network throughput, non-active
systemd units, and a live error stream.

Kibana is at `https://<STACK_IP>:5601` and you will still want it — Fleet lives
there, and Discover is a much better log explorer than Grafana's Elasticsearch
datasource. Grafana is the dashboard; Kibana is the microscope.

### Generated dashboards

`stack/gen-dashboards.py` writes a dashboard per host and one per network
receiver. `bootstrap.sh` runs it; re-run it whenever the estate changes:

```bash
./stack/gen-dashboards.py              # write or refresh everything
./stack/gen-dashboards.py --list       # what it found, without writing
./stack/gen-dashboards.py --dry-run    # which files would change
```

Hosts come from Fleet, not from a list in the script, and **the panels each one
gets are chosen from what its agent policy actually collects**. A host on
`proxmox-host` grows temperature, SMART and ZFS panels because that policy
carries `proxmox_hw`; one on `media-vm` gets container and per-app rows instead,
built from the Custom Logs datasets really configured on it. Add an integration
in the Fleet UI and the next run reflects it — there is nothing here to keep in
step by hand.

| Dashboard | Covers |
|---|---|
| `Host — <name>` | One per enrolled agent: CPU, memory, filesystem, network and disk rates, a table of every mounted filesystem (size, mount point, type, free and in use), log volume and errors, plus whatever else its policy collects |
| `Network — receiver health` | All four UDP receivers: event counts, how long since each last heard anything, and which addresses are sending |
| `Network — QNAP NAS` | Users, source addresses, categories, applications, the file-access trail |
| `Network — UniFi firewall` | Allow vs block, rule sets and rule numbers, top blocked sources and ports |
| `Network — UniFi activity` | CEF event types and severities, reporting devices |
| `Network — flows (NetFlow)` | Top talkers, conversations and destination ports by bytes |

Output goes to `grafana/provisioning/dashboards/json/generated/`, which the
existing provisioner already watches — new dashboards appear within 30 seconds
and nothing restarts. Files are rewritten only when their content actually
changes, so a re-run does not discard UI edits to dashboards whose data has not
moved, and a host that leaves Fleet takes its dashboard with it.

Everything it writes carries a `generated` tag, and that tag is the only thing
that makes a file eligible to be overwritten or removed. Anything you write
yourself in that directory — or `homelab-overview.json` in the parent — is never
touched, even if it happens to share a filename.

Receiver health is the panel to look at first. Every other source in this stack
polls or tails a file and fails loudly; the four network receivers sit on a UDP
socket waiting to be sent to, so a NAS that stopped sending, a changed port and a
quiet night all look identical on a graph of counts. "Last event" tells them
apart.

## Retention

`LOG_RETENTION_DAYS` (default 30) and `METRIC_RETENTION_DAYS` (default 90) in
`.env` become ILM policies applied through the `logs@custom` and `metrics@custom`
component templates. Those are composed by *every* data stream template —
integration and Logstash alike — so the setting really is global. Replicas are
forced to 0, which is correct for a single node and keeps the cluster green
instead of permanently yellow.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Elasticsearch exits immediately | `vm.max_map_count`. `bootstrap.sh` sets it; check `sysctl vm.max_map_count`. |
| Kibana will not start | `KIBANA_ENCRYPTION_KEY` must be at least 32 characters. |
| Agent enrolls but sends nothing | Almost always a wrong log path. Kibana → Fleet → agent → Logs. |
| Journald integration is empty | The agent was installed without `--install-servers`. Re-run the installer. |
| Agent install stops at `tar: … Cannot change mode …: Bad address` | The 9.x agent tarball ships 80 directory entries with mode `0o20000000755` — Go's `fs.ModeDir` bit leaked into the tar header. Most kernels mask it off; some (QNAP QTS under LXD) fail the chmod with EFAULT. The download is fine — the installers retry the extraction with python. |
| Agent cannot reach Fleet Server | The cert only covers `STACK_IP` and `STACK_DNS`. Enrolling by any other name fails hostname verification. |
| Agents enrol fine, then every one fails with `x509: certificate signed by unknown authority` — while Kibana, Grafana and Logstash are all healthy | Elasticsearch is serving a leaf-only chain. Agents trust it by CA fingerprint, which matches a CA *in the presented chain*, so the CA must be appended to `certs/elasticsearch/elasticsearch.crt`. Everything else uses a CA file directly, which is why only the agents break. See below. |
| Logstash TLS listener dead | Its key must be PKCS#8. `setup-certs.sh` converts it; check `certs/logstash/logstash.pkcs8.key` exists. |
| Everything yellow | Fixed by the replicas-0 component template. Re-run `bootstrap.sh`. |
| Elasticsearch slow, host busy | ZFS ARC on the Proxmox host competing with the VM. Cap `zfs_arc_max`. |
| Guest swaps while the host looks fine | Ballooning is on. `qm set <vmid> --balloon 0`. |
| Agents fail to connect after a host reboot | The stack VM booted after its guests. `qm set <vmid> --onboot 1 --startup order=1`. |
| QNAP or UniFi logs never arrive | A syslog listener bound to `localhost`, or the stack VM's firewall. See [docs/network-devices.md](docs/network-devices.md). |

### Custom Logs collected but never parsed

Symptom: `logs-proxmox_hw-default` fills up, but the temperature and SMART
panels stay empty — and media-app stack traces arrive one line per document.

The Custom Logs package compiles to the deprecated **`log`** input, not to
`filestream`, and `parsers:` is a filestream-only option. A `log` input ignores
it silently and ships every line raw, so `hw.kind` and `hw.sensor.celsius`
never exist. It takes the same settings as flat `json.*` / `multiline.*` keys
instead.

```bash
cd fleet
./migrate-custom-logs.py --check   # report only
./migrate-custom-logs.py           # convert every affected policy in place
```

Idempotent, and it refuses any hand-edited block it does not recognise rather
than mangling it. Agents apply the change on their next check-in. Documents
already indexed are not rewritten — only data collected from then on is parsed.

### Agents fail with "certificate signed by unknown authority"

Deployments whose certificates were generated before this was fixed serve a
leaf-only chain. Agents trust Elasticsearch by CA fingerprint, which matches a
CA *in the presented chain*, so it never matches — while Kibana, Grafana,
Logstash and Fleet Server stay healthy, because they use a CA file directly.
Enrolment succeeds too, so agents appear, go healthy and quietly ship nothing.

```bash
cd stack
./migrate-cert-chain.sh --check   # report only
./migrate-cert-chain.sh           # back up, fix, restart, verify
```

It appends the existing CA to each service certificate. It does **not**
regenerate anything — the CA is unchanged, so no agent needs re-enrolling and
they reconnect on their own. Idempotent, and it keeps a timestamped backup.

Do not reach for `setup-certs.sh` here: that mints a *new* CA and would
invalidate every enrolled agent.

Useful:

```bash
docker compose logs -f elasticsearch kibana fleet-server logstash
elastic-agent status                 # on an agent host
journalctl -u elastic-hwstats -n 50  # on Proxmox
curl -s --cacert stack/certs/ca/ca.crt -u elastic:$PW \
  https://<STACK_IP>:9200/_cat/indices/logs-*?v
```

## Deliberately not included

- **Packetbeat / `network_traffic`.** Flow-level capture on a hypervisor with
  bridged VM traffic is expensive and noisy. `system.network` and
  `linux.network_summary` answer "how much traffic, on which interface". Add the
  Network Packet Capture integration to the `proxmox-host` policy if you want
  per-connection detail.
- **Alerting.** Kibana rules or Grafana alerts, once you know what normal looks
  like. Setting thresholds before you have a baseline just teaches you to ignore
  them.
- **Snapshots.** The stack is reproducible from this repo but the *data* is not.
  Register a snapshot repository on an NFS share if the history matters to you.
- **A second Elasticsearch node.** One node, replicas 0. A disk failure loses
  the logs; that is the right trade for a homelab.

## Layout

```
stack/        docker-compose.yml, .env, cert + bootstrap scripts, per-app
              credential minting, the cert-chain migration, the dashboard
              generator, Logstash and Grafana config
pipelines/    ingest pipelines and their installer
fleet/        agent policy provisioner, stream toggle, enrollment tokens
proxmox/      agent installer, hwstats.py, systemd units
vms/          agent installer
docs/         integration guides
```

## Guides

- [Shipping logs from anything](docs/shipping-logs.md) — the three routes in,
  dataset naming, adding a parser, and how to verify.
- [MonitoringApp](docs/monitoringapp.md) — what to fix before its Java backend
  and Go agent can connect.
- [CalendarSync](docs/calendarsync.md) — one wrong port, and a TLS decision.
- [QNAP and UniFi](docs/network-devices.md) — device-side settings, what parses,
  and what to check when nothing arrives.
- [Prompt: MonitoringApp behind Elastic Agent](docs/monitoringapp-agent-prompt.md)
  — hand this to an agent working in that repo to switch it from
  direct-to-Elasticsearch onto file + Elastic Agent.
