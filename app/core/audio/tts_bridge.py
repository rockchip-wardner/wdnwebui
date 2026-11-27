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
import time
from dataclasses import dataclass
from importlib.util import find_spec
from typing import TYPE_CHECKING, Optional


# 播放目标常量(纯字符串,与具体实现无关)
PLAY_DEVICE = "device"    # SDK 流式通过 ALSA 板载 codec 播放(原行为)
PLAY_BROWSER = "browser"  # SDK 静默;服务端 WAV 通过 <audio> 给浏览器播
PLAY_BOTH = "both"        # SDK 流式 + WAV 同时给浏览器
PLAY_TARGETS = (PLAY_DEVICE, PLAY_BROWSER, PLAY_BOTH)

#: **同传队列专用**：只合成、**不在会话里播** —— 由队列自己的播放器线程播。
#:
#: 为什么需要它：``PLAY_BOTH`` 的播放是**边合成边播**（PCM 一到就进
#: aplay），于是「合成」和「播放」被**焊死在一句**里 —— 下一句的合成
#: 必须等这一句的合成结束，而这一句的播放还没完 ⇒ 每句白等一个
#: **首窗延迟**（实测 3.7 s：decoder 首窗要 50 帧 = 4 s 音频，talker
#: 50 步 ≈ 3.7 s）⇒ 队列吞吐只有 **0.67× 实时** ⇒ 实时同传的滞后
#: 必然越落越远（实测 25 s 源音频 ⇒ 滞后 23 s 且持续增长）。
#:
#: ⇒ ``PLAY_QUEUE`` 让会话**只产出 PCM**，播放交给队列的播放器线程 ⇒
#: 下一句的合成可以和这一句的播放**重叠** ⇒ 吞吐回到 ≈1× 实时。
#:
#: ⚠️ **不进 ``PLAY_TARGETS``** —— 那是语音生成卡自己 API 的取值校验，
#: 用户不该能从那个接口选到这个内部模式。
PLAY_QUEUE = "queue"


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
    host mixer 默认 100% 的硬件)。

    ``save_wav`` / ``tempo`` 是**同声传译队列**加的两个字段（2026-11-21）：

    * ``save_wav=False`` —— 同传是「一句接一句」地播，落 WAV 只是往 eMMC
      灌垃圾（同传一次会议能灌几百 MB）⇒ 默认关掉。
    * ``tempo>1.0`` —— 播放变速（sox ``tempo`` 效果，不变调）。**只影响
      播放**，不影响合成；``1.0`` 时走原来的 ``aplay`` 路径，零风险。
    """
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
    save_wav:    bool = True          # False = 不落盘（同传用）
    tempo:       float = 1.0          # 播放变速（1.0 = 不变速）
    #: **首个解码窗口攒多少帧才开声**（0 = SDK 默认 25）。
    #:
    #: ⚠️ 上游实现首窗硬等满 50 帧（= 4 s 音频）⇒ 实测「合成首窗」
    #: ≈ **3.0 s** —— 同传链路上最大的一段固定延迟。decoder 是**因果**的
    #: ⇒ 首窗不需要未来帧 ⇒ 攒 25 帧即可 ⇒ **首声提前 ~1.5 s**
    #: （详见 ``wdnai.backends.tts.FIRST_WINDOW_FRAMES``）。
    first_window_frames: int = 0


class TTSUnavailable(RuntimeError):
    """TTS 能力不可用 —— 插件没装 / 实例没跑 / 队列不存在。

    调用方（ASR 卡的同传引擎）据此**降级为仅字幕**，而不是让整个功能挂掉。
    """


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


def tts_model_id() -> str:
    """TTS 模型的 ``model_id``（没装 / 没发现 ⇒ 空串）。

    给「模型加载进度条」用：进度条是**按 model_id 找实例**的
    （``state.js::_watchModelProgress`` 轮询 ``GET /api/compute``，
    拿 ``instances[]`` 里 ``model_id`` 对得上、``status`` 变成
    ``running`` 的那条判为就绪）⇒ 卡片得先知道**要等的那个 model_id**
    是谁。

    ⚠️ **为什么不在这里建会话**：进度条要等的是**还没起来**的模型 ⇒
    这时候压根没有会话 ⇒ 只能从**模型表**里查。

    ⚠️ **为什么按 capability 查而不是写死 "qwen3-tts"**：模型包换了
    id（版本升级）时，写死的名字就失效 ⇒ 进度条永远等不到 ⇒ 表现为
    「进度条卡住」而不是「模型没装」⇒ 很难查。
    """
    try:
        from app.core.compute.registry import MODEL_PRESETS
    except Exception:  # noqa: BLE001 — 宿主还没就绪 ⇒ 当作「没有 TTS」
        return ""
    for model_id, preset in (MODEL_PRESETS or {}).items():
        caps = (preset or {}).get("capability_types") or []
        if "swcap_text_to_speech" in caps:
            return str(model_id)
    return ""


def start_model(model_id: str) -> dict:
    """把某个模型拉起来（**卡位由卡位策略决定**）⇒ 给「模型加载进度条」用。

    ⚠️ **为什么需要它**：向导的进度条是**按 model_id 等实例出现**的
    （``state.js::_watchModelProgress`` 轮询 ``GET /api/compute``）⇒
    只把 model_id 报上去**不会有人去起它** ⇒ 进度条会一直等到超时。

    ⚠️ **卡位怎么选**：走 :mod:`app.core.compute.card_roles` 的**同一份
    策略** —— 感知链（ASR + 翻译）跟卡跑、**发声链（TTS）独占一张卡**
    ⇒ TTS 会落到一张**没被感知链占用**的卡（需求：ASR+翻译一张卡、
    TTS 独立一张卡）。

    ⚠️ ``allow_adopt=False`` ⇒ **不允许**拿对端板子的实例 ⇒ 本板自己起。

    返回 ``{"ok": bool, "model_id": str, "npu_index": int, "detail": str}``。
    """
    out = {"ok": False, "model_id": str(model_id or ""), "npu_index": -1,
           "detail": ""}
    if not model_id:
        out["detail"] = "没有模型 id"
        return out
    try:
        from app.core.compute import card_roles
        from app.core.compute.registry import get_registry
        reg = get_registry()
        card = card_roles.pick_card(reg, model_id)
        out["npu_index"] = int(card)
        inst = reg.ensure_worker_for_model(
            model_id, allow_adopt=False, npu_index=card, eager_load=False)
        # ⚠️⚠️ **必须 bind** —— ``ensure_worker_for_model`` 只**起 worker
        #    进程**；``eager_load=False`` 时 worker **不会自己加载**模型
        #    （``--npu`` 不传给它）⇒ 状态会一直停在 ``not_loaded`` ⇒
        #    进度条永远等不到。
        #    ⇒ ``bind_instance`` 发那条 ``load`` RPC（fire-and-forget：
        #    立刻把状态置 ``loading``，后台加载完翻 ``running``）。
        #    —— 与 ``POST /api/compute/instances`` 走的是同一条路。
        reg.bind_instance(inst, card)
        out["ok"] = True
        out["pid"] = getattr(inst, "pid", 0)
    except Exception as exc:  # noqa: BLE001 — 起不来 ⇒ 软失败
        out["detail"] = "%s: %s" % (type(exc).__name__, exc)
    return out


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


# --------------------------------------------------------------------------- #
# 同声传译队列（2026-11-21）
# --------------------------------------------------------------------------- #
#
# 同传是**另一个卡片**在调 TTS 卡 ⇒ 卡片之间不能直接 import（AGENTS 铁律：
# 卡片只能 import ``app.core.*``）⇒ 这一层就是那道门。
#
# 队列在插件侧（``cards/tts/queue.py``）：单实例串行消费 + 变速 + 滞后记账。
# 宿主这边只做「候选路径解析 + 插件缺失抛 TTSUnavailable」。

#: TTS **队列**实现所在模块的候选路径 —— 与会话模块同构。
_TTS_QUEUE_MODULES = (
    "wdnapps_audio_chat.cards.tts.queue",
    "app.apps.tts.queue",
)


def _queue_module():
    """拿到队列模块；插件没装 → ``TTSUnavailable``（不是 ImportError）。

    为什么不是 ImportError：调用方（同传引擎）要区分「**没有 TTS**」和
    「**TTS 出 bug**」—— 前者降级为仅字幕，后者要报错。
    """
    for mod_path in _TTS_QUEUE_MODULES:
        try:
            mod = importlib.import_module(mod_path)
        except ImportError:
            continue
        return mod
    raise TTSUnavailable(
        "TTS 队列不可用:候选路径全部 import 失败: "
        + ", ".join(_TTS_QUEUE_MODULES))


def submit_queued(text: str, language: str = "auto", speaker: str = "",
                  ref_speaker: str = "", volume: float = 0.5,
                  tempo: Optional[float] = None,
                  src_end_ts: float = 0.0, src_start_ts: float = 0.0,
                  src_start_sec: float = -1.0, stream_id: str = "",
                  play_to: str = PLAY_BOTH,
                  first_window_frames: int = 0,
                  cache_key: str = "",
                  background: bool = False) -> dict:
    """把一句译文**排队**给 TTS 播（同传的唯一入口）。

    返回 ``{seq, queued, est_audio_sec, depth}``。

    ⚠️ ``tempo=None``（默认）⇒ **继承队列当前的变速**（``queue_set_tempo``
    设的那个值）；给数值 = 覆盖这一句。

    ⚠️ **``speaker``（内置音色 SPK_ID）和 ``ref_speaker``（参考音色 /
    voice-clone）是两个不同的 SDK 参数** —— 本板只有参考音色可用 ⇒ 必须
    走 ``ref_speaker`` ⇒ 混用会让 SDK 跳过音色条件（「not in SPK_ID,
    skipping」）⇒ **每句话音色都不一样**。

    ⚠️ **``cache_key``**（2026-11-26）—— 非空 ⇒ 这句的音频进**预合成
    缓存**（按「媒体时间轴上的位置」索引）⇒ 文件 / 视频源可以「后台一直
    处理」，播放时**按字幕位置直接取缓存播** ⇒ 拖动进度条也能立刻跟上。

    ⚠️ **``background=True``**（2026-11-26）—— **只合成进缓存、不排队
    播放** ⇒ 走**独立的后台队列** ⇒ 不占播放深度、**不合并**、不进播放
    FIFO。
    ⇒ 文件 / 视频源的「后台一直处理」走这条通道 ⇒ 播放队列保持很小 ⇒
    **一句一个作业** + 按位置放行 ⇒ 声音严格跟字幕（用户实测：把整段灌进
    播放队列 ⇒ 队列爆 ⇒ 合并出几百秒的大 blob ⇒ 声音彻底跟不上字幕）。

    Raises:
        TTSUnavailable: 插件没装 / TTS 实例没跑。
        ValueError: 文本为空。
    """
    if not (text or "").strip():
        raise ValueError("text 不能为空")
    q = _queue_module()
    try:
        return q.submit_queued(
            text=text, language=language, speaker=speaker,
            ref_speaker=ref_speaker, volume=volume,
            tempo=tempo, src_end_ts=src_end_ts, src_start_ts=src_start_ts,
            src_start_sec=src_start_sec, stream_id=stream_id,
            play_to=play_to, first_window_frames=first_window_frames,
            cache_key=cache_key, background=background)
    except TTSUnavailable:
        raise
    except RuntimeError as exc:
        raise TTSUnavailable(str(exc)) from exc


def queue_status() -> dict:
    """队列快照：深度 / 积压秒数 / 当前 tempo / 滞后统计。"""
    return _queue_module().snapshot()


# --------------------------------------------------------------------------- #
# 合成一段 PCM（**不落盘、不播放**）—— 配音导出用
# --------------------------------------------------------------------------- #
#
# 「音频文件 / 含音轨的视频」这两个来源不再做同声传译 ⇒ 改成**导出**目标
# 语言的音轨（ASR → 翻译 → TTS ⇒ 拼成一条 WAV）。那条音轨是**自己拼**的
# ⇒ 不能走队列那条「合成完就播」的通道（它一合成完就进播放 FIFO）。
#
# ⇒ 这里开一条**只产出 PCM** 的通道：走 TTS 卡的 ``run_queued``
# （``play_to=queue`` ⇒ 会话自己不播，只把 PCM 交给 sink）。


class _SynthJob:
    """``TtsSession.run_queued`` 需要的 job 载体（只要这几个属性）。"""

    __slots__ = ("text", "language", "speaker", "ref_speaker", "volume",
                 "tempo", "first_window_frames")

    def __init__(self, text: str, language: str, speaker: str,
                 ref_speaker: str, volume: float, first_window_frames: int):
        self.text = text
        self.language = language
        self.speaker = speaker
        self.ref_speaker = ref_speaker
        self.volume = volume
        self.tempo = 1.0
        self.first_window_frames = first_window_frames


def synthesize_pcm(text: str, language: str = "auto", speaker: str = "",
                   ref_speaker: str = "", volume: float = 0.5,
                   first_window_frames: int = 0,
                   busy_wait_sec: float = 90.0) -> tuple:
    """合成一句话 ⇒ ``(pcm_float32_bytes, sample_rate)``。

    ⚠️ **不落盘、不播放** —— 配音导出要拿到 PCM **自己拼**成一条音轨。

    ⚠️ ``speaker``（内置音色 SPK_ID）和 ``ref_speaker``（参考音色 /
    voice-clone）是**两个不同的 SDK 参数** —— 本板只有参考音色可用 ⇒
    混用会让 SDK 跳过音色条件（「not in SPK_ID, skipping」）⇒ **每句话
    音色都不一样**。

    ⚠️ **「TTS 正在运行或正在清理」要重试**（2026-11-26 实测）——
    TTS 卡的 ``run_queued`` 只等「状态不是 RUNNING」⇒ 但 ``start()``
    还会看**上一次那个 worker 线程是不是还活着** ⇒ 上一句刚合成完、
    线程还没退出时，下一句就会撞上这个错。
    ⇒ 这里**重试到它让开**（最多等 ``busy_wait_sec``）。

    Raises:
        TTSUnavailable: 插件没装 / TTS 实例没跑。
        RuntimeError: 合成失败（错误信息在 ``detail`` 里）。
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("text 不能为空")
    try:
        sess = get_session()
    except ImportError as exc:
        raise TTSUnavailable(str(exc)) from exc
    if not getattr(sess, "available", False):
        raise TTSUnavailable("TTS 不可用: {}".format(
            getattr(sess, "_init_error", "未知原因")))

    import numpy as np

    job = _SynthJob(text, language, speaker, ref_speaker, volume,
                    first_window_frames)
    deadline = time.monotonic() + float(busy_wait_sec)
    while True:
        chunks = []
        rates = []

        def _sink(chunk, sr):
            flat = np.asarray(chunk, dtype="float32").reshape(-1)
            if flat.size:
                chunks.append(flat)
                rates.append(int(sr))

        try:
            snap = sess.run_queued(job, sink=_sink)
        except RuntimeError as exc:
            msg = str(exc)
            if ("正在运行" in msg or "正在清理" in msg) and \
                    time.monotonic() < deadline:
                # ⚠️ **上一个 worker 线程还没退出** ⇒ 等它让开再试。
                time.sleep(0.4)
                continue
            raise
        err = str((snap or {}).get("error") or "")
        if err:
            if ("正在运行" in err or "正在清理" in err) and \
                    time.monotonic() < deadline:
                time.sleep(0.4)
                continue
            raise RuntimeError("合成失败: {}".format(err))
        if not chunks:
            raise RuntimeError("合成没有产出音频")
        sr = rates[0] if rates else 24000
        pcm = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
        return pcm.astype("<f4").tobytes(), int(sr)


