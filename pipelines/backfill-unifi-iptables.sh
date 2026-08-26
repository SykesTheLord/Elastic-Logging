#!/usr/bin/env bash
# Re-parse UniFi firewall documents that the iptables package half-parsed.
#
# The iptables package was written against the 9.x BSD kernel framing. Network
# 10.x sends RFC 5424 — "<13>1 <ts> <host> <rule> - - [<rule>] DESCR=..." — which
# still matches the package's generic fallback pattern. So the document indexes
# without any error at all, while silently losing:
#
#   @timestamp                     the header timestamp is never parsed
#   observer.name                  no device attribution, on any of them
#   iptables.ubiquiti.rule_number  reads as "RET" on a 10.x label
#   rule.id                        copied from that, so also "RET"
#   event.action                   absent, or the numeric rule id
#
# logs-iptables.log@custom recovers all of it from event.original, which the
# iptables package always keeps. This replays that pipeline over documents
# already indexed. New data is fixed the moment install.sh has run.
#
#   ./backfill-unifi-iptables.sh --check   # report only
#   ./backfill-unifi-iptables.sh           # rewrite in place
#
# Safe to re-run: every processor in that pipeline is guarded on the field being
# unset, or recomputes the same value from the same input.
set -euo pipefail
cd "$(dirname "$0")"

CHECK=false
case "${1:-}" in
  --check|-n|--dry-run) CHECK=true ;;
  "")                   ;;
  *) echo "usage: $0 [--check]" >&2; exit 2 ;;
esac

ENVF=../stack/.env
[[ -f $ENVF ]] || { echo "!! ../stack/.env not found."; exit 1; }
set -a
# shellcheck disable=SC1090
. "$ENVF"
set +a

CA=../stack/certs/ca/ca.crt
ES="https://localhost:${ES_PORT:-9200}"
IDX="logs-iptables.log-*"

es() { curl -fsS --cacert "$CA" -u "elastic:${ELASTIC_PASSWORD}" "$@"; }
jget() { python3 -c 'import json,sys;d=json.load(sys.stdin)
for k in sys.argv[1].split("."):
    d = d.get(k) if isinstance(d, dict) else None
print("" if d is None else d)' "$1"; }

# A document the package framed correctly always has observer.name; one it fell
# through on never does. That is the whole test.
#
# It is tempting to also require event.original, since that is what the pipeline
# re-reads — but do not. Fleet maps event.original as "type": "keyword",
# "index": false, so an exists query on it matches almost nothing and this script
# would silently find one document out of every four it should. The field is
# still in _source, which is all an ingest pipeline needs.
#
# CEF events that were delivered to this port are a different problem with a
# different script, so they are excluded rather than churned through a pipeline
# that cannot do anything for them.
# The second clause catches 9.x lines the package parsed correctly apart from the
# disposition: it maps d->drop and a->accept but has no entry for r, so a reject
# stays the bare letter until logs-iptables.log@custom expands it.
QUERY='{"bool":{
  "must_not": [{"match_phrase":{"message":"CEF:0|Ubiquiti"}}],
  "minimum_should_match": 1,
  "should": [
    {"bool":{"must_not":[{"exists":{"field":"observer.name"}}]}},
    {"term":{"event.action":"r"}}
  ]
}}'

echo
echo "Half-parsed UniFi firewall documents in ${IDX}"

if ! es -o /dev/null "${ES}/_ingest/pipeline/logs-iptables.log%40custom"; then
  echo "!! the logs-iptables.log@custom pipeline is not installed. Run ./install.sh first."
  exit 1
fi

TOTAL=$(es "${ES}/${IDX}/_count" -H 'Content-Type: application/json' \
        -d "{\"query\":${QUERY}}" | jget count)
echo "   ${TOTAL:-0} document(s) to re-parse"

if [[ ${TOTAL:-0} -eq 0 ]]; then
  echo
  echo "Nothing to do."
  exit 0
fi

if $CHECK; then
  echo
  echo "   preview of what the pipeline recovers from the first one:"
  es "${ES}/${IDX}/_search?size=1" -H 'Content-Type: application/json' \
     -d "{\"query\":${QUERY},\"_source\":[\"event.original\"]}" \
   | python3 -c '
import json, sys, urllib.request, base64, os, ssl
hits = json.load(sys.stdin)["hits"]["hits"]
if not hits:
    raise SystemExit
src = hits[0]["_source"]
print("      before: no observer.name, no rule.id")
print("      line  : %s" % (src.get("event", {}).get("original", "")[:96] + " ..."))'
  echo
  echo "--check given: nothing changed. Re-run without it to rewrite."
  exit 0
fi

echo "   replaying logs-iptables.log@custom over them ..."
RESULT=$(es -XPOST "${ES}/${IDX}/_update_by_query?pipeline=logs-iptables.log%40custom&refresh=true&conflicts=proceed&wait_for_completion=true" \
  -H 'Content-Type: application/json' -d "{\"query\":${QUERY}}")
printf '%s' "$RESULT" | python3 -c '
import json, sys
r = json.load(sys.stdin)
print("      %d updated, %d noop, %d conflict(s), %d failure(s)" % (
    r.get("updated", 0), r.get("noops", 0),
    r.get("version_conflicts", 0), len(r.get("failures", []))))
for f in r.get("failures", [])[:5]:
    print("      !! %s" % str(f)[:300])'

echo
echo "Done. If any backing index is read-only — ILM will do that on rollover —"
echo "_update_by_query reports it as a failure above and leaves that index alone;"
echo "those documents keep whatever the package originally extracted."
