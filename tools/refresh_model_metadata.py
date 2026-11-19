#!/usr/bin/env python3
"""重算模型目录的 ``SHA256SUMS`` + ``config.json``，让它们与磁盘上的文件一致。

为什么需要这个脚本
==================

模型目录里的两个元数据文件描述的是「仓里有哪些文件、各自多大、摘要多少」：

* ``SHA256SUMS`` —— ``<sha256>  <文件名>``，逐行；
* ``config.json`` 的 ``files[]`` —— 每个文件的 ``path`` / ``size`` / ``sha256`` /
  ``lfs``。

模型文件被**手工替换**（转换工具重新导出、直接从别的板子拷过来）之后，这两个
文件就**过期**了：摘要对不上、size 对不上。后果是：

1. ``tools/publish_hf_model.py`` 的**传前校验**会判定「本地文件坏了」而**拒绝
   上传**（传上去的是坏文件，比不传更糟）；
2. 已经推上去的仓里，``SHA256SUMS`` 与实际内容对不上 ⇒ 下载方按摘要校验会
   报「校验和不匹配」。

⇒ 每次手工换完模型文件，**先跑本脚本**再推送。

用法
====

::

    # 干跑：只报出会改什么，不落盘
    python tools/refresh_model_metadata.py --dir /userdata/models/Qwen3.5-VL-9B --dry-run

    # 实际写入（模型目录通常是 root 的，要 sudo）
    sudo python tools/refresh_model_metadata.py --dir /userdata/models/Qwen3.5-VL-9B

它做三件事
==========

1. 逐文件重算 ``size`` 与 ``sha256``，写回 ``config.json`` 的 ``files[]``；
2. 重写 ``SHA256SUMS``（文件名按字节序排序，与仓里其它模型的格式一致）；
3. 把 ``config.json`` 的 ``worker`` 段**投影**自模型目录里的 worker manifest
   —— 标签 / 显存估算这类值只有一个真源（manifest），描述文件不再手抄第二份。

只改**内容**，不改结构：``config.json`` 里 ``files[]`` 的路径清单以磁盘上实际
存在的文件为准；manifest 缺失时只做前两步。

两种模式
========

* **config 驱动**（目录里有 ``config.json``，transformers 布局）—— 文件清单取
  ``config.json`` 的 ``files[]``，同时刷新它的 size/sha256 与 ``worker`` 段。
* **目录驱动**（目录里**没有** ``config.json``，RKNN 部署包）—— 文件清单 =
  目录下全部文件（递归），减去元文件本身；只写 ``SHA256SUMS``。

两种模式下的 ``SHA256SUMS`` 覆盖范围一致：**模型内容 + worker manifest +
转换报告**，不含 ``README*`` / ``LICENSE`` / ``NOTICE`` / ``.gitattributes`` /
``config.json`` / ``SHA256SUMS`` 自己。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

#: ``config.json`` 的 worker 段里，哪些字段以 manifest 为准。
#: ``id`` 不在其中 —— 它是 spec_factory 的名字（``qwen3_5_vl_9b``），
#: 与 manifest 的 model_id（文件名）不是一回事。
WORKER_FIELDS_FROM_MANIFEST = (
    "label",
    "capability_types",
    "model_dir_env",
    "model_dir_name",
    "available_targets",
    "memory_estimate_mb",
    "estimated_init_seconds",
)

#: 元文件本身 —— 不进 ``SHA256SUMS``（它是「关于这些文件的清单」，
#: 把自己也算进去就成了自指）。
META_FILE_NAMES = {
    "README.md", "README_CN.md", "LICENSE", "LICENSE-QWEN", "NOTICE",
    ".gitattributes", "config.json", "SHA256SUMS",
}

#: 目录驱动模式要跳过的目录。
SKIP_DIR_NAMES = {".git", ".cache", "__pycache__"}

CHUNK = 1 << 20


def sha256_file(path: Path) -> Tuple[str, int]:
    """返回 ``(sha256 十六进制, 字节数)``。分块读，不吃内存。"""
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def find_manifest(model_dir: Path) -> Optional[Path]:
    """模型目录里的 worker manifest —— 目录里除 config.json 外的唯一 .json。"""
    cands = sorted(p for p in model_dir.glob("*.json") if p.name != "config.json")
    return cands[0] if len(cands) == 1 else None


def scan_content_files(model_dir: Path) -> List[str]:
    """目录驱动模式的文件清单：递归全部文件，去掉元文件与隐藏目录。

    返回**相对路径**（用 ``/`` 分隔），按字节序排序 —— 与 ``sha256sum -c``
    在仓根目录下直接可用。
    """
    out: List[str] = []
    for root, dirs, files in os.walk(model_dir):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIR_NAMES)
        for name in files:
            if name in META_FILE_NAMES:
                continue
            rel = (Path(root) / name).relative_to(model_dir).as_posix()
            out.append(rel)
    return sorted(out)


def _read_sums(path: Path) -> Dict[str, str]:
    """读现有的 ``SHA256SUMS`` → ``{文件名: sha256}``；读不到就当空。"""
    out: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        digest, _, name = line.partition("  ")
        if name.strip():
            out[name.strip()] = digest.strip()
    return out


def refresh(model_dir: Path, manifest: Optional[Path], dry_run: bool) -> int:
    cfg_path = model_dir / "config.json"
    have_cfg = cfg_path.exists()
    if not have_cfg:
        print("（目录里没有 config.json —— 走「目录驱动」模式：只写 SHA256SUMS）")
        cfg = None
        listed: List[str] = scan_content_files(model_dir)
        if not listed:
            print("!! 目录里没有可登记的文件")
            return 1
    else:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        raw = cfg.get("files")
        if not isinstance(raw, list) or not raw:
            print("!! config.json 没有 files[]")
            return 1
        listed = [str(e["path"]) for e in raw]
        missing = [p for p in listed if not (model_dir / p).is_file()]
        if missing:
            print("!! config.json 列了但磁盘上没有：%s" % ", ".join(missing))
            return 1

    # ---- ① 重算每个文件的 size / sha256 ---------------------------------- #
    print("① 重算文件摘要")
    # 旧值取自现有的 SHA256SUMS（那是唯一权威）；没有就当全变。
    old = _read_sums(model_dir / "SHA256SUMS")
    digests: Dict[str, str] = {}
    sizes: Dict[str, int] = {}
    changed: List[str] = []
    for name in listed:
        digest, size = sha256_file(model_dir / name)
        digests[name] = digest
        sizes[name] = size
        if old.get(name) != digest:
            changed.append(name)
            print("   ~ %-46s %s → %s  (%d B)"
                  % (name, str(old.get(name))[:12], digest[:12], size))
        if cfg:
            for entry in cfg["files"]:
                if str(entry["path"]) == name:
                    entry["sha256"] = digest
                    entry["size"] = size
    if not changed:
        print("   所有文件的摘要都已是最新的")

    # ---- ② worker 段投影自 manifest -------------------------------------- #
    if cfg is not None:
        print("② worker 段对齐 manifest")
        if manifest is None:
            print("   （目录里没有唯一的 worker manifest，跳过）")
        else:
            try:
                man = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                print("   !! manifest 读不了，跳过：%s" % exc)
            else:
                worker = cfg.setdefault("worker", {})
                for key in WORKER_FIELDS_FROM_MANIFEST:
                    if key not in man:
                        continue
                    if worker.get(key) != man[key]:
                        print("   ~ worker.%s: %s → %s" % (key, worker.get(key), man[key]))
                        worker[key] = man[key]
    else:
        print("② worker 段 —— 目录驱动模式不涉及 config.json，跳过")

    # ---- ③ 写盘 ----------------------------------------------------------- #
    sums = "".join("%s  %s\n" % (digests[name], name) for name in sorted(listed))

    if dry_run:
        print("\n干跑：未写盘。将刷新 SHA256SUMS（%d 项）%s"
              % (len(listed), "与 config.json" if cfg else ""))
        return 0

    if cfg is not None:
        cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8")
    (model_dir / "SHA256SUMS").write_text(sums, encoding="utf-8")
    print("\n已写入：SHA256SUMS%s"
          % (" / %s" % cfg_path if cfg else ""))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", dest="model_dir", required=True, help="模型目录")
    ap.add_argument("--manifest", default=None,
                    help="worker manifest 路径（默认取目录里除 config.json 外的唯一 .json）")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写盘")
    args = ap.parse_args(argv)

    model_dir = Path(args.model_dir).expanduser().resolve()
    if not model_dir.is_dir():
        print("!! 目录不存在：%s" % model_dir)
        return 1

    manifest = Path(args.manifest).expanduser() if args.manifest else find_manifest(model_dir)
    if manifest is not None and not manifest.is_file():
        print("!! manifest 不存在：%s" % manifest)
        return 1
    if manifest:
        print("模型目录 : %s" % model_dir)
        print("manifest : %s" % manifest)
    else:
        print("模型目录 : %s" % model_dir)
    print()
    return refresh(model_dir, manifest, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
