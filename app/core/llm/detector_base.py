"""``wdnai``-backed video detector (v2 API).

Frames arrive from ``VideoSession`` via a frame-sink callback; every
Nth frame is fed to a single ``LLMSession`` with the user prompt.
Completed inferences live in an in-memory ring buffer and are pushed
to the registered broadcasters.

The service drives the v2 ``LLMSession.respond`` API with a list-
shaped ``attachment`` (so the session can also carry text/audio
later) and an ``LMOutput``-derived ``generating`` schema; for VLM
``.attachment`` is just an empty list, but the slot exists so the
same schema could be reused with a TTS-style backend without
code changes here.

**两种 generating schema，按 detector 实例分别配置**：

* ``VLMDoc`` —— 音视频描述页（``get_detector()``）使用，模型返回
  ``text`` + 可选 ``level``。
* ``TargetDoc`` —— 视频目标检测页（``get_target_detector()``）使
  用，模型直接给出 ``x1, y1, x2, y2``（0–1000 归一化）和 ``desc``
  （ROI 内描述文本）。后端按 bbox 裁切原帧、JPEG 编码、base64 内
  嵌到事件 JSON（``crop_b64`` / ``crop_w`` / ``crop_h``），``desc``
  同时写到事件 ``text`` 字段（沿用历史展示路径）。

**单帧多轮工具循环**（agent-loop）：``_infer_sync`` 现在会对一帧
跑最多 ``compute.tool_call_max_iters``（默认 5，``0`` 无限）轮
``sess.respond``；每轮若模型返回 ``@Callable`` 实例就执行副作用
并把"调用 + 返回"作为文本 attachment 喂回下一轮 prompt，直到模型
返回 ``generating`` schema 实例或循环上限命中。循环上限 + 兜底硬
上限共同防止失控模型锁死 worker。``TargetDoc`` 模式不挂任何
``@Callable`` 工具，因此循环天然只跑一轮就退出。

``wdnai`` is heavy (RKNN model load is several seconds) and inference
is single-shot — one worker, one session, queue maxsize=1 (latest-only).
On boxes without the model files ``start()`` records an error in
``snapshot()["init_error"]`` and the worker exits cleanly.
"""
from __future__ import annotations

import asyncio
import json
import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import numpy as np

from app.core.config import get_config
from app.core.video.session import get_session


def _result_raw_text(result) -> str:
    """The model's raw output behind whatever ``respond()`` handed back.

    ``LLMSession.respond()`` has three return shapes and the detector has
    to cope with all of them:

    * a ``generating`` instance (``VLMDoc`` / ``TargetDoc`` / …) — the
      output validated;
    * a plain ``str`` — it did not. The SDK's ``_safe_validate`` returns
      the raw model text rather than raising, which is deliberate: a
      schema miss must not lose what the model actually said;
    * a ``@Callable`` instance — the model called a tool.

    Only the first two carry text, and ``getattr(result, "text", "")``
    silently returns ``""`` for a ``str`` — which would turn "the model
    wrote something we could not parse" into "the model wrote nothing".
    """
    if isinstance(result, str):
        return result
    text = getattr(result, "text", None)
    return str(text) if text else ""


def _result_schema_parse_failed(result, generating_cls) -> bool:
    """Did ``respond()`` come back unparsed for a schema we asked for?

    True whenever a schema was requested and what arrived is not an
    instance of it. The old transport flagged this explicitly
    (``_ProxyResult.schema_parse_failed``); now the shape says it — and
    says it in a way that cannot be wrong by omission, because there is
    no separate flag to forget to set.
    """
    if getattr(result, "schema_parse_failed", False):
        return True
    if generating_cls is None:
        return False
    try:
        return not isinstance(result, generating_cls)
    except TypeError:  # generating_cls not a class — can't type-check it
        return False


# 历史默认（不再使用，简化版见 DEFAULT_*_DESC）：见 _build_vlm_doc_class / _build_target_doc_class 函数体内 inline 字符串
# App 提示词默认值（简化中文一句）；用户可在「系统配置 → App 提示词模板」覆盖。
DEFAULT_VLM_DOC_TEXT_DESC = "用中文一句话描述画面中的场景、主体（人/物）、动作和环境。"
DEFAULT_VLM_DOC_LEVEL_DESC = "严重程度标签：正常 / 警告 / 危险。"
DEFAULT_TARGET_DOC_DESC_DESC = "对目标框内内容的一句话中文描述。"
DEFAULT_LOCATOR_DOC_DESC = "一句话中文描述本次定位结果（含类别与位置概要）。"

# 历史：TargetDoc 早期是 4 个独立 float field（x1 / y1 / x2 / y2） + desc。
# 真机 Qwen3-VL-4B 在 768×768 变体上约 30% 概率把坐标输出成 0-1 范围
# 小数（而不是协议规定的 0-1000），导致 unletterbox_bbox 还原到原图后
# 整 ROI 压到左上角 ~0 像素 → 裁切为空 → (目标检测失败: 裁切区域为空)
# 事件。samples/object_detect.py 的 ``Crop`` @Callable 工具用的是单
# ``bbox: list[float]`` 字段（description 写"0-1000 坐标"），
# 真机模型 100% 输出 0-1000 范围——与本路径 4 个独立 float field 的不稳
# 定输出形成对比。
#
# v0.14 修复：TargetDoc 改为 ``bbox: list[float]`` + ``desc: str`` 单一
# list，description 显式"0–1000 坐标 [x1, y1, x2, y2]"——与
# ``samples/object_detect.py:43`` 同源；同时保留 ``_normalize_bbox_to_0_1000``
# 作为兜底（兼容老 worker 进程或 schema 未生效的瞬态窗口），把任何
# 0-1 范围 bbox ×1000 升到 0-1000。
TARGET_DOC_SCHEMA_VERSION = 2
# LocatorDoc — 视频目标定位页 schema。结构升级时 +1 让 ``_SCHEMA_CACHE`` 强制
# miss + rebuild,避免 reload 后 worker 端 model_dump 出的新字段主进程用老 class
# model_validate 触发 silent fallback。``v1`` 初始版:boxes(List[BoundingBox])
# + text(str) + level(Literal["Normal","Warning"])。
LOCATOR_DOC_SCHEMA_VERSION = 1


# wdnai is imported LAZILY (at start() time, not at module load) — RKNN native
# libraries can race with wdnav's mpp init or fork-unsafe state if loaded too
# eagerly, and a missing wheel on dev boxes would otherwise crash app startup
# even for users who never touch the detection panel.
_WDNAI_CACHE: dict = {}

# ``wdnai.grounding`` 是 pure-python (无 RKNN native deps),可在模块加载
# 时安全 import —— 这样 ``LocatorDoc`` schema 的 ``boxes: List[BoundingBox]``
# 类型注解能在模块 globals 命名空间里被 pydantic forward-ref 解析器找到。
# 放在模块级而不是 ``_build_grounding_doc_class`` 函数内的原因是
# ``@Generable`` 装饰器用 caller frame globals 解析 forward ref; 函数
# 内的 ``from ... import ... as _BoundingBox`` 创建的本地变量不在
# globals 里,会让 pydantic v2 抛 ``PydanticUserError: ... not fully
# defined``。把它放到模块 globals 就能在 schema build 时正常解析。
from typing import Literal as _Literal  # noqa: F401  (for forward-ref in LocatorDoc.level)
from typing import List as _List  # noqa: F401  (for forward-ref in LocatorDoc.boxes)
try:
    from wdnai.grounding import BoundingBox as _BoundingBox  # noqa: F401
    from wdnai.grounding import GroundingResult as _GroundingResult  # noqa: F401
except Exception:  # noqa: BLE001
    # 兜底:wdnai.grounding 缺失时(SDK wheel 没装/版本不匹配)让 schema
    # 仍能 build —— 把 ``boxes`` 退化为 ``dict``,模型端 JSON-Schema
    # 提示照常生成;真机 SDK 装好后再用真实 ``BoundingBox``。
    _BoundingBox = dict  # type: ignore[assignment,misc]
    _GroundingResult = None  # type: ignore[assignment,misc]

import logging
LOG = logging.getLogger("detector")
# Uvicorn 的 logging config 把 root logger 设为 WARNING，detector 的
# named logger 虽然 propagate 到 root，但 root 不显示 INFO / DEBUG。
# 这里给 detector 自带一个 StreamHandler（stderr），不依赖 root 的
# 配置，reload 模式下也不会重复挂载（用 getattr 检测）。
if not getattr(LOG, "_wdnwebui_handler_attached", False):
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("[detector] %(asctime)s %(levelname)s %(message)s")
    )
    LOG.addHandler(_handler)
    LOG.setLevel(logging.INFO)
    LOG._wdnwebui_handler_attached = True  # type: ignore[attr-defined]
    LOG.propagate = False


def _load_wdnai():
    """Lazily import wdnai; cache the result so only the first call hits
    the import machinery. Returns ``(ok, Attachment, LLMSession,
    SystemLanguageModel, LMOutput, error_str)``."""
    if "_Attachment" in _WDNAI_CACHE:
        return (
            _WDNAI_CACHE["_ok"],
            _WDNAI_CACHE["_Attachment"],
            _WDNAI_CACHE["_LLMSession"],
            _WDNAI_CACHE["_SystemLanguageModel"],
            _WDNAI_CACHE["_LMOutput"],
            _WDNAI_CACHE["_Generable"],
            _WDNAI_CACHE["_Field"],
            _WDNAI_CACHE["_letterbox_resize_with_meta"],
            _WDNAI_CACHE["_unletterbox_bbox"],
            _WDNAI_CACHE["_error"],
        )
    try:
        from wdnai import (
            Attachment,
            Field,
            Generable,
            LMOutput,
            LLMSession,
            SystemLanguageModel,
        )
        # `wdnai.utils` lives outside the top-level package (it's the
        # SDK helper module), so it's imported explicitly here. Lazy
        # import keeps the wdnav/RKNN init order untouched (mirrors
        # `_load_wdnai`'s overall constraint).
        try:
            from wdnai.utils import (
                letterbox_resize_with_meta,
                unletterbox_bbox,
            )
            _lbox_ok = True
            _lbox_err = ""
        except Exception as _e:  # noqa: BLE001
            letterbox_resize_with_meta = None  # type: ignore[assignment]
            unletterbox_bbox = None  # type: ignore[assignment]
            _lbox_ok = False
            _lbox_err = str(_e)
        _WDNAI_CACHE.update(
            _ok=True,
            _Attachment=Attachment,
            _LLMSession=LLMSession,
            _SystemLanguageModel=SystemLanguageModel,
            _LMOutput=LMOutput,
            _Generable=Generable,
            _Field=Field,
            _letterbox_resize_with_meta=letterbox_resize_with_meta,
            _unletterbox_bbox=unletterbox_bbox,
            _error="" if _lbox_ok else _lbox_err,
        )
        return (
            True, Attachment, LLMSession, SystemLanguageModel,
            LMOutput, Generable, Field,
            letterbox_resize_with_meta, unletterbox_bbox, "",
        )
    except Exception as e:
        _WDNAI_CACHE.update(
            _ok=False,
            _Attachment=None,
            _LLMSession=None,
            _SystemLanguageModel=None,
            _LMOutput=None,
            _Generable=None,
            _Field=None,
            _letterbox_resize_with_meta=None,
            _unletterbox_bbox=None,
            _error=str(e),
        )
        return (
            False, None, None, None, None, None, None,
            None, None, str(e),
        )


# --------------------------------------------------------------------------- #
# Structured output schema — inherits LMOutput so the SDK can populate
# ``attachment`` automatically when a future backend produces binary
# outputs. Currently empty for VLM (text-only path).
# --------------------------------------------------------------------------- #
def _build_vlm_doc_class(text_desc: str, level_desc: str):
    """Build the ``VLMDoc`` schema lazily, once wdnai is importable.

    The class is created with ``@Generable`` + ``LMOutput`` as the base so
    the SDK can attach the parsed fields into an instance that also
    carries the standard ``attachment`` slot.

    ``text_desc`` / ``level_desc`` —— 用户在「系统配置 → App 提示词模板」
    覆盖的 ``Field(description=...)`` 文本；缺失时回退到
    ``DEFAULT_VLM_DOC_TEXT_DESC`` / ``DEFAULT_VLM_DOC_LEVEL_DESC``。
    """
    # ``from typing import Optional`` *inside* this function so the name
    # shows up in the function's locals — ``pydantic`` resolves forward
    # refs by ``typing.get_type_hints``, which searches the enclosing
    # frame's namespace. Without this, the ``Optional[str]`` annotation
    # for ``level`` cannot be evaluated and instantiation fails.
    from typing import Optional

    _, _, _, _, LMOutput, Generable, Field, _, _, _ = _load_wdnai()

    # 注意：继承 LMOutput 会带上 ``attachment: List[Attachment]`` 字段，
    # 但 Attachment 不是 pydantic 模型 —— pydantic v2 的
    # ``model_json_schema()`` 遇到非 pydantic 类型会抛
    # ``PydanticInvalidForJsonSchema``，SDK 的 ``_safe_schema`` catch
    # 后返回空字符串。**直接后果**：模型在 prompt 里看到的
    # "Option B — Structured response" schema hint 是空的，根本不知
    # 道该输出什么结构 → 模型倾向于反复调工具而不返回 structured
    # output → 触发 ``loop hit effective_max=N without VLMDoc``。
    #
    # VLMDoc 实际不会从模型产出 attachment（detector 也不会读它），
    # 改成继承 BaseModel 即可让 schema 正常生成。
    from pydantic import BaseModel as _PydBaseModel

    @Generable
    class VLMDoc(_PydBaseModel):
        text: str = Field(description=text_desc)
        level: Optional[str] = Field(default=None, description=level_desc)

    return VLMDoc


# --------------------------------------------------------------------------- #
# Schema cache keyed by (schema_name, schema_version, tuple(sorted(field_descs))).
#
# Key 包含三个部分：
#   1. ``schema_name`` —— "VLMDoc" / "TargetDoc"。
#   2. ``schema_version`` —— 模块常量 ``VLMDOC_SCHEMA_VERSION`` /
#      ``TARGET_DOC_SCHEMA_VERSION``；schema 结构（字段名 / 类型）变化时
#      版本号 +1，让 reload 后即使 ``descs`` 没变也能 cache miss 强制
#      rebuild 拿到新 class。**特别重要**：schema class 是
#      ``isinstance(result, TargetDoc)`` 检查的对象，类 identity 必须随
#      结构变化而变化；否则 worker 端 model_dump 出的新字段
#      （``bbox``）主进程用老 class model_validate 会失败，触发 silent
#      fallback 到裸 raw text string。
#   3. ``tuple(sorted(field_descs))`` —— 用户在「系统配置 → App 提示词模板」
#      覆盖的 ``Field(description=...)`` 文本；缺 / 空时 fallback 到
#      ``DEFAULT_*_DESC`` 常量。任何 PUT 触发 desc 变化 → 新 cache key →
#      rebuild。Schema rebuild 只在 key miss 时发生（一般每次"保存"最多
#      1 次，不是每次推理）。
#
# TargetDoc 的 4 个坐标字段描述（v1 老格式）或 ``bbox`` 字段描述
# （v2 新格式）是协议级格式规约（"0–1000 归一化"），不属于自然语言提示
# 词，按用户决策保持 hardcoded 不开放给配置；只 ``desc`` 进入 cache key。
# --------------------------------------------------------------------------- #
_SCHEMA_CACHE: dict[tuple, type] = {}
VLMDOC_SCHEMA_VERSION = 1


