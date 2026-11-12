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


def test_build_chain_order() -> None:
    print("\n[10] 配置顺序 = 故障转移顺序")
    chain = build_chain(["youdao", "iciba", "mymemory"])
    check("顺序照抄配置", chain.names == ["youdao", "iciba", "mymemory"], str(chain.names))
    chain = build_chain(["youdao", "iciba", "mymemory", "baidu"], baidu={})
    check("没配 key 的百度被跳过", "baidu" not in chain.names, str(chain.names))
    chain = build_chain(["baidu"], baidu={"appid": "1", "key": "2"})
    check("配了 key 的百度进链", "baidu" in chain.names, str(chain.names))


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
    test_build_chain_order()
    if "--live" in sys.argv:
        live_check()
    print("\n" + "=" * 72)
    print(f"通过 {_PASS} 项,失败 {_FAIL} 项")
    sys.exit(1 if _FAIL else 0)
