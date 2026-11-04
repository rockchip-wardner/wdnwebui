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

⚠️ **占 NPU**：两个实现都要加载 DA3 三段权重（合计 ~1.3 GB）并跑 N 帧推理
（默认 **1 帧**，即当前部署的 1-view / 280×280 导出；``--views N`` 可切回
多视图导出）。
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

#: 导出时烤进去的 view 数。**当前部署的导出是 1-view（280×280）** ——
#: ``global`` 段输入 ``[1,401,1,768]``、``head`` 段输出 ``[1,1,280,280]``。
#: C++ demo 的 ``argv[5]`` 与 Python 侧的图片张数都必须等于它。
DEFAULT_VIEWS = 1

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


def _run_cpp(out_dir, views=DEFAULT_VIEWS):
    """跑 C++ 参考实现。

    C++ 侧刻意用 **model-zoo 里原始命名的** ``da3_base_*.rknn``：
    C++ demo 的 ``make_weight_path`` 只做「``.rknn`` → ``.weight``」，
    读不了部署侧的新命名（``....1828.rknn`` 配 ``....weight``）。
    两边是**同一份权重**（md5 逐个核对过），比的是实现，不是权重。
    """
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = CPP_LIB_DIR + ":" + env.get("LD_LIBRARY_PATH", "")
    cmd = [
        CPP_BIN,
        os.path.join(CPP_MODEL_DIR, "da3_base_local.rknn"),
        os.path.join(CPP_MODEL_DIR, "da3_base_global.rknn"),
        os.path.join(CPP_MODEL_DIR, "da3_base_head.rknn"),
        os.path.join(CPP_MODEL_DIR, "images.txt"),
        str(views),
        out_dir,
    ]
    code, out = _run(cmd, env=env, cwd=CPP_DIR)
    if code != 0:
        print(out[-3000:])
        raise SystemExit(f"❌ C++ demo 退出码 {code}")
    return out


def _run_python(out_dir, views=DEFAULT_VIEWS, list_path=None):
    """跑 wdnai 的 Python 示例。

    Python 侧加载的是**部署侧的新命名**（``/userdata/models/Depth-Anything-V3/``
    下的 ``*_DepthAnythingV3.280x280.1view.1828.rknn``），也就是改名后的
    真实产物。

    图片清单**显式给**（与 C++ 侧同一份文件），保证两边吃的是同一批素材，
    不依赖 sample 自带的默认清单。
    """
    cmd = [VENV_PY, EXAMPLE, "--output-dir", out_dir]
    if list_path:
        cmd += ["--image-list", list_path]
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


def _write_image_list(path, views):
    """从 model-zoo 的 ``images.txt`` 取前 ``views`` 行写成一份清单。

    条目形如 ``model/frame_01.jpg``，是**相对 C++ demo 根目录**的路径；
    Python sample 的清单解析规则是「清单自身目录 → 其父目录 → cwd」，
    所以清单必须放在 demo 根目录下才能解析到同一批素材。
    """
    src = os.path.join(CPP_MODEL_DIR, "images.txt")
    with open(src, "r", encoding="utf-8") as f:
        lines = [ln.strip() for ln in f if ln.strip()]
    if len(lines) < views:
        raise SystemExit(f"❌ {src} 只有 {len(lines)} 行，不够 {views} 个 view")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines[:views]) + "\n")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--skip-cpp", action="store_true",
                    help="只跑 Python 侧（不比对 C++）")
    ap.add_argument("--keep", action="store_true",
                    help="保留临时输出目录")
    ap.add_argument("--views", type=int, default=DEFAULT_VIEWS,
                    help=f"导出时烤进去的 view 数（默认 {DEFAULT_VIEWS}）")
    args = ap.parse_args()
    views = args.views

    tmp = tempfile.mkdtemp(prefix="da3_parity_")
    cpp_dir = os.path.join(tmp, "cpp")
    py_dir = os.path.join(tmp, "py")
    os.makedirs(cpp_dir, exist_ok=True)
    os.makedirs(py_dir, exist_ok=True)
    # 清单必须落在 C++ demo 根目录下 —— 条目是相对该目录的（``model/xxx.jpg``）。
    list_path = _write_image_list(os.path.join(CPP_DIR, f"images_{views}view.txt"), views)
    print(f"临时目录: {tmp}")
    print(f"view 数: {views}（清单 {list_path}）")

    try:
        print("\n== 1. 跑 C++ 参考实现 ==")
        cpp_out = _run_cpp(cpp_dir, views)
        print("\n== 2. 跑 wdnai Python 示例 ==")
        py_out = _run_python(py_dir, views, list_path)

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
        names = ["depth_%02d.jpg" % i for i in range(views)] + ["depth_montage.jpg"]
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
        try:
            os.remove(list_path)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
