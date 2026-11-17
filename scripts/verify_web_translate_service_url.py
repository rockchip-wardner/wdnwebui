#!/usr/bin/env python3
"""网页翻译「服务地址」自检 —— 向导走完后给出的那个 URL 是不是真的能用。

背景
----
网页翻译卡片走完向导后要告诉用户「这个服务在哪儿」：别的程序照着这个
地址就能调板子的翻译接口。地址出现在两个地方：

* **向导的完成页**（框架 ``state.js`` 的 ``finish_extra`` 扩展点）；
* **卡片功能页的「服务详情」**（``服务地址`` 行 + 一行 curl 示例）。

踩过的坑
--------
⚠️ **给出的地址必须是真的路由** —— 写死一个 ``/api/translate`` 看着像那么
回事，真打过去 404，用户拿着它去接别的设备就白忙一场。所以本脚本会
**对照后端的路由定义**核一遍（``app/system/api/translate.py`` 里的
``@router.post("/translate")`` + system 层的 ``prefix="/api"``）。

⚠️ **host/协议不能由后端给** —— 板子前面有 TLS relay，用户可能从
``http://板子IP:8000`` 也可能从 ``https://板子IP:8443`` 访问，端口只有
浏览器知道 ⇒ 地址一律用 ``location.origin`` 拼。

本脚本**抠真前端代码**跑（不复读、不复刻）：
* 卡片 ``app.js`` —— 整个 IIFE 在最小 DOM stub 里执行，断言渲染出来的
  ``#wt-service-url`` 文本；
* 框架 ``state.js`` 的 ``_renderFinishStep`` —— 抠出来配 stub 跑，断言
  完成页的摘要里有「服务地址: <url>」。

用法::

    .venv/bin/python scripts/verify_web_translate_service_url.py
    .venv/bin/python scripts/verify_web_translate_service_url.py --online
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

CARD = os.path.join(REPO, ".venv/src/wdnapps_audio_chat/src/wdnapps_audio_chat"
                          "/cards/web_translate")
WIZARD_JSON = os.path.join(CARD, "static/wizard.json")
TEMPLATE_HTML = os.path.join(CARD, "static/template.html")
CARD_APP_JS = os.path.join(CARD, "static/app.js")
CARD_CSS = os.path.join(CARD, "static/app.css")
CARD_API = os.path.join(CARD, "api.py")
STATE_JS = os.path.join(REPO, "app/static/core/wizard/state.js")
FRAMEWORK_TRANSLATE_JS = os.path.join(REPO, "app/static/core/translate.js")
TRANSLATE_API = os.path.join(REPO, "app/system/api/translate.py")
SYSTEM_INIT = os.path.join(REPO, "app/system/__init__.py")

# 脚本里用到的 origin（固定值，方便断言）
ORIGIN = "http://192.168.169.133:8000"

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
# DOM stub（卡片 app.js 与框架 state.js 共用）
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
  // innerHTML = "" 在**浏览器里会清掉子节点**。stub 必须照做，否则
  // 「重新填一遍下拉框」会被误判成「选项累积了 N 份」。
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
  location: { origin: "ORIGIN_HERE", href: "ORIGIN_HERE/" },
  isSecureContext: false,
  addEventListener() {},
};
1;
"""


