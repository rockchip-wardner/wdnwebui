"""共享切片迭代器：每 1 秒采样一帧，按有效帧累积成片。

VR（gscore 判据）与微信管线（``judge=None`` = 全部有效）共用同一套边界
规则，避免两份切片逻辑各自漂移：

* 每片最多 :data:`SLICE_MAX_FRAMES` 个有效帧；
* 片间重叠 :data:`SLICE_OVERLAP_FRAMES` 个有效帧；
* ``start = 首个有效帧时间``，``end = 最后一个有效帧时间 + 1``；
* 跳过的帧不参与 embedding，但仍落在片段 ``[start, end)`` 内
  （例如 0、1 取、2 跳过、3、4 取 → 片段 ``[0, 5)``，4 帧）。

``judge=None`` 时每帧都算有效帧，行为与旧的「固定 4 秒片 / 1 秒重叠」
逐片一致（4 帧一片，下一片从上一片最后一帧开始）。
"""
from __future__ import annotations

from typing import Callable, Iterator, List, Optional, Tuple

import numpy as np

#: 每片最多包含多少个「有效帧」（不是秒数 —— 跳帧时片段时长会变长）。
SLICE_MAX_FRAMES = 4
#: 相邻片段重叠多少个有效帧（不是秒数）。
SLICE_OVERLAP_FRAMES = 1


def iter_slices(
    source: str,
    fps: float,
    duration_s: float,
    *,
    judge=None,
    should_stop: Optional[Callable[[], bool]] = None,
    on_sample: Optional[Callable[[float, bool], None]] = None,
) -> Iterator[Tuple[float, float, List[np.ndarray]]]:
    """按 1 秒/帧采样，逐片 yield ``(start, end, frames)``。

    ``judge`` 需要提供 ``observe(thumb_2d_gray, frame_pos) -> bool`` 与
    ``long_side``；为 ``None`` 时所有帧都取（微信管线）。
    """
    from app.core.video.utils import _read_chunk_frames

    if fps <= 0 or duration_s <= 0:
        return

    block = SLICE_MAX_FRAMES
    pending: List[Tuple[float, np.ndarray]] = []
    pending_has_new = False
    block_start = 0.0
    # 第一个采样帧**无条件取**。自动判据（gscore）本来就把首帧当参照帧；
    # 而 ``FrameJudge`` 的 manual 语义是 ``pos > 0 and pos % n == 0`` ——
    # 直接照搬会让短视频（< every_n 秒）一帧都不取、0 个片段。切片至少要
    # 覆盖开头那一秒。
    taken_any = False

    while block_start < duration_s:
        if should_stop is not None and should_stop():
            return
        frames = _read_chunk_frames(source, block_start, block, fps)
        if not frames:
            break
        for j, frame in enumerate(frames):
            t = float(block_start + j)
            if t >= duration_s:
                break
            if should_stop is not None and should_stop():
                return
            if judge is None:
                take = True
            else:
                try:
                    take = bool(judge.observe(_judge_thumb(frame, judge.long_side), int(t)))
                except Exception:
                    # 判据异常按「取」处理（宁可多算一片，不漏检）
                    take = True
                if not taken_any:
                    take = True
            if on_sample is not None:
                try:
                    on_sample(t, take)
                except Exception:
                    pass
            if not take:
                continue
            taken_any = True
            pending.append((t, frame))
            pending_has_new = True
            while len(pending) >= SLICE_MAX_FRAMES:
                chunk = pending[:SLICE_MAX_FRAMES]
                yield _make_slice(chunk)
                pending = pending[-SLICE_OVERLAP_FRAMES:] if SLICE_OVERLAP_FRAMES else []
                pending_has_new = False
        block_start += block

    if pending and (pending_has_new or len(pending) > SLICE_OVERLAP_FRAMES):
        yield _make_slice(pending)


def _make_slice(chunk: List[Tuple[float, np.ndarray]]) -> Tuple[float, float, List[np.ndarray]]:
    start = float(chunk[0][0])
    end = float(chunk[-1][0]) + 1.0
    return start, end, [frame for _, frame in chunk]


def _judge_thumb(frame: np.ndarray, long_side: int) -> np.ndarray:
    """BGR 帧 → 长边 ``long_side`` 的 2D 灰度缩略图（供 FrameJudge.observe）。

    VR 的帧来自 PyAV（已是软解 BGR），与三卡「解码时 RGA 直接出 Y 平面」
    的路径不同；这里每采样帧只缩一次（1 秒/帧），用 cv2 软缩即可，不引入
    全局 RGA 锁竞争。
    """
    import cv2

    if frame is None:
        return np.zeros((long_side, long_side), dtype=np.uint8)
    h, w = frame.shape[:2]
    scale = float(long_side) / max(h, w)
    if scale < 1.0:
        frame = cv2.resize(
            frame,
            (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
            interpolation=cv2.INTER_AREA,
        )
    if frame.ndim == 3:
        return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return frame
