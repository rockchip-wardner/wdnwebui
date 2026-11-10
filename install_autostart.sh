#!/usr/bin/env bash
# Install the systemd autostart unit of wdnwebui
# Usage: sudo ./install_autostart.sh [--uninstall]

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
    if [ "$changed" -eq 1 ]; then
        systemctl daemon-reload
    fi
    echo "uninstall complete"
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
Environment=WDNWEBUI_CONFIG=$SCRIPT_DIR/config.jsonc
ExecStart=$SCRIPT_DIR/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=5

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

echo "done. Start it immediately with: systemctl start wdnwebui.service"
echo "v0.6+: discovery uses FastDDS SIMPLE multicast (239.255.0.1:7400), there is no separate daemon."
echo "If LAN multicast is blocked, add Environment=WDNDDS_INITIAL_PEERS=ip1,ip2,... to the systemd unit"
