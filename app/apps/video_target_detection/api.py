"""视频目标检测 endpoints —— 本页特有的那一小块。

共用的 REST / WS 端点由 :func:`app.core.video.multi_api.build_multi_router`
生成；这里只剩 ``TargetDoc`` detector 与它那套 prompt 字段解析。
detector 用 ``get_target_detector()``（结构化输出直接拿
``x1, y1, x2, y2`` + ``desc``），**不**挂任何 ``@Callable`` 工具 ——
该页没有"工具集合"概念，``/tools`` 端点也不存在。
"""
from __future__ import annotations

from fastapi import HTTPException

from app.core.llm.detector_base import get_target_detector
from app.core.video.multi_api import MultiCardSpec, PromptPlan, build_multi_router
from app.core.video.multi_session import get_multi_session

# 会话单例按卡隔离：同一进程里 vd / vtl 各用一个 key，否则 vtl 的 open()
# 会把 vtd 的解码器全 close 掉（切回上一页画面全黑）。
SESSION_KEY = "vtd"


def _session():
    return get_multi_session(SESSION_KEY)


def _parse_prompt(payload: dict) -> PromptPlan:
    """解析 ``POST /prompt`` 的 body。

    ``{prompt: str, every_n: int, image_size: int | None, model_id: str | None,
      rr_interval: float | None}``

    ``image_size`` 缺省 / ``None`` = 按帧自动选最近的 vision 变体（保留
    legacy 行为）；``model_id`` 缺省 / 空串 = auto-pick。

    ``every_n`` 是遗留字段：多路下由轮转调度器承担节流，工厂会锁死为 1。
    这里仍解析它，但**只用于在老前端没传 ``rr_interval`` 时当「秒/路」
    解读**（页面既有语义），且不做 ``>=1`` 校验 —— 否则前端合法的 0
    （轮转间隔 0 秒）会被误判非法直接 400。

    ``enabled_tools`` 忽略即可（该页只走 TargetDoc），仍接受是为了让老前端的
    过渡期请求体不掉字段。
    """
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt 不能为空")

    try:
        legacy_every_n = int(payload.get("every_n", 30) or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="every_n 必须为整数")

    # 提前到 HTTP 层校验只是为了给出更具体的消息，合法性 detector.start()
    # 内部还会再查一遍。
    raw_image_size = payload.get("image_size", None)
    image_size: int | None
    if raw_image_size is None:
        image_size = None
    elif isinstance(raw_image_size, bool) or not isinstance(raw_image_size, int):
        raise HTTPException(
            status_code=400,
            detail=f"image_size 必须是整数或 null: {raw_image_size!r}",
        )
    else:
        image_size = raw_image_size

    raw_model_id = payload.get("model_id", None)
    if raw_model_id is None or (isinstance(raw_model_id, str)
                                and not raw_model_id.strip()):
        model_id: str | None = None
    elif not isinstance(raw_model_id, str):
        raise HTTPException(
            status_code=400,
            detail=f"model_id 必须是字符串或 null: {raw_model_id!r}",
        )
    else:
        model_id = raw_model_id

    raw_interval = payload.get("rr_interval", None)
    if raw_interval is None:
        rr_interval = float(legacy_every_n)   # 老前端：every_n 即秒/路
    else:
        try:
            rr_interval = float(raw_interval)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="rr_interval 必须是数字")

    return PromptPlan(
        start_kwargs={
            "prompt": prompt,
            "enabled_tools": [],      # 该页不挂工具，显式置空
            "image_size": image_size,
            "model_id": model_id,
        },
        rr_interval=rr_interval,
    )


router = build_multi_router(
    MultiCardSpec(
        session_getter=_session,
        detector_getter=get_target_detector,
        parse_prompt=_parse_prompt,
    ),
    tag="video-target-detection",
)