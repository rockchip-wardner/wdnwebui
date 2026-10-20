# USB HID 模拟卡片 — 部署与使用

把 RK3588 当成一块 USB HID 设备通过 OTG 上报给 PC。包含两个独立 HID function：

- **Boot Keyboard** (`/dev/hidg0`)：6KRO 键盘，US 布局
- **Digitizer / Tablet** (`/dev/hidg1`)：单指绝对坐标 pointer，逻辑坐标 0..32767

## 一次性配置（root 权限）

```bash
# 1. 加载 configfs + 写 HID gadget + bind OTG UDC
sudo bash /home/rockchip/wdnwebui/scripts/setup_usb_hid_gadget.sh

# 2. 装 udev 规则 + 改 /dev/hidgN 权限给 wdnwebui 用户
sudo cp /home/rockchip/wdnwebui/scripts/99-wdnwebui-usb-hid-gadget.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger --name-match=hidg0 --name-match=hidg1
sudo chmod 660 /dev/hidg0 /dev/hidg1
sudo chgrp rockchip /dev/hidg0 /dev/hidg1
ls -l /dev/hidg0 /dev/hidg1    # 期望: crw-rw---- root rockchip
```

PC 端验证：设备管理器里应该出现两个新 HID 设备（HID Keyboard Device + HID-compliant digitizer）。

如果没出现，把 Type-C 线拔了重插一次。

## 撤销

```bash
sudo bash /home/rockchip/wdnwebui/scripts/teardown_usb_hid_gadget.sh
```

## 端点

| Method | Path | 用途 |
|---|---|---|
| GET  | `/api/usb-hid/` | gadget 状态快照（用于页头 traffic light） |
| POST | `/api/usb-hid/keyboard/type` | `{text, hold_ms?}` 逐字输入 ASCII |
| POST | `/api/usb-hid/keyboard/tap` | `{key, keycode?, modifier?}` 单键按下/释放 |
| POST | `/api/usb-hid/keyboard/hotkey` | `{keys:[…], modifier?}` 组合键 |
| POST | `/api/usb-hid/keyboard/release` | 清空所有键（panic-stop） |
| POST | `/api/usb-hid/digitizer/move` | `{x, y}` 屏幕像素坐标 |
| POST | `/api/usb-hid/digitizer/click` | `{x, y, press_ms?}` tip-down + tip-up |
| POST | `/api/usb-hid/digitizer/leave` | 抬笔 (out-of-range) |
| POST | `/api/usb-hid/desk` | `{width, height}` 记录屏幕大小 |
| POST | `/api/usb-hid/reset` | 同时释放键 + 抬笔 |

## 验证

```bash
python3 /home/rockchip/wdnwebui/scripts/verify_usb_hid.py
```

期望输出（udev 权限已设的情况下）：12 行全 `[OK]`，最后 `all checks passed ✓`。

## 文件清单

| 文件 | 作用 |
|---|---|
| `app/core/usb_gadget/__init__.py` | 包 docstring |
| `app/core/usb_gadget/keycodes.py` | USB HID keycode 表 + modifier 解析 + char/keyname→keycode |
| `app/core/usb_gadget/hid_descriptors.py` | 键盘 + digitizer HID report descriptors |
| `app/core/usb_gadget/manager.py` | configfs 写入器 + UDC 绑定 + `/dev/hidgN` FD 缓存 |
| `app/apps/usb_hid/manifest.json` | 卡片 manifest |
| `app/apps/usb_hid/__init__.py` | register_api / register_shutdown |
| `app/apps/usb_hid/api.py` | FastAPI router（10 个端点） |
| `app/apps/usb_hid/service.py` | KeyboardWriter / DigitizerWriter |
| `app/apps/usb_hid/static/template.html` | 页面 DOM |
| `app/apps/usb_hid/static/app.js` | 前端逻辑（无 WS） |
| `app/apps/usb_hid/static/app.css` | 页面样式 + digitizer canvas |
| `app/apps/usb_hid/static/wizard.json` | 极简向导（仅检查 gadget 是否就绪） |
| `scripts/setup_usb_hid_gadget.sh` | 一次性 configfs + UDC bind |
| `scripts/teardown_usb_hid_gadget.sh` | 对应拆除 |
| `scripts/99-wdnwebui-usb-hid-gadget.rules` | udev 规则 |
| `scripts/verify_usb_hid.py` | 12 项端到端校验 |
| `config.jsonc` (`usb_hid_gadget` section) | 默认配置（autobind=false） |
