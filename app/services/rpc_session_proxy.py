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
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

import numpy as _np

LOG = logging.getLogger("rpc_session_proxy")


# shm image transfer for the VLM image pipeline (音视频描述 / 视频目标检测
# / 视频文件分析 共用)。主进程把原始 BGR ndarray 直接 ``tofile()`` 写到
# ``/dev/shm``，wire 只传 path + shape + dtype；worker 端
# ``np.fromfile().reshape()`` 还原 byte-exact。
#
# 为什么走 /dev/shm path-list 而不是 base64 inline（参考
# ``video_content_search._write_frames_to_shm`` 的相同写法）：
#   1. **无损**：480×640 单帧 max 像素差从 JPEG q=85 的 96/255 → 0。
#      VLM 4B 类模型对像素抖动敏感，JPEG 块状 artifact 在 1080p + 768
#      variant 上会让模型误判 / 输出退化（用户实测报"泰文 / 乱码"）。
#   2. **LAN peer 兜底**：内置 FastDDS SHM transport（同机）+ UDPv4/TCPv4
#      （跨机）由 wdndds cshim 自动切换（use_builtin_transports=true,
#      见 cshim_src/wdndds_cshim.cpp:312），同机走 SHM；LAN peer 走
#      builtin UDPv4/TCPv4，proxy 的 ``is_remote=True`` 切回 base64 JPEG。
#      path-list 只能在 caller 自己的 /dev/shm 上被同机 worker 读，
#      LAN peer 拿不到 → 自动 fallback。
#   3. **Wire payload 小**：wire 只传 ~150 字节（path + shape + dtype），
#      跨 builtin SHM / UDPv4 / TCPv4 都远低于 per-message 限额。1080p
#      裸 BGR 6.2 MB 即使 builtin TCPv4 兜底能塞，DDS serialize / 反序列化
#      每帧一遍太重；走 tmpfs RAM 拷贝更便宜。
#   4. **tmpfs 资源**：/dev/shm 是 RAM-backed，板上典型 4 GB 充裕。
#      每秒 1 帧推理净占用 ≈ 0（每帧 unlink 立刻回收）。boot +
#      atexit 各扫一次 ``wdnwebui_vd_*.bin`` 防泄漏（max_age=1h，
#      防并发 boot 互清；atexit 用 max_age=0 全清）。
_VD_SHM_DIR = "/dev/shm"
_VD_SHM_PREFIX = "wdnwebui_vd_"


# Cleanup hook — if the main process is killed between
# ``_bgr_ndarray_to_path`` write and the worker's ``np.fromfile`` +
# ``os.unlink``, the /dev/shm temp file would leak (no other process
# will pick it up). The boot sweep clears entries older than 1 h
# (so concurrent boots don't sweep each other's in-flight writes);
# the atexit hook (max_age=0) clears whatever's still here at shutdown.
def _sweep_stale_vd_shm(max_age_seconds: float = 3600.0) -> int:
    """Remove stale ``/dev/shm/wdnwebui_vd_*.bin`` files.

    Args:
        max_age_seconds: skip files newer than this (default 1 h).
    Returns:
        Number of files actually removed.
    """
    try:
        import glob as _glob
        import os as _os
        import time as _time
    except ImportError:
        return 0
    n = 0
    now = _time.time()
    try:
        for p in _glob.glob(_os.path.join(_VD_SHM_DIR, f"{_VD_SHM_PREFIX}*.bin")):
            try:
                st = _os.stat(p)
            except OSError:
                continue
            if now - st.st_mtime < max_age_seconds:
                continue
            try:
                _os.unlink(p)
                n += 1
            except OSError:
                pass
    except OSError:
        pass
    if n:
        LOG.info("swept %d stale %s files", n, _VD_SHM_PREFIX)
    return n


try:
    import atexit as _atexit
    _atexit.register(_sweep_stale_vd_shm, 0.0)
except ImportError:
    pass
_sweep_stale_vd_shm()  # best-effort sweep on import


# Lazy import for cv2 (only used by image-encoding path).
_cv2_lock = __import__("threading").Lock()


