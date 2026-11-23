#!/usr/bin/env python3
"""M1 — **语言对膨胀比 k 表**（译音时长 ÷ 源语时长）。

同传的产能账全靠这张表：TTS 单卡产能只有 **1.02× 实时**，所以
* k ≤ 1.02 ⇒ 滞后恒定（够用）
* k > 1.02 ⇒ 滞后随讲话时长**持续增长**（只能靠 tempo 压，压不动就如实显示）

量法：

1. 用**文件路径**跑一遍 ASR（英文素材）⇒ 从 ``translation`` 帧拿到
   每句的 ``start_sec`` / ``end_sec`` ⇒ **源句时长**；
2. 同一句分别翻成 10 种可发声语言（``POST /translate/line``）；
3. 每条译文送 TTS 合成 ⇒ 快照里的 ``audio_sec`` = **译音时长**；
4. k = 译音时长 ÷ 源句时长。

用法::

    python m1_lang_expansion.py [--audio PATH] [--out FILE]
"""
from __future__ import annotations

import argparse
import json
import ssl
import statistics
import threading
import time
from pathlib import Path

import requests
import websockets

BASE = "https://127.0.0.1:8000"
WS_URL = "wss://127.0.0.1:8000/api/qwen3-asr/ws"
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

LANGS = ["chinese", "english", "german", "italian", "portuguese",
         "spanish", "japanese", "korean", "french", "russian"]

SRC_SENTENCES = [
    "The weather is very nice today, and the park near my house is full of "
    "people who came out for a walk after dinner.",
    "I have been thinking about how much has changed in this small town over "
    "the past ten years.",
    "Now the factory has become a small park with a playground and a coffee "
    "shop, and the school has doubled in size.",
    "I do not miss the noise from the factory, but I do miss the way the "
    "whole street used to be busy every morning.",
]


