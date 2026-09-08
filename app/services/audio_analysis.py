"""音轨理解：视频音轨 → 6s 切片 → Qwen2.5-Omni-3B 推理 → 段落事件。

Omni-3B 单次 ``run_audio`` 上限 **6.0 秒**（wdnai/backends/omni.py 硬限制）；
超过会被 ``mel[:, :n_frame]`` 静默截断。>6s 音频必须先切片。

事件形状::

    {id, chunk_idx, start_s, end_s, ts, prompt, text, level}

WS 推 ``hello / event / progress / state`` 四种帧。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, Callable, List, Optional

from app.services.compute_registry import (
    get_registry,
    get_session_call_lock,
)

LOG = logging.getLogger("wdnwebui.audio_analysis")


# Omni-3B 单次 run_audio 上限：omni.py AUDIO_SHAPES 最大 n_frame=600
# × HOP_LENGTH=160 / SAMPLE_RATE=16000 = 6.0s。
CHUNK_SECONDS = 6.0
SAMPLE_RATE   = 16000
SAMPLE_WIDTH  = 2        # s16le
N_CHANNELS    = 1        # mono
MAX_NEW_TOKENS = 512


_STATE_IDLE       = "idle"
_STATE_EXTRACTING = "extracting"
_STATE_DONE       = "done"
_STATE_STOPPED    = "stopped"
_STATE_ERROR      = "error"


@dataclass
class AudioEvent:
    """前端事件 payload。``level='error'`` 表示该段 SDK / NPU 异常。"""
    id:        int
    chunk_idx: int
    start_s:   float
    end_s:     float
    ts:        float
    prompt:    str
    text:      str
    level:     Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


class AudioAnalysisService:
    """音轨理解 singleton（lazy）。NPU 互斥通过 ``get_session_call_lock()``。"""

    HISTORY_MAX = 200

    def __init__(self) -> None:
        self._events: deque[AudioEvent] = deque(maxlen=self.HISTORY_MAX)
        self._events_lock = threading.Lock()
        self._lock = threading.RLock()
        self._broadcasters: List[Callable[[dict], None]] = []
        self._broadcaster_lock = threading.Lock()
        self._stop_event: threading.Event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._state: str = _STATE_IDLE
        self._error: str = ""
        self._source: str = ""
        self._prompt: str = ""
        self._progress: tuple = (0, 0)
        # 用户从下拉框选中的 swcap_audio_understand 实例 model_id;
        # 空串 = auto-pick (与旧行为一致)。
        self._chosen_model_id: str = ""
        self._next_id: int = 1
        self._closed = False

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state":        self._state,
                "error":        self._error,
                "source":       self._source,
                "prompt":       self._prompt,
                "progress":     list(self._progress),
                "model_id":     self._chosen_model_id,
                "events_count": self._events_count_unlocked(),
                "available":    self._probe_capability_available(),
            }

    def _events_count_unlocked(self) -> int:
        return len(self._events)

    def list_events(self) -> List[dict]:
        with self._events_lock:
            return [ev.to_dict() for ev in self._events]

    def clear_events(self) -> None:
        with self._events_lock:
            self._events.clear()
        # 不重置 _next_id —— id 全局单调递增

    def _probe_capability_available(self) -> bool:
        try:
            get_registry().get_session("swcap_audio_understand")
            return True
        except RuntimeError:
            return False

    def register_broadcaster(self, fn: Callable[[dict], None]) -> None:
        with self._broadcaster_lock:
            self._broadcasters.append(fn)

    def unregister_broadcaster(self, fn: Callable[[dict], None]) -> None:
        with self._broadcaster_lock:
            try:
                self._broadcasters.remove(fn)
            except ValueError:
                pass

    def _broadcast(self, payload: dict) -> None:
        with self._broadcaster_lock:
            fns = list(self._broadcasters)
        for fn in fns:
            try:
                fn(payload)
            except Exception:
                LOG.debug("audio_analysis broadcaster raised", exc_info=True)

    def _set_state(self, state: str, error: str = "") -> None:
        with self._lock:
            self._state = state
            self._error = error
            snap = self.snapshot()
        self._broadcast({"type": "state", **snap})

    def start(self, source: str, prompt: str,
              model_id: Optional[str] = None) -> dict:
        """启动一次音频理解。失败 → ValueError / FileNotFoundError / RuntimeError
        由 API 层映射到 400 / 404 / 502。

        ``model_id``：前端下拉框选中的 ``swcap_audio_understand``
        实例 id。``None`` 或 ``""`` = 走 ``get_session`` 的本机优先
        auto-pick (默认行为,与旧版本一致)。
        """
        source = (source or "").strip()
        prompt = (prompt or "").strip()
        if not source:
            raise ValueError("source 不能为空")
        if not prompt:
            raise ValueError("prompt 不能为空")
        if model_id is not None and (not isinstance(model_id, str)
                                     or not model_id.strip()):
            raise ValueError(f"model_id 必须是字符串或 None: {model_id!r}")
        if not os.path.isfile(source):
            raise FileNotFoundError(f"视频源不存在: {source!r}")

        # 预检 capability —— 不等 worker 探测才报错
        # 显式 model_id 时校验该实例是否 running,失败抛 RuntimeError。
        if model_id:
            get_registry().get_session_for_model(
                "swcap_audio_understand", model_id.strip())
        else:
            get_registry().get_session("swcap_audio_understand")

        self._stop_event.set()
        old = self._thread
        if old is not None and old.is_alive():
            old.join(timeout=2.0)

        with self._lock:
            self._stop_event = threading.Event()
            self._source = source
            self._prompt = prompt
            self._progress = (0, 0)
            self._error = ""
            self._closed = False
            self._chosen_model_id = (model_id.strip()
                                    if model_id and isinstance(model_id, str)
                                    else "")
        self.clear_events()

        self._set_state(_STATE_EXTRACTING)
        self._thread = threading.Thread(
            target=self._worker_loop, name="audio-analysis-worker", daemon=True,
        )
        self._thread.start()
        return self.snapshot()

    def stop(self) -> dict:
        """用户中途停止 —— 只翻 state + 触发 worker 早退；session 不释放。"""
        self._stop_event.set()
        self._set_state(_STATE_STOPPED)
        return self.snapshot()

    def close(self) -> None:
        """app shutdown 钩子 —— stop + 等线程退出。"""
        self._closed = True
        self._stop_event.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)

    def _worker_loop(self) -> None:
        """PyAV 解码 → 6s 切片 → 逐段 omni 推理；单段失败 → 写 error event + 继续。"""
        try:
            chunks = self._decode_audio_track()
            if self._stop_event.is_set() or self._closed:
                return

            total_samples_per_chunk = int(CHUNK_SECONDS * SAMPLE_RATE)
            total_samples = len(chunks) // SAMPLE_WIDTH
            n_chunks = (total_samples + total_samples_per_chunk - 1) // total_samples_per_chunk
            if n_chunks == 0:
                n_chunks = 1

            with self._lock:
                self._progress = (0, n_chunks)
            self._broadcast({"type": "progress", "done": 0, "total": n_chunks})

            for idx in range(n_chunks):
                if self._stop_event.is_set() or self._closed:
                    return

                start_sample = idx * total_samples_per_chunk
                end_sample   = min(start_sample + total_samples_per_chunk, total_samples)
                chunk_pcm    = chunks[start_sample * SAMPLE_WIDTH:
                                       end_sample   * SAMPLE_WIDTH]
                start_s = start_sample / SAMPLE_RATE
                end_s   = end_sample   / SAMPLE_RATE

                ev = self._infer_one_chunk(idx, chunk_pcm, start_s, end_s)
                with self._events_lock:
                    self._events.append(ev)
                with self._lock:
                    self._progress = (idx + 1, n_chunks)
                self._broadcast({
                    "type": "event",
                    "event": ev.to_dict(),
                })
                self._broadcast({
                    "type": "progress",
                    "done": idx + 1, "total": n_chunks,
                })

            if self._stop_event.is_set() or self._closed:
                return
            self._set_state(_STATE_DONE)

        except Exception as e:                                # noqa: BLE001
            LOG.exception("audio_analysis worker failed")
            self._set_state(_STATE_ERROR,
                            error=f"{type(e).__name__}: {e}")

    def _decode_audio_track(self) -> bytes:
        """PyAV 解码 → 16kHz mono s16le PCM bytes。无音轨 / 空解码抛 RuntimeError。"""
        import av

        with self._lock:
            source = self._source

        container = av.open(source, timeout=(2.0, 5.0))
        try:
            audio_streams = list(container.streams.audio)
            if not audio_streams:
                raise RuntimeError("视频无音轨")

            resampler = av.AudioResampler(
                format="s16", layout="mono", rate=SAMPLE_RATE,
            )

            pcm_chunks: List[bytes] = []
            try:
                for frame in container.decode(audio=0):
                    if self._stop_event.is_set() or self._closed:
                        return b""
                    resampled = resampler.resample(frame)
                    items = resampled if isinstance(resampled, list) else [resampled]
                    for r in items:
                        if r is None:
                            continue
                        arr = r.to_ndarray()  # shape (1, N) 或 (N,) s16
                        pcm_chunks.append(arr.tobytes())
            finally:
                try:
                    resampler.close()
                except Exception:                            # noqa: BLE001
                    pass

            if not pcm_chunks:
                raise RuntimeError("音轨解码为空")
            return b"".join(pcm_chunks)
        finally:
            try:
                container.close()
            except Exception:                                # noqa: BLE001
                pass

    def _infer_one_chunk(self, idx: int, chunk_pcm: bytes,
                          start_s: float, end_s: float) -> AudioEvent:
        """单段 omni 推理。NPU 互斥走进程级 ``_llm_session_call_lock``。"""
        from wdnai import Attachment

        ts = time.time()
        ev_id = self._next_id_and_inc()

        try:
            # 用户从下拉框选了特定实例时,锁定到该 instance;
            # 否则走 ``get_session`` 的本机优先 auto-pick。
            if self._chosen_model_id:
                sess = get_registry().get_session_for_model(
                    "swcap_audio_understand", self._chosen_model_id)
            else:
                sess = get_registry().get_session("swcap_audio_understand")
        except RuntimeError as e:
            return AudioEvent(
                id=ev_id, chunk_idx=idx,
                start_s=round(start_s, 2), end_s=round(end_s, 2),
                ts=ts, prompt=self._prompt,
                text=f"[错误] 功能未启用: {e}",
                level="error",
            )

        # 隔离线程 + 新 event loop 跑 async respond()，避免与主进程
        # event loop 共享 thread pool（与 VFA 同模板）。
        coro = sess.respond(
            prompt=self._prompt,
            attachment=[Attachment(chunk_pcm, kind="audio")],
            sample_rate=SAMPLE_RATE,
            n_channels=N_CHANNELS,
            sample_width=SAMPLE_WIDTH,
            max_new_tokens=MAX_NEW_TOKENS,
        )
        result_holder: List[Any] = [None]
        error_holder: List[Optional[BaseException]] = [None]
        finished: List[bool] = [False]

        def _runner() -> None:
            loop = asyncio.new_event_loop()
            try:
                result_holder[0] = loop.run_until_complete(
                    asyncio.wait_for(coro, timeout=60.0)
                )
            except asyncio.TimeoutError:
                error_holder[0] = "timeout"
            except Exception as ex:                           # noqa: BLE001
                error_holder[0] = ex
            finally:
                try:
                    loop.close()
                except Exception:                            # noqa: BLE001
                    pass
                finished[0] = True

        try:
            with get_session_call_lock():
                sess.reset()
                t = threading.Thread(target=_runner, daemon=True,
                                     name=f"audio-infer-{idx}")
                t.start()
                t.join(timeout=65.0)
        except Exception as e:                                # noqa: BLE001
            LOG.warning("audio chunk %d failed (lock/reset): %s", idx, e)
            return AudioEvent(
                id=ev_id, chunk_idx=idx,
                start_s=round(start_s, 2), end_s=round(end_s, 2),
                ts=ts, prompt=self._prompt,
                text=f"[错误] {type(e).__name__}: {e}",
                level="error",
            )

        if not finished[0]:
            LOG.warning("audio chunk %d: SDK 超时（60s 内未返回）", idx)
            return AudioEvent(
                id=ev_id, chunk_idx=idx,
                start_s=round(start_s, 2), end_s=round(end_s, 2),
                ts=ts, prompt=self._prompt,
                text="[错误] 推理超时：60s 内 SDK 未响应",
                level="error",
            )
        if error_holder[0] == "timeout":
            return AudioEvent(
                id=ev_id, chunk_idx=idx,
                start_s=round(start_s, 2), end_s=round(end_s, 2),
                ts=ts, prompt=self._prompt,
                text="[错误] 推理超时",
                level="error",
            )
        if error_holder[0] is not None:
            err = error_holder[0]
            LOG.warning("audio chunk %d failed: %s", idx, err)
            return AudioEvent(
                id=ev_id, chunk_idx=idx,
                start_s=round(start_s, 2), end_s=round(end_s, 2),
                ts=ts, prompt=self._prompt,
                text=f"[错误] {type(err).__name__}: {err}",
                level="error",
            )

        result = result_holder[0]
        # 诊断：probe 多代理返回类型分支。AudioAnalysis 是纯文本输出，
        # 没有 schema / tool 概念，理论上只可能是 str / _ProxyResult /
        # LMOutput 之一。如果 result 是 None / 异常类型 / BaseModel
        # 子类，可能是 proxy 端 `_sync_call` 的 ``_worker_business_error``
        # 抛错路径漏掉、或者 SDK 走的 raw text fallback 之外的小概率
        # 路径。
        LOG.info(
            "audio chunk %d: post-respond result_type=%s "
            "has_text_attr=%s repr_len=%d",
            idx,
            type(result).__name__,
            isinstance(getattr(result, "text", None), str),
            len(repr(result)) if result is not None else -1,
        )
        # 兼容三种返回：str / _ProxyResult / LMOutput。先取 .text，None
        # 时回退到 str(result)（_ProxyResult.__str__ 也走 self.text）。
        text_attr = getattr(result, "text", None)
        if isinstance(text_attr, str):
            text_out = text_attr
        elif isinstance(result, str):
            text_out = result
        elif result is None:
            text_out = ""
        else:
            text_out = str(result)

        # 空文本 → 打诊断日志（区分"模型没生成" vs "SDK 返回类型识别错"）。
        if not text_out:
            LOG.warning(
                "audio chunk %d: empty model output (result_type=%s, "
                "raw_repr=%r)",
                idx, type(result).__name__, repr(result)[:200],
            )

        text_out = _sanitize(text_out)
        if not text_out:
            text_out = "(空)"

        return AudioEvent(
            id=ev_id, chunk_idx=idx,
            start_s=round(start_s, 2), end_s=round(end_s, 2),
            ts=ts, prompt=self._prompt, text=text_out,
        )

    def _next_id_and_inc(self) -> int:
        with self._lock:
            i = self._next_id
            self._next_id += 1
            return i


_singleton: Optional[AudioAnalysisService] = None
_singleton_lock = threading.Lock()


def get_service() -> AudioAnalysisService:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = AudioAnalysisService()
    return _singleton


# Text sanitization (mirror of detector._sanitize_event_text)
_SANITIZE_CHATML = re.compile(r"<\|[^|]*\|>")
_SANITIZE_CTRL   = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize(s: str) -> str:
    """去 chatml token + ASCII 控制字符。镜像 detector 清洗规则。"""
    if not s:
        return ""
    s = _SANITIZE_CHATML.sub("", s)
    s = _SANITIZE_CTRL.sub("", s)
    s = s.replace("\ufffd", "")
    return s.strip()