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

默认 provider 是 MyMemory(``api.mymemory.translated.net``):免 key、覆盖 90+
语言,实测本板可达且稳定(1~3s/次)。

配置(``config.jsonc``,设备级、不入库)::

    "translate": {
      "provider": "mymemory",
      "email": ""            // 可选:填邮箱把每日配额从 5k 词提到 50k
    }
"""
from __future__ import annotations

import logging
import threading

from app.core.config import get_config
from app.core.translate.providers import (
    MyMemoryProvider,
    QuotaExceeded,
    TranslationError,
    TranslationProvider,
    build_provider,
)
from app.core.translate.service import TranslateService

LOG = logging.getLogger("core.translate")

__all__ = [
    "MyMemoryProvider",
    "QuotaExceeded",
    "TranslateService",
    "TranslationError",
    "TranslationProvider",
    "get_translate_service",
    "reset_translate_service",
]

_service: TranslateService | None = None
_lock = threading.Lock()


def get_translate_service() -> TranslateService:
    """拿到全局翻译服务(首次调用时按配置造 provider)。"""
    global _service
    with _lock:
        if _service is None:
            _service = TranslateService(_build_provider())
        return _service


def reset_translate_service() -> None:
    """丢掉当前服务(改过 config 之后调用,让新配置生效)。"""
    global _service
    with _lock:
        _service = None


def _build_provider() -> TranslationProvider:
    section = {}
    try:
        section = get_config().get_section("translate", {}) or {}
    except Exception as exc:  # 配置读不到不该让翻译功能整体不可用
        LOG.warning("translate: 读取 config.jsonc 的 translate 段失败: %s", exc)
    name = str(section.get("provider") or "mymemory")
    email = str(section.get("email") or "")
    provider = build_provider(name, email)
    LOG.info("translate: provider=%s", provider.name)
    return provider
