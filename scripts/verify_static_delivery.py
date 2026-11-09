#!/usr/bin/env python3
# coding=utf-8
"""回归防护：静态资源交付必须"**压得动 + 缓存得住**"。

背景
----
用户报「网页打不开」。服务端逐项排查后确认**毫秒级响应、页面渲染正常**
（本机无头浏览器 1.1 s 加载完 42 个资源，16 并发请求 <200 ms）；但用户
浏览器加载同一页花了 **76 秒** —— HTML → CSS → core JS → 卡片 JS 四批，
每批之间隔 16~22 s。

把链路上的放大器找出来，一共两个，**都在服务端**：

1. **完全没有压缩**：index.html 116 KB、framework.css 140 KB、
   framework.js 64 KB …… 首屏几百 KB **明文**传输。
2. **静态资源一律 ``no-cache``**：每次刷新都重下 42 个文件（带 ETag 回校验，
   也照样要跑满 42 个往返）。

两个都修掉之后：首屏字节数降到约 1/5，第二次打开只剩 1 个请求（HTML）
+ 若干 API。

本脚本断言（需要服务在跑；**不占 NPU**）：

  A. **gzip 生效**：HTML / CSS / JS 带 ``Accept-Encoding: gzip`` 时返回
     ``Content-Encoding: gzip``，且**解压后与明文响应逐字节相同**
     （压坏了内容比不压更糟）。
  B. **视频不被压坏**：``video/mp4`` 不带 Content-Encoding；
     **206 Partial Content 也不带**（压了就破坏 Range 语义），
     且 ``Content-Range`` 标明的区间与实际字节跟源文件对得上。
  C. **缓存策略分档**：带 ``?v=`` 版本戳的资源 → ``immutable`` 长期缓存；
     不带的 → ``no-cache`` 回校验。
  D. **卡片资源也版本戳化**：HTML 里的卡片 CSS link 与 manifest JSON 里的
     ``frontend.js`` 都带 ``?v=`` —— 这样 framework.js **一行不用改**就能
     吃到 immutable 缓存（改动边界最小）。
  E. **WebSocket 不受影响**：握手仍是 101，且不带 Content-Encoding。

用法::

    python -m scripts.verify_static_delivery
"""
from __future__ import annotations

import base64
import gzip
import http.client
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
# scripts/ 也要在 path 上 —— 浏览器段要复用 verify_asr_wizard._find_chrome
# （那个函数负责处理「venv 里 playwright 版本与 headless_shell revision
# 对不上」的问题，直接用 playwright 自带的 launch 会找不到浏览器）。
sys.path.insert(0, str(REPO / "scripts"))

BASE = os.environ.get("WDNWEBUI_BASE", "http://127.0.0.1:8000").rstrip("/")

_PASS = 0
_FAIL = 0
_FAILURES: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    """记一条断言结果。返回 ok，方便调用方据此分支。"""
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print("  \033[32m✓\033[0m %s%s" % (label, ("  — " + detail) if detail else ""))
    else:
        _FAIL += 1
        _FAILURES.append(label + (("  — " + detail) if detail else ""))
        print("  \033[31m✗\033[0m %s%s" % (label, ("  — " + detail) if detail else ""))
    return ok


