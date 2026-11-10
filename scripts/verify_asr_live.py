#!/usr/bin/env python3
"""Qwen3-ASR 实时链路的**契约级**回归。

**不需要 NPU、不需要服务在跑** —— 它用一只**假 SDK session** 直接驱动
:class:`wdnapps_audio_chat.cards.qwen3_asr.service._LiveAsrSession`，把「协议契约」钉死。

假 session 模仿 SDK 的在线路径（:meth:`wdnai.backends.asr.ASRBackend.
_run_online_stream`）的切块规则：

* 按**累计采样数**切 1 秒块（调用方怎么切块无关）；
* 只有**最后被执行的块**才带 ``is_last``；
* 收尾那次调用若带了尾巴，尾巴才是最后一块。

断言清单
========

=====  ============================================================
L1    收尾那次调用**必须携带本会话的最后一块**（否则末尾 token
      不会被确认 —— SDK 会警告「最后 N 个 token 未确认」）
L2    实时路径切出来的块与「文件路径」**逐块相同**（→ 文本逐字节等价）
L3    服务端缓冲满 → 丢**最旧**的块 + 广播 ``gap`` 帧
L4    seq 缺号 → 记丢块 + 广播 ``gap`` 帧
L5    连续丢 5 块 → **熔断**（会话报错结束，不静默产出错误文本）
L6    端点命中 → 广播 ``endpoint`` 帧 + **自动开新 SDK 会话**
L7    纯静音 30 s → 强制结束，**不**产 ``endpoint`` 帧
L8    会话上限 → 强制重置
L9    ``stop()`` 释放会话（不再回包）
L10   静音**照样**喂给模型（端点只决定何时收尾）
L11   SDK ``final_text()`` 的三种返回形状都要被正确拆解
      （``language X\n正文`` / 纯语言头 / 语言头粘在正文前）
L12   停滞检测：已确认文本连续 N 轮不变 → 计数达标（触发切段）；
      文本增长 → 计数归零
L13   模型侧停顿检测：假设文本**内容**连续相同 → 计数达标；
      **长度相同但内容变了** → 计数归零（v0.32 的关键修正）
L14   自适应声学停顿检测：相对「最近 N 秒电平中位数 - 12 dB」判定；
      持续安静**不会**一直判成停顿（滑动中位数会跟着降）
=====  ============================================================

用法::

    .venv/bin/python scripts/verify_asr_live.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import numpy as np  # noqa: E402

from wdnapps_audio_chat.cards.qwen3_asr import service as svc_mod  # noqa: E402
from wdnapps_audio_chat.cards.qwen3_asr.service import (  # noqa: E402
    _LiveAsrManager, _LiveAsrSession, get_asr_service)

PASSED = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}" + (f"  ({detail})" if detail else ""))


# --------------------------------------------------------------------------- #
# 假 SDK session —— 模仿 SDK 的在线切块规则
# --------------------------------------------------------------------------- #
class FakeSdkSession:
    """假 SDK session：按 1 秒网格切块，记录每一块的 (流, 是否最后, 采样数)。"""

    SPC = 16000

    def __init__(self) -> None:
        self.blocks: list[tuple[str, bool, int]] = []
        self.calls: list[dict] = []
        self.streams: dict[str, dict] = {}
        self.final_calls_without_audio = 0

    async def respond(self, attachment=None, sample_rate=16000, n_channels=1,
                      sample_width=2, on_delta=None, **opts):
        pcm = attachment[0].data
        sid = opts.get("asr_stream_id")
        final = bool(opts.get("asr_stream_final"))
        origin = opts.get("asr_stream_origin_sec")
        self.calls.append({"sid": sid, "final": final, "origin": origin,
                           "nbytes": len(pcm)})
        st = self.streams.setdefault(
            sid, {"pending": 0, "idx": 0, "samples": 0})
        n = len(pcm) // 2
        if n == 0 and final:
            self.final_calls_without_audio += 1
        st["pending"] += n
        st["samples"] += n
        while st["pending"] >= self.SPC:
            st["pending"] -= self.SPC
            st["idx"] += 1
            self.blocks.append((sid, False, self.SPC))
            if on_delta is not None:
                on_delta(json.dumps({
                    "type": "asr_round", "round": st["idx"],
                    "committed": "seg{}".format(st["idx"]), "unfixed": "",
                    "language": "English",
                    "chunk_start_sec": float(st["idx"] - 1),
                    "chunk_samples": self.SPC,
                }))
        if not final:
            return "partial"
        # 收尾：尾巴才是最后一块。
        if st["pending"] > 0:
            self.blocks.append((sid, True, st["pending"]))
            st["pending"] = 0
        else:
            # 最后一块是整块 → 把上一条补成 last
            for i in range(len(self.blocks) - 1, -1, -1):
                if self.blocks[i][0] == sid:
                    self.blocks[i] = (sid, True, self.blocks[i][2])
                    break
        text = "final:{}".format(sid)
        if on_delta is not None:
            on_delta(json.dumps({"type": "asr_final", "language": "English",
                                 "text": text}))
        self.streams.pop(sid, None)
        return text


# --------------------------------------------------------------------------- #
def make_session(mgr, fake, **kw):
    """建一个接了假 session 的实时会话。"""
    s = mgr.create(source="browser", sample_rate=16000, n_channels=1,
                   sample_width=2, chunk_ms=500, **kw)
    s._session = fake
    return s


def collect_frames(sub_q):
    """把订阅队列里的帧取干净。"""
    out = []
    while True:
        try:
            out.append(sub_q.get_nowait())
        except asyncio.QueueEmpty:
            return out


def tone(sec: float, sr: int = 16000, amp: float = 0.4) -> bytes:
    n = int(sr * sec)
    t = np.arange(n) / sr
    return (np.sin(2 * np.pi * 440 * t) * amp * 32767).astype("<i2").tobytes()


def silence(sec: float, sr: int = 16000) -> bytes:
    return np.zeros(int(sr * sec), dtype="<i2").tobytes()


def file_path_blocks(pcm: bytes, spc: int = 16000):
    """文件路径的切块（SDK 的 ``_run_online`` 规则）。"""
    n = len(pcm) // 2
    out = []
    i = 0
    while i < n:
        take = min(spc, n - i)
        out.append(("file", i + take >= n, take))
        i += take
    return out


# --------------------------------------------------------------------------- #
async def t_l1_l2_blocks_match_file_path() -> None:
    """L1 + L2：收尾必须带最后一块；块序列与文件路径逐块相同。"""
    print("\n=== L1/L2: 块边界与文件路径一致 ===")
    mgr = _LiveAsrManager()
    fake = FakeSdkSession()
    s = make_session(mgr, fake)
    await s.start()

    # 3.5 秒音频 → 500 ms 一块
    pcm = tone(3.5)
    for i in range(0, len(pcm), 16000):
        await s.submit(pcm[i:i + 16000], seq=i // 16000)
    res = await s.finish()
    check("L1 收尾返回了最终文本", bool(res.get("text")), str(res)[:120])
    check("L1 收尾那次调用带了音频",
          fake.calls and fake.calls[-1]["nbytes"] > 0,
          "calls={}".format(fake.calls[-3:]))
    check("L1 收尾那次调用带了 final=True",
          fake.calls and fake.calls[-1]["final"] is True)
    check("L1 假 session 没收到「空收尾」",
          fake.final_calls_without_audio == 0,
          "count={}".format(fake.final_calls_without_audio))

    live = [(b[2], b[1]) for b in fake.blocks]
    ref = [(b[2], b[1]) for b in file_path_blocks(pcm)]
    check("L2 实时路径的块序列 ≡ 文件路径", live == ref,
          "live={} ref={}".format(live, ref))
    check("L2 最后一块带 is_last", live and live[-1][1] is True, str(live[-1:]))


async def t_l2b_whole_second_exact() -> None:
    """L2 的边界用例：总长正好是整秒 → 末块必须带 is_last。"""
    print("\n=== L2b: 总长正好整秒（最容易丢末尾 token 的情形）===")
    mgr = _LiveAsrManager()
    fake = FakeSdkSession()
    s = make_session(mgr, fake)
    await s.start()
    pcm = tone(2.0)                      # 正好 2 秒
    for i in range(0, len(pcm), 16000):
        await s.submit(pcm[i:i + 16000], seq=i // 16000)
    await s.finish()
    live = [(b[2], b[1]) for b in fake.blocks]
    ref = [(b[2], b[1]) for b in file_path_blocks(pcm)]
    check("L2b 整秒音频的块序列 ≡ 文件路径", live == ref,
          "live={} ref={}".format(live, ref))
    check("L2b 整秒音频的末块带 is_last", live[-1][1] is True, str(live[-1:]))
    check("L2b 没有「空收尾」调用",
          fake.final_calls_without_audio == 0)


async def t_l3_server_buffer_overflow() -> None:
    """L3：服务端缓冲满 → 丢最旧 + gap 帧。"""
    print("\n=== L3: 服务端有界缓冲（丢最旧）===")
    mgr = _LiveAsrManager()
    fake = FakeSdkSession()
    s = make_session(mgr, fake)
    sub_q: asyncio.Queue = asyncio.Queue()
    get_asr_service().register_subscriber(sub_q)
    await s.start()
    collect_frames(sub_q)

    # 并发塞 6 块（缓冲上限 3）→ 最旧的被丢
    pending = [asyncio.create_task(s.submit(tone(0.5), seq=i))
               for i in range(6)]
    await asyncio.gather(*pending, return_exceptions=True)
    frames = collect_frames(sub_q)
    gaps = [f for f in frames if f.get("type") == "gap"]
    check("L3 缓冲满时广播了 gap 帧", bool(gaps), "frames={}".format(
        [f.get("type") for f in frames]))
    check("L3 gap 帧带 from_seq / to_seq",
          bool(gaps) and "from_seq" in gaps[0] and "to_seq" in gaps[0],
          str(gaps[:1]))
    check("L3 丢了块（dropped_chunks > 0）", s.dropped_chunks > 0,
          "dropped={}".format(s.dropped_chunks))
    await s.stop()
    get_asr_service().unregister_subscriber(sub_q)


async def t_l4_seq_gap() -> None:
    """L4：seq 缺号 → 记丢块 + gap 帧。"""
    print("\n=== L4: seq 缺号（采集端丢块）===")
    mgr = _LiveAsrManager()
    fake = FakeSdkSession()
    s = make_session(mgr, fake)
    sub_q: asyncio.Queue = asyncio.Queue()
    get_asr_service().register_subscriber(sub_q)
    await s.start()
    collect_frames(sub_q)

    await s.submit(tone(0.5), seq=0)
    await s.submit(tone(0.5), seq=3)      # 1、2 丢了
    frames = collect_frames(sub_q)
    gaps = [f for f in frames if f.get("type") == "gap"]
    check("L4 seq 缺号广播了 gap 帧", bool(gaps), "frames={}".format(
        [f.get("type") for f in frames]))
    check("L4 gap 帧报的是缺失区间 1..2",
          bool(gaps) and gaps[0]["from_seq"] == 1 and gaps[0]["to_seq"] == 2,
          str(gaps[:1]))
    check("L4 dropped_chunks 记了 2",
          s.dropped_chunks == 2, "dropped={}".format(s.dropped_chunks))
    await s.stop()
    get_asr_service().unregister_subscriber(sub_q)


async def t_l5_circuit_breaker() -> None:
    """L5：连续丢 5 块 → 熔断（报错结束）。"""
    print("\n=== L5: 连续丢块熔断 ===")
    mgr = _LiveAsrManager()
    fake = FakeSdkSession()
    s = make_session(mgr, fake)
    sub_q: asyncio.Queue = asyncio.Queue()
    get_asr_service().register_subscriber(sub_q)
    await s.start()
    collect_frames(sub_q)

    await s.submit(tone(0.5), seq=0)
    # 一次跳 5 → 连续丢 5 块 → 熔断
    res = await s.submit(tone(0.5), seq=6)
    frames = collect_frames(sub_q)
    errs = [f for f in frames if f.get("type") == "error"]
    check("L5 熔断广播了 error 帧", bool(errs), "frames={}".format(
        [f.get("type") for f in frames]))
    check("L5 熔断后返回体带 error", bool(res.get("error")), str(res)[:120])
    check("L5 熔断后会话已关闭", s.closed)
    await s.stop()
    get_asr_service().unregister_subscriber(sub_q)


async def t_l6_endpoint() -> None:
    """L6：端点命中 → endpoint 帧 + 自动开新 SDK 会话。"""
    print("\n=== L6: 端点检测（自动切句）===")
    mgr = _LiveAsrManager()
    fake = FakeSdkSession()
    s = make_session(mgr, fake)
    sub_q: asyncio.Queue = asyncio.Queue()
    get_asr_service().register_subscriber(sub_q)
    await s.start()
    collect_frames(sub_q)

    await s.submit(tone(1.0), seq=0)          # 说话
    for i in range(3):                        # 1.5 s 静音 → 端点
        await s.submit(silence(0.5), seq=1 + i)
    frames = collect_frames(sub_q)
    eps = [f for f in frames if f.get("type") == "endpoint"]
    check("L6 端点命中广播了 endpoint 帧", bool(eps), "frames={}".format(
        [f.get("type") for f in frames]))
    check("L6 endpoint 帧带 text / index",
          bool(eps) and "text" in eps[0] and "index" in eps[0], str(eps[:1]))
    sids = {sid for sid, _, _ in fake.blocks}
    check("L6 端点后开了新的 SDK 会话（asr_stream_id 变了）",
          s._sdk_stream_id.endswith("#2"),
          "sdk_stream_id={} blocks={}".format(s._sdk_stream_id, sids))
    check("L6 静音照样喂给了模型（块里有静音段）",
          len(fake.blocks) >= 2, "blocks={}".format(len(fake.blocks)))
    await s.stop()
    get_asr_service().unregister_subscriber(sub_q)


async def t_l7_pure_silence() -> None:
    """L7：纯静音 30 s → 强制结束，**不**产 endpoint 帧。"""
    print("\n=== L7: 纯静音超时 ===")
    mgr = _LiveAsrManager()
    fake = FakeSdkSession()
    s = make_session(mgr, fake)
    sub_q: asyncio.Queue = asyncio.Queue()
    get_asr_service().register_subscriber(sub_q)
    await s.start()
    collect_frames(sub_q)

    for i in range(60):                       # 60 × 0.5 s = 30.0 s
        await s.submit(silence(0.5), seq=i)
    frames = collect_frames(sub_q)
    eps = [f for f in frames if f.get("type") == "endpoint"]
    dones = [f for f in frames if f.get("type") == "live_done"]
    check("L7 纯静音不产 endpoint 帧", not eps, "eps={}".format(eps[:1]))
    check("L7 纯静音 30 s 后强制结束", bool(dones), "frames={}".format(
        [f.get("type") for f in frames]))
    check("L7 结束原因是 silence_timeout",
          bool(dones) and dones[0].get("reason") == "silence_timeout",
          str(dones[:1]))
    check("L7 强制结束后会话关闭", s.closed)
    await s.stop()
    get_asr_service().unregister_subscriber(sub_q)


async def t_l8_session_limit() -> None:
    """L8：会话上限 → 强制重置。"""
    print("\n=== L8: 会话时长上限 ===")
    old = svc_mod._LIVE_MAX_SESSION_SEC
    svc_mod._LIVE_MAX_SESSION_SEC = 2.0        # 临时改成 2 s 便于测
    try:
        mgr = _LiveAsrManager()
        fake = FakeSdkSession()
        s = make_session(mgr, fake)
        sub_q: asyncio.Queue = asyncio.Queue()
        get_asr_service().register_subscriber(sub_q)
        await s.start()
        collect_frames(sub_q)
        for i in range(5):                     # 5 × 0.5 s = 2.5 s
            await s.submit(tone(0.5), seq=i)
        frames = collect_frames(sub_q)
        dones = [f for f in frames if f.get("type") == "live_done"]
        check("L8 达到会话上限后强制重置", bool(dones), "frames={}".format(
            [f.get("type") for f in frames]))
        check("L8 结束原因是 session_limit",
              bool(dones) and dones[0].get("reason") == "session_limit",
              str(dones[:1]))
        await s.stop()
        get_asr_service().unregister_subscriber(sub_q)
    finally:
        svc_mod._LIVE_MAX_SESSION_SEC = old


async def t_l9_stop_releases() -> None:
    """L9：stop() 释放会话。"""
    print("\n=== L9: stop() 释放会话 ===")
    mgr = _LiveAsrManager()
    fake = FakeSdkSession()
    s = make_session(mgr, fake)
    await s.start()
    await s.submit(tone(0.5), seq=0)
    res = await s.stop()
    check("L9 stop() 返回 ok", res.get("ok") is True, str(res))
    check("L9 stop() 后会话关闭", s.closed)
    try:
        await s.submit(tone(0.5), seq=1)
        check("L9 关闭后不再接受块", False, "submit 没抛异常")
    except RuntimeError:
        check("L9 关闭后不再接受块", True)


async def t_l10_silence_is_fed() -> None:
    """L10：静音照样喂给模型（端点只决定何时收尾）。"""
    print("\n=== L10: 静音仍然喂给模型 ===")
    mgr = _LiveAsrManager()
    fake = FakeSdkSession()
    s = make_session(mgr, fake)
    await s.start()
    await s.submit(tone(1.0), seq=0)
    await s.submit(silence(0.5), seq=1)
    # 收尾会把喂送缓冲里的尾巴一起交给 SDK —— 于是**全部**音频都进过模型。
    await s.finish()
    total = sum(b[2] for b in fake.blocks)
    check("L10 静音也进了模型（累计采样 = 1.5 s）",
          total == 24000, "samples={}".format(total))
    await s.stop()


# --------------------------------------------------------------------------- #
async def t_l11_language_header_shapes() -> None:
    """L11: SDK ``final_text()`` 的三种返回形状都要被正确拆解。

    SDK（``asr.py::_OnlineDecoder.final_text``）有**三种**返回形状：

    1. ``"language Chinese\\n<正文>"`` —— 正常形状（committed 里出现了
       语言分隔符 ``[PAD151704]``）；
    2. ``"language None"`` / ``"language Chinese"`` —— committed 里**还没
       出现分隔符**，原样返回已提交 token 的文本 → 只有语言头、**没有
       正文**；
    3. ``"languageThe second, let's start."`` —— 分隔符那个 token 落在
       **回退区**（没进 committed）→ 语言头直接**粘在正文前面**。

    早先只处理了形状 1：形状 2 会往转写里塞一句 ``language None``，
    形状 3 会让正文以 ``language`` 开头（实测在 97 分钟视频里出现过）。
    """
    print("\n=== L11: 语言头三种形状的拆解 ===")
    from wdnapps_audio_chat.cards.qwen3_asr.service import _split_language, _valid_language

    def split(raw: str) -> tuple:
        lang, body = _split_language(raw)
        return _valid_language(lang), body

    check("L11 正常形状：'language Chinese\\n正文'",
          split("language Chinese\n本节目由蚂蚁财富黑卡独家冠名播出。")
          == ("Chinese", "本节目由蚂蚁财富黑卡独家冠名播出。"),
          repr(split("language Chinese\n本节目由蚂蚁财富黑卡独家冠名播出。")))
    check("L11 无正文形状：'language None' → 正文为空",
          split("language None") == ("", ""), repr(split("language None")))
    check("L11 无正文形状：'language None\\n' → 正文为空",
          split("language None\n") == ("", ""), repr(split("language None\n")))
    check("L11 无正文形状：'language Chinese' → 正文为空",
          split("language Chinese") == ("", ""),
          repr(split("language Chinese")))
    check("L11 粘连形状：'languageThe second…' → 剥掉语言头",
          split("languageThe second, let's start. Let's start with it.")
          == ("", "The second, let's start. Let's start with it."),
          repr(split("languageThe second, let's start. Let's start with it.")))
    check("L11 普通正文原样返回",
          split("本节目由蚂蚁财富黑卡独家冠名播出。")
          == ("", "本节目由蚂蚁财富黑卡独家冠名播出。"),
          repr(split("本节目由蚂蚁财富黑卡独家冠名播出。")))


# --------------------------------------------------------------------------- #
async def t_l12_stall_detector() -> None:
    """L12: 停滞检测的状态机（确定性，不需要模型）。

    背景（见 ``docs/QWEN3_ASR_LIVE_MIC_LANDING_CHECKLIST.md`` §8.8.3）：
    SDK 的回退策略是 ``stable_count = len(round_tokens) - 5``。当某一秒
    音频「没有新内容」时，模型只吐那 5 个回退 token → ``stable_count == 0``
    → **一个 token 都不提交** → 下一轮输入完全相同 → 输出相同 → **永久
    自锁**（实测 97 分钟视频：t=26 s 起冻结 395 秒）。

    主进程的兜底是「已确认文本连续 N 轮长度不变 → 切 epoch 换新会话」。
    这个状态机**必须**是确定性的 —— 端到端能不能触发取决于模型在静音段
    会不会幻觉出文本（概率性），所以机制本身在这里单独钉死。

    信号：``_AsrManager._on_delta`` 收到的每轮 ``committed`` 长度。
    """

    print("\n=== L12: 停滞检测状态机 ===")
    from wdnapps_audio_chat.cards.qwen3_asr.service import (_STALL_ROUNDS_LIMIT, _AsrManager)

    mgr = _AsrManager()

    def _round(committed: str, n: int) -> str:
        return json.dumps({"type": "asr_round", "round": n,
                           "committed": committed, "unfixed": "",
                           "language": ""})

    # 连续 N 轮「已确认文本长度不变」→ 计数达标（= 会触发切段）
    #
    # 注意计数语义：第 1 轮是**基线**（把基准长度定下来），从第 2 轮起
    # 每轮「长度与上一轮相同」计 1 → **喂 N+1 轮**才够 N。
    for i in range(_STALL_ROUNDS_LIMIT + 1):
        mgr._on_delta(_round("abc", i + 1))
    check("L12 连续 {} 轮文本不变 → 停滞计数达标".format(_STALL_ROUNDS_LIMIT),
          mgr._stall_rounds >= _STALL_ROUNDS_LIMIT,
          "stall_rounds={} limit={}".format(mgr._stall_rounds, _STALL_ROUNDS_LIMIT))

    # 文本增长 → 计数归零（模型没卡住就不该切段）
    mgr._on_delta(_round("abcdef", 100))
    check("L12 文本增长 → 停滞计数归零",
          mgr._stall_rounds == 0,
          "stall_rounds={}".format(mgr._stall_rounds))

    # 长度**变短**也算「变了」—— 切 epoch 后 decoder 重置，长度会变小
    mgr._on_delta(_round("abcdef", 101))
    mgr._on_delta(_round("ab", 102))
    check("L12 长度变短也归零（切 epoch 后 committed 会重置）",
          mgr._stall_rounds == 0,
          "stall_rounds={}".format(mgr._stall_rounds))


# --------------------------------------------------------------------------- #
async def t_l13_model_pause_detector() -> None:
    """L13: 模型侧停顿检测的状态机（确定性，不需要模型）。

    背景（v0.32）：切 SDK 会话的切口如果落在句子中间，新会话就是
    「零上下文 + 半个句子」→ 只能转写出语法不通的碎片（实测英文
    "In the past, so of the context."）。修法是**把切口对齐到说话停顿**。

    停顿信号 = 模型的假设文本（``unfixed``）**内容连续相同**。

    ``unfixed`` 是**当前轮输出的尾巴**（SDK ``asr.py:1076``：
    ``new_unfixed = round_tokens[stable_count:]``，即回退末尾那 5 个
    token）：人在说话 → 前沿 token 每轮都变；人停下 → 模型没新东西可说，
    把同一段再吐一遍 → 尾巴**一字不差**。

    ⚠️ **必须比「内容」而不是「长度」**：最初实现比长度，实测中文
    《圆桌派》150 秒里「等长」出现 21 次，但只有 1 次内容也相同，其余
    20 次都是模型在改写 → 在句子中间误切一刀。
    """
    print("\n=== L13: 模型侧停顿检测状态机 ===")
    from wdnapps_audio_chat.cards.qwen3_asr.service import _PAUSE_ROUNDS_LIMIT, _AsrManager

    mgr = _AsrManager()

    def _round(committed: str, unfixed: str, n: int) -> str:
        return json.dumps({"type": "asr_round", "round": n,
                           "committed": committed, "unfixed": unfixed,
                           "language": ""})

    # 连续 N 轮「尾巴内容一字不差」→ 计数达标（= 会话可以在这里切）
    for i in range(_PAUSE_ROUNDS_LIMIT + 1):
        mgr._on_delta(_round("abc", " model frontier.", i + 1))
    check("L13 连续 {} 轮假设文本内容相同 → 停顿计数达标".format(
        _PAUSE_ROUNDS_LIMIT),
        mgr._pause_hit,
        "pause_rounds={} limit={} hit={}".format(
            mgr._pause_rounds, _PAUSE_ROUNDS_LIMIT, mgr._pause_hit))

    # ⚠️ **长度相同但内容变了** → 必须归零（模型在改写，不是停顿）
    mgr._on_delta(_round("abcd", " model frontierX", 50))
    check("L13 长度相同但内容变了 → 停顿计数归零（模型在改写）",
          not mgr._pause_hit and mgr._pause_rounds == 1,
          "pause_rounds={} hit={}".format(mgr._pause_rounds, mgr._pause_hit))

    # 尾巴变空 → 归零（模型没吐出任何东西）
    mgr._on_delta(_round("abcde", "", 51))
    check("L13 假设文本为空 → 停顿计数归零",
          not mgr._pause_hit and mgr._pause_rounds == 0,
          "pause_rounds={} hit={}".format(mgr._pause_rounds, mgr._pause_hit))


# --------------------------------------------------------------------------- #
async def t_l14_adaptive_pause_detector() -> None:
    """L14: 自适应声学停顿检测（确定性，不需要模型 / 音频设备）。

    背景（v0.32）：绝对能量门限切不出真实视频里的停顿 —— 实测
    《圆桌派》（**有背景音乐**）每秒 RMS 中位数 -23.6 dBFS，150 秒里
    低于 -30 dBFS 的只有 2 秒。相对门限（最近 N 秒中位数 - 12 dB）
    才能同时覆盖「安静录音」和「带背景音乐的访谈」。

    关键不变式：**持续安静不会一直判成停顿** —— 滑动中位数会跟着降，
    相对关系保持住（否则一段长静音会被切成很多段）。
    """
    print("\n=== L14: 自适应声学停顿检测 ===")
    from app.core.audio.endpoint import AdaptivePauseDetector

    def _tone(dbfs: float, n: int = 1) -> list:
        """一串 1 秒的「电平块」（直接喂 dB，不走 PCM）。"""
        return [dbfs] * n

    # 1) 持续响 → 一秒都不该判停顿
    det = AdaptivePauseDetector(drop_db=12.0, window_sec=30.0,
                                min_history_sec=5.0)
    hits = [i for i, db in enumerate(_tone(-20.0, 40))
            if det.feed(db, 1.0)]
    check("L14 持续说话（-20 dBFS）→ 一次停顿都不判",
          not hits, "命中秒={}".format(hits[:10]))

    # 2) 响 20 秒 + 静 1 秒 → 只有那 1 秒判停顿
    det = AdaptivePauseDetector(drop_db=12.0, window_sec=30.0,
                                min_history_sec=5.0)
    hits = []
    for i in range(40):
        db = -45.0 if i == 20 else -20.0
        if det.feed(db, 1.0):
            hits.append(i)
    check("L14 响 20 秒后接 1 秒安静 → 只命中那一秒",
          hits == [20], "命中秒={}".format(hits))

    # 3) 持续安静 40 秒 → **不能**一直判停顿（中位数会跟着降）
    det = AdaptivePauseDetector(drop_db=12.0, window_sec=30.0,
                                min_history_sec=5.0)
    hits = [i for i in range(40) if det.feed(-50.0, 1.0)]
    check("L14 持续安静 40 秒 → 不会一直判停顿（中位数跟着降）",
          not hits, "命中秒数={}".format(len(hits)))

    # 4) 历史太短 → 不判（开头的电平样本不够，中位数不可信）
    det = AdaptivePauseDetector(drop_db=12.0, window_sec=30.0,
                                min_history_sec=5.0)
    hits = [i for i in range(4) if det.feed(-60.0, 1.0)]
    check("L14 历史不足 5 秒 → 不判停顿",
          not hits, "命中秒={}".format(hits))

    # 5) dB ↔ 线性幅度换算没写错（-6 dB ≈ 幅度减半）
    det = AdaptivePauseDetector(drop_db=12.0, window_sec=30.0,
                                min_history_sec=5.0)
    for _ in range(10):
        det.feed(-20.0, 1.0)
    det.feed(-20.0 - 6.0206, 1.0)
    check("L14 -6 dB ≈ 幅度减半（相对门限按 dB 算）",
          abs(det.median_dbfs - (-20.0)) < 1.0,
          "median={:.2f}".format(det.median_dbfs))


# --------------------------------------------------------------------------- #
async def main() -> int:
    print("=== Qwen3-ASR 实时链路契约回归 ===")
    for fn in (t_l1_l2_blocks_match_file_path, t_l2b_whole_second_exact,
               t_l3_server_buffer_overflow, t_l4_seq_gap,
               t_l5_circuit_breaker, t_l6_endpoint, t_l7_pure_silence,
               t_l8_session_limit, t_l9_stop_releases, t_l10_silence_is_fed,
               t_l11_language_header_shapes, t_l12_stall_detector,
               t_l13_model_pause_detector, t_l14_adaptive_pause_detector):
        await fn()
    print(f"\n=== {PASSED} 通过 / {len(FAILED)} 失败 ===")
    for f in FAILED:
        print(f"  ✗ {f}")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
