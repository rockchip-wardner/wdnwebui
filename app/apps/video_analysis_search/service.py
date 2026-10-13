"""视频文件分析服务：自管理 wdnav.VideoCapture + 后台推理线程。

设计要点：

* **独立于 ``VideoSession``** —— 视频监控 / 音视频描述 各有各的
  capture 生命周期，VAS 跑批量分析时不该把它们的视频源也抢走。
  所以这里自己持有一个 ``wdnav.VideoCapture``（懒加载 + 独立
  decode 线程），不挂 ``VideoSession.add_frame_sink``。
* **LLMSession 不归本服务所有** —— 严格走 ``ComputeRegistry``
  的 ``swcap_video_describe`` capability，遵守设计原则 1。多个消费者
  （detector + VAS）共用同一 session，所有 ``session.respond``
  前后用 ``ComputeRegistry.get_session_call_lock()`` 串行化。
* **每 N 帧调一次 VLM**，把结果写到 ``self._events`` 里持久化；
  全文件走完后跑一次综述（reset + 单次 respond，prompt 包含
  所有 per-frame 文本）。事件本身不含帧字节，"回查视频文件取帧"
  走 ``GET /api/video-analysis-search/frame/{frame_pos}`` 重新
  解码源文件（仅 file 源；live 源取帧拒绝）。
* **wdnai 懒加载**：同 detector，只有 ``start()`` 才碰
  ``Attachment`` / ``LLMSession``，避免 web 启动时 segfault。
"""
from __future__ import annotations

import asyncio
import hashlib
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

from app.core.video.change_detector import (
    DEFAULT_CHANGE_THRESHOLD,
    SAMPLE_MODE_AUTO,
    SAMPLE_MODE_MANUAL,
    ChangeDetector,
)
from app.core.video.frame_judge import (DEFAULT_LONG_SIDE, LEGACY_MODE_ALIASES,
                                       metrics, normalize_mode)


try:
    from wdnav import VideoCapture as _WdnavVideoCapture
except Exception:  # pragma: no cover - platform-specific load failure
    _WdnavVideoCapture = None


LOG = logging.getLogger("vas_service")


# run 持久化真源从 ``app/apps/video_analysis_search/vas_runs/*.json`` 迁到
# LanceDB（``app/core/video/cache`` 表的 ``kind='analysis'`` 行）—— 见
# ``WechatVideoCache.save_analysis`` / ``list_analysis``。旧 JSON 目录
# 不再使用（升级时手工清空即可）。


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
    VAS 不消费 ``attachment`` 槽（TTS 才用），改基类零下游破坏。
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

        **总字数约束（v0.15 wire 安全）**：5 字段 JSON-encoded 后总长度
        ≤ ~200B，留 55B 余量在 FastCDR ComputeReply.text 255B 硬截断
        内（参考 compute_worker.py text SHM path-list 阈值 200B）——
        减少触发 /dev/shm path-list 兜底频率，常规 inline 走 wire。
        实际渲染时 SDK 用 ``str(dict)``（不是 ``json.dumps(indent=2)``）
        节省 ~30% 体积，但保守起见按 ≤ 200B 预算。每个字段都在上面
        max_length 范围内，5 字段自然落在 150 字以内。

        **❌ 反例（schema-echo 防御，v0.15）**：Qwen3-VL-4B RKNN3 首
        token 不稳定时（约 ~46% schema-echo 概率，5c33fb3 提到的）
        偶发把字段名本身当 JSON 值 echo 出来。**严禁**按下方反例输出：

          ``{"text": "scene 小区入口 actors action objects text", "scene": "", ...}``

        ↑ 字段名 (scene/actors/action/objects/text) 本身被当 value，
        完全没描述画面内容 —— 这是错误输出。text 字段必须是真正
        描述画面的自然语言（30-80 字中文，含场景/动作/物体），不是
        字段名拼接。即使 image 难描述时也必须给出有意义的描述，
        宁可空字段 ("scene": ""，"text": "画面模糊难以辨认") 也
        不要把字段名当 value 填充。

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


# SceneFrame 6 个字段名。合法 SceneFrame.text 是自然语言描述, 含
# ≥3 个字段名 (Qwen3-VL-4B 在 RKNN3 上 echo schema hint) 即 schema-echo。
_SCENE_FRAME_FIELD_NAMES = ("scene", "actors", "action", "objects", "text", "note")


def _looks_like_schema_echo(text: str) -> bool:
    """检测 schema-echo 退化输出 (Qwen3-VL-4B RKNN3 概率性故障)。

    典型样本: ``"scene 小区入口 actors action objects text"`` —— 用户
    VAS prompt 是字段名拼接的退化输入, 模型 echo 回来。``_looks_garbled``
    漏判 (Latin 字段名每个仅 1 次 + 空格分隔 CJK), 显示成有效 text →
    前端把 echo 当描述渲染。这里 word-boundary 检测 ≥3 个 SceneFrame
    字段名即视为 echo 静默丢弃走"本帧分析失败"占位, 与 garbled 同路径。

    **结构化格式排除** (v0.15+):模型输出 YAML/JSON key-value 时也含字
    段名 (例 ``scene: 户外\nactors: 行人``), 这种是**有效**结构化输入
    不该丢弃。判断:含 ASCII ``:`` / 全角 ``：`` / ``{`` / ``[`` 即视为
    尝试结构化输出,跳过 echo 检测。Path 2 / Path 3 现在也用此函数,
    必须避免把合法 YAML 输入误判成 echo 静默丢失 scene/action 真值。
    """
    if not text or len(text) < 10:
        return False
    # 含结构化格式标记 → 不是纯 echo,放过 (Path 2/3 已分别走
    # _normalize_structured / _parse_kv_lines 处理)
    if any(c in text for c in (":", "：", "{", "[")):
        return False
    text_lower = text.lower()
    import re as _re_schema_echo
    found = 0
    for name in _SCENE_FRAME_FIELD_NAMES:
        if _re_schema_echo.search(rf"\b{_re_schema_echo.escape(name)}\b", text_lower):
            found += 1
            if found >= 3:
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
    空串（str 无 ``text`` 属性），**等同于静默丢弃**。VAS 原本把 raw
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
            # schema-echo (Qwen3-VL-4B RKNN3 把 schema hint 字段名 echo 回来)
            # _looks_garbled 漏判 (Latin 各 1 次 + 空格分隔), 这里 word-boundary
            # 检测 ≥3 个 SceneFrame 字段名即视为 echo 静默丢弃
            if _looks_like_schema_echo(text):
                LOG.warning(
                    "vfa: SceneFrame text 命中 schema-echo (≥3 字段名), 静默丢弃: %r",
                    text[:80],
                )
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
            # SceneFrame 的字段现在就平铺在 result 顶层（``model_dump()`` 的
            # 结果），不再套在 ``result["result_data"]`` 下。保留
            # ``structured`` 这个键是因为它可能来自模型的原始输出 ——
            # 模型偶尔会把 SceneFrame 当成"里面再包一层"的 JSON。
            for _key_vfa in ("structured", "result_data"):
                _struct_vfa = result.get(_key_vfa)
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
            # **schema-echo 兜底**（v0.15+）：用户 VAS prompt 在末尾显式给
            # 出 JSON 字段名示例（scene/actors/action/objects/text），
            # Qwen3-VL-4B 在 RKNN3 上概率性 (~17-46%, 取决于 prompt
            # 长度 / image_size) 退化输出时把这些字段名 echo 进 ``text``
            # 字段，但其他字段（scene/action/...）可能仍是真值。dict 路径
            # 必须做同样判定,否则前端"逐帧分析"列表会把 echo 当描述渲染。
            # Path 1 的 SceneFrame 实例路径已有此检查,这里补 Path 2。
            # 行为:丢弃 bad text,从 structured 字段合成 fallback text,
            # 让 scene='户外街道' 这种真值仍能被前端反查 / 取帧 modal 看到。
            if text and _looks_like_schema_echo(text):
                LOG.warning(
                    "vfa: raw dict 路径 text 字段命中 schema-echo (≥3 字段名),"
                    " 静默丢弃 raw text: %r", text[:80],
                )
                text = ""
                if structured:
                    scene = structured.get("scene", "") or ""
                    action = structured.get("action", "") or ""
                    text = (
                        f"{scene}：{action}".strip("：")
                        or action or scene or ""
                    )
                if structured and structured.get("note") and _looks_like_schema_echo(
                    structured["note"]
                ):
                    # 旧 _normalize_structured 把 dict 顶层 ``text`` 塞进
                    # note_extras(v0.15+ 已修),但历史 run 仍有此数据。
                    # 兜底清掉 echo 污染的 note。
                    structured["note"] = None
                return structured, text
            return structured, text
        except Exception as e:
            LOG.warning("vfa: raw dict 归一化失败: %s", e)

    # 失败分支：result 是 str。优先尝试 JSON parse，再 regex。
    raw = str(result).strip()
    # **schema-echo 兜底**(v0.15+):同上,但作用于 str 路径(Path 3)。
    # 模型偶尔不输出 JSON,直接 print 退化模型输出作为 str (例
    # ``"scene 户外街道 actors action objects text"``)。Path 1 的
    # SceneFrame 实例已经处理,这里补 Path 3。命中则丢弃整段 ——
    # str 路径上 text 是 raw text 本身,保留会污染前端;让
    # ``_infer_one`` 走"空事件 + dirty 占位"语义。
    if raw and _looks_like_schema_echo(raw):
        LOG.warning(
            "vfa: raw str 路径命中 schema-echo (≥3 字段名), 静默丢弃: %r",
            raw[:80],
        )
        return None, ""
    parsed = _parse_structured_from_str(raw)
    if parsed is not None:
        return parsed, raw

    return None, raw


