#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# USB HID Gadget — one-time configfs setup.
#
# Writes a configfs USB gadget with two HID functions (boot keyboard +
# digitizer / tablet) and binds it to the RK3588 OTG controller
# (fc000000.usb). Run this once per boot (or once, if you also install
# the bundled systemd unit below).
#
# Requires: kernel built with CONFIG_USB_CONFIGFS_F_HID=y. On RK3588's
# stock 6.12 kernel this is enabled by default. Verify with:
#   zcat /proc/config.gz | grep CONFIG_USB_CONFIGFS_F_HID
#
# Usage:  sudo bash scripts/setup_usb_hid_gadget.sh
# -----------------------------------------------------------------------------
set -euo pipefail

GADGET_NAME="${GADGET_NAME:-g1}"
GADGET_DIR="/sys/kernel/config/usb_gadget/${GADGET_NAME}"
CONFIGFS_MOUNT="/sys/kernel/config"
UDC_DIR="/sys/class/udc"

# ---- VID/PID overridable via env ------------------------------------------- #
VID="${VID:-0x1D6B}"          # Linux Foundation (avoids colliding with vendors)
PID="${PID:-0x0103}"          # Multifunction Composite Gadget
MANUFACTURER="${MANUFACTURER:-wdnwebui}"
PRODUCT="${PRODUCT:-USB HID Bridge}"
SERIAL="${SERIAL:-rk3588-hid-$(cat /proc/sys/kernel/random/boot_id 2>/dev/null | cut -c1-8 || echo '0001')}"

