"""视频内容检索：REST + WebSocket。

后端功能本期为 mock（见 :mod:`app.services.video_content_search`），
前端先用这套契约跑通完整交互；后续按 vl_rag_demo 实现真实后端时
只替换 service 内部实现，字段名（``chunk_seconds`` / ``sim_threshold``
/ ``top_k`` / ``start_time`` 等）保持不变。
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from app.services.video_content_search import get_service


LOG = logging.getLogger(__name__)
router = APIRouter(tags=["video-content-search"])


@router.get("/video-content-search")
async def get_snapshot() -> dict:
    """当前快照（状态 / 进度 / 检索结果）。"""
    return get_service().snapshot()


@router.post("/video-content-search/open")
async def open_source(payload: dict) -> dict:
    """选择视频源。Body: ``{source: str}``（本期只允许单个视频文件）。"""
    payload = payload or {}
    source = (payload.get("source") or "").strip()
    if not source:
        raise HTTPException(status_code=400, detail="source 不能为空")
    return get_service().open(source)


@router.post("/video-content-search/extract")
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


@router.post("/video-content-search/extract/stop")
async def stop_extract() -> dict:
    """停止切片提取。"""
    return get_service().stop_extract()


@router.post("/video-content-search/query")
async def query_video(payload: dict) -> dict:
    """文本检索。Body: ``{text, sim_threshold, top_k}``。"""
    payload = payload or {}
    text = (payload.get("text") or "").strip()
    debug = bool(payload.get("debug", False))
    try:
        # Default threshold 0.5 + min_results 3: only strong matches pass,
        # but the user always gets the best top-3 with their actual
        # sim scores for self-diagnosis.
        sim_threshold = float(payload.get("sim_threshold", 0.5))
        top_k = int(payload.get("top_k", 10))
        min_results = int(payload.get("min_results", 3))
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


@router.websocket("/video-content-search/ws")
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


@router.websocket("/video-content-search/play")
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
    from app.services.video_content_search import iter_segment_jpeg

    await ws.accept()
    loop = asyncio.get_event_loop()
    main_q: "asyncio.Queue" = asyncio.Queue(maxsize=16)
    pause_event = threading.Event()
    pause_event.set()   # 初始 = 未暂停；pause 时 clear()，resume 时 set()
    stop_event = threading.Event()
    thread: Optional[threading.Thread] = None

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
            for jpeg in iter_segment_jpeg(source, start, end, paced=True):
                if stop_event.is_set():
                    return
                pause_event.wait()
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

        while True:
            try:
                item = await asyncio.wait_for(main_q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if isinstance(item, tuple):
                await ws.send_json(item[1])
                if item[1].get("type") in ("eof", "error"):
                    break
            else:
                await ws.send_bytes(item)

            # 非阻塞收客户端控制（暂停 / 恢复）
            try:
                ctl = await asyncio.wait_for(ws.receive_json(), timeout=0.01)
                action = ctl.get("action")
                if action == "pause":
                    pause_event.clear()      # 阻塞 reader
                elif action == "resume":
                    pause_event.set()        # 放行 reader
            except asyncio.TimeoutError:
                pass
            except Exception:
                break
    except WebSocketDisconnect:
        return
    finally:
        stop_event.set()
        pause_event.clear()
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)


@router.get("/video-content-search/frame")
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
        LOG.debug("vcs: 取帧 %s t=%s 失败: %s", source, t, e)
    finally:
        container.close()
    return None
