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
# 让 ``active_app_guard`` 可导入（拦下改生产配置的 active-app 写操作）
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from active_app_guard import (  # noqa: E402
    install_on_page as install_active_app_guard)

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


def release_npu() -> None:
    """把 NPU 让出来 —— 后端**一次只能跑一个识别**。

    后面的用例（live 的「文件路径参考」）要自己跑一遍识别；前面的用例若
    还占着识别，它就会拿到 409「上一条识别还在进行中」，而且它取的
    「参考文本」是**上一条识别的残留** → 一串假失败。

    实测：U21（拖动进度条）跑完没收尾 → live 的 V3/V4/V5c 全挂，
    根因就是这个（V3 报 409，V4/V5c 是被 V3 的参考文本带偏的）。
    """
    try:
        api("POST", "/api/qwen3-asr/stop")
    except Exception:  # noqa: BLE001 — 收尾失败无所谓
        pass


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
    """uploads 目录里挑一个**小**的 mp4 当视频播放用例的样本。

    ⚠️ **必须排除测试自己的临时文件**（``_asr_*``）—— 它们是各个用例
    复制进去、用完删掉的**草稿**。板子重启 / 用例崩了就会留下残骸，
    而本函数取的是「最小的 mp4」⇒ 残骸会**插队**成为样本。

    实测踩过：重启后残留的 ``_asr_switch_test.mp4``（142 KB）比
    ``rk-0.mp4``（1.07 MB）小 ⇒ 被挑中 ⇒ U12「视频能真正解码出画面」
    红了（``videoWidth=0``），而样本其实换了另一个视频轨。

    ⇒ 只认「用户自己的」素材（不以 ``_`` 开头）。
    """
    try:
        from app.core.config import default_resource_root
        d = default_resource_root() / "uploads"
    except Exception:  # noqa: BLE001
        return ""
    try:
        cands = [p for p in d.glob("*.mp4")
                 if p.is_file()
                 and not p.name.startswith(("_", "."))
                 and p.stat().st_size < 20 * 1024 * 1024]
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
    # ⚠️ 拦下 active-app 的写操作：DELETE 会连带清空 compute.autostart
    # （设备级配置）⇒ 回归脚本不该改生产配置。详见 active_app_guard.py。
    install_active_app_guard(page)
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
    # ⚠️ 「默认选中『音频文件』」必须在**干净状态**下验。
    #
    # 页面会**恢复**上一批次的来源（``_restoreFromServer`` → 按服务端的
    # ``rounds_source.kind`` 切页签）—— 这是有意的行为（U19 有断言：
    # 刷新后页签回到「含音轨的视频」）。而上一段（W7 视频向导）刚好留
    # 下了 video 归属的轮次 ⇒ 不清干净的话，U1 量到的是「恢复」而不是
    # 「默认」。
    try:
        api("POST", "/api/qwen3-asr/reset")
    except Exception:  # noqa: BLE001 — 收尾失败不致命
        pass
    page.reload(wait_until="domcontentloaded")
    goto_card(page, APP)
    page.wait_for_timeout(1200)

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
        page.fill("#asr-source", sample)
        page.dispatch_event("#asr-source", "input")
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
                page.fill("#asr-source", tmp)
                page.dispatch_event("#asr-source", "input")
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
                page.fill("#asr-source", tmp_long)
                page.dispatch_event("#asr-source", "input")
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
                page.fill("#asr-source", wav)
                page.dispatch_event("#asr-source", "input")
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
                page.fill("#asr-source", tmp_mp4)
                page.dispatch_event("#asr-source", "input")
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


def verify_reset(page, shot_dir: Path) -> None:
    """U22: 「清空」= 真清干净（识别中点也不长回来）+ 画面停住。

    用户把「清空」挪到了「停止」左边（运行控制区）—— 位置显眼了，
    **误点概率也高了**：识别进行中点「清空」是很自然的手滑。

    而原来的后端 ``reset()`` 只清结果、**不停正在跑的识别** → 那个
    线程继续推轮次 → 结果**又长回来**（实测：32 轮 → 清成 3 →
    4.5 s 后涨回 26，「清空」等于没点）。
    """
    print("\n--- U22 「清空」：真清干净 + 画面停住 ---")
    long_mp4 = Path("/tmp/asr_ref/asr_long_90s.mp4")
    tmp_mp4 = "/userdata/assets/uploads/_asr_reset_test.mp4"
    have_mp4 = False
    if long_mp4.is_file():
        try:
            shutil.copyfile(str(long_mp4), tmp_mp4)
            have_mp4 = True
        except OSError:
            have_mp4 = False
    if not have_mp4:
        print("  [SKIP] 缺 /tmp/asr_ref/asr_long_90s.mp4（U22 跳过）")
        return

    def _probe():
        return page.evaluate("""() => {
            const A = window.Qwen3Asr || {};
            const v = document.getElementById('asr-video');
            const body = document.getElementById('asr-transcript-body');
            const live = document.getElementById('asr-live-committed');
            return {
                rounds: (A.rounds || []).length,
                state: (A.snapshot && A.snapshot.state) || '',
                busy: !!(A.snapshot && A.snapshot.busy),
                paused: v ? !!v.paused : null,
                rows: body ? body.querySelectorAll('.asr-sub-line').length : 0,
                live: live ? (live.textContent || '').trim().length : 0,
            };
        }""")

    try:
        page.select_option("#asr-mode", "single")
        page.click('#asr-source-tabs .asr-source-tab[data-source=video]')
        page.wait_for_timeout(500)
        page.fill("#asr-source", tmp_mp4)
        page.dispatch_event("#asr-source", "input")
        page.wait_for_timeout(900)
        page.click("#asr-start-btn")

        # 等识别跑起来（有轮次了）
        deadline = time.time() + 60
        while time.time() < deadline:
            if _probe()["rounds"] > 5:
                break
            page.wait_for_timeout(300)
        before = _probe()
        print(f"  清空前：{before}")
        check("U22 清空前识别在跑（有轮次）",
              before["rounds"] > 5 and before["busy"], f"{before}")

        page.click("#asr-reset-btn")
        page.wait_for_timeout(600)
        mid = _probe()
        print(f"  清空 +0.6s：{mid}")
        check("U22 点清空后本地轮次立刻清零",
              mid["rounds"] == 0, f"rounds={mid['rounds']}")

        # ⚠️ **关键**：等一会儿，看结果会不会**长回来**
        page.wait_for_timeout(5000)
        after = _probe()
        print(f"  清空 +5.6s：{after}")
        check("U22 清空 5 秒后轮次**没有长回来**（真清干净）",
              after["rounds"] == 0, f"rounds={after['rounds']}")
        check("U22 清空后转写框也空了",
              after["rows"] == 0, f"rows={after['rows']}")
        check("U22 清空后单行结果也空了",
              after["live"] == 0, f"live={after['live']}")

        snap = api_state()
        check("U22 后端状态回到 idle、轮次为 0",
              snap.get("state") == "idle" and (snap.get("round_count") or 0) == 0,
              f"state={snap.get('state')!r} rounds={snap.get('round_count')}")
        check("U22 识别中点清空 → 画面也停住（视频暂停）",
              after["paused"] is True, f"paused={after['paused']}")
        page.screenshot(path=str(shot_dir / "10_u22_reset.png"))
    finally:
        release_npu()
        try:
            os.remove(tmp_mp4)
        except OSError:
            pass


