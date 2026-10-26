"""视频内容检索 — 真实后端（Qwen3-VL-Embedding-2B）。

流程（对齐 vl_rag_demo 的语义，但为**单视频 + 内存索引**版本）：

1. ``open(source)`` —— 记录视频文件路径，校验存在。
2. ``start_extract()`` —— 每 1 秒采样一帧，经 gscore 判据（``FrameJudge``）
   决定是否要；按「最多 ``SLICE_MAX_FRAMES`` 个有效帧 / 重叠
   ``SLICE_OVERLAP_FRAMES`` 个有效帧」累积成片，经 wdnai
   ``LLMSession.embed_video`` 得到 2048 维归一化向量，存内存
   ``List[{start_time, end_time, chunk_idx, fps, vec}]``。跳帧不丢时间：
   ``start``/``end`` 覆盖首个/最后一个有效帧（含中间跳过的秒）。
3. ``query(text)`` —— 匹配阈值固定 0.40、检索上限固定 10，
   ``LLMSession.embed_text`` 编码查询向量，遍历内存索引算余弦相似度，
   阈值过滤 + top_k 截断。

LLMSession 从 ComputeRegistry 取（``swcap_vision_embedding`` capability），
与 detector 共用进程级 ``get_session_call_lock`` 串行化。

命中项字段（对齐 demo 语义，前端契约不变）：
    {video_path, start_time, end_time, chunk_idx, similarity}
缩略图：由 ``GET /api/video-retrieval/frame`` 端点按
``video_path + start_time`` 实时取帧（PyAV seek + RGA 缩放）。

v0.8：从 ``app/apps/video_search/service.py`` 平移到
``app/core/video/search.py`` —— ``video_retrieval`` 卡片（监控反查，只检索）
检索合并页）通过 ``app.core.*`` 引用检索能力，遵守卡片零跨卡片 import
规则。切片实现已抽到 ``app/core/video/slicing.py``（VR 与微信管线共用）；
v0.27 起 VR 走 gscore 动态切片，不再有固定 4s/1s 参数。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Iterator, List, Optional

import numpy as np
from wdnai import SoftwareCapability

from app.core.compute.registry import (
    get_registry,
    get_session_call_lock,
)
from app.core.video.frame_judge import EXPOSED_METRICS, FrameJudge
from app.core.video.slicing import (
    SLICE_MAX_FRAMES,
    SLICE_OVERLAP_FRAMES,
    iter_slices,
)
from app.core.video.utils import (
    _VISION_SIZE,
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
# 切片口径由 ``app.core.video.slicing`` 的 ``SLICE_MAX_FRAMES`` /
# ``SLICE_OVERLAP_FRAMES`` 决定（每片「有效帧」上限/重叠，不是秒数）。
FIXED_SIM_THRESHOLD = 0.40
FIXED_TOP_K = 10

# 判据默认：gscore / 兜底间隔 10 秒（采样是 1 秒/帧，所以 every_n=10 帧）。
DEFAULT_JUDGE_EVERY_N = 10

# ---------------------------------------------------------------- #
# 图片检索（以图 / 图+文检索视频片段）
# ---------------------------------------------------------------- #
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp")
IMAGE_MAX_BYTES = 20 * 1024 * 1024
# 质量门槛（真机实测标定）：
#   纯色/近黑 std≈0；随机噪声 Laplacian≈4.9e4；
#   真实图片（bus/demo/视频帧）Laplacian 68~1.1e4。
# std 挡纯色，Laplacian 挡随机噪声；常量集中在这里便于按现场素材调整。
IMAGE_MIN_STD = 3.0
IMAGE_MAX_LAPLACIAN_VAR = 20000.0
IMAGE_THUMB_LONG_SIDE = 240


def validate_image_path(path: str) -> str:
    """校验图片路径并返回规范化后的绝对路径（不通过抛 ValueError）。"""
    p = (path or "").strip()
    if not p:
        raise ValueError("图片路径不能为空")
    if not os.path.isabs(p):
        raise ValueError("图片路径必须是绝对路径")
    if not os.path.isfile(p):
        raise ValueError(f"图片不存在: {p}")
    if os.path.splitext(p)[1].lower() not in IMAGE_EXTS:
        raise ValueError("图片格式不支持（支持 jpg/jpeg/png/bmp/webp）")
    try:
        size = os.path.getsize(p)
    except OSError:
        size = 0
    if size > IMAGE_MAX_BYTES:
        raise ValueError(f"图片过大（上限 {IMAGE_MAX_BYTES // 1024 // 1024} MB）")
    return p


def read_image_bgr(path: str) -> np.ndarray:
    """读原图（BGR ndarray），只做路径校验、不缩放。"""
    import cv2

    p = validate_image_path(path)
    image = cv2.imread(p)
    if image is None:
        raise ValueError(f"无法读取图片: {p}")
    return image


def read_query_image(path: str) -> np.ndarray:
    """读图并缩放到 576×576（与 ``_read_chunk_frames`` 的索引帧预处理一致）。"""
    import cv2

    image = read_image_bgr(path)
    if (image.shape[0], image.shape[1]) != (_VISION_SIZE, _VISION_SIZE):
        image = cv2.resize(image, (_VISION_SIZE, _VISION_SIZE),
                           interpolation=cv2.INTER_LINEAR)
    return image


def load_query_image(path: str, *, check_quality: bool = True) -> np.ndarray:
    """质量检查在**原图**上做，再缩放到 576×576。

    不能在缩放后的图上检查：把 128×128 的随机噪声双线性放大到 576×576
    本身就是一次低通滤波，高频噪声会被平滑掉，质量门槛就失效了。
    """
    import cv2

    image = read_image_bgr(path)
    if check_quality:
        err = image_quality_error(image)
        if err:
            raise ValueError(err)
    if (image.shape[0], image.shape[1]) != (_VISION_SIZE, _VISION_SIZE):
        image = cv2.resize(image, (_VISION_SIZE, _VISION_SIZE),
                           interpolation=cv2.INTER_LINEAR)
    return image


def image_quality_error(image: np.ndarray) -> Optional[str]:
    """图片质量门槛：纯色 / 随机噪声返回中文原因，正常图返回 None。"""
    import cv2

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if float(gray.std()) < IMAGE_MIN_STD:
        return "图片几乎是纯色（无法检索），请换一张"
    lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    if lap_var > IMAGE_MAX_LAPLACIAN_VAR:
        return "图片噪声过高（无法检索），请换一张"
    return None


def read_image_thumb(path: str, long_side: int = IMAGE_THUMB_LONG_SIDE) -> bytes:
    """读图并编码成 JPEG 缩略图（只读，不参与检索）。"""
    import cv2

    p = validate_image_path(path)
    image = cv2.imread(p)
    if image is None:
        raise ValueError(f"无法读取图片: {p}")
    h, w = image.shape[:2]
    scale = float(long_side) / max(h, w)
    if scale < 1.0:
        image = cv2.resize(
            image,
            (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
            interpolation=cv2.INTER_AREA,
        )
    ok, buf = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
    if not ok:
        raise ValueError("图片缩略图编码失败")
    return bytes(buf)


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
        LOG.info("video-retrieval: query threshold %.2f filtered all hits — fallback "
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
    ``/api/video-retrieval/play`` WS 推帧用。生成器在取帧/编码
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
        self._chunk_seconds: int = SLICE_MAX_FRAMES
        self._chunk_overlap_seconds: int = SLICE_OVERLAP_FRAMES
        self._slices_total: int = 0
        self._slices_embedded: int = 0
        # 采样统计（1 秒/帧）：已评估 / 取 / 跳过
        self._frames_evaluated: int = 0
        self._frames_taken: int = 0
        self._frames_skipped: int = 0
        # gscore 判据（每路/每视频一个实例，提取开始时 reset）
        self._judge = FrameJudge(
            mode=EXPOSED_METRICS[0],
            every_n=DEFAULT_JUDGE_EVERY_N,
            long_side=160,
        )
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
                "duration_s": self._duration_s,
                "frames_evaluated": self._frames_evaluated,
                "frames_taken": self._frames_taken,
                "frames_skipped": self._frames_skipped,
                "fps": self._fps,
                "judge": self._judge.snapshot(),
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
            self._frames_evaluated = 0
            self._frames_taken = 0
            self._frames_skipped = 0
            self._judge.reset()
            self._query = None
            self._hits = []
            self._index = []
            self._fps = 0.0
            self._duration_s = 0.0
            self._md5 = ""
        self._broadcast({"type": "state", **self.snapshot()})
        return self.snapshot()

    def start_extract(self) -> dict:
        # 切片口径由 ``slicing.SLICE_MAX_FRAMES`` / ``SLICE_OVERLAP_FRAMES``
        # 固定（有效帧上限 / 重叠帧数），调用方不再传参。
        # 预检：实例未启用时立即抛 RuntimeError，由 API 层转 503，
        # 避免用户点"开始提取"后还要等 worker 探测元数据才报错。
        # worker 里仍保留二次 _ensure_session 兜底（幂等）。
        LOG.info("video-retrieval: start_extract requested (max_frames=%d, "
                 "overlap_frames=%d, source=%s)",
                 SLICE_MAX_FRAMES, SLICE_OVERLAP_FRAMES, self._source)
        self._ensure_session()
        with self._lock:
            if self._state == _STATE_EXTRACTING:
                raise ValueError("已在提取中")
            if not self._source:
                raise ValueError("请先选择视频文件")
            if self._source_exists is False:
                raise ValueError(f"文件不存在: {self._source}")
            self._chunk_seconds = SLICE_MAX_FRAMES
            self._chunk_overlap_seconds = SLICE_OVERLAP_FRAMES
            self._slices_embedded = 0
            self._slices_total = 0
            self._frames_evaluated = 0
            self._frames_taken = 0
            self._frames_skipped = 0
            self._judge.reset()
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
                        md5_hex, model_id, SLICE_MAX_FRAMES,
                        SLICE_OVERLAP_FRAMES,
                        judge_fp=self._judge_fingerprint(),
                        slice_version="gscore-v1")
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("video-retrieval: cache lookup failed (treat as "
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
                LOG.info("video-retrieval: cache hit md5=%s (%d segments, "
                         "judge=%s) — restored, skipped embedding",
                         md5_hex[:12], len(cached["index"]),
                         self._judge_fingerprint())
                snap = self.snapshot()
                self._broadcast({"type": "state", **snap})
                return snap

        with self._lock:
            self._md5 = md5_hex
            self._worker = threading.Thread(
                target=self._worker_loop, name="vr-extract-worker", daemon=True
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
                self._frames_evaluated = 0
                self._frames_taken = 0
                self._frames_skipped = 0
                self._index = []
        self._broadcast({"type": "state", **self.snapshot()})
        return self.snapshot()

    def encode_query(self, text: str = "", image_path: str = "",
                     check_image_quality: bool = True) -> np.ndarray:
        """把查询编码成归一化向量（图 / 文 / 图+文）。

        有图走 ``embed_video([img], prompt=text)`` —— 单帧就是图片
        embedding，与索引里的视频片段同一 2048 维空间；纯文字走
        ``embed_text``。图片读完统一缩到 576×576（与索引帧一致）。
        """
        text = (text or "").strip()
        image_path = (image_path or "").strip()
        if not text and not image_path:
            raise ValueError("请至少填写检索问题或选择一张图片")
        if text and len(text) > 50:
            raise ValueError("检索问题不能超过 50 个字")
        session = self._ensure_session()
        lock = get_session_call_lock()
        with lock:
            if image_path:
                image = load_query_image(
                    image_path, check_quality=check_image_quality)
                vec = session.embed_video([image], prompt=text or "")
            else:
                vec = session.embed_text(text)
        return np.asarray(vec, dtype=np.float32).reshape(-1)

    def query(self, text: str = "",
              sim_threshold: float = FIXED_SIM_THRESHOLD,
              top_k: int = FIXED_TOP_K,
              min_results: int = 0, debug: bool = False, *,
              image_path: str = "",
              check_image_quality: bool = True) -> dict:
        # 阈值 / 上限固化：调用方传入值一律忽略，改参数请改模块常量。
        # ``min_results``（宽松匹配兜底条数）仍由调用方决定。
        sim_threshold = FIXED_SIM_THRESHOLD
        top_k = FIXED_TOP_K
        text = (text or "").strip()
        image_path = (image_path or "").strip()
        if not text and not image_path:
            raise ValueError("请至少填写检索问题或选择一张图片")
        if text and len(text) > 50:
            raise ValueError("检索问题不能超过 50 个字")
        if not 0.0 <= sim_threshold <= 1.0:
            raise ValueError("阈值必须在 0–1.0 之间")
        if not 1 <= top_k <= 10:
            raise ValueError("上限必须在 1–10 之间")
        if min_results < 0:
            min_results = 0
        image_name = os.path.basename(image_path) if image_path else ""
        LOG.info("video-retrieval: query text=%r image=%r threshold=%.2f "
                 "top_k=%d min_results=%d debug=%s (index_size=%d)",
                 text, image_name, sim_threshold, top_k, min_results, debug,
                 len(self._index) if hasattr(self, "_index") else 0)
        with self._lock:
            if self._state not in (_STATE_READY, _STATE_IDLE):
                raise ValueError("请先完成视频切片提取")
            if not self._index:
                self._query = {"text": text, "image": image_name,
                               "sim_threshold": sim_threshold, "top_k": top_k}
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

        qvec = self.encode_query(text, image_path, check_image_quality)
        qvec = qvec / (np.linalg.norm(qvec) + 1e-9)

        with self._lock:
            index = list(self._index)
        result_hits, meta = _score_hits(
            qvec, index, sim_threshold, top_k, min_results)

        snap = None
        with self._lock:
            self._query = {"text": text, "image": image_name,
                           "sim_threshold": sim_threshold, "top_k": top_k}
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
        return get_registry().get_session(SoftwareCapability.VISION_EMBEDDING)

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
            self._frames_evaluated = 0
            self._frames_taken = 0
            self._frames_skipped = 0
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
            self._frames_evaluated = 0
            self._frames_taken = 0
            self._frames_skipped = 0
            self._judge.reset()
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
            self._frames_evaluated = 0
            self._frames_taken = 0
            self._frames_skipped = 0
            self._judge.reset()
            self._query = None
            self._hits = []
            self._index = []

    # ------------------------------------------------------------------ #
    # 判据（gscore）
    # ------------------------------------------------------------------ #
    def judge_snapshot(self) -> dict:
        return self._judge.snapshot()

    def set_judge(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            payload = {}
        try:
            self._judge.reconfigure(
                mode=payload.get("mode"),
                threshold=payload.get("threshold"),
                every_n=payload.get("every_n"),
            )
        except Exception as e:  # noqa: BLE001
            raise ValueError(str(e))
        self._judge.reset()
        snap = self._judge.snapshot()
        # 广播完整 state（含 judge 子对象）给页面；返回值只给判据快照，
        # 与三卡 ``/judge`` 端点同契约（共享前端组件 update() 直接吃它）。
        self._broadcast({"type": "state", **self.snapshot()})
        return snap

    def reset_judge(self) -> dict:
        self._judge.reset()
        return self._judge.snapshot()

    def _judge_fingerprint(self) -> str:
        j = self._judge
        return f"{j.mode}:{j.threshold:g}:{j.every_n}"

    # ------------------------------------------------------------------ #
    # 后台 worker：gscore 切片 + embedding
    # ------------------------------------------------------------------ #
    def _worker_loop(self) -> None:
        try:
            meta = _probe_video_meta(self._source)
            if meta is None or meta["fps"] <= 0:
                raise RuntimeError("无法读取视频元数据（fps / 帧数）")
            fps = meta["fps"]
            duration_s = (meta["duration_s"] if meta["duration_s"] > 0
                          else meta["total_frames"] / fps)
            if duration_s <= 0:
                raise RuntimeError("视频时长无效")
            with self._lock:
                self._fps = fps
                self._duration_s = duration_s
                self._slices_total = 0
                self._frames_evaluated = 0
                self._frames_taken = 0
                self._frames_skipped = 0
            self._broadcast({"type": "state", **self.snapshot()})
            session = self._ensure_session()
            lock = get_session_call_lock()
        except Exception as e:  # noqa: BLE001
            LOG.exception("vr extract worker error: %s", e)
            self._set_state(_STATE_ERROR, str(e))
            return

        last_progress = {"wall": 0.0}

        def _on_sample(_t: float, take: bool) -> None:
            with self._lock:
                self._frames_evaluated += 1
                if take:
                    self._frames_taken += 1
                else:
                    self._frames_skipped += 1
                evaluated = self._frames_evaluated
                embedded = self._slices_embedded
                total = self._slices_total
                duration = self._duration_s
            # 按**墙钟**每 1 秒广播一次（1Hz）。不能用采样时间 ``t`` 做
            # 节流：一个 block 的 4 帧是瞬间解完的，会一次连发 4 条。
            # 片段完成时另有一次带精确 embedded 的 progress。
            # 广播放在锁外 —— 回调（WS 入队）不该拖住计数锁。
            now = time.monotonic()
            if last_progress["wall"] == 0.0 or now - last_progress["wall"] >= 1.0:
                last_progress["wall"] = now
                self._broadcast({
                    "type": "progress",
                    "embedded": embedded,
                    "total": total,
                    "frames_evaluated": evaluated,
                    "duration_s": duration,
                })

        chunk_idx = 0
        try:
            for start, end_time, frames in iter_slices(
                    self._source, fps, duration_s,
                    judge=self._judge,
                    should_stop=self._stop_worker.is_set,
                    on_sample=_on_sample):
                if self._stop_worker.is_set():
                    return
                with self._lock:
                    self._slices_total += 1
                    total = self._slices_total
                LOG.debug("video-retrieval: slice %d [%.2fs-%.2fs, %d frames]",
                          chunk_idx, start, end_time, len(frames))
                try:
                    import time as _time
                    t_chunk = _time.time()
                    vec = None
                    # FastDDS 首次调用 reply topic discovery race → client 端
                    # timeout。已匹配后不会再 race，所以 timeout 后立即重试即可。
                    for _attempt in range(2):
                        try:
                            with lock:
                                vec = session.embed_video(frames, prompt="")
                            break
                        except RuntimeError as rpc_err:
                            if "timeout" not in str(rpc_err).lower() or _attempt == 1:
                                raise
                            LOG.warning("video-retrieval: slice %d embed_video "
                                        "timeout (reply discovery race), retrying",
                                        chunk_idx)
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
                                "total": total,
                                "frames_evaluated": self._frames_evaluated,
                                "duration_s": self._duration_s,
                            })
                    LOG.debug("video-retrieval: slice %d done in %.2fs (%d/%d)",
                              chunk_idx, _time.time() - t_chunk,
                              self._slices_embedded, total)
                except Exception as chunk_err:  # noqa: BLE001
                    LOG.warning(
                        "video-retrieval: slice %d [%.2fs-%.2fs] embedding failed "
                        "after %.2fs: %s — skipping",
                        chunk_idx, start, end_time, _time.time() - t_chunk,
                        chunk_err)
                chunk_idx += 1
        except Exception as e:  # noqa: BLE001
            LOG.exception("vr extract worker error: %s", e)
            self._set_state(_STATE_ERROR, str(e))
            return

        if self._stop_worker.is_set():
            return
        with self._lock:
            if self._state == _STATE_EXTRACTING:
                if self._slices_total == 0:
                    self._state = _STATE_ERROR
                    self._error = (
                        "未能从视频中取到任何片段（解码失败或时长为空），"
                        "请换文件重试。"
                    )
                elif self._slices_embedded == 0:
                    self._state = _STATE_ERROR
                    self._error = (
                        f"提取完成但 0/{self._slices_total} 切片成功 embedding — "
                        "所有切片解码或推理失败，请查看后端日志或换文件重试。"
                    )
                else:
                    self._state = _STATE_READY
                    self._error = ""
        LOG.info("video-retrieval: extract done — %d/%d slices embedded in %.1fs wall",
                 self._slices_embedded, self._slices_total,
                 time.time() - self._started_at)
        self._broadcast({"type": "state", **self.snapshot()})

        # ---- 全成功入库（部分成功不存，重跑可补全）。
        # 幂等覆盖键 = md5 + 切片参数 + 判据口径 + 切片版本。
        cache = get_video_cache()
        if (cache is not None and self._md5
                and self._slices_total > 0
                and self._slices_embedded >= self._slices_total):
            model_id = (getattr(session, "model_id", None)
                        or getattr(session, "_model_id", "") or "")
            if model_id:
                try:
                    with self._lock:
                        index_copy = list(self._index)
                        cache_meta = {
                            "filename": os.path.basename(self._source),
                            "source_path": self._source,
                            "fps": fps,
                            "duration_s": duration_s,
                            "slices_total": self._slices_total,
                            "slices_embedded": self._slices_embedded,
                            "chunk_seconds": SLICE_MAX_FRAMES,
                            "chunk_overlap_seconds": SLICE_OVERLAP_FRAMES,
                            "judge_fp": self._judge_fingerprint(),
                            "slice_version": "gscore-v1",
                        }
                    cache.save(self._md5, model_id, index_copy, cache_meta)
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("video-retrieval: cache save failed (skipped): %s", exc)
        elif self._slices_total > 0 and self._slices_embedded < self._slices_total:
            LOG.warning("video-retrieval: partial extraction (%d/%d) — not saved to "
                        "cache; re-extract to complete",
                        self._slices_embedded, self._slices_total)


_service: Optional[VideoSearchService] = None


def get_service() -> VideoSearchService:
    global _service
    if _service is None:
        _service = VideoSearchService()
    return _service
