#!/usr/bin/env python3
"""回归:wizard source 步骤"上传在途时点下一步"导致沿用旧 url/路径。

BUG 复现链(修复前):
  t0 向导 source input 里填的是**旧**视频源(上次用的 rtsp://… 或本地路径)
  t1 拖入新视频 → POST /upload 在途(几 MB~几百 MB 要几秒到几十秒),
     此时 el.value 仍是旧值
  t2 用户点「下一步」→ _captureValues 把**旧值**当作用户填的视频源存下
  t3 fetch resolve → el.value = 新 path,但 el 已随 step 重建脱离文档,
     state.values 里仍是旧值
  t4 finish → applyWizardSnapshot(opts.source = 旧值) → 打开的不是刚传的视频

做法:在页面内覆写 window.fetch,把 upload 请求挂起成一个可控的 Promise,
由测试显式放行 —— 制造一个确定性的"上传在途"窗口,再断言:
  C1 上传在途时「下一步」按钮 disabled
  C2 上传在途时点「下一步」不跳步(仍在 source 步骤)
  C3 上传在途时「打开」按钮同样 disabled(同源问题,防放旧源)
  C4 上传完成后 wizard 权威值是**新上传路径**,不是旧 url
  C5 上传完成后「下一步」恢复可用
  C6 上传失败时「下一步」恢复可用(没把用户永久卡在 source 步)

用法:
  .venv/bin/python scripts/verify_wizard_upload_inflight.py
(需先起服务:./run.py 或 uvicorn app.main:app --port 8000)
"""
import sys

from playwright.sync_api import sync_playwright

APP_ID = "video-detection"
OLD_SOURCE = "rtsp://192.168.1.64:554/old-stream"
NEW_PATH = "/tmp/uploads/wizard_upload_fix.mp4"
UPLOAD_URL = f"/api/{APP_ID}/upload"

# 页面内 fetch 拦截:把 upload 请求挂起,等 window.__releaseUpload() 放行。
# 故意用 page.evaluate 在页面上下文里做(而非 Playwright route 阻塞)——
# route handler 跑在 dispatch 线程上,阻塞它会死锁测试主线程。
INSTALL_GATE = """
([uploadUrl, newPath]) => {
  window.__uploadGate = { pending: 0, done: 0 };
  const origFetch = window.fetch.bind(window);
  window.__releaseUpload = () => {
    window.__uploadGate.pending = 0;
    window.__uploadGate.done++;
  };
  window.fetch = function (input, init) {
    const url = typeof input === "string" ? input : (input && input.url) || "";
    if (!url.includes(uploadUrl)) return origFetch(input, init);
    window.__uploadGate.pending++;
    return new Promise((resolve) => {
      window.__releaseUpload = () => {
        resolve(new Response(JSON.stringify({ ok: true, path: newPath }),
                            { status: 200,
                              headers: { "Content-Type": "application/json" } }));
      };
    });
  };
}
"""

DROP_FILE = """
async ([sel, name]) => {
  const dt = new DataTransfer();
  dt.items.add(new File([new Uint8Array(65536)], name, { type: "video/mp4" }));
  const el = document.querySelector(sel);
  if (!el) return "no-target";
  el.dispatchEvent(new DragEvent("drop",
    { dataTransfer: dt, bubbles: true, cancelable: true }));
  return "dropped";
}
"""


def goto_source_step(page):
    """从向导入口跳到 source 步骤(drop-zone 出现)。

    env_check 步骤在模型未 running 时「下一步」是 disabled 的,那是设计
    行为,不是本用例要测的东西,所以直接调 WizardState.stepForward() 前进,
    不去干扰 env_check 语义。
    """
    page.wait_for_selector(".wd-wizard-section", timeout=10000)
    for _ in range(6):
        if page.locator(".wd-wizard-drop-zone").count():
            return True
        page.evaluate("() => window.WizardState && window.WizardState.stepForward()")
        page.wait_for_timeout(250)
    return page.locator(".wd-wizard-drop-zone").count() > 0


