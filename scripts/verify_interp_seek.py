#!/usr/bin/env python3
"""回归：同传的 **seek（用户拖动进度条）** 处理。

背景（实测 2026-11-22，用户报）
--------------------------------
「『含音轨的视频』播放的时候，字幕在实时翻译，但是多次拖动进度条，
没有和字幕的翻译同声播放出来」

修前实测（``tools/probes/interp/m23_seek_sync.py``，17 句里 **15 句错位**）：

* **前跳**（播放头 0 → 60 s）：播的却是 29~47 s 的内容 ⇒ 错位 **+27~+35 s**
  （跳过的句子还在待播队列里 ⇒ 放行器全放行 ⇒ 陈旧译文照播）；
* **后跳**（播放头 60 → 20 s）：播的却是 48~77 s 的内容 ⇒ 错位 **−20~−31 s**
  （FIFO 里旧位置的音频没清 ⇒ 照播）。

契约
====
1. **前跳要能认出来**（判据 = 「比两次上报之间的真实时长多跳了多少」）；
2. **媒体卡住 / 缓冲造成的一次补跳不算 seek**；
3. **暂停很久再恢复不算 seek**；
4. seek 之后：**新位置之前的待播句被丢**（计 ``skipped_seek``）；
5. seek 之后：**新位置之后的句子被重新武装**（重播 ⇒ 计 ``replayed``）
   ⇒ 用户拖回去时，已经播过的句子**能再说一遍**；
6. seek 之后：**队列里的陈旧音频被冲掉** + **播放时钟复位**；
7. 重播后待播队列**按位置排序**（否则放行顺序会乱）。

跑法::

    .venv/bin/python scripts/verify_interp_seek.py
"""
from __future__ import annotations

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from wdnapps_audio_chat.cards.qwen3_asr.interp import (  # noqa: E402
    SEEK_BACK_TOLERANCE,
    SEEK_FWD_TOLERANCE,
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


def seg(index: int, start: float, end: float,
        stream_id: str = "file-1#0") -> InterpSegment:
    return InterpSegment(index=index, text="第%d句" % index, lang="English",
                         stream_id=stream_id, mode="steady",
                         src_start_sec=start, src_end_sec=end,
                         origin="file")


def _flush_spy(calls: list):
    """替掉宿主桥的 ``queue_flush_position``（不碰真的队列）。"""
    import app.core.audio.tts_bridge as bridge

    def _fake(pos_sec, direction=""):
        calls.append((float(pos_sec), str(direction)))
        return {"flushed": 3, "pos": float(pos_sec), "dir": direction}

    bridge.queue_flush_position = _fake


def main() -> int:
    print("== ① 放行器：seek 判定（双向）==")

    # 1. 正常播放（每 0.25 s 前进 0.25 s）⇒ 不算 seek
    g = Gate("follow")
    g.update_playhead("s", 0.0, True)
    ok = True
    for i in range(1, 6):
        time.sleep(0.05)
        if g.update_playhead("s", i * 0.25, True):
            ok = False
    check("正常播放 ⇒ 不算 seek", ok)

    # 2. 前跳 ⇒ 算 seek，方向 = fwd
    g2 = Gate("follow")
    g2.update_playhead("s", 1.0, True)
    time.sleep(0.05)
    check("**前跳 ⇒ 算 seek**", g2.update_playhead("s", 41.0, True) is True)
    check("前跳方向 = fwd", (g2.last_seek() or {}).get("dir") == "fwd",
          f"得到 {(g2.last_seek() or {}).get('dir')}")
    check("前跳记下了起止位置",
          (g2.last_seek() or {}).get("to") == 41.0
          and (g2.last_seek() or {}).get("from") == 1.0)

    # 3. 后跳 ⇒ 算 seek，方向 = back
    g3 = Gate("follow")
    g3.update_playhead("s", 60.0, True)
    check("**后跳 ⇒ 算 seek**", g3.update_playhead("s", 20.0, True) is True)
    check("后跳方向 = back", (g3.last_seek() or {}).get("dir") == "back")

    # 4. 媒体卡住 / 缓冲 ⇒ 一次补跳**不算** seek
    g4 = Gate("follow")
    g4.update_playhead("s", 0.0, True)
    g4._ph_ts = time.time() - 10.0        # 模拟「10 秒没上报」
    check("缓冲补跳 ⇒ 不算 seek", g4.update_playhead("s", 10.0, True) is False)

    # 5. 暂停很久再恢复 ⇒ 不算 seek
    g5 = Gate("follow")
    g5.update_playhead("s", 10.0, True)
    g5._ph_ts = time.time() - 60.0        # 暂停 60 s
    check("暂停后恢复 ⇒ 不算 seek", g5.update_playhead("s", 10.25, True) is False)

    # 6. 小幅回退（< 容差）⇒ 不算 seek（老行为不能退化）
    g6 = Gate("follow")
    g6.update_playhead("s", 30.0, True)
    check("小幅回退 ⇒ 不算 seek",
          g6.update_playhead("s", 30.0 - SEEK_BACK_TOLERANCE + 0.5, True) is False)
    # ⚠️ **换一个新 gate** —— ``prev`` 会被上一次上报更新 ⇒ 同一个 gate
    # 上连着两次回退，第二次只回退了 1 秒 ⇒ 不算 seek（老回归脚本也是
    # 这么写的：边界用例必须换新 gate）。
    g6b = Gate("follow")
    g6b.update_playhead("s", 30.0, True)
    check("大幅回退 ⇒ 算 seek",
          g6b.update_playhead("s", 30.0 - SEEK_BACK_TOLERANCE - 0.5,
                              True) is True)

    print("\n== ② 引擎：seek 之后的重同步 ==")

    calls: list = []
    _flush_spy(calls)

    eng = InterpEngine()
    # 5 句，位置：1、10、20、30、40（秒）
    segs = [seg(1, 1.0, 4.0), seg(2, 10.0, 14.0), seg(3, 20.0, 24.0),
            seg(4, 30.0, 34.0), seg(5, 40.0, 44.0)]
    for s in segs:
        eng._cache.append(s)
    # 第 2 句在「待播队列」里（放行器压着）；第 1、3 句已经放行过（播过了）
    eng._pending.append(segs[1])
    eng._armed.add(segs[1].index)

    # ---- 前跳到 20 s ----
    eng._resync_seek({"dir": "fwd", "from": 5.0, "to": 20.0})
    ids = [s.index for s in eng._pending]
    check("前跳：跳过的句子（位置 < 20）被丢",
          segs[0].index not in ids and segs[1].index not in ids,
          f"待播 = {ids}")
    check("前跳：新位置之后的句子被重播（重新武装）",
          segs[2].index in ids and segs[3].index in ids and segs[4].index in ids,
          f"待播 = {ids}")
    check("前跳：计数器 skipped_seek +1",
          eng._counters.skipped_seek == 1,
          f"得到 {eng._counters.skipped_seek}")
    check("前跳：计数器 replayed = 3（新位置之后的三句）",
          eng._counters.replayed == 3,
          f"得到 {eng._counters.replayed}")
    check("前跳：队列的陈旧音频被冲掉",
          len(calls) == 1 and calls[0][0] == 20.0 and calls[0][1] == "fwd",
          f"得到 {calls}")
    check("前跳：待播队列按位置排序",
          [s.src_start_sec for s in eng._pending]
          == sorted(s.src_start_sec for s in eng._pending),
          f"待播 = {[s.src_start_sec for s in eng._pending]}")

    # ---- 后跳到 10 s ----
    calls.clear()
    eng2 = InterpEngine()
    for s in segs:
        eng2._cache.append(s)
    eng2._resync_seek({"dir": "back", "from": 60.0, "to": 10.0})
    ids2 = [s.index for s in eng2._pending]
    check("后跳：位置在新位置之前的句子不重播（用户跳过了）",
          segs[0].index not in ids2, f"待播 = {ids2}")
    check("后跳：新位置之后的句子**全部重播**（已经播过的也要再说一遍）",
          segs[1].index in ids2 and segs[2].index in ids2
          and segs[3].index in ids2 and segs[4].index in ids2,
          f"待播 = {ids2}")
    check("后跳：计数器 replayed = 4",
          eng2._counters.replayed == 4,
          f"得到 {eng2._counters.replayed}")
    check("后跳：队列的陈旧音频被冲掉（方向 back）",
          len(calls) == 1 and calls[0][0] == 10.0 and calls[0][1] == "back",
          f"得到 {calls}")

    # ---- 重播不会重复入队 ----
    eng3 = InterpEngine()
    for s in segs:
        eng3._cache.append(s)
        eng3._pending.append(s)
        eng3._armed.add(s.index)
    eng3._resync_seek({"dir": "back", "from": 60.0, "to": 10.0})
    ids3 = [s.index for s in eng3._pending]
    check("已经在等的句子**不会**被重复入队",
          len(ids3) == len(set(ids3)), f"待播 = {ids3}")
    check("位置在新位置之前的句子被丢（后跳 ⇒ 它在身后了）",
          segs[0].index not in ids3, f"待播 = {ids3}")
    check("其余句子都还在（等着按新位置重播）", len(ids3) == 4, f"待播 = {ids3}")

    print("\n== ③ TTS 队列：冲掉陈旧音频 + 复位播放时钟 ==")
    import numpy as np

    from wdnapps_audio_chat.cards.tts import queue as tq

    # ⚠️ **用独立的队列实例**（不碰全局单例）⇒ 断言不受消费线程干扰。
    queue = tq.TTSQueue()
    queue.shutdown()          # 两个线程都停 ⇒ FIFO 不会被偷偷取走
    # 一句「陈旧的」待合成 job + 一段「陈旧的」PCM
    stale = tq.TTSJob(seq=999, text="陈旧的一句", language="chinese",
                      src_start_sec=5.0)
    queue._jobs.append(stale)
    queue._inflight.append(stale)
    queue._fifo_push(stale, np.zeros(240, dtype=np.float32))
    check("入队后深度 = 1", queue.snapshot()["depth"] == 1,
          f"得到 {queue.snapshot()['depth']}")
    check("FIFO 里有一段陈旧音频", len(queue._fifo) >= 1,
          f"得到 {len(queue._fifo)}")
    # 播放时钟停在「旧位置」
    queue.set_clock(provider=lambda: 60.0, offset=0.0)
    res = queue.flush_by_position(10.0, "back")
    check("flush 后深度 = 0（陈旧的 job 被丢掉）",
          queue.snapshot()["depth"] == 0,
          f"得到 {queue.snapshot()['depth']}")
    check("flush 报告了冲掉的段数", int(res.get("flushed") or 0) >= 1,
          f"得到 {res}")
    check("flush 后 FIFO 是空的（陈旧 PCM 被丢掉）", len(queue._fifo) == 0,
          f"得到 {len(queue._fifo)}")
    check("flush 复位了播放时钟（后跳 ⇒ 播放头变小）",
          queue._clock_last <= 0.0, f"得到 {queue._clock_last}")
    check("flush 把正在合成的那句也作废了（它的 PCM 不会被播出来）",
          stale.cancelled is True)

    print("\n== ④ 被 seek 作废的句子**不能堵住 FIFO** ==")
    #
    # ⚠️ **实测 2026-11-22 的第四层根因**（最隐蔽的一层）：
    # 播放线程里「**先判播放时钟 ⇒ 再判 job 是否已取消**」⇒ 被 seek 作废的
    # 那句**还会继续吐 PCM**（合成线程已经在跑了，停不掉）⇒ 它的位置在
    # **未来**（比如 100 s，播放头在 10 s）⇒ 时钟说「还没到」⇒ 分片被
    # **放回 FIFO 头部** ⇒ **整条 FIFO 被它堵住**（FIFO 是顺序的 ⇒
    # 队头不动 ⇒ 后面全不动）⇒ 这 30 秒里**一句都没播** ⇒ 播放头走到
    # 100 s 才放开 ⇒ **整队一起进管道**（实测 6 句的 ``t_play`` 完全相同）。
    #
    # ⇒ 修法：**先判「已取消」，再判时钟**。
    # ⇒ 这里用**假 writer**（不出声）断言「后面的句子照样能播」。
    from wdnapps_audio_chat.cards.tts import service as tts_service

    class _FakeWriter:
        """假 aplay writer —— 只记写了哪些块（回归脚本不该在板子出声）。"""

        def __init__(self, *a, **kw) -> None:
            self.ok = True
            self.sizes: list = []

        def write(self, chunk) -> None:
            self.sizes.append(int(len(chunk)))

        def set_volume(self, v) -> None:
            pass

        def reset_peaks(self) -> None:
            pass

        def backlog_sec(self) -> float:
            return 1.0

        def playback_end(self) -> float:
            return time.time()

        def close(self) -> None:
            pass

    old_writer = tts_service.AplayWriter
    tts_service.AplayWriter = _FakeWriter
    try:
        q2 = tq.TTSQueue()
        # ① 被作废的句子：位置在**未来**（100 s），而时钟在 10 s
        dead = tq.TTSJob(seq=1, text="作废的", language="chinese",
                         src_start_sec=100.0)
        dead.cancelled = True
        q2._fifo_push(dead, np.zeros(240, dtype=np.float32))
        # ② 该播的句子：位置 5 s（时钟 10 s ⇒ 够得着）
        good = tq.TTSJob(seq=2, text="该播的", language="chinese",
                         src_start_sec=5.0)
        q2._fifo_push(good, np.zeros(480, dtype=np.float32))
        q2.set_clock(provider=lambda: 10.0, offset=0.0)
        deadline = time.time() + 5.0
        sizes: list = []
        while time.time() < deadline:
            with q2._cond:
                w = q2._writer
            sizes = list(getattr(w, "sizes", []) or []) if w is not None else []
            if 480 in sizes:
                break
            time.sleep(0.05)
        check("作废的句子**没有**堵住 FIFO（后面的句子照样出声）",
              480 in sizes, f"写出去的块 = {sizes}")
        check("作废的句子的音频**没有**被播出来", 240 not in sizes,
              f"写出去的块 = {sizes}")
        q2.shutdown()
    finally:
        tts_service.AplayWriter = old_writer

    print(f"\n通过 {_PASS} / 失败 {_FAIL}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
