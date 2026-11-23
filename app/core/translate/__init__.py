"""板端翻译服务 —— 前端不直连任何翻译站点,统一走本机。

背景
----
浏览器在局域网里直连翻译站点不可靠:Google 系域名(translate.google.com /
translate.googleapis.com / translate.googleusercontent.com)在境内网络基本不
可达 —— 实测本板到这三个域名全部连接超时,而阿里云镜像 / 百度 / 有道 / GitHub
都通。所以「前端挂一个 translate.js 挂件」这条路在这套网络里走不通。

于是改成:前端只跟**本机**说话(``POST /api/translate``),板子再转发给真正
干活的翻译服务(provider)。好处:

* 浏览器不需要能访问任何境外站点;
* provider 可插拔 —— 换一家只改 ``config.jsonc`` 的 ``translate.provider``;
* 服务端能加缓存(同一批文案只翻一次),省配额也省时间。

默认 provider 链是**三家免 key 的翻译站轮着来**(顺序即故障转移顺序):

* ``youdao`` —— 有道/金山词霸直连(``dict-trans.youdao.com``),0.1~0.9s/次;
* ``iciba``  —— 金山词霸经 SimplyTranslate 中转站,是通往同一家翻译站的
  **另一条路**(配额与网络路径都不同,一条被限流另一条大概率还活着);
* ``mymemory`` —— MyMemory,免 key 但匿名每天只有 5000 词(填邮箱到 5 万)。

哪一家被限流(429)或抽风,链会自动切到下一家,并给每家**单独记冷却**。

配置(``config.jsonc``,设备级、不入库)::

    "translate": {
      // 按顺序尝试:第一个是主力,后面的被限流(429)时自动接上
      "providers": ["youdao", "iciba", "mymemory", "baidu"],
      "email": "",                          // MyMemory:填邮箱把每日配额 5k → 50k 词
      "baidu": {"appid": "", "key": ""},    // 百度官方 API(要 key;免 key 的网页版已被反爬堵死)
      "iciba": {"relay": "https://simplytranslate.org"}   // 金山词霸中转站
    }
"""
from __future__ import annotations

import logging
import threading

from app.core.config import get_config
from app.core.translate.base import (
    TranslationResult,
    UnsupportedLanguageError,
)
from app.core.translate.npu_provider import NpuTranslateProvider
from app.core.translate.providers import (
    DEFAULT_PROVIDERS,
    PROVIDER_LABELS,
    BaiduProvider,
    IcibaProvider,
    MyMemoryProvider,
    ProviderChain,
    QuotaExceeded,
    TranslationError,
    TranslationProvider,
    YoudaoProvider,
    build_chain,
)
from app.core.translate.service import TranslateService

LOG = logging.getLogger("core.translate")

__all__ = [
    "DEFAULT_PROVIDERS",
    "PROVIDER_LABELS",
    "BaiduProvider",
    "IcibaProvider",
    "MyMemoryProvider",
    "NpuTranslateProvider",
    "ProviderChain",
    "QuotaExceeded",
    "TranslateService",
    "TranslationError",
    "TranslationProvider",
    "TranslationResult",
    "UnsupportedLanguageError",
    "YoudaoProvider",
    "get_translate_service",
    "npu_translate_enabled",
    "reset_translate_service",
    "set_npu_translate_enabled",
]

_service: TranslateService | None = None
_lock = threading.Lock()

#: NPU 那一路是否启用。``None`` = 跟着 ``config.jsonc`` 的
#: ``translate.providers`` 走（默认）；``True`` / ``False`` = 运行期被
#: 「网页翻译」卡片显式开/关过。见 :func:`set_npu_translate_enabled`。
_npu_enabled: bool | None = None


def _build_chain() -> ProviderChain:
    """按 config.jsonc 的 ``translate`` 段造 provider 链。

    兼容旧的 ``translate.provider``(单数)写法;新写法是 ``providers``(复数,
    带顺序)—— 顺序就是**故障转移顺序**:前一个被限流(429)时自动切下一个。
    """
    section = {}
    try:
        section = get_config().get_section("translate", {}) or {}
    except Exception as exc:  # 配置读不到不该让翻译功能整体不可用
        LOG.warning("translate: reading the translate section of config.jsonc failed: %s", exc)

    names = section.get("providers")
    if not names:
        # 旧写法:单个 provider
        names = [section.get("provider") or DEFAULT_PROVIDERS[0]]
    if isinstance(names, str):
        names = [names]
    names = [str(n).strip().lower() for n in names if str(n).strip()]

    # ⚠️ 「网页翻译」卡片可以在**运行期**开关 NPU 那一路（启用/禁用按钮）。
    #
    #   * 启用 → 把 npu 放到链的**最前面**（主力），外网那几家退成备选；
    #   * 禁用 → 把 npu 从链里**摘掉**，界面翻译继续走外网。
    #
    # 这里的开关是**运行期状态**，不是配置：卡片点了「禁用」之后，链里就不该
    # 再有 npu（否则每次翻译都先撞一次 NPU 的 60s 冷却才轮到外网）。
    if _npu_enabled is False:
        names = [n for n in names if n != NpuTranslateProvider.name]
    elif _npu_enabled is True and NpuTranslateProvider.name not in names:
        names = [NpuTranslateProvider.name] + names

    email = str(section.get("email") or "")
    baidu = section.get("baidu") or {}
    iciba = section.get("iciba") or {}

    chain = build_chain(list(names), email=email, baidu=baidu, iciba=iciba)
    LOG.info(
        "translate: provider chain = %s", " → ".join(chain.names) or "(空)"
    )
    return chain


def get_translate_service() -> TranslateService:
    """拿到全局翻译服务(首次调用时按配置造 provider 链)。"""
    global _service
    with _lock:
        if _service is None:
            _service = TranslateService(_build_chain())
        return _service


def reset_translate_service() -> None:
    """丢掉当前服务(改过 config 之后调用,让新配置生效)。"""
    global _service
    with _lock:
        _service = None


def set_npu_translate_enabled(enabled: bool | None) -> bool:
    """运行期开关「NPU 翻译」这一路 provider。

    ``enabled=True``  → npu 进链并排在**最前**（主力）；
    ``enabled=False`` → npu 出链（界面翻译继续走外网那几家）；
    ``enabled=None``  → 交回 ``config.jsonc`` 决定。

    改完立刻重建服务(``reset_translate_service``) —— provider 链是在服务
    构造时定下的,只改标志位不重建的话,老链还在用。

    返回**当前实际生效**的状态(``True`` / ``False``)。
    """
    global _npu_enabled, _service
    with _lock:
        _npu_enabled = None if enabled is None else bool(enabled)
        _service = None
    # 重建一次,好让日志里立刻看到新的链
    get_translate_service()
    return _npu_enabled is not False


def npu_translate_enabled() -> bool:
    """NPU 那一路当前**是否在链里**（按实际构建出来的链算）。"""
    try:
        return NpuTranslateProvider.name in get_translate_service().provider_names
    except Exception:  # noqa: BLE001
        return False
