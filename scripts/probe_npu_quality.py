"""NPU 翻译**质量**实测：短文案到底能不能翻。

    .venv/bin/python scripts/probe_npu_quality.py [--suffix none|num]

对每条文案跑一次推理，把原文/译文打出来，并按文种判「有没有真的翻成英文」。

``--suffix num`` 复现基准脚本的缓存绕过手法（追加 ``#0``）；
``--suffix none`` 不加任何后缀 —— 用来判断「短文案翻不出来」到底是
模型的问题，还是基准脚本那个 ``#N`` 后缀惹的祸。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from bench_npu_dual import (  # noqa: E402
    MODEL_ID, SEGMENTS, build_session, ensure_env, run_one, spawn_worker,
    wait_ready,
)
from app.core.translate.npu_provider import _script_set  # noqa: E402


def judge(out: str, src: str) -> str:
    """译文判定：ok=真翻成英文 / echo=复读原文 / other=其它。"""
    out = (out or "").strip()
    if not out:
        return "empty"
    s = _script_set(out)
    has_han = "han" in s
    has_latin = "latin" in s
    if has_han and not has_latin:
        return "echo"          # 还是中文 —— 没翻出来
    if has_han and has_latin:
        return "partial"       # 一半中文一半英文
    return "ok"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suffix", choices=["none", "num"], default="none")
    ap.add_argument("--card", type=int, default=0)
    args = ap.parse_args()

    ensure_env()

    from wdndds.dds._rpc import Hub as RpcHub
    import os

    hub = RpcHub(participant_name="wdnwebui-q-%d" % os.getpid(),
                 domain_id=0, discovery_servers=[])
    hub.start()
    side = RpcHub(participant_name="wdnwebui-q-side-%d" % os.getpid(),
                  domain_id=0, discovery_servers=[])
    side.start()

    procs = []
    try:
        p, svc, log = spawn_worker(args.card, "quality")
        procs.append(p)
        if not wait_ready(p, log):
            print("worker 没就绪 —— %s" % log)
            return 2
        sess = build_session(svc, hub, side)
        run_one(sess, "预热")   # 等 DDS 握手

        stats = {"ok": 0, "echo": 0, "partial": 0, "empty": 0}
        t0 = time.perf_counter()
        for i, s in enumerate(SEGMENTS):
            text = s if args.suffix == "none" else "%s #%d" % (s, i)
            out = run_one(sess, text)
            v = judge(out, text)
            stats[v] = stats.get(v, 0) + 1
            flag = {"ok": "✓", "echo": "✗", "partial": "!", "empty": "·"}.get(v, "?")
            print("%s %-26s → %s" % (flag, text[:24], (out or "（空）")[:56]))
        dt = time.perf_counter() - t0
        total = sum(stats.values())
        print("\n共 %d 条，用时 %.2fs（单条 %.0f ms）" % (total, dt, dt * 1000 / total))
        print("判定: " + "  ".join("%s=%d" % (k, v) for k, v in stats.items() if v))
        print("真翻成英文的比例: %.0f%%" % (100.0 * stats.get("ok", 0) / total))
        return 0
    finally:
        for pr in procs:
            try:
                pr.terminate()
                pr.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    pr.kill()
                except Exception:  # noqa: BLE001
                    pass
        try:
            hub.stop()
            side.stop()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
