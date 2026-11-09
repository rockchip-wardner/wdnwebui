#!/usr/bin/env python3
"""板载麦克风 → 实时 ASR 的**独立采集进程**。

为什么是独立进程
================

1. **ALSA 读是阻塞 I/O** —— ``snd_pcm_readi`` 会一直等到攒满一个 period。
   塞进 webui 进程会占死线程，整个 Web 服务卡住。
2. **设备独占** —— ALSA 默认独占声卡。webui 还要提供其它服务，不该被
   声卡绑住。
3. **可独立重启** —— 采集崩了重启采集进程即可，不用重启 webui。

它与**浏览器采集**走**同一套 HTTP 接口**（``/live/start`` →
``/live/chunk`` → ``/live/finish``），服务端不关心音频从哪来。

用法
====

::

    # 列出本机的采集设备
    .venv/bin/python tools/asr_mic_agent.py --list-devices

    # 板载麦克风实时识别（默认 hw:1,0 = ES8323 板载麦）
    sudo .venv/bin/python tools/asr_mic_agent.py --device hw:1,0

    # 从文件喂（不接麦克风）—— 用来验证「板载路径」这条链路
    .venv/bin/python tools/asr_mic_agent.py --file /userdata/asr_en.wav

    # 跑 10 秒就停
    .venv/bin/python tools/asr_mic_agent.py --seconds 10

权限
====

``/dev/snd/pcmC1D0c`` 是 ``root:audio`` ``rw-rw----`` → 采集进程必须在
**audio 组**里（``sudo -u rockchip -g audio``），或以 root 跑。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

# 允许「从仓库根目录直接跑」：``python tools/asr_mic_agent.py``
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.core.audio.alsa import (AlsaCapture, AlsaError,  # noqa: E402
                                 current_user_in_audio_group,
                                 default_capture_device, list_capture_devices)
from app.core.audio.capture import StreamResampler  # noqa: E402

LOG = logging.getLogger("asr_mic_agent")

DEFAULT_URL = "http://127.0.0.1:8000"
DEFAULT_DEVICE = "hw:1,0"
DEFAULT_CHUNK_MS = 500


# --------------------------------------------------------------------------- #
# HTTP —— 只用标准库 urllib（本仓不为此装 pip 包）
# --------------------------------------------------------------------------- #
def _request(url: str, *, method: str = "GET", body: Optional[bytes] = None,
             headers: Optional[dict] = None, timeout: float = 30.0) -> dict:
    """发一个 HTTP 请求；返回解析后的 JSON。"""
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            detail = json.loads(raw.decode("utf-8")).get("detail") or raw
        except Exception:  # noqa: BLE001
            detail = raw
        raise RuntimeError("HTTP {} {} → {}".format(e.code, url, detail)) from e
    except urllib.error.URLError as e:
        raise RuntimeError("连不上 {}: {}".format(url, e.reason)) from e
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {"raw": raw[:200].decode("utf-8", "replace")}


def _api(base: str, path: str) -> str:
    return base.rstrip("/") + "/api/qwen3-asr" + path


# --------------------------------------------------------------------------- #
def _iter_wav_chunks(path: str, chunk_ms: int):
    """把 WAV 文件按 chunk_ms 切成裸 s16le 块（16 kHz 单声道）。

    ⚠️ **文件采样率可以不是 16 kHz** —— 会用
    ``app.core.audio.capture.StreamResampler``（**带 carry** 的流式线性
    插值）降到 16 kHz，算法与 ALSA 实时路径、与浏览器前端**完全一致**
    （清单 B3「跨端一致」的前提）。

    为什么不用一次性整段重采样：流式路径必须与整段路径**逐样本相同**，
    否则「同一段录音」的识别结果可能在边缘分叉。
    """
    from app.core.audio.wav import parse_wav

    wav = parse_wav(Path(path).read_bytes())
    if wav.n_channels != 1 or wav.sample_width != 2:
        raise RuntimeError(
            "文件必须是单声道 s16le（当前 {} 声道 / {} 字节每采样）".format(
                wav.n_channels, wav.sample_width))
    need = int(16000 * chunk_ms / 1000) * 2
    if wav.sample_rate == 16000:
        data = wav.pcm
        for i in range(0, len(data), need):
            yield data[i:i + need]
        return

    resampler = StreamResampler(wav.sample_rate, 16000)
    buf = bytearray()
    src_chunk = int(wav.sample_rate * chunk_ms / 1000) * 2
    for i in range(0, len(wav.pcm), src_chunk):
        out = resampler.push(wav.pcm[i:i + src_chunk])
        if not out:
            continue
        buf += out
        while len(buf) >= need:
            yield bytes(buf[:need])
            del buf[:need]
    tail = resampler.flush()
    if tail:
        buf += tail
    if buf:
        yield bytes(buf)


def _iter_pcm_chunks(path: str, chunk_ms: int):
    """把**裸** s16le PCM（16 kHz 单声道）按 chunk_ms 切块。

    用来喂「已经采好的 PCM」—— 比如浏览器路径实际上传的字节，
    以此验证「同一段 PCM 从两条链路进来 → 文本逐字节相同」。
    """
    need = int(16000 * chunk_ms / 1000) * 2
    data = Path(path).read_bytes()
    for i in range(0, len(data), need):
        yield data[i:i + need]


def _iter_alsa_chunks(capture: AlsaCapture, chunk_ms: int):
    """从 ALSA 设备产出 16 kHz 定长块。"""
    for chunk in capture.chunks_16k(chunk_ms=chunk_ms):
        yield chunk


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="板载麦克风 → 实时 ASR 采集进程")
    ap.add_argument("--device", default=DEFAULT_DEVICE,
                    help="ALSA 采集设备（默认 {}）".format(DEFAULT_DEVICE))
    ap.add_argument("--url", default=DEFAULT_URL,
                    help="webui 基址（默认 {}）".format(DEFAULT_URL))
    ap.add_argument("--chunk-ms", type=int, default=DEFAULT_CHUNK_MS,
                    help="攒块粒度（毫秒，默认 {}）".format(DEFAULT_CHUNK_MS))
    ap.add_argument("--period-ms", type=int, default=100,
                    help="ALSA 读一次的时长（毫秒，默认 100）")
    ap.add_argument("--rate", type=int, default=48000,
                    help="请求的 ALSA 采样率（默认 48000）")
    ap.add_argument("--seconds", type=float, default=0.0,
                    help="跑多少秒就停（0 = 一直跑）")
    ap.add_argument("--file", default="",
                    help="不从麦克风读，改喂这个 WAV 文件"
                         "（单声道 s16le，任意采样率 —— 非 16 kHz 会用"
                         "线性插值降到 16 kHz）")
    ap.add_argument("--pcm", default="",
                    help="不从麦克风读，改喂这个**裸** s16le PCM 文件"
                         "（16 kHz 单声道，无 WAV 头）")
    ap.add_argument("--list-devices", action="store_true",
                    help="列出本机采集设备后退出")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.list_devices:
        print("ALSA 采集设备：")
        for d in list_capture_devices():
            print("  {:10s} {}".format(d.name, d.description))
        print("当前进程在 audio 组：{}".format(current_user_in_audio_group()))
        return 0

    # ---- 打开设备 ----
    capture = None
    if args.pcm:
        LOG.info("从裸 PCM 喂：%s", args.pcm)
        source = _iter_pcm_chunks(args.pcm, args.chunk_ms)
    elif args.file:
        LOG.info("从文件喂：%s", args.file)
        source = _iter_wav_chunks(args.file, args.chunk_ms)
    else:
        device = args.device
        if device == "auto":
            device = default_capture_device()
        LOG.info("打开 ALSA 设备 %s", device)
        try:
            capture = AlsaCapture(device, sample_rate=args.rate,
                                  period_ms=args.period_ms)
            capture.open()
        except AlsaError as e:
            LOG.error("打开设备失败：%s", e)
            if not current_user_in_audio_group():
                LOG.error("当前用户不在 audio 组 —— 需要 "
                          "`sudo -u <user> -g audio` 或以 root 跑")
            return 2
        source = _iter_alsa_chunks(capture, args.chunk_ms)

    # ---- 开会话 ----
    try:
        start = _request(
            _api(args.url, "/live/start"), method="POST",
            body=json.dumps({
                "source": "board",
                "sample_rate": 16000,
                "n_channels": 1,
                "sample_width": 2,
                "chunk_ms": args.chunk_ms,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"})
    except RuntimeError as e:
        LOG.error("开实时会话失败：%s", e)
        if capture is not None:
            capture.close()
        return 3
    stream_id = start.get("stream_id") or ""
    if not stream_id:
        LOG.error("后端没给 stream_id：%r", start)
        if capture is not None:
            capture.close()
        return 3
    LOG.info("实时会话已开：%s（端点参数 %s）", stream_id, start.get("endpoint"))

    stopping = {"flag": False}

    def _on_sig(signum, _frame):
        stopping["flag"] = True
        LOG.info("收到信号 %s，准备收尾", signum)

    signal.signal(signal.SIGINT, _on_sig)
    signal.signal(signal.SIGTERM, _on_sig)

    # ---- 推流 ----
    seq = 0
    t0 = time.monotonic()
    sent = 0
    dropped = 0
    try:
        for chunk in source:
            if stopping["flag"]:
                break
            if args.seconds and (time.monotonic() - t0) >= args.seconds:
                break
            try:
                resp = _request(
                    _api(args.url, "/live/chunk"), method="POST", body=chunk,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "X-Asr-Stream-Id": stream_id,
                        "X-Asr-Seq": str(seq),
                    })
            except RuntimeError as e:
                LOG.error("上行失败（第 %s 块）：%s", seq, e)
                break
            seq += 1
            sent += 1
            d = int(resp.get("dropped_chunks") or 0)
            if d > dropped:
                dropped = d
                LOG.warning("服务端报告丢了 %s 块", d)
            if sent % 20 == 0:
                LOG.info("已送 %s 块（%.1f s），lag=%s ms",
                         sent, time.monotonic() - t0, resp.get("lag_ms"))
    except KeyboardInterrupt:
        LOG.info("被 Ctrl-C 打断")
    finally:
        if capture is not None:
            capture.close()

    # ---- 收尾 ----
    # ⚠️ 超时给 15 s：收尾会触发**最后一次推理**（final=True），实测
    # 0.5~3 s。给太短会「收尾失败」→ 末块没喂给模型 → 尾句丢。
    # 给太长则 webui 的 stop 端点要干等 —— webui 侧的宽限期是 8 s，
    # 超了就 SIGKILL（所以这里宁短勿长：宁可让 webui 走「安全网」收
    # 掉会话，也不要把自己吊在 HTTP 上）。
    t_fin = time.monotonic()
    try:
        final = _request(
            _api(args.url, "/live/finish"), method="POST",
            headers={"X-Asr-Stream-Id": stream_id}, timeout=15.0)
        LOG.info("最终文本：%s", (final.get("text") or "").strip())
        print(final.get("text") or "")
    except RuntimeError as e:
        LOG.error("收尾失败：%s", e)
    LOG.info("共送 %s 块，服务端丢块 %s，收尾耗时 %.2fs",
             sent, dropped, time.monotonic() - t_fin)
    return 0


if __name__ == "__main__":
    sys.exit(main())
