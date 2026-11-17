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
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

CARD_PKG = os.path.join(
    REPO, ".venv/src/wdnapps_audio_chat/src/wdnapps_audio_chat/cards/content_review"
)
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

    print("\n" + "=" * 72)
    print(f"通过 {_PASS} 项,失败 {_FAIL} 项")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