def _load_app_prompts(app_key: str, schema_cls: str) -> dict[str, str]:
    """从 config.jsonc["app"]["prompts"][app_key][schema_cls] 读 schema 提示词覆盖。

    配置结构在 v0.11 重整：``compute.app_prompts`` → ``app.prompts``
    （新顶层 ``app`` section 装所有"应用层"用户配置 —— 目前只有 prompts，
    未来可能加更多）。读路径保留 ``compute.app_prompts`` 兜底，让老
    config.jsonc 不需要手动迁移；后续任意 save 触发落盘后，老路径会
    自然被 ``app.prompts`` 替换。

    缺字段 / 空值 → fallback 到 ``DEFAULT_*_DESC``（不写盘）。
    """
    cfg_store = get_config()
    # New path: app.prompts
    app_cfg = cfg_store.get_section("app", {}) or {}
    prompts = app_cfg.get("prompts") or {}
    if not isinstance(prompts, dict) or not prompts:
        # Backward-compat fallback: compute.app_prompts
        compute_cfg = cfg_store.get_section("compute", {}) or {}
        prompts = compute_cfg.get("app_prompts") or {}
    if not isinstance(prompts, dict):
        prompts = {}
    app_block = prompts.get(app_key) or {}
    schema_block = app_block.get(schema_cls) or {}
    result: dict[str, str] = {}
    # 映射表：每个 schema 类的可配字段名 → 默认常量
    defaults = {
        ("video_detection", "VLMDoc"): {"text": DEFAULT_VLM_DOC_TEXT_DESC, "level": DEFAULT_VLM_DOC_LEVEL_DESC},
        ("video_target_detection", "TargetDoc"): {"desc": DEFAULT_TARGET_DOC_DESC_DESC},
        ("video_target_localization", "LocatorDoc"): {"desc": DEFAULT_LOCATOR_DOC_DESC},
    }
    for field, default_desc in defaults.get((app_key, schema_cls), {}).items():
        user_val = schema_block.get(field)
        if isinstance(user_val, str) and user_val.strip():
            result[field] = user_val.strip()
        else:
            result[field] = default_desc
    return result


def _get_vlm_doc_cls():
    descs = _load_app_prompts("video_detection", "VLMDoc")
    cache_key = ("VLMDoc", VLMDOC_SCHEMA_VERSION, tuple(sorted(descs.items())))
    cached = _SCHEMA_CACHE.get(cache_key)
    if cached is not None:
        return cached
    cls = _build_vlm_doc_class(descs["text"], descs["level"])
    _SCHEMA_CACHE[cache_key] = cls
    return cls


# --------------------------------------------------------------------------- #
# TargetDoc —— 视频目标检测页 structured-output schema                       #
# --------------------------------------------------------------------------- #
# 取代旧版 ``target_detection`` @Callable 工具（AGENTS.md "TargetDoc 结
# 构化输出 (视频目标检测页)"）：模型在生成结构化输出时直接给出
# ``bbox: [x1, y1, x2, y2]``（0–1000 归一化）+ ``desc``（ROI 内描述
# 文本），detector 在 worker 线程里按 bbox 裁原帧、JPEG 编码、base64
# 内嵌进事件 JSON（``crop_b64`` / ``crop_w`` / ``crop_h``），``desc``
# 复制到 ``text`` 字段，事件历史列表沿用现有展示路径。
#
# v2 schema 设计（与 ``samples/object_detect.py:43`` 同源）：
#   - 单 ``bbox: list[float]`` 字段（不是 4 个独立 float field），description
#     写明"0–1000 坐标 [x1, y1, x2, y2]（相对于 LLM 看到的 letterboxed
#     正方形）"，让模型看到清晰的"list of 4 floats"语义。Qwen3-VL-4B 在
#     ``bbox: list[float]`` 单字段 schema 上输出 0-1000 范围非常稳定
#     （与 ``samples`` 一致），而 4 个独立 field (x1/y1/x2/y2) schema 上
#     约 30% 概率输出 0-1 小数 → 不可用。
#   - ``_normalize_bbox_to_0_1000`` 兜底归一化（处理 schema-version 过渡
#     期间老 worker 仍返回 0-1 范围 dict 的瞬态窗口），把任何
#     ``max(|bbox|) <= 1.5`` 的 bbox ×1000 升到 0-1000。
def _build_target_doc_class(desc_desc: str):
    """Build the ``TargetDoc`` schema lazily, once wdnai is importable.

    与 ``VLMDoc`` 一样继承 ``BaseModel`` 而非 ``LMOutput`` —— 避免
    ``attachment: List[Attachment]`` 字段污染 ``model_json_schema``，
    否则 SDK 的 ``_safe_schema`` 会返回空串，模型看不到字段含义。

    ``desc_desc`` —— 用户在「系统配置 → App 提示词模板」覆盖的
    ``desc`` 字段 ``Field(description=...)`` 文本；缺失时回退到
    ``DEFAULT_TARGET_DOC_DESC_DESC``。``bbox`` 字段的 description 是协议
    级格式规约（"0–1000 归一化"），不属于自然语言提示词，按用户决策
    保持 hardcoded 不开放给配置。
    """
    from typing import Optional, List  # noqa: F401  (kept for parity w/ VLMDoc)

    _, _, _, _, _, Generable, Field, _, _, _ = _load_wdnai()

    from pydantic import BaseModel as _PydBaseModel

    # 重要：TargetDoc **不能带 class docstring**。pydantic 把 docstring
    # 渲染到 ``model_json_schema()`` 的顶层 ``description`` 字段 —— 模
    # 型在 prompt 里看到 ``{"description": "...", "properties": {...}}``
    # 时，会把 ``description`` 当成数据字段（不是 JSON Schema 元数据），
    # 把真正的 bbox/desc 包到 ``{"description": {...}}`` 里。
    # pydantic 校验失败 → 走 ``_safe_validate`` 的 raw text fallback →
    # ``DetectorEvent.text`` 变成半截 schema JSON，事件历史列表里
    # 显示 ``{ "description": "Structured target-detection..." }`` 这
    # 类乱码 + bbox/crop 全无。VLMDoc 没这个 bug 是因为它本就没 docstring。
    @Generable
    class TargetDoc(_PydBaseModel):
        bbox: List[float] = Field(
            description=(
                "裁剪区域0-1000坐标 [x1, y1, x2, y2]，"
                "相对于LLM看到的letterboxed正方形（含灰边）。"
            ),
        )
        desc: str = Field(description=desc_desc)

    return TargetDoc


# Cached after first build. (cache unified into _SCHEMA_CACHE above — see
# ``_get_vlm_doc_cls`` for the key construction.)


def _get_target_doc_cls():
    descs = _load_app_prompts("video_target_detection", "TargetDoc")
    cache_key = (
        "TargetDoc", TARGET_DOC_SCHEMA_VERSION,
        tuple(sorted(descs.items())),
    )
    cached = _SCHEMA_CACHE.get(cache_key)
    if cached is not None:
        return cached
    cls = _build_target_doc_class(descs["desc"])
    _SCHEMA_CACHE[cache_key] = cls
    return cls


# --------------------------------------------------------------------------- #
# LocatorDoc —— 视频目标定位页 structured-output schema                        #
# --------------------------------------------------------------------------- #
# 视觉定位 (grounding) 模型输出：boxes: List[BoundingBox] + text(str) +
# level(Literal["Normal","Warning"])。一个事件可携带多个 bbox(多目标)，
# 整帧 base64 JPEG 附在事件 JSON,前端用原图 + bbox overlay 渲染。
#
# 字段语义对照:
#   - ``boxes`` —— 模型从 ``<box>`` 标记解析出的 ROI 列表,每个 bbox 是
#     ``BoundingBox(label, x1, y1, x2, y2)`` 0–1000 归一化坐标。
#   - ``text`` —— 模型回显的原始 grounding 文本(含 ``<ref>``/``<box>`` 标记)。
#   - ``level`` —— "Normal" / "Warning" 二选一。
#
# 与 ``TargetDoc`` 同源:继承 ``pydantic.BaseModel`` 而非 ``LMOutput`` —— 避
# 免 ``attachment: List[Attachment]`` 字段污染 ``model_json_schema()``,否则
# SDK 的 ``_safe_schema`` 返回空串,模型看不到字段语义。
def _build_grounding_doc_class(desc_desc: str):
    """Build the ``LocatorDoc`` schema lazily, once wdnai is importable.

    ``desc_desc`` —— 用户在「系统配置 → App 提示词模板」覆盖的
    ``desc`` 字段 ``Field(description=...)`` 文本;缺失时回退到
    ``DEFAULT_LOCATOR_DOC_DESC``。``boxes`` 字段的 description 是协议
    级格式规约("``BoundingBox`` 列表,每个 0–1000 归一化 4 坐标"),
    与 ``TargetDoc.bbox`` 一致保持 hardcoded 不开放给配置。

    **不能带 class docstring** —— pydantic 把 docstring 渲染到
    ``model_json_schema()`` 的顶层 ``description`` 字段,模型会把它当成
    数据字段包到 ``{"description": {...}}`` 里(详见 ``_build_target_doc_class``
    注释)。

    类型注解 ``boxes: List[BoundingBox]`` / ``level: Literal[...]`` 引用
    模块 globals 里的 ``_BoundingBox`` (``wdnai.grounding.BoundingBox``) 和
    ``_Literal`` (``typing.Literal``) —— pydantic 的 forward-ref resolver 用
    caller frame globals 找类型名,所以这些名字必须出现在模块 globals 命名
    空间里(``_BoundingBox`` 在模块顶部 import,``Literal`` 来自 typing
    标准库)。
    """
    from typing import List as _List

    _, _, _, _, _, Generable, Field, _, _, _ = _load_wdnai()

    from pydantic import BaseModel as _PydBaseModel

    @Generable
    class LocatorDoc(_PydBaseModel):
        boxes: "_List[_BoundingBox]" = Field(
            description=(
                "定位结果列表,每个元素是 ``BoundingBox(label, x1, y1, x2, y2)`` "
                "0–1000 归一化坐标(x1/y1 左上角,x2/y2 右下角)。"
            ),
        )
        text: str = Field(
            description=(
                "模型回显的原始 grounding 文本(含 ``<ref>label</ref><box>(x1,y1,x2,y2)</box>`` 标记)。"
            ),
        )
        level: "_Literal['Normal', 'Warning']" = Field(
            default="Normal",
            description="事件等级标签;常规观察为 ``Normal``,异常/风险提示为 ``Warning``。",
        )

    return LocatorDoc


def _get_grounding_doc_cls():
    """缓存 + 解析 ``LocatorDoc`` schema 类。"""
    descs = _load_app_prompts("video_target_localization", "LocatorDoc")
    cache_key = (
        "LocatorDoc", LOCATOR_DOC_SCHEMA_VERSION,
        tuple(sorted(descs.items())),
    )
    cached = _SCHEMA_CACHE.get(cache_key)
    if cached is not None:
        return cached
    cls = _build_grounding_doc_class(descs["desc"])
    _SCHEMA_CACHE[cache_key] = cls
    return cls


@dataclass
class DetectorEvent:
    """Public-facing event payload pushed to WS subscribers.

    The shape is what the frontend already expects (id / frame_pos / ts
    / prompt / text); the LMOutput instance is also attached so future
    consumers can pull ``attachment`` or other structured fields.

    ``tool_call`` is populated when the model selected the
    ``@Callable`` tool (``tts_alert`` currently);
    the front-end renders the event in the events list, with optional
    crop thumbnail + description text overlay.

    ``crop_b64`` / ``crop_w`` / ``crop_h`` are populated only for
    视频目标检测页 events (model returned ``TargetDoc``): the cropped
    JPEG bytes (base64) plus the crop pixel dimensions. Lives entirely
    in memory — not persisted to disk.

    ``desc`` carries the model-provided free-form description of the
    ROI; populated only for ``TargetDoc`` events. ``text`` is the same
    string (or the fallback "目标检测"), so legacy consumers that read
    ``text`` keep working.
    """
    id: int
    frame_pos: int
    ts: float
    prompt: str
    text: str
    level: Optional[str] = None
    video_ts: float = 0.0
    attachment_count: int = 0
    tool_call: Optional[str] = None
    bbox: Optional[list] = None
    crop_b64: Optional[str] = None
    crop_w: Optional[int] = None
    crop_h: Optional[int] = None
    desc: Optional[str] = None
    # ``True`` when the tool that produced this event is a pure
    # side-effect (e.g. ``tts_alert`` 播完了就是播完了，不再再让模型
    # wrap-up). detector 看到 ``tool_ev.consume_iteration`` 时直接跳出
    # multi-iter 循环，不再把 "你刚说了 X" 的 transcript 喂回下一轮
    # 避免模型退化到复读用户的提示词。
    consume_iteration: bool = False
    # **判据在这一帧上的分数**（`conc` 之类度量的原始值），手动跳帧模式下
    # 为 None（没做画面比对，没有分数可言）。
    #
    # 为什么必须记进事件：这是回答"为什么阈值 10 还会出事件"的唯一凭据。
    # 事件历史原来只有 frame_pos 和描述文本，用户盯着页面只能看到"又刷了一
    # 条"，分不清是**阈值触发**（score > threshold）、**兜底间隔触发**
    # （gap >= every_n 且 score >= threshold/2，**本来就允许阈值以下的帧**
    # 送检），还是压根没走判据。没有这个字段就只能猜，而这两者的处置完全
    # 不同：前者要调阈值，后者是兜底在正常工作、只是频率比预期高。
    judge_score: Optional[float] = None
    # 视频目标定位页 (LocatorDoc) 专用字段。一帧一事件，事件内 ``boxes``
    # 是本次定位到的全部目标 bbox 列表(可能为空 —— "未检测到")；
    # ``frame_b64`` + ``frame_w`` + ``frame_h`` 是整帧 JPEG base64,前端
    # 用原图 + bbox overlay 渲染(避免逐 box 多次 base64 编码传输)。
    # ``text`` 字段沿用历史展示路径 —— "label1 @ [x1,y1,x2,y2];
    # label2 @ [...]" 形式(或空列表时 "未定位到目标")。
    boxes: Optional[list] = None
    frame_b64: Optional[str] = None
    frame_w: Optional[int] = None
    frame_h: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "id":               self.id,
            "frame_pos":        self.frame_pos,
            "ts":               self.ts,
            "video_ts":         self.video_ts,
            "prompt":           self.prompt,
            "text":             self.text,
            "level":            self.level,
            "attachment_count": self.attachment_count,
            "tool_call":        self.tool_call,
            "bbox":             self.bbox,
            "crop_b64":         self.crop_b64,
            "crop_w":           self.crop_w,
            "crop_h":           self.crop_h,
            "desc":             self.desc,
            "consume_iteration": self.consume_iteration,
            "boxes":            self.boxes,
            "frame_b64":        self.frame_b64,
            "frame_w":          self.frame_w,
            "frame_h":          self.frame_h,
            "judge_score":      self.judge_score,
        }


# --------------------------------------------------------------------------- #
# 事件文本清洗：去除模型可能漏出的 chatml / vision / 特殊 token + 控制字符
# --------------------------------------------------------------------------- #
# 模型在 VLM 推理时偶尔会把内部 token（如 ``<|im_start|>`` /
# ``<|im_end|>`` / ``<|vision_start|>`` / ``<|image_pad|>``）漏出到
# 文本里，或在 JSON 解析失败回退到 raw text 时把整段半截 JSON（带
# 不闭合的 `{"bbox":...`）也塞进 ``DetectorEvent.text`` / ``desc``，
# 前端展示就成了"乱码"。这里统一过一遍再写到事件：
#
# 1. 去掉所有 ``<|...|>`` chatml 风格特殊 token
# 2. 去掉 ASCII 控制字符（保留 \n \r \t —— 这三种算"普通空白"）
# 3. 去掉 Unicode replacement char U+FFFD（解码失败留下的）
# 4. 折叠 3+ 连续换行为 2 个，避免事件历史里出现大段空行
# 5. 整体 strip
#
# 不做更激进的清洗（如去 emoji / 去 HTML 标签）—— 那些是合法内容。
_SANITIZE_CHATML_TOKEN = re.compile(r"<\|[^|]*\|>")
_SANITIZE_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SANITIZE_BLANK_LINES = re.compile(r"\n{3,}")
_DEGENERATE_VLM_TEXT = frozenset({"VLMDoc", "TargetDoc", "TTSResult"})
_TRAILING_PUNCT = re.compile(r"[，。！？!?,.;:：、\s]+$")


def _sanitize_event_text(s: Optional[str]) -> Optional[str]:
    """清洗模型/SDK 漏出的特殊 token 与控制字符。

    用于 ``DetectorEvent.text`` / ``DetectorEvent.desc`` 写入前过滤。
    返回 ``None`` 当且仅当输入是 ``None``；空字符串清洗后仍是空
    字符串（调用方自行决定是否替换为 "(空)" 之类的占位文本）。
    """
    if s is None:
        return None
    if not isinstance(s, str):
        s = str(s)
    s = _SANITIZE_CHATML_TOKEN.sub("", s)
    s = _SANITIZE_CONTROL.sub("", s)
    s = s.replace("\ufffd", "")
    s = _SANITIZE_BLANK_LINES.sub("\n\n", s)
    return s.strip()


