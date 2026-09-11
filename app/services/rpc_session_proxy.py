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


# Response-side shm files (mirror of _VD_SHM_*) — worker writes crop /
# result_data payloads to ``/dev/shm/wdnwebui_cr_*`` and main process
# reads + unlinks them inside ``_sync_call``. Same boot/atexit sweep
# pattern: max_age=1h on import (don't yank files another process may
# still be reading) + max_age=0 on shutdown (clear all leftovers).
_REPLY_SHM_PREFIX = "wdnwebui_cr_"


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


def _sweep_stale_reply_shm(max_age_seconds: float = 3600.0) -> int:
    """Remove stale ``/dev/shm/wdnwebui_cr_*.{jpg,json}`` files.

    Same pattern as :func:`_sweep_stale_vd_shm` — the worker writes
    these (crop JPEG bytes, result_data JSON) and the main process
    reads + unlinks inside ``_sync_call``. A kill between write/read
    leaves them behind; the boot sweep (1 h max_age) protects against
    concurrent boot races; the atexit sweep (max_age=0) cleans up at
    shutdown.
    """
    try:
        import glob as _glob
        import os as _os
        import time as _time
    except ImportError:
        return 0
    n = 0
    now = _time.time()
    for suffix in ("jpg", "json"):
        try:
            for p in _glob.glob(
                    _os.path.join(_VD_SHM_DIR, f"{_REPLY_SHM_PREFIX}*.{suffix}")):
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
        LOG.info("swept %d stale %s files", n, _REPLY_SHM_PREFIX)
    return n


try:
    import atexit as _atexit
    _atexit.register(_sweep_stale_vd_shm, 0.0)
    _atexit.register(_sweep_stale_reply_shm, 0.0)
except ImportError:
    pass
