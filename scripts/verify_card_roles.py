#!/usr/bin/env python3
"""回归：**两卡卡位策略** —— 翻译跟随识别、语音合成用剩下的卡、重启后布局不变。

背景
----
一张 RK1828 协卡 5120 MB 跑三个模型（识别 1600 + 翻译 2200 + 语音合成
1900）怎么排？实测结论：

* **感知链**（识别 + 翻译）**同卡** —— 真机实测同卡 vs 分卡的争用 ≈ 0
  （识别轮间隔中位 1.21 s vs 1.22 s，端点后第一轮都是 1.68 s）。因为识别
  的端点判定发生在**收尾推理之后**，翻译那一拍正好落在识别的空档里。
* **发声链**（语音合成）**独占一张卡** —— 它是**流式生成、边生成边播**，
  播报那几秒 NPU 一直忙；与识别共卡会把识别轮间隔顶到 > 1 s ⇒ 客户端
  上行队列溢出 ⇒ 丢块 + 「有音频丢失」。

⇒ 两卡板的标准排法：**卡0 = 识别 + 翻译（感知链）**，**卡1 = 语音合成**。

这条策略落在宿主的 :mod:`app.core.compute.card_roles`（**纯函数**），
三处都问它：翻译模型起在哪张卡、语音合成起在哪张卡、开机自启恢复出来的
布局。**纯函数 ⇒ 同样输入必然同样输出 ⇒ 重启后布局不变**。

跑法::

    .venv/bin/python scripts/verify_card_roles.py              # 纯逻辑
    .venv/bin/python scripts/verify_card_roles.py --live       # 真机：重启后布局不变
    .venv/bin/python scripts/verify_card_roles.py --live --restart   # 顺带重启服务

⚠️ ``--live --restart`` 会 **systemctl restart wdnwebui.service**
（连带杀掉 cgroup 里的 compute_worker —— 它们会被 autostart 重新拉起）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.compute import card_roles as cr  # noqa: E402
from app.core.compute.registry import (  # noqa: E402
    _crop_autostart_for_cards,
    _merge_autostart_entry,
)

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f"  ({detail})" if detail else ""))


# --------------------------------------------------------------------------- #
# 假 registry（不碰真机状态）
# --------------------------------------------------------------------------- #
PRESETS = {
    "qwen3-asr": {"memory_estimate_mb": 1600},
    "hy-mt-1-5": {"memory_estimate_mb": 2200},
    "qwen3-tts": {"memory_estimate_mb": 1900},
    "qwen3-vl-4b": {"memory_estimate_mb": 3500},
    "LocateAnything-3b": {"memory_estimate_mb": 5500},
}


class FakeReg:
    """只实现 card_roles 用到的那几个方法。"""

    def __init__(self, cards, running=None, presets=None):
        self._cards = list(cards)
        self._running = dict(running or {})
        self._presets = dict(presets or PRESETS)

    def model_cards(self):
        return list(self._cards)

    def local_model_cards(self, model_id):
        return list(self._running.get(model_id, []))

    def local_memory_usage(self):
        per = {}
        for m, cs in self._running.items():
            mem = int(self._presets[m]["memory_estimate_mb"])
            for c in cs:
                per[c] = per.get(c, 0) + mem
        return per, sum(per.values()), 0


def ids(entries) -> str:
    return ",".join(f"{e['model']}@{e['npu_index']}" for e in entries)


# --------------------------------------------------------------------------- #
print("\n【1】角色与共卡白名单")
check("识别 + 翻译 = 感知链", cr.role_of("qwen3-asr") == cr.ROLE_PERCEPTION
      and cr.role_of("hy-mt-1-5") == cr.ROLE_PERCEPTION)
check("语音合成 = 发声链", cr.role_of("qwen3-tts") == cr.ROLE_SPEECH)
check("感知链内部**允许共卡**",
      cr.can_share_card("qwen3-asr", "hy-mt-1-5") is True)
check("感知链 ↔ 发声链**不允许共卡**",
      cr.can_share_card("qwen3-asr", "qwen3-tts") is False
      and cr.can_share_card("hy-mt-1-5", "qwen3-tts") is False)
check("其它模型之间也不允许共卡（一卡一模型仍是默认）",
      cr.can_share_card("qwen3-asr", "LocateAnything-3b") is False)
check("同模型自己不能共卡",
      cr.can_share_card("qwen3-asr", "qwen3-asr") is False)
check("显存 1600 + 2200 = 3800 ≤ 5120",
      cr.fits(1600, 2200) is True)
check("显存 3500 + 2500 = 6000 > 5120",
      cr.fits(3500, 2500) is False)

# --------------------------------------------------------------------------- #
print("\n【2】两卡板的标准排法（卡0 = 感知链，卡1 = 发声链）")
reg2 = FakeReg([0, 1])
plan = cr.plan_layout(reg2)
check("空板：感知链落在卡 0",
      plan["plan"].get("qwen3-asr") == 0
      and plan["plan"].get("hy-mt-1-5") == 0, str(plan["plan"]))
check("空板：语音合成落在卡 1（剩下的那张）",
      plan["plan"].get("qwen3-tts") == 1, str(plan["plan"]))
check("占用：卡0 = 3800 MB / 卡1 = 1900 MB",
      plan["per_card_memory_mb"].get(0) == 3800
      and plan["per_card_memory_mb"].get(1) == 1900,
      str(plan["per_card_memory_mb"]))

print("\n【3】翻译**跟随**识别所在的卡（不是写死卡号）")
reg = FakeReg([0, 1, 2], {"qwen3-asr": [1]})
check("识别在卡 1 ⇒ 翻译跟到卡 1",
      cr.pick_card(reg, "hy-mt-1-5") == 1)
check("语音合成躲开卡 1 ⇒ 落卡 0",
      cr.pick_card(reg, "qwen3-tts") == 0)
reg = FakeReg([0, 1, 2], {"qwen3-asr": [2]})
check("识别在卡 2 ⇒ 翻译跟到卡 2",
      cr.pick_card(reg, "hy-mt-1-5") == 2)
check("语音合成落卡 0",
      cr.pick_card(reg, "qwen3-tts") == 0)

print("\n【4】识别 + 翻译都已在卡 0 ⇒ 语音合成必须换卡")
reg = FakeReg([0, 1], {"qwen3-asr": [0], "hy-mt-1-5": [0]})
check("语音合成落卡 1", cr.pick_card(reg, "qwen3-tts") == 1)
check("翻译仍在卡 0（已在跑就不搬家）",
      cr.pick_card(reg, "hy-mt-1-5") == 0)

print("\n【5】语音合成先起 ⇒ 感知链让路（不与发声链抢卡）")
reg = FakeReg([0, 1, 2], {"qwen3-tts": [0]})
check("识别落到卡 1（卡 0 留给发声链）",
      cr.pick_card(reg, "qwen3-asr") == 1)
check("翻译跟到卡 1", cr.pick_card(reg, "hy-mt-1-5") == 1)

print("\n【6】autostart 裁剪 —— 感知链允许共卡")
kept, dropped = _crop_autostart_for_cards(
    [{"model": "qwen3-asr", "npu_index": 0},
     {"model": "hy-mt-1-5", "npu_index": 0},
     {"model": "qwen3-tts", "npu_index": 1}],
    [0, 1])
check("识别@0 + 翻译@0 + 合成@1 **三条都保留**",
      ids(kept) == "qwen3-asr@0,hy-mt-1-5@0,qwen3-tts@1", ids(kept))
check("一条都没丢", len(dropped) == 0, ids(dropped))

kept, dropped = _crop_autostart_for_cards(
    [{"model": "qwen3-asr", "npu_index": 0},
     {"model": "LocateAnything-3b", "npu_index": 0}],
    [0, 1])
check("感知链 ↔ 别的模型 仍然互斥（一条被丢）",
      ids(kept) == "qwen3-asr@0" and len(dropped) == 1, ids(kept))
check("丢弃条目带人话原因", bool(dropped[0].get("reason")), dropped[0].get("reason", ""))

kept, dropped = _crop_autostart_for_cards(
    [{"model": "qwen3-asr", "npu_index": 0},
     {"model": "hy-mt-1-5", "npu_index": 0},
     {"model": "qwen3-vl-4b", "npu_index": 0}],
    [0, 1])
check("同卡塞第三个模型（1600+2200+3500 = 7300 > 5120）⇒ 被丢",
      ids(kept) == "qwen3-asr@0,hy-mt-1-5@0" and len(dropped) == 1, ids(kept))

kept, _ = _crop_autostart_for_cards(
    [{"model": "qwen3-asr", "npu_index": 0},
     {"model": "hy-mt-1-5", "npu_index": 0},
     {"model": "qwen3-tts", "npu_index": 1}], [0, 1])
check("★ 两卡布局在 autostart 里**完整存活**（重启后能原样恢复）",
      ids(kept) == "qwen3-asr@0,hy-mt-1-5@0,qwen3-tts@1", ids(kept))

# --------------------------------------------------------------------------- #
print("\n【7】autostart 合并 —— 感知链不互相挤掉")
merged, added, stale = _merge_autostart_entry([], "qwen3-asr", 0)
check("空配置 → 新增一条", added and ids(merged) == "qwen3-asr@0", ids(merged))
merged, added, stale = _merge_autostart_entry(
    merged, "hy-mt-1-5", 0)
check("翻译写进同一张卡 ⇒ **两条都在**",
      ids(merged) == "qwen3-asr@0,hy-mt-1-5@0" and added, ids(merged))
check("没有条目被挤掉", len(stale) == 0, ids(stale))
merged, added, stale = _merge_autostart_entry(
    merged, "qwen3-tts", 1)
check("合成写到卡 1 ⇒ 三条都在",
      ids(merged) == "qwen3-asr@0,hy-mt-1-5@0,qwen3-tts@1", ids(merged))
merged, added, stale = _merge_autostart_entry(
    [{"model": "LocateAnything-3b", "npu_index": 0}], "qwen3-asr", 0)
check("不能共卡的旧条目仍然被替换掉",
      ids(merged) == "qwen3-asr@0" and len(stale) == 1, ids(merged))

# --------------------------------------------------------------------------- #
print("\n【8】record_autostart_entry —— 真写 config")


def _check_record():
    from app.core.compute import registry as reg_mod

    class _FakeConfig:
        def __init__(self):
            self.section = {"compute": {"autostart": [], "active_app": None}}

        def get_section(self, name, default=None):
            return self.section.get(name, default)

        def set_section(self, name, value):
            self.section[name] = value

    cfg = _FakeConfig()
    orig = reg_mod.get_config
    reg_mod.get_config = lambda: cfg
    try:
        reg = reg_mod.ComputeRegistry.__new__(reg_mod.ComputeRegistry)
        reg._lock = __import__("threading").RLock()
        reg._instances = {}
        changed = reg.record_autostart_entry("hy-mt-1-5", 0)
        again = reg.record_autostart_entry("hy-mt-1-5", 0)
    finally:
        reg_mod.get_config = orig
    written = cfg.get_section("compute")["autostart"]
    return changed, again, written


changed, again, written = _check_record()
check("第一次写入 ⇒ 有变化", changed is True)
check("写进 config 的 autostart",
      written == [{"model": "hy-mt-1-5", "npu_index": 0}], str(written))
check("重复写同一条目 ⇒ 幂等（无变化）", again is False)

# --------------------------------------------------------------------------- #
# 真机部分
# --------------------------------------------------------------------------- #
BASE = os.environ.get("WDNWEBUI_BASE", "https://127.0.0.1:8000")


def _http_json(path, method="GET", timeout=20):
    import ssl
    import urllib.request
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(BASE + path, method=method)
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.loads(r.read().decode("utf-8"))


def _current_layout():
    """现在每个模型跑在哪张卡（只算本机实例）。"""
    snap = _http_json("/api/compute")
    out = {}
    for inst in snap.get("instances") or []:
        if inst.get("status") != "running":
            continue
        if inst.get("_source") not in ("spawned", "adopted_local"):
            continue
        out.setdefault(inst.get("model_id"), set()).add(inst.get("npu_index"))
    return {k: sorted(v) for k, v in out.items()}


if "--live" in sys.argv:
    print("\n【9】真机：重启服务后布局不变")
    try:
        before = _current_layout()
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️ 连不上 {BASE}：{exc}")
        before = {}
    print(f"  重启前：{before}")

    if "--restart" in sys.argv and before:
        subprocess.run(["sudo", "systemctl", "restart", "wdnwebui.service"],
                       check=False)
        print("  已重启服务，等模型回来 …")
        deadline = time.time() + 240
        after = {}
        while time.time() < deadline:
            time.sleep(5)
            try:
                after = _current_layout()
            except Exception:  # noqa: BLE001
                continue
            if set(after) >= set(before) and all(
                    after.get(k) == before.get(k) for k in before):
                break
        check("重启后**每个模型的卡位都不变**", after == before,
              f"{before} → {after}")
    else:
        print("  （加 --restart 才会真的重启服务）")

    try:
        roles = _http_json("/api/compute/card-roles")
        check("策略端点可用", "plan" in roles and "current" in roles,
              str(roles.get("plan")))
        check("感知链的建议卡位一致（识别与翻译同一张卡）",
              roles["plan"].get("qwen3-asr") == roles["plan"].get("hy-mt-1-5"),
              str(roles["plan"]))
        check("语音合成与感知链**不同卡**",
              roles["plan"].get("qwen3-tts") != roles["plan"].get("qwen3-asr"),
              str(roles["plan"]))
    except Exception as exc:  # noqa: BLE001
        check("策略端点可用", False, str(exc))

# --------------------------------------------------------------------------- #
print("\n" + "=" * 56)
print(f"通过 {PASS} / {PASS + FAIL}")
if FAIL:
    print("❌ 有失败项")
    sys.exit(1)
print("✅ 全部通过")
