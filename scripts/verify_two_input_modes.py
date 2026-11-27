#!/usr/bin/env python3
"""回归：**两种输入，两套逻辑**（2026-11-26 用户要求）。

用户要求
--------
「音频文件」「含音轨的视频」「板载麦克风」「浏览器麦克风」**分两种逻辑**：

1. **板载麦克风 / 浏览器麦克风** ⇒ **真实的同传逻辑**
   （说一句 → 立刻译 → 立刻念；不等字幕位置）；
2. **音频文件 / 含音轨的视频** ⇒ **配音导出**
   （ASR + 翻译 + TTS ⇒ 目标语言的音轨；视频还会烧**双字幕**并
   **重新封装**）—— **不再同传**（同传是「边识别边念」，文件类来源
   要的是**处理好的成品文件**）。

⚠️ **2026-11-26 变更**：文件类来源原本是「语音播放跟着当前字幕走」
（预合成 + 跟播放）⇒ 用户明确要求**去掉同传**、改成**导出**⇒
   本脚本 A 部分的分流判据随之改成「文件类 ⇒ 配音导出」。

覆盖
====
A. **分流判据**（前端 + 后端）：四种输入各走哪条路；
B. **预合成缓存**（队列层）：按位置索引、命中直跳、LRU 淘汰、队满不合并；
C. **播放按位置取**（防饿死）：预合成时「后合成的先合成完」不会挡住
   「此刻该播的那句」；
D. **实时流不受影响**：麦克风（live）**不开**预合成 ⇒ FIFO 顺序语义不变。
E. **过期译文不占用合成线程**（内容不丢）。

⚠️ **不占 NPU、不碰用户的会话** —— 全是纯逻辑。
⚠️ **不启动音频设备** —— 只测队列的**逻辑**，不真的出声。

跑法::

    .venv/bin/python scripts/verify_two_input_modes.py
"""
from __future__ import annotations

import os
import sys
import wave

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_PLUGIN = os.path.join(_ROOT, ".venv", "src", "wdnapps-audio-chat", "src")
if _PLUGIN not in sys.path:
    sys.path.insert(0, _PLUGIN)

import numpy as np  # noqa: E402

_OK = 0
_FAIL: list = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global _OK
    if cond:
        _OK += 1
        print(f"  \033[32mPASS\033[0m {name}")
    else:
        _FAIL.append(name)
        print(f"  \033[31mFAIL\033[0m {name} {extra}")


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


# --------------------------------------------------------------------------- #
# A. 分流判据（前端 + 后端）
# --------------------------------------------------------------------------- #
def part_a() -> None:
    section("A. 分流判据：四种输入 → 两套逻辑")
    app_js = os.path.join(
        _PLUGIN, "wdnapps_audio_chat", "cards", "qwen3_asr", "static", "app.js")
    interp_py = os.path.join(
        _PLUGIN, "wdnapps_audio_chat", "cards", "qwen3_asr", "interp.py")
    js = open(app_js, "r", encoding="utf-8").read()
    py = open(interp_py, "r", encoding="utf-8").read()

    # ---- 前端：来源 → 同传源三态 ----
    check("前端：板载麦/浏览器麦 → 同传源 live",
          'if (src === "board" || src === "browser") return "live";' in js,
          "麦克风应映射成 live")
    # ---- 前端：文件类来源 ⇒ **配音导出**，不做同传（2026-11-26）----
    check("前端：文件类来源藏起同传块",
          'interpBlock.classList.toggle("hidden", this._isFileSource());' in js)
    check("前端：文件类来源显示配音导出块",
          'dubBlock.classList.toggle("hidden", !this._isFileSource());' in js)
    check("前端：_interpStart 对文件类来源直接拒绝",
          "文件类来源不做同传" in js)
    check("前端：切到文件类来源 ⇒ 停掉同传",
          "if (this._isFileSource() && this._interp.running)" in js)
    # ---- 前端：有播放头 ⇒ 跟播放；没有 ⇒ 不等播放 ----
    check("前端：只有文件/视频有播放头",
          'return src === "file" || src === "video";' in js)
    check("前端：没有播放头 ⇒ gate=now（真同传不等字幕）",
          'if (!this._interpHasPlayhead()) return "now";' in js)

    # ---- 前端：麦克风自动开同传（真同传）----
    check("前端：麦克风来源自动开同传",
          "_autoStartInterpForMic" in js and
          "await this._autoStartInterpForMic();" in js)
    check("前端：自动开同传只对麦克风两类生效",
          'if (src !== "board" && src !== "browser") return;' in js)
    check("前端：浏览器麦启动后自动开同传",
          "await this._liveOpenMic();" in js and
          "await this._autoStartInterpForMic();" in js)
    check("前端：板载麦启动后自动开同传",
          "this._boardState = d;" in js and
          "await this._autoStartInterpForMic();" in js)

    # ---- 前端：麦克风下不再空转前端驱动的行级翻译 ----
    check("前端：麦克风不走前端驱动的行级翻译",
          'if (src === "board" || src === "browser") return;' in js)

    # ---- 后端：预合成模式按来源开 ----
    check("后端：预合成只对 file/video 开",
          'self._prefetch = self._source in ("file", "video")' in py)
    check("后端：引擎认 live/file/video 三态",
          'self._source = "live"            # live | file | video' in py)


