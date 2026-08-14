#!/usr/bin/env bash
# Generate a platform-internal CA and service certificates.
#
# An air-gapped site cannot use a public CA — there is no ACME, no OCSP responder
# and no CRL distribution point reachable from the network. So the platform issues
# its own, and every node agent pins it.
#
# Consumed from Phase 1, when the control plane starts talking to node agents.
# §M04 is explicit that the Docker socket must not be exposed over an unsecured
# network; mTLS with this CA plus a per-node bearer token is how that is met.
#
#   ./scripts/gen_certs.sh                    # CA + server cert for the control plane
#   ./scripts/gen_certs.sh node gpu-node-01   # client cert for one node agent
#
# Output: certs/ (git-ignored — private keys must never be committed)

set -euo pipefail

CERT_DIR="${CERT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/certs}"
DAYS_CA="${DAYS_CA:-3650}"
DAYS_LEAF="${DAYS_LEAF:-825}"   # ~27 months: the maximum most TLS stacks accept
KEY_BITS=4096
SUBJECT_PREFIX="/C=AE/O=Private AI Platform"

mkdir -p "$CERT_DIR"
chmod 700 "$CERT_DIR"

log() { printf '  %s\n' "$*"; }

ensure_ca() {
  if [[ -f "$CERT_DIR/ca.crt" && -f "$CERT_DIR/ca.key" ]]; then
    log "CA already present — reusing $CERT_DIR/ca.crt"
    return
  fi
  log "Generating CA (valid ${DAYS_CA} days)"
  openssl genrsa -out "$CERT_DIR/ca.key" "$KEY_BITS" 2>/dev/null
  chmod 600 "$CERT_DIR/ca.key"
  openssl req -x509 -new -nodes \
    -key "$CERT_DIR/ca.key" \
    -sha256 -days "$DAYS_CA" \
    -subj "${SUBJECT_PREFIX}/OU=Platform CA/CN=AI Platform Root CA" \
    -out "$CERT_DIR/ca.crt" 2>/dev/null
  log "CA written to $CERT_DIR/ca.crt"
}

# issue <name> <cn> <extfile-content>
issue() {
  local name="$1" cn="$2" ext="$3"
  log "Issuing certificate for '${cn}'"
  openssl genrsa -out "$CERT_DIR/${name}.key" "$KEY_BITS" 2>/dev/null
  chmod 600 "$CERT_DIR/${name}.key"
  openssl req -new \
    -key "$CERT_DIR/${name}.key" \
    -subj "${SUBJECT_PREFIX}/CN=${cn}" \
    -out "$CERT_DIR/${name}.csr" 2>/dev/null

  printf '%s\n' "$ext" > "$CERT_DIR/${name}.ext"
  openssl x509 -req -in "$CERT_DIR/${name}.csr" \
    -CA "$CERT_DIR/ca.crt" -CAkey "$CERT_DIR/ca.key" -CAcreateserial \
    -out "$CERT_DIR/${name}.crt" -days "$DAYS_LEAF" -sha256 \
    -extfile "$CERT_DIR/${name}.ext" 2>/dev/null

  rm -f "$CERT_DIR/${name}.csr" "$CERT_DIR/${name}.ext"
  log "  -> ${name}.crt / ${name}.key"
}

case "${1:-server}" in
  server)
    ensure_ca
    # SANs must cover every name the control plane is reached by. Modern TLS
    # clients ignore CN entirely, so a missing SAN fails verification even when
    # the CN looks right — a routinely wasted afternoon.
    issue "server" "ai-platform.local" "$(cat <<'EXT'
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:ai-platform.local,DNS:localhost,DNS:backend,DNS:nginx,IP:127.0.0.1
EXT
)"
    ;;
  node)
    node_name="${2:?Usage: $0 node <node-name>}"
    ensure_ca
    # clientAuth only: a node-agent certificate must not be usable to impersonate
    # the control plane.
    issue "node-${node_name}" "${node_name}" "$(cat <<EXT
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=clientAuth
subjectAltName=DNS:${node_name}
EXT
)"
    ;;
  *)
    printf 'Usage: %s [server|node <node-name>]\n' "$0" >&2
    exit 2
    ;;
esac

printf '\nDone. Certificates in %s\n' "$CERT_DIR"
printf 'Distribute ca.crt to every node agent; keep ca.key offline and backed up.\n'
