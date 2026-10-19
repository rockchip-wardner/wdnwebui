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

    N 个解码线程   各自 1 个 VideoCapture，按每路自适应 decode_fps 节流
                   read0_multi([(显示尺寸)])                → 绝大多数帧
                   read0_multi([(显示尺寸), (送检尺寸)])    → 轮转点到的那帧
                   → 按 display_fps 编码 JPEG 送显
    1 个 VLM 调度   严格轮转 ch0→ch1→…→chN-1，向该路 _ana_due 要图、
                   等它交接，再提交下一路
    N 个 WS 订阅    每个订阅者拿 (channel_idx, jpeg_bytes)，drop-oldest

热路径与冷路径**不共用同一张图**
--------------------------------
显示（热路径）与送检（冷路径）是两件独立的事，各自在**同一次解码**的
DMA-BUF 上单独 RGA 到自己的尺寸。早前它们共用一张（``_out = max(显示,
送检)``），于是送检尺寸绑架了显示尺寸：为了 640 的送检图，浏览器也只能
看 640，32 路被拖到 609.5 帧/秒。解耦后热路径回到 384、32 路 769.9
帧/秒，而送检照样拿真 640。

代价是**第二次 RGA 只在被轮转点到的那一帧上发生**（约 2%），不是每帧
都付双倍 —— 所以送检图必须由解码线程产出：轮转线程自己去调 ``read0``
会与该路解码线程抢同一个 ``VideoCapture``、打乱解码顺序。

三个参数为什么是这个默认值（全部来自本机实测，见 AGENTS.md）

送检尺寸 —— **不由参数决定，按源尺寸算**（:func:`derive_inference_size`），
与单路 ``VideoSession`` 同口径：720p/1080p → 640、4K → 960。早前写死 384
等于让所有素材掉进便宜的 384 vision 变体，那是**精度降档**而非吞吐优化。
``analysis_size`` 只剩「强制覆盖」这一个用途，默认 None = 自动。

显示尺寸 —— **纯显示需求**，按路数分档（:func:`derive_display_size`）：
1~4 路 960 长边铺满播放器，5 路以上 384。

``decode_fps`` / ``display_fps`` —— 都只是**每路上限**，实际值由
:func:`derive_decode_fps` / :func:`derive_display_fps` 按路数摊分，N 路共享
``MAX_TOTAL_DECODE_FPS`` / ``MAX_TOTAL_DISPLAY_FPS``（都是 384 热路径口径）。

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
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from wdnav import VideoCapture as _WdnavVideoCapture
except Exception:  # pragma: no cover - platform-specific load failure
    _WdnavVideoCapture = None

# 帧判据：每路一个独立实例 —— 各摄像头画面不同，共用一个参照帧没有意义。
from app.core.video.frame_judge import (
    DEFAULT_EVERY_N,
    DEFAULT_LONG_SIDE,
    EXPOSED_METRICS,
    FrameJudge,
)


LOG = logging.getLogger("multi_channel_session")

MAX_CHANNELS = 32
# 每路 fps 的**上限**（不是实际值）。实际值按路数在总预算里摊分，
# 见 derive_decode_fps / derive_display_fps。25 是常见监控源帧率。
DEFAULT_DECODE_FPS = 25.0
DEFAULT_DISPLAY_FPS = 25.0
DEFAULT_JPEG_QUALITY = 70
OPEN_TIMEOUT = 10.0
# 所有路**合计**的解码预算（次/秒）= 热路径「解码 + RGA 到显示尺寸 + JPEG」。
# 多路时显示 384（见 derive_display_size），冷热各自 RGA，所以这里只看显示档
# 的单帧成本：720P 源 384x216 实测 1.94ms/帧。
#   * 32 路 → 每路 ~24fps（实测 769.9 帧/秒、0 错误）
#   * 25 路以内每路仍跑满 25fps，不受此预算影响
# 别把值调到 800 以上：调度器会给每路 25fps 的节流目标而硬件给不出来，
# 每帧都「追不上节流」退化成满速空跑，fps 会乱跳（实测 15.4~21.5）。
MAX_TOTAL_DECODE_FPS = 780.0
# 所有路合计的 UI 推送预算（张/秒）。384x216 JPEG 实测 0.62ms/张，780 张
# ≈ 0.48 核，留足余量给 WS 发送与浏览器解码。解码预算是硬顶（769.9），
# 这个值取到与之相当即可，再大也没有更多帧可推。
MAX_TOTAL_DISPLAY_FPS = 780.0
# 单路推理等待上限。正常单路 ~0.85~1.9s，30s 足够宽裕；超时的路直接跳过
# 继续轮下一路，不让一路卡死把整个轮转拖住。
RR_INFER_TIMEOUT = 30.0
# 轮转线程向解码线程要送检图时的等待上限（秒）。解码线程在下一帧上就会
# 多跑一次 RGA 产出来，正常是一帧周期（1/display_fps ≈ 40ms）内；留 1s
# 余量给 decode_fps 被预算摊到很低的路数。
ANA_WAIT_TIMEOUT = 1.0
# 一次「已完成的推理」耗时下限（毫秒）。正常单路 850~1900ms，远大于此；
# 若 wait_idle 亚毫秒返回，说明这一帧压根没进队列（见 _rr_loop 的退避分支）。
RR_MIN_INFER_MS = 100.0
# feeder 等自己 slot 空闲的最长阻塞。多卡下这是"排队"不是"推理"，给短一点：
# 等太久会让所有 feeder 一起睡着，退化成串行。
RR_SLOT_IDLE_WAIT = 0.5
# 连续多少次「帧没进队列」后判定为异常空转并退避。单次亚毫秒返回可能是正常
# 的（队列正好满），连续十几次说明 feeder 在对着没人消费的队列空转。
RR_SPIN_STREAK_LIMIT = 10
RR_SPIN_BACKOFF = 0.5
# 本次既没推理也没空转（判据挡下 / 没拿到送检帧）时的退避。
RR_NO_PROGRESS_WAIT = 0.3

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


