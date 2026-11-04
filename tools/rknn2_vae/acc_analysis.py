#!/usr/bin/env python3
"""用 rknn-toolkit2 文档里的 `accuracy_analysis` 做**逐层**精度分析。

文档（site-packages/rknn/api/rknn.py）：

    accuracy_analysis(inputs, output_dir='./snapshot', target=None, device_id=None)
        generate inference snapshots (fp32 && qnt) and calculate quantize error.
        :param inputs: The path list of image (jpg/png/bmp/npy).

它会为**每一层**产出 fp32 与 qnt 两份快照，并算出量化误差 ⇒
**直接回答「哪几层量化误差大」**，也就是混合量化该把哪些层留在 fp16。

用法:
    acc_analysis.py --tag acc1 --algorithm kl_divergence [--target rk3588]
"""
import argparse
import glob
import json
import os
import sys


DISABLE_RULES = [
    "reduce_tp_in_mesh_forward",
    "reduce_tp_in_mesh_backward",
    "squeeze_1_in_nd_transpose",
    "swap_reshape_transpose_v2",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--onnx", default="onnx/vae512_ps_g2g.onnx")
    ap.add_argument("--dataset", default="calib_real/dataset.txt")
    ap.add_argument("--algorithm", default="kl_divergence")
    ap.add_argument("--method", default="channel")
    ap.add_argument("--target", default=None)
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--inputs", nargs="*",
                    default=["/datasets/tmp/vae512_in.npy"])
    args = ap.parse_args()

    from rknn.api import RKNN

    outdir = args.outdir or ("acc_%s" % args.tag)
    os.makedirs(outdir, exist_ok=True)

    rknn = RKNN(verbose=True)
    rknn.config(target_platform="rk3588", float_dtype="float16",
                quantized_dtype="w8a8", quantized_algorithm=args.algorithm,
                quantized_method=args.method, disable_rules=DISABLE_RULES)
    if rknn.load_onnx(model=args.onnx) != 0:
        print("RESULT tag=%s status=load_onnx_failed" % args.tag)
        return 1
    if rknn.build(do_quantization=True, dataset=args.dataset) != 0:
        print("RESULT tag=%s status=build_failed" % args.tag)
        return 1
    print("### BUILD_OK, 开始 accuracy_analysis", flush=True)

    try:
        rknn.accuracy_analysis(inputs=args.inputs, output_dir=outdir,
                               target=args.target)
        print("### ACC_ANALYSIS_OK")
    except Exception as e:
        print("### ACC_ANALYSIS_ERROR %s: %s" % (type(e).__name__, e))

    files = sorted(glob.glob(os.path.join(outdir, "**", "*"), recursive=True))
    print("### OUT_FILES %d" % len(files))
    for f in files[:80]:
        try:
            print("###   %s (%d B)" % (f, os.path.getsize(f)))
        except OSError:
            print("###   %s" % f)
    print("RESULT tag=%s status=acc_done outdir=%s" % (args.tag, outdir))
    try:
        rknn.release()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
