#!/usr/bin/env python3
"""回归：TTS **合成队列**（``cards/tts/queue.py``）。

契约
====
1. 提交 ⇒ 进队列，深度 +1；
2. **队满 ⇒ 合并队尾**（内容不丢！深度不超过 ``QUEUE_MAX``）；
3. ``clear`` ⇒ 清掉未开始的（``cleared`` 计数对）；
4. ``set_tempo`` ⇒ 只改**未开始**的 job，且夹在 ``[1.0, 1.15]``；
5. 不可出声的目标语 ⇒ 拒（调用方据此降级为仅字幕）；
6. 空文本 ⇒ 拒；
7. **串行消费**：一次只播一句，顺序不乱。

⚠️ 本脚本**不碰 NPU**：把 ``service.get_session`` 换成一个假会话。

跑法::

    .venv/bin/python scripts/verify_tts_queue.py
"""
from __future__ import annotations

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from wdnapps_audio_chat.cards.tts import queue as q  # noqa: E402
from wdnapps_audio_chat.cards.tts import service as tts_service  # noqa: E402

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


class _FakeSession:
    """假 TTS 会话：不碰 NPU，只记录播报顺序。

    ⚠️ **签名必须跟 ``TtsSession.run_queued(job, sink=...)`` 一致** ——
    队列现在把 PCM 分片出口（``sink``）传进来。
    """

    def __init__(self) -> None:
        self.available = True
        self.init_error = ""
        self.model_dir = ""
        self.played: list[str] = []
        self.tempos: list[float] = []
        self.concurrent = 0
        self.max_concurrent = 0
        self.sink_seen = False

    def run_queued(self, job, sink=None) -> dict:
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        if sink is not None:
            # ⚠️ **故意不真的推 PCM** —— 推了就会起真的 aplay（回归脚本
            # 不该在板子出声）。队列的播放器没有 PCM 时会立刻收尾 ✓。
            self.sink_seen = True
        time.sleep(0.02)
        self.played.append(job.text)
        # 记下**这一句实际用的变速** —— 队列级变速到底有没有落到 job 上，
        # 看这里最实（消费线程一拿到 job 就已经带着 tempo 了）。
        self.tempos.append(float(getattr(job, "tempo", 1.0) or 1.0))
        self.concurrent -= 1
        return {"ok": True}


#: 假音色表（不依赖板上的模型目录 ⇒ 任何机器都能跑）。
_FAKE_CATALOG = {
    "voices": [{"id": "news_female", "kind": "ref"}],
    "languages": [{"id": x} for x in
                  ("auto", "chinese", "english", "german", "italian",
                   "portuguese", "spanish", "japanese", "korean",
                   "french", "russian")],
}


