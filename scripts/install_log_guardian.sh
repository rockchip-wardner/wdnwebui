#!/usr/bin/env bash
# Install the repo-owned disk guardian (scripts/log_guardian.py).
#
#   sudo bash scripts/install_log_guardian.sh [--uninstall]
#
# Why not patch /usr/bin/log-guardian
# ----------------------------------
# /usr/bin/log-guardian belongs to the OS image and has **no source copy in
# this repo**. A patch applied there is silently reverted by the next system
# upgrade — nobody notices until the disk is full and the guardian has been
# doing nothing for weeks (that is exactly the state this board was in:
# `journalctl -u log-guardian | grep -c Rotating` = 0 since boot).
#
# What this installs instead
# --------------------------
#   /usr/local/sbin/log-guardian          the implementation (stdlib only)
#   /etc/systemd/system/log-guardian.service   the unit
#
# Both paths are reserved for the local administrator by Debian policy, so a
# system upgrade / re-flash does **not** overwrite them — no re-patching.
#
# The unit keeps the vendor's name on purpose: systemd loads /etc over /lib,
# so the vendor unit is *shadowed*, not shadowed-and-also-running. We also
# re-point the enable symlink from /usr/lib/... to /etc/....
#
# The vendor script in /usr/bin is left untouched (it may still be patched);
# it is simply never executed any more.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SCRIPT_DIR/log_guardian.py"
DEST="/usr/local/sbin/log-guardian"
UNIT_PATH="/etc/systemd/system/log-guardian.service"
UNIT_NAME="log-guardian.service"

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
            exit 2
            ;;
    esac
done

if [ "$EUID" -ne 0 ]; then
    echo "root privileges are required: sudo bash scripts/install_log_guardian.sh" >&2
    exit 1
fi

if [ "$UNINSTALL" = true ]; then
    echo "== uninstalling =="
    systemctl stop "$UNIT_NAME" 2>/dev/null || true
    systemctl disable "$UNIT_NAME" 2>/dev/null || true
    rm -f "$UNIT_PATH" "$DEST"
    systemctl daemon-reload
    # 交还给系统那份：把 enable 软链指回 /usr/lib
    if [ -f /usr/lib/systemd/system/log-guardian.service ] ||
       [ -f /lib/systemd/system/log-guardian.service ]; then
        systemctl enable "$UNIT_NAME" 2>/dev/null || true
        systemctl start "$UNIT_NAME" 2>/dev/null || true
        echo "handed back to the vendor unit: $(systemctl is-active "$UNIT_NAME")"
    else
        echo "no vendor unit found — disk protection is now OFF"
    fi
    echo "done."
    exit 0
fi

if [ ! -f "$SRC" ]; then
    echo "ERROR: $SRC not found" >&2
    exit 2
fi

echo "== installing log-guardian (repo-owned) =="

install -d -m 755 /usr/local/sbin
install -m 755 "$SRC" "$DEST"
echo "installed: $DEST"

# --- the unit -----------------------------------------------------------------
# Type=simple + a foreground loop ⇒ systemd tracks the real MainPID
# (the vendor script backgrounds itself ⇒ MainPID=0 and `systemctl status`
# never showed the process that was actually working).
cat > "$UNIT_PATH" <<'EOF'
[Unit]
Description=Reclaim disk space by rotating/truncating stale log files (wdnwebui)
After=local-fs.target

[Service]
Type=simple
ExecStart=/usr/local/sbin/log-guardian
Restart=on-failure
RestartSec=30
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
EOF
echo "installed: $UNIT_PATH"

systemctl daemon-reload

# The enable symlink currently points at /usr/lib/systemd/system/... — re-point it
# at our /etc copy so the vendor unit is never the one that runs.
systemctl disable "$UNIT_NAME" >/dev/null 2>&1 || true
systemctl enable "$UNIT_NAME" >/dev/null
systemctl restart "$UNIT_NAME"

sleep 2
echo "active:   $(systemctl is-active "$UNIT_NAME")"
echo "enabled:  $(systemctl is-enabled "$UNIT_NAME")"
echo "MainPID:  $(systemctl show "$UNIT_NAME" -p MainPID --value)  (vendor unit gave 0)"
echo "exec:     $(systemctl show "$UNIT_NAME" -p ExecStart --value | head -c 120)"

if ! systemctl is-active --quiet "$UNIT_NAME"; then
    echo "ERROR: unit failed to start — journalctl -u $UNIT_NAME -n 30" >&2
    exit 1
fi

echo
echo "done.  Guarded dirs: /var/log (min-age 0) and /tmp (min-age 1h)."
echo "A system upgrade will NOT overwrite this install (/usr/local + /etc/systemd/system)."