def fetch(path: str, headers: dict | None = None, method: str = "GET"):
    """发一个请求，返回 ``(status, headers, body)``。

    **不解压**：``Content-Encoding: gzip`` 的原始字节要自己拿来做断言。
    urllib 默认不发 Accept-Encoding —— 正好当"明文基线"。
    """
    req = urllib.request.Request(BASE + path, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


# --------------------------------------------------------------------------- #
# A. gzip 生效                                                                #
# --------------------------------------------------------------------------- #
def test_gzip_effective() -> None:
    print("\n=== A. gzip 压缩生效 ===")
    targets = [
        ("/", "text/html"),
        ("/static/core/framework.css", "text/css"),
        ("/static/core/framework.js", "text/javascript"),
        ("/static/core/qrcode.js", "text/javascript"),
    ]
    for path, label in targets:
        st_id, h_id, body_id = fetch(path)
        st_gz, h_gz, body_gz = fetch(path, {"Accept-Encoding": "gzip"})
        enc = (h_gz.get("Content-Encoding") or h_gz.get("content-encoding") or "").lower()
        if not check(enc == "gzip", "%s 带 gzip 时返回 Content-Encoding: gzip" % label,
                     "实际 %r" % enc):
            continue
        try:
            plain = gzip.decompress(body_gz)
        except Exception as exc:                       # noqa: BLE001
            check(False, "%s gzip 体能解开" % label, repr(exc))
            continue
        check(plain == body_id, "%s 解压后与明文逐字节相同" % label,
              "明文 %d B / 解压后 %d B" % (len(body_id), len(plain)))
        ratio = len(body_gz) / max(1, len(body_id))
        check(ratio < 0.6, "%s 压缩后明显更小" % label,
              "%d B → %d B（%.0f%%）" % (len(body_id), len(body_gz), ratio * 100))
        vary = (h_gz.get("Vary") or h_gz.get("vary") or "")
        check("accept-encoding" in vary.lower(),
              "%s 带 Vary: Accept-Encoding" % label, repr(vary))


def test_gzip_first_load_bytes() -> None:
    print("\n=== A2. 首屏总字节数 ===")
    # 首屏真正要拉的：HTML + core 的 CSS/JS + 每张卡的 app.css/app.js
    html = fetch("/")[2]
    html_gz = fetch("/", {"Accept-Encoding": "gzip"})[2]
    core_css = fetch("/static/core/framework.css")[2]
    core_js = fetch("/static/core/framework.js")[2]
    print("     HTML   明文 %7d B → gzip %7d B" % (len(html), len(html_gz)))
    print("     core.css %6d B → gzip %6d B" % (len(core_css),
                                                 len(fetch("/static/core/framework.css",
                                                           {"Accept-Encoding": "gzip"})[2])))
    print("     core.js  %6d B → gzip %6d B" % (len(core_js),
                                                 len(fetch("/static/core/framework.js",
                                                           {"Accept-Encoding": "gzip"})[2])))
    total_id = len(html) + len(core_css) + len(core_js)
    total_gz = (len(html_gz)
                + len(fetch("/static/core/framework.css",
                            {"Accept-Encoding": "gzip"})[2])
                + len(fetch("/static/core/framework.js",
                            {"Accept-Encoding": "gzip"})[2]))
    check(total_gz < total_id * 0.5,
          "HTML+core 三个文件压缩后不到原来的一半",
          "%d B → %d B（%.0f%%）" % (total_id, total_gz, total_gz / total_id * 100))


# --------------------------------------------------------------------------- #
# B. 视频 / Range 不能被压坏                                                   #
# --------------------------------------------------------------------------- #
def test_video_not_breaks() -> None:
    print("\n=== B. 视频与 Range 请求不被压坏 ===")
    # 找一张带 preview.mp4 的卡
    video = None
    for m in sorted((REPO / "app" / "apps").glob("*/static/preview.mp4")):
        card_id = m.parent.parent.name.replace("_", "-")
        cand = "/static/apps/%s/preview.mp4" % card_id
        st, _, _ = fetch(cand)
        if st == 200:
            video = (cand, m)
            break
    if video is None:
        check(False, "找到一张带 preview.mp4 的卡")
        return
    url, path = video

    st, h, body = fetch(url, {"Accept-Encoding": "gzip"})
    enc = (h.get("Content-Encoding") or h.get("content-encoding") or "").lower()
    ctype = (h.get("Content-Type") or h.get("content-type") or "")
    check(st == 200, "GET %s → 200" % url, "HTTP %d" % st)
    check(enc == "", "mp4 不带 Content-Encoding", repr(enc))
    check(ctype.startswith("video/"), "mp4 的 Content-Type 是 video/*", repr(ctype))
    check(len(body) == path.stat().st_size,
          "mp4 响应体与磁盘文件同长", "%d vs %d" % (len(body), path.stat().st_size))

    # Range 请求：必须 206、必须不带 Content-Encoding、字节要对得上
    st, h, body = fetch(url, {"Accept-Encoding": "gzip", "Range": "bytes=100-199"})
    enc = (h.get("Content-Encoding") or h.get("content-encoding") or "").lower()
    crange = h.get("Content-Range") or h.get("content-range") or ""
    check(st == 206, "Range 请求 → 206 Partial Content", "HTTP %d" % st)
    check(enc == "", "206 响应不带 Content-Encoding（压了就破坏 Range 语义）", repr(enc))
    check(len(body) == 100, "206 响应体正好 100 字节", "%d 字节" % len(body))
    raw = path.read_bytes()[100:200]
    check(body == raw, "206 响应字节与源文件区间一致")
    check(crange.startswith("bytes 100-199/"), "Content-Range 头正确", repr(crange))


# --------------------------------------------------------------------------- #
# C. 缓存策略分档                                                             #
# --------------------------------------------------------------------------- #
def test_cache_policy() -> None:
    print("\n=== C. 缓存策略：带戳 immutable / 不带戳 no-cache ===")
    st, h, body = fetch("/static/core/framework.js")
    cc = (h.get("Cache-Control") or h.get("cache-control") or "")
    check(st == 200, "GET framework.js → 200", "HTTP %d" % st)
    check("no-cache" in cc, "无版本戳的静态资源 → no-cache（回校验保新鲜度）", repr(cc))

    st, h, body = fetch("/static/core/framework.js?v=123456")
    cc = (h.get("Cache-Control") or h.get("cache-control") or "")
    check(st == 200, "GET framework.js?v=123456 → 200", "HTTP %d" % st)
    check("immutable" in cc and "max-age=31536000" in cc,
          "带 ?v= 的静态资源 → immutable 长期缓存", repr(cc))
    check("no-cache" not in cc, "带 ?v= 时不再 no-cache", repr(cc))


# --------------------------------------------------------------------------- #
# D. 卡片资源版本戳                                                            #
# --------------------------------------------------------------------------- #
def test_card_assets_stamped() -> None:
    print("\n=== D. 卡片 JS/CSS 也带版本戳 ===")
    st, h, html = fetch("/")
    text = html.decode("utf-8", "replace")

    css_links = re.findall(r'<link rel="stylesheet" href="([^"]+)"', text)
    card_css = [u for u in css_links if "/static/apps/" in u]
    check(bool(card_css), "HTML 里有卡片 CSS link", "%d 条" % len(card_css))
    check(all("?v=" in u for u in card_css),
          "每条卡片 CSS link 都带 ?v= 版本戳",
          ", ".join(u for u in card_css if "?v=" not in u) or "全部带戳")

    # manifest JSON 里的 frontend.js
    m = re.search(r"window\.__APP_MANIFESTS__\s*=\s*(\{.*?\});", text, re.S)
    if not m:
        m = re.search(r"__APP_MANIFESTS__\s*=\s*(\{.*?\})\s*;?\s*</script>", text, re.S)
    if not check(bool(m), "从 HTML 里抠出 window.__APP_MANIFESTS__"):
        return
    try:
        data = json.loads(m.group(1))
    except Exception as exc:                            # noqa: BLE001
        check(False, "manifest JSON 可解析", repr(exc))
        return
    cards = data.get("cards") or []
    check(bool(cards), "manifest 里有卡片", "%d 张" % len(cards))
    bad = []
    for c in cards:
        for u in (c.get("frontend", {}).get("js") or []):
            if "?v=" not in u:
                bad.append(u)
    check(not bad, "manifest 里每条 frontend.js 都带 ?v=",
          ", ".join(bad) or "全部带戳")

    # 带戳的卡片 JS 必须真的能取到（且能吃 immutable）
    if cards:
        u = cards[0].get("frontend", {}).get("js", [None])[0]
        if u:
            st, h, _ = fetch(u)
            cc = (h.get("Cache-Control") or h.get("cache-control") or "")
            check(st == 200, "带戳的卡片 JS 可取到", "%s → HTTP %d" % (u, st))
            check("immutable" in cc, "带戳的卡片 JS 吃 immutable 缓存", repr(cc))


# --------------------------------------------------------------------------- #
# E. WebSocket 不受影响                                                        #
# --------------------------------------------------------------------------- #
def test_websocket_unaffected() -> None:
    print("\n=== E. WebSocket 握手不受压缩影响 ===")
    parts = urllib.parse.urlsplit(BASE)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    key = base64.b64encode(b"0123456789abcdef").decode()
    req = (
        "GET /api/system-logs/ws HTTP/1.1\r\n"
        "Host: %s\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: %s\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "Accept-Encoding: gzip\r\n"
        "\r\n" % (host, key)
    )
    try:
        s = socket.create_connection((host, port), timeout=10)
    except OSError as exc:
        check(False, "连上 %s:%d" % (host, port), repr(exc))
        return
    try:
        s.sendall(req.encode())
        data = s.recv(4096)
    except OSError as exc:
        check(False, "WS 握手", repr(exc))
        return
    finally:
        try:
            s.close()
        except OSError:
            pass
    head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1")
    check("101" in head.split("\r\n")[0],
          "WS 握手返回 101 Switching Protocols", head.split("\r\n")[0])
    check("content-encoding" not in head.lower(),
          "101 响应没有 Content-Encoding（WS 不该被压）")


# --------------------------------------------------------------------------- #
# F. 预览视频：首帧海报 + 首屏不加载视频                                        #
# --------------------------------------------------------------------------- #
def test_preview_poster() -> None:
    print("\n=== F. 预览视频：首帧海报 + 首屏不整段下载 ===")
    st, _, html = fetch("/")
    text = html.decode("utf-8", "replace")

    m = re.search(r"window\.__APP_MANIFESTS__\s*=\s*(\{.*?\});", text, re.S)
    if not m:
        check(False, "从 HTML 里抠出 window.__APP_MANIFESTS__")
        return
    try:
        cards = (json.loads(m.group(1)).get("cards") or [])
    except Exception as exc:                            # noqa: BLE001
        check(False, "manifest JSON 可解析", repr(exc))
        return

    with_video = [c for c in cards if c.get("preview_video")]
    check(bool(with_video), "有卡声明了 preview_video", "%d 张" % len(with_video))
    if not with_video:
        return

    c = with_video[0]
    cid = c["id"]
    pv = c["preview_video"]
    poster = c.get("preview_poster") or ""

    # 1. preview_video 带版本戳 → immutable
    st, h, _ = fetch(pv)
    cc = (h.get("Cache-Control") or h.get("cache-control") or "")
    check(st == 200, "带戳的 preview_video 可取到", "%s → HTTP %d" % (pv, st))
    check("immutable" in cc, "preview_video 吃 immutable 缓存", repr(cc))

    # 2. 海报 URL 存在且是 JPEG，体积远小于 mp4
    check(bool(poster), "manifest 里有 preview_poster", poster)
    if poster:
        st, h, body = fetch(poster)
        ctype = (h.get("Content-Type") or h.get("content-type") or "")
        check(st == 200, "GET 海报 → 200", "%s → HTTP %d" % (poster, st))
        check(ctype.startswith("image/jpeg"), "海报 Content-Type 是 image/jpeg", repr(ctype))
        check(body[:2] == b"\xff\xd8", "海报是合法 JPEG（SOI 标记）",
              body[:4].hex())
        st2, _, mp4body = fetch(pv)
        check(len(body) < len(mp4body) / 5,
              "海报比原视频小 5 倍以上",
              "海报 %d B vs 视频 %d B" % (len(body), len(mp4body)))


def test_card_video_lazy() -> None:
    print("\n=== F2. 卡片预览视频首屏不加载（懒加载）===")
    try:
        from playwright.sync_api import sync_playwright
        from verify_asr_wizard import _find_chrome
    except Exception as exc:                            # noqa: BLE001
        print("[SKIP] 没装 playwright: %r" % exc)
        return
    with sync_playwright() as p:
        br = p.chromium.launch(headless=True, args=["--no-sandbox"],
                               executable_path=_find_chrome())
        page = br.new_page()
        mp4_req = []
        page.on("request", lambda r: mp4_req.append(r.url)
                if r.url.endswith(".mp4") else None)
        page.goto(BASE + "/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
        info = page.evaluate("""() => {
            const vs = [...document.querySelectorAll('video.app-card-video')];
            return {
                count: vs.length,
                withSrc: vs.filter(v => v.getAttribute('src')).length,
                withPoster: vs.filter(v => v.getAttribute('poster')).length,
                withData: vs.filter(v => v.getAttribute('data-preview-src')).length,
            };
        }""")
        br.close()
    check(info["count"] > 0, "网格里有预览视频元素", str(info))
    check(info["withSrc"] == 0,
          "首屏没有任何 <video> 带 src（不触发整段下载）",
          "带 src 的有 %d 个" % info["withSrc"])
    check(info["withPoster"] == info["count"],
          "每个视频都有 poster（首帧静态图）",
          "%d/%d" % (info["withPoster"], info["count"]))
    check(info["withData"] == info["count"],
          "每个视频都带 data-preview-src（悬停时才用）",
          "%d/%d" % (info["withData"], info["count"]))
    check(not mp4_req,
          "首屏没有发出任何 .mp4 请求",
          "发了 %d 个：%s" % (len(mp4_req), mp4_req[:3]))


def main() -> int:
    print("=" * 72)
    print("静态资源交付回归：gzip / 缓存 / Range / WebSocket")
    print("BASE = %s" % BASE)
    print("=" * 72)

    try:
        st, _, _ = fetch("/health")
        if st != 200:
            print("服务没起来（/health → %d），先跑 run.py 或 systemctl start" % st)
            return 2
    except Exception as exc:                            # noqa: BLE001
        print("连不上 %s: %r" % (BASE, exc))
        return 2

    test_gzip_effective()
    test_gzip_first_load_bytes()
    test_video_not_breaks()
    test_cache_policy()
    test_card_assets_stamped()
    test_preview_poster()
    test_card_video_lazy()
    test_websocket_unaffected()

    print("\n" + "=" * 72)
    print("通过 %d / 失败 %d" % (_PASS, _FAIL))
    if _FAILURES:
        print("\n失败项：")
        for f in _FAILURES:
            print("  ✗ %s" % f)
    print("=" * 72)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
