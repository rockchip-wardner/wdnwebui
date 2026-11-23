#!/usr/bin/env python3
"""回归：同传的**放行器**（``Gate``）与播放头 / seek 处理。

契约
====
1. ``now`` 模式 ⇒ 直接放行（不管有没有播放头）；
2. ``follow`` 模式 ⇒ 等播放头到达 ``src_start − FOLLOW_LEAD_SEC``；
3. 还没有播放头 ⇒ 不放行；
4. 用户暂停（``playing=false``）⇒ 不放行；
5. 播放头**回退**超过 ``SEEK_BACK_TOLERANCE`` ⇒ 判定为 seek ⇒
   跳过的待播句被清掉（计 ``skipped_seek``）；
6. 实时流（没有时间轴）⇒ 不能跟播；
7. **卡住兜底**：媒体报暂停 + 播放头从没走过 + 卡够 ``FOLLOW_STALL_SEC``
   ⇒ 放行（别把句子无限期压着）；
8. **一次播放头都没上报过** ⇒ 同样按「卡住」处理；
9. **会话切换清记账**（``reset()``）—— 否则上一轮「媒体播过」会让这一轮
   的兜底**永不触发**（用户报「有翻译，但是没有播放」的最后一层）。

跑法::

    .venv/bin/python scripts/verify_interp_gate.py
"""
from __future__ import annotations

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from wdnapps_audio_chat.cards.qwen3_asr.interp import (  # noqa: E402
    FOLLOW_LEAD_SEC,
    FOLLOW_STALL_SEC,
    FOLLOW_STALL_WARN_SEC,
    SEEK_BACK_TOLERANCE,
    Gate,
    InterpSegment,
)

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ok   {name}")
    else:
        _FAIL += 1
        print(f"  FAIL {name} {extra}")


def seg(start: float, end: float, stream_id: str = "file-1#0") -> InterpSegment:
    return InterpSegment(index=1, text="测试", lang="English",
                         stream_id=stream_id, mode="steady",
                         src_start_sec=start, src_end_sec=end,
                         origin="file")


