#!/usr/bin/env python3
"""把 worker manifest 从仓库暂存目录铺进各自的模型目录。

worker manifest 现在**随模型文件夹一起分发**：一个模型文件夹 = 权重 +
描述它的 manifest。HuggingFace 上的一个 repo 因此就是完整的安装单元，
用户 ``git clone`` 下来就能用。

这个脚本负责**一次性迁移**：把仓库 ``tools/worker_manifests/`` 里的
manifest 复制到模型根目录下对应的模型文件夹里。

用法
----
    python -m scripts.migrate_worker_manifests            # 实际写入
    python -m scripts.migrate_worker_manifests --dry-run  # 只报告
    python -m scripts.migrate_worker_manifests --root /userdata/models

规则
----
* 目标目录 = ``<models_root>/<model_dir_name>``。``model_dir_name`` 是
  manifest 里的建议目录名；迁移时它是指南，迁移**之后**真正生效的是
  「manifest 所在目录」。
* 模型目录不存在 → **跳过**并报告（不报错、不创建空目录）。用户还没
  clone 那个模型是正常状态，不是错误。
* 已存在的同名 manifest → 默认覆盖（``--no-clobber`` 可改成跳过）。
* 写入的是 **manifest 本身**，不碰任何权重文件。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STAGING_DIR = REPO_ROOT / "tools" / "worker_manifests"


def _models_root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("WDNAI_MODELS_DIR")
    if env and env.strip():
        return Path(env.strip())
    sys.path.insert(0, str(REPO_ROOT))
    from app.core.config import default_models_root
    return default_models_root()


def _plan(staging: Path, root: Path) -> list[dict]:
    """Map every staged manifest to its target path under ``root``."""
    plan: list[dict] = []
    if not staging.is_dir():
        return plan
    for src in sorted(staging.glob("*.json")):
        try:
            raw = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            plan.append({"src": src, "dst": None,
                         "status": "unreadable", "detail": str(exc)})
            continue
        model_id = str(raw.get("id") or src.stem)
        dir_name = str(raw.get("model_dir_name") or "").strip()
        if not dir_name:
            plan.append({"src": src, "dst": None, "status": "no-dir-name",
                         "detail": "manifest 没有 model_dir_name"})
            continue
        dst = root / dir_name / f"{model_id}.json"
        plan.append({
            "src": src,
            "dst": dst,
            "model_id": model_id,
            "dir_name": dir_name,
            "status": "pending",
            "detail": "",
        })
    return plan


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=None,
                    help="模型根目录（默认取 WDNAI_MODELS_DIR / default_models_root()）")
    ap.add_argument("--staging", default=str(STAGING_DIR),
                    help=f"manifest 暂存目录（默认 {STAGING_DIR}）")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写入")
    ap.add_argument("--no-clobber", action="store_true",
                    help="目标已存在时跳过，不覆盖")
    args = ap.parse_args(argv)

    root = _models_root(args.root)
    staging = Path(args.staging).expanduser()

    print(f"模型根目录 : {root}")
    print(f"暂存目录   : {staging}")
    if not root.is_dir():
        print(f"!! 模型根目录不存在 —— 没有已安装的模型可迁移（跳过全部）")
        return 1
    if not staging.is_dir():
        print(f"!! 暂存目录不存在: {staging}")
        return 1

    plan = _plan(staging, root)
    if not plan:
        print("暂存目录里没有 manifest —— 无事可做")
        return 0

    copied = skipped = failed = 0
    for item in plan:
        src, dst = item["src"], item["dst"]
        if item["status"] != "pending":
            print(f"  [跳过] {src.name}: {item['status']} ({item['detail']})")
            failed += 1
            continue
        model_dir = dst.parent
        if not model_dir.is_dir():
            print(f"  [跳过] {item['model_id']}: 模型目录 {model_dir} 不存在"
                  f"（该模型未安装）")
            skipped += 1
            continue
        if dst.exists() and args.no_clobber:
            print(f"  [跳过] {item['model_id']}: {dst} 已存在（--no-clobber）")
            skipped += 1
            continue
        if args.dry_run:
            print(f"  [计划] {src.name} → {dst}")
            copied += 1
            continue
        try:
            shutil.copyfile(src, dst)
            print(f"  [完成] {src.name} → {dst}")
            copied += 1
        except OSError as exc:
            print(f"  [失败] {src.name} → {dst}: {exc}")
            failed += 1

    verb = "计划写入" if args.dry_run else "写入"
    print(f"\n{verb} {copied} 个，跳过 {skipped} 个，失败 {failed} 个。")
    if skipped:
        print("提示：跳过的模型说明该模型文件夹还没放到位 —— "
              "clone 完模型后重跑本脚本即可补上 manifest。")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
