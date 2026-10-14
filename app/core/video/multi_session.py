"""MultiChannelSession — 视频目标监控页的 1~32 路并发解码 + 轮转推理。

为什么另起一个类，而不是把 ``VideoSession`` 改成多路
----------------------------------------------------
``app.core.video.session.VideoSession`` 是**进程级单例**，且被三个页面共用
（``video_detection`` / ``video_target_detection`` /
``video_target_localization``）。它内部只有 **1 个** ``VideoCapture`` +
**1 个** 解码线程。把 32 路能力塞进去等于让另外两个页面的解码路径也承担
多路的复杂度与风险 —— 违反「改动只触及目标功能及其直接依赖」。

所以这里是一个**独立实现**：本页换用它，另外两页继续用原单例，零影响。
``N=1`` 时本类就是老行为的退化情况，一套代码覆盖 1~32 路。

线程模型
--------
::

N 个解码线程   各自 1 个 VideoCapture，按**每路自适应**的 decode_fps 节流 read0()
                   → latest_bgr（VLM 用） + 按每路自适应 display_fps 编码 JPEG（UI 用）
    1 个 VLM 调度   严格轮转 ch0→ch1→…→chN-1，每路等上一路推理完才提交下一路
    N 个 WS 订阅    每个订阅者拿 (channel_idx, jpeg_bytes)，drop-oldest

三个参数为什么是这个默认值（全部来自本机实测，见 AGENTS.md）

``analysis_size=384`` —— RGA 输出尺寸**决定 VLM 用哪个 vision 变体**。
wdnai 选变体的规则是「``image_size`` 最接近输入图**最长边**」；本机
384 / 768 两个变体都在，喂 640 会误选 768（权重 252MB vs 61MB，单帧
2.06s vs 0.74s）。所以按源宽高比缩到**最长边正好等于** ``analysis_size``。

``decode_fps`` —— **RGA 次数 == 解码帧数，绕不开**（``VideoCapture.read0()``
内部写死 ``frame._cached_bgr = frame.numpy()``）。真正的上限是**所有路加起来**
的 RGA 吞吐，实测墙在 750~800 ops/s（720P 源 → 384x216 输出）；32 路 × 25fps
= 800 正好压线且 **0 错误**。所以 ``decode_fps`` 只是**每路上限**，实际值由
:func:`derive_decode_fps` 按路数摊分：N 路共享 ``MAX_TOTAL_DECODE_FPS`` 的预算。
N <= 32（上限）时每路都能跑满源帧率 25fps，只有超上限的 N 才降帧。

``display_fps`` —— 同样是**按路数摊分的每路上限**。实测
``cv2.imencode('.jpg')`` 384x216 只要 **0.77ms/次**（不是老解码循环注释里写的
15~100ms，那个数字测的是另一档尺寸/负载），所以 UI 推送远比解码便宜。
总预算 ``MAX_TOTAL_DISPLAY_FPS``：路数少时每路接近源帧率（单路 25fps 流畅
播放，不卡），路数多时才降帧。

降级行为
--------
所有参数下**都不会抛错**：解码线程单路异常只记进该路 ``errors`` 并重建
``VideoCapture``，其余 N-1 路不受影响。能力不足表现为**掉帧**，不是失败。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from wdnav import VideoCapture as _WdnavVideoCapture
except Exception:  # pragma: no cover - platform-specific load failure
    _WdnavVideoCapture = None


LOG = logging.getLogger("multi_channel_session")

MAX_CHANNELS = 32
DEFAULT_ANALYSIS_SIZE = 384
# 每路 fps 的**上限**（不是实际值）。实际值按路数在总预算里摊分，
# 见 derive_decode_fps / derive_display_fps。25 是常见监控源帧率。
DEFAULT_DECODE_FPS = 25.0
DEFAULT_DISPLAY_FPS = 25.0
DEFAULT_JPEG_QUALITY = 70
OPEN_TIMEOUT = 10.0
# 所有路**合计**的 RGA 预算（次/秒）。实测墙 750~800 ops/s
# （720P 源 → 384x216 输出，32 路 × 25fps = 800 时 0 错误），
# 这里取 800：N<=32 时每路都能跑满 25fps。
MAX_TOTAL_DECODE_FPS = 800.0
# 所有路合计的 UI 推送预算（张/秒）。单张 384x216 JPEG 实测 0.77ms，
# 800 张/秒 ≈ 0.6 核，留足余量给 WS 发送与浏览器解码。
MAX_TOTAL_DISPLAY_FPS = 400.0
# 单路推理等待上限。正常单路 ~0.85~1.9s，30s 足够宽裕；超时的路直接跳过
# 继续轮下一路，不让一路卡死把整个轮转拖住。
RR_INFER_TIMEOUT = 30.0
# 一次「已完成的推理」耗时下限（毫秒）。正常单路 850~1900ms，远大于此；
# 若 wait_idle 亚毫秒返回，说明这一帧压根没进队列（见 _rr_loop 的退避分支）。
RR_MIN_INFER_MS = 100.0

STATE_IDLE = "idle"
STATE_OPENING = "opening"
STATE_PLAYING = "playing"
STATE_PAUSED = "paused"
STATE_EOF = "eof"
STATE_ERROR = "error"


def derive_analysis_size(src_w: int, src_h: int, size: int) -> Tuple[int, int]:
    """按源宽高比缩到「最长边 = size」，返回 (w, h)。

    必须**按源自己的宽高比**而不是硬编码 16:9：``letterbox_resize`` 是保比例
    的，4:3 的 384x288 在 384x384 画布里有效画面占 75%，16:9 的 384x216
    只占 56%。宽度对齐到 16 字节，避免 ``numpy()`` 再切一刀破坏连续性。
    """
    if src_w <= 0 or src_h <= 0:
        return size, size
    if src_w >= src_h:
        w = size
        h = max(2, (src_h * size) // src_w)
    else:
        h = size
        w = max(2, (src_w * size) // src_h)
    return (w + 15) & ~15, h


# 浏览器显示用的最长边，按路数分档。
#   1~4 路 → 960（实测单路 24.9fps / 0 错误，铺满播放器不糊）
#   5 路以上 → 回落 analysis_size（384）
DISPLAY_LONG_EDGE_HIGH = 960
DISPLAY_HIGH_MAX_CHANNELS = 4


def derive_display_size(n_channels: int, src_w: int, src_h: int,
                        analysis_size: int) -> Tuple[int, int]:
    """返回给**浏览器**的解码输出尺寸（也就是 RGA 输出尺寸）。

    显示与送检**分离**：显示尺寸只影响「用户看到的清晰度」，送检尺寸
    ``analysis_size`` 固定不变（它决定 VLM 选哪个 vision 变体 —— 384 变体
    权重 61MB / 单帧 0.74s，768 变体 252MB / 2.06s，不能随手放大）。

    分档依据：显示尺寸直接决定 RGA 输出字节量与 JPEG 体积
    （384×216 = 243KB/帧，960×540 = 1519KB/帧，6.2 倍）。路数少时这点开销
    可忽略，值得换清晰度；路数多时它就是硬约束，必须降回来。
    """
    long_edge = (DISPLAY_LONG_EDGE_HIGH
                 if n_channels <= DISPLAY_HIGH_MAX_CHANNELS
                 else analysis_size)
    return derive_analysis_size(src_w, src_h, long_edge)


def derive_decode_fps(n_channels: int, cap: float = DEFAULT_DECODE_FPS) -> float:
    """按路数摊分**总 RGA 预算**，返回每路的解码帧率。

    之前这里对所有路数一律返回 5.0，那是拿 32 路极限场景的保守值一刀切：
    开 1 路视频也只有 5fps，播放明显卡顿。正确做法是「N 路共享一个总预算」，
    路数少时每路跑满源帧率，路数多时才降帧 —— 因为降帧的代价是所有路一起
    变卡，而 RGA 是**所有路合计**受限的，不该让单路替多路背锅。

    实测（720P 源 → 384x216）：32 路 × 25fps 合计 797~800 ops/s，**0 错误**，
    所以预算取 800；N <= MAX_CHANNELS 时每路都能跑满 cap。
    """
    n = max(1, int(n_channels))
    budget = MAX_TOTAL_DECODE_FPS / n
    return max(1.0, min(float(cap), budget))


def derive_display_fps(n_channels: int, cap: float = DEFAULT_DISPLAY_FPS) -> float:
    """按路数摊分**总推送预算**，返回每路的 UI 显示帧率。

    与解码同理：单张 384x216 JPEG 只要 0.77ms，1~6 路时完全负担得起
    每路 25fps（流畅播放）；只有路数很多时才需要降 UI 推送频率。
    """
    n = max(1, int(n_channels))
    budget = MAX_TOTAL_DISPLAY_FPS / n
    return max(1.0, min(float(cap), budget))


def probe_size(path: str) -> Tuple[int, int]:
    """Demux-only 探测分辨率。只读容器元信息，不解码、不碰硬件。"""
    try:
        import av
        with av.open(str(path)) as container:
            ctx = container.streams.video[0].codec_context
            if ctx is not None and ctx.width and ctx.height:
                return int(ctx.width), int(ctx.height)
    except Exception as exc:  # noqa: BLE001
        LOG.debug("probe_size(%s) 失败: %s", path, exc)
    return 0, 0


class Channel:
    """一路视频 = 1 个解码线程 + 1 组最新帧槽。"""

    __slots__ = (
        "idx", "source", "state", "error", "width", "height", "fps",
        "frame_pos", "loop", "frames", "opens", "errors", "thread",
        "started_at", "last_frame_ts", "_lock", "_latest_bgr",
        "_out_w", "_out_h", "_stop", "_play", "_live", "_publish",
    )

    def __init__(
        self,
        idx: int,
        source: str,
        out_w: int,
        out_h: int,
        loop: bool,
        stop_evt: threading.Event,
        play_evt: threading.Event,
    ) -> None:
        self.idx = idx
        self.source = source
        self._out_w = out_w
        self._out_h = out_h
        self.loop = loop
        self.state = STATE_IDLE
        self.error = ""
        self.width = 0
        self.height = 0
        self.fps = 0.0
        self.frame_pos = 0
        self.frames = 0
        self.opens = 0
        self.errors: List[str] = []
        self.thread: Optional[threading.Thread] = None
        self.started_at = time.monotonic()
        self.last_frame_ts = 0.0
        self._live = "://" in source or source.startswith("/dev/video")
        self._lock = threading.Lock()
        self._latest_bgr: Any = None
        self._stop = stop_evt
        self._play = play_evt
        # 帧广播回调（由 MultiChannelSession 注入），把 JPEG 推给 WS 订阅者
        self._publish: Optional[Callable[[int, bytes], None]] = None

    # -- 采样侧 ------------------------------------------------------------ #
    def snapshot_bgr(self):
        """原子取最近一帧 BGR（**显示尺寸**）。不触发 RGA —— 解码时已转好。"""
        with self._lock:
            return self._latest_bgr

    def snapshot_bgr_for_analysis(self, w: int, h: int):
        """取当前帧并 RGA 缩到 (w, h) 送检（VLM 用）。

        显示尺寸可能大于送检尺寸（1~4 路时浏览器要 960 长边），所以这里
        按需缩一次。**只在轮转提交那一瞬调用**（每路 ~2s 一次），不是每帧
        都缩 —— 否则 RGA 次数翻倍，直接撞吞吐上限。

        缩放走 ``wdnav.rga.resize_bgr``（硬件），**不做软 resize 兜底**：
        RGA 失败就返回 None，让这一路本轮跳过（轮转循环已经处理
        ``frame is None``），宁可少一路也不要悄悄走软路径。
        """
        frame = self.snapshot_bgr()
        if frame is None:
            return None
        try:
            fh, fw = frame.shape[0], frame.shape[1]
        except Exception:  # noqa: BLE001
            return None
        if (fw, fh) == (w, h):
            return frame
        try:
            from wdnav.rga import resize_bgr
            return resize_bgr(frame, w, h)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("mc-ch%02d RGA 缩到 %dx%d 失败: %s", self.idx, w, h, exc)
            return None

    def set_frame(self, bgr) -> None:
        with self._lock:
            self._latest_bgr = bgr

    def snapshot(self) -> dict:
        el = max(time.monotonic() - self.started_at, 1e-6)
        return {
            "idx": self.idx,
            "source": self.source,
            "state": self.state,
            "error": self.error,
            "width": self.width,
            "height": self.height,
            "fps": round(self.fps, 2),
            "frame_pos": self.frame_pos,
            "loop": self.loop,
            "frames": self.frames,
            "decode_fps": round(self.frames / el, 2),
            "opens": self.opens,
            "errors": len(self.errors),
            "last_frame_ts": self.last_frame_ts,
            "has_frame": self.snapshot_bgr() is not None,
        }

    def start(self, decode_fps: float, display_fps: float,
              jpeg_quality: int) -> None:
        self.thread = threading.Thread(
            target=self._run,
            args=(decode_fps, display_fps, jpeg_quality),
            name=f"mc-dec-{self.idx:02d}",
            daemon=True,
        )
        self.thread.start()

    # -- 解码循环 --------------------------------------------------------- #
    def _run(self, decode_fps: float, display_fps: float,
             jpeg_quality: int) -> None:
        import cv2

        cap = None
        period = 1.0 / decode_fps if decode_fps > 0 else 0.0
        disp_period = 1.0 / display_fps if display_fps > 0 else 0.0
        next_disp = 0.0
        t0 = time.monotonic()
        target = 0
        try:
            while not self._stop.is_set():
                self._play.wait(timeout=0.3)
                if self._stop.is_set():
                    break
                if not self._play.is_set():
                    continue

                if cap is None:
                    try:
                        new_cap = _WdnavVideoCapture(self.source,
                                                     timeout=OPEN_TIMEOUT)
                    except Exception as exc:  # noqa: BLE001
                        self._fail(f"打开失败: {exc}")
                        self._stop.wait(1.0)
                        continue
                    if not new_cap.isOpened():
                        try:
                            new_cap.release()
                        except Exception:
                            pass
                        self._fail("VideoCapture.isOpened() = False")
                        self._stop.wait(1.0)
                        continue
                    cap = new_cap
                    self.opens += 1
                    self.width = int(getattr(cap, "width", 0) or 0)
                    self.height = int(getattr(cap, "height", 0) or 0)
                    self.fps = float(getattr(cap, "fps", 0.0) or 0.0) or 25.0
                    self.frame_pos = 0
                    self.state = STATE_PLAYING
                    self.error = ""
                    t0 = time.monotonic()
                    target = 0
                    # 与 t0 同源，固定步进才不会第一帧就疯狂追平
                    next_disp = t0

                try:
                    ret, frame0 = cap.read0(self._out_w, self._out_h)
                except Exception as exc:  # noqa: BLE001
                    self._note_error(f"read0 异常: {exc}")
                    self._drop_cap(cap)
                    cap = None
                    self._stop.wait(0.2)
                    continue

                if not ret or frame0 is None:
                    # EOF —— 循环重开，或停在 eof 等前端决定
                    self._drop_cap(cap)
                    cap = None
                    with self._lock:
                        self.frame_pos = 0
                    if self.loop and not self._live:
                        self.state = STATE_PLAYING
                        self._stop.wait(0.05)
                        continue
                    self.state = STATE_EOF
                    return

                try:
                    frame = frame0.numpy()
                except Exception:  # noqa: BLE001
                    # RGA 失败 —— 丢这一帧，不打断整路
                    continue
                if frame is None:
                    continue

                self.frames += 1
                self.last_frame_ts = time.time()
                try:
                    self.frame_pos = int(cap.frame_pos)
                except Exception:  # noqa: BLE001
                    pass

                # 只有到 display_fps 的点才做 JPEG 编码（实测 384x216 只要
                # 0.77ms/次，很便宜）。
                #
                # 这里**必须按固定步长推进 next_disp**，不能写成
                # `next_disp = now + disp_period`：当 display_fps == decode_fps
                # （低路数时的正常取值）两个节流是**同一个周期**，帧到达时刻
                # 相对阈值有毫秒级抖动，`now >= next_disp` 就会隔帧不成立 ——
                # 实测请求 25fps 只推出 15.6fps（帧被白白丢掉一半）。固定步进
                # + 追平能保证「到了显示时刻就一定推」。
                now = time.monotonic()
                jpg = None
                if disp_period <= 0 or now >= next_disp:
                    next_disp += disp_period
                    while next_disp <= now:
                        next_disp += disp_period
                    try:
                        ok, buf = cv2.imencode(
                            ".jpg", frame,
                            [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality],
                        )
                        jpg = bytes(buf) if ok else None
                    except Exception:  # noqa: BLE001
                        jpg = None
                self.set_frame(frame)
                if jpg is not None and self._publish is not None:
                    try:
                        self._publish(self.idx, jpg)
                    except Exception:  # noqa: BLE001
                        pass

                if period:
                    target += 1
                    remain = t0 + target * period - time.monotonic()
                    if remain > 0:
                        # Event.wait 而非 sleep：stop 时立刻醒
                        self._stop.wait(remain)
        finally:
            self._drop_cap(cap)
            LOG.info("mc-dec-%02d 退出（帧 %d / 重开 %d / 错误 %d）",
                     self.idx, self.frames, self.opens, len(self.errors))

    @staticmethod
    def _drop_cap(cap) -> None:
        if cap is not None:
            try:
                cap.release()
            except Exception:  # noqa: BLE001
                pass

    def _fail(self, msg: str) -> None:
        self.state = STATE_ERROR
        self.error = msg

    def _note_error(self, msg: str) -> None:
        self.errors.append(msg)
        if len(self.errors) > 50:
            del self.errors[:-50]
        self.error = msg


class MultiChannelSession:
    """1~32 路视频的并发解码会话 + VLM 轮转调度器。"""

    def __init__(
        self,
        *,
        analysis_size: int = DEFAULT_ANALYSIS_SIZE,
        decode_fps: float = DEFAULT_DECODE_FPS,
        display_fps: float = DEFAULT_DISPLAY_FPS,
        jpeg_quality: int = DEFAULT_JPEG_QUALITY,
    ) -> None:
        self._lock = threading.Lock()
        self._channels: List[Channel] = []
        self._stop_evt = threading.Event()
        self._play_evt = threading.Event()
        self._subscribers: List[queue.Queue] = []
        self._subs_lock = threading.Lock()
        self._state = STATE_IDLE
        self._error = ""
        self._loop = True
        self._detector: Any = None
        self._rr_thread: Optional[threading.Thread] = None
        self._rr_current = -1
        self._rr_lock = threading.Lock()
        self._rr_round = 0
        self._rr_infer_ms = 0.0
        self._rr_last_ts = 0.0
        # 「轮转间隔」秒/路：上一路推理完后额外等的秒数，0 = 立刻轮下一路
        self._rr_interval = 0.0
        # 累计跳过的路次（推理超时 = 这一路没等到结果，不阻塞整轮）
        self._rr_skips = 0
        self.analysis_size = analysis_size
        self.decode_fps = decode_fps
        self.display_fps = display_fps
        # open() 按实际路数算出的每路**实际** fps（快照里上报给前端）
        self._eff_decode_fps = decode_fps
        self._eff_display_fps = display_fps
        self.jpeg_quality = jpeg_quality
        self._out_w = analysis_size
        self._out_h = analysis_size
        # 送检尺寸（VLM 用），与显示尺寸解耦
        self._ana_w = analysis_size
        self._ana_h = analysis_size

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def open(self, sources: List[str], loop: bool = True) -> dict:
        if _WdnavVideoCapture is None:
            self._state = STATE_ERROR
            self._error = "wdnav.VideoCapture 不可用：原生库缺失"
            raise RuntimeError(self._error)

        cleaned = [s.strip() for s in (sources or []) if s and s.strip()]
        if not cleaned:
            raise ValueError("sources 不能为空")
        if len(cleaned) > MAX_CHANNELS:
            raise ValueError(
                f"最多 {MAX_CHANNELS} 路，收到 {len(cleaned)} 路"
            )

        self.close()

        with self._lock:
            self._state = STATE_OPENING
            self._error = ""
            self._loop = bool(loop)

        # 统一按第一路的宽高比决定尺寸，保证各路一致（尺寸不同会让 RGA
        # 每路走不同分支，反而更慢且前端 tile 尺寸不一）。
        #   _out_w/_out_h  = RGA 输出 / 浏览器显示（按路数分档）
        #   _ana_w/_ana_h  = 送检 VLM 的尺寸（恒为 analysis_size）
        sw, sh = probe_size(cleaned[0]) if "://" not in cleaned[0] else (0, 0)
        self._ana_w, self._ana_h = derive_analysis_size(
            sw, sh, self.analysis_size)
        self._out_w, self._out_h = derive_display_size(
            len(cleaned), sw, sh, self.analysis_size)
        if (self._out_w, self._out_h) != (self._ana_w, self._ana_h):
            LOG.info("mc-open: 显示 %dx%d / 送检 %dx%d（送检尺寸决定 vision 变体，"
                     "不随显示尺寸变化）", self._out_w, self._out_h,
                     self._ana_w, self._ana_h)

        self._stop_evt.clear()
        self._play_evt.set()

        # 每路 fps 按**实际路数**摊分总预算：开 1 路就跑满源帧率（流畅，
        # 不卡），开 32 路才降到预算上限。self.decode_fps / display_fps
        # 是每路**上限**，不是写死的实际值。
        n = len(cleaned)
        eff_decode = derive_decode_fps(n, self.decode_fps)
        eff_display = derive_display_fps(n, self.display_fps)

        channels = [
            Channel(i, src, self._out_w, self._out_h, bool(loop),
                    self._stop_evt, self._play_evt)
            for i, src in enumerate(cleaned)
        ]
        with self._lock:
            self._channels = channels
            self._eff_decode_fps = eff_decode
            self._eff_display_fps = eff_display
        for ch in channels:
            ch._publish = self.publish_frame
        for ch in channels:
            ch.start(eff_decode, eff_display, self.jpeg_quality)
        LOG.info("mc-open: %d 路 decode=%.1ffps display=%.1ffps "
                 "(每路上限 %.0f/%.0f, 总预算 %.0f/%.0f)",
                 n, eff_decode, eff_display, self.decode_fps,
                 self.display_fps, MAX_TOTAL_DECODE_FPS,
                 MAX_TOTAL_DISPLAY_FPS)

        with self._lock:
            self._state = STATE_PLAYING
        return self.snapshot()

    def close(self) -> dict:
        self._stop_evt.set()
        self._play_evt.set()  # 放行卡在 play wait 上的线程
        rr = self._rr_thread
        self._rr_thread = None
        if rr is not None:
            rr.join(timeout=2.0)
        channels: List[Channel] = []
        with self._lock:
            channels = self._channels
            self._channels = []
            self._state = STATE_IDLE
        for ch in channels:
            if ch.thread is not None:
                ch.thread.join(timeout=2.0)
        # 只清空每个订阅队列里**残留的旧帧**，绝不能动订阅者名单本身。
        #
        # close() 会被 open() 开头调用（换源 = 先 close 再建新通道），也会被
        # 前端 /close 调用。如果这里 _drain_subs() 清名单，那么任何**已经连上**
        # 的 WebSocket（stream_ws 里持着 sub_q 一直 poll）会被永久摘出广播
        # 列表：session.snapshot() 照常推，所以前端 ~1Hz 的 fps 标签还在跳，
        # 但 publish_frame 遍历不到它的队列 —— 表现就是「宫格有帧率标签、
        # 就是没有画面」，且硬刷新无效（重开又孤儿一次）。
        # 名单只在会话对象真正被丢弃时清，见 reset_multi_session()。
        with self._subs_lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass
        return self.snapshot()

    def pause(self) -> dict:
        with self._lock:
            if self._channels and self._state == STATE_PLAYING:
                self._play_evt.clear()
                self._state = STATE_PAUSED
        return self.snapshot()

    def play(self) -> dict:
        with self._lock:
            if self._channels:
                self._play_evt.set()
                self._state = STATE_PLAYING
        return self.snapshot()

    # ------------------------------------------------------------------ #
    # WS 订阅
    # ------------------------------------------------------------------ #
    def subscribe(self) -> "queue.Queue":
        q: "queue.Queue" = queue.Queue(maxsize=MAX_CHANNELS * 2)
        with self._subs_lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: "queue.Queue") -> None:
        with self._subs_lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def _drain_subs(self) -> None:
        with self._subs_lock:
            self._subscribers.clear()

    def publish_frame(self, idx: int, jpg: bytes) -> None:
        """由解码线程调用，把 (idx, jpg) 广播给所有订阅者（drop-oldest）。"""
        item = (idx, jpg)
        with self._subs_lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(item)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(item)
                except queue.Full:
                    pass

    # ------------------------------------------------------------------ #
    # VLM 轮转调度
    # ------------------------------------------------------------------ #
    def attach_detector(self, detector) -> None:
        """把 WdnaiDetector 接上，轮转线程随即按 ch0..chN-1 依次提交。"""
        with self._lock:
            self._detector = detector

    def start_round_robin(self) -> None:
        """起轮转线程，至多一个在跑。无条件新建并先换掉引用，旧线程靠
        "自己不再是 _rr_thread" 自查退出 —— 若改用"旧线程还活着就
        return"+stop 置 None，stop→start 后旧线程的退出判据永远不成立，
        两个线程一起转（详见 AGENTS.md）。"""
        with self._rr_lock:
            t = threading.Thread(
                target=self._rr_loop, name="mc-vlm-rr", daemon=True)
            self._rr_thread = t
            t.start()

    def set_round_robin_interval(self, seconds: float) -> float:
        """设置「轮转间隔」秒/路。返回实际生效值。

        轮转线程每轮开始时读一次，所以运行中改也即时生效，不必重启检测。
        """
        try:
            v = max(0.0, min(300.0, float(seconds)))
        except (TypeError, ValueError):
            v = 0.0
        with self._lock:
            self._rr_interval = v
        return v

    def stop_round_robin(self) -> None:
        with self._rr_lock:
            self._rr_thread = None
        with self._lock:
            self._rr_current = -1

    def _rr_loop(self) -> None:
        """VLM 轮转调度：**一路一帧，串行，严格第 1 路 → 第 N 路 → 回第 1 路**。

        语义（就是「逐路巡检」，不是多路同步）：

        * N 路视频各自**独立地持续解码**，播放不受推理影响 —— 解码线程
          只往 ``latest_bgr`` 写，推理读的是快照，两者互不阻塞。
        * 轮到第 i 路时，取它**此刻最新的一帧**提交给 detector；
          等这一路推理完成后，再取第 i+1 路的当前帧。
        * 第 N 路完成 -> 回到第 1 路开新一轮。每轮都从第 1 路开始、
          顺序固定，便于对照画面排查。

        **必须串行**：``detector._item_q`` 是 maxsize=1 的 drop-oldest
        队列，N 路同时挤只有最后进去的那路能跑，其余静默丢弃。所以靠
        ``detector.wait_idle()`` 做背压 —— 同一模型句柄不可重入。

        一轮 = N 次推理。6 路 x ~1.9s ≈ 11s/轮 —— 这是单模型句柄串行的
        固有代价，不是缺陷。想拉长观察间隔用「轮转间隔」秒/路。
        """
        while not self._stop_evt.is_set():
            with self._rr_lock:
                # 用**线程身份**判定退出，而不是「引用是不是 None」。
                # 被 stop 摘掉、或被新一轮 start 取代的线程都会在这里
                # 自行退出，保证任何时刻只有一个轮转线程在提交。
                if self._rr_thread is not threading.current_thread():
                    return
            with self._lock:
                channels = list(self._channels)
                detector = self._detector
                interval = self._rr_interval
                ana_w, ana_h = self._ana_w, self._ana_h
            if not channels or detector is None:
                self._stop_evt.wait(0.2)
                continue

            analyzed = 0
            spin = 0
            # 一轮固定从第 1 路（index 0）开始，顺序 0 -> 1 -> ... -> N-1
            for ch in channels:
                if self._stop_evt.is_set():
                    return
                # 每路都要复查身份：判据只放 while 顶部的话，被取代的
                # 旧线程要跑完整轮才退出，期间两线程同时提交、顺序乱掉。
                with self._rr_lock:
                    if self._rr_thread is not threading.current_thread():
                        return
                # 此刻该路的最新帧，按送检尺寸缩好（显示尺寸可能更大，
                # 见 derive_display_size）。没出帧 / 缩放失败就跳过本路。
                frame = ch.snapshot_bgr_for_analysis(ana_w, ana_h)
                if frame is None:
                    continue
                with self._lock:
                    self._rr_current = ch.idx
                t0 = time.perf_counter()
                try:
                    detector.submit_frame(frame, ch.frame_pos, channel=ch.idx)
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("mc-vlm-rr ch%02d submit 失败: %s",
                                ch.idx, exc)
                    continue
                # 等这一路推理完再轮下一路。单路卡死时不等满上限：
                # 超时就跳过继续轮，否则一路挂住会把整个轮转拖死。
                try:
                    done = detector.wait_idle(timeout=RR_INFER_TIMEOUT)
                except Exception:  # noqa: BLE001
                    done = False
                dt = (time.perf_counter() - t0) * 1000.0
                with self._lock:
                    self._rr_infer_ms = dt
                    self._rr_last_ts = time.time()
                if done and dt >= RR_MIN_INFER_MS:
                    analyzed += 1
                    spin = 0
                elif done:
                    # dt 接近 0 = 这帧根本没进队列（submit_frame 在门槛
                    # 上直接 return，_pending 都不设，wait_idle 秒回 True）。
                    # 不识别就会 15 次/秒空转：烧核、一帧没推理，round 却
                    # 照涨，看着像"在正常工作"。
                    spin += 1
                    if spin in (10, 60, 300):
                        LOG.warning(
                            "mc-vlm-rr 连续 %d 次提交被丢弃（detector 未启用？"
                            "），已降速", spin)
                    if spin >= 10:
                        self._stop_evt.wait(min(1.0, 0.05 * spin))
                else:
                    spin = 0
                    with self._lock:
                        self._rr_skips += 1
                    LOG.warning("mc-vlm-rr ch%02d 推理超时 %.1fs，跳过",
                                ch.idx, RR_INFER_TIMEOUT)
                # 「轮转间隔」：上一路结束后额外等一会儿再轮下一路
                if interval > 0:
                    self._stop_evt.wait(interval)

            # 一轮跑完才计数（上一版在第一次 submit 后就 +1，轮数会虚高）
            with self._lock:
                if analyzed:
                    self._rr_round += 1
                self._rr_current = -1
            if not analyzed:
                self._stop_evt.wait(0.3)

    def vlm_snapshot(self) -> dict:
        with self._lock:
            return {
                "attached": self._detector is not None,
                "running": bool(
                    self._rr_thread is not None and self._rr_thread.is_alive()
                ),
                "current": self._rr_current,
                "round": self._rr_round,
                "interval": self._rr_interval,
                "skips": self._rr_skips,
                "last_infer_ms": round(self._rr_infer_ms, 1),
                "last_ts": self._rr_last_ts,
            }

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    def channels_snapshot(self) -> List[dict]:
        with self._lock:
            return [c.snapshot() for c in self._channels]

    def snapshot(self) -> dict:
        with self._lock:
            state = self._state
            err = self._error
            loop = self._loop
            n = len(self._channels)
        chans = self.channels_snapshot()
        total_frames = sum(c["frames"] for c in chans)
        started = min((c["last_frame_ts"] for c in chans if c["frames"]),
                      default=0.0)
        return {
            "state": state,
            "error": err,
            "channels": n,
            "loop": loop,
            "out_w": self._out_w,
            "out_h": self._out_h,
            "analysis_size": self.analysis_size,
            "ana_w": self._ana_w,
            "ana_h": self._ana_h,
            "decode_fps_target": self._eff_decode_fps,
            "display_fps_target": self._eff_display_fps,
            "decode_fps_cap": self.decode_fps,
            "total_frames": total_frames,
            "total_errors": sum(c["errors"] for c in chans),
            "last_frame_ts": started,
            "vlm": self.vlm_snapshot(),
            "channel_list": chans,
        }


_session: Optional[MultiChannelSession] = None
_session_lock = threading.Lock()


def get_multi_session() -> MultiChannelSession:
    """本页多路会话单例（与 ``app.core.video.session`` 的单例互不相干）。"""
    global _session
    with _session_lock:
        if _session is None:
            _session = MultiChannelSession()
        return _session


def reset_multi_session() -> None:
    """测试用：丢弃单例。"""
    global _session
    with _session_lock:
        if _session is not None:
            _session.close()
            # 会话对象即将被丢弃，此时才允许清订阅名单（close() 不能清，
            # 否则 open() 复用单例时会摘掉已连接的 WebSocket —— 见 close 注释）
            _session._drain_subs()
        _session = None