"""音视频描述页可调用的 wdnai 工具集。

当前仅保留一个内置工具：

* ``tts_alert``     — 语音提醒工具：模型给出文本，触发 TtsSession
  在本机播放；依赖 ``swcap_audio_tts`` capability。

工具按 ``@Callable`` 协议暴露给 wdnai，会话模型选中时 SDK 返回一个
已填充字段的实例，detector 再执行 ``instance(frame, ctx)`` 拿到
具体动作的副作用（调 TTS）。SDK 不负责自动 ``__call__``，
这点与 ``examples/function_call.py`` / ``object_detect.py``
保持一致。

视频目标检测页历史上挂过 ``target_detection`` 工具，本版本已下线
—— 见 ``app/services/detector.py`` 的 ``TargetDoc`` 结构化输出
+ ``get_target_detector()`` 单例。目标检测改为直接走
``session.respond(generating=TargetDoc)``，不再经 ``@Callable``
链路，因此工具注册表里不再有 ``target_detection`` 项。
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