# Shipping CalendarSync logs to the stack

**Status: closer than MonitoringApp — one setting is wrong, and TLS needs a
decision. No code changes required.**

`~/Projects/CalendarSync` already has an `elastic` logging destination that
sends ECS JSON, one document per line, over a TCP socket
(`LogstashTcpSocketAppender` via `ElasticTcpAppender`). That is exactly the
protocol the stack's Logstash intake speaks. Its own comment — *"the app never
talks to Elasticsearch itself"* — describes the right architecture, and it
means no certificates or API keys are needed on the plain-text path.

---

## 1. The port is wrong  *(blocker)*

`application.yml` defaults to **5044**. The stack does not listen there:

| Port | What it is |
|---|---|
| 5000 | Logstash TCP, plain — LAN only |
| 5001 | Logstash TCP, TLS |
| 5044 | *nothing* — the Beats/Lumberjack convention, not a TCP JSON input |

In `.env` next to `docker-compose.yml`:

```bash
CALENDARSYNC_LOG_DESTINATION=elastic
CALENDARSYNC_LOG_ELASTIC_HOST=192.168.1.50
CALENDARSYNC_LOG_ELASTIC_PORT=5000
CALENDARSYNC_LOG_ELASTIC_TLS=false
```

That is the whole change for the plain-text path. Restart and logs appear in
`logs-app.calendarsync-default`.

---

## 2. TLS needs the CA inside the container

`ElasticTcpAppender.setTls(true)` builds a bare logback `SSLConfiguration`,
which uses the **JVM default truststore**. The stack's Logstash certificate is
signed by the private CA from `stack/setup-certs.sh`, so port 5001 fails the
handshake until that CA is trusted. The app runs in Docker, so the CA has to
get inside the container too.

Pick one:

**Plain TCP on the LAN (recommended).** Port 5000, `TLS=false`. The traffic
crosses your own switch between two machines you own. This is the default this
guide assumes.

**TLS.** Mount a truststore and point the JVM at it — no image rebuild:

```bash
JDK=$(readlink -f "$(which java)" | sed 's:/bin/java::')
cp "$JDK/lib/security/cacerts" ./elastic-truststore.p12
keytool -importcert -noprompt -alias homelab-elastic-ca \
  -file ~/elastic-logging/stack/certs/ca/ca.crt \
  -keystore ./elastic-truststore.p12 -storepass changeit
```

```yaml
# docker-compose.yml, calendarsync service
    environment:
      CALENDARSYNC_LOG_ELASTIC_PORT: 5001
      CALENDARSYNC_LOG_ELASTIC_TLS: "true"
      JAVA_TOOL_OPTIONS: >-
        -Djavax.net.ssl.trustStore=/certs/elastic-truststore.p12
        -Djavax.net.ssl.trustStorePassword=changeit
    volumes:
      - ./elastic-truststore.p12:/certs/elastic-truststore.p12:ro
```

Copy the JDK's `cacerts` rather than making a store with only our CA:
`javax.net.ssl.trustStore` replaces the default outright, and CalendarSync
makes outbound HTTPS calls to Google, Microsoft and CalDAV servers that would
all start failing.

> `ElasticTcpAppender` throws away logback's ability to configure a per-appender
> `<trustStore>`, because `setTls(boolean)` constructs an empty
> `SSLConfiguration`. Adding a `trustStore` passthrough to that class would let
> TLS be configured without a JVM-wide setting. Worth doing if you ever ship
> this over a network you do not control.

---

## 3. What this destination costs you

`destination=elastic` attaches **one** appender to the root logger, so stdout
goes quiet — `docker logs calendarsync` shows only the Spring banner and
logback status lines. If Logstash is unreachable, the appender drops events
after its ring buffer fills and reports connect failures through logback's
status manager, which Boot prints to stderr. So you are not blind, but you are
reading status lines rather than application logs.

If you would rather keep both, add a second `<appender-ref/>` to
`destination-elastic.xml` — the file's own comment says as much.

---

## What already works

Verified by running CalendarSync's actual encoder and pushing the bytes it
produces through the real pipeline:

- **Field mapping.** Boot's `StructuredLogEncoder` with `format=ecs` emits
  properly nested ECS, and the intake recognises it and passes it through to
  the document root untouched — `log.level`, `log.logger`, `error.type`,
  `error.stack_trace` and `process.thread.name` all land where they should.
- **Timestamps.** The `@timestamp` in the payload is used, not the arrival time.
- **The dataset.** `spring.application.name: calendarsync` becomes
  `service.name`, which the intake turns into `logs-app.calendarsync-default`.
- **MDC.** Anything you `MDC.put` arrives as a top-level field, so
  `MDC.put("connectionId", …)` is queryable as `connectionId`.

Sample of what actually lands, from a real encoded event:

```json
{
  "@timestamp": "2026-08-21T12:47:53.793Z",
  "event":   { "dataset": "app.calendarsync", "module": "app" },
  "log":     { "level": "error", "logger": "com.sykessec.calendarsync.sync.SyncJob" },
  "service": { "name": "calendarsync", "version": "0.1.0", "environment": "prod" },
  "message": "Sync failed for connection google-1",
  "connectionId": "google-1",
  "error":   { "type": "java.lang.IllegalStateException", "message": "token expired",
               "stack_trace": "…" }
}
```

---

## Verify

```bash
# From the CalendarSync host — does the intake even answer?
nc -vz 192.168.1.50 5000

# Then, after a restart:
cd ~/elastic-logging/stack && source .env
curl -s --cacert certs/ca/ca.crt -u "elastic:$ELASTIC_PASSWORD" \
  "https://$STACK_IP:9200/logs-app.calendarsync-default/_search?size=3&pretty"
```

| Symptom | Cause |
|---|---|
| Nothing indexed, `docker logs` shows connection refused | Port still 5044, or the stack VM's firewall |
| `PKIX path building failed` in the status lines | TLS without the truststore — step 2 |
| Documents land in `logs-app.generic-default` | `service.name` missing; check `spring.application.name` survived a config change |
| Documents appear but stdout is silent | Expected — see section 3 |

---

## The simpler alternative

CalendarSync runs in Docker on a Proxmox guest that already has an Elastic
Agent with the Docker integration, which collects every container's stdout. Leaving
`destination=console` gets the logs in with **no configuration at all** — they
land in `logs-docker.container_logs-default`, tagged with the container name.

You lose the ECS structure, the `app.calendarsync` dataset, and the parsed
`log.level`. Given the app already produces good ECS, spending one env var to
keep all that is the better trade — but if you want logs today and nothing
else, the Agent is already collecting them.
