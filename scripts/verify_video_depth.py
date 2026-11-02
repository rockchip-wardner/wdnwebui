"""回归防护：视频实时深度估计（video-depth）链路的单元测试。

用法::

    python -m scripts.verify_video_depth          # 不占 NPU（默认）
    python -m scripts.verify_video_depth --npu    # 占 NPU（真机跑一次 DA3）

不占 NPU 的部分覆盖的是**最容易悄悄坏掉、又最难一眼看出来**的几处：

1. **turbo 调色板与上游逐位一致**
   宿主**不能** import ``wdnai.backends.depth_anything``（它传递性 import
   ``rknn3lite``，宿主进程碰 NPU runtime 是越界的），所以调色板是宿主
   自己实现的一份。这里**从上游源码文本里解析多项式系数**再逐项比对 ——
   上游文件读得到、但**不 import**（import 就会拖起 rknn3lite）。

2. **深度图的显示量方向**
   C++ demo 着色的是 ``1/depth``，**越大越近**（红端）。方向搞反了画面
   不会报错，只会把近处画成远处 —— 所以这里钉死。

3. **重采样发生在深度数组层，不是 JPEG 层**
   先出 280×280 的 JPEG 再缩图 = 有损压缩跑两遍 + 缩的是颜色不是深度值。

4. **DepthDetector 的 duck-type 契约**
   ``multi_api`` / ``multi_session`` 只按鸭子类型调用它，缺一个方法就是
   运行期才炸。

5. **stop() 之后不留悬挂状态**
   ``stop()`` 里 ``self._slots = []`` 写在 ``for slot in list(self._slots)``
   之前，那段循环就是空操作；再加上推理线程按 idx 去 ``self._slots[idx]``
   取对象，``stop()`` 撞上取的那一瞬间就是 IndexError。

6. **start/stop 反复切换不泄漏线程**
   退出判据不能是 Event 的**当前值**：``start()`` 换一个新 Event 的话，
   还没退干净的旧线程读到的是新的（未置位）Event —— 永远退不出去。

7. **卡片契约**（插件仓）
   entry point 名 == manifest id；``wizard.json`` 的 ``canvas_id`` /
   ``open_source_button`` / ``start_buttons`` 都指向 template.html 里
   **真实存在**的 id；多路源字段是 ``textarea`` 且 id 是**复数**；
   app.js 里 ``getElementById`` 用到的 id 在 template.html 里都有。

第 7 条特别值钱：卡片模板与前端 JS 是**两份**文件，改了模板忘了改 JS
（或反过来）时，页面**不报错**，只是某个按钮点了没反应。
"""
from __future__ import annotations

import argparse
import ast
import base64
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, ".")

import numpy as np  # noqa: E402

FAILS: List[str] = []
PASSES: List[str] = []


def ok(msg: str) -> None:
    PASSES.append(msg)
    print(f"  ✅ {msg}")


def fail(msg: str) -> None:
    FAILS.append(msg)
    print(f"  ❌ {msg}")


def check(cond: bool, msg: str, detail: str = "") -> bool:
    if cond:
        ok(msg)
    else:
        fail(msg + (f"\n     {detail}" if detail else ""))
    return bool(cond)


# --------------------------------------------------------------------------- #
# 工具                                                                       #
# --------------------------------------------------------------------------- #
PLUGIN_PKG = Path(".venv/src/wdnapps-video-analyzer/src/wdnapps_video_analyzer")
CARD_DIR = PLUGIN_PKG / "cards" / "video_depth"
WDNAI_DEPTH_SRC = (Path(".venv/src/wdnai/src/wdnai/backends/depth_anything.py"))


def depth_payload(depth: np.ndarray, *, views: int = 1) -> str:
    """构造一段与 ``DepthResult.text`` **同构**的 JSON（worker 回的东西）。

    结构对着 ``wdnai.backends.depth_anything.DepthResult.text`` 抄：
    ``views`` / ``height`` / ``width`` + fp16 base64 的 ``depth_b64``。
    """
    d = np.asarray(depth, dtype=np.float32)
    if d.ndim == 2:
        d = d[None, ...]
    h, w = d.shape[-2], d.shape[-1]
    b64 = base64.b64encode(
        np.ascontiguousarray(d, dtype=np.float16).tobytes()).decode("ascii")
    return json.dumps({
        "model": "DepthAnythingV3",
        "views": int(views),
        "height": int(h),
        "width": int(w),
        "depth_b64": b64,
        "include_maps": True,
    })


class FakeDepthSession:
    """假 session —— 顶替 registry 给的 ``LLMRPCSession``。

    只实现 ``respond()``（深度链路的唯一入口）。它会**记录**收到的
    prompt / attachment / overrides —— 这样能断言「深度估计确实把
    ``include_maps=True`` 透传下去了」（漏了这个参数，worker 回的
    JSON 里就没有 ``depth_b64``，主进程只能拿到一句统计摘要）。
    """

    def __init__(self, depth: Optional[np.ndarray] = None,
                 delay: float = 0.0) -> None:
        self.depth = depth if depth is not None else np.linspace(
            0.5, 1.5, 28 * 28, dtype=np.float32).reshape(28, 28)
        self.delay = delay
        self.calls = 0
        self.last_prompt: Optional[str] = None
        self.last_overrides: Dict[str, Any] = {}
        self.last_attachment: Any = None

    async def respond(self, prompt=None, attachment=None, **overrides):
        self.calls += 1
        self.last_prompt = prompt
        self.last_overrides = dict(overrides)
        self.last_attachment = attachment
        if self.delay:
            time.sleep(self.delay)
        return depth_payload(self.depth)


