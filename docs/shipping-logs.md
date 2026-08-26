# Shipping logs from anything

How to get logs from a new application, host, or cluster into this stack.
Worked examples: [MonitoringApp](monitoringapp.md) (direct to Elasticsearch)
and [CalendarSync](calendarsync.md) (TCP to Logstash).

---

## Pick a route

Three ways in. Pick by what the source can already do, not by what is most
elegant — the cheapest route that preserves the fields you care about wins.

| | Elastic Agent | TCP → Logstash | Direct → Elasticsearch |
|---|---|---|---|
| **Use when** | the source writes files, journald, or container stdout | the app can open a socket and write JSON | the app already has an Elasticsearch appender |
| **App changes** | none | config only | config only |
| **Needs the CA** | yes, at enrolment | only for TLS (port 5001) | yes, always |
| **Needs credentials** | one enrolment token per policy | none | one API key per app |
| **Central control** | full — change collection in Fleet, no redeploy | parsing is central, the address is not | none; the app decides everything |
| **If the stack is down** | agent buffers on disk | events dropped after the buffer fills | events dropped after the queue fills |

**Default to Elastic Agent.** It buffers, it is managed centrally, and it needs
nothing from the application. Reach for the other two when the app already
produces structured logs and you want to keep that structure without a file on
disk in between.

---

## Route 1 — Elastic Agent

For a whole machine or cluster of machines. Nothing changes in the application.

1. Copy `stack/certs/ca/ca.crt` to the host.
2. `./fleet/enrollment-tokens.sh` for the command and token.
3. `sudo ./vms/install-agent.sh --url https://<STACK_IP>:8220 --token <token> --ca ./ca.crt`

To collect a log file the existing policies do not cover, add a **Custom Logs**
integration in Kibana → Fleet → the policy:

- **Paths** — a glob, e.g. `/opt/myapp/logs/*.log`. Name the *current* file rather than globbing the directory when the app rotates in place: the *arr apps roll at 1 MB into `<app>.0.txt` … `<app>.49.txt`, so `*.txt` re-ingests the whole retained history on the first run and picks up `<app>.debug.txt` the moment someone raises the log level. Rotation is a rename and the input follows the inode, so tailing the current file alone loses nothing. For a containerised app this is the **host** side of the bind mount, never the in-container path — the agent runs on the host
- **Dataset name** — see the naming rules below
- **Custom configurations** — the multiline parser, if entries can span lines:

  ```yaml
  parsers:
    - multiline:
        type: pattern
        pattern: '^\d{4}-\d{2}-\d{2}'   # what a NEW entry starts with
        negate: true
        match: after
        max_lines: 200
  ```

- **Processors** — `- add_locale: ~` if the timestamps have no UTC offset. The
  ingest pipeline needs the host's timezone to resolve them, and without this
  it assumes UTC and your logs land in the wrong hour.

Do **not** set an ingest pipeline here. Routing is automatic — see *Parsing* below.

> On 9.x the agent must be installed with `--install-servers`. The default
> "basic" flavor omits the journald dependencies and the Journald integration
> then collects nothing, silently. Both installers in this repo already pass it.

---

## Route 2 — TCP to Logstash

For an application that can write newline-delimited JSON to a socket. No
credentials, and no certificates unless you want TLS.

```
<STACK_IP>:5000    plain, LAN only
<STACK_IP>:5001    TLS
```

```bash
echo '{"dataset":"payments","level":"error","message":"charge failed","order_id":991}' \
  | nc 192.168.1.50 5000
```

The intake handles three shapes:

**Already ECS** — anything from Spring Boot's `StructuredLogEncoder`, the
`ecs-logging-*` libraries, or your own ECS output. Recognised by an
`ecs.version` field, in either the nested (`{"ecs":{"version":…}}`) or
flat-dotted (`{"ecs.version":…}`) spelling. The document is passed through to
the root **untouched** — it is already correct, and burying it under `app.*`
would make it inconsistent with everything Elastic Agent writes. The dataset
comes from `event.dataset`, else `service.name`.

**Plain JSON** — the usual field names are promoted and everything else is kept
under `app.*`, so a stray field can never collide with an ECS mapping:

