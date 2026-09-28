"""Shared video utility helpers (extracted from video_content_search).

These three private helpers were originally ``_cosine_similarity`` /
``_probe_video_meta`` / ``_cleanup_shm_session`` living inside
``app/services/video_content_search.py``. Both ``video_content_search``
and ``im_bridge`` consumed them — a pre-existing cross-coupling that the
zero-coupling rule prohibits. They've been moved here so either card can
import without touching the other.

Stable enough that both cards' behaviour stays identical; if either
helper grows card-specific branching it should move back to that card.
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional

import numpy as np

LOG = logging.getLogger("core.video.utils")


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """两个归一化向量的余弦相似度（内积）。"""
    return float(np.dot(a, b))


def probe_video_meta(source: str) -> Optional[dict]:
    """用 PyAV 探测 fps / 总帧数 / 时长。失败返回 None。

    多数 MP4 不在 moov 头写 ``stream.duration`` / ``stream.frames``，导致
    metadata 返回 0；此时回退到 ``container.decode(video=0)`` 顺序解全片
    拿真实 last_pts + 帧数（慢但对任何 PyAV 可解码的视频都准）。
    """
    if not source or not os.path.isfile(source):
        return None
    try:
        import av
        with av.open(source, timeout=(2.0, 5.0)) as container:
            stream = container.streams.video[0]
            fps = float(stream.average_rate) if stream.average_rate else 0.0
            total_frames = int(stream.frames) if stream.frames else 0
            duration_s = float(stream.duration * stream.time_base) if stream.duration else 0.0

            if (duration_s <= 0 or total_frames <= 0) and fps > 0:
                LOG.info(
                    "vcs: probe metadata 不全 (duration=%.1fs, frames=%d) — "
                    "fall back to full decode for %s",
                    duration_s, total_frames, source,
                )
                last_pts = 0.0
                count = 0
                for packet in container.demux(video=0):
                    for frame in packet.decode():
                        last_pts = float(frame.pts * frame.time_base) if frame.pts else last_pts
                        count += 1
                if count > 0:
                    duration_s = last_pts
                    total_frames = count

            return {
                "fps": fps,
                "total_frames": total_frames,
                "duration_s": duration_s,
            }
    except Exception as e:  # noqa: BLE001
        LOG.warning("probe_video_meta failed for %s: %s", source, e)
        return None


def cleanup_shm_session(shm_session_id: str) -> int:
    """Cleanup the /dev/shm fastdds / wdnwebui_vd_<id> file backing this session.

    Returns the number of files removed (0 or 1).
    """
    import glob
    if not shm_session_id:
        return 0
    pattern = f"/dev/shm/wdnwebui_vd_{shm_session_id}.bin"
    removed = 0
    for path in glob.glob(pattern):
        try:
            os.unlink(path)
            removed += 1
        except OSError:
            pass
    return removed


# ── Chunking helpers — shared by video_content_search + im_bridge ──


def compute_slices(fps: float, duration_s: float, chunk_seconds: int,
                   overlap_seconds: int) -> int:
    """按 chunk 参数计算切片总数，与 ``_worker_loop`` 的终止语义完全一致。

    遍历 start 序列（步进 = chunk_seconds - overlap_seconds），每片按
    1 秒/帧采样帧号、剔除超出总帧数的帧；空片即停；end_time（= start +
    实际帧数）达到或超过时长即停。重叠残片不计入。

    VCS 提取 worker 与微信视频管线（``wechat_video_manager``）共用 ——
    ``total`` 必须等于 worker 实际遍历的段数：此前微信侧用解析公式
    ``(duration - overlap) // step + 1``，在不能整除时**多算 1 段**
    （19s / chunk=4 / overlap=1 → 算 7 段、实际 6 段），"段数不足"
    被误判成"有切片失败"，部分成功分支对空 ``failures`` 取
    ``failures[0]`` 抛 IndexError，整条管线 failed。
    """
    total_frames = int(duration_s * fps)
    if fps <= 0 or total_frames <= 0:
        return 0
    step = chunk_seconds - overlap_seconds
    if step <= 0:
        return 0
    n = 0
    start = 0.0
    while start < duration_s:
        frame_numbers = [
            int(round((start + j) * fps))
            for j in range(chunk_seconds)
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




def _read_chunk_frames(source: str, start_s: float, chunk_seconds: int,
                       fps: float, max_frames: Optional[int] = None) -> List[np.ndarray]:
    """从 ``source`` 读出 [start_s, start_s + chunk_seconds) 区间内
    ``chunk_seconds`` 个等距帧（BGR ndarray），按 demo 语义。

    - PyAV ``avformat_seek_file`` 跳到最近的 keyframe，再顺序 decode 到
      目标时间点 —— 避免从头顺序读全片（与 VFA 取帧同一优化）。
    - 每帧缩放到 576×576（embedding 模型 vision 输入）。
    """
    import av
    LOG.info("vcs: PyAV opening %s for chunk @%.2fs (chunk_seconds=%d)",
             source, start_s, chunk_seconds)
    import time as _time
    t0 = _time.time()

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
        LOG.info("vcs: PyAV done in %.2fs — %d/%d frames decoded",
                 _time.time() - t0, len(frames), len(frame_numbers))
        return frames
    finally:
        container.close()




def _write_frames_to_shm(frames: List[np.ndarray], shm_session_id: str,
                        chunk_idx: int) -> List[str]:
    """Encode frames as JPEG and write to /dev/shm (tmpfs, RAM-backed).

    Returns a list of *absolute* paths the worker can ``cv2.imread()``.
    Filenames are deterministic from (session_id, chunk_idx, frame_idx) so
    a re-run of the same chunk overwrites cleanly. JPEG q=85 is fine
    because the per-frame file is ~50 KiB and the *list of paths* sent
    over cshim is only ~50 bytes total — well under the 64 KiB UDP /
    SHM transport limit that blocks the original inline base64 payload.
    """
    import cv2 as _cv2
    out_dir = "/dev/shm"
    paths: List[str] = []
    for fi, fr in enumerate(frames):
        p = f"{out_dir}/wdndds_vcs_{shm_session_id}_c{chunk_idx:04d}_f{fi:02d}.jpg"
        ok, buf = _cv2.imencode(".jpg", fr, [int(_cv2.IMWRITE_JPEG_QUALITY), 85])
        if not ok:
            raise RuntimeError(f"cv2.imencode failed for chunk {chunk_idx} frame {fi}")
        try:
            with open(p, "wb") as f:
                f.write(buf.tobytes())
        except OSError as exc:
            raise RuntimeError(
                f"/dev/shm 写入失败 {p}（fd 耗尽或 tmpfs 异常）: {exc}") from exc
        # fail-loud 自校验：写即 stat。tmpfs 同机对 worker 应立即可见；
        # 若此刻文件已消失/为空，说明存在外部清扫或写穿透假象 ——
        # 与其让 worker 端 imread 间接报错，不如在写端留真因铁证。
        try:
            st = os.stat(p)
            if st.st_size == 0:
                raise RuntimeError(
                    f"/dev/shm 写入后文件为空（可能被外部截断）: {p}")
            LOG.info("vcs: shm write ok %s (%d bytes)", p, st.st_size)
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"/dev/shm 写入后文件立即消失（存在外部清理者）: {p}") from exc
        paths.append(p)
    return paths


