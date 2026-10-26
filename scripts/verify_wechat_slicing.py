#!/usr/bin/env python3
"""回归防护：微信视频管线与 VR 共用切片口径（但暂不接判据）。

钉三件事：

1. ``video_manager._CHUNK_SECONDS`` / ``_CHUNK_OVERLAP_SECONDS`` 直接引用
   ``slicing.SLICE_MAX_FRAMES`` / ``SLICE_OVERLAP_FRAMES``（不再各自写死
   4 / 1）。
2. 微信侧用 ``iter_slices(judge=None)``（全取），**不接 gscore** —— 微信
   用户只发一个视频，没有"画面上有什么"的持续语义，跳帧只会丢内容。
3. ``judge=None`` 时 ``iter_slices`` 产出的段数与 ``compute_slices`` 预估
   **严格一致**（部分成功判定依赖它，差 1 段就会把"全成功"误报成"部分
   失败"）。

用法::

    python -m scripts.verify_wechat_slicing
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, ".")

import numpy as np

from app.core.video import slicing
from app.core.video.utils import compute_slices

REPO = Path(__file__).resolve().parent.parent
fails: list[str] = []
notes: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        fails.append(name)


def _install_reader(duration_s: float, fps: float):
    import app.core.video.utils as U

    orig = U._read_chunk_frames

    def _read(_source, start, chunk_seconds, fps_):
        out = []
        for j in range(int(chunk_seconds)):
            t = float(start + j)
            if int(round(t * fps_)) < int(duration_s * fps_):
                out.append(np.zeros((8, 8, 3), np.uint8))
        return out

    U._read_chunk_frames = _read
    return orig


def _count_slices(duration_s: float, fps: float = 25) -> int:
    import app.core.video.utils as U

    orig = _install_reader(duration_s, fps)
    try:
        return sum(1 for _ in slicing.iter_slices(
            "stub.mp4", fps, duration_s, judge=None))
    finally:
        U._read_chunk_frames = orig


def main() -> int:
    src = (REPO / "app/system/im_bridge/video_manager.py").read_text(
        encoding="utf-8")
    check("常量直接引用 slicing",
          "_CHUNK_SECONDS = SLICE_MAX_FRAMES" in src
          and "_CHUNK_OVERLAP_SECONDS = SLICE_OVERLAP_FRAMES" in src)
    check("微信不接判据（iter_slices judge=None）",
          "judge=None" in src)
    check("缓存标记 fixed",
          '"slice_version": "fixed"' in src)

    for dur in (4.0, 4.5, 10.0, 19.0, 30.0, 0.5):
        got = _count_slices(dur)
        want = compute_slices(25, dur, slicing.SLICE_MAX_FRAMES,
                              slicing.SLICE_OVERLAP_FRAMES)
        check(f"段数一致 duration={dur}s", got == want,
              f"iter={got} compute={want}")

    print()
    print(f"{len(fails)} 项失败 / {len(notes)} 条备注")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
