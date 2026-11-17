"""翻译 provider 的**公共基类**。

为什么单独拆一个文件
--------------------
``providers.py``（外网那几家）和 ``npu_provider.py``（板载 NPU 模型）
都需要 ``TranslationProvider`` / ``TranslationError`` / ``QuotaExceeded``。
两边都要用、而 ``providers.py`` 又要把 NPU 那家登记进 ``PROVIDERS`` ——
放在一起就是**循环 import**（providers → npu_provider → providers）。

所以基类放这儿，两边都从 ``base`` 导入。``providers.py`` 仍然把三个名字
**原样再导出**（``from app.core.translate.providers import TranslationError``
这种老写法一律不用改）。
"""
from __future__ import annotations


class TranslationError(RuntimeError):
    """翻译失败(网络不通 / 语言对不支持 / 返回格式不对)。"""


class QuotaExceeded(TranslationError):
    """免费额度用尽。

    与「网络不通」必须分成两个异常:前者要等,后者可以立刻重试 —— 混在一起
    的话前端只能给出「翻译失败」这种没法指导用户下一步的提示。
    """


class UnsupportedLanguageError(TranslationError):
    """语言对不在这个 provider 的支持范围内。

    为什么单独一个类
    ----------------
    「语言不支持」是**确定性的** —— 再试一万次也是不支持。而
    :class:`~app.core.translate.providers.ProviderChain` 拿到普通的
    :class:`TranslationError` 会给该 provider 记一段**冷却**(默认 60 秒),
    冷却期里它被**静默**剔除(日志里一行告警都没有)。

    ⇒ 于是「这个语言不支持」会**连累别的语言**:接下来一分钟里所有翻译
    都走了外网,而日志里完全看不出为什么。实测 6 种语言不支持
    (sv/da/no/fi/el/hu),每次都把 NPU 冷却掉 60 秒。

    这个异常告诉链:「换下一家吧,但**别记我的冷却**」。
    """


class TranslationResult(str):
    """provider 的返回值 —— **它本身就是 str**(老代码一个字都不用改),
    额外带三个元信息字段。

    为什么要有它
    ------------
    provider 可能只翻成功了批里的**一部分**:比如 NPU 把 24 条里的 23 条
    翻出来了,第 24 条翻不出来。以前这种情况只能**整批**抛异常 → 整批
    交给外网重翻,已经跑完的那 23 条**全白跑**(实测:28 种界面语言里有 7
    种因为一条不合格而整批回退外网)。

    现在把「哪几条没翻出来」一起带上来,上层**只补翻那几条**。

    字段
    ----
    ``provider``
        真正干活的 provider 名。
    ``misses``
        批内**下标集合**:这些条目没翻出来(译文里保留的是原文)。
    ``failed_provider``
        这些条目是哪一家翻不出来的 —— 上层补翻时**跳过它**,免得同一段
        文字在同一家身上又失败一次。
    ``perf``
        **推理性能表**（本地 provider 才有）—— 数字字典：
        ``n_decode_tokens`` / ``n_prefill_tokens`` / ``prefill_ms`` /
        ``decode_ms``。

        ⚠️ 为什么它得跟着返回值走：这些数字**只有 worker 进程里才有**
        （SDK 把它们写进 ``session._backend.sink.perf``），而 provider 跑在
        Web UI 进程里 —— 拿不到 ⇒ worker 必须显式带过 wire（见
        ``wdndds/ai_session.py``），provider 再从 session 上取回来。

        ⚠️ **本地 provider 才有**：外网那几家（有道 / 金山词霸 / MyMemory）
        压根不推理 ⇒ 这个字段是空的。
    """

    __slots__ = ("provider", "misses", "failed_provider", "perf")

    def __new__(cls, text, provider: str = "", misses=frozenset(),
                failed_provider: str = "", perf=None):
        obj = super().__new__(cls, text if text is not None else "")
        obj.provider = provider or ""
        obj.misses = frozenset(misses or ())
        obj.failed_provider = failed_provider or ""
        # ⚠️ 只留**数字**：这个 dict 最后要进 JSON 响应，混进别的类型
        # 会让整个响应序列化失败（推理成功了但前端收到 500）。
        obj.perf = {
            str(k): v for k, v in dict(perf or {}).items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        }
        return obj

    @property
    def ok(self) -> bool:
        """这一批是不是**全部**翻出来了。"""
        return not self.misses

    @property
    def text(self) -> str:
        """译文正文。

        它本身就是 str（``out == "译文"`` 直接可用），这个属性只是让调用处
        读起来清楚 —— 免得一会儿 ``out`` 一会儿 ``out.text``。
        """
        return str(self)


class TranslationProvider:
    """provider 接口。子类实现 :meth:`translate`。"""

    name = "base"

    #: 这个 provider 是不是**本地的**（推理在本机跑，不 outbound 走网络）。
    #:
    #: ⚠️ 这个标志只有一个用处，但它很值钱：``TranslateService`` 在**批与批
    #: 之间**会 sleep 一小段时间（``_BATCH_GAP``），那是**给免费外网接口防
    #: 限流**的 —— 免费站点按「连接建立速率」掐人，连着打会成片 SSL 握手超时。
    #:
    #: 本地 provider（板载 NPU）**根本没有外网连接**，那个间隔纯属白等：
    #: 实测它占 4 实例端到端耗时的 **30%**（3.99s 里 1.2s）。标了
    #: ``local = True`` 就能省掉。
    local = False

    def translate(self, text: str, source: str, target: str) -> str:
        """把 ``text`` 从 ``source`` 翻成 ``target``。"""
        raise NotImplementedError


__all__ = [
    "TranslationError",
    "QuotaExceeded",
    "UnsupportedLanguageError",
    "TranslationResult",
    "TranslationProvider",
]
