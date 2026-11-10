#!/usr/bin/env python3
"""回归：WebSocket 客户端断开后，服务端 handler **必须**收尾。

背景（v0.43）
------------
WS handler 只要「只写不读」或「读不到断开」，就会**永久占住一个 ASGI
task**：客户端每次断开都漏一个 handler，而 uvicorn 关停时要等
``server_state.tasks`` 清空 ⇒ 停机卡死 ⇒ systemd SIGKILL 整个 cgroup
⇒ compute_worker 连坐被杀。

修法（三个仓）：

* ``wdnwebui`` 的 ``system-logs/ws`` —— 读一行 × 等断开的竞速；
* ``wdnapps-video-analyzer`` 的 ``audio_analysis/ws`` —— 只读 watcher +
  FIRST_COMPLETED 竞速；
* ``wdnapps-audio-chat`` 的 ``tts/ws`` —— 推送协程与只读协程竞速；
* 本仓的 ``qwen3-asr/ws`` —— reader task（本来就对，加了关闭日志）。

本脚本对**每个端点 × 每种断开方式**断言「服务端的关闭标志出现了」。
断开方式覆盖三种真实场景：

============  =====================================================
``normal``    客户端正常关闭（发 close frame）
``sigkill``   客户端进程被 **SIGKILL** —— 模拟浏览器标签被杀
``relay``     经 TLS relay(8443) 连接 —— 局域网浏览器的真实路径
============  =====================================================

用法::

    .venv/bin/python scripts/verify_ws_disconnect.py
    .venv/bin/python scripts/verify_ws_disconnect.py --base http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import socket
import ssl
import subprocess
import sys
import time
from datetime import datetime

import requests
import websockets

# 端点 → (服务端关闭标志的正则, 是否伴随子进程需要清理)
ENDPOINTS = {
    "/api/system-logs/ws": (r"system-logs ws closed", True),
    "/api/audio-analysis/ws": (r"audio_analysis ws closed", False),
    "/api/tts/ws": (r"ws\[\d+\] closed", False),
    "/api/qwen3-asr/ws": (r"qwen3_asr ws closed", False),
}

RELAY_PORT = 8443
NORMAL = "normal"
SIGKILL = "sigkill"
RELAY = "relay"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def journalctl_count(pattern: str, since: str) -> int:
    """Count matching lines in the service journal since ``since``."""
    out = subprocess.run(
        ["journalctl", "-u", "wdnwebui", "--since", since, "--no-pager"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        return -1
    return len(re.findall(pattern, out.stdout))


def follower_processes() -> int:
    """How many ``journalctl`` followers are alive (the log tail spawns one)."""
    out = subprocess.run(["ps", "-eo", "comm"], capture_output=True, text=True).stdout
    return sum(1 for line in out.splitlines() if line.strip() == "journalctl")


def relay_available() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", RELAY_PORT), timeout=2):
            return True
    except OSError:
        return False


async def _first_frame(ws) -> None:
    """Read the first frame (some endpoints push a snapshot on connect)."""
    try:
        await asyncio.wait_for(ws.recv(), timeout=15)
    except Exception:  # noqa: BLE001
        pass


async def _close_normal(base: str, path: str) -> None:
    url = "ws://" + base.split("://", 1)[-1] + path
    async with websockets.connect(url, open_timeout=15) as ws:
        try:
            await _first_frame(ws)
        except Exception:  # noqa: BLE001
            pass


async def _close_via_relay(path: str) -> None:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    url = "wss://127.0.0.1:%d%s" % (RELAY_PORT, path)
    async with websockets.connect(url, open_timeout=15, ssl=ctx) as ws:
        try:
            await _first_frame(ws)
        except Exception:  # noqa: BLE001
            pass


def _close_sigkill(base: str, path: str) -> None:
    """Connect from a child process, then SIGKILL it (no close frame)."""
    url = "ws://" + base.split("://", 1)[-1] + path
    code = (
        "import asyncio,websockets\n"
        "async def m():\n"
        "    async with websockets.connect(%r, open_timeout=15) as ws:\n"
        "        await asyncio.sleep(300)\n"
        "asyncio.run(m())\n" % url
    )
    child = subprocess.Popen([sys.executable, "-c", code],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    time.sleep(4.0)
    os.kill(child.pid, 9)


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--wait", type=float, default=6.0,
                    help="seconds to wait after the disconnect before judging")
    args = ap.parse_args(argv)
    base = args.base.rstrip("/")

    try:
        requests.get(base + "/api/compute", timeout=5).raise_for_status()
    except Exception as e:  # noqa: BLE001
        print("服务不可达: %s (%s)" % (base, e))
        return 2

    have_relay = relay_available()
    print("服务: %s    TLS relay(8443): %s" % (base, "有" if have_relay else "无（跳过 relay 用例）"))
    print()

    total = failed = skipped = 0
    for path, (pattern, has_follower) in ENDPOINTS.items():
        for mode in (NORMAL, SIGKILL, RELAY):
            if mode == RELAY and not have_relay:
                skipped += 1
                print("  SKIP  %-28s %-8s （relay 未运行）" % (path, mode))
                continue

            since = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            base_log = journalctl_count(pattern, since)
            base_proc = follower_processes() if has_follower else -1

            try:
                if mode == NORMAL:
                    asyncio.run(_close_normal(base, path))
                elif mode == SIGKILL:
                    _close_sigkill(base, path)
                else:
                    asyncio.run(_close_via_relay(path))
            except Exception as e:  # noqa: BLE001
                print("  FAIL  %-28s %-8s 连接过程异常 %s" % (path, mode, e))
                failed += 1
                total += 1
                continue

            time.sleep(args.wait)
            new_log = journalctl_count(pattern, since)
            new_proc = follower_processes() if has_follower else -1

            total += 1
            ok = new_log > base_log
            detail = "关闭标志 %d→%d" % (base_log, new_log)
            if has_follower:
                proc_ok = new_proc <= base_proc
                detail += ", 跟随进程 %d→%d" % (base_proc, new_proc)
                ok = ok and proc_ok
            print(("  PASS  " if ok else "  FAIL  ")
                  + "%-28s %-8s %s" % (path, mode, detail))
            if not ok:
                failed += 1

    print()
    print("结果: %d 条断言, %d 失败, %d 跳过" % (total, failed, skipped))
    if failed:
        print("失败项 —— handler 没有在客户端断开后收尾")
        return 1
    print("WS 断开收尾回归全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
