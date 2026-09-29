#!/usr/bin/env python3
"""验证 drop-zone 代码已挂上(不依赖 env_check 通过):
1. 直接看 wizard.json upload_endpoint 字段
2. 模拟 app 状态,把 wizard 切到 source 步骤,看 drop-zone 渲染
3. 不依赖真实模型加载(避免 env_check 阻塞)
"""
from playwright.sync_api import sync_playwright

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        ctx = browser.new_context()
        page = ctx.new_page()
        page.on("console", lambda m: print(f"  [{m.type}] {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: print(f"  [pageerror] {e}"))

        ok = 0
        apps = ["video-detection", "video-target-detection", "video-target-localization"]
        for app_id in apps:
            print(f"\n=== {app_id} ===")
            try:
                page.goto(f"http://127.0.0.1:8000/#{app_id}", wait_until="domcontentloaded", timeout=15000)
                page.wait_for_selector(".wd-wizard-section", timeout=8000)
                page.wait_for_timeout(500)

                # 1) wizard.json upload_endpoint 字段
                cfg = page.evaluate(f"""
                    fetch('/static/apps/{app_id}/wizard.json', {{cache:'no-store'}})
                      .then(r => r.ok ? r.json() : null)
                """)
                if not cfg:
                    # 用同步 fetch
                    cfg = page.evaluate(f"""(async () => {{
                        const r = await fetch('/static/apps/{app_id}/wizard.json', {{cache:'no-store'}});
                        return r.ok ? r.json() : null;
                    }})()""")
                ue = cfg.get("upload_endpoint") if cfg else None
                osb = cfg.get("open_source_button") if cfg else None
                print(f"  wizard.json: open_source_button={osb!r}, upload_endpoint={ue!r}")

                # 2) upload endpoint 是否注册
                url_status = page.evaluate(f"""(async () => {{
                    const r = await fetch('{ue}', {{method:'POST'}});
                    return r.status;
                }})()""")
                print(f"  POST {ue} (无文件) → HTTP {url_status} (期望 422 = 已注册;404 = 未注册)")

                if ue and osb and url_status != 404:
                    # 3) 直接渲染 source 步骤(假设 env_check 已通过)
                    page.evaluate(f"""
                        (async () => {{
                            const r = await fetch('/static/apps/{app_id}/wizard.json', {{cache:'no-store'}});
                            const cfg = await r.json();
                            const cards = window.__APP_MANIFESTS__?.cards || [];
                            const manifest = cards.find(c => c.id === '{app_id}');
                            // 强制 envCheck swMissing 为空(模拟模型已就绪)
                            const envCheck = {{
                                hwRequired: manifest?.hardware_capabilities || [],
                                hwMissing: [],
                                swRequired: manifest?.software_capabilities || [],
                                swMissing: [],
                            }};
                            // _active = null,先把 wizard 卸载
                            try {{ window.WizardState.unmount(); }} catch (_) {{}}
                            // 直接渲染到 source 步骤(idx=1)
                            await window.WizardState.mount(manifest, cfg);
                            window.WizardState.stepForward();  // env → source
                        }})()
                    """)
                    page.wait_for_timeout(1500)

                    dz = page.locator(".wd-wizard-drop-zone").count()
                    dt = page.locator(".wd-wizard-drop-text").count()
                    txt = page.locator(".wd-wizard-drop-text").first.inner_text() if dt else ""
                    print(f"  source 步骤渲染后: drop-zone={dz}, text='{txt}'")

                    if dz >= 1 and dt >= 1 and "拖动" in txt:
                        print(f"  ✅ {app_id} drop-zone 代码挂上")
                        ok += 1
                    else:
                        print(f"  ❌ {app_id} drop-zone 未渲染")
                else:
                    print(f"  ⚠️ {app_id}: wizard.json 或 upload endpoint 配置不全,跳过")

                # cleanup
                try:
                    page.evaluate("try { window.WizardState.unmount(); } catch(_){}")
                except Exception:
                    pass
            except Exception as e:
                print(f"  ❌ {app_id}: {e}")

        browser.close()
        print(f"\n=== 总结: {ok}/{len(apps)} ===")
        if ok == len(apps):
            print("✅ 全部通过")
        else:
            print(f"❌ {len(apps) - ok} 失败")

if __name__ == "__main__":
    main()