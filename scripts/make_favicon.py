#!/usr/bin/env python3
"""生成应用图标（favicon）—— SVG + ICO，**从同一份描述**产出，不会走样。

产出：

* ``app/static/core/favicon.svg`` —— 矢量图。现代浏览器直接用；
* ``app/static/core/favicon.ico`` —— 16/32/48 三档位（PNG-in-ICO），
  给不认 SVG favicon 的浏览器（Safari / iOS / 老 IE）。

图形沿用侧栏「应用面板」那个图标（``index.html.template`` 里的
``#i-app-list``）：蓝色圆角底 + 三条横线 + 右端三个点。用 ``currentColor``
的版本在侧栏里是单色描边；favicon 必须自带颜色（浏览器标签页的背景色
不可控），所以这里把底色固定成产品的强调色 ``#2563eb``（framework.css 的
``--accent``），前景改白色。

本机没有 ffmpeg / PIL / cairosvg，所以 ICO 是**自己画的**：用 numpy +
OpenCV 画 4 倍尺寸再降采样（抗锯齿），编成 PNG，再按 ICO 容器格式打包。

用法::

    .venv/bin/python scripts/make_favicon.py
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "app" / "static" / "core"

ACCENT = (37, 99, 235)      # #2563eb (BGR，cv2 的顺序)
WHITE = (255, 255, 255)

#: 图标在 24×24 网格上的几何（与侧栏 sprite 同比例）
VIEW = 24
LINES = [(4, 20), (4, 20), (4, 18)]   # (x1, x2)，y 依次 6 / 12 / 18
LINE_Y = [6, 12, 18]
STROKE = 2.0
DOTS = [(20, 6), (20, 12), (18, 18)]
DOT_R = 1.2
RADIUS = 4.5


def svg_source() -> str:
    """生成 SVG 文本（与下面的位图绘制同源）。"""
    lines = "".join(
        f'    <path d="M{x1} {y}h{x2 - x1}"/>\n'
        for (x1, x2), y in zip(LINES, LINE_Y))
    dots = "".join(
        f'    <circle cx="{cx}" cy="{cy}" r="{DOT_R}"/>\n'
        for cx, cy in DOTS)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {VIEW} {VIEW}" width="64" height="64" role="img" aria-label="守望者">
  <rect x="0" y="0" width="{VIEW}" height="{VIEW}" rx="{RADIUS}" fill="#2563eb"/>
  <g fill="none" stroke="#ffffff" stroke-width="{STROKE}" stroke-linecap="round">
{lines}  </g>
  <g fill="#ffffff">
{dots}  </g>
</svg>
"""


def _rounded_rect_mask(size: int, scale: float) -> np.ndarray:
    """圆角矩形遮罩（1 = 图形内）。"""
    import cv2

    m = np.zeros((size, size), np.uint8)
    r = int(round(RADIUS * scale))
    cv2.rectangle(m, (0, r), (size - 1, size - 1 - r), 255, -1)
    cv2.rectangle(m, (r, 0), (size - 1 - r, size - 1), 255, -1)
    for cx, cy in ((r, r), (size - 1 - r, r), (r, size - 1 - r),
                   (size - 1 - r, size - 1 - r)):
        cv2.circle(m, (cx, cy), r, 255, -1)
    return m


def render_png(size: int) -> bytes:
    """画一张 size×size 的 PNG（内部按 4 倍画再降采样 → 抗锯齿）。"""
    import cv2

    big = size * 4
    scale = big / VIEW
    img = np.zeros((big, big, 3), np.uint8)
    img[:] = ACCENT

    mask = _rounded_rect_mask(big, scale)
    img[mask == 0] = (0, 0, 0)

    white = np.zeros((big, big), np.uint8)
    for (x1, x2), y in zip(LINES, LINE_Y):
        cv2.line(white,
                 (int(round(x1 * scale)), int(round(y * scale))),
                 (int(round(x2 * scale)), int(round(y * scale))),
                 255, max(1, int(round(STROKE * scale))),
                 lineType=cv2.LINE_AA)
    for cx, cy in DOTS:
        cv2.circle(white,
                   (int(round(cx * scale)), int(round(cy * scale))),
                   max(1, int(round(DOT_R * scale))), 255, -1,
                   lineType=cv2.LINE_AA)

    # 白色前景只画在圆角底之内
    white = cv2.bitwise_and(white, mask)
    img[white > 0] = WHITE

    small = cv2.resize(img, (size, size), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".png", small)
    if not ok:
        raise RuntimeError("PNG 编码失败")
    return buf.tobytes()


def build_ico(sizes=(16, 32, 48)) -> bytes:
    """打包 ICO（每个 entry 用 PNG 载荷）。"""
    images = [(s, render_png(s)) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries = b""
    payload = b""
    for size, blob in images:
        entries += struct.pack(
            "<BBBBHHII",
            size if size < 256 else 0, size if size < 256 else 0,
            0, 0, 1, 32, len(blob), offset)
        payload += blob
        offset += len(blob)
    return header + entries + payload


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    svg_path = OUT_DIR / "favicon.svg"
    svg_path.write_text(svg_source(), encoding="utf-8")
    print(f"[PASS] {svg_path} ({svg_path.stat().st_size} 字节)")

    ico_path = OUT_DIR / "favicon.ico"
    ico = build_ico()
    ico_path.write_bytes(ico)
    print(f"[PASS] {ico_path} ({len(ico)} 字节, 16/32/48)")

    # 自检：ICO 头能解析出 3 个 entry
    reserved, typ, count = struct.unpack("<HHH", ico[:6])
    assert (reserved, typ, count) == (0, 1, 3), (reserved, typ, count)
    print(f"[PASS] ICO 头自检：type={typ} count={count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
