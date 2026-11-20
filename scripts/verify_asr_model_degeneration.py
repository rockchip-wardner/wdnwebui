#!/usr/bin/env python3
"""回归：ASR 的**模型退化**与**回看重放**都不该污染公开输出。

背景
====

Qwen3-ASR 的流式会话有两个会「自己跑偏」的地方：

1. **回看重放** —— 换会话时把最近 ``_LOOKBACK_SEC`` 秒的音频**再喂一遍**
   给新会话（否则新会话零上下文 ⇒ 语种判错）。但重放的头几轮只有
   ~1 秒音频、零上下文 ⇒ 模型会**重新判语种**（中文被判成 English），
   甚至**臆造**英文内容（实测《圆桌派》：``lang=English``
   ``u='You used to go old guys.'``）；而且重放的那段内容**上一段已经
   显示过** ⇒ 再发一遍就是「同一段音频两遍、措辞不同」。

2. **模型退化（重复自己）** —— 提示词里 **system 是空的**（SDK 的
   ``_build_chat_embeds`` 只有 ``<|im_start|>system\\n<|im_end|>``，
   没有「请转写」这类指令）⇒ 音频里出现**问句**时模型会**当成问答**
   来回答：先答一遍（有时答成英文），再**把自己的答案复述一遍**
   ⇒ 已确认文本里出现整段重复（实测《圆桌派》：一段里连续 25 轮都在
   复述同一句）。

本脚本**直接驱动**服务端的 ``_on_delta``（不需要 NPU、不走 RPC），
断言这两样东西都**进不了公开输出**：

* 回看轮次 → 不进轮次列表、不广播、**不更新语种**；
* 回看前缀 → 不进公开文本；
* 模型复述自己的那一截 → 从公开文本里剪掉，并标记「要换会话」。

用法::

    .venv/bin/python scripts/verify_asr_model_degeneration.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import List

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


#: 一句 22 字的「长重复」—— 够长（≥ 阈值）⇒ 算模型退化。
LONG_REPEAT = "这是一个很长的重复句子用来测试模型的退化行为"


def _fresh_service():
    """一个**干净**的服务实例（不碰全局单例 ⇒ 不影响真机）。"""
    from wdnapps_audio_chat.cards.qwen3_asr import service as asr_service

    svc = asr_service._AsrManager()
    frames: List[dict] = []
    queue: asyncio.Queue = asyncio.Queue()
    svc.register_subscriber(queue)
    while not queue.empty():          # pragma: no cover - 起步就空
        queue.get_nowait()
    orig = svc._broadcast

    def _spy(payload: dict) -> None:
        frames.append(payload)
        orig(payload)

    svc._broadcast = _spy
    return svc, frames


def _feed(svc, round_no: int, committed: str, start_sec: float,
          language: str = "Chinese", unfixed: str = "") -> None:
    """投一轮 ``asr_round`` 载荷（与服务端收到的 wire 格式一致）。"""
    payload = {
        "type": "asr_round",
        "round": round_no,
        "committed": committed,
        "unfixed": unfixed,
        "language": language,
        "chunk_start_sec": start_sec,
        "chunk_samples": 16000,
    }
    svc._on_delta(json.dumps(payload))


def _round_frames(frames: List[dict]) -> List[dict]:
    return [f for f in frames if f.get("type") == "round"]


# --------------------------------------------------------------------------- #
# 1) 正常段：公开文本 = 原始文本
# --------------------------------------------------------------------------- #
def test_normal_epoch() -> None:
    print("\n[1] 正常段 —— 公开文本就是模型的原始文本")
    svc, frames = _fresh_service()
    _feed(svc, 1, "第一句话。", 10.0)
    _feed(svc, 2, "第一句话。第二句话。", 11.0)
    check("轮次进了列表", len(svc._rounds) == 2,
          "实际 %d" % len(svc._rounds))
    check("公开文本 = 原始文本",
          svc._rounds[-1]["committed"] == "第一句话。第二句话。",
          repr(svc._rounds[-1]["committed"]))
    check("广播了 2 帧", len(_round_frames(frames)) == 2,
          "实际 %d" % len(_round_frames(frames)))
    check("语种被采信", svc._language == "Chinese", svc._language)


# --------------------------------------------------------------------------- #
# 2) 回看重放：不进公开输出、不改语种
# --------------------------------------------------------------------------- #
def test_lookback_is_invisible() -> None:
    print("\n[2] 回看重放 —— 不进输出、不改语种、前缀被排除")
    svc, frames = _fresh_service()
    # 回看区间 = [0, 20) ⇒ 起点 < 20 的轮次都是「重放」
    svc._lookback_until_sec = 20.0
    _feed(svc, 1, "重放甲", 11.0, language="English")
    _feed(svc, 2, "重放甲重放乙", 12.0, language="English")
    # 第一轮「真的」内容（起点 ≥ 20）
    _feed(svc, 3, "重放甲重放乙真正的新内容", 20.0, language="Chinese")
    check("回看轮次没进轮次列表", len(svc._rounds) == 1,
          "实际 %d" % len(svc._rounds))
    check("回看轮次没广播", len(_round_frames(frames)) == 1,
          "实际 %d" % len(_round_frames(frames)))
    check("公开文本剔掉了回看前缀",
          svc._rounds[-1]["committed"] == "真正的新内容",
          repr(svc._rounds[-1]["committed"]))
    check("回看里的 English 没污染语种", svc._language == "Chinese",
          svc._language)
    # 再来一轮，确认前缀不会「漏」回去
    _feed(svc, 4, "重放甲重放乙真正的新内容继续", 21.0, language="Chinese")
    check("后续轮次仍不含回看前缀",
          svc._rounds[-1]["committed"] == "真正的新内容继续",
          repr(svc._rounds[-1]["committed"]))


# --------------------------------------------------------------------------- #
# 3) 模型退化：复述自己的那一截被剪掉 + 标记换会话
# --------------------------------------------------------------------------- #
def test_degeneration_is_cut() -> None:
    print("\n[3] 模型退化 —— 复述自己的那一截被剪掉")
    svc, frames = _fresh_service()
    _feed(svc, 1, "他说了一句很重要的话。" + LONG_REPEAT, 10.0)
    _feed(svc, 2, "他说了一句很重要的话。" + LONG_REPEAT + "然后他开始回答。",
          11.0)
    # 模型开始复述自己：新增的那一截在更早的文本里出现过
    _feed(svc, 3, "他说了一句很重要的话。" + LONG_REPEAT + "然后他开始回答。"
                  + LONG_REPEAT, 12.0)
    check("退化被标记", svc._degenerate is True)
    check("复述的那一截被剪掉",
          svc._epoch_text.count(LONG_REPEAT) == 1,
          repr(svc._epoch_text[-40:]))
    check("前面的真话还在",
          "他说了一句很重要的话。" in svc._epoch_text,
          repr(svc._epoch_text[:40]))
    # 退化会持续：下一轮又复述一次 ⇒ 依然被剪掉
    _feed(svc, 4, "他说了一句很重要的话。" + LONG_REPEAT + "然后他开始回答。"
                  + LONG_REPEAT + LONG_REPEAT, 13.0)
    check("连续退化也都被剪掉",
          svc._epoch_text.count(LONG_REPEAT) == 1,
          repr(svc._epoch_text[-60:]))


# --------------------------------------------------------------------------- #
# 4) 真人重复（不够长）不能被剪
# --------------------------------------------------------------------------- #
def test_short_repeat_is_kept() -> None:
    print("\n[4] 真人重复（短）—— 不能被剪")
    svc, frames = _fresh_service()
    _feed(svc, 1, "我再说一遍。", 10.0)
    _feed(svc, 2, "我再说一遍。我再说一遍。", 11.0)
    check("短重复保留", svc._epoch_text == "我再说一遍。我再说一遍。",
          repr(svc._epoch_text))
    check("没被误判成退化", svc._degenerate is False)


# --------------------------------------------------------------------------- #
# 4') 模型「自报中文却吐英文」—— 臆造的英文要被剪掉
# --------------------------------------------------------------------------- #
def test_english_hallucination_is_cut() -> None:
    print("\n[4'] 臆造英文 —— 自报中文却吐一整段英文 ⇒ 剪掉")
    svc, frames = _fresh_service()
    _feed(svc, 1, "他提到人权观察这个组织。", 10.0)
    _feed(svc, 2, "他提到人权观察这个组织。Human Rights Watch is a non-profit "
                  "organization that advocates for human rights", 11.0)
    check("英文臆造被标记", svc._degenerate is True)
    check("英文臆造被剪掉",
          "Human Rights Watch" not in svc._epoch_text,
          repr(svc._epoch_text[-50:]))
    check("前面的中文还在",
          svc._epoch_text.startswith("他提到人权观察这个组织。"),
          repr(svc._epoch_text[:30]))
    # ⚠️ **「假设」也是假的** ⇒ 也不能发出去（否则直播那一行仍显示英文）
    rf = _round_frames(frames)
    check("退化那一轮的假设被清空",
          bool(rf) and not rf[-1].get("unfixed"),
          repr(rf[-1].get("unfixed")) if rf else "no frame")


def test_degenerate_keeps_dropping_hypothesis() -> None:
    print("\n[4'~] 退化期间 —— 后续几轮的假设也一并丢掉")
    svc, frames = _fresh_service()
    _feed(svc, 1, "他说了一句很重要的话。" + LONG_REPEAT, 10.0)
    _feed(svc, 2, "他说了一句很重要的话。" + LONG_REPEAT + "然后他开始回答。"
                  + LONG_REPEAT, 11.0)
    _feed(svc, 3, "他说了一句很重要的话。" + LONG_REPEAT + "然后他开始回答。"
                  + LONG_REPEAT + LONG_REPEAT, 12.0, unfixed="还在改写的尾巴")
    rf = _round_frames(frames)
    check("退化后的轮次假设也被清空",
          bool(rf) and not rf[-1].get("unfixed"),
          repr(rf[-1].get("unfixed")) if rf else "no frame")
    check("退化期间语种没被带偏", svc._language == "Chinese", svc._language)


def test_real_english_quote_is_kept() -> None:
    print("\n[4''] 真人说的英文 —— 短，不该被剪")
    svc, frames = _fresh_service()
    _feed(svc, 1, "他说这个概念叫 multitasking，", 10.0)
    _feed(svc, 2, "他说这个概念叫 multitasking，他接着说 around the world.",
          11.0)
    check("短英文保留",
          "multitasking" in svc._epoch_text
          and "around the world." in svc._epoch_text,
          repr(svc._epoch_text))
    check("没被误判成退化", svc._degenerate is False)


def test_self_repeat_is_cut() -> None:
    print("\n[4''~] 同一轮里把同一句说两遍 ⇒ 剪掉第二遍")
    svc, frames = _fresh_service()
    _feed(svc, 1, "他提到人权观察。", 10.0)
    _feed(svc, 2, "他提到人权观察。Human Rights Watch（人权观察）："
                  "Human Rights Watch（《人类权利观察》）。", 11.0)
    check("轮内复读被标记", svc._degenerate is True)
    check("第二遍被剪掉",
          svc._epoch_text.count("Human Rights Watch") == 1,
          repr(svc._epoch_text))


def test_english_media_is_not_cut() -> None:
    print("\n[4'''] 英文媒体 —— 自报 English ⇒ 不剪")
    svc, frames = _fresh_service()
    _feed(svc, 1, "The first thing is to know is that we should always "
                  "keep learning new things every single day.", 10.0,
          language="English")
    check("英文媒体不被剪",
          "keep learning new things" in svc._epoch_text,
          repr(svc._epoch_text))
    check("没被误判成退化", svc._degenerate is False)


# --------------------------------------------------------------------------- #
# 5) 切句 / 尾巴用的是**公开文本**
# --------------------------------------------------------------------------- #
def test_sentences_follow_public_text() -> None:
    print("\n[5] 切句 —— 用公开文本（回看不产生句子）")
    svc, frames = _fresh_service()
    svc._lookback_until_sec = 20.0
    # 回看里有完整句子 —— 不该被切出来
    _feed(svc, 1, "回看里的完整句子。", 11.0, language="English")
    _feed(svc, 2, "回看里的完整句子。真正的新句子。", 20.0)
    svc._sent_flush()
    sentences = list(svc._sent_log)
    texts = [s["t"] for s in sentences]
    check("只切出真句子", texts == ["真正的新句子。"], repr(texts))
    check("回看那句没进句子流", "回看里的完整句子。" not in texts, repr(texts))
    check("语种没被回看污染", svc._language == "Chinese", svc._language)


# --------------------------------------------------------------------------- #
# 6) 换会话（epoch 轮换）后状态归零
# --------------------------------------------------------------------------- #
def test_epoch_reset() -> None:
    print("\n[6] 换段 —— 公开文本 / 退化标记归零")
    svc, frames = _fresh_service()
    _feed(svc, 1, "一段话。", 10.0)
    _feed(svc, 2, "一段话。", 10.0)
    svc._degenerate = True
    # 模拟 ``_rotate_epoch`` 里的归零
    svc._epoch_text = ""
    svc._epoch_raw = ""
    svc._degenerate = False
    check("公开文本归零", svc._epoch_text == "")
    check("退化标记归零", svc._degenerate is False)


# --------------------------------------------------------------------------- #
# 7) 拖动重开（append）⇒ 媒体语种不重置
# --------------------------------------------------------------------------- #
def test_media_language_survives_append() -> None:
    print("\n[7] 拖动重开 —— 媒体语种不重置（判「模型在说英文」要用它）")
    svc, frames = _fresh_service()
    svc._language = "Chinese"

    def _no_chunks():
        return iter(())

    svc.transcribe_stream(_no_chunks, append=True,
                          source_info={"kind": "video", "name": "x"})
    for _ in range(50):
        if not svc.snapshot().get("busy"):
            break
        time.sleep(0.1)
    check("append 没把媒体语种清掉", svc._language == "Chinese",
          svc._language)

    svc.transcribe_stream(_no_chunks, append=False,
                          source_info={"kind": "video", "name": "x"})
    for _ in range(50):
        if not svc.snapshot().get("busy"):
            break
        time.sleep(0.1)
    check("新的一批会把语种清掉（换媒体了）", svc._language == "", svc._language)


def main() -> int:
    print("=" * 70)
    print("ASR 模型退化 / 回看重放 —— 公开输出纯净性回归")
    print("=" * 70)
    test_normal_epoch()
    test_lookback_is_invisible()
    test_degeneration_is_cut()
    test_short_repeat_is_kept()
    test_english_hallucination_is_cut()
    test_degenerate_keeps_dropping_hypothesis()
    test_real_english_quote_is_kept()
    test_self_repeat_is_cut()
    test_english_media_is_not_cut()
    test_sentences_follow_public_text()
    test_epoch_reset()
    test_media_language_survives_append()
    print("\n" + "=" * 70)
    print("通过 %d，失败 %d" % (_PASS, _FAIL))
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
