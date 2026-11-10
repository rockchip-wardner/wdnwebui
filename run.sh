#!/usr/bin/env bash
# Bring up the USB HID gadget before wdnwebui starts.
#
# The setup is idempotent — if /sys/kernel/config/usb_gadget/g1 is
# already configured and bound to fc000000.usb, this is a near-no-op
# (a few stat() calls + a status check, ~10 ms). Re-running on every
# wdnwebui boot avoids the "reboot and forget to run setup again"
# class of failure that has bitten the 智能电脑控制 (smart computer
# control) card before.
#
# `sudo -n` is the non-interactive mode: if the NOPASSWD rule in
# /etc/sudoers.d/wdnwebui-usb-hid is in place it runs without a
# password prompt; if it isn't, we print a warning and let the web UI
# still come up. The card will surface the "Gadget 未就绪" (gadget not
# ready) state in the page header instead of silently disappearing — see
# the banner reason list in app/apps/usb_hid/static/app.js's
# applySnapshot().
#
# Why not just chmod 0660 the whole /sys/kernel/config/usb_gadget?
#   configfs write access is enforced by the kernel, not the
#   filesystem — even a 0777 dir will still refuse non-root writes,
#   so this script must reach root for the bind step. The sudoers
#   rule is the minimum surface that achieves that.
#
# Paths are derived from this script's own location, NOT hardcoded to
# /home/rockchip/wdnwebui — the repo may be installed anywhere
# (/opt/wdnwebui, /usr/local/wdnwebui, a user's home, …). A baked-in
# path makes the whole script fail on every machine but this one.
#
# NOTE: the sudoers rule in /etc/sudoers.d/wdnwebui-usb-hid pins the
# *absolute* path of the setup script. If the repo is moved, that
# rule must be updated to the new path too — otherwise `sudo -n`
# fails and the HID gadget silently stays unconfigured.
#
# Resolve the real script location, following symlinks: a wrapper
# symlinked into /usr/local/bin must still find the repo it belongs
# to, not look for scripts/ next to the symlink.
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
HID_SETUP="${SCRIPT_DIR}/scripts/setup_usb_hid_gadget.sh"

# Run from the repo root so relative paths (config.jsonc, static
# assets, the .venv) resolve the same way they always have.
cd "${SCRIPT_DIR}"

sudo -n "${HID_SETUP}" \
  || echo "[run.sh] warning: USB HID gadget setup skipped — sudo NOPASSWD for ${HID_SETUP} not configured; /api/usb-hid will report 'absent' until setup runs" >&2

"${SCRIPT_DIR}/.venv/bin/python" "${SCRIPT_DIR}/run.py" "$@"
