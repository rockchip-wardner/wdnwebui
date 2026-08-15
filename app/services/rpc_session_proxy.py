"""rpc_session_proxy — wdnai.LLMSession drop-in backed by wdndds cshim RPC.

Each compute instance lives in its own compute_worker subprocess; the
LLMSession is held by that worker. The main wdnwebui process holds
a wdndds ``RpcClient`` and uses this proxy to forward calls.

Wire: standard Pub-Sub with RELIABLE QoS, two topics per service
  - Request topic = ``wdndds/rpc/req/<model_id>``
  - Reply topic   = ``wdndds/rpc/reply/<model_id>``

The cshim wraps each request/reply in a ``{wdndds_reply_suffix,
data}`` envelope so the client can match replies to in-flight calls.

The proxy preserves the LLMSession surface that detector / tts /
text_chat / vfa / video_content_search use:

  * ``session.reset()``              → ``RpcClient.call({"action": "reset"})``
  * ``await session.respond(...)``   → ``RpcClient.call({"action": "infer", ...})``
  * ``session.embed_text(text)``     → ``RpcClient.call({"action": "embed_text", ...})``
  * ``session.embed_video(frames)``   → ``RpcClient.call({"action": "embed_video", ...})``
"""
from __future__ import annotations

import asyncio
import base64
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

LOG = logging.getLogger("rpc_session_proxy")


# Lazy import for cv2 (only used by image-encoding path).
_cv2_lock = __import__("threading").Lock()


