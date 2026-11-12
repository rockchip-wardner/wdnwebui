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
    "ProviderChain",
    "QuotaExceeded",
    "TranslateService",
    "TranslationError",
    "TranslationProvider",
    "YoudaoProvider",
    "get_translate_service",
    "reset_translate_service",
]

_service: TranslateService | None = None
_lock = threading.Lock()


def _build_chain() -> ProviderChain:
    """按 config.jsonc 的 ``translate`` 段造 provider 链。

    兼容旧的 ``translate.provider``(单数)写法;新写法是 ``providers``(复数,
    带顺序)—— 顺序就是**故障转移顺序**:前一个被限流(429)时自动切下一个。
    """
    section = {}
    try:
        section = get_config().get_section("translate", {}) or {}
    except Exception as exc:  # 配置读不到不该让翻译功能整体不可用
        LOG.warning("translate: 读取 config.jsonc 的 translate 段失败: %s", exc)

    names = section.get("providers")
    if not names:
        # 旧写法:单个 provider
        names = [section.get("provider") or DEFAULT_PROVIDERS[0]]
    if isinstance(names, str):
        names = [names]
    email = str(section.get("email") or "")
    baidu = section.get("baidu") or {}
    iciba = section.get("iciba") or {}

    chain = build_chain(list(names), email=email, baidu=baidu, iciba=iciba)
    LOG.info(
        "translate: provider 链 = %s", " → ".join(chain.names) or "(空)"
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