def queue_clear() -> dict:
    """清空**未开始**的 job（正在合成的那句跑完）。"""
    return _queue_module().clear()


def queue_cache_status() -> dict:
    """**预合成音频缓存**的统计（2026-11-26）。

    ⚠️ **为什么需要暴露出来**：文件 / 视频源要「后台一直处理」⇒ 面板要能
    显示「已经合成好多少秒 / 共多少秒」⇒ 用户才知道**拖到后面也能立刻
    播**。
    """
    q = _queue_module()
    fn = getattr(q, "cache_stats", None)
    if fn is None:
        return {"entries": 0, "bytes": 0, "max_bytes": 0,
                "hits": 0, "misses": 0}
    return fn()


def queue_cache_has(key: str) -> bool:
    """预合成缓存里**有没有**这一句（按位置索引）。

    ⚠️ **用途**：同传引擎在「重新武装」某一句（拖动后重播）时 ⇒ 先问
    缓存有没有 ⇒ 有就**直接提交播**（不用重新合成）⇒ **立刻出声**。
    """
    q = _queue_module()
    fn = getattr(q, "cache_has", None)
    if fn is None:
        return False
    try:
        return bool(fn(key))
    except Exception:  # noqa: BLE001
        return False


def queue_cache_clear() -> dict:
    """清空预合成缓存（换素材 / 换语言时用）。"""
    q = _queue_module()
    fn = getattr(q, "cache_clear", None)
    if fn is None:
        return {"cleared": 0}
    return fn()


