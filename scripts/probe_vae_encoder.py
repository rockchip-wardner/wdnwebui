#!/usr/bin/env python3
"""板端探针：直接用 ctypes 调 **rknn2 runtime**（librknnrt）跑 VAE 编码器。

======================================================================
干什么用
======================================================================
VAE 编码器（``vae_qwenimage21-512.encoder.3588.rknn``）是刚转出来的，
在接进 cshim 之前先在**板端**独立验一遍：

* 模型能不能在本机 RK3588 的 NPU 上加载、跑通；
* 输入/输出的**张量布局**（几维、什么顺序）—— 这是接 cshim 的关键：
  cshim 喂的是**逻辑 NCHW**，runtime 自己转内部布局；
* 精度：和 119 的 **PyTorch 金标准**（onnxruntime 逐位一致的那份）比。

用法（板端）：
    .venv/bin/python scripts/probe_vae_encoder.py \
        --model /home/rockchip/models/Qwen-Image-2.1/vae_qwenimage21-512.encoder.3588.rknn \
        --input /tmp/vae_enc_in0.npy --golden /tmp/vae_enc_gold0.npy
"""
import argparse
import ctypes
import os
import sys

import numpy as np

# ---------------------------------------------------------------------------
# rknn2 runtime 的 ctypes 绑定（结构体布局照抄 include/rknn_api.h）
# ---------------------------------------------------------------------------
RKNN_MAX_DIMS = 16
RKNN_MAX_NAME_LEN = 256

RKNN_TENSOR_FLOAT32 = 0
RKNN_TENSOR_FLOAT16 = 1
RKNN_TENSOR_INT8 = 2
RKNN_TENSOR_UINT8 = 3
RKNN_TENSOR_INT16 = 4
RKNN_TENSOR_UINT16 = 5
RKNN_TENSOR_INT32 = 6
RKNN_TENSOR_UINT32 = 7
RKNN_TENSOR_INT64 = 8
RKNN_TENSOR_UINT64 = 9
RKNN_TENSOR_BOOL = 10

RKNN_TENSOR_NCHW = 0
RKNN_TENSOR_NHWC = 1
RKNN_TENSOR_NC1HWC2 = 2

RKNN_QUERY_IN_OUT_NUM = 0
RKNN_QUERY_INPUT_ATTR = 1
RKNN_QUERY_OUTPUT_ATTR = 2
RKNN_QUERY_PERF_DETAIL = 4
RKNN_QUERY_DEVICE_MEMORY = 7

RKNN_NPU_CORE_0_1_2 = 7


class RknnTensorAttr(ctypes.Structure):
    _fields_ = [
        ("index", ctypes.c_uint32),
        ("n_dims", ctypes.c_uint32),
        ("dims", ctypes.c_uint32 * RKNN_MAX_DIMS),
        ("name", ctypes.c_char * RKNN_MAX_NAME_LEN),
        ("n_elems", ctypes.c_uint32),
        ("size", ctypes.c_uint32),
        ("fmt", ctypes.c_int),
        ("type", ctypes.c_int),
        ("qnt_type", ctypes.c_int),
        ("fl", ctypes.c_int8),
        ("zp", ctypes.c_int32),
        ("scale", ctypes.c_float),
        ("w_stride", ctypes.c_uint32),
        ("size_with_stride", ctypes.c_uint32),
        ("pass_through", ctypes.c_uint8),
        ("elem_offset", ctypes.c_uint32),
    ]


class RknnInputOutputNum(ctypes.Structure):
    _fields_ = [("n_input", ctypes.c_uint32), ("n_output", ctypes.c_uint32)]


class RknnInput(ctypes.Structure):
    _fields_ = [
        ("index", ctypes.c_uint32),
        ("buf", ctypes.c_void_p),
        ("size", ctypes.c_uint32),
        ("pass_through", ctypes.c_uint8),
        ("type", ctypes.c_int),
        ("fmt", ctypes.c_int),
        ("enable_cache", ctypes.c_uint8),
    ]


class RknnOutput(ctypes.Structure):
    _fields_ = [
        ("want_float", ctypes.c_uint8),
        ("is_prealloc", ctypes.c_uint8),
        ("index", ctypes.c_uint32),
        ("buf", ctypes.c_void_p),
        ("size", ctypes.c_uint32),
    ]


class RknnInitExtend(ctypes.Structure):
    _fields_ = [
        ("ctx", ctypes.c_uint64),
        ("real_model_offset", ctypes.c_int32),
        ("real_model_size", ctypes.c_uint32),
        ("model_buffer_fd", ctypes.c_int32),
        ("model_buffer_flags", ctypes.c_uint32),
        ("user_key", ctypes.c_void_p),
        ("reserved", ctypes.c_uint8 * 112),
    ]


