"""DDS-based registration & discovery (走 :mod:`wdndds` + FastDDS)。

设计目标
--------

* Discovery 页 "在网注册" 开关拨上 → 起 wdndds ``Hub``，把当前活跃 SW 能力
  (``ComputeRegistry.list_active_capabilities``) + 已启用 HW 能力
  (``list_hardware_capabilities`` 中 ``enabled=True`` 的项) 注册成 capability
  实例，让同网段的 FastDDS participant 都能在 ``wdndds/registry`` topic 上看到
  本机。
* 开关拨下 → 关闭 ``Hub``，对端通过 SPDP participant 失活会自动从它们的列表
  里移除。
* **FastDDS native lib 没装时** → state 转 ``unavailable``，API 返回 503，UI
  显式渲染红色错误条；不要静默成功也不要 500。
* session-only，不持久化 enabled —— 跟 ``ComputeRegistry`` 一样遵循
  "boot 不自动加载" 原则；用户必须从 Discovery 页手动打开开关。

为什么不让 ``wdndds.registry`` 一直挂着：

* 真机一旦连接就立刻开始发 SPDP 广播（UDP 组播 239.255.0.1 起 / discovery
  port 7400 起），用户没主动"在网注册"之前广播 entry 不合适。
* ``register()`` 没有公共 ``unregister()``；要"撤掉本机 entry"只能
  ``Hub.close()`` + 重置模块级 ``_hub``。这条路径单独走 ``_teardown()``。

线程模型：

* 状态读写都在 asyncio 上下文（FastAPI handler），用 ``RLock`` 串行化；
* ``wdndds.start_scan`` 的回调跑在 wdndds 自己的后台 pump 线程上，回调只
  操作 ``_discovered`` dict，加同一把锁。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from app.services.config import get_config


LOG = logging.getLogger("dds_discovery")


# --------------------------------------------------------------------------- #
# capability_type 字符串 → wdndds Capability 类的工厂
# --------------------------------------------------------------------------- #
# 复用 wdndds 暴露的叶子类，每个类对应一个 ``capability_type`` 字符串（与
# ``HARDWARE_CAPABILITIES`` / ``MODEL_PRESETS`` 完全一致）。
_CAPABILITY_FACTORIES: Dict[str, str] = {
    # HW
    "hwcap_video_input":   "HWVideoInput",
    "hwcap_audio_input":   "HWAudioInput",
    "hwcap_audio_output":  "HWAudioOutput",
    "hwcap_hdmi_display":  "HWHDMIDisplay",
    # SW
    "swcap_video_query":   "SWVideoQuery",
    "swcap_video_describe": "SWVideoDescribe",
    "swcap_video_analyse": "SWVideoAnalyse",
    "swcap_audio_query":   "SWAudioQuery",
    "swcap_audio_analyse": "SWAudioAnalyse",
    "swcap_audio_tts":     "SWAudioTTS",
    "swcap_text_chat":     "SWTextChat",
}


def _build_capabilities(sw_caps: List[dict], hw_caps: List[dict]) -> List[Any]:
    """把 registry 出来的能力元数据 → wdndds Capability 实例列表。

    * 不传 ``callback`` —— 走 ``_default_handle``，对端 invoke 我们就回
      一个默认应答。这一轮只做"广播自己"，不接 invoke handler。
    * 同一 capability_type 多实例（多 NPU 都启了同一 model）→ 用
      ``instance_name`` 后缀区分。
    """
    import wdndds

    factory_names = set(_CAPABILITY_FACTORIES.values())
    out: List[Any] = []
    seen_names_per_type: Dict[str, int] = {}

    def _make(type_str: str, instance_name: str) -> Any:
        cls_name = _CAPABILITY_FACTORIES.get(type_str)
        if cls_name is None:
            LOG.debug("build_capabilities: unknown type %s, skip", type_str)
            return None
        if not hasattr(wdndds, cls_name):
            LOG.debug("build_capabilities: wdndds.%s missing, skip", cls_name)
            return None
        cls = getattr(wdndds, cls_name)
        return cls(instance_name)

    for cap in sw_caps:
        t = cap.get("type", "")
        base = cap.get("name") or t
        # 防重名：同一 type 多次时追加 npu_index
        key = (t, base)
        suffix = ""
        if seen_names_per_type.get(t, 0) > 0 and base == t:
            suffix = f"_{cap.get('npu_index', 0)}"
        # 多 NPU + 同 instance_name 时也强制区分：用 npu_index 后缀
        if cap.get("model"):
            inst = f"{base}{suffix}"
        else:
            inst = base
        seen_names_per_type[t] = seen_names_per_type.get(t, 0) + 1
        made = _make(t, inst)
        if made is not None:
            out.append(made)

    for cap in hw_caps:
        if not cap.get("enabled", True):
            continue
        t = cap.get("capability_type", "")
        if not t:
            continue
        # HW 一般只挂一个（板子的物理接口），instance_name 走 class_name
        # 或 capability_type 后缀；用户后续要扩展多实例 HW 再调整
        made = _make(t, cap.get("class_name") or t)
        if made is not None:
            out.append(made)
    return out


# --------------------------------------------------------------------------- #
# Manager
# --------------------------------------------------------------------------- #
class DdsDiscoveryManager:
    """管理 wdndds ``Hub`` + scan callback + 远端设备表。

    状态机：

    * ``uninitialized`` —— 从未 connect 过
    * ``unavailable``    —— FastDDS native lib 不可用（dds.check_runtime().available=False）
    * ``ready``          —— 已 connect + register + start_scan，对端可见
    * ``starting``       —— connect/register 进行中
    * ``stopping``       —— teardown 进行中
    * ``error``          —— 上一次 start/stop 报错
    """

    STATE_UNINITIALIZED = "uninitialized"
    STATE_UNAVAILABLE = "unavailable"
    STATE_READY = "ready"
    STATE_STARTING = "starting"
    STATE_STOPPING = "stopping"
    STATE_ERROR = "error"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: str = self.STATE_UNINITIALIZED
        self._last_error: str = ""
        self._device_name: str = ""
        self._started_at: float = 0.0
        self._stopped_at: float = 0.0

        self._hub: Any = None          # wdndds Hub 实例，register 成功后存
        self._scan_cb: Optional[Callable[[Any, Any], None]] = None

        # 远端设备：scan callback 填这里
        self._discovered: Dict[str, dict] = {}

        # 缓存 fastdds runtime check 首次结果，避免每次开 toggle 都打一次
        self._runtime_cache: Optional[dict] = None

    # ------------------------------------------------------------ runtime
    def _check_runtime(self) -> dict:
        """调 ``wdndds.dds.check_runtime()`` 并缓存。

        fastdds 没装时 ``wdndds.dds.check_runtime()`` 返回
        ``{available: False, error: "wdndds.dds requires ..."}``，此处原样转发。
        """
        if self._runtime_cache is not None:
            return self._runtime_cache
        try:
            import wdndds  # noqa: F401
            info = wdndds.dds.check_runtime()
        except Exception as e:  # noqa: BLE001
            info = {"available": False, "error": f"{type(e).__name__}: {e}"}
        self._runtime_cache = info
        return info

    def runtime_info(self) -> dict:
        """对外接口：UI 渲染 banner / 错误条用。每次调用都重读缓存。"""
        return self._check_runtime()

    # ------------------------------------------------------------ device name
    def _resolve_device_name(self) -> str:
        """从 ``config.json["base"]["device_name"]`` 读对外广播的设备名。

        缺省时兜底 ``"pyrkaiweb"`` —— 不能为空，wdndds 的 registry 协议不强
        制，但 UI 展示上需要非空串。
        """
        try:
            base = get_config().get_section("base", {})
            name = (base.get("device_name") or "").strip()
        except Exception:
            name = ""
        return name or "pyrkaiweb"

    # ------------------------------------------------------------ registry read
    def _registry_snapshot(self) -> tuple[List[dict], List[dict]]:
        """懒导入 registry，避免循环依赖 + rkai native lib 在模块加载时被触达。"""
        try:
            from app.services.compute_registry import get_registry
            reg = get_registry()
            return reg.list_active_capabilities(), reg.list_hardware_capabilities()
        except Exception as e:  # noqa: BLE001
            LOG.debug("registry read failed: %s", e)
            return [], []

    # ------------------------------------------------------------ scan callback
    def _on_scan_event(self, device: Any, event: Any) -> None:
        """``wdndds.start_scan`` 回调：维护远端设备表。

        跑在 wdndds 内部 pump 线程，所以访问 ``_discovered`` 时拿锁。
        """
        with self._lock:
            name = getattr(device, "name", "") or ""
            if not name:
                return
            now = time.time()
            if event.kind == "added":
                caps = sorted({c.capability_type for c in device.capabilities})
                existing = self._discovered.get(name)
                if existing is None:
                    self._discovered[name] = {
                        "name":         name,
                        "capabilities": caps,
                        "first_seen":   now,
                        "last_seen":    now,
                    }
                else:
                    existing["capabilities"] = caps
                    existing["last_seen"] = now
            elif event.kind == "removed":
                self._discovered.pop(name, None)

    # ------------------------------------------------------------ start / stop
    def start(self) -> dict:
        """开 toggle：connect + register + start_scan。

        * 503 —— fastdds 没装（带 ``dds_runtime`` 字段给 UI 直接渲染）
        * 502 —— 启动流程中任何一步抛异常
        * 200 —— 已经在 ready 状态（幂等）
        """
        from fastapi import HTTPException

        with self._lock:
            if self._state == self.STATE_READY:
                return {"ok": True, "snapshot": self.snapshot()}
            if self._state in (self.STATE_STARTING, self.STATE_STOPPING):
                raise HTTPException(
                    status_code=409,
                    detail=f"dds discovery 正在切换中，当前 state={self._state}",
                )
            self._state = self.STATE_STARTING
            self._last_error = ""
        try:
            runtime = self._check_runtime()
            if not runtime.get("available", False):
                with self._lock:
                    self._state = self.STATE_UNAVAILABLE
                    self._last_error = (
                        runtime.get("error")
                        or "FastDDS Python bindings 未安装（apt 上没有对应包，需从 Fast-DDS-Python 源码构建）"
                    )
                raise HTTPException(
                    status_code=503,
                    detail={
                        "reason": "fastdds_unavailable",
                        "message": (
                            "FastDDS Python bindings 未安装，DDS 注册功能不可用。"
                            " Debian/Ubuntu 上没有 apt 包（注意不是 "
                            "libfastdds-dev / fastdds-python3 —— 后者根本不存在），"
                            " 只能从 Fast-DDS-Python 源码 colcon 构建。"
                            " 构建依赖：sudo apt install -y libasio-dev "
                            "libtinyxml2-dev libfastrtps-dev。"
                        ),
                        "dds_runtime": runtime,
                    },
                )

            import wdndds  # noqa: F401

            sw_caps, hw_caps = self._registry_snapshot()
            caps = _build_capabilities(sw_caps, hw_caps)

            device_name = self._resolve_device_name()

            # wdndds.connect() / register() / start_scan()
            try:
                hub = wdndds.connect()
            except RuntimeError as e:
                # 已连过 —— teardown 再重连
                LOG.info("wdndds.connect raised (%s); tearing down and retrying", e)
                self._force_reset_wdndds_hub()
                hub = wdndds.connect()

            try:
                wdndds.register(device_name, caps)
            except RuntimeError as e:
                # "device already registered in this process" —— 同样 reset
                LOG.info("wdndds.register raised (%s); tearing down and retrying", e)
                self._force_reset_wdndds_hub()
                hub = wdndds.connect()
                wdndds.register(device_name, caps)

            cb = self._on_scan_event
            wdndds.start_scan(cb)

            with self._lock:
                self._hub = hub
                self._scan_cb = cb
                self._device_name = device_name
                self._started_at = time.time()
                self._state = self.STATE_READY
            return {"ok": True, "snapshot": self.snapshot()}
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            LOG.exception("dds discovery start failed")
            with self._lock:
                self._state = self.STATE_ERROR
                self._last_error = f"{type(e).__name__}: {e}"
                # 启动失败：清理可能半成品的 Hub
                self._force_reset_wdndds_hub_locked()
            from fastapi import HTTPException as _HTTP
            raise _HTTP(
                status_code=502,
                detail=f"dds discovery 启动失败: {e}",
            ) from e

    def stop(self) -> dict:
        """关 toggle：stop_scan + close Hub + 重置 wdndds registry 状态。"""
        from fastapi import HTTPException

        with self._lock:
            if self._state in (self.STATE_UNINITIALIZED, self.STATE_UNAVAILABLE):
                return {"ok": True, "snapshot": self.snapshot()}
            if self._state in (self.STATE_STARTING, self.STATE_STOPPING):
                raise HTTPException(
                    status_code=409,
                    detail=f"dds discovery 正在切换中，当前 state={self._state}",
                )
            self._state = self.STATE_STOPPING
        try:
            self._teardown_hub()
            with self._lock:
                self._state = self.STATE_UNINITIALIZED
                self._stopped_at = time.time()
                self._discovered.clear()
            return {"ok": True, "snapshot": self.snapshot()}
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            LOG.exception("dds discovery stop failed")
            with self._lock:
                self._state = self.STATE_ERROR
                self._last_error = f"{type(e).__name__}: {e}"
            from fastapi import HTTPException as _HTTP
            raise _HTTP(
                status_code=502,
                detail=f"dds discovery 停止失败: {e}",
            ) from e

    # ------------------------------------------------------------ teardown
    def _teardown_hub(self) -> None:
        """stop_scan + close + 重置 wdndds registry 的全局 _hub。"""
        with self._lock:
            cb = self._scan_cb
            hub = self._hub
            self._scan_cb = None
            self._hub = None
        # 先 stop_scan（避免 close 后再调），再 close
        if cb is not None:
            try:
                import wdndds
                wdndds.stop_scan(cb)
            except Exception as e:  # noqa: BLE001
                LOG.debug("stop_scan 失败（忽略）：%s", e)
        if hub is not None:
            try:
                hub.close()
            except Exception as e:  # noqa: BLE001
                LOG.debug("hub.close() 失败（忽略）：%s", e)
        # wdndds.registry._hub 是模块私有变量；close() 之后它仍然指向已
        # 关闭的 Hub，下次 connect() 会抛 RuntimeError。手动置 None 让
        # 下次 connect() 走 _hub_lock 分支重建。
        try:
            import wdndds
            import wdndds.registry as _reg_mod
            with _reg_mod._hub_lock:  # type: ignore[attr-defined]
                _reg_mod._hub = None  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001
            LOG.debug("重置 wdndds.registry._hub 失败（忽略）：%s", e)

    def _force_reset_wdndds_hub(self) -> None:
        """start 失败时强制把 wdndds 内部 _hub 复位，不抛异常。"""
        try:
            import wdndds
            import wdndds.registry as _reg_mod
            hub = getattr(_reg_mod, "_hub", None)
            if hub is not None:
                try:
                    hub.close()
                except Exception:
                    pass
            with _reg_mod._hub_lock:  # type: ignore[attr-defined]
                _reg_mod._hub = None  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001
            LOG.debug("_force_reset_wdndds_hub 失败（忽略）：%s", e)

    def _force_reset_wdndds_hub_locked(self) -> None:
        """持锁版本（start() 失败回调用）。"""
        # 先解锁再调用非锁版本，避免自死锁
        self._force_reset_wdndds_hub()

    # ------------------------------------------------------------ snapshot
    def snapshot(self) -> dict:
        runtime = self._check_runtime()
        with self._lock:
            discovered = sorted(
                self._discovered.values(),
                key=lambda d: d.get("name") or "",
            )
            return {
                "state":            self._state,
                "available":        bool(runtime.get("available", False)),
                "dds_runtime":      runtime,
                "device_name":      self._device_name,
                "last_error":       self._last_error,
                "started_at":       self._started_at,
                "stopped_at":       self._stopped_at,
                "discovered":       discovered,
                "discovered_count": len(discovered),
                "last_updated":     time.time(),
            }

    # ------------------------------------------------------------ shutdown
    def close(self) -> None:
        """app shutdown hook：best-effort 关闭 hub。"""
        try:
            self._teardown_hub()
            with self._lock:
                self._state = self.STATE_UNINITIALIZED
        except Exception as e:  # noqa: BLE001
            LOG.warning("dds discovery close failed: %s", e)


# --------------------------------------------------------------------------- #
# Singleton
# --------------------------------------------------------------------------- #
_manager: Optional[DdsDiscoveryManager] = None
_manager_lock = threading.Lock()


def get_dds_discovery() -> DdsDiscoveryManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = DdsDiscoveryManager()
        return _manager


def close_dds_discovery() -> None:
    """app shutdown hook —— no-op 如果 manager 从未创建过。"""
    global _manager
    with _manager_lock:
        if _manager is None:
            return
        _manager.close()
        _manager = None