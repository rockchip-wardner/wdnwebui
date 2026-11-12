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

import hashlib
import json
import logging
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

LOG = logging.getLogger("core.translate")

_UA = "wdnwebui/translate (board-side proxy)"

# 有些站点会按 UA 掐人(有道/中转站),这两个 provider 用浏览器 UA
_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


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

# 「NEXT AVAILABLE IN  23 HOURS 16 MINUTES 08 SECONDS」→ 给用户看的中文提示
_RESET_RE = re.compile(
    r"NEXT AVAILABLE IN\s+(\d+)\s+HOURS?\s+(\d+)\s+MINUTES?", re.IGNORECASE
)


def _quota_message(details: str) -> str:
    """把 provider 的配额提示翻成一句用户看得懂的话。

    光说「额度用尽」没用 —— 用户要知道**什么时候恢复**、**怎么才能不中断**。
    """
    m = _RESET_RE.search(details or "")
    if m:
        hours, minutes = int(m.group(1)), int(m.group(2))
        return (
            f"翻译服务今日免费额度已用尽,约 {hours} 小时 {minutes} 分钟后恢复;"
            "在 config.jsonc 填 translate.email 可把配额提到 5 万词/天"
        )
    return "翻译服务今日免费额度已用尽,请稍后再试"


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

    def __init__(self, email: str = "", timeout: float = 10.0, retries: int = 3):
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
            raise QuotaExceeded(_quota_message(details))

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
            except urllib.error.HTTPError as exc:
                # 429 = 被限流(配额/频率)。**不重试**:重试只会让限流更严重,
                # 而且等下来还是 429。直接判成配额问题,让上层走冷却逻辑。
                if exc.code == 429:
                    body = ""
                    try:
                        body = exc.read().decode("utf-8", "replace")
                    except Exception:
                        pass
                    detail = ""
                    try:
                        detail = str(json.loads(body).get("responseDetails") or "")
                    except Exception:
                        detail = body
                    if detail:
                        raise QuotaExceeded(_quota_message(detail)) from exc
                    raise QuotaExceeded(
                        "翻译服务限流了(HTTP 429),请稍后再试"
                    ) from exc
                last = exc
            except Exception as exc:  # 网络层什么都可能抛(超时 / SSL / DNS)
                last = exc
            LOG.warning(
                "translate: provider=%s 第 %d/%d 次请求失败: %s",
                self.name, attempt, self.retries, last,
            )
            if attempt < self.retries:
                # 退避别太长:一次界面翻译通常只有几批,每批都等 30s 的话
                # 用户看到的就是"转圈转到超时"。
                time.sleep(0.8 * attempt + random.random() * 0.4)
        raise TranslationError(f"翻译服务不可达: {last}")