log() { printf "[usb-hid-setup] %s\n" "$*"; }
die() { printf "[usb-hid-setup] ERROR: %s\n" "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "must run as root (sudo bash $0)"

# ---- 0. configfs must be mounted ------------------------------------------- #
if ! mountpoint -q "${CONFIGFS_MOUNT}"; then
    log "configfs not mounted at ${CONFIGFS_MOUNT}; mounting now"
    mount -t configfs none "${CONFIGFS_MOUNT}"
fi

# ---- 1. pick UDC ---------------------------------------------------------- #
mapfile -t UDCS < <(ls -1 "${UDC_DIR}" 2>/dev/null)
[[ ${#UDCS[@]} -gt 0 ]] || die "no UDC in ${UDC_DIR} — is dwc3 loaded?"
UDC="${WDNWEBUI_USB_HID_UDC:-${UDCS[0]}}"
log "using UDC: ${UDC}"

# ---- 2. tear down stale state --------------------------------------------- #
if [[ -d "${GADGET_DIR}" ]]; then
    log "stale gadget at ${GADGET_DIR}, tearing down"
    echo "" > "${GADGET_DIR}/UDC" 2>/dev/null || true
    rm -f "${GADGET_DIR}/configs/c.1/hid.usb0" \
          "${GADGET_DIR}/configs/c.1/hid.usb1" 2>/dev/null || true
    rm -rf "${GADGET_DIR}/configs/c.1" \
           "${GADGET_DIR}/functions/hid.usb0" \
           "${GADGET_DIR}/functions/hid.usb1" \
           "${GADGET_DIR}/strings/0x409" \
           "${GADGET_DIR}" 2>/dev/null || true
fi

# ---- 3. write descriptors ------------------------------------------------- #
# (Descriptors are duplicated here from app/core/usb_gadget/hid_descriptors.py
#  to keep this script self-contained — no Python import needed. Keep them
#  in sync if you change one.)
KEYBOARD_DESC_HEX="05010906a101050719e029e715002600ff007508950102950108 075081010950067508150026ff000507190029 ff8100c0"
DIGITIZER_DESC_HEX="05010902a1010901a10005091901 2901150025019501750181020932093201810295 067501810105010930093116 0000270ff7f0000751095028102c0c0"

write_atomic() {
    local f="$1" v="$2"
    printf "%s" "$v" > "$f"
}

mkdir -p "${GADGET_DIR}"
write_atomic "${GADGET_DIR}/idVendor"             "0x${VID#0x}"
write_atomic "${GADGET_DIR}/idProduct"            "0x${PID#0x}"
write_atomic "${GADGET_DIR}/bcdUSB"               "0x0200"
write_atomic "${GADGET_DIR}/bcdDevice"            "0x0100"
write_atomic "${GADGET_DIR}/max_speed"            "high-speed"

mkdir -p "${GADGET_DIR}/strings/0x409"
write_atomic "${GADGET_DIR}/strings/0x409/serialnumber"   "${SERIAL}"
write_atomic "${GADGET_DIR}/strings/0x409/manufacturer"   "${MANUFACTURER}"
write_atomic "${GADGET_DIR}/strings/0x409/product"        "${PRODUCT}"

mkdir -p "${GADGET_DIR}/configs/c.1/strings/0x409"
write_atomic "${GADGET_DIR}/configs/c.1/MaxPower"          "250"
write_atomic "${GADGET_DIR}/configs/c.1/bmAttributes"      "0x80"
write_atomic "${GADGET_DIR}/configs/c.1/strings/0x409/configuration" "HID Keyboard + Tablet"

# ---- 4. create functions -------------------------------------------------- #
mkdir -p "${GADGET_DIR}/functions/hid.usb0"
write_atomic "${GADGET_DIR}/functions/hid.usb0/protocol"      "1"
write_atomic "${GADGET_DIR}/functions/hid.usb0/subclass"      "1"
write_atomic "${GADGET_DIR}/functions/hid.usb0/report_length" "8"
# Pull the keyboard report descriptor straight from the Python module
# (single source of truth). The descriptor is binary data with NUL
# bytes (e.g. Logical Minimum=0x00), so we MUST write it to a
# tempfile first then `cat` to configfs — `$(...)` substitution
# silently drops NUL bytes and would yield a truncated descriptor
# (which is what triggered the Win10 Status=Error bug originally).
SCRIPT_DIR_HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR_HERE}/.." && pwd)"
if [[ -f "${REPO_ROOT}/.venv/bin/python" ]]; then
    PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
else
    die "no python interpreter found (need .venv/bin/python or python3)"
fi
KB_DESC_TMP="$(mktemp /tmp/usb-hid-kb-desc.XXXXXX)"
trap 'rm -f "${KB_DESC_TMP}" "${DIG_DESC_TMP}"' EXIT
( cd "${REPO_ROOT}" && "${PYTHON_BIN}" -c '
import sys
sys.path.insert(0, ".")
from wdnhid.hid_descriptors import BOOT_KEYBOARD_REPORT_DESC
sys.stdout.buffer.write(BOOT_KEYBOARD_REPORT_DESC)
' > "${KB_DESC_TMP}" ) || die "failed to extract keyboard descriptor"
cat "${KB_DESC_TMP}" > "${GADGET_DIR}/functions/hid.usb0/report_desc"

mkdir -p "${GADGET_DIR}/functions/hid.usb1"
# Protocol = 3 (Digitizer). The HID 1.11 spec table for bInterfaceProtocol
# says 1=keyboard, 2=mouse, 3=digitizer. Writing "2" here made Win10/11
# load the HID Mouse class driver instead, which silently drops absolute
# X/Y reports — touch events appeared broken on the host even though
# /dev/hidg1 writes were succeeding. Three is the only value that lets
# Tablet PC Settings bind this device as a touchscreen.
write_atomic "${GADGET_DIR}/functions/hid.usb1/protocol"      "3"
write_atomic "${GADGET_DIR}/functions/hid.usb1/subclass"      "0"
# Report length must match DIGITIZER_TABLET_REPORT_DESC byte count
# exposed by wdnhid.hid_descriptors — currently 6 bytes (1 byte
# Contact Count + 1 byte Tip Switch/Contact ID/pad + 4 bytes X+Y).
# Reading the live value from the Python module keeps this script and
# the descriptor in lockstep; if you change one, change the other.
write_atomic "${GADGET_DIR}/functions/hid.usb1/report_length" "$(
    cd "${REPO_ROOT}" && "${PYTHON_BIN}" -c '
import sys
sys.path.insert(0, ".")
from wdnhid.hid_descriptors import DIGITIZER_TABLET_REPORT_LEN
sys.stdout.write(str(DIGITIZER_TABLET_REPORT_LEN))
'
)"
# Same trick for the digitizer descriptor.
DIG_DESC_TMP="$(mktemp /tmp/usb-hid-dig-desc.XXXXXX)"
( cd "${REPO_ROOT}" && "${PYTHON_BIN}" -c '
import sys
sys.path.insert(0, ".")
from wdnhid.hid_descriptors import DIGITIZER_TABLET_REPORT_DESC
sys.stdout.buffer.write(DIGITIZER_TABLET_REPORT_DESC)
' > "${DIG_DESC_TMP}" ) || die "failed to extract digitizer descriptor"
cat "${DIG_DESC_TMP}" > "${GADGET_DIR}/functions/hid.usb1/report_desc"

# ---- 5. link functions to config ------------------------------------------ #
ln -sf "${GADGET_DIR}/functions/hid.usb0" "${GADGET_DIR}/configs/c.1/hid.usb0"
ln -sf "${GADGET_DIR}/functions/hid.usb1" "${GADGET_DIR}/configs/c.1/hid.usb1"

# ---- 6. bind UDC ---------------------------------------------------------- #
write_atomic "${GADGET_DIR}/UDC" "${UDC}"

# ---- 7. udev nudge so /dev/hidgN comes up with sensible perms ------------- #
udevadm settle -t 2 || true
ls -l /dev/hidg0 /dev/hidg1 2>/dev/null || true

log "done. Check host device manager for two new HID devices:"
log "  - Boot Keyboard  (/dev/hidg0)"
log "  - Tablet/Digitizer (/dev/hidg1)"
log ""
log "to remove: sudo bash scripts/setup_usb_hid_gadget.sh --teardown"
