#!/usr/bin/env bash
# Install the systemd autostart unit of wdnwebui
# Usage: sudo ./install_autostart.sh [--uninstall]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$SCRIPT_DIR/.venv/bin/python"

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

# 证书巡检：每天查一次「证书在不在 / 私钥读得到吗 / 30 天内会不会过期」。
#
# 为什么需要：8000 改为原生 TLS 后，**证书成了主服务的硬依赖** ——
# 证书文件缺失 ⇒ uvicorn 在绑定端口前就退出 ⇒ systemd 反复重启 ⇒ 全站不可用。
# 这个定时器把「事后才发现」变成「提前一天在 journal 里看到」。
write_cert_check_units() {
    cat > "$CERT_CHECK_UNIT" <<EOF
[Unit]
Description=wdnwebui TLS 证书巡检（存在性 + 30 天有效期）

[Service]
Type=oneshot
# ⚠️ 失败必须**非零退出**（exit 1）⇒ systemd 把单元标成 failed
#    ⇒ ``systemctl status`` 能看出异常、按单元失败告警的监控也会触发。
#    早先写成 ``a && b && echo OK || echo WARN`` 的形式 —— 那个链条的最后
#    一个命令是 echo，退出码恒为 0 ⇒ 证书没了也显示 success（已实测）。
ExecStart=/bin/sh -c 'if /usr/bin/test -r $TLS_CERT && /usr/bin/test -r $TLS_KEY && /usr/bin/openssl x509 -checkend 2592000 -noout -in $TLS_CERT; then echo "wdnwebui 证书 OK"; else echo "WARN: wdnwebui 证书缺失/不可读/30 天内过期 —— 服务将无法启动" >&2; exit 1; fi'

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
    systemctl enable --now "$(basename "$CERT_CHECK_TIMER")"
    echo "wrote $CERT_CHECK_UNIT / $CERT_CHECK_TIMER (timer enabled)"
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

write_web_unit
write_cert_check_units

echo "done. Start it immediately with: systemctl start wdnwebui.service"
echo "v0.6+: discovery uses FastDDS SIMPLE multicast (239.255.0.1:7400), there is no separate daemon."
echo "If LAN multicast is blocked, add Environment=WDNDDS_INITIAL_PEERS=ip1,ip2,... to the systemd unit"
echo ""
echo "TLS: 8000 只提供 HTTPS，证书在 $TLS_DIR"
echo "  客户端需信任 CA 根：$TLS_DIR/ca.pem"
echo "  证书缺失时服务**不会启动**（ConditionPathExists）—— 先跑 scripts/gen_tls_ca.sh"