def main():
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok)))
        print(f"  {'✅' if ok else '❌'} {name}" + (f"  — {detail}" if detail else ""))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        page_errors = []

        # ---------------- 主用例:上传在途 ---------------- #
        ctx = browser.new_context()
        page = ctx.new_page()
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.goto(f"http://127.0.0.1:8000/#{APP_ID}",
                  wait_until="domcontentloaded", timeout=20000)
        if not goto_source_step(page):
            check("到达 source 步骤", False, "drop-zone 未出现")
            browser.close()
            return 1

        page.evaluate(INSTALL_GATE, [UPLOAD_URL, NEW_PATH])

        # t0:source input 里填"旧"视频源(用户上一次用的)
        page.locator("#wd-wizard-body textarea[data-field-id='vd-sources']").fill(OLD_SOURCE)

        # t1:拖入新视频 → upload 在途
        res = page.evaluate(DROP_FILE, [".wd-wizard-canvas-wrap", "brand-new-clip.mp4"])
        if res != "dropped":
            check("拖放触发上传", False, res)
            browser.close()
            return 1
        page.wait_for_function("() => window.__uploadGate && window.__uploadGate.pending > 0",
                               timeout=5000)

        # C1 上传在途时「下一步」disabled
        nxt = page.locator("#wd-wizard-next-btn")
        check("C1 上传在途时「下一步」disabled", nxt.is_disabled(),
              f"disabled={nxt.is_disabled()}")

        # C3 上传在途时「打开」disabled
        openbtn = page.locator("#wd-wizard-source-open-vd-sources")
        has_open = openbtn.count() > 0
        check("C3 上传在途时「打开」disabled",
              has_open and openbtn.is_disabled(),
              f"count={has_open} disabled={openbtn.is_disabled() if has_open else 'N/A'}")

        # C2 强行 click(模拟键盘 Enter / 脚本旁路),断言不跳步
        page.evaluate("() => document.getElementById('wd-wizard-next-btn')?.click()")
        page.wait_for_timeout(250)
        still_source = page.locator(".wd-wizard-drop-zone").count() > 0
        check("C2 上传在途点「下一步」不跳步", still_source,
              f"drop-zone 仍在={still_source}")

        # t3:放行上传
        page.evaluate("() => window.__releaseUpload()")
        page.wait_for_function(
            "() => (window._wd_wizard_values||{})['vd-sources']"
            " && window._wd_wizard_values['vd-sources'].includes('wizard_upload_fix')",
            timeout=8000)

        # C4 权威值 = 新上传路径,不是旧 url
        got = page.evaluate("() => (window._wd_wizard_values||{})['vd-sources']")
        check("C4 上传后 wizard 值为新上传路径", got == NEW_PATH,
              f"vd-sources={got!r} (旧值 {OLD_SOURCE!r})")

        # C5 上传完成后「下一步」恢复可用
        page.wait_for_timeout(300)
        check("C5 上传完成后「下一步」恢复可用", nxt.is_enabled(),
              f"enabled={nxt.is_enabled()}")
        ctx.close()

        # ---------------- 失败用例:上传失败要解锁 ---------------- #
        ctx2 = browser.new_context()
        page2 = ctx2.new_page()
        page2.on("pageerror", lambda e: page_errors.append(str(e)))
        page2.route(f"**{UPLOAD_URL}", lambda route: route.fulfill(
            status=500, content_type="application/json", body='{"ok":false}'))
        page2.goto(f"http://127.0.0.1:8000/#{APP_ID}",
                   wait_until="domcontentloaded", timeout=20000)
        if not goto_source_step(page2):
            check("C6 失败用例到达 source 步骤", False, "drop-zone 未出现")
            browser.close()
            return 1
        page2.evaluate(DROP_FILE, [".wd-wizard-canvas-wrap", "boom.mp4"])
        page2.wait_for_function(
            "() => { const b = document.querySelector('.wd-wizard-drop-status');"
            " return b && b.className.includes('drop-status-err'); }",
            timeout=8000)
        n2 = page2.locator("#wd-wizard-next-btn")
        check("C6 上传失败后「下一步」恢复可用", n2.is_enabled(),
              f"enabled={n2.is_enabled()}")
        ctx2.close()
        browser.close()

        if page_errors:
            print(f"\n  ⚠️  页面 JS 报错: {page_errors}")

    passed = sum(1 for _, ok in checks if ok)
    print(f"\n=== 总结: {passed}/{len(checks)} ===")
    return 0 if passed == len(checks) and not page_errors else 1


if __name__ == "__main__":
    sys.exit(main())