"""视频文件分析服务：自管理 wdnav.VideoCapture + 后台推理线程。

设计要点：

* **独立于 ``VideoSession``** —— 视频监控 / 音视频描述 各有各的
  capture 生命周期，VFA 跑批量分析时不该把它们的视频源也抢走。
  所以这里自己持有一个 ``wdnav.VideoCapture``（懒加载 + 独立
  decode 线程），不挂 ``VideoSession.add_frame_sink``。
* **LLMSession 不归本服务所有** —— 严格走 ``ComputeRegistry``
  的 ``swcap_video_describe`` capability，遵守设计原则 1。多个消费者
  （detector + VFA）共用同一 session，所有 ``session.respond``
  前后用 ``ComputeRegistry.get_session_call_lock()`` 串行化。
* **每 N 帧调一次 VLM**，把结果写到 ``self._events`` 里持久化；
  全文件走完后跑一次综述（reset + 单次 respond，prompt 包含
  所有 per-frame 文本）。事件本身不含帧字节，"回查视频文件取帧"
  走 ``GET /api/video-file-analysis/frame/{frame_pos}`` 重新
  解码源文件（仅 file 源；live 源取帧拒绝）。
* **wdnai 懒加载**：同 detector，只有 ``start()`` 才碰
  ``Attachment`` / ``LLMSession``，避免 web 启动时 segfault。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional


try:
    from wdnav import VideoCapture as _WdnavVideoCapture
except Exception:  # pragma: no cover - platform-specific load failure
    _WdnavVideoCapture = None


LOG = logging.getLogger("vfa_service")


# 持久化目录：默认 ``app/services/vfa_runs/``，可用 ``VFA_RUNS_DIR``
# 环境变量覆盖（部署到只读目录或共享存储时改）。
_DEFAULT_RUNS_DIR = Path(__file__).resolve().parent / "vfa_runs"


def _probe_total_frames(source: str) -> int:
    """读视频总帧数。

    ``wdnav.VideoCapture`` 只暴露 ``width / height / fps / frame_pos``，不
    暴露 ``total_frames``；这里用 PyAV 单独探一次 metadata 拿全文件帧数。
    返回 0 表示拿不到（RTSP / live / raw / metadata 缺失）—— 前端进度
    会自动退到 indeterminate 模式。

    优先用 ``stream.frames``（容器显式声明的帧数），拿不到再退到
    ``duration / time_base * average_rate`` 计算。

    网络 URL（rtsp/rtmp/hls）加 2s open_timeout，避免卡死探测线程。
    """
    is_network = "://" in source
    try:
        import av  # 懒加载，避免非 RK 开发板启动就崩
        kwargs = {}
        if is_network:
            kwargs["timeout"] = (2.0, 5.0)
        with av.open(source, **kwargs) as container:
            if not container.streams.video:
                return 0
            stream = container.streams.video[0]
            # 1. 容器显式声明
            if stream.frames and stream.frames > 0:
                return int(stream.frames)
            # 2. duration × rate
            if stream.duration and stream.time_base and stream.time_base > 0:
                avg_rate = stream.average_rate
                if avg_rate and avg_rate > 0:
                    seconds = float(stream.duration * stream.time_base)
                    return int(round(seconds * float(avg_rate)))
            return 0
    except Exception as e:
        LOG.debug("vfa: total_frames probe failed for %s: %s", source, e)
        return 0


_WDNAI_CACHE: dict = {}


def _load_wdnai():
    """懒导入 wdnai；和 detector._load_wdnai 互不影响，但 cache 共享。"""
    if "_Attachment" in _WDNAI_CACHE:
        return (
            _WDNAI_CACHE["_ok"],
            _WDNAI_CACHE["_Attachment"],
            _WDNAI_CACHE["_error"],
        )
    try:
        from wdnai import Attachment  # noqa: F401
        _WDNAI_CACHE.update(_ok=True, _Attachment=Attachment, _error="")
        return True, Attachment, ""
    except Exception as e:
        _WDNAI_CACHE.update(_ok=False, _Attachment=None, _error=str(e))
        return False, None, str(e)


def _build_scene_frame_schema():
    """构建 ``SceneFrame`` @Generable schema。

    ``@Generable`` 修饰的类在 wdnai SDK 内部被转成 pydantic BaseModel，
    ``session.respond(generating=SceneFrame)`` 时 SDK 会自动把 schema
    作为 JSON hint 追加到 prompt 末尾，让模型按字段输出。

    注意：schema 渲染用 ``str(dict)`` 而不是 ``json.dumps(indent=2)``，
    Qwen3-VL 对 schema 格式敏感（详见 wdnai/AGENTS.md）。

    **继承 ``BaseModel`` 而非 ``LMOutput``** —— 与 detector.VLMDoc / TargetDoc
    同源。``LMOutput`` 自带 ``attachment: List[Attachment]`` 字段会让
    pydantic 的 ``model_json_schema()`` 抛 ``PydanticInvalidForJsonSchema``，
    SDK 的 ``_safe_schema`` 兜底返回的"输出 JSON 对象 + 字段名清单"
    弱 hint 让 Qwen3-VL-4B 在 RKNN3 上 JSON 命中率仅 ~30% (旧版实测)，
    自由发挥时还会把 schema 例子里的 ``<30-100字描述>`` 占位符抄回去当
    输出（"乱码"现象）。继承 BaseModel 让 SDK 拿到完整 JSON Schema
    （含 ``properties.required.description``），模型遵循率显著提升。
    VFA 不消费 ``attachment`` 槽（TTS 才用），改基类零下游破坏。
    """
    from typing import List, Optional
    from wdnai import Generable, Field
    from pydantic import BaseModel as _PydBaseModel

    @Generable
    class SceneFrame(_PydBaseModel):
        """单帧视频的结构化分析 —— 反查 / 检索 / 报告生成的数据基础。

        **字段顺序刻意设计**（关键修复：解决"视频文件分析 vs 视频目标检测"
        在相同视频/提示词下乱码率差异的根因）：

          1. scene / actors / action / objects 是**约束型字段**（短字符串 +
             数组，token 候选空间小），**先输出** → 模型在生成阶段就被
             "锚定"在 JSON 语法 + 短 token 模式里。
          2. text（最长的 CJK 序列）放到**最末** → 此时 decoder 已 anchor，
             RKNN3 quantization noise 让首 token 漂移到 Latin/Cyrillic
             subword 的概率显著降低（VTD 的 TargetDoc 之所以不乱码就是
             这个原理 —— 4 个数字字段把 decoder 锚定在数字 + 标点 token
             空间，~30% 输出 token 是数字/标点，相当于隐式 stabilizer）。
          3. note 仍是 optional（保持向后兼容）。

        **字段长度约束**（pydantic Field(max_length=...) → schema hint 暴露
        给模型）：

          - scene:   ≤12 字（短场景标识，类似 TargetDoc x1 的"数字约束"）
          - action:  ≤20 字（短动作描述）
          - actors:  ≤3 项（数组约束）
          - objects: ≤4 项（数组约束）
          - text:    ≤80 字（比改前 ~30-100 字略缩，drift 窗口减小）
          - note:    ≤30 字

        text 仍承担反查模糊匹配（``?text=...``）的主来源 —— 80 字足够
        覆盖"含场景、人物动作、关键物体"的简洁描述。
        """
        # 1. 场景（短字符串先输出 → 锚定 decoder）
        scene: str = Field(
            description="场景类别（地点 + 上下文，≤12 字）",
            max_length=12,
        )
        # 2. 人物（数组约束）
        actors: List[str] = Field(
            description="画面中的人物或主体，按显著度排序（无则 []，最多 3 项）",
            max_length=3,
        )
        # 3. 动作（短文本）
        action: str = Field(
            description="主要动作或事件，一句话（≤20 字）",
            max_length=20,
        )
        # 4. 物体（数组约束）
        objects: List[str] = Field(
            description="关键物体，按显著度排序（无则 []，最多 4 项）",
            max_length=4,
        )
        # 5. 文本（最长的 CJK 序列放最后 → decoder 已 anchor 后再生成）
        text: str = Field(
            description=(
                "简要描述（≤80 字）。含场景、人物动作、关键物体。"
                "反查模糊匹配（?text=...）的来源字段，模型原话越丰富越好。"
                "必填字段。"
            ),
            max_length=80,
        )
        # 6. 可选备注
        note: Optional[str] = Field(
            default=None,
            description="补充观察（异常 / 情绪 / 不确定项，≤30 字，无则 null）",
            max_length=30,
        )

    return SceneFrame


# Cached after first build.
_SCENE_FRAME_CLS = None


def _get_scene_frame_cls():
    global _SCENE_FRAME_CLS
    if _SCENE_FRAME_CLS is None:
        _SCENE_FRAME_CLS = _build_scene_frame_schema()
    return _SCENE_FRAME_CLS


def _looks_garbled(text: str) -> bool:
    """复用 wdnai SDK 的启发式检测 VLM 输出是否乱码。

    Qwen3-VL-4B 在 RKNN3 上首 token 不稳定（量化噪声），约 ~15% 的帧
    会产出 CJK/Latin token salad（典型样本：
    ``稽abilidade巡射赴abilidade...``）。SDK 自己的
    ``LLMSession._parse_output`` Path 5 兜底时调同一个函数 bump
    ``StreamSink.dirty_score``；这里在 wdnwebui 端再调用一次做独立
    检测，避免 ``_looks_garbled`` 内部 bump 被绕过时还能识别。

    **额外规则（针对 SDK 漏判的 subword-level token salad）**：
    SDK 的启发式依赖 CJK 比例 <30% 等硬阈值，对 "Strand吃过斯顿斯顿斯顿
    吃过weepweep见过minster吃过吃过吃过..." 这种 CJK 比例 43.8% 但本质
    是拉丁/西里尔短串被 CJK 包裹的样本漏判。这里追加两条针对 RKNN3
    量化噪声独有模式的规则：

      1. 任一 Latin/Cyrillic 短串（≥2 字符）在文中出现 ≥3 次
         （不必连续）→ flag（对应 "稽abilidade..." 的 5+ 次重复）。
      2. CJK + Latin/Cyrillic(2-8 字符) + CJK 直接相连模式（无空格）
         出现 ≥3 次 → flag（对应 "吃过weepweep见过minster..." 的 subword
         交替）。长度上限 8 字符避免 "iPhone"/"Python"/"Windows" 等
         长词误报；阈值 ≥3 避免单次偶发误报。

    wdnai.utils 是 vendored 在 ``.venv/src/wdnai/src/wdnai/utils.py`` 里；
    旧版（v0.1.x）可能没这函数，try/except ImportError 兜底返 False
    （按"宁放过不错杀"策略 —— 真的乱码只是 UI 难看，误杀会导致
    干净输出被错误重试一次）。
    """
    if not text or len(text) < 8:
        return False
    # 1. SDK 内置启发式（CJK 比例 <30% / 半截 JSON / 控制字符 / 长 Latin
    #    连续 / 单字符重复 8+ 等）
    try:
        from wdnai.utils import _looks_garbled as _sdk_garbled
        if bool(_sdk_garbled(text)):
            return True
    except (ImportError, AttributeError):
        pass

    # 2. 任一 Latin/Cyrillic 短串（≥2 字符）出现 ≥3 次
    latin_runs = re.findall(r'[A-Za-zА-Яа-я]{2,}', text)
    if len(latin_runs) >= 3:
        from collections import Counter
        max_count = max(Counter(latin_runs).values())
        if max_count >= 3:
            return True

    # 3. CJK + Latin/Cyrillic(2-8 字符) + CJK 直接相连模式 ≥3 次
    #    长度上限 8 排除 iPhone/Python/Windows 等长英文词；
    #    无空格要求排除 "新款 iPhone 15" 等带空格自然混合文本。
    cjk_latin_cjk = re.findall(r'[一-鿿][A-Za-zА-Яа-я]{2,8}[一-鿿]', text)
    if len(cjk_latin_cjk) >= 3:
        return True

    return False


def _split_scene_frame_result(result) -> tuple:
    """把 ``_call_llm(generating=SceneFrame)`` 的返回值拆成
    ``(structured_dict, text)``：

    真实硬件上的 5 个分支（按优先级）：
      1. ``result`` 是 ``SceneFrame`` 实例 → 字段直接取
         （但若 ``_looks_garbled(text)`` 为 True，视为 SDK Path 5 兜底
         假象 —— structured 全部 default、text 是 raw token salad；
         **静默丢弃 raw text**（返回 ``(None, "")``），与 VTD 行为
         对齐 —— 详见下方注释）
      2. ``result`` 是 dict（pydantic 验证失败的 raw dict）→ 智能归一化：
         已知字段直接映射，未知字段（如 ``analysis`` / ``error``）拼到 ``note``，
         避免数据全丢。
      3. ``result`` 是 str 且能 parse 成 JSON → 用解析值
         （SDK JSON 解析失败时常见，模型输出含 ``{`` 但 pydantic 拒绝）
      4. ``result`` 是 str 含 free-form 文本 → 用正则 + 关键词表抽出
         ``scene / actors / objects``，作为粗粒度结构化字段
      5. 抽不出任何结构 → ``structured=None``，text 保留 raw

    **与 VTD (WdnaiDetector) 的行为对齐**：VTD 走 ``detector.py:_infer_sync``
    对 SDK Path 5 兜底返回的 ``str`` 走 ``getattr(str, "text", "")`` 时拿到
    空串（str 无 ``text`` 属性），**等同于静默丢弃**。VFA 原本把 raw
    text 塞给 ``_sanitize_text`` 再传给前端，但真正的 CJK+Latin token
    salad 既不能 regex 也不能 sanitize 修复 —— 用户看到"乱码"。
    现统一行为：检测到 garbage 时返回 ``(None, "")``，由 _infer_one
    构造空事件（_dirty=True，note="(本帧分析失败)"）。
    """
    SceneFrame = _get_scene_frame_cls()

    # Path 1：SDK 解析成功 —— 但要警惕 SDK Path 5 兜底：模型输出不是
    # JSON 时 SDK 把 raw text 包成 SceneFrame(text=raw, 其他字段=default)，
    # 这种假象下 text 是 token salad，structured 全部空但 Path 1 仍命中。
    # 用 _looks_garbled 二次判定：乱码则视为失败，**静默丢弃 raw text**
    # （与 VTD 行为一致 —— VTD 走 detector.py:_infer_sync 时
    # ``getattr(str, "text", "")`` 自然拿到空串）。
    if isinstance(result, SceneFrame):
        try:
            text = getattr(result, "text", None) or ""
            # 检测 SDK Path 5 假象 + 真正的乱码
            if _looks_garbled(text):
                LOG.warning(
                    "vfa: SceneFrame 来自 SDK Path 5 兜底（text=%d chars, garbled），静默丢弃",
                    len(text),
                )
                # 与 VTD 行为对齐：返回空 text，原始 raw_text 仅写入日志
                return None, ""
            # structured 只放 5 个过滤字段（不含 text，避免与顶层 text 重复）
            structured = {
                "scene":   getattr(result, "scene", "") or "",
                "actors":  list(getattr(result, "actors", []) or []),
                "action":  getattr(result, "action", "") or "",
                "objects": list(getattr(result, "objects", []) or []),
                "note":    getattr(result, "note", None),
            }
            # text 单独从 model 拿（顶层 ev.text 用它，不进 structured）
            if not text:
                # SDK 没回 text（模型漏输出）时退到短摘要
                text = f"{structured['scene']}：{structured['action']}".strip("：")
            if not text:
                text = structured["action"] or structured["scene"] or "(空)"
            return structured, text
        except Exception as e:
            LOG.warning("vfa: SceneFrame 字段提取失败: %s", e)

    # Path 2：pydantic 验证失败的 raw dict（_call_llm 兜底返回）
    if isinstance(result, dict):
        try:
            structured = _normalize_structured(result)
            text = _text_from_dict(result)
            # dict 路径同样需要 _looks_garbled 兜底：SDK Path 5 在 JSON
            # 解析失败的某些路径会返回 dict（raw input），里头 text 字段
            # 可能是 token salad。静默丢弃与 Path 1 一致。
            if text and _looks_garbled(text):
                LOG.warning(
                    "vfa: raw dict 路径 text 字段乱码（%d chars），静默丢弃", len(text),
                )
                return None, ""
            return structured, text
        except Exception as e:
            LOG.warning("vfa: raw dict 归一化失败: %s", e)

    # 失败分支：result 是 str。优先尝试 JSON parse，再 regex。
    raw = str(result).strip()
    parsed = _parse_structured_from_str(raw)
    if parsed is not None:
        return parsed, raw

    return None, raw


def _text_from_dict(obj: dict) -> str:
    """从 dict 里抽一个 fallback 文本。

    优先取 ``text`` 字段（VFA SceneFrame 的主描述字段，承担反查模
    糊匹配来源），其次取第一个长字符串值。

    **字段顺序无关性**：SceneFrame schema 重排后 ``text`` 不再是 dict
    的第一个字段，按值顺序遍历会先撞到 ``scene``（短字符串）或
    ``actors/objects``（数组），导致 fallback 路径下 _text_from_dict
    返回错误的文本。显式优先 ``text`` 字段规避这个回归。
    """
    if isinstance(obj, dict):
        # 优先取 'text' 字段（SceneFrame 主描述来源）
        if "text" in obj and isinstance(obj["text"], str) and obj["text"]:
            return obj["text"]
        # 兜底：第一个长字符串
        for v in obj.values():
            if isinstance(v, str) and len(v) >= 4:
                return v
    # fallback：把所有 string 拼起来
    return " ".join(str(v) for v in (obj or {}).values() if v)


def _parse_structured_from_str(text: str) -> Optional[dict]:
    """str 兜底：从模型输出里 parse 出结构化字段。

    两种来源：
      - 模型尝试输出 JSON 但 SDK 解析失败（常见：输出 ``{...}`` 含
        非法值，如 ``"scene": ["array", "instead", "of", "string"]``）
      - 模型输出 free-form 文本，含 ``### 场景定位`` / ``### 关键物体``
        等 markdown 小节

    先试 JSON parse，再 regex 抽取。
    """
    if not text or not text.startswith("{"):
        return _regex_extract_structured(text)
    # 找第一个完整 {...} 块（可能嵌套）
    import json, re
    # 尝试整段 parse
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return _normalize_structured(obj)
    except Exception:
        pass
    # 找第一个 {...} 块 parse
    m = re.search(r"\{[^{}]*\}", text)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return _normalize_structured(obj)
        except Exception:
            pass
    # JSON 没成 → regex
    return _regex_extract_structured(text)


def _normalize_structured(obj: dict) -> dict:
    """把 dict 字段归一化到 SceneFrame 的字段名（**不含 text**）。

    text 是顶层字段，单独存放在 ``ev.text``，不进 structured（避免
    与顶层 text 字段重复）。structured 只放可过滤的 5 个字段。

    智能匹配：scene/actors/action/objects/note 直接映射；其他未知
    字段（Qwen3-VL 经常输出 ``analysis`` / ``error``）拼到 ``note`` 里。
    """
    known: dict = {}
    note_extras: list = []
    for k, v in obj.items():
        if k in ("scene", "actors", "action", "objects", "note"):
            known[k] = v
        elif isinstance(v, str) and v:
            note_extras.append(f"{k}: {v}")
        elif isinstance(v, list) and v:
            note_extras.append(f"{k}: {','.join(str(x) for x in v if x)}")
    structured = {
        "scene":   str(known.get("scene") or ""),
        "actors":  _as_str_list(known.get("actors")),
        "action":  str(known.get("action") or ""),
        "objects": _as_str_list(known.get("objects")),
        "note":    None,
    }
    if known.get("note") is not None:
        structured["note"] = str(known["note"])
    if note_extras:
        extra = "; ".join(note_extras)
        structured["note"] = (
            f"{structured['note']} | {extra}" if structured["note"] else extra
        )
    return structured


def _as_str_list(v) -> List[str]:
    if isinstance(v, list):
        return [str(x) for x in v if x]
    if isinstance(v, str):
        return [v] if v else []
    return []


# --------------------------------------------------------------------------- #
# Batch rescue：当 VLM per-frame SceneFrame 全部失败时，用一次 LLM 调用
# 批量提取结构化字段（成本：1 次 LLM 调用处理 N 帧，amortized 1s/N per frame）。
# --------------------------------------------------------------------------- #
RESCUE_BATCH_PROMPT = (
    "以下是从视频中按时间顺序抽出的 {frame_count} 帧描述（per-frame VLM 输出 "
    "free-form 文本，未按 JSON schema 走）。请**严格按格式**为每帧输出一行 JSON：\n"
    "[帧 <frame_pos>] {{\"scene\": \"<场景>\", \"actors\": [\"<人物>\"], "
    "\"action\": \"<动作>\", \"objects\": [\"<物体>\"]}}\n"
    "\n"
    "字段说明：scene=地点/环境；actors=人物或主体（无则 []）；action=一句话动作；"
    "objects=关键物体（无则 []）。**不要**输出 ``text`` 字段（每帧的原文已在下方输入里）。"
    "只输出 JSON 行，**不要任何其他文字**。\n"
    "\n"
    "--- 输入 ---\n"
    "{lines}"
)


def _parse_rescue_line(line: str) -> Optional[dict]:
    """从模型输出的一行 ``[帧 N] {"scene":...}`` 解析成 dict。"""
    if not line:
        return None
    import re, json
    m = re.search(r"\{[^{}]*\}", line)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    return _normalize_structured(obj)


def _build_rescue_prompt(events) -> Tuple[str, list]:
    """构造 batch prompt，并返回 (prompt, [(frame_pos, ev_id), ...]) 顺序列表
    用于按 LLM 输出顺序回填 structured。"""
    lines = []
    order = []
    for e in events:
        text = (e.text or "").strip()
        if len(text) > 1200:  # 单帧太长截掉，避免 prompt 爆
            text = text[:1200] + "…"
        lines.append(f"[帧 {e.frame_pos}]\n{text}")
        order.append((e.frame_pos, e.id))
    return RESCUE_BATCH_PROMPT.format(
        frame_count=len(events),
        lines="\n\n".join(lines),
    ), order


def _sanitize_actors(actors) -> list:
    """清洗 actor 列表：剔除明显不是人的词（如车型 / 物品 / 抽象描述）。

    历史 run（落盘前 regex 抽错）里 actors 偶尔混进 "车型" 这类
    关键词，这里按 _OBJECT_KW 反向过滤掉。"""
    if not actors:
        return actors or []
    return [a for a in actors if a and not _looks_like_object(a)]


def _looks_like_object(value: str) -> bool:
    """粗略判断一个值是不是 object 关键词（车 / 设备 / 物品等）。"""
    if not value:
        return False
    for kw in _OBJECT_KW:
        if kw in value:
            return True
    # 常见车型 / 车型名后缀
    if any(suffix in value for suffix in ("车型", "SUV", "汽车", "轿车", "卡车")):
        return True
    return False


def _regex_extract_structured(text: str) -> Optional[dict]:
    """text 兜底最后一道防线：用 markdown 标题 + 关键词表粗粒度抽
    scene / actors / objects。匹配失败返回 None（不强制结构化）。"""
    import re
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines()]

    scene = actor = object_ = None
    for i, line in enumerate(lines):
        # 同 api 端：超过 _SECTION_HEADER_MAX_LEN 字符的 # 行不是
        # 干净的 heading（VLM 经常塞长描述进去），不当作 section header
        if line.startswith("#") and len(line) <= 40:
            if scene is None and ("场景" in line or "地点" in line or "概览" in line):
                scene = _pick_value(lines, i, _SCENE_KW)
            elif actor is None and ("人物" in line):
                # 只用 "人物" —— "主体" 太宽泛会被 "画面主体" 误触发
                actor = _pick_value(lines, i, _ACTOR_KW)
            elif object_ is None and ("物体" in line or "关键" in line):
                object_ = _pick_value(lines, i, _OBJECT_KW)
        if scene and actor and object_:
            break
    if not (scene or actor or object_):
        return None
    return {
        "scene":   scene or "",
        "actors":  [actor] if actor else [],
        "action":  "",
        "objects": [object_] if object_ else [],
        "note":    None,
        "_source": "regex",
    }


# 模块级常量（同时供 _regex_extract_structured 和 api/ 端点复用）
_SCENE_KW = (
    "办公室", "会议室", "街道", "走廊", "厨房", "客厅", "卧室", "餐厅",
    "教室", "商场", "超市", "医院", "银行", "工厂", "工地", "公园",
    "小区", "车库", "楼梯", "电梯", "室内", "室外", "门口", "入口",
    "大楼", "路边", "天桥", "地下", "停车场",
)
_ACTOR_KW = (
    "人", "男人", "女人", "小孩", "男孩", "女孩", "老人", "行人", "顾客",
    "医生", "老师", "学生", "警察", "司机", "工人", "店主", "宠物",
    "狗", "猫", "鸟",
)
# 同 api 端：超 _SECTION_HEADER_MAX_LEN 字符的 # 行不是 section header
_SECTION_HEADER_MAX_LEN = 40
_OBJECT_KW = (
    "车", "小车", "汽车", "卡车", "自行车", "电动车", "摩托车", "公交车",
    "桌子", "椅子", "电脑", "笔记本电脑", "手机", "门", "窗户", "雨伞",
    "灯", "包", "书", "纸", "笔", "杯子", "水杯", "话筒",
)


def _first_kw(line: str, keywords) -> Optional[str]:
    for kw in keywords:
        if kw in line:
            return kw
    return None


def _truncate_lines_keep_edges(text: str, max_chars: int) -> str:
    """超长 lines 字符串按行等比采样（保留首尾），截到 max_chars 内。

    用于 chunk summary prompt 字符预算超限时 —— 等距丢中间几行不会
    丢失关键信息（每行结构类似：[帧 N] scene: action ...）。
    """
    if not text or len(text) <= max_chars:
        return text
    lines = text.splitlines()
    if not lines:
        return text[:max_chars] + "…"
    # 算理想保留行数：每行 ~40 字符上限（含中英文混排）
    target_lines = max(4, max_chars // 40)
    if len(lines) <= target_lines + 2:
        # 行数接近目标保留数，没必要采样
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "…"
    # 保留首尾 + 中间等距采样
    head_n = max(1, target_lines // 3)
    tail_n = max(1, target_lines // 3)
    middle_target = max(1, target_lines - head_n - tail_n - 1)  # -1 for marker
    sampled: List[str] = []
    sampled.extend(lines[:head_n])
    middle_count = len(lines) - head_n - tail_n
    if middle_count > 0:
        step = max(1, middle_count // middle_target)
        i = head_n
        taken = 0
        while i < len(lines) - tail_n and taken < middle_target:
            sampled.append(lines[i])
            i += step
            taken += 1
    sampled.append(f"…（共 {len(lines)} 行，保留 {len(sampled) + tail_n} 行）")
    sampled.extend(lines[-tail_n:])
    out = "\n".join(sampled)
    if len(out) > max_chars:
        out = out[:max_chars] + "…"
    return out


def _sanitize_text(raw: str, max_len: int = 500) -> str:
    """清洗模型原始输出：去 think 块 / markdown 头 / 控制字符 / 部分 JSON 残留。

    当 VLM 输出不符合 JSON schema 时，wdnai SDK 会把 raw 文本塞进
    ``LMOutput.text`` 字段（或 wdnwebui 的 ``ev.text``）。这个函数把
    raw 文本整理成干净的自然语言描述，避免前端事件列表里出现：
      - ``<think>...</think>`` 思维链残留（thinking 模型泄漏）
      - ``### 画面描述`` / ``- 项目`` 等 markdown 噪声
      - ``{"text": "…"}`` 半截 JSON
      - 控制字符 / U+FFFD 替换字符
      - 超长输出截断

    返回空字符串表示无可用文本（让调用方退到 scene/action 兜底）。
    """
    if not raw:
        return ""
    s = str(raw)
    # 1. 剥离 think 块（Qwen3 等 thinking 模型偶发泄漏）
    s = re.sub(r"<think>.*?</think>", "", s, flags=re.DOTALL)
    s = re.sub(r"<thinking>.*?</thinking>", "", s, flags=re.DOTALL)
    # 2. 去掉 markdown 代码块围栏 (``` ... ``` / ```json ... ```) —— 必须在
    #    行内代码剥离之前，否则行内代码 regex 会先吃掉一个反引号，剩下
    #    的两个反引号无法被 fence regex 匹配。
    s = re.sub(r"```(?:json)?\s*", "", s)
    s = re.sub(r"```", "", s)
    # 3. 剥离 markdown 标题 (#, ##, ###) 和列表符号
    s = re.sub(r"(?m)^\s*#{1,6}\s*", "", s)
    s = re.sub(r"(?m)^\s*[-*•·]\s+", "", s)
    s = re.sub(r"(?m)^\s*\d+\.\s+", "", s)
    # 4. 剥离加粗 (**text**) 和行内代码
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    # 5. 如果整体看起来是 JSON 对象/数组（SDK 解析失败时把整段塞回 text），
    #    先尝试从 raw JSON 里抽 "text" 字段。注意：这里直接 json.loads 而
    #    不是 _parse_structured_from_str，因为后者会做归一化（text 字段不
    #    在 structured 里，会被塞进 note），我们需要的是 text 原值。
    #    两阶段：
    #    a) 整体以 { 或 [ 起头 → 整段 parse
    #    b) 中间有 {...} JSON 块 → 抽出来后看里面是否有 text 字段
    stripped = s.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        text_value = _extract_json_text_field(stripped)
        if text_value:
            s = text_value
        else:
            # JSON 解析失败 / 没 text 字段：去掉 { } [ ] " 字符 + 把键值分隔符变空格
            s = re.sub(r'[{}\[\]"]', " ", s)
            s = re.sub(r"\s*:\s*", " ", s)
            s = re.sub(r"\s*,\s*", " ", s)
    else:
        # 文本里夹杂 JSON 块（典型 "标题\n```json\n{...}\n```" 跑过 fence
        # 剥离后的残余）。尝试抓第一段 {...} 块，如果能解析且有 text 字段，
        # 用 text 替换该块；否则去掉 JSON 语法字符避免乱码。
        m = re.search(r"\{[^{}]+\}", s)
        if m:
            inner = _extract_json_text_field(m.group(0))
            if inner:
                s = s.replace(m.group(0), inner)
            else:
                # 块里没 text 字段：去掉 { } [ ] " + 把键值分隔符变空格
                cleaned = re.sub(r'[{}\[\]"]', " ", m.group(0))
                cleaned = re.sub(r"\s*:\s*", " ", cleaned)
                cleaned = re.sub(r"\s*,\s*", " ", cleaned)
                s = s.replace(m.group(0), cleaned)
    # 6. 去除控制字符 + U+FFFD 替换字符
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", s)
    s = s.replace("�", "")
    # 7. 折叠空白：多空格 / 多换行
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n[ \t]*\n+", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = s.strip()
    if not s:
        return ""
    # 8. 截断
    if len(s) > max_len:
        s = s[:max_len] + "…"
    return s


def _extract_json_text_field(text: str) -> Optional[str]:
    """从 raw JSON 文本里直接抽 ``text`` 字段（不走 _normalize_structured）。

    用 ``_parse_structured_from_str`` 会把 text 字段归一化进 note —— 这里
    我们要 text 原值，所以自己 parse 一次取 text 字段。失败返回 None。
    """
    if not text:
        return None
    try:
        import json as _json
        obj = _json.loads(text)
    except Exception:
        # 试 regex 抓第一段 {...}
        import re as _re
        m = _re.search(r"\{[^{}]*\}", text)
        if not m:
            return None
        try:
            obj = _json.loads(m.group(0))
        except Exception:
            return None
    if not isinstance(obj, dict):
        return None
    text_value = obj.get("text")
    if isinstance(text_value, str) and text_value.strip():
        return text_value
    return None


def _pick_value(lines, header_idx, keywords) -> Optional[str]:
    import re
    for j in range(header_idx + 1, min(header_idx + 6, len(lines))):
        nxt = lines[j].strip()
        if not nxt:
            continue
        clean = nxt.lstrip("-* \t").replace("**", "")
        if not clean or clean.startswith("#"):
            break
        kw = _first_kw(clean, keywords)
        if kw:
            return kw
        m = re.match(r"^([^。,，.;；:：\n\r]+)", clean)
        if m:
            v = m.group(1).strip().rstrip("，,;；:")
            if v and len(v) >= 2:
                return v[:24]
        return clean[:24]
    return None


# --------------------------------------------------------------------------- #
# 事件 & 综述
# --------------------------------------------------------------------------- #
@dataclass
class FrameEvent:
    """单帧分析结果。前端拿 ``to_dict()`` 渲染。

    ``text`` 是**完整文本**（模型原话 30-80 字 + 救援 chunk 的
    LLM 输出），既用于事件列表显示，也用于反查模糊匹配
    （``?text=雨天穿红衣的人`` —— 直接子串匹配 ``text``）。

    ``structured`` 是 @Generable SceneFrame 的 dict，**只存 4 个核心
    字段**（scene/actors/action/objects）+ 可选 note。不再重复存
    description —— ``text`` 已经承担了这个职责。

    ``_dirty`` 标记 SDK Path 5 兜底 + 重试仍乱码的帧（典型：
    Qwen3-VL-4B 在 RKNN3 上首 token 不稳定，~17% 概率产出 token salad）。
    前端可据此在事件历史列表 / 反查 modal 里加视觉警告徽章，
    但事件仍保留（text 可能是部分有效内容）。
    """
    id: int
    frame_pos: int
    ts: float
    prompt: str
    text: str
    structured: Optional[dict] = None
    _dirty: bool = False

    def to_dict(self) -> dict:
        out = {
            "id":        self.id,
            "frame_pos": self.frame_pos,
            "ts":        self.ts,
            "prompt":    self.prompt,
            "text":      self.text,
        }
        if self.structured is not None:
            out["structured"] = self.structured
        if self._dirty:
            # 下划线开头字段通常前端会忽略；这里显式给出方便
            # UI / API 消费者识别"该帧 SDK 输出质量异常"。
            out["_dirty"] = True
        return out


# 综述 prompt 模板
#
# Qwen3-VL-4B 的 ``max_position_embeddings = 8192``，单次 prompt+output
# 必须落在窗口内。100+ 帧时把全部 per-frame 文本拼起来会轻松爆掉
# （实测 17258 token > 8192 → RKNN native abort）。
#
# Apollo 三段式：场景检测 → per-scene 小结 → 最终综述。
#   pass 1：按场景切（无场景则按 SUMMARY_CHUNK_SIZE 兜底切），每段独立生成
#           ~120 字小总结（用 max_new_tokens=200 硬控输出）
#   pass 2：把所有 scene 小结拼起来做最终 300 字综述（max_new_tokens=400）
#
# 单帧文本先截到 SUMMARY_FRAME_TEXT_MAX 字符，再算 chunk 总长，确保
# pass 1 单 prompt 也不超窗口。
#
# 关键约束：每个 chunk 的 prompt（template + lines）必须 ≤ SUMMARY_CHUNK_PROMPT_BUDGET
# 字符。template CHUNK_SUMMARY_PROMPT ≈ 200 字符，留 SUMMARY_CHUNK_PROMPT_BUDGET
# 给 lines —— 每个 chunk ≤ 15 帧 × 60 字符 ≈ 900 字符 + 模板 ≈ 1100 字符，
# 远低于 8192 token 窗口，且单个 chunk 完整在 NPU 上下文内。
SUMMARY_CHUNK_SIZE             = 15      # 单块最多 15 帧；prompt 安全
SUMMARY_FRAME_TEXT_MAX         = 60      # 单帧文本最大字符数（紧凑）
SUMMARY_CHUNK_PROMPT_BUDGET    = 2200    # 单 chunk prompt 字符上限（含模板）
SUMMARY_PROMPT_CHAR_BUDGET     = 3000    # 兜底：最终 prompt parts 字符上限（< 5000 token）
CHUNK_MAX_NEW_TOKENS           = 200     # pass 1 单块硬控输出
FINAL_MAX_NEW_TOKENS           = 400     # pass 2 最终综述硬控输出

# Chunk-level rescue 在早期实现里嵌在 _infer_one（每 30 events 同步跑一次
# batch LLM 兜底）。问题：worker 线程被 rescue 阻塞，且 30 events × 500 字符
# 的 prompt 已经接近 8192 token 窗口上限，RKNN3 偶发 abort / hang；中途失败
# 的 daemon 线程还会污染后续 session_run。结果：单帧分析时间被人为拉长，
# 且容易触发 RKNN native 卡死。改方案：把 rescue 推迟到 EOF 后 _run_summary
# 开头跑一次；同时严格按字符预算控制单次 rescue 事件数，避免超窗口。
#
# **加固（v0.5.x 起）**：SceneFrame 改 BaseModel 后 per-frame JSON 命中率
# 上升，rescue 兜底的 batch 数下降；同步放宽两个上限 —— 让 30 events/batch
# 真正能装满（之前 1800 字符预算只够 ~13 events/batch，30 限制形同虚设）。
# 安全校验：5000 字符 ≈ 2500 input tokens；30 events × ~80 token 输出 JSON
# 行 = 2400 output tokens；总 ~4900 tokens，远低于 8192 token 窗口。
RESCUE_MAX_EVENTS_PER_CALL     = 30      # 单次 rescue LLM 调用最多处理多少 events（改前 15）
RESCUE_PROMPT_CHAR_BUDGET      = 5000    # rescue prompt 字符上限（输入侧，改前 1800）

# 场景检测（cv2 HSV 直方图）
SCENE_HIST_BINS            = (50, 60)        # H×S 直方图格子数
SCENE_BHATTACHARYYA_THRESH = 0.5             # Bhattacharyya 距离阈值
SCENE_DECAY_ALPHA          = 0.1             # accumulateWeighted 衰减

CHUNK_SUMMARY_PROMPT = (
    "以下是一段视频中按时间顺序的 {frame_count} 帧描述（起止帧号 {first_frame}~{last_frame}）。"
    "请用不超过 120 字总结这段内容的关键信息（人物、动作、场景、事件）。"
    "\n\n{lines}"
)

FINAL_SUMMARY_PROMPT = (
    "以下是按时间顺序的 {scene_count} 个场景的总结。"
    "请基于这些内容，对整段视频写一段不超过 300 字的最终综述，"
    "包含：主题、按时间顺序的关键事件。"
    "\n\n{parts}"
)


# --------------------------------------------------------------------------- #
# 主服务
# --------------------------------------------------------------------------- #
class VideoFileAnalysisService:
    """单实例 + 单次分析任务。再开新一轮要先 ``close()``。

    状态机：
      idle → opening → running → done
                       ↘ error
                       ↘ stopped（用户中途 stop，不出综述）
    """

    STATE_IDLE = "idle"
    STATE_OPENING = "opening"
    STATE_RUNNING = "running"
    # STATE_SUMMARIZING：per-frame 分析已结束（_decode_loop EOF + _worker_loop
    # 收到 None sentinel），正在跑 _run_summary。前端用这个状态显示
    # "生成综述中 N/M"，避免 UI 卡在 "分析中" 误导用户。
    STATE_SUMMARIZING = "summarizing"
    STATE_DONE = "done"
    STATE_ERROR = "error"
    STATE_STOPPED = "stopped"

    HISTORY_MAX = 5000  # 事件硬上限；超长视频保护 OOM

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state = self.STATE_IDLE
        self._error = ""
        self._source = ""
        self._prompt = ""
        self._every_n = 30
        self._is_live = False
        self._cap = None
        self._width = 0
        self._height = 0
        self._fps = 0.0
        self._total_frames = 0
        self._total_frames_source = "unknown"
        self._frame_pos = 0
        self._sampled = 0           # 本次分析已抽样的帧数
        self._analyzed = 0          # 已完成的推理数
        self._dirty_count = 0       # retry 后仍乱码的帧数（监测指标）
        self._started_at = 0.0
        self._finished_at = 0.0

        self._events: deque = deque(maxlen=self.HISTORY_MAX)
        self._events_lock = threading.Lock()
        self._next_event_id = 1
        self._summary = ""
        self._summary_ts = 0.0
        # 综述阶段进度：(done, total)；0/0 表示无进度或已完成。
        # 用于前端在 STATE_SUMMARIZING 阶段显示 "生成综述中 3/10"。
        self._summary_progress = (0, 0)

        # 持久化：每次 ``start()`` 分配一个 run_id；状态变更（done /
        # stopped / error / 新一轮挤掉旧 run）时把当前快照写盘。
        self._run_id: str = ""
        self._runs_dir: Path = Path(
            os.environ.get("VFA_RUNS_DIR") or str(_DEFAULT_RUNS_DIR)
        )
        self._persist_lock = threading.Lock()

        # Apollo 风格场景检测 —— decode 线程每帧调用 _update_scene_detection
        # 累积 HSV 直方图，BHATTACHARYYA 距离超阈值的帧号写入 _scene_boundaries。
        # _run_summary 按场景切块而非固定帧数。
        self._scenes_lock = threading.Lock()
        self._scene_boundaries: List[int] = []  # 升序：每个场景的首帧 frame_pos
        self._last_scene_hist = None  # 最近一次场景的累积 HSV 直方图

        self._stop_event = threading.Event()
        # 反压信号：worker 拿走样本后 set，decode 推下一个样本前必须等它。
        # 启动时即 set（队列空、worker 还没开始），decode 可立即推进。
        self._worker_ready = threading.Event()
        self._worker_ready.set()
        self._decode_thread: Optional[threading.Thread] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._frame_q: "queue.Queue" = queue.Queue(maxsize=1)
        self._broadcaster_lock = threading.Lock()
        self._broadcasters: List[Callable[[dict], None]] = []

    # ------------------------------------------------------------------ #
    # 对外状态
    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict:
        ok, _, wdnai_err = _load_wdnai()
        with self._lock:
            events_count = len(self._events)
        # 监测字段：structured 填充率。修复 SceneFrame → BaseModel 后预期
        # per-frame JSON 命中率从 ~30% 升到 ~50-70%；rescue 兜底后总填
        # 充率 ~99%。前端可据此判断修复效果。
        with self._events_lock:
            structured_filled = sum(
                1 for ev in self._events if ev.structured
            )
        with self._lock:
            return {
                "state":         self._state,
                "error":         self._error,
                "source":        self._source,
                "prompt":        self._prompt,
                "every_n":       self._every_n,
                "is_live":       self._is_live,
                "width":         self._width,
                "height":        self._height,
                "fps":           self._fps,
                "frame_pos":     self._frame_pos,
                "total_frames":  self._total_frames,
                "total_frames_source": getattr(self, "_total_frames_source", "unknown"),
                "sampled":       self._sampled,
                "analyzed":      self._analyzed,
                "dirty_count":   self._dirty_count,
                "events_count":  events_count,
                # 监测：SceneFrame 字段填充情况，null/空字典视为未填充
                "structured_filled": structured_filled,
                "structured_total":  events_count,
                "scene_count":    len(self._scene_boundaries),
                "summary":       self._summary,
                "summary_progress": list(self._summary_progress),
                "available":     ok,
                "init_error":    wdnai_err,
                "started_at":    self._started_at,
                "finished_at":   self._finished_at,
                # run 标识：每次 start() 重新分配 (uuid.uuid4().hex)，供
                # WS handler 跨多次连续分析（同一 WS 连接不重连）区分事件
                # id 空间 —— 见 api/video_file_analysis.py vfa_ws 的
                # last_run_id / last_event_id 重置逻辑。
                "run_id":        self._run_id,
            }

    def events(self) -> list:
        with self._events_lock:
            return [ev.to_dict() for ev in self._events]

    def summary(self) -> dict:
        with self._lock:
            return {"summary": self._summary, "ts": self._summary_ts}

    def clear(self) -> None:
        with self._events_lock:
            self._events.clear()
            self._next_event_id = 1
        with self._lock:
            self._summary = ""
            self._summary_ts = 0.0

    # ------------------------------------------------------------------ #
    # 控制
    # ------------------------------------------------------------------ #
    def start(self, source: str, prompt: str, every_n: int = 30) -> dict:
        ok, _, wdnai_err = _load_wdnai()
        if not ok:
            raise RuntimeError(f"wdnai 不可用: {wdnai_err}")
        if _WdnavVideoCapture is None:
            raise RuntimeError("wdnav.VideoCapture 不可用：原生库缺失")
        if not (source or "").strip():
            raise ValueError("source 不能为空")
        if not (prompt or "").strip():
            raise ValueError("prompt 不能为空")
        try:
            every_n = max(1, int(every_n))
        except (TypeError, ValueError):
            raise ValueError("every_n 必须为整数")

        # 业务级校验：算力是否就绪
        from app.services.compute_registry import get_registry
        try:
            get_registry().get_session("swcap_video_describe")
        except RuntimeError as e:
            raise RuntimeError(str(e)) from e

        # 同一服务串行：若还在跑，先强制收尾（不抛错）。同时把上一
        # 轮的快照落盘（run_id 不同），避免被覆盖丢失。
        if self._state in (self.STATE_RUNNING, self.STATE_OPENING,
                           self.STATE_DONE, self.STATE_STOPPED, self.STATE_ERROR):
            self._teardown(reason="restart", persist=True)

        self._reset_state()
        with self._lock:
            self._run_id = uuid.uuid4().hex
            self._state = self.STATE_OPENING
            self._source = source
            self._prompt = prompt
            self._every_n = every_n
            self._is_live = (
                "://" in source or source.startswith("/dev/video")
            )
            self._started_at = time.time()

        try:
            cap = _WdnavVideoCapture(source, timeout=10.0)
        except Exception as e:
            self._fail(f"打开失败: {e}")
            raise RuntimeError(f"打开失败: {e}") from e
        if not cap.isOpened():
            try:
                cap.release()
            except Exception:
                pass
            self._fail("VideoCapture.isOpened() = False")
            raise RuntimeError(f"cannot open {source!r}")

        with self._lock:
            self._cap = cap
            self._width = cap.width
            self._height = cap.height
            self._fps = float(getattr(cap, "fps", 0.0) or 0.0)
            self._total_frames = 0
            self._state = self.STATE_RUNNING

        # wdnav.VideoCapture 不暴露 total_frames；用 PyAV 单独探一次 metadata
        # （cap 已就绪后再读，不阻塞 decode）。
        total = int(getattr(cap, "total_frames", 0) or 0)
        if total <= 0:
            total = _probe_total_frames(source)
        with self._lock:
            if total > 0:
                self._total_frames = total
            self._total_frames_source = (
                "wdnav" if int(getattr(cap, "total_frames", 0) or 0) > 0
                else ("pyav" if total > 0 else "unknown")
            )
        if total > 0:
            LOG.info("vfa: source=%s total_frames=%d (%s)",
                     source, total, self._total_frames_source)
            # 推一次 state 让前端立刻看到正确的 total_frames
            self._broadcast({"type": "state", **self.snapshot()})

        self._stop_event.clear()
        self._decode_thread = threading.Thread(
            target=self._decode_loop,
            name="vfa-decode",
            daemon=True,
        )
        self._decode_thread.start()
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="vfa-infer",
            daemon=True,
        )
        self._worker_thread.start()
        return self.snapshot()

    def stop(self) -> dict:
        """立即停 —— 不出综述，state='stopped'。"""
        with self._lock:
            if self._state in (self.STATE_IDLE,):
                return self.snapshot()
        self._stop_event.set()
        self._teardown(reason="stopped")
        return self.snapshot()

    def close(self) -> dict:
        self._stop_event.set()
        self._teardown(reason="closed")
        return self.snapshot()

    # ------------------------------------------------------------------ #
    # 广播（WS）
    # ------------------------------------------------------------------ #
    def register_broadcaster(self, fn: Callable[[dict], None]) -> None:
        with self._broadcaster_lock:
            self._broadcasters.append(fn)

    def unregister_broadcaster(self, fn: Callable[[dict], None]) -> None:
        with self._broadcaster_lock:
            try:
                self._broadcasters.remove(fn)
            except ValueError:
                pass

    def _broadcast(self, payload: dict) -> None:
        with self._broadcaster_lock:
            bs = list(self._broadcasters)
        for fn in bs:
            try:
                fn(payload)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # 持久化：runs_dir/<run_id>.json
    # ------------------------------------------------------------------ #
    def _runs_path(self, run_id: str) -> Path:
        return self._runs_dir / f"{run_id}.json"

    def _ensure_runs_dir(self) -> None:
        try:
            self._runs_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:  # pragma: no cover - permission / disk
            LOG.warning("vfa runs dir mkdir failed (%s): %s", self._runs_dir, e)

    def _maybe_persist(self) -> None:
        """在没有 run_id 或 state 仍为 idle 时跳过；其余都落盘。"""
        with self._lock:
            run_id = self._run_id
            state = self._state
        if not run_id:
            return
        if state == self.STATE_IDLE:
            return
        try:
            self._persist()
        except Exception as e:  # pragma: no cover - disk
            LOG.warning("vfa persist failed (%s): %s", run_id, e)

    def _persist(self) -> Optional[Path]:
        """把当前快照写到 runs_dir/<run_id>.json，原子 rename。"""
        with self._lock:
            run_id = self._run_id
            if not run_id:
                return None
            payload = {
                "run_id":      run_id,
                "source":      self._source,
                "prompt":      self._prompt,
                "every_n":     self._every_n,
                "is_live":     self._is_live,
                "state":       self._state,
                "error":       self._error,
                "started_at":  self._started_at,
                "finished_at": self._finished_at,
                "summary":     self._summary,
                "summary_ts":  self._summary_ts,
                # 帧率用于反查界面计算"该帧时间戳"：frame_pos / fps = 绝对秒。
                # 老 run（升级前落盘）没这个字段，load_run_meta 读不到时 fallback 0。
                "fps":         self._fps,
            }
        with self._events_lock:
            payload["events"] = [ev.to_dict() for ev in self._events]

        self._ensure_runs_dir()
        path = self._runs_path(run_id)
        tmp = path.with_suffix(".json.tmp")
        with self._persist_lock:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        return path

    def list_runs(self) -> List[dict]:
        """扫描 runs_dir，按 mtime 倒序返回摘要列表。损坏的 JSON 跳过。

        自愈机制：老 run（升级前落盘）JSON 里没 fps / total_frames 字段，
        列表里读出来都是 0 —— 前端"反查"界面的"帧范围"会显示"总帧数未知"。
        这里在 list 阶段对缺失字段探测一次（PyAV 直接读源文件元数据），
        探测成功后**回填到 JSON**（atomic rename，复用 _persist_lock 防并发），
        下次 list_runs 不再探测。**对所有 list_runs 调用方透明**（历史列表、
        查询 modal 入口等），不需要前端改。
        """
        import os
        self._ensure_runs_dir()
        out: List[dict] = []
        for path in self._runs_dir.glob("*.json"):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            evs = data.get("events") or []
            source = data.get("source", "")
            entry = {
                "run_id":       data.get("run_id") or path.stem,
                "source":       source,
                "prompt":       data.get("prompt", ""),
                "every_n":      data.get("every_n", 30),
                "state":        data.get("state", "unknown"),
                "started_at":   data.get("started_at", 0.0),
                "finished_at":  data.get("finished_at", 0.0),
                "events_count": len(evs) if isinstance(evs, list) else 0,
                "has_summary":  bool(data.get("summary")),
                "path":         str(path),
                # 给"反查"界面"帧范围"做提示用：显示总帧数 + 总时长，让用户
                # 直观感受文件大小、避免乱填超出范围的 frame_min/max。
                # 老 run（升级前落盘）没这俩字段，fallback 0 —— 前端会显示
                # "总帧数未知"。下方"自愈"逻辑会在这次 list 时探测并回填 JSON。
                "total_frames": int(data.get("total_frames") or 0),
                "fps":          float(data.get("fps") or 0.0),
            }
            out.append(entry)

            # 自愈：老 run 缺 fps / total_frames 时探测一次 + 写回 JSON
            if (not entry["total_frames"] or not entry["fps"]) and source:
                probed = self._probe_source_meta(source)
                if probed:
                    if not entry["total_frames"] and probed["total_frames"] > 0:
                        entry["total_frames"] = probed["total_frames"]
                    if not entry["fps"] and probed["fps"] > 0:
                        entry["fps"] = probed["fps"]
                    if (probed["total_frames"] > 0 or probed["fps"] > 0):
                        self._backfill_run_meta(
                            path, data,
                            total_frames=probed["total_frames"],
                            fps=probed["fps"],
                        )
        out.sort(key=lambda r: -(r.get("finished_at") or r.get("started_at") or 0))
        return out

    @staticmethod
    def _probe_source_meta(source: str) -> Optional[dict]:
        """用 PyAV 探测源视频的 fps 和总帧数。失败返回 None（不要阻塞 list_runs）。

        不走 wdnav：PyAV 12.x 之后 libavformat 的 demuxer 比 wdnav 自带的轻量很多，
        只读 metadata 不解码，单个文件 ~50-200ms。10 个 run 第一次 list 也只
        多 1-2 秒。
        """
        if not source or not os.path.isfile(source):
            return None
        try:
            import av
            with av.open(source, timeout=(2.0, 5.0)) as container:
                stream = container.streams.video[0]
                return {
                    "total_frames": int(stream.frames) if stream.frames else 0,
                    "fps": float(stream.average_rate) if stream.average_rate else 0.0,
                }
        except Exception as e:
            LOG.debug("vfa: list_runs 探测 %s 失败: %s", source, e)
            return None

    def _backfill_run_meta(self, path: "Path", data: dict, *,
                          total_frames: int = 0, fps: float = 0.0) -> None:
        """把探测到的 fps / total_frames 回填到 JSON（atomic write）。"""
        if total_frames > 0:
            data["total_frames"] = total_frames
        if fps > 0:
            data["fps"] = fps
        tmp = path.with_suffix(".json.tmp")
        try:
            with self._persist_lock:  # 跟 _persist 共用，防并发写
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, path)
            LOG.debug("vfa: list_runs 回填 meta: %s total_frames=%d fps=%s",
                      path.name, total_frames, fps)
        except Exception as e:
            LOG.warning("vfa: list_runs 回填 %s 失败（忽略）: %s", path.name, e)

    def load_run_meta(self, run_id: str) -> dict:
        """轻量读 run JSON：只取 source / prompt / every_n / total 等元数据，
        不解析 events 数组（动辄几千条，全解析浪费 CPU）。

        用途：取帧 endpoint 只需知道原始视频路径 + 元信息做展示，
        不需要把整个 run 加载到内存替换当前状态。
        """
        path = self._runs_path(run_id)
        if not path.exists():
            raise ValueError(f"run not found: {run_id}")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"corrupt run file: {run_id}")
        return {
            "run_id":       data.get("run_id") or run_id,
            "source":       data.get("source", ""),
            "prompt":       data.get("prompt", ""),
            "every_n":      int(data.get("every_n") or 30),
            "is_live":      bool(data.get("is_live", False)),
            "state":        data.get("state", "unknown"),
            "total_frames": int(data.get("total_frames") or 0),
            # 老 run（升级前落盘的）没 fps 字段，load 后默认 0，前端 detect 到会
            # 不显示"该帧时间戳"那行而不是显示 00:00。
            "fps":          float(data.get("fps") or 0.0),
        }

    def load_run(self, run_id: str) -> dict:
        """把指定 run_id 的 JSON 加载到内存（替换当前状态）。"""
        path = self._runs_path(run_id)
        if not path.exists():
            raise ValueError(f"run not found: {run_id}")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"corrupt run file: {run_id}")
        # 先把当前活跃 run（若有）落盘
        with self._lock:
            prev_state = self._state
        if prev_state != self.STATE_IDLE and self._run_id and self._run_id != run_id:
            self._maybe_persist()
        # 停掉当前的 decode / worker
        self._stop_event.set()
        for t in (self._decode_thread, self._worker_thread):
            if t is not None and t.is_alive():
                t.join(timeout=1.5)
        self._decode_thread = None
        self._worker_thread = None
        with self._lock:
            cap = self._cap
            self._cap = None
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
        # 清空帧队列，避免上一轮的 stale 帧被新 worker 误拾
        while True:
            try:
                self._frame_q.get_nowait()
            except queue.Empty:
                break

        evs_raw = data.get("events") or []
        evs: List[FrameEvent] = []
        max_id = 0
        for raw in evs_raw:
            if not isinstance(raw, dict):
                continue
            try:
                fe = FrameEvent(
                    id=int(raw.get("id", 0)),
                    frame_pos=int(raw.get("frame_pos", 0)),
                    ts=float(raw.get("ts", 0.0)),
                    prompt=str(raw.get("prompt", "")),
                    text=str(raw.get("text", "")),
                    structured=raw.get("structured"),
                )
            except Exception:
                continue
            evs.append(fe)
            if fe.id > max_id:
                max_id = fe.id

        with self._lock:
            self._run_id = run_id
            self._source = data.get("source", "")
            self._prompt = data.get("prompt", "")
            self._every_n = int(data.get("every_n", 30) or 30)
            self._is_live = bool(data.get("is_live", False))
            self._state = data.get("state", self.STATE_DONE)
            self._error = data.get("error", "")
            self._started_at = float(data.get("started_at", 0.0))
            self._finished_at = float(data.get("finished_at", 0.0))
            self._summary = data.get("summary", "")
            self._summary_ts = float(data.get("summary_ts", 0.0))
            self._sampled = len(evs)
            self._analyzed = len(evs)
            self._frame_pos = max((e.frame_pos for e in evs), default=0)
        with self._events_lock:
            self._events.clear()
            for fe in evs:
                # 清洗：历史 run（落盘前 regex bug）actor 列表里可能混进
                # "车型" 这类非人关键词 —— 加载时一并剔掉。
                if fe.structured and isinstance(fe.structured.get("actors"), list):
                    cleaned = _sanitize_actors(fe.structured["actors"])
                    if len(cleaned) != len(fe.structured["actors"]):
                        LOG.warning(
                            "vfa: 清洗 run=%s frame=%d actors: %s → %s",
                            run_id[:8], fe.frame_pos,
                            fe.structured["actors"], cleaned,
                        )
                        fe.structured["actors"] = cleaned
                self._events.append(fe)
            self._next_event_id = max_id + 1
        # 加载完 → 状态必须是 idle（已脱离任何运行），否则下次 start 会拒
        with self._lock:
            self._state = self.STATE_IDLE
            self._error = ""
        self._broadcast({"type": "state", **self.snapshot()})
        return self.snapshot()

    def delete_run(self, run_id: str) -> bool:
        path = self._runs_path(run_id)
        if not path.exists():
            return False
        with self._persist_lock:
            try:
                path.unlink()
            except FileNotFoundError:
                return False
        return True

    # ------------------------------------------------------------------ #
    # 内部：reset / teardown / fail
    # ------------------------------------------------------------------ #
    def _reset_state(self) -> None:
        with self._events_lock:
            self._events.clear()
            self._next_event_id = 1
        with self._scenes_lock:
            self._scene_boundaries.clear()  # 清场景边界
            self._last_scene_hist = None
        with self._lock:
            self._summary = ""
            self._summary_ts = 0.0
            self._error = ""
            self._frame_pos = 0
            self._sampled = 0
            self._analyzed = 0
            self._dirty_count = 0
            self._total_frames = 0
            self._width = 0
            self._height = 0
            self._fps = 0.0
            self._finished_at = 0.0
        # 清空帧队列 + 重置反压信号，让新 run 的 decode 线程立即可推
        while True:
            try:
                self._frame_q.get_nowait()
            except queue.Empty:
                break
        self._worker_ready.set()

    def _update_scene_detection(self, frame_bgr, cur_pos: int) -> None:
        """每帧调一次。HSV 0/1 通道 (Hue+Sat) 直方图 + BHATTACHARYYA 距离
        累积式比较，超阈值即视为场景切换。

        accumulateWeighted 让直方图跟随帧渐变（α=0.1），能容忍渐变场景
        （比如镜头缓慢拉远）不被误切为多个场景。

        **stale run 防护**：若调用方所属 run 已被新 run 取代（_run_id
        不再是入口值），不再写 _last_scene_hist / _scene_boundaries，
        否则会污染新 run 的场景检测状态。
        """
        import cv2
        try:
            hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
            hist = cv2.calcHist(
                [hsv], [0, 1], None,
                list(SCENE_HIST_BINS),
                [0, 180, 0, 256],
            )
            hist = cv2.normalize(hist, hist).flatten()
        except Exception:
            return
        # stale run 检查：若 _run_id 已被新 run 覆盖（decode 线程 _teardown
        # join 超时还在跑），不要写 _last_scene_hist / _scene_boundaries
        # —— 这些状态已被 _reset_state 清空，写入会污染新 run。
        run_id_at_start = self._run_id
        with self._scenes_lock:
            if self._run_id != run_id_at_start:
                return
            if self._last_scene_hist is None:
                self._last_scene_hist = hist
                self._scene_boundaries.append(cur_pos)
                return
            try:
                distance = cv2.compareHist(
                    self._last_scene_hist, hist, cv2.HISTCMP_BHATTACHARYYA,
                )
            except Exception:
                return
            if distance > SCENE_BHATTACHARYYA_THRESH:
                # 场景切换 → 新场景首帧 = cur_pos
                self._last_scene_hist = hist
                self._scene_boundaries.append(cur_pos)
            else:
                # 渐变累积
                try:
                    cv2.accumulateWeighted(
                        hist, self._last_scene_hist, SCENE_DECAY_ALPHA,
                    )
                except Exception:
                    pass

    def _fail(self, msg: str) -> None:
        with self._lock:
            self._state = self.STATE_ERROR
            self._error = msg
            self._finished_at = time.time()
        self._maybe_persist()

    def _teardown(self, reason: str, persist: bool = True) -> None:
        with self._lock:
            cap = self._cap
            self._cap = None
            # 自然 EOF 路径会在 _decode_loop 里把状态切到 done；
            # 这里只覆盖 stop / close 触发的早停。
            if self._state == self.STATE_RUNNING:
                self._state = (
                    self.STATE_STOPPED if reason == "stopped" else self.STATE_IDLE
                )
                self._finished_at = time.time()
        # 持久化要在 _run_id 还在的时候做；reset_state 不清 _run_id，
        # 但下一轮 start() 会赋新值 —— 所以这里只持久化"上一轮"。
        if persist:
            self._maybe_persist()
        self._stop_event.set()
        # 解码线程结束条件：_stop_event.is_set() 或 cap 释放后 read() 抛错。
        for t in (self._decode_thread, self._worker_thread):
            if t is not None and t.is_alive():
                t.join(timeout=1.5)
        self._decode_thread = None
        self._worker_thread = None
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
            # 缓解 wdnav-shim DMA buffer leak（mpp_buffer deinit 警告）：
            # wdnav.VideoCapture.release() 不释放所有 DMA buffer（vendor bug），
            # 但 Python 端如有 lingering 引用会延迟 native 端回收。强制 drop
            # 引用 + GC 可减少 leak 速率（不能根治）。详见日志
            # ``mpp_buffer: ... with 7311360 bytes not released``。
            cap = None
            import gc
            gc.collect()
        # 推一次 state change 给 WS
        self._broadcast({"type": "state", **self.snapshot()})

    # ------------------------------------------------------------------ #
    # Decode 线程：读帧 → 每 N 帧入推理队列
    # ------------------------------------------------------------------ #
    def _decode_loop(self) -> None:
        """解码线程：自由读帧，但在 push 每个采样前必须等 worker 把上一
        个样本拿走（``_worker_ready`` 反压），不丢任何应分析的帧。"""
        with self._lock:
            cap = self._cap
        if cap is None:
            return
        while not self._stop_event.is_set():
            try:
                ret, frame = cap.read()
            except Exception as e:  # pragma: no cover
                self._fail(f"read() 异常: {e}")
                self._broadcast({"type": "state", **self.snapshot()})
                return
            if not ret or frame is None:
                # 自然 EOF —— 必须把 sentinel 真的塞进队列，否则 worker
                # 永远等不到综述触发。满了就等 worker 拉走再重试。
                while True:
                    try:
                        self._frame_q.put_nowait(None)
                        break
                    except queue.Full:
                        # 等 worker 把上一个样本拿走（反压让位）
                        if not self._worker_ready.wait(timeout=0.1):
                            pass
                        if self._stop_event.is_set():
                            return
                return
            with self._lock:
                self._frame_pos = int(getattr(cap, "frame_pos", self._frame_pos) or 0)
                cur_pos = self._frame_pos
                every_n = self._every_n
            # 场景检测 —— 每帧调用（cv2 操作很便宜）
            self._update_scene_detection(frame, cur_pos)
            # 取样判定 —— 反压点：worker 还在推理前一个样本时，decode 停
            # 在这里直到 worker 把上一个样本从队列里 get 走（set event）。
            if cur_pos > 0 and cur_pos % every_n == 0:
                # 等 worker 准备好（短超时轮询，方便响应 stop）
                while not self._stop_event.is_set():
                    if self._worker_ready.wait(timeout=0.1):
                        break
                if self._stop_event.is_set():
                    return
                # 此时队列空，可以独占 push
                self._frame_q.put_nowait((frame, cur_pos))
                # 自己把门关上：worker 还没 get 之前 decode 别再推下一个样本
                self._worker_ready.clear()
                with self._lock:
                    self._sampled += 1

    # ------------------------------------------------------------------ #
    # Worker 线程：调 VLM + 写事件 + EOF 综述
    # ------------------------------------------------------------------ #
    def _worker_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    item = self._frame_q.get(timeout=1.0)
                except queue.Empty:
                    continue
                if item is None:
                    # decode 线程发来的 EOF sentinel → 跑综述
                    self._run_summary()
                    return
                frame_bgr, frame_pos = item
                # 立刻开门让 decode 推下一个样本（pipeline 并行）。
                # 必须在 _infer_one 之前 set，否则 decode 会空等整个推理周期。
                self._worker_ready.set()
                self._infer_one(frame_bgr, frame_pos)
            # stop_event 被 set：用户中途停了
            with self._lock:
                self._finished_at = time.time()
            # 退出时把门开着，避免下一轮 start() 之前任何残留 decode
            # 线程因为误判 wait 而卡住（虽然正常 teardown 会 join）
            self._worker_ready.set()
        except Exception as e:  # pragma: no cover
            self._fail(f"worker 异常: {e}")
            self._worker_ready.set()
            self._broadcast({"type": "state", **self.snapshot()})

    def _infer_one(self, frame_bgr, frame_pos: int) -> None:
        # 捕获入口 _run_id；后续每次状态写操作前比对，过期则跳过。
        # _teardown 时 join 超时（1.5s），旧 worker 可能仍在 _call_llm
        # 等待 reinit（最长 60s）；若该等待期间新 start() 已开始，旧 worker
        # 完成 LLM 调用后**不应**把结果加到新 run 的 events 列表里，
        # 否则前端会看到"上一视频的最后一帧塞到当前 run 末尾"。
        run_id_at_start = self._run_id
        with self._lock:
            prompt = self._prompt
        # 走结构化输出（@Generable SceneFrame）：每个事件都带
        # ``structured`` dict 落盘，方便后续按 scene / actor / object 反查。
        # 用户 prompt 仍可自定义（per-frame 提问），SDK 自动追加 JSON
        # schema hint 让模型按字段输出。
        #
        # max_new_tokens=300（旧版 200）：5 字段 JSON 含 30-100 字中文 text
        # 实测 120-180 token，200 太紧偶尔截断出半截 JSON → 兜底清洗后
        # 出现"乱码"。300 留 buffer，避免在中文 + JSON 语法双重开销下
        # 切到错误路径。
        #
        # **乱码处理策略（与 VTD 对齐）**：Qwen3-VL-4B 在 RKNN3 上首 token
        # 不稳定，约 ~15% 帧会产出 token salad（CJK 比例 <30% + 重复
        # Latin 子串等）。SDK Path 5 兜底会把 raw text 包成
        # SceneFrame(text=garbage, 其他=default)。
        #
        # **不做 retry**（v0.5.x 起）：spec 默认 temperature=1e-6 +
        # top_k=1（near-greedy），且 ``RKNN3Lite.session_run`` 不支持
        # per-call 覆盖这两个参数。同 prompt + 同 image 重复调用 N 次
        # 产生**完全相同的输出**——retry 徒劳。
        #
        # **静默丢弃策略**：仿 VTD ``detector.py:_infer_sync`` 行为——
        # 对 SDK Path 5 兜底返回的 ``str`` 走 ``getattr(str, "text", "")``
        # 自然拿到空串。VFA 端 ``_split_scene_frame_result`` 检测到
        # garbage 时返回 ``(None, "")``，本方法构造**空事件 + _dirty=True**：
        #   text="",
        #   structured={"scene":"","actors":[],"action":"","objects":[],
        #               "note":"(本帧分析失败：模型输出乱码)"},
        #   _dirty=True
        # 前端看到"空分析"而非乱码；落盘 JSON 保留 note 用于事后排查。
        SceneFrame = _get_scene_frame_cls()
        result = self._call_llm(
            prompt, frame_bgr, generating=SceneFrame,
            max_new_tokens=300,
        )
        structured_dict, raw_text = _split_scene_frame_result(result)

        # 失败标记：_call_llm 把 SDK 错误返回成 "(功能未启用: ...)" /
        # "(推理超时: ...)" / "(推理失败: ...)" 等字符串。这种情况下
        # structured_dict 是 None（_split_scene_frame_result 失败路径），
        # 前端没法识别。让 structured_dict 至少含一个 error 字段让前端
        # 显示"分析失败"占位。
        inference_failed = bool(raw_text and raw_text.startswith("(") and raw_text.endswith(")"))
        if inference_failed:
            err_text = raw_text
            structured_dict = {
                "text": err_text,  # "(推理失败: xxx)" 完整保留给前端展示
                "_inference_failed": True,
                "scene": "",
                "actors": [],
                "action": "",
                "objects": [],
                "note": err_text,
            }
            text = err_text  # 错误占位不洗，避免吞掉错误信息
            is_dirty = False
        else:
            # 乱码帧已被 _split_scene_frame_result 静默丢弃（返回 text=""），
            # 这里再二次校验 raw_text 防漏。
            if not raw_text and structured_dict is None:
                # structured_dict is None + raw_text == "" → 静默丢弃路径
                structured_dict = {
                    "scene": "",
                    "actors": [],
                    "action": "",
                    "objects": [],
                    "note": "(本帧分析失败：模型输出乱码)",
                }
                text = ""
                is_dirty = True
            elif structured_dict is None:
                # 罕见：structured_dict is None + raw_text 非空但 sanitize 后空
                # （regex 抽不出结构化字段）。同样标 dirty，让 UI 知情。
                text = _sanitize_text(raw_text, max_len=500) or ""
                structured_dict = {
                    "scene": "",
                    "actors": [],
                    "action": "",
                    "objects": [],
                    "note": "(本帧未能解析结构化字段)",
                }
                is_dirty = True
            else:
                # 正常路径：清洗 raw_text 去除 think 块 / markdown 头 / 控制字符 /
                # 部分 JSON 残留，避免前端事件列表显示残留噪声。
                text = _sanitize_text(raw_text, max_len=500)
                is_dirty = False

        # per-frame 保护：模型可能输出超长 text（>500 字符罕见，但发生会污染
        # 结构化字段大小、拖慢后续处理）。截断 text 字段到 500 字符内，
        # 保留 "..." 后缀表示截断；structured.text 也同步截断。
        if text and len(text) > 500:
            text = text[:500] + "…"
        if structured_dict and structured_dict.get("text") and len(structured_dict["text"]) > 500:
            structured_dict["text"] = structured_dict["text"][:500] + "…"

        # stale run 检查：本 worker 可能因 _teardown join 超时还在跑，
        # 等到 _call_llm 返回时新 run 已开始（_run_id 被覆盖）。这种
        # 情况下**不要**把旧 run 的结果写进 self._events（已被 _reset_state
        # 清空，会污染新 run）；也不要 broadcast（前端已切到新 run 的 WS）。
        if self._run_id != run_id_at_start:
            LOG.debug(
                "vfa: _infer_one 检测到 stale run（frame_pos=%d），丢弃结果",
                frame_pos,
            )
            return

        with self._lock:
            self._analyzed += 1
            if is_dirty:
                self._dirty_count += 1
        with self._events_lock:
            ev = FrameEvent(
                id=self._next_event_id,
                frame_pos=frame_pos,
                ts=time.time(),
                prompt=prompt,
                text=text,
                structured=structured_dict,
                _dirty=is_dirty,
            )
            self._next_event_id += 1
            self._events.append(ev)
            d = ev.to_dict()
        self._broadcast({"type": "event", "event": d})

        # 注意：早期实现里每 30 events 在 worker 线程同步跑一次 batch rescue
        # 提取 structured，但 30 events × 500 字符的 prompt 极易超出 Qwen3-VL
        # 8192 token 上限，触发 RKNN3 native abort / hang；abort 路径下的
        # daemon 线程还会污染后续 session_run，连锁导致后续 _call_llm 全部
        # 35s 超时，整次分析卡死。改方案：rescue 推迟到 _run_summary 开头
        # 跑一次，由字符预算（RESCUE_PROMPT_CHAR_BUDGET）严格控制单批
        # events 数，确保 prompt 落在 NPU 上下文内。
        # 这里不再做 chunk-level rescue。

    def _rescue_structured_batch(self) -> int:
        """对所有 structured=None 的 events 跑 batch LLM 兜底。

        Qwen3-VL-4B 在 RKNN3 上不稳定输出 JSON，导致 80%+ events 落盘
        时 structured=None。这步在 EOF 后综述前调用，把整批事件的 text
        转成结构化字段。

        单批 events 数受 ``RESCUE_PROMPT_CHAR_BUDGET`` 控制 —— 旧实现
        一锅炖所有 events（典型 100+ events × 500 chars ≈ 50000+ 字符），
        远超 Qwen3-VL 8192 token 窗口，触发 RKNN3 native abort / hang。
        这里按累积字符切成 ≤ RESCUE_MAX_EVENTS_PER_CALL 的多个子批，逐
        批调 LLM，避开 prompt 超窗导致的整次分析卡死。

        返回成功提取数量。"""
        with self._events_lock:
            unstruc = [ev for ev in self._events
                       if not ev.structured
                       and ev.text
                       and not ev.text.startswith("(")]  # 跳过错误路径
        if not unstruc:
            return 0

        # 按字符预算切批：每批累积文本 + 模板（~200 chars）≤ RESCUE_PROMPT_CHAR_BUDGET
        def _fits(ev_text_len: int, cur: int, max_events: int) -> bool:
            return (
                cur + ev_text_len + 80 <= RESCUE_PROMPT_CHAR_BUDGET
                and max_events < RESCUE_MAX_EVENTS_PER_CALL
            )

        batches: List[list] = []
        cur_batch: list = []
        cur_chars = 0
        for ev in unstruc:
            ev_len = len(ev.text or "")
            if not _fits(ev_len, cur_chars, len(cur_batch)) and cur_batch:
                batches.append(cur_batch)
                cur_batch = []
                cur_chars = 0
            cur_batch.append(ev)
            cur_chars += ev_len
        if cur_batch:
            batches.append(cur_batch)

        if len(batches) > 1:
            LOG.info(
                "vfa: rescue 分 %d 批（unstruc=%d，char_budget=%d）",
                len(batches), len(unstruc), RESCUE_PROMPT_CHAR_BUDGET,
            )

        rescued_total = 0
        for batch_idx, batch in enumerate(batches):
            if self._stop_event.is_set():
                break
            prompt, _order = _build_rescue_prompt(batch)
            try:
                raw = self._call_llm(
                    prompt, None,
                    max_new_tokens=min(2000, 80 * len(batch) + 200),
                )
            except Exception as e:
                LOG.warning(
                    "vfa: rescue 批 %d/%d LLM 调用失败: %s",
                    batch_idx + 1, len(batches), e,
                )
                continue
            if not raw or raw.startswith("("):
                LOG.warning(
                    "vfa: rescue 批 %d/%d LLM 不可用: %s",
                    batch_idx + 1, len(batches), (raw or "")[:60],
                )
                continue
            # 按行 parse：模型应输出 N 行 JSON（按 events 顺序）
            lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            ev_by_pos = {ev.frame_pos: ev for ev in batch}
            used_ids = set()
            parsed: list = []
            for ln in lines:
                m = re.search(r"\[帧\s*(\d+)\]", ln)
                if m:
                    fp = int(m.group(1))
                    if fp in ev_by_pos and id(ev_by_pos[fp]) not in used_ids:
                        obj = _parse_rescue_line(ln)
                        if obj:
                            parsed.append((fp, obj))
                            used_ids.add(id(ev_by_pos[fp]))
                            continue
                # fallback：按顺序配
                for ev in batch:
                    if id(ev) in used_ids:
                        continue
                    obj = _parse_rescue_line(ln)
                    if obj:
                        parsed.append((ev.frame_pos, obj))
                        used_ids.add(id(ev))
                        break
            # 回填
            rescued_batch = 0
            with self._events_lock:
                for fp, obj in parsed:
                    ev = ev_by_pos.get(fp)
                    if ev is None:
                        continue
                    ev.structured = obj
                    self._broadcast({"type": "event", "event": ev.to_dict()})
                    rescued_batch += 1
            rescued_total += rescued_batch
            LOG.info(
                "vfa: rescue 批 %d/%d 成功 %d/%d",
                batch_idx + 1, len(batches), rescued_batch, len(batch),
            )
        if rescued_total > 0:
            self._maybe_persist()
        return rescued_total

    def _run_summary(self) -> None:
        """综述生成 —— Apollo 三段式（场景检测 → per-scene 小结 → 最终综述）。

        1. 场景检测（已在线完成）：_decode_loop 每帧调 _update_scene_detection，
           记录 _scene_boundaries（每个场景首帧 frame_pos）。
        2. 救援兜底：扫一遍 events，对 structured=None 的批量调 LLM 抽
           structured（一次调用处理一批事件，amortized 成本）。
        3. per-scene 小结：把 events 按 _scene_boundaries 分组；超大场景按
           SUMMARY_CHUNK_SIZE 兜底切。每段独立 LLM 调用生成 ≤120 字小结，
           用 max_new_tokens=200 硬控输出。
        4. 最终综述：所有 scene 小结拼起来 → LLM 生成 ≤300 字综述。

        关键：所有路径都在 try/finally 保护下，**finally 强制设置 STATE_DONE**
        并 broadcast state，避免任何 LLM 异常把前端卡在"分析中"。

        **stale run 防护**：若 ``_teardown`` 调用时本 run_summary 还在跑
        （join 超时 1.5s 后新 run 已开始），本方法的 finally 会把
        ``_state`` 改回 "stopped" → 污染新 run 的 state。所有关键状态
        写操作前检查 ``_run_id`` 是否仍是进入时的 run（被新 run 覆盖即
        直接 return），避免 stale 完成污染新 run。
        """
        # 捕获入口 _run_id；后续每次状态写操作前比对，过期则跳过
        run_id_at_start = self._run_id

        # 全部 try/except/finally 包裹，强制设置 STATE_DONE
        # 避免任何 LLM 异常让前端永远"分析中"
        evs = []
        # 关键：进入 _run_summary 后**立即**把 state 切到 STATE_SUMMARIZING，
        # 并广播。前端用这个状态显示 "生成综述中 N/M"，UI 不再卡在 "分析中"。
        # _decode_loop 已发 EOF sentinel 意味着 per-frame 全部完成；
        # 这时继续显示 "分析中" 是误导用户。
        with self._lock:
            if self._run_id != run_id_at_start:
                # stale: 新 run 已接管，不要切到 SUMMARIZING 污染新 run
                return
            self._state = self.STATE_SUMMARIZING
            self._summary = ""
            self._summary_ts = 0.0
        self._broadcast({"type": "state", **self.snapshot()})

        try:
            with self._events_lock:
                evs = sorted(self._events, key=lambda e: e.frame_pos)
            if not evs:
                if self._run_id != run_id_at_start:
                    return
                with self._lock:
                    self._state = "done"
                    self._finished_at = time.time()
                    self._broadcast({"type": "state", **self.snapshot()})
                self._maybe_persist()
                return

            # 0. 兜底：批量 LLM 提取 structured=None 的 events
            # 推迟到这里（不在 _infer_one 里跑）—— 之前嵌在每 30 events 同步
            # 跑 batch rescue，prompt 极易超 8192 token 上限，触发 RKNN3 卡死。
            if self._stop_event.is_set():
                return
            if self._run_id != run_id_at_start:
                return
            self._rescue_structured_batch()

            # 单帧文本截断（更紧凑：SUMMARY_FRAME_TEXT_MAX=60 字符）
            truncated: List[tuple] = []
            for e in evs:
                text = (e.text or "").strip()
                if len(text) > SUMMARY_FRAME_TEXT_MAX:
                    text = text[:SUMMARY_FRAME_TEXT_MAX] + "…"
                truncated.append((e.frame_pos, text))

            # 1. 按场景切块
            scene_ranges = self._group_by_scenes(truncated)
            chunks: List[List[tuple]] = []
            for start, end in scene_ranges:
                scene_chunk = truncated[start:end]
                if len(scene_chunk) <= SUMMARY_CHUNK_SIZE:
                    chunks.append(scene_chunk)
                else:
                    for j in range(0, len(scene_chunk), SUMMARY_CHUNK_SIZE):
                        chunks.append(scene_chunk[j:j + SUMMARY_CHUNK_SIZE])

            total_chunks = len(chunks)
            LOG.info("vfa: 综述 %d 个 chunk，共 %d 帧", total_chunks, len(evs))

            # 2. per-chunk 小结 —— 关键：每个 chunk 都校验 prompt 字符预算，
            # 避免 _format_chunk_lines 在 structured 全空时 fallback 到 500 字符
            # 文本 × 15 帧 ≈ 7500 字符，叠加模板接近 8000 字符，刚好在窗口边缘，
            # RKNN3 偶发 abort。主动裁到 SUMMARY_CHUNK_PROMPT_BUDGET。
            chunk_summaries: List[str] = []
            for idx, chunk in enumerate(chunks):
                if self._stop_event.is_set():
                    return
                lines = self._format_chunk_lines(chunk)
                # 字符预算：lines 过长时按帧数等比截断（保留首尾）
                header_overhead = (
                    len(CHUNK_SUMMARY_PROMPT.format(
                        frame_count=len(chunk),
                        first_frame=chunk[0][0],
                        last_frame=chunk[-1][0],
                        lines="",
                    ))
                )
                max_lines_chars = max(
                    400, SUMMARY_CHUNK_PROMPT_BUDGET - header_overhead
                )
                if len(lines) > max_lines_chars:
                    LOG.warning(
                        "vfa: chunk %d/%d lines 超 %d 字符（%d），裁剪",
                        idx + 1, total_chunks, max_lines_chars, len(lines),
                    )
                    lines = _truncate_lines_keep_edges(
                        lines, max_lines_chars,
                    )
                prompt = CHUNK_SUMMARY_PROMPT.format(
                    frame_count=len(chunk),
                    first_frame=chunk[0][0],
                    last_frame=chunk[-1][0],
                    lines=lines,
                )
                text = self._call_llm(prompt, None, max_new_tokens=CHUNK_MAX_NEW_TOKENS)
                if not text or text.startswith("("):
                    LOG.warning(
                        "vfa: chunk %d/%d summary failed: %s",
                        idx + 1, total_chunks, (text or "(空)")[:60],
                    )
                    text = f"(本段 {len(chunk)} 帧：综述生成失败)"
                # 综述阶段：每个 chunk 小结也走一遍 _sanitize_text 防止模型
                # 在 free-form 文本里输出 markdown / 控制字符污染最终综述。
                text = _sanitize_text(text, max_len=400)
                if not text:
                    text = f"(本段 {len(chunk)} 帧：综述生成失败)"
                chunk_summaries.append(text)
                # 每完成一个 chunk 就广播状态 + summary_progress，让前端看到进度
                with self._lock:
                    self._summary_progress = (idx + 1, total_chunks)
                self._broadcast({"type": "state", **self.snapshot()})

            # 3. 最终综述
            if len(chunk_summaries) == 1:
                summary = chunk_summaries[0]
            else:
                parts = "\n".join(
                    f"[场景 {idx + 1}] {s}" for idx, s in enumerate(chunk_summaries)
                )
                if len(parts) > SUMMARY_PROMPT_CHAR_BUDGET:
                    LOG.warning(
                        "vfa: summary parts too long (%d chars), truncating",
                        len(parts),
                    )
                    parts = parts[:SUMMARY_PROMPT_CHAR_BUDGET] + "\n…(后续省略)"
                final_prompt = FINAL_SUMMARY_PROMPT.format(
                    scene_count=len(chunk_summaries),
                    parts=parts,
                )
                summary = self._call_llm(
                    final_prompt, None, max_new_tokens=FINAL_MAX_NEW_TOKENS,
                )
                if not summary or summary.startswith("("):
                    LOG.warning(
                        "vfa: final summary failed: %s", (summary or "(空)")[:60],
                    )
                    # 兜底：把 scene 小结直接拼起来当综述（避免卡在 "分析中"）
                    summary = "\n".join(
                        f"【场景 {i + 1}】{s}" for i, s in enumerate(chunk_summaries)
                    )

            # 最终清洗：去掉 markdown / 控制字符 / 替换字符（如果模型在最终
            # 输出里塞了这些，用户在综述框看到的就是 "乱码"）
            summary_clean = _sanitize_text(summary, max_len=2000)
            if not summary_clean:
                summary_clean = summary.strip() if summary else ""

            # stale 检查：若 _teardown 让新 run 接管了 self._run_id，
            # 此处不要再写 self._summary / self._state —— 否则会污染新 run。
            if self._run_id == run_id_at_start:
                with self._lock:
                    self._summary = summary_clean
                    self._summary_ts = time.time()
                    self._state = "done"
                    self._summary_progress = (0, 0)
                    self._finished_at = time.time()
                self._broadcast({"type": "summary", **self.summary()})
                self._broadcast({"type": "state", **self.snapshot()})
                self._maybe_persist()
        except Exception as e:
            LOG.error("vfa: _run_summary 异常: %s（已降级为简单摘要）", e)
            # 任何异常 → 简单摘要（避免卡死）
            if self._run_id == run_id_at_start:
                with self._lock:
                    self._summary = f"（综述生成失败：{e}）"
                    self._summary_ts = time.time()
                    self._state = "done"
                    self._summary_progress = (0, 0)
                    self._finished_at = time.time()
                self._broadcast({"type": "summary", **self.summary()})
        finally:
            # 关键：无论如何都设置 STATE_DONE（除非用户取消），
            # 避免前端永远"分析中"。但 stale run 不能污染新 run 的状态。
            if self._run_id == run_id_at_start:
                if self._stop_event.is_set():
                    with self._lock:
                        self._state = "stopped"
                        self._summary_progress = (0, 0)
                        self._finished_at = time.time()
                else:
                    with self._lock:
                        if self._state not in ("done", "stopped", "error"):
                            self._state = "done"
                            self._summary_progress = (0, 0)
                            self._finished_at = time.time()
                self._broadcast({"type": "state", **self.snapshot()})

    def _group_by_scenes(self, truncated: List[tuple]) -> List[tuple]:
        """按 _scene_boundaries 把 events 切片。

        返回 [(start_idx, end_idx), ...] —— 每对对应一个场景在
        truncated 列表里的 [start, end) 范围。
        """
        if not truncated:
            return []
        with self._scenes_lock:
            # 取出所有场景首帧 frame_pos
            scene_starts = sorted(set(self._scene_boundaries))
        if not scene_starts:
            return [(0, len(truncated))]
        # 把 truncated 按 scene_starts 切片
        # truncated 是按 frame_pos 升序的，binary-search 找每个场景起始 idx
        ranges: List[tuple] = []
        prev_idx = 0
        prev_start_frame = truncated[0][0]
        for s_frame in scene_starts:
            # 找 truncated 里第一个 frame_pos >= s_frame 的 idx
            # 线性扫就够了（truncated 不长，场景数也少）
            for i in range(prev_idx, len(truncated)):
                if truncated[i][0] >= s_frame:
                    ranges.append((prev_idx, i))
                    prev_idx = i
                    break
            else:
                # s_frame 比所有事件都大
                break
        # 最后一个场景到末尾
        ranges.append((prev_idx, len(truncated)))
        # 过滤空场景（边界相邻但同帧）
        return [(s, e) for s, e in ranges if e > s]

    def _format_chunk_lines(self, chunk: list) -> str:
        """把一个 chunk 的 events 序列化成给 LLM 的 prompt lines。

        优先用 structured 字段压缩：``[帧 X] scene: action [obj1, obj2]``。
        没 structured 的（旧 run 或推理失败）退回 ``[帧 X] text``。
        """
        # chunk 里是 (frame_pos, text)；要拿 structured 必须从 events 表查
        with self._events_lock:
            # 建一个 frame_pos -> structured 的快速查表
            by_frame = {e.frame_pos: e.structured for e in self._events}
        lines: List[str] = []
        for fp, text in chunk:
            structured = by_frame.get(fp)
            if structured and isinstance(structured, dict):
                scene = structured.get("scene") or ""
                action = structured.get("action") or ""
                objects = structured.get("objects") or []
                actors = structured.get("actors") or []
                bits = [scene, action]
                tail = []
                if objects:
                    tail.append(f"物:{','.join(objects)}")
                if actors:
                    tail.append(f"人:{','.join(actors)}")
                base = ":".join(b for b in bits if b).strip(":") or text
                line = f"[帧 {fp}] {base}"
                if tail:
                    line += " (" + " | ".join(tail) + ")"
                lines.append(line)
            else:
                lines.append(f"[帧 {fp}] {text}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # LLM 调用：LLMSession 来自 registry；session.respond 前后包进程锁
    # ------------------------------------------------------------------ #
    def _call_llm(self, prompt: str, frame_bgr, *,
                  max_new_tokens: Optional[int] = None,
                  generating=None) -> Any:
        """调 LLMSession。

        ``max_new_tokens`` 透传给 rknn3 session_run（通过 ``**overrides``），
        用来硬控输出 token 数，避免 chunk 摘要无止境生成挤爆最终 prompt
        窗口。

        ``generating`` 是可选的 @Generable 类 / BaseModel 子类；传了就
        让模型按 JSON schema 输出，SDK 自动追加 schema hint 并把结果
        parse 成对应类的实例（不是 str）。调用方拿到原始返回值自己处
        理 —— 综述路径继续用 str，per-frame 路径取 pydantic 字段。

        **prompt 改造（历史 + 回滚开关）**：
        旧实现会在 ``generating=SceneFrame`` 时在 user prompt 前硬塞一
        段中文 JSON 指令 + 示例 JSON。问题：与 SDK 自动追加的 schema hint
        形成"两份指令打架"，且模型偶尔把示例里的 ``<30-100字描述>`` 占
        位符当模板抄回去当输出（"乱码"）。修复后 SceneFrame 继承
        ``BaseModel``（不再是 ``LMOutput``），SDK 拿到完整 JSON Schema，
        schema hint 本身就够强，不再需要手工 prompt 改造。

        **回滚开关** ``VFA_FORCE_JSON_PROMPT=1``：若改动导致填充率反
        而下降，可设该环境变量恢复旧 prompt 改造（不推荐，仅作为
        紧急回滚口）。
        """
        from app.services.compute_registry import get_registry, get_session_call_lock
        try:
            sess = get_registry().get_session("swcap_video_describe")
        except RuntimeError as e:
            # get_session 抛 RuntimeError("功能未启用: <cap>")，直接包裹
            # 不再加 "功能未启用: " 前缀（否则会出现双重前缀）。
            return f"({e})"

        ok, Attachment, wdnai_err = _load_wdnai()
        if not ok:
            return f"(wdnai 不可用: {wdnai_err})"

        # 回滚开关：env var "1" 时套用旧 prompt 改造（紧急回滚口）
        if (
            generating is not None
            and prompt
            and os.environ.get("VFA_FORCE_JSON_PROMPT") == "1"
        ):
            prompt = (
                "**只输出一个 JSON 对象**，必须包含以下字段：\n"
                "  - text:    30-100 字自然语言描述（必填，反查模糊匹配的来源字段，\n"
                "            含场景、人物动作、关键物体、环境氛围等细节）\n"
                "  - scene:   字符串（场景类别）\n"
                "  - actors:  数组（人物/主体），无则 []\n"
                "  - action:  字符串（主要动作），一句话\n"
                "  - objects: 数组（关键物体），无则 []\n"
                "**不要**输出 ``analysis`` / ``error`` / ``result`` 之类的字段，"
                "**不要**任何解释或分析文字，只输出 JSON 对象本身。\n\n"
                "--- 格式示例（占位符请按实际内容填） ---\n"
                "{\"text\": \"<30-100字描述>\", \"scene\": \"<场景>\", "
                "\"actors\": [\"<人物>\"], \"action\": \"<动作>\", \"objects\": [\"<物体>\"]}\n\n"
                "--- 用户问题 ---\n"
                + prompt
            )

        overrides = {}
        if max_new_tokens is not None:
            overrides["max_new_tokens"] = int(max_new_tokens)

        lock = get_session_call_lock()
        try:
            with lock:
                sess.reset()
                attachment = [Attachment(frame_bgr)] if frame_bgr is not None else []
                coro = sess.respond(
                    prompt=prompt,
                    attachment=attachment,
                    generating=generating,
                    **overrides,
                )
                # 重要：在线程隔离的新 event loop 里跑 SDK coroutine。
                # 用 `asyncio.run` 在 worker thread 里偶发死锁（与 main loop
                # 共享 thread 池资源），改用独立 Thread + new_event_loop。
                # 加 30s timeout 防 SDK 永久 hang（rknn3lite 偶发 bug
                # 或 NPU 满载时会等无穷）。
                result_holder = [None]
                error_holder = [None]
                finished = [False]

                def _runner():
                    loop = asyncio.new_event_loop()
                    try:
                        result_holder[0] = loop.run_until_complete(
                            asyncio.wait_for(coro, timeout=30.0)
                        )
                    except asyncio.TimeoutError:
                        error_holder[0] = "timeout"
                    except Exception as ex:
                        error_holder[0] = ex
                    finally:
                        try:
                            loop.close()
                        except Exception:
                            pass
                        finished[0] = True

                t = threading.Thread(target=_runner, daemon=True)
                t.start()
                t.join(timeout=35.0)

                # 防呆：超时/异常路径返回安全的 error string 而非 None
                # （下游 text.startswith("(") 会 NoneType 崩溃）
                if not finished[0]:
                    LOG.warning("vfa: _call_llm thread 35s 内未完成（可能 SDK 卡死）")
                    return f"(推理失败：超时 35s SDK 未响应)"
                if error_holder[0] == "timeout":
                    LOG.warning("vfa: LLM 推理超时（30s），可能 NPU 满载或 prompt 过长")
                    return f"(推理超时：30s 内 LLM 未响应)"
                if error_holder[0] is not None:
                    # 关键：不要 raise！否则 _run_summary 异常退出，
                    # state 卡在 STATE_RUNNING，前端永远显"分析中"。
                    # 改成返回 error string，_run_summary 会进入 fallback
                    # 路径（startswith("(")），下游 graceful 处理。
                    LOG.warning("vfa: _call_llm 异常: %s", error_holder[0])
                    return f"(推理失败：{error_holder[0]})"
                if result_holder[0] is None:
                    return f"(推理失败：返回为空)"
                result = result_holder[0]
        except Exception as e:
            # pydantic.ValidationError：SDK 拿到了 JSON 但字段对不上
            # SceneFrame（如模型输出 {"analysis":"..."} 缺 scene/actors 等字段）。
            # 把原始 dict 抽出来，让下游 _normalize_structured 兜底映射。
            try:
                from pydantic import ValidationError as _PydValErr
                if isinstance(e, _PydValErr):
                    errs = e.errors()
                    # pydantic v2: 第一个错误的 'input' 通常是整个原始 dict
                    raw_dict = (errs[0].get("input") if errs else None) or e.input
                    if isinstance(raw_dict, dict):
                        LOG.warning(
                            "vfa: SceneFrame 验证失败，原始 dict 兜底: %s",
                            list(raw_dict.keys()),
                        )
                        return raw_dict
            except ImportError:
                pass
            LOG.warning("vfa: sess.respond 抛异常: %s", e)
            return f"(推理失败: {e})"

        # 调试：记录 SDK 实际返回类型（揭示 JSON 解析失败还是 raw str）
        if generating is not None and not str(type(result).__name__).startswith("str"):
            LOG.debug("vfa: LLM 返回 SceneFrame 成功: %s", type(result).__name__)
        elif generating is not None:
            sample = str(result)[:80].replace("\n", " ")
            LOG.warning(
                "vfa: LLM 期望 SceneFrame 但拿到 str（JSON 解析失败？），前 80 字符: %s",
                sample,
            )
        return result


# --------------------------------------------------------------------------- #
# Singleton
# --------------------------------------------------------------------------- #
_service: Optional[VideoFileAnalysisService] = None
_service_lock = threading.Lock()


def get_service() -> VideoFileAnalysisService:
    global _service
    with _service_lock:
        if _service is None:
            _service = VideoFileAnalysisService()
        return _service