class FakeRegistry:
    """假 ``ComputeRegistry`` —— 只实现深度 detector 用到的两个方法。"""

    def __init__(self, sessions: List[FakeDepthSession]) -> None:
        self.sessions = sessions

    def get_session(self, capability_type, npu_index=None):
        if not self.sessions:
            raise RuntimeError(f"功能未启用: {capability_type}")
        return self.sessions[0]

    def get_session_for_model(self, capability_type, model_id, npu_index=None):
        for s in self.sessions:
            if getattr(s, "model_id", model_id) == model_id:
                return s
        if not self.sessions:
            raise RuntimeError(f"功能未启用: {capability_type}")
        return self.sessions[0]

    def pending_models_for_capability(self, capability_type):
        return []


def install_fake_registry(sessions: List[FakeDepthSession]) -> None:
    """把假 registry 塞进 ``app.core.compute.registry``。

    ``DepthDetector.start()`` 是**函数内 import** ``get_registry``，所以
    替换模块属性即可生效（不需要改 detector 的代码路径）。
    """
    import app.core.compute.registry as reg
    reg.get_registry = lambda: FakeRegistry(sessions)  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# 1) turbo 调色板：与上游逐位一致                                             #
# --------------------------------------------------------------------------- #
CHANNELS = ("red", "green", "blue")


def _channel_exprs(path: Path) -> Dict[str, str]:
    """从 Python 源码里抽出 turbo 三个通道的多项式表达式（**不执行**源码）。

    用 ``ast`` 而不是正则：正则扛不住「系数换行 + 续行反斜杠 + 括号包裹」
    这几种排版差异（上游是一行长表达式，本地是拆行 + 外层括号），而
    ``ast`` 拿到的是语法树里的表达式，与排版无关。
    """
    if not path.is_file():
        return {}
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}
    out: Dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1:
            continue
        tgt = node.targets[0]
        if not (isinstance(tgt, ast.Name) and tgt.id in CHANNELS):
            continue
        # 用 ``ast.unparse`` 而不是 ``ast.get_source_segment``：后者取的是
        # **原始文本**（本地那份是拆行 + 外层括号，括号又不属于表达式节点
        # → 取出来的片段没有括号，eval 会报 "unexpected indent"）。
        # unparse 给出归一化的单行表达式，与排版无关。
        try:
            out.setdefault(tgt.id, ast.unparse(node.value))
        except Exception:  # noqa: BLE001
            continue
    return out


def test_turbo() -> None:
    print("\n[1] turbo 调色板")
    import app.core.depth.palette as pal
    from app.core.depth.palette import turbo_rgb

    up = _channel_exprs(WDNAI_DEPTH_SRC)
    mine = _channel_exprs(Path(pal.__file__))
    if not check(set(up) == set(CHANNELS),
                 "能从上游源码解析出 turbo 三个通道的表达式",
                 f"实际 {sorted(up)}"):
        return
    check(set(mine) == set(CHANNELS),
          "能从本地源码解析出 turbo 三个通道的表达式",
          f"实际 {sorted(mine)}")

    # **直接求值上游的表达式**，与本地实现逐位比。
    # 两侧都用同一套环境（x2..x5 = x 的幂），因此比的是「多项式本身」，
    # 不是「谁写得更像谁」。
    grid = np.linspace(0.0, 1.0, 257, dtype=np.float32)
    env = {"np": np, "x": grid,
           "x2": grid * grid, "x3": grid ** 3, "x4": grid ** 4,
           "x5": grid ** 5}
    for chan in CHANNELS:
        if chan not in mine:
            fail(f"本地源码里没有 {chan} 表达式")
            continue
        try:
            up_vals = np.asarray(eval(up[chan], dict(env)), dtype=np.float32)
            my_vals = np.asarray(eval(mine[chan], dict(env)), dtype=np.float32)
        except Exception as e:  # noqa: BLE001
            fail(f"求值 {chan} 表达式失败: {e}")
            continue
        if up_vals.shape != my_vals.shape:
            fail(f"{chan} 表达式形状不同: {up_vals.shape} vs {my_vals.shape}")
            continue
        diff = float(np.max(np.abs(up_vals - my_vals)))
        check(diff <= 1e-6,
              f"{chan} 多项式与上游**逐位**相同（最大差 {diff:.2e}）")

    # 端到端：上游表达式 → 上色 → 与本实现的 uint8 输出逐位比
    up_rgb = np.stack([
        np.clip(np.asarray(eval(up[c], dict(env)), dtype=np.float32), 0.0, 1.0)
        for c in CHANNELS], axis=-1)
    up_rgb = (up_rgb * 255.0 + 0.5).astype(np.uint8)
    my_rgb = turbo_rgb(grid)
    same = bool(np.array_equal(up_rgb, my_rgb))
    check(same, "整条着色链路（多项式→clamp→舍入）与上游逐字节相同",
          f"不同像素数 {int((up_rgb != my_rgb).any(axis=-1).sum())}")

    # 锚点值（与 C++ demo 的 turbo_color 逐通道对拍过，最大差 0）
    anchors = {
        0.00: (35, 23, 27),
        0.25: (38, 189, 225),
        0.50: (150, 250, 80),
        0.75: (255, 128, 29),
        1.00: (144, 13, 0),
    }
    for x, want in anchors.items():
        got = tuple(int(v) for v in turbo_rgb(np.array([x], dtype=np.float32))[0])
        check(got == want, f"turbo({x:.2f}) == {want}", f"实际 {got}")


