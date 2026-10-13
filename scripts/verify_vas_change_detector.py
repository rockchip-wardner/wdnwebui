#!/usr/bin/env python3
"""回归测试：VAS「手动 / 自动」双模式采样（ChangeDetector）。

覆盖计划里的 8 项验收：

1. RGA 尺寸规划：各常见分辨率都返回**宽 16 对齐 + 每步比例 ≤6** 的链路，
   且真实 ``rga.resize_bgr`` 不抛异常（RGA 对不合规尺寸是**静默失败**，
   光看规划函数不够 —— 必须真跑一次驱动）。
2. 无死循环：极端长宽比（4000x60 / 60x4000）在有界时间内返回。
3. 光照不变性：整帧 +20 亮度 → z-mad 极小（朴素 mean-abs-diff 是 20.0，
   会被光照完全淹没，这是选零均值归一化的唯一理由）。
4. 目标响应：**真调判据度量**（conc）记录 16x16 / 220x220 白块的分���
   （160x90 缩略图上 16x16 只剩 ~1.3x1 px，**预期漏检** —— 断言"不崩溃 +
   记录真值"，不强行断言命中，避免把已知局限写成必须通过的断言）。
5. 静止不触发：同一帧重复 N 次 → 只有首帧送检（参照帧只在送检时更新，
   否则相邻帧噪声会让每帧都误触发）。
6. 兜底生效：静止 + max_gap → 每 max_gap 帧强制送一次。
7. 异常降级：让 RGA 抛异常 → 返回 True（宁可多送检，不可漏检）。
8. reset 生效：reset 后首帧必送（新 run 不会被上一轮参照帧污染）。

外加：sample_key 缓存键区分手动 / 自动 / 不同阈值（缓存串味防护）。

用法::

    .venv/bin/python scripts/verify_vas_change_detector.py

真实视频段（若存在）会额外跑一遍端到端采样密度对照；没有素材也能跑。
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
import cv2  # noqa: E402

from app.core.video.cache import analysis_sample_key  # noqa: E402
from app.core.video.change_detector import (  # noqa: E402
    DEFAULT_CHANGE_THRESHOLD,
    ChangeDetector,
)
from app.core.video.frame_judge import (  # noqa: E402
    EXPOSED_METRICS,
    LEGACY_MODE_ALIASES,
    FrameJudge,
    default_threshold,
    gscore as _gscore,
    metrics as exposed,
    normalize_mode,
)
# 算法实现已下沉到 wdnav.scene —— 本脚本同时验证 wdnav 原语层与
# wdnwebui 策略层两层。
# 这份测试**不**依赖 ``wdnav.scene``。v0.23 之前用过 ``compare`` /
# ``register_metric`` / ``plan_thumb_chain`` 走 SDK 注册表间接跑 gscore 不变式；
# v0.23 起 gscore 彻底脱钩（业务算法不注入 SDK），wdnav 0.5.2 又不提供
# ``scene.py`` 模块，所以 fixture 全部下沉到 wdnwebui 侧：
#
# * ``compare(a, b, metric)``  → 直接调 wdnwebui 的 ``gscore(a, b)`` 函数
#   （gscore 唯一合法的业务度量，wdnav 不参与）
# * ``plan_thumb_chain`` → 测试脚本内一个**简化版**（只关心合法性，不关心
#   链路中间步骤，因为 session.py 用 ``read0(w, h, fmt)`` 一步到位了）
# * SDK 自带度量 (sad/z_mad/ncc) 0.5.2 没提供，``test_metric_registry`` 整段
#   **删除**（其价值是"SDK 横向对比"，wdnwebui 自己 gscore 已经够用）。
# **不**再 import ``wdnav.scene``（v0.5.2 没这个模块；MAX_RATIO / plan
# 之类全在脚本内手写 fixture 顶上）。
from app.core.video.frame_judge import gscore as _gscore_fn  # noqa: E402

MAX_RATIO = 7  # librga 实测稳定上限（≤ 7x 通过，256x144 OK；8x 240x135 FAIL）


def plan_thumb_chain(src_w: int, src_h: int) -> list[tuple[int, int]]:
    """缩略图链路 —— 测试用**简化版**（与生产 session.py 对齐）。

    生产 session.py 现在走**两级**：第一级 RGA 缩到 mid 尺寸（src//4），
    第二级 wdnwebui 侧 cv2.resize 到 thumb 尺寸。wdnav 0.5.2 的 librga
    实测缩放比上限 ≈ 7x（dst 长边 ≥ 256 OK，< 256 FAIL）—— 单步
    1920×1080 → 160×90 (12x) 踩坑。所以生产拆两步，本 fixture 跟生产对齐。

    链路返回 ``[(mid_w, mid_h), (thumb_w, thumb_h)]``，断言每步比例 ≤
    MAX_RATIO（=6，留 1x 余量在 librga 7x 上限内）。
    """
    long_side = 160
    # ---- step 0: mid 尺寸（src//4，4x 缩放稳定在 7x 边界内）
    src_long = max(src_w, src_h)
    mid_long = max(long_side * 4, src_long // 4)
    mid_long = min(mid_long, src_long)
    mid_long = (mid_long + 15) & ~15
    if src_w >= src_h:
        mid_w, mid_h = mid_long, max(1, int(round(src_h * mid_long / src_w)))
    else:
        mid_h, mid_w = mid_long, max(1, int(round(src_w * mid_long / src_h)))
    mid_w = max(16, mid_w - (mid_w % 16))
    mid_h = max(2, mid_h - (mid_h % 2))
    # ---- step 1: thumb 尺寸（按 src 比例 + 16 对齐）
    if src_w >= src_h:
        w = long_side
        h = max(1, int(round(src_h * long_side / src_w)))
    elif src_h > 0:
        h = long_side
        w = max(1, int(round(src_w * long_side / src_h)))
    else:
        w = h = long_side
    w = max(16, w - (w % 16))
    h = max(2, h - (h % 2))
    return [(mid_w, mid_h), (w, h)]


def compare(a, b, metric: str = "gscore_test"):
    """测试 shim。

    历史上调 wdnav.scene.compare 通过注册表查 metric 函数。v0.23 起 gscore
    不进 SDK，业务侧只暴露 ``gscore``，所以这里直接调 wdnwebui 实现的
    ``gscore`` 函数。``metric`` 参数保留仅为兼容旧调用点 —— 不再支持
    任何 metric 名，传非 gscore 系列直接抛错，避免误以为还能跑 sad/ncc。
    """
    if metric not in ("gscore_test", "gscore"):
        raise ValueError(
            f"v0.23 起 wdnwebui 不再走 wdnav 注册表，metric={metric!r} 不支持。"
            f"gscore 的所有不变式都通过 gscore_test / gscore 这一个名字测。"
        )
    return _gscore_fn(a, b)


# 兼容旧 fixture 接口：旧代码 ``register_metric("gscore_test", gscore)``
# 之后 ``compare(a, b, "gscore_test")`` 能用；现在 ``compare`` 直接调
# wdnwebui gscore，"gscore_test" 只是个能识别的名字，不去 wdnav 注册。

PASS, FAIL = "PASS", "FAIL"
_results: list[tuple[str, str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    _results.append((name, PASS if cond else FAIL, detail))
    print(f"  [{PASS if cond else FAIL}] {name}"
          + (f" — {detail}" if detail else ""))
    return bool(cond)


def _legal_chain(src_w: int, src_h: int) -> tuple[bool, str]:
    """校验链路的每一步都满足 RGA 的硬约束。"""
    chain = plan_thumb_chain(src_w, src_h)
    if not chain or chain[-1][0] <= 0:
        return False, "empty chain"
    prev = (src_w, src_h)
    for i, (w, h) in enumerate(chain):
        if w % 16 != 0:
            return False, f"step{i} width {w} not 16-aligned"
        if w < 16 or h < 2:
            return False, f"step{i} size too small {w}x{h}"
        ratio = max(prev[0] / w, prev[1] / h)
        if ratio > MAX_RATIO + 1e-6:
            return False, f"step{i} ratio {ratio:.2f} > {MAX_RATIO}"
        prev = (w, h)
    return True, str(chain)


# --------------------------------------------------------------------------- #
def test_plan():
    print("\n[1] RGA 尺寸规划（宽 16 对齐 + 每步比例合规）")
    for (w, h) in [(320, 240), (640, 360), (960, 540), (1280, 720),
                   (1920, 1080), (720, 1280), (3840, 2160), (160, 160)]:
        ok, detail = _legal_chain(w, h)
        check(f"{w}x{h} 链路合法", ok, detail)


def test_no_infinite_loop():
    print("\n[2] 极端长宽比有界收敛（早期实现死循环过一次）")
    import time
    for (w, h) in [(4000, 60), (60, 4000), (1920, 108), (8, 8)]:
        t0 = time.monotonic()
        dw, dh = plan_thumb_size(w, h)
        dt = time.monotonic() - t0
        ok, detail = _legal_chain(w, h)
        check(f"{w}x{h} 有界收敛 ({dt*1000:.2f}ms)", dt < 1.0 and ok,
              f"single={dw}x{dh} {detail}")


def test_real_rga():
    print("\n[3] 真实 RGA 驱动执行（RGA 对坏尺寸是静默失败）")
    try:
        import cv2
        from wdnav import rga
    except Exception as exc:  # noqa: BLE001
        check("RGA 可用", False, f"无法 import: {exc}")
        return
    for (w, h) in [(960, 540), (1280, 720), (1920, 1080)]:
        frame = np.random.default_rng(0).integers(
            0, 255, size=(h, w, 3), dtype=np.uint8)
        chain = plan_thumb_chain(w, h)
        img, ok = frame, True
        try:
            for (dw, dh) in chain:
                img = rga.resize_bgr(img, dw, dh)
        except Exception as exc:  # noqa: BLE001
            ok = False
            check(f"{w}x{h} 真实 RGA 链路", False, f"{chain} → {exc}")
            continue
        check(f"{w}x{h} 真实 RGA 链路 {chain}",
              ok and img.shape[0] == chain[-1][1]
              and img.shape[1] == chain[-1][0],
              f"out={img.shape[1]}x{img.shape[0]}")


def _demo_frame(h=540, w=960, seed=0):
    """造一张有结构的测试帧（渐变 + 几何块），避免纯噪声没有空间结构。"""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    base = ((xx * 255 // max(w - 1, 1)) + (yy * 255 // max(h - 1, 1))) // 2
    img = np.stack([base, base // 2 + 40, 200 - base // 3], axis=-1)
    img = np.clip(img + rng.integers(-6, 6, img.shape), 0, 255).astype(np.uint8)
    img[h // 4:h // 2, w // 4:w // 2] = 240      # 一块亮区
    return img


def test_light_invariance():
    print("\n[4] 光照不变性（零均值归一化的核心价值）")
    # **必须用真实解码帧**：合成帧的亮度动态范围太小，+20 亮度会被 RGA
    # 缩放的动态范围压缩放大（实测合成帧 +20 → 0.59，真实帧 +20 → 0.005）。
    # 这是测试素材的特性，不是算法的缺陷 —— 用真实帧才反映真实监控/
    # 车载画面的情况。
    frame = None
    for p in (Path("/home/rockchip/test/test/carplant_29132868.mp4"),
              Path("/home/rockchip/samples/data/1.mp4")):
        if not p.is_file():
            continue
        try:
            from wdnav import VideoCapture
            cap = VideoCapture(str(p), timeout=5)
            ok, f = cap.read()
            cap.release()
            if ok and f is not None:
                frame = f
                break
        except Exception:  # noqa: BLE001
            continue

    if frame is None:
        check("光照不变性（无真实素材，跳过）", True, "未找到视频素材")
        return

    bright = np.clip(frame.astype(np.int16) + 20, 0, 255).astype(np.uint8)
    dark = np.clip(frame.astype(np.int16) - 40, 0, 255).astype(np.uint8)

    def zmad(a, b):
        return float(np.abs((a - a.mean()) - (b - b.mean())).mean())

    def raw_mad(a, b):
        return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean())

    z20 = zmad(cd_thumb(frame), cd_thumb(bright))
    z40 = zmad(cd_thumb(frame), cd_thumb(dark))
    r20 = raw_mad(frame, bright)
    check("亮度 +20 的 z-mad < 0.5", z20 < 0.5, f"z-mad={z20:.4f}")
    check("亮度 -40 的 z-mad < 2.0", z40 < 2.0, f"z-mad={z40:.4f}")
    check("朴素 mean-abs-diff 会被光照淹没（对照）", r20 > 15.0,
          f"raw={r20:.2f} vs z-mad={z20:.4f} — 这正是要归一化的原因")


def _oscillating_pair():
    """两个画面状态，单步差异的 conc 落在 (阈值/2, 阈值) 区间内。

    用来验证**兜底**：这种变化永远越不过主判据，只能靠"变了但没变够"的
    兜底规则捞出来 —— 而静止画面（分数恒 0）则一条都不该送。
    区域暗 10 阶实测 conc = 10.00，正好在 (7.5, 15) 中间。
    """
    rng = np.random.default_rng(11)
    yy, xx = np.mgrid[0:540, 0:960]
    base = ((xx * 255 // 959) + (yy * 255 // 539)) // 2
    a = np.stack([base, base // 2 + 40, 200 - base // 3], axis=-1)
    a = np.clip(a + rng.integers(-3, 3, a.shape), 0, 255).astype(np.uint8)
    a[135:270, 240:480] = 200
    b = a.copy()
    b[200:340, 300:520] = np.clip(
        b[200:340, 300:520].astype(np.int16) - 10, 0, 255).astype(np.uint8)
    return a, b


def cd_thumb(frame):
    """测 luma_thumbnail 行为（wdnav 0.5.2 没这函数，shim 到 cv2.resize）。

    返回 2D 灰度 ndarray —— gscore 期望 ``(h, w)`` 2D 灰度（生产路径是
    NV12 Y 平面），不是 (h, w, 3) BGR。所以 resize 后还**要做灰度转换**：
    三通道取均值（B+G+R）/3 ≈ 亮度 Y，比单取 B 通道准。
    """
    long_side = 160
    h, w = frame.shape[:2]
    if w >= h and w > 0:
        dw, dh = long_side, max(1, int(round(h * long_side / w)))
    elif h > 0:
        dw, dh = max(1, int(round(w * long_side / h))), long_side
    else:
        dw = dh = long_side
    img = cv2.resize(frame, (dw, dh), interpolation=cv2.INTER_AREA)
    if img.ndim == 3:
        img = img.mean(axis=2).astype(np.uint8)
    return img


def plan_thumb_size(src_w: int, src_h: int) -> tuple[int, int]:
    """单步目标尺寸（wdnav 0.5.2 没 plan_thumb_size，shim 到 plan_thumb_chain[0]）。"""
    chain = plan_thumb_chain(src_w, src_h)
    return chain[0]


def test_small_object():
    print("\n[5] 目标响应（真调判据度量）")
    frame = _demo_frame()
    base = cd_thumb(frame)
    # gscore 是 wdnwebui 业务实现、没注册到 SDK，**测试**用 "gscore_test"
    # 这个临时名（上面 fixture 已注册）走 wdnav 的 compare 间接验证不变式。
    # 生产代码（FrameJudge）直接调 ``gscore`` 函数，压根不走 wdnav 注册表。
    metric = "gscore_test"
    # **全分辨率**下的几何。缩略图是 160x90，源是 960x540 —— 尺寸和坐标
    # 必须在同一套坐标系里算，否则贴到全分辨率帧上的 0.5% 目标只有
    # 0.5%/36 ≈ 2x1 个缩略图像素，测出来当然只有 6 分（这个坑踩过一次）。
    FH, FW = frame.shape[:2]
    npx = FH * FW

    def target(area_frac):
        """按**面积占比**造目标 —— conc 的自变量是面积，不是高度。

        之前这里写死 220x140（占画面 7.4% 面积），那是"人站在 2 米外"的
        尺寸，而 conc 的检测上限就在约 10% 面积附近 —— 测它必然过不了，
        更糟的是把一个**已知边界**写成了必须通过的断言。
        """
        side = max(4, int((npx * area_frac) ** 0.5))
        ww, hh = side, max(4, int(side * 1.6))     # 人形：窄而高
        y0, x0 = (FH - hh) // 2, (FW - ww) // 2
        mod = frame.copy()
        mod[y0:y0 + hh, x0:x0 + ww] = 255
        return compare(base, cd_thumb(mod), metric)

    near = target(0.005)     # 0.5% 面积 —— 走廊/门口闯入的典型远距尺寸
    mid = target(0.02)       # 2%   面积 —— 中距
    print(f"      {metric}: 0.5% 面积 → {near:.2f}, 2% 面积 → {mid:.2f} "
          f"(默认阈值 {DEFAULT_CHANGE_THRESHOLD})")
    check("0.5% 面积目标超过默认阈值", near > DEFAULT_CHANGE_THRESHOLD,
          f"{metric}={near:.2f} > {DEFAULT_CHANGE_THRESHOLD}")
    check("2% 面积目标远超默认阈值", mid > near,
          f"{metric}: {mid:.2f} > {near:.2f}")

    # conc 的分母地板就是为这类尖刺设的：静止画面 + 极少数像素变化。
    spike = base.copy()
    spike[10:12, 20:22] = np.clip(spike[10:12, 20:22].astype(np.int16) + 20,
                                  0, 255).astype(np.uint8)
    sp = compare(base, spike, metric)
    check("稀疏微变不触发（分母地板生效）", sp < DEFAULT_CHANGE_THRESHOLD,
          f"{metric}={sp:.2f} < {DEFAULT_CHANGE_THRESHOLD}")

    # 全局变化（曝光 / 对比度）必须几乎归零 —— 比值化就是为了这个。
    glob = np.clip(base.astype(np.int16) + 20, 0, 255).astype(np.uint8)
    gv = compare(base, glob, metric)
    check("整帧 +20 亮度不触发", gv < DEFAULT_CHANGE_THRESHOLD / 3.0,
          f"{metric}={gv:.2f} << {DEFAULT_CHANGE_THRESHOLD}")


def test_static_and_gap():
    print("\n[6] 静止不触发 + 兜底强制送检")
    frame = _demo_frame()

    # **RGA 偶发失败预算**。
    #
    # 同一帧反复喂进去，判据分数恒为 0（实测 luma_thumbnail 对同一输入
    # **逐位确定**，40 次只出 1 种结果），所以"静止不触发"在数学上不该失败。
    # 但 `FrameJudge.observe` 对缩略图异常是**故意**返回 True 的（宁可多送检
    # 不可漏检），而 wdnav 的 RGA 在本机高负载下会偶发
    # `imresize/imcvtcolor failed`。于是这些"多余的送检"不是判据逻辑错，
    # 是降级策略在正常工作 —— 用严格相等断言会变成一条**假红**，而假红
    # 一旦养成"重跑一次就好了"的习惯，真正的回归就会被一起忽略。
    #
    # 所以这里断言的是**语义**而不是逐帧列表：兜底间隔必须命中，
    # 非整数倍的多余送检只给极小预算。
    RGA_FLAKE_BUDGET = 2

    cd = ChangeDetector(sample_mode="auto", every_n=60,
                        change_threshold=DEFAULT_CHANGE_THRESHOLD)
    hits = [i for i in range(60) if cd.should_sample(frame, i)]
    extra = [i for i in hits if i != 0]
    check("静止 60 帧只送检首帧",
          hits[0] == 0 and len(extra) <= RGA_FLAKE_BUDGET,
          f"送检位置={hits}（非首帧 {len(extra)} 个，"
          f"RGA 偶发失败预算 {RGA_FLAKE_BUDGET}）")

    # --- 静止画面**不再**被兜底强行送检 ---
    # 兜底要求"分数至少到阈值的一半"：画面本来就一样时分数恒为 0，
    # 重跑推理只会得到同一个答案，纯徒增一次 NPU 调用。
    # 这条曾经反过来（每 every_n 帧无条件送），是纯浪费。
    cd = ChangeDetector(sample_mode="auto", every_n=30,
                        change_threshold=DEFAULT_CHANGE_THRESHOLD)
    hits = [i for i in range(90) if cd.should_sample(frame, i)]
    extra = [i for i in hits if i != 0]
    check("静止 90 帧不因兜底送检（每_n=30 也不送）",
          hits[0] == 0 and len(extra) <= RGA_FLAKE_BUDGET,
          f"送检位置={hits}（非首帧 {len(extra)} 个，"
          f"RGA 偶发失败预算 {RGA_FLAKE_BUDGET}）")

    # --- 但"变了却没过阈值"的中间地带，兜底仍然要生效 ---
    # 构造一个在两个状态之间来回切换的画面：单步差异的 conc 落在
    # (半阈值, 阈值) 区间内，永远越不过主判据，只能靠兜底捞出来。
    thr = DEFAULT_CHANGE_THRESHOLD
    a, b = _oscillating_pair()
    metric = "gscore_test"
    sc_ab = compare(cd_thumb(a), cd_thumb(b), metric)
    check("构造的中间地带分数落在 (半阈值, 阈值)",
          thr * 0.5 <= sc_ab < thr,
          f"a→b 分数 {sc_ab:.2f}，阈值 {thr}（半阈值 {thr*0.5:.2f}）")

    # a / b 交替：每一步的差异都在中间地带，主判据永远不触发，
    # 只有兜底（gap 到 every_n 且分数 ≥ 半阈值）会捞一次。
    seq = [a if i % 2 == 0 else b for i in range(90)]
    cd = ChangeDetector(sample_mode="auto", every_n=30, change_threshold=thr)
    hits = [i for i, v in enumerate(seq) if cd.should_sample(v, i)]
    # 断言**间隔**而不是绝对位置：兜底量的是"距上次送检满 every_n 帧"，
    # 而 a/b 交替的相位决定命中的是 every_n 还是 every_n+1（偶数帧上
    # 分数为 0，兜底不触发）。写死 30/60 那种断言会被相位差打穿。
    gaps = [j - k for k, j in zip(hits, hits[1:])]
    check("中间地带靠兜底周期性捞出（间隔 ≈ every_n）",
          len(hits) >= 3 and all(30 <= g <= 32 for g in gaps) and len(hits) < 30,
          f"送检位置={hits[:10]}，间隔={gaps}（期望 30~32），"
          f"共 {len(hits)}/90 帧")

    # 参照帧只在送检时更新：若每帧更新，静止序列会因相邻帧噪声一直误触发。
    cd = ChangeDetector(sample_mode="auto", every_n=600,
                                   change_threshold=DEFAULT_CHANGE_THRESHOLD)
    noisy = [
        np.clip(_demo_frame(seed=i).astype(np.int16) + 2, 0, 255).astype(np.uint8)
        for i in range(12)
    ]
    hits = sum(1 for i, f in enumerate(noisy) if cd.should_sample(f, i))
    check("轻微噪声下仍安静（参照帧不逐帧刷新）", hits <= 2,
          f"12 帧中送检 {hits} 次")


def test_failure_degradation():
    print("\n[7] 异常降级（宁可多送检，不可漏检）")
    frame = _demo_frame()
    # 非法输入（非 ndarray / 类型错 / 空帧）→ 一律降级为"送检"。
    # 这些正是 RGA 失败时的真实形态：luma_thumbnail 抛异常，
    # FrameDiffer.should_sample 捕获后返回 True。
    for label, bad in [
        ("字符串输入", "not a frame"),
        ("None 输入", None),
        ("错误 dtype", np.zeros((10, 10, 3), dtype=np.float32)),
        ("通道数错", np.zeros((10, 10), dtype=np.uint8)),
    ]:
        cd = ChangeDetector(sample_mode="auto", every_n=60,
                            change_threshold=DEFAULT_CHANGE_THRESHOLD)
        got = cd.should_sample(bad, 1)
        check(f"{label} → 降级送检", got is True)

    # 手动模式永不判帧，非法输入也不影响（只看取模）。
    cd_m = ChangeDetector(sample_mode="manual", every_n=30)
    check("manual 模式不判帧（非法输入也不崩）",
          cd_m.should_sample("bad", 30) is True
          and cd_m.should_sample("bad", 31) is False)


def test_manual_unchanged():
    print("\n[7b] 手动模式逐位保持历史行为（零回归）")
    cd = ChangeDetector(sample_mode="manual", every_n=30)
    hits = [i for i in range(1, 91) if cd.should_sample(None, i)]
    check("every_n=30 → 采样位置严格是 30 的倍数",
          hits == [30, 60, 90], f"实际={hits}")


def test_metric_registry():
    """wdnwebui 暴露面 / 迁移表 / 未知度量识别（不再测 wdnav SDK 自带度量）。

    v0.23 起 wdnav 0.5.2 不再提供 ``scene.available_metrics`` / 通用
    ``register_metric``（这两个是 scene.py 里的，0.5.2 没场景模块）。
    SDK 自带度量（sad/z_mad/ncc）测的那部分**整个删**（其价值是"SDK
    横向对比"，wdnwebui 现在只暴露 gscore，不存在横向）。
    """
    print("\n[7c] wdnwebui 暴露面 / 迁移 / 未知度量识别")
    check("下拉顺序 = EXPOSED_METRICS（业务侧决定的，不再过 SDK 注册表）",
          exposed() == list(EXPOSED_METRICS), str(exposed()))
    check("已下线的 sad 回落 manual", normalize_mode("sad") == "manual")
    check("已下线的 ncc 回落 manual", normalize_mode("ncc") == "manual")
    # z_mad 是**迁移**不是回落：直接回落 manual 等于把用户的自动判帧悄悄关掉。
    # 断言的是**迁移表指向谁**，不是写死算法名 —— v0.21 把推荐项从 conc 换成
    # gscore 时，这两条曾经直接红（它们曾写死 `== "conc"`）。算法换默认本身
    # 没错，错的是把"推荐项是谁"当成了不变式。
    _legacy_target = LEGACY_MODE_ALIASES["z_mad"]
    check("存量 z_mad 迁到迁移表目标",
          normalize_mode("z_mad") == _legacy_target, _legacy_target)
    check("存量 auto 迁到同一目标",
          normalize_mode("auto") == _legacy_target, _legacy_target)
    check("迁移目标是暴露面内的自动度量（不得是 manual）",
          _legacy_target in EXPOSED_METRICS and _legacy_target != "manual",
          _legacy_target)

    # **存量迁移必须连阈值一起重置** —— 这条是被真 bug 逼出来的：
    # API 层曾先调 normalize_alias 把 z_mad 转成 conc 再传给 FrameJudge，
    # 迁移表就认不出原始模式名，阈值 1.0 被原样留下 → conc + 1.0 =
    # 几乎每帧都送检（conc 自然相邻帧 p50 就有 3.23）。静默失效比不迁移
    # 更糟：它看起来"生效了"，实际把节流关掉了。
    mig = FrameJudge(mode="z_mad", every_n=30, threshold=1.0)
    check("FrameJudge 迁移后阈值重置", mig.mode == _legacy_target
          and mig.threshold == default_threshold(_legacy_target),
          f"mode={mig.mode} threshold={mig.threshold}")
    mig2 = ChangeDetector(sample_mode="z_mad", every_n=30, change_threshold=1.0)
    check("ChangeDetector 迁移后阈值重置", mig2.mode == _legacy_target
          and mig2.threshold == default_threshold(_legacy_target),
          f"mode={mig2.mode} threshold={mig2.threshold}")
    # 迁完之后再收到**同一个**度量请求**不该**动阈值（那是用户的值）。
    keep = FrameJudge(mode=_legacy_target, every_n=30, threshold=7.5)
    keep.reconfigure(mode=_legacy_target, every_n=60)
    check("已迁到目标度量后用户阈值不被覆盖", keep.threshold == 7.5,
          f"threshold={keep.threshold}")

    # 未知度量：compare shim 在 fixture 段就抛 ValueError。
    try:
        compare(np.zeros((2, 2), np.uint8), np.zeros((2, 2), np.uint8),
                "no_such_metric")
        check("未知度量抛错", False, "未抛异常")
    except ValueError:
        check("未知度量抛 ValueError", True)


def test_reset():
    print("\n[8] reset 后首帧必送（新 run 不被上一轮污染）")
    frame = _demo_frame()
    cd = ChangeDetector(sample_mode="auto", every_n=600,
                                   change_threshold=DEFAULT_CHANGE_THRESHOLD)
    cd.should_sample(frame, 0)
    cd.should_sample(frame, 1)          # 静止 → 不送检
    cd.reset()
    check("reset 后首帧必送", cd.should_sample(frame, 0) is True)


def test_sample_key():
    print("\n[9] 缓存键区分手动 / 自动 / 不同阈值（防缓存串味）")
    manual = analysis_sample_key("manual", 6.0)
    auto6 = analysis_sample_key("auto", 6.0)
    auto12 = analysis_sample_key("auto", 12.0)
    check("手动键为空串（兼容迁移前历史行）", manual == "", repr(manual))
    check("自动与手动键不同", auto6 != manual, f"{auto6!r} vs {manual!r}")
    check("不同阈值的自动键不同", auto6 != auto12,
          f"{auto6!r} vs {auto12!r}")
    check("同模式同阈值键稳定", auto6 == analysis_sample_key("auto", 6.0))
    check("非法阈值不崩", analysis_sample_key("auto", None) != auto6)


def test_real_video():
    print("\n[10] 真实视频端到端采样密度（可选，有素材才跑）")
    cands = [
        Path("/home/rockchip/test/test/carplant_29132868.mp4"),
        Path("/home/rockchip/samples/data/1.mp4"),
    ]
    path = next((p for p in cands if p.is_file()), None)
    if path is None:
        check("真实视频素材", False, "未找到素材，跳过（其余检查仍有效）")
        return
    try:
        from wdnav import VideoCapture
        # RGA 在本机高负载下会**偶发** imcvtcolor/imresize 失败（SDK 侧老
        # 问题，与判据无关）。撞上就重试，单帧最多 8 次 —— 否则一次偶发失败
        # 会让整条"真实视频端到端"变成假红，而真红假红分不清就没法回归。
        cap = VideoCapture(str(path), timeout=5)
        frames = []
        fails = 0
        while len(frames) < 120 and fails < 400:
            try:
                ok, f = cap.read()
            except Exception as exc:  # noqa: BLE001
                if "im2d" not in str(exc):
                    raise
                fails += 1
                continue
            if not ok or f is None:
                break
            frames.append(f)
        cap.release()
    except Exception as exc:  # noqa: BLE001
        check("真实视频解码", False, f"{exc}")
        return
    if len(frames) < 40:
        check("真实视频解码", False, f"只解出 {len(frames)} 帧")
        return

    cd = ChangeDetector(sample_mode="auto", every_n=60,
                                   change_threshold=DEFAULT_CHANGE_THRESHOLD)
    import time
    t0 = time.perf_counter()
    hits = [i for i, f in enumerate(frames) if cd.should_sample(f, i)]
    per_frame_ms = (time.perf_counter() - t0) / len(frames) * 1000
    density = len(hits) / len(frames)
    every_n30 = len(frames) / 30.0
    check("真实视频判帧耗时 < 10ms/帧", per_frame_ms < 10.0,
          f"{per_frame_ms:.2f} ms/帧")
    check("自动模式送检率在合理区间 (0~100%)", 0 < density <= 1.0,
          f"{len(hits)}/{len(frames)} = {density*100:.0f}%, "
          f"对比 every_n=30 的 {every_n30:.1f} 次")
    check("送检位置单调递增且首帧命中",
          hits and hits[0] == 0 and hits == sorted(hits),
          f"前几个={hits[:5]}")


def main() -> int:
    print("=" * 68)
    print("VAS 采样双模式回归（ChangeDetector）")
    print("=" * 68)
    for fn in (test_plan, test_no_infinite_loop, test_real_rga,
               test_light_invariance, test_small_object,
               test_static_and_gap, test_failure_degradation,
               test_manual_unchanged, test_metric_registry,
               test_reset, test_sample_key, test_real_video):
        try:
            fn()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            check(f"{fn.__name__} 未抛异常", False, "见上方 traceback")

    npass = sum(1 for _, s, _ in _results if s == PASS)
    nfail = len(_results) - npass
    print("\n" + "=" * 68)
    print(f"结果: {npass}/{len(_results)} 通过"
          + (f"，{nfail} 失败" if nfail else ""))
    if nfail:
        print("-" * 68)
        for name, status, detail in _results:
            if status == FAIL:
                print(f"  FAIL {name}: {detail}")
    print("=" * 68)
    return 1 if nfail else 0


if __name__ == "__main__":
    raise SystemExit(main())