"""WeChat bot service for wdnwebui.

Thin singleton wrapper around :class:`wdndds.Wechat` that adds:

* a WebSocket broadcaster so the im-bridge page can watch the login state
  machine and stream new incoming messages in real time;
* a polling thread that detects login-state changes and pushes them to
  subscribers (the SDK only fires callbacks on its own private asyncio
  loop, so the FastAPI event loop needs an explicit bridge).

The :class:`wdndds.Wechat` instance is constructed lazily on first access
(``get_wechat()``); it never auto-starts the long-poll, so the user must
explicitly POST ``/api/wechat/login`` from the UI to begin. This matches
the "always manual login" decision in the design doc.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from typing import Any, Deque, Optional

import wdndds
from wdndds import Wechat


_LOG = logging.getLogger("wechat")


# Max buffered frames per WS subscriber before the oldest is dropped.
_SUB_QUEUE_MAX = 200


def _wechat_log(msg: str) -> None:
    print(f"[wechat] {msg}", flush=True)


class _WechatManager:
    """Owns the process-wide :class:`wdndds.Wechat` instance + WS fan-out."""

    POLL_INTERVAL = 1.0  # login-state poll cadence

    def __init__(self) -> None:
        self._wechat: Wechat = wdndds.Wechat()
        # Subscribe to inbound messages so we can push them to WS clients.
        self._wechat.register_message_handler(self._handle_incoming)

        self._subs_lock = threading.Lock()
        self._subs: list[asyncio.Queue] = []

        # State-diff cache: what we last pushed to subscribers. Used to
        # avoid emitting the same snapshot twice.
        self._last_login_state: Optional[str] = None
        self._last_qr_url: Optional[str] = None
        self._last_started: Optional[bool] = None
        self._last_user_id: Optional[str] = None
        self._last_error: Optional[str] = None
        self._last_last_event: Optional[str] = None
        self._last_last_event_ts: float = 0.0
        self._last_recent_len: int = 0
        self._last_recent_tail: Optional[dict] = None

        self._poller_stop = threading.Event()
        self._poller_thread = threading.Thread(
            target=self._poll_loop, name="wechat-poller", daemon=True,
        )
        self._poller_thread.start()

    # ------------------------------------------------------------- WS
    def register_subscriber(self, queue: asyncio.Queue) -> None:
        with self._subs_lock:
            self._subs.append(queue)

    def unregister_subscriber(self, queue: asyncio.Queue) -> None:
        with self._subs_lock:
            try:
                self._subs.remove(queue)
            except ValueError:
                pass

    def _broadcast(self, payload: dict) -> None:
        """Push ``payload`` to all subscribers (thread-safe).

        Runs on whichever thread the caller is on — usually the wdndds
        loop thread (callbacks) or the poller thread. Each subscriber
        queue is an :class:`asyncio.Queue` owned by the FastAPI event
        loop; we use ``put_nowait`` and drop the oldest item on overflow
        to keep the publisher non-blocking.
        """
        with self._subs_lock:
            queues = list(self._subs)
        if not queues:
            return
        for q in queues:
            try:
                if q.full():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                q.put_nowait(payload)
            except Exception as exc:  # noqa: BLE001
                _wechat_log(f"broadcast failed: {exc!r}")

    # ------------------------------------------------------------- snapshot
    def snapshot(self) -> dict:
        ls = self._wechat.login_status()
        return {
            "started": self._wechat.started,
            "user_id": self._wechat.user_id,
            "cred_path": self._wechat.cred_path,
            "login_state": ls["state"],
            "qr_url": ls["qr_url"],
            "last_event": ls["last_event"],
            "last_event_ts": ls["last_event_ts"],
            "error": ls["error"],
            "known_users": self._wechat.known_users(),
            "recent_messages": self._wechat.recent_messages(limit=50),
        }

    def known_users(self) -> list[str]:
        return self._wechat.known_users()

    def recent_messages(self, limit: int = 50) -> list[dict]:
        return self._wechat.recent_messages(limit=limit)

    # ------------------------------------------------------------- actions
    def start_login(self, *, force: bool = False) -> dict:
        snap = self._wechat.start_login(force=force)
        # Push a state diff immediately so the UI flips to "awaiting_scan"
        # without waiting for the next poll tick.
        self._push_state_diff()
        return snap

    def cancel_login(self) -> dict:
        snap = self._wechat.cancel_login()
        self._push_state_diff()
        return snap

    def logout(self) -> dict:
        self._wechat.logout()
        self._push_state_diff()
        return {"ok": True}

    def send_text(self, user_id: str, text: str) -> None:
        # ``send_text`` records the outbound in the wdndds cache; pushing
        # the diff afterwards surfaces it to the UI.
        self._wechat.send_text(user_id, text)
        self._push_state_diff()

    # ------------------------------------------------------------- events
    def _handle_incoming(self, msg: Any) -> None:
        """Called from the wdndds loop thread for each user message."""
        # ``recent_messages`` is now one item longer; the diff logic in
        # ``_push_state_diff`` will detect that and forward the tail to
        # subscribers. We still call it directly so the user sees the
        # message without waiting up to ``POLL_INTERVAL`` seconds.
        self._push_state_diff()

    def _push_state_diff(self) -> None:
        ls = self._wechat.login_status()
        started = self._wechat.started
        user_id = self._wechat.user_id
        recent = self._wechat.recent_messages(limit=50)
        recent_len = len(recent)
        recent_tail = recent[0] if recent else None

        diffs: dict[str, Any] = {}
        if ls["state"] != self._last_login_state:
            diffs["login_state"] = ls["state"]
            self._last_login_state = ls["state"]
        if ls["qr_url"] != self._last_qr_url:
            diffs["qr_url"] = ls["qr_url"]
            self._last_qr_url = ls["qr_url"]
        if started != self._last_started:
            diffs["started"] = started
            self._last_started = started
        if user_id != self._last_user_id:
            diffs["user_id"] = user_id
            self._last_user_id = user_id
        if ls["error"] != self._last_error:
            diffs["error"] = ls["error"]
            self._last_error = ls["error"]
        # ``last_event`` is metadata-only ("qr" / "scanned" / "starting" /
        # "cancelled" / "logged_out" …) but the UI uses it to flip the
        # status hint while ``login_state`` itself hasn't moved (e.g.
        # user just clicked 登录 and the SDK hasn't fetched a QR yet —
        # ``login_state`` stays ``idle`` for a moment). Push it through so
        # the UI can show "正在发起登录…".
        if ls["last_event"] != self._last_last_event:
            diffs["last_event"] = ls["last_event"]
            diffs["last_event_ts"] = ls["last_event_ts"]
            self._last_last_event = ls["last_event"]
            self._last_last_event_ts = ls["last_event_ts"]

        if diffs:
            self._broadcast({"type": "state", "state": diffs})

        if recent_tail and (
            recent_len != self._last_recent_len
            or recent_tail != self._last_recent_tail
        ):
            self._broadcast({"type": "message", "message": recent_tail})
            self._last_recent_len = recent_len
            self._last_recent_tail = recent_tail

    # ------------------------------------------------------------- poller
    def _poll_loop(self) -> None:
        """Periodic state-diff push (catches transitions the SDK doesn't
        fire a callback for, e.g. ``started`` flipping via session-expiry
        re-login)."""
        while not self._poller_stop.is_set():
            try:
                self._push_state_diff()
            except Exception as exc:  # noqa: BLE001
                _wechat_log(f"poll loop error: {exc!r}")
            self._poller_stop.wait(self.POLL_INTERVAL)

    def close(self) -> None:
        self._poller_stop.set()
        if self._poller_thread.is_alive():
            self._poller_thread.join(timeout=2.0)
        with self._subs_lock:
            self._subs.clear()
        try:
            self._wechat.close()
        except Exception as exc:  # noqa: BLE001
            _wechat_log(f"close error: {exc!r}")


_manager: Optional[_WechatManager] = None
_manager_lock = threading.Lock()


def get_wechat() -> _WechatManager:
    """Lazy singleton accessor — never auto-starts the bot."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = _WechatManager()
    return _manager


def close_wechat() -> None:
    """Shutdown hook — no-op if the manager was never created."""
    global _manager
    with _manager_lock:
        if _manager is None:
            return
        _manager.close()
        _manager = None


def reset_for_tests() -> None:
    """Drop the singleton (test-only)."""
    global _manager
    with _manager_lock:
        if _manager is not None:
            _manager.close()
        _manager = None