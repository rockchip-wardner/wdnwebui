#!/usr/bin/env python3
"""回归（浏览器级）：**音频文件 / 含音轨的视频**也能实时翻译。

覆盖
====

1. **翻译面板对三种来源都可见** —— 它**不在** live 块里（live 块由
   ``asr.live.enabled`` 门控）⇒ 选「音频文件」时它照样在。
2. **语言下拉被填满** —— 语言表是运行时才有的（provider 的语言表）。
3. **没有独立的译文列表** —— ⚠️ 这是本脚本最核心的一条：译文**只在**
   结果框（句子流）里，与原文**一句一句对应** ⇒ 同一句话在屏幕上
   **只出现一遍**（原来「实时翻译」下面那栏把原文又抄了一遍 ⇒ 两遍）。
4. **文件识别 ⇒ 译文真的渲染出来** —— 走完整的 UI 流程（填设备端路径
   → 点「开始识别」→ 轮次帧 → 译文帧 → 结果框里成对出行）。
5. **结果框里不再有连续文本** —— 那两个「镜像」span（回归脚本读最终
   文本用的）必须**藏起来** ⇒ 否则句子流与连续文本在同一个框里出现两遍。
6. **「清空」后结果框空了** —— 且翻译面板的控制条与说明还在。

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


def _sent_rows(page) -> list[dict]:
    """结果框（句子流）里的每一行 —— 原文 + 译文。"""
    return page.evaluate(
        """() => [...document.querySelectorAll('#asr-sentences .asr-sent')]
           .map(r => ({
             src: (r.querySelector('.asr-sent-src') || {}).textContent || '',
             dst: (r.querySelector('.asr-sent-dst') || {}).textContent || '',
           }))"""
    )


def _panel_state(page) -> dict:
    """翻译面板的可见性 / 语言数 / **有没有独立的译文列表**。"""
    return page.evaluate(
        """() => {
          const blk = document.getElementById('asr-tr-block');
          const sel = document.getElementById('asr-tr-target');
          const note = document.querySelector('#asr-tr-block .asr-tr-note');
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
          // ⚠️ **旧的独立译文列表不许再存在** —— 它把原文又抄了一遍
          // （与结果框逐字重复）⇒ 同一句话在屏幕上出现两遍。
          const old = document.getElementById('asr-rt-translated');
          const oldEmpty = document.getElementById('asr-tr-empty');
          const mirror = document.getElementById('asr-live-committed');
          return {
            exists: !!blk,
            visible: vis(blk),
            inLiveBlock: !!(blk && blk.closest('#asr-live-block')),
            options: sel ? sel.options.length : 0,
            value: sel ? sel.value : '',
            noteExists: !!note,
            noteVisible: vis(note),
            noteText: note ? (note.textContent || '').trim() : '',
            oldListExists: !!old,
            oldEmptyExists: !!oldEmpty,
            // 结果框里那两个「镜像」span 必须**藏起来**（否则句子流与
            // 连续文本在同一个框里出现两遍）。
            mirrorDisplay: mirror ? getComputedStyle(mirror).display : '',
            mirrorLen: mirror ? (mirror.textContent || '').trim().length : 0,
          };
        }"""
    )


def main() -> int:
    print("=" * 72)
    print("回归（浏览器）：音频文件 ⇒ 实时翻译（译文并入结果框）")
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

    # ⚠️ **先把上一轮的识别结果清掉** —— 下面要断言「还没识别时结果框是
    # 空的」，这个前提得由脚本自己保证。
    #
    # ⚠️ 轮次只存在**服务端的内存**里 ⇒ 别的脚本（尤其
    # ``verify_asr_video_dual_subtitle.py``）跑完之后，服务端还留着**视频**
    # 轮次 ⇒ 页面一打开就会**恢复**它们 ⇒ 译文区就不是空的了 ⇒ 断言会
    # **假失败**。
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
        check("翻译块有一行说明（告诉用户译文在哪）", state["noteVisible"],
              f"state={state}")

        # --- 2. **没有独立的译文列表**（本脚本的核心）---
        check("**没有独立的译文列表**（译文只在结果框里）",
              not state["oldListExists"], f"state={state}")
        check("旧的空态提示节点也没了", not state["oldEmptyExists"],
              f"state={state}")
        check("结果框里那两个「镜像」span **藏起来了**"
              "（否则句子流与连续文本出现两遍）",
              state["mirrorDisplay"] == "none", f"state={state}")

        # --- 3. 走 UI 流程识别一个文件 ---
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

        # --- 4. 等译文出现在结果框（与原文成对）---
        rows: list[dict] = []
        t0 = time.time()
        while time.time() - t0 < 240:
            rows = _sent_rows(page)
            done = [r for r in rows
                    if (r["dst"] or "").strip()
                    and "翻译中" not in (r["dst"] or "")
                    and "未翻译" not in (r["dst"] or "")]
            if done:
                rows = done
                break
            page.wait_for_timeout(1000)
        check("文件识别 ⇒ 结果框里**原文 + 译文成对出行了**", len(rows) >= 1,
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
            # ⚠️ **译文挂在它自己的原文下面** ⇒ 一一对应，不重复。
            check("译文是**逐句成对**的（行数一致）",
                  len(rows) == len(_sent_rows(page)),
                  f"rows={rows}")

        # --- 5c. 那句没意义的提示**不许再出现**（2026-11-27 用户要求删掉）---
        # ⚠️ 「点击『开始识别』重新跑一遍。」—— 没有信息量（按钮就在旁边）⇒
        #    删掉了 ⇒ 断言钉住，防回退。
        # ⚠️ **在清空之前查** —— 清空之后提示行会变成「已清空」⇒ 查它
        #    查不出那句话（假通过）。
        hint_now = page.evaluate(
            """() => (document.getElementById('asr-hint')
                    ?.textContent || '').trim()""")
        check("页面上**没有**「点击「开始识别」重新跑一遍。」",
              "重新跑一遍" not in hint_now, f"实际 = {hint_now!r}")

        # --- 5d. 识别的**完成信息**在「实时翻译」块里（2026-11-27 用户要求）---
        # ⚠️ **原来它在页面最底下的全局提示行**（``#asr-hint``，「显示模式」
        #    上一行）⇒ 用户提出来：离「开始识别 / 停止 / 清空」按钮太远
        #    （按钮在「实时翻译」块里，提示却掉到卡片底部）⇒ **搬进这一块**。
        #
        # ⚠️⚠️ **必须等「识别完成」那一帧到**（实测踩到）—— 译文是**边识别
        #    边出**的 ⇒ 译文先到、收尾帧（``done``）后到（实测 2.0 s vs
        #    2.9 s）⇒ 不等就查 ⇒ 查到的永远是**空行**（假失败）。
        t1 = time.time()
        while time.time() - t1 < 120:
            got = page.evaluate(
                """() => (document.getElementById('asr-tr-info')
                        ?.textContent || '').trim()""")
            if got:
                break
            page.wait_for_timeout(500)
        info = page.evaluate(
            """() => {
                const el = document.getElementById('asr-tr-info');
                const blk = document.getElementById('asr-tr-block');
                const grp = document.querySelector('#asr-tr-block .asr-tr-run');
                const hint = document.getElementById('asr-hint');
                const r = (e) => (e ? e.getBoundingClientRect() : null);
                const box = r(el), gbox = r(grp);
                const cs = el ? getComputedStyle(el) : null;
                return {
                    exists: !!el,
                    text: el ? (el.textContent || '').trim() : '',
                    visible: !!el && !!cs && cs.display !== 'none'
                             && !!box && box.height > 0,
                    inBlock: !!(blk && el && blk.contains(el)),
                    belowGroup: !!(box && gbox && box.top >= gbox.bottom - 1),
                    hint: hint ? (hint.textContent || '').trim() : '',
                };
            }""")
        check("有完成信息那一行（#asr-tr-info）", info["exists"])
        check("识别跑完后那一行**有内容**", bool(info["text"]),
              f"实际 = {info['text']!r}")
        check("内容是「识别完成（…）」", info["text"].startswith("识别完成（"),
              f"实际 = {info['text']!r}")
        check("内容里带**耗时**", " s" in info["text"], f"实际 = {info['text']!r}")
        check("内容里带**语种**", "语种" in info["text"],
              f"实际 = {info['text']!r}")
        check("那一行在「实时翻译」块里", info["inBlock"])
        check("那一行在**按钮组下方**", info["belowGroup"])
        check("那一行**可见**", info["visible"])
        # ⚠️ **全局提示行不再拖这条详情** —— 它是「识别中…」「已停止」「出错」
        #    那种临时提示 ⇒ 详情搬走后它只留一句短的「识别完成」。
        check("全局提示行**不再**写那条详情（只剩短状态）",
              "识别完成（" not in info["hint"] and "语种" not in info["hint"],
              f"实际 = {info['hint']!r}")
        print("    完成信息:", info["text"])

        # --- 5. 「清空」⇒ 结果框空了 ---
        page.locator("#asr-reset-btn").click()
        page.wait_for_timeout(2500)
        rows2 = _sent_rows(page)
        state3 = _panel_state(page)
        check("「清空」后结果框空了", len(rows2) == 0, f"rows={rows2}")
        check("「清空」后翻译面板的控制条还在",
              state3["visible"] and state3["noteVisible"], f"state={state3}")

        # --- 5b. 「清空」⇒ 「实时翻译」的状态显示「已清空」（2026-11-27 用户要求）---
        # ⚠️ **为什么要有这条** —— 清空之前这里写着「翻译模型就绪 · 实时翻译
        #    已开启」⇒ 用户以为**还在译**（其实结果已经没了）⇒ 参照「配音
        #    导出」的清空（它清完就把状态写成「已清空」）。
        cleared_state = page.evaluate(
            """() => {
                const clear = document.getElementById('asr-tr-clear-status');
                const btn = document.getElementById('asr-reset-btn');
                const dub = document.getElementById('asr-dub-status');
                const cls = (el) => el ? (el.className || '') : '';
                return {
                    clear: clear ? (clear.textContent || '').trim() : '',
                    clearCls: cls(clear),
                    dubCls: cls(dub),
                    tr: (document.getElementById('asr-tr-status')
                         ?.textContent || '').trim(),
                    hint: (document.getElementById('asr-hint')
                           ?.textContent || '').trim(),
                    info: (document.getElementById('asr-tr-info')
                           ?.textContent || '').trim(),
                    resetDisabled: btn ? btn.disabled : null,
                };
            }""")
        # ⚠️ **「已清空」在「清空」按钮右边那个状态位里**（2026-11-27 用户
        #    要求）⇒ 与「配音导出」的 ``#asr-dub-status`` **同位置同风格**。
        check("「清空」后「清空」按钮右边的状态 = 「已清空」",
              cleared_state["clear"] == "已清空",
              f"实际 = {cleared_state['clear']!r}")
        check("那个状态位的**样式**与「配音导出」的同一款（asr-live-status）",
              "asr-live-status" in cleared_state["clearCls"]
              and cleared_state["clearCls"] == cleared_state["dubCls"],
              f"清空位={cleared_state['clearCls']!r} "
              f"配音位={cleared_state['dubCls']!r}")
        check("「清空」后**模型状态**那行不被顶掉（还是模型的事）",
              "已清空" not in cleared_state["tr"],
              f"实际 = {cleared_state['tr']!r}")
        check("「清空」后提示行 = 「已清空」",
              cleared_state["hint"] == "已清空",
              f"实际 = {cleared_state['hint']!r}")
        # ⚠️ **没东西可清 ⇒ 「清空」按钮灰着**（2026-11-27 用户要求）。
        check("「清空」后「清空」按钮**变灰**（没东西可清了）",
              cleared_state["resetDisabled"] is True,
              f"disabled={cleared_state['resetDisabled']}")
        # ⚠️ **完成信息也清掉** —— 它讲的是「上一次识别」⇒ 结果都清了 ⇒
        #    那一行不该留着（「已清空」由按钮右边的状态位说）。
        check("「清空」后完成信息也清掉了",
              cleared_state["info"] == "", f"实际 = {cleared_state['info']!r}")

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