| Your field | Becomes |
|---|---|
| `dataset` · `service` · `app` | the data stream |
| `message` · `msg` · `log` | `message` |
| `level` · `severity` · `levelname` · `loglevel` | `log.level` |
| `timestamp` · `time` · `ts` | `@timestamp` (ISO8601, epoch s or ms) |
| `host` · `hostname` | `host.name` |
| `logger` · `exception` · `trace_id` | `log.logger` · `error.stack_trace` · `trace.id` |

**Plain text** — kept verbatim in `message`, with a severity word anywhere in
the line picked up as `log.level`. Lands in `logs-app.generic-default`.

Everything through this route is prefixed `app.`, so
`{"dataset":"payments"}` lands in `logs-app.payments-default`. That prefix is
how you tell at a glance which documents came from an application socket rather
than from an agent.

TLS needs the CA on the client. For a JVM, copy the JDK's `cacerts` and add our
CA to the copy — never build a store containing only our CA, because
`javax.net.ssl.trustStore` replaces the default outright and the app stops
trusting every public CA.

---

## Route 3 — Direct to Elasticsearch

For an app that already has an Elasticsearch appender. It POSTs to
`/<data-stream>/_bulk` and needs three things:

**The `create` action.** A data stream accepts no other op type:

```
{"create":{}}
{"@timestamp":"…","message":"…"}
```

**A scoped API key**, one per application:

```bash
cd ~/elastic-logging/stack
./create-app-credentials.sh myapp.component
```

The key can create documents in `logs-myapp.component-*` and nothing else — it
gets 403 on any other data stream and 403 on every read. Sent as
`Authorization: ApiKey <encoded>`. Revoke with `--revoke`.

**The CA.** Java uses the JVM truststore; Go, Python and curl use the OS store
(`cp ca.crt /usr/local/share/ca-certificates/ && update-ca-certificates`).
A JVM does not read the OS store — that catches people out every time.

You do not need to set `event.dataset` or `data_stream.*`; the stack fills both
in from the data stream name.

---

## Dataset naming

The data stream name is `logs-<dataset>-<namespace>`, so the dataset **may not
contain a hyphen** — that is the field separator. Lowercase letters, digits,
underscores and dots only, and at most 100 characters.

```
monitoring.backend     good — dots group related components
calendarsync           good
payments_api           good
monitoring-backend     WRONG — hyphen
Payments-API           WRONG — uppercase and a hyphen
```

Route 2 sanitises rather than rejects (`Payments-API` becomes
`app.payments_api`). Routes 1 and 3 will fail the write, so get it right there.

Group related components with a dot prefix: `monitoring.backend` and
`monitoring.agent` are then one `logs-monitoring.*` wildcard on a dashboard.

---

## Parsing

Structured logs need nothing. For an unstructured format, add a pipeline named
after the dataset and it is picked up automatically:

```
logs@default-pipeline           built in, Elastic-managed
  └─ logs@custom                this stack's dispatcher
       ├─ dot_expander          flat "log.level" → nested log.level
       ├─ backfill              event.dataset + data_stream.* from the index name
       ├─ logs-<dataset>@custom your parser, if one exists
       └─ lowercase log.level   one casing across every producer
```

That is the path for Custom Logs datasets and anything writing straight to
Elasticsearch. An **integration** data stream does not use
`logs@default-pipeline` at all — its `default_pipeline` is the package's own,
and Fleet appends its own chain to the end of it:

```
logs-<dataset>-<pkgver>         the integration package's pipeline
  └─ ... the package's own processors ...
  └─ global@custom
  └─ logs@custom                ← this stack's dispatcher runs here too
  └─ logs-<pkg>.integration@custom
  └─ logs-<dataset>@custom
```

Two things follow. A `logs-<dataset>@custom` for an integration is invoked
**twice** — once by the dispatcher's `event.dataset` lookup, once by Fleet — so
write it idempotently. And because the `@custom` call sits at the *end* of the
package pipeline, a processor failure earlier in that pipeline triggers its
`on_failure` and stops execution before reaching your parser: the hook can
enrich a successful parse but can never rescue a failed one. `pipelines/`
carries a worked example of both — see `docs/network-devices.md`.

So adding a parser is one file:

