"""Qwen3-ASR 流式语音识别服务。

链路
====

::

    浏览器上传 wav
        → POST /api/qwen3-asr/transcribe（multipart）
        → WAV 解析成 (PCM, 采样率, 声道, 位宽)
        → ComputeRegistry.get_session("swcap_audio_asr")
        → wdndds RPC（compute_worker 子进程里的 LLMLocalSession）
        → wdnai ASRBackend.run_audio(online=True)
        → 每秒一块增量解码，**每轮**通过 sink observer 发出一个 JSON 载荷
        → wdndds 的 RELIABLE delta 旁路 topic 回到主进程
        → 本服务解析后广播给浏览器（WebSocket）

为什么走 delta 旁路而不是 reply
------------------------------

一次流式识别 = N 轮 LLM（1 秒音频一块），reply 通道在推理期间是「聋」的，
只有 **全部轮次跑完**才回一句话 —— 那样就看不到「实时刷新」了。
``on_delta`` 是 SDK 唯一的一条流式通道（``LLMLocalSession`` 把它注册成
sink observer，wdndds 把它转发到 RELIABLE 旁路 topic），所以增量输出
只能走它。

worker 端不需要认识 ASR
----------------------

``wdnai`` 的 ASR backend 每轮发一个 JSON 载荷（``{"type":"asr_round",
"committed":…, "unfixed":…, "language":…, "final":…}``）。worker 只负责
把文本递出去，**不解析**它 —— 解析发生在主进程（本模块）和浏览器里。
这与「worker 只做传输、不理解 schema」的原则一致。

两种显示模式
============

* **实时单行**（对应 C demo 的 ``-s``）：一行里，已确认文本 + 蓝色未确认
  尾巴，整行原地刷新。
* **逐轮明细**（对应 C demo 的默认输出）：每轮一行，显示该轮确认的增量
  与未确认尾巴。

两种模式吃的是**同一份**数据（每轮的 committed / unfixed），只是渲染
方式不同 —— 和 C demo 一样。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.core.audio.capture import (CAPTURE_N_CHANNELS, CAPTURE_SAMPLE_RATE,
                                    CAPTURE_SAMPLE_WIDTH, PcmFeedBuffer)
from app.core.audio.endpoint import (AdaptivePauseDetector, EndpointConfig,
                                    EndpointDetector, rms_dbfs, to_mono_s16)
from app.core.config import asr_live_enabled

_LOG = logging.getLogger("qwen3_asr")

#: 每个 WS 订阅者最多缓存多少帧（溢出丢最旧）。
_SUB_QUEUE_MAX = 500

#: 文件 / 视频模式下，**单个 SDK 流式会话**最长音频（秒）。
#:
#: 超过就切一个新会话（新 ``asr_stream_id``）—— worker 侧的流式会话
#: 状态（``audio_embed_history`` / ``committed_token_ids``）随音频
#: **单调增长**，不切就会把内存吃满。
#:
#: ⚠️ **为什么是 60 s 而不是「一次跑到底」**
#: --------------------------------------
#: C++ 参考实现（``rknn3-model-zoo/examples/Qwen3_ASR/cpp/online_main.cc``）
#: 里有两条硬约束，SDK 的 Python 版逐条对齐了，但**主进程这一层**
#: 原先只对齐了前者：
#:
#: 1. ``ONLINE_MAX_AUDIO_SECONDS = 60`` —— 注释原文「流式模式下，最长推理
#:    60秒(音频过长会导致RTF降低)」。原因是**每轮都会把整段
#:    ``audio_embed_history`` 重新 prefill 一遍**，成本随长度线性增长。
#: 2. ``build_llm_input()`` 里有 ``MAX_CONTEXT_LEN`` 检查，超了直接返回
#:    -1 报错（SDK 侧缺这一条）。
#:
#: 所以「长音频不截断」的正确实现是 **分段会话**，不是 **单个超长会话**：
#: 段内用增量会话，段间切 ``asr_stream_id``。段边界不丢内容 ——
#: 收尾那次调用带 ``asr_stream_final=True``，末块不砍尾（对齐 C demo 的
#: ``is_last``）。
#:
#: 60 s = C++ demo 的 ``ONLINE_MAX_AUDIO_SECONDS``；实测（见
#: :data:`_STALL_ROUNDS_LIMIT` 的说明）模型在 ~26~40 s 后就可能停止
#: 推进，60 s 是留了余量的上限。
_FILE_SESSION_MAX_SEC = 60.0

#: **停滞检测阈值**（连续多少轮「已确认文本没有增长」就判定模型卡住）。
#:
#: 增量会话的推进规则（C++ ``online_main.cc:482-497`` / SDK
#: ``asr.py::_run_online_block``）：
#:
#:     stable_count = len(round_tokens) - ONLINE_ROLLBACK_TOKENS
#:
#: 即 **每轮只回退末尾 5 个 token**，其余全部提交。而模型每轮至少会
#: 重述这 5 个回退 token —— 于是当某一轮「这一秒音频没有新内容」
#: （静音 / 纯音乐 / 人声间隙）时：
#:
#: * ``len(round_tokens) == 5`` → ``stable_count == 0`` → **一个 token
#:   都不提交** → ``committed_token_ids`` 不变；
#: * 下一轮模型看到的「已确认文本」和上一轮**完全相同**，输入不变
#:   → 输出不变 → 又只吐 5 个回退 token → **永久自锁**。
#:
#: 实测（97 分钟真人访谈视频）：t≈26 s 触发自锁，之后 **395 秒零
#: 输出**；直到某一轮模型偶然多吐几个 token 才解冻，然后把积压的
#: 内容一次性倒出来（每轮 +700 字，远超语速），文本与音频彻底脱节，
#: 最后退化成 ``<小红帽>《小红帽》`` 式的无限重复。
#:
#: 判定用「已确认文本连续 N 轮长度不变」—— 这个信号在 SDK 侧等价于
#: ``stable_count == 0``，但**不需要改 SDK**（round 帧里就有）。
#:
#: 取 8 轮（8 秒）：跳过前 4 轮的「冷启动」窗口（前 4 轮本来就不提交，
#: 见 C++ 的 ``remaining_rollback_rounds``），又不至于让正常停顿
#: （几秒的沉默）误触发。
_STALL_ROUNDS_LIMIT = 8

#: **停顿检测阈值**：连续多少轮「模型当前的假设文本**完全没变**」算说话停顿。
#:
#: 为什么需要它（声学检测不够用）：真实视频/录音的底噪往往很高 ——
#: 实测《圆桌派》片段的每秒 RMS 中位数 **-23.6 dBFS**、最低才 -58.7，
#: 低于 -40 dBFS 的秒数 150 秒里只有 2 秒 → **绝对能量门限根本切不出停顿**。
#:
#: 而「模型没有产出新内容」是**零延迟**的说话停顿信号：假设文本
#: （``unfixed``）是模型对**当前这一秒**的猜测，说话停了它就不增长。
#:
#: ⚠️ **``unfixed`` 到底是什么**（SDK ``asr.py:1076`` / ``1339-1396``）
#: ---------------------------------------------------------------
#: 它**不是**累积的假设，而是**当前轮输出的尾巴**：
#:
#:     stable_count = len(round_tokens) - rollback_tokens   # 回退末尾 5 个
#:     new_unfixed  = round_tokens[stable_count:]          # ← 就是这 5 个
#:
#: 也就是说 ``unfixed`` ≈ 「**模型刚说到的前沿那 5 个 token**」。
#: 人在说话 → 每轮都有新 token 涌进前沿 → 尾巴**每轮都变**；
#: 人停下了 → 模型没有新东西可说，只是把同一段文本再吐一遍 → 尾巴
#: **一字不差**。所以「尾巴内容完全相同」= 「模型这一秒什么都没产出」。
#:
#: ⚠️ **必须比「内容」，不能只比「长度」**（v0.32 实测修正）
#: --------------------------------------------------------
#: 最初实现比的是「假设文本的**长度**」没变。实测中文《圆桌派》150 秒里
#: 这样的「等长」出现了 **21 次**，但其中只有 **1 次**内容也相同
#: （t=124，真静音，dB=-58.7）；其余 **20 次都是模型在改写**（内容变了、
#: 长度恰好一样）。按长度判 → 在 41 秒处误切一刀，切口落在句子中间 →
#: 新会话开头就是残句「要缩短在一分钟以内。」
#:
#: 改成比「**内容完全相同**」之后，误报全部消失：只剩那 1 处真静音。
#: 这也符合 ``unfixed`` 的真实语义（见上）—— 说话停了，前沿那 5 个 token
#: 就一字不差地重复。
#:
#: 取 2 轮：即「**连续 2 轮假设文本一字不差**」≈ 说话停了约 1 秒。
#: 1 轮太弱（模型偶尔会重复），2 轮实测与真实停顿吻合。
#:
#: ⚠️ 这个信号**没有**「已确认文本滞后」的问题 —— 它看的是**假设**
#: （实时），不是 confirmed（滞后 4~5 轮）。
#:
#: ⚠️ 反过来也成立：**连续讲话（访谈/播客）里真停顿很少** ——
#: 《圆桌派》150 秒只有 1 处。这类内容下「找不到停顿」的切口只能落在
#: 60 s 硬上限上（见 :data:`_FILE_SESSION_MAX_SEC`），仍会有碎片 ——
#: 要根治得靠「回看（look-back）」，见 AGENTS.md。
_PAUSE_ROUNDS_LIMIT = 2

#: **自适应静音阈值**：某一秒的 RMS 低于「最近 30 秒电平的中位数 - 本值」
#: 才算停顿。
#:
#: 为什么不能用绝对门限：真实视频的底噪差多 —— 实测《圆桌派》片段的
#: 每秒 RMS 中位数 **-23.6 dBFS**，150 秒里低于 -30 dBFS 的只有 2 秒；
#: 而英文讲座的中位数是 -21.1 dBFS。绝对门限要么切不出、要么误切。
#:
#: 相对门限能同时覆盖两种内容：实测真停顿处的电平是
#: -35.3 / -42.1 / -45.5 / -47.3 / -58.7 dBFS（比中位数低 12~35 dB），
#: 而说话中的秒基本都在中位数 ±3 dB 之内 → 12 dB 的余量分得很开。
#:
#: 实现见 :class:`app.core.audio.endpoint.AdaptivePauseDetector`。
#:
#: ⚠️ 停顿的**个数**由内容决定：连续讲话（访谈/播客）里真停顿很少 ——
#: 《圆桌派》150 秒只有 1 处真静音。这种内容下「找不到停顿」的切口
#: 只能落在 60 s 硬上限上（见 :data:`_FILE_SESSION_MAX_SEC`），仍会有碎片 ——
#: 要根治得靠「回看（look-back）」，见 AGENTS.md。
_PAUSE_DROP_DB = 12.0

#: 自适应门限用的**滑动窗口**（秒）。
_PAUSE_LEVEL_WINDOW = 30

#: 自适应门限的**最小历史**（秒）—— 开头电平样本太少，中位数不可信。
_PAUSE_MIN_HISTORY_SEC = 5.0

#: **切会话的最短时长**（秒）—— 不到这个长度不因为「说话停顿」而切。
#:
#: 为什么要有下限：切会话 = 新 SDK 会话 = **前 4 轮不允许提交**
#: （SDK ``asr.py:1067`` 的 ``chunk_idx < rollback_rounds``）→ 新会话
#: 头几秒的文本是模型的**纯猜测**。切得越勤，这种「热身期」占比越高。
#: 40 s 时热身期约占 4/40 = 10%，60 s 时约 6.7%。
#:
#: 为什么不需要更长的上限：:data:`_FILE_SESSION_MAX_SEC` = 60 s 是 **C++
#: demo 的硬约束**（``ONLINE_MAX_AUDIO_SECONDS``，每轮都要把整段历史重新
#: prefill 一遍，成本随长度线性增长）—— **不能突破**。所以本方案是
#: 「**在 40~60 s 这个窗口里挑一个停顿点切**」，而不是「把上限拉长」。
#: 窗口越宽，越容易找到停顿（实测英文样本的停顿间隔 7~12 秒，20 秒的
#: 窗口命中率很高），但窗口越宽 → 会话越多 → 热身期占比越高。
_FILE_SESSION_MIN_SEC = 40.0

#: ASR RPC session 的**进程级互斥**。
#:
#: ``LLMRPCSession`` 只有**一个** delta 桥（``wdndds/ai_session.py`` 的
#: ``self._active``）—— 两次并发的 ``respond()`` 会互相抢走对方的
#: 流式通道：文件路径那一轮的增量会被实时路径吞掉（反之亦然）。
#: 文件路径与实时路径共用同一个 RPC session（同一个 provider），所以
#: 两条路径必须**串行**。
#:
#: 用 ``threading.Lock`` 而不是 ``asyncio.Lock``：文件路径在**另一个
#: 线程**里跑自己的事件循环（``asyncio.run``），跨事件循环的
#: ``asyncio.Lock`` 不可用。实时路径用非阻塞拿锁 + 让出事件循环的方式
#: 等它（见 :meth:`_LiveAsrSession._rpc_call`）。
_ASR_RPC_LOCK = threading.RLock()


class _AsrManager:
    """流式 ASR 服务；单例（:func:`get_asr_service`）。"""

    STATE_IDLE = "idle"
    STATE_RUNNING = "running"
    STATE_DONE = "done"
    STATE_ERROR = "error"
    STATE_STOPPED = "stopped"

    #: 本板 ASR 模型的 model_id。``get_session(capability)`` 在装了
    #: Omni 的机器上会按「本地优先 + 卡号升序」挑一个 provider，能力名
    #: 已经把两者分开了（``swcap_audio_asr`` vs ``swcap_audio_chat``），
    #: 所以这里直接按能力取，不用按 model_id。
    CAPABILITY = "swcap_audio_asr"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: str = self.STATE_IDLE
        self._error: str = ""
        self._current_request_id: Optional[int] = None
        self._last_result: Optional[Dict[str, Any]] = None
        self._language: str = ""
        self._rounds: List[Dict[str, Any]] = []
        #: **这批轮次归属哪个来源**（页面刷新后要靠它把历史恢复回来）。
        #:
        #: ``{"kind": "video"|"audio", "name": str, "path": str}`` ——
        #: * ``kind`` —— 媒体是**含音轨的视频**还是**纯音频**。前端按它决定
        #:   把轮次算作「视频来源」还是「音频文件来源」。
        #: * ``path`` —— 设备端路径（浏览器上传的没有路径 → 空串）。
        #:
        #: ⚠️ **归属必须由后端记，不能由前端猜**：页面刷新后前端不知道这批
        #: 轮次是哪一路来的，靠当前页签猜就会把「音频文件」的轮次画到视频
        #: 画面上（U15 那个「切来源串台」的坑）。后端是唯一知道真相的地方。
        self._rounds_source: Dict[str, Any] = {}
        self._last_started: float = 0.0
        self._id_seq = 0
        #: 本次识别用的流式参数（透传给 SDK 的 respond）。
        self._options: Dict[str, Any] = {}
        #: 用户点了「停止」→ 流式路径在下一块音频后收工。
        self._cancel = False
        #: **音轨在容器里的起点**（秒）。容器里音轨可能晚于画面起点，
        #: 前端要靠它把「播放头时间」换算成「音频时间」才能对齐字幕。
        #: 文件路径在**第一个块**解出来时就能知道（WAV 恒为 0）。
        self._audio_origin_sec: float = 0.0
        #: **停滞检测**：连续多少轮「已确认文本没有增长」。
        #: 见 :data:`_STALL_ROUNDS_LIMIT` 的说明 —— 模型一旦自锁
        #: （``stable_count == 0``），已确认文本就不再增长。
        self._stall_rounds: int = 0
        #: 上一轮已确认文本的长度（停滞检测的比对基准）。
        self._last_committed_len: int = -1
        #: **停顿检测**：上一轮的**假设文本**（比对基准）。
        #: 见 :data:`_PAUSE_ROUNDS_LIMIT` —— 必须比「内容」而不是「长度」。
        self._last_unfixed: str = ""
        #: 连续多少轮「假设文本**内容**没有变化」→ 说话停顿。
        self._pause_rounds: int = 0
        #: 本轮是否命中停顿（文件主循环消费后即失效）。
        self._pause_hit: bool = False

        self._session = None
        self._model_id: Optional[str] = None

        self._subs_lock = threading.Lock()
        self._subs: List[asyncio.Queue] = []

    # -------------------------------------------------------------- WS
    def register_subscriber(self, q: asyncio.Queue) -> None:
        with self._subs_lock:
            self._subs.append(q)

    def unregister_subscriber(self, q: asyncio.Queue) -> None:
        with self._subs_lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    def _broadcast(self, payload: dict) -> None:
        """跨线程 push；每个 subscriber 一个 asyncio.Queue，溢出丢最旧。"""
        with self._subs_lock:
            queues = list(self._subs)
        for q in queues:
            try:
                if q.full():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                q.put_nowait(payload)
            except Exception as e:  # noqa: BLE001
                _LOG.warning("broadcast failed: %s", e)

    def broadcast(self, payload: dict) -> None:
        """广播一帧给所有 WS 订阅者。

        实时路径（:class:`_LiveAsrSession`）复用**同一个**订阅池 ——
        浏览器只有一条 WS，文件路径与实时路径的帧走同一条线。
        调用方必须**已经回到事件循环线程**再调（实时路径的 delta 回调
        在 cshim 线程上，它会先 ``call_soon_threadsafe`` 切回来）。
        """
        self._broadcast(payload)

    # -------------------------------------------------------------- 状态
    def _resolve_model_id(self) -> str:
        """当前能提供 ASR 能力的模型 id（没有就空串）。

        ⚠️ **不能**只在跑过一次识别之后才有值。

        向导的「等能力就绪」判据就是 ``snapshot.model_id``
        （``static/app.js`` 的 ``isReady: (s) => !!(s && s.model_id)``）。
        若 ``model_id`` 只在 ``_select_session()`` 里被填（而那要等
        **一次识别跑起来**），那么**刚重启服务**时这个判据永远为假
        → 向导的「完成配置」等满 20 s 就超时 → **走完向导却什么都没跑**。

        所以这里从 registry 里查「当前谁在提供这个能力」——语义也更对：
        前端那个「模型未启用」横幅要表达的是「能力不可用」，而不是
        「还没识别过」。

        用 :func:`peek_registry` 而不是 :func:`get_registry`：快照每 5 秒
        轮询一次，不能因为轮询就把整个 registry 拉起来。
        """
        if self._model_id:
            return self._model_id
        try:
            from app.core.compute.registry import peek_registry
            reg = peek_registry()
            if reg is None:
                return ""
            handles = reg.list_sessions(self.CAPABILITY, include_remote=True)
        except Exception:  # noqa: BLE001 — 查不到就当没启用
            return ""
        return (handles[0].model_id or "") if handles else ""

    def snapshot(self) -> dict:
        with self._lock:
            snap = {
                "state": self._state,
                "busy": self._state == self.STATE_RUNNING,
                "error": self._error,
                "language": self._language,
                "model_id": self._model_id or self._resolve_model_id(),
                "current_request_id": self._current_request_id,
                "round_count": len(self._rounds),
                "rounds_source": dict(self._rounds_source),
                "last_result": self._last_result,
                "last_started": self._last_started,
                "options": dict(self._options),
                "audio_origin_sec": self._audio_origin_sec,
            }
        # 实时会话共用同一条 WS / 同一个能力 —— 快照要能看到它。
        live = get_live_asr_service()
        active = live.active()
        snap["live_enabled"] = asr_live_enabled()
        snap["live"] = active.snapshot() if active is not None else {
            "active": False,
        }
        if active is not None and not snap["busy"]:
            # 实时会话在跑 → 对外呈现「运行中」，DemoGuard 才会拦离开。
            snap["state"] = self.STATE_RUNNING
            snap["busy"] = True
        return snap

    def rounds(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._rounds)

    def rounds_source(self) -> Dict[str, Any]:
        """这批轮次归属的来源（见 :attr:`_rounds_source`）。"""
        with self._lock:
            return dict(self._rounds_source)

    def _set_state(self, state: str, *, error: str = "") -> None:
        with self._lock:
            self._state = state
            self._error = error

    # -------------------------------------------------------------- REST
    def transcribe(
        self,
        pcm: bytes,
        *,
        sample_rate: int,
        n_channels: int,
        sample_width: int,
        options: Optional[Dict[str, Any]] = None,
    ) -> int:
        """发起一次流式识别；返回 request_id。上一轮还在跑抛 ValueError。

        ``options`` —— 流式参数（``online_chunk_seconds`` /
        ``online_max_new_tokens`` / ``online_rollback_tokens`` /
        ``online_rollback_rounds``），原样透传给 SDK 的 ``respond()``。
        这些是 **C demo 的同名常量**（ONLINE_CHUNK_MS 等），透传后
        「与 C demo 一致」这条性质对任意参数组合都成立。
        """
        if not pcm:
            raise ValueError("音频内容为空")
        with self._lock:
            if self._state == self.STATE_RUNNING:
                raise ValueError("上一条识别还在进行中")
            self._id_seq += 1
            rid = self._id_seq
            self._current_request_id = rid
            self._last_started = time.time()
            self._rounds = []
            self._language = ""
            self._last_result = None
            self._options = dict(options or {})
            self._set_state(self.STATE_RUNNING)

        self._broadcast({"type": "state", "state": self.snapshot()})
        threading.Thread(
            target=self._run,
            args=(rid, pcm, sample_rate, n_channels, sample_width),
            name="qwen3-asr-run",
            daemon=True,
        ).start()
        return rid

    def stop(self) -> None:
        """停止正在跑的识别（当前轮丢弃）。"""
        with self._lock:
            if self._state != self.STATE_RUNNING:
                return
            self._cancel = True
            session = self._session
        if session is not None:
            try:
                session.stop()
            except Exception as e:  # noqa: BLE001 — cancel is best-effort
                _LOG.debug("session.stop() failed (best-effort): %s", e)

    def reset(self) -> None:
        """清空本轮结果，回到 idle。"""
        with self._lock:
            self._rounds = []
            self._rounds_source = {}
            self._language = ""
            self._last_result = None
            self._current_request_id = None
            self._set_state(self.STATE_IDLE)
        self._broadcast({"type": "state", "state": self.snapshot()})

    # -------------------------------------------------------------- 推理
    def _select_session(self):
        """拿 ASR 的 RPC session；没启用就抛 RuntimeError（HTTP 502）。"""
        from app.core.compute.registry import get_registry

        registry = get_registry()
        handles = registry.list_sessions(self.CAPABILITY, include_remote=True)
        if not handles:
            raise RuntimeError("功能未启用: {}".format(self.CAPABILITY))
        chosen = handles[0]
        with self._lock:
            self._session = chosen.session
            self._model_id = chosen.model_id
        return chosen.model_id, chosen.session

    # -------------------------------------------------------------- 流式识别
    def transcribe_stream(
        self,
        chunk_factory,
        *,
        options: Optional[Dict[str, Any]] = None,
        append: bool = False,
        start_sec: float = 0.0,
        source_info: Optional[Dict[str, Any]] = None,
    ) -> int:
        """发起一次**流式**识别（文件 / 视频）。返回 request_id。

        与 :meth:`transcribe`（一次性喂整段）的区别：

        ==================  ==========================  ==========================
                            ``transcribe``              ``transcribe_stream``
        ==================  ==========================  ==========================
        内存                 整段 PCM 进内存              **边解边喂**，不落全量
        时长                 截断到 60 s（见下）          **不截断**
        解码                 调用前一次性完成            worker 线程里边解边喂
        ==================  ==========================  ==========================

        ⚠️ **为什么文件路径不再截断到 60 s**：60 s 是 C demo 的
        ``ONLINE_MAX_AUDIO_SECONDS`` —— 那条路径把**整段**音频反复重喂
        给 LLM，音频越长 RTF 越差（C demo 用截断兜底）。这里走的是
        **增量会话**（每 1 秒一块，历史单调增长），长录音的代价是线性
        的，不该再砍尾巴。

        ``chunk_factory`` —— 无参可调用对象，返回
        ``Iterator[MediaPcmChunk]``。⚠️ 它做的是**阻塞 I/O**（PyAV 解码 /
        读文件）→ 由 worker 线程调用，不占事件循环。

        ``start_sec`` —— 从**容器的第几秒**开始识别（秒）。这是「拖动
        进度条 → 字幕跟过去」的后端支点：不用每次都从头解一遍 98 分钟
        的音轨。轮次的时间戳仍是**绝对**音频时间（音轨相对起点），
        前端的时间轴不用改。

        ``append`` —— **保留**已有的轮次（而不是清空）。拖动后重开识别
        时用它：前面已经识别过的部分要留在字幕时间轴上。

        ``source_info`` —— 这批轮次归属的来源（见 :attr:`_rounds_source`）：
        ``{"kind": "video"|"audio", "name": ..., "path": ...}``。
        * **不是 append**（新的一批）→ 记下来（覆盖旧的）；
        * **append**（拖动后重开，还是同一段媒体）→ **保持原来的** ——
          拖动重开的是同一个视频，来源当然不变。
        """
        with self._lock:
            if self._state == self.STATE_RUNNING:
                raise ValueError("上一条识别还在进行中")
            self._id_seq += 1
            rid = self._id_seq
            self._current_request_id = rid
            self._last_started = time.time()
            if not append:
                self._rounds = []
                self._rounds_source = dict(source_info or {})
            elif source_info and not self._rounds_source:
                # append 但还没有归属（例如服务重启后第一次就是拖动重开）
                self._rounds_source = dict(source_info)
            self._language = ""
            self._last_result = None
            self._options = dict(options or {})
            self._cancel = False
            self._audio_origin_sec = 0.0
            self._set_state(self.STATE_RUNNING)

        self._broadcast({"type": "state", "state": self.snapshot()})
        threading.Thread(
            target=self._run_stream,
            args=(rid, chunk_factory, start_sec),
            name="qwen3-asr-stream",
            daemon=True,
        ).start()
        return rid

    def _run_stream(self, rid: int, chunk_factory,
                    start_sec: float = 0.0) -> None:
        """worker 线程：流式解码 → 增量会话 → 逐轮 delta → 收尾。"""
        # ⚠️ **停滞计数器必须在新请求开始时清零**。
        #
        # 它平时只在 :meth:`_stream_main` 的 ``_rotate_epoch()`` 里重置 ——
        # 如果上一条请求是**以 stall 收尾**的（那次轮换把计数清零了，
        # 但紧接着的最后一轮又把它加回去了），计数就会**跨请求泄漏**。
        # 于是新请求的**第一个块**就被判成「卡住」→ 立刻切会话 →
        # 第一段只有 1 秒 → 开头就是一句没头没尾的碎片。
        #
        # 实测（英文 CS229 讲座 132 s）：连着跑两条时，第二条的第一段
        # 只有 1 秒（``epoch rotated: reason=stall audio_sec=1.0``），
        # 它的文本 "The reason why we say." 后面接着另一段从
        # "Language models…" 开始 —— 同一句话被劈成两半。
        with self._lock:
            self._stall_rounds = 0
            self._last_committed_len = -1
            self._pause_rounds = 0
            self._last_unfixed = ""
            self._pause_hit = False
        try:
            model_id, session = self._select_session()
        except RuntimeError as e:
            self._fail(rid, str(e))
            return
        except Exception as e:  # noqa: BLE001
            self._fail(rid, "取 ASR session 失败: {}".format(e))
            return

        t0 = time.perf_counter()
        try:
            # 与实时路径共用同一个 RPC session → 必须串行（见
            # ``_ASR_RPC_LOCK`` 的说明）。
            with _ASR_RPC_LOCK:
                result = asyncio.run(
                    self._stream_main(rid, session, chunk_factory,
                                      start_sec))
        except Exception as e:  # noqa: BLE001
            self._fail(rid, "识别失败: {}".format(e))
            return
        elapsed = time.perf_counter() - t0

        language = result.get("language") or ""
        body = result.get("text") or ""
        with self._lock:
            self._language = language or self._language
            self._last_result = {
                "request_id": rid,
                "model_id": model_id,
                "language": language or self._language,
                "text": body,
                "raw": body,
                "elapsed_sec": elapsed,
                "audio_sec": result.get("audio_sec", 0.0),
                "segments": result.get("segments", 1),
                "epochs": result.get("epochs", 1),
                "epoch_errors": result.get("epoch_errors", 0),
            }
            self._set_state(self.STATE_DONE)
        self._broadcast({
            "type": "done",
            "request_id": rid,
            "language": language or self._language,
            "text": body,
            "elapsed_sec": elapsed,
            "audio_sec": result.get("audio_sec", 0.0),
        })
        self._broadcast({"type": "state", "state": self.snapshot()})

    async def _stream_main(self, rid: int, session, chunk_factory,
                           start_sec: float = 0.0) -> dict:
        """增量会话的主体（在一个事件循环里跑完整个文件）。

        ⚠️ **源格式透传，不在主进程侧重采样**
        ----------------------------------
        SDK 的增量会话**自己负责重采样与切块**（``asr.py`` 的
        ``_run_online_stream``，注释原文：「The incremental path owns the
        resampling」）：

        * 它的 ``_StreamResampler`` **按会话保持状态** → 跨块相位对齐；
        * 它在 **16 kHz 网格**上切块 → 与「整段一次性」的切块边界
          **完全一致**（这正是 SDK 承诺的「两条路径文本一致」的地基）；
        * 它在 ``asr_stream_final=True`` 时 flush 自己的重采样尾巴。

        所以主进程侧**只负责按帧对齐地切块 + 透传**。主进程再重采样
        一遍会带来两个真实问题：

        1. **24-bit WAV 直接崩** —— 主进程的 ``StreamResampler`` 只认
           s16le（``np.frombuffer(..., dtype="<i2")``），而 C demo 的
           ``asr_en.wav`` 是 **s24le**（PCM 长度 2167377 = 奇数）→
           ``ValueError: buffer size must be a multiple of element size``。
           SDK 侧 ``_pcm_to_mono_float32`` 本来就支持 2/3/4 字节。
        2. **多一次量化** —— 主进程把 float 夹回 s16 再交给 SDK，多一次
           ±1 LSB 的误差机会。

        分块喂与整段喂的**逐字节等价性**靠两条不变式保证：

        * 块边界按**帧**对齐（``PcmFeedBuffer`` 的 ``frame_bytes``）→
          SDK 的逐采样转换（s24le / s16le → f32）分块做与整段做结果相同；
        * ``PcmFeedBuffer`` **留住最后 1 个采样** → 收尾那次调用一定带
          得下音频 → SDK 能把末块标成 ``is_last``（末块不回滚，
          与 C demo 一致）。
        """
        from wdnai import AudioAttachment

        segments: List[str] = []
        languages: List[str] = []
        audio_sec = 0.0
        epoch = 0
        stream_id = "file-{}#0".format(rid)
        epoch_sec = 0.0
        epoch_origin = 0.0
        #: 本次识别从**容器的第几秒**开始（拖动进度条 → 识别跟过去）。
        #: 0 = 从头开始。它只在**第一个块**到达时用来把时间轴的起点
        #: 对到绝对位置（见下面的 ``media_start_sec``）。
        container_start_sec = max(0.0, float(start_sec or 0.0))
        # 源格式（采样率 / 声道 / 位宽）—— 第一个块到达时确定；同一个
        # 文件里所有块的格式一致。
        src_rate = CAPTURE_SAMPLE_RATE
        src_channels = CAPTURE_N_CHANNELS
        src_width = CAPTURE_SAMPLE_WIDTH
        feed: Optional[PcmFeedBuffer] = None
        # 自适应停顿检测 —— 用来把切会话的切口对齐到**说话停顿**。
        #
        # ⚠️ 这个检测器看的是**音频信号**（不是 SDK 会话）→ 电平参照要
        # **跨会话连续**，所以换会话时**不** reset（与 EndpointDetector 相反）。
        detector = AdaptivePauseDetector(
            drop_db=_PAUSE_DROP_DB,
            window_sec=_PAUSE_LEVEL_WINDOW,
            min_history_sec=_PAUSE_MIN_HISTORY_SEC,
        )

        async def _submit(pcm: bytes, *, final: bool) -> str:
            """把一块**源格式** PCM 喂进增量会话。"""
            attachment = AudioAttachment(pcm, mime_type="audio/pcm")
            options = dict(self._options)
            options["asr_stream_id"] = stream_id
            options["asr_stream_final"] = bool(final)
            options["asr_stream_origin_sec"] = epoch_origin
            return await session.respond(
                attachment=[attachment],
                sample_rate=src_rate,
                n_channels=src_channels,
                sample_width=src_width,
                on_delta=self._on_delta,
                **options,
            )

        def _absorb(raw: str) -> None:
            """把一次 final 调用的返回文本并进结果。"""
            lang, body = _split_language(raw if isinstance(raw, str)
                                         else str(raw))
            lang = _valid_language(lang)
            body = (body or "").strip()
            if body:
                segments.append(body)
            if lang:
                languages.append(lang)

        def _new_feed() -> PcmFeedBuffer:
            return PcmFeedBuffer(
                sample_rate=src_rate,
                n_channels=src_channels,
                sample_width=src_width,
                block_seconds=1.0,
            )

        # ---- 段的收尾容错 ---------------------------------------------- #
        # 一段（epoch）收尾时的那次 ``asr_stream_final=True`` 调用**可能**
        # 失败（worker 抛异常 / RPC 断了）。对 97 分钟的识别来说，一段收尾
        # 失败就整场报废，代价太大（17 分钟算力 + 前面所有文本一起丢）。
        #
        # 所以：**单段收尾失败 → 记日志 + 丢掉这一段的尾巴（≤ 1 s）+ 继续
        # 下一段**。但下面两种情况仍然直接报错：
        #
        # * **连续 2 段都收不动** → worker 本身坏了（模型没起来 / RPC 断
        #   了 / NPU 掉了）→ 继续跑只是白烧算力；
        # * **整场一段文本都没产出** → 交一份空结果比报错更没用。
        epoch_count = 0
        epoch_errors = 0
        consecutive_errors = 0
        last_error: Optional[BaseException] = None

        async def _close_epoch(reason: str) -> bool:
            """收掉当前会话（带尾块）。返回 False = 收尾失败（已记日志）。

            失败时**不抛**（除非判定 worker 整体坏了）—— 调用方继续下一段。
            """
            nonlocal epoch_count, epoch_errors, consecutive_errors, last_error
            epoch_count += 1
            tail = feed.flush()
            try:
                _absorb(await _submit(tail, final=True))
            except Exception as e:  # noqa: BLE001 — 单段失败不毁全场
                epoch_errors += 1
                consecutive_errors += 1
                last_error = e
                _LOG.warning(
                    "asr stream epoch %s 收尾失败（%s）：本段尾部丢弃，"
                    "继续下一段: %s", epoch, reason, e)
                if consecutive_errors >= 2:
                    raise
                return False
            consecutive_errors = 0
            return True

        async def _rotate_epoch(reason: str) -> None:
            """收掉当前会话，开一个新的。

            **段边界不丢内容**：收尾那次调用带 ``asr_stream_final=True``，
            SDK 把末块标成 ``is_last``（不砍尾）→ 这一段的最后一个
            token 也会被确认 —— 对齐 C demo 的 ``is_last`` 语义。

            调用点有两种：

            * ``length`` —— 段太长（> :data:`_FILE_SESSION_MAX_SEC`）。
              worker 侧的流式状态（``audio_embed_history`` /
              ``committed_token_ids``）随音频**单调增长**，不切就会吃满
              内存，而且每轮都要重新 prefill 整段历史（RTF 随长度劣化）。
            * ``stall`` —— **模型输出卡住**（已确认文本连续
              :data:`_STALL_ROUNDS_LIMIT` 轮没有增长）。这是必须兜底
              的：SDK 的回退策略（``stable_count = len - rollback_tokens``）
              在「这一秒没有新内容」时会让 ``stable_count == 0`` →
              committed 不前进 → 模型输入不变 → 输出不变 → **永久
              自锁**。切一个新会话（音频历史清零）能立刻解开。
            """
            nonlocal stream_id, epoch, epoch_sec, epoch_origin, feed
            await _close_epoch(reason)
            epoch += 1
            stream_id = "file-{}#{}".format(rid, epoch)
            epoch_origin = audio_sec
            epoch_sec = 0.0
            feed = _new_feed()
            with self._lock:
                self._stall_rounds = 0
                self._last_committed_len = -1
                # 换会话 → decoder 重置 → 假设文本从头开始 → 停顿计数归零
                self._pause_rounds = 0
                self._last_unfixed = ""
                self._pause_hit = False
            _LOG.info(
                "asr stream epoch rotated: rid=%s epoch=%s reason=%s "
                "audio_sec=%.1f", rid, epoch, reason, audio_sec)

        for chunk in chunk_factory():
            if self._cancel:
                break
            if feed is None:
                src_rate = int(chunk.sample_rate or CAPTURE_SAMPLE_RATE)
                src_channels = int(chunk.n_channels or CAPTURE_N_CHANNELS)
                src_width = int(chunk.sample_width or CAPTURE_SAMPLE_WIDTH)
                feed = _new_feed()
                # 音轨起点（容器里音轨可能晚于画面起点）→ 告诉前端，
                # 它要靠这个把「播放头时间」换算成「音频时间」才能对齐字幕。
                with self._lock:
                    self._audio_origin_sec = float(
                        getattr(chunk, "origin_sec", 0.0) or 0.0)
                # ⚠️ **从中间位置开始识别**（拖动进度条 → 识别跟过去）时，
                # 时间轴的起点不是 0：``audio_sec`` / ``epoch_origin`` 要
                # 对到**绝对音频时间**（音轨相对起点），否则字幕会整体错位。
                #
                # ``media_start_sec`` = 解码出来的**第一块**在容器时间轴上
                # 的位置（seek 只能落到最近的包边界 → 不一定正好等于
                # 请求的 ``start_sec``，实测偏差 ±20 ms）。
                media_start = float(
                    getattr(chunk, "media_start_sec", 0.0) or 0.0)
                audio_sec = media_start - self._audio_origin_sec
                epoch_origin = audio_sec
                if container_start_sec > 0:
                    _LOG.info(
                        "asr stream start offset: rid=%s container=%.1fs "
                        "audio=%.1fs", rid, container_start_sec, audio_sec)
            frame_bytes = max(1, src_channels * src_width)
            dur = len(chunk.pcm) / float((src_rate or 1) * frame_bytes or 1)
            audio_sec += dur
            # 这一块里有没有「说话停顿」—— 只在**这一块**成立时才算数
            # （否则标记会一直挂着，等到后面某个跟停顿无关的位置才切）。
            chunk_paused = False
            for blk in feed.push(chunk.pcm):
                await _submit(blk, final=False)
                # **自适应停顿检测**（**只观察**）：这一块是不是「说话停顿」。
                #
                # 为什么要它：切会话的切口如果落在句子中间，新会话就是
                # **零上下文 + 半个句子** → 模型只能转写出语法不通的碎片
                # （实测英文 "In the past, so of the context."、中文
                # "甚至可以剪接，把我五分钟、…"），而且碎片会被确认下来
                # 进入最终文本。切在**停顿**处就没有这个问题。
                #
                # 判据是**相对**的：这一块的电平低于「最近 30 秒电平的中位数
                # - 12 dB」→ 算停顿。绝对门限（-40 dBFS）在有背景音乐的
                # 访谈里完全失效（实测《圆桌派》150 秒里只有 2 秒低于它）。
                #
                # ⚠️ 检测器假定输入是 s16le，而文件来源的位深不固定
                # （媒体 → s16le；wav 可能是 s24le）→ 先归一化。
                blk_dur = len(blk) / float((src_rate or 1) * frame_bytes or 1)
                if blk_dur <= 0.0:
                    continue
                mono = to_mono_s16(blk, sample_width=src_width,
                                   n_channels=src_channels)
                if detector.feed(rms_dbfs(mono), blk_dur):
                    chunk_paused = True
            # **模型侧停顿**（零延迟）：这一轮模型的假设文本**内容完全没变**
            # → 模型没产出新内容 → 说话停了。
            #
            # 声学检测在「背景音乐 / 底噪很高」的内容上会失效（实测
            # 《圆桌派》150 秒里只有 1 处真静音），模型侧则不受底噪影响 ——
            # 见 _PAUSE_ROUNDS_LIMIT。
            with self._lock:
                if self._pause_hit:
                    chunk_paused = True
            epoch_sec += dur
            # 切会话的三个理由（按优先级）：
            #   silence —— 说话停顿（切在停顿处 → 新会话从句子开头开始）
            #   length  —— 段太长（C++ demo 的 60 s 硬上限，兜底）
            #   stall   —— 模型输出卡住（自锁兜底，见 _STALL_ROUNDS_LIMIT）
            reason = None
            if chunk_paused and epoch_sec >= _FILE_SESSION_MIN_SEC:
                reason = "silence"
            elif epoch_sec >= _FILE_SESSION_MAX_SEC:
                reason = "length"
            else:
                with self._lock:
                    stalled = self._stall_rounds
                if stalled >= _STALL_ROUNDS_LIMIT:
                    reason = "stall"
            if reason:
                await _rotate_epoch(reason)
                # ⚠️ **不** reset 电平检测器：它看的是音频信号，电平参照要
                # 跨会话连续（否则每个新会话的头几秒没有参照 → 中位数乱跳）。

        if feed is None:
            # 一个块都没解出来（空文件 / 解码失败）→ 仍然收一次尾，
            # 让 SDK 把会话正常关掉。
            feed = _new_feed()
        # 收尾：末块（``PcmFeedBuffer`` 留住了最后 1 个采样）必须带上 ——
        # 否则最后几个 token 不会被确认（末块要标 is_last）。
        await _close_epoch("final")
        if epoch_errors and not segments:
            # 一段文本都没产出 → 报错比交一份空结果有用（见 ``_close_epoch``）。
            if last_error is not None:
                raise last_error
            raise RuntimeError("识别失败: 没有任何一段成功")
        return {
            "text": " ".join(s for s in segments if s),
            "language": languages[-1] if languages else "",
            "audio_sec": audio_sec,
            "segments": len(segments),
            # 真实分段数（含空段）。``segments`` 只数「有正文」的段，
            # 纯静音段的收尾文本是空的 → 会被漏掉；回归脚本要判断
            # 「长音频确实被拆成了多个会话」得看这个。
            "epochs": epoch_count,
            "epoch_errors": epoch_errors,
        }

    def _on_delta(self, raw: str) -> None:
        """worker 发来的一轮载荷 —— 解析后广播给浏览器。

        解析只发生在这里（主进程侧）；worker 端不认识这个 schema。
        """
        try:
            payload = json.loads(raw)
        except Exception:  # noqa: BLE001 — 非 JSON 的 delta 一律忽略
            _LOG.debug("ignoring non-JSON delta: %r", raw[:120])
            return
        if not isinstance(payload, dict):
            return
        kind = payload.get("type")
        if kind == "asr_final":
            # 收尾载荷：完整结果。真正的「done」由 RPC reply 带回，
            # 这里只记下语种。
            lang = _valid_language(payload.get("language") or "")
            if lang:
                with self._lock:
                    self._language = lang
            return
        if kind != "asr_round":
            return
        frame = {
            "round": int(payload.get("round") or 0),
            "committed": payload.get("committed") or "",
            "unfixed": payload.get("unfixed") or "",
            "language": payload.get("language") or "",
        }
        # 时间戳是**加法式**扩展 —— 老消费方忽略这两个键即可。
        start = payload.get("chunk_start_sec")
        if start is not None:
            try:
                frame["chunk_start_sec"] = float(start)
                frame["chunk_end_sec"] = (
                    float(start)
                    + float(payload.get("chunk_samples") or 0)
                    / float(CAPTURE_SAMPLE_RATE or 1))
            except (TypeError, ValueError):
                pass
        with self._lock:
            self._rounds.append(frame)
            lang = _valid_language(frame["language"])
            if lang:
                self._language = lang
            origin = self._audio_origin_sec
            # **停滞检测**：已确认文本（epoch 内累积）连续 N 轮长度不变
            # → 模型输出卡住了（SDK 侧等价于 ``stable_count == 0``）。
            # 见 :data:`_STALL_ROUNDS_LIMIT`。
            #
            # 注意 ``committed`` 是**当前会话内**的累积文本 —— 切 epoch
            # 后 decoder 重置，长度会变小 → 计数器自动归零。
            clen = len(frame["committed"])
            if clen == self._last_committed_len:
                self._stall_rounds += 1
            else:
                self._stall_rounds = 0
            self._last_committed_len = clen
            # **停顿检测**（模型侧，零延迟）：假设文本**内容**连续不变 →
            # 模型这一秒没产出新内容 → 说话停了。见 _PAUSE_ROUNDS_LIMIT。
            #
            # 用假设（unfixed）而不是已确认（committed）：后者滞后 4~5 轮，
            # 用它判停顿会把切口推到停顿之后好几秒 → 又切回句子中间了。
            #
            # ⚠️ **必须比「内容」，不能只比「长度」**：实测中文《圆桌派》
            # 150 秒里「等长」出现 21 次，但只有 1 次内容也相同（其余 20 次
            # 是模型在改写）→ 按长度判会在句子中间误切一刀，新会话开头
            # 就是残句。按内容判之后只剩真停顿。
            #
            #   ⚠️ 计的是「**内容连续相同的轮数**」（含第一轮），不是「不变
            #   的次数」—— 「连续 2 轮相同」只对应 **1 次**不变 ≈ 1 秒没说话。
            utxt = frame["unfixed"]
            if not utxt:
                self._pause_rounds = 0
            elif utxt == self._last_unfixed:
                self._pause_rounds += 1
            else:
                self._pause_rounds = 1
            self._last_unfixed = utxt
            self._pause_hit = self._pause_rounds >= _PAUSE_ROUNDS_LIMIT
        # 音轨起点（容器里音轨可能晚于画面起点）——前端要靠它换算
        # 「播放头时间 → 音频时间」才能让字幕跟着画面走。
        # 常见情况是 0，就不带这个键（省一点带宽）。
        if abs(origin) > 1e-6:
            frame["audio_origin_sec"] = origin
        self._broadcast({"type": "round", **frame})

    def _run(
        self,
        rid: int,
        pcm: bytes,
        sample_rate: int,
        n_channels: int,
        sample_width: int,
    ) -> None:
        """worker 线程：拿 session → RPC → 收 delta → 收最终文本。"""
        try:
            model_id, session = self._select_session()
        except RuntimeError as e:
            self._fail(rid, str(e))
            return
        except Exception as e:  # noqa: BLE001
            self._fail(rid, "取 ASR session 失败: {}".format(e))
            return

        from wdnai import AudioAttachment

        attachment = AudioAttachment(pcm, mime_type="audio/pcm")

        async def _call():
            return await session.respond(
                attachment=[attachment],
                sample_rate=sample_rate,
                n_channels=n_channels,
                sample_width=sample_width,
                on_delta=self._on_delta,
                **self._options,
            )

        t0 = time.perf_counter()
        try:
            # 与实时路径共用同一个 RPC session → 必须串行（见
            # ``_ASR_RPC_LOCK`` 的说明）。
            with _ASR_RPC_LOCK:
                result = asyncio.run(_call())
        except Exception as e:  # noqa: BLE001
            self._fail(rid, "识别失败: {}".format(e))
            return
        elapsed = time.perf_counter() - t0

        text = result if isinstance(result, str) else str(result)
        language = ""
        body = text
        if "\n" in text:
            head, body = text.split("\n", 1)
            if head.lower().startswith("language "):
                language = head[len("language "):].strip()
                body = body
        with self._lock:
            self._language = language or self._language
            self._last_result = {
                "request_id": rid,
                "model_id": model_id,
                "language": language or self._language,
                "text": body,
                "raw": text,
                "elapsed_sec": elapsed,
            }
            self._set_state(self.STATE_DONE)
        self._broadcast({
            "type": "done",
            "request_id": rid,
            "language": language or self._language,
            "text": body,
            "elapsed_sec": elapsed,
        })
        self._broadcast({"type": "state", "state": self.snapshot()})

    def _fail(self, rid: int, detail: str) -> None:
        _LOG.warning("asr request %s failed: %s", rid, detail)
        with self._lock:
            self._set_state(self.STATE_ERROR, error=detail)
        self._broadcast({"type": "error", "request_id": rid, "detail": detail})
        self._broadcast({"type": "state", "state": self.snapshot()})


# --------------------------------------------------------------------------- #
# 实时（麦克风）会话
# --------------------------------------------------------------------------- #

#: 服务端有界缓冲的块数上限（清单 §6 L3）。超过就丢**最旧**的块。
_LIVE_MAX_BUFFER_CHUNKS = 3

#: 连续丢块达到这个数就熔断 —— 会话状态已经不可信了。
_LIVE_MAX_CONSECUTIVE_DROPS = 5

#: 实时会话的时长上限（秒）。**不套用**文件路径的 60 s 截断 ——
#: 官方 Python 流式路径完全不做时长检查，1200 s 的上限只管离线路径。
_LIVE_MAX_SESSION_SEC = 300.0

#: 会话空闲回收（秒）—— 采集端崩了也不会永远占着 SDK 会话。
_LIVE_SESSION_TTL_SEC = 600.0

#: 端点检测参数（清单 §7.1）。
_LIVE_ENDPOINT_CONFIG = EndpointConfig(
    silence_threshold_dbfs=-45.0,
    endpoint_silence_ms=1200,
    max_silence_sec=30.0,
    min_speech_sec=0.3,
    action_on_endpoint=True,
)


def _split_language(text: str) -> tuple:
    """把 SDK 的 ``"language English\\n<正文>"`` 拆成 ``(语种, 正文)``。

    SDK 的 ``final_text`` 返回的就是这个形状（与离线路径的
    ``run_audio`` 一致）。文件路径在 :meth:`_AsrManager._run` 里把它
    拆开 —— 实时路径要**同样**拆，否则前端会拿到带前缀的正文。

    ⚠️ **还有第二种形状**：SDK 的 ``final_text`` 在「committed token 里
    还没出现语言分隔符 ``[PAD151704]``」时**原样返回**已提交 token 的
    文本 —— 那可能**只有语言头本身**（``language None`` /
    ``language Chinese``），后面**没有正文、也没有换行**。

    早先没处理这个形状，于是切会话（epoch 轮换）时，一个静音段的收尾
    返回 ``"language None"`` 被**当成正文**并进转写文本 ——
    实测在 120 s 样本里混进了 6 处 ``language None``。

    ⚠️ **第三种形状（粘连）**：模型输出的是 ``language`` + 分隔符 + 正文，
    但**分隔符那个 token 恰好落在回退区**（没进 ``committed``）→
    SDK 找不到分隔符 → 原样返回 ``"language" + 正文``（**没有空格、
    没有换行**）。实测在 97 分钟视频里出现过一段
    ``languageThe second, let's start...``。
    """
    text = text or ""
    head, sep, body = text.partition("\n")
    if sep:
        if head.lower().startswith("language "):
            return head[len("language "):].strip(), body
        return "", text
    # 没有换行 —— 只有两种可能：整个就是语言头，或「语言头 + 正文」粘连。
    low = text.lower()
    if low.startswith("language"):
        rest = text[len("language"):].lstrip()
        if not rest or rest.lower() in ("none", "null", "unknown", "und",
                                        "n/a"):
            # 只有语言头（语种识别失败）→ 没有正文。
            return "", ""
        if not any(ch.isspace() for ch in rest):
            # ``language <语种>`` 后面没有正文 —— 语种名是**单个词**。
            # （正文里也会有单个词，但这一段只有语言头时才可能这样。）
            return "", ""
        # 「language」直接粘着正文 → 剥掉语言头，只剥**开头**这一个
        # —— 正文中间出现的 "language" 不受影响。
        return "", rest
    return "", text


def _valid_language(lang: str) -> str:
    """过滤掉「没有语种」的返回值。

    模型在**纯静音**上会吐 ``language None`` —— 那不是语种，是「这段
    音频里没有语音」。不滤掉的话，一段静音就会把上一句的语种冲掉，
    还会往转写文本里塞一句 ``language None``。
    """
    lang = (lang or "").strip()
    if not lang:
        return ""
    if lang.lower() in ("none", "null", "unknown", "und", "n/a"):
        return ""
    return lang


@dataclass
class _LiveItem:
    """队列里的一项：一块 PCM + 等它处理完的 future。"""

    seq: int
    pcm: bytes
    future: Any
    enqueued_at: float = field(default_factory=time.monotonic)


class _LiveAsrSession:
    """一次实时识别会话（浏览器麦克风 / 板载 ALSA）。

    生命周期
    --------

    ``start`` → （若干次 ``submit``）→ ``finish`` / ``stop``

    三层背压（清单 §6）
    ------------------

    * **L1 采集层**（浏览器）：队列上限 2 块，满则丢最旧 —— 在前端。
    * **L2 传输层**：客户端串行 ``await`` 响应 —— 在前端。
    * **L3 服务端**（本类）：单 worker 串行提交 + 有界缓冲
      :data:`_LIVE_MAX_BUFFER_CHUNKS` 块 + 连续丢
      :data:`_LIVE_MAX_CONSECUTIVE_DROPS` 块熔断 + 会话上限
      :data:`_LIVE_MAX_SESSION_SEC` 强制重置。

    端点联动（清单 §7）
    ------------------

    端点命中 → 提交 ``final=True`` → 关 SDK 会话 → 广播 ``endpoint`` 帧
    → **自动开新会话**（新的 ``asr_stream_id`` + 新的原点）。已确认文本
    在 SDK 侧清零，前端负责把多句拼起来（用 ``endpoint.index`` 编号）。
    """

    CAPABILITY = "swcap_audio_asr"

    def __init__(
        self,
        manager: "_LiveAsrManager",
        stream_id: str,
        *,
        source: str = "browser",
        sample_rate: int = CAPTURE_SAMPLE_RATE,
        n_channels: int = CAPTURE_N_CHANNELS,
        sample_width: int = CAPTURE_SAMPLE_WIDTH,
        chunk_ms: int = 500,
        endpoint_config: Optional[EndpointConfig] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._manager = manager
        self.stream_id = stream_id
        self.source = source
        self.sample_rate = int(sample_rate)
        self.n_channels = int(n_channels)
        self.sample_width = int(sample_width)
        self.chunk_ms = int(chunk_ms)
        self._options = dict(options or {})
        self._detector = EndpointDetector(endpoint_config or
                                          _LIVE_ENDPOINT_CONFIG)
        # 喂送缓冲：永远留住最后 1 个采样做收尾（见 PcmFeedBuffer 的说明）
        self._feed = PcmFeedBuffer(
            sample_rate=self.sample_rate,
            n_channels=self.n_channels,
            sample_width=self.sample_width,
            block_seconds=1.0,
        )
        self._queue: asyncio.Queue = asyncio.Queue(
            maxsize=_LIVE_MAX_BUFFER_CHUNKS)
        self._worker: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        self._session = None
        self._model_id: str = ""
        self._sdk_epoch = 0
        self._sdk_stream_id = ""
        self._origin_sec = 0.0
        self._timeline_sec = 0.0

        self._last_round = 0
        self._last_seq = 0
        self._committed = ""
        self._unfixed = ""
        self._language = ""
        self._final_text = ""
        self._segments: List[str] = []
        #: 当前 SDK 会话里**有没有说过话**。
        #:
        #: 纯静音的会话，模型会吐 ``language None`` —— 那不是转写内容，
        #: 拼进文本里就是「…for other people. language None」。所以只有
        #: 「这一段确实有语音」的会话才允许贡献文本。
        self._session_had_speech = False
        self._lag_ms = 0
        self._dropped_chunks = 0
        self._consecutive_drops = 0
        self._duplicate_chunks = 0
        self._expected_seq = 0
        self._endpoint_index = 0
        self._endpoint_hit = False
        self._closed = False
        self._finished = False
        self._error = ""
        self._rounds: List[Dict[str, Any]] = []
        self._created_at = time.monotonic()
        self._last_seen = time.monotonic()

    # -------------------------------------------------------------- 生命周期
    def select_session(self):
        """拿 ASR 的 RPC session；没启用就抛 RuntimeError（HTTP 502）。"""
        from app.core.compute.registry import get_registry

        registry = get_registry()
        handles = registry.list_sessions(self.CAPABILITY, include_remote=True)
        if not handles:
            raise RuntimeError("功能未启用: {}".format(self.CAPABILITY))
        chosen = handles[0]
        self._session = chosen.session
        self._model_id = chosen.model_id
        return chosen

    async def start(self) -> None:
        """起 worker 协程。"""
        self._loop = asyncio.get_running_loop()
        self._sdk_epoch += 1
        self._sdk_stream_id = "{}#{}".format(self.stream_id, self._sdk_epoch)
        self._worker = asyncio.create_task(
            self._worker_loop(), name="asr-live-{}".format(self.stream_id))
        _LOG.info("live session %s started (source=%s model=%s)",
                  self.stream_id, self.source, self._model_id)

    async def submit(self, pcm: bytes, seq: int) -> dict:
        """提交一块 PCM，返回这一块的识别结果。

        客户端**串行 await** 本方法 → 服务端的处理速率自动被推理速率钳住
        （L2 传输层背压）。

        丢块记账在**入队时**做（不是在 worker 里）：采集端丢的块只有
        「下一个块的序号」能证明它丢了，而那个序号在入队时就已经看到了。
        """
        if self._closed:
            raise RuntimeError("会话已结束: {}".format(self.stream_id))
        loop = asyncio.get_running_loop()
        fut = loop.create_future()

        # 1) 序号判定 + 丢块记账。
        kind, from_seq, to_seq = self._classify_seq(seq)
        if kind == "dup":
            # 重复送来的旧块：音频已经在模型里了，再喂一遍就是重复。
            _LOG.warning("live %s: 重复块 seq=%s（已丢弃）",
                         self.stream_id, seq)
            self._duplicate_chunks += 1
            res = self._result_payload(seq)
            fut.set_result(res)
            return res
        if kind == "gap":
            n = to_seq - from_seq + 1
            self._dropped_chunks += n
            self._consecutive_drops += n
            self._publish({
                "type": "gap",
                "stream_id": self.stream_id,
                "dropped_chunks": n,
                "from_seq": from_seq,
                "to_seq": to_seq,
            })
            _LOG.warning("live %s: 音频丢失 %s 块（seq %s..%s）",
                         self.stream_id, n, from_seq, to_seq)
            if self._consecutive_drops >= _LIVE_MAX_CONSECUTIVE_DROPS:
                self._fail("连续丢失 {} 块音频，会话已中止（识别结果不可信）"
                           .format(self._consecutive_drops))
                await self.stop()
                res = self._result_payload(seq)
                fut.set_result(res)
                return res

        # 2) 入队 —— 有界缓冲：满了丢**最旧**的块（保实时性优先）。
        item = _LiveItem(seq=seq, pcm=pcm, future=fut,
                         enqueued_at=time.monotonic())
        while True:
            try:
                self._queue.put_nowait(item)
                break
            except asyncio.QueueFull:
                try:
                    old = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    continue
                self._drop_item(old)
        self._last_seen = time.monotonic()
        return await fut

    async def finish(self) -> dict:
        """结束会话：把尾巴喂给 SDK（final=True）→ 返回最终文本。"""
        if not self._closed:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=120.0)
            except asyncio.TimeoutError:
                _LOG.warning("live %s: 收尾等待超时", self.stream_id)
            if not self._finished:
                await self._finalize(reason="finish")
        # 收尾完成 → worker 协程没活干了，**收掉它**。
        # 不收的话它会一直挂在 ``await self._queue.get()`` 上，等会话被
        # GC 掉时抛 "Task was destroyed but it is pending"（实测见过）。
        await self._stop_worker()
        return {
            "ok": True,
            "language": self._language,
            "text": self.transcript(),
            "elapsed_sec": round(self._timeline_sec, 3),
            "endpoint_hit": self._endpoint_hit,
            "segments": len(self._segments),
        }

    async def stop(self) -> dict:
        """立即中止：停掉 worker、丢掉队列、释放 SDK 会话。"""
        self._closed = True
        await self._stop_worker()
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not item.future.done():
                item.future.set_result(self._result_payload(item.seq))
        self._feed.reset()
        _LOG.info("live session %s stopped (dropped=%s)",
                  self.stream_id, self._dropped_chunks)
        return {"ok": True, "dropped_chunks": self._dropped_chunks}

    async def _stop_worker(self) -> None:
        """收掉 worker 协程（幂等）。"""
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        worker.cancel()
        try:
            await worker
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    def cancel_worker_sync(self) -> None:
        """**同步**收掉 worker 协程（不 await）。

        给「没有事件循环上下文也能调」的路径用 —— 目前是 TTL 回收
        （:meth:`_LiveAsrManager._reap`），它可能在 cshim 线程上被调到。

        ⚠️ ``Task.cancel()`` **不是线程安全的** → 不在事件循环线程时
        改用 ``call_soon_threadsafe``。
        """
        worker = self._worker
        if worker is None:
            return
        self._worker = None
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            worker.cancel()
            return
        try:
            loop.call_soon_threadsafe(worker.cancel)
        except RuntimeError:  # pragma: no cover — 循环已关闭
            pass

    # -------------------------------------------------------------- 状态
    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def elapsed_sec(self) -> float:
        return self._timeline_sec

    @property
    def dropped_chunks(self) -> int:
        return self._dropped_chunks

    @property
    def endpoint_index(self) -> int:
        return self._endpoint_index

    def snapshot(self) -> dict:
        return {
            "stream_id": self.stream_id,
            "source": self.source,
            "model_id": self._model_id,
            "state": "error" if self._error else ("done" if self._finished
                                                  else "running"),
            "active": not self._closed,
            "language": self._language,
            "committed": self._committed,
            "unfixed": self._unfixed,
            "round_count": len(self._rounds),
            "elapsed_sec": round(self._timeline_sec, 3),
            "dropped_chunks": self._dropped_chunks,
            "duplicate_chunks": self._duplicate_chunks,
            "endpoint_index": self._endpoint_index,
            "endpoint_hit": self._endpoint_hit,
            "error": self._error,
            "chunk_ms": self.chunk_ms,
        }

    # -------------------------------------------------------------- worker
    async def _worker_loop(self) -> None:
        """单 worker：保证同一会话内 ``respond()`` 严格串行。"""
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    return
                await self._handle(item)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                _LOG.warning("live %s: 处理失败: %s", self.stream_id, e)
                self._fail("实时识别失败: {}".format(e))
                if not item.future.done():
                    item.future.set_result(self._result_payload(item.seq))
                return
            finally:
                self._queue.task_done()

    async def _handle(self, item: _LiveItem) -> None:
        """处理一块 PCM（丢包判定已经在入队时做完了）。"""
        pcm = item.pcm
        dur = len(pcm) / float(
            self.sample_rate * self.n_channels * self.sample_width or 1)

        # 1) 端点检测。**静音照样要喂给模型** —— 端点只决定何时收尾。
        ev = self._detector.feed(
            pcm, duration_sec=dur, sample_rate=self.sample_rate,
            sample_width=self.sample_width, n_channels=self.n_channels)
        if ev.has_speech:
            self._session_had_speech = True

        # 2) 喂给 SDK（整秒块；留住最后 1 个采样做收尾）
        for blk in self._feed.push(pcm):
            await self._submit(blk, final=False)
        # 成功喂进去一块 → 「连续丢块」计数清零。
        self._consecutive_drops = 0

        # 3) 端点 / 纯静音超时 / 会话上限
        if ev.action:
            await self._finalize(reason="endpoint")
        elif ev.force_end:
            _LOG.info("live %s: 纯静音 %.1fs，强制结束",
                      self.stream_id, ev.elapsed_sec)
            await self._finalize(reason="silence_timeout")
        elif self._timeline_sec >= _LIVE_MAX_SESSION_SEC:
            _LOG.info("live %s: 达到会话上限 %.0fs，强制重置",
                      self.stream_id, _LIVE_MAX_SESSION_SEC)
            await self._finalize(reason="session_limit")

        self._last_seen = time.monotonic()
        self._lag_ms = int((time.monotonic() - item.enqueued_at) * 1000)
        if not item.future.done():
            item.future.set_result(self._result_payload(item.seq))

    # -------------------------------------------------------------- SDK
    async def _submit(self, pcm: bytes, *, final: bool) -> str:
        """把一块 PCM 喂给 SDK；返回它回传的文本。"""
        if self._session is None:
            raise RuntimeError("功能未启用: {}".format(self.CAPABILITY))
        from wdnai import AudioAttachment

        attachment = AudioAttachment(pcm, mime_type="audio/pcm")
        options = dict(self._options)
        options["asr_stream_id"] = self._sdk_stream_id
        options["asr_stream_final"] = bool(final)
        options["asr_stream_origin_sec"] = self._origin_sec

        async def _call():
            return await self._session.respond(
                attachment=[attachment],
                sample_rate=self.sample_rate,
                n_channels=self.n_channels,
                sample_width=self.sample_width,
                on_delta=self._on_delta,
                **options,
            )

        text = await self._rpc_call(_call)
        self._timeline_sec += len(pcm) / float(
            self.sample_rate * self.n_channels * self.sample_width or 1)
        if final:
            raw = text if isinstance(text, str) else str(text)
            lang, body = _split_language(raw)
            self._final_text = body
            lang = _valid_language(lang)
            if lang:
                self._language = lang
        return text or ""

    async def _rpc_call(self, factory):
        """在进程级互斥下调用 RPC（见 ``_ASR_RPC_LOCK``）。"""
        while True:
            if _ASR_RPC_LOCK.acquire(blocking=False):
                break
            # 另一条识别正在跑 → 让出事件循环，别把整个 Web 服务卡住。
            await asyncio.sleep(0.05)
        try:
            return await factory()
        finally:
            _ASR_RPC_LOCK.release()

    def _on_delta(self, raw: str) -> None:
        """worker 发来的一轮载荷 —— 解析后广播给浏览器。

        解析只发生在这里（主进程侧）；worker 端不认识这个 schema。
        回调在 cshim 线程上跑 → 广播要切回事件循环（见 :meth:`_publish`）。
        """
        try:
            payload = json.loads(raw)
        except Exception:  # noqa: BLE001 — 非 JSON 的 delta 一律忽略
            return
        if not isinstance(payload, dict):
            return
        kind = payload.get("type")
        if kind == "asr_final":
            lang = _valid_language(payload.get("language") or "")
            if lang:
                self._language = lang
            return
        if kind != "asr_round":
            return
        frame = {
            "type": "round",
            "stream_id": self.stream_id,
            "round": int(payload.get("round") or 0),
            "committed": payload.get("committed") or "",
            "unfixed": payload.get("unfixed") or "",
            "language": payload.get("language") or "",
        }
        # 时间戳是**加法式**扩展 —— 老消费方忽略这两个键即可。
        start = payload.get("chunk_start_sec")
        if start is not None:
            try:
                frame["chunk_start_sec"] = float(start)
                frame["chunk_end_sec"] = (
                    float(start)
                    + float(payload.get("chunk_samples") or 0)
                    / float(CAPTURE_SAMPLE_RATE or 1))
            except (TypeError, ValueError):
                pass
        self._last_round = frame["round"]
        self._committed = frame["committed"]
        self._unfixed = frame["unfixed"]
        lang = _valid_language(frame["language"])
        if lang:
            self._language = lang
        self._rounds.append(frame)
        self._publish(frame)

    async def _finalize(self, *, reason: str) -> None:
        """收尾：尾巴喂给 SDK（final=True）→ 关会话 → 广播。"""
        if self._finished:
            return
        tail = self._feed.flush()
        try:
            if tail:
                await self._submit(tail, final=True)
        except Exception as e:  # noqa: BLE001
            _LOG.warning("live %s: 收尾失败: %s", self.stream_id, e)
            self._fail("收尾失败: {}".format(e))
            return
        self._finished = True

        if reason == "endpoint":
            # 一句话结束 → 广播 endpoint 帧 → **自动开新会话**。
            self._endpoint_hit = True
            self._endpoint_index += 1
            if self._final_text.strip() and self._session_had_speech:
                self._segments.append(self._final_text.strip())
            self._publish({
                "type": "endpoint",
                "stream_id": self.stream_id,
                "language": self._language,
                "text": self._final_text,
                "index": self._endpoint_index,
                "elapsed_sec": round(self._timeline_sec, 3),
            })
            self._rotate()
            self._finished = False
        else:
            self._closed = True
            self._publish({
                "type": "live_done",
                "stream_id": self.stream_id,
                "reason": reason,
                "language": self._language,
                "text": self._final_text,
                "elapsed_sec": round(self._timeline_sec, 3),
                "dropped_chunks": self._dropped_chunks,
            })

    def transcript(self) -> str:
        """**累积**的转写文本（端点切出的各句 + 当前句）。

        端点会把 SDK 会话重置，已确认文本在 SDK 侧清零 —— 但对用户
        来说那是一**整段话**，所以这里把各句拼起来。浏览器端也做同样
        的拼接（靠 ``endpoint`` 帧），两条路径读到的文本因此一致。
        """
        parts = list(self._segments)
        cur = self._final_text.strip()
        if cur and self._session_had_speech:
            parts.append(cur)
        return " ".join(p for p in parts if p)

    def _rotate(self) -> None:
        """端点之后开新 SDK 会话：新的 ``asr_stream_id`` + 新的原点。"""
        self._sdk_epoch += 1
        self._sdk_stream_id = "{}#{}".format(self.stream_id, self._sdk_epoch)
        self._origin_sec = self._timeline_sec
        self._detector.reset()
        self._session_had_speech = False
        self._last_round = 0
        self._committed = ""
        self._unfixed = ""
        self._final_text = ""      # 上一句已经进 _segments，别重复计入
        _LOG.info("live %s: 端点 → 新会话 %s（原点 %.2fs）",
                  self.stream_id, self._sdk_stream_id, self._origin_sec)

    # -------------------------------------------------------------- 杂项
    def _classify_seq(self, seq: int) -> tuple:
        """比对块序号。返回 ``(kind, from_seq, to_seq)``。

        kind: ``"ok"`` / ``"gap"``（缺号）/ ``"dup"``（重复或乱序）。

        ⚠️ 这里推进 ``_expected_seq`` —— 所以**入队时**就该调用它：
        采集端丢的块只有「下一个块的序号」能证明，而那是在入队时看到的。
        在 worker 里判定会晚一步（而且会和「服务端缓冲丢最旧」重复计数）。
        """
        if seq == self._expected_seq:
            self._expected_seq = seq + 1
            return ("ok", seq, seq)
        if seq > self._expected_seq:
            from_seq = self._expected_seq
            to_seq = seq - 1
            self._expected_seq = seq + 1
            return ("gap", from_seq, to_seq)
        return ("dup", seq, seq)

    def _drop_item(self, item: _LiveItem) -> None:
        """丢掉队列里最旧的一块（缓冲满时）。"""
        self._dropped_chunks += 1
        self._consecutive_drops += 1
        self._publish({
            "type": "gap",
            "stream_id": self.stream_id,
            "dropped_chunks": 1,
            "from_seq": item.seq,
            "to_seq": item.seq,
        })
        _LOG.warning("live %s: 服务端缓冲满，丢弃最旧的块 seq=%s",
                     self.stream_id, item.seq)
        if not item.future.done():
            item.future.set_result(self._result_payload(item.seq))

    def _fail(self, detail: str) -> None:
        self._error = detail
        self._closed = True
        _LOG.warning("live %s: %s", self.stream_id, detail)
        self._publish({
            "type": "error",
            "stream_id": self.stream_id,
            "detail": detail,
        })

    def _result_payload(self, seq: int) -> dict:
        return {
            "ok": not self._error,
            "seq": seq,
            "round": self._last_round,
            "committed": self._committed,
            "unfixed": self._unfixed,
            "language": self._language,
            "lag_ms": self._lag_ms,
            "buffered_chunks": self._queue.qsize(),
            "dropped_chunks": self._dropped_chunks,
            "error": self._error,
        }

    # -------------------------------------------------------------- 广播
    def _publish(self, payload: dict) -> None:
        """广播一帧给浏览器（线程安全）。

        delta 回调在 cshim 线程上 → 必须切回事件循环再 ``put_nowait``
        （``asyncio.Queue`` 不是线程安全的）。
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._publish_now(payload)
            return
        try:
            loop.call_soon_threadsafe(self._publish_now, payload)
        except RuntimeError:  # pragma: no cover — 循环已关闭
            pass

    def _publish_now(self, payload: dict) -> None:
        try:
            get_asr_service().broadcast(payload)
        except Exception as e:  # noqa: BLE001
            _LOG.debug("live broadcast failed: %s", e)


