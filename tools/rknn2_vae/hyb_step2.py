#!/usr/bin/env python3
"""混合量化第 ② 步：把改好的量化配置编译成 .rknn，并在模拟器里对拍。

文档（User_Guide 6.3.2 第 3 步 + API_Reference 2.12.2 + 官方 example step2.py）：

    rknn.hybrid_quantization_step2(model_input='<model_name>.model',
                                   data_input='<model_name>.data',
                                   model_quantization_cfg='<model_name>.quantization.cfg')
    rknn.export_rknn('out.rknn')

**注意 step2 之后不要再调 rknn.build()** —— 官方 example 里没有这一步，
step2 自己就完成了量化图的编译。之前踩过：step1/step2 之后又在同一个对象上
build() ⇒ 报 "The 'latents__cvt_float32_to_int8' already in self.nodes"。

用法:
    python hyb_step2.py --tag c2h --model hybwork/c2/vae512_c2.model \
        --data hybwork/c2/vae512_c2.data \
        --cfg hybwork/c2/vae512_c2.quantization.cfg \
        --out rknn4/c2_hyb.rknn
"""
import argparse
import glob
import json
import os
import re
import sys
import time

import numpy as np

DISABLE_RULES = [
    "reduce_tp_in_mesh_forward",
    "reduce_tp_in_mesh_backward",
    "squeeze_1_in_nd_transpose",
    "swap_reshape_to_transpose_v2",
]


def to_hwc(out, ref):
    """把 RKNN 输出摆成 ref（[H,W,C]）的形状（穷举常见摆法）。"""
    cands = [
        ("raw", lambda o: o),
        ("squeeze+tr", lambda o: np.squeeze(o).transpose(1, 2, 0)),
        ("[0,0]+tr", lambda o: o[0, 0].transpose(1, 2, 0)),
        ("[0,0]", lambda o: o[0, 0]),
        ("[0,:,0]", lambda o: o[0, :, 0]),
        ("nchw->hwc", lambda o: o[0].transpose(1, 2, 0)),
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
    ap.add_argument("--model", required=True, help="step1 生成的 <name>.model")
    ap.add_argument("--data", required=True, help="step1 生成的 <name>.data")
    ap.add_argument("--cfg", required=True, help="改好的 <name>.quantization.cfg")
    ap.add_argument("--out", default=None)
    ap.add_argument("--input-npy", default="/datasets/tmp/vae512_in_c2.npy")
    ap.add_argument("--ref", default="/datasets/tmp/vae512_ref.npy")
    ap.add_argument("--algorithm", default="normal")
    ap.add_argument("--method", default="channel")
    ap.add_argument("--opt-level", type=int, default=3)
    ap.add_argument("--log", default=None, help="把构建日志写到哪（默认 logs/）")
    ap.add_argument("--with-config", action="store_true",
                    help="step2 前也调 config()（官方 example 不调；仅在 step2 报"
                         "「缺 target_platform」之类时才需要）")
    args = ap.parse_args()

    from rknn.api import RKNN

    x = np.load(args.input_npy).astype(np.float32)
    ref = np.load(args.ref).astype(np.float32)

    rknn = RKNN(verbose=True)
    # 官方 example（examples/functions/hybrid_quant/step2.py）**不调 config()**：
    # target_platform / quantized_dtype 等信息已经写在 step1 产出的 .model 里。
    if args.with_config:
        rknn.config(
            target_platform="rk3588",
            float_dtype="float16",
            quantized_dtype="w8a8",
            quantized_algorithm=args.algorithm,
            quantized_method=args.method,
            disable_rules=DISABLE_RULES,
            optimization_level=args.opt_level,
        )

    t0 = time.perf_counter()
    ret = rknn.hybrid_quantization_step2(
        model_input=os.path.abspath(args.model),
        data_input=os.path.abspath(args.data),
        model_quantization_cfg=os.path.abspath(args.cfg),
    )
    step2_s = time.perf_counter() - t0
    print("### STEP2 ret=%s %.1f s" % (ret, step2_s), flush=True)
    if ret != 0:
        print("RESULT tag=%s status=step2_failed" % args.tag, flush=True)
        return 1

    status, rel, cos = "ok", float("nan"), float("nan")
    try:
        if rknn.init_runtime() != 0:
            status = "init_runtime_failed"
        else:
            kw = {"data_format": "nchw"} if x.ndim == 4 else {}
            out = np.asarray(rknn.inference(inputs=[x], **kw))
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
        print("### INFER_ERROR %s" % e, flush=True)

    if args.out:
        outp = os.path.abspath(args.out)
        os.makedirs(os.path.dirname(outp) or ".", exist_ok=True)
        t1 = time.perf_counter()
        if rknn.export_rknn(outp) != 0:
            print("### EXPORT_FAILED", flush=True)
        else:
            print("### EXPORT_OK %s %d bytes (%.1f s)" %
                  (outp, os.path.getsize(outp), time.perf_counter() - t1), flush=True)

    print("RESULT tag=%s status=%s rel_L2=%.6g cosine=%.6f step2_s=%.0f" %
          (args.tag, status, rel, cos, step2_s), flush=True)
    os.makedirs("results", exist_ok=True)
    with open("results/hybrid_%s.json" % args.tag, "w") as f:
        json.dump(dict(tag=args.tag, status=status, rel_L2=rel, cosine=cos,
                       step2_s=step2_s), f, indent=1)
    try:
        rknn.release()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
