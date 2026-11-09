#!/usr/bin/env python3
"""能力词表与需求组的验证脚本（不依赖 NPU / 不依赖浏览器）。

覆盖计划里的验收项：

2. worker manifest 的 ``capability_types`` 出现非枚举成员（含
   ``swcap_video_query`` 这类从未上线的幽灵能力）必须报错；
3. 卡片 manifest 的 ``software_capabilities`` 出现非枚举成员必须报错；
4. 需求组：组内能力**必须由同一个实例**同时满足，跨实例不算就绪；
   组声明不完整时退化为逐能力（历史语义），不静默丢需求；
5. 单卡判定的单位是「组」：usb_hid（2 能力 / 1 组）= 单模型需求，
   video-detection（2 能力 / 2 组）= 双模型需求。

⚠️ 第 5 项的**前端**实现在 ``app/static/core/wizard/state.js``（按组数而非
能力数判定），本脚本只验证后端 ``_normalize_capability_groups`` 给出的组划分
与之一致。JS 侧需要 node + 浏览器，见 ``verify_wizard_single_card.mjs``。

用法::

    .venv/bin/python scripts/verify_capability_groups.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wdnai import SoftwareCapability, migrate_legacy          # noqa: E402
from wdnai.capability import RETIRED_CAPABILITIES             # noqa: E402
from wdnai.capability import RetiredCapabilityError          # noqa: E402

from app.core.compute.registry import (                       # noqa: E402
    MODEL_PRESETS,
    _normalize_capability_groups,
)

_FAILED: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}")
    if not ok:
        if detail:
            print(f"       {detail}")
        _FAILED.append(label)
    return ok


def _mk_instance(model_id: str, npu: int, caps: list[str], source: str = "spawned"):
    """A minimal ModelInstance — no proc, no NPU, no DDS."""
    from app.core.compute.registry import ModelInstance
    return ModelInstance(
        key=(npu, model_id, source),
        npu_index=npu,
        model_id=model_id,
        capability_types=list(caps),
        instance_name=model_id,
        status="running",
        _source=source,
    )


class _FakeRegistry:
    """Just enough of ComputeRegistry for the group-matching code paths.

    ``set_active_app`` also writes config.jsonc; this subclass stubs that out
    so the script never touches the user's runtime config.
    """

    def __init__(self, instances):
        import threading
        from app.core.compute.registry import ComputeRegistry
        self._instances = {i.key: i for i in instances}
        self._lock = threading.RLock()
        self._ComputeRegistry = ComputeRegistry

    def list_running_by_capability(self, capability_type):
        return _FakeRegistry._cover(self, {capability_type})

    def list_running_by_capabilities(self, capabilities):
        return _FakeRegistry._cover(self, set(capabilities))

    @staticmethod
    def _cover(reg, wanted):
        if not wanted:
            return []
        out = []
        for inst in reg._instances.values():
            if inst.status != "running":
                continue
            if not wanted.issubset(set(inst.capability_types)):
                continue
            out.append({
                "model_id": inst.model_id,
                "label": inst.instance_name,
                "source": inst._source,
                "npu_index": inst.npu_index,
                "capability_types": list(inst.capability_types),
            })
        out.sort(key=lambda e: e["model_id"])
        return out


# --------------------------------------------------------------------------- #
print("=== 1. 能力词表（wdnai 唯一权威定义）===")
# 10 个成员。``swcap_vision_depth``（Depth Anything V3）、
# ``swcap_text_to_image``（Qwen-Image）、``swcap_audio_asr``（Qwen3-ASR）
# 是 SDK 后续加入的，之前的断言还写着 7 个 —— 词表以 SDK 为准，这里同步过来。
EXPECTED = {
    "swcap_text_chat", "swcap_vision_chat", "swcap_vision_grounding",
    "swcap_audio_chat", "swcap_vision_embedding", "swcap_text_to_speech",
    "swcap_tool_call", "swcap_vision_depth", "swcap_text_to_image",
    "swcap_audio_asr",
}
check(set(SoftwareCapability.values()) == EXPECTED,
      "枚举恰为 10 个成员", str(SoftwareCapability.values()))

print("\n=== 2. worker manifest 校验必须拒绝非枚举能力 ===")
from app.core.compute.workers import _validate                  # noqa: E402

_BASE_WORKER = {
    "label": "T", "spec_factory": "f",
}
for bad, why in [
    (["swcap_video_query"], "幽灵能力（老迁移表的目标，无 worker 声明）"),
    (["swcap_video_describe"], "上一版名字"),
    (["swcap_typo"], "拼写错误"),
    (["TEXT_CHAT"], "成员名而非值"),
]:
    manifest, errors = _validate({**_BASE_WORKER, "capability_types": bad},
                                 "probe")
    check(manifest is None and any("非法能力" in e for e in errors),
          f"拒绝 {bad[0]}（{why}）", str(errors))

ok, errors = _validate({**_BASE_WORKER,
                        "capability_types": ["swcap_vision_chat"]}, "probe")
check(ok is not None and not errors, "接受 swcap_vision_chat", str(errors))

print("\n=== 3. 已安装 worker 只用枚举内的能力 ===")
declared = {c for p in MODEL_PRESETS.values()
            for c in (p.get("capability_types") or [])}
check(declared <= EXPECTED, "已安装 worker 声明的能力都在词表内",
      str(sorted(declared - EXPECTED)))

# provider 是**板子状态**，不是词表属性：worker manifest 随模型文件夹一起
# 分发（``<models_root>/<ModelDir>/<model_id>.json``），没装模型就没有
# provider。原来「词表每个能力都有 provider」是恒真的，因为 MODEL_PRESETS
# 是代码仓里的全量清单；现在 MODEL_PRESETS = **本板已安装的模型**，那条
# 断言不再成立 —— 而且它**不该**成立：没装 Depth-Anything-V3 的板子本来
# 就没有 swcap_vision_depth 的 provider。
#
# 真正要守的是这两条：
#   1. 出厂清单（tools/worker_manifests/）声明的能力都在词表内；
#   2. 本板已安装的模型 ⊆ 出厂清单（板上不会出现清单外的 worker）。
STAGING = ROOT / "tools" / "worker_manifests"
if STAGING.is_dir():
    shipped: set[str] = set()
    shipped_ids: set[str] = set()
    for f in sorted(STAGING.glob("*.json")):
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        shipped |= set(raw.get("capability_types") or [])
        shipped_ids.add(str(raw.get("id") or f.stem))
    check(shipped <= EXPECTED, "出厂清单没有枚举外能力",
          str(sorted(shipped - EXPECTED)))
    check(set(MODEL_PRESETS) <= shipped_ids,
          "本板已安装的模型都在出厂清单内",
          str(sorted(set(MODEL_PRESETS) - shipped_ids)))
    uncovered = sorted(EXPECTED - shipped)
    print(f"  [INFO] 词表里没有 worker 提供的能力: {uncovered or '无'}"
          "（合法：词表封闭 ≠ 每个能力都必须有实现）")
else:
    print("[SKIP] 出厂清单目录不存在（manifest 已全部随模型分发）—— "
          "跳过出厂清单对照检查")

print("\n=== 4. 卡片 manifest 校验（capability_groups 派生 + 引用完整性）===")
from app.core.manifest import _derive_capability_groups, discover  # noqa: E402

cards = discover()
check(all(m.status == "ok" for m in cards), "8 张卡全部 status=ok",
      "; ".join(f"{m.id}:{m.errors}" for m in cards if m.status != "ok"))
grouped = {m.id: m.capability_groups for m in cards if m.capability_groups}
check(grouped == {"usb-hid": [["swcap_vision_chat", "swcap_tool_call"]]},
      "只有 usb-hid 声明多能力组", str(grouped))
for m in cards:
    for grp in m.capability_groups:
        for cap in grp:
            check(cap in m.software_capabilities,
                  f"{m.id} 组内 {cap} 已在 software_capabilities 中")

usb_dir = next(m.dir for m in cards if m.id == "usb-hid")
check(_derive_capability_groups(usb_dir)
      == [["swcap_vision_chat", "swcap_tool_call"]],
      "_derive_capability_groups 复现 usb_hid 的组")

print("\n=== 5. 组归一化：单位是「组」不是「能力」===")
USB_CAPS = ["swcap_vision_chat", "swcap_tool_call"]
USB_GROUP = [USB_CAPS]
VD_CAPS = ["swcap_vision_chat", "swcap_audio_chat"]
VD_GROUPS = [["swcap_vision_chat"], ["swcap_audio_chat"]]

check(len(_normalize_capability_groups(USB_CAPS, USB_GROUP)) == 1,
      "usb_hid 2 能力 / 1 组 → 单模型需求（单卡可用）",
      str(_normalize_capability_groups(USB_CAPS, USB_GROUP)))
check(len(_normalize_capability_groups(VD_CAPS, VD_GROUPS)) == 2,
      "vd 2 能力 / 2 组 → 双模型需求（单卡需二选一）",
      str(_normalize_capability_groups(VD_CAPS, VD_GROUPS)))
check(len(_normalize_capability_groups(["swcap_text_chat"], None)) == 1,
      "未声明 group 的单能力 → 1 组（历史行为不变）")
check(_normalize_capability_groups(VD_CAPS, [["swcap_vision_chat"]])
      == [["swcap_vision_chat"], ["swcap_audio_chat"]],
      "组声明不完整 → 缺失项各自成组，不被静默丢弃",
      str(_normalize_capability_groups(VD_CAPS, [["swcap_vision_chat"]])))
check(_normalize_capability_groups(["swcap_text_chat"], [[], None, "junk"])
      == [["swcap_text_chat"]],
      "空组 / 非列表组被忽略")

print("\n=== 6. AND 语义：一个实例同时满足整组 ===")
one_model = [_mk_instance("qwen3.5-vl-4b", 0,
                          ["swcap_vision_chat", "swcap_tool_call"])]
two_models = [
    _mk_instance("vlm-only", 0, ["swcap_vision_chat"]),
    _mk_instance("tool-only", 1, ["swcap_tool_call"]),
]
check(len(_FakeRegistry(one_model).list_running_by_capabilities(USB_CAPS)) == 1,
      "一个实例同时具备两项 → 命中 1 个")
check(len(_FakeRegistry(two_models).list_running_by_capabilities(USB_CAPS)) == 0,
      "两个实例各满足一半 → **不命中**（这就是原 bug）",
      str(_FakeRegistry(two_models).list_running_by_capabilities(USB_CAPS)))
check(len(_FakeRegistry(two_models).list_running_by_capability(
          "swcap_vision_chat")) == 1,
      "逐能力查仍会命中（所以前端求交集不够，后端必须按 AND）")

print("\n=== 7. set_active_app 的组校验 ===")
# ⚠️ `set_active_app` 成功时会把 autostart 增量**写进真实 config.jsonc**。
# 这个脚本构造的实例带着假模型名（vlm-only / tool-only），一旦走到写入分支
# 就会把假条目留在用户配置里 —— 曾经就这么污染过一次（开机日志刷
# "compute.autostart[1].model='vlm-only' 不在 MODEL_PRESETS"）。
# 所以这里把 get_config 换成内存桩，让写入无处可去。
import app.core.compute.registry as _reg_mod  # noqa: E402


class _FakeCfg:
    def __init__(self):
        self.writes = []

    def get_section(self, name, default=None):
        return {}

    def set_section(self, name, value):
        self.writes.append((name, value))


_fake_cfg = _FakeCfg()
_real_get_config = _reg_mod.get_config
_reg_mod.get_config = lambda: _fake_cfg
try:
    reg = _FakeRegistry(two_models)
    try:
        from app.core.compute.registry import ComputeRegistry
        ComputeRegistry.set_active_app(
            reg, "usb-hid", USB_CAPS, USB_GROUP)
        check(False, "组未被单个实例满足 → 必须拒绝",
              "却返回了成功（会写进错误的 active_app）")
    except ValueError as e:
        msg = str(e)
        check("swcap_vision_chat + swcap_tool_call" in msg,
              "组未被单个实例满足 → 拒绝且文案点名是哪一组", msg)
    except Exception as e:  # noqa: BLE001
        check(False, "组校验路径可执行", f"{type(e).__name__}: {e}")
    check(not _fake_cfg.writes, "组校验失败时不得写配置", str(_fake_cfg.writes))

    # 反面：组被满足时应成功写入（但只进内存桩，不碰真实 config.jsonc）
    _fake_cfg.writes.clear()
    ok_reg = _FakeRegistry(one_model)
    ComputeRegistry.set_active_app(ok_reg, "usb-hid", USB_CAPS, USB_GROUP)
    check(len(_fake_cfg.writes) == 1,
          "组被满足时正常写入（且只写到桩）", str(_fake_cfg.writes))
    # 同组两个能力由同一实例承载 → autostart 只应追加**一条**，不能按能力加两次
    written = _fake_cfg.writes[0][1].get("autostart") if _fake_cfg.writes else None
    check(isinstance(written, list) and len(written) == 1,
          "同组只追加一条 autostart（不是每个能力一条）", str(written))

    # 上游 d144747 的那个维度：同一个模型跑两张卡时，两张卡的实例都承载
    # 这些 capability，autostart 里两张都要记 —— 只记一张下次开机另一张卡
    # 空着。组逻辑（本次）合进来时不能把这个维度弄丢，所以 pin 住。
    _fake_cfg.writes.clear()
    two_cards = [
        _mk_instance("qwen3.5-vl-4b", 0, USB_CAPS),
        _mk_instance("qwen3.5-vl-4b", 1, USB_CAPS),
    ]
    ComputeRegistry.set_active_app(
        _FakeRegistry(two_cards), "usb-hid", USB_CAPS, USB_GROUP)
    written = _fake_cfg.writes[0][1].get("autostart") if _fake_cfg.writes else None
    check(isinstance(written, list) and len(written) == 2
          and {e["npu_index"] for e in written} == {0, 1},
          "同模型跑两张卡 → 两张实例都记进 autostart（d144747 不丢）",
          str(written))

    # 两个组共用一个 cap 时，同一个实例不能被记两次
    _fake_cfg.writes.clear()
    both = [_mk_instance("qwen3.5-vl-4b", 0, USB_CAPS + ["swcap_audio_chat"])]
    ComputeRegistry.set_active_app(
        _FakeRegistry(both), "usb-hid", USB_CAPS, [USB_GROUP, ["swcap_audio_chat"]])
    written = _fake_cfg.writes[0][1].get("autostart") if _fake_cfg.writes else None
    check(isinstance(written, list) and len(written) == 1,
          "多组共用同一实例 → autostart 只记一条（不重复）", str(written))
finally:
    _reg_mod.get_config = _real_get_config

print("\n=== 8. 历史能力名迁移 ===")
for old, want in [
    ("video_describe", SoftwareCapability.VISION_CHAT),
    ("swcap_video_describe", SoftwareCapability.VISION_CHAT),
    ("swcap_computer_control", SoftwareCapability.TOOL_CALL),
    ("swcap_video_retrieval", SoftwareCapability.VISION_EMBEDDING),
]:
    got, renamed = migrate_legacy(old)
    check(got is want and renamed, f"{old} → {want.value}", str(got))
for gone in RETIRED_CAPABILITIES:
    try:
        migrate_legacy(gone)
        check(False, f"已下线能力 {gone} → 报错而非静默映射", "却返回了值")
    except RetiredCapabilityError as e:
        check("已下线" in str(e), f"已下线能力 {gone} → 明确报错", str(e))

print("\n=== 9. 用户可见文案不被枚举名污染 ===")
check(f"功能未启用: {SoftwareCapability.VISION_CHAT}"
      == "功能未启用: swcap_vision_chat",
      "f-string 渲染出裸值（否则全站报错变 "
      "'SoftwareCapability.VISION_CHAT'）")

print()
if _FAILED:
    print(f"❌ {len(_FAILED)} 项未通过:")
    for f in _FAILED:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")