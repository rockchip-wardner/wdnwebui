"""``AnnotateROI`` —— 目标标注工具。

模型给出 bbox，detector 不再走 VLM，直接把 bbox 通过 WS 事件
推给前端；前端在 ``<canvas>`` 之上叠一层 SVG，红框 5 秒淡出。

字段与 ``CropRegion`` 一致：0–1000 归一化 bbox。
"""
from __future__ import annotations

from typing import Annotated, List

from rkai import Callable as RkaiCallable
from rkai import Field as RkaiField


@RkaiCallable
class AnnotateROI:
    """目标标注工具：给出 0–1000 归一化坐标 [x1, y1, x2, y2]，前端会在画布上框出该 ROI。"""

    bbox: Annotated[List[float], "0–1000 归一化坐标 [x1, y1, x2, y2]"] = RkaiField(
        default_factory=list,
        description="0–1000 归一化坐标 [x1, y1, x2, y2]",
    )

    def __call__(self, frame_bgr, ctx):
        if not isinstance(self.bbox, (list, tuple)) or len(self.bbox) != 4:
            return {"bbox": [0.0, 0.0, 0.0, 0.0], "invalid": True}
        return {"bbox": [float(v) for v in self.bbox], "invalid": False}
