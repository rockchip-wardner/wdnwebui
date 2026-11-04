#!/usr/bin/env python3
"""混合量化第 ① 步：按 rknn-toolkit2 文档生成临时模型 / 数据 / 量化配置。

文档（User_Guide 6.3.2「混合量化使用流程」第 1 步 + API_Reference 2.12.1）：

    rknn.config(target_platform=..., quantized_dtype='w8a8', ...)
    rknn.load_onnx(model='xxx.onnx')
    rknn.hybrid_quantization_step1(dataset='dataset.txt', proposal=False)

产物落在**当前工作目录**：
    <model_name>.model              临时模型（优化后的图）
    <model_name>.data               校正数据
    <model_name>.quantization.cfg   量化配置（要手改 custom_quantize_layers）

与官方 example 的差别：
  * 官方用 proposal=False，本脚本同样 —— proposal=True 在 2.5.0 上会崩
    （KeyError: 'add_2-rs'，见 README「混合量化」一节）。
  * 官方第 2 步是**另一个进程**里用全新 RKNN 对象调 step2 —— 本脚本同此，
    所以本脚本跑完就退出，把 .model/.data/.cfg 留给 hyb_step2.py。

用法:
    python hyb_step1.py --tag c2 --onnx onnx/vae512_c2.onnx \
        --dataset calib_c2/dataset.txt --workdir hybwork/c2
"""
import argparse
import glob
import json
import os
import shutil
import sys
import time

#: rknn-toolkit2 2.5.0 在这张图上会崩的优化规则（工具链自己给出的绕过方式）。
DISABLE_RULES = [
    "reduce_tp_in_mesh_forward",
    "reduce_tp_in_mesh_backward",
    "squeeze_1_in_nd_transpose",
    "swap_reshape_to_transpose_v2",
]


def snap(d):
    out = {}
    for f in sorted(glob.glob(os.path.join(d, "*"))):
        try:
            out[os.path.basename(f)] = os.path.getsize(f)
        except OSError:
            pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--onnx", required=True)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--algorithm", default="normal")
    ap.add_argument("--method", default="channel")
    ap.add_argument("--opt-level", type=int, default=3)
    ap.add_argument("--proposal", action="store_true",
                    help="打开 proposal（2.5.0 上已知会崩）")
    args = ap.parse_args()

    from rknn.api import RKNN

    onnx = os.path.abspath(args.onnx)
    dataset = os.path.abspath(args.dataset)
    workdir = os.path.abspath(args.workdir)
    if os.path.isdir(workdir):
        shutil.rmtree(workdir)
    os.makedirs(workdir, exist_ok=True)

    rknn = RKNN(verbose=True)
    rknn.config(
        target_platform="rk3588",
        float_dtype="float16",
        quantized_dtype="w8a8",
        quantized_algorithm=args.algorithm,
        quantized_method=args.method,
        disable_rules=DISABLE_RULES,
        optimization_level=args.opt_level,
    )
    if rknn.load_onnx(model=onnx) != 0:
        print("RESULT tag=%s status=load_onnx_failed" % args.tag, flush=True)
        return 1
    print("### LOAD_OK %s" % onnx, flush=True)

    # step1 把产物写在**进程当前目录**下 —— 必须先 chdir 到 workdir，
    # 否则会把 .model/.data/.cfg 落在跑脚本的目录里、跟别的实验互相覆盖。
    os.chdir(workdir)
    before = snap(workdir)
    t0 = time.perf_counter()
    try:
        ret = rknn.hybrid_quantization_step1(
            dataset=dataset,
            proposal=args.proposal,
            proposal_dataset_size=1,
        )
        print("### STEP1 ret=%s %.1f s" % (ret, time.perf_counter() - t0), flush=True)
        if ret != 0:
            print("RESULT tag=%s status=step1_failed" % args.tag, flush=True)
            return 1
    except Exception as e:
        print("### STEP1_ERROR %s: %s" % (type(e).__name__, e), flush=True)
        print("RESULT tag=%s status=step1_exception" % args.tag, flush=True)
        return 1

    after = snap(workdir)
    new = {k: v for k, v in after.items() if k not in before}
    print("### STEP1_NEW_FILES %s" % json.dumps(new, indent=1), flush=True)

    model = sorted(glob.glob(os.path.join(workdir, "*.model")))
    data = sorted(glob.glob(os.path.join(workdir, "*.data")))
    cfgs = sorted(glob.glob(os.path.join(workdir, "*.quantization.cfg")))
    print("### MODEL %s" % model, flush=True)
    print("### DATA  %s" % data, flush=True)
    print("### CFG   %s" % cfgs, flush=True)

    if cfgs:
        with open(cfgs[0]) as f:
            head = f.read(6000)
        print("### CFG_HEAD_BEGIN")
        print(head)
        print("### CFG_HEAD_END", flush=True)

    try:
        rknn.release()
    except Exception:
        pass
    print("RESULT tag=%s status=step1_ok" % args.tag, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
