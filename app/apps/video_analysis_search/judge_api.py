"""VAS 的帧判据配置端点 —— 与 vd / vtd / vtl 共用同一套语义，但走 VAS
自己的 service。

差别在于**状态归属**：那三张卡共用 ``VideoSession`` 单例（判据也只有一份，
挂 ``/api/video/judge``），而 VAS 自管理 ``VideoCapture`` 与 decode 循环
（见 service.py 顶部"独立于 VideoSession"），所以配置得落到它自己的服务上。
判据实现两者是同一份（``core/video/frame_judge.py``）。

路由挂在 ``/api/video-analysis-search/judge*``。
"""

from __future__ import annotations

from typing import Optional

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.video.change_detector import ChangeDetector
from app.core.video.frame_judge import (MODE_MANUAL, cost_ms, default_threshold,
                                       metrics)

from app.apps.video_analysis_search.service import get_service

LOG = logging.getLogger("vas.judge")

router = APIRouter(tags=["vas-judge"])


class JudgePayload(BaseModel):
    mode: str = Field(default=MODE_MANUAL,
                      description="manual（手动跳帧）或任一暴露的度量名（见 /judge/metrics）")
    every_n: int = Field(default=30, ge=1, le=3600, description="帧间隔 / 兜底间隔")
    # 默认 None 而非数字：见 core/video/api.py JudgePayload 的同名字段。
    threshold: Optional[float] = Field(default=None, gt=0, le=1000,
                                       description="变化阈值（不传=按模式规则）")


@router.get("/judge/metrics")
async def get_metrics() -> dict:
    """下拉可选项 + 每个算法的**实测单帧成本**（ms/帧）与默认阈值。"""
    ms = metrics()
    return {
        "mode": MODE_MANUAL,
        "metrics": ms,
        "cost_ms": {m: round(cost_ms(m) or 0.0, 2) for m in ms},
        # 见 core/video/api.py 同名字段：刻度按度量不同，不许前端写死。
        "default_threshold": {m: default_threshold(m) for m in ms},
    }


@router.get("/judge")
async def get_judge() -> dict:
    return get_service().judge_view()


@router.post("/judge")
async def set_judge(payload: JudgePayload) -> dict:
    # **空 body / 缺 mode 字段 = 保留当前 mode**。靠 ``model_fields_set``
    # 判定"Pydantic 是不是替我们填了 default"：早期用 `raw if raw else None`
    # 对 `{"mode": ""}` 之外的空 body 都不生效（JudgePayload.mode 的 default
    # 是 "manual" 永远非空），于是 `POST /judge {}` 会把当前 gscore 静默
    # 打回 manual。契约是"字段缺失 = 沿用当前值"。详见 core/video/api.py
    # set_judge 的同位置注释。
    if "mode" in payload.model_fields_set:
        raw = str(payload.mode or "").strip().lower()
        mode_arg = raw if raw else None  # 显式 mode="" 同样保留当前
    else:
        mode_arg = None
    try:
        return get_service().configure_judge(
            mode=mode_arg, every_n=payload.every_n,
            threshold=payload.threshold)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


__all__ = ["router", "ChangeDetector"]
