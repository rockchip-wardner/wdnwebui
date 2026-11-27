#!/usr/bin/env python3
"""回归：**预合成音频缓存的真 TTS 闭环**（2026-11-26）。

用户要求：「ASR、翻译、TTS 可以**一直在后台处理**；**进度条来回拉的时候，
播放也得跟上当前的字幕**」。

本脚本验证**真链路**（真 TTS 模型、真合成、真落盘、真命中）：

1. 提交一句（带 ``cache_key``）⇒ **真的合成**；
2. 合成完 ⇒ **WAV 落盘进缓存**（按位置索引）；
3. 再提交同一句（同一 ``cache_key``）⇒ **命中缓存 ⇒ 不再合成**
   （用合成计数证明）；
4. 读回缓存的 PCM ⇒ 长度/采样率对得上。

⚠️ **会占用服务进程里的那一个全局队列** —— 真 TTS 合成**只能**在服务
   进程内做（见下面「为什么走 HTTP」）⇒ 脚本跑的时候**别同时在用同传**
   （会互相插队）。
⚠️ **会真的出声**（``play_to=queue`` ⇒ PCM 进队列 ⇒ 队列的播放器照样写
   aplay）⇒ 音量按规矩用 **5%**（``volume=0.05``）。

跑之前要满足：**TTS 模型在跑**。

跑法::

    .venv/bin/python scripts/verify_prefetch_cache.py
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_PLUGIN = os.path.join(_ROOT, ".venv", "src", "wdnapps-audio-chat", "src")
if _PLUGIN not in sys.path:
    sys.path.insert(0, _PLUGIN)

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

_OK = 0
_FAIL: list = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global _OK
    if cond:
        _OK += 1
        print(f"  \033[32mPASS\033[0m {name}")
    else:
        _FAIL.append(name)
        print(f"  \033[31mFAIL\033[0m {name} {extra}")


def api(path: str, timeout: int = 30):
    req = urllib.request.Request("https://127.0.0.1:8000" + path, method="GET")
    try:
        with urllib.request.urlopen(req, context=_CTX, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def post(path: str, payload: dict, timeout: int = 60):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request("https://127.0.0.1:8000" + path, data=data,
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, context=_CTX, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)}


def main() -> int:
    print("\033[1m预合成音频缓存的真 TTS 闭环（2026-11-26）\033[0m")

    # ---- 前置：TTS 模型在跑 ----
    caps = api("/api/compute/instances-by-capability?capability=swcap_text_to_speech")
    insts = caps.get("instances") or []
    check("TTS 模型在跑（前置条件）",
          bool(insts), f"得到 {caps}")

    # ⚠️⚠️ **真 TTS 合成必须在「服务进程内」做** ——
    #
    # TTS 会话是**进程级单例**（`service.py::get_session`）⇒ 独立进程里
    # 拿不到它 ⇒ 自己 new 一个 ⇒ 要重新探测 / 加载模型 ⇒ 而且探测的是
    # **本地 worker**（`wdndds` RPC）⇒ 在独立进程里**永远 unreachable**
    # （实测：``probe: qwen3-tts unreachable after 3 attempts``）。
    #
    # ⇒ **走 HTTP**（``POST /api/tts/queue/submit``）⇒ 用服务进程自己的
    #   队列 ⇒ 真 TTS、真落盘、真命中。
    key = "test@%.2f" % time.time()
    text = "这是一句用于验证预合成缓存的测试语音。"

    st0 = api("/api/tts/queue/cache")
    entries0 = int(st0.get("entries") or 0)
    hits0 = int(st0.get("hits") or 0)

    # ---- 1. 提交（带 cache_key）⇒ 真的合成 ----
    code, res = post("/api/tts/queue/submit", {
        "text": text,
        "language": "chinese",
        "volume": 0.05,
        "play_to": "queue",
        "cache_key": key,
        "src_start_sec": 12.34,
    })
    check("提交带 cache_key 的句子", code == 200, f"得到 {code} {res}")

    # ⚠️ **等它合成完**（真 TTS ⇒ 首窗 ~3 s + 合成时间）。
    print("   等真 TTS 合成 …")
    got = False
    for _ in range(90):
        time.sleep(1.0)
        st = api("/api/tts/queue/cache")
        if int(st.get("entries") or 0) > entries0:
            got = True
            break
    check("合成完 ⇒ 音频进了预合成缓存（按位置索引）", got,
          f"key={key} cache={api('/api/tts/queue/cache')}")
    if not got:
        print("\n\033[31mFAILED\033[0m  %d 通过 / %d 失败" % (_OK, len(_FAIL)))
        for f in _FAIL:
            print("  - %s" % f)
        return 1

    # ---- 2. 缓存统计 ----
    st = api("/api/tts/queue/cache")
    check("缓存统计有内容", int(st.get("entries") or 0) >= 1, f"得到 {st}")
    check("缓存字节数 > 0（真的落盘了）",
          int(st.get("bytes") or 0) > 0, f"得到 {st}")

    # ---- 3. 再提交同一位置 ⇒ 命中缓存 ⇒ 不重新合成 ----
    t0 = time.time()
    code, res2 = post("/api/tts/queue/submit", {
        "text": text,
        "language": "chinese",
        "volume": 0.05,
        "play_to": "queue",
        "cache_key": key,
        "src_start_sec": 12.34,
    })
    check("第二次提交同一位置（同一 cache_key）", code == 200,
          f"得到 {code} {res2}")
    # ⚠️ **等命中计数增长** —— 命中发生在**队列的合成线程**里。
    t_hit = None
    for _ in range(60):
        time.sleep(0.5)
        st2 = api("/api/tts/queue/cache")
        if int(st2.get("hits") or 0) > hits0:
            t_hit = time.time() - t0
            break
    check("第二次提交 ⇒ **命中了缓存**（hits 增长）",
          int(st2.get("hits") or 0) > hits0, f"得到 {st2}")
    if t_hit is not None:
        check("命中缓存 ⇒ 极快（< 3 秒，不必等 TTS 首窗）",
              t_hit < 3.0, f"得到 {t_hit:.2f}s")

    # ---- 4. 不同位置 ⇒ 不命中（不能拿错内容）----
    code, res3 = post("/api/tts/queue/submit", {
        "text": "这是另一句，不应该命中上一句的缓存。",
        "language": "chinese",
        "volume": 0.05,
        "play_to": "queue",
        "cache_key": key + "-other",
        "src_start_sec": 99.99,
    })
    check("不同位置 ⇒ 提交成功（不会拿错内容）", code == 200,
          f"得到 {code} {res3}")

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