def queue_set_prefetch_mode(on: bool) -> dict:
    """开关**预合成模式**（2026-11-26）。

    ⚠️ **开 ⇒ 播放按「播放头位置」挑**（不是 FIFO）⇒ 配合预合成缓存 ⇒
    拖动进度条也能立刻跟上当前的字幕。
    ⚠️ **实时流（麦克风）必须关** ⇒ 老的顺序语义不变。
    """
    q = _queue_module()
    fn = getattr(q, "set_prefetch_mode", None)
    if fn is None:
        return {"prefetch": bool(on)}
    return fn(bool(on))


def queue_flush_position(pos_sec: float, direction: str = "") -> dict:
    """**seek（用户拖了进度条）之后**：冲掉与新位置不匹配的音频。

    ⚠️ **为什么不能直接用** :func:`queue_clear`（实测 2026-11-22，用户报
    「含音轨的视频多次拖动进度条，没跟字幕的翻译同声播出来」）：

    * ``queue_clear`` **不清**正在合成的那句 ⇒ 它的 PCM 之后还会进队列
      ⇒ **陈旧的译文照样播出来**（实测错位 −20~−31 秒）；
    * ``queue_clear`` **不复位播放时钟** ⇒ 后跳之后播放头变小 ⇒ 播放
      时钟的停滞判定永远不成立 ⇒ 8 秒后兜底放行 ⇒ 播的还是旧内容。

    ⇒ 这一层做三件事：丢待播 job、丢 FIFO 里陈旧的 PCM、**复位播放时钟**。
    """
    q = _queue_module()
    fn = getattr(q, "flush_by_position", None)
    if fn is None:
        # 插件版本旧 ⇒ 退回全清（行为差一点，但不至于把陈旧音频留着）。
        return q.clear()
    return fn(pos_sec, direction)