# --------------------------------------------------------------------------- #
# B. 预合成缓存（队列层）
# --------------------------------------------------------------------------- #
def part_b() -> None:
    section("B. 预合成音频缓存：按位置索引 / 命中直跳 / LRU / 队满不合并")
    from wdnapps_audio_chat.cards.tts.queue import (
        _AudioCache,
        TTSJob,
        TTSQueue,
    )
    import tempfile

    tmp = tempfile.mkdtemp(prefix="wdn-prefetch-test-")

    # ---- 缓存本身 ----
    # ⚠️ **上限要给够**（单句 WAV ≈ 48 KB）⇒ 否则刚 put 就被淘汰 ⇒
    # 测的就不是「能不能命中」而是「会不会被淘汰」了。
    cache = _AudioCache(max_bytes=4 * 1024 * 1024, root=tmp)
    p1 = os.path.join(tmp, "a.wav")
    with wave.open(p1, "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(24000)
        fh.writeframes(np.zeros(24000, dtype=np.int16).tobytes())
    cache.put("s@1.00", p1, 1.0)
    hit = cache.get("s@1.00")
    check("缓存：按位置索引能命中", hit is not None and hit[1] == 1.0,
          f"得到 {hit}")
    check("缓存：没命中返回 None", cache.get("s@9.99") is None)
    check("缓存：has() 判存在", cache.has("s@1.00") and
          not cache.has("s@9.99"))

    # ---- LRU 淘汰 ----
    cache2 = _AudioCache(max_bytes=10, root=tmp)
    for i in range(4):
        pp = os.path.join(tmp, "b%d.wav" % i)
        with wave.open(pp, "wb") as fh:
            fh.setnchannels(1)
            fh.setsampwidth(2)
            fh.setframerate(24000)
            fh.writeframes(np.zeros(24000, dtype=np.int16).tobytes())
        cache2.put("k%d" % i, pp, 1.0)
    st = cache2.stats()
    check("缓存：超容量 ⇒ LRU 淘汰（条目变少）",
          st["entries"] < 4, f"得到 {st}")
    # ⚠️ **不留「至少一条」的底线**（2026-11-26）—— 底线会让「单个文件就
    # 超过上限」时永远超限 ⇒ 缓存能无限涨 ⇒ eMMC 撑爆。
    check("缓存：淘汰后字节数回到上限内",
          st["bytes"] <= st["max_bytes"], f"得到 {st}")
    check("缓存：单文件超上限 ⇒ 删光（磁盘占用有硬上限）",
          st["entries"] == 0, f"得到 {st}")

    # ---- 队列：cache_key 落到 job 上 ----
    q = TTSQueue()
    job = TTSJob(seq=1, text="你好")
    check("队列：TTSJob 有 cache_key 字段",
          hasattr(job, "cache_key") and job.cache_key == "")
    check("队列：TTSJob 有 cached_audio_sec 字段",
          hasattr(job, "cached_audio_sec"))

    # ---- 队列：提交时带 cache_key ⇒ job 上有 ----
    try:
        q.submit_queued("测试", language="chinese", cache_key="s@12.34")
    except Exception as exc:  # noqa: BLE001
        # ⚠️ TTS 没跑时可能抛 ⇒ 只关心**参数通路**，下面用静态检查兜底。
        check("队列：submit_queued 接受 cache_key", False, str(exc))
    else:
        with q._cond:
            j = q._jobs[-1] if q._jobs else None
        check("队列：submit_queued 接受 cache_key", True)
        check("队列：cache_key 落到 job 上",
              j is not None and j.cache_key == "s@12.34",
              f"得到 {getattr(j, 'cache_key', None)!r}")

    # ---- 缓存命中 ⇒ 直接取 PCM（跳过合成）----
    wav_path = os.path.join(tmp, "hit.wav")
    pcm = (np.sin(np.arange(24000) * 0.05) * 0.5 * 32767).astype(np.int16)
    with wave.open(wav_path, "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(24000)
        fh.writeframes(pcm.tobytes())
    q._cache.put("s@1.00", wav_path, 1.0)
    got = q._cache_load_pcm("s@1.00")
    check("缓存命中 ⇒ 能读回 PCM",
          got is not None and got.size == 24000, f"得到 {None if got is None else got.size}")
    check("缓存未命中 ⇒ 读回 None", q._cache_load_pcm("s@99.00") is None)

    # ---- 队满合并 ⇒ 丢弃缓存键（否则会播错内容）----
    check("队满合并 ⇒ 丢弃缓存键",
          "job.cache_key = \"\"" in open(
              os.path.join(_PLUGIN, "wdnapps_audio_chat", "cards", "tts",
                           "queue.py"), "r", encoding="utf-8").read())

    # ---- 统计接口 ----
    check("队列：cache_stats 可用",
          isinstance(q.cache_stats(), dict) and
          "entries" in q.cache_stats())
    check("队列：cache_has 可用", q.cache_has("s@1.00") is True)
    check("队列：set_prefetch_mode 可用",
          q.set_prefetch_mode(True).get("prefetch") is True and
          q.prefetch_mode() is True)
    q.set_prefetch_mode(False)


# --------------------------------------------------------------------------- #
# E. 过期译文不占用合成线程（2026-11-26 真机实测）
# --------------------------------------------------------------------------- #
def part_e() -> None:
    section("E. 预合成模式：过期译文不占用合成线程（内容不丢）")
    from wdnapps_audio_chat.cards.qwen3_asr.interp import (
        PREFETCH_MAX_LAG_SEC,
        InterpEngine,
        InterpSegment,
    )

    check("过期容差常量存在且 > 0", PREFETCH_MAX_LAG_SEC > 0,
          f"得到 {PREFETCH_MAX_LAG_SEC}")

    eng = InterpEngine()
    eng._prefetch = True
    eng._source = "video"
    eng._gate_mode = "follow"
    # ⚠️ **播放头在 20 s** ⇒ 位置 1.06 的那句**过期**了（落后 19 s）。
    eng._gate._playhead["vid"] = 20.0
    eng._gate._last_ph = (20.0, True)

    def _seg(idx: int, pos: float) -> InterpSegment:
        return InterpSegment(index=idx, text="句子%d" % idx, lang="zh",
                             stream_id="vid", mode="steady",
                             src_start_sec=pos, translated="句子%d" % idx)

    stale = _seg(1, 1.06)
    # ⚠️ **新鲜 = 位置在播放头前方**（预合成就是提前合成）⇒ 用 29.06。
    fresh = _seg(2, 29.06)

    # ---- ① 有新鲜的 ⇒ 只放新鲜的（过期的让位，别堵线程）----
    out = eng._within_window([stale, fresh])
    check("有新鲜句子 ⇒ **过期的这句不提交**（不堵合成线程）",
          fresh in out and stale not in out,
          f"放出 {[getattr(s, 'index', None) for s in out]}")
    check("过期的这句**没被丢掉**（还在待放行里，引擎自己管）",
          eng._stale_skipped >= 1, f"得到 {eng._stale_skipped}")

    # ---- ② 全都过期 ⇒ 一句都不放（否则又去堵线程了）----
    out2 = eng._within_window([stale])
    check("全都过期 ⇒ **一句都不放**（等新鲜的来）",
          out2 == [], f"放出 {[getattr(s, 'index', None) for s in out2]}")

    # ---- ③ 源读完了 ⇒ 剩下的全放（补播，内容不丢）----
    eng._gate._last_done = True
    out3 = eng._within_window([stale])
    check("源读完了 ⇒ 过期的那句**也放**（补播，内容不丢）",
          stale in out3, f"放出 {[getattr(s, 'index', None) for s in out3]}")

    # ---- ④ 实时流（live）⇒ 根本不走这个逻辑 ----
    eng2 = InterpEngine()
    eng2._prefetch = False
    eng2._source = "live"
    out4 = eng2._within_window([stale])
    check("实时流 ⇒ 过期过滤**不生效**（原样放行）",
          stale in out4, f"放出 {[getattr(s, 'index', None) for s in out4]}")

    # ---- ⑤ 预合成模式 ⇒ **一句一个作业**（不合并）----
    #
    # ⚠️ **合并的作业跟预合成天然冲突**（2026-11-26 真机实测）：
    #   * 缓存按「位置」索引 ⇒ 合并出来的作业**不对应任何单一位置** ⇒
    #     缓存键是错的 ⇒ 拖动后命中不了；
    #   * 播放按「位置」放行 ⇒ 合并作业的位置取**第一句** ⇒ 后面那几句的
    #     译文会**提前播**（实测一批 3 句 ⇒ 后面两句提前 ~11 秒 ⇒ 正是
    #     用户说的「译文跟不上当前字幕」）。
    src = open(os.path.join(_PLUGIN, "wdnapps_audio_chat", "cards", "qwen3_asr",
                             "interp.py"), "r", encoding="utf-8").read()
    qsrc = open(os.path.join(_PLUGIN, "wdnapps_audio_chat", "cards", "tts",
                              "queue.py"), "r", encoding="utf-8").read()
    check("预合成模式 ⇒ **不用攒批**（直接放）",
          "if self._prefetch:\n            return True" in src)
    check("预合成模式 ⇒ 播放通道**一句一个作业**（`self._submit([seg])`）",
          "self._submit([seg])" in src)
    check("预合成模式 ⇒ **队满不合并**（队列侧拒收）",
          "if self._no_merge:" in qsrc and '"rejected": True' in qsrc)

    # ---- ⑥ ⚠️⚠️ **双通道架构**（2026-11-26，用户实测三连击）----
    #
    # ⚠️ **为什么必须有两条通道**（用户实测：「TTS 播报明显比视频的实时语音
    # 字幕更慢」「为什么变速一直是 0.90」「前后拖动进度条，TTS 播报也跟不上
    # 字幕」）：
    #
    # * **播放队列**是给**实时流**设计的 ⇒ 它满的时候会**把队尾和新句拼成
    #   一段** ⇒ 预合成把整段素材灌进去 ⇒ 队列爆（实测 depth=128 /
    #   merged=941 / 排队 **8.3 小时**音频）⇒ 单段音频几百秒 ⇒ 声音彻底
    #   跟不上字幕；
    # * ⇒ **后台预热**（只合成进缓存、不排队播放）与**播放**（只送播放头附近
    #   的那几句）**必须分开**。
    section("F. 双通道：后台预热（只落缓存） + 播放（只送播放头附近）")
    check("队列：**独立的后台队列**（`_bg_jobs`）",
          "self._bg_jobs: List[TTSJob] = []" in qsrc)
    check("队列：作业带**后台标记**（`background`）",
          "background: bool = False" in qsrc)
    check("队列：后台作业**不进播放 FIFO**",
          "if not job.background:" in qsrc)
    check("队列：**前台优先**（先取播放队列，再取后台队列）",
          "if self._jobs:" in qsrc and "job = self._bg_jobs.pop(0)" in qsrc)
    check("引擎：**播放通道**只送播放头附近的（`_foreground_candidates`）",
          "_foreground_candidates" in src)
    check("引擎：**背景通道**跟着用户走（`_background_candidate`）",
          "_background_candidate" in src)
    check("引擎：放行线程**分派到双通道**（`_gate_worker_prefetch`）",
          "self._gate_worker_prefetch(pending)" in src)

    # ---- ⑦ ⚠️ **预合成模式 ⇒ 变速固定 1.0**（用户实测「变速一直 0.90」）----
    #
    # ⚠️ **为什么**：那套「按产能限速」是给**实时流**的（怕缓冲被吃空 ⇒
    # 断音）；预合成的音频**提前合成好** ⇒ 命中缓存 ⇒ 不存在断音 ⇒
    # ⇒ 限速只剩代价：声音**比字幕慢 10%**（5 分钟落后 30 秒）。
    check("预合成模式 ⇒ 变速**不被产能上限压**",
          "if need > 0.0 and measured and not self._prefetch:" in src)
    check("预合成模式 ⇒ 变速**不被缓冲余量档压**",
          "if self._prefetch:" in src and "want = TEMPO_DEFAULT" in src)

    # ---- ⑧ ⚠️ **seek 不清空后台预热**（否则每拖一次就白做）----
    check("seek ⇒ **不清后台预热作业**（`clear(include_background=False)`）",
          "self.clear(include_background=False)" in qsrc)

    # ---- ⑨ ⚠️⚠️ **变速的「真实值」必须是队列的**（2026-11-26 真 bug）----
    #
    # **实测症状**：队列侧 ``tempo=0.9``、引擎侧 ``tempo=1.0`` ⇒
    # **永久不同步** ⇒ 预合成模式下**播放实际仍慢 10%** ⇒ 用户报的
    # 「变速一直是 0.90 / TTS 比字幕慢」**根本没被修掉**。
    #
    # **根因**：引擎的死区判定用的是**自己的计数器** ⇒ 一旦队列被外部
    # 改过（``POST /queue/tempo``、跨脚本污染）或推送被异常吞掉 ⇒
    # 计数器说「已经是 want」⇒ 判定「不用改」⇒ **队列再也纠正不回来**。
    #
    # ⇒ 判定必须读**队列的真实值** ⇒ 这样**推送失败也会自动重试**（自愈）。
    check("引擎：死区判定读**队列真实值**（不是自己的计数器）",
          'cur = float(st.get("tempo") or self._counters.tempo or 1.0)' in src)
    check("引擎：推送失败 ⇒ 计数器**还原** ⇒ 下一轮会再试（自愈）",
          "self._counters.tempo = cur" in src)

    # ---- ⑩ ⚠️ **行为断言**：预合成模式下变速**物理上**只能是 1.0 ----
    #
    # ⚠️ 这两条是**行为**断言（真跑队列），不是文本匹配 ⇒ 能真的抓住
    # 「预合成下变速被压」这个 bug。
    from wdnapps_audio_chat.cards.tts.queue import TTSQueue

    q = TTSQueue()
    try:
        # 模拟「从实时流切进来」：队列里留着按产能限速的 0.9
        q.set_tempo(0.9)
        q.set_prefetch_mode(True)
        check("预合成模式 ⇒ **一进模式就把变速拉回 1.0**",
              abs(float(q.snapshot().get("tempo") or 0.0) - 1.0) < 1e-6,
              f"得到 {q.snapshot().get('tempo')}")
        # 外部再来一次（HTTP 端点 / 别的脚本）
        q.set_tempo(0.9)
        check("预合成模式 ⇒ **外部改不动变速**（夹回 1.0）",
              abs(float(q.snapshot().get("tempo") or 0.0) - 1.0) < 1e-6,
              f"得到 {q.snapshot().get('tempo')}")
    finally:
        q.shutdown()


# --------------------------------------------------------------------------- #
# C. 播放按位置取（防饿死）
# --------------------------------------------------------------------------- #
def part_c() -> None:
    section("C. 预合成模式：播放按「播放头位置」取（不饿死）")
    from wdnapps_audio_chat.cards.tts.queue import TTSJob, TTSQueue

    q = TTSQueue()
    # ⚠️ **模拟预合成后的 FIFO**：先合成完的是「位置靠后」的那句
    #    （内容短 ⇒ 先合成完）⇒ FIFO 队头是**还没到播放位置**的那段。
    q._fifo.append((TTSJob(seq=1, text="后", src_start_sec=100.0),
                    np.zeros(10, dtype=np.float32), 0.0))
    q._fifo.append((TTSJob(seq=2, text="前", src_start_sec=10.0),
                    np.zeros(10, dtype=np.float32), 0.0))

    # ---- 没有播放头 ⇒ 按 FIFO（老行为）----
    q._clock_provider = None
    got = q._fifo_pop(timeout=0.05, reserve=0.0)
    check("无播放头 ⇒ 照旧按 FIFO（队头先出）",
          got is not None and got[0].text == "后",
          f"得到 {None if got is None else got[0].text!r}")

    # ---- 有播放头 + 预合成模式 ⇒ 挑「位置已到」的那句 ----
    q._fifo.clear()
    q._fifo.append((TTSJob(seq=1, text="后", src_start_sec=100.0),
                    np.zeros(10, dtype=np.float32), 0.0))
    q._fifo.append((TTSJob(seq=2, text="前", src_start_sec=10.0),
                    np.zeros(10, dtype=np.float32), 0.0))
    q._clock_provider = lambda: 12.0
    q._prefetch_mode = True
    got = q._fifo_pop(timeout=0.05, reserve=0.0)
    check("预合成模式 ⇒ 挑「位置已到播放头」的那句（不被队头挡住）",
          got is not None and got[0].text == "前",
          f"得到 {None if got is None else got[0].text!r}")
    check("挑出来的位置 <= 播放头",
          got is not None and float(got[0].src_start_sec) <= 12.0)

    # ---- ⚠️⚠️ **同位置 ⇒ 必须取「第一个」分片**（2026-11-26 实测踩坑）----
    #
    # 真实 FIFO 里**同一句的多个音频分片 + 结束标记（chunk=None）共享
    # 同一个 ``src_start_sec``** ⇒ 若挑选时用 ``>=``（最后一个胜出）⇒
    # **结束标记被第一个取走** ⇒
    #   * ``_finish_job`` 在**一个字都没播**时就结算 ⇒ ``t_play = 0``
    #     ⇒ 面板/回归统计的「出声句数」**严重偏少**；
    #   * 这一句的音频分片还留在 FIFO 里 ⇒ 后面**倒着播**
    #     （chunk N → N-1 → … → 1）⇒ 用户听到的是**反的**译文。
    #
    # ⇒ 断言：**先挑到的必须是音频分片，不能是结束标记**，
    #    且**分片顺序必须是 FIFO 顺序**（不倒序）。
    q._fifo.clear()
    job_multi = TTSJob(seq=9, text="多片", src_start_sec=10.0)
    q._fifo.append((job_multi, np.zeros(4, dtype=np.float32), 0.0))
    q._fifo.append((job_multi, np.zeros(4, dtype=np.float32), 0.0))
    q._fifo.append((job_multi, None, 0.0))          # ← 结束标记
    q._clock_provider = lambda: 12.0
    q._prefetch_mode = True
    got1 = q._fifo_pop(timeout=0.05, reserve=0.0)
    check("同位置 ⇒ 第一个挑到的是**音频分片**（不是结束标记）",
          got1 is not None and got1[1] is not None,
          f"得到 chunk={None if got1 is None else ('None(结束标记)' if got1[1] is None else 'ndarray')}")
    got2 = q._fifo_pop(timeout=0.05, reserve=0.0)
    check("同位置 ⇒ 第二个挑到的还是音频分片（顺序不倒）",
          got2 is not None and got2[1] is not None,
          f"得到 chunk={None if got2 is None else ('None(结束标记)' if got2[1] is None else 'ndarray')}")
    got3 = q._fifo_pop(timeout=0.05, reserve=0.0)
    check("同位置 ⇒ **结束标记最后才出**",
          got3 is not None and got3[1] is None,
          f"得到 chunk={None if got3 is None else ('None(结束标记)' if got3[1] is None else 'ndarray')}")
    q._prefetch_mode = False
    q._clock_provider = None

    # ---- 预合成模式关 ⇒ 回到 FIFO ----
    q._fifo.clear()
    q._fifo.append((TTSJob(seq=1, text="后", src_start_sec=100.0),
                    np.zeros(10, dtype=np.float32), 0.0))
    q._fifo.append((TTSJob(seq=2, text="前", src_start_sec=10.0),
                    np.zeros(10, dtype=np.float32), 0.0))
    q._prefetch_mode = False
    got = q._fifo_pop(timeout=0.05, reserve=0.0)
    check("预合成模式关 ⇒ 回到 FIFO（队头先出）",
          got is not None and got[0].text == "后",
          f"得到 {None if got is None else got[0].text!r}")
    q._prefetch_mode = False
    q._clock_provider = None


# --------------------------------------------------------------------------- #
# D. 实时流不受影响
# --------------------------------------------------------------------------- #
def part_d() -> None:
    section("D. 实时流（麦克风）不受预合成影响")
    from wdnapps_audio_chat.cards.tts.queue import TTSJob, TTSQueue

    q = TTSQueue()
    # ⚠️ **实时流的 job 没有位置** ⇒ 不该进缓存、也不该被位置挑选。
    job = TTSJob(seq=1, text="实时", src_start_sec=-1.0)
    check("实时流 job 没有缓存键", job.cache_key == "")
    q._prefetch_mode = True
    q._clock_provider = lambda: 12.0
    q._fifo.append((job, np.zeros(10, dtype=np.float32), 0.0))
    picked = q._pick_by_position()
    check("实时流（位置未知）⇒ 不参与位置挑选",
          picked is None, f"得到 {picked}")
    q._prefetch_mode = False
    q._clock_provider = None

    # ⚠️ **引擎侧**：live 源 ⇒ 预合成关 ⇒ 提交窗口不过滤（真同传低延迟）
    from wdnapps_audio_chat.cards.qwen3_asr.interp import InterpEngine
    eng = InterpEngine()
    check("引擎：默认（live）不开预合成", eng._prefetch is False)
    # ⚠️ **不真的 start**（会起线程 / 拉模型）⇒ 只验证赋值的判据。
    src_py = open(
        os.path.join(_PLUGIN, "wdnapps_audio_chat", "cards", "qwen3_asr",
                     "interp.py"), "r", encoding="utf-8").read()
    check("引擎：预合成判据是 file/video",
          'self._prefetch = self._source in ("file", "video")' in src_py)


def main() -> int:
    print("\033[1m两种输入、两套逻辑（2026-11-26）的回归\033[0m")
    part_a()
    part_b()
    part_c()
    part_d()
    part_e()
    print("\n" + "=" * 62)
    if _FAIL:
        print("\033[31mFAILED\033[0m  %d 通过 / %d 失败" % (_OK, len(_FAIL)))
        for f in _FAIL:
            print("  - %s" % f)
        return 1
    print("\033[32mALL PASS\033[0m  %d 条断言全过" % _OK)
    return 0


if __name__ == "__main__":
    sys.exit(main())
