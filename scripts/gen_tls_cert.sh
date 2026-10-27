#!/bin/bash
# 生成 wdnwebui TLS 转发生用的自签证书（幂等：已有就不重签）。
#
#   bash scripts/gen_tls_cert.sh [IP] [额外SAN]
#
# 证书放在 /home/rockchip/.config/wdnwebui/tls/（**不在仓库里**，免得
# 污染 git status）。浏览器第一次访问 https://<IP>:8443 会告警
# 「不是私密连接」——点「高级 → 继续前往」之后页面就是安全上下文，
# 麦克风能正常拿到（实测）。
#
# 想连告警都没有：用私有 CA 签发（把 CSR 交给自己的 CA），再把根证书
# 装到客户端的信任库里。
set -euo pipefail

IP="${1:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
EXTRA="${2:-}"
DIR=/home/rockchip/.config/wdnwebui/tls
CERT="$DIR/cert.pem"
KEY="$DIR/key.pem"

if [ -z "$IP" ]; then
  echo "用法: $0 <板子IP> [额外SAN]" >&2
  exit 2
fi

mkdir -p "$DIR"
if [ -f "$CERT" ] && [ -f "$KEY" ]; then
  echo "证书已存在: $CERT"
  openssl x509 -in "$CERT" -noout -subject -enddate
  exit 0
fi

SAN="IP:$IP"
# 板子的主机名也加进去（用 https://<主机名>:8443 访问时能用）
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
echo "已生成: $CERT (CN=$IP, SAN=$SAN)"
openssl x509 -in "$CERT" -noout -subject -enddate
