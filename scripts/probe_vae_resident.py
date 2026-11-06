#!/usr/bin/env python3
"""板端探针：VAE **解码器 + 编码器**能不能**同时**常驻在 3588 的 NPU 上。

======================================================================
为什么必须先验这个
======================================================================
出图链路的现状是：**VAE 解码器在 open 阶段就常驻**（4096 MiB 的 NPU 池里
占了 2311 MiB：internal 1825 + weight 487）。图生图要再加一个**编码器**
（205 MB 的 .rknn）。

如果两者加起来超过 NPU 池 ⇒ open 阶段就装不下 ⇒ 必须改成「**用完就换**」
（编码器用完释放，解码器再装回来），那是另一套设计。

所以先实测：**两个模型同时 init 会不会失败**。

用法（板端）：
    .venv/bin/python scripts/probe_vae_resident.py
"""
import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from probe_vae_encoder import (  # noqa: E402
    RKNN_NPU_CORE_0_1_2, RknnInitExtend, RknnInputOutputNum, RknnTensorAttr,
    load_lib,
)

MODEL_DIR = "/home/rockchip/models/Qwen-Image-2.1"
DECODER = os.path.join(MODEL_DIR, "vae_qwenimage21-512.3588.rknn")
ENCODER = os.path.join(MODEL_DIR, "vae_qwenimage21-512.encoder.3588.rknn")

RKNN_QUERY_DEVICE_MEMORY = 7


class RknnDeviceMemory(ctypes.Structure):
    _fields_ = [("id", ctypes.c_uint32),
                ("memory_total", ctypes.c_uint64),
                ("memory_used", ctypes.c_uint64)]


def init_model(lib, path, core_mask):
    ctx = ctypes.c_uint64(0)
    ext = RknnInitExtend()
    ret = lib.rknn_init(ctypes.byref(ctx), path.encode(), 0, 0,
                        ctypes.byref(ext))
    if ret != 0:
        return 0, ret
    lib.rknn_set_core_mask(ctx, core_mask)
    return ctx, 0


def query_mem(lib, ctx):
    try:
        mem = RknnDeviceMemory()
        ret = lib.rknn_query(ctx, RKNN_QUERY_DEVICE_MEMORY,
                             ctypes.byref(mem), ctypes.sizeof(mem))
        if ret == 0:
            return mem.memory_used, mem.memory_total
    except Exception:  # noqa: BLE001
        pass
    return None, None


def main() -> int:
    lib = load_lib()
    lib.rknn_init.argtypes = [ctypes.POINTER(ctypes.c_uint64), ctypes.c_void_p,
                               ctypes.c_uint32, ctypes.c_uint32,
                               ctypes.POINTER(RknnInitExtend)]
    lib.rknn_query.argtypes = [ctypes.c_uint64, ctypes.c_uint32,
                               ctypes.c_void_p, ctypes.c_uint32]
    lib.rknn_set_core_mask.argtypes = [ctypes.c_uint64, ctypes.c_int]
    lib.rknn_destroy.argtypes = [ctypes.c_uint64]

    used0, total0 = query_mem(lib, ctypes.c_uint64(0))
    print("=== 起手：设备内存 ===")
    print("  用量:", used0, "/", total0)

    print("\n=== ① 装解码器（现状：常驻）===")
    dctx, ret = init_model(lib, DECODER, RKNN_NPU_CORE_0_1_2)
    print("  init ret =", ret)
    if ret == 0:
        u, t = query_mem(lib, dctx)
        print("  设备内存 用量/总量:", u, "/", t)
        io = RknnInputOutputNum()
        lib.rknn_query(dctx, 0, ctypes.byref(io), ctypes.sizeof(io))
        print("  输入/输出数:", io.n_input, io.n_output)
        for i in range(io.n_output):
            a = RknnTensorAttr()
            a.index = i
            if lib.rknn_query(dctx, 2, ctypes.byref(a), ctypes.sizeof(a)) == 0:
                dims = [a.dims[d] for d in range(min(a.n_dims, 6))]
                print(f"    out#{i} {a.name.decode()} dims={dims} type={a.type}")

    print("\n=== ② 在解码器**常驻**的前提下装编码器 ===")
    ectx, ret = init_model(lib, ENCODER, RKNN_NPU_CORE_0_1_2)
    print("  init ret =", ret)
    if ret == 0:
        u, t = query_mem(lib, ectx)
        print("  设备内存 用量/总量:", u, "/", t)
        io = RknnInputOutputNum()
        lib.rknn_query(ectx, 0, ctypes.byref(io), ctypes.sizeof(io))
        for i in range(io.n_input):
            a = RknnTensorAttr()
            a.index = i
            if lib.rknn_query(ectx, 1, ctypes.byref(a), ctypes.sizeof(a)) == 0:
                dims = [a.dims[d] for d in range(min(a.n_dims, 6))]
                print(f"    in#{i} {a.name.decode()} dims={dims} type={a.type}")
        for i in range(io.n_output):
            a = RknnTensorAttr()
            a.index = i
            if lib.rknn_query(ectx, 2, ctypes.byref(a), ctypes.sizeof(a)) == 0:
                dims = [a.dims[d] for d in range(min(a.n_dims, 6))]
                print(f"    out#{i} {a.name.decode()} dims={dims} type={a.type}")
        print("  ✅ 两个模型**同时常驻**成功")
        lib.rknn_destroy(ectx)
    else:
        print("  ❌ 编码器装不下（与解码器冲突）⇒ 必须改成「用完就换」")
    if dctx:
        lib.rknn_destroy(dctx)
    return 0


if __name__ == "__main__":
    sys.exit(main())
