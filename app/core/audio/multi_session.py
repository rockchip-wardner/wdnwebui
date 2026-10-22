"""MultiAudioSession —— 1~32 路音频流 → 严格顺序轮转 → 送 Omni 推理。

与视频那侧的对照：视频多路取的是「此刻的一帧」，描述当下；音频多路取的是
「一段连续的音频」，取到哪个时刻取决于这一路排在轮转序列的哪一位。所以
轮转严格 0→N-1，每路拿到的都是「它自己的下一段」（文件源）或「它此刻的
最新一段」（RTSP 源），与它在序列中的位置无关。

**滞后**（:attr:`lag_s`）是分时复用的固有代价：单实例 0.7s/次 → 32 路一圈
22.4s，某一路的描述必然比它的音频晚二十几秒。不是缺陷，前端显式显示。

NPU 互斥：**不用** ``get_session_call_lock()``。那把锁是进程级的，被音频分析
/ VR 视频检索 / 微信视频检索共用，而 Omni 与 VLM 是两个不同
的 session、worker 进程和 NPU，不需要互斥。这里改用
一把 **Omni 专属的模块级锁**，只保护「同一 Omni session 上的 reset() +
respond() 不并发」。

线程模型::

    N 个采集线程   每路一个 AudioSource（文件：顺序解码；RTSP：环形缓冲）
    1 个轮转线程   严格 ch0→ch1→…→chN-1，每路取一段送推理（阻塞）
    1 个推理调用   Omni 单实例，串行

结束条件：**全是文件源**时每路音轨放完 → 该路 done → 全部 done → 会话
``done``；**含 RTSP 源**时 RTSP 永远不 done，会话不会自动结束，一直轮转到
用户停止（混合源同理）。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

from wdnai import SoftwareCapability

from app.core.audio.source import (
    DEFAULT_WINDOW_S,
    AudioChunk,
    AudioSource,
    make_audio_source,
)

LOG = logging.getLogger("wdnwebui.audio.multi_session")

MAX_CHANNELS = 32
MAX_NEW_TOKENS = 512
# SDK 推理超时。实测单次 0.7s，60s 是宽裕的上限（worker 卡住时不至于
# 把整轮转拖死）。
INFER_TIMEOUT = 60.0
# open() 之后等所有源「首段就绪或确定放完」的上限。文件源首段 ~56ms，
# RTSP 源要攒满一个窗口（6s）。超过这个上限就开轮转，没就绪的路首轮
# 会被跳过、下一轮补上 —— 不因为一路 RTSP 慢就把整个会话卡住。
PRIMING_TIMEOUT = 8.0
# 一轮里所有路都没取到段时的退避（秒）。典型场景：RTSP 源还在预热。
RR_IDLE_WAIT = 0.2
# 连续多少段推理失败就熔断整个会话。3 是「肯定不是偶发」的下限：正常
# 路径下偶发单段失败是有可能的（音频太短等），但连续 3 段不同通道都失败
# 基本只有一个原因 —— **worker 进程没了 / RPC 通道断了**。
#
# 不熔断的代价：本地 worker 起来后 registry 就不再 probe 它（见
# compute/registry.py::_probe_instance 的退出条件），worker 被 OOM kill
# 后状态永远停在 "running"，于是每段都要等 SDK 的 5 次重试 × 5s 超时
# 才失败 —— 32 路 399 段就是好几个小时，界面上只看到一条条「推理失败」。
CONSEC_FAIL_LIMIT = 3

STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_STOPPED = "stopped"
STATE_ERROR = "error"

# Omni 专属互斥锁。**不是** get_session_call_lock()
_omni_call_lock = threading.Lock()


@dataclass
class _Channel:
    """一路音频的状态。"""

    idx: int
    source: AudioSource
    done: bool = False
    chunks_done: int = 0
    errors: int = 0
    lag_s: float = 0.0

    def snapshot(self) -> dict:
        src = self.source.snapshot()
        return {
            "idx": self.idx,
            "source": self.source.source,
            "kind": src["kind"],
            "state": src["state"],
            "error": src["error"],
            "done": self.done,
            "chunks_done": self.chunks_done,
            "total_chunks": src["total_chunks"],
            "exhausted": src["exhausted"],
            "errors": self.errors,
            "lag_s": round(self.lag_s, 1),
        }


class MultiAudioSession:
    """多路音频会话（单例由调用方持有）。"""

    def __init__(self, on_event: Optional[Callable[[dict], None]] = None,
                 on_state: Optional[Callable[[str], None]] = None) -> None:
        self._lock = threading.RLock()
        self._channels: List[_Channel] = []
        self._event_id = 0
        self._state = STATE_IDLE
        self._error = ""
        self._prompt = ""
        self._model_id = ""
        self._round = 0
        self._current = -1
        self._inferred = 0
        self._errors = 0
        # 连续推理失败计数。达到 CONSEC_FAIL_LIMIT 就熔断：worker 进程死了
        # 时 registry 仍会报 "running"（本地 worker 起来后就不再被 probe），
        # 于是每段都要等满超时才失败 —— 399 段就是好几个小时。熔断后明确
        # 报「worker 失联」，让用户一眼看懂。
        self._consec_failures = 0
        self._rr_thread: Optional[threading.Thread] = None
        self._rr_gen = 0
        self._stop_evt = threading.Event()
        self._session = None          # Omni session（懒获取，失败时重取）
        # 事件 / 状态回调。**直接回调**而不是让调用方轮询 —— 32 路轮转
        # 一圈 22.4s，轮询会让前端的事件延迟到下一个轮询周期。
        self._on_event = on_event
        self._on_state = on_state

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def open(self, sources: List[str], prompt: str,
             model_id: str = "") -> dict:
        """开一路或多路音频分析。

        ``sources`` 1~32 条，每条一路。文件走顺序解码，rtsp 走环形缓冲。
        """
        with self._lock:
            if self._state == STATE_RUNNING:
                raise RuntimeError("音频分析已在运行，请先停止")
        srcs = [s for s in (sources or []) if (s or "").strip()]
        if not srcs:
            raise ValueError("sources 不能为空")
        if len(srcs) > MAX_CHANNELS:
            raise ValueError(f"最多 {MAX_CHANNELS} 路，当前 {len(srcs)} 路")

        chans: List[_Channel] = []
        for i, s in enumerate(srcs):
            src = make_audio_source(s, window_s=DEFAULT_WINDOW_S)
            chans.append(_Channel(idx=i, source=src))

        with self._lock:
            # **代号必须最先推进**：换了 _channels / _stop_evt 却还留着旧代号，
            # 旧轮转线程就可能在这个窗口里读到**新**的通道列表、代号校验却通过
            # —— 它会去消费新会话的源。先换代号就没有这个窗口。
            self._rr_gen += 1
            gen = self._rr_gen
            self._channels = chans
            self._prompt = prompt or ""
            self._model_id = model_id or ""
            self._state = STATE_RUNNING
            self._error = ""
            self._round = 0
            self._current = -1
            self._inferred = 0
            self._errors = 0
            self._consec_failures = 0
            self._event_id = 0
            self._stop_evt = threading.Event()

        # 起采集线程
        for ch in chans:
            try:
                ch.source.start()
            except Exception as exc:  # noqa: BLE001
                LOG.warning("audio source %s 启动失败: %s", ch.source.source, exc)
                ch.source.state = "error"
                ch.source._error = str(exc)   # noqa: SLF001
                ch.source.exhausted = True

        # 等首段就绪 —— 否则第一轮会白跑一遍（所有路都取不到段）。
        deadline = time.time() + PRIMING_TIMEOUT
        for ch in chans:
            while time.time() < deadline and not ch.source.primed():
                time.sleep(0.02)

        # 起轮转线程（代号已在上面的状态块里捕获并推进过，这里不要再动 ——
        # 中途再改一次会让「状态已换、代号未换」的窗口重新出现）
        self._rr_thread = threading.Thread(
            target=self._rr_loop, args=(gen,), name="audio-rr", daemon=True)
        self._rr_thread.start()
        return self.snapshot()

    def close(self) -> dict:
        """停止轮转 + 停所有采集线程。"""
        with self._lock:
            self._rr_gen += 1
            self._state = STATE_STOPPED
            self._current = -1
            chans = list(self._channels)
            self._channels = []
        self._stop_evt.set()
        t = self._rr_thread
        if t is not None and t.is_alive():
            t.join(timeout=3.0)
            if t.is_alive():
                # 正常一次推理 ~0.7s，3s 到点说明这次推理很慢或卡住。
                # 线程不会泄漏：_rr_gen 前进后它会在**当前这次推理结束**时
                # 自行退出（代际校验同时拦掉它的结果，不让它污染新会话）。
                # 这里不能等满 INFER_TIMEOUT —— stop() 不该被一次卡住的推理
                # 拖住。
                LOG.info("audio: 轮转线程仍在推理中，停止后其结果将被丢弃")
        self._rr_thread = None
        for ch in chans:
            try:
                ch.source.stop()
            except Exception as exc:  # noqa: BLE001
                LOG.warning("停止音频源 %s 失败: %s", ch.source.source, exc)
        return self.snapshot()

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self._state,
                "error": self._error,
                "prompt": self._prompt,
                "model_id": self._model_id,
                "round": self._round,
                "current": self._current,
                "channels": len(self._channels),
                "inferred": self._inferred,
                "errors": self._errors,
                "per_channel": [c.snapshot() for c in self._channels],
            }

    # ------------------------------------------------------------------ #
    # 轮转
    # ------------------------------------------------------------------ #
    def _rr_loop(self, gen: int) -> None:
        """严格 ch0→ch1→…→chN-1 轮转。

        每一轮遍历所有**还活着**的路：已放完的路直接跳过，所以轮转会随
        文件源陆续放完而自动变短（ch0 放完后序列变成 ch1→…→chN-1）。
        """
        while not self._stop_evt.is_set():
            with self._lock:
                if gen != self._rr_gen:
                    return
                channels = list(self._channels)
            if not channels:
                break

            progressed = False
            for ch in channels:
                if self._stop_evt.is_set():
                    return
                # 代际校验要**每路都查**，不能只放在 while 顶部：一轮 N 路 ×
                # ~0.7s = 十几秒，只查一次旧线程能白跑完整轮。
                with self._lock:
                    stale = gen != self._rr_gen
                if stale:
                    return
                if ch.done:
                    continue
                chunk = ch.source.next_chunk()
                if chunk is None:
                    if ch.source.exhausted:
                        with self._lock:
                            ch.done = True
                    continue
                progressed = True
                ok = self._infer(ch, chunk, gen)
                # 连续失败熔断。ok=None 是「结果作废」（会话已换代），不算失败。
                if ok is None:
                    consec = 0
                else:
                    with self._lock:
                        self._consec_failures = 0 if ok else self._consec_failures + 1
                        consec = self._consec_failures
                if consec >= CONSEC_FAIL_LIMIT:
                    with self._lock:
                        self._state = STATE_ERROR
                        self._error = (
                            f"连续 {consec} 段推理失败（Omni worker 可能已失联），"
                            "会话已停止")
                    LOG.error("audio: %s", self._error)
                    self._notify_state()
                    return

            # 收尾块也要验代号。旧线程跑到这里时 `_channels` 已被 close() 清空
            # （`remaining` 因此为空），不管的话它会把新会话的 `_round` 多加
            # 一轮、把 `_state` 改成 DONE（用户点的是「停止」）。
            with self._lock:
                if gen != self._rr_gen:
                    return
                self._round += 1
                remaining = [c for c in self._channels if not c.done]
            if not remaining:
                # 全部放完 → 正常结束。（含 RTSP 源时不会走到这里。）
                with self._lock:
                    self._state = STATE_DONE
                LOG.info("audio-rr: 全部 %d 路分析完毕", len(self._channels))
                self._notify_state()
                break
            if not progressed:
                # 一轮里所有路都没取到段：典型是 RTSP 还在预热。退避，
                # 否则这里就是 tight-loop（每秒几千次空转烧核）。
                self._stop_evt.wait(RR_IDLE_WAIT)

    # ------------------------------------------------------------------ #
    def _get_session(self):
        """拿 Omni session。失败时重取一次（worker 可能重启过）。"""
        if self._session is not None:
            return self._session
        from app.core.compute.registry import get_registry
        reg = get_registry()
        if self._model_id:
            self._session = reg.get_session_for_model(
                SoftwareCapability.AUDIO_CHAT, self._model_id)
        else:
            self._session = reg.get_session(SoftwareCapability.AUDIO_CHAT)
        return self._session

    def _infer(self, ch: _Channel, chunk: AudioChunk, gen: int) -> None:
        """把一段音频送 Omni 推理，产出事件。

        隔离线程 + 新 event loop 跑 async respond()，避免与主进程 event loop
        共享 thread pool。

        ``gen`` 是本次推理所属的**会话代号**。推理可能横跨一次 stop/重开
        （单次推理最长 INFER_TIMEOUT+5s，而 close() 只等 3s），回来时会话早已
        换代 —— 这时结果必须**丢弃**，理由见下方代际校验。
        """
        t_start = time.time()
        text = ""
        level: Optional[str] = None
        err = ""

        try:
            sess = self._get_session()
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
            text = f"[错误] {err}"
            level = "error"
            self._session = None
        else:
            from wdnai import Attachment
            coro = sess.respond(
                prompt=self._prompt,
                attachment=[Attachment(chunk.pcm, kind="audio")],
                sample_rate=16000,
                n_channels=1,
                sample_width=2,
                max_new_tokens=MAX_NEW_TOKENS,
            )
            result: List[Any] = [None]
            error: List[Optional[BaseException]] = [None]
            finished: List[bool] = [False]

            def _runner() -> None:
                loop = asyncio.new_event_loop()
                try:
                    result[0] = loop.run_until_complete(
                        asyncio.wait_for(coro, timeout=INFER_TIMEOUT))
                except asyncio.TimeoutError:
                    error[0] = "timeout"
                except Exception as ex:  # noqa: BLE001
                    error[0] = ex
                finally:
                    try:
                        loop.close()
                    except Exception:  # noqa: BLE001
                        pass
                    finished[0] = True

            try:
                with _omni_call_lock:
                    sess.reset()
                    th = threading.Thread(
                        target=_runner, daemon=True,
                        name=f"audio-infer-ch{ch.idx}")
                    th.start()
                    th.join(timeout=INFER_TIMEOUT + 5.0)
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {exc}"
                text = f"[错误] {err}"
                level = "error"
                self._session = None

            if not level:
                if not finished[0]:
                    text = f"[错误] 推理超时：{INFER_TIMEOUT:.0f}s 内 SDK 未响应"
                    level = "error"
                elif error[0] == "timeout":
                    text = f"[错误] 推理超时：{INFER_TIMEOUT:.0f}s 内 SDK 未响应"
                    level = "error"
                elif error[0] is not None:
                    text = f"[错误] {type(error[0]).__name__}: {error[0]}"
                    level = "error"
                    self._session = None
                else:
                    raw = result[0]
                    text = (getattr(raw, "text", None)
                            or (raw if isinstance(raw, str) else str(raw)))
                    text = (text or "").strip()

        # ---- 代际校验：在途结果可能已经过期 ------------------------------
        # 会话被 stop() / 重开() 后 _rr_gen 会前进，这次推理的结果已不属于
        # 当前会话：投进事件列表 → 上一轮的路出现在新一轮里；计入
        # inferred/errors → 进度条凭空多几段；改写 _current → 「当前通道」
        # 指错路。必须在**动任何状态之前**拦掉。
        with self._lock:
            stale = gen != self._rr_gen
        if stale:
            LOG.info("audio: 丢弃过期结果 ch%d 段%d（会话已换代）",
                     ch.idx, ch.chunks_done)
            return None      # 不是失败，是「作废」—— 熔断计数不计入

        ts = time.time()
        with self._lock:
            ch.chunks_done += 1
            # 滞后 = 从这段音频被**产出**到描述出来，隔了多久。
            # 文件源 = 预取排队 + 推理；RTSP 源 ≈ 推理本身。
            ch.lag_s = ts - chunk.captured_at
            if level == "error":
                ch.errors += 1
                self._errors += 1
            else:
                self._inferred += 1
            self._current = ch.idx

        ev = {
            "id": self._next_id(),
            "channel": ch.idx,
            "chunk_idx": ch.chunks_done - 1,
            "start_s": round(chunk.win_start_s, 2),
            "end_s": round(chunk.win_end_s, 2),
            "duration_s": round(chunk.duration_s, 3),
            "captured_at": chunk.captured_at,
            "lag_s": round(ts - chunk.captured_at, 1),
            "ts": ts,
            "prompt": self._prompt,
            "text": text,
            "level": level,
        }
        if self._on_event is not None:
            try:
                self._on_event(ev)
            except Exception:  # noqa: BLE001
                LOG.debug("audio on_event 回调抛异常", exc_info=True)
        if level == "error":
            LOG.warning("audio ch%d 段%d 推理失败: %s",
                        ch.idx, ev["chunk_idx"], text[:120])
        return level != "error"     # True=成功 / False=失败 / None=作废

    def _next_id(self) -> int:
        with self._lock:
            self._event_id += 1
            return self._event_id

    def _notify_state(self) -> None:
        if self._on_state is not None:
            try:
                self._on_state(self._state)
            except Exception:  # noqa: BLE001
                LOG.debug("audio on_state 回调抛异常", exc_info=True)
