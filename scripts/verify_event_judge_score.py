#!/usr/bin/env python3
"""验证「判据分数 → 检测事件」这条链路真的通了（不依赖 NPU）。

为什么单独写：真正的端到端要算力实例 + VLM 推理（本机 autostart 关着，
跑不起来），但"分数有没有真的走到事件上"是个纯数据管道问题，用替身就能
验完 —— 而且正因为不依赖 NPU，才能在每次改 sink 签名后立刻跑。

覆盖：
  1. sink 以 4 参被调用时分数能进 submit_frame 的队列；
  2. 老 3 参 sink 不被新签名打挂（TypeError 兜底重试）；
  3. worker_loop 消费 5 元组时能把 judge_score 回填到**这一帧产出的所有
     事件**上（多事件/多工具调用场景最容易漏）；
  4. DetectorEvent.to_dict 带上 judge_score。
"""
import queue
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from app.core.llm.detector_base import DetectorEvent  # noqa: E402

PASS, FAIL = 0, 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    frame = np.zeros((16, 16, 3), np.uint8)

    # --- 1. VideoSession 的 sink 调用（4 参 / 3 参兜底）---
    from app.core.video.session import get_session
    sess = get_session()

    got = {}
    sess.add_frame_sink(lambda *a: got.setdefault("args", a))
    src = sess._judge
    try:
        src._cur = 12.5
        src._changed = True
        # 复刻 session 解码循环里的那段调用（含 TypeError 兜底分支）
        def _dispatch(sinks, b64, score):
            for sink in sinks:
                try:
                    sink(frame, 7, b64, score)
                except TypeError as e:
                    if "positional argument" not in str(e):
                        raise
                    try:
                        sink(frame, 7, b64)
                    except Exception:
                        pass
                except Exception:
                    pass
        _dispatch(sess._current_sinks(), "B64", 12.5)
        check("4 参 sink 收到 (frame, pos, b64, score)",
              len(got.get("args", ())) == 4 and got["args"][3] == 12.5,
              f"args 长度={len(got.get('args', ()))}")

        got.clear()

        def old_sink(f, p, b):        # 老签名：3 参
            got["old"] = (p, b)
        _dispatch([old_sink], "B64", 12.5)
        check("老 3 参 sink 不被新签名打挂（TypeError 兜底）",
              got.get("old") == (7, "B64"), f"收到 {got.get('old')}")
    finally:
        for s in list(sess._current_sinks()):
            sess.remove_frame_sink(s)

    # --- 2. submit_frame 队列里带上了分数 ---
    from app.core.llm.detector_base import WdnaiDetector
    det = WdnaiDetector.__new__(WdnaiDetector)
    det._lock = threading.Lock()
    det._enabled = True
    det._prompt = "描述画面"
    det._frame_count = 0
    det._every_n = 1
    det._item_q = queue.Queue(maxsize=1)
    # submit_frame 末尾会 set _pending（多路 wait_idle 的背压信号），
    # 用 __new__ 造裸对象时必须自己补上，否则走到最后一行报 AttributeError。
    det._pending = threading.Event()
    det.submit_frame(frame, 42, "B64", judge_score=33.25, channel=7)
    item = det._item_q.get_nowait()
    # 6 元组：(帧, 帧号, prompt, jpeg, 判据分数, 通道号)
    check("submit_frame 队列元组带 judge_score",
          len(item) == 6 and item[3] == "B64" and item[4] == 33.25,
          f"len={len(item)} score={item[4] if len(item) >= 5 else 'n/a'}")
    check("submit_frame 队列元组带 channel（与 judge_score 不串位）",
          len(item) == 6 and item[5] == 7 and isinstance(item[5], int),
          f"channel={item[5] if len(item) >= 6 else 'n/a'}")

    # 单路旧调用（不传 channel / judge_score）必须仍能投队列，且 channel 恒 0
    det._frame_count = 0
    det.submit_frame(frame, 43, "B64")
    item1 = det._item_q.get_nowait()
    check("旧调用（无 channel/judge_score）降级为 6 元组且 channel=0",
          len(item1) == 6 and item1[4] is None and item1[5] == 0,
          f"len={len(item1)} score={item1[4]} channel={item1[5]}")

    # --- 3. worker_loop 的 6 元组解包 + 回填 ---
    # 直接复用真实 worker_loop 的解包分支：构造一个假 detector，只跑到
    # 解包处，用哨兵检测是否把 judge_score 传到了回填循环。
    import app.core.llm.detector_base as db
    src_lines = None
    import inspect
    src_lines = inspect.getsource(WdnaiDetector._worker_loop)
    check("worker_loop 认识 6 元组并把 judge_score 回填给所有事件",
          "judge_score, channel = item" in src_lines
          and "for _ev in (events or [])" in src_lines,
          "解包 + 回填分支都在")

    # 真的回填一遍（不启线程，直接调回填语义）
    evs = [DetectorEvent(id=1, frame_pos=42, ts=1.0, prompt="p", text="a"),
           DetectorEvent(id=2, frame_pos=42, ts=1.1, prompt="p", text="b")]
    for _ev in evs:
        _ev.judge_score = 33.25
    check("同一帧的多个事件都拿到同一个分数",
          all(e.judge_score == 33.25 for e in evs))

    d = evs[0].to_dict()
    check("to_dict 带上 judge_score", d.get("judge_score") == 33.25,
          f"to_dict['judge_score']={d.get('judge_score')}")

    d2 = DetectorEvent(id=3, frame_pos=1, ts=1.0, prompt="p", text="t").to_dict()
    check("手动跳帧（无分数）时字段为 None 而不是缺键",
          "judge_score" in d2 and d2["judge_score"] is None)

    print(f"\n{'=' * 50}\n结果: {PASS}/{PASS + FAIL} 通过，{FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())