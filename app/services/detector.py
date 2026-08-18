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
import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import numpy as np

from app.services.config import get_config


_WDNAI_IMPORT_ERROR = ""
# wdnai is imported LAZILY (at start() time, not at module load) — RKNN native
# libraries can race with wdnav's mpp init or fork-unsafe state if loaded too
# eagerly, and a missing wheel on dev boxes would otherwise crash app startup
# even for users who never touch the detection panel.
_WDNAI_CACHE: dict = {}

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
        _WDNAI_CACHE.update(
            _ok=True,
            _Attachment=Attachment,
            _LLMSession=LLMSession,
            _SystemLanguageModel=SystemLanguageModel,
            _LMOutput=LMOutput,
            _Generable=Generable,
            _Field=Field,
            _error="",
        )
        return True, Attachment, LLMSession, SystemLanguageModel, LMOutput, Generable, Field, ""
    except Exception as e:
        _WDNAI_CACHE.update(
            _ok=False,
            _Attachment=None,
            _LLMSession=None,
            _SystemLanguageModel=None,
            _LMOutput=None,
            _Generable=None,
            _Field=None,
            _error=str(e),
        )
        return False, None, None, None, None, None, None, str(e)


# --------------------------------------------------------------------------- #
# Structured output schema — inherits LMOutput so the SDK can populate
# ``attachment`` automatically when a future backend produces binary
# outputs. Currently empty for VLM (text-only path).
# --------------------------------------------------------------------------- #
def _build_vlm_doc_class():
    """Build the ``VLMDoc`` schema lazily, once wdnai is importable.

    The class is created with ``@Generable`` + ``LMOutput`` as the base so
    the SDK can attach the parsed fields into an instance that also
    carries the standard ``attachment`` slot.
    """
    # ``from typing import Optional`` *inside* this function so the name
    # shows up in the function's locals — ``pydantic`` resolves forward
    # refs by ``typing.get_type_hints``, which searches the enclosing
    # frame's namespace. Without this, the ``Optional[str]`` annotation
    # for ``level`` cannot be evaluated and instantiation fails.
    from typing import Optional

    _, _, _, _, LMOutput, Generable, Field, _ = _load_wdnai()

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
        text: str = Field(
            description=(
                "Natural-language description of what is happening in the "
                "frame: scene, main subjects (people/objects), their actions "
                "and the surrounding environment. Reply in the same language "
                "as the user's request. Ignore on-screen overlays such as "
                "timestamps, watermarks, subtitles or captions — they are "
                "not part of the actual scene."
            ),
        )
        level: Optional[str] = Field(
            default=None,
            description="Optional severity tag (e.g. 'normal' / 'warning' / 'danger').",
        )

    return VLMDoc


# Cached after first build.
_VLM_DOC_CLS = None


def _get_vlm_doc_cls():
    global _VLM_DOC_CLS
    if _VLM_DOC_CLS is None:
        _VLM_DOC_CLS = _build_vlm_doc_class()
    return _VLM_DOC_CLS


# --------------------------------------------------------------------------- #
# TargetDoc —— 视频目标检测页 structured-output schema                       #
# --------------------------------------------------------------------------- #
# 取代旧版 ``target_detection`` @Callable 工具：模型在生成结构化输出时
# 直接给出 ``x1, y1, x2, y2``（0–1000 归一化） + ``desc``（ROI 内描
# 述文本），detector 在 worker 线程里按 bbox 裁原帧、JPEG 编码、base64
# 内嵌进事件 JSON（``crop_b64`` / ``crop_w`` / ``crop_h``），``desc``
# 复制到 ``text`` 字段，事件历史列表沿用现有展示路径。
def _build_target_doc_class():
    """Build the ``TargetDoc`` schema lazily, once wdnai is importable.

    与 ``VLMDoc`` 一样继承 ``BaseModel`` 而非 ``LMOutput`` —— 避免
    ``attachment: List[Attachment]`` 字段污染 ``model_json_schema``，
    否则 SDK 的 ``_safe_schema`` 会返回空串，模型看不到字段含义。
    """
    from typing import Optional  # noqa: F401  (kept for parity w/ VLMDoc)

    _, _, _, _, _, Generable, Field, _ = _load_wdnai()

    from pydantic import BaseModel as _PydBaseModel

    @Generable
    class TargetDoc(_PydBaseModel):
        """Structured target-detection output for wdnwebui's
        video-target-detection panel.

        模型应给出 0–1000 归一化 ROI 坐标 ``x1, y1, x2, y2``（左上 +
        右下）以及 ``desc``（对 ROI 框出内容的描述）。detector 会按
        坐标裁原图 + JPEG 编码 + base64 内嵌，把 ``desc`` 写到事件
        ``text`` 字段。
        """
        x1: float = Field(description="ROI 左上角 x，0–1000 归一化。")
        y1: float = Field(description="ROI 左上角 y，0–1000 归一化。")
        x2: float = Field(description="ROI 右下角 x，0–1000 归一化。")
        y2: float = Field(description="ROI 右下角 y，0–1000 归一化。")
        desc: str = Field(description="对 ROI 框出区域内容的描述文本。")

    return TargetDoc


