#!/usr/bin/env python3
"""回归：长音频增量识别不会中途卡死 / 不会在末块崩掉。

被测的缺陷
==========
**缺陷 1 —— 增量会话自锁**

SDK / C++ demo 的流式回退策略（C++ ``online_main.cc:482-497``、
SDK ``asr.py::_run_online_block``）是::

    stable_count = len(round_tokens) - ONLINE_ROLLBACK_TOKENS   # 回退末尾 5 个

模型每轮**至少**会重述这 5 个回退 token。于是当某一轮「这一秒音频
没有新内容」（静音 / 纯音乐 / 人声间隙）时::

    len(round_tokens) == 5  →  stable_count == 0  →  一个 token 都不提交
    → committed_token_ids 不变 → 下一轮模型输入不变 → 输出不变
    → **永久自锁**

实测（97 分钟真人访谈视频）：t≈26 s 触发，之后 **395 秒零输出**；
直到某一轮模型偶然多吐几个 token 才解冻，然后把积压内容一次性倒出来
（每轮 +700 字，远超语速），文本与音频彻底脱节，最后退化成
``<小红帽>《小红帽》`` 式的无限重复。

修复（``wdnapps_audio_chat/cards/qwen3_asr/service.py``）：

1. **会话分段**：``_FILE_SESSION_MAX_SEC`` 900 → 60（对齐 C demo 的
   ``ONLINE_MAX_AUDIO_SECONDS``）—— 长音频靠**分段会话**而不是单个
   超长会话；
2. **停滞检测**：已确认文本连续 8 轮不增长 → 提前切 epoch 换新会话。

**缺陷 2 —— 末块太短把 worker 打崩**

末块的长度 = 音频总长 mod 块长。只要总长没落在块网格上，末块就可能
只有几十个采样（97 分钟真人访谈视频的末块：196 采样 = 12 ms）。SDK 的
mel 前端按 C 的语义把音频裁到 ``n_frame * hop``（1 帧 → 160 采样），
而反射填充需要 ``n_fft // 2``（200）采样 —— C 的 ``reflect_pad`` 在那里
是**越界读**（UB），numpy 版则直接抛::

    ValueError: could not broadcast input array from shape (160,)
    into shape (200,)

→ worker 抛异常 → 整场识别报废（前面 97 分钟的文本一起丢）。

修复：SDK 侧 ``_reflect_pad`` 只反射**实际存在**的采样（越界处补 0），
且在线路径的块长下限提到 ``N_FFT + HOP_LENGTH``（够凑 2 帧 mel）；
主进程侧再加一道容错 —— **单段收尾失败不毁全场**。

本脚本怎么测
============
两个用例，各自造一段音频：

* **用例 A（自锁）**：前 10 s **数字静音** → 必然踩中「这一秒没有新内容」
  → 触发自锁；后 15 s 真实语音（``asr_en.wav`` 的内容）→ 修复前**完全
  识别不到**。断言：语音被识别出来 / 没有 ``language None`` / 没有重复
  退化 / 分成了 ≥ 2 个会话（``epochs``）。
* **用例 B（末块）**：6 s 真实语音 + **100 个采样**的尾巴（末块 = 100
  采样 < 560）→ 修复前 worker 必崩。断言：状态是 ``done``（不是
  ``error``）/ 语音被识别出来 / 报错里没有 ``could not broadcast``。

跑法：``.venv/bin/python scripts/verify_asr_long_audio.py``
（需要服务在跑；只发识别请求，不落盘到板子）
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

BASE = "http://127.0.0.1:8000"

#: 静音前缀（秒）—— 让模型先踩进「没有新内容」的状态。
SILENCE_LEAD_SEC = 10.0

#: 用例 B 的语音长度（秒）与尾巴长度（采样）。
TAIL_CASE_SEC = 6.0
#: 末块必须 < ``N_FFT + HOP_LENGTH``（560 @ 16 kHz）才会踩到越界。
TAIL_CASE_TAIL_SAMPLES = 100

#: 参考语音里的关键词（任一命中即认为识别成功）。大小写不敏感。
KEYWORDS = ("listening", "music", "writing", "solo")

_PASS, _FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"
_FAILED: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    print(f"[{_PASS if ok else _FAIL}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        _FAILED.append(label)
    return ok


# --------------------------------------------------------------------------- #
# 测试音频：静音 + asr_en.wav
# --------------------------------------------------------------------------- #
def _find_asr_en_wav() -> Path | None:
    """找一个 24-bit 48 kHz 的 ``asr_en.wav``（C demo 的资产）。"""
    cands = [
        # C++ demo 的原始资产（rknn_Qwen3_ASR_demo）
        Path("/userdata/rknn3/rknn3-model-zoo/install/rk3588_linux_aarch64/"
             "rknn_Qwen3_ASR_demo/asr_en.wav"),
        Path("/userdata/assets/uploads/asr_en.wav"),
        _ROOT / ".venv/src/wdnai/examples/Qwen3_ASR/asr_en.wav",
    ]
    for c in cands:
        if c.is_file():
            return c
    return None


def _to_s16_mono(raw: bytes) -> bytes:
    """任意位深的单声道 PCM → s16le（按幅度归一，不做降采样）。

    与 ``scripts/make_asr_live_wav.py`` 同一套转换。
    """
    import numpy as np  # noqa: PLC0415

    from app.core.audio.wav import parse_wav  # noqa: PLC0415

    w = parse_wav(raw)
    if w.sample_width == 2:
        return w.pcm
    if w.sample_width == 3:
        b = np.frombuffer(w.pcm, dtype=np.uint8).reshape(-1, 3)
        v = (b[:, 0].astype(np.int32)
             | (b[:, 1].astype(np.int32) << 8)
             | (b[:, 2].astype(np.int32) << 16))
        sign = (b[:, 2] & 0x80) != 0
        v[sign] -= 0x1000000
        return np.clip((v.astype(np.float32) / 8388608.0) * 32768.0,
                       -32768.0, 32767.0).astype("<i2").tobytes()
    raise RuntimeError("暂不支持 {} 字节每采样".format(w.sample_width))


def _speech_s16() -> tuple:
    """取 ``asr_en.wav`` 的 16 kHz 单声道 s16 PCM。返回 ``(pcm, 源文件)``。"""
    src = _find_asr_en_wav()
    if src is None:
        raise FileNotFoundError("找不到 asr_en.wav")
    from app.core.audio.capture import resample_s16_linear  # noqa: PLC0415

    raw = src.read_bytes()
    s16 = _to_s16_mono(raw)
    from app.core.audio.wav import parse_wav  # noqa: PLC0415
    rate = parse_wav(raw).sample_rate
    if rate != 16000:
        s16 = resample_s16_linear(s16, rate, 16000)
    return s16, src


def _write_wav(dest: Path, pcm: bytes) -> Path:
    with wave.open(str(dest), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm)
    return dest


def _build_test_wav(dest: Path) -> tuple:
    """造「静音 + 语音」的 16 kHz 单声道 s16 WAV（用例 A：自锁）。

    返回 ``(路径, 源文件)``。识别是否成功用**关键词**判定
    （见 :data:`KEYWORDS`），不做全文比对 —— 全文比对会因分段而
    在段边界产生差异。
    """
    s16, src = _speech_s16()
    silence = b"\x00" * (int(SILENCE_LEAD_SEC * 16000) * 2)
    return _write_wav(dest, silence + s16), src


def _build_tail_wav(dest: Path) -> tuple:
    """造「语音 + 极短尾巴」的 16 kHz 单声道 s16 WAV（用例 B：末块越界）。

    尾巴只有 :data:`TAIL_CASE_TAIL_SAMPLES` 个采样 → SDK 切出来的末块
    也是这么长 → **小于** ``N_FFT + HOP_LENGTH``（560）→ 修复前 worker
    必崩（``ValueError: could not broadcast input array from shape
    (160,) into shape (200,)``）。
    """
    s16, src = _speech_s16()
    keep = int(TAIL_CASE_SEC * 16000) * 2
    tail = b"\x00" * (TAIL_CASE_TAIL_SAMPLES * 2)
    return _write_wav(dest, s16[:keep] + tail), src


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read().decode())


def _post(path: str, body: str):
    req = urllib.request.Request(
        BASE + path, data=body.encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"http_error": e.code, "body": e.read().decode()[:300]}


# --------------------------------------------------------------------------- #
def _transcribe(wav_path: Path, timeout: float = 600.0) -> tuple:
    """跑一次识别并等它结束。返回 ``(snapshot, last_result, text, elapsed)``。"""
    _post("/api/qwen3-asr/reset", "")
    time.sleep(0.5)
    resp = _post("/api/qwen3-asr/transcribe",
                 "path=" + urllib.parse.quote(str(wav_path)))
    if "http_error" in resp:
        print(f"[{_FAIL}] 发起识别失败: {resp}")
        return {}, {}, "", 0.0

    t0 = time.time()
    snap = {}
    while time.time() - t0 < timeout:
        time.sleep(1.0)
        snap = _get("/api/qwen3-asr/")
        if not snap.get("busy"):
            break
    lr = snap.get("last_result") or {}
    return snap, lr, (lr.get("text") or ""), time.time() - t0


def _worst_repeat(text: str) -> tuple:
    """最严重的重复子串（出现 ≥ 3 次）。返回 ``(子串, 次数)``。"""
    worst, worst_n = "", 0
    for i in range(0, len(text), 7):
        for j in range(i + 15, min(i + 600, len(text))):
            s = text[i:j]
            n = text.count(s)
            if n >= 3 and len(s) > len(worst):
                worst, worst_n = s, n
    return worst, worst_n


# --------------------------------------------------------------------------- #
def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    args = ap.parse_args()
    BASE = args.base

    print("=== 长音频增量识别：中途卡死 / 末块越界 回归 ===\n")
    try:
        _get("/api/qwen3-asr/")
    except Exception as exc:  # noqa: BLE001
        print(f"[{_FAIL}] 服务不可达: {exc}")
        return 1

    with tempfile.TemporaryDirectory() as td:
        # ---------------- 用例 A：静音触发自锁 -------------------------- #
        print("--- 用例 A：静音 + 语音（缺陷 1：增量会话自锁）---")
        wav_path, src = _build_test_wav(Path(td) / "long_audio_case.wav")
        print(f"源音频: {src}")
        print(f"测试音频: {wav_path} (静音 {SILENCE_LEAD_SEC:.0f}s + 语音)\n")

        snap, lr, text, elapsed = _transcribe(wav_path)
        print(f"耗时 {elapsed:.1f}s  audio_sec={lr.get('audio_sec')} "
              f"segments={lr.get('segments')} text_len={len(text)}\n")

        # A1) 语音段被识别出来
        low = text.lower()
        hit = [k for k in KEYWORDS if k in low]
        check(bool(hit),
              "A1 静音之后的真实语音被识别出来（修复前会被自锁吃掉）",
              f"命中关键词 {hit} / 文本前 120 字 {text[:120]!r}")

        # A2) 没有 language None 混进正文
        check("language none" not in low,
              "A2 正文里没有混入 'language None'",
              f"出现 {low.count('language none')} 次")

        # A3) 没有重复退化
        worst, worst_n = _worst_repeat(text)
        check(worst_n < 3,
              "A3 没有重复退化（同一子串出现 ≥ 3 次）",
              f"最严重: {worst_n} 次 x {len(worst)} 字符 {worst[:60]!r}")

        # A4) 分了多个会话 —— 说明不是「一个超长会话跑到底」
        epochs = int(lr.get("epochs") or 0)
        check(epochs >= 2,
              "A4 长音频被切成了多个会话（不是单个超长会话）",
              f"epochs={epochs} segments={lr.get('segments')}")

        # ---------------- 用例 B：极短末块 -------------------------- #
        print("\n--- 用例 B：语音 + {} 采样的尾巴（缺陷 2：末块越界）---"
              .format(TAIL_CASE_TAIL_SAMPLES))
        tail_wav, _ = _build_tail_wav(Path(td) / "short_tail_case.wav")
        print(f"测试音频: {tail_wav}\n")

        snap, lr, text, elapsed = _transcribe(tail_wav)
        err = snap.get("error") or ""
        print(f"耗时 {elapsed:.1f}s  state={snap.get('state')} "
              f"text_len={len(text)}  error={err[:120]!r}\n")

        # B1) 状态是 done（不是 error）
        check(snap.get("state") == "done" and not err,
              "B1 极短末块没有让 worker 崩（修复前是识别失败）",
              f"state={snap.get('state')} error={err[:160]!r}")

        # B2) 语音仍然被识别出来
        low = text.lower()
        hit = [k for k in KEYWORDS if k in low]
        check(bool(hit),
              "B2 极短末块不影响正文识别",
              f"命中关键词 {hit} / 文本前 120 字 {text[:120]!r}")

        # B3) 报错里没有 numpy 广播错误
        check("could not broadcast" not in err.lower()
              and "could not broadcast" not in text.lower(),
              "B3 没有 numpy 广播越界报错",
              f"error={err[:160]!r}")

    print()
    if _FAILED:
        print(f"[{len(_FAILED)} 项失败] " + "; ".join(_FAILED))
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
