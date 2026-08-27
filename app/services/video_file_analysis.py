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
    # 0. v0.10 关键防误判：如果 text 是合法 JSON（顶层 dict 形如 {"x": "y"}），
    #    跳过所有 Latin/CJK 启发式。原因：JSON 内含大量 key 名 / value 英文
    #    （"scene"/"actors"/"action"），任一 Latin 短串都会出现 ≥3 次，
    #    但本质是结构化输出不是乱码。SDK 自己的 _looks_garbled 对半截
    #    JSON 也会误判（"{" 出现但后续无法 parse → 标记 garbage）。
    #    这条短路让真 JSON 永远不被误杀。
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            import json as _json
            obj = _json.loads(stripped)
            if isinstance(obj, dict):
                return False  # 合法 JSON 顶层 dict → 一定不是 garbled
        except Exception:
            pass  # 不是合法 JSON → 落到下面的启发式

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
            # v0.10 修复：SDK Path 5 兜底 + wdnai 后端 JSON 解析失败时，
            # 模型的原始 JSON 字符串可能落在 3 个不同位置：
            #   1. result["note"] （早期 _normalize_structured 残留）
            #   2. result["text"] （worker's RPC reply 字段；SDK 返回 str
            #       时整段模型输出塞这里）
            #   3. result["structured"] （worker model_dump 的成功路径）
            # 三种位置都尝试用 regex 抽 JSON，把 scene/actors/action/
            # objects/note 提升到顶层（setdefault，已有顶层字段优先），
            # 让 _normalize_structured 拿到正确数据。
            _json_str = None
            for _k in ("note", "text", "structured_text"):
                _v = result.get(_k)
                if isinstance(_v, str) and "{" in _v:
                    _json_str = _v
                    break
            if _json_str is None:
                # 也尝试 result["text"] 整体作为 JSON（worker's reply 的
                # text 字段是模型原始输出，pydantic 失败时整段 JSON 落地）
                _v = result.get("text")
                if isinstance(_v, str) and _v.strip().startswith("{"):
                    _json_str = _v
            if _json_str is not None:
                try:
                    import json as _json_vfa
                    import re as _re_vfa
                    _m = _re_vfa.search(r"\{(?:[^{}]|\{[^{}]*\})*\}", _json_str)
                    if _m:
                        _parsed = _json_vfa.loads(_m.group(0))
                        if isinstance(_parsed, dict):
                            for _kn, _kv in _parsed.items():
                                result.setdefault(_kn, _kv)
                except Exception:
                    pass
            # v0.10 修复：worker 的 model_dump reply 把 SceneFrame 字段
            # 塞进 reply["structured"]（rpc_session_proxy 直透成
            # _ProxyResult._raw["structured"]）。Path 2 拿到 result 后
            # 把 structured 子字典的字段提升到顶层，让 _normalize_structured
            # 按 scene/actors/action/objects/note 正常归一化。
            _struct_vfa = result.get("structured")
            if isinstance(_struct_vfa, dict) and _struct_vfa:
                for _kn, _kv in _struct_vfa.items():
                    result.setdefault(_kn, _kv)
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

    **模型输出类型兜底**（v0.10 修）：SDK Path 5 兜底 + 正则抽 JSON 后，
    模型偶尔输出类型与 SceneFrame schema 不符的 JSON —— 例如车辆类
    视频上模型把 ``action`` / ``text`` 写成 ``[]`` 或 list of dicts；
    ``objects`` 写成 ``[{"type": "车辆", "description": "黑色轿车", ...}]``
    这种 TargetDoc 风格的 dict 列表。直接 ``str(v)`` 会得到 ``"[]"`` 或
    ``"{'type': ...}"`` 这种垃圾显示。这里做"类型拉直"：list 视情况
    拼接成字符串，list of dict 提取 ``description`` / ``text`` / ``name``
    三个候选字段。
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
        "scene":   _coerce_scene_like(known.get("scene")),
        "actors":  _coerce_list_of_str(known.get("actors")),
        "action":  _coerce_action_like(known.get("action")),
        "objects": _coerce_objects(known.get("objects")),
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


