#!/usr/bin/env python3
"""验证「每模型推理预算」（manifest 的 ``infer_timeout_s``）这条链路。

不碰 NPU —— 只验证三层读到的是**同一个数**：

  1. manifest 解析      ``WorkerManifest.infer_timeout_s``
  2. worker 侧的预算    ``_base`` 里 ``_serve_one`` 收到的 ``infer_timeout_s``
  3. 客户端 RPC 预算    ``proxy._infer_timeout_ms_for``（必须 **>** worker 的）

为什么要有这个字段：一次文生图（28 步）在本板要 ~90 s，而全局默认的
60 s 会把它砍掉，报 ``inference exceeded 60.0s``；把全局默认调大又会
让一个卡住的对话 worker 长时间占着 pump。

用法::

    .venv/bin/python scripts/test_infer_timeout.py [model_id]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAILED = []


def check(label, got, want=None, *, ok=None):
    if ok is None:
        ok = (got == want)
    mark = "✅" if ok else "❌"
    print(f"{mark} {label}: {got!r}" + ("" if ok else f" (期望 {want!r})"))
    if not ok:
        FAILED.append(label)


def main():
    model_id = sys.argv[1] if len(sys.argv) > 1 else "qwenimage21"

    from app.core.compute.workers import (
        DEFAULT_INFER_TIMEOUT_S,
        discover,
        manifest_infer_timeout_s,
    )
    from app.core.compute.workers._base import _INFER_TIMEOUT_SEC
    from app.core.compute.proxy import (
        _INFER_TIMEOUT_MARGIN_S,
        _INFER_TIMEOUT_MS,
        _infer_timeout_ms_for,
    )

    # ---- 1. manifest 解析 ------------------------------------------------ #
    worker_s = manifest_infer_timeout_s(model_id)
    check(f"manifest_infer_timeout_s({model_id!r})", worker_s,
          ok=worker_s > 0)
    if worker_s <= 0:
        print("⚠️  这个模型没声明 infer_timeout_s ⇒ 用全局默认 "
              f"{DEFAULT_INFER_TIMEOUT_S}s")
        worker_s = DEFAULT_INFER_TIMEOUT_S

    for m in discover():
        if m.id != model_id:
            continue
        check(f"WorkerManifest({model_id}).infer_timeout_s",
              m.infer_timeout_s, ok=abs(m.infer_timeout_s - worker_s) < 1e-6)
        check(f"WorkerManifest({model_id}).status", m.status, "ok")
        check(f"WorkerManifest({model_id}).errors", m.errors, [])

    # ---- 2. worker 侧 ---------------------------------------------------- #
    check("worker 默认预算（未声明时）", _INFER_TIMEOUT_SEC,
          DEFAULT_INFER_TIMEOUT_S)

    # ---- 3. 客户端预算必须大于 worker 的 --------------------------------- #
    client_ms = _infer_timeout_ms_for(model_id)
    worker_ms = int(round(worker_s * 1000))
    check("客户端预算 > worker 预算", client_ms > worker_ms, ok=client_ms > worker_ms)
    check("客户端预算 == worker + margin",
          client_ms, worker_ms + int(_INFER_TIMEOUT_MARGIN_S * 1000))

    # 没声明的模型保持全局默认
    other = _infer_timeout_ms_for("__no_such_model__")
    check("未声明模型沿用全局默认", other, _INFER_TIMEOUT_MS)

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败: {', '.join(FAILED)}")
        return 1
    print(f"✅ 全部通过（worker {worker_s:.0f}s / client {client_ms} ms）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
