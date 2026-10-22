#!/usr/bin/env python
"""校验已安装的卡片插件包是否真的注册上了 ``wdnwebui.cards`` entry point。

给 ``update_rkwhl.sh`` 的 ``verify_plugin_cards`` 调用，也可以单独跑：

    .venv/bin/python scripts/check_plugin_entrypoints.py wdnapps_video_analyzer

为什么需要它
------------
这两个插件包是 pure-Python，没有 ``.so`` 可以断言。但它们有一个更致命的
失败模式：**pip 装成功了，卡片却一个都不出现**，而且全程不报错。

最常见的原因是 entry point 名与 ``manifest.json`` 的 ``id`` 不一致——包目录
用下划线（``cards/video_detection/``），卡 id 一贯用连字符
（``"video-detection"``），照着模块名写就全错。宿主 ``discover()`` 判 FATAL
之后**静默跳过**该卡，于是"装上了"和"卡片能用"是两件事，必须在这里断言。

刻意用 ``importlib.util.find_spec`` 而不是 ``import_module``：前者只 import
父包（``cards/__init__.py`` 是空的，正好），**不执行叶子模块**；后者会顺着
``api.py`` 拉起整个 ``app.core.*``（compute registry、解码器初始化），
在更新脚本里既慢又带副作用。

输出约定：一行一卡，``OK <id> (<dir>)`` 或 ``BAD: <原因>``，**恒定 exit 0**
——判定交给调用方 grep，省掉 shell 里传退出码的那套易错写法。
"""
from __future__ import annotations

import json
import sys
from importlib.metadata import entry_points
from importlib.util import find_spec
from pathlib import Path

GROUP = "wdnwebui.cards"


def check_pkg(pkg: str) -> int:
    """Return the number of problems found for ``pkg``."""
    try:
        eps = sorted(entry_points(group=GROUP), key=lambda e: e.name)
    except Exception as e:  # noqa: BLE001
        print(f"BAD: 读不到 {GROUP} entry point 组: {e}")
        return 1

    # entry point 名的下划线/连字符与卡 id 不同，统一按模块顶层包名归属，
    # 免得包名写法变化就查不到自己的卡。
    mine = [e for e in eps if e.value.split(".")[0] == pkg.replace("-", "_")]

    if not mine:
        print(f"BAD: {pkg} 没注册任何 {GROUP} entry point")
        return 1

    bad = 0
    for e in mine:
        try:
            spec = find_spec(e.value)
        except Exception as ex:  # noqa: BLE001
            print(f"BAD: {e.name}: 定位模块 {e.value} 失败: {ex}")
            bad += 1
            continue
        if spec is None or not spec.submodule_search_locations:
            print(f"BAD: {e.name}: {e.value} 不是包，无法定位卡片目录")
            bad += 1
            continue

        d = Path(list(spec.submodule_search_locations)[0])
        mf = d / "manifest.json"
        if not mf.is_file():
            print(f"BAD: {e.name}: 卡片目录没有 manifest.json ({d})")
            bad += 1
            continue

        try:
            declared = str(
                json.loads(mf.read_text(encoding="utf-8")).get("id", "")
            ).strip()
        except Exception as ex:  # noqa: BLE001
            print(f"BAD: {e.name}: manifest.json 解析失败: {ex}")
            bad += 1
            continue

        if declared != e.name:
            print(
                f"BAD: {e.name}: manifest id {declared!r} != entry point 名 "
                f"{e.name!r} —— 宿主会 FATAL 后静默跳过这张卡"
            )
            bad += 1
            continue

        if not (d / "static").is_dir():
            print(f"BAD: {e.name}: 卡片目录缺 static/ —— 卡片能被发现但前端 404")
            bad += 1
            continue

        print(f"OK {e.name} ({d})")

    return bad


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("用法: check_plugin_entrypoints.py <包名> [<包名> ...]", file=sys.stderr)
        print("       不带参数时校验所有 wdnwebui.cards 插件包", file=sys.stderr)
        return 2
    total = sum(check_pkg(p) for p in argv[1:])
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))