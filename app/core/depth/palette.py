"""深度热力图渲染 —— turbo 调色板 + 色标窗口。

**为什么宿主自己实现 turbo，而不用 ``wdnai.backends.depth_anything.turbo_color``**

``wdnai.backends.depth_anything`` 在模块顶层 import 了 ``._common`` →
``.._librknn`` → ``rknn3lite``。宿主进程碰 NPU runtime 是越界的（模型归
worker 所有），所以这里**自实现**一份纯 numpy 的 turbo。

逐位一致性：本模块的多项式系数与 ``(uint8_t)(c * 255.0f + 0.5f)`` 舍入
与 C++ demo（``examples/depth_anything_v3/cpp/main.cpp`` 的
``turbo_color``）**完全一致**，已用同一 C 源码编译对拍验证（逐通道最大差 0）。

**显示量的约定**（与 C++ demo 逐字一致，勿改）

C++ demo 的着色链路是::

    inverse_depth = 1.0f / view_depth[i];
    normalized   = (inverse_depth - lower) / (upper - lower);
    turbo_color(normalized, rgb);

其中 ``lower`` / ``upper`` 是**全体 view 有效像素**上 ``inverse_depth`` 的
2% / 98% 分位。所以：

* 显示量是 **1/depth**，**越大越近**（红端）；
* 色标窗口跨所有 view 共享 —— 同一份素材里颜色相同 = 相对深度相同。

本模块把「窗口」从「一次性算完」改成「随帧滑动」，让实时流有连续可比的
颜色；每路各自维护一档 EMA，再按需取跨路并集。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

import numpy as np

#: 色标窗口的 2% / 98% 分位（与 C++ demo 一致）
LOW_PCT = 2.0
HIGH_PCT = 98.0

#: 每路 EMA 的平滑系数：新窗口占 30%，旧窗口占 70%。
#: 取值理由：DA3 单 view 推理约 116 ms，窗口每秒最多更新几次；
#: 0.3 意味着约 3 帧收敛到新范围，肉眼看是「缓慢跟随」而不是「跳变」。
EMA_ALPHA = 0.3

#: 热力图 JPEG 的质量与色度采样。C++ 用 libjpeg-turbo 的 ``TJSAMP_422``，
#: OpenCV 默认 4:2:0 会在 turbo 的陡峭色带上明显串色 —— 这里显式对齐 4:2:2。
JPEG_QUALITY = 90


def turbo_rgb(values: np.ndarray) -> np.ndarray:
    """``[0,1]`` → Turbo RGB（``uint8``，形状 ``(..., 3)``）。

    5 次多项式逐通道求值 + ``[0,1]`` clamp + ``(uint8_t)(c*255+0.5)`` 舍入，
    与 C++ demo 的 ``turbo_color`` 逐位一致。
    """
    x = np.clip(np.asarray(values, dtype=np.float32), 0.0, 1.0)
    x2 = x * x
    x3 = x2 * x
    x4 = x3 * x
    x5 = x4 * x
    red = (0.13572138 + 4.61539260 * x - 42.66032258 * x2 + 132.13108234 * x3
           - 152.94239396 * x4 + 59.28637943 * x5)
    green = (0.09140261 + 2.19418839 * x + 4.84296658 * x2 - 14.18503333 * x3
             + 4.27729857 * x4 + 2.82956604 * x5)
    blue = (0.10667330 + 12.64194608 * x - 60.58204836 * x2 + 110.36276771 * x3
            - 89.90310912 * x4 + 27.34824973 * x5)
    out = np.stack([
        np.clip(red, 0.0, 1.0),
        np.clip(green, 0.0, 1.0),
        np.clip(blue, 0.0, 1.0),
    ], axis=-1)
    return (out * 255.0 + 0.5).astype(np.uint8)


def inverse_depth(depth: np.ndarray) -> np.ndarray:
    """深度数组 → 显示量 ``1/depth``（**越大越近**）。

    与 C++ demo 一致：只有 ``depth > 0`` 且有限的位置才算有效，其余记 0。
    """
    d = np.asarray(depth, dtype=np.float32)
    positive = (d > 0.0) & np.isfinite(d)
    return np.where(positive, 1.0 / np.where(positive, d, 1.0), 0.0).astype(np.float32)


def percentile_window(values: np.ndarray) -> Optional[Tuple[float, float]]:
    """有效显示量上的 2% / 98% 分位窗口；有效像素不足 10 个时返回 ``None``。"""
    v = np.asarray(values, dtype=np.float32).reshape(-1)
    v = v[np.isfinite(v) & (v > 0.0)]
    if v.size <= 10:
        return None
    s = np.sort(v)
    lo = float(s[int(LOW_PCT / 100.0 * (s.size - 1))])
    hi = float(s[int(HIGH_PCT / 100.0 * (s.size - 1))])
    if hi <= lo:
        hi = lo + 1e-6
    return lo, hi


@dataclass
class _ChannelWindow:
    """单路的 EMA 窗口。"""

    lo: float = 0.0
    hi: float = 1.0
    seen: bool = False

    def update(self, lo: float, hi: float, alpha: float = EMA_ALPHA) -> None:
        if not self.seen:
            self.lo, self.hi, self.seen = lo, hi, True
            return
        self.lo += alpha * (lo - self.lo)
        self.hi += alpha * (hi - self.hi)
        if self.hi <= self.lo:
            self.hi = self.lo + 1e-6


class DepthWindow:
    """跨路共享 / 每路独立 的色标窗口管理。

    每路来一张深度图就更新自己那档 EMA；取窗口时：

    * ``shared=True`` → 所有路的**并集**（跨路颜色可比）；
    * ``shared=False`` → 该路自己的窗口（每路用满色阶）。

    线程安全：更新发生在各路推理线程里，读取发生在渲染线程里。
    """

    def __init__(self, *, shared: bool = True) -> None:
        self._lock = threading.Lock()
        self._channels: Dict[int, _ChannelWindow] = {}
        self._shared = bool(shared)

    @property
    def shared(self) -> bool:
        return self._shared

    @shared.setter
    def shared(self, value: bool) -> None:
        with self._lock:
            self._shared = bool(value)

    def update(self, channel: int, inv: np.ndarray) -> Optional[Tuple[float, float]]:
        """用一张深度图的显示量更新该路窗口，返回**本次渲染实际使用的**窗口。

        更新用的是「本路自己的 EMA」，返回的可能是「跨路并集」—— 两者分开算，
        这样同一轮里先到的路不会因为后到的路而立刻改色。
        """
        win = percentile_window(inv)
        if win is None:
            return None
        lo, hi = win
        with self._lock:
            cw = self._channels.setdefault(int(channel), _ChannelWindow())
            cw.update(lo, hi)
            return self._window_locked(int(channel))

    def _window_locked(self, channel: int) -> Optional[Tuple[float, float]]:
        if not self._channels:
            return None
        if not self._shared:
            cw = self._channels.get(channel)
            return (cw.lo, cw.hi) if cw else None
        los = [c.lo for c in self._channels.values()]
        his = [c.hi for c in self._channels.values()]
        lo, hi = min(los), max(his)
        if hi <= lo:
            hi = lo + 1e-6
        return lo, hi

    def window(self, channel: int) -> Optional[Tuple[float, float]]:
        with self._lock:
            return self._window_locked(int(channel))

    def reset(self) -> None:
        with self._lock:
            self._channels.clear()


def render_depth_jpeg(
    depth: np.ndarray,
    lo: float,
    hi: float,
    *,
    out_size: Optional[Tuple[int, int]] = None,
    quality: int = JPEG_QUALITY,
) -> Tuple[bytes, Dict[str, float]]:
    """把一张深度图渲染成 turbo 热力图 JPEG。

    返回 ``(jpeg_bytes, stats)``。``stats`` 里是**原始深度数组**的
    min/max/mean，以及实际使用的色标窗口（显示量 ``1/depth`` 的量纲）。

    无效像素（``depth <= 0`` 或非有限）渲染成纯黑 —— 与 C++ demo 的
    ``memset(rgb + i*3, 0, 3)`` 一致。

    ``out_size=(w, h)``：**在深度数组层面**重采样到目标尺寸，再着色。

    为什么不是「先出 280×280 的 JPEG、再把 JPEG 缩到目标尺寸」：

    * JPEG 是有损的，缩放后再编码 = 有损压缩跑两遍（实测 turbo 的陡峭色带
      在 4:2:0 下本来就容易串色，两遍更糊）；
    * 缩 JPEG 缩的是**颜色**，缩数组缩的是**深度值** —— 后者才是物理量，
      重采样后再归一化才正确（插值会把近处的深度值混进远处像素）。

    模型输出固定 280×280（正方形），而送检帧是**按源宽高比**的（如
    288×157）。深度图要还原成源视场比例才能和左侧视频对齐，所以这一步
    重采样是必须的，不是可选的美化。
    """
    import cv2

    d = np.asarray(depth, dtype=np.float32)
    valid = (d > 0.0) & np.isfinite(d)
    vals = d[valid]
    # stats 取**原始数组**的统计（重采样不改变物理量的分布语义）
    stats = {
        "depth_min": float(vals.min()) if vals.size else 0.0,
        "depth_max": float(vals.max()) if vals.size else 0.0,
        "depth_mean": float(vals.astype(np.float64).mean()) if vals.size else 0.0,
        "inv_lo": float(lo),
        "inv_hi": float(hi),
        "valid_px": int(valid.sum()),
    }

    if out_size is not None:
        ow, oh = int(out_size[0]), int(out_size[1])
        if ow > 0 and oh > 0 and (ow, oh) != (d.shape[1], d.shape[0]):
            # 缩小用 INTER_AREA（面积平均，不丢细节）；放大用 INTER_LINEAR。
            interp = cv2.INTER_AREA if ow < d.shape[1] else cv2.INTER_LINEAR
            d = cv2.resize(d, (ow, oh), interpolation=interp)
            valid = (d > 0.0) & np.isfinite(d)

    inv = inverse_depth(d)
    span = max(float(hi) - float(lo), 1e-6)
    norm = (inv - float(lo)) / span
    norm = np.where(valid, np.clip(norm, 0.0, 1.0), 0.0)

    rgb = turbo_rgb(norm)
    bgr = np.ascontiguousarray(rgb[:, :, ::-1])
    bgr[~valid] = 0

    params = [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]
    sampling = getattr(cv2, "IMWRITE_JPEG_SAMPLING_FACTOR_422", None)
    if sampling is not None:
        params += [int(cv2.IMWRITE_JPEG_SAMPLING_FACTOR), int(sampling)]
    ok, buf = cv2.imencode(".jpg", bgr, params)
    if not ok:
        raise RuntimeError("深度热力图 JPEG 编码失败")

    return buf.tobytes(), stats


@dataclass
class DepthStats:
    """一次深度推理的统计（随事件下发，供前端显示数值）。"""

    depth_min: float = 0.0
    depth_max: float = 0.0
    depth_mean: float = 0.0
    inv_lo: float = 0.0
    inv_hi: float = 0.0
    valid_px: int = 0
    extra: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, float]:
        out = {
            "depth_min": self.depth_min,
            "depth_max": self.depth_max,
            "depth_mean": self.depth_mean,
            "inv_lo": self.inv_lo,
            "inv_hi": self.inv_hi,
            "valid_px": self.valid_px,
        }
        out.update(self.extra)
        return out
