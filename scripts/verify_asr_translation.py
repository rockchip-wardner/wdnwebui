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
    for i in range(1, 60):
        svc5.submit(index=i, text=f"句子{i}", lang="Chinese",
                    stream_id="live-q", loop=None)
    st = svc5.status()
    check("队列有界（满了丢最旧，不无限涨）",
          st["queue_depth"] <= 32, f"queue_depth={st['queue_depth']}")
    check("丢弃被计数", st["dropped"] > 0, f"dropped={st['dropped']}")

    # --- 关掉开关 ⇒ 队列清空 ---
    svc5.configure(enabled=False)
    check("关掉开关 ⇒ 排队中的句子被丢掉",
          svc5.status()["queue_depth"] == 0,
          f"queue_depth={svc5.status()['queue_depth']}")

    # --- clear() 清统计 ---
    svc5.clear()
    st = svc5.status()
    check("clear() 清统计", st["submitted"] == 0 and st["dropped"] == 0,
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
# [6] API 路由
# --------------------------------------------------------------------------- #
def test_api_routes() -> None:
    print("\n[6] API 路由齐了")
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
    print("ASR 卡：实时翻译（流式同传，第一期）")
    print("=" * 72)

    test_manifest_guard()
    test_provider_fast_path()
    test_language_normalize()
    test_translation_service()
    test_endpoint_hook()
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
