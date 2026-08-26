#!/usr/bin/env bash
# Installs the ingest pipelines that parse the media-app log formats.
#
# Wiring: the built-in `logs@default-pipeline` calls `logs@custom` for every
# logs-*-* data stream. Our `logs@custom` is a one-line dispatcher that hands
# off to `logs-<event.dataset>@custom`. So adding a new app is just: create
# `logs-<name>@custom`, point a Custom Logs integration at it, done — no
# integration configuration and nothing to break on a stack upgrade.
set -euo pipefail
cd "$(dirname "$0")"

ENVF=../stack/.env
[[ -f $ENVF ]] || { echo "!! ../stack/.env not found."; exit 1; }
set -a
# .env is generated at deploy time, so there is nothing for shellcheck to follow.
# shellcheck disable=SC1090
. "$ENVF"
set +a

CA=../stack/certs/ca/ca.crt
ES="https://localhost:${ES_PORT:-9200}"
put() { curl -fsS --cacert "$CA" -u "elastic:${ELASTIC_PASSWORD}" \
        -XPUT "${ES}/_ingest/pipeline/$1" -H 'Content-Type: application/json' --data-binary "@$2" >/dev/null; }

# Shared parsers.
for f in arr-common serilog-common emby-common bazarr-common; do
  put "$f" "$f.json"; echo "   pipeline $f"
done

# Per-dataset shims. Each simply delegates to the shared parser, which keeps
# one place to fix a grok pattern for all six *arr apps.
shim() {
  local dataset=$1 parser=$2
  printf '{"description":"%s -> %s","processors":[{"pipeline":{"name":"%s"}}]}' \
    "$dataset" "$parser" "$parser" > /tmp/shim.json
  put "logs-${dataset}@custom" /tmp/shim.json
  echo "   pipeline logs-${dataset}@custom -> ${parser}"
}

for d in sonarr radarr lidarr readarr prowlarr; do shim "$d" arr-common; done
shim bazarr bazarr-common
for d in jellyfin kavita;                              do shim "$d" serilog-common; done
shim emby emby-common

# Network devices. These attach to integration data streams rather than to
# Custom Logs, so they are not reached through the logs@custom dispatcher's
# event.dataset lookup alone — Fleet appends its own call to
# logs-<dataset>@custom at the end of each package pipeline. Both mechanisms
# fire, so these pipelines are written to be idempotent.
for f in unifi-cef-common unifi-cef-rescue; do
  put "$f" "$f.json"; echo "   pipeline $f"
done
for f in 'logs-cef.log@custom' 'logs-iptables.log@custom'; do
  put "$f" "$f.json"; echo "   pipeline $f"
done

# The dispatcher goes in last so nothing is routed to a pipeline that does not
# exist yet.
put "logs@custom" "logs@custom.json"
echo "   pipeline logs@custom (dispatcher)"
rm -f /tmp/shim.json
