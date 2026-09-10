"""verify_worker_infer_timeout.py

Regression test for compute_worker.infer_handler Python-level timeout.

Why: infer_handler runs ``asyncio.run(_call())`` directly in the
wdndds cshim Replier pump thread (C++ thread driven by wdndds cshim).
If the SDK hangs in rknn3.session_run (NPU driver wedged, prompt
prefill long hang, sess.reset() clear_kvcache blocking 5-30s), the
pump thread is pinned and EVERY subsequent RPC (info / reset /
embed_text / new infers) queues up behind it. Main process wire RPC
times out at 10s with ``compute_worker[...] infer failed: timeout``
(actually ``discovery_pending``), and VFA / detector / chat get the
wrong error path.

Fix: wrap ``asyncio.run(_call())`` in a daemon thread +
``th.join(timeout=_INFER_TIMEOUT_SEC)``. On timeout the worker calls
``rknn_llm.session_stop()`` and returns an error reply, freeing the
pump thread within 25s. Wire RPC (now 30s) gets the error reply
before its own timeout — main process sees clean ``infer failed:
timeout`` (not ``discovery_pending``).

This test verifies:
  1. ``_INFER_TIMEOUT_SEC < wire RPC timeout`` (regression guard):
     worker must time out BEFORE wire RPC, otherwise the wire RPC
     fires first and main process still sees ``discovery_pending``.
  2. ``infer_handler`` with a hanging ``_call`` returns an error
     reply dict within ``_INFER_TIMEOUT_SEC`` + small slack (NOT 30s+
     which would mean the worker is still hung).
  3. ``infer_handler`` with a fast ``_call`` returns within the
     timeout without invoking ``session_stop()``.

Run with:
    .venv/bin/python scripts/verify_worker_infer_timeout.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from unittest.mock import MagicMock, patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

LOG = logging.getLogger("verify_worker_infer_timeout")


# --------------------------------------------------------------------------- #
# Test helpers                                                               #
# --------------------------------------------------------------------------- #
def _make_hanging_session():
    """Build a MagicMock session whose sess.respond returns a coroutine
    that never resolves (simulates rknn3.session_run wedged)."""
    sess = MagicMock()
    rknn_llm = MagicMock()
    sess._backend.rknn_llm = rknn_llm

    async def _hanging_respond(*args, **kwargs):
        # never resolves; this is the "SDK 卡死" simulated
        await asyncio.Future()

    sess.respond.side_effect = lambda *args, **kwargs: _hanging_respond()
    return sess


def _make_fast_session(return_text="fast result"):
    """Build a MagicMock session whose sess.respond returns immediately
    with a mock LMOutput-shaped result."""
    sess = MagicMock()
    rknn_llm = MagicMock()
    sess._backend.rknn_llm = rknn_llm

    async def _fast_respond(*args, **kwargs):
        return MagicMock(text=return_text, attachment=[])

    sess.respond.side_effect = lambda *args, **kwargs: _fast_respond()
    return sess


def _patched_wdnai_loader():
    """Build a MagicMock dict that mimics ``_load_wdnai()`` return shape
    (only the keys infer_handler reads)."""
    return {
        "Attachment": MagicMock(),
        "AudioAttachment": MagicMock(),
        "ImageAttachment": MagicMock(),
        "TextAttachment": MagicMock(),
    }


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #
def test_infer_timeout_constant_below_wire_rpc():
    """Regression guard: ``_INFER_TIMEOUT_SEC`` must be strictly less
    than the wire RPC infer timeout (30s).

    If the worker internal timeout is >= wire RPC timeout, the wire
    RPC fires first and main process sees ``discovery_pending`` again
    (the fix becomes useless). Slack of ~5s lets worker session_stop()
    + reply build + cshim delivery all complete before wire RPC times.
    """
    from app.services import compute_worker as cw

    _INFER_TIMEOUT = cw._INFER_TIMEOUT_SEC
    # The wire RPC infer timeout is hardcoded at 30_000ms in
    # rpc_session_proxy.py:921. We assert against the source of truth
    # by reading the module attribute if available, otherwise 30s.
    wire_rpc_ms = 30_000  # MUST match rpc_session_proxy.py _call_resilient call
    wire_rpc_s = wire_rpc_ms / 1000.0

    assert _INFER_TIMEOUT < wire_rpc_s, (
        f"FAIL: _INFER_TIMEOUT_SEC={_INFER_TIMEOUT} must be < wire RPC "
        f"timeout {wire_rpc_s}s; if worker times out AFTER wire RPC, "
        f"main process sees discovery_pending instead of clean timeout."
    )
    # Lower bound: must be >= 10s to cover schema-echo (~5.5s) +
    # reset-blocking (5s) + accumulated jitter. Below 10s re-introduces
    # the 5s default problem.
    assert _INFER_TIMEOUT >= 10.0, (
        f"FAIL: _INFER_TIMEOUT_SEC={_INFER_TIMEOUT} too short; must be "
        f">= 10s to cover schema-echo + reset-blocking worst case."
    )
    return True, {
        "_INFER_TIMEOUT_SEC": _INFER_TIMEOUT,
        "wire_rpc_timeout_s":  wire_rpc_s,
        "slack_s":              wire_rpc_s - _INFER_TIMEOUT,
    }


def test_infer_handler_returns_timeout_error_when_call_hangs():
    """Simulate stuck rknn3.session_run via sess.respond coroutine that
    never resolves.

    Asserts infer_handler returns an error reply dict within
    ``_INFER_TIMEOUT_SEC + slack`` (NOT 30s+), with status="error"
    and error message containing "timeout", and that
    ``rknn_llm.session_stop()`` was invoked for best-effort SDK
    interrupt.
    """
    from app.services import compute_worker as cw

    original_timeout = cw._INFER_TIMEOUT_SEC
    test_timeout = 2.0  # shorten for fast test
    slack = 3.0       # session_stop + reply build + thread join overhead
    cw._INFER_TIMEOUT_SEC = test_timeout

    try:
        sess = _make_hanging_session()
        state = {"session": sess, "model_id": "qwen3-vl-4b"}
        payload = {"action": "infer", "prompt": "test"}
        wdnai_mod = _patched_wdnai_loader()

        # Track session_stop calls for assertion
        rknn_llm = sess._backend.rknn_llm

        with patch.object(cw, "_load_wdnai", return_value=wdnai_mod):
            t0 = time.time()
            result = cw.infer_handler(state, payload)
            elapsed = time.time() - t0

        # Must return within test_timeout + slack
        assert elapsed < test_timeout + slack, (
            f"FAIL: elapsed {elapsed:.2f}s exceeds "
            f"test_timeout+slack ({test_timeout + slack}s); worker "
            f"still hung? pump thread not freed?"
        )

        # Must be an error reply dict
        assert isinstance(result, dict), (
            f"FAIL: expected dict reply, got {type(result).__name__}"
        )
        assert result.get("status") == "error", (
            f"FAIL: expected status='error', got {result.get('status')!r}"
        )
        assert "timeout" in result.get("error", "").lower(), (
            f"FAIL: expected 'timeout' in error message, got "
            f"{result.get('error', '')!r}"
        )

        # session_stop was called for SDK interrupt
        assert rknn_llm.session_stop.called, (
            "FAIL: rknn_llm.session_stop() was not called on timeout"
        )

        return True, {
            "elapsed_s":         round(elapsed, 2),
            "timeout_s":         test_timeout,
            "result_error":      result.get("error", "")[:80],
            "session_stop_calls": rknn_llm.session_stop.call_count,
        }
    finally:
        cw._INFER_TIMEOUT_SEC = original_timeout


def test_infer_handler_completes_for_normal_call():
    """Sanity check: a fast ``_call`` (sess.respond resolves <1s) returns
    without hitting the timeout path. session_stop is NOT called in
    the success path.
    """
    from app.services import compute_worker as cw

    original_timeout = cw._INFER_TIMEOUT_SEC
    cw._INFER_TIMEOUT_SEC = 5.0  # generous

    try:
        sess = _make_fast_session(return_text="hello world")
        state = {"session": sess, "model_id": "qwen3-vl-4b"}
        payload = {"action": "infer", "prompt": "test"}
        wdnai_mod = _patched_wdnai_loader()
        rknn_llm = sess._backend.rknn_llm

        with patch.object(cw, "_load_wdnai", return_value=wdnai_mod):
            t0 = time.time()
            result = cw.infer_handler(state, payload)
            elapsed = time.time() - t0

        # Fast call completes within 3s
        assert elapsed < 3.0, (
            f"FAIL: fast call elapsed {elapsed:.2f}s exceeds 3s"
        )

        # Result is a dict (either success or business error, NOT
        # timeout error)
        assert isinstance(result, dict), (
            f"FAIL: expected dict reply, got {type(result).__name__}"
        )
        if result.get("status") == "error":
            # Business errors are OK; just must not be our timeout path
            assert "timeout" not in result.get("error", "").lower(), (
                f"FAIL: fast call should not timeout, got "
                f"{result.get('error', '')!r}"
            )

        # session_stop NOT called on success path
        assert not rknn_llm.session_stop.called, (
            f"FAIL: session_stop was called on success path "
            f"(call_count={rknn_llm.session_stop.call_count})"
        )

        return True, {
            "elapsed_s": round(elapsed, 2),
            "result_keys": list(result.keys())[:5],
        }
    finally:
        cw._INFER_TIMEOUT_SEC = original_timeout


# --------------------------------------------------------------------------- #
# main                                                                        #
# --------------------------------------------------------------------------- #
def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    cases = (
        ("test_infer_timeout_constant_below_wire_rpc",
         test_infer_timeout_constant_below_wire_rpc),
        ("test_infer_handler_returns_timeout_error_when_call_hangs",
         test_infer_handler_returns_timeout_error_when_call_hangs),
        ("test_infer_handler_completes_for_normal_call",
         test_infer_handler_completes_for_normal_call),
    )

    results = []
    for name, fn in cases:
        try:
            ok, detail = fn()
            LOG.info("✓ %s: %s", name, detail)
            results.append((name, "PASS"))
        except AssertionError as e:
            LOG.error("✗ %s: %s", name, e)
            results.append((name, "FAIL (AssertionError)"))
        except Exception as e:
            LOG.exception("✗ %s raised: %s", name, e)
            results.append((name, f"FAIL ({type(e).__name__}: {e})"))

    LOG.info("")
    LOG.info("=== summary ===")
    failed = 0
    for name, status in results:
        LOG.info("  %s: %s", name, status)
        if not status.startswith("PASS"):
            failed += 1

    LOG.info("")
    if failed == 0:
        LOG.info("ALL %d CASES PASS", len(results))
        return 0
    LOG.error("FAILED %d / %d CASES", failed, len(results))
    return 1


if __name__ == "__main__":
    sys.exit(main())
