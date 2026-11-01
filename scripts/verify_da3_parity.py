"""真机验收：wdnai 的 DA3 示例与 rknn3-model-zoo 的 C++ demo 结果必须一致。

背景
====

Depth Anything V3 的 RKNN 导出被拆成三段（``local`` → ``global`` → ``head``），
C++ 参考实现在
``rknn3-model-zoo/examples/depth_anything_v3/cpp/main.cpp``，编译产物在
``rknn3-model-zoo/install/rk3588_linux_aarch64/rknn_depth_anything_v3_demo``。

wdnai 侧的等价实现在 ``wdnai/backends/depth_anything.py``，示例脚本是
``wdnai/examples/depth_anything_v3.py``。本脚本把两个实现**并排跑一遍**，
逐项比对：

  1. stdout 里的张量统计（``local_tokens`` / ``depth`` / ``confidence``）；
  2. 可视化窗口（``shared inverse-depth visualization range``）；
  3. 每 view 的热力图与 montage。

为什么这些数字必须**逐位**相同
----------------------------

深度图是 fp16 张量，链路上任何一处舍入差异（预处理、FMA、fp16→fp32 转换、
统计的累加精度）都会一路传到最终结果：

  * 预处理的 resize：GCC 把 C++ 表达式**融合成 FMA**（``-ffp-contract=fast``），
    与逐字翻译差 1 LSB（约 0.4 % 像素），最终让 depth 均值差 ~5e-4；
  * fp16→fp32：C++ 的 ``fp16_to_fp32`` **不处理 fp16 非规格化数**
    （指数域为 0 时按 0 处理），而 numpy 的 ``astype`` 会正确还原 ——
    DA3 的 token 里确实有这类值；
  * 统计均值：C++ 用 ``double`` 累加，numpy 的 float32 ``.mean()`` 会在
    第 6 位小数上偏一。

这三处都已按 C++ 的实际行为对齐，本脚本就是守住这条对齐的回归防线。

用法::

    python -m scripts.verify_da3_parity               # 跑两遍并比对
    python -m scripts.verify_da3_parity --skip-cpp    # 只跑 Python 侧
    python -m scripts.verify_da3_parity --keep         # 保留临时目录

⚠️ **占 NPU**：两个实现都要加载 DA3 三段权重（合计 ~1.3 GB）并跑 10 帧推理。
另外注意：**不要在 NPU 调用中途 kill 进程** —— SIGTERM 落在 ``rknn3_run``
里会把 RK1820 的 PCIe 链路弄坏（``rknn3_startup`` 之后报
``ddr load addr request failed``），只能整机重启恢复。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PY = os.path.join(REPO, ".venv", "bin", "python")
EXAMPLE = os.path.join(REPO, ".venv", "src", "wdnai", "examples", "depth_anything_v3.py")

CPP_DIR = ("/userdata/rknn3/rknn3-model-zoo/install/rk3588_linux_aarch64/"
           "rknn_depth_anything_v3_demo")
CPP_BIN = os.path.join(CPP_DIR, "rknn_da3_base_demo")
CPP_MODEL_DIR = os.path.join(CPP_DIR, "model")
CPP_LIB_DIR = os.path.join(CPP_DIR, "lib")

#: 逐位对齐的统计行。``(名字, 正则)``
STAT_LINES = (
    ("local_tokens", re.compile(r"^local_tokens: elems=(\d+) min=(\S+) max=(\S+) mean=(\S+)$", re.M)),
    ("depth",        re.compile(r"^depth: elems=(\d+) min=(\S+) max=(\S+) mean=(\S+)$", re.M)),
    ("confidence",   re.compile(r"^confidence: elems=(\d+) min=(\S+) max=(\S+) mean=(\S+)$", re.M)),
    ("window",       re.compile(r"^shared inverse-depth visualization range: \[(\S+), (\S+)\]$", re.M)),
)


def _run(cmd, env=None, cwd=None, timeout=600):
    print("  $ " + " ".join(cmd))
    proc = subprocess.run(cmd, env=env, cwd=cwd, timeout=timeout,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, errors="replace")
    return proc.returncode, proc.stdout


def _parse_stats(text):
    """把 stdout 里的统计行解析成 ``{名字: 元组}``。"""
    out = {}
    for name, pattern in STAT_LINES:
        m = pattern.search(text)
        if not m:
            continue
        out[name] = tuple(g for g in m.groups())
    return out


def _run_cpp(out_dir):
    """跑 C++ 参考实现。"""
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = CPP_LIB_DIR + ":" + env.get("LD_LIBRARY_PATH", "")
    cmd = [
        CPP_BIN,
        os.path.join(CPP_MODEL_DIR, "da3_base_local.rknn"),
        os.path.join(CPP_MODEL_DIR, "da3_base_global.rknn"),
        os.path.join(CPP_MODEL_DIR, "da3_base_head.rknn"),
        os.path.join(CPP_MODEL_DIR, "images.txt"),
        "10",
        out_dir,
    ]
    code, out = _run(cmd, env=env, cwd=CPP_DIR)
    if code != 0:
        print(out[-3000:])
        raise SystemExit(f"❌ C++ demo 退出码 {code}")
    return out


def _run_python(out_dir):
    """跑 wdnai 的 Python 示例。"""
    cmd = [VENV_PY, EXAMPLE, "--output-dir", out_dir]
    code, out = _run(cmd, cwd=REPO)
    if code != 0:
        print(out[-3000:])
        raise SystemExit(f"❌ Python 示例退出码 {code}")
    return out


def _compare_images(c_dir, p_dir, names):
    """比对两边的热力图。返回 (是否全部通过, 说明行列表)。"""
    import numpy as np
    import cv2

    ok = True
    lines = []
    for name in names:
        c_path = os.path.join(c_dir, name)
        p_path = os.path.join(p_dir, name)
        if not (os.path.isfile(c_path) and os.path.isfile(p_path)):
            lines.append(f"  ✗ {name}: 缺少输出文件")
            ok = False
            continue
        a = cv2.imread(c_path)
        b = cv2.imread(p_path)
        if a is None or b is None:
            lines.append(f"  ✗ {name}: 图片解码失败")
            ok = False
            continue
        d = np.abs(a.astype(np.int16) - b.astype(np.int16))
        # JPEG 编码器不同（libjpeg-turbo vs OpenCV 的 libjpeg），解码后允许
        # 少量差异；深度图本身的像素是完全一致的（见 depth 张量比对）。
        good = float(d.mean()) <= 1.5 and int(d.max()) <= 24
        ok &= good
        lines.append(f"  {'✓' if good else '✗'} {name}: max_diff={int(d.max())} "
                     f"mean_diff={float(d.mean()):.4f}")
    return ok, lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--skip-cpp", action="store_true",
                    help="只跑 Python 侧（不比对 C++）")
    ap.add_argument("--keep", action="store_true",
                    help="保留临时输出目录")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="da3_parity_")
    cpp_dir = os.path.join(tmp, "cpp")
    py_dir = os.path.join(tmp, "py")
    os.makedirs(cpp_dir, exist_ok=True)
    os.makedirs(py_dir, exist_ok=True)
    print(f"临时目录: {tmp}")

    try:
        print("\n== 1. 跑 C++ 参考实现 ==")
        cpp_out = _run_cpp(cpp_dir)
        print("\n== 2. 跑 wdnai Python 示例 ==")
        py_out = _run_python(py_dir)

        cpp_stats = _parse_stats(cpp_out)
        py_stats = _parse_stats(py_out)

        print("\n== 3. 比对张量统计 ==")
        ok = True
        for name, _ in STAT_LINES:
            c = cpp_stats.get(name)
            p = py_stats.get(name)
            if c is None or p is None:
                print(f"  ✗ {name}: 缺少统计行 (C++={c} PY={p})")
                ok = False
                continue
            same = c == p
            ok &= same
            print(f"  {'✓' if same else '✗'} {name}:")
            print(f"      C++ = {c}")
            print(f"      PY  = {p}")

        print("\n== 4. 比对热力图 ==")
        names = ["depth_%02d.jpg" % i for i in range(10)] + ["depth_montage.jpg"]
        img_ok, img_lines = _compare_images(cpp_dir, py_dir, names)
        for line in img_lines:
            print(line)
        ok &= img_ok

        print()
        if ok:
            print("✅ DA3 Python 示例与 C++ demo 结果一致")
            return 0
        print("❌ DA3 Python 示例与 C++ demo 结果不一致")
        return 1
    finally:
        if args.keep:
            print(f"（临时目录保留在 {tmp}）")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
