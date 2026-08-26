# Prompt: put MonitoringApp behind Elastic Agent

Everything below the line is the prompt. Paste it to an agent working in
`/home/jacob/Projects/MonitoringApp`, after replacing `192.168.1.50` with the
stack VM's real address.

It is an alternative to [monitoringapp.md](monitoringapp.md), which covers the
direct-to-Elasticsearch route. This one is usually the better choice: the Go
agent runs on many hosts, and giving each its own API key and CA is more moving
parts than having Elastic Agent read a file.

---

## Context

This repository is a homelab monitoring platform: a Go agent on each monitored
host, and a Java/Spring Boot backend. Both already support writing their own
logs as ECS JSON to a rolling file — `monitoring.logging.sink: file` with
`format: json` for the backend, `logging.sink: file` with `format: json` for the
agent. Neither needs new code for what follows.

Separately, an Elastic Stack 9.5.1 deployment exists at `~/elastic-logging`. It
runs in a VM on the Proxmox host and is managed through Fleet. Elastic Agent is
already installed on the Proxmox hypervisor and on the media VMs.

**Goal: have Elastic Agent collect MonitoringApp's own logs from disk, instead
of MonitoringApp shipping them to Elasticsearch itself.**

Why this way round: Elastic Agent buffers to disk when the stack is
unreachable, where both of MonitoringApp's own appenders drop events by design.
It also means no Elasticsearch credentials and no CA on any monitored host —
which matters because the Go agent runs on every one of them.

## Facts you need

| | |
|---|---|
| Stack address | `192.168.1.50` |
| Fleet Server | `https://192.168.1.50:8220` |
| Backend dataset | `monitoring.backend` → `logs-monitoring.backend-default` |
| Agent dataset | `monitoring.agent` → `logs-monitoring.agent-default` |
| Backend log path | `/var/log/monitoring-backend/backend.log` |
| Agent log path | `/var/log/monitoring-agent/agent.log` |
| CA for enrolment | `~/elastic-logging/stack/certs/ca/ca.crt` |
| Enrolment tokens | `~/elastic-logging/fleet/enrollment-tokens.sh` |

A dataset may not contain a hyphen — that separates the three parts of a data
stream name. `monitoring.backend`, never `monitoring-backend`.

## Task 1 — switch both components to the file sink

**Backend**, in `backend/src/main/resources/application.yml`:

```yaml
monitoring:
  logging:
    sink: file
    level: INFO
    file:
      path: "/var/log/monitoring-backend/backend.log"
      format: json          # ECS, not the human-readable pattern
      max-size: 50MB
      max-history: 7
      total-size-cap: 500MB
```

`deploy/backend.service` already declares `LogsDirectory=monitoring-backend`,
so systemd creates `/var/log/monitoring-backend` owned by the service user with
mode 0750 and makes it writable under `ProtectSystem=strict`. Nothing to change
there.

**Backend**, one line in `backend/src/main/resources/logback-spring.xml`, inside
the `<encoder class="co.elastic.logging.logback.EcsEncoder">` block of the FILE
appender:

```xml
<eventDataset>monitoring.backend</eventDataset>
```

Without it `logback-ecs-encoder` derives `event.dataset` from `serviceName` and
stamps `monitoring-backend`, which will not match the data stream the documents
land in. They still index, but the backend shows up under a second name on any
dashboard that groups by source.

**Agent**, in `deploy/agent-config-example.yaml` and any real config:

```yaml
logging:
  sink: file
  level: info
  format: json            # routes through newECSHandler, same as the elastic sink
  file:
    path: "/var/log/monitoring-agent/agent.log"
    max_size_mb: 50
    max_backups: 5
    max_age_days: 7
    compress: true
```

`deploy/agent.service` already declares `LogsDirectory=monitoring-agent`. The Go
agent needs no `event.dataset` — the stack fills it in from the data stream name.

Leave the `elastic` sink code in both components. It works and it is a
reasonable option for a deployment that does not have Elastic Agent; this change
is about which sink is *selected*, not about removing one.

Update `docs/backend-README.md` and `docs/agent-README.md` to describe the file
sink as the recommended production choice and say why, with a pointer to the
elastic sink as the alternative.

## Task 2 — get an Elastic Agent onto each host

**Check first whether one is already there.** A host runs at most one Elastic
Agent, and several of these hosts already have one:

```bash
elastic-agent status 2>/dev/null && echo "ALREADY ENROLLED — do not install a second"
```

- **Already enrolled** (the Proxmox host, the media VMs, the stack VM): do
  nothing here. Task 3 attaches the log collection to the policy that agent is
  already running.
- **Not enrolled** and Debian or Ubuntu: use the installer from the logging
  repo, which handles the version, the CA and the flavor flag:

  ```bash
  ~/elastic-logging/fleet/enrollment-tokens.sh     # prints the exact command
  sudo ~/elastic-logging/vms/install-agent.sh \
    --url https://192.168.1.50:8220 --token <token> --ca ./ca.crt
  ```

