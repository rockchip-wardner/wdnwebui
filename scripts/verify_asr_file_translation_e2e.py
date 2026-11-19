#!/usr/bin/env python3
"""真机端到端：**音频文件 / 含音轨的视频** ⇒ 自己断句 ⇒ **译文**推回浏览器。

这条脚本验证的是**整条链路**（不是 mock）：

```
音频文件 ──► ASR worker ──► round 帧 ──► 自己断句 ──► 翻译服务 ──► MT worker
                                                                          │
        translation 帧 ◄── 同一条 WS ◄──────────────────────────────────┘
```

⚠️ **文件路径没有 ``endpoint`` 帧**（那是实时路径的端点检测给的句级信号）
⇒ 后端改用**自己按标点断句**（``service.py`` 的 ``_SentenceSplitter``）：
句末标点为主，epoch 切换 / 长度上限 / 收尾为兜底。

跑之前要满足：

1. **ASR 模型在跑**（``qwen3-asr``）—— 没有就自己起：
   ``POST /api/compute/instances {"model":"qwen3-asr","npu_index":0}``；
2. **翻译模型在跑**（``hy-mt-1-5``）—— 脚本会调
   ``POST /api/qwen3-asr/translate/enable`` 自己起（等 ~30 s）。

⚠️ **板端地址**默认 ``https://127.0.0.1:8000``（自签证书 ⇒ 跳过校验）。

用法::

    .venv/bin/python scripts/verify_asr_file_translation_e2e.py
    .venv/bin/python scripts/verify_asr_file_translation_e2e.py --target zh-CN
"""
from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import sys
import time
from pathlib import Path

import requests
import websockets

DEFAULT_WAV = Path("/userdata/assets/uploads/asr_en.wav")

#: 文件路径的译文序号基准（``service.py`` 的 ``_TR_FILE_INDEX_BASE``）。
#: 文件路径的句序号落在**这个区间**，实时路径是小整数（1、2、3…）⇒
#: 两条路径的译文不会互相顶掉。
TR_FILE_INDEX_BASE = 100000

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ok   {name}")
    else:
        _FAIL += 1
        print(f"  FAIL {name} {extra}")


def _base(args) -> str:
    return args.base.rstrip("/")


def _http(args, method: str, path: str, payload=None, timeout: float = 60.0,
          data=None, headers=None):
    url = _base(args) + path
    kw = {"verify": False, "timeout": timeout}
    if payload is not None:
        kw["json"] = payload
    if data is not None:
        kw["data"] = data
    if headers:
        kw["headers"] = headers
    return requests.request(method, url, **kw)


def _ensure_asr(args) -> bool:
    snap = _http(args, "GET", "/api/compute").json()
    running = any(i.get("model_id") == "qwen3-asr"
                   and i.get("status") == "running"
                   and i.get("_source") in ("spawned", "adopted_local")
                   for i in (snap.get("instances") or []))
    if running:
        return True
    print("  … ASR 模型没在跑，先起一个（~30 s）")
    _http(args, "POST", "/api/compute/instances",
          {"model": "qwen3-asr", "npu_index": 0}, timeout=180)
    for _ in range(24):
        time.sleep(5)
        snap = _http(args, "GET", "/api/compute").json()
        if any(i.get("model_id") == "qwen3-asr"
               and i.get("status") == "running"
               for i in (snap.get("instances") or [])):
            return True
    return False


