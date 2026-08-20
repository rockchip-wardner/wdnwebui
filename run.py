"""Entry point for wdnwebui service.

Usage:
    python run.py
    python run.py --config /etc/wdnwebui/config.json
    # or
    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
import argparse
import os
import signal
import subprocess
import sys
import time
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


def _pid_listening_on_port(port: int) -> int | None:
    """Return the PID of the process holding TCP ``port`` in LISTEN, or
    ``None``. Uses ``ss -tlnp`` (iproute2) which is universal on Linux
    and avoids shelling to ``lsof`` (not always installed).
    """
    try:
        out = subprocess.run(
            ["ss", "-tlnp", f"sport = :{port}"],
            capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    # Output line ends with ``users:(("python",pid=1234,fd=5))``.
    import re
    for line in out.stdout.splitlines():
        m = re.search(r"pid=(\d+)", line)
        if m:
            return int(m.group(1))
    return None


def _ancestor_pids() -> set[int]:
    """Walk ``/proc/*/status`` PPid chain back to PID 1. Returns the
    set of PIDs that are ancestors of (or equal to) ourselves —
    killing any of these would also kill us, so they MUST be
    excluded from the kill set.
    """
    out = {os.getpid()}
    pid = os.getpid()
    for _ in range(64):  # safety cap; realistic chains are < 10
        try:
            with open(f"/proc/{pid}/status") as f:
                ppid = None
                for line in f:
                    if line.startswith("PPid:"):
                        ppid = int(line.split()[1])
                        break
            if ppid is None or ppid <= 1:
                break
            if ppid in out:
                break  # cycle / already-seen
            out.add(ppid)
            pid = ppid
        except OSError:
            break
    return out


def _kill_stale_wdnwebui_processes(enabled: bool) -> None:
    """Best-effort cleanup of stale wdnwebui processes from a previous
    unclean shutdown. Kills **every** wdnwebui-flavored process — the
    previous uvicorn (port 8000), the prior ``run.py`` + uvicorn-reloader
    spawn/resource_tracker children, **all orphan ``compute_worker.py``
    subprocesses**, and the **wdndds_daemon** if one is already running
    — so the new boot starts from a clean slate with no BrokenPipe
    pollution from dead parents and no stale NPU model sessions.

    Trade-off vs. the old preserve-and-adopt design: each restart now
    pays the cost of rebuilding the daemon's FastDDS endpoints (peers
    re-register on next announce) and re-instantiating every model's
    ``LLMSession`` (~55s per model on RK3588). For dev / single-host
    runs this is the right trade — the previous "preserve" design
    surfaced BrokenPipeError pollution and 60s+ reload crossover bugs
    whenever the old main process died ungracefully. Production hosts
    that genuinely want adoption semantics should invoke ``uvicorn
    app.main:app`` directly (the systemd unit installed by
    ``install_autostart.sh`` does this) and skip ``run.py`` entirely.

    Opt-out: pass ``enabled=False`` (``--no-kill-stale`` /
    ``WDNWEBUI_KILL_STALE_ON_BOOT=0``). When disabled the entire
    /proc scan is skipped.

    Excluded from the kill list no matter what:
      * our own ancestor chain (would kill us)
      * anything not matching wdnwebui repo paths (defence in depth)
    """
    if not enabled:
        return

    repo_root = Path(__file__).resolve().parent
    ancestors = _ancestor_pids()
    my_pid = os.getpid()
    killed: list[tuple[str, int]] = []

    def _collect() -> list[tuple[str, int]]:
        """Walk ``/proc`` once; return all wdnwebui-related PIDs to kill.

        Detection rules (all scoped to this repo's repo_root / CWD /
        .venv python to avoid touching foreign processes):
          (a) ``uvicorn`` holding port 8000 — the actual blocker
          (b) ``python run.py`` ancestor (the reloader's parent)
          (c) uvicorn-reloader's ``multiprocessing.spawn`` /
              ``resource_tracker`` children
          (d) any ``compute_worker.py`` subprocess (orphan or not)
          (e) ``wdndds_daemon`` matched via comm-set detection
        """
        candidates: list[tuple[str, int]] = []

        # (a) uvicorn holding port 8000 — the actual blocker.
        holder = _pid_listening_on_port(8000)
        if holder and holder != my_pid and holder not in ancestors:
            candidates.append(("uvicorn(port 8000)", holder))

        worker_path = repo_root / "app" / "services" / "compute_worker.py"
        try:
            worker_path_str = str(worker_path.resolve())
        except OSError:
            worker_path_str = str(worker_path)
        repo_python_marker = str(repo_root / ".venv" / "bin" / "python")

        try:
            pids = sorted(int(p) for p in os.listdir("/proc") if p.isdigit())
        except OSError:
            pids = []
        for pid in pids:
            if pid == my_pid or pid in ancestors:
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    raw = f.read()
            except OSError:
                continue
            if not raw:
                continue
            try:
                cmdline = raw.split(b"\0")
                cmd_str = raw.replace(b"\0", b" ").decode("utf-8", "replace")
            except Exception:
                continue

            argv = [a for a in cmdline if a]
            has_run_py = any(a.endswith(b"run.py") for a in argv)
            has_repo_python = any(
                a.endswith(b"/python") or a.endswith(b"/python3")
                for a in argv
            )

            # (b) stale ``python run.py`` — the actual uvicorn parent.
            if len(argv) >= 2 and has_run_py and has_repo_python:
                try:
                    cwd_target = os.readlink(f"/proc/{pid}/cwd")
                except OSError:
                    continue
                if cwd_target == str(repo_root):
                    candidates.append(("run.py", pid))
                    continue

            try:
                exe_link = os.readlink(f"/proc/{pid}/exe")
            except OSError:
                exe_link = ""

            # (c) uvicorn-reloader multiprocessing children.
            if (b"multiprocessing.spawn" in raw
                    or b"multiprocessing.resource_tracker" in raw) \
                    and repo_python_marker in exe_link:
                candidates.append(("uvicorn-reload-fork", pid))
                continue

            # (d) orphan ``compute_worker.py``. The triple filter
            # (bare filename + resolved path + repo name) excludes
            # foreign workers from another dev box.
            if ("compute_worker.py" in cmd_str
                    and worker_path_str in cmd_str
                    and "wdnwebui" in cmd_str):
                candidates.append(("compute_worker.py", pid))
                continue

            # (e) wdndds_daemon via cmdline catches those that haven't
            # had prctl renamed them yet.
            if (b"wdndds_daemon" in raw
                    and repo_python_marker in exe_link):
                candidates.append(("wdndds_daemon", pid))
                continue

        # (e') comm-based daemon detection via /proc/PID/comm; the
        # prctl-set name is not visible in cmdline.
        try:
            for pid in wdndds_daemon.find_daemon_pids(exclude_self=True):
                if pid != my_pid and pid not in ancestors:
                    candidates.append(("wdndds_daemon", pid))
        except Exception:
            pass

        return candidates

    candidates = _collect()

    if not candidates:
        return

    print(
        f"[run.py] kill-stale: cleaning up {len(candidates)} stale "
        f"process(es) from a previous run.py invocation:",
        file=sys.stderr,
    )
    for label, pid in candidates:
        print(f"  - {label} pid={pid}", file=sys.stderr)

    # SIGTERM with 3s grace. For "run.py" candidates we also walk
    # children (deepest first) and signal each descendant — covers
    # uvicorn's reload child + the actual worker + compute_workers
    # spawned via Popen with start_new_session, all without touching
    # unrelated processes in the same process group (the shell that
    # launched us via nohup shares the pgrp).
    def _signal_pid(pid: int, sig: int) -> None:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    # Expand "run.py" candidates into their full descendant tree.
    expanded: list[tuple[str, int]] = list(candidates)
    for label, pid in candidates:
        if label == "run.py":
            for child in _descendant_pids(pid):
                expanded.append((f"{label}.child", child))

    for label, pid in expanded:
        _signal_pid(pid, signal.SIGTERM)

    deadline = time.time() + 3.0
    pending = list(expanded)
    while pending and time.time() < deadline:
        time.sleep(0.2)
        pending = [(lbl, p) for lbl, p in pending
                   if _pid_alive(p)]
    survivors = pending

    # SIGKILL anything that didn't honour SIGTERM.
    if survivors:
        print(
            f"[run.py] kill-stale: SIGKILL {len(survivors)} survivor(s) "
            f"after 3s grace",
            file=sys.stderr,
        )
        for label, pid in survivors:
            _signal_pid(pid, signal.SIGKILL)
        # Give the kernel a moment to release sockets.
        time.sleep(0.5)

    # Post-kill verification: if port 8000 is STILL occupied, the
    # original sweep missed something (common when the previous run
    # was launched under a wrapper like ``nohup`` that stripped the
    # argv path or the holder's cmdline is unreadable). Walk the
    # parent's PPid chain until we find a wdnwebui-flavored ancestor
    # and kill its process group as a last resort. If even that
    # can't release the port, leave a loud hint and let ``uvicorn.run``
    # raise the [Errno 98] — caller can then ``kill -9`` by hand.
    new_holder = _pid_listening_on_port(8000)
    if new_holder and new_holder != my_pid and new_holder not in ancestors:
        ancestor = _find_wdnwebui_ancestor(new_holder, repo_root, ancestors)
        if ancestor and ancestor not in {p for _, p in expanded}:
            print(
                f"[run.py] kill-stale: port 8000 still held by pid={new_holder}; "
                f"killing wdnwebui ancestor pid={ancestor} (+ descendants)",
                file=sys.stderr,
            )
            for child in _descendant_pids(ancestor):
                _signal_pid(child, signal.SIGKILL)
            _signal_pid(ancestor, signal.SIGKILL)
            time.sleep(0.5)
            # If STILL busy (foreign process holding 8000), surface it
            # so the user knows what to kill manually.
            leftover = _pid_listening_on_port(8000)
            if leftover and leftover != my_pid and leftover not in ancestors:
                try:
                    with open(f"/proc/{leftover}/cmdline", "rb") as f:
                        cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
                except OSError:
                    cmd = "(unreadable)"
                print(
                    f"[run.py] kill-stale: WARNING port 8000 STILL held by "
                    f"pid={leftover} cmd='{cmd}' — kill -9 manually",
                    file=sys.stderr,
                )

    for label, pid in candidates:
        if not _pid_alive(pid):
            killed.append((label, pid))

    if killed:
        print(
            f"[run.py] kill-stale: {len(killed)} process(es) reaped "
            f"before binding port 8000",
            file=sys.stderr,
        )


def _find_wdnwebui_ancestor(
    pid: int, repo_root: Path, exclude: set[int]
) -> int | None:
    """Walk ``pid``'s PPid chain looking for a process whose CWD is
    ``repo_root`` AND whose cmdline contains ``run.py`` AND whose
    interpreter is the repo's ``.venv/bin/python``. Returns the topmost
    such ancestor, or ``None`` if the chain breaks / no match.

    Used by the post-kill verification path: when the original
    ``_kill_stale_wdnwebui_processes`` sweep missed the port 8000
    holder (e.g. it was launched under ``nohup`` and the wrapper
    proc owns the LISTEN socket), we walk up from the holder to find
    a wdnwebui process we can safely kill the whole group of.
    """
    seen: set[int] = set()
    cur = pid
    result: int | None = None
    for _ in range(64):
        if cur in seen or cur in exclude or cur <= 1:
            break
        seen.add(cur)
        try:
            with open(f"/proc/{cur}/status") as f:
                ppid = None
                for line in f:
                    if line.startswith("PPid:"):
                        ppid = int(line.split()[1])
                        break
            if ppid is None:
                break
        except OSError:
            break
        try:
            with open(f"/proc/{cur}/cmdline", "rb") as f:
                raw = f.read()
            cwd = os.readlink(f"/proc/{cur}/cwd")
        except OSError:
            if ppid <= 1:
                break
            cur = ppid
            continue
        argv = [a for a in raw.split(b"\0") if a]
        if (
            cwd == str(repo_root)
            and any(a.endswith(b"run.py") for a in argv)
            and any(a.endswith(b"/python") or a.endswith(b"/python3") for a in argv)
        ):
            result = cur
        if ppid <= 1:
            break
        cur = ppid
    return result


def _descendant_pids(pid: int) -> list[int]:
    """Return all descendant PIDs of ``pid`` (children, grand-children, …)
    in post-order (deepest first). Reads
    ``/proc/<pid>/task/<pid>/children`` which the kernel maintains
    automatically — no need to walk /proc on each candidate. Excludes
    ``pid`` itself; caller signals the root separately.
    """
    out: list[int] = []
    stack = [pid]
    while stack:
        cur = stack.pop()
        try:
            with open(f"/proc/{cur}/task/{cur}/children") as f:
                raw = f.read().strip()
        except OSError:
            continue
        if not raw:
            continue
        for tok in raw.split():
            try:
                child = int(tok)
            except ValueError:
                continue
            stack.append(child)
            out.append(child)
    return out


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _ensure_wdndds_built() -> None:
    """Fail fast if wdndds native libs are not built.

    wdndds 的 ``libwdndds_cshim.so`` + ``libwdndds_idl.so`` 不在 wheel
    里 — 由 ``wdndds._build.py``（setuptools ``build_py`` 子类）在
    ``pip install`` 时从 ``idl/`` + ``cshim_src/`` 源码编译到
    ``.venv/src/wdndds/src/wdndds/dds/_native/``。wheel /
    ``pip install -e`` 都没有自动跑 build hook 时（apt 依赖缺失、
    fresh clone 跳过 ``build_native.sh`` 等），这两个文件就缺失，
    后续 ``wdndds_daemon`` 和 ``ComputeRegistry`` 的 ``RpcClient``
    加载 cshim 时会报
    ``FileNotFoundError: wdndds: .../libwdndds_cshim.so not found``。

    本函数是 fail-fast 网关：**不在这里自动 build**（apt 需要 sudo、
    会卡 service 启动），只在缺失时直接 ``SystemExit`` 并指向
    ``bootstrap_wdndds_native.sh``，让运维显式跑一次完成 apt + 编译。
    脚本是幂等的：再次执行是 no-op（<1s）。

    Opt-out: 设置 ``WDNWEBUI_SKIP_WDNDDS_BOOTSTRAP=1``（用于生产环境
    通过其它机制预置 .so 文件的场景，比如 basebox 镜像预装）。
    """
    if os.environ.get("WDNWEBUI_SKIP_WDNDDS_BOOTSTRAP") == "1":
        return

    repo_root = Path(__file__).resolve().parent
    wdndds_src = repo_root / ".venv" / "src" / "wdndds"
    cshim = (
        wdndds_src
        / "src" / "wdndds" / "dds" / "_native" / "libwdndds_cshim.so"
    )
    idl = (
        wdndds_src
        / "src" / "wdndds" / "dds" / "_native" / "libwdndds_idl.so"
    )

    if cshim.is_file() and idl.is_file():
        return

    bootstrap = repo_root / "bootstrap_wdndds_native.sh"
    print(
        f"[run.py] FATAL: wdndds native libraries not built.\n"
        f"  missing: {cshim}\n"
        f"           {idl}\n"
        f"\n"
        f"Run once on this host:\n"
        f"  sudo {bootstrap}\n"
        f"\n"
        f"The script installs apt build deps (libfoonathan-memory-dev,\n"
        f"libfoonathan-memory0.7.2, patchelf), patches the partial-native\n"
        f"cmake stub, and runs pip install to trigger the wdndds build_py\n"
        f"hook that compiles libwdndds_cshim.so + libwdndds_idl.so.\n"
        f"It is idempotent — re-running it after a successful bootstrap\n"
        f"is a no-op.\n"
        f"\n"
        f"To bypass this check (production: .so files pre-staged via a\n"
        f"different mechanism):\n"
        f"  WDNWEBUI_SKIP_WDNDDS_BOOTSTRAP=1 .venv/bin/python run.py",
        file=sys.stderr,
    )
    raise SystemExit(1)


_ensure_wdndds_built()
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
    parser.add_argument(
        "--kill-stale",
        action="store_true", default=None,
        help="Reap stale wdnwebui MAIN processes (port 8000 uvicorn + "
             "uvicorn-reloader fork + prior run.py) from a previous "
             "unclean shutdown before binding. Orphan compute_worker "
             "processes are intentionally NOT killed — they're "
             "independently managed and adopted by "
             "ComputeRegistry.boot_compute_services() on the new boot. "
             "Default: ON for manual CLI runs. Explicit opt-out: "
             "--no-kill-stale.",
    )
    parser.add_argument(
        "--no-kill-stale",
        action="store_true", default=None,
        help="Skip stale MAIN-process cleanup. Inverse of --kill-stale; "
             "the last flag wins if both are passed. compute_workers "
             "are never killed by run.py regardless of this flag.",
    )
    args, _unknown = parser.parse_known_args()

    # Resolve kill-stale default: env override > CLI > default ON.
    # Default is ON because run.py is only invoked manually (the
    # production systemd unit invokes uvicorn directly — see
    # install_autostart.sh), and manual runs almost always hit
    # leftovers from a previous unclean shutdown.
    env_kill = os.environ.get("WDNWEBUI_KILL_STALE_ON_BOOT", "").strip()
    if env_kill == "0":
        kill_stale = False
    elif env_kill == "1":
        kill_stale = True
    elif args.no_kill_stale and not args.kill_stale:
        kill_stale = False
    elif args.kill_stale and not args.no_kill_stale:
        kill_stale = True
    else:
        # both / neither — default behaviour is ON.
        kill_stale = True

    config_path = args.config.expanduser()
    if not config_path.is_absolute():
        config_path = (Path(__file__).resolve().parent / config_path).resolve()
    else:
        config_path = config_path.resolve()

    # Propagate to the app via env so app.services.config picks it up
    # before any FastAPI route is hit.
    os.environ["WDNWEBUI_CONFIG"] = str(config_path)

    # Reap stale uvicorn (port 8000) + compute_workers from any
    # previous unclean shutdown BEFORE we try to bind. Done before
    # _start_wdndds_daemon() so the daemon sees a clean slate too
    # (and so we don't kill anything spawned by the daemon we just
    # brought up).
    _kill_stale_wdnwebui_processes(kill_stale)

    # Bring up the FastDDS discovery daemon before uvicorn binds the
    # web port. _start_wdndds_daemon() is idempotent — it walks
    # /proc/*/comm for the daemon's prctl-set name ("wdndds-daemon")
    # and skips Popen when one is already running, so re-invoking
    # run.py, uvicorn reloads, or systemd-started daemons all coexist
    # without double-spawn.
    _start_wdndds_daemon()

    # VCS extract over a long source runs 50-75s; uvicorn's default 20s WS
    # ping often times the connection out mid-extract so the progress bar
    # only resyncs on reconnect (visible as a single 0% → 100% jump).
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        ws_ping_interval=5.0,
        ws_ping_timeout=60.0,
    )


if __name__ == "__main__":
    main()