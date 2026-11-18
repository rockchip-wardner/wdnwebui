"""图文内容审查（Intern-Decision-4B）耗时基准。

用法:
    .venv/bin/python scripts/bench_content_review.py [--repeat 1] [--dims 1,2,4,7,16]

打的是**真实链路**：POST /api/content-review/review —— 与界面「开始审查」按钮
走的是同一条路径（ContentReviewManager.review → DDS/RPC → worker → NPU）。

这个模型**不是对话模型**
-----------------------
Intern-Decision-4B 是 **prefill-only** 的结构化决策模型：

* 每个决策字段跑**一次** ``max_new_tokens=1`` / ``prefill_only=True`` 的推理，
  解码 token 恒为 0 ⇒ 界面上的 tok/s 是**预填充速率**，不是生成速度；
* 每个字段必须**换一次 LLM session**（``recreate_llm_session()``）—— 模型有
  linear-attention 层，递归状态跨调用残留，C++ 参考实现每个字段都
  ``rknn3_session_init/destroy``。

⇒ 所以耗时随**维度数**增长，而且因为「字段 i 的 prompt 前缀包含字段 0..i-1 的
定义」，总 prefill token 是 **O(N²)**。本脚本就是把这条曲线测出来。

⚠️ **并发请求会串行叠加**
------------------------
worker 一次只跑一个推理。并发 N 个请求不是并行，而是排队（本脚本的
``--concurrency`` 段会验证：总墙钟 ≈ N × 单个耗时）。界面里同一个标签页的
重复点击有 ``_busy`` 守卫，但**多开标签页 / 多个客户端会互相排队**。

⚠️ **图片大小几乎不影响耗时**
---------------------------
服务端最终一律缩到 384×384，视觉编码器输出固定 151 个 token —— 那 151 个
token 会被**每个字段重新 prefill 一遍**（151 × N）。所以「传小图省时间」是
错觉：实测 320×240 与 4K 的耗时差 < 0.2 s，差的只是解码开销。
"""
from __future__ import annotations

import argparse
import base64
import json
import statistics
import threading
import time
import urllib.request

API = "http://127.0.0.1:8000/api/content-review/review"

#: 用来凑够维度数的样本（名称 + 说明）。名称会直接进 prompt，说明也会。
SAMPLE_ITEMS = [
    ("广告", "推销引流优惠"),
    ("诈骗", "转账冒充诱导"),
    ("色情低俗", "性相关露骨"),
    ("暴力血腥", "血腥伤害"),
    ("政治", "政治敏感话题"),
    ("辱骂", "脏话攻击"),
    ("火星文", "变形错别字"),
    ("赌博", "博彩下注"),
    ("毒品", "违禁药物"),
    ("烟草", "烟酒广告"),
    ("暴力恐怖", "恐怖袭击"),
    ("假冒", "假证办证"),
    ("隐私", "泄露个人信息"),
    ("版权", "盗版搬运"),
    ("垃圾信息", "无意义刷屏"),
    ("二维码", "可疑二维码"),
]

#: 前端上传前会把长边缩到 1280 并转 JPEG（见 static/app.js）。
FRONTEND_EDGE = 1280


def make_items(n: int):
    """n 个审查维度（模型硬上限 16）。"""
    out = []
    for i in range(n):
        name, desc = SAMPLE_ITEMS[i % len(SAMPLE_ITEMS)]
        # 重名会让后端合并 → 名字带序号，保证真的是 n 个字段
        out.append({"name": "%s%d" % (name, i + 1), "description": desc})
    return out


