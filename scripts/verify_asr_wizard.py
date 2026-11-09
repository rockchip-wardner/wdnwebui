#!/usr/bin/env python3
"""Qwen3-ASR 卡片的向导回归 —— 用真浏览器走一遍向导。

用户反馈：「流式语音识别」的向导界面**怪怪的**。本脚本把向导的**契约**
钉死，顺便验证「视频文件（含音轨）」这条新链路：

  W1 env 步渲染出硬件 / 软件能力行（swcap_audio_asr）
  W2 音频源步有**文本框 + 浏览… + 上传…**三个控件（设备端路径 + 本机上传）
  W3 音频源步有「显示模式」下拉（实时单行 / 逐轮明细）
  W4 **没有** vtl 那套 gscore / 判帧 / 阈值 / 帧间隔字段
     —— ASR 每一段音频都要解析，没有「跳过某些段」这回事
  W5 识别参数步是 ASR 自己的四个流式参数，且默认值 = C++ demo 的常量
  W6 走完向导 → **真的发起了识别**（页面出现逐轮结果），不是「向导完成但什么都没跑」
  W7 设备端路径走视频文件（mp4）也能识别，且文本与 C demo 一致
  U17 拖动进度条 → 识别**跟过去** → 字幕显示拖动位置的内容
     （含：已识别区间的记账 / 多段识别的时间轴排序 / 重开识别的起点）

用法：
    .venv/bin/python scripts/verify_asr_wizard.py            # 需要本机服务在跑
    .venv/bin/python scripts/verify_asr_wizard.py --headful  # 看着它点

依赖：playwright + chromium（本机已装）。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
# 让 ``app.core.audio.capture`` 可导入（V6a 要用板载那套重采样做基准）
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from playwright.sync_api import sync_playwright
except Exception as exc:  # noqa: BLE001
    print(f"[SKIP] playwright 不可用: {exc}")
    sys.exit(0)

BASE = os.environ.get("WDNWEBUI_BASE", "http://127.0.0.1:8000")
APP = "qwen3-asr"
NS = "wd-wizard-"

WAV = ("/userdata/rknn3/rknn3-model-zoo/install/rk3588_linux_aarch64/"
       "rknn_Qwen3_ASR_demo/asr_en.wav")
MP4 = "/tmp/asr_ref/asr_real_audio.mp4"

C_FINAL = (
    "Uh huh. Oh yeah, yeah! He wasn't even that big when I started listening to "
    "him, but in his solo music, didn't do overly well, but he did very well when "
    "he started writing for other people."
)

PASSED = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}" + (f"  ({detail})" if detail else ""))


def goto_home(page) -> None:
    """回到应用面板（首页的卡片网格），并确认它真的可见。

    应用面板的预览视频必须在**面板可见时**验 —— 隐藏容器里的
    ``<video>`` 浏览器可能只加载 metadata 不解码，判据会飘。
    """
    page.goto(BASE, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_function("() => window.ROUTER", timeout=20000)
    # boot-restore 会把 hash 改回「上次进入的卡片」—— 等它跑完再切
    page.wait_for_timeout(3000)
    page.evaluate("() => { window.location.hash = '#apps'; }")
    page.wait_for_timeout(1200)
    try:
        page.wait_for_function(
            """() => {
                const el = document.getElementById('page-apps');
                return !!el && !el.classList.contains('hidden');
            }""", timeout=15000)
    except Exception:
        raise AssertionError("回不到应用面板（首页卡片网格）")


def open_wizard(page) -> None:
    page.goto(BASE, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_function(
        "() => window.WizardState && window.ROUTER && window.WizardRegistry",
        timeout=20000)
    # 清掉「已完成 / 上次停留步骤」的痕迹，保证每次都从 env 步开始
    page.evaluate("""(id) => {
        const base = 'wdnwebui.wizard.' + id + '.';
        for (const k of Object.keys(localStorage))
            if (k.startsWith(base)) localStorage.removeItem(k);
    }""", APP)
    # boot-restore 会把 hash 改回「上次进入的卡片」—— 必须等它跑完，
    # 否则我们设的 hash 会被它覆盖掉，页面上停留的是另一张卡
    # （症状：向导浮在别的卡片下面，截图里全是 vtl 的控件）。
    page.wait_for_timeout(2500)
    goto_card(page, APP)
    page.evaluate("(id) => { window.enterWizardMode(id); }", APP)
    page.wait_for_selector(f"#{NS}root-{APP}:not(.hidden)", timeout=20000)
    page.wait_for_timeout(500)


def goto_card(page, app_id: str) -> None:
    """切到某张卡片页，并确认路由真的切过去了。

    注意：**不能**等 ``#page-<id>`` 可见 —— 向导挂载时本来就会把原
    卡片页藏起来（``WizardState.mount`` 里 ``pageSection.classList.add
    ("hidden")``），向导走完 ``unmount()`` 才把它放回来。所以这里只
    断言「页面段存在」+「路由 currentPage() 就是它」。
    """
    page.evaluate("(id) => { window.location.hash = '#' + id; }", app_id)
    page.wait_for_timeout(600)
    try:
        page.wait_for_function(
            """(id) => {
                const el = document.getElementById('page-' + id);
                return !!el && window.ROUTER
                    && window.ROUTER.currentPage() === id;
            }""", arg=app_id, timeout=15000)
    except Exception:
        cur = page.evaluate("() => (location.hash || '')")
        rp = page.evaluate(
            "() => (window.ROUTER ? window.ROUTER.currentPage() : 'no-router')")
        has = page.evaluate(
            """(id) => !!document.getElementById('page-' + id)""", app_id)
        raise AssertionError(
            f"切不到卡片页 {app_id}（hash={cur!r}, router={rp!r}, "
            f"section_exists={has}）")
    page.wait_for_timeout(300)


def step_title(page) -> str:
    return page.evaluate(
        f"() => {{ const e = document.querySelector('#{NS}root-{APP} "
        f".{NS}step-title'); return e ? e.textContent.trim() : ''; }}")


def field_ids(page) -> list[str]:
    return page.eval_on_selector_all(
        f"#{NS}root-{APP} [data-field-id]",
        "els => els.map(e => e.dataset.fieldId)")


def field_labels(page) -> list[str]:
    return page.eval_on_selector_all(
        f"#{NS}root-{APP} .{NS}field-label",
        "els => els.map(e => e.textContent.trim())")


def click_next(page) -> None:
    page.click(f"#{NS}root-{APP} #{NS}next-btn")
    page.wait_for_timeout(450)


def set_field(page, fid: str, value: str) -> None:
    page.fill(f'#{NS}root-{APP} [data-field-id="{fid}"]', value)
    page.dispatch_event(
        f'#{NS}root-{APP} [data-field-id="{fid}"]', "change")


def _find_chrome() -> str | None:
    """找一个能用的 chromium。

    venv 里的 playwright 版本不一定和 ~/.cache/ms-playwright 里下载的
    browser 版本对得上（playwright 按自己的 revision 找
    ``chromium_headless_shell-<rev>``，对不上就报 "Executable doesn't
    exist"）。与其让整个回归因为版本号漂移跑不起来，不如扫一遍候选。
    """
    env = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    cands: list[str] = []
    if env:
        cands.append(env)
    cache = Path.home() / ".cache" / "ms-playwright"
    if cache.is_dir():
        for d in sorted(cache.glob("chromium-*"), reverse=True):
            cands.append(str(d / "chrome-linux-arm64" / "chrome"))
            cands.append(str(d / "chrome-linux" / "chrome"))
    cands += ["/usr/bin/chromium", "/usr/bin/chromium-browser",
              "/usr/bin/google-chrome"]
    for c in cands:
        if c and Path(c).is_file():
            return c
    return None


def api_state() -> dict:
    """读后端快照（绕开浏览器的时序问题）。"""
    with urllib.request.urlopen(BASE + "/api/qwen3-asr/", timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


# --------------------------------------------------------------------------- #
# 实时（麦克风）链路的公共设施
# --------------------------------------------------------------------------- #
#: 实时链路的 **16 kHz** 测试音频 —— 文件路径（参考）的输入。
LIVE_WAV16 = "/tmp/asr_ref/asr_16k_mono_s16.wav"

#: 实时链路的 **48 kHz** 测试音频 —— 浏览器假麦克风的输入（Chromium 的
#: 假设备**固定**以 48 kHz 播放），也是板载 ``--file`` 的输入（模拟
#: ALSA 采集设备的原生采样率）。
LIVE_WAV48 = "/tmp/asr_ref/asr_48k_mono_s16.wav"

#: feature flag 所在的配置文件。
CONFIG_PATH = (os.environ.get("WDNWEBUI_CONFIG")
               or "/userdata/wdnwebui/config.jsonc")


def api(method: str, path: str, body=None, headers=None,
        timeout: int = 300) -> tuple:
    """打一个 API，返回 ``(status, json)``。"""
    req = urllib.request.Request(BASE + path, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return e.code, {"detail": raw[:200]}


def set_live_flag(enabled: bool) -> bool:
    """热切换 feature flag（``config.jsonc`` → ``asr.live.enabled``）。

    flag 是**每次重新读文件**的（1 秒 TTL 缓存）→ 改完等 1.5 秒就生效，
    不用重启服务。
    """
    p = Path(CONFIG_PATH)
    try:
        txt = p.read_text(encoding="utf-8")
    except OSError as e:
        print(f"  [WARN] 读不到 {CONFIG_PATH}: {e}")
        return False
    pat = re.compile(
        r'("asr"\s*:\s*\{\s*"live"\s*:\s*\{\s*"enabled"\s*:\s*)(true|false)')
    new, n = pat.subn(
        lambda m: m.group(1) + ("true" if enabled else "false"), txt)
    if n == 0:
        print(f"  [WARN] {CONFIG_PATH} 里没有 asr.live.enabled 段")
        return False
    p.write_text(new, encoding="utf-8")
    time.sleep(1.6)          # 等 TTL 过期
    return True


def live_start(source: str = "board") -> tuple:
    """开一个实时会话。"""
    return api("POST", "/api/qwen3-asr/live/start",
               json.dumps({"source": source, "sample_rate": 16000,
                           "n_channels": 1, "sample_width": 2,
                           "chunk_ms": 500}).encode("utf-8"),
               {"Content-Type": "application/json"})


def live_chunk(stream_id: str, seq: int, pcm: bytes) -> tuple:
    """送一块 PCM。"""
    return api("POST", "/api/qwen3-asr/live/chunk", pcm,
               {"Content-Type": "application/octet-stream",
                "X-Asr-Stream-Id": stream_id,
                "X-Asr-Seq": str(seq)})


def read_wav_pcm(path: str, expect_rate: int | None = None) -> bytes:
    """读 WAV 的裸 PCM（只支持单声道 / s16）。"""
    import wave

    with wave.open(path, "rb") as f:
        if (f.getnchannels(), f.getsampwidth()) != (1, 2):
            raise AssertionError("实时测试音频必须是单声道 s16")
        if expect_rate is not None and f.getframerate() != expect_rate:
            raise AssertionError(
                "实时测试音频必须是 {} Hz（实际 {}）".format(
                    expect_rate, f.getframerate()))
        return f.readframes(f.getnframes())


def wait_run_done(req_before: int, timeout: int = 150) -> dict:
    """等**这一次**识别真的跑完。

    判据用后端的 ``current_request_id``：每次 ``transcribe`` 都会 +1，
    所以「id 变大了 且 state 落到 done」精确对应「这一次跑完了」。

    不能用「页面上出现非空文本」当判据 —— 流式识别的已确认文本会
    **逐轮增长**（第 7 轮就已经有 "…listening to him," 了），中途取值
    必然比最终结果短；也不能用徽章状态 —— 上一轮跑完后徽章就停在
    「完成」，页面重载后会被 stale 快照骗过去。
    """
    deadline = time.time() + timeout
    snap: dict = {}
    while time.time() < deadline:
        time.sleep(0.6)
        try:
            snap = api_state()
        except Exception:  # noqa: BLE001 — 服务短暂不可用就再等
            continue
        rid = snap.get("current_request_id") or 0
        if rid > req_before and snap.get("state") in ("done", "error", "stopped"):
            return snap
    return snap


def dom_transcript(page) -> str:
    """页面上显示的已确认文本（用户实际看到的东西）。"""
    return page.evaluate(
        "() => { const e = document.getElementById('asr-live-committed');"
        "return e ? e.textContent.trim() : ''; }")


#: 设备端视频播放用的样本 —— uploads 目录里挑一个**小**的 mp4。
#:
#: 只要能**解码出画面**就行，不要求有音轨（带音轨的那些都是 100 MB+ 的
#: 真实素材，回归里不该去拖它们）。
def _pick_sample_mp4() -> str:
    try:
        from app.core.config import default_resource_root
        d = default_resource_root() / "uploads"
    except Exception:  # noqa: BLE001
        return ""
    try:
        cands = [p for p in d.glob("*.mp4")
                 if p.is_file() and p.stat().st_size < 20 * 1024 * 1024]
    except OSError:
        return ""
    if not cands:
        return ""
    return str(min(cands, key=lambda p: p.stat().st_size))


def _pick_wav_sample() -> str:
    """U15 用的小体积 wav（音频文件来源）。

    优先用 ``/tmp/asr_ref/`` 下的参考素材（``scripts/make_asr_live_wav.py``
    生成）—— 它就是 ``asr_en.wav`` 的 48 kHz 版本，正是会触发「第 1 轮
    瞬时猜测、下一轮被回滚」的那段音频（U15 要验的就是这个场景）。
    其次用 uploads 里的小 wav。都没有 → 返回 ""（用例跳过）。
    """
    for name in ("asr_48k_mono_s16.wav", "asr_16k_mono_s16.wav"):
        p = Path("/tmp/asr_ref") / name
        if p.is_file():
            return str(p)
    try:
        from app.core.config import default_resource_root
        d = default_resource_root() / "uploads"
    except Exception:  # noqa: BLE001
        return ""
    try:
        cands = [p for p in d.glob("*.wav")
                 if p.is_file() and 0 < p.stat().st_size < 20 * 1024 * 1024]
    except OSError:
        return ""
    if not cands:
        return ""
    return str(min(cands, key=lambda p: p.stat().st_size))


# --------------------------------------------------------------------------- #
# 跨端一致（B3）的三个比对工具
# --------------------------------------------------------------------------- #
def _resample_48k_to_16k(pcm48: bytes) -> bytes:
    """用**板载**那套重采样（``StreamResampler``）把 48 kHz 降到 16 kHz。

    这是 V6a 的「板载侧」基准 —— 必须与浏览器里的
    ``window.__asrLiveResample`` 输出**逐字节相同**。
    """
    from app.core.audio.capture import StreamResampler

    r = StreamResampler(48000, 16000)
    return r.push(pcm48) + r.flush()


def _first_diff(a: bytes, b: bytes) -> str:
    """两个字节串的第一处不同（给断言信息用）。"""
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            return f"@{i} ({a[i]} vs {b[i]})"
    if len(a) != len(b):
        return f"长度 {len(a)} vs {len(b)}"
    return "无"


def _best_correlation(a: bytes, b: bytes, max_lag: int = 2000) -> tuple:
    """求两段 PCM 的最佳对齐偏移（FFT 互相关）。

    返回 ``(偏移, 相关系数)``；偏移 > 0 表示 ``a`` 比 ``b`` **晚**
    （``a[i]`` 对应 ``b[i + 偏移]``）。

    ⚠️ Chromium 的采集链路有**固定延迟**（实测约 480 个 16 kHz 采样
    ≈ 30 ms），所以两段音频不会零对齐 —— 必须先对齐再比。
    """
    import numpy as np

    x = np.frombuffer(a, dtype="<i2").astype(np.float64)
    y = np.frombuffer(b, dtype="<i2").astype(np.float64)
    n = int(min(len(x), len(y), 16000 * 10))
    if n < 4000:
        return 0, 0.0
    x = x[:n]
    y = y[:n]
    x = x - x.mean()
    y = y - y.mean()
    nfft = 1 << int(np.ceil(np.log2(2 * n)))
    # ⚠️ 方向：``ifft(conj(X)·Y)[L] = Σ x[i]·y[i+L]`` —— 峰值在
    # 「x 比 y 晚 L 个采样」处（与下面 ``_aligned_diff`` 的约定一致）。
    cc = np.fft.irfft(np.conj(np.fft.rfft(x, nfft)) * np.fft.rfft(y, nfft), nfft)
    hi = int(min(max_lag + 1, n))
    lag = int(np.argmax(cc[:hi]))
    den = float(np.sqrt((x * x).sum() * (y * y).sum())) or 1.0
    return lag, float(cc[lag] / den)


def _aligned_diff(a: bytes, b: bytes, lag: int) -> tuple:
    """对齐之后比较两段 PCM，返回 ``(逐字节相同比例, 最大绝对差)``。"""
    import numpy as np

    x = np.frombuffer(a, dtype="<i2").astype(np.int64)
    y = np.frombuffer(b, dtype="<i2").astype(np.int64)
    if lag >= 0:
        u = x[:len(x) - lag]
        v = y[lag:lag + len(u)]
    else:
        u = x[-lag:]
        v = y[:len(u)]
    n = min(len(u), len(v))
    if n <= 0:
        return 0.0, 0
    d = np.abs(u[:n] - v[:n])
    return float((d == 0).mean()), int(d.max())


def _segment_align(a: bytes, b: bytes, seg_samples: int = 8000,
                   max_lag: int = 4000) -> list:
    """**逐段**对齐两段 PCM，返回 ``[(偏移, 相关系数), ...]``。

    为什么不用「整段一次对齐」：Chromium 的采集链路在负载高时会
    **偶发丢一个 buffer**（实测 10 ms = 480 个 48 kHz 采样），整段
    对齐会把「偏移变了」误判成「内容不同」。逐段对齐能同时看住两件事：

    * 每段的**相关系数** ≈ 1 → 两端确实是同一段录音；
    * 每段的**偏移**都在小范围内 → 两端没有跑飞。

    ⚠️ 相关系数必须**按重叠区**归一化 —— 偏移 L 个采样时只有
    ``n - L`` 个采样参与相关；用整段的能量归一化会把「错位」算成
    「不相关」（实测同一段音频会从 1.00 掉到 0.94）。
    """
    import numpy as np

    x = np.frombuffer(a, dtype="<i2").astype(np.float64)
    y = np.frombuffer(b, dtype="<i2").astype(np.float64)
    nseg = min(len(x), len(y)) // seg_samples
    out: list = []
    nfft = 1 << int(np.ceil(np.log2(2 * seg_samples)))
    for k in range(nseg):
        xs = x[k * seg_samples:(k + 1) * seg_samples]
        ys = y[k * seg_samples:(k + 1) * seg_samples]
        xc = xs - xs.mean()
        yc = ys - ys.mean()
        cc = np.fft.irfft(
            np.conj(np.fft.rfft(xc, nfft)) * np.fft.rfft(yc, nfft), nfft)
        lag = int(np.argmax(cc[:max_lag + 1]))
        # 按**重叠区**归一化
        m = seg_samples - lag
        if m <= 0:
            out.append((lag, 0.0))
            continue
        u = xc[:m]
        v = yc[lag:lag + m]
        den = float(np.sqrt((u * u).sum() * (v * v).sum()))
        if den <= 0:
            # 两边都是静音 → 内容「相同」（不是「不相关」）
            out.append((lag, 1.0))
            continue
        out.append((lag, float((u * v).sum() / den)))
    return out


def _envelope_corr(a: bytes, b: bytes, hop: int = 160,
                   max_hop_shift: int = 4) -> float:
    """两段 PCM 的 **10 ms RMS 包络**相关系数（允许小幅错位）。

    包络对「几十毫秒的偏移」和「偶发丢帧」都不敏感 —— 而细结构对它
    敏感。所以包络相关用来判「**是不是同一段录音**」，细结构相关用来
    判「内容有没有被改坏」。

    ⚠️ 在 ``±max_hop_shift`` 个 hop 内取**最佳**：采集链路的固定延迟
    （≈ 480 个采样 = 3 个 hop）和偶发丢帧都会让包络整体错开一点；
    不补偿的话陡峭的音节边界一错位，相关性直接从 1.0 掉到 0.7。
    """
    import numpy as np

    x = np.frombuffer(a, dtype="<i2").astype(np.float64)
    y = np.frombuffer(b, dtype="<i2").astype(np.float64)
    nx = len(x) // hop
    ny = len(y) // hop
    if nx < 100 or ny < 100:
        return 0.0
    ex = np.sqrt((x[:nx * hop].reshape(nx, hop) ** 2).mean(axis=1))
    ey = np.sqrt((y[:ny * hop].reshape(ny, hop) ** 2).mean(axis=1))
    best = 0.0
    for shift in range(0, max_hop_shift + 1):
        n = min(nx, ny - shift)
        if n < 100:
            break
        a1 = ex[:n]
        b1 = ey[shift:shift + n]
        a1 = a1 - a1.mean()
        b1 = b1 - b1.mean()
        den = float(np.sqrt((a1 * a1).sum() * (b1 * b1).sum()))
        if den <= 0:
            continue
        best = max(best, float((a1 * b1).sum() / den))
    return best


def _text_similarity(a: str, b: str) -> float:
    """两段文本的相似度（0~1，词级 difflib）。

    Chromium 的采集链路会调理音频（延迟 + 幅度微调），token 可能有一
    两个不同 → 这里只要求「基本一致」，不做逐字节断言。
    """
    import difflib

    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(None, a.split(), b.split()).ratio()


#: 页面里注入的 fetch 补丁 —— 记录**浏览器实际上传的 PCM**、第一块的
#: 发出时刻、以及在途请求数。用来做「跨端一致」的比对。
_FETCH_PATCH = r"""
(() => {
  window.__asrPcm = new Uint8Array(0);
  window.__asrSent = 0;
  window.__asrPending = 0;
  window.__asrT0 = null;
  const orig = window.fetch;
  window.fetch = function (input, init) {
    const u = (typeof input === 'string') ? input
        : (input && input.url) || '';
    let isChunk = false;
    if (u.indexOf('/live/chunk') >= 0 && init && init.body) {
      const b = init.body;
      const arr = (b instanceof Uint8Array) ? b : new Uint8Array(b);
      const merged = new Uint8Array(window.__asrPcm.length + arr.length);
      merged.set(window.__asrPcm, 0);
      merged.set(arr, window.__asrPcm.length);
      window.__asrPcm = merged;
      window.__asrSent += 1;
      window.__asrPending += 1;
      isChunk = true;
      if (window.__asrT0 === null) window.__asrT0 = performance.now();
    }
    const pr = orig.apply(this, arguments);
    if (isChunk) {
      pr.then(() => { window.__asrPending -= 1; },
              () => { window.__asrPending -= 1; });
    }
    return pr;
  };
})();
"""

#: 页面里注入的 WebSocket 补丁 —— 记录收到的所有帧（``endpoint`` /
#: ``gap`` / ``live_done`` 都要靠它判）。
_WS_PATCH = r"""
(() => {
  window.__asrWs = [];
  const Orig = window.WebSocket;
  function Patched(url, protocols) {
    const ws = (protocols === undefined) ? new Orig(url) : new Orig(url, protocols);
    ws.addEventListener('message', (ev) => {
      try { window.__asrWs.push(JSON.parse(ev.data)); } catch (_) {}
    });
    return ws;
  }
  Patched.prototype = Orig.prototype;
  window.WebSocket = Patched;
})();
"""


def verify_live(p, shot_dir: Path) -> None:
    """实时（麦克风）链路的端到端用例。

    覆盖清单 §9.2 的 B1~B12 里**能在本机复现**的那些：

    ======  ==========================================================
    V1      feature flag 关掉 → live 端点 404（B11）
    V2      flag 打开 → ``/live/start`` 返回 stream_id + 端点参数
    V3      **文件路径**（16 kHz 音频）→ 参考文本
    V4      **板载路径**（``tools/asr_mic_agent.py --file`` 48 kHz）
            → 与参考文本**逐字节相同**
    V5      **浏览器路径**（假麦克风）→ 有文本；首片段延迟 ≤ 1.7 s
    V6a     浏览器重采样 ≡ 板载重采样（**逐字节**，B3 的地基）
    V6b     **同一段 PCM** 经两条链路 → 文本**逐字节相同**（B3）
    V6c     浏览器上传的 PCM 与板载 PCM 是**同一段音频**（相关系数）
    V7      seq 缺号 → 服务端记丢块并上报（B4）
    V8      连续丢 5 块 → 熔断（B5）
    V9      离开页面 → 后端实时流被关闭（B9）
    V10     端点检测命中 → 服务端计数 + 前端收到 ``endpoint`` 帧（B6）
    ======  ==========================================================

    ⚠️ **为什么 B3 拆成 V6a/V6b/V6c 三条**
    ------------------------------------

    B3 原文是「同一段录音，浏览器采集与板载采集的最终文本**逐字节
    相同**」。在**当前测试环境**下这句话没法直接断言：

    * Chromium 的假麦克风**固定以 48 kHz 播放**，采集链路自带约
      **9.3 ms 的固定延迟**（实测 445 个采样）**并且会调理采样**
      （实测平均差 29 LSB、最大 1574 LSB —— AEC/NS 这类处理关不干净）。
      ⇒ 浏览器拿到的 PCM 与源文件**不可能逐字节相同**；
    * 真实麦克风同样有调理（AEC/NS/AGC 是**特性**，不是 bug）。

    所以「跨端一致」被拆成三条**可判定**的命题：

    =============  ==================================================
    V6a            **算法一致**：两端用**同一套**线性插值 → 同一段
                    输入 PCM 出来的 16 kHz PCM **逐字节相同**
                    （这条是产品代码的性质，与测试环境无关）
    V6b            **结果一致**：**同一段 PCM**（浏览器实际上传的那
                    段）从两条链路进来 → 文本**逐字节相同**
    V6c            **素材一致**：浏览器上传的 PCM 与板载路径喂的 PCM
                    是同一段录音（对齐后相关系数 ≥ 0.98）
    =============  ==================================================

    B7（纯静音 30 s）/ B8（会话上限 300 s）要跑太久，不在这里做 ——
    它们由 ``scripts/verify_asr_live.py`` 的契约档覆盖（L7 / L8）。
    """
    print("\n=== 实时识别（麦克风）===")
    for f in (LIVE_WAV16, LIVE_WAV48):
        if not Path(f).is_file():
            print(f"  [SKIP] 缺 {f}"
                  "（先跑 scripts/make_asr_live_wav.py）")
            return

    # ---------------- V1: feature flag 关掉 → 404 ----------------
    set_live_flag(False)
    st, d = live_start()
    check("V1 feature flag 关掉时 live 端点返回 404", st == 404,
          f"st={st} {d}")
    set_live_flag(True)

    # ---------------- V2: flag 打开 → 正常 ----------------
    st, d = live_start()
    check("V2 flag 打开后 /live/start 返回 200", st == 200, f"st={st} {d}")
    check("V2 返回了 stream_id", bool(d.get("stream_id")), str(d)[:120])
    check("V2 返回了端点参数（静音门限 / 端点时长）",
          bool(d.get("endpoint", {}).get("silence_ms"))
          and bool(d.get("endpoint", {}).get("threshold_dbfs")),
          str(d.get("endpoint")))
    if d.get("stream_id"):
        api("DELETE", "/api/qwen3-asr/live/stop", None,
            {"X-Asr-Stream-Id": d["stream_id"]})

    # ---------------- V3: 文件路径（参考） ----------------
    print("\n--- 参考：文件路径（16 kHz 音频）---")
    req_before = api_state().get("current_request_id") or 0
    st, d = api("POST", "/api/qwen3-asr/transcribe",
                urllib.parse.urlencode({"path": LIVE_WAV16}).encode(),
                {"Content-Type": "application/x-www-form-urlencoded"})
    check("V3 文件路径接受 16 kHz wav", st == 200, f"st={st} {str(d)[:150]}")
    snap = wait_run_done(req_before)
    ref_text = ((snap.get("last_result") or {}).get("text") or "").strip()
    print(f"  参考文本（{len(ref_text)} 字）：{ref_text[:80]!r}")
    check("V3 参考文本非空", bool(ref_text), f"snap={snap.get('state')}")

    # ---------------- V4: 板载路径 ----------------
    print("\n--- 板载路径（tools/asr_mic_agent.py --file 48 kHz）---")
    agent = _ROOT / "tools" / "asr_mic_agent.py"
    proc = subprocess.run(
        [sys.executable, str(agent), "--file", LIVE_WAV48],
        capture_output=True, text=True, timeout=300, cwd=str(_ROOT))
    board_text = (proc.stdout or "").strip()
    # agent 的 stdout 是「最终文本」；日志走 stderr。
    board_text = board_text.splitlines()[-1].strip() if board_text else ""
    print(f"  板载文本：{board_text[:80]!r}")
    check("V4 板载路径有识别结果", bool(board_text),
          f"rc={proc.returncode} err={proc.stderr[-300:]!r}")
    # 板载路径喂的是 48 kHz 文件 → 用 StreamResampler 降到 16 kHz，
    # 结果与「16 kHz 文件」**逐字节相同** → 识别结果必然一致。
    # 端点切句只影响「分成几句」，不影响文本本身。
    check("V4 板载文本 ≡ 文件路径文本", board_text == ref_text,
          f"board={board_text[:100]!r} ref={ref_text[:100]!r}")
    check("V4b 板载文本 ≡ C++ demo 文本", board_text == C_FINAL,
          f"board={board_text[:120]!r}")

    # ---------------- V5/V6/V10: 浏览器路径 ----------------
    print("\n--- 浏览器路径（假麦克风）---")
    exe = _find_chrome()
    browser = p.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            # 假麦克风设备 + 自动授权
            "--use-fake-device-for-media-stream",
            "--use-fake-ui-for-media-stream",
            # 把 48 kHz wav 当作麦克风输入，%noloop = 放一遍就静音。
            # ⚠️ Chromium 的假设备**固定以 48 kHz 播放** → 测试资产必须是
            # 48 kHz（16 kHz 的文件会被它自己重采样一遍）。
            f"--use-file-for-fake-audio-capture={LIVE_WAV48}%noloop",
        ],
        executable_path=exe)
    page = browser.new_page(viewport={"width": 1440, "height": 960})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    browser_text = ""
    try:
        page.add_init_script(_FETCH_PATCH)
        page.add_init_script(_WS_PATCH)
        page.goto(BASE, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_function("() => window.ROUTER", timeout=20000)
        page.wait_for_timeout(3000)
        goto_home(page)
        goto_card(page, APP)
        page.wait_for_timeout(1500)
        # 这张卡带向导 → 进卡时框架会把**功能页藏起来**、挂向导
        # （framework.js 的 ``_prepareCardEntry``）。要测功能页就得先把
        # 向导卸掉 —— 这也正是用户「跳过向导」后的状态。
        page.evaluate(
            "() => { try { window.WizardState.unmount(); } catch (_) {} }")
        page.wait_for_timeout(1200)

        # 实时来源要**显式切过去**。
        #
        # 四个来源页签是**互斥**的（U3 的不变式），默认停在「音频文件」
        # —— 实时区块只在「板载麦 / 浏览器麦」下可见。所以实时用例
        # 必须先切页签，否则「实时区块不可见」会被误读成「实时功能坏了」。
        page.click('#asr-source-tabs .asr-source-tab[data-source=browser]')
        page.wait_for_timeout(800)

        # ⚠️ **非安全上下文 → 浏览器麦用例整体跳过**（不是失败）。
        #
        # ``navigator.mediaDevices`` 只在 https / localhost 下存在。用局域网
        # IP 走明文 http:// 时，浏览器**根本不提供**麦克风 API —— V5/V6a/
        # V6b/V6c/V5c 测的是「浏览器采到的音频」，没有音频可比。
        #
        # 跳过而不是判失败：功能**没坏**，是这个访问方式用不了浏览器麦。
        # 这种情形由 U9 / U10 单独验（提示条 + 可读错误）。
        secure_ctx = page.evaluate("() => !!window.isSecureContext")
        if not secure_ctx:
            print("  [SKIP] 非安全上下文（http:// + 局域网 IP）—— 浏览器不提供"
                  " navigator.mediaDevices，V5/V6a/V6b/V6c/V5c 跳过。"
                  "改用 https:// 或 http://127.0.0.1 跑可覆盖。")
            return

        # 实时区块要可见（flag 开着）
        live_visible = page.evaluate(
            """() => {
                const el = document.getElementById('asr-live-block');
                return !!el && !el.classList.contains('hidden');
            }""")
        check("V5 实时区块在页面上可见", live_visible is True,
              f"visible={live_visible}")

        # 点「开始实时识别」。
        # fetch 补丁已经在页面加载时打好了（add_init_script）：它记下
        # **第一块音频真正发出去的时刻**（B1 的「说话时 ≤ 1.7 s」从
        # 这一刻算，不含 getUserMedia / AudioContext / worklet 下载的
        # 启动开销），同时把**实际上传的 PCM** 留下来做跨端比对。
        page.click("#asr-mic-btn")
        page.wait_for_timeout(1200)
        started = page.evaluate(
            "() => (document.getElementById('asr-live-status')||{}).textContent")
        print(f"  状态行：{started!r}")
        ctx_rate = page.evaluate(
            "() => (window.__asrLiveCtx ? window.__asrLiveCtx.sampleRate : -1)")
        print(f"  AudioContext 采样率：{ctx_rate} Hz"
              "（原生采样率；重采样由前端自己做，与板载同算法）")

        # 等文本出现。
        # 判据取「黑字 + 蓝字」的**任意**非空：模型按 1 秒一块解码，
        # 第 1 轮就有**蓝色未确认尾巴**，但**已确认**（黑字）要到第 5 轮
        # 才出现（SDK 的 rollback_rounds=4：前 4 轮不提交任何 token）。
        text_deadline = time.time() + 90
        first_text_at = None
        while time.time() < text_deadline:
            t = page.evaluate(
                """() => {
                    const c = document.getElementById('asr-rt-committed');
                    const u = document.getElementById('asr-rt-unfixed');
                    return ((c ? c.textContent : '')
                            + (u ? u.textContent : '')).trim();
                }""")
            if t:
                first_text_at = time.time()
                break
            page.wait_for_timeout(200)
        check("V5 浏览器麦克风出现了识别文本", first_text_at is not None,
              f"status={started!r}")
        speak_lag = None
        if first_text_at:
            # 从「第一块音频发出去」算起（页面里的 performance.now()）
            lag = page.evaluate("""() => {
                const c = document.getElementById('asr-rt-committed');
                const u = document.getElementById('asr-rt-unfixed');
                const txt = ((c ? c.textContent : '')
                             + (u ? u.textContent : '')).trim();
                return { t0: window.__asrT0, len: txt.length };
            }""")
            if lag.get("t0"):
                speak_lag = page.evaluate("""() => {
                    const c = document.getElementById('asr-rt-committed');
                    const u = document.getElementById('asr-rt-unfixed');
                    const txt = ((c ? c.textContent : '')
                                 + (u ? u.textContent : '')).trim();
                    return txt.length ? performance.now() - window.__asrT0 : -1;
                }""")
            print(f"  从「开始说话」到首个片段："
                  f"{None if speak_lag is None or speak_lag < 0 else round(speak_lag / 1000, 2)}s")
            check("V5 说话到首个片段 ≤ 1.7 s（§6 时延预算）",
                  speak_lag is not None and 0 <= speak_lag <= 1700,
                  f"{speak_lag}ms")

        # ---------------- V6a: 两端重采样逐字节一致 ----------------
        print("\n--- V6a: 浏览器重采样 ≡ 板载重采样（逐字节）---")
        src48 = read_wav_pcm(LIVE_WAV48, expect_rate=48000)
        js_out = page.evaluate(
            """(b64) => {
                const bin = atob(b64);
                const n = bin.length >> 1;
                const i16 = new Int16Array(n);
                for (let i = 0; i < n; i++) {
                    i16[i] = bin.charCodeAt(i * 2)
                        | (bin.charCodeAt(i * 2 + 1) << 8);
                }
                const f32 = new Float32Array(n);
                for (let i = 0; i < n; i++) f32[i] = i16[i] / 32768;
                const out = window.__asrLiveResample(f32, 48000);
                let s = '';
                const CH = 8192;
                for (let i = 0; i < out.length; i += CH) {
                    s += String.fromCharCode.apply(
                        null, out.subarray(i, i + CH));
                }
                return btoa(s);
            }""",
            base64.b64encode(src48).decode("ascii"))
        js_pcm = base64.b64decode(js_out)
        py_pcm = _resample_48k_to_16k(src48)
        check("V6a 浏览器重采样 ≡ 板载重采样（逐字节相同）",
              js_pcm == py_pcm,
              f"js={len(js_pcm)}B py={len(py_pcm)}B "
              f"首个不同={_first_diff(js_pcm, py_pcm)}")

        # ---------------- V10: 端点检测命中 ----------------
        print("\n--- V10: 端点检测 ---")
        # 假音频放完 → 静音 1.2 s → 服务端命中端点 → 广播 endpoint 帧
        # → 前端把句子拼起来。**必须在帧到达的那一刻抓** —— 状态行会被
        # 下一条 chunk 的响应立刻覆盖掉（所以不能最后再读一次）。
        ep_deadline = time.time() + 60
        ep_frame = None
        while time.time() < ep_deadline:
            for fr in page.evaluate("() => window.__asrWs || []"):
                if fr.get("type") == "endpoint" and fr.get("stream_id"):
                    ep_frame = fr
                    break
            if ep_frame:
                break
            live_state = api_state().get("live") or {}
            if int(live_state.get("endpoint_index") or 0) >= 1:
                # 服务端已经计数并广播了 —— 但帧还在路上（WS 是异步的），
                # **再等一会儿**把它收进来。
                frame_deadline = time.time() + 10
                while time.time() < frame_deadline and not ep_frame:
                    page.wait_for_timeout(200)
                    for fr in page.evaluate("() => window.__asrWs || []"):
                        if fr.get("type") == "endpoint" and fr.get("stream_id"):
                            ep_frame = fr
                            break
                break
            page.wait_for_timeout(300)
        # ⚠️ 端点计数要在**循环之后**再读一次 —— 循环可能是因为「先收到
        # 帧」而退出的（那条路径根本没读过后端快照）。
        ep_index = int((api_state().get("live") or {}).get("endpoint_index") or 0)
        check("V10 端点检测命中（服务端 endpoint_index ≥ 1）", ep_index >= 1,
              f"index={ep_index}")
        check("V10 前端收到了 endpoint 帧（WS 广播，B6）", ep_frame is not None,
              f"frames={len(page.evaluate('() => window.__asrWs || []'))}")
        if ep_frame:
            print(f"  endpoint 帧：index={ep_frame.get('index')} "
                  f"text={(ep_frame.get('text') or '')[:60]!r}")
        page.screenshot(path=str(shot_dir / "7_live.png"))

        # ---------------- V6b: 同一段 PCM → 两条链路文本相同 ----------------
        print("\n--- V6b: 同一段 PCM 经两条链路 → 文本逐字节相同 ---")
        # 等上传队列排空（在途请求为 0 且持续 1.5 s）→ 此刻「已上传的
        # PCM」与「服务端已处理的 PCM」完全一致。
        drain_deadline = time.time() + 30
        while time.time() < drain_deadline:
            if page.evaluate("() => window.__asrPending") == 0:
                time.sleep(1.5)
                if page.evaluate("() => window.__asrPending") == 0:
                    break
            time.sleep(0.2)
        b64 = page.evaluate("""() => {
            const p = window.__asrPcm;
            let s = '';
            const CH = 8192;
            for (let i = 0; i < p.length; i += CH) {
                s += String.fromCharCode.apply(null, p.subarray(i, i + CH));
            }
            return btoa(s);
        }""")
        browser_pcm = base64.b64decode(b64)
        print(f"  浏览器上传的 PCM：{len(browser_pcm)} 字节"
              f"（{len(browser_pcm) / 2 / 16000:.1f} s @16 kHz）")

        live_state = api_state().get("live") or {}
        sid = live_state.get("stream_id") or ""
        st, fin = api("POST", "/api/qwen3-asr/live/finish", None,
                      {"X-Asr-Stream-Id": sid})
        browser_text = (fin.get("text") or "").strip()
        print(f"  浏览器路径最终文本（服务端）：{browser_text[:80]!r}")

        pcm_path = Path("/tmp/asr_ref/browser_uploaded.pcm")
        pcm_path.parent.mkdir(parents=True, exist_ok=True)
        pcm_path.write_bytes(browser_pcm)
        proc = subprocess.run(
            [sys.executable, str(agent), "--pcm", str(pcm_path)],
            capture_output=True, text=True, timeout=300, cwd=str(_ROOT))
        same_text = (proc.stdout or "").strip()
        same_text = same_text.splitlines()[-1].strip() if same_text else ""
        print(f"  板载路径最终文本（同一段 PCM）：{same_text[:80]!r}")
        check("V6b 同一段 PCM：板载路径文本 ≡ 浏览器路径文本（逐字节，B3）",
              same_text == browser_text and bool(browser_text),
              f"board={same_text[:100]!r} browser={browser_text[:100]!r} "
              f"rc={proc.returncode}")

        # ---------------- V6c: 两端确实是同一段录音 ----------------
        print("\n--- V6c: 两端音频的一致性 ---")
        board_pcm16 = read_wav_pcm(LIVE_WAV16, expect_rate=16000)
        # 先按「开头几秒」的固定延迟整体平移（Chromium 采集链路有固定
        # 延迟 ≈ 30 ms），再逐段独立对齐。
        lag0, _ = _best_correlation(browser_pcm[:16000 * 4],
                                    board_pcm16[:16000 * 4])
        # ⚠️ ``lag0`` 是**采样**数，而 PCM 是 s16le（1 采样 = 2 字节）——
        # 直接拿它切字节会只平移一半，而且当 lag0 是奇数时切出来的字节数
        # 是奇数，``np.frombuffer(..., '<i2')`` 直接抛
        # 「buffer size must be a multiple of element size」。
        shifted = browser_pcm[lag0 * 2:] if lag0 >= 0 else browser_pcm
        env = _envelope_corr(browser_pcm, board_pcm16)
        segs = _segment_align(shifted, board_pcm16)
        cors = sorted(c for _, c in segs)
        lags = [l for l, _ in segs]
        # 第 0 段是**采集启动瞬态**（设备刚起播、内容对不齐）→ 不计入
        median = cors[len(cors) // 2] if cors else 0.0
        good = sum(1 for c in cors if c >= 0.99)
        print(f"  整体偏移 {lag0} 个采样（{lag0 / 16:.1f} ms —— Chromium "
              f"采集链路的固定延迟）")
        print(f"  10 ms RMS 包络相关系数：{env:.6f}")
        print(f"  逐 0.5 s 分段细结构相关：中位数 {median:.6f}，"
              f"{good}/{len(cors)} 段 ≥ 0.99")
        print(f"  各段相对偏移 {min(lags) if lags else 0}~"
              f"{max(lags) if lags else 0} 个采样"
              f"（偶发丢帧会让偏移变化，但幅度很小）")
        check("V6c 浏览器上传的 PCM 与板载 PCM 是同一段音频"
              "（包络相关 ≥ 0.95）", env >= 0.95, f"env={env:.4f}")
        check("V6c 两端音频的细结构一致（分段相关中位数 ≥ 0.999）",
              median >= 0.999, f"median={median:.6f}")
        check("V6c 两端的偏移没有跑飞（各段相对偏移极差 ≤ 2000 采样）",
              bool(lags) and (max(lags) - min(lags)) <= 2000,
              f"lags {min(lags)}..{max(lags)}" if lags else "no segs")

        # 浏览器文本与参考文本的相似度（Chromium 的采集链路有固定延迟
        # 和调理，token 可能有一两个不同 —— 这里只要求「基本一致」）。
        sim = _text_similarity(browser_text, ref_text)
        print(f"  浏览器文本 vs 参考文本：相似度 {sim:.3f}"
              f"（逐字节相同={browser_text == ref_text}）")
        check("V5c 浏览器文本与参考文本基本一致（相似度 ≥ 0.75）",
              sim >= 0.75, f"sim={sim:.3f}")

        # ---------------- V9: 离开页面 → 后端实时流被关闭 ----------------
        print("\n--- V9: 离开页面 → 后端实时流被关闭 ---")
        page2 = browser.new_page(viewport={"width": 1440, "height": 960})
        page2.add_init_script(_FETCH_PATCH)
        page2.add_init_script(_WS_PATCH)
        try:
            page2.goto(BASE, wait_until="domcontentloaded", timeout=30000)
            page2.wait_for_function("() => window.ROUTER", timeout=20000)
            page2.wait_for_timeout(3000)
            goto_card(page2, APP)
            page2.wait_for_timeout(1500)
            page2.evaluate(
                "() => { try { window.WizardState.unmount(); } catch (_) {} }")
            page2.wait_for_timeout(1200)
            # 同 V5：实时来源要**显式切过去**（页签互斥，默认是「音频文件」）
            page2.click('#asr-source-tabs .asr-source-tab[data-source=browser]')
            page2.wait_for_timeout(800)
            page2.click("#asr-mic-btn")
            # 等后端真的开出会话
            live2 = {}
            for _ in range(40):
                time.sleep(0.5)
                live2 = api_state().get("live") or {}
                if live2.get("active"):
                    break
            check("V9 会话在跑时快照能看到 live", bool(live2.get("stream_id")),
                  str(live2)[:150])
            # 切走这张卡 → 前端的 hashchange 兜底 → DELETE /live/stop
            page2.evaluate("() => { window.location.hash = ''; }")
            for _ in range(20):
                time.sleep(0.5)
                after = api_state().get("live") or {}
                if not after.get("active"):
                    break
            after = api_state().get("live") or {}
            check("V9 离开页面后后端实时流被关闭",
                  not after.get("active"), str(after)[:150])
        finally:
            page2.close()
    finally:
        browser.close()

    # ---------------- V7: seq 缺号 → 记丢块 ----------------
    print("\n--- 背压 / 丢包 ---")
    st, d = live_start()
    sid = d.get("stream_id") or ""
    pcm = read_wav_pcm(LIVE_WAV16, expect_rate=16000)
    chunk = 16000 * 2
    seq = 0
    last = {}
    for i in range(0, min(len(pcm), chunk * 4), chunk):
        st, last = live_chunk(sid, seq, pcm[i:i + chunk])
        seq += 1
    # 故意跳过 3 个序号 → 服务端应记 3 块丢失
    st, gap_resp = live_chunk(sid, seq + 3, b"\x00" * chunk)
    check("V7 seq 缺号被记成丢块",
          (gap_resp.get("dropped_chunks") or 0) >= 3,
          f"resp={gap_resp}")
    api("DELETE", "/api/qwen3-asr/live/stop", None,
        {"X-Asr-Stream-Id": sid})

    # ---------------- V8: 连续丢 5 块 → 熔断 ----------------
    st, d = live_start()
    sid = d.get("stream_id") or ""
    st, r0 = live_chunk(sid, 0, pcm[:chunk])
    # 一次跳 5 → 连续丢 5 块 → 熔断
    st, r1 = live_chunk(sid, 6, pcm[chunk:chunk * 2])
    check("V8 连续丢 5 块后熔断（409）", st == 409, f"st={st} {r1}")
    check("V8 熔断有明确的错误说明",
          bool(r1.get("detail")) and "丢失" in str(r1.get("detail")),
          str(r1)[:150])
    api("DELETE", "/api/qwen3-asr/live/stop", None,
        {"X-Asr-Stream-Id": sid})

    check("实时链路页面没有 JS 报错", not errors, f"errors={errors[:3]}")


# --------------------------------------------------------------------------- #
# 四种输入源 + 去掉 60 s 截断
# --------------------------------------------------------------------------- #
#: 长音频（> 60 s）—— 用来证明「文件路径不再截断」。
#:
#: 60 s 是 C demo 的 ``ONLINE_MAX_AUDIO_SECONDS``；旧实现按它砍尾巴，
#: UI 上还写着「最长处理 60 秒」。现在走增量会话，**不截断**。
LONG_WAV = "/tmp/asr_ref/asr_long_90s.wav"

#: 板载 stop 的**死锁**回归阈值。
#:
#: 旧实现把「等 agent 退出」的同步等待直接写在 ``async def`` 端点里 →
#: **堵死事件循环** → agent 的 ``/live/finish`` 排在后面处理不了 →
#: agent 被 SIGKILL（退出码 -9）、``/live/finish`` 丢、live 会话残留。
#: 症状：stop 端点耗时 = grace 秒数（5 s），且这 5 s 内服务**一条请求
#: 都不回**。所以判据是「stop 必须远快于 grace」。
BOARD_STOP_MAX_SEC = 5.0


def verify_sources(page, shot_dir: Path) -> None:
    """四种输入源的 UI 回归 + 「不再 60 s 截断」的后端回归。

    覆盖本轮扩展的四个来源：

    ======  ====================================================
    U1      四个来源页签齐全（文件 / 视频 / 板载麦 / 浏览器麦）
    U2      切到视频 → ``accept`` 换成视频类型；切回文件 → 音频
    U3      四个面板**互斥**（切到谁就只显示谁）
    U4      板载面板把设备列出来了（``/live/board/devices`` 通了）
    U5      页面**不再**出现「60 秒 / 最长处理」字样
    U6      ``/transcribe`` 的响应里没有 ``truncated`` 字段了
    U7      **> 60 s 的音频不被截断**（``audio_sec`` 真的 > 60）
    U8      板载 stop **不堵死事件循环**（远快于 grace、优雅退出）
    ======  ====================================================
    """
    print("\n=== 四种输入源（文件 / 视频 / 板载麦 / 浏览器麦）===")
    goto_card(page, APP)
    page.wait_for_timeout(900)

    # ---------------- U1 四个页签 ----------------
    tabs = page.eval_on_selector_all(
        "#asr-source-tabs .asr-source-tab",
        """els => els.map(e => ({
               src: e.dataset.source, text: e.textContent.trim(),
               on: e.classList.contains('is-active'),
               sel: e.getAttribute('aria-selected')}))""")
    print("  页签: " + " | ".join(
        "{}({}){}".format(t["text"], t["src"], "*" if t["on"] else "")
        for t in tabs))
    check("U1 有四个来源页签", len(tabs) == 4, f"tabs={tabs}")
    check("U1 页签覆盖 文件/视频/板载麦/浏览器麦",
          {t["src"] for t in tabs} == {"file", "video", "board", "browser"},
          f"tabs={tabs}")
    check("U1 默认选中「音频文件」",
          any(t["src"] == "file" and t["on"] for t in tabs), f"tabs={tabs}")

    def _panel_state() -> dict:
        """面板显隐 + 关键控件（**一次 evaluate 读完**）。"""
        return page.evaluate(
            """() => {
                const vis = (id) => {
                    const e = document.getElementById(id);
                    return e ? !e.classList.contains('hidden') : null;
                };
                const fi = document.getElementById('asr-file');
                const sel = document.getElementById('asr-board-device');
                const hint = document.getElementById('asr-source-hint');
                const liveBlock = document.getElementById('asr-live-block');
                return {
                    file: vis('asr-panel-file'), board: vis('asr-panel-board'),
                    browser: vis('asr-panel-browser'),
                    liveBlock: liveBlock
                        ? !liveBlock.classList.contains('hidden') : null,
                    accept: fi ? fi.getAttribute('accept') : null,
                    hint: hint ? hint.textContent.trim() : '',
                    devices: sel ? [...sel.options].map(o => o.value) : [],
                    boardStart: !!document.getElementById('asr-board-start'),
                    boardStop: !!document.getElementById('asr-board-stop'),
                    micBtn: !!document.getElementById('asr-mic-btn'),
                    micStop: !!document.getElementById('asr-mic-stop-btn'),
                    meter: !!document.getElementById('asr-meter-fill'),
                };
            }""")

    # ---------------- U2 切到「含音轨的视频」 ----------------
    page.click('#asr-source-tabs .asr-source-tab[data-source=video]')
    page.wait_for_timeout(500)
    v = _panel_state()
    print("  video: accept=" + str(v.get("accept")))
    check("U2 切到视频后 accept 收视频类型",
          "video/*" in (v.get("accept") or ""), f"accept={v.get('accept')}")
    check("U2 切到视频后仍显示文件控件（视频/文件共用一套）",
          v.get("file") is True, f"state={v}")
    check("U2 切到视频后提示语说的是「视频」",
          "视频" in (v.get("hint") or ""), f"hint={v.get('hint')!r}")

    # ---------------- U3 切到「浏览器麦克风」 ----------------
    page.click('#asr-source-tabs .asr-source-tab[data-source=browser]')
    page.wait_for_timeout(500)
    b = _panel_state()
    check("U3 切到浏览器麦后只显示浏览器面板",
          b.get("browser") is True and b.get("file") is False
          and b.get("board") is False, f"state={b}")
    check("U3 浏览器面板有开始/中止按钮和电平表",
          b.get("micBtn") and b.get("micStop") and b.get("meter"),
          f"state={b}")

    # ---------------- U4 切到「板载麦克风」 ----------------
    page.click('#asr-source-tabs .asr-source-tab[data-source=board]')
    # 设备列表是异步拉的 → 等下拉里有选项
    try:
        page.wait_for_function(
            """() => {
                const sel = document.getElementById('asr-board-device');
                return !!sel && sel.options.length > 0;
            }""", timeout=10000)
    except Exception:  # noqa: BLE001 — 下面用断言报告
        pass
    bd = _panel_state()
    print("  板载设备: " + ", ".join(bd.get("devices") or []))
    check("U4 切到板载麦后只显示板载面板",
          bd.get("board") is True and bd.get("file") is False
          and bd.get("browser") is False, f"state={bd}")
    check("U4 板载面板有开始/停止按钮",
          bd.get("boardStart") and bd.get("boardStop"), f"state={bd}")
    check("U4 板载面板列出了采集设备", len(bd.get("devices") or []) > 0,
          f"devices={bd.get('devices')}")
    page.screenshot(path=str(shot_dir / "7_sources_board.png"))

    # ---------------- U5 页面不再说「60 秒」 ----------------
    txt = page.evaluate(
        "() => { const e = document.getElementById('page-qwen3-asr');"
        "return e ? (e.innerText || '') : ''; }")
    bad = [k for k in ("60 秒", "60秒", "60s", "最长处理", "最长 60")
           if k in txt]
    check("U5 页面不再出现「60 秒 / 最长处理」字样", not bad, f"found={bad}")

    # 切回文件来源（后面的用例都用文件路径）
    page.click('#asr-source-tabs .asr-source-tab[data-source=file]')
    page.wait_for_timeout(400)
    f = _panel_state()
    check("U2 切回文件后 accept 收音频类型",
          "audio/*" in (f.get("accept") or ""), f"accept={f.get('accept')}")

    # ---------------- U6 / U7 不再 60 s 截断 ----------------
    if not Path(LONG_WAV).is_file():
        print(f"  [SKIP] 缺 {LONG_WAV}（长音频截断用例跳过）")
    else:
        req_before = api_state().get("current_request_id") or 0
        st, d = api("POST", "/api/qwen3-asr/transcribe",
                    urllib.parse.urlencode({"path": LONG_WAV}).encode(),
                    {"Content-Type": "application/x-www-form-urlencoded"})
        check("U6 长音频的转写请求被接受", st == 200, f"st={st} {str(d)[:150]}")
        check("U6 响应里不再有 truncated 字段",
              "truncated" not in d and "truncated" not in (d.get("snapshot") or {}),
              f"keys={sorted(d.keys())}")
        snap = wait_run_done(req_before, timeout=300)
        last = snap.get("last_result") or {}
        audio_sec = float(last.get("audio_sec") or 0)
        print(f"  长音频: audio_sec={audio_sec:.2f} "
              f"elapsed={last.get('elapsed_sec')} "
              f"text_len={len(last.get('text') or '')}")
        check("U7 长音频没有被 60 s 截断（audio_sec > 60）", audio_sec > 60.0,
              f"audio_sec={audio_sec}")
        check("U7 长音频识别出了文本", bool((last.get("text") or "").strip()),
              f"text_len={len(last.get('text') or '')}")

    # ---------------- U8 板载 stop 不堵死事件循环 ----------------
    # 这条是**回归护栏**：旧实现把同步等待写在 async 端点里 → 事件循环
    # 被堵 5 s → agent 的 /live/finish 处理不了 → SIGKILL（-9）+ 会话残留。
    set_live_flag(True)
    st, d = api("POST", "/api/qwen3-asr/live/board/start",
                json.dumps({"device": "hw:1,0"}).encode("utf-8"),
                {"Content-Type": "application/json"}, timeout=30)
    check("U8 板载采集能起来", st == 200 and d.get("running") is True,
          f"st={st} {str(d)[:150]}")
    try:
        time.sleep(4)
        t0 = time.monotonic()
        st, d = api("POST", "/api/qwen3-asr/live/board/stop", timeout=30)
        dt = time.monotonic() - t0
        print(f"  板载 stop 耗时 {dt:.2f}s · 退出码 {d.get('last_exit')}")
        check("U8 板载 stop 不堵死事件循环（远快于 grace）",
              dt < BOARD_STOP_MAX_SEC, f"{dt:.2f}s")
        check("U8 agent 优雅退出（退出码 0，不是 -9/SIGKILL）",
              d.get("last_exit") == 0, f"last_exit={d.get('last_exit')}")
        # 收尾后不能有残留的板载实时会话（否则会占着 SDK 会话到 TTL）
        snap2 = api_state()
        live = snap2.get("live") or {}
        check("U8 stop 之后没有残留的板载实时会话",
              not live.get("active") or live.get("source") != "board",
              f"live={live}")
    finally:
        # 兜底：不管上面成没成，都把采集进程收掉（别留个占着声卡的孤儿）
        try:
            api("POST", "/api/qwen3-asr/live/board/stop", timeout=30)
        except Exception:  # noqa: BLE001
            pass

    # ---------------- U9 / U10 安全上下文的可发现性 ----------------
    # 浏览器只在**安全上下文**（https 或 localhost）里暴露
    # ``navigator.mediaDevices``。用局域网 IP 走明文 http:// 时它是
    # undefined —— 点「开始实时识别」**静默失败**。
    #
    # 两条判据按当前上下文**分支**：
    #   非安全 → 必须显示提示条，且点击后给出**可读**的错误（不是「已停止」）
    #   安全   → 不显示提示条（麦克风可用，V5 已验）
    #
    # 同一份脚本跑两遍就能覆盖两条：``WDNWEBUI_BASE`` 指向 http://<IP>:8000
    # 与 https://<IP>:8443。
    secure = page.evaluate("() => !!window.isSecureContext")
    # 要点按钮就得先切到浏览器麦面板（按钮在那个面板里）。
    page.click('#asr-source-tabs .asr-source-tab[data-source=browser]')
    page.wait_for_timeout(600)
    hint = page.evaluate(
        """() => {
            const e = document.getElementById('asr-mic-hint');
            if (!e) return null;
            return {hidden: e.classList.contains('hidden'),
                    text: e.textContent.trim()};
        }""")
    print(f"  isSecureContext={secure} · 提示条 hidden={hint and hint.get('hidden')}")
    if not secure:
        check("U9 非安全上下文时浏览器麦面板给出提示条",
              hint is not None and hint.get("hidden") is False,
              f"hint={hint}")
        check("U9 提示条里写明了 https 访问地址",
              bool(hint and ":8443" in (hint.get("text") or "")),
              f"text={hint and hint.get('text')}")
        # 点一下 → 错误提示必须是**可读的**，不能被「已停止」盖掉。
        page.click("#asr-mic-btn")
        page.wait_for_timeout(1500)
        st = page.evaluate(
            "() => (document.getElementById('asr-live-status')||{}).textContent || ''")
        print(f"  点按钮后状态行: {st!r}")
        check("U10 非安全上下文点按钮给出可读错误（不是「已停止」）",
              bool(st.strip()) and st.strip() != "已停止"
              and ("安全上下文" in st or "不支持" in st),
              f"status={st!r}")
    else:
        check("U9 安全上下文时不显示提示条（麦克风可用）",
              hint is not None and hint.get("hidden") is True,
              f"hint={hint}")

    # ---------------- U11~U14 视频播放（含音轨的视频来源） ----------------
    # 「含音轨的视频」这个来源不只是「拿它的音轨去识别」—— 画面本身也要
    # 在 UI 上播出来，字幕跟着播放头走。
    print("\n--- 视频播放（含音轨的视频来源）---")
    sample = _pick_sample_mp4()
    if not sample:
        print("  [SKIP] uploads 目录里没有小体积 mp4（视频播放用例跳过）")
    else:
        # U11：播放器只在视频来源下出现
        page.click('#asr-source-tabs .asr-source-tab[data-source=video]')
        page.wait_for_timeout(500)
        vis = page.evaluate(
            """() => {
                const e = document.getElementById('asr-video-wrap');
                return !!e && !e.classList.contains('hidden');
            }""")
        check("U11 视频来源下播放器可见", vis is True, f"visible={vis}")
        page.click('#asr-source-tabs .asr-source-tab[data-source=file]')
        page.wait_for_timeout(400)
        vis2 = page.evaluate(
            """() => {
                const e = document.getElementById('asr-video-wrap');
                return !!e && !e.classList.contains('hidden');
            }""")
        check("U11 切回音频文件后播放器隐藏", vis2 is False, f"visible={vis2}")

        # U12：设备端路径 → 走媒体端点，且**真的能解码出画面**
        page.click('#asr-source-tabs .asr-source-tab[data-source=video]')
        page.wait_for_timeout(400)
        page.fill("#asr-path", sample)
        page.dispatch_event("#asr-path", "input")
        try:
            page.wait_for_function(
                """() => {
                    const el = document.getElementById('asr-video');
                    return !!el && el.readyState >= 2 && el.videoWidth > 0;
                }""", timeout=20000)
            decoded = True
        except Exception:
            decoded = False
        vst = page.evaluate(
            """() => {
                const el = document.getElementById('asr-video');
                return {src: (el.currentSrc || el.src || ''),
                        w: el.videoWidth, h: el.videoHeight,
                        dur: el.duration,
                        err: el.error ? el.error.code : null};
            }""")
        print(f"  样本: {sample}")
        print(f"  video: {vst.get('w')}x{vst.get('h')} dur={vst.get('dur')}")
        check("U12 设备端视频能真正解码出画面", decoded, f"v={vst}")
        check("U12 视频 src 指向媒体端点",
              "/api/qwen3-asr/media" in (vst.get("src") or ""),
              f"src={vst.get('src')}")

        # U13：媒体端点**没有鉴权** → 路径必须被白名单挡住
        def _media_status(p: str) -> int:
            st, _ = api("GET", "/api/qwen3-asr/media?path="
                        + urllib.parse.quote(p), timeout=20)
            return st

        check("U13 媒体端点拒绝白名单外的路径（/etc/passwd）",
              _media_status("/etc/passwd") in (400, 403),
              f"st={_media_status('/etc/passwd')}")
        check("U13 媒体端点拒绝路径逃逸（uploads/../../etc/passwd）",
              _media_status("/userdata/assets/uploads/../../etc/passwd")
              in (400, 403),
              f"st={_media_status('/userdata/assets/uploads/../../etc/passwd')}")

        # U14：字幕按播放头**逐词**推进（YouTube 式实时字幕）
        #
        # ⚠️ **语义变了**：以前字幕是「最终文本的前缀，越往后越长」；
        # 现在是「当前行 + 逐词点亮 + 当前词高亮」（用户要求照 YouTube
        # 实时字幕的逻辑做）。所以不能再断言「后段包含前段」，要断言的是：
        #
        #   * 字幕随播放头**变**（不是把全文糊在画面上）；
        #   * 字幕里每个字都在**最终文本**里（不含会被回滚的猜测）；
        #   * 累计点亮的字数随播放头**单调不减**（真的在往前推进）。
        tmp = "/userdata/assets/uploads/_asr_video_test.mp4"
        made = False
        if Path("/tmp/asr_ref/asr_real_audio.mp4").is_file():
            try:
                shutil.copyfile("/tmp/asr_ref/asr_real_audio.mp4", tmp)
                made = True
            except OSError:
                made = False
        if not made:
            print("  [SKIP] 缺 /tmp/asr_ref/asr_real_audio.mp4（字幕同步用例跳过）")
        else:
            try:
                page.fill("#asr-path", tmp)
                page.dispatch_event("#asr-path", "input")
                page.wait_for_timeout(1500)
                page.click("#asr-start-btn")
                page.wait_for_timeout(7000)
                try:
                    page.wait_for_function(
                        """() => {
                            const b = document.getElementById('asr-state-badge');
                            return b && b.getAttribute('data-state') === 'done';
                        }""", timeout=120000)
                except Exception:
                    pass
                page.wait_for_timeout(800)

                def _substate() -> dict:
                    return page.evaluate(
                        """() => {
                            const A = window.Qwen3Asr;
                            const sub = document.getElementById('asr-video-sub');
                            const tl = A._buildSubTimeline();
                            const t = document.getElementById('asr-video').currentTime
                                      - (A._audioOriginSec || 0);
                            let cum = 0;
                            for (const ln of tl.lines) {
                                if (ln.start > t) break;
                                for (const w of ln.words) {
                                    if (w.start <= t) cum += w.text.length;
                                    else break;
                                }
                            }
                            const cur = sub.querySelector('.asr-sub-current');
                            const lines = Array.from(
                                sub.querySelectorAll('.asr-sub-line'))
                                .filter(e => e.style.visibility !== 'hidden'
                                             && e.textContent.trim());
                            return {
                                text: (sub.textContent || '').trim(),
                                cum: cum,
                                lines: lines.length,
                                current: cur ? cur.textContent.trim() : '',
                                tlLines: tl.lines.length,
                            };
                        }""")

                states = []
                for tt in (2, 6, 11):
                    page.evaluate(
                        "() => { document.getElementById('asr-video').currentTime = %d; }"
                        % tt)
                    page.wait_for_timeout(700)
                    st = _substate()
                    states.append(st)
                    print(f"  字幕@{tt}s = {st['text'][:66]!r}")
                    print(f"           高亮={st['current']!r} 累计点亮={st['cum']} "
                          f"可见行={st['lines']} 时间轴行={st['tlLines']}")
                final_text = page.evaluate(
                    """() => (document.getElementById('asr-live-committed')
                            .textContent || '').trim()""")
                print(f"  最终文本: {final_text[:66]!r}")

                check("U14 字幕随播放头变化（不是把全文糊在画面上）",
                      len({s["text"] for s in states}) == len(states),
                      f"{[s['text'][:30] for s in states]}")

                def _in_final(txt: str) -> bool:
                    return all(w in final_text for w in txt.split() if w)

                check("U14 字幕内容都在最终文本里（不含会被回滚的猜测）",
                      bool(final_text)
                      and all(_in_final(s["text"]) for s in states if s["text"]),
                      f"final={final_text[:40]!r}")

                cums = [s["cum"] for s in states]
                check("U14 累计点亮的字数随播放头单调不减（真的在往前推进）",
                      all(cums[i] <= cums[i + 1] for i in range(len(cums) - 1))
                      and cums[-1] > cums[0],
                      f"cum={cums}")

                check("U14 当前正在说的词被高亮（YouTube 式逐词推进）",
                      any(s["current"] for s in states),
                      f"{[s['current'] for s in states]}")

                # ---- U16：节奏字幕时间轴的结构不变量 ----
                # 时间轴是**前端本地算**的（round 帧 → 字符时间），
                # 这里直接读它，验证：
                #   * 分成了多行；
                #   * 行的 [start, end] 递增且**不重叠**；
                #   * 行内词的时间递增；
                #   * 还没说到的词「占位但不可见」（不跳字）。
                tl = page.evaluate(
                    """() => window.Qwen3Asr._buildSubTimeline().lines.map(l => ({
                            start: l.start, end: l.end,
                            words: l.words.map(w => ({
                                text: w.text, start: w.start, end: w.end})),
                        }))""")
                print(f"  时间轴: {len(tl)} 行")
                for ln in tl[:8]:
                    print("    [%6.2f-%6.2f] %r"
                          % (ln["start"], ln["end"],
                             "".join(w["text"] for w in ln["words"])))
                check("U16 时间轴分成了多行（不是一整块）",
                      len(tl) >= 2, f"{len(tl)} 行")
                mono_ok = True
                no_overlap = True
                for i, ln in enumerate(tl):
                    for w in ln["words"]:
                        if w["end"] < w["start"] - 1e-6:
                            mono_ok = False
                    for w1, w2 in zip(ln["words"], ln["words"][1:]):
                        if w2["start"] < w1["start"] - 1e-6:
                            mono_ok = False
                    if i and ln["start"] < tl[i - 1]["start"] - 1e-6:
                        mono_ok = False
                    if i and ln["start"] < tl[i - 1]["end"] - 1e-6:
                        no_overlap = False
                check("U16 行内词的时间递增、行的时间递增", mono_ok, "")
                check("U16 相邻行的时间区间不重叠", no_overlap, "")

                pend_css = page.evaluate(
                    """() => {
                        const el = document.querySelector('#asr-video-sub .asr-sub-pending');
                        if (!el) return null;
                        return getComputedStyle(el).opacity;
                    }""")
                check("U16 还没说到的词占位但不可见（字幕不跳字）",
                      pend_css is None or float(pend_css) == 0.0,
                      f"opacity={pend_css}")

                # ---- U17：拖动进度条 → 识别跟过去 → 字幕显示该位置 ----
                #
                # 用户报的 bug：播放 98 分钟的视频时，**拖动进度条**，字幕
                # 不显示拖到的那个位置的内容。
                #
                # 根因：服务端是**从头顺序**识别整个文件的 → 拖到后面的
                # 位置时，那里**还没识别** → 没有字幕。
                #
                # 这里断言三件事：
                #   a) 「已识别区间」的记账与判定（纯前端，确定性）；
                #   b) 时间轴对**乱序**的多段识别按绝对时间排序
                #      （用户往回拖过 → 两段时间倒序）；
                #   c) 拖动到「还没识别」的位置 → 前端会**重开识别**
                #      （新一段的起点 = 拖动点往前 8 秒的引导）。
                cov = page.evaluate(
                    """() => {
                        const A = window.Qwen3Asr;
                        A._subRanges = [];
                        A._subNoteRange(0, 10);
                        A._subNoteRange(10, 20);
                        const inside = A._subCovered(15);
                        const gap = A._subCovered(30);
                        const merged = (A._subRanges || []).map(
                            r => [r.start, r.end]);
                        A._subRanges = [];
                        return { inside: inside, gap: gap, merged: merged };
                    }""")
                check("U17 已识别区间被合并成连续的一段",
                      cov["merged"] == [[0, 20]], f"{cov['merged']}")
                check("U17 区间内判定为「识别过」、区间外判定为「没识别」",
                      cov["inside"] is True and cov["gap"] is False,
                      f"inside={cov['inside']} gap={cov['gap']}")

                # b) 乱序的多段识别 → 时间轴按绝对时间排序
                merged_tl = page.evaluate(
                    """() => {
                        const A = window.Qwen3Asr;
                        const keep = A.rounds;
                        A.rounds = [
                          {round: 1, committed: 'aaa', unfixed: '', language: '',
                           chunk_start_sec: 100, chunk_end_sec: 101},
                          {round: 2, committed: 'aaaa', unfixed: '', language: '',
                           chunk_start_sec: 101, chunk_end_sec: 102},
                          {round: 1, committed: 'bbb', unfixed: '', language: '',
                           chunk_start_sec: 10, chunk_end_sec: 11},
                          {round: 2, committed: 'bbbb', unfixed: '', language: '',
                           chunk_start_sec: 11, chunk_end_sec: 12},
                        ];
                        A._subTl = null; A._subTlKey = -1;
                        const lines = A._buildSubTimeline().lines.map(
                            l => ({start: l.start, end: l.end,
                                  text: l.words.map(w => w.text).join('')}));
                        A.rounds = keep;
                        A._subTl = null; A._subTlKey = -1;
                        return lines;
                    }""")
                starts = [ln["start"] for ln in merged_tl]
                check("U17 多段识别（时间倒序）→ 时间轴按绝对时间排序",
                      bool(starts) and starts == sorted(starts),
                      f"starts={starts}")
                check("U17 倒序的两段都被保留（没有互相吞掉）",
                      len(merged_tl) >= 2
                      and any(ln["text"].startswith("bbb") for ln in merged_tl)
                      and any(ln["text"].startswith("aaa") for ln in merged_tl),
                      f"{[ln['text'] for ln in merged_tl]}")

                # c) 拖动到「还没识别」的位置 → 重开识别
                page.evaluate(
                    """() => {
                        const A = window.Qwen3Asr;
                        // 假装「那个位置还没识别过」→ 触发重开
                        A._subRanges = [];
                        A._seekGuardUntil = 0;
                        A._seekTarget = -1;
                    }""")
                before_rounds = page.evaluate(
                    "() => (window.Qwen3Asr.rounds || []).length")
                page.evaluate(
                    "() => { document.getElementById('asr-video').currentTime = 10; }")
                # 等新一段的轮次（起点 = 拖动点 - 8 秒引导）
                new_seg = None
                for _ in range(40):
                    page.wait_for_timeout(1000)
                    new_seg = page.evaluate(
                        """() => {
                            const rs = window.Qwen3Asr.rounds || [];
                            for (const r of rs) {
                                if (r.chunk_start_sec < 9
                                    && r.chunk_start_sec > 0) {
                                    return {start: r.chunk_start_sec,
                                            n: rs.length};
                                }
                            }
                            return null;
                        }""")
                    if new_seg:
                        break
                print(f"  重开识别: {new_seg}")
                check("U17 拖到没识别过的位置 → 前端重开识别（起点=拖动点-8s）",
                      new_seg is not None and abs(new_seg["start"] - 2.0) < 2.5,
                      f"{new_seg}")
                # 暂停播放 → 「当前行」与「DOM 里渲染的行」是同一个时刻
                page.evaluate(
                    "() => { document.getElementById('asr-video').pause(); }")
                page.wait_for_timeout(400)
                after = page.evaluate(
                    """() => {
                        const A = window.Qwen3Asr;
                        const video = document.getElementById('asr-video');
                        const sub = document.getElementById('asr-video-sub');
                        const t = video.currentTime - (A._audioOriginSec || 0);
                        const lines = A._buildSubTimeline().lines;
                        let exp = -1;
                        for (let i = 0; i < lines.length; i++) {
                            if (lines[i].start <= t) exp = i; else break;
                        }
                        const vis = Array.from(
                            sub.querySelectorAll('.asr-sub-line'))
                            .filter(e => e.style.visibility !== 'hidden'
                                         && e.textContent.trim());
                        return {
                            exp: exp >= 0 ? lines[exp].words.map(
                                w => w.text).join('').trim() : '',
                            shown: vis.map(e => e.textContent.trim()),
                            ranges: (A._subRanges || []).map(
                                r => [Math.round(r.start), Math.round(r.end)]),
                        };
                    }""")
                print(f"  重开后区间: {after['ranges']}")
                print(f"  拖动位置的字幕: {after['shown']}")
                # ⚠️ 先**暂停**再比对：播放头一直在走 → 「读到当前行」和
                # 「DOM 里渲染的是哪一行」可能差一帧（跨过行边界时当前行
                # 会变）。暂停后两者才是同一个时刻。
                check("U17 重开后字幕仍显示拖动位置的内容",
                      bool(after["exp"])
                      and any(after["exp"] in s for s in after["shown"]),
                      f"exp={after['exp'][:40]!r} shown={after['shown']}")
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass

        # ---------------- U18：文字显示框 = 字幕同款转写框 ----------------
        #
        # 用户要求：「含音轨的视频」来源下，主「文字显示框」也按**实时字幕
        # 的格式**输出 —— 同样的分行、同样的逐词点亮、同样的当前词高亮 ——
        # 而且**历史全部保留**，框可以**上拉回看**。
        #
        # 用 90 秒的样本（行数够多 → 框真的能滚起来；15 秒的样本只有 7 行，
        # 装不满，测不了滚动）。
        print("\n--- U18 文字显示框 = 字幕同款转写框 ---")
        long_mp4 = Path("/tmp/asr_ref/asr_long_90s.mp4")
        tmp_long = "/userdata/assets/uploads/_asr_transcript_test.mp4"
        have_long = False
        if long_mp4.is_file():
            try:
                shutil.copyfile(str(long_mp4), tmp_long)
                have_long = True
            except OSError:
                have_long = False
        if not have_long:
            print("  [SKIP] 缺 /tmp/asr_ref/asr_long_90s.mp4（U18 跳过）")
        else:
            try:
                page.click('#asr-source-tabs .asr-source-tab[data-source=video]')
                page.wait_for_timeout(500)
                page.fill("#asr-path", tmp_long)
                page.dispatch_event("#asr-path", "input")
                page.wait_for_timeout(900)
                page.click("#asr-start-btn")
                try:
                    page.wait_for_function(
                        """() => {
                            const b = document.getElementById('asr-state-badge');
                            return b && b.getAttribute('data-state') === 'done';
                        }""", timeout=180000)
                except Exception:
                    pass
                page.wait_for_timeout(1500)
                # 暂停 → 「当前行」与「DOM 里渲染的行」是同一个时刻
                page.evaluate(
                    "() => { document.getElementById('asr-video').pause(); }")
                page.wait_for_timeout(400)
                # ⚠️ 显示模式要切到「实时结果」—— 「文字显示框」（也就是
                # 转写框的宿主）在「识别过程」模式下是**收起来**的
                # （那是模式本身的契约：那块地方让给逐轮明细）。
                # 向导用例跑完会把模式留在「识别过程」，这里显式切回来。
                page.select_option("#asr-mode", "single")
                page.wait_for_timeout(500)

                tr = page.evaluate(
                    """() => {
                        const A = window.Qwen3Asr;
                        const box = document.getElementById('asr-transcript');
                        const body = document.getElementById('asr-transcript-body');
                        const tl = A._buildSubTimeline().lines;
                        const dom = Array.from(body.querySelectorAll('.asr-sub-line'));
                        const curEl = body.querySelector('.asr-tr-current');
                        const curW = body.querySelector(
                            '.asr-tr-current .asr-sub-current');
                        const cs = getComputedStyle(body);
                        // 逐行比对 DOM 与时间轴
                        let mismatch = -1;
                        for (let i = 0; i < Math.max(tl.length, dom.length); i++) {
                            const t = tl[i] ? tl[i].words.map(w => w.text).join('') : null;
                            const d = dom[i] ? dom[i].textContent : null;
                            if (t !== d) { mismatch = i; break; }
                        }
                        return {
                            hidden: box.classList.contains('hidden'),
                            visible: body.getClientRects().length > 0,
                            tlLines: tl.length,
                            domLines: dom.length,
                            mismatch: mismatch,
                            mismatchTl: mismatch >= 0 && tl[mismatch]
                                ? tl[mismatch].words.map(w => w.text).join('') : '',
                            mismatchDom: mismatch >= 0 && dom[mismatch]
                                ? dom[mismatch].textContent : '',
                            curIdx: dom.findIndex(
                                e => e.classList.contains('asr-tr-current')),
                            curText: curEl ? curEl.textContent.trim() : '',
                            curWord: curW ? curW.textContent.trim() : '',
                            words: body.querySelectorAll('.asr-sub-word').length,
                            overflowY: cs.overflowY,
                            scrollH: body.scrollHeight,
                            clientH: body.clientHeight,
                            scrollTop: Math.round(body.scrollTop),
                            follow: A._trFollow,
                            // 「单行实时结果」那两个 span 只是镜像 —— 还得有值
                            mirror: (document.getElementById('asr-live-committed')
                                     .textContent || '').trim().length,
                        };
                    }""")
                print(f"  转写框: 时间轴 {tr['tlLines']} 行 / DOM {tr['domLines']} 行 "
                      f"（{tr['words']} 个词）")
                print(f"  滚动: scrollH={tr['scrollH']} clientH={tr['clientH']} "
                      f"overflow-y={tr['overflowY']}")
                print(f"  当前行 #{tr['curIdx']}: {tr['curText'][:44]!r} "
                      f"当前词={tr['curWord']!r}")

                check("U18 视频来源下文字框变成字幕同款转写框（可见）",
                      tr["hidden"] is False and tr["visible"] is True,
                      f"hidden={tr['hidden']} visible={tr['visible']}")
                check("U18 转写框保留**全部**历史行（行数 == 时间轴行数）",
                      tr["domLines"] == tr["tlLines"] and tr["tlLines"] >= 10,
                      f"dom={tr['domLines']} tl={tr['tlLines']}")
                check("U18 转写框逐行与时间轴一致（没有停在旧文本上）",
                      tr["mismatch"] < 0,
                      f"行#{tr['mismatch']} tl={tr['mismatchTl'][:40]!r} "
                      f"dom={tr['mismatchDom'][:40]!r}")
                check("U18 当前行被标出、当前正在说的词高亮",
                      tr["curIdx"] >= 0 and bool(tr["curWord"]),
                      f"curIdx={tr['curIdx']} word={tr['curWord']!r}")
                check("U18 转写框可滚动（overflow-y: auto 且内容超出框高）",
                      tr["overflowY"] == "auto" and tr["scrollH"] > tr["clientH"],
                      f"overflow={tr['overflowY']} "
                      f"{tr['scrollH']}>{tr['clientH']}")
                check("U18 「单行实时结果」那两个 span 仍保留最终文本（镜像）",
                      tr["mirror"] > 0, f"{tr['mirror']} 字")

                # 上翻回看 → 不该被拽回
                #
                # ⚠️ 要滚到**当前行离开视野**的位置：当前行还在视野里时
                # 跟随本来就该继续（用户没走远），只有「当前行不见了」
                # 才算「用户去回看历史了」→ 暂停跟随，别把人拽回来。
                page.evaluate(
                    """() => {
                        const b = document.getElementById('asr-transcript-body');
                        b.scrollTop = b.scrollHeight;
                        b.dispatchEvent(new Event('scroll'));
                    }""")
                page.wait_for_timeout(300)
                up1 = page.evaluate(
                    """() => {
                        const b = document.getElementById('asr-transcript-body');
                        return {top: Math.round(b.scrollTop),
                                follow: window.Qwen3Asr._trFollow};
                    }""")
                page.evaluate("() => window.Qwen3Asr._renderTranscript()")
                page.wait_for_timeout(300)
                up2 = page.evaluate(
                    """() => {
                        const b = document.getElementById('asr-transcript-body');
                        return {top: Math.round(b.scrollTop),
                                follow: window.Qwen3Asr._trFollow};
                    }""")
                print(f"  上翻后 scrollTop={up1['top']} follow={up1['follow']}"
                      f" → 再渲染 {up2['top']} follow={up2['follow']}")
                check("U18 用户上翻回看时不被拽回（跟随暂停）",
                      up1["follow"] is False and up2["top"] == up1["top"]
                      and up2["follow"] is False,
                      f"{up1} → {up2}")

                # 画面字幕与文字转写框的**当前行 / 当前词**必须一致
                # —— 「显示效果一样」不是「看起来像」，是同一份算法算出来的。
                same_line = []
                for tt in (3.0, 12.0, 33.0, 58.0):
                    page.evaluate(
                        "() => { document.getElementById('asr-video').pause(); }")
                    page.evaluate(
                        "() => { document.getElementById('asr-video').currentTime = %f; }"
                        % tt)
                    page.wait_for_timeout(400)
                    page.evaluate("() => window.Qwen3Asr._renderTranscript()")
                    page.wait_for_timeout(200)
                    same_line.append(page.evaluate(
                        """() => {
                            const sub = document.getElementById('asr-video-sub');
                            const body = document.getElementById('asr-transcript-body');
                            const slots = Array.from(
                                sub.querySelectorAll('.asr-sub-line'))
                                .filter(e => e.style.visibility !== 'hidden');
                            const subCur = slots.length ? slots[slots.length - 1] : null;
                            const trCur = body.querySelector('.asr-tr-current');
                            const subWord = sub.querySelector('.asr-sub-current');
                            const trWord = body.querySelector(
                                '.asr-tr-current .asr-sub-current');
                            return {
                                subLine: subCur ? subCur.textContent.trim() : '',
                                trLine: trCur ? trCur.textContent.trim() : '',
                                subWord: subWord ? subWord.textContent.trim() : '',
                                trWord: trWord ? trWord.textContent.trim() : '',
                            };
                        }"""))
                print("  字幕/转写当前行: "
                      + " | ".join(f"{s['subLine'][:18]!r}" for s in same_line))
                check("U18 画面字幕与文字转写框的当前行**逐字一致**",
                      bool(same_line)
                      and all(s["subLine"] == s["trLine"] and s["subLine"]
                              for s in same_line),
                      f"{[s['subLine'][:20] for s in same_line]} vs "
                      f"{[s['trLine'][:20] for s in same_line]}")
                check("U18 画面字幕与文字转写框的**当前词**一致",
                      bool(same_line)
                      and all(s["subWord"] == s["trWord"] for s in same_line),
                      f"{[s['subWord'] for s in same_line]} vs "
                      f"{[s['trWord'] for s in same_line]}")

                # 「识别过程」模式 → 文字框让位给逐轮明细（模式本身的契约）
                page.select_option("#asr-mode", "rounds")
                page.wait_for_timeout(500)
                in_rounds = page.evaluate(
                    """() => {
                        const A = window.Qwen3Asr;
                        const live = document.getElementById('asr-live');
                        const rounds = document.getElementById('asr-rounds');
                        const rows = Array.from(
                            rounds.querySelectorAll('.asr-round'));
                        // 期望值：按老算法（整表重建）重算一遍，逐行比对
                        let prev = '';
                        const exp = [];
                        (A.rounds || []).forEach((r) => {
                            exp.push({
                                no: '#' + r.round,
                                add: (r.committed && r.committed.startsWith(prev))
                                    ? r.committed.slice(prev.length)
                                    : (r.committed || ''),
                                tail: r.unfixed || '',
                            });
                            prev = r.committed || prev;
                        });
                        const got = rows.map((e) => ({
                            no: e.querySelector('.asr-round-no').textContent,
                            add: e.querySelector('.asr-round-add').textContent,
                            tail: e.querySelector('.asr-round-tail').textContent,
                        }));
                        let bad = -1;
                        for (let i = 0; i < Math.max(exp.length, got.length); i++) {
                            if (JSON.stringify(exp[i]) !== JSON.stringify(got[i])) {
                                bad = i; break;
                            }
                        }
                        return {liveHidden: live.classList.contains('hidden'),
                                roundsHidden: rounds.classList.contains('hidden'),
                                rows: rows.length, exp: exp.length, bad: bad,
                                badExp: bad >= 0 ? exp[bad] : null,
                                badGot: bad >= 0 ? got[bad] : null};
                    }""")
                check("U18 「识别过程」模式下文字框让位给逐轮明细",
                      in_rounds["liveHidden"] is True
                      and in_rounds["roundsHidden"] is False,
                      f"{in_rounds}")
                # 逐轮面板改成**增量追加**之后，内容必须和「整表重建」一样
                check("U18 逐轮明细增量追加 → 行数与内容都和整表重建一致",
                      in_rounds["rows"] == in_rounds["exp"]
                      and in_rounds["bad"] < 0,
                      f"rows={in_rounds['rows']} exp={in_rounds['exp']} "
                      f"bad={in_rounds['bad']} "
                      f"{in_rounds.get('badExp')} != {in_rounds.get('badGot')}")
                page.select_option("#asr-mode", "single")
                page.wait_for_timeout(400)

                # 切回「音频文件」→ 转写框收起，回到单行「实时结果」
                page.click('#asr-source-tabs .asr-source-tab[data-source=file]')
                page.wait_for_timeout(800)
                back = page.evaluate(
                    """() => {
                        const box = document.getElementById('asr-transcript');
                        const c = document.getElementById('asr-live-committed');
                        const cs = getComputedStyle(c);
                        return {hidden: box.classList.contains('hidden'),
                                display: cs.display,
                                text: (c.textContent || '').trim().length};
                    }""")
                check("U18 切回音频文件来源 → 转写框收起、单行结果恢复显示",
                      back["hidden"] is True and back["display"] != "none",
                      f"{back}")
            finally:
                try:
                    os.remove(tmp_long)
                except OSError:
                    pass

        # U15：**换来源不能让上一来源的结果串台**（用户报的 bug）。
        #
        # 症状：「音频文件」识别结束后切到「含音轨的视频」，画面下方凭空
        # 多出一句「嗯哼。」—— 那是**音频文件**第 1 轮的瞬时猜测
        # （committed='' / unfixed='嗯哼。'），第 2 轮就被回滚推翻了，
        # 最终文本里根本没有这句；而这个视频**还没开始识别**。
        #
        # 两个成因，都要挡住：
        #   1. 轮次跨来源保留 → 字幕按播放头取轮时取到了上一来源的轮次；
        #   2. 字幕取的是那一轮的 ``unfixed`` —— 那是**会被回滚**的猜测。
        print("\n--- U15 换来源不串台 ---")
        wav = _pick_wav_sample()
        ref_mp4 = Path("/tmp/asr_ref/asr_real_audio.mp4")
        tmp_mp4 = "/userdata/assets/uploads/_asr_switch_test.mp4"
        have_mp4 = False
        if ref_mp4.is_file():
            try:
                shutil.copyfile(str(ref_mp4), tmp_mp4)
                have_mp4 = True
            except OSError:
                have_mp4 = False
        if not wav or not have_mp4:
            print("  [SKIP] 缺 wav / 参考 mp4 素材（U15 跳过）")
        else:
            try:
                # 1) 先用「音频文件」跑一遍
                page.click('#asr-source-tabs .asr-source-tab[data-source=file]')
                page.wait_for_timeout(400)
                page.fill("#asr-path", wav)
                page.dispatch_event("#asr-path", "input")
                page.wait_for_timeout(600)
                page.click("#asr-start-btn")
                try:
                    page.wait_for_function(
                        """() => {
                            const b = document.getElementById('asr-state-badge');
                            return b && b.getAttribute('data-state') === 'done';
                        }""", timeout=120000)
                except Exception:
                    pass
                page.wait_for_timeout(1500)
                file_text = page.evaluate(
                    """() => (document.getElementById('asr-live-committed')
                            .textContent || '').trim()""")
                print(f"  音频文件结果: {file_text[:60]!r}")
                check("U15 音频文件识别出了文本", bool(file_text),
                      f"{len(file_text)} 字")

                # 2) 切到「含音轨的视频」—— **还没开始识别**
                page.click('#asr-source-tabs .asr-source-tab[data-source=video]')
                page.wait_for_timeout(1200)
                sub_after_switch = page.evaluate(
                    """() => (document.getElementById('asr-video-sub')
                            .textContent || '').trim()""")
                check("U15 切到视频来源后字幕是空的（没有上一来源的残留）",
                      sub_after_switch == "",
                      f"字幕={sub_after_switch[:60]!r}")

                # 3) 视频识别完成后，字幕里的每个字都必须在**最终文本**里
                #
                # ⚠️ 语义跟着字幕实现一起改了：现在是「YouTube 式节奏字幕」
                # —— 画面上是**当前那几行**，不是「最终文本的前缀」。
                # 所以要验的是「字幕里的每个词都出现在最终文本里」。
                page.fill("#asr-path", tmp_mp4)
                page.dispatch_event("#asr-path", "input")
                page.wait_for_timeout(800)
                page.click("#asr-start-btn")
                try:
                    page.wait_for_function(
                        """() => {
                            const b = document.getElementById('asr-state-badge');
                            return b && b.getAttribute('data-state') === 'done';
                        }""", timeout=120000)
                except Exception:
                    pass
                page.wait_for_timeout(1500)
                final_text = page.evaluate(
                    """() => (document.getElementById('asr-live-committed')
                            .textContent || '').trim()""")
                page.evaluate(
                    "() => { document.getElementById('asr-video').currentTime = 2; }")
                page.wait_for_timeout(900)
                sub = page.evaluate(
                    """() => (document.getElementById('asr-video-sub')
                            .textContent || '').trim()""")
                print(f"  视频结果: {final_text[:60]!r}")
                print(f"  字幕@2s : {sub[:60]!r}")
                check("U15 字幕里的每个词都在最终文本里（不含会被回滚的猜测）",
                      bool(sub) and bool(final_text)
                      and all(w in final_text for w in sub.split() if w),
                      f"字幕={sub[:60]!r} 最终={final_text[:60]!r}")
            finally:
                try:
                    os.remove(tmp_mp4)
                except OSError:
                    pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headful", action="store_true")
    ap.add_argument("--shot-dir", default="/tmp/asr_ref/wizard_shots")
    args = ap.parse_args()

    shot_dir = Path(args.shot_dir)
    shot_dir.mkdir(parents=True, exist_ok=True)

    exe = _find_chrome()
    if not exe:
        print("[SKIP] 找不到可用的 chromium（playwright install chromium 试试）")
        return 0
    print(f"[INFO] chromium: {exe}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not args.headful, args=["--no-sandbox"],
            executable_path=exe)
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        # ---------------- 应用面板的预览视频 ----------------
        # manifest 的 preview_video → 卡片 static/ 下的 mp4 → 应用面板里
        # 循环播放。判据要查**真解码**（readyState + videoWidth），
        # 光有 <video> 标签说明不了文件能播（路径写错 / 文件损坏都会
        # 渲染成一个黑框）。
        #
        # 必须在**应用面板可见时**验 —— 隐藏容器里的 <video> 浏览器可能
        # 只加载 metadata 不解码，判据会飘。
        #
        # 首屏**不加载**视频：<video> 只带 poster（服务端抽的首帧 JPEG），
        # 鼠标悬停到那张卡才补 src 并播 —— 否则浏览器会整段下载所有预览
        # 视频（实测 9 张卡 3.2 MB）。所以这里要**先悬停**再验解码。
        print("\n=== 应用面板：预览视频 ===")
        goto_home(page)
        vid = page.evaluate("""() => {
            const a = document.querySelector('.app-card[data-page=qwen3-asr]');
            const v = a && a.querySelector('video.app-card-video');
            if (!v) return null;
            return { src: v.getAttribute('src'),
                     dataSrc: v.getAttribute('data-preview-src'),
                     poster: v.getAttribute('poster'),
                     readyState: v.readyState,
                     w: v.videoWidth, h: v.videoHeight,
                     dur: v.duration, loop: v.loop, muted: v.muted };
        }""")
        print("  " + (str(vid) if vid else "没有 <video> 元素"))
        check("W9 应用面板的 ASR 卡片有预览视频", vid is not None, f"vid={vid}")
        if vid:
            check("W9 预览视频指向卡片 static/ 下的 mp4",
                  (vid["dataSrc"] or "").split("?")[0]
                  == "/static/apps/qwen3-asr/preview.mp4",
                  f"data-preview-src={vid['dataSrc']}")
            check("W9 首屏不加载视频（无 src，只带 poster）",
                  not vid["src"] and bool(vid["poster"]),
                  f"src={vid['src']} poster={vid['poster']}")
            # 悬停 → 补 src → 真正解码出画面才算能播
            try:
                page.hover('.app-card[data-page=qwen3-asr]')
            except Exception as exc:
                check(False, "W9 悬停到 ASR 卡片", repr(exc))
            try:
                page.wait_for_function(
                    """() => {
                        const a = document.querySelector(
                            '.app-card[data-page=qwen3-asr]');
                        const v = a && a.querySelector('video.app-card-video');
                        return !!(v && v.readyState >= 2 && v.videoWidth > 0);
                    }""", timeout=20000)
                decoded = True
            except Exception:
                decoded = False
            check("W9 悬停后预览视频能真正解码出画面", decoded, f"vid={vid}")
            check("W9 预览视频是静音循环的", vid["loop"] and vid["muted"],
                  f"loop={vid['loop']} muted={vid['muted']}")
        page.screenshot(path=str(shot_dir / "0_app_grid.png"), full_page=True)

        open_wizard(page)

        # ---------------- 第 0 步：卡片页本身存在 ----------------
        # 回归护栏：**首页 index.html.template 里必须有
        # {% card_slot "qwen3-asr" %}**。缺了它，卡片页面段根本不会
        # 被拼进首页，向导容器会 fallback 到 document.body.appendChild
        # —— 表现为「向导浮在别的卡片下面」，用户看到的界面就整个是错的。
        sec = page.evaluate("""() => {
            const el = document.getElementById('page-qwen3-asr');
            if (!el) return null;
            return { parent: el.parentNode.tagName,
                     siblings: el.parentNode.children.length };
        }""")
        check("W0 卡片页 section 在首页 DOM 里（card_slot 已声明）",
              sec is not None, f"section={sec}")
        wiz_parent = page.evaluate(
            f"""() => {{
                const el = document.getElementById('{NS}root-{APP}');
                const pg = document.getElementById('page-{APP}');
                return el && pg && el.parentNode === pg.parentNode
                    ? 'sibling' : 'other';
            }}""")
        check("W0 向导容器挂在卡片页旁边（不是 body 末尾）",
              wiz_parent == "sibling", f"parent={wiz_parent}")

        # ---------------- 第 1 步：运行环境检查 ----------------
        print("\n=== 第 1 步：运行环境检查 ===")
        rows = page.eval_on_selector_all(
            f"#{NS}root-{APP} .{NS}env-row",
            "els => els.map(e => e.textContent.trim())")
        print("  env 行: " + " | ".join(rows))
        check("W1 env 步渲染出能力行", len(rows) > 0, f"rows={rows}")
        check("W1 列出 swcap_audio_asr 能力",
              any("swcap_audio_asr" in r for r in rows), f"rows={rows}")
        page.screenshot(path=str(shot_dir / "1_env.png"))

        # ---------------- 第 2 步：音频源 ----------------
        print("\n=== 第 2 步：音频源 ===")
        click_next(page)
        check("W2 步骤标题是「音频源」", step_title(page) == "音频源",
              step_title(page))
        ids = field_ids(page)
        print("  字段: " + ", ".join(ids))
        check("W2 有音频源字段", "asr-audio" in ids, f"ids={ids}")
        check("W2 有显示模式字段", "asr-mode" in ids, f"ids={ids}")
        # 三个控件：文本框 + 浏览… + 上传…
        has_text = page.locator(
            f'#{NS}root-{APP} [data-field-id="asr-audio"]').count() > 0
        btns = page.eval_on_selector_all(
            f"#{NS}root-{APP} .{NS}browse-btn",
            "els => els.map(e => e.textContent.trim())")
        print("  按钮: " + ", ".join(btns))
        check("W2 音频源是文本框（设备端路径）", has_text, f"ids={ids}")
        check("W2 有「浏览…」按钮", any("浏览" in b for b in btns), f"btns={btns}")
        check("W2 有「上传…」按钮", any("上传" in b for b in btns), f"btns={btns}")
        # 显示模式的下拉选项
        opts = page.eval_on_selector_all(
            f'#{NS}root-{APP} [data-field-id="asr-mode"] option',
            "els => els.map(e => e.textContent.trim())")
        print("  显示模式: " + ", ".join(opts))
        check("W3 显示模式有「实时结果」和「识别过程」",
              any("实时结果" in o for o in opts) and any("识别过程" in o for o in opts),
              f"opts={opts}")
        # UI 文案不该再出现「已确认 / 未确认尾巴 / 逐轮」这类内部术语
        check("W3 显示模式文案不含内部术语",
              not any(k in " ".join(opts)
                      for k in ("已确认", "未确认", "尾巴", "逐轮", "明细")),
              f"opts={opts}")
        # W4：不该有 vtl 的 gscore / 判帧字段，也不该有 ASR 的流式参数
        labels = " ".join(field_labels(page))
        check("W4 没有 gscore / 判帧 / 阈值 / 帧间隔字段",
              not any(k in labels for k in ("gscore", "判帧", "阈值", "帧间隔")),
              f"labels={labels}")
        check("W4 音频源步也没有流式参数字段",
              not any(k in labels for k in ("分块", "token", "回退", "前几轮")),
              f"labels={labels}")
        page.screenshot(path=str(shot_dir / "2_source.png"))

        # ---------------- 第 3 步：完成配置 → 真的发起识别 ----------------
        # 流程只有 3 步：运行环境检查 → 音频源 → 完成配置。
        # **没有「识别参数」步** —— 分块长度 / token 上限 / 回退策略
        # 等于 C++ demo 的 ONLINE_* 常量，属于内部契约，不暴露给用户。
        print("\n=== 第 3 步：完成配置 → 发起识别 ===")
        set_field(page, "asr-audio", WAV)
        click_next(page)
        check("W5 步骤标题是「完成配置」", step_title(page) == "完成配置",
              step_title(page))
        # 向导总步数 = 3（env / source / finish），没有识别参数步
        nsteps = page.evaluate(
            f"""() => {{
                const el = document.getElementById('{NS}root-{APP}');
                return el ? el.querySelectorAll('.{NS}step-dot').length : -1;
            }}""")
        print(f"  向导步骤数: {nsteps}")
        check("W5 向导只有 3 步（没有识别参数步）",
              nsteps in (3, -1), f"steps={nsteps}")
        # **直接读 wizard.json** —— 比查 DOM 硬：DOM 只能看到当前这一步，
        # 配置里藏着的参数步照样能过 DOM 检查。
        cfg = json.loads(
            urllib.request.urlopen(
                BASE + "/static/apps/qwen3-asr/wizard.json",
                timeout=5).read().decode("utf-8"))
        step_ids = [s.get("id") for s in cfg.get("steps", [])]
        print("  wizard.json steps: " + ", ".join(map(str, step_ids)))
        check("W5 wizard.json 里没有识别参数步",
              "params" not in step_ids, f"steps={step_ids}")
        cfg_fields = [f.get("id") for s in cfg.get("steps", [])
                      for f in (s.get("fields") or [])]
        check("W5 wizard.json 里没有流式参数字段",
              not any(k in " ".join(cfg_fields)
                      for k in ("chunk", "token", "rollback")),
              f"fields={cfg_fields}")
        # 功能页也不该有「识别参数」UI
        page_params = page.evaluate(
            """() => {
                const bad = [];
                for (const sel of ['.asr-params', '#asr-chunk-seconds',
                                   '#asr-max-new-tokens', '#asr-rollback-tokens',
                                   '#asr-rollback-rounds'])
                    if (document.querySelector(sel)) bad.push(sel);
                return bad;
            }""")
        check("W5 功能页没有「识别参数」UI",
              not page_params, f"found={page_params}")
        # 功能页的「显示模式」下拉也要用同一套大白话文案（且要短 ——
        # <select> 收起态会裁掉 option 文本，见 AGENTS.md）
        page_modes = page.evaluate(
            """() => [...document.querySelectorAll(
                   '#page-qwen3-asr #asr-mode option')]
                .map(o => o.textContent.trim())""")
        check("W5 功能页的显示模式也是大白话文案",
              page_modes == ["实时结果", "识别过程"], f"opts={page_modes}")

        page.wait_for_selector(f"#{NS}root-{APP} #{NS}finish-btn", timeout=10000)
        # 等识别结果出现在功能页上（**按后端 request_id 判这一次跑完**）
        req_before = api_state().get("current_request_id") or 0
        text = ""
        state = ""
        snap = {}
        page.click(f"#{NS}root-{APP} #{NS}finish-btn")
        snap = wait_run_done(req_before)
        state = snap.get("state") or "?"
        text = dom_transcript(page)
        print(f"  状态: {state} · 轮数: {snap.get('round_count')} "
              f"· 文本: {text[:90]!r}")
        page.screenshot(path=str(shot_dir / "4_done.png"))
        check("W6 向导走完真的发起了识别（页面出现转写文本）",
              bool(text.strip()), f"text={text[:80]!r}")
        check("W6 转写文本与 C++ demo 一致",
              text.strip() == C_FINAL, f"text={text[:120]!r}")
        check("W6 后端最终文本与 C++ demo 一致",
              (snap.get("last_result") or {}).get("text", "").strip()
              == C_FINAL,
              f"text={str((snap.get('last_result') or {}).get('text'))[:120]!r}")
        # 向导走完 → unmount() 把卡片页放回来，用户才看得到结果
        shown = page.evaluate(
            """() => {
                const el = document.getElementById('page-qwen3-asr');
                return el ? !el.classList.contains('hidden') : 'no-section';
            }""")
        check("W6 向导结束后卡片页被放回（能看结果）",
              shown is True, f"visible={shown}")

        # ---------------- 视频路径 ----------------
        if Path(MP4).is_file():
            print("\n=== 视频路径（设备端 mp4，含音轨）===")
            page.evaluate(
                "(id) => { window.location.hash = ''; }", APP)
            page.wait_for_timeout(500)
            open_wizard(page)
            click_next(page)          # env → source
            set_field(page, "asr-audio", MP4)
            click_next(page)          # source → finish（3 步流程，没有识别参数步）
            page.wait_for_selector(f"#{NS}root-{APP} #{NS}finish-btn", timeout=10000)
            vreq = api_state().get("current_request_id") or 0
            page.click(f"#{NS}root-{APP} #{NS}finish-btn")
            vsnap = wait_run_done(vreq)
            vtext = dom_transcript(page)
            print(f"  状态: {vsnap.get('state')} · 轮数: "
                  f"{vsnap.get('round_count')} · 文本: {vtext[:90]!r}")
            page.screenshot(path=str(shot_dir / "5_video.png"))
            check("W7 设备端视频（mp4）也能识别且文本一致",
                  vtext.strip() == C_FINAL, f"text={vtext[:120]!r}")
            check("W7 后端最终文本与 C++ demo 一致",
                  (vsnap.get("last_result") or {}).get("text", "").strip()
                  == C_FINAL,
                  f"text={str((vsnap.get('last_result') or {}).get('text'))[:120]!r}")
        else:
            print(f"\n（跳过视频用例：{MP4} 不存在）")

        # ---------------- 逐轮明细不覆盖「完整 JSON」 ----------------
        # 用户报过的 bug：切到「逐轮明细」后，逐轮列表**压住**下面的
        # 「完整 JSON」。
        #
        # 成因（不是定位问题，是**布局溢出**）：framework.css 的
        # `.imb-card` 是 `height: calc(100vh - 220px)` 的固定高度卡片，
        # `.imb-card-body` 是 flex item 却没写 `min-height: 0` ——
        # flex item 的 `min-height: auto` = min-content 高度，内容一旦
        # 超过分配高度，body 就撑出卡片；`details.raw-output` 是
        # `.imb-card` 的**兄弟**、按卡片高度定位，于是被溢出部分压住。
        #
        # 判据：逐轮列表的底边不能超过「完整 JSON」的顶边。
        print("\n=== 显示模式切换：逐轮明细 vs 完整 JSON ===")
        page.select_option("#asr-mode", "rounds")
        page.wait_for_timeout(800)
        geom = page.evaluate(
            """() => {
                const R = '#page-qwen3-asr ';
                const g = (s) => {
                    const e = document.querySelector(R + s);
                    if (!e) return null;
                    const b = e.getBoundingClientRect();
                    return { top: b.top, bottom: b.bottom,
                             hidden: e.classList.contains('hidden') };
                };
                return { rounds: g('#asr-rounds'), raw: g('details.raw-output') };
            }""")
        ro, raw = geom.get("rounds") or {}, geom.get("raw") or {}
        print(f"  rounds.bottom={ro.get('bottom')} raw.top={raw.get('top')}")
        overlap = (not ro.get("hidden")) and ro.get("bottom", 0) > raw.get("top", 0)
        check("W8 逐轮明细没有覆盖「完整 JSON」", not overlap,
              f"rounds.bottom={ro.get('bottom')} raw.top={raw.get('top')}")
        page.screenshot(path=str(shot_dir / "6_rounds_mode.png"))

        # ---------------- 四种输入源 + 不再 60 s 截断 ----------------
        verify_sources(page, shot_dir)

        check("页面没有 JS 报错", not errors, f"errors={errors[:3]}")
        browser.close()

        # ---------------- 实时（麦克风）链路 ----------------
        verify_live(p, shot_dir)

    print(f"\n=== {PASSED} 通过 / {len(FAILED)} 失败 ===")
    for f in FAILED:
        print(f"  ✗ {f}")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
