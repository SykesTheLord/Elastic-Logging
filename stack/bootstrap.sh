#!/usr/bin/env bash
# One-shot bring-up for the stack. Safe to re-run: every step is idempotent.
set -euo pipefail
cd "$(dirname "$0")"

[[ -f .env ]] || { echo "!! No .env found. Copy .env.example to .env and edit it first."; exit 1; }
set -a; . ./.env; set +a
: "${STACK_IP:?}" "${ELASTIC_PASSWORD:?}" "${KIBANA_SYSTEM_PASSWORD:?}"

CA=certs/ca/ca.crt
ES="https://localhost:${ES_PORT}"
KB="https://localhost:${KIBANA_PORT}"
if [[ $EUID -eq 0 ]]; then SUDO=""; else SUDO="sudo"; fi

esc() { curl -fsS --cacert "$CA" -u "elastic:${ELASTIC_PASSWORD}" "$@"; }
kbc() { curl -fsS --cacert "$CA" -u "elastic:${ELASTIC_PASSWORD}" -H 'kbn-xsrf: true' "$@"; }
step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

# ── Preflight ────────────────────────────────────────────────────────────────
step "Preflight"

# Tooling. `docker compose` is the v2 plugin; Ubuntu's own docker.io package
# does not ship it, and without this check the first failure is an opaque
# "docker: 'compose' is not a docker command" forty lines further down.
command -v docker >/dev/null || { echo "!! docker is not installed."; exit 1; }
docker compose version >/dev/null 2>&1 || {
  echo "!! The Docker Compose v2 plugin is missing."
  echo "   sudo apt-get install -y docker-compose-plugin"
  exit 1; }
docker info >/dev/null 2>&1 || {
  echo "!! Cannot talk to the Docker daemon. Is it running, and are you in the docker group?"
  exit 1; }

