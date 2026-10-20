#!/usr/bin/env bash
# Teardown the USB HID gadget (unbind UDC + remove configfs tree).
# Symmetric to setup_usb_hid_gadget.sh.
set -euo pipefail

GADGET_DIR="${GADGET_DIR:-/sys/kernel/config/usb_gadget/g1}"
[[ $EUID -eq 0 ]] || { echo "must run as root" >&2; exit 1; }

if [[ ! -d "${GADGET_DIR}" ]]; then
    echo "[teardown] nothing to do (no gadget at ${GADGET_DIR})"
    exit 0
fi

echo "" > "${GADGET_DIR}/UDC" 2>/dev/null || true
rm -f "${GADGET_DIR}/configs/c.1/hid.usb0" \
      "${GADGET_DIR}/configs/c.1/hid.usb1" || true
rmdir "${GADGET_DIR}/configs/c.1/strings/0x409" || true
rmdir "${GADGET_DIR}/configs/c.1" || true
rmdir "${GADGET_DIR}/configs" || true
rmdir "${GADGET_DIR}/functions/hid.usb1" || true
rmdir "${GADGET_DIR}/functions/hid.usb0" || true
rmdir "${GADGET_DIR}/functions" || true
rmdir "${GADGET_DIR}/strings/0x409" || true
rmdir "${GADGET_DIR}/strings" || true
rmdir "${GADGET_DIR}" || true

echo "[teardown] ${GADGET_DIR} removed"