def _strip_trailing_punct(s: str) -> str:
    return _TRAILING_PUNCT.sub("", s)


def _format_raw_text_for_log(text, label: str = "raw_text", limit: int = 1500) -> None:
    """Dump ``text`` as a multi-line, JSON-formatted LOG.error for debugging.

    用 ``%r`` 会把 ``\\n`` 转义成字面 ``\\\\n``,多行 JSON 全挤一行没法看。
    这里先尝试 ``json.loads`` + ``json.dumps(indent=2, ensure_ascii=False)``
    美化;非 JSON 原文输出;超过 ``limit`` 字符截断。两条独立 LOG 调用:
    一条 metadata + 一条 formatted text,后者换行缩进可直接看。
    """
    if not text:
        LOG.error("%s: (empty)", label)
        return
    try:
        _obj = json.loads(text)
        _formatted = json.dumps(_obj, indent=2, ensure_ascii=False)
    except Exception:
        _formatted = str(text)
    if len(_formatted) > limit:
        _formatted = (
            _formatted[:limit]
            + f"\n...[truncated {len(_formatted) - limit} chars]"
        )
    LOG.error("%s (formatted):\n%s", label, _formatted)


# 诊断 (v0.13): 应用层 TargetDoc rescue —— worker 是 thin RPC server
# (只把 client 传的 prompt / generating / tools 透传给 wdnai SDK,不做
# schema-specific 解析);SDK 在 ValidationError 时返回 raw text;client
# (本 detector) 负责从 raw text 里抢救结构化数据。
#
# wdnai SDK 对没 ``text`` 字段的 schema (TargetDoc) 在 JSON 解析失败时
# 会 fallback 到 raw text (见 ``session.py:_safe_validate``)。Qwen3.5-VL-4B
# 在 schema-constrained 模式下经常把 SDK 自动注入的 schema hint 复述为
# 输出,形如:
#   {"properties": {"x1": 590, "y1": 478, "x2": 830, "y2": 690, ...},
#    "required": [...], "title": "TargetDoc", "type": "object"}
# 这种形态 SDK 解析失败,返回 raw text。本函数从 ``TargetDoc.model_json_schema()``
# 动态知道 required 字段集,然后尝试在 raw_text 解析出的 dict (顶层或
# 嵌套 ``properties``) 里找到这些字段,``model_validate`` 成 TargetDoc 实例。
#
# 这是**应用层**逻辑,不是 SDK 内部 —— worker 不知道 TargetDoc 这个具体
# schema,所有 rescue 知识都在调用方 (detector) 这里。
def _try_rescue_target_doc(raw_text: str, target_doc_cls):
    """Try to rescue a TargetDoc instance from a raw-text model output.

    输入:模型返回的 raw text + TargetDoc class。
    输出:TargetDoc 实例 (校验通过) 或 None (无法 rescue)。

    尝试形式 (按优先级):
      1. 顶层 ``{bbox: [x1,y1,x2,y2], desc: ...}`` 标准 JSON 实例
         (v2 schema 形态, samples 同款)
      2. 顶层 ``{x1, y1, x2, y2, desc: ...}`` 老 schema 形态
         (v1 老 worker 仍可能返回这个) → 内部 convert 成 v2 后
         model_validate
      3. 嵌套 ``{properties: {...}, required, title, type}``:模型把
         schema 当模板填值 (Qwen3.5 误读 SDK 默认 schema_hint 时)
      4. 顶层部分字段 + 缺字段 → 仍尝试 model_validate 让 pydantic
         用默认值补齐

    失败返回 None,继续走原 VLMDoc fallthrough 路径。
    """
    if not raw_text or not isinstance(raw_text, str) or target_doc_cls is None:
        return None
    try:
        s = raw_text.strip()
        # 去掉 markdown fence (```json ... ```),如果模型意外加了
        if s.startswith("```"):
            lines = s.split("\n")
            cleaned = []
            in_fence = False
            for ln in lines:
                ln_stripped = ln.strip()
                if ln_stripped.startswith("```"):
                    in_fence = not in_fence
                    continue
                cleaned.append(ln)
            s = "\n".join(cleaned).strip()
        # extract_json 优先 (SDK helper,有 fence / 杂字符容忍)
        data = None
        try:
            from wdnai.utils import extract_json as _exj
            data = _exj(s)
        except Exception:
            try:
                data = json.loads(s)
            except Exception:
                return None
        if not isinstance(data, dict):
            return None
        # 从 schema 动态知道 required 字段集 (不强写 bbox / desc)
        try:
            schema_dict = target_doc_cls.model_json_schema()
        except Exception:
            schema_dict = {}
        required_fields = list(schema_dict.get("required") or [])
        if not required_fields:
            return None

        # Schema-version 兼容 (v1 → v2 过渡期间 worker 端可能仍是老
        # 4-field schema, model_dump 出来 {x1, y1, x2, y2, desc};代理
        # 端 cache 是新 v2 schema,期望 {bbox, desc})。rescue 时检测
        # 老 dict 形态并 convert 到新形态,保证 model_validate 通过。
        if "bbox" in required_fields and "bbox" not in data:
            old_xy = (data.get("x1"), data.get("y1"),
                      data.get("x2"), data.get("y2"))
            if all(isinstance(v, (int, float)) for v in old_xy):
                data = dict(data)  # 不修改原 dict
                data["bbox"] = [float(v) for v in old_xy]

        # 数字字段归一化:模型偶尔把 ``bbox`` 输出成 ``[60, 425]`` 长度 2 的
        # 数组(Qwen3.5-VL-4B 的 bbox 训练数据有时是 ``[x1, y1, x2, y2]``
        # 4-tuple 格式或 ``[x1, y1, x2]`` 3-tuple 格式,模型就照着输
        # 短版),pydantic model_validate 会按 list[float] 长度 4 校验失败。
        # v2 schema 期望 list[float] length==4, 长度不匹配时取最保守位置
        # (4-tuple → 保留所有;3-tuple → 仅保留前 3 维并补 0;
        # 2-tuple → 仅保留首 2 维并补 0)。
        def _flatten_bbox_list(v):
            if isinstance(v, (int, float)):
                return float(v)
            if isinstance(v, str):
                try:
                    return float(v.strip())
                except (TypeError, ValueError):
                    return None
            if isinstance(v, list):
                if len(v) == 4:
                    return [float(x) for x in v]
                if len(v) == 3:
                    return [float(v[0]), float(v[1]), float(v[2]), 0.0]
                if len(v) == 2:
                    return [float(v[0]), float(v[1]), 0.0, 0.0]
                if len(v) == 1 and v:
                    return [float(v[0]), 0.0, 0.0, 0.0]
                return None
            return None

        # v1 老 schema (4 个独立 float field x1/y1/x2/y2) 的数字字段
        # flatten —— Qwen3.5-VL-4B 训练数据有时把 4-tuple 摊开到 4 个
        # 字段 (x1=[60,425] 表示同一组 bbox 的 x1),启发式按字段位置
        # 取对应索引。
        def _flatten_xy_scalar(v, field: str = ""):
            if isinstance(v, bool):
                return int(v)
            if isinstance(v, (int, float)):
                return float(v)
            if isinstance(v, str):
                try:
                    return float(v.strip())
                except (TypeError, ValueError):
                    return None
            if isinstance(v, list):
                if len(v) == 4 and field in ("x1", "y1", "x2", "y2"):
                    pos = ("x1", "y1", "x2", "y2").index(field)
                    return _flatten_xy_scalar(v[pos])
                if v:
                    return _flatten_xy_scalar(v[0])
                return None
            return None

        def _try_validate(d: dict):
            """尝试 model_validate, 失败前先把数字字段 flatten 成期望形态。"""
            if not all(k in d for k in required_fields):
                return None
            try:
                fields_info = schema_dict.get("properties") or {}
                flat = {}
                for k in required_fields:
                    ftype = (fields_info.get(k) or {}).get("type", "")
                    if k == "bbox":
                        # v2 schema 期望 list[float] length 4
                        v = _flatten_bbox_list(d.get(k))
                        if v is None:
                            return None
                        flat[k] = v
                    elif k in ("x1", "y1", "x2", "y2"):
                        # v1 老 schema: 4 个独立 float field
                        v = _flatten_xy_scalar(d.get(k), field=k)
                        if v is None:
                            return None
                        flat[k] = v
                    elif ftype in ("number", "integer"):
                        v = _flatten_xy_scalar(d.get(k))
                        if v is None:
                            return None
                        flat[k] = v
                    elif ftype == "string":
                        v = d.get(k)
                        if not isinstance(v, str):
                            v = str(v) if v is not None else ""
                        flat[k] = v
                    else:
                        flat[k] = d.get(k)
                # 复制所有非 required 字段(如果有)
                for k, v in d.items():
                    if k not in flat:
                        flat[k] = v
                return target_doc_cls.model_validate(flat)
            except Exception:
                return None

        # 形式 1:顶层 dict 含全部 required (含 flatten)
        r = _try_validate(data)
        if r is not None:
            return r
        # 形式 2:properties 嵌套 (Qwen3.5 把 schema 当模板填值)
        props = data.get("properties")
        if isinstance(props, dict) and all(k in props for k in required_fields):
            r = _try_validate(props)
            if r is not None:
                return r
        # 形式 3:data 顶层有部分字段 (其他用 default)
        r = _try_validate(data)
        if r is not None:
            return r
        return None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 视频目标检测页专用：letterbox 尺寸选择 + bbox 区域裁切 + JPEG 编码 + base64 内嵌
# --------------------------------------------------------------------------- #
def _normalize_bbox_to_0_1000(bbox) -> Optional[List[float]]:
    """把模型返回的 bbox 统一归一化到 0–1000 坐标空间。

    真机 Qwen3-VL-4B / Qwen3.5-VL-4B 在 schema-constrained 输出模式下
    bbox 数值单位不稳定：
      - 大部分（约 70%）按 0–1000 范围输出（与 description 协议一致）
      - 部分（约 30%，768×768 变体下尤其频繁）按 0–1 范围小数输出

    0–1 范围 bbox 直接当 0–1000 处理会让 ``unletterbox_bbox`` 把 ROI 还原
    到原图左上角 ~0 像素 → 裁切为空 → 触发 "(目标检测失败: 裁切区域为
    空)" 错误事件。本函数做单位自适应归一化：

    **判别规则**（按优先级）：
      1. ``max(|bbox|) <= 1.5`` **且** 4 个值都 ≤ 1.0 → 视为 0–1 范围
         → ×1000 升到 0–1000
      2. 否则按 0–1000 处理，但 clamp 到 [0, 1000]
      3. 输入类型异常（None / 长度不对 / 全 None）→ 返回 None

    为什么用 1.5 而不是 1.0 当阈值：模型偶发输出 ``[1.0, 1.0, 1.0, 1.0]``
    这种全 1.0 bbox（恰好在边界），按 0–1 ×1000 = [1000, 1000, 1000, 1000]
    全画面与按 0–1000 clamp [1000, 1000, 1000, 1000] 结果一致，留一点余量
    让 ``max=1.5`` 这种"刚好越界"的 bbox 也走 ×1000 路径。
    """
    if bbox is None:
        return None
    if not isinstance(bbox, (list, tuple)):
        return None
    if len(bbox) != 4:
        return None
    try:
        nums = []
        for v in bbox:
            if v is None:
                return None
            try:
                nums.append(float(v))
            except (TypeError, ValueError):
                return None
    except Exception:  # noqa: BLE001
        return None

    abs_max = max(abs(v) for v in nums)
    # 0-1 范围判别: max <= 1.5 (含 1.0 边界) → ×1000
    if abs_max <= 1.5 and all(v <= 1.0 for v in nums):
        return [max(0.0, min(1000.0, v * 1000.0)) for v in nums]
    # 0-1000 范围: clamp 到 [0, 1000]
    return [max(0.0, min(1000.0, v)) for v in nums]


# "未检测到目标" 语义关键词 —— 模型在 TargetDoc 模式下可能 bbox 给一个
# 全画面（[0, 0, 1000, 1000]）但 desc 文本提示"未检测到" / "没有" /
# "无车" 等。这种自相矛盾的输出 detector 应该识别并跳过（不写 history、
# 不发 WS），避免把整个画面作为 ROI 裁切给用户看。
_NO_DETECTION_KEYWORDS = (
    "未检测到", "未检测", "没有", "没有检测到", "没有检测", "未发现",
    "未找到", "未识别到", "没有识别到", "没有找到", "画面中没有",
    "图中没有", "图像中没有", "图中未", "画面中未", "无车", "没有车",
    "不存在", "无目标", "没有目标",
)


def _is_no_detection_desc(desc: str) -> bool:
    """检测 ``desc`` 文本是否提示"未检测到目标"语义。

    触发时 detector 跳过该帧的事件写盘（不写 history、不发 WS），
    避免用户看到 bbox=[0,0,1000,1000] 全画面 + "未检测到车辆" 的自
    相矛盾事件。返回 True 时调用方应跳过事件构造。
    """
    if not desc or not isinstance(desc, str):
        return False
    s = desc.strip()
    if not s:
        return False
    return any(kw in s for kw in _NO_DETECTION_KEYWORDS)


def _pick_letterbox_size(
    image_size, available_sizes, h: int, w: int,
) -> int:
    """Decide which vision-variant size the script should pre-letterbox
    the input frame to.

    Mirrors ``wdnai.backends.vlm._pick_variant``'s auto-pick rule
    (closest by longest edge, ties broken by smaller) so that the
    backend always agrees with our script-side choice. When the user
    explicitly selected an ``image_size``, that value wins.

    Returns the chosen edge length as an int (one of ``available_sizes``
    when the list is non-empty, else a hard-coded fallback of ``768``
    which matches the largest currently-shipped variant).
    """
    if image_size is not None:
        try:
            return int(image_size)
        except (TypeError, ValueError):
            pass
    cleaned: List[int] = []
    for s in available_sizes or []:
        if isinstance(s, bool):
            continue
        try:
            cleaned.append(int(s))
        except (TypeError, ValueError):
            continue
    if not cleaned:
        return 768
    target = max(int(h), int(w))
    return min(cleaned, key=lambda s: abs(s - target))


def _crop_and_encode(
    frame_bgr: "np.ndarray",
    bbox_0_1000,
    meta,
    unletterbox_bbox_fn,
) -> tuple:
    """Crop ``frame_bgr`` at the given 0–1000 normalized bbox, JPEG-encode,
    base64 the bytes. Returns ``(b64_str, w, h)``. Empty / invalid bbox
    → ``("", 0, 0)``.

    ``bbox_0_1000`` is interpreted in the **letterboxed canvas's**
    coordinate space (the canvas that was sent to the VLM as
    ``Attachment``); ``meta`` is the :class:`LetterboxMeta` returned by
    ``letterbox_resize_with_meta`` and ``unletterbox_bbox_fn`` is the
    matching helper. Together they map the bbox back to original-frame
    pixels so the ROI has the source aspect ratio (no grey padding).

    OpenCV import is lazy: we only need it on TargetDoc invocation,
    and opencv-python-headless is a hard dep already used by the
    upload pipeline.
    """
    if frame_bgr is None or frame_bgr.ndim < 2:
        return "", 0, 0
    try:
        if not isinstance(bbox_0_1000, (list, tuple)) or len(bbox_0_1000) != 4:
            return "", 0, 0
    except TypeError:
        return "", 0, 0

    if unletterbox_bbox_fn is None or meta is None:
        # Defensive fallback: if the SDK helpers aren't available (older
        # wheel installed), fall back to the old "treat bbox as
        # original-frame 0-1000" math so detection still produces *some*
        # crop rather than crashing the frame loop.
        try:
            x1f, y1f, x2f, y2f = (float(v) for v in bbox_0_1000)
        except (TypeError, ValueError):
            return "", 0, 0
        h, w = frame_bgr.shape[:2]
        x1 = max(0, min(w, int(round(x1f / 1000.0 * w))))
        x2 = max(0, min(w, int(round(x2f / 1000.0 * w))))
        y1 = max(0, min(h, int(round(y1f / 1000.0 * h))))
        y2 = max(0, min(h, int(round(y2f / 1000.0 * h))))
    else:
        try:
            x1, y1, x2, y2 = unletterbox_bbox_fn(bbox_0_1000, meta)
        except (TypeError, ValueError):
            return "", 0, 0
        x1, x2 = int(round(x1)), int(round(x2))
        y1, y2 = int(round(y1)), int(round(y2))
        h, w = frame_bgr.shape[:2]
        x1 = max(0, min(w, x1))
        x2 = max(0, min(w, x2))
        y1 = max(0, min(h, y1))
        y2 = max(0, min(h, y2))

    if x1 > x2:
        x1, x2 = x2, x1
    if y1 > y2:
        y1, y2 = y2, y1
    if x2 - x1 <= 0 or y2 - y1 <= 0:
        return "", 0, 0

    crop = frame_bgr[y1:y2, x1:x2]

    import base64
    import cv2

    ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        return "", 0, 0
    return base64.b64encode(buf.tobytes()).decode("ascii"), crop.shape[1], crop.shape[0]

