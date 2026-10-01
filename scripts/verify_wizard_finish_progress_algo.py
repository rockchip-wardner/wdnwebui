"""
模拟 _waitForModelsRunning 的核心算法,验证各种场景的进度计算。
"""
import json
import time

# 直接复制 state.js 760-860 里的核心算法 (Python 版)
def compute_progress(snapshot, targets, now=None):
    if now is None:
        now = time.time()
    per_model = snapshot.get("estimated_init_seconds_per_model") or {}
    default_est = snapshot.get("default_estimated_init_seconds") or 30
    instances = [i for i in (snapshot.get("instances") or []) if i.get("_source") != "adopted"]
    running_set = {i["model_id"] for i in instances if i.get("status") == "running"}

    if all(m in running_set for m in targets):
        return {"state": "all_running", "lines": []}

    item_lines = []
    done_count = 0
    err_count = 0
    for m in targets:
        est = per_model[m] if m in per_model else default_est
        inst = next((i for i in instances if i.get("model_id") == m), None)
        if inst is None:
            item_lines.append(f"{m}  等待启动…")
            continue
        if inst.get("status") == "running":
            item_lines.append(f"{m}  ✓ 100%")
            done_count += 1
            continue
        if inst.get("status") == "error":
            item_lines.append(f"{m}  ✗ 启动失败: {inst.get('error') or '未知错误'}")
            err_count += 1
            continue
        started_at = inst.get("started_at") or now
        elapsed = max(0.0, now - started_at)
        pct = min(99, max(0, round((elapsed / est) * 100)))
        remaining = max(0, round(est - elapsed))
        item_lines.append(f"{m}  {pct}% (~{remaining}s 剩余)")

    if len(targets) == 1:
        return {"state": "loading", "header": [], "lines": item_lines}
    return {
        "state": "loading",
        "header": [f"模型加载进度 ({done_count}/{len(targets)})"],
        "lines": item_lines,
    }


# === 测试场景 ===
NOW = 1000.0  # 假设现在是 1000s

# 场景 1: 单模型 loading 中,started_at=5 秒前,estimated=50s → 10%
snap = {
    "estimated_init_seconds_per_model": {"qwen3-vl-4b": 60},
    "default_estimated_init_seconds": 10,
    "instances": [{
        "model_id": "qwen3-vl-4b", "status": "loading",
        "started_at": NOW - 5, "_source": "spawned",
    }],
}
result = compute_progress(snap, ["qwen3-vl-4b"], NOW)
print("场景 1 (loading 5s/60s):", json.dumps(result, ensure_ascii=False))
assert result["state"] == "loading"
assert "8% " in result["lines"][0] or "10% " in result["lines"][0] or "9% " in result["lines"][0], f"期望 ~8-10%, 实际: {result['lines'][0]}"

# 场景 2: 多模型,一个 loading 一个 running
snap = {
    "estimated_init_seconds_per_model": {"qwen3-vl-4b": 60, "qwen2.5-omni-3b": 50},
    "default_estimated_init_seconds": 10,
    "instances": [
        {"model_id": "qwen3-vl-4b", "status": "loading", "started_at": NOW - 30, "_source": "spawned"},
        {"model_id": "qwen2.5-omni-3b", "status": "running", "started_at": NOW - 60, "_source": "spawned"},
    ],
}
result = compute_progress(snap, ["qwen3-vl-4b", "qwen2.5-omni-3b"], NOW)
print("场景 2 (混合):", json.dumps(result, ensure_ascii=False))
assert result["state"] == "loading"
assert result["header"][0] == "模型加载进度 (1/2)"
assert "qwen3-vl-4b  50%" in result["lines"][0], f"30/60=50%; got {result['lines'][0]}"
assert "qwen2.5-omni-3b  ✓ 100%" in result["lines"][1]

# 场景 3: 全部 running → clearBox
snap = {
    "estimated_init_seconds_per_model": {"qwen3-vl-4b": 60},
    "default_estimated_init_seconds": 10,
    "instances": [
        {"model_id": "qwen3-vl-4b", "status": "running", "started_at": NOW - 50, "_source": "spawned"},
    ],
}
result = compute_progress(snap, ["qwen3-vl-4b"], NOW)
print("场景 3 (全部 running):", json.dumps(result, ensure_ascii=False))
assert result["state"] == "all_running"

# 场景 4: peer adopted 不算本机 (应继续等待)
snap = {
    "estimated_init_seconds_per_model": {"qwen3-vl-4b": 60},
    "default_estimated_init_seconds": 10,
    "instances": [
        # peer adopted 被过滤,不在 instances 中
        {"model_id": "qwen3-vl-4b", "status": "running", "started_at": NOW - 50, "_source": "adopted"},
    ],
}
result = compute_progress(snap, ["qwen3-vl-4b"], NOW)
print("场景 4 (peer adopted 过滤):", json.dumps(result, ensure_ascii=False))
assert result["state"] == "loading", "peer adopted 不算本机 running"

# 场景 5: 99% 封顶 (避免撞 100% 后还没报 running 的抖动)
snap = {
    "estimated_init_seconds_per_model": {"qwen3-vl-4b": 60},
    "default_estimated_init_seconds": 10,
    "instances": [
        {"model_id": "qwen3-vl-4b", "status": "loading", "started_at": NOW - 60, "_source": "spawned"},
    ],
}
result = compute_progress(snap, ["qwen3-vl-4b"], NOW)
print("场景 5 (超时封顶 99%):", json.dumps(result, ensure_ascii=False))
assert "99%" in result["lines"][0], f"60/60=100% 应被封顶到 99%; got {result['lines'][0]}"

# 场景 6: 模型启动失败
snap = {
    "estimated_init_seconds_per_model": {"qwen3-vl-4b": 60},
    "default_estimated_init_seconds": 10,
    "instances": [
        {"model_id": "qwen3-vl-4b", "status": "error", "started_at": NOW - 5,
         "_source": "spawned", "error": "rknn 模型文件不存在"},
    ],
}
result = compute_progress(snap, ["qwen3-vl-4b"], NOW)
print("场景 6 (error):", json.dumps(result, ensure_ascii=False))
assert "✗ 启动失败: rknn 模型文件不存在" in result["lines"][0]

# 场景 7: snapshot 里没有该 model (POST 后 1s 内)
snap = {
    "estimated_init_seconds_per_model": {"qwen3-vl-4b": 60},
    "default_estimated_init_seconds": 10,
    "instances": [],
}
result = compute_progress(snap, ["qwen3-vl-4b"], NOW)
print("场景 7 (实例尚未出现):", json.dumps(result, ensure_ascii=False))
assert "等待启动…" in result["lines"][0]

# 场景 8: 缺 estimated 用 default 兜底
snap = {
    "estimated_init_seconds_per_model": {},
    "default_estimated_init_seconds": 25,
    "instances": [
        {"model_id": "unknown-model", "status": "loading", "started_at": NOW - 10, "_source": "spawned"},
    ],
}
result = compute_progress(snap, ["unknown-model"], NOW)
print("场景 8 (default 兜底 25s):", json.dumps(result, ensure_ascii=False))
assert "40% " in result["lines"][0] or "41% " in result["lines"][0], f"10/25=40%; got {result['lines'][0]}"

print("\n✅ 所有场景通过")
