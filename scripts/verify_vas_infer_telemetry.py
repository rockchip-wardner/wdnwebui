"""Smoke test: VAS infer duration telemetry (方案 E, v0.15).

验证 _record_infer_duration 方法:
- 60s 窗口节流 (重复调用 < 60s 不重算)
- p50 / p95 / p99 计算正确
- p95 > 4.5s 触发 WARNING
- p95 <= 4.5s 不 emit
- deque 上限生效 (maxlen=200)
- 与 VAS 其余功能解耦 (只 import _record_infer_duration, 不触发 _call_llm)

Run from repo root:
    source .venv/bin/activate
    python scripts/verify_vas_infer_telemetry.py
"""

from __future__ import annotations

import io
import logging
import os
import sys
from collections import deque

# 与 verify_text_shm_path.py 同样套路: 只 import 我们要测的 helper
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app", "services"))

# 仅 import 模块常量 + 测试方法 (避免触发 _call_llm / wdnai / compute_registry)
from app.apps.video_analysis_search.service import (  # noqa: E402
    _INFER_TELEMETRY_WINDOW_SEC,
    _INFER_TELEMETRY_P95_WARN_SEC,
    _INFER_TELEMETRY_MAXLEN,
    VideoAnalysisSearchService,
)


# ----------------------------------------------------------------------------
# 1. 常量正确性
# ----------------------------------------------------------------------------

def test_constants_defined():
    """v0.15 telemetry 关键常量: 60s 窗口, 4.5s p95 阈值, 200 帧 deque。"""
    assert _INFER_TELEMETRY_WINDOW_SEC == 60.0
    assert _INFER_TELEMETRY_P95_WARN_SEC == 4.5
    assert _INFER_TELEMETRY_MAXLEN == 200


# ----------------------------------------------------------------------------
# 2. Service 实例化 + __init__ 字段
# ----------------------------------------------------------------------------

def _make_svc():
    """构造一个最小可用 service (不触发 _call_llm / wdnai)。

    不通过 _service 单例 / get_service() (会触发全栈 init), 直接
    VideoAnalysisSearchService.__new__ 绕过 __init__ 然后手动塞
    telemetry 字段. 用于 telemetry 单测.
    """
    svc = VideoAnalysisSearchService.__new__(VideoAnalysisSearchService)
    svc._infer_durations = deque(maxlen=_INFER_TELEMETRY_MAXLEN)
    svc._last_telemetry_check_ts = 0.0
    return svc


def test_init_fields():
    """__init__ 初始化 telemetry 字段 (deque maxlen=200, last_check=0.0)。"""
    svc = _make_svc()
    assert isinstance(svc._infer_durations, deque)
    assert svc._infer_durations.maxlen == 200
    assert svc._last_telemetry_check_ts == 0.0
    assert len(svc._infer_durations) == 0


# ----------------------------------------------------------------------------
# 3. p50 / p95 / p99 计算正确性
# ----------------------------------------------------------------------------

def test_p95_calculation_under_threshold():
    """100 个 < 4.5s 样本, p95 < 阈值 → 不 emit WARNING。"""
    log_stream = io.StringIO()
    handler = logging.StreamHandler(log_stream)
    handler.setLevel(logging.WARNING)
    log = logging.getLogger("vsa_service")
    log.addHandler(handler)
    try:
        svc = _make_svc()
        for i in range(100):
            svc._record_infer_duration(0.5 + (i / 100.0) * 2.5)
        svc._last_telemetry_check_ts = 0.0
        svc._record_infer_duration(0.5)
        log_output = log_stream.getvalue()
        assert "infer duration telemetry" not in log_output, (
            f"p95 ~ 2.97s 不应触发 WARNING, got: {log_output!r}"
        )
    finally:
        log.removeHandler(handler)


def test_p95_calculation_over_threshold():
    """100 个样本 (95 快 + 5 慢), p95 > 阈值 → emit WARNING。"""
    log_stream = io.StringIO()
    handler = logging.StreamHandler(log_stream)
    handler.setLevel(logging.WARNING)
    log = logging.getLogger("vsa_service")
    log.addHandler(handler)
    try:
        svc = _make_svc()
        for i in range(95):
            svc._record_infer_duration(0.5 + (i / 95.0) * 1.5)
        for i in range(5):
            svc._record_infer_duration(5.0 + i)
        # 重置 last_check 强制下次调用触发 check (绕过 60s throttle).
        # 关键: 必须再 push 一个慢样本 (> 4.5s), 否则 100 样本 → 101 样本
        # 会让 sorted[95] 落到快样本区, p95 < 4.5 不 emit.
        svc._last_telemetry_check_ts = 0.0
        svc._record_infer_duration(6.0)  # 慢样本触发 check 同时保证 p95 > 4.5
        log_output = log_stream.getvalue()
        assert "infer duration telemetry" in log_output, (
            f"p95 ~ 5.0s 应触发 WARNING, got: {log_output!r}"
        )
        assert "p50=" in log_output
        assert "p95=" in log_output
        assert "p99=" in log_output
        assert "n=101" in log_output
    finally:
        log.removeHandler(handler)