# --------------------------------------------------------------------------- #
# 2) 显示量方向 + 色标窗口                                                     #
# --------------------------------------------------------------------------- #
def test_display_semantics() -> None:
    print("\n[2] 显示量方向与色标窗口")
    from app.core.depth.palette import (
        DepthWindow, inverse_depth, percentile_window, turbo_rgb,
    )

    # 1/depth：越大越近。深度 0.5（近）→ 显示量 2.0；深度 2.0（远）→ 0.5。
    inv = inverse_depth(np.array([0.5, 2.0, 0.0, -1.0, np.nan, np.inf],
                                 dtype=np.float32))
    check(abs(float(inv[0]) - 2.0) < 1e-6, "inverse_depth(0.5) == 2.0",
          f"实际 {inv[0]}")
    check(abs(float(inv[1]) - 0.5) < 1e-6, "inverse_depth(2.0) == 0.5",
          f"实际 {inv[1]}")
    check(inv[2] == 0.0 and inv[3] == 0.0,
          "无效深度（0 / 负）→ 显示量 0", f"实际 {inv[2:4]}")
    check(np.isnan(inv[4]) or inv[4] == 0.0,
          "NaN 深度不产生有效显示量", f"实际 {inv[4]}")
    check(inv[5] == 0.0 or np.isinf(inv[5]),
          "inf 深度不产生有限显示量", f"实际 {inv[5]}")

    # 色标：显示量大的（近）→ 颜色更「红」；显示量小的（远）→ 更「蓝」。
    near = turbo_rgb(np.array([1.0], dtype=np.float32))[0]
    far = turbo_rgb(np.array([0.0], dtype=np.float32))[0]
    check(int(near[0]) > int(far[0]),
          "近处（显示量大）红色分量更大 —— 方向没搞反",
          f"near={near.tolist()} far={far.tolist()}")

    # 分位窗口
    v = np.linspace(1.0, 2.0, 1000, dtype=np.float32)
    win = percentile_window(v)
    check(win is not None and win[0] < win[1], "percentile_window 给出窗口",
          f"实际 {win}")
    if win:
        check(abs(win[0] - 1.02) < 0.02 and abs(win[1] - 1.98) < 0.02,
              "窗口取 2% / 98% 分位", f"实际 {win}")
    check(percentile_window(np.ones(5, dtype=np.float32)) is None,
          "有效像素不足 10 个 → 无窗口")
    check(percentile_window(np.zeros(100, dtype=np.float32)) is None,
          "全零（无有效像素）→ 无窗口")

    # DepthWindow：每路 EMA + 跨路并集
    a = np.full((10, 10), 1.0, dtype=np.float32)
    b = np.full((10, 10), 3.0, dtype=np.float32)
    w = DepthWindow(shared=True)
    wa = w.update(0, a)
    wb = w.update(1, b)
    check(wa is not None and wb is not None, "两路都能拿到窗口")
    if wa and wb:
        # 共享模式：第 2 路更新后窗口是**并集**（下界取第 1 路，上界取第 2 路）
        check(abs(wb[0] - wa[0]) < 1e-3 and abs(wb[1] - 3.0) < 1e-3,
              "共享模式下窗口是跨路并集",
              f"第 1 路后 {wa} → 第 2 路后 {wb}")

    # 独立模式：每路用自己的窗口（值不同 → 窗口不同）
    w2 = DepthWindow(shared=False)
    w2.update(0, a)
    w2.update(1, b)
    per0 = w2.window(0)
    per1 = w2.window(1)
    check(per0 is not None and per1 is not None,
          "独立模式下每路都能查到窗口")
    if per0 and per1:
        check(abs(per0[0] - 1.0) < 1e-3 and abs(per1[0] - 3.0) < 1e-3,
              "独立模式下每路用自己的窗口（不并集）", f"{per0} vs {per1}")

    # 退化输入（整幅同深度）→ 窗口 hi == lo 时必须留出一点跨度，
    # 否则归一化会除零。
    w3 = DepthWindow(shared=True)
    win3 = w3.update(0, a)
    check(win3 is not None and win3[1] > win3[0],
          "恒定深度时窗口仍有非零跨度（归一化不会除零）", f"实际 {win3}")