def verify_seek(page, shot_dir: Path) -> None:
    """U21: 拖动进度条 → 视频**继续播**，字幕继续跟着走。

    用户报的 bug：「拖动进度条到目标位置后，视频播放和字幕都暂停了」。

    根因：拖动进度条会触发「重开识别」（``_maybeFollowSeek`` → 先
    ``POST /stop`` 停掉正在跑的那一轮，再从拖动点往前 8 秒重开）——
    这个 stop 是**前端内部**的，不是用户喊停。而 stopped 帧一律冻
    画面 → 视频被暂停 → 字幕也跟着停。

    修法：stop 带 ``reason``（``user`` / ``internal``）⇒ 只有用户喊停
    才冻画面。
    """
    print("\n--- U21 拖动进度条 → 视频继续播 ---")
    long_mp4 = Path("/tmp/asr_ref/asr_long_90s.mp4")
    tmp_mp4 = "/userdata/assets/uploads/_asr_seek_test.mp4"
    have_mp4 = False
    if long_mp4.is_file():
        try:
            shutil.copyfile(str(long_mp4), tmp_mp4)
            have_mp4 = True
        except OSError:
            have_mp4 = False
    if not have_mp4:
        print("  [SKIP] 缺 /tmp/asr_ref/asr_long_90s.mp4（U21 跳过）")
        return

    def _probe():
        return page.evaluate("""() => {
            const A = window.Qwen3Asr || {};
            const v = document.getElementById('asr-video');
            const sub = document.getElementById('asr-video-sub');
            const body = document.getElementById('asr-transcript-body');
            return {
                sub: sub ? (sub.textContent || '').trim() : '',
                paused: v ? !!v.paused : null,
                t: v ? (v.currentTime || 0) : 0,
                raf: A._subRaf || 0,
                rows: body ? body.querySelectorAll('.asr-sub-line').length : 0,
                seekBusy: !!A._seekBusy,
                state: (A.snapshot && A.snapshot.state) || '',
                rounds: (A.snapshot && A.snapshot.round_count) || 0,
            };
        }""")

    try:
        page.select_option("#asr-mode", "single")
        page.click('#asr-source-tabs .asr-source-tab[data-source=video]')
        page.wait_for_timeout(500)
        page.fill("#asr-source", tmp_mp4)
        page.dispatch_event("#asr-source", "input")
        page.wait_for_timeout(900)
        page.click("#asr-start-btn")

        # 等识别真的跑起来 + 字幕出来了
        got = False
        deadline = time.time() + 90
        while time.time() < deadline:
            st = _probe()
            if st["sub"] and st["rows"] > 0 and st["paused"] is False:
                got = True
                break
            page.wait_for_timeout(400)
        before = _probe()
        print(f"  拖动前：字幕={before['sub'][:30]!r} 播放头={before['t']:.2f}s "
              f"暂停={before['paused']} 轮次={before['rounds']}")
        check("U21 拖动前视频在播、字幕在画面上", got,
              f"sub={before['sub'][:30]!r} paused={before['paused']}")

        # 拖到**还没识别过**的位置 → 触发「重开识别」
        page.evaluate("""() => {
            const v = document.getElementById('asr-video');
            v.currentTime = 30;
        }""")
        page.wait_for_timeout(400)
        # 等重开识别走完（内部 stop → 重开）
        deadline = time.time() + 40
        while time.time() < deadline:
            st = _probe()
            if not st["seekBusy"]:
                break
            page.wait_for_timeout(300)
        page.wait_for_timeout(1200)
        after = _probe()
        print(f"  拖动后：字幕={after['sub'][:30]!r} 播放头={after['t']:.2f}s "
              f"暂停={after['paused']} rAF={after['raf']} 状态={after['state']!r}")

        # ① **视频没有被暂停**（这条就是用户报的 bug）
        check("U21 拖动进度条后视频**没有**被暂停",
              after["paused"] is False, f"paused={after['paused']}")
        # ② 播放头继续前进（真的还在播）
        t1 = after["t"]
        page.wait_for_timeout(2000)
        later = _probe()
        check("U21 拖动后播放头继续前进（视频还在播）",
              later["t"] > t1 + 0.2, f"{t1:.2f} → {later['t']:.2f}")
        # ③ 字幕的 rAF 循环还活着
        check("U21 拖动后字幕 rAF 循环仍在跑",
              later["raf"] != 0, f"raf={later['raf']}")
        # ④ 字幕有内容（跟着新位置）
        check("U21 拖动后字幕跟着新位置（不是空的）",
              bool(later["sub"]), f"sub={later['sub'][:40]!r}")
        # ⑤ 识别确实重开了（状态回到 running，轮次在长）
        check("U21 拖动后识别重开了（状态 running）",
              later["state"] == "running", f"state={later['state']!r}")
        check("U21 拖动后轮次继续增长", later["rounds"] > 0,
              f"rounds={later['rounds']}")
        page.screenshot(path=str(shot_dir / "9_u21_seek.png"))
    finally:
        # ⚠️ **必须让出 NPU** —— 这个用例结束时识别还在跑（拖动后重开
        # 的那一轮），后面的 live 用例要自己跑识别 → 会撞 409。
        release_npu()
        try:
            os.remove(tmp_mp4)
        except OSError:
            pass


