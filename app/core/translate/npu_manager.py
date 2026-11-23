"""本板 NPU 翻译模型的**实例生命周期**（起 / 停 / 选 session）。

为什么单独一个模块
------------------
「翻译」在本仓有**两条互不相干**的用法：

1. **界面翻译**（``app.core.translate`` 的 provider 链）—— 批量、带缓存、
   带冷却、带外网兜底，追求的是「一条都不漏译」；
2. **流式同传**（实时字幕翻译）—— 一句一译、**不能重试**、追求的是
   「这句 0.3 秒内出译文」。

两者对**模型实例**的要求是一样的（都要本板跑着 ``swcap_translate``），
但对**推理方式**的要求几乎相反。⇒ **实例生命周期**抽到这里共享，
**推理方式**各走各的。

⚠️ 本模块**不碰** ``app.core.translate`` 的 provider 链 —— 那是界面翻译
的私事（它会把 ``npu`` 这一路接进链里、维护冷却状态、还会回退到外网
三家）。流式同传要的是**直连 NPU**：一次推理、一个句子、失败就降级。

谁在用
------
* ``wdnapps_audio_chat.cards.qwen3_asr`` —— 流式同传的译文（第一期）；
* ``wdnapps_audio_chat.cards.web_translate`` —— 目前仍用自己的
  ``WebTranslateManager``（它额外要把 npu 接进 provider 链）。将来可以
  切到这里共用启停逻辑，但**不要**现在就动它 —— 那张卡已经上线并在用。

为什么**不**放在插件里
----------------------
插件之间禁止互相 import（``cards/*/__init__.py`` 里明写 "No cross-card
imports."）。若把这段逻辑写在 ASR 卡里，网页翻译卡就用不上；写在网页
翻译卡里，ASR 卡就得跨卡 import。⇒ 放**宿主** ``app.core.*``，两边都能用。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

LOG = logging.getLogger("core.translate.npu_manager")

#: 一张 EP 协卡的 NPU 显存（MB）。RK1828 协卡实测 5120 MB。
#:
#: ⚠️ 这是**实测值**，不是型号常量 —— 换板子要重测。网页翻译卡里有一份
#: 同样的常量（``cards/web_translate/service.py::CARD_MEMORY_MB``），
#: 两处必须一致。
CARD_MEMORY_MB = 5120


class NpuTranslateInstances:
    """本板 NPU 翻译模型（HY-MT1.5）的实例管理器。

    线程安全。所有方法都是**慢操作**（起模型要等 30 s 加载），调用方
    应当放进线程里跑，**不要**直接写在 async 端点的事件循环线程上。
    """

    #: 首选翻译模型（与 ``tools/worker_manifests/hy-mt-1-5.json`` 一致）。
    MODEL_ID = "hy-mt-1-5"

    #: 提供翻译能力的 capability（wdnai 的封闭词表）。
    CAPABILITY = "swcap_translate"

    #: 一份翻译模型的显存估算（manifest 的 ``memory_estimate_mb``）。
    MEMORY_MB = 2200

    #: 起实例后等它 ready 的上限（模型加载约 30 s，留一倍余量）。
    LOAD_TIMEOUT_S = 120.0
    LOAD_POLL_S = 2.0

    def __init__(self) -> None:
        self._lock = threading.RLock()
        #: 启停是**慢操作** —— 两个请求同时进来会把卡搞乱
        #: （两个 init 线程抢同一份 NPU 内存 ⇒ rc=-6）。
        self._op_lock = threading.Lock()
        #: session 轮转游标（并发单位是**实例**，见 :meth:`pick_session`）。
        self._rr = 0
        #: 本管理器专用的 provider 实例（懒创建）。
        self._provider_inst = None

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    def status(self) -> Dict[str, Any]:
        """当前状态快照（界面每次刷新拉一次）。

        ⚠️ 走 ``peek_registry()`` —— **不**因为看一眼状态就把整个算力
        注册表拉起来（DDS hub + 对端发现 + 硬件探测，慢且有副作用）。
        """
        reg = self._peek_registry()
        instances = self._local_instances(reg)
        running = [i for i in instances if i.get("status") == "running"]
        cards = []
        if reg is not None:
            try:
                cards = list(reg.model_cards())
            except Exception:  # noqa: BLE001
                cards = []
        per_card, total, _remote = self._usage(reg)
        free_cards = [c for c in cards
                      if per_card.get(c, 0) + self.MEMORY_MB <= CARD_MEMORY_MB]
        return {
            "model_id": self.MODEL_ID,
            "capability": self.CAPABILITY,
            "installed": self._installed(),
            "registry_up": reg is not None,
            "running": bool(running),
            "count": len(running),
            "expected": 1,
            "instances": [
                {"npu_index": i.get("npu_index"),
                 "replica": int(i.get("replica") or 0),
                 "status": i.get("status"),
                 "pid": i.get("pid")}
                for i in instances
            ],
            "cards": cards,
            "free_cards": free_cards,
            "used_memory_mb": total,
        }

    # ------------------------------------------------------------------ #
    # 启停
    # ------------------------------------------------------------------ #
    def ensure(self, npu_index: Optional[int] = None) -> Dict[str, Any]:
        """确保本板有一个**正在跑**的翻译实例；返回状态快照。

        * ``npu_index`` 给了就起在那张卡上；不给就按**卡位策略**挑 ——
          翻译模型**跟随 ASR 所在的卡**（见
          :mod:`app.core.compute.card_roles`）。
        * 已经有实例在跑 ⇒ 直接复用（不重复 load，也不换卡）。
        * 起的是**本机** worker（``allow_adopt=False``）——
          「每块板子只管自己的算力」，也不要把 peer 的 service 摘掉。

        ⚠️ **慢操作**：要等模型加载完（~30 s）。
        """
        with self._op_lock:
            reg = self._registry()
            if not self._installed():
                raise RuntimeError(
                    "本机没有安装翻译模型 %s（模型目录里找不到）"
                    % self.MODEL_ID)
            # 已经有翻译实例在跑 ⇒ 直接复用（**不换卡**：用户手动摆过的
            # 卡位是有意为之，策略不该把它搬走）。
            running = [i for i in self._local_instances(reg)
                       if i.get("status") == "running"]
            if running and npu_index is None:
                return self.status()
            card = (int(npu_index) if npu_index is not None
                    else self._pick_card(reg))
            bad = reg.npu_card_error(card)
            if bad:
                raise RuntimeError(bad)

            inst = self._find_slot(reg, card)
            if inst and inst.get("status") == "running":
                self._record_autostart(reg, card)
                return self.status()
            if inst and inst.get("status") == "loading":
                # 正在加载 —— 等它，别再起一份（两份抢同一份显存 ⇒ rc=-6）
                if self._wait_ready(reg, card):
                    self._record_autostart(reg, card)
                    return self.status()
                raise RuntimeError("翻译模型加载超时（卡 #%d）" % card)

            with reg.model_op_lock(self.MODEL_ID):
                inst = reg.ensure_worker_for_model(
                    self.MODEL_ID, allow_adopt=False, npu_index=card,
                    eager_load=False, replica=0,
                )
                reg.bind_instance(inst, card)
            if not self._wait_ready(reg, card):
                err = self._find_slot(reg, card) or {}
                raise RuntimeError(
                    "翻译模型启动失败（卡 #%d）：%s"
                    % (card, err.get("error") or "未知原因"))
            self._record_autostart(reg, card)
            return self.status()

    def disable(self) -> Dict[str, Any]:
        """停掉本板**所有**翻译实例（peer 的不管 —— 那是别的板子的算力）。"""
        with self._op_lock:
            reg = self._peek_registry()
            if reg is None:
                return self.status()
            for inst in self._local_instances(reg):
                npu = inst.get("npu_index")
                if not isinstance(npu, int) or npu < 0:
                    npu = None
                replica = int(inst.get("replica") or 0)
                try:
                    with reg.model_op_lock(self.MODEL_ID):
                        reg.stop_instance_local(self.MODEL_ID, npu, replica)
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("npu_manager: unloading the instance failed (card %s, copy %d): %s",
                                npu, replica + 1, exc)
            return self.status()

    # ------------------------------------------------------------------ #
    # session
    # ------------------------------------------------------------------ #
    def sessions(self) -> List[Any]:
        """本板**所有**正在跑的翻译 session（每个实例一个）。

        只取**本机**实例：远端 peer 的翻译结果照样能用，但那是别人的
        板子，不该混进「本板 NPU」的账里。
        """
        reg = self._peek_registry()
        if reg is None:
            return []
        try:
            handles = reg.list_sessions(self.CAPABILITY,
                                        include_remote=False,
                                        model_id=self.MODEL_ID)
        except Exception:  # noqa: BLE001
            return []
        return [h.session for h in handles]

    def pick_session(self) -> Any:
        """轮转取一个 session。

        ⚠️ **并发单位是实例，不是线程** —— 一个 ``LLMRPCSession`` 的 RPC
        执行器是 ``ThreadPoolExecutor(max_workers=1)``
        （``wdndds/ai_session.py``）⇒ 同一个 session 同时只能一个推理在飞。
        实测（RK1828 双卡）：1 实例 24 条界面文案 3.30 s；4 实例（2 卡×2）
        1.45 s。⇒ 起几份实例就能并发几句。
        """
        sessions = self.sessions()
        if not sessions:
            raise RuntimeError(
                "NPU 翻译未启用（%s 没有正在运行的实例）" % self.CAPABILITY)
        with self._lock:
            self._rr = (self._rr + 1) % len(sessions)
            idx = self._rr
        return sessions[idx]

    # ------------------------------------------------------------------ #
    # 翻一句（流式场景的入口）
    # ------------------------------------------------------------------ #
    def resolve_languages(self, source: str, target: str) -> tuple:
        """把「随便什么语言写法」归一成 provider 的语言 key。

        * ``source`` —— ASR 报的是**语言名**（SDK 的 ``language English`` 头）；
        * ``target`` —— 用户选的目标语言（provider 的 key，如 ``en``）。

        返回 ``(源语言 key, 目标语言 key)``；认不出来抛
        ``UnsupportedLanguageError``。

        ⚠️ **归一在宿主这一侧做** —— 调用方（卡片）只管把「ASR 报的那个
        字符串」和「用户选的那个 key」原样传进来，不必知道 provider 的
        语言表长什么样。
        """
        from app.core.translate.base import UnsupportedLanguageError
        from app.core.translate.npu_provider import normalize_language

        src = normalize_language(source)
        tgt = normalize_language(target)
        if not src:
            raise UnsupportedLanguageError("认不出源语言: %r" % (source,))
        if not tgt:
            raise UnsupportedLanguageError("认不出目标语言: %r" % (target,))
        return src, tgt

    def translate_sentence(self, text: str, source: str, target: str
                           ) -> tuple:
        """翻一句 —— 流式同传的**唯一**入口。

        流程：归一语种 → 轮转选 session → **单次措辞**推理。

        返回 ``(译文, 性能表)``。

        抛：

        * :class:`RuntimeError` —— 模型没启用（没有可用 session）；
        * ``UnsupportedLanguageError`` —— 源/目标语言认不出来；
        * ``NpuQualityError`` —— 这句翻不出来（**不重试**）；
        * ``TranslationError`` —— 推理级故障（worker 挂了/超时）。
        """
        src, tgt = self.resolve_languages(source, target)
        session = self.pick_session()
        return self._provider().translate_one_fast(text, src, tgt,
                                                   session=session)

    def _provider(self):
        """本管理器专用的 provider 实例（**不复用**界面翻译那个）。

        ⚠️ 界面翻译的 provider 带着**负缓存**（同一句刚翻不出来就不再试）
        与 provider 链的冷却状态；流式同传要的是「每句都真跑一次」，
        两者语义相反 ⇒ 各自一份。
        """
        if self._provider_inst is None:
            from app.core.translate.npu_provider import NpuTranslateProvider
            self._provider_inst = NpuTranslateProvider(
                model_id=self.MODEL_ID, capability=self.CAPABILITY)
        return self._provider_inst

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    @staticmethod
    def _registry():
        """拿到（或**拉起**）算力注册表。

        ⚠️ 只有**要真的起/停模型**时才用 —— ``get_registry()`` 会初始化
        整个注册表（DDS hub + 对端发现 + 硬件探测），慢且有副作用。
        「只想看一眼状态」用 :meth:`_peek_registry`。
        """
        from app.core.compute.registry import get_registry
        return get_registry()

    @staticmethod
    def _peek_registry():
        """已初始化的注册表（没有就 ``None``）—— **不**触发初始化。

        状态查询走这里：ASR 卡片每 5 秒轮询一次翻译状态，不能因为
        轮询就把整个 registry 拉起来。
        """
        from app.core.compute.registry import peek_registry
        return peek_registry()

    @staticmethod
    def _usage(reg) -> tuple:
        """本板显存占用（只算本机实例）。"""
        if reg is None:
            return {}, 0, 0
        try:
            return reg.local_memory_usage()
        except Exception:  # noqa: BLE001
            return {}, 0, 0

    def _installed(self) -> bool:
        """本板**装没装**这个模型（文件系统判断，不碰 registry）。"""
        try:
            from app.core.compute.workers import find_manifest_path
            return find_manifest_path(self.MODEL_ID) is not None
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _local_instances(reg) -> List[Dict[str, Any]]:
        """本机（spawned / adopted_local）的翻译实例。"""
        out: List[Dict[str, Any]] = []
        if reg is None:
            return out
        try:
            snap = reg.snapshot()
        except Exception:  # noqa: BLE001
            return out
        for inst in snap.get("instances") or []:
            if inst.get("model_id") != NpuTranslateInstances.MODEL_ID:
                continue
            src = inst.get("_source") or "spawned"
            if src not in ("spawned", "adopted_local"):
                continue
            out.append(inst)
        return out

    @staticmethod
    def _find_slot(reg, card: int) -> Optional[Dict[str, Any]]:
        for inst in NpuTranslateInstances._local_instances(reg):
            if inst.get("npu_index") == card:
                return inst
        return None

    def _pick_card(self, reg) -> int:
        """按**卡位策略**挑一张卡（抛 RuntimeError 表示挑不出来）。

        策略见 :mod:`app.core.compute.card_roles`：

        * 翻译模型属于**感知链** ⇒ **跟随 ASR 所在的卡**（ASR 在哪张卡，
          翻译就跟到哪张）。真机实测同卡与分卡的争用 ≈ 0
          （ASR 轮间隔中位 1.21 s vs 1.22 s，端点后第一轮都是 1.68 s）——
          因为 ASR 的端点判定发生在**收尾推理之后**，翻译那一拍正好落在
          ASR 的空档里。
        * ASR 没跑（还没起 / 起不来）⇒ 退化成「挑一张放得下的卡」。

        ⚠️ 只算**本机**实例的占用 —— 远端 peer 的 ``npu_index`` 是**对端板
        自己的卡号**，把它算进来会把一张空卡判成占满（真机现场：peer 的
        5500 MB 被记到本板卡 1 ⇒ 本板 3 张卡全空却被判成「卡 1 满了」）。
        """
        from app.core.compute import card_roles
        try:
            return card_roles.pick_card(reg, self.MODEL_ID)
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("挑不出翻译模型的卡位: %s" % exc) from exc

    def _record_autostart(self, reg, card: int) -> None:
        """把翻译模型的卡位记进 ``compute.autostart``。

        为什么宿主侧要写这个：ASR 卡的 ``software_capabilities`` **只有**
        ``swcap_audio_asr``（故意不加 ``swcap_translate`` —— 加了会打断
        ASR 自己的 active_app / autostart 落盘逻辑，见 AGENTS.md）。
        ⇒ 翻译模型不会被 ``set_active_app`` 记进 autostart ⇒ 板子重启
        后翻译模型不会自动起来 ⇒ **重启后布局变了**。

        这里补上：模型真跑起来了就把卡位记下来 ⇒ 重启后恢复出来的布局
        与现在一致。

        ⚠️ **失败不影响主流程** —— 写配置失败只记日志，模型该跑还是跑。
        """
        try:
            reg.record_autostart_entry(self.MODEL_ID, int(card))
        except Exception as exc:  # noqa: BLE001
            LOG.warning("npu_manager: recording the autostart of the translation model failed (card #%d): %s",
                        card, exc)

    def _wait_ready(self, reg, card: int) -> bool:
        """等卡上的翻译实例变成 running（最多 :attr:`LOAD_TIMEOUT_S`）。"""
        deadline = time.time() + self.LOAD_TIMEOUT_S
        while time.time() < deadline:
            inst = self._find_slot(reg, card)
            if inst and inst.get("status") == "running":
                return True
            if inst and inst.get("status") == "error":
                return False
            time.sleep(self.LOAD_POLL_S)
        return False


#: 进程内单例。
_INSTANCES: Optional[NpuTranslateInstances] = None
_INSTANCES_LOCK = threading.Lock()


def get_npu_translate_instances() -> NpuTranslateInstances:
    """拿到全局的翻译实例管理器（首次调用时创建）。"""
    global _INSTANCES
    with _INSTANCES_LOCK:
        if _INSTANCES is None:
            _INSTANCES = NpuTranslateInstances()
        return _INSTANCES
