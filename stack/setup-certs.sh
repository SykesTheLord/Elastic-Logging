#!/usr/bin/env bash
# Generates a private CA and per-service TLS certificates for the stack.
#
# Re-run this if STACK_IP or STACK_DNS ever changes — the addresses are baked
# into the certificate SANs, and agents will refuse to connect otherwise.
set -euo pipefail
cd "$(dirname "$0")"

# Already root (the usual case on a fresh VM, and under `sudo ./setup-certs.sh`)?
# Then call the tools directly — a minimal Ubuntu image has no sudo binary, and
# shelling out to one that does not exist aborts the script half-way through.
if [[ $EUID -eq 0 ]]; then SUDO=""; else SUDO="sudo"; fi

[[ -f .env ]] || { echo "!! No .env found. Copy .env.example to .env and edit it first."; exit 1; }
set -a; . ./.env; set +a

: "${STACK_VERSION:?}" "${STACK_IP:?}" "${STACK_DNS:?}"

FORCE=false
[[ ${1:-} == --force || ${1:-} == -f ]] && FORCE=true

if [[ -f certs/ca/ca.crt ]]; then
  if $FORCE; then
    echo ">> --force given: regenerating (every enrolled agent will need re-enrolling)"
  elif [[ -t 0 ]]; then
    read -rp "certs/ already exists. Regenerate and invalidate every enrolled agent? [y/N] " ok
    [[ ${ok:-} == [yY] ]] || { echo "Aborted."; exit 0; }
  else
    # No TTY: a half-finished first run must not turn into a silent hang.
    echo "!! certs/ already exists and there is no terminal to ask on."
    echo "   Re-run with --force to regenerate, or delete certs/ first."
    exit 1
  fi
  $SUDO rm -rf certs
fi
mkdir -p certs

cat > certs/instances.yml <<YAML
instances:
  - name: elasticsearch
    dns:  [ elasticsearch, localhost, ${STACK_DNS} ]
    ip:   [ 127.0.0.1, ${STACK_IP} ]
  - name: kibana
    dns:  [ kibana, localhost, ${STACK_DNS} ]
    ip:   [ 127.0.0.1, ${STACK_IP} ]
  - name: fleet-server
    dns:  [ fleet-server, localhost, ${STACK_DNS} ]
    ip:   [ 127.0.0.1, ${STACK_IP} ]
  - name: logstash
    dns:  [ logstash, localhost, ${STACK_DNS} ]
    ip:   [ 127.0.0.1, ${STACK_IP} ]
YAML

echo ">> Generating CA and certificates with elasticsearch-certutil ..."
# --user 0:0 matters: the Elasticsearch image runs as uid 1000, and `mkdir`
# above created certs/ owned by root, so the default user cannot write its
# output there. Everything is chowned to 1000:0 a few lines below anyway.
docker run --rm --user 0:0 -v "$PWD/certs:/certs" \
  docker.elastic.co/elasticsearch/elasticsearch:"${STACK_VERSION}" \
  bash -c '
    set -e
    elasticsearch-certutil ca --silent --pem --days 3650 -out /certs/ca.zip
    unzip -q -o /certs/ca.zip -d /certs
    elasticsearch-certutil cert --silent --pem --days 3650 \
      --in /certs/instances.yml \
      --ca-cert /certs/ca/ca.crt --ca-key /certs/ca/ca.key \
      -out /certs/certs.zip
    unzip -q -o /certs/certs.zip -d /certs
    rm -f /certs/ca.zip /certs/certs.zip
  '

# Serve the full chain, not just the leaf.
#
# certutil --pem writes only the leaf certificate. Elastic Agent trusts
# Elasticsearch by CA fingerprint (ca_trusted_fingerprint), and that works by
# looking for a certificate with that SHA-256 *in the chain the server
# presents* — so with a leaf-only chain it can never match, and every agent
# fails with "x509: certificate signed by unknown authority" while enrolment
# still succeeds. Appending the CA is what Elasticsearch's own auto-configured
# security does, and it is why that setup works out of the box.
echo ">> Appending the CA to each certificate so the full chain is served ..."
for svc in elasticsearch kibana fleet-server logstash; do
  cat certs/ca/ca.crt >> "certs/${svc}/${svc}.crt"
done

# Logstash's TCP input is Netty-based and only accepts PKCS#8 keys, while
# certutil emits PKCS#1. Convert once here so the TLS listener actually starts.
echo ">> Converting the Logstash key to PKCS#8 ..."
$SUDO openssl pkcs8 -topk8 -nocrypt \
  -in  certs/logstash/logstash.key \
  -out certs/logstash/logstash.pkcs8.key

# Containers run as uid 1000 (es/kibana/logstash) or root (elastic-agent).
# Grafana is uid 472 and only ever reads ca/ca.crt.
echo ">> Fixing ownership and permissions ..."
$SUDO chown -R 1000:0 certs
$SUDO find certs -type d -exec chmod 0750 {} \;
$SUDO find certs -name '*.key' -exec chmod 0640 {} \;
$SUDO find certs -name '*.crt' -exec chmod 0644 {} \;
$SUDO chmod 0755 certs certs/ca
$SUDO chmod 0644 certs/ca/ca.crt

# ca_trusted_fingerprint is the SHA-256 of the DER-encoded CA certificate.
FP=$($SUDO openssl x509 -in certs/ca/ca.crt -outform DER | sha256sum | cut -d' ' -f1)
if grep -q '^ES_CA_FINGERPRINT=' .env; then
  sed -i "s|^ES_CA_FINGERPRINT=.*|ES_CA_FINGERPRINT=${FP}|" .env
else
  printf '\nES_CA_FINGERPRINT=%s\n' "$FP" >> .env
fi

echo
echo "OK. CA fingerprint written to .env:"
echo "    ES_CA_FINGERPRINT=${FP}"
echo
echo "Copy certs/ca/ca.crt to every Proxmox host and VM that will run an agent —"
echo "it is needed to verify Fleet Server at enrollment time."
