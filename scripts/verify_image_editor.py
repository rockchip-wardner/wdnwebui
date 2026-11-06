#!/usr/bin/env python3
"""端到端验收：**图片处理卡**（图 + 文本 → 图）。

走完整链路：卡片 API → worker RPC → wdnai session → cshim img2img → PNG。

用法（板端）：
    .venv/bin/python scripts/verify_image_editor.py \
        --source /home/rockchip/assets/media_generator/runs/022a16e93794/img01.png \
        --prompt "把背景换成夕阳下的海滩，人物保持不变" \
        --strength 0.75 --steps 8
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
API = BASE + "/api/image-editor"


def _post(path, payload, timeout=120):
    req = urllib.request.Request(
        path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(path, timeout=120):
    with urllib.request.urlopen(path, timeout=timeout) as r:
        return r.read()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=False,
                    default="/home/rockchip/assets/media_generator/runs/022a16e93794/img01.png",
                    help="输入图（PNG/JPEG）")
    ap.add_argument("--prompt", default="把背景换成夕阳下的海滩，人物保持不变")
    ap.add_argument("--strength", type=float, default=0.75)
    ap.add_argument("--steps", type=int, default=8)
    ap.add_argument("--out", default="/tmp/img2img_out.png")
    args = ap.parse_args()

    with open(args.source, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    print(f"输入图 {args.source}（{len(b64)} 字节 base64）", flush=True)

    # ---- ⓪ 等卡片空闲 ----
    # 这张卡**一次只能跑一个生成**（busy 时后端返 409「上一张还在生成中」）。
    # 连着跑多个验收脚本时，上一个的生成往往还没跑完 ⇒ 直接发起必然 409。
    # 等到 idle 再发起（最多等 10 分钟）。
    for _ in range(200):
        try:
            snap = json.loads(_get(API + "/").decode("utf-8"))
            if snap.get("state") == "idle" and not snap.get("busy"):
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(3)
    else:
        print("⚠️  卡片一直忙，继续发起（可能撞 409）", flush=True)

    # ---- ① 发起 ----
    t0 = time.time()
    try:
        res = _post(API + "/generate", {
            "prompt": args.prompt,
            "steps": args.steps,
            "fixed_noise": False,
            "image": b64,
            "strength": args.strength,
        })
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        print("❌ 发起失败 HTTP", e.code, body, flush=True)
        return 1
    print("已发起:", json.dumps(res, ensure_ascii=False)[:300], flush=True)

    # ---- ② 等结果 ----
    deadline = time.time() + 600
    run_id = None
    while time.time() < deadline:
        time.sleep(3)
        try:
            snap = json.loads(_get(API + "/").decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            print("  状态查询失败:", e, flush=True)
            continue
        state = snap.get("state")
        err = snap.get("error")
        run_id = (snap.get("request") or {}).get("run_id") or run_id
        print(f"  [{time.time()-t0:6.1f}s] state={state} busy={snap.get('busy')} "
              f"err={err!r}", flush=True)
        if err:
            print("❌ 生成失败:", err, flush=True)
            return 2
        if state == "idle" and run_id:
            break
    if not run_id:
        print("❌ 没拿到 run_id", flush=True)
        return 3

    # ---- ③ 取图 ----
    try:
        png = _get(API + "/image/" + run_id)
    except urllib.error.HTTPError as e:
        print("❌ 取图失败 HTTP", e.code, e.read().decode("utf-8", "replace"),
              flush=True)
        return 4
    with open(args.out, "wb") as f:
        f.write(png)
    print(f"✅ 输出 {args.out}（{len(png)} 字节，用时 {time.time()-t0:.1f}s）",
          flush=True)

    # ---- ④ 与输入图比一下：像素不该完全一样（说明确实被改写了）----
    try:
        import numpy as np
        from PIL import Image
        import io
        src = np.asarray(Image.open(args.source).convert("RGB").resize((512, 512)))
        dst = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))
        diff = float(np.abs(src.astype(np.float32) - dst.astype(np.float32)).mean())
        print(f"与输入图的平均像素差: {diff:.2f}（0 = 完全一样）", flush=True)
        if diff < 1.0:
            print("⚠️ 输出和输入几乎一样 —— 图生图可能没生效", flush=True)
    except Exception as e:  # noqa: BLE001
        print("（对比跳过:", e, "）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
