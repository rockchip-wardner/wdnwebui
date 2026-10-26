#!/usr/bin/env python3
"""回归防护：VR 的 gscore 动态切片（``app/core/video/slicing.py``）。

钉住三条不变量：

1. **片段边界 = 有效帧序列**：``start`` = 首个有效帧时间、
   ``end`` = 最后一个有效帧时间 + 1；跳过的秒仍落在片段区间内。
2. **重叠按有效帧数**（不是秒数）：下一片从上一片最后
   ``SLICE_OVERLAP_FRAMES`` 个有效帧开始。
3. **全跳过只留首帧片段**；``judge=None`` 时与旧固定 4s/1s 的
   ``compute_slices`` 段数严格一致（微信管线的兼容前提）。

不占 NPU、不依赖真实视频文件（桩掉 ``_read_chunk_frames``）。

用法::

    python -m scripts.verify_vr_gscore_slicing
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, ".")

import numpy as np

from app.core.video import slicing
from app.core.video.frame_judge import FrameJudge
from app.core.video.utils import compute_slices

fails: list[str] = []
notes: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        fails.append(name)


def _install_reader(duration_s: float, fps: float, frame_fn) -> object:
    import app.core.video.utils as U

    orig = U._read_chunk_frames

    def _read(_source, start, chunk_seconds, fps_):
        out = []
        for j in range(int(chunk_seconds)):
            t = float(start + j)
            if int(round(t * fps_)) < int(duration_s * fps_):
                out.append(frame_fn(int(t)))
        return out

    U._read_chunk_frames = _read
    return orig


def _restore(orig) -> None:
    import app.core.video.utils as U

    U._read_chunk_frames = orig


class StubJudge:
    long_side = 160

    def __init__(self, takes: dict[int, bool]):
        self.takes = takes

    def observe(self, _thumb, pos):
        return bool(self.takes.get(int(pos), False))


def collect(duration_s, judge, frame_fn=None, fps=25, should_stop=None,
            on_sample=None):
    frame_fn = frame_fn or (lambda _t: np.zeros((8, 8, 3), np.uint8))
    orig = _install_reader(duration_s, fps, frame_fn)
    try:
        return list(slicing.iter_slices(
            "stub.mp4", fps, duration_s,
            judge=judge, should_stop=should_stop, on_sample=on_sample))
    finally:
        _restore(orig)


def main() -> int:
    check("常量：每片 4 个有效帧", slicing.SLICE_MAX_FRAMES == 4,
          f"SLICE_MAX_FRAMES={slicing.SLICE_MAX_FRAMES}")
    check("常量：重叠 1 个有效帧", slicing.SLICE_OVERLAP_FRAMES == 1,
          f"SLICE_OVERLAP_FRAMES={slicing.SLICE_OVERLAP_FRAMES}")

    # [1] 全取（judge=None）10s → [0,4) [3,7) [6,10)
    slices = collect(10.0, None)
    got = [(round(a, 2), round(b, 2), len(fs)) for a, b, fs in slices]
    check("[1] 全取 10s 的片段序列",
          got == [(0.0, 4.0, 4), (3.0, 7.0, 4), (6.0, 10.0, 4)], str(got))
    check("[1b] 与 compute_slices 段数一致",
          len(slices) == compute_slices(25, 10.0, 4, 1),
          f"iter={len(slices)} compute={compute_slices(25, 10.0, 4, 1)}")

    # [2] 跳过的秒不丢时间：0,1 取；2 跳；3,4 取 → [0,5) 4 帧
    takes = {0: True, 1: True, 2: False, 3: True, 4: True,
             5: False, 6: True, 7: True, 8: True, 9: True}
    slices = collect(10.0, StubJudge(takes))
    got = [(round(a, 2), round(b, 2), len(fs)) for a, b, fs in slices]
    check("[2] 跳帧后片段覆盖被跳过的秒",
          got[0] == (0.0, 5.0, 4), str(got[0]))
    check("[2b] 第二片从上一片最后一个有效帧开始",
          got[1][0] == 4.0 and got[1][2] == 4, str(got[1]))
    check("[2c] 末片收尾（重叠帧无新帧时不单独成片）",
          got[-1] == (8.0, 10.0, 2), str(got[-1]))

    # [3] 一直跳过（只首帧有效）→ 1 片 [0,1)
    slices = collect(6.0, StubJudge({0: True}))
    got = [(round(a, 2), round(b, 2), len(fs)) for a, b, fs in slices]
    check("[3] 全跳过只剩首帧片段", got == [(0.0, 1.0, 1)], str(got))

    # [4] should_stop → 不产片段
    stop = {"v": False}

    def _should_stop():
        stop["v"] = True
        return True

    slices = collect(10.0, None, should_stop=_should_stop)
    check("[4] should_stop 立刻中止", slices == [], str(slices))

    # [5] on_sample 统计：评估数 == 总秒数，取/跳之和一致
    st = {"eval": 0, "take": 0, "skip": 0}

    def _on_sample(_t, take):
        st["eval"] += 1
        st["take" if take else "skip"] += 1

    collect(10.0, StubJudge({0: True, 1: True, 2: False, 3: True}),
            on_sample=_on_sample)
    check("[5] on_sample 统计一致",
          st == {"eval": 10, "take": 3, "skip": 7}, str(st))

    # [6] 真实 gscore：静止画面只取首帧
    static = lambda _t: np.full((64, 64, 3), 120, np.uint8)  # noqa: E731
    js = FrameJudge(mode="gscore", every_n=10, threshold=10.0, long_side=160)
    slices = collect(8.0, js, frame_fn=static)
    check("[6] gscore 静止画面只产 1 片",
          len(slices) == 1 and slices[0][0] == 0.0, str([
              (a, b, len(fs)) for a, b, fs in slices]))

    # [7] 真实 gscore：每帧都不同 → 门全开（8s → 3 片）
    rng = np.random.default_rng(7)
    noisy = lambda _t: rng.integers(0, 256, (64, 64, 3), dtype=np.uint8)  # noqa: E731
    jn = FrameJudge(mode="gscore", every_n=10, threshold=10.0, long_side=160)
    slices = collect(8.0, jn, frame_fn=noisy)
    check("[7] gscore 画面持续变化 → 全取（3 片）",
          len(slices) == 3, str([(a, b, len(fs)) for a, b, fs in slices]))

    print()
    print(f"{len(fails)} 项失败 / {len(notes)} 条备注")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
