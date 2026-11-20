#!/usr/bin/env python3
"""回归（浏览器级）：**音频文件 / 含音轨的视频**也能实时翻译。

覆盖
====

1. **翻译面板对三种来源都可见** —— 它**不在** live 块里（live 块由
   ``asr.live.enabled`` 门控）⇒ 选「音频文件」时它照样在。
2. **语言下拉被填满** —— 语言表是运行时才有的（provider 的语言表）。
3. **文件识别 ⇒ 译文真的渲染出来** —— 走完整的 UI 流程（填设备端路径
   → 点「开始识别」→ 轮次帧 → 译文帧 → ``#asr-rt-translated`` 出行）。
4. **「清空」后空态提示回得来** —— ⚠️ 译文区原来用 ``innerHTML = ""``
   重建，会把空态提示节点一起删掉 ⇒ 清空后是一片空白。

跑之前要满足：**ASR 模型**（``qwen3-asr``）与**翻译模型**在跑，脚本
自己会开实时翻译开关。

跑法::

    .venv/bin/python scripts/verify_asr_file_translate_ui.py
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

# ⚠️ Playwright 的两套 TLS 栈不一样：页面走 Chromium（认板子 CA），
#    ctx.request 走 Node（不认）⇒ 必须在 Node 进程起来之前设 CA。
os.environ.setdefault(
    "NODE_EXTRA_CA_CERTS",
    "/home/rockchip/.config/wdnwebui/tls/ca.pem")

from playwright.sync_api import sync_playwright  # noqa: E402

from active_app_guard import (  # noqa: E402
    install as install_active_app_guard,
    install_active_app_spoof,
)

BASE = "https://127.0.0.1:8000"
CARD = "qwen3-asr"
SOURCE = "/userdata/assets/uploads/asr_en.wav"

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ok   {name}")
    else:
        _FAIL += 1
        print(f"  FAIL {name} {extra}")


def api(method: str, path: str, payload=None, timeout: int = 120):
    url = BASE + path
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body or "{}")
        except Exception:  # noqa: BLE001
            return e.code, {"raw": body}
    except Exception as e:  # noqa: BLE001
        return -1, {"error": repr(e)}


def _ensure_models() -> tuple[bool, bool]:
    """ASR 模型 + 翻译模型都在跑？"""
    _, snap = api("GET", "/api/compute")
    asr_ok = any(i.get("model_id") == "qwen3-asr"
                 and i.get("status") == "running"
                 for i in (snap.get("instances") or []))
    if not asr_ok:
        print("  … ASR 模型没在跑，起一个（~30 s）")
        api("POST", "/api/compute/instances",
            {"model": "qwen3-asr", "npu_index": 0}, timeout=180)
        for _ in range(24):
            time.sleep(5)
            _, snap = api("GET", "/api/compute")
            if any(i.get("model_id") == "qwen3-asr"
                   and i.get("status") == "running"
                   for i in (snap.get("instances") or [])):
                asr_ok = True
                break
    _, st = api("GET", "/api/qwen3-asr/translate/status")
    mt_ok = bool((st.get("model") or {}).get("running"))
    if not mt_ok:
        print("  … 翻译模型没在跑，起一个（~30 s）")
        api("POST", "/api/qwen3-asr/translate/enable", {}, timeout=220)
        _, st = api("GET", "/api/qwen3-asr/translate/status")
        mt_ok = bool((st.get("model") or {}).get("running"))
    return asr_ok, mt_ok


def _tr_rows(page) -> list[dict]:
    """译文区里的每一行（原文 + 译文）。"""
    return page.evaluate(
        """() => [...document.querySelectorAll('#asr-rt-translated .asr-tr-item')]
           .map(r => ({
             src: (r.querySelector('.asr-tr-src') || {}).textContent || '',
             dst: (r.querySelector('.asr-tr-dst') || {}).textContent || '',
           }))"""
    )


def _panel_state(page) -> dict:
    """翻译面板的可见性 / 语言数 / 空态提示在不在。"""
    return page.evaluate(
        """() => {
          const blk = document.getElementById('asr-tr-block');
          const sel = document.getElementById('asr-tr-target');
          const empty = document.getElementById('asr-tr-empty');
          const vis = (el) => {
            if (!el) return false;
            const r = el.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) return false;
            // 祖先里有 hidden / display:none ⇒ 也不算数
            for (let p = el; p; p = p.parentElement) {
              const cs = getComputedStyle(p);
              if (cs.display === 'none' || cs.visibility === 'hidden') return false;
            }
            return true;
          };
          return {
            exists: !!blk,
            visible: vis(blk),
            inLiveBlock: !!(blk && blk.closest('#asr-live-block')),
            options: sel ? sel.options.length : 0,
            value: sel ? sel.value : '',
            emptyExists: !!empty,
            emptyVisible: vis(empty),
            emptyText: empty ? (empty.textContent || '').trim() : '',
          };
        }"""
    )


def main() -> int:
    print("=" * 72)
    print("回归（浏览器）：音频文件 ⇒ 实时翻译")
    print("=" * 72)

    asr_ok, mt_ok = _ensure_models()
    check("ASR 模型在跑", asr_ok)
    check("翻译模型在跑", mt_ok)
    if not (asr_ok and mt_ok):
        print("❌ 模型没起来，脚本到此为止")
        return 1

    # 打开实时翻译（目标语言选中文：源语是英文）
    api("POST", "/api/qwen3-asr/translate/settings",
        {"enabled": True, "target_lang": "zh-CN"})
    _, st = api("GET", "/api/qwen3-asr/translate/status")
    check("实时翻译已开启", bool(st.get("enabled")), f"status={st}")

    # ⚠️ **先把上一轮的识别结果清掉** —— 下面要断言「空态提示在（**还没识别
    # 时**）」，这个前提得由脚本自己保证。
    #
    # ⚠️ 轮次只存在**服务端的内存**里 ⇒ 别的脚本（尤其
    # ``verify_asr_video_dual_subtitle.py``）跑完之后，服务端还留着**视频**
    # 轮次 ⇒ 页面一打开就会**恢复**它们 ⇒ 而且（行级翻译是前端驱动的）
    # 恢复的这些行会**重新要一遍译文** ⇒ 译文区就不是空的了 ⇒ 「空态提示
    # 在」这条断言会**假失败**。
    #
    # ⇒ 这里显式 ``POST /reset`` ⇒ 前置条件不靠运气。
    api("POST", "/api/qwen3-asr/reset")

    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 1100},
                                  ignore_https_errors=True)
        # ⚠️ 不碰服务端的 active_app / autostart 配置（见 active_app_guard）
        install_active_app_guard(ctx)
        install_active_app_spoof(ctx)
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))
        page.on("console", lambda m: errors.append(f"[console.{m.type}] {m.text}")
                if m.type == "error" else None)

        page.goto(f"{BASE}/#{CARD}", wait_until="networkidle")
        page.wait_for_timeout(2500)

        # ⚠️ **先把向导跳过** —— 全新浏览器 profile 的 localStorage 是空的
        # ⇒ 框架会**自动挂起向导**，向导在时卡片页是 hidden 的
        # （点「跳过向导」后 unmount(silent=false) 才把 page 显示出来）。
        skip = page.locator("#wd-wizard-root-qwen3-asr .wd-wizard-skip-btn").first
        if skip.count() and skip.is_visible():
            skip.click()
            page.wait_for_timeout(1500)
        page.wait_for_selector("#page-qwen3-asr:not(.hidden)", timeout=15000)
        page.wait_for_timeout(800)

        # --- 1. 翻译面板可见（不在 live 块里）---
        state = _panel_state(page)
        check("翻译面板存在", state["exists"], f"state={state}")
        check("翻译面板**不在** live 块里（否则文件来源看不到）",
              not state["inLiveBlock"], f"state={state}")
        check("翻译面板**可见**（选的是「音频文件」来源）", state["visible"],
              f"state={state}")
        check("语言下拉被填满（运行时语言表）", state["options"] > 1,
              f"options={state['options']}")
        check("空态提示在（还没识别时）", state["emptyVisible"], f"state={state}")

        # --- 2. 走 UI 流程识别一个文件 ---
        page.evaluate(
            """(p) => {
              const el = document.getElementById('asr-source');
              if (el) {
                el.value = p;
                el.dispatchEvent(new Event('input', {bubbles: true}));
              }
            }""",
            SOURCE,
        )
        page.wait_for_timeout(400)
        btn = page.locator("#asr-start-btn")
        check("「开始识别」按钮可点", btn.is_enabled(), "按钮被禁用")
        btn.click()

        # --- 3. 等译文出现在译文区 ---
        rows: list[dict] = []
        t0 = time.time()
        while time.time() - t0 < 240:
            rows = _tr_rows(page)
            if rows:
                break
            page.wait_for_timeout(1000)
        check("文件识别 ⇒ 译文区**真的出行了**", len(rows) >= 1,
              f"等了 {time.time() - t0:.0f}s")
        for r in rows:
            print("    原文: %s" % (r["src"] or "")[:70])
            print("    译文: %s" % (r["dst"] or "")[:70])

        if rows:
            check("每一行都有原文", all((r["src"] or "").strip() for r in rows),
                  f"rows={rows}")
            check("每一行都有译文（不是「未翻译」）",
                  all((r["dst"] or "").strip() and "未翻译" not in (r["dst"] or "")
                      for r in rows), f"rows={rows}")

        state2 = _panel_state(page)
        check("有译文时空态提示收起来了", not state2["emptyVisible"],
              f"state={state2}")

        # --- 4. 「清空」⇒ 空态提示回得来 ---
        page.locator("#asr-reset-btn").click()
        page.wait_for_timeout(2500)
        rows2 = _tr_rows(page)
        state3 = _panel_state(page)
        check("「清空」后译文区空了", len(rows2) == 0, f"rows={rows2}")
        check("「清空」后**空态提示回得来**（不是一片空白）",
              state3["emptyVisible"], f"state={state3}")

        check("没有 JS 错误", not errors, f"errors={errors[:4]}")
        browser.close()

    print("\n" + "=" * 72)
    print(f"通过 {_PASS} / 失败 {_FAIL}")
    print("=" * 72)
    if _FAIL:
        print("❌ 有断言没过")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