def queue_set_reserve(seconds: float) -> dict:
    """设 TTS 队列的**延迟线留量**（FIFO 里留多少秒不播）。

    ⚠️ **跟播模式必须 > 0**：放行器的提前量 = 生产延迟 + 留量 ⇒
    留量就是管线的**余量** ⇒ 合成抖动不会变成断音；同时播放时刻
    可精确对齐（可听滞后 ≈ 0）。
    """
    q = _queue_module()
    return q.set_reserve(seconds)


def queue_set_clock(provider=None, offset: float = 0.0) -> dict:
    """设 TTS 队列的**播放时钟**（跟播用）。

    ⚠️ ``provider`` = 返回「当前播放头（媒体秒）」的可调用对象；
    ``None`` = 关掉时钟（播放不再等源话）。
    """
    return _queue_module().set_clock(provider, offset)


def queue_set_media_state(provider=None) -> dict:
    """设 TTS 队列的**媒体状态**（跟播用，2026-11-25）。

    ⚠️ ``provider`` = 返回「媒体状态字典」的可调用对象，字段：

    * ``ended`` / ``failed`` —— 源播完 / 媒体坏了 ⇒ 剩下的**补播**；
    * ``playing`` —— 媒体声称在播；
    * ``moved`` —— 播放头**曾经真的走过**（区分「用户暂停」与
      「自动播放被拦」）；
    * ``alive`` —— 前端**还在上报**（心跳在）。

    ⚠️ **为什么必须单独一路**（用户报「视频暂停了同传还在播」「声音不
    跟随当前字幕」）：播放时钟只回答「播放头在哪」，**回答不了「媒体是不是
    暂停了」** ⇒ 暂停时播放头不动 ⇒ 8 秒兜底放行 ⇒ **积压一次性狂喷**
    （实测 184 秒 ⇒ 声音跑到画面前面 174 秒）⇒ 有这一路才分得清
    「暂停」（按住）与「缓冲 / 失联」（放行）。
    """
    return _queue_module().set_media_state(provider)


def queue_set_tempo(tempo: float) -> dict:
    """改**未开始** job 的变速（积压一降就该回落 1.0）。"""
    return _queue_module().set_tempo(tempo)


if TYPE_CHECKING:  # 仅类型提示用,运行时无副作用
    from wdnapps_audio_chat.cards.tts.service import TtsSession  # noqa: F401