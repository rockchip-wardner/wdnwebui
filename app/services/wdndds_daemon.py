"""wdndds_daemon — FastDDS Discovery Server daemon for wdnwebui.

This is the central discovery broker that every wdnwebui process on
the LAN points to. It replaces the unreliable SIMPLE discovery
(multicast + initialPeers) that proved broken on embedded boards.

What this daemon does:
  - Creates a DomainParticipant in SERVER discovery protocol on the
    configured UDPv4 port.
  - Listens for ALL participants on the LAN (wdnwebui main, compute
    workers, im-bridge bot, …) and brokers their discovery metadata
    to each other.
  - Exposes a small HTTP REST API for the UI to query daemon state
    (liveness + discovered participants).
  - Persists no state; on restart, all clients re-register.

What this daemon does NOT do:
  - Carry compute payloads. The actual per-model RPC payloads still
    flow via SHM (same host) or TCPv4 (cross-host) — discovery is
    only the broker, not the data plane.
  - Authenticate or rate-limit. LAN trust only. Production deployments
    behind a firewall should restrict the discovery port (default
    7411) to the LAN subnet.

Wire format:
  Discovery: SERVER protocol on UDPv4 (default 7411)
  REST:     HTTP/1.1 on TCP (default 7412) — GET /status, GET /peers

Usage:
  # foreground
  python -m app.services.wdndds_daemon --udp-port 7411 --http-port 7412

  # daemonize (double-fork)
  python -m app.services.wdndds_daemon --udp-port 7411 --http-port 7412 \\
      --pidfile /var/run/wdndds-daemon.pid --logfile /var/log/wdndds-daemon.log

  # from app (auto-started when config.json says so)
  python -c 'from app.services.wdndds_daemon import start_in_subprocess; \\
             start_in_subprocess(port=7411, http_port=7412)'
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import http.server
import json
import logging
import os
import signal
import socket
import socketserver
import sys
import threading
import time
from typing import Optional


_LOG = logging.getLogger("wdndds.daemon")

# Default ports — overridable via CLI.
DEFAULT_UDP_PORT = 7411
DEFAULT_HTTP_PORT = 7412

# Process comm name (visible in ``ps -c`` / ``top`` / ``/proc/<pid>/comm``).
# Matches the systemd unit filename in ``install_autostart.sh`` so
# ``pgrep -f`` / process listings all line up. 14 bytes — fits in
# TASK_COMM_LEN - 1 = 15.
DAEMON_COMM_NAME = "wdndds-daemon"

# Global state for the REST handler.
_state_lock = threading.Lock()
_state: dict = {
    "started_at": 0.0,
    "udp_port": DEFAULT_UDP_PORT,
    "guid": "",
    "participant_count": 0,
    "peers": [],   # [{guid, name, host}, ...]  best-effort
}


# --------------------------------------------------------------------------- #
# Process comm (comm field) — for auto-start dedup                            #
# --------------------------------------------------------------------------- #
_PR_SET_NAME = 15
_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_libc.prctl.restype = ctypes.c_int
_libc.prctl.argtypes = [
    ctypes.c_int,                       # option
    ctypes.c_char_p,                    # arg2
    ctypes.c_ulong, ctypes.c_ulong,     # arg3, arg4
    ctypes.c_ulong,                     # arg5
]


def _set_comm_name(name: str) -> None:
    """Set this process's comm (visible in ``ps -c`` / ``top`` /
    ``/proc/<pid>/comm``) via ``prctl(PR_SET_NAME)``.

    Truncated to 15 bytes (TASK_COMM_LEN - 1). The main process's
    :func:`_find_daemon_pids` walks ``/proc/*/comm`` looking for
    :data:`DAEMON_COMM_NAME` so the auto-start path can skip when one
    is already up (uvicorn reload otherwise leaves an orphan per
    restart, same problem compute_worker had).

    Why not preexec_fn? See compute_worker._set_comm_name for the
    full story; tl;dr preexec_fn's PR_SET_NAME is immediately
    overwritten by the subsequent execvp (argv[0] becomes new comm).
    Setting it here, after Python has already started, sticks.
    """
    name_bytes = name.encode("utf-8")[:15]
    try:
        _libc.prctl(_PR_SET_NAME, name_bytes, 0, 0, 0)
    except Exception as e:  # noqa: BLE001
        _LOG.warning("set comm name to %r failed: %s", name, e)


def find_daemon_pids(comm_name: str = DAEMON_COMM_NAME,
                     exclude_self: bool = True) -> list[int]:
    """Walk ``/proc/*/comm`` looking for processes whose comm equals
    :data:`DAEMON_COMM_NAME`. Used by the main process's
    ``_spawn_daemon_once`` to skip Popen when one is already running.

    Falls back to a cmdline substring match for legacy daemons started
    before the comm was renamed (comm == ``"python"`` + cmdline
    contains ``app.services.wdndds_daemon``). That branch becomes a
    no-op once every old daemon has been replaced.

    Returns the list of matching PIDs (possibly several during a
    restart window). The caller is expected to treat "any match" as
    "already running".
    """
    own_pid = os.getpid() if exclude_self else -1
    comm_needle = comm_name.encode("utf-8")
    cmdline_needle = b"app.services.wdndds_daemon"
    pids: list[int] = []
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid == own_pid:
                continue
            try:
                with open(f"/proc/{pid}/comm", "rb") as f:
                    comm = f.read().rstrip(b"\x00").rstrip()
            except (OSError, FileNotFoundError):
                continue
            if comm == comm_needle:
                pids.append(pid)
                continue
            # Legacy: daemon started before PR_SET_NAME was added;
            # comm is still "python", match by cmdline substring.
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmdline = f.read()
            except (OSError, FileNotFoundError):
                continue
            if cmdline_needle in cmdline:
                pids.append(pid)
    except OSError:
        # /proc not mounted (containers without procfs etc.) — assume
        # nothing's running and let the caller Popen.
        pass
    return pids


# --------------------------------------------------------------------------- #
# wdndds Hub                                                                  #
# --------------------------------------------------------------------------- #
def _build_hub(udp_port: int, name: str = "wdndds-discovery-daemon"):
    """Create the daemon's DomainParticipant in SERVER mode."""
    from wdndds.dds._rpc import Hub
    # Server listens on its own host:port. We pass its own address so
    # FastDDS sets up the metatraffic unicast listener correctly.
    host = _detect_primary_host()
    csv = f"{host}:{udp_port}"
    hub = Hub(participant_name=name,
              domain_id=0,
              discovery_servers=[csv],
              is_discovery_server=True)
    hub.start()
    return hub