def verify_stop(page, shot_dir: Path) -> None:
    """U20: 点「停止」→ 画面（含字幕）必须**立刻停住**，状态是「已停止」。

    用户报的 bug：「流式语音识别」里点了「停止」，**字幕还在滚动**。

    根因有两层（都修了）：

    1. **字幕的驱动源是「播放头」**（YouTube 式：字幕跟着画面走，跟识别
       进度无关）—— 原来「停止」只停后端识别，**视频还在播** → 字幕的
       rAF 循环继续跑 → 字幕继续按播放头滚动 → 用户以为识别还在跑。
    2. **后端把「用户取消」当成了「识别失败」** —— 取消会让在飞的那次
       RPC 抛 ``generation stopped``，那条异常走 ``_fail`` → 状态落成
       ``error``（徽标显示「出错」）→ 用户以为识别崩了。
    """
    print("\n--- U20 点「停止」→ 字幕停住 + 状态「已停止」---")
    long_mp4 = Path("/tmp/asr_ref/asr_long_90s.mp4")
    tmp_mp4 = "/userdata/assets/uploads/_asr_stop_test.mp4"
    have_mp4 = False
    if long_mp4.is_file():
        try:
            shutil.copyfile(str(long_mp4), tmp_mp4)
            have_mp4 = True
        except OSError:
            have_mp4 = False
    if not have_mp4:
        print("  [SKIP] 缺 /tmp/asr_ref/asr_long_90s.mp4（U20 跳过）")
        return

    def _probe():
        return page.evaluate("""() => {
            const A = window.Qwen3Asr || {};
            const v = document.getElementById('asr-video');
            const sub = document.getElementById('asr-video-sub');
            const body = document.getElementById('asr-transcript-body');
            const badge = document.getElementById('asr-state-badge');
            const live = document.getElementById('asr-live-committed');
            return {
                sub: sub ? (sub.textContent || '').trim() : '',
                paused: v ? !!v.paused : null,
                t: v ? (v.currentTime || 0) : 0,
                raf: A._subRaf || 0,
                rows: body ? body.querySelectorAll('.asr-sub-line').length : 0,
                badge: badge ? badge.textContent.trim() : '',
                live: live ? (live.textContent || '').trim() : '',
                hint: (document.getElementById('asr-hint') || {}).textContent || '',
            };
        }""")

    try:
        page.select_option("#asr-mode", "single")
        page.click('#asr-source-tabs .asr-source-tab[data-source=video]')
        page.wait_for_timeout(500)
        page.fill("#asr-source", tmp_mp4)
        page.dispatch_event("#asr-source", "input")
        page.wait_for_timeout(900)
        page.click("#asr-start-btn")

        # 等字幕真的出来（视频在播 + 时间轴已有行 + 播放头落在已识别区间）
        got = False
        deadline = time.time() + 90
        while time.time() < deadline:
            st = _probe()
            if st["sub"] and st["rows"] > 0 and st["paused"] is False:
                got = True
                break
            page.wait_for_timeout(400)
        before = _probe()
        print(f"  停止前：字幕={before['sub'][:40]!r} 行数={before['rows']} "
              f"播放头={before['t']:.2f}s 暂停={before['paused']} "
              f"rAF={before['raf']}")
        check("U20 停止前字幕确实在画面上（能复现用户的场景）", got,
              f"sub={before['sub'][:40]!r} rows={before['rows']}")
        check("U20 停止前字幕 rAF 循环在跑", before["raf"] != 0,
              f"raf={before['raf']}")

        page.click("#asr-stop-btn")
        # 等后端把状态落成 stopped（「停止」的语义）
        try:
            page.wait_for_function(
                """() => {
                    const b = document.getElementById('asr-state-badge');
                    return b && b.getAttribute('data-state') === 'stopped';
                }""", timeout=30000)
        except Exception:
            pass
        page.wait_for_timeout(1200)
        after = _probe()
        print(f"  停止后：字幕={after['sub'][:40]!r} 播放头={after['t']:.2f}s "
              f"暂停={after['paused']} rAF={after['raf']} 徽标={after['badge']!r}")

        # ① 后端状态：必须是「已停止」，不是「出错」也不是「完成」
        snap = api_state()
        check("U20 后端状态是 stopped（不是 error / done）",
              snap.get("state") == "stopped",
              f"state={snap.get('state')!r} error={snap.get('error')!r}")
        check("U20 停止后没有把 RPC 的异常当成识别失败",
              not (snap.get("error") or ""), f"error={snap.get('error')!r}")
        check("U20 徽标显示「已停止」", after["badge"] == "已停止",
              f"badge={after['badge']!r}")

        # ② 画面停住：视频暂停 + 字幕 rAF 循环停
        check("U20 停止后视频已暂停", after["paused"] is True,
              f"paused={after['paused']}")
        check("U20 停止后字幕 rAF 循环已停", after["raf"] == 0,
              f"raf={after['raf']}")

        # ③ **字幕不再滚动** —— 这是用户报的 bug 本身。
        #    判据：字幕文本 + 播放头在 2 秒内都不变。
        t1 = after["t"]
        sub1 = after["sub"]
        page.wait_for_timeout(2000)
        later = _probe()
        check("U20 停止后字幕内容不再变化（不滚了）",
              later["sub"] == sub1, f"{sub1[:30]!r} → {later['sub'][:30]!r}")
        check("U20 停止后播放头不再前进",
              abs(later["t"] - t1) < 0.05, f"{t1:.3f} → {later['t']:.3f}")

        # ④ 已识别的内容**保留**（点停止不是白识别）
        check("U20 停止后已识别的内容保留（转写框有行）",
              after["rows"] > 0, f"rows={after['rows']}")
        check("U20 停止后单行结果保留",
              bool(after["live"]) or bool((snap.get("last_result") or {}).get("text")),
              f"live={after['live'][:40]!r}")
        check("U20 提示语说明是「已停止」", "已停止" in after["hint"],
              f"hint={after['hint'][:60]!r}")
        page.screenshot(path=str(shot_dir / "8_u20_stopped.png"))
    finally:
        release_npu()
        try:
            os.remove(tmp_mp4)
        except OSError:
            pass


