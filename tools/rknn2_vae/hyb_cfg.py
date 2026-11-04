#!/usr/bin/env python3
"""改写 step1 生成的 `<name>.quantization.cfg` 里的 custom_quantize_layers。

文档（User_Guide 6.3.2 第 2 步）：

    custom_quantize_layers:
        Conv__350:0: float16
        Conv__358:0: float16
    quantize_parameters:
        ...

    「custom_quantize_layers下每一行可按照tensor名: 量化类型的格式添加自定义量化层，
      该tensor对应层的运算类型即改为指定运算类型。目前量化数据类型可选择float16。」

支持的 dtype：int8 / int16 / float16。

**为什么用文本级改写而不是 yaml.dump**：
cfg 里 quantize_parameters 存着一堆 float 量化参数，yaml 重新序列化会改写
数字的字面量（1e-05 / 0.00001 / 1.0E-05 之类），工具链再读回来时逐字节不同
⇒ 量化参数悄悄变了。文本级改写只碰 custom_quantize_layers 那一段，
其余部分逐字节保持原样。

用法:
    python hyb_cfg.py --cfg hybwork/c2/vae512_c2.quantization.cfg \
        --fp16 mul_71_sw_sw,mul_75_sw_sw ... --mode union --out <改好的 cfg>
"""
import argparse
import os
import re
import sys

#: 一条 custom_quantize_layers 条目的形状：`  <tensor>:<idx>: <dtype>`
ENTRY_RE = re.compile(r"^\s+(?P<name>[^:\s]+):(?P<idx>\d+):\s+(?P<dtype>\w+)\s*$")
HEADER_RE = re.compile(r"^custom_quantize_layers:\s*(?P<inline>\{.*\})?\s*$")


def parse_block(lines, start):
    """返回 (entries, end_idx)：entries 是 [(name, idx, dtype)]，end_idx 是块结束行号（不含）。"""
    entries = []
    i = start + 1
    while i < len(lines):
        ln = lines[i]
        if ln.strip() == "":
            # 空行：只要后面还有更深的缩进就继续，否则块结束
            j = i
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            if j < len(lines) and lines[j][:1] in (" ", "\t"):
                i = j
                continue
            break
        if not ln.startswith((" ", "\t")):
            break  # 顶格 => 下一个顶层 key，块结束
        m = ENTRY_RE.match(ln)
        if m:
            entries.append((m.group("name"), int(m.group("idx")), m.group("dtype")))
        i += 1
    return entries, i


def write_cfg(src, dst, entries, keep_quant_params=True, with_index=False):
    with open(src) as f:
        lines = f.read().split("\n")

    hdr = None
    for i, ln in enumerate(lines):
        if HEADER_RE.match(ln):
            hdr = i
            break
    if hdr is None:
        raise SystemExit("找不到 custom_quantize_layers 段")

    old_entries, end = parse_block(lines, hdr)
    block = ["custom_quantize_layers:"]
    for name, idx, dtype in entries:
        # 这张图的张量名是**裸名**（quantize_parameters 的 key 就是 `mul_53_sw_sw:`），
        # 不带 mobilenet 文档例子里那种 `:0` 后缀 —— 加了会报
        # "Invalid operands name 'mul_53_sw_sw:0'"。
        block.append("    %s%s: %s" % (name, (":%d" % idx) if with_index else "", dtype))
    new_lines = lines[:hdr] + block + lines[end:]
    if not keep_quant_params:
        raise SystemExit("当前实现总是保留 quantize_parameters 段")
    with open(dst, "w") as f:
        f.write("\n".join(new_lines))
    return old_entries