```bash
cat > pipelines/logs-myapp@custom.json <<'JSON'
{
  "processors": [
    { "grok": { "field": "message",
        "patterns": ["^%{TIMESTAMP_ISO8601:_tmp.ts} %{WORD:log.level} %{GREEDYDATA:_tmp.msg}$"],
        "ignore_failure": true } },
    { "date": { "field": "_tmp.ts", "target_field": "@timestamp",
        "timezone": "{{{event.timezone}}}", "if": "ctx._tmp?.ts != null", "ignore_failure": true } },
    { "set": { "field": "message", "copy_from": "_tmp.msg", "if": "ctx._tmp?.msg != null" } },
    { "remove": { "field": "_tmp", "ignore_missing": true } }
  ]
}
JSON
```

Then add it to the loop in `pipelines/install.sh` and re-run that script.

Three rules worth keeping. Put `ignore_failure: true` on the grok so a line that
matches nothing keeps its original `message` instead of being dropped. Use a
`_tmp` scratch object you remove at the end so half-parsed fields never reach the
index. And if the source can produce multiline entries, end the pattern with a
body that spans newlines — `MSGBODY: [\s\S]*` — never `%{GREEDYDATA:...}$`,
which matches the first line only and silently discards every stack trace when
`message` is overwritten from it.

Test before deploying — `_simulate` takes documents and returns what the
pipeline would produce, without indexing anything:

```bash
curl -s --cacert stack/certs/ca/ca.crt -u "elastic:$ELASTIC_PASSWORD" \
  "https://$STACK_IP:9200/_ingest/pipeline/logs-myapp@custom/_simulate?pretty" \
  -H 'Content-Type: application/json' \
  -d '{"docs":[{"_source":{"message":"2026-08-21T10:00:00 ERROR something broke"}}]}'
```

These `@custom` pipelines are the documented user hook. Elastic never
overwrites them, so nothing here breaks on a stack upgrade.

---

## Retention

Nothing to do. `logs@custom` and `metrics@custom` component templates are
composed by every data stream template, integration and application alike, so
`LOG_RETENTION_DAYS` in `.env` already applies to your new dataset. Replicas
are forced to 0, which is correct on a single node.

For a dataset that deserves different treatment, give it its own ILM policy via
a `logs-<dataset>@custom` **component template** — note that is a different
thing from the `logs-<dataset>@custom` **ingest pipeline** above, despite the
identical name. Elastic reuses the convention across both namespaces.

---

## Verify

Whichever route, the same three checks:

```bash
cd ~/elastic-logging/stack && source .env
ES="https://$STACK_IP:9200"; CA=certs/ca/ca.crt

# 1. Did the data stream get created?
curl -s --cacert $CA -u "elastic:$ELASTIC_PASSWORD" "$ES/_cat/indices/.ds-logs-myapp*?v"

# 2. What do the documents actually look like?
curl -s --cacert $CA -u "elastic:$ELASTIC_PASSWORD" \
  "$ES/logs-myapp.*/_search?size=3&pretty" -H 'Content-Type: application/json' \
  -d '{"_source":["@timestamp","event.dataset","log.level","log.logger","message"],"sort":[{"@timestamp":"desc"}]}'

# 3. Does the dashboard's own error query match them?
curl -s --cacert $CA -u "elastic:$ELASTIC_PASSWORD" \
  "$ES/logs-myapp.*/_count?pretty" -H 'Content-Type: application/json' \
  -d '{"query":{"query_string":{"query":"log.level:(error OR fatal OR critical)"}}}'
```

Check 3 is the one people skip. A document can index perfectly and still be
invisible to every dashboard because `log.level` came through as `ERROR` and
keyword queries are case-sensitive. The pipeline normalises casing for you —
this confirms it actually happened.

| Symptom | Cause |
|---|---|
| No data stream at all | Nothing arrived. Check connectivity first: `nc -vz <STACK_IP> <port>` |
| `400 ... op_type create` | Route 3 using `index` instead of `create` |
| `403 Forbidden` | The API key's dataset does not match the data stream being written |
| `PKIX path building failed` | JVM truststore missing the CA — the OS store does not count |
| Indexed but `event.dataset` is wrong | An ECS library set it. Override it in the app; the stack only fills it in when absent |
| Timestamps hours out | Offset-less timestamps without `add_locale` |
| Indexed but dashboards are empty | Check 3 |
