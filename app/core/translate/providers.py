"""翻译 provider —— 真正把一句中文翻成另一种语言的那一端。

为什么要有这一层
----------------
浏览器在局域网里**直连翻译站点不可靠**:Google 系域名(translate.google.com /
translate.googleapis.com / translate.googleusercontent.com)在境内网络基本不
可达 —— 实测本板到这三个域名全部连接超时,而阿里云镜像 / 百度 / 有道 / GitHub
都通。所以「在前端挂个 translate.js 挂件」这条路在这套网络里走不通。

于是前端只跟**本机**说话(``POST /api/translate``),板子再转发给真正干活的
provider。换一家只改 ``config.jsonc`` 的 ``translate.provider``,前端一行都不用动。

实现约束
--------
* **只用标准库**(:mod:`urllib`)。``requests`` 在 venv 里装着,但它**不在
  requirements.txt 里**(是别的包的传递依赖)—— 哪天上游升级把它带掉,翻译
  功能就会以 ImportError 的形式炸掉,所以这里不依赖它。
* **必须重试**。免费接口(MyMemory)实测会随机 SSL 握手超时 / 被限流,单次请求
  成功率大约八成,所以每次请求最多试 3 次,退避 1.5s / 4s。
"""
from __future__ import annotations

import json
import logging
import random
import time
import urllib.parse
import urllib.request

LOG = logging.getLogger("core.translate")

_UA = "wdnwebui/translate (board-side proxy)"


class TranslationError(RuntimeError):
    """翻译失败(网络不通 / 语言对不支持 / 返回格式不对)。"""


class QuotaExceeded(TranslationError):
    """免费额度用尽。

    与「网络不通」必须分成两个异常:前者要等,后者可以立刻重试 —— 混在一起
    的话前端只能给出「翻译失败」这种没法指导用户下一步的提示。
    """


class TranslationProvider:
    """provider 接口。子类实现 :meth:`translate`。"""

    name = "base"

    def translate(self, text: str, source: str, target: str) -> str:
        """把 ``text`` 从 ``source`` 翻成 ``target``。"""
        raise NotImplementedError


# MyMemory 用 ``responseDetails`` 报配额 / 限流,这几个串要挑出来单独报
_QUOTA_MARKERS = (
    "USED ALL AVAILABLE FREE TRANSLATIONS",
    "TOO MANY REQUESTS",
    "QUOTA",
    "RATE LIMIT",
)


class MyMemoryProvider(TranslationProvider):
    """MyMemory(``api.mymemory.translated.net``)—— 免 key、覆盖 90+ 语言。

    实测(本板,2026-11):可达、1~3s/次;单次 query 上限 **500 字符**(超了返回
    403 ``QUERY LENGTH LIMIT EXCEEDED``);匿名用户每日 5000 词。在
    ``config.jsonc`` 的 ``translate.email`` 里填邮箱可把配额提到 50000 词/天。

    语言码用 ISO 639-1 / RFC3066(``zh-CN`` / ``en`` / ``ja`` …)。注意
    ``auto`` **不是**合法源语言(实测返回 403 ``'AUTO' IS AN INVALID SOURCE
    LANGUAGE``),所以源语言必须写死成具体的码。
    """

    name = "mymemory"
    endpoint = "https://api.mymemory.translated.net/get"

    def __init__(self, email: str = "", timeout: float = 12.0, retries: int = 3):
        self.email = (email or "").strip()
        self.timeout = timeout
        self.retries = max(1, int(retries))

    def translate(self, text: str, source: str, target: str) -> str:
        params = {"q": text, "langpair": f"{source}|{target}"}
        if self.email:
            # 带邮箱 = 配额 5k → 50k 词/天
            params["de"] = self.email
        url = self.endpoint + "?" + urllib.parse.urlencode(params)
        body = self._get(url)

        try:
            data = json.loads(body)
        except ValueError as exc:
            raise TranslationError(
                f"翻译服务返回了非 JSON 内容: {body[:120]!r}"
            ) from exc

        details = str(data.get("responseDetails") or "")
        upper = details.upper()
        if any(marker in upper for marker in _QUOTA_MARKERS):
            raise QuotaExceeded(f"翻译服务免费额度已用尽({details[:120]})")

        status = data.get("responseStatus")
        translated = (data.get("responseData") or {}).get("translatedText")
        try:
            ok = int(status) == 200
        except (TypeError, ValueError):
            ok = status is None
        if not ok:
            # 403 常见两种:超长(QUERY LENGTH LIMIT) / 配额。配额已在上面挑走,
            # 到这里的 403 基本都是「这次请求太长了」—— 调用方要负责分批。
            detail = details or str(translated or "")[:160]
            raise TranslationError(f"翻译服务返回错误(status={status}): {detail[:160]}")
        if not translated:
            raise TranslationError(
                f"翻译服务没有返回译文(status={status}, details={details[:120]})"
            )
        return str(translated)

    def _get(self, url: str) -> str:
        """带重试的 GET。免费接口随机抽风,单次失败不代表这条路不通。"""
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": _UA, "Accept": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read().decode("utf-8", "replace")
            except Exception as exc:  # 网络层什么都可能抛(超时 / SSL / DNS)
                last = exc
                LOG.warning(
                    "translate: provider=%s 第 %d/%d 次请求失败: %s",
                    self.name, attempt, self.retries, exc,
                )
                if attempt < self.retries:
                    # 退避别太长:一次界面翻译通常只有几批,每批都等 30s 的话
                    # 用户看到的就是"转圈转到超时"。
                    time.sleep(0.8 * attempt + random.random() * 0.4)
        raise TranslationError(f"翻译服务不可达: {last}")


# provider 注册表 —— ``config.jsonc`` 的 ``translate.provider`` 按名字取
PROVIDERS = {
    MyMemoryProvider.name: MyMemoryProvider,
}


def build_provider(name: str = "mymemory", email: str = "") -> TranslationProvider:
    """按名字造一个 provider;不认识的名字回退到 mymemory。"""
    key = (name or "mymemory").strip().lower()
    cls = PROVIDERS.get(key)
    if cls is None:
        LOG.warning("translate: 未知的 provider %r,回退 mymemory", name)
        cls = MyMemoryProvider
    return cls(email=email)