def check_names(cfg_path, name_file):
    """校验待留 fp16 的层名在 cfg 里存不存在。

    cfg 的 quantize_parameters 段是「每层一个 key」的 YAML，key 的缩进固定
    4 空格；custom_quantize_layers 段是 `名: idx: dtype`。
    """
    with open(cfg_path) as f:
        text = f.read()
    lines = text.split("\n")

    # quantize_parameters 的 key：缩进 4 且以 ':' 结尾
    qp = set()
    in_qp = False
    for ln in lines:
        if ln.startswith("quantize_parameters:"):
            in_qp = True
            continue
        if in_qp:
            if ln and not ln.startswith(" "):
                break
            m = re.match(r"^    ([^:\s][^:]*):\s*$", ln)
            if m:
                qp.add(m.group(1).strip())

    cql = set()
    for m in re.finditer(r"^\s+([^:\s]+):\d+:\s*(\w+)\s*$", text, re.M):
        cql.add(m.group(1))

    names = []
    with open(name_file) as f:
        for ln in f:
            ln = ln.split("#")[0].strip()
            if ln:
                names.append(ln.split()[0])

    print("### quantize_parameters 层数: %d" % len(qp))
    print("### custom_quantize_layers 层数: %d" % len(cql))
    print("### 待校验: %d 个" % len(names))
    miss_qp, miss_cql = [], []
    for n in names:
        a = "qp" if n in qp else ("cql" if n in cql else "MISS")
        if n not in qp and n not in cql:
            miss_qp.append(n)
        print("    %-26s %s" % (n, a))
    print("### 完全不存在的层: %d" % len(miss_qp))
    for n in miss_qp:
        print("    MISSING %s" % n)
    return 0 if not miss_qp else 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cfg", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--fp16", default="", help="逗号分隔的 tensor 名 -> float16")
    ap.add_argument("--fp16-file", default=None, help="每行一个 tensor 名 -> float16")
    ap.add_argument("--int8", default="", help="逗号分隔的 tensor 名 -> int8")
    ap.add_argument("--mode", default="union", choices=["union", "ours", "theirs"],
                    help="union=工具链自动填的 + 我们加的；ours=只用我们加的；theirs=只用工具链的")
    ap.add_argument("--with-index", action="store_true",
                    help="张量名带 :0 后缀（mobilenet 文档例子的格式；"
                         "这张图的张量名是裸名，不要加）")
    ap.add_argument("--list", action="store_true", help="只打印现有的条目，不改写")
    ap.add_argument("--check-file", default=None,
                    help="校验：这些层名在 cfg 的 quantize_parameters / "
                         "custom_quantize_layers 里存不存在")
    args = ap.parse_args()

    if args.check_file:
        return check_names(args.cfg, args.check_file)

    with open(args.cfg) as f:
        lines = f.read().split("\n")
    hdr = None
    for i, ln in enumerate(lines):
        if HEADER_RE.match(ln):
            hdr = i
            break
    if hdr is None:
        raise SystemExit("找不到 custom_quantize_layers 段")
    old, end = parse_block(lines, hdr)
    print("### 工具链自动填了 %d 条 custom_quantize_layers" % len(old))
    for n, i, d in old[:200]:
        print("    %s:%d: %s" % (n, i, d))
    if args.list:
        return 0

    ours = []
    if args.fp16:
        ours += [(n.strip(), 0, "float16") for n in args.fp16.split(",") if n.strip()]
    if args.fp16_file:
        with open(args.fp16_file) as f:
            for ln in f:
                ln = ln.split("#")[0].strip()
                if ln:
                    # 每行可能带尾注（"名字 # 说明"）—— 只取第一个 token
                    ours.append((ln.split()[0], 0, "float16"))
    for n in [x.strip() for x in args.int8.split(",") if x.strip()]:
        ours.append((n, 0, "int8"))

    if args.mode == "union":
        merged = list(old) + [e for e in ours if e[0] not in {o[0] for o in old}]
    elif args.mode == "ours":
        merged = ours
    else:
        merged = list(old)

    # 同一个 tensor 只保留最后一条（ours 覆盖 theirs）
    dedup = {}
    for n, i, d in merged:
        dedup[n] = (n, i, d)
    merged = list(dedup.values())

    write_cfg(args.cfg, args.out, merged, with_index=args.with_index)
    print("### 写出 %s：%d 条（theirs=%d ours=%d）" %
          (args.out, len(merged), len(old), len(ours)))
    for n, i, d in merged:
        print("    %s%s: %s" % (n, (":%d" % i) if args.with_index else "", d))
    return 0


if __name__ == "__main__":
    sys.exit(main())