def _text_from_dict(obj: dict) -> str:
    """从 dict 里抽一个 fallback 文本。

    优先取 ``text`` 字段（VAS SceneFrame 的主描述字段，承担反查模
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


_KV_LINE_RE = re.compile(
    r"^\s*[-*•]?\s*\**\s*(scene|actors|action|objects|text|note)\s*\**\s*[:：]\s*(.*)$",
    re.IGNORECASE,
)


def _split_kv_items(val: str) -> List[str]:
    """把 ``行人、骑车人`` / ``a, b`` / ``[a, b]`` 拆成列表。"""
    v = (val or "").strip().strip("[]")
    parts = re.split(r"[、,，;；/|]", v)
    return [p.strip().strip('"').strip("'").strip() for p in parts if p.strip()]


def _parse_kv_lines(text: str) -> Optional[dict]:
    """解析模型输出的行式 ``key: value``（YAML-ish，非 JSON）。

    典型：``scene: 城市街道\\nactors: 行人、骑车人\\n...`` —— 字段其实
    齐全，旧实现只认 JSON + markdown ``#`` 标题 → 落 raw text（前端
    显示整段原文）。这里要求 **≥2 个字段名且至少 1 个非空值** 才认：
      ① 单行偶然含 ``scene:`` 的普通描述不误吞；
      ② 值全空的 schema-echo（如 ``scene:\\nactors:``）不当作有效结构化
         —— 交给 ``_looks_like_schema_echo`` 静默丢弃。

    ``text`` 字段单独放在 ``_text``（``_normalize_structured`` 不认该键），
    由 Path 3 取出当事件描述，避免前端显示整段原文。
    """
    if not text:
        return None
    found: dict = {}
    for line in text.splitlines():
        m = _KV_LINE_RE.match(line)
        if not m:
            continue
        key = m.group(1).lower()
        if key in found:
            continue
        found[key] = m.group(2).strip()
    if len(found) < 2:
        return None
    if not any(v for k, v in found.items() if k != "text"):
        return None
    out: dict = {
        "scene":   found.get("scene", ""),
        "actors":  _split_kv_items(found.get("actors", "")),
        "action":  found.get("action", ""),
        "objects": _split_kv_items(found.get("objects", "")),
        "note":    found.get("note") or None,
    }
    if found.get("text"):
        out["_text"] = found["text"]
    return out


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
        # 模型的 ``scene: xxx\nactors: xxx`` 行式输出（YAML-ish，
        # 非 JSON）字段其实齐全，先试行式解析；失败再走 markdown/regex。
        _kv = _parse_kv_lines(text)
        if _kv is not None:
            return _kv
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

    智能匹配：scene/actors/action/objects/note 直接映射；``text`` 也
    列入"已知但不抽出"集合（worker 端 ``SceneFrame.model_dump()`` 经
    ``model_dump()`` 得到的 dict 里通常含 ``text`` 顶层字段——
    旧实现把它当未知字段塞进 ``note_extras``，schema-echo 模式下会让
    note 也带 "text: scene ... actors ..." 噪声）。其他未知字段
    （Qwen3-VL 经常输出 ``analysis`` / ``error``）拼到 ``note`` 里。

    **模型输出类型兜底**（v0.10 修）：SDK Path 5 兜底 + 正则抽 JSON 后，
    模型偶尔输出类型与 SceneFrame schema 不符的 JSON —— 例如车辆类
    视频上模型把 ``action`` / ``text`` 写成 ``[]`` 或 list of dicts；
    ``objects`` 写成 ``[{"type": "车辆", "description": "黑色轿车", ...}]``
    这种 TargetDoc 风格的 dict 列表。直接 ``str(v)`` 会得到 ``"[]"`` 或
    ``"{'type': ...}"`` 这种垃圾显示。这里做"类型拉直"：list 视情况
    拼接成字符串，list of dict 提取 ``description`` / ``text`` / ``name``
    三个候选字段。
    """
    # 已知但不抽出: text → 顶层 ev.text, 不进 structured 也不进 note_extras
    _KNOWN_BUT_DROP = ("text",)
    known: dict = {}
    note_extras: list = []
    for k, v in obj.items():
        if k in ("scene", "actors", "action", "objects", "note"):
            known[k] = v
        elif k in _KNOWN_BUT_DROP:
            # 顶层 text 在 _text_from_dict 已取出, 这里跳过避免污染 note
            continue
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
            # **schema-echo 兜底(v0.15+)**: Path 2 的 echo 检测因结构性
            # 标记 (``{`` / ``:``) 短路,内层 ``text`` 字段才是 echo 真身。
            # 这里在抽取后再做一次判定,命中即返回空串让上游走 dirty 占位。
            if _looks_like_schema_echo(text_value):
                LOG.warning(
                    "vfa: _sanitize_text 抽 JSON.text 后命中 schema-echo "
                    "(≥3 字段名), 返回空串: %r", text_value[:80],
                )
                return ""
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
                # 同上,内层 text 也可能命中 schema-echo
                if _looks_like_schema_echo(inner):
                    LOG.warning(
                        "vfa: _sanitize_text 抽 JSON 块.text 后命中 schema-echo "
                        "(≥3 字段名), 整段丢弃: %r", inner[:80],
                    )
                    return ""
                s = s.replace(m.group(0), inner)
            else:
                # 块里没 text 字段：去掉 { } [ ] " + 把键值分隔符变空格
                cleaned = re.sub(r'[{}\[\]"]', " ", m.group(0))
                cleaned = re.sub(r"\s*:\s*", " ", cleaned)
                cleaned = re.sub(r"\s*,\s*", " ", cleaned)
                s = s.replace(m.group(0), cleaned)

    # 终态兜底: _split_scene_frame_result 三条路径已分别过滤,但清洗过程
    # 可能拼接出意外残留 echo (例 multi-step 文本合并后)。最后一道关。
    if s and _looks_like_schema_echo(s):
        LOG.warning(
            "vfa: _sanitize_text 终态检测命中 schema-echo (≥3 字段名), "
            "返回空串: %r", s[:80],
        )
        return ""
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
    text: str
    structured: Optional[dict] = None
    _dirty: bool = False

    def to_dict(self) -> dict:
        out = {
            "id":        self.id,
            "frame_pos": self.frame_pos,
            "ts":        self.ts,
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

# 单次 LLM 推理的 asyncio.wait_for 上限（秒）。
# - qwen3-vl-4b / qwen2.5-vl-3b 健康推理 ~0.5-3s, 5s 已够
# - qwen3.5-vl-4b schema-constrained mode (~46% 概率 schema-echo, 详见
#   commit 5c33fb3) 稳态 5.5-6s
# - 方案B: 10s → 15s, 给健康慢路径 5s 余量 (commit 6fa3df9 修字段后
#   用户报 "(推理失败：超时 10s SDK 未响应)" 概率性出现, 真机
#   NPU 暂忙 / prefill 长 / 其他并发偷 NPU 都会让单次跑 > 10s)
# - wire RPC 层同步对齐到 15s (rpc_session_proxy._call_resilient),
#   避免 wire 先于 Python 触底留下 "discovery_pending" 误报
# - 兜底不变: _on_call_timeout ≥2 次连续 timeout → worker restart + 重绑
# - 2026-09: 15s → 20s。真机发现 SceneFrame schema 未注册到 worker 时
#   模型无 schema hint → 输出乱码且 decode 拖到 ~18s（正常结构化输出应
#   1-3s）。20s 给足余量；配套 wire RPC 15_000 → 20_000
#   (rpc_session_proxy.respond)，避免 wire 先触底留下 "discovery_pending"
#   误报。若需 ≥ 30s 就要查 NPU / 长 context 真因, 不应再加 timeout。
_INFER_TIMEOUT_SEC             = 20.0

# 60s 窗口 infer telemetry —— 让运维看到真实慢路径分布, 后续 v0.16+
# 可基于 p95 数据细化 timeout / 调度 / NPU 预算. 不影响任何 service 模块.
_INFER_TELEMETRY_WINDOW_SEC     = 60.0   # 检查窗口
_INFER_TELEMETRY_P95_WARN_SEC   = 4.5    # 接近 _INFER_TIMEOUT_SEC 15 的一半, 提前预警
_INFER_TELEMETRY_MAXLEN         = 200    # deque 上限, ~3-4 分钟滚动窗口 (1fps VAS)

# 慢路径 per-call timeout 上限（秒）。per-frame 走默认 5s 守住
# "快速发现 hang"；以下三条已知耗时 > 5s，调用方显式传对应上限避免
# 被 5s 默认误杀并触发 worker 重启（每个被误杀 = 30s init 成本）。
RESCUE_TIMEOUT_SEC            = 30.0  # rescue batch LLM（5000 char + ≤2000 token）
CHUNK_TIMEOUT_SEC             = 30.0  # chunk 小结（CHUNK_SUMMARY_PROMPT 模板 + N 行）
FINAL_SUMMARY_TIMEOUT_SEC     = 60.0  # Apollo 最终综述（10 chunk × 120 字 ≈ 2500 字 prompt，RK3588 实测 ~25-30s）

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
class VideoAnalysisSearchService:
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
        # 采样模式：``manual`` 固定间隔（原行为，零额外开销）/ 其余为
        # ``frame_judge.metrics()`` 暴露的判帧算法名，按画面
        # 变化采样。自动模式下 ``_every_n`` 语义变为"**最长间隔上限**"
        # （兜底：静止画面太久也强制送检，防止 VLM 结果永不刷新）。
        self._sample_mode = SAMPLE_MODE_AUTO
        self._change_threshold = DEFAULT_CHANGE_THRESHOLD
        # 本轮真正在用的判帧器；手动模式下是 FrameJudge(manual) 实例，
        # 它不做差分、不跑 RGA，所以构造它本身没有开销。
        self._change_detector = None
        # 运行中改判据时暂存 ``(mode, every_n, threshold)``，**下次 start
        # 才生效**。中途换阈值会让本轮的帧集混用两把尺子，落库的
        # ``sample_mode`` / 缓存键也跟着错位。``None`` = 无待生效改动。
        self._pending_judge = None
        # 判据 WS 推送节流（decode 侧）：解码可能跑到几百 fps，而 VLM 推理
        # 约 1 帧/数秒 —— 只靠 state 推送的话曲线会一跳一大段。
        self._judge_push_ts = 0.0
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
        # 本 run 是否已自动重启过 worker（防抖：单 run 最多 1 次）
        self._auto_restarted = False
        # v0.15: infer duration telemetry —— 60s 窗口 p95 告警
        # deque(maxlen=200) 约 3-4 分钟 1fps VAS 滚动窗口
        self._infer_durations: deque = deque(maxlen=_INFER_TELEMETRY_MAXLEN)
        self._last_telemetry_check_ts: float = 0.0
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
        # stopped / error / 新一轮挤掉旧 run）时把当前快照写入 LanceDB
        # （``kind='analysis'`` 行）。
        self._run_id: str = ""
        # 分析缓存键成分：md5（源文件本地 md5；live 流为空 → 不改
        # 用伪 md5，保证 live run 也能进历史列表）+ prompt_hash（用户
        # 原始"分析要求"的 sha256 前 16 位）。与 every_n 一起构成逐帧
        # 分析缓存的命中键 —— 逐帧结果由 prompt 问出，换 prompt 必须
        # 重新逐帧（正确性）。
        self._md5: str = ""
        self._prompt_hash: str = ""
        # 本次 run 是否为"分析缓存命中恢复"（逐帧结果来自缓存，
        # 只有综述是新跑的）。命中恢复的 run 在非 done 终态（stop /
        # error）**不写库** —— 否则会把缓存行的 state 覆盖成 stopped，
        # 导致下次 lookup_analysis（只命中 done）失配、逐帧白分析。
        self._from_cache: bool = False

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
                # 采样模式：manual = 固定间隔；auto = 按画面变化采样
                # （every_n 此时是"最长间隔"兜底）。
                "sample_mode":   self._sample_mode,
                "change_threshold": self._change_threshold,
                # 判据完整视图（分数环形缓冲 + 当前判定）—— 与 vd/vtd/vtl
                # 的 snap.judge 同名同结构，前端一套组件直接读。
                # **判据器可能还没建**：进程刚起来、还没 open 过一次分析、
                # 也没人调过 /judge 时它是 None（那时它还没被任何 decode 循环
                # 消费过，没有分数可言）。直接 .snapshot() 会让
                # GET /analysis 整个 500 —— 前端一进 VAS 页就打这个端点。
                "judge":         self._judge_snapshot_safe(),
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
                # id 空间 —— 见 api/video_analysis_search.py vfa_ws 的
                # last_run_id / last_event_id 重置逻辑。
                "run_id":        self._run_id,
                # 「分析反查」（facets / query 走 LanceDB 纯读盘）是否可用：
                # **仅完整跑完（done，已 _persist）可反查**。综述生成中
                # （summarizing，含缓存命中路径）逐帧虽已在库，但综述尚未
                # 落盘、且此刻「停止」仍可点（会转 stopped）—— 用户要求等
                # 综述跑完、整个分析流程结束后才允许反查。
                # 其它（opening / running / stopped / error）：禁止。
                "queryable": bool(self._run_id)
                             and self._state == self.STATE_DONE,
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
            # 仅当不在运行态时复位 state / run_id（api docstring 承诺
            # "运行中的分析不会被中断" —— 运行中清空只清数据、不打断）。
            # 否则前端只能靠内存标志（_progressForceIdle / _clearedRun）
            # 对抗后端 done 快照，卡片 JS 重载（切页往返）即失效，表现为
            # 清空后从别的页面切回进度条复活 100% + 反查按钮变亮。
            # run_id 清空后 WS handler 用 `if new_run_id and ...` 跳过，
            # 不会误重置 event id 空间（events 已清空）。
            if self._state not in (self.STATE_OPENING, self.STATE_RUNNING,
                                   self.STATE_SUMMARIZING):
                self._state = self.STATE_IDLE
                self._run_id = ""
        self._broadcast({"type": "state", **self.snapshot()})

    # ------------------------------------------------------------------ #
    # 控制
    # ------------------------------------------------------------------ #
    def start(self, source: str, prompt: str, every_n: int = 30,
                 sample_mode: str = "manual",
                 change_threshold: Optional[float] = None) -> dict:
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

        # 采样模式校验。``manual`` = 固定 every_n 帧（原行为，零额外开销）；
        # 其余是判帧算法名（见 ``frame_judge.metrics``）。老前端可能仍传
        # 字面量 ``auto``（历史上它硬编码走已下架的 z_mad），这里显式映射，
        # 不靠兼容层兜。
        mode_in = str(sample_mode or "manual").strip().lower()
        sample_mode = normalize_mode(mode_in)
        if sample_mode != mode_in and mode_in not in LEGACY_MODE_ALIASES:
            raise ValueError(
                "sample_mode 必须是 manual 或 %s 之一" % "/".join(metrics()))
        if mode_in in LEGACY_MODE_ALIASES:
            # 存量配置带着**旧刻度**的阈值（z_mad 是灰阶口径，线上实测 1.0），
            # 直接套到 conc 上会几乎每帧都触发 —— 比漏检更糟。这里丢掉它，
            # 走 conc 的默认值，而不是给老客户端一个 400（那会让"升级后点开始
            # 就报错"，比静默失效还难排查）。
            change_threshold = None
        if change_threshold is None:
            change_threshold = DEFAULT_CHANGE_THRESHOLD
        try:
            change_threshold = float(change_threshold)
        except (TypeError, ValueError):
            raise ValueError("change_threshold 必须是数字")
        if not (0.0 < change_threshold <= 1000.0):
            raise ValueError("change_threshold 必须在 (0, 1000] 区间")

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
                "}\n"
            )

        # 业务级校验：算力是否就绪
        from app.core.compute.registry import get_registry
        try:
            get_registry().get_session("swcap_video_describe")
        except RuntimeError as e:
            raise RuntimeError(str(e)) from e

        # 分析缓存：md5（源文件本地字节）+ prompt_hash（本次"分析
        # 要求"）→ every_n 组成命中键。命中 → 跳过 decode / 采样 / 逐帧
        # VLM，直接恢复逐帧结果 + 场景边界并重跑综述。
        #
        # 注：``prompt`` 此处已追加了 SceneFrame schema hint（确定性派生），
        # 因此基于它算 prompt_hash 与"用户原始输入"一一对应且稳定。
        is_live = "://" in source or source.startswith("/dev/video")
        md5 = ""
        if not is_live and os.path.isfile(source):
            try:
                from app.core.video.cache import file_md5
                md5 = file_md5(source)
            except Exception as e:  # noqa: BLE001
                LOG.warning("vfa: md5 计算失败（本次不查缓存）%s: %s",
                            source, e)
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        # 采样模式指纹进键：自动模式下阈值不同 → 采到的帧集不同 → 结果
        # 不同。缺了它会出现"我改了模式，结果却一模一样"（缓存串味）。
        # 手动模式返回空串，与迁移前写入的历史行完全一致（不失效旧缓存）。
        from app.core.video.cache import analysis_sample_key
        sample_key = analysis_sample_key(sample_mode, change_threshold)
        if md5:
            from app.core.video.cache import get_video_cache
            cache = get_video_cache()
            entry = (cache.lookup_analysis(md5, every_n, prompt_hash,
                                           sample_key)
                     if cache is not None else None)
            if entry:
                LOG.info("vfa: 分析缓存命中 md5=%s every_n=%d mode=%s (%d 帧) — "
                         "跳过逐帧，直接生成综述",
                         md5[:12], every_n, sample_mode,
                         len(entry.get("events") or []))
                return self._start_from_cache(
                    entry, source=source, prompt=prompt, every_n=every_n,
                    md5=md5, prompt_hash=prompt_hash,
                    sample_mode=sample_mode,
                    change_threshold=change_threshold)

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
            self._sample_mode = sample_mode
            self._change_threshold = change_threshold
            # 本轮判据配置已物化进上面三个字段 —— 运行中改的待生效值在这里
            # 被吸收，不要残留到下一轮。判据推送节流也随新 run 归零。
            self._pending_judge = None
            self._judge_push_ts = 0.0
            # 手动模式下判帧器不做差分、不跑 RGA → decode 循环走原来的
            # ``cur_pos % every_n == 0``，零额外开销。
            self._change_detector = ChangeDetector(
                sample_mode=sample_mode, every_n=every_n,
                change_threshold=change_threshold)
            self._is_live = (
                "://" in source or source.startswith("/dev/video")
            )
            self._started_at = time.time()
            # 把本帧算好的 md5 / prompt_hash 落到实例字段。
            # 旧实现只在 ``_start_from_cache``（缓存命中路径）赋值 —— 正常
            # 逐帧路径从没赋过 → ``_persist`` 用 ``self._md5 or
            # f"live-{run_id}"`` 永远落 ``live-*`` 伪 md5 + 空 prompt_hash：
            # ① 每次分析新建一行（历史列表按 md5 聚合 → 同视频多行）；
            # ② lookup_analysis 用真实文件 md5 永远查不到 → 分析缓存完全失效。
            self._md5 = md5
            self._prompt_hash = prompt_hash

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

    def _start_from_cache(self, entry: dict, *, source: str, prompt: str,
                          every_n: int, md5: str, prompt_hash: str,
                          sample_mode: str = "manual",
                          change_threshold: Optional[float] = None) -> dict:
        """缓存命中路径：恢复逐帧结果 + 场景边界，只跑综述。

        **跳过** decode / 采样 / 逐帧 VLM 三段（昂贵的 NPU 逐帧推理），
        直接从 LanceDB 恢复 ``events`` + ``scene_boundaries`` 进入
        ``STATE_SUMMARIZING``；综述完成时 ``_persist`` 按
        ``(md5, every_n, prompt_hash, sample_key)`` 幂等覆盖同一缓存行 →
        不产生重复历史条目。
        """
        if change_threshold is None:
            change_threshold = DEFAULT_CHANGE_THRESHOLD
        if self._state in (self.STATE_RUNNING, self.STATE_OPENING,
                           self.STATE_DONE, self.STATE_STOPPED,
                           self.STATE_ERROR, self.STATE_SUMMARIZING):
            self._teardown(reason="restart", persist=True)
        self._reset_state()

        # 刻意**不复用**缓存行的 run_id —— 复用会让前端按 run_id 判等的
        # 内存标志（_srcClearedRun 防重复清源 / _clearedRun 排除已清空
        # run）与新一轮碰撞，导致"第二次命中缓存后视频源不清空""清空后
        # 重来反查又变灰"。新 run_id 下 _persist 仍按
        # (md5, every_n, prompt_hash) 幂等覆盖同一缓存行，历史不重复。
        run_id = uuid.uuid4().hex
        with self._lock:
            self._run_id = run_id
            self._md5 = md5
            self._prompt_hash = prompt_hash
            self._state = self.STATE_SUMMARIZING
            self._source = source
            self._prompt = prompt
            self._every_n = every_n
            # 缓存命中恢复：逐帧来自缓存（不再解码），但采样模式要如实
            # 恢复 —— 否则前端显示的"间隔 N 帧"与实际运行模式不符，
            # 后续 _persist 也会用错的 sample_key 覆盖缓存行。
            self._sample_mode = sample_mode
            self._change_threshold = change_threshold
            # 同 start()：配置已物化，清掉待生效值（本路径不再解码，
            # 判据器只用于让历史元数据如实显示）。
            self._pending_judge = None
            self._change_detector = ChangeDetector(
                sample_mode=sample_mode, every_n=every_n,
                change_threshold=change_threshold)
            self._is_live = False
            self._started_at = time.time()
            self._error = ""
            self._from_cache = True

        # 恢复逐帧 events（顺带清洗 actors 列表，与 load_run 一致）
        evs: List[FrameEvent] = []
        max_id = 0
        for raw in entry.get("events") or []:
            if not isinstance(raw, dict):
                continue
            try:
                fe = FrameEvent(
                    id=int(raw.get("id", 0)),
                    frame_pos=int(raw.get("frame_pos", 0)),
                    ts=float(raw.get("ts", 0.0)),
                    text=str(raw.get("text", "")),
                    structured=raw.get("structured"),
                )
            except Exception:
                continue
            evs.append(fe)
            if fe.id > max_id:
                max_id = fe.id
        with self._events_lock:
            self._events.clear()
            for fe in evs:
                if fe.structured and isinstance(fe.structured.get("actors"),
                                                list):
                    fe.structured["actors"] = _sanitize_actors(
                        fe.structured["actors"])
                self._events.append(fe)
            self._next_event_id = max_id + 1

        # 恢复场景边界（否则综述退化为固定 15 帧切块）
        with self._scenes_lock:
            self._scene_boundaries.clear()
            for b in entry.get("scene_boundaries") or []:
                try:
                    self._scene_boundaries.append(int(b))
                except (TypeError, ValueError):
                    pass
            self._last_scene_hist = None

        with self._lock:
            self._sampled = len(evs)
            self._analyzed = len(evs)
            self._frame_pos = max((e.frame_pos for e in evs), default=0)

        self._broadcast({
            "type": "cached",
            "frames": len(evs),
            "run_id": run_id,
            "message": (f"已从缓存加载 {len(evs)} 帧逐帧结果，"
                        f"正在生成综述…"),
        })
        self._broadcast({"type": "state", **self.snapshot()})

        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._summary_only_loop,
            name="vfa-summary",
            daemon=True,
        )
        self._worker_thread.start()
        return self.snapshot()

    def _summary_only_loop(self) -> None:
        """缓存命中路径的 worker：只跑综述（无 decode / 逐帧线程）。"""
        try:
            self._run_summary()
        except Exception as e:  # pragma: no cover
            LOG.error("vfa: 缓存命中综述失败: %s", e)
            self._fail(f"综述失败: {e}")

    def stop(self) -> dict:
        """立即停 —— 不出综述，state='stopped'。"""
        with self._lock:
            if self._state in (self.STATE_IDLE,):
                return self.snapshot()
        self._stop_event.set()
        self._teardown(reason="stopped")
        return self.snapshot()

    # ------------------------------------------------------------------ #
    # 帧判据（前端配置入口，见 judge_api.py）
    # ------------------------------------------------------------------ #
    def _judge_snapshot_safe(self) -> dict:
        """判据器的快照，**判据器还没建时**退回配置视图（不会 500）。

        ``snapshot()`` 与 ``judge_view()`` 都要用，所以抽出来共用一份 ——
        两处各写一遍 fallback，迟早有一处漏掉 None 判断。
        """
        cd = self._change_detector
        if cd is None:
            return self._judge_cfg_view()
        return cd.snapshot()

    def _judge_cfg_view(self) -> dict:
        """「只有配置、没有运行数据」的判据视图。"""
        return {
            "mode": self._sample_mode,
            "manual": self._sample_mode == SAMPLE_MODE_MANUAL,
            "every_n": self._every_n, "threshold": self._change_threshold,
            "long_side": DEFAULT_LONG_SIDE, "cur": None, "changed": False,
            "evaluated": 0, "sampled": 0, "above_ratio": 0.0, "recent": [],
            "applied": True,
        }

    def judge_view(self) -> dict:
        """**配置**视图：前端配置区该显示什么。与 vd/vtd/vtl 的
        ``snap.judge`` 同名同结构，前端一套组件直接读。

        与 ``snapshot()["judge"]`` 的区别：那个是**本轮运行真实**的判据状态
        （喂浮框 / 曲线），这个是**用户设的**配置（喂配置区）。运行中改配置
        不会影响本轮，两者短暂不一致是有意为之。
        """
        with self._lock:
            if self._pending_judge is not None:
                mode, every_n, threshold = self._pending_judge
                return {
                    "mode": mode, "manual": mode == SAMPLE_MODE_MANUAL,
                    "every_n": every_n, "threshold": threshold,
                    "long_side": DEFAULT_LONG_SIDE, "cur": None,
                    "changed": False, "evaluated": 0, "sampled": 0,
                    "above_ratio": 0.0, "recent": [], "applied": False,
                }
            return {**self._judge_snapshot_safe(), "applied": True}

    def configure_judge(self, mode: Optional[str] = None,
                       every_n: Optional[int] = None,
                       threshold: Optional[float] = None) -> dict:
        """配置帧判据 —— 空闲时立刻生效，运行中则**下次开始分析时生效**。

        为什么运行中不热切
        ------------------
        判据决定"哪些帧送去跑 VLM"。中途换阈值 / 换算法，本轮的帧集就会
        混用两把尺子，既不可复现（同一视频两次跑结果不同），落库时的
        ``sample_mode`` 与缓存键也会按新值写、按旧值采 —— 历史列表上显示的
        "间隔 N 帧 / 阈值 20" 是假的。所以运行中只暂存（``_pending_judge``），
        空闲时没有 decode 循环在跑，直接换判据器。

        ``mode`` / ``every_n`` / ``threshold`` 任一为 None → **保留当前
        值**，不重建 ChangeDetector。JudgePayload default = "manual"（向
        后兼容），但前端只改 every_n 的请求 body 里 mode 字段是空字符串，
        judge_api 把它转 None 传进来 —— 这条路径不再误把 mode 强制设成
        manual。ChangeDetector 本身也用 SAMPLE_MODE_AUTO（gscore）当默认
        sample_mode，新装服务从一开始就是 gscore。
        """
        with self._lock:
            cur_mode = self._sample_mode
            cur_n = self._every_n
            cur_t = self._change_threshold
            new_mode = cur_mode if mode is None or mode == "" else mode
            new_n = cur_n if every_n is None else int(every_n)
            new_t = cur_t if threshold is None else float(threshold)
            cd = ChangeDetector(sample_mode=new_mode, every_n=new_n,
                                change_threshold=new_t)
            active = self._state in (self.STATE_OPENING, self.STATE_RUNNING,
                                     self.STATE_SUMMARIZING)
            if active:
                self._pending_judge = (cd.mode, cd.every_n, cd.threshold)
            else:
                self._change_detector = cd
                self._pending_judge = None
                # 同步 run 元数据：这三个字段会随快照落进缓存与历史列表，
                # 必须与真实判据一致，否则恢复出来的 run 显示的是假配置。
                self._every_n = cd.every_n
                self._sample_mode = cd.sample_mode
                self._change_threshold = cd.threshold
        LOG.info("vas judge configured: mode=%s every_n=%d threshold=%.2f%s",
                 cd.mode, cd.every_n, cd.threshold,
                 "（下次开始生效）" if active else "")
        return self.judge_view()

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
    # 持久化：LanceDB（wechat_video_cache 的 kind='analysis' 行）
    # ------------------------------------------------------------------ #
    def _maybe_persist(self) -> None:
        """在没有 run_id 或 state 仍为 idle 时跳过；其余都落库。"""
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

    def _persist(self) -> bool:
        """把当前快照写入 LanceDB（``kind='analysis'`` 行，幂等覆盖）。

        分析缓存键 = ``(md5, every_n, prompt_hash, sample_key)``；同一 run
        重复 persist（done 后 summary 更新、stop 后状态变更）会覆盖同一行，
        不产生重复。``sample_key`` 区分手动 / 自动采样（阈值不同结果不同）。

        **live 流**没有本地文件 → md5 用 ``live-<run_id>`` 伪值：依然进
        历史列表（可见 run 记录），但不可能被其它 live 流命中复用。这是
        刻意的 —— live 流内容每次不同，复用毫无意义。
        """
        from app.core.video.cache import analysis_sample_key
        with self._lock:
            run_id = self._run_id
            if not run_id:
                return False
            md5 = self._md5 or f"live-{run_id}"
            every_n = self._every_n
            prompt = self._prompt
            prompt_hash = self._prompt_hash
            sample_mode = self._sample_mode
            change_threshold = self._change_threshold
            payload = {
                "run_id":      run_id,
                "source":      self._source,
                "prompt":      prompt,
                "every_n":     every_n,
                # 采样模式随快照落库：历史列表 / 恢复 run 时前端要按模式
                # 显示（自动模式的 every_n 是"最长间隔"而非固定间隔）。
                "sample_mode": sample_mode,
                "change_threshold": change_threshold,
                "is_live":     self._is_live,
                "state":       self._state,
                "error":       self._error,
                "started_at":  self._started_at,
                "finished_at": self._finished_at,
                "summary":     self._summary,
                "summary_ts":  self._summary_ts,
                # 帧率用于反查界面计算"该帧时间戳"：frame_pos / fps = 绝对秒。
                "fps":         self._fps,
                "total_frames": self._total_frames,
            }
            summary = self._summary
            dirty_count = self._dirty_count
            source = self._source
            fps = self._fps
            total_frames = self._total_frames
            from_cache = self._from_cache
            state = self._state
        # 缓存命中恢复的 run：逐帧来自缓存（已完整），非 done 终态不写库
        # —— 避免 stop / error 把缓存行 state 覆盖掉导致后续失配。
        if from_cache and state != "done":
            return False
        with self._events_lock:
            events = [ev.to_dict() for ev in self._events]
        with self._scenes_lock:
            boundaries = list(self._scene_boundaries)
        payload["events"] = events
        payload["scene_boundaries"] = boundaries

        from app.core.video.cache import get_video_cache
        cache = get_video_cache()
        if cache is None:
            return False

        duration_s = 0.0
        if fps and total_frames:
            duration_s = float(total_frames) / float(fps)
        filename = os.path.basename(source) if source else ""
        return cache.save_analysis(
            md5=md5, every_n=every_n, prompt=prompt,
            prompt_hash=prompt_hash, run_id=run_id, events=events,
            scene_boundaries=boundaries, summary=summary,
            meta={"filename": filename, "source_path": source,
                  "duration_s": duration_s, "fps": fps,
                  "dirty_count": dirty_count},
            payload=payload,
            sample_key=analysis_sample_key(sample_mode, change_threshold),
        )

    def list_runs(self) -> List[dict]:
        """从 LanceDB 取分析历史（按 finished_at / started_at 倒序）。

        响应结构与旧的 JSON 扫描版**向后兼容**（老字段不动）：每条含
        run_id / source / prompt / every_n / state / started_at /
        finished_at / events_count / has_summary / path / total_frames / fps，
        另增 ``sample_mode``（历史列表按模式显示"间隔 N 帧"还是"自动判帧"）。
        ``total_frames`` / ``started_at`` / ``finished_at`` 从行内
        ``meta_json``（完整 payload）取 —— 新 run 一定有值，不再需要
        PyAV 自愈探测。
        """
        from app.core.video.cache import get_video_cache
        cache = get_video_cache()
        if cache is None:
            return []
        out: List[dict] = []
        for a in cache.list_analysis():
            payload = a.get("payload") or {}
            out.append({
                "run_id":       a["run_id"],
                "source":       str(payload.get("source") or ""),
                "prompt":       a.get("prompt") or "",
                "every_n":      int(a.get("every_n") or 30),
                "sample_mode":  str(payload.get("sample_mode") or "manual"),
                "state":        a.get("state") or "unknown",
                "started_at":   float(payload.get("started_at") or 0.0),
                "finished_at":  float(payload.get("finished_at") or 0.0),
                "events_count": int(a.get("frame_count") or 0),
                "has_summary":  bool(a.get("summary")),
                "path":         f"lancedb://{a['run_id']}",
                "total_frames": int(payload.get("total_frames") or 0),
                "fps":          float(a.get("fps") or 0.0),
            })
        out.sort(key=lambda r: -(r.get("finished_at") or r.get("started_at") or 0))
        return out

    def load_run_meta(self, run_id: str) -> dict:
        """轻量读分析记录元数据：不加载 events 数组（动辄几千条）。

        用途：取帧 endpoint 只需知道原始视频路径 + 元信息做展示，
        不需要把整个 run 加载到内存替换当前状态。
        """
        from app.core.video.cache import get_video_cache
        cache = get_video_cache()
        entry = cache.get_analysis_by_run_id(run_id) if cache else None
        if entry is None:
            raise ValueError(f"run not found: {run_id}")
        payload = entry.get("payload") or {}
        return {
            "run_id":       entry.get("run_id") or run_id,
            "source":       str(payload.get("source") or ""),
            "prompt":       str(entry.get("prompt") or ""),
            "every_n":      int(entry.get("every_n") or 30),
            "sample_mode": str(payload.get("sample_mode") or "manual"),
            "is_live":      bool(payload.get("is_live", False)),
            "state":        str(entry.get("state") or "unknown"),
            "total_frames": int(payload.get("total_frames") or 0),
            "fps":          float(entry.get("fps") or 0.0),
        }

    def load_run_events_readonly(self, run_id: str) -> List[dict]:
        """只读获取历史 run 的事件（不碰 state / 内存 events / 线程 / 广播）。

        反查（facets / query）是纯查询，不能走 ``load_run`` —— 后者末尾会
        把 ``_state`` 重置为 ``IDLE`` 并广播，导致前端「分析反查」在打开 /
        关闭一次 modal 后就不可再点。本方法只从 LanceDB 读 events，并复刻
        ``load_run`` 的 actors 清洗，其余原样返回。
        """
        from app.core.video.cache import get_video_cache
        cache = get_video_cache()
        entry = cache.get_analysis_by_run_id(run_id) if cache else None
        if entry is None:
            raise ValueError(f"run not found: {run_id}")
        out: List[dict] = []
        for raw in entry.get("events") or []:
            if not isinstance(raw, dict):
                continue
            ev = dict(raw)
            s = ev.get("structured")
            if isinstance(s, dict) and isinstance(s.get("actors"), list):
                cleaned = _sanitize_actors(s["actors"])
                if len(cleaned) != len(s["actors"]):
                    s = dict(s)
                    s["actors"] = cleaned
                    ev["structured"] = s
            out.append(ev)
        return out

    def load_run(self, run_id: str) -> dict:
        """把指定 run_id 的分析记录加载到内存（替换当前状态）。"""
        from app.core.video.cache import get_video_cache
        cache = get_video_cache()
        entry = cache.get_analysis_by_run_id(run_id) if cache else None
        if entry is None:
            raise ValueError(f"run not found: {run_id}")
        data = dict(entry.get("payload") or {})
        data["events"] = entry.get("events") or []
        data["scene_boundaries"] = entry.get("scene_boundaries") or []
        # 先把当前活跃 run（若有）落库
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
            # 采样模式随历史快照恢复（缺省手动 = 迁移前的老行）。
            self._sample_mode = str(data.get("sample_mode") or "manual")
            # 存量快照里的阈值是**旧度量刻度**（z_mad 灰阶口径），不能直接
            # 沿用 —— 判据器会把它迁成 conc+默认阈值，这里若不同步，
            # 落库的 sample_mode / 缓存键就会与真正在跑的判据对不上。
            if self._sample_mode in LEGACY_MODE_ALIASES:
                self._sample_mode = normalize_mode(self._sample_mode)
                self._change_threshold = DEFAULT_CHANGE_THRESHOLD
            else:
                self._change_threshold = float(
                    data.get("change_threshold")
                    or DEFAULT_CHANGE_THRESHOLD)
            # 判帧器按恢复出的模式重建：历史 run 的 snapshot 会带上真实
            # 模式，否则前端显示的"间隔 N 帧"与实际运行模式不符。
            self._change_detector = ChangeDetector(
                sample_mode=self._sample_mode, every_n=self._every_n,
                change_threshold=self._change_threshold)
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
        # 恢复场景边界（综述分组依据）—— 不恢复会让综述退化为固定 15 帧切块
        with self._scenes_lock:
            self._scene_boundaries.clear()
            for b in data.get("scene_boundaries") or []:
                try:
                    self._scene_boundaries.append(int(b))
                except (TypeError, ValueError):
                    pass
            self._last_scene_hist = None
        # 加载完 → 状态必须是 idle（已脱离任何运行），否则下次 start 会拒
        with self._lock:
            self._state = self.STATE_IDLE
            self._error = ""
        self._broadcast({"type": "state", **self.snapshot()})
        return self.snapshot()

    def delete_run(self, run_id: str) -> bool:
        from app.core.video.cache import get_video_cache
        cache = get_video_cache()
        if cache is None:
            return False
        return cache.delete_analysis_by_run_id(run_id) > 0

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
            self._md5 = ""
            self._prompt_hash = ""
            self._from_cache = False
            self._frame_pos = 0
            self._sampled = 0
            self._analyzed = 0
            self._dirty_count = 0
            self._total_frames = 0
            self._width = 0
            self._height = 0
            self._fps = 0.0
            self._finished_at = 0.0
            # 判帧器状态必须清 —— 否则上一 run 的参照帧会污染新 run 的
            # 首帧判定（首帧不送检，用户点"开始"后迟迟没结果）。
            if self._change_detector is not None:
                self._change_detector.reset()
        # 每次 run 重置超时计数 + 自动重启防抖（避免跨 run 残留）
        self._consecutive_timeouts = 0
        self._auto_restarted = False
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
                change_detector = self._change_detector
            # 场景检测 —— 每帧调用（cv2 操作很便宜）
            self._update_scene_detection(frame, cur_pos)
            # 取样判定交给 ChangeDetector：手动模式走固定间隔（逐位不变、
            # 零额外开销），自动模式走毫秒级判帧（变化 或 满 every_n 帧
            # 兜底才送）。它内部已把异常降级为"送检"，不静默丢帧。
            take = change_detector.should_sample(frame, cur_pos)
            # 判据单独推一路（每 0.5s 最多一次）。只靠 state 推送的话，
            # 解码已经跑出去几百帧而 VLM 才回来一帧 —— 曲线会一跳一大段，
            # 浮框里的"这一帧变了没有"也严重滞后。手动模式无分数可推，跳过。
            if change_detector.is_auto:
                now = time.monotonic()
                if now - self._judge_push_ts >= 0.5:
                    self._judge_push_ts = now
                    self._broadcast({"type": "judge",
                                     "judge": change_detector.snapshot()})
            # 反压点：worker 还在推理前一个样本时，decode 停
            # 在这里直到 worker 把上一个样本从队列里 get 走（set event）。
            if take:
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
        # 自然拿到空串。VAS 端 ``_split_scene_frame_result`` 检测到
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
                    timeout_sec=RESCUE_TIMEOUT_SEC,
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
                text = self._call_llm(prompt, None, max_new_tokens=CHUNK_MAX_NEW_TOKENS,
                                   timeout_sec=CHUNK_TIMEOUT_SEC)
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
                    timeout_sec=FINAL_SUMMARY_TIMEOUT_SEC,
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
        """每次 _call_llm 触发 _INFER_TIMEOUT_SEC 超时时调用。

        连续 ≥2 次 → 触发承载 ``swcap_video_describe`` 推理的 worker
        重启 + **自动重绑回原 NPU**（``restart_and_rebind_instance_local``）
        → 清掉 wedged 的 C 层，worker 恢复 running 后后续帧继续。

        **防抖** ``_auto_restarted``：单次 run 最多自动重启 1 次 ——
        重启期间 ~60s 所有帧都会失败，若模型本身持续超时（资源紧张 /
        模型问题）无限重启会让整个 run 卡死。第 2 次起只 LOG warning，
        让 run 按正常 error 路径收敛。

        历史教训：本方法此前调 ``stop_instance_local``（unload → kill -9
        → 从 registry 移除），但**不** respawn / bind，导致后续
        ``get_session`` 抛 ``功能未启用``，分析 + 综述全废。

        不再硬编码 ``"qwen3-vl-4b"`` —— 通过 registry 找当前本机 running
        的 ``swcap_video_describe`` 实例（qwen3-vl-4b 或 qwen3.5-vl-4b
        都行），按实际加载的模型 + NPU 重启。
        """
        self._consecutive_timeouts += 1
        if self._consecutive_timeouts < 2:
            return
        try:
            from app.core.compute.registry import get_registry
            registry = get_registry()
            target_model = None
            target_npu = -1
            for inst in registry.list_with_source():
                caps = inst.get("capability_types") or []
                src = inst.get("_source") or ""
                if ("swcap_video_describe" in caps
                        and inst.get("status") == "running"
                        and src in ("spawned", "adopted_local")):
                    target_model = inst.get("model_id")
                    target_npu = inst.get("npu_index")
                    break
            if (not target_model
                    or not isinstance(target_npu, int)
                    or target_npu < 0):
                LOG.warning(
                    "vfa: 重启跳过 —— 无本机 running swcap_video_describe 实例")
                self._consecutive_timeouts = 0
                return
            if self._auto_restarted:
                LOG.warning(
                    "vfa: %d 次连续 _call_llm 超时，但本 run 已自动重启过一次 "
                    "—— 跳过（避免重启循环）",
                    self._consecutive_timeouts,
                )
                self._consecutive_timeouts = 0
                return
            LOG.warning(
                "vfa: %d 次连续 _call_llm 超时，触发 %s (npu=%d) "
                "worker 重启 + 重绑（清 wedged C 层）",
                self._consecutive_timeouts, target_model, target_npu,
            )
            self._auto_restarted = True
            registry.restart_and_rebind_instance_local(target_model, target_npu)
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

    def _record_infer_duration(self, elapsed_sec: float) -> None:
        """记录单次 _call_llm 耗时到 200 帧滚动 deque; 60s 窗口 p95 > 4.5s 告警。

        设计: 抽样 (而不是每帧) + 周期检查 (而不是 per-frame LOG)。
        - 200 帧 deque = ~3-4 分钟 (1fps VAS) 滚动, 长窗口更平滑
        - 60s 节流: 避免 1fps VAS 每帧 1 行 LOG 刷屏
        - p95 > 4.5s 阈值 = 接近 _INFER_TIMEOUT_SEC 15s 的一半, 提前预警
          慢路径聚集 (例如: 多人同时跑 / NPU 抢占 / 队列调度), 让运维
          在 timeout 真的开始报错前就能看到
        - 不影响任何 service 模块, 仅 +1 dict append + period LOG

        防御性: 兼容 v0.15 之前启动的旧 instance (那时 __init__ 没
        初始化 telemetry 字段), 静默跳过 telemetry 即可, 避免
        AttributeError 把每个 _call_llm 调用都崩掉。
        """
        if not hasattr(self, "_infer_durations"):
            return
        self._infer_durations.append(elapsed_sec)
        now = time.time()
        if (now - self._last_telemetry_check_ts) < _INFER_TELEMETRY_WINDOW_SEC:
            return
        if len(self._infer_durations) < 10:
            # 样本不足, 跳过本次检查, 等下一轮再 emit
            self._last_telemetry_check_ts = now
            return
        sorted_d = sorted(self._infer_durations)
        n = len(sorted_d)
        p50 = sorted_d[n // 2]
        p95_idx = min(int(n * 0.95), n - 1)
        p95 = sorted_d[p95_idx]
        p99_idx = min(int(n * 0.99), n - 1)
        p99 = sorted_d[p99_idx]
        if p95 > _INFER_TELEMETRY_P95_WARN_SEC:
            LOG.warning(
                "vfa: infer duration telemetry (60s window, n=%d): "
                "p50=%.2fs p95=%.2fs p99=%.2fs — 接近 _INFER_TIMEOUT_SEC=15s, "
                "建议排查 NPU 抢占 / 队列调度 / 上下文长度",
                n, p50, p95, p99,
            )
        self._last_telemetry_check_ts = now

    def _call_llm(self, prompt: str, frame_bgr, *,
                  max_new_tokens: Optional[int] = None,
                  generating=None,
                  timeout_sec: Optional[float] = None) -> Any:
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

        **回滚开关** ``VAS_FORCE_JSON_PROMPT=1``：若改动导致填充率反
        而下降，可设该环境变量恢复旧 prompt 改造（不推荐，仅作为
        紧急回滚口）。
        """
        from app.core.compute.registry import get_registry, get_session_call_lock
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
            # The tokenizer lives in the worker, not here: ``sess`` is an
            # RPC handle and has no ``_backend``. Ask over the control
            # plane instead of silently skipping the diagnostic — a
            # diagnostic that quietly stops running is worse than none,
            # because the log then looks like the prompt was fine.
            from app.core.compute.proxy import count_tokens as _count_tokens
            spec = getattr(sess, "spec", None)
            if spec is not None:
                user_t = _count_tokens(sess, [prompt])[0]
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
            and os.environ.get("VAS_FORCE_JSON_PROMPT") == "1"
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
                t0 = time.time()  # telemetry: SDK call start
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
                # 加 timeout 防 SDK 永久 hang（rknn3lite 偶发 bug 或
                # NPU 满载时会等无穷）。
                #
                # 默认 5s（per-frame 设计意图：健康推理单帧稳态 < 5s，超
                # 出必有异常，连续 ≥2 次触发 worker 重启清 wedged C 层）。
                # 已知的慢路径（chunk/final summary、rescue batch）由调用方
                # 显式传 timeout_sec=30s/60s，不走 5s 默认。
                _eff_timeout = float(
                    timeout_sec if timeout_sec is not None else _INFER_TIMEOUT_SEC
                )
                result_holder = [None]
                error_holder = [None]
                finished = [False]

                def _runner():
                    loop = asyncio.new_event_loop()
                    try:
                        result_holder[0] = loop.run_until_complete(
                            asyncio.wait_for(coro, timeout=_eff_timeout)
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
                # join 与 asyncio.wait_for 同一上限,不额外留余量。
                t.join(timeout=_eff_timeout)

                # 防呆：超时/异常路径返回安全的 error string 而非 None
                # （下游 text.startswith("(") 会 NoneType 崩溃）
                if not finished[0]:
                    LOG.warning("vfa: _call_llm thread %.0fs 内未完成（SDK 卡死）",
                                _eff_timeout)
                    _interrupt_rknn_session(sess, wait_sec=5.0)
                    self._on_call_timeout()
                    self._record_infer_duration(time.time() - t0)
                    return f"(推理失败：超时 {_eff_timeout:.0f}s SDK 未响应)"
                if error_holder[0] == "timeout":
                    LOG.warning("vfa: LLM 推理超时（%.0fs），可能 NPU 满载或 prompt 过长",
                                _eff_timeout)
                    # 关键：asyncio.wait_for 只取消 Python 协程，C 层
                    # rknn3.session_run() 还在 NPU 上跑（无人在等它返回）。
                    # 不显式 session_stop() + 等待，下次 sess.reset() → clear_kvcache()
                    # 会阻塞等 C 层跑完（RK3588 实测 5-30s），连锁卡死后续每个
                    # chunk。计算 worker 已用同款模式（compute_worker.py
                    # embed_video handler:611-619）。
                    #
                    # session_stop() 是 async signal，C 层在 kernel 间
                    # 协作点检查 → 实际释放可能花数秒。这里 sync 等待让 C 层
                    # 处理 stop 信号并实际让出 NPU mutex，下次 reset() 不阻塞。
                    _interrupt_rknn_session(sess, wait_sec=5.0)
                    self._on_call_timeout()
                    self._record_infer_duration(time.time() - t0)
                    return f"(推理超时：{_eff_timeout:.0f}s 内 LLM 未响应)"
                if error_holder[0] is not None:
                    # 关键：不要 raise！否则 _run_summary 异常退出，
                    # state 卡在 STATE_RUNNING，前端永远显"分析中"。
                    # 改成返回 error string，_run_summary 会进入 fallback
                    # 路径（startswith("(")），下游 graceful 处理。
                    LOG.warning("vfa: _call_llm 异常: %s", error_holder[0])
                    self._reset_timeout_counter()
                    self._record_infer_duration(time.time() - t0)
                    return f"(推理失败：{error_holder[0]})"
                if result_holder[0] is None:
                    self._reset_timeout_counter()
                    self._record_infer_duration(time.time() - t0)
                    return f"(推理失败：返回为空)"
                result = result_holder[0]
                self._reset_timeout_counter()
                self._record_infer_duration(time.time() - t0)
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
            self._record_infer_duration(time.time() - t0)
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

        # ``respond()`` 归一化。``generating=SceneFrame`` 校验成功时返回真正的
        # SceneFrame 实例（``model_dump()`` 后面就是它自己）；校验失败时
        # SDK 的 ``_safe_validate`` 返回**裸 str**（模型原文），而不是
        # 抛异常 —— 那正是我们想要的：模型写了什么得留住，下游
        # ``_split_scene_frame_result`` 的 str 路径会再抽一次 JSON。
        #
        # 所以这里要处理的是"一个 schema 实例，但下游只认 dict/str"这
        # 一种情况，不是以前那种 RPC 包裹对象（``._raw``）。
        if generating is None:
            if not isinstance(result, str):
                text_attr = getattr(result, "text", None)
                if isinstance(text_attr, str):
                    result = text_attr
                else:
                    result = str(result)
        else:
            if not isinstance(result, (dict, str)):
                result = result.model_dump()
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
    —— 当前 VAS 路径只用 VLM/text backend，这一处覆盖足够。

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
    if sess is None:
        return
    try:
        # 推理在另一个进程：``sess._backend`` 恒为 None，直接摸
        # ``_backend.rknn_llm.session_stop()`` 会静默 return —— 也就是
        # 这个函数以前等于没做。``sess.stop()`` 是 transport 的取消
        # 通道，把标志发到旁路 topic 后立即返回。
        sess.stop()
    except Exception as ex:
        LOG.warning("vfa: session.stop() 异常（继续）: %s", ex)
    if wait_sec > 0:
        # 同步等待：C 层在 kernel 间检查 stop flag 并让出 NPU mutex。
        # Python 端 sleep（C 线程不阻塞），sleep 期间主线程可以处理
        # 其他事件；上限 wait_sec 防止极端 wedged 情况无限等。
        time.sleep(wait_sec)


# --------------------------------------------------------------------------- #
# Singleton
# --------------------------------------------------------------------------- #
_service: Optional[VideoAnalysisSearchService] = None
_service_lock = threading.Lock()


def get_service() -> VideoAnalysisSearchService:
    global _service
    with _service_lock:
        if _service is None:
            _service = VideoAnalysisSearchService()
        return _service
