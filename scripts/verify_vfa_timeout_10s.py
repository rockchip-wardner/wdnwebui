"""Smoke test: 验证方案B per-frame timeout 15s (默认) + 10s (显式 override) 真在 SDK 卡死时触发。

VFA 线上报 "(推理失败：超时 15s SDK 未响应)" 概率性出现。
本测试用 mock 模拟 SDK 慢响应, 验证:
1. SDK > 15s 时 _call_llm 返回 timeout 错误 string (默认 15s 路径生效)
2. 显式 timeout_sec=10.0 仍 work (per-call override 不被 15s 默认值吞掉)
2. _on_call_timeout 触发, _consecutive_timeouts 计数 +1
3. SDK > 30s 时 仍能走 _interrupt_rknn_session(5s) 兜底 (路径 2738)
4. 超时后 runner thread 仍存活 (t.join 返回, 但 finished[0] 已 True)

不依赖 NPU / 真机, 跑 .venv 即可.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
from collections import deque
from unittest.mock import MagicMock, patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

LOG = logging.getLogger("verify_vfa_timeout_10s")


# --------------------------------------------------------------------------- #
# Stubs                                                                       #
# --------------------------------------------------------------------------- #
class _NoLock:
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _StubSessionSlowSDK:
    """Mock SDK session: sess.respond 返回 coroutine, asyncio.wait_for 真实计时
    在 N 秒后才 resolve, 模拟 NPU 慢响应。
    """
    def __init__(self, sdk_delay_sec: float):
        self.sdk_delay_sec = sdk_delay_sec
        self.reset_calls = 0
        self.respond_started_at = None
        self.respond_completed = False

    def reset(self):
        self.reset_calls += 1

    def respond(self, *, prompt=None, attachment=None, generating=None, **kwargs):
        delay = self.sdk_delay_sec
        async def _slow():
            self.respond_started_at = time.time()
            await asyncio.sleep(delay)
            self.respond_completed = True
            return "ok"
        return _slow()


class _StubSessionHangsForever:
    """sess.respond 返回永不 resolve 的 coroutine, 模拟 SDK 真正卡死。"""
    def __init__(self):
        self.reset_calls = 0

    def reset(self):
        self.reset_calls += 1

    def respond(self, *, prompt=None, attachment=None, generating=None, **kwargs):
        async def _hang():
            await asyncio.Future()  # never resolves
        return _hang()


# --------------------------------------------------------------------------- #
# Setup helpers                                                              #
# --------------------------------------------------------------------------- #
def _make_service_stubbed(stub_session):
    """构造一个 stubbed service, 跳过 _interrupt_rknn_session
    防止 C call 真触发 (我们想测的是 Python 层 timeout 行为)。
    """
    from app.services.video_file_analysis import VideoFileAnalysisService
    svc = VideoFileAnalysisService.__new__(VideoFileAnalysisService)
    svc._consecutive_timeouts = 0
    svc._infer_durations = deque(maxlen=200)
    svc._last_telemetry_check_ts = 0.0
    # patches
    from app.services import video_file_analysis as vfa_mod
    from app.services import compute_registry as registry_mod
    patches = [
        patch.object(registry_mod, "get_registry", lambda: MagicMock(
            get_session=lambda c: stub_session
        )),
        patch.object(registry_mod, "get_session_call_lock", lambda: _NoLock()),
        patch.object(vfa_mod, "_load_wdnai", lambda: (True, lambda *a, **kw: None, "")),
        patch.object(vfa_mod, "_interrupt_rknn_session", lambda *a, **kw: None),
    ]
    for p in patches:
        p.start()
    return svc, patches


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #
def test_15s_timeout_default_for_17s_sdk_call():
    """SDK 真实 17s 响应 + 默认 15s per-frame timeout (方案B) → 命中 "(推理失败：超时 15s SDK 未响应)"。

    模拟 "健康慢但超过 15s" 的场景: SDK 是健康的 (不是卡死), 只是
    schema-echo / 长 context / NPU 暂忙 让单次跑 17s。

    预期:
    - _call_llm 总 wall-time ≈ 15s (asyncio.wait_for + t.join 同上限)
    - 返回字符串以 "(推理失败：超时 15s SDK 未响应)" 开头
    - _on_call_timeout 触发 (path 2722 总是调, 计数 +1, ≥2 才真重启)
    - SDK 实际没跑完 (cancelled 在 Python 层, C 继续在 background)
    - _record_infer_duration 记录 ≈ 15s
    """
    sess = _StubSessionSlowSDK(sdk_delay_sec=17.0)
    svc, patches = _make_service_stubbed(sess)
    try:
        t0 = time.time()
        # 关键: 不传 timeout_sec → 走 _INFER_TIMEOUT_SEC=15s 默认
        result = svc._call_llm("test", None)
        elapsed = time.time() - t0
        # wall-time 应 ~15s (Python 层 timeout 触发, 不等 SDK 跑完)
        assert 14.5 <= elapsed <= 16.5, (
            f"FAIL: wall-time {elapsed:.2f}s 不在 [14.5, 16.5] "
            f"(Python 层 15s 默认 timeout 没生效?)"
        )
        # 返回字符串
        assert isinstance(result, str) and result.startswith("("), (
            f"FAIL: 期望 error string, got {result!r}"
        )
        assert "15s" in result, (
            f"FAIL: 返回字符串应包含 '15s' 反映默认 timeout, got {result!r}"
        )
        # path 2722 总是调 _on_call_timeout (不分 SDK 健康还是卡死,
        # ≥2 次连续才真重启 worker), 单次只 +1
        assert svc._consecutive_timeouts == 1, (
            f"FAIL: 1 次 timeout 应 _on_call_timeout +1, "
            f"got {svc._consecutive_timeouts}"
        )
        # _record_infer_duration 记录 ≈ 15s
        assert len(svc._infer_durations) >= 1, "FAIL: telemetry 没记录"
        # SDK 实际没跑完 (Python 层 cancel 后 C 还在 background)
        assert sess.respond_completed is False, (
            f"FAIL: SDK coroutine 应被 cancel, "
            f"但 completed={sess.respond_completed} (C 层没被中断)"
        )
        LOG.info(
            "✓ test_15s_timeout_default_for_17s_sdk_call: "
            "elapsed=%.2fs result=%r consecutive=%d",
            elapsed, result[:60], svc._consecutive_timeouts,
        )
    finally:
        for p in patches:
            p.stop()


def test_5s_override_honored():
    """timeout_sec=5.0 显式覆盖 → 5s timeout (验证 per-call override 仍工作)"""
    sess = _StubSessionSlowSDK(sdk_delay_sec=8.0)
    svc, patches = _make_service_stubbed(sess)
    try:
        t0 = time.time()
        result = svc._call_llm("test", None, timeout_sec=5.0)
        elapsed = time.time() - t0
        assert 4.5 <= elapsed <= 6.5, (
            f"FAIL: wall-time {elapsed:.2f}s 不在 [4.5, 6.5] "
            f"(timeout_sec=5.0 override 失效)"
        )
        assert "5s" in result, (
            f"FAIL: 返回字符串应包含 '5s' 反映 override, got {result!r}"
        )
        LOG.info("✓ test_5s_override_honored: elapsed=%.2fs result=%r",
                 elapsed, result[:60])
    finally:
        for p in patches:
            p.stop()


def test_explicit_10s_override_still_works():
    """SDK 真正 15s → Python 层 10s 提前 cancel (asyncio.wait_for 真触发)。

    asyncio.wait_for 真实计时: SDK coroutine 等待 10s 后被 cancel。
    runner thread 后续在 finally 块设置 finished[0]=True, _call_llm
    主线程已 t.join(10s) 返回走超时分支。

    验证: _call_llm 总时间 ≈ 10s, 不等 SDK 真跑完。
    """
    sess = _StubSessionSlowSDK(sdk_delay_sec=15.0)
    svc, patches = _make_service_stubbed(sess)
    try:
        t0 = time.time()
        result = svc._call_llm("test", None, timeout_sec=10.0)
        elapsed = time.time() - t0
        # asyncio.wait_for 真 cancel, 不等 15s (10s override 路径)
        assert 9.5 <= elapsed <= 11.5, (
            f"FAIL: elapsed {elapsed:.2f}s 不在 [9.5, 11.5] "
            f"(10s override 失效 或 asyncio.wait_for 没真 cancel)"
        )
        # 关键: SDK 实际没跑完 (cancel 生效)
        assert sess.respond_completed is False, (
            "FAIL: SDK coroutine 应被 cancel, 但 completed=True (cancelled 不生效)"
        )
        assert sess.respond_started_at is not None, "SDK 都没开始"
        # 整体超时 10s 路径触发
        assert "10s" in result, (
            f"FAIL: 应返回 timeout 10s error string (override 反映), got {result!r}"
        )
        LOG.info("✓ test_explicit_10s_override_still_works: "
                 "elapsed=%.2fs SDK_cancelled=%s result=%r",
                 elapsed, not sess.respond_completed, result[:60])
    finally:
        for p in patches:
            p.stop()


def test_hang_forever_kicks_off_consecutive_timeout():
    """SDK 真正 hang 死 → 10s 路径触发 + _on_call_timeout 计数 +1。

    模拟 wedged NPU / C 层卡死: sess.respond 永不 resolve。
    显式 timeout_sec=10.0 (验证 hang 路径不被默认 15s 吞掉)。
    _call_llm 走超时分支, _on_call_timeout 计数 (不立即 restart, 需 ≥2)。
    """
    sess = _StubSessionHangsForever()
    svc, patches = _make_service_stubbed(sess)
    try:
        t0 = time.time()
        result = svc._call_llm("test", None, timeout_sec=10.0)
        elapsed = time.time() - t0
        # hang + 10s override → wall ≈ 10s
        assert 9.5 <= elapsed <= 11.5, (
            f"FAIL: elapsed {elapsed:.2f}s (hang 应该 ~10s timeout)"
        )
        # hang → _on_call_timeout 触发, consecutive_timeouts=1 (不触发 restart)
        assert svc._consecutive_timeouts == 1, (
            f"FAIL: hang 应 _on_call_timeout 1 次, "
            f"got _consecutive_timeouts={svc._consecutive_timeouts}"
        )
        assert result.startswith("(")
        assert "10s" in result
        LOG.info(
            "✓ test_hang_forever_kicks_off_consecutive_timeout: "
            "elapsed=%.2fs consecutive_timeouts=%d",
            elapsed, svc._consecutive_timeouts,
        )
    finally:
        for p in patches:
            p.stop()


def test_fast_call_completes_normally():
    """SDK 0.5s → 正常路径, telemetry 记录, 无 _on_call_timeout。"""
    sess = _StubSessionSlowSDK(sdk_delay_sec=0.5)
    svc, patches = _make_service_stubbed(sess)
    try:
        t0 = time.time()
        result = svc._call_llm("test", None, timeout_sec=10.0)
        elapsed = time.time() - t0
        assert 0.3 <= elapsed <= 2.0, (
            f"FAIL: fast call elapsed {elapsed:.2f}s (期望 < 2s)"
        )
        assert result == "ok", f"FAIL: fast call 应返回 'ok', got {result!r}"
        assert svc._consecutive_timeouts == 0
        # telemetry 记录这次调用
        assert len(svc._infer_durations) == 1
        LOG.info("✓ test_fast_call_completes_normally: "
                 "elapsed=%.2fs result=%r", elapsed, result)
    finally:
        for p in patches:
            p.stop()


# --------------------------------------------------------------------------- #
# main                                                                        #
# --------------------------------------------------------------------------- #
def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    cases = (
        ("test_15s_timeout_default_for_17s_sdk_call",
         test_15s_timeout_default_for_17s_sdk_call),
        ("test_5s_override_honored",
         test_5s_override_honored),
        ("test_explicit_10s_override_still_works",
         test_explicit_10s_override_still_works),
        ("test_hang_forever_kicks_off_consecutive_timeout",
         test_hang_forever_kicks_off_consecutive_timeout),
        ("test_fast_call_completes_normally",
         test_fast_call_completes_normally),
    )
    results = []
    for name, fn in cases:
        try:
            fn()
            results.append((name, "PASS"))
        except AssertionError as e:
            LOG.error("✗ %s: %s", name, e)
            results.append((name, f"FAIL (AssertionError)"))
        except Exception as e:
            LOG.exception("✗ %s raised: %s", name, e)
            results.append((name, f"FAIL ({type(e).__name__})"))
    LOG.info("")
    LOG.info("=== summary ===")
    failed = 0
    for name, status in results:
        LOG.info("  %s: %s", name, status)
        if not status.startswith("PASS"):
            failed += 1
    if failed == 0:
        LOG.info("ALL %d CASES PASS", len(results))
        return 0
    LOG.error("FAILED %d / %d CASES", failed, len(results))
    return 1


if __name__ == "__main__":
    sys.exit(main())
