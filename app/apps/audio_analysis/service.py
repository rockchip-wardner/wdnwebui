"""音轨理解：视频/流音轨 → 6s 切片 → Qwen2.5-Omni-3B 推理 → 段落事件。

支持 **1~32 路**：每一路是一个音频源（文件 / RTSP），会话按**严格 0→N-1
顺序轮转**，轮流把每一路的一段音频送去推理。取音频与送推理是**两级解耦**
的（见 :mod:`app.core.audio.multi_session` 与 :mod:`app.core.audio.source`）。

6s 上限：Omni-3B 单次 ``run_audio`` 上限 **6.0 秒** —— 那是 RKNN 音频编码器的
**编译期输入形状** ``[1, 128, 600]``（600 帧 × 160 / 16000 = 6.000s），不是
运行时可调的参数。超长音频会被 ``mel[:, :600]`` **静默截断**（不报错、不
告警），所以切片责任在调用方。

事件形状::

    {id, channel, chunk_idx, start_s, end_s, duration_s, captured_at,
     lag_s, ts, prompt, text, level}

``channel`` 是 0 基的通道号；``start_s``/``end_s`` 是该段在**源时间轴**上的
位置；``lag_s`` = 推理完成时刻 − 该段被**产出**的时刻（文件源 = 预取排队 +
推理；RTSP 源 ≈ 推理本身）。

WS 推 ``hello / event / progress / state`` 四种帧。
"""
from __future__ import annotations

import logging
import re
import threading
from collections import deque
from dataclasses import asdict, dataclass
from typing import Callable, List, Optional, Union

from app.core.audio.multi_session import MAX_CHANNELS, MultiAudioSession
from app.core.compute.registry import get_registry

LOG = logging.getLogger("wdnwebui.audio_analysis")


_STATE_IDLE       = "idle"
_STATE_EXTRACTING = "extracting"
_STATE_DONE       = "done"
_STATE_STOPPED    = "stopped"
_STATE_ERROR      = "error"

# MultiAudioSession 的状态 → 本服务的状态。
_SESSION_STATE_MAP = {
    "idle": _STATE_IDLE,
    "running": _STATE_EXTRACTING,
    "done": _STATE_DONE,
    "stopped": _STATE_STOPPED,
    "error": _STATE_ERROR,
}


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
    # ---- 多路新增（旧调用方忽略即可）----
    channel:     int = 0
    duration_s:  float = 0.0
    captured_at: float = 0.0
    lag_s:       float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