def verify_restore(page, shot_dir: Path) -> None:
    """U19: 刷新页面后恢复 + 不同来源的历史不串线。

    用户反馈：「刷新页面后转写框内容丢失」。

    根因：轮次只存在**服务端的内存**里（``service.py::_rounds``），浏览器
    一刷新 ``this.rounds`` 就空了 → 视频来源的「字幕同款转写框」被清空。

    这里钉死两件事：

    1. **刷新后还在** —— 轮次 / 转写框 / 来源页签 / 设备端路径都要恢复，
       而且**归属由服务端说了算**（``GET /rounds`` 一起返回 ``source``）：
       前端自己猜会把「音频文件」的轮次画到视频画面上。
    2. **不同来源的历史不串线** —— 音频文件的那句话不能出现在视频页签下，
       视频的轮次也不能被画到麦克风页签下。
    """
    print("\n--- U19 刷新恢复 + 来源不串线 ---")

    mp4_src = Path("/tmp/asr_ref/asr_real_audio.mp4")
    tmp_mp4 = "/userdata/assets/uploads/_asr_restore_test.mp4"
    have_mp4 = False
    if mp4_src.is_file():
        try:
            shutil.copyfile(str(mp4_src), tmp_mp4)
            have_mp4 = True
        except OSError:
            have_mp4 = False

    def _mark_wizard_done():
        """把向导标成「已完成」→ 刷新后**不会**重挂向导（卡片页直接可见）。

        ⚠️ 不能像 ``open_wizard`` 那样把痕迹**清空** —— 清空了等于「没走过
        向导」，刷新后框架的 ``tryMount`` 会把向导重新挂上，向导会把卡片页
        藏起来 → 后面点页签都点不到（元素 not visible）。

        键名 = ``wdnwebui.wizard.<appId>.completed``（``state.js::_lsKey``，
        没有 ``localStorage_key`` 时就是这个前缀）。
        """
        page.evaluate("""(id) => {
            const base = 'wdnwebui.wizard.' + id;
            localStorage.setItem(base + '.completed', '1');
            for (const k of Object.keys(localStorage))
                if (k.startsWith(base) && k.endsWith('.completed'))
                    localStorage.setItem(k, '1');
        }""", APP)

    def _reload_and_wait(n_rounds: int):
        """刷新页面 → 等恢复完成（轮次数够 + 归属落定）。"""
        _mark_wizard_done()
        page.reload(wait_until="domcontentloaded", timeout=30000)
        page.wait_for_function("() => !!window.Qwen3Asr", timeout=20000)
        try:
            page.wait_for_function(
                """(n) => {
                    const A = window.Qwen3Asr;
                    return !!A && (A.rounds || []).length >= n && !!A._roundsSource;
                }""", arg=n_rounds, timeout=25000)
        except Exception:
            pass
        page.wait_for_timeout(600)

    def _state():
        return page.evaluate("""() => {
            const A = window.Qwen3Asr || {};
            const c = document.getElementById('asr-live-committed');
            const box = document.getElementById('asr-transcript');
            const body = document.getElementById('asr-transcript-body');
            const rt = document.getElementById('asr-rt-committed');
            const tab = document.querySelector(
                '#asr-source-tabs .asr-source-tab.is-active');
            // 「看得见吗」要一路往上算 —— 父元素 display:none 时，
            // 子元素的 computed display 仍然是它自己的值。
            const vis = (e) => {
                if (!e) return null;
                let n = e;
                while (n && n !== document.body) {
                    if (getComputedStyle(n).display === 'none') return false;
                    n = n.parentElement;
                }
                return true;
            };
            return {
                n: (A.rounds || []).length,
                src: A._roundsSource || '',
                source: A._source || '',
                text: c ? (c.textContent || '').trim() : '',
                singleShown: vis(c),
                trHidden: box ? box.classList.contains('hidden') : null,
                trRows: body ? body.querySelectorAll('.asr-sub-line').length : 0,
                trText: body ? (body.textContent || '').trim() : '',
                rtText: rt ? (rt.textContent || '').trim() : '',
                tab: tab ? tab.dataset.source : '',
                path: (document.getElementById('asr-source') || {}).value || '',
            };
        }""")

    try:
        # ---------------- 1) 音频文件批次 → 刷新 → 还在 ----------------
        page.select_option("#asr-mode", "single")
        page.click('#asr-source-tabs .asr-source-tab[data-source=file]')
        page.wait_for_timeout(400)
        page.fill("#asr-source", WAV)
        page.dispatch_event("#asr-source", "input")
        page.wait_for_timeout(600)
        page.click("#asr-start-btn")
        try:
            page.wait_for_function(
                """() => {
                    const b = document.getElementById('asr-state-badge');
                    return b && b.getAttribute('data-state') === 'done';
                }""", timeout=180000)
        except Exception:
            pass
        page.wait_for_timeout(1200)
        a_before = _state()
        print(f"  刷新前：轮次={a_before['n']} 文本={a_before['text'][:50]!r} "
              f"页签={a_before['tab']!r}")
        check("U19 音频文件识别出了轮次和文本",
              a_before["n"] > 0 and bool(a_before["text"]),
              f"n={a_before['n']} text={a_before['text'][:50]!r}")

        _reload_and_wait(a_before["n"])
        a_after = _state()
        print(f"  刷新后：轮次={a_after['n']} 文本={a_after['text'][:50]!r} "
              f"页签={a_after['tab']!r} 路径={a_after['path']!r}")
        check("U19 刷新后轮次恢复（数量与刷新前一致）",
              a_after["n"] == a_before["n"] and a_after["n"] > 0,
              f"{a_before['n']} → {a_after['n']}")
        check("U19 刷新后文本恢复（与刷新前逐字一致）",
              a_after["text"] == a_before["text"] and bool(a_after["text"]),
              f"{a_before['text'][:50]!r} → {a_after['text'][:50]!r}")
        check("U19 刷新后来源页签仍是「音频文件」",
              a_after["tab"] == "file", f"tab={a_after['tab']!r}")
        check("U19 刷新后设备端路径填回输入框",
              a_after["path"] == WAV, f"path={a_after['path']!r}")
        check("U19 刷新后批次归属是 file（服务端说了算）",
              a_after["src"] == "file", f"src={a_after['src']!r}")

        # ---------------- 2) 切到视频页签 → 音频文件的话不串线 ----------------
        #
        # ⚠️ **单行文字框不藏**（U18 的契约：切回文件页签要还能看到结果）
        # —— 真正要挡住的是「画到画面上」的那两处：视频字幕 + 转写框。
        page.click('#asr-source-tabs .asr-source-tab[data-source=video]')
        page.wait_for_timeout(900)
        v_state = _state()
        check("U19 切到视频页签后，音频文件的轮次没被画进转写框（不串线）",
              v_state["trHidden"] is True and v_state["trRows"] == 0,
              f"hidden={v_state['trHidden']} rows={v_state['trRows']}")
        sub_after = page.evaluate(
            """() => (document.getElementById('asr-video-sub')
                    .textContent || '').trim()""")
        check("U19 切到视频页签后画面字幕是空的（没画上一来源的轮次）",
              sub_after == "", f"字幕={sub_after[:60]!r}")
        check("U19 切到视频页签后音频文件的文本还在（没被删，切回去能看）",
              v_state["text"] == a_before["text"],
              f"{v_state['text'][:50]!r}")

        # ---------------- 3) 视频批次 → 刷新 → 转写框还在 ----------------
        if not have_mp4:
            print("  [SKIP] 缺 /tmp/asr_ref/asr_real_audio.mp4"
                  "（U19 视频段跳过）")
            return
        page.fill("#asr-source", tmp_mp4)
        page.dispatch_event("#asr-source", "input")
        page.wait_for_timeout(800)
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
        v_before = _state()
        print(f"  视频刷新前：轮次={v_before['n']} 转写行={v_before['trRows']}")
        check("U19 视频识别后转写框有内容",
              v_before["trRows"] > 0 and v_before["trHidden"] is False,
              f"rows={v_before['trRows']} hidden={v_before['trHidden']}")

        _reload_and_wait(v_before["n"])
        v_after = _state()
        print(f"  视频刷新后：轮次={v_after['n']} 转写行={v_after['trRows']} "
              f"页签={v_after['tab']!r} 路径={v_after['path']!r}")
        check("U19 刷新后转写框的行数与刷新前一致",
              v_after["trRows"] == v_before["trRows"] and v_after["trRows"] > 0,
              f"{v_before['trRows']} → {v_after['trRows']}")
        check("U19 刷新后转写框的文本与刷新前一致",
              v_after["trText"] == v_before["trText"] and bool(v_after["trText"]),
              f"{v_before['trText'][:40]!r} → {v_after['trText'][:40]!r}")
        check("U19 刷新后来源页签回到「含音轨的视频」",
              v_after["tab"] == "video", f"tab={v_after['tab']!r}")
        check("U19 刷新后批次归属是 video（不是上一次的 file）",
              v_after["src"] == "video", f"src={v_after['src']!r}")
        check("U19 刷新后设备端路径填回输入框（播放器能重载）",
              v_after["path"] == tmp_mp4, f"path={v_after['path']!r}")

        # ---------------- 4) 麦克风页签 → 文件的结果不串线 ----------------
        page.click('#asr-source-tabs .asr-source-tab[data-source=browser]')
        page.wait_for_timeout(900)
        m_state = _state()
        check("U19 麦克风页签下，文件那批的单行文本被藏起来",
              m_state["singleShown"] is False,
              f"shown={m_state['singleShown']}")
        check("U19 麦克风区没有文件的历史文本",
              m_state["rtText"] == "", f"rt={m_state['rtText'][:50]!r}")

        # ---------------- 5) API 层：归属跟着轮次一起下发 ----------------
        st, data = api("GET", "/api/qwen3-asr/rounds")
        src = (data or {}).get("source") or {}
        check("U19 /rounds 一起返回轮次的归属（kind=video）",
              src.get("kind") == "video", f"source={src}")
        check("U19 /rounds 的归属带上了设备端路径（播放器能重载）",
              src.get("path") == tmp_mp4, f"source={src}")
        check("U19 新批次替换了旧批次（音频文件的轮次没留下）",
              (data or {}).get("rounds") is not None
              and len(data["rounds"]) == v_after["n"],
              f"server={len((data or {}).get('rounds') or [])} "
              f"page={v_after['n']}")
        page.screenshot(path=str(shot_dir / "7_u19_mic.png"))
    finally:
        try:
            os.remove(tmp_mp4)
        except OSError:
            pass


