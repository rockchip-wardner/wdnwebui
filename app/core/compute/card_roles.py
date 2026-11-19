"""卡位策略 —— 「谁跟谁、谁独占、怎么挑剩下的卡」。

为什么要有这个模块
------------------
「几块卡跑几个模型」这件事有**唯一正确答案**，但它会出现在**三个**地方：

* 翻译模型起在哪张卡 —— ``app.core.translate.npu_manager``；
* 发声模型起在哪张卡 —— 算力页 / ``POST /api/compute/instances``；
* 开机自启恢复出来的布局 —— ``compute.autostart`` + boot 裁剪。

三处各写一份就会漂（改了 A 忘了 B ⇒ 重启后布局变了）。⇒ 抽成**一个
纯策略模块**，三处都问它。

**策略是纯函数** ⇒ 给同样的输入必然得到同样的输出 ⇒ 这正是
「**重启后布局不变**」的实现基础：重启前后输入相同（同一批模型、
同一批卡、同样的显存占用口径），输出必然相同。

两条链
------
* **感知链**（perception）：``qwen3-asr`` → ``hy-mt-1-5``
  （识别 → 翻译）。两者都是「读完一句出结果」的短推理，**不重叠**：
  ASR 的端点判定发生在**收尾推理之后** ⇒ 翻译那一拍正好落在 ASR 的
  空档里。真机实测（同卡 vs 分卡，各 10+ 轮）：

  ==============  =================  ================
  布局             ASR 轮间隔中位      端点后第一轮
  ==============  =================  ================
  ASR 卡0 + MT 卡0  1.21 s             1.68 s
  ASR 卡0 + MT 卡1  1.22 s             1.68 s
  ==============  =================  ================

  ⇒ **争用 ≈ 0** ⇒ MT **跟随 ASR 所在的卡**。

  ⚠️ 是「跟随」而不是「写死卡号 0」：ASR 在哪张卡，MT 就跟到哪张；
  ASR 没跑时才退化成「挑一张放得下的卡」。

* **发声链**（speech）：``qwen3-tts``
  **流式生成、边生成边播**（``cards/tts/service.py`` 的 ``AplayWriter``
  持续把 PCM 窗口喂给 ``aplay``）⇒ 播报那几秒 NPU 一直忙。
  与 ASR 同卡 ⇒ 识别轮间隔被顶到 > 1 s ⇒ 客户端 2 块的上行队列
  溢出 ⇒ 丢块 + 「有音频丢失」。⇒ **TTS 独占一张卡**
  （挑一张**没被感知链占用**且放得下的卡）。

  ⇒ 两卡板的标准排法：**卡0 = ASR + MT（感知链）**，
  **卡1 = TTS（发声链）**。

⚠️ 本模块**不碰 registry 的锁、不改任何状态** —— 它只回答「该放哪」。
真正的启停在各自的地方做。
"""
from __future__ import annotations

import logging
from typing import Dict, Iterable, List, Optional, Sequence

LOG = logging.getLogger("core.compute.card_roles")

#: 一张 EP 协卡的 NPU 显存（MB）。RK1828 协卡实测 5120 MB。
#:
#: ⚠️ **实测值**，不是型号常量 —— 换板子要重测。与
#: ``app.core.translate.npu_manager.CARD_MEMORY_MB`` 是同一个数
#: （两处必须一致）。
CARD_MEMORY_MB = 5120

#: 感知链 —— 跟卡跑（识别 + 翻译）
ROLE_PERCEPTION = "perception"
#: 发声链 —— 独占一张卡（流式生成 + 播报）
ROLE_SPEECH = "speech"
#: 其余模型 —— 不参与策略，按「挑一张放得下的」处理
ROLE_STANDALONE = "standalone"

#: 模型 → 角色。
#:
#: ⚠️ 这里写的是 **manifest 的 id**（``workers/<model_id>.json`` 的文件名
#: 主干），不是卡 id。
MODEL_ROLES: Dict[str, str] = {
    "qwen3-asr": ROLE_PERCEPTION,
    "hy-mt-1-5": ROLE_PERCEPTION,
    "qwen3-tts": ROLE_SPEECH,
}

#: 感知链的**锚点** —— 链上其它模型跟随它所在的卡。
PERCEPTION_ANCHOR = "qwen3-asr"