def _detect_primary_host() -> str:
    """Best-effort detection of the host's primary non-loopback IPv4."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return "127.0.0.1"


# --------------------------------------------------------------------------- #
# REST API                                                                    #
# --------------------------------------------------------------------------- #
class _DaemonHTTPHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Silence the default stderr spam; route via the daemon logger.
        _LOG.debug("http: " + (format % args))

    def do_GET(self):
        if self.path == "/status":
            body = self._snapshot()
        elif self.path == "/peers":
            body = self._peers()
        elif self.path == "/health":
            body = {"ok": True}
        else:
            self.send_error(404)
            return
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _snapshot(self) -> dict:
        with _state_lock:
            return {
                "running": True,
                "started_at": _state["started_at"],
                "uptime_sec": max(0.0, time.time() - _state["started_at"]),
                "udp_port": _state["udp_port"],
                "guid": _state["guid"],
                "participant_count": _state["participant_count"],
            }

    def _peers(self) -> dict:
        with _state_lock:
            return {"peers": list(_state["peers"])}


def _start_http_server(port: int) -> socketserver.TCPServer:
    class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = _Server(("0.0.0.0", port), _DaemonHTTPHandler)
    threading.Thread(target=server.serve_forever,
                     name="wdndds-daemon-http",
                     daemon=True).start()
    _LOG.info("HTTP API listening on :%d", port)
    return server


# --------------------------------------------------------------------------- #
# Participant listener — polls the daemon's Hub for peer changes              #
# --------------------------------------------------------------------------- #
def _poll_participants(hub, interval_sec: float = 2.0):
    """Background loop that updates the _state with the current peer list.

    FastDDS 3.6's get_discovered_participants returns RETCODE_UNSUPPORTED,
    so Hub.list_peers() is currently a no-op stub. This thread still
    keeps the daemon's state fresh — when we add proper enumeration
    in a follow-up, it will start working automatically.
    """
    last: list = []
    while True:
        try:
            peers = hub.list_peers()
            if peers != last:
                with _state_lock:
                    _state["peers"] = peers
                    _state["participant_count"] = len(peers)
                last = peers
        except Exception as e:  # noqa: BLE001
            _LOG.debug("poll_participants: %s", e)
        time.sleep(interval_sec)


# --------------------------------------------------------------------------- #
# Subprocess launcher (used by wdnwebui main.py to auto-start)                #
# --------------------------------------------------------------------------- #
def start_in_subprocess(port: int = DEFAULT_UDP_PORT,
                        http_port: int = DEFAULT_HTTP_PORT,
                        logfile: Optional[str] = None) -> "subprocess.Popen":
    """Launch the daemon in a detached subprocess.

    Returns the Popen handle. The caller is responsible for tracking
    the PID (we write it nowhere by default; wdnwebui stashes it in
    a state dict).
    """
    import subprocess
    args = [
        sys.executable, "-m", "app.services.wdndds_daemon",
        "--udp-port", str(port),
        "--http-port", str(http_port),
    ]
    if logfile:
        args += ["--logfile", logfile]
    stdin = subprocess.DEVNULL
    stdout = subprocess.DEVNULL if logfile else None
    stderr = subprocess.DEVNULL if logfile else None
    return subprocess.Popen(
        args,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
    )


def is_daemon_running(port: int = DEFAULT_UDP_PORT,
                      http_port: int = DEFAULT_HTTP_PORT) -> bool:
    """Quick check: is the daemon's REST API responding?"""
    try:
        with socket.create_connection(("127.0.0.1", http_port), timeout=1.0):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Main                                                                        #
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(prog="wdndds_daemon")
    ap.add_argument("--udp-port", type=int, default=DEFAULT_UDP_PORT,
                    help=f"UDPv4 discovery port (default {DEFAULT_UDP_PORT})")
    ap.add_argument("--http-port", type=int, default=DEFAULT_HTTP_PORT,
                    help=f"HTTP REST port (default {DEFAULT_HTTP_PORT})")
    ap.add_argument("--pidfile", type=str, default=None,
                    help="Write our PID here")
    ap.add_argument("--logfile", type=str, default=None,
                    help="Redirect stdout+stderr here")
    args = ap.parse_args()

    # Rename our comm BEFORE the hub starts so the main process's
    # auto-start dedup (``_find_daemon_pids`` walks /proc/*/comm)
    # can see us immediately. Must come after argparse / sys imports
    # — preexec_fn PR_SET_NAME would be clobbered by execvp, so we
    # set it here in main() once Python is fully up.
    _set_comm_name(DAEMON_COMM_NAME)

    # Set up logging
    log_handlers = [logging.StreamHandler(stream=sys.stderr)]
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        handlers=log_handlers,
    )

    if args.pidfile:
        with open(args.pidfile, "w") as f:
            f.write(str(os.getpid()))

    _LOG.info("starting wdndds discovery daemon "
              "(udp_port=%d http_port=%d pid=%d)",
              args.udp_port, args.http_port, os.getpid())

    hub = _build_hub(args.udp_port)

    def _on_peer_event(guid: str, name: str, discovered: bool):
        with _state_lock:
            if discovered:
                # Replace any existing entry for this guid (e.g. name
                # changed or we just observed a rediscovery).
                _state["peers"] = [p for p in _state["peers"] if p["guid"] != guid]
                _state["peers"].append({"guid": guid, "name": name})
                _LOG.info("peer + %s %s (%d total)",
                          guid[:20], name, len(_state["peers"]))
            else:
                _state["peers"] = [p for p in _state["peers"] if p["guid"] != guid]
                _LOG.info("peer - %s %s (%d total)",
                          guid[:20], name, len(_state["peers"]))
            _state["participant_count"] = len(_state["peers"])

    hub.set_peer_listener(_on_peer_event)

    with _state_lock:
        _state["started_at"] = time.time()
        _state["udp_port"] = args.udp_port
        _state["guid"] = hub.guid()
        _state["peers"] = []
        _state["participant_count"] = 0

    http_server = _start_http_server(args.http_port)
    poll_thread = threading.Thread(
        target=_poll_participants, args=(hub,),
        name="wdndds-daemon-poll",
        daemon=True)
    poll_thread.start()

    stop_event = threading.Event()
    def _on_signal(signum, frame):
        _LOG.info("received signal %d — shutting down", signum)
        stop_event.set()
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        while not stop_event.is_set():
            stop_event.wait(timeout=1.0)
    finally:
        _LOG.info("shutting down")
        try:
            hub.close()
        except Exception as e:  # noqa: BLE001
            _LOG.warning("hub.close: %s", e)
        try:
            http_server.shutdown()
        except Exception:
            pass
        if args.pidfile:
            try:
                os.unlink(args.pidfile)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())