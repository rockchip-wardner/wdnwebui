"""网页翻译（NPU）吞吐基准。

用法:
    .venv/bin/python scripts/bench_web_translate.py [--repeat 3] [--segments 24]

打的是**真实链路**：POST /api/translate —— 与界面右上角「界面语言」切换
走的是同一条路径（TranslateService → provider 链 → NPU provider）。

⚠️ **缓存会把测量变成假数据**
--------------------------------
``TranslateService`` 按 ``(source, target, text)`` 记账，命中缓存的条目
**根本不调 provider**，一轮 24 条全命中时端到端只要 0.01s —— 那测的是
字典，不是 NPU。

绕缓存有两条路，本脚本只认第二条：

1. ❌ 给文案加后缀（``（1）`` / `` #1`` / `` N``）—— 实测会让 HY-MT 把短
   文案**复读回来**（成功率 87% → ~50%），整批回退外网 provider，测出来
   的还是外网的速度。
2. ✅ **轮换目标语言** —— 不同 ``target`` 就是不同的缓存键，文案保持原样，
   测到的是干净的 NPU 路径。

所以每轮都用一个**没跑过**的目标语言，并且脚本自己核对 ``cached`` 字段：
``cached > 0`` 的轮次 = 混了缓存 → **作废，不进统计**。

万一语言都用完了（缓存把该组合都占了），用
``POST /api/web-translate/disable`` → ``/enable`` 重置一下（重建服务时
缓存会一起清空，见 ``app.core.translate.set_npu_translate_enabled``）。
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.request

API = "http://127.0.0.1:8000/api/translate"
STATUS = "http://127.0.0.1:8000/api/translate/status"

#: 基准文案集 —— **NPU 能整批翻通**的 24 条真实界面文案。
#:
#: ⚠️ **不能用「所有界面文案」直接测**：只要有**一条** NPU 翻不出来
#: （例如 ``简体中文`` —— 译成英文还是中文、译成日文是纯汉字没有假名），
#: 整批 24 条就会抛 ``TranslationError`` 回退外网 —— 那一轮测的是**外网**
#: 的速度，完全测不到 NPU。
#:
#: 这 24 条是 ``scripts/probe_npu_items.py`` 逐条试出来的（NPU 单条翻译
#: 成功 = provider 报 npu）：把 ``界面语言`` / ``简体中文`` 这类
#: **语言名本身**和 ``外网翻译站`` / ``本板 NPU 翻译`` 这种超短术语剔掉了。
#:
#: ⚠️ 这也意味着：**端到端测出来的数比「NPU 纯推理」慢** —— 真实界面里
#: 混着翻不出来的条目时，整批回退外网是常态（见文件头的说明）。
SEGMENTS = [
    "正在翻译，请稍候。", "翻译失败，请检查网络。", "模型加载中，请稍候。",
    "该卡位已被其它模型占用。", "启用翻译服务", "禁用翻译服务",
    "翻译服务已启用。", "实时语音识别", "视频目标监控", "图片生成",
    "文本对话", "网页翻译", "算力配置", "系统运行正常",
    "正在读取状态，请稍候。", "服务详情", "模型实例", "并行实例",
    "计算卡", "协处理器", "已安装", "未启用", "正在启动模型", "已停止",
]

#: 轮换用的目标语言 —— 全部是 HY-MT 支持的（``LANG_CODES``）。
#:
#: ⚠️ **拉丁字母系的目标语排在前面**：文种判定对拉丁字母系很宽松（输出里
#: 有拉丁字母就算翻出来了），而 ja/ko/ru/ar 有**硬性文种要求**（日语必须
#: 有假名、韩语必须谶 Hangul…）。界面文案里混着 "English"、"简体中文" 这类
#: **语言名本身**，译成日文就是纯汉字（"英語"）—— 文种判定判它「没翻出来」
#: → 整批 24 条抛 ``TranslationError`` 回退外网 → 那一轮测的是外网速度。
#:
#: 那是另一个话题（复读/回退瓶颈），这里只负责测**吞吐**。
CYCLE = ["en", "fr", "de", "es", "pt", "it", "nl", "pl", "cs", "tr",
         "id", "vi", "ms", "uk", "ja", "ko", "ru", "ar", "he", "fa"]


def _post(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _wait_npu_ready(limit_s: int = 90) -> float:
    """等 NPU provider 的冷却过去。

    ⚠️ **必须等**：provider 链里某个 provider 抛 ``TranslationError`` 会被
    记 **60 秒冷却**（``ProviderChain.error_cooldown``），冷却期间
    ``_available()`` 把它**静默**剔除 —— 日志里一行告警都没有，但请求
    已经全走了外网 provider。不等的话，后面几轮测到的全是外网的速度。

    返回实际等待的秒数。
    """
    t0 = time.time()
    while time.time() - t0 < limit_s:
        try:
            cd = _get(STATUS).get("cooldown") or {}
        except Exception:  # noqa: BLE001
            cd = {}
        left = int(cd.get("npu") or 0)
        if left <= 0:
            return time.time() - t0
        time.sleep(min(left, limit_s - (time.time() - t0)) + 1)
    return time.time() - t0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--segments", type=int, default=24)
    ap.add_argument("--target", default="en")
    ap.add_argument("--cycle-targets", action="store_true",
                    help="每轮换一个目标语言来绕开缓存（而不是给文案加后缀）")
    ap.add_argument("--wait-cooldown", action="store_true", default=True,
                    help="轮次前等 NPU 的冷却过去（默认开；不等的轮次会静默走外网）")
    ap.add_argument("--no-wait-cooldown", dest="wait_cooldown",
                    action="store_false")
    ap.add_argument("--targets", default="",
                    help="逗号分隔的目标语言序列（覆盖默认轮换表）")
    args = ap.parse_args()

    cycle = ([t.strip() for t in args.targets.split(",") if t.strip()]
             or CYCLE)

    segs = SEGMENTS[: max(1, args.segments)]
    chars = sum(len(s) for s in segs)

    st = _get(STATUS)
    print("provider 链: %s | npu_active=%s | 缓存=%d"
          % (" → ".join(st.get("providers", [])), st.get("npu_active"),
             st.get("cache_size", 0)))

    times: list[float] = []
    providers: list[str] = []
    stale: list[int] = []
    for i in range(args.repeat):
        target = (cycle[i % len(cycle)] if args.cycle_targets
                  else args.target)
        # ⚠️ 上一轮若整批回退外网，npu 会进 60 秒冷却 —— 这一轮会**静默**
        # 走外网（日志里没有告警），测出来的是外网速度。先等冷却过去。
        if args.wait_cooldown:
            waited = _wait_npu_ready()
            if waited > 1:
                print("  （等 NPU 冷却 %.0fs）" % waited)
        t0 = time.perf_counter()
        out = _post(API, {"segments": segs, "source": "zh-CN", "target": target})
        dt = time.perf_counter() - t0
        cached = int(out.get("cached", 0))
        prov = out.get("provider")
        # 有效轮次 = 没吃缓存 **且** 真的走了 NPU
        fresh = cached == 0 and prov == "npu"
        if fresh:
            times.append(dt)
            providers.append(prov)
        else:
            stale.append(i + 1)
        print("  第 %d 次: %6.2fs  provider=%-8s target=%-3s 条数=%d failed=%d cached=%d%s"
              % (i + 1, dt, prov, target, out.get("segments", 0),
                 out.get("failed", 0), cached,
                 "" if fresh else ("  ← 缓存命中" if cached else "  ← 没走 NPU，作废")))

    if stale:
        print("\n⚠️ 第 %s 轮无效（缓存命中或没走 NPU）。换个目标语言，或先 disable→enable "
              "清缓存再测。" % stale)

    if not times:
        print("\n❌ 没有一轮是干净的 —— 这次测不出任何东西。")
        return 1

    best, med = min(times), statistics.median(times)
    if any(p != "npu" for p in providers):
        print("\n⚠️ 有轮次没走 NPU（provider=%s）—— 数字里混了外网 provider 的"
              "速度，不能当 NPU 的性能看。" % providers)
    print("\n有效轮次=%d/%d  条数=%d 字数=%d  最短=%.2fs  中位=%.2fs  最慢=%.2fs"
          % (len(times), args.repeat, len(segs), chars, best, med, max(times)))
    print("吞吐: %.2f 条/秒 (中位)  |  单条均耗 %.0f ms"
          % (len(segs) / med, med * 1000 / len(segs)))
    print("\n注：这是**端到端**（POST /api/translate）。批与批之间的 "
          "_BATCH_GAP=1.2s 间隔**只在外网 provider 生效**（那是给免费外网接口"
          "防限流用的）；NPU 干的活不 sleep —— 所以本板 NPU 的数字里**不含**"
          "那 1.2s。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
