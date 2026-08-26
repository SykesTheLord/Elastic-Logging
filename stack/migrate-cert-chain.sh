#!/usr/bin/env bash
# Repair a deployment whose certificates serve a leaf-only chain.
#
# Elastic Agent trusts Elasticsearch by CA fingerprint, and that matches a CA
# *in the chain the server presents*. Certificates generated before this was
# fixed contain only the leaf, so every agent fails with
#
#     x509: certificate signed by unknown authority
#
# while Kibana, Grafana, Logstash and Fleet Server all stay healthy — they use
# a CA file directly rather than the fingerprint. Enrolment succeeds too, so
# agents appear, go healthy, and quietly ship nothing.
#
# This appends the existing CA to each service certificate. It deliberately
# does NOT regenerate anything: the CA is unchanged, so every enrolled agent
# keeps working and none need re-enrolling.
#
#   ./migrate-cert-chain.sh           # check, then fix, then verify
#   ./migrate-cert-chain.sh --check   # report only, change nothing
set -euo pipefail
cd "$(dirname "$0")"

CHECK_ONLY=false
[[ ${1:-} == --check || ${1:-} == -n || ${1:-} == --dry-run ]] && CHECK_ONLY=true

if [[ $EUID -eq 0 ]]; then SUDO=""; else SUDO="sudo"; fi
step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

SERVICES="elasticsearch kibana fleet-server logstash"

# ── Preflight ────────────────────────────────────────────────────────────────
step "Checking this deployment"
[[ -f .env ]]            || { echo "!! No .env here. Run this from the stack/ directory."; exit 1; }
[[ -f certs/ca/ca.crt ]] || { echo "!! No certs/ca/ca.crt. Nothing to migrate — run ./setup-certs.sh."; exit 1; }
set -a; . ./.env; set +a

REAL_FP=$(openssl x509 -in certs/ca/ca.crt -outform DER 2>/dev/null | sha256sum | cut -d' ' -f1)
echo "   CA on disk: ${REAL_FP:0:16}…"

# A fingerprint in .env that does not match the CA on disk is a different
# fault, and appending chains will not fix it.
if [[ -n "${ES_CA_FINGERPRINT:-}" && "$ES_CA_FINGERPRINT" != "$REAL_FP" ]]; then
  echo "   !! ES_CA_FINGERPRINT in .env does not match certs/ca/ca.crt."
  echo "      Someone regenerated the CA without re-running bootstrap.sh. Fix that"
  echo "      first: set it to ${REAL_FP}"
  echo "      then 'docker compose up -d kibana' so Fleet picks up the new output."
fi

# ── What needs doing ─────────────────────────────────────────────────────────
TODO=""
for s in $SERVICES; do
  f="certs/${s}/${s}.crt"
  if [[ ! -f $f ]]; then
    printf '   %-14s missing — skipping\n' "$s"
    continue
  fi
  n=$(grep -c 'BEGIN CERTIFICATE' "$f" || true)
  if (( n <= 1 )); then
    printf '   %-14s leaf only            NEEDS FIXING\n' "$s"
    TODO="$TODO $s"
  else
    printf '   %-14s %d certs in chain    ok\n' "$s" "$n"
  fi
done

if [[ -z "$TODO" ]]; then
  echo
  echo "Nothing to do — every certificate already serves its chain."
  exit 0
fi

if $CHECK_ONLY; then
  echo
  echo "--check given: nothing changed. Re-run without it to apply."
  exit 0
fi

# ── Back up, then append ─────────────────────────────────────────────────────
BACKUP="certs.bak-$(date +%Y%m%d-%H%M%S)"
step "Backing up to ${BACKUP}"
$SUDO cp -a certs "$BACKUP"
echo "   done — restore with: rm -rf certs && mv ${BACKUP} certs"

step "Appending the CA to the certificates that need it"
for s in $TODO; do
  $SUDO tee -a "certs/${s}/${s}.crt" < certs/ca/ca.crt >/dev/null
  echo "   ${s}: now $(grep -c 'BEGIN CERTIFICATE' "certs/${s}/${s}.crt") certs"
done

# ── Restart whatever is actually running ─────────────────────────────────────
RUNNING=$(docker compose ps --services --status running 2>/dev/null || true)
RESTART=""
for s in $SERVICES; do
  grep -qx "$s" <<<"$RUNNING" 2>/dev/null && RESTART="$RESTART $s"
done

if [[ -z "$RESTART" ]]; then
  step "Nothing running to restart"
  echo "   Certificates are fixed. Start the stack with: docker compose up -d"
  exit 0
fi

step "Restarting:${RESTART}"
# shellcheck disable=SC2086
docker compose restart $RESTART

# ── Verify against the live listener ─────────────────────────────────────────
step "Verifying"
printf '   waiting for Elasticsearch '
for i in $(seq 1 40); do
  if curl -fsS --cacert certs/ca/ca.crt -u "elastic:${ELASTIC_PASSWORD}" \
       "https://localhost:${ES_PORT}/_cluster/health" >/dev/null 2>&1; then
    echo " up"; break
  fi
  printf '.'; sleep 5
  [[ $i -eq 40 ]] && { echo; echo "!! Elasticsearch did not come back. docker compose logs elasticsearch"; exit 1; }
done

CHAIN=$(echo | openssl s_client -connect "localhost:${ES_PORT}" -showcerts 2>/dev/null \
        | grep -c 'BEGIN CERTIFICATE' || true)
echo "   certificates now served by Elasticsearch: ${CHAIN}"
if (( CHAIN < 2 )); then
  echo "!! Still serving a leaf-only chain. Restore with:"
  echo "     rm -rf certs && mv ${BACKUP} certs && docker compose restart${RESTART}"
  exit 1
fi

SERVED_FP=$(echo | openssl s_client -connect "localhost:${ES_PORT}" -showcerts 2>/dev/null \
  | awk '/BEGIN CERT/{c++} c==2' | sed -n '/BEGIN/,/END/p' \
  | openssl x509 -outform DER 2>/dev/null | sha256sum | cut -d' ' -f1)
if [[ "$SERVED_FP" == "$REAL_FP" ]]; then
  echo "   the CA in the chain matches ES_CA_FINGERPRINT — agents will trust it"
else
  echo "   !! chain CA ${SERVED_FP:0:16}… does not match ${REAL_FP:0:16}…"
  exit 1
fi

cat <<EOF

────────────────────────────────────────────────────────────────────────────
 Done. The CA was not changed, so no agent needs re-enrolling — they will
 reconnect on their own within a minute or two.

 Watch one recover:
   elastic-agent status            # on any agent host
   journalctl -u elastic-agent -f

 Backup kept at ${BACKUP} — delete it once you are happy.
────────────────────────────────────────────────────────────────────────────
EOF
