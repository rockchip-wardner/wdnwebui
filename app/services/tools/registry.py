"""工具注册中心：单例 + 元数据查询。

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
"""
from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

import numpy as np

from wdnai import Callable as WdnaiCallable
from wdnai import Field as WdnaiField


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
    """执行工具时的上下文，``__call__(frame_bgr, ctx)`` 第二个参数。"""
    width: int
    height: int
    prompt: str


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
    global _registry
    with _registry_lock:
        if _registry is None:
            from .tts_alert import TtsAlert

            reg = ToolRegistry()
            reg.register(
                key="tts_alert",
                name="语音提醒",
                description=(
                    "语音提醒工具：检测到需要立即提醒的场景时调用，"
                    "传入提醒文本，将通过 Qwen3-TTS 在本机播放。"
                ),
                requires_capability="swcap_audio_tts",
                tool_class=TtsAlert,
                icon="🔊",
            )
            _registry = reg
        return _registry
