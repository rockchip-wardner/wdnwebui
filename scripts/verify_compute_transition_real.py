#!/usr/bin/env python3
"""真机验证：模型启停的四种过渡态组合。

不改代码，只跑 API，验四种组合的行为（见 AGENTS.md「模型启停的过渡态」）：

  S1 开启一个**正在启动中**的模型 → 排队等它加载完 → 200 + 最终 running
  S2 开启一个**正在关闭中**的模型 → 排队等它关完 → 200 + 重新拉起
  S3 关闭一个**正在启动中**的模型 → 409「正在启动中，请等加载完成后再禁用」
  S4 关闭一个**正在关闭中**的模型 → 幂等 200（不是 404）

⚠️ 会真的启停模型，占一张 NPU 卡（默认 qwen3-vl-4b @ npu1，约 3.5GB）。
   板子上还有别人在用的时候挑一张空卡（--npu 参数）。

用法：
    .venv/bin/python scripts/verify_compute_transition_real.py [--npu 1] [--model qwen3-vl-4b]
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000/api"
FAILED: list[str] = []
PASSED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ✓ {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED.append(name)
        print(f"  ✗ {name}" + (f"  ({detail})" if detail else ""))


def call(method: str, path: str, body=None, timeout: float = 300.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"detail": raw[:200]}


def local_inst(model: str, npu: int):
    st, d = call("GET", "/compute")
    for i in d.get("instances", []):
        if (i.get("model_id") == model and i.get("_source") == "spawned"
                and i.get("npu_index") == npu):
            return i
    return None


def wait_for(model: str, npu: int, pred, timeout: float, label: str):
    t0 = time.time()
    while time.time() - t0 < timeout:
        inst = local_inst(model, npu)
        if pred(inst):
            return inst, time.time() - t0
        time.sleep(1.0)
    raise AssertionError(f"{label} 超时 {timeout}s（当前 {local_inst(model, npu)}）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--npu", type=int, default=1)
    ap.add_argument("--model", default="qwen3-vl-4b")
    args = ap.parse_args()
    model, npu = args.model, args.npu

    print(f"=== 目标: {model} @ npu{npu} ===", flush=True)

    # 清理：这张卡上不要有残留
    if local_inst(model, npu):
        call("DELETE", "/compute/instances", {"model": model, "npu_index": npu})

    # ---------------------------------------------------------------- #
    print("\n=== S1/S3：模型正在启动中 ===", flush=True)
    st, d = call("POST", "/compute/instances", {"model": model, "npu_index": [npu]})
    check("S1 启动请求受理", st == 200, f"http={st}")
    # 等它进入 loading（worker 已 spawn、正在加载）
    inst, _ = wait_for(model, npu,
                       lambda i: i and i.get("status") == "loading",
                       30, "进入 loading")
    check("S1 抓到 loading 窗口", True,
          f"transition={inst.get('transition')}")

    # S3：loading 中禁用 → 409
    st, d = call("DELETE", "/compute/instances",
                 {"model": model, "npu_index": npu})
    check("S3 loading 中禁用 → 409", st == 409, f"http={st} {d.get('detail')}")
    check("S3 文案提到启动中", "启动中" in str(d.get("detail")),
          str(d.get("detail")))

    # S1：loading 中再启用 → 排队等它加载完，不重复 bind、不 rc=-6
    t0 = time.time()
    st, d = call("POST", "/compute/instances", {"model": model, "npu_index": [npu]})
    dt = time.time() - t0
    check("S1 loading 中启用 → 200", st == 200, f"http={st} ({dt:.1f}s)")
    check("S1 无错误", not d.get("errors"), str(d.get("errors"))[:120])
    inst, _ = wait_for(model, npu,
                       lambda i: i and i.get("status") == "running",
                       240, "等它加载完")
    check("S1 最终 running（没 rc=-6）", inst.get("status") == "running",
          f"status={inst.get('status')} error={inst.get('error')}")
    check("S1 有排队（不是立刻返回）", dt > 0.5, f"{dt:.1f}s")

    # ---------------------------------------------------------------- #
    print("\n=== S4：模型正在关闭中，再删一次 ===", flush=True)
    # 注意：这里**不能**有并发的 POST —— S2 的 POST 会把模型重新拉起
    # (loading)，那时的 DELETE 撞的是 loading(→409)，不是 stopping。
    # S4 要单独测：只发两个 DELETE。
    results: dict[str, object] = {}

    def do_delete():
        results["del"] = call("DELETE", "/compute/instances",
                              {"model": model, "npu_index": npu})

    th = threading.Thread(target=do_delete)
    th.start()
    time.sleep(0.3)
    st3, d3 = call("DELETE", "/compute/instances",
                   {"model": model, "npu_index": npu})
    th.join()
    st1, d1 = results["del"]
    check("S4 第一个 disable 成功", st1 == 200, f"http={st1}")
    check("S4 重复 disable → 幂等 200（不是 404）", st3 == 200,
          f"http={st3} {str(d3.get('detail'))[:80]}")
    wait_for(model, npu,
             lambda i: i is None or i.get("status") != "running",
             60, "等它彻底停下")

    # ---------------------------------------------------------------- #
    print("\n=== S2：模型正在关闭中，同时启用 ===", flush=True)
    # 先把它拉起来
    call("POST", "/compute/instances", {"model": model, "npu_index": [npu]})
    wait_for(model, npu, lambda i: i and i.get("status") == "running",
             240, "等它重新起来")
    results.clear()

    def do_delete2():
        results["del"] = call("DELETE", "/compute/instances",
                              {"model": model, "npu_index": npu})

    th = threading.Thread(target=do_delete2)
    th.start()
    time.sleep(0.3)
    st2, d2 = call("POST", "/compute/instances", {"model": model, "npu_index": [npu]})
    th.join()
    st1, d1 = results["del"]
    check("S2 第一个 disable 成功", st1 == 200, f"http={st1}")
    check("S2 关闭中启用 → 200（排队后重拉）", st2 == 200, f"http={st2}")
    check("S2 无错误", not d2.get("errors"), str(d2.get("errors"))[:120])

    # 收尾：把模型停掉，别占着别人的卡
    call("DELETE", "/compute/instances", {"model": model, "npu_index": npu})
    wait_for(model, npu,
             lambda i: i is None or i.get("status") != "running",
             120, "收尾清理")

    print("\n" + "=" * 60)
    print(f"通过 {PASSED} / {PASSED + len(FAILED)}")
    if FAILED:
        print(f"❌ 失败: {FAILED}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
