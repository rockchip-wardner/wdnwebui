#!/usr/bin/env python3
"""推理性能透出自检 —— 总时间与 tokens/s 是不是真的从板子上透出来了。

背景
----
两张卡都要显示「这次推理花了多久、多少 tokens/s」：

* **网页翻译** —— 「自动检测 翻译成 xxx」那一行右边；
* **图文内容审查** —— 「开始审查」按钮右边。

这两个数**不能在前端估**：估出来的数字只是装饰，用户拿它判断「这块板子
快不快」就会被误导。⚠️ 而且**前端不许计时**（用户明确要求）—— 计时与
换算全在后端做完再发过去。

真实的数据链路（**四段**，缺一段数字就是假的）::

    SDK backend                DDS wire              宿主/插件            卡片前端
    backend.perf             worker: reply["perf"]  .perf / stats      #wt-test-perf
      (或 sink.perf)           (过 wire 过来)          tokens_per_second  #cr-perf
      (worker 进程内)

为什么必须过 wire
------------------
perf 住在 **worker 进程**里（``session._backend.sink.perf`` 或
``backend.perf``），而 Web UI 进程拿到的是 ``LLMRPCSession`` 代理，
**够不着** ⇒ worker 必须显式把数字放进 reply，client 再收进
``session.last_perf``。

⚠️ **只带数字**：perf 里可能有别的类型，JSON 编不过去 ⇒ **推理成功了
但调用方收到传输错误**。这是最坑的一种失败，所以本脚本专门断言「非数字
字段被丢掉、且不会炸」。

两种 backend
------------
* **通用 LLM**（翻译用的 HY-MT）：``backend.sink.perf`` —— 有解码 token，
  吞吐 = **解码速率**。
* **决策模型**（Intern-Decision）：**没有** ``StreamSink``（它只收
  logits）⇒ 自己记 ``backend.perf``。而且它每个字段跑一次
  ``max_new_tokens=1`` 的 **prefill-only** 推理 ⇒ 解码 token 恒为 0 ⇒
  吞吐 = **预填充速率**（提示词处理速度）。

本脚本**抠真代码**跑（不复读、不复刻）：
* ``wdndds.ai_session`` 的 ``_read_perf`` / ``LLMRPCSession._decode_reply``；
* ``wdnai`` 的 ``perf_table`` 与决策 backend 的 ``_record_perf``；
* ``app.core.translate.base.TranslationResult``；
* ``app.core.translate.npu_provider`` 的 ``_session_perf`` / ``_merge_perf``；
* ``app.core.translate.service.TranslateService``（用假 provider 驱动）；
* 插件 ``content_review.service`` 的 ``_perf_table``；
* 两张卡的 ``app.js``（dukpy + DOM stub）。

用法::

    .venv/bin/python scripts/verify_infer_perf.py
"""
from __future__ import annotations

import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

CARD = os.path.join(REPO, ".venv/src/wdnapps_audio_chat/src/wdnapps_audio_chat"
                          "/cards/web_translate")
CARD_APP_JS = os.path.join(CARD, "static/app.js")
CARD_TEMPLATE = os.path.join(CARD, "static/template.html")
CARD_CSS = os.path.join(CARD, "static/app.css")

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


