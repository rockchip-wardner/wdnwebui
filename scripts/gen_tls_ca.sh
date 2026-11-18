#!/bin/bash
# Generate the certificate for the wdnwebui TLS relay, **issued by a
# private CA** (idempotent).
#
#   bash scripts/gen_tls_ca.sh [IP] [--force-leaf]
#
# Why not "one self-signed certificate handed to the client"
# -----------------------------------------------------
# A self-signed certificate (the kind produced by
# ``scripts/gen_tls_cert.sh``) has only two ways to be used:
#
#   1. click "Advanced → Continue" on every visit — annoying;
#   2. install the certificate into the trust store of the client by
#      hand — but after a **self-signed leaf certificate** is in there,
#      every replacement (renewal, a new IP) needs **another round of
#      distribution**, and some browsers handle "a certificate the user
#      added manually" inconsistently.
#
# The private CA way: the CA root certificate is **self-signed**, but it
# is only used to **issue** the server certificate; the client trusts the
# **root**, and therefore:
#
#   * the client installs **one** file, valid **forever**;
#   * the server certificate can be renewed / moved to another IP at any
#     time, **without touching the client**;
#   * the browser does **not** complain about "not a private connection"
#     (as long as the SAN of the leaf certificate covers the address you
#     actually use — the SAN here holds the board IP + the hostname).
#
# The artefacts (all of them **outside the repo**, so that git status
# stays clean)::
#
#   /home/rockchip/.config/wdnwebui/tls/
#   ├── ca-key.pem   the CA private key (600) — only used when issuing,
#   │                it can be kept offline
#   ├── ca.pem       the CA root certificate — **this is the file you
#   │                hand to the client**
#   ├── key.pem      the server private key (600)
#   ├── cert.pem     the server certificate (issued by the CA) —
#                    tls_relay.py loads it
#   └── ca.cnf / server.cnf
#
# After the certificates are issued ``ca-key.pem`` can be moved away
# (kept offline) — put it back when a new certificate has to be issued.
# **A leaked CA private key means anybody can forge a certificate that
# this board accepts.**
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=tls_common.sh
source "$SCRIPT_DIR/tls_common.sh"

usage() {
    cat >&2 <<EOF
usage: $0 [IP] [--force-leaf]
       $0 --ip=<addr> [--force-leaf]

  IP            板子对外的 IP，省略则自动探测
  --force-leaf  强制重签服务器证书（换 IP 后必须加，否则旧证书被原样跳过）
  --ip=<addr>   同位置参数 IP
EOF
}

# 参数解析。IP 曾是裸位置参数（$1），于是 `--force-leaf` 会被当成 IP ⇒
# SAN 写成 "IP:--force-leaf" ⇒ openssl 失败 ⇒ 留下半成品目录。
IP=""
FORCE_LEAF=0
_IP_GIVEN=0

for a in "$@"; do
    case "$a" in
        --force-leaf) FORCE_LEAF=1 ;;
        --ip=*)       IP="${a#--ip=}"; _IP_GIVEN=1 ;;
        -h|--help)    usage; exit 0 ;;
        -*)           echo "unknown option: $a" >&2; usage; exit 2 ;;
        *)
            if [ "$_IP_GIVEN" -eq 1 ]; then
                echo "unexpected argument: $a (IP already given)" >&2
                usage
                exit 2
            fi
            IP="$a"; _IP_GIVEN=1
            ;;
    esac
done

DIR="${WDNWEBUI_TLS_DIR:-/home/rockchip/.config/wdnwebui/tls}"
CERT="$DIR/cert.pem"
KEY="$DIR/key.pem"
CA_CERT="$DIR/ca.pem"
CA_KEY="$DIR/ca-key.pem"

if [ "$_IP_GIVEN" -eq 1 ]; then
    if ! wdn_tls_is_ip "$IP"; then
        echo "ERROR: '$IP' 不是合法的 IP 地址。" >&2
        usage
        exit 2
    fi
