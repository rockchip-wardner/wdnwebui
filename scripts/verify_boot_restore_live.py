#!/usr/bin/env python3
"""真机验证：开机自启恢复的**完整链路**（占 NPU，会重启服务）。

覆盖 v0.27 的真机诉求：重启后 autostart 把模型拉起来 → 卡片自动进 →
「启动检测」按钮转圈显示"正在等待模型加载" → 模型就绪后**自动开始检测**，
并且视频源自动重开。

跑法：
    .venv/bin/python scripts/verify_boot_restore_live.py

⚠️ 这个脚本会**重启 wdnwebui 服务**（杀掉旧的 run.py 进程组 + compute_worker，
再用 run.py 拉起来）。别的浏览器标签页会被断开。

为什么必须真机：模型加载窗口（~40s）没法在单测里造出来 —— 它的全部意义
就是"模型还没好的时候用户看到什么"。纯逻辑测试（verify_boot_restore.py）
只能保证状态机正确，保证不了"转圈真的转了、源真的开了、检测真的自己起来了"。

判据：
  1. 加载窗口内：按钮带 ``is-boot-wait`` class + 文案含"等待模型加载"；
  2. 就绪后：detector ``enabled=true`` 且 ``session_ready=true``；
  3. 视频源自动重开：``channel_list`` 非空。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get("WDNWEBUI_BASE", "http://127.0.0.1:8000")
VIDEO = os.environ.get("WDNWEBUI_TEST_VIDEO",
                       "/home/rockchip/samples/data/1.mp4")
APP_ID = "video-detection"

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f"  ({detail})" if detail else ""))


def api(path: str, timeout: float = 20.0):
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"_error": str(e)}


def kill_web() -> None:
    """杀掉 run.py 进程组 + 独立 session 的 compute_worker。

    worker 是 registry spawn 时 setsid 出来的（PGID == 自己 PID），所以
    ``kill -- -<run.py 的 pgid>`` 打不到它们 —— 必须按各自的 PGID 单独杀。
    漏掉的后果是下次启动时 registry 的 cmdline 去重命中旧 worker、跳过 spawn，
    表现为「功能未启用」。
    """
    out = subprocess.run(["ps", "-eo", "pid,pgid,cmd"], capture_output=True,
                         text=True).stdout
    groups: set[str] = set()
    for line in out.splitlines():
        if "run.py" in line and "grep" not in line:
            parts = line.split(None, 2)
            if len(parts) >= 3:
                groups.add(parts[1])
        if "compute_worker.py" in line and "grep" not in line:
            parts = line.split(None, 2)
            if len(parts) >= 3:
                groups.add(parts[1])
    for g in groups:
        subprocess.run(["kill", "-KILL", f"-{g}"], capture_output=True)
    time.sleep(1.5)


def start_web(log_path: str):
    log = open(log_path, "wb")
    # 必须用 run.py（不是 uvicorn app.main:app）：run.py 的
    # _ensure_wdndds_native_path() 负责把 wdndds 原生库目录加进
    # LD_LIBRARY_PATH，缺了 worker 加载不了 cshim 会秒退。
    subprocess.Popen([".venv/bin/python", "run.py"], cwd=ROOT,
                     stdout=log, stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, start_new_session=True)
    for _ in range(120):
        time.sleep(1)
        try:
            with urllib.request.urlopen(BASE + "/api/compute/active-app",
                                        timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            continue
    return False


def main() -> int:
    from playwright.sync_api import sync_playwright

    # 素材必须是**真视频**：仓库里 /home/rockchip/assets/uploads/sample.mp4
    # 只有 48 字节，是个占位文件。用它跑会得到一个"通道建成了但 has_frame=False"
    # 的假绿 —— 通道数非空不代表解码成功。所以先验大小。
    try:
        size = os.path.getsize(VIDEO)
    except OSError as e:
        print(f"  ❌ 素材不存在: {VIDEO} ({e})")
        return 2
    if size < 100_000:
        print(f"  ❌ 素材太小（{size} 字节），多半是占位文件而非真视频: {VIDEO}")
        print("     换一个有画面的素材，或设 WDNWEBUI_TEST_VIDEO 指向真文件。")
        return 2

    print("\n【0】准备：重启服务（模型要重新加载，才能看到加载窗口）")
    kill_web()
    log_path = "/tmp/wdn_boot_restore_live.log"
    if not start_web(log_path):
        print(f"  ❌ 服务没起来，看 {log_path}")
        return 2
    print("  ✅ 服务已起（模型正在加载）")

    saved = {
        "sources": [VIDEO],
        "prompt": "用中文一句话描述画面中的场景、主体（人/物）、动作和环境。",
        "loop": True,
        "every_n": "30",
        "saved_at": int(time.time() * 1000),
    }

    print("\n【1】打开页面（预置 localStorage 存档），观察加载窗口")
    saw_spin = False
    spin_text = ""
    auto_started = False
    detail = ""

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        # 预置存档 —— 必须在页面脚本跑之前写好，否则 begin() 读不到。
        # Playwright 的 add_init_script 不接受参数，payload 直接内联进脚本文本
        # （JSON.stringify 之前先 json.dumps 成合法 JSON 字面量）。
        page.add_init_script(
            "try { localStorage.setItem('wdn.lastrun.video-detection', "
            + json.dumps(json.dumps(saved, ensure_ascii=False))
            + "); } catch (e) {}")
        page.goto(BASE + "/", wait_until="domcontentloaded")

        deadline = time.time() + 150
        while time.time() < deadline:
            state = page.evaluate("""() => {
              const btn = document.getElementById('vd-detector-start');
              const st  = document.getElementById('vd-detector-status');
              return {
                hash: location.hash,
                spinning: !!(btn && btn.classList.contains('is-boot-wait')),
                text: btn ? (btn.textContent || '').trim() : '',
                status: st ? (st.textContent || '').trim() : '',
                disabled: btn ? btn.disabled : null,
              };
            }""")
            if state["spinning"]:
                saw_spin = True
                spin_text = state["text"]
                detail = state["status"]
                print(f"     转圈中：按钮='{state['text']}' 状态='{state['status']}' "
                      f"禁用={state['disabled']}")
            # 等自动启动：按钮不再转圈 且 后端 enabled
            if saw_spin and not state["spinning"]:
                snap = api("/api/video-detection/events")
                s = snap.get("snapshot") or {}
                if s.get("enabled"):
                    auto_started = True
                    break
            time.sleep(1.0)

        # 兜底：再等一会儿看后端有没有真的起来
        if not auto_started:
            for _ in range(40):
                snap = api("/api/video-detection/events")
                s = snap.get("snapshot") or {}
                if s.get("enabled") and s.get("session_ready"):
                    auto_started = True
                    break
                time.sleep(1.5)

        # 事件要等判据攒够帧（gscore 默认 every_n=600 兜底间隔），不是立刻就有 ——
        # 这里给一段等待，否则会在"刚启动、还没出事件"的瞬间断言，白红。
        evs = []
        for _ in range(40):
            evs = (api("/api/video-detection/events").get("events") or [])
            if evs:
                break
            time.sleep(1.5)

        sess = api("/api/video-detection/")
        final = sess.get("session") or {}
        channels = final.get("channel_list") or []
        # 断言要卡在"真的解出帧了"，而不是"通道建起来了" —— 通道数非空但
        # has_frame=False / frames=0 是最常见的假绿（素材坏了、解码失败都长这样）。
        got_frames = [c for c in channels if (c.get("frames") or 0) > 0]
        broken = [c for c in channels if c.get("state") == "error"]
        check("加载窗口内按钮转圈（is-boot-wait）", saw_spin, spin_text)
        check("转圈时文案是「正在等待模型加载…」",
              "等待模型加载" in spin_text or "等待模型加载" in detail,
              f"{spin_text} / {detail}")
        check("模型就绪后自动启动检测（enabled=true）", auto_started)
        check("视频源自动重开（channel_list 非空）",
              len(channels) > 0, f"{len(channels)} 路")
        check("通道真的解出帧了（frames>0，不是只建了个空壳）",
              len(got_frames) > 0,
              f"有帧通道 {len(got_frames)}/{len(channels)}"
              + (f"，报错: {broken[0].get('error')}" if broken else ""))
        check("检测真的产出了事件", len(evs) > 0, f"{len(evs)} 条")
        check("页面无 JS 报错", len(errors) == 0, " | ".join(errors[:3]))
        browser.close()

    print("\n" + "═" * 70)
    print(f"PASS={PASS}  FAIL={FAIL}")
    print("═" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
