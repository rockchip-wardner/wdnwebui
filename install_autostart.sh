#!/usr/bin/env bash
# Install the systemd autostart unit of wdnwebui
# Usage: sudo ./install_autostart.sh [--uninstall]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$SCRIPT_DIR/.venv/bin/python"

# TLS 公共 helper —— IP 探测 + SAN 精确比对。
# 与 scripts/gen_tls_ca.sh、scripts/tls_cert_check.sh 共用**同一份**实现，
# 免得三处各写各的、慢慢漂移。
TLS_COMMON="$SCRIPT_DIR/scripts/tls_common.sh"
if [ ! -f "$TLS_COMMON" ]; then
    echo "ERROR: 缺少 $TLS_COMMON（TLS 公共 helper）。" >&2
    exit 1
fi
# shellcheck source=scripts/tls_common.sh
source "$TLS_COMMON"

WEB_UNIT="/etc/systemd/system/wdnwebui.service"
CERT_CHECK_UNIT="/etc/systemd/system/wdnwebui-cert-check.service"
CERT_CHECK_TIMER="/etc/systemd/system/wdnwebui-cert-check.timer"

# TLS 证书目录 —— 与 scripts/gen_tls_ca.sh 用同一个环境变量名。
#
# 8000 已改为 uvicorn 原生 TLS（--ssl-certfile/--ssl-keyfile）。证书**不在
# 仓库里**，所以这里必须写绝对路径，不能用 $SCRIPT_DIR。
TLS_DIR="${WDNWEBUI_TLS_DIR:-/home/rockchip/.config/wdnwebui/tls}"
TLS_CERT="$TLS_DIR/cert.pem"
TLS_KEY="$TLS_DIR/key.pem"

UNINSTALL=false

for arg in "$@"; do
    case "$arg" in
        --uninstall) UNINSTALL=true ;;
        -h|--help)
            sed -n '2,3p' "$0"
            exit 0
            ;;
        *)
            echo "unknown argument: $arg" >&2
            sed -n '2,3p' "$0" >&2
            exit 2
            ;;
    esac
done

if [ "$EUID" -ne 0 ]; then
    echo "root privileges are required: please run it with sudo, e.g. sudo $0" >&2
    exit 1
fi

SERVICE_USER="${SUDO_USER:-$(whoami)}"

if [ ! -x "$PY" ]; then
    echo "$PY not found, please activate the venv or finish the dependency installation first" >&2
    exit 1
fi

uninstall() {
    local changed=0
    if [ -f "$WEB_UNIT" ]; then
        unit_name="$(basename "$WEB_UNIT")"
        systemctl disable --now "$unit_name" 2>/dev/null || true
        rm -f "$WEB_UNIT"
        echo "removed $WEB_UNIT"
        changed=1
    fi
    if [ -f "$CERT_CHECK_UNIT" ]; then
        systemctl disable --now "$(basename "$CERT_CHECK_UNIT")" 2>/dev/null || true
        systemctl disable --now "$(basename "$CERT_CHECK_TIMER")" 2>/dev/null || true
        rm -f "$CERT_CHECK_UNIT" "$CERT_CHECK_TIMER"
        echo "removed $CERT_CHECK_UNIT / $CERT_CHECK_TIMER"
        changed=1
    fi
    if [ "$changed" -eq 1 ]; then
        systemctl daemon-reload
    fi
    echo "uninstall complete"
}

# 证书巡检：查文件在不在 / 私钥读不读得到 / 30 天内过期不 / **SAN 是否覆盖
# 当前对外 IP**，最后一项不满足时自动重签 + 重启主服务。
#
# 逻辑放在 scripts/tls_cert_check.sh 而不是内联进 ExecStart —— 它要自动修复，
# 内联的单引号地狱没法维护。
write_cert_check_units() {
    cat > "$CERT_CHECK_UNIT" <<EOF
[Unit]
Description=wdnwebui TLS 证书巡检（存在性 + 30 天有效期 + SAN 覆盖当前 IP）
# 等网络就绪 —— 开机时 DHCP 租约可能还没拿到 IP，此时巡检会误判「SAN 没覆盖」。
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
# 退出码语义 = 当前是否还有问题：0 正常或已自动修复，1 修不好 ⇒ failed ⇒ 告警。
ExecStart=/bin/bash $SCRIPT_DIR/scripts/tls_cert_check.sh

[Install]
WantedBy=multi-user.target
EOF
    cat > "$CERT_CHECK_TIMER" <<EOF
[Unit]
Description=每日巡检 wdnwebui TLS 证书

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
EOF
    systemctl daemon-reload
    # 开机跑一次 —— 开机后 IP 变了要能当场发现并修复（而不是等午夜定时器）。
    systemctl enable --now "$(basename "$CERT_CHECK_UNIT")"
    # 每日巡检 —— 把「事后才发现」变成「提前一天在 journal 里看到」。
    systemctl enable --now "$(basename "$CERT_CHECK_TIMER")"
    echo "wrote $CERT_CHECK_UNIT / $CERT_CHECK_TIMER (service+timer enabled)"
}