# --------------------------------------------------------------------------- #
# 3) render_depth_jpeg                                                        #
# --------------------------------------------------------------------------- #
def test_render() -> None:
    print("\n[3] 热力图渲染")
    import cv2
    from app.core.depth.palette import render_depth_jpeg

    depth = np.full((28, 28), 1.0, dtype=np.float32)
    depth[:14, :] = 0.5          # 上半近
    depth[0, 0] = 0.0            # 一个无效像素

    jpeg, stats = render_depth_jpeg(depth, 0.5, 2.0)
    check(jpeg[:2] == b"\xff\xd8", "输出是合法 JPEG（SOI 标记）")
    arr = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    check(arr is not None and arr.shape[:2] == (28, 28),
          "默认按原始深度尺寸出图",
          f"实际 {None if arr is None else arr.shape}")
    if arr is not None:
        # 上半（近，显示量 2.0）应比下半（远，显示量 1.0）更红
        near_b, near_g, near_r = arr[7, 14].tolist()
        far_b, far_g, far_r = arr[21, 14].tolist()
        check(near_r > far_r,
              "近处的像素更红（turbo 红端 = 近）",
              f"near=({near_b},{near_g},{near_r}) far=({far_b},{far_g},{far_r})")

    check(abs(stats["depth_min"] - 0.5) < 1e-6
          and abs(stats["depth_max"] - 1.0) < 1e-6,
          "stats 的 min/max 取**原始数组**", f"实际 {stats}")
    check(stats["valid_px"] == 28 * 28 - 1,
          "valid_px 数对（扣掉 1 个无效像素）", f"实际 {stats['valid_px']}")

    # 无效像素 → 纯黑。
    #
    # **不能查单个像素**：JPEG 是 8×8 DCT 块 + 有损的，边界像素会被邻块
    # 串色（实测那个 0 像素解出来是 (0,0,72) —— 红色从右边渗过来）。
    # 所以用「整幅无效」来判：全黑图解出来必须还是黑。
    black_jpeg, black_stats = render_depth_jpeg(
        np.zeros((32, 32), dtype=np.float32), 0.5, 2.0)
    black = cv2.imdecode(np.frombuffer(black_jpeg, dtype=np.uint8),
                         cv2.IMREAD_COLOR)
    check(black is not None and int(black.max()) <= 2,
          "整幅无效深度 → 整张纯黑（与 C++ memset(rgb,0,3) 一致）",
          f"实际 max={None if black is None else int(black.max())}")
    check(black_stats["valid_px"] == 0, "全无效时 valid_px == 0")

    # 半幅无效：无效区的亮度必须显著低于有效区
    half = np.full((32, 32), 1.0, dtype=np.float32)
    half[:, :16] = 0.0
    half_jpeg, _ = render_depth_jpeg(half, 0.5, 2.0)
    half_img = cv2.imdecode(np.frombuffer(half_jpeg, dtype=np.uint8),
                            cv2.IMREAD_COLOR)
    if half_img is not None:
        dark = float(half_img[:, :16].mean())
        bright = float(half_img[:, 16:].mean())
        check(dark < bright / 2.0,
              "无效区显著暗于有效区",
              f"无效区均值 {dark:.1f} vs 有效区均值 {bright:.1f}")

    # out_size：在**数组层**重采样
    jpeg2, stats2 = render_depth_jpeg(depth, 0.5, 2.0, out_size=(56, 28))
    arr2 = cv2.imdecode(np.frombuffer(jpeg2, dtype=np.uint8), cv2.IMREAD_COLOR)
    check(arr2 is not None and arr2.shape[:2] == (28, 56),
          "out_size=(w,h) 生效（输出宽高对调）",
          f"实际 {None if arr2 is None else arr2.shape}")
    check(stats2["depth_min"] == stats["depth_min"],
          "重采样不改变 stats（取的是原始数组）")


# --------------------------------------------------------------------------- #
# 4) 深度负载解析                                                             #
# --------------------------------------------------------------------------- #
def test_payload_decode() -> None:
    print("\n[4] worker 负载解析")
    from app.core.depth.depth_detector import DepthDetector

    d = np.linspace(0.5, 1.5, 20 * 30, dtype=np.float32).reshape(20, 30)
    arr = DepthDetector._decode_depth_payload(depth_payload(d))
    check(arr is not None and arr.shape == (20, 30),
          "单 view：解出 (H, W) 深度数组",
          f"实际 {None if arr is None else arr.shape}")
    if arr is not None:
        check(abs(float(arr.mean()) - float(d.mean())) < 1e-2,
              "fp16 往返后均值基本不变",
              f"{arr.mean()} vs {d.mean()}")

    # 多 view：只取第 0 张（深度卡是单 view，但容错要有）
    dv = np.stack([d, d * 2.0], axis=0)
    arr2 = DepthDetector._decode_depth_payload(depth_payload(dv, views=2))
    check(arr2 is not None and arr2.shape == (20, 30),
          "多 view 时取第 0 张", f"实际 {None if arr2 is None else arr2.shape}")

    check(DepthDetector._decode_depth_payload("not json") is None,
          "非 JSON → None")
    check(DepthDetector._decode_depth_payload(json.dumps({"views": 1})) is None,
          "没有 depth_b64 → None")
    check(DepthDetector._decode_depth_payload(None) is None,
          "非字符串 → None")

    # 尺寸不符必须报错（不能静默解出一张错图）
    bad = json.loads(depth_payload(d))
    bad["width"] = bad["width"] + 1
    try:
        DepthDetector._decode_depth_payload(json.dumps(bad))
        fail("尺寸不符时应抛 ValueError")
    except ValueError:
        ok("尺寸不符时抛 ValueError（不会静默解出错图）")


# --------------------------------------------------------------------------- #
# 5) DepthDetector 的 duck-type 契约                                           #
# --------------------------------------------------------------------------- #
DUCK_METHODS = (
    "slot_count", "start", "stop", "snapshot", "events", "events_since",
    "latest", "clear", "diag_history", "clear_diagnostics",
    "register_broadcaster", "unregister_broadcaster", "submit_frame",
    "submit_frame_to", "wait_idle", "wait_slot_idle",
)


def test_duck_contract() -> None:
    print("\n[5] DepthDetector duck-type 契约")
    from app.core.depth import DepthDetector, get_depth_detector

    det = DepthDetector()
    missing = [m for m in DUCK_METHODS if not hasattr(det, m)]
    check(not missing, "multi_api / multi_session 需要的成员齐全",
          f"缺: {missing}")

    # 单例
    check(get_depth_detector() is get_depth_detector(), "detector 是进程级单例")

    # snapshot 的字段名必须与 WdnaiDetector 对齐（前端共享组件按那套读）
    snap = det.snapshot()
    for key in ("available", "init_error", "enabled", "session_ready",
                "model_loading", "slot_count", "queue_size", "slots"):
        check(key in snap, f"snapshot 含字段 {key!r}")


