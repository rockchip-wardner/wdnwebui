#!/usr/bin/env python3
"""GPU 合成 vs CPU 合成 的逐帧画质对比。

用法：
    .venv/bin/python tools/cmp_avatar_composite.py <gpu_job_dir> <cpu_job_dir> [out_dir]
"""
import sys
from pathlib import Path

import cv2
import numpy as np

JOBS = Path("/home/rockchip/.wardner/wdnwebui/app/uploads/digital_human/jobs")


def load(p):
    im = cv2.imread(str(p))
    return None if im is None else im.astype(np.float32)


def psnr(a, b):
    mse = float(np.mean((a - b) ** 2))
    if mse <= 1e-12:
        return 99.0
    return 10.0 * np.log10(255.0 * 255.0 / mse)


def main():
    gpu_dir = Path(sys.argv[1])
    cpu_dir = Path(sys.argv[2])
    out_dir = Path(sys.argv[3]) if len(sys.argv) > 3 else Path("/tmp/avatar-cmp")
    face_box_file = sys.argv[4] if len(sys.argv) > 4 else None
    out_dir.mkdir(parents=True, exist_ok=True)

    gframes = sorted(gpu_dir.glob("*.jpg"))
    cframes = sorted(cpu_dir.glob("*.jpg"))
    n = min(len(gframes), len(cframes))
    print("GPU 帧: %d   CPU 帧: %d   对比 %d 帧" % (len(gframes), len(cframes), n))

    full_psnr, box_psnr = [], []
    box = None            # 嘴部框 (x1,y1,x2,y2)
    diff_acc = None

    for i in range(n):
        g = load(gframes[i])
        c = load(cframes[i])
        if g is None or c is None:
            continue
        if g.shape != c.shape:
            print("第 %d 帧尺寸不同: %s vs %s" % (i, g.shape, c.shape))
            continue
        full_psnr.append(psnr(g, c))
        d = np.abs(g - c).max(axis=2)
        if diff_acc is None:
            diff_acc = np.zeros(d.shape, dtype=np.float64)
        diff_acc += d

    diff_mean = diff_acc / max(1, n)
    # 嘴部框：优先用形象包里的 face_box.txt（真实坐标），否则按差异自动定位
    box = None
    if face_box_file:
        y1, y2, x1, x2 = (int(v) for v in
                          Path(face_box_file).read_text().split())
        box = (x1, y1, x2, y2)
        print("face_box.txt: x1=%d y1=%d x2=%d y2=%d  尺寸 %dx%d"
              % (x1, y1, x2, y2, x2 - x1, y2 - y1))
    else:
        thr = max(2.0, float(np.percentile(diff_mean, 97.0)))
        ys, xs = np.where(diff_mean > thr)
        if len(xs) > 100:
            x1, x2 = int(np.percentile(xs, 1)), int(np.percentile(xs, 99))
            y1, y2 = int(np.percentile(ys, 1)), int(np.percentile(ys, 99))
            pad = 8
            box = (max(0, x1 - pad), max(0, y1 - pad),
                   min(2048, x2 + pad), min(2048, y2 + pad))
            print("嘴部差异区(自动定位): x1=%d y1=%d x2=%d y2=%d"
                  % (box[0], box[1], box[2], box[3]))
    print("差异像素统计: mean=%.3f  p95=%.3f  p99=%.3f  max=%.3f"
          % (diff_mean.mean(), np.percentile(diff_mean, 95),
             np.percentile(diff_mean, 99), diff_mean.max()))

    if box:
        for i in range(n):
            g = load(gframes[i])
            c = load(cframes[i])
            if g is None or c is None or g.shape != c.shape:
                continue
            x1, y1, x2, y2 = box
            box_psnr.append(psnr(g[y1:y2, x1:x2], c[y1:y2, x1:x2]))

    def stat(name, arr):
        if not arr:
            print("%s: 无数据" % name)
            return
        a = np.array(arr)
        print("%s: 均值 %.2f dB  中位 %.2f dB  最小 %.2f dB  最大 %.2f dB"
              % (name, a.mean(), np.median(a), a.min(), a.max()))

    stat("整帧 PSNR", full_psnr)
    stat("嘴部框内 PSNR", box_psnr)

    # ---- 出图：并排 + 差异放大 ----------------------------------------
    picks = [0, n // 3, 2 * n // 3, n - 1]
    rows = []
    for i in picks:
        g = cv2.imread(str(gframes[i]))
        c = cv2.imread(str(cframes[i]))
        if g is None or c is None:
            continue
        d = cv2.absdiff(g, c)
        d = cv2.convertScaleAbs(d, alpha=3.0)
        d = cv2.applyColorMap(d, cv2.COLORMAP_JET)
        label_h = 34
        def band(img, text, color):
            bar = np.full((label_h, img.shape[1], 3), 30, np.uint8)
            cv2.putText(bar, text, (10, 24), cv2.FONT_HERSHEY_SIMPLEX,
                        0.7, color, 2, cv2.LINE_AA)
            return np.vstack([bar, img])
        row = np.hstack([
            band(g, "GPU (wdnav OpenGL)  frame %d" % i, (255, 255, 255)),
            band(c, "CPU (cv2 + numpy)    frame %d" % i, (255, 255, 255)),
            band(d, "abs diff x3", (255, 255, 255)),
        ])
        rows.append(row)
    if rows:
        w = min(r.shape[1] for r in rows)
        rows = [r[:, :w] for r in rows]
        # 缩小到能看全
        scale = 1600.0 / w
        rows = [cv2.resize(r, (int(w * scale), int(r.shape[0] * scale)),
                           interpolation=cv2.INTER_AREA) for r in rows]
        cv2.imwrite(str(out_dir / "compare_overview.jpg"), np.vstack(rows),
                    [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        print("对比总览:", out_dir / "compare_overview.jpg")

    # ---- 嘴部特写（放大 3 倍） ----------------------------------------
    if box:
        x1, y1, x2, y2 = box
        tiles = []
        for i in picks:
            g = cv2.imread(str(gframes[i]))
            c = cv2.imread(str(cframes[i]))
            if g is None or c is None:
                continue
            tiles.append(cv2.resize(g[y1:y2, x1:x2], None, fx=3, fy=3,
                                    interpolation=cv2.INTER_NEAREST))
            tiles.append(cv2.resize(c[y1:y2, x1:x2], None, fx=3, fy=3,
                                    interpolation=cv2.INTER_NEAREST))
        if tiles:
            w = min(t.shape[1] for t in tiles)
            h = min(t.shape[0] for t in tiles)
            tiles = [t[:h, :w] for t in tiles]
            cv2.imwrite(str(out_dir / "compare_mouth_zoom.jpg"),
                        np.hstack(tiles), [int(cv2.IMWRITE_JPEG_QUALITY), 95])
            print("嘴部特写:", out_dir / "compare_mouth_zoom.jpg")

    # ---- 嘴部框内单帧并排（放大） -------------------------------------
    if box:
        x1, y1, x2, y2 = box
        i = n // 2
        g = cv2.imread(str(gframes[i]))
        c = cv2.imread(str(cframes[i]))
        if g is not None and c is not None:
            z = 3
            gg = cv2.resize(g[y1:y2, x1:x2], None, fx=z, fy=z,
                            interpolation=cv2.INTER_NEAREST)
            cc = cv2.resize(c[y1:y2, x1:x2], None, fx=z, fy=z,
                            interpolation=cv2.INTER_NEAREST)
            sep = np.full((gg.shape[0], 6, 3), 255, np.uint8)
            cv2.imwrite(str(out_dir / "mouth_gpu_vs_cpu.jpg"),
                        np.hstack([gg, sep, cc]),
                        [int(cv2.IMWRITE_JPEG_QUALITY), 95])
            print("嘴部并排:", out_dir / "mouth_gpu_vs_cpu.jpg")
    return 0


sys.exit(main())
