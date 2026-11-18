#!/usr/bin/env bash
# wdnwebui TLS 证书巡检 —— 被 systemd 单元 wdnwebui-cert-check.service 调用。
#
# 检查：文件在不在 / 私钥读不读得到 / 30 天内过期不 / **SAN 是否覆盖当前
# 对外 IP**。最后一项不满足时 ⇒ 自动重签 + 重启主服务。
#
# 退出码 = 当前是否还有问题：0 正常或已修复，1 修不好（单元标 failed ⇒ 告警）。
#
# 为什么必须查 SAN：板子 IP 是 DHCP 分配的 ⇒ 换 IP 后**证书文件仍然在**
# ⇒ ConditionPathExists 满足 ⇒ 服务照常启动，但浏览器报「证书名称与域名
# 不符」⇒ 看起来跟「服务没起来」一模一样，根因却完全不同。
#
# 手动跑法：sudo bash scripts/tls_cert_check.sh
set -uo pipefail   # 不用 -e：退出码由本脚本显式控制

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=tls_common.sh
source "$SCRIPT_DIR/tls_common.sh"

TLS_DIR="${WDNWEBUI_TLS_DIR:-/home/rockchip/.config/wdnwebui/tls}"
CERT="$TLS_DIR/cert.pem"
KEY="$TLS_DIR/key.pem"
CA_CERT="$TLS_DIR/ca.pem"
CA_KEY="$TLS_DIR/ca-key.pem"
EXPIRY_WARN_SECONDS="${WDNWEBUI_TLS_EXPIRY_WARN:-2592000}"   # 30 天
MAIN_UNIT="wdnwebui.service"

_now() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }
log()  { echo "[$(_now)] $*"; }
fail() { echo "[$(_now)] ERROR: $*" >&2; exit 1; }

command -v openssl >/dev/null 2>&1 || fail "找不到 openssl，无法巡检证书"

# ---- 1. 文件在不在 -------------------------------------------------------- #
[ -r "$CERT" ]    || fail "证书不存在或不可读: $CERT"
[ -r "$KEY" ]     || fail "私钥不存在或不可读: $KEY"
[ -r "$CA_CERT" ] || fail "CA 根证书不存在或不可读: $CA_CERT"

# ---- 2. 有效期 ------------------------------------------------------------ #
# openssl -checkend 成功时也会往 stdout 打一行 "Certificate will not expire"
# ⇒ 收进变量，只在失败时打出来。
if ! _exp_out="$(openssl x509 -checkend "$EXPIRY_WARN_SECONDS" -noout -in "$CERT" 2>&1)"; then
    fail "证书 $EXPIRY_WARN_SECONDS 秒内过期（或已过期）: $CERT ${_exp_out:+（$_exp_out）}"
fi

# ---- 3. SAN 是否覆盖当前对外 IP ------------------------------------------- #
CURRENT_IP="$(wdn_tls_detect_ip)"
if [ -z "$CURRENT_IP" ]; then
    # 开机时 DHCP 租约可能还没拿到 IP ⇒ 瞬时状态，不是故障 ⇒ 跳过。
    log "SKIP: 本机对外 IP 尚未就绪（DHCP 可能还没拿到租约），本次巡检跳过"
    exit 0
fi

if wdn_tls_san_has_ip "$CERT" "$CURRENT_IP"; then
    log "OK: 证书有效，SAN 覆盖当前 IP $CURRENT_IP"
    exit 0
fi

log "WARN: 证书 SAN 未覆盖当前 IP $CURRENT_IP ⇒ 自动重签"

# ---- 4. 自动修复 ---------------------------------------------------------- #
if [ ! -r "$CA_KEY" ]; then
    fail "CA 私钥不在（$CA_KEY）⇒ 无法自动重签。请手工执行：
       bash $SCRIPT_DIR/gen_tls_ca.sh --ip=$CURRENT_IP --force-leaf
       sudo systemctl restart $MAIN_UNIT"
fi

# 以证书属主的身份签发 ⇒ 文件属主保持不变（服务以 root 跑，读得了）。
OWNER="$(stat -c %U "$CERT" 2>/dev/null || echo root)"
if [ "$(id -u)" -eq 0 ] && [ "$OWNER" != "root" ] && id "$OWNER" >/dev/null 2>&1; then
    sudo -u "$OWNER" bash "$SCRIPT_DIR/gen_tls_ca.sh" --ip="$CURRENT_IP" --force-leaf \
        || fail "自动重签失败"
else
    bash "$SCRIPT_DIR/gen_tls_ca.sh" --ip="$CURRENT_IP" --force-leaf \
        || fail "自动重签失败"
fi

# 重签完必须复核 —— 不然「以为修好了」比没修更糟。
wdn_tls_san_has_ip "$CERT" "$CURRENT_IP" \
    || fail "重签后 SAN 仍不覆盖 $CURRENT_IP（请人工介入）"
log "FIXED: 已重签证书，SAN 现在包含 $CURRENT_IP"

# 重启主服务让它加载新证书。重启会连带杀掉 cgroup 里的 compute_worker
# （KillMode=control-group），但主服务启动时会按 compute.autostart 重新
# pre-warm ⇒ 算力会自动回来。
if systemctl cat "$MAIN_UNIT" >/dev/null 2>&1; then
    systemctl restart "$MAIN_UNIT" || fail "重签成功，但重启 $MAIN_UNIT 失败"
    log "FIXED: 已重启 $MAIN_UNIT（worker 会按 compute.autostart 重新拉起）"
else
    log "NOTE: $MAIN_UNIT 未安装，跳过重启"
fi

exit 0