def main() -> int:
    print("== 放行器 ==")

    # 1. now ⇒ 直放
    g = Gate("now")
    check("now 模式直放", g.admit(seg(10.0, 12.0)) is True)

    # 2. follow ⇒ 没有播放头不放行
    g = Gate("follow")
    check("follow + 无播放头 ⇒ 不放行", g.admit(seg(10.0, 12.0)) is False)

    # 3. follow + 播放头没到 ⇒ 不放行
    g.update_playhead("file-1#0", 5.0, True)
    check("follow + 播放头未到 ⇒ 不放行", g.admit(seg(10.0, 12.0)) is False)

    # 4. follow + 播放头到达（提前 1 s）⇒ 放行
    g.update_playhead("file-1#0", 10.0 - FOLLOW_LEAD_SEC + 0.01, True)
    check("follow + 播放头到达 ⇒ 放行", g.admit(seg(10.0, 12.0)) is True)

    # 5. 暂停 ⇒ 不放行
    g.update_playhead("file-1#0", 20.0, False)
    check("暂停 ⇒ 不放行", g.admit(seg(10.0, 12.0)) is False)

    # 6. 实时流（没有时间轴）⇒ 不能跟播
    g2 = Gate("follow")
    g2.update_playhead("live-abc", 100.0, True)
    live_seg = InterpSegment(index=2, text="测试", lang="English",
                             stream_id="live-abc", mode="steady",
                             src_start_sec=-1.0, src_end_sec=-1.0,
                             origin="live")
    check("实时流（无时间轴）⇒ 不放行", g2.admit(live_seg) is False)

    # 7. seek 判定：回退超过容差
    g3 = Gate("follow")
    g3.update_playhead("file-1#0", 30.0, True)
    check("小幅回退（< 容差）不算 seek",
          g3.update_playhead("file-1#0", 30.0 - SEEK_BACK_TOLERANCE + 0.5,
                             True) is False)
    check("大幅回退（> 容差）算 seek",
          g3.update_playhead("file-1#0", 5.0, True) is True)

    # 8. 播放头回退容差的边界（**每次都用新的 gate** —— prev 是上一次的值）
    g4 = Gate("follow")
    g4.update_playhead("s", 100.0, True)
    check("回退正好等于容差 ⇒ 不算 seek",
          g4.update_playhead("s", 100.0 - SEEK_BACK_TOLERANCE, True) is False)
    g5 = Gate("follow")
    g5.update_playhead("s", 100.0, True)
    check("回退超过容差 0.01 ⇒ 算 seek",
          g5.update_playhead("s", 100.0 - SEEK_BACK_TOLERANCE - 0.01,
                             True) is True)

    # 9. 换档位
    g5 = Gate("now")
    g5.set_mode("follow")
    check("切到 follow 后不再直放", g5.admit(seg(10.0, 12.0)) is False)
    g5.set_mode("now")
    check("切回 now 后直放", g5.admit(seg(10.0, 12.0)) is True)

    # ------------------------------------------------------------------ #
    # 10. **卡住兜底**（2026-11-21，用户报「有翻译，但是没有播放」）
    #
    # 根因：浏览器按自动播放策略**拒绝** ``el.play()`` ⇒ 媒体一直暂停 ⇒
    # 播放头永远 0 ⇒ 「跟播放」把**所有**句子**永久压住** ⇒ 译文有、
    # 一声不响、面板也看不出为什么。
    #
    # ⇒ 媒体**报的是暂停** + 播放头**从没真正走过** + 卡住超过
    #    ``FOLLOW_STALL_SEC`` ⇒ **兜底放行**（别把句子无限期压着）。
    # ------------------------------------------------------------------ #
    g6 = Gate("follow")
    g6.update_playhead("file-1#0", 0.0, False)     # 媒体暂停（自动播放被拦）
    check("卡住但未超时 ⇒ 不放行", g6.admit(seg(10.0, 12.0)) is False)
    g6._ph_at = time.time() - (FOLLOW_STALL_SEC + 1)
    check("卡住超时 ⇒ **兜底放行**（不再压住句子）",
          g6.admit(seg(10.0, 12.0)) is True)
    check("兜底计数 +1", g6.stalled_released() == 1,
          f"得到 {g6.stalled_released()}")

    # 11. **用户主动暂停**（媒体**播过**再停）⇒ 不兜底（译文跟着停）
    g7 = Gate("follow")
    g7.update_playhead("file-1#0", 12.0, True)     # 真的播过
    g7.update_playhead("file-1#0", 12.0, False)    # 用户暂停
    g7._ph_at = time.time() - (FOLLOW_STALL_SEC + 1)
    check("用户主动暂停（播过再停）⇒ **不兜底**，译文跟着停",
          g7.admit(seg(10.0, 12.0)) is False)
    check("主动暂停不计兜底", g7.stalled_released() == 0,
          f"得到 {g7.stalled_released()}")

    # 12. 媒体**声称在播**，但播放头**完全不动**（卡在缓冲 / 解码不动）
    #     ⇒ **兜底放行**（2026-11-22，用户报「只播放了第一句」的现场）
    #
    # ⚠️ 用户那次浏览器**只上报过 1 次**播放头：(ph=0, playing=true)
    # ⇒ 媒体声称在播，但 currentTime 一动不动 ⇒
    #   * 「播放头到达就放行」永远不成立；
    #   * 旧的兜底要求「媒体报的是暂停」⇒ **也不触发**；
    #   * ⇒ 只有 ``src_start ≤ 1 s`` 的那一句被放行 ⇒ **只听到第一句**。
    #
    # ⇒ 判据改成「**播放头冻住**」（不管媒体怎么说）。
    g8 = Gate("follow")
    g8.update_playhead("file-1#0", 0.0, True)      # 声称在播
    check("声称在播但播放头没动 ⇒ 刚卡住时不放行",
          g8.admit(seg(10.0, 12.0)) is False)
    g8._ph_at = time.time() - (FOLLOW_STALL_SEC + 1)
    check("**声称在播但播放头冻住 ⇒ 兜底放行**（媒体卡死了）",
          g8.admit(seg(10.0, 12.0)) is True)
    check("（同上）兜底计数 +1", g8.stalled_released() == 1,
          f"得到 {g8.stalled_released()}")

    # 12b. 媒体在播、播放头**在动**（只是慢）⇒ 正常按播放头放行，不兜底
    g8b = Gate("follow")
    g8b.update_playhead("file-1#0", 0.0, True)
    g8b.update_playhead("file-1#0", 0.4, True)     # 播放头在动 ⇒ moved=True
    check("播放头在动 ⇒ 不算卡住（不兜底）",
          g8b.admit(seg(10.0, 12.0)) is False)
    g8b._ph_at = time.time() - (FOLLOW_STALL_SEC + 1)
    check("播放头在动 ⇒ 仍然不兜底（等播放头）",
          g8b.admit(seg(10.0, 12.0)) is False)

    # 13. ``stalled`` 标志（前端据此提示「媒体没在放」）
    g9 = Gate("follow")
    g9.update_playhead("file-1#0", 0.0, False)
    check("刚卡住 ⇒ 还没到告警阈值 ⇒ stalled=False",
          g9.stalled("file-1#0") is False)
    g9._ph_at = time.time() - (FOLLOW_STALL_WARN_SEC + 1)
    check("卡过告警阈值 ⇒ stalled=True（前端会提示）",
          g9.stalled("file-1#0") is True)
    g9.set_mode("now")
    check("now 模式 ⇒ 不算卡住", g9.stalled("file-1#0") is False)

    # 14. **播放头的 stream_id 对不上 ⇒ 也要能用**（2026-11-21，用户报
    #     「有翻译，但是没有播放」的**根因**）
    #
    # 前端上报播放头时带的 ``stream_id`` **可能是空的**（``_interp.streamId``
    # 从来没被赋值 ⇒ 一直是 ``""``）⇒ 播放头存进了 ``""`` 这个键 ⇒
    # 句子（``file-xxx#0``）**配不上** ⇒ 「跟播放」把**所有**句子永久压住
    # ⇒ 译文有、**一声不响**。
    #
    # ⇒ 放行器必须**兜底**：拿「最近一次上报的播放头」。
    g10 = Gate("follow")
    g10.update_playhead("", 12.0, True)          # 前端报了空 id
    check("播放头 stream_id 对不上 ⇒ **仍然放行**（兜底回退）",
          g10.admit(seg(10.0, 12.0)) is True)
    g11 = Gate("follow")
    g11.update_playhead("", 5.0, True)           # 空 id + 播放头还没到
    check("播放头对不上但**确实没到** ⇒ 仍不放行",
          g11.admit(seg(10.0, 12.0)) is False)

    # ------------------------------------------------------------------ #
    # 15. **一次播放头都没上报过**（前端根本没在报）
    #
    # ⇒ 媒体没在放（页面没报 / 元素不在）⇒ 之前 ``playing_now is None``
    #    ⇒ 兜底条件不成立 ⇒ 句子被**无限期**压住 ⇒ 「有翻译没声音」。
    # ⇒ 现在按「不是 True 就是在放」判定 ⇒ 超时后兜底放行。
    # ------------------------------------------------------------------ #
    g12 = Gate("follow")
    check("没上报过播放头 ⇒ 刚起时不算卡住",
          g12.stalled("file-1#0") is False)
    check("没上报过播放头 ⇒ 刚起时不放行",
          g12.admit(seg(10.0, 12.0)) is False)
    g12._armed_at = time.time() - (FOLLOW_STALL_WARN_SEC + 1)
    check("没上报过播放头 + 卡过告警阈值 ⇒ stalled=True（面板会提示）",
          g12.stalled("file-1#0") is True)
    g12._armed_at = time.time() - (FOLLOW_STALL_SEC + 1)
    check("没上报过播放头 + 卡超时 ⇒ **兜底放行**",
          g12.admit(seg(10.0, 12.0)) is True)

    # ------------------------------------------------------------------ #
    # 16. **会话切换必须清记账**（2026-11-21，用户报「有翻译，但是没有播放」）
    #
    # ``_ph_moved``（播放头**曾经真的走过**）是「自动播放被拦」与「用户
    # 主动暂停」的分界线 ⇒ 它**跨会话残留**的话：
    #
    # * 上一轮媒体播过 ⇒ 这一轮「自动播放被拦」（媒体从没播过）⇒ 兜底
    #   **永远不触发** ⇒ 句子被永久压住 ⇒ 有翻译、一声不响；
    # * 反过来更糟：上一轮从没播过 ⇒ 这一轮用户**主动暂停** ⇒ 兜底误触发。
    #
    # ⇒ ``Gate.reset()`` 必须把这两样都清掉。
    # ------------------------------------------------------------------ #
    g13 = Gate("follow")
    g13.update_playhead("file-1#0", 12.0, True)     # 真的播过
    g13.update_playhead("file-1#0", 12.0, False)    # 用户暂停
    check("（重置前）播过再停 ⇒ never_played=False",
          g13.never_played() is False)
    g13.reset()
    check("reset 后 ⇒ 播放头记账清空（never_played=True）",
          g13.never_played() is True)
    check("reset 后 ⇒ 兜底计数清零", g13.stalled_released() == 0,
          f"得到 {g13.stalled_released()}")
    g13.update_playhead("file-1#0", 0.0, False)    # 新会话：自动播放被拦
    g13._ph_at = time.time() - (FOLLOW_STALL_SEC + 1)
    check("**reset 后 ⇒ 自动播放被拦 ⇒ 兜底能放行**（不再永久压住）",
          g13.admit(seg(10.0, 12.0)) is True)

    # ------------------------------------------------------------------ #
    # 17. 兜底放行**不会**把「用户主动暂停」误判成「自动播放被拦」
    #     （新会话里媒体播过再停 ⇒ 不兜底）
    # ------------------------------------------------------------------ #
    g14 = Gate("follow")
    g14.update_playhead("file-1#0", 12.0, True)    # 真的播过
    g14.update_playhead("file-1#0", 12.0, False)   # 用户暂停
    g14._ph_at = time.time() - (FOLLOW_STALL_SEC + 1)
    check("新会话里「用户主动暂停」⇒ **不兜底**",
          g14.admit(seg(10.0, 12.0)) is False)
    check("新会话里「用户主动暂停」⇒ never_played=False",
          g14.never_played() is False)

    # ------------------------------------------------------------------ #
    # 18. **源播完了**（媒体 ended）⇒ 剩下的句子**直接放行**
    #
    # ⚠️ **用户报「只播放了第一句」的根因**（实测，2026-11-22）：
    # 媒体播完 ⇒ ``paused=true, ended=true, currentTime=停在时长`` ⇒
    # 前端上报的 ``playing=false`` ⇒ 放行器「播放头到达就放行」那条
    # 判据（要求 ``playing is not False``）**失效**；而兜底放行又要求
    # 「播放头**从没走过**」⇒ 媒体播过 ⇒ 兜底**也不触发**
    # ⇒ ⇒ **媒体播完之后才识别出来的句子被永久压住**
    # ⇒ 文件源天然如此（源 15 s，ASR+MT 要十几秒才出齐）
    # ⇒ ⇒ 用户听到的**只有第一句**。
    #
    # ⇒ 修法：前端把「媒体 ended / 加载失败」一起上报 ⇒ 放行器知道
    #    「源不会再往前走」⇒ 剩下的译文**直接补播**。
    # ------------------------------------------------------------------ #
    g15 = Gate("follow")
    g15.update_playhead("file-1#0", 3.0, True)      # 媒体在播
    check("源还在播、播放头没到 ⇒ 不放行",
          g15.admit(seg(10.0, 12.0)) is False)
    g15.update_playhead("file-1#0", 15.05, False, ended=True)   # 播完了
    check("**源播完 ⇒ 剩下的句子直接放行**（不再永久压住）",
          g15.admit(seg(10.0, 12.0)) is True)
    check("source_done=True（源不会再往前走）",
          g15.source_done("file-1#0") is True)
    check("源播完 ⇒ 不算兜底放行（走的是正常通道）",
          g15.stalled_released() == 0,
          f"得到 {g15.stalled_released()}")
    # 媒体加载失败 ⇒ 同样直接放
    g16 = Gate("follow")
    g16.update_playhead("file-1#0", 0.0, False, failed=True)
    check("**媒体加载失败 ⇒ 直接放行**",
          g16.admit(seg(10.0, 12.0)) is True)
    check("媒体加载失败 ⇒ source_done=True",
          g16.source_done("file-1#0") is True)
    # 重新播放 ⇒ ended 标志要能清掉（否则会一直当成「源结束了」）
    g16.update_playhead("file-1#0", 0.0, True, ended=False)
    check("重新播放 ⇒ source_done 回到 False",
          g16.source_done("file-1#0") is False)
    check("重新播放且播放头没到 ⇒ 又开始等了",
          g16.admit(seg(10.0, 12.0)) is False)

    print(f"\n通过 {_PASS} / 失败 {_FAIL}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
