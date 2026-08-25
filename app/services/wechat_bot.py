"""Synchronous facade around the ``wechatbot-sdk`` async client.

Inlined from ``wdndds.wechat`` into wdnwebui so the im-bridge feature
doesn't pull in the full ``wdndds`` package at the Python-import level
(``wdndds`` is still needed for DDS/RPC in compute_registry).

The upstream SDK (``wechatbot.WeChatBot``) is async-first and assumes the
caller is running an asyncio event loop. Service processes (FastAPI handlers,
cron scripts, debugging CLIs) are usually synchronous entry points, so this
module wraps the SDK behind a small, thread-safe, **synchronous** API:

* a dedicated daemon thread runs one private ``asyncio`` event loop forever;
* every public method schedules the corresponding async coroutine on that
  loop via :func:`asyncio.run_coroutine_threadsafe` and blocks until it
  completes (or times out);
* SDK callbacks (``on_qr_url`` / ``on_scanned`` / ``on_expired`` / ``on_error``)
  are invoked from inside the loop's thread, so they can mutate the
  :class:`Wechat` state directly without locking.

Login flow
----------
:meth:`Wechat.start_login` schedules ``bot.login()`` as a background task on
the loop and returns immediately — the QR URL is delivered through the
``on_qr_url`` callback and exposed via :meth:`Wechat.login_status`. When the
user confirms the scan on the phone, the login task transitions the bot to
the long-poll state automatically (:meth:`Wechat.start` is launched as a
sibling task) so the caller can begin sending replies as soon as it sees
``state == "confirmed"``.

Credentials
-----------
Stored on disk under :data:`DEFAULT_CRED_PATH` (``~/.wechatbot/credentials.json``)
by default; override per-instance with ``cred_path=`` or globally with the
``WDNDDS_WECHAT_CRED_PATH`` environment variable. Pass ``force=True`` to
``start_login()`` to discard the stored credentials and re-issue a QR.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Deque, Optional

import wechatbot
from wechatbot import (
    Credentials,
    IncomingMessage,
    WeChatBot,
)
from wechatbot.errors import (
    AuthError,
    MediaError,
    NoContextError,
    WeChatBotError,
)

__all__ = [
    "DEFAULT_CRED_PATH",
    "WECHAT_CRED_PATH_ENV",
    "Wechat",
    "WechatError",
]

# Re-export SDK error classes so consumers can
# ``except Wechat.NoContextError`` without taking a hard dependency on
# ``wechatbot``. We subclass so ``isinstance`` checks still match.
WechatError = WeChatBotError

DEFAULT_CRED_PATH = Path.home() / ".wechatbot" / "credentials.json"
WECHAT_CRED_PATH_ENV = "WDNDDS_WECHAT_CRED_PATH"


_LOG = logging.getLogger("wechat_bot")


async def _drain_loop() -> None:
    """Yield once on the loop so cancelled tasks can finish unwinding."""
    import asyncio as _asyncio

    await _asyncio.sleep(0)


async def _cancel_all_tasks() -> None:
    """Cancel every pending Task on the running loop.

    Used during shutdown so SDK-internal ``aiohttp`` tasks don't keep the
    loop alive past our ``loop.stop()`` call. Idempotent — already-done
    tasks are skipped.
    """
    import asyncio as _asyncio

    current = _asyncio.current_task()
    for task in _asyncio.all_tasks():
        if task is current or task.done():
            continue
        try:
            task.cancel()
        except Exception:
            pass


def _resolve_cred_path(explicit: Optional[str]) -> str:
    """Pick the credentials file path: explicit > env > SDK default."""
    if explicit:
        return str(Path(explicit).expanduser())
    env = os.environ.get(WECHAT_CRED_PATH_ENV)
    if env:
        return str(Path(env).expanduser())
    return str(DEFAULT_CRED_PATH)


class Wechat:
    """Synchronous wrapper around :class:`wechatbot.WeChatBot`.

    A single instance owns one private asyncio loop running on a daemon
    thread. Constructing the instance never blocks on network I/O — it only
    spins up the loop and constructs the SDK client.

    Thread safety
    -------------
    All public methods are safe to call from multiple threads; the SDK
    coroutines are serialised through ``run_coroutine_threadsafe``.
    """

    # Login state machine. ``idle`` means "no login in progress or completed";
    # ``awaiting_scan`` means a QR has been delivered and we're waiting for
    # the user to scan; ``scanned`` means the phone confirmed but the user
    # still needs to tap "登录"; ``confirmed`` means credentials issued;
    # ``expired`` / ``error`` are terminal for the current QR.
    STATE_IDLE = "idle"
    STATE_AWAITING_SCAN = "awaiting_scan"
    STATE_SCANNED = "scanned"
    STATE_CONFIRMED = "confirmed"
    STATE_EXPIRED = "expired"
    STATE_ERROR = "error"
    _TERMINAL_STATES = frozenset({STATE_CONFIRMED, STATE_EXPIRED, STATE_ERROR})
    _ACTIVE_STATES = frozenset({STATE_AWAITING_SCAN, STATE_SCANNED})

    # Long-poll lifecycle.
    STATE_RUNNING = "running"  # alias of "confirmed" + long-poll alive

    # Default history size for ``recent_messages()``.
    DEFAULT_HISTORY_LIMIT = 50

    # Hard cap on the in-memory history (also enforced by ``deque(maxlen=...)``).
    HISTORY_MAX = 200

    def __init__(
        self,
        *,
        cred_path: Optional[str] = None,
        bot_agent: Optional[str] = None,
    ) -> None:
        self._cred_path = _resolve_cred_path(cred_path)
        self._bot_agent = bot_agent

        self._bot: Optional[WeChatBot] = None
        self._creds: Optional[Credentials] = None

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._loop_ready = threading.Event()
        self._closing = False
        # Guards construction / teardown; per-method state mutations
        # happen on the loop thread (callbacks) or under explicit ``with``
        # blocks (snapshot access), so they don't need this lock.
        self._init_lock = threading.Lock()

        # Login state.
        self._login_state = self.STATE_IDLE
        self._qr_url: Optional[str] = None
        self._last_event: str = ""
        self._last_event_ts: float = 0.0
        self._error: Optional[str] = None
        self._cancelled = False

        # Long-poll state.
        self._started = False
        self._start_task: Optional[asyncio.Task] = None
        self._login_task: Optional[asyncio.Task] = None

        # User-registered message handlers + in-memory ring buffer.
        self._handlers: list[Callable[[IncomingMessage], Any]] = []
        self._recent: Deque[dict] = collections.deque(maxlen=self.HISTORY_MAX)

        # Per-user known set, populated by inbound + outbound traffic.
        self._known_users: set[str] = set()
        self._known_users_lock = threading.Lock()

        self._start_loop()

    # ------------------------------------------------------------------ loop
    def _start_loop(self) -> None:
        """Spin up the daemon thread + asyncio loop and build the bot."""
        with self._init_lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._run_loop, name="wechat-bot-loop", daemon=True,
            )
            self._thread.start()
        if not self._loop_ready.wait(timeout=10.0):
            raise RuntimeError("wechat loop did not start within 10s")
        # Build the WeChatBot on the loop so its aiohttp machinery
        # binds to the right loop.
        fut = asyncio.run_coroutine_threadsafe(
            self._build_bot(), self._loop,
        )
        self._bot = fut.result(timeout=10.0)

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop_ready.set()
            self._loop.run_forever()
        finally:
            try:
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            except Exception:
                pass
            self._loop.close()

    async def _build_bot(self) -> WeChatBot:
        bot = WeChatBot(
            cred_path=self._cred_path,
            bot_agent=self._bot_agent,
            on_qr_url=self._on_qr_url,
            on_scanned=self._on_scanned,
            on_expired=self._on_expired,
            on_error=self._on_error,
        )
        # Inbound messages fan out to ``_on_incoming``, which records to
        # the ring buffer and dispatches to user-registered handlers.
        bot.on_message(self._on_incoming)
        return bot

    # ----------------------------------------------------------- callbacks
    # All four run on the asyncio loop thread (the SDK calls them sync from
    # inside its coroutines), so no locking needed for the booleans.

    def _on_qr_url(self, url: str) -> None:
        if self._cancelled:
            return
        self._qr_url = url
        self._login_state = self.STATE_AWAITING_SCAN
        self._last_event = "qr"
        self._last_event_ts = time.time()
        self._error = None

    def _on_scanned(self) -> None:
        if self._cancelled:
            return
        self._login_state = self.STATE_SCANNED
        self._last_event = "scanned"
        self._last_event_ts = time.time()

    def _on_expired(self) -> None:
        if self._cancelled:
            return
        self._login_state = self.STATE_EXPIRED
        self._last_event = "expired"
        self._last_event_ts = time.time()
        self._qr_url = None

    def _on_error(self, err: Exception) -> None:
        if self._cancelled:
            return
        self._error = f"{type(err).__name__}: {err}"
        self._login_state = self.STATE_ERROR
        self._last_event = "error"
        self._last_event_ts = time.time()

    # ------------------------------------------------------------------ auth
    @property
    def credentials(self) -> Optional[Credentials]:
        return self._creds

    @property
    def user_id(self) -> Optional[str]:
        return self._creds.user_id if self._creds else None

    @property
    def cred_path(self) -> str:
        return self._cred_path

    @property
    def started(self) -> bool:
        """True when the long-poll loop is running."""
        return self._started

    def start_login(self, *, force: bool = False) -> dict:
        """Kick off QR-code login. Returns the current login snapshot.

        Idempotent: if a login is already in progress or the bot is already
        running, returns the existing snapshot without restarting anything.

        ``force=True`` discards any stored credentials under ``cred_path``
        and issues a fresh QR. The default ``False`` reuses the existing
        credentials (if any) and short-circuits straight to ``confirmed``.
        """
        self._cancelled = False
        if self._bot is None:
            raise RuntimeError("wechat bot not initialised")

        if self._login_state in self._ACTIVE_STATES:
            return self.login_status()
        if self._started or self._login_state == self.STATE_CONFIRMED:
            return self.login_status()

        # Reset transient state for a new attempt.
        self._qr_url = None
        self._error = None
        self._last_event = "starting"
        self._last_event_ts = time.time()

        fut = asyncio.run_coroutine_threadsafe(
            self._login_async(force=force), self._loop,
        )
        # ``run_coroutine_threadsafe`` returns a concurrent.futures.Future
        # wrapping a Task; the underlying asyncio Task is captured by
        # ``_login_async`` itself once it starts running (see below), so
        # ``close()`` can cancel it cleanly.
        fut.add_done_callback(self._on_login_done)
        return self.login_status()

    async def _login_async(self, *, force: bool) -> None:
        """Run ``bot.login`` on the loop; on success, kick off ``bot.start``.

        Cancellation
        ------------
        WeChatBot exposes no public ``cancel()`` for the login flow — its
        ``auth.login`` polls ``poll_qr_status`` until confirmed/expired, and
        the QR expires after ``MAX_QR_REFRESH_COUNT`` refreshes anyway. We
        just honour the cancel flag in the callbacks; the task itself runs
        to natural completion so the SDK's aiohttp sessions unwind cleanly.
        """
        # Capture the running Task so ``close()`` can cancel it.
        self._login_task = asyncio.current_task()
        try:
            creds = await self._bot.login(force=force)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 — surfaced via login_status()
            if self._cancelled:
                self._login_state = self.STATE_IDLE
                self._last_event = "cancelled"
                self._last_event_ts = time.time()
                return
            self._error = f"{type(exc).__name__}: {exc}"
            self._login_state = self.STATE_ERROR
            self._last_event = "login_failed"
            self._last_event_ts = time.time()
            return

        self._creds = creds
        self._login_state = self.STATE_CONFIRMED
        self._last_event = "confirmed"
        self._last_event_ts = time.time()
        self._qr_url = None

        # Auto-launch the long-poll on the same loop.
        if not self._started and not self._cancelled:
            self._start_task = asyncio.create_task(
                self._start_async(), name="wechat-bot-poll",
            )

    def _on_login_done(self, fut: "asyncio.Future[None]") -> None:
        # ``_login_async`` already records errors on the state machine; this
        # hook is just a safety net to log unexpected crashes.
        try:
            fut.result()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("wechat login task crashed: %r", exc)

    def cancel_login(self) -> dict:
        """Mark a pending login as cancelled (callbacks will be ignored).

        The underlying ``bot.login()`` coroutine keeps running until the QR
        expires or the user scans — there's no public cancellation hook in
        the SDK. This is sufficient for "hide the QR, the user changed
        their mind" UX; the next :meth:`start_login` resets the cancel flag.
        """
        self._cancelled = True
        self._login_state = self.STATE_IDLE
        self._qr_url = None
        self._last_event = "cancelled"
        self._last_event_ts = time.time()
        self._error = None
        return self.login_status()

    def login_status(self) -> dict:
        """Snapshot of the login state machine for the UI."""
        return {
            "state": self._login_state,
            "qr_url": self._qr_url,
            "last_event": self._last_event,
            "last_event_ts": self._last_event_ts,
            "error": self._error,
        }

    # ------------------------------------------------------------------ poll
    async def _start_async(self) -> None:
        """Long-poll loop. Runs until ``bot.stop()`` is called or it errors."""
        if self._bot is None:
            return
        self._started = True
        try:
            await self._bot.start()
        except Exception as exc:  # noqa: BLE001
            self._error = f"{type(exc).__name__}: {exc}"
            self._login_state = self.STATE_ERROR
            self._last_event = "poll_error"
            self._last_event_ts = time.time()
            _LOG.warning("wechat long-poll crashed: %r", exc)
        finally:
            self._started = False

    def stop(self) -> None:
        """Stop the long-poll loop (login state survives).

        ``WeChatBot.stop()`` is a **synchronous** method that just sets
        ``self._stopped = True`` on the SDK client; the long-poll
        coroutine (``bot.start()``) sees the flag on its next iteration
        and exits, at which point :meth:`_start_async`'s ``finally`` clause
        flips ``self._started`` back to ``False``.

        Earlier revisions wrapped this in ``asyncio.run_coroutine_threadsafe``
        which raised ``TypeError("A coroutine object is required")`` and
        silently left the bot running — making subsequent ``start_login()``
        calls short-circuit on the ``self._started`` guard.

        Beyond the sync flag-flip we also force-cancel the wrapping
        asyncio task: ``bot.start()`` may be blocked on a 45s aiohttp
        ``get_updates`` call, and waiting that long to see ``_started``
        flip back to ``False`` is unacceptable for a UI-facing ``stop()``.
        """
        if self._bot is None:
            return
        try:
            self._bot.stop()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("wechat stop failed: %r", exc)
        # Force-cancel the long-poll task if it's stuck on aiohttp.
        if self._loop is not None and not self._loop.is_closed():
            task = self._start_task
            if task is not None and not task.done():
                try:
                    self._loop.call_soon_threadsafe(task.cancel)
                except Exception:
                    pass
                # Give the cancellation a moment to propagate so
                # ``_start_async``'s ``finally`` flips ``_started`` back
                # to ``False`` before the caller reads it.
                try:
                    asyncio.run_coroutine_threadsafe(
                        _drain_loop(), self._loop,
                    ).result(timeout=1.0)
                except Exception:
                    pass
        # Defensive: even if the task is stuck (aiohttp cancellation
        # took too long), tell the public API the bot is no longer
        # running. ``start_login`` checks this flag and would otherwise
        # refuse to issue a fresh QR.
        self._started = False

    def logout(self) -> None:
        """Clear stored credentials and stop the bot.

        Removes the on-disk credentials file (so the next ``start_login()``
        without ``force=True`` will still see no creds and trigger a fresh
        QR) and tears down the long-poll loop.
        """
        self.stop()
        cred_path = Path(self._cred_path)
        try:
            if cred_path.exists():
                cred_path.unlink()
        except OSError as exc:
            _LOG.warning("could not delete %s: %r", cred_path, exc)
        self._creds = None
        self._login_state = self.STATE_IDLE
        self._last_event = "logged_out"
        self._last_event_ts = time.time()
        self._error = None
        self._qr_url = None

    # ------------------------------------------------------------------ send
    def send_text(self, user_id: str, text: str) -> None:
        """Send a text message to ``user_id`` (must have prior context)."""
        self._require_started()
        coro = self._bot.send(user_id, text)  # type: ignore[union-attr]
        try:
            asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=15.0)
        except NoContextError:
            self._record_outbound(user_id, "text", text, error="no_context")
            raise
        except WeChatBotError:
            raise
        self._record_outbound(user_id, "text", text)

    def send_image(
        self, user_id: str, data: bytes, caption: Optional[str] = None,
    ) -> None:
        self._require_started()
        content: dict = {"image": data}
        if caption:
            content["caption"] = caption
        coro = self._bot.send_media(user_id, content)  # type: ignore[union-attr]
        try:
            asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=30.0)
        except NoContextError:
            self._record_outbound(user_id, "image", caption or "", error="no_context")
            raise
        self._record_outbound(user_id, "image", caption or "")

    def send_file(
        self, user_id: str, data: bytes, file_name: str,
    ) -> None:
        self._require_started()
        coro = self._bot.send_media(  # type: ignore[union-attr]
            user_id, {"file": data, "file_name": file_name},
        )
        try:
            asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=60.0)
        except NoContextError:
            self._record_outbound(
                user_id, "file", file_name, error="no_context",
            )
            raise
        self._record_outbound(user_id, "file", file_name)

    def send_video(
        self, user_id: str, data: bytes, caption: Optional[str] = None,
    ) -> None:
        self._require_started()
        content: dict = {"video": data}
        if caption:
            content["caption"] = caption
        coro = self._bot.send_media(user_id, content)  # type: ignore[union-attr]
        try:
            asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=60.0)
        except NoContextError:
            self._record_outbound(user_id, "video", caption or "", error="no_context")
            raise
        self._record_outbound(user_id, "video", caption or "")

    def download_message(self, msg: IncomingMessage) -> Optional[Any]:
        """Download media (image / file / video / voice) from ``msg``.

        同步透传 SDK 的 ``client.download()``（私有 asyncio loop 调度）。
        CDN 大文件下载可能耗时数十秒，调用方应放在后台线程执行；
        timeout 120s 为上限兜底。返回 ``None`` 表示该消息无可下载媒体
        （SDK 语义），成功时为含 ``data`` bytes / ``file_name`` /
        ``format`` 的对象。
        """
        self._require_started()
        coro = self._bot.download(msg)  # type: ignore[union-attr]
        return asyncio.run_coroutine_threadsafe(
            coro, self._loop,
        ).result(timeout=120.0)

    def _require_started(self) -> None:
        if self._bot is None or self._loop is None:
            raise RuntimeError("wechat bot not initialised")
        if not self._started:
            raise RuntimeError(
                "wechat bot is not running. Call start_login() first."
            )

    # ------------------------------------------------------------------ msgs
    def register_message_handler(
        self, handler: Callable[[IncomingMessage], Any],
    ) -> Callable[[IncomingMessage], Any]:
        """Register a callback fired for every incoming user message.

        Runs on the asyncio loop thread; handlers should be quick and
        non-blocking. Returns ``handler`` so it can be used as a decorator.
        """
        self._handlers.append(handler)
        return handler

    def recent_messages(self, limit: int = DEFAULT_HISTORY_LIMIT) -> list[dict]:
        """Most recent N messages (both inbound and outbound), newest first."""
        limit = max(1, min(int(limit), self.HISTORY_MAX))
        # ``deque`` is ordered oldest→newest; reverse for newest-first.
        items = list(self._recent)[-limit:][::-1]
        return items

    def known_users(self) -> list[str]:
        with self._known_users_lock:
            return sorted(self._known_users)

    # Called from the SDK's long-poll thread (== loop thread).
    def _on_incoming(self, msg: IncomingMessage) -> None:
        self._record_inbound(msg)
        for handler in self._handlers:
            try:
                result = handler(msg)
                if asyncio.iscoroutine(result):
                    # Schedule so the handler can await without blocking
                    # the dispatch loop. Errors are reported via on_error.
                    asyncio.ensure_future(result)
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("wechat handler raised: %r", exc)

    def _record_inbound(self, msg: IncomingMessage) -> None:
        with self._known_users_lock:
            self._known_users.add(msg.user_id)
        kind = msg.type
        if kind == "image" and msg.images:
            text = msg.images[0].url or "[image]"
        elif kind == "voice" and msg.voices:
            text = msg.voices[0].text or "[voice]"
        elif kind == "file" and msg.files:
            text = msg.files[0].file_name or "[file]"
        elif kind == "video":
            text = "[video]"
        else:
            text = msg.text or ""
        self._recent.append({
            "direction": "in",
            "user_id": msg.user_id,
            "type": kind,
            "text": text,
            "ts": msg.timestamp.isoformat() if msg.timestamp else "",
            "ts_ms": int(msg.timestamp.timestamp() * 1000) if msg.timestamp else 0,
            "has_media": bool(msg.images or msg.voices or msg.files or msg.videos),
        })

    def _record_outbound(
        self,
        user_id: str,
        kind: str,
        text: str,
        *,
        error: Optional[str] = None,
    ) -> None:
        with self._known_users_lock:
            self._known_users.add(user_id)
        now = time.time()
        self._recent.append({
            "direction": "out",
            "user_id": user_id,
            "type": kind,
            "text": text,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "ts_ms": int(now * 1000),
            "has_media": kind in ("image", "file", "video"),
            "error": error,
        })

    # ------------------------------------------------------------------ close
    def close(self) -> None:
        """Stop the long-poll, cancel any pending login, tear down the loop.

        Shutdown is intentionally aggressive: we cancel *every* task on
        the private loop (not just the ones we tracked), tell the loop to
        stop, then wait briefly for the daemon thread to drain. If aiohttp
        is mid-request, the cancellation may not propagate instantly —
        we cap the wait at 2s and rely on ``daemon=True`` so the thread
        doesn't block interpreter exit.
        """
        with self._init_lock:
            if self._closing:
                return
            self._closing = True
        self._cancelled = True
        if self._bot is not None and self._loop is not None and not self._loop.is_closed():
            try:
                # Sync — just sets the long-poll flag.
                self._bot.stop()
            except Exception:
                pass
            # Cancel *all* tasks on the loop, not just our tracked ones —
            # otherwise ``aiohttp.ClientSession`` connections created
            # inside SDK coroutines can keep the loop alive past our
            # ``loop.stop()`` call.
            try:
                asyncio.run_coroutine_threadsafe(
                    _cancel_all_tasks(), self._loop,
                ).result(timeout=1.0)
            except Exception:
                pass
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass
        if self._thread is not None:
            # Cap the join so a stuck aiohttp request can't keep the
            # process from exiting — the thread is daemon so the
            # interpreter will kill it on shutdown anyway.
            self._thread.join(timeout=2.0)
        self._bot = None
        self._loop = None
        self._started = False

    # ---------------------------------------------------------- diagnostics
    @staticmethod
    def check_runtime() -> dict:
        """Diagnostic dict mirroring :func:`wdndds.dds.check_runtime`."""
        try:
            import wechatbot as _wb  # noqa: F401

            return {
                "available": True,
                "module": "wechatbot",
                "path": getattr(_wb, "__file__", ""),
                "version": getattr(_wb, "__version__", "0.3.0"),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "available": False,
                "module": "wechatbot",
                "error": f"{type(exc).__name__}: {exc}",
            }

    def __repr__(self) -> str:
        uid = self.user_id or "-"
        state = "running" if self._started else self._login_state
        return f"<Wechat user={uid} state={state}>"


# Make ``Wechat.NoContextError`` / ``MediaError`` / ``AuthError`` /
# ``WeChatBotError`` accessible as nested attributes so consumers can write
# ``except Wechat.NoContextError`` without taking a direct dep on
# ``wechatbot``. We keep the SDK exception types as-is so ``isinstance``
# checks against ``wechatbot.NoContextError`` still match.
Wechat.NoContextError = NoContextError  # type: ignore[attr-defined]
Wechat.AuthError = AuthError  # type: ignore[attr-defined]
Wechat.MediaError = MediaError  # type: ignore[attr-defined]
Wechat.WeChatBotError = WeChatBotError  # type: ignore[attr-defined]
Wechat.Credentials = Credentials  # type: ignore[attr-defined]
Wechat.IncomingMessage = IncomingMessage  # type: ignore[attr-defined]