def _coerce_scene_like(v) -> str:
    """scene / action / text 字段预期是 str。模型偶尔输出 list / dict：
    list 取第一个非空元素并 stringify；dict 取首个 str 值；其余 fallback。
    """
    if v is None or v == "":
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        # 优先取第一个 string，其次 stringify dict 元素
        for x in v:
            if isinstance(x, str) and x:
                return x
            if isinstance(x, dict):
                _desc = _first_dict_string(x, ("scene", "action", "text", "description", "value"))
                if _desc:
                    return _desc
        # 全是空 list 或 dict → 取首元素的 str()
        for x in v:
            if x:
                return str(x)
        return ""
    if isinstance(v, dict):
        return _first_dict_string(v, ("scene", "action", "text", "description", "value")) or str(v)
    return str(v)


def _coerce_action_like(v) -> str:
    """action 字段专用兜底：list/dict → 拼接短描述；空 list → ""（非 "[]"）。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        parts: List[str] = []
        for x in v:
            if isinstance(x, str) and x:
                parts.append(x)
            elif isinstance(x, dict):
                _d = _first_dict_string(x, ("action", "desc", "description", "text", "value"))
                if _d:
                    parts.append(_d)
        return "、".join(parts)
    if isinstance(v, dict):
        return _first_dict_string(v, ("action", "desc", "description", "text", "value")) or str(v)
    return str(v)


def _coerce_list_of_str(v) -> List[str]:
    """actors / objects 字段预期是 List[str]。模型偶尔输出 list of dict：
    dict 元素提取 description / name / text / value 等候选字段。"""
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v else []
    if isinstance(v, list):
        out: List[str] = []
        for x in v:
            if isinstance(x, str) and x:
                out.append(x)
            elif isinstance(x, dict):
                _d = _first_dict_string(x, ("description", "name", "text", "value", "label"))
                if _d:
                    out.append(_d)
        return out
    if isinstance(v, dict):
        _d = _first_dict_string(v, ("description", "name", "text", "value", "label"))
        return [_d] if _d else []
    return [str(v)] if v else []


def _coerce_objects(v) -> List[str]:
    """objects 字段专用兜底：兼容 TargetDoc 风格 list of dict。"""
    return _coerce_list_of_str(v)


def _first_dict_string(d: dict, candidates: tuple) -> str:
    """从 dict 里按优先级取第一个非空 string 值。"""
    for k in candidates:
        v = d.get(k)
        if isinstance(v, str) and v:
            return v
        if isinstance(v, list) and v:
            for x in v:
                if isinstance(x, str) and x:
                    return x
    # 兜底：取第一个非空 string 值
    for v in d.values():
        if isinstance(v, str) and v:
            return v
    return ""


# 通用垃圾值检测：Qwen3-VL 在不同类型视频上有不同 hallucinate 模式：
#   - 车辆视频 → "粤B·88888" / "车牌颜色：蓝色" / "车型:小型轿车"
#   - 监控视频 → "2024-01-15 14:32:18" / "ID: 0042"
#   - 体育视频 → "3:2" / "Curry #30"
#   - 产品视频 → "SKU-12345" / "材质: 100% 棉"
#   - 监控视频 → "x: 234, y: 567" (坐标)
# 这些输出**不是** SceneFrame schema 期望的"看到了什么 + 在做什么"
# 自然语言描述，需要从结构化字段里清掉（保留进 note 供回溯）。
#
# 通用检测思路（不针对任何特定视频类型）：
#   1. 字符串 < 12 字符 且 含 ≤1 个 CJK 字符 且 含 1+ 个数字 / 大写字母 / ·  → → 短串特征
#   2. 字符串是 "label: value" / "label: value, label2: value2" 模式 → attribute 行
#   3. 字符串是 (中文车牌前缀 + 大写字母 + · + 数字) 这种特定模式   → 车牌（保留兼容）
#   4. 字符串只有数字 / 标点 / 全大写缩写                        → 编号 / ID / 比分
# 任何一条命中即视为"非 SceneFrame 自然语言描述"，从结构化字段中剔除。
_GARBAGE_PATTERNS = [
    # 纯短串（含数字 / 大写 / ·，< 12 字符）：车牌 / ID / SKU / 编号
    re.compile(r"^[\u4e00-\u9fffA-Za-z]{0,4}[A-Z0-9·\-]{3,12}$"),
    # 短串 + 空格 + 短串（如 "车牌 5J5666"）：车牌 / 编号标签
    re.compile(r"^[\u4e00-\u9fffA-Za-z]{2,8}\s+[A-Z0-9·\-]{3,12}$"),
    # label: value 或 label: value, label2: value2 — attribute 行
    re.compile(r"^[\u4e00-\u9fffA-Za-z]{1,8}\s*[:：=]\s*[^\n,]{1,30}(?:\s*[,，;；]\s*[\u4e00-\u9fffA-Za-z]{1,8}\s*[:：=]\s*[^\n,]{1,30})*$"),
    # 时间戳："2024-01-15" / "2024-01-15 14:32:18"
    re.compile(r"^\d{1,4}[-/]\d{1,2}[-/]\d{1,4}(\s+\d{1,2}:\d{1,2}(:\d{1,2})?)?$"),
    # 纯时间："14:32:18" / "14:32"
    re.compile(r"^\d{1,2}:\d{1,2}(:\d{1,2})?$"),
    # 比分："3:2" / "21:18"
    re.compile(r"^\d{1,3}\s*[:：]\s*\d{1,3}$"),
    # 坐标："x: 234, y: 567" / "(123, 456)"
    re.compile(r"^[xXyY]\s*[:：=]\s*\d+(\s*[,，;；]\s*[xXyY]\s*[:：=]\s*\d+)*$"),
    # 纯数字 / 纯大写缩写
    re.compile(r"^[\d]{3,}$"),
    re.compile(r"^[A-Z]{3,8}$"),
]


def _is_garbage_value(s: str) -> bool:
    """返回 True 表示 ``s`` 不是 SceneFrame 字段期望的自然语言描述。

    通用判定：长度超限 / 命中 attribute / ID / 编号 / 坐标 / 时间戳等
    单一字段抽取模式。中文场景短语 / 动作描述 不会被误判。
    """
    if not s or not s.strip():
        return False
    s = s.strip()
    if len(s) > 60:  # SceneFrame 字段都不该超过这个长度（text 最大 80 字但纯文本长串不会是 garbage）
        return False
    return any(p.match(s) for p in _GARBAGE_PATTERNS)


def _sanitize_structured_fields(structured: dict, raw_text: str) -> tuple:
    """从结构化字段里清洗 attribute / 编号类垃圾，保留进 note 让落盘可追溯。

    检测 scene / action / text 是否是"非 SceneFrame 自然语言描述"的
    单字段抽取（车牌 / ID / 时间戳 / 坐标 / 比分 / label: value 等），
    若是则置空并把原始 attribute 串塞进 ``note``。
    同时扫描 raw_text 抓类似的 attribute 子串，同样摘出来归档到 note。

    Returns:
        (cleaned_structured_dict, garbage_note_str)
        garbage_note_str 为空表示没检测到垃圾。
    """
    cleaned = dict(structured)
    garbage_bits: List[str] = []

    for _field in ("scene", "action", "text"):
        _v = cleaned.get(_field) or ""
        if _v and _is_garbage_value(_v):
            garbage_bits.append(f"{_field}={_v}")
            cleaned["_field"] = ""

    # 扫 raw_text 抓垃圾子串（不限位置）
    if raw_text:
        for _p in _GARBAGE_PATTERNS:
            for _m in _p.finditer(raw_text):
                _hit = _m.group(0).strip()
                # 跳过空或纯标点的 hit
                if not _hit or _hit in garbage_bits:
                    continue
                garbage_bits.append(_hit)

    note_str = "; ".join(garbage_bits) if garbage_bits else ""
    return cleaned, note_str


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
            # v0.10.3 反查清洗：note 字段只用来落盘追溯（车牌 / 时间戳 /
            # attribute-only 等垃圾诊断信息），不应泄漏到反查 API 响应
            # —— 否则 chip 候选会混入 "粤B·88888" / "14:32:18" 之类噪声。
            # 内存里保留 self.structured["note"] 给运维排障用，只是不
            # 序列化进 to_dict()。
            _struct = {k: v for k, v in self.structured.items() if k != "note"}
            out["structured"] = _struct
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
CHUNK_MAX_NEW_TOKENS           = 150     # pass 1 单块硬控输出（v0.10: 200→150，省 ~2s/call）
FINAL_MAX_NEW_TOKENS           = 280     # pass 2 最终综述硬控输出（v0.10: 400→280，省 ~6s）

# 单次 LLM 推理的 asyncio.wait_for 上限（秒）。v0.10：30 → 60。
# 30s 在 RK3588 qwen3-vl-4b 上贴着 Apollo 综述最后一步的边界：1829-frame
# 视频 ≈ 10 个 chunk，每段 ≤120 字 + 模板 ≈ 2500 字 prompt + 400
# 新 token 输出，实测 ~25-30s；任何 prompt 略长或 NPU 短暂忙就破。
# 60s 留余量覆盖合理慢调用，又不至于让真卡死的 SDK hang 几分钟才报错。
_INFER_TIMEOUT_SEC             = 60.0

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
        # 连续 _call_llm 超时计数：≥2 触发 worker restart 清掉 wedged
        # rknn3 C 层（session_stop() 在用户 RK3588 实测不释放，65s/call
        # 串联等死）。worker 重启 = 30s init 成本，但根治"LLM 队列 N"
        # 队列积压。成功调用一次重置回 0。
        self._consecutive_timeouts = 0
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

        # v0.10 关键修复：用户传 "describe scene" 这种英文 prompt 时，
        # qwen3-vl-4b 会输出 700+ 字的英文段落描述（"This is a close-up
        # photograph of a white bowl..."），而不是 SceneFrame JSON schema。
        # SDK 的 schema hint 被忽略，每帧都触发 _split_scene_frame_result
        # 的"text 字段乱码"兜底，event 全是 dirty=True 的空事件。
        #
        # v0.10.2 泛化：原版只针对车牌 / 车辆属性这类误读 prompt 的输出做了
        # 约束，但 Qwen3-VL 在不同类型视频上有不同 hallucinate 模式：
        #   - 车辆视频：把 prompt 误读成 attribute-extraction，输出
        #     "粤B·88888" / "车牌颜色：蓝色" / "车型:小型轿车" 等
        #   - 监控视频：只输出时间戳 / ID 编号
        #   - 体育视频：只输出比分 / 球员名
        #   - 产品视频：只输出 SKU / 颜色 / 材质
        # 通用规则是：模型**只描述看到了什么 + 在发生什么**，不要做属性抽取。
        # 这里给通用 SceneFrame schema 描述 + 一个中性示例（不绑车型）。
        if "json" not in prompt.lower() and "schema" not in prompt.lower():
            prompt = (
                prompt.rstrip("。. \n")
                + "\n\n【输出格式（严格按 JSON schema）】\n"
                "请输出一个 JSON 对象，必须且只能有以下 5 个字段：\n"
                "{\n"
                '  "scene":   "<场景地点或环境，2-12 字中文，如 \"厨房\"、\"户外街道\"、\"小区入口\">",\n'
                '  "actors":  ["<人物或主体1>", "<人物或主体2>"],   // 字符串数组，无则 []，最多 3 项\n'
                '  "action":  "<正在发生的动作或事件，2-20 字中文一句话>",\n'
                '  "objects": ["<关键物体1>", "<关键物体2>"],       // 字符串数组，无则 []，最多 4 项\n'
                '  "text":    "<30-80 字中文自然语言描述：场景 + 动作 + 关键物体综合>"  \n'
                "}\n\n"
                "【类型约束（模型常错的地方）】\n"
                "- scene / action / text 必须是**字符串**（不能是数组、字典、数字）\n"
                "- actors / objects 必须是**字符串数组**（不能是数组里套字典、不能是单字符串）\n"
                "  错误示例：objects=[{\"type\":\"车辆\",\"color\":\"白色\"}]\n"
                "  正确示例：objects=[\"白色轿车\", \"行人\"]\n\n"
                "【输出风格约束（避免输出污染）】\n"
                '- 只描述"看到了什么 + 正在发生什么"，**不要做结构化属性抽取**\n'
                "  （车牌号 / 车牌颜色 / 车型 / 产品 SKU / 颜色尺寸 / 时间戳 /\n"
                "  比分球员 / 监控 ID / 坐标位置 等单独字段不属于 scene/action/text）\n"
                "- text 字段必须是一段自然语言描述（30-80 字），不是单 token、数字串、\n"
                "  label: value 形式的 attribute-only 输出\n"
                "- 全中文输出（除 JSON 键名外）\n\n"
                "【正确示例】\n"
                '{"scene":"户外街道","actors":["行人"],"action":"行人从画面左侧走向右侧","objects":["白色轿车","树木","人行道"],"text":"一名行人从画面左侧走向右侧，身旁停着一辆白色轿车，背景可见行道树和人行道。"}'
            )

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
            max_new_tokens=250,
        )
        structured_dict, raw_text = _split_scene_frame_result(result)

        # v0.10.2 垃圾输出清洗：Qwen3-VL 经常把 prompt 误读成 attribute-extraction
        # 任务，输出车牌号 / 监控 ID / 时间戳 / 坐标 / 比分等"非 SceneFrame"
        # 内容。这些会污染反查索引（chip 候选里出现 "粤B·88888" / "14:32:18" 等），
        # 需要从结构化字段里清掉，残留 attribute 信息塞进 note 让落盘可追溯。
        if structured_dict is not None:
            _sanitized = _sanitize_structured_fields(structured_dict, raw_text or "")
            structured_dict = _sanitized[0]
            _garbage_note = _sanitized[1]
            if _garbage_note:
                _existing_note = structured_dict.get("note") or ""
                structured_dict["note"] = (
                    f"{_existing_note} | {_garbage_note}" if _existing_note else _garbage_note
                )

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
    def _on_call_timeout(self) -> None:
        """每次 _call_llm 触发 60s timeout 时调用。

        连续 ≥2 次 → 触发 ``stop_instance_local(<swcap_video_describe>``)
        干掉当前承载视频描述推理的 worker 进程 → 35s 内 spawn 新进程 +
        init LLMSession → 队列里积压的 _call_llm 失败（但 1-2 个 error
        事件比 5+ 分钟卡死好），之后稳态运行。

        不再硬编码 ``"qwen3-vl-4b"`` —— 通过 registry 找当前 running
        的 ``swcap_video_describe`` 实例的 model_id（qwen3-vl-4b 或
        qwen3-5-vl-4b 都行），按实际加载的模型重启。

        阈值定 ≥2 而非 1：单次 timeout 可能是长 prompt / NPU 短暂忙，
        不必重启；连续 2 次说明 C 层 rknn3 session 真正 wedged，
        session_stop() + wait_sec=5 已实测不够，必须靠 worker 重启
        抹掉 NPU mutex 状态。
        """
        self._consecutive_timeouts += 1
        if self._consecutive_timeouts < 2:
            return
        try:
            from app.services.compute_registry import get_registry
            registry = get_registry()
            target_model = None
            for inst in registry.list_with_source():
                caps = inst.get("capability_types") or []
                if "swcap_video_describe" in caps and inst.get("status") == "running":
                    target_model = inst.get("model_id")
                    break
            if not target_model:
                LOG.warning("vfa: 重启跳过 —— 当前无 running swcap_video_describe 实例")
                return
            LOG.warning(
                "vfa: %d 次连续 _call_llm 超时，触发 %s worker 重启（清 wedged C 层）",
                self._consecutive_timeouts, target_model,
            )
            registry.stop_instance_local(target_model)
        except Exception as ex:
            LOG.error("vfa: worker 重启失败: %s", ex)
        # 重启后新 worker 是 fresh session，counter 清零。
        self._consecutive_timeouts = 0

    def _reset_timeout_counter(self) -> None:
        """成功 / 非 timeout 错误路径调用，重置连续超时计数。

        单次成功（或非 timeout 异常）就足以证明 C 层 rknn3 session 还
        能正常调度，counter 不需要累加。
        """
        self._consecutive_timeouts = 0

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

        # Prompt 长度诊断（用户报告 "60s timeout 但不是 NPU wedged"——
        # 怀疑是 prompt 超过 qwen3-vl-4b max_context_len=4096 导致 rknn3
        # session_run 在 prefill 时长挂）。本段在每条 _call_llm 入口 log
        # 输入 token 总数，逼近 4096 时 WARNING、超过 ERROR（让用户立刻
        # 看到是 prompt 而不是 C-level 引起的 stall）。
        try:
            tokenizer = getattr(getattr(sess, "_backend", None), "tokenizer", None)
            spec = getattr(getattr(sess, "_backend", None), "spec", None)
            if tokenizer is not None and spec is not None:
                user_t = len(tokenizer(prompt, add_special_tokens=False)["input_ids"])
                img_size = getattr(spec, "image_size", 384) or 384
                img_tokens = (img_size // 14) ** 2 + 6  # patches + start/end/pad
                # 估算 SDK wrapping：system + chatml overhead + JSON schema hint
                overhead = 12 + 80 + 150
                total_in = user_t + img_tokens + overhead
                budget = getattr(spec, "max_context_len", 4096)
                gen_out = max_new_tokens or 300
                total = total_in + gen_out
                if total_in > budget:
                    LOG.error(
                        "vfa: _call_llm INPUT 超过 model context：%d tokens > %d "
                        "（user=%d + image=%d + overhead=%d；image_size=%d, "
                        "max_new_tokens=%d）→ 必然触发 rknn3 prefill 阶段长挂",
                        total_in, budget, user_t, img_tokens, overhead,
                        img_size, gen_out,
                    )
                elif total_in > budget * 0.8:
                    LOG.warning(
                        "vfa: _call_llm INPUT 逼近 context 上限：%d / %d tokens "
                        "（user=%d + image=%d + overhead=%d；image_size=%d）",
                        total_in, budget, user_t, img_tokens, overhead, img_size,
                    )
                else:
                    LOG.debug(
                        "vfa: _call_llm tokens: in=%d (%d user + %d image + %d overhead) "
                        "+ out=%d = total %d / %d",
                        total_in, user_t, img_tokens, overhead, gen_out, total, budget,
                    )
        except Exception as diag_ex:
            LOG.debug("vfa: prompt token 诊断失败（忽略）: %s", diag_ex)

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
                # 加 _INFER_TIMEOUT_SEC timeout 防 SDK 永久 hang（rknn3lite
                # 偶发 bug 或 NPU 满载时会等无穷）。
                #
                # v0.10：30s → 60s。Apollo 三段式综述最后一步（final summary）
                # 把所有 chunk 小结拼起来再调一次 LLM：1829-frame 视频通常 10
                # 个 chunk，每段 ≤120 字 + 模板 ≈ 2500 字 prompt + 400
                # 新 token 输出。RK3588 qwen3-vl-4b 实测这一调用 ~25-30s，
                # 30s 阈值被贴着边缘，任何 prompt 略长或 NPU 短暂忙就破。
                result_holder = [None]
                error_holder = [None]
                finished = [False]

                def _runner():
                    loop = asyncio.new_event_loop()
                    try:
                        result_holder[0] = loop.run_until_complete(
                            asyncio.wait_for(coro, timeout=_INFER_TIMEOUT_SEC)
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
                # join 留 5s 余量覆盖 asyncio.wait_for 拆 + runner 收尾
                t.join(timeout=_INFER_TIMEOUT_SEC + 5.0)

                # 防呆：超时/异常路径返回安全的 error string 而非 None
                # （下游 text.startswith("(") 会 NoneType 崩溃）
                if not finished[0]:
                    LOG.warning("vfa: _call_llm thread %.0fs 内未完成（SDK 卡死）",
                                _INFER_TIMEOUT_SEC + 5.0)
                    _interrupt_rknn_session(sess, wait_sec=5.0)
                    self._on_call_timeout()
                    return f"(推理失败：超时 {_INFER_TIMEOUT_SEC:.0f}s SDK 未响应)"
                if error_holder[0] == "timeout":
                    LOG.warning("vfa: LLM 推理超时（%.0fs），可能 NPU 满载或 prompt 过长",
                                _INFER_TIMEOUT_SEC)
                    # 关键：asyncio.wait_for 只取消 Python 协程，C 层
                    # rknn3.session_run() 还在 NPU 上跑（无人在等它返回）。
                    # 不显式 session_stop() + 等待，下次 sess.reset() → clear_kvcache()
                    # 会阻塞等 C 层跑完（RK3588 实测 5-30s），连锁卡死后续每个
                    # chunk。计算 worker 已用同款模式（compute_worker.py
                    # embed_video handler:611-619）。
                    #
                    # session_stop() 是 async signal，C 层在 kernel 间
                    # 协作点检查 → 实际释放可能花数秒（长 prompt prefill /
                    # generation 跑到一半）。这里 sync 等待最多 5s 让 C 层
                    # 处理 stop 信号并实际让出 NPU mutex，下次 reset() 不阻塞。
                    _interrupt_rknn_session(sess, wait_sec=5.0)
                    self._on_call_timeout()
                    return f"(推理超时：{_INFER_TIMEOUT_SEC:.0f}s 内 LLM 未响应)"
                if error_holder[0] is not None:
                    # 关键：不要 raise！否则 _run_summary 异常退出，
                    # state 卡在 STATE_RUNNING，前端永远显"分析中"。
                    # 改成返回 error string，_run_summary 会进入 fallback
                    # 路径（startswith("(")），下游 graceful 处理。
                    LOG.warning("vfa: _call_llm 异常: %s", error_holder[0])
                    self._reset_timeout_counter()
                    return f"(推理失败：{error_holder[0]})"
                if result_holder[0] is None:
                    self._reset_timeout_counter()
                    return f"(推理失败：返回为空)"
                result = result_holder[0]
                self._reset_timeout_counter()
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

        # 兼容 RpcSessionProxy 包装：worker 把推理结果序列化成 dict，
        # 主进程 ``_RpcSessionProxy.respond`` 统一包成 ``_ProxyResult``
        # （rpc_session_proxy.py:301-309）。_ProxyResult 不是 str/dict，
        # 但 ``.text`` 字段等同 ``sess.respond()`` 的 str 返回、
        # ``._raw`` 字段是 worker 发回的完整 JSON dict（含 scene /
        # actors / action / objects / text 等 SceneFrame 字段）。
        #
        # 不归一化的话，``_run_summary`` 拿 ``text.startswith("(")``
        # / ``_rescue_structured_batch`` 拿 ``raw.startswith("(")`` 都
        # 会 AttributeError；``_infer_one`` → ``_split_scene_frame_result``
        # 走 Path 5 兜底返回 ``(None, _ProxyResult)``，上游同样崩。
        # ``audio_analysis.py:385-395`` 已用同款模式。
        if generating is None:
            if not isinstance(result, str):
                text_attr = getattr(result, "text", None)
                if isinstance(text_attr, str):
                    result = text_attr
                else:
                    result = str(result)
        else:
            if not isinstance(result, (dict, str)):
                raw = getattr(result, "_raw", None)
                if isinstance(raw, dict) and raw:
                    result = raw
                else:
                    # _raw 缺失（老 worker / fallback 路径）—— 用顶层字段
                    # 拼个最小 dict，让 _split_scene_frame_result Path 2
                    # 接管；缺 scene/actors 等字段时由 _normalize_structured
                    # 走智能归一化兜底。
                    text_attr = getattr(result, "text", None)
                    if isinstance(text_attr, str):
                        result = {"text": text_attr}
        return result


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _interrupt_rknn_session(sess, *, wait_sec: float = 2.0) -> None:
    """向 rknn3 发送 session_stop() 中断当前阻塞的 session_run 并等待 C 层实际释放。

    asyncio.wait_for / t.join 超时只取消 Python 协程，**不**中断 C 层
    rknn3.session_run()；NPU 满载或 SDK 卡死时，C 层会在后台持续占用
    RKNN session，导致下一次 ``sess.reset()`` 调 ``clear_kvcache()`` 时
    阻塞等 C 层跑完（数十秒～数分钟），连锁卡死后续每个 chunk。

    这里主动 session_stop() 让 C 层几百 ms 内主动退出，session 进入
    可用态。LLMSession + VLM/Text/TTS backend 都有 ``_backend.rknn_llm``
    句柄可调 session_stop()；TTS backend 略有不同（``_backend.talker``）
    —— 当前 VFA 路径只用 VLM/text backend，这一处覆盖足够。

    **v0.10 新增 wait_sec**：session_stop() 是 async signal（不是
    force-kill），C 层只在 kernel 边界检查 stop 标志。RK3588 qwen3-vl-4b
    实测：长 prompt prefill 跑到一半时收到 stop，要等当前 kernel 跑完、
    下一个协作点才发现 flag → 实际释放花 1-5s。如果不等待直接 return，
    下次 ``sess.reset()`` 在 C 层 mutex 未释放时调 clear_kvcache() 就会
    阻塞 → 用户看到"卡住"现象（这是用户报告的 §v0.10 残留 bug）。
    实测最大 5s 已覆盖大部分边界，更长说明 C 层真正 wedged，需要
    worker 重启（已超出本函数能力）。

    调用方需仍在 ``with lock`` 内，避免下一轮 ``sess.reset()`` 在
    C 层 abort 完成前抢跑。
    """
    backend = getattr(sess, "_backend", None)
    if backend is None:
        return
    rknn_llm = getattr(backend, "rknn_llm", None)
    if rknn_llm is None:
        return
    try:
        rknn_llm.session_stop()
    except Exception as ex:
        # session_stop() 内部可能因 driver 状态异常抛错；不影响主路径
        LOG.warning("vfa: session_stop() 异常（继续）: %s", ex)
    if wait_sec > 0:
        # 同步等待：C 层在 kernel 间检查 stop flag 并让出 NPU mutex。
        # Python 端 sleep（C 线程不阻塞），sleep 期间主线程可以处理
        # 其他事件；上限 wait_sec 防止极端 wedged 情况无限等。
        time.sleep(wait_sec)


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
