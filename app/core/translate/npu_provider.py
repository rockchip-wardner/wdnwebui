"""NPU 翻译 provider —— 用本板 NPU(RK1828)上的 HY-MT1.5 做翻译。

为什么要有这一路
----------------
网页右上角那个「界面语言」下拉（``app/static/core/translate.js``）走的是
``POST /api/translate``，默认落到三家**外网**翻译站（有道 / 金山词霸 /
MyMemory）。在国内网络里这三家都不稳：免费额度按天算、随机限流、随时
抽风。板子上明明装着一颗能跑的翻译模型（``swcap_translate`` / HY-MT1.5），
却没接到这条链上 —— 这一路就是把它接进来。

实测（RK1828 单卡，worker + DDS）：
* 单句 **0.11~0.50s**（外网 provider 是 1~3s，而且一批 24 条要 1~3 分钟）
* 贪心解码（``top_k=1`` / ``temperature=1e-6``）—— 翻译要忠实原文，
  采样噪声会漏译或重译

五个必须踩准的坑（都是实测出来的，不是推测）
--------------------------------------------
1. **「按码写」还是「按名写」，**取决于语言**。

   模型自己的 GGUF 元数据 ``general.languages`` 列了它认的语言**码**
   （``zh en fr pt es ja tr ru ar ko th it de vi ms id hi pl cs nl he uk …``）。
   拉丁字母系的语言（en/fr/de/es/pt/nl/pl…）用**码**写指令它一听就懂；
   但**非拉丁字母系**的用码写，它经常**译到别的语言去**：

       ``zh→hi`` 用码写 → 回**英文**（"hi" 被当成了问候语！）
       ``zh→ru`` 用码写 → 回**韩语**/中文
       ``zh→th`` 用码写 → 回中文/泰米尔语
       ``zh→uk`` 用码写 → 回**中文**（0/24 命中）

   这些语言得**按名字写**（"Translate from Chinese to Russian:"）才译得对。
   ⇒ 所以措辞顺序是**按语言定制**的（:data:`PROMPT_ORDER`，实测表）。

   实测（24 条界面文案 × 5 条措辞，「第一条就翻对」的条目数）::

       语言   原顺序(先码后名)   按本表
       hi            0/24          24/24
       uk            0/24          24/24
       ko            6/24          24/24
       ru            0/24          22/24
       th            2/24          23/24
       fr           22/24          24/24

   「成功率」两者一样（5 条全试也就 24/24），差的是**推理次数** ——
   而推理次数就是端到端耗时。

2. **没有哪条指令是 100% 可靠的** —— 同一个模型同一句话，换个措辞就
   可能不翻（直接把中文原文微调一下吐回来）。所以按实测成功率排
   一串措辞，逐个试，并用**文种校验 + 去标签 + 复读检测**判定哪一次
   真的翻成了目标语言。

   ⚠️ **翻不出来的那一条，只牵连它自己**（2026-11-16 修）。以前任何一条
   失败都让**整批**抛异常 → 链把整批 24 条交给外网 → NPU 已经跑完的 23 条
   **全白跑**。现在这条保留原文、单独记进 ``misses``，由上层**只补翻这
   几条**（:class:`~app.core.translate.base.TranslationResult`）。

   界面文案里天然混着 NPU 翻不出来的条目：

   * **语言名本身**：``简体中文`` 译成英文还是中文、译成日文是纯汉字
     （没有假名）→ 文种判定判它「没翻出来」；
   * **超短术语**：``外网翻译站`` / ``本板 NPU 翻译`` 这类 4~6 个字的词，
     模型经常把原文原样吐回来。

3. **编号列表整批翻会丢条目**。``TranslateService`` 默认把一批文案拼成
   ``"1. xxx\n2. yyy"`` 发出去（MyMemory 会保留行首编号）。HY-MT 不行：
   实测 3 条的编号列表只吐出第 1 条。所以本 provider **自己拆编号、
   逐条翻、再按原样拼回编号**。

4. **「语言不支持」是确定性的失败，不能给它记冷却**。

   provider 链拿到普通的 ``TranslationError`` 会给该 provider 记 60 秒
   冷却，冷却期里它被**静默**剔除 —— 于是「这个语言不支持」会**连累别的
   语言**：接下来一分钟所有翻译都走了外网，而日志里一行告警都没有。

   ⇒ 不支持的语言抛 :class:`~app.core.translate.base.UnsupportedLanguageError`，
   链会换下一家但**不记冷却**（实测 6 种语言不支持，每次都把 NPU 冷却掉）。

5. **模型词表里简体和繁体是同一个码**（都是 ``zh``）—— 它分不出来。

   实测让模型转繁体的结果是「**半繁体半简体**」（"网页翻译" →
   "网页翻译服务"，「网」还是简体）—— 比不转更糟。⇒ 简↔繁明确判成
   「NPU 不支持」，交给外网（有道 ``zh-CHS→zh-CHT`` 是真转换），**零推理
   开销**。

为什么质量类失败要单独一个异常类
--------------------------------
``ProviderChain`` 对 ``QuotaExceeded``（长冷却）和 ``TranslationError``
（短冷却）处理不同，但**两者都会切到链上的下一家**。而这里要分**三层**：

* **推理级故障**（worker 挂了 / 超时）→ provider 级的毛病 → 抛
  ``TranslationError`` → 整批换下一家 + 记冷却（对）。
* **质量级失败**（这句它就是翻不出来）→ **内容级**问题 → 抛
  :class:`NpuQualityError` → 只牵连这一条，**不记冷却**（对）。
* **语言不支持** → 确定性 → 抛 ``UnsupportedLanguageError`` → 换下一家，
  **不记冷却**（对）。

⚠️ 以前把后两种也当成 ``TranslationError``，于是「一条不合格」升级成
「一分钟内所有语言都走外网」—— 那才是「界面切了语言却慢得要死、而且
看不出为什么」的真正成因。
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from app.core.translate.base import (
    TranslationError,
    TranslationProvider,
    TranslationResult,
    UnsupportedLanguageError,
)

LOG = logging.getLogger("core.translate")


class NpuQualityError(TranslationError):
    """模型**翻是翻了,但没翻对**(复读原文 / 方向搞反 / 文种不对)。

    与「provider 挂了」必须分开:

    * 「推理失败」(worker 挂了 / 超时) → provider 级的故障 → 该给它记冷却,
      整批换下一家;
    * 「质量不行」(这句它就是翻不出来) → **内容级**的问题 → 只能把**这一条**
      交给外网补翻,不能牵连同批其它条,更不能给整个 provider 记冷却
      (否则这一分钟内所有语言都走外网)。

    ⚠️ 这个区分就是「一条不合格拖垮 24 条」那个 bug 的根因。

    ⚠️ **带着 ``perf``**（2026-11-17）：这条翻不出来之前**已经烧掉了几次
    推理**（每次试一条措辞），那些 token 和时间是真花掉了。抛异常时把它
    带上 ⇒ 上层能把这次**失败的**推理也计进性能汇总。

    不带的话：界面上的「总耗时」算了两次推理，而「tokens/s」只算了成功
    那一次 ⇒ 两个数对不上，用户自己一除就觉得界面在骗人。
    """

    def __init__(self, message: str, perf: Optional[Dict[str, float]] = None):
        super().__init__(message)
        #: 这条翻不出来之前**已经烧掉**的推理性能表（原始计数 + 派生量）。
        self.perf: Dict[str, float] = dict(perf or {})

# --------------------------------------------------------------------------- #
# 语言表
# --------------------------------------------------------------------------- #
#
# ``LANG_CODES`` —— 模型 ``general.languages`` 里的**码**。指令里用码最稳
# （见文件头坑 1）。不在表里的语言不在模型支持范围内 —— 那 6 种
# （sv/da/no/fi/el/hu）压根没有码，:meth:`_check_pair` 会判成「不支持」。
#
# ⚠️ ``zh-TW`` 映射到 ``zh``：**只能当源语言用**（繁体中文 → 其它语言，
# 模型能翻）。当**目标**语言是不行的 —— 简繁共用一个码，模型分不出来
# （:meth:`_check_pair` 里 ``csrc == ctgt`` 那条会把 zh-CN→zh-TW 判成
# 「不支持」）。界面语言下拉里也就没有繁体中文这一项了。
LANG_CODES: Dict[str, str] = {
    "zh-CN": "zh",
    "zh-TW": "zh",
    "en": "en",
    "fr": "fr",
    "pt": "pt",
    "es": "es",
    "ja": "ja",
    "tr": "tr",
    "ru": "ru",
    "ar": "ar",
    "ko": "ko",
    "th": "th",
    "it": "it",
    "de": "de",
    "vi": "vi",
    "ms": "ms",
    "id": "id",
    "tl": "tl",
    "hi": "hi",
    "pl": "pl",
    "cs": "cs",
    "nl": "nl",
    "km": "km",
    "my": "my",
    "fa": "fa",
    "gu": "gu",
    "ur": "ur",
    "te": "te",
    "mr": "mr",
    "he": "he",
    "bn": "bn",
    "ta": "ta",
    "uk": "uk",
    "bo": "bo",
    "kk": "kk",
    "mn": "mn",
    "ug": "ug",
}

#: 语言**名** —— 只在码那几条都失败后兜底用（按名写容易带标签）。
#:
#: ⚠️ 表里**只有模型词表里有的语言**（外加 ``zh-TW``，它是**源**语言时
#: 要用到）。模型词表里没有的那几种（sv/da/no/fi/el/hu）连名字都不该有 ——
#: 它们在 :func:`NpuTranslateProvider._check_pair` 那里就被判成「不支持」
#: 了，走外网。
LANG_NAMES: Dict[str, str] = {
    "zh-CN": "Chinese",
    "zh-TW": "Traditional Chinese",
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "pt": "Portuguese",
    "it": "Italian",
    "ru": "Russian",
    "ar": "Arabic",
    "hi": "Hindi",
    "th": "Thai",
    "vi": "Vietnamese",
    "id": "Indonesian",
    "ms": "Malay",
    "tr": "Turkish",
    "pl": "Polish",
    "nl": "Dutch",
    "cs": "Czech",
    "he": "Hebrew",
    "uk": "Ukrainian",
    # —— 模型词表里有、但这张表**原本漏掉**的 14 种 ——
    #
    # ⚠️ 漏掉它们的代价：``_check_pair`` 以前要求「源和目标的**名字**都得
    # 有」，于是这 14 种语言被误判成「NPU 不支持」→ 整批丢给外网。
    # 名字只是**兜底措辞**用的（码写指令才是主力），不该拿它当准入条件。
    "tl": "Tagalog",
    "km": "Khmer",
    "my": "Burmese",
    "fa": "Persian",
    "gu": "Gujarati",
    "ur": "Urdu",
    "te": "Telugu",
    "mr": "Marathi",
    "bn": "Bengali",
    "ta": "Tamil",
    "bo": "Tibetan",
    "kk": "Kazakh",
    "mn": "Mongolian",
    "ug": "Uyghur",
}

#: 目标语言期望的**文种**（用来判定模型到底翻没翻）。
#:
#: 没有这一层的话，「模型没翻、直接把原文改两个字吐回来」会被当成成功 ——
#: 而界面上看到的就是「界面语言切了但还是中文」，比报错还难查。
TARGET_SCRIPTS: Dict[str, Tuple[str, ...]] = {
    "ja": ("kana",),        # 日文可以带汉字，但**必须**有假名
    "ko": ("hangul",),
    "zh-CN": ("han",),
    "zh-TW": ("han",),
    "ru": ("cyrillic",),
    "uk": ("cyrillic",),
    "ar": ("arabic",),
    "he": ("hebrew",),
    "hi": ("deva",),
    "th": ("thai",),
    "bn": ("bengali",),
    "ta": ("tamil",),
    "hebrew": ("hebrew",),
}

#: 拉丁字母系的语言（英/法/德/西/……）—— 期望看到拉丁字母。
_LATIN_TARGETS = {
    "en", "fr", "de", "es", "pt", "it", "nl",
    "cs", "pl", "tr", "id", "ms", "vi", "tl",
}

# --------------------------------------------------------------------------- #
# 指令措辞（按实测成功率排序，逐个试）
# --------------------------------------------------------------------------- #
#
# 全部用**码**；名字那两条排在最后兜底。
_PROMPTS_CODE = (
    "Translate the following text from {csrc} to {ctgt}:\n{text}",
    "{csrc} -> {ctgt}:\n{text}",
    "Translate from {csrc} to {ctgt}:\n{text}",
)
_PROMPTS_NAME = (
    "Translate the following text from {nsrc} to {ntgt}:\n{text}",
    "Translate from {nsrc} to {ntgt}: {text}",
)

#: 全部措辞（``_PROMPTS_CODE`` + ``_PROMPTS_NAME``）—— 下标含义:
#:
#:   0  Translate the following text from {码} to {码}:
#:   1  {码} -> {码}:
#:   2  Translate from {码} to {码}:
#:   3  Translate the following text from {名} to {名}:
#:   4  Translate from {名} to {名}:
_ALL_PROMPTS: Tuple[str, ...] = _PROMPTS_CODE + _PROMPTS_NAME

#: 没单独测过的语言用这个顺序。
DEFAULT_PROMPT_ORDER: Tuple[int, ...] = (0, 1, 2, 3, 4)

#: **每种语言的措辞顺序** —— 实测出来的（2026-11-16）。
#:
#: 为什么不能「一律先码后名」
#: ------------------------------
#: 模型对**裸语言码**的反应很看语言。拉丁字母系的（en/fr/de/es/pt…）用码
#: 它一听就懂；但**非拉丁字母系**的用码写指令，它经常**译到别的语言去**
#: （实测 ``zh→hi`` 用码写它回英文 —— "hi" 被当成了问候语；
#: ``zh→ru`` 用码写它回韩语/中文；``zh→th`` 用码写它回中文/泰米尔语）。
#:
#: 那些语言得**按名字写**才译得对 ⇒ 原来的「先码后名」顺序会让它们
#: **白烧 3 次推理**才轮到对的那条。
#:
#: 实测（24 条界面文案 × 5 条措辞，逐条按 provider 自己的判定打分）:
#:
#:   语言   原顺序第1条命中   按本表第1条命中
#:   hi          0/24              24/24
#:   uk          0/24              24/24
#:   ko          6/24              24/24
#:   ru          0/24              22/24
#:   th          2/24              23/24
#:   de         11/24              21/24
#:   tr         16/24              24/24
#:   fr         22/24              24/24
#:   nl         17/24              24/24
#:   ja         16/24              16/24
#:
#: 「命中」= 这条措辞**第一条就翻对**的条目数；其余条目靠后面的措辞兜底，
#: 所以**成功率不受影响**（原顺序 5 条全试也就 24/24），但**推理次数**
#: 差好几倍 —— 而推理次数就是端到端耗时。
#:
#: ⚠️ 这张表是**实测快照**，不是原理：模型换了权重就得重测
#: （脚本：``scripts/probe_prompt_order.py``）。
PROMPT_ORDER: Dict[str, Tuple[int, ...]] = {
    "ar": (4, 0, 1, 2, 3),
    "bo": (2, 1, 4, 0, 3),
    "cs": (4, 0, 1, 2, 3),
    "de": (4, 3, 0, 1, 2),
    "en": (0, 1, 2, 3, 4),
    "es": (2, 0, 1, 3, 4),
    "fa": (2, 0, 1, 4, 3),
    "fr": (2, 0, 1, 3, 4),
    "gu": (1, 2, 4, 0, 3),
    "he": (3, 4, 0, 1, 2),
    "hi": (4, 0, 1, 2, 3),
    "id": (4, 2, 0, 1, 3),
    "it": (2, 4, 0, 1, 3),
    "ja": (0, 2, 4, 1, 3),
    "kk": (2, 0, 1, 3, 4),
    "km": (0, 2, 1, 3, 4),
    "ko": (4, 0, 1, 2, 3),
    "mn": (0, 1, 4, 2, 3),
    "mr": (2, 1, 0, 3, 4),
    "ms": (4, 1, 2, 0, 3),
    "my": (2, 1, 0, 3, 4),
    "nl": (2, 0, 1, 3, 4),
    "pl": (0, 1, 2, 3, 4),
    "pt": (2, 0, 1, 3, 4),
    "ru": (4, 3, 0, 1, 2),
    "te": (2, 1, 0, 3, 4),
    "th": (4, 0, 1, 2, 3),
    "tl": (0, 4, 1, 2, 3),
    "tr": (4, 0, 1, 2, 3),
    "ug": (2, 1, 0, 3, 4),
    "uk": (4, 0, 1, 2, 3),
    "ur": (2, 0, 1, 3, 4),
    "vi": (2, 4, 0, 1, 3),
}

#: 行首编号 —— ``TranslateService`` 拼批时用的形状
_ITEM_RE = re.compile(r"^\s*(\d{1,3})\s*[.)、]?\s*(.*)$")

#: 「翻译标签」的判据：前缀里出现这些词/语言名之一，就把它当标签剥掉。
_LABEL_WORDS = (
    "translate", "translation", "übersetz", "traduc", "traduzione", "traduz",
    "перевод", "перекла", "翻譯", "翻译", "翻訳", "번역", "번역문",
    "convert", "轉換", "转换",
)


def _script_set(text: str) -> set:
    """粗粒度文种集合：``{"han", "kana", "latin", ...}``。"""
    out = set()
    for ch in text or "":
        if not ch.strip():
            continue
        try:
            name = unicodedata.name(ch)
        except ValueError:
            continue
        if "HIRAGANA" in name or "KATAKANA" in name:
            out.add("kana")
        elif "HANGUL" in name:
            out.add("hangul")
        elif "BENGALI" in name:
            out.add("bengali")
        elif "TAMIL" in name:
            out.add("tamil")
        elif "THAI" in name:
            out.add("thai")
        elif "DEVANAGARI" in name:
            out.add("deva")
        elif "HEBREW" in name:
            out.add("hebrew")
        elif "GREEK" in name:
            out.add("greek")
        elif "ARABIC" in name:
            out.add("arabic")
        elif "CYRILLIC" in name:
            out.add("cyrillic")
        elif "CJK" in name and "IDEOGRAPH" in name:
            out.add("han")
        elif "LATIN" in name:
            out.add("latin")
    return out


def _run_coro(coro):
    """把 session 的 async 接口桥到当前线程（与 ``app.core.chat.agent`` 同款）。

    ``/api/translate`` 是**同步**端点（FastAPI 丢线程池跑），所以这里没有
    loop 可 await；但万一哪天有人从 async 上下文调进来，硬 ``asyncio.run``
    会炸 —— 那就丢到一次性线程里跑完再取结果（不 deadlock）。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box: Dict[str, Any] = {}

    def _target():
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    th = threading.Thread(target=_target, name="npu-translate", daemon=True)
    th.start()
    th.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


