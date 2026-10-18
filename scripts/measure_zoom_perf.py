#!/usr/bin/env python3
"""实测「一路放大到 720p 档」对其它路的解码吞吐 / VLM 轮转的影响。

为什么必须实测：放大把被点那一路从 384x222 抬到 576x1280（~11 倍像素），
RGA 缩放 + NV12→BGR + JPEG 编码都在同一个解码热路径里。理论上是**单路**
多花 CPU，但 AGENTS.md 记过两次「一路的事拖垮全部」的教训（wait_idle 忙等
饿死解码、每路 fps 按总预算摊分），所以不能靠推理，得量。

判据：同一批路、同一段时长，zoom 前后的**每路解码帧率**和 **VLM 轮转速率**
对比 —— 只看被放大的那一路会漏掉「它把预算吃光、别人跟着掉」这种退化。

用法: .venv/bin/python scripts/measure_zoom_perf.py [路数] [采样秒数]
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = "http://localhost:8000"
API = f"{BASE}/api/video-target-localization"
# 竖屏 576x1280：长边正好 1280，放大档能吃满 720p 的像素量（737k px，
# 与横屏 1280x720 的 921k px 同量级），本机没有横屏大素材。
SRC = "/home/rockchip/work/tmp/test.mp4"
MODEL = "LocateAnything-3b"


def req(url: str, body=None, method=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
        method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {"__err": e.read().decode()[:300]}
    except Exception as e:                       # noqa: BLE001
        return 0, {"__err": repr(e)[:200]}


def sess():
    _, j = req(f"{API}/")
    return j.get("session") or j


def sample(seconds: float):
    """采一段窗口的吞吐：每路解码帧率 + VLM 轮转速率 + 解码错误数。

    snapshot 里没有 uptime 字段，用墙钟差当分母。
    """
    t0 = sess(); w0 = time.monotonic()
    time.sleep(seconds)
    t1 = sess(); dt = time.monotonic() - w0

    a = {c["idx"]: c for c in t0.get("channel_list", [])}
    b = {c["idx"]: c for c in t1.get("channel_list", [])}
    per = []
    for i in sorted(b):
        df = b[i].get("frames", 0) - a.get(i, {}).get("frames", 0)
        per.append(round(df / dt, 2))

    v0, v1 = t0.get("vlm", {}), t1.get("vlm", {})
    return {
        "dec": per,
        "total_dec": round(sum(per), 2),
        "round_per_s": round((v1.get("round", 0) - v0.get("round", 0)) / dt, 3),
        "errors": sum(c.get("errors", 0) for c in b.values()),
        "disp": [c.get("disp_w") for c in b.values()],
        "state": t1.get("state"),
    }


def main():
    nch = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    win = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0

    print(f"== 放大态性能实测 · {nch} 路 · 采样 {win}s ==")
    print("素材:", SRC)

    # ---- 加载定位模型（只上 npu0；一张卡只能一个模型）----
    st, j = req(f"{BASE}/api/compute/instances",
                {"model": MODEL, "npu_index": 0})
    print(f"load {MODEL} → npu0: HTTP {st} {json.dumps(j, ensure_ascii=False)[:180]}")
    if st not in (200, 201):
        print("加载失败，退出"); return 1

    for _ in range(120):
        time.sleep(2)
        _, c = req(f"{BASE}/api/compute")
        inst = [i for i in c.get("instances", [])
                if i.get("model_id") == MODEL or i.get("model") == MODEL and i.get("npu_index") == 0]
        if inst and inst[0].get("status") == "loaded":
            break
    print("模型状态:", inst[0].get("status") if inst else "?",
          "err:", (inst[0].get("error") or "")[:120] if inst else "")

    # ---- 开 N 路 + 启动检测 ----
    srcs = [SRC] * nch
    st, j = req(f"{API}/open", {"sources": srcs, "loop": True})
    print("open:", st, json.dumps(j, ensure_ascii=False)[:160])
    if st >= 400:
        print("open 失败"); return 1
    time.sleep(6)

    st, j = req(f"{API}/prompt", {"prompts": ["行人"], "every_n": 1})
    print("prompt:", st, json.dumps(j, ensure_ascii=False)[:200])
    if st >= 400:
        print("启动检测失败"); return 1

    print("\n等待检测稳定…")
    time.sleep(12)

    s0 = sess()
    print(f"  会话: state={s0.get('state')} 路数={s0.get('channels')} "
          f"disp={s0.get('disp_w')}x{s0.get('disp_h')} "
          f"analysis={s0.get('analysis_size')}")

    print("\n[基线] 未放大，采样中…")
    base = sample(win)
    print(f"  每路解码fps: {base['dec']}  合计 {base['total_dec']}  "
          f"轮转 {base['round_per_s']}/s  错误 {base['errors']}  disp={base['disp']}")

    print(f"\n[放大] ch1 切到 720p 档…")
    st, j = req(f"{API}/zoom", {"channel": 0, "zoomed": True})
    s1 = sess()
    print(f"  zoom → HTTP {st}  后端 zoomed={s1.get('zoomed')} disp={s1.get('channel_list') and [c['disp_w'] for c in s1['channel_list']]}")
    time.sleep(4)          # 等解码尺寸切换 + 稳态

    print("  采样中…")
    z = sample(win)
    print(f"  每路解码fps: {z['dec']}  合计 {z['total_dec']}  "
          f"轮转 {z['round_per_s']}/s  错误 {z['errors']}  disp={z['disp']}")

    print("\n[还原] ch1 回到原档…")
    req(f"{API}/zoom", {"channel": 0, "zoomed": False})
    time.sleep(4)
    print("  采样中…")
    back = sample(win)
    print(f"  每路解码fps: {back['dec']}  合计 {back['total_dec']}  "
          f"轮转 {back['round_per_s']}/s  错误 {back['errors']}  disp={back['disp']}")

    print("\n== 汇总 ==")
    print(f"  像素量: {s0.get('channel_list')[0]['disp_w']}x"
          f"{s0.get('channel_list')[0]['disp_h']} → "
          f"{s1.get('channel_list')[0]['disp_w']}x{s1.get('channel_list')[0]['disp_h']}")
    for name, r in (("基线", base), ("放大", z), ("还原", back)):
        others = r["dec"][1:] if nch > 1 else []
        avg = round(sum(others) / len(others), 2) if others else 0
        print(f"  {name:4s} 合计解码 {r['total_dec']:7.2f}  其它路均值 {avg:6.2f}  "
              f"轮转 {r['round_per_s']:.3f}/s  错误 {r['errors']}")
    if base["total_dec"] > 0:
        print(f"\n  放大态总解码吞吐掉 {base['total_dec'] - z['total_dec']:.2f} fps "
              f"({(1 - z['total_dec'] / base['total_dec']) * 100:.1f}%)")
    if base["round_per_s"] > 0:
        print(f"  轮转速率掉 {(1 - z['round_per_s'] / base['round_per_s']) * 100:.1f}%")

    # ---- 收尾 ----
    req(f"{API}/prompt/stop", {})
    req(f"{API}/close", {})
    req(f"{BASE}/api/compute/instances", {"model": MODEL, "npu_index": 0}, method="DELETE")
    print("\n已清理")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["pkill", "-f", "[h]eadless_shell"], capture_output=True)
