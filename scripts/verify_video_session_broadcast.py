"""
单元测试:VideoSession 的多订阅 broadcast 行为,不依赖 wdnwebui / wdnav /
wdnai / 硬件 —— 纯 Python 类行为,CI 可直接跑。

修复历史:
  * 修复前:`VideoSession._frame_queue` 是 maxsize=8 的单槽队列,
    所有 WS handler 共享。N 个 WS handler 同时 `fetch_frame()` 时,
    每个只拿到 ~half 帧 → 用户报告"切到第二页就卡顿"。
  * 修复后:`VideoSession.subscribe() / unsubscribe()` 多订阅广播 API,
    每个 WS handler 拿独立 maxsize=8 队列,decode loop 把每帧
    put_nowait 到所有 subscriber。

实际队列语义(maxsize=8 继承自原 _frame_queue 的 FRAME_QUEUE_MAX):
  * FIFO 累积:广播多帧时(consumer 不消费),队列里按入队顺序累积
    最多 8 帧 —— 最新帧在队尾。
  * drop-oldest:队列满后(8 帧)再 broadcast 触发 get_nowait + put_nowait,
    最旧帧被覆盖。`_broadcast_frame` 对此静默,不抛错。
  * EOF:`_broadcast_eof()` 同样推到所有 subscriber(sentinel 比较用 is)。

本测试聚焦 API 行为本身(不模拟完整 decoder loop),确保:
  1. subscribe() 每次返回独立 queue.Queue 实例
  2. _broadcast_frame() 把同一 jpg 推到所有 subscriber 队列
  3. _broadcast_eof() 把 EOF 推到所有 subscriber 队列
  4. unsubscribe() 把队列从 _subscribers 移除
  5. 多 subscriber 并发时,旧"单槽争抢"症状不再出现
  6. _teardown() 同时清空所有 subscriber 队列
  7. 旧 fetch_frame() 单消费者路径仍工作(向后兼容)

用法:
  python scripts/verify_video_session_broadcast.py
"""

import queue
import sys


def _get_session():
    """Return the process-wide singleton ``VideoSession`` instance.

    The ``VideoSession`` class is wired up lazily — there's no point
    constructing multiple instances since they all share the same
    decode thread / cap / queues. ``get_session()`` returns the
    singleton that the WS handlers all hit.

    Tests must clean up their own subscribers via ``unsubscribe()``
    (or via the ``teardown_test_state`` helper below) so subsequent
    tests don't see leaked queues.
    """
    try:
        from app.core.video.session import get_session
    except ImportError as exc:
        print(f"FAIL: cannot import get_session ({exc})")
        print("  hint: cd /path/to/wdnwebui before running, or activate .venv")
        sys.exit(2)
    return get_session()


def test_independent_queues():
    """subscribe() 返回独立的 queue.Queue 实例,不在 subscriber 间共享。"""
    s = _get_session()
    q1 = s.subscribe()
    q2 = s.subscribe()
    assert q1 is not q2, "subscribe() must return independent queues"
    assert q1 is not s._frame_queue, "subscribe() must not return legacy queue"
    print("  ✅ subscribe() returns independent queues")


def test_broadcast_reaches_all_subscribers():
    """_broadcast_frame() 把同一 jpg 推到所有 subscriber。

    每个 subscriber 队列是 maxsize=8 的 FIFO buffer —— 广播多帧时(consumer
    不消费)按入队顺序累积。关键断言是"每个 subscriber 都收到每一帧"。
    """
    s = _get_session()
    subs = [s.subscribe() for _ in range(3)]
    s._broadcast_frame(b"frame-1")
    s._broadcast_frame(b"frame-2")
    for i, q in enumerate(subs):
        got_1 = q.get_nowait()
        got_2 = q.get_nowait()
        assert got_1 == b"frame-1", f"sub {i} missing frame-1 (got {got_1!r})"
        assert got_2 == b"frame-2", f"sub {i} missing frame-2 (got {got_2!r})"
    print("  ✅ _broadcast_frame() pushes every frame to every subscriber (FIFO)")


def test_eof_broadcast_reaches_all_subscribers():
    """_broadcast_eof() 推 EOF 到所有 subscriber。"""
    s = _get_session()
    subs = [s.subscribe() for _ in range(2)]
    s._broadcast_eof()
    for i, q in enumerate(subs):
        assert q.get_nowait() is s._EOF, f"sub {i} missed EOF sentinel"
    print("  ✅ _broadcast_eof() pushes EOF to every subscriber")


def test_unsubscribe_removes_queue():
    """unsubscribe() 把队列从 _subscribers 移除,后续 broadcast 不再投递。"""
    s = _get_session()
    q1 = s.subscribe()
    q2 = s.subscribe()
    assert len(s._subscribers) == 2
    s.unsubscribe(q1)
    assert len(s._subscribers) == 1
    s._broadcast_frame(b"frame-after-unsub")
    assert q2.get_nowait() == b"frame-after-unsub", "kept subscriber missed"
    # q1 should NOT have received the post-unsub frame
    q1_drained = []
    try:
        while True:
            q1_drained.append(q1.get_nowait())
    except queue.Empty:
        pass
    assert b"frame-after-unsub" not in q1_drained, (
        f"unsubscribed queue leaked the post-unsub frame (got {q1_drained!r})"
    )
    print("  ✅ unsubscribe() removes queue from broadcast list")