def verify_wizard_preview(page, shot_dir: Path) -> None:
    """U23：向导「音频源」步 —— 硬件能力三行 ✓ + 预览播放。

    用户诉求（2026-11-10）：

    1. 环境检查步的**硬件能力**要有 ``✓ hwcap_video_input`` /
       ``✓ hwcap_audio_input`` / ``✓ hwcap_audio_output`` 三个；
    2. 「音频源」步要有**视频源步同款**的逻辑（浏览 / 预览 / 关闭 /
       拖放上传区），但**不要** gscore 那部分 UI 与逻辑；
    3. 音频文件**或**含音轨的视频，都能在**向导界面**里预览播放。

    ⚠️ 预览素材必须落在**媒体端点放行的目录**里（``<assets>/uploads`` /
    ``<assets>/tts``）—— ``/api/qwen3-asr/media`` 只放行这两个根，别的
    路径一律拒（那个端点没有鉴权）。所以这里把本地的参考素材**复制**
    一份进去用完再删。
    """
    print("\n--- U23 向导「音频源」步：硬件能力 + 预览播放 ---")
    uploads = Path("/userdata/assets/uploads")
    wav_in = uploads / "u23_preview.wav"
    mp4_in = uploads / "u23_preview.mp4"
    try:
        uploads.mkdir(parents=True, exist_ok=True)
        shutil.copy(WAV, wav_in)
        shutil.copy(MP4, mp4_in)
    except OSError as e:
        print(f"  （跳过 U23：素材准备失败 {e}）")
        return

    try:
        open_wizard(page)

        # ---------------- 1) 环境检查步：硬件能力 ----------------
        hw = page.eval_on_selector_all(
            f"#{NS}root-{APP} .{NS}env-row",
            """els => els.map(e => ({
                text: e.textContent.trim(),
                ok: e.classList.contains('env-ok'),
            }))""")
        print("  env 行: " + " | ".join(r["text"] for r in hw))
        for cap in ("hwcap_video_input", "hwcap_audio_input", "hwcap_audio_output"):
            row = next((r for r in hw if cap in r["text"]), None)
            check(f"U23 硬件能力列出 {cap}", row is not None,
                  f"rows={[r['text'] for r in hw]}")
            check(f"U23 {cap} 带 ✓", bool(row) and row["text"].startswith("✓"),
                  f"text={row['text'] if row else None}")

        # ---------------- 2) 音频源步：源字段那套控件 ----------------
        click_next(page)
        check("U23 步骤标题是「音频源」", step_title(page) == "音频源",
              step_title(page))
        src_btns = page.eval_on_selector_all(
            f"#{NS}root-{APP} .{NS}source-action-btn",
            "els => els.map(e => e.textContent.trim())")
        labels = " ".join(field_labels(page))
        print("  源按钮: " + ", ".join(src_btns))
        check("U23 有「预览播放」按钮", any("预览" in b for b in src_btns),
              f"src_btns={src_btns}")
        check("U23 有「关闭」按钮", any("关闭" in b for b in src_btns),
              f"src_btns={src_btns}")
        check("U23 有拖放上传区",
              page.locator(f"#{NS}root-{APP} .{NS}drop-zone").count() > 0)
        # gscore 那部分**不要**（判帧算法 / 阈值 / 帧间隔）
        check("U23 没有 gscore / 判帧 / 阈值 / 帧间隔字段",
              not any(k in labels for k in ("gscore", "判帧", "阈值", "帧间隔")),
              f"labels={labels}")
        page.screenshot(path=str(shot_dir / "u23_source.png"))

        # ---------------- 3) 预览播放 ----------------
        def _preview(path: str, tag: str, want_tag: str) -> None:
            set_field(page, "asr-source", path)
            page.click(f"#{NS}root-{APP} #{NS}source-open-asr-source")
            page.wait_for_timeout(2200)
            info = page.evaluate(f"""() => {{
                const root = document.getElementById('{NS}root-{APP}');
                const wrap = document.getElementById('asr-preview-wrap');
                const v = document.getElementById('asr-preview-video');
                const a = document.getElementById('asr-preview-audio');
                let el = null;
                if (v && !v.classList.contains('hidden')) el = v;
                else if (a && !a.classList.contains('hidden')) el = a;
                // 居中判定：播放器的几何中心 vs 所在容器（.asr-preview-wrap）
                const rect = (n) => n ? (() => {{
                    const b = n.getBoundingClientRect();
                    return {{l: b.left, r: b.right, w: b.width,
                             cx: (b.left + b.right) / 2}};
                }})() : null;
                const elRect = rect(el);
                const wrapRect = rect(wrap);
                return {{
                    inWizard: !!(wrap && root.contains(wrap)),
                    tag: el ? el.tagName : null,
                    src: el ? (el.getAttribute('src') || '') : '',
                    t: el ? el.currentTime : null,
                    paused: el ? el.paused : null,
                    ready: el ? el.readyState : null,
                    err: el && el.error ? el.error.code : null,
                    elRect: elRect,
                    wrapRect: wrapRect,
                    centerDev: (elRect && wrapRect)
                        ? Math.abs(elRect.cx - wrapRect.cx) : null,
                }};
            }}""")
            print(f"  [{tag}] {info}")
            check(f"U23 {tag} 预览器被搬进向导面板", info["inWizard"],
                  f"info={info}")
            check(f"U23 {tag} 用 {want_tag} 元素播", info["tag"] == want_tag,
                  f"info={info}")
            check(f"U23 {tag} 媒体走 /media 端点",
                  "/api/qwen3-asr/media?path=" in (info["src"] or ""),
                  f"src={info['src']}")
            check(f"U23 {tag} 真的在播（播放头在走、没暂停）",
                  bool(info["t"]) and info["t"] > 0 and info["paused"] is False,
                  f"info={info}")
            check(f"U23 {tag} 媒体没有解码错误", info["err"] is None,
                  f"err={info['err']}")
            # 居中（用户要求：之前是左对齐）。
            # 播放器 ``max-width:720px`` 到顶后，块级元素默认贴左边 ⇒
            # 必须 ``margin: 0 auto``。判据：几何中心偏差 ≤ 2px。
            check(f"U23 {tag} 预览播放器在向导画布里**居中**",
                  info["centerDev"] is not None
                  and info["centerDev"] <= 2.0,
                  f"dev={info['centerDev']} el={info['elRect']} "
                  f"wrap={info['wrapRect']}")
            page.screenshot(path=str(shot_dir / f"u23_preview_{tag}.png"))

        _preview(str(wav_in), "音频", "AUDIO")
        _preview(str(mp4_in), "视频", "VIDEO")

        # ---------------- 3b) 换源预览 ⇒ 上一个必须**停掉** ----------------
        # 症状（用户实测）：先预览视频、再预览音频 ⇒ **上一次的视频还在
        # 放声音**，两个叠着放。
        # 根因：``display:none`` **不会**让浏览器暂停媒体（播放与可见性
        # 无关）⇒ 只藏不停，播放头照走。
        print("--- 换源预览：上一个必须停 ---")
        set_field(page, "asr-source", str(mp4_in))
        page.click(f"#{NS}root-{APP} #{NS}source-open-asr-source")
        page.wait_for_timeout(1500)
        set_field(page, "asr-source", str(wav_in))
        page.click(f"#{NS}root-{APP} #{NS}source-open-asr-source")
        page.wait_for_timeout(1800)
        sw = page.evaluate("""() => {
            const v = document.getElementById('asr-preview-video');
            const a = document.getElementById('asr-preview-audio');
            return {
                vPaused: v ? v.paused : null,
                vT: v ? v.currentTime : null,
                vSrc: v ? (v.getAttribute('src') || '') : null,
                aPaused: a ? a.paused : null,
                aT: a ? a.currentTime : null,
            };
        }""")
        print("  换源后:", sw)
        check("U23 换源后上一个播放器停住了（不叠音）",
              sw["vPaused"] is True, f"sw={sw}")
        check("U23 换源后上一个播放器的源被清掉",
              not (sw["vSrc"] or ""), f"sw={sw}")
        check("U23 换源后播放头归零", (sw["vT"] or 0) == 0, f"sw={sw}")
        check("U23 换源后新播放器在播",
              sw["aPaused"] is False and (sw["aT"] or 0) > 0, f"sw={sw}")
        page.screenshot(path=str(shot_dir / "u23_switch.png"))

        # ---------------- 4) 「关闭」→ 停播 + 搬回卡片页 ----------------
        page.click(f"#{NS}root-{APP} .{NS}source-action-btn:not(.{NS}source-open-btn)")
        page.wait_for_timeout(800)
        closed = page.evaluate(f"""() => {{
            const root = document.getElementById('{NS}root-{APP}');
            const wrap = document.getElementById('asr-preview-wrap');
            const v = document.getElementById('asr-preview-video');
            const a = document.getElementById('asr-preview-audio');
            return {{
                inWizard: !!(wrap && root.contains(wrap)),
                videoPaused: v ? v.paused : null,
                audioPaused: a ? a.paused : null,
                audioSrc: a ? (a.getAttribute('src') || '') : null,
            }};
        }}""")
        print("  关闭后:", closed)
        check("U23 关闭后预览器被搬回卡片页", not closed["inWizard"],
              f"closed={closed}")
        check("U23 关闭后预览停住了（不会在后台继续放声音）",
              closed["videoPaused"] is not False and closed["audioPaused"] is not False,
              f"closed={closed}")
        check("U23 关闭后预览源被清掉", not (closed["audioSrc"] or ""),
              f"closed={closed}")
    finally:
        for pth in (wav_in, mp4_in):
            try:
                os.remove(pth)
            except OSError:
                pass


