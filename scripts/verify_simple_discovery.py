#!/usr/bin/env python3
"""verify_simple_discovery.py — minimal end-to-end check that FastDDS 3.6
SIMPLE discovery works for wdnwebui's wdndds cshim.

Background
----------
The wdndds cshim (``wdndds/cshim_src/wdndds_cshim.cpp``) falls through to
FastDDS' default SIMPLE discovery (``DiscoveryProtocol::SIMPLE`` + UDP
multicast on 239.255.0.1:7400 + the ``initialPeersList`` from the
auto-installed XML profile at ``$TMPDIR/wdndds_default_profile.xml``)
whenever the caller passes ``discovery_servers=[]`` (or omits it).
This script exercises that path so we can validate it on a real network
before / after the SIMPLE-only migration (v0.6+).

Usage
-----
Two terminals / two hosts. One runs ``server``, the other runs
``client``. Both talk via SIMPLE discovery.

    # On host A:
    .venv/bin/python scripts/verify_simple_discovery.py server \\
        --name board-A

    # On host B (or same host):
    .venv/bin/python scripts/verify_simple_discovery.py client \\
        --name board-B

The ``client`` mode prints whether it ever discovered the server peer,
how long it took, and whether the end-to-end RPC call succeeded.

A third mode ``probe-only`` is also useful for diagnosing multicast
without doing any RPC — it just listens for peer events and prints them.

Exit codes
----------
    0 — peer discovered AND RPC round-trip succeeded (or probe-only: at
        least one peer seen within the timeout window)
    1 — peer discovered but RPC round-trip FAILED
    2 — no peer discovered within ``--timeout`` seconds
    3 — wdndds cshim init failed (FastDDS / libwdndds_cshim missing)

This script does NOT depend on the wdnwebui app, ComputeRegistry,
the wdndds SDK, or any router / middleware — only on ``wdndds`` itself
plus a working FastDDS 3.6 + libwdndds_cshim.so install.

Override the SIMPLE ``initialPeersList`` (unicast fallback when
multicast is blocked) by setting ``WDNDDS_INITIAL_PEERS`` to a
comma-separated list of IPs *before* importing wdndds:

    WDNDDS_INITIAL_PEERS=192.168.1.10,192.168.1.11 \\
        .venv/bin/python scripts/verify_simple_discovery.py server
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import threading
import time
from typing import Optional


LOG = logging.getLogger("verify.simple")


# --------------------------------------------------------------------------- #
# WDNDDS_INITIAL_PEERS → FastDDS initialPeersList
# --------------------------------------------------------------------------- #
def _install_initial_peers_profile() -> None:
    """Inject WDNDDS_INITIAL_PEERS into FastDDS initialPeersList as
    a unicast-fallback escape hatch when LAN multicast is blocked
    (consumer WiFi AP isolation, restrictive VLANs, Docker bridge)."""
    peers_env = os.environ.get("WDNDDS_INITIAL_PEERS", "").strip()
    if not peers_env:
        return
    peers = [p.strip() for p in peers_env.split(",") if p.strip()]
    if not peers:
        return

    locator_xml = "\n".join(
        f"""                    <locator>
                        <udpv4>
                            <address>{p}</address>
                            <port>7410</port>
                        </udpv4>
                    </locator>"""
        for p in peers
    )

    candidates = [
        os.environ.get("XDG_RUNTIME_DIR"),
        os.environ.get("TMPDIR"),
        os.environ.get("TMP"),
        "/tmp",
        "/var/tmp",
    ]
    target_dir = next((d for d in candidates if d and d != ""), "/tmp")
    if not target_dir.endswith("/"):
        target_dir += "/"
    path = f"{target_dir}wdndds_initial_peers_profile.xml"
    try:
        with open(path, "w") as f:
            f.write(
                '<?xml version="1.0" encoding="UTF-8" ?>\n'
                '<profiles xmlns="http://www.eprosima.com/XMLSchemas/fastRTPS_Profiles">\n'
                '    <participant profile_name="wdndds_initial_peers" '
                'is_default_profile="true">\n'
                '        <rtps>\n'
                '            <builtin>\n'
                '                <initialPeersList>\n'
                f'{locator_xml}\n'
                '                </initialPeersList>\n'
                '            </builtin>\n'
                '        </rtps>\n'
                '    </participant>\n'
                '</profiles>\n'
            )
        # Override whatever _cshim.py auto-installed.
        os.environ["FASTRTPS_DEFAULT_PROFILES_FILE"] = path
        LOG.info("WDNDDS_INITIAL_PEERS=%s → wrote profile %s",
                 peers, path)
    except OSError as e:
        LOG.warning("failed to write initial-peers profile %s: %s",
                    path, e)


_install_initial_peers_profile()


# --------------------------------------------------------------------------- #
# Mode: server
# --------------------------------------------------------------------------- #
def run_server(args: argparse.Namespace) -> int:
    """Bring up a Hub hosting a single echo service. Log peer events."""
    from wdndds.dds._rpc import Hub

    peer_events: list[tuple[float, str, str, bool]] = []
    peer_lock = threading.Lock()

    def _on_peer_event(guid: str, name: str, ip: str, discovered: bool) -> None:
        ts = time.time()
        with peer_lock:
            peer_events.append((ts, guid, name, ip, discovered))
        event_str = "DISCOVERED" if discovered else "REMOVED"
        LOG.info("[%6.2fs] peer %s guid=%s name=%s ip=%s",
                 ts - peer_events[0][0] if peer_events else 0.0,
                 event_str, guid[:24], name, ip or "--")

    def _router(svc_name: str, payload: dict) -> dict:
        LOG.info("[server] got request svc=%s payload=%s",
                 svc_name, payload)
        return {
            "ok": True,
            "echo": payload,
            "server_name": args.name,
            "server_host": socket.gethostname(),
        }

    LOG.info("server mode: bringing up Hub name=%s (SIMPLE discovery)",
             args.name)
    hub = Hub(
        participant_name=args.name,
        domain_id=args.domain,
        services=[args.service],
        callback=_router,
        discovery_servers=[],   # ← forces SIMPLE (no daemon)
    )
    try:
        hub.start()
    except Exception as e:  # noqa: BLE001
        LOG.error("hub.start() failed: %s", e)
        return 3

    LOG.info("hub started: guid=%s service=%s",
             hub.guid()[:24], args.service)
    hub.set_peer_listener(_on_peer_event)

    stop = threading.Event()
    def _on_signal(signum, frame):
        LOG.info("received signal %d, stopping", signum)
        stop.set()
    import signal
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    LOG.info("server running — Ctrl-C to stop")
    try:
        while not stop.is_set():
            stop.wait(timeout=1.0)
    finally:
        try:
            hub.close()
        except Exception:
            pass

    LOG.info("final peer log:")
    with peer_lock:
        for ts, guid, name, ip, discovered in peer_events:
            LOG.info("  +%6.2fs  %s  guid=%s  name=%s ip=%s",
                     ts - peer_events[0][0], "DISCOVERED" if discovered else "REMOVED",
                     guid[:24], name, ip or "--")
    return 0


# --------------------------------------------------------------------------- #
# Mode: client
# --------------------------------------------------------------------------- #
def run_client(args: argparse.Namespace) -> int:
    """Bring up a Hub, wait for any peer (or a named peer) to be visible,
    then attempt one RPC round-trip."""
    from wdndds.dds._rpc import Hub

    discovery_complete = threading.Event()
    discovery_event = threading.Event()  # set when *any* peer arrives
    found_server = threading.Event()     # set when the named server arrives
    rpc_done = threading.Event()
    rpc_result: dict = {}
    first_peer_at: list[float] = []

    def _on_peer_event(guid: str, name: str, ip: str, discovered: bool) -> None:
        if not discovered:
            return
        if not first_peer_at:
            first_peer_at.append(time.time())
            LOG.info("first peer discovered: guid=%s name=%s",
                     guid[:24], name)
            discovery_event.set()
        if name == args.name:
            LOG.info("target server discovered: guid=%s name=%s",
                     guid[:24], name)
            found_server.set()
        if found_server.is_set() and not discovery_complete.is_set():
            discovery_complete.set()

    LOG.info("client mode: bringing up Hub (SIMPLE discovery), "
             "waiting for peer name=%s", args.name)
    hub = Hub(
        participant_name=f"client-{socket.gethostname()}-{os.getpid()}",
        domain_id=args.domain,
        discovery_servers=[],   # ← forces SIMPLE
    )
    try:
        hub.start()
    except Exception as e:  # noqa: BLE001
        LOG.error("hub.start() failed: %s", e)
        return 3

    LOG.info("hub started: guid=%s", hub.guid()[:24])
    hub.set_peer_listener(_on_peer_event)

    deadline = time.time() + args.timeout
    LOG.info("probing for peer (timeout=%ds)...", args.timeout)
    while time.time() < deadline and not found_server.is_set():
        time.sleep(0.2)

    if not found_server.is_set():
        LOG.error("NO peer with name=%s discovered within %ds",
                  args.name, args.timeout)
        try:
            hub.close()
        except Exception:
            pass
        return 2

    elapsed = time.time() - (first_peer_at[0] if first_peer_at else time.time())
    LOG.info("target peer visible after ~%.2fs", elapsed)

    # Now attempt the RPC. The cshim's wdndds_requester_call has an
    # internal 5s discovery-grace wait for SEDP endpoint matching on
    # a fresh requester; for that reason we sleep briefly before the
    # call so the first-call race doesn't trip the discovery_pending
    # guard.
    LOG.info("sleeping 2.0s to let SEDP endpoints propagate...")
    time.sleep(2.0)

    LOG.info("attempting RPC call svc=%s action=info", args.service)
    svc = hub.create_service(args.service)
    req = svc.create_requester(timeout_ms=args.rpc_timeout * 1000)
    rpc_start = time.time()
    try:
        result = req.call({"action": "info", "caller": socket.gethostname()})
    except Exception as e:  # noqa: BLE001
        LOG.error("RPC call raised: %s", e)
        result = {"ok": False, "error": str(e)}
    rpc_elapsed = time.time() - rpc_start

    LOG.info("RPC result (%.2fs): %s", rpc_elapsed,
             json.dumps(result, ensure_ascii=False))
    rpc_result = result
    rpc_done.set()

    try:
        hub.close()
    except Exception:
        pass

    if not result.get("ok", False):
        LOG.error("peer discovered but RPC FAILED")
        return 1
    LOG.info("DISCOVERY + RPC both OK (exit 0)")
    return 0


# --------------------------------------------------------------------------- #
# Mode: probe-only
# --------------------------------------------------------------------------- #
def run_probe(args: argparse.Namespace) -> int:
    """Bring up a Hub, log every peer event for --timeout seconds,
    report whether at least one peer was seen. No RPC, no service
    registration — pure discovery diagnostic."""
    from wdndds.dds._rpc import Hub

    events: list[tuple[float, str, str, bool]] = []
    event_lock = threading.Lock()

    def _on_peer_event(guid: str, name: str, ip: str, discovered: bool) -> None:
        ts = time.time()
        with event_lock:
            events.append((ts, guid, name, discovered))
        LOG.info("[+%5.1fs] %s guid=%s name=%s",
                 ts - (events[0][0] if events else ts),
                 "DISCOVERED" if discovered else "REMOVED",
                 guid[:24], name)

    LOG.info("probe mode: listening for peers on domain %d for %ds "
             "(SIMPLE discovery)", args.domain, args.timeout)
    hub = Hub(
        participant_name=args.name or f"probe-{socket.gethostname()}",
        domain_id=args.domain,
        discovery_servers=[],   # ← forces SIMPLE
    )
    try:
        hub.start()
    except Exception as e:  # noqa: BLE001
        LOG.error("hub.start() failed: %s", e)
        return 3

    LOG.info("hub started: guid=%s", hub.guid()[:24])
    hub.set_peer_listener(_on_peer_event)

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        time.sleep(0.5)

    with event_lock:
        discovered = [e for e in events if e[3]]
        removed = [e for e in events if not e[3]]

    LOG.info("--- summary ---")
    LOG.info("peers discovered: %d", len(discovered))
    LOG.info("peers removed:    %d", len(removed))
    LOG.info("total events:     %d", len(events))
    for ts, guid, name, disc in events:
        LOG.info("  +%5.1fs  %s  guid=%s  name=%s",
                 ts - events[0][0] if events else 0,
                 "DISCOVERED" if disc else "REMOVED",
                 guid[:24], name)

    try:
        hub.close()
    except Exception:
        pass
    return 0 if discovered else 2


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Verify FastDDS SIMPLE discovery works for wdnwebui. "
                    "Two processes: one server, one client. "
                    "Run on same host or different hosts on the same LAN.",
    )
    ap.add_argument("mode", choices=["server", "client", "probe-only"],
                    help="server: register a service, wait forever. "
                         "client: discover + RPC. probe-only: just listen.")
    ap.add_argument("--name", default=None,
                    help="participant name (server: required. "
                         "client: name to wait for. probe-only: optional).")
    ap.add_argument("--service", default="wdndds/rpc/verify-test",
                    help="service name to register / call (default: "
                         "%(default)s)")
    ap.add_argument("--domain", type=int, default=0,
                    help="DDS domain id (default 0)")
    ap.add_argument("--timeout", type=int, default=30,
                    help="seconds to wait for peer discovery "
                         "(client + probe-only)")
    ap.add_argument("--rpc-timeout", type=int, default=10,
                    help="RPC call timeout in seconds (client)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="DEBUG-level logging")
    args = ap.parse_args(argv)

    _setup_logging(args.verbose)

    if args.mode in ("server", "probe-only") and args.name is None:
        args.name = f"{args.mode}-{socket.gethostname()}-{os.getpid()}"

    LOG.info("============================================")
    LOG.info("verify_simple_discovery mode=%s name=%s service=%s",
             args.mode, args.name, args.service)
    LOG.info("WDNDDS_INITIAL_PEERS=%r",
             os.environ.get("WDNDDS_INITIAL_PEERS", "<unset>"))
    LOG.info("FASTRTPS_DEFAULT_PROFILES_FILE=%r",
             os.environ.get("FASTRTPS_DEFAULT_PROFILES_FILE", "<unset>"))
    LOG.info("============================================")

    if args.mode == "server":
        return run_server(args)
    elif args.mode == "client":
        return run_client(args)
    else:
        return run_probe(args)


if __name__ == "__main__":
    sys.exit(main())