# --------------------------------------------------------------------------- #
class WdnaiDetector:
    """Single VLM session shared by the video-detection page.

    Uses the v2 SDK surface:

        result = await session.respond(
            prompt=prompt,
            attachment=[Attachment(frame)],     # list
            generating=VLMOutputCls,            # @Generable subclass — VLMDoc or TargetDoc
            tools=[AlarmCls, ...],
        )
        # result.text, result.level, result.attachment

    **两个工厂函数绑定不同的 ``generating`` schema**：

    * ``get_detector()`` —— 音视频描述页，用 ``VLMDoc``，可选挂
      ``@Callable`` 工具（如 ``tts_alert``）。
    * ``get_target_detector()`` —— 视频目标检测页，用 ``TargetDoc``
      （``x1, y1, x2, y2`` + ``desc``），**不**挂任何工具，由模型
      在结构化输出里直接给出 ROI 坐标 + 描述文本。

    一帧推理支持**多轮工具循环**（agent-loop）：模型可在同一帧内连
    续调多次 ``@Callable`` 工具（如 ``tts_alert``），每轮的"工具调
    用 + 返回"会以**文本 attachment** 形式回灌到下一轮的 prompt，
    直到模型返回 ``generating`` schema 的实例或循环上限命中。循环
    上限从 ``config.jsonc["compute"]["tool_call_max_iters"]`` 读取，
    默认 5；``0`` 表示无限循环（仍受 ``_ABSOLUTE_MAX_ITERS`` 硬上
    限兜底，防止失控模型锁死 worker）。``TargetDoc`` 模式没有工
    具，因此循环天然只跑一轮就跳出。
    """

    HISTORY_MAX = 200
    ITEM_QUEUE_MAX = 1  # latest-only — older frames are dropped
    # 工具调用循环的硬上限。即使 ``tool_call_max_iters=0``（无限）
    # 也会卡在这里 —— 兜底避免失控模型在同一帧里无限循环把 worker
    # 锁死。200 次 × 1s/inference ≈ 3 分钟单帧最坏延迟，足够 cover
    # 任何合理的多工具串联场景，又不至于把队列永久堵住。
    _ABSOLUTE_MAX_ITERS = 200
    # 默认循环上限（config.jsonc 缺失 / 类型不对 / 负值时使用）。
    _DEFAULT_MAX_ITERS = 5
    # 诊断 (v0.13): 保留最近 N 次推理的诊断信息 (worker 端 + proxy 端 +
    # 主进程侧的事件 trace),供 /diagnostics 端点 dump 给用户 / 调试。
    # 不持久化,只用于"刚刚发生了什么"的现场还原。
    _DIAG_HISTORY_MAX = 50
    # watchdog: _worker_loop 连续 N 秒没产出任何事件时 LOG.warning。
    # 注意 frame source 可能本来就不密集 (every_n=30 + 25fps = 1.2s/帧),
    # 所以阈值要放得足够宽(默认 120s = 100 帧 @ 30 fps @ every_n=30)。
    _WATCHDOG_IDLE_SECONDS = 120.0

    def __init__(self, *, generating_cls=None) -> None:
        """``generating_cls`` —— 该 detector 实例要传给
        ``session.respond(generating=...)`` 的 ``@Generable`` 子类。

        * ``None``（默认）→ ``VLMDoc``（音视频描述页 schema）
        * 显式 ``TargetDoc`` 类 → 视频目标检测页 schema
        * callable（无参函数 / lambda），返回上面的 schema 类——
          ``start()`` 里会调一次拿到真正的类，避免 worker 线程首次
          import 卡顿；工厂通常传这种。

        解析延迟到 ``start()``：wdnai 第一次 import 的失败路径在那里
        走 ``available`` 兜底。
        """
        self._lock = threading.Lock()
        self._enabled = False
        self._prompt = ""
        self._every_n = 30
        self._enabled_tools: List[str] = []
        self._frame_count = 0
        self._model_name = ""  # UI 仅作状态文案，已加载即已加载 —— 不展示
                                # 模型名（音视频描述 / 视频目标检测页都不暴露
                                # 当前模型 id 给前端）。
        self._generating_cls = generating_cls
        # 视觉模型目标尺寸 —— 用户从 ``#vd-detect-size`` 下拉框选
        # 的值。``None`` 表示"按图像分辨率自动选最近的变体"（leg‐
        # acy 行为）。在 ``start()`` 里校验后写入；``_infer_sync`` 透
        # 传给 ``sess.respond(image_size=...)``，backend 的
        # ``_pick_variant(image_size=...)`` 据此选择对应的 vision
        # 变体。
        self._image_size: Optional[int] = None
        # 缓存本实例对应的 schema 类，避免每次推理都重读缓存字典。
        # start() 里再赋值一次（要拿到已 build 好的类 ——
        # ``_get_vlm_doc_cls`` / ``_get_target_doc_cls`` 仅在首次
        # ``_load_wdnai()`` 成功后才返回有效类）。
        # LLMSession 不再由 detector 持有，统一在 ComputeRegistry 里管理。
        self._init_error = ""
        self._events: deque = deque(maxlen=self.HISTORY_MAX)
        self._events_lock = threading.Lock()
        self._latest: Optional[dict] = None
        self._next_event_id = 1
        self._item_q: "queue.Queue" = queue.Queue(maxsize=self.ITEM_QUEUE_MAX)
        self._worker: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._broadcaster_lock = threading.Lock()
        self._broadcasters: List[Callable[[dict], None]] = []
        # 诊断 (v0.13): 最近 N 次推理的诊断 trace, 每条形如:
        #   {
        #     "ts":          float,    # wall-clock 时间戳
        #     "frame_pos":   int,      # 来自 sink 的 frame_pos
        #     "it_infer_ms": float,    # 单次 _infer_sync 总耗时
        #     "schema":      str,      # generating_cls.__name__
        #     "result_type": str,      # worker 端 sess.respond 返回类型
        #     "text_len":    int,      # 模型输出文本长度 (clean 后)
        #     "text":        str,      # 完整清洗后文本 (不再截断,便于复盘)
        #     "exit_reason": str,      # vlm_doc / target_doc / cap_hit / tool_*
        #     "events":      int,      # 本帧产出事件数
        #     "bbox":        list|None,
        #     "crop_w":      int|None,
        #     "crop_h":      int|None,
        #     "schema_parse_failed": bool,
        #     "had_exception": bool,
        #     "exception_type": str|None,
        #     "exception_msg":  str|None,
        #     "raw_text_first": str,   # raw_text 前 500 字符,排查 SDK fallback
        #   }
        # ``deque(maxlen=N)`` 自动环形覆盖; ring buffer 让 ``start()`` /
        # 首次推理前为空,不影响业务逻辑。
        self._diag_history: deque = deque(maxlen=self._DIAG_HISTORY_MAX)
        self._diag_lock = threading.Lock()
        # watchdog: _worker_loop 上一次产出事件的时间戳。
        self._last_event_ts: float = 0.0
        # 选中的 swcap_video_describe 实例 model_id。
        # ``""`` (空) = auto-pick (跟 ``get_session`` 一致的本机优先规则)。
        # ``start(model_id=...)`` 时填入；``_infer_sync`` 每次拿 session
        # 都按这个值走,允许用户在不重启 worker 的前提下切换底层实例
        # (例如两个 qwen3-vl-4b 不同 NPU,或本机 vs LAN peer)。
        self._chosen_model_id: str = ""
        # ``_last_bbox_raw`` —— ``_build_target_event`` 把模型原始
        # bbox（未归一化）写入此处；``_infer_sync`` 在写 _diag_history
        # 前读出 → 给 ``bbox_raw`` 字段，排查"模型到底输出 0-1 还是
        # 0-1000" 时直接看诊断端点。线程安全由 _worker_loop 单线
        # 程串行保证。
        self._last_bbox_raw: Optional[list] = None


    # ------------------------------------------------------------------ #
    @property
    def available(self) -> bool:
        ok, *_ = _load_wdnai()
        return ok

