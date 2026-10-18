"""视频目标定位 endpoints —— 本页特有的那一小块。

共用的 REST / WS 端点由 :func:`app.core.video.multi_api.build_multi_router`
生成；这里只剩 ``LocatorDoc`` detector、``prompts[]`` 多目标拼装与送检尺寸。

送检尺寸为什么固定 448
--------------------
``LocateAnythingBackend`` 视觉硬编码 448×448 单变体，内部自己
``letterbox_resize(rgb, 448)``。送 640（或 720p 原图）只是让 RGA 白缩一遍、
结果完全一样 —— 送检尺寸定 448。bbox 是 0–1000 归一化、与送检尺寸无关，
前端叠加框不需要换算。
"""
from __future__ import annotations

from fastapi import HTTPException

from app.core.llm.detector_base import get_localizer_detector
from app.core.video.multi_api import (
    MultiCardSpec,
    PromptPlan,
    build_multi_router,
    parse_models_field,
)
from app.core.video.multi_session import MultiChannelSession, get_multi_session

# 会话单例按卡隔离（见 get_multi_session 的 docstring）。
SESSION_KEY = "vtl"

# 送检长边。对齐 LocateAnything 的固定 448×448 vision。
ANALYSIS_SIZE = 448

DEFAULT_LOCATOR_PROMPT_PREFIX = "找出所有符合以下描述的对象："
LOCATOR_PROMPT_SEPARATOR = "</c>"


def _session() -> MultiChannelSession:
    return get_multi_session(SESSION_KEY,
                             lambda: MultiChannelSession(
                                 analysis_size=ANALYSIS_SIZE))


def _build_vtl_prompt(items: list) -> str:
    """``前缀 + LOCATOR_PROMPT_SEPARATOR.join(非空条目)``；全空抛
    ``ValueError``（由 :func:`_parse_prompt` 转成 400）。非字符串条目按
    ``str(x)`` 归一。
    """
    cleaned: list[str] = []
    for x in items:
        if not isinstance(x, str):
            x = str(x)
        s = x.strip()
        if s:
            cleaned.append(s)
    if not cleaned:
        raise ValueError("至少需要一条非空的定位描述")
    return DEFAULT_LOCATOR_PROMPT_PREFIX + LOCATOR_PROMPT_SEPARATOR.join(cleaned)


def _parse_prompt(payload: dict) -> PromptPlan:
    """解析 ``POST /prompt`` 的 body —— 两种互斥形态：

    1. 老写法 ``{"prompt": str}``（完整 prompt 串）
    2. 新写法 ``{"prompts": [str, ...]}``（原始条目，前缀与 join 由
       :func:`_build_vtl_prompt` 负责）

    ``prompts`` 给了就以它为准（空 list 也算给了）；没给才回落老 ``prompt``。
    本页不接受 ``image_size``（vision 固定 448，size picker 无意义）；
    ``enabled_tools`` 恒为空 —— 该页只走 ``LocatorDoc``。

    ``models`` 是勾选的**实例**列表（多卡并行推理），缺省 = 单实例自动挑。
    """
    legacy_prompt = payload.get("prompt")
    prompts_list = payload.get("prompts")

    if prompts_list is not None and not isinstance(prompts_list, list):
        raise HTTPException(status_code=400, detail="prompts 必须是字符串列表")

    legacy_non_empty = (isinstance(legacy_prompt, str)
                        and legacy_prompt.strip() != "")
    prompts_non_empty = (isinstance(prompts_list, list)
                         and len(prompts_list) > 0)

    if legacy_non_empty and prompts_non_empty:
        raise HTTPException(
            status_code=400, detail="prompt 与 prompts 互斥，只能传一个")

    if prompts_list is not None:
        try:
            prompt = _build_vtl_prompt(prompts_list)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    else:
        prompt = legacy_prompt.strip() if isinstance(legacy_prompt, str) else ""
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt 不能为空")

    # every_n 是遗留字段，多路下由轮转调度器承担节流（工厂锁死为 1）。
    # 只在解析失败时报错；合法值解读成「秒/路」，兼容页面既有语义 ——
    # 所以**不再做 >=1 校验**，否则前端合法的 0（间隔 0 秒）会被误判非法。
    try:
        legacy_every_n = int(payload.get("every_n", 30) or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="every_n 必须为整数")

    raw_interval = payload.get("rr_interval", None)
    if raw_interval is None:
        rr_interval = float(legacy_every_n)
    else:
        try:
            rr_interval = float(raw_interval)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="rr_interval 必须是数字")

    return PromptPlan(
        start_kwargs={"prompt": prompt, "enabled_tools": [],
                      "models": parse_models_field(payload)},
        rr_interval=rr_interval,
    )


router = build_multi_router(
    MultiCardSpec(
        session_getter=_session,
        detector_getter=get_localizer_detector,
        parse_prompt=_parse_prompt,
    ),
    tag="video-target-localization",
)
# POST /zoom（宫格点开放大）已挪进 multi_api.build_multi_router —— v0.24 起
# vd / vtd / vtl 三张卡共用一份实现，不再各卡挂一份副本。