# --------------------------------------------------------------------------- #
# 6) 推理生命周期（假 session，不占 NPU）                                       #
# --------------------------------------------------------------------------- #
def test_detector_lifecycle() -> None:
    print("\n[6] DepthDetector 生命周期（假 session）")
    from app.core.depth.depth_detector import DepthDetector

    sess = FakeDepthSession()
    install_fake_registry([sess])

    det = DepthDetector()
    det.start(models=[{"model_id": "DepthAnythingV3", "npu_index": 0}])
    check(det.slot_count == 1, "start 后 slot_count == 1",
          f"实际 {det.slot_count}")
    check(det.enabled, "start 后 enabled")

    # 送检帧：288×157（16:9 源按长边 280 缩出来 + 16 字节对齐）
    frame = np.zeros((157, 288, 3), dtype=np.uint8)
    ok_submit = det.submit_frame_to(0, frame, 42, frame_b64="",
                                     judge_score=None, channel=0)
    check(ok_submit, "submit_frame_to 接受了一帧")
    check(det.wait_slot_idle(0, timeout=5.0), "wait_slot_idle 在超时前返回")
    check(sess.calls == 1, "假 session 被调用了一次", f"实际 {sess.calls}")

    # 关键：include_maps 必须透传下去（漏了就没有 depth_b64）
    check(sess.last_overrides.get("include_maps") is True,
          "respond() 收到了 include_maps=True",
          f"实际 overrides={sess.last_overrides}")
    check(sess.last_prompt == "", "prompt 是空串（深度估计没有文本入口）",
          f"实际 {sess.last_prompt!r}")
    check(sess.last_attachment is not None
          and len(sess.last_attachment) == 1,
          "带了一张图片附件", f"实际 {sess.last_attachment}")

    evs = det.events()
    check(len(evs) == 1, "产生了 1 条深度事件", f"实际 {len(evs)}")
    if evs:
        ev = evs[0]
        check(ev["channel"] == 0, "事件的 channel 正确", f"实际 {ev['channel']}")
        check(ev["frame_pos"] == 42, "事件的 frame_pos 透传", f"实际 {ev['frame_pos']}")
        check(ev.get("depth_b64"), "事件带 depth_b64")
        # 深度图按**送检帧的宽高比**回拉：长边 560，短边按比例
        # （288×157 → 560×round(157*560/288)=560×305）。
        exp_w = 560
        exp_h = max(16, int(round(157 * 560 / 288)))
        check(ev["width"] == exp_w and ev["height"] == exp_h,
              f"深度图按送检帧宽高比回拉（288×157 → {exp_w}×{exp_h}）",
              f"实际 {ev['width']}×{ev['height']}")
        import cv2
        raw = base64.b64decode(ev["depth_b64"])
        img = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        check(img is not None and img.shape[1] == exp_w and img.shape[0] == exp_h,
              f"depth_b64 是一张 {exp_w}×{exp_h} 的合法 JPEG",
              f"实际 {None if img is None else img.shape}")
        check(ev.get("stats", {}).get("depth_min") is not None,
              "事件带深度统计")

    # wait_idle
    check(det.wait_idle(timeout=1.0), "wait_idle 返回 True")

    # stop 之后：slot 清空、等待者立刻返回
    det.stop()
    check(det.slot_count == 0, "stop 后 slot_count == 0",
          f"实际 {det.slot_count}")
    check(not det.enabled, "stop 后 enabled == False")
    t0 = time.monotonic()
    check(det.wait_slot_idle(0, timeout=30.0), "stop 后 wait_slot_idle 立刻返回")
    check(time.monotonic() - t0 < 0.5, "stop 后 wait_slot_idle 没有等满超时",
          f"耗时 {time.monotonic() - t0:.3f}s")
    check(det.wait_idle(timeout=30.0), "stop 后 wait_idle 立刻返回")


def test_detector_multichannel() -> None:
    print("\n[7] 多路：通道隔离 + 共享色标")
    from app.core.depth.depth_detector import DepthDetector

    near = np.full((20, 20), 0.6, dtype=np.float32)
    far = np.full((20, 20), 1.6, dtype=np.float32)
    s0 = FakeDepthSession(near)
    s1 = FakeDepthSession(far)
    s0.model_id = s1.model_id = "DepthAnythingV3"
    install_fake_registry([s0, s1])

    det = DepthDetector()
    det.start(models=[{"model_id": "DepthAnythingV3", "npu_index": 0},
                      {"model_id": "DepthAnythingV3", "npu_index": 1}])
    check(det.slot_count == 2, "两个实例 → 两个推理 slot",
          f"实际 {det.slot_count}")

    frame = np.zeros((157, 288, 3), dtype=np.uint8)
    det.submit_frame_to(0, frame, 1, channel=0)
    det.submit_frame_to(1, frame, 1, channel=1)
    det.wait_slot_idle(0, timeout=5.0)
    det.wait_slot_idle(1, timeout=5.0)

    evs = det.events()
    check(len(evs) == 2, "两路各产生一条事件", f"实际 {len(evs)}")
    chans = sorted(e["channel"] for e in evs)
    check(chans == [0, 1], "两条事件的 channel 分别是 0 / 1", f"实际 {chans}")
    npus = sorted(e["npu_index"] for e in evs)
    check(npus == [0, 1], "两条事件的 npu_index 分别是 0 / 1", f"实际 {npus}")

    # 共享色标：两路的事件用的是**同一个**窗口（跨路颜色可比）
    wins = {(e["stats"]["inv_lo"], e["stats"]["inv_hi"]) for e in evs}
    check(len(wins) == 1,
          "共享色标窗口下两路用的是同一档窗口", f"实际 {wins}")

    det.stop()


