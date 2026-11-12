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
_BATCH_GAP = 1.2
# 内存缓存条目上限
_MAX_CACHE = 5000
# 配额用尽后的冷却(秒):这段时间内直接快速失败,不去打接口
_QUOTA_COOLDOWN = 120.0

_WS = re.compile(r"\s+")
# 行首的 "12. " / "12) " / "12、" —— 解析 MT 回传的编号列表
_ITEM_RE = re.compile(r"^\s*(\d{1,3})\s*[.)、]?\s*(.*)$")


def normalize(text: str) -> str:
    """把一段文案压成单行。

    编号列表靠换行分隔,一段文案里如果自带换行,切分就会错位。界面上的文本
    节点本来也是按 HTML 空白折叠渲染的,压成单行不影响观感。
    """
    return _WS.sub(" ", text).strip()


class TranslateService:
    """带缓存与分批的翻译服务。线程安全。"""

    def __init__(self, provider: TranslationProvider):
        self._provider = provider
        self._cache: dict[tuple[str, str, str], str] = {}
        # RLock 而不是 Lock:这把锁会被「查缓存」「写缓存」「看缓存大小」三处
        # 拿,万一哪天又出现嵌套,RLock 最多是重复计数,Lock 就是死锁 —— 而
        # 死锁的表现是**整个翻译服务(连同所有读缓存的请求)永久挂住**。
        self._lock = threading.RLock()
        self._quota_until = 0.0

    # ------------------------------------------------------------------ #
    # 对外
    # ------------------------------------------------------------------ #
    @property
    def provider_name(self) -> str:
        return self._provider.name

    def cache_size(self) -> int:
        with self._lock:
            return len(self._cache)

    def translate_segments(
        self, segments: Sequence[str], source: str, target: str
    ) -> tuple[list[str], dict]:
        """把一批文案翻成 ``target``。

        返回 ``(译文列表, 统计信息)``;译文列表与入参**一一对应、同序**。
        """
        self._check_quota_cooldown()

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
        requests = 0
        failed = 0
        for batch in self._pack(pending):
            translated, calls, misses = self._translate_batch(batch, source, target)
            requests += calls
            failed += misses
            for text, out in zip(batch, translated):
                # ⚠️ 不要在外面再套一层 ``with self._lock`` —— _remember 内部
                # 自己会拿锁,非重入锁这么套一层就是**自死锁**(实测:请求永远
                # 不返回,连带所有碰这把锁的请求一起挂死)。
                self._remember(source, target, text, out)
                for idx in groups[text]:
                    results[idx] = out
            time.sleep(_BATCH_GAP)

        stats = {
            "provider": self._provider.name,
            "segments": len(norm),
            "unique": len(groups),
            "cached": len(groups) - len(pending),
            "translated": len(pending),
            "requests": requests,
            # 没能翻出来的条数(保留原文)—— 前端要把它显示出来,不然用户
            # 以为整页都翻好了。
            "failed": failed,
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
    ) -> tuple[list[str], int, int]:
        """翻一批。

        返回 ``(译文列表, 本次实际发出的请求数, 失败条数)``。

        批次请求失败(免费接口会随机 SSL 握手超时)时**不整批放弃**:退化成
        逐条翻;单条再失败就保留原文。页面上少数几条没翻好,比整页失败强得多
        —— 用户看得见的就是"这一行还是中文",而不是"翻译服务不可用"。
        """
        joined = self._pack_numbered(texts)
        raw: str | None = None
        try:
            raw = self._provider.translate(joined, source, target)
        except QuotaExceeded:
            with self._lock:
                self._quota_until = time.time() + _QUOTA_COOLDOWN
            raise
        except TranslationError as exc:
            LOG.warning(
                "translate: 批次请求失败(%d 条),退化为逐条翻译: %s", len(texts), exc
            )
        if raw is not None:
            parsed = self._parse_numbered(raw, len(texts))
            if parsed is not None:
                return parsed, 1, 0
            # 编号没对上(MT 把两条并成一条、或把编号改写了)
            LOG.warning(
                "translate: 批次编号解析失败(%d 条),退化为逐条翻译", len(texts)
            )

        out: list[str] = []
        failed = 0
        for text in texts:
            try:
                out.append(self._provider.translate(text, source, target))
            except QuotaExceeded:
                raise
            except TranslationError as exc:
                LOG.warning("translate: 单条翻译失败,保留原文: %s", exc)
                out.append(text)
                failed += 1
        return out, len(texts) + 1, failed

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

    def _check_quota_cooldown(self) -> None:
        with self._lock:
            if self._quota_until > time.time():
                raise QuotaExceeded("翻译服务免费额度已用尽,稍后再试")
