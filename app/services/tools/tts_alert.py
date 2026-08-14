"""``TtsAlert`` —— 语音提醒工具。

模型给出提醒文本 + 语种 + 说话人 embedding；``__call__`` 在
``WdnaiDetector`` 的 worker 线程里调用 ``TtsSession.start()``
触发本机播放。

能力门控：

* ``WdnaiDetector._infer_sync`` 已经在 worker 内先做了一次
  ``get_registry().get_session("swcap_video_describe")`` 校验；
* TTS 工具启用时，本 ``__call__`` 仍会再做一次 capability
  检查（``get_session("swcap_audio_tts")``），未启用则直接抛
  ``RuntimeError("功能未启用: swcap_audio_tts")``，由 detector
  写入 ``DetectorEvent(level="error")``。
"""
from __future__ import annotations

from typing import Annotated

from wdnai import Callable as WdnaiCallable
from wdnai import Field as WdnaiField


@WdnaiCallable
class TtsAlert:
    """语音提醒工具：传入提醒文本，本机将通过 TTS 播放。"""

    text: Annotated[str, "要播放的提醒文本"] = WdnaiField(
        default="",
        description="要播放的提醒文本",
    )
    language: Annotated[str, "语种：auto / chinese / english …"] = WdnaiField(
        default="auto",
        description="语种：auto / chinese / english / japanese / korean",
    )
    ref_speaker: Annotated[str, "声音名：girl_base / ahu"] = WdnaiField(
        default="girl_base",
        description="声音名：girl_base / ahu",
    )

    def __call__(self, frame_bgr, ctx):
        text = (self.text or "").strip()
        if not text:
            return {"text": "", "played": False, "error": "text 为空"}

        # 能力门控：未启用 swcap_audio_tts → 抛错，由 detector 写入错误事件。
        from app.services.compute_registry import get_registry
        try:
            get_registry().get_session("swcap_audio_tts")
        except RuntimeError as e:
            raise RuntimeError(str(e)) from e

        from app.services.tts_session import PLAY_DEVICE, TTSRequest, get_session

        req = TTSRequest(
            text=text,
            language=(self.language or "auto"),
            speaker="",
            ref_speaker=(self.ref_speaker or "girl_base"),
            instruct="",
            volume=0.1,
            no_play=False,
            play_to=PLAY_DEVICE,
            output="",
            non_streaming_mode=False,
        )
        try:
            get_session().start(req)
        except Exception as e:  # noqa: BLE001
            return {"text": text, "played": False, "error": str(e)}

        return {"text": text, "played": True, "error": ""}
