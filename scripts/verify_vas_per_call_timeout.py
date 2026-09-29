"""verify_vas_per_call_timeout.py

Regression test for vas_service._call_llm per-call timeout override.

Why: commit 90faa84 tightened the global _INFER_TIMEOUT_SEC to 5s.
The final-summary / chunk-summary / rescue-batch calls in VAS are
legitimately slower than 5s on real hardware (Apollo final summary
~25-30s on qwen3-vl-4b per the original 46135b2 commit's own docstring).
Those calls now pass ``timeout_sec=...`` explicitly so they are not
killed by the per-frame default, while per-frame calls keep the default.

v0.15 (方案A 短期降级): per-frame default 5s → 10s, 对齐 5c33fb3 wire
10s 上限。qwen3.5-vl-4b schema-constrained mode ~46% 概率 schema-echo
稳态 5.5-6s, 5s 误杀健康慢路径。10s 与 wire RPC layer 触底时序几乎重合,
保留 _on_call_timeout ≥2 次连续 → worker restart 兜底。

This test verifies the contract:
  1. per-call timeout override is honored (asyncio.wait_for sees the
     override, not the module default);
  2. per-frame default still works (no override ⇒ module default);
  3. timeout path returns the same "(推理超时：Xs 内 LLM 未响应)" /
     "(推理失败：超时 Xs SDK 未响应)" strings the production uses;
  4. counter / on_call_timeout behavior is unchanged.

Run with:
    .venv/bin/python scripts/verify_vas_per_call_timeout.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

LOG = logging.getLogger("verify_vas_per_call_timeout")


# --------------------------------------------------------------------------- #
# Stubs                                                                       #
# --------------------------------------------------------------------------- #
class _NoLock:
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _StubSessionHanging:
    """Fake session that hangs forever. Used to drive the timeout path."""
    def __init__(self):
        self.reset_calls = 0

    def reset(self):
        self.reset_calls += 1

    def respond(self, *, prompt=None, attachment=None, generating=None, **kwargs):
        async def _hang():
            await asyncio.Future()  # never resolves
        return _hang()


class _StubSessionSlow:
    """Fake session that sleeps for ``delay`` seconds then returns text."""
    def __init__(self, delay: float, return_text: str = "ok"):
        self.delay = delay
        self.return_text = return_text
        self.reset_calls = 0

    def reset(self):
        self.reset_calls += 1

    def respond(self, *, prompt=None, attachment=None, generating=None, **kwargs):
        async def _slow():
            await asyncio.sleep(self.delay)
            return self.return_text
        return _slow()


class _StubRegistry:
    def __init__(self, session_factory):
        self._session_factory = session_factory
        self.get_session_calls = 0
        self.stop_instance_local_calls = []

    def get_session(self, capability: str):
        self.get_session_calls += 1
        return self._session_factory()

    def list_with_source(self):
        return [{
            "model_id": "qwen3-vl-4b",
            "capability_types": ["swcap_video_describe"],
            "status": "running",
        }]

    def stop_instance_local(self, model_id: str):
        self.stop_instance_local_calls.append(model_id)


def _patches_for(registry):
    from app.apps.video_analysis_search import service as vas_mod
    from app.core.compute import registry as registry_mod
    patches = [
        patch.object(registry_mod, "get_registry", lambda: registry),
        patch.object(registry_mod, "get_session_call_lock", lambda: _NoLock()),
        patch.object(vas_mod, "_load_wdnai",
                     lambda: (True, lambda *a, **kw: None, "")),
    ]
    for p in patches:
        p.start()
    return patches


def _make_service():
    from collections import deque
    from app.apps.video_analysis_search.service import VideoAnalysisSearchService
    svc = VideoAnalysisSearchService.__new__(VideoAnalysisSearchService)
    svc._consecutive_timeouts = 0
    # v0.15 telemetry 字段: 测试 stub service 也得初始化, 否则
    # _record_infer_duration 调用会 AttributeError
    svc._infer_durations = deque(maxlen=200)
    svc._last_telemetry_check_ts = 0.0
    return svc


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #
def test_per_call_timeout_override_honored():
    """A 1s delay should NOT be killed by the 5s module default when
    the caller passes ``timeout_sec=3.0``.

    Verifies: override reaches asyncio.wait_for and t.join.
    """
    from app.apps.video_analysis_search import service as vas_mod

    real_timeout = vas_mod._INFER_TIMEOUT_SEC
    real_rescue = vas_mod.RESCUE_TIMEOUT_SEC
    real_chunk = vas_mod.CHUNK_TIMEOUT_SEC
    real_final = vas_mod.FINAL_SUMMARY_TIMEOUT_SEC
    vas_mod._INFER_TIMEOUT_SEC = 5.0
    vas_mod.RESCUE_TIMEOUT_SEC = 3.0
    try:
        sess = _StubSessionSlow(delay=1.0, return_text="ok")
        registry = _StubRegistry(lambda: sess)
        patches = _patches_for(registry)
        try:
            svc = _make_service()
            t0 = time.time()
            result = svc._call_llm(
                "test", None, timeout_sec=vas_mod.RESCUE_TIMEOUT_SEC,
            )
            elapsed = time.time() - t0
        finally:
            for p in patches:
                p.stop()

        assert result == "ok", (
            f"FAIL: expected 'ok' (call completed within override timeout), "
            f"got {result!r}"
        )
        assert 0.9 <= elapsed <= 2.0, (
            f"FAIL: elapsed {elapsed:.2f}s not in [0.9, 2.0] "
            f"(1s call + slack, but module default 5s should NOT have fired)"
        )
        return True, (result, round(elapsed, 2))
    finally:
        vas_mod._INFER_TIMEOUT_SEC = real_timeout
        vas_mod.RESCUE_TIMEOUT_SEC = real_rescue
        vas_mod.CHUNK_TIMEOUT_SEC = real_chunk
        vas_mod.FINAL_SUMMARY_TIMEOUT_SEC = real_final


def test_per_call_timeout_kills_legit_slow_call():
    """A 5s delay must be killed when caller passes ``timeout_sec=2.0``,
    even though the module default is 5s (which would otherwise allow it).
    """
    from app.apps.video_analysis_search import service as vas_mod

    real_timeout = vas_mod._INFER_TIMEOUT_SEC
    real_rescue = vas_mod.RESCUE_TIMEOUT_SEC
    vas_mod._INFER_TIMEOUT_SEC = 5.0
    vas_mod.RESCUE_TIMEOUT_SEC = 2.0
    try:
        sess = _StubSessionSlow(delay=5.0)
        registry = _StubRegistry(lambda: sess)
        patches = _patches_for(registry)
        try:
            svc = _make_service()
            t0 = time.time()
            result = svc._call_llm(
                "test", None, timeout_sec=vas_mod.RESCUE_TIMEOUT_SEC,
            )
            elapsed = time.time() - t0
        finally:
            for p in patches:
                p.stop()

        assert result.startswith("(推理") and "未响应" in result or "SDK 未响应" in result, (
            f"FAIL: expected timeout error string, got {result!r}"
        )
        # Must be killed at ~2s (override), NOT 5s (module default).
        # Allow upper bound of ~6s for _interrupt_rknn_session 5s wait.
        assert 1.8 <= elapsed <= 6.5, (
            f"FAIL: elapsed {elapsed:.2f}s not in [1.8, 6.5] "
            f"(should be killed by 2s override + 5s interrupt wait slack)"
        )
        return True, (result, round(elapsed, 2))
    finally:
        vas_mod._INFER_TIMEOUT_SEC = real_timeout
        vas_mod.RESCUE_TIMEOUT_SEC = real_rescue


def test_no_override_uses_module_default():
    """No ``timeout_sec`` ⇒ falls back to ``_INFER_TIMEOUT_SEC`` (5s).

    A 1s call completes; a hypothetical 6s call would be killed.
    """
    from app.apps.video_analysis_search import service as vas_mod

    real_timeout = vas_mod._INFER_TIMEOUT_SEC
    vas_mod._INFER_TIMEOUT_SEC = 5.0
    try:
        sess = _StubSessionSlow(delay=1.0, return_text="per-frame-ok")
        registry = _StubRegistry(lambda: sess)
        patches = _patches_for(registry)
        try:
            svc = _make_service()
            t0 = time.time()
            result = svc._call_llm("per-frame", None)
            elapsed = time.time() - t0
        finally:
            for p in patches:
                p.stop()

        assert result == "per-frame-ok", (
            f"FAIL: expected 'per-frame-ok', got {result!r}"
        )
        assert 0.9 <= elapsed <= 2.0, (
            f"FAIL: elapsed {elapsed:.2f}s out of range"
        )
        return True, (result, round(elapsed, 2))
    finally:
        vas_mod._INFER_TIMEOUT_SEC = real_timeout


def test_summary_constants_above_5s():
    """Slow-path constants must exceed the per-frame default — that's the entire
    point of the fix. If any regress to ≤ per-frame, the regression returns.

    v0.15: per-frame default 5s → 10s (对齐 5c33fb3 wire 10s). Slow-path
    constants (rescue 30s, chunk 30s, final 60s) 必须仍 > per-frame。
    """
    from app.apps.video_analysis_search import service as vas_mod
    per_frame = vas_mod._INFER_TIMEOUT_SEC
    assert per_frame == 15.0, (
        f"FAIL: per-frame default must be 15s (v0.15 方案B 缓解 10-12s 健康慢路径), "
        f"got {per_frame}"
    )
    assert vas_mod.RESCUE_TIMEOUT_SEC > per_frame, (
        f"FAIL: RESCUE_TIMEOUT_SEC={vas_mod.RESCUE_TIMEOUT_SEC} must "
        f"exceed per-frame default {per_frame}s (rescue batch legitimately slow)"
    )
    assert vas_mod.CHUNK_TIMEOUT_SEC > per_frame, (
        f"FAIL: CHUNK_TIMEOUT_SEC={vas_mod.CHUNK_TIMEOUT_SEC} must "
        f"exceed per-frame default {per_frame}s (chunk summary legitimately slow)"
    )
    assert vas_mod.FINAL_SUMMARY_TIMEOUT_SEC > per_frame, (
        f"FAIL: FINAL_SUMMARY_TIMEOUT_SEC={vas_mod.FINAL_SUMMARY_TIMEOUT_SEC} "
        f"must exceed per-frame default {per_frame}s "
        f"(Apollo final summary ~25-30s on qwen3-vl-4b)"
    )
    return True, {
        "_INFER_TIMEOUT_SEC":      per_frame,
        "RESCUE_TIMEOUT_SEC":      vas_mod.RESCUE_TIMEOUT_SEC,
        "CHUNK_TIMEOUT_SEC":       vas_mod.CHUNK_TIMEOUT_SEC,
        "FINAL_SUMMARY_TIMEOUT_SEC": vas_mod.FINAL_SUMMARY_TIMEOUT_SEC,
    }


# --------------------------------------------------------------------------- #
# main                                                                        #
# --------------------------------------------------------------------------- #
def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    cases = (
        ("test_per_call_timeout_override_honored",
         test_per_call_timeout_override_honored),
        ("test_per_call_timeout_kills_legit_slow_call",
         test_per_call_timeout_kills_legit_slow_call),
        ("test_no_override_uses_module_default",
         test_no_override_uses_module_default),
        ("test_summary_constants_above_5s",
         test_summary_constants_above_5s),
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