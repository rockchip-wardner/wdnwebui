#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 Qwen-Image VAE 解码器 ONNX 里的 **5D「通道族」张量** 改成 RKNN2 工具链友好的排布。

背景（实测：RKNNMemStatisticsPass 逐算子内存表）
----------------------------------------------
RKNN-Toolkit2 把 5D 张量按 NCHW 解释成 ``(d0*d1, d2, d3, d4)``：

    ONNX  [1, 1152, 1, 32, 32]  ->  (1152, 1, 32, 32)   # C=1
    ONNX  [1, 1, 288, 256, 256]  ->  (1, 288, 256, 256)  # C=288  OK

NC1HWC2 要求 C2=16，所以 C=1/2/4 会被 **补到 16** ⇒ 通道维 16~32 倍放大。
VAE 里绝大多数中间张量都是 [1, C, 1, H, W]（D=1），于是
``576ch x 256 x 256`` 的一个中间量被物化成 9216 通道 = 2304 MiB。

修法（**扁平内存逐字节不变**）
------------------------------
把「D=1 的 5D 张量」的通道轴从位置 1 挪到位置 2::

    [1, C, 1, H, W]   ->   [1, 1, C, H, W]

被挪动的轴长度是 1 ⇒ 扁平下标完全一致 ⇒ 数值逐位不变；
工具链随即按 (1, C, H, W) 解释 ⇒ C1=C/16, C2=16 ⇒ **不再放大**。

三个难点（这版都处理了）
------------------------
1. **Slice 的输出形状是机械推导的**，改不了标签 ⇒ 在它后面插一个
   恒等 Reshape 来完成换标签。
2. **6D/7D「像素 shuffle 链」夹在 Conv 路径中间**（add_7 → 6D → slice_12
   → add_19）。链内部的 6D/7D 张量形状不能换（轴不可拆），所以在链的
   **入口插一个「还原 Reshape」**，把通道轴挪回原位，链内部原样不动。
3. **per-channel 向量 [C,1,1,1]** 与 5D 张量广播，换标签后必须变成
   [1,C,1,1]（raw data 一个字节都不用动）。

用法
----
    python to4d.py <src.onnx> <dst.onnx> [--no-input4d] [--diag]
