#!/bin/bash
# Generate the self-signed certificate used by the wdnwebui TLS relay
# (idempotent: an existing one is not re-signed).
#
#   bash scripts/gen_tls_cert.sh [IP] [extra SAN]
#
# The certificate lives in /home/rockchip/.config/wdnwebui/tls/ (**not in
# the repo**, so that git status stays clean). The first visit of a
# browser to https://<IP>:8443 warns about "not a private connection" —
# after clicking "Advanced → Continue" the page is a secure context and
# the microphone works (measured).
#
# To avoid the warning altogether: have a private CA issue the
# certificate (hand the CSR to your own CA) and install the root
# certificate into the trust store of the client.
set -euo pipefail

IP="${1:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
EXTRA="${2:-}"
DIR=/home/rockchip/.config/wdnwebui/tls
CERT="$DIR/cert.pem"
KEY="$DIR/key.pem"

if [ -z "$IP" ]; then
  echo "usage: $0 <board IP> [extra SAN]" >&2
  exit 2
fi

mkdir -p "$DIR"
if [ -f "$CERT" ] && [ -f "$KEY" ]; then
  echo "the certificate already exists: $CERT"
  openssl x509 -in "$CERT" -noout -subject -enddate
  exit 0
fi

SAN="IP:$IP"
# The hostname of the board is added as well (so that
# https://<hostname>:8443 works)
HOSTN="$(hostname 2>/dev/null || true)"
[ -n "$HOSTN" ] && SAN="$SAN,DNS:$HOSTN"
[ -n "$EXTRA" ] && SAN="$SAN,$EXTRA"

cat > "$DIR/openssl.cnf" <<EOF
[req]
distinguished_name = dn
x509_extensions = v3
prompt = no
[dn]
CN = $IP
[v3]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = $SAN
EOF

openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout "$KEY" -out "$CERT" -days 3650 \
  -config "$DIR/openssl.cnf" 2>/dev/null

chmod 600 "$KEY"
chmod 644 "$CERT"
echo "generated: $CERT (CN=$IP, SAN=$SAN)"
openssl x509 -in "$CERT" -noout -subject -enddate