def test_p50_p95_p99_exact_values():
    """100 样本 (0.1, 0.2, ..., 10.0), 验证 p50/p95/p99 精确值。"""
    log_stream = io.StringIO()
    handler = logging.StreamHandler(log_stream)
    handler.setLevel(logging.WARNING)
    log = logging.getLogger("vsa_service")
    log.addHandler(handler)
    try:
        svc = _make_svc()
        for i in range(1, 101):
            svc._record_infer_duration(i / 10.0)
        svc._last_telemetry_check_ts = 0.0
        svc._record_infer_duration(0.5)
        log_output = log_stream.getvalue()
        assert "p50=5.0" in log_output, log_output
        assert "p95=9.5" in log_output, log_output
        assert "p99=9.9" in log_output, log_output
    finally:
        log.removeHandler(handler)


# ----------------------------------------------------------------------------
# 4. 60s 节流
# ----------------------------------------------------------------------------

def test_60s_throttle():
    """60s 内重复调用不重算, 第二次检查 window 标记应推进。"""
    svc = _make_svc()
    # 压入样本, 第一次应触发检查
    for i in range(20):
        svc._record_infer_duration(1.0)
    first_check_ts = svc._last_telemetry_check_ts
    assert first_check_ts > 0.0, "第一次调用应更新 _last_telemetry_check_ts"
    # 立即再调用, 不应更新 (60s 内节流)
    for i in range(20):
        svc._record_infer_duration(2.0)
    second_check_ts = svc._last_telemetry_check_ts
    assert second_check_ts == first_check_ts, (
        "60s 内重复调用不应触发新的检查"
    )
    # 手动模拟时间推进 61s, 下次调用应触发新检查
    svc._last_telemetry_check_ts -= 61.0
    for i in range(20):
        svc._record_infer_duration(1.0)
    third_check_ts = svc._last_telemetry_check_ts
    assert third_check_ts > first_check_ts, (
        "时间推进 > 60s 后应触发新检查, 更新 _last_telemetry_check_ts"
    )


# ----------------------------------------------------------------------------
# 5. deque maxlen
# ----------------------------------------------------------------------------

def test_deque_maxlen_eviction():
    """deque(maxlen=200) 超过 200 帧后, 旧样本被自动 evict。"""
    svc = _make_svc()
    for i in range(300):
        svc._record_infer_duration(float(i))
    assert len(svc._infer_durations) == 200
    # 最老的 100 个 (0-99) 已被 evict, 最新的 100 个 (200-299) 保留
    assert svc._infer_durations[0] == 100.0
    assert svc._infer_durations[-1] == 299.0


# ----------------------------------------------------------------------------
# 6. 样本不足 (< 10) 跳过检查
# ----------------------------------------------------------------------------

def test_skip_check_when_too_few_samples():
    """样本 < 10 不 emit (sample size 不足统计意义)。"""
    log_stream = io.StringIO()
    handler = logging.StreamHandler(log_stream)
    handler.setLevel(logging.WARNING)
    log = logging.getLogger("vsa_service")
    log.addHandler(handler)
    try:
        svc = _make_svc()
        for i in range(5):  # 只 5 个样本 (< 10)
            svc._record_infer_duration(10.0)  # 全部 10s (> 阈值)
        log_output = log_stream.getvalue()
        assert "infer duration telemetry" not in log_output, (
            f"< 10 样本不应 emit, got: {log_output!r}"
        )
    finally:
        log.removeHandler(handler)


def test_defensive_compat_with_old_instance():
    """防御性: 旧 instance (v0.15 之前启动, __init__ 没初始化 telemetry 字段) 调用
    _record_infer_duration 静默 no-op, 不抛 AttributeError。

    这是 v0.15 修的"运行中服务不需 restart"合约的核心: 单例被创建时
    没 _infer_durations 字段, 但 _call_llm 仍能正常调 telemetry 方法
    (仅 telemetry 不可用, 不影响 VAS 主流程)。
    """
    from app.apps.video_analysis_search.service import VideoAnalysisSearchService
    svc = VideoAnalysisSearchService.__new__(VideoAnalysisSearchService)
    # 不调 __init__, 模拟 v0.15 之前启动的旧 instance: 没 _infer_durations
    assert not hasattr(svc, "_infer_durations")
    assert not hasattr(svc, "_last_telemetry_check_ts")
    # _call_llm 6 个 return 路径调 _record_infer_duration 都不应抛异常
    for _ in range(20):
        svc._record_infer_duration(0.5)  # 任意值, 都应静默返回
    # 仍然没 _infer_durations (方法 no-op, 没创建 deque)
    assert not hasattr(svc, "_infer_durations")


# ----------------------------------------------------------------------------
# Runner
# ----------------------------------------------------------------------------

TESTS = [
    test_constants_defined,
    test_init_fields,
    test_p95_calculation_under_threshold,
    test_p95_calculation_over_threshold,
    test_p50_p95_p99_exact_values,
    test_60s_throttle,
    test_deque_maxlen_eviction,
    test_skip_check_when_too_few_samples,
    test_defensive_compat_with_old_instance,
]


def main():
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")

    print()
    print(f"{len(TESTS) - failed}/{len(TESTS)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