# --------------------------------------------------------------------------- #
# DOM stub（跟 verify_web_translate_service_url.py 同款）
# --------------------------------------------------------------------------- #
DOM_STUB = r"""
function mkNode(tag) {
  const classes = new Set();
  const node = {
    tagName: tag, textContent: "", style: {}, dataset: {}, innerHTML: "",
    children: [], hidden: false, checked: false, value: "", type: "",
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
      toggle: (c) => (classes.has(c) ? classes.delete(c) : classes.add(c)),
    },
    appendChild(c) { this.children.push(c); return c; },
    removeChild(c) {
      const i = this.children.indexOf(c);
      if (i >= 0) this.children.splice(i, 1);
      return c;
    },
    addEventListener() {},
    removeEventListener() {},
    setAttribute() {},
    getAttribute() { return null; },
    select() {},
    querySelectorAll() { return []; },
  };
  Object.defineProperty(node, "className", {
    get: () => Array.from(classes).join(" "),
    set: (v) => {
      classes.clear();
      String(v == null ? "" : v).split(/\s+/)
        .filter(Boolean).forEach((c) => classes.add(c));
    },
  });
  Object.defineProperty(node, "innerHTML", {
    get: () => node.__html || "",
    set: (v) => {
      node.__html = String(v == null ? "" : v);
      if (node.__html === "") node.children.length = 0;
    },
  });
  return node;
}
const REG = {};
globalThis.document = {
  createElement: mkNode,
  getElementById: (id) => (REG[id] || (REG[id] = mkNode("div"))),
  body: mkNode("body"),
  visibilityState: "visible",
  execCommand: () => true,
};
globalThis.setTimeout = (fn) => { try { fn(); } catch (_) {} return 0; };
globalThis.setInterval = () => 0;
globalThis.clearTimeout = () => {};
globalThis.clearInterval = () => {};
globalThis.navigator = { clipboard: null, userAgent: "dukpy" };
globalThis.window = {
  location: { origin: "http://192.168.169.133:8000", href: "http://x/" },
  isSecureContext: false,
  addEventListener() {},
};
1;
"""


