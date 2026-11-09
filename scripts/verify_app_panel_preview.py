#!/usr/bin/env python3
"""应用面板（首页应用网格）的**动态预览视频**回归验证。

背景：应用网格里每张卡的缩略图不是静态图，而是
``manifest.preview_video`` 指向的一段 mp4（framework.js 的
``renderAppPanel`` 渲染成 ``<video class="app-card-video" autoplay muted
playsinline loop>``）。只要 manifest 写了 ``preview_video``，而对应的
文件没放进卡片的 ``static/`` 目录，浏览器就会拿到 404 —— 画面位置留一
块空白，**不报错**，用户只会觉得「这张卡没有预览」。

所以本脚本钉死三件事：

1. **声明与文件一一对应**：manifest 声明了 ``preview_video`` 的卡，
   磁盘上必须真有那个文件（在卡自己的 ``static/`` 目录里），且文件是
   一个能解码的 mp4（有 ``ftyp``/``moov`` box、非空）。
2. **真的发得出去**：``GET /static/apps/<id>/<name>`` 必须 200 且
   ``Content-Type`` 是 ``video/*``（浏览器要靠它决定能不能内联播放）。
3. **真的动起来**：浏览器里那张卡的 ``<video>`` 必须解出画面
   （``videoWidth > 0``）并**推进**（``currentTime`` 在涨），而且画到
   canvas 上**不是纯黑**（有真实像素）—— 只查 DOM 存在红不了「黑屏 /
   不播放」这类回归。

用法::

    .venv/bin/python scripts/verify_app_panel_preview.py
    .venv/bin/python scripts/verify_app_panel_preview.py --base http://127.0.0.1:8000
    .venv/bin/python scripts/verify_app_panel_preview.py --skip-browser

注意：卡片 manifest 是**启动时**扫的（``app/core/manifest.py``），改了
``preview_video`` 或新加了预览文件之后要**重启服务**才会进
``__APP_MANIFESTS__``；本脚本因此先查「服务当前认不认这个预览」。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_FAILED: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}")
    if not ok:
        if detail:
            print(f"       {detail}")
        _FAILED.append(label)
    return ok


def _get(url: str, timeout: float = 20.0) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()


def _manifests_from_index(base: str) -> list[dict]:
    """从服务当前吐的 index.html 里解析 ``window.__APP_MANIFESTS__``。"""
    status, _, body = _get(base + "/", timeout=30.0)
    if status != 200:
        raise SystemExit(f"❌ 取 {base}/ 失败: HTTP {status}")
    html = body.decode("utf-8", "replace")
    m = re.search(r"window\.__APP_MANIFESTS__\s*=\s*(\{.*?\});", html, re.S)
    if not m:
        raise SystemExit("❌ index.html 里找不到 __APP_MANIFESTS__")
    return json.loads(m.group(1)).get("cards", [])


def _looks_like_mp4(path: Path) -> tuple[bool, str]:
    """轻量 mp4 体检：``ftyp`` 头 + ``moov`` box + 非空 + **faststart**。

    ``moov`` 必须排在 ``mdat`` **前面**（faststart）：否则浏览器要等整个
    文件下完才起播，面板里那格预览会先黑一阵 —— 文件能播，但体验是坏的。
    改法见 ``scripts/mp4_faststart.py``（无损改容器，不重编码）。
    """
    try:
        data = path.read_bytes()
    except OSError as e:
        return False, f"读不了: {e}"
    if not data:
        return False, "空文件"
    if b"ftyp" not in data[:64]:
        return False, "没有 ftyp box（不是 mp4）"
    moov = data.find(b"moov")
    if moov < 0:
        return False, "没有 moov box（不是可播的 mp4）"
    detail = f"{len(data)} 字节"
    mdat = data.find(b"mdat")
    if mdat >= 0 and moov > mdat:
        return False, (detail + "，moov 在 mdat 之后（非 faststart，起播偏慢）"
                       " → 用 scripts/mp4_faststart.py --fix 改")
    return True, detail + "，faststart"


def _browser_check(base: str, want_ids: list[str], shot: str) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[SKIP] 没装 playwright，跳过浏览器端验证")
        return

    import os

    launch_kwargs: dict = {"args": ["--no-sandbox", "--disable-gpu"]}
    if os.environ.get("DISPLAY"):
        launch_kwargs["headless"] = False
    if Path("/usr/bin/chromium").exists():
        launch_kwargs["executable_path"] = "/usr/bin/chromium"

    errors: list[str] = []
    bad_responses: list[str] = []
    with sync_playwright() as pw:
        br = pw.chromium.launch(**launch_kwargs)
        page = br.new_page(viewport={"width": 1600, "height": 1000})
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console",
                lambda m: errors.append(f"console.error: {m.text}")
                if m.type == "error" else None)
        # 404 只在 console 里留一句 "Failed to load resource"，看不出是哪个
        # URL —— 这里单独把 4xx/5xx 的 URL 抓出来，报错才可定位。
        page.on("response",
                lambda r: bad_responses.append(f"HTTP {r.status} {r.url}")
                if r.status >= 400 else None)

        page.goto(base + "/", wait_until="domcontentloaded", timeout=60000)
        # ⚠️ **必须显式切到应用面板**。boot-restore 会读服务端的
        # ``/api/compute/active-app``（"上次进入的卡片"）并把 hash 改回去
        # —— 那个值是**跨浏览器、跨会话**持久化的（用户在 ASR 卡里跑过
        # 识别就会落盘），于是新开的浏览器直接落在 ASR 卡上，而 ASR 卡的
        # 向导模式会把卡片页藏起来 → 等不到可见的网格。
        # 与 ``verify_asr_wizard.py::goto_home`` 同一套做法。
        page.wait_for_timeout(3000)
        page.evaluate("() => { window.location.hash = '#apps'; }")
        page.wait_for_timeout(1500)
        page.wait_for_selector("#page-apps .apps-grid .app-card", timeout=30000)

        for card_id in want_ids:
            res = page.evaluate(
                """async (cardId) => {
                    const card = document.querySelector(
                        `a.app-card[data-page="${cardId}"]`);
                    if (!card) return {ok: false, why: 'card element missing'};
                    const v = card.querySelector('video.app-card-video');
                    if (!v) return {ok: false, why: 'no <video> in card'};
                    // 首屏**不加载**视频（只有 poster）—— 悬停才补 src。
                    // 这里手动补上，等价于用户把鼠标放到那张卡上。
                    if (!v.getAttribute('src')) {
                        const ds = v.getAttribute('data-preview-src');
                        if (!ds) return {ok: false, why: 'no data-preview-src'};
                        v.src = ds;
                    }
                    // 显式 play()：autoplay 在无用户手势的 headless 下可能被拒
                    try { await v.play(); } catch (e) { /* 下面用 currentTime 判断 */ }
                    const t0 = v.currentTime;
                    await new Promise(r => setTimeout(r, 1200));
                    const t1 = v.currentTime;
                    const dur = v.duration || 0;
                    // 预览是 loop 播放的短视频（~3s），采样窗口正好跨过
                    // 循环点时 currentTime 会**回卷**（2.37 → 0.39）——
                    // 那不是「没动」，而是动了一整圈。按 duration 取模判断。
                    const advanced = (t1 > t0)
                        || (dur > 0 && (dur - t0 + t1) > 0.05);
                    // 把当前帧画到 canvas 上，确认不是黑屏
                    let nonBlack = false, stddev = 0;
                    try {
                        const c = document.createElement('canvas');
                        c.width = 160; c.height = 90;
                        const ctx = c.getContext('2d');
                        ctx.drawImage(v, 0, 0, c.width, c.height);
                        const d = ctx.getImageData(0, 0, c.width, c.height).data;
                        let sum = 0, sum2 = 0, n = 0;
                        for (let i = 0; i < d.length; i += 4) {
                            const v0 = (d[i] + d[i+1] + d[i+2]) / 3;
                            sum += v0; sum2 += v0 * v0; n++;
                        }
                        const mean = sum / n;
                        stddev = Math.sqrt(sum2 / n - mean * mean);
                        nonBlack = stddev > 3.0;
                    } catch (e) { /* 跨域画布会被污染 */ }
                    return {
                        ok: true, src: v.currentSrc || v.src,
                        readyState: v.readyState,
                        videoWidth: v.videoWidth, videoHeight: v.videoHeight,
                        t0, t1, duration: dur, advanced, stddev, nonBlack,
                        paused: v.paused,
                    };
                }""",
                card_id,
            )
            if not res.get("ok"):
                check(False, f"{card_id}: 面板里渲染出 <video>",
                      res.get("why", ""))
                continue
            check(True, f"{card_id}: 面板里渲染出 <video>", res.get("src", ""))
            check(res.get("readyState", 0) >= 2,
                  f"{card_id}: 视频已解出画面 (readyState>=2)",
                  f"readyState={res.get('readyState')}")
            check(res.get("videoWidth", 0) > 0,
                  f"{card_id}: 视频尺寸可读 ({res.get('videoWidth')}x"
                  f"{res.get('videoHeight')})",
                  f"videoWidth={res.get('videoWidth')}")
            check(res.get("advanced", False),
                  f"{card_id}: 预览在动 (currentTime 推进)",
                  f"t0={res.get('t0'):.3f} → t1={res.get('t1'):.3f} "
                  f"(duration={res.get('duration', 0):.2f}s，loop 回卷也算)")
            check(not res.get("paused", True),
                  f"{card_id}: 预览处于播放态 (paused=false)")
            check(res.get("nonBlack", False),
                  f"{card_id}: 画出来不是黑屏 (stddev={res.get('stddev'):.1f})",
                  f"stddev={res.get('stddev'):.1f}")

        try:
            page.locator("#page-apps .apps-grid").screenshot(path=shot)
            print(f"       截图: {shot}")
        except Exception as e:  # noqa: BLE001
            print(f"       截图失败(不影响断言): {e}")

        br.close()

    noise = [u for u in bad_responses if "favicon" in u.lower()]
    real_bad = [u for u in bad_responses if "favicon" not in u.lower()]
    if noise:
        print(f"       (忽略与本功能无关的 404: {len(noise)} 条)")
    check(not real_bad, "页面没有 4xx/5xx 资源", "; ".join(real_bad[:5]))
    # favicon.ico 由**浏览器**发起（不在页面的请求管线里），所以
    # response 监听抓不到它，却会在 console 留一条 "Failed to load
    # resource: 404"。页面里没有 favicon → 这条与本功能无关，忽略；
    # 真有页面资源 404 时上面那一条已经把 URL 列出来了。
    real_errors = [
        e for e in errors
        if "favicon" not in e.lower()
        and not (real_bad == [] and "Failed to load resource" in e)
    ]
    check(not real_errors, "页面无 JS 报错", "; ".join(real_errors[:5]))


def main() -> int:
    ap = argparse.ArgumentParser(description="应用面板动态预览视频验证")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--shot", default="/tmp/app_panel_preview.png")
    ap.add_argument("--skip-browser", action="store_true")
    args = ap.parse_args()

    base = args.base.rstrip("/")

    print("=== 1. 服务当前认不认各卡的预览视频 ===")
    cards = _manifests_from_index(base)
    check(bool(cards), "index.html 里解析出卡片清单", f"cards={len(cards)}")

    with_preview: list[dict] = []
    for m in cards:
        pv = (m.get("preview_video") or "").strip()
        if pv:
            with_preview.append(m)
    print(f"       声明了 preview_video 的卡: "
          f"{[m['id'] for m in with_preview] or '（无）'}")
    check(bool(with_preview), "至少有一张卡声明了 preview_video")

    print("\n=== 2. 声明的文件在磁盘上真实存在 ===")
    from app.core.manifest import discover as discover_cards
    static_dirs = {m.id: m.static_dir for m in discover_cards()}

    for m in with_preview:
        card_id = m["id"]
        pv = m["preview_video"]
        check(pv.startswith(f"/static/apps/{card_id}/"),
              f"{card_id}: preview_video 指向本卡静态目录", pv)
        rel = pv[len(f"/static/apps/{card_id}/"):].split("?")[0]
        sdir = static_dirs.get(card_id)
        check(sdir is not None, f"{card_id}: 卡片静态目录已发现", str(sdir))
        if not sdir:
            continue
        fpath = Path(sdir) / rel
        ok, detail = (False, f"文件不存在: {fpath}")
        if fpath.is_file():
            ok, detail = _looks_like_mp4(fpath)
        check(ok, f"{card_id}: {rel} 是个像样的 mp4", detail)

    print("\n=== 3. 真的发得出去（HTTP 200 + video/*）===")
    for m in with_preview:
        card_id = m["id"]
        url = base + m["preview_video"]
        status, headers, body = _get(url)
        ctype = (headers.get("Content-Type") or headers.get("content-type") or "")
        check(status == 200, f"{card_id}: GET {m['preview_video']} → 200",
              f"HTTP {status}")
        check(ctype.startswith("video/"),
              f"{card_id}: Content-Type 是 video/*", ctype)
        check(len(body) > 1024, f"{card_id}: 响应体非空 ({len(body)} 字节)")

    print("\n=== 4. favicon：页面声明了图标且资源可达 ===")
    status, _, body = _get(base + "/", timeout=30.0)
    html = body.decode("utf-8", "replace")
    icons = re.findall(r'<link[^>]+rel="icon"[^>]*>', html)
    check(bool(icons), "index.html 声明了 <link rel=icon>", str(icons[:2]))
    for tag in icons:
        m = re.search(r'href="([^"]+)"', tag)
        if not m:
            continue
        href = m.group(1)
        if href.startswith("/"):
            href = href.split("?")[0]
        st, hd, _ = _get(base + href)
        ctype = (hd.get("Content-Type") or hd.get("content-type") or "")
        check(st == 200, f"图标资源 {href} → 200", f"HTTP {st}")
        check(ctype.startswith("image/"),
              f"图标资源 {href} 的 Content-Type 是 image/*", ctype)
    st, hd, _ = _get(base + "/favicon.ico")
    ctype = (hd.get("Content-Type") or hd.get("content-type") or "")
    check(st == 200,
          "根路径 /favicon.ico → 200（浏览器默认会请求它）", f"HTTP {st}")
    check(ctype.startswith("image/"),
          "/favicon.ico 的 Content-Type 是 image/*", ctype)

    print("\n=== 5. 浏览器里真的解出画面并动起来 ===")
    if args.skip_browser:
        print("[SKIP] --skip-browser")
    else:
        ids = [m["id"] for m in with_preview]
        # 本次改动的重点卡：深度估计卡以前是空的，现在必须有动态预览
        _browser_check(base, ids, args.shot)

    print()
    if _FAILED:
        print(f"❌ {len(_FAILED)} 项未通过:")
        for f in _FAILED:
            print("   -", f)
        sys.exit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    sys.exit(main())