[[ -f "$CA" ]] || { echo "!! No certificates. Run ./setup-certs.sh first."; exit 1; }
[[ -n "${ES_CA_FINGERPRINT:-}" ]] || { echo "!! ES_CA_FINGERPRINT is empty. Re-run ./setup-certs.sh."; exit 1; }
[[ ${#KIBANA_ENCRYPTION_KEY} -ge 32 ]] || { echo "!! KIBANA_ENCRYPTION_KEY must be >= 32 characters."; exit 1; }
[[ "$ELASTIC_PASSWORD" == changeme* ]] && { echo "!! Passwords in .env are still the placeholders. Edit it first."; exit 1; }

# Elasticsearch will not start without this and the message it prints is opaque.
CUR_MMC=$(sysctl -n vm.max_map_count)
if (( CUR_MMC < 262144 )); then
  echo "   vm.max_map_count is ${CUR_MMC}, raising to 262144 (persisted)"
  echo 'vm.max_map_count=262144' | $SUDO tee /etc/sysctl.d/99-elasticsearch.conf >/dev/null
  $SUDO sysctl -q -w vm.max_map_count=262144
fi

# Heap vs the VM's actual RAM. Getting this wrong on a Proxmox guest is the
# classic way to end up with an Elasticsearch that OOM-kills under load.
HEAP_G=$(echo "${ES_HEAP}" | sed 's/[gG]$//')
RAM_G=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
if [[ "$HEAP_G" =~ ^[0-9]+$ ]] && (( RAM_G > 0 )); then
  if (( HEAP_G * 2 > RAM_G )); then
    echo "   !! ES_HEAP is ${ES_HEAP} but this VM has ${RAM_G} GB."
    echo "      Elasticsearch wants about half the RAM as heap and the rest as"
    echo "      filesystem cache. Lower ES_HEAP or give the VM more memory."
    exit 1
  fi
  echo "   memory: ${RAM_G} GB total, ${ES_HEAP} heap"
fi

# Disk. Index data plus the Logstash persisted queue plus images.
AVAIL_G=$(df -BG --output=avail . | tail -1 | tr -dc '0-9')
if [[ -n "$AVAIL_G" ]] && (( AVAIL_G < 20 )); then
  echo "   !! Only ${AVAIL_G} GB free here. The images alone are ~4 GB before any data."
  exit 1
fi
echo "   disk: ${AVAIL_G:-?} GB free"

# A published port already in use leaves the container in a restart loop with
# the reason buried in `docker compose logs`. Only worth checking on a first
# run — on a re-run these ports are held by our own containers, and this script
# is meant to be re-runnable.
if [[ -z "$(docker compose ps -q 2>/dev/null)" ]]; then
  BUSY=""
  for p in "$ES_PORT" "$KIBANA_PORT" "$FLEET_PORT" "$GRAFANA_PORT" "$LOGSTASH_TCP_PORT" "$LOGSTASH_TLS_PORT"; do
    if ss -tlnH 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${p}\$"; then BUSY="$BUSY $p"; fi
  done
  [[ -n "$BUSY" ]] && { echo "!! Already listening on:$BUSY — free them or change the ports in .env."; exit 1; }
fi

# A logging stack with a wrong clock produces data that is quietly useless, and
# a Proxmox guest that has been paused, migrated or restored from a snapshot is
# exactly where that happens.
if command -v timedatectl >/dev/null 2>&1; then
  if ! timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -q '^yes$'; then
    echo "   !! The clock is not NTP-synchronised. Timestamps will be wrong and"
    echo "      nothing will tell you so. Fix with: sudo timedatectl set-ntp true"
  else
    echo "   clock: NTP synchronised"
  fi
fi
echo "   ok"

# ── Elasticsearch ────────────────────────────────────────────────────────────
step "Starting Elasticsearch"
docker compose up -d elasticsearch
printf '   waiting for green/yellow '
for i in $(seq 1 60); do
  if esc -o /dev/null "${ES}/_cluster/health?wait_for_status=yellow&timeout=5s" 2>/dev/null; then
    echo " up"; break
  fi
  printf '.'; sleep 5
  [[ $i -eq 60 ]] && { echo; echo "!! Elasticsearch never came up. Check: docker compose logs elasticsearch"; exit 1; }
done

# ── Built-in and service accounts ────────────────────────────────────────────
step "Configuring users and roles"
esc -o /dev/null -XPOST -H 'Content-Type: application/json' \
  "${ES}/_security/user/kibana_system/_password" \
  -d "{\"password\":\"${KIBANA_SYSTEM_PASSWORD}\"}"
echo "   kibana_system password set"

# Logstash may only create documents in its own app.* data streams.
esc -o /dev/null -XPUT -H 'Content-Type: application/json' "${ES}/_security/role/logstash_writer" -d '{
  "cluster": ["monitor", "read_ilm"],
  "indices": [{
    "names": ["logs-app.*"],
    "privileges": ["auto_configure", "create_doc", "create_index", "write", "view_index_metadata"]
  }]
}'
esc -o /dev/null -XPUT -H 'Content-Type: application/json' "${ES}/_security/user/logstash_writer" \
  -d "{\"password\":\"${LOGSTASH_WRITER_PASSWORD}\",\"roles\":[\"logstash_writer\"],\"full_name\":\"Logstash TCP intake\"}"
echo "   logstash_writer created"

# Grafana gets read-only access and nothing else.
esc -o /dev/null -XPUT -H 'Content-Type: application/json' "${ES}/_security/role/grafana_reader" -d '{
  "cluster": ["monitor"],
  "indices": [{
    "names": ["logs-*", "metrics-*", "traces-*"],
    "privileges": ["read", "view_index_metadata"]
  }]
}'
esc -o /dev/null -XPUT -H 'Content-Type: application/json' "${ES}/_security/user/grafana_reader" \
  -d "{\"password\":\"${GRAFANA_READER_PASSWORD}\",\"roles\":[\"grafana_reader\"],\"full_name\":\"Grafana read-only\"}"
echo "   grafana_reader created"

# ── Retention and single-node defaults ───────────────────────────────────────
step "Applying retention and single-node settings"
for kind in logs:${LOG_RETENTION_DAYS} metrics:${METRIC_RETENTION_DAYS}; do
  name=${kind%%:*}; days=${kind##*:}
  esc -o /dev/null -XPUT -H 'Content-Type: application/json' "${ES}/_ilm/policy/homelab-${name}" -d "{
    \"policy\": {
      \"phases\": {
        \"hot\":    { \"actions\": { \"rollover\": { \"max_primary_shard_size\": \"20gb\", \"max_age\": \"7d\" } } },
        \"delete\": { \"min_age\": \"${days}d\", \"actions\": { \"delete\": {} } }
      }
    }
  }"
  # @custom component templates are the upgrade-safe hook: Elastic never
  # overwrites them, unlike the built-in logs@settings / metrics@settings.
  esc -o /dev/null -XPUT -H 'Content-Type: application/json' "${ES}/_component_template/${name}@custom" -d "{
    \"template\": {
      \"settings\": {
        \"index.number_of_replicas\": 0,
        \"index.lifecycle.name\": \"homelab-${name}\"
      }
    }
  }"
  echo "   ${name}: replicas=0, delete after ${days}d"