class AudioAnalysisService:
    """音轨理解 singleton（lazy）。多路会话由 :class:`MultiAudioSession` 承载。"""

    HISTORY_MAX = 200

    def __init__(self) -> None:
        self._events: deque[AudioEvent] = deque(maxlen=self.HISTORY_MAX)
        self._events_lock = threading.Lock()
        self._lock = threading.RLock()
        self._broadcasters: List[Callable[[dict], None]] = []
        self._broadcaster_lock = threading.Lock()

        self._state: str = _STATE_IDLE
        self._error: str = ""
        self._sources: List[str] = []
        self._prompt: str = ""
        # 用户从下拉框选中的 swcap_audio_understand 实例 model_id;
        # 空串 = auto-pick (与旧行为一致)。
        self._chosen_model_id: str = ""
        self._closed = False

        self._session = MultiAudioSession(
            on_event=self._on_session_event,
            on_state=self._on_session_state,
        )

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict:
        sess = self._session.snapshot()
        with self._lock:
            done = sum(c["chunks_done"] for c in sess["per_channel"])
            total = sum(c["total_chunks"] or 0 for c in sess["per_channel"])
            return {
                "state":        self._state,
                "error":        self._error,
                # 旧字段：单路时的那个 source。多路下取第一路，向后兼容。
                "source":       self._sources[0] if self._sources else "",
                "sources":      list(self._sources),
                "prompt":       self._prompt,
                # 旧字段：(已完成段数, 总段数)。多路下是**合计**。
                "progress":     [done, total or done],
                "model_id":     self._chosen_model_id,
                "events_count": len(self._events),
                "available":    self._probe_capability_available(),
                # ---- 多路新增 ----
                "channels":     sess["channels"],
                "round":        sess["round"],
                "current":      sess["current"],
                "inferred":     sess["inferred"],
                "errors":       sess["errors"],
                "per_channel":  sess["per_channel"],
            }

    def list_events(self) -> List[dict]:
        with self._events_lock:
            return [ev.to_dict() for ev in self._events]

    def clear_events(self) -> None:
        with self._events_lock:
            self._events.clear()

    def _probe_capability_available(self) -> bool:
        """能力是否可用。

        **用 list_sessions 而不是 get_session**：get_session 每次都会打一条
        ``LOG.info("get_session(...)")``，而 snapshot() 在多路下每次 progress
        帧都要跑（160 段 = 160 条日志噪音）。list_sessions 是纯查询。
        """
        try:
            return bool(get_registry().list_sessions(
                "swcap_audio_understand", include_remote=True))
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------ #
    # 广播
    # ------------------------------------------------------------------ #
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

    # ------------------------------------------------------------------ #
    # 会话回调
    # ------------------------------------------------------------------ #
    def _on_session_event(self, raw: dict) -> None:
        """MultiAudioSession 每产出一段就调这里 → 转成事件 + 广播。"""
        text = _sanitize(raw.get("text") or "")
        if not text and not raw.get("level"):
            text = "(空)"
        ev = AudioEvent(
            id=raw["id"],
            chunk_idx=raw["chunk_idx"],
            start_s=raw["start_s"],
            end_s=raw["end_s"],
            ts=raw["ts"],
            prompt=raw.get("prompt", ""),
            text=text,
            level=raw.get("level"),
            channel=raw.get("channel", 0),
            duration_s=raw.get("duration_s", 0.0),
            captured_at=raw.get("captured_at", 0.0),
            lag_s=raw.get("lag_s", 0.0),
        )
        with self._events_lock:
            self._events.append(ev)
        self._broadcast({"type": "event", "event": ev.to_dict()})
        self._broadcast({"type": "progress", **self._progress_payload()})

    def _on_session_state(self, sess_state: str) -> None:
        """会话状态变化（全部放完 → done；连续失败熔断 → error）。"""
        mapped = _SESSION_STATE_MAP.get(sess_state, sess_state)
        if mapped == _STATE_EXTRACTING:
            return
        # 会话自己的错误文案要透出来（熔断时的「Omni worker 可能已失联」），
        # 否则前端只看到「错误: 」后面是空的。
        err = ""
        if mapped == _STATE_ERROR:
            err = (self._session.snapshot().get("error") or "")
        self._set_state(mapped, err)

    def _progress_payload(self) -> dict:
        sess = self._session.snapshot()
        done = sum(c["chunks_done"] for c in sess["per_channel"])
        total = sum(c["total_chunks"] or 0 for c in sess["per_channel"])
        return {
            "done": done,
            "total": total or done,
            "current": sess["current"],
            "round": sess["round"],
            # 路数也得带上：state 帧可能在 open 之前就发过（channels=0），
            # 只靠继承上一份快照会把 0 一直传下去。
            "channels": sess["channels"],
        }

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self, source: Union[str, List[str]], prompt: str,
              model_id: Optional[str] = None) -> dict:
        """启动音频理解（1~32 路）。

        ``source`` 传字符串 = 单路（向后兼容）；传列表 = 多路。
        失败 → ValueError / FileNotFoundError / RuntimeError 由 API 层映射到
        400 / 404 / 502。

        ``model_id``：前端下拉框选中的 ``swcap_audio_understand`` 实例 id。
        ``None`` 或 ``""`` = 走 ``get_session`` 的本机优先 auto-pick。
        """
        srcs = ([source] if isinstance(source, str)
                else list(source or []))
        srcs = [(s or "").strip() for s in srcs]
        srcs = [s for s in srcs if s]
        prompt = (prompt or "").strip()
        if not srcs:
            raise ValueError("source 不能为空")
        if len(srcs) > MAX_CHANNELS:
            raise ValueError(f"最多 {MAX_CHANNELS} 路，当前 {len(srcs)} 路")
        if not prompt:
            raise ValueError("prompt 不能为空")
        if model_id is not None and (not isinstance(model_id, str)
                                     or not model_id.strip()):
            raise ValueError(f"model_id 必须是字符串或 None: {model_id!r}")

        # 预检 capability —— 不等 worker 探测才报错
        if model_id:
            get_registry().get_session_for_model(
                "swcap_audio_understand", model_id.strip())
        else:
            get_registry().get_session("swcap_audio_understand")

        # 停掉上一轮（旧的单路 worker 也一并收掉）
        self._stop_session()

        with self._lock:
            self._sources = srcs
            self._prompt = prompt
            self._error = ""
            self._closed = False
            self._chosen_model_id = (model_id.strip()
                                     if model_id and isinstance(model_id, str)
                                     else "")
        self.clear_events()

        self._set_state(_STATE_EXTRACTING)
        try:
            self._session.open(srcs, prompt, self._chosen_model_id)
        except Exception as exc:  # noqa: BLE001
            # open() 失败（源不存在 / 超过 32 路 / 会话已占用）→ 状态回 error，
            # 异常继续往外抛由 API 层映射状态码。
            self._set_state(_STATE_ERROR, error=f"{type(exc).__name__}: {exc}")
            raise
        # **open 之后补一次 state 广播**：上面那次是在 open 之前发的，那时
        # 会话还没有路，channels=0。WS 客户端拿到的 state 帧比 POST 响应更
        # 晚到（要再走一次网络），会把 POST 响应里正确的 channels 覆盖成 0
        # —— 状态栏就永远显示不出「N 路 / 第几轮 / 当前通道」。
        self._set_state(_STATE_EXTRACTING)
        self._broadcast({"type": "progress", **self._progress_payload()})
        return self.snapshot()

    def _stop_session(self) -> None:
        try:
            self._session.close()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("停止音频会话失败: %s", exc)

    def stop(self) -> dict:
        """用户中途停止 —— 停轮转 + 停所有采集线程；session 不释放。"""
        self._stop_session()
        self._set_state(_STATE_STOPPED)
        return self.snapshot()

    def close(self) -> None:
        """app shutdown 钩子 —— stop + 等线程退出。"""
        self._closed = True
        self._stop_session()
        self._set_state(_STATE_STOPPED)


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