class BaiduProvider(TranslationProvider):
    """百度翻译开放平台官方 API(需要 appid + key)。

    为什么不是百度**网页版**那个免 key 接口:那个接口的 token 藏在混淆后的
    SPA bundle 里、还带反爬(实测直接打 ``v2transapi`` 返回 ``errno 1022``),
    逆向成本高、百度一改就废。官方 API 反而稳,而且免费额度 100 万字符/月,
    对国内用户最实用 —— 拿个 key 填进 config 就能当 MyMemory 的备选。

    百度用的语言码**不是** ISO(``jp`` 不是 ``ja``、``kor`` 不是 ``ko``…),
    所以有一张映射表:见 :data:`BAIDU_LANG_CODES`。
    """

    name = "baidu"
    endpoint = "https://fanyi-api.baidu.com/api/trans/vip/translate"

    # 百度语言码 ← 我们的 ISO 码
    BAIDU_LANG_CODES = {
        "zh-CN": "zh", "zh-TW": "cht", "en": "en", "ja": "jp", "ko": "kor",
        "fr": "fra", "de": "de", "es": "spa", "pt": "pt", "it": "it",
        "ru": "ru", "ar": "ara", "hi": "hin", "th": "th", "vi": "vie",
        "id": "id", "ms": "ms", "tr": "tr", "pl": "pl", "nl": "nl",
        "sv": "swe", "da": "dan", "no": "nor", "fi": "fin", "cs": "cs",
        "el": "el", "he": "he", "uk": "ukr", "hu": "hu",
    }

    def __init__(self, appid: str = "", key: str = "", timeout: float = 10.0,
                 retries: int = 2):
        self.appid = (appid or "").strip()
        self.key = (key or "").strip()
        self.timeout = timeout
        self.retries = max(1, int(retries))

    def _code(self, iso: str) -> str:
        return self.BAIDU_LANG_CODES.get(iso, iso)

    def translate(self, text: str, source: str, target: str) -> str:
        if not (self.appid and self.key):
            raise TranslationError("百度翻译没配 appid/key(填在 config.jsonc 的 translate.baidu)")
        salt = str(int(time.time() * 1000))
        sign = hashlib.md5(
            (self.appid + text + salt + self.key).encode("utf-8")
        ).hexdigest()
        body = {
            "q": text,
            "from": self._code(source),
            "to": self._code(target),
            "appid": self.appid,
            "salt": salt,
            "sign": sign,
        }
        raw = self._post(body)
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise TranslationError(f"百度翻译返回了非 JSON 内容: {raw[:120]!r}") from exc
        if "error_code" in data:
            code = data.get("error_code")
            msg = str(data.get("error_msg") or "")
            if str(code) in ("54001", "54003", "54004", "54005"):
                raise QuotaExceeded(f"百度翻译配额/频率超限({code} {msg})")
            raise TranslationError(f"百度翻译返回错误({code}: {msg})")
        results = data.get("trans_result") or []
        if not results:
            raise TranslationError(f"百度翻译没有返回译文: {raw[:120]}")
        # 百度对长文本会切片,按 src 顺序拼回去
        return "".join(str(r.get("dst") or "") for r in results)

    def _post(self, body: dict) -> str:
        data = urllib.parse.urlencode(body).encode("utf-8")
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                req = urllib.request.Request(
                    self.endpoint, data=data,
                    headers={"User-Agent": _UA, "Content-Type": "application/x-www-form-urlencoded"},
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read().decode("utf-8", "replace")
            except Exception as exc:  # 网络层什么都可能抛
                last = exc
                LOG.warning(
                    "translate: provider=baidu 第 %d/%d 次请求失败: %s",
                    attempt, self.retries, exc,
                )
                if attempt < self.retries:
                    time.sleep(0.8 * attempt)
        raise TranslationError(f"百度翻译不可达: {last}")


# --------------------------------------------------------------------------- #
# 有道 / 金山词霸
# --------------------------------------------------------------------------- #

# 有道前端 JS 里的**固定**签名常量 —— 不是用户 key,不用配置。
#
# 注意:真正干活的是新版 SSE 接口 ``dict-trans.youdao.com/webtranslate/sse``。
# 旧接口 ``dict.youdao.com/webtranslate`` 的响应虽然能解密(AES-128-CBC),
# 但实测对非浏览器客户端**只返回随机诱饵译文**(译文跟原文毫无关系),是
# 有意为之的反爬 —— 别再往那个方向钻了。
_YOUDAO_KEY_SECRET = "kSy5gtKA4yRUxAVPJPrdYKZ0jBKyd3t1"
_YOUDAO_KEY_ID = "translate-webmain-key-getter"
_YOUDAO_TARGET_KEY_ID = "translate-webfanyi-webmain"

# 限流类提示的关键词 —— 命中就当配额问题(长冷却),不当作普通网络错误
_QUOTA_HINTS = ("限流", "频繁", "QUOTA", "LIMIT", "TOO MANY", "429", "超出")


def _is_quota_hint(text: str) -> bool:
    upper = (text or "").upper()
    return any(hint in upper for hint in _QUOTA_HINTS)


def _js_str(value) -> str:
    """JS ``String(value)`` 语义 —— 签名要对齐前端就不能自己发明格式。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class YoudaoProvider(TranslationProvider):
    """有道 / 金山词霸 网页翻译(``dict-trans.youdao.com``)—— **免 key**。

    实测(本板,2026-11):

    * 直连可达,稳定 **0.1~0.9s/次**(密钥复用后甚至 0.12s);
    * 编号列表会**原样保留行首编号和换行**(实测 ``1. xxx\\n2. yyy`` 回传
      ``1. The system...\\n2. The network...``)—— 正好是分批算法要的;
    * 长文本会明显变慢(2100 字 → 15s),所以还是按 480 字分批;
    * 12 个语种实测全部正确(中→英/日/韩/法/德/俄/阿/泰/越、简→繁、英/日→中)。

    **为什么免 key**:签名密钥 ``_YOUDAO_KEY_SECRET`` 是前端 JS 里的固定常量,
    真正的 ``secretKey``/``token`` 由服务端匿名下发(``POST /translate/key``),
    用户不需要配任何东西。下发的密钥会缓存 30 分钟复用(每次重新申请也就
    0.1s,但没必要)。

    **源语言是服务端自动识别的**,``from`` 只是建议值;``to`` 决定目标语言。
    """

    name = "youdao"
    host = "https://dict-trans.youdao.com"

    # 我们的 ISO 码 → 有道码。中文必须写 zh-CHS / zh-CHT,其余与 ISO 一致。
    LANG_CODES = {
        "zh-CN": "zh-CHS",
        "zh-TW": "zh-CHT",
    }

    # 匿名下发的密钥缓存多久(秒)
    SECRET_TTL = 1800.0

    def __init__(self, timeout: float = 30.0, retries: int = 2):
        self.timeout = timeout
        self.retries = max(1, int(retries))
        self._lock = threading.Lock()
        # (secretKey, token, yduuid, 过期时间戳)
        self._session: tuple[str, str, str, float] | None = None

    # ------------------------------------------------------------------ #
    # 对外
    # ------------------------------------------------------------------ #
    def translate(self, text: str, source: str, target: str) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        src = self.LANG_CODES.get(source, source)
        dst = self.LANG_CODES.get(target, target)
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            secret, token, yduuid = self._session_data(force=attempt > 1)
            try:
                return self._translate_once(text, src, dst, secret, token, yduuid)
            except QuotaExceeded:
                raise
            except TranslationError as exc:
                last = exc
                # 密钥过期是最常见的失败原因 —— 丢掉缓存重新匿名申请一次。
                self._drop_session()
                if attempt < self.retries:
                    time.sleep(0.5 * attempt)
        raise TranslationError(f"有道翻译不可达: {last}")

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _session_data(self, force: bool = False) -> tuple[str, str, str]:
        """取(secretKey, token, yduuid);默认走缓存,``force`` 强制重新申请。"""
        with self._lock:
            if not force and self._session and self._session[3] > time.time():
                return self._session[0], self._session[1], self._session[2]
        secret, token, yduuid = self._fetch_secret()
        with self._lock:
            self._session = (secret, token, yduuid, time.time() + self.SECRET_TTL)
        return secret, token, yduuid

    def _drop_session(self) -> None:
        with self._lock:
            self._session = None

    def _fetch_secret(self) -> tuple[str, str, str]:
        """匿名申请密钥(不需要用户配 key)。"""
        yduuid = str(uuid.uuid4())
        params = self._signed_params(
            {"keyid": _YOUDAO_KEY_ID, "targetKeyid": _YOUDAO_TARGET_KEY_ID},
            _YOUDAO_KEY_SECRET,
            _YOUDAO_KEY_ID,
            yduuid,
        )
        # 前端用的是 axios.post —— 这里用 GET 会拿到 500。
        url = (
            self.host + "/translate/key?"
            + "&".join(f"{k}={_js_str(v)}" for k, v in params.items())
        )
        raw = self._http(url, method="POST")
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise TranslationError(f"有道密钥接口返回了非 JSON 内容: {raw[:120]!r}") from exc
        if data.get("code") != 0:
            raise TranslationError(f"有道密钥接口返回异常: {raw[:160]}")
        payload = data.get("data") or {}
        secret = str(payload.get("secretKey") or "")
        token = str(payload.get("token") or "")
        if not secret:
            raise TranslationError(f"有道密钥接口没给密钥: {raw[:160]}")
        return secret, token, yduuid

    def _translate_once(
        self, text: str, src: str, dst: str, secret: str, token: str, yduuid: str
    ) -> str:
        params = self._signed_params(
            {
                "modelName": "llmLite",
                "useTerm": False,
                # 前端先 encodeURIComponent 再放进 FormData —— 服务端会再解一次
                "i": urllib.parse.quote(text),
                "from": src,
                "to": dst,
                "yduuid": yduuid,
                "signSecretKey": secret,
                "keyId": _YOUDAO_TARGET_KEY_ID,
                "product": "webfanyi",
                "keyfrom": "webfanyi.webmain",
                "client": "webmain",
                "token": token,
                "source": "webmain",
            },
            secret,
            _YOUDAO_TARGET_KEY_ID,
            yduuid,
        )
        body, ctype = self._multipart(params)
        raw = self._http(
            self.host + "/webtranslate/sse",
            data=body,
            headers={"Content-Type": ctype},
            method="POST",
        )
        return self._parse_sse(raw)

    @staticmethod
    def _signed_params(
        params: dict, secret: str, key_id: str, yduuid: str
    ) -> dict:
        """复刻前端 genParamV3:固定公共参数 + 业务参数,再算 sign。"""
        merged: dict = {
            "product": "webfanyi",
            "appVersion": "12.0.0",
            "client": "web",
            "mid": 1,
            "vendor": "web",
            "screen": 1,
            "model": 1,
            "imei": 1,
            "network": "wifi",
            "keyfrom": "webfanyi.webmain",
            "keyid": key_id,
            "mysticTime": int(time.time() * 1000),
            "yduuid": yduuid,
            "abtest": 0,
        }
        merged.update(params)
        # 签名:按 key 排序,末尾追加 key=<密钥>,整体 MD5
        signable = {k: _js_str(v) for k, v in merged.items() if v != "" and v is not None}
        keys = sorted(signable.keys())
        keys.append("key")
        signable["key"] = _js_str(secret)
        raw = "&".join(f"{k}={signable[k]}" for k in keys)
        merged["sign"] = hashlib.md5(raw.encode("utf-8")).hexdigest()
        merged["pointParam"] = ",".join(keys)
        return merged

    @staticmethod
    def _multipart(fields: dict) -> tuple[bytes, str]:
        """拼 multipart/form-data(前端用的是 FormData)。"""
        boundary = "----WebKitFormBoundary" + uuid.uuid4().hex
        parts: list[str] = []
        for key, value in fields.items():
            if value == "" or value is None:
                continue
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{_js_str(value)}\r\n"
            )
        body = ("".join(parts) + f"--{boundary}--\r\n").encode("utf-8")
        return body, f"multipart/form-data; boundary={boundary}"

    @staticmethod
    def _parse_sse(raw: str) -> str:
        """从 SSE 文本里拼出译文。"""
        chunks: list[str] = []
        event = ""
        for line in (raw or "").splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                if event == "message" and obj.get("transIncre"):
                    chunks.append(str(obj["transIncre"]))
                elif event == "error":
                    msg = str(obj.get("msg") or data)
                    if _is_quota_hint(msg):
                        raise QuotaExceeded(f"有道翻译限流了: {msg[:120]}")
                    raise TranslationError(f"有道翻译返回错误: {msg[:160]}")
                elif event == "end":
                    msg = str(obj.get("msg") or "")
                    if msg:
                        raise TranslationError(f"有道翻译中断: {msg[:160]}")
        out = "".join(chunks).strip()
        if not out:
            raise TranslationError("有道翻译没有返回译文")
        return out

    def _http(
        self,
        url: str,
        data: bytes | None = None,
        headers: dict | None = None,
        method: str | None = None,
    ) -> str:
        hdrs = {
            "User-Agent": _BROWSER_UA,
            "Accept": "*/*",
            "Referer": "https://fanyi.youdao.com/",
            "Origin": "https://fanyi.youdao.com",
        }
        if headers:
            hdrs.update(headers)
        try:
            req = urllib.request.Request(
                url, data=data, headers=hdrs,
                method=method or ("POST" if data is not None else "GET"),
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            if exc.code == 429:
                raise QuotaExceeded("有道翻译限流了(HTTP 429)") from exc
            raise TranslationError(
                f"有道翻译返回 HTTP {exc.code}: {body[:160]}"
            ) from exc
        except Exception as exc:
            raise TranslationError(f"有道翻译网络错误: {exc}") from exc


class IcibaProvider(TranslationProvider):
    """金山词霸 —— 经 SimplyTranslate 公共实例的 ``iciba`` 引擎。

    这是**通往金山词霸的第二条路**:除了直连有道
    (:class:`YoudaoProvider`),还有这条经中转站的。两条路的配额、网络路径
    都不同 —— 一条被限流/抽风时,另一条大概率还活着。

    引擎在**中转站那边**跑,本板只需要能访问中转站。

    实测(本板,2026-11):``simplytranslate.org`` 可达,1.3~2.2s/次;中转站上
    的 google / bing / deepl 引擎实测都是空译文(它们自己够不到那些站点),
    **只有 iciba 引擎是通的**。中转站是第三方服务,随时可能消失 —— 所以它
    在链里排第二,后面还有 MyMemory 兜底。
    """

    name = "iciba"
    default_relay = "https://simplytranslate.org"

    # 我们的 ISO 码 → 金山词霸码
    LANG_CODES = {
        "zh-CN": "zh",
        "zh-TW": "zh-TW",
    }

    def __init__(
        self,
        relay: str = "",
        timeout: float = 20.0,
        retries: int = 2,
    ):
        self.relay = (relay or self.default_relay).rstrip("/")
        self.timeout = timeout
        self.retries = max(1, int(retries))

    def translate(self, text: str, source: str, target: str) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        params = {
            "engine": "iciba",
            "from": self.LANG_CODES.get(source, source),
            "to": self.LANG_CODES.get(target, target),
            "text": text,
        }
        url = self.relay + "/api/translate/?" + urllib.parse.urlencode(params)
        raw = self._get(url)
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise TranslationError(
                f"金山词霸中转站返回了非 JSON 内容: {raw[:120]!r}"
            ) from exc
        out = str(data.get("translated_text") or "").strip()
        if not out:
            raise TranslationError(
                f"金山词霸中转站没有返回译文: {raw[:160]}"
            )
        return out

    def _get(self, url: str) -> str:
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": _BROWSER_UA,
                        "Accept": "application/json",
                        "Referer": self.relay + "/",
                    },
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", "replace")
                except Exception:
                    pass
                if exc.code == 429:
                    raise QuotaExceeded("金山词霸中转站限流了(HTTP 429)") from exc
                if exc.code in (403, 401):
                    raise QuotaExceeded(
                        f"金山词霸中转站拒绝访问(HTTP {exc.code})"
                    ) from exc
                last = exc
            except Exception as exc:
                last = exc
            LOG.warning(
                "translate: provider=%s 第 %d/%d 次请求失败: %s",
                self.name, attempt, self.retries, last,
            )
            if attempt < self.retries:
                time.sleep(0.8 * attempt)
        raise TranslationError(f"金山词霸中转站不可达: {last}")


# provider 注册表 —— ``config.jsonc`` 的 ``translate.providers`` 按名字取
PROVIDERS = {
    MyMemoryProvider.name: MyMemoryProvider,
    BaiduProvider.name: BaiduProvider,
    YoudaoProvider.name: YoudaoProvider,
    IcibaProvider.name: IcibaProvider,
}


class ProviderChain:
    """按顺序尝试多个 provider —— 一个被限流/挂了就**自动换下一个**。

    这就是「429 了换个翻译网站」的实现:

    * 配额用尽(429 / QuotaExceeded)→ 该 provider 进入**长冷却**(默认 30 分钟),
      立刻试下一个;
    * 网络类失败(超时/SSL)→ 该 provider 进入**短冷却**(默认 60 秒),试下一个;
    * 全部 provider 都在冷却 → 直接报「都在冷却中」,不浪费时间去撞墙。

    冷却是为了**不再打同一个已经废掉的接口**:配额型失败(比如 MyMemory 的
    每日 5000 词)每 30 分钟试一次就够了,试中了也是 429。
    """

    def __init__(self, providers: list[TranslationProvider],
                 quota_cooldown: float = 1800.0,
                 error_cooldown: float = 60.0):
        self._providers = list(providers)
        self._quota_cooldown = quota_cooldown
        self._error_cooldown = error_cooldown
        self._until: dict[str, float] = {}
        self._lock = threading.Lock()
        self.last_used: str = ""

    @property
    def names(self) -> list[str]:
        return [p.name for p in self._providers]

    def cooldown_state(self) -> dict[str, int]:
        """每个 provider 还要冷却多少秒(给 /status 看)。"""
        now = time.time()
        with self._lock:
            return {
                name: max(0, int(until - now))
                for name, until in self._until.items()
                if until > now
            }

    def _available(self) -> list[TranslationProvider]:
        now = time.time()
        with self._lock:
            return [p for p in self._providers if self._until.get(p.name, 0) <= now]

    def _mark(self, provider: TranslationProvider, seconds: float) -> None:
        with self._lock:
            self._until[provider.name] = time.time() + seconds

    def translate(self, text: str, source: str, target: str) -> str:
        available = self._available()
        if not available:
            raise QuotaExceeded(
                "所有翻译服务都在冷却中(刚被限流),请稍后再试"
            )
        last: Exception | None = None
        for provider in available:
            try:
                out = provider.translate(text, source, target)
                self.last_used = provider.name
                return out
            except QuotaExceeded as exc:
                self._mark(provider, self._quota_cooldown)
                last = exc
                LOG.warning(
                    "translate: provider=%s 配额用尽,切下一个: %s",
                    provider.name, exc,
                )
                continue
            except TranslationError as exc:
                self._mark(provider, self._error_cooldown)
                last = exc
                LOG.warning(
                    "translate: provider=%s 请求失败,切下一个: %s",
                    provider.name, exc,
                )
                continue
        # 全挂了:把**最后一个**错误抛出去(通常是最有信息量的那个)
        raise last if last else TranslationError("没有可用的翻译服务")


def build_provider(name: str = "youdao", email: str = "") -> TranslationProvider:
    """按名字造一个 provider;不认识的名字回退到 youdao。"""
    key = (name or "youdao").strip().lower()
    cls = PROVIDERS.get(key)
    if cls is None:
        LOG.warning("translate: 未知的 provider %r,回退 youdao", name)
        cls = YoudaoProvider
    if cls is MyMemoryProvider:
        return cls(email=email)
    return cls()


# 默认的故障转移顺序:
#   有道(直连、免 key、最快) → 金山词霸(经中转站) → MyMemory(免 key 但每日限词)
# 百度官方 API 需要 key,得用户自己配了才会进链。
DEFAULT_PROVIDERS = ["youdao", "iciba", "mymemory"]

# 给界面看的中文名(前端显示用)
PROVIDER_LABELS = {
    "youdao": "有道",
    "iciba": "金山词霸",
    "mymemory": "MyMemory",
    "baidu": "百度",
}


def build_chain(
    names: list[str],
    email: str = "",
    baidu: dict | None = None,
    iciba: dict | None = None,
) -> ProviderChain:
    """按配置造一条 provider 链。

    ``names`` 是**顺序**(第一个是主力,后面的是备选)。没配 appid/key 的
    provider 会被跳过 —— 总得让用户知道它没生效,而不是运行时报一句
    「没配 key」。
    """
    baidu = baidu or {}
    iciba = iciba or {}
    providers: list[TranslationProvider] = []
    for raw in names or DEFAULT_PROVIDERS:
        name = str(raw).strip().lower()
        if name == "mymemory":
            providers.append(MyMemoryProvider(email=email))
        elif name == "youdao":
            providers.append(YoudaoProvider())
        elif name == "iciba":
            relay = str(iciba.get("relay") or "")
            providers.append(IcibaProvider(relay=relay))
        elif name == "baidu":
            appid = str(baidu.get("appid") or "")
            key = str(baidu.get("key") or "")
            if appid and key:
                providers.append(BaiduProvider(appid=appid, key=key))
            else:
                LOG.warning(
                    "translate: provider=baidu 没配 appid/key,已从链里跳过"
                )
        else:
            LOG.warning("translate: 未知的 provider %r,已跳过", raw)
    if not providers:
        # 一个都没配上 —— 至少留个 youdao,免得整个功能直接没了
        LOG.warning("translate: 没有可用的 provider,回退 youdao")
        providers.append(YoudaoProvider())
    return ProviderChain(providers)
