"""``CropRegion`` —— 原图缩放工具。

模型给出 0–1000 归一化 bbox，``__call__`` 在 worker 线程里按
bbox 切出 BGR ndarray，并构造二次推理用的 prompt（用户原 prompt
+ 一句 ROI 描述）。二次推理由 ``RkaiDetector._infer_sync`` 负责
发起，本工具只负责把 crop + prompt 准备好。

字段约束：

* ``bbox`` 必填，长度 4；超出 [0, 1000] 的元素会在裁剪前被夹紧。
"""
from __future__ import annotations

from typing import Annotated, List

import numpy as np

from rkai import Callable as RkaiCallable
from rkai import Field as RkaiField


@RkaiCallable
class CropRegion:
    """原图缩放工具：当画面中存在主体不清晰、或希望放大观察某区域时调用。"""

    bbox: Annotated[List[float], "0–1000 归一化坐标 [x1, y1, x2, y2]"] = RkaiField(
        default_factory=list,
        description="0–1000 归一化坐标 [x1, y1, x2, y2]",
    )

    def __call__(self, frame_bgr: np.ndarray, ctx):
        x1, y1, x2, y2 = self._clamp_bbox(self.bbox, ctx.width, ctx.height)
        crop = frame_bgr[y1:y2, x1:x2]
        if crop.size == 0:
            return {
                "crop": None,
                "followup_prompt": ctx.prompt,
                "bbox_pixels": [x1, y1, x2, y2],
                "empty": True,
            }
        bbox_str = self._format_bbox(x1, y1, x2, y2, ctx.width, ctx.height)
        followup_prompt = (
            f"{ctx.prompt} 本次只关注 ROI（像素坐标 x1={x1}, y1={y1}, "
            f"x2={x2}, y2={y2}，归一化 {bbox_str}）内的内容。"
        )
        return {
            "crop": crop,
            "followup_prompt": followup_prompt,
            "bbox_pixels": [x1, y1, x2, y2],
            "bbox_normalized": self._coerce_bbox(self.bbox),
            "empty": False,
        }

    @staticmethod
    def _coerce_bbox(bbox) -> List[float]:
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            return [0.0, 0.0, 0.0, 0.0]
        return [float(v) for v in bbox]

    @staticmethod
    def _clamp_bbox(bbox, width: int, height: int):
        vals = CropRegion._coerce_bbox(bbox)
        x1 = int(max(0, min(1000, vals[0])) / 1000.0 * width)
        y1 = int(max(0, min(1000, vals[1])) / 1000.0 * height)
        x2 = int(max(0, min(1000, vals[2])) / 1000.0 * width)
        y2 = int(max(0, min(1000, vals[3])) / 1000.0 * height)
        # 至少 1px 高度 / 宽度
        if x2 <= x1:
            x2 = min(width, x1 + 1)
        if y2 <= y1:
            y2 = min(height, y1 + 1)
        return x1, y1, x2, y2

    @staticmethod
    def _format_bbox(x1: int, y1: int, x2: int, y2: int, width: int, height: int) -> str:
        if width <= 0 or height <= 0:
            return "n/a"
        return (
            f"x1={x1/width:.3f}, y1={y1/height:.3f}, "
            f"x2={x2/width:.3f}, y2={y2/height:.3f}"
        )
