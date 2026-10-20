"""视频内容检索 — 真实后端（Qwen3-VL-Embedding-2B）。

流程（对齐 vl_rag_demo 的语义，但为**单视频 + 内存索引**版本）：

1. ``open(source)`` —— 记录视频文件路径，校验存在。
2. ``start_extract()`` —— 切片参数固化为 4s / 重叠 1s，后台 daemon
   线程用 PyAV 按固定时间窗切片，每片取等距帧缩放到 576×576，经 wdnai
   ``LLMSession.embed_video`` 得到 2048 维归一化向量，存内存
   ``List[{start_time, end_time, chunk_idx, fps, vec}]``。逐片推进
   ``slices_embedded`` 并 WS 广播 ``progress``。
3. ``query(text)`` —— 匹配阈值固定 0.40、检索上限固定 10，
   ``LLMSession.embed_text`` 编码查询向量，遍历内存索引算余弦相似度，
   阈值过滤 + top_k 截断。

LLMSession 从 ComputeRegistry 取（``swcap_video_retrieval`` capability），
与 detector / VAS 分析侧共用进程级 ``get_session_call_lock`` 串行化。

命中项字段（对齐 demo 语义，前端契约不变）：
    {video_path, start_time, end_time, chunk_idx, similarity}
缩略图：由 ``GET /api/video-analysis-search/search/frame`` 端点按
``video_path + start_time`` 实时取帧（PyAV seek + RGA 缩放）。

v0.8：从 ``app/apps/video_search/service.py`` 平移到
``app/core/video/search.py`` —— ``video_analysis_search`` 卡片（分析 +
检索合并页）通过 ``app.core.*`` 引用检索能力，遵守卡片零跨卡片 import
规则。原文件缺失的 ``compute_slices`` / ``_read_chunk_frames``
两个 import 在平移时补齐（此前只在 worker 线程里才触发 NameError）。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Iterator, List, Optional

import numpy as np

from app.core.compute.registry import (
    get_registry,
    get_session_call_lock,
)
from app.core.video.utils import (
    _read_chunk_frames,
    compute_slices,
    cosine_similarity as _cosine_similarity,
    probe_video_meta as _probe_video_meta,
)
from app.core.video.cache import file_md5, get_video_cache

LOG = logging.getLogger(__name__)

_STATE_IDLE = "idle"
_STATE_EXTRACTING = "extracting"
_STATE_READY = "ready"
_STATE_ERROR = "error"

# embedding 模型的 vision 输入边长（Qwen3-VL-Embedding-2B 固定 576×576）
_VISION_SIZE = 576
_EMBED_DIM = 2048

# 检索参数固化 —— 页面与向导不暴露；需要调整时直接改这里的值。
# 取值经 ``start_extract`` / ``query`` 强制覆盖调用方传入值（与
# ``service.ANALYSIS_PROMPT`` 的固化方式一致），保证落盘缓存键与检索口径
# 恒定；历史缓存条目仍按自身记录的切片参数检索。
FIXED_CHUNK_SECONDS = 4
FIXED_CHUNK_OVERLAP_SECONDS = 1
FIXED_SIM_THRESHOLD = 0.40
FIXED_TOP_K = 10


def _score_hits(qvec: np.ndarray, index: List[dict], sim_threshold: float,
                top_k: int, min_results: int = 0) -> "tuple[List[dict], dict]":
    """对一组归一化向量索引做余弦检索（threshold 过滤 + top_k 截断 +
    min_results 兜底）—— ``VideoSearchService.query`` 与
    ``POST /query_multi`` 共用的评分实现。

    返回 ``(hits: [{video_path, start_time, end_time, chunk_idx,
    similarity, above_threshold}], meta: {top_score, above_count,
    below_count, fallback_used})``。
    """
    scored = [(_cosine_similarity(qvec, e["vec"]), e) for e in index]
    scored.sort(key=lambda t: -t[0])
    all_sims = [s for s, _ in scored]
    hits: list = []
    fallback: list = []
    for sim, entry in scored:
        if sim < sim_threshold:
            if min_results > 0 and len(fallback) < min_results:
                fallback.append((sim, entry))
            continue
        hits.append((sim, entry))
        if len(hits) >= top_k:
            break
    fallback_used = False
    if not hits and fallback:
        hits = fallback[:top_k]
        fallback_used = True
        LOG.info("vas-search: query threshold %.2f filtered all hits — fallback "
                 "top-%d (best sim=%.3f)", sim_threshold, len(hits),
                 hits[0][0] if hits else 0.0)
    out = [{
        "video_path": entry.get("video_path") or "",
        "start_time": entry["start_time"],
        "end_time": entry["end_time"],
        "chunk_idx": entry["chunk_idx"],
        "similarity": round(float(sim), 4),
        "above_threshold": bool(sim >= sim_threshold),
    } for sim, entry in hits]
    above_count = sum(1 for s in all_sims if s >= sim_threshold)
    below_count = len(all_sims) - above_count
    top_score = max(all_sims) if all_sims else None
    meta = {
        "top_score": round(float(top_score), 4) if top_score is not None else None,
        "above_threshold_count": above_count,
        "below_threshold_count": below_count,
        "fallback_used": fallback_used,
        "all_sims": [round(float(s), 4) for s in all_sims],
    }
    return out, meta




def iter_segment_jpeg(source: str, start_s: float, end_s: float,
                      max_frames: int = 600, paced: bool = False,
                      fallback_fps: float = 25.0,
                      pause_event=None) -> Iterator[bytes]:
    """按播放顺序生成 [start_s, end_s) 区间内每一帧的 JPEG bytes。

    PyAV 打开一次容器，seek 到 start_s 附近 keyframe 后顺序 decode 到
    end_s；每帧 ``to_ndarray(format="bgr24")`` + cv2 编码 JPEG。供
    ``/api/video-analysis-search/play`` WS 推帧用。生成器在取帧/编码
    异常时提前结束（由 WS handler 补发 error 帧）。

    ``paced=True`` 时按源视频 fps 节流（首帧立即出，后续每帧间隔
    ``1/fps``），fps 缺失回退 ``fallback_fps``。

    ``pause_event``（``threading.Event``，可选）非 None 时：``paced``
    节流每帧前检查，若被 clear 则阻塞等待；唤醒后**重置时钟基准**
    （按首帧重新对齐）—— 否则 ``next_t`` 落后"暂停时长"，恢复播放时
    会连续不 sleep 补帧（burst）。
    """
    import av
    import cv2
    import time

    container = av.open(source, timeout=(2.0, 5.0))
    try:
        stream = container.streams.video[0]
        time_base = float(stream.time_base)
        if time_base <= 0:
            raise RuntimeError("视频流元数据缺失（time_base）")
        fps = float(stream.average_rate) if stream.average_rate else fallback_fps
        interval = 1.0 / max(fps, 1.0) if paced else 0.0
        start_frame = int(round(start_s * fps))
        end_frame = int(round(end_s * fps))
        if end_frame <= start_frame:
            return
        offset = int((start_frame / fps) / time_base)
        try:
            container.seek(offset, backward=True, any_frame=False, stream=stream)
        except Exception:
            pass
        sent = 0
        first = True
        next_t = 0.0
        for frame in container.decode(video=0):
            if frame.pts is None:
                continue
            abs_time = float(frame.pts) * time_base
            frame_number = int(round(abs_time * fps))
            if frame_number < start_frame:
                continue
            if frame_number >= end_frame:
                break
            if sent >= max_frames:
                break
            bgr = frame.to_ndarray(format="bgr24")
            ok, buf = cv2.imencode(
                ".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 85]
            )
            if ok:
                # 绝对时钟对齐节流：编码耗时被 sleep 补偿，帧间隔 ≈ 1/fps
                if paced:
                    # 暂停（pause_event 被 clear）→ 阻塞在此；唤醒后重置
                    # 时钟基准（first=True），避免按暂停时长连续补帧（burst）。
                    if pause_event is not None and not pause_event.is_set():
                        pause_event.wait()
                        first = True
                    now = time.monotonic()
                    if first:
                        next_t = now + interval
                    else:
                        next_t += interval
                        delay = next_t - now
                        if delay > 0:
                            time.sleep(delay)
                    first = False
                sent += 1
                yield bytes(buf)
    finally:
        container.close()



class VideoSearchService:
    """单视频检索状态机 + 后台 worker + WS 广播。

    状态流：
        idle → extracting → ready（提取完成）
        idle → extracting → stopped（中途停止，退回 idle）
    任一阶段出错 → error。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._broadcasters: List[Callable[[dict], None]] = []
        self._broadcaster_lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._stop_worker = threading.Event()

        self._state: str = _STATE_IDLE
        self._error: str = ""
        self._source: str = ""
        self._source_exists: Optional[bool] = None
        self._chunk_seconds: int = FIXED_CHUNK_SECONDS
        self._chunk_overlap_seconds: int = FIXED_CHUNK_OVERLAP_SECONDS
        self._slices_total: int = 0
        self._slices_embedded: int = 0
        self._query: Optional[dict] = None
        self._hits: List[dict] = []
        self._started_at: float = 0.0

        # 内存索引：每条 {video_path, start_time, end_time, chunk_idx, fps, vec}
        self._index: List[dict] = []
        self._fps: float = 0.0
        self._duration_s: float = 0.0
        self._md5: str = ""   # start_extract 时算（全成功后 save 缓存用）

    # ------------------------------------------------------------------ #
    # 状态 / 快照
    # ------------------------------------------------------------------ #
    def _set_state(self, state: str, error: str = "") -> None:
        with self._lock:
            self._state = state
            self._error = error
        self._broadcast({"type": "state", **self.snapshot()})

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self._state,
                "error": self._error,
                "source": self._source,
                "source_exists": self._source_exists,
                "chunk_seconds": self._chunk_seconds,
                "chunk_overlap_seconds": self._chunk_overlap_seconds,
                "slices_total": self._slices_total,
                "slices_embedded": self._slices_embedded,
                "fps": self._fps,
                "query": self._query,
                "hits": self._hits,
            }

    # ------------------------------------------------------------------ #
    # WS 广播
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
                pass

    # ------------------------------------------------------------------ #
    # API 动作
    # ------------------------------------------------------------------ #
    def open(self, source: str) -> dict:
        source = (source or "").strip()
        if not source:
            raise ValueError("source 不能为空")
        with self._lock:
            self._source = source
            self._source_exists = os.path.isfile(source)
            self._state = _STATE_IDLE
            self._error = ""
            self._slices_total = 0
            self._slices_embedded = 0
            self._query = None
            self._hits = []
            self._index = []
            self._fps = 0.0
            self._duration_s = 0.0
            self._md5 = ""
        self._broadcast({"type": "state", **self.snapshot()})
        return self.snapshot()

    def start_extract(
            self,
            chunk_seconds: int = FIXED_CHUNK_SECONDS,
            chunk_overlap_seconds: int = FIXED_CHUNK_OVERLAP_SECONDS) -> dict:
        # 切片参数固化：调用方传入值一律忽略，改参数请改模块常量。
        chunk_seconds = FIXED_CHUNK_SECONDS
        chunk_overlap_seconds = FIXED_CHUNK_OVERLAP_SECONDS
        # 预检：实例未启用时立即抛 RuntimeError，由 API 层转 503，
        # 避免用户点"开始提取"后还要等 worker 探测元数据才报错。
        # worker 里仍保留二次 _ensure_session 兜底（幂等）。
        LOG.info("vas-search: start_extract requested (chunk=%ds, overlap=%ds, source=%s)",
                 chunk_seconds, chunk_overlap_seconds, self._source)
        self._ensure_session()
        with self._lock:
            if self._state == _STATE_EXTRACTING:
                raise ValueError("已在提取中")
            if not self._source:
                raise ValueError("请先选择视频文件")
            if self._source_exists is False:
                raise ValueError(f"文件不存在: {self._source}")
            if not 3 <= chunk_seconds <= 6:
                raise ValueError("chunk_seconds 必须在 3–6 之间")
            if chunk_overlap_seconds not in (1, 2):
                raise ValueError("chunk_overlap_seconds 必须为 1 或 2")
            if chunk_overlap_seconds >= chunk_seconds:
                raise ValueError("重叠时长必须小于切片时长")
            self._chunk_seconds = chunk_seconds
            self._chunk_overlap_seconds = chunk_overlap_seconds
            self._slices_embedded = 0
            self._query = None
            self._hits = []
            self._index = []
            self._started_at = time.time()
            self._state = _STATE_EXTRACTING
            self._error = ""
            self._stop_worker.clear()

        # 缓存判定（lock 外做 IO：md5 计算 + LanceDB 查询）：
        # 同视频 + 同模型 + 同切片参数 → 直接还原索引，跳过全部
        # 切片 embedding（每片 1-3s NPU 推理）—— state 秒级 READY，
        # 前端进度条直接满格。跨入口共享：微信端已分析过的视频
        # （6s/1s）在此页选同参数提取也命中。
        cache = get_video_cache()
        cached = None
        md5_hex = ""
        if cache is not None:
            try:
                md5_hex = file_md5(self._source)
            except OSError as exc:
                raise RuntimeError(f"视频文件 md5 计算失败：{exc}") from exc
            try:
                session_for_id = self._ensure_session()
            except RuntimeError:
                session_for_id = None
            model_id = ""
            if session_for_id is not None:
                model_id = (getattr(session_for_id, "model_id", None)
                            or getattr(session_for_id, "_model_id", "") or "")
            if model_id:
                try:
                    cached = cache.lookup(
                        md5_hex, model_id, chunk_seconds,
                        chunk_overlap_seconds)
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("vas-search: cache lookup failed (treat as "
                                "miss): %s", exc)
            if cached:
                base = os.path.basename(self._source)
                with self._lock:
                    self._md5 = md5_hex
                    self._index = [
                        {**e, "video_path": base}
                        for e in cached["index"]
                    ]
                    self._fps = cached.get("fps", 0.0) or 0.0
                    self._duration_s = cached["duration_s"]
                    self._slices_total = cached["slices_total"]
                    self._slices_embedded = cached["slices_embedded"]
                    self._state = _STATE_READY
                    self._error = ""
                LOG.info("vas-search: cache hit md5=%s (%d segments, "
                         "chunk=%ds/overlap=%ds) — restored, skipped "
                         "embedding", md5_hex[:12],
                         len(cached["index"]), chunk_seconds,
                         chunk_overlap_seconds)
                snap = self.snapshot()
                self._broadcast({"type": "state", **snap})
                return snap

        with self._lock:
            self._md5 = md5_hex
            self._worker = threading.Thread(
                target=self._worker_loop, name="vcs-extract-worker", daemon=True
            )
            self._worker.start()
        self._broadcast({"type": "state", **self.snapshot()})
        return self.snapshot()

    def stop_extract(self) -> dict:
        self._stop_worker.set()
        with self._lock:
            if self._state == _STATE_EXTRACTING:
                self._state = _STATE_IDLE
                self._error = ""
                self._slices_embedded = 0
                self._slices_total = 0
                self._index = []
        self._broadcast({"type": "state", **self.snapshot()})
        return self.snapshot()

    def query(self, text: str, sim_threshold: float, top_k: int,
             min_results: int = 0, debug: bool = False) -> dict:
        # 阈值 / 上限固化：调用方传入值一律忽略，改参数请改模块常量。
        # ``min_results``（宽松匹配兜底条数）仍由调用方决定。
        sim_threshold = FIXED_SIM_THRESHOLD
        top_k = FIXED_TOP_K
        text = (text or "").strip()
        if not text:
            raise ValueError("检索问题不能为空")
        if len(text) > 50:
            raise ValueError("检索问题不能超过 50 个字")
        if not 0.0 <= sim_threshold <= 1.0:
            raise ValueError("阈值必须在 0–1.0 之间")
        if not 1 <= top_k <= 10:
            raise ValueError("上限必须在 1–10 之间")
        if min_results < 0:
            min_results = 0
        LOG.info("vas-search: query text=%r threshold=%.2f top_k=%d min_results=%d "
                 "debug=%s (index_size=%d)",
                 text, sim_threshold, top_k, min_results, debug,
                 len(self._index) if hasattr(self, "_index") else 0)
        with self._lock:
            if self._state not in (_STATE_READY, _STATE_IDLE):
                raise ValueError("请先完成视频切片提取")
            if not self._index:
                self._query = {"text": text, "sim_threshold": sim_threshold, "top_k": top_k}
                self._hits = []
                snap = self.snapshot()
                snap["query_meta"] = {
                    "index_size": 0,
                    "top_score": None,
                    "above_threshold_count": 0,
                    "below_threshold_count": 0,
                    "fallback_used": False,
                }
                self._broadcast({"type": "state", **snap})
                return snap

        session = self._ensure_session()
        lock = get_session_call_lock()
        with lock:
            qvec = session.embed_text(text)
        qvec = qvec / (np.linalg.norm(qvec) + 1e-9)

        with self._lock:
            index = list(self._index)
        result_hits, meta = _score_hits(
            qvec, index, sim_threshold, top_k, min_results)

        snap = None
        with self._lock:
            self._query = {"text": text, "sim_threshold": sim_threshold, "top_k": top_k}
            self._hits = result_hits
            snap = self.snapshot()
            snap["query_meta"] = {
                "index_size": len(index),
                "top_score": meta["top_score"],
                "above_threshold_count": meta["above_threshold_count"],
                "below_threshold_count": meta["below_threshold_count"],
                "fallback_used": meta["fallback_used"],
            }
            if debug:
                snap["query_debug"] = {
                    "all_similarities": meta["all_sims"],
                    "qvec_norm": round(float(np.linalg.norm(qvec)), 4),
                    "qvec_l2_unit": abs(round(float(np.linalg.norm(qvec)), 4) - 1.0) < 0.01,
                }
        self._broadcast({"type": "state", **snap})
        return snap

    def _ensure_session(self):
        """从 ComputeRegistry 拿 embedding session。未启用抛 RuntimeError。"""
        return get_registry().get_session("swcap_video_retrieval")

    def clear_query(self) -> dict:
        """清空最近一次检索结果（query / hits），前端"清空"按钮调用。

        单视频检索结果存在服务端单例里，会被 WS 快照持续渲染；必须在
        服务端清掉，否则前端清空后下一次快照又会把它渲染回来。多视频
        （勾选）路径无状态，本端点对其是幂等的。

        同时把 ``ready`` 复位为 ``idle``（并清零切片计数）：结果清空后
        "已提取完成"的展示也要一并复位，否则前端任何一次快照重放
        （WS 重连 / 卡片 JS 重载 / 切页返回）都会按 ``state=ready`` 把
        进度条刷回 100%（用户实测：切页再切回进度条复活）。内存索引
        ``_index`` 保留 —— 重新点检索会先走 ``start_extract``，由 LanceDB
        缓存命中秒级还原后重查，无需重新 embedding。
        """
        with self._lock:
            self._query = None
            self._hits = []
            if self._state == _STATE_READY:
                self._state = _STATE_IDLE
            self._slices_total = 0
            self._slices_embedded = 0
        snap = self.snapshot()
        self._broadcast({"type": "state", **snap})
        return snap

    def reset_session(self) -> dict:
        """作废当前会话索引（LanceDB 持久缓存被清空时调用）。

        与 ``clear_query`` 的区别：``clear_query`` 只清结果、保留索引
        （供重查）；本方法连内存索引 / 切片计数一起清掉并回到 ``idle``。

        ``/search/cache/clear`` 端点调用 —— 持久缓存已删，会话索引若
        继续存在，前端 ``vasStartSearch`` 会看到 ``state=ready`` + 同源
        走短路、跳过 ``start_extract``，表现为"进度条直接满格 + 历史无
        新条目"（用户实测）。保留 ``_source``，用户可重新点开始提取。

        先停掉正在跑的提取 worker（``_worker_loop`` 在切片刻/终态两处
        检查 ``_stop_worker`` 并直接 return）—— 否则清空后 worker 仍会
        把会话写回 ``ready``、甚至把 ``save()`` 重新写进刚清空的 LanceDB。
        """
        self._stop_worker.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=2.0)
        with self._lock:
            self._worker = None
            self._state = _STATE_IDLE
            self._error = ""
            self._slices_total = 0
            self._slices_embedded = 0
            self._query = None
            self._hits = []
            self._index = []
            self._fps = 0.0
            self._duration_s = 0.0
            self._md5 = ""
        snap = self.snapshot()
        self._broadcast({"type": "state", **snap})
        return snap

    def close(self) -> None:
        self._stop_worker.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=2.0)
        with self._lock:
            self._worker = None
            self._state = _STATE_IDLE
            self._error = ""
            self._source = ""
            self._source_exists = None
            self._slices_total = 0
            self._slices_embedded = 0
            self._query = None
            self._hits = []
            self._index = []

    # ------------------------------------------------------------------ #
    # 后台 worker：切片 + embedding
    # ------------------------------------------------------------------ #
    def _compute_slices(self, fps: float, duration_s: float) -> int:
        """按 chunk 参数计算切片总数 —— 转调公共 ``compute_slices``
        （与 ``_worker_loop`` 终止语义一致，微信管线共用同一实现）。"""
        return compute_slices(fps, duration_s, self._chunk_seconds,
                              self._chunk_overlap_seconds)

    def _worker_loop(self) -> None:
        try:
            meta = _probe_video_meta(self._source)
            if meta is None or meta["fps"] <= 0:
                raise RuntimeError("无法读取视频元数据（fps / 帧数）")
            fps = meta["fps"]
            duration_s = meta["duration_s"] if meta["duration_s"] > 0 else meta["total_frames"] / fps

            with self._lock:
                self._fps = fps
                self._duration_s = duration_s
                self._slices_total = self._compute_slices(fps, duration_s)
                if self._slices_total <= 0:
                    self._state = _STATE_ERROR
                    self._error = (
                        f"无法计算切片数（fps={fps:.2f}, "
                        f"duration={duration_s:.1f}s, total_frames={int(duration_s * fps)}）。"
                        "请检查视频文件是否完整或换一个文件重试。"
                    )
                    self._broadcast({"type": "state", **self.snapshot()})
                    return
                self._broadcast({"type": "state", **self.snapshot()})

            session = self._ensure_session()
            step = self._chunk_seconds - self._chunk_overlap_seconds
            chunk_idx = 0
            start = 0.0
            lock = get_session_call_lock()
        except Exception as e:  # pragma: no cover - defensive
            LOG.exception("vcs extract worker error: %s", e)
            self._set_state(_STATE_ERROR, str(e))
            return

        while start < duration_s:
            if self._stop_worker.is_set():
                return
            frames = _read_chunk_frames(
                self._source, start, self._chunk_seconds, fps)
            if not frames:
                break
            # 帧采样间隔 1 秒/帧（demo 语义），所以实际覆盖秒数 = 帧数；
            # 短尾片截断时 end_time 按实际帧数计
            end_time = start + len(frames)
            LOG.debug("vas-search: chunk %d/%d [%.2fs-%.2fs, %d frames]",
                      chunk_idx, self._slices_total, start, end_time, len(frames))
            try:
                LOG.debug("vas-search: chunk %d — calling embed_video", chunk_idx)
                import time as _time
                t_chunk = _time.time()
                vec = None
                # FastDDS 首次调用 reply topic discovery race → client 端
                # timeout. 已匹配后不会再 race，所以 timeout 后立即重试即可。
                for _attempt in range(2):
                    try:
                        with lock:
                            vec = session.embed_video(frames, prompt="")
                        break
                    except RuntimeError as rpc_err:
                        if "timeout" not in str(rpc_err).lower() or _attempt == 1:
                            raise
                        LOG.warning("vas-search: chunk %d embed_video timeout "
                                    "(reply discovery race), retrying", chunk_idx)
                vec = np.asarray(vec, dtype=np.float32)
                norm = float(np.linalg.norm(vec))
                if norm > 1e-6:
                    vec = vec / norm
                    with self._lock:
                        self._index.append({
                            "video_path": os.path.basename(self._source),
                            "start_time": round(start, 2),
                            "end_time": round(end_time, 2),
                            "chunk_idx": chunk_idx,
                            "fps": fps,
                            "vec": vec,
                        })
                        self._slices_embedded += 1
                        self._broadcast({
                            "type": "progress",
                            "embedded": self._slices_embedded,
                            "total": self._slices_total,
                        })
                LOG.debug("vas-search: chunk %d done in %.2fs (slices_embedded=%d/%d)",
                          chunk_idx, _time.time() - t_chunk,
                          self._slices_embedded, self._slices_total)
            except Exception as chunk_err:  # noqa: BLE001
                LOG.warning(
                    "vas-search: chunk %d [%.2fs-%.2fs] embedding failed after %.2fs: %s — skipping",
                    chunk_idx, start, end_time, _time.time() - t_chunk, chunk_err)
            chunk_idx += 1
            start += step
            if end_time >= duration_s:
                break

        if self._stop_worker.is_set():
            return
        with self._lock:
            if self._state == _STATE_EXTRACTING:
                if self._slices_total > 0 and self._slices_embedded == 0:
                    self._state = _STATE_ERROR
                    self._error = (
                        f"提取完成但 0/{self._slices_total} 切片成功 embedding — "
                        "所有切片解码或推理失败，请查看后端日志或换文件重试。"
                    )
                else:
                    self._state = _STATE_READY
                    self._error = ""
        LOG.info("vas-search: extract done — %d/%d slices embedded in %.1fs wall",
                 self._slices_embedded, self._slices_total,
                 time.time() - self._started_at)
        self._broadcast({"type": "state", **self.snapshot()})

        # ---- 全成功入库（与微信管线同语义：部分成功不存，重跑可补全）。
        # 同 model + 同 chunk 参数的条目幂等覆盖。失败仅 LOG 不阻塞。
        cache = get_video_cache()
        if (cache is not None and self._md5
                and self._slices_embedded >= self._slices_total):
            model_id = (getattr(session, "model_id", None)
                        or getattr(session, "_model_id", "") or "")
            if model_id:
                try:
                    cache.save(self._md5, model_id, self._index, {
                        "filename": os.path.basename(self._source),
                        "source_path": self._source,
                        "fps": fps,
                        "duration_s": duration_s,
                        "slices_total": self._slices_total,
                        "slices_embedded": self._slices_embedded,
                        "chunk_seconds": self._chunk_seconds,
                        "chunk_overlap_seconds": self._chunk_overlap_seconds,
                    })
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("vas-search: cache save failed (skipped): %s", exc)
        elif self._slices_embedded < self._slices_total:
            LOG.warning("vas-search: partial extraction (%d/%d) — not saved to "
                        "cache; re-extract to complete",
                        self._slices_embedded, self._slices_total)


_service: Optional[VideoSearchService] = None


def get_service() -> VideoSearchService:
    global _service
    if _service is None:
        _service = VideoSearchService()
    return _service
