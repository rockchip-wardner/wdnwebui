#!/usr/bin/env python3
"""回归：网页翻译的**模型包 manifest** 必须声明 max_replicas_per_card=2。

背景（2026-11-18 真机现场）
--------------------------
向导「一键开启所需模型」→ ``POST /api/compute/instances`` → **400**：

    {"detail":"hy-mt-1-5 每张卡最多 1 份（请求了第 2 份）"}

后端那一条校验读的是 **worker manifest** 的 ``max_replicas_per_card``
（``app/core/compute/workers/__init__.py`` 里**缺省 = 1**）。而卡片的
``wizard.json`` 按「每卡 2 份」规划 4 个槽位（2026-11-16 实测：2 卡 ×
每卡 2 份 = 1.45s / 2.31×）⇒ **计划与后端契约打架**。

模型包里的 manifest 当时**漏了这个字段** ⇒ 整个一键开启是死路。

⇒ 治本：模型包（``<模型目录>/<model_id>.json``）必须带

    "max_replicas_per_card": 2

⚠️ **manifest 随模型一起分发** —— 重新下载模型包会把本脚本改过的
字段冲掉 ⇒ 修完要把这个字段加进**模型包的上游**（模型仓），否则
下次重装又会复现。

本脚本断言：

1. 盘上的 manifest 有 ``max_replicas_per_card == 2``；
2. 后端 ``GET /api/compute`` 报出来的上限也是 2（热重扫已生效）；
3. 卡片的槽位计划（每卡 2 份）**不超**这个上限；
4. 后端确实按这个字段校验（静态查 400 分支）—— 字段不是摆设。

用法::

    .venv/bin/python scripts/verify_translate_replica_manifest.py
"""
from __future__ import annotations

import glob
import json
import os
import ssl
import sys
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from card_dirs import card_dir as _card_dir  # noqa: E402

CARD = str(_card_dir("web-translate"))
WIZARD_JSON = os.path.join(CARD, "static/wizard.json")
BASE = os.environ.get("WDNWEBUI_BASE", "https://127.0.0.1:8000").rstrip("/")
MODEL_ID = "hy-mt-1-5"
WANT_MAX_REP = 2

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ok   {name}")
    else:
        _FAIL += 1
        print(f"  FAIL {name} {extra}")


def get_json(path: str):
    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(BASE + path, timeout=20, context=ctx) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    print("=" * 72)
    print(f"网页翻译：模型包 manifest 的 max_replicas_per_card（{MODEL_ID}）")
    print("=" * 72)

    snap = get_json("/api/compute")
    models_root = snap.get("models_root") or ""
    print(f"  models_root = {models_root}")

    # ---------------------------------------------------------------- [1] --
    print("\n[1] 盘上的 worker manifest")
    hits = sorted(glob.glob(os.path.join(models_root, "*", MODEL_ID + ".json")))
    check(f"找到 {MODEL_ID}.json", bool(hits), str(hits))
    if not hits:
        print("\n" + "=" * 72)
        print(f"通过 {_PASS} / 失败 {_FAIL}")
        print("❌ 本板没装这个模型 —— 后续断言跳过")
        return 1
    manifest_path = hits[0]
    raw = open(manifest_path, encoding="utf-8").read()
    man = json.loads(raw)
    print(f"  manifest = {manifest_path}")
    check("manifest 声明了 max_replicas_per_card",
          "max_replicas_per_card" in man, str(list(man.keys())))
    check(f"max_replicas_per_card == {WANT_MAX_REP}",
          man.get("max_replicas_per_card") == WANT_MAX_REP,
          f"实际 {man.get('max_replicas_per_card')}")
    check("manifest 仍是合法 JSON（没被改坏）",
          isinstance(man.get("capability_types"), list)
          and "swcap_translate" in man.get("capability_types", []),
          str(man.get("capability_types")))
    check("notes 等长字段没丢", bool(man.get("notes")), "")

    # ---------------------------------------------------------------- [2] --
    print("\n[2] 后端已经看到新上限（模型热重扫）")
    entry = None
    for m in snap.get("supported_models") or []:
        if m.get("id") == MODEL_ID:
            entry = m
            break
    check("supported_models 里有这个模型", entry is not None)
    if entry:
        check(f"后端报的上限 == {WANT_MAX_REP}",
              entry.get("max_replicas_per_card") == WANT_MAX_REP,
              f"实际 {entry.get('max_replicas_per_card')}")

    # ---------------------------------------------------------------- [3] --
    print("\n[3] 卡片的槽位计划不超上限")
    wiz = json.load(open(WIZARD_JSON, encoding="utf-8"))
    plan = wiz["swcap_to_model"]["swcap_translate"]
    slots = plan.get("instance_slots") or []
    max_replica = max([int(sl.get("replica") or 0) for sl in slots] or [0])
    print(f"  计划 {slots} ⇒ 最高副本 {max_replica}")
    check(f"计划最高副本（{max_replica}）≤ 模型上限（{WANT_MAX_REP}）",
          max_replica <= WANT_MAX_REP, "")
    check("计划确实是「每卡 2 份 × 2 张卡」= 4 份",
          len(slots) == 4 and max_replica == 1, str(slots))

    # ---------------------------------------------------------------- [4] --
    print("\n[4] 后端确实按这个字段校验（字段不是摆设）")
    api_src = open(os.path.join(REPO, "app/system/api/compute.py"),
                   encoding="utf-8").read()
    check("POST /compute/instances 里有 max_replicas_per_card 校验",
          "max_replicas_per_card" in api_src)
    check("超限时返 400（每张卡最多 N 份）",
          "每张卡最多" in api_src and "status_code=400" in api_src)
    manifest_src = open(os.path.join(REPO, "app/core/compute/workers/__init__.py"),
                        encoding="utf-8").read()
    check("manifest 缺字段时默认 1（这就是现场 400 的来源）",
          'raw.get("max_replicas_per_card", 1)' in manifest_src)

    print("\n" + "=" * 72)
    print(f"通过 {_PASS} / 失败 {_FAIL}")
    print("=" * 72)
    if _FAIL:
        print("❌ 有断言没过")
        return 1
    print("✅ 全部通过")
    print(f"\n⚠️ 提醒：manifest 随模型分发 —— 重新下载 {MODEL_ID} 会把字段冲掉，")
    print("   请把 \"max_replicas_per_card\": 2 加进**模型包的上游仓库**。")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
