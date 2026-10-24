#!/usr/bin/env python3
"""回归：算力配置的**启动期**能力名迁移绝不能让服务起不来。

故障链（真实踩过）：

1. wdnai 的 ``migrate_legacy`` 曾用 ``value in SoftwareCapability`` 判断
   「是不是当前能力名」。而 ``Enum.__contains__`` 的语义在 Python 版本间
   变过：3.11 对非枚举成员抛 ``TypeError``（连**合法**能力串也是普通 str，
   不是枚举实例 → 一律 TypeError），3.12 才改成按值返回 True/False。
   本板 Python 3.11.2 → ``migrate_legacy("swcap_vision_chat")`` 直接炸。
2. ``ComputeRegistry._migrate_legacy_capability_types()`` 在**启动路径**上
   跑，``except`` 只接 ``RetiredCapabilityError`` / ``ValueError`` →
   TypeError 逃出去 → ``ComputeRegistry.__init__`` 抛 → **整个服务起不来**。

现在两层都堵住了：SDK 侧改成「按值查表 + 捕获 ValueError」（与同文件
``coerce()`` 一致），webui 侧把启动迁移的兜底从 ``except ValueError``
放宽成 ``except Exception`` —— 能力名原样保留，只记日志，不拦停启动。

用例：

  T1 正常的历史别名能迁移（swcap_video_describe → swcap_vision_chat）
  T2 已下线能力被丢弃，且**不**抛
  T3 未知能力串原样保留
  T4 SDK 抽风（抛 TypeError）→ 原样保留且**不抛**（启动路径的兜底）
  T5 ``migrate_legacy`` 本身对合法能力串返回成员（3.11 上不再 TypeError）

不占 NPU、不起服务、不碰真机。
跑法：``.venv/bin/python scripts/verify_boot_capability_migration.py``
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wdnai import SoftwareCapability, migrate_legacy  # noqa: E402
from wdnai.capability import RetiredCapabilityError   # noqa: E402

from app.core.compute import registry as reg           # noqa: E402

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


class _FakeConfig:
    """最小 get_config() 替身：只存 compute 段。"""

    def __init__(self, compute: dict):
        self._compute = compute

    def get_section(self, name: str, default=None):
        if name == "compute":
            return self._compute
        return default

    def set_section(self, name: str, value: dict) -> None:
        if name == "compute":
            self._compute = value


def _run_migration(compute: dict) -> dict:
    """跑一次启动期的能力名迁移，返回迁移后的 compute 段。"""
    fake = _FakeConfig(compute)
    original = reg.get_config
    reg.get_config = lambda: fake
    try:
        obj = reg.ComputeRegistry.__new__(reg.ComputeRegistry)
        obj._migrate_legacy_capability_types()
    finally:
        reg.get_config = original
    return fake.get_section("compute", {})


# --------------------------------------------------------------------------- #
def t1_legacy_alias_migrates() -> None:
    compute = _run_migration({
        "instances": [{"model": "demo", "capability_types": ["swcap_video_describe"]}],
    })
    caps = compute["instances"][0]["capability_types"]
    check("T1a 历史别名迁移成当前能力名",
          caps == ["swcap_vision_chat"], f"caps={caps}")


def t2_retired_capability_dropped() -> None:
    compute = _run_migration({
        "instances": [{"model": "demo", "capability_types": ["swcap_video_query"]}],
    })
    caps = compute["instances"][0]["capability_types"]
    check("T2a 已下线能力被丢弃（不阻停启动）", caps == [], f"caps={caps}")


def t3_unknown_kept_verbatim() -> None:
    compute = _run_migration({
        "instances": [{"model": "demo", "capability_types": ["swcap_not_a_thing"]}],
    })
    caps = compute["instances"][0]["capability_types"]
    check("T3a 未知能力串原样保留",
          caps == ["swcap_not_a_thing"], f"caps={caps}")


def t4_sdk_broken_does_not_raise() -> None:
    """SDK 侧再抽风（抛 TypeError）也不能让启动路径抛出去。"""
    original = reg.migrate_legacy

    def _boom(_value):
        raise TypeError("unsupported operand type(s) for 'in': 'str' and 'EnumType'")

    reg.migrate_legacy = _boom
    try:
        compute = _run_migration({
            "instances": [{"model": "demo", "capability_types": ["swcap_vision_chat"]}],
        })
    except Exception as e:  # noqa: BLE001
        check("T4a SDK 抛异常时启动路径不跟着抛", False, f"{type(e).__name__}: {e}")
        return
    finally:
        reg.migrate_legacy = original
    caps = compute["instances"][0]["capability_types"]
    check("T4a SDK 抛异常时启动路径不跟着抛", True)
    check("T4b 能力名原样保留（不丢配置）",
          caps == ["swcap_vision_chat"], f"caps={caps}")


def t5_migrate_legacy_on_py311() -> None:
    """3.11 上 migrate_legacy 对合法能力串必须返回成员（不再 TypeError）。"""
    try:
        member, renamed = migrate_legacy("swcap_vision_chat")
    except Exception as e:  # noqa: BLE001
        check("T5a 合法能力串解析成成员", False, f"{type(e).__name__}: {e}")
        return
    check("T5a 合法能力串解析成成员",
          member is SoftwareCapability.VISION_CHAT and renamed is False,
          f"member={member!r} renamed={renamed}")
    try:
        migrate_legacy("swcap_not_a_thing")
        check("T5b 未知能力串抛 ValueError（不是 TypeError）", False, "却返回了值")
    except ValueError as e:
        check("T5b 未知能力串抛 ValueError（不是 TypeError）",
              not isinstance(e, TypeError), f"{type(e).__name__}")
    except TypeError as e:
        check("T5b 未知能力串抛 ValueError（不是 TypeError）", False, f"TypeError: {e}")


def main() -> int:
    print("=== 启动期能力名迁移回归 ===")
    t1_legacy_alias_migrates()
    t2_retired_capability_dropped()
    t3_unknown_kept_verbatim()
    t4_sdk_broken_does_not_raise()
    t5_migrate_legacy_on_py311()
    print(f"\n{'PASS' if FAIL == 0 else 'FAIL'}: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
