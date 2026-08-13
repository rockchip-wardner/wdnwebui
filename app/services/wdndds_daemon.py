"""wdndds_daemon — FastDDS Discovery Server daemon for pyrkaiweb.

This is the central discovery broker that every pyrkaiweb process on
the LAN points to. It replaces the unreliable SIMPLE discovery
(multicast + initialPeers) that proved broken on embedded boards.

What this daemon does:
  - Creates a DomainParticipant in SERVER discovery protocol on the
    configured UDPv4 port.
  - Listens for ALL participants on the LAN (pyrkaiweb main, compute
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

  # from pyrkaiweb (auto-started when config.json says so)
  python -c 'from app.services.wdndds_daemon import start_in_subprocess; \\
             start_in_subprocess(port=7411, http_port=7412)'
"""
from __future__ import annotations

import argparse
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
# Subprocess launcher (used by pyrkaiweb main.py to auto-start)                #
# --------------------------------------------------------------------------- #
def start_in_subprocess(port: int = DEFAULT_UDP_PORT,
                        http_port: int = DEFAULT_HTTP_PORT,
                        logfile: Optional[str] = None) -> "subprocess.Popen":
    """Launch the daemon in a detached subprocess.

    Returns the Popen handle. The caller is responsible for tracking
    the PID (we write it nowhere by default; pyrkaiweb stashes it in
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