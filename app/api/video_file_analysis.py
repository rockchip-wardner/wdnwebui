"""视频文件分析：REST + WebSocket。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
from typing import Iterator, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from app.services.video_file_analysis import LOG, get_service


router = APIRouter(tags=["video-file-analysis"])


@router.get("/video-file-analysis")
async def get_snapshot() -> dict:
    """当前分析快照（状态 / 进度 / 综述）。"""
    return get_service().snapshot()


@router.get("/video-file-analysis/events")
async def list_events() -> dict:
    """per-frame 事件列表 + 当前综述。"""
    svc = get_service()
    return {
        "snapshot": svc.snapshot(),
        "events":   svc.events(),
        "summary":  svc.summary(),
    }


@router.post("/video-file-analysis/open")
async def open_analysis(payload: dict) -> dict:
    """启动一次文件级 VLM 分析。

    Body: ``{source: str, prompt: str, every_n: int}``
    """
    payload = payload or {}
    source = (payload.get("source") or "").strip()
    prompt = (payload.get("prompt") or "").strip()
    if not source:
        raise HTTPException(status_code=400, detail="source 不能为空")
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt 不能为空")
    try:
        every_n = int(payload.get("every_n", 30))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="every_n 必须为整数")
    if every_n < 1:
        raise HTTPException(status_code=400, detail="every_n 必须 >= 1")
    try:
        return get_service().start(source, prompt, every_n)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        # rkai 缺失 / rkv 缺失 / 功能未启用
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/video-file-analysis/stop")
async def stop_analysis() -> dict:
    """用户中途停止 —— 不出综述。"""
    return get_service().stop()


@router.post("/video-file-analysis/close")
async def close_analysis() -> dict:
    """彻底关闭（清空所有状态，可重新 open）。"""
    return get_service().close()


@router.post("/video-file-analysis/events/clear")
async def clear_events() -> dict:
    """清空事件 + 综述。运行中的分析不会被中断。"""
    get_service().clear()
    return {"ok": True}


# --------------------------------------------------------------------- #
# 历史 run 管理：每次分析结束 / 停止 / 出错都落盘到 runs_dir；这里
# 提供列表 / 加载 / 删除三个端点。"加载" 会把内存里的活跃状态替换成
# 磁盘上的旧 run（前端用它来回看任意一次历史分析）。
# --------------------------------------------------------------------- #
@router.get("/video-file-analysis/runs")
async def list_runs() -> dict:
    return {"runs": get_service().list_runs()}


@router.get("/video-file-analysis/runs/{run_id}")
async def load_run(run_id: str) -> dict:
    try:
        snap = get_service().load_run(run_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    svc = get_service()
    return {
        "snapshot": snap,
        "events":   svc.events(),
        "summary":  svc.summary(),
    }


@router.delete("/video-file-analysis/runs/{run_id}")
async def delete_run(run_id: str) -> dict:
    ok = get_service().delete_run(run_id)
    if not ok:
        raise HTTPException(status_code=404, detail="run not found")
    return {"ok": True}


# --------------------------------------------------------------------- #
# 取帧：反查结果点开后，回溯原始视频取出对应 frame_pos 的帧。
#
# rkv.VideoCapture 只暴露顺序 read()（无 seek API），所以每帧都要
# 从头顺序读取到 frame_pos。对长视频这是 O(N) 开销，但对用户典型
# 用法（点开最近几条结果）足够快（JPEG 编码 ~50ms/帧）。
#
# 同步取帧（每帧 read() 阻塞 ~10-30ms）× 30s hard timeout：上限
# 大约能取到 1000-3000 帧，覆盖 99% 实际点开的位置（every_n=60
# → 1 小时视频最深 ~3600 帧位置）。
# --------------------------------------------------------------------- #
@router.get("/video-file-analysis/runs/{run_id}/frame/{frame_pos}")
async def get_run_frame(run_id: str, frame_pos: int) -> Response:
    """从历史 run 对应的原始视频取出指定 frame_pos 的帧（JPEG）。"""

    if frame_pos < 0:
        raise HTTPException(status_code=400, detail="frame_pos 必须 >= 0")

    try:
        meta = get_service().load_run_meta(run_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    source = (meta.get("source") or "").strip()
    if not source:
        raise HTTPException(
            status_code=410,
            detail="run 没有 source 路径（可能来自老版本 / 跨设备迁移）",
        )

    # 安全检查：source 必须是普通文件（防止 source 被改写成设备/管道/socket）
    # 不限制在某个固定目录（用户分析的路径是任意的），但要拦掉非文件 / symlink 链外。
    import os
    if not os.path.isfile(source):
        raise HTTPException(
            status_code=404,
            detail=f"原始视频不可访问: {source}（可能已被移动/删除）",
        )
    if meta.get("is_live"):
        raise HTTPException(
            status_code=400,
            detail="live source 不支持回溯取帧（无确定 frame_pos）",
        )

    # fps 优先用实际取帧时从 stream 探测出来的值（PyAV / rkv 两条路径都
    # 各自测过）。fallback 顺序：探测 fps → meta JSON 里的 fps → 0。
    # 这样：
    #   - 新 run（meta 已存 fps）：两者一致，前端显示正常时间戳。
    #   - 老 run（meta 没存 fps）：探测 fps 仍然能拿到（视频文件没变的话），
    #     重新落盘后下次取帧就不再走 fallback。
    #   - meta fps 跟 source 文件 fps 不一致（极少见，比如落盘后转封装）：
    #     探测值优先。
    fps = float(meta.get("fps") or 0.0)

    # 同步跑取帧逻辑（在线程池里执行，避免阻塞 event loop）
    loop = asyncio.get_event_loop()

    def _seek_and_read_with_pyav() -> Optional[Tuple[bytes, float]]:
        """PyAV + V4L2 mem-to-mem 路径：硬件 H.264/HEVC 解码 + libavformat seek。

        跟 rkv 路径共用同一颗 RK3588 VPU，但 PyAV 多了 libavformat 的
        ``avformat_seek_file`` 能力 —— 可以直接跳到离 target 最近的 keyframe，
        不需要从 frame 0 一路顺序读到 target。

        返回 (jpeg_bytes, fps) — fps 来自实际 stream.average_rate，比
        run JSON 里读到的 fps 更可靠（老 run 可能没存 fps 字段，或者
        run JSON 跨设备迁移时 source 跟当时不同）。

        实测对比（960x540 H.264 @ 25fps 取 frame 1220）：
          rkv.read0() 从头读  : 18.8 s  (15.4 ms/帧 × 1220 帧)
          PyAV v4l2m2m + seek : 0.12 s  (seek 0 ms + decode 19 帧)
        加速 ~150x，主要赢在**跳过了 keyframe 之前的全部顺序读**。
        """
        import av

        target = int(frame_pos)
        container = av.open(source, timeout=(2.0, 5.0))
        try:
            stream = container.streams.video[0]
            time_base = float(stream.time_base)
            if time_base <= 0 or stream.average_rate is None:
                raise RuntimeError("视频流元数据缺失（time_base / frame_rate）")
            fps = float(stream.average_rate)
            if fps <= 0:
                raise RuntimeError("视频帧率异常")

            # PyAV seek 的 offset 单位是 stream.time_base（不是秒、不是帧号）。
            # target_frame → target_seconds → offset（time_base units）。
            target_seconds = target / fps
            offset = int(target_seconds / time_base)

            # 跳到离 target 最近的 keyframe（backward=True + any_frame=False）。
            # 容错：seek 失败不抛 —— 老 PyAV / 不支持的格式可能直接 raise。
            try:
                container.seek(offset, backward=True, any_frame=False, stream=stream)
            except Exception as e:
                LOG.debug("vfa: PyAV seek 失败（fallback 到 rkv）: %s", e)
                return None

            # Decode 找到 target 帧。
            # key —— seek 之后 frame.pts 保持绝对时间戳（pts × time_base = 绝对秒），
            # 所以我们能用 frame_number = round(abs_time × fps) 反推绝对帧号，
            # 不需要知道 seek 落点在哪。
            target_ndarray = None
            max_extra_frames = 300  # 安全上限：seek 后超过这个数还没到 target 视为失败
            for frame in container.decode(video=0):
                if frame.pts is None:
                    continue
                abs_time = float(frame.pts) * time_base
                frame_number = int(round(abs_time * fps))
                if frame_number >= target:
                    # PyAV 默认输出格式与 cv2 期望的 BGR 不一致（PyAV 输出
                    # AV_PIX_FMT_YUV420P / RGB 之类），用 format="bgr24" 强制
                    # 一次硬件转换（h264_v4l2m2m 后端会走 RGA）。
                    target_ndarray = frame.to_ndarray(format="bgr24")
                    break
                if frame_number > target + max_extra_frames:
                    break
            if target_ndarray is None:
                # 视频截断 / seek 没找对位置 → fallback 到 rkv。
                return None

            import cv2
            ok, buf = cv2.imencode(
                ".jpg",
                target_ndarray,
                [int(cv2.IMWRITE_JPEG_QUALITY), 85],
            )
            if not ok:
                raise RuntimeError("cv2.imencode 失败")
            return bytes(buf), fps
        finally:
            # PyAV container.close() 自动清理 libavcodec / mpp buffers，
            # 不需要手动 drain，process 退出时 mpp_buf_slot 干净。
            container.close()

    def _seek_and_read_with_rkv() -> Optional[Tuple[bytes, float]]:
        """rkv fallback：硬件 decode 但无 seek，只能从 frame 0 顺序读到 target。

        适用于 PyAV seek 失败 / 不支持的格式 / h264_v4l2m2m 不可用的情况。
        长视频中段取帧会比较慢（实测 15ms/帧 × target 帧），但至少能用。

        返回 (jpeg_bytes, fps) —— fps 从 cap.fps 读（rkv 在 __init__ 时
        已经从 rkv_dec_create 拿到了原视频流的平均帧率，O(1)）。
        """
        try:
            from rkv import VideoCapture as _RkvCap
        except Exception as e:
            raise RuntimeError(f"rkv 不可用: {e}")
        if _RkvCap is None:
            raise RuntimeError("rkv.VideoCapture 不可用（原生库缺失）")

        cap = _RkvCap(source, timeout=10.0)
        if not cap.isOpened():
            cap.release()
            raise RuntimeError(f"无法打开原始视频: {source}")

        # rkv 在打开时已经读了 fps（rkv_dec_create 内部从 demuxer 拿 average_rate），
        # 这里直接 cap.fps 拿，O(1) 不需要再开一次视频。
        fps = float(getattr(cap, "fps", 0.0) or 0.0)

        target = int(frame_pos)
        try:
            # cap.read0() 跳过 RGA，只 recv + release + 返回 DecodedFrame，
            # ~0.1ms / C 调用。长视频中段不可避免会慢，但已经比 cap.read() 省 RGA。
            target_ndarray: Optional[object] = None
            max_reads = max(target + 64, 1024)
            for _ in range(max_reads):
                ret, df = cap.read0()
                if not ret:
                    raise RuntimeError(
                        f"frame_pos={target} 超出视频实际帧数 "
                        f"（cap.frame_pos={cap.frame_pos}）"
                    )
                if cap.frame_pos >= target:
                    target_ndarray = df.numpy() if df is not None else None
                    if target_ndarray is None:
                        raise RuntimeError("target frame numpy() 失败")
                    break

            import cv2
            ok, buf = cv2.imencode(
                ".jpg",
                target_ndarray,
                [int(cv2.IMWRITE_JPEG_QUALITY), 85],
            )
            if not ok:
                raise RuntimeError("cv2.imencode 失败")
            result = bytes(buf)
            target_ndarray = None

            # Drain decoder 输出队列 + flush reference frames，避免 cap.release()
            # 时 mpp_buf_slot assertion 撞（reference frames 必须 EOS 才释放）。
            decoder = getattr(cap, "_decoder", None)
            if decoder is not None and not getattr(decoder, "_closed", True):
                idle = 0
                while idle < 5:
                    df = decoder.recv_frame(timeout_ms=5)
                    if df is None:
                        idle += 1
                        continue
                    idle = 0
                    decoder.release_frame(df)
                try:
                    decoder.send_packet(b"", pts=0, eos=True)
                except Exception as e:
                    LOG.debug("vfa: 取帧 send_packet(eos=True) 失败（忽略）: %s", e)
                idle = 0
                while idle < 10:
                    df = decoder.recv_frame(timeout_ms=10)
                    if df is None:
                        idle += 1
                        continue
                    idle = 0
                    decoder.release_frame(df)
            return result, fps
        finally:
            try:
                cap.release()
            except Exception:
                pass

    def _seek_and_read() -> Tuple[bytes, float]:
        """主入口：先试 PyAV+v4l2m2m+seek（快 ~150x），失败 fallback 到 rkv。

        选 PyAV 作首选的额外好处：
          - PyAV container.close() 自动清理 libavcodec / mpp，process 退出时
            mpp_buf_slot warning 比 rkv 路径少很多（实测干净退出）。
          - seek 落点后 decode ~keyframe_interval 帧，1-2 秒就够。

        返回 (jpeg_bytes, fps) —— fps 来自实际 stream 的探测值，比
        run JSON 里的字段更可靠（老 run 落盘时还没加 fps，或者 run JSON
        跨设备迁移时 source 跟落盘时不同）。
        """
        try:
            result = _seek_and_read_with_pyav()
            if result is not None:
                return result
            LOG.debug("vfa: PyAV 取帧失败，fallback 到 rkv 顺序读")
        except Exception as e:
            LOG.debug("vfa: PyAV 取帧异常，fallback 到 rkv: %s", e)
        return _seek_and_read_with_rkv()

    try:
        jpg_bytes, detected_fps = await asyncio.wait_for(
            loop.run_in_executor(None, _seek_and_read),
            timeout=30.0,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=f"取帧超时（30s），frame_pos={frame_pos} 太深 / 视频过大",
        )
    except Exception as e:
        LOG.warning("vfa: 取帧失败 run=%s frame_pos=%d: %s", run_id[:8], frame_pos, e)
        raise HTTPException(status_code=500, detail=f"取帧失败: {e}")

    # 探测 fps 优先于 meta 里的 fps（meta 可能是 0 / 跨设备迁移不一致）。
    if detected_fps > 0:
        fps = detected_fps

    return Response(
        content=jpg_bytes,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "X-Frame-Pos":    str(frame_pos),
            # 帧率。前端用 frame_pos / fps 算"该帧时间戳"。fps 是 float
            # 字符串（python repr），例如 "25.0"。fps=0 视为无效，前端
            # 隐藏时间戳行（老 run 落盘时还没加 fps 字段时取到 0）。
            "X-Fps":          str(fps),
            # 前端 openFrameModal 拿到 image 后用这个 header 回填
            # "原始视频" 那栏。HTTP header 值必须是 ASCII（RFC 7230），
            # 中文 / 非 ASCII 路径必须 percent-encode —— 否则 Starlette
            # 内部 latin-1 编码直接抛 UnicodeEncodeError。
            # safe="/" 让路径分隔符保持原样，前端 decode 后路径可读。
            "X-Source":      urllib.parse.quote(source, safe="/"),
        },
    )


# --------------------------------------------------------------------- #
# 反查（query）—— 在历史 run 内按 structured 字段过滤事件。
# 每个查询参数都是"包含子串"语义（中文 actor/object 不强求精确匹配）。
# 不指定任何条件时返回所有事件。
# --------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 真语义搜索：LLM 单次调用评分 + 排序
# --------------------------------------------------------------------------- #
SEMANTIC_PROMPT = (
    "评估每个事件与查询的相关性。\n"
    "\n"
    "【查询】\n"
    "{query}\n"
    "\n"
    "【事件列表】（每个事件含 id、frame 帧号、text 描述、structured 结构化字段）\n"
    "{events_json}\n"
    "\n"
    "【评分标准】（0-1 浮点数）\n"
    "- 0.9-1.0: 完整匹配查询的核心要点\n"
    "- 0.7-0.8: 匹配主要要点，遗漏次要细节\n"
    "- 0.4-0.6: 部分匹配（如查询\"雨天\"事件有\"天色阴沉\"，或查询\"穿红衣\"事件有\"红衣\"）\n"
    "- 0.1-0.3: 弱相关（场景相似但内容不同）\n"
    "- 0.0: 不相关\n"
    "\n"
    "注意：可以语义理解（如\"车\"匹配\"汽车\"、\"雨天\"匹配\"阴沉\"）。\n"
    "不要编造事件不存在的细节；只根据事件的 text 和 structured 字段判断。\n"
    "\n"
    "【输出格式】严格 JSON 数组（不要其他任何文字）：\n"
    "[{{\"id\": <event_id>, \"score\": <0.0-1.0>, \"reason\": \"<≤15字理由>\"}}]\n"
    "按 score 降序，只含 score >= 0.3 的事件。\n"
)


def _build_semantic_events(events: list) -> list:
    """压缩 event 数据为 LLM 看的紧凑 dict（text 截前 80 字，省 token）。"""
    out = []
    for ev in events:
        s = ev.get("structured") or {}
        out.append({
            "id":      ev.get("id"),
            "frame":   ev.get("frame_pos"),
            "text":    (ev.get("text") or "")[:80],
            "scene":   s.get("scene", ""),
            "actors":  s.get("actors", []),
            "action":  s.get("action", ""),
            "objects": s.get("objects", []),
        })
    return out


# 语义搜索 prompt 字符预算（events JSON 部分），控制单批 events 数
# Qwen3-VL-4B 窗口 8192 token ≈ 4000 字符（1 token ≈ 0.5 字符，4 字节/字符）。
# 留 ~2500 token 给 query/instruction/output（≈ 1000 字符输入 + 500 token 输出）
SEMANTIC_MAX_PROMPT_CHARS = 3000  # 留余量给 query + instruction + output
SEMANTIC_MAX_EVENTS     = 30      # 单批上限（之前 60 太激进，单批跑 30+s）
SEMANTIC_MAX_BATCHES    = 2       # 总批数硬上限（之前 5，200 events × 5 批 = 150s 超时）
SEMANTIC_MAX_NEW_TOKENS = 600     # 输出只需 [{id,score,reason} × N]，N=30 时 ~600 token 足够
SEMANTIC_FAST_PATH_LIMIT = 15     # 预过滤后候选数 ≤ 此值 → 跳过 LLM，按子串相似度排序


def _prefilter_events_for_query(events: list, query: str) -> list:
    """语义搜索预过滤：按 query 字符 + 2-gram 与 event 文本的重叠打分。

    目的：避免把 200 个 events 全丢给 LLM 评分。预过滤后保留"可能相关"
    的 events，LLM 只评这部分。

    评分（cheap heuristic，不调 LLM）：
      +2  每个 query 单字出现在 event text / scene / action / objects
      +1  每个 query 2-gram chunk（"出租车" → ["出租", "租车"]）匹配
      +0  完全不重叠 → 剔除（除非预过滤后 < 5 个 events 兜底补齐）

    兜底：query 同义词（如"出租车" ↔ "的士"）字符零重叠时，预过滤会
    误杀。补救：预过滤后少于 5 个 events 时，强制补到 5 个（取原始顺序
    前几个）让 LLM 兜底判断这些候选，宁可让 LLM 多评几个也别误杀。
    """
    if not events or not query:
        return events
    # query 拆单字 + 2-gram
    q = query.strip()
    chars = [c for c in q if not c.isspace()]
    bigrams = [q[i:i+2] for i in range(len(q) - 1)] if len(q) >= 2 else []
    if not chars and not bigrams:
        return events

    def _score(ev: dict) -> int:
        structured = ev.get("structured") or {}
        text = (ev.get("text") or "")
        haystacks = [text]
        for k in ("scene", "action", "note"):
            v = structured.get(k) or ""
            if v:
                haystacks.append(v)
        for v in (structured.get("actors") or []) + (structured.get("objects") or []):
            if v:
                haystacks.append(str(v))
        joined = "\n".join(haystacks)
        s = 0
        for c in chars:
            if c and c in joined:
                s += 2
        for bg in bigrams:
            if bg and bg in joined:
                s += 1
        return s

    # 用 enumerate 保位置稳定排序（避免 events.index 的 O(n²)）
    scored = [(s, i, ev) for i, (ev, s) in enumerate((ev, _score(ev)) for ev in events) if s > 0]
    if not scored:
        return events
    scored.sort(key=lambda x: (-x[0], x[1]))
    filtered = [ev for _, _, ev in scored]

    # 兜底：预过滤后 < 5 个时，强制补到 5 个（取原始顺序前几个），
    # 让 LLM 兜底判断同义词 / 弱相关候选（如"出租车" ↔ "的士"）。
    if len(filtered) < 5 and len(events) > len(filtered):
        seen_ids = {ev.get("id") for ev in filtered}
        for ev in events:
            if ev.get("id") in seen_ids:
                continue
            filtered.append(ev)
            seen_ids.add(ev.get("id"))
            if len(filtered) >= 5:
                break
    return filtered


def _sample_events(events: list, max_count: int) -> list:
    """等距采样：保留首尾和中间均匀分布。"""
    if len(events) <= max_count:
        return events
    n = len(events)
    step = n / max_count
    return [events[int(i * step)] for i in range(max_count)]


def _calc_batch_size(events: list) -> int:
    """根据 events 实际平均字符数，动态算能塞进 prompt 的 events 数量。

    sample 5 个 events 算 avg（比 sample 1 个稳定），
    budget 4000 chars 留给 query + instruction + output。
    """
    if not events:
        return 0
    sample_n = min(5, len(events))
    total_sample_chars = 0
    for ev in events[:sample_n]:
        total_sample_chars += len(json.dumps(_build_semantic_events([ev])[0], ensure_ascii=False))
    avg_chars = max(1, total_sample_chars // sample_n)
    # 留余量给 query + instruction + output
    budget = SEMANTIC_MAX_PROMPT_CHARS
    batch_size = min(SEMANTIC_MAX_EVENTS, budget // avg_chars)
    return max(1, int(batch_size))


def _parse_semantic_result(raw: str, by_id: dict, limit: int) -> list:
    """从 LLM 输出的 JSON 数组解析评分结果，还原成 events 列表。"""
    import re, json
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return []
    out = []
    for entry in arr:
        if not isinstance(entry, dict):
            continue
        ev_id = entry.get("id")
        score = entry.get("score", 0.0)
        try:
            score = float(score)
        except (TypeError, ValueError):
            continue
        if ev_id not in by_id or score < 0.3:
            continue
        out.append((ev_id, score, entry.get("reason", "")))
    out.sort(key=lambda x: -x[1])
    return out[:limit]


def _semantic_search(
    all_events: list, query: str,
    frame_min, frame_max, limit: int,
) -> dict:
    """LLM 语义搜索：1-N 次 LLM 调用给 events 打分排序。

    设计要点：
    - **预过滤**（_prefilter_events_for_query）：按 query 字符 + 2-gram 与
      event 文本的重叠打分，剔除明显不相关的 events，避免把 200 个
      events 全丢给 LLM 浪费推理。典型 200 events → 预过滤后 ~5-30 候选。
    - **快路径**：预过滤后候选数 ≤ SEMANTIC_FAST_PATH_LIMIT (8) 时，直接
      按字符重叠度排序返回，跳过 LLM（毫秒级响应），适用于 query 明确
      的场景（如"出租车"）。
    - **批数硬上限** SEMANTIC_MAX_BATCHES=2：超 2 批直接 truncate 到前
      SEMANTIC_MAX_BATCHES * SEMANTIC_MAX_EVENTS = 60 candidates，让总
      时间稳定在 60-90s 内。
    - **max_new_tokens=600**（之前 2000）：输出格式固定为紧凑 JSON 数组，
      30 个候选 × ~20 token = 600 足够。

    每次 LLM 喂 ≤ SEMANTIC_MAX_EVENTS (30) 个 events，prompt ≤ 3000 字符。
    """
    if frame_min is not None or frame_max is not None:
        all_events = [
            e for e in all_events
            if (frame_min is None or e.get("frame_pos", 0) >= frame_min)
            and (frame_max is None or e.get("frame_pos", 0) <= frame_max)
        ]
    total_events = len(all_events)
    if not total_events:
        return {"matched": 0, "limit": limit, "events": [], "ranked": True, "query": query,
                "sampled": False, "total_events": 0, "scored_count": 0, "batches": 0,
                "prefiltered": 0, "fast_path": False}

    # 关键：预过滤，避免 LLM 看到 200 个 events 评分
    candidate_events = _prefilter_events_for_query(all_events, query)
    prefiltered_count = len(candidate_events)
    sampled = prefiltered_count < total_events

    # 快路径：候选极少时跳过 LLM，按字符重叠度排序
    if prefiltered_count <= SEMANTIC_FAST_PATH_LIMIT:
        # 直接按预过滤的分数（即上面的 overlap score）排序
        # 复用 _prefilter_events_for_query 的排序结果
        # （函数已按分数降序返回，但这里我们要 events 没分数，重新算）
        ranked = _rank_by_overlap(candidate_events, query)
        out_events = []
        for ev in ranked[:limit]:
            d = dict(ev)
            d["_score"] = 1.0  # 快路径不打分，统一标记 1.0
            d["_reason"] = "(快路径：字符重叠匹配)"
            out_events.append(d)
        return {
            "matched": len(out_events),
            "limit": limit,
            "events": out_events,
            "ranked": True,
            "query": query,
            "sampled": sampled,
            "total_events": total_events,
            "scored_count": len(out_events),
            "batches": 0,
            "prefiltered": prefiltered_count,
            "fast_path": True,
        }

    from app.services.video_file_analysis import get_service
    svc = get_service()
    import json as _json

    # 限制 batch_size：单批 ≤ SEMANTIC_MAX_EVENTS (30) events
    char_batch_size = _calc_batch_size(candidate_events[:3])
    # 限制总批数 ≤ SEMANTIC_MAX_BATCHES (2)，避免用户等几十秒
    min_batch_size = max(1, (prefiltered_count + SEMANTIC_MAX_BATCHES - 1) // SEMANTIC_MAX_BATCHES)
    batch_size = min(
        SEMANTIC_MAX_EVENTS,
        max(char_batch_size, min_batch_size),
    )
    if batch_size < 1:
        batch_size = SEMANTIC_MAX_EVENTS

    # 截断到最多 2 批（batch_size × 2 candidates），保证总时间可控
    max_candidates = batch_size * SEMANTIC_MAX_BATCHES
    if len(candidate_events) > max_candidates:
        candidate_events = candidate_events[:max_candidates]
        sampled = True

    # 分批
    batches = []
    for i in range(0, len(candidate_events), batch_size):
        chunk = candidate_events[i:i + batch_size]
        if len(chunk) > batch_size:
            chunk = _sample_events(chunk, batch_size)
        batches.append(chunk)
    scored_count = 0

    all_scored = []  # 全部 [(id, score, reason), ...]
    for batch_idx, batch in enumerate(batches):
        events_compact = _build_semantic_events(batch)
        prompt = SEMANTIC_PROMPT.format(
            query=query,
            events_json=json.dumps(events_compact, ensure_ascii=False, indent=0),
        )
        try:
            raw = svc._call_llm(
                prompt, frame_bgr=None, max_new_tokens=SEMANTIC_MAX_NEW_TOKENS,
            )
        except Exception as e:
            LOG.error("vfa: semantic batch %d LLM failed: %s", batch_idx, e)
            continue  # 这一批失败，跳过但继续下一批
        if not raw or raw.startswith("("):
            LOG.warning("vfa: semantic batch %d LLM unavailable: %s", batch_idx, raw[:60])
            continue
        scored = _parse_semantic_result(raw, {e["id"]: e for e in batch}, limit=batch_size)
        all_scored.extend(scored)
        scored_count += len(scored)

    if not all_scored:
        return {"matched": 0, "limit": limit, "events": [],
                "ranked": True, "query": query, "error": "all batches failed",
                "sampled": sampled, "total_events": total_events,
                "scored_count": 0, "batches": len(batches),
                "prefiltered": prefiltered_count, "fast_path": False}

    # 合并去重（同一 event 可能出现在多批，保留 score 最高的）
    best_by_id: dict = {}
    for ev_id, score, reason in all_scored:
        if ev_id not in best_by_id or score > best_by_id[ev_id][1]:
            best_by_id[ev_id] = (ev_id, score, reason)

    # 全局按 score 降序排序，截 limit
    sorted_results = sorted(best_by_id.values(), key=lambda x: -x[1])[:limit]

    # 拼 events 完整 dict
    by_id_full = {e["id"]: e for e in all_events}
    out = []
    for ev_id, score, reason in sorted_results:
        ev = by_id_full.get(ev_id)
        if not ev:
            continue
        d = dict(ev)
        d["_score"] = score
        d["_reason"] = reason
        out.append(d)

    return {
        "matched": len(out),
        "limit": limit,
        "events": out,
        "ranked": True,
        "query": query,
        "sampled": sampled,
        "total_events": total_events,
        "scored_count": scored_count,
        "batches": len(batches),
        "prefiltered": prefiltered_count,
        "fast_path": False,
    }


def _rank_by_overlap(events: list, query: str) -> list:
    """快路径排序：按 query 字符 + 2-gram 与 event 文本的命中数降序。

    与 _prefilter_events_for_query 同款打分（重叠越多越相关），但这里
    输出排序后的完整 list 而不是过滤后的子集。返回 list[event]。
    """
    if not events or not query:
        return events
    q = query.strip()
    chars = [c for c in q if not c.isspace()]
    bigrams = [q[i:i+2] for i in range(len(q) - 1)] if len(q) >= 2 else []

    def _score(ev: dict) -> int:
        structured = ev.get("structured") or {}
        text = (ev.get("text") or "")
        haystacks = [text]
        for k in ("scene", "action", "note"):
            v = structured.get(k) or ""
            if v:
                haystacks.append(v)
        for v in (structured.get("actors") or []) + (structured.get("objects") or []):
            if v:
                haystacks.append(str(v))
        joined = "\n".join(haystacks)
        s = 0
        for c in chars:
            if c and c in joined:
                s += 2
        for bg in bigrams:
            if bg and bg in joined:
                s += 1
        return s

    # 稳定排序：同分按原顺序
    return sorted(events, key=lambda ev: -_score(ev))


@router.get("/video-file-analysis/runs/{run_id}/query")


async def query_run(
    run_id: str,
    scene: Optional[str] = None,
    # 多值 query 参数必须显式 Query(...) —— FastAPI 不会把
    # Optional[List[str]] = None 自动当 list 解析（默认会拿 None）。
    actor: Optional[List[str]] = Query(default=None),
    object: Optional[List[str]] = Query(default=None),  # noqa: A002
    action: Optional[List[str]] = Query(default=None),
    text: Optional[str] = None,
    # 模糊搜索（n-gram 跨字段匹配）：query 切成 2-gram chunks，任一 chunk
    # 出现在任一字段就命中 —— 支持"雨天穿红衣"模糊匹配 text 里的
    # "天阴沉" / "穿红衣的男人"。三个搜索模式（q / exact / semantic）
    # 互斥：q 与 exact 不能同时传（前端 UI 已经避免）。
    q: Optional[str] = None,
    # 精准匹配搜索（完整字符串匹配）：query 必须**完整**作为子串出现在
    # text 或 structured 任一字段中才命中。比 q 严格 —— "出租车"只能
    # 命中确实含"出租车"字符的 event，不会被 2-gram 拆词。
    exact: Optional[str] = None,
    # 真语义搜索（LLM 评分 + 排序，1 次 LLM 调用，与 q / exact 互斥）
    semantic: Optional[str] = None,
    frame_min: Optional[int] = None,
    frame_max: Optional[int] = None,
    limit: int = 200,
) -> dict:
    """反查指定 run 的事件。

    所有过滤参数都是可选的，不传 = 不过滤。``limit`` 默认 200，
    避免一次返回太多事件。

    多个字段是 AND 关系。``actor`` / ``object`` / ``action`` 是数组
    包含匹配（任一项包含子串即命中），适合前端 multi-select chips。

    搜索模式优先级：``semantic`` > ``exact`` > ``q``。同时传多个时按优先级
    只走一种（语义 → 精准 → 模糊），前端 UI 通常只让用户填一个。
    """
    try:
        from app.services.video_file_analysis import get_service
        svc = get_service()
        # 走 load_run 让历史 run 进入内存
        svc.load_run(run_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    all_events = svc.events()
    # 真语义搜索分支：1 次 LLM 调用，LLM 给所有 events 打 0-1 相关分。
    # 重要：_semantic_search 是 sync 函数（内部用 asyncio.run 调 LLM），
    # 在 async FastAPI endpoint 里直接调会触发 "asyncio.run() cannot be
    # called from a running event loop"。丢到 thread pool 后新线程没有
    # running loop，asyncio.run 正常工作。
    if semantic and semantic.strip():
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, _semantic_search,
            all_events, semantic.strip(), frame_min, frame_max, limit,
        )
    # 精准匹配 / 模糊搜索走纯 Python 路径，毫秒级返回，不需要 thread pool。
    # 互斥处理：exact 优先于 q（前端 UI 通常只让用户填一个）。
    effective_exact = (exact or "").strip() if exact else ""
    effective_q = (q or "").strip() if q else ""
    out = []
    for ev in all_events:
        structured = ev.get("structured") or {}
        text_body = ev.get("text") or ""
        # structured 路径（新 run）：strict match
        if structured:
            if scene is not None and (scene not in (structured.get("scene") or "")):
                continue
            if actor is not None:
                actors = structured.get("actors") or []
                if not any(a in act for a in actor for act in actors):
                    continue
            if object is not None:
                objects = structured.get("objects") or []
                if not any(o in obj for o in object for obj in objects):
                    continue
            if action is not None:
                act = structured.get("action") or ""
                if not any(a in act for a in action):
                    continue
        else:
            # 老 run 兜底：structured 为空时退到 text substring 匹配
            # （更宽松，接受 scene/actor/object/action 关键词直接在 text 里出现）
            if scene is not None and scene not in text_body:
                continue
            if actor is not None and not any(a in text_body for a in actor):
                continue
            if object is not None and not any(o in text_body for o in object):
                continue
            if action is not None and not any(a in text_body for a in action):
                continue
        if text is not None and (text not in text_body):
            continue
        # 精准匹配搜索 exact：完整 query 字符串必须**作为子串**出现在
        # text 或 structured 任一字段中。比 q 严格 —— 不切 n-gram，"出租
        # 车"只命中确实含"出租车"完整字符串的 event。逐字段扫：
        #   text_body / scene / action / note / actors / objects
        if effective_exact:
            exact_lower = effective_exact  # 不做大小写折叠（中文无意义）
            exact_candidates = [
                text_body,
                structured.get("scene") or "",
                structured.get("action") or "",
                structured.get("note") or "",
            ]
            for a in structured.get("actors") or []:
                exact_candidates.append(a or "")
            for o in structured.get("objects") or []:
                exact_candidates.append(o or "")
            # 兜底：老 run structured 为空时已经在 text 兜底分支匹配过
            # 这里再扫一次 text_body 即可（fallback 分支 text_body 含场景/动作）
            if not any(exact_lower in cand for cand in exact_candidates):
                continue
        # 模糊搜索 q：n-gram 跨字段匹配（text + structured 全部字段）
        # 把查询切成 2-gram chunks，任一 chunk 出现在任一字段 → 命中
        # 这样支持 "雨天穿红衣" 模糊匹配 text 里的 "天阴沉" / "穿红衣的男人"
        if effective_q:
            if len(effective_q) == 1:
                chunks = [effective_q]
            else:
                chunks = [effective_q[i:i+2] for i in range(len(effective_q) - 1)]
            candidates = [
                text_body,
                structured.get("scene") or "",
                structured.get("action") or "",
                structured.get("note") or "",
            ]
            for a in structured.get("actors") or []:
                candidates.append(a or "")
            for o in structured.get("objects") or []:
                candidates.append(o or "")
            # 也扫一下 structured 顶层所有 string 值
            for v in structured.values():
                if isinstance(v, str) and v:
                    candidates.append(v)
            if not any(any(c in cand for c in chunks) for cand in candidates):
                continue
        fp = ev.get("frame_pos") or 0
        if frame_min is not None and fp < frame_min:
            continue
        if frame_max is not None and fp > frame_max:
            continue
        out.append(ev)
        if len(out) >= limit:
            break
    return {
        "run_id": run_id,
        "matched": len(out),
        "limit":   limit,
        "events":  out,
    }


# --------------------------------------------------------------------------- #
# Text 兜底：从老的 free-form per-frame 文本里正则抽 scene/actor/object。
# 老 run（无 structured 字段）能借此生成 facets 候选 + 支持 text 模糊匹配。
# --------------------------------------------------------------------------- #
# 常见场景关键词：办公 / 街道 / 室内 / 室外 / 走廊 / 厨房 / 会议室 / 餐厅 / 教室
# 常见人物关键词：人 / 男 / 女 / 小孩 / 老人 / 宠物 / 狗 / 猫 / 行人 / 顾客
# 常见物体关键词：车 / 桌 / 椅 / 电脑 / 手机 / 门 / 窗 / 雨伞 / 灯
_SCENE_KW = (
    "办公室", "会议室", "街道", "走廊", "厨房", "客厅", "卧室", "餐厅",
    "教室", "商场", "超市", "医院", "银行", "工厂", "工地", "公园",
    "小区", "车库", "楼梯", "电梯", "室内", "室外", "门口", "入口",
    "大楼", "路边", "天桥", "地下", "停车场",
)
_ACTOR_KW = (
    "人", "男人", "女人", "小孩", "男孩", "女孩", "老人", "行人", "顾客",
    "医生", "老师", "学生", "警察", "司机", "工人", "店主", "宠物",
    "狗", "猫", "鸟",
)
# 注意：原先有 "主体" 关键词，但太宽泛 —— 会被 "### 画面主体" 这种
# "image subject" 语义误触发，从而把 "车型" 等车辆关键词收进 actor。
# 改为只用 "人物"。如果事件用 "人物" 作为 section header，仍能匹配。
_SECTION_HEADER_MAX_LEN = 40   # 超过这个字符数的 # 开头行不算 section header
_OBJECT_KW = (
    "车", "小车", "汽车", "卡车", "自行车", "电动车", "摩托车", "公交车",
    "桌子", "椅子", "电脑", "笔记本电脑", "手机", "门", "窗户", "雨伞",
    "灯", "包", "书", "纸", "笔", "杯子", "水杯", "话筒",
)


def _extract_facets_from_text(text: str) -> Iterator[Tuple[Optional[str], Optional[str], Optional[str]]]:
    """从 per-frame 老文本里提取 (scene, actor, object) 三元组。

    策略：找带 ``场景|地点|环境|概览`` / ``人物`` / ``物体|关键|物品``
    关键词的 markdown 标题行（``### 1. **场景概览**``），取**下一行非
    空内容**作为值。值先用关键词表匹配，取不到再退到首句截断。

    actor 只用 "人物"（不再用 "主体"）—— "主体" 太宽泛，会被 "画面主体"
    误归为人物 section，导致 vehicle type 进 actors。
    """
    if not text:
        return
    lines = [ln.strip() for ln in text.splitlines()]
    scene_val = actor_val = object_val = None

    for i, line in enumerate(lines):
        if not line:
            continue
        if scene_val is None and _is_section_header(line, ("场景", "地点", "环境", "概览")):
            scene_val = _pick_value(lines, i, _SCENE_KW)
        elif actor_val is None and _is_section_header(line, ("人物",)):
            actor_val = _pick_value(lines, i, _ACTOR_KW)
        elif object_val is None and _is_section_header(line, ("物体", "关键", "物品")):
            object_val = _pick_value(lines, i, _OBJECT_KW)
        if scene_val and actor_val and object_val:
            break
    if scene_val or actor_val or object_val:
        yield (scene_val, actor_val, object_val)


def _is_section_header(line: str, keywords) -> bool:
    """这行是不是带 keywords 的 markdown 标题？

    防误判：超过 _SECTION_HEADER_MAX_LEN 字符的 # 开头行不算 section
    header（VLM 经常把 "### 一、画面主体：一辆灰色小型电动汽车" 这种
    长标题当作 heading，但实际是带内容的描述）。
    """
    if not line.startswith("#"):
        return False
    if len(line) > _SECTION_HEADER_MAX_LEN:
        return False
    return any(k in line for k in keywords)


def _pick_value(lines, header_idx, keywords) -> Optional[str]:
    """从标题下一行非空行抽取值。优先匹配关键词表，取不到退到首句。"""
    import re
    for j in range(header_idx + 1, min(header_idx + 6, len(lines))):
        nxt = lines[j].strip()
        if not nxt:
            continue
        clean = nxt.lstrip("-* \t").replace("**", "")
        if not clean or clean.startswith("#"):
            break
        # 优先匹配关键词（更稳定的归一化值）
        kw = _first_kw(clean, keywords)
        if kw:
            return kw
        # 退到首句（第一个标点 / 换行前的部分）
        m = re.match(r"^([^。,，.;；:：\n\r]+)", clean)
        if m:
            v = m.group(1).strip().rstrip("，,;；:")
            if v and len(v) >= 2:
                return v[:24]
        return clean[:24]
    return None


def _first_kw(line: str, keywords) -> Optional[str]:
    """行内第一个匹配的关键词。"""
    for kw in keywords:
        if kw in line:
            return kw
    return None


def _is_vehicle_like(value: str) -> bool:
    """actor 清洗：粗略判断是不是车型 / 设备 / 物品关键词。

    历史 run 在落盘前 regex bug 会把 "车型" 这种非人关键词收进 actors。
    """
    if not value:
        return False
    for kw in _OBJECT_KW:
        if kw in value:
            return True
    return any(s in value for s in ("车型", "SUV", "汽车", "轿车", "卡车"))


def _strip_label(line: str) -> str:
    """去掉 markdown 加粗标签 / 列表符号，保留正文。

    输入示例：
      "### 1. **场景定位：小区出入口**"        → "小区出入口"
      "**地点**：住宅小区入口，有门禁"             → "住宅小区入口，有门禁"
      "- 中心是一个带岗亭的出入口"                  → "中心是一个带岗亭的出入口"
    """
    s = line.lstrip("#*>·- \t")
    s = s.replace("**", "")
    # 去掉编号前缀 "1." "2." 等
    import re
    s = re.sub(r"^\s*\d+\.\s*", "", s)
    # 截到第一个标签边界
    for sep in ("：", ":"):
        if sep in s:
            s = s.split(sep, 1)[1].strip()
            break
    # 截掉尾部残留的 markdown 强调
    s = s.rstrip(" *")
    return s[:40]


@router.get("/video-file-analysis/runs/{run_id}/facets")
async def facets_run(run_id: str) -> dict:
    """返回 run 内 scene / actor / object 的去重集合 —— 给前端
    autocomplete / 过滤面板做候选下拉。

    老 run（无 ``structured`` 字段）走 text 兜底：从每条 text 里正则
    抓 ``场景|地点|环境`` / ``人物|人|主体`` / ``物体|物品|关键`` 段
    的列表，作为候选下拉（精度有限，但旧 run 也能反查）。
    """
    try:
        svc = get_service()
        svc.load_run(run_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    scenes: set = set()
    actors: set = set()
    objects: set = set()
    action_counter: dict = {}  # value -> count（按频次排序给前端 chip 排序）
    for ev in svc.events():
        s = ev.get("structured") or {}
        if s.get("scene"):
            scenes.add(s["scene"])
        # 过滤"车型"等混入 actors 的非人关键词（兼容早期 regex bug 落盘）
        for a in s.get("actors") or []:
            if a and not _is_vehicle_like(a):
                actors.add(a)
        for o in s.get("objects") or []:
            if o:
                objects.add(o)
        # action 频次统计（按出现次数排序，前端 chip 展示 top 15）
        act = (s.get("action") or "").strip()
        if act:
            action_counter[act] = action_counter.get(act, 0) + 1
        # text 兜底 —— 老 run 没 structured 时按行匹配中文关键词
        if not s:
            for scene, actor, object_ in _extract_facets_from_text(
                ev.get("text") or ""
            ):
                if scene:    scenes.add(scene)
                if actor:    actors.add(actor)
                if object_:  objects.add(object_)
    # action 按频次降序，频次相同按字符序升序，取 top 15
    actions_sorted = sorted(
        action_counter.keys(), key=lambda a: (-action_counter[a], a)
    )[:15]
    return {
        "run_id":  run_id,
        "scenes":  sorted(scenes),
        "actors":  sorted(actors),
        "objects": sorted(objects),
        "actions": actions_sorted,
    }


# --------------------------------------------------------------------- #
# WebSocket：推送 event / summary / state
# --------------------------------------------------------------------- #
@router.websocket("/video-file-analysis/ws")
async def vfa_ws(ws: WebSocket) -> None:
    """单向上行（server → client）。前端只读 snapshot 也用 REST 兜底。

    协议：
      - ``{"type":"event", "event": {...}}``  单帧推理完成
      - ``{"type":"summary", "summary": "...", "ts": ...}``  综述完成
      - ``{"type":"state", ...snapshot...}``  状态变更（EOF done / stopped / error）
      - ``{"type":"hello", ...snapshot...}``  连上瞬间的初始快照
    """
    await ws.accept()
    svc = get_service()
    loop = asyncio.get_event_loop()

    # 单播 fan-out：broadcaster 闭包通过 asyncio.run_coroutine_threadsafe
    # 把消息投到主事件循环。WS 断开时注销。
    main_q: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=256)

    def _push(payload: dict) -> None:
        # 后台线程（decode / worker）调这里 —— 用 call_soon_threadsafe
        # 把 put_nowait 调度到主循环线程；满了就丢，避免阻塞 VLM。
        try:
            loop.call_soon_threadsafe(main_q.put_nowait, payload)
        except Exception:
            pass

    svc.register_broadcaster(_push)

    last_event_id = 0
    # 跨多次连续分析（同一 WS 连接不重连）的 id 空间隔离：每次
    # ``VideoFileAnalysisService.start()`` 都会分配新 ``run_id`` 并把
    # ``_next_event_id`` 重置回 1；WS handler 的 ``last_event_id`` 是闭
    # 包内局部变量，不感知 run 切换会让第二轮所有 ``id > 0`` 的事件被过
    # 滤掉（表现为"第二次开始分析，逐帧结果 + 进度条未刷新"）。
    # 见 ``video_file_analysis.py:907`` ``_reset_state`` + ``start()``。
    last_run_id = ""
    try:
        # 初始快照
        await ws.send_json({"type": "hello", **svc.snapshot()})
        last_run_id = svc._run_id or ""
        # 把已经存在的事件补一遍（用户切换页面再回来时不丢上下文）
        for ev in svc.events():
            if ev["id"] > last_event_id:
                last_event_id = ev["id"]
                await ws.send_json({"type": "event", "event": ev})
        cur = svc.summary()
        if cur.get("summary"):
            await ws.send_json({"type": "summary", **cur})

        while True:
            try:
                payload = await asyncio.wait_for(main_q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # 心跳：每 2s 推一次 status（若状态变了）
                now = loop.time()
                if not hasattr(vfa_ws, "_last_status"):
                    vfa_ws._last_status = 0.0
                if now - vfa_ws._last_status >= 2.0:
                    vfa_ws._last_status = now
                    await ws.send_json({"type": "state", **svc.snapshot()})
                continue

            # run 边界检测：state / hello 广播带 ``run_id``，与上次记录
            # 不同即视为新一轮分析开始 —— 重置事件 id 过滤器，让新 run
            # 从 ``id=1`` 起的事件顺利通过。前端 ``vfaStart`` 已经在切到
            # ``/open`` 前清空 ``VFA.events``，这里不需要补发旧事件。
            if payload.get("type") in ("state", "hello"):
                new_run_id = payload.get("run_id", "") or ""
                if new_run_id and new_run_id != last_run_id:
                    last_run_id = new_run_id
                    last_event_id = 0

            if payload.get("type") == "event":
                ev = payload.get("event") or {}
                if ev.get("id", 0) > last_event_id:
                    last_event_id = ev["id"]
                    await ws.send_json(payload)
            else:
                await ws.send_json(payload)
    except WebSocketDisconnect:
        return
    except Exception as e:  # pragma: no cover - defensive
        try:
            await ws.send_json({"type": "error", "detail": str(e)})
        except Exception:
            pass
    finally:
        svc.unregister_broadcaster(_push)
