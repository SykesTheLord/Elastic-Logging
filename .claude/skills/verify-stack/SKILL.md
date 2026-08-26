---
name: verify-stack
description: Exercise a change against real throwaway containers before calling it done. Use whenever a change touches stack/, fleet/, pipelines/, or an agent input — syntax checks pass on almost every bug this repo has had.
---

Verify a change by running it, not by reading it. Pick the cheapest harness that can
actually disprove the change, run it, then tear everything down.

All of these bind to high ports so they cannot collide with a real deployment. Never
point a test at the live stack.

## Which harness

| Change | Harness |
|---|---|
| Ingest pipeline, mapping, ILM | Elasticsearch alone |
| Fleet policy, integration config | Elasticsearch + Kibana |
| Agent input, parser, multiline, ndjson | Filebeat alone |
| Shell script, bootstrap, certs | `ubuntu:24.04` acting as the VM |

## Elasticsearch alone

```bash
docker run -d --name vtest-es -p 19200:9200 \
  -e discovery.type=single-node -e xpack.security.enabled=false \
  -e "ES_JAVA_OPTS=-Xms1g -Xmx1g" \
  docker.elastic.co/elasticsearch/elasticsearch:9.5.1
for i in $(seq 1 60); do curl -fsS localhost:19200/_cluster/health >/dev/null 2>&1 && break; sleep 5; done
```

Install a pipeline, then prove it with `_simulate` against a **real** log line, and
assert on the output fields rather than eyeballing them. For a routing change, index
into the data stream and read the document back — `_simulate` will not exercise
`logs@default-pipeline` or the `logs@custom` dispatcher.

## Elasticsearch + Kibana (Fleet)

Add security, set the `kibana_system` password, and give Kibana a config listing the
packages under test in `xpack.fleet.packages`. Wait for `/api/fleet/epm/packages/installed`
to report them before creating policies.

The decisive check is always the **compiled** policy, not the API response:

```
GET /api/fleet/agent_policies/<id>/full
```

That is what the agent actually receives. Confirm the streams, the listen addresses,
and that nothing unwanted was enabled — Fleet silently enables every input you do not
explicitly disable.

## Filebeat (agent inputs)

The fastest way to prove a parser change, and it avoids a whole Fleet enrolment:

```bash
docker run --rm -v "$PWD/t:/w" --user 0:0 \
  docker.elastic.co/beats/filebeat:9.5.1 \
  filebeat -e --strict.perms=false -c /w/fb.yml --path.data /w/data
```

`--strict.perms=false` is required or Filebeat refuses a config file it does not own.
Use `output.console` and assert on the emitted JSON. Remember the Custom Logs package
produces `type: log` with `allow_deprecated_use: true`, so test that input type — not
`filestream`.

## ubuntu:24.04 as the VM

For anything that runs on the stack VM. Run as root, share the host's network so
`localhost` means the same thing, mount the Docker socket for sibling containers, and
bind-mount the repo **at a path identical to the host's** so the inner
`docker compose` bind mounts resolve against the host daemon:

```bash
docker run -d --name vtest-vm --network host \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$REPO:$REPO" -w "$REPO" ubuntu:24.04 sleep infinity
```

Then install `curl python3 openssl iproute2` plus `docker-ce-cli docker-compose-plugin`
from Docker's apt repo — Ubuntu's `docker.io` has no Compose v2.

Put the repo somewhere with real disk. The scratchpad is a 16 GB tmpfs and
`bootstrap.sh` requires 20 GB free.

## Finish

Report what was proven and what was not. If a check was skipped, say so — do not imply
coverage that was not exercised.

Then tear everything down:

```bash
docker rm -f vtest-es vtest-kb vtest-vm 2>/dev/null
docker volume ls -q --filter name=elastic-logging | xargs -r docker volume rm
```

Confirm with `docker ps` that nothing of yours is left running. Leave containers you did
not create alone.
