"""TTS 跨应用抽象(放 core 而非 apps/tts/: video_detection 的 tts_alert
工具直接 import TTSRequest + get_session(),违反 AGENTS.md「卡片只能
import app.core.*」零耦合铁律,A1 静态扫描发现)。

本模块暴露:
  - TTSRequest dataclass
  - PLAY_DEVICE / PLAY_BROWSER / PLAY_BOTH 常量
  - default_ref_speaker() —— 默认参考音色(从 SDK 内嵌表派生)
  - get_session() —— 懒代理,内部再 import 卡片 service 拿单例,
    避免 core → 卡片 反向依赖在 import 阶段就触发循环

为什么 get_session() 是「候选路径列表」而不是写死一个
----------------------------------------------------
语音生成卡已从 app/apps/tts 拆成插件包 wdnapps_audio_chat(见该包
README)。core **不得**硬依赖某一个具体包路径 —— 那会让「插件没装」
从「一张卡缺席」变成「core 报错」。所以按候选列表逐个试,插件包优先、
树内旧路径兜底(开发期/过渡),都没有就抛 ImportError,由调用方(内置
工具 tts_alert,注册时已 try/except)按「工具不可用」软失败处理。
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from importlib.util import find_spec
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


#: TTS 会话实现所在模块的候选路径 —— 插件包优先,树内旧路径兜底。
_TTS_SESSION_MODULES = (
    "wdnapps_audio_chat.cards.tts.service",
    "app.apps.tts.service",
)


def session_available() -> bool:
    """TTS 会话实现是否可用（语音生成卡装了吗）—— **只探测，不建单例**。

    给内置工具注册用：插件包没装时就不该注册 tts_alert —— 让模型看到
    一个「一调就抛 ImportError」的工具，比看不到这个工具更糟（它会真的
    去调，然后拿到一个错误）。

    探的是**卡片包**（父模块）而不是叶子模块：``find_spec`` 不执行目标
    模块的代码，只 import 它的父包（插件的顶层 ``__init__`` 是空的），
    所以探测既便宜又无副作用。
    """
    for mod_path in _TTS_SESSION_MODULES:
        pkg = mod_path.rsplit(".", 1)[0]
        try:
            if find_spec(pkg) is None:
                continue
        except (ImportError, ValueError):
            continue
        return True
    return False


def get_session():
    """TTS 会话单例(``TtsSession``)。

    懒加载避免 core → 卡片 service → core 循环;boot 时 service
    还未 import,第一次调用 ``get_session`` 时所有模块都已就绪。

    候选路径全试完仍拿不到 → 抛 ImportError(插件没装 / 卡片还没搬完),
    调用方负责降级。
    """
    for mod_path in _TTS_SESSION_MODULES:
        try:
            mod = importlib.import_module(mod_path)
        except ImportError:
            continue
        return mod.get_session()
    raise ImportError(
        "TTS 会话实现不可用:候选路径全部 import 失败: "
        + ", ".join(_TTS_SESSION_MODULES))


if TYPE_CHECKING:  # 仅类型提示用,运行时无副作用
    from wdnapps_audio_chat.cards.tts.service import TtsSession  # noqa: F401