def main() -> int:
    fake = _FakeSession()
    tts_service.get_session = lambda: fake      # 不碰 NPU
    tts_service.voice_catalog = lambda model_dir=None: _FAKE_CATALOG

    # ⚠️ **本板只有一个 TTS 卡位**（npu2 是禁区）⇒ 队列恒为「单 worker
    # 串行」⇒ 下面的串行断言才有意义。**不要**再引入并行分片路径。

    print("== TTS 队列 ==")

    # 1. 提交
    r = q.submit_queued(text="今天天气很好", language="chinese")
    check("提交返回 seq", "seq" in r, f"得到 {r}")
    # ⚠️ **深度可能是 0** —— worker 抢得够快的话这句已经被取走了
    # （在途 + 已播也算「在系统里」）⇒ 断言「深度 + 在途 ≥ 1」。
    _snap0 = q.snapshot()
    check("提交后深度 + 在途 ≥ 1",
          int(_snap0["depth"]) + int(_snap0["inflight"]) >= 1,
          f"得到 depth={_snap0['depth']} inflight={_snap0['inflight']}")

    # 2. 不可出声的语种
    try:
        q.submit_queued(text="你好", language="thai")
        check("泰语被拒", False, "没抛错")
    except ValueError:
        check("泰语被拒（ValueError）", True)
    except Exception as exc:  # noqa: BLE001
        check("泰语被拒", False, f"异常类型 {type(exc).__name__}")

    # 3. 空文本
    try:
        q.submit_queued(text="   ", language="chinese")
        check("空文本被拒", False, "没抛错")
    except ValueError:
        check("空文本被拒", True)

    # 4. 队满 ⇒ 合并队尾（不丢内容）
    #
    # ⚠️ **不能断言「合并计数 ≥ 7」** —— 消费线程是**并发**的（假会话每句
    # 睡 20 ms）⇒ 提交 135 句的途中它已经抽走几句 ⇒ 合并次数取决于机器
    # 快慢（实测随机 6~7 次）⇒ 那个断言是 **flake**，不是回归点。
    #
    # ⇒ 改断言**真正的不变量**：**内容一条都不丢**（合并后的那句 =
    # 队尾文本 + 新文本 ⇒ 提交过的每一句都还在「队列里」或「已经播过」），
    # 外加「确实合并过」。
    q.clear()
    submitted = ["第{}句".format(i) for i in range(q.QUEUE_MAX + 7)]
    for t in submitted:
        q.submit_queued(text=t, language="chinese")
    snap = q.snapshot()
    check(f"队满后深度 ≤ {q.QUEUE_MAX}", snap["depth"] <= q.QUEUE_MAX,
          f"得到 {snap['depth']}")
    check("确实合并过（队满 ⇒ 合并队尾，不是丢）", snap["merged"] >= 1,
          f"得到 {snap['merged']}")
    with q.get_queue()._cond:          # 别和消费线程抢着改 _jobs
        qq = q.get_queue()
        # ⚠️ **正在播的那句**也算「没丢」—— 它已经从队列里取走了，但还没
        # 进 ``fake.played``（假会话每句睡 20 ms）⇒ 只看「队列 + 已播」
        # 会误判成丢了。
        parts = [j.text for j in list(qq._jobs)]
        if qq._playing is not None:
            parts.append(qq._playing.text)
        queued_text = " ".join(parts)
    all_text = " ".join(fake.played) + " " + queued_text
    missing = [t for t in submitted if t not in all_text]
    check("队满合并后**内容一条都不丢**", not missing,
          f"丢了 {missing[:5]}（共 {len(missing)} 句）")

    # 5. clear
    q.clear()
    check("clear 后深度 = 0", q.snapshot()["depth"] == 0,
          f"得到 {q.snapshot()['depth']}")

    # 6. 变速夹取
    check("tempo 9 夹到 1.15", q.set_tempo(9.0)["tempo"] == 1.15)
    # ⚠️ **下限是 0.9，不是 1.0**（实测 2026-11-23，M28）—— TTS 的可持续
    # 产能只有 ~0.96× ⇒ 播放必须能**慢于 1.0**，否则管道存货被吃空 ⇒
    # 收尾断音（「最后几个词隔很久才播出来」）。
    check("tempo 0.5 夹到 0.9（可慢于 1.0）",
          q.set_tempo(0.5)["tempo"] == 0.9)
    check("tempo 0.96 原样", q.set_tempo(0.96)["tempo"] == 0.96)
    check("tempo 1.08 原样", q.set_tempo(1.08)["tempo"] == 1.08)

    # 6b. **队列的变速对新提交的句子也生效**（``tempo=None`` = 继承）
    #
    # ⚠️ 之前新句一律用调用方传的默认 1.0 ⇒ 「设了 tempo 1.15，新句还是
    # 原速播」（实测：journal 里 ``aplay-writer`` 没带 ``tempo=1.150``）
    # ⇒ **变速对新句不生效** ⇒ 自动提速等于没提速。
    q.clear()
    q.set_tempo(1.15)
    # ⚠️ **先等上一批真的排空** —— 上一节塞了 135 句，消费线程还在播；
    # 它会在 ``clear`` 之后才把「正在播的那句」记进 ``fake.played`` ⇒
    # 不等就会把**上一批的 job** 当成这一批的第一个（断言下标全错）。
    deadline = time.time() + 30
    while time.time() < deadline:
        snap = q.snapshot()
        if snap["depth"] == 0 and not snap["playing"]:
            break
        time.sleep(0.05)
    fake.played.clear()
    fake.tempos.clear()
    q.submit_queued(text="继承变速", language="chinese")
    q.submit_queued(text="显式覆盖", language="chinese", tempo=1.0)
    deadline = time.time() + 15
    while time.time() < deadline and len(fake.tempos) < 2:
        time.sleep(0.05)
    check("不传 tempo ⇒ 新句**继承队列的变速**（1.15）",
          len(fake.tempos) >= 1 and abs(fake.tempos[0] - 1.15) < 0.001,
          f"得到 {fake.tempos[:2]}")
    check("显式给 tempo ⇒ 覆盖这一句（1.0）",
          len(fake.tempos) >= 2 and abs(fake.tempos[1] - 1.0) < 0.001,
          f"得到 {fake.tempos[:2]}")
    q.set_tempo(1.0)

    # 7. 串行消费 + 顺序
    q.clear()
    q.set_tempo(1.0)
    # ⚠️ 先等**上一批**播完（worker 可能正拿着第一句）⇒ 再清记录，
    # 否则「顺序不乱」的断言里混着上一批的句子。
    deadline = time.time() + 15
    while time.time() < deadline:
        snap = q.snapshot()
        if snap["depth"] == 0 and not snap["playing"]:
            break
        time.sleep(0.05)
    fake.played.clear()
    for i in range(5):
        q.submit_queued(text=f"顺序{i}", language="chinese")
    deadline = time.time() + 15
    while time.time() < deadline and len(fake.played) < 5:
        time.sleep(0.05)
    check("5 句都播了", len(fake.played) == 5, f"得到 {len(fake.played)}")
    check("顺序不乱", fake.played == [f"顺序{i}" for i in range(5)],
          f"得到 {fake.played}")
    check("同一时刻只播一句（串行）", fake.max_concurrent == 1,
          f"最大并发 {fake.max_concurrent}")
    check("PCM 分片出口已挂上（sink 传给了会话）", fake.sink_seen,
          "会话没收到 sink ⇒ 播放器拿不到分片")

    # 7b. **播放顺序 = 提交顺序**（两段流水线的 FIFO 保证）
    #
    # ⚠️ 这是**新结构**（合成线程 + 播放线程）才有的断言：合成顺序对
    # 不够 —— 必须确认**播出去**的顺序也是提交顺序（否则同传会串句）。
    deadline = time.time() + 15
    while time.time() < deadline and len(q.get_queue().played_texts()) < 5:
        time.sleep(0.05)
    played = q.get_queue().played_texts()
    check("**播放顺序 = 提交顺序**", played[-5:] == [f"顺序{i}" for i in range(5)],
          f"得到 {played[-5:]}")

    # 8. 滞后记账
    snap = q.snapshot()
    check("播完计数 = 5", snap["done"] >= 5, f"得到 {snap['done']}")

    q.shutdown()
    print(f"\n通过 {_PASS} / 失败 {_FAIL}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
