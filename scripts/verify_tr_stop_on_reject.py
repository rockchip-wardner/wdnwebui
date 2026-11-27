#!/usr/bin/env python3
"""回归：**「翻译没开」⇒ 前端立刻停手**（2026-11-26 实测踩到）。

用户实测现象
------------
「实时翻译」明明是**关**的，前端却一直 ``POST /api/qwen3-asr/translate/line``
⇒ 实测 **~67 次/分 × 6 个标签页** ⇒ 白白空转，把「失败」计数刷爆。

根因
----
* 服务端翻译没开 ⇒ 每次请求都返回 ``{"ok": false, "skipped": "翻译没开"}``；
* 前端原来只 ``console.warn`` 就 ``return`` ⇒ **既不增长 tries、也不停手**；
* 典型触发场景：**服务重启** ⇒ 服务端开关复位成关，而页面里的勾还开着。

修法
----
* 服务端拒收时**回传权威的 ``enabled``**；
* 前端拒收时把本地开关**拉回服务端的值** ⇒ 立刻停手。

本脚本覆盖
----------
1. **拒收响应带权威值**（API 层）；
2. **前端收到拒收 ⇒ 停手**（浏览器层，**真的数请求数**）；
3. **停手后不再发新请求**（观察窗口内请求数**不再增长**）；
4. **勾选框被同步**（用户看得见服务端把它关了）。

⚠️ **不占 NPU**：翻译全程是关的 ⇒ 服务端**拒收** ⇒ 一次推理都不跑。
⚠️ **不碰用户的会话**：用独立的 Playwright 上下文 ⇒ 与用户自己的标签页
    无关；但**仍按规矩 mock 掉 ``PUT/DELETE /api/compute/active-app``**
    （别改生产的 ``compute.autostart``）。

跑法::

    .venv/bin/python scripts/verify_tr_stop_on_reject.py
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

_SCRIPT_DIR = os.path.join(_ROOT, "scripts")
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

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

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

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


def api(method: str, path: str, payload=None, timeout: int = 60):
    url = BASE + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, context=_CTX, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": str(e)}
    try:
        return r.status, json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return r.status, {}


def _run_checks() -> None:
    """跑断言（**由 ``main`` 用 try/finally 包着** ⇒ 中途抛异常也会恢复开关）。"""

    # ---------------- 1. API 层：拒收响应带权威值 ----------------
    st, body = api("GET", "/api/qwen3-asr/translate/status")
    if body.get("enabled"):
        # ⚠️ 翻译开着 ⇒ 先关掉（这个脚本要验证的是「关着」时的行为）。
        #
        # ⚠️⚠️ **条件曾经写反**（2026-11-26 实测踩坑）：原来写的是
        # ``if not body.get("enabled")`` ⇒ **只在「已经关着」时才去关**
        # ⇒ 翻译**开着**时压根没关 ⇒ 后面的「前置条件」「拒收返回 ok=false」
        # 「拒收响应带权威 enabled」三条一起挂（实测第 2 轮 3/9 失败）。
        # ⇒ 现在：开着就关；本来就关着 ⇒ 什么都不用做。
        api("POST", "/api/qwen3-asr/translate/settings", {"enabled": False})
        time.sleep(1.0)
    st, body = api("GET", "/api/qwen3-asr/translate/status")
    check("翻译开关是关的（前置条件）", body.get("enabled") is False,
          f"得到 {body.get('enabled')!r}")

    st, body = api("POST", "/api/qwen3-asr/translate/line",
                   {"text": "测试", "seq": 999999, "lang": "zh-CN"})
    check("拒收返回 ok=false", body.get("ok") is False, f"得到 {body}")
    check("拒收响应带**权威的 enabled 字段**",
          body.get("enabled") is False, f"得到 {body}")
    check("拒收仍带 skipped 说明", bool(body.get("skipped")), f"得到 {body}")

    # ---------------- 2/3/4. 浏览器层：前端真的停手 ----------------
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        ctx = browser.new_context(ignore_https_errors=True)
        # ⚠️ **必须**在 ``ctx.new_page()`` **之前**装（别改生产的
        #    ``compute.autostart``，也别让页面跳到上一张卡）。
        install_active_app_guard(ctx)
        install_active_app_spoof(ctx)
        page = ctx.new_page()

        # ⚠️ 数「行级翻译请求」—— 这是风暴的度量。
        hits = {"n": 0, "bodies": []}

        def on_req(req):
            if req.method == "POST" and req.url.endswith("/api/qwen3-asr/translate/line"):
                hits["n"] += 1
                try:
                    hits["bodies"].append(req.post_data)
                except Exception:  # noqa: BLE001
                    pass

        page.on("request", on_req)

        page.goto(BASE + "/#qwen3-asr", wait_until="domcontentloaded")
        page.wait_for_timeout(2500)

        # ⚠️ 强制「前端以为开着、服务端其实关着」—— 这正是重启后的状态。
        #
        # ⚠️ **顺序很重要**：``_tr.enabled = true`` 之后**立刻**就要触发
        #    请求 —— 因为 ``_trRefresh`` 的 5 秒轮询会拿服务端的权威值
        #    （false）把它拉回去 ⇒ 那样就测不到「拒收 ⇒ 停手」了。
        #
        # ⚠️ ``_trPending`` 里带上 ``enabled`` ⇒ 轮询**不会**覆盖本地值
        #    （这正是「页面里的勾还开着、服务端已经关了」的真实状态）。
        page.evaluate("""() => {
            const w = window.Qwen3Asr;
            if (!w) return false;
            w._tr.enabled = true;
            w._trPending = { enabled: true, at: Date.now() };
            return true;
        }""")
        before = page.evaluate(
            "() => !!window.Qwen3Asr && window.Qwen3Asr._tr.enabled")
        check("前置：前端以为「实时翻译开着」", before is True, f"得到 {before!r}")

        # ⚠️ 造一条**待翻的行** ⇒ 前端会来要它 ⇒ 拿到**拒收** ⇒ 触发停手。
        #
        # ⚠️ **打桩时间轴**（而不是伪造 ``rounds``）—— 我们要验的是
        #    「拒收 ⇒ 停手」，**不是**「时间轴怎么排」⇒ 直接给一行。
        #
        # ⚠️ **直接调 ``_trRequestLine``**（而不是 ``_trRequestLines``）——
        #    后者有两道闸（``_trReqGateAt`` 节流 + 「这行还没定稿」判定）
        #    ⇒ 在这个**只验拒收→停手**的脚本里反而会让请求发不出去。
        page.evaluate("""() => {
            const w = window.Qwen3Asr;
            w._trMode = 'line';
            // ⚠️ 行 id 的格式是 ``<epoch>:<行号>:<…>`` ⇒ 槽位取前两段。
            w._buildSubTimeline = () => ({
              lines: [{ id: '0:0:t', start: 0, end: 10,
                        words: [{ text: '测试句子' }] }],
            });
            // ⚠️ 源语得认出来，否则前端不会要（见 ``_trSrcLang``）。
            w.snapshot = { language: 'zh-CN' };
            w._tr.modelReady = true;
            w._trRequestLine({ id: '0:0:t', start: 0, end: 10 },
                             '0:0', '测试句子');
        }""")

        # ⚠️ **观察窗口** —— 第一次请求会拿到拒收 ⇒ 触发停手。
        page.wait_for_timeout(4000)
        first = hits["n"]
        check("前端确实发过行级翻译请求（风暴的度量）", first >= 1,
              f"窗口内 {first} 次")

        # ⚠️ **再观察一段** ⇒ 请求数**不该再增长**。
        page.wait_for_timeout(6000)
        second = hits["n"]
        check("收到拒收后**不再发新请求**（停手）", second == first,
              f"{first} → {second}（又发了 {second - first} 次）")

        after = page.evaluate("""() => {
            const w = window.Qwen3Asr;
            return {
                enabled: !!w._tr.enabled,
                checked: (() => {
                    const cb = document.getElementById('asr-tr-enabled');
                    return cb ? !!cb.checked : null;
                })(),
            };
        }""")
        check("本地开关被拉回服务端的值（enabled=false）",
              after.get("enabled") is False, f"得到 {after}")
        check("勾选框也被同步（用户看得见服务端把它关了）",
              after.get("checked") is False, f"得到 {after}")

        browser.close()


def _restore_translate() -> None:
    """把翻译开关恢复原样（**必须无条件执行**）。"""

    # ⚠️⚠️ **必须恢复**（2026-11-26 实测踩坑）—— 这个脚本把服务端的翻译
    # 开关**关掉**来验证「关着时的行为」⇒ 如果**不恢复** ⇒
    # **后面跑的任何脚本都继承「翻译是关的」这个状态** ⇒
    #   * 连跑两轮时，第二轮的**前置状态**就变了 ⇒ 本脚本自己都挂
    #     （实测第 2 轮 3/9 失败）；
    #   * 别的脚本（``verify_asr_wizard_translate`` 等）会以为
    #     「翻译模型没在跑」⇒ 去**停掉 / 重启模型** ⇒ **拖慢整轮**。
    #
    # ⚠️ **放在 ``finally`` 里**（2026-11-26 补）—— 之前它挂在 ``main``
    #   的末尾 ⇒ 脚本**中途抛异常**（浏览器崩了 / 断言炸了）⇒ 恢复被跳过
    #   ⇒ 翻译开关留在「关」的状态 ⇒ **污染后面的脚本**。
    try:
        api("POST", "/api/qwen3-asr/translate/settings",
            {"enabled": bool(_ORIG_ENABLED)})
        time.sleep(0.5)
        _st, _body = api("GET", "/api/qwen3-asr/translate/status")
        print("   翻译开关已恢复为 %r" % _body.get("enabled"))
    except Exception as exc:  # noqa: BLE001
        print("   ⚠️ 恢复翻译开关失败: %s" % exc)


def main() -> int:
    print("\033[1m「翻译没开」⇒ 前端立刻停手（2026-11-26）\033[0m")

    # ⚠️ **先记下原值**（在关它**之前**）⇒ 收尾时恢复原样。
    global _ORIG_ENABLED
    st, body = api("GET", "/api/qwen3-asr/translate/status")
    _ORIG_ENABLED = bool(body.get("enabled"))

    try:
        _run_checks()
    finally:
        _restore_translate()

    print("\n" + "=" * 62)
    if _FAIL:
        print("\033[31mFAILED\033[0m  %d 通过 / %d 失败" % (_PASS, _FAIL))
        return 1
    print("\033[32mALL PASS\033[0m  %d 条断言全过" % _PASS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
