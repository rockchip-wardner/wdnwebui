#!/bin/bash
# 生成 wdnwebui TLS 转发用的**私有 CA 签发**的证书（幂等）。
#
#   bash scripts/gen_tls_ca.sh [IP] [--force-leaf]
#
# 为什么不是「一张自签证书直接发给客户端」
# ----------------------------------------
# 自签证书（``scripts/gen_tls_cert.sh`` 那种）只有两种用法：
#
#   1. 每次访问点「高级 → 继续前往」—— 烦；
#   2. 手动把证书塞进客户端信任库—— 但**自签的叶子证书**塞进去之后，
#      换一张（续签、换 IP）就要**重新分发一次**，而且部分浏览器对
#      「用户手动加入的证书」处理不一致。
#
# 私有 CA 的路子是：CA 根证书**自签**，但它只用来**签发**服务端证书；
# 客户端信任的是**根**，于是：
#
#   * 客户端只装**一个**文件，**永久**有效；
#   * 服务端证书可以随时续签 / 换 IP，**不用再碰客户端**；
#   * 浏览器**不弹**「不是私密连接」（前提：叶子证书的 SAN 覆盖你
#     实际访问的地址 —— 这里的 SAN 会有板子 IP + 主机名）。
#
# 产物（都在**仓库外**，免得污染 git status）::
#
#   /home/rockchip/.config/wdnwebui/tls/
#   ├── ca-key.pem   CA 私钥（600）—— 只在签发时用，可离线保存
#   ├── ca.pem       CA 根证书 —— **这个发给客户端**
#   ├── key.pem      服务端私钥（600）
#   ├── cert.pem     服务端证书（由 CA 签发）—— tls_relay.py 加载它
#   └── ca.cnf / server.cnf
#
# 签发完可以把 ``ca-key.pem`` 挪走（离线保管）—— 以后要签新证书时
# 再放回来。**CA 私钥泄露 = 任何人都能伪造一个本板认可的证书。**
set -euo pipefail

IP="${1:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
FORCE_LEAF=0
for a in "$@"; do
  [ "$a" = "--force-leaf" ] && FORCE_LEAF=1
done

DIR="${WDNWEBUI_TLS_DIR:-/home/rockchip/.config/wdnwebui/tls}"
CERT="$DIR/cert.pem"
KEY="$DIR/key.pem"
CA_CERT="$DIR/ca.pem"
CA_KEY="$DIR/ca-key.pem"

if [ -z "$IP" ]; then
  echo "用法: $0 <板子IP> [--force-leaf]" >&2
  exit 2
fi

mkdir -p "$DIR"
chmod 700 "$DIR"

HOSTN="$(hostname 2>/dev/null || true)"
SAN="IP:$IP"
[ -n "$HOSTN" ] && SAN="$SAN,DNS:$HOSTN"
# 顺手把 localhost 也加上：客户端用 https://localhost:8443 访问时同样可信。
SAN="$SAN,DNS:localhost,IP:127.0.0.1"

# --------------------------------------------------------------------------- #
# 1) CA 根证书（自签，CA:TRUE）
# --------------------------------------------------------------------------- #
if [ ! -f "$CA_CERT" ] || [ ! -f "$CA_KEY" ]; then
  echo "== 生成私有 CA =="
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
  echo "== CA 已存在，跳过 =="
  openssl x509 -in "$CA_CERT" -noout -subject -enddate
fi

# --------------------------------------------------------------------------- #
# 2) 服务端证书（由 CA 签发）
# --------------------------------------------------------------------------- #
if [ "$FORCE_LEAF" = "1" ] || [ ! -f "$CERT" ] || [ ! -f "$KEY" ]; then
  echo "== 签发服务端证书（SAN=$SAN）=="
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
  echo "== 服务端证书已存在，跳过（要重签加 --force-leaf）=="
fi

echo
echo "产物："
openssl x509 -in "$CERT" -noout -subject -issuer -enddate
echo "SAN: $(openssl x509 -in "$CERT" -noout -ext subjectAltName | tail -1 | xargs)"
echo
echo "把下面这个文件发给客户端，装进它的信任库："
echo "  $CA_CERT"
