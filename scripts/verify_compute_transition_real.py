#!/usr/bin/env python3
"""真机验证：模型启停的四种过渡态组合。

不改代码，只跑 API，验四种组合的行为（见 AGENTS.md「模型启停的过渡态」）：

  S1 开启一个**正在启动中**的模型 → 排队等它加载完 → 200 + 最终 running
  S2 开启一个**正在关闭中**的模型 → 排队等它关完 → 200 + 重新拉起
  S3 关闭一个**正在启动中**的模型 → 排队等它加载完 → 200 + 真的停掉
     （腾卡必需：拒绝的话卡上那份继续占显存 → 新模型 rc=-6 起不来）
  S4 关闭一个**正在关闭中**的模型 → 幂等 200（不是 404）

S3 会等模型加载完（真机 30~90s）才停 —— 这一段本来就慢，别当成卡死。

⚠️ 会真的启停模型，占一张 NPU 卡（默认 qwen3-vl-4b，约 3.5GB），**脚本
   结尾会把目标模型停掉**。板子上还有别人在用的时候挑一张空卡。

**卡号怎么选（v0.29 改）**：

  * 默认**自动挑** —— 读 ``/api/compute`` 的 ``model_cards``（能跑 rknn
    模型的卡位），优先挑一张**没有别的模型**占着的卡。
  * 旧版默认 ``--npu 1``，在**单卡板**上就是 npu1 —— 那张卡根本不存在，
    worker 侧 ``LLMSession(model, npu=1)`` 抛 IndexError 秒退，留下一个
    **起不来也删不掉**的僵尸实例，还把后续所有启停都堵在 120s 排队上。
    现在显式指定的卡号**不在本机卡位内就直接退出**，不再造僵尸。

用法：
    .venv/bin/python scripts/verify_compute_transition_real.py [--npu N] [--model qwen3-vl-4b]
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


def snapshot() -> dict:
    st, d = call("GET", "/compute")
    if st != 200:
        raise SystemExit(f"读 /api/compute 失败: HTTP {st} {d}")
    return d


def pick_npu(model: str, requested: int | None) -> int:
    """决定这次跑在哪个卡上。卡号不在本机卡位内 → **直接退出**。

    旧版默认 ``--npu 1``：在单卡板上那是一张**不存在的卡** —— worker
    侧 ``LLMSession(model, npu=1)`` 抛 IndexError 秒退，registry 里留下
    一个起不来也删不掉的僵尸实例，还把后续所有启停都堵在 120s 排队上
    （真机踩过）。现在默认自动挑卡，显式指定越界卡号则拒绝开跑。

    自动挑的规则：优先挑**没有别的模型占着**的卡位（一张卡只能放一个
    模型，同卡叠模型 rc=-6）。
    """
    snap = snapshot()
    model_cards = snap.get("model_cards") or []
    if not model_cards:
        failed = snap.get("cards_failed") or ""
        raise SystemExit(
            "本机没有可用算力卡位（model_cards=[]）"
            + (f"；探测失败原因：{failed}" if failed else "")
            + " —— 跑不了这个验证（先看 /api/compute 的 cards_failed）")
    if requested is not None:
        if requested not in model_cards:
            avail = "/".join(str(c) for c in model_cards)
            raise SystemExit(
                f"npu{requested} 不在本机算力卡位 {model_cards} 内 —— "
                f"别在单卡板上指定不存在的卡（会造出永远起不来的僵尸实例）。"
                f"可用卡位：--npu {avail}")
        return requested
    # 谁占着哪张卡（只看本机实例；peer 的算力不是本机的卡）
    busy: dict[int, list[str]] = {}
    for inst in snap.get("instances", []):
        if inst.get("_source") == "adopted":
            continue
        if inst.get("status") not in ("running", "loading"):
            continue
        n = inst.get("npu_index")
        if isinstance(n, int) and n >= 0:
            busy.setdefault(n, []).append(inst.get("model_id") or "?")
    for n in model_cards:
        if not [m for m in busy.get(n, []) if m != model]:
            return n
    n = model_cards[0]
    print(f"⚠️ 所有算力卡位都被别的模型占着（{busy}），只能试 npu{n} —— "
          f"若该卡上有别的模型，本脚本会因显存不足失败（rc=-6）。",
          flush=True)
    return n


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
    ap.add_argument("--npu", type=int, default=None,
                    help="跑在哪个卡（默认自动挑一张没被别的模型占着的卡位）")
    ap.add_argument("--model", default="qwen3-vl-4b")
    args = ap.parse_args()
    model = args.model
    npu = pick_npu(model, args.npu)

    print(f"=== 目标: {model} @ npu{npu} ===", flush=True)

    # 清理：这张卡上不要有残留
    if local_inst(model, npu):
        print(f"⚠️ {model} 已经跑在 npu{npu} 上 —— 本脚本会把它停掉重起，"
              f"结尾还会停掉它。", flush=True)
        call("DELETE", "/compute/instances", {"model": model, "npu_index": npu})

    # ---------------------------------------------------------------- #
    print("\n=== S1：模型正在启动中，再启用一次 ===", flush=True)
    st, d = call("POST", "/compute/instances", {"model": model, "npu_index": [npu]})
    check("S1 启动请求受理", st == 200, f"http={st}")
    # 等它进入 loading（worker 已 spawn、正在加载）
    inst, _ = wait_for(model, npu,
                       lambda i: i and i.get("status") == "loading",
                       30, "进入 loading")
    check("S1 抓到 loading 窗口", True,
          f"transition={inst.get('transition')}")

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
    print("\n=== S3：模型正在启动中，禁用 → 排队等它加载完 → 停掉 ===", flush=True)
    st, d = call("POST", "/compute/instances", {"model": model, "npu_index": [npu]})
    check("S3 启动请求受理", st == 200, f"http={st}")
    inst, _ = wait_for(model, npu,
                       lambda i: i and i.get("status") == "loading",
                       30, "进入 loading")
    check("S3 抓到 loading 窗口", True,
          f"transition={inst.get('transition')}")
    t0 = time.time()
    st, d = call("DELETE", "/compute/instances",
                 {"model": model, "npu_index": npu})
    dt = time.time() - t0
    check("S3 loading 中禁用 → 200（排队后停掉）", st == 200,
          f"http={st} {d.get('detail')}")
    check("S3 有排队（不是立刻返回）", dt > 0.5, f"{dt:.1f}s")
    inst, _ = wait_for(model, npu, lambda i: i is None, 60, "等它彻底停下")
    check("S3 实例已从 registry 移除（卡腾空了）", inst is None, str(inst))

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
