#!/usr/bin/env python3
"""流式 ASR 冒烟（活体链路：浏览器麦 start → chunk* → finish）。

为什么要有这个脚本
------------------
``scripts/verify_asr_live.py`` 是**全量**回归（49 条断言，覆盖停滞检测、
VAD、语言头拆解……），跑一次要几分钟，而且需要浏览器。日常只想确认
「板子上的 ASR 实例还活着、实时链路还通」时，用这个**几十秒**的冒烟。

它验的是**线上真正在跑的那条路**：``POST /live/start`` → 逐块
``POST /live/chunk`` → ``POST /live/finish``，与前端「开始录音」按钮
发的请求完全一致。

⚠️ **素材在 ``/tmp``（tmpfs）—— 板子重启就没了**
------------------------------------------------
缺素材时**必须报错退出**，不能静默跳过（上一轮就因为板子重启导致用例
静默 SKIP 而漏掉了问题）。先跑::

    .venv/bin/python scripts/make_asr_live_wav.py --print

判据里两个容易写错的地方（都踩过）
----------------------------------
* ``round`` 是**本段内**已凑满 1 秒的推理轮次。500 ms 一块 ⇒ 同一轮的
  两块共享同一个 round 号（``0,0,1,1,2,2…``）⇒ 判据是「**段内**单调
  不减」，不是严格递增。
* VAD 静音断句（``silence_ms=1200``）之后会**开新的一段**，round 归零
  重数 —— 这是设计行为，不是异常。

用法::

    .venv/bin/python scripts/verify_asr_live_smoke.py
    .venv/bin/python scripts/verify_asr_live_smoke.py --base http://127.0.0.1:8000
"""

import argparse
import json
import sys
import wave
from pathlib import Path

import requests

WAV = Path("/tmp/asr_ref/asr_16k_mono_s16.wav")
CHUNK_MS = 500


class Checker:
    """收集断言结果 —— 计数必须准确（上一版把「失败数」印成了 0）。"""

    def __init__(self) -> None:
        self.total = 0
        self.failed: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.total += 1
        print(("  PASS  " if ok else "  FAIL  ") + name + (("  | " + detail) if detail else ""))
        if not ok:
            self.failed.append(name)
        return ok

    def report(self) -> int:
        passed = self.total - len(self.failed)
        print()
        print("结果: %d 通过 / %d 失败（共 %d 条断言）" % (passed, len(self.failed), self.total))
        if self.failed:
            print("失败项:", self.failed)
            return 1
        return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    args = ap.parse_args(argv)
    base = args.base.rstrip("/")
    ck = Checker()

    if not WAV.exists():
        print("缺少样本: %s" % WAV)
        print("先跑: .venv/bin/python scripts/make_asr_live_wav.py --print")
        return 2

    with wave.open(str(WAV), "rb") as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        pcm = w.readframes(w.getnframes())
    dur = len(pcm) / float(sr * ch * sw)
    print("样本: %s  %dHz %dch %dbit  %.1fs" % (WAV.name, sr, ch, sw * 8, dur))

    # --- start ---
    try:
        r = requests.post(base + "/api/qwen3-asr/live/start",
                          json={"source": "browser", "sample_rate": sr,
                                "n_channels": ch, "sample_width": sw,
                                "chunk_ms": CHUNK_MS}, timeout=60)
    except requests.RequestException as e:
        print("live/start 请求失败: %s" % e)
        return 2
    print("live/start ->", r.status_code, r.text[:160])
    if r.status_code != 200:
        ck.check("live/start 建立会话", False, "HTTP %d" % r.status_code)
        return ck.report()
    sid = r.json()["stream_id"]
    ck.check("live/start 建立会话", bool(sid), "stream_id=%s" % sid)

    # --- chunk ---
    frame_bytes = sr * ch * sw
    chunk_bytes = frame_bytes * CHUNK_MS // 1000
    hdr = {"X-Asr-Stream-Id": sid, "X-Asr-Seq": "0"}
    rounds: list[int] = []
    last: dict = {}
    seq = 0
    broke = False
    try:
        for off in range(0, len(pcm), chunk_bytes):
            blk = pcm[off:off + chunk_bytes]
            if not blk:
                break
            hdr["X-Asr-Seq"] = str(seq)
            seq += 1
            try:
                rr = requests.post(base + "/api/qwen3-asr/live/chunk",
                                   data=blk, headers=hdr, timeout=60)
            except requests.RequestException as e:
                ck.check("chunk #%d 被接受" % seq, False, "请求异常 %s" % e)
                broke = True
                break
            if rr.status_code != 200:
                ck.check("chunk #%d 被接受" % seq, False,
                         "HTTP %d %s" % (rr.status_code, rr.text[:160]))
                broke = True
                break
            j = rr.json()
            rounds.append(int(j.get("round") or 0))
            last = j
        if not broke:
            ck.check("全部 %d 块被接受" % seq, True)

        # --- finish ---
        try:
            rf = requests.post(base + "/api/qwen3-asr/live/finish",
                               headers={"X-Asr-Stream-Id": sid}, timeout=60)
        except requests.RequestException as e:
            ck.check("live/finish 返回 200", False, "请求异常 %s" % e)
            return ck.report()
        print("live/finish ->", rf.status_code, rf.text[:200])
        ck.check("live/finish 返回 200", rf.status_code == 200)
        fin = rf.json() if rf.status_code == 200 else {}
    finally:
        # 收尾必须执行 —— 后端一次只允许一个实时会话，留活儿会挡住别的用例
        try:
            requests.delete(base + "/api/qwen3-asr/live/stop",
                            headers={"X-Asr-Stream-Id": sid}, timeout=30)
        except requests.RequestException:
            pass

    # --- 断言 ---
    segs: list[list[int]] = []
    cur: list[int] = []
    for r in rounds:
        if r == 0 and cur:
            segs.append(cur)
            cur = [r]
        else:
            cur.append(r)
    if cur:
        segs.append(cur)

    ck.check("至少凑满 1 秒的推理轮次", any(s and max(s) >= 1 for s in segs),
             "round 序列=%s" % rounds[:12])
    ck.check("每段内 round 单调不减",
             all(s == sorted(s) for s in segs),
             "段数=%d 各段最大=%s" % (len(segs), [max(s) for s in segs if s]))

    lang = fin.get("language") or last.get("language")
    ck.check("语种已识别", bool(lang), "language=%s" % lang)

    text = (fin.get("text") or fin.get("committed") or
            ((last.get("committed") or "") + (last.get("unfixed") or ""))).strip()
    ck.check("最终文本非空", bool(text), repr(text[:100]))
    ck.check("文本长度合理(>10 字符)", len(text) > 10, "len=%d" % len(text))

    print()
    print("=== 最终文本 ===")
    print(text[:600])
    return ck.report()


if __name__ == "__main__":
    sys.exit(main())
