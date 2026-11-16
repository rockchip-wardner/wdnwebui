#!/usr/bin/env python
"""网页翻译**进度条**回归自检（2026-11-16）。

问题
----
用户报「网页翻译进度条会不停回退」。根因不是进度条本身画错,而是**增量
翻译也在画全局进度条**:

    页面持续重建 DOM（卡片 3s 轮询、算力页 1s 轮询）
      → 每次都产生一批「全新」文本节点
      → MutationObserver 触发增量翻译
      → translateTo(target, onlyNew=true) 调了 setProgress(0, N)
      → 全局进度条被打回 0% → 一轮轮「重新开始」

实测:浏览器每 24~65s 一轮增量翻译,每轮十几个请求(服务端命中缓存、秒回),
于是进度条 0% → 100% → 0% → 100% 地来回跳。

修法
----
1. **增量翻译不碰全局进度条** —— 进度条是给「整页翻译」这个用户主动发起
   的操作用的。
2. **setProgress 单调** —— 进度条只增不减(硬保证)。
3. **客户端译文缓存** —— 页面重建出的节点内容跟之前翻过的一模一样,直接
   套用缓存里的译文,不发请求 ⇒ 增量翻译几乎零成本。

怎么测
------
机器上没有浏览器,但有 ``dukpy``(QuickJS)。这里用一份**最小 DOM stub**
把**真实的** ``app/static/core/translate.js`` 跑起来,复现「页面重建 DOM
→ 增量翻译」这个场景,断言:

* 增量翻译跑完之后,进度条**没有**被点亮、进度值**没有**变;
* 增量翻译**没有**发出任何网络请求(本地缓存命中);
* 整页翻译过程中,进度条的宽度**只增不减**。

用法::

    .venv/bin/python scripts/test_translate_progress.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
#: 可以用环境变量指向别的 translate.js —— 负向验证时用（把增量路径改回旧
#: 行为，测试必须**失败**，否则说明这套断言抓不住 bug）。
SRC = Path(os.environ.get("WDN_TRANSLATE_JS")
           or (REPO / "app" / "static" / "core" / "translate.js"))

# --------------------------------------------------------------------------- #
# 最小 DOM stub —— 只实现 translate.js 真正用到的那部分
# --------------------------------------------------------------------------- #
STUB_JS = r"""
// ---- 元素 ----
function makeEl(tag) {
  const el = {
    tagName: String(tag || "div").toUpperCase(),
    className: "",
    textContent: "",
    innerHTML: "",
    hidden: false,
    title: "",
    type: "",
    dataset: {},
    children: [],
    parentElement: null,
    _attrs: {},
    _handlers: {},
    classList: {
      toggle() {}, add() {}, remove() {}, contains() { return false; },
    },
    setAttribute(k, v) { this._attrs[k] = v; if (k === "data-code") this.dataset.code = v; },
    getAttribute(k) { return this._attrs[k]; },
    appendChild(c) { this.children.push(c); c.parentElement = this; return c; },
    append() { for (const c of arguments) this.appendChild(c); },
    addEventListener(t, f) { this._handlers[t] = f; },
    contains(n) {
      if (n === this) return true;
      return this.children.some((c) => c.contains && c.contains(n));
    },
    closest() { return null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
  };
  // style.width 每次被写都记一笔 —— 用来断言「进度条只增不减」。
  // 连同**写的时候进度条是不是可见**一起记:``clearProgress()`` 会先把条
  // 藏起来再把宽度复位成 0% —— 那是「整轮结束」,不是「进度条往回走」,
  // 断言时要区分开。
  el._widths = [];
  el.style = {
    set width(v) {
      el._widths.push(String(v));
      if (globalThis.__widths) {
        globalThis.__widths.push({
          w: String(v),
          hidden: !!(el.parentElement && el.parentElement.hidden),
        });
      }
    },
    get width() { return el._widths.length ? el._widths[el._widths.length - 1] : "0%"; },
  };
  return el;
}

function makeText(text) {
  return {
    tagName: "#text",
    nodeValue: text,
    parentElement: null,
    children: [],
    contains() { return false; },
  };
}

function textNodes(root) {
  const out = [];
  (function walk(node) {
    for (const c of node.children || []) {
      if (c.tagName === "#text") out.push(c);
      else walk(c);
    }
  })(root);
  return out;
}

// ---- 全局 ----
const body = makeEl("body");
globalThis.window = globalThis;
globalThis.NodeFilter = { SHOW_TEXT: 4 };
globalThis.document = {
  readyState: "complete",         // 让 boot() 立刻跑（buildUI / startObserver）
  visibilityState: "visible",
  body,
  createElement: makeEl,
  createTextNode: makeText,
  querySelector() { return null; },
  querySelectorAll() { return []; },
  addEventListener() {},
  createTreeWalker(root) {
    const nodes = textNodes(root);
    let i = 0;
    return { nextNode() { return i < nodes.length ? nodes[i++] : null; } };
  },
};
globalThis.localStorage = {
  _d: {},
  getItem(k) { return this._d[k] === undefined ? null : this._d[k]; },
  setItem(k, v) { this._d[k] = String(v); },
  removeItem(k) { delete this._d[k]; },
};

// ---- 网络 ----
globalThis.__requests = [];
globalThis.fetch = function (url, opts) {
  const body = opts && opts.body ? JSON.parse(opts.body) : {};
  const u = String(url);
  // 只数**翻译**请求 —— boot() 还会拉一次 /api/translate/status
  if (u.indexOf("/api/translate") >= 0 && u.indexOf("/status") < 0) {
    globalThis.__requests.push({ url: u, segments: body.segments || [] });
  }
  let out = { translations: (body.segments || []).map((s) => "T[" + s + "]"), failed: 0 };
  if (u.indexOf("/status") >= 0) {
    out = { source: "zh-CN", provider: "npu", providers: ["npu"], npu_active: true };
  }
  return Promise.resolve({ ok: true, json: () => Promise.resolve(out) });
};

// ---- MutationObserver ----
globalThis.__observerCb = null;
globalThis.MutationObserver = function (cb) {
  globalThis.__observerCb = cb;
  this.observe = function () {};
  this.disconnect = function () {};
};

// ---- 定时器:存起来,由测试显式跑 ----
globalThis.__timers = [];
globalThis.setTimeout = function (fn, ms) { globalThis.__timers.push(fn); return globalThis.__timers.length; };
globalThis.setInterval = function () { return 0; };
globalThis.clearTimeout = function () {};
globalThis.__runTimers = function () {
  const t = globalThis.__timers.splice(0, globalThis.__timers.length);
  t.forEach((fn) => fn());
};
globalThis.console = { log() {}, warn() {}, error() {} };
"""


def main() -> int:
    try:
        from dukpy import JSInterpreter
    except ImportError:
        print("⚠️  没装 dukpy,跳过（pip install dukpy 后可跑）")
        return 0

    src = SRC.read_text(encoding="utf-8")
    harness = STUB_JS + "\n" + src + "\n" + r"""
// ===================== 测试 =====================
const results = [];
function check(name, cond, extra) {
  results.push({ name: name, ok: !!cond, extra: extra === undefined ? "" : String(extra) });
}
// 等条件成立（只用微任务 —— setTimeout 在这个 stub 里是被存起来的）
async function until(cond, max) {
  for (let i = 0; i < (max || 500); i++) {
    if (cond()) return true;
    await Promise.resolve();
  }
  return false;
}

// ---- 布景:页面里有一个中文文本节点 ----
const page = makeEl("div");
page.className = "page";
const node1 = makeText("系统运行正常");
page.appendChild(node1);
body.appendChild(page);

(async function () {
  await until(() => globalThis.__observerCb);   // 等 boot() 走到 startObserver()

  // ============ 1. 整页翻译:进度条 0 → 100,且只增不减 ============
  await window.Translate.setLanguage("en");
  await until(() => true);
  const p1 = window.Translate.progress;
  check("整页翻译后进度条已复位(隐藏)", p1.visible === false, JSON.stringify(p1));
  check("整页翻译发了 1 次请求", globalThis.__requests.length === 1,
        "requests=" + globalThis.__requests.length);
  check("译文写进节点了", node1.nodeValue === "T[系统运行正常]", node1.nodeValue);
  check("节点记住原文", node1.__wdnOrig === "系统运行正常", node1.__wdnOrig);

  // ============ 2. 页面重建 DOM → 增量翻译 ============
  // 模拟「卡片 3s 轮询把内容重写一遍」:同样的文案,换成**新**的文本节点。
  const before = globalThis.__requests.length;
  const node2 = makeText("系统运行正常");
  page.children.splice(0, page.children.length);
  page.appendChild(node2);
  // 触发 observer（childList 有新节点 ⇒ dirty）
  globalThis.__observerCb([{ type: "childList", addedNodes: [node2] }]);
  globalThis.__runTimers();
  await until(() => globalThis.__timers.length === 0);

  const p2 = window.Translate.progress;
  check("增量翻译没有发请求(本地缓存命中)",
        globalThis.__requests.length === before,
        "before=" + before + " after=" + globalThis.__requests.length);
  check("增量翻译没有点亮进度条", p2.visible === false, JSON.stringify(p2));
  check("增量翻译没有改进度值", p2.shown === 0, "shown=" + p2.shown);
  check("新节点被套上了缓存里的译文",
        node2.nodeValue === "T[系统运行正常]", node2.nodeValue);

  // ============ 3. 进度条宽度只增不减 ============
  // 只看**可见期间**的宽度 —— clearProgress() 是「整轮结束」：它先把条藏
  // 起来再把宽度复位成 0%，那不是「进度条往回走」。
  const recs = globalThis.__widths || [];
  const vis = recs.filter((r) => !r.hidden).map((r) => parseFloat(r.w) || 0);
  let mono = true;
  for (let i = 1; i < vis.length; i++) {
    if (vis[i] < vis[i - 1]) { mono = false; break; }
  }
  check("可见期间宽度单调不减", mono, "visible=" + JSON.stringify(vis));
  check("可见期间确实画过进度", vis.length >= 2 && vis[vis.length - 1] === 100,
        "visible=" + JSON.stringify(vis));

  // ---- 输出 ----
  let pass = 0, fail = 0;
  for (const r of results) {
    if (r.ok) { pass++; globalThis.__out.push("  ok   " + r.name); }
    else { fail++; globalThis.__out.push("  FAIL " + r.name + " " + r.extra); }
  }
  globalThis.__out.push("");
  globalThis.__out.push("  宽度记录: " + JSON.stringify(recs));
  globalThis.__out.push("  翻译请求数: " + globalThis.__requests.length);
  globalThis.__out.push("__DONE__");
})();
"""
    # 把 width 序列导出来
    harness = harness.replace(
        "globalThis.__timers = [];",
        "globalThis.__timers = [];\nglobalThis.__out = [];\n"
        "globalThis.__widths = [];",
    )
    # 在 el.style 的 setter 里同步到全局数组
    harness = harness.replace(
        "set width(v) { el._widths.push(String(v)); },",
        "set width(v) { el._widths.push(String(v)); "
        "if (globalThis.__widths) globalThis.__widths.push(String(v)); },",
    )

    # ⚠️ dukpy 的模块级 ``evaljs()`` **每次都新建解释器**（上下文不共享）——
    # 必须自己持有一个 JSInterpreter，否则后一次 evaljs 看不到前一次的状态。
    js = JSInterpreter()
    js.evaljs(harness)
    # 等异步体跑完（__DONE__ 出现为止）
    import time
    out = ""
    for _ in range(100):
        time.sleep(0.05)
        out = js.evaljs("globalThis.__out.join('\\n')")
        if out and "__DONE__" in out:
            break
    out = (out or "").replace("__DONE__", "")
    print(out or "(无输出)")
    # ⚠️ 用 ``indexOf('FAIL') >= 0`` 而不是 ``=== 0`` —— 输出行是
    # ``"  FAIL xxx"``（前面有缩进），``=== 0`` 永远不成立 ⇒ 计数恒为 0
    # ⇒ 这个脚本会**永远**报「全部通过」。踩过。
    fail = js.evaljs(
        "globalThis.__out.filter(function(l){"
        "return l.indexOf('FAIL') >= 0;}).length")
    total = js.evaljs(
        "globalThis.__out.filter(function(l){"
        "return l.indexOf('  ok   ') === 0;}).length")
    print("\n通过 %d 项，失败 %d 项 —— %s"
          % (total, fail, "✅ 全部通过" if not fail else "❌ 有失败"))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
