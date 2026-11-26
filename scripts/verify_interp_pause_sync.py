#!/usr/bin/env python3
"""回归：同传的三个问题（2026-11-25 用户提的）——**纯逻辑，不占 NPU**。

用户提的三件事
--------------
1. 同传结果显示「已播报」，但喇叭里没有声音；
2. 「含音轨的视频」同传的声音播报**还是没有跟随视频中当前显示的翻译字幕**；
3. 视频**暂停**的时候，同传播完当前字幕的译文后**也应该暂停播报**。

实测到的根因（全部来自实测数据）
--------------------------------
① **电平探针是死的** —— ``queue.py::_finish_job`` 里 ``job.peak`` /
   ``job.raw_peak`` **从来没被赋值** ⇒ ``last_peak`` / ``last_raw_peak``
   **恒为 0** ⇒ 面板那个「音量偏小」提示**永远不会显示** ⇒ 用户只看到
   「已播报」。
   而真正送进 aplay 的峰值实测只有 **0.0156**（= **-36 dBFS**）⇒
   **板载小喇叭上听不见**。
   ⇒ 修法：**每句峰值归一化**（目标 0.9）+ ``tanh`` 软限幅。

② **队列的 8 秒兜底放行会「狂喷积压」** —— ``queue.py::_clock_ready``：
   播放头**不动**超过 8 秒 ⇒ **对所有句子一律放行**。
   ⇒ 视频**一暂停**就满足这个条件 ⇒ 实测一次放出 **184 秒**积压 ⇒
   声音跑到画面**前面 174 秒**（``lag_sec=173.89``）⇒
   ⇒ **问题 2 与问题 3 是同一个根因**。

③ **离线转写 ⇒ 译文产出远快于播放头 ⇒ TTS 队列积压上百秒** ——
   文件 / 视频源是**离线一次性转写**的 ⇒ 几十秒内攒出几百句译文 ⇒
   TTS 产能只有 ~1.0× 实时 ⇒ 追不上 ⇒ 队列里堆出上百秒
   （实测 ``queued_audio_sec=184.23`` / ``depth=11``）⇒
   而队列**先进先出** ⇒ TTS 先做**最老**的那几段（位置早被播放头越过）
   ⇒ **播放头此刻要的那句排在队尾** ⇒ **被饿死** ⇒ 声音落后几十秒。
   ⇒ 修法：**提交窗口** —— 只把「播放头前方 N 秒」的译文送进 TTS。

④ **多标签页互相覆盖播放头** —— 同传引擎是**服务端全局单例** ⇒
   多个标签页的播放头**共用一个键** ⇒ 后上报的覆盖先上报的 ⇒
   实测那一轮日志里有 **6 个浏览器连接**同时上报播放头 ⇒ 播放头在 6 个
   位置之间来回跳 ⇒ 对齐判定完全失效。
   ⇒ 修法：**按标签页 id 隔离**（前端存在 ``sessionStorage``）。

本脚本覆盖
----------
* **A** 队列的「按住 vs 放行」判定（问题 3 + 问题 2 的核心）；
* **B** 提交窗口（问题 2 的另一半）；
* **C** 标签页隔离（问题 2 的另一半）；
* **D** 自动增益 + 电平探针（问题 1）；
* **E** 静态断言（前端 / API / 宿主桥的接线没漏）。

⚠️ **本脚本不碰 NPU、不碰正在跑的会话** ⇒ 任何时候都能跑。

跑法::

    .venv/bin/python scripts/verify_interp_pause_sync.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ⚠️ **插件包在 .venv 里（editable 安装）** ⇒ 直接 import 即可。
from wdnapps_audio_chat.cards.qwen3_asr import interp as _interp  # noqa: E402
from wdnapps_audio_chat.cards.tts import queue as _queue  # noqa: E402

_FAIL: list[str] = []
_OK = 0


def check(cond: bool, label: str) -> bool:
    global _OK
    if cond:
        _OK += 1
        print("  \033[32mPASS\033[0m %s" % label)
    else:
        _FAIL.append(label)
        print("  \033[31mFAIL\033[0m %s" % label)
    return bool(cond)


def section(title: str) -> None:
    print("\n\033[1m%s\033[0m" % title)


# --------------------------------------------------------------------------- #
# A. 队列的「按住 vs 放行」
# --------------------------------------------------------------------------- #

class _FakeJob:
    """只需要 ``src_start_sec``（``_clock_ready`` 只看它）。"""

    def __init__(self, src_start_sec: float) -> None:
        self.src_start_sec = src_start_sec


def _mk_queue():
    q = _queue.TTSQueue()
    return q


def part_a() -> None:
    section("A. 队列的「按住 vs 放行」（问题 3：暂停 ⇒ 播报也暂停）")
    q = _mk_queue()
    try:
        # 播放头固定在 30 秒。
        q.set_clock(lambda: 30.0, 0.0)

        # ---- ① 用户**暂停**（播过、现在停）⇒ **按住** ----
        state = {"playing": False, "ended": False, "failed": False,
                 "moved": True, "alive": True}
        q.set_media_state(lambda: dict(state))
        r = q._clock_ready(_FakeJob(10.0))
        check(r is False, "暂停 ⇒ 按住不放（不狂喷积压）")
        # ⚠️ **位置已经过了**（30 > 10）也一样按住 —— 否则「暂停时把积压
        # 一次性放出来」的老毛病还在。
        r2 = q._clock_ready(_FakeJob(5.0))
        check(r2 is False, "暂停 + 位置已过 ⇒ 仍然按住")

        # ---- ② 源**播完** ⇒ 放行（剩下的补播）----
        state["ended"] = True
        state["playing"] = False
        check(q._clock_ready(_FakeJob(999.0)) is True,
              "源播完 ⇒ 放行（剩下的补播）")
        state["ended"] = False

        # ---- ③ 媒体**坏了** ⇒ 放行 ----
        state["failed"] = True
        check(q._clock_ready(_FakeJob(999.0)) is True, "媒体坏了 ⇒ 放行")
        state["failed"] = False

        # ---- ④ 前端**失联**（心跳断了）⇒ 按住（别狂喷）----
        state["alive"] = False
        check(q._clock_ready(_FakeJob(999.0)) is False,
              "前端失联 ⇒ 按住（不狂喷积压）")
        state["alive"] = True

        # ---- ⑤ 正常播放 ⇒ **严格按播放头** ----
        state["playing"] = True
        check(q._clock_ready(_FakeJob(100.0)) is False,
              "在播 + 还没到位置 ⇒ 不放（严格对齐）")
        check(q._clock_ready(_FakeJob(20.0)) is True,
              "在播 + 到了位置 ⇒ 放")

        # ---- ⑥ 「从没播过」（自动播放被拦）⇒ 8 秒兜底**仍然放行** ----
        # ⚠️ 这条不能坏 —— 坏了就是「有翻译、一声不响」（踩过好几次）。
        state["playing"] = False
        state["moved"] = False
        # 让播放头「卡住」超过 8 秒（兜底放行的条件）。
        q._clock_stall_at = time.time() - (_queue._CLOCK_STALL_SEC + 1.0)
        check(q._clock_ready(_FakeJob(999.0)) is True,
              "从没播过 + 卡住 8 秒 ⇒ 兜底放行（保「有翻译一声不响」的老修复）")

        # ---- ⑦ 计数（诊断用）----
        snap = q.snapshot()
        check(int(snap.get("clock_held_paused") or 0) >= 1,
              "计数 clock_held_paused 记下了「因为暂停按住」")
        check(int(snap.get("clock_held_lost") or 0) >= 1,
              "计数 clock_held_lost 记下了「因为失联按住」")
        check(int(snap.get("clock_released") or 0) >= 1,
              "计数 clock_released 记下了「兜底放行」")
        check(bool(snap.get("media_state")) is True,
              "快照里有 media_state（接上了媒体状态通道）")
    finally:
        q.shutdown()


def part_a2() -> None:
    section("A2. 暂停「按住」只在**句边界**生效（不拦腰截断）")
    q = _mk_queue()
    try:
        q.set_clock(lambda: 30.0, 0.0)
        state = {"playing": False, "ended": False, "failed": False,
                 "moved": True, "alive": True}
        q.set_media_state(lambda: dict(state))
        # 句**中**（boundary=False）⇒ 不按暂停 ⇒ 当前这句继续播完。
        check(q._clock_ready(_FakeJob(10.0), boundary=False) is True,
              "句中 ⇒ 不因暂停而停（当前那句播完）")
        # 句**首**（boundary=True）⇒ 按住。
        check(q._clock_ready(_FakeJob(10.0), boundary=True) is False,
              "句首 ⇒ 因暂停而按住")
        # ⚠️ **位置没到**这条**逐块判**（boundary=False 也要判）。
        check(q._clock_ready(_FakeJob(999.0), boundary=False) is False,
              "句中 + 位置没到 ⇒ 仍然不放（对齐精度不受影响）")
    finally:
        q.shutdown()


# --------------------------------------------------------------------------- #
# B. 提交窗口
# --------------------------------------------------------------------------- #

def part_b() -> None:
    section("B. 提交窗口（问题 2：队列只装「马上就要播」的内容）")
    eng = _interp.InterpEngine()

    def seg(idx: int, start: float):
        return _interp.InterpSegment(
            index=idx, text="x", lang="English", stream_id="file-1#0",
            mode="steady", src_start_sec=start, src_end_sec=start + 2.0,
            created_ts=time.time(), origin="file",
        )

    segs = [seg(1, 100.0), seg(2, 140.0), seg(3, 200.0)]

    # 播放头 = 100 秒 ⇒ 窗口 = 100 + 45 = 145 ⇒ 200 秒那句**不该**提交。
    eng._gate_mode = "follow"
    eng._source = "file"
    eng._gate.update_playhead("file-1#0", 100.0, True)
    kept = eng._within_window(segs)
    starts = sorted(s.src_start_sec for s in kept)
    check(starts == [100.0, 140.0],
          "窗口外的（200 s）不提交 ⇒ 队列里只剩「马上要播的」")
    check(len(kept) == 2, "窗口内两句都保留")

    # 「now」模式 ⇒ 不限制。
    eng._gate_mode = "now"
    check(len(eng._within_window(segs)) == 3, "「不等播放」模式不受窗口限制")
    eng._gate_mode = "follow"

    # 源播完了 ⇒ 剩下的全放（补播）—— 否则尾巴上的译文**永远播不出来**。
    eng._gate.update_playhead("file-1#0", 100.0, False, ended=True)
    check(len(eng._within_window(segs)) == 3, "源播完 ⇒ 不过滤（剩下的补播）")

    # 实时流 ⇒ 不限制。
    eng._gate.update_playhead("file-1#0", 100.0, True, ended=False)
    eng._source = "live"
    check(len(eng._within_window(segs)) == 3, "实时流不受窗口限制")

    # ⚠️ **窗口外的内容不能丢** —— 它必须还留在待播队列里。
    eng._source = "file"
    eng._gate.update_playhead("file-1#0", 100.0, True)
    with eng._lock:
        eng._pending.extend(segs)
        pending_before = len(eng._pending)
    kept = eng._within_window(list(eng._pending))
    with eng._lock:
        still = len(eng._pending)
    check(still == pending_before,
          "窗口过滤**不删**待播内容（只是不提交 ⇒ 播放头往前走就进来了）")
    check(len(kept) < still, "窗口过滤确实筛掉了一部分（否则没意义）")

    # ⚠️ **文件源的批次要限量**（否则一个作业就是 45 秒的连续语音 ⇒
    # 用户按了暂停，「按住」要等 45 秒播完才生效）。
    eng._source = "file"
    eng._gate.update_playhead("file-1#0", 100.0, True)
    many = [seg(100 + i, 100.0 + i * 3.0) for i in range(15)]   # 45 秒
    for s in many:
        s.translated = "x" * 40          # 让估算有长度
    n = eng._file_batch_size(many)
    check(1 < n < len(many),
          "文件源批次被限量（%d / %d 句 ⇒ 不会一口气 45 秒）" % (n, len(many)))


# --------------------------------------------------------------------------- #
# C. 标签页隔离
# --------------------------------------------------------------------------- #

def part_c() -> None:
    section("C. 标签页隔离（问题 2：多标签页互相覆盖播放头）")
    eng = _interp.InterpEngine()
    eng._client_id = "tabA"

    r = eng.on_playhead("file-1#0", 12.0, True, client_id="tabB")
    check(bool(r.get("ignored")) is True, "别的标签页的播放头 ⇒ 不认")
    check(eng._gate.playhead("file-1#0") is None,
          "别的标签页的播放头**没进**放行器")
    check(int(eng._ph_ignored) == 1, "被忽略的上报有计数（诊断用）")

    r = eng.on_playhead("file-1#0", 12.0, True, client_id="tabA")
    check(not r.get("ignored"), "发起方的播放头 ⇒ 认")
    check(abs(float(eng._gate.playhead("file-1#0")) - 12.0) < 0.01,
          "发起方的播放头**进**了放行器")

    # ⚠️ **没带 client_id**（老客户端）⇒ 不限制（别把老前端搞哑）。
    eng2 = _interp.InterpEngine()
    eng2._client_id = "tabA"
    r = eng2.on_playhead("file-1#0", 7.0, True)
    check(not r.get("ignored"), "老客户端（没带 client_id）⇒ 仍然认")


# --------------------------------------------------------------------------- #
# D. 自动增益 + 电平探针
# --------------------------------------------------------------------------- #

class _FakeSink:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, b) -> None:
        self.data.extend(b)

    def flush(self) -> None:
        pass


def _fake_writer(volume: float):
    """**绕开 ``__init__``**（它会真去 spawn aplay）—— 只测增益的数学。"""
    import numpy as np
    from wdnapps_audio_chat.cards.tts.service import AplayWriter
    w = object.__new__(AplayWriter)
    w._volume = float(volume)
    w._tempo = 1.0
    w._sample_rate = 24000
    w._alive = True
    w._sink = _FakeSink()
    w._upstream = None
    w._peak = 0.0
    w._raw_peak = 0.0
    w._gain = 1.0
    w._job_peak = 0.0
    w._written_sec = 0.0
    w._first_write_ts = 0.0
    w._last_write_ts = 0.0
    w._last_tick_ts = 0.0
    w._backlog_sec = 0.0
    w._play_pos = 0.0
    w._buf = bytearray()
    import threading
    w._cond = threading.Condition(threading.Lock())
    return w, np


def part_d() -> None:
    section("D. 自动增益 + 电平探针（问题 1：显示已播报但听不见）")
    vol = 0.05   # 产品默认 5%
    # ⚠️ **实测口径**：模型**原始**峰值约 0.31（用户那一轮 out_peak=0.0156
    # = 原始 0.31 × 音量 0.05）⇒ 归一化后的目标 = 0.9 × 0.05 = **0.045**。

    # ① **实测那条句子**（原始 0.31）⇒ 归一化后到 0.9 × 音量。
    w, np = _fake_writer(vol)
    mid = (np.sin(np.linspace(0, 6.28, 4800)) * 0.31).astype(np.float32)
    w.write(mid)
    out_peak = w._peak
    raw_peak = w._raw_peak
    check(abs(raw_peak - 0.31) < 1e-3,
          "原始峰值照记（%.4f ⇒ 模型输出本身的大小）" % raw_peak)
    check(abs(out_peak - 0.045) < 0.002,
          "实测那条句子归一化到 ≈0.045（-27 dBFS ⇒ 听得见），实测 %.4f" % out_peak)

    # ② **更安静的句子**（原始 0.05 ⇒ 需要 18 倍增益）⇒ 上限不能挡它。
    # ⚠️ **这条是第一版回归抓到的真 bug**：上限 4.0 ⇒ 增益被砍一半 ⇒
    # 输出只剩 0.01（-40 dBFS）⇒ **还是听不见**。
    w2, np2 = _fake_writer(vol)
    quiet = (np.sin(np.linspace(0, 6.28, 4800)) * 0.05).astype(np.float32)
    w2.write(quiet)
    check(abs(w2._peak - 0.045) < 0.002,
          "安静句子（原始 0.05）也归一化到 ≈0.045，实测 %.4f" % w2._peak)

    # ③ **响的句子** ⇒ 增益被压回来 ⇒ 两句**一样响**。
    w3, np3 = _fake_writer(vol)
    loud = (np.sin(np.linspace(0, 6.28, 4800)) * 0.90).astype(np.float32)
    w3.write(loud)
    check(abs(w3._peak - 0.045) < 0.002,
          "响句子也归一化到 ≈0.045（每句一样响），实测 %.4f" % w3._peak)

    # ④ **增益有上限**（防止「纯噪声」被放大成满量程）。
    from wdnapps_audio_chat.cards.tts.service import (
        AUTO_GAIN_MAX,
        AUTO_GAIN_TARGET,
    )
    w4, np4 = _fake_writer(1.0)
    tiny = (np.sin(np.linspace(0, 6.28, 2400)) * 1e-4).astype(np.float32)
    w4.write(tiny)
    check(w4._gain <= AUTO_GAIN_MAX + 1e-6,
          "增益不超过上限 %.0f（实测 %.2f）" % (AUTO_GAIN_MAX, w4._gain))
    check(abs(AUTO_GAIN_TARGET - 0.9) < 1e-6,
          "归一化目标是 0.9（留余量给软限幅）")

    # ⑤ **软限幅**：音量拉满时也不削波（``tanh`` 而不是 ``clip``）。
    w5, np5 = _fake_writer(1.0)
    hot = (np.sin(np.linspace(0, 6.28, 4800)) * 0.9).astype(np.float32)
    w5.write(hot)
    check(w5._peak <= 1.0 and w5._peak > 0.6,
          "音量 100%% 时输出 %.3f ∈ (0.6, 1.0]（软限幅不硬削）" % w5._peak)

    # ⑥ **逐句复位**：上一句的增益不能带到下一句。
    w6, np6 = _fake_writer(vol)
    w6.write(mid)
    w6.reset_peaks()
    check(w6._gain == 1.0 and w6._job_peak == 0.0,
          "reset_peaks() 把增益也复位（逐句独立）")


# --------------------------------------------------------------------------- #
# E. 静态断言（接线没漏）
# --------------------------------------------------------------------------- #

def part_e() -> None:
    section("E. 静态断言（前端 / API / 宿主桥的接线没漏）")
    app_js = os.path.join(
        _ROOT, ".venv", "src", "wdnapps-audio-chat", "src", "wdnapps_audio_chat",
        "cards", "qwen3_asr", "static", "app.js")
    api_py = os.path.join(
        _ROOT, ".venv", "src", "wdnapps-audio-chat", "src", "wdnapps_audio_chat",
        "cards", "qwen3_asr", "api.py")
    bridge_py = os.path.join(_ROOT, "app", "core", "audio", "tts_bridge.py")
    css_py = os.path.join(
        _ROOT, ".venv", "src", "wdnapps-audio-chat", "src", "wdnapps_audio_chat",
        "cards", "qwen3_asr", "static", "app.css")
    for p in (app_js, api_py, bridge_py, css_py):
        if not os.path.exists(p):
            check(False, "文件存在: %s" % p)
            return
    js = open(app_js, "r", encoding="utf-8").read()
    api = open(api_py, "r", encoding="utf-8").read()
    bridge = open(bridge_py, "r", encoding="utf-8").read()
    css = open(css_py, "r", encoding="utf-8").read()

    check("client_id: this._interpClientId()" in js,
          "前端：播放头上报带 client_id")
    check("client_id: this._interpClientId()," in js
          or "client_id: this._interpClientId()" in js,
          "前端：开始同传带 client_id")
    check("sessionStorage" in js, "前端：client id 存在 sessionStorage（刷新不变）")
    check("已暂停（媒体暂停了，播报跟着停）" in js,
          "前端：面板有「已暂停（播报跟着停）」的提示")
    check("last_out_peak" in js, "前端：电平探针读 last_out_peak（真送进 aplay 的）")
    check("clock_released" in js, "前端：兜底放行读队列侧的 clock_released")

    check("client_id" in api, "API：payload 里有 client_id")
    check("queue_set_media_state" in bridge, "宿主桥：queue_set_media_state 在")
    check(hasattr(_queue.TTSQueue, "set_media_state"),
          "队列：TTSQueue.set_media_state 存在")

    # ---- 前端的新行为（2026-11-26 用户两条要求）----
    check("_interpSyncCurrent" in js and
          "this._interpSyncCurrent(ph.sec)" in js,
          "前端：播放头 ⇒ 更新「当前正在播报的那一句」")
    check("_interpPickByTime" in js, "前端：按播放头找当前句的判据在")
    check("is-current" in js and "is-current" in css,
          "前端：高亮当前句的 class 与样式都在")
    check("正在播报" in js, "前端：角标有「正在播报」状态")
    check("_syncResultBoxes" in js and
          "interpHas" in js,
          "前端：底部结果框的显隐统一在一处（合并规则）")
    check("TR_REQ_MAX_ATTEMPTS" in js and
          ">= TR_REQ_MAX_ATTEMPTS" in js,
          "前端：行级翻译的重试**封顶**（别把算力烧光）")

    # ---- 「翻译没开」⇒ 立刻停手（2026-11-26 实测踩到）----
    #
    # ⚠️ 实测：服务重启后服务端开关复位成关，页面里的勾还开着 ⇒ 前端每
    #    5 秒重试一次、永远不停（~67 次/分 × 6 个标签页）⇒ 白白空转。
    # ⇒ 拒收时必须**把服务端的权威开关值拉回来** ⇒ 停手。
    check("_trSyncEnabledFromServer" in js,
          "前端：有「按服务端权威值停手」的函数")
    check(js.count("_trSyncEnabledFromServer(d)") >= 2,
          "前端：行级 + 句子级**两条**拒收路径都接上了停手")
    check('"enabled": False' in api or '"enabled": False,' in api,
          "API：拒收响应里带上服务端的权威 enabled 值")


def part_f() -> None:
    section("F. 宿主侧的 MT 判据（2026-11-25 补回：提示词复读防护）")
    # ⚠️ **这两个判据本来就在宿主**（插件一直 import 它们）—— 但**从没
    # 提交进宿主仓** ⇒ 插件的 try/except 把它吞了 ⇒ 「只有标点的碎片不翻」
    # 这道闸**整条是死的** ⇒ 模型把**指令**吐回来 ⇒ 上屏 + 被念出来。
    try:
        from app.core.translate.npu_provider import (
            _looks_like_instruction,
            detect_text_language,
            has_translatable_content,
            resolve_source_language,
        )
    except Exception as exc:  # noqa: BLE001
        check(False, "宿主 npu_provider 导出四个判据（%s）" % exc)
        return
    check(has_translatable_content("Hello world") is True, "有字母 ⇒ 该翻")
    check(has_translatable_content("你好，世界") is True, "有汉字 ⇒ 该翻")
    check(has_translatable_content("。") is False, "只有标点 ⇒ 不该翻")
    check(has_translatable_content("   ") is False, "空白 ⇒ 不该翻")
    check(has_translatable_content("") is False, "空 ⇒ 不该翻")
    check(has_translatable_content("😀") is False, "只有 emoji ⇒ 不该翻")
    check(has_translatable_content("2024") is False, "纯数字 ⇒ 不该翻")

    check(resolve_source_language("这是我的老婆。", "English", "en") == "zh-CN",
          "ASR 把中文判成 English ⇒ 校正回 zh-CN")
    check(resolve_source_language("这是我的老婆。", "zh-CN", "en") == "zh-CN",
          "本来就是中文 ⇒ 不动")
    check(resolve_source_language("Hello there", "English", "zh-CN") == "English",
          "英文文本 ⇒ 不动")

    check(_looks_like_instruction(
        "Translate the following text from zh to en:") is True,
        "认得出「Translate the following text …」")
    check(_looks_like_instruction("zh -> en:") is True, "认得出「zh -> en:」")
    check(_looks_like_instruction("Translate from Chinese to English") is True,
          "认得出「Translate from …」")
    check(_looks_like_instruction("This is a normal sentence.") is False,
        "正常句子不被误判")
    check(_looks_like_instruction("好的") is False, "中文正常句子不被误判")

    # ⚠️ ``detect_text_language`` **本来也在宿主**（插件的语种锁定判据
    # 一直 import 它）—— 也**从没提交** ⇒ 插件的 try/except 把它吞了 ⇒
    # 「自报语种与正文自不自洽」这道闸**整条是死的**。
    check(detect_text_language("这是我的老婆。") == "zh-CN", "汉字 ⇒ zh-CN")
    check(detect_text_language("こんにちは") == "ja", "假名 ⇒ ja")
    check(detect_text_language("안녕하세요") == "ko", "谚文 ⇒ ko")
    check(detect_text_language("Hello there") == "en", "拉丁字母 ⇒ en")
    check(detect_text_language("Привет") == "ru", "西里尔 ⇒ ru")
    check(detect_text_language("   ") == "", "空白 ⇒ 判不出")
    check(detect_text_language("") == "", "空 ⇒ 判不出")


def part_g() -> None:
    section("G. MT 的负缓存（同一句翻不出来 ⇒ 别再烧推理）")
    # ⚠️ **实测依据**（用户 2026-11-26 那一轮的行级翻译日志）：
    # 前端**每 5 秒重试一次、无上限** ⇒ 同一句话被重试了**几百次**
    # （「Translate the following text from zh to en:」338 次 /
    #  「己的概括能力。」174 次 / 「徐老师。」158 次…）⇒
    # ⇒ 每次都是**一次真推理** ⇒ **MT 的算力全烧在这 7 句上**
    # ⇒ ⇒ ASR/TTS 抢不到算力 ⇒ **译文越落越远**。
    try:
        from app.core.translate.npu_provider import (
            NpuQualityError,
            NpuTranslateProvider,
        )
    except Exception as exc:  # noqa: BLE001
        check(False, "导入 NpuTranslateProvider（%s）" % exc)
        return
    p = NpuTranslateProvider()
    calls = {"n": 0}

    def fake_infer(_session, _prompt):
        calls["n"] += 1
        # ⚠️ 模型把**指令**原样吐回来（实测最常见的失败形态）
        return ("Translate the following text from zh to en:\n徐老师。",
                {"n_decode_tokens": 1})

    p._infer = fake_infer
    text = "徐老师。"
    try:
        p.translate_one_fast(text, "zh-CN", "en", session=object())
        check(False, "第一次应当失败（模型复读指令）")
    except NpuQualityError:
        check(True, "第一次失败（模型复读指令）")
    n1 = calls["n"]
    check(n1 >= 1, "第一次真的调了推理（%d 次）" % n1)
    try:
        p.translate_one_fast(text, "zh-CN", "en", session=object())
        check(False, "第二次应当仍然失败")
    except NpuQualityError:
        check(True, "第二次仍然失败（质量失败是确定性的）")
    check(calls["n"] == n1,
          "第二次**没有**再推理（负缓存命中 ⇒ 推理次数 %d → %d）"
          % (n1, calls["n"]))
    # ⚠️ **换一句 ⇒ 照常推理**（缓存是按文本的）。
    try:
        p.translate_one_fast("另一句话", "zh-CN", "en", session=object())
    except NpuQualityError:
        pass
    check(calls["n"] > n1, "换一句 ⇒ 照常推理（缓存是按文本的）")
    # ⚠️ **别把正常的重复句误伤**：同一句**成功**过 ⇒ 不进负缓存。
    p2 = NpuTranslateProvider()
    ok_calls = {"n": 0}

    def ok_infer(_session, _prompt):
        ok_calls["n"] += 1
        return ("Teacher Xu, what does this \"close\" mean?", {"n_decode_tokens": 1})

    p2._infer = ok_infer
    outs = []
    for _ in range(3):
        outs.append(p2.translate_one_fast("徐老师。", "zh-CN", "en",
                                           session=object())[0])
    # ⚠️ **成功的翻译不进负缓存** ⇒ 每次都真推理（第一条措辞就中 ⇒ 1 次/回）
    check(ok_calls["n"] == 3,
          "同一句成功 3 次 ⇒ 每次都真推理（%d 次）⇒ 负缓存只挡失败"
          % ok_calls["n"])
    check(all(o and "徐" not in o for o in outs),
          "三次都真的翻出来了（不是被缓存挡住的）")


def part_h() -> None:
    section("H. 「翻译没开」⇒ 立刻停手（2026-11-26 实测）")
    # ⚠️ **实测踩到的**（2026-11-26）：服务重启后服务端开关复位成关，而
    #    页面里的勾还开着 ⇒ 前端每 5 秒重试一次、永远不停
    #    （~67 次/分 × 6 个标签页）⇒ 白白空转。
    #
    # ⚠️ **静态断言只能证明「接线在」** ⇒ 这里**真的把那段 JS 跑起来**
    #    （node）⇒ 证明「拒收 ⇒ 本地开关被拉回来 ⇒ 停手」。
    #
    # ⚠️ 不用浏览器：这段逻辑是**纯函数**（改状态 + 同步 UI）⇒ 抽出来
    #    用 node 求值即可 ⇒ **不占 NPU、不碰用户的会话**。
    app_js = os.path.join(
        _ROOT, ".venv", "src", "wdnapps-audio-chat", "src", "wdnapps_audio_chat",
        "cards", "qwen3_asr", "static", "app.js")
    if not os.path.exists(app_js):
        check(False, "文件存在: %s" % app_js)
        return
    src = open(app_js, "r", encoding="utf-8").read()
    # 抽出这个方法（连同它的注释）—— 它是**纯逻辑**，不依赖 DOM。
    # ⚠️ **按大括号配平抽方法体**（不能用正则贪/惰匹配 —— 方法体里有
    #    嵌套的 ``{}`` ⇒ 正则会截断在第一个 ``}`` ⇒ 语法错误）。
    start = src.find("  _trSyncEnabledFromServer(d) {")
    if start < 0:
        check(False, "抽出 _trSyncEnabledFromServer 的实现")
        return
    i = src.find("{", start)
    depth = 0
    end = -1
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
        i += 1
    if end < 0:
        check(False, "抽出 _trSyncEnabledFromServer 的实现（大括号没配平）")
        return
    body = src[start:end]
    # ⚠️ **只取方法体本身**（注释里可能有很长的中文）⇒ 传给 node 的
    #    参数才不会超长（``node -e`` 走 argv ⇒ 有长度上限）。
    code_only = "\n".join(
        ln for ln in body.splitlines()
        if not ln.strip().startswith("//"))
    node = shutil.which("node")
    if not node:
        # ⚠️ 板端不一定有 node ⇒ 降级成「静态断言」，别让整个套件红。
        check("this._tr.enabled = srv;" in body,
              "静态：拒收 ⇒ 本地开关被拉回服务端的值（node 缺失，跳过行为验证）")
        return

    # ⚠️ 构造一个最小宿主：``this._tr`` + ``this._trSyncUi``（打桩）。
    # ⚠️ 方法体是**对象方法**写法（``_trSyncEnabledFromServer(d) { … }``）
    #    ⇒ 要包进一个对象字面量里才能在 node 里求值。
    harness = """
