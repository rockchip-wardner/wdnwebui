#!/usr/bin/env python3
"""M27 —— **TTS 产能体检**：瓶颈在 talker（自回归）还是在解码器？

用户报（2026-11-23）：「ASR+翻译之后 TTS 播出声音太久了」「中间还是会有顿挫」。

这一版只回答一个问题：**这块板上的 TTS 到底能产出多少音频秒 / 墙钟秒**，
以及**产能被谁卡住**：

===========================  ==================================================
量                           口径
===========================  ==================================================
``frames``                   talker 产出的帧数（1 帧 = 80 ms 音频）
``audio_sec``                产出的音频秒数（= frames × 0.08）
``gen_sec``                  talker + 解码器的总耗时（worker 日志）
``dec_sec``                  **解码器**（speech_decoder）单独耗时
``talker_sec``               ``gen_sec − dec_sec`` ⇒ talker 的耗时
``产能 r``                    ``audio_sec / gen_sec``（含首窗 ⇒ **真实可持续速率**）
``边际 r``                    首窗之后的产量速率（队列的 ``m_rate``）
===========================  ==================================================

⚠️ **为什么必须先分清 talker / 解码器**：
解码器每个窗口喂 ``kLeftContextSize + kChunkSize`` = 50 帧、只用后 25 帧
⇒ **每帧被解码两次** ⇒ 若解码器是瓶颈，产能有**接近一倍**的提升空间
（把窗口加大 / 减少左上下文冗余）；若 talker 是瓶颈（自回归，一步一帧），
⇒ 产能就是**模型在这块板上的物理上限**，只能靠「内容更短」或「接受滞后」。

用法::

    .venv/bin/python tools/probes/interp/m27_tts_capacity.py [--long]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import time
import urllib.request

BASE = "https://127.0.0.1:8000"
WORKER_LOG = "/tmp/wdnwebui-worker/qwen3-tts.log"

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

#: 该文件（asr_en.wav）的真实译文 —— 端到端复现用。
FILE_SENTENCES = [
    "嗯嗯。哦对了，当然了！",
    "当他开始给我听的时候，他甚至还没有那么大呢。",
    "在他的独奏作品中，表现并不出色，但他的表现还是不错的。",
    "嗯，当他开始为其他人写作的时候。",
]

#: 产能测试用：长句 ⇒ 首窗与句级开销被摊薄 ⇒ 看到的是**边际**产能。
LONG_SENTENCES = [
    "在这项测试里我们要连续合成一段比较长的中文，用来测量这块板上的语音合成"
    "到底能产出多少秒的音频，以及这些时间分别花在了哪里。",
    "第二个句子同样比较长，目的是让合成器进入稳定状态，从而排除首窗延迟对"
    "整体速率的干扰，得到一个可以代表真实产能的数字。",
]

_RE_TALKER = re.compile(
    r"\[talker\+decoder\]\s+(\d+)\s+frames,\s+([\d.]+)s of audio in ([\d.]+)s")
_RE_DEC = re.compile(r"\[decoder\]\s+(\d+)\s+calls,\s+([\d.]+)s")


def _req(path: str, payload=None, method=None, timeout=180):
    url = BASE + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method or ("POST" if data else "GET"))
    with urllib.request.urlopen(req, context=_CTX, timeout=timeout) as r:
        raw = r.read()
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return {"_raw": raw[:200].decode("utf-8", "replace")}


def _log_size() -> int:
    try:
        return os.path.getsize(WORKER_LOG)
    except OSError:
        return 0


def _log_since(offset: int) -> str:
    try:
        with open(WORKER_LOG, "rb") as f:
            f.seek(offset)
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _parse_log(text: str) -> list:
    """把 worker 日志切成一句一句的产能记录。"""
    rows = []
    cur = {}
    for line in text.splitlines():
        m = _RE_TALKER.search(line)
        if m:
            if cur:
                rows.append(cur)
            cur = {
                "frames": int(m.group(1)),
                "audio_sec": float(m.group(2)),
                "gen_sec": float(m.group(3)),
                "dec_sec": 0.0,
                "dec_calls": 0,
            }
            continue
        m = _RE_DEC.search(line)
        if m and cur:
            cur["dec_calls"] = int(m.group(1))
            cur["dec_sec"] = float(m.group(2))
    if cur:
        rows.append(cur)
    for r in rows:
        r["talker_sec"] = round(max(0.0, r["gen_sec"] - r["dec_sec"]), 3)
        r["r_all"] = round(r["audio_sec"] / r["gen_sec"], 3) if r["gen_sec"] else 0.0
        # 首窗 = 25 帧（2 s 音频）；边际速率 = 首窗之后的产量
        first_audio = min(r["audio_sec"], 2.0)
        rest_audio = max(0.0, r["audio_sec"] - first_audio)
        first_time = r["gen_sec"] * (first_audio / r["audio_sec"]) if r["audio_sec"] else 0.0
        rest_time = max(0.0, r["gen_sec"] - first_time)
        r["r_marginal"] = round(rest_audio / rest_time, 3) if rest_time > 0.2 else 0.0
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--long", action="store_true",
                    help="用长句测边际产能（默认用该文件的真实译文）")
    args = ap.parse_args()

    sents = LONG_SENTENCES if args.long else FILE_SENTENCES

    print("== 清场 ==")
    _req("/api/tts/queue/clear", {}, method="POST")
    _req("/api/tts/queue/tempo", {"tempo": 1.0}, method="POST")
    time.sleep(1.0)

    off = _log_size()
    print("== 灌 %d 句 ==" % len(sents))
    t0 = time.time()
    for s in sents:
        r = _req("/api/tts/queue/submit", {"text": s, "volume": 0.5})
        print("   seq=%s est=%.2fs %s" % (r.get("seq"), float(r.get("est_audio_sec") or 0),
                                         s[:18]))
    # 等队列播完
    deadline = time.time() + 180
    while time.time() < deadline:
        st = _req("/api/tts/queue/status")
        if int(st.get("depth") or 0) == 0 and not st.get("playing"):
            break
        time.sleep(0.5)
    wall = time.time() - t0
    time.sleep(1.0)

    rows = _parse_log(_log_since(off))
    st = _req("/api/tts/queue/status")
    tms = st.get("timings") or []

    print("\n== 合成侧（worker 日志）==")
    print("%-4s %-7s %-10s %-10s %-10s %-9s %-9s" % (
        "#", "帧", "音频s", "总耗时s", "解码s", "talker s", "产能r"))
    tot_audio = 0.0
    tot_gen = 0.0
    tot_dec = 0.0
    for i, r in enumerate(rows):
        tot_audio += r["audio_sec"]
        tot_gen += r["gen_sec"]
        tot_dec += r["dec_sec"]
        print("%-4s %-7s %-10.2f %-10.2f %-10.2f %-9.2f %-9.3f" % (
            i + 1, r["frames"], r["audio_sec"], r["gen_sec"], r["dec_sec"],
            r["talker_sec"], r["r_all"]))
    if rows:
        print("   合计：音频 %.2f s / 合成 %.2f s ⇒ **产能 r = %.3f**"
              % (tot_audio, tot_gen, tot_audio / tot_gen if tot_gen else 0))
        print("   解码器占比 %.1f%%  talker 占比 %.1f%%"
              % (100.0 * tot_dec / tot_gen if tot_gen else 0,
                 100.0 * (tot_gen - tot_dec) / tot_gen if tot_gen else 0))
        print("   解码器：%.2f s / %d 次调用 ⇒ %.0f ms/次"
              % (tot_dec, sum(r["dec_calls"] for r in rows),
                 1000.0 * tot_dec / max(1, sum(r["dec_calls"] for r in rows))))

    print("\n== 播放侧（队列 timings）==")
    print("%-4s %-9s %-9s %-9s %-9s %-8s %-8s %-8s" % (
        "#", "音频s", "首窗s", "合成s", "边际r", "gap", "starv", "tempo"))
    for t in tms[-len(sents):]:
        print("%-4s %-9.2f %-9.2f %-9.2f %-9.3f %-8.3f %-8.2f %-8.2f" % (
            t.get("seq"), float(t.get("audio_sec") or 0), float(t.get("synth_lat") or 0),
            float(t.get("o_rate") or 0) * 0 + float(t.get("synth_lat") or 0),
            float(t.get("m_rate") or 0), float(t.get("gap") or 0),
            float(t.get("starved") or 0), float(t.get("tempo") or 0)))
    print("\n   墙钟（提交→播完）= %.2f s" % wall)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
