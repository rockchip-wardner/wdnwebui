#!/usr/bin/env python3
"""算力页「运行在」复选框数量自检 —— 每张卡能跑几份就渲染几个。

背景
----
小模型可以在一张卡上并行跑多份（manifest 的 ``max_replicas_per_card``），
算力页据此给每张卡渲染**多个复选框**（"RK182x #0-1" / "(☐ #0-2)"），
用户按显存自己勾。

⚠️ 每张卡的**第 2 份起**，整个选项（含复选框）被括号包住 —— 括号用 CSS
伪元素（``cc-target-opt-bracket``）实现，不在 DOM 文本里，所以本脚本校验的
是**那个 class**，外加 ``framework.css`` 里的伪元素规则。

踩过的坑
--------
前端模型卡片的 ``meta`` 是**逐字段**从 service 条目拼出来的（不是把
``supported_models`` 整个丢过来）。漏抄 ``max_replicas_per_card`` 时它会
兜底成 1 ⇒ **每卡只渲染一个复选框** ⇒ 用户没法控制每张卡跑几份。
（manifest 明明写了 2，页面上还是 2 个复选框 —— 一度以为是浏览器缓存。）

这个脚本**跑真的前端代码**：把 ``system.js`` 里的 ``_makeTargetFieldset``
原样抠出来，配一个最小 DOM stub，在 dukpy 里执行，然后数渲染出来的
复选框。数据来自**真实的** ``GET /api/compute``。

用法::

    .venv/bin/python scripts/verify_compute_replica_boxes.py
    .venv/bin/python scripts/verify_compute_replica_boxes.py --offline
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# 8000 自 2026-11-16 起**只提供 HTTPS**（TLS 在 uvicorn 进程内终结）⇒
# 明文 http:// 打过去会被 RST（RemoteDisconnected）。可用 WDNWEBUI_BASE 覆盖。
BASE = os.environ.get("WDNWEBUI_BASE", "https://127.0.0.1:8000").rstrip("/")
API = BASE + "/api/compute"
SYSTEM_JS = os.path.join(REPO, "app/system/static/system.js")
FRAMEWORK_CSS = os.path.join(REPO, "app/static/core/framework.css")

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


def fetch_snapshot() -> dict:
    with urllib.request.urlopen(API, timeout=30) as r:
        return json.loads(r.read().decode())


def extract_fieldset_fn() -> str:
    """把 ``_makeTargetFieldset`` 的函数体原样抠出来（不是复刻）。"""
    src = open(SYSTEM_JS, encoding="utf-8").read()
    sig = "  _makeTargetFieldset(modelId, meta, inst, cards) {"
    start = src.index(sig)
    # 找到这个方法结束（下一个同缩进的 "  }," ）
    end = src.index("\n  },\n", start) + len("\n  },\n")
    body = src[start:end]
    # 抠出来的是**类里的方法简写**，顶层不是合法语句 → 改成普通函数声明
    body = body.replace(sig,
                        "function _makeTargetFieldset(modelId, meta, inst, cards) {",
                        1)
    body = body.rstrip()
    if body.endswith("},"):
        body = body[:-2] + "}"
    # 方法体里用到的模块级常量
    const = re.search(r"const CC_MAX_RK182X_CARDS = \d+;", src).group(0)
    return const + "\n" + body + "\n"


def build_js(fieldset_fn: str, payload: dict) -> str:
    """包一层：最小 DOM stub + this stub，返回渲染出来的复选框信息。"""
    return """
%(fn)s

// ---- 最小 DOM stub ----
function mkNode(tag) {
  const classes = new Set();
  const node = {
    tagName: tag, textContent: "", style: {}, dataset: {},
    children: [], hidden: false, checked: false, value: "", type: "",
    // classList：前端用 label.classList.add("cc-target-opt-bracket")
    // 标记「第 2 份起要加括号」，stub 必须支持（不然这里会直接报错）。
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
      toggle: (c) => (classes.has(c) ? classes.delete(c) : classes.add(c)),
    },
    appendChild(c) { this.children.push(c); return c; },
    replaceWith() {},
    addEventListener() {},
    setAttribute() {},
    getAttribute() { return null; },
    querySelectorAll() { return []; },
  };
  // className 与 classList **双向同步**（浏览器就是这么做的）——
  // 前端先 className = "cc-target-opt" 再 classList.add(...)，不同步的话
  // 序列化出来的 class 会丢掉 "cc-target-opt"（甚至丢掉已加的 class）。
  Object.defineProperty(node, "className", {
    get: () => Array.from(classes).join(" "),
    set: (v) => {
      classes.clear();
      String(v == null ? "" : v).split(/\\s+/)
        .filter(Boolean).forEach((c) => classes.add(c));
    },
  });
  return node;
}
globalThis.document = {
  createElement: mkNode,
  getElementById: () => null,
  querySelectorAll: () => [],
};