def load_lib():
    for name in ("librknnrt.so.2.5.0", "librknnrt.so.2", "librknnrt.so",
                 "librknnrt.so.1"):
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    raise RuntimeError("找不到 librknnrt（rknn2-runtime 没装？）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--input", required=True, help="输入 .npy（fp32）")
    ap.add_argument("--golden", default="", help="PyTorch 金标准 .npy")
    ap.add_argument("--core-mask", type=int, default=RKNN_NPU_CORE_0_1_2)
    args = ap.parse_args()

    lib = load_lib()
    lib.rknn_init.argtypes = [ctypes.POINTER(ctypes.c_uint64), ctypes.c_void_p,
                               ctypes.c_uint32, ctypes.c_uint32,
                               ctypes.POINTER(RknnInitExtend)]
    lib.rknn_query.argtypes = [ctypes.c_uint64, ctypes.c_uint32,
                               ctypes.c_void_p, ctypes.c_uint32]
    lib.rknn_set_core_mask.argtypes = [ctypes.c_uint64, ctypes.c_int]
    lib.rknn_inputs_set.argtypes = [ctypes.c_uint64, ctypes.c_uint32,
                                     ctypes.POINTER(RknnInput)]
    lib.rknn_run.argtypes = [ctypes.c_uint64, ctypes.c_void_p]
    lib.rknn_outputs_get.argtypes = [ctypes.c_uint64, ctypes.c_uint32,
                                     ctypes.POINTER(RknnOutput), ctypes.c_void_p]
    lib.rknn_outputs_release.argtypes = [ctypes.c_uint64, ctypes.c_uint32,
                                         ctypes.POINTER(RknnOutput)]
    lib.rknn_destroy.argtypes = [ctypes.c_uint64]

    ctx = ctypes.c_uint64(0)
    ext = RknnInitExtend()
    ret = lib.rknn_init(ctypes.byref(ctx), args.model.encode(), 0, 0,
                        ctypes.byref(ext))
    print("rknn_init ret =", ret, flush=True)
    if ret != 0:
        return 1
    try:
        print("set_core_mask ret =",
              lib.rknn_set_core_mask(ctx, args.core_mask), flush=True)

        io = RknnInputOutputNum()
        assert lib.rknn_query(ctx, RKNN_QUERY_IN_OUT_NUM,
                              ctypes.byref(io), ctypes.sizeof(io)) == 0
        print(f"输入 {io.n_input} 个 / 输出 {io.n_output} 个", flush=True)

        attrs = []
        for kind, n in ((RKNN_QUERY_INPUT_ATTR, io.n_input),
                        (RKNN_QUERY_OUTPUT_ATTR, io.n_output)):
            for i in range(n):
                a = RknnTensorAttr()
                a.index = i
                if lib.rknn_query(ctx, kind, ctypes.byref(a),
                                  ctypes.sizeof(a)) != 0:
                    continue
                dims = [a.dims[d] for d in range(min(a.n_dims, 8))]
                print(f"  {'in ' if kind == RKNN_QUERY_INPUT_ATTR else 'out'} "
                      f"#{i} {a.name.decode()} dims={dims} fmt={a.fmt} "
                      f"type={a.type} n_elems={a.n_elems} size={a.size}",
                      flush=True)
                attrs.append(a)

        # ---- 跑 ----
        x = np.load(args.input).astype(np.float32)
        print(f"输入张量 shape={x.shape} range=[{x.min():.3f},{x.max():.3f}]",
              flush=True)
        buf = x.ctypes.data_as(ctypes.c_void_p)
        rin = RknnInput()
        rin.index = 0
        rin.buf = buf
        rin.size = x.nbytes
        rin.pass_through = 0
        rin.type = RKNN_TENSOR_FLOAT32
        rin.fmt = RKNN_TENSOR_NCHW
        rin.enable_cache = 0
        ret = lib.rknn_inputs_set(ctx, 1, ctypes.byref(rin))
        print("rknn_inputs_set ret =", ret, flush=True)
        if ret != 0:
            return 2
        ret = lib.rknn_run(ctx, None)
        print("rknn_run ret =", ret, flush=True)
        if ret != 0:
            return 3

        outs = (RknnOutput * io.n_output)()
        for i in range(io.n_output):
            outs[i].want_float = 1      # 让 runtime 反量化成 fp32
            outs[i].is_prealloc = 0
            outs[i].index = i
        ret = lib.rknn_outputs_get(ctx, io.n_output, outs, None)
        print("rknn_outputs_get ret =", ret, flush=True)
        if ret != 0:
            return 4

        out = np.ctypeslib.as_array(ctypes.cast(outs[0].buf, ctypes.POINTER(
            ctypes.c_float)), shape=(outs[0].size // 4,)).copy()
        print(f"输出 shape={out.shape} mean=%.4f std=%.4f min=%.3f max=%.3f"
              % (out.mean(), out.std(), out.min(), out.max()), flush=True)

        if args.golden and os.path.exists(args.golden):
            gold = np.load(args.golden).astype(np.float32).reshape(-1)
            if gold.size == out.size:
                rel = float(np.linalg.norm(out - gold) /
                            (np.linalg.norm(gold) + 1e-9))
                print("✅ 与 PyTorch 金标准: rel_L2=%.6f  max_abs=%.4f"
                      % (rel, float(np.abs(out - gold).max())), flush=True)
            else:
                print(f"⚠️ 尺寸对不上：板端 {out.size} vs 金标准 {gold.size}",
                      flush=True)
        lib.rknn_outputs_release(ctx, io.n_output, outs)
        return 0
    finally:
        lib.rknn_destroy(ctx)


if __name__ == "__main__":
    sys.exit(main())
