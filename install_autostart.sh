#!/usr/bin/env bash
# 安装 wdnwebui 的 systemd 开机自启单元
# 用法：sudo ./install_autostart.sh [--uninstall]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$SCRIPT_DIR/.venv/bin/python"

WEB_UNIT="/etc/systemd/system/wdnwebui.service"

UNINSTALL=false

for arg in "$@"; do
    case "$arg" in
        --uninstall) UNINSTALL=true ;;
        -h|--help)
            sed -n '2,3p' "$0"
            exit 0
            ;;
        *)
            echo "未知参数: $arg" >&2
            sed -n '2,3p' "$0" >&2
            exit 2
            ;;
    esac
done

if [ "$EUID" -ne 0 ]; then
    echo "需要 root 权限：请用 sudo 运行，例：sudo $0" >&2
    exit 1
fi

SERVICE_USER="${SUDO_USER:-$(whoami)}"

if [ ! -x "$PY" ]; then
    echo "找不到 $PY，请先激活 venv 或完成依赖安装" >&2
    exit 1
fi

uninstall() {
    local changed=0
    if [ -f "$WEB_UNIT" ]; then
        unit_name="$(basename "$WEB_UNIT")"
        systemctl disable --now "$unit_name" 2>/dev/null || true
        rm -f "$WEB_UNIT"
        echo "已移除 $WEB_UNIT"
        changed=1
    fi
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

if [ "$UNINSTALL" = true ]; then
    uninstall
    exit 0
fi

write_web_unit

echo "完成。可手动 systemctl start wdnwebui.service 立即拉起。"
echo "v0.6+：发现走 FastDDS SIMPLE multicast (239.255.0.1:7400)，无独立 daemon。"
echo "若 LAN 组播被阻断，在 systemd unit 里加 Environment=WDNDDS_INITIAL_PEERS=ip1,ip2,..."