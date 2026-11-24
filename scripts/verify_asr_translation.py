#!/usr/bin/env python3
"""回归：ASR 卡的**实时翻译**（流式同传，第一期）。

覆盖三块
========

1. **manifest 守卫** —— ``software_capabilities`` 必须**仍然只有**
   ``swcap_audio_asr``。

   ⚠️ 为什么这条要单独守着：``framework.js`` 的 ``_capsAllLocalRunning()``
   是 ``caps.every(...)`` —— 声明的**每一个** cap 都得本机 running，卡片
   才会被写进 ``compute.active_app``、cap 才会增量写进
   ``compute.autostart``。⇒ 有人「顺手」把 ``swcap_translate`` 加进
   manifest，只跑 ASR 的用户就会**丢掉 ASR 的 autostart**（重启不自动起）
   + 开机不恢复向导。这是一条**静默**的回归 —— 断言比注释可靠。

2. **provider 的单次措辞通道** —— 只试一条措辞、**不重试**、不走负缓存。

3. **翻译服务** —— 开关 / 队列 / 译文帧 / 各条降级 / endpoint 钩子。

4. **文件 / 视频路径的「自己断句」** —— 那条路径**没有** ``endpoint``
   帧（没有句级信号）⇒ 后端按标点自己切句（``_SentenceSplitter``），
   切出的完整句再送翻译；句序号落在**另一个区间**，不与实时路径撞。

**不碰真机**：假 provider（``translate_sentence`` 直接返回或抛异常）、
假广播订阅者。零 RPC、零 NPU 占用。

用法::

    .venv/bin/python scripts/verify_asr_translation.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Optional

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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


# --------------------------------------------------------------------------- #
# 假的翻译管理器
# --------------------------------------------------------------------------- #
class FakeManager:
    """替掉 ``app.core.translate.npu_manager`` 的单例。"""

    def __init__(self, behavior: str = "ok") -> None:
        self.behavior = behavior
        self.calls: list = []

    def translate_sentence(self, text: str, source: str, target: str):
        self.calls.append((text, source, target))
        if self.behavior == "quality":
            from app.core.translate.npu_provider import NpuQualityError
            raise NpuQualityError("这句没翻出来")
        if self.behavior == "unavailable":
            raise RuntimeError("NPU 翻译未启用")
        if self.behavior == "unsupported":
            from app.core.translate.base import UnsupportedLanguageError
            raise UnsupportedLanguageError("认不出源语言")
        return "Hello there", {"decode_ms": 120.0}


def _install_fake_manager(behavior: str = "ok") -> FakeManager:
    """把宿主模块的单例换成假的（翻译服务是**调用时** import 的 ⇒ 生效）。"""
    import app.core.translate.npu_manager as mod
    fake = FakeManager(behavior)
    mod._INSTANCES = fake
    return fake


def _subscribe():
    """**先**订阅再触发动作 —— 否则广播可能比订阅早到（竞态）。"""
    from wdnapps_audio_chat.cards.qwen3_asr.service import get_asr_service
    svc = get_asr_service()
    q = asyncio.Queue()
    svc.register_subscriber(q)
    return svc, q


def _collect(svc, q, timeout: float = 5.0) -> list:
    """收下广播出来的 WS 帧。

    ⚠️ 翻译服务在**工作线程**里广播（``asyncio.Queue.put_nowait`` 不是
    线程安全的，但它只是往 deque 里塞 + 唤醒等待者，没有等待者时是安全的）
    ⇒ 这里直接读队列。
    """
    out = []
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            try:
                out.append(q.get_nowait())
            except asyncio.QueueEmpty:
                if out:
                    break
                time.sleep(0.05)
    finally:
        svc.unregister_subscriber(q)
    return out


def _collect_until(svc, q, pred, timeout: float = 8.0,
                   settle: float = 1.0) -> list:
    """收帧直到 ``pred(frame)`` 命中，**再静置** ``settle`` 秒收尾。

    ⚠️ **不能**用 :func:`_collect`：它「一收到东西就收工」⇒ 翻译帧是在
    **工作线程**里异步推的，很可能比先到的 round 帧晚几十毫秒 ⇒ 会漏。

    ⚠️ 为什么要**静置**：一次识别会切出**好几句**，译文由**两个工作线程**
    并行推 ⇒ 先到的那条不代表最后一条都到齐了。
    """
    out = []
    deadline = time.time() + timeout
    hit_at: Optional[float] = None
    try:
        while time.time() < deadline:
            try:
                out.append(q.get_nowait())
            except asyncio.QueueEmpty:
                pass
            if any(pred(f) for f in out):
                if hit_at is None:
                    hit_at = time.time()
                elif time.time() - hit_at >= settle:
                    break
            time.sleep(0.05)
    finally:
        svc.unregister_subscriber(q)
    return out


# --------------------------------------------------------------------------- #
# [1] manifest 守卫
# --------------------------------------------------------------------------- #
def test_manifest_guard() -> None:
    from scripts.card_dirs import card_dir  # type: ignore

    print("\n[1] manifest 守卫：software_capabilities 仍然只有 swcap_audio_asr")
    import os.path
    manifest_path = os.path.join(card_dir("qwen3-asr"), "manifest.json")
    with open(manifest_path, encoding="utf-8") as fp:
        man = json.load(fp)
    caps = list(man.get("software_capabilities") or [])
    check("声明的能力里**没有** swcap_translate",
          "swcap_translate" not in caps, f"caps={caps}")
    check("仍然声明 swcap_audio_asr", "swcap_audio_asr" in caps, f"caps={caps}")
    check("只有一项（多加一项就会打断 ASR 的 autostart）",
          len(caps) == 1, f"caps={caps}")


# --------------------------------------------------------------------------- #
# [2] provider：快路径（混搭措辞 + 一条兜底措辞）
# --------------------------------------------------------------------------- #
def test_provider_fast_path() -> None:
    print("\n[2] provider：translate_one_fast（混搭措辞 + 1 条兜底）")
    from app.core.translate.npu_provider import (NpuQualityError,
                                                 NpuTranslateProvider)

    class FakeSession:
        def __init__(self, out: str) -> None:
            self.out = out
            self.prompts: list = []
            self.last_perf = None

        async def respond(self, prompt: str, max_new_tokens: int = 512):
            self.prompts.append(prompt)
            return self.out

    prov = NpuTranslateProvider()
    sess = FakeSession("你好，世界")
    out, perf = prov.translate_one_fast("Hello, world", "en", "zh-CN",
                                        session=sess)
    check("翻出来了", out == "你好，世界", f"out={out!r}")
    check("第一条就中 ⇒ **只跑一次推理**", len(sess.prompts) == 1,
          f"prompts={sess.prompts}")
    check("第 1 条是**混搭措辞**（源用码、目标用名）",
          sess.prompts[0].startswith("Translate the following text from en to Chinese:"),
          f"prompt={sess.prompts[0]!r}")
    check("提示词里带了目标语言", "Chinese" in sess.prompts[0],
          f"prompt={sess.prompts[0]!r}")

    # 第 1 条复读 ⇒ 换第 2 条措辞再试（不是同一条重试）
    class EchoThenOk(FakeSession):
        async def respond(self, prompt: str, max_new_tokens: int = 512):
            self.prompts.append(prompt)
            return self.out if len(self.prompts) > 1 else "Hello, world"

    sess_echo = EchoThenOk("你好，世界")
    out_echo, _ = prov.translate_one_fast("Hello, world", "en", "zh-CN",
                                          session=sess_echo)
    check("第 1 条复读 ⇒ 换措辞后翻出来了", out_echo == "你好，世界",
          f"out={out_echo!r}")
    check("一共试了 2 条", len(sess_echo.prompts) == 2,
          f"prompts={sess_echo.prompts}")
    check("第 2 条与第 1 条**写法不同**",
          sess_echo.prompts[0] != sess_echo.prompts[1]
          and "from en to Chinese" not in sess_echo.prompts[1],
          f"prompts={sess_echo.prompts}")

    # 复读原文 ⇒ 两条都失败 ⇒ 质量失败（降级显示原文，不当成功）
    sess2 = FakeSession("Hello, world")   # 复读原文
    try:
        prov.translate_one_fast("Hello, world", "en", "zh-CN", session=sess2)
        check("复读原文应判质量失败", False, "却返回了成功")
    except NpuQualityError:
        check("复读原文判质量失败（降级显示原文）", True)
    check("质量失败**最多试 2 条**（延迟上界 ~1 s）", len(sess2.prompts) == 2,
          f"prompts={sess2.prompts}")

    # 同一条翻两次 ⇒ 两次都真的跑了（**不走负缓存**）
    sess3 = FakeSession("OK")
    prov.translate_one_fast("好的", "zh-CN", "en", session=sess3)
    prov.translate_one_fast("好的", "zh-CN", "en", session=sess3)
    check("同一句翻两次都真的推理了（不走负缓存）",
          len(sess3.prompts) == 2, f"prompts={sess3.prompts}")

    # 源语 == 目标语 ⇒ 直接返回，不推理
    sess4 = FakeSession("x")
    out4, _ = prov.translate_one_fast("hello", "en", "en", session=sess4)
    check("源语 == 目标语 ⇒ 不推理", len(sess4.prompts) == 0 and out4 == "hello",
          f"prompts={sess4.prompts} out={out4!r}")


def test_language_normalize() -> None:
    print("\n[3] 语种归一：ASR 报的是**语言名**")
    from app.core.translate.npu_provider import normalize_language
    cases = {
        "English": "en",
        "Chinese": "zh-CN",
        "Japanese": "ja",
        "Mandarin": "zh-CN",
        "Traditional Chinese": "zh-TW",
        "zh": "zh-CN",
        "zh-CN": "zh-CN",
        "ENGLISH": "en",
    }
    for raw, want in cases.items():
        got = normalize_language(raw)
        check(f"{raw!r} → {want}", got == want, f"got={got!r}")
    check("认不出来 ⇒ 空串（不瞎猜）", normalize_language("Klingon") == "")
    check("空串 ⇒ 空串", normalize_language("") == "")

    # 管理器对外的契约：**归一在宿主这一侧**做
    from app.core.translate.npu_manager import NpuTranslateInstances
    from app.core.translate.base import UnsupportedLanguageError
    mgr = NpuTranslateInstances()
    src, tgt = mgr.resolve_languages("Chinese", "en")
    check("管理器把 ASR 的语言名归一成 provider 的 key",
          (src, tgt) == ("zh-CN", "en"), f"got={(src, tgt)}")
    try:
        mgr.resolve_languages("Klingon", "en")
        check("认不出源语 ⇒ 抛 UnsupportedLanguageError", False, "却没抛")
    except UnsupportedLanguageError:
        check("认不出源语 ⇒ 抛 UnsupportedLanguageError", True)


# --------------------------------------------------------------------------- #
# [4] 翻译服务
# --------------------------------------------------------------------------- #
def test_translation_service() -> None:
    print("\n[4] 翻译服务：开关 / 帧 / 降级 / 队列")
    from wdnapps_audio_chat.cards.qwen3_asr.translation import (
        QUEUE_MAX,
        TranslationService,
    )

    # --- 关着时：不入队（零开销） ---
    fake = _install_fake_manager("ok")
    svc = TranslationService()
    svc.submit(index=1, text="你好", lang="Chinese", stream_id="live-x",
               loop=None)
    check("开关关着 ⇒ 不入队（零开销）", svc.status()["submitted"] == 0,
          f"submitted={svc.status()['submitted']}")
    check("开关关着 ⇒ 一次推理都没有", not fake.calls, f"calls={fake.calls}")

    # --- 开着时：一句 ⇒ 一个 translation 帧 ---
    svc.configure(enabled=True, target_lang="en")
    sub_svc, sub_q = _subscribe()
    svc.submit(index=1, text="你好，世界", lang="Chinese",
               stream_id="live-x", loop=None)
    frames = _collect(sub_svc, sub_q)
    tr = [f for f in frames if f.get("type") == "translation"]
    check("收到一个 translation 帧", len(tr) == 1, f"frames={frames}")
    if tr:
        f = tr[0]
        check("帧带句序号", f.get("index") == 1, f"index={f.get('index')}")
        check("帧带原文", f.get("source") == "你好，世界",
              f"source={f.get('source')!r}")
        check("帧带译文", f.get("text") == "Hello there",
              f"text={f.get('text')!r}")
        check("状态是 done", f.get("status") == "done",
              f"status={f.get('status')}")
        check("带 stream_id", f.get("stream_id") == "live-x",
              f"stream_id={f.get('stream_id')}")
        check("带延迟", isinstance(f.get("latency_ms"), int),
              f"latency_ms={f.get('latency_ms')}")
    # ⚠️ 卡片把「ASR 报的语言名」**原样**交给管理器（归一在管理器内部做
    # —— 调用方不必知道 provider 的语言表长什么样）
    check("翻译调用带上了源语（ASR 的语言名）与目标语（provider 的 key）",
          fake.calls and fake.calls[0][1] == "Chinese" and fake.calls[0][2] == "en",
          f"calls={fake.calls}")

    # --- 降级：翻不出来 ---
    _install_fake_manager("quality")
    svc2 = TranslationService()
    svc2.configure(enabled=True, target_lang="en")
    sub_svc, sub_q = _subscribe()
    svc2.submit(index=1, text="你好", lang="Chinese", stream_id="live-y",
                loop=None)
    frames = _collect(sub_svc, sub_q)
    tr = [f for f in frames if f.get("type") == "translation"]
    check("翻不出来 ⇒ 仍然推一帧（前端要显示原文 + 未翻译）",
          len(tr) == 1, f"frames={frames}")
    if tr:
        check("失败时状态是 failed", tr[0].get("status") == "failed",
              f"status={tr[0].get('status')}")
        check("失败时 text 是**原文**", tr[0].get("text") == "你好",
              f"text={tr[0].get('text')!r}")
        check("失败时带原因", bool(tr[0].get("reason")),
              f"reason={tr[0].get('reason')!r}")

    # --- 降级：模型没启用 ---
    _install_fake_manager("unavailable")
    svc3 = TranslationService()
    svc3.configure(enabled=True, target_lang="en")
    sub_svc, sub_q = _subscribe()
    svc3.submit(index=1, text="你好", lang="Chinese", stream_id="live-z",
                loop=None)
    frames = _collect(sub_svc, sub_q)
    tr = [f for f in frames if f.get("type") == "translation"]
    check("模型没启用 ⇒ 状态是 unavailable",
          tr and tr[0].get("status") == "unavailable",
          f"frames={frames}")

    # --- 源语 == 目标语 ⇒ 透传，不推理 ---
    fake4 = _install_fake_manager("ok")
    svc4 = TranslationService()
    svc4.configure(enabled=True, target_lang="Chinese")
    sub_svc, sub_q = _subscribe()
    svc4.submit(index=1, text="你好", lang="Chinese", stream_id="live-w",
                loop=None)
    frames = _collect(sub_svc, sub_q)
    tr = [f for f in frames if f.get("type") == "translation"]
    check("源语 == 目标语 ⇒ 状态是 skipped",
          tr and tr[0].get("status") == "skipped", f"frames={frames}")
    check("源语 == 目标语 ⇒ 一次推理都没跑", not fake4.calls,
          f"calls={fake4.calls}")

    # --- 队列满 ⇒ 丢最旧 ---
    _install_fake_manager("ok")
    svc5 = TranslationService()
    svc5.configure(enabled=True, target_lang="en")
    # ⚠️ **按模块里的 ``QUEUE_MAX`` 填**（2026-11-20 从 32 放大到 256）——
    # 写死数字的话，改了上限这条断言就测不到「队列有界」了。
    for i in range(1, QUEUE_MAX + 20):
        svc5.submit(index=i, text=f"句子{i}", lang="Chinese",
                    stream_id="live-q", loop=None)
    st = svc5.status()
    check("队列有界（满了丢最旧，不无限涨）",
          st["queue_depth"] <= QUEUE_MAX, f"queue_depth={st['queue_depth']}")
    check("丢弃被计数", st["dropped"] > 0, f"dropped={st['dropped']}")

    # --- 关掉开关 ⇒ 队列清空 ---
    svc5.configure(enabled=False)
    check("关掉开关 ⇒ 排队中的句子被丢掉",
          svc5.status()["queue_depth"] == 0,
          f"queue_depth={svc5.status()['queue_depth']}")
    # ⚠️ **把 svc5 的后台线程彻底停掉** —— 假翻译器是**调用时**取单例的
    # （``get_npu_translate_instances()``）⇒ svc5 的 worker 要是还活着，就会
    # 在**下一个**测试里继续翻那些「句子N」，而那时单例已经换成新的假件了
    # ⇒ 那些调用被算到下一个测试头上 ⇒ 假失败（实测：端点钩子那条看到
    # 「句子102」）。⇒ close + 等在飞的那一句翻完。
    svc5.close()
    time.sleep(0.8)

    # --- clear() 清统计 ---
    svc5.clear()
    st = svc5.status()
    # ⚠️ **不能**断言 dropped == 0 —— clear() 会把代数 +1，正在推理的那
    # 一句回来时算「作废」⇒ dropped 会 +1（这是**故意**的：清空后不该再
    # 有译文冒出来）。
    check("clear() 清统计", st["submitted"] == 0 and st["translated"] == 0,
          f"status={st}")


# --------------------------------------------------------------------------- #
# [5] endpoint 钩子
# --------------------------------------------------------------------------- #
def test_endpoint_hook() -> None:
    print("\n[5] endpoint 钩子：一句话结束 ⇒ 送去翻译")
    from wdnapps_audio_chat.cards.qwen3_asr import service as asr_service
    from wdnapps_audio_chat.cards.qwen3_asr.translation import (
        TranslationService,
    )

    fake = _install_fake_manager("ok")
    tr_svc = TranslationService()
    tr_svc.configure(enabled=True, target_lang="en")

    # 用一个**最小替身**跑 ``_finalize(reason="endpoint")`` ——
    # 真跑一遍端点分支 ⇒ 断言它确实把句子送去了翻译。
    class Stub:
        def __init__(self):
            self._finished = False
            self._feed = type("F", (), {"flush": lambda self: b""})()
            self._final_text = "今天我们讨论预算。"
            self._language = "Chinese"
            self._endpoint_index = 3
            self._endpoint_hit = False
            self._session_had_speech = True
            self._segments = []
            self._timeline_sec = 12.5
            self._closed = False
            self.stream_id = "live-stub"
            self._loop = None
            self.published = []
            self.rotated = 0

        async def _submit(self, tail, final=False):
            return ""

        def _publish(self, payload):
            self.published.append(payload)

        def _rotate(self):
            self.rotated += 1

        # 真方法（走真钩子 ⇒ 断言的是**真实接线**）
        def _submit_translation(self):
            asr_service._LiveAsrSession._submit_translation(self)

    stub = Stub()
    # 让翻译服务单例 = 上面那个（endpoint 钩子拿的是单例）
    import wdnapps_audio_chat.cards.qwen3_asr.translation as tr_mod
    tr_mod._SERVICE = tr_svc

    asyncio.run(asr_service._LiveAsrSession._finalize(stub, reason="endpoint"))
    check("endpoint 帧照常广播", any(p.get("type") == "endpoint"
                                    for p in stub.published),
          f"published={stub.published}")
    check("端点后开了新会话", stub.rotated == 1, f"rotated={stub.rotated}")
    check("这一句被送去翻译了", len(fake.calls) == 1, f"calls={fake.calls}")
    if fake.calls:
        check("送去的是这一句的原文", fake.calls[0][0] == "今天我们讨论预算。",
              f"calls={fake.calls}")
    tr_mod._SERVICE = None


# --------------------------------------------------------------------------- #
# [6] 文件 / 视频路径：自己断句
# --------------------------------------------------------------------------- #
def test_clear_drops_inflight() -> None:
    """「清空」⇒ **正在推理**的那一句不再推给页面。"""
    print("\n[6] 「清空」：正在推理的译文不再推回来")
    from wdnapps_audio_chat.cards.qwen3_asr.translation import (
        TranslationService,
    )
    import app.core.translate.npu_manager as npu_mod

    class SlowManager:
        """慢吞吞的假管理器（好让「推理中」这个窗口稳定复现）。"""

        def __init__(self) -> None:
            self.calls: list = []

        def translate_sentence(self, text: str, source: str, target: str):
            self.calls.append((text, source, target))
            time.sleep(1.5)
            return "你好", {"decode_ms": 10.0}

    fake = SlowManager()
    npu_mod._INSTANCES = fake

    svc = TranslationService()
    # ⚠️ 目标语言**不能**是 zh-CN —— 源语 Chinese 归一后也是 zh-CN ⇒
    # 源语 == 目标语 ⇒ 走「透传、不推理」分支 ⇒ 根本没有推理窗口。
    svc.configure(enabled=True, target_lang="en")
    sub_svc, sub_q = _subscribe()
    svc.submit(index=1, text="你好，世界", lang="Chinese",
               stream_id="live-gen", loop=None)
    time.sleep(0.4)          # 让工作线程**开始**推理（进入 sleep）
    check("推理已经开始", bool(fake.calls), f"calls={fake.calls}")
    svc.clear()              # ← 用户点了「清空」
    frames = _collect(sub_svc, sub_q, timeout=3.0)
    tr = [f for f in frames if f.get("type") == "translation"]
    check("「清空」⇒ 已经在跑的那句**不再推**（页面不会又冒一行）",
          len(tr) == 0, f"frames={frames}")


def test_file_path_splitter() -> None:
    print("\n[7] 文件 / 视频路径：自己断句（_SentenceSplitter）")
    from wdnapps_audio_chat.cards.qwen3_asr.service import _SentenceSplitter

    def run(steps, flush: bool = True) -> list:
        sp = _SentenceSplitter()
        out: list = []
        # ⚠️ emit 的签名是 **4 参**（text, index, c0, c1）—— c0/c1 是这句在
        # 当前 epoch 文本里的**字符区间**（半开）⇒ 前端据此换算音频时间。
        emit = lambda t, i, c0, c1: out.append((i, t, c0, c1))   # noqa: E731
        for c in steps:
            sp.feed(c, emit)
        if flush:
            sp.flush(emit)
        return out

    # --- 中文句末标点 ---
    out = run(["第一句。第二句。第三句。"])
    check("中文句末标点 ⇒ 一句一条", len(out) == 3, f"out={out}")
    check("切出来的内容对",
          [t for _, t, _, _ in out] == ["第一句。", "第二句。", "第三句。"],
          f"out={out}")
    check("句序号从 1 起、连续", [i for i, _, _, _ in out] == [1, 2, 3], f"out={out}")
    check("字符区间连续且首尾相接（前端靠它换算音频时间）",
          all(out[k][2] < out[k][3] for k in range(len(out)))
          and [out[k][3] for k in range(len(out) - 1)]
              == [out[k + 1][2] for k in range(len(out) - 1)],
          f"out={out}")
    check("字符区间落在原文里（切出来的就是那一段）",
          all(src[o[2]:o[3]].strip() == o[1]
              for o in out
              for src in ["第一句。第二句。第三句。"]),
          f"out={out}")

    # --- 英文句点（带护栏）---
    out = run(["Hello there. This is a test."])
    check("英文句点 ⇒ 切句", len(out) == 2, f"out={out}")

    # --- 小数不切 ---
    out = run(["The value is 3.14 exactly."])
    check("小数里的点**不**当句末", len(out) == 1, f"out={out}")

    # --- 缩写不切 ---
    out = run(["Mr. Smith and Dr. Jones went home."])
    check("缩写后的点**不**当句末", len(out) == 1, f"out={out}")

    # --- 长度兜底（没标点的长音频也要边播边出译文）---
    out = run(["x" * 130])
    check("没有标点 ⇒ 超长强制切", len(out) >= 2, f"out={len(out)}")

    # --- 收尾 flush（最后一句通常没标点）---
    out = run(["Hello there. And this tail has no punctuation"])
    check("收尾时把没标点的尾巴也提交了",
          bool(out) and out[-1][1] == "And this tail has no punctuation",
          f"out={out}")

    # --- epoch 切换：显式 flush + 句序号继续 ---
    sp = _SentenceSplitter()
    out = []
    emit = lambda t, i, c0, c1: out.append((i, t, c0, c1))       # noqa: E731
    sp.feed("first epoch text without punctuation", emit)
    sp.flush(emit)          # ← _rotate_epoch 里的显式 flush
    sp.reset_epoch()
    sp.feed("second epoch text without punctuation", emit)
    sp.flush(emit)
    check("epoch 切换 ⇒ 两段各出一条", len(out) == 2, f"out={out}")
    check("epoch 切换后句序号继续（不归零）",
          [i for i, _, _, _ in out] == [1, 2], f"out={out}")


def test_splitter_no_mid_word_cut() -> None:
    """兜底切句**不能把一个词劈成两半**（用户报「单词截断」）。

    背景
    ----
    旧的兜底是 ``cut = _TR_MAX_SENTENCE_CHARS``（**纯按字符数切**）⇒
    切点常常落在词中间。实测（英文样本 ``asr_en.wav``）：

    .. code-block:: text

        He wasn't even that big when I started listening to him, bu
        t in his solo music, didn't do overly well, but he did very
        well when he started writing for other people.

    ⇒ ① 界面上看到的是**半个词**；② 送进翻译模型的是**半个词** ⇒
    译文也跟着错位（「well」被当成独立的一句 ⇒ 翻成「嗯，」）。
    """
    print("\n[7b] 兜底切句：不劈开词（单词截断）")
    from wdnapps_audio_chat.cards.qwen3_asr.service import _SentenceSplitter

    def run(steps, flush: bool = True) -> list:
        sp = _SentenceSplitter()
        out: list = []
        emit = lambda t, i, c0, c1: out.append((i, t, c0, c1))   # noqa: E731
        for c in steps:
            sp.feed(c, emit)
        if flush:
            sp.flush(emit)
        return out

    # --- 用户报的那段英文（asr_en.wav 的真实识别结果）---
    full = ("Uh huh. Oh yeah, yeah! He wasn't even that big when I started "
            "listening to him, but in his solo music, didn't do overly well, "
            "but he did very well when he started writing for other people.")

    def cuts_are_word_safe(src: str, out: list) -> bool:
        """每一刀都**没有落在词中间**吗？

        判据：切点**前一个**字符和切点**后一个**字符不能**同时**是
        字母 / 数字（同时是 ⇒ 把一个词劈成了两半）。
        """
        for k in range(len(out) - 1):
            c1 = out[k][3]
            if c1 <= 0 or c1 >= len(src):
                continue
            if src[c1 - 1].isalnum() and src[c1].isalnum():
                return False
        return True

    # ① **逐轮喂**（真实场景：committed 是**逐词**长出来的）
    # ⚠️ 必须逐轮喂：一次喂全文 ⇒ 走的是「末尾就是句末标点 ⇒ 整段等收尾」
    #    那条路，看不出兜底切句的行为。
    seq = [
        "Uh huh. Oh yeah, yeah! He wasn't even that big when",
        "Uh huh. Oh yeah, yeah! He wasn't even that big when I started",
        "Uh huh. Oh yeah, yeah! He wasn't even that big when I started listening to him,",
        "Uh huh. Oh yeah, yeah! He wasn't even that big when I started listening to him, but in",
        "Uh huh. Oh yeah, yeah! He wasn't even that big when I started listening to him, but in his solo music,",
        "Uh huh. Oh yeah, yeah! He wasn't even that big when I started listening to him, but in his solo music, didn't",
        "Uh huh. Oh yeah, yeah! He wasn't even that big when I started listening to him, but in his solo music, didn't do overly well,",
        "Uh huh. Oh yeah, yeah! He wasn't even that big when I started listening to him, but in his solo music, didn't do overly well, but he did very well when he started writing for other people.",
    ]
    out = run(seq)
    texts = [t for _, t, _, _ in out]
    check("逐轮喂 ⇒ 切成了多句（不是一整段）", len(out) >= 3, f"texts={texts}")
    check("逐轮喂 ⇒ **没有一刀落在词中间**（单词截断没了）",
          cuts_are_word_safe(full, out), f"texts={texts}")
    check("逐轮喂 ⇒ 一个字符都不丢",
          "".join(texts).replace(" ", "") == full.replace(" ", ""),
          f"texts={texts}")
    check("逐轮喂 ⇒ 字符区间连续且首尾相接",
          all(out[k][3] == out[k + 1][2] for k in range(len(out) - 1)),
          f"out={out}")
    check("逐轮喂 ⇒ 最后一句带着句号（句末标点没被落在下一段）",
          texts and texts[-1].endswith("."), f"texts={texts}")
    check("逐轮喂 ⇒ 没有「只剩一个句号」的碎片句",
          not any(t.strip() in (".", "!", "?", "。", "！", "？") for t in texts),
          f"texts={texts}")
    check("逐轮喂 ⇒ 没有「半个词」这种碎片（bu / t / peopl 之类）",
          not any(t.strip() in ("bu", "t", "e", "peopl", "le") for t in texts),
          f"texts={texts}")
    # 兜底切句优先切在**子句标点**上（语义完整的小句）
    check("兜底切句优先切在**子句标点**上（语义完整的小句）",
          any(t.endswith(",") for t in texts), f"texts={texts}")

    # ② **一次喂全文**（末尾就是句末标点 ⇒ 整段等收尾）
    out = run([full])
    texts = [t for _, t, _, _ in out]
    check("一次喂全文 ⇒ 也没有一刀落在词中间",
          cuts_are_word_safe(full, out), f"texts={texts}")
    check("一次喂全文 ⇒ 一个字符都不丢",
          "".join(texts).replace(" ", "") == full.replace(" ", ""),
          f"texts={texts}")
    check("一次喂全文 ⇒ 末尾那句完整保留（没被劈开）",
          texts and texts[-1].endswith("other people."),
          f"texts={texts}")

    # --- 句末标点**在文本末尾**时 ⇒ 不硬切（等下一轮 / 收尾 flush）---
    # 「3.」后面可能是「14」⇒ 硬切会把小数劈开。
    out = run(["The reading was exactly 3."])
    check("末尾的句点不硬切（等下一轮）",
          len(out) == 1 and out[0][1] == "The reading was exactly 3.",
          f"out={out}")
    out = run(["The reading was exactly 3.", "The reading was exactly 3.14 now"])
    check("末尾的句点等到了下一轮 ⇒ 小数没被劈开",
          any("3.14" in x[1] for x in out), f"out={out}")

    # --- 纯中文：子句标点（逗号）也要认 ---
    cn = "这是一段很长很长的没有任何标点符号的中文句子用来测试强制切句的行为看看会切成什么样"
    out = run([cn])
    check("纯中文 ⇒ 仍能切出句子（兜底没坏）", len(out) >= 1, f"out={out}")
    check("纯中文 ⇒ 一个字符都不丢",
          "".join(t for _, t, _, _ in out) == cn, f"out={out}")

    # --- 没有边界的超长词 ⇒ 按上限硬切（不能死循环 / 不能不切）---
    out = run(["x" * 130])
    check("没有边界的超长串 ⇒ 仍能切（不会卡住）", len(out) >= 2, f"out={out}")


# --------------------------------------------------------------------------- #
# [8] 文件 / 视频路径：round 帧 ⇒ 自己断句 ⇒ 译文
# --------------------------------------------------------------------------- #
def test_file_path_hook() -> None:
    print("\n[8] 文件 / 视频路径：round 帧 ⇒ 切句 ⇒ 译文帧")
    from wdnapps_audio_chat.cards.qwen3_asr import service as asr_service
    from wdnapps_audio_chat.cards.qwen3_asr.translation import (
        TranslationService,
    )
    import wdnapps_audio_chat.cards.qwen3_asr.translation as tr_mod

    fake = _install_fake_manager("ok")
    tr_svc = TranslationService()
    # ⚠️ 目标语言**不能**选 en —— 源语是 English（归一后也是 en）⇒
    # 源语 == 目标语 ⇒ 走「透传、不推理」分支 ⇒ 根本不会调翻译模型。
    tr_svc.configure(enabled=True, target_lang="zh-CN")
    tr_mod._SERVICE = tr_svc

    svc = asr_service.get_asr_service()
    base = 7 * asr_service._TR_FILE_INDEX_BASE
    svc._tr_ctx = {
        "rid": 7,
        "stream_id": "file-7",
        "base": base,
        "loop": None,
    }
    svc._language = "English"

    # ⚠️ **只订阅一次** —— 两次订阅会把第一批的译文帧吃掉（第一批的帧
    # 在第一次收帧时就到了），后面就只剩第二批 ⇒ 序号对不上。
    sub_svc, sub_q = _subscribe()
    svc._on_delta(json.dumps({
        "type": "asr_round", "round": 1,
        "committed": "Hello there. This is fine.",
        "unfixed": "", "language": "English",
    }))
    # --- 收尾 flush：最后一句没标点也要译 ---
    svc._on_delta(json.dumps({
        "type": "asr_round", "round": 2,
        "committed": "Hello there. This is fine. And a tail",
        "unfixed": "", "language": "English",
    }))
    # ⚠️ 方法名是 ``_sent_flush``（不是 ``_tr_flush``）—— **切句与翻译解耦**
    # 了：收尾把尾巴切成一句是「切句」的事，跟翻译开没开无关 ⇒ 切句器
    # 不再看翻译上下文 ⇒ 方法名从「_tr_」改成了「_sent_」。
    svc._sent_flush()
    frames = _collect_until(sub_svc, sub_q,
                             lambda f: f.get("type") == "translation")
    tr = sorted((f for f in frames if f.get("type") == "translation"),
                key=lambda f: f.get("index") or 0)
    check("round 帧 ⇒ 自己切句 ⇒ 译文帧", len(tr) >= 1, f"frames={frames}")
    if tr:
        f = tr[0]
        check("译文帧带文件路径的 stream_id", f.get("stream_id") == "file-7",
              f"stream_id={f.get('stream_id')}")
        check("句序号落在文件路径的区间（不与实时路径的小整数撞）",
              int(f.get("index") or 0) >= asr_service._TR_FILE_INDEX_BASE,
              f"index={f.get('index')}")
        check("送去翻译的是切出来的那一句",
              bool(fake.calls) and fake.calls[0][0] == "Hello there.",
              f"calls={fake.calls}")
        check("尾巴那一句送去了翻译",
              any(c[0] == "And a tail" for c in fake.calls), f"calls={fake.calls}")
        idxs = [int(f.get("index") or 0) for f in tr]
        check("句序号连续（base+1、base+2…）",
              idxs == list(range(base + 1, base + 1 + len(idxs))), f"idxs={idxs}")
        srcs = [f.get("source") for f in tr]
        check("三句都切出来了",
              srcs == ["Hello there.", "This is fine.", "And a tail"],
              f"srcs={srcs}")

    svc._tr_ctx = None
    tr_mod._SERVICE = None


# --------------------------------------------------------------------------- #
# [9] API 路由
# --------------------------------------------------------------------------- #
def test_api_routes() -> None:
    print("\n[9] API 路由齐了")
    from wdnapps_audio_chat.cards.qwen3_asr import api
    paths = {}
    for r in api.router.routes:
        p = getattr(r, "path", "")
        for m in (getattr(r, "methods", None) or set()):
            paths.setdefault(p, set()).add(m)
    for path, method in (("/translate/status", "GET"),
                          ("/translate/languages", "GET"),
                          ("/translate/settings", "POST"),
                          ("/translate/enable", "POST"),
                          ("/translate/disable", "POST")):
        check(f"{method} {path}", method in paths.get(path, set()),
              f"paths={sorted(paths)}")


def main() -> int:
    print("=" * 72)
    print("ASR 卡：实时翻译（实时路径 + 文件 / 视频路径）")
    print("=" * 72)

    test_manifest_guard()
    test_provider_fast_path()
    test_language_normalize()
    test_translation_service()
    test_endpoint_hook()
    test_clear_drops_inflight()
    test_file_path_splitter()
    test_splitter_no_mid_word_cut()
    test_file_path_hook()
    test_api_routes()

    print("\n" + "=" * 72)
    print(f"通过 {_PASS} / 失败 {_FAIL}")
    print("=" * 72)
    if _FAIL:
        print("❌ 有断言没过")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
