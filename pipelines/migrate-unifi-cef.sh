#!/usr/bin/env bash
# Rescue UniFi CEF events that were delivered to the iptables listener.
#
# UniFi splits its logging in two: Control Plane -> Integrations sends CEF,
# CyberSecure -> Traffic Logging sends iptables-format firewall lines. Point the
# CEF half at the iptables port (9001) and every event fails the iptables grok:
#
#   Processor 'grok' with tag 'grok_message_44b8bbb5' ... failed with message
#   'Provided Grok expressions do not match field value: [... CEF:0|Ubiquiti|...]'
#
# Nothing is lost — the package's on_failure still indexes the document with the
# raw line in event.original — but nothing is parsed either. This reindexes those
# documents into logs-cef.log-*, parsed, using the unifi-cef-rescue pipeline.
#
#   ./migrate-unifi-cef.sh --check   # report what would move, change nothing
#   ./migrate-unifi-cef.sh           # rescue; originals are left in place
#   ./migrate-unifi-cef.sh --purge   # rescue, then delete the originals
#
# Fix the port in the UniFi UI first, or this runs again next week against the
# same growing pile. Settings -> Control Plane -> Integrations -> Activity
# Logging (Syslog) -> SIEM Server belongs on 9003, not 9001.
set -euo pipefail
cd "$(dirname "$0")"

MODE=rescue
case "${1:-}" in
  --check|-n|--dry-run) MODE=check ;;
  --purge)              MODE=purge ;;
  "")                   ;;
  *) echo "usage: $0 [--check|--purge]" >&2; exit 2 ;;
esac

ENVF=../stack/.env
[[ -f $ENVF ]] || { echo "!! ../stack/.env not found."; exit 1; }
set -a
# .env is generated at deploy time, so there is nothing for shellcheck to follow.
# shellcheck disable=SC1090
. "$ENVF"
set +a

CA=../stack/certs/ca/ca.crt
ES="https://localhost:${ES_PORT:-9200}"
SRC="logs-iptables.log-*"
DST="logs-cef.log-${LOG_NAMESPACE:-default}"

es() { curl -fsS --cacert "$CA" -u "elastic:${ELASTIC_PASSWORD}" "$@"; }
jget() { python3 -c 'import json,sys;d=json.load(sys.stdin)
for k in sys.argv[1].split("."):
    d = d.get(k) if isinstance(d, dict) else None
print("" if d is None else d)' "$1"; }

# Matched on message, not event.original: message is match_only_text and always
# searchable, while event.original is mapped by a dynamic template that does not
# reliably index it.
QUERY='{"bool":{"filter":[
  {"term":{"event.kind":"pipeline_error"}},
  {"match_phrase":{"message":"CEF:0|Ubiquiti"}}
]}}'

echo
echo "Stranded UniFi CEF events in ${SRC}"

if ! es -o /dev/null "${ES}/_ingest/pipeline/unifi-cef-rescue"; then
  echo "!! the unifi-cef-rescue pipeline is not installed. Run ./install.sh first."
  exit 1
fi

TOTAL=$(es "${ES}/${SRC}/_count" -H 'Content-Type: application/json' \
        -d "{\"query\":${QUERY}}" | jget count)
echo "   ${TOTAL:-0} document(s) to rescue"

if [[ ${TOTAL:-0} -eq 0 ]]; then
  echo
  echo "Nothing to do."
  exit 0
fi

# What is actually in there, so this is not a blind bulk move. Aggregating by
# device is not possible yet — that is precisely what has not been parsed — so
# this reports the span and a couple of the raw lines instead.
echo
es "${ES}/${SRC}/_search?size=2" -H 'Content-Type: application/json' -d "{
  \"query\": ${QUERY},
  \"sort\": [{\"@timestamp\": \"asc\"}],
  \"_source\": [\"message\", \"@timestamp\"],
  \"aggs\": {\"first\": {\"min\": {\"field\": \"@timestamp\"}},
            \"last\":  {\"max\": {\"field\": \"@timestamp\"}}}
}" | python3 -c '
import json, sys
d = json.load(sys.stdin)
a = d["aggregations"]
print("   spanning %s .. %s" % (a["first"].get("value_as_string", "?"),
                                a["last"].get("value_as_string", "?")))
print("   sample lines:")
for h in d["hits"]["hits"]:
    print("      %s" % (h["_source"].get("message", "")[:96] + " ..."))'

if [[ $MODE == check ]]; then
  echo
  echo "--check given: nothing changed. Re-run without it to rescue."
  exit 0
fi

echo
echo "   reindexing into ${DST} through unifi-cef-rescue ..."
# conflicts=proceed matters more than it looks. op_type=create means a document
# rescued by an earlier run collides on its _id, and without this the whole
# reindex returns 409 — curl -f then aborts the script on a bare exit code,
# after having already moved some of the documents.
RESULT=$(es -XPOST "${ES}/_reindex?refresh=true&wait_for_completion=true" \
  -H 'Content-Type: application/json' -d "{
    \"conflicts\": \"proceed\",
    \"source\": {\"index\": \"${SRC}\", \"query\": ${QUERY}},
    \"dest\": {\"index\": \"${DST}\", \"op_type\": \"create\", \"pipeline\": \"unifi-cef-rescue\"}
  }")
CREATED=$(printf '%s' "$RESULT" | jget created)
ALREADY=$(printf '%s' "$RESULT" | jget version_conflicts)
FAILURES=$(printf '%s' "$RESULT" | python3 -c 'import json,sys;print(len(json.load(sys.stdin).get("failures",[])))')
echo "      ${CREATED} created, ${ALREADY:-0} already rescued, ${FAILURES} failure(s)"

if [[ ${FAILURES:-0} -ne 0 ]]; then
  printf '%s' "$RESULT" | python3 -c '
import json, sys
for f in json.load(sys.stdin).get("failures", [])[:5]:
    print("      !! %s" % str(f)[:300])'
  echo "   originals left in place. Fix the cause and re-run; op_type=create means"
  echo "   the documents already moved will not be duplicated."
  exit 1
fi

if [[ $MODE == purge ]]; then
  echo "   deleting the originals from ${SRC} ..."
  DELETED=$(es -XPOST "${ES}/${SRC}/_delete_by_query?refresh=true&conflicts=proceed" \
    -H 'Content-Type: application/json' -d "{\"query\":${QUERY}}" | jget deleted)
  echo "      ${DELETED} deleted"
else
  echo
  echo "   Originals kept. Re-run with --purge to remove them once you have"
  echo "   confirmed the rescued copies look right:"
  echo "      GET ${DST}/_search  {\"query\":{\"term\":{\"tags\":\"unifi-cef-rescued\"}}}"
fi

echo
echo "Done. Rescued documents carry the tag unifi-cef-rescued and are otherwise"
echo "identical to events that arrived on 9003 in the first place."
