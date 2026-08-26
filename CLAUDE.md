# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

An Elastic Stack 9.5.1 + Grafana deployment for a homelab, targeting an Ubuntu VM on
Proxmox VE 9. Bash and stdlib Python plus JSON/YAML config — no application code, no
build, no package manager. The configuration *is* the product.

## Where each script runs

Three different machines. Getting this wrong is the easiest mistake to make here.

| Directory | Runs on |
|---|---|
| `stack/`, `fleet/`, `pipelines/` | the **stack VM** — they reach Elasticsearch and Kibana at `https://localhost:…` and read `stack/certs/` |
| `proxmox/` | the **Proxmox host** (the hypervisor itself, not a guest) |
| `vms/` | each guest, **including the stack VM**, which needs its own agent |

## Verifying changes

`./lint.sh` runs shellcheck plus parse checks over every script and config. It uses a
local shellcheck if there is one and the `koalaman/shellcheck` container otherwise, so
it needs nothing installed. `--quick` skips shellcheck.

But linting is not verification. Every serious bug found in this repo so far was
runtime-only and passed both `bash -n` and shellcheck cleanly. Exercise a change before
calling it done:

- **Config or Fleet API changes** — real Elasticsearch + Kibana containers, then query
  the compiled agent policy or the indexed document to confirm the effect.
- **Script changes** — an `ubuntu:24.04` container standing in for the VM: run as root,
  `--network host`, mount `/var/run/docker.sock`, and bind-mount the repo **at a path
  identical to the host's** so the inner `docker compose` bind mounts resolve.
- **Agent input or parser changes** — `docker.elastic.co/beats/filebeat:9.5.1` with a
  few lines of config and console output. Much cheaper than a full Fleet enrolment.
- **Dashboard changes** — a real Grafana container with the provisioning directory
  mounted proves the JSON loads; only running the panel targets through
  `/api/ds/query` against an Elasticsearch holding documents proves the queries
  return anything. An empty panel and a correct one look identical in the UI.

Tear the containers down afterwards. `/verify-stack` automates the common cases.

## Landmines

These look correct and silently do nothing:

