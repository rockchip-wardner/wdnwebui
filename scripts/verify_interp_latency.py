#!/usr/bin/env python3
"""回归：同传的**滞后口径**与**首窗优化**（2026-11-22 新增）。

背景
====
用户报「ASR+翻译都结束了，同声传译才播放出声音，显示滞后了 15 秒」。
分段耗时实测（热态）拆出两件事：

1. **面板的「滞后」在文件源下量错了** —— 基准取的是 ``created_ts``
   （**离线转写完成**的时刻），而文件源的句子是**一次性全部转写完**的
   ⇒ 所有句子的 ``created_ts`` 几乎相同 ⇒ 面板算出的滞后**随句子数
   单调增长**（实测 6.4 → 10.4 → 14.6 → 17.8 秒），而用户实际听到的
   错位只有几秒 ⇒ **面板在骗人**。
   ⇒ 修法：文件源改用**音频时间轴** —— 源话说完的时刻 =
   ``媒体零点 + src_end_sec``，媒体零点由播放头上报反推
   （``anchor = 上报时刻 − playhead_sec``）。

2. **TTS 首窗 3.0 s** —— 上游实现首个解码窗口硬等满 50 帧（4 s 音频）
   ⇒ 同传链路上最大的一段固定延迟（翻译推理只要 0.3 s）。
   ⇒ 修法：decoder 是**因果**的 ⇒ 首窗攒 25 帧即可开声 ⇒ 实测
   首窗 3.23 s → 2.02 s。

本脚本覆盖的契约
================
A. ``Gate.media_anchor()`` —— 媒体零点的反推与复位；
B. **文件源**的滞后基准 = ``媒体零点 + src_end_sec``（不是
   ``created_ts``）；**实时流**仍用 ``created_ts``；
C. 同传把 ``first_window_frames=25`` 传给 TTS 队列，队列原样带进
   ``TTSJob``；
D. 宿主桥 ``submit_queued`` 能把 ``first_window_frames`` 透传下去。

跑法::

    .venv/bin/python scripts/verify_interp_latency.py
"""
from __future__ import annotations

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from wdnapps_audio_chat.cards.qwen3_asr.interp import (  # noqa: E402
    INTERP_FIRST_WINDOW_FRAMES,
    Gate,
    InterpEngine,
    InterpSegment,
)

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ok   {name}")
    else:
        _FAIL += 1
        print(f"  FAIL {name} {extra}")