class NpuTranslateProvider(TranslationProvider):
    """用板载 NPU 模型做翻译（默认 HY-MT1.5，能力名 ``swcap_translate``）。

    模型实例的**生命周期**不由本 provider 管 —— 谁启停由「网页翻译」卡片
    决定（见 ``wdnapps_audio_chat.cards.web_translate``）。本类只负责：
    * 找到正在跑、且提供 ``swcap_translate`` 的 session；
    * 按实测可靠的措辞拼提示词；
    * 校验译文**真的翻成了目标语言**，翻不出来就抛 ``TranslationError``，
      让 provider 链继续找外网那几家。
    """

    name = "npu"

    #: 本板推理 —— **不走外网** ⇒ 批与批之间不需要「防限流」的间隔
    #: （那个间隔是给免费外网接口准备的，本地推理用不上）。
    local = True

    #: 提供翻译能力的 capability（wdnai 的封闭词表）
    capability = "swcap_translate"

    #: 首选模型（card / wizard 里声明的那个）；找不到就退而求其次 —— 只要
    #: capability 对得上，用哪个模型翻译都行。
    model_id = "hy-mt-1-5"

    #: 「这条最近翻不出来」的**记忆时长**（秒）。
    #:
    #: 同一个模型、同一段文字、同一套措辞 —— 再试一遍大概率还是不行。
    #: 而翻失败的条目**不会进服务层缓存**（故意的：缓存下来就永远不重试了），
    #: 所以界面每次增量翻译都会把同一段坏文案再送一遍 → 每次白烧 5 次推理。
    #: 记下来、短期内直接跳过，省掉这些推理，也省掉外网那一趟。
    _BAD_TTL = 300.0
    #: 负缓存条数上限（防无限增长）
    _BAD_MAX = 2000

    def __init__(self, model_id: Optional[str] = None, capability: Optional[str] = None):
        self.model_id = model_id or self.model_id
        self.capability = capability or self.capability
        # (源, 目标, 原文) → 过期时间戳
        self._bad: Dict[Tuple[str, str, str], float] = {}
        self._bad_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # 「这条最近翻不出来」的负缓存
    # ------------------------------------------------------------------ #
    def _is_known_bad(self, key: Tuple[str, str, str]) -> bool:
        now = time.time()
        with self._bad_lock:
            until = self._bad.get(key)
            if until is None:
                return False
            if until <= now:
                self._bad.pop(key, None)
                return False
            return True

    def _mark_bad(self, key: Tuple[str, str, str]) -> None:
        now = time.time()
        with self._bad_lock:
            if len(self._bad) >= self._BAD_MAX:
                # 淘汰最早的一批（按过期时间排序）
                for k in sorted(self._bad, key=self._bad.get)[:self._BAD_MAX // 2]:
                    self._bad.pop(k, None)
            self._bad[key] = now + self._BAD_TTL

    # ------------------------------------------------------------------ #
    # provider 接口
    # ------------------------------------------------------------------ #
    def translate(self, text: str, source: str, target: str) -> TranslationResult:
        """翻一批文案。

        入参可能是 ``TranslateService`` 拼出来的**编号列表**
        （``"1. xxx\\n2. yyy"``）。HY-MT 整批翻会**丢条目**（实测 3 条只回
        第 1 条），所以这里拆开逐条翻、再按原格式拼回去。

        ⚠️ **多实例并行**（2026-11-16）：拆出来的每一条会**轮转分发**到本板
        正在跑的各个 ``swcap_translate`` session 上并行翻。

        为什么必须多 session 而不是多线程：一个 ``LLMRPCSession`` 的 RPC
        执行器是 ``ThreadPoolExecutor(max_workers=1)``（``wdndds/ai_session.py``），
        **同一个 session 同时只能有一个推理在飞**。实测（RK1828 双卡）：

            1 实例            24 条界面文案 3.30s
            1 实例 8 线程     3.22s   ← 客户端线程再多也没用
            2 实例（同卡）    2.74s   1.21×
            2 实例（两张卡）  1.76s   1.85×
            4 实例（2 卡×2）  1.45s   2.31×

        ⇒ 并发的单位是**实例**，不是线程。

        ⚠️ **端到端（``POST /api/translate``）的数字**：批与批之间的 1.2s
        间隔（``TranslateService._BATCH_GAP``）**只在外网 provider 生效**
        —— 那是给免费外网接口防限流用的，本板 NPU 没有外网连接，不 sleep。
        实测（同一批 24 条文案、同一串目标语言、10 个有效轮次）::

            1 实例            5.52s
            4 实例（2 卡×2）  2.78s     1.99×（端到端）

        与 provider 层的 2.31× 基本吻合（provider 层只跑一条提示词、不重试，
        所以略快）。

        ⚠️ **去掉那 1.2s 之前**，端到端是 6.74s / 3.99s（1.69×）—— 那个固定
        间隔把加速比**压掉了一截**（它占 4 实例端到端时间的 30%）。

        ⚠️ **一条翻不出来不再牵连整批**（2026-11-16 修）。以前任何一条抛
        ``TranslationError`` 都会让**整批**失败 → 链把整批交给外网 → NPU 已经
        跑完的那 23 条**全白跑**（实测:28 种界面语言里有 7 种因为一条不合格
        而整批回退外网）。现在翻不出来的那几条被单独记进 ``misses`` 返回给
        上层,由上层**只补翻那几条**。
        """
        text = text or ""
        if not text.strip():
            return TranslationResult(text, provider=self.name)

        # 语言对不支持 —— **在拆批之前**判掉:整批都白拆,而且不该给这个
        # provider 记冷却(见 :class:`UnsupportedLanguageError`)。
        self._check_pair(source, target)

        items = self._split_numbered(text)
        if len(items) > 1:
            bodies = [body for (_num, body) in items]
            outs, misses, perf = self._translate_many(bodies, source, target)
            joined = "\n".join("%d. %s" % (i, out)
                               for i, out in enumerate(outs, 1))
            return TranslationResult(
                joined, provider=self.name,
                misses=frozenset(misses),
                failed_provider=self.name if misses else "",
                perf=perf,
            )

        # 单条(没有编号可拆)
        try:
            out, perf = self._translate_one(text, source, target)
        except NpuQualityError as exc:
            # 这条它就是翻不出来 —— **不牵连别人**,交给上层补翻
            LOG.warning("translate: NPU 这条没翻出来(保留原文): %s", exc)
            # ⚠️ **失败那次的推理也要计入性能汇总** —— 那些 token 和时间
            # 是真花掉了（用户等了那么久）。不带的话「总耗时」算了两次
            # 推理、而「tokens/s」只算成功那次 ⇒ 两个数对不上。
            return TranslationResult(text, provider=self.name,
                                    misses=frozenset({0}),
                                    failed_provider=self.name,
                                    perf=getattr(exc, "perf", None))
        return TranslationResult(out, provider=self.name, perf=perf)

    def _translate_many(self, bodies: List[str], source: str,
                        target: str) -> Tuple[List[str], set, Dict[str, float]]:
        """把一批文案翻完；能并行就并行。

        分发方式 = **按条轮转**（第 i 条给第 ``i % N`` 个 session）。
        轮转而不是「前一段给 A、后一段给 B」：轮转让**任何一个 session 慢**
        只影响它自己那 1/N 的条目，成块分配则慢的那份会把整块都拖住。

        返回 ``(译文列表, 没翻出来的条目下标集合, 累计的推理性能表)``。

        ⚠️ **单条失败不再牵连整批**（2026-11-16 修）。以前任何一条抛异常
        → 整批抛 → 链把整批交给外网 → NPU 已经跑完的那 23 条全白跑。
        现在翻不出来的那条**保留原文**、单独记进 misses，由上层补翻。

        只有**推理级**故障（worker 挂了 / 超时）才会冒出去 —— 那是 provider
        级的毛病，该整批换下一家，也该给它记冷却。
        """
        sessions = self._sessions()
        misses: set = set()
        perf: Dict[str, float] = {}
        perf_lock = threading.Lock()

        if len(sessions) <= 1:
            sess = sessions[0] if sessions else None
            out: List[str] = []
            for idx, body in enumerate(bodies):
                try:
                    val, one = self._translate_one(body, source, target, sess)
                except NpuQualityError as exc:
                    LOG.warning("translate: NPU 这条没翻出来(保留原文): %s", exc)
                    out.append(body)
                    misses.add(idx)
                    # ⚠️ 失败那次的推理也计入性能汇总（真花掉了）
                    with perf_lock:
                        _merge_perf(perf, getattr(exc, "perf", None))
                    continue
                with perf_lock:
                    _merge_perf(perf, one)
                out.append(val)
            return out, misses, _finish_perf(perf)

        out2: List[Optional[str]] = [None] * len(bodies)
        lock = threading.Lock()

        def one(idx: int, body: str) -> None:
            sess = sessions[idx % len(sessions)]
            try:
                val, one_perf = self._translate_one(body, source, target, sess)
            except NpuQualityError as exc:
                # 这条翻不出来 —— **不牵连整批**，保留原文、记进 misses
                LOG.warning("translate: NPU 这条没翻出来(保留原文): %s", exc)
                with perf_lock:
                    _merge_perf(perf, getattr(exc, "perf", None))
                with lock:
                    out2[idx] = body
                    misses.add(idx)
                return
            with perf_lock:
                _merge_perf(perf, one_perf)
            with lock:
                out2[idx] = val

        # 线程数 = session 数：再多也没用（每个 session 只能一个在飞）。
        with ThreadPoolExecutor(max_workers=len(sessions),
                                thread_name_prefix="npu-translate") as ex:
            futs = [ex.submit(one, i, b) for i, b in enumerate(bodies)]
            for fut in futs:
                fut.result()   # 推理级异常在这里冒出来 → 链切下一个 provider
        return [o if o is not None else "" for o in out2], misses, _finish_perf(perf)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    @staticmethod
    def _split_numbered(text: str) -> List[Tuple[int, str]]:
        """把 ``"1. a\\n2. b"`` 拆成 ``[(1, "a"), (2, "b")]``。

        不是编号列表就返回 ``[]``（调用方按单条处理）。
        """
        lines = [ln for ln in (text or "").split("\n") if ln.strip()]
        if len(lines) < 2:
            return []
        out: List[Tuple[int, str]] = []
        want = 1
        for ln in lines:
            m = _ITEM_RE.match(ln)
            if m and int(m.group(1)) == want:
                out.append((want, m.group(2).strip()))
                want += 1
        if len(out) != len(lines) or not out:
            return []
        return out

    def _translate_one(self, text: str, source: str, target: str,
                       session: Any = None) -> Tuple[str, Dict[str, float]]:
        """翻一条，按实测成功率顺序试措辞，用文种校验挑真正翻出来的那个。

        ``session`` 不传就现取一个（单实例路径）。

        返回 ``(译文, 本条累计的推理性能表)``。

        ⚠️ 性能表是**所有尝试之和**：一条文案可能要试好几条措辞
        （:data:`PROMPT_ORDER`），每条都是一次完整推理 —— 用户等的是
        全部这些加起来的时间，只记最后一次的话 tokens/s 会好看得离谱。

        抛的异常分两类 —— **这个区分很重要**：

        * :class:`UnsupportedLanguageError` —— 语言对不在模型词表里。
          确定性的，链会换下一家，但**不给它记冷却**。
        * :class:`NpuQualityError` —— 这条它就是翻不出来（复读 / 方向反了）。
          **内容级**问题：只牵连这一条，不给 provider 记冷却。
        * 其它 :class:`TranslationError` —— 推理级故障（worker 挂了/超时）。
          provider 级的毛病，该整批换下一家、该记冷却。
        """
        text = (text or "").strip()
        if not text:
            return text, {}
        if source == target:
            return text, {}
        self._check_pair(source, target)

        key = (source, target, text)
        if self._is_known_bad(key):
            # 刚才就试过了 —— 不再白烧 5 次推理
            raise NpuQualityError(
                f"NPU 这条最近翻不出来（{text[:24]!r}），跳过重试"
            )

        if session is None:
            session = self._session()

        csrc = LANG_CODES.get(source)
        ctgt = LANG_CODES.get(target)
        nsrc = LANG_NAMES.get(source)
        ntgt = LANG_NAMES.get(target)

        # ⚠️ **按语言的措辞顺序**（实测，:data:`PROMPT_ORDER`）——
        # 非拉丁字母的语言用「码」写指令模型会译错语言，先按「名」写才成。
        order = PROMPT_ORDER.get(target, DEFAULT_PROMPT_ORDER)
        attempts = []
        for i in order:
            if i >= 3 and not (nsrc and ntgt):
                continue   # 没有名字 → 这两条「按名写」的措辞用不了
            attempts.append(
                _ALL_PROMPTS[i].format(csrc=csrc, ctgt=ctgt, nsrc=nsrc,
                                       ntgt=ntgt, text=text)
            )

        last = ""
        perf: Dict[str, float] = {}
        for prompt in attempts:
            try:
                out, one = self._infer(session, prompt)
            except TranslationError:
                raise
            except Exception as exc:  # noqa: BLE001
                LOG.warning("translate: NPU 推理失败(%s): %s", type(exc).__name__, exc)
                raise TranslationError(f"NPU 翻译推理失败: {exc}") from exc
            _merge_perf(perf, one)
            last = out
            cleaned = self._strip_label(out, source, target)
            if self._looks_translated(cleaned, text, target):
                return cleaned, _finish_perf(perf)
            LOG.debug("translate: NPU 这次没翻出来，换下一个措辞: %r", out[:60])
        self._mark_bad(key)
        raise NpuQualityError(
            f"NPU 翻译没能翻出目标语言（最后得到: {last[:60]!r}）",
            perf=_finish_perf(perf),
        )

    @staticmethod
    def _check_pair(source: str, target: str) -> None:
        """语言对不在模型词表里 → 抛 :class:`UnsupportedLanguageError`。

        ⚠️ 用的是**另一个异常类**，因为它**不该给 provider 记冷却**：
        「这个语言不支持」是确定性的，冷却它 60 秒会让**其它**语言也一起
        走外网（实测踩过：6 种语言不支持，每次都把 NPU 冷却掉）。
        """
        if source == target:
            return
        # ⚠️ 准入条件只看**码**（模型词表里的东西）：名字只是**兜底措辞**
        # 用的，缺了名字只是少两条兜底措辞，**不是**「这个语言不支持」。
        csrc = LANG_CODES.get(source)
        ctgt = LANG_CODES.get(target)
        if not csrc or not ctgt:
            raise UnsupportedLanguageError(
                f"NPU 翻译不支持的语言对 {source!r} → {target!r}"
            )
        if csrc == ctgt:
            # **同码不同语言** —— 模型词表里简体和繁体**共用一个码**（``zh``），
            # 它根本分不出来。实测让模型转繁体的结果是「半繁体半简体」
            # （"网页翻译" → "网页翻译服务"，「网」还是简体）—— 比不转更糟。
            # ⇒ 明确交给外网（有道 zh-CHS→zh-CHT 是真转换），**零推理开销**。
            raise UnsupportedLanguageError(
                f"NPU 翻译不支持的语言对 {source!r} → {target!r}"
                f"（模型词表里简体/繁体是同一个码 {csrc!r}，它分不出来）"
            )

    def _strip_label(self, out: str, source: str, target: str) -> str:
        """剥掉模型有时会加的「翻译标签」前缀。

        按名字写指令时模型常吐 ``Translate from Chinese to Japanese: 译文`` /
        ``Übersetzen aus Chinesisch ins Deutsche: 译文`` / ``中文翻译：译文``。
        译文本身是对的，只是前面多了个标签 —— 剥掉即可，不必浪费一次推理。

        只在**前缀像标签**时才剥（前缀里出现了语言名/码或「翻译」类的词），
        否则 ``Note: xxx`` 这种正文里的冒号会被误伤。
        """
        out = (out or "").strip()
        for sep in (":", "："):
            idx = out.find(sep)
            if idx <= 0 or idx > 45:
                continue
            head = out[:idx]
            tail = out[idx + 1:].strip()
            if not tail:
                continue
            low = head.lower()
            looks_label = any(w in low for w in _LABEL_WORDS)
            if not looks_label:
                for code in (LANG_CODES.get(source), LANG_CODES.get(target)):
                    if code and re.search(r"\b%s\b" % re.escape(code), low):
                        looks_label = True
                        break
            if not looks_label:
                for name in (LANG_NAMES.get(source), LANG_NAMES.get(target)):
                    if name and name.lower() in low:
                        looks_label = True
                        break
            if looks_label:
                return tail
        return out

    def _looks_translated(self, out: str, src: str, target: str) -> bool:
        """判定译文是不是真的翻成了目标语言。

        模型「没翻」时的典型表现是**把原文微调一下吐回来**（"应用面板正在加载"
        → "应用面板正在加载中"）—— 文本看着像译文，内容还是中文。所以不能
        只看「输出 != 输入」，得看**文种**。

        另外还要挡掉**复读**（实测瑞典语那档会吐成
        "hjälpa till hjälpa till hjälpa till …"）。
        """
        out = (out or "").strip()
        if not out:
            return False
        if out == src.strip():
            return False
        if self._is_degenerate(out, src):
            return False
        scripts = _script_set(out)
        if not scripts:
            return False
        expected = TARGET_SCRIPTS.get(target)
        if expected is None:
            expected = ("latin",) if target in _LATIN_TARGETS else None
        if expected is None:
            # 不认识的目标语：只要**换了文种**就算翻出来了
            src_scripts = _script_set(src)
            return bool(scripts - src_scripts)
        return any(s in scripts for s in expected)

    @staticmethod
    def _is_degenerate(out: str, src: str = "") -> bool:
        """复读检测：同一个词反复出现 / 输出离谱地长。

        ⚠️ 长度上限是**相对原文**的（``max(400, 原文长 × 4 + 100)``）——
        以前写死 400 字,而 ``TranslateService`` 一条文案可以到 2000 字，
        于是**长文案一律被判复读**，永远翻不出来。
        """
        limit = max(400, len(src or "") * 4 + 100)
        if len(out) > limit:
            return True
        words = re.findall(r"\S+", out.lower())
        if len(words) >= 6:
            for w in set(words):
                if w and len(w) > 1 and words.count(w) >= 4:
                    return True
        return False

    def _sessions(self) -> List[Any]:
        """本板**所有**正在跑的翻译 session（每个实例一个）。

        一台板子上可以同时跑多份翻译模型（2 卡 × 每卡 2 份 = 4 份）——
        每一份都是独立的 worker 进程、独立的 rknn3 context、独立的
        per-PID DDS service，所以每个都能**同时**跑一个推理。
        ⇒ 并发的单位是实例，不是线程（见 :meth:`translate` 的实测表）。

        只取**本机**实例（``include_remote=False``）：远端 peer 的翻译结果
        照样能用，但那是别人的板子，不该混进「本板 NPU」的账里。
        """
        try:
            from app.core.compute.registry import get_registry
            reg = get_registry()
        except Exception:  # noqa: BLE001
            return []
        try:
            handles = reg.list_sessions(self.capability,
                                         include_remote=False,
                                         model_id=self.model_id)
        except Exception:  # noqa: BLE001
            return []
        return [h.session for h in handles]

    def _session(self) -> Any:
        """拿到正在跑的翻译 session。

        拿不到 = 模型没启用 / 还没起来 —— 抛 ``TranslationError``，让链继续
        试外网那几家（而不是让整个界面翻译挂掉）。
        """
        try:
            from app.core.compute.registry import get_registry
            reg = get_registry()
        except Exception as exc:  # noqa: BLE001
            raise TranslationError(f"拿不到算力注册表: {exc}") from exc
        try:
            return reg.get_session_for_model(self.capability, self.model_id)
        except RuntimeError:
            pass
        # 指定的模型没跑 —— 只要**有** provider 提供这个能力就用它
        try:
            return reg.get_session(self.capability)
        except RuntimeError as exc:
            raise TranslationError(
                f"NPU 翻译未启用（{self.capability} 没有正在运行的实例）"
            ) from exc

    def _infer(self, session: Any, prompt: str) -> Tuple[str, Dict[str, float]]:
        """跑一次推理，返回 ``(纯文本, 本次推理的性能表)``。

        ⚠️ 性能表必须**在 respond() 返回的当下**就从 session 上取走 ——
        ``session.last_perf`` 是「最近一次推理」的单槽位，如果同一个 session
        被两条线程同时用（理论上不该发生：一个 session 同时只能一个推理在
        飞），后一个会把前一个的覆盖掉。取完立刻用掉才安全。
        """
        result = _run_coro(session.respond(prompt=prompt, max_new_tokens=512))
        # ⚠️ 先取 perf，再处理文本 —— 顺序不能反（见上面的说明）。
        perf = _session_perf(session)
        if isinstance(result, str):
            return result, perf
        text = getattr(result, "text", None)
        if isinstance(text, str):
            return text, perf
        return ("" if result is None else str(result)), perf


# --------------------------------------------------------------------------- #
# 推理性能表（tokens/s 的原料）
# --------------------------------------------------------------------------- #
#
# ⚠️ **数字只在 worker 进程里**。SDK 的 backend 把它们写进
# ``session._backend.sink.perf``（``n_decode_tokens`` / ``n_prefill_tokens``
# / ``prefill_ms`` / ``decode_ms``），而 provider 跑在 **Web UI 进程**里 ——
# 那边拿到的是 RPC 代理，够不着那个 sink。
#
# ⇒ worker 必须显式把数字带过 wire（见 ``wdndds/ai_session.py`` 的
# ``_handle_request``），client 侧收在 ``session.last_perf``。
#
# 两条取法：
#   * ``last_perf``      —— RPC 代理（**本板正常路径**）
#   * ``_backend.sink`` —— 本地 session（调试 / 同进程直连时的退路）
def _session_perf(session: Any) -> Dict[str, float]:
    """从一个 session 上读出最近一次推理的性能表（读不到就空字典）。"""
    if session is None:
        return {}
    perf = getattr(session, "last_perf", None)
    if not isinstance(perf, dict) or not perf:
        # 退路：本地 session（没走 RPC）—— 直接读 SDK 的 sink
        sink = getattr(getattr(session, "_backend", None), "sink", None)
        perf = getattr(sink, "perf", None)
    if not isinstance(perf, dict):
        return {}
    return {
        str(k): float(v) for k, v in perf.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }


def _merge_perf(dst: Dict[str, float], src: Dict[str, float]) -> Dict[str, float]:
    """把一次推理的性能表**累加**进汇总表。

    ⚠️ 为什么要累加而不是覆盖：provider 一条文案可能要试**好几条措辞**
    （:data:`PROMPT_ORDER`），每条措辞都是一次完整推理。真正花掉的 token
    和时间是**所有这些推理之和** —— 只记最后一次的话，tokens/s 会好看得
    离谱，而用户实际等的是那么久。

    ⚠️ **只累加原始计数**（token 数 / 两段耗时）；派生量（tokens/s 等）
    **不累加** —— 每次推理各自的速率加起来没有意义。它们由
    :func:`wdnai.perf_table` 从累加后的原始计数**重算**（见
    :func:`_finish_perf`）。

    ⚠️ 派生量用 **SDK 自己的算法**算（``wdnai.perf_table``）—— 和 SDK
    打印的那张 perf 表**同一份公式**，不会两边漂。
    """
    for key, val in (src or {}).items():
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            continue
        if key in _PERF_COUNTER_KEYS:
            dst[key] = dst.get(key, 0.0) + float(val)
        # 派生量不累加 —— 稍后统一重算
    return dst


#: perf 表里**可以累加**的原始计数（其余是派生量，不累加）。
_PERF_COUNTER_KEYS = ("n_decode_tokens", "n_prefill_tokens",
                      "prefill_ms", "decode_ms")


def _finish_perf(perf: Dict[str, float]) -> Dict[str, float]:
    """把累加好的原始计数**算成**最终的性能表（含派生量）。

    ⚠️ 派生量由 **SDK 的** :func:`wdnai.perf_table` 算 —— 和 SDK 打印的
    perf 表**同一份公式**。宿主要是自己另写一套，两边迟早漂（改了 SDK
    的算法，界面上的 tokens/s 就悄悄对不上打印出来的表了）。

    ⚠️ **没有原始计数就返回空字典**（不是「一堆 0」）：压根没推理过 ⇒
    性能表就该是空的。
    """
    if not perf:
        return {}
    from wdnai import perf_table

    out = dict(perf)
    out.update(perf_table(out))
    return out