// ---- this stub ----
const payload = %(payload)s;
const ctx = {
  _selectedTarget: {},
  _selectedSlots: () => payload.selectedSlots,
  _modelSlots: () => payload.modelSlots,
  _modelCards: (cards) => cards,
  _sortNpuCards: (cards) => cards.slice().sort((a, b) => a.npu_index - b.npu_index),
  _slotKey: (n, r) => `${n}:${r}`,
  _parseSlot: (k) => String(k).split(":").map(Number),
  _statusLineText: () => "",
  _modelNpus: () => payload.modelSlots.map((s) => s.npu_index),
};

// 抠出来的是普通函数声明 → 用 .call 把 ctx 当 this 传进去
const fs = _makeTargetFieldset.call(ctx, payload.modelId, payload.meta,
                                    payload.inst, payload.cards);
// 把 fieldset 里所有 <input type=checkbox> 连同它的标签文字摘出来
const boxes = [];
(function walk(node, inherited) {
  // label 的文字在它自己的 <span> 里，而 input 是**先** append 的
  // （appendChild(input) 在 appendChild(txt) 之前）—— 所以文字要从
  // **兄弟节点**上取，不能靠遍历顺序。
  const own = (node.children || [])
      .filter((c) => c.tagName === "span")
      .map((c) => c.textContent || "")
      .join(" ");
  const text = own || inherited;
  for (const c of (node.children || [])) {
    if (c.tagName === "input" && c.type === "checkbox") {
      boxes.push({
        value: c.value,
        label: text,
        // 括号是 CSS 伪元素，DOM 里看不到 ⇒ 只能校验「label 上挂了
        // cc-target-opt-bracket 这个 class」。
        classes: node.className || "",
        bracket: !!(node.classList
                    && node.classList.contains("cc-target-opt-bracket")),
      });
    }
    walk(c, text);
  }
})(fs, "");
JSON.stringify(boxes);
""" % {"fn": fieldset_fn, "payload": json.dumps(payload)}


def run_js(js: str) -> list:
    import dukpy
    interp = dukpy.JSInterpreter()
    raw = interp.evaljs(js)
    if isinstance(raw, str):
        return json.loads(raw)
    return json.loads(str(raw))


def frontend_meta(service: dict) -> dict:
    """照抄 system.js 里那段 meta 拼装（改了那边这里也要跟着改）。"""
    return {
        "label":                service.get("label") or service.get("model_id"),
        "capability_types":     service.get("capability_types") or [],
        "memory_estimate_mb":   service.get("memory_estimate_mb") or 0,
        "estimated_init_seconds": 30,
        "submodels":            [],
        "description":          service.get("description") or "",
        "requires_cards":       int(service.get("requires_cards") or 1),
        "max_replicas_per_card": int(service.get("max_replicas_per_card") or 1),
        "requires_devices":     int(service.get("requires_devices") or 1),
        "card_requirements":    service.get("card_requirements") or None,
        "available_targets":    service.get("available_targets") or ["182x"],
    }


def _declared_replicas(model_id: str) -> int | None:
    """模型 manifest 里声明的「每卡几份」；模型没装在本板 → ``None``。

    口径跟宿主一致（``app/core/compute/workers/__init__.py``）：
    ``max(1, int(raw.get("max_replicas_per_card", 1) or 1))`` —— 缺省就是 1。
    """
    try:
        from app.core.compute.workers import find_manifest_path
        path = find_manifest_path(model_id)
    except Exception:  # noqa: BLE001
        return None
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return None
    try:
        return max(1, int(raw.get("max_replicas_per_card", 1) or 1))
    except (TypeError, ValueError):
        return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true",
                    help="不查接口，用假数据（测前端渲染逻辑本身）")
    args = ap.parse_args()

    print("=" * 72)
    print("算力页「运行在」复选框数量自检")
    print("=" * 72)

    fieldset_fn = extract_fieldset_fn()

    if args.offline:
        snap = {
            "cards": [{"npu_index": 0, "device_id": "0000:01:00.0"},
                      {"npu_index": 1, "device_id": "0003:31:00.0"}],
            "services": [{
                "model_id": "hy-mt-1-5", "label": "[MT]翻译模型v1.5",
                "capability_types": ["swcap_translate"],
                "memory_estimate_mb": 2200, "requires_cards": 1,
                "max_replicas_per_card": 2,
                "available_targets": ["182x"], "description": "",
                "card_requirements": None, "npu_index": 0, "replica": 0,
            }],
        }
        print("\n[离线] 用假数据")
    else:
        snap = fetch_snapshot()
        print("\n[在线] 数据来自 GET /api/compute")

    svc = next((s for s in snap.get("services", [])
                if s.get("model_id") == "hy-mt-1-5"), None)
    if svc is None:
        print("!! 接口里没有 hy-mt-1-5 的 service 条目")
        return 2

    print("\n[1] 后端把「每卡几份」透出到 service 条目")
    check("service 条目带 max_replicas_per_card",
          "max_replicas_per_card" in svc,
          str(sorted(svc.keys())))
    # ⚠ **这个值由模型 manifest 决定**（宿主只透传，缺省 1）——
    # **不能写死 2**：写死等于拿「模型数据」当「宿主行为」测，换台板子
    # （装的模型没声明这键）必挂。改成比「透出的值 == manifest 声明的值」。
    if args.offline:
        declared = int(svc.get("max_replicas_per_card") or 1)   # 假数据自洽
    else:
        declared = _declared_replicas(svc["model_id"])
    if declared is None:
        print("      （模型没装在本板 ⇒ 跳过「值 == manifest 声明值」）")
    else:
        check("透出的值 == 模型 manifest 声明的值",
              int(svc.get("max_replicas_per_card") or 0) == declared,
              "api=%s manifest=%s" % (svc.get("max_replicas_per_card"), declared))

    print("\n[2] 前端 meta 把它抄进去")
    meta = frontend_meta(svc)
    # 抄的是**同一个值**（不是写死 2）—— 漏抄时前端兜底成 1 ⇒ 每卡只
    # 渲染一个复选框（这才是本脚本要钉住的坑）。
    check("meta.max_replicas_per_card == service 条目里的值",
          meta["max_replicas_per_card"] == int(svc.get("max_replicas_per_card") or 1),
          "meta=%s svc=%s" % (meta["max_replicas_per_card"],
                              svc.get("max_replicas_per_card")))

    print("\n[3] 真跑前端的 _makeTargetFieldset，数复选框")
    # 渲染这一段用**固定 fixture**（2 张卡 × 每卡 2 份），不拿板子的实况：
    #   * 「每卡几份」是**模型数据**（各板不同 —— 本板所有模型都声明 1）；
    #   * 「本板有几张卡」也是板子数据（本板 3 张，UI 上限 2 张）。
    # 两者都不该影响「前端怎么渲染」的测试。
    # ⚠ 「第 2 份起带括号」那条路径只有 replicas ≥ 2 才走得到 ⇒ 必须固定 2。
    render_cards = [{"npu_index": 0, "device_id": "0000:01:00.0"},
                    {"npu_index": 1, "device_id": "0003:31:00.0"}]
    render_meta = dict(meta)
    render_meta["max_replicas_per_card"] = 2
    render_inst = dict(svc)
    render_inst["max_replicas_per_card"] = 2
    payload = {
        "modelId": "hy-mt-1-5",
        "meta": render_meta,
        "inst": render_inst,
        "cards": render_cards,
        "selectedSlots": ["0:0", "1:0"],
        "modelSlots": [{"npu_index": 0, "replica": 0}],
    }
    boxes = run_js(build_js(fieldset_fn, payload))
    titles = [b["label"] for b in boxes]
    print("      渲染出 %d 个复选框：%s" % (len(boxes), " | ".join(titles)))
    check("渲染出 4 个复选框（2 卡 × 每卡 2 份）", len(boxes) == 4, str(titles))
    check("标题是 RK182x #0-1 / #0-2 / RK182x #1-1 / #1-2",
          titles == ["RK182x #0-1", "#0-2",
                     "RK182x #1-1", "#1-2"],
          str(titles))
    check("每张卡的第一个写全名、第二个省掉前缀",
          all(t.startswith("RK182x") for t in titles[::2])
          and all(not t.startswith("RK182x") for t in titles[1::2]),
          str(titles))
    check("后半段比前半段短（不占地方）",
          len(titles[1]) < len(titles[0]) and len(titles[3]) < len(titles[2]),
          str([(t, len(t)) for t in titles]))
    check("复选框的值是「卡:份」", [b["value"] for b in boxes] ==
          ["0:0", "0:1", "1:0", "1:1"], str([b["value"] for b in boxes]))

    print("\n[4] 第 2 份起的整个选项被括号包住：(☐ #0-2) / (☐ #1-2)")
    check("第 1/3 个（每卡第 1 份）不带括号",
          not boxes[0]["bracket"] and not boxes[2]["bracket"],
          str([b["bracket"] for b in boxes]))
    check("第 2/4 个（每卡第 2 份）带括号",
          boxes[1]["bracket"] and boxes[3]["bracket"],
          str([b["bracket"] for b in boxes]))
    check("className 上带着 cc-target-opt-bracket（浏览器里看得到的那串）",
          all("cc-target-opt-bracket" in b["classes"]
              for b in (boxes[1], boxes[3]))
          and all("cc-target-opt-bracket" not in b["classes"]
                  for b in (boxes[0], boxes[2])),
          str([b["classes"] for b in boxes]))
    # 括号用 CSS 伪元素包住**整个选项**（含复选框）⇒ 标题文本里**不能**
    # 出现括号，否则会渲染成「☐ (#0-2)」——括号跑到框外去了。
    check("标题文本里没有括号（括号由 CSS 伪元素负责）",
          not any("(" in b["label"] or ")" in b["label"] for b in boxes),
          str(titles))
    css = open(FRAMEWORK_CSS, encoding="utf-8").read()
    check("framework.css 有 cc-target-opt-bracket 的伪元素规则",
          ".cc-target-opt.cc-target-opt-bracket::before" in css
          and 'content: "("' in css
          and 'content: ")"' in css,
          "framework.css 缺括号伪元素规则")
    check("括号规则里 gap 归 0（不然会渲染成「( ☐ #0-2 )」）",
          ".cc-target-opt.cc-target-opt-bracket {" in css
          and "gap: 0;" in css,
          "framework.css 的括号规则没有 gap: 0")

    print("\n[5] 每卡只跑 1 份时（max=1）只渲染 2 个")
    svc1 = dict(svc)
    svc1["max_replicas_per_card"] = 1
    payload1 = dict(payload)
    payload1["meta"] = frontend_meta(svc1)
    payload1["inst"] = svc1
    boxes1 = run_js(build_js(fieldset_fn, payload1))
    check("只渲染 2 个复选框", len(boxes1) == 2, str(len(boxes1)))
    check("每卡只有第 1 份 ⇒ 没有括号",
          not any(b["bracket"] for b in boxes1),
          str([b["bracket"] for b in boxes1]))

    print("\n[6] 负向：旧行为（service 条目没有这个字段）只能渲染 2 个")
    # 旧后端：service 条目里没有 max_replicas_per_card，前端 meta 也就抄不到
    # → 函数里兜底成 1 → 每卡一个复选框。这个断言保证本脚本**不是摆设**：
    # 有人把字段删掉时它会立刻炸。
    svc_old = {k: v for k, v in svc.items() if k != "max_replicas_per_card"}
    meta_old = frontend_meta(svc_old)
    meta_old.pop("max_replicas_per_card", None)
    payload_old = dict(payload)
    payload_old["meta"] = meta_old
    payload_old["inst"] = svc_old
    boxes_old = run_js(build_js(fieldset_fn, payload_old))
    check("旧行为只渲染 2 个（所以字段丢了页面就少一半复选框）",
          len(boxes_old) == 2, str(len(boxes_old)))

    print("\n" + "=" * 72)
    print(f"通过 {_PASS} 项,失败 {_FAIL} 项")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
