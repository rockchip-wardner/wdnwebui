"""视频内容检索 — mock 服务（前端演示占位）。

本期后端功能留空：本服务只模拟"切片 embedding 进度 + 检索命中"，
返回固定的演示数据，让前端 UI 能跑通「选视频 → 提取 → 检索 →
点时间点播放」的完整交互。后续按 vl_rag_demo（Qwen3-VL-Embedding
视频检索）实现真实后端时，只需替换本文件内部逻辑，API 契约不变。

命中项字段（对齐 demo 语义）：
    {video_path, start_time, end_time, chunk_idx, similarity}
缩略图：mock 阶段无真实图，前端用占位色块；字段名预留。
"""
from __future__ import annotations

import logging
import os
import random
import threading
import time
from typing import Callable, List, Optional

LOG = logging.getLogger(__name__)

_STATE_IDLE = "idle"
_STATE_EXTRACTING = "extracting"
_STATE_READY = "ready"
_STATE_ERROR = "error"

# 模拟切片嵌入的基准耗时（秒/片），配合随机抖动制造真实感
_EMBED_SECONDS_PER_SLICE = 0.25
_EMBED_JITTER = 0.08

# mock 命中库：一条真实视频被切成若干 chunk 后的"检索结果"。
# 演示数据与索引无关 —— extract 只改进度，query 从这里按相似度过滤。
_MOCK_HITS: List[dict] = [
    {"video_path": "sample.mp4", "start_time": 12.0,  "end_time": 16.0,  "chunk_idx": 3, "similarity": 0.91},
    {"video_path": "sample.mp4", "start_time": 20.0,  "end_time": 24.0,  "chunk_idx": 5, "similarity": 0.84},
    {"video_path": "sample.mp4", "start_time": 34.0,  "end_time": 38.0,  "chunk_idx": 9, "similarity": 0.77},
    {"video_path": "sample.mp4", "start_time": 48.0,  "end_time": 52.0,  "chunk_idx": 12, "similarity": 0.72},
    {"video_path": "sample.mp4", "start_time": 61.0,  "end_time": 65.0,  "chunk_idx": 16, "similarity": 0.66},
    {"video_path": "sample.mp4", "start_time": 73.0,  "end_time": 77.0,  "chunk_idx": 19, "similarity": 0.58},
    {"video_path": "sample.mp4", "start_time": 80.0,  "end_time": 84.0,  "chunk_idx": 21, "similarity": 0.51},
    {"video_path": "sample.mp4", "start_time": 90.0,  "end_time": 94.0,  "chunk_idx": 24, "similarity": 0.44},
]


class VideoContentSearchService:
    """mock 状态机 + 后台 worker + WS 广播。

    状态流：
        idle → extracting → ready（提取完成）
        idle → extracting → stopped（中途停止，退回 idle）
    任一阶段出错 → error，展示给前端。
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
        self._broadcast({"type": "state", **self.snapshot()})
        return self.snapshot()

    def start_extract(self, chunk_seconds: int, chunk_overlap_seconds: int) -> dict:
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
            # 演示用：总切片数按"视频时长"假想得出。真实后端按文件
            # 时长 + chunk 参数计算；这里固定一个值保证 UI 有进度可看。
            self._slices_total = 20
            self._slices_embedded = 0
            self._hits = []
            self._query = None
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
            # mock：按相似度降序 + 阈值过滤 + top_k 截断
            hits = [
                dict(h)
                for h in sorted(_MOCK_HITS, key=lambda h: -h["similarity"])
                if h["similarity"] >= sim_threshold
            ][:top_k]
            self._query = {
                "text": text,
                "sim_threshold": sim_threshold,
                "top_k": top_k,
            }
            self._hits = hits
        self._broadcast({"type": "state", **self.snapshot()})
        return self.snapshot()

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

    # ------------------------------------------------------------------ #
    # 后台 worker：逐片推进嵌入进度
    # ------------------------------------------------------------------ #
    def _worker_loop(self) -> None:
        try:
            while not self._stop_worker.is_set():
                with self._lock:
                    if self._state != _STATE_EXTRACTING:
                        return
                    embedded = self._slices_embedded
                    total = self._slices_total
                if embedded >= total:
                    break
                sleep = _EMBED_SECONDS_PER_SLICE + random.uniform(0, _EMBED_JITTER)
                if self._stop_worker.wait(sleep):
                    return
                with self._lock:
                    if self._state != _STATE_EXTRACTING:
                        return
                    self._slices_embedded += 1
                    embedded = self._slices_embedded
                    total = self._slices_total
                self._broadcast({
                    "type": "progress",
                    "embedded": embedded,
                    "total": total,
                })
            with self._lock:
                if self._state == _STATE_EXTRACTING:
                    self._state = _STATE_READY
                    self._error = ""
                    self._hits = []
                    self._query = None
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
