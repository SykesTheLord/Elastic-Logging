---
name: add-log-source
description: Onboard a new application, host, or network device into the logging stack — pick the route in, name the dataset, add a parser, wire the Fleet policy, and prove it parses.
---

Onboard `$ARGUMENTS` as a new log source.

Read `docs/shipping-logs.md` first — it holds the detail this skill assumes.

## 1. Pick the route

Cheapest route that keeps the fields worth keeping:

- **Elastic Agent** — the source writes files, journald, or container stdout. Default to
  this: it buffers when the stack is down and needs nothing from the application.
- **TCP to Logstash** (`:5000`, `:5001` TLS) — the app can write newline-delimited JSON.
  Already-ECS payloads pass through untouched.
- **Direct to Elasticsearch** — the app already has an ES appender. Needs a scoped API
  key from `stack/create-app-credentials.sh` and the CA in its trust store.
- **A syslog/CEF/NetFlow receiver** — for appliances. Check the Elastic Package Registry
  for a first-party integration before writing a parser; `qnap_nas`, `cef`, `iptables`
  and `netflow` all already exist.

## 2. Name the dataset

Lowercase, digits, underscores and dots only — **no hyphens**, that separates the three
parts of a data stream name. Group related components with a dot (`monitoring.backend`,
`monitoring.agent`) so one wildcard covers them on a dashboard.

## 3. Add a parser, only if the format is unstructured

Structured input needs nothing. Otherwise add `pipelines/logs-<dataset>@custom.json` and
wire it into the loop in `pipelines/install.sh`. The `logs@custom` dispatcher routes to
it automatically on `event.dataset` — never set a pipeline on the integration.

Two rules that keep a bad pattern from losing data: put `ignore_failure: true` on the
grok so an unmatched line keeps its original `message`, and use a `_tmp` scratch object
you `remove` at the end so half-parsed fields never reach the index.

## 4. Wire the collection

For a Custom Logs input, use flat `json.*` / `multiline.*` keys in the integration's
custom configuration. `parsers:` is a filestream-only option and the Custom Logs package
compiles to the deprecated `log` input, which ignores it silently.

Add `- add_locale: ~` to the processors if the source writes timestamps with no UTC
offset, or they land in the wrong hour.

Remember `fleet/setup-policies.py` only *creates* policies — for a live stack, add the
integration in the Fleet UI, and update the script so a rebuild matches.

## 5. Prove it

Use `/verify-stack`. Then check three things, in order:

1. the data stream exists and the count is non-zero
2. a document has the fields you expect — `@timestamp` from the payload, not ingest time
3. the dashboards can see it:
   `log.level:(error OR fatal)` matches, since `log.level` is a keyword and case-sensitive

The third is the one people skip, and the one that leaves a source indexed but invisible.
