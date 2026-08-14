"""Qwen3-TTS session service for wdnwebui.

Drives TTS inference through the v2 ``LLMSession.respond`` API; the
service owns the per-call state machine, history, broadcaster, and
WAV file I/O. ``model_dir`` resolution is the same as the SDK's
TTSBackend (env var → ``$WDNAI_MODELS_DIR/Qwen3-TTS`` →
``/userdata/models/Qwen3-TTS``); the layout is documented in
``wdnai/models/README.md``.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import struct
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

import numpy as np


# --------------------------------------------------------------------------- #
# Playback target selector
# --------------------------------------------------------------------------- #
PLAY_DEVICE = "device"   # SDK live-stream via internal AudioPlayer (orig)
PLAY_BROWSER = "browser"  # SDK silent; server-side WAV is served to <audio>
PLAY_BOTH = "both"       # SDK live-stream + WAV served to browser in parallel
PLAY_TARGETS = (PLAY_DEVICE, PLAY_BROWSER, PLAY_BOTH)

LOG = logging.getLogger("tts_session")


# Plain-print so diagnostics land in uvicorn's stdout regardless of
# how the root logger is configured.
def _tts_log(msg: str) -> None:
    print(f"[tts_session] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Non-blocking aplay subprocess
# --------------------------------------------------------------------------- #
class AplayWriter:
    """Subprocess wrapper that pipes raw float32 PCM into aplay
    without blocking the caller.

    Why this exists: the SDK's ``AudioPlayer.play_chunk`` does a
    synchronous ``self._proc.stdin.write(chunk.tobytes())``. With
    ``play_to="both"`` the SDK runs ``audio_player.play_chunk(audio)``
    and ``on_audio_chunk(audio, sr)`` on the same decoder worker
    thread; a 64 KB-OS-pipe filling up blocks the worker ~3 s for a
    4 s chunk, so ``on_audio_chunk`` (the browser's WS feed) trails
    the device by ~3 s. By running aplay ourselves from a dedicated
    thread, the SDK worker never blocks on aplay."""

    def __init__(self, sample_rate: int = 24000, channels: int = 1,
                 volume: float = 0.5, device: str = "plughw:1,0") -> None:
        self._volume = float(volume)
        self._alive = True
        cmd = ["aplay", "-t", "raw", "-f", "FLOAT_LE",
               "-r", str(sample_rate), "-c", str(channels)]
        if device:
            cmd += ["-D", device]
        cmd.append("-")
        try:
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            _tts_log(f"aplay-writer: pid={self._proc.pid} cmd={' '.join(cmd)}")
        except Exception as exc:
            LOG.warning("aplay-writer: failed to start aplay: %s", exc)
            self._alive = False
            self._proc = None
            return
        self._buf = bytearray()
        self._cond = threading.Condition(threading.Lock())
        self._thread = threading.Thread(target=self._drain,
                                        name="aplay-writer", daemon=True)
        self._thread.start()

    def write(self, audio) -> None:
        if not self._alive or self._proc is None or self._proc.stdin is None:
            return
        chunk = np.clip(np.asarray(audio, dtype=np.float32) * self._volume,
                        -1.0, 1.0)
        with self._cond:
            if not self._alive:
                return
            self._buf.extend(chunk.tobytes())
            self._cond.notify()

    def _drain(self) -> None:
        while True:
            with self._cond:
                while self._alive and not self._buf:
                    self._cond.wait()
                if not self._alive and not self._buf:
                    break
                # Snap off in 256 KB chunks so the lock doesn't hold
                # an arbitrary-long memcpy.
                data = bytes(self._buf[:256 * 1024])
                del self._buf[:256 * 1024]
            if self._proc is None or self._proc.stdin is None:
                self._alive = False
                break
            try:
                self._proc.stdin.write(data)
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError, EOFError):
                # ``ValueError`` / ``EOFError`` happen when ``close()``
                # closed stdin while we were mid-write.
                self._alive = False
                break

    def close(self, drain_timeout: float = 15.0) -> None:
        """Stop accepting chunks; tear down in the right order so
        the drain thread never sees a half-closed subprocess."""
        with self._cond:
            self._alive = False
            self._cond.notify_all()
        # Wait for the drain thread to exit on its own. If it's
        # blocked on stdin.write, the close below closes stdin which
        # unblocks it via BrokenPipeError; we handle that in _drain.
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=drain_timeout)
        if self._proc is None:
            return
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=drain_timeout)
        except subprocess.TimeoutExpired:
            try:
                self._proc.kill()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Lazy wdnai SDK loader
# --------------------------------------------------------------------------- #
_WDNAI_CACHE: dict = {}


def _load_wdnai():
    """Lazily import wdnai + the SDK's TTS bits. Only first call hits
    ``import``; subsequent calls return the cached entry."""
    if "_ok" in _WDNAI_CACHE:
        return (
            _WDNAI_CACHE["_ok"],
            _WDNAI_CACHE["_Attachment"],
            _WDNAI_CACHE["_LLMSession"],
            _WDNAI_CACHE["_SystemLanguageModel"],
            _WDNAI_CACHE["_LMOutput"],
            _WDNAI_CACHE["_Generable"],
            _WDNAI_CACHE["_Field"],
            _WDNAI_CACHE["_error"],
        )
    try:
        from wdnai import (
            Attachment,
            Field,
            Generable,
            LMOutput,
            LLMSession,
            SystemLanguageModel,
        )
        _WDNAI_CACHE.update(
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
        _WDNAI_CACHE.update(
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
    """Build the ``TTSResult`` schema lazily, once wdnai is importable."""
    global _TTS_RESULT_CLS
    if _TTS_RESULT_CLS is not None:
        return _TTS_RESULT_CLS
    from typing import Optional
    _, _, _, _, LMOutput, Generable, Field, _ = _load_wdnai()

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
    played PCM at ~10% on hardware whose mixers default to 100%.

    ``play_to`` selects where the synthesised audio is heard:

    * ``"device"``  — SDK streams chunks live through the host's sound
      card (the original behaviour; requires ALSA + a working
      ``AudioPlayer`` sink).
    * ``"browser"`` — SDK runs silently (``no_play=True``); the WAV
      file written to ``output`` is served via ``GET /api/tts/output``
      and the web page plays it through a hidden ``<audio>`` element.
      When a WS client is subscribed to ``audio`` it additionally
      streams 24 kHz mono float32 PCM chunks live over the WS.
    * ``"both"``    — SDK streams to the device **and** the chunks
      are mirrored to the browser over WS **and** the final WAV is
      served for re-play via ``<audio>``.
    """
    text:        str = ""
    language:    str = "auto"
    speaker:     str = ""            # SPK_ID entry (serena / ryan / …)
    ref_speaker: str = "girl_base"   # qwen3tts_speaker_embed.h entry
    instruct:    str = ""
    volume:      float = 0.1
    no_play:     bool = False
    play_to:     str = PLAY_DEVICE   # one of PLAY_DEVICE / PLAY_BROWSER / PLAY_BOTH
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

    Mirrors ``VideoSession`` / ``WdnaiDetector`` so the JS side can
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

        # Active SDK session is owned by ComputeRegistry (算力配置) — see
        # ``app/services/compute_registry.py``. TtsSession no longer
        # builds an LLMSession itself; ``_infer_one`` looks it up via
        # ``get_registry().get_session("swcap_audio_tts")`` and surfaces
        # "功能未启用: swcap_audio_tts" if no instance is running.

        # Latest snapshot data surfaced to the WS
        self._frames = 0
        self._audio_sec = 0.0
        self._elapsed = 0.0
        self._output: Optional[Path] = None
        self._last_started = 0.0

        # Output WAV history
        self._history: List[dict] = []
        self._history_lock = threading.Lock()

        # Independent aplay subprocess used only for ``play_to=both``
        # so the SDK's decoder worker isn't blocked by aplay's stdin
        # pipe (which used to delay the browser-side WS feed by ~3 s
        # per chunk). ``None`` otherwise.
        self._aplay_writer: Optional[AplayWriter] = None

        self._broadcast_lock = threading.Lock()
        self._broadcasters: List[Callable[[dict], None]] = []

        # PCM-chunk subscribership (used when ``play_to`` involves the
        # browser). See ``subscribe_audio`` / ``_on_audio_chunk``.
        self._audio_lock = threading.Lock()
        self._audio_subs: dict = {}

        self._model_dir = self._resolve_model_dir(model_dir)

        # Probe once at boot so the UI can render the banner without
        # waiting for the first /start request.
        ok, _, _, _, _, _, _, err = _load_wdnai()
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
        root = os.environ.get("WDNAI_MODELS_DIR", "/userdata/models")
        return Path(root) / "Qwen3-TTS"

    @property
    def available(self) -> bool:
        ok, *_ = _load_wdnai()
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
                "output_url":     self._output_url_for(self._output) if self._output else "",
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
            "play_to":     req.play_to,
            "output":      req.output,
        }

    @staticmethod
    def _output_url_for(path: Optional[Path]) -> str:
        """Return a stable URL the browser can use to fetch the saved WAV.

        The endpoint lives at ``/api/tts/output`` (see ``app/api/tts.py``)
        and is parameterised by the on-disk file path so the same snapshot
        shape (an absolute ``output`` + a relative ``output_url``) survives
        web reloads and history lookups. Path is URL-encoded (``/`` left
        intact) so future upload dirs with spaces in their name keep working."""
        if path is None:
            return ""
        return "/api/tts/output?path=" + urllib.parse.quote(
            str(path), safe="/")

    def register_broadcaster(self, fn: Callable[[dict], None]) -> None:
        with self._broadcast_lock:
            self._broadcasters.append(fn)

    def unregister_broadcaster(self, fn: Callable[[dict], None]) -> None:
        with self._broadcast_lock:
            try:
                self._broadcasters.remove(fn)
            except ValueError:
                pass

    # ------------------------------------------------------------------ #
    # Audio-chunk subscribership
    # ------------------------------------------------------------------ #
    # WS clients that want PCM chunks subscribe here; the SDK's
    # ``on_audio_chunk`` callback (called from the decoder worker
    # thread) pushes the float32 ndarray into ``_audio_queue`` and we
    # hand it out per subscriber on the asyncio side. Per-subscriber
    # byte queues are bounded; if a slow client doesn't keep up we
    # drop the oldest chunk (latest-only, mirroring the rest of the
    # service's "drop oldest" policy for freshest data).
    def subscribe_audio(self, sub_id: str, loop: asyncio.AbstractEventLoop) -> "asyncio.Queue[bytes]":
        """Register a subscriber; return an asyncio Queue that will
        receive raw float32 LE bytes plus a sentinel ``None`` on EOF."""
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=4)
        with self._audio_lock:
            self._audio_subs[sub_id] = (loop, q)
            return q

    def unsubscribe_audio(self, sub_id: str) -> None:
        with self._audio_lock:
            self._audio_subs.pop(sub_id, None)

    def _broadcast(self, event: dict) -> None:
        with self._broadcast_lock:
            bs = list(self._broadcasters)
        for fn in bs:
            try:
                fn(event)
            except Exception as exc:           # noqa: BLE001
                LOG.debug("tts broadcaster raised: %s", exc)

    def _on_audio_chunk(self, audio, sample_rate: int) -> None:
        """SDK callback: a new float32 mono PCM chunk is ready.

        Runs on the SDK's decoder thread. Convert to little-endian
        bytes once and push to every subscriber's asyncio.Queue via
        ``call_soon_threadsafe``; each subscriber's WS handler then
        forwards the bytes as a binary frame.
        """
        if audio is None or audio.size == 0:
            return
        # Per-chunk header: 8-byte (sample_rate, channels, sample_count)
        # little-endian. Browser ignores it for now (it uses the
        # constructor-passed SR) but it's there for future re-emit.
        try:
            chunk = audio.astype("<f4", copy=False).tobytes()
            sr = int(sample_rate)
            n = int(audio.size)
            header = struct.pack("<III", sr, 1, n) + chunk
        except Exception as exc:               # noqa: BLE001
            LOG.debug("audio chunk encode failed: %s", exc)
            return

        with self._audio_lock:
            subs = list(self._audio_subs.items())
        for _sub_id, (loop, q) in subs:
            try:
                loop.call_soon_threadsafe(self._enqueue_chunk, q, header)
            except Exception as exc:           # noqa: BLE001
                LOG.debug("audio schedule failed: %s", exc)

        # Mirror to the local aplay subprocess when ``play_to=both``.
        # The writer's drain thread ships bytes to aplay's stdin
        # asynchronously, so the SDK worker never blocks here.
        if self._aplay_writer is not None:
            try:
                self._aplay_writer.write(audio)
            except Exception as exc:           # noqa: BLE001
                LOG.debug("aplay-writer write failed: %s", exc)

    @staticmethod
    def _enqueue_chunk(q: asyncio.Queue, data: bytes) -> None:
        """Runs on the subscriber's asyncio loop. Drop-oldest if the
        queue is full so a slow client doesn't backpressure the SDK."""
        while not q.empty():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            pass

    def _finish_audio_stream(self) -> None:
        """Send a ``None`` sentinel to every subscriber so the WS
        handler can close the audio portion of the stream cleanly."""
        with self._audio_lock:
            subs = list(self._audio_subs.items())
        for _sub_id, (loop, q) in subs:
            try:
                loop.call_soon_threadsafe(self._enqueue_chunk, q, b"EOF")
            except Exception as exc:           # noqa: BLE001
                LOG.debug("audio EOF schedule failed: %s", exc)

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

        # For ``play_to=both`` we run our own aplay subprocess via
        # ``AplayWriter`` so the SDK's worker thread never blocks on
        # aplay's 64 KB stdin pipe. The SDK still receives each
        # chunk via ``on_audio_chunk`` and forwards it both to the
        # browser (immediate) and to the writer (queued to aplay).
        # For ``device`` we let the SDK own the aplay path; for
        # ``browser`` we don't touch aplay at all.
        if self._aplay_writer is not None:
            try:
                self._aplay_writer.close()
            except Exception:
                pass
        self._aplay_writer = None
        if req.play_to == PLAY_BOTH:
            try:
                self._aplay_writer = AplayWriter(volume=req.volume)
            except Exception as exc:
                LOG.warning("aplay-writer: could not be created: %s", exc)

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
        # Tell the underlying LLM session to stop. ``rknn3lite``
        # forwards ``session_stop()`` to the C runtime which aborts
        # the in-flight inference cleanly. The session is owned by
        # ComputeRegistry (算力配置), so we look it up by capability.
        try:
            from app.services.compute_registry import get_registry
            sess = get_registry().get_session("swcap_audio_tts")
        except RuntimeError:
            sess = None
        if sess is not None:
            try:
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
        # LLMSession 由 ComputeRegistry 持有并负责释放 —— 这里只停本服务的 worker。

    def _infer_one(self, req: TTSRequest):
        """Single synchronous TTS inference: ``synthesize, return WAV bytes``.

        Wraps ``LLMSession.respond`` in a private asyncio loop because the
        C-level ``rknn3_session_run`` is blocking; the SDK is async-
        facing only for forwarding. ``no_play``/``stop_event``/``on_step``
        are forwarded so the SDK streams chunks live (when ``no_play``
        is False), the user can interrupt mid-run, and the WS UI gets
        incremental ``frames``/``audio_sec`` ticks.

        ``req.play_to`` (``device`` / ``browser`` / ``both``) is mapped
        onto the SDK's binary ``no_play`` flag: device-only and both
        keep device playback on (``no_play=False``); browser-only skips
        it so the SDK doesn't touch the host sound card at all
        (``no_play=True``).

        LLMSession 由 ComputeRegistry 统一管理（"算力配置"页面启用）。
        找不到 swcap_audio_tts capability → RuntimeError("功能未启用: swcap_audio_tts")。
        """
        try:
            from app.services.compute_registry import get_registry
            sess = get_registry().get_session("swcap_audio_tts")
        except RuntimeError as e:
            self._init_error = str(e)
            raise
        try:
            ok, Attachment, *_ = _load_wdnai()
            TTSResult = _get_tts_result_cls()

            sess.reset()
            # ``play_to=both`` runs our own aplay via ``AplayWriter``,
            # so we tell the SDK to skip its own (blocking) aplay
            # path. ``play_to=device`` keeps the SDK's original
            # behaviour; ``play_to=browser`` already needs ``no_play``.
            no_play = req.play_to in (PLAY_BROWSER, PLAY_BOTH)

            async def _run() -> TTSResult:
                return await sess.respond(
                    attachment=[Attachment(req.text, kind="text")],
                    generating=TTSResult,
                    language=req.language,
                    speaker=req.speaker,
                    ref_speaker=req.ref_speaker,
                    instruct=req.instruct,
                    volume=req.volume,
                    no_play=no_play,
                    non_streaming_mode=req.non_streaming_mode,
                    stop_event=self._stop_event,
                    on_step=self._on_step,
                    on_audio_chunk=self._on_audio_chunk,
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
            try:
                result = self._infer_one(req)
            finally:
                # Always signal EOF to browser-side subscribers so the
                # AudioContext stops scheduling further chunks even if
                # the inference raised.
                self._finish_audio_stream()
                # Drain the local aplay writer so the device finishes
                # playing whatever's still in its stdin pipe. We do
                # this after ``_finish_audio_stream`` so the last
                # chunk has already been enqueued.
                if self._aplay_writer is not None:
                    try:
                        self._aplay_writer.close()
                    except Exception as exc:           # noqa: BLE001
                        LOG.debug("aplay-writer close failed: %s", exc)
                    self._aplay_writer = None

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
                        "ts":         time.time(),
                        "text":       req.text,
                        "language":   req.language,
                        "speaker":    req.ref_speaker or req.speaker,
                        "output":     str(self._output),
                        "output_url": self._output_url_for(self._output),
                        "play_to":    req.play_to,
                        "size":       size,
                        "audio_sec":  round(self._audio_sec, 3),
                        "frames":     self._frames,
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
    """Lazy singleton. The first call probes the wdnai SDK; on non-RK
    dev boxes the returned session just reports ``state="unavailable"``.
    """
    global _session
    with _session_lock:
        if _session is None:
            _session = TtsSession()
        return _session