def main() -> int:
    print("== 滞后口径 ==")

    # ---- A. 媒体零点 ---------------------------------------------- #
    g = Gate("follow")
    check("没有播放头上报 ⇒ 媒体零点 = 0", g.media_anchor() == 0.0,
          f"得到 {g.media_anchor()}")

    t0 = time.time()
    g.update_playhead("file-1#0", 5.0, True)
    anchor = g.media_anchor()
    check("播放头 5.0 s ⇒ 媒体零点 ≈ 现在 − 5 s",
          abs(anchor - (t0 - 5.0)) < 1.0,
          f"得到 {anchor}（t0={t0}）")
    check("媒体零点早于现在", anchor <= time.time() + 0.01,
          f"得到 {anchor}")

    # ⚠️ 媒体**没在播**时不能更新零点（播放前播放头恒为 0 ⇒
    # 「现在 − 0 = 现在」⇒ 零点会一直往后滑）。
    before = g.media_anchor()
    g.update_playhead("file-1#0", 0.0, False)
    check("媒体暂停 ⇒ 媒体零点不动",
          abs(g.media_anchor() - before) < 0.001,
          f"{before} → {g.media_anchor()}")

    g.reset()
    check("新会话（reset）⇒ 媒体零点清零", g.media_anchor() == 0.0,
          f"得到 {g.media_anchor()}")

    # ---- B. 文件源 / 实时流的滞后基准 ---------------------------- #
    print("\n== 滞后基准 ==")
    captured: list[dict] = []

    class _FakeBridge:
        """顶掉 ``app.core.audio.tts_bridge`` —— 只抓参数。"""

        class TTSUnavailable(RuntimeError):
            pass

        @staticmethod
        def submit_queued(**kwargs):
            captured.append(kwargs)
            return {"seq": len(captured), "queued": True}

    import sys as _sys
    import types

    fake = types.ModuleType("app.core.audio.tts_bridge")
    fake.submit_queued = _FakeBridge.submit_queued
    fake.TTSUnavailable = _FakeBridge.TTSUnavailable
    _sys.modules["app.core.audio.tts_bridge"] = fake

    try:
        eng = InterpEngine()
        eng._gate.set_mode("follow")
        # 模拟「媒体正在播，播放头在 3 s」
        t_anchor = time.time() - 3.0
        eng._gate.update_playhead("file-1#0", 3.0, True)
        # 把零点钉死（update_playhead 用的是 time.time()，误差 ~ms）
        eng._gate._anchor = t_anchor

        seg = InterpSegment(index=1, text="hello", lang="English",
                            stream_id="file-1#0", mode="steady",
                            src_start_sec=1.0, src_end_sec=4.5,
                            created_ts=time.time() - 30.0,   # 离线转写
                            origin="file")
        seg.translated = "你好"
        eng._submit(seg)

        check("文件源：入队成功", len(captured) == 1, f"抓到 {len(captured)}")
        if captured:
            got = float(captured[0].get("src_end_ts") or 0.0)
            want = t_anchor + 4.5
            check("文件源：滞后基准 = 媒体零点 + src_end_sec",
                  abs(got - want) < 0.5,
                  f"得到 {got}，期望 {want}")
            check("文件源：基准**不是** created_ts（离线转写时刻）",
                  abs(got - seg.created_ts) > 1.0,
                  f"得到 {got} vs created_ts {seg.created_ts}")
            check("首窗帧数传给队列",
                  int(captured[0].get("first_window_frames") or 0)
                  == INTERP_FIRST_WINDOW_FRAMES,
                  f"得到 {captured[0].get('first_window_frames')}")

        # 实时流 ⇒ 仍用 created_ts
        captured.clear()
        seg2 = InterpSegment(index=2, text="hi", lang="English",
                             stream_id="live-x", mode="steady",
                             created_ts=time.time() - 7.0, origin="live")
        seg2.translated = "嗨"
        eng._submit(seg2)
        check("实时流：入队成功", len(captured) == 1, f"抓到 {len(captured)}")
        if captured:
            got = float(captured[0].get("src_end_ts") or 0.0)
            check("实时流：滞后基准 = created_ts",
                  abs(got - seg2.created_ts) < 0.5,
                  f"得到 {got} vs {seg2.created_ts}")
    finally:
        _sys.modules.pop("app.core.audio.tts_bridge", None)

    # ---- C. 队列把首窗帧数带进 job -------------------------------- #
    print("\n== 首窗帧数 ==")
    from wdnapps_audio_chat.cards.tts.queue import (  # noqa: E402
        submit_queued as q_submit,
    )

    q = q_submit(text="测试首窗", language="chinese",
                 first_window_frames=INTERP_FIRST_WINDOW_FRAMES)
    check("队列接受 first_window_frames", bool(q), f"得到 {q}")
    snap = q_submit(text="测试首窗2", language="chinese")
    check("不给也能提交（默认 0）", bool(snap), f"得到 {snap}")

    # ---- D. 宿主桥透传 --------------------------------------------- #
    from app.core.audio import tts_bridge  # noqa: E402

    import inspect

    sig = inspect.signature(tts_bridge.submit_queued)
    check("宿主桥 submit_queued 有 first_window_frames 形参",
          "first_window_frames" in sig.parameters,
          f"签名 {sig}")

    # ---- E. 本机 TTS 记进 autostart -------------------------------- #
    # ⚠️ 这是「重启后语音走对端板子」+「MT 冷启动 24 s」的根因修复。
    print("\n== TTS autostart ==")
    recorded: list[tuple] = []

    class _FakeReg:
        @staticmethod
        def list_running_by_capability(cap):
            return [{"model_id": "qwen3-tts", "npu_index": 1,
                     "source": "spawned", "capability_types": [cap]}]

        @staticmethod
        def record_autostart_entry(model_id, npu_index):
            recorded.append((model_id, npu_index))
            return True

    ok = InterpEngine._record_tts_autostart(_FakeReg(), "swcap_text_to_speech")
    check("已经在跑的本机 TTS ⇒ 记进 autostart",
          ok and recorded == [("qwen3-tts", 1)],
          f"得到 {recorded}")

    recorded.clear()
    ok = InterpEngine._record_tts_autostart(
        _FakeReg(), "swcap_text_to_speech", model_id="qwen3-tts",
        npu_index=0)
    check("显式给 model/npu ⇒ 记进 autostart",
          ok and recorded == [("qwen3-tts", 0)],
          f"得到 {recorded}")

    class _FakePeerReg(_FakeReg):
        @staticmethod
        def list_running_by_capability(cap):
            return [{"model_id": "qwen3-tts", "npu_index": 1,
                     "source": "adopted", "capability_types": [cap]}]

    recorded.clear()
    ok = InterpEngine._record_tts_autostart(_FakePeerReg(),
                                            "swcap_text_to_speech")
    check("**对端 peer** 的实例不记（那是别人的板子）",
          not ok and not recorded,
          f"得到 {recorded}")

    # ---- F. 新一轮识别 ⇒ 上一轮的译文作废 --------------------------- #
    # ⚠️ 这是「重复点开始识别 ⇒ 同一批句子重复入队 ⇒ 队列翻倍 ⇒ 滞后暴涨」
    # 的修复（实测：一轮 4 句的文件按 3~4 次「开始识别」⇒ 收到 14 句）。
    print("\n== 新一轮识别 ==")
    cleared: list[int] = []

    fake2 = types.ModuleType("app.core.audio.tts_bridge")
    fake2.submit_queued = _FakeBridge.submit_queued
    fake2.TTSUnavailable = _FakeBridge.TTSUnavailable

    def _queue_clear():
        cleared.append(1)

    fake2.queue_clear = _queue_clear
    _sys.modules["app.core.audio.tts_bridge"] = fake2
    try:
        eng = InterpEngine()
        eng._gate.set_mode("follow")
        eng._gate.update_playhead("file-1#0", 4.0, True)
        # 媒体已经播完 ⇒ 上报一次 ended
        eng._gate.update_playhead("file-1#0", 15.0, False, ended=True)
        seg = InterpSegment(index=1, text="hi", lang="English",
                            stream_id="file-1#0", mode="steady",
                            src_start_sec=1.0, src_end_sec=2.0,
                            origin="file")
        eng._pending.append(seg)
        eng._queue.put(seg)

        eng.on_new_stream("file-2#0")

        check("新一轮识别 ⇒ 待播（放行器压着的）清空",
              len(eng._pending) == 0, f"得到 {len(eng._pending)}")
        check("新一轮识别 ⇒ 翻译队列清空", eng._queue.qsize() == 0,
              f"得到 {eng._queue.qsize()}")
        check("新一轮识别 ⇒ 放行器复位（媒体零点清零）",
              eng._gate.media_anchor() == 0.0,
              f"得到 {eng._gate.media_anchor()}")
        check("新一轮识别 ⇒ **「源已结束」这个媒体事实要留着**"
              "（否则新一轮的句子会被压住 ⇒ 永远不播）",
              eng._gate.source_done("file-2#0") is True,
              f"source_done={eng._gate.source_done('file-2#0')}")
        check("新一轮识别 ⇒ **TTS 队列也清空**（否则重复的译文继续播）",
              len(cleared) == 1, f"清队列调用 {len(cleared)} 次")
    finally:
        _sys.modules.pop("app.core.audio.tts_bridge", None)

    print(f"\n通过 {_PASS} / 失败 {_FAIL}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