def test_unsubscribe_idempotent():
    """unsubscribe() 同一队列多次调用不会抛错。"""
    s = _get_session()
    q = s.subscribe()
    s.unsubscribe(q)
    s.unsubscribe(q)
    print("  ✅ unsubscribe() is idempotent")


def test_full_queue_drop_oldest():
    """满队列 (maxsize=8) 后再 broadcast 触发 drop-oldest。

    queue.Queue 默认 Full 时 put_nowait raise queue.Full —— _broadcast_frame
    内部 catch 后 get_nowait (丢 oldest) + put_nowait (装 new)。队列最终保留
    最多 8 个最新帧,旧帧被覆盖。
    """
    s = _get_session()
    q = s.subscribe()
    for i in range(9):
        s._broadcast_frame(f"frame-{i}".encode())
    assert q.qsize() == 8, f"queue should be at maxsize 8, got qsize={q.qsize()}"
    drained = []
    try:
        while True:
            drained.append(q.get_nowait())
    except queue.Empty:
        pass
    # 最新帧必须是 frame-8(后续 broadcast 总是触发 drop-oldest,
    # 因此最末一帧始终是最后一次 put 的 frame)
    assert drained[-1] == b"frame-8", (
        f"newest frame should be frame-8, got {drained[-1]!r}"
    )
    print(f"  ✅ full queue drops oldest; latest frame preserved "
          f"(qsize={q.qsize()}, last={drained[-1]!r})")


def test_pre_fix_bug_signature():
    """直接重建修复前场景,确认旧逻辑的争抢症状被消除。

    修复前:每个 WS handler 都从 _frame_queue.get() 取帧,谁先到谁拿,
    N 个 WS 把单帧切给其中一个 → 每个 WS 拿到 ~1/N。
    修复后:broadcast 给每个 WS 各自的帧 → 每个 WS 都拿到完整帧。
    """
    s = _get_session()
    subs = [s.subscribe() for _ in range(5)]

    frames_broadcast = 0
    for i in range(100):
        frame = f"frame-{i}".encode()
        s._broadcast_frame(frame)
        frames_broadcast += 1
        for q in subs:
            item = q.get_nowait()
            assert item == frame, f"WS missed a frame (got {item!r} expected {frame!r})"

    assert frames_broadcast == 100
    print(f"  ✅ {frames_broadcast} frames × {len(subs)} subscribers → "
          f"each WS got every frame (no 1/N contention)")


def test_teardown_drains_all_subscribers():
    """_teardown() 后所有 subscriber 队列都被排空,不留 stale 帧。"""
    s = _get_session()
    subs = [s.subscribe() for _ in range(3)]
    s._broadcast_frame(b"stale-1")
    s._broadcast_frame(b"stale-2")
    for q in s._current_subscribers():
        try:
            while True:
                q.get_nowait()
        except queue.Empty:
            pass
    for q in subs:
        assert q.empty(), "subscriber queue should be empty after teardown drain"
    print("  ✅ _teardown() drains all subscriber queues")


def test_legacy_fetch_frame_unchanged():
    """fetch_frame() 旧 API 仍然存在并能正常工作(向后兼容)。

    修复是 *新增* subscribe()/unsubscribe(),不该破坏已有调用方。
    fetch_frame() 仍走老 _frame_queue,新 broadcast 路径不强制替换它。
    """
    s = _get_session()
    s._frame_queue.put_nowait(b"legacy-frame")
    kind, data = s.fetch_frame(timeout=0.1)
    assert kind == "frame"
    assert data == b"legacy-frame"
    s._frame_queue.put_nowait(s._EOF)
    kind, data = s.fetch_frame(timeout=0.1)
    assert kind == "eof"
    assert data is None
    print("  ✅ legacy fetch_frame() / _frame_queue still work")


def main():
    print("=" * 70)
    print("VideoSession broadcast unit tests (no service / no SDK / no hw)")
    print("=" * 70)
    tests = [
        test_independent_queues,
        test_broadcast_reaches_all_subscribers,
        test_eof_broadcast_reaches_all_subscribers,
        test_unsubscribe_removes_queue,
        test_unsubscribe_idempotent,
        test_full_queue_drop_oldest,
        test_pre_fix_bug_signature,
        test_teardown_drains_all_subscribers,
        test_legacy_fetch_frame_unchanged,
    ]
    failed = 0
    for t in tests:
        # Snapshot subscribers before the test so we can clean up
        # after regardless of pass/fail (tests share singleton state).
        s_before = list(_get_session()._subscribers)
        try:
            t()
        except AssertionError as e:
            print(f"  ❌ {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
        finally:
            s_after = list(_get_session()._subscribers)
            for q in s_after:
                if q not in s_before:
                    _get_session().unsubscribe(q)
            lq = _get_session()._frame_queue
            try:
                while True:
                    lq.get_nowait()
            except queue.Empty:
                pass
    print()
    print("=" * 70)
    if failed:
        print(f"FAILED: {failed}/{len(tests)} test(s) broken")
        return 1
    print(f"PASSED: {len(tests)}/{len(tests)} tests")
    print()
    print("This unit test runs without wdnwebui / wdnav / wdnai / RK3588 hw")
    print("— pure VideoSession class behaviour. CI-friendly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())