# --------------------------------------------------------------------------- #
# [3] 卡片 app.js —— 真跑整个 IIFE
# --------------------------------------------------------------------------- #
def run_card_app_js(live_languages: str = "[]") -> dict:
    """把卡片 app.js 原样跑一遍（配 DOM stub），取出它渲染出来的字段。

    ``live_languages`` = 注入到 ``window.Translate.languages`` 的 JSON 文本
    （模拟框架那份清单已就绪 / 还没就绪）。
    """
    import dukpy
    src = open(CARD_APP_JS, encoding="utf-8").read()
    stub = DOM_STUB.replace("ORIGIN_HERE", ORIGIN)
    stub += r"""
globalThis.fetch = async (url) => ({
  ok: true,
  status: 200,
  json: async () => ({
    enabled: true, model_id: "hy-mt-1-5", npu_index: 0,
    provider_in_chain: true, instance_status: "running",
    running_count: 4, expected_count: 4, instances: [], cards: [],
    detail: "", conflict: "", instance_error: "",
  }),
});
"""
    stub += "globalThis.window.Translate = { languages: " + live_languages + " };\n"
    stub += "1;   // dukpy 的 evaljs 要求最后一句有值（否则 \"Invalid Result Value\"）\n"
    probe = r"""
JSON.stringify({
  url: (REG["wt-service-url"] || {}).textContent || "",
  curl: (REG["wt-service-curl"] || {}).textContent || "",
  options: ((REG["wt-test-target"] || {}).children || []).map(
    (o) => ({ value: o.value || "", text: o.textContent || "" })
  ),
});
"""
    interp = dukpy.JSInterpreter()
    interp.evaljs(stub)
    interp.evaljs(src)
    raw = interp.evaljs(probe)
    return json.loads(raw if isinstance(raw, str) else str(raw))


# --------------------------------------------------------------------------- #
# [4] 框架 _renderFinishStep —— 抠出来真跑
# --------------------------------------------------------------------------- #
def extract_finish_fn() -> str:
    """把 ``_renderFinishStep``（连同它依赖的 ``_ce``）原样抠出来。"""
    src = open(STATE_JS, encoding="utf-8").read()

    def grab(start_sig: str) -> str:
        start = src.index(start_sig)
        end = src.index("\n  }\n", start) + len("\n  }\n")
        return src[start:end]

    wiz_ns = re.search(r'const WIZARD_NS = "[^"]+";', src).group(0)
    ce = grab("  function _ce(tag, cls, text) {")
    finish = grab("  function _renderFinishStep(body, foot, cfg, stepDef, state) {")
    return wiz_ns + "\n" + ce + "\n" + finish + "\n"


