"""``CropRegion`` —— 目标剪裁工具。

模型给出 bbox，detector 不再走 VLM，直接按 bbox 在原始 BGR
帧上裁切、JPEG 编码、base64 内嵌到事件 JSON（``crop_b64`` /
``crop_w`` / ``crop_h``），前端在 ``<canvas>`` 之上叠红框 +
浮动窗展示剪裁图；事件历史列表里挂小缩略图。

字段：0–1000 归一化坐标 [x1, y1, x2, y2]，与 ``AnnotateROI``
同 schema；工具内部仅做字段校验，不做剪裁 —— 剪裁是 detector
的事，便于复用与单测。
"""
from __future__ import annotations

from typing import Annotated, List

from rkai import Callable as RkaiCallable
from rkai import Field as RkaiField


@RkaiCallable
class CropRegion:
    """目标剪裁工具：给出 0–1000 归一化坐标 [x1, y1, x2, y2]，后端会裁切原图并以 JPEG 通过事件推回前端展示。"""

    bbox: Annotated[List[float], "0–1000 归一化坐标 [x1, y1, x2, y2]"] = RkaiField(
        default_factory=list,
        description="0–1000 归一化坐标 [x1, y1, x2, y2]",
    )

    def __call__(self, frame_bgr, ctx):
        if not isinstance(self.bbox, (list, tuple)) or len(self.bbox) != 4:
            return {"bbox": [0.0, 0.0, 0.0, 0.0], "invalid": True}
        return {"bbox": [float(v) for v in self.bbox], "invalid": False}