const __impl = {
%s
};
const self_ = {
  _tr: { enabled: true },
  _trSyncUi() { self_.synced = (self_.synced || 0) + 1; },
  _trSyncEnabledFromServer: __impl._trSyncEnabledFromServer,
};
// ⚠️ 用 ``self_`` 当 ``this``（方法体里的 ``this`` 就是它）。
self_._trSyncEnabledFromServer({ ok: false, skipped: "翻译没开", enabled: false });
const afterReject = self_._tr.enabled;
// ⚠️ 服务端说「开着」⇒ 不该把用户的勾关掉。
self_._tr.enabled = true;
self_._trSyncEnabledFromServer({ ok: true, enabled: true });
const afterOk = self_._tr.enabled;
// ⚠️ 服务端**没给**权威值 ⇒ 不猜（保持原样）。
self_._tr.enabled = true;
self_._trSyncEnabledFromServer({ ok: false, skipped: "翻译没开" });
const afterNoField = self_._tr.enabled;
console.log(JSON.stringify({ afterReject, afterOk, afterNoField,
                             synced: self_.synced || 0 }));
""" % code_only
    # ⚠️ 走**临时文件**而不是 ``node -e``（argv 长度上限）。
    tmp_path = os.path.join(
        tempfile.gettempdir(), "wdnwebui_tr_stop_harness.js")
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(harness)
        out = subprocess.run(
            [node, tmp_path], capture_output=True, text=True, timeout=30)
    except Exception as exc:  # noqa: BLE001
        check(False, "跑 node（%s）" % exc)
        return
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    if out.returncode != 0:
        check(False, "node 执行成功（stderr: %s）" % (out.stderr or "")[:200])
        return
    try:
        r = json.loads((out.stdout or "").strip().splitlines()[-1])
    except Exception as exc:  # noqa: BLE001
        check(False, "解析 node 输出（%s）: %r" % (exc, (out.stdout or "")[:200]))
        return
    check(r.get("afterReject") is False,
          "拒收（enabled=False）⇒ 本地开关被关掉 ⇒ 停手")
    check(r.get("afterOk") is True,
          "服务端说开着 ⇒ 不动用户的勾")
    check(r.get("afterNoField") is True,
          "服务端没给权威值 ⇒ 不猜（保持原样）")
    check(int(r.get("synced") or 0) >= 1,
          "状态变了 ⇒ 勾选框被同步（用户看得见服务端把它关了）")


def main() -> int:
    print("\033[1m同传三个问题（2026-11-25）的回归\033[0m")
    part_a()
    part_a2()
    part_b()
    part_c()
    part_d()
    part_e()
    part_f()
    part_g()
    part_h()
    print("\n" + "=" * 62)
    if _FAIL:
        print("\033[31mFAILED\033[0m  %d 通过 / %d 失败" % (_OK, len(_FAIL)))
        for f in _FAIL:
            print("  - %s" % f)
        return 1
    print("\033[32mALL PASS\033[0m  %d 条断言全过" % _OK)
    return 0


if __name__ == "__main__":
    sys.exit(main())
