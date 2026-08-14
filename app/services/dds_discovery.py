"""DDS-based registration & discovery (cshim-backed).

Discovery toggle ON:
  - Start wdndds Hub (one DomainParticipant per process) via cshim
  - Per-device peer_matched events surface through the cshim Hub's
    peer_cb trampoline → 维护 ``discovered`` dict (device_name -> {caps})

Discovery toggle OFF:
  - Stop Hub; for endpoint tearing, future work

Note: in v0.4 the per-model compute_worker subprocesses are
**NOT** advertised through this Hub — they communicate with the
main process over the wdndds RPC layer (cshim-based) which is
separate from discovery. The discovery Hub is reserved for
inter-device (cross-wdnwebui) service discovery in future
multi-board setups.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional


LOG = logging.getLogger("dds_discovery")


# --------------------------------------------------------------------------- #
# Manager                                                                    #
# --------------------------------------------------------------------------- #
class DdsDiscoveryManager:
    """Manage the wdndds Hub for cross-device discovery.

    State machine:
    * ``uninitialized`` — never connected
    * ``unavailable``   — FastDDS native lib not available
    * ``ready``         — Hub up; peer events flow
    * ``starting`` / ``stopping`` — transient
    * ``error``         — last start/stop errored
    """

    STATE_UNINITIALIZED = "uninitialized"
    STATE_UNAVAILABLE   = "unavailable"
    STATE_READY         = "ready"
    STATE_STARTING      = "starting"
    STATE_STOPPING      = "stopping"
    STATE_ERROR         = "error"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: str = self.STATE_UNINITIALIZED
        self._last_error: str = ""
        self._device_name: str = ""
        self._started_at: float = 0.0
        self._stopped_at: float = 0.0
        self._hub: Any = None  # wdndds.dds._rpc.Hub
        self._local_capabilities: list[dict] = []
        self._discovered: Dict[str, dict] = {}
        self._runtime_cache: Optional[dict] = None

    def runtime_info(self) -> dict:
        """Snapshot FastDDS runtime availability. Cached after first call.

        Always returns a dict ``{available: bool, error: str | None, version: str | None}``
        regardless of whether the underlying ``check_runtime()`` returned a
        tuple (v0.5+) or a dict (legacy).
        """
        if self._runtime_cache is not None:
            return self._runtime_cache
        try:
            from wdndds.dds._cshim import check_runtime
            raw = check_runtime()
        except Exception as e:  # noqa: BLE001
            self._runtime_cache = {"available": False, "error": str(e),
                                   "version": None}
            return self._runtime_cache
        if isinstance(raw, dict):
            self._runtime_cache = {
                "available": bool(raw.get("available", False)),
                "error":     raw.get("error"),
                "version":   raw.get("version"),
            }
        else:
            available, error = (raw + (None,))[:2] if raw else (False, None)
            self._runtime_cache = {
                "available": bool(available),
                "error":     error,
                "version":   None,
            }
        return self._runtime_cache

    def start(self, *, device_name: str = "",
              capabilities: Optional[list[dict]] = None) -> dict:
        """Open the discovery Hub.

        ``capabilities`` is the list of capability descriptors
        (mirror of ComputeRegistry.list_active_capabilities())
        that the Hub should advertise to peers. Not used yet in
        v0.4 (per-instance compute_worker is RPC-only); reserved
        for the cross-device discovery in future multi-board setups.
        """
        with self._lock:
            if self._state == self.STATE_READY:
                return {"ok": True, "snapshot": self.snapshot()}
            if self._state in (self.STATE_STARTING, self.STATE_STOPPING):
                from fastapi import HTTPException
                raise HTTPException(
                    status_code=409,
                    detail=f"dds discovery 正在切换中，当前 state={self._state}",
                )
            self._state = self.STATE_STARTING
            self._last_error = ""
            self._local_capabilities = list(capabilities or [])

        try:
            runtime = self.runtime_info()
            if not runtime.get("available", False):
                with self._lock:
                    self._state = self.STATE_UNAVAILABLE
                    self._last_error = runtime.get(
                        "error", "FastDDS native lib 未安装")
                from fastapi import HTTPException
                raise HTTPException(
                    status_code=503,
                    detail={
                        "reason": "fastdds_unavailable",
                        "message": "FastDDS native library not available",
                        "dds_runtime": runtime,
                    },
                )

            from wdndds.dds._rpc import Hub as _RpcHub
            # Hub with no services (we only need it for discovery
            # event flow, not RPC serving). Pass a placeholder
            # service — cshim requires >= 1. Point at the LAN's
            # wdndds_daemon if config.json has one configured (same
            # daemon as the compute RPC layer).
            from app.services.config import get_config
            compute_cfg = get_config().get_section("compute") or {}
            servers = compute_cfg.get("discovery_servers") or []
            if isinstance(servers, str):
                servers = [s.strip() for s in servers.split(",") if s.strip()]
            # v0.8: FastDDS participant_name 加 device_name 后缀，跟
            # compute_worker / main process 的命名约定一致，方便 LAN
            # daemon 的 /peers 端点调试时一眼认出是哪个板子。
            base_cfg = get_config().get_section("base") or {}
            dn = (base_cfg.get("device_name") or "").strip() or "wdnwebui"
            hub = _RpcHub(
                ["_discovery_placeholder"],
                domain_id=0,
                discovery_servers=list(servers),
                participant_name=f"wdnwebui-discovery-{dn}",
            )
            hub.start()

            with self._lock:
                self._hub = hub
                self._device_name = device_name
                self._started_at = time.time()
                self._state = self.STATE_READY
            return {"ok": True, "snapshot": self.snapshot()}
        except Exception as e:  # noqa: BLE001
            LOG.exception("dds discovery start failed")
            with self._lock:
                self._state = self.STATE_ERROR
                self._last_error = f"{type(e).__name__}: {e}"
            from fastapi import HTTPException
            raise HTTPException(
                status_code=502,
                detail=f"dds discovery 启动失败: {e}",
            ) from e

    def stop(self) -> dict:
        with self._lock:
            if self._state in (self.STATE_UNINITIALIZED, self.STATE_UNAVAILABLE):
                return {"ok": True, "snapshot": self.snapshot()}
            if self._state in (self.STATE_STARTING, self.STATE_STOPPING):
                from fastapi import HTTPException
                raise HTTPException(
                    status_code=409,
                    detail=f"dds discovery 正在切换中，当前 state={self._state}",
                )
            self._state = self.STATE_STOPPING
        try:
            with self._lock:
                hub = self._hub
                self._hub = None
            if hub is not None:
                hub.close()
            with self._lock:
                self._state = self.STATE_UNINITIALIZED
                self._stopped_at = time.time()
                self._discovered.clear()
            return {"ok": True, "snapshot": self.snapshot()}
        except Exception as e:  # noqa: BLE001
            LOG.exception("dds discovery stop failed")
            with self._lock:
                self._state = self.STATE_ERROR
                self._last_error = f"{type(e).__name__}: {e}"
            from fastapi import HTTPException
            raise HTTPException(
                status_code=502,
                detail=f"dds discovery 停止失败: {e}",
            ) from e

    def snapshot(self) -> dict:
        runtime = self.runtime_info()
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

    def close(self) -> None:
        try:
            self.stop()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Singleton                                                                  #
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
    """App shutdown hook — best-effort tear-down of the discovery Hub."""
    global _manager
    with _manager_lock:
        if _manager is None:
            return
        _manager.close()
        _manager = None
