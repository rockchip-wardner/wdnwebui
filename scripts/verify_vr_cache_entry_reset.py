#!/usr/bin/env python3
"""回归防护：删除单条缓存条目后，当前会话必须作废内存索引。

背景（用户实测）：向导完成并检索成功 → 删除对应缓存条目 → 退出应用 →
重新进向导 → 进度条直接 100%、检索有结果，但**没有生成缓存**。

根因：``DELETE /cache/entry`` 只删 LanceDB 行，没有像 ``/cache/clear``
那样 ``reset_session()``；单例仍是 ``state=ready`` + 旧 ``_index``，前端
``vrStartSearch`` 对「ready + 同源」短路 → 只查旧索引、不重新提取。

本脚本钉住：
1. 被删条目 md5 == 当前会话 ``_md5`` → 调 ``reset_session`` 且返回
   ``session_reset=true``；
2. md5 不同 → 不动当前会话；
3. 前端删除按钮处理 ``session_reset``、向导完成强制 ``vrStart(true)``、
   手动按钮 handler 不把 click 事件当 forceExtract 参数。

不占 NPU。

用法::

    python -m scripts.verify_vr_cache_entry_reset
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, ".")

from scripts.card_dirs import resolve_cards

_CARD = next(c for c in resolve_cards() if c["id"] == "video-retrieval")
api = importlib.import_module(f"{_CARD['ns']}.{_CARD['mod']}.api")
APP_JS = Path(_CARD["dir"]) / "static" / "app.js"

fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("[PASS] " if cond else "[FAIL] ") + name
          + (f" — {detail}" if detail else ""))
    if not cond:
        fails.append(name)


class FakeCache:
    def __init__(self):
        self.calls = []

    def delete_video(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return 1


class FakeService:
    def __init__(self, md5):
        self._md5 = md5
        self.resets = 0

    def reset_session(self):
        self.resets += 1
        return {}


async def main() -> int:
    payload = {
        "md5": "abc123", "chunk_seconds": 4,
        "chunk_overlap_seconds": 1,
        "judge_fp": "gscore:10:10", "slice_version": "gscore-v1",
    }

    # 1) md5 匹配 → reset
    svc = FakeService("abc123")
    cache = FakeCache()
    with patch.object(api, "get_service", return_value=svc), \
            patch("app.core.video.cache.get_video_cache", return_value=cache):
        out = await api.delete_cache_entry(dict(payload))
    check("[1] md5 匹配 → reset_session", svc.resets == 1, str(svc.resets))
    check("[2] 返回 session_reset=true", out.get("session_reset") is True, str(out))
    check("[3] 删除参数带 judge_fp/slice_version",
          bool(cache.calls) and cache.calls[0][1].get("judge_fp") == "gscore:10:10",
          str(cache.calls[:1]))

    # 2) md5 不同 → 不动会话
    svc = FakeService("other")
    cache = FakeCache()
    with patch.object(api, "get_service", return_value=svc), \
            patch("app.core.video.cache.get_video_cache", return_value=cache):
        out = await api.delete_cache_entry(dict(payload))
    check("[4] md5 不同 → 不 reset", svc.resets == 0, str(svc.resets))
    check("[5] 返回 session_reset=false", out.get("session_reset") is False, str(out))

    # 3) 前端接线
    js = APP_JS.read_text(encoding="utf-8")
    check("[6] 前端删除按钮处理 session_reset",
          "data.session_reset" in js)
    check("[7] 向导完成强制 vrStart(true)",
          "start: () => vrStart(true)" in js)
    check("[8] 手动开始不把 click 事件当 forceExtract",
          'startBtn?.addEventListener("click", () => vrStart())' in js)
    check("[9] forceExtract 跳过 ready 同源短路",
          "!forceExtract && st === \"ready\"" in js)

    print()
    print(f"{len(fails)} 项失败")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
