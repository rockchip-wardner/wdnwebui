"""Built-in ``@Callable`` tool registration (worker + main process).

**Why this module exists**:
    The tool registry (``app.core.tools.registry``) is a process-level
    singleton. v0.4 起 ``VideoSearch`` / ``TtsAlert`` 等内置工具的
    注册散落在两个调用点 ——

    1. ``app/system/__init__.py:register_all()`` 注册 ``VideoSearch``
       （im-bridge 拥有）
    2. ``app/apps/video_detection/__init__.py:register_api()`` 注册
       ``TtsAlert`` （音视频描述页拥有）

    这两个调用点都只在**主进程** uvicorn 里跑，**compute_worker 子
    进程**从来不执行。结果：

    * 主进程 ``session.respond(..., tools=[VideoSearch])``
      发到 worker
    * 主进程 ``chat_agent._tool_classes(['VideoSearch'])`` 在自己的
      空 registry 里查不到类 → ``LOG.warning("unknown tool name ..."
      -- skipping)``
    * ``tool_specs=None`` → ``build_chatml_prompt()`` 出来的 prompt
      完全没有工具 schema → 模型看不到 VideoSearch 描述，只能
      "假装在调用"（hallucinate 写 "I will call VideoSearch" 然后
      凭空捏造返回）
    * 同样 ``parse_classes=[]`` → 即使模型真的按格式吐了 JSON 也
      没人解析

    实测 prompt 从正常的 ~1900 字符（含工具描述）缩到 192 字符。

**修复**:
    把"注册内置工具"集中到本文件，**幂等**（多次调用不重复注册），
    主进程和 worker 进程各自在启动时调一次。模块里只**惰性 import**
    工具类（避免冷启动 worker 时拉一堆卡片 import 链），import 失败
    走 ``LOG.warning`` 静默跳过 —— 工具模块本身已是 optional 资产，
    缺模块 = 工具不可用，与其他路径行为对齐。

**新增内置工具**:
    直接在本文件 ``_REGISTRARS`` 列表里加 ``(label, register_fn)``
    一行；``register_fn`` 写一个 try/except 块调 ``registry.register``，
    异常走 ``LOG.warning``。
"""
from __future__ import annotations

import logging
from typing import Callable, List, Tuple

_LOG = logging.getLogger("app.core.tools.builtins")

# 模块级幂等 flag —— 同一进程内多次调用 ``register_builtin_tools()``
# 只在第一次真正跑 import + register，避免每次 chat_run / infer 都重
# 复 import 卡片模块。uvicorn 主进程 + compute_worker 子进程各持一份
# flag（独立 Python 进程），互不影响。
_REGISTERED = False


def _register_video_search(registry) -> None:
    """Lazy-import ``app.system.im_bridge.video_search.VideoSearch``
    and register it.

    Worker 进程 import 该模块会顺道 import ``video_manager``；后者顶
    层只 import 标准库 + wdnai 等基础 SDK，不触发 wdnav/RKNN init，
    安全。

    **渠道限定** (``channels=("chat_wechat",)``):
    VideoSearch 只对微信聊天桥接渠道（``channel="chat_wechat"``）有
    意义 —— ``__call__`` 依赖 ``ctx.user_id`` 拉对应用户上传的视频
    索引。web 文本对话页（``channel="chat_web"``）``user_id`` 始终
    空，工具必返 ``no_video`` —— 历史上有用户报告模型在 web 端
    误调 VideoSearch,既浪费一轮推理又污染对话占位。限定渠道后
    web 端 schema 不再注入此工具,模型无从调用。
    """
    try:
        from app.system.im_bridge.video_search import VideoSearch
    except Exception as e:  # noqa: BLE001
        _LOG.warning("builtins: failed to import VideoSearch: %s", e)
        return
    registry.register(
        key="video_search",
        name="视频内容检索",
        description=(
            "在用户最新发送的视频中按文字描述检索相关片段,"
            "返回命中时间段与相似度;依赖视频已完成后台分析"
            "(收到视频后自动进行)。"
        ),
        requires_capability="swcap_video_retrieval",
        tool_class=VideoSearch,
        icon="🎬",
        channels=("chat_wechat",),
    )
    _LOG.info("builtins: registered VideoSearch")


