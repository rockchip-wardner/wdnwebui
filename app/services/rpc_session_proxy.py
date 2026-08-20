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
import threading
import time
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

    Construct via :func:`make_rpc_session_proxy(rpc_client, model_id, _svc)`.
    """

    def __init__(self, rpc_client, model_id: str, _svc=None):
        self._client = rpc_client
        self._model_id = model_id
        # wdndds cshim requester 一次性：timeout / send-failed 后 channel
        # 永久 wedged。传 service 引用以便 destroy + create_requester 自愈。
        self._svc = _svc
        self._client_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"rpc-proxy-{model_id}",
        )

    def _recreate_client(self) -> None:
        """Destroy wedged cshim client + spin up a fresh requester."""
        if self._svc is None:
            LOG.warning("rpc-proxy[%s]: no _svc ref, can't recreate", self._model_id)
            return
        with self._client_lock:
            old = self._client
            try:
                old.destroy()
            except Exception as e:  # noqa: BLE001
                LOG.debug("rpc-proxy[%s]: destroy old client raised %s",
                          self._model_id, e)
            try:
                self._client = self._svc.create_requester(timeout_ms=10_000)
                LOG.info("rpc-proxy[%s]: cshim client recreated (was wedged)",
                         self._model_id)
            except Exception as e:  # noqa: BLE001
                LOG.error("rpc-proxy[%s]: recreate failed: %s", self._model_id, e)

    def _call_with_recreate(self, payload: dict, timeout_ms: int) -> dict:
        """call + auto-recreate on first wedge.

        wdndds cshim deadlock path:
          1. first call → reply topic discovery race → timeout
          2. next call → "send failed" (RC=-1, requester wedged)
        Either symptom triggers destroy + recreate_requester + retry.
        After recreate, new SEDP/SPDP endpoints match within ~1s on
        localhost, so the retry almost always succeeds.
        """
        reply = self._client.call(payload, timeout_ms=timeout_ms)
        if reply.get("ok") or not reply.get("error"):
            return reply
        err = str(reply.get("error", "")).lower()
        wedged = ("timeout" in err or "send failed" in err
                  or "no data" in err)
        if not wedged:
            return reply
        LOG.warning("rpc-proxy[%s]: cshim wedged (%s); recreating client",
                    self._model_id, err)
        self._recreate_client()
        # probe warm-up so retry doesn't re-hit the same discovery race
        try:
            self._client.call({"action": "info"}, timeout_ms=10_000)
        except Exception as e:  # noqa: BLE001
            LOG.debug("rpc-proxy[%s]: post-recreate warmup raised %s",
                      self._model_id, e)
        return self._client.call(payload, timeout_ms=timeout_ms)

    # Max consecutive chat_poll timeouts before we give up. A single
    # timeout is usually transient (worker pump starved of the GIL while
    # _chat_run_loop holds it during backend.run), so we retry a few
    # times before surfacing an error.
    _POLL_RETRY = 3
    _POLL_RETRY_SLEEP = 0.5

    def _call_poll_with_retry(self, offset: int) -> dict:
        """chat_poll with bounded retry on transient timeout + self-heal.

        Each attempt goes through ``_call_with_recreate`` so a wedged
        cshim requester is destroyed + recreated instead of failing
        forever. Only repeated consecutive timeouts are surfaced as an
        error; real worker errors (``ok:false`` non-timeout) return
        immediately.
        """
        last = {"ok": False, "error": "unknown"}
        for _ in range(self._POLL_RETRY):
            poll = self._call_with_recreate(
                {"action": "chat_poll", "offset": offset}, timeout_ms=5_000)
            if poll.get("ok"):
                return poll
            err = str(poll.get("error", "")).lower()
            if "timeout" not in err and "no data" not in err and "send failed" not in err:
                return poll
            last = poll
            time.sleep(self._POLL_RETRY_SLEEP)
        return last

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

    def chat_run(self, history, text, *, context_limit=None,
                 max_new_tokens=2048, on_delta=None, stop_event=None,
                 poll_interval_ms=150, timeout_ms=300000,
                 tool_names: Optional[list] = None) -> dict:
        """Run one text-chat turn against the worker.

        Synchronous (call it from a plain thread). The worker runs the
        inference on a background thread and appends token deltas to its
        chat state; this method polls ``chat_poll`` until the run
        finishes, invoking ``on_delta(text)`` for each delta chunk.

        ``tool_names`` lists the ``@Callable`` class names (e.g.
        ``"TtsAlert"``) the worker should expose to the LLM for tool
        calling. Worker injects the tool schema into the prompt and
        parses ``<tool_call>{...}</tool_call>`` output; if the model
        emits a tool call, ``result["tool_call"]`` carries the class
        name and ``result["tool_call_args"]`` the validated arguments
        (dict). Stage 1: caller decides what to do — agent-loop is
        planned for stage 2.

        Returns the worker's ``result`` dict on ``done`` (text /
        thinking / answer / elapsed_sec / prompt_tokens /
        answer_tokens / tool_call / tool_call_args). Raises
        ``RuntimeError`` on worker errors / timeout.
        """
        payload = {
            "action": "chat_run",
            "history": history,
            "text": text,
            "context_limit": int(context_limit or 0),
            "max_new_tokens": int(max_new_tokens or 2048),
            "tools": list(tool_names or []),
        }
        reply = self._call_with_recreate(payload, timeout_ms=10_000)
        if not reply.get("ok"):
            err = reply.get("error", "unknown error")
            raise RuntimeError(
                f"compute_worker[{self._model_id}] chat_run failed: {err}")
        data = reply.get("data") or {}
        if data.get("status") != "chat_started":
            raise RuntimeError(
                f"compute_worker[{self._model_id}] chat_run: "
                f"{data.get('error') or data.get('status') or 'unknown'}")

        offset = 0
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        while True:
            if stop_event is not None and stop_event.is_set():
                self.chat_stop()
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"compute_worker[{self._model_id}] chat_run timed out")
            # chat_poll 走 _call_with_recreate（requester wedged 时自动
            # destroy+recreate+warmup+retry），并对瞬时 timeout 做有限重试。
            # worker 端 _chat_run_loop 的 backend.run 推理会持 GIL，阻塞 pump
            # 线程响应 chat_poll；GIL 阻塞是瞬时的，重试几次通常能恢复。
            poll = self._call_poll_with_retry(offset)
            if not poll.get("ok"):
                raise RuntimeError(
                    f"compute_worker[{self._model_id}] chat_poll failed: "
                    f"{poll.get('error', 'unknown error')}")
            data = poll.get("data") or {}
            status = data.get("status")
            if on_delta is not None:
                for d in data.get("deltas") or []:
                    if d:
                        on_delta(d)
            offset = int(data.get("offset") or offset)
            if status == "done":
                result = data.get("result") or {}
                result.setdefault("status", "done")
                return result
            if status == "error":
                raise RuntimeError(data.get("error") or "chat error")
            if status == "idle":
                raise RuntimeError("chat went idle unexpectedly")
            time.sleep(poll_interval_ms / 1000.0)

    def chat_stop(self) -> None:
        try:
            self._client.call({"action": "chat_stop"}, timeout_ms=5_000)
        except Exception as e:  # noqa: BLE001
            LOG.warning("chat_stop on %s raised: %s", self._model_id, e)

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
        # 视觉模型目标尺寸（音视频描述页 ``#vd-detect-size``）——
        # 顶层字段而非 ``options``，因为这是 vision backend 特有的
        # channel 控制，不是 sampling 参数；也不进白名单，避免
        # 误把它当成采样配置采样化。worker ``infer_handler`` 直接读
        # ``payload["image_size"]``，透传给 ``sess.respond(image_size=
        # ...)``。``None`` / 缺省 → backend 走 auto-pick。
        _image_size = overrides.get("image_size")
        if isinstance(_image_size, int) and not isinstance(_image_size, bool):
            payload["image_size"] = _image_size
        reply = self._client.call(payload, timeout_ms=120_000)
        if not reply.get("ok"):
            err = reply.get("error", "unknown error")
            raise RuntimeError(
                f"compute_worker[{self._model_id}] infer failed: {err}")
        data = reply.get("data") or {}

        # ---- Tool-instance path -------------------------------------------
        # Worker 端 SDK 的 ``dispatch_tool_call`` 把模型返回的 JSON
        # 还原成 ``@Callable`` 实例并直接 return。Worker 把这个实例
        # ``model_dump()`` 后作为 ``tool_call_args`` 发回 RPC，我们
        # 在这里按 ``tool_call``（class 名）查到对应 class 并
        # ``cls.model_validate(args)`` 重建——这样 detector 端
        # ``isinstance(result, TtsAlert)`` 通过、``result(frame_bgr,
        # ctx)`` 可直接调用。不重建的话 detector 拿到的是
        # ``_ProxyResult``，永远 instanceof 不上。
        #
        # Registry key 是 UI 用的 ``"tts_alert"``，worker 发的是 class
        # 名 ``"TtsAlert"`` —— 两者不同。按 class 名查 registry。
        tool_call_name = data.get("tool_call")
        tool_call_args = data.get("tool_call_args")
        if tool_call_name and isinstance(tool_call_args, dict):
            try:
                from app.services.tools import get_tool_registry
                cls = None
                for spec in get_tool_registry().list_specs():
                    if spec.tool_class.__name__ == tool_call_name:
                        cls = spec.tool_class
                        break
                if cls is None:
                    raise KeyError(tool_call_name)
                instance = cls.model_validate(tool_call_args)
                # Return the actual instance so the caller's
                # isinstance() check + __call__(frame_bgr, ctx)
                # both work without a wrapper adapter.
                return instance
            except KeyError:
                LOG.warning("proxy: unknown tool class %r in reply — "
                            "falling back to _ProxyResult", tool_call_name)
            except Exception as e:  # noqa: BLE001
                LOG.warning("proxy: tool instance reconstruct failed "
                            "(%s: %s) — falling back to _ProxyResult",
                            type(e).__name__, e)

        # ---- Attachment (TTS WAV bytes / future VLM-decoded images) -----
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

        # ---- Schema-instance reconstruction ---------------------------------
        # Worker forwards ``result_data = result.model_dump()`` for schema
        # returns (TargetDoc / VLMDoc / TTSResult). Reconstruct the actual
        # class so detector's ``isinstance(result, TargetDoc)`` succeeds.
        # Without this the proxy returns ``_ProxyResult`` whose only
        # ``text / level / bbox / crop_b64`` slots are all ``None`` for
        # TargetDoc — every event then falls through to the VLMDoc
        # branch with empty text and gets silently dropped as degenerate.
        gen_name = payload.get("generating")
        result_data = data.get("result_data")
        if gen_name == "TargetDoc" and isinstance(result_data, dict):
            try:
                from app.services.detector import _get_target_doc_cls
                TargetDoc = _get_target_doc_cls()
                return TargetDoc.model_validate(result_data)
            except Exception as e:  # noqa: BLE001
                LOG.warning(
                    "proxy: TargetDoc reconstruct failed (%s: %s) — "
                    "falling back to _ProxyResult",
                    type(e).__name__, e,
                )

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
        reply = self._call_with_recreate(
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

    def embed_video(self, frames_or_paths, prompt: str = "") -> "np.ndarray":
        """Embedding one video chunk → 2048-dim vector.

        Accepts either:
          - ``List[np.ndarray]`` (BGR frames) — encoded to JPEG+base64
            and shipped inline. This is the legacy path; works for
            small payloads only.
          - ``List[str]`` (absolute paths to JPEG files, e.g. on
            ``/dev/shm``) — the worker reads each file with
            ``cv2.imread()``. This is the **preferred** path: the DDS
            payload is only ~50 bytes (a list of paths) instead of
            ~500 KiB (base64-encoded JPEG frames), which keeps the
            message well under the 64 KiB UDP / SHM transport limit
            that otherwise wedges ``write()`` indefinitely on FastDDS 3.6.2.
        Auto-detects: if the first element is a string, treats the
        whole list as paths; otherwise falls back to base64 encoding.
        """
        import base64 as _b64
        # Prefer the /dev/shm path-list contract — one path per frame
        # is ~50 bytes per chunk vs ~500 KiB inline base64.
        if (frames_or_paths and isinstance(frames_or_paths[0], str)):
            paths = list(frames_or_paths)
            reply = self._call_with_recreate(
                {"action": "embed_video", "frame_paths": paths,
                 "prompt": prompt or ""},
                timeout_ms=300_000,
            )
        else:
            # Legacy fallback: keep the old base64 path working for
            # any other caller that bypasses the VCS service.
            encoded = []
            for fr in frames_or_paths:
                if hasattr(fr, "shape") and hasattr(fr, "dtype"):
                    encoded.append(_bgr_ndarray_to_b64(fr, quality=60))
                elif isinstance(fr, (bytes, bytearray)):
                    encoded.append(_b64.b64encode(bytes(fr)).decode("ascii"))
                else:
                    raise TypeError(
                        f"frame must be BGR ndarray, bytes, or str path; got {type(fr).__name__}")
            reply = self._call_with_recreate(
                {"action": "embed_video", "frames": encoded, "prompt": prompt or ""},
                timeout_ms=300_000,
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


def make_rpc_session_proxy(rpc_client, model_id: str, _svc=None) -> _RpcSessionProxy:
    return _RpcSessionProxy(rpc_client, model_id, _svc=_svc)


# Public alias
RpcSessionProxy = _RpcSessionProxy