def _ensure_mt(args) -> bool:
    st = _http(args, "GET", "/api/qwen3-asr/translate/status").json()
    if (st.get("model") or {}).get("running"):
        return True
    print("  … 翻译模型没在跑，用新端点起一个（~30 s）")
    _http(args, "POST", "/api/qwen3-asr/translate/enable", {}, timeout=220)
    st = _http(args, "GET", "/api/qwen3-asr/translate/status").json()
    return bool((st.get("model") or {}).get("running"))


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
async def run(args) -> int:
    print("=" * 72)
    print("真机端到端：音频文件 ⇒ 自己断句 ⇒ 译文")
    print("=" * 72)

    if not args.wav.exists():
        print("❌ 样本不存在: %s" % args.wav)
        return 1

    # --- 1. 模型 ---
    check("ASR 模型在跑", _ensure_asr(args))
    if not _ensure_asr(args):
        print("❌ ASR 模型起不来，脚本到此为止")
        return 1
    check("翻译模型在跑", _ensure_mt(args))
    if not _ensure_mt(args):
        print("❌ 翻译模型起不来，脚本到此为止")
        return 1

    # --- 2. 打开实时翻译 ---
    _http(args, "POST", "/api/qwen3-asr/translate/settings",
          {"enabled": True, "target_lang": args.target})
    st = _http(args, "GET", "/api/qwen3-asr/translate/status").json()
    check("实时翻译已开启", bool(st.get("enabled")), f"status={st}")
    check("目标语言 = %s" % args.target, st.get("target_lang") == args.target,
          f"target={st.get('target_lang')}")

    # --- 3. 打开 WS，发起文件识别 ---
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ws_url = _base(args).replace("https://", "wss://").replace("http://", "ws://")
    frames: list = []

    async with websockets.connect(ws_url + "/api/qwen3-asr/ws",
                                  ssl=ctx, open_timeout=15) as ws:

        async def _reader():
            try:
                async for raw in ws:
                    try:
                        frames.append(json.loads(raw))
                    except Exception:
                        pass
            except Exception:
                pass

        reader = asyncio.create_task(_reader())
        await asyncio.sleep(0.5)

        r = _http(args, "POST", "/api/qwen3-asr/transcribe",
                  data={"path": str(args.wav)}, timeout=120)
        if r.status_code != 200:
            print("❌ transcribe 失败: HTTP %s %s" % (r.status_code, r.text[:200]))
            reader.cancel()
            return 1
        check("文件识别已发起", bool(r.json().get("request_id")),
              f"body={r.text[:200]}")

        # 等识别跑完 + 译文出来（识别 ~30~60 s，译文再慢几秒）
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            kinds = [f.get("type") for f in frames]
            if "done" in kinds and kinds.count("translation") >= 1:
                break
            await asyncio.sleep(1.0)
        # 最后一条译文可能比 done 晚一点（翻译是异步的）
        await asyncio.sleep(4.0)
        reader.cancel()

    # --- 4. 断言 ---
    kinds: dict = {}
    for f in frames:
        kinds[f.get("type")] = kinds.get(f.get("type"), 0) + 1
    print("  WS 帧统计: %s" % kinds)

    rounds = [f for f in frames if f.get("type") == "round"]
    translations = [f for f in frames if f.get("type") == "translation"]
    done = [f for f in frames if f.get("type") == "done"]

    check("收到了 round 帧（识别在跑）", len(rounds) >= 1, f"frames={kinds}")
    check("收到了 done 帧（识别跑完）", len(done) >= 1, f"frames={kinds}")
    check("收到了译文帧（文件路径也有译文！）", len(translations) >= 1,
          f"frames={kinds}")

    if not translations:
        print("\n" + "=" * 72)
        print(f"通过 {_PASS} / 失败 {_FAIL}")
        print("=" * 72)
        return 1

    for t in translations:
        print("    #%s 原文: %s" % (t.get("index"), (t.get("source") or "")[:70]))
        print("         译文: %s   [%s, %s ms]"
              % ((t.get("text") or "")[:70], t.get("status"),
                 t.get("latency_ms")))

    # --- 译文帧属于**文件路径** ---
    sids = {t.get("stream_id") for t in translations}
    check("译文帧的 stream_id 是文件路径的（file-<请求号>）",
          all(str(s).startswith("file-") for s in sids), f"stream_ids={sids}")

    # --- 句序号落在文件路径的区间（不与实时路径的小整数撞）---
    idxs = [int(t.get("index") or 0) for t in translations]
    check("句序号落在文件路径的区间（≥ %d）" % TR_FILE_INDEX_BASE,
          all(i >= TR_FILE_INDEX_BASE for i in idxs), f"idxs={idxs}")
    check("句序号**不重复**（前端按序号覆盖，重复会顶掉）",
          len(set(idxs)) == len(idxs), f"idxs={idxs}")
    check("句序号递增", idxs == sorted(idxs), f"idxs={idxs}")

    # --- 至少一句翻成功 ---
    ok_tr = [t for t in translations if t.get("status") == "done"]
    check("至少一句翻成功（status=done）", len(ok_tr) >= 1,
          f"statuses={[t.get('status') for t in translations]}")
    if ok_tr:
        t = ok_tr[0]
        check("译文不是空的", bool((t.get("text") or "").strip()),
              f"text={t.get('text')!r}")
        check("译文带原文（前端要对照显示）", bool((t.get("source") or "").strip()),
              f"source={t.get('source')!r}")
        check("译文不是复读原文（目标语言不同）",
              (t.get("text") or "").strip() != (t.get("source") or "").strip(),
              f"text={t.get('text')!r} source={t.get('source')!r}")

    # --- 切出来的句子确实来自识别结果 ---
    if rounds:
        committed = rounds[-1].get("committed") or ""
        srcs = [(t.get("source") or "").strip() for t in translations]
        check("每一句都能在识别文本里找到（自己断句，不是乱切的）",
              all(s and s in committed for s in srcs),
              f"srcs={srcs} committed={committed[:120]!r}")
        # 收尾 flush：最后一句（通常没标点）也要译
        tail = committed.strip()[-40:]
        check("最后一句也译了（收尾 flush）",
              any(s and committed.strip().endswith(s) for s in srcs),
              f"srcs={srcs} tail={tail!r}")

    print("\n" + "=" * 72)
    print(f"通过 {_PASS} / 失败 {_FAIL}")
    print("=" * 72)
    if _FAIL:
        print("❌ 有断言没过")
        return 1
    print("✅ 全部通过")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://127.0.0.1:8000")
    ap.add_argument("--wav", default=str(DEFAULT_WAV))
    ap.add_argument("--target", default="zh-CN")
    ap.add_argument("--timeout", type=float, default=240.0)
    args = ap.parse_args()
    args.wav = Path(args.wav)
    requests.packages.urllib3.disable_warnings()  # type: ignore
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main() or 0)