# ------------------------------------------------------------------ #
    def _required_capability(self) -> str:
        """Return the ``ComputeRegistry`` capability this detector needs.

        Capability 分流规则(M4 起多 capability schema 共存):

        * ``LocatorDoc`` —— 视频目标定位页 —— 走 ``swcap_grounding``
          (LocateAnything-3b 视觉定位模型)。判别靠 schema 类名,LocatorDoc
          在 ``_get_grounding_doc_cls`` 创建后 ``__name__`` 不变,稳定性靠
          schema-version 在 ``_SCHEMA_CACHE`` 强制 rebuild 兜底。
        * 其它 (``VLMDoc`` / ``TargetDoc``) —— 走 ``swcap_video_describe``
          (qwen3-vl-4b / qwen3.5-vl-4b 通用 VLM),默认分支覆盖两个老
          detector 单例。

        调用方:
          * ``snapshot()`` — 按本页 schema 决定查哪个 capability,避免
            视频目标定位页在 user 已加载 ``swcap_grounding`` 但未加载
            ``swcap_video_describe`` 时,被错误地标"功能未启用:
            swcap_video_describe"。
          * ``start()`` — 同样的 capability 校验,503 透传。

        返回常量字符串;**绝不**触网 / 触 NPU —— 纯 schema-class 查表,
        调用安全 (snapshot 1Hz WS / 页面加载 / ``start()`` 三处共用)。

        **Factory 解析细节**:``__init__`` 把 ``generating_cls`` 原样存
        进 ``self._generating_cls``(``_get_target_doc_cls`` /
        ``_get_grounding_doc_cls`` 工厂函数,**不**立即调,或 ``None``)。
        ``start()`` 会先 resolve 成真类再 capability 校验,但 ``snapshot()``
        在 ``start()`` 之前就会被 1Hz WS / 页面加载触发 —— 直接读
        ``self._generating_cls.__name__`` 拿到 ``"_get_grounding_doc_cls"``
        而非 ``"LocatorDoc"``,命中不到目标分支,bug 依旧。这里在读
        ``__name__`` 前先调一次工厂拿到真类(失败兜底 ``None``,默认
        走 swcap_video_describe)。
        """
        cls = self._generating_cls
        if callable(cls) and not isinstance(cls, type):
            try:
                cls = cls()
            except Exception:  # noqa: BLE001
                cls = None
        if getattr(cls, "__name__", "") == "LocatorDoc":
            return "swcap_grounding"
        return "swcap_video_describe"

    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict:
        from app.core.compute.registry import get_registry
        registry = get_registry()
        capability = self._required_capability()
        try:
            sess = registry.get_session(capability)
            session_ready = sess is not None
            session_error = ""
        except RuntimeError as e:
            sess = None
            session_ready = False
            session_error = str(e)
        # Query the running VLM instance for its loaded vision-model
        # native sizes — never hardcode [384, 768] on the client.
        # Filter by capability instead of model_id so any future VLM
        # preset (qwen3.5-vl-4b etc.) is picked up automatically.
        # Empty list when the worker hasn't reported yet (model not
        # loaded, network probe failure, missing capability).
        #
        # ``available_image_sizes`` 是 ``swcap_video_describe`` VLM 概
        # 念(只有 VLM 实例会上报);视频目标定位页走 ``swcap_grounding``
        # —— 该 capability 实例不暴露 ``available_image_sizes`` 字段,
        # 即使循环也会落空。直接按 capability != swcap_video_describe
        # 短路,``available_sizes`` 留空数组,前端下拉框按现有契约显示
        # 空状态。
        available_sizes: List[int] = []
        if capability == "swcap_video_describe" and sess is not None:
            try:
                for inst in registry.list_with_source():
                    caps = inst.get("capability_types") or []
                    if "swcap_video_describe" not in caps:
                        continue
                    raw = inst.get("available_image_sizes") or []
                    if isinstance(raw, list):
                        cleaned = []
                        for s in raw:
                            if isinstance(s, bool):
                                continue
                            try:
                                cleaned.append(int(s))
                            except (TypeError, ValueError):
                                continue
                        available_sizes = sorted(set(cleaned))
                    break
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            return {
                "available":              self.available,
                "init_error":             session_error or self._init_error,
                "enabled":                self._enabled,
                "prompt":                 self._prompt,
                "every_n":                self._every_n,
                "model":                  "",  # UI 不再展示模型名，仅作
                                                # snapshot shape 兼容位
                "session_ready":          session_ready,
                "worker_alive":           self._worker is not None and self._worker.is_alive(),
                "queue_size":             self._item_q.qsize(),
                "enabled_tools":          list(self._enabled_tools),
                # 数据驱动：UI ``#vd-detect-size`` 下拉框从这里读。
                # Empty list when qwen3-vl-4b instance isn't running
                # yet or hasn't reported its loaded variants — UI
                # should grey out / disable the dropdown in that case.
                "available_image_sizes":  available_sizes,
                # 当前 session 实际选定的变体（来自最近一次 ``start``
                # 调用的 ``image_size``）。None 表示 auto-pick by
                # image resolution（legacy behaviour preserved）。
                "image_size":             self._image_size,
                # 当前选定的 model_id ("", "qwen3-vl-4b", "qwen3.5-vl-4b"...)。
                # 空字符串 = auto-pick,前端用此判定下拉框是否用户自定义过。
                "model_id":               self._chosen_model_id,
            }

    # ------------------------------------------------------------------ #
    def start(self, prompt: str, every_n: int = 30,
              enabled_tools: Optional[List[str]] = None,
              image_size: Optional[int] = None,
              model_id: Optional[str] = None) -> dict:
        if not self.available:
            raise RuntimeError("wdnai 不可用")
        if not (prompt or "").strip():
            raise ValueError("prompt 不能为空")
        # model_id：用户从下拉框选的 swcap_video_describe 实例。
        # ``None``（默认）→ 走 ``get_session`` 的本机优先 auto-pick，
        # 与旧行为一致。显式传入时校验该实例正在 running + 暴露
        # ``swcap_video_describe`` + 给出对应的 RpcSessionProxy，失败
        # 抛 RuntimeError 让 HTTP 层 503 透传。
        from app.core.compute.registry import get_registry
        registry = get_registry()
        if model_id is not None:
            if not isinstance(model_id, str) or not model_id.strip():
                raise ValueError(f"model_id 必须是字符串或 None: {model_id!r}")
            try:
                registry.get_session_for_model("swcap_video_describe", model_id)
            except RuntimeError:
                raise
            # 记录选中的 model_id，``_infer_sync`` 取 proxy 时再次校验
            # (防止用户在 running 列表变化时仍用旧选择 → 服务层兜底)。
            chosen_model_id = model_id.strip()
        else:
            chosen_model_id = ""
        # image_size 校验：必须是 qwen3-vl-4b 实例实际加载的某个
        # vision 变体尺寸。``None`` 表示"按图自动选"（legacy 行
        # 为）。Backend 的 ``_pick_variant(image_size=...)`` 在不匹
        # 配时也会兜底取 nearest 并 warn —— 但前端下拉框只暴露合
        # 法值，所以这里严格匹配更友好。
        if image_size is not None:
            if not isinstance(image_size, int) or isinstance(image_size, bool):
                raise ValueError(
                    f"image_size 必须是整数或 None: {image_size!r}"
                )
            if image_size <= 0:
                raise ValueError(
                    f"image_size 必须 > 0: {image_size}"
                )
            from app.core.compute.registry import get_registry
            registry = get_registry()
            available_sizes: List[int] = []
            try:
                for inst in registry.list_with_source():
                    caps = inst.get("capability_types") or []
                    if "swcap_video_describe" not in caps:
                        continue
                    # 用户选过 model_id 时,只对那个实例校验;
                    # 否则取任意 swcap_video_describe running 实例
                    # (auto-pick 与 get_session 一致)。
                    if (chosen_model_id
                            and inst.get("model_id") != chosen_model_id):
                        continue
                    raw = inst.get("available_image_sizes") or []
                    if isinstance(raw, list):
                        for s in raw:
                            try:
                                available_sizes.append(int(s))
                            except (TypeError, ValueError):
                                continue
                    break
            except Exception:  # noqa: BLE001
                pass
            if available_sizes and image_size not in available_sizes:
                raise ValueError(
                    f"image_size={image_size} 不在模型已加载的视觉变体列表 "
                    f"{available_sizes} 中；可选值：{available_sizes}"
                )
        # 工具名校验放在最前面：未知 key → 400（与 capability
        # 区分开）。这一步独立于 VLM 是否可用，纯字符串 / 注册表
        # 校验。
        from app.core.tools.registry import get_tool_registry
        reg = get_tool_registry()
        tool_keys: List[str] = []
        for key in enabled_tools or []:
            if not isinstance(key, str):
                raise ValueError(f"enabled_tools 项必须是字符串: {key!r}")
            if not reg.has(key):
                raise ValueError(f"未知工具: {key!r}")
            tool_keys.append(key)
        # Build the structured-output class now (lazy) so that any
        # import failure surfaces here instead of inside the worker.
        # 该 detector 实例的 schema 在 ``__init__`` 时记录，未指定
        # 时回退到 ``VLMDoc``（音视频描述页）。视频目标检测页实例会
        # 显式传 ``TargetDoc`` / ``LocatorDoc`` 进来。
        #
        # **注意**:schema 解析放在 capability 校验**之前** —— 不同
        # schema 对应不同 capability（``VLMDoc``/``TargetDoc`` 走
        # ``swcap_video_describe``,``LocatorDoc`` 走 ``swcap_grounding``),
        # 必须先知道 schema class 才能选正确 capability。
        if self._generating_cls is None:
            self._generating_cls = _get_vlm_doc_cls()
        elif callable(self._generating_cls) and not isinstance(self._generating_cls, type):
            # 工厂通常传 ``_get_target_doc_cls`` 这种 callable —— 调
            # 用一次拿到真正的 schema 类，避开 worker 线程里首次
            # build 的卡顿。**注意**：pydantic model 类也是 callable
            # （``VLMDoc()`` 会触发实例化校验），所以必须先排除
            # ``isinstance(_, type)``，否则已经缓存的 class 会被当成
            # 工厂再调一次，撞到 ``text`` 必填字段抛 ValidationError，
            # 让第二次 POST /prompt 拿 400。
            self._generating_cls = self._generating_cls()
        # 验证类型 —— ``isinstance(cls, type)`` 排除残留的 None /
        # lambda 之类，失败抛 ValueError 让 HTTP 层 400。
        if not isinstance(self._generating_cls, type):
            raise ValueError(
                f"generating_cls 解析后不是类: {self._generating_cls!r}"
            )
        # 业务级校验：算力是否就绪。这一步失败 → 503 / RuntimeError，
        # 前端能区分"功能未启用"和"运行时错误"。
        #
        # **多 capability schema 分流**(M4 引入):
        # ``VLMDoc`` / ``TargetDoc`` 走 ``swcap_video_describe``,
        # ``LocatorDoc`` 走 ``swcap_grounding`` (LocateAnything-3b
        # 视觉定位模型)。通过 ``self._required_capability()`` 查表 ——
        # 与 ``snapshot()`` 同源,保证 1Hz WS 状态帧 + POST /prompt
        # 503 详情使用同一 capability 字符串,前端不会看到前后矛盾的
        # "功能未启用: ..."。
        from app.core.compute.registry import get_registry
        try:
            get_registry().get_session(self._required_capability())
        except RuntimeError as e:
            raise RuntimeError(str(e))
        # 工具依赖的 capability 校验（双层防护第二层）
        for key in tool_keys:
            spec = reg.get(key)
            if spec.requires_capability:
                try:
                    get_registry().get_session(spec.requires_capability)
                except RuntimeError as e:
                    raise RuntimeError(str(e)) from e
        with self._lock:
            self._enabled = True
            self._prompt = prompt
            self._every_n = max(1, int(every_n))
            self._enabled_tools = tool_keys
            self._image_size = image_size  # None → backend auto-pick
            self._chosen_model_id = chosen_model_id
            self._frame_count = 0
        if self._worker is None or not self._worker.is_alive():
            self._stop_event.clear()
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="wdnai-detect",
                daemon=True,
            )
            self._worker.start()
        return self.snapshot()

    def stop(self) -> dict:
        with self._lock:
            self._enabled = False
        try:
            self._item_q.put_nowait(None)
        except queue.Full:
            pass
        # 清空残留的 None sentinel 与过期帧,避免下次 start() 新 worker
        # 立即拿到 None 退出,造成 "启动定位后 worker_loop 0 秒死" 的隐性
        # 失败 (前后端都不报错但 history / bbox 全无)。
        try:
            while True:
                self._item_q.get_nowait()
        except queue.Empty:
            pass
        return self.snapshot()

    def close(self) -> None:
        """Stop the worker — the LLMSession is owned by ComputeRegistry,
        not us, so we don't close it here. Registry.close_all() handles
        that on app shutdown."""
        self.stop()

    # ------------------------------------------------------------------ #
    def submit_frame(
        self,
        frame_bgr: "np.ndarray",
        frame_pos: int,
        frame_b64: str = "",
        judge_score: Optional[float] = None,
    ) -> None:
        with self._lock:
            if not (self._enabled and self._prompt):
                return
            self._frame_count += 1
            if self._frame_count % self._every_n != 0:
                return
            prompt = self._prompt
        try:
            while True:
                self._item_q.get_nowait()
        except queue.Empty:
            pass
        try:
            self._item_q.put_nowait(
                (frame_bgr, frame_pos, prompt, frame_b64, judge_score),
            )
        except queue.Full:
            pass

    # ------------------------------------------------------------------ #
    def events(self) -> list:
        with self._events_lock:
            return [ev.to_dict() if hasattr(ev, "to_dict") else dict(ev) for ev in self._events]

    def latest(self) -> Optional[dict]:
        with self._events_lock:
            return dict(self._latest) if self._latest else None

    def clear(self) -> None:
        with self._events_lock:
            self._events.clear()
            self._latest = None

    def events_since(self, since_id: int) -> list:
        with self._events_lock:
            out = []
            for ev in self._events:
                d = ev.to_dict() if hasattr(ev, "to_dict") else dict(ev)
                if d["id"] > since_id:
                    out.append(d)
            return out

    def register_broadcaster(self, fn: Callable[[dict], None]) -> None:
        with self._broadcaster_lock:
            self._broadcasters.append(fn)

    def unregister_broadcaster(self, fn: Callable[[dict], None]) -> None:
        with self._broadcaster_lock:
            try:
                self._broadcasters.remove(fn)
            except ValueError:
                pass

    def _broadcast(self, event: dict) -> None:
        with self._broadcaster_lock:
            bs = list(self._broadcasters)
        for fn in bs:
            try:
                fn(event)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    def _infer_sync(
        self,
        frame_bgr: "np.ndarray",
        prompt: str,
        frame_pos: int = -1,
        frame_b64: str = "",
    ) -> List[DetectorEvent]:
        """Run a multi-turn tool loop on one frame; return a list of events.

        每帧开头 ``sess.reset()`` 清一次 KV cache（保留原帧间隔离语
        义）；之后循环 ``sess.respond``：

        * 模型返回 ``@Callable`` 实例 → 执行 ``result(frame_bgr, ctx)``
          拿副作用 dict → 构造 ``DetectorEvent`` → 把"调用 + 返回"
          作为文本 attachment 喂回下一轮 prompt → 继续循环。
        * 模型返回该 detector 实例配置的 ``generating`` schema
          （``VLMDoc`` 或 ``TargetDoc``）→ 构造 ``DetectorEvent`` →
          跳出循环：
          - ``VLMDoc``：取 ``text`` / ``level``；
          - ``TargetDoc``：取 ``x1, y1, x2, y2, desc``，按
            ``unletterbox_bbox`` 把 letterboxed 坐标映射回原图像素
            后裁原图 + JPEG 编码 + base64 内嵌进 ``crop_b64``，
            ``desc`` 复制到 ``text``。
        * 工具抛异常 → 构造 error/disabled ``DetectorEvent`` → **直接**
          跳出循环（按设计：异常立即报 error，不再继续喂回 prompt）。
        * 命中循环上限（``compute.tool_call_max_iters``，默认 5）→ 跳出
          循环；``0`` 视为无限，仍受 ``_ABSOLUTE_MAX_ITERS`` 兜底。
        ``TargetDoc`` 模式 ``enabled_tools`` 为空，因此循环天然只跑
        一轮就跳出。

        返回 ``List[DetectorEvent]`` —— 一帧可产出多个事件
        （多次工具调用 + 一次 schema 实例），由 ``_worker_loop`` 分配
        连续 id 并依次广播。
        """
        # 诊断 (v0.13): 入口日志 + diag history 占位 (耗时 / exit_reason /
        # text / bbox / exception 等 end-of-call 时填充)。frame_pos 由
        # _worker_loop 调用时再写入,这里拿到的是 -1 占位。
        _diag_rec: dict = {
            "ts": time.time(),
            "frame_pos": -1,
            "it_infer_ms": 0.0,
            "schema": getattr(self._generating_cls, "__name__", "?") if self._generating_cls else "?",
            "result_type": "?",
            "text_len": 0,
            "text": "",
            "exit_reason": "none",
            "events": 0,
            "bbox": None,
            "crop_w": None,
            "crop_h": None,
            "schema_parse_failed": False,
            "had_exception": False,
            "exception_type": None,
            "exception_msg": None,
            "raw_text_first": "",
        }
        _t0_infer = time.monotonic()
        # Per-frame capability must route through _required_capability() —
        # hardcoding "swcap_video_describe" leaks that string into LocatorDoc
        # disabled-events (text="(功能未启用: swcap_video_describe)") which the
        # user sees in the VTL events history.
        _required_cap = self._required_capability()
        try:
            from app.core.compute.registry import get_registry
            _registry = get_registry()
            if self._chosen_model_id:
                sess = _registry.get_session_for_model(
                    _required_cap, self._chosen_model_id)
            else:
                sess = _registry.get_session(_required_cap)
        except RuntimeError as e:
            LOG.warning(
                "frame infer: capability disabled (%s): %s",
                _required_cap, e,
            )
            _diag_rec.update(
                it_infer_ms=(time.monotonic() - _t0_infer) * 1000.0,
                exit_reason="disabled",
                had_exception=True,
                exception_type="RuntimeError",
                exception_msg=str(e),
            )
            self._append_diag(_diag_rec)
            return [DetectorEvent(
                id=-1, frame_pos=-1, ts=time.time(),
                prompt=prompt, text=f"({e})",
                level="disabled", attachment_count=0,
            )]

        with self._lock:
            enabled_tools = list(self._enabled_tools)
            # 本实例的 generating schema；start() 已经解析过
            # （None 时回退到 ``VLMDoc``）。
            generating_cls = self._generating_cls
            # 用户在 ``#vd-detect-size`` 选的尺寸；``None`` → backend
            # 走 legacy auto-pick（按图最大边最近匹配）。start() 已校
            # 验过合法性，这里只读不写。
            image_size = self._image_size

        try:
            (
                ok, Attachment, _sess_cls, _model_cls, _lm_cls, _gen_cls,
                _field_cls, letterbox_resize_with_meta, unletterbox_bbox, wdnai_err,
            ) = _load_wdnai()
            if generating_cls is None:
                # 兜底：start() 之前没跑过（实际不会发生——start() 第一
                # 件事就是解析 schema，这里只是防御性 null-check）。
                generating_cls = _get_vlm_doc_cls()
            TargetDoc = (
                _get_target_doc_cls()
                if generating_cls is not _get_vlm_doc_cls()
                else None
            )
            LocatorDoc = (
                _get_grounding_doc_cls()
                if generating_cls is not _get_vlm_doc_cls()
                else None
            )

            from app.core.tools.registry import get_tool_registry
            reg = get_tool_registry()
            tool_classes = [reg.get_class(k) for k in enabled_tools] if enabled_tools else None

            sess.reset()

            max_iters_cfg = self._get_tool_call_max_iters()
            # 安全规则：用户显式设值用 ``min(cfg, _ABSOLUTE_MAX_ITERS)``，
            # ``0``（无限）退化为硬上限 —— 任何路径都不能让 worker 在
            # 单帧里跑超过 _ABSOLUTE_MAX_ITERS 次，避免失控模型锁死
            # 队列或用户误填超大值导致前端无响应。
            effective_max = (
                min(max_iters_cfg, self._ABSOLUTE_MAX_ITERS)
                if max_iters_cfg > 0
                else self._ABSOLUTE_MAX_ITERS
            )

            h, w = frame_bgr.shape[:2]
            from app.core.tools.registry import ToolContext
            ctx = ToolContext(width=w, height=h, prompt=prompt)

            # ---- script-side pre-letterbox ---------------------------------
            # 模型看到的 image 必须是一个 letterboxed 正方形（vision RKNN
            # 训练 / 导出只接受方形）。脚本侧做 letterbox（而不是让
            # backend 在 worker 里做）有两个收益：
            #   1. backend 返回的 bbox 一定在已知 letterboxed 坐标空间里，
            #      配合 ``unletterbox_bbox(meta)`` 能精确还原到原图像素；
            #      旧的“直接拿 0-1000 当原图坐标”路径在 16:9 帧 +
            #      768×768 变体上会系统性偏 ~50px（参见 start() 里
            #      的 0-1→0-1000 hack，正是这个偏的征兆）。
            #   2. 主进程这边就能拿到 ``LetterboxMeta``，传给
            #      ``_crop_and_encode``，让 JPEG 缩略图按原图宽高比裁，
            #      不会带 letterbox 灰边进 UI 缩略图。
            # ``image_size`` 用户显式指定 → 用之；否则按 backend
            # ``_pick_variant`` 的 auto 规则（最长边最近匹配）选一个，
            # 然后把同一个 ``target_size`` 透传给 ``sess.respond``，让
            # backend 走我们选中的变体，bbox 坐标空间与 meta 严格对齐。
            try:
                from app.core.compute.registry import get_registry
                available_sizes: List[int] = []
                for inst in get_registry().list_with_source():
                    if "swcap_video_describe" not in (inst.get("capability_types") or []):
                        continue
                    for s in (inst.get("available_image_sizes") or []):
                        if isinstance(s, bool):
                            continue
                        try:
                            available_sizes.append(int(s))
                        except (TypeError, ValueError):
                            continue
                    break
            except Exception:  # noqa: BLE001
                available_sizes = []
            target_size = _pick_letterbox_size(
                image_size, available_sizes, h, w,
            )
            if letterbox_resize_with_meta is not None:
                try:
                    frame_attachment = Attachment(
                        letterbox_resize_with_meta(frame_bgr, target_size)[0]
                    )
                    lbox_meta = letterbox_resize_with_meta(frame_bgr, target_size)[1]
                except Exception as e:  # noqa: BLE001
                    LOG.warning(
                        "frame infer: letterbox failed (%s) — falling back "
                        "to raw frame attachment", e,
                    )
                    frame_attachment = Attachment(frame_bgr)
                    lbox_meta = None
            else:
                LOG.warning(
                    "frame infer: wdnai.utils.letterbox_resize_with_meta "
                    "missing (%s) — bbox→pixel mapping may drift", wdnai_err,
                )
                frame_attachment = Attachment(frame_bgr)
                lbox_meta = None
            events: List[DetectorEvent] = []
            tool_transcripts: List[str] = []

            LOG.info(
                "frame infer start: schema=%s tools=%s max_iters(cfg=%s, effective=%s) prompt=%r",
                generating_cls.__name__,
                enabled_tools or "(none)",
                max_iters_cfg,
                effective_max,
                prompt,  # 完整 prompt, 不再截断,便于复盘
            )

            exit_reason = "none"
            last_result_type: Optional[str] = None
            last_raw_text: str = ""
            for i in range(effective_max):
                # attachments: 第一轮只放帧，后续轮追加文本 transcript。
                attachments = [frame_attachment]
                if tool_transcripts:
                    blob = (
                        "# Prior tool calls in this turn\n"
                        "You have already called the following tools in this turn. "
                        "You may call another tool, or emit the structured response "
                        "(`generating=` schema). Do NOT repeat an identical call "
                        "verbatim — vary the arguments or finalize.\n\n"
                        + "\n\n".join(tool_transcripts)
                    )
                    attachments.append(Attachment(blob, kind="text"))

                iter_prompt = prompt if i == 0 else None

                LOG.debug(
                    "frame iter %d/%d: sess.respond(tools=%s, with_transcript=%s)",
                    i + 1,
                    effective_max,
                    [c.__name__ for c in (tool_classes or [])],
                    bool(tool_transcripts),
                )
                coro = sess.respond(
                    prompt=iter_prompt,
                    attachment=attachments,
                    generating=generating_cls,
                    tools=tool_classes,
                    # 始终传 ``target_size``（与脚本侧 letterbox 的
                    # canvas 边长严格一致），让 backend ``_pick_variant``
                    # 选我们预先 letterbox 好的那个变体 —— bbox 坐标空
                    # 间与 ``LetterboxMeta`` 才能对齐。否则 backend 自
                    # 作主张切到另一个变体会让 bbox→pixel 映射整体偏移
                    # 几十像素（之前 16:9 帧 + 768×768 路径上的
                    # 0-1→0-1000 hack 就是这个偏移的征兆）。
                    image_size=target_size,
                )
                result = asyncio.run(coro)
                last_result_type = type(result).__name__
                # 完整 raw text, 不再截断 —— 排查 SDK fallback 时 [:120]
                # 看不清是 JSON 半截还是空字符串。Log 端用 LOG.warning
                # 接收完整字符串。
                last_raw_text = _result_raw_text(result)

                # 诊断：result 类型是 SDK / proxy 路径的核心信号。任何
                # "推理 2 次后停"的问题几乎都跟 result 类型与生成
                # schema / tool 不匹配有关。下面所有分支的 fallback 日志
                # 都依赖这个 type name。
                try:
                    _VLMDocCls = _get_vlm_doc_cls()
                    _TargetDocCls = _get_target_doc_cls()
                except Exception:  # noqa: BLE001
                    _VLMDocCls = _TargetDocCls = None
                LOG.info(
                    "frame iter %d: post-respond result_type=%s "
                    "is_vlm_doc=%s is_target_doc=%s is_tts_alert=%s "
                    "raw_text_len=%d schema_parse_failed=%s",
                    i + 1,
                    type(result).__name__,
                    _VLMDocCls is not None and isinstance(result, _VLMDocCls),
                    _TargetDocCls is not None and isinstance(result, _TargetDocCls),
                    bool(tool_classes) and isinstance(result, tuple(tool_classes)),
                    len(last_raw_text),
                    _result_schema_parse_failed(result, generating_cls),
                )

                # ------------------------------------------------------------------ #
                # 应用层 rescue —— wdnai SDK 的 ``_safe_validate`` 在               #
                # ValidationError 时返回 raw text string 而不是抛异常；Qwen3.5-VL-4B#
                # 经常把 SDK 自动注入的 schema hint 复述为输出(形如                 #
                # ``{properties: {x1: 590, ...}, ...")``)导致校验失败。              #
                # worker 是 thin RPC server,不应该做 schema-specific 解析 ——      #
                # 由 client (本 detector) 应用层处理。                             #
                # ------------------------------------------------------------------ #
                # duck-type check:不看是不是 ``str``(那是 SDK 的既有行为,        #
                # 任何解析失败都是它),而是看"没有 .text 属性且不是 TargetDoc      #
                # 实例" —— 后者兼容仍在用包裹对象的老会话。
                if (TargetDoc is not None
                        and not isinstance(result, TargetDoc)
                        and getattr(result, "text", None) is None
                        and isinstance(last_raw_text, str)
                        and last_raw_text):
                    _rescued = _try_rescue_target_doc(
                        last_raw_text, TargetDoc,
                    )
                    if _rescued is not None:
                        LOG.info(
                            "frame iter %d: RESCUED TargetDoc from raw "
                            "text (model output didn't match schema, "
                            "extracted bbox/desc)",
                            i + 1,
                        )
                        result = _rescued

                # ---- tool branch -------------------------------------------
                tool_ev, is_fatal = self._run_tool_side_effect(
                    frame_bgr, ctx, result, prompt, tool_classes,
                )
                if tool_ev is not None:
                    events.append(tool_ev)
                    LOG.info(
                        "frame iter %d: tool_call=%s is_fatal=%s level=%s",
                        i + 1,
                        tool_ev.tool_call,
                        is_fatal,
                        tool_ev.level,
                    )
                    # 工具失败 → 直接跳出（按设计：异常 → error → 不再喂回）。
                    if is_fatal:
                        exit_reason = f"tool_error[{tool_ev.tool_call}]"
                        break
                    # 纯 side-effect 工具（tts_alert 等）成功后跳出循环：
                    # 它们的 effect 本身就是目的，不再让模型 wrap-up 一段
                    # VLMDoc —— 那个 wrap-up 反而会让模型复读自己的工具
                    # arguments（"你刚说了 X"→下一轮 text="X"→退化）。
                    if tool_ev.consume_iteration:
                        exit_reason = (
                            f"tool_consumed[{tool_ev.tool_call}]"
                        )
                        break
                    # 默认：写 transcript 进下一轮 prompt，继续循环。
                    tool_transcripts.append(
                        self._format_tool_transcript(result, tool_ev)
                    )
                    continue

                # ---- TargetDoc branch --------------------------------------
                # 视频目标检测页：模型直接给出 x1, y1, x2, y2 + desc。
                # 后端按 bbox 裁原图 + JPEG 编码 + base64 内嵌到事件
                # JSON，``desc`` 复制到 ``text`` 字段。事件历史列表沿
                # 用现有展示路径。
                if TargetDoc is not None and isinstance(result, TargetDoc):
                    target_ev = self._build_target_event(
                        result, frame_bgr, prompt, lbox_meta, unletterbox_bbox,
                    )
                    if target_ev is None:
                        # _build_target_event 返回 None = "未检测到目标"
                        # 语义,detector 跳过该事件写盘 (不广播、不写
                        # _events, 但仍标记 target_doc exit_reason 让
                        # watchdog 知道本帧跑完了工具循环)。
                        LOG.info(
                            "frame iter %d: TargetDoc skipped (no-detection "
                            "semantic from bbox+desc)",
                            i + 1,
                        )
                        exit_reason = "target_doc_skipped"
                        break
                    events.append(target_ev)
                    bbox = target_ev.bbox or [0.0, 0.0, 0.0, 0.0]
                    LOG.info(
                        "frame iter %d: TargetDoc bbox=[%.0f,%.0f,%.0f,%.0f] crop=%dx%d",
                        i + 1,
                        bbox[0], bbox[1], bbox[2], bbox[3],
                        target_ev.crop_w or 0,
                        target_ev.crop_h or 0,
                    )
                    exit_reason = "target_doc"
                    break

                # ---- LocatorDoc branch (grounding multi-bbox) ---------------
                # 视频目标定位页：模型返回 ``LocatorDoc`` (``boxes`` +
                # ``text`` + ``level``)。detector 把整帧 base64 JPEG +
                # 每个 box 的裁切 base64 写进事件,前端用原图 + bbox
                # overlay 渲染。
                #
                # dispatch 同时支持本模块的 ``LocatorDoc`` 和 SDK 的
                # ``GroundingResult``(后者 ``LMOutput`` 子类,字段形
                # 状一致;SDK fallback 路径可能直接返回它)。duck-typed
                # `_build_grounding_event` 内部同时接受两者。
                #
                # 顺序:LocatorDoc(本模块 pydantic class,严格匹配在前)
                # → _GroundingResult(SDK fallback class,守卫
                # `_GroundingResult is not None` 兼容 dev box 没装
                # wdnai.grounding 的场景)。
                _locator_match = (
                    (LocatorDoc is not None and isinstance(result, LocatorDoc))
                    or (_GroundingResult is not None and isinstance(result, _GroundingResult))
                )
                if _locator_match:
                    locator_ev = self._build_grounding_event(
                        result, frame_bgr, prompt, frame_pos, frame_b64,
                    )
                    if locator_ev is None:
                        # 未定位到目标 — 跳过该帧,不写 _events。
                        # 注:这里仍 break 跳出 iter 循环(模型本轮
                        # 已出 LocatorDoc,不再继续 tool loop);只是
                        # 本帧不产生 event。
                        exit_reason = "grounding_no_boxes"
                        break
                    events.append(locator_ev)
                    LOG.info(
                        "frame iter %d: grounding boxes=%d frame=%dx%d "
                        "level=%s",
                        i + 1,
                        len(locator_ev.boxes or []),
                        locator_ev.frame_w or 0,
                        locator_ev.frame_h or 0,
                        locator_ev.level,
                    )
                    exit_reason = "grounding_doc"
                    break

                # ---- VLMDoc branch (default) ------------------------------
                # VLMDoc 的 text 字段直接来自模型输出（SDK `_parse_output` 在
                # JSON 解析失败时会回退到 raw text，可能含特殊 token 或半截
                # JSON），写事件前统一过 sanitize 兜底。
                att_count = len(getattr(result, "attachment", []) or [])
                raw_text = _result_raw_text(result)
                clean_text = _sanitize_event_text(raw_text) or ""
                # 模型输出没解析成 generating schema —— 拿到的是 SDK 的
                # raw-text fallback(``str``)而不是 schema 实例。给事件
                # text 加 ``[解析失败]`` 前缀,前端历史记录里能直接区分
                # "正常 VLM 描述" vs "目标检测 schema 解析失败"。
                _schema_parse_failed = _result_schema_parse_failed(
                    result, generating_cls)
                if _schema_parse_failed:
                    LOG.error(
                        "frame iter %d: SCHEMA PARSE FAILED — "
                        "result_type=%s generating=%s",
                        i + 1, type(result).__name__,
                        getattr(generating_cls, "__name__", generating_cls),
                    )
                    _format_raw_text_for_log(raw_text, "raw_text (unparsed)")
                # 诊断：探测"result 不是 generating schema 实例"的隐形
                # 失败模式 —— 当生成 schema 是 VLMDoc 时，正常情况下
                # _parse_output 应该返回 VLMDoc 实例；如果不是，要么是
                # SDK 走了 raw-text fallback (返回 str)，要么是解析后
                # 构造出奇怪的 BaseModel 子类。两种都打 warning 让
                # 后续复现可定位。
                try:
                    _is_schema_inst = (
                        generating_cls is not None
                        and isinstance(result, generating_cls)
                    )
                except TypeError:
                    _is_schema_inst = False
                if generating_cls is not None and not _is_schema_inst:
                    # 升级到 ERROR: 目标检测页触发 = 用户能看到的"概率性
                    # 推理失败"事件,但事件 history 里没有任何 bbox/crop,
                    # 用户无法区分"模型正常输出文字" vs "模型输出没解析"。
                    LOG.error(
                        "frame iter %d: result not an instance of %s — "
                        "actual=%s",
                        i + 1,
                        getattr(generating_cls, "__name__", "?"),
                        type(result).__name__,
                    )
                    _format_raw_text_for_log(raw_text, "full raw_text")
                # schema parse 失败时,事件 text 加前缀 (不再仅 LOG.error
                # 而是写进 history 让用户能复盘)。
                _history_text = clean_text
                if _schema_parse_failed and generating_cls is not None:
                    _schema_name = getattr(generating_cls, "__name__", "?")
                    _history_text = (
                        f"[{_schema_name} 解析失败] "
                        f"{clean_text if clean_text else '(空)'}"
                    )
                final_ev = DetectorEvent(
                    id=-1,
                    frame_pos=-1,
                    ts=time.time(),
                    prompt=prompt,
                    text=_history_text,
                    level=getattr(result, "level", None) if not _schema_parse_failed else "error",
                    attachment_count=att_count,
                )
                prompt_stripped = (prompt or "").strip()
                text_stripped = clean_text.strip()
                # 提示词的尾部中英文标点（"，。！？!?,;: " 等）经常被
                # 模型回显时漏掉 / 漏多，用去掉标点的版本做 echo 判定。
                prompt_norm = _strip_trailing_punct(prompt_stripped)
                text_norm = _strip_trailing_punct(text_stripped)
                degenerate = (
                    (text_norm == prompt_norm and bool(prompt_norm))
                    or text_stripped in _DEGENERATE_VLM_TEXT
                    or not text_stripped
                )
                # 修复 (v0.13): schema 解析失败的事件**必须**写进 history,
                # 否则用户看到「什么都没推理出来」却无法区分:
                #   (a) 模型根本没产生可用输出 (clean_text 为空 + parse 失败)
                #   (b) 模型正常输出但被 degenerate 误过滤掉
                # 之前 (a) 的事件 _history_text 是 "[VLMDoc 解析失败] (空)",
                # 但 degenerate 检查的是 clean_text (空) → 整条事件被丢弃,
                # history 里看不到任何东西。本次修复: schema_parse_failed=True
                # 时跳过 degenerate 过滤,让事件 (含 [解析失败] 前缀 + level=error)
                # 一定写出 ——前端能立刻识别"模型输出了但没解析成 schema"。
                # 同样的逻辑对 TargetDoc 救援路径也适用: 救援出来的 TargetDoc
                # 实例 text 仍可能为空 (desc 写在 desc 字段,不是 text),
                # 但事件含 bbox/crop_b64,不能被这个过滤丢。
                force_keep = _schema_parse_failed or (
                    TargetDoc is not None
                    and isinstance(result, TargetDoc)
                )
                if degenerate and not force_keep:
                    LOG.info(
                        "frame iter %d: VLMDoc degenerate (text_len=%d "
                        "text=%r) — drop event",
                        i + 1,
                        len(clean_text),
                        clean_text,  # 完整文本, 不再截断
                    )
                else:
                    if degenerate and force_keep:
                        LOG.info(
                            "frame iter %d: VLMDoc degenerate but "
                            "schema_parse_failed=True — keep event for "
                            "diagnostics (text_len=%d text=%r)",
                            i + 1,
                            len(clean_text),
                            clean_text,  # 完整文本, 不再截断
                        )
                    events.append(final_ev)
                LOG.info(
                    "frame iter %d: VLMDoc level=%s text_len=%d (sanitized %d) "
                    "schema_parse_failed=%s",
                    i + 1,
                    final_ev.level,
                    len(raw_text),
                    len(clean_text),
                    _schema_parse_failed,
                )
                exit_reason = "vlm_doc"
                break
            else:
                # Python 没有 do-while/until —— for 循环里所有分支都
                # break 出来后实际不会到这里。保留 else 仅为安全垫，
                # 不应被命中。命中说明上面三个 break 都漏了。
                pass

                # 不应到达这里 —— 上面的 tool / TargetDoc / VLMDoc 三个
                # 分支里只要有一个匹配都会 break。日志埋点，捕获 SDK 返回
                # result 类型与任何已知分支都不匹配的"第四态"（例如
                # 仍不是 VLMDoc/TargetDoc/工具实例）。
                # 这是用户报告"推理 2 次后停"的核心可疑路径 —— 若该类
                # 结果反复出现而 multi-iter 循环命中 cap_hit，可能让上层
                # 误判"推理停"。
                LOG.warning(
                    "frame iter ?: result type %s fell through ALL branches "
                    "(not tool / not TargetDoc / not %s)",
                    last_result_type or "?",
                    getattr(generating_cls, "__name__", "?")
                    if generating_cls else "?",
                )
                _format_raw_text_for_log(last_raw_text, "last_raw_text")

                if exit_reason == "none":
                    exit_reason = (
                        f"cap_hit(max_iters={effective_max})"
                    )
                    LOG.warning(
                        "frame infer: loop hit effective_max=%d without schema, "
                        "final tool still calling — likely runaway model",
                        effective_max,
                    )

            # 诊断 (v0.13): 填充 diag record 关键字段并写入 ring buffer。
            _diag_rec.update(
                it_infer_ms=(time.monotonic() - _t0_infer) * 1000.0,
                exit_reason=exit_reason,
                events=len(events),
            )
            # 取最近一次 result 的类型 / 文本 (如果有),否则保持 "?"。
            # last_result_type / last_raw_text 在循环里更新过;events 里
            # 可能含有 schema_parse_failed 标记的 DetectorEvent。
            _diag_rec["result_type"] = last_result_type or "?"
            _diag_rec["raw_text_first"] = (
                last_raw_text[:500] if last_raw_text else ""
            )
            for _ev in events:
                if _ev.text:
                    _diag_rec["text"] = _ev.text
                    _diag_rec["text_len"] = len(_ev.text)
                if _ev.bbox is not None:
                    _diag_rec["bbox"] = list(_ev.bbox)
                    _diag_rec["crop_w"] = _ev.crop_w
                    _diag_rec["crop_h"] = _ev.crop_h
                if getattr(_ev, "level", None) == "error":
                    _diag_rec["schema_parse_failed"] = True
                break
            # 模型原始 bbox（归一化前，可能 0-1 或 0-1000）—— 排查
            # bbox 单位问题直接看这个字段，不必 dump reply JSON。
            _raw_bbox = getattr(self, "_last_bbox_raw", None)
            if _raw_bbox is not None:
                _diag_rec["bbox_raw"] = list(_raw_bbox)
            # TargetDoc skipped (no-detection 语义识别) 时, _build_target_event
            # 仍会写入 _last_bbox_raw (因为先归一化后才判定 skip),但
            # events 列表是空 —— 上面 for 循环没机会填 bbox/crop_w 等字段。
            # 这里单独补一组,让 skipped 事件也能在 diag 里看到 bbox。
            if exit_reason == "target_doc_skipped" and not events:
                if _raw_bbox is not None and _diag_rec.get("bbox") is None:
                    try:
                        _norm = _normalize_bbox_to_0_1000(_raw_bbox)
                        if _norm is not None:
                            _diag_rec["bbox"] = _norm
                    except Exception:  # noqa: BLE001
                        pass
            self._append_diag(_diag_rec)

            LOG.info(
                "frame infer done: iters=%d events=%d tools_called=%s exit=%s "
                "it_infer=%.0fms",
                len(events),
                len(events),
                [e.tool_call for e in events if e.tool_call],
                exit_reason,
                _diag_rec["it_infer_ms"],
            )
            return events
        except BaseException as e:
            # 诊断 (v0.13): 用 BaseException (不只是 Exception) 兜底 —
            # SIGSEGV / SystemError / RecursionError 在某些 rknn3 后端
            # 卡死场景下会以 BaseException 子类抛出,之前的 ``except
            # Exception`` 抓不到 → _worker_loop 直接死。
            LOG.exception(
                "frame infer crashed (BaseException): %s: %s — prompt=%r",
                type(e).__name__, e, prompt,
            )
            _diag_rec.update(
                it_infer_ms=(time.monotonic() - _t0_infer) * 1000.0,
                exit_reason=f"exception[{type(e).__name__}]",
                had_exception=True,
                exception_type=type(e).__name__,
                exception_msg=str(e)[:500],
            )
            self._append_diag(_diag_rec)
            return [DetectorEvent(
                id=-1, frame_pos=-1, ts=time.time(),
                prompt=prompt, text=f"(推理失败: {type(e).__name__}: {e})",
                level=None, attachment_count=0,
            )]

    # ------------------------------------------------------------------ #
    def _append_diag(self, rec: dict) -> None:
        """Append a diagnosis record to the in-memory ring buffer
        (``self._diag_history``). Lock-protected so _worker_loop /
        _infer_sync / diagnostics 端点并发读安全。

        字段约定见 ``__init__`` 中 ``_diag_history`` 的注释; 调用方
        写入前先把 ``text`` / ``bbox`` 等填好。
        """
        with self._diag_lock:
            self._diag_history.append(rec)

    def diag_history(self) -> list:
        """Read-only snapshot of recent diagnosis records (新 → 旧)。
        Used by ``/diagnostics`` HTTP endpoint.
        """
        with self._diag_lock:
            return list(self._diag_history)

    def clear_diagnostics(self) -> None:
        """Clear the diagnostics ring buffer (历史事件 ``_events`` 不动)。"""
        with self._diag_lock:
            self._diag_history.clear()

    # ------------------------------------------------------------------ #
    def _build_target_event(
        self,
        result: "object",
        frame_bgr: "np.ndarray",
        prompt: str,
        lbox_meta=None,
        unletterbox_bbox_fn=None,
    ) -> Optional[DetectorEvent]:
        """把 ``TargetDoc`` 实例 + 原帧包成 ``DetectorEvent``。

        * 从 ``bbox: [x1, y1, x2, y2]``（v2 schema，``samples/object_detect.py``
          同款 ``list[float]``）读出 bbox；duck-type fallback 到 v1 老 schema
          的 ``x1, y1, x2, y2`` 4 个独立 field（schema-version 过渡期
          老 worker 进程仍可能返回这种形态）；
        * bbox 走 ``_normalize_bbox_to_0_1000`` 单位自适应归一化（max ≤ 1.5
          视为 0–1 范围 ×1000），兼容模型偶发输出 0-1 小数的情况
          （真机 Qwen3-VL-4B 768×768 变体约 30% 概率）；
        * ``desc`` 过 sanitize（防 chatml token / 控制字符 / 半截
          JSON 漏出），再复制到 ``text``（沿用历史展示路径）；
        * 按 bbox 裁原图 + JPEG 编码 + base64 内嵌到 ``crop_b64``：
          ``_crop_and_encode`` 拿 ``lbox_meta`` + ``unletterbox_bbox``
          把 letterboxed 坐标精确映射回原图像素，裁出来的 ROI 是原
          比例（不包含 letterbox 灰边）；``lbox_meta=None`` 时退回
          到老的"把 0-1000 当原图坐标"的粗略算法以保证向下兼容。
        * 裁切为空时 ``crop_b64=""``，事件仍写出，``level='error'``。
        * ``tool_call='target_detection'`` —— 沿用历史事件的判别
          字段，前端用它识别"带 bbox / crop 的目标检测事件"。
          注意这里不再是 ``@Callable`` 工具调用，而是 schema 名
          语义化产物；与 ``tts_alert`` 区分（后者
          ``tool_call='tts_alert'`` 仍是真正的工具调用路径）。

        "未检测到目标" 语义识别：bbox=[0,0,1000,1000] 全画面 + desc 含
        "未检测到"/"没有"/"无" 等关键词 → 返回 None（detector 跳过事件
        写 history 与 WS 广播），避免自相矛盾的事件被前端展示。

        **返回 ``None``** 时表示该帧不应产生 detector 事件（detector 调
        用方 ``_worker_loop`` 检测到 None 直接 continue，不广播、不写
        ``_events``）。
        """
        # 把 ``bbox_raw``（模型原始 bbox，可能 0-1 或 0-1000）记录到
        # detector 进程级变量, _infer_sync 结束时读出来写进 diag。
        # 设计原因:_build_target_event 是同步函数调用, 不返回
        # bbox_raw 给调用方 (避免接口改; 调用方需要传 frame_bgr /
        # lbox_meta 等, 但 bbox_raw 是内部诊断字段)。用 detector 实
        # 例属性做进程内传递最简洁 —— 线程安全由 _worker_loop 单线
        # 程串行保证。
        self._last_bbox_raw = None
        # 1) 读 bbox 字段 —— 优先 v2 schema (result.bbox: list[float]),
        #    fallback 到 v1 老 schema (result.x1/y1/x2/y2 4 独立 field)
        bbox_raw: Optional[list] = None
        try:
            v = getattr(result, "bbox", None)
            if isinstance(v, (list, tuple)) and len(v) == 4:
                bbox_raw = list(v)
        except Exception:  # noqa: BLE001
            bbox_raw = None
        if bbox_raw is None:
            try:
                bbox_raw = [
                    float(result.x1),
                    float(result.y1),
                    float(result.x2),
                    float(result.y2),
                ]
            except Exception as e:  # noqa: BLE001
                LOG.warning(
                    "target_event: bbox 字段缺失 (既无 result.bbox 也无 "
                    "result.x1/y1/x2/y2): %s",
                    e,
                )
                return DetectorEvent(
                    id=-1, frame_pos=-1, ts=time.time(), prompt=prompt,
                    text=f"(目标检测失败: 字段缺失: {e})",
                    level="error",
                    attachment_count=0,
                    tool_call="target_detection",
                )
        # 2) bbox 单位自适应归一化 (0-1 → 0-1000)
        # 把模型原始 bbox 存到 detector 实例属性,供 _infer_sync 写
        # _diag_history 用 (用户排查"模型到底输出的是 0-1 还是 0-1000"
        # 时能直接看到原始值,不必 dump reply JSON)。
        self._last_bbox_raw = bbox_raw
        bbox = _normalize_bbox_to_0_1000(bbox_raw)
        if bbox is None:
            return DetectorEvent(
                id=-1, frame_pos=-1, ts=time.time(), prompt=prompt,
                text=f"(目标检测失败: bbox 无效: {bbox_raw!r})",
                level="error",
                attachment_count=0,
                tool_call="target_detection",
            )
        desc = _sanitize_event_text(getattr(result, "desc", "") or "") or ""
        # 3) "未检测到目标" 语义识别 —— bbox 全画面 + desc 提示 → 跳过
        if (bbox[0] <= 0.0 and bbox[1] <= 0.0
                and bbox[2] >= 1000.0 and bbox[3] >= 1000.0
                and _is_no_detection_desc(desc)):
            LOG.info(
                "target_event: skip no-detection (desc=%r)",
                desc[:60],
            )
            return None
        # 4) 裁切 + JPEG 编码 + base64
        crop_b64, crop_w, crop_h = _crop_and_encode(
            frame_bgr, bbox, lbox_meta, unletterbox_bbox_fn,
        )
        if not crop_b64:
            return DetectorEvent(
                id=-1, frame_pos=-1, ts=time.time(), prompt=prompt,
                text=f"(目标检测失败: 裁切区域为空){(': ' + desc) if desc else ''}",
                level="error",
                attachment_count=0,
                tool_call="target_detection",
                bbox=bbox,
                desc=desc,
            )
        display_text = desc if desc else "目标检测"
        return DetectorEvent(
            id=-1, frame_pos=-1, ts=time.time(), prompt=prompt,
            text=display_text,
            level=None,
            attachment_count=0,
            tool_call="target_detection",
            bbox=bbox,
            crop_b64=crop_b64,
            crop_w=crop_w,
            crop_h=crop_h,
            desc=desc,
        )

    # ------------------------------------------------------------------ #
    def _build_grounding_event(
        self,
        result,
        frame_bgr: "np.ndarray",
        prompt: str,
        frame_pos: int = -1,
        frame_b64: str = "",
    ) -> Optional[DetectorEvent]:
        """把 ``LocatorDoc`` (或 SDK ``GroundingResult``) 实例 + 原帧包成
        ``DetectorEvent``。

        与 ``_build_target_event`` 同样 duck-type 接受 duck-typed
        ``.boxes`` / ``.text`` / ``.level`` 字段,同时支持本模块的
        ``LocatorDoc`` 和 SDK 的 ``GroundingResult``(真机 worker
        ``sess.respond(generating=...)`` 返回的实例就是前者,但 SDK
        内部 schema 校验也可能 fallback 到后者)。

        行为约定:

        * ``boxes`` —— duck-type 读 ``result.boxes`` 列表(可空)。
          0-1000 归一化坐标 → 乘以原图 ``w`` / ``h`` 转到像素后按
          ``int(round(...))`` + clamp 到 ``[0, dim]``,填到 ``boxes[i].bbox``。
          ``boxes[i].crop_b64`` / ``crop_w`` / ``crop_h`` **故意留空**:VTL
          前端只画 bbox + label,真用 crop 缩略图的是 TargetDoc 路径
          (见 ``_build_target_event``)。
        * ``frame_b64`` —— 整帧 JPEG + base64(quality 跟实时流共用
          ``VideoSession.JPEG_QUALITY``),前端用原图 + bbox overlay
          渲染(整帧一张避免逐 box 多次 base64 传输)。
        * ``event.text`` —— "label1 @ [x1,y1,x2,y2]; label2 @ [...]"
          拼接形式。
        * ``event.tool_call = "grounding"`` —— 前端用此判别"多 bbox
          定位事件"(与 ``target_detection`` 单框 + crop 区分)。

        **返回 ``None``** 时表示该帧未定位到任何目标 —— 调用方
        ``_infer_sync`` 收到 None 跳过,不写 ``_events`` 不广播,
        历史列表不会显示这一帧。"模型崩了"与"未检测到"的区分
        在调用方:前者 ``_infer_sync`` 整 iter 失败(走 except
        路径),后者正常返回 0 boxes。
        """
        h, w = (0, 0)
        if frame_bgr is not None and getattr(frame_bgr, "ndim", 0) >= 2:
            h, w = frame_bgr.shape[:2]

        # ---- 1) 整帧 base64 (预编码,来自 _decode_loop 线程) -----------
        # 跨线程契约:整帧 JPEG + base64 已在 ``VideoSession._decode_loop``
        # 完成,经 ``_item_q`` → ``worker_loop`` → ``_infer_sync`` 传进来。切勿
        # 在这里再跑 ``cv2.imencode``,否则会重新把每事件 ~30-50ms 阻塞
        # 拉回主进程 worker_loop, 抵消优化。
        frame_b64 = frame_b64 or ""

        # ---- 2) 每个 box: 仅做坐标反变换 → 像素 bbox --------------------
        # ``box.crop_b64`` 故意留空 —— VTL 前端 ``drawVtlBoxes`` / 历史缩略图
        # 只消费 ``box.label`` + ``box.bbox``(用整帧 ``frame_b64`` 做底图),
        # 不读单 box 裁切图。每 box 一次 cv2.imencode + base64 在 worker_loop
        # 同步阻塞 ~30-100ms(1-5 box),把 detector 单例消费 _item_q 的节拍
        # 拖慢到实时流的两倍,产生可见卡顿。TargetDoc 路径 ``event.crop_b64``
        # 仍保留(单框 + 前端 ``tool_call==='target_detection'`` 真消费 crop),
        # 见 ``_build_target_event``。
        raw_boxes = []
        try:
            raw_boxes = list(getattr(result, "boxes", []) or [])
        except Exception:  # noqa: BLE001
            raw_boxes = []
        # 防御兜底：SDK structured parse 偶尔返回 ``boxes=[]`` 但 ``text``
        # 里仍带 ``<ref>/<box>`` 标记(raw-text fallback / schema parse
        # miss),此时用 ``wdnai.utils._parse_locate_output`` 直接从文本
        # 重新解析。导入放在函数体内,dev box 没装 wdnai 时也不会 crash。
        if not raw_boxes:
            _text_attr = _result_raw_text(result)
            if "<box>" in _text_attr:
                try:
                    from wdnai.utils import _parse_locate_output
                    _parsed_boxes, _ = _parse_locate_output(_text_attr)
                    raw_boxes = list(_parsed_boxes or [])
                except Exception as _parse_e:  # noqa: BLE001
                    LOG.debug(
                        "grounding_event: _parse_locate_output fallback "
                        "failed: %s",
                        _parse_e,
                    )
                    raw_boxes = []

        out_boxes: list = []
        for box in raw_boxes:
            # 兼容 ``BoundingBox`` 实例 (pydantic model) 与 dict 形态
            if hasattr(box, "x1"):
                try:
                    x1f = float(box.x1)
                    y1f = float(box.y1)
                    x2f = float(box.x2)
                    y2f = float(box.y2)
                except Exception:  # noqa: BLE001
                    continue
                label = str(getattr(box, "label", "") or "")
            elif isinstance(box, dict):
                try:
                    x1f = float(box.get("x1", 0))
                    y1f = float(box.get("y1", 0))
                    x2f = float(box.get("x2", 0))
                    y2f = float(box.get("y2", 0))
                except Exception:  # noqa: BLE001
                    continue
                label = str(box.get("label", "") or "")
            else:
                continue
            # BoundingBox 坐标是 letterbox 输入空间 (448×448) 的 0-1000
            # 归一化值,不能直接乘到原图尺寸 — 必走 unletterbox。
            try:
                from wdnai.utils import LetterboxMeta, unletterbox_bbox
                _m_scale = 448.0 / max(h, w)
                _m_ch = int(round(h * _m_scale))
                _m_cw = int(round(w * _m_scale))
                meta = LetterboxMeta(
                    size=448, orig_h=h, orig_w=w,
                    scale=_m_scale, content_h=_m_ch, content_w=_m_cw,
                    pad_top=(448 - _m_ch) // 2,
                    pad_left=(448 - _m_cw) // 2,
                )
                ux1f, uy1f, ux2f, uy2f = unletterbox_bbox(
                    [x1f, y1f, x2f, y2f], meta, clamp=True,
                )
            except Exception as _ul_e:  # noqa: BLE001
                LOG.warning(
                    "grounding_event: unletterbox failed (using naive scale): %s",
                    _ul_e,
                )
                ux1f, uy1f, ux2f, uy2f = (
                    x1f / 1000.0 * w, y1f / 1000.0 * h,
                    x2f / 1000.0 * w, y2f / 1000.0 * h,
                )
            px1 = int(round(ux1f))
            px2 = int(round(ux2f))
            py1 = int(round(uy1f))
            py2 = int(round(uy2f))
            if px1 > px2:
                px1, px2 = px2, px1
            if py1 > py2:
                py1, py2 = py2, py1
            out_boxes.append({
                "label": label,
                "bbox":  [px1, py1, px2, py2],
            })

        # ---- 3) text 拼接 + level 透传 ---------------------------------
        if not out_boxes:
            LOG.info(
                "grounding_event: no boxes detected (frame_pos=%d, prompt=%r) "
                "— skip event (frame will not appear in history)",
                frame_pos,
                (prompt or "")[:60],
            )
            return None

        counts = {}
        for b in out_boxes:
            label = b.get("label", "") or "(未命名)"
            counts[label] = counts.get(label, 0) + 1
        parts = [f"定位到 {len(out_boxes)} 个目标"]
        for label, count in counts.items():
            parts.append(f'"{label}" 数量：{count}')
        text = "; ".join(parts)

        level = getattr(result, "level", None)

        return DetectorEvent(
            id=-1, frame_pos=-1, ts=time.time(), prompt=prompt,
            text=text,
            level=level,
            attachment_count=0,
            tool_call="grounding",
            boxes=out_boxes,
            frame_b64=frame_b64,
            frame_w=int(w) if w else 0,
            frame_h=int(h) if h else 0,
        )

    # ------------------------------------------------------------------ #
    def _get_tool_call_max_iters(self) -> int:
        """读 ``config.jsonc["compute"]["tool_call_max_iters"]``。

        默认 ``5``；``0`` 表示无限（仍受 ``_ABSOLUTE_MAX_ITERS`` 兜底）；
        缺失 / 类型错误 / 负值 → 回退到 ``5``。每次推理前重读，配置热生效。
        """
        try:
            cfg = get_config().get_section("compute", {})
            if not isinstance(cfg, dict):
                return self._DEFAULT_MAX_ITERS
            v = cfg.get("tool_call_max_iters", self._DEFAULT_MAX_ITERS)
            iv = int(v)
            if iv < 0:
                return self._DEFAULT_MAX_ITERS
            return iv
        except Exception:
            return self._DEFAULT_MAX_ITERS

    # ------------------------------------------------------------------ #
    def _run_tool_side_effect(
        self,
        frame_bgr: "np.ndarray",
        ctx,
        result,
        prompt: str,
        tool_classes: Optional[List[type]] = None,
    ) -> Tuple[Optional[DetectorEvent], bool]:
        """如果 ``result`` 是某个 ``@Callable`` 工具的实例，执行副作用
        并返回 ``(DetectorEvent, is_fatal)``。

        * ``is_fatal=True`` 表示工具抛异常或返回无效结果 —— 调用方应
          立即跳出循环，不再把这一笔喂回 prompt（按设计：异常 → error
          → 跳出）。
        * ``is_fatal=False`` 表示工具正常执行 —— 调用方应把"调用 +
          返回"写进 transcript 继续下一轮。
        * ``result`` 不是任何已知工具实例 → 返回 ``(None, False)``，
          调用方按 ``VLMDoc`` / ``TargetDoc`` 默认分支处理。

        ``tool_classes`` 由调用方从 ``app.core.tools.registry`` 拿
        （``[reg.get_class(k) for k in enabled_tools]``），不在本模块
        里硬 import 任何具体工具类 —— core 层不耦合任何卡片,
        ``tts_alert`` / 未来的其它 ``@Callable`` 工具都从 registry 拿
        类引用。空列表/None 走"无工具"分支。

        单帧可多次调用本方法（每次 iter 一次）；返回值 ``is_fatal``
        控制是否中断循环。
        """
        if tool_classes and isinstance(result, tuple(tool_classes)):
            try:
                side = result(frame_bgr, ctx)
            except RuntimeError as e:
                return DetectorEvent(
                    id=-1, frame_pos=-1, ts=time.time(), prompt=prompt,
                    text=f"({e})", level="disabled",
                    attachment_count=0, tool_call="tts_alert",
                ), True
            except Exception as e:  # noqa: BLE001
                return DetectorEvent(
                    id=-1, frame_pos=-1, ts=time.time(), prompt=prompt,
                    text=f"(语音提醒失败: {e})", level="error",
                    attachment_count=0, tool_call="tts_alert",
                ), True
            if side.get("played"):
                text_v = (side.get("text") or "").strip()
                prompt_norm = _strip_trailing_punct(prompt or "")
                text_norm = _strip_trailing_punct(text_v)
                if text_norm and text_norm == prompt_norm:
                    LOG.info(
                        "tts_alert prompt-echo (text=%r) — drop event",
                        text_v[:60],
                    )
                    return None, False
                snippet = text_v if len(text_v) <= 60 else text_v[:57] + "..."
                return DetectorEvent(
                    id=-1, frame_pos=-1, ts=time.time(), prompt=prompt,
                    text=f"语音提醒: {snippet}",
                    level=None, attachment_count=0,
                    tool_call="tts_alert",
                    consume_iteration=bool(side.get("consume_iteration")),
                ), False
            # TTS 在跑（同一帧多轮工具循环里上一次的合成还没结束）——
            # 静默跳过，不写 error 事件也不打断循环。第一轮成功播的
            # 那一次 DetectorEvent 已经记录在 events 里了。
            if side.get("silent"):
                return None, False
            err = side.get("error") or "unknown"
            return DetectorEvent(
                id=-1, frame_pos=-1, ts=time.time(), prompt=prompt,
                text=f"(语音提醒失败: {err})", level="error",
                attachment_count=0, tool_call="tts_alert",
            ), True

        return None, False

    # ------------------------------------------------------------------ #
    def _format_tool_transcript(self, tool_result, event: DetectorEvent) -> str:
        """Format a tool call + its return into a prose block the model
        will see as a text attachment on the next iteration's prompt.

        **Deliberately prose, not JSON** —— SDK 的
        ``try_parse_tool_call`` 只解析模型的 *response*，但模型看到
        prompt 里有 JSON block 时倾向于把那段 JSON 抄回去当自己的
        输出（"respond with only JSON"），导致 transcript 里的旧
        工具调用被误当成新调用重发。用纯文本 + 仿 Python repr 的
        写法让模型"读懂"但不"复制"。

        当前唯一保留的工具是 ``tts_alert``（音视频描述页）；
        ``target_detection`` 已下线（视频目标检测改走 ``TargetDoc``
        结构化输出），因此这里不再有 ``target_detection`` 分支。
        """
        name = tool_result.__class__.__name__
        try:
            arguments = tool_result.model_dump()
        except Exception:
            try:
                arguments = dict(tool_result.__dict__)
            except Exception:
                arguments = {}

        # model_dump 可能含 chatml token / 控制字符（模型把 token 漏到
        # 字段里时），repr 序列化后再 sanitize，防止 transcript 进下一
        # 轮 prompt 后干扰模型。
        try:
            arguments = _sanitize_event_text(repr(arguments)) or ""
        except Exception:
            # 不再截断 [:200] —— tool transcript 是喂给模型做 wrap-up
            # 的输入,截断会让模型看到半截 JSON / 参数,影响下一轮推理
            # 质量。完整保留,模型自己会截到 context limit。
            arguments = repr(arguments)

        if event.tool_call == "tts_alert":
            played = event.level is None and (event.text or "").startswith("语音提醒")
            result_str = "played" if played else f"failed: {event.text}"
        else:
            result_str = "(no result)"

        return (
            f"Step: you called {name} with arguments={arguments!r}. "
            f"Result: {result_str}."
        )

    # ------------------------------------------------------------------ #
    def _worker_loop(self) -> None:
        # 诊断 (v0.13): 入口 / watchdog 日志 + BaseException 兜底
        LOG.info(
            "worker_loop[%s] started (generating_cls=%s)",
            self._generating_cls.__name__ if self._generating_cls else "?",
            self._generating_cls.__name__ if self._generating_cls else "?",
        )
        _last_seen_event_ts = time.monotonic()
        _WATCHDOG_CHECK_EVERY = 30  # 每 N 次循环 (~30s) 检查一次 idle
        _loop_iter = 0
        while not self._stop_event.is_set():
            _loop_iter += 1
            try:
                item = self._item_q.get(timeout=1.0)
            except queue.Empty:
                # 诊断 (v0.13): 每 30 次空轮询 (~30s) 触发 watchdog 检测,
                # 如果从最后一次事件时间戳算起超过 _WATCHDOG_IDLE_SECONDS
                # (默认 120s) 没事件 → LOG.warning 给上游排查源是否还在
                # 推帧。注意: video_session 一旦源 EOF 或文件短,frame_pos
                # 停滞是正常行为,warning 让用户区分"框架空闲" vs "源停了"。
                if _loop_iter % _WATCHDOG_CHECK_EVERY == 0:
                    _now = time.monotonic()
                    _idle = _now - _last_seen_event_ts
                    if _idle > self._WATCHDOG_IDLE_SECONDS:
                        LOG.warning(
                            "worker_loop[%s] idle for %.0fs (last event "
                            "_last_seen_event_ts ago, source 可能停了 / "
                            "submit_frame 未被调用 / every_n 太大)",
                            self._generating_cls.__name__ if self._generating_cls else "?",
                            _idle,
                        )
                continue
            if item is None:
                # explicit stop sentinel
                LOG.info(
                    "worker_loop[%s] received stop sentinel — exiting",
                    self._generating_cls.__name__ if self._generating_cls else "?",
                )
                return
            if len(item) == 5:
                frame_bgr, frame_pos, prompt, frame_b64, judge_score = item
            elif len(item) == 4:
                frame_bgr, frame_pos, prompt, frame_b64 = item
                judge_score = None
            else:
                frame_bgr, frame_pos, prompt = item
                frame_b64 = ""
                judge_score = None
            _t0_infer = time.monotonic()
            try:
                events = self._infer_sync(frame_bgr, prompt, frame_pos, frame_b64)
                # 判据分数在这一帧上是**所有事件共用的**（一次推理一帧，
                # 一帧里的多个事件分数一样），所以统一在这里回填，而不是
                # 在每个 DetectorEvent(...) 构造点都加一遍参数 —— 那些点
                # 有 10 处以上，漏一处就出现"有些事件有分数有些没有"。
                if judge_score is not None:
                    for _ev in (events or []):
                        try:
                            _ev.judge_score = judge_score
                        except Exception:  # noqa: BLE001
                            pass
            except BaseException as e:
                # 兜底: _infer_sync 内部已经 catch BaseException 写 diag,
                # 但万一再次冒出,worker_loop 也要保命,不让一次异常永久
                # 杀掉整个 daemon thread (用户报告"推理 2 次后停"的可疑
                # 路径之一)。
                LOG.exception(
                    "worker_loop[%s] caught unhandled exception from "
                    "_infer_sync (will continue): %s: %s",
                    self._generating_cls.__name__ if self._generating_cls else "?",
                    type(e).__name__, e,
                )
                continue
            _infer_ms = (time.monotonic() - _t0_infer) * 1000.0
            _video_ts = 0.0
            _video_ts_source = "default"
            try:
                _sess_snap = get_session().snapshot()
                _fps = float(_sess_snap.get("fps") or 0)
                if _fps <= 0 and not _sess_snap.get("is_live"):
                    import cv2 as _cv2
                    _src = _sess_snap.get("source") or ""
                    if _src.startswith("/"):
                        _probe = _cv2.VideoCapture(_src)
                        if _probe.isOpened():
                            _fps = float(
                                _probe.get(_cv2.CAP_PROP_FPS) or 0
                            ) or 0
                            if _fps > 0:
                                _video_ts_source = "cv2"
                        _probe.release()
                if _fps <= 0:
                    _fps = 25.0
                    if _video_ts_source == "default":
                        _video_ts_source = "fallback-25"
                if frame_pos >= 0:
                    _video_ts = float(frame_pos) / _fps
                else:
                    _video_ts_source = "frame_pos<0"
                # 安全网:如果 video_ts 仍然 0 (fps=0 兜底都失败),
                # 用 session 从打开到现在的 wall clock 时间当 fallback
                if _video_ts <= 0:
                    _started = _sess_snap.get("last_frame_ts") or 0
                    if _started > 0:
                        _video_ts = time.time() - _started
                        _video_ts_source = "wall-clock"
                LOG.info(
                    "vtl_timing: frame_pos=%d fps=%.2f video_ts=%.3f source=%s",
                    frame_pos, _fps, _video_ts, _video_ts_source,
                )
            except Exception as _e:  # noqa: BLE001
                LOG.warning(
                    "vtl_timing failed: %s: %s", type(_e).__name__, _e
                )
            with self._events_lock:
                for ev in events:
                    ev.id = self._next_event_id
                    ev.frame_pos = frame_pos
                    ev.ts = _video_ts
                    ev.video_ts = _video_ts
                    self._next_event_id += 1
                    self._events.append(ev)
                    self._latest = ev.to_dict()
                    _last_seen_event_ts = time.monotonic()
                # ``latest`` 在多 event 帧下指向最后一个；前端
                # ``addVtdEvent`` 用 unshift() 重新排序，最终列表的
                # 顺序仍是 frame_pos 内 id 倒序（最新在前）。
            for ev in events:
                self._broadcast(ev.to_dict())
            # 诊断 (v0.13): 每帧处理耗时日志 (DEBUG 级别,高频不刷屏)
            LOG.debug(
                "worker_loop[%s] frame_pos=%d events=%d it_infer=%.0fms",
                self._generating_cls.__name__ if self._generating_cls else "?",
                frame_pos, len(events), _infer_ms,
            )
        LOG.info(
            "worker_loop[%s] stopped (stop_event set)",
            self._generating_cls.__name__ if self._generating_cls else "?",
        )


