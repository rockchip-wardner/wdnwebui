#!/usr/bin/env python3
"""回归：算力页「开机自启」徽章必须跟着 config 走。

真机症状：跑完「音视频描述」应用后，config.jsonc 的 ``compute.autostart``
里有两条（``qwen3-vl-4b@npu0`` + ``qwen2.5-omni-3b@npu1``），但算力配置页
上只有 [VLM]视觉描述v3 带「开机自启」徽章，[Omni]多感知描述v2.5 没有。

根因：徽章读的是实例上缓存的 ``_autostart`` 标志，而那个标志**只在 boot
时置位** —— 运行期进卡片时 ``set_active_app()`` 把模型追加进 config，实例
上的标志还是空的。同一模型的两张卡也会一个有徽章一个没有（取决于 boot
循环有没有扫到它，纯时序问题）。

修法：徽章（和算力页 radio 的默认卡位）一律从 **config 现算** ——
``_autostart_npu_map()`` 读配置，``_with_autostart_markers()`` 覆盖实例
dict 上过期的快照值。

跑法：``.venv/bin/python scripts/verify_autostart_badge.py``
（纯逻辑，不碰真机状态）
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.compute import registry as reg_mod  # noqa: E402
from app.core.compute.registry import (  # noqa: E402
    _autostart_npu_map,
    _with_autostart_markers,
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


def _inst_dict(model_id: str, source: str, flag: str = "",
               npu: int = -1) -> dict:
    """一份实例 dict（只带徽章逻辑用到的字段）。"""
    return {"model_id": model_id, "_source": source, "_autostart": flag,
            "_default_npu_index": -1, "npu_index": npu}


print("\n【1】徽章按 config 派生，不按实例上缓存的标志")
npus = {"qwen3-vl-4b": 0, "qwen2.5-omni-3b": 1}

d = _with_autostart_markers(_inst_dict("qwen2.5-omni-3b", "spawned"), npus)
check("config 里有 → enabled（哪怕实例上的标志是空的）",
      d["_autostart"] == "enabled", d["_autostart"])
check("默认卡位 = config 里的卡号",
      d["_default_npu_index"] == 1, str(d["_default_npu_index"]))

d = _with_autostart_markers(
    _inst_dict("LocateAnything-3b", "spawned", flag="enabled"), npus)
check("config 里没有 → 没有徽章（哪怕实例上的标志是 enabled —— "
      "反向过期）", d["_autostart"] == "", d["_autostart"])
check("config 里没有 → 默认卡位 -1",
      d["_default_npu_index"] == -1, str(d["_default_npu_index"]))

print("\n【2】同一个模型的本地卡 / peer 卡 / 占位卡")
d_local = _with_autostart_markers(_inst_dict("qwen3-vl-4b", "spawned"), npus)
d_orphan = _with_autostart_markers(
    _inst_dict("qwen3-vl-4b", "adopted_local"), npus)
d_peer = _with_autostart_markers(_inst_dict("qwen3-vl-4b", "adopted"), npus)
d_ph = _with_autostart_markers(_inst_dict("qwen3-vl-4b", "unavailable"), npus)
check("本机卡（spawned）→ 有徽章", d_local["_autostart"] == "enabled")
check("本机孤儿卡（adopted_local）→ 有徽章",
      d_orphan["_autostart"] == "enabled")
check("占位卡（unavailable）→ 有徽章", d_ph["_autostart"] == "enabled")
check("peer 卡（adopted）→ **不**显示「开机自启」（那是本板的声明，"
      "跟对端板子的卡无关）", d_peer["_autostart"] == "", d_peer["_autostart"])
check("peer 卡的默认卡位仍按 config 给", d_peer["_default_npu_index"] == 0)

print("\n【3】_autostart_npu_map 读 config")


class _FakeConfig:
    def __init__(self, section):
        self.section = section

    def get_section(self, name, default=None):
        return self.section.get(name, default)


def _with_fake_config(section, fn):
    orig = reg_mod.get_config
    reg_mod.get_config = lambda: _FakeConfig(section)
    try:
        return fn()
    finally:
        reg_mod.get_config = orig


got = _with_fake_config(
    {"compute": {"autostart": [{"model": "qwen3-vl-4b", "npu_index": 0},
                               {"model": "qwen2.5-omni-3b",
                                "npu_index": 1}]}},
    _autostart_npu_map)
check("正常解析 → {model: npu}",
      got == {"qwen3-vl-4b": 0, "qwen2.5-omni-3b": 1}, str(got))

got = _with_fake_config({"compute": {}}, _autostart_npu_map)
check("没有 autostart 字段 → 空 map", got == {}, str(got))

got = _with_fake_config({"compute": {"autostart": "not-a-list"}},
                         _autostart_npu_map)
check("配置非法 → 空 map（不抛，页面照常渲染）", got == {}, str(got))

got = _with_fake_config(
    {"compute": {"autostart": [{"model": "qwen3-vl-4b", "npu_index": 0,
                                "bogus": 1}]}},
    _autostart_npu_map)
check("条目含未知字段 → 空 map（schema 拒了就是没配）", got == {}, str(got))

print("\n" + "=" * 56)
print(f"通过 {PASS} / {PASS + FAIL}")
if FAIL:
    print("❌ 有失败项")
    sys.exit(1)
print("✅ 全部通过")
