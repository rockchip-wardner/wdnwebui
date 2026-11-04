#!/usr/bin/env python3
"""构建一个量化配置的 RKNN 模型，在**模拟器**里跑，与 PyTorch 金标准对拍。

用途：板端真机一次要 15 分钟编译 + 20 分钟推理，还占着 NPU；模拟器在 x86 上
就能判「这个量化配置行不行」，把搜索空间缩小后再上板。

按 rknn-toolkit2 2.5.0 文档（site-packages/rknn/api/rknn.py）里
config()/build() 的参数来配置。

用法:
    quant_build.py --tag kl --quant w8a8 --algorithm kl_divergence --method channel
    quant_build.py --tag ah --quant w8a8 --algorithm normal --auto-hybrid \
                   --cos-thresh 0.98
"""
import argparse
import json
import os
import re
import sys
import time

import numpy as np

#: rknn-toolkit2 2.5.0 在这张图上会崩：
#:   reduce_tp_in_mesh_forward/_backward -> ValueError: 3 is not in list
#:   squeeze_1_in_nd_transpose + swap_reshape_transpose_v2 -> 互相反复改写，死循环
#: 这四条是工具链自己给出的绕过方式（见 build_rknn2.py 的 DEFAULT_DISABLE_RULES）。
DISABLE_RULES = [
    "reduce_tp_in_mesh_forward",
    "reduce_tp_in_mesh_backward",
    "squeeze_1_in_nd_transpose",
    "swap_reshape_transpose_v2",
]



