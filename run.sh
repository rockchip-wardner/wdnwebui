#!/usr/bin/env bash
# Bring up the USB HID gadget before wdnwebui starts.
#
# The setup is idempotent — if /sys/kernel/config/usb_gadget/g1 is
# already configured and bound to fc000000.usb, this is a near-no-op
# (a few stat() calls + a status check, ~10 ms). Re-running on every
# wdnwebui boot avoids the "reboot and forget to run setup again"
# class of failure that has bitten the 智能电脑控制 card before.
#
# `sudo -n` is the non-interactive mode: if the NOPASSWD rule in
# /etc/sudoers.d/wdnwebui-usb-hid is in place it runs without a
# password prompt; if it isn't, we print a warning and let the web UI
# still come up. The card will surface "Gadget 未就绪" in the page
# header instead of silently disappearing — see the banner reason
# list in app/apps/usb_hid/static/app.js's applySnapshot().
#
# Why not just chmod 0660 the whole /sys/kernel/config/usb_gadget?
#   configfs write access is enforced by the kernel, not the
#   filesystem — even a 0777 dir will still refuse non-root writes,
#   so this script must reach root for the bind step. The sudoers
#   rule is the minimum surface that achieves that.
sudo -n /home/rockchip/wdnwebui/scripts/setup_usb_hid_gadget.sh \
  || echo "[run.sh] warning: USB HID gadget setup skipped — sudo NOPASSWD for ${0%/*}/setup_usb_hid_gadget.sh not configured; /api/usb-hid will report 'absent' until setup runs" >&2

.venv/bin/python run.py