def test_restart_no_leak() -> None:
    print("\n[8] 反复 start/stop 不泄漏线程")
    from app.core.depth.depth_detector import DepthDetector

    sess = FakeDepthSession()
    install_fake_registry([sess])
    det = DepthDetector()

    base = threading.active_count()
    for _ in range(3):
        det.start(models=[{"model_id": "DepthAnythingV3", "npu_index": 0}])
        frame = np.zeros((157, 288, 3), dtype=np.uint8)
        det.submit_frame_to(0, frame, 1, channel=0)
        det.wait_slot_idle(0, timeout=5.0)
        det.stop()
    time.sleep(0.3)
    after = threading.active_count()
    check(after <= base + 1,
          "3 轮 start/stop 后线程数回到基线",
          f"基线 {base} → 现在 {after}")

    # 旧线程必须真的退出（代号判据生效）
    names = [t.name for t in threading.enumerate() if t.name.startswith("depth-infer-")]
    check(not names, "没有残留的 depth-infer-* 线程", f"实际 {names}")


def test_debug_log_switch() -> None:
    print("\n[11] 深度事件日志开关")
    import logging
    import os

    from app.core.depth import DEBUG_ENV, depth_debug_enabled
    from app.core.depth.depth_detector import DepthDetector

    # ---- 1) 环境变量：显式值必须压过配置项 -------------------------------
    os.environ[DEBUG_ENV] = "1"
    check(depth_debug_enabled() is True, "WDNWEBUI_DEPTH_DEBUG=1 → 打开")
    for val in ("true", "yes", "on"):
        os.environ[DEBUG_ENV] = val
        check(depth_debug_enabled() is True,
              f"WDNWEBUI_DEPTH_DEBUG={val} → 打开")
    for val in ("0", "false", "no", "off"):
        os.environ[DEBUG_ENV] = val
        check(depth_debug_enabled() is False,
              f"WDNWEBUI_DEPTH_DEBUG={val} → 关闭")
    os.environ.pop(DEBUG_ENV, None)

    # 未设置时读配置项（config.jsonc 的 debug.depth_event_log）——
    # 本机没配 → 关。不断言「必须关」（配置里打开时本来就该开），
    # 只断言「读得到配置、不抛异常」。
    try:
        v = depth_debug_enabled()
        check(isinstance(v, bool), "未设环境变量时回落读配置项", f"实际 {v!r}")
    except Exception as e:  # noqa: BLE001
        fail(f"读配置项时抛异常: {e}")

    # ---- 2) 开关真的控制日志 ---------------------------------------------
    def _run_and_capture() -> list[str]:
        """跑一次假推理，把 ``core.depth`` 的日志收进 list。"""
        sess = FakeDepthSession()
        install_fake_registry([sess])
        records: list[str] = []
        cap = _ListHandler(records)
        log = logging.getLogger("core.depth")
        log.addHandler(cap)
        old_level = log.level
        log.setLevel(logging.INFO)
        try:
            det = DepthDetector()
            det.start(models=[{"model_id": "DepthAnythingV3", "npu_index": 0}])
            frame = np.zeros((157, 288, 3), dtype=np.uint8)
            det.submit_frame_to(0, frame, 1, channel=0)
            det.wait_slot_idle(0, timeout=5.0)
            det.stop()
        finally:
            log.removeHandler(cap)
            log.setLevel(old_level)
        return records

    os.environ[DEBUG_ENV] = "0"
    off_records = _run_and_capture()
    check(not any("[depth-event]" in m for m in off_records),
          "开关关闭时**不**产生深度事件日志",
          f"实际打了 {sum('[depth-event]' in m for m in off_records)} 条")

    os.environ[DEBUG_ENV] = "1"
    on_records = _run_and_capture()
    hits = [m for m in on_records if "[depth-event]" in m]
    check(bool(hits), "开关打开时深度事件进日志",
          f"实际 {len(hits)} 条")
    if hits:
        # 一条日志要带齐「历史」需要的字段：通道 / 深度范围 / 色标窗口 / 耗时
        h = hits[0]
        need = ("ch=", "depth=[", "mean=", "window=[", "infer=")
        missing = [k for k in need if k not in h]
        check(not missing, "事件日志带齐通道/深度/色标/耗时字段",
              f"缺 {missing}\n     实际: {h}")
    os.environ.pop(DEBUG_ENV, None)