#: 模型 id → 人话名字（错误信息里用；不用 id 报错用户看不懂）。
MODEL_LABELS: Dict[str, str] = {
    "qwen3-asr": "识别模型",
    "hy-mt-1-5": "翻译模型",
    "qwen3-tts": "语音合成模型",
}


def label_of(model_id: str) -> str:
    """模型的人话名字。"""
    return MODEL_LABELS.get(model_id, model_id)


# --------------------------------------------------------------------------- #
# 纯逻辑（不碰 registry）
# --------------------------------------------------------------------------- #
def _presets(presets: Optional[dict] = None) -> dict:
    """模型 preset 表（默认取宿主 registry 的 ``MODEL_PRESETS``）。

    ⚠️ **延迟导入** —— ``app.core.compute.registry`` 在模块级 import 本
    模块，反向 import 会成环。
    """
    if presets is not None:
        return presets
    from app.core.compute.registry import MODEL_PRESETS
    return MODEL_PRESETS


def role_of(model_id: str) -> str:
    """这个模型属于哪条链（见 :data:`MODEL_ROLES`）。"""
    return MODEL_ROLES.get(model_id, ROLE_STANDALONE)


def perception_models() -> List[str]:
    """感知链上的模型 id（顺序 = :data:`MODEL_ROLES` 的声明顺序）。"""
    return [m for m, r in MODEL_ROLES.items() if r == ROLE_PERCEPTION]


def can_share_card(model_a: str, model_b: str) -> bool:
    """这两个模型允许**共卡**吗。

    只有**感知链内部**允许（ASR + 翻译）—— 真机实测 1600 MB + 2200 MB
    = 3800 MB ≤ 5120 MB，**同卡稳定共卡**（ASR 轮间隔中位 1.21 s，
    与分卡的 1.22 s 无差别；端点后第一轮两边都是 1.68 s）。

    其余组合维持「**一卡一模型**」（AGENTS.md「一卡一模型是不变量」）：
    同卡叠两个大模型会 rc=-6，没有实测证据就不放开。

    ⚠️ 显存够不够是**另一道**闸（:func:`fits`）—— 这里只回答「角色上
    允不允许共卡」。
    """
    if not model_a or not model_b or model_a == model_b:
        return False
    return (role_of(model_a) == ROLE_PERCEPTION
            and role_of(model_b) == ROLE_PERCEPTION)


def memory_of(model_id: str, presets: Optional[dict] = None) -> int:
    """这个模型**一份实例**占多少显存（MB）。

    ⚠️ **未知模型按「占满整张卡」算** —— 保守：宁可少起一个，也别让
    两个体量未知的模型挤在同一张卡上撞 rc=-6。

    取值来自 manifest 的 ``memory_estimate_mb``（算力页显示的同一个数）。
    """
    info = (_presets(presets).get(model_id) or {})
    mem = int(info.get("memory_estimate_mb") or 0)
    return mem if mem > 0 else CARD_MEMORY_MB


def fits(used_mb: int, need_mb: int,
         card_mb: int = CARD_MEMORY_MB) -> bool:
    """这张卡还放得下 ``need_mb`` 吗。"""
    return int(used_mb) + int(need_mb) <= int(card_mb)


def card_share(model_id: str, cards: Sequence[int],
               presets: Optional[dict] = None) -> Dict[int, int]:
    """一份实例摊到它占的**每一张**卡上的显存（MB）。

    多卡模型（分段式 9B）的 ``memory_estimate_mb`` 是**几张卡加起来**
    的 ⇒ 平摊，余数给第一张 —— 与
    ``app.core.compute.registry._local_memory_usage`` 同一口径。
    """
    if not cards:
        return {}
    total = memory_of(model_id, presets)
    share, rest = divmod(total, len(cards))
    out: Dict[int, int] = {}
    for i, c in enumerate(cards):
        out[int(c)] = share + (rest if i == 0 else 0)
    return out


# --------------------------------------------------------------------------- #
# registry 感知（只读）
# --------------------------------------------------------------------------- #
def _cards(reg) -> List[int]:
    """本机能承载 RKNN 模型的卡号（升序）。"""
    if reg is None:
        return []
    try:
        return sorted(int(c) for c in reg.model_cards())
    except Exception:  # noqa: BLE001
        return []


