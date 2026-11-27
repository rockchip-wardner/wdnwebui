#!/usr/bin/env python3
"""回归：同传的 **API 契约**（5 个路由 + TTS 队列的 4 个路由）。

覆盖
====
* ``POST /api/qwen3-asr/interp/start``   —— 起同传（参数校验 + 返回字段）
* ``GET  /api/qwen3-asr/interp/status``  —— 指标字段齐全
* ``POST /api/qwen3-asr/interp/settings``—— 改设置（不重启引擎）
* ``POST /api/qwen3-asr/interp/playhead``—— 播放头上报
* ``GET  /api/qwen3-asr/interp/languages``—— **只列能出声的目标语**
* ``POST /api/qwen3-asr/interp/stop``    —— 停同传
* ``GET/POST /api/tts/queue/*``          —— 队列的 4 个路由

跑之前要满足：**服务已重启**（加载了同传的新代码）。

跑法::

    .venv/bin/python scripts/verify_interp_api.py
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

BASE = "https://127.0.0.1:8000"

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


def api(method: str, path: str, payload=None, timeout: int = 120):
    url = BASE + path
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            body = r.read().decode("utf-8") or "{}"
            try:
                return r.status, json.loads(body)
            except Exception:  # noqa: BLE001
                return r.status, {"raw": body}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body or "{}")
        except Exception:  # noqa: BLE001
            return e.code, {"raw": body}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": str(e)}


def main() -> int:
    print("== 同传 API ==")

    # ---- 语言清单（只列能出声的） -------------------------------- #
    st, data = api("GET", "/api/qwen3-asr/interp/languages")
    check("GET /interp/languages ⇒ 200", st == 200, f"得到 {st}")
    items = data.get("items") or []
    check("语言清单非空", len(items) > 0, f"得到 {len(items)}")
    check("每项都有 id/name/tts",
          all(it.get("id") and it.get("name") and it.get("tts") for it in items),
          f"样例 {items[:2]}")
    check("清单里没有 zh-TW（模型分不出简繁）",
          not any(it.get("id") == "zh-TW" for it in items))
    check("清单里都是 TTS 能出声的语种",
          all(it.get("tts") in ("chinese", "english", "german", "italian",
                                "portuguese", "spanish", "japanese",
                                "korean", "french", "russian")
              for it in items),
          f"样例 {items[:2]}")

    # ---- start ---------------------------------------------------- #
    st, data = api("POST", "/api/qwen3-asr/interp/start",
                   {"mode": "fast", "gate": "now", "target_lang": "zh-CN",
                    "speaker": "news_female", "volume": 0.1,
                    "source": "live"})
    check("POST /interp/start ⇒ 200", st == 200, f"得到 {st} {data}")
    check("start 返回 ok", data.get("ok") is True, f"得到 {data}")
    check("start 返回 running", data.get("running") is True, f"得到 {data}")
    check("start 记住了档位", data.get("mode") == "fast", f"得到 {data}")

    # ---- status --------------------------------------------------- #
    st, data = api("GET", "/api/qwen3-asr/interp/status")
    check("GET /interp/status ⇒ 200", st == 200, f"得到 {st}")
    for field in ("mode", "gate", "source", "segments", "translated",
                  "submitted", "played", "lag_sec", "queue_depth", "tempo",
                  "tts", "skipped_seek", "replayed", "last_seek"):
        check(f"status 里有 {field}", field in data, f"键 = {sorted(data)}")

    # ---- settings ------------------------------------------------- #
    st, data = api("POST", "/api/qwen3-asr/interp/settings",
                   {"mode": "steady", "gate": "follow",
                    "target_lang": "en", "speaker": "male_announcer",
                    "volume": 0.2})
    check("POST /interp/settings ⇒ 200", st == 200, f"得到 {st}")
    check("settings 改档位生效", data.get("mode") == "steady", f"得到 {data}")
    check("settings 改播放时机生效", data.get("gate") == "follow",
          f"得到 {data}")
    check("settings 改目标语生效", data.get("target_lang") == "en",
          f"得到 {data}")
    check("settings 改音色生效", data.get("speaker") == "male_announcer",
          f"得到 {data}")

    # ---- **start 不带音色字段 ⇒ 不能把已选的音色抹掉** -------------
    #
    # ⚠️ **踩过的坑**（2026-11-21）：``interp/start`` 之前写死
    # ``ref_speaker=data.ref_speaker or ""`` ⇒ **start 请求没带音色字段**
    # 时把用户刚在面板上选好的音色**抹成空** ⇒ 之后每句都没有音色条件 ⇒
    # **每句话音色都不一样**（worker 日志里没有 ``[speaker] using
    # voice-clone`` 那行）。
    #
    # ⇒ start 的语义是「按面板当前状态起」，**不是**「重置成默认」。
    st, data = api("POST", "/api/qwen3-asr/interp/start",
                   {"mode": "fast", "gate": "now"})
    check("start（不带音色）⇒ 200", st == 200, f"得到 {st} {data}")
    check("**start 不带音色 ⇒ 保留已选音色**（不被抹掉）",
          data.get("speaker") == "male_announcer", f"得到 {data}")
    st, data = api("POST", "/api/qwen3-asr/interp/settings",
                   {"ref_speaker": "news_female"})
    check("settings 设参考音色生效", data.get("ref_speaker") == "news_female",
          f"得到 {data}")
    st, data = api("POST", "/api/qwen3-asr/interp/start",
                   {"mode": "fast", "gate": "now"})
    check("**start 不带音色 ⇒ 保留已选参考音色**",
          data.get("ref_speaker") == "news_female", f"得到 {data}")

    # ---- playhead -------------------------------------------------- #
    st, data = api("POST", "/api/qwen3-asr/interp/playhead",
                   {"stream_id": "file-1#0", "playhead_sec": 12.5,
                    "playing": True})
    check("POST /interp/playhead ⇒ 200", st == 200, f"得到 {st}")
    check("playhead 返回 ok", data.get("ok") is True, f"得到 {data}")
    st, data = api("GET", "/api/qwen3-asr/interp/status")
    check("status 里有播放头", "playhead_sec" in data, f"键 = {sorted(data)}")

    # ---- TTS 队列的 4 个路由 --------------------------------------- #
    st, data = api("GET", "/api/tts/queue/status")
    check("GET /tts/queue/status ⇒ 200", st == 200, f"得到 {st}")
    for field in ("depth", "queued_audio_sec", "playing", "last_lag_sec",
                  "avg_lag_sec", "merged", "done", "tempo"):
        check(f"队列 status 里有 {field}", field in data, f"键 = {sorted(data)}")

    st, data = api("POST", "/api/tts/queue/submit",
                   {"text": "今天天气很好", "language": "chinese",
                    "volume": 0.02, "tempo": 1.0})
    check("POST /tts/queue/submit ⇒ 200", st == 200, f"得到 {st} {data}")
    check("submit 返回 seq", "seq" in data, f"得到 {data}")

    st, data = api("POST", "/api/tts/queue/submit",
                   {"text": "สวัสดี", "language": "thai"})
    check("不可出声的语种 ⇒ 400", st == 400, f"得到 {st} {data}")

    st, data = api("POST", "/api/tts/queue/submit", {"text": "  "})
    check("空文本 ⇒ 400", st == 400, f"得到 {st}")

    st, data = api("POST", "/api/tts/queue/tempo", {"tempo": 1.08})
    check("POST /tts/queue/tempo ⇒ 200", st == 200, f"得到 {st}")
    check("tempo 生效", abs(float(data.get("tempo", 0)) - 1.08) < 1e-6,
          f"得到 {data}")
    # ⚠️⚠️ **必须把变速恢复回去**（2026-11-26 实测踩到的**跨脚本污染**）——
    #
    # 变速是**队列级全局状态** ⇒ 本脚本把它设成 1.08 之后**不恢复** ⇒
    # 后面的脚本全部继承这个值。
    #
    # ⚠️ **实测它怎么咬人的**：本脚本把队列设成 1.08 ⇒ 之后某次「非
    # 预合成」期间，引擎按产能把它压到 **0.9** ⇒ 再切到预合成时，引擎
    # 的死区判定用的是**自己的计数器**（已是 1.0）⇒ 认为「不用升」⇒
    # **队列的 0.9 永远纠正不回来** ⇒ 播放实际仍慢 10% ⇒
    # 用户报「变速怎么还是 0.90 / TTS 比字幕慢」。
    #
    # ⇒ 任何**改动全局状态**的脚本都**必须恢复**（放 `finally` ⇒ 中途
    # 抛异常也不会把脏状态留给后面的脚本）。
    try:
        st, data = api("POST", "/api/tts/queue/tempo", {"tempo": 1.0})
        check("把变速恢复回 1.0", st == 200
              and abs(float(data.get("tempo", 0)) - 1.0) < 1e-6,
              f"得到 {st} {data}")
    except Exception as exc:  # noqa: BLE001
        check("把变速恢复回 1.0", False, f"恢复失败: {exc}")

    st, data = api("POST", "/api/tts/queue/clear")
    check("POST /tts/queue/clear ⇒ 200", st == 200, f"得到 {st}")
    check("clear 返回计数", "cleared" in data, f"得到 {data}")

    # ---- stop ------------------------------------------------------ #
    st, data = api("POST", "/api/qwen3-asr/interp/stop")
    check("POST /interp/stop ⇒ 200", st == 200, f"得到 {st}")
    check("stop 后 running = false", data.get("running") is False,
          f"得到 {data}")

    print(f"\n通过 {_PASS} / 失败 {_FAIL}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
