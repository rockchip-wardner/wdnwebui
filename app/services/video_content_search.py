"""视频内容检索 — 真实后端（Qwen3-VL-Embedding-2B）。

流程（对齐 vl_rag_demo 的语义，但为**单视频 + 内存索引**版本）：

1. ``open(source)`` —— 记录视频文件路径，校验存在。
2. ``start_extract(chunk_seconds, chunk_overlap_seconds)`` —— 后台 daemon
   线程用 PyAV 按固定时间窗切片，每片取等距帧缩放到 576×576，经 wdnai
   ``LLMSession.embed_video`` 得到 2048 维归一化向量，存内存
   ``List[{start_time, end_time, chunk_idx, fps, vec}]``。逐片推进
   ``slices_embedded`` 并 WS 广播 ``progress``。
3. ``query(text, sim_threshold, top_k)`` —— ``LLMSession.embed_text`` 编码
   查询向量，遍历内存索引算余弦相似度，阈值过滤 + top_k 截断。

LLMSession 从 ComputeRegistry 取（``swcap_video_retrieval`` capability），
与 detector / VFA 共用进程级 ``get_session_call_lock`` 串行化。

命中项字段（对齐 demo 语义，前端契约不变）：
    {video_path, start_time, end_time, chunk_idx, similarity}
缩略图：由 ``GET /api/video-content-search/frame`` 端点按
``video_path + start_time`` 实时取帧（PyAV seek + RGA 缩放）。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Iterator, List, Optional, Tuple

import numpy as np

from app.services.compute_registry import (
    get_registry,
    get_session_call_lock,
)

LOG = logging.getLogger(__name__)

_STATE_IDLE = "idle"
_STATE_EXTRACTING = "extracting"
_STATE_READY = "ready"
_STATE_ERROR = "error"

# embedding 模型的 vision 输入边长（Qwen3-VL-Embedding-2B 固定 576×576）
_VISION_SIZE = 576
_EMBED_DIM = 2048


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """两个归一化向量的余弦相似度（内积）。"""
    return float(np.dot(a, b))


def _probe_video_meta(source: str) -> Optional[dict]:
    """用 PyAV 探测 fps / 总帧数 / 时长。失败返回 None。"""
    if not source or not os.path.isfile(source):
        return None
    try:
        import av
        with av.open(source, timeout=(2.0, 5.0)) as container:
            stream = container.streams.video[0]
            fps = float(stream.average_rate) if stream.average_rate else 0.0
            total_frames = int(stream.frames) if stream.frames else 0
            duration_s = float(stream.duration * stream.time_base) if stream.duration else 0.0
            return {
                "fps": fps,
                "total_frames": total_frames,
                "duration_s": duration_s,
            }
    except Exception as e:
        LOG.debug("vcs: PyAV 探测 %s 失败: %s", source, e)
        return None


def _read_chunk_frames(source: str, start_s: float, chunk_seconds: int,
                       fps: float, max_frames: Optional[int] = None) -> List[np.ndarray]:
    """从 ``source`` 读出 [start_s, start_s + chunk_seconds) 区间内
    ``chunk_seconds`` 个等距帧（BGR ndarray），按 demo 语义。

    - PyAV ``avformat_seek_file`` 跳到最近的 keyframe，再顺序 decode 到
      目标时间点 —— 避免从头顺序读全片（与 VFA 取帧同一优化）。
    - 每帧缩放到 576×576（embedding 模型 vision 输入）。
    """
    import av

    target_frames = chunk_seconds if max_frames is None else min(chunk_seconds, max_frames)
    container = av.open(source, timeout=(2.0, 5.0))
    try:
        stream = container.streams.video[0]
        time_base = float(stream.time_base)
        if time_base <= 0 or fps <= 0:
            raise RuntimeError("视频流元数据缺失（time_base / fps）")

        # 本片要采的帧的绝对帧号
        frame_numbers = [
            int(round((start_s + j) * fps))
            for j in range(target_frames)
        ]
        frame_numbers = [fn for fn in frame_numbers if fn >= 0]
        if not frame_numbers:
            return []

        # 跳到第一帧所在秒附近的 keyframe
        offset = int((frame_numbers[0] / fps) / time_base)
        try:
            container.seek(offset, backward=True, any_frame=False, stream=stream)
        except Exception:
            pass  # seek 失败 → 从头顺序读

        # 顺序 decode，按绝对帧号收集目标帧
        by_number: dict = {}
        for frame in container.decode(video=0):
            if frame.pts is None:
                continue
            abs_time = float(frame.pts) * time_base
            frame_number = int(round(abs_time * fps))
            if frame_number in frame_numbers:
                by_number[frame_number] = frame
                if len(by_number) >= len(frame_numbers):
                    break
            if frame_number > max(frame_numbers) + 300:
                break

        import cv2
        frames: List[np.ndarray] = []
        for fn in frame_numbers:
            f = by_number.get(fn)
            if f is None:
                continue
            bgr = f.to_ndarray(format="bgr24")
            if (bgr.shape[0], bgr.shape[1]) != (_VISION_SIZE, _VISION_SIZE):
                bgr = cv2.resize(bgr, (_VISION_SIZE, _VISION_SIZE),
                                 interpolation=cv2.INTER_LINEAR)
            frames.append(bgr)
        return frames
    finally:
        container.close()


def iter_segment_jpeg(source: str, start_s: float, end_s: float,
                      max_frames: int = 600, paced: bool = False,
                      fallback_fps: float = 25.0) -> Iterator[bytes]:
    """按播放顺序生成 [start_s, end_s) 区间内每一帧的 JPEG bytes。

    PyAV 打开一次容器，seek 到 start_s 附近 keyframe 后顺序 decode 到
    end_s；每帧 ``to_ndarray(format="bgr24")`` + cv2 编码 JPEG。供
    ``/api/video-content-search/play`` WS 推帧用。生成器在取帧/编码
    异常时提前结束（由 WS handler 补发 error 帧）。

    ``paced=True`` 时按源视频 fps 节流（首帧立即出，后续每帧间隔
    ``1/fps``），fps 缺失回退 ``fallback_fps``。
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


