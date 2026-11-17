"""翻译服务 —— 分批、缓存、失败隔离。

一次「把整个界面翻成英文」意味着几百条中文文案。逐条发请求 = 几百次
往返(慢、吃配额、还容易被限流),所以这里做三件事:

1. **去重**:同一个中文串在界面里出现几十次是常态(「应用面板」「刷新」…),
   只翻一份,剩下的直接复用。
2. **分批**:把几十条拼成一次请求。拼法是**编号列表**(``1. xxx\\n2. yyy``)——
   实测 MyMemory 会原样保留行首编号和换行结构,切回来时按编号对齐,不会串行。
   单次 query 上限 500 字符(MyMemory 硬限制),所以一批控制在 450 字符 / 25 条。
3. **缓存**:``(源, 目标, 原文) → 译文`` 缓存在内存里。切一次语言之后再切回来
   是秒回(不花配额),界面里反复出现的新文案也只翻一次。

编号对不上时(比如 MT 把两条并成了一条)会退化成**逐条翻译**——慢一点,但一定
不会把 A 的译文贴到 B 上。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Sequence

from app.core.translate.providers import (
    ProviderChain,
    QuotaExceeded,
    TranslationError,
    TranslationProvider,
)

LOG = logging.getLogger("core.translate")

# 单次请求字符上限。provider 的硬上限是 500(超了 403),留 20 余量。
_MAX_BATCH_CHARS = 480
# 一批最多多少条 —— 编号列表太长 MT 容易跑偏
_MAX_BATCH_SEGMENTS = 60
# 批与批之间的间隔(秒)。
#
# 免费接口按**连接建立速率**掐人:实测同一 IP 每 0.4s 发一个请求,十次里能撞
# 两次 SSL 握手超时(要等 12s 超时 + 重试,一次请求拖成 15s);改成 1.2s 间隔
# 连打 10 次零失败。慢几秒换不来超时,值。
#
# ⚠️ **只对走外网的 provider 生效** —— 本地 provider(板载 NPU)没有外网
# 连接,这个间隔是白等的(它占 4 实例端到端耗时的 30%)。判据见
# :meth:`TranslateService.translate_segments` 与
# :attr:`app.core.translate.base.TranslationProvider.local`。
_BATCH_GAP = 1.2
# 内存缓存条目上限
_MAX_CACHE = 5000

_WS = re.compile(r"\s+")
# 行首的 "12. " / "12) " / "12、" —— 解析 MT 回传的编号列表
_ITEM_RE = re.compile(r"^\s*(\d{1,3})\s*[.)、]?\s*(.*)$")


#: perf 表里**可以累加**的原始计数（其余是派生量，不累加）。
_PERF_COUNTER_KEYS = ("n_decode_tokens", "n_prefill_tokens",
                      "prefill_ms", "decode_ms")


def _merge_perf(dst: dict[str, float], src: dict | None) -> dict[str, float]:
    """把一份**推理性能表**累加进汇总表。

    ⚠️ 累加而不是覆盖：一个 provider 一条文案可能要试**好几条措辞**
    （``PROMPT_ORDER``），每条措辞都是一次完整推理 —— 真正花掉的 token
    和时间是**所有这些推理之和**。只记最后一次的话，tokens/s 会好看得
    离谱，而用户实际等的是那么久。

    ⚠️ **只累加原始计数**；派生量（tokens/s 等）由 :func:`_finish_perf`
    从累加后的计数**重算** —— 每次推理各自的速率加起来没有意义。

    ⚠️ 只加**数字**字段：这个 dict 最后要进 JSON 响应，混进别的类型
    会让整个响应序列化失败（推理成功了但前端收到 500）。
    """
    if not isinstance(src, dict):
        return dst
    for key, val in src.items():
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            continue
        if key in _PERF_COUNTER_KEYS:
            dst[key] = dst.get(key, 0.0) + float(val)
    return dst


def _finish_perf(perf: dict[str, float]) -> dict[str, float]:
    """把累加好的原始计数算成最终性能表（补上派生量）。

    ⚠️ 派生量用 **SDK 自己的** :func:`wdnai.perf_table` 算 —— 和 SDK 打印的
    那张 perf 表**同一份公式**。宿主要是自己另写一套，两边迟早漂。

    ⚠️ **没有原始计数就返回空字典**（不是「一堆 0」）：外网 provider 压根
    没推理 ⇒ 性能表就该是空的 ⇒ 前端一眼看出「这条路没有推理数据」。
    """
    if not perf:
        return {}
    from wdnai import perf_table

    out = dict(perf)
    out.update(perf_table(out))
    return out


def normalize(text: str) -> str:
    """把一段文案压成单行。

    编号列表靠换行分隔,一段文案里如果自带换行,切分就会错位。界面上的文本
    节点本来也是按 HTML 空白折叠渲染的,压成单行不影响观感。
    """
    return _WS.sub(" ", text).strip()


class TranslateService:
    """带缓存与分批的翻译服务。线程安全。"""

    def __init__(self, provider: TranslationProvider | ProviderChain):
        # 传单个 provider 也行 —— 包成一条只有一节的链,调用方不用改。
        self._chain = (
            provider if isinstance(provider, ProviderChain)
            else ProviderChain([provider])
        )
        self._cache: dict[tuple[str, str, str], str] = {}
        # RLock 而不是 Lock:这把锁会被「查缓存」「写缓存」「看缓存大小」三处
        # 拿,万一哪天又出现嵌套,RLock 最多是重复计数,Lock 就是死锁 —— 而
        # 死锁的表现是**整个翻译服务(连同所有读缓存的请求)永久挂住**。
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # 对外
    # ------------------------------------------------------------------ #
    @property
    def provider_name(self) -> str:
        """最后一次真正干活的 provider(链里可能换过)。"""
        return self._chain.last_used or (self._chain.names[0] if self._chain.names else "")

    @property
    def provider_names(self) -> list[str]:
        return self._chain.names

    def cooldown_state(self) -> dict[str, int]:
        return self._chain.cooldown_state()

    def cache_size(self) -> int:
        with self._lock:
            return len(self._cache)

    def translate_segments(
        self, segments: Sequence[str], source: str, target: str
    ) -> tuple[list[str], dict]:
        """把一批文案翻成 ``target``。

        返回 ``(译文列表, 统计信息)``;译文列表与入参**一一对应、同序**。

        ⚠️ 统计信息里带了两个**性能**字段（2026-11-17 加）：

        * ``elapsed_ms`` —— **端到端**耗时（毫秒）。从进函数到出函数的
          墙钟时间，含缓存查找、批次拆分、补翻，以及批与批之间的间隔。
          这是**用户实际等到的那个时间**。
        * ``perf`` —— **推理**性能表（数字字典）：
          ``n_decode_tokens`` / ``n_prefill_tokens`` / ``prefill_ms`` /
          ``decode_ms``。只有本地 provider（本板 NPU）才有；外网那几家
          压根不推理 ⇒ 这个字段是空的。
        """
        t_start = time.perf_counter()
        norm = [normalize(s) for s in segments]
        results: list[str] = [""] * len(norm)

        # 1. 去重:同一条文案只翻一次
        groups: dict[str, list[int]] = {}
        for idx, text in enumerate(norm):
            groups.setdefault(text, []).append(idx)

        # 2. 查缓存
        pending: list[str] = []
        for text in groups:
            with self._lock:
                hit = self._cache.get((source, target, text))
            if hit is None:
                pending.append(text)
            else:
                for idx in groups[text]:
                    results[idx] = hit

        # 3. 分批翻剩下的。
        #
        #    **串行**,不加并发:免费接口(MyMemory)会掐并发 —— 实测同一 IP
        #    同时打 4 路就会成片 SSL 握手超时(单发 1~3s 正常)。一次界面翻译
        #    也就 5~8 批,串行 10~25s 可以接受,换来的是**不超时**。
        # 冷却检查交给 provider 链(每家各管各的冷却):全都命中缓存时根本
        # 不需要 provider,不该被限流拦住(限流的是外网,不是缓存)。
        requests = 0
        failed = 0
        #: 各 provider 各自翻了多少条 —— 用来回答「谁来翻」。
        #:
        #: ⚠️ 不能只看 ``chain.last_used``:补翻那一条之后 last_used 变成了
        #: 外网那家，但实际是 **23 条 NPU + 1 条外网**。上报「多数条」那家
        #: 才符合实际（前端据此显示 NPU 徽标）。
        provider_counts: dict[str, int] = {}
        #: **推理性能表汇总**（跨批次累加）—— 只有本地 provider 才有。
        perf_total: dict[str, float] = {}
        for batch in self._pack(pending):
            translated, calls, misses, provs, perf = self._translate_batch(
                batch, source, target)
            requests += calls
            failed += len(misses)
            _merge_perf(perf_total, perf)
            for name, cnt in provs.items():
                provider_counts[name] = provider_counts.get(name, 0) + cnt
            for j, (text, out) in enumerate(zip(batch, translated)):
                if j in misses:
                    # 翻失败的**绝对不能写缓存**。写了就是把「原文 = 译文」记
                    # 进缓存 —— 下次直接命中,返回的还是中文,而且**再也不会
                    # 重试**(实测踩过:接口 429 限流时,整批文案被永久毒化)。
                    for idx in groups[text]:
                        results[idx] = text      # 保留原文
                    continue
                # ⚠️ 不要在外面再套一层 ``with self._lock`` —— _remember 内部
                # 自己会拿锁,非重入锁这么套一层就是**自死锁**(实测:请求永远
                # 不返回,连带所有碰这把锁的请求一起挂死)。
                self._remember(source, target, text, out)
                for idx in groups[text]:
                    results[idx] = out
            # 自适应间隔:刚失败过就多歇一会儿,别再把接口往限流里推
            #
            # ⚠️ **这个间隔只对「外网 provider」有意义**。
            #
            # 它的存在是因为免费接口(MyMemory / 有道)按**连接建立速率**掐人 ——
            # 连着打会成片 SSL 握手超时。而**本地 provider**(板载 NPU 推理)
            # 根本没有外网连接,这个间隔纯属白等:实测它占 4 实例端到端
            # 耗时的 **30%**(3.99s 里 1.2s),去掉之后同一批文案从
            # 3.99s 掉到 ~2.8s。
            #
            # 判据:这一批**有没有真的用到外网**(而不是「最后一家是谁」——
            # 补翻一条之后 last_used 就变成外网那家了,但那是 1 条不是 24 条)。
            # 批里有条目没翻出来(misses)时照旧多歇一会儿 —— 那是真出错了,
            # 该退避。
            used_external = any(
                not self._chain.provider_is_local(name) for name in provs
            )
            if misses:
                time.sleep(_BATCH_GAP * 2)
            elif used_external:
                time.sleep(_BATCH_GAP)

        # ⚠️ **性能**（2026-11-17 加）——
        #
        # ⚠️ **全部在宿主算好再发给前端** —— 前端只负责显示，一个数都不
        # 自己算（用户明确要求：不要在 JS 端计时 / 换算）。
        #
        # ``elapsed_ms``      = 端到端墙钟耗时（用户实际等的那个时间）。
        # ``elapsed_s``       = 同一个数，秒为单位（给界面显示用）。
        # ``tokens_per_second`` = **SDK 算好的**解码吞吐（tokens/s）。
        # ``perf``            = 完整性能表（原始计数 + 派生量）。
        #   只有本地 provider（本板 NPU）才有 —— 外网那几家不推理。
        elapsed_ms = int((time.perf_counter() - t_start) * 1000)
        perf_final = _finish_perf(perf_total)
        stats = {
            "provider": self._dominant_provider(provider_counts),
            "providers": self.provider_names,
            "segments": len(norm),
            "unique": len(groups),
            "cached": len(groups) - len(pending),
            "translated": len(pending),
            "requests": requests,
            # 没能翻出来的条数(保留原文)—— 前端要把它显示出来,不然用户
            # 以为整页都翻好了。
            "failed": failed,
            "elapsed_ms": elapsed_ms,
            "elapsed_s": round(elapsed_ms / 1000.0, 2),
            "tokens_per_second": round(
                float(perf_final.get("decode_tokens_per_second") or 0.0), 1),
            "perf": perf_final,
        }
        return results, stats

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _pack(self, texts: Sequence[str]) -> list[list[str]]:
        """把待翻文案切成若干批。

        **按拼出来的实际长度算**:批次发出去的是编号列表,``"100. xxx"`` 这种
        编号本身也占字符 —— 只按原文长度切,71 条 246 字的文案会被编号撑到
        530 字符,直接撞上 provider 的 500 字符硬上限(403)。
        """
        batches: list[list[str]] = []
        current: list[str] = []
        size = 0
        for text in texts:
            piece = "%d. %s" % (len(current) + 1, text)
            add = len(piece) + (1 if current else 0)  # 行首的换行
            if current and (
                size + add > _MAX_BATCH_CHARS
                or len(current) >= _MAX_BATCH_SEGMENTS
            ):
                batches.append(current)
                current, size = [], 0
                piece = "1. %s" % text
                add = len(piece)
            current.append(text)
            size += add
        if current:
            batches.append(current)
        return batches

    def _translate_batch(
        self, texts: Sequence[str], source: str, target: str
    ) -> tuple[list[str], int, set[int], dict[str, int], dict[str, float]]:
        """翻一批。

        返回 ``(译文列表, 本次实际发出的请求数, 失败条在批内的下标集合,
        各 provider 翻了多少条, 推理性能表)``。

        失败分三种,处理方式**不一样**:

        * **网络类**(批次请求挂了):整批**保留原文**并记账 —— 绝不能退化成
          逐条翻:被限流的接口上,60 次单发只会更糟(限流是按连接建立速率算的)。
          已经翻成功的那几批在缓存里,用户再点一次只补没翻出来的那几批。
        * **编号对不上**(MT 把两条并成一条):这是**正确性**问题不是网络问题,
          退化成逐条翻 —— 宁可慢,也不能把 A 的译文贴到 B 上。
        * **provider 只翻出了一部分**(NPU 有几条翻不出来):**只补翻那几条**,
          其余条目的译文**沿用 NPU 的** —— 这是「一条不合格拖垮 24 条」
          那个 bug 的修复点。
        """
        joined = self._pack_numbered(texts)
        perf: dict[str, float] = {}
        try:
            res = self._chain.translate(joined, source, target)
        except QuotaExceeded:
            # 链里已经把出问题的 provider 记进冷却了,这里直接往上抛
            raise
        except TranslationError as exc:
            LOG.warning(
                "translate: 批次请求失败(%d 条),本批保留原文: %s", len(texts), exc
            )
            return list(texts), 0, set(range(len(texts))), {}, perf

        _merge_perf(perf, getattr(res, "perf", None))
        parsed = self._parse_numbered(res.text, len(texts))
        if parsed is not None:
            out = parsed
            calls = 1
            misses = set(res.misses or ())
            provs: dict[str, int] = {}
            if res.provider:
                provs[res.provider] = max(0, len(texts) - len(misses))
            if misses:
                out, misses, extra, fixed, rperf = self._repair(
                    out, misses, texts, source, target, res.failed_provider)
                calls += extra
                _merge_perf(perf, rperf)
                for name, cnt in fixed.items():
                    provs[name] = provs.get(name, 0) + cnt
            return out, calls, misses, provs, perf

        # 编号没对上(MT 把两条并成一条、或把编号改写了)→ 逐条翻
        LOG.warning(
            "translate: 批次编号解析失败(%d 条),退化为逐条翻译", len(texts)
        )
        out = []
        misses = set()
        provs = {}
        for i, text in enumerate(texts):
            try:
                r = self._chain.translate(text, source, target)
            except QuotaExceeded:
                raise
            except TranslationError as exc:
                LOG.warning("translate: 单条翻译失败,保留原文: %s", exc)
                out.append(text)
                misses.add(i)
                continue
            _merge_perf(perf, getattr(r, "perf", None))
            if r.misses:
                out.append(text)
                misses.add(i)
                continue
            out.append(r.text)
            if r.provider:
                provs[r.provider] = provs.get(r.provider, 0) + 1
        return out, len(texts) + 1, misses, provs, perf

    def _repair(
        self, out: list[str], misses: set[int], texts: Sequence[str],
        source: str, target: str, failed_provider: str,
    ) -> tuple[list[str], set[int], int, dict[str, int], dict[str, float]]:
        """**只补翻没翻出来的那几条** —— 已经翻好的不牵连。

        provider 侧已经把它们单独报上来了(``TranslationResult.misses``)。
        这里把这几条交给外网补(并**跳过**刚刚翻不出来的那家,免得同一段
        文字在同一家身上又失败一次),其余条目**沿用 NPU 的译文**。

        以前是「任何一条失败 → 整批抛异常 → 整批交给外网」—— NPU 已经
        跑完的那 23 条**全白跑**(实测 28 种界面语言里有 7 种栽在这上面)。

        返回值最后一项是**补翻这段的推理性能表**（补翻走的是外网 ⇒ 一般
        是空的，但 provider 链的顺序不保证，理论上可能有本地 provider 兜底）。
        """
        skip = (failed_provider,) if failed_provider else ()
        left = set(misses)
        calls = 0
        provs: dict[str, int] = {}
        perf: dict[str, float] = {}
        for i in sorted(misses):
            try:
                r = self._chain.translate(texts[i], source, target, skip=skip)
            except QuotaExceeded:
                raise
            except TranslationError as exc:
                LOG.warning("translate: 单条补翻失败,保留原文: %s", exc)
                continue
            calls += 1
            _merge_perf(perf, getattr(r, "perf", None))
            if r.misses:
                # 补翻的那家也翻不出来 —— 保留原文
                continue
            out[i] = r.text
            left.discard(i)
            if r.provider:
                provs[r.provider] = provs.get(r.provider, 0) + 1
        return out, left, calls, provs, perf

    def _dominant_provider(self, counts: dict[str, int]) -> str:
        """「谁来翻」= **翻了多数条**的那家。

        不用 ``chain.last_used``:补翻一条之后 last_used 变成外网那家,
        但实际是 23 条 NPU + 1 条外网 —— 上报 last_used 会让前端显示
        「有道在翻」,而实际上大头是 NPU。
        """
        if not counts:
            return self.provider_name
        return max(counts.items(), key=lambda kv: kv[1])[0]

    @staticmethod
    def _pack_numbered(texts: Sequence[str]) -> str:
        """拼成编号列表。

        实测 MyMemory 会保留行首编号与换行,切回来能按编号一一对齐。
        """
        return "\n".join(f"{i}. {t}" for i, t in enumerate(texts, 1))

    @staticmethod
    def _parse_numbered(text: str, expected: int) -> list[str] | None:
        """按行首编号把译文切回来;对不上就返回 None(交给调用方退化处理)。"""
        out: dict[int, str] = {}
        want = 1
        for line in (text or "").split("\n"):
            m = _ITEM_RE.match(line)
            # 只有「行首数字正好是下一条期望的序号」才算新的一条 —— 否则
            # 译文里出现 "3 个文件" 这种开头会被误当成新条目。
            if m and int(m.group(1)) == want:
                out[want] = m.group(2).strip()
                want += 1
                continue
            if want > 1:
                # 续行(被折行的一条译文)→ 拼回上一条
                out[want - 1] = (out[want - 1] + " " + line.strip()).strip()
        if want != expected + 1:
            return None
        return [out[i] for i in range(1, expected + 1)]

    def _remember(self, source: str, target: str, text: str, out: str) -> None:
        with self._lock:
            if len(self._cache) >= _MAX_CACHE:
                # 简单粗暴:清掉一半(FIFO 味道够用,这里不需要精确 LRU)
                for key in list(self._cache)[: _MAX_CACHE // 2]:
                    self._cache.pop(key, None)
            self._cache[(source, target, text)] = out

