"""视频内容检索路由（video_analysis_search 卡片的检索侧）。

由卡片 ``__init__.py`` 挂载到 ``/api/video-analysis-search/search/*``。
业务逻辑在 ``app.core.video.search``（遵守卡片零跨卡片 import 规则）。
字段名（``chunk_seconds`` / ``sim_threshold`` / ``top_k`` / ``start_time``
等）与前端契约保持不变。
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from app.core.video.search import get_service


LOG = logging.getLogger(__name__)
router = APIRouter(tags=["video-analysis-search"])


@router.get("/")
async def get_snapshot() -> dict:
    """当前快照（状态 / 进度 / 检索结果）。"""
    return get_service().snapshot()


@router.get("/cache/stats")
async def get_cache_stats() -> dict:
    """清空缓存按钮的提示条数来源：{enabled, max_entries, videos, rows}。"""
    from app.core.video.cache import get_video_cache
    c = get_video_cache()
    if c is None:
        return {"enabled": False, "max_entries": 0, "videos": 0, "rows": 0}
    return c.stats()


@router.get("/cache/list")
async def list_cache_entries() -> dict:
    """UI"已缓存视频"窗口数据源（按 analyzed_at 倒序）。

    每条 = (md5 × 切片参数) —— 同视频不同切片参数是不同行。
    ``source_path`` 为空（微信管线）或文件已不在本机的条目，
    ``frame_enabled=false`` —— 前端据此置灰取帧回看。
    """
    from app.core.video.cache import get_video_cache
    c = get_video_cache()
    if c is None:
        return {"enabled": False, "videos": []}
    # 窗口只显示 web 端（VCS）条目：渠道判别复用 source_path 双语义
    # （微信管线固定存 "" 占位 —— 见 wechat_video_cache.list_videos）。
    entries = c.list_videos(channel="web")
    for e in entries:
        e["frame_enabled"] = bool(
            e.get("source_path") and os.path.isfile(e["source_path"]))
    return {"enabled": True, "videos": entries}


@router.delete("/cache/entry")
async def delete_cache_entry(payload: dict) -> dict:
    """按行（md5 × 切片参数三元键）删除单个缓存条目。

    Body: ``{md5, chunk_seconds, chunk_overlap_seconds}``。同视频
    其它切片参数档的索引保留。
    """
    payload = payload or {}
    md5 = (payload.get("md5") or "").strip()
    chunk_seconds = int(payload.get("chunk_seconds") or 0)
    chunk_overlap_seconds = int(payload.get("chunk_overlap_seconds") or 0)
    if not md5:
        raise HTTPException(status_code=400, detail="md5 不能为空")
    from app.core.video.cache import get_video_cache
    c = get_video_cache()
    deleted = c.delete_video(md5, chunk_seconds, chunk_overlap_seconds) \
        if c is not None else 0
    LOG.info("vcs API: DELETE /cache/entry md5=%s chunk=%s → %d rows",
             md5[:12], chunk_seconds, deleted)
    return {"ok": True, "rows_deleted": deleted}


@router.post("/query_multi")
async def query_multi(payload: dict) -> dict:
    """对**一组已缓存条目**依次做文本检索（勾选路径，多选）。

    Body: ``{text, sim_threshold?, top_k?, min_results?,
    entries: [{md5, chunk_seconds, chunk_overlap_seconds}]}``。

    无状态批量语义：``embed_text`` 只算一次，然后逐条目从 lance
    还原向量做余弦检索 —— 不触碰当前单例的内存索引 / 状态机
    （"浏览选文件"路径不受勾选检索污染）。

    宽松匹配（``min_results > 0``）为**全局判定 + 全局兜底**：所有
    组合计无达标命中时，才把各组低于阈值的候选全局降序取 top
    ``min_results`` 条按来源组落位（共享一份配额，标 below）；任一
    组有达标命中则全部不兜底。返回::

        {groups: [{md5, filename, source_path, chunk_seconds,
          chunk_overlap_seconds, duration_s, slices, frame_enabled,
          hits, top_score, fallback_used, miss}]}
    （lance 里查不到的条目 → ``miss=True, hits=[]``）
    """
    payload = payload or {}
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="检索问题不能为空")
    entries = payload.get("entries") or []
    if not isinstance(entries, list) or not entries:
        raise HTTPException(status_code=400, detail="entries 不能为空")

    try:
        sim_threshold = float(payload.get("sim_threshold", 0.4))
        top_k = int(payload.get("top_k", 10))
        min_results = int(payload.get("min_results", 0) or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="参数类型错误")
    if not 0.0 <= sim_threshold <= 1.0:
        raise HTTPException(status_code=400, detail="阈值必须在 0–1.0 之间")
    if not 1 <= top_k <= 10:
        raise HTTPException(status_code=400, detail="上限必须在 1–10 之间")
    if min_results < 0:
        min_results = 0

    from app.core.video.cache import get_video_cache
    from app.core.compute.registry import get_session_call_lock
    c = get_video_cache()
    if c is None:
        raise HTTPException(status_code=409, detail="缓存未启用（cache_enabled=false 或 lancedb 不可用）")
    # 去重 + 截断条目数（防误传超大列表）
    clean: list = []
    seen = set()
    for e in entries:
        if not isinstance(e, dict):
            continue
        md5 = (e.get("md5") or "").strip()
        cs = int(e.get("chunk_seconds") or 0)
        ov = int(e.get("chunk_overlap_seconds") or 0)
        key = (md5, cs, ov)
        if md5 and key not in seen:
            seen.add(key)
            clean.append({"md5": md5, "chunk_seconds": cs,
                          "chunk_overlap_seconds": ov})
    if not clean:
        raise HTTPException(status_code=400, detail="entries 里的 md5 均为空")

    from app.core.video.search import _score_hits
    try:
        # 未启用模型 → RuntimeError → 502（与单视频 query 端点同语义）。
        session = get_service()._ensure_session()
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    lock = get_session_call_lock()
    with lock:
        qvec = session.embed_text(text)
    import numpy as np
    qvec = np.asarray(qvec, dtype=np.float32).reshape(-1)
    qvec = qvec / (float(np.linalg.norm(qvec)) + 1e-9)

    # ---- 两阶段评分（宽松匹配 = 全局判定 + 全局兜底） ----
    # 阶段 1：每组先做**严格**检索（min_results=0，阈值过滤 + 组内
    # top_k），累计全组合计的达标命中数。此前每组各自带 min_results
    # 兜底 —— N 个视频全无命中会兜出 N×top_k 条（每个视频各一份），
    # 与"一次总的宽松匹配"语义不符（用户决策 v0.12.4）。
    groups = []
    index_by_gi: list = []      # (group_dict, cached_index) 供阶段 2
    total_above = 0
    for e in clean:
        cached = None
        try:
            cached = c.lookup(e["md5"],
                              getattr(session, "model_id", None)
                              or getattr(session, "_model_id", "") or "",
                              e["chunk_seconds"], e["chunk_overlap_seconds"])
        except Exception as exc:  # noqa: BLE001
            LOG.warning("vcs query_multi: lookup %s failed: %s",
                        e["md5"][:12], exc)
        if cached is None:
            groups.append({
                "md5": e["md5"], "miss": True, "filename": "",
                "hits": [], "frame_enabled": False,
            })
            continue
        # video_path 不覆写 —— cache 索引无 video_path 字段；
        # hits 的取帧走 group.source_path。
        hits_, meta = _score_hits(
            qvec, cached["index"], sim_threshold, top_k, 0)
        total_above += int(meta["above_threshold_count"])
        source_path = cached.get("source_path") or ""
        g = {
            "md5": e["md5"],
            "miss": False,
            "filename": cached.get("filename") or "",
            "source_path": source_path,
            "frame_enabled": bool(source_path) and os.path.isfile(source_path),
            "chunk_seconds": e["chunk_seconds"],
            "chunk_overlap_seconds": e["chunk_overlap_seconds"],
            "duration_s": cached.get("duration_s") or 0.0,
            "slices": len(cached["index"]),
            "hits": hits_,
            "top_score": meta["top_score"],
            "above_threshold_count": meta["above_threshold_count"],
            "below_threshold_count": meta["below_threshold_count"],
            "fallback_used": False,
        }
        groups.append(g)
        index_by_gi.append((g, cached["index"]))

    # 阶段 2：全组合计**零达标命中**且勾选宽松匹配 → 收集所有组低于
    # 阈值的候选，全局降序取 top min_results 条（跨组共享一份配额，
    # 非"每组一份"），按来源组落位并标 below_threshold。有任一达标
    # 命中时不兜底（无命中的组只显示"无命中"提示行）。
    if min_results > 0 and total_above == 0 and index_by_gi:
        cands: list = []
        for gi, (g, index) in enumerate(index_by_gi):
            if g["hits"]:
                continue
            fb, _ = _score_hits(
                qvec, index, sim_threshold, top_k, min_results)
            for h in fb:
                if h.get("similarity", 0.0) >= sim_threshold:
                    continue
                cands.append((float(h["similarity"]), gi, h))
        cands.sort(key=lambda t: -t[0])
        for sim, gi, h in cands[:min_results]:
            g = index_by_gi[gi][0]
            h["above_threshold"] = False
            g["hits"].append(h)
            g["fallback_used"] = True
        LOG.info("vcs query_multi: global lenient fallback — %d strict "
                 "hits total=%d candidates, kept=%d",
                 total_above, len(cands), min(len(cands), min_results))
    return {"ok": True, "text": text, "sim_threshold": sim_threshold,
            "top_k": top_k, "groups": groups}


@router.post("/cache/clear")
async def clear_cache() -> dict:
    """一键删除 LanceDB 里全部已缓存的视频分析结果。

    幂等：缓存不可用 / 空表都返回 200（videos_cleared=0）。同时作废
    当前会话的内存索引（``get_service().reset_session()``）—— 持久缓存
    已删，会话索引若继续存在，前端 ``start_extract`` 会走 ``state=ready``
    短路跳过重新提取（表现为"进度条直接满格 + 历史无新条目"）。
    """
    from app.core.video.cache import get_video_cache
    c = get_video_cache()
    cleared = c.clear() if c is not None else 0
    get_service().reset_session()
    LOG.info("vcs API: POST /cache/clear → %d entries (session reset)", cleared)
    return {"ok": True, "videos_cleared": cleared}


@router.post("/open")
async def open_source(payload: dict) -> dict:
    """选择视频源。Body: ``{source: str}``（本期只允许单个视频文件）。"""
    payload = payload or {}
    source = (payload.get("source") or "").strip()
    if not source:
        raise HTTPException(status_code=400, detail="source 不能为空")
    return get_service().open(source)


@router.post("/extract")
async def start_extract(payload: dict) -> dict:
    """开始切片 embedding 提取。

    Body: ``{chunk_seconds: int, chunk_overlap_seconds: int,
    chunk_prompt?: str}``。参数值域以**前端档位**为准：
    chunk_seconds ∈ {3,4,5,6}，chunk_overlap_seconds ∈ {1,2}。
    """
    LOG.info("vcs API: POST /extract payload=%s", payload)
    payload = payload or {}
    try:
        chunk_seconds = int(payload.get("chunk_seconds", 4))
        chunk_overlap_seconds = int(payload.get("chunk_overlap_seconds", 1))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="切片参数必须为整数")
    try:
        return get_service().start_extract(chunk_seconds, chunk_overlap_seconds)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        # wdnai 缺失 / 功能未启用（qwen3-vl-embedding-2b 实例未启动）
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/extract/stop")
async def stop_extract() -> dict:
    """停止切片提取。"""
    return get_service().stop_extract()


@router.post("/query")
async def query_video(payload: dict) -> dict:
    """文本检索。Body: ``{text, sim_threshold, top_k, min_results?, debug?}``。

    ``sim_threshold`` 严格过滤相似度：返回的每条 hit 都满足
    ``similarity >= sim_threshold``。当 **没有任何** hit 达到阈值，
    默认返回空列表；调用方如果想看"最佳猜测"（即使低于阈值），
    显式传 ``min_results=N`` 启用 fallback 模式。
    """
    payload = payload or {}
    text = (payload.get("text") or "").strip()
    debug = bool(payload.get("debug", False))
    try:
        sim_threshold = float(payload.get("sim_threshold", 0.4))
        top_k = int(payload.get("top_k", 10))
        # 默认 0 = 严格模式，没有达到阈值的片段就返回空。
        # 设为 N>0 时启用 fallback：阈值过滤完后无命中时，
        # 补回 top-N（无视阈值），用于自检 / 调阈值。
        min_results = int(payload.get("min_results", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="阈值/上限必须为数值")
    # embed_text 是同步 NPU 推理：挪到线程池执行（不阻塞 event loop），
    # 并加 35s 超时兜底 —— SDK 卡死时前端不会无限 pending（对齐 VFA 做法）。
    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(
                None, get_service().query,
                text, sim_threshold, top_k, min_results, debug),
            timeout=35.0,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="检索超时：35s 内未完成（可能 NPU 繁忙）")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        # wdnai 缺失 / 功能未启用（实例在提取后被停掉等情况）
        raise HTTPException(status_code=503, detail=str(e))


@router.websocket("/ws")
async def vcs_ws(ws: WebSocket) -> None:
    """单向上行（server → client）。

    协议：
      - ``{"type":"hello", ...snapshot...}``  连上瞬间的初始快照
      - ``{"type":"progress", embedded, total}``  切片嵌入进度
      - ``{"type":"state", ...snapshot...}``  状态变更（完成 / 停止 / 出错）
    """
    await ws.accept()
    svc = get_service()
    loop = asyncio.get_event_loop()
    main_q: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=128)

    def _push(payload: dict) -> None:
        try:
            loop.call_soon_threadsafe(main_q.put_nowait, payload)
        except Exception:
            pass

    svc.register_broadcaster(_push)
    try:
        await ws.send_json({"type": "hello", **svc.snapshot()})
        while True:
            try:
                payload = await asyncio.wait_for(main_q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
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


@router.websocket("/play")
async def vcs_play_ws(ws: WebSocket) -> None:
    """播放命中片段：服务端按源 fps 顺序推 JPEG 帧。

    协议（双向）：
      客户端 → 服务端 首条 JSON：``{"source", "start", "end"}``
      服务端 → 客户端 binary：一帧 JPEG
      客户端 → 服务端：``{"action":"pause"}`` / ``{"action":"resume"}``
      服务端 → 客户端 JSON：``{"type":"eof"}`` / ``{"type":"error", detail}``

    后台 daemon 线程跑 ``iter_segment_jpeg``（PyAV 顺序解码 + JPEG 编码），
    经 ``loop.call_soon_threadsafe`` 投递到 asyncio 队列；WS 循环 drain。
    断开连接即停线程。暂停用 threading.Event 阻塞取帧循环。
    """
    from app.core.video.search import iter_segment_jpeg

    await ws.accept()
    loop = asyncio.get_event_loop()
    main_q: "asyncio.Queue" = asyncio.Queue(maxsize=16)
    pause_event = threading.Event()
    pause_event.set()   # 初始 = 未暂停；pause 时 clear()，resume 时 set()
    stop_event = threading.Event()
    thread: Optional[threading.Thread] = None
    get_task: Optional[asyncio.Future] = None
    recv_task: Optional[asyncio.Future] = None

    def _push_bytes(data: bytes) -> None:
        try:
            loop.call_soon_threadsafe(main_q.put_nowait, data)
        except Exception:
            pass

    def _push_json(payload: dict) -> None:
        try:
            loop.call_soon_threadsafe(main_q.put_nowait, ("json", payload))
        except Exception:
            pass

    def _reader(source: str, start: float, end: float) -> None:
        try:
            for jpeg in iter_segment_jpeg(source, start, end, paced=True,
                                          pause_event=pause_event):
                if stop_event.is_set():
                    return
                _push_bytes(jpeg)
            _push_json({"type": "eof"})
        except BaseException as e:
            LOG.warning("vcs-play reader error: %s", e)
            _push_json({"type": "error", "detail": f"播放取帧失败: {e}"})

    try:
        # 等首条参数
        try:
            first = await asyncio.wait_for(ws.receive_json(), timeout=10.0)
        except Exception:
            await ws.close(code=1008)
            return
        source = (first.get("source") or "").strip()
        try:
            start = float(first.get("start", 0))
            end = float(first.get("end", start + 1))
        except (TypeError, ValueError):
            await ws.close(code=1008)
            return
        if not source or not os.path.isfile(source) or end <= start:
            await ws.send_json({"type": "error", "detail": "参数无效或文件不可访问"})
            await ws.close()
            return

        thread = threading.Thread(
            target=_reader, args=(source, start, end), name="vcs-play", daemon=True
        )
        thread.start()

        # 帧队列 + 客户端控制消息**并发**等待：有帧立即发（不等控制，
        # 保持满帧率）；无帧时（暂停中）也能立刻响应 resume。
        # 旧实现"发帧后串行等控制"会在每帧叠加超时（帧率被拖慢），
        # 且超时 continue 会跳过收控制（暂停后收不到 resume）。
        recv_task = asyncio.ensure_future(ws.receive_json())
        while True:
            get_task = asyncio.ensure_future(main_q.get())
            done, _pending = await asyncio.wait(
                {get_task, recv_task},
                timeout=1.0,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if get_task in done:
                item = get_task.result()
                if isinstance(item, tuple):
                    await ws.send_json(item[1])
                    if item[1].get("type") in ("eof", "error"):
                        break
                else:
                    await ws.send_bytes(item)
            elif not get_task.done():
                get_task.cancel()

            if recv_task in done:
                try:
                    ctl = recv_task.result()
                except Exception:
                    break                    # 客户端断开
                action = (ctl or {}).get("action")
                if action == "pause":
                    pause_event.clear()      # 阻塞 reader（生成器内 wait）
                elif action == "resume":
                    pause_event.set()        # 放行 reader + 重置时钟基准
                recv_task = asyncio.ensure_future(ws.receive_json())
    except WebSocketDisconnect:
        return
    finally:
        for _t in (get_task, recv_task):
            if _t is not None and not _t.done():
                _t.cancel()
        # 先放行可能阻塞在 pause_event.wait() 的 reader（clear 会让它永久
        # 等待），再置 stop_event 让它尽快退出循环。
        pause_event.set()
        stop_event.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)


@router.get("/frame")
async def get_video_frame(source: str = "", t: float = 0.0) -> Response:
    """从源视频取指定时间点的帧（JPEG），用于命中卡片缩略图。

    Query: ``source``（视频文件路径）+ ``t``（绝对秒）。PyAV seek 到
    最近的 keyframe 后 decode 到目标时间点，RGA 硬转 BGR + JPEG 编码。
    """
    from fastapi.responses import Response

    source = (source or "").strip()
    if not source:
        raise HTTPException(status_code=400, detail="source 不能为空")
    if t < 0:
        raise HTTPException(status_code=400, detail="t 必须 >= 0")
    import os as _os
    if not _os.path.isfile(source):
        raise HTTPException(status_code=404, detail=f"视频文件不可访问: {source}")

    jpeg = _seek_frame_jpeg(source, t)
    if jpeg is None:
        raise HTTPException(status_code=404, detail="取帧失败（可能超出视频时长）")
    return Response(content=jpeg, media_type="image/jpeg")


def _seek_frame_jpeg(source: str, t: float) -> Optional[bytes]:
    """PyAV seek 到 ``t`` 秒附近的帧，JPEG 编码返回。失败返回 None。"""
    import av

    try:
        container = av.open(source, timeout=(2.0, 5.0))
    except Exception:
        return None
    try:
        stream = container.streams.video[0]
        time_base = float(stream.time_base)
        fps = float(stream.average_rate) if stream.average_rate else 0.0
        if time_base <= 0 or fps <= 0:
            return None
        target_frame = int(round(t * fps))
        offset = int((target_frame / fps) / time_base)
        try:
            container.seek(offset, backward=True, any_frame=False, stream=stream)
        except Exception:
            pass
        for frame in container.decode(video=0):
            if frame.pts is None:
                continue
            abs_time = float(frame.pts) * time_base
            frame_number = int(round(abs_time * fps))
            if frame_number >= target_frame:
                bgr = frame.to_ndarray(format="bgr24")
                import cv2
                ok, buf = cv2.imencode(
                    ".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 85]
                )
                return bytes(buf) if ok else None
    except Exception as e:  # pragma: no cover - defensive
        LOG.debug("vas-search: 取帧 %s t=%s 失败: %s", source, t, e)
    finally:
        container.close()
    return None


@router.get("/history")
async def list_unified_history() -> dict:
    """统一历史记录（检索缓存 + 分析历史，按视频 md5 聚合）。

    前端「视频分析检索」页的历史列表数据源 —— 一个视频一行，行内
    ``retrieval`` 是该视频的检索索引档位列表、``analysis`` 是其分析
    记录列表（含 run_id，供加载 / 反查 / 删除）。按 ``updated_at``
    倒序；``frame_enabled`` 按源文件是否仍在本机判定（决定能否取帧 /
    播放）。
    """
    from app.core.video.cache import get_video_cache
    c = get_video_cache()
    if c is None:
        return {"enabled": False, "items": []}
    c.checkout_latest()
    items = c.list_history_grouped(channel="web")
    for it in items:
        sp = it.get("source_path") or ""
        it["frame_enabled"] = bool(sp and os.path.isfile(sp))
    return {"enabled": True, "items": items}


@router.post("/query/clear")
async def clear_query() -> dict:
    """清空最近一次检索结果（query / hits），前端"清空"按钮调用。"""
    return get_service().clear_query()