def derive_inference_size(src_w: int, src_h: int,
                           thumb_long_side: int = DEFAULT_LONG_SIDE,
                           ) -> Tuple[int, int]:
    """送检 VLM 的尺寸，与单路 ``VideoSession`` 的 buf1 逐字同口径。

    公式直接照抄 ``session.py`` 的
    ``mid_long = max(thumb_long_side * 4, max(src_w, src_h) // 4)``
    （再对齐 16 字节、上限不放大）—— 两套口径会长出不一致的行为，而
    wdnai 是按**输入图最长边**挑 vision 变体的，多路送小一档就是白降精度。

    720p / 1080p → 640 长边；4K → 960；8K → 1280。源更小时收敛到源尺寸。
    """
    if src_w <= 0 or src_h <= 0:
        return thumb_long_side * 4, thumb_long_side * 4
    src_long = max(src_w, src_h)
    mid = max(thumb_long_side * 4, src_long // 4)
    mid = min(mid, src_long)
    mid = (mid + 15) & ~15
    if src_w >= src_h:
        return mid, max(2, (src_h * mid) // src_w)
    return max(2, (src_w * mid) // src_h), mid


# 浏览器显示用的最长边，按路数分档（纯显示需求，不受送检尺寸影响）。
#   1~4 路 → 960（实测单路 24.9fps / 0 错误，铺满播放器不糊）
#   5 路以上 → 384（省 RGA 像素与 JPEG 体积，见 MAX_TOTAL_DECODE_FPS）
DISPLAY_LONG_EDGE_HIGH = 960
DISPLAY_LONG_EDGE_LOW = 384
DISPLAY_HIGH_MAX_CHANNELS = 4
# 放大态那一路的显示最长边 —— 720p。只加在**被放大的一路**：8.9 倍像素
# 全员升档会把 RGA + JPEG 预算吃穿。
DISPLAY_LONG_EDGE_ZOOM = 1280


def derive_display_size(n_channels: int, src_w: int, src_h: int,
                        ) -> Tuple[int, int]:
    """返回给**浏览器**的显示尺寸 —— 纯显示需求，与送检尺寸无关。

    分界线沿用 ``DISPLAY_HIGH_MAX_CHANNELS = 4``（不是拍的新数）：1~4 路
    大图时前端才有「疯狂屏闪」问题，改成别的值会把那个 bug 的触发条件挪走。
    """
    long_edge = (DISPLAY_LONG_EDGE_HIGH
                 if n_channels <= DISPLAY_HIGH_MAX_CHANNELS
                 else DISPLAY_LONG_EDGE_LOW)
    return derive_analysis_size(src_w, src_h, long_edge)


def derive_decode_fps(n_channels: int, cap: float = DEFAULT_DECODE_FPS) -> float:
    """按路数摊分**总解码预算**，返回每路的解码帧率。

    之前这里对所有路数一律返回 5.0，那是拿 32 路极限场景的保守值一刀切：
    开 1 路视频也只有 5fps，播放明显卡顿。正确做法是「N 路共享一个总预算」，
    路数少时每路跑满源帧率，路数多时才降帧 —— 降帧的代价是所有路一起变卡，
    而热路径是**所有路合计**受限的，不该让单路替多路背锅。

    实测（720P 源 → 640x360）：32 路合计 659.6 帧/秒、**0 错误**，所以预算取
    660；N <= 25 时每路都能跑满 cap，超过才开始摊分降帧。
    """
    n = max(1, int(n_channels))
    budget = MAX_TOTAL_DECODE_FPS / n
    return max(1.0, min(float(cap), budget))


def derive_display_fps(n_channels: int, cap: float = DEFAULT_DISPLAY_FPS) -> float:
    """按路数摊分**总推送预算**，返回每路的 UI 显示帧率。

    与解码同理：单张 640x360 JPEG 只要 2.2ms，1~6 路时完全负担得起
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
    """一路视频 = 1 个解码线程 + 显示帧槽 + 送检帧槽。"""

    __slots__ = (
        "idx", "source", "state", "error", "width", "height", "fps",
        "frame_pos", "loop", "frames", "opens", "errors", "thread",
        "started_at", "last_frame_ts", "_lock", "_latest_bgr",
        "_disp_w", "_disp_h", "_stop", "_play", "_live", "_publish",
        "judge", "_ana_w", "_ana_h", "_ana_due", "_ana_bgr",
        "_ana_seq", "_ana_seen",
        # 解码→广播那一段的三个计数器（判「解码出来却没推出去」丢在哪）：
        # _disp_skip 显示节流没到点而故意不推 / _disp_err JPEG 编码失败 /
        # _pub_exc publish_frame 抛异常。decode_fps == display_fps 时都应是 0。
        "_disp_skip", "_disp_err", "_pub_exc",
        # 判据归谁打分：False = 向导模式（解码线程按 sample_every 采样打分，
        # 只为让浮框/曲线动起来），True = 应用页面（轮转线程在送检时打分，
        # 参照帧 = 上一次送检的帧）。由 session 的 _set_judge_owner 切换，
        # 切换时必须丢掉参照帧 —— 两种模式的参照帧语义不同。
        "rr_owns_judge",
    )

    def __init__(
        self,
        idx: int,
        source: str,
        disp_w: int,
        disp_h: int,
        ana_w: int,
        ana_h: int,
        loop: bool,
        stop_evt: threading.Event,
        play_evt: threading.Event,
    ) -> None:
        self.idx = idx
        self.source = source
        self._disp_w = disp_w
        self._disp_h = disp_h
        self._ana_w = ana_w
        self._ana_h = ana_h
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
        # 送检帧交接：轮转线程置 _ana_due，解码线程在**同一帧**上多出一份
        # 送检尺寸的 BGR。序号判新旧，见 request_inference。
        self._ana_due = threading.Event()
        self._ana_bgr: Any = None
        self._ana_seq = 0        # 解码线程每产出一张送检图 +1
        self._ana_seen = 0       # 轮转线程已消费到的序号
        # 解码→广播的三个计数器，见 __slots__ 处的说明
        self._disp_skip = 0
        self._disp_err = 0
        self._pub_exc = 0
        # 判据归属：默认向导模式（解码线程采样打分）
        self.rr_owns_judge = False
        self._stop = stop_evt
        self._play = play_evt
        # 帧广播回调（由 MultiChannelSession 注入），把 JPEG 推给 WS 订阅者
        self._publish: Optional[Callable[[int, bytes], None]] = None
        # 本路专属的帧判据器。由 MultiChannelSession.open() 按配置注入 ——
        # 每路独立，因为参照帧是"上一张**本路**送去推理的图"。
        self.judge: Optional[FrameJudge] = None

    # -- 采样侧 ------------------------------------------------------------ #
    def snapshot_bgr(self):
        """原子取最近一帧 BGR（**显示尺寸**）。不触发 RGA —— 解码时已转好。"""
        with self._lock:
            return self._latest_bgr

    def request_inference(self) -> None:
        """请求本路下一次解码时额外产出一张送检尺寸的图（轮转线程调用）。

        解码线程在同一帧上多跑一次 RGA，代价只落在被点到的那一帧上（约
        2%），不是每帧双倍。**必须由解码线程产出**：轮转线程自己去调
        read0 会与该路解码线程抢同一个 VideoCapture、打乱解码顺序。
        """
        self._ana_due.set()

    def take_inference(self, timeout: float = 1.0):
        """等解码线程交出送检图；没有就返回 None（让本轮跳过）。

        ``timeout`` 要宽于一个解码帧周期（1000/display_fps），否则轮转会
        频繁空手而归 —— 那正是 ``RR_MIN_INFER_MS`` 要抓的「空转」。
        """
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                if self._ana_seq > self._ana_seen:
                    self._ana_seen = self._ana_seq
                    return self._ana_bgr
            if self._stop.is_set() or time.monotonic() >= deadline:
                return None
            time.sleep(0.005)

    def _publish_inference(self, img) -> None:
        with self._lock:
            self._ana_bgr = img
            self._ana_seq += 1

    def snapshot_gray_for_judge(self, long_side: int):
        """取当前帧 → RGA 缩到长边 → 转灰度，给本路判据器。失败返回 None。

        源是**显示尺寸**的帧。缩放走 ``wdnav.rga.resize_bgr``，**不做软
        resize 兜底**；灰度用 ``cv2.cvtColor(BGR2GRAY)``，与单路 ``VideoSession``
        的 buf2 一致（**不要**改用 ``frame[..., 0]``，那是 B 通道不是 Y，
        分数刻度会偏）。

        调用频率由 :meth:`FrameJudge.should_sample` 决定（默认每
        :data:`~app.core.video.frame_judge.DEFAULT_SAMPLE_EVERY` 帧一次），
        且调用方必须在**动手缩之前**先问 —— 这里每次调用都是一次 RGA，而 RGA
        是全机串行队列，32 路逐帧调会让总吞吐掉到 1/3。
        """
        frame = self.snapshot_bgr()
        if frame is None:
            return None
        fh, fw = frame.shape[0], frame.shape[1]
        tw, th = derive_analysis_size(fw, fh, long_side)
        try:
            from wdnav.rga import resize_bgr
            small = resize_bgr(frame, tw, th) if (fw, fh) != (tw, th) else frame
            import cv2
            return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) \
                if small.ndim == 3 else small
        except Exception as exc:  # noqa: BLE001
            LOG.warning("mc-ch%02d 判据缩略图 %dx%d 失败: %s",
                        self.idx, tw, th, exc)
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
            "disp_skip": self._disp_skip,
            "disp_err": self._disp_err,
            "pub_exc": self._pub_exc,
            "last_frame_ts": self.last_frame_ts,
            "disp_w": self._disp_w,
            "disp_h": self._disp_h,
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
        # 显示节流按**帧数**，见解码循环里那段注释（时间节流在这里是错的工具）。
        # 每 N 帧发一次 = 每秒发 decode_fps/N ≈ display_fps。
        disp_every = (max(1, int(round(decode_fps / display_fps)))
                      if (display_fps and display_fps > 0) else 1)
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
                    # 显示节流已改成按帧数，重开解码器时不需要重置任何显示状态

                # 绝大多数帧只出显示尺寸；轮转要图的那一帧才多跑一次 RGA
                want_ana = self._ana_due.is_set()
                sizes = ([(self._disp_w, self._disp_h),
                          (self._ana_w, self._ana_h)] if want_ana
                         else [(self._disp_w, self._disp_h)])
                try:
                    ret, frame0 = cap.read0_multi(sizes)
                except Exception as exc:  # noqa: BLE001
                    self._note_error(f"read0_multi 异常: {exc}")
                    self._drop_cap(cap)
                    cap = None
                    self._stop.wait(0.2)
                    continue

                if not ret or frame0 is None:
                    # EOF —— 循环重开，或停在 eof 等前端决定
                    self._drop_cap(cap)
                    cap = None
                    self._ana_due.clear()
                    with self._lock:
                        self.frame_pos = 0
                    if self.loop and not self._live:
                        self.state = STATE_PLAYING
                        self._stop.wait(0.05)
                        continue
                    self.state = STATE_EOF
                    return

                outs = getattr(frame0, "_multi_bgr", None)
                if not outs:
                    # RGA 失败 —— 丢这一帧，不打断整路
                    continue
                frame = outs[0]
                if want_ana and len(outs) > 1:
                    self._publish_inference(outs[1])
                    self._ana_due.clear()

                self.frames += 1
                self.last_frame_ts = time.time()
                try:
                    self.frame_pos = int(cap.frame_pos)
                except Exception:  # noqa: BLE001
                    pass

                # 帧判据 —— **向导模式专用**（未启动检测），只为让浮框/曲线
                # 实时刷新。应用页面（rr_owns_judge=True）改由轮转线程在送检
                # 那一刻打分，参照帧语义不同，见 _run_one_channel。
                #
                # 采样门控必须问在 RGA 之前：缩略图是 RGA 出来的，而 RGA 是全机
                # 串行队列（每帧多打一次，32 路 RGA 从 1.000 涨到 2.000 次/帧，
                # 吞吐掉 62%）。所以下面三行的顺序不能调换。rr_owns_judge 也
                # 要一起判，否则两个线程会让参照帧在两种语义之间来回跳。
                if (self.judge is not None and self.judge.is_auto
                        and not self.rr_owns_judge
                        and self.judge.should_sample(self.frames)):
                    thumb = self.snapshot_gray_for_judge(self.judge.long_side)
                    if thumb is not None:
                        try:
                            self.judge.observe(thumb, self.frames)
                        except Exception as exc:  # noqa: BLE001
                            # 与 _rr_loop 同样"宁可多送不可漏检"：打分失败时
                            # 不影响解码主循环，下一帧重试。
                            LOG.warning("mc-ch%02d 解码侧判据异常，下帧重试: %s",
                                        self.idx, exc)

                # 显示节流：**按帧数**（`self.frames % disp_every`），不是按时间。
                #
                # 时间节流在这里是错的工具：解码循环每轮的检查时刻是
                # `t0 + k*period + body`（body = 那一轮的活干完的时刻），
                # 两条链只差一个 body 偏移，相位永远对不齐。滑动阈值在
                # body 一抖动时就隔帧不成立（实测请求 25fps 只推出 15.6fps）；
                # 固定步长从 t0 推则更糟 —— body < period 时**每一帧**都落在
                # 「下一个理想槽点之前」被全部跳掉（32 路实测 _disp_skip =
                # 248 帧/秒，占解码 34%，白丢的帧下游补不回来）。
                #
                # 按帧数没有相位问题：解码被 throttle 限死在 decode_fps，
                # 「每 N 帧发一次」天然就是 display_fps，且不会超发。本仓库
                # derive_decode_fps / derive_display_fps 共用一个预算 780，
                # 所以 disp_every 恒为 1，这个节流器实际从不丢帧。
                now = time.monotonic()
                jpg = None
                if disp_every <= 1 or self.frames % disp_every == 0:
                    try:
                        ok, buf = cv2.imencode(
                            ".jpg", frame,
                            [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality],
                        )
                        jpg = bytes(buf) if ok else None
                        if not ok:
                            self._disp_err += 1
                    except Exception:  # noqa: BLE001
                        jpg = None
                        self._disp_err += 1
                else:
                    # 故意不推这一帧。与 imencode 失败分开计数：那个是错误，
                    # 这个是帧率整形；32 路下 decode_fps == display_fps 时不该发生。
                    self._disp_skip += 1
                self.set_frame(frame)
                if jpg is not None and self._publish is not None:
                    try:
                        self._publish(self.idx, jpg)
                    except Exception:  # noqa: BLE001
                        self._pub_exc += 1

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
        analysis_size: Optional[int] = None,
        decode_fps: float = DEFAULT_DECODE_FPS,
        display_fps: float = DEFAULT_DISPLAY_FPS,
        jpeg_quality: int = DEFAULT_JPEG_QUALITY,
    ) -> None:
        self._lock = threading.Lock()
        self._channels: List[Channel] = []
        self._stop_evt = threading.Event()
        self._play_evt = threading.Event()
        self._subscribers: List[queue.Queue] = []
        # 订阅名单的**不可变快照**。publish_frame 是被 32 个解码线程各调一次
        # 的热路径，原来每帧 list(self._subscribers) 要加锁 + 拷列表，还要和
        # subscribe/unsubscribe 抢同一把锁。名单只在连接增减时变动，维护一份
        # 快照即可 —— publish 只读引用，**不拿锁**。
        self._subs_snapshot: Tuple["queue.Queue", ...] = ()
        self._subs_lock = threading.Lock()
        # 广播侧计数。**按通道分槽**而不是一个全局 int：`self._pub_frames += 1`
        # 在 32 个解码线程上**不是原子的**（CPython 的 `+=` 是 LOAD/ADD/STORE
        # 三条字节码），实测 770 次/秒下会丢 7~15% 的更新。每路的解码线程是
        # 该槽位的**唯一写者**，所以分槽即无竞争，汇总时再相加。dropped 是
        # 稀疏事件，保持全局计即可。
        self._pub_per_ch: List[int] = [0] * MAX_CHANNELS
        self._pub_drops = 0
        self._state = STATE_IDLE
        self._error = ""
        self._loop = True
        self._zoomed: Optional[int] = None
        self._detector: Any = None
        self._rr_threads: List[threading.Thread] = []
        # 轮转代号：start / stop 都自增，拿着旧代号的 feeder 自行退出
        self._rr_gen = 0
        # 多卡共享的取路状态。取路与占用必须同一把锁，否则两个 feeder 会拿到
        # 同一路、同一帧被两个模型同时推理。
        self._ch_lock = threading.Lock()
        self._ch_cursor = 0
        self._ch_inflight: set = set()
        self._rr_current = -1
        self._rr_lock = threading.Lock()
        self._rr_round = 0
        self._rr_infer_ms = 0.0
        self._rr_last_ts = 0.0
        # 「轮转间隔」秒/路：上一路推理完后额外等的秒数，0 = 立刻轮下一路
        self._rr_interval = 0.0
        # 累计跳过的路次（推理超时 = 这一路没等到结果，不阻塞整轮）
        self._rr_skips = 0
        # 「帧压根没进队列」的累计空转次数（已被退避挡下，不会以每秒几千次
        # 的速率刷）。与 _rr_skips 分开：那个是"等满推理超时才跳过"。
        self._rr_spins = 0
        # 累计被帧判据挡下的路次（判为"这一帧不值得送检"）。与 _rr_skips 分开
        # 计数：前者是判据在省算力（好事），后者是推理卡住了（坏事）。
        self._rr_judge_skips = 0
        # 判据配置。持有一个活的 FrameJudge 当配置本：mode/every_n/threshold
        # 的归一化（迁移表、模式换了跟着换默认阈值）全部复用它自己的实现，
        # 不在这里重写一遍。默认与单路 VideoSession 一致（gscore）。
        self._judge_proto = FrameJudge(mode=EXPOSED_METRICS[0],
                                       every_n=DEFAULT_EVERY_N,
                                       long_side=DEFAULT_LONG_SIDE)
        self.analysis_size = analysis_size
        self.decode_fps = decode_fps
        self.display_fps = display_fps
        # open() 按实际路数算出的每路**实际** fps（快照里上报给前端）
        self._eff_decode_fps = decode_fps
        self._eff_display_fps = display_fps
        self.jpeg_quality = jpeg_quality
        # open() 之前的占位初值，真正取值由 open() 按源尺寸算出
        self._disp_w = DEFAULT_LONG_SIDE * 4
        self._disp_h = DEFAULT_LONG_SIDE * 4
        self._ana_w = DEFAULT_LONG_SIDE * 4
        self._ana_h = DEFAULT_LONG_SIDE * 4

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
        #   _disp_w/_disp_h = 浏览器显示尺寸（按路数分档，纯显示需求）
        #   _ana_w/_ana_h   = 送检 VLM 的尺寸（与单路 VideoSession 同公式）
        # 两者**互不影响**：各自在同一次解码上单独 RGA，不再取 max 共用
        # 一张图。原先共用时送检尺寸会绑架显示（640 档下 32 路只能跑
        # 20.6fps），解耦后热路径 384 实测 32 路 769.9 帧/秒。
        sw, sh = probe_size(cleaned[0]) if "://" not in cleaned[0] else (0, 0)
        if self.analysis_size:
            self._ana_w, self._ana_h = derive_analysis_size(
                sw, sh, self.analysis_size)
        else:
            self._ana_w, self._ana_h = derive_inference_size(sw, sh)
        self._disp_w, self._disp_h = derive_display_size(len(cleaned), sw, sh)
        LOG.info("mc-open: 显示 %dx%d / 送检 %dx%d",
                 self._disp_w, self._disp_h, self._ana_w, self._ana_h)

        self._stop_evt.clear()
        self._play_evt.set()

        # 每路 fps 按**实际路数**摊分总预算：开 1 路就跑满源帧率（流畅，
        # 不卡），开 32 路才降到预算上限。self.decode_fps / display_fps
        # 是每路**上限**，不是写死的实际值。
        n = len(cleaned)
        eff_decode = derive_decode_fps(n, self.decode_fps)
        eff_display = derive_display_fps(n, self.display_fps)

        channels = [
            Channel(i, src, self._disp_w, self._disp_h,
                    self._ana_w, self._ana_h, bool(loop),
                    self._stop_evt, self._play_evt)
            for i, src in enumerate(cleaned)
        ]
        # 换源 = 新的通道表，放大态不再指向有意义的通道 → 回全屏。
        self._zoomed = None
        # 每路一个独立判据器（参照帧必须是本路的图）。重建通道 = 重建判据：
        # 旧参照帧是旧视频源的内容，换源后继续用等于拿 A 源的图判 B 源。
        proto = self._judge_proto
        for ch in channels:
            ch.judge = FrameJudge(mode=proto.mode, every_n=proto.every_n,
                                  threshold=proto.threshold,
                                  long_side=proto.long_side,
                                  sample_every=proto.sample_every)
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
        with self._rr_lock:
            self._rr_gen += 1
            rrs = list(self._rr_threads)
            self._rr_threads = []
        for rr in rrs:
            rr.join(timeout=2.0)
        channels: List[Channel] = []
        with self._lock:
            channels = self._channels
            self._channels = []
            self._state = STATE_IDLE
            # 快照描述的是本次会话，清零后前端不会读到上一轮的残留进度。
            # open() 开头会调 close()，所以新一轮的计数天然从 0 起。
            self._rr_round = 0
            self._rr_skips = 0
            self._rr_spins = 0
            self._rr_judge_skips = 0
            self._rr_current = -1
            self._rr_infer_ms = 0.0
            self._rr_last_ts = 0.0
            # 通道表已清空，_zoomed 留着就是指向不存在通道的悬空下标 ——
            # 前端读到会朝幽灵值发还原请求。
            self._zoomed = None
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
            self._subs_snapshot = tuple(self._subscribers)
        return q

    def unsubscribe(self, q: "queue.Queue") -> None:
        with self._subs_lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass
            self._subs_snapshot = tuple(self._subscribers)

    def _drain_subs(self) -> None:
        with self._subs_lock:
            self._subscribers.clear()
            self._subs_snapshot = ()

    def publish_frame(self, idx: int, jpg: bytes) -> None:
        """由解码线程调用，把 (idx, jpg) 广播给所有订阅者（drop-oldest）。

        订阅名单读 `_subs_snapshot`（一次属性读，CPython 下是原子的），**不加
        锁** —— 见 __init__ 里为什么维护快照。队列满时丢最旧的一帧：实时流
        宁可少一帧也不能攒成延迟。

        `_pub_drops` 是「解码到了但没能推出去」的帧数，与浏览器侧的
        `busyDrop`（收到了但没画）配对，两个数一比就知道丢在哪一段。
        """
        subs = self._subs_snapshot
        if not subs:
            return
        item = (idx, jpg)
        drops = 0
        for q in subs:
            try:
                q.put_nowait(item)
            except queue.Full:
                # 正常路径就是**这里**：队列满 = 订阅者跟不上。必须在这里计数 ——
                # 早先只在「丢完还塞不进去」的二次失败上计数，于是 32 路下明明
                # 每秒丢掉一两百帧，pub_drops 却一直是 0。
                drops += 1
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(item)
                except queue.Full:
                    pass
        if 0 <= idx < MAX_CHANNELS:
            # 唯一写者 → 天然无竞争（见 __init__ 里的注释）
            self._pub_per_ch[idx] += 1
        if drops:
            self._pub_drops += drops

    # ------------------------------------------------------------------ #
    # VLM 轮转调度
    # ------------------------------------------------------------------ #
    def attach_detector(self, detector) -> None:
        """把 WdnaiDetector 接上，轮转线程随即开始提交。"""
        with self._lock:
            self._detector = detector

    def _set_judge_owner(self, rr_owns: bool) -> None:
        """切换判据由谁打分。

        ``False``（向导模式，未启动检测）：解码线程按 ``sample_every`` 采样
        打分，参照帧 = 上一次判变的**采样**帧。``True``（应用页面）：轮转线程
        在送检那一刻打分，参照帧 = 上一次**送检**的帧。

        **切换时必须丢掉参照帧**：两种模式的参照帧语义不同，混用会算出
        「上次的采样帧 vs 本次的送检帧」这种没意义的对比（采样帧可能来自
        半分钟前）。走 ``FrameJudge.reset()`` 而非只清 _last_luma —— 曲线
        上的历史点也是按旧口径算的。
        """
        with self._lock:
            channels = list(self._channels)
        for ch in channels:
            ch.rr_owns_judge = bool(rr_owns)
            if ch.judge is not None:
                try:
                    ch.judge.reset()
                except Exception:  # noqa: BLE001
                    LOG.warning("mc-ch%02d 判据重置失败", ch.idx, exc_info=True)

    def start_round_robin(self) -> None:
        """起轮转线程，**每个推理 slot 一条**（K 条），至多一组在跑。

        退出靠 ``_rr_gen`` 代号而非线程身份：线程身份判据在"停一次立刻再起"
        下不成立（旧线程可能还没被调度到、引用就被换掉了），AGENTS.md 记过
        由此引发的"两个线程一起转、轮转顺序乱成 1,2,1,3,4,2"。
        """
        self.stop_round_robin(judge_owner=False)   # 只停线程，判据归属随后统一切
        with self._lock:
            detector = self._detector
        k = 1
        if detector is not None:
            try:
                k = max(1, int(getattr(detector, "slot_count", 1)))
            except Exception:  # noqa: BLE001
                k = 1
        # 进「应用页面」语义：判据改由轮转线程在送检时打分。
        self._set_judge_owner(True)
        with self._rr_lock:
            self._rr_gen += 1
            gen = self._rr_gen
            self._rr_threads = [
                threading.Thread(
                    target=self._rr_loop, args=(gen, i),
                    name="mc-vlm-rr-%d" % i, daemon=True)
                for i in range(k)
            ]
            threads = list(self._rr_threads)
        for t in threads:
            t.start()
        LOG.info("mc-vlm-rr 起了 %d 条 feeder（slot 数 = %d）", k, k)

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

    def stop_round_robin(self, *, judge_owner: Optional[bool] = None) -> None:
        """停轮转线程。

        ``judge_owner``：是否同时把判据交回解码线程（向导模式）。默认
        ``None`` = 交回。``start_round_robin()`` 内部传 ``False`` 保持不动，
        否则「先 stop（交回 + 重置参照帧）再 start（改成轮转 + 再重置）」
        会把归属来回 toggle，中间解码线程有机会用向导语义抢建参照帧。
        """
        with self._rr_lock:
            self._rr_gen += 1          # 让所有旧 feeder 自行退出
            self._rr_threads = []
        with self._lock:
            self._rr_current = -1
            self._ch_inflight.clear()
        if judge_owner is not False:
            # 顺序在 _rr_gen 递增之后 —— 旧 feeder 可能正跑到判据那一行，
            # 但它们下一步会因代号过期退出，且 observe 内部持 RLock 不会写花。
            self._set_judge_owner(False)

    def _take_channel(self, channels) -> Optional[Any]:
        """原子地从共享游标取一路并登记占用。返回 None = 全被占用。

        **K=1 退化成严格 0→1→…→N-1**：只有一个 feeder 时任何时刻至多一路被
        占用，游标又只增不回退 —— 与多卡化之前的轮转语义逐字相同。
        """
        with self._ch_lock:
            n = len(channels)
            for _ in range(n):
                idx = self._ch_cursor % n
                self._ch_cursor += 1
                if idx in self._ch_inflight:
                    continue
                self._ch_inflight.add(idx)
                return channels[idx]
        return None

    def _release_channel(self, idx: int) -> None:
        with self._ch_lock:
            self._ch_inflight.discard(idx)

    def _rr_loop(self, gen: int, slot_idx: int = 0) -> None:
        """VLM 轮转调度：每个推理 slot 一条 feeder，**共享一路游标**。

        每条 feeder：等自己的 slot 空闲 → 从共享游标抢下一路 → 取该路**此刻
        最新的一帧**提交 → 等推理完释放该路。

        **抢下一路**而不是按 ``ch % K == slot`` 静态分配：路数不能被 K 整除时
        静态分配会永久失衡（3 路 2 卡 → 卡0 干 2/3 的活）。共享游标 + 「谁先
        空闲谁先取」让跑得快的卡自动多干活。取路时登记进 ``_ch_inflight``，
        同一路不会被两个 slot 同时推理。
        """
        spin_streak = 0
        while not self._stop_evt.is_set():
            with self._rr_lock:
                if gen != self._rr_gen:
                    return
            with self._lock:
                channels = list(self._channels)
                detector = self._detector
                interval = self._rr_interval
            if not channels or detector is None:
                self._stop_evt.wait(0.2)
                continue

            # 等自己的 slot 空闲（多卡下不能用 detector 的全局 pending）
            try:
                if slot_idx >= getattr(detector, "slot_count", 1):
                    self._stop_evt.wait(0.2)
                    continue
                detector.wait_slot_idle(slot_idx, timeout=RR_SLOT_IDLE_WAIT)
            except Exception as exc:  # noqa: BLE001
                # 这里不能静默 pass：detector 上若缺 wait_slot_idle，
                # AttributeError 被吞掉后 feeder 立刻空转 —— 每秒几千次、
                # round 恒 0，现象和"模型没返回"一模一样。
                LOG.warning("mc-vlm-rr slot%d 等 slot 空闲失败: %s",
                            slot_idx, exc)
                self._stop_evt.wait(0.5)
                continue

            ch = self._take_channel(channels)
            if ch is None:
                # 全被别的 feeder 占着 —— 短暂退避，别空转烧核
                self._stop_evt.wait(0.05)
                continue

            try:
                analyzed, spin = self._run_one_channel(ch, detector, slot_idx,
                                                        interval)
            finally:
                self._release_channel(ch.idx)
            if analyzed:
                spin_streak = 0
                with self._lock:
                    self._rr_round += 1
            elif spin:
                # 连续空转（帧压根没进队列）要**跨轮累计**才退避：亚毫秒返回时
                # 一轮就是几毫秒，不退避就是每秒几千次空转，烧一个核且一帧
                # 没推理，而 round 不涨、事件不涨，从外面看"一切正常"。
                spin_streak += 1
                if spin_streak >= RR_SPIN_STREAK_LIMIT:
                    LOG.warning(
                        "mc-vlm-rr slot%d 连续 %d 次空转（帧未进队列），退避",
                        slot_idx, spin_streak)
                    with self._lock:
                        self._rr_spins += spin_streak
                    spin_streak = 0
                    self._stop_evt.wait(RR_SPIN_BACKOFF)
            else:
                # 判据挡下 / 没拿到送检帧：同样得退避，否则 feeder 会在
                # 「判据 + 缩略图」上 tight-loop。
                self._stop_evt.wait(RR_NO_PROGRESS_WAIT)

    def _run_one_channel(self, ch, detector, slot_idx: int,
                          interval: float) -> tuple:
        """跑一路：判据门控 → 取送检帧 → 投给 slot → 等推理完。"""
        with self._lock:
            self._rr_current = ch.idx
        try:
            # 判据帧号必须单调：不能用 ch.frame_pos（cap.frame_pos 循环重播时归零，
            # gap 会变负；every_n 大于视频长度时循环之后彻底静默）。
            judge_pos = ch.frames
            judge_score: Optional[float] = None
            # 判据门控只在自动模式下走，manual 整段跳过（不缩缩略图也不
            # 算分），此时行为与接入判据前一致。
            #
            # **应用页面模式下判据在这里自己打分**（rr_owns_judge=True）：
            # 比较的是「上一次送检的帧 vs 当前帧」，参照帧语义才是对的
            # —— 用户明确要求进入应用页面后按送检间隔算，而不是向导模式
            # 那个固定 25 帧的采样节奏。顺带还省掉了解码线程每帧的 RGA。
            # 回退分支（rr_owns_judge=False）才用只读的 decide_take_for_rr
            # 读解码线程留下的状态。
            if ch.judge is not None and ch.judge.is_auto:
                try:
                    if ch.rr_owns_judge:
                        thumb = ch.snapshot_gray_for_judge(ch.judge.long_side)
                        take = (True if thumb is None
                                else ch.judge.observe(thumb, judge_pos))
                    else:
                        take = ch.judge.decide_take_for_rr(judge_pos)
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("mc-ch%02d 判据异常，按放行处理: %s",
                                ch.idx, exc)
                    take = True
                if not take:
                    with self._lock:
                        self._rr_judge_skips += 1
                    return False, 0
                judge_score = ch.judge.current_score

            # 请求本路在**下一次解码**上多出一张送检尺寸的图（同一次
            # 解码的 DMA-BUF 多跑一次 RGA），然后等它交接。没有出帧就
            # 跳过本路。等待窗口要宽于一个解码帧周期。
            ch.request_inference()
            frame = ch.take_inference(timeout=ANA_WAIT_TIMEOUT)
            if frame is None:
                return False, 0

            t0 = time.perf_counter()
            try:
                ok = detector.submit_frame_to(
                    slot_idx, frame, ch.frame_pos,
                    judge_score=judge_score, channel=ch.idx)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("mc-vlm-rr ch%02d submit 失败: %s", ch.idx, exc)
                return False, 0
            if not ok:
                # 没进队列（detector 没启用 / 队列满）—— 不算推理，别白等
                return False, 0
            # 等**本 slot**推理完。单路卡死时不等满上限：超时就跳过继续轮，
            # 否则一个 slot 挂住会拖慢它那一路。
            try:
                done = detector.wait_slot_idle(slot_idx,
                                               timeout=RR_INFER_TIMEOUT)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("mc-vlm-rr slot%d 等推理结束失败: %s",
                            slot_idx, exc)
                done = False
            dt = (time.perf_counter() - t0) * 1000.0
            with self._lock:
                self._rr_infer_ms = dt
                self._rr_last_ts = time.time()
            if done and dt >= RR_MIN_INFER_MS:
                spin = 0
            elif done:
                # dt 接近 0 = 这一帧压根没进队列。退避由调用方按连续次数统一
                # 做，这里只上报"本次是空转"。
                spin = 1
            else:
                spin = 0
                with self._lock:
                    self._rr_skips += 1
                LOG.warning("mc-vlm-rr slot%d ch%02d 推理超时 %.1fs，跳过",
                            slot_idx, ch.idx, RR_INFER_TIMEOUT)
            # 「轮转间隔」：本路结束后额外等一会儿再抢下一路
            if interval > 0:
                self._stop_evt.wait(interval)
            return bool(done and dt >= RR_MIN_INFER_MS), spin
        finally:
            with self._lock:
                self._rr_current = -1

    def vlm_snapshot(self) -> dict:
        with self._lock:
            threads = list(self._rr_threads)
            inflight = sorted(self._ch_inflight)
        return {
            "attached": self._detector is not None,
            "running": any(t.is_alive() for t in threads),
            # 多卡并行：每个推理 slot 一条 feeder，K = 勾选的实例数
            "feeder_count": len(threads),
            "inflight_channels": inflight,
            "current": self._rr_current,
            "round": self._rr_round,
            "interval": self._rr_interval,
            "skips": self._rr_skips,
            "spins": self._rr_spins,
            "last_infer_ms": round(self._rr_infer_ms, 1),
            "last_ts": self._rr_last_ts,
        }

    # ------------------------------------------------------------------ #
    # 帧判据（每路一个独立判据器）
    # ------------------------------------------------------------------ #
    def configure_judge(
        self,
        mode: Optional[str] = None,
        every_n: Optional[int] = None,
        threshold: Optional[float] = None,
        channels: Optional[Dict[int, Dict[str, Any]]] = None,
    ) -> dict:
        """配置判据，对**已在跑**的各路立刻生效。

        归一化全交给 ``_judge_proto.reconfigure()``（迁移表、模式换了换默认
        阈值都在那里），这里只负责把结果下发到每一路 —— 与单路
        ``VideoSession.configure_judge`` 语义相同，差别只在这里有 N 份要同步。

        **三种下发模式**（互斥语义，但实现上可叠加）：

        * 顶层 ``mode/every_n/threshold`` 全 None + ``channels`` 不传 → no-op。
        * 顶层字段非空 + ``channels`` 为空 → 把当前配置当"新默认"，下发到
          **所有路**（覆盖各路现有值）。这是向导批量设 + 「应用到所有路」
          按钮的入口。
        * 顶层全 None + ``channels`` 非空 → **只动 channels 字典里**的
          那些路；其他路**保持现状，绝不被 reconfigure 冲回**。这一条是关键：
          之前对未指定路也跑了一次 ``reconfigure(proto_thr)``，等价于"用
          proto 当前值"把它们之前单独设的阈值冲回 —— 4 路下改 ch2 阈值会
          顺手把 ch0 之前单独设的 5.5 冲成 10，实测踩过。

        顶层与 ``channels`` 都给：把顶层当 fallback，下发所有路。
        """
        with self._lock:
            chs = [ch for ch in self._channels if ch.judge is not None]

        per_overrides: Dict[int, Dict[str, Any]] = {}
        if channels:
            for idx, cfg in channels.items():
                per_overrides[int(idx)] = {
                    "mode": cfg.get("mode"),
                    "every_n": cfg.get("every_n"),
                    "threshold": cfg.get("threshold"),
                }

        top_has = mode is not None or every_n is not None or threshold is not None
        if top_has and not per_overrides:
            # 1) 顶层下发所有路 —— 走 proto 拿到归一化结果
            self._judge_proto.reconfigure(mode=mode, every_n=every_n,
                                          threshold=threshold)
            proto_mode = self._judge_proto.mode
            proto_every = self._judge_proto.every_n
            proto_thr = self._judge_proto.threshold
            for ch in chs:
                ch.judge.reconfigure(mode=proto_mode, every_n=proto_every,
                                     threshold=proto_thr)
            LOG.info("mc-judge: %d 路下发 proto=mode=%s thr=%s",
                     len(chs), proto_mode, proto_thr)
        elif per_overrides:
            # 2) 仅按路下发 —— proto **不动**（用户没显式要改它），每路从
            #    当前值 + per_overrides 增量合并；channels 之外的 ch 跳过。
            for ch in chs:
                ov = per_overrides.get(ch.idx)
                if ov is None:
                    continue
                cur = ch.judge
                ov_mode = ov.get("mode") or cur.mode
                ov_every = ov.get("every_n") or cur.every_n
                ov_thr = ov.get("threshold") if ov.get("threshold") is not None else cur.threshold
                # reconfigure 会清参照帧与分数历史：阈值/算法变了之后，旧参照帧
                # 是按旧口径选出来的，继续用会把新旧分数混在一份曲线里。
                cur.reconfigure(mode=ov_mode, every_n=ov_every,
                                threshold=ov_thr)
            LOG.info("mc-judge: %d 路精细下发", len(per_overrides))
        else:
            LOG.info("mc-judge: no-op")
        return self.judge_snapshot()

    def reset_judge(self) -> dict:
        with self._lock:
            channels = [ch for ch in self._channels if ch.judge is not None]
        for ch in channels:
            ch.judge.reset()
        return self.judge_snapshot()

    def judge_snapshot(self) -> dict:
        """各路判据的**聚合**快照。

        对外仍是单个判据器的形状，共享前端组件 ``frame-judge.js`` 一套就能
        读，不用为多路另写曲线组件。标量是各路合计，逐路明细挂在
        ``per_channel`` 上（共享组件不读，给排查用）。
        """
        proto = self._judge_proto
        with self._lock:
            channels = list(self._channels)
        per: List[dict] = []
        evaluated = sampled = 0
        recent: List[List] = []
        cur: Optional[float] = None
        changed = False
        gen = 0
        for ch in channels:
            if ch.judge is None:
                continue
            s = ch.judge.snapshot()
            # gen 是配置代次，前端据此丢弃变更前就在路上的旧快照。各路是同一
            # 份配置下发下去的，取一个即可。
            gen = gen or s.get("gen") or 0
            evaluated += s["evaluated"]
            sampled += s["sampled"]
            # 曲线点带通道号：各路 pos 各自独立，不带号画出来是乱的
            recent.extend([p, sc, ch.idx] for p, sc in s["recent"])
            if s["cur"] is not None:
                cur = s["cur"]
                changed = s["changed"]
            per.append({
                "channel": ch.idx,
                "evaluated": s["evaluated"],
                "sampled": s["sampled"],
                "cur": s["cur"],
                "changed": s["changed"],
                # v0.23 每路独立配置：前端按路渲染判据配置
                "mode": ch.judge.mode,
                "every_n": ch.judge.every_n,
                "threshold": ch.judge.threshold,
            })
        recent.sort(key=lambda x: x[0])
        del recent[:-200]
        return {
            "gen": gen,
            "mode": proto.mode,
            "manual": not proto.is_auto,
            "every_n": proto.every_n,
            "threshold": proto.threshold,
            "long_side": proto.long_side,
            "cur": cur,
            "changed": changed,
            "evaluated": evaluated,
            "sampled": sampled,
            "every_n_eff": (evaluated / sampled) if sampled else None,
            "recent": recent,
            "per_channel": per,
            "judge_skips": self._rr_judge_skips,
        }

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    def channels_snapshot(self) -> List[dict]:
        with self._lock:
            return [c.snapshot() for c in self._channels]

    # ------------------------------------------------------------------ #
    # 放大态（纯显示）                                                  #
    # ------------------------------------------------------------------ #
    def set_zoom(self, idx: int, zoomed: bool) -> dict:
        """把第 ``idx`` 路切到 / 退出放大显示档。

        只改这一路的 ``_disp_w/_disp_h``。解码循环每帧都读 ``self._disp_w``，
        所以下一帧就是新尺寸，不需要重开解码器。

        Raises:
            ValueError: idx 不在范围内。
        """
        with self._lock:
            if not (0 <= idx < len(self._channels)):
                raise ValueError(
                    f"channel {idx} 不存在（共 {len(self._channels)} 路）")
            ch = self._channels[idx]
            if zoomed:
                src_long = max(ch.width, ch.height)
                # min(720p, max(源长边, 当前档))：不封顶则一路能吃掉整个
                # RGA/JPEG 预算；而只用源长边也不够 —— 非放大档本身就会放大源
                # （608 的源在 ≤4 路档下已渲成 960），照源 clamp 会让放大后
                # 清晰度反降。
                cur_long = max(self._disp_w, self._disp_h)
                long_edge = min(DISPLAY_LONG_EDGE_ZOOM,
                                max(src_long, cur_long))
                size = derive_analysis_size(ch.width, ch.height, long_edge)
            else:
                size = (self._disp_w, self._disp_h)
            # 写的是 Channel 自己的槽位，赋值即生效
            ch._disp_w, ch._disp_h = size
            self._zoomed = idx if zoomed else None
        LOG.info("zoom: ch%d -> %s (%dx%d)", idx,
                 "放大" if zoomed else "还原", size[0], size[1])
        return self.snapshot()

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
        with self._lock:
            zoomed = self._zoomed
        return {
            "state": state,
            "error": err,
            "channels": n,
            "loop": loop,
            "zoomed": zoomed,
            "disp_w": self._disp_w,
            "disp_h": self._disp_h,
            "analysis_size": self.analysis_size,
            "ana_w": self._ana_w,
            "ana_h": self._ana_h,
            "decode_fps_target": self._eff_decode_fps,
            "display_fps_target": self._eff_display_fps,
            "decode_fps_cap": self.decode_fps,
            "total_frames": total_frames,
            "total_errors": sum(c["errors"] for c in chans),
            # 广播侧计数：pub_frames=进广播的帧数（按通道汇总，各槽唯一写者
            # 所以无竞争），pub_drops=订阅队列满被丢的帧数。
            # drops>0 说明「后端解出来了但推不出去」。
            "pub_frames": sum(self._pub_per_ch),
            "pub_drops": self._pub_drops,
            "last_frame_ts": started,
            "vlm": self.vlm_snapshot(),
            "judge": self.judge_snapshot(),
            "channel_list": chans,
        }


_sessions: Dict[str, "MultiChannelSession"] = {}
_session_lock = threading.Lock()


def get_multi_session(
    key: str = "default",
    factory: Optional[Callable[[], "MultiChannelSession"]] = None,
) -> MultiChannelSession:
    """按 key 取多路会话单例（与 ``app.core.video.session`` 的单例互不相干）。

    **必须按 key 隔离**：多张卡跑在同一个 uvicorn 进程里，若共用一个
    实例，用户在 vtd 开 32 路再切到 vtl 开 32 路，后者调 ``open()`` 会
    把前者的 ``VideoCapture`` 全部 ``close()`` 掉 —— 表现为「切回上一页
    画面全黑」。每张卡传自己的 key（``"vtd"`` / ``"vtl"`` / ``"vd"``）。

    ``factory`` 只在该 key **首次**创建时用（vtl 传
    ``analysis_size=448``：它的 vision 硬编码 448×448，送更大只是让
    RGA 白缩一遍）。key 已存在时忽略 —— 实例已开跑，改尺寸无意义，
    换源请走 ``open()``。
    """
    with _session_lock:
        s = _sessions.get(key)
        if s is None:
            s = _sessions[key] = (factory() if factory
                                  else MultiChannelSession())
        return s


def reset_multi_session(key: Optional[str] = None) -> None:
    """测试用：丢弃单例。``key=None`` 清掉全部。"""
    with _session_lock:
        targets = list(_sessions.items()) if key is None else \
            [(key, _sessions[key])] if key in _sessions else []
        for k, s in targets:
            s.close()
            # 会话对象即将被丢弃，此时才允许清订阅名单（close() 不能清，
            # 否则 open() 复用单例时会摘掉已连接的 WebSocket —— 见 close 注释）
            s._drain_subs()
            _sessions.pop(k, None)