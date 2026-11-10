"""System log endpoints backed by ``journalctl``.

The web UI never sees a shell — every call goes through the
``subprocess`` API with a fixed argv list, so unit / priority filters
can't break out into command injection. ``systemctl`` itself just
needs read access to the systemd journal, which we have on the dev
box (wdnwebui runs as root via ``sudo nohup``); on a locked-down
deployment we'd plug ``POLKITKIT``/an explicit `machinectl shell`
in here.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)


router = APIRouter(tags=["system-logs"])

JOURNALCTL = shutil.which("journalctl") or "/usr/bin/journalctl"

# Filters — validated strictly, never expanded into a shell.
PRIORITIES = {
    "emerg", "alert", "crit", "err", "warning",
    "notice", "info", "debug",
}
UNIT_RE = re.compile(r"^[A-Za-z0-9_.@:-]{1,128}$")
MAX_LINES = 2000
MAX_MSG_CHARS = 4 * 1024  # truncate huge entries so the WS/REST payload stays bounded
MAX_FOLLOW_SECONDS = 30 * 60  # 30 min cap per WS connection
# How often the tail pings the client. Short on purpose: when the server
# shuts down, uvicorn closes the transport *without* delivering a
# ``websocket.disconnect`` to the app, so a failing send is the only way
# the handler learns the socket is gone. See ``_ping`` below.
PING_INTERVAL_SEC = 3
# Upper bound for a single send to the client. See ``_ping`` below.
_WS_SEND_TIMEOUT = 5.0

# systemd-numeric → name. systemd priority 0=emerg ... 7=debug.
_PRIORITY_NAME = {
    0: "emerg", 1: "alert", 2: "crit", 3: "err",
    4: "warning", 5: "notice", 6: "info", 7: "debug",
}


# --------------------------------------------------------------------------- #
def _normalize(j: dict) -> dict:
    """Pick a flat subset of fields the UI actually uses."""
    ts_us = j.get("__REALTIME_TIMESTAMP") or j.get("_SOURCE_REALTIME_TIMESTAMP")
    try:
        ts_ms = int(ts_us) // 1000 if ts_us else 0
    except (TypeError, ValueError):
        ts_ms = 0
    try:
        prio = int(j.get("PRIORITY", 6))
    except (TypeError, ValueError):
        prio = 6
    msg = j.get("MESSAGE")
    if isinstance(msg, list):
        # Some services (rknn3 and others) emit MESSAGE as a list of
        # byte values when the original payload was non-UTF-8 binary.
        # Decode as latin-1 so we don't lose data; tag so the UI can
        # decide whether to render.
        try:
            msg = bytes(msg).decode("utf-8", errors="replace")
        except Exception:
            msg = repr(msg)[:400]
    elif isinstance(msg, bytes):
        msg = msg.decode("utf-8", errors="replace")
    elif not isinstance(msg, str):
        msg = str(msg)
    msg = msg or ""
    if len(msg) > MAX_MSG_CHARS:
        msg = msg[:MAX_MSG_CHARS] + f"… [+{len(msg) - MAX_MSG_CHARS} chars]"
    return {
        "cursor": j.get("__CURSOR"),
        "ts_ms": ts_ms,
        "ts_iso": (
            time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts_ms / 1000))
            + f".{(ts_ms % 1000):03d}Z"
            if ts_ms else ""
        ),
        "priority": str(prio),
        "priority_name": _PRIORITY_NAME.get(prio, "info"),
        "unit": (
            j.get("_SYSTEMD_UNIT")
            or j.get("SYSLOG_IDENTIFIER")
            or "?"
        ),
        "pid": j.get("_PID") or j.get("SYSLOG_PID"),
        "message": msg.rstrip(),
    }


def _run(cmd: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# --------------------------------------------------------------------------- #
@router.get("/system-logs/units")
async def list_units() -> dict:
    """Curated list of relevant systemd units + journalctl availability."""
    journalctl_ok = Path(JOURNALCTL).is_file()
    units: list[dict] = []
    if journalctl_ok:
        try:
            result = _run(
                [
                    "systemctl", "list-units", "--no-pager", "--no-legend",
                    "--type=service",
                    "--state=running,failed,exited,activating",
                ],
                timeout=8,
            )
        except subprocess.TimeoutExpired:
            result = None
        except Exception as e:
            result = None
        if result is not None and result.returncode == 0:
            for line in result.stdout.splitlines():
                parts = line.split(None, 4)
                if len(parts) < 5:
                    continue
                unit, loaded, active, sub, desc = parts
                if not unit.endswith(".service"):
                    continue
                units.append({
                    "unit": unit,
                    "state": f"{active}/{sub}",
                    "description": desc.strip(),
                })
    return {
        "journalctl": JOURNALCTL,
        "available": journalctl_ok,
        "priorities": sorted(PRIORITIES),
        "units": units,
    }


@router.get("/system-logs")
async def list_logs(
    unit: Optional[str] = Query(None, description="systemd unit, e.g. sshd.service"),
    priority: Optional[str] = Query(None, description="min priority (err/warning/...)"),
    lines: int = Query(200, ge=1, le=MAX_LINES),
) -> dict:
    if not Path(JOURNALCTL).is_file():
        raise HTTPException(status_code=503, detail=f"journalctl not found: {JOURNALCTL}")
    if unit is not None and not UNIT_RE.match(unit):
        raise HTTPException(status_code=400, detail="bad unit (allowed: [A-Za-z0-9_.@:-]{1,128})")
    if priority is not None and priority not in PRIORITIES:
        raise HTTPException(status_code=400, detail=f"priority must be one of {sorted(PRIORITIES)}")
    cmd = [
        # NOTE: don't pass ``--no-tail`` here. The flag is documented as
        # "show all lines, even in follow mode" but in practice it also
        # makes ``-n`` get ignored — we get the entire journal back
        # instead of the last N lines. Confirmed empirically on
        # systemd 252.
        JOURNALCTL, "--no-pager", "-n", str(lines),
        "--output=json",
    ]
    if unit:
        cmd += ["-u", unit]
    if priority:
        cmd += ["--priority", priority]
    try:
        result = _run(cmd, timeout=15)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="journalctl timeout")
    if result.returncode != 0 and not result.stdout:
        raise HTTPException(
            status_code=502,
            detail=(result.stderr or "journalctl failed").strip()[:400],
        )
    entries: list[dict] = []
    for line in result.stdout.splitlines():
        try:
            j = json.loads(line)
        except Exception:
            continue
        entries.append(_normalize(j))
    return {
        "entries": entries,
        "unit": unit,
        "priority": priority,
        "lines": lines,
        "truncated": len(entries) >= lines,
    }


# --------------------------------------------------------------------------- #
@router.websocket("/system-logs/ws")
async def ws_logs(
    ws: WebSocket,
    unit: str = "",
    priority: str = "",
) -> None:
    """Tail mode: dump last 100 lines then ``-f`` follow.

    Streams ``{"type":"line", "entry": {...}}`` JSON frames for every
    new line. To stop cleanly, the client closes the connection; the
    subprocess is terminated and awaited in the ``finally`` block.
    """
    await ws.accept()
    if unit and not UNIT_RE.match(unit):
        await ws.send_json({"type": "error", "detail": "bad unit"})
        await ws.close()
        return
    if priority and priority not in PRIORITIES:
        await ws.send_json({"type": "error", "detail": "bad priority"})
        await ws.close()
        return

    if not Path(JOURNALCTL).is_file():
        await ws.send_json({"type": "error", "detail": "journalctl not found"})
        await ws.close()
        return

    # -n 100 dumps the most recent 100 lines, then `-f` blocks waiting
    # for new entries. Both come on stdout interleaved.
    cmd = [
        JOURNALCTL, "--no-pager",
        "-n", "100", "-f",
        "--output=json",
    ]
    if unit:
        cmd += ["-u", unit]
    if priority:
        cmd += ["--priority", priority]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        await ws.send_json({"type": "error", "detail": str(e)})
        await ws.close()
        return
    except Exception as e:
        await ws.send_json({"type": "error", "detail": f"spawn failed: {e}"})
        await ws.close()
        return

    await ws.send_json({
        "type": "ready",
        "unit": unit or None,
        "priority": priority or None,
        "pid": proc.pid,
    })

    # A dedicated watcher for the client going away. The drain loops below
    # block on ``stream.readline()`` — waiting for ``journalctl`` to say
    # something — so they never call ``receive()`` and never notice that the
    # socket is gone. On a server shutdown uvicorn closes the transport and
    # then waits for this handler's task to finish; without this watcher the
    # handler waits forever, the task never leaves ``server_state.tasks``,
    # and the unit has to be SIGKILLed once systemd's stop timeout expires
    # (which takes the compute workers down with it).
    async def _watch_disconnect() -> None:
        # Block until the client goes away. Messages sent by the client are
        # ignored on purpose — this endpoint is a one-way tail.
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                return

    disconnect = asyncio.create_task(_watch_disconnect(), name="logs-disconnect")

    async def _drain(stream, label):
        sent_error = False
        while True:
            read = asyncio.create_task(stream.readline())
            done, _pending = await asyncio.wait(
                {read, disconnect}, return_when=asyncio.FIRST_COMPLETED
            )
            if disconnect in done:
                # Client gone (or the server is shutting the socket down).
                read.cancel()
                try:
                    proc.terminate()
                except Exception:
                    pass
                return
            line = read.result()
            if not line:
                # EOF — journalctl exited
                return
            try:
                j = json.loads(line.decode("utf-8", errors="replace"))
            except Exception:
                if label == "stderr" and not sent_error:
                    try:
                        await ws.send_json({"type": "info", "detail": line.decode("utf-8", "replace")[:200]})
                        sent_error = True
                    except Exception:
                        return
                continue
            entry = _normalize(j)
            entry["stream"] = label
            try:
                await ws.send_json({"type": "line", "entry": entry})
            except (WebSocketDisconnect, RuntimeError):
                try:
                    proc.terminate()
                except Exception:
                    pass
                return

    async def _ping():
        # Periodic liveness ping — keeps proxies from reaping an idle WS
        # during quiet periods, and lets us notice a hang early.
        #
        # The interval is deliberately short: when the *server* shuts down,
        # uvicorn closes the transport without delivering a
        # ``websocket.disconnect`` to the app, so the only way this handler
        # learns the socket is gone is by **failing to send**. A ping every
        # few seconds turns "wait for the graceful-shutdown timeout" into
        # "notice it right away" — the difference between a service that
        # stops in a second and one that has to be SIGKILLed.
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL_SEC)
                if proc.returncode is not None:
                    return
                try:
                    # ⚠️ **send 必须加超时**: a peer that vanished without
                    # closing cleanly leaves the socket in CLOSE-WAIT with
                    # unflushed data — ``send`` then waits forever for the
                    # write buffer to drain and never returns, so this
                    # coroutine never ends and the unit's stop times out.
                    await asyncio.wait_for(
                        ws.send_json({"type": "ping"}),
                        timeout=_WS_SEND_TIMEOUT,
                    )
                except Exception:
                    # ClientDisconnected / WebSocketDisconnect / RuntimeError
                    # — the socket is gone, stop the follower.
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    return
        except asyncio.CancelledError:
            return

    try:
        done, pending = await asyncio.wait(
            [
                asyncio.create_task(_drain(proc.stdout, "line"), name="logs-stdout"),
                asyncio.create_task(_drain(proc.stderr, "stderr"), name="logs-stderr"),
                asyncio.create_task(_ping(), name="logs-ping"),
            ],
            return_when=asyncio.FIRST_COMPLETED,
            timeout=MAX_FOLLOW_SECONDS,
        )
        for t in pending:
            t.cancel()
        for t in done:
            exc = t.exception()
            if exc and not isinstance(exc, (WebSocketDisconnect, RuntimeError)):
                # Surface the error but don't crash the WS handler.
                try:
                    await ws.send_json({"type": "error", "detail": str(exc)})
                except Exception:
                    pass
        if not done:
            try:
                await ws.send_json({"type": "info", "detail": "follow timeout"})
            except Exception:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        # The disconnect watcher lives outside the ``asyncio.wait`` set, so
        # it has to be stopped explicitly — otherwise it lingers as a
        # pending task after the tail ends.
        if not disconnect.done():
            disconnect.cancel()
        else:
            # Consume a possible exception (e.g. WebSocketDisconnect) so
            # asyncio doesn't log "Task exception was never retrieved".
            try:
                disconnect.exception()
            except BaseException:
                pass
        if proc.returncode is None:
            # Kill the follower **even when this coroutine is cancelled**.
            # ``asyncio.CancelledError`` derives from ``BaseException``, so a
            # plain ``except Exception`` never sees it — and cancellation is
            # exactly what happens during a uvicorn graceful shutdown.
            # Skipping the kill there left an orphaned ``journalctl -f``
            # behind, which then kept the systemd cgroup busy and made the
            # unit stop time out.
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.CancelledError:
                # Shutting down — kill immediately, don't await.
                try:
                    proc.kill()
                except Exception:
                    pass
                raise
            except Exception:
                # TimeoutError (and anything else) — escalate to SIGKILL.
                try:
                    proc.kill()
                except Exception:
                    pass