def _bgr_ndarray_to_b64(arr, quality: int = 85) -> str:
    """Encode a BGR ndarray as JPEG and base64."""
    import cv2  # lazy
    ok, buf = cv2.imencode(".jpg", arr, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RuntimeError("cv2.imencode failed")
    return base64.b64encode(buf.tobytes()).decode("ascii")


class _AttachmentProxy:
    """Adapter carrying the same attribute surface as
    :class:`wdnai.Attachment` — ``kind`` / ``mime_type`` / ``data``
    plus the small accessors ``tts_session.py`` reads.

    Kept minimal so we don't have to import the real
    ``wdnai.Attachment`` (which would pull in RKNN3 native libs
    eagerly and segfault on a non-RK dev box — see
    ``WdnaiDetector._load_wdnai`` constraint). The wdnai Attachment
    is just a dataclass; the only method tts_session uses is
    ``.data`` / ``.kind`` / ``.mime_type``.
    """

    __slots__ = ("kind", "mime_type", "data")

    def __init__(self, *, kind: str = "", mime_type: str = "", data=bytes()):
        self.kind = kind
        self.mime_type = mime_type
        self.data = data

    def __repr__(self) -> str:
        return (
            f"_AttachmentProxy(kind={self.kind!r}, "
            f"mime_type={self.mime_type!r}, "
            f"data=<{len(bytes(self.data))} bytes>)"
        )


class _ProxyResult:
    """Adapter object: detector/tts/vfa code reads ``result.text`` etc.;
    this mirrors the fields that wdnai returns from ``sess.respond``.

    ``attachment`` is a list of ``wdnai.Attachment``-shaped objects
    (``kind`` / ``mime_type`` / ``data`` attributes) — populated only
    when the worker's ``infer_handler`` forwarded binary payloads
    (TTS WAV bytes today; future VLM-decoded images may also flow
    through this slot).
    """

    def __init__(self, *, text=None, level=None, tool_call=None,
                 bbox=None, crop_b64=None, attachment=None, raw=None):
        self.text = text
        self.level = level
        self.tool_call = tool_call
        self.bbox = bbox
        self.crop_b64 = crop_b64
        self.attachment = attachment or []
        self._raw = raw or {}

    @property
    def desc(self) -> Optional[str]:
        return self.text

    def __repr__(self) -> str:
        return (
            f"_ProxyResult(text={self.text!r}, level={self.level!r}, "
            f"tool_call={self.tool_call!r}, "
            f"attachment={len(self.attachment)} items)"
        )

    def __str__(self) -> str:
        return self.text or ""


class _RpcSessionProxy:
    """Drop-in for ``wdnai.LLMSession`` that talks to a compute_worker
    subprocess via wdndds cshim RPC.

    Construct via :func:`make_rpc_session_proxy(rpc_client, model_id)`.
    """

    def __init__(self, rpc_client, model_id: str):
        self._client = rpc_client
        self._model_id = model_id
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"rpc-proxy-{model_id}",
        )

    @property
    def model_id(self) -> str:
        return self._model_id

    def reset(self) -> None:
        try:
            r = self._client.call({"action": "reset"}, timeout_ms=10_000)
            if not r.get("ok"):
                LOG.warning("reset on %s returned: %s", self._model_id, r)
        except Exception as e:  # noqa: BLE001
            LOG.warning("reset on %s raised: %s", self._model_id, e)

    def close(self) -> None:
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    async def respond(self, prompt=None, attachment=None,
                      generating=None, tools=None, perf=False,
                      **overrides) -> Any:
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(
            self._executor,
            self._sync_call,
            prompt, attachment, generating, tools, perf, overrides,
        )
        return await future

    def _sync_call(self, prompt, attachment, generating, tools,
                   perf, overrides) -> Any:
        # options 白名单：
        #   - LLM 采样参数
        #   - audio 通道参数（omni run_audio 需要）
        #   - TTS 通道参数（qwen3-tts TTSBackend.run_text 读这些）。
        #     没有这些时 SDK 用默认值（ref_speaker="girl_base",
        #     volume=0.5, no_play=False）能跑出声音，但用户在
        #     音视频描述页 tts_alert 工具传的 ref_speaker / volume
        #     必须透传到 worker 才会真正生效。
        payload = {
            "action": "infer",
            "prompt": prompt,
            "attachment": self._encode_attachments(attachment),
            "generating": getattr(generating, "__name__", None)
                          if generating is not None else None,
            "tools": [t.__name__ for t in (tools or [])],
            "options": {k: v for k, v in overrides.items()
                        if k in {"max_new_tokens", "temperature",
                                  "top_p", "top_k",
                                  "sample_rate", "n_channels", "sample_width",
                                  # TTS channel kwargs — see wdnai/backends/tts.py
                                  # TTSBackend.run_text overrides.get() calls.
                                  "language", "speaker", "ref_speaker",
                                  "instruct", "volume", "no_play",
                                  "non_streaming_mode"}},
        }
        reply = self._client.call(payload, timeout_ms=120_000)
        if not reply.get("ok"):
            err = reply.get("error", "unknown error")
            raise RuntimeError(
                f"compute_worker[{self._model_id}] infer failed: {err}")
        data = reply.get("data") or {}

        # Decode any attachment bytes the worker serialized. Worker
        # encodes ``result.attachment[0].data`` as base64 (TTS WAV
        # bytes, future VLM-decoded images, …) because cshim RPC
        # only carries JSON-serialisable values. We rehydrate it into
        # an object with the same attribute surface that wdnai.Attachment
        # exposes (``kind`` / ``mime_type`` / ``data``) so
        # tts_session.py can do ``result.attachment[0].data`` and
        # write the WAV to disk.
        attachments = []
        attachment_b64 = data.get("attachment_b64")
        if attachment_b64:
            try:
                raw = base64.b64decode(attachment_b64)
                attachments.append(_AttachmentProxy(
                    kind="audio", mime_type="audio/wav", data=raw,
                ))
            except Exception:
                # Bad base64 / oversized — leave attachment empty and
                # let the caller see an empty wav_bytes path.
                pass

        return _ProxyResult(
            text=data.get("text"),
            level=data.get("level"),
            tool_call=data.get("tool_call"),
            bbox=data.get("bbox"),
            crop_b64=data.get("crop_b64"),
            attachment=attachments,
            raw=data,
        )

    # ---- Embedding surface (Qwen3-VL-Embedding-2B only) -------------------
    # ``video_content_search`` service calls session.embed_text / embed_video.
    # Unlike ``respond`` (which is async), these are sync wdnai APIs, so we
    # call the proxy's sync path directly via the executor. The cshim RPC
    # worker side picks them up via the new ``embed_text`` / ``embed_video``
    # actions in compute_worker.py.

    def embed_text(self, text: str) -> "np.ndarray":
        """Encoding text query → 2048-dim normalized vector.

        Returns a numpy ``float32`` array on the calling thread. Synchronous
        version of the wdnai LLMSession.embed_text API.
        """
        reply = self._client.call(
            {"action": "embed_text", "text": text},
            timeout_ms=60_000,
        )
        if not reply.get("ok"):
            err = reply.get("error", "unknown error")
            raise RuntimeError(
                f"compute_worker[{self._model_id}] embed_text failed: {err}")
        data = reply.get("data") or {}
        vec = data.get("vec") or []
        import numpy as _np
        return _np.asarray(vec, dtype=_np.float32).reshape(-1)

    def embed_video(self, frames, prompt: str = "") -> "np.ndarray":
        """Embedding one video chunk (list of BGR ndarrays) → 2048-dim vector.

        Frames are JPEG-encoded + base64'd here; the worker decodes them
        back to BGR ndarrays via cv2.imdecode. We don't bypass this
        encoding (which would require sharing DMA-BUFs across the cshim
        boundary) because the per-call payload already fits in one DDS
        message for typical 16-frame chunks at JPEG q=85.
        """
        import base64 as _b64
        encoded = []
        for fr in frames:
            if hasattr(fr, "shape") and hasattr(fr, "dtype"):
                encoded.append(_bgr_ndarray_to_b64(fr))
            elif isinstance(fr, (bytes, bytearray)):
                encoded.append(_b64.b64encode(bytes(fr)).decode("ascii"))
            else:
                raise TypeError(
                    f"frame must be BGR ndarray or bytes, got {type(fr).__name__}")
        reply = self._client.call(
            {"action": "embed_video", "frames": encoded, "prompt": prompt or ""},
            timeout_ms=120_000,
        )
        if not reply.get("ok"):
            err = reply.get("error", "unknown error")
            raise RuntimeError(
                f"compute_worker[{self._model_id}] embed_video failed: {err}")
        data = reply.get("data") or {}
        vec = data.get("vec") or []
        import numpy as _np
        return _np.asarray(vec, dtype=_np.float32).reshape(-1)

    def _encode_attachments(self, attachment) -> list:
        if attachment is None:
            return []
        if not isinstance(attachment, (list, tuple)):
            attachment = [attachment]
        out = []
        for item in attachment:
            try:
                from wdnai import Attachment as _WdnaiAttach
                if isinstance(item, _WdnaiAttach):
                    # wdnai.Attachment channels: image/video/text/audio
                    # (按 ``kind`` 字段)。音频用 dict 包成``data_b64+kind``
                    # 让 worker 端能识别（否则字符串会被当成 text 兜底）。
                    if getattr(item, "images", None):
                        for img in item.images:
                            out.append(_bgr_ndarray_to_b64(img))
                        continue
                    if getattr(item, "frames", None):
                        for fr in item.frames:
                            out.append(_bgr_ndarray_to_b64(fr))
                        continue
                    if getattr(item, "text", None):
                        out.append(item.text)
                        continue
                    if getattr(item, "data", None):
                        kind = getattr(item, "kind", "audio") or "audio"
                        if kind == "audio":
                            out.append({
                                "data_b64": base64.b64encode(
                                    bytes(item.data)).decode("ascii"),
                                "kind":     "audio",
                            })
                        else:
                            out.append(base64.b64encode(
                                bytes(item.data)).decode("ascii"))
                        continue
                    continue
            except ImportError:
                pass
            if hasattr(item, "shape") and hasattr(item, "dtype"):
                out.append(_bgr_ndarray_to_b64(item))
                continue
            if isinstance(item, (bytes, bytearray)):
                # 裸 bytes：默认按音频处理（TTS 常见输出 shape）。
                out.append({
                    "data_b64": base64.b64encode(bytes(item)).decode("ascii"),
                    "kind":     "audio",
                })
                continue
            if isinstance(item, str):
                out.append(item)
                continue
            out.append(str(item))
        return out


def make_rpc_session_proxy(rpc_client, model_id: str) -> _RpcSessionProxy:
    return _RpcSessionProxy(rpc_client, model_id)


# Public alias
RpcSessionProxy = _RpcSessionProxy