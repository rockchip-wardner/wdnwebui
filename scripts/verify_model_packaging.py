#!/usr/bin/env python3
"""回归测试：worker 配置随模型文件夹分发。

worker manifest 从代码仓搬到**模型文件夹里**，于是：

* 一个模型文件夹 = 权重 + 描述它的 manifest = **一个分发单元**
  （HuggingFace 上一个 repo 就是完整安装包，``git clone`` 即装好）；
* 「本板装了哪些模型」是**板子状态**：模型文件夹不在 → 该 worker 在算力
  配置页的「本地算力」里不出现；
* clone 完模型不用重启服务（后台每 ~2s 重扫 + 手动刷新端点）。

覆盖
----
 1. 发现：manifest 在模型目录里 → 能发现；模型目录不存在 → 不出现。
 2. 模型目录里的**其它 json** 不会被误当成 manifest。
 3. ``worker.json`` 备选命名可用（id 从 manifest body 取）。
 4. SDK 跟随**实际**模型目录（目录名可任意，不受 SDK 硬编码名约束）。
 5. spec 的所有路径都落在模型目录内（env 覆盖生效的硬校验）。
 6. 卡片能力词表与「本板装了哪些模型」**无关** —— 少装一个模型不会让
    别的卡片被判 FATAL。
 7. autostart 里指向未安装模型的条目 → 跳过该条，**其余照常**。
 8. ``refresh_model_presets()`` 能把目录变化反映到 MODEL_PRESETS。
 9. 静态审计：仓库里不再有 ``workers/*.json``；worker 入口脚本仍在原位
    （``/proc/<pid>/cmdline`` 的 needle 依赖它的路径）。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_FAILED: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}")
    if not ok:
        if detail:
            print(f"       {detail}")
        _FAILED.append(label)
    return ok


def _make_model_dir(root: Path, name: str, manifest: dict,
                    manifest_name: str | None = None) -> Path:
    """Create a model folder holding one worker manifest."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    mid = str(manifest.get("id") or "probe")
    fname = manifest_name or f"{mid}.json"
    (d / fname).write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    return d


_MIN_MANIFEST = {
    "label": "探针模型",
    "capability_types": ["swcap_text_chat"],
    "spec_factory": "qwen3_vl_4b",
    "worker_name": "probe",
}

