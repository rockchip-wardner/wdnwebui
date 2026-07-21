"""Qwen3-TTS session service for pyrkaiweb.

Drives TTS inference through the v2 ``LLMSession.respond`` API; the
service owns the per-call state machine, history, broadcaster, and
WAV file I/O. ``model_dir`` resolution is the same as the SDK's
TTSBackend (env var → ``$RKAI_MODELS_DIR/Qwen3-TTS`` →
``/userdata/models/Qwen3-TTS``); the layout is documented in
``rkai/models/README.md``.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

import numpy as np

LOG = logging.getLogger("tts_session")


# --------------------------------------------------------------------------- #
# Lazy rkai SDK loader
# --------------------------------------------------------------------------- #
_RKAI_CACHE: dict = {}


def _load_rkai():
    """Lazily import rkai + the SDK's TTS bits. Only first call hits
    ``import``; subsequent calls return the cached entry."""
    if "_ok" in _RKAI_CACHE:
        return (
            _RKAI_CACHE["_ok"],
            _RKAI_CACHE["_Attachment"],
            _RKAI_CACHE["_LLMSession"],
            _RKAI_CACHE["_SystemLanguageModel"],
            _RKAI_CACHE["_LMOutput"],
            _RKAI_CACHE["_Generable"],
            _RKAI_CACHE["_Field"],
            _RKAI_CACHE["_error"],
        )
    try:
        from rkai import (
            Attachment,
            Field,
            Generable,
            LMOutput,
            LLMSession,
            SystemLanguageModel,
        )
        _RKAI_CACHE.update(
            _ok=True,
            _Attachment=Attachment,
            _LLMSession=LLMSession,
            _SystemLanguageModel=SystemLanguageModel,
            _LMOutput=LMOutput,
            _Generable=Generable,
            _Field=Field,
            _error="",
        )
        return True, Attachment, LLMSession, SystemLanguageModel, LMOutput, Generable, Field, ""
    except Exception as e:
        _RKAI_CACHE.update(
            _ok=False,
            _Attachment=None,
            _LLMSession=None,
            _SystemLanguageModel=None,
            _LMOutput=None,
            _Generable=None,
            _Field=None,
            _error=str(e),
        )
        return False, None, None, None, None, None, None, str(e)


# --------------------------------------------------------------------------- #
# Structured output schema — inherits LMOutput so the SDK can populate
# ``attachment`` automatically with the synthesised WAV bytes.
# --------------------------------------------------------------------------- #
_TTS_RESULT_CLS = None


def _get_tts_result_cls():
    """Build the ``TTSResult`` schema lazily, once rkai is importable."""
    global _TTS_RESULT_CLS
    if _TTS_RESULT_CLS is not None:
        return _TTS_RESULT_CLS
    from typing import Optional
    _, _, _, _, LMOutput, Generable, Field, _ = _load_rkai()

    @Generable
    class TTSResult(LMOutput):
        """Qwen3-TTS output: ``text`` echoes the prompt, ``attachment``
        carries the 24 kHz mono float32 WAV bytes (SDK fills it in)."""
        text:        str = Field(description="The input prompt that was synthesised.")
        sample_rate: int = Field(default=24000)
        channels:    int = Field(default=1)

    _TTS_RESULT_CLS = TTSResult
    return _TTS_RESULT_CLS


def _wav_duration_seconds(wav_bytes: bytes) -> float:
    """Decode the audio duration from a RIFF/WAVE header."""
    if not wav_bytes or len(wav_bytes) < 44:
        return 0.0
    try:
        bio = io.BytesIO(wav_bytes)
        head = bio.read(12)
        if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            return 0.0
        sample_rate = 24000
        bits_per_sample = 32
        num_channels = 1
        data_size = 0
        while True:
            head = bio.read(8)
            if len(head) < 8:
                break
            cid, csize = head[:4], struct.unpack("<I", head[4:8])[0]
            cdata = bio.read(csize)
            if cid == b"fmt ":
                # fmt chunk: format_tag(2) channels(2) sample_rate(4) byte_rate(4) block_align(2) bits_per_sample(2)
                _fmt, num_channels, sample_rate, _br, _ba, bits_per_sample = struct.unpack(
                    "<HHIIHH", cdata[:16]
                )
            elif cid == b"data":
                data_size = csize
                break
            if csize % 2:
                bio.read(1)
        if sample_rate > 0 and num_channels > 0 and bits_per_sample > 0 and data_size > 0:
            bytes_per_frame = num_channels * bits_per_sample // 8
            return data_size / (sample_rate * bytes_per_frame)
    except Exception:
        pass
    return 0.0


# --------------------------------------------------------------------------- #
# Request / snapshot shapes
# --------------------------------------------------------------------------- #
@dataclass
class TTSRequest:
    """TTS run request. ``volume`` is 0.0–1.0 linear amplitude (matching
    the upstream ``AudioPlayer``); the web default of 0.1 keeps the
    played PCM at ~10% on hardware whose mixers default to 100%."""
    text:        str = ""
    language:    str = "auto"
    speaker:     str = ""            # SPK_ID entry (serena / ryan / …)
    ref_speaker: str = "girl_base"   # qwen3tts_speaker_embed.h entry
    instruct:    str = ""
    volume:      float = 0.1
    no_play:     bool = False
    output:      str = ""            # absolute path; auto-filled if empty
    non_streaming_mode: bool = False


# --------------------------------------------------------------------------- #
# Service singleton
# --------------------------------------------------------------------------- #
class TtsSession:
    """Single Qwen3-TTS session. State::

        idle → start() → loading → LLMSession ready → running
              → done / error       (terminal)
              → stop()  → stopped (terminal)

    Mirrors ``VideoSession`` / ``RkaiDetector`` so the JS side can
    treat all three uniformly (same snapshot fields, broadcaster API).
    """

    STATE_IDLE       = "idle"
    STATE_LOADING    = "loading"
    STATE_RUNNING    = "running"
    STATE_DONE       = "done"
    STATE_ERROR      = "error"
    STATE_STOPPED    = "stopped"
    STATE_UNAVAILABLE = "unavailable"

    HISTORY_MAX = 50

    def __init__(self, model_dir: Optional[Path] = None) -> None:
        # ``_lock`` is ``RLock`` because ``stop()`` returns ``snapshot()``
        # from inside its critical section; a plain ``Lock`` would
        # deadlock with the no-op ``stop()`` path on idle shutdown.
        self._lock = threading.RLock()
        self._state = self.STATE_IDLE
        self._error = ""
        self._init_error = ""
        self._request: Optional[TTSRequest] = None
        self._worker: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Active SDK session (lazy — created on first start())
        self._llm_session = None

        # Latest snapshot data surfaced to the WS
        self._frames = 0
        self._audio_sec = 0.0
        self._elapsed = 0.0
        self._output: Optional[Path] = None
        self._last_started = 0.0

        # Output WAV history
        self._history: List[dict] = []
        self._history_lock = threading.Lock()

        self._broadcast_lock = threading.Lock()
        self._broadcasters: List[Callable[[dict], None]] = []

        self._model_dir = self._resolve_model_dir(model_dir)

        # Probe once at boot so the UI can render the banner without
        # waiting for the first /start request.
        ok, _, _, _, _, _, _, err = _load_rkai()
        if not ok:
            self._state = self.STATE_UNAVAILABLE
            self._init_error = err
            LOG.warning("TTS unavailable: %s", err)

    @staticmethod
    def _resolve_model_dir(override: Optional[Path]) -> Path:
        if override is not None:
            return Path(override)
        env = os.environ.get("TTS_MODEL_DIR")
        if env:
            return Path(env)
        root = os.environ.get("RKAI_MODELS_DIR", "/userdata/models")
        return Path(root) / "Qwen3-TTS"

    @property
    def available(self) -> bool:
        ok, *_ = _load_rkai()
        return ok

    @property
    def model_dir(self) -> Path:
        return self._model_dir

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state":          self._state,
                "available":      self.available,
                "init_error":     self._init_error,
                "model_dir":      str(self._model_dir),
                "model_dir_exists": self._model_dir.is_dir(),
                "running":        self._state == self.STATE_RUNNING,
                "busy":           self._state in (self.STATE_LOADING, self.STATE_RUNNING),
                "frames":         self._frames,
                "audio_sec":      round(self._audio_sec, 3),
                "elapsed":        round(self._elapsed, 3),
                "output":         str(self._output) if self._output else "",
                "error":          self._error,
                "request":        self._request_to_dict(self._request) if self._request else None,
                "history_count":  len(self._history),
            }

    def history(self) -> List[dict]:
        with self._history_lock:
            return list(self._history)

    @staticmethod
    def _request_to_dict(req: Optional[TTSRequest]) -> Optional[dict]:
        if req is None:
            return None
        return {
            "text":        req.text,
            "language":    req.language,
            "speaker":     req.speaker,
            "ref_speaker": req.ref_speaker,
            "volume":      req.volume,
            "no_play":     req.no_play,
            "output":      req.output,
        }

    def register_broadcaster(self, fn: Callable[[dict], None]) -> None:
        with self._broadcast_lock:
            self._broadcasters.append(fn)

    def unregister_broadcaster(self, fn: Callable[[dict], None]) -> None:
        with self._broadcast_lock:
            try:
                self._broadcasters.remove(fn)
            except ValueError:
                pass

    def _broadcast(self, event: dict) -> None:
        with self._broadcast_lock:
            bs = list(self._broadcasters)
        for fn in bs:
            try:
                fn(event)
            except Exception as exc:           # noqa: BLE001
                LOG.debug("tts broadcaster raised: %s", exc)

    def _emit(self) -> None:
        self._broadcast(self.snapshot())

    # ------------------------------------------------------------------ #
    def start(self, req: TTSRequest) -> dict:
        if not self.available:
            raise RuntimeError(f"TTS 不可用: {self._init_error}")
        if not (req.text or "").strip():
            raise ValueError("text 不能为空")

        # Force the LMOutput subclass to materialise now so any import
        # error surfaces here, before the worker is spawned.
        _get_tts_result_cls()

        with self._lock:
            live_worker = self._worker is not None and self._worker.is_alive()
            if live_worker or self._state in (self.STATE_LOADING, self.STATE_RUNNING):
                raise RuntimeError(
                    "TTS 正在运行或正在清理，请稍候再试"
                )
            self._state = self.STATE_LOADING
            self._error = ""
            self._frames = 0
            self._audio_sec = 0.0
            self._elapsed = 0.0
            self._output = None
            self._request = req

        if not req.output:
            req.output = str(self._default_output_path())
        self._output = Path(req.output)
        self._output.parent.mkdir(parents=True, exist_ok=True)

        self._stop_event.clear()
        self._last_started = time.time()
        self._worker = threading.Thread(
            target=self._run_safe, args=(req,),
            name="tts-session", daemon=True,
        )
        self._worker.start()
        return self.snapshot()

    def _default_output_path(self) -> Path:
        here = Path(__file__).resolve().parent
        up = here / "uploads"
        up.mkdir(parents=True, exist_ok=True)
        return up / f"tts-{int(time.time())}.wav"

    def stop(self) -> dict:
        with self._lock:
            if self._state not in (self.STATE_LOADING, self.STATE_RUNNING):
                return self.snapshot()
            self._state = self.STATE_STOPPED
            self._error = ""
        self._stop_event.set()
        sess = self._llm_session
        if sess is not None:
            try:
                # Tell the underlying LLM session to stop. ``rknn3lite``
                # forwards ``session_stop()`` to the C runtime which
                # aborts the in-flight inference cleanly.
                inner = getattr(sess, "rknn_llm", None) or getattr(sess, "_backend", None)
                target = getattr(inner, "rknn_llm", inner)
                stop_fn = getattr(target, "session_stop", None)
                if callable(stop_fn):
                    stop_fn()
            except Exception as exc:           # noqa: BLE001
                LOG.debug("session_stop raised: %s", exc)
        return self.snapshot()

    def close(self) -> None:
        self.stop()
        worker = self._worker
        if worker is not None:
            worker.join(timeout=5)
        self._worker = None
        if self._llm_session is not None:
            try:
                self._llm_session.close()
            except Exception:                     # noqa: BLE001
                pass
            self._llm_session = None

    # ------------------------------------------------------------------ #
    def _ensure_session(self) -> None:
        if self._llm_session is not None:
            return
        ok, _, LLMSession, SystemLanguageModel, _, _, _, err = _load_rkai()
        if not ok:
            with self._lock:
                self._init_error = err
            raise RuntimeError(self._init_error)
        try:
            spec = SystemLanguageModel.qwen3_tts()
            spec.tts_model_dir = str(self._model_dir)
        except Exception as e:
            with self._lock:
                self._init_error = f"无法构建 qwen3_tts spec: {e}"
            raise
        try:
            sess = LLMSession(model=spec)
        except Exception as e:
            with self._lock:
                self._init_error = f"LLMSession 初始化失败: {e}"
            raise
        with self._lock:
            self._llm_session = sess
            self._init_error = ""

    def _infer_one(self, req: TTSRequest):
        """Single synchronous TTS inference: ``synthesize, return WAV bytes``.

        Wraps ``LLMSession.respond`` in a private asyncio loop because the
        C-level ``rknn3_session_run`` is blocking; the SDK is async-
        facing only for forwarding. ``no_play``/``stop_event``/``on_step``
        are forwarded so the SDK streams chunks live (when ``no_play``
        is False), the user can interrupt mid-run, and the WS UI gets
        incremental ``frames``/``audio_sec`` ticks.
        """
        try:
            self._ensure_session()
            sess = self._llm_session
            assert sess is not None
            ok, Attachment, *_ = _load_rkai()
            TTSResult = _get_tts_result_cls()

            sess.reset()

            async def _run() -> TTSResult:
                return await sess.respond(
                    attachment=[Attachment(req.text, kind="text")],
                    generating=TTSResult,
                    language=req.language,
                    speaker=req.speaker,
                    ref_speaker=req.ref_speaker,
                    instruct=req.instruct,
                    volume=req.volume,
                    no_play=req.no_play,
                    non_streaming_mode=req.non_streaming_mode,
                    stop_event=self._stop_event,
                    on_step=self._on_step,
                )
            return asyncio.run(_run())
        except Exception as e:  # pragma: no cover - depends on NPU
            raise RuntimeError(f"TTS 推理失败: {e}")

    def _on_step(self, n_frames: int, audio_sec: float) -> None:
        """SDK decoder-worker callback: tick the snapshot under ``_lock``
        and broadcast. Runs off the main thread."""
        with self._lock:
            self._frames = n_frames
            self._audio_sec = round(float(audio_sec), 3)
            self._elapsed = round(time.time() - self._last_started, 3)
        self._emit()

    # ------------------------------------------------------------------ #
    def _run_safe(self, req: TTSRequest) -> None:
        t0 = time.time()
        try:
            self._set_state(self.STATE_RUNNING, "synthesizing")

            # ``_infer_one`` already routes through the SDK with the
            # user's ``no_play`` intent — the SDK's internal
            # ``AudioPlayer`` streams chunks live; we don't re-spawn
            # aplay here (would replay the synthesis on top of the
            # live stream).
            result = self._infer_one(req)

            attachments = list(getattr(result, "attachment", []) or [])
            wav_bytes = attachments[0].data if attachments else b""

            with self._lock:
                self._audio_sec = _wav_duration_seconds(wav_bytes)
                self._elapsed = time.time() - t0
                self._output = Path(req.output)

            if wav_bytes:
                with open(self._output, "wb") as f:
                    f.write(wav_bytes)
                try:
                    size = self._output.stat().st_size
                except OSError:
                    size = 0
                with self._history_lock:
                    self._history.append({
                        "ts":        time.time(),
                        "text":      req.text,
                        "language":  req.language,
                        "speaker":   req.ref_speaker or req.speaker,
                        "output":    str(self._output),
                        "size":      size,
                        "audio_sec": round(self._audio_sec, 3),
                        "frames":    self._frames,
                    })
                    while len(self._history) > self.HISTORY_MAX:
                        self._history.pop(0)

            self._set_state(self.STATE_DONE, f"saved {self._output}")
        except Exception as exc:                   # noqa: BLE001
            LOG.exception("tts synthesis failed")
            self._set_state(self.STATE_ERROR, f"{type(exc).__name__}: {exc}")

    def _set_state(self, state: str, message: str = "") -> None:
        with self._lock:
            self._state = state
            if state == self.STATE_ERROR:
                self._error = message
        self._emit()


# --------------------------------------------------------------------------- #
# Singleton accessor
# --------------------------------------------------------------------------- #
_session: Optional[TtsSession] = None
_session_lock = threading.Lock()


def get_session() -> TtsSession:
    """Lazy singleton. The first call probes the rkai SDK; on non-RK
    dev boxes the returned session just reports ``state="unavailable"``.
    """
    global _session
    with _session_lock:
        if _session is None:
            _session = TtsSession()
        return _session