"""
import argparse
import copy
import os

import numpy as np
import onnx
from onnx import external_data_helper, helper, numpy_helper


def _dims(vi):
    out = []
    for d in vi.type.tensor_type.shape.dim:
        if d.HasField("dim_value"):
            out.append(int(d.dim_value))
        elif d.HasField("dim_param"):
            out.append(d.dim_param)
        else:
            out.append(-1)
    return tuple(out)


def swap12(s):
    l = list(s)
    l[1], l[2] = l[2], l[1]
    return tuple(l)


def is_cf(s):
    """5D 且 axis2(D)==1、axis1(C)>1 ⇒ 受 16 倍放大之害的形状。"""
    return (len(s) == 5 and isinstance(s[0], int) and s[0] == 1
            and isinstance(s[1], int) and s[1] > 1
            and isinstance(s[2], int) and s[2] == 1)


def set_shape(vi, vals):
    dim = vi.type.tensor_type.shape.dim
    del dim[:]
    for v in vals:
        d = dim.add()
        if isinstance(v, int) and v >= 0:
            d.dim_value = int(v)
        else:
            d.dim_param = str(v)


# 需要按「输入轴」重映射的属性算子
AXIS_ON_INPUT = {"Gather", "Split", "Squeeze", "Softmax", "Concat",
                 "ReduceL2", "ReduceSum", "ReduceMean", "ReduceMax", "ReduceMin"}


def inline_int64(m, base_dir):
    """把 **int64 常量**（shape/axes）从外部 .data 改成内联。

    为什么必须这么做：这些常量在原图里放在 vae512_ps_g2g.onnx.data 里，
    而 self-check 要「剥掉 value_info 后重新推断」—— 推断器读不到外部常量
    的值就算不出 Unsqueeze/Reshape 的输出形状（自检会误报几百处不一致）。
    权重（float）保持外部引用不变，模型体积不受影响。
    """
    n = 0
    for t in m.graph.initializer:
        if (t.data_type == onnx.TensorProto.INT64
                and t.data_location == onnx.TensorProto.EXTERNAL):
            external_data_helper.load_external_data_for_tensor(t, base_dir)
            arr = numpy_helper.to_array(t)
            t.CopyFrom(numpy_helper.from_array(arr, name=t.name))
            n += 1
    return n


def transform(src, dst, input4d=True, diag=False):
    base_dir = os.path.dirname(os.path.abspath(src))
    m = onnx.load(src, load_external_data=False)
    print("[0a] 内联 int64 常量 %d 个" % inline_int64(m, base_dir))

    # ---- 0. shape inference（ps_g2g 图的 SDPA 已拆成 MatMul/Softmax） ----
    try:
        m = onnx.shape_inference.infer_shapes(m, strict_mode=False, data_prop=True)
        print("[0] shape_inference OK：value_info %d 条" % len(m.graph.value_info))
    except Exception as e:
        print("[0] shape_inference 失败(%s)，沿用原图 value_info" % e)
    g = m.graph

    shapes = {}
    for vi in list(g.input) + list(g.output) + list(g.value_info):
        shapes[vi.name] = _dims(vi)
    inits = {t.name: t for t in g.initializer}
    for name, t in inits.items():
        shapes.setdefault(name, tuple(t.dims))
    orig_shapes = dict(shapes)

    swapped = {n: is_cf(s) for n, s in shapes.items()}
    print("[1] 张量 %d，5D-通道族 %d" % (len(shapes), sum(1 for v in swapped.values() if v)))

    def sig_of(name):
        if swapped.get(name):
            return lambda a: {1: 2, 2: 1}.get(a, a)
        return lambda a: a

    def read_i64(name):
        try:
            return np.array(numpy_helper.to_array(inits[name])).reshape(-1).astype(np.int64)
        except Exception:
            return None

    new_inits = []
    used = set(inits)
    seq = [9000]

    def make_i64(vals):
        """新建一个 int64 常量。**必须用 val_* 命名** —— 工具链只给 val_*
        命名的常量建 value info（原图所有 shape 常量都叫 val_N），用别的名字
        会报 "Can not find tensor value info for '<name>'!"。"""
        vals = [int(v) for v in vals]
        key = "val_%d" % seq[0]
        seq[0] += 1
        used.add(key)
        new_inits.append(numpy_helper.from_array(np.array(vals, dtype=np.int64), name=key))
        return key

    # ---- 1. 找出「6D/7D 链入口」：输入是通道族、输出 rank>=6 -------------
    # 这些节点不能直接吃换标签后的输入（输出形状会变），要在前面插「还原 Reshape」。
    restore_in = set()          # 需要还原标签的节点（按节点 id）
    for idx, node in enumerate(g.node):
        if not node.input or not node.output:
            continue
        in_sh = shapes.get(node.input[0])
        out_sh = shapes.get(node.output[0])
        if not in_sh or not out_sh:
            continue
        if swapped.get(node.input[0]) and len(out_sh) >= 6:
            restore_in.add(idx)
    print("[1b] 6D/7D 链入口 %d 个（要插还原 Reshape）" % len(restore_in))

    # ---- 2. 逐节点改写属性 ------------------------------------------------
    changed = []
    for idx, node in enumerate(g.node):
        op = node.op_type
        data_in = node.input[0] if node.input else None
        out0 = node.output[0] if node.output else None
        # 链入口的输入被「还原」了 ⇒ 按原标签处理轴
        in_relabelled = swapped.get(data_in) and idx not in restore_in
        s_in = sig_of(data_in) if in_relabelled else (lambda a: a)
        s_out = sig_of(out0) if out0 else (lambda a: a)

        if op in ("Squeeze", "Unsqueeze"):
            if len(node.input) >= 2 and node.input[1] in inits:
                ax = read_i64(node.input[1])
                if ax is None:
                    raise RuntimeError("读不出 axes %s" % node.input[1])
                old = [int(a) for a in ax]
                new = ([int(s_in(a)) for a in old] if op == "Squeeze"
                       else [int(s_out(a)) for a in old])
                if new != old:
                    node.input[1] = make_i64(new)
                    changed.append((node.name, op, old, new))

        elif op == "Gather":
            for a in node.attribute:
                if a.name == "axis":
                    old = a.i
                    a.i = int(s_in(a.i))
                    if a.i != old:
                        changed.append((node.name, op, old, a.i))

        elif op == "Split":
            for a in node.attribute:
                if a.name == "axis":
                    old = a.i
                    a.i = int(s_in(a.i))
                    if a.i != old:
                        changed.append((node.name, op, old, a.i))

        elif op == "Softmax":
            for a in node.attribute:
                if a.name == "axis" and a.i >= 0:
                    old = a.i
                    a.i = int(s_in(a.i))
                    if a.i != old:
                        changed.append((node.name, op, old, a.i))

        elif op == "Concat":
            for a in node.attribute:
                if a.name == "axis":
                    old = a.i
                    a.i = int(s_out(a.i))
                    if a.i != old:
                        changed.append((node.name, op, old, a.i))

        elif op.startswith("Reduce"):
            if len(node.input) >= 2 and node.input[1] in inits:
                ax = read_i64(node.input[1])
                if ax is not None:
                    old = [int(a) for a in ax]
                    new = [int(s_in(a)) for a in old]
                    if new != old:
                        node.input[1] = make_i64(new)
                        changed.append((node.name, op, old, new))

        elif op == "Reshape":
            if len(node.input) >= 2 and node.input[1] in inits:
                tgt = read_i64(node.input[1])
                if tgt is None:
                    raise RuntimeError("读不出 reshape 目标 %s" % node.input[1])
                old = [int(v) for v in tgt]
                new = list(swap12(tuple(old))) if (out0 and swapped.get(out0)) else old
                node.input[1] = make_i64(new)
                if new != old:
                    changed.append((node.name, op, old, new))

        elif op == "Expand":
            if len(node.input) >= 2 and node.input[1] in inits:
                tgt = read_i64(node.input[1])
                if tgt is None:
                    raise RuntimeError("读不出 expand 目标 %s" % node.input[1])
                old = [int(v) for v in tgt]
                new = list(swap12(tuple(old))) if (out0 and swapped.get(out0)) else old
                node.input[1] = make_i64(new)
                if new != old:
                    changed.append((node.name, op, old, new))

        elif op == "Transpose":
            perm = None
            for a in node.attribute:
                if a.name == "perm":
                    perm = list(a.ints)
            if perm:
                new_perm = [int(s_in(int(perm[s_out(i)]))) for i in range(len(perm))]
                for a in node.attribute:
                    if a.name == "perm":
                        a.ints[:] = new_perm
                if new_perm != list(perm):
                    changed.append((node.name, op, perm, new_perm))

        elif op in ("Slice", "Pad"):
            if len(node.input) >= 3 and node.input[3] in inits:
                ax = read_i64(node.input[3])
                if ax is not None:
                    old = [int(a) for a in ax]
                    node.input[3] = make_i64([int(s_in(a)) for a in old])

        elif op in ("Conv", "ConvTranspose", "Mul", "Div", "Add", "Sub",
                    "Sigmoid", "Clip", "Tanh", "Erf", "Pow", "Sqrt", "Rsqrt",
                    "MatMul", "Gemm", "Identity", "Neg", "Abs", "Log", "Exp",
                    "Resize", "Cast"):
            pass
        else:
            print("!! 未处理算子: %s (%s)" % (op, node.name))

    print("[2] 改写属性 %d 处" % len(changed))
    if diag:
        for c in changed[:200]:
            print("      %-24s %-10s %s -> %s" % c)

    # ---- 3. 插入「还原 Reshape」（链入口）与「换标签 Reshape」（Slice 后）----
    nodes = list(g.node)
    extra_after = {}     # idx -> [node]  插在该节点**之后**
    extra_before = {}    # idx -> [node]  插在该节点**之前**
    inserted_vi = []
    # 3a. Slice 的输出形状是机械推导的 ⇒ 在其后插一个 Reshape 完成换标签
    for node in nodes:
        if node.op_type != "Slice" or not node.output:
            continue
        out0 = node.output[0]
        if not swapped.get(out0):
            continue
        newname = out0 + "_rl"
        tgt = make_i64(list(swap12(shapes[out0])))
        rs = helper.make_node("Reshape", [out0, tgt], [newname], name=node.name + "_relabel")
        extra_after.setdefault(id(node), []).append(rs)
        # 把消费者的输入改到新张量
        for cn in g.node:
            if cn is node:
                continue
            for k, inp in enumerate(cn.input):
                if inp == out0:
                    cn.input[k] = newname
        shapes[newname] = swap12(shapes[out0])
        swapped[newname] = False
        # Slice 自己的输出保持原标签（形状是机械推导的），换标签由新张量承担
        swapped[out0] = False
        g.value_info.append(helper.make_tensor_value_info(
            newname, onnx.TensorProto.FLOAT, list(shapes[newname])))
        inserted_vi.append(newname)
        print("    Slice %s 输出 %s -> 插换标签 Reshape %s" % (node.name, out0, newname))
    # 3b. 链入口：输入还原成原标签
    for idx, node in enumerate(nodes):
        if idx not in restore_in:
            continue
        inp = node.input[0]
        old_shape = orig_shapes.get(inp)
        if not old_shape:
            continue
        tgt = make_i64(list(old_shape))
        newname = inp + "_rs"
        rs = helper.make_node("Reshape", [inp, tgt], [newname], name=node.name + "_restore")
        extra_before.setdefault(id(node), []).append(rs)
        node.input[0] = newname
        shapes[newname] = tuple(old_shape)
        swapped[newname] = False
        g.value_info.append(helper.make_tensor_value_info(
            newname, onnx.TensorProto.FLOAT, list(shapes[newname])))
        inserted_vi.append(newname)
        print("    链入口 %s：输入 %s 还原成 %s" % (node.name, inp, list(old_shape)))
    # ⚠️ 必须**按拓扑序**重建节点列表：工具链的 fold_constant 依赖前序节点
    # 已经处理过，追加到末尾会报 KeyError('<新张量名>')。
    rebuilt = []
    for node in nodes:
        rebuilt.extend(extra_before.get(id(node), []))
        rebuilt.append(node)
        rebuilt.extend(extra_after.get(id(node), []))
    del g.node[:]
    g.node.extend(rebuilt)

    # ---- 4. 新 initializer + value_info ------------------------------------
    for t in new_inits:
        g.initializer.append(t)
        shapes[t.name] = tuple(t.dims)
        g.value_info.append(helper.make_tensor_value_info(
            t.name, onnx.TensorProto.INT64, list(t.dims)))

    # ---- 5. per-channel 向量 [C,1,1,1] -> [1,C,1,1] ------------------------
    n_fix = 0
    for t in g.initializer:
        d = list(t.dims)
        if len(d) == 4 and d[0] > 1 and d[1] == 1 and d[2] == 1 and d[3] == 1:
            t.dims[:] = [1, d[0], 1, 1]
            n_fix += 1
    print("[3] per-channel 向量 [C,1,1,1] -> [1,C,1,1]: %d 个" % n_fix)

    # ---- 6. 图输入 5D -> 4D ------------------------------------------------
    override = {t.name for t in new_inits}
    if input4d:
        for vi in g.input:
            s = _dims(vi)
            if not is_cf(s):
                continue
            new4 = (s[0], s[1], s[3], s[4])
            cons = [n for n in g.node if vi.name in n.input]
            print("[4] 图输入 %s %s -> %s ; 消费者 %s"
                  % (vi.name, list(s), list(new4), [(n.name, n.op_type) for n in cons]))
            if len(cons) != 1 or cons[0].op_type != "Squeeze":
                print("    !! 消费者不是单个 Squeeze，跳过输入降维")
                continue
            n0 = cons[0]
            out_name = n0.output[0]
            for cn in g.node:
                for k, inp in enumerate(cn.input):
                    if inp == out_name:
                        cn.input[k] = vi.name
            g.node.remove(n0)
            shapes[vi.name] = new4
            swapped[vi.name] = False
            override.add(vi.name)

    # ---- 7. 形状回写 --------------------------------------------------------
    for vi in list(g.input) + list(g.output) + list(g.value_info):
        old = _dims(vi)
        if vi.name in override:
            ns = shapes[vi.name]
        elif swapped.get(vi.name):
            ns = swap12(old)
        else:
            ns = old
        set_shape(vi, ns)

    # ---- 8. 自检：剥掉 value_info 重新推断，逐张量比对 ----------------------
    try:
        mchk = copy.deepcopy(m)
        # 只保留「插入的 Reshape 输出」的 value_info（它们需要**类型**，
        # 否则推断器报 "Input 0 expected to have type but instead is null"）；
        # 其余全部删掉 ⇒ 形状全部由结构重算。
        for vi in list(mchk.graph.value_info):
            if vi.name not in inserted_vi:
                mchk.graph.value_info.remove(vi)
        m3 = onnx.shape_inference.infer_shapes(mchk, strict_mode=False, data_prop=True)
        sh3 = {}
        for vi in list(m3.graph.input) + list(m3.graph.output) + list(m3.graph.value_info):
            sh3[vi.name] = _dims(vi)
        prod = {}
        for n in m3.graph.node:
            for o in n.output:
                prod[o] = n
        bad = []
        for nm, old_s in orig_shapes.items():
            if nm in override or nm not in sh3:
                continue
            exp = swap12(old_s) if swapped.get(nm) else old_s
            got = sh3[nm]
            if tuple(got) != tuple(exp):
                n = prod.get(nm)
                bad.append((nm, list(old_s), list(exp), list(got),
                            (n.op_type, list(n.input)) if n else None))
        if bad:
            print("!! 形状不一致 %d 处（重算 vs 期望）：" % len(bad))
            for b in bad[:20]:
                print("     %-26s 原=%-22s 期望=%-22s 实际=%-22s %s" % b)
        else:
            print("[5] 自检通过：%d 个张量的重算形状与预期完全一致" % len(orig_shapes))
    except Exception as e:
        print("!! 自检失败：%s" % e)

    # ---- 9. 保存 ------------------------------------------------------------
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    onnx.save_model(m, dst)
    print("[6] 写出 %s (%.1f MB)" % (dst, os.path.getsize(dst) / 1048576))
    return m


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--no-input4d", action="store_true")
    ap.add_argument("--diag", action="store_true")
    a = ap.parse_args()
    transform(a.src, a.dst, input4d=not a.no_input4d, diag=a.diag)