class VideoContentSearchService:
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
        self._chunk_seconds: int = 4
        self._chunk_overlap_seconds: int = 1
        self._slices_total: int = 0
        self._slices_embedded: int = 0
        self._query: Optional[dict] = None
        self._hits: List[dict] = []
        self._started_at: float = 0.0

        # 内存索引：每条 {video_path, start_time, end_time, chunk_idx, fps, vec}
        self._index: List[dict] = []
        self._fps: float = 0.0
        self._duration_s: float = 0.0

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
        self._broadcast({"type": "state", **self.snapshot()})
        return self.snapshot()

    def start_extract(self, chunk_seconds: int, chunk_overlap_seconds: int) -> dict:
        # 预检：实例未启用时立即抛 RuntimeError，由 API 层转 503，
        # 避免用户点"开始提取"后还要等 worker 探测元数据才报错。
        # worker 里仍保留二次 _ensure_session 兜底（幂等）。
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

    def query(self, text: str, sim_threshold: float, top_k: int) -> dict:
        text = (text or "").strip()
        if not text:
            raise ValueError("检索问题不能为空")
        if len(text) > 50:
            raise ValueError("检索问题不能超过 50 个字")
        if not 0.0 <= sim_threshold <= 1.0:
            raise ValueError("阈值必须在 0–1.0 之间")
        if not 1 <= top_k <= 10:
            raise ValueError("上限必须在 1–10 之间")
        with self._lock:
            if self._state not in (_STATE_READY, _STATE_IDLE):
                raise ValueError("请先完成视频切片提取")
            if not self._index:
                self._query = {"text": text, "sim_threshold": sim_threshold, "top_k": top_k}
                self._hits = []
                self._broadcast({"type": "state", **self.snapshot()})
                return self.snapshot()

        session = self._ensure_session()
        lock = get_session_call_lock()
        with lock:
            qvec = session.embed_text(text)
        qvec = qvec / (np.linalg.norm(qvec) + 1e-9)

        scored: List[Tuple[float, dict]] = []
        with self._lock:
            index = list(self._index)
        for entry in index:
            sim = _cosine_similarity(qvec, entry["vec"])
            scored.append((sim, entry))

        scored.sort(key=lambda t: -t[0])
        hits = []
        for sim, entry in scored:
            if sim < sim_threshold:
                continue
            if len(hits) >= top_k:
                break
            hits.append({
                "video_path": entry["video_path"],
                "start_time": entry["start_time"],
                "end_time": entry["end_time"],
                "chunk_idx": entry["chunk_idx"],
                "similarity": round(float(sim), 4),
            })

        with self._lock:
            self._query = {"text": text, "sim_threshold": sim_threshold, "top_k": top_k}
            self._hits = hits
        self._broadcast({"type": "state", **self.snapshot()})
        return self.snapshot()

    def _ensure_session(self):
        """从 ComputeRegistry 拿 embedding session。未启用抛 RuntimeError。"""
        return get_registry().get_session("swcap_video_retrieval")

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
        """按 chunk 参数计算切片总数，与 ``_worker_loop`` 的终止语义完全一致：

        遍历 start 序列（步进 = chunk_seconds - overlap），每片按 1 秒/帧
        采样帧号、剔除超出总帧数的帧；空片即停；end_time（= start + 实际
        帧数）达到或超过时长即停。重叠残片不计入。
        """
        total_frames = int(duration_s * fps)
        if fps <= 0 or total_frames <= 0:
            return 0
        step = self._chunk_seconds - self._chunk_overlap_seconds
        if step <= 0:
            return 0
        n = 0
        start = 0.0
        while start < duration_s:
            frame_numbers = [
                int(round((start + j) * fps))
                for j in range(self._chunk_seconds)
            ]
            frame_numbers = [fn for fn in frame_numbers if 0 <= fn < total_frames]
            if not frame_numbers:
                break
            end_time = start + len(frame_numbers)
            n += 1
            start += step
            if end_time >= duration_s:
                break
        return n

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
                self._broadcast({"type": "state", **self.snapshot()})

            session = self._ensure_session()
            step = self._chunk_seconds - self._chunk_overlap_seconds
            chunk_idx = 0
            start = 0.0
            lock = get_session_call_lock()

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
                with lock:
                    vec = session.embed_video(frames, prompt="")
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
                chunk_idx += 1
                start += step
                if end_time >= duration_s:
                    break

            if self._stop_worker.is_set():
                return
            with self._lock:
                if self._state == _STATE_EXTRACTING:
                    self._state = _STATE_READY
                    self._error = ""
            self._broadcast({"type": "state", **self.snapshot()})
        except Exception as e:  # pragma: no cover - defensive
            LOG.exception("vcs extract worker error: %s", e)
            self._set_state(_STATE_ERROR, str(e))


_service: Optional[VideoContentSearchService] = None


def get_service() -> VideoContentSearchService:
    global _service
    if _service is None:
        _service = VideoContentSearchService()
    return _service
