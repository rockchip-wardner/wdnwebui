"""微信视频管理器 —— 接收视频消息、落盘、后台切片 embedding、完成通知。

设计要点（与 "Chat 工具调用 (chat-agent)" 的 VideoSearch 工具配合）：

* **确定性触发**：视频提取是消息协议层的确定性行为，不走 LLM 工具
  链路 —— ``wechat_llm.handle_incoming`` 对 video 消息直接分流到
  :meth:`enqueue_video`（纯内存入队，毫秒级，不阻塞回调线程）。
* **单槽注册表**：每个 user_id 只保留最新一个视频（新视频到达即覆盖
  旧的：删落盘文件 + 释放旧索引）。串行队列中过期的任务自动跳过。
* **异步性挪出工具链路**：收到视频即自动提取，LLM 对话中的
  ``VideoSearch`` 工具保持纯同步查询 —— 四态（ok / extracting /
  downloading / no_video）由 :meth:`entry_status` 与 :meth:`search`
  反馈给模型。
* **进度感知 A+C**：推送侧节点播报（开始确认 / 完成 / 失败说明，
  固定文案零 LLM 成本）；拉取侧工具返回实时 progress。
* **不复用 UI 页 VCS 单例**：``VideoContentSearchService`` 是全局
  单源状态机，多用户多视频会互相覆盖。本模块复用其底层函数
  （``_probe_video_meta`` / ``_read_chunk_frames`` /
  ``_write_frames_to_shm`` / ``_cosine_similarity``），维护独立的多
  用户注册表；UI 页互不干扰。

切片参数：6s 片 / 1s 重叠（独立于 UI 页默认值）。检索：threshold
0.5 固定 + 兜底返回最接近的 3 段并标记 ``all_below_threshold``，
质量信号透出给 LLM 自行表述。
"""
from __future__ import annotations

import logging
import os
import queue as _queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from app.services.video_content_search import (
    _cleanup_shm_session,
    _cosine_similarity,
    _probe_video_meta,
    _read_chunk_frames,
    _write_frames_to_shm,
)

_LOG = logging.getLogger("wechat_video_manager")

_CHUNK_SECONDS = 6
_CHUNK_OVERLAP_SECONDS = 1
_SIM_THRESHOLD = 0.5
_FALLBACK_MIN_RESULTS = 3
_SEARCH_TOP_K = 5
_DOWNLOAD_RETRIES = 3
_RETRY_BACKOFF_SECONDS = (2, 4, 8)
_DEFAULT_MAX_SIZE_MB = 200
_DEFAULT_MAX_DURATION_MIN = 10

_STATE_DOWNLOADING = "downloading"
_STATE_EXTRACTING = "extracting"
_STATE_READY = "ready"
_STATE_ERROR = "error"

_UPLOAD_ROOT = Path(__file__).resolve().parent / "uploads" / "wechat_videos"


@dataclass
class VideoEntry:
    """单个用户当前持有的视频素材（单槽，新视频覆盖旧的）。"""

    seq: int                      # 单调递增，过期任务判定
    user_id: str
    filename: str                 # 展示用名（video-YYYYmmdd-HHMMSS）
    path: str                     # 落盘绝对路径
    ts: float
    state: str = _STATE_DOWNLOADING
    progress: float = 0.0         # extracting 阶段 0-1
    error: str = ""
    fps: float = 0.0
    duration_s: float = 0.0
    index: List[dict] = field(default_factory=list)
    slices_total: int = 0
    slices_embedded: int = 0