else
    IP="$(wdn_tls_detect_ip)"
    if [ -z "$IP" ]; then
        echo "ERROR: 探测不到本机对外 IP，请显式指定（$0 <IP>）。" >&2
        usage
        exit 2
    fi
fi

mkdir -p "$DIR"
chmod 700 "$DIR"

# 主机名必须是合法 DNS label —— 含非法字符时 openssl 会写出一条无效 SAN。
HOSTN="$( { hostname 2>/dev/null || true; } | tr -cd 'A-Za-z0-9.-' | sed 's/^[.-]*//; s/[.-]*$//')"

# SAN 的 IP 条目：显式给的 IP 在前，其余候选地址追加。
# 候选全写进去是「探测猜错时的兜底」——猜错只是多一条无害条目，漏了却是
# 浏览器报「证书名称与域名不符」。
SAN="$( { printf '%s\n' "$IP"; wdn_tls_detect_ips; } \
        | awk 'NF && !seen[$0]++' | sed 's/.*/IP:&/' | paste -sd, - )"

[ -n "$HOSTN" ] && SAN="$SAN,DNS:$HOSTN"
# Add localhost as well: it is just as trusted when the client uses
# https://localhost:8000.
SAN="$SAN,DNS:localhost,IP:127.0.0.1"

# --------------------------------------------------------------------------- #
# 1) the CA root certificate (self-signed, CA:TRUE)
# --------------------------------------------------------------------------- #
if [ ! -f "$CA_CERT" ] || [ ! -f "$CA_KEY" ]; then
  echo "== generating the private CA =="
  cat > "$DIR/ca.cnf" <<EOF
[req]
distinguished_name = dn
x509_extensions = v3
prompt = no
[dn]
CN = wdnwebui Local CA
O = wdnwebui
[v3]
basicConstraints = critical,CA:TRUE,pathlen:0
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
EOF
  openssl req -x509 -newkey rsa:4096 -nodes \
    -keyout "$CA_KEY" -out "$CA_CERT" -days 3650 \
    -config "$DIR/ca.cnf" 2>/dev/null
  chmod 600 "$CA_KEY"
  chmod 644 "$CA_CERT"
  openssl x509 -in "$CA_CERT" -noout -subject -enddate
else
  echo "== the CA already exists, skipping =="
  openssl x509 -in "$CA_CERT" -noout -subject -enddate
fi

# --------------------------------------------------------------------------- #
# 2) the server certificate (issued by the CA)
# --------------------------------------------------------------------------- #
if [ "$FORCE_LEAF" = "1" ] || [ ! -f "$CERT" ] || [ ! -f "$KEY" ]; then
  echo "== issuing the server certificate (SAN=$SAN) =="
  cat > "$DIR/server.cnf" <<EOF
[req]
distinguished_name = dn
prompt = no
[dn]
CN = $IP
O = wdnwebui
[v3]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = $SAN
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid,issuer
EOF
  openssl req -newkey rsa:2048 -nodes \
    -keyout "$KEY" -out "$DIR/csr.pem" \
    -config "$DIR/server.cnf" 2>/dev/null
  openssl x509 -req -in "$DIR/csr.pem" \
    -CA "$CA_CERT" -CAkey "$CA_KEY" -CAcreateserial \
    -CAserial "$DIR/ca.srl" -days 3650 \
    -extfile "$DIR/server.cnf" -extensions v3 \
    -out "$CERT" 2>/dev/null
  chmod 600 "$KEY"
  chmod 644 "$CERT"
  rm -f "$DIR/csr.pem"
else
  echo "== the server certificate already exists, skipping (add --force-leaf to re-issue it) =="
fi

echo
echo "artefacts:"
openssl x509 -in "$CERT" -noout -subject -issuer -enddate
echo "SAN: $(openssl x509 -in "$CERT" -noout -ext subjectAltName | tail -1 | xargs)"
echo
echo "Hand the file below to the client and install it into its trust store:"
echo "  $CA_CERT"