class _LiveAsrManager:
    """实时会话管理器（单例）。

    负责：建会话 / 找会话 / 回收空闲会话 / 全部停掉。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: Dict[str, _LiveAsrSession] = {}

    # ------------------------------------------------------------------ #
    def create(self, *, source: str, sample_rate: int, n_channels: int,
               sample_width: int, chunk_ms: int,
               options: Optional[Dict[str, Any]] = None) -> _LiveAsrSession:
        """建一个新会话（stream_id 自动生成）。"""
        import uuid

        self._reap()
        stream_id = "live-{}".format(uuid.uuid4().hex[:12])
        session = _LiveAsrSession(
            self, stream_id, source=source, sample_rate=sample_rate,
            n_channels=n_channels, sample_width=sample_width,
            chunk_ms=chunk_ms, options=options)
        with self._lock:
            self._sessions[stream_id] = session
        return session

    def get(self, stream_id: str) -> Optional[_LiveAsrSession]:
        with self._lock:
            return self._sessions.get(stream_id)

    def drop(self, stream_id: str) -> None:
        with self._lock:
            self._sessions.pop(stream_id, None)

    def sessions(self) -> List[_LiveAsrSession]:
        with self._lock:
            return list(self._sessions.values())

    def active(self) -> Optional[_LiveAsrSession]:
        """当前活跃的会话（有几个算几个；前端只展示一个）。"""
        self._reap()
        for s in self.sessions():
            if not s.closed:
                return s
        return None

    def has_active(self) -> bool:
        return self.active() is not None

    async def stop_all(self) -> None:
        for s in self.sessions():
            try:
                await s.stop()
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            self._sessions.clear()

    def _reap(self) -> None:
        """回收空闲超时的会话（采集端崩了也不会永远占着）。"""
        now = time.monotonic()
        stale = []
        for s in self.sessions():
            if (now - s._last_seen) > _LIVE_SESSION_TTL_SEC:
                stale.append(s)
        for s in stale:
            # ⚠️ 直接 ``drop`` 会把 worker 协程留在那儿悬着（会话没了，
            # 协程还挂在 ``queue.get()`` 上）→ GC 时抛 "Task was destroyed
            # but it is pending"。回收要**连 worker 一起收**。
            s.cancel_worker_sync()
            self.drop(s.stream_id)
            _LOG.info("live session %s reaped (idle)", s.stream_id)


# --------------------------------------------------------------------------- #
# Singleton
# --------------------------------------------------------------------------- #
_asr_manager: Optional[_AsrManager] = None
_asr_manager_lock = threading.Lock()
_live_manager: Optional[_LiveAsrManager] = None
_live_manager_lock = threading.Lock()


def get_asr_service() -> _AsrManager:
    """Return the ASR service singleton (lazy)."""
    global _asr_manager
    if _asr_manager is None:
        with _asr_manager_lock:
            if _asr_manager is None:
                _asr_manager = _AsrManager()
    return _asr_manager


def get_live_asr_service() -> _LiveAsrManager:
    """Return the live-ASR session manager singleton (lazy)."""
    global _live_manager
    if _live_manager is None:
        with _live_manager_lock:
            if _live_manager is None:
                _live_manager = _LiveAsrManager()
    return _live_manager


def close() -> None:
    """Release the singleton (shutdown hook)."""
    global _asr_manager
    with _asr_manager_lock:
        if _asr_manager is not None:
            try:
                _asr_manager.stop()
            except Exception:  # noqa: BLE001
                pass
        _asr_manager = None
    # 实时会话也要停 —— 否则 SDK 侧的流式会话会挂到 TTL 才回收。
    global _live_manager
    with _live_manager_lock:
        mgr = _live_manager
        _live_manager = None
    if mgr is not None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        try:
            if loop is not None:
                # 已经在事件循环里（FastAPI 的 shutdown 钩子）→ 不能
                # ``asyncio.run``（会抛 "cannot be called from a running
                # event loop"）。挂成任务，让循环自己收尾。
                loop.create_task(mgr.stop_all())
            else:
                asyncio.run(mgr.stop_all())
        except Exception:  # noqa: BLE001
            pass
