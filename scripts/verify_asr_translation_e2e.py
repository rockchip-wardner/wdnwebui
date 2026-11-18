#!/usr/bin/env python3
"""真机端到端：ASR 实时识别 ⇒ 端点切句 ⇒ **译文**推回浏览器。

这条脚本验证的是**整条链路**（不是 mock）：

```
PCM ──► ASR worker ──► endpoint 帧 ──► 翻译服务 ──► MT worker ──► translation 帧
```

跑之前要满足：

1. **ASR 模型在跑**（``qwen3-asr``）—— 没有就自己起：
   ``POST /api/compute/instances {"model":"qwen3-asr","npu_index":0}``；
2. **翻译模型在跑**（``hy-mt-1-5``）—— 脚本会调
   ``POST /api/qwen3-asr/translate/enable`` 自己起（等 ~30 s）；
3. 板载麦 / 浏览器麦 / 文件路径都**不用** —— 脚本自己拿
   ``asr_en.wav`` 当麦克风喂进去。

⚠️ **板端地址**默认 ``https://127.0.0.1:8000``（自签证书 ⇒ 跳过校验）。

用法::

    .venv/bin/python scripts/verify_asr_translation_e2e.py
    .venv/bin/python scripts/verify_asr_translation_e2e.py --target zh-CN
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import sys
import time
from pathlib import Path

import numpy as np
import requests
import websockets

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))          # 宿主模块（app.core.audio.capture）
DEFAULT_WAV = Path("/userdata/assets/uploads/asr_en.wav")

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
# HTTP / WS 工具
# --------------------------------------------------------------------------- #
def _base(args) -> str:
    return args.base.rstrip("/")


def _http(args, method: str, path: str, payload=None, timeout: float = 60.0,
          headers=None, data=None):
    url = _base(args) + path
    kw = {"verify": False, "timeout": timeout}
    if payload is not None:
        kw["json"] = payload
    if data is not None:
        # ⚠️ 音频块是**裸 PCM**（不是 JSON）—— 服务端读的是 request body
        kw["data"] = data
        kw.setdefault("headers", {})["Content-Type"] = "application/octet-stream"
    if headers:
        kw["headers"] = {**(kw.get("headers") or {}), **headers}
    return requests.request(method, url, **kw)


def _load_pcm(wav_path: Path, tail_silence_s: float = 2.0, dst_rate: int = 16000):
    """读 WAV → s16le 单声道裸 PCM，**重采样到 16 kHz**。

    ⚠️ 实时会话**只收 16 kHz**（服务端校验：``sample_rate 必须是 16000``）——
    浏览器那边（``app.js`` 的 ``_resampleTo16k``）就是先降采样再发。

    ⚠️ 这里用**宿主自己的** ``resample_s16_linear``（``app.core.audio.capture``）
    —— 与浏览器同一套算法、同一套「向零截断」量化规则（差 1 LSB 就会让
    两端的 PCM 不一致）。

    ⚠️ 末尾补一段**静音** —— 端点检测靠静音停顿切句；不补静音的话最后一句
    没有端点（收尾时会补提交，见 ``service.py::_finalize``）。
    """
    import wave
    with wave.open(str(wav_path), "rb") as w:
        rate = w.getframerate()
        width = w.getsampwidth()
        channels = w.getnchannels()
        raw = w.readframes(w.getnframes())
    if channels > 1:
        arr = np.frombuffer(raw, dtype=np.uint8).reshape(-1, channels)
        arr = arr[:, 0]
        raw = arr.tobytes()
    if width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        s32 = (b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16))
        s32 = np.where(s32 & 0x800000, s32 - 0x1000000, s32)
        pcm = (s32 >> 8).astype("<i2").tobytes()
    elif width == 2:
        pcm = raw
    else:
        raise SystemExit("只支持 16/24 bit WAV，实际 %d bit" % (width * 8))
    if tail_silence_s > 0:
        pcm += b"\x00" * int(rate * tail_silence_s) * 2
    if rate != dst_rate:
        from app.core.audio.capture import resample_s16_linear
        pcm = resample_s16_linear(pcm, rate, dst_rate)
        rate = dst_rate
    return pcm, rate


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
async def run(args) -> int:
    print("=" * 72)
    print("真机端到端：实时识别 ⇒ 译文")
    print("=" * 72)

    # --- 1. ASR 模型 ---
    snap = _http(args, "GET", "/api/compute").json()
    asr_running = any(i.get("model_id") == "qwen3-asr"
                      and i.get("status") == "running"
                      and i.get("_source") in ("spawned", "adopted_local")
                      for i in (snap.get("instances") or []))
    if not asr_running:
        print("  … ASR 模型没在跑，先起一个（~30 s）")
        _http(args, "POST", "/api/compute/instances",
              {"model": "qwen3-asr", "npu_index": 0})
        for _ in range(24):
            time.sleep(5)
            snap = _http(args, "GET", "/api/compute").json()
            asr_running = any(i.get("model_id") == "qwen3-asr"
                              and i.get("status") == "running"
                              for i in (snap.get("instances") or []))
            if asr_running:
                break
    check("ASR 模型在跑", asr_running)
    if not asr_running:
        print("❌ ASR 模型起不来，脚本到此为止")
        return 1

    # --- 2. 翻译模型（新端点） ---
    st = _http(args, "GET", "/api/qwen3-asr/translate/status").json()
    if not (st.get("model") or {}).get("running"):
        print("  … 翻译模型没在跑，用新端点起一个（~30 s）")
        _http(args, "POST", "/api/qwen3-asr/translate/enable", {}, timeout=220)
        st = _http(args, "GET", "/api/qwen3-asr/translate/status").json()
    check("翻译模型在跑", bool((st.get("model") or {}).get("running")),
          f"status={st.get('model')}")
    if not (st.get("model") or {}).get("running"):
        print("❌ 翻译模型起不来，脚本到此为止")
        return 1

    # --- 3. 打开实时翻译 ---
    _http(args, "POST", "/api/qwen3-asr/translate/settings",
          {"enabled": True, "target_lang": args.target})
    st = _http(args, "GET", "/api/qwen3-asr/translate/status").json()
    check("实时翻译已开启", bool(st.get("enabled")), f"status={st}")
    check("目标语言 = %s" % args.target, st.get("target_lang") == args.target,
          f"target={st.get('target_lang')}")

    # --- 4. 打开 WS，开实时会话，喂音频 ---
    pcm, rate = _load_pcm(args.wav)
    print("  … 样本 %s（%d Hz，%.1f s）"
          % (args.wav, rate, len(pcm) / 2 / rate))

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ws_url = _base(args).replace("https://", "wss://").replace("http://", "ws://")
    frames: list = []

    async with websockets.connect(ws_url + "/api/qwen3-asr/ws",
                                  ssl=ctx, open_timeout=15) as ws:

        async def _reader():
            try:
                async for raw in ws:
                    try:
                        frames.append(json.loads(raw))
                    except Exception:
                        pass
            except Exception:
                pass

        reader = asyncio.create_task(_reader())
        await asyncio.sleep(0.5)

        r = _http(args, "POST", "/api/qwen3-asr/live/start", {
            "source": "browser",
            "sample_rate": rate,
            "n_channels": 1,
            "sample_width": 2,
            "chunk_ms": 1000,
        })
        if r.status_code != 200:
            print("❌ live/start 失败: HTTP %s %s" % (r.status_code, r.text[:200]))
            reader.cancel()
            return 1
        stream_id = r.json().get("stream_id") or ""
        check("实时会话已开", bool(stream_id), f"stream_id={stream_id}")

        chunk_bytes = rate * 2          # 1 秒 = 48k × 2 byte
        seq = 0
        t0 = time.time()
        for off in range(0, len(pcm), chunk_bytes):
            block = pcm[off:off + chunk_bytes]
            rr = _http(args, "POST", "/api/qwen3-asr/live/chunk", timeout=30,
                       data=block,
                       headers={"X-Asr-Stream-Id": stream_id,
                                "X-Asr-Seq": str(seq)})
            if rr.status_code != 200:
                print("  … 第 %d 块被拒: HTTP %s %s"
                      % (seq, rr.status_code, rr.text[:120]))
                break
            seq += 1
            await asyncio.sleep(0.35)    # 实时节奏（略快于 1 s/块）
        print("  … 喂完 %d 块（%.1f s）" % (seq, time.time() - t0))

        # 等端点 + 译文（译文比端点慢 ~0.2~0.5 s）
        deadline = time.time() + 40
        while time.time() < deadline:
            kinds = [f.get("type") for f in frames]
            if "translation" in kinds and kinds.count("endpoint") >= 1:
                break
            await asyncio.sleep(0.5)

        _http(args, "POST", "/api/qwen3-asr/live/finish", timeout=60,
              headers={"X-Asr-Stream-Id": stream_id})
        await asyncio.sleep(2.0)
        reader.cancel()

    # --- 5. 断言 ---
    kinds = {}
    for f in frames:
        kinds[f.get("type")] = kinds.get(f.get("type"), 0) + 1
    print("  WS 帧统计: %s" % kinds)

    endpoints = [f for f in frames if f.get("type") == "endpoint"]
    translations = [f for f in frames if f.get("type") == "translation"]
    check("收到端点帧（一句话结束）", len(endpoints) >= 1, f"frames={kinds}")
    check("收到译文帧", len(translations) >= 1, f"frames={kinds}")

    for t in translations:
        src = (t.get("source") or "")[:60]
        dst = (t.get("text") or "")[:60]
        print("    原文: %s" % src)
        print("    译文: %s   [%s, %s ms]"
              % (dst, t.get("status"), t.get("latency_ms")))
        if t.get("reason"):
            print("    原因: %s" % t.get("reason"))

    done = [t for t in translations if t.get("status") == "done"]
    check("至少一句翻成功（status=done）", len(done) >= 1,
          f"statuses={[t.get('status') for t in translations]}")
    if done:
        t = done[0]
        check("译文不是空的", bool((t.get("text") or "").strip()),
              f"text={t.get('text')!r}")
        check("译文带原文（前端要对照显示）", bool((t.get("source") or "").strip()),
              f"source={t.get('source')!r}")
        check("译文不是复读原文（目标语言不同）",
              (t.get("text") or "").strip() != (t.get("source") or "").strip(),
              f"text={t.get('text')!r} source={t.get('source')!r}")

    print("\n" + "=" * 72)
    print(f"通过 {_PASS} / 失败 {_FAIL}")
    print("=" * 72)
    if _FAIL:
        print("❌ 有断言没过")
        return 1
    print("✅ 全部通过")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://127.0.0.1:8000")
    ap.add_argument("--wav", default=str(DEFAULT_WAV))
    ap.add_argument("--target", default="zh-CN")
    args = ap.parse_args()
    requests.packages.urllib3.disable_warnings()  # type: ignore
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main() or 0)