def make_image(w: int = FRONTEND_EDGE, h: int = 720) -> str:
    """生成一张测试图（前端会缩到同样大小再转 JPEG）。"""
    import cv2
    import numpy as np

    a = np.zeros((h, w, 3), dtype=np.uint8)
    for i in range(0, w, 40):
        cv2.line(a, (i, 0), (w - i, h), (0, 200, 255), 3)
    cv2.putText(a, "SAMPLE", (20, h // 2), cv2.FONT_HERSHEY_SIMPLEX,
                1.5, (255, 255, 255), 3)
    ok, buf = cv2.imencode(".jpg", a, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if not ok:
        raise RuntimeError("JPEG 编码失败")
    return base64.b64encode(buf.tobytes()).decode()


def post(payload: dict, timeout: float = 300.0):
    """POST 一次审查请求，返回 (响应体, 端到端墙钟秒)。"""
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        API, data=body, headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode())
    return data, time.perf_counter() - t0


def one(n: int, text: str, image_b64: str, repeat: int):
    """跑一个维度数，返回统计结果。"""
    walls, prefills, toks = [], [], []
    for _ in range(repeat):
        payload = {"text": text, "items": make_items(n)}
        if image_b64:
            payload["image_b64"] = image_b64
        data, wall = post(payload)
        perf = data.get("perf") or {}
        walls.append(wall)
        prefills.append(float(perf.get("prefill_ms") or 0))
        toks.append(float(perf.get("n_prefill_tokens") or 0))
    wall = statistics.median(walls)
    prefill_ms = statistics.median(prefills)
    return {
        "dims": n,
        "wall": wall,
        "prefill_ms": prefill_ms,
        "prefill_tok": statistics.median(toks),
        # 非 prefill 的开销 ≈ 每字段重建 session + 视觉编码 + RPC
        "overhead_per_dim": (wall * 1000.0 - prefill_ms) / max(n, 1),
    }


def _fit_quadratic(points):
    """最小二乘拟合 ``y = A·x + B·x²``，返回 (A, B)；点数不足返回 (None, None)。

    为什么要二次拟合而不是「除以维度数」
    ------------------------------------
    Intern-Decision 的 prompt 前缀是**在每个 ``<decision>`` 标记处切开**的
    ⇒ 字段 i 的前缀里已经含有字段 0..i-1 的定义 ⇒ 总 prefill token 随维度数
    **平方**增长。两点割线求斜率会把二次项算丢，必须做二次拟合。
    """
    if len(points) < 3:
        return None, None
    s1 = s2 = s3 = s4 = 0.0
    t0 = t1 = t2 = 0.0
    for x, y in points:
        x2, x3, x4 = x * x, x ** 3, x ** 4
        s1 += x
        s2 += x2
        s3 += x3
        s4 += x4
        t0 += y
        t1 += x * y
        t2 += x2 * y
    # 解 [s2 s3; s3 s4]·[A;B] = [t1;t2]
    det = s2 * s4 - s3 * s3
    if abs(det) < 1e-9:
        return None, None
    a = (t1 * s4 - t2 * s3) / det
    b = (t2 * s2 - t1 * s3) / det
    return a, b


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=1,
                    help="每个维度数重复几次取中位数")
    ap.add_argument("--dims", default="1,2,4,7,10,13,16",
                    help="要测的维度数（逗号分隔，模型上限 16）")
    ap.add_argument("--text", default="限时优惠，点击领取优惠券",
                    help="待审查的文字")
    ap.add_argument("--no-image", action="store_true",
                    help="只测纯文本")
    ap.add_argument("--concurrency", type=int, default=3,
                    help="并发段同时发几个请求（验证串行叠加）")
    args = ap.parse_args()

    dims = [int(x) for x in args.dims.split(",") if x.strip()]
    image_b64 = "" if args.no_image else make_image()

    print("=" * 88)
    print("图文内容审查耗时基准 —— %s"
          % ("纯文本" if args.no_image else "图文"))
    print("=" * 88)
    print("  %-6s %10s %12s %13s %14s"
          % ("维度", "wall(s)", "prefill_ms", "prefill_tok", "开销/字段(ms)"))
    rows = []
    for n in dims:
        r = one(n, args.text, image_b64, args.repeat)
        rows.append(r)
        print("  %-6d %10.2f %12.1f %13.0f %14.0f"
              % (r["dims"], r["wall"], r["prefill_ms"],
                 r["prefill_tok"], r["overhead_per_dim"]))

    if len(rows) >= 2:
        # prefill_tok 随维度数是**二次**的（字段 i 的 prompt 前缀包含字段
        # 0..i-1 的定义）⇒ 拟合 pt = A·N + B·N²（最小二乘），再报：
        #   base   = 固定前缀（system + state + 首字段定义 + 图片占位）
        #   边际   = 第 N 个维度额外带进来的 token 数（A + B·(2N-1)）
        A, B = _fit_quadratic([(r["dims"], r["prefill_tok"]) for r in rows])
        if A is not None:
            n0, t0 = rows[0]["dims"], rows[0]["prefill_tok"]
            n1, t1 = rows[-1]["dims"], rows[-1]["prefill_tok"]
            print("\n  → 拟合 prefill_tok ≈ %.0f·N %+.0f·N²"
                  % (A, B))
            print("  → 固定前缀 %.0f token；第 %d 个维度边际 +%.0f token"
                  % (A + B, n1, A + B * (2 * n1 - 1)))
            print("  → 维度从 %d 减到 %d：prefill token %.0f → %.0f（省 %.0f%%）"
                  % (n1, n0, t1, t0, (t1 - t0) / max(t1, 1) * 100))

    if image_b64 and args.concurrency > 1:
        print()
        print("=" * 88)
        print("并发 %d 个请求（验证是否串行叠加）" % args.concurrency)
        print("=" * 88)
        results = []

        def worker(i):
            _, wall = post({"text": args.text, "items": make_items(7),
                            "image_b64": image_b64})
            results.append((i, wall))

        t0 = time.perf_counter()
        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(args.concurrency)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        total = time.perf_counter() - t0
        for i, wall in sorted(results):
            print("  请求 %d: wall=%.2fs" % (i, wall))
        single = rows[min(range(len(rows)),
                          key=lambda k: abs(rows[k]["dims"] - 7))]["wall"]
        print("  总墙钟 %.2fs（串行应 ≈ %d × %.2fs = %.2fs）"
              % (total, args.concurrency, single, args.concurrency * single))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
