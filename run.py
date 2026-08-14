"""Entry point for wdnwebui service.

Usage:
    python run.py
    python run.py --config /etc/wdnwebui/config.json
    # or
    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
import argparse
import os
import sys
from pathlib import Path

import uvicorn

from app import __version__
from app.services import wdndds_daemon
from app.services.config import get_config


def _start_wdndds_daemon() -> None:
    """Auto-start ``wdndds_daemon`` before uvicorn boots.

    The daemon process renames its comm to ``"wdndds-daemon"`` via
    ``prctl(PR_SET_NAME)`` immediately after import (see
    :func:`app.services.wdndds_daemon._set_comm_name`). We walk
    ``/proc/*/comm`` via
    :func:`app.services.wdndds_daemon.find_daemon_pids` first; any
    match — including daemons started by systemd, manually, or by a
    previous ``run.py`` invocation — short-circuits Popen so reload
    loops and double-launches are impossible.

    Ports are read from ``config.json["compute"]`` so a board
    configured with non-default ``discovery_server_port`` /
    ``daemon_http_port`` still comes up correctly. Falls back to the
    daemon's CLI defaults (7411 / 7412) when the keys are missing.

    All failures are logged to stderr and swallowed — a daemon crash
    must not block the web service from coming up. The user can
    restart manually or fix the config and retry.
    """
    existing = wdndds_daemon.find_daemon_pids()
    if existing:
        print(f"[run.py] wdndds_daemon already running (pids={existing}), "
              f"skip auto-start", file=sys.stderr)
        return

    compute_cfg = get_config().get_section("compute") or {}
    udp_port = int(compute_cfg.get("discovery_server_port",
                                  wdndds_daemon.DEFAULT_UDP_PORT))
    http_port = int(compute_cfg.get("daemon_http_port",
                                    wdndds_daemon.DEFAULT_HTTP_PORT))
    logfile = compute_cfg.get("daemon_logfile")

    try:
        proc = wdndds_daemon.start_in_subprocess(
            port=udp_port, http_port=http_port, logfile=logfile)
        print(f"[run.py] wdndds_daemon auto-started pid={proc.pid} "
              f"udp={udp_port} http={http_port}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"[run.py] wdndds_daemon auto-start failed: "
              f"{type(e).__name__}: {e}", file=sys.stderr)


def _ensure_wdndds_native_path() -> None:
    """Prepend wdndds native lib dir to LD_LIBRARY_PATH。

    wdndds cshim 链接 libwdndds_cshim.so + libwdndds_idl.so +
    libfast{cdr,dds}.so，位于 ``.venv/src/wdndds/native/lib``。
    该目录不在系统 loader 搜索路径里（apt 没包、ldconfig 没注册）；
    不加上 spawn 出的 compute_worker 子进程会 fork 后 dlopen 失败，
    进而 ComputeRegistry Hub 起不来。

    这里设的 LD_LIBRARY_PATH 通过继承环境传给子进程，worker 端无需
    各自配置。当路径已指向可加载库时本函数是 no-op。
    """
    repo_root = Path(__file__).resolve().parent
    native_lib = repo_root / ".venv" / "src" / "wdndds" / "native" / "lib"
    if not native_lib.is_dir():
        return
    marker = native_lib / "libwdndds_cshim.so"
    if not marker.is_file():
        return
    current = os.environ.get("LD_LIBRARY_PATH", "")
    parts = [str(native_lib)] + ([p for p in current.split(":") if p] if current else [])
    seen: set[str] = set()
    deduped = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    os.environ["LD_LIBRARY_PATH"] = ":".join(deduped)


_ensure_wdndds_native_path()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="wdnwebui",
        description=f"wdnwebui service v{__version__}",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parent / "config.json",
        help="Path to the service config.json "
             "(default: <repo>/config.json). Propagated to the app via "
             "the WDNWEBUI_CONFIG env var.",
    )
    args, _unknown = parser.parse_known_args()

    config_path = args.config.expanduser()
    if not config_path.is_absolute():
        config_path = (Path(__file__).resolve().parent / config_path).resolve()
    else:
        config_path = config_path.resolve()

    # Propagate to the app via env so app.services.config picks it up
    # before any FastAPI route is hit.
    os.environ["WDNWEBUI_CONFIG"] = str(config_path)

    # Bring up the FastDDS discovery daemon before uvicorn binds the
    # web port. _start_wdndds_daemon() is idempotent — it walks
    # /proc/*/comm for the daemon's prctl-set name ("wdndds-daemon")
    # and skips Popen when one is already running, so re-invoking
    # run.py, uvicorn reloads, or systemd-started daemons all coexist
    # without double-spawn.
    _start_wdndds_daemon()

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )


if __name__ == "__main__":
    main()