def _usage(reg) -> Dict[int, int]:
    """本板每张卡的显存占用（只算本机实例）。"""
    if reg is None:
        return {}
    try:
        per, _total, _remote = reg.local_memory_usage()
    except Exception:  # noqa: BLE001
        return {}
    return {int(k): int(v) for k, v in (per or {}).items()}


def perception_anchor_card(reg) -> Optional[int]:
    """感知链锚点（ASR）当前在本板的哪张卡；没跑则 ``None``。"""
    if reg is None:
        return None
    try:
        cards = reg.local_model_cards(PERCEPTION_ANCHOR)
    except Exception:  # noqa: BLE001
        return None
    return int(cards[0]) if cards else None


def current_layout(reg) -> Dict[str, List[int]]:
    """本机各模型**当前**跑在哪些卡上（只算本机实例）。"""
    out: Dict[str, List[int]] = {}
    if reg is None:
        return out
    for model_id in sorted(_presets().keys()):
        try:
            cards = [int(c) for c in reg.local_model_cards(model_id)]
        except Exception:  # noqa: BLE001
            cards = []
        if cards:
            out[model_id] = cards
    return out


# --------------------------------------------------------------------------- #
# 布局规划（纯读）
# --------------------------------------------------------------------------- #
def _first_fitting(cards: Sequence[int], usage: Dict[int, int],
                    need_mb: int, model_id: str) -> int:
    """按卡号顺序挑第一张放得下的卡。"""
    for c in cards:
        if fits(usage.get(int(c), 0), need_mb):
            return int(c)
    raise RuntimeError(
        "没有一张协卡放得下%s（需 %d MB，一张卡 %d MB）"
        % (label_of(model_id), need_mb, CARD_MEMORY_MB))


def _running_cards(reg, model_id: str) -> List[int]:
    """某个模型**当前**在本机跑在哪些卡上（空 = 没跑）。"""
    if reg is None:
        return []
    try:
        return [int(c) for c in reg.local_model_cards(model_id)]
    except Exception:  # noqa: BLE001
        return []


def plan_layout(reg, model_ids: Optional[Sequence[str]] = None,
                presets: Optional[dict] = None) -> dict:
    """算出一份**建议布局**（纯读，不改任何状态、不启停任何模型）。

    算法（顺序很重要）：

    1. **感知链先落** —— 它的目标卡 = 锚点（ASR）现在所在的卡；ASR 还没跑
       时，取「ASR + 翻译**加起来**放得下」的第一张卡（⇒ 给整条链留够
       位置，而不是让翻译把卡占了、ASR 挤不下）。
    2. **发声链后落** —— 挑一张**没被感知链占用**、且放得下的卡。发声链
       独占：播报那几秒 NPU 一直忙，与识别共卡会挤掉识别 ⇒ 客户端上行
       队列溢出 ⇒ 丢块 + 「有音频丢失」。
    3. 其余模型 → 挑一张放得下的卡（与旧行为一致）。

    ⚠️ **已经在跑的模型不搬家** —— 计划里它的卡位就是它现在的卡，显存也
    不重复计入（``usage`` 起点就是当前占用）。

    返回::

        {
          "cards": [0, 1],                  # 本机算力卡位
          "per_card_memory_mb": {0: 1600},  # 规划后的占用（含计划新增）
          "roles": {"qwen3-asr": "perception", ...},
          "current": {"qwen3-asr": [0]},    # 现在实际跑在哪
          "plan": {"qwen3-asr": 0, ...},    # 建议放哪
          "notes": ["..."],                 # 人类可读的解释
        }
    """
    cards = _cards(reg)
    presets = presets if presets is not None else _presets()

    if model_ids is None:
        model_ids = [m for m in sorted(presets.keys())
                     if role_of(m) in (ROLE_PERCEPTION, ROLE_SPEECH)]
    model_ids = [m for m in model_ids if m in presets]

    usage = _usage(reg)          # 当前占用（已含在跑的实例）
    running = {m: _running_cards(reg, m) for m in model_ids}
    anchor = perception_anchor_card(reg)
    plan: Dict[str, int] = {}
    notes: List[str] = []

    # ---- 1. 感知链的目标卡 ------------------------------------------- #
    chain_need = sum(memory_of(m, presets) for m in perception_models())
    target = anchor
    if target is None:
        for c in cards:
            if fits(usage.get(c, 0), chain_need):
                target = c
                break
        if target is None:
            notes.append("没有一张卡放得下整条感知链（需要 %d MB）" % chain_need)

    # ---- 2. 感知链落位 ------------------------------------------------ #
    for m in [m for m in model_ids if role_of(m) == ROLE_PERCEPTION]:
        need = memory_of(m, presets)
        cur = running.get(m) or []
        if cur:
            plan[m] = cur[0]           # 已经在跑 ⇒ 就在那儿，不搬家
            continue
        card = None
        if target is not None and fits(usage.get(target, 0), need):
            card = target
        if card is None:
            try:
                card = _first_fitting(cards, usage, need, m)
            except RuntimeError as exc:
                notes.append("%s：%s" % (label_of(m), exc))
                continue
        plan[m] = card
        usage[card] = usage.get(card, 0) + need

    # ---- 3. 发声链落位（躲开感知链占用的卡） -------------------------- #
    blocked = {plan[m] for m in perception_models() if m in plan}
    for m in [m for m in model_ids if role_of(m) == ROLE_SPEECH]:
        need = memory_of(m, presets)
        cur = running.get(m) or []
        if cur:
            plan[m] = cur[0]
            continue
        free = [c for c in cards if c not in blocked]
        if not free:
            notes.append(
                "没有空闲卡可独占给%s（感知链占了卡 %s）—— 退回「所有卡里挑"
                "一张放得下的」，同卡共跑会挤占识别"
                % (label_of(m), sorted(blocked)))
            free = cards
        try:
            card = _first_fitting(free, usage, need, m)
        except RuntimeError as exc:
            notes.append("%s：%s" % (label_of(m), exc))
            continue
        plan[m] = card
        usage[card] = usage.get(card, 0) + need

    if anchor is not None:
        notes.append("感知链锚点%s在卡 #%d，翻译模型跟随它"
                     % (label_of(PERCEPTION_ANCHOR), anchor))
    else:
        notes.append("%s还没跑 —— 感知链会自动挑一张放得下的卡"
                     % label_of(PERCEPTION_ANCHOR))

    return {
        "cards": cards,
        "per_card_memory_mb": dict(sorted(usage.items())),
        "roles": {m: role_of(m) for m in model_ids},
        "current": current_layout(reg),
        "plan": plan,
        "notes": notes,
    }


