"""``ChangeDetector`` —— 判帧能力的**向后兼容外壳**，已由
:class:`app.core.video.frame_judge.FrameJudge` 取代。

保留它只为两件事：

1. VAS（``app.apps.video_analysis_search``）历史上用的就是这套 API
   （``sample_mode`` / ``change_threshold`` / ``should_sample``），改调用点
   要动缓存落库 / 历史恢复两条链路，风险不值当；
2. ``scripts/verify_vas_change_detector.py`` 直接测这个类。

**新代码一律用 :class:`FrameJudge`。** 判帧器的唯一实现现在只有一份
（``frame_judge.py``），本模块只是把旧的 manual/auto 两态翻译过去。

两态到多算法的映射
------------------
``sample_mode`` 是二值的：``manual`` = 固定间隔，``auto`` = 按画面变化。
而 ``FrameJudge`` 的 ``mode`` 除了 ``manual`` 之外还能是任一度量名。
历史上 ``auto`` **硬编码走 z_mad**（``ChangeDetector`` 里写死的
``FrameDiffer(metric="z_mad")``），所以这里保持 ``auto → z_mad`` 的映射，
VAS 的采样行为逐位不变；其余度量名原样透传，供新 UI 使用。
"""

from __future__ import annotations

import logging
from typing import List, Optional

from app.core.video.frame_judge import (
    EXPOSED_METRICS,
    MODE_MANUAL,
    LEGACY_MODE_ALIASES,
    FrameJudge,
    metrics,
)

_LOG = logging.getLogger("vas_change_detector")

#: ``auto`` 在新判帧器里对应哪个度量 —— 历史上硬编码 z_mad，z_mad 下架后
#: 指向当前推荐度量（``frame_judge.LEGACY_MODE_ALIASES`` 是同一张表，两边
#: 都取 :data:`EXPOSED_METRICS` 的表头，不会各认一份漂移）。
#: v0.21 起表头是 ``gscore``（conc 对大范围变化结构性失明，已降为对比项）。
#: v0.21 起 SAMPLE_MODE_AUTO **同时是默认 mode** —— 用户进向导看到的
#: 判据算法 = gscore（gscore 是唯一推荐项，conc 是待下架对比项）。
#: **必须**在 import 后**立刻**定义 —— 模块级赋值顺序敏感，下面
#: ``DEFAULT_CHANGE_THRESHOLD`` / ``ChangeDetector.__init__`` 都依赖它。
SAMPLE_MODE_AUTO = EXPOSED_METRICS[0]

# 旧常量名保留：VAS / 缓存元数据 / 前端仍在用。
DEFAULT_CHANGE_THRESHOLD = FrameJudge(mode=SAMPLE_MODE_AUTO).threshold
SAMPLE_MODE_MANUAL = MODE_MANUAL

#: 调用方传进来的**字面量**既可能是 "auto"（旧 API 语义），也可能是某个
#: 已下架的度量名（"z_mad"）。两者都要认 —— 只认前者的话，存量配置里的
#: z_mad 会被当成未知模式静默回落到 manual，首帧也因此被 ``pos > 0`` 滤掉，
#: 表现为"静止画面一帧都不送检"。
_AUTO_ALIASES = frozenset(LEGACY_MODE_ALIASES) | {SAMPLE_MODE_AUTO}


class ChangeDetector(FrameJudge):
    """``FrameJudge`` 的 manual/auto 视图。**没有额外行为**。"""

    def __init__(self,
                 sample_mode: str = SAMPLE_MODE_AUTO,
                 every_n: int = 60,
                 change_threshold: Optional[float] = None,
                 long_side: int = 160) -> None:
        mode = str(sample_mode or SAMPLE_MODE_MANUAL).strip().lower()
        if mode in _AUTO_ALIASES:
            mode = SAMPLE_MODE_AUTO
            # 自动分支不继承调用方阈值 —— 存量配置里的旧阈值是按已下架度量
            # 的刻度标的，照搬会让几乎每帧都触发。交给 FrameJudge 用默认值。
            change_threshold = None
        elif mode != MODE_MANUAL and mode not in set(metrics()):
            _LOG.warning("change_detector: 未知 sample_mode %r，回落 manual", mode)
            mode = SAMPLE_MODE_MANUAL
        super().__init__(mode=mode, every_n=every_n,
                         threshold=change_threshold, long_side=long_side)

    # ------------------------------------------------------------------ #
    @property
    def sample_mode(self) -> str:
        """报**真实模式**（``manual`` 或度量名），不做二值折叠。

        早期实现把它折叠成 ``manual`` / ``auto`` 两个值，理由是"老 UI 只有
        这两个选项"。但它同时被 ``analysis_sample_key`` 拿去算缓存指纹 ——
        折叠后 ``sad`` 与 ``ncc`` 都变成 ``auto``，两者选出的帧集不同却共用
        一条缓存（串味）。老 UI 已经换成多算法下拉，不再需要这层折叠。
        """
        return self.mode

    def should_sample(self, frame_bgr, frame_pos: int) -> bool:
        """与 :meth:`FrameJudge.observe` 同义（保持旧调用点不变）。"""
        return self.observe(frame_bgr, frame_pos)

    def stats(self) -> dict:
        return self.snapshot()


def available_metrics() -> List[str]:
    """可用的判帧算法名（含 manual 之外的度量）。"""
    return metrics()