#!/usr/bin/env python3
"""回归：同传的**可听滞后**（用户报「ASR+翻译后，TTS 播放滞后非常多」）。

用户报（2026-11-23 06:12）
    「流式语音识别」中，ASR+翻译后，TTS 播放**滞后非常多**。

实测（M29 探针，15 s 英文素材，热态）
====================================
改前：可听滞后 **2.63 → 4.89 → 4.86 s**（每秒源话增长 **0.20 s**）
      ⇒ 5 分钟素材 ⇒ 落后 **60 秒** ⇒ 「滞后非常多」。

**根因不是某一段慢，而是三个量凑出来的**：

    可听滞后(t) ≈ L0 + (k ÷ 产能 − 1) × t

* ``L0`` —— 起播延迟（ASR 出第一句 + 翻译 + TTS 首窗）；
* ``k`` —— **长度比**（译文念出来比源话长多少）；
* ``产能`` —— TTS 每秒能产出多少秒音频（**每批**都有固定开销）。

改前：``k = 1.104``、产能 ~0.81（3 批 × ~1.3 s 死时间）⇒ 增长 0.23 s/s。

本脚本覆盖的契约
================
A. **送进 TTS 的文本去掉停顿标点**（字幕不变）—— 中文标点每个要
   0.25~1.0 秒静音（实测省 15.9% 的音频）；
B. **变速不得低于 1.0** —— 变慢会让译文**落后得更快**；
C. **跟播模式按「内容量」攒批**（摊薄每批的固定开销）；
D. **裁掉每段音频开头的静音**（模型每段带 0.35~0.65 秒）。

跑法::

    .venv/bin/python scripts/verify_interp_lag_floor.py
"""
from __future__ import annotations