class _ListHandler(logging.Handler):
    """把日志消息收进一个 list（测试用）。"""

    def __init__(self, sink: list) -> None:
        super().__init__()
        self._sink = sink

    def emit(self, record) -> None:  # noqa: ANN001
        try:
            self._sink.append(record.getMessage())
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# 9) 卡片契约（插件仓）                                                       #
# --------------------------------------------------------------------------- #
def test_card_contract() -> None:
    print("\n[9] 卡片契约（插件仓）")
    from importlib.metadata import entry_points

    card_id = "video-depth"
    if not CARD_DIR.is_dir():
        fail(f"卡片目录不存在: {CARD_DIR}")
        return

    # entry point 名 == manifest id
    eps = {e.name: e.value for e in entry_points(group="wdnwebui.cards")}
    check(eps.get(card_id) ==
          "wdnapps_video_analyzer.cards.video_depth",
          "entry point 已注册且指向正确的模块", f"实际 {eps.get(card_id)}")

    manifest = json.loads((CARD_DIR / "manifest.json").read_text(encoding="utf-8"))
    check(manifest.get("id") == card_id, "manifest.id == entry point 名",
          f"实际 {manifest.get('id')}")
    check(manifest.get("software_capabilities") == ["swcap_vision_depth"],
          "声明的软件能力是 swcap_vision_depth",
          f"实际 {manifest.get('software_capabilities')}")

    # **index.html.template 必须有该卡的 card_slot 占位符**。
    #
    # 卡片模板不是运行时 fetch 的 —— 它由 html_assembly 在**拼 index.html
    # 时就内联进去**（占位符协议见 app/core/html_assembly.py 的文件头）。
    # 漏了这一行的话：manifest 能发现、/static/apps/<id>/template.html 也
    # 能 200、API 也全在，但**页面上根本没有这个卡片** —— 前端 `showPage`
    # 遍历 `.page` 元素时找不到它，表现为「点了卡片没反应 / 白屏」。
    idx_tpl = Path("app/static/index.html.template")
    if check(idx_tpl.is_file(), "找到了 index.html.template"):
        idx_src = idx_tpl.read_text(encoding="utf-8")
        check(f'{{% card_slot "{card_id}" %}}' in idx_src,
              f'index.html.template 里有 {{% card_slot "{card_id}" %}} 占位符',
              "漏了 → 卡片模板不会被内联进 index.html，页面上根本没有这张卡")

    template = (CARD_DIR / "static" / "template.html").read_text(encoding="utf-8")
    ids_in_template = set(re.findall(r'id="([^"]+)"', template))

    wizard = json.loads((CARD_DIR / "static" / "wizard.json").read_text(encoding="utf-8"))

    # canvas_id 必须显式声明，且指向真实存在的元素
    cid = wizard.get("canvas_id")
    check(bool(cid), "wizard.json 显式声明了 canvas_id")
    check(cid in ids_in_template, f"canvas_id {cid!r} 在 template.html 里存在",
          f"template 里有: {sorted(ids_in_template)}")

    # 按钮 id
    for key in ("open_source_button",):
        bid = wizard.get(key)
        check(bid in ids_in_template,
              f"{key} = {bid!r} 在 template.html 里存在")
    for bid in wizard.get("start_buttons", []):
        check(bid in ids_in_template,
              f"start_buttons 里的 {bid!r} 在 template.html 里存在")

    # 多路源字段：必须是 textarea 且 id 是复数
    src_fields = []
    for step in wizard.get("steps", []):
        for f in step.get("fields", []) or []:
            if f.get("ui_key") == "source":
                src_fields.append(f)
    check(len(src_fields) == 1, "wizard 里恰好一个 source 字段",
          f"实际 {len(src_fields)}")
    if src_fields:
        f = src_fields[0]
        check(f.get("type") == "textarea",
              "多路源字段必须显式 type=textarea",
              f"实际 {f.get('type')!r}")
        check(f.get("id", "").endswith("sources"),
              "多路源字段 id 用复数（-sources）", f"实际 {f.get('id')!r}")
        check(f.get("id") in ids_in_template,
              f"源字段 id {f.get('id')!r} 在 template.html 里存在")

    # app.js 里 getElementById 的每个 id 都得在模板里
    appjs = (CARD_DIR / "static" / "app.js").read_text(encoding="utf-8")
    used = set(re.findall(r'getElementById\("([^"]+)"\)', appjs))
    missing = sorted(used - ids_in_template)
    check(not missing,
          "app.js 里 getElementById 用到的 id 全在 template.html 里",
          f"缺: {missing}")

    # **UI 上不再有「深度事件历史」** —— 事件历史改走日志（debug 开关）。
    # 这条是回归防护：有人日后「顺手加回来」时这里会红。
    check("vdp-event-list" not in template
          and "深度事件历史" not in template,
          "template.html 里没有「深度事件历史」区段")
    check("vdp-event-list" not in appjs and "vdp-events-clear" not in appjs
          and "renderVdpEventList" not in appjs,
          "app.js 里没有事件列表的残留引用")
    # 后端的历史仍在（供日志与诊断用），只是不再上页面
    from app.core.depth import get_depth_detector
    check(hasattr(get_depth_detector(), "events"),
          "后端仍保留事件历史（日志/诊断用）")

    # 色标图例：**条在上、说明在下**。横排（右）会有歧义。
    css = (CARD_DIR / "static" / "app.css").read_text(encoding="utf-8")
    legend_rule = ""
    if ".vdp-legend {" in css:
        legend_rule = css.split(".vdp-legend {", 1)[1].split("}", 1)[0]
    check("flex-direction: column" in legend_rule,
          ".vdp-legend 是纵向排列（色标条在上、说明在下）",
          f"实际规则: {legend_rule.strip()!r}")
    bar_idx = template.find("vdp-legend-bar")
    text_idx = template.find("vdp-legend-text")
    check(0 <= bar_idx < text_idx,
          "图例 markup 里色标条排在说明文字之前")

    # 前端 JS 语法（node --check）
    if shutil_which("node"):
        try:
            r = subprocess.run(["node", "--check", str(CARD_DIR / "static" / "app.js")],
                               capture_output=True, text=True, timeout=30)
            check(r.returncode == 0, "app.js 通过 node --check（语法正确）",
                  r.stderr[:500])
        except Exception as e:  # noqa: BLE001
            fail(f"node --check 执行失败: {e}")
    else:
        print("  ⚠️ 没有 node，跳过 JS 语法检查")

    # 后端：_parse_prompt 的契约
    from wdnapps_video_analyzer.cards.video_depth.api import _parse_prompt
    from app.core.video.multi_api import PromptPlan

    plan = _parse_prompt({"rr_interval": 0.5,
                          "models": [{"model_id": "DepthAnythingV3",
                                      "npu_index": 0}]})
    check(isinstance(plan, PromptPlan), "_parse_prompt 返回 PromptPlan")
    check(plan.rr_interval == 0.5, "rr_interval 被解析", f"实际 {plan.rr_interval}")
    check(plan.start_kwargs.get("prompt") == "",
          "深度页没有文本 prompt", f"实际 {plan.start_kwargs.get('prompt')!r}")
    check(plan.start_kwargs.get("models") == [
        {"model_id": "DepthAnythingV3", "npu_index": 0}],
        "models 被解析", f"实际 {plan.start_kwargs.get('models')}")

    # every_n 兜底（老前端只有这个字段）
    plan2 = _parse_prompt({"every_n": 3})
    check(plan2.rr_interval == 3.0, "every_n 作为 rr_interval 的兜底",
          f"实际 {plan2.rr_interval}")
    plan3 = _parse_prompt({})
    check(plan3.rr_interval == 0.0, "缺省 rr_interval == 0（轮转跑满）",
          f"实际 {plan3.rr_interval}")

    # 重复实例必须 400（两条线程挤同一个 worker，句柄不可重入）
    from fastapi import HTTPException
    try:
        _parse_prompt({"models": [{"model_id": "M", "npu_index": 0},
                                  {"model_id": "M", "npu_index": 0}]})
        fail("重复实例应抛 HTTPException(400)")
    except HTTPException as e:
        check(e.status_code == 400, "重复实例 → 400", f"实际 {e.status_code}")