- **Custom Logs compiles to the deprecated `log` input, not `filestream`.** `parsers:`
  is filestream-only and is ignored — use flat `json.*` / `multiline.*` keys instead.
  Still true as of package `log` 2.4.4, now titled **Custom Logs (Deprecated)**: it
  emits `allow_deprecated_use: true`, which is the only reason the input still starts
  at all — standalone Filebeat 9.5.1 refuses a `log` input outright ("Log input is
  deprecated. Use Filestream input instead"). The successor is the separate
  `filestream` package, **Custom Logs (Filestream)** 2.5.1, which takes `parsers:`
  properly. Migrating means new integration policies, not an edit to the existing
  ones — and `fleet/migrate-custom-logs.py` converts the other direction.
- **`fleet/setup-policies.py` only ever *creates* policies.** Editing it changes nothing
  on a live stack, and neither does restarting the stack — the policies live in Fleet's
  saved objects, not in a file anything reads at boot. Use `fleet/migrate-media-paths.py`
  for the media log paths and multiline patterns, `fleet/migrate-unifi-inputs.py` for the
  UniFi listeners, `fleet/migrate-filesystem-types.py` for `FS_IGNORE_TYPES`,
  `fleet/toggle-stream.py`, or the Fleet UI.
- **Logstash's `data_stream_dataset` is not sprintf-templated.** Per-event routing needs
  `index =>` with `action => create`, plus `manage_template => false`.
- **Agents trust Elasticsearch by CA fingerprint**, which only matches a CA present in
  the *served chain* — so each service `.crt` must have the CA appended to it.
- **Elastic Agent 9.x needs `--install-servers`**, or the journald input collects nothing.
- **`/etc/os-release` defines `VERSION`.** Never name a shell variable `VERSION` in a
  script that sources it.
- **UniFi firewall logs changed shape in Network 10.x, and the package half-parses
  them without erroring.** 10.x sends RFC 5424 (`<13>1 <ts> <host> <rule> - - [<rule>]
  DESCR="..." IN=...`), which still matches the iptables package's generic fallback
  pattern — so the document indexes cleanly while losing `@timestamp`, `observer.name`,
  the rule number (it reads `RET`) and the disposition. `logs-iptables.log@custom`
  puts them back from the raw line.
- **`event.original` is gone by the time anything can query it.** Fleet's
  `.fleet_final_pipeline-1` removes it unless the `preserve_original_event` tag is
  set, so an `@custom` pipeline can read it during ingest but a backfill over
  already-indexed data cannot — and `exists` on it matches nothing regardless,
  because it is mapped `"type": "keyword", "index": false`. Fall back to `message`:
  a package's header `on_failure` copies the whole line there, which happens on
  exactly the documents whose framing it failed to parse.
- **A grok failure aborts the whole package pipeline, so `logs-<dataset>@custom`
  never runs.** Fleet appends the `@custom` call to the *end* of the package
  pipeline; the pipeline-level `on_failure` stops execution before reaching it. The
  `@custom` hook can enrich a successful parse, never rescue a failed one.
- **`logs@custom` runs for integration data streams too.** Fleet's chain is
  `global@custom` → `logs@custom` → `logs-<pkg>.integration@custom` →
  `logs-<dataset>@custom`, so this repo's dispatcher fires as well and any
  `logs-<dataset>@custom` for an integration is invoked **twice**. Write them
  idempotently: guard on the field being unset, and dedupe before appending.
- **The udp input truncates at 10 KiB and says nothing.** `max_message_size`
  defaults to 10 KiB; a 20,078-byte datagram arrives as exactly 10,240 bytes with
  no error anywhere. Long CEF lines lose their trailing extensions and still parse,
  which is why it goes unnoticed. Both UniFi listeners set it explicitly.
- **`event.action` on the iptables feed is a single letter *only sometimes*.** The
  9.x `[LAN_IN-2000-D]` label yields `d`/`a`/`r`; iptables ≥1.23.1 maps `d`→`drop`
  and `a`→`accept` in its own pipeline but **not** `r`, which stays `r` until
  `logs-iptables.log@custom` expands it. On 10.x the disposition is not in the log
  line at all — only in the `DESCR=` text, which is why rules are named `Block …`
  and `Allow …`.
- **`%{GREEDYDATA}` stops at the first newline, and grok anchors `$` per line.** So a
  parser ending `%{GREEDYDATA:_tmp.msg}$` *succeeds* on a joined multiline event by
  matching the first line only — and a following `set message copy_from _tmp.msg`
  then replaces the whole event with that line, throwing the stack trace away with
  no error anywhere. Every media-app parser here had this. Capture the body with
  `MSGBODY: [\s\S]*` instead. Not `(?s)`: Elasticsearch's grok **rejects inline
  flags**, and the resulting pipeline PUT fails — which is silent if you discard
  curl's output, leaving the old pipeline in place and a green-looking test.
- **A terms aggregation drops documents that lack the field.** Grouping a table
  by `host.name` shows no network devices at all — they set `observer.name` and no
  host — rather than showing them with the column blank, and in a *nested* bucket
  the outer row disappears too, so a table quietly loses rows while still looking
  populated. Set a `missing` value. It is parsed as the field's own type, so a
  string sentinel on `source.ip` or `destination.port` throws and the panel then
  renders empty rather than erroring visibly: words for keywords, numbers for
  numerics, nothing for `ip`.
- **`iptables.ubiquiti.input_zone` / `output_zone` are always empty for UniFi.**
  The integration splits them out of the rule set name on a hyphen; UniFi's chains
  use underscores (`LAN_IN`, `WAN_LOCAL`). A panel grouped on them draws an
  authoritative blank.
- **The filesystem metricset ignores `nfs`, `cifs` and `zfs` by default.** With no
  `filesystem.ignore_types` set, Metricbeat builds the ignore list from every type
  marked `nodev` in `/proc/filesystems`, and those three are all nodev — so a host
  reports its root filesystem and nothing else, with no error and a green agent.
  Verified against 9.5.1: seven mounts on the host, two collected. `FS_IGNORE_TYPES`
  in `setup-policies.py` names the pseudo filesystems explicitly instead. A disk with
  no mounted filesystem (LVM-thin, a zvol, an unformatted spare) is out of reach of
  this metricset regardless.
- **`linux.service` is a *metrics* data stream.** systemd unit panels belong on the
  `es-metrics` datasource; on `es-logs` they return nothing, and a count panel with
  `min_doc_count: 0` then draws a confident flat zero rather than looking broken.

## Conventions

- Python is **standard library only** — the target VMs have no guaranteed pip packages.
- Scripts run as root or under `sudo`. Use `if [[ $EUID -eq 0 ]]; then SUDO=""; else SUDO="sudo"; fi`
  rather than calling `sudo` unconditionally; minimal images have no `sudo` binary.
- Keep every component on `STACK_VERSION` from `stack/.env`.
- Dataset names take no hyphens: `monitoring.backend`, never `monitoring-backend`.
- Log parsing hangs off the `logs@custom` dispatcher — add a `logs-<dataset>@custom`
  pipeline rather than configuring a pipeline on the integration.
- Field names come from the integration package, not from memory. Fetch the real one —
  `curl https://epr.elastic.co/epr/<pkg>/<pkg>-<ver>.zip`, read
  `data_stream/*/fields/*.yml` — before querying a field. ECS fields a package no
  longer declares are set by its ingest pipeline instead, so grep that too.
- Grafana dashboards are generated, not hand-maintained: `stack/gen-dashboards.py`
  builds them from what Fleet reports. Change the generator, not the JSON.

## Never commit

`stack/.env`, `stack/certs/`, `certs.bak-*` — the CA private key and every password.

## Further reference

Read these when the task touches them; they are too long to load every session.

- `README.md` — deployment, VM sizing, what each policy collects, troubleshooting
- `docs/shipping-logs.md` — the three routes in, dataset naming, adding a parser
- `docs/network-devices.md` — QNAP and UniFi
- `docs/monitoringapp.md`, `docs/calendarsync.md` — the two local projects
