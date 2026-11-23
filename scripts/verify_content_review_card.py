#!/usr/bin/env python3
"""图文内容审查卡自检 —— 决策任务构造得对不对，概率能不能过 DDS。

背景
----
「图文内容审查」卡把用户维护的**可增删列表**翻译成 Intern-Decision 的
决策任务（整张列表 = **一个** ``choice`` 字段 = 列表项当选项），再把模型
返回的**概率**画成进度条。

这条链路上有三个**不报错但会静默出错**的坑，本脚本逐个钉住：

1. **决策任务形状**：整张列表必须落在**一个** ``choice`` 字段里（选项键 =
   列表项名称），且选项数不能超过候选符号数（62）—— 否则
   ``wdnai.decision.parse_decision_task`` 直接抛错。本脚本用**官方解析器**
   校验我们构造的任务，而不是自己断言字段名。
   ⚠️ 2026-11-18 起任务从「每项一个 ``noul`` 字段」改成「**一个**
   ``choice`` 字段、列表项当选项」：RKNN 上是 prefill-only 模型，N 个字段
   = N 次前向且总 prefill ∝ N²（16 项要 26 秒），压成一个字段后 1~16 项
   都约 1 秒。
2. **概率过 DDS**：决策模型的完整概率 / logits 在 ``TextAttachment`` 里，
   而 ``wdndds`` 的 ``encode_attachments_out`` 曾经**只认图片和音频** ⇒
   文本附件被静默丢掉 ⇒ 调用方只拿到答案 JSON，概率全是 0（判定对、
   概率空）。本脚本做一次真实的 encode/decode 往返。
3. **列表语义**：空项要丢、重名要合并、超过 16 项要拦（模型硬上限）。

用法::

    .venv/bin/python scripts/verify_content_review_card.py
"""
from __future__ import annotations

import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# 卡片源码目录问解析器，别硬编码 ``.venv/src/<checkout 名>/…`` ——
# checkout 目录名跟仓库名不一定一致（本板是 ``wdnapps-audio-chat``，
# 仓库名是 ``wdnapps_audio_chat``），写死就 FileNotFoundError。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from card_dirs import card_dir as _card_dir  # noqa: E402

