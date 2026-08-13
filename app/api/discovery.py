"""DDS 注册发现 REST API。

* ``GET    /api/discovery/dds``           完整 snapshot（state + dds_runtime
                                            + discovered[] + last_error）。
                                            Discovery 页每秒轮询这个端点。
* ``POST   /api/discovery/dds/start``     "在网注册" 开关 ON 时调：起 wdndds
                                            Hub + register + start_scan。
* ``POST   /api/discovery/dds/stop``      "在网注册" 开关 OFF 时调：stop_scan
                                            + close + 重置 wdndds 内部状态。
* ``GET    /api/discovery/dds/runtime``   只读 fastdds 可用性检查（不触发
                                            connect）。UI banner 用。

错误处理：

* 503 —— ``POST /start`` 时 FastDDS native bindings 没装（带 ``detail`` 是
  dict，UI 直接渲染）。
* 502 —— ``POST /start`` / ``/stop`` 时启动 / 关闭流程报错。
* 409 —— 切换中（STARTING / STOPPING）又来一次 POST。
"""

from __future__ import annotations

from fastapi import APIRouter
from app.services.config import get_config

from app.services.dds_discovery import get_dds_discovery


router = APIRouter(tags=["discovery"])

@router.get("/discovery/daemon")
async def get_daemon_status():
    """Return the wdndds_daemon status (or {running: false} if not up).

    Hits the daemon's HTTP API (default 127.0.0.1:7412). The
    Discovery page renders this as a banner under the existing
    "在网注册" toggle.
    """
    import urllib.request
    import urllib.error
    import json as _json
    compute_cfg = get_config().get_section("compute") or {}
    http_port = int(compute_cfg.get("daemon_http_port", 7412))
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{http_port}/status", timeout=1) as resp:
            return _json.loads(resp.read())
    except (urllib.error.URLError, ConnectionRefusedError, TimeoutError, OSError) as e:
        return {"running": False, "error": str(e)}


@router.post("/discovery/daemon/start")
async def post_daemon_start():
    """Launch the wdndds_daemon in a detached subprocess.

    Idempotent: if it's already running (HTTP responds), just returns
    the current status.
    """
    import json as _json
    import urllib.request
    compute_cfg = get_config().get_section("compute") or {}
    udp_port = int(compute_cfg.get("discovery_server_port", 7411))
    http_port = int(compute_cfg.get("daemon_http_port", 7412))
    logfile = compute_cfg.get("daemon_logfile")
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{http_port}/status", timeout=0.5) as resp:
            return _json.loads(resp.read())
    except Exception:
        pass
    from app.services import wdndds_daemon
    p = wdndds_daemon.start_in_subprocess(
        port=udp_port, http_port=http_port, logfile=logfile)
    return {"running": True, "pid": p.pid, "udp_port": udp_port,
            "http_port": http_port}


@router.post("/discovery/daemon/stop")
async def post_daemon_stop():
    """Stop the wdndds_daemon (kill the subprocess)."""
    import subprocess
    import urllib.error
    compute_cfg = get_config().get_section("compute") or {}
    http_port = int(compute_cfg.get("daemon_http_port", 7412))
    # Try graceful via HTTP shutdown endpoint if available
    try:
        import urllib.request
        with urllib.request.urlopen(
                f"http://127.0.0.1:{http_port}/status", timeout=0.5):
            pass
    except Exception:
        return {"running": False, "stopped": False,
                "note": "daemon was not running"}
    # Find pid via ps
    try:
        out = subprocess.check_output(["pgrep", "-f", "wdndds_daemon"]).decode()
        for pid in out.split():
            subprocess.run(["kill", "-TERM", pid.strip()])
        return {"running": False, "stopped": True}
    except subprocess.CalledProcessError:
        return {"running": False, "stopped": False, "note": "no daemon process found"}



@router.get("/discovery/dds")
async def get_dds_discovery_snapshot() -> dict:
    """Discovery 页"在网注册"渲染源 —— 每秒轮询。"""
    return get_dds_discovery().snapshot()


@router.get("/discovery/dds/runtime")
async def get_dds_discovery_runtime() -> dict:
    """只读 FastDDS 可用性快照（不触发 connect / register）。"""
    return get_dds_discovery().runtime_info()


@router.post("/discovery/dds/start")
async def post_dds_discovery_start() -> dict:
    """开 toggle：起 wdndds Hub 并注册本机能力。"""
    return get_dds_discovery().start()


@router.post("/discovery/dds/stop")
async def post_dds_discovery_stop() -> dict:
    """关 toggle：拆 Hub，对端 SPDP 失活后从它们的列表里移除本机。"""
    return get_dds_discovery().stop()