_detector: Optional[WdnaiDetector] = None
_target_detector: Optional[WdnaiDetector] = None
_localizer_detector: Optional[WdnaiDetector] = None
_detector_lock = threading.Lock()


def get_detector() -> WdnaiDetector:
    """音视频描述页（``VLMDoc`` + 可选 ``tts_alert`` 工具）后端 detector 单例。

    ``generating_cls`` 留空 → ``WdnaiDetector.__init__`` 默认走
    ``VLMDoc``。
    """
    global _detector
    with _detector_lock:
        if _detector is None:
            _detector = WdnaiDetector()
        return _detector


def get_target_detector() -> WdnaiDetector:
    """视频目标检测页（``TargetDoc`` 直接结构化输出）后端 detector 单例。

    与音视频描述共用 ``WdnaiDetector`` 类，但实例上挂的是 ``TargetDoc``
    schema（``x1, y1, x2, y2`` + ``desc``），不挂任何 ``@Callable``
    工具。状态独立（prompt / 事件历史各自维护）；frame sink 共享同一
    个 ``VideoSession`` 的解码流。

    把 ``_get_target_doc_cls`` 函数传给 detector（而非 class 本身）
    —— 这样 ``start()`` 里按"callable → class"两步解析，首次 build
    与 wdnai import 失败路径跟 ``VLMDoc`` 走同一条逻辑。
    """
    global _target_detector
    with _detector_lock:
        if _target_detector is None:
            _target_detector = WdnaiDetector(generating_cls=_get_target_doc_cls)
        return _target_detector


def get_localizer_detector() -> WdnaiDetector:
    """视频目标定位页（``LocatorDoc`` 多 bbox + 整帧 base64）后端 detector 单例。

    第 3 个 ``WdnaiDetector`` 单例,挂 ``LocatorDoc`` schema
    （``boxes: List[BoundingBox]`` + ``text`` + ``level``）。事件可携带
    多个 bbox,整帧 JPEG 附在 ``frame_b64`` 字段,前端用原图 + bbox
    overlay 渲染。

    与音视频描述 / 视频目标检测走同一条 ``WdnaiDetector`` 类路径,
    状态独立（prompt / 事件历史 / frame_count 各自维护）。factory 传
    ``_get_grounding_doc_cls`` 可调用对象,``start()`` 时 lazy resolve 为
    真正的 class —— 与 ``get_target_detector`` 同构。
    """
    global _localizer_detector
    with _detector_lock:
        if _localizer_detector is None:
            _localizer_detector = WdnaiDetector(
                generating_cls=_get_grounding_doc_cls,
            )
        return _localizer_detector
