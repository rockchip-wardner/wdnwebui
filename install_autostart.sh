#!/usr/bin/env bash
# 安装 wdnwebui 的 systemd 开机自启单元（可选：wdndds_daemon）
# 用法：sudo ./install_autostart.sh [--with-daemon] [--uninstall]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$SCRIPT_DIR/.venv/bin/python"

WEB_UNIT="/etc/systemd/system/wdnwebui.service"
DAEMON_UNIT="/etc/systemd/system/wdndds-daemon.service"

INSTALL_WEB=true
INSTALL_DAEMON=false
UNINSTALL=false

for arg in "$@"; do
    case "$arg" in
        --with-daemon) INSTALL_DAEMON=true ;;
        --daemon-only) INSTALL_WEB=false; INSTALL_DAEMON=true ;;
        --uninstall) UNINSTALL=true ;;
        -h|--help)
            sed -n '2,4p' "$0"
            exit 0
            ;;
        *)
            echo "未知参数: $arg" >&2
            sed -n '2,4p' "$0" >&2
            exit 2
            ;;
    esac
done

if [ "$EUID" -ne 0 ]; then
    echo "需要 root 权限：请用 sudo 运行，例：sudo $0 --with-daemon" >&2
    exit 1
fi

SERVICE_USER="${SUDO_USER:-$(whoami)}"

if [ ! -x "$PY" ]; then
    echo "找不到 $PY，请先激活 venv 或完成依赖安装" >&2
    exit 1
fi

uninstall() {
    local changed=0
    for unit in "$WEB_UNIT" "$DAEMON_UNIT"; do
        if [ -f "$unit" ]; then
            unit_name="$(basename "$unit")"
            systemctl disable --now "$unit_name" 2>/dev/null || true
            rm -f "$unit"
            echo "已移除 $unit"
            changed=1
        fi
    done
    if [ "$changed" -eq 1 ]; then
        systemctl daemon-reload
    fi
    echo "卸载完成"
}

write_web_unit() {
    cat > "$WEB_UNIT" <<EOF
[Unit]
Description=wdnwebui service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$SCRIPT_DIR
Environment=WDNWEBUI_CONFIG=$SCRIPT_DIR/config.json
ExecStart=$SCRIPT_DIR/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable wdnwebui.service
    echo "已写入 $WEB_UNIT（已 enable，未启动）"
}

write_daemon_unit() {
    cat > "$DAEMON_UNIT" <<EOF
[Unit]
Description=wdndds FastDDS discovery daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$SCRIPT_DIR
ExecStart=$PY -m app.services.wdndds_daemon --udp-port 7411 --http-port 7412
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable wdndds-daemon.service
    echo "已写入 $DAEMON_UNIT（已 enable，未启动）"
}

if [ "$UNINSTALL" = true ]; then
    uninstall
    exit 0
fi

if [ "$INSTALL_WEB" = true ]; then
    write_web_unit
fi

if [ "$INSTALL_DAEMON" = true ]; then
    write_daemon_unit
fi

echo "完成。可手动 systemctl start wdnwebui.service 立即拉起。"