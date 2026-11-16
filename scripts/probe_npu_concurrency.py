"""定位并发瓶颈：同卡两个 HY 实例到底能不能真并行？

    .venv/bin/python scripts/probe_npu_concurrency.py [--card 0]

三组计时（每组只发 **1 个**请求，量「两个请求同时打」的墙钟）：

  1. 单发 A            —— 基准：一个请求多少毫秒
  2. 同时发 A + A      —— 同一个 session 两个请求（executor max_workers=1
                           ⇒ 必然串行，预期 ≈ 2×基准）
  3. 同时发 A + B      —— **两个 worker、同一张卡**，这才是关键那组：
                           若 ≈ 1×基准 ⇒ 卡上真并行；
                           若 ≈ 2×基准 ⇒ 卡/驱动把两个 context 串行化了，
                           双实例白搭。
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

sys.path.insert(0, str(REPO / "scripts"))
from bench_npu_dual import (  # noqa: E402
    MODEL_ID, build_session, card_device_id, ensure_env, run_one, spawn_worker,
    wait_ready,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", type=int, default=0)
    args = ap.parse_args()

    ensure_env()

    from wdndds.dds._rpc import Hub as RpcHub

    hub = RpcHub(participant_name="wdnwebui-probe-%d" % __import__("os").getpid(),
                 domain_id=0, discovery_servers=[])
    hub.start()
    side = RpcHub(participant_name="wdnwebui-probe-side-%d" % __import__("os").getpid(),
                  domain_id=0, discovery_servers=[])
    side.start()

    procs = []
    sessions = []
    try:
        for tag in ("w1", "w2"):
            print("起 worker %s（卡 %d）…" % (tag, args.card), flush=True)
            p, svc, log = spawn_worker(args.card, tag)
            procs.append(p)
            if not wait_ready(p, log):
                print("worker %s 没就绪 —— %s" % (tag, log))
                return 2
            sessions.append(build_session(svc, hub, side))
            print("worker %s 就绪 (pid=%d)" % (tag, p.pid), flush=True)

        text = "网页翻译服务已经启用，正在使用本板 NPU 翻译。"

        # 预热（第一次 RPC 要等 DDS 握手）
        for s in sessions:
            run_one(s, text)

        def timed(fn):
            t0 = time.perf_counter()
            fn()
            return time.perf_counter() - t0

        # 1. 单发
        base = []
        for _ in range(3):
            base.append(timed(lambda s=sessions[0]: run_one(s, text)))
        base_med = sorted(base)[len(base) // 2]
        print("\n1. 单发 A           : %.0f ms" % (base_med * 1000))

        # 2. 同时发 A + A
        same = []
        for _ in range(3):
            def two_same():
                ths = [threading.Thread(target=run_one,
                                        args=(sessions[0], text)) for _ in range(2)]
                for t in ths:
                    t.start()
                for t in ths:
                    t.join()
            same.append(timed(two_same))
        same_med = sorted(same)[len(same) // 2]
        print("2. 同时发 A + A     : %.0f ms  (%.2f× 基准)"
              % (same_med * 1000, same_med / base_med))

        # 3. 同时发 A + B（两个 worker，同一张卡）
        cross = []
        for _ in range(3):
            def two_cross():
                ths = [threading.Thread(target=run_one, args=(s, text))
                       for s in sessions]
                for t in ths:
                    t.start()
                for t in ths:
                    t.join()
            cross.append(timed(two_cross))
        cross_med = sorted(cross)[len(cross) // 2]
        print("3. 同时发 A + B     : %.0f ms  (%.2f× 基准)  ← 同卡双实例"
              % (cross_med * 1000, cross_med / base_med))

        print("\n结论：")
        if cross_med < base_med * 1.4:
            print("  卡上**能并行** —— 双实例有效（3 组时间接近 1×基准）。")
        else:
            print("  同卡两个实例**没有真正并行** —— 瓶颈在卡/驱动侧，"
                  "双实例拿不到加速。")
        return 0
    finally:
        for p in procs:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass
        try:
            hub.stop()
            side.stop()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
