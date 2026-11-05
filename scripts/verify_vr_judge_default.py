#!/usr/bin/env python3
"""回归防护：监控反查的默认判据阈值必须独立于三张视频卡。

背景：三张视频卡是 25fps 逐帧口径，gscore 默认阈值 10；监控反查是
1 秒/帧采样，同一个 10 在 1 秒间隔上 p50 就已接近阈值，几乎每帧都放行
（实测课堂素材 2531s → 770+ 段）。VR 因此在 1 秒采样口径上单独标定
默认值 25。

本脚本钉住：
1. VR 初始阈值 = 25，且共享 gscore 默认值仍为 10（三卡不受影响）；
2. `manual → gscore` 且未显式传 threshold 时回到 25
   （FrameJudge.reconfigure 默认会回落到共享的 10，必须由 service 补）；
3. 只改 every_n 不覆盖用户自定义阈值；
4. 显式 threshold 优先；
5. VR `/judge/metrics` 下发自己的 default_threshold；
6. 判据同步预览使用**生效结果**（而不是原始请求体）；
7. wizard.json 的默认阈值 = 25。

不占 NPU。

用法::

    python -m scripts.verify_vr_judge_default
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, ".")

from scripts.card_dirs import resolve_cards

from app.core.video import search as S
from app.core.video.frame_judge import EXPOSED_METRICS, default_threshold

_CARD = next(c for c in resolve_cards() if c["id"] == "video-retrieval")
api = importlib.import_module(f"{_CARD['ns']}.{_CARD['mod']}.api")
WIZARD = Path(_CARD["dir"]) / "static" / "wizard.json"

fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("[PASS] " if cond else "[FAIL] ") + name
          + (f" — {detail}" if detail else ""))
    if not cond:
        fails.append(name)


def main() -> int:
    check("常量：VR 默认阈值 = 25",
          S.DEFAULT_JUDGE_THRESHOLD == 25,
          str(S.DEFAULT_JUDGE_THRESHOLD))
    check("常量：VR every_n 仍为 10",
          S.DEFAULT_JUDGE_EVERY_N == 10,
          str(S.DEFAULT_JUDGE_EVERY_N))
    check("共享 gscore 默认阈值仍为 10（三卡不受影响）",
          default_threshold(EXPOSED_METRICS[0]) == 10.0,
          str(default_threshold(EXPOSED_METRICS[0])))

    svc = S.VideoSearchService()
    check("[1] 初始 mode / threshold",
          svc.judge_snapshot()["mode"] == "gscore"
          and svc.judge_snapshot()["threshold"] == 25.0,
          str(svc.judge_snapshot()))

    svc.set_judge({"mode": "manual"})
    check("[2] 切到 manual", svc.judge_snapshot()["mode"] == "manual",
          str(svc.judge_snapshot()["mode"]))

    svc.set_judge({"mode": "gscore"})
    snap = svc.judge_snapshot()
    check("[3] manual → gscore 未传阈值 → 25",
          snap["mode"] == "gscore" and snap["threshold"] == 25.0,
          f"mode={snap['mode']} threshold={snap['threshold']}")

    svc.set_judge({"mode": "gscore", "threshold": 30})
    svc.set_judge({"every_n": 20})
    snap = svc.judge_snapshot()
    check("[4] 只改 every_n 不覆盖用户阈值 30",
          snap["threshold"] == 30.0 and snap["every_n"] == 20,
          f"threshold={snap['threshold']} every_n={snap['every_n']}")

    svc.set_judge({"mode": "manual"})
    svc.set_judge({"mode": "auto"})
    snap = svc.judge_snapshot()
    check("[5] manual → 旧别名 auto → gscore 且阈值 25",
          snap["mode"] == "gscore" and snap["threshold"] == 25.0,
          f"mode={snap['mode']} threshold={snap['threshold']}")

    data = asyncio.run(api.get_judge_metrics())
    check("[6] VR /judge/metrics 的 gscore 默认阈值 = 25",
          data.get("default_threshold", {}).get(EXPOSED_METRICS[0]) == 25,
          str(data.get("default_threshold")))
    check("[7] VR /judge/metrics 的 default_every_n = 10",
          data.get("default_every_n") == 10,
          str(data.get("default_every_n")))

    seen = {}

    class _FakeSession:
        def configure_judge(self, **kwargs):
            seen.update(kwargs)

    class _FakeService:
        def set_judge(self, payload):
            return {"mode": "gscore", "every_n": 20, "threshold": 25.0}

    with patch.object(api, "get_service", return_value=_FakeService()), \
            patch.object(api, "get_session", return_value=_FakeSession()):
        asyncio.run(api.set_judge(api.JudgePayload(mode="gscore")))
    check("[8] 预览同步的是生效结果（含补出来的 25）",
          seen.get("threshold") == 25.0,
          str(seen))

    wizard = json.loads(WIZARD.read_text(encoding="utf-8"))
    fields = {}
    for step in wizard.get("steps", []):
        for f in step.get("fields", []) or []:
            if isinstance(f, dict) and f.get("id"):
                fields[f["id"]] = f
    thr = fields.get("vr-judge-threshold", {}).get("default")
    every = fields.get("vr-judge-every-n", {}).get("default")
    check("[9] wizard.json 默认阈值 = 25", thr == 25, str(thr))
    check("[10] wizard.json 默认 every_n = 10", every == 10, str(every))

    if fails:
        print(f"\nFAILED: {len(fails)} 项 — {', '.join(fails)}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
