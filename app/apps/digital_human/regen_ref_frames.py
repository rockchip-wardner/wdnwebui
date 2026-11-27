#!/usr/bin/env python3
"""重新生成形象素材的 ``ref_frames/`` —— 从 ``bg_video.mp4`` 按 ``face_box.txt`` 裁。

为什么需要
==========

LiteAvatar 的渲染是「把 decoder 生成的脸贴回背景帧」。引擎按帧号配对：
第 i 帧背景 ↔ 第 i 张参考帧（``ref_latents[bg_id]``），所以**参考帧必须就是
背景视频同一时刻、同一个裁剪框**。

官方素材包里这两段是**不同的素材**：参考帧是另一段视频里按同样 face_box
裁的。于是贴图里的脸（姿态/表情/轮廓）和背景里的头颈对不上：

* face_box 边界（发丝/下颌/衣领）出现**接缝亮线**；
* 贴图自己的轮廓和背景的轮廓错位 → 脸的边缘出现"双轮廓"。

实测（生产形象 643×425 face_box，接缝环 = face_box 边框内外 14 px 带）：

============================  ======  =====  =====
方案                          环mean  环p99  环max
============================  ======  =====  =====
官方参考帧（异源）             1.68   20.7    91
**从 bg_video 重裁**            1.19    8.7    35
============================  ======  =====  =====

p99 降 58%、max 降 62%，而脸部内部（嘴部动画本身）几乎不变。

⚠️ 这**只解决"贴图不准"**。嘴部"花屏"（牙齿糊成一团）是 decoder 在 256²
分辨率下的固有限制，参考帧换不换都一样 —— 那要重建 384 的 decoder RKNN。

用法
====

::

    # 单个形象（先备份旧参考帧）
    python regen_ref_frames.py --avatar-dir ~/models/Lite-Avatar/avatars/<id>

    # 全部形象
    python regen_ref_frames.py --all --avatars-dir ~/models/Lite-Avatar/avatars

    # 只看会做什么，不写盘
    python regen_ref_frames.py --all --avatars-dir ... --dry-run

官方 ``ref_frames/`` **已经删掉了** —— 它是另一段视频裁的（跟
``bg_video.mp4`` 不同源，贴回去脸缘有接缝），而且下载时会被直接丢弃
（见 ``avatars.py`` 的 ``DROP_DIRS_ON_EXTRACT``）。引擎在 ``ref_frames_bg/``
缺失时会自己从背景帧重裁一份，所以这个脚本现在是**手动重生成**用的
（产物与引擎自动生成的逐字节相同）。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path


#: 新参考帧的目录名 —— **官方 ref_frames/ 不动**（它在 SHA256SUMS 发布清单里）。
#: 引擎优先用这个目录，没有就回落到官方的 ref_frames/。
OUT_DIR_NAME = "ref_frames_bg"


def _log(msg: str) -> None:
    print("[regen-refs] {}".format(msg), flush=True)


def regenerate(avatar_dir: Path, *, count: int = 150, quality: int = 95,
                out_name: str = OUT_DIR_NAME, dry_run: bool = False) -> int:
    """重新生成一个形象的参考帧。返回写出的帧数。"""
    import cv2

    bg = avatar_dir / "bg_video.mp4"
    box_file = avatar_dir / "face_box.txt"
    ref_dir = avatar_dir / out_name
    for p in (bg, box_file):
        if not p.is_file():
            raise SystemExit("缺少素材: {}".format(p))

    y1, y2, x1, x2 = (int(v) for v in box_file.read_text().split())
    if not (0 <= y1 < y2 and 0 <= x1 < x2):
        raise SystemExit("face_box 非法: {}".format(box_file.read_text().strip()))

    cap = cv2.VideoCapture(str(bg))
    if not cap.isOpened():
        raise SystemExit("打不开背景视频: {}".format(bg))

    if dry_run:
        _log("{}: 将从 {} 裁 [{}:{}, {}:{}] 共 {} 帧 → {}".format(
            avatar_dir.name, bg.name, x1, x2, y1, y2, count, ref_dir))
        cap.release()
        return 0

    # 旧的同源参考帧先备份（引擎也会自动生成，产物逐字节相同）。
    if ref_dir.is_dir():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        bak = avatar_dir / (out_name + ".bak-" + stamp)
        ref_dir.rename(bak)
        _log("{}: 旧 {} 已备份到 {}".format(avatar_dir.name, out_name, bak.name))
    ref_dir.mkdir(parents=True, exist_ok=True)

    n = 0
    try:
        while n < count:
            ok, frame = cap.read()
            if not ok:
                break
            crop = frame[y1:y2, x1:x2]
            cv2.imwrite(str(ref_dir / "ref_{:05d}.jpg".format(n)), crop,
                        [int(cv2.IMWRITE_JPEG_QUALITY), quality])
            n += 1
    finally:
        cap.release()

    _log("{}: 写出 {} 张参考帧（{}×{}, JPEG q{}）".format(
        avatar_dir.name, n, x2 - x1, y2 - y1, quality))
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="从 bg_video.mp4 重新裁剪形象参考帧（消除贴图接缝）")
    ap.add_argument("--avatar-dir", help="单个形象目录（含 bg_video.mp4）")
    ap.add_argument("--all", action="store_true", help="处理目录下所有形象")
    ap.add_argument("--avatars-dir", default="", help="--all 时的形象根目录")
    ap.add_argument("--count", type=int, default=150,
                    help="裁多少帧（默认 150，和引擎的 BG_FRAME_COUNT 一致）")
    ap.add_argument("--quality", type=int, default=95, help="参考帧 JPEG 质量")
    ap.add_argument("--out-name", default=OUT_DIR_NAME,
                    help="输出目录名（默认 {}, 不动官方 ref_frames/）".format(OUT_DIR_NAME))
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不写盘")
    args = ap.parse_args(argv)

    if args.all:
        root = Path(args.avatars_dir).expanduser()
        if not root.is_dir():
            raise SystemExit("形象目录不存在: {}".format(root))
        dirs = sorted(d for d in root.iterdir()
                      if d.is_dir() and (d / "bg_video.mp4").is_file())
        if not dirs:
            raise SystemExit("{} 下没有形象目录".format(root))
    elif args.avatar_dir:
        dirs = [Path(args.avatar_dir).expanduser()]
    else:
        raise SystemExit("要 --avatar-dir 或 --all")

    total = 0
    for d in dirs:
        total += regenerate(d, count=args.count, quality=args.quality,
                            out_name=args.out_name, dry_run=args.dry_run)
    _log("完成：{} 个形象，{} 张参考帧".format(len(dirs), total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