# Cached after first build.
_TARGET_DOC_CLS = None


def _get_target_doc_cls():
    global _TARGET_DOC_CLS
    if _TARGET_DOC_CLS is None:
        _TARGET_DOC_CLS = _build_target_doc_class()
    return _TARGET_DOC_CLS


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
    attachment_count: int = 0
    tool_call: Optional[str] = None
    bbox: Optional[list] = None
    crop_b64: Optional[str] = None
    crop_w: Optional[int] = None
    crop_h: Optional[int] = None
    desc: Optional[str] = None
    # ``True`` when the tool that produced this event is a pure
    # side-effect (e.g. ``tts_alert`` 播完了就是播完了，不再需要模型
    # wrap-up). detector 看到 ``tool_ev.consume_iteration`` 时直接跳出
    # multi-iter 循环，不再把 "你刚说了 X" 的 transcript 喂回下一轮
    # 避免模型退化到复读用户的提示词。
    consume_iteration: bool = False

    def to_dict(self) -> dict:
        return {
"id":               self.id,
            "frame_pos":        self.frame_pos,
            "ts":               self.ts,
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


# --------------------------------------------------------------------------- #
# 视频目标检测页专用：bbox 区域裁切 + JPEG 编码 + base64 内嵌
# --------------------------------------------------------------------------- #
def _crop_and_encode(frame_bgr: "np.ndarray", bbox_0_1000) -> tuple:
    """Crop ``frame_bgr`` at the given 0–1000 normalized bbox, JPEG-encode,
    base64 the bytes. Returns ``(b64_str, w, h)``. Empty / invalid bbox
    → ``("", 0, 0)``.

    Kept module-private (no leading underscore on import) so a unit test
    can target it directly. OpenCV import is lazy: we only need it on
    TargetDoc invocation, and opencv-python-headless is a hard dep
    already used by the upload pipeline.
    """
    if frame_bgr is None or frame_bgr.ndim < 2:
        return "", 0, 0
    try:
        if not isinstance(bbox_0_1000, (list, tuple)) or len(bbox_0_1000) != 4:
            return "", 0, 0
        x1f, y1f, x2f, y2f = (float(v) for v in bbox_0_1000)
    except (TypeError, ValueError):
        return "", 0, 0

    h, w = frame_bgr.shape[:2]
    x1 = max(0, min(w, int(round(x1f / 1000.0 * w))))
    x2 = max(0, min(w, int(round(x2f / 1000.0 * w))))
    y1 = max(0, min(h, int(round(y1f / 1000.0 * h))))
    y2 = max(0, min(h, int(round(y2f / 1000.0 * h))))
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
    上限从 ``config.json["compute"]["tool_call_max_iters"]`` 读取，
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
    # 默认循环上限（config.json 缺失 / 类型不对 / 负值时使用）。
    _DEFAULT_MAX_ITERS = 5

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
        self._model_name = "qwen3-vl-4b"
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


    # ------------------------------------------------------------------ #
    @property
    def available(self) -> bool:
        ok, *_ = _load_wdnai()
        return ok

    def snapshot(self) -> dict:
        from app.services.compute_registry import get_registry
        registry = get_registry()
        try:
            sess = registry.get_session("swcap_video_describe")
            session_ready = sess is not None
            session_error = ""
        except RuntimeError as e:
            sess = None
            session_ready = False
            session_error = str(e)
        # Query the running qwen3-vl-4b instance for its loaded
        # vision-model native sizes — never hardcode [384, 768] on
        # the client. Empty list when the worker hasn't reported yet
        # (model not loaded, network probe failure, missing capability).
        available_sizes: List[int] = []
        if sess is not None:
            try:
                for inst in registry.list_with_source():
                    if inst.get("model_id") == "qwen3-vl-4b":
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
                "model":                  self._model_name,
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
            }

    # ------------------------------------------------------------------ #
    def start(self, prompt: str, every_n: int = 30,
              enabled_tools: Optional[List[str]] = None,
              image_size: Optional[int] = None) -> dict:
        if not self.available:
            raise RuntimeError("wdnai 不可用")
        if not (prompt or "").strip():
            raise ValueError("prompt 不能为空")
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
            from app.services.compute_registry import get_registry
            registry = get_registry()
            available_sizes: List[int] = []
            try:
                for inst in registry.list_with_source():
                    if inst.get("model_id") == "qwen3-vl-4b":
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
        from app.services.tools import get_tool_registry
        reg = get_tool_registry()
        tool_keys: List[str] = []
        for key in enabled_tools or []:
            if not isinstance(key, str):
                raise ValueError(f"enabled_tools 项必须是字符串: {key!r}")
            if not reg.has(key):
                raise ValueError(f"未知工具: {key!r}")
            tool_keys.append(key)
        # 业务级校验：算力是否就绪。这一步失败 → 503 / RuntimeError，
        # 前端能区分"功能未启用"和"运行时错误"。
        from app.services.compute_registry import get_registry
        try:
            get_registry().get_session("swcap_video_describe")
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
        # Build the structured-output class now (lazy) so that any
        # import failure surfaces here instead of inside the worker.
        # 该 detector 实例的 schema 在 ``__init__`` 时记录，未指定
        # 时回退到 ``VLMDoc``（音视频描述页）。视频目标检测页实例会
        # 显式传 ``TargetDoc`` 进来。
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
        with self._lock:
            self._enabled = True
            self._prompt = prompt
            self._every_n = max(1, int(every_n))
            self._enabled_tools = tool_keys
            self._image_size = image_size  # None → backend auto-pick
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
        return self.snapshot()

    def close(self) -> None:
        """Stop the worker — the LLMSession is owned by ComputeRegistry,
        not us, so we don't close it here. Registry.close_all() handles
        that on app shutdown."""
        self.stop()

    # ------------------------------------------------------------------ #
    def submit_frame(self, frame_bgr: "np.ndarray", frame_pos: int) -> None:
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
            self._item_q.put_nowait((frame_bgr, frame_pos, prompt))
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
    def _infer_sync(self, frame_bgr: "np.ndarray", prompt: str) -> List[DetectorEvent]:
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
          - ``TargetDoc``：取 ``x1, y1, x2, y2, desc``，按 bbox 裁原
            图 + JPEG 编码 + base64 内嵌进 ``crop_b64``，``desc`` 复
            制到 ``text``。
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
        try:
            from app.services.compute_registry import get_registry
            sess = get_registry().get_session("swcap_video_describe")
        except RuntimeError as e:
            LOG.warning(
                "frame infer: capability disabled (swcap_video_describe): %s",
                e,
            )
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
            ok, Attachment, *_ = _load_wdnai()
            if generating_cls is None:
                # 兜底：start() 之前没跑过（实际不会发生——start() 第一
                # 件事就是解析 schema，这里只是防御性 null-check）。
                generating_cls = _get_vlm_doc_cls()
            TargetDoc = (
                _get_target_doc_cls()
                if generating_cls is not _get_vlm_doc_cls()
                else None
            )

            from app.services.tools import get_tool_registry
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
            from app.services.tools.registry import ToolContext
            ctx = ToolContext(width=w, height=h, prompt=prompt)

            frame_attachment = Attachment(frame_bgr)
            events: List[DetectorEvent] = []
            tool_transcripts: List[str] = []

            LOG.info(
                "frame infer start: schema=%s tools=%s max_iters(cfg=%s, effective=%s) prompt=%r",
                generating_cls.__name__,
                enabled_tools or "(none)",
                max_iters_cfg,
                effective_max,
                (prompt[:60] + "…") if len(prompt) > 60 else prompt,
            )

            exit_reason = "none"
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
                    # 用户在 ``#vd-detect-size`` 选定的视觉模型
                    # 目标尺寸；backend 端 ``VLMBackend.run_images`` 会
                    # 透传给 ``_pick_variant(image_size=...)`` 强制选
                    # 那个变体。``None`` 时保留 auto-pick。
                    image_size=image_size,
                )
                result = asyncio.run(coro)

                # ---- tool branch -------------------------------------------
                tool_ev, is_fatal = self._run_tool_side_effect(
                    frame_bgr, ctx, result, prompt,
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
                    target_ev = self._build_target_event(result, frame_bgr, prompt)
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

                # ---- VLMDoc branch (default) ------------------------------
                # VLMDoc 的 text 字段直接来自模型输出（SDK `_parse_output` 在
                # JSON 解析失败时会回退到 raw text，可能含特殊 token 或半截
                # JSON），写事件前统一过 sanitize 兜底。
                att_count = len(getattr(result, "attachment", []) or [])
                raw_text = str(getattr(result, "text", "") or "")
                clean_text = _sanitize_event_text(raw_text) or ""
                final_ev = DetectorEvent(
                    id=-1,
                    frame_pos=-1,
                    ts=time.time(),
                    prompt=prompt,
                    text=clean_text,
                    level=getattr(result, "level", None),
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
                if degenerate:
                    LOG.info(
                        "frame iter %d: VLMDoc degenerate (text_len=%d "
                        "text=%r) — drop event",
                        i + 1,
                        len(clean_text),
                        clean_text[:80],
                    )
                else:
                    events.append(final_ev)
                LOG.info(
                    "frame iter %d: VLMDoc level=%s text_len=%d (sanitized %d)",
                    i + 1,
                    final_ev.level,
                    len(raw_text),
                    len(clean_text),
                )
                exit_reason = "vlm_doc"
                break

            if exit_reason == "none":
                exit_reason = (
                    f"cap_hit(max_iters={effective_max})"
                )
                LOG.warning(
                    "frame infer: loop hit effective_max=%d without schema, "
                    "final tool still calling — likely runaway model",
                    effective_max,
                )

            LOG.info(
                "frame infer done: iters=%d events=%d tools_called=%s exit=%s",
                len(events),
                len(events),
                [e.tool_call for e in events if e.tool_call],
                exit_reason,
            )
            return events
        except Exception as e:  # pragma: no cover - depends on model
            LOG.exception("frame infer crashed: %s", e)
            return [DetectorEvent(
                id=-1, frame_pos=-1, ts=time.time(),
                prompt=prompt, text=f"(推理失败: {e})",
                level=None, attachment_count=0,
            )]

    # ------------------------------------------------------------------ #
    def _build_target_event(
        self,
        result: "object",
        frame_bgr: "np.ndarray",
        prompt: str,
    ) -> DetectorEvent:
        """把 ``TargetDoc`` 实例 + 原帧包成 ``DetectorEvent``。

        * 从 ``x1, y1, x2, y2`` 拼 ``bbox`` 数组；
        * ``desc`` 过 sanitize（防 chatml token / 控制字符 / 半截
          JSON 漏出），再复制到 ``text``（沿用历史展示路径）；
        * 按 bbox 裁原图 + JPEG 编码 + base64 内嵌到 ``crop_b64``；
          裁切为空时 ``crop_b64=""``，事件仍写出，``level='error'``。
        * ``tool_call='target_detection'`` —— 沿用历史事件的判别
          字段，前端用它识别"带 bbox / crop 的目标检测事件"。
          注意这里不再是 ``@Callable`` 工具调用，而是 schema 名
          语义化产物；与 ``tts_alert`` 区分（后者
          ``tool_call='tts_alert'`` 仍是真正的工具调用路径）。
        """
        try:
            bbox = [
                float(result.x1),
                float(result.y1),
                float(result.x2),
                float(result.y2),
            ]
        except Exception as e:  # noqa: BLE001
            return DetectorEvent(
                id=-1, frame_pos=-1, ts=time.time(), prompt=prompt,
                text=f"(目标检测失败: 字段缺失: {e})",
                level="error",
                attachment_count=0,
                tool_call="target_detection",
            )
        desc = _sanitize_event_text(getattr(result, "desc", "") or "") or ""
        crop_b64, crop_w, crop_h = _crop_and_encode(frame_bgr, bbox)
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
    def _get_tool_call_max_iters(self) -> int:
        """读 ``config.json["compute"]["tool_call_max_iters"]``。

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

        当前唯一保留的 ``@Callable`` 工具是 ``tts_alert``（音视频描述
        页）。视频目标检测页的检测逻辑走 ``TargetDoc`` 结构化输出，
        不经工具分支。

        单帧可多次调用本方法（每次 iter 一次）；返回值 ``is_fatal``
        控制是否中断循环。
        """
        from app.services.tools.tts_alert import TtsAlert

        if isinstance(result, TtsAlert):
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
            arguments = repr(arguments)[:200]

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
        while not self._stop_event.is_set():
            try:
                item = self._item_q.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                return
            frame_bgr, frame_pos, prompt = item
            events = self._infer_sync(frame_bgr, prompt)
            with self._events_lock:
                for ev in events:
                    ev.id = self._next_event_id
                    ev.frame_pos = frame_pos
                    self._next_event_id += 1
                    self._events.append(ev)
                    self._latest = ev.to_dict()
                # ``latest`` 在多 event 帧下指向最后一个；前端
                # ``addVtdEvent`` 用 unshift() 重新排序，最终列表的
                # 顺序仍是 frame_pos 内 id 倒序（最新在前）。
            for ev in events:
                self._broadcast(ev.to_dict())


_detector: Optional[WdnaiDetector] = None
_target_detector: Optional[WdnaiDetector] = None
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
