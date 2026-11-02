"""深度检测器 —— DA3 单目深度估计的实时推理侧。

与 :class:`app.core.llm.detector_base.WdnaiDetector` **同一套 duck-type 契约**
（``app.core.video.multi_api.build_multi_router`` 与
:class:`app.core.video.multi_session.MultiChannelSession` 只按鸭子类型调用），
但换了一整套东西：

============  ==========================================  ==========================
              ``WdnaiDetector``（VLM）                    ``DepthDetector``（本类）
============  ==========================================  ==========================
capability   ``swcap_vision_chat`` / ``_grounding``      ``swcap_vision_depth``
输入          1 帧 + 文本 prompt                          1 帧（无 prompt）
输出          文本 / 结构化 bbox                          深度图 + 置信度
schema        ``@Generable`` 子类                        无
工具          ``@Callable`` 工具 + agent-loop             无
结果通路      文本 wire                                   文本 wire（JSON + base64 深度图）
============  ==========================================  ==========================

**深度图怎么过线**

worker 端的 ``DepthResult.text`` 是 JSON 摘要；``include_maps=True`` 时额外带
``depth_b64`` / ``confidence_b64``（fp16 + base64）。一张 280×280 的 fp16 深度
图约 157 KB raw / 209 KB base64 —— 单 view 远在 DDS 的 1 MiB 单消息上限之内，
而且 ``wdndds.ai_session`` 对超长文本会自动改走 ``/dev/shm`` 侧文件
（``_INLINE_TEXT_LIMIT = 512``），所以**不需要新开 RPC 通路**，复用现成的
``respond()`` 即可。

**渲染在宿主侧做**

worker 只回原始深度数组；turbo 着色、色标窗口、JPEG 编码都在主进程完成。
这既符合「worker 是薄 RPC server」的约定（业务级的事留在主进程），也让
色标窗口可以跨路共享 —— 同一个窗口才能横向比较不同摄像头。

**几何**

送检帧由 :class:`MultiChannelSession` 按**源宽高比**缩到最长边 280
（16:9 源 → 288×157），DA3 后端再把它拉伸到模型固定的 280×280。两次都是
轴对齐缩放，复合后与 C++ demo 的「原图直接拉伸到 280×280」**几何等价**
（只是多一次重采样，略微软一点）。本类把 280×280 的深度图按送检帧的宽高比
拉回去，于是深度画面与视频画面覆盖同一个视场，左右两栏能对齐。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .palette import DepthWindow, inverse_depth, render_depth_jpeg

LOG = logging.getLogger("core.depth")

#: 深度图 base64 的长度上限（防御性）。一张 280×280 fp16 → 209 KB base64；
#: 留 4× 余量，超了就当这一帧失败而不是把半个兆塞进 DDS。
_MAX_DEPTH_B64 = 4 * 1024 * 1024

#: 深度热力图 JPEG 的长边像素。模型输出只有 280×280，放大到 560 是为了
#: 瓦片放大时看着不糊（双线性上采样，不增加信息，但避免浏览器再缩一次）。
_DEPTH_RENDER_LONG_EDGE = 560

#: 深度事件**日志**的调试开关（环境变量名）。
#:
#: UI 上的「深度事件历史」列表已移除（200 条事件 + 缩略图既占页面又占
#: 带宽），历史改由日志承担 —— 但日志默认是**关**的，否则每秒好几条
#: INFO 会把 journal 淹掉。要看历史就把这个开关打开。
DEBUG_ENV = "WDNWEBUI_DEPTH_DEBUG"

_TRUTHY = {"1", "true", "yes", "on"}
_FALSEY = {"0", "false", "no", "off"}


def depth_debug_enabled() -> bool:
    """深度事件日志的开关（默认**关**）。

    两级开关，任一为真即打开：

    1. 环境变量 ``WDNWEBUI_DEPTH_DEBUG`` —— 运维最快的入口。在 systemd
       drop-in 里加一行 ``Environment=WDNWEBUI_DEPTH_DEBUG=1`` 即可。
       **每次调用都读**（`os.environ` 的字典查找，O(1)，可忽略）。
    2. ``config.jsonc`` 的 ``debug.depth_event_log`` —— 持久化的那份。
       配置本来就是启动时读的，改完要重启服务。

    环境变量**显式写了 0/false 时优先于配置项** —— 否则运维想在某台机器上
    临时关掉日志却关不掉（配置项仍然是 true）。
    """
    raw = os.environ.get(DEBUG_ENV, "").strip().lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSEY:
        return False
    try:
        from app.core.config import get_config
        dbg = get_config().get_section("debug", {}) or {}
        return bool(dbg.get("depth_event_log"))
    except Exception:  # noqa: BLE001 — 开关读不到时按「关」处理
        return False


@dataclass
class _Slot:
    """一条推理 slot —— 一个模型实例一条线程。

    与 :class:`app.core.llm.detector_base._InferSlot` 同构：队列 maxsize=1
    （只留最新一帧），``pending`` 表示「正在推理」。
    """

    q: "queue.Queue" = field(default_factory=lambda: queue.Queue(maxsize=1))
    pending: threading.Event = field(default_factory=threading.Event)
    model_id: str = ""
    npu_index: Optional[int] = None


class DepthDetector:
    """DA3 深度估计检测器。

    生命周期与 ``WdnaiDetector`` 对齐：

    * ``start()`` —— 从 :class:`~app.core.compute.registry.ComputeRegistry`
      拿 ``swcap_vision_depth`` 的 session（**禁止自建 session**，见 AGENTS.md
      设计原则 4），起 N 条推理线程（每个勾选的实例一条）。
    * ``submit_frame_to()`` —— 轮转调度器投递帧。
    * ``stop()`` —— 停线程、释放引用。

    没有 prompt、没有 schema、没有工具 —— 深度估计没有文本入口。
    """

    #: 事件历史条数（环形）。深度事件带一张热力图 JPEG（~20 KB），
    #: 200 条就是 4 MB，所以历史里只留**最近 12 条**带图，其余剥掉。
    HISTORY_MAX = 200
    _EVENT_IMAGE_KEEP = 12

    #: 诊断 ring buffer 的条数。
    _DIAG_HISTORY_MAX = 50

    def __init__(self, *, shared_window: bool = True) -> None:
        self._lock = threading.Lock()
        self._enabled = False
        self._every_n = 1
        self._slots: List[_Slot] = []
        self._threads: List[threading.Thread] = []
        self._sessions: List[Any] = []
        #: 启动代号。每次 start/stop 自增，推理线程拿代号比对 —— **退出判据
        #: 不能是 Event 的当前值**：``stop()`` 之后 ``start()`` 会换一个
        #: Event 的话，还没退干净的旧线程会读到**新的**（未置位的）Event，
        #: 于是永远退不出去（线程泄漏 + 两组线程同时推理）。
        #: 这与 ``multi_session._rr_gen`` 是同一个坑的两种写法。
        self._gen = 0
        self._window = DepthWindow(shared=shared_window)

        self._events: deque = deque(maxlen=self.HISTORY_MAX)
        self._events_lock = threading.Lock()
        self._latest: Optional[dict] = None
        self._next_event_id = 1

        self._diag_history: deque = deque(maxlen=self._DIAG_HISTORY_MAX)
        self._diag_lock = threading.Lock()

        self._broadcaster_lock = threading.Lock()
        self._broadcasters: List[Callable[[dict], None]] = []

        self._init_error = ""
        self._last_error = ""
        self._frame_count = 0
        self._infer_count = 0
        self._last_infer_ms = 0.0

    # ------------------------------------------------------------------ #
    # 快照 / 查询                                                        #
    # ------------------------------------------------------------------ #
    @property
    def slot_count(self) -> int:
        """并发推理 slot 数 = 勾选的实例数；K=1 时与单实例行为一致。"""
        with self._lock:
            return len(self._slots)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def available(self) -> bool:
        """宿主侧不加载模型，能力是否可用由 registry 决定 —— 这里只反映
        ``start()`` 有没有成功过。"""
        return not self._init_error

    def slot_pins(self) -> List[dict]:
        """slot 清单（给前端 / 诊断看，不含 session 对象）。"""
        with self._lock:
            return [{"model_id": s.model_id, "npu_index": s.npu_index}
                    for s in self._slots]

    def snapshot(self) -> dict:
        """detector 快照。

        字段名与 :meth:`app.core.llm.detector_base.WdnaiDetector.snapshot`
        **逐字对齐** —— 前端的状态条、``wizard-start.js`` 的能力探测、
        ``model-pick.js`` 的多卡判定都按那套字段名读。深度页虽然不需要
        prompt / image_size / available_image_sizes，但**必须给出同名字段**
        （空值即可），否则共享组件会读到 undefined。
        """
        from app.core.compute.registry import get_registry
        from wdnai import SoftwareCapability

        capability = SoftwareCapability.VISION_DEPTH
        registry = get_registry()
        session_ready = False
        session_error = ""
        try:
            sess = registry.get_session(capability)
            session_ready = sess is not None
        except RuntimeError as e:
            session_error = str(e)
        except Exception as e:  # noqa: BLE001
            session_error = f"{type(e).__name__}: {e}"

        # 「还没 running」有两种完全不同的含义，必须分开（与 WdnaiDetector 同）：
        #   * 本机有实例、正在加载 → 等待即可，前端转圈而不是报错；
        #   * 本机确实没有实例 → 真的错误。
        model_loading = False
        loading_models: List[str] = []
        if not session_ready:
            try:
                loading_models = list(
                    registry.pending_models_for_capability(capability))
            except Exception:  # noqa: BLE001
                loading_models = []
            model_loading = bool(loading_models)

        with self._lock:
            slots = list(self._slots)
            queue_size = sum(s.q.qsize() for s in slots)
            enabled = self._enabled
            every_n = self._every_n
            init_error = self._init_error
        return {
            "available":              not init_error,
            "init_error":             session_error or init_error,
            "enabled":                enabled,
            "prompt":                 "",
            "every_n":                every_n,
            "model":                  "DepthAnythingV3",
            "session_ready":          session_ready,
            "model_loading":          model_loading,
            "loading_models":         loading_models,
            "worker_alive":           bool(slots),
            "slot_count":             len(slots),
            "slots":                  [{"model_id": s.model_id,
                                       "npu_index": s.npu_index}
                                      for s in slots],
            "queue_size":             queue_size,
            "enabled_tools":          [],
            "available_image_sizes":  [],
            "image_size":             None,
            "model_id":               slots[0].model_id if slots else "",
            # ---- 深度页专有 ----
            "frames":                 self._frame_count,
            "infers":                 self._infer_count,
            "last_infer_ms":          round(self._last_infer_ms, 2),
            "last_error":             self._last_error,
            "shared_window":          self._window.shared,
            # 深度事件日志开关的**当前**状态（前端状态条可显示；
            # 运维看一眼就知道历史到底有没有在打）。
            "debug_log":              depth_debug_enabled(),
        }

    def events(self) -> List[dict]:
        with self._events_lock:
            return [dict(ev) for ev in self._events]

    def events_since(self, since_id: int) -> List[dict]:
        """事件增量。WS 循环每帧调一次 —— **只为要发的条目做拷贝**。"""
        with self._events_lock:
            out = []
            for ev in self._events:
                eid = ev.get("id")
                if eid is not None and eid > since_id:
                    out.append(dict(ev))
            return out

    def latest(self) -> Optional[dict]:
        with self._events_lock:
            return dict(self._latest) if self._latest else None

    def clear(self) -> None:
        with self._events_lock:
            self._events.clear()
            self._latest = None

    def diag_history(self) -> List[dict]:
        with self._diag_lock:
            return [dict(d) for d in reversed(self._diag_history)]

    def clear_diagnostics(self) -> None:
        with self._diag_lock:
            self._diag_history.clear()

    def register_broadcaster(self, fn: Callable[[dict], None]) -> None:
        with self._broadcaster_lock:
            self._broadcasters.append(fn)

    def unregister_broadcaster(self, fn: Callable[[dict], None]) -> None:
        with self._broadcaster_lock:
            try:
                self._broadcasters.remove(fn)
            except ValueError:
                pass

    # ------------------------------------------------------------------ #
    # 生命周期                                                            #
    # ------------------------------------------------------------------ #
    def start(self, prompt: str = "", every_n: int = 1,
              enabled_tools: Optional[List[str]] = None,
              model_id: Optional[str] = None,
              models: Optional[List[dict]] = None,
              **_: Any) -> dict:
        """启动深度估计。

        ``prompt`` / ``enabled_tools`` **被忽略** —— 深度估计没有文本入口，
        留着是为了兼容 :func:`app.core.video.multi_api.build_multi_router`
        的统一调用形态（它总会传 ``every_n`` 与 ``enabled_tools``）。

        ``models`` 是勾选的实例列表 ``[{"model_id","npu_index"}, ...]``；
        缺省 → 单实例自动挑（与 ``get_session`` 的本机优先规则一致）。

        Raises:
            RuntimeError: 能力未启用 / 实例没跑起来 —— HTTP 层转 503。
            ValueError: 参数非法 —— HTTP 层转 400。
        """
        from app.core.compute.registry import get_registry
        from wdnai import SoftwareCapability

        capability = SoftwareCapability.VISION_DEPTH
        registry = get_registry()

        sessions: List[Tuple[Any, str, Optional[int]]] = []
        if models:
            for m in models:
                mid = m.get("model_id")
                npu = m.get("npu_index")
                if not isinstance(mid, str) or not mid.strip():
                    raise ValueError(f"model_id 必须是非空字符串: {m!r}")
                sess = registry.get_session_for_model(capability, mid.strip(), npu)
                sessions.append((sess, mid.strip(), npu))
        elif model_id:
            sess = registry.get_session_for_model(capability, model_id.strip())
            sessions.append((sess, model_id.strip(), None))
        else:
            sess = registry.get_session(capability)
            sessions.append((sess, "", None))

        self.stop()
        self._init_error = ""
        self._last_error = ""

        with self._lock:
            self._gen += 1
            gen = self._gen
            self._sessions = [s for s, _, _ in sessions]
            self._slots = [_Slot(model_id=mid, npu_index=npu)
                           for _, mid, npu in sessions]
            self._every_n = 1
            self._enabled = True
            threads: List[threading.Thread] = []
            for idx, slot in enumerate(self._slots):
                # slot / session **直接传给线程**：线程里再按 idx 去
                # ``self._slots[idx]`` 取对象的话，``stop()`` 清空列表的
                # 那一刻正好在取，就会 IndexError 把线程带走。
                th = threading.Thread(
                    target=self._worker_loop, args=(slot, sessions[idx][0], idx, gen),
                    name=f"depth-infer-{idx}", daemon=True)
                th.start()
                threads.append(th)
            self._threads = threads

        LOG.info("depth detector 启动：%d 个 slot %s",
                 len(self._slots), self.slot_pins())
        return self.snapshot()

    def stop(self) -> dict:
        """停线程、释放 session 引用。可重复调用。"""
        with self._lock:
            # 代号自增 → 所有旧推理线程自行退出（不靠 Event 的当前值）。
            self._gen += 1
            self._enabled = False
            self._sessions = []
            slots = list(self._slots)
            self._slots = []
            threads = list(self._threads)
            self._threads = []
        for th in threads:
            th.join(timeout=2.0)
        # 释放还挂着 pending 的等待者（wait_idle / wait_slot_idle 靠它判空闲）。
        for slot in slots:
            slot.pending.clear()
        self._window.reset()
        return self.snapshot()

    # ------------------------------------------------------------------ #
    # 帧提交 / 等待                                                       #
    # ------------------------------------------------------------------ #
    def submit_frame(self, frame_bgr: "np.ndarray", frame_pos: int,
                     frame_b64: str = "", judge_score: Optional[float] = None,
                     channel: int = 0) -> None:
        """提交一帧待推理（投递到 slot 0）。"""
        self.submit_frame_to(0, frame_bgr, frame_pos, frame_b64,
                             judge_score, channel)

    def submit_frame_to(self, slot_idx: int, frame_bgr: "np.ndarray",
                        frame_pos: int, frame_b64: str = "",
                        judge_score: Optional[float] = None,
                        channel: int = 0) -> bool:
        """把一帧投给指定的推理 slot；返回是否真的进了队列。

        队列 maxsize=1 —— 只留最新一帧，旧的直接丢（实时流不需要补帧）。
        """
        with self._lock:
            if slot_idx < 0 or slot_idx >= len(self._slots):
                return False
            slot = self._slots[slot_idx]
            if not self._enabled:
                return False
            self._frame_count += 1
            if self._every_n > 1 and self._frame_count % self._every_n != 0:
                return False
        try:
            while True:
                slot.q.get_nowait()
        except queue.Empty:
            pass
        try:
            slot.q.put_nowait(
                (frame_bgr, frame_pos, frame_b64, judge_score, channel))
        except queue.Full:
            return False
        slot.pending.set()
        return True

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """等当前这帧推理完（或超时）。返回 True=已空闲，False=超时。

        必须用 ``time.sleep`` 而不是 ``Event.wait()``：后者在 event 已 set 时
        立即返回（只等"变成 set"，不查当前状态），而本方法的正常语义正是
        "已 set = 正在推理"，于是退化成忙等 —— 推理全程烧满一个核并抢 GIL，
        把解码线程饿死。详见 AGENTS.md。
        """
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            with self._lock:
                slots = list(self._slots)
            if not any(s.pending.is_set() for s in slots):
                return True
            remain = deadline - time.monotonic()
            if remain <= 0:
                return False
            time.sleep(min(0.005, remain))

    def wait_slot_idle(self, slot_idx: int = 0, timeout: float = 30.0) -> bool:
        """等**指定**推理 slot 空闲。轮转调度器调用。

        ``timeout=0`` 是非阻塞探询。
        """
        with self._lock:
            if slot_idx < 0 or slot_idx >= len(self._slots):
                return True                      # slot 已不存在 = 没有活要干
            slot = self._slots[slot_idx]
        if not slot.pending.is_set():
            return True
        if timeout <= 0:
            return False
        deadline = time.monotonic() + timeout
        while True:
            if not slot.pending.is_set():
                return True
            remain = deadline - time.monotonic()
            if remain <= 0:
                return False
            time.sleep(min(0.005, remain))

    # ------------------------------------------------------------------ #
    # 推理线程                                                            #
    # ------------------------------------------------------------------ #
    def _worker_loop(self, slot: _Slot, session: Any, slot_idx: int,
                     gen: int) -> None:
        """一条推理线程 = 一个 slot 的消费循环。

        ``slot`` / ``session`` **由参数传入**而不是按 idx 去
        ``self._slots[idx]`` 取 —— ``stop()`` 会清空那个列表，线程正好在
        取的那一瞬间就会 IndexError。
        """
        while self._is_current(gen):
            try:
                item = slot.q.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                slot.pending.clear()
                continue
            frame, frame_pos, frame_b64, judge_score, channel = item
            try:
                self._infer_sync(slot, session, frame, frame_pos,
                                 frame_b64, judge_score, channel, gen)
            except Exception as exc:  # noqa: BLE001 — 推理失败不能带死线程
                LOG.warning("depth slot%d 推理失败: %s", slot_idx, exc)
                self._last_error = f"{type(exc).__name__}: {exc}"
                self._push_diag({
                    "ts": time.time(),
                    "ok": False,
                    "error": self._last_error,
                    "channel": channel,
                    "frame_pos": frame_pos,
                })
            finally:
                slot.pending.clear()

    def _is_current(self, gen: int) -> bool:
        """本线程是否仍属于当前这次启动。"""
        with self._lock:
            return gen == self._gen and self._enabled

    # ------------------------------------------------------------------ #
    # 单帧推理                                                            #
    # ------------------------------------------------------------------ #
    def _infer_sync(self, slot: _Slot, session: Any, frame: "np.ndarray",
                    frame_pos: int, frame_b64: str, judge_score: Optional[float],
                    channel: int, gen: int) -> None:
        """跑一帧深度，产出事件。

        调用形态：``session.respond(prompt="", attachment=[ImageAttachment(frame)],
        include_maps=True)``。``generating=None`` → SDK 原样返回文本（不解析
        schema），文本就是 :class:`wdnai.backends.depth_anything.DepthResult`
        的 JSON 摘要。
        """
        from wdnai import ImageAttachment

        t0 = time.perf_counter()
        coro = session.respond(
            prompt="",
            attachment=[ImageAttachment(frame)],
            include_maps=True,
        )
        text = asyncio.run(coro)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        depth = self._decode_depth_payload(text)
        if depth is None:
            raise RuntimeError("深度结果里没有 depth_b64")

        inv = inverse_depth(depth)
        win = self._window.update(channel, inv)
        if win is None:
            raise RuntimeError("深度图有效像素不足，无法确定色标窗口")
        lo, hi = win

        # 深度图按**送检帧的宽高比**回拉 —— 模型输出是 280×280 的拉伸结果，
        # 直接显示会让画面比例与左侧视频对不上。两次轴对齐缩放可复合，
        # 所以这里恢复的是源视场，不是又拉伸一次。
        ah, aw = int(frame.shape[0]), int(frame.shape[1])
        if aw > 0 and ah > 0 and aw != ah:
            scale = _DEPTH_RENDER_LONG_EDGE / float(max(aw, ah))
            out_w = max(16, int(round(aw * scale)))
            out_h = max(16, int(round(ah * scale)))
        else:
            out_w = out_h = _DEPTH_RENDER_LONG_EDGE

        # 在**深度数组层**重采样再着色（不是缩 JPEG）。
        jpeg, stats = render_depth_jpeg(depth, lo, hi, out_size=(out_w, out_h))

        # 停机竞态：推理期间可能已经 stop 了 —— 这条事件不该再进历史。
        if not self._is_current(gen):
            return

        self._infer_count += 1
        self._last_infer_ms = elapsed_ms

        event = {
            "id": self._next_event_id,
            "channel": int(channel),
            "ts": time.time(),
            "frame_pos": int(frame_pos),
            "model_id": slot.model_id,
            "npu_index": slot.npu_index,
            "judge_score": judge_score,
            "depth_b64": base64.b64encode(jpeg).decode("ascii"),
            "width": out_w,
            "height": out_h,
            "stats": stats,
            "infer_ms": round(elapsed_ms, 2),
        }
        self._next_event_id += 1

        # 深度事件历史 → **日志**（UI 上的事件列表已移除）。
        #
        # 默认关：每秒好几条 INFO 会把 journal 淹掉，而绝大多数时候没人
        # 要看。要看就把 ``WDNWEBUI_DEPTH_DEBUG=1`` 打开（见
        # :func:`depth_debug_enabled`）。
        #
        # 打在**广播之前**：广播器抛异常不该把这条历史吞掉。
        if depth_debug_enabled():
            LOG.info(
                "[depth-event] id=%d ch=%d npu=%s pos=%d "
                "depth=[%.3f,%.3f] mean=%.3f window=[%.3f,%.3f] infer=%.0fms",
                event["id"], event["channel"], event["npu_index"],
                event["frame_pos"],
                stats.get("depth_min", 0.0), stats.get("depth_max", 0.0),
                stats.get("depth_mean", 0.0),
                stats.get("inv_lo", 0.0), stats.get("inv_hi", 0.0),
                event["infer_ms"])

        with self._events_lock:
            self._events.append(event)
            self._latest = event
            self._trim_event_images()

        self._push_diag({
            "ts": event["ts"],
            "ok": True,
            "channel": event["channel"],
            "frame_pos": event["frame_pos"],
            "infer_ms": event["infer_ms"],
            "stats": stats,
        })

        with self._broadcaster_lock:
            broadcasters = list(self._broadcasters)
        for fn in broadcasters:
            try:
                fn(event)
            except Exception:  # noqa: BLE001
                LOG.debug("depth 广播失败", exc_info=True)

    def _trim_event_images(self) -> None:
        """历史里只给最近 ``_EVENT_IMAGE_KEEP`` 条留热力图。

        一张热力图 JPEG ~20 KB，200 条就是 4 MB —— 页面初始化拉一次
        ``/events`` 扛不住。实时事件（WS 推送的那条）带图，前端画的就是它。
        """
        keep = self._EVENT_IMAGE_KEEP
        n = len(self._events)
        for i, ev in enumerate(self._events):
            if i < n - keep and "depth_b64" in ev:
                ev.pop("depth_b64", None)

    @staticmethod
    def _decode_depth_payload(text: Any) -> Optional["np.ndarray"]:
        """从 worker 回的 JSON 里解出深度数组（fp16 → float32）。

        ``DepthResult.text`` 带 ``depth_b64``（fp16 base64，形状
        ``(views, H, W)``）与 ``height`` / ``width`` / ``views``。深度卡是
        **单 view**，所以只取第 0 张。
        """
        if not isinstance(text, str):
            return None
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        b64 = payload.get("depth_b64")
        if not b64:
            return None
        if len(b64) > _MAX_DEPTH_B64:
            raise ValueError(f"depth_b64 过大: {len(b64)} 字节")
        raw = base64.b64decode(b64)
        views = int(payload.get("views") or 1)
        h = int(payload.get("height") or 0)
        w = int(payload.get("width") or 0)
        if h <= 0 or w <= 0:
            return None
        arr = np.frombuffer(raw, dtype=np.float16).astype(np.float32)
        expect = views * h * w
        if arr.size != expect:
            raise ValueError(
                f"深度数据尺寸不符: 收到 {arr.size}，期望 {expect}（{views}×{h}×{w}）")
        arr = arr.reshape(views, h, w)
        return arr[0]

    def _push_diag(self, item: dict) -> None:
        with self._diag_lock:
            self._diag_history.append(item)


# --------------------------------------------------------------------------- #
# 单例                                                                    #
# --------------------------------------------------------------------------- #
_depth_detector: Optional[DepthDetector] = None
_detector_lock = threading.Lock()


def get_depth_detector() -> DepthDetector:
    """视频深度估计页的 detector 单例。

    与 ``get_detector`` / ``get_target_detector`` / ``get_localizer_detector``
    同构：进程级单例 + 锁保护 + 惰性构造。
    """
    global _depth_detector
    with _detector_lock:
        if _depth_detector is None:
            _depth_detector = DepthDetector()
        return _depth_detector


def stop_depth_detector() -> bool:
    """停掉单例（若已创建）。返回是否真的停了一个。

    与 :func:`get_depth_detector` 分开是为了让「停」**不创建**单例 ——
    关停钩子不该因为一次从没跑过的卡而凭空造出对象。
    """
    global _depth_detector
    with _detector_lock:
        det = _depth_detector
        if det is None:
            return False
    try:
        det.stop()
    except Exception:  # noqa: BLE001 — 关停钩子必须 best-effort
        LOG.warning("停止深度 detector 失败", exc_info=True)
        return False
    return True
