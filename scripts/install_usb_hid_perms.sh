#!/usr/bin/env bash
# One-shot: install udev rules for /dev/hidgN and fix permissions so
# the wdnwebui service user (rockchip) can write HID reports.
# Idempotent: safe to re-run.
set -euo pipefail

_src="${BASH_SOURCE[0]}"
while [ -L "${_src}" ]; do
  _dir="$(cd -P "$(dirname "${_src}")" && pwd)"
  _src="$(readlink "${_src}")"
  case "${_src}" in
    /*) ;;
    *) _src="${_dir}/${_src}" ;;
  esac
done
SCRIPT_DIR="$(cd -P "$(dirname "${_src}")" && pwd)"
RULES_SRC="${SCRIPT_DIR}/99-wdnwebui-usb-hid-gadget.rules"
RULES_DST="/etc/udev/rules.d/99-wdnwebui-usb-hid-gadget.rules"
GROUP="${WDNWEBUI_USB_HID_GID:-rockchip}"
MODE="0660"

[[ $EUID -eq 0 ]] || { echo "must run as root" >&2; exit 1; }

if ! getent group "${GROUP}" >/dev/null; then
    echo "group ${GROUP} does not exist; creating it" >&2
    groupadd "${GROUP}"
fi

install -m 0644 "${RULES_SRC}" "${RULES_DST}"
echo "installed ${RULES_DST}"

udevadm control --reload-rules
udevadm trigger --name-match=hidg0 --name-match=hidg1
udevadm settle -t 2 || true

# Some kernels don't apply the rule to already-existing chardevs; chmod
# explicitly so the running device is usable right now.
for dev in /dev/hidg0 /dev/hidg1; do
    [[ -e "${dev}" ]] || continue
    chgrp "${GROUP}" "${dev}"
    chmod "${MODE}" "${dev}"
done

ls -l /dev/hidg0 /dev/hidg1
