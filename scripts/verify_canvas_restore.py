#!/usr/bin/env python3
"""验证 wizard unmount 把画面**按原位**还原。

只判「canvas 还在页面里」不够 —— 这正是上一版放过本 bug 的原因：多路卡的
画面容器自己就是 .vd-stage，.vd-stage-col 里它后面还排着「视频源 / 判据 /
控制」几段，还原成 appendChild 时画面确实「在页面里」，只是掉到了配置区
**下面**。所以断言的是 **(父节点, 兄弟下标)** 与搬走之前逐字相同。

覆盖四张卡：三张多路（画面容器 = grid 容器 = .vd-stage）+ 一张单路
（VR 的 <canvas> 套在 .vd-stage 里），后者防的是「为多路改坏单路」。
"""
import sys

from playwright.sync_api import sync_playwright

VIDEO = "/userdata/test/tmp/video_target_detection/60b0af0d5cc54e9fa22b169864876c2c.mp4"
BASE = "http://127.0.0.1:8000"

# (app_id, wizard.json 里声明或按 -open→-canvas 推导出的画面 id, 视频源字段 id)
CARDS = [
    ("video-detection", "vd-grid", "vd-sources"),
    ("video-target-detection", "vtd-grid", "vtd-sources"),
    ("video-target-localization", "vtl-grid", "vtl-sources"),
    ("video-retrieval", "vr-canvas", "vr-source"),   # 无 canvas_id，推导
]

# 画面在哪 + 在第几位。必须连父节点一起记，只记「第 0 位」会漏掉
# 「被塞进另一个父节点且恰好也是第 0 位」。
PROBE = """
([cid]) => {
  const c = document.getElementById(cid);
  if (!c) return {exists: false};
  const p = c.parentElement;
  return {
    exists: true,
    parent: p ? (p.id || (p.className || '').toString().slice(0, 40)) : null,
    index: p ? [...p.children].indexOf(c) : -1,
    total: p ? p.children.length : 0,
    inWizard: !!(c.closest && c.closest('.wd-wizard-canvas-wrap')),
  };
}
"""


def mount_and_goto_source(page, app_id, src_id):
    """按现有脚本的做法手动 mount 向导并走到含视频源字段的那一步。"""
    # manifest 是启动时异步注册的，等它就位再 mount
    page.wait_for_function(
        "id => (window.__APP_MANIFESTS__?.cards || []).some(c => c.id === id)",
        arg=app_id, timeout=15000)
    # 注意必须写成**函数表达式**而不是 `(async (appId) => {...})()`:
    # Playwright 只对前者传 arg，IIFE 形式会把参数整个吞掉，appId 恒为
    # undefined → mount 里读 app.id 直接抛。
    page.evaluate("""
        async (appId) => {
            try { window.WizardState.unmount(); } catch(_) {}
            const manifest = (window.__APP_MANIFESTS__?.cards || [])
                .find(c => c.id === appId);
            if (!manifest) throw new Error('manifest not found: ' + appId);
            const r = await fetch('/static/apps/' + appId + '/wizard.json', {cache:'no-store'});
            const cfg = await r.json();
            await window.WizardState.mount(manifest, cfg);
        }
    """, app_id)
    page.wait_for_timeout(500)
    # env_check 之后可能还有别的步，逐步前进直到视频源字段出现
    for _ in range(5):
        if page.evaluate("id => !!document.querySelector('[data-field-id=\"' + id + '\"]')", src_id):
            return True
        page.evaluate("try { window.WizardState.stepForward(); } catch(_) {}")
        page.wait_for_timeout(350)
    return page.evaluate("id => !!document.querySelector('[data-field-id=\"' + id + '\"]')", src_id)


def run(page, app_id, cid, src_id, errs):
    print(f"=== {app_id} (画面={cid}) ===")
    page.goto(f"{BASE}/#/{app_id}", wait_until="domcontentloaded", timeout=15000)
    page.evaluate("Object.keys(localStorage).filter(k => k.includes('wizard')).forEach(k => localStorage.removeItem(k))")
    page.wait_for_timeout(600)

    if not mount_and_goto_source(page, app_id, src_id):
        print("  ❌ 走不到视频源步，跳过")
        return 1

    before = page.evaluate(PROBE, [cid])
    if not before.get("exists"):
        print(f"  ❌ 搬走之前画面就不在 DOM 里: {before}")
        return 1
    print(f"  搬走前: parent={before['parent']} index={before['index']}/{before['total']}")

    page.evaluate("""([sid, v]) => {
        const el = document.querySelector('[data-field-id="' + sid + '"]');
        if (el) { el.value = v; el.dispatchEvent(new Event('input', {bubbles: true})); }
    }""", [src_id, VIDEO])
    page.wait_for_timeout(200)
    page.evaluate("""() => {
        const b = [...document.querySelectorAll('.wd-wizard-source-action-btn')]
            .find(x => x.textContent.trim() === '播放视频');
        if (b) b.click();
    }""")
    page.wait_for_timeout(2500)

    in_wiz = page.evaluate(PROBE, [cid])
    if not in_wiz.get("inWizard"):
        print(f"  ❌ 画面没搬进 wizard，后面的断言无意义: {in_wiz}")
        return 1
    print("  点「播放视频」后: 画面在 wizard canvas-wrap 内 ✅")

    page.evaluate("try { window.WizardState.unmount(); } catch(_) {}")
    page.wait_for_timeout(600)

    after = page.evaluate(PROBE, [cid])
    print(f"  unmount 后: parent={after.get('parent')} index={after.get('index')}/{after.get('total')}")

    fails = []
    if not after.get("exists"):
        fails.append("画面彻底消失")
    else:
        if after["parent"] != before["parent"]:
            fails.append(f"父节点变了: {before['parent']} → {after['parent']}")
        if after["index"] != before["index"]:
            fails.append(f"位置错了: 第 {before['index']} 位 → 第 {after['index']} 位"
                         f"（共 {after['total']} 个兄弟）")
    # 只清当前这张卡。会话不清会让下一张卡带着上一张的解码器跑。
    # VR 的关闭端点是 /source/close（单路预览用），多路三张才是 /close。
    close_path = ("/api/video-retrieval/source/close" if app_id == "video-retrieval"
                  else "/api/%s/close" % app_id)
    page.evaluate("""(p) => { try {
        fetch(p, {method: 'POST', headers: {'Content-Type': 'application/json'},
                  body: JSON.stringify({sources: []})});
    } catch(_) {} }""", close_path)

    for f in fails:
        print(f"  ❌ {f}")
    if not fails:
        print("  ✅ 画面按原位还原")
    return 1 if fails else 0


def main():
    rc = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        errs = []
        fails404 = []
        page.on("pageerror", lambda e: errs.append("pageerror: " + str(e)))
        page.on("console", lambda m: (
            fails404.append(m.text[:120]) if m.type == "error"
            and "Failed to load resource" in m.text else
            errs.append(m.text[:200]) if m.type == "error" else None))
        page.on("response", lambda r: fails404.append("%d %s" % (r.status, r.url))
                if r.status >= 400 else None)
        for app_id, cid, src_id in CARDS:
            rc |= run(page, app_id, cid, src_id, errs)
            print()
        if fails404:
            print(f"  ⚠️  资源请求失败 {len(fails404)} 条（与本用例无关）: {fails404[:3]}")
        if errs:
            print(f"  ❌ 页面 JS 报错 {len(errs)} 条: {errs[:3]}")
            rc = 1
        else:
            print("  ✅ 无页面 JS 报错")
        browser.close()
    print()
    print("结果: " + ("全部通过" if rc == 0 else "存在失败项"))
    return rc


if __name__ == "__main__":
    sys.exit(main() or 0)
