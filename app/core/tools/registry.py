"""工具注册中心：单例 + 元数据查询 + 可用性过滤。

每个工具以 ``@Callable`` 子类的形式出现，工具在模块加载时通过
``register(...)`` 注入到本注册表；key 与 Callable 类一一对应。
前端 / detector / API 都通过 ``get_tool_registry()`` 取这一份
真源，不会重复装饰器。

wdnai 在工具模块顶部 import：tts_alert 等用例直接 ``from wdnai
import Callable, Field``，注册过程不引入任何 lazy 间接层，
与设计要求一致。

注意：原 ``target_detection`` 工具（bbox + ROI 描述）已下线 —
视频目标检测页直接用 ``session.respond(generating=TargetDoc)``
结构化输出，不再走 @Callable 工具链路。当前唯一保留的工具是
``tts_alert``（音视频描述页使用）。

新增一个工具的完整步骤（零框架改动）：

1. 在本包内新增 ``<tool>.py``：定义 ``@WdnaiCallable`` 类，
   实现 ``__call__(self, frame_bgr, ctx)``；
2. 在 :func:`get_tool_registry` 里 ``reg.register(...)`` 一行，
   需要模型算力的工具标注 ``requires_capability``（纯软件工具
   传 None）；
3. 完成 —— schema 注入、worker 端解析、capability 过滤、
   chat_agent 的 agent-loop 全部自动生效。

**__call__ 返回值契约**（chat_agent 依赖此契约分流，见
``app/services/chat_agent.py``）：

* side-effect 型：返回 dict 带 ``consume_iteration: True``
  （如 tts_alert）——执行本身即目的；chat_agent 成功后走
  wrap-up 轮生成自然语言确认。
* 结果型：返回业务数据 dict、不带该标志——结果被序列化为
  transcript 回喂模型继续推理（可链式再次 tool_call）。
* 抛异常：chat_agent 视为该次调用失败，以失败文案出口。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from threading import RLock
from typing import Callable, Dict, List, Optional, Tuple, Type

from wdnai import Callable as WdnaiCallable


@dataclass(frozen=True)
class ToolSpec:
    """对外暴露的元数据。``key`` 是 URL-safe 唯一 id，
    ``requires_capability`` 为 None 表示不需要 NPU 算力。"""
    key: str
    name: str
    description: str
    requires_capability: Optional[str]
    tool_class: Type
    icon: str = ""
    fields: Tuple[Tuple[str, str], ...] = ()   # (name, description)


@dataclass
class ToolContext:
    """执行工具时的上下文，``__call__(frame_bgr, ctx)`` 第二个参数。

    ``user_id`` / ``channel`` 标识对话来源（wechat 场景由
    ``chat_agent.run_chat_with_tools`` 从桥接层透传；web 场景
    ``channel="web"`` 且 ``user_id=None``），供渠道感知型工具使用。
    """
    width: int
    height: int
    prompt: str
    user_id: str = ""
    channel: str = ""


class ToolRegistry:
    """进程级单例。线程安全（内部 RLock）。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._tools: Dict[str, ToolSpec] = {}

    # ------------------------------------------------------------------ #
    def register(
        self,
        *,
        key: str,
        name: str,
        description: str,
        requires_capability: Optional[str],
        tool_class: Type,
        icon: str = "",
    ) -> None:
        spec = ToolSpec(
            key=key,
            name=name,
            description=description,
            requires_capability=requires_capability,
            icon=icon,
            tool_class=tool_class,
            fields=self._extract_fields(tool_class),
        )
        with self._lock:
            self._tools[key] = spec

    @staticmethod
    def _extract_fields(tool_class: Type) -> Tuple[Tuple[str, str], ...]:
        """Read ``model_fields`` (pydantic v2) — populated by the
        ``@Callable`` decorator — to build a (name, description) list
        for the UI to render helper text per field.
        """
        out: List[Tuple[str, str]] = []
        for fname, fobj in getattr(tool_class, "model_fields", {}).items():
            desc = ""
            try:
                desc = (fobj.description or "").strip()
            except Exception:
                desc = ""
            out.append((fname, desc))
        return tuple(out)

    # ------------------------------------------------------------------ #
    def list_specs(self) -> List[ToolSpec]:
        with self._lock:
            return list(self._tools.values())

    def get(self, key: str) -> ToolSpec:
        with self._lock:
            spec = self._tools.get(key)
            if spec is None:
                raise KeyError(f"未知 tool: {key!r}")
            return spec

    def get_class(self, key: str) -> Type:
        return self.get(key).tool_class

    def has(self, key: str) -> bool:
        with self._lock:
            return key in self._tools

    def keys(self) -> List[str]:
        with self._lock:
            return list(self._tools.keys())


# --------------------------------------------------------------------------- #
# 单例 + 仅保留的一个内置工具
# --------------------------------------------------------------------------- #
_registry: Optional[ToolRegistry] = None
_registry_lock = RLock()


def get_tool_registry() -> ToolRegistry:
    """Return the singleton tool registry.

    Tools are now registered by their owning cards at boot time
    (see ``app/apps/<card>/register_api()`` → ``reg.register(...)``).
    The registry itself is created empty here; cards wire their
    own tools in.

    Example (in a card's ``api.py`` or ``__init__.py``)::

        from app.core.tools.registry import get_tool_registry
        from app.apps.video_detection.tts_alert import TtsAlert

        get_tool_registry().register(
            key="tts_alert",
            name="语音提醒",
            description="...",
            requires_capability="swcap_audio_tts",
            tool_class=TtsAlert,
            icon="🔊",
        )
    """
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = ToolRegistry()
        return _registry


def resolve_available_tool_names() -> List[str]:
    """列出**当前可用**工具的 ``@Callable`` class 名（供 chat_run）。

    capability 门控：``requires_capability`` 非 None 的工具要求对应
    模型实例已加载（``get_registry().get_session(cap)`` 成功），否则
    从返回列表中排除 —— 模型根本看不到不可用工具的 schema，从源头
    避免调用必败工具。纯软件工具（None）始终可用。

    任何单条失败都不影响其余工具；整体异常回退空列表。
    """
    out: List[str] = []
    try:
        specs = get_tool_registry().list_specs()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("tools").warning("resolve_available_tool_names: %r", exc)
        return out
    for spec in specs:
        cap = spec.requires_capability
        if cap:
            try:
                from app.core.compute.registry import get_registry
                get_registry().get_session(cap)
            except Exception:  # noqa: BLE001
                continue
        out.append(spec.tool_class.__name__)
    return out