def pick_card(reg, model_id: str, *,
              avoid_cards: Iterable[int] = ()) -> int:
    """按策略给 ``model_id`` 挑一张卡。

    * **感知链**（ASR / 翻译）→ 跟随锚点（ASR）所在的卡；锚点没跑时取
      「整条链放得下」的那张卡；再放不下就退化成「挑一张放得下的卡」。
    * **发声链**（TTS）→ 挑一张**没被感知链占用**、且放得下的卡
      （发声链独占 —— 播报那几秒 NPU 一直忙，与识别共卡会挤掉识别）。
      实在没有空卡才退回「所有卡里挑一张放得下的」，并**记一条警告**。
    * **其余模型** → 挑一张放得下的卡（与旧行为一致）。

    ⚠️ 计划里会**连带**把感知链一起算（不然不知道该躲哪张卡），但那只
    是**算**，不会真的启停任何模型。

    抛 :class:`RuntimeError` 表示挑不出来（没有卡 / 都放不下）。
    """
    if not _cards(reg):
        raise RuntimeError("本机没有可用的 NPU 协卡")
    models = [model_id]
    role = role_of(model_id)
    if role == ROLE_SPEECH:
        # 发声链要知道感知链会占哪张卡 —— 一起算
        models += [m for m in perception_models() if m not in models]
    elif role == ROLE_PERCEPTION:
        models += [m for m in perception_models() if m not in models]
    plan = plan_layout(reg, model_ids=models)
    card = plan.get("plan", {}).get(model_id)
    if card is None:
        # 计划里放不下 ⇒ 给一条人话错误
        reason = next((n for n in plan.get("notes", [])
                       if label_of(model_id) in n), "没有合适的卡位")
        raise RuntimeError(reason)
    extra = {int(c) for c in avoid_cards}
    if extra and card in extra:
        # 调用方点名要避开这张卡 → 退化成「所有卡里挑一张放得下的」
        for c in _cards(reg):
            if c not in extra and fits(_usage(reg).get(c, 0),
                                       memory_of(model_id)):
                return c
    return card
