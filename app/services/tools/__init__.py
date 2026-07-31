"""音视频检测页可调用的 rkai 工具集。

三个内置工具：

* ``crop_region``   — 原图缩放工具：模型给出 0–1000 归一化 bbox，
  后端按 bbox 切出 BGR 二次送 VLM 推理。
* ``annotate_roi``  — 目标标注工具：模型给出 bbox，前端 canvas
  叠加 5 秒淡出的红色矩形。
* ``tts_alert``     — 语音提醒工具：模型给出文本，触发 TtsSession
  在本机播放；依赖 ``audio_output`` capability。

工具按 ``@Callable`` 协议暴露给 rkai，会话模型选中时 SDK 返回一个
已填充字段的实例，detector 再执行 ``instance(frame, ctx)`` 拿到
具体动作的副作用（裁图 / 广播 bbox / 调 TTS）。SDK 不负责自动
``__call__``，这点与 ``examples/function_call.py`` / ``object_detect.py``
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
