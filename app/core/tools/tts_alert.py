"""``TtsAlert`` —— 语音提醒工具。

模型给出提醒文本 + 语种 + 说话人 embedding；``__call__`` 在
``WdnaiDetector`` 的 worker 线程里调用 ``TtsSession.start()``
触发本机播放。

能力门控：

* ``WdnaiDetector._infer_sync`` 已经在 worker 内先做了一次
  ``get_registry().get_session("swcap_vision_chat")`` 校验；
* TTS 工具启用时，本 ``__call__`` 仍会再做一次 capability
  检查（``get_session("swcap_text_to_speech")``），未启用则直接抛
  ``RuntimeError("功能未启用: swcap_text_to_speech")``，由 detector
  写入 ``DetectorEvent(level="error")``。

并发：

TTS 服务是单 worker（``TtsSession`` 单例 + 后台线程），单次合成
需要 2-5 秒。如果模型在 detector 的多轮工具循环里连发 tts_alert
（每帧 1 次 + max_iters 默认 5），第二个调用会撞上
``"TTS 正在运行或正在清理，请稍候再试"``。这里**静默跳过**而非
写入 error 事件——避免在 alert 已经播过一次的循环里刷屏
``语音提醒失败: ...``。Detector 仍能拿到第一轮成功播的那一次
事件。
"""
from __future__ import annotations

from typing import Annotated

from wdnai import Callable as WdnaiCallable
from wdnai import Field as WdnaiField
from wdnai import SoftwareCapability


# tts_alert 默认音量。AGENTS.md 早期约定 standalone TTS page 默认
# 0.1（"10% on hardware whose mixers default to 100%"），但 tts_alert
# 是 alert 场景——用户期望听见，不能默默 10%。0.5 在大多数 3.5mm
# 耳机 / 板载喇叭上已经是清晰可闻的音量，且仍给系统 mixer 留余量。
# 用户可调系统的 ``amixer`` 控制或后续在 UI 加 slider。
_ALERT_VOLUME = 0.5


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
    ref_speaker: Annotated[str, "声音名（留空 = 默认参考音色）"] = WdnaiField(
        default="",
        description="声音名：news_female / baby_girl / lovely_girl / cool_lady / "
                   "male_announcer / uncle_bass（留空则用默认参考音色）",
    )

    def __call__(self, frame_bgr, ctx):
        text = (self.text or "").strip()
        if not text:
            return {"text": "", "played": False, "error": "text 为空"}

        # 能力门控：未启用 swcap_text_to_speech → 抛错，由 detector 写入错误事件。
        from app.core.compute.registry import get_registry
        try:
            get_registry().get_session(SoftwareCapability.TEXT_TO_SPEECH)
        except RuntimeError as e:
            raise RuntimeError(str(e)) from e

        from app.core.audio.tts_bridge import (
            PLAY_DEVICE, TTSRequest, default_ref_speaker, get_session,
        )

        sess = get_session()
        # Concurrency guard: TTS 单 worker，busy 时不应该再入队
        # 同一个 alert，而是当作"已经提醒过了"安静返回。``snapshot()``
        # 的 ``busy`` 字段就是 ``_state in (LOADING, RUNNING)`` 的对外
        # 表达 —— TtsSession 没有直接暴露 ``state`` 属性。
        if sess.snapshot().get("busy"):
            return {
                "text": text,
                "played": False,
                "error": "skipped(tts busy)",
                "silent": True,
            }

        req = TTSRequest(
            text=text,
            language=(self.language or "auto"),
            speaker="",
            ref_speaker=(self.ref_speaker or default_ref_speaker()),
            instruct="",
            volume=_ALERT_VOLUME,
            no_play=False,
            play_to=PLAY_DEVICE,
            output="",
            non_streaming_mode=False,
        )
        try:
            sess.start(req)
        except Exception as e:  # noqa: BLE001
            return {"text": text, "played": False, "error": str(e)}

        # tts_alert 是纯 side-effect：模型调用工具即"提醒已播"，
        # 没有需要 wrap-up 的 follow-up 输出。返回 ``consume_iteration=True``
        # 让 detector 跳出 multi-iter 循环——不再把"你刚说了
        # '看到人就提醒'"的 transcript 喂回下一轮，避免模型退化到
        # 把用户提示词当 VLMDoc 的 text 复读出来。
        return {
            "text": text,
            "played": True,
            "error": "",
            "consume_iteration": True,
        }