done

# ── Kibana ───────────────────────────────────────────────────────────────────
step "Starting Kibana (first boot downloads integration packages — be patient)"
docker compose up -d kibana
printf '   waiting for Kibana '
for i in $(seq 1 80); do
  if curl -fsS -k "${KB}/api/status" 2>/dev/null | grep -q '"level":"available"'; then echo " up"; break; fi
  printf '.'; sleep 5
  [[ $i -eq 80 ]] && { echo; echo "!! Kibana never became available. Check: docker compose logs kibana"; exit 1; }
done

# ── Fleet Server service token ───────────────────────────────────────────────
step "Minting the Fleet Server service token"
if [[ -z "${FLEET_SERVER_SERVICE_TOKEN:-}" ]]; then
  # A token name can only be created once, so clear any stale one first.
  esc -o /dev/null -XDELETE "${ES}/_security/service/elastic/fleet-server/credential/token/homelab" 2>/dev/null || true
  TOKEN=$(esc -XPOST "${ES}/_security/service/elastic/fleet-server/credential/token/homelab" \
          | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"]["value"])')
  sed -i "s|^FLEET_SERVER_SERVICE_TOKEN=.*|FLEET_SERVER_SERVICE_TOKEN=${TOKEN}|" .env
  export FLEET_SERVER_SERVICE_TOKEN="$TOKEN"
  echo "   token minted and written to .env"
else
  echo "   reusing the token already in .env"
fi

# ── Ingest pipelines for the media apps ──────────────────────────────────────
step "Installing ingest pipelines"
"../pipelines/install.sh"

# ── Everything else ──────────────────────────────────────────────────────────
step "Starting Fleet Server, Logstash and Grafana"
docker compose up -d

step "Creating Fleet agent policies"
python3 "../fleet/setup-policies.py"

# Writes the network-receiver dashboards now, and a per-host dashboard for
# every agent. There are none yet on a first run — re-run it after enrolling.
step "Generating Grafana dashboards"
python3 ./gen-dashboards.py

cat <<EOF

────────────────────────────────────────────────────────────────────────────
 Stack is up.

   Kibana    https://${STACK_IP}:${KIBANA_PORT}      elastic / (see .env)
   Grafana   http://${STACK_IP}:${GRAFANA_PORT}      admin / (see .env)
   Fleet     https://${STACK_IP}:${FLEET_PORT}
   App logs  tcp://${STACK_IP}:${LOGSTASH_TCP_PORT}  (plain)
             tcp://${STACK_IP}:${LOGSTASH_TLS_PORT}  (TLS)

 Next: enrol the agents.
   1. Copy stack/certs/ca/ca.crt to the Proxmox host and each VM.
   2. Grab the enrollment tokens from Kibana → Fleet → Enrollment tokens,
      or run:  ./fleet/enrollment-tokens.sh
   3. On Proxmox:  sudo ./proxmox/install-agent.sh <enrollment-token>
      On each VM:  sudo ./vms/install-agent.sh <enrollment-token>
   4. Once they check in, give each host a dashboard:
      ./stack/gen-dashboards.py

 Smoke-test the TCP intake from anywhere on the LAN:
   echo '{"dataset":"test","level":"info","message":"hello"}' | nc ${STACK_IP} ${LOGSTASH_TCP_PORT}
────────────────────────────────────────────────────────────────────────────
EOF
