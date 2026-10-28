#!/usr/bin/env python3
# coding=utf-8
"""生成自带音色的试听样例（离线产物，随仓库走）。

选音色时想先听一段效果 —— 但一个音色合成一段要十几秒（NPU 上跑），放到
页面上现算等不得。所以样例**预先合成好**放进卡片 static 目录::

    app/apps/tts/static/previews/<voice_id>.wav

脚本走板子自己的 HTTP 接口合成（跟功能页同一条路），再把 SDK 写的
**32bit float** WAV 转成 **16bit PCM** —— 体积减半，且浏览器对 PCM 的
兼容性最稳（float WAV 有些播放器不收）。

用法::

    .venv/bin/python -u scripts/gen_voice_previews.py
    .venv/bin/python -u scripts/gen_voice_previews.py --base http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import json
import shutil
import struct
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PREVIEW_DIR = REPO / "app" / "apps" / "tts" / "static" / "previews"

#: 试听文本。短的、覆盖面广的 —— 试听要的是**音色**，不是内容。
SAMPLE_TEXT = "你好，欢迎使用语音生成，请试听当前音色的效果。"

#: 合成语言。样例统一中文 —— 语种不同音色表现也不同，中文最通用。
SAMPLE_LANGUAGE = "chinese"

#: 单个音色的合成超时（秒）。NPU 上十几秒算完，留一倍余量。
SYNTH_TIMEOUT = 120


def _http(base: str, path: str, payload: dict | None = None) -> dict:
    url = base + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _ref_voices(base: str) -> list[str]:
    """自带音色（kind == "ref"）的 id 列表。"""
    data = _http(base, "/api/tts/voices")
    voices = [v["id"] for v in data.get("voices", []) if v.get("kind") == "ref"]
    if not voices:
        raise RuntimeError("后端没给出自带音色（kind=ref），SDK 内嵌表是空的？")
    return voices


def _synth(base: str, voice: str, out_path: Path) -> None:
    """合成一段并写到 out_path（等真的落盘）。"""
    payload = {
        "text": SAMPLE_TEXT,
        "language": SAMPLE_LANGUAGE,
        "ref_speaker": voice,
        "play_to": "browser",     # 不往板载喇叭出声 —— 批量生成不该吵
        "output": str(out_path),
    }
    _http(base, "/api/tts/start", payload)

    deadline = time.time() + SYNTH_TIMEOUT
    while time.time() < deadline:
        snap = _http(base, "/api/tts/")
        state = snap.get("state")
        if state == "done":
            if snap.get("output") != str(out_path):
                raise RuntimeError(f"{voice}: 落盘路径不是预期的 {out_path}")
            return
        if state == "error":
            raise RuntimeError(f"{voice}: 合成失败 {snap.get('error')}")
        time.sleep(1.0)
    raise RuntimeError(f"{voice}: 合成超时（{SYNTH_TIMEOUT}s）")


def _to_pcm16(src: Path, dst: Path) -> float:
    """float32 WAV → pcm16 WAV。返回时长（秒）。"""
    d = src.read_bytes()
    if d[:4] != b"RIFF" or d[8:12] != b"WAVE":
        raise RuntimeError(f"{src}: 不是 WAV")
    pos, fmt, rate, ch, bits = 12, None, None, None, None
    data = b""
    while pos + 8 <= len(d):
        cid = d[pos:pos + 4]
        size = struct.unpack("<I", d[pos + 4:pos + 8])[0]
        body = d[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            # fmt tag / 声道数 / 采样率 / 字节率 / 块对齐 / 位深
            fmt, ch, rate, _, _, bits = struct.unpack("<HHIIHH", body[:16])
        elif cid == b"data":
            data = body
            break
        pos += 8 + size + (size & 1)
    if fmt is None or not data:
        raise RuntimeError(f"{src}: WAV 头/数据块不完整")

    if fmt == 3 and bits == 32:                       # IEEE float
        samples = struct.unpack("<%df" % (len(data) // 4), data)
        pcm = b"".join(struct.pack("<h", max(-32768, min(32767, int(s * 32767))))
                       for s in samples)
        bits, align = 16, ch * 2
    elif fmt == 1 and bits == 16:                     # 已经是 PCM16
        pcm, align = data, ch * 2
    else:
        raise RuntimeError(f"{src}: 不支持的 WAV 格式 fmt={fmt} bits={bits}")

    block = align
    byte_rate = rate * block
    out = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    out += b"fmt " + struct.pack("<IHHIIHH", 16, 1, ch, rate, byte_rate, block, bits)
    out += b"data" + struct.pack("<I", len(pcm)) + pcm
    dst.write_bytes(out)
    return len(pcm) / float(byte_rate)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="http://127.0.0.1:8000",
                    help="板子服务地址")
    ap.add_argument("--out", default=str(PREVIEW_DIR), help="样例输出目录")
    args = ap.parse_args()

    base = args.base.rstrip("/")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        voices = _ref_voices(base)
    except (urllib.error.URLError, RuntimeError) as exc:
        print(f"拿不到音色清单: {exc}", file=sys.stderr)
        print("（板子服务没起来？先起 wdnwebui）", file=sys.stderr)
        return 1

    print(f"自带音色 {len(voices)} 个: {', '.join(voices)}")
    tmp_dir = out_dir / ".tmp"
    tmp_dir.mkdir(exist_ok=True)
    try:
        for voice in voices:
            tmp = tmp_dir / (voice + ".wav")
            print(f"[{voice}] 合成中…", flush=True)
            _synth(base, voice, tmp)
            dur = _to_pcm16(tmp, out_dir / (voice + ".wav"))
            size = (out_dir / (voice + ".wav")).stat().st_size
            print(f"[{voice}] ok  {dur:.1f}s  {size // 1024} KB", flush=True)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    print(f"\n✅ {len(voices)} 个样例写入 {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
