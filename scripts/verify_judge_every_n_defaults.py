#!/usr/bin/env python3
"""回归防护：判据的**帧间隔默认值三处必须一致**（600），且 30 必须已被清除。

背景（用户实测，2026-10）：

    「视频目标监控」快速配置里帧间隔默认 **30**，而音视频描述 / 视频目标
    定位都是 **600**。

根因不是设计决定，是抄错了地方：vtd 的 ``wizard.json`` 在 ``a0a98cb``
（多路页接通帧判据）里被从 600 改成了 30 —— 那个 30 是**后端当时的**
``DEFAULT_EVERY_N = 30``，即 detector 时代"每 30 帧送一次"的固定节流。
同一批 commit 里 vtl / vd 的 600 没动，差异就一直留着；v0.25 把视频卡拆成
插件包时又被原样冻结进 ``wdnapps_video_analyzer``。

**危害不只是"数字不一样"**。多路页的判据是**每轮转评估一次**
（``multi_session._run_one_channel`` → ``judge.observe(thumb, ch.frames)``），
两次评估之间该路已经解了 25~50+ 帧，``gap`` 一次就涨过 30 → 兜底条件
``gap >= every_n and score >= threshold*0.5`` **恒成立** → gscore 门被架空，
画面没变也照送 NPU。600 才是"兜底间隔"该有的量级（≈24s）。

所以本脚本钉两件事：

  1. **三处默认值一致**：后端 ``frame_judge.DEFAULT_EVERY_N``、共享前端组件
     ``app/static/core/frame-judge.js``、每张卡的 ``wizard.json`` 里
     ``<prefix>-judge-every-n.default`` —— 全部等于 600。
  2. **行为正确**：用真实的 ``FrameJudge`` 跑一轮"多路轮转"模拟，
     断言 every_n=30 时兜底恒成立（门失效）、every_n=600 时门真的挡得住。

卡片源码现在在插件包里（``.venv/src/wdnapps-video-analyzer/``），脚本通过
``scripts/card_dirs.py`` 定位卡目录，**不硬编码** ``app/apps/<id>/``。

用法::

    python -m scripts.verify_judge_every_n_defaults
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, ".")

REPO = Path(__file__).resolve().parent.parent
EXPECTED = 600

fails: list[str] = []
notes: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}"
          + (f" — {detail}" if detail else ""))
    if not ok:
        fails.append(f"{label}" + (f" — {detail}" if detail else ""))


# --------------------------------------------------------------------------- #
# 1) 后端常量                                                               #
# --------------------------------------------------------------------------- #
def check_backend() -> None:
    print("\n[1] 后端常量 frame_judge.DEFAULT_EVERY_N")
    from app.core.video.frame_judge import DEFAULT_EVERY_N
    check(DEFAULT_EVERY_N == EXPECTED,
          f"DEFAULT_EVERY_N == {EXPECTED}",
          f"实测 {DEFAULT_EVERY_N}")


# --------------------------------------------------------------------------- #
# 2) 共享前端组件                                                           #
# --------------------------------------------------------------------------- #
def check_frontend() -> None:
    print("\n[2] 共享前端组件 app/static/core/frame-judge.js")
    src = (REPO / "app" / "static" / "core" / "frame-judge.js").read_text(
        encoding="utf-8")

    # 2a) 控件初值
    m = re.search(r'this\.inpEvery\.value\s*=\s*"(\d+)"', src)
    check(bool(m) and int(m.group(1)) == EXPECTED,
          f'inpEvery.value == "{EXPECTED}"',
          m.group(1) if m else "未找到")

    # 2b) _fallbackCfg 的 every_n
    m = re.search(r"every_n:\s*(\d+)", src)
    check(bool(m) and int(m.group(1)) == EXPECTED,
          f"_fallbackCfg every_n == {EXPECTED}",
          m.group(1) if m else "未找到")

    # 2c) 输入框为空时的 fallback 不能再是 30
    stale = re.findall(r'inpEvery\?\.value\s*\|\|\s*"(\d+)"', src)
    stale += re.findall(r"this\._everyN\s*\|\|\s*(\d+)", src)
    check(all(int(v) == EXPECTED for v in stale),
          f"输入框空值 fallback 都是 {EXPECTED}",
          f"实测 {stale}" if stale else "无 fallback")


# --------------------------------------------------------------------------- #
# 3) 每张卡的 wizard.json                                                   #
# --------------------------------------------------------------------------- #
def check_wizards() -> None:
    print("\n[3] 各卡 wizard.json 的 <prefix>-judge-every-n.default")
    from scripts.card_dirs import resolve_card_dirs

    found = 0
    for card_id, card_dir in resolve_card_dirs().items():
        wizard = Path(card_dir) / "static" / "wizard.json"
        if not wizard.is_file():
            continue
        try:
            cfg = json.loads(wizard.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            check(False, f"{card_id}: wizard.json 可解析", str(exc))
            continue
        for step in cfg.get("steps") or []:
            for field in step.get("fields") or []:
                fid = str(field.get("id") or "")
                if not fid.endswith("-judge-every-n"):
                    continue
                found += 1
                default = field.get("default")
                check(default == EXPECTED,
                      f"{card_id}: {fid}.default == {EXPECTED}",
                      f"实测 {default}")
    check(found > 0, "至少找到一张带判据字段的卡",
          f"找到 {found} 个字段")


# --------------------------------------------------------------------------- #
# 4) 行为：every_n=30 会架空 gscore 门                                      #
# --------------------------------------------------------------------------- #
def _middle_band_scene():
    """构造 A/B 两帧：gscore(A,B) 落在 [thr/2, thr) 的"变了但没到阈值"带。"""
    import numpy as np

    from app.core.video.frame_judge import FrameJudge

    thr = 10.0
    yy, xx = np.mgrid[0:90, 0:160]
    base = (120 + 40 * np.sin(xx / 23.0) * np.cos(yy / 17.0)).astype(np.uint8)
    for size, amp in ((10, 40), (12, 40), (8, 120), (12, 20), (14, 20)):
        mod = base.copy()
        mod[40:40 + size, 70:70 + size] = np.clip(
            mod[40:40 + size, 70:70 + size].astype(int) + amp, 0, 255
        ).astype(np.uint8)
        j = FrameJudge(mode="gscore", every_n=1)
        j.observe(base, 0)
        j.observe(mod, 1)
        s_ab = j.current_score
        j2 = FrameJudge(mode="gscore", every_n=1)
        j2.observe(mod, 0)
        j2.observe(base, 1)
        if thr / 2 <= s_ab < thr and thr / 2 <= j2.current_score < thr:
            return base, mod, s_ab
    return None, None, None


def check_behavior() -> None:
    print("\n[4] 行为：every_n=30 会让 gscore 门失效（多路轮转形态）")
    from app.core.video.frame_judge import FrameJudge

    a, b, score = _middle_band_scene()
    if a is None:
        check(False, "构造出中间带素材（thr/2 <= score < thr）")
        return
    notes.append(f"素材 gscore(A,B)={score:.2f}（中间带 5.0 ~ 10.0）")

    def sim(every_n: int, turns: int = 20, frames_per_turn: int = 47) -> int:
        """多路轮转：每轮转之间该路解了 frames_per_turn 帧。"""
        j = FrameJudge(mode="gscore", every_n=every_n, threshold=10.0)
        taken = 0
        for t in range(turns):
            if j.observe(a if t % 2 == 0 else b, t * frames_per_turn + 1):
                taken += 1
        return taken

    t30, t600 = sim(30), sim(600)
    check(t30 == 20,
          "every_n=30 → 兜底恒成立，20/20 轮全放行（= 门被架空）",
          f"实测 {t30}/20")
    check(t600 < 20,
          f"every_n=600 → 门真的挡得住（< 20/20 轮放行）",
          f"实测 {t600}/20")


def main() -> int:
    print("=" * 66)
    print("判据帧间隔默认值一致性 + 行为回归")
    print("=" * 66)
    check_backend()
    check_frontend()
    check_wizards()
    check_behavior()
    for n in notes:
        print(f"\nnote: {n}")
    if fails:
        print("\n❌ 失败：")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("\n✅ 帧间隔默认值三处一致（600）；every_n=30 架空判据门的行为已钉住")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