def run_finish_step(cfg: dict) -> str:
    """跑 _renderFinishStep，返回完成页摘要的纯文本。"""
    import dukpy
    fn = extract_finish_fn()
    js = DOM_STUB.replace("ORIGIN_HERE", ORIGIN) + "\n" + fn + r"""
// _renderFinishStep 用到的三个自由变量（抠出来后就成了顶层未定义符号）
function _renderFootNext() {}
async function _finish() {}
function _renderFinishError() {}

const body = document.createElement("div");
const foot = document.createElement("div");
_renderFinishStep(body, foot, CFG, { title: "完成配置", kind: "finish" },
                   { values: {}, currentStep: 1 });
function textOf(node) {
  const kids = (node.children || []).map(textOf).join(" ");
  return ((node.textContent || "") + " " + kids).trim();
}
let summary = "";
(function walk(n) {
  if (String(n.className || "").indexOf("summary") >= 0) {
    summary = textOf(n);
    return;
  }
  (n.children || []).forEach(walk);
})(body);
summary;
"""
    js = js.replace("CFG", json.dumps(cfg))
    interp = dukpy.JSInterpreter()
    raw = interp.evaljs(js)
    return raw if isinstance(raw, str) else str(raw)


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--online", action="store_true",
                    help="额外用 HTTP 打一下 /api/translate/status")
    args = ap.parse_args()

    print("=" * 72)
    print("网页翻译「服务地址」自检")
    print("=" * 72)

    # ---------------------------------------------------------------- [1] --
    print("\n[1] 卡片在 wizard.json 里声明了 finish_extra")
    wiz = json.load(open(WIZARD_JSON, encoding="utf-8"))
    extra = wiz.get("finish_extra") or []
    check("wizard.json 有 finish_extra", bool(extra), str(extra))
    urls = [e.get("url_path") for e in extra if isinstance(e, dict)]
    check("声明了「服务地址」这一行",
          any(e.get("label") == "服务地址" for e in extra if isinstance(e, dict)),
          str(extra))
    check("每个 url_path 都是 /api 开头（相对路径）",
          all(u and u.startswith("/api/") for u in urls), str(urls))

    # ---------------------------------------------------------------- [2] --
    print("\n[2] 卡片功能页有「服务地址」那一行")
    tpl = open(TEMPLATE_HTML, encoding="utf-8").read()
    check("模板里有 wt-service-url 元素", 'id="wt-service-url"' in tpl)
    check("模板里有 wt-copy-url 复制按钮", 'id="wt-copy-url"' in tpl)
    check("模板里有 wt-service-curl 调用示例", 'id="wt-service-curl"' in tpl)

    # ---------------------------------------------------------------- [3] --
    print("\n[3] 真跑卡片 app.js，看它渲染出来的地址")
    card = run_card_app_js()
    print("      wt-service-url  =", card["url"])
    check("渲染出 origin + /api/translate",
          card["url"] == ORIGIN + "/api/translate", repr(card["url"]))
    check("地址里带的是 location.origin（后端不猜端口）",
          card["url"].startswith(ORIGIN), repr(card["url"]))
    check("curl 示例里有这个地址和 POST",
          card["url"] in card["curl"] and "curl -X POST" in card["curl"],
          repr(card["curl"][:120]))

    # ---------------------------------------------------------------- [4] --
    print("\n[4] 真跑框架 _renderFinishStep，看完成页的摘要")
    summary = run_finish_step(wiz)
    print("      摘要 =", summary.replace("\n", " ")[:160])
    check("完成页摘要里出现「服务地址」", "服务地址" in summary, repr(summary))
    check("完成页摘要里是 origin + /api/translate",
          ORIGIN + "/api/translate" in summary, repr(summary))
    check("有 finish_extra 时不再显示「(未填任何配置)」",
          "(未填任何配置)" not in summary, repr(summary))
    # 负向：不声明 finish_extra 时仍应显示「(未填任何配置)」
    summary_empty = run_finish_step({"steps": wiz.get("steps", [])})
    check("没声明 finish_extra 时还是「(未填任何配置)」",
          "(未填任何配置)" in summary_empty, repr(summary_empty))

    # ---------------------------------------------------------------- [5] --
    print("\n[5] 这个地址得是真路由（不然用户拿着 404 去接设备）")
    api_src = open(TRANSLATE_API, encoding="utf-8").read()
    check("translate.py 里有 POST /translate",
          '@router.post("/translate")' in api_src)
    check("translate.py 里有 GET /translate/status",
          '@router.get("/translate/status")' in api_src)
    sys_init = open(SYSTEM_INIT, encoding="utf-8").read()
    check("translate 路由挂在 /api 前缀下",
          "translate.router" in sys_init and 'prefix="/api"' in sys_init)
    check("wizard.json 里声明的路径都有对应路由",
          all(u in ("/api/translate", "/api/translate/status") for u in urls),
          str(urls))

    if args.online:
        print("\n[6] 在线：打一下状态端点")
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:8000/api/translate/status", timeout=20) as r:
                body = json.loads(r.read().decode())
            check("GET /api/translate/status = 200", True)
            check("返回里带 provider 链", "provider" in body, str(body)[:200])
        except Exception as exc:  # noqa: BLE001
            check("GET /api/translate/status = 200", False, repr(exc))

    # ---------------------------------------------------------------- [6] --
    print("\n[6] 紧凑布局（别让「服务详情」底下空一大块）")
    css = open(CARD_CSS, encoding="utf-8").read()
    check("卡片高度改回按内容自适应（min-height:0）",
          "#page-web-translate .imb-card" in css
          and "height: auto;" in css and "min-height: 0;" in css)
    check("紧凑覆盖都限定在 #page-web-translate 作用域内",
          css.count("#page-web-translate") >= 8, str(css.count("#page-web-translate")))
    unscoped = []
    for line in css.splitlines():
        t = line.strip()
        if (t.startswith(".imb-card") or t.startswith(".page-header")
                or t.startswith(".raw-output")) and "{" in t:
            unscoped.append(t)
    check("没有没限定作用域的 .imb-card/.page-header 覆盖",
          not unscoped, str(unscoped))
    # ⚠️ 两张卡必须**上下堆叠**（用户明确要求：不要并排）
    check("没有并排容器（两张卡保持上下堆叠）",
          "wt-grid" not in tpl and "wt-grid" not in css)
    check("「服务详情」和「试一下」都在页面里",
          "服务详情" in tpl and "试一下" in tpl)

    # ---------------------------------------------------------------- [7] --
    print("\n[7] 「试一下」的下拉框是**目标**语言（源语言固定中文）")
    js_src = open(CARD_APP_JS, encoding="utf-8").read()
    card_api = open(CARD_API, encoding="utf-8").read()
    check("下拉框标了 aria-label=目标语言", 'aria-label="目标语言"' in tpl)
    check("下拉框 id 是 wt-test-target", 'id="wt-test-target"' in tpl)
    check("值取自下拉框", '$("wt-test-target")' in js_src)
    check("请求里 source 固定 zh-CN", 'source: "zh-CN"' in js_src)
    check("请求里 target 用的是下拉框的值", "target: target" in js_src)
    check("后端 payload 同样是 source=zh-CN / target",
          'source: str = "zh-CN"' in card_api and 'target: str = "en"' in card_api,
          card_api[:0])

    # 按钮叫「翻译成」且在**下拉框左边** —— 连读成「翻译成 ▾日语」
    i_btn = tpl.find('id="wt-test-btn"')
    i_sel = tpl.find('id="wt-test-target"')
    check("有「翻译成」按钮", i_btn >= 0 and "翻译成" in tpl, str(i_btn))
    check("按钮在**下拉框左边**",
          i_btn >= 0 and i_sel > i_btn, "btn=%d select=%d" % (i_btn, i_sel))
    btn_tag = ""
    if i_btn >= 0:
        m = re.search(r'<button[^>]*id="wt-test-btn"[^>]*>([^<]*)</button>', tpl)
        btn_tag = (m.group(1).strip() if m else "")
    check("按钮上的文字是「翻译成」", btn_tag == "翻译成", repr(btn_tag))
    check("下拉框在按钮**右边**（模板顺序）",
          tpl.find('<select id="wt-test-target"') > tpl.find('<button', 0, i_btn))

    # ---------------------------------------------------------------- [8] --
    print("\n[8] 目标语言清单 = 右上角「界面语言」那份（22 项，不能漏）")
    # 框架那份（app/static/core/translate.js 的 LANGUAGES，21 种）
    fw_src = open(FRAMEWORK_TRANSLATE_JS, encoding="utf-8").read()
    fw = re.findall(r'\{\s*code:\s*"([^"]+)",\s*zh:\s*"([^"]+)"', fw_src)
    check("框架的界面语言清单解析得到 21 种", len(fw) == 21, str(len(fw)))

    # 卡片那份（template.html 里硬编码的 <option>）
    opts = re.findall(r'<option value="([^"]+)"([^>]*)>([^<]+)</option>', tpl)
    check("卡片下拉框有 22 项（中文 + 21 种）", len(opts) == 22, str(len(opts)))
    check("第一项是「中文」",
          bool(opts) and opts[0][0] == "zh-CN" and opts[0][2].strip() == "中文",
          str(opts[:1]))
    check("中文那一项**不带**「(原文)」",
          bool(opts) and "原文" not in opts[0][2], str(opts[:1]))
    check("其余 21 项跟框架那份一致（code + 中文名 + 顺序）",
          [(c, t.strip()) for c, _a, t in opts[1:]] == fw,
          str([(c, t) for c, _a, t in opts[1:4]]))
    check("没有多余的 option（22 项之外没有别的）",
          tpl.count("<option") == 22, str(tpl.count("<option")))

    # 默认选中英语（中文是「还原原文」，当默认目标没意义）
    sel_opts = re.findall(r'<option value="([^"]+)"([^>]*)>([^<]+)</option>', tpl)
    picked = [c for c, a, _t in sel_opts if "selected" in a]
    check("默认选中的是英语", picked == ["en"], str(picked))

    # 卡片不再从框架那份动态取（用户要求直接抄一份写在模板里）
    check("app.js 不再读 window.Translate.languages",
          "window.Translate" not in js_src)
    check("app.js 里没有残留的 fillTargetLanguages",
          "fillTargetLanguages" not in js_src)

    print("\n" + "=" * 72)
    print(f"通过 {_PASS} 项,失败 {_FAIL} 项")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