def _fmt_ts(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    return f"{s // 60:02d}:{s % 60:02d}"


class WechatVideoManager:
    """单例。后台 worker 串行消费：download → probe → 切片 embedding。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: Dict[str, VideoEntry] = {}
        self._seq = 0
        self._queue: "_queue.Queue[Optional[tuple]]" = _queue.Queue()
        self._worker = threading.Thread(
            target=self._worker_loop, name="wechat-video-manager", daemon=True,
        )
        self._worker.start()

    # ------------------------------------------------------------ 注册入口
    def enqueue_video(self, user_id: str, msg: Any) -> dict:
        """消息回调线程调用 —— 纯内存入队（毫秒级），下载在 worker 做。

        单槽覆盖：新视频到达即作废旧 entry（尽力删除落盘文件）；队列里
        未开始的旧任务因 seq 不匹配被跳过。
        """
        with self._lock:
            self._seq += 1
            seq = self._seq
            old = self._entries.get(user_id)
            if old is not None and old.path:
                try:
                    os.unlink(old.path)
                except OSError:
                    pass
                _LOG.info("wechat video: dropped previous video of %s (%s)",
                          user_id, old.filename)
            filename = time.strftime("video-%Y%m%d-%H%M%S")
            entry = VideoEntry(
                seq=seq, user_id=user_id, filename=filename,
                path="", ts=time.time(),
            )
            self._entries[user_id] = entry
        self._queue.put((user_id, seq, msg))
        _LOG.info("wechat video: enqueued %s for %s (seq=%d)",
                  filename, user_id, seq)
        return {"seq": seq, "filename": filename}

    # ------------------------------------------------------------ 状态查询
    def entry_status(self, user_id: str) -> Optional[dict]:
        """给 VideoSearch 工具的状态快照（不含索引向量）。None=无视频。"""
        with self._lock:
            entry = self._entries.get(user_id)
            if entry is None:
                return None
            return {
                "state": entry.state,
                "progress": round(entry.progress, 3),
                "filename": entry.filename,
                "duration_s": round(entry.duration_s, 1),
                "error": entry.error,
                "slices_embedded": entry.slices_embedded,
                "slices_total": entry.slices_total,
            }

    def search(self, user_id: str, text: str, top_k: int = _SEARCH_TOP_K) -> dict:
        """对最新 ready 视频做文本检索（threshold 固定 + 兜底质量信号）。"""
        text = (text or "").strip()[:50]
        if not text:
            raise ValueError("检索问题不能为空")
        top_k = max(1, min(10, int(top_k or _SEARCH_TOP_K)))
        with self._lock:
            entry = self._entries.get(user_id)
            if entry is None or entry.state != _STATE_READY or not entry.index:
                raise ValueError("该用户没有已就绪的视频索引")
            index = list(entry.index)
            source_name = entry.filename
            duration_s = entry.duration_s

        from app.services.compute_registry import (
            get_registry, get_session_call_lock,
        )
        session = get_registry().get_session("swcap_video_retrieval")
        with get_session_call_lock():
            qvec = session.embed_text(text)
        qvec = np.asarray(qvec, dtype=np.float32)
        qvec = qvec / (np.linalg.norm(qvec) + 1e-9)

        scored = sorted(
            ((_cosine_similarity(qvec, e["vec"]), e) for e in index),
            key=lambda t: -t[0])
        hits: List[dict] = []
        fallback: List[dict] = []
        for sim, e in scored:
            item = {
                "time": f"{_fmt_ts(e['start_time'])}-{_fmt_ts(e['end_time'])}",
                "similarity": round(float(sim), 4),
                "above_threshold": bool(sim >= _SIM_THRESHOLD),
            }
            if sim >= _SIM_THRESHOLD:
                hits.append(item)
                if len(hits) >= top_k:
                    break
            elif len(fallback) < _FALLBACK_MIN_RESULTS:
                fallback.append(item)
        all_below = not hits
        if all_below and fallback:
            hits = fallback
        return {
            "status": "ok",
            "video": source_name,
            "duration_s": round(duration_s, 1),
            "hits": hits,
            "top_score": hits[0]["similarity"] if hits else None,
            "all_below_threshold": all_below,
            "hint": (
                f"所有片段与问题的相似度均低于{_SIM_THRESHOLD}，以上为最接近的"
                f"{len(hits)}段，请谨慎表述匹配程度" if all_below else ""),
        }

    # ------------------------------------------------------------ 后台 worker
    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                break
            user_id, seq, msg = item
            try:
                self._process(user_id, seq, msg)
            except Exception as exc:  # noqa: BLE001
                _LOG.exception("wechat video pipeline failed for %s", user_id)
                self._fail(user_id, seq, f"{type(exc).__name__}: {exc}")

    def _entry_of(self, user_id: str, seq: int) -> Optional[VideoEntry]:
        with self._lock:
            entry = self._entries.get(user_id)
            if entry is None or entry.seq != seq:
                return None   # 已被更新的视频覆盖 → 过期任务跳过
            return entry

    def _download_video(self, user_id: str, entry: VideoEntry, msg: Any) -> str:
        """CDN 下载并落盘，返回本地路径。瞬时网络故障自动重试。

        CDN 传输偶发中断（ContentLength 不满足 / SSL bad_record_mac 等，
        真机实测复现），SDK 的 ``resp.read()`` 无重试 —— 这里按指数退避
        重发同一条 msg 的下载请求（CDN 凭证为静态参数，秒级窗口内复用
        无碍）；每次重试前推送节点通知保持过程可见。SDK 返回 None
        （无可下载媒体，确定性结果）不重试，直接失败。
        """
        from app.services.wechat import get_wechat

        data_obj = None
        last_err: Optional[Exception] = None
        for attempt in range(1, _DOWNLOAD_RETRIES + 1):
            try:
                data_obj = get_wechat().download_message(msg)
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt >= _DOWNLOAD_RETRIES:
                    raise RuntimeError(
                        f"视频下载在 {_DOWNLOAD_RETRIES} 次尝试后仍失败："
                        f"{type(exc).__name__}: {exc}") from exc
                wait = _RETRY_BACKOFF_SECONDS[
                    min(attempt - 1, len(_RETRY_BACKOFF_SECONDS) - 1)]
                self._notify(
                    user_id,
                    f"⚠️ 视频 {entry.filename} 下载中断"
                    f"（{type(exc).__name__}），{wait} 秒后自动重试"
                    f"（第 {attempt} 次重试，共 {_DOWNLOAD_RETRIES - 1} 次）…")
                time.sleep(wait)
        if data_obj is None or not getattr(data_obj, "data", None):
            raise RuntimeError("微信未返回可下载的视频内容")
        out_dir = _UPLOAD_ROOT / user_id
        out_dir.mkdir(parents=True, exist_ok=True)
        # 扩展名优先级：SDK format → 原始上传文件名后缀（"文件"形态发送
        # 时 SDK 不填 format）→ 默认 mp4
        orig_name = ""
        for f in getattr(msg, "files", []) or []:
            orig_name = getattr(f, "file_name", "") or ""
            break
        ext = ((getattr(data_obj, "format", "") or "")
               or os.path.splitext(orig_name)[1].lstrip(".")
               or "mp4").lstrip(".").lower() or "mp4"
        path = out_dir / f"{entry.filename}.{ext}"
        path.write_bytes(bytes(data_obj.data))
        _LOG.info("wechat video: downloaded %s (%.1f KiB) for %s",
                  path.name, len(data_obj.data) / 1024, user_id)
        return str(path)

    def _fail(self, user_id: str, seq: int, err: str) -> None:
        with self._lock:
            entry = self._entries.get(user_id)
            if entry is None or entry.seq != seq:
                return
            entry.state = _STATE_ERROR
            entry.error = err
        self._notify(
            user_id,
            f"⚠️ 视频 {entry.filename} 分析失败：{err}\n"
            f"可重新发送视频再试一次。")

    def _notify(self, user_id: str, text: str) -> None:
        """节点播报（固定文案）。NoContextError 等发送失败仅记日志。"""
        try:
            from app.services.wechat import get_wechat
            get_wechat().send_text(user_id, text)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("wechat video notify to %s failed: %r", user_id, exc)

    # ------------------------------------------------------------ 提取管线
    def _resolve_limits(self) -> "tuple[int, float]":
        """热读 ``config.compute.wechat_video`` 大小/时长上限。

        缺失 / 非法 / ≤0 回退默认（200 MB / 10 分钟）；每次管线执行时
        读取 —— 改配置即时生效无需重启。
        """
        max_size_mb, max_duration_min = _DEFAULT_MAX_SIZE_MB, \
            _DEFAULT_MAX_DURATION_MIN
        try:
            from app.services.config import get_config
            cfg = get_config().get_section("compute", {})
            wv = cfg.get("wechat_video") if isinstance(cfg, dict) else None
            if isinstance(wv, dict):
                v = int(wv.get("max_size_mb", max_size_mb))
                if v > 0:
                    max_size_mb = v
                f = float(wv.get("max_duration_min", max_duration_min))
                if f > 0:
                    max_duration_min = f
        except Exception:  # noqa: BLE001
            pass
        return max_size_mb * 1024 * 1024, max_duration_min * 60.0

    @staticmethod
    def _precheck_msg_meta(msg: Any, max_bytes: float,
                           max_seconds: float) -> Optional[str]:
        """下载前预检：利用 SDK 消息元数据拦截明显超限的视频。

        video 形态带 ``duration_ms`` 无 size；file 形态带 ``size`` 无
        duration —— 两者互补覆盖。返回拒绝原因，通过则 None。
        """
        for v in getattr(msg, "videos", []) or []:
            ms = getattr(v, "duration_ms", None)
            if ms and ms / 1000.0 > max_seconds:
                return (f"视频时长 {_fmt_ts(ms / 1000.0)} 超过上限 "
                        f"{_fmt_ts(max_seconds)}，请裁剪后重新发送")
            break
        for f in getattr(msg, "files", []) or []:
            sz = getattr(f, "size", None)
            if sz and sz > max_bytes:
                return (f"视频大小 {sz / 1024 / 1024:.1f} MB 超过上限 "
                        f"{max_bytes / 1024 / 1024:.0f} MB，"
                        f"请裁剪后重新发送")
            break
        return None

    def _process(self, user_id: str, seq: int, msg: Any) -> None:
        entry = self._entry_of(user_id, seq)
        if entry is None:
            return

        # ---- 0. 资源上限（可配置，默认 200MB / 10 分钟）----
        max_bytes, max_seconds = self._resolve_limits()
        reject = self._precheck_msg_meta(msg, max_bytes, max_seconds)
        if reject:
            raise RuntimeError(reject)

        # ---- 0.5 embedding 会话最终防线：handle_incoming 已做 capability
        # 预检，这里再取一次防 TOCTOU 窗口（预检通过后、下载期间被卸载）。
        # 放在下载之前 —— 未启用时连流量都不浪费。
        from app.services.compute_registry import (
            get_registry, get_session_call_lock,
        )
        session = get_registry().get_session("swcap_video_retrieval")
        lock = get_session_call_lock()

        # ---- 1. 固定文案确认（A 组合播报节点）----
        self._notify(
            user_id,
            f"📥 已收到视频 {entry.filename}，正在下载与分析，完成后会通知"
            f"你。期间可以直接问我分析进度。")

        # ---- 2. CDN 下载（SDK 透传，120s 上限）----
        path = self._download_video(user_id, entry, msg)
        # 大小复核：video 形态消息无 size 元数据，预检拦不到，此处兜底
        fsize = os.path.getsize(path)
        if fsize > max_bytes:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise RuntimeError(
                f"视频大小 {fsize / 1024 / 1024:.1f} MB 超过上限 "
                f"{max_bytes / 1024 / 1024:.0f} MB，请裁剪后重新发送")
        with self._lock:
            entry.path = str(path)
        _LOG.info("wechat video: downloaded %s for %s",
                  Path(path).name, user_id)

        # ---- 3. 元数据探测 ----
        meta = _probe_video_meta(str(path))
        if meta is None or meta.get("fps", 0) <= 0:
            raise RuntimeError("无法读取视频元数据，文件可能损坏或格式不支持")
        fps = meta["fps"]
        duration_s = (meta["duration_s"] if meta["duration_s"] > 0
                      else meta["total_frames"] / fps)
        # 时长复核（最终防线）：file 形态无时长元数据，预检拦不到
        if duration_s > max_seconds:
            raise RuntimeError(
                f"视频时长 {_fmt_ts(duration_s)} 超过上限 "
                f"{_fmt_ts(max_seconds)}，请裁剪后重新发送")
        step = _CHUNK_SECONDS - _CHUNK_OVERLAP_SECONDS
        total = max(1, int((max(0.0, duration_s - _CHUNK_OVERLAP_SECONDS))
                           // step) + 1)
        with self._lock:
            entry.fps = fps
            entry.duration_s = duration_s
            entry.state = _STATE_EXTRACTING
            entry.slices_total = total
        self._notify(user_id, f"⏳ 开始分析 {entry.filename}"
                              f"（{duration_s:.0f} 秒，共 {total} 段）…")

        # ---- 4. 切片 embedding（复用 VCS 底层函数 + SHM 帧传递；
        #         session/lock 已在步骤 0.5 获取）----
        shm_session_id = f"wxv{seq:x}"
        chunk_idx = 0
        start = 0.0
        while start < duration_s:
            frames = _read_chunk_frames(str(path), start, _CHUNK_SECONDS, fps)
            if not frames:
                break
            end_time = start + len(frames)
            frame_paths = _write_frames_to_shm(frames, shm_session_id, chunk_idx)
            try:
                vec = None
                for _attempt in range(2):
                    try:
                        with lock:
                            vec = session.embed_video(frame_paths, prompt="")
                        break
                    except RuntimeError as rpc_err:
                        if ("timeout" not in str(rpc_err).lower()
                                or _attempt == 1):
                            raise
                        _LOG.warning("chunk %d embed timeout (discovery race),"
                                     " retrying", chunk_idx)
                vec = np.asarray(vec, dtype=np.float32)
                norm = float(np.linalg.norm(vec))
                if norm > 1e-6:
                    vec = vec / norm
                    with self._lock:
                        entry.index.append({
                            "start_time": round(start, 2),
                            "end_time": round(end_time, 2),
                            "chunk_idx": chunk_idx,
                            "fps": fps,
                            "vec": vec,
                        })
                        entry.slices_embedded += 1
                        entry.progress = (
                            entry.slices_embedded / total if total else 1.0)
            finally:
                for _p in frame_paths:
                    try:
                        os.unlink(_p)
                    except OSError:
                        pass
            chunk_idx += 1
            start += step
            if end_time >= duration_s:
                break
        _cleanup_shm_session(shm_session_id)

        # ---- 5. 终态判定 + 完成通知 ----
        with self._lock:
            if not entry.index:
                raise RuntimeError(
                    f"提取完成但 0/{total} 段成功 embedding — "
                    "所有切片解码或推理失败")
            entry.state = _STATE_READY
            entry.progress = 1.0
            n_hits_src = len(entry.index)
        _LOG.info("wechat video: ready %s for %s — %d/%d segments",
                  entry.filename, user_id, n_hits_src, total)
        self._notify(
            user_id,
            f"✅ 视频 {entry.filename} 分析完成（{n_hits_src}/{total} 段），"
            f"现在可以针对它提问了。")

    # ------------------------------------------------------------ shutdown
    def close(self) -> None:
        self._queue.put(None)

    def bot_status(self) -> dict:
        """调试用：各用户当前 entry 概览（不含向量）。"""
        with self._lock:
            return {
                uid: {
                    "state": e.state, "filename": e.filename,
                    "progress": round(e.progress, 2),
                    "segments": f"{e.slices_embedded}/{e.slices_total}",
                } for uid, e in self._entries.items()
            }


_manager: Optional[WechatVideoManager] = None
_manager_lock = threading.Lock()


def get_wechat_video_manager() -> WechatVideoManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = WechatVideoManager()
        return _manager


def close_wechat_video_manager() -> None:
    """shutdown hook —— no-op if never created。"""
    global _manager
    with _manager_lock:
        if _manager is None:
            return
        _manager.close()
        _manager = None