def _bgr_ndarray_to_b64(arr, quality: int = 85) -> str:
    """JPEG-encode a BGR ndarray then base64.

    **Kept only for the LAN-peer fallback path** (when the proxy's
    ``_is_remote=True``, see :func:`make_rpc_session_proxy`) and for
    non-uint8-BGR ndarrays that fall through the path-list branch.
    Local-worker VLM image transfer uses ``_bgr_ndarray_to_path``
    instead — the path-list form is byte-exact (no JPEG) and avoids
    paying the wire payload / DDS serialize overhead on every frame.
    """
    import cv2  # lazy
    ok, buf = cv2.imencode(".jpg", arr, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RuntimeError("cv2.imencode failed")
    return base64.b64encode(buf.tobytes()).decode("ascii")


def _bgr_ndarray_to_path(arr) -> dict:
    """Write a BGR ``uint8`` ndarray to ``/dev/shm`` and return the path
    descriptor.

    Returns ``{"data_path": "<abs path>", "shape": [h, w, 3], "dtype": "uint8"}``.
    Worker reads with ``np.fromfile(path).reshape(h, w, 3)`` — no
    compression, no base64, byte-exact to what the detector sent.

    Non-uint8-BGR-HxWx3 inputs fall through to
    :func:`_bgr_ndarray_to_b64` (legacy JPEG+base64 path); this should
    never trigger from the detector (which always feeds uint8 BGR)
    but keeps audio-byte / text / multi-frame-list paths working.
    """
    import os as _os
    import uuid as _uuid
    if not isinstance(arr, _np.ndarray):
        # Fallback for unusual inputs — go through the JPEG path.
        # (Should never hit: detector only sends uint8 BGR HxWx3.)
        return {"data_b64": _bgr_ndarray_to_b64(arr)}
    if arr.dtype != _np.uint8 or arr.ndim != 3 or arr.shape[2] != 3:
        # Same fallback for non-BGR images.
        return {"data_b64": _bgr_ndarray_to_b64(arr)}
    _os.makedirs(_VD_SHM_DIR, exist_ok=True)
    name = f"{_VD_SHM_PREFIX}{_uuid.uuid4().hex}.bin"
    path = _os.path.join(_VD_SHM_DIR, name)
    arr.tofile(path)  # raw C-contig uint8 bytes
    h, w, _ = arr.shape
    return {
        "data_path": path,
        "shape": [int(h), int(w), 3],
        "dtype": "uint8",
    }


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

    def __init__(self, rpc_client, model_id: str, _svc=None,
                 on_client_recreated=None, *, is_remote: bool = False):
        self._client = rpc_client
        self._model_id = model_id
        # wdndds cshim requester 一次性：timeout / send-failed 后 channel
        # 永久 wedged。传 service 引用以便 destroy + create_requester 自愈。
        self._svc = _svc
        # recreate 成功后回调（registry 用来同步替换 inst.rpc_client，
        # 避免其它持有者继续引用已销毁的 requester）。
        self._on_client_recreated = on_client_recreated
        self._client_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"rpc-proxy-{model_id}",
        )
        # ``True`` when the proxy targets a LAN peer worker on another
        # machine. /dev/shm is per-host, so the raw-bytes path-list
        # trick can't reach the remote worker — fall back to
        # JPEG+base64 (lossy but crosses machine boundaries). Local
        # workers default to ``False`` (raw BGR via /dev/shm, byte-exact).
        self._is_remote = bool(is_remote)

    def _recreate_client(self) -> None:
        """Destroy wedged cshim client + spin up a fresh requester.

        destroy 必须在旧 requester 的 ``Requester._lock`` 内执行：
        ``wdndds_requester_destroy`` 与正在进行的 ``wdndds_requester_call``
        并发是 C 层 use-after-free（probe 线程 / 管理操作可能正拿着同一
        requester 在 call），实测会产出损坏 reply 甚至直接 SIGSEGV。
        recreate 完成后通过 ``_on_client_recreated`` 回调通知 registry
        同步替换 ``inst.rpc_client``，消除悬挂引用。
        """
        if self._svc is None:
            LOG.warning("rpc-proxy[%s]: no _svc ref, can't recreate", self._model_id)
            return
        with self._client_lock:
            old = self._client
            try:
                with old._lock:
                    old.destroy()
            except Exception as e:  # noqa: BLE001
                LOG.debug("rpc-proxy[%s]: destroy old client raised %s",
                          self._model_id, e)
            try:
                new_client = self._svc.create_requester(timeout_ms=10_000)
            except Exception as e:  # noqa: BLE001
                LOG.error("rpc-proxy[%s]: recreate failed: %s",
                          self._model_id, e)
                return
            self._client = new_client
            LOG.info("rpc-proxy[%s]: cshim client recreated (was wedged)",
                     self._model_id)
        cb = self._on_client_recreated
        if cb is not None:
            try:
                cb(self._client)
            except Exception as e:  # noqa: BLE001
                LOG.debug("rpc-proxy[%s]: on_client_recreated callback "
                          "raised %s", self._model_id, e)

    def _call_with_recreate(self, payload: dict, timeout_ms: int) -> dict:
        """call + auto-recreate on first wedge.

        wdndds cshim deadlock path:
          1. first call → reply topic discovery race → timeout
          2. next call → "send failed" (RC=-1, requester wedged)

        **只有「结构性故障」才 recreate**：send failed / no data /
        write rc / discovery_pending —— 这些说明 requester 或 endpoint
        匹配真的坏了，destroy + recreate + warmup 能自愈。

        **纯 timeout 不 recreate**：worker 端 rknn3lite C 扩展推理期间
        不释放 GIL，cshim pump 线程存在实测 ~35s 无法应答任何 RPC 的
        「聋哑窗口」——这是 worker 忙，不是通道坏。此时销毁 requester
        不仅无益，还会把 reply 一起丢掉并引发 destroy/call 并发竞态。
        timeout 交给上层 ``_call_poll_with_retry`` 的有限重试消化。
        """
        reply = self._client.call(payload, timeout_ms=timeout_ms)
        if reply.get("ok") or not reply.get("error"):
            return reply
        err = str(reply.get("error", "")).lower()
        wedged = ("send failed" in err or "no data" in err
                  or "write rc" in err or "discovery_pending" in err)
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

    # chat_poll resilience budget.
    #
    # worker 推理期间独立 participant 的 info 响应仅
    # ~10ms，而主进程长期使用的 requester 会出现 35-60s 的通道级 wedge
    # （cshim requester 一次性：进入坏状态后该 handle 永久超时，新建
    # requester 立即恢复）。因此策略分两层：
    #   * soft：前 _POLL_SOFT_RETRIES 次纯重试，覆盖瞬时抖动；
    #   * hard：之后每次失败先安全 recreate（probe 已隔离 + destroy
    #     持锁 + registry 回调同步）再重试。实测 recreate 后首次调用
    #     即恢复。
    # 最坏预算 ≈ soft 2×5.5s + hard 6×6.5s ≈ 50s，仍在 chat_run 300s
    # deadline 内；典型断流窗口在第一次 recreate 后即结束（~17s）。
    _POLL_SOFT_RETRIES = 2
    _POLL_HARD_RETRIES = 8
    _POLL_RETRY_SLEEP = 0.5
    _POLL_TIMEOUT_MS = 5_000

    def _call_resilient(self, payload: dict, timeout_ms: int, *,
                        soft_retries: Optional[int] = None,
                        hard_retries: Optional[int] = None) -> dict:
        """Transient-failure retry with safe recreate escalation.

        前 ``soft_retries`` 次失败只做纯重试；超过后每次先 destroy +
        recreate cshim client（安全版：持锁销毁、probe 线程已隔离、
        registry 通过回调同步新 handle）再 warmup + 重试。非瞬时错误
        （业务 error / malformed 等）原样返回交由调用方处理。
        """
        if soft_retries is None:
            soft_retries = self._POLL_SOFT_RETRIES
        if hard_retries is None:
            hard_retries = self._POLL_HARD_RETRIES
        last = {"ok": False, "error": "unknown"}
        for attempt in range(hard_retries):
            if attempt > soft_retries:
                LOG.warning(
                    "rpc-proxy[%s]: %d consecutive transient failures — "
                    "recreating cshim client", self._model_id, attempt)
                self._recreate_client()
                # warm-up 加速新 requester 的 SEDP 匹配；失败无碍
                try:
                    self._client.call({"action": "info"},
                                      timeout_ms=timeout_ms)
                except Exception as e:  # noqa: BLE001
                    LOG.debug("rpc-proxy[%s]: post-recreate warmup raised "
                              "%s", self._model_id, e)
            r = self._client.call(payload, timeout_ms=timeout_ms)
            if r.get("ok") or not r.get("error"):
                return r
            err = str(r.get("error", "")).lower()
            last = r
            if not any(k in err for k in ("timeout", "no data",
                                          "send failed")):
                return r
            time.sleep(self._POLL_RETRY_SLEEP)
        return last

    def _call_poll_with_retry(self, offset: int) -> dict:
        """chat_poll with bounded retry + safe recreate escalation."""
        return self._call_resilient(
            {"action": "chat_poll", "offset": offset},
            timeout_ms=self._POLL_TIMEOUT_MS)

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
        # 首次 call 也走弹性路径：通道若已 wedge，soft 重试 + recreate
        # 自愈后再发起；否则用户会直接看到 chat_run failed。
        reply = self._call_resilient(payload, timeout_ms=10_000,
                                     soft_retries=1, hard_retries=4)
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
        t_send = time.time()
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        shm_path = f"/dev/shm/wdnwebui_chat_{self._model_id}.json"
        while True:
            if stop_event is not None and stop_event.is_set():
                self.chat_stop()
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"compute_worker[{self._model_id}] chat_run timed out")

            # 数据面双通道：
            #   1) 同机 worker 会把 chat 状态原子发布到 /dev/shm —— 直接
            #      读文件，完全绕开 DDS（实测 DDS RELIABLE 交付存在数十秒
            #      级抖动，是流式中断的根因）。start_ts 校验防止读到上一
            #      轮残留快照。
            #   2) 文件缺失/过期（跨机 peer、worker 老版本）→ 回退
            #      chat_poll RPC。
            data: Optional[dict] = None
            try:
                with open(shm_path, "r") as f:
                    snap = json.load(f)
                if (isinstance(snap, dict)
                        and isinstance(snap.get("start_ts"), (int, float))
                        and snap["start_ts"] >= t_send - 1.0):
                    data = snap
                elif isinstance(snap, dict):
                    LOG.warning(
                        "rpc-proxy[%s]: shm snapshot REJECTED "
                        "start_ts=%s t_send=%s status=%s",
                        self._model_id, snap.get("start_ts"), t_send,
                        snap.get("status"))
            except Exception as e:  # noqa: BLE001
                LOG.warning("rpc-proxy[%s]: shm read failed: %s: %s",
                            self._model_id, type(e).__name__, e)
                data = None

            via_shm = data is not None
            if not via_shm:
                # chat_poll 走 _call_resilient：前 2 次纯重试覆盖瞬时抖动，
                # 之后每次失败安全 recreate cshim client。
                poll = self._call_poll_with_retry(offset)
                if not poll.get("ok"):
                    raise RuntimeError(
                        f"compute_worker[{self._model_id}] chat_poll failed: "
                        f"{poll.get('error', 'unknown error')}")
                data = poll.get("data") or {}

            status = data.get("status")
            if on_delta is not None:
                if via_shm:
                    all_deltas = data.get("deltas") or []
                    new_deltas = all_deltas[offset:]
                    offset = len(all_deltas)
                else:
                    new_deltas = data.get("deltas") or []
                    offset = int(data.get("offset") or offset)
                for d in new_deltas:
                    if d:
                        on_delta(d)
            elif via_shm:
                offset = len(data.get("deltas") or [])
            else:
                offset = int(data.get("offset") or offset)

            if status == "done":
                result = dict(data.get("result") or {})
                result.setdefault("status", "done")
                return result
            if status == "error":
                raise RuntimeError(data.get("error") or "chat error")
            if status == "idle" and not via_shm:
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
                                   "non_streaming_mode",
                                   # UDS 路径：worker 连过来把 SDK 的
                                   # on_audio_chunk 流回主进程（bound
                                   # method 不能 pickle，此为唯一通道）。
                                   "tts_pcm_socket_path"}},
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
# infer 路径走 ``_call_resilient``：冷启动 + 多模态首帧 vision
        # prefill 容易单帧 90-150s（实测 Qwen3-VL-4B / Qwen3.5-VL-4B
        # 第一次 infer 都偶发 130s+），单纯一次 120s 超时后直接 raise
        # 会让用户看到"启动失败"而 worker 端其实跑完了。
        # soft_retries=1 + hard_retries=2 → 最坏预算 ≈
        #   3 × 240s + recreate ≈ 720s，覆盖最坏的多模态首帧延迟
        # （vision + MRoPE prefill）；单帧稳态推理一般 < 5s 命中第 1 次。
        # 超时后 recreate cshim client 清理可能的端点状态残留。
        reply = self._call_resilient(payload, timeout_ms=240_000,
                                     soft_retries=1, hard_retries=2)
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
                            if self._is_remote:
                                out.append(_bgr_ndarray_to_b64(img))
                            else:
                                out.append(_bgr_ndarray_to_path(img))
                        continue
                    if getattr(item, "frames", None):
                        for fr in item.frames:
                            if self._is_remote:
                                out.append(_bgr_ndarray_to_b64(fr))
                            else:
                                out.append(_bgr_ndarray_to_path(fr))
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
                if self._is_remote:
                    out.append(_bgr_ndarray_to_b64(item))
                else:
                    out.append(_bgr_ndarray_to_path(item))
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


def make_rpc_session_proxy(rpc_client, model_id: str, _svc=None,
                           on_client_recreated=None,
                           *, is_remote: bool = False) -> _RpcSessionProxy:
    """Build a proxy.

    ``on_client_recreated(new_requester)`` — optional callback invoked
    after a wedged client was destroyed + recreated. The registry uses
    it to keep ``inst.rpc_client`` pointing at the live requester so
    probe threads and admin operations never touch a destroyed handle.

    ``is_remote=True`` when the proxy targets a **LAN peer worker** (a
    worker running on another machine). The proxy then falls back to
    JPEG+base64 for image attachments instead of writing to
    ``/dev/shm`` — the path string travels through DDS, but the
    file lives on the caller's filesystem which the remote worker
    cannot access. Default ``False`` (local worker → /dev/shm path,
    lossless).
    """
    return _RpcSessionProxy(rpc_client, model_id, _svc=_svc,
                            on_client_recreated=on_client_recreated,
                            is_remote=is_remote)


# Public alias
RpcSessionProxy = _RpcSessionProxy