_sweep_stale_vd_shm()  # best-effort sweep on import
_sweep_stale_reply_shm()  # best-effort sweep on import


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
    :class:`wdnai.AudioAttachment` — ``kind`` / ``mime_type`` / ``data``
    (TTS WAV bytes only). New code should use :class:`wdnai.ImageAttachment`
    / / ``TextAttachment`` / ``AudioAttachment`` directly; this proxy
    stays around for the wire encode path which historically populated
    it on the reply side from the worker's ``attachment_b64`` field.

    Kept minimal so we don't have to import the real wdnai.Attachment
    tree (which would chain through backends / librknn and segfault on
    a non-RK dev box — see ``WdnaiDetector._load_wdnai`` constraint).
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
                 bbox=None, crop_b64=None, attachment=None, raw=None,
                 schema_parse_failed: bool = False):
        self.text = text
        self.level = level
        self.tool_call = tool_call
        self.bbox = bbox
        self.crop_b64 = crop_b64
        self.attachment = attachment or []
        self._raw = raw or {}
        # 诊断 (v0.13): worker 端 schema parse 失败 → 主进程拿到的不是
        # TargetDoc 实例而是 _ProxyResult(text=raw) 时,detector 端根据此
        # flag 给事件 text 加 ``[解析失败]`` 前缀,前端历史记录里能区分
        # "正常 VLM 描述" vs "目标检测 schema 解析失败"。
        self.schema_parse_failed = bool(schema_parse_failed)

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

        Memory: ``Service.create_requester`` appends the new
        ``Requester`` to ``Service._requesters`` for lifecycle tracking;
        the old wedged requester was already in there. Without an
        explicit ``Service.remove_requester(old)`` after destroy(), the
        list would grow unbounded — every wedged-client recreate
        leaks one Python ``Requester`` object + its (now-dead) C
        handle + its slot in the wdndds ``_cb_refs`` keepalive dict.
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
            # Drop the dead Requester from the Service's tracking list
            # BEFORE allocating the new one — this keeps the list
            # size at exactly 1 for the lifetime of the proxy.
            try:
                self._svc.remove_requester(old)
            except Exception as e:  # noqa: BLE001
                LOG.debug("rpc-proxy[%s]: svc.remove_requester(old) raised %s",
                          self._model_id, e)
            try:
                # create_requester 内部不发起 RPC,只是 cshim entity
                # 创建 (SEDP discovery grace ~5s)。5s 足够。
                new_client = self._svc.create_requester(timeout_ms=5_000)
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
        # (info handler 立即返回,5s 足够)
        try:
            self._client.call({"action": "info"}, timeout_ms=5_000)
        except Exception as e:  # noqa: BLE001
            LOG.debug("rpc-proxy[%s]: post-recreate warmup raised %s",
                      self._model_id, e)
        return self._client.call(payload, timeout_ms=timeout_ms)

    def _decode_reply_shm(self, data: dict) -> dict:
        """Replace ``crop_path`` / ``result_data_path`` / ``structured_path``
        / ``text_path`` with their inlined values read from /dev/shm.
        Mirrors the request-side ``_bgr_ndarray_to_path`` round trip —
        the worker writes the heavy payload to tmpfs and ships only the
        path string; we read the file, in-place replace the reference with
        the original content, and unlink so the file doesn't accumulate
        across frames.

        Returns the (mutated) ``data`` dict for chaining. If no path
        is present, returns ``data`` unchanged.

        v0.14 加 ``text_path`` 处理 (VFA SceneFrame Path 5 兜底截断):
        当 SDK extract_json 失败返 SceneFrame(text=<raw_text>) 时,
        整段模型原始输出 (5 字段 SceneFrame JSON, ~200-600 字节)
        原本会被 FastCDR 在 ComputeReply.text 255 字节处静默截断。
        worker 改为 JSON-encoded > 200 字节时写 ``/dev/shm/wdnwebui_cr_*.txt``
        并把 ``data["text"] = None`` + ``data["text_path"] = path``;这里
        读回 SHM 内容填 ``data["text"]``, 与 ``crop_path`` /
        ``result_data_path`` / ``structured_path`` 三路同源同款 (都用
        ``_write_reply_payload_to_shm`` 写、``os.unlink`` 回收,
        FileNotFoundError 走 warning + None fallback)。VFA
        ``_split_scene_frame_result`` Path 2 拿到完整 ``_raw["text"]`` 后
        regex 抽 JSON 走智能归一化兜底 (regex 在 truncation 文本上失败,
        但在 SHM 读回的完整文本上能拿到 scene/actors/action/objects/text)。
        """
        text_path = data.get("text_path")
        if isinstance(text_path, str) and text_path:
            try:
                with open(text_path, "rb") as f:
                    raw_bytes = f.read()
                data["text"] = raw_bytes.decode("utf-8")
            except FileNotFoundError:
                LOG.warning(
                    "rpc-proxy[%s]: text_path %s missing (worker may "
                    "have crashed between write and reply) — falling "
                    "back to None, VFA Path 2 will see empty _raw.text",
                    self._model_id, text_path,
                )
            except (OSError, UnicodeDecodeError) as e:
                LOG.warning(
                    "rpc-proxy[%s]: text_path %s read failed: %s",
                    self._model_id, text_path, e,
                )
            finally:
                try:
                    import os as _os_text
                    _os_text.unlink(text_path)
                except OSError:
                    pass
            data.pop("text_path", None)

        crop_path = data.get("crop_path")
        if isinstance(crop_path, str) and crop_path:
            try:
                with open(crop_path, "rb") as f:
                    raw_bytes = f.read()
                import base64 as _b64
                data["crop_b64"] = _b64.b64encode(raw_bytes).decode("ascii")
            except FileNotFoundError:
                LOG.warning(
                    "rpc-proxy[%s]: crop_path %s missing (worker may "
                    "have crashed between write and reply) — falling "
                    "back to None, detector will re-crop from frame_bgr",
                    self._model_id, crop_path,
                )
            except OSError as e:
                LOG.warning(
                    "rpc-proxy[%s]: crop_path %s read failed: %s",
                    self._model_id, crop_path, e,
                )
            finally:
                try:
                    import os as _os_cr
                    _os_cr.unlink(crop_path)
                except OSError:
                    pass
            data.pop("crop_path", None)

        result_data_path = data.get("result_data_path")
        if isinstance(result_data_path, str) and result_data_path:
            try:
                with open(result_data_path, "rb") as f:
                    raw_bytes = f.read()
                import json as _json_rd
                parsed = _json_rd.loads(raw_bytes.decode("utf-8"))
                if isinstance(parsed, dict):
                    data["result_data"] = parsed
            except FileNotFoundError:
                LOG.warning(
                    "rpc-proxy[%s]: result_data_path %s missing — "
                    "TargetDoc/VLMDoc reconstruction will fall back "
                    "to _ProxyResult defaults",
                    self._model_id, result_data_path,
                )
            except (OSError, ValueError) as e:
                LOG.warning(
                    "rpc-proxy[%s]: result_data_path %s read failed: %s",
                    self._model_id, result_data_path, e,
                )
            finally:
                try:
                    import os as _os_rd
                    _os_rd.unlink(result_data_path)
                except OSError:
                    pass
            data.pop("result_data_path", None)

        # Same path-list pattern for SceneFrame's ``structured`` payload
        # (VFA's `_split_scene_frame_result` reads `_raw["structured"]`
        # in Path 2). Worker routes through /dev/shm when JSON > 100 B.
        structured_path = data.get("structured_path")
        if isinstance(structured_path, str) and structured_path:
            try:
                with open(structured_path, "rb") as f:
                    raw_bytes = f.read()
                import json as _json_s
                parsed = _json_s.loads(raw_bytes.decode("utf-8"))
                if isinstance(parsed, dict):
                    data["structured"] = parsed
            except FileNotFoundError:
                LOG.warning(
                    "rpc-proxy[%s]: structured_path %s missing — "
                    "VFA Path 2 will see empty _raw.structured",
                    self._model_id, structured_path,
                )
            except (OSError, ValueError) as e:
                LOG.warning(
                    "rpc-proxy[%s]: structured_path %s read failed: %s",
                    self._model_id, structured_path, e,
                )
            finally:
                try:
                    import os as _os_s
                    _os_s.unlink(structured_path)
                except OSError:
                    pass
            data.pop("structured_path", None)

        return data

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

    @staticmethod
    def _worker_business_error(reply: Any) -> Optional[str]:
        """Return the worker-side business error text, or ``None``.

        wire 顶层 ``ok`` 只反映**传输层**成败 —— cshim Replier 回调没崩
        就恒为 ``True``（见 wdndds_cshim.cpp reply 组装：handler 返回的
        dict 原样嵌进 ``data`` 字段，ok 与 handler 语义无关）。handler
        的业务错误以 ``{"status": "error", "error": ...}`` 形状放在
        ``data`` 里（与 chat_poll 的消费方式一致）。只查顶层 ``ok`` 会
        把 handler 错误吞成空结果。
        """
        if not isinstance(reply, dict):
            return None
        data = reply.get("data")
        if isinstance(data, dict) and data.get("status") == "error":
            return str(data.get("error") or "unknown worker error")
        return None

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
        # 诊断 (v0.13): 每个 attempt 独立计时 + report_id / matched_count
        # 信息,便于排查 cshim discovery race / 240s timeout 之类阻塞。
        import time as _t_cshim
        _t_total = _t_cshim.monotonic()
        _matched_count = self._matched_count()
        LOG.info(
            "rpc-proxy[%s]: _call_resilient START action=%s timeout_ms=%d "
            "soft_retries=%d hard_retries=%d matched_count(before)=%d "
            "payload_keys=%s",
            self._model_id, payload.get("action"),
            timeout_ms, soft_retries, hard_retries, _matched_count,
            sorted(payload.keys()),
        )
        # hard_retries=0 表示"不重试",但首次调用必须执行(否则函数
        # 返回初始值 {"ok": False, "error": "unknown"},掩盖真实错误)。
        # 用 ``max(1, hard_retries)`` 强制至少 1 次 call,真正不重试
        # 是指 attempt > 0 时不再重试 (loop 里 attempt 从 0 开始,只跑一次)。
        for attempt in range(max(1, hard_retries)):
            _t_attempt = _t_cshim.monotonic()
            if attempt > soft_retries:
                LOG.warning(
                    "rpc-proxy[%s]: %d consecutive transient failures — "
                    "recreating cshim client (matched before=%d)",
                    self._model_id, attempt,
                    self._matched_count(),
                )
                self._recreate_client()
                # warm-up 加速新 requester 的 SEDP 匹配；失败无碍
                try:
                    self._client.call({"action": "info"},
                                      timeout_ms=timeout_ms)
                except Exception as e:  # noqa: BLE001
                    LOG.debug("rpc-proxy[%s]: post-recreate warmup raised "
                              "%s", self._model_id, e)
            LOG.info(
                "rpc-proxy[%s]: _call_resilient ATTEMPT %d/%d enter "
                "matched_count=%d",
                self._model_id, attempt + 1, hard_retries,
                self._matched_count(),
            )
            r = self._client.call(payload, timeout_ms=timeout_ms)
            _elapsed_ms = (_t_cshim.monotonic() - _t_attempt) * 1000.0
            _r_req_id = r.get("request_id") if isinstance(r, dict) else None
            _r_err = str(r.get("error", "")) if isinstance(r, dict) else "?"
            _r_ok = bool(r.get("ok")) if isinstance(r, dict) else False
            _r_has_data = (
                isinstance(r, dict)
                and isinstance(r.get("data"), dict)
                and bool(r.get("data"))
            )
            LOG.info(
                "rpc-proxy[%s]: _call_resilient ATTEMPT %d/%d exit "
                "elapsed=%.0fms ok=%s request_id=%s data=%s error=%r",
                self._model_id, attempt + 1, hard_retries,
                _elapsed_ms, _r_ok, _r_req_id, _r_has_data, _r_err,
            )
            if r.get("ok") or not r.get("error"):
                LOG.info(
                    "rpc-proxy[%s]: _call_resilient SUCCESS total=%.0fms "
                    "after attempt %d/%d",
                    self._model_id,
                    (_t_cshim.monotonic() - _t_total) * 1000.0,
                    attempt + 1, hard_retries,
                )
                return r
            err = str(r.get("error", "")).lower()
            last = r
            if not any(k in err for k in ("timeout", "no data",
                                          "send failed")):
                LOG.info(
                    "rpc-proxy[%s]: _call_resilient NON-TRANSIENT error "
                    "(no retry): %r",
                    self._model_id, err,
                )
                return r
            # 瞬时错误 (timeout/no data/send failed) — log 决策
            LOG.warning(
                "rpc-proxy[%s]: _call_resilient attempt %d/%d TRANSIENT "
                "error after %.0fms: %r (matched_count=%d, will retry)",
                self._model_id, attempt + 1, hard_retries,
                _elapsed_ms, err, self._matched_count(),
            )
            time.sleep(self._POLL_RETRY_SLEEP)
        LOG.error(
            "rpc-proxy[%s]: _call_resilient EXHAUSTED %d attempts total=%.0fms "
            "last_error=%r matched_count(after)=%d",
            self._model_id, hard_retries,
            (_t_cshim.monotonic() - _t_total) * 1000.0,
            last.get("error"), self._matched_count(),
        )
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
            # reset_handler 立即返回 {"status":"reset"}/{"status":"noop"},5s 足够。
            r = self._client.call({"action": "reset"}, timeout_ms=5_000)
            if not r.get("ok"):
                LOG.warning("reset on %s returned: %s", self._model_id, r)
        except Exception as e:  # noqa: BLE001
            LOG.warning("reset on %s raised: %s", self._model_id, e)

    def close(self) -> None:
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    def _matched_count(self) -> int:
        """返回当前 cshim requester 的 rep_reader matched count,用于
        排查 discovery race / SEDP 状态。返回 -1 表示 cshim 不可用 / query 失败。
        """
        try:
            client = getattr(self, "_client", None)
            if client is None:
                return -1
            native = getattr(client, "_native", None)
            svc = getattr(client, "_service", None)
            hub = getattr(svc, "_hub", None) if svc is not None else None
            lib = getattr(hub, "_lib", None) if hub is not None else None
            if native is None or lib is None:
                return -1
            return int(lib.wdndds_requester_matched_count(native))
        except Exception as e:  # noqa: BLE001
            LOG.debug("rpc-proxy[%s]: matched_count query failed: %s",
                      self._model_id, e)
            return -1

    def start_matched_sampler(self, interval_s: float = 5.0) -> None:
        """启动 daemon 线程,周期性 LOG matched_count (5s 默认),用于
        排查 cshim discovery race 时看 SEDP 状态演化。仅第一次调用生效。

        日志级别 = DEBUG —— 排查时需手动
            ``logging.getLogger("app.services.rpc_session_proxy").setLevel(logging.DEBUG)``
        才看得到。默认 INFO 下不会刷屏。生产默认不调用,排查用时手动启用。
        """
        if getattr(self, "_sampler_started", False):
            return
        self._sampler_started = True
        import threading as _thr
        def _loop():
            while True:
                try:
                    mc = self._matched_count()
                    LOG.debug(
                        "rpc-proxy[%s] MATCHED_SAMPLE: matched_count=%d "
                        "(call_active=%s)",
                        self._model_id, mc,
                        self._client is not None,
                    )
                except Exception as e:  # noqa: BLE001
                    LOG.debug("rpc-proxy[%s] sampler raised: %s",
                              self._model_id, e)
                _thr.Event().wait(timeout=interval_s)
        _thr.Thread(target=_loop, name=f"rpc-match-sampler-{self._model_id}",
                    daemon=True).start()
        LOG.debug(
            "rpc-proxy[%s] MATCHED_SAMPLE started (interval=%.1fs)",
            self._model_id, interval_s,
        )

    def chat_run(self, history, text, *, context_limit=None,
                 max_new_tokens=2048, on_delta=None, stop_event=None,
                 poll_interval_ms=150, timeout_ms=60_000,
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
        # 所有 wire RPC 超时统一 5s —— 健康 RPC 应在 < 5s 返 reply,
        # 超出意味着 DDS 通道 wedge / worker 异常 / 计算资源争用。
        # 不重试直接 raise,让上层立即感知(不掩盖 bug)。
        reply = self._call_resilient(payload, timeout_ms=5_000,
                                     soft_retries=0, hard_retries=0)
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
            # v0.15 LAN peer flag: 主进程在 ``_is_remote=True`` 时 (跨机
            # worker) 透传, worker 端跳过所有 /dev/shm path-list 路由
            # (4 个 SHM 块全 drop), 避免 FastCDR 255 字节截断崩 wire。
            "is_remote": self._is_remote,
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
# infer 路径走 ``_call_resilient``：
        # wire RPC 超时:infer 走 15s —— 与 video_file_analysis
        # ._INFER_TIMEOUT_SEC 对齐 (方案B)。Qwen3.5-VL-4B 在
        # schema-constrained 模式有 ~46% 概率复述 SDK 注入的 schema
        # definition (schema-echo, 详见 commit 5c33fb3), 稳态 5.5-6s,
        # 但真机 NPU 暂忙 / prefill 长 偶尔跑到 10-12s。5c33fb3 设
        # 5s → 后改 10s → 这次 15s 给 5s 余量。兜底: _on_call_timeout
        # ≥2 次连续 timeout → worker restart。其他 wire RPC (info /
        # reset / chat_stop) 保持 5s 不变。不重试不延后,立即 raise 让上层
        # (detector / VFA / chat) 走错误处理路径,而不是花 720s 等 worker。
        reply = self._call_resilient(payload, timeout_ms=15_000,
                                     soft_retries=0, hard_retries=0)
        if not reply.get("ok"):
            err = reply.get("error", "unknown error")
            LOG.warning(
                "rpc-proxy[%s]: infer failed (image_size=%r): %s",
                self._model_id, _image_size, err,
            )
            raise RuntimeError(
                f"compute_worker[{self._model_id}] infer failed: {err}")
        biz_err = self._worker_business_error(reply)
        if biz_err:
            # handler 业务错误（session not loaded / SDK 抛错 / 参数校验）
            # —— wire 顶层 ok 是 True，不在这里 raise 就会被解析成空
            # _ProxyResult，detector 端静默丢弃。
            LOG.warning(
                "rpc-proxy[%s]: infer handler error (image_size=%r): %s",
                self._model_id, _image_size, biz_err,
            )
            raise RuntimeError(
                f"compute_worker[{self._model_id}] infer failed: {biz_err}")
        data = reply.get("data") or {}
        # v0.12 修复：worker 把 heavy payload（crop JPEG / result_data JSON）
        # 写到 /dev/shm 并把 path + size 走 wire 顶替内联 base64 / 大 JSON，
        # 避免 ComputeReply 单 string 字段 255 字节上限触发 FastCDR 截断。
        # 这里把 ``crop_path`` / ``result_data_path`` 还原成原来的内联形式，
        # 后续 detector / VFA 代码看到的还是 ``crop_b64`` / ``result_data``。
        data = self._decode_reply_shm(data)
        # 诊断：worker 已经把 reply 发回主进程，记录 data 的 keys / 体积
        # 区分 "worker 算出但 reply 格式坏" vs "正常 reply"
        try:
            import json as _json_diag
            _data_str = _json_diag.dumps(data, ensure_ascii=False)
            _data_bytes = len(_data_str.encode("utf-8"))
        except Exception:
            _data_bytes = -1
        LOG.info(
            "rpc-proxy[%s]: infer reply received (image_size=%r) "
            "data_keys=%s bytes=%d text_len=%d",
            self._model_id, _image_size,
            sorted(data.keys()) if isinstance(data, dict) else type(data).__name__,
            _data_bytes,
            len(data.get("text", "") or "") if isinstance(data, dict) else 0,
        )

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
        # 诊断：worker 端 model_dump 后传回的 result_data 是 detector
        # 后续分支（VLMDoc / TargetDoc / TargetEvent 重建）的唯一依据。
        # 一旦 worker 端 dump 失败 / data 类型不对 / 字段缺失，主进程
        # 会拿到 _ProxyResult 而不是真 schema 实例，触发 detector 端
        # "result not an instance of VLMDoc" 的 warning。预先在这里把
        # 缺失 / 类型异常打出来，避免上游再去反推。
        if gen_name and gen_name != "TargetDoc" and isinstance(
                result_data, dict) and result_data:
            LOG.warning(
                "proxy: gen_name=%s delivered with structured result_data — "
                "only TargetDoc is reconstructed here; other schema will "
                "fall through to _ProxyResult (fields=%s)",
                gen_name, sorted(result_data.keys())[:8],
            )
        # ------------------------------------------------------------------ #
        # 诊断 (v0.13): gen_name=TargetDoc 但 result_data 缺失 = 模型输出     #
        # 没解析成 TargetDoc schema (worker 端 silent 跳过 model_dump)。     #
        # 这条路径是用户报告"概率性目标检测失败"的核心可疑路径之一:          #
        #   worker.infer_handler 在 result 是 str 且 gen_name 非 None 时      #
        #   跳过了 result_data 块,主进程这里又拿不到 dict,TargetDoc 重建    #
        #   路径走不进去,fallback 到 _ProxyResult。 detector 端拿到          #
        #   _ProxyResult 后 isinstance(_, TargetDoc) 永假 → fall through    #
        #   到 VLMDoc 分支,把 raw text 当 VLMDoc.text 写出 (无 bbox/crop)。 #
        # 现在补 ERROR 日志 + 让 _ProxyResult 携带 schema_parse_failed       #
        # marker 透传给 detector,detector 在 fall-through 写事件时给 text     #
        # 加 ``[解析失败]`` 前缀,前端历史记录里能直接看到是"目标检测解析     #
        # 失败"而不是普通 VLM 文本。                                          #
        # ------------------------------------------------------------------ #
        if gen_name == "TargetDoc" and not isinstance(result_data, dict):
            LOG.error(
                "rpc-proxy[%s]: TargetDoc result_data MISSING (gen_name=%s, "
                "result_data_type=%s, reply_keys=%s, text_len=%d) — worker "
                "端 schema parse 失败,silent fallback 到 _ProxyResult。 "
                "raw_reply_text=%r",
                self._model_id, gen_name,
                type(result_data).__name__ if result_data is not None else "None",
                sorted(data.keys()) if isinstance(data, dict) else type(data).__name__,
                len(data.get("text", "") or "") if isinstance(data, dict) else 0,
                data.get("text", "") if isinstance(data, dict) else None,
            )
        if gen_name == "TargetDoc" and isinstance(result_data, dict):
            try:
                from app.services.detector import _get_target_doc_cls
                TargetDoc = _get_target_doc_cls()
                # v0.14 schema-version 兼容:老 worker 进程仍是 v1
                # (x1/y1/x2/y2 4 独立 field) schema 时,model_dump 出来
                # 的 dict 不含 ``bbox`` 字段;主进程 cache 是 v2 schema
                # (期望 bbox:list[float]) → 直接 model_validate 会失败。
                # 桥接:dict 含 x1/y1/x2/y2 时,内部合成 bbox 字段重试。
                converted = result_data
                if "bbox" not in result_data:
                    _xy = (
                        result_data.get("x1"),
                        result_data.get("y1"),
                        result_data.get("x2"),
                        result_data.get("y2"),
                    )
                    if all(isinstance(v, (int, float)) for v in _xy):
                        converted = dict(result_data)
                        converted["bbox"] = [float(v) for v in _xy]
                return TargetDoc.model_validate(converted)
            except Exception as e:  # noqa: BLE001
                LOG.error(
                    "rpc-proxy[%s]: TargetDoc reconstruct failed (%s: %s) — "
                    "raw_result_data=%r — falling back to _ProxyResult",
                    self._model_id, type(e).__name__, e, result_data,
                )

        # ------------------------------------------------------------------ #
        # 诊断 (v0.13): 把 worker 端写入的 schema_parse_failed marker 透传     #
        # 给 _ProxyResult._raw,detector 端可读 ``_raw`` 字段识别。           #
        # ------------------------------------------------------------------ #
        _schema_parse_failed = bool(data.get("schema_parse_failed"))

        return _ProxyResult(
            text=data.get("text"),
            level=data.get("level"),
            tool_call=data.get("tool_call"),
            bbox=data.get("bbox"),
            crop_b64=data.get("crop_b64"),
            attachment=attachments,
            raw=data,
            schema_parse_failed=_schema_parse_failed,
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
            timeout_ms=5_000,
        )
        if not reply.get("ok"):
            err = reply.get("error", "unknown error")
            raise RuntimeError(
                f"compute_worker[{self._model_id}] embed_text failed: {err}")
        biz_err = self._worker_business_error(reply)
        if biz_err:
            raise RuntimeError(
                f"compute_worker[{self._model_id}] embed_text failed: {biz_err}")
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
                timeout_ms=5_000,
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
                timeout_ms=5_000,
            )
        if not reply.get("ok"):
            err = reply.get("error", "unknown error")
            raise RuntimeError(
                f"compute_worker[{self._model_id}] embed_video failed: {err}")
        biz_err = self._worker_business_error(reply)
        if biz_err:
            # worker 端 imread / SDK 失败（如 cv2.imread can't open/read）
            # —— 不在这里 raise 就会静默返回空向量，上层
            # wechat_video_manager 的 norm 检查再静默跳过，最终只报
            # "0/10 段成功" 而真因全无。
            LOG.warning(
                "rpc-proxy[%s]: embed_video handler error: %s",
                self._model_id, biz_err)
            raise RuntimeError(
                f"compute_worker[{self._model_id}] embed_video failed: {biz_err}")
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
                from wdnai import (
                    Attachment as _WdnaiAttach,
                    AudioAttachment as _WdnaiAudio,
                    ImageAttachment as _WdnaiImage,
                    TextAttachment as _WdnaiText,
                )
                if isinstance(item, _WdnaiImage):
                    # Multi-frame ImageAttachment — each frame ships as
                    # its own wire entry (data_path for local worker /
                    # data_b64 JPEG for LAN peer). Worker reconstructs
                    # one ImageAttachment per frame.
                    for img in item.images:
                        if self._is_remote:
                            out.append(_bgr_ndarray_to_b64(img))
                        else:
                            out.append(_bgr_ndarray_to_path(img))
                    continue
                if isinstance(item, _WdnaiText):
                    # Text prefix — sent as a plain string. The worker's
                    # wire decoder falls through to the text branch.
                    out.append(item.text)
                    continue
                if isinstance(item, _WdnaiAudio):
                    # Audio bytes — wrap with kind so the worker decoder
                    # routes to the audio branch (otherwise it would be
                    # mistaken for a base64 image).
                    out.append({
                        "data_b64": base64.b64encode(
                            bytes(item.data)).decode("ascii"),
                        "kind":     "audio",
                    })
                    continue
                # Generic Attachment (custom subclass / duck-typed
                # _AttachmentProxy from older code paths) — fall back to
                # the historical ``getattr`` probing so existing
                # serialisation keeps working.
                if isinstance(item, _WdnaiAttach):
                    if getattr(item, "images", None):
                        for img in item.images:
                            if self._is_remote:
                                out.append(_bgr_ndarray_to_b64(img))
                            else:
                                out.append(_bgr_ndarray_to_path(img))
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
    proxy = _RpcSessionProxy(rpc_client, model_id, _svc=_svc,
                             on_client_recreated=on_client_recreated,
                             is_remote=is_remote)
    # 诊断 (v0.13) sampler 默认不启动 — 每实例每 5s 一条 INFO log,
    # 多实例叠加刷屏严重。排查 cshim discovery race 时手动:
    #     proxy.start_matched_sampler(interval_s=5.0)
    # (启动后 log 仍为 DEBUG 级,需调 logger 级别才可见。)
    return proxy


# Public alias
RpcSessionProxy = _RpcSessionProxy
