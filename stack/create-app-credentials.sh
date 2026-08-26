#!/usr/bin/env bash
# Mint an Elasticsearch API key that can write one application's logs and
# nothing else.
#
#   ./create-app-credentials.sh monitoring.backend
#
# Prints the encoded key. That value goes verbatim into the application's
# config — the elastic ecs-logging appenders send it as "Authorization: ApiKey
# <value>". It is shown once and cannot be retrieved again; re-run to issue a
# replacement, then revoke the old one:
#
#   ./create-app-credentials.sh --revoke monitoring.backend
set -euo pipefail
cd "$(dirname "$0")"

[[ -f .env ]] || { echo "!! No .env found."; exit 1; }
set -a; . ./.env; set +a

REVOKE=false
if [[ ${1:-} == --revoke ]]; then REVOKE=true; shift; fi
DATASET=${1:-}
[[ -n $DATASET ]] || { sed -n '2,14p' "$0"; exit 1; }

# A dataset may not contain '-' (that separates the three parts of a data
# stream name), and must be lowercase.
if [[ ! $DATASET =~ ^[a-z0-9_]+(\.[a-z0-9_]+)*$ ]]; then
  echo "!! '$DATASET' is not a valid dataset name."
  echo "   Lowercase letters, digits, underscores and dots only — no hyphens."
  echo "   e.g. monitoring.backend, calendarsync, payments_api"
  exit 1
fi

CA=certs/ca/ca.crt
ES="https://localhost:${ES_PORT}"
esc() { curl -fsS --cacert "$CA" -u "elastic:${ELASTIC_PASSWORD}" "$@"; }
NAME="app-${DATASET//./-}"

if $REVOKE; then
  esc -XDELETE "${ES}/_security/api_key" -H 'Content-Type: application/json' \
    -d "{\"name\":\"${NAME}\"}" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print("   invalidated:", len(d.get("invalidated_api_keys",[])), "key(s)")'
  exit 0
fi

# auto_configure lets the data stream and its mappings be created on first
# write; create_doc is the only write op a data stream accepts.
KEY=$(esc -XPOST "${ES}/_security/api_key" -H 'Content-Type: application/json' -d "{
  \"name\": \"${NAME}\",
  \"role_descriptors\": {
    \"${NAME}\": {
      \"cluster\": [],
      \"indices\": [{
        \"names\": [\"logs-${DATASET}-*\"],
        \"privileges\": [\"auto_configure\", \"create_doc\"]
      }]
    }
  },
  \"metadata\": { \"managed_by\": \"homelab-logging\", \"dataset\": \"${DATASET}\" }
}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["encoded"])')

cat <<EOF

  Dataset      logs-${DATASET}-default
  Key name     ${NAME}
  Encoded key  ${KEY}

  Shown once. The key may only create documents in logs-${DATASET}-*; it
  cannot read anything, touch another dataset, or see the cluster.

  Java / logback (ecs-logging-java):
      <apiKey>${KEY}</apiKey>

  Go / any HTTP client:
      Authorization: ApiKey ${KEY}

EOF