def shutil_which(name: str) -> Optional[str]:
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = Path(d) / name
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def test_host_discovery() -> None:
    print("\n[10] 宿主卡片发现")
    from app.core.manifest import discover
    cards = {c.id: c for c in discover()}
    c = cards.get("video-depth")
    if not check(c is not None, "宿主能发现 video-depth 卡片"):
        return
    check(c.status == "ok", "卡片校验通过", f"实际 {c.status} errors={c.errors}")
    check(c.static_dir is not None and (c.static_dir / "app.js").is_file(),
          "静态资源齐全（app.js 在 static/ 下）")
    check(c.static_dir is not None
          and (c.static_dir / "wizard.json").is_file(),
          "wizard.json 在 static/ 下（否则向导读不到）")


# --------------------------------------------------------------------------- #
# 11) 占 NPU 的真机验证                                                       #
# --------------------------------------------------------------------------- #
def test_live_npu() -> None:
    print("\n[11] 真机 DA3 推理（占 NPU）")
    from app.core.depth.palette import render_depth_jpeg
    from app.core.compute.registry import get_registry
    from wdnai import SoftwareCapability

    registry = get_registry()
    try:
        sess = registry.get_session(SoftwareCapability.VISION_DEPTH)
    except RuntimeError as e:
        fail(f"深度能力不可用: {e}")
        return
    if sess is None:
        fail("深度能力不可用：get_session 返回 None")
        return

    img_path = Path("/userdata/rknn3/rknn3-model-zoo/install/rk3588_linux_aarch64/"
                    "rknn_depth_anything_v3_demo/model/frame_01.jpg")
    if not img_path.is_file():
        fail(f"测试图片不存在: {img_path}")
        return

    import cv2
    bgr = cv2.imread(str(img_path))
    if bgr is None:
        fail(f"读不出测试图片: {img_path}")
        return

    import asyncio
    t0 = time.perf_counter()
    text = asyncio.run(sess.respond(prompt="", attachment=[__import__(
        "wdnai").ImageAttachment(bgr)], include_maps=True))
    dt = (time.perf_counter() - t0) * 1000.0

    from app.core.depth.depth_detector import DepthDetector
    depth = DepthDetector._decode_depth_payload(text)
    if not check(depth is not None, "真机结果里有 depth_b64"):
        return
    print(f"     深度图 {depth.shape} · 推理 {dt:.0f} ms")
    check(dt < 2000, "单次推理在 2s 以内", f"实际 {dt:.0f} ms")

    inv_lo = float(np.percentile(1.0 / depth[depth > 0], 2))
    inv_hi = float(np.percentile(1.0 / depth[depth > 0], 98))
    jpeg, stats = render_depth_jpeg(depth, inv_lo, inv_hi)
    out = Path("/tmp/da3_verify_depth.jpg")
    out.write_bytes(jpeg)
    print(f"     热力图 → {out} · {stats}")


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="视频深度估计链路单元测试")
    ap.add_argument("--npu", action="store_true", help="额外跑一次真机推理（占 NPU）")
    args = ap.parse_args()

    test_turbo()
    test_display_semantics()
    test_render()
    test_payload_decode()
    test_duck_contract()
    test_detector_lifecycle()
    test_detector_multichannel()
    test_restart_no_leak()
    test_card_contract()
    test_host_discovery()
    test_debug_log_switch()
    if args.npu:
        test_live_npu()
    else:
        print("\n[12] 真机验证已跳过（加 --npu 跑）")

    print(f"\n{'=' * 60}")
    print(f"通过 {len(PASSES)} 项，失败 {len(FAILS)} 项")
    if FAILS:
        print("\n❌ 失败：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("\n✅ 视频深度估计链路单元测试全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