class FrameBus:
    """收集 WS 上的 translation 帧（按 seq 取）。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.by_seq: dict[int, dict] = {}
        self.sentences: dict[int, dict] = {}
        self.done_seen = False
        self.stop = threading.Event()

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        import asyncio

        async def _main():
            while not self.stop.is_set():
                try:
                    async with websockets.connect(
                            WS_URL, ssl=SSL_CTX, open_timeout=10,
                            ping_interval=None) as ws:
                        while not self.stop.is_set():
                            raw = await asyncio.wait_for(ws.recv(), timeout=5)
                            try:
                                msg = json.loads(raw)
                            except Exception:  # noqa: BLE001
                                continue
                            if msg.get("type") != "translation":
                                if msg.get("type") == "done":
                                    with self.lock:
                                        self.done_seen = True
                                continue
                            with self.lock:
                                if msg.get("seq") is not None:
                                    self.by_seq[int(msg["seq"])] = msg
                                else:
                                    idx = int(msg.get("index") or 0)
                                    self.sentences[idx] = msg
                except Exception:  # noqa: BLE001
                    time.sleep(1.0)

        asyncio.run(_main())

    def wait_seq(self, seq: int, timeout: float = 60.0) -> dict | None:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            with self.lock:
                if seq in self.by_seq:
                    return self.by_seq.pop(seq)
            time.sleep(0.05)
        return None

    def clear(self) -> None:
        with self.lock:
            self.by_seq.clear()
            self.sentences.clear()

    def wait_done(self, timeout: float = 300.0) -> bool:
        """等识别结束（``done`` 帧）—— 免得把**上一次**识别的帧当这次的。"""
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            with self.lock:
                if self.done_seen:
                    return True
            time.sleep(0.1)
        return False

    def wait_sentence(self, count: int, timeout: float = 120.0) -> list:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            with self.lock:
                if len(self.sentences) >= count:
                    return [self.sentences[k] for k in sorted(self.sentences)]
            time.sleep(0.1)
        with self.lock:
            return [self.sentences[k] for k in sorted(self.sentences)]


def _session() -> requests.Session:
    s = requests.Session()
    s.verify = False
    s.trust_env = False
    return s


def run_asr(s: requests.Session, audio: str) -> list:
    """跑一遍文件路径 ASR，返回带 start/end 的译文帧（源句时长）。"""
    s.post(BASE + "/api/qwen3-asr/translate/settings",
           json={"enabled": True, "target_lang": "chinese"}, timeout=30)
    r = s.post(BASE + "/api/qwen3-asr/transcribe",
               data={"path": audio}, timeout=300)
    r.raise_for_status()
    return []


def tts_audio_sec(s: requests.Session, text: str, language: str) -> float:
    """合成一句，返回音频时长（秒）。"""
    r = s.post(BASE + "/api/tts/start",
               json={"text": text, "language": language, "volume": 0.02,
                     "play_to": "device"},
               timeout=60)
    if r.status_code != 200:
        raise RuntimeError("tts start: %s %s" % (r.status_code, r.text[:200]))
    t0 = time.monotonic()
    while time.monotonic() - t0 < 120:
        snap = s.get(BASE + "/api/tts/", timeout=30).json()
        if not snap.get("busy") and snap.get("state") == "done":
            return float(snap.get("audio_sec") or 0.0)
        time.sleep(0.4)
    raise RuntimeError("tts 超时")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", default="/userdata/assets/uploads/asr_en.wav")
    ap.add_argument("--out", default="/userdata/assets/uploads/.m1_k_table.json")
    ap.add_argument("--max-sentences", type=int, default=0,
                    help="每个语言最多翻几句（0 = 全部）")
    args = ap.parse_args()

    s = _session()
    bus = FrameBus()
    bus.start()
    time.sleep(1.0)

    # ---- 1. 源句时长（跑一遍 ASR，拿 translation 帧的 start/end） ------ #
    print("[1] 跑 ASR 文件路径，收集源句时长 …")
    bus.clear()
    run_asr(s, args.audio)
    bus.wait_done(timeout=300)
    # ⚠️ **翻译是异步的** —— ``done`` 帧只说明「识别完了」，译文还在路上
    # ⇒ 等**翻译队列排空**再收帧（不然只收到第一句）。
    t0 = time.monotonic()
    while time.monotonic() - t0 < 180:
        try:
            st = s.get(BASE + "/api/qwen3-asr/translate/status", timeout=20).json()
            if int(st.get("queue_depth") or 0) == 0:
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    time.sleep(1.0)          # 尾巴上的译文帧
    frames = bus.wait_sentence(1, timeout=60)
    src_durs: list[float] = []
    src_texts: list[str] = []
    for fr in frames:
        if fr.get("start_sec") is None or fr.get("end_sec") is None:
            continue
        dur = float(fr["end_sec"]) - float(fr["start_sec"])
        if dur <= 0.2:
            continue
        text = fr.get("source") or ""
        # ⚠️ 模型**偶发退化**会把英文音频转成中文短句 ⇒ 那种句子算出来的
        # k 没有意义 ⇒ 只留**含 ASCII 字母**的句子。
        if not any(c.isascii() and c.isalpha() for c in text):
            continue
        src_durs.append(dur)
        src_texts.append(text)
    print(f"    拿到 {len(src_durs)} 句（英文）："
          + ", ".join("%.2fs" % d for d in src_durs))
    for d, t in zip(src_durs, src_texts):
        print(f"      {d:.2f}s  {t[:60]}")
    # ⚠️ **取最长的那句** —— 短句（1 s 出头）算出来的 k 噪声太大。
    if src_durs:
        k_long = max(range(len(src_durs)), key=lambda i: src_durs[i])
        src_durs = [src_durs[k_long]]
        src_texts = [src_texts[k_long]]
        print(f"    取最长的那句（{src_durs[0]:.2f}s）做基准")
    if args.max_sentences:
        src_durs = src_durs[: args.max_sentences]
        src_texts = src_texts[: args.max_sentences]
    if not src_durs:
        print("    ⚠ 没拿到源句时长 ⇒ 用固定素材时长兜底")
        src_durs = [4.0] * len(SRC_SENTENCES)
        src_texts = list(SRC_SENTENCES)

    # ---- 2. 逐语言：翻译 → 合成 → k ---------------------------------- #
    table: dict[str, list[float]] = {}
    texts: dict[str, list[str]] = {}
    # ⚠️ **seq 必须唯一且远离 0** —— 之前 M6 压测往翻译队列里灌了几千条
    # 带 seq=0/1/2… 的请求 ⇒ 按 seq 匹配会**捡到旧的帧**（同一条中文）⇒
    # 每个语言的 k 都一样。用一个高基数偏移避开。
    seq_base = 900000 + (int(time.time()) % 1000) * 100
    for lang in LANGS:
        s.post(BASE + "/api/qwen3-asr/translate/settings",
               json={"target_lang": lang}, timeout=30)
        ks: list[float] = []
        ts: list[str] = []
        for i, (dur, stext) in enumerate(zip(src_durs, src_texts)):
            seq = seq_base + i
            s.post(BASE + "/api/qwen3-asr/translate/line",
                   json={"text": stext, "lang": "english", "seq": seq},
                   timeout=60)
            fr = bus.wait_seq(seq, timeout=90)
            if not fr or fr.get("status") != "done":
                print(f"    [{lang}] 第 {i} 句翻译没回来"
                      f"（status={fr and fr.get('status')}）")
                continue
            text = fr.get("text") or ""
            if not text:
                continue
            try:
                sec = tts_audio_sec(s, text, lang)
            except Exception as exc:  # noqa: BLE001
                print(f"    [{lang}] 第 {i} 句合成失败: {exc}")
                continue
            ks.append(sec / dur)
            ts.append(text)
            print(f"    [{lang}] {i}: 源 {dur:.2f}s → 译音 {sec:.2f}s "
                  f"⇒ k={sec / dur:.2f}")
        if ks:
            table[lang] = ks
            texts[lang] = ts

    # ---- 3. 汇总 ------------------------------------------------------- #
    print("\n=== k 表（译音时长 ÷ 源语时长）===")
    print(f"{'语言':<12}{'n':<4}{'k 均值':<9}{'k 中位':<9}{'k 最大':<9}结论")
    summary = {}
    for lang, ks in table.items():
        m = statistics.mean(ks)
        med = statistics.median(ks)
        mx = max(ks)
        verdict = "够(≤1.02)" if med <= 1.02 else (
            "tempo 可压(≤1.15)" if med <= 1.15 else "⚠ 压不住")
        print(f"{lang:<12}{len(ks):<4}{m:<9.2f}{med:<9.2f}{mx:<9.2f}{verdict}")
        summary[lang] = {"n": len(ks), "mean": m, "median": med, "max": mx,
                         "verdict": verdict}
    Path(args.out).write_text(json.dumps(
        {"k_table": table, "summary": summary, "texts": texts},
        ensure_ascii=False, indent=2))
    print("结果写入", args.out)
    bus.stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
