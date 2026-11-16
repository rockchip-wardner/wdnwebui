#!/usr/bin/env python3
"""翻译 provider 链自检 —— 故障转移 / 冷却 / 缓存语义。

用法::

    .venv/bin/python scripts/test_translate_chain.py          # 离线(假 provider)
    .venv/bin/python scripts/test_translate_chain.py --live   # 再跑一遍真机链路

离线部分不碰网络:用假 provider 把「配额超限 → 自动换站 → 冷却 → 恢复」
这套逻辑**逐条钉死**,以后谁改了 chain 的行为,这里会立刻炸。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.translate.providers import (  # noqa: E402
    ProviderChain,
    QuotaExceeded,
    TranslationError,
    TranslationProvider,
    build_chain,
)
from app.core.translate.service import TranslateService  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ok   {name}")
    else:
        _FAIL += 1
        print(f"  FAIL {name} {extra}")


class FakeProvider(TranslationProvider):
    """假 provider:可以设定行为(成功 / 配额超限 / 网络错误)。"""

    def __init__(self, name: str, mode: str = "ok", value: str | None = None):
        self.name = name
        self.mode = mode
        self.value = value
        self.calls = 0

    def translate(self, text: str, source: str, target: str) -> str:
        self.calls += 1
        if self.mode == "quota":
            raise QuotaExceeded(f"{self.name} 配额用尽")
        if self.mode == "error":
            raise TranslationError(f"{self.name} 网络错误")
        if self.mode == "empty":
            return ""
        return self.value if self.value is not None else f"[{self.name}] {text}"


def test_chain_order() -> None:
    print("\n[1] 链按顺序用,第一个能用就不走后头")
    a = FakeProvider("a")
    b = FakeProvider("b")
    chain = ProviderChain([a, b])
    out = chain.translate("系统运行正常", "zh-CN", "en")
    check("用的是第一个 provider", out == "[a] 系统运行正常", out)
    check("第二个没被调用", b.calls == 0)


def test_failover_on_quota() -> None:
    print("\n[2] 配额超限(429)自动换下一家")
    a = FakeProvider("a", mode="quota")
    b = FakeProvider("b")
    chain = ProviderChain([a, b])
    out = chain.translate("系统运行正常", "zh-CN", "en")
    check("换到第二个并拿到译文", out == "[b] 系统运行正常", out)
    check("第一个进了冷却", "a" in chain.cooldown_state())


def test_cooldown_skips() -> None:
    print("\n[3] 冷却期内不再打废掉的 provider")
    a = FakeProvider("a", mode="quota")
    b = FakeProvider("b")
    chain = ProviderChain([a, b])
    chain.translate("系统运行正常", "zh-CN", "en")
    a.calls = 0
    chain.translate("温度正常", "zh-CN", "en")
    check("冷却期内没再打 a", a.calls == 0)


def test_failover_on_error() -> None:
    print("\n[4] 网络错误也换下一家(但只短暂冷却)")
    a = FakeProvider("a", mode="error")
    b = FakeProvider("b")
    chain = ProviderChain([a, b], quota_cooldown=1800.0, error_cooldown=1.0)
    out = chain.translate("系统运行正常", "zh-CN", "en")
    check("换到第二个", out == "[b] 系统运行正常", out)
    state = chain.cooldown_state()
    check("网络错是短冷却(≤1s)", state.get("a", 0) <= 1, str(state))


def test_all_cooldown() -> None:
    print("\n[5] 全部在冷却 → 快速失败,不去撞墙")
    a = FakeProvider("a", mode="quota")
    b = FakeProvider("b", mode="quota")
    chain = ProviderChain([a, b])
    # 两家都配额用尽 → 这一轮就抛配额错
    try:
        chain.translate("系统运行正常", "zh-CN", "en")
        check("第一家全挂时应抛 QuotaExceeded", False)
    except QuotaExceeded:
        check("第一家全挂时抛 QuotaExceeded", True)
    # 之后两家都在冷却 → 不该再去撞墙
    calls_before = a.calls + b.calls
    t0 = time.time()
    try:
        chain.translate("系统运行正常", "zh-CN", "en")
        check("冷却中应抛 QuotaExceeded", False)
    except QuotaExceeded:
        check("冷却中抛 QuotaExceeded", True)
    check("冷却中不再发请求", a.calls + b.calls == calls_before)
    check("快速失败(<0.5s)", time.time() - t0 < 0.5)


def test_service_reports_provider() -> None:
    print("\n[6] 服务层上报真正干活的 provider")
    a = FakeProvider("a", mode="quota")
    b = FakeProvider("b")
    svc = TranslateService(ProviderChain([a, b]))
    _, stats = svc.translate_segments(["系统运行正常"], "zh-CN", "en")
    check("stats.provider = b", stats["provider"] == "b", str(stats))
    check("stats.providers 含两家", stats["providers"] == ["a", "b"], str(stats))


def test_failure_not_cached() -> None:
    print("\n[7] 翻失败的不写缓存(不毒化)")
    a = FakeProvider("a", mode="error")
    svc = TranslateService(ProviderChain([a]))
    out, stats = svc.translate_segments(["系统运行正常"], "zh-CN", "en")
    check("保留了原文", out == ["系统运行正常"], str(out))
    check("记账 failed=1", stats["failed"] == 1, str(stats))
    check("没写缓存", svc.cache_size() == 0, str(svc.cache_size()))


def test_numbered_alignment() -> None:
    print("\n[8] 编号列表能按编号对齐(编号本身会被剥掉)")
    class Numbered(FakeProvider):
        def translate(self, text, source, target):
            self.calls += 1
            out = []
            for line in text.split("\n"):
                num, _, rest = line.partition(". ")
                out.append(f"{num}. {rest} (translated)")
            return "\n".join(out)

    svc = TranslateService(ProviderChain([Numbered("n")]))
    out, _ = svc.translate_segments(["系统运行正常", "网络已连接"], "zh-CN", "en")
    # 行首编号只是传输用的标记 —— 切回来时要去掉,内容要一条不差地对上
    check("两条都对齐且剥掉编号",
          out == ["系统运行正常 (translated)", "网络已连接 (translated)"], str(out))


def test_single_provider_compat() -> None:
    print("\n[9] 传单个 provider(旧调用方式)也能用")
    svc = TranslateService(FakeProvider("solo"))
    out, stats = svc.translate_segments(["系统运行正常"], "zh-CN", "en")
    check("能翻", out == ["[solo] 系统运行正常"], str(out))
    check("provider 名对", stats["provider"] == "solo", str(stats))


def test_local_provider_skips_batch_gap() -> None:
    """本地 provider（板载 NPU）**不**吃批间 1.2s 间隔，外网照旧。

    为什么这条要钉死：那个间隔（``_BATCH_GAP``）是**给免费外网接口防限流**
    的 —— 免费站点按连接建立速率掐人。本板 NPU 没有外网连接，让它白等
    1.2s 就是纯亏：实测它占 4 实例端到端耗时的 **30%**（3.99s 里 1.2s），
    把加速比从 1.99× 压到 1.69×。

    判据是 provider 自己的属性 ``local``（``TranslationProvider.local``），
    不是 service 里硬编码 provider 名 —— 以后再加本地 provider 不用改
    service。
    """
    print("\n[11] 本地 provider 不吃批间间隔(外网照旧)")

    class Local(FakeProvider):
        local = True

    slept: list[float] = []
    real_sleep = time.sleep

    def fake_sleep(sec):
        slept.append(sec)

    # ---- 本地 provider → 一秒都不该睡 ----
    svc = TranslateService(ProviderChain([Local("npu")]))
    import app.core.translate.service as svc_mod
    svc_mod.time.sleep = fake_sleep
    try:
        out, stats = svc.translate_segments(["系统运行正常"], "zh-CN", "en")
    finally:
        svc_mod.time.sleep = real_sleep
    check("本地 provider 翻出来了", out == ["[npu] 系统运行正常"], str(out))
    check("本地 provider 一秒都没睡", slept == [], f"睡了 {slept}")

    # ---- 外网 provider → 照旧睡 1.2s ----
    slept.clear()
    svc = TranslateService(ProviderChain([FakeProvider("youdao")]))
    svc_mod.time.sleep = fake_sleep
    try:
        svc.translate_segments(["系统运行正常"], "zh-CN", "en")
    finally:
        svc_mod.time.sleep = real_sleep
    check("外网 provider 照旧睡 _BATCH_GAP",
          slept == [svc_mod._BATCH_GAP], f"睡了 {slept}")

    # ---- 判据本身 ----
    chain = ProviderChain([Local("npu"), FakeProvider("youdao")])
    check("provider_is_local(npu)", chain.provider_is_local("npu") is True)
    check("provider_is_local(youdao)", chain.provider_is_local("youdao") is False)
    check("provider_is_local(空/未知) 保守返回 False",
          chain.provider_is_local("") is False
          and chain.provider_is_local("nope") is False)


def test_build_chain_order() -> None:
    print("\n[10] 配置顺序 = 故障转移顺序")
    chain = build_chain(["youdao", "iciba", "mymemory"])
    check("顺序照抄配置", chain.names == ["youdao", "iciba", "mymemory"], str(chain.names))
    chain = build_chain(["youdao", "iciba", "mymemory", "baidu"], baidu={})
    check("没配 key 的百度被跳过", "baidu" not in chain.names, str(chain.names))
    chain = build_chain(["baidu"], baidu={"appid": "1", "key": "2"})
    check("配了 key 的百度进链", "baidu" in chain.names, str(chain.names))


# --------------------------------------------------------------------------- #
# 「一条不合格拖垮 24 条」那个 bug 的回归
# --------------------------------------------------------------------------- #
class PartialProvider(TranslationProvider):
    """只翻成功一部分的 provider(模拟 NPU:23 条成了,第 24 条翻不出来)。"""

    def __init__(self, name: str, bad_index: int = -1, total: int = 1):
        self.name = name
        self.bad_index = bad_index
        self.total = total
        self.calls = 0
        self.saw: list[str] = []

    def translate(self, text, source, target):
        from app.core.translate.base import TranslationResult
        self.calls += 1
        self.saw.append(text)
        lines = [ln for ln in (text or "").split("\n") if ln.strip()]
        bodies = []
        for ln in lines:
            m = __import__("re").match(r"^\s*(\d{1,3})\s*[.)、]?\s*(.*)$", ln)
            bodies.append(m.group(2).strip() if m else ln.strip())
        misses = set()
        outs = []
        for i, body in enumerate(bodies):
            if i == self.bad_index:
                misses.add(i)
                outs.append(body)
            else:
                outs.append("[%s] %s" % (self.name, body))
        joined = "\n".join("%d. %s" % (i, o) for i, o in enumerate(outs, 1))
        return TranslationResult(joined, provider=self.name,
                                 misses=frozenset(misses),
                                 failed_provider=self.name if misses else "")


def test_partial_failure_not_fatal() -> None:
    print("\n[12] 一条翻不出来**不牵连整批**(核心修复)")
    a = PartialProvider("a", bad_index=1, total=3)
    b = FakeProvider("b")
    svc = TranslateService(ProviderChain([a, b]))
    out, stats = svc.translate_segments(
        ["系统运行正常", "算力配置", "已安装"], "zh-CN", "en")
    check("三条都有译文", all(o for o in out), str(out))
    check("翻出来的那两条用的是 a", out[0].startswith("[a]"), str(out))
    check("翻不出来的那条被 b 补上了", out[1].startswith("[b]"), str(out))
    check("没有条目保留原文", stats["failed"] == 0, str(stats))
    check("上报的是翻了多数条的那家", stats["provider"] == "a", str(stats))


def test_repair_skips_failed_provider() -> None:
    print("\n[13] 补翻时**跳过**刚刚翻不出来的那家")
    a = PartialProvider("a", bad_index=0, total=2)
    b = FakeProvider("b")
    svc = TranslateService(ProviderChain([a, b]))
    out, stats = svc.translate_segments(["算力配置", "已安装"], "zh-CN", "en")
    check("补翻那条由 b 翻", out[0].startswith("[b]"), str(out))
    check("a 只被调用过一次(整批那次)", a.calls == 1, "a.calls=%d" % a.calls)
    check("a 没有再收到那条补翻请求",
          not any("算力配置" in t and "1." not in t for t in a.saw),
          str(a.saw))


def test_skip_argument() -> None:
    print("\n[14] chain.translate 的 skip 参数")
    a = FakeProvider("a")
    b = FakeProvider("b")
    chain = ProviderChain([a, b])
    out = chain.translate("系统运行正常", "zh-CN", "en", skip=("a",))
    check("跳过的 provider 没被调用", a.calls == 0, "a.calls=%d" % a.calls)
    check("用的是下一个", out == "[b] 系统运行正常", out)


def test_unsupported_language_no_cooldown() -> None:
    print("\n[15] 语言不支持 -> **不记 provider 冷却**(不能连累别的语言)")
    from app.core.translate.base import UnsupportedLanguageError

    class NoLang(FakeProvider):
        def translate(self, text, source, target):
            self.calls += 1
            raise UnsupportedLanguageError("不支持的语言对")

    a = NoLang("a")
    b = FakeProvider("b")
    chain = ProviderChain([a, b])
    out = chain.translate("系统运行正常", "zh-CN", "sv")
    check("换到了下一家", out == "[b] 系统运行正常", out)
    check("没有进冷却", chain.cooldown_state() == {}, str(chain.cooldown_state()))
    out2 = chain.translate("系统运行正常", "zh-CN", "en")
    check("下一条请求仍然先试 a", a.calls == 2, "a.calls=%d" % a.calls)
    check("照样翻出来了", out2 == "[b] 系统运行正常", out2)


def test_partial_no_cooldown() -> None:
    print("\n[16] provider 只翻出一部分 -> 也不记冷却")
    a = PartialProvider("a", bad_index=0, total=1)
    b = FakeProvider("b")
    chain = ProviderChain([a, b])
    chain.translate("1. 算力配置", "zh-CN", "en")
    check("没有进冷却", chain.cooldown_state() == {}, str(chain.cooldown_state()))


def test_npu_negative_cache() -> None:
    print("\n[17] NPU: 翻不出来的那条短期内不再重试(负缓存)")
    from app.core.translate.npu_provider import NpuTranslateProvider

    prov = NpuTranslateProvider()
    prov._infer = lambda session, prompt: "算力配置"   # 复读原文 -> 翻不出来
    calls = {"n": 0}

    def counting(session, prompt):
        calls["n"] += 1
        return "算力配置"

    prov._infer = counting
    try:
        prov._translate_one("算力配置", "zh-CN", "en", session=object())
    except Exception as exc:
        check("第一次报质量错", type(exc).__name__ == "NpuQualityError",
              type(exc).__name__)
    check("试了 5 个措辞", calls["n"] == 5, "calls=%d" % calls["n"])
    before = calls["n"]
    try:
        prov._translate_one("算力配置", "zh-CN", "en", session=object())
    except Exception as exc:
        check("第二次还是质量错", type(exc).__name__ == "NpuQualityError",
              type(exc).__name__)
    check("第二次**一次都没试**(命中负缓存)", calls["n"] == before,
          "calls=%d" % calls["n"])


def test_npu_unsupported_language() -> None:
    print("\n[18] NPU: 语言对不支持 -> UnsupportedLanguageError(不记冷却)")
    from app.core.translate.base import UnsupportedLanguageError
    from app.core.translate.npu_provider import NpuTranslateProvider

    prov = NpuTranslateProvider()
    try:
        prov.translate("1. 算力配置", "zh-CN", "sv")
        check("抛了 UnsupportedLanguageError", False, "没抛")
    except UnsupportedLanguageError:
        check("抛了 UnsupportedLanguageError", True)
    except Exception as exc:
        check("抛了 UnsupportedLanguageError", False, type(exc).__name__)


def test_npu_check_pair() -> None:
    print("\n[19] NPU: 语言对准入判定(同码不同语言 = 不支持)")
    from app.core.translate.base import UnsupportedLanguageError
    from app.core.translate.npu_provider import NpuTranslateProvider

    prov = NpuTranslateProvider()
    try:
        prov._check_pair("zh-CN", "en")
        check("支持的语言对不抛异常", True)
    except Exception as exc:
        check("支持的语言对不抛异常", False, repr(exc))
    try:
        prov._check_pair("zh-CN", "zh-TW")
        check("简→繁(同码)抛 UnsupportedLanguageError", False, "没抛")
    except UnsupportedLanguageError:
        check("简→繁(同码)抛 UnsupportedLanguageError", True)
    except Exception as exc:
        check("简→繁(同码)抛 UnsupportedLanguageError", False, repr(exc))
    try:
        prov._check_pair("zh-CN", "sv")
        check("词表外抛 UnsupportedLanguageError", False, "没抛")
    except UnsupportedLanguageError:
        check("词表外抛 UnsupportedLanguageError", True)
    except Exception as exc:
        check("词表外抛 UnsupportedLanguageError", False, repr(exc))


def test_npu_prompt_order() -> None:
    print("\n[20] NPU: 每种语言有自己的措辞顺序(实测表)")
    from app.core.translate.npu_provider import (NpuTranslateProvider,
                                                  PROMPT_ORDER,
                                                  DEFAULT_PROMPT_ORDER,
                                                  _ALL_PROMPTS)
    prov = NpuTranslateProvider()
    check("表里有 30 种以上语言", len(PROMPT_ORDER) >= 30, str(len(PROMPT_ORDER)))
    check("默认顺序是 5 条全用", DEFAULT_PROMPT_ORDER == (0, 1, 2, 3, 4),
          str(DEFAULT_PROMPT_ORDER))
    ok = all(len(v) == 5 and sorted(v) == [0, 1, 2, 3, 4]
             for v in PROMPT_ORDER.values())
    check("每种语言都是 5 条的排列(不丢兜底)", ok)
    check("非拉丁字母的语言把「按名写」排前面",
          PROMPT_ORDER["hi"][0] >= 3 and PROMPT_ORDER["uk"][0] >= 3,
          "hi=%s uk=%s" % (PROMPT_ORDER["hi"], PROMPT_ORDER["uk"]))
    check("拉丁字母的语言把「按码写」排前面",
          PROMPT_ORDER["en"][0] < 3 and PROMPT_ORDER["fr"][0] < 3,
          "en=%s fr=%s" % (PROMPT_ORDER["en"], PROMPT_ORDER["fr"]))


def live_check() -> None:
    print("\n[LIVE] 真机链路:翻一句中文,看实际谁在干活")
    from app.core.translate import get_translate_service, reset_translate_service

    reset_translate_service()
    svc = get_translate_service()
    print(f"  provider 链 = {' → '.join(svc.provider_names)}")
    t0 = time.time()
    out, stats = svc.translate_segments(
        ["系统运行正常", "网络已连接", "设备温度 45 摄氏度"], "zh-CN", "en"
    )
    dt = time.time() - t0
    for src, dst in zip(["系统运行正常", "网络已连接", "设备温度 45 摄氏度"], out):
        print(f"    {src}  →  {dst}")
    print(f"  实际 provider = {stats['provider']} / {stats['providers']}")
    print(f"  用时 {dt:.1f}s,请求 {stats['requests']} 次,失败 {stats['failed']} 条")
    ok = stats["failed"] == 0 and all(o for o in out)
    check("真机翻译成功", ok, str(stats))


if __name__ == "__main__":
    print("=" * 72)
    print("翻译 provider 链自检")
    print("=" * 72)
    test_chain_order()
    test_failover_on_quota()
    test_cooldown_skips()
    test_failover_on_error()
    test_all_cooldown()
    test_service_reports_provider()
    test_failure_not_cached()
    test_numbered_alignment()
    test_single_provider_compat()
    test_local_provider_skips_batch_gap()
    test_build_chain_order()
    test_partial_failure_not_fatal()
    test_repair_skips_failed_provider()
    test_skip_argument()
    test_unsupported_language_no_cooldown()
    test_partial_no_cooldown()
    test_npu_negative_cache()
    test_npu_unsupported_language()
    test_npu_check_pair()
    test_npu_prompt_order()
    if "--live" in sys.argv:
        live_check()
    print("\n" + "=" * 72)
    print(f"通过 {_PASS} 项,失败 {_FAIL} 项")
    sys.exit(1 if _FAIL else 0)
