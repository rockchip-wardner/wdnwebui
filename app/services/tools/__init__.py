"""音视频描述页可调用的 rkai 工具集。

一个内置工具：

* ``tts_alert``     — 语音提醒工具：模型给出文本，触发 TtsSession
  在本机播放；依赖 ``audio_output`` capability。

工具按 ``@Callable`` 协议暴露给 rkai，会话模型选中时 SDK 返回一个
已填充字段的实例，detector 再执行 ``instance(frame, ctx)`` 拿到
具体动作的副作用（调 TTS）。SDK 不负责自动 ``__call__``，
这点与 ``examples/function_call.py`` / ``object_detect.py``
保持一致。
"""
from .registry import (
    ToolContext,
    ToolRegistry,
    ToolSpec,
    get_tool_registry,
)

__all__ = [
    "ToolContext",
    "ToolRegistry",
    "ToolSpec",
    "get_tool_registry",
]
