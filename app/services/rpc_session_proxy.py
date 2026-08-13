"""rpc_session_proxy — rkai.LLMSession drop-in backed by wdndds cshim RPC.

Each compute instance lives in its own compute_worker subprocess; the
LLMSession is held by that worker. The main pyrkaiweb process holds
a wdndds ``RpcClient`` and uses this proxy to forward calls.

Wire: standard Pub-Sub with RELIABLE QoS, two topics per service
  - Request topic = ``wdndds/rpc/req/<model_id>``
  - Reply topic   = ``wdndds/rpc/reply/<model_id>``

The cshim wraps each request/reply in a ``{wdndds_reply_suffix,
data}`` envelope so the client can match replies to in-flight calls.

The proxy preserves the LLMSession surface that detector / tts /
text_chat / vfa use:

  * ``session.reset()``              → ``RpcClient.call({"action": "reset"})``
  * ``await session.respond(...)``   → ``RpcClient.call({"action": "infer", ...})``
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


class _ProxyResult:
    """Adapter object: detector/tts/vfa code reads ``result.text`` etc.;
    this mirrors the fields that rkai returns from ``sess.respond``."""

    def __init__(self, *, text=None, level=None, tool_call=None,
                 bbox=None, crop_b64=None, raw=None):
        self.text = text
        self.level = level
        self.tool_call = tool_call
        self.bbox = bbox
        self.crop_b64 = crop_b64
        self._raw = raw or {}

    @property
    def desc(self) -> Optional[str]:
        return self.text

    def __repr__(self) -> str:
        return (
            f"_ProxyResult(text={self.text!r}, level={self.level!r}, "
            f"tool_call={self.tool_call!r})"
        )

    def __str__(self) -> str:
        return self.text or ""


class _RpcSessionProxy:
    """Drop-in for ``rkai.LLMSession`` that talks to a compute_worker
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
        payload = {
            "action": "infer",
            "prompt": prompt,
            "attachment": self._encode_attachments(attachment),
            "generating": getattr(generating, "__name__", None)
                          if generating is not None else None,
            "tools": [t.__name__ for t in (tools or [])],
            "options": {k: v for k, v in overrides.items()
                        if k in {"max_new_tokens", "temperature",
                                  "top_p", "top_k"}},
        }
        reply = self._client.call(payload, timeout_ms=120_000)
        if not reply.get("ok"):
            err = reply.get("error", "unknown error")
            raise RuntimeError(
                f"compute_worker[{self._model_id}] infer failed: {err}")
        data = reply.get("data") or {}
        return _ProxyResult(
            text=data.get("text"),
            level=data.get("level"),
            tool_call=data.get("tool_call"),
            bbox=data.get("bbox"),
            crop_b64=data.get("crop_b64"),
            raw=data,
        )

    def _encode_attachments(self, attachment) -> list:
        if attachment is None:
            return []
        if not isinstance(attachment, (list, tuple)):
            attachment = [attachment]
        out = []
        for item in attachment:
            try:
                from rkai import Attachment as _RkAttach
                if isinstance(item, _RkAttach):
                    # rkai.Attachment exposes channels by ``kind``:
                    #   "image" → ``item.images`` (list[BGR ndarray])
                    #   "video" → ``item.frames`` (list[BGR ndarray])
                    #   "text"  → ``item.text``    (str)
                    #   "audio" → ``item.data``    (bytes)
                    # Each channel is serialized so the worker can
                    # reconstruct an equivalent Attachment on its side.
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
                        out.append(base64.b64encode(bytes(item.data)).decode("ascii"))
                        continue
                    continue
            except ImportError:
                pass
            if hasattr(item, "shape") and hasattr(item, "dtype"):
                out.append(_bgr_ndarray_to_b64(item))
                continue
            if isinstance(item, (bytes, bytearray)):
                out.append(base64.b64encode(bytes(item)).decode("ascii"))
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