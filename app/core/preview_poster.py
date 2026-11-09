"""卡片预览视频的「首帧海报」（poster）。

为什么需要它
------------
应用网格里每张卡都挂了一个 ``<video autoplay ...>`` 预览视频。``autoplay``
会让浏览器**整段下载**每个视频 —— 实测 9 张卡共 **3.2 MB**，占首屏总字节的
67%。在慢链路 / 走隧道的客户端上，这一项就是「网页一直转圈」的根因
（实测某个客户端加载同一个首页花了 76 秒）。

浏览器侧改不掉：只要 ``src`` 在，Chrome 就会发**开区间** Range
（``bytes 0-``），服务端照直把整个文件发过去。实测三种属性组合的过线字节：

============================  ==========
``autoplay`` + metadata       3.2 MB
无 autoplay + metadata         3.4 MB
``preload="none"``（仍带 src） 2.5 MB
**完全不设 src**               **0 B**
============================  ==========

所以解法是：**首屏用一张静态海报图（视频首帧的 JPEG）占位，鼠标悬停到那张卡
时才去加载视频**。海报由服务端从 mp4 抽第一帧生成，~16 KB/张，9 张合计
~144 KB —— 相比 3.2 MB 少 23 倍，而画面与原来几乎一致（静态帧 vs 动态帧）。

海报端点
--------
``GET /preview/<card_id>.jpg?v=<mtime>``（在 :mod:`app.main` 注册）。

* ``v=`` 是**预览视频的 mtime** —— 海报是视频派生的，视频变了 mtime 就变，
  URL 跟着变，于是海报可以吃 ``immutable`` 长期缓存（内容没变就不会重取）。
* 解码走 **PyAV**（FFmpeg 软解）：``wdnav`` 只有 MPP / RGA / V4L2 的**视频**
  能力，没有解码器，抽帧只能软解 —— 与 :mod:`app.core.audio.decode` 是
  **同一个例外**，不是新开的口子。
* 惰性导入 ``av`` / ``cv2``：它们是重 native 库，在模块 import 期加载会在
  pytest 收集期引发段错误（见 AGENTS.md 的记录），所以只在真的要出海报时
  才 import。
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Optional

LOG = logging.getLogger("core.preview_poster")

#: 海报最长边（像素）。预览视频是 608×352，缩到 480 宽 ≈ 16 KB JPEG；
#: 再小卡片上就看不清了（预览区就那么大）。
POSTER_MAX_WIDTH = 480

#: JPEG 质量。72 在「体积 / 观感」之间比较平衡（实测 16 KB/张）。
POSTER_QUALITY = 72

#: 「卡 id → preview.mp4 路径」的缓存有效期（秒）。
#:
#: 为什么要缓存：``discover()`` 会重扫 ``app/apps/*`` + 入口点，每次调用
#: 都要跑一遍文件系统。首页 ``/`` 本来就每次请求都调它，海报端点再跟着调
#: 一次会把「[skip] xxx: no manifest.json」的告警刷成 10 倍。
#: 30 秒的延迟意味着「刚装上的卡」最多 30 秒后才有海报 —— 可以接受。
_DISCOVERY_TTL = 30.0

#: card_id → (preview.mp4 的 mtime, JPEG 字节)
_poster_cache: dict[str, tuple[int, bytes]] = {}

#: 「卡 id → preview.mp4 路径」的缓存。
#:
#: 用**可变容器**而不是「模块级元组 + 重新赋值」—— 后者会让 Python 把
#: ``_discovery_cache`` 当成函数内的**局部变量**（函数体里有赋值），于是
#: 函数开头那句读取直接抛 UnboundLocalError（实测 500）。
#: 这里统一用 dict + 原地改键，绕开 global 声明。
_discovery_cache: dict = {"ts": 0.0, "table": {}}

_lock = threading.Lock()


def _preview_map() -> dict[str, Path]:
    """返回 ``{card_id: preview.mp4 路径}``，带 TTL 缓存。"""
    now = time.monotonic()
    with _lock:
        if now - _discovery_cache["ts"] <= _DISCOVERY_TTL:
            return _discovery_cache["table"]

    from app.core.manifest import discover as discover_cards

    table = {}
    for m in discover_cards():
        if not m.static_dir:
            continue
        p = m.static_dir / "preview.mp4"
        if p.is_file():
            table[m.id] = p

    with _lock:
        _discovery_cache["ts"] = time.monotonic()
        _discovery_cache["table"] = table
    return table


def preview_video_path(card_id: str) -> Optional[Path]:
    """卡 id → 该卡的 ``preview.mp4`` 路径；没有则 None。"""
    return _preview_map().get(card_id)


def poster_bytes(card_id: str, video_path: Optional[Path] = None) -> Optional[bytes]:
    """返回该卡预览视频首帧的 JPEG 字节；拿不到返回 ``None``。

    结果按**视频 mtime** 缓存 —— 视频换了内容（mtime 变）缓存自动失效。
    """
    if video_path is None:
        video_path = preview_video_path(card_id)
    if video_path is None:
        return None
    try:
        mtime = int(video_path.stat().st_mtime)
    except OSError:
        return None

    with _lock:
        hit = _poster_cache.get(card_id)
    if hit is not None and hit[0] == mtime:
        return hit[1]

    data = _encode_first_frame(video_path)
    if data is None:
        return None
    with _lock:
        _poster_cache[card_id] = (mtime, data)
    return data


def _encode_first_frame(video_path: Path) -> Optional[bytes]:
    """解码第一帧 → 缩放 → JPEG 编码。失败返回 ``None``（不抛）。"""
    # 惰性导入重 native 库（见模块 docstring）。
    try:
        import av
        import cv2
    except Exception as exc:            # noqa: BLE001
        LOG.warning("海报生成不可用（缺 av/cv2）: %s", exc)
        return None

    try:
        with av.open(str(video_path)) as container:
            streams = container.streams.video
            if not streams:
                return None
            arr = None
            for frame in container.decode(streams[0]):
                arr = frame.to_ndarray(format="rgb24")
                break
    except Exception as exc:            # noqa: BLE001
        LOG.warning("抽首帧失败 %s: %s", video_path, exc)
        return None
    if arr is None:
        return None

    h, w = arr.shape[:2]
    if w > POSTER_MAX_WIDTH:
        nw = POSTER_MAX_WIDTH
        nh = max(1, int(h * nw / w))
        arr = cv2.resize(arr, (nw, nh), interpolation=cv2.INTER_AREA)

    # to_ndarray(format="rgb24") 给的是 RGB，OpenCV 的 imencode 吃 BGR。
    ok, buf = cv2.imencode(
        ".jpg", arr[:, :, ::-1],
        [int(cv2.IMWRITE_JPEG_QUALITY), POSTER_QUALITY],
    )
    if not ok:
        return None
    return buf.tobytes()


def prewarm() -> None:
    """后台预热所有卡片的海报（避免第一次进网格时现解码）。

    失败只记日志 —— 海报是锦上添花，出不来时前端退化成灰底占位框，
    不该让整页挂掉。
    """
    try:
        for card_id, path in _preview_map().items():
            if poster_bytes(card_id, path) is None:
                LOG.warning("预热海报失败: %s", card_id)
    except Exception as exc:            # noqa: BLE001
        LOG.warning("预热海报异常（忽略）: %s", exc)
