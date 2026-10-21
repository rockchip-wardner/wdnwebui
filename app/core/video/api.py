"""帧判据的共享 HTTP 入口 —— vd / vtd / vtl 三张卡共用。

三张卡共用 :mod:`app.core.video.session` 的单例，所以**判据也只有一份**：
任一张卡上改配置，三张卡同时生效（它们本来就在看同一个视频源）。挂一个
core 级 router 比让每张卡各抄一份端点更不容易走样。

``video-retrieval``（监控反查）**不在此列** —— 它只保留检索，没有判据。

路由前缀由 :func:`register_judge_api` 决定（``app/main.py`` 调）。
"""

from __future__ import annotations

from typing import Optional

import logging

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel, Field

from app.core.video.frame_judge import (
    DEFAULT_LONG_SIDE,
    EXPOSED_METRICS,
    MODE_MANUAL,
    cost_ms,
    default_threshold,
    metrics,
    normalize_mode,
)
from app.core.video.session import get_session

LOG = logging.getLogger("core.video.judge")

#: 判据默认长边，前端展示用（改这里记得同步 frame_judge 的同名常量）。
JUDGE_LONG_SIDE = DEFAULT_LONG_SIDE

router = APIRouter(tags=["video-judge"])


class JudgePayload(BaseModel):
    mode: str = Field(default=MODE_MANUAL,
                      description="manual（手动跳帧）或任一暴露的度量名（见 /judge/metrics）")
    every_n: int = Field(default=30, ge=1, le=3600,
                         description="帧间隔：手动跳帧的固定间隔；自动判帧的兜底间隔")
    # **默认 None，不是数字**。"没传阈值"和"显式设成某个值"必须能区分：
    # 填一个数字默认值的话，客户端只想改 every_n 时也会把阈值冲掉（实测
    # 先设 7.5、再发 {mode:"conc", every_n:60}，阈值被弹回默认）。
    # None 由 FrameJudge 按"模式变了用新默认、模式没变保留现值"解释。
    threshold: Optional[float] = Field(default=None, gt=0, le=1000,
                                       description="自动判帧的变化阈值（不传=按模式规则）")


@router.get("/judge/metrics")
async def get_metrics() -> dict:
    """下拉可选项 + 每个算法的**实测单帧成本**（ms/帧），前端拿去标注。"""
    ms = metrics()
    return {
        "mode": MODE_MANUAL,
        "metrics": ms,
        "cost_ms": {m: round(cost_ms(m) or 0.0, 2) for m in ms},
        # 每个度量的默认阈值。**必须由后端给** —— 不同度量的刻度根本不可比
        # （z_mad 是灰阶口径 6.0，conc 是比值口径 15.0），前端写死一个数迟早
        # 会在换了度量之后悄悄失效，而失效表现为"阈值不对"而不是报错。
        "default_threshold": {m: default_threshold(m) for m in ms},
        "long_side": DEFAULT_LONG_SIDE,
    }


def _normalize_alias(mode: str) -> str:
    """把旧客户端的 ``auto`` 字面量翻成当前默认度量，再交给 normalize_mode 收敛。

    不做这一步的话 ``POST /judge {"mode":"auto"}`` 会被当成未知模式静默回落
    manual —— 自动判帧"看起来关了"，而用户什么都没改。存量配置里的 ``z_mad``
    同理（``normalize_mode`` 内部有迁移表）。

    **只用于校验** —— 真正传给 ``FrameJudge`` 的是原始字符串，见 :func:`set_judge`
    里那条"静默失效"的注释。
    """
    m = str(mode or "").strip().lower()
    return normalize_mode(EXPOSED_METRICS[0] if m == "auto" else m)


@router.get("/judge")
async def get_judge() -> dict:
    return get_session().judge_snapshot()


@router.post("/judge")
async def set_judge(payload: JudgePayload) -> dict:
    """配置判据（真节流）：改完立刻对后续解码生效。

    阈值 / 算法变了会清空判据器的参照帧 —— 旧口径下选出来的"上一帧"对新
    口径没有意义，留着会算出一段假的连续趋势。

    **必须把 payload.mode 原样交给 ``configure_judge``**，只在校验时归一化。
    这里曾经先调 ``_normalize_alias`` 再传下去，结果存量请求里的 ``z_mad``
    到 ``FrameJudge._migrate`` 手里已经变成 ``conc`` —— 迁移表认不出原始
    模式名，于是**阈值不重置**，线上实测变成 ``conc + threshold=1.0``：
    conc 的自然相邻帧 p50 就有 3.23，1.0 意味着几乎每帧都送检。存量迁移
    静默失效比不迁移更糟 —— 它看起来是"生效了"，实际把节流关掉了。

    **这里不做 400 校验**：未知 / 已下线的算法名一律**回落 manual**（项目
    契约，前端据此把下拉里补出来的幽灵 option 剪掉）。改成 400 的话，
    POST 失败 → 前端拿不到新状态 → `_pruneModes()` 不执行 → 幽灵项永远
    留在下拉里选不上。曾一度改成 400，就是这条。

    **空 body / 缺 mode 字段 = 保留当前 mode**（不要把 mode 强制 reset 到
    default）。靠 ``payload.model_fields_set`` 判定"Pydantic 是不是替我们
    填了 default"：早期用 `raw if raw else None` 的写法对 `{"mode": ""}`
    之外的空 body 都不生效（因为 JudgePayload.mode 的 default 是 "manual"，
    Pydantic 自动填进去后 raw 永远非空），于是 `POST /judge {}` 会把当前
    gscore 静默打回 manual —— 用户感受就是"向导进来 gscore，刷新一下就
    变 manual 了"。这里的契约是**"字段缺失 = 沿用当前值"**。
    """
    if "mode" in payload.model_fields_set:
        raw = str(payload.mode or "").strip().lower()
        mode_arg = raw if raw else None  # 显式 mode="" 同样保留当前
    else:
        mode_arg = None
    snap = get_session().configure_judge(
        mode=mode_arg, every_n=payload.every_n,
        threshold=payload.threshold)           # 迁移由 FrameJudge 负责
    LOG.info("video judge configured: mode=%s every_n=%s threshold=%.2f",
             snap.get("mode"), payload.every_n, snap.get("threshold"))
    return snap


@router.post("/judge/reset")
async def reset_judge() -> dict:
    """清空判据参照帧（不动配置）。"""
    s = get_session()
    s.reset_judge()
    return s.judge_snapshot()


def register_judge_api(app: FastAPI, prefix: str = "/api/video") -> None:
    app.include_router(router, prefix=prefix)
    LOG.info("core.video: judge API mounted at %s", prefix)
