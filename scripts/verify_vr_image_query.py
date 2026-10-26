#!/usr/bin/env python3
"""回归防护：监控反查的图片检索（图 / 文 / 图+文）。

钉住：
1. 编码选择 —— 有图走 ``embed_video([img])``（单帧），纯文字走
   ``embed_text``；图+文时 text 作为 prompt 一起编码。
2. 路径校验 —— 相对路径 / 不存在 / 非图片后缀 / 超大文件都拒绝。
3. 质量检查 —— 纯色 / 随机噪声拒绝；正常图放行；关掉检查时放行。
4. 默认检索策略不因图片改变 —— 阈值 0.40、``min_results=0``（严格）。
5. ``_query.image`` 字段供前端显示「以图检索」。

不占 NPU（fake session）；真实效果由 Phase 0 实测覆盖。

用法::

    python -m scripts.verify_vr_image_query
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, ".")

import cv2
import numpy as np

from app.core.video import search as S

TMP = Path("/tmp/opencode")
fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        fails.append(name)


class FakeSession:
    def __init__(self):
        self.calls = []

    def embed_text(self, text):
        self.calls.append(("text", text))
        return np.array([1.0, 0.0], np.float32)

    def embed_video(self, frames, prompt=""):
        self.calls.append(("video", len(frames), prompt))
        return np.array([1.0, 0.0], np.float32)


def make_images():
    TMP.mkdir(parents=True, exist_ok=True)
    real = TMP / "vr_iq_real.jpg"
    solid = TMP / "vr_iq_solid.jpg"
    noise = TMP / "vr_iq_noise.png"
    cv2.imwrite(str(real), cv2.imread("/userdata/test/demo.jpg"))
    cv2.imwrite(str(solid), np.full((64, 64, 3), 128, np.uint8))
    rng = np.random.default_rng(1)
    # PNG（无损）保存随机噪声：JPEG 会把高频噪声压掉，测不出质量门槛。
    cv2.imwrite(str(noise),
                rng.integers(0, 256, (128, 128, 3), dtype=np.uint8))
    return str(real), str(solid), str(noise)


def main() -> int:
    real, solid, noise = make_images()

    check("[1] 正常图质量检查放行", S.image_quality_error(
        S.read_image_bgr(real)) is None)
    check("[2] 纯色图被拒绝",
          bool(S.image_quality_error(S.read_image_bgr(solid))))
    check("[3] 随机噪声图被拒绝",
          bool(S.image_quality_error(S.read_image_bgr(noise))))

    # 路径校验
    for name, path in [
        ("相对路径", "demo.jpg"),
        ("不存在", "/tmp/opencode/not_exists_xyz.jpg"),
    ]:
        try:
            S.validate_image_path(path)
            ok = False
        except ValueError:
            ok = True
        check(f"[4] 拒绝{name}", ok)
    txt = TMP / "vr_iq_notimage.txt"
    txt.write_text("x")
    try:
        S.validate_image_path(str(txt))
        ok = False
    except ValueError:
        ok = True
    check("[5] 拒绝非图片后缀", ok)
    with patch.object(S, "IMAGE_MAX_BYTES", 1):
        try:
            S.validate_image_path(real)
            ok = False
        except ValueError:
            ok = True
    check("[6] 拒绝超大图片", ok)

    # 编码选择（fake session）
    svc = S.VideoSearchService()
    fake = FakeSession()
    svc._ensure_session = lambda: fake

    vec = svc.encode_query("", real)
    check("[7] 纯图 → embed_video 单帧", fake.calls[-1] == ("video", 1, ""),
          str(fake.calls[-1]))
    check("[8] 纯图向量归一化前非空", float(np.linalg.norm(vec)) > 0)

    svc.encode_query("有人在搬箱子", real)
    check("[9] 图+文 → embed_video(prompt=文字)",
          fake.calls[-1] == ("video", 1, "有人在搬箱子"), str(fake.calls[-1]))

    svc.encode_query("有人在搬箱子", "")
    check("[10] 纯文字 → embed_text", fake.calls[-1] == ("text", "有人在搬箱子"),
          str(fake.calls[-1]))

    for name, text, img in [
        ("文字与图片都为空", "", ""),
        ("文字超过 50 字", "字" * 51, ""),
    ]:
        try:
            svc.encode_query(text, img)
            ok = False
        except ValueError:
            ok = True
        check(f"[11] {name} → ValueError", ok)

    # 关掉质量检查 → 噪声图也放行
    svc.encode_query("", noise, check_image_quality=False)
    check("[12] 关掉质量检查时噪声图放行",
          fake.calls[-1][0] == "video")

    # query：默认严格 0.40 / min_results=0 / _query.image
    svc._state = S._STATE_READY
    svc._index = [{"video_path": "x.mp4", "start_time": 0.0, "end_time": 4.0,
                   "chunk_idx": 0, "fps": 25, "vec": np.array([1.0, 0.0],
                                                             np.float32)}]
    svc._broadcast = lambda _p: None
    snap = svc.query("", image_path=real)
    check("[13] query 记录 image 文件名",
          snap.get("query", {}).get("image") == Path(real).name,
          str(snap.get("query")))
    check("[14] 默认严格阈值 0.40",
          snap.get("query", {}).get("sim_threshold") == S.FIXED_SIM_THRESHOLD)
    check("[15] 命中相似度 >= 0.40",
          bool(snap.get("hits")) and all(
              h["similarity"] >= S.FIXED_SIM_THRESHOLD
              for h in snap["hits"]),
          str([h["similarity"] for h in snap.get("hits", [])]))

    # 宽松匹配（min_results）仍可显式开启
    svc._index = [{"video_path": "x.mp4", "start_time": 0.0, "end_time": 4.0,
                   "chunk_idx": 0, "fps": 25, "vec": np.array([0.0, 1.0],
                                                             np.float32)}]
    snap = svc.query("", image_path=real, min_results=10)
    check("[16] 宽松匹配（min_results=10）返回低于阈值候选",
          bool(snap.get("hits")) and snap["hits"][0]["above_threshold"] is False,
          str(snap.get("query_meta")))

    print()
    print(f"{len(fails)} 项失败")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
