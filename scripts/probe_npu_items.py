#!/usr/bin/env python
"""逐条探针：哪些中文文案 NPU 能翻出来，哪些翻不出来。

    .venv/bin/python scripts/probe_npu_items.py [--target en] [--all-targets]

走的是**真实链路** ``POST /api/translate``，但**每次只发一条** —— 单条时
provider 链里 NPU 成功就是 NPU，失败就落到外网，一眼能看出是哪一条把
整批拖垮了（整批时只要有一条翻不出来，整批 24 条都回退外网）。

为什么需要它
------------
界面文案里混着两类「NPU 翻不出来」的东西：

* **语言名本身** —— "简体中文"、"English" 译成日文是「简体中文」「English」，
  文种判定（目标语必须含假名/谚文）判它「没翻出来」；
* **超短术语** —— "协处理器"、"本板 NPU 翻译" 这类 4~6 字的词，模型经常
  把原文原样吐回来。

这些条目会让**整批**回退外网（一个失败 = 24 条全废），所以要测吞吐就得
先挑出一个「NPU 全部能翻」的文案集。

输出：每条一行 ``✅/❌`` + 译文片段 + provider。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

API = "http://127.0.0.1:8000/api/translate"

#: 候选文案 —— 短文案和长文案混在一起，好看出「多长才稳」
CANDIDATES = [
    # —— 界面上的短词/术语（预计最容易翻不出来）——
    "界面语言", "简体中文", "英语", "日语", "韩语", "法语", "德语",
    "算力配置", "系统运行正常", "文本对话", "网页翻译", "计算卡",
    "协处理器", "已安装", "未启用", "已停止", "正在启动模型",
    "本板 NPU 翻译", "外网翻译站", "翻译服务已启用。",
    # —— 完整句子（预计稳）——
    "正在翻译，请稍候。", "翻译失败，请检查网络。", "模型加载中，请稍候。",
    "该卡位已被其它模型占用。", "启用翻译服务", "禁用翻译服务",
    "实时语音识别", "视频目标监控", "图片生成", "保存成功", "删除失败",
    "请选择文件", "上传中", "已完成",
    "正在读取状态，请稍候。", "服务详情", "模型实例", "并行实例",
]


def _post(payload: dict) -> dict:
    req = urllib.request.Request(
        API, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


def probe(text: str, target: str) -> tuple[bool, str, str]:
    """单条试一次。返回 (是不是 NPU 翻的, 译文, provider)。"""
    t0 = time.perf_counter()
    try:
        out = _post({"segments": [text], "source": "zh-CN", "target": target})
    except urllib.error.HTTPError as exc:
        return False, "(HTTP %s)" % exc.code, "?"
    except Exception as exc:  # noqa: BLE001
        return False, "(%s)" % exc, "?"
    tr = (out.get("translations") or [""])[0]
    prov = out.get("provider") or "?"
    dt = time.perf_counter() - t0
    good = prov == "npu"
    print("  %s %-22s → %-40s  %-7s %.2fs"
          % ("✅" if good else "❌", text[:20], (tr or "")[:38], prov, dt),
          flush=True)
    return good, tr, prov


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="en")
    ap.add_argument("--targets", default="",
                    help="逗号分隔的多个目标语言（逐个试，取都通过的那些）")
    ap.add_argument("--emit-good", default="",
                    help="把「全部目标语言都翻得出来」的文案写成 Python 列表")
    args = ap.parse_args()

    targets = ([t.strip() for t in args.targets.split(",") if t.strip()]
               or [args.target])

    good_map: dict[str, list[bool]] = {}
    for text in CANDIDATES:
        row = []
        for tgt in targets:
            ok, _tr, _prov = probe(text, tgt)
            row.append(ok)
            if not ok:
                break          # 已经挂了，不用再试别的目标语言
        good_map[text] = row

    print("\n===== 汇总（目标语言：%s）=====" % ", ".join(targets))
    all_ok = [t for t, r in good_map.items() if all(r)]
    print("全部目标语言都翻得出来（%d 条）：" % len(all_ok))
    for t in all_ok:
        print("    %r," % t)

    if args.emit_good:
        with open(args.emit_good, "w", encoding="utf-8") as fp:
            fp.write("# 由 scripts/probe_npu_items.py 生成 —— "
                     "NPU 能稳定翻译的文案集\n")
            fp.write("GOOD_SEGMENTS = [\n")
            for t in all_ok:
                fp.write("    %r,\n" % t)
            fp.write("]\n")
        print("\n已写入 %s" % args.emit_good)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
