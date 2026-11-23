#!/usr/bin/env python3
"""M7 — sox ``tempo`` + 板载 codec 出声（M7 结论固化版）。

⚠️ 本板 sox 14.4.2 的三个坑（踩过）：

1. ``-D`` **不是**选设备，而是 ``--no-dither`` ⇒ ``play`` 只能用**默认**
   ALSA 设备（板上是 card 0 = hdmiin，只有采集）⇒ 变速路径必须
   **sox 负责变速、aplay 负责出声**（两段管道）。
2. sox 的**效果参数放在输出文件之后**：``sox in.wav ... - tempo 1.1``。
3. sox 读 stdin 写 stdout 时，**输入和输出各要一份格式参数**。

sox 的原始 PCM 格式参数是 ``-e floating-point -b 32 -L``（不是 aplay 的
``-f FLOAT_LE``）。

实测结论（2026-11-21）：
* tempo 精确等于 1/倍率（偏差 < 0.01%），**音高不变**（FFT 峰值 440 Hz）；
* ``sox → aplay`` 管道能真正打开板载 codec（/proc/asound/card1/pcm0p
  显示 MMAP_INTERLEAVED / S24_LE / 48 kHz / 2ch）。
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

WORK = Path("/userdata/assets/uploads/.m7")
DEVICE = "plughw:1,0"
SR = 24000
CH = 1
FREQ = 440.0

SOX_FMT = ["-t", "raw", "-e", "floating-point", "-b", "32", "-L",
           "-r", str(SR), "-c", str(CH)]
APLAY_FMT = ["-t", "raw", "-f", "FLOAT_LE", "-r", str(SR), "-c", str(CH)]


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, **kw)


def sox_tempo(path: Path, tempo: float | None) -> bytes:
    """渲染 wav（可选经过 tempo）为 raw float32。"""
    cmd = ["sox", str(path)] + SOX_FMT + ["-"]
    if tempo:
        cmd += ["tempo", "%.3f" % tempo]
    p = run(cmd)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace"))
    return p.stdout


def peak_freq(pcm: bytes) -> float:
    import numpy as np

    x = np.frombuffer(pcm, dtype=np.float32)
    if x.size < 1024:
        return 0.0
    x = x - x.mean()
    spec = np.abs(np.fft.rfft(x * np.hanning(x.size)))
    freqs = np.fft.rfftfreq(x.size, 1.0 / SR)
    return float(freqs[int(spec.argmax())])


def codec_params() -> str:
    p = Path("/proc/asound/card1/pcm0p/sub0/hw_params")
    try:
        return p.read_text()
    except OSError:
        return ""


def play_via_aplay(raw: bytes) -> tuple[int, str]:
    """基准路径：直接 aplay（tempo=1.0 走的正是这条路）。"""
    proc = subprocess.Popen(["aplay"] + APLAY_FMT + ["-D", DEVICE, "-"],
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)
    try:
        proc.stdin.write(raw)
        proc.stdin.flush()
        time.sleep(0.5)
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    err = proc.stderr.read().decode("utf-8", "replace").strip()
    try:
        rc = proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = proc.wait()
    return rc, err


def play_with_tempo(raw: bytes, tempo: float) -> tuple[int, str, str]:
    """变速路径：sox(变速) → aplay(出声)。"""
    sox_cmd = ["sox"] + SOX_FMT + ["-"] + SOX_FMT + ["-", "tempo", "%.3f" % tempo]
    sox = subprocess.Popen(sox_cmd, stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    aplay = subprocess.Popen(["aplay"] + APLAY_FMT + ["-D", DEVICE, "-"],
                             stdin=sox.stdout, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE)
    sox.stdout.close()
    params = ""
    try:
        sox.stdin.write(raw)
        sox.stdin.flush()
        time.sleep(0.8)
        params = codec_params()
        sox.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    a_err = aplay.stderr.read().decode("utf-8", "replace").strip()
    s_err = sox.stderr.read().decode("utf-8", "replace").strip()
    try:
        rc = aplay.wait(timeout=30)
    except subprocess.TimeoutExpired:
        aplay.kill()
        rc = aplay.wait()
    sox.wait(timeout=10)
    return rc, (a_err + " | " + s_err).strip(), params


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    wav = WORK / "sine3.wav"
    p = run(["sox", "-n", "-r", str(SR), "-c", str(CH), str(wav),
             "synth", "3", "sine", str(FREQ)])
    if p.returncode != 0:
        print("sox synth failed:", p.stderr.decode("utf-8", "replace"))
        return 1

    base = sox_tempo(wav, None)
    base_sec = len(base) / (4 * SR * CH)
    print(f"[1] 素材 3.000 s 正弦 {FREQ:.0f} Hz（{len(base)} bytes float32）")

    print("[2] tempo 离线效果（时长 / 音高）")
    table = {}
    for tempo in (1.0, 1.04, 1.08, 1.15):
        out = sox_tempo(wav, tempo)
        sec = len(out) / (4 * SR * CH)
        table[tempo] = sec
        print(f"    tempo={tempo:.2f} -> {sec:.3f} s "
              f"(缩短 {100 * (1 - sec / base_sec):5.1f}%)  峰值 {peak_freq(out):.1f} Hz")
        if tempo > 1.0:
            expect = base_sec / tempo
            err = abs(sec - expect) / expect
            assert err < 0.02, f"tempo={tempo} 时长偏差 {100 * err:.1f}% 过大"
            assert abs(peak_freq(out) - FREQ) < 5, "tempo 改了音高！"

    print(f"[3] 板载 codec 实播（{DEVICE}）")
    run(["amixer", "-c", "1", "sset", "Speaker", "on"])

    rc, err = play_via_aplay(base)
    print(f"    aplay(tempo=1.0)      rc={rc}  {err[:90]}")
    assert rc == 0, "aplay 基准路径失败"

    rc, err, params = play_with_tempo(base, 1.10)
    print(f"    sox(tempo 1.10)→aplay rc={rc}  {err[:120]}")
    print("    播放期间 /proc/asound/card1/pcm0p:")
    for line in params.splitlines()[:6]:
        print("      ", line)
    assert rc == 0, "变速管道失败"
    assert params.strip(), "读不到 hw_params ⇒ 不能证明出声"
    m = re.search(r"rate:\s*(\d+)", params)
    assert m, "hw_params 里没有 rate ⇒ codec 未真正打开"

    print("[4] 结论")
    print("    ✓ tempo 精确 1/倍率，音高不变")
    print("    ✓ sox→aplay 管道能真正打开板载 ES8388 codec")
    print("    ⇒ 同传变速路径可行；tempo=1.0 仍走 aplay（零风险默认）")
    print(f"    实测表: { {k: round(v, 3) for k, v in table.items()} }")
    return 0


if __name__ == "__main__":
    sys.exit(main())
