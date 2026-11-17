#!/usr/bin/env python3
"""回归防护：监控反查 `/play` 的入队 / 窗口尺寸 / 日志护栏。

2026-11-17 真机实测：客户端消费不过来时，旧写法
``loop.call_soon_threadsafe(q.put_nowait, frame)`` 会在事件循环回调里
抛 ``asyncio.QueueFull``；asyncio 默认异常处理器把
``<Queue ... _queue=[b'\\xff\\xd8...']>`` 整段 repr 写进日志 —— 一个
16 槽的 JPEG 队列约 5MB，实测几十秒刷出 665MB，事件循环被拖死。

本脚本钉住：
1. 入队满时**丢最旧、保最新**，异常绝不到 asyncio 异常处理器；
2. eof/error 这类终态 JSON 不会因为队列满而丢失；
3. 窗口尺寸收敛 + 按源宽高比 contain 且**不放大**；
4. 回放帧上限按片段时长（上限 3000 帧）；
5. 日志护栏会把超长记录截断。

不占 NPU。

用法::

    python -m scripts.verify_vr_play_queue
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import os
import sys

sys.path.insert(0, ".")

from scripts.card_dirs import resolve_cards

from app.core.log_guard import TruncateLongRecords
from app.core.video.search import _fit_output_size, iter_segment_jpeg

_CARD = next(c for c in resolve_cards() if c["id"] == "video-retrieval")
api = importlib.import_module(f"{_CARD['ns']}.{_CARD['mod']}.api")

SRC = "/userdata/videos/分场景测试视频/教室上课/1744338600_student_1080p.mp4"

fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("[PASS] " if cond else "[FAIL] ") + name
          + (f" — {detail}" if detail else ""))
    if not cond:
        fails.append(name)


def test_enqueue() -> None:
    q: asyncio.Queue = asyncio.Queue(maxsize=2)
    q.put_nowait(b"a")
    q.put_nowait(b"b")
    api._enqueue_latest(q, b"c")
    got = [q.get_nowait(), q.get_nowait()]
    check("[1] 队列满时丢最旧、保最新", got == [b"b", b"c"], str(got))

    q2: asyncio.Queue = asyncio.Queue(maxsize=2)
    q2.put_nowait(b"a")
    q2.put_nowait(b"b")
    api._enqueue_latest(q2, ("json", {"type": "eof"}))
    got2 = [q2.get_nowait(), q2.get_nowait()]
    check("[2] 终态 JSON 不会被丢弃",
          got2[-1] == ("json", {"type": "eof"}), str(got2[0]))


async def _callback_no_error() -> list:
    loop = asyncio.get_running_loop()
    errors: list = []
    old = loop.get_exception_handler()
    loop.set_exception_handler(lambda _l, ctx: errors.append(ctx))
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    q.put_nowait(b"old")
    loop.call_soon_threadsafe(api._enqueue_latest, q, b"new")
    await asyncio.sleep(0.05)
    loop.set_exception_handler(old)
    check("[3] QueueFull 不会逃到事件循环异常处理器", not errors, str(errors))
    check("[3b] 回调执行后保留最新帧", q.get_nowait() == b"new", "")
    return errors


def test_dims() -> None:
    check("[4] 窗口尺寸合法性收敛",
          api._clamp_play_dim(960) == 960
          and api._clamp_play_dim("abc") == 0
          and api._clamp_play_dim(2) == 0
          and api._clamp_play_dim(99999) == 0
          and api._clamp_play_dim(0) == 0,
          "")

    out = _fit_output_size(1920, 1080, 1286, 724)
    check("[5] 1920x1080 → 1286x724 窗口（contain）",
          out is not None and out[0] <= 1286 and out[1] <= 724
          and abs(out[0] / out[1] - 1920 / 1080) < 0.02, str(out))

    out = _fit_output_size(1920, 1080, 2560, 1440)
    check("[6] 窗口比源大时不放大", out == (1920, 1080), str(out))

    out = _fit_output_size(1920, 1080, 800, 800)
    check("[7] 方形窗口按源宽高比 contain", out == (800, 450), str(out))

    check("[8] 客户端没给尺寸 → 回退源分辨率",
          _fit_output_size(1920, 1080, 0, 0) is None, "")

    check("[9] 单次回放上限 3000 帧", api.PLAY_MAX_FRAMES == 3000,
          str(api.PLAY_MAX_FRAMES))


def test_log_guard() -> None:
    logging.getLogger("log_guard").addHandler(logging.NullHandler())
    logging.getLogger("log_guard").propagate = False
    guard = TruncateLongRecords(max_chars=4096)
    rec = logging.LogRecord(
        "asyncio", logging.ERROR, "queue.py", 42,
        "%s", (b"\xff\xd8" + b"\x00" * 200000,), None)
    guard.filter(rec)
    msg = rec.getMessage()
    check("[10] 超长日志被截断", len(msg) < 4300 and "truncated" in msg,
          f"len={len(msg)}")

    rec2 = logging.LogRecord("x", logging.INFO, "x.py", 1, "ok", (), None)
    guard.filter(rec2)
    check("[11] 正常日志不被改写", rec2.getMessage() == "ok",
          rec2.getMessage())


def test_real_frame() -> None:
    if not os.path.isfile(SRC):
        print(f"[SKIP] 找不到素材 {SRC}")
        return
    import numpy as np
    import cv2

    one = list(iter_segment_jpeg(
        SRC, 100.0, 101.0, max_frames=1,
        out_size_provider=lambda: (640, 360)))
    img = cv2.imdecode(np.frombuffer(one[0], np.uint8), cv2.IMREAD_COLOR) \
        if one else None
    check("[12] 按窗口尺寸编码（640x360）",
          img is not None and img.shape[1] == 640 and img.shape[0] == 360,
          str(None if img is None else img.shape))

    raw = list(iter_segment_jpeg(SRC, 100.0, 101.0, max_frames=1))
    img2 = cv2.imdecode(np.frombuffer(raw[0], np.uint8), cv2.IMREAD_COLOR) \
        if raw else None
    check("[13] 没给窗口尺寸时保持源分辨率",
          img2 is not None and img2.shape[1] == 1920 and img2.shape[0] == 1080,
          str(None if img2 is None else img2.shape))


async def main() -> int:
    test_enqueue()
    await _callback_no_error()
    test_dims()
    test_log_guard()
    test_real_frame()
    if fails:
        print(f"\nFAILED: {len(fails)} 项 — {', '.join(fails)}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
