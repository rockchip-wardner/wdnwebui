"""音视频描述 endpoints —— 这张卡的特有部分。

共用的 REST / WS 端点由 :func:`app.core.video.multi_api.build_multi_router`
生成；这里只留下三样卡片特有的东西：

* detector 单例 :func:`get_detector` —— ``VLMDoc`` **自由文本**描述 + 可选
  ``tts_alert`` 语音提醒。vtd / vtl 走的是结构化输出（``TargetDoc`` /
  ``LocatorDoc``），本页不要 bbox，所以工具集合是页面独有的。
* :func:`_parse_prompt` —— ``enabled_tools`` 白名单 + ``image_size``
  （下拉框来自 ``/available-vision-sizes``）+ ``model_id``。
* ``/tools`` 与 ``/available-vision-sizes`` 两个页面独有端点。

**送检尺寸不设死**：与 vtd 同口径走 ``derive_inference_size``（按源算，
720p → 640），只有 vtl 因为 LocateAnything 硬编码 448 才固定。多路不比
单路更该在尺寸上将就 —— 送检尺寸决定 VLM 选哪个 vision 变体，送 384 就是
掉进便宜档换精度降档。

帧怎么到 detector：``MultiChannelSession`` 自带帧源，工厂在 ``POST /prompt``
里 ``attach_detector`` + ``start_round_robin()``，轮转线程逐路取当前帧提交
（不再经由 ``VideoSession`` 的 frame sink）。
"""
from __future__ import annotations

from fastapi import HTTPException

from app.core.llm.detector_base import get_detector
from app.core.video.multi_api import MultiCardSpec, PromptPlan, build_multi_router
from app.core.video.multi_session import MultiChannelSession, get_multi_session

# 会话单例按卡隔离：vtd / vtl 各自一个 key。共用同一个 key 时，另一张卡的
# open() 会 close 掉本卡的解码器，表现为「切回上一页画面全黑」。
SESSION_KEY = "vd"

# 音视频描述页只允许「语音提醒」工具。其他工具即便注册表里有也不
# 在这里暴露 —— 该页面是面向「描述+提醒」场景的固定工具集。
ALLOWED_TOOLS = ("tts_alert",)


def _session() -> MultiChannelSession:
    return get_multi_session(SESSION_KEY)


def _parse_prompt(payload: dict) -> PromptPlan:
    """解析 ``POST /prompt`` 的 body。

    ``{prompt: str, every_n: int, enabled_tools: list[str],
      image_size: int | None, model_id: str | None,
      rr_interval: float | None}``

    ``enabled_tools`` 先校验是 list，再按 :data:`ALLOWED_TOOLS` 过滤 ——
    前端 UI 已按白名单过滤，这里再卡一层是为了直接 curl 越权。

    ``image_size`` 缺省 / ``None`` = 按帧自动选最近的 vision 变体（保留
    legacy 行为）；``model_id`` 缺省 / 空串 = auto-pick。

    ``every_n`` 是遗留字段：多路下由轮转调度器承担节流，工厂锁死为 1。
    这里仍解析它，但**只用于**老前端没传 ``rr_interval`` 时当「秒/路」
    解读（页面既有语义），且不做 ``>=1`` 校验 —— 否则前端合法的 0
    （轮转间隔 0 秒）会被误判非法直接 400。
    """
    payload = payload or {}
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt 不能为空")

    raw_tools = payload.get("enabled_tools") or []
    if not isinstance(raw_tools, list):
        raise HTTPException(status_code=400, detail="enabled_tools 必须是列表")
    enabled_tools = [k for k in raw_tools if k in ALLOWED_TOOLS]

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

    try:
        legacy_every_n = int(payload.get("every_n", 30) or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="every_n 必须为整数")

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
            "enabled_tools": enabled_tools,
            "image_size": image_size,
            "model_id": model_id,
        },
        rr_interval=rr_interval,
    )


router = build_multi_router(
    MultiCardSpec(
        session_getter=_session,
        detector_getter=get_detector,
        parse_prompt=_parse_prompt,
    ),
    tag="video-detection",
)


# --------------------------------------------------------------------- #
# 本页独有端点                                                          #
# --------------------------------------------------------------------- #
# /upload 与其余会话 / 检测 / 诊断 / 判据 / WS 端点都由工厂提供。


@router.get("/available-vision-sizes")
async def available_vision_sizes() -> dict:
    """List the vision-model native sizes currently loaded into the
    running ``swcap_video_describe`` compute instance (qwen3-vl-4b /
    qwen3.5-vl-4b / future VLM presets — anything advertising that
    capability whose backend exposes ``available_image_sizes``).

    Returned shape: ``{"sizes": [384, 768], "default": 384}`` where
    ``sizes`` is the data-driven enumeration reported by the worker's
    info RPC (single-element ``[384]`` on legacy single-resolution
    installs; ``[384, 768]`` on full multi-resolution installs;
    empty when the instance isn't running yet). ``default`` is the
    smallest reported size — matches the prior hardcoded behaviour
    of the 384x384 default.

    The 音视频描述 page's ``#vd-detect-size`` dropdown is populated
    from this endpoint on page open. **No** 384/768 list is baked
    into the client.
    """
    from app.core.compute.registry import get_registry
    registry = get_registry()
    sizes: list[int] = []
    for inst in registry.list_with_source():
        # Filter by capability instead of model_id so any future VLM
        # preset exposing ``swcap_video_describe`` (e.g.
        # qwen3.5-vl-4b) is picked up automatically.
        caps = inst.get("capability_types") or []
        if "swcap_video_describe" not in caps:
            continue
        raw = inst.get("available_image_sizes") or []
        if not isinstance(raw, list):
            continue
        for s in raw:
            if isinstance(s, bool):
                continue
            try:
                sizes.append(int(s))
            except (TypeError, ValueError):
                continue
        sizes = sorted(set(sizes))
        break
    return {
        "sizes":   sizes,
        "default": sizes[0] if sizes else None,
    }


@router.get("/tools")
async def list_tools() -> dict:
    """返回当前 detector 可启用的工具列表 + 能力可用性。

    每条记录包含 ``key`` / ``name`` / ``description`` /
    ``requires_capability`` / ``available`` / ``reason`` / ``fields``。
    该页只暴露 :data:`ALLOWED_TOOLS` 集合里的工具（语音提醒）；其他工具
    注册在 ``app.services.tools.registry`` 里但不在这里展示。
    """
    from app.core.compute.registry import get_registry
    from app.core.tools.registry import get_tool_registry

    reg = get_tool_registry()
    out = []
    for spec in reg.list_specs():
        if spec.key not in ALLOWED_TOOLS:
            continue
        available = True
        reason = ""
        if spec.requires_capability:
            try:
                get_registry().get_session(spec.requires_capability)
            except RuntimeError as e:
                available = False
                reason = str(e)
        out.append({
            "key":                 spec.key,
            "name":                spec.name,
            "icon":                spec.icon,
            "description":         spec.description,
            "requires_capability": spec.requires_capability,
            "available":           available,
            "reason":              reason,
            "fields":              [
                {"name": fname, "description": fdesc}
                for fname, fdesc in spec.fields
            ],
        })
    return {"tools": out}