def verify_wizard_video_source(page, shot_dir: Path) -> None:
    """U24：向导里选**含音轨的视频** → 完成配置 → 功能页必须切成视频来源。

    用户实测 bug（2026-11-10）：

    * 向导「音频源」步选了一个 .mp4（带音轨）；
    * 点「完成配置」进功能页 ⇒ **页签还停在「音频文件」**，视频**也不播**。

    根因：向导里**没有**「输入来源」页签（那是功能页的东西）⇒
    ``applyWizardSnapshot`` 回填路径后从没设置过 ``_source`` ⇒
    ``_syncVideoPlayer()`` / ``_playVideoIfAny()`` 都以
    ``_source === "video"`` 为前提 ⇒ 播放器不出现、视频不播；
    同时后端 ``_source_kind`` 按后缀判成 "video" ⇒ **前后端口径不一致**。

    修法：``applyWizardSnapshot`` 按路径后缀推断来源类型并切页签
    （``_applyKindFromPath``），与后端 ``_source_kind`` 同口径。
    """
    print("\n--- U24 向导选视频 → 完成配置 → 功能页是视频来源 ---")
    uploads = Path("/userdata/assets/uploads")
    mp4_in = uploads / "u24_source.mp4"
    try:
        uploads.mkdir(parents=True, exist_ok=True)
        shutil.copy(MP4, mp4_in)
    except OSError as e:
        print(f"  （跳过 U24：素材准备失败 {e}）")
        return

    try:
        open_wizard(page)
        click_next(page)               # env → source
        print("  步骤:", step_title(page))
        set_field(page, "asr-source", str(mp4_in))
        click_next(page)               # source → finish
        page.wait_for_selector(f"#{NS}root-{APP} #{NS}finish-btn", timeout=10000)
        print("  步骤:", step_title(page))

        page.click(f"#{NS}root-{APP} #{NS}finish-btn")
        # 等这一次识别真的跑完（后端一次只能跑一个识别）。
        wait_run_done(0, timeout=180)
        page.wait_for_timeout(2500)

        st = page.evaluate("""() => {
            const tab = document.querySelector(
                '#asr-source-tabs .asr-source-tab.is-active');
            const wrap = document.getElementById('asr-video-wrap');
            const vid = document.getElementById('asr-video');
            const b = vid ? vid.getBoundingClientRect() : null;
            const A = window.Qwen3Asr || {};
            return {
                tab: tab ? tab.dataset.source : null,
                wrapHidden: wrap ? wrap.classList.contains('hidden') : null,
                src: vid ? (vid.getAttribute('src') || '') : null,
                paused: vid ? vid.paused : null,
                t: vid ? vid.currentTime : null,
                err: vid && vid.error ? vid.error.code : null,
                w: b ? b.width : null,
                h: b ? b.height : null,
                source: A._source || null,
                roundsSource: A._roundsSource || null,
            };
        }""")
        print("  完成配置后:", st)
        page.screenshot(path=str(shot_dir / "u24_after_finish.png"))

        check("U24 来源页签是「含音轨的视频」", st["tab"] == "video",
              f"tab={st['tab']!r}")
        check("U24 卡片内部来源状态是 video", st["source"] == "video",
              f"source={st['source']!r}")
        check("U24 批次归属是 video（与后端同口径）",
              st["roundsSource"] == "video",
              f"roundsSource={st['roundsSource']!r}")
        check("U24 视频播放器容器可见（没被藏）",
              st["wrapHidden"] is False, f"hidden={st['wrapHidden']}")
        check("U24 播放器 src 指向 /media 端点",
              "/api/qwen3-asr/media?path=" in (st["src"] or ""),
              f"src={st['src']}")
        check("U24 视频真的在播（播放头在走、没暂停）",
              st["paused"] is False and (st["t"] or 0) > 0,
              f"paused={st['paused']} t={st['t']}")
        check("U24 视频没有解码错误", st["err"] is None, f"err={st['err']}")
        check("U24 视频有实际尺寸",
              (st["w"] or 0) > 0 and (st["h"] or 0) > 0,
              f"w={st['w']} h={st['h']}")
    finally:
        release_npu()
        try:
            os.remove(mp4_in)
        except OSError:
            pass


def verify_leave_stops(page, shot_dir: Path) -> None:
    """U25：识别在跑时**离开这张卡** ⇒ 识别必须停（否则 NPU 一直被占）。

    用户诉求（2026-11-10）：

    「流式语音识别」从功能界面正在识别的时候，切到向导模式，或者返回首页，
    或者切到其它界面，识别应该停止才合理，要不推理一直会占用。

    三个场景（都从**功能界面**出发）：

    ======  ======================================================
    U25-A   点「向导模式」⇒ 识别停
    U25-B   点「返回首页」⇒ 识别停
    U25-C   切到另一张卡（hash 变化）⇒ 识别停
    ======  ======================================================

    ⚠️ **A 之前是坏的**：「向导模式」按钮只调 ``enterWizardMode()``，
    没有任何停止逻辑 ⇒ 识别继续跑 ⇒ ① NPU 一直被占；② 向导走完要
    ``applyWizardSnapshot`` → **发起新的识别**，而后端「一次只能跑一个
    识别」⇒ 新请求直接 409「上一条识别还在进行中」。

    B / C 走框架的 DemoGuard（hashchange → ``stopSync`` → sendBeacon；
    「返回首页」→ ``stopAndClear``），本来就工作 —— 一并守住，防止回归。
    """
    print("\n--- U25 识别在跑时离开这张卡 ⇒ 必须停 ---")
    uploads = Path("/userdata/assets/uploads")
    mp4_in = uploads / "u25_leave.mp4"
    long_mp4 = Path("/tmp/asr_ref/asr_long_90s.mp4")
    try:
        uploads.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(long_mp4), mp4_in)
    except OSError as e:
        print(f"  （跳过 U25：素材准备失败 {e}）")
        return

    try:
        # 把向导标成「已完成」+ 卸载 ⇒ 卡片功能页直接可见（能点按钮）
        page.evaluate("""(id) => {
            localStorage.setItem('wdnwebui.wizard.' + id + '.completed', '1');
            if (window.WizardState) window.WizardState.unmount();
        }""", APP)
        page.reload(wait_until="domcontentloaded", timeout=30000)
        page.wait_for_function("() => !!window.Qwen3Asr", timeout=20000)
        goto_card(page, APP)
        page.wait_for_timeout(1200)

        stops: list = []
        page.on("request", lambda r: stops.append(r.url)
                if "/qwen3-asr/stop" in r.url else None)

        def _start() -> bool:
            """点「开始识别」并等它真的跑起来（后端 busy=true）。"""
            stops.clear()
            page.fill("#asr-source", str(mp4_in))
            page.dispatch_event("#asr-source", "input")
            page.wait_for_timeout(600)
            page.click("#asr-start-btn")
            deadline = time.time() + 30
            while time.time() < deadline:
                if (api_state().get("busy")):
                    return True
                time.sleep(0.4)
            return False

        def _stopped() -> tuple:
            """动作之后等 3.5 s，看后端还跑不跑。"""
            time.sleep(3.5)
            s = api_state()
            return bool(s.get("busy")), (s.get("state") or "")

        def _back_to_card() -> None:
            """回卡片功能页（向导若挂着要先卸）。"""
            page.evaluate("""(id) => {
                localStorage.setItem('wdnwebui.wizard.' + id + '.completed', '1');
                if (window.WizardState) window.WizardState.unmount();
            }""", APP)
            page.wait_for_timeout(500)
            goto_card(page, APP)
            page.wait_for_timeout(900)

        # ---------------- A) 点「向导模式」 ----------------
        started = _start()
        print(f"  A 已启动: {started}")
        page.click("[data-wizard-mode][data-page-id='%s']" % APP)
        page.wait_for_timeout(1200)
        busy, state = _stopped()
        print(f"  A 后端 busy={busy} state={state!r} stop={stops}")
        check("U25-A 点「向导模式」⇒ 识别停了（释放算力）",
              started and not busy, f"busy={busy} state={state!r}")
        check("U25-A 页面确实发了停止请求",
              any("/qwen3-asr/stop" in u for u in stops), f"stops={stops}")
        page.screenshot(path=str(shot_dir / "u25_a_wizard_mode.png"))

        # ---------------- B) 点「返回首页」 ----------------
        _back_to_card()
        started = _start()
        print(f"  B 已启动: {started}")
        page.click("#page-%s .back-home-btn" % APP)
        page.wait_for_timeout(1200)
        busy, state = _stopped()
        print(f"  B 后端 busy={busy} state={state!r} stop={stops}")
        check("U25-B 点「返回首页」⇒ 识别停了（释放算力）",
              started and not busy, f"busy={busy} state={state!r}")

        # ---------------- C) 切到另一张卡 ----------------
        _back_to_card()
        started = _start()
        print(f"  C 已启动: {started}")
        page.evaluate("() => { window.location.hash = '#text-chat'; }")
        page.wait_for_timeout(1200)
        busy, state = _stopped()
        print(f"  C 后端 busy={busy} state={state!r} stop={stops}")
        check("U25-C 切到其它卡 ⇒ 识别停了（释放算力）",
              started and not busy, f"busy={busy} state={state!r}")
    finally:
        release_npu()
        try:
            os.remove(mp4_in)
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
        # ⚠️ 同上：拦住 active-app 写操作，别改生产 autostart 配置
        install_active_app_guard(page)
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
        # W1b：**硬件能力**三行（用户点名要的）—— manifest 声明的
        # hwcap_* 在 env 步逐行列出，各带一个 ✓。
        hw_rows = page.eval_on_selector_all(
            f"#{NS}root-{APP} .{NS}env-row",
            """els => els.map(e => ({
                text: e.textContent.trim(),
                ok: e.classList.contains('env-ok') ||
                    e.classList.contains('wd-wizard-env-ok'),
            }))""")
        for cap in ("hwcap_video_input", "hwcap_audio_input", "hwcap_audio_output"):
            row = next((r for r in hw_rows if cap in r["text"]), None)
            check(f"W1b 硬件能力列出 {cap}", row is not None,
                  f"rows={[r['text'] for r in hw_rows]}")
            check(f"W1b {cap} 是 ✓（不是 ✗）",
                  bool(row) and row["text"].startswith("✓"),
                  f"text={row['text'] if row else None}")
        page.screenshot(path=str(shot_dir / "1_env.png"))

        # ---------------- 第 2 步：音频源 ----------------
        print("\n=== 第 2 步：音频源 ===")
        click_next(page)
        check("W2 步骤标题是「音频源」", step_title(page) == "音频源",
              step_title(page))
        ids = field_ids(page)
        print("  字段: " + ", ".join(ids))
        check("W2 有音频源字段", "asr-source" in ids, f"ids={ids}")
        check("W2 有显示模式字段", "asr-mode" in ids, f"ids={ids}")
        # 控件：文本框 + 浏览… + （上传走**拖放区**，见下面 W2b）
        has_text = page.locator(
            f'#{NS}root-{APP} [data-field-id="asr-source"]').count() > 0
        btns = page.eval_on_selector_all(
            f"#{NS}root-{APP} .{NS}browse-btn",
            "els => els.map(e => e.textContent.trim())")
        print("  按钮: " + ", ".join(btns))
        check("W2 音频源是文本框（设备端路径）", has_text, f"ids={ids}")
        check("W2 有「浏览…」按钮", any("浏览" in b for b in btns), f"btns={btns}")
        # W2b：**源字段**那一套（浏览 / 预览播放 / 关闭 / 拖放上传区）——
        # 音频源步现在走的是视频卡同款的「源字段」路径。
        src_btns = page.eval_on_selector_all(
            f"#{NS}root-{APP} .{NS}source-action-btn",
            "els => els.map(e => e.textContent.trim())")
        drop_zone = page.locator(
            f"#{NS}root-{APP} .{NS}drop-zone").count() > 0
        drop_hint = page.eval_on_selector(
            f"#{NS}root-{APP} .{NS}drop-hint", "e => e.textContent.trim()")
        print("  源按钮: " + ", ".join(src_btns) + f" | 拖放区={drop_zone}")
        check("W2b 有「预览播放」按钮（源字段路径生效）",
              any("预览" in b for b in src_btns), f"src_btns={src_btns}")
        check("W2b 有「关闭」按钮", any("关闭" in b for b in src_btns),
              f"src_btns={src_btns}")
        check("W2b 有拖放上传区（上传入口从「上传…」按钮换成拖放区）",
              drop_zone, f"drop_zone={drop_zone}")
        check("W2b 拖放区文案说的是音频/视频（不是「本地视频」）",
              "音频" in drop_hint and "视频" in drop_hint, f"hint={drop_hint!r}")
        # 板端**没有 emoji 字体** ⇒ 拖放区图标不能是 emoji（会渲染成豆腐块）
        drop_icon = page.eval_on_selector(
            f"#{NS}root-{APP} .{NS}drop-icon", "e => e.innerHTML.trim()")
        check("W2b 拖放区图标不是 emoji（板端会渲染成豆腐块）",
              "<svg" in drop_icon.lower(), f"icon={drop_icon[:60]!r}")
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
        set_field(page, "asr-source", WAV)
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
            set_field(page, "asr-source", MP4)
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

        # ---------------- 刷新恢复 + 来源不串线 ----------------
        verify_restore(page, shot_dir)

        # ---------------- 点「停止」→ 字幕停住 ----------------
        verify_stop(page, shot_dir)

        # ---------------- 拖动进度条 → 视频继续播 ----------------
        verify_seek(page, shot_dir)

        # ---------------- 「清空」→ 真清干净 ----------------
        verify_reset(page, shot_dir)

        # ---------------- 向导「音频源」步：硬件能力 + 预览播放 ----------------
        verify_wizard_preview(page, shot_dir)

        # ---------------- 向导选视频 → 完成配置 → 功能页是视频来源 ----------------
        verify_wizard_video_source(page, shot_dir)

        # ---------------- 识别在跑时离开这张卡 ⇒ 必须停 ----------------
        verify_leave_stops(page, shot_dir)

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