print("=== 1. 发现：manifest 在模型目录里 ===")
_tmp = Path(tempfile.mkdtemp(prefix="wdn-modelpkg-"))
_prev_env = os.environ.get("WDNAI_MODELS_DIR")
try:
    os.environ["WDNAI_MODELS_DIR"] = str(_tmp)

    from app.core.compute.workers import (          # noqa: E402
        discover, find_manifest_path, model_presets, models_root,
    )

    _make_model_dir(_tmp, "Model-A", dict(_MIN_MANIFEST, id="probe-a"))
    _make_model_dir(_tmp, "Model-B", dict(_MIN_MANIFEST, id="probe-b"))

    ids = {m.id for m in discover()}
    check(ids == {"probe-a", "probe-b"}, "两个模型目录各发现一个 worker",
          str(sorted(ids)))
    check(models_root() == _tmp, "models_root 跟随 WDNAI_MODELS_DIR",
          str(models_root()))
    check(find_manifest_path("probe-a") is not None,
          "find_manifest_path 能定位 manifest")

    # 模型目录不在 → 它的 worker 不出现
    shutil.rmtree(_tmp / "Model-B")
    ids = {m.id for m in discover()}
    check(ids == {"probe-a"}, "删掉模型目录 → 该 worker 不再出现",
          str(sorted(ids)))
    check("probe-b" not in model_presets(), "MODEL_PRESETS 同步少一项",
          str(sorted(model_presets())))
    check(find_manifest_path("probe-b") is None,
          "find_manifest_path 对未安装的模型返回 None")

    print("\n=== 2. 模型目录里的其它 json 不会被误当成 manifest ===")
    (_tmp / "Model-A" / "tokenizer_config.json").write_text(
        json.dumps({"vocab_size": 151936, "model_type": "qwen3"}),
        encoding="utf-8")
    (_tmp / "Model-A" / "generation_config.json").write_text(
        json.dumps({"temperature": 0.7}), encoding="utf-8")
    ids = {m.id for m in discover()}
    check(ids == {"probe-a"}, "无关 json 不产生额外 worker 条目",
          str(sorted(ids)))

    print("\n=== 3. worker.json 备选命名 ===")
    _tmp2 = Path(tempfile.mkdtemp(prefix="wdn-modelpkg2-"))
    os.environ["WDNAI_MODELS_DIR"] = str(_tmp2)
    _make_model_dir(_tmp2, "Canonical", dict(_MIN_MANIFEST, id="probe-c"),
                    manifest_name="worker.json")
    ids = {m.id for m in discover()}
    check(ids == {"probe-c"}, "worker.json 的 id 从 manifest body 取",
          str(sorted(ids)))
    check(find_manifest_path("probe-c") is not None,
          "find_manifest_path 支持 worker.json")
    shutil.rmtree(_tmp2, ignore_errors=True)

    print("\n=== 4/5. SDK 跟随实际模型目录，路径不越界 ===")
    os.environ["WDNAI_MODELS_DIR"] = str(_tmp)
    # 借用真机上已安装的 Qwen3-VL-4B 权重（软链），验证目录名可任意
    _real = Path("/userdata/models/Qwen3-VL-4B")
    if _real.is_dir():
        _weird = _tmp / "hf-repo-name-completely-different"
        _weird.mkdir(parents=True, exist_ok=True)
        for f in _real.iterdir():
            if f.suffix == ".json":
                shutil.copyfile(f, _weird / f.name)
            else:
                os.symlink(f, _weird / f.name)
        from app.core.compute.workers import build_model_spec  # noqa: E402
        try:
            spec = build_model_spec("qwen3-vl-4b")
            inside = all(
                str(getattr(spec, a, "") or "").startswith(str(_weird))
                for a in ("rknn_llm_path", "rknn_vision_path", "tokenizer_path")
                if getattr(spec, a, "")
            )
            check(inside,
                  "SDK 跟随实际目录（目录名不必等于 SDK 硬编码名）",
                  f"rknn_llm_path={getattr(spec, 'rknn_llm_path', '')}")
        except Exception as e:  # noqa: BLE001
            check(False, "SDK 跟随实际目录（目录名不必等于 SDK 硬编码名）",
                  f"{type(e).__name__}: {e}")
    else:
        print("[SKIP] 本机没有 /userdata/models/Qwen3-VL-4B —— "
              "跳过「目录名可任意」的真机验证")

    print("\n=== 6. 卡片能力词表与本板装了哪些模型无关 ===")
    from app.core.manifest import _known_software_capabilities  # noqa: E402
    from wdnai import SoftwareCapability                     # noqa: E402
    caps = _known_software_capabilities()
    check(caps == set(SoftwareCapability.values()),
          "能力词表 = SDK 封闭枚举（不是 worker 清单的并集）",
          f"{sorted(caps)}")
    check("swcap_vision_grounding" in caps,
          "词表含 grounding —— 即使本板没装对应模型也应在（词表 ≠ provider）",
          str(sorted(caps)))

    print("\n=== 7. autostart 里未安装的模型 → 跳过该条，其余照常 ===")
    os.environ["WDNAI_MODELS_DIR"] = str(_tmp)
    from app.core.compute.registry import (            # noqa: E402
        _parse_autostart_config, refresh_model_presets,
    )
    # 用一个「已安装」的探针 + 一个不存在的模型
    got = _parse_autostart_config([
        {"model": "probe-a", "npu_index": 0},
        {"model": "definitely-not-installed", "npu_index": 1},
    ])
    check(got == [{"model": "probe-a", "npu_index": 0}],
          "未安装模型的条目被跳过，其余保留", str(got))

    print("\n=== 8. refresh_model_presets 把目录变化反映到 MODEL_PRESETS ===")
    from app.core.compute.registry import MODEL_PRESETS  # noqa: E402
    _make_model_dir(_tmp, "Model-C", dict(_MIN_MANIFEST, id="probe-d"))
    changed = refresh_model_presets()
    check(changed and "probe-d" in MODEL_PRESETS,
          "新增模型目录 → refresh 后出现在 MODEL_PRESETS",
          str(sorted(MODEL_PRESETS)))
    shutil.rmtree(_tmp / "Model-C")
    changed = refresh_model_presets()
    check(changed and "probe-d" not in MODEL_PRESETS,
          "删除模型目录 → refresh 后从 MODEL_PRESETS 消失",
          str(sorted(MODEL_PRESETS)))

finally:
    os.environ.pop("WDNAI_MODELS_DIR", None)
    if _prev_env is not None:
        os.environ["WDNAI_MODELS_DIR"] = _prev_env
    shutil.rmtree(_tmp, ignore_errors=True)

print("\n=== 9. 静态审计 ===")
workers_dir = ROOT / "app" / "core" / "compute" / "workers"
leftovers = sorted(p.name for p in workers_dir.glob("*.json"))
check(not leftovers,
      "仓库里不再有 workers/*.json（manifest 已随模型分发）",
      ", ".join(leftovers))
check((workers_dir / "compute_worker.py").is_file(),
      "worker 入口脚本仍在原位（/proc needle 依赖它的路径）")
check((ROOT / "tools" / "worker_manifests").is_dir(),
      "出厂清单目录存在（HF 上传用）")

# 入口脚本路径不得被改动 —— registry._find_workers_by_name 与
# system_monitor 都按 compute_worker.py\x00--model\x00<id>\x00 匹配
# /proc/<pid>/cmdline，改路径/文件名会静默破坏孤儿检测与去重。
from app.core.compute.workers import ENTRY_FILENAME  # noqa: E402
check(ENTRY_FILENAME == "compute_worker.py",
      "入口文件名未变", ENTRY_FILENAME)

print("\n" + "=" * 60)
if _FAILED:
    print(f"❌ {len(_FAILED)} 项未通过:")
    for f in _FAILED:
        print(f"   - {f}")
    raise SystemExit(1)
print("✅ 全部通过")
