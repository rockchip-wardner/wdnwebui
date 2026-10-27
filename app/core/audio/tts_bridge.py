"""TTS 跨应用抽象(放 core 而非 apps/tts/: video_detection 的 tts_alert
工具直接 import TTSRequest + get_session(),违反 AGENTS.md「卡片只能
import app.core.*」零耦合铁律,A1 静态扫描发现)。

本模块暴露:
  - TTSRequest dataclass
  - PLAY_DEVICE / PLAY_BROWSER / PLAY_BOTH 常量
  - default_ref_speaker() —— 默认参考音色(从 SDK 内嵌表派生)
  - get_session() —— 懒代理,内部再 import apps/tts/service 拿单例,
    避免 core → apps 反向依赖在 import 阶段就触发循环
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING


# 播放目标常量(纯字符串,与具体实现无关)
PLAY_DEVICE = "device"    # SDK 流式通过 ALSA 板载 codec 播放(原行为)
PLAY_BROWSER = "browser"  # SDK 静默;服务端 WAV 通过 <audio> 给浏览器播
PLAY_BOTH = "both"        # SDK 流式 + WAV 同时给浏览器
PLAY_TARGETS = (PLAY_DEVICE, PLAY_BROWSER, PLAY_BOTH)


def default_ref_speaker() -> str:
    """默认参考音色 —— SDK 内嵌表的**第一条**。

    为什么不写死名字:参考音色表会被整体替换(增删参考音色)。写死的话,
    删掉那个音色之后请求会带着一个**不存在**的 ref_speaker 去合成 ——
    表现是「音色没生效」,而不是「音色列表里少了一项」,很难查。

    SDK 侧同名常量 ``wdnai.backends.tts.DEFAULT_REF_SPEAKER`` 是同一个值,
    两边都从那张表派生,不会漂移。
    """
    from wdnai.backends.tts import DEFAULT_REF_SPEAKER

    return DEFAULT_REF_SPEAKER


@dataclass
class TTSRequest:
    """TTS 合成请求。``play_to`` 决定音频去向:device/browser/both 三选一。
    ``volume`` 0.0–1.0 线性振幅(原 service.py 注释: web 默认 0.1 适配
    host mixer 默认 100% 的硬件)。"""
    text:        str = ""
    language:    str = "auto"
    speaker:     str = ""             # SPK_ID (serena / ryan / ...)
    ref_speaker: str = ""             # 空 = default_ref_speaker()
    instruct:    str = ""
    volume:      float = 0.1
    no_play:     bool = False
    play_to:     str = PLAY_DEVICE
    output:      str = ""             # 绝对路径;空则自动填充
    non_streaming_mode: bool = False


def get_session():
    """返回 ``app.apps.tts.service.TtsSession`` 单例。

    懒加载避免 core → apps.tts.service → core 循环;boot 时 tts.service
    还未 import,第一次调用 ``get_session`` 时所有模块都已就绪。
    """
    from app.apps.tts.service import get_session as _impl
    return _impl()


if TYPE_CHECKING:  # 仅类型提示用,运行时无副作用
    from app.apps.tts.service import TtsSession  # noqa: F401