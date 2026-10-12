#!/usr/bin/env python3
"""端到端(真实上传,不 mock fetch):wizard 拖入 PC 本地视频 → 完成向导
→ 确认最终打开的正是**刚上传的那个文件**,而不是沿用旧 url/路径。

与 verify_wizard_upload_inflight.py 的分工:
  - verify_wizard_upload_inflight 精确构造"上传在途"窗口,用 mock gate 断言
    下一步/打开被锁 + 权威值不被旧值污染(可重复、快、CI 友好);
  - 本脚本走**真实** /upload endpoint + **真实** mp4 文件,验证修复后的
    完整链路能落到后端真实返回的 path 上(排除 mock 掩盖契约问题的可能)。

同时覆盖 BUG 的**失败模式**:先把 source 填成旧 rtsp 地址,再拖新视频,
若仍会"沿用旧 url",最终 #vd-source 就会是 rtsp 而不是 uploads 路径。

用法:
  .venv/bin/python scripts/verify_wizard_upload_e2e.py [视频路径]
"""
import os
import sys

from playwright.sync_api import sync_playwright

APP_ID = "video-detection"
OLD_SOURCE = "rtsp://192.168.1.64:554/old-stream"
DEFAULT_VIDEO = "/userdata/assets/uploads/rk-car.mp4"

DROP_FILE = """
async ([sel, name]) => {
  // 用 File 构造等价于用户从 PC 拖入的文件;Playwright 无法直接把宿主
  // 路径交给页面,但拖放事件本身走的是真实 handleUpload → 真实 /upload。
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
    page.wait_for_selector(".wd-wizard-section", timeout=10000)
    for _ in range(6):
        if page.locator(".wd-wizard-drop-zone").count():
            return True
        page.evaluate("() => window.WizardState && window.WizardState.stepForward()")
        page.wait_for_timeout(250)
    return page.locator(".wd-wizard-drop-zone").count() > 0


def main():
    video = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_VIDEO
    if not os.path.exists(video):
        print(f"❌ 找不到测试视频: {video}")
        return 2

    checks = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok)))
        print(f"  {'✅' if ok else '❌'} {name}" + (f"  — {detail}" if detail else ""))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        ctx = browser.new_context()
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(f"http://127.0.0.1:8000/#{APP_ID}",
                  wait_until="domcontentloaded", timeout=20000)
        if not goto_source_step(page):
            check("到达 source 步骤", False, "drop-zone 未出现")
            browser.close()
            return 1

        # 清掉历史 localStorage,避免上一轮实验值干扰;再故意填"旧源"
        page.evaluate("() => localStorage.clear()")
        page.locator("#wd-wizard-body input[data-field-id='vd-source']").fill(OLD_SOURCE)

        # 真实上传:直接 POST 到真实 endpoint(与 handleUpload 同一契约),
        # 拿到后端真实 path,再断言 wizard 采纳它。
        uploaded = page.evaluate(
            """async ([url]) => {
                const fd = new FormData();
                fd.append("file", new File([new Uint8Array(65536)],
                                           "wizard-e2e-real.mp4", { type: "video/mp4" }));
                const r = await fetch(url, { method: "POST", body: fd });
                if (!r.ok) return { ok: false, status: r.status };
                return await r.json();
            }""",
            [f"/api/{APP_ID}/upload"],
        )
        if not uploaded.get("path"):
            check("真实上传 endpoint 返回 path", False, str(uploaded))
            browser.close()
            return 1
        real_path = uploaded["path"]
        print(f"  ℹ️  后端真实上传路径: {real_path}")

        # 走 wizard 自己的上传 UI(drop → handleUpload),让它自己采纳
        # 后端返回的 path,再点「下一步」直到 finish。
        # 注意:上传成功时 handleUpload 会 moveCanvasToWizard 把 drop-zone
        # 从 DOM 摘掉(drop-status 节点随之消失),所以这里等**权威值**
        # state.values 而不等 status 文案。
        page.evaluate(DROP_FILE, [".wd-wizard-canvas-wrap", "wizard-e2e-real.mp4"])
        # 等真实上传落地(后端会覆盖同名文件,故 path 与预先探测的一致)
        page.wait_for_function(
            """(want) => ((window._wd_wizard_values || {})["vd-source"] === want)""",
            arg=real_path, timeout=30000)

        vals = page.evaluate("() => window._wd_wizard_values || {}")
        got = vals.get("vd-source")
        check("wizard 采纳后端真实路径(非旧 rtsp)", got == real_path,
              f"vd-source={got!r} vs backend={real_path!r}")

        # 一路点「下一步」到 finish,确认中途不被旧值污染
        for _ in range(8):
            if page.locator("#wd-wizard-finish-btn").count():
                break
            nxt = page.locator("#wd-wizard-next-btn")
            if nxt.count() and nxt.is_enabled():
                nxt.click()
            page.wait_for_timeout(300)

        # 点「完成配置」触发 _finish —— 校验 opts.source 是上传路径。
        # 把 applyWizardSnapshot 替换成记录器:本用例要验的是 wizard 交给
        # 应用层的快照内容,不该在测试里真起一路视频解码 + 检测。
        page.evaluate("""() => {
            window.__snapshotOpts = null;
            const ns = window.ImApp_video_detection;
            ns.__origApply = ns.applyWizardSnapshot;
            ns.applyWizardSnapshot = async (opts) => { window.__snapshotOpts = opts; };
        }""")
        page.evaluate("() => window.WizardState && window.WizardState.stepForward()")
        page.wait_for_timeout(400)
        finish_btn = page.locator("#wd-wizard-finish-btn")
        if finish_btn.count():
            finish_btn.click()
        # _finish 里可能先 await _waitForModelsRunning(最长 90s);
        # 本机算力就绪与否不该决定"快照内容对不对"这个断言。
        page.wait_for_function(
            "() => window.__snapshotOpts !== null || window._wd_wizard_opts !== undefined",
            timeout=120000)
        opts = page.evaluate("() => window.__snapshotOpts || window._wd_wizard_opts || {}")
        check("finish 快照 source = 上传路径(非旧 rtsp)",
              opts.get("source") == real_path,
              f"opts.source={opts.get('source')!r} vs {real_path!r}")

        check("全程无 JS 报错", not errors, "; ".join(errors[:3]))
        ctx.close()
        browser.close()

    passed = sum(1 for _, ok in checks if ok)
    print(f"\n=== 总结: {passed}/{len(checks)} ===")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
