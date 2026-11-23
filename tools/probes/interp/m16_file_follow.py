#!/usr/bin/env python3
"""M16 —— **「只播第一句」的时序诊断**（文件源 + 跟播放）。

用户实测：浏览器里跑音频文件源，**只听到第一句**，后面的译文就没了。

本探针把「浏览器」这一侧**模拟**出来（播放头上报 + 媒体播完后的冻结
状态），逐秒记录：

* 播放头（``ph``）与「媒体是否还在播」；
* 切句数 / 翻译数 / **送进队列数** / **待放行数**（``pending``）；
* TTS 队列的深度 / 已播数。

⇒ 分辨三种可能：

1. **切句就没了** ⇒ ASR 侧问题；
2. **切句有、翻译有、但 ``submitted`` 不涨** ⇒ **放行器压着不放**
   （本探针要抓的就是这个）；
3. **submitted 涨了、但队列里没播** ⇒ TTS 队列侧问题。

⚠️ **媒体播完后的状态**是本探针的关键：真实浏览器里
``<audio>`` 播完 ⇒ ``paused=true, ended=true, currentTime=停在时长`` ⇒
前端上报的 ``playing`` 变成 **false**（但播放头**不再往前走**）。

用法::

    .venv/bin/python tools/probes/interp/m16_file_follow.py [hold|end]
"""
from __future__ import annotations

import json
import ssl
import subprocess
import sys
import time
import urllib.request

BASE = "https://127.0.0.1:8000"
SSL_CTX = ssl.create_default_context()
WAV = "/userdata/assets/uploads/asr_en.wav"
#: 文件时长（秒）—— 探针要按它模拟「播完」。
DURATION = 15.05


def api(method: str, path: str, payload=None, timeout: int = 120):
    data = None
    hdrs = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=hdrs,
                                 method=method)
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        body = r.read().decode("utf-8")
    try:
        return json.loads(body or "{}")
    except Exception:  # noqa: BLE001
        return {"raw": body}


def report(ph: float, playing: bool, ended: bool = False) -> None:
    """模拟浏览器每 250 ms 上报一次播放头。"""
    api("POST", "/api/qwen3-asr/interp/playhead",
        {"stream_id": "file-1#0", "playhead_sec": ph, "playing": playing,
         "ended": ended})


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "end"
    print(f"=== 文件源 + 跟播放 · 媒体播完后（mode={mode}）===")
    api("POST", "/api/qwen3-asr/interp/stop")
    api("POST", "/api/tts/queue/clear")
    time.sleep(1.0)

    api("POST", "/api/qwen3-asr/interp/start",
        {"mode": "steady", "gate": "follow", "target_lang": "zh-CN",
         "volume": 0.5, "source": "file"})
    time.sleep(0.5)

    t0 = time.monotonic()
    # 文件识别（multipart）⇒ 另开一个线程跑，别挡住播放头模拟
    proc = subprocess.Popen([
        "curl", "-sk", "-X", "POST", BASE + "/api/qwen3-asr/transcribe",
        "-F", "path=" + WAV,
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print("--- 模拟媒体播放（0 → 15 s），之后按 mode 决定 ---")
    prev = None
    for i in range(90):
        t = time.monotonic() - t0
        if t < DURATION:
            # 媒体在播 ⇒ 播放头跟着走
            report(min(t, DURATION), True)
        elif mode == "end":
            # ⚠️ **媒体播完**：播放头**冻在时长**、playing=false、ended=true
            report(DURATION, False, ended=True)
        else:
            # 「一直暂停在末尾」（没有 ended 标记的老前端）
            report(DURATION, False)
        time.sleep(0.25)
        st = api("GET", "/api/qwen3-asr/interp/status")
        q = api("GET", "/api/tts/queue/status")
        row = {
            "t": round(t, 1),
            "ph": st.get("playhead_sec"),
            "stalled": st.get("playhead_stalled"),
            "seg": st.get("segments"), "tr": st.get("translated"),
            "sub": st.get("submitted"), "pend": st.get("pending"),
            "qd": q.get("depth"), "qdone": q.get("done"),
            "peak": q.get("last_peak"),
        }
        if row != prev:
            print("  ", json.dumps(row, ensure_ascii=False))
            prev = row
        if proc.poll() is not None and t > DURATION + 25:
            break
    api("POST", "/api/qwen3-asr/interp/stop")
    st = api("GET", "/api/qwen3-asr/interp/status")
    print("最终:", json.dumps({
        "segments": st.get("segments"), "translated": st.get("translated"),
        "submitted": st.get("submitted"), "pending": st.get("pending"),
        "stalled_released": st.get("stalled_released"),
        "playhead_moved": st.get("playhead_moved"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