def _extract_js_function(src: str, name: str) -> str:
    """抠出 ``function <name>(...) {...}`` 的完整源码；抠不到返回空串。

    ⚠️ **按花括号配平** —— 正则会被嵌套花括号截断。
    """
    i = src.find("function %s(" % name)
    if i < 0:
        return ""
    start = src.find("{", i)
    if start < 0:
        return ""
    depth = 0
    for j in range(start, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    return ""


# --------------------------------------------------------------------------- #
# [1] DDS worker 侧：_read_perf
# --------------------------------------------------------------------------- #
def test_worker_read_perf() -> None:
    print("\n[1] worker 侧：_read_perf 只取数字")
    from wdndds.ai_session import _read_perf

    class _Sink:
        perf = {
            "n_decode_tokens": 42,
            "n_prefill_tokens": 17,
            "prefill_ms": 120.5,
            "decode_ms": 900.25,
        }

    class _Backend:
        sink = _Sink()

    class _Session:
        _backend = _Backend()

    got = _read_perf(_Session())
    check("读到了四个字段", len(got) == 4, str(got))
    check("token 数是 int", got.get("n_decode_tokens") == 42, str(got))
    check("decode 耗时保留小数", abs(got.get("decode_ms", 0) - 900.25) < 1e-6,
          str(got))

    # ⚠️ **非数字必须被丢掉** —— 否则 JSON 序列化失败 ⇒ 推理成功了却
    # 传输报错。
    class _DirtySink:
        perf = {"n_decode_tokens": 7, "weird": object(), "flag": True,
                "text": "abc"}

    class _DirtyBackend:
        sink = _DirtySink()

    class _DirtySession:
        _backend = _DirtyBackend()

    dirty = _read_perf(_DirtySession())
    check("非数字字段被丢掉", set(dirty) == {"n_decode_tokens"}, str(dirty))
    check("bool 被当成非数字丢掉", "flag" not in dirty, str(dirty))

    # 没有 sink / 没有 perf → 空字典，不许炸
    class _Empty:
        pass

    check("没有 sink 时返回空字典", _read_perf(_Empty()) == {},
          str(_read_perf(_Empty())))
    check("session 是 None 时返回空字典", _read_perf(None) == {}, "")


# --------------------------------------------------------------------------- #
# [2] DDS client 侧：_decode_reply 把 perf 收进 last_perf
# --------------------------------------------------------------------------- #
def test_client_decode_reply() -> None:
    print("\n[2] client 侧：_decode_reply 收下 perf")
    from wdndds.ai_session import LLMRPCSession

    class _Fake:
        service_name = "wdndds/rpc/fake"
        last_perf: dict = {}

    fake = _Fake()
    reply = {"ok": True, "data": {
        "status": "ok",
        "text": "你好",
        "perf": {"n_decode_tokens": 12, "decode_ms": 800.0},
    }}
    text, atts, stopped = LLMRPCSession._decode_reply(fake, reply)
    check("文本照常解出来", text == "你好", text)
    check("last_perf 收到了 token 数",
          fake.last_perf.get("n_decode_tokens") == 12, str(fake.last_perf))
    check("last_perf 收到了 decode 耗时",
          fake.last_perf.get("decode_ms") == 800.0, str(fake.last_perf))

    # 没有 perf 的 reply ⇒ last_perf 清空（不能留着上一轮的）
    fake2 = _Fake()
    fake2.last_perf = {"n_decode_tokens": 99}
    LLMRPCSession._decode_reply(fake2, {"ok": True, "data": {"status": "ok",
                                                           "text": "x"}})
    check("reply 里没 perf ⇒ last_perf 清空", fake2.last_perf == {},
          str(fake2.last_perf))

    # ⚠️ 非数字要被挡掉（worker 侧已经滤过，这里再兜一层）
    fake3 = _Fake()
    LLMRPCSession._decode_reply(fake3, {"ok": True, "data": {
        "status": "ok", "text": "x",
        "perf": {"n_decode_tokens": 3, "bad": "字符串"},
    }})
    check("非数字被挡掉", set(fake3.last_perf) == {"n_decode_tokens"},
          str(fake3.last_perf))


# --------------------------------------------------------------------------- #
# [3] 宿主：TranslationResult 带 perf
# --------------------------------------------------------------------------- #
def test_translation_result_perf() -> None:
    print("\n[3] TranslationResult 带 perf 字段")
    from app.core.translate.base import TranslationResult

    r = TranslationResult("译文", provider="npu",
                           perf={"n_decode_tokens": 5, "decode_ms": 100.0})
    check("perf 带上了", r.perf.get("n_decode_tokens") == 5, str(r.perf))
    check("perf 是 dict", isinstance(r.perf, dict), str(type(r.perf)))

    r2 = TranslationResult("译文", provider="npu")
    check("不给 perf 时是空字典", r2.perf == {}, str(r2.perf))

    # ⚠️ 非数字必须被丢掉（这个 dict 最后要进 JSON 响应）
    r3 = TranslationResult("x", provider="npu",
                           perf={"ok": 1, "bad": "字符串"})
    check("非数字被丢掉", r3.perf == {"ok": 1.0}, str(r3.perf))

    # 老代码照旧能用（它本身就是 str）
    check("本身还是 str", isinstance(r, str) and r == "译文", str(r))


# --------------------------------------------------------------------------- #
# [4] 宿主 provider：_session_perf / _merge_perf
# --------------------------------------------------------------------------- #
def test_sdk_perf_table() -> None:
    print("\n[4] SDK：perf_table 算出来的 tokens/s")
    from wdnai import perf_table

    t = perf_table({"n_decode_tokens": 42, "n_prefill_tokens": 9,
                    "prefill_ms": 120.0, "decode_ms": 2100.0})
    check("解码吞吐 = 解码 token / 解码秒",
          abs(t["decode_tokens_per_second"] - 20.0) < 1e-6, str(t))
    check("预填充吞吐 = 预填充 token / 预填充秒",
          abs(t["prefill_tokens_per_second"] - 75.0) < 1e-6, str(t))
    check("LLM 延迟 = 预填充 + 解码",
          abs(t["llm_latency_ms"] - 2220.0) < 1e-6, str(t))
    check("每 token 耗时也算出来了",
          abs(t["decode_ms_per_token"] - 50.0) < 1e-6, str(t))

    # ⚠️ 计数为 0 ⇒ 速率必须是 0，**不能是 inf/nan** ——
    # 这些数要进 JSON 响应，inf 编不过去 ⇒ 推理成功了前端却收到 500。
    z = perf_table({})
    check("空表全是 0", all(v == 0 for v in z.values()), str(z))
    check("没有 inf/nan",
          all(v == v and abs(v) != float("inf") for v in z.values()), str(z))
    z2 = perf_table({"n_decode_tokens": 5, "decode_ms": 0.0})
    check("耗时为 0 时速率也是 0", z2["decode_tokens_per_second"] == 0.0, str(z2))

    # ⚠️ **公式必须和 printf_perf 打印的一致**（同一份算法，不会两边漂）
    import inspect
    from wdnai.backends import _common
    src = inspect.getsource(_common.printf_perf)
    check("printf_perf 复用了 perf_table", "perf_table(" in src, src[:400])


def test_provider_perf_helpers() -> None:
    print("\n[5] provider：取 perf / 累加 perf")
    from app.core.translate.npu_provider import _merge_perf, _session_perf

    class _Session:
        last_perf = {"n_decode_tokens": 10, "decode_ms": 500.0}

    check("从 last_perf 读到", _session_perf(_Session()).get("n_decode_tokens") == 10,
          str(_session_perf(_Session())))

    # 退路：本地 session（没走 RPC）—— 直接读 SDK 的 sink
    class _Sink:
        perf = {"n_decode_tokens": 8, "decode_ms": 400.0}

    class _Backend:
        sink = _Sink()

    class _Local:
        _backend = _Backend()

    check("本地 session 走 sink 退路",
          _session_perf(_Local()).get("n_decode_tokens") == 8,
          str(_session_perf(_Local())))
    check("没有 perf 时是空字典", _session_perf(object()) == {}, "")

    # ⚠️ **累加**而不是覆盖：一条文案可能要试好几条措辞，每条都是一次推理
    total: dict = {}
    _merge_perf(total, {"n_decode_tokens": 10, "decode_ms": 500.0,
                        "n_prefill_tokens": 3, "prefill_ms": 50.0})
    _merge_perf(total, {"n_decode_tokens": 7, "decode_ms": 300.0,
                        "n_prefill_tokens": 2, "prefill_ms": 20.0})
    check("token 数是累加的", total.get("n_decode_tokens") == 17, str(total))
    check("decode 耗时是累加的", abs(total.get("decode_ms", 0) - 800.0) < 1e-6,
          str(total))
    check("预填充 token 也是累加的", total.get("n_prefill_tokens") == 5, str(total))

    # 非数字不许进来
    total2: dict = {}
    _merge_perf(total2, {"n_decode_tokens": 1, "bad": "字符串", "flag": True})
    check("非数字被丢掉", total2 == {"n_decode_tokens": 1.0}, str(total2))
    check("传 None 不炸", _merge_perf(total2, None) is total2, "")


# --------------------------------------------------------------------------- #
# [5] 宿主服务层：stats 里带 elapsed_ms + perf
# --------------------------------------------------------------------------- #
def test_service_stats() -> None:
    print("\n[5] 服务层：stats 带端到端耗时与 perf")
    from app.core.translate.base import TranslationResult
    from app.core.translate.providers import ProviderChain
    from app.core.translate.service import TranslateService

    class _PerfProvider:
        name = "npu"
        local = True

        def translate(self, text, source, target):
            # ⚠️ **照着编号列表回**（服务层是按编号切回来的）—— 不带编号
            # 会走「逐条翻」的退路，perf 会被计两遍。
            lines = []
            for i, ln in enumerate((text or "").split("\n"), 1):
                if not ln.strip():
                    continue
                lines.append("%d. 译文 %s" % (i, ln.strip()))
            return TranslationResult(
                "\n".join(lines), provider="npu",
                perf={"n_decode_tokens": 11, "decode_ms": 1100.0,
                      "n_prefill_tokens": 4, "prefill_ms": 60.0})

    svc = TranslateService(ProviderChain([_PerfProvider()]))
    out, stats = svc.translate_segments(["系统运行正常"], "zh-CN", "en")
    check("翻出来了", out and out[0].startswith("译文"), str(out))
    check("stats 有 elapsed_ms", "elapsed_ms" in stats, str(stats))
    check("elapsed_ms 是整数", isinstance(stats.get("elapsed_ms"), int),
          str(stats.get("elapsed_ms")))
    check("elapsed_ms >= 0", stats.get("elapsed_ms", -1) >= 0, str(stats))
    check("stats 有 elapsed_s（给界面直接显示）", "elapsed_s" in stats, str(stats))
    check("elapsed_s 是秒", abs(stats.get("elapsed_s", 0)
                             - stats["elapsed_ms"] / 1000.0) < 0.011, str(stats))
    check("stats 带 perf", isinstance(stats.get("perf"), dict), str(stats))
    check("perf 里 token 数对得上",
          stats["perf"].get("n_decode_tokens") == 11, str(stats.get("perf")))
    check("perf 里 decode 耗时对得上",
          abs(stats["perf"].get("decode_ms", 0) - 1100.0) < 1e-6,
          str(stats.get("perf")))
    # ⚠️ **tokens/s 是宿主算好的**（用 SDK 的公式）—— 前端不做这个换算
    check("stats 带 tokens_per_second", "tokens_per_second" in stats, str(stats))
    check("tokens_per_second = 解码 token / 解码秒",
          abs(stats.get("tokens_per_second", 0)
              - 11 / (1100.0 / 1000.0)) < 0.05, str(stats.get("tokens_per_second")))
    check("perf 里有 SDK 算好的派生量",
          abs(stats["perf"].get("decode_tokens_per_second", 0)
              - 11 / (1100.0 / 1000.0)) < 0.05, str(stats.get("perf")))

    # 外网 provider（不推理）⇒ perf 是空的，但 elapsed_ms 照样有
    class _PlainProvider:
        name = "youdao"
        local = False

        def translate(self, text, source, target):
            return "译文 " + text

    svc2 = TranslateService(ProviderChain([_PlainProvider()]))
    _, stats2 = svc2.translate_segments(["系统运行正常"], "zh-CN", "en")
    check("不推理的 provider ⇒ perf 为空", stats2.get("perf") == {},
          str(stats2.get("perf")))
    check("不推理的 provider ⇒ 也有 elapsed_ms",
          isinstance(stats2.get("elapsed_ms"), int), str(stats2))
    check("不推理的 provider ⇒ tokens_per_second 是 0",
          stats2.get("tokens_per_second") == 0, str(stats2.get("tokens_per_second")))


# --------------------------------------------------------------------------- #
# [6] 卡片前端：_renderPerf / _clearPerf
# --------------------------------------------------------------------------- #
def test_card_perf_render() -> None:
    print("\n[6] 卡片前端：只显示后端算好的数（前端不计时）")
    import dukpy

    src = open(CARD_APP_JS, encoding="utf-8").read()
    fn = _extract_js_function(src, "_renderPerf")
    clear = _extract_js_function(src, "_clearPerf")
    check("app.js 里有 _renderPerf", bool(fn), "")
    check("app.js 里有 _clearPerf", bool(clear), "")
    if not fn or not clear:
        return

    js = DOM_STUB + "\n" + fn + "\n" + clear + r"""
const el = document.getElementById("wt-test-perf");
_renderPerf(el, { elapsed_ms: 1830, elapsed_s: 1.83, tokens_per_second: 20.0,
                  perf: { decode_tokens: 42, prefill_tokens: 9,
                          decode_ms: 2100, prefill_ms: 120, llm_latency_ms: 2220 } });
JSON.stringify({
  text: el.textContent, hidden: el.hidden, title: el.title,
});
"""

    interp = dukpy.JSInterpreter()
    raw = interp.evaljs(js)
    got = json.loads(raw if isinstance(raw, str) else str(raw))
    check("显示了总耗时", "1.83 s" in got["text"], got["text"])
    check("显示了 tokens/s", "20.0 tok/s" in got["text"], got["text"])
    check("有标题（悬停细账）", "解码 42 token" in got["title"], got["title"])
    check("不是隐藏的", got["hidden"] is False, str(got))

    # ⚠️ **前端不许自己算** —— 源码里不能出现「拿 token 数除以耗时」这种
    # 换算，也不能拿 Date.now() 自己计时。
    body = fn
    check("前端没有用 Date.now() 计时", "Date.now" not in body, body[:200])
    check("前端没有用 performance.now() 计时",
          "performance.now" not in body, body[:200])
    check("前端没有拿原始计数自己算（没碰 n_decode_tokens）",
          "n_decode_tokens" not in body, body[:400])
    check("前端没有拿原始计数自己算（没碰 n_prefill_tokens）",
          "n_prefill_tokens" not in body, body[:400])
    check("前端没有自己做 ms→秒 的换算", "/ 1000" not in body
          and "1000.0 /" not in body, body[:400])
    check("前端用的是后端算好的 tokens_per_second",
          "tokens_per_second" in body, body[:400])
    check("前端用的是后端算好的 elapsed_s",
          "elapsed_s" in body, body[:400])

    # 外网 provider：没有 tokens/s ⇒ 只显示总时间，**不编 tok/s**
    js2 = DOM_STUB + "\n" + fn + "\n" + clear + r"""
const el = document.getElementById("wt-test-perf");
_renderPerf(el, { elapsed_ms: 2500, elapsed_s: 2.5, tokens_per_second: 0, perf: {} });
JSON.stringify({ text: el.textContent, hidden: el.hidden });
"""
    interp2 = dukpy.JSInterpreter()
    raw2 = interp2.evaljs(js2)
    got2 = json.loads(raw2 if isinstance(raw2, str) else str(raw2))
    check("无 perf 时仍显示总耗时", "2.50 s" in got2["text"], got2["text"])
    check("无 perf 时**不编造** tok/s", "tok/s" not in got2["text"], got2["text"])

    # 缓存命中：耗时 0，但那是**真实的** ⇒ 照常显示，不能藏
    js2b = DOM_STUB + "\n" + fn + "\n" + clear + r"""
const el = document.getElementById("wt-test-perf");
_renderPerf(el, { elapsed_ms: 0, elapsed_s: 0, tokens_per_second: 0,
                  cached: 1, perf: {} });
JSON.stringify({ text: el.textContent, hidden: el.hidden });
"""
    interp2b = dukpy.JSInterpreter()
    raw2b = interp2b.evaljs(js2b)
    got2b = json.loads(raw2b if isinstance(raw2b, str) else str(raw2b))
    check("缓存命中时显示 0.00 s", "0.00 s" in got2b["text"], got2b["text"])
    check("缓存命中有标注", "缓存命中" in got2b["text"], got2b["text"])
    check("缓存命中时不隐藏", got2b["hidden"] is False, str(got2b))

    # 什么都没有 ⇒ 藏起来
    js3 = DOM_STUB + "\n" + fn + "\n" + clear + r"""
const el = document.getElementById("wt-test-perf");
_renderPerf(el, {});
JSON.stringify({ text: el.textContent, hidden: el.hidden });
"""
    interp3 = dukpy.JSInterpreter()
    raw3 = interp3.evaljs(js3)
    got3 = json.loads(raw3 if isinstance(raw3, str) else str(raw3))
    check("没数据时藏起来", got3["hidden"] is True and got3["text"] == "",
          str(got3))

    # _clearPerf
    js4 = DOM_STUB + "\n" + fn + "\n" + clear + r"""
const el = document.getElementById("wt-test-perf");
_renderPerf(el, { elapsed_s: 1.0, tokens_per_second: 5.0,
                  perf: { decode_tokens: 5, decode_ms: 100 } });
_clearPerf(el);
JSON.stringify({ text: el.textContent, hidden: el.hidden, title: el.title });
"""
    interp4 = dukpy.JSInterpreter()
    raw4 = interp4.evaljs(js4)
    got4 = json.loads(raw4 if isinstance(raw4, str) else str(raw4))
    check("清空后隐藏", got4["hidden"] is True and got4["text"] == "", str(got4))


# --------------------------------------------------------------------------- #
# [7] 模板 / 样式：计时位在「自动检测 翻译成 xxx」那一行里
# --------------------------------------------------------------------------- #
def test_template_layout() -> None:
    print("\n[7] 模板：计时位挂在「翻译成」那一行")
    html = open(CARD_TEMPLATE, encoding="utf-8").read()
    css = open(CARD_CSS, encoding="utf-8").read()

    check("模板里有 #wt-test-perf", 'id="wt-test-perf"' in html, "")
    check("计时位默认隐藏", 'id="wt-test-perf" hidden' in html, "")

    # 它必须在 .wt-test-actions 这一行里（源语言下拉 + 翻译成 + 目标语言）
    m = re.search(r'<div class="wt-test-actions">(.*?)</div>', html, re.S)
    check("找得到 .wt-test-actions 那一行", m is not None, "")
    if m:
        row = m.group(1)
        check("计时位在那一行里", 'id="wt-test-perf"' in row, row[-300:])
        check("源语言下拉在那一行里", 'id="wt-test-source"' in row, "")
        check("目标语言下拉在那一行里", 'id="wt-test-target"' in row, "")
        check("计时位在目标语言**之后**（行右边）",
              row.find('id="wt-test-perf"') > row.find('id="wt-test-target"'),
              "")

    check("CSS 有 .wt-test-perf 规则", ".wt-test-perf" in css, "")
    check("CSS 把计时位顶到右边", "margin-left: auto" in css, "")


# --------------------------------------------------------------------------- #
# [8] 图文内容审查：同一条链路，但 backend 不一样
# --------------------------------------------------------------------------- #
REVIEW_CARD = os.path.join(REPO, ".venv/src/wdnapps_audio_chat/src/wdnapps_audio_chat"
                               "/cards/content_review")
REVIEW_APP_JS = os.path.join(REVIEW_CARD, "static/app.js")
REVIEW_TEMPLATE = os.path.join(REVIEW_CARD, "static/template.html")
REVIEW_CSS = os.path.join(REVIEW_CARD, "static/app.css")


def test_decision_backend_perf() -> None:
    print("\n[8] 决策模型 backend：自己记性能表（它没有 StreamSink）")
    from wdnai.backends.intern_decision import InternDecisionBackend

    class _Fake:
        # ⚠️ **每个实例一份**（写成类属性的话两个实例会共用同一个 dict，
        # 累加断言就会互相污染）
        def __init__(self):
            self._perf: dict = {}

    fake = _Fake()
    # ⚠️ session_run 返回 (n_decode, n_prefill, llm_start, llm_end)
    InternDecisionBackend._record_perf(fake, (0, 120, 100.0, 100.4), 500.0)
    check("记下了预填充 token 数",
          fake._perf.get("n_prefill_tokens") == 120, str(fake._perf))
    check("用 C 侧的墙钟锚点算耗时",
          abs(fake._perf.get("prefill_ms", 0) - 400.0) < 1e-6, str(fake._perf))

    # 第二个字段 ⇒ **累加**（每个字段跑一次推理）
    InternDecisionBackend._record_perf(fake, (0, 80, 200.0, 200.2), 500.0)
    check("多个字段的 token 是累加的",
          fake._perf.get("n_prefill_tokens") == 200, str(fake._perf))
    check("多个字段的耗时也是累加的",
          abs(fake._perf.get("prefill_ms", 0) - 600.0) < 1e-6, str(fake._perf))

    # ⚠️ **prefill-only ⇒ 解码 token 恒为 0** ⇒ 吞吐走预填充速率
    from wdnai import perf_table
    t = perf_table(fake._perf)
    check("prefill-only 的主吞吐 = 预填充速率",
          abs(t["tokens_per_second"] - 200 / 0.6) < 1e-6, str(t))
    check("解码速率是 0（它压根不解码）",
          t["decode_tokens_per_second"] == 0.0, str(t))

    # 墙钟退路：C 侧锚点缺失时用 Python 侧的墙钟
    fake2 = _Fake()
    InternDecisionBackend._record_perf(fake2, None, 250.0)
    check("锚点缺失时用墙钟退路",
          abs(fake2._perf.get("prefill_ms", 0) - 250.0) < 1e-6, str(fake2._perf))


def test_worker_reads_backend_perf() -> None:
    print("\n[9] worker 侧：backend.perf 也要读得到（决策模型没有 sink）")
    from wdndds.ai_session import _read_perf

    class _Backend:
        # ⚠️ **没有 sink** —— 决策模型就是这种
        perf = {"n_prefill_tokens": 120.0, "prefill_ms": 400.0}

    class _Session:
        _backend = _Backend()

    got = _read_perf(_Session())
    check("读到了 backend.perf",
          got.get("n_prefill_tokens") == 120.0, str(got))
    check("耗时也读到了",
          abs(got.get("prefill_ms", 0) - 400.0) < 1e-6, str(got))


def test_review_service_perf() -> None:
    print("\n[10] 审查 service：把 perf 算好再发给前端")
    from wdnapps_audio_chat.cards.content_review.service import _perf_table

    class _Session:
        last_perf = {"n_prefill_tokens": 200.0, "prefill_ms": 600.0}

    t = _perf_table(_Session())
    check("算出了主吞吐",
          abs(t.get("tokens_per_second", 0) - 200 / 0.6) < 1e-6, str(t))
    check("原始计数也在", t.get("n_prefill_tokens") == 200.0, str(t))

    class _Empty:
        last_perf: dict = {}

    check("没有 perf 时是空字典", _perf_table(_Empty()) == {},
          str(_perf_table(_Empty())))
    check("session 是 None 时不炸", _perf_table(None) == {}, "")


def test_review_card_render() -> None:
    print("\n[11] 审查卡前端：按钮右边显示后端算好的数")
    import dukpy

    src = open(REVIEW_APP_JS, encoding="utf-8").read()
    fn = _extract_js_function(src, "renderPerf")
    clear = _extract_js_function(src, "clearPerf")
    check("app.js 里有 renderPerf", bool(fn), "")
    check("app.js 里有 clearPerf", bool(clear), "")
    if not fn or not clear:
        return

    js = DOM_STUB + "\n" + fn + "\n" + clear + r"""
const el = document.getElementById("cr-perf");
renderPerf(el, { elapsed_sec: 9.12, tokens_per_second: 320.5,
                 perf: { prefill_tokens: 200, prefill_ms: 600,
                         llm_latency_ms: 600 } });
JSON.stringify({ text: el.textContent, hidden: el.hidden, title: el.title });
"""
    interp = dukpy.JSInterpreter()
    raw = interp.evaljs(js)
    got = json.loads(raw if isinstance(raw, str) else str(raw))
    check("显示了总耗时", "9.12 s" in got["text"], got["text"])
    check("显示了 tokens/s", "320.5 tok/s" in got["text"], got["text"])
    check("有标题（悬停细账）", "预填充 200 token" in got["title"], got["title"])
    check("不是隐藏的", got["hidden"] is False, str(got))

    # ⚠️ **前端不许自己算**
    check("前端没有用 Date.now() 计时", "Date.now" not in fn, fn[:200])
    check("前端没有用 performance.now() 计时",
          "performance.now" not in fn, fn[:200])
    check("前端没有自己算速率（没碰 prefill_tokens）",
          "prefill_tokens" not in fn.replace("perf.prefill_tokens", ""),
          fn[:400])
    check("前端用的是后端算好的 tokens_per_second",
          "tokens_per_second" in fn, fn[:400])

    # 没有 perf ⇒ 只显示总耗时
    js2 = DOM_STUB + "\n" + fn + "\n" + clear + r"""
const el = document.getElementById("cr-perf");
renderPerf(el, { elapsed_sec: 9.12, tokens_per_second: 0, perf: {} });
JSON.stringify({ text: el.textContent, hidden: el.hidden });
"""
    interp2 = dukpy.JSInterpreter()
    raw2 = interp2.evaljs(js2)
    got2 = json.loads(raw2 if isinstance(raw2, str) else str(raw2))
    check("无 perf 时仍显示总耗时", "9.12 s" in got2["text"], got2["text"])
    check("无 perf 时**不编造** tok/s", "tok/s" not in got2["text"], got2["text"])


def test_review_template_layout() -> None:
    print("\n[12] 审查卡模板：计时位在「开始审查」按钮右边")
    html = open(REVIEW_TEMPLATE, encoding="utf-8").read()
    css = open(REVIEW_CSS, encoding="utf-8").read()

    check("模板里有 #cr-perf", 'id="cr-perf"' in html, "")
    check("计时位默认隐藏", 'id="cr-perf" hidden' in html, "")

    m = re.search(r'<div class="cr-run-row">(.*?)</div>', html, re.S)
    check("找得到 .cr-run-row 那一行", m is not None, "")
    if m:
        row = m.group(1)
        check("计时位在那一行里", 'id="cr-perf"' in row, row[-300:])
        check("「开始审查」按钮在那一行里", 'id="cr-run-btn"' in row, "")
        check("计时位在按钮**右边**",
              row.find('id="cr-perf"') > row.find('id="cr-run-btn"'), "")

    check("CSS 有 .cr-perf 规则", ".cr-perf" in css, "")


# --------------------------------------------------------------------------- #
def main() -> int:
    print("=" * 72)
    print("推理性能透出自检（总时间 + tokens/s）")
    print("=" * 72)

    test_worker_read_perf()
    test_client_decode_reply()
    test_translation_result_perf()
    test_sdk_perf_table()
    test_provider_perf_helpers()
    test_service_stats()
    test_card_perf_render()
    test_template_layout()
    test_decision_backend_perf()
    test_worker_reads_backend_perf()
    test_review_service_perf()
    test_review_card_render()
    test_review_template_layout()

    print("\n" + "=" * 72)
    print(f"通过 {_PASS} 项,失败 {_FAIL} 项")
    print("=" * 72)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