- **Not enrolled** and Arch: that installer is `apt`-based and will not run.
  Install from the tarball instead — same flags, and `--install-servers` is not
  optional:

  ```bash
  VERSION=9.5.1
  curl -fsSL "https://artifacts.elastic.co/downloads/beats/elastic-agent/elastic-agent-${VERSION}-linux-x86_64.tar.gz" | tar -xz
  sudo ./elastic-agent-${VERSION}-linux-x86_64/elastic-agent install \
    --non-interactive --install-servers \
    --url=https://192.168.1.50:8220 \
    --enrollment-token=<token> \
    --certificate-authorities=./ca.crt
  ```

From 9.0 the default "basic" agent flavor ships without the journald
dependencies. Omitting `--install-servers` gives you an agent that enrols
cleanly and then silently collects nothing from the journal.

If some hosts run only the Go agent and have no policy that fits, create one
called `monitoringapp-hosts` in Kibana → Fleet, with the **System** and
**Journald** integrations, and enrol them against that.

## Task 3 — collect the files

In Kibana → Fleet, add a **Custom Logs** integration. Do this twice, once per
component. A Fleet integration policy can be attached to several agent
policies at once, so create each one once and add it to every policy whose
hosts run that component — do not duplicate it per host.

**Backend:**

- Log file path: `/var/log/monitoring-backend/backend.log`
- Dataset name: `monitoring.backend`
- Custom configurations:

  ```yaml
  parsers:
    - ndjson:
        target: ""
        overwrite_keys: true
        expand_keys: true
        add_error_key: true
  exclude_files: ['\.gz$']
  ```

**Agent:**

- Log file path: `/var/log/monitoring-agent/agent.log`
- Dataset name: `monitoring.agent`
- Same `parsers` and `exclude_files` block.

Three things about that config:

- `target: ""` puts the parsed fields at the document root rather than nesting
  them under a key. Without it every ECS field ends up one level too deep.
- `overwrite_keys: true` lets the `@timestamp` and `message` inside the JSON win
  over the ones the agent would otherwise add. Without it every event is
  stamped with its collection time, not when it happened.
- `expand_keys: true` turns the flat `"log.level"` key both components emit into
  a real nested object. The stack's `logs@custom` ingest pipeline also runs a
  `dot_expander`, so this is belt-and-braces rather than load-bearing — but
  doing it at the edge keeps `_source` consistent.
- `exclude_files` keeps the reader off rotated `.gz` files. Point the path at
  the exact active file, not a `*.log` glob: both components rotate by renaming,
  and filestream follows that correctly by inode.

Do **not** set an ingest pipeline in the integration. Routing is automatic —
`logs@custom` dispatches on `event.dataset`.

No permission changes are needed. Both log directories are mode 0750 owned by
their service user, and Elastic Agent runs as root.

## Verify

Restart both components, then from the stack VM:

```bash
cd ~/elastic-logging/stack && source .env
curl -s --cacert certs/ca/ca.crt -u "elastic:$ELASTIC_PASSWORD" \
  "https://$STACK_IP:9200/logs-monitoring.*/_search?size=5&pretty" \
  -H 'Content-Type: application/json' \
  -d '{"_source":["@timestamp","event.dataset","log.level","log.logger","message","host.name"],"sort":[{"@timestamp":"desc"}]}'
```

You want, for both datasets:

- `event.dataset` of exactly `monitoring.backend` / `monitoring.agent`
- `log.level` **lowercase** — the pipeline normalises it; if you see `ERROR`,
  the document reached Elasticsearch without passing through `logs@custom`
- `@timestamp` matching when the line was logged, not when it was collected
- `host.name` set, which is the thing the direct-to-Elasticsearch route does
  not give you and the main practical reason to prefer this one

Then confirm the dashboards can actually see it — this is the check people skip:

```bash
curl -s --cacert certs/ca/ca.crt -u "elastic:$ELASTIC_PASSWORD" \
  "https://$STACK_IP:9200/logs-monitoring.*/_count?pretty" \
  -H 'Content-Type: application/json' \
  -d '{"query":{"query_string":{"query":"log.level:(error OR fatal)"}}}'
```

If nothing arrives, work outwards: is the file being written
(`tail -f /var/log/monitoring-backend/backend.log` — is it JSON?), is the agent
healthy (`elastic-agent status`), is it reading the file (Kibana → Fleet →
the agent → Logs)?

## Constraints

- No changes to Java or Go source. Everything above is configuration, and both
  components already support it.
- Do not delete the `elastic` sinks.
- Do not install a second Elastic Agent on a host that has one.
- Do not weaken the systemd hardening in `deploy/*.service` — `ProtectSystem`,
  `NoNewPrivileges` and the `LogsDirectory` modes all stay as they are.
- If you find that a documented path or setting does not match what is actually
  in the repo, report the discrepancy rather than inventing a workaround.
