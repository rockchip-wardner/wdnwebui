"""verify_worker_infer_timeout.py

Regression test for the compute worker's replier-pump bound.

Why this matters
----------------

The wdndds cshim runs **one serial pump thread per replier**. Inference
happens on that thread. So if a backend hangs inside its C call — a
wedged NPU driver, a long prefill, a ``clear_kvcache`` that blocks for
30 s — the pump is pinned and *every* subsequent request queues behind
it: ``info``, ``reset``, ``count_tokens``, the next infer. The worker
stops being slow and becomes deaf, and the main process only ever sees
opaque timeouts with nothing pointing at the cause.

``wdndds.serve_local_session(..., infer_timeout_s=…)`` bounds this. On
expiry the pump is released, the caller gets a clean error reply, and
the worker keeps serving.

What this test checks
---------------------
  1. The worker's own bound stays below the main process's inference
     budget, so the failure arrives as a worker reply that names the
     problem instead of as a client-side timeout.
  2. A hanging session produces an error reply within (bound + slack) —
     not within the full client budget.
  3. A fast session returns normally, without tripping the bound, and
     its text survives the round trip.
  4. After a timeout the pump is *still usable*: a subsequent request on
     the same service succeeds. This is the property the old
     worker-side timeout existed to provide, so it is the one that
     actually matters.

Run::

    .venv/bin/python scripts/verify_worker_infer_timeout.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from typing import Any, List, Optional, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

LOG = logging.getLogger("verify_worker_infer_timeout")

# Bound used by the worker; kept short so the test finishes quickly.
_TEST_TIMEOUT_SEC = 2.0
_SLACK_SEC = 2.0


# --------------------------------------------------------------------------- #
# Session stubs — real wdnai.LLMSession subclasses, not mocks                   #
# --------------------------------------------------------------------------- #
def _spec(kind: str = "llm"):
    class _Spec:
        def __init__(self) -> None:
            self.kind = kind
            self.model_id = "timeout-test"
            self.npu = 0

    return _Spec()


class _HangingSession:
    """A session whose ``run_prompt`` never returns — a wedged backend."""

    def __init__(self, spec: Any) -> None:
        self.spec = spec

    async def run_prompt(self, prompt: str, attachment=None, *,
                         on_delta=None, **overrides: Any):
        await asyncio.Future()  # never resolves
        raise AssertionError("unreachable")

    def reset(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FastSession:
    """A session that answers immediately."""

    def __init__(self, spec: Any) -> None:
        self.spec = spec

    async def run_prompt(self, prompt: str, attachment=None, *,
                         on_delta=None, **overrides: Any):
        return "fast result", []

    def reset(self) -> None:
        pass

    def close(self) -> None:
        pass


def _wait_for_service(client, deadline_s: float = 10.0) -> None:
    """Block until the replier is discoverable over DDS."""
    end = time.time() + deadline_s
    while time.time() < end:
        try:
            if client._call_once({"action": "info", "op": "info"}, 5_000).get("ok"):
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.25)
    raise AssertionError("service never became discoverable")


def _make_pair(session, tag: str):
    """Serve ``session`` and dial it back, on a per-test unique name."""
    from wdndds import LLMRPCSession, serve_local_session

    name = f"wdndds/rpc/timeout-test-{os.getpid()}-{tag}"
    served = serve_local_session(
        session, service_name=name, infer_timeout_s=_TEST_TIMEOUT_SEC)
    client = LLMRPCSession(
        session.spec, service_name=name,
        infer_timeout_ms=60_000, timeout_ms=10_000)
    return served, client


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #
def test_worker_bound_is_below_client_budget():
    from app.core.compute.proxy import _INFER_TIMEOUT_MS
    from app.core.compute.workers import _base

    worker_ms = _base._INFER_TIMEOUT_SEC * 1000.0
    client_ms = float(_INFER_TIMEOUT_MS)
    assert worker_ms < client_ms, (
        f"worker bound {_base._INFER_TIMEOUT_SEC}s must be under the client's "
        f"{client_ms}ms infer budget, or the client times out first and the "
        f"failure never gets a reason attached")
    # And it must be long enough for a real 768px VLM frame (~35s).
    assert _base._INFER_TIMEOUT_SEC >= 45.0, (
        f"{_base._INFER_TIMEOUT_SEC}s is too tight for a 768px VLM frame; "
        f"a slow-but-healthy turn would be reported as a wedge")


def test_hang_returns_error_within_bound():
    served, client = _make_pair(_HangingSession(_spec()), "hang")
    try:
        _wait_for_service(client)
        t0 = time.time()
        try:
            asyncio.run(client.respond(prompt="hello"))
            raise AssertionError("hanging session did not time out")
        except RuntimeError as exc:
            elapsed = time.time() - t0
            detail = str(exc).lower()
            assert "timeout" in detail or "timed out" in detail, exc
            assert elapsed < _TEST_TIMEOUT_SEC + _SLACK_SEC, (
                f"took {elapsed:.1f}s — the pump was still held past the bound")
            assert elapsed < _TEST_TIMEOUT_SEC * 3, (
                f"took {elapsed:.1f}s — client budget fired instead of the "
                f"worker bound, so the caller got no diagnosis")
    finally:
        client.close()
        served.close()


def test_fast_session_round_trips_without_tripping_the_bound():
    served, client = _make_pair(_FastSession(_spec()), "fast")
    try:
        _wait_for_service(client)
        t0 = time.time()
        out = asyncio.run(client.respond(prompt="hello"))
        elapsed = time.time() - t0
        assert out == "fast result", out
        assert elapsed < _TEST_TIMEOUT_SEC, (
            f"fast session took {elapsed:.1f}s — bound is misfiring")
    finally:
        client.close()
        served.close()


def test_pump_survives_a_timeout():
    """The property the bound exists for.

    A wedged request must not leave the service unusable: the next
    caller has to get a real answer. If the pump were still pinned, the
    worker would be permanently deaf and every later error would look
    like a transport fault.
    """
    # Same session object so the pinned thread stays hung in the
    # background — exactly the situation a wedged NPU leaves behind.
    session = _FastSession(_spec())
    served, client = _make_pair(session, "survive")
    try:
        _wait_for_service(client)

        # First call: make the session hang just for this one request.
        async def _hang(*args, **kwargs):  # noqa: ARG001
            await asyncio.Future()  # never resolves

        session.run_prompt = _hang  # type: ignore[assignment]
        t0 = time.time()
        try:
            asyncio.run(client.respond(prompt="wedge me"))
            raise AssertionError("expected the wedged call to time out")
        except RuntimeError as exc:
            wedged_for = time.time() - t0
            # Guard the test itself: an immediate failure (bad stub, typo in
            # the coroutine) also raises RuntimeError, but proves nothing
            # about the pump. The wedged path must actually burn the bound.
            assert wedged_for >= _TEST_TIMEOUT_SEC * 0.8, (
                f"the 'wedged' call returned after only {wedged_for:.2f}s — "
                f"it failed fast rather than timing out, so this test would "
                f"pass without exercising anything: {exc}")
            assert wedged_for < _TEST_TIMEOUT_SEC + _SLACK_SEC, (
                f"wedged call took {wedged_for:.2f}s, past the bound")

        # Next call: backend recovered.
        session.run_prompt = _FastSession.run_prompt.__get__(session)  # type: ignore[assignment]
        out = asyncio.run(client.respond(prompt="are you there?"))
        assert out == "fast result", (
            f"pump still pinned after a timeout — got {out!r}")
    finally:
        client.close()
        served.close()


def main() -> int:
    tests: List[Tuple[str, Any]] = sorted(
        (n, f) for n, f in globals().items()
        if n.startswith("test_") and callable(f))
    failures = 0
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
            print(f"  PASS  {name}  ({time.time() - t0:.1f}s)")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    print("-" * 60)
    print(f"{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())