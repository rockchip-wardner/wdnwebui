"""回归防护：模型目录扫描不能被**大 JSON** 拖死。

背景（2026-11-03 实测，RK3588 真机）：算力配置页卡在「加载中」，根因之一
是 ``/api/compute`` 每秒轮询时把 event loop 堵了近 2 秒 ——
``discover()`` 会把模型目录里**每个** ``*.json`` 都 ``json.loads`` 解析
一遍，包括 ``/userdata/models/Qwen3-TTS/tokenizer.json``（**11.4 MB**）。

manifest 本身只有几百字节到 2KB，所以只解析 ``< MAX_MANIFEST_BYTES``
(64KB) 的 json。canonical 名（``worker.json``）超限时仍然解析 —— 好让它走
「解析失败 → ``status=fatal``」的既有分支，而不是**静默消失**。

本脚本**不占 NPU**，在临时目录里造模型目录：

  1. 大 json（11MB）不被解析、不被当成 manifest；
  2. 同目录下的正常 manifest 仍被正确发现，且 discover() < 100ms；
  3. canonical 名超限时报 fatal（而不是静默消失）；
  4. 非 canonical 的超大坏 json 被安静跳过。

用法::

    python -m scripts.verify_manifest_scan_cost
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.compute.workers import (  # noqa: E402
    CANONICAL_MANIFEST_NAME,
    MAX_MANIFEST_BYTES,
    discover,
    models_root,
)

FAILS: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  [OK]   {label}")
    else:
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))
        FAILS.append(label)


@contextmanager
def _model_root(root: Path):
    """把模型根指到临时目录，退出时还原。"""
    prev = os.environ.get("WDNAI_MODELS_DIR")
    os.environ["WDNAI_MODELS_DIR"] = str(root)
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("WDNAI_MODELS_DIR", None)
        else:
            os.environ["WDNAI_MODELS_DIR"] = prev


def _manifest(mdir: Path, name: str, mid: str) -> None:
    (mdir / name).write_text(json.dumps({
        "id": mid,
        "label": mid,
        "spec_factory": "wdnai.model.build_spec",
        "model_dir_name": mdir.name,
        "worker_name": mid[:8],
        "capability_types": ["swcap_vision_chat"],
    }), encoding="utf-8")


def _big_json(mdir: Path, name: str = "tokenizer.json") -> Path:
    """造一个 ~11MB 的合法 json —— 无脑 json.loads 要 ~2s。"""
    p = mdir / name
    p.write_text(json.dumps({"vocab": "x" * (11 * 1024 * 1024)}), encoding="utf-8")
    return p


def main() -> int:
    print("== verify_manifest_scan_cost ==")

    # ---- 大 json + 正常 manifest 同目录
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        mdir = root / "BigModel"
        mdir.mkdir()
        big = _big_json(mdir)
        print(f"  （造的 tokenizer.json = {big.stat().st_size / 1024 / 1024:.1f} MB）")
        _manifest(mdir, "bigmodel.json", "bigmodel")

        with _model_root(root):
            t0 = time.perf_counter()
            found = discover()
            dt = (time.perf_counter() - t0) * 1000

        ids = {m.id for m in found}
        check("bigmodel" in ids, "同目录下的正常 manifest 仍被发现",
              f"ids={sorted(ids)}")
        check(len(found) == 1, "大 json 没被当成 manifest",
              f"found={[m.id for m in found]}")
        check(dt < 100, f"discover() 耗时 < 100ms（实测 {dt:.1f}ms）", f"{dt:.1f}ms")

    # ---- 只有大 json → 不产出任何 manifest
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        mdir = root / "OnlyBig"
        mdir.mkdir()
        _big_json(mdir)
        with _model_root(root):
            found = discover()
        check(len(found) == 0, "只有大 json 的目录不产出 manifest",
              f"found={[m.id for m in found]}")

    # ---- canonical 名超限 + 非法 JSON → 必须报 fatal（不能静默消失）
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        mdir = root / "OversizeCanonical"
        mdir.mkdir()
        (mdir / CANONICAL_MANIFEST_NAME).write_text(
            "{ not valid json " + "x" * (MAX_MANIFEST_BYTES + 10), encoding="utf-8")
        with _model_root(root):
            found = discover()
        # 解析失败时拿不到 body 里的 id —— 既有行为是用文件名 stem 当 id
        ov = next((m for m in found if m.id == Path(CANONICAL_MANIFEST_NAME).stem), None)
        check(ov is not None and ov.status == "fatal",
              "canonical 名超限时报 fatal（不静默消失）",
              f"found={[(m.id, m.status) for m in found]}")

    # ---- 非 canonical 名 + 超大 + 非法 JSON → 安静跳过（不刷 WARNING）
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        mdir = root / "BigBroken"
        mdir.mkdir()
        (mdir / "something.json").write_text(
            "{ broken " + "x" * (MAX_MANIFEST_BYTES + 10), encoding="utf-8")
        with _model_root(root):
            found = discover()
        check(len(found) == 0, "非 canonical 的超大坏 json 被安静跳过",
              f"found={[(m.id, m.status) for m in found]}")

    # ---- 真实模型根（现场有 11.4MB tokenizer.json）
    real_root = models_root()
    if real_root.is_dir():
        t0 = time.perf_counter()
        found = discover()
        dt = (time.perf_counter() - t0) * 1000
        check(dt < 100,
              f"真实模型根 discover() < 100ms（{real_root}，实测 {dt:.1f}ms）",
              f"{dt:.1f}ms")
        check(len(found) > 0, "真实模型根仍能发现 manifest", f"found={len(found)}")

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 条")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