# TLS 证书 —— 单元的 ``ConditionPathExists`` 直接依赖它。
#
# 必须在写单元**之前**准备好：证书缺失时 systemd 会**静默跳过**单元
# （Result=success / ActiveState=inactive / ConditionResult=no，journal 里
# 一条记录都没有）—— 现场只看到「服务 enabled 但没起来」，查不到任何报错。
ensure_tls_cert() {
    local gen="$SCRIPT_DIR/scripts/gen_tls_ca.sh"

    if ! command -v openssl >/dev/null 2>&1; then
        echo "ERROR: 找不到 openssl，无法签发/校验 TLS 证书。" >&2
        return 1
    fi

    local ip
    ip="$(wdn_tls_detect_ip)"
    if [ -z "$ip" ]; then
        echo "ERROR: 探测不到本机 IP，无法签发带正确 SAN 的证书。" >&2
        echo "       手动执行: bash $gen --ip=<本机IP>" >&2
        return 1
    fi

    # 已有证书：SAN 覆盖当前 IP 就复用；不覆盖（板子换 IP 了）就重签。
    local force=""
    if [ -r "$TLS_CERT" ] && [ -r "$TLS_KEY" ]; then
        if wdn_tls_san_has_ip "$TLS_CERT" "$ip"; then
            echo "TLS 证书已存在且 SAN 覆盖 $ip: $TLS_CERT"
            return 0
        fi
        echo "== 现有证书的 SAN 不含当前 IP $ip ⇒ 重新签发 =="
        force="--force-leaf"
    else
        echo "== 未找到 TLS 证书 ⇒ 签发 =="
    fi

    if [ ! -f "$gen" ]; then
        echo "ERROR: 缺少 $gen，无法自动签发 TLS 证书。" >&2
        echo "       手动执行: bash $gen --ip=$ip" >&2
        return 1
    fi

    # 以普通用户身份签发 ⇒ 文件属主跟家目录一致（服务以 root 跑 ⇒ 可读）。
    if [ "$EUID" -eq 0 ] && [ "$SERVICE_USER" != "root" ] && id "$SERVICE_USER" >/dev/null 2>&1; then
        sudo -u "$SERVICE_USER" bash "$gen" "$ip" $force
    else
        bash "$gen" "$ip" $force
    fi

    if [ ! -r "$TLS_CERT" ] || [ ! -r "$TLS_KEY" ]; then
        echo "ERROR: 证书签发后仍不可读: $TLS_CERT / $TLS_KEY" >&2
        return 1
    fi
    echo "TLS 证书就绪: $TLS_CERT"
}

write_web_unit() {
    cat > "$WEB_UNIT" <<EOF
[Unit]
Description=wdnwebui service
After=network-online.target
Wants=network-online.target
# ★ 证书缺失时**不启动**，而不是让 uvicorn 抛 FileNotFoundError 后被
#   systemd 每 5 秒重启一轮（Restart=on-failure）—— 那样整个 webui 会
#   无限重启刷屏，而且算力 worker 会被反复连坐杀掉。
#
# ⚠️ ConditionPathExists 必须写在 [Unit] 段！写进 [Service] 会被 systemd
#    **静默忽略**（日志里只有一行 "Unknown key 'ConditionPathExists' in
#    section [Service], ignoring."）⇒ 等于没有保护。已实测。
ConditionPathExists=$TLS_CERT
ConditionPathExists=$TLS_KEY

[Service]
Type=simple
User=root
WorkingDirectory=$SCRIPT_DIR
Environment=WDNWEBUI_CONFIG=$SCRIPT_DIR/config.jsonc
# --timeout-graceful-shutdown caps how long uvicorn waits for in-flight
# connections (WebSockets included) before closing them. Without a cap,
# uvicorn waits forever on a half-open socket, systemd hits its own stop
# timeout and SIGKILLs the whole cgroup — which takes the compute workers
# down with it. 5s keeps restarts snappy and still lets an in-flight
# request finish writing its response.
#
# --ssl-certfile/--ssl-keyfile: 8000 只提供 HTTPS（8443 中继已退役）。
# uvicorn 的 certfile 是必填，且私钥与证书不同文件时 keyfile 也必须给
# ⇒ 两个都要。缺证书时上面的 ConditionPathExists 会挡住启动。
ExecStart=$SCRIPT_DIR/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 \
  --ssl-certfile $TLS_CERT \
  --ssl-keyfile $TLS_KEY \
  --timeout-graceful-shutdown 5
Restart=on-failure
RestartSec=5
# Backstop for the graceful shutdown above: it must be larger than
# --timeout-graceful-shutdown, otherwise systemd kills the process while
# uvicorn is still shutting down cleanly.
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable wdnwebui.service
    echo "wrote $WEB_UNIT (enabled, not started)"
}

if [ "$UNINSTALL" = true ]; then
    uninstall
    exit 0
fi

# ⚠️ 顺序：先签证书，再写单元 —— 反过来的话单元会被装成「证书缺失 ⇒ 静默
#    跳过」的状态，而 journal 里一条记录都没有。
ensure_tls_cert
write_web_unit
write_cert_check_units

echo "done. Start it immediately with: systemctl start wdnwebui.service"
echo "v0.6+: discovery uses FastDDS SIMPLE multicast (239.255.0.1:7400), there is no separate daemon."
echo "If LAN multicast is blocked, add Environment=WDNDDS_INITIAL_PEERS=ip1,ip2,... to the systemd unit"
echo ""
echo "TLS: 8000 只提供 HTTPS，证书在 $TLS_DIR"
echo "  ⚠️ 访问地址是 https://<板子IP>:8000 —— http:// 打不开（明文协议已被移除）"
echo "  客户端需信任 CA 根：$TLS_DIR/ca.pem"
echo "  证书缺失时服务**不会启动**（ConditionPathExists）—— 上面已自动签发"