CARD_PKG = str(_card_dir("content-review"))
WIZARD_JSON = os.path.join(CARD_PKG, "static/wizard.json")
MANIFEST = os.path.join(CARD_PKG, "manifest.json")
TEMPLATE = os.path.join(CARD_PKG, "static/template.html")
CARD_JS = os.path.join(CARD_PKG, "static/app.js")

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
# DOM stub —— 把卡片 app.js 原样跑一遍（抠真前端代码，不复刻逻辑）
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
    addEventListener(type, fn) {
      (this._h || (this._h = {}))[type] = (this._h[type] || []).concat([fn]);
    },
    removeEventListener() {},
    // ⚠️ **派发 click** —— 自检要**模拟用户点「开始审查」**，而不是直接调
    // 内部函数：只有走真实的监听器，才能钉住「事件 → 徽章」这条链路。
    // 以前 stub 里的 addEventListener 是空的 ⇒ 点按钮什么都不会发生。
    click() {
      const hs = (this._h || {}).click || [];
      hs.forEach((fn) => { try { fn({ target: this }); } catch (_) {} });
    },
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
  return node;
}
const REG = {};
globalThis.document = {
  createElement: mkNode,
  getElementById: (id) => (REG[id] || (REG[id] = mkNode("div"))),
  body: mkNode("body"),
  visibilityState: "visible",
  execCommand: () => true,
  querySelectorAll: () => [],
};
globalThis.setTimeout = (fn) => { try { fn(); } catch (_) {} return 0; };
globalThis.setInterval = () => 0;
globalThis.clearTimeout = () => {};
globalThis.clearInterval = () => {};
globalThis.navigator = { clipboard: null, userAgent: "dukpy" };
globalThis.window = {
  location: { origin: "http://dukpy", href: "http://dukpy/" },
  isSecureContext: false,
  addEventListener() {},
};
globalThis.localStorage = {
  _data: {},
  getItem(k) { return this._data[k] === undefined ? null : this._data[k]; },
  setItem(k, v) { this._data[k] = String(v); },
  removeItem(k) { delete this._data[k]; },
};
globalThis.fetch = async () => ({
  ok: true, status: 200,
  json: async () => ({
    enabled: true, model_id: "intern-decision-4b", npu_index: 0,
    default_npu_index: 0, instance_status: "running", instance_error: "",
    installed: true, capability: "swcap_decision", max_items: 16,
    max_text_chars: 4000, detail: "", warning: "", cards: [],
    card_memory_mb: 5120,
    // ⚠️ **模型候选列表**（2026-11-19）—— 标签 / 显存估算的真源是 manifest。
    models: [
      { id: "intern-decision-4b", label: "[DM]图像文字决策模型4B",
        installed: true, memory_mb: 3200, running: true,
        instance_status: "running", npu_index: 0 },
      { id: "intern-decision-0.8b", label: "[JEV]图像文字决策模型0.8B",
        installed: true, memory_mb: 900, running: false,
        instance_status: "not_loaded", npu_index: null },
    ],
  }),
});
1;
"""

#: 取出渲染出来的维度名 / 提示语 / 回写后的 localStorage
PROBE = r"""
JSON.stringify({
  names: ((REG["cr-items"] || {}).children || []).map(
    (r) => (((r.children || [])[0] || {}).value) || ""
  ),
  notice: ((REG["cr-run-note"] || {}).textContent) || "",
  store: globalThis.localStorage.getItem("wdnwebui.content_review.items"),
});
"""


def run_card_app_js(seed=None) -> dict:
    """把卡片 app.js 原样跑一遍（DOM stub + 假 localStorage）。

    ``seed`` = localStorage 里**预先**放好的那条记录：
      - ``None``  → 模拟「从没用过这张卡的浏览器」
      - ``list``  → 直接给那条记录的 Python 形态（内部会 JSON 化）
      - ``str``   → 已经是 JSON 文本，原样塞进去

    ⚠️ localStorage 里存的必须是**字符串**（浏览器就是这么存的）——
    app.js 拿到后要 ``JSON.parse``，塞数组会 parse 失败然后走「没存过」
    的分支，看起来就像迁移没生效。
    """
    import dukpy
    src = open(CARD_JS, encoding="utf-8").read()
    stub = DOM_STUB
    if seed is not None:
        text = seed if isinstance(seed, str) else json.dumps(seed, ensure_ascii=False)
        stub += (
            'globalThis.localStorage._data["wdnwebui.content_review.items"] = '
            + json.dumps(text) + ";\n"     # JSON 字符串字面量 = JS 字符串字面量
        )
    stub += "1;\n"
    interp = dukpy.JSInterpreter()
    interp.evaljs(stub)
    interp.evaljs(src)
    raw = interp.evaljs(PROBE)
    return json.loads(raw if isinstance(raw, str) else str(raw))


#: 探模型下拉：选项（值 / 文案 / 禁用 / 选中）+ 提示语 + 记住的 model id
PROBE_MODEL = r"""
JSON.stringify({
  options: (((REG["cr-model-select"] || {}).children) || []).map(
    (o) => ({
      value: o.value || "",
      text: o.textContent || "",
      disabled: !!o.disabled,
      selected: !!o.selected,
    })
  ),
  hint: ((REG["cr-model-hint"] || {}).textContent) || "",
  selectDisabled: !!((REG["cr-model-select"] || {}).disabled),
  store: globalThis.localStorage.getItem("wdnwebui.content_review.model"),
});
"""


def run_model_select() -> dict:
    """把卡片 app.js 跑一遍，探模型下拉渲染出来的选项。

    顺带模拟**用户切换模型**（派发 change 事件）⇒ 断言选择被记住了
    （localStorage）—— 下拉只是画出来不算数，得真的切得动。

    ⚠️ 下拉是 ``refresh()``（**异步**）之后才渲染的 ⇒ 要轮询等它落定，
    不能 evaljs 完就探。
    """
    import time

    import dukpy

    src = open(CARD_JS, encoding="utf-8").read()
    interp = dukpy.JSInterpreter()
    interp.evaljs(DOM_STUB)
    interp.evaljs(src)
    before = {}
    for _ in range(100):
        before = json.loads(interp.evaljs(PROBE_MODEL))
        if before.get("options"):
            break
        time.sleep(0.02)
    # 模拟用户在下拉里选了 0.8B（走**真实的** change 监听器）
    interp.evaljs(
        'var _sel = REG["cr-model-select"];'
        '(_sel._h && _sel._h.change || []).forEach(function (fn) {'
        '  fn({ target: { value: "intern-decision-0.8b" } });'
        '});'
    )
    after = json.loads(interp.evaljs(PROBE_MODEL))
    return {"before": before, "after": after}


def run_review_click() -> dict:
    """模拟用户点「开始审查」，返回徽章 / 提示语的最终状态。

    ⚠️ **必须走真实的 click 监听器**（不是直接调内部函数）—— 只有这样才能
    钉住「点按钮 → 徽章变」这条链路；直接调内部函数的话，徽章没接线也能
    「测过」（2026-11-18 那个「结果卡永远是未审查」的 bug 就是这么漏掉的）。

    ``run()`` 是 async 而监听器不 await 它 ⇒ 轮询提示语等它落定。
    """
    import time

    import dukpy

    src = open(CARD_JS, encoding="utf-8").read()
    interp = dukpy.JSInterpreter()
    interp.evaljs(DOM_STUB)
    interp.evaljs(src)
    interp.evaljs(
        'document.getElementById("cr-text").value = "限时优惠，点击领取优惠券";'
        'document.getElementById("cr-run-btn").click();'
    )
    note = ""
    for _ in range(100):
        note = interp.evaljs('((REG["cr-run-note"] || {}).textContent) || ""')
        if note in ("完成", "失败"):
            break
        time.sleep(0.02)
    return {
        "note": note,
        "result_badge": interp.evaljs(
            '((REG["cr-result-badge"] || {}).textContent) || ""'),
        "result_state": interp.evaljs(
            '(((REG["cr-result-badge"] || {}).dataset || {}).state) || ""'),
        "review_badge": interp.evaljs(
            '((REG["cr-review-badge"] || {}).textContent) || ""'),
    }


def main() -> int:
    print("=" * 72)
    print("图文内容审查卡自检")
    print("=" * 72)

    from wdnapps_audio_chat.cards.content_review.service import (
        CHOICE_FIELD,
        ContentReviewManager,
        DEFAULT_MODEL,
        MAX_ITEMS,
        MODEL_IDS,
    )

    # ---------------------------------------------------------------- [1] --
    print("\n[1] 卡片声明")
    man = json.load(open(MANIFEST, encoding="utf-8"),)
    check("manifest id = content-review", man.get("id") == "content-review", str(man.get("id")))
    check("标题是「图文内容审查」", man.get("title") == "图文内容审查", str(man.get("title")))
    check("display_order = 72（排在网页翻译 71 后面）",
          man.get("display_order") == 72, str(man.get("display_order")))
    check("用的是决策能力 swcap_decision",
          "swcap_decision" in (man.get("software_capabilities") or []),
          str(man.get("software_capabilities")))
    check("默认模型是 intern-decision-4b（= [DM]图像文字决策模型4B）",
          DEFAULT_MODEL == "intern-decision-4b", DEFAULT_MODEL)
    check("支持两个型号：4B + 0.8B（0.8B 同规格同协议，只是更小更快）",
          MODEL_IDS == ("intern-decision-4b", "intern-decision-0.8b"),
          str(MODEL_IDS))

    tpl = open(TEMPLATE, encoding="utf-8").read()
    check("页面 section id = page-content-review",
          'id="page-content-review"' in tpl)
    check("模板里有模型下拉（cr-model-select）",
          'id="cr-model-select"' in tpl)
    js = open(CARD_JS, encoding="utf-8").read()
    check("前端挂了向导完成钩子 applyWizardSnapshot",
          "ImApp_content_review" in js and "applyWizardSnapshot" in js)
    check("前端启停/审查都带 model_id",
          js.count("model_id") >= 4, str(js.count("model_id")))
    check("前端不再用 st.conflict（改成 warning 告警）",
          "st.conflict" not in js and "st.warning" in js)

    wiz = json.load(open(WIZARD_JSON, encoding="utf-8"))
    m = (wiz.get("swcap_to_model") or {}).get("swcap_decision") or {}
    check("向导首选仍是 intern-decision-4b",
          m.get("model_id") == "intern-decision-4b", str(m.get("model_id")))
    check("向导候选里也列了 0.8B（首选没装时能退过去）",
          "intern-decision-0.8b" in (m.get("models") or []), str(m.get("models")))

    # ---------------------------------------------------------------- [2] --
    print("\n[2] 列表清洗：空项丢 / 重名合并 / 超上限拦下")
    items = ContentReviewManager._normalize_items([
        {"name": "广告", "description": "推销"},
        {"name": "  "},                       # 空 → 丢
        {"name": "广告", "description": "重复"},  # 重名 → 合并
        {"name": "诈骗"},
    ])
    check("空项被丢掉", len(items) == 2, str(items))
    check("重名被合并（保留第一条）",
          [i["name"] for i in items] == ["广告", "诈骗"], str(items))
    check("第一项的说明保留", items[0]["description"] == "推销", str(items[0]))
    try:
        ContentReviewManager._normalize_items(
            [{"name": "x%d" % i} for i in range(MAX_ITEMS + 1)])
        check("超过 %d 项时报错" % MAX_ITEMS, False, "没报错")
    except RuntimeError as exc:
        check("超过 %d 项时报错" % MAX_ITEMS, "上限" in str(exc), str(exc))

    # ---------------------------------------------------------------- [3] --
    print("\n[3] 决策任务形状（用官方解析器校验，不是自己断言）")
    from wdnai.decision import parse_decision_task

    clean = ContentReviewManager._normalize_items([
        {"name": "广告", "description": "推销、引流"},
        {"name": "诈骗", "description": ""},
    ])
    questions, field_map = ContentReviewManager._build_questions(clean)
    check("整张列表压成**一个**决策字段（1 次前向出全部维度）",
          len(questions) == 1, str(list(questions)))
    check("字段名是固定的 choice 字段名",
          list(questions) == [CHOICE_FIELD], str(list(questions)))
    check("字段类型是 choice",
          all(q["type"] == "choice" for q in questions.values()),
          str({k: v["type"] for k, v in questions.items()}))
    criteria = questions[CHOICE_FIELD]["criteria"]
    check("每个列表项 = 一个选项（选项键 = 列表项名称）",
          list(criteria) == ["广告", "诈骗"], str(list(criteria)))
    check("选项说明 = 列表项的说明",
          "推销" in criteria["广告"], criteria.get("广告", ""))
    check("没有说明的项也有可读的兜底说明",
          bool(criteria.get("诈骗")), str(criteria.get("诈骗")))
    check("选项数不超过候选符号数（62）",
          len(criteria) <= 62, str(len(criteria)))
    check("字段映射与列表项一一对应",
          [k for _, k in field_map] == ["广告", "诈骗"], str(field_map))
    try:
        task = parse_decision_task({"questions": questions})
        check("官方 parse_decision_task 接受我们构造的任务", True)
        check("解析出的字段数 = 1", len(task.fields) == 1, str(len(task.fields)))
        check("解析出的选项就是列表项",
              [o.key for o in task.fields[0].options] == ["广告", "诈骗"],
              str([o.key for o in task.fields[0].options]))
    except Exception as exc:  # noqa: BLE001
        check("官方 parse_decision_task 接受我们构造的任务", False, repr(exc))

    # 重名项：选项键必须唯一（JSON key 重复会被静默覆盖）
    dup = ContentReviewManager._normalize_items([
        {"name": "广告"}, {"name": "广告", "description": "x"},
    ])
    q2, fmap2 = ContentReviewManager._build_questions(dup)
    check("同名的两项被合并成一个选项（不会覆盖）",
          len(q2[CHOICE_FIELD]["criteria"]) == 1,
          str(list(q2[CHOICE_FIELD]["criteria"])))
    check("字段映射与任务一一对应", len(fmap2) == 1, str(fmap2))

    # ---------------------------------------------------------------- [4] --
    print("\n[4] 概率明细解析（附件 → 每维占比）")
    detail = {
        "decisions": {
            CHOICE_FIELD: {"type": "choice", "symbol": "A", "label": "广告",
                            "probabilities": {"广告": 0.68, "诈骗": 0.32},
                            "logits": [1.0, 0.5]},
        },
        "temperature": 1.9924182891845703,
    }
    summary = "内容类型[A]=广告 (68.00%)"
    from wdnai.attachment import TextAttachment
    atts = [TextAttachment(summary + "\n" + json.dumps(detail, ensure_ascii=False))]
    got = ContentReviewManager._parse_detail(atts)
    check("能从附件里抠出明细 JSON", bool(got.get("decisions")), str(got)[:120])
    check("概率读得出来",
          abs(got["decisions"][CHOICE_FIELD]["probabilities"]["广告"] - 0.68) < 1e-9,
          str(got.get("decisions", {}).get(CHOICE_FIELD)))
    check("校准温度也在", "temperature" in got, str(got.get("temperature")))
    check("附件为空时返回空 dict（不炸）",
          ContentReviewManager._parse_detail([]) == {},
          str(ContentReviewManager._parse_detail([])))

    # 一个 choice 字段 → 拆回每个维度（不碰 NPU 就能测）
    res, picked, picked_symbol = ContentReviewManager._split_choice(
        got.get("decisions") or {}, field_map)
    check("每个维度一行", len(res) == len(field_map), str(len(res)))
    check("维度顺序与列表一致",
          [r["name"] for r in res] == [i["name"] for i, _ in field_map],
          str([r["name"] for r in res]))
    check("占比来自归一化分布",
          abs(res[0]["probability"] - 0.68) < 1e-9, str(res[0]))
    check("选项符号按顺序 A、B、C…",
          [r["symbol"] for r in res] == ["A", "B"], str([r["symbol"] for r in res]))
    check("选中的那一类被标出来",
          picked == "广告" and res[0]["selected"] and not res[1]["selected"],
          "%s / %s" % (picked, [r["selected"] for r in res]))
    check("模型选的符号带回来了", picked_symbol == "A", picked_symbol)

    # ---------------------------------------------------------------- [5] --
    print("\n[5] 概率能不能过 DDS wire（wdndds 的附件编解码）")
    try:
        from wdndds.ai_session import (
            decode_attachments_out,
            encode_attachments_out,
        )
        enc = encode_attachments_out([TextAttachment(summary + "\n" + json.dumps(detail))])
        check("TextAttachment 被编码进 wire", bool(enc) and "text" in enc[0], str(enc)[:160])
        dec = decode_attachments_out(enc)
        check("解码回来还是 TextAttachment",
              bool(dec) and getattr(dec[0], "text", "").startswith(summary),
              str(dec)[:160])
        parsed = ContentReviewManager._parse_detail(dec)
        check("过完 wire 仍能读出概率",
              abs(parsed.get("decisions", {}).get(CHOICE_FIELD, {})
                  .get("probabilities", {}).get("广告", 0) - 0.68) < 1e-9,
              str(parsed)[:160])
        res2, picked2, _ = ContentReviewManager._split_choice(
            parsed.get("decisions") or {}, field_map)
        check("过完 wire 仍能拆回每个维度",
              len(res2) == len(field_map) and res2[0]["probability"] > 0.67,
              str(res2)[:160])
    except Exception as exc:  # noqa: BLE001
        check("wdndds 附件编解码可用", False, repr(exc))

    # ---------------------------------------------------------------- [6] --
    print("\n[6] 共驻：决策模型不再独占整卡，卡上挤了只告警不拦停")
    import contextlib

    class FakeReg:
        """假的算力注册表 —— 只给这几个函数用到的部分。

        ⚠️ ``model_cards()`` 必须返回 **List[int]**（卡号列表）——
        真实现就是这个形状；写成 dict 列表会掩盖「取下标」那类 bug
        （历史上就因为当成 dict 读而 TypeError，冲突检测静默失效）。
        """

        #: 算力页的模型清单 —— 标签 / 显存估算的真源是模型目录里的 manifest。
        _MODELS = [
            {"id": "intern-decision-4b", "label": "[DM]图像文字决策模型4B",
             "capability_types": ["swcap_decision"], "exists": True,
             "memory_estimate_mb": 3200},
            {"id": "intern-decision-0.8b", "label": "[JEV]图像文字决策模型0.8B",
             "capability_types": ["swcap_decision"], "exists": True,
             "memory_estimate_mb": 900},
        ]

        def __init__(self, instances, cards=(0, 1)):
            self._instances = [dict(i) for i in instances]
            self._cards = list(cards)
            self.stopped = []

        def snapshot(self):
            return {"instances": self._instances}

        def list_cards(self):
            return [{"npu_index": c, "device_id": "x"} for c in self._cards]

        def model_cards(self):
            return list(self._cards)

        def list_supported_models(self):
            return [dict(m) for m in self._MODELS]

        def model_op_lock(self, _mid):
            return contextlib.nullcontext()

        def stop_instance_local(self, model, npu, replica):
            self.stopped.append((model, npu, replica))
            self._instances = [
                i for i in self._instances
                if not (i["model_id"] == model
                        and i["npu_index"] == npu
                        and i["replica"] == replica)
            ]

    def inst(model, card, replica, status="running", mem=2200, source="spawned",
             caps=("x",)):
        return {"model_id": model, "label": model, "npu_index": card,
                "npu_indices": [card], "replica": replica, "status": status,
                "memory_estimate_mb": mem, "capability_types": list(caps),
                "_source": source}

    M4B = "intern-decision-4b"
    M08B = "intern-decision-0.8b"

    # ---- 不再清场：启用决策模型不会停掉卡上的其它模型 ----
    check("启用不再自动清场（_clear_card 已移除）",
          not hasattr(ContentReviewManager, "_clear_card"))

    # ---- 余量够 ⇒ 没有告警（0.8B 900 + 翻译 2200 = 3100 < 5120） ----
    reg = FakeReg([inst("hy-mt-1-5", 0, 0, mem=2200)])
    warn = ContentReviewManager._memory_warning(reg, 0, M08B)
    check("0.8B 与翻译同卡放得下（2200+900 < 5120）⇒ 不告警",
          warn == "", warn)
    check("清场没发生：卡上的翻译实例没被动",
          reg.stopped == [], str(reg.stopped))

    # ---- 余量不够 ⇒ 有告警（4B 3200 + 翻译 2 份 4400 > 5120） ----
    reg2 = FakeReg([inst("hy-mt-1-5", 0, 0, mem=2200),
                     inst("hy-mt-1-5", 0, 1, mem=2200)])
    warn2 = ContentReviewManager._memory_warning(reg2, 0, M4B)
    check("4B 与两份额译同卡放不下 ⇒ 有告警", bool(warn2), warn2)
    check("告警里说清了余量与上限",
          "5120" in warn2 and "3200" in warn2, warn2)
    check("告警不是拦截（enable 不会因此拒绝）",
          not isinstance(warn2, RuntimeError), type(warn2).__name__)

    # ---- 本模型自己的实例不计入占用（问的是「还能不能再加一份」） ----
    reg3 = FakeReg([inst(M4B, 0, 0, mem=3200)])
    check("卡上只有决策模型自己时不算占用 ⇒ 不告警",
          ContentReviewManager._memory_warning(reg3, 0, M4B) == "",
          ContentReviewManager._memory_warning(reg3, 0, M4B))

    # ---- 远端 peer 不占本板显存 ----
    reg4 = FakeReg([inst("peer-model", 0, 0, mem=5500, source="adopted")])
    check("远端 peer 的显存不计入本板卡位 ⇒ 不告警",
          ContentReviewManager._memory_warning(reg4, 0, M4B) == "",
          ContentReviewManager._memory_warning(reg4, 0, M4B))

    # ---- 另一张卡上的实例不计入 ----
    reg5 = FakeReg([inst("hy-mt-1-5", 1, 0, mem=2200)])
    check("另一张卡上的实例不计入本卡 ⇒ 不告警",
          ContentReviewManager._memory_warning(reg5, 0, M4B) == "",
          ContentReviewManager._memory_warning(reg5, 0, M4B))

    # ---- 两个决策模型不叠同一张卡（否则 autostart 条目互相顶掉）----
    DEC = ("swcap_decision",)
    reg8 = FakeReg([inst(M4B, 0, 0, mem=3200, caps=DEC)])
    check("首选卡上跑着别的决策模型 ⇒ 换一张空卡",
          ContentReviewManager._pick_card(reg8, 0, M08B) == 1,
          str(ContentReviewManager._pick_card(reg8, 0, M08B)))
    check("首选卡空着 ⇒ 就用它",
          ContentReviewManager._pick_card(reg8, 1, M08B) == 1,
          str(ContentReviewManager._pick_card(reg8, 1, M08B)))
    check("自己已经在那张卡上 ⇒ 不换卡",
          ContentReviewManager._pick_card(reg8, 0, M4B) == 0,
          str(ContentReviewManager._pick_card(reg8, 0, M4B)))
    # 每张卡都被**别的**决策模型占着 ⇒ 只能报错（用第三、第四个模型 id
    # 构造 —— 真实只有 4B / 0.8B 两个型号时这条走不到，属防御分支）
    reg9 = FakeReg([inst("dec-third", 0, 0, mem=3200, caps=DEC),
                     inst("dec-fourth", 1, 0, mem=900, caps=DEC)])
    try:
        got = ContentReviewManager._pick_card(reg9, 0, "dec-fifth")
        check("每张卡都被别的决策模型占着 ⇒ 明确报错", False,
              "居然返回了 %r" % got)
    except RuntimeError as exc:
        check("每张卡都被别的决策模型占着 ⇒ 明确报错", True, str(exc))
    # 另一个型号已经在的那张卡，对它自己是可以用的
    check("自己已经跑在某张卡上 ⇒ 换过去是对的",
          ContentReviewManager._pick_card(reg9, 0, "dec-fourth") == 1,
          str(ContentReviewManager._pick_card(reg9, 0, "dec-fourth")))
    # 别的模型（翻译）占着卡不算「决策模型占卡」⇒ 决策模型照常能上去
    reg10 = FakeReg([inst("hy-mt-1-5", 0, 0, mem=2200)])
    check("卡上跑的是**别的**模型 ⇒ 决策模型照常能用这张卡",
          ContentReviewManager._pick_card(reg10, 0, M08B) == 0,
          str(ContentReviewManager._pick_card(reg10, 0, M08B)))

    # ---- 翻译避让：卡 0 被决策模型占着（3200）⇒ 翻译只在卡 1 起 ----
    from wdnapps_audio_chat.cards.web_translate.service import (
        CARD_MEMORY_MB,
        TRANSLATE_MEMORY_MB,
        _cards_with_room_for_one_copy,
        _slots,
    )
    reg6 = FakeReg([inst(M4B, 0, 0, mem=3200)])
    free = _cards_with_room_for_one_copy(reg6)
    check("决策模型占着的卡对翻译不可用（3200+2200 > 5120）",
          0 not in free, str(free))
    check("空卡对翻译可用", 1 in free, str(free))
    slots = _slots(reg6)
    check("翻译槽位避开被占用的卡", all(n != 0 for (n, _r) in slots), str(slots))
    check("翻译槽位仍然落在另一张卡上", len(slots) == 2, str(slots))
    check("卡容量与每份估算写进了常量",
          CARD_MEMORY_MB == 5120 and TRANSLATE_MEMORY_MB == 2200,
          "%s/%s" % (CARD_MEMORY_MB, TRANSLATE_MEMORY_MB))

    # 两张卡都空 ⇒ 翻译照旧 4 份
    reg7 = FakeReg([])
    check("没有别的模型时翻译仍是 4 份",
          len(_slots(reg7)) == 4, str(_slots(reg7)))

    # 视觉常驻：决策模型的视觉编码器默认常驻（启动即加载）
    try:
        # ⚠️ 用 ``_intern_decision_spec()`` 而不是
        # ``SystemLanguageModel.intern_decision_4b()`` —— 后者会**校验模型
        # 文件是否在默认目录**（/userdata/models），本板装在别处 ⇒ 直接
        # FileNotFoundError，跟被测的 option 毫无关系。
        from wdnai.model import (
            _INTERN_DECISION_MODELS,
            _intern_decision_spec,
        )
        for tag in ("4B", "0.8B"):
            spec = _intern_decision_spec(_INTERN_DECISION_MODELS[tag])
            check("%s 决策模型默认 keep_vision_resident = True" % tag,
                  bool(spec.options.get("keep_vision_resident")) is True,
                  str(spec.options.get("keep_vision_resident")))
    except Exception as exc:  # noqa: BLE001
        check("决策模型默认 keep_vision_resident = True", False, repr(exc))

    # ---------------------------------------------------------------- [N] --
    print("\n[7] 默认维度 + localStorage 增量迁移（真跑 app.js）")

    # 默认维度名（用户能在界面「恢复默认」里看到的那一套）
    NEW_DEFAULTS = ["广告", "诈骗", "色情低俗", "暴力血腥", "政治", "辱骂", "火星文"]
    OLD4 = [
        {"name": "广告", "description": "画面或文字里有推销、引流、优惠信息"},
        {"name": "诈骗", "description": "涉及转账、冒充、诱导点击等欺诈内容"},
        {"name": "色情低俗", "description": "露骨或低俗的性相关内容"},
        {"name": "暴力血腥", "description": "血腥、暴力、伤害相关的画面或描述"},
    ]

    js_ok = True
    try:
        import dukpy  # noqa: F401
    except Exception:  # noqa: BLE001
        try:
            sys.path.append("/tmp/jscheck")
            import dukpy  # noqa: F401
        except Exception:  # noqa: BLE001
            js_ok = False

    if not js_ok:
        print("  SKIP 缺 dukpy，装上再跑："
              ".venv/bin/pip install dukpy（前端逻辑项跳过）")
    else:
        # [a] 从没用过这张卡的浏览器 ⇒ 全套默认维度
        r = run_card_app_js(None)
        names = r["names"]
        check("新访客拿到 7 个默认维度", len(names) == 7, str(names))
        check("默认维度含 政治 / 辱骂 / 火星文",
              all(x in names for x in ("政治", "辱骂", "火星文")), str(names))
        check("默认维度含原有的 广告 / 诈骗 / 色情低俗 / 暴力血腥",
              all(x in names for x in ("广告", "诈骗", "色情低俗", "暴力血腥")), str(names))

        # [b] 老访客（浏览器里存的是**旧格式裸数组**）⇒ 补上新增的 3 项
        r2 = run_card_app_js(json.dumps(OLD4, ensure_ascii=False))
        n2 = r2["names"]
        check("老访客自动补齐到 7 项", len(n2) == 7, str(n2))
        check("老访客原有的 4 项保留在前",
              n2[:4] == ["广告", "诈骗", "色情低俗", "暴力血腥"], str(n2))
        check("新增的 3 项追加在后面",
              n2[4:] == ["政治", "辱骂", "火星文"], str(n2))
        check("有迁移提示告诉用户默认变了",
              "政治" in r2["notice"] and "辱骂" in r2["notice"], r2["notice"])

        # [c] 用户自己删掉的旧维度**不复活**（但新增的照样补）
        r3 = run_card_app_js(json.dumps(OLD4[1:], ensure_ascii=False))
        n3 = r3["names"]
        check("用户删掉的默认维度不复活", "广告" not in n3, str(n3))
        check("用户删掉旧维度后新增的 3 项仍然补上",
              all(x in n3 for x in ("政治", "辱骂", "火星文")), str(n3))

        # [d] 用户自己加的维度要保住
        mine = OLD4 + [{"name": "我的自定义", "description": "我自己加的"}]
        r4 = run_card_app_js(json.dumps(mine, ensure_ascii=False))
        n4 = r4["names"]
        check("用户自定义的维度保留",
              "我的自定义" in n4, str(n4))
        check("自定义维度不会被默认值挤掉",
              len(n4) == 8, str(n4))

        # [e] 幂等：拿迁移后写回的数据再跑一次 ⇒ 不重复添加
        r5 = run_card_app_js(r2["store"] or "")
        n5 = r5["names"]
        check("第二次打开不会重复添加", len(n5) == 7, str(n5))
        check("第二次打开没有多余的迁移提示", not r5["notice"], r5["notice"])

        # [f] 源码里默认维度名单别被改漏了
        js_src = open(CARD_JS, encoding="utf-8").read()
        for nm in NEW_DEFAULTS:
            check("app.js 的默认维度含 %s" % nm,
                  ('name: "%s"' % nm) in js_src)

        # [g] 模型下拉：两个型号都在、标签来自后端、切换得动
        rm = run_model_select()
        opts = rm["before"].get("options") or []
        check("模型下拉列出了两个型号", len(opts) == 2, str(opts))
        vals = [o["value"] for o in opts]
        check("下拉里两个型号的 id 正确",
              vals == ["intern-decision-4b", "intern-decision-0.8b"], str(vals))
        check("下拉标签来自后端（不是前端写死的）",
              "决策模型4B" in opts[0]["text"] and "决策模型0.8B" in opts[1]["text"],
              str([o["text"] for o in opts]))
        check("下拉里带上了显存估算",
              "3200 MB" in opts[0]["text"] and "900 MB" in opts[1]["text"],
              str([o["text"] for o in opts]))
        check("运行中的型号在选项里标出来",
              "运行中" in opts[0]["text"], str(opts[0]["text"]))
        check("提示语给出当前型号的显存", "3200 MB" in rm["before"].get("hint", ""),
              rm["before"].get("hint"))
        check("切换模型后选择被记住了（localStorage）",
              rm["after"].get("store") == "intern-decision-0.8b",
              str(rm["after"].get("store")))

    # ---------------------------------------------------------------- [N] --
    print("\n[8] 紧凑布局（别让结果被顶到屏幕外）")

    css = open(os.path.join(CARD_PKG, "static/app.css"), encoding="utf-8").read()
    tpl2 = open(TEMPLATE, encoding="utf-8").read()

    # 框架的 .imb-card 有 min-height:480px ⇒ 单列堆叠三个卡 = 1440px，
    # 结果区被顶到屏幕外。这张卡必须显式改回「按内容自适应」。
    check("卡片高度改回按内容自适应（min-height:0）",
          "#page-content-review .imb-card" in css
          and "height: auto;" in css and "min-height: 0;" in css)
    check("紧凑覆盖都限定在 #page-content-review 作用域内",
          css.count("#page-content-review") >= 10, str(css.count("#page-content-review")))
    # 没限定作用域的话，所有卡片的 app.css 拼在一起 ⇒ 会改到别的卡
    unscoped = []
    for line in css.splitlines():
        t = line.strip()
        if (t.startswith(".imb-card") or t.startswith(".page-header")
                or t.startswith(".raw-output")) and "{" in t:
            unscoped.append(t)
    check("没有没限定作用域的 .imb-card/.page-header 覆盖",
          not unscoped, str(unscoped))
    check("维度列表改成多列网格",
          "#page-content-review .cr-items" in css and "grid-template-columns" in css)

    # 结构：上面两张卡并排（.cr-grid），结果卡仍在外面保持全宽
    def _div_block(src: str, opener: str) -> str:
        """取 opener 那个 div 的完整内容（按 <div>/</div> 配平）。"""
        i = src.find(opener)
        if i < 0:
            return ""
        depth = 0
        for m in re.finditer(r"<div\b|</div>", src[i:]):
            depth += 1 if m.group(0).startswith("<div") else -1
            if depth == 0:
                return src[i:i + m.end()]
        return ""

    grid = _div_block(tpl2, '<div class="cr-grid">')
    check("模板里有 .cr-grid 并排容器", bool(grid))
    check("审查维度 在并排容器里", "审查维度" in grid)
    check("待审查内容 在并排容器里", "待审查内容" in grid)
    check("审查结果 仍在并排容器**外面**（保持全宽）", "审查结果" not in grid)
    check("输入框收成 3 行", 'rows="3"' in tpl2 and 'rows="4"' not in tpl2)

    # 结果区里的维度名是**数据**（模型的字段名），不能被界面翻译接管：
    # 界面语言是英语时它们会被翻掉，而上面输入框里的维度名不动（INPUT 在
    # 跳过名单里）⇒ 两边对不上，用户认不出哪条结果对应哪个维度。
    m_res = re.search(r'<div[^>]*id="cr-results"[^>]*>', tpl2)
    check("结果区标了 translate=\"no\"（维度名不许被界面翻译接管）",
          bool(m_res) and 'translate="no"' in m_res.group(0),
          m_res.group(0) if m_res else "(没找到)")

    # ---------------------------------------------------------------- [9] --
    print("\n[9] 结果卡徽章会跟着走（模拟用户点「开始审查」）")

    # ⚠️ **这条是被真实 bug 逼出来的**（2026-11-18 修）：结果卡的抬头徽章
    # ``cr-result-badge`` 是模板里写死的「未审查」，而 app.js 从来没碰过它
    # （只更新了另一个元素 ``cr-review-badge``）⇒ 审查完了抬头还是「未审查」。
    # 症状很像「审查没生效」，其实是徽章没接线。
    r = run_review_click()
    check("点「开始审查」后提示语落定为「完成」",
          r["note"] == "完成", r["note"])
    check("**结果卡抬头徽章**跟着变成「完成」（原来是永远「未审查」）",
          r["result_badge"] == "完成", r["result_badge"])
    check("结果卡徽章的 state 是 ok",
          r["result_state"] == "ok", r["result_state"])
    check("待审查卡那个徽章也在「完成」",
          r["review_badge"] == "完成", r["review_badge"])
    check("app.js 确实提到了 cr-result-badge",
          "cr-result-badge" in js, "")

    # 徽章的状态色：框架只给 awaiting_scan/scanned/confirmed/running/
    # expired/error/cancelled 配了色，这张卡用的 idle/busy/ok 一律落回灰底
    # ⇒ 「完成」跟「未审查」长得一模一样。
    check("ok 状态的徽章配了绿色",
          '#page-content-review #cr-result-badge[data-state="ok"]' in css,
          "")
    check("busy 状态的徽章配了琥珀色",
          '#page-content-review #cr-result-badge[data-state="busy"]' in css,
          "")

    print("\n" + "=" * 72)
    print(f"通过 {_PASS} 项,失败 {_FAIL} 项")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