import os
import sys
import time
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_ROOT, os.path.join(_ROOT, ".venv", "src",
                               "wdnapps-audio-chat", "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402

from wdnapps_audio_chat.cards.qwen3_asr.interp import (  # noqa: E402
    INTERP_MERGE_HOLD_SEC,
    MERGE_MAX_HOLD_SEC,
    MERGE_MIN_AUDIO_SEC,
    TEMPO_FLOOR,
    InterpEngine,
    InterpSegment,
    _estimate_audio_sec,
    _tts_text,
)
from wdnapps_audio_chat.cards.tts.queue import (  # noqa: E402
    trim_head_silence,
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


def _seg(index: int, text: str, translated: str,
         start: float = 0.0) -> InterpSegment:
    return InterpSegment(index=index, text=text, lang="English",
                         stream_id="file-1#0", mode="steady",
                         src_start_sec=start, src_end_sec=start + 3.0,
                         origin="file", translated=translated)


def main() -> int:
    print("== A. TTS 文本：去停顿标点，字幕不受影响 ==")
    txt = "嗯嗯。哦对了，当然了！"
    check("中文标点被去掉", _tts_text(txt) == "嗯嗯哦对了当然了",
          f"得到 {_tts_text(txt)!r}")
    check("**拉丁文标点保留**（它的停顿很短，留着更清楚）",
          _tts_text("Hello, world! How are you?")
          == "Hello, world! How are you?",
          f"得到 {_tts_text('Hello, world! How are you?')!r}")
    check("**字幕文本不受影响**（引擎的 translated 字段原样保留）",
          _seg(1, txt, txt).translated == txt)
    check("全角括号 / 引号也去掉",
          _tts_text("他说「好」（真的）") == "他说好真的",
          f"得到 {_tts_text('他说「好」（真的）')!r}")
    check("空文本不炸", _tts_text("") == "" and _tts_text(None) == "")

    print("\n== B. 变速下限 = 1.0（永不把播放拖慢）==")
    check("TEMPO_FLOOR = 1.0", TEMPO_FLOOR == 1.0, f"得到 {TEMPO_FLOOR}")
    eng = InterpEngine()
    check("长度比 < 1 ⇒ 变速取 1.0（不需要放慢）",
          eng._snap_tempo(0.8) == 1.0, f"得到 {eng._snap_tempo(0.8)}")
    check("长度比 1.104 ⇒ 取阶梯上 ≤ 的档（1.08）",
          eng._snap_tempo(1.104) == 1.08, f"得到 {eng._snap_tempo(1.104)}")
    check("长度比很大 ⇒ 不超过上限 1.15",
          eng._snap_tempo(9.0) == 1.15, f"得到 {eng._snap_tempo(9.0)}")

    print("\n== C. 跟播模式按「内容量」攒批 ==")
    check("攒批阈值 = 18 秒音频", MERGE_MIN_AUDIO_SEC == 18.0,
          f"得到 {MERGE_MIN_AUDIO_SEC}")
    check("攒批有上界（不会永远不开口）",
          0 < MERGE_MAX_HOLD_SEC <= 30.0, f"得到 {MERGE_MAX_HOLD_SEC}")
    check("实时流仍用合并窗口（低延迟优先）", INTERP_MERGE_HOLD_SEC == 1.0)
    # 估算口径：中文 ~4.5 字/秒
    check("音频估算：10 个汉字 ≈ 2.2 秒",
          abs(_estimate_audio_sec("一二三四五六七八九十") - 2.22) < 0.05,
          f"得到 {_estimate_audio_sec('一二三四五六七八九十')}")

    # ---- 攒批判定 ----
    eng2 = InterpEngine()
    eng2._gate_mode = "follow"
    eng2._source = "file"
    eng2._running = True
    # 假装「源还没读完」
    eng2._counters.segments = 10
    eng2._counters.translated = 1
    now = time.time()
    small = [_seg(1, "a", "一二三四五六七八九十")]
    eng2._ready_since[1] = now
    check("内容量不够 ⇒ **不放行**（继续攒）",
          eng2._batch_ready(small, now) is False)
    big = [_seg(i, "a", "一" * 120) for i in range(1, 4)]
    for s in big:
        eng2._ready_since[s.index] = now
    check("内容量够了 ⇒ 放行",
          eng2._batch_ready(big, now) is True)
    check("攒满上限 ⇒ 兜底放行（不会永远压着）",
          eng2._batch_ready(small, now + MERGE_MAX_HOLD_SEC + 0.1) is True)

    # 实时流：低延迟 ⇒ 合并窗口
    eng3 = InterpEngine()
    eng3._gate_mode = "now"
    eng3._source = "live"
    eng3._running = True
    live = [_seg(1, "a", "一二三四五六七八九十")]
    eng3._ready_since[1] = now
    check("实时流（now）⇒ 内容再多也**不等**（低延迟优先）",
          eng3._batch_ready(live, now) is False)
    check("实时流 ⇒ 等够合并窗口才放行",
          eng3._batch_ready(live, now + INTERP_MERGE_HOLD_SEC + 0.1) is True)

    # 文件源 + 「不等播放」⇒ **也**按内容量攒（离线转写，攒批不花延迟）
    eng4 = InterpEngine()
    eng4._gate_mode = "now"
    eng4._source = "file"
    eng4._running = True
    eng4._counters.segments = 10
    eng4._counters.translated = 1
    eng4._ready_since[1] = now
    check("文件源 + 不等播放 ⇒ 也按**内容量**攒（不是等合并窗口）",
          eng4._batch_ready(live, now) is False)
    check("文件源 + 不等播放 ⇒ 攒够就放",
          eng4._batch_ready(big, now) is True)

    print("\n== E. 「源已读完」只认**当前这一轮** ==")
    check("流 id 里的请求号能解析出来",
          eng4._rid_of_stream() == 0, "空流 ⇒ 0")
    eng5 = InterpEngine()
    eng5._stream_id = "file-7#0"
    check("file-7#0 ⇒ rid = 7", eng5._rid_of_stream() == 7,
          f"得到 {eng5._rid_of_stream()}")
    eng5._stream_id = "live-abc"
    check("实时流 id（live-xxx）⇒ rid = 0", eng5._rid_of_stream() == 0,
          f"得到 {eng5._rid_of_stream()}")

    # 注入假的 ASR 服务：上一轮跑完的 done ⇒ 不能当「这一轮跑完了」
    fake = types.ModuleType("wdnapps_audio_chat.cards.qwen3_asr.service")

    class _Svc:
        def __init__(self, state, rid):
            self._state = state
            self._rid = rid

        def snapshot(self):
            return {"state": self._state, "current_request_id": self._rid}

    def _install(state, rid):
        fake.get_asr_service = lambda: _Svc(state, rid)
        sys.modules["wdnapps_audio_chat.cards.qwen3_asr.service"] = fake

    _install("done", 99)
    eng6 = InterpEngine()
    eng6._source = "file"
    eng6._stream_id = "file-7#0"
    eng6._counters.segments = 3
    eng6._counters.translated = 3
    check("**上一轮**跑完的 done ⇒ 不算「这一轮跑完」（攒批继续攒）",
          eng6._batch_is_final() is False)
    _install("done", 7)
    check("**这一轮**跑完 ⇒ 算「源已读完」（剩下的全放）",
          eng6._batch_is_final() is True)
    _install("running", 7)
    check("还在跑 ⇒ 不算读完",
          eng6._batch_is_final() is False)
    sys.modules.pop("wdnapps_audio_chat.cards.qwen3_asr.service", None)

    print("\n== D. 裁掉每段开头的静音 ==")
    sr = 24000
    tone = (0.3 * np.sin(2 * np.pi * 220
                         * np.arange(int(1.0 * sr)) / sr)).astype(np.float32)
    silence = np.zeros(int(0.4 * sr), dtype=np.float32)
    out = trim_head_silence(np.concatenate([silence, tone]), sr)
    check("0.4 秒头静音被裁掉",
          out is not None and abs((len(out) / sr) - 1.0) < 0.02,
          f"得到 {None if out is None else len(out)/sr}")
    check("**没有头静音 ⇒ 原样返回 None**（不裁）",
          trim_head_silence(tone, sr) is None)
    check("整块都是静音 ⇒ 不裁（等下一块，避免把内容裁没）",
          trim_head_silence(np.zeros(sr, dtype=np.float32), sr) is None)
    check("空 PCM 不炸", trim_head_silence(np.zeros(0, dtype=np.float32), sr)
          is None)

    print(f"\n通过 {_PASS} / 失败 {_FAIL}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