def _register_tts_alert(registry) -> None:
    """Lazy-import ``app.apps.video_detection.tts_alert.TtsAlert``
    and register it.

    Worker 进程 import 该模块只 import wdnai + 标准库 + 一段文本；
    不触发音视频描述页的 VideoSession / RKNN init。卡片注册路径
    （主进程 ``register_api``）调同一个函数（idempotent）。

    **渠道限定** (``channels=("chat_web", "chat_wechat")``):
    语音提醒在 web 文本对话与微信聊天桥接两端都有合理用例：
    用户在 chat 页可以让模型播一句话,在微信侧桥接可以让模型回复
    完用户后顺便播报关键提醒。detector 音视频描述页的 tts_alert
    路径不走 chat_run,因此不受 channels 限定影响（detector 直接传
    ``tools=[TtsAlert]`` 给 ``session.respond``,不走
    ``resolve_available_tool_names``）。
    """
    try:
        from app.apps.video_detection.tts_alert import TtsAlert
    except Exception as e:  # noqa: BLE001
        _LOG.warning("builtins: failed to import TtsAlert: %s", e)
        return
    registry.register(
        key="tts_alert",
        name="语音提醒",
        description=(
            "语音提醒工具：检测到需要立即提醒的场景时调用，"
            "传入提醒文本，将通过 Qwen3-TTS 在本机播放。"
        ),
        requires_capability="swcap_audio_tts",
        tool_class=TtsAlert,
        icon="🔊",
        channels=("chat_web", "chat_wechat"),
    )
    _LOG.info("builtins: registered TtsAlert")


# 新增内置工具时在此追加；label 仅日志用。
_REGISTRARS: List[Tuple[str, Callable]] = [
    ("video_search", _register_video_search),
    ("tts_alert", _register_tts_alert),
]


def register_builtin_tools(registry) -> None:
    """把 wdnwebui 自带的 ``@Callable`` 工具全部注册到 ``registry``。

    调用方：

    * 主进程 — 替代 ``app/system/__init__.py`` 与
      ``app/apps/video_detection/__init__.py`` 里原本的内联注册；
      进程内多次调用幂等；
    * chat agent 第一次
      调用前（或 ``main()`` 启动早期）调一次，保证 prompt 注入 +
      输出解析都能在 worker registry 里查到类；
    * ``get_tool_registry`` 首次调用 — 兜底 reload 后空 registry
      场景。

    幂等：``_REGISTERED`` 全局 flag + 该 registry 已含任一 builtin
    key → 跳过。uvicorn reload 时 ``_REGISTERED`` 被重置为 False
    但 ``_registry`` 是新空对象 → 本函数被 ``get_tool_registry``
    触发时**重新注册**（这是 reload 修复的关键 —— 否则 reload
    后 chat 流程拿不到工具，``resolve_available_tool_names`` 返
    空 list,模型没工具 schema,返空 answer,前端抛
    "（模型未输出有效内容）"）。

    任意一个 ``_REGISTRARS`` 元素 import 失败都不会中断其他注册；
    失败走 ``LOG.warning``（与既有行为一致）。
    """
    global _REGISTERED
    if _REGISTERED:
        existing_keys = {s.key for s in registry.list_specs()}
        if existing_keys & {key for key, _ in _REGISTRARS}:
            return
    for label, fn in _REGISTRARS:
        try:
            fn(registry)
        except Exception as e:  # noqa: BLE001
            _LOG.warning("builtins: registrar %s raised: %s", label, e)
    _REGISTERED = True


def reset_registration_flag() -> None:
    """测试 / 调试用：清幂等 flag，允许重跑注册。生产不要调。"""
    global _REGISTERED
    _REGISTERED = False