#!/usr/bin/env python3
"""解析每张卡的**源码目录** → ``{card_id: 目录绝对路径}``。

卡片发现有两条通道（见 ``app/core/manifest.py``）：

* 内置目录 ``app/apps/<id>/``
* entry point 插件包（``wdnwebui.cards`` 组）—— 音视频描述 / 视频目标监控 /
  监控反查 / 视频任意目标定位 / 音视频分析 / 智能电脑控制 / 语音生成 /
  文生图 / 图生图 这 9 张卡已迁到 ``wdnapps_video_analyzer`` /
  ``wdnapps_hdmi_processor`` / ``wdnapps_audio_chat`` /
  ``wdnapps_media_generator`` 四个插件包里，**源码不在本仓库**，在
  ``.venv/src/`` 下。

verify 脚本不能再硬编码 ``app/apps/<id>/`` —— 那样插件卡一律读不到文件。
统一走这里，脚本就不用关心卡在仓库里还是在插件包里。

命令行用法（给 node 脚本用）::

    .venv/bin/python scripts/card_dirs.py          # 打印 JSON 映射

刻意用 ``importlib.util.find_spec`` 而不是 ``import_module``：前者只 import
父包（``cards/__init__.py`` 是空的），**不执行叶子模块** —— 后者会顺着
``api.py`` 拉起整个 ``app.core.*``（compute registry、解码器初始化），
在测试脚本里既慢又带副作用。理由与 ``check_plugin_entrypoints.py`` 相同。
"""
from __future__ import annotations

import json
import os
import sys
from importlib.metadata import entry_points
from importlib.util import find_spec
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APPS_DIR = REPO / "app" / "apps"
ENTRY_POINT_GROUP = "wdnwebui.cards"


def _read_manifest_id(card_dir: Path) -> str | None:
    """卡片目录 → manifest 里的 ``id``（读不到返回 None）。"""
    manifest_path = card_dir / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    card_id = raw.get("id")
    return card_id if isinstance(card_id, str) and card_id else None


def _builtin_card_dirs() -> list[tuple[Path, str, str]]:
    """``(目录, 命名空间, 自身模块名)``。内置卡在 ``app.apps`` 命名空间下。"""
    if not APPS_DIR.is_dir():
        return []
    out = []
    for d in sorted(APPS_DIR.iterdir()):
        if not d.is_dir() or d.name.startswith((".", "_")):
            continue
        if (d / "manifest.json").is_file():
            out.append((d, "app.apps", d.name.replace("-", "_")))
    return out


def _plugin_card_dirs() -> list[tuple[Path, str, str]]:
    """``(目录, 命名空间, 自身模块名)``。插件卡在 ``<包>.cards`` 命名空间下
    —— 跨卡 import 形如 ``wdnapps_video_analyzer.cards.video_retrieval``。"""
    try:
        eps = list(entry_points(group=ENTRY_POINT_GROUP))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for ep in eps:
        try:
            spec = find_spec(ep.value)
        except Exception:  # noqa: BLE001
            continue
        if spec is None or not spec.origin:
            continue
        card_dir = Path(spec.origin).resolve().parent
        if not (card_dir / "manifest.json").is_file():
            continue
        pkg = ep.value.split(".")[0]
        out.append((card_dir, f"{pkg}.cards", card_dir.name.replace("-", "_")))
    return out


def resolve_cards() -> list[dict]:
    """所有卡：``[{"id":…, "name": 目录名, "dir": 绝对路径, "ns":…, "mod":…}, …]``。

    ``id`` 是 manifest 里的卡 id（连字符，如 ``video-detection``），
    ``name`` 是目录名（下划线，如 ``video_target_detection``）—— 两种写法
    历史脚本都用过，两个都给。

    ``ns`` 是卡的 import 命名空间（``app.apps`` / ``<包>.cards``），
    ``mod`` 是自身模块名 —— 判断「跨卡 import」要靠这两个。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for card_dir, ns, mod in _plugin_card_dirs() + _builtin_card_dirs():
        card_id = _read_manifest_id(card_dir)
        if not card_id or card_id in seen:
            continue
        seen.add(card_id)
        out.append({"id": card_id, "name": card_dir.name,
                    "dir": str(card_dir), "ns": ns, "mod": mod})
    return out


def resolve_card_dirs() -> dict[str, str]:
    """``{card_id: 卡片目录绝对路径}``。同一张卡被两条通道同时提供时，
    **内置目录优先**（与宿主 ``discover()`` 的顺序一致）。"""
    return {c["id"]: c["dir"] for c in resolve_cards()}


def card_dir(card_id: str) -> Path:
    """单张卡的目录；找不到直接抛（调用方据此报「卡不存在」）。"""
    dirs = resolve_card_dirs()
    if card_id not in dirs:
        raise KeyError(
            f"找不到卡片 {card_id!r}（已发现：{sorted(dirs)}）")
    return Path(dirs[card_id])


def main() -> int:
    if len(sys.argv) > 1:
        try:
            print(str(card_dir(sys.argv[1])))
        except KeyError as e:
            print(str(e), file=sys.stderr)
            return 1
        return 0
    print(json.dumps(resolve_cards(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
