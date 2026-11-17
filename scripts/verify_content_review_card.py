#!/usr/bin/env python3
"""图文内容审查卡自检 —— 决策任务构造得对不对，概率能不能过 DDS。

背景
----
「图文内容审查」卡把用户维护的**可增删列表**翻译成 Intern-Decision 的
决策任务（每个列表项 = 一个 ``noul`` 字段 = 一次是否判断），再把模型
返回的**概率**画成进度条。

这条链路上有三个**不报错但会静默出错**的坑，本脚本逐个钉住：

1. **决策任务形状**：``noul`` 的 ``criteria`` 必须**恰好两项**且键命中
   no/yes，否则 ``wdnai.decision.parse_decision_task`` 直接抛错 —— 本脚本
   用**官方解析器**校验我们构造的任务，而不是自己断言字段名。
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
    max_text_chars: 4000, detail: "", conflict: "", cards: [],
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


def main() -> int:
    print("=" * 72)
    print("图文内容审查卡自检")
    print("=" * 72)

    from wdnapps_audio_chat.cards.content_review.service import (
        ContentReviewManager,
        DEFAULT_MODEL,
        MAX_ITEMS,
    )

    # ---------------------------------------------------------------- [1] --
    print("\n[1] 卡片声明")
    man = json.load(open(MANIFEST, encoding="utf-8"))
    check("manifest id = content-review", man.get("id") == "content-review", str(man.get("id")))
    check("标题是「图文内容审查」", man.get("title") == "图文内容审查", str(man.get("title")))
    check("display_order = 72（排在网页翻译 71 后面）",
          man.get("display_order") == 72, str(man.get("display_order")))
    check("用的是决策能力 swcap_decision",
          "swcap_decision" in (man.get("software_capabilities") or []),
          str(man.get("software_capabilities")))
    check("模型是 intern-decision-4b（= [JEV]图像文字决策模型4B）",
          DEFAULT_MODEL == "intern-decision-4b", DEFAULT_MODEL)

    tpl = open(TEMPLATE, encoding="utf-8").read()
    check("页面 section id = page-content-review",
          'id="page-content-review"' in tpl)
    js = open(CARD_JS, encoding="utf-8").read()
    check("前端挂了向导完成钩子 applyWizardSnapshot",
          "ImApp_content_review" in js and "applyWizardSnapshot" in js)

    wiz = json.load(open(WIZARD_JSON, encoding="utf-8"))
    m = (wiz.get("swcap_to_model") or {}).get("swcap_decision") or {}
    check("向导把 swcap_decision 绑到 intern-decision-4b",
          m.get("model_id") == "intern-decision-4b", str(m.get("model_id")))

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
    check("每个列表项一个字段", len(questions) == 2, str(list(questions)))
    check("字段名 = 列表项名称（它直接进模型 prompt）",
          set(questions) == {"广告", "诈骗"}, str(list(questions)))
    check("字段类型是 noul（每项独立的肯定概率，不归一化）",
          all(q["type"] == "noul" for q in questions.values()),
          str({k: v["type"] for k, v in questions.items()}))
    check("criteria 恰好两项且键是 no/yes",
          all(sorted(q["criteria"]) == ["no", "yes"] for q in questions.values()),
          str({k: sorted(v["criteria"]) for k, v in questions.items()}))
    check("说明写进了 instructions",
          "推销" in questions["广告"]["instructions"],
          questions["广告"]["instructions"])
    try:
        task = parse_decision_task({"questions": questions})
        check("官方 parse_decision_task 接受我们构造的任务", True)
        check("解析出的字段数一致", len(task.fields) == 2, str(len(task.fields)))
        check("解析出的选项是 no/yes 两项",
              all(len(f.options) == 2 for f in task.fields),
              str([f.options for f in task.fields]))
    except Exception as exc:  # noqa: BLE001
        check("官方 parse_decision_task 接受我们构造的任务", False, repr(exc))

    # 重名项：字段名必须唯一（JSON key 重复会被静默覆盖）
    dup = ContentReviewManager._normalize_items([
        {"name": "广告"}, {"name": "广告", "description": "x"},
    ])
    q2, fmap2 = ContentReviewManager._build_questions(dup)
    check("同名的两项被合并成一个（不会覆盖字段）", len(q2) == 1, str(list(q2)))
    check("字段映射与任务一一对应", len(fmap2) == len(q2), str(fmap2))

    # ---------------------------------------------------------------- [4] --
    print("\n[4] 概率明细解析（附件 → 每项概率）")
    detail = {
        "decisions": {
            "广告": {"type": "noul", "symbol": "B", "label": "yes",
                     "probabilities": {"no": 0.03, "yes": 0.97},
                     "logits": [1.0, 2.0]},
        },
        "temperature": 1.9924182891845703,
    }
    summary = "广告[B]=yes (97.00%)"
    from wdnai.attachment import TextAttachment
    atts = [TextAttachment(summary + "\n" + json.dumps(detail, ensure_ascii=False))]
    got = ContentReviewManager._parse_detail(atts)
    check("能从附件里抠出明细 JSON", bool(got.get("decisions")), str(got)[:120])
    check("概率读得出来",
          abs(got["decisions"]["广告"]["probabilities"]["yes"] - 0.97) < 1e-9,
          str(got.get("decisions", {}).get("广告")))
    check("校准温度也在", "temperature" in got, str(got.get("temperature")))
    check("附件为空时返回空 dict（不炸）",
          ContentReviewManager._parse_detail([]) == {},
          str(ContentReviewManager._parse_detail([])))

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
              abs(parsed.get("decisions", {}).get("广告", {})
                  .get("probabilities", {}).get("yes", 0) - 0.97) < 1e-9,
              str(parsed)[:160])
    except Exception as exc:  # noqa: BLE001
        check("wdndds 附件编解码可用", False, repr(exc))

    # ---------------------------------------------------------------- [6] --
    print("\n[6] 独占：启用决策要自动清场，翻译要避开被占的卡")
    import contextlib

    class FakeReg:
        """假的算力注册表 —— 只给这两个函数用到的部分。

        ⚠️ ``model_cards()`` 必须返回 **List[int]**（卡号列表）——
        真实现就是这个形状；写成 dict 列表会掩盖「取下标」那类 bug
        （历史上就因为当成 dict 读而 TypeError，冲突检测静默失效）。
        """

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

    def inst(model, card, replica, status="running", mem=2200):
        return {"model_id": model, "label": model, "npu_index": card,
                "npu_indices": [card], "replica": replica, "status": status,
                "memory_estimate_mb": mem, "capability_types": ["x"]}

    # 清场：卡 0 上有翻译 **2 份** + 另一个模型 1 份 ⇒ 三个实例都要停
    reg = FakeReg([
        inst("hy-mt-1-5", 0, 0),
        inst("hy-mt-1-5", 0, 1),
        inst("qwen3-vl-4b", 0, 0),
        inst("hy-mt-1-5", 1, 0),      # 另一张卡 —— 不该被动
    ])
    stopped = ContentReviewManager._clear_card(reg, 0)
    check("清场停掉了卡 0 上的全部 3 个实例", len(stopped) == 3,
          str([(s["model_id"], s["replica"]) for s in stopped]))
    check("同卡第 2 份也没漏（stop_instance_local 只停一个实例）",
          ("hy-mt-1-5", 0, 1) in reg.stopped, str(reg.stopped))
    check("另一张卡上的实例没被动", ("hy-mt-1-5", 1, 0) not in reg.stopped,
          str(reg.stopped))
    check("决策模型自己的实例不会被自己清掉",
          all(s["model_id"] != DEFAULT_MODEL for s in stopped), str(stopped))

    # 清场：决策模型自己已经在那张卡上 ⇒ 什么都不用停
    reg2 = FakeReg([inst(DEFAULT_MODEL, 0, 0, mem=3200)])
    check("卡上只有决策模型时清场是空操作",
          ContentReviewManager._clear_card(reg2, 0) == [], str(reg2.stopped))

    # 翻译避让：卡 0 被决策模型占着（3200）⇒ 翻译只在卡 1 起
    from wdnapps_audio_chat.cards.web_translate.service import (
        CARD_MEMORY_MB,
        TRANSLATE_MEMORY_MB,
        _cards_with_room_for_one_copy,
        _slots,
    )
    reg3 = FakeReg([inst(DEFAULT_MODEL, 0, 0, mem=3200)])
    free = _cards_with_room_for_one_copy(reg3)
    check("决策模型占着的卡对翻译不可用（3200+2200 > 5120）",
          0 not in free, str(free))
    check("空卡对翻译可用", 1 in free, str(free))
    slots = _slots(reg3)
    check("翻译槽位避开被占用的卡", all(n != 0 for (n, _r) in slots), str(slots))
    check("翻译槽位仍然落在另一张卡上", len(slots) == 2, str(slots))
    check("卡容量与每份估算写进了常量",
          CARD_MEMORY_MB == 5120 and TRANSLATE_MEMORY_MB == 2200,
          "%s/%s" % (CARD_MEMORY_MB, TRANSLATE_MEMORY_MB))

    # 两张卡都空 ⇒ 翻译照旧 4 份
    reg4 = FakeReg([])
    check("没有别的模型时翻译仍是 4 份",
          len(_slots(reg4)) == 4, str(_slots(reg4)))

    # 视觉常驻：决策模型的视觉编码器默认常驻（启动即加载）
    try:
        # ⚠️ 用 ``_intern_decision_4b_spec()`` 而不是
        # ``SystemLanguageModel.intern_decision_4b()`` —— 后者会**校验模型
        # 文件是否在默认目录**（/userdata/models），本板装在别处 ⇒ 直接
        # FileNotFoundError，跟被测的 option 毫无关系。
        from wdnai.model import _intern_decision_4b_spec
        spec = _intern_decision_4b_spec()
        check("决策模型默认 keep_vision_resident = True",
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

    print("\n" + "=" * 72)
    print(f"通过 {_PASS} 项,失败 {_FAIL} 项")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