def to_hwc(out, ref):
    """把 RKNN 输出摆成 ref（[H,W,C]）的形状（穷举常见摆法）。"""
    cands = [
        ("raw", lambda o: o),
        ("squeeze+tr", lambda o: np.squeeze(o).transpose(1, 2, 0)),
        ("[0,0]+tr", lambda o: o[0, 0].transpose(1, 2, 0)),
        ("[0,0]", lambda o: o[0, 0]),
        ("[0,:,0]", lambda o: o[0, :, 0]),
    ]
    for name, fn in cands:
        try:
            r = fn(out)
        except Exception:
            continue
        if r.shape == ref.shape:
            return r, name
    return np.squeeze(out), "unmatched"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--onnx", default="onnx/vae512_ps_g2g.onnx")
    ap.add_argument("--dataset", default="calib_real/dataset.txt")
    ap.add_argument("--out", default=None, help="导出 .rknn 的路径")
    ap.add_argument("--quant", default="w8a8")
    ap.add_argument("--algorithm", default="normal")
    ap.add_argument("--method", default="channel")
    ap.add_argument("--auto-hybrid", action="store_true")
    ap.add_argument("--cos-thresh", type=float, default=0.98)
    ap.add_argument("--euc-thresh", type=float, default=None)
    ap.add_argument("--hybrid-level", type=int, default=0)
    ap.add_argument("--input-npy", default="/datasets/tmp/vae512_in.npy")
    ap.add_argument("--ref", default="/datasets/tmp/vae512_ref.npy")
    ap.add_argument("--skip-export", action="store_true")
    ap.add_argument("--opt-level", type=int, default=3)
    ap.add_argument("--single-core", action="store_true")
    ap.add_argument("--memory-plan", default=None)
    ap.add_argument("--compress-weight", action="store_true")
    ap.add_argument("--no-quant", action="store_true",
                    help="build(do_quantization=False)：激活保持 fp16")
    ap.add_argument("--quantize-weight", action="store_true",
                    help="config(quantize_weight=True)：配合 --no-quant 单独量化权重")
    ap.add_argument("--op-target", default=None,
                    help="config(op_target=...)")
    ap.add_argument("--compute-order", default=None,
                    choices=["WidthFirst", "DepthFirst"],
                    help="config(compute_order=...)：图执行拓扑顺序，影响 internal 内存")
    ap.add_argument("--memory-plan-strategy", default=None,
                    choices=["BestFit", "GreedyBySize", "GreedyByLifetime",
                             "SlowPeakGrowth"],
                    help="config(memory_plan_strategy=...)：internal 内存分配方案")
    args = ap.parse_args()

    from rknn.api import RKNN

    x = np.load(args.input_npy).astype(np.float32)
    ref = np.load(args.ref).astype(np.float32)

    print("### CONFIG tag=%s quant=%s algorithm=%s method=%s auto_hybrid=%s "
          "cos=%s euc=%s hybrid_level=%s" %
          (args.tag, args.quant, args.algorithm, args.method,
           int(args.auto_hybrid), args.cos_thresh, args.euc_thresh,
           args.hybrid_level), flush=True)

    rknn = RKNN(verbose=True)
    cfg = dict(
        target_platform="rk3588",
        float_dtype="float16",
        quantized_dtype=args.quant,
        quantized_algorithm=args.algorithm,
        quantized_method=args.method,
        auto_hybrid_cos_thresh=args.cos_thresh,
        disable_rules=DISABLE_RULES,
        quantized_hybrid_level=args.hybrid_level,
        optimization_level=args.opt_level,
    )
    if args.single_core:
        cfg["single_core_mode"] = True
    if args.memory_plan:
        cfg["memory_plan_strategy"] = args.memory_plan
    if args.compress_weight:
        cfg["compress_weight"] = True
    if args.euc_thresh is not None:
        cfg["auto_hybrid_euc_thresh"] = args.euc_thresh
    if args.quantize_weight:
        cfg["quantize_weight"] = True
    if args.op_target:
        cfg["op_target"] = eval(args.op_target)
    if args.compute_order:
        cfg["compute_order"] = args.compute_order
    if args.memory_plan_strategy:
        cfg["memory_plan_strategy"] = args.memory_plan_strategy
    rknn.config(**cfg)
    if rknn.load_onnx(model=args.onnx) != 0:
        print("RESULT tag=%s status=load_onnx_failed" % args.tag)
        return 1

    t0 = time.perf_counter()
    ret = rknn.build(do_quantization=not args.no_quant,
                     dataset=None if args.no_quant else args.dataset,
                     auto_hybrid=args.auto_hybrid)
    build_s = time.perf_counter() - t0
    if ret != 0:
        print("RESULT tag=%s status=build_failed" % args.tag)
        return 1
    print("### BUILD_OK %.1f s" % build_s, flush=True)

    # ---- 模拟器推理 + 对拍 ----
    status = "ok"
    rel = cos = float("nan")
    try:
        if rknn.init_runtime() != 0:
            status = "init_runtime_failed"
        else:
            out = np.asarray(rknn.inference(inputs=[x]))
            o, how = to_hwc(out, ref)
            r = ref.astype(np.float32)
            o = o.astype(np.float32)
            d = np.abs(o - r)
            rel = float(np.linalg.norm(o - r) / (np.linalg.norm(r) + 1e-12))
            cos = float((o.ravel() @ r.ravel()) /
                        (np.linalg.norm(o) * np.linalg.norm(r) + 1e-12))
            print("### ACC how=%s maxd=%.6g meand=%.6g" % (how, d.max(), d.mean()))
            for c in range(min(4, o.shape[-1])):
                print("### CH%d out_mean=%+.4f out_std=%.4f ref_mean=%+.4f ref_std=%.4f"
                      % (c, o[..., c].mean(), o[..., c].std(),
                         r[..., c].mean(), r[..., c].std()))
            np.save("/datasets/tmp/sim_out_%s.npy" % args.tag, o)
    except Exception as e:
        status = "infer_failed:%s" % type(e).__name__
        print("### INFER_ERROR %s" % e)

    if args.out and not args.skip_export:
        outp = args.out
        os.makedirs(os.path.dirname(outp) or ".", exist_ok=True)
        if rknn.export_rknn(outp) != 0:
            print("### EXPORT_FAILED")
        else:
            print("### EXPORT_OK %s %d bytes" % (outp, os.path.getsize(outp)))

    print("RESULT tag=%s status=%s rel_L2=%.6g cosine=%.6f build_s=%.0f" %
          (args.tag, status, rel, cos, build_s), flush=True)

    mem_int = mem_w = 0.0
    try:
        _log = open("logs/build_%s.log" % args.tag, "r", errors="ignore").read()
        _mi = re.search(r"Total Internal Memory Size: ([0-9.e+]+)KB", _log)
        _mw = re.search(r"Total Weight Memory Size: ([0-9.e+]+)KB", _log)
        if _mi:
            mem_int = float(_mi.group(1))
        if _mw:
            mem_w = float(_mw.group(1))
    except Exception:
        pass
    print("### MEM internal=%.0f MiB weight=%.0f MiB total=%.0f MiB"
          % (mem_int / 1024.0, mem_w / 1024.0, (mem_int + mem_w) / 1024.0),
          flush=True)
    summary = dict(tag=args.tag, quant=args.quant, algorithm=args.algorithm,
                   method=args.method, auto_hybrid=args.auto_hybrid,
                   cos_thresh=args.cos_thresh, euc_thresh=args.euc_thresh,
                   hybrid_level=args.hybrid_level, status=status,
                   rel_L2=rel, cosine=cos, build_s=build_s)
    with open("results/sweep_%s.json" % args.tag, "w") as f:
        json.dump(summary, f, indent=1)
    try:
        rknn.release()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
