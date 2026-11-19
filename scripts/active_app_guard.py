#!/usr/bin/env python3
"""拦住浏览器回归脚本对 **active-app** 的写操作 —— 脚本不该改生产配置。

背景
====

``DELETE /api/compute/active-app`` 会**连带清空** ``compute.autostart``
（见 ``app/core/compute/registry.py::clear_active_app``）—— 这是产品
语义：「退出应用」=「我不在这张卡的模式里了，也不想让这些算力下次
开机自己起来」。

``PUT /api/compute/active-app`` 则会把承载该卡 caps 的
``(model, npu_index)`` **增量追加**进 ``compute.autostart``。

⇒ 任何用浏览器驱动前端、并在卡之间切换的回归脚本，都会**改到设备级
的** ``config.jsonc``。跑一次脚本就可能把板子的开机自启清空 ——
2026-11-10 实测踩到（跑完浏览器回归后 ``config.jsonc`` 的
``compute.autostart`` 变成 ``[]``，下次重启板子没有任何算力自启）。

⇒ AGENTS.md 早就写了「回归脚本必须 mock ``PUT``/``DELETE
/api/compute/active-app``」。历史上 ``verify_usb_hid_wizard.py`` /
``verify_wizard_image_field.py`` / ``verify_web_translate_wizard_e2e.py``
做的是「真调一次 DELETE」—— 那**同样**会清空 autostart，不算 mock。
2026-11-19 改成用 :func:`install_active_app_spoof`（前端侧伪装 GET），
服务端配置不再被碰。

做法
====

**1. 写方法 → 在 BrowserContext 层拦下**（:func:`install`）：

* 前端拿到 200 ⇒ 不报 pageerror，脚本断言不受影响；
* 请求**根本没到服务端** ⇒ 生产配置原样不动。

**只拦写方法** —— ``GET`` 必须放行：``bootRestore()`` 靠它读
``active_app`` 来决定开机跳哪张卡，拦掉会让依赖开机恢复的脚本
（``verify_boot_restore*.py``）行为失真。

**2. 「不想被开机恢复跳到上一张卡」→ 在页面里伪装 GET**
（:func:`install_active_app_spoof`）：

``bootRestore()`` 开机时读到非空 active_app ⇒ 把浏览器跳到**上一张用过
的卡** ⇒ 回归脚本看到的向导事件是**别的卡**的 ⇒ 假失败。

以前的做法是发真的 ``DELETE /api/compute/active-app`` ⇒ **连带清空
``compute.autostart``** ⇒ 跑一次回归，板子的开机自启就没了。

⇒ 改成**只在前端骗它**：服务端配置一个字节都不动。

⚠️ **别用「备份 + 跑完写回磁盘」来还原** —— 服务进程内存里的
``compute`` section 才是权威：它每次 ``set_section("compute", ...)`` 都
先重读磁盘（``ConfigStore._merge_disk``），但随后用**调用方手里的旧
快照**覆盖该 section ⇒ 外部脚本写回磁盘的内容会被服务**下一次写配置
吃掉**（前端每秒 PUT 一次 active-app ⇒ 几秒内就被覆盖回 1 条）。
⇒ 要么让服务端自己写，要么**根本不碰**（本模块的 spoof）。

用法
====

::

    from active_app_guard import (
        install as install_active_app_guard,
        install_active_app_spoof,
    )

    ctx = browser.new_context(...)
    install_active_app_guard(ctx)          # ← 必须在 new_page() 之前
    install_active_app_spoof(ctx)          # ← 同上
    page = ctx.new_page()
"""
from __future__ import annotations

#: 匹配 active-app 端点（含 query string）。
ACTIVE_APP_GLOB = "**/api/compute/active-app*"

#: 只拦这些方法 —— GET/HEAD/OPTIONS 放行（读操作不伤配置）。
_WRITE_METHODS = frozenset({"PUT", "POST", "PATCH", "DELETE"})

_NOOP_BODY = '{"ok": true, "active_app": null}'

#: 在页面里把 ``GET /api/compute/active-app`` 伪装成「没有 active_app」。
#:
#: 为什么需要它
#: ------------
#: 前端 ``bootRestore()`` 开机时读一次 active_app：非空 ⇒ 把浏览器直接
#: 跳到**上一张用过的卡**。回归脚本要测的是**自己那张**卡 ⇒ 上那张卡会
#: 先挂上向导再被卸载 ⇒ 脚本看到的「第一个向导事件」是别的卡的
#: ``wizard:unmounted`` ⇒ **假失败**。
#:
#: 以前的做法是发真的 ``DELETE /api/compute/active-app`` ⇒ **后端会连带
#: 清空 ``compute.autostart``**（产品语义：退出应用 = 这些算力下次开机
#: 别自己起来）⇒ 跑一次回归，板子的开机自启就没了。
#:
#: ⇒ 改成**只在前端骗它**：服务端配置一个字节都不动。
_SPOOF_ACTIVE_APP_JS = """
(function () {
  const orig = window.fetch;
  window.fetch = function (input, init) {
    try {
      const url = (typeof input === "string") ? input
                : ((input && input.url) || "");
      const method = ((init && init.method) ||
                      (input && input.method) || "GET").toUpperCase();
      if (method === "GET" && /\\/api\\/compute\\/active-app/.test(url)) {
        return Promise.resolve(new Response(
          JSON.stringify({ active_app: null }),
          { status: 200, headers: { "Content-Type": "application/json" } }));
      }
    } catch (_) { /* 放行原请求 */ }
    return orig.apply(this, arguments);
  };
})();
"""


def _handler(route) -> None:
    """写方法 → 吞掉并回 200；读方法 → 放行。"""
    if route.request.method.upper() not in _WRITE_METHODS:
        route.continue_()
        return
    route.fulfill(status=200, content_type="application/json",
                  body=_NOOP_BODY)


def install(ctx) -> None:
    """在 **BrowserContext** 上装拦截。

    必须在 ``ctx.new_page()`` **之前**调用 —— context 上装的规则对其
    后续创建的所有 page 生效。
    """
    ctx.route(ACTIVE_APP_GLOB, _handler)


def install_active_app_spoof(ctx) -> None:
    """让页面里的 ``GET /api/compute/active-app`` 恒返回「空」。

    用途：阻止前端 ``bootRestore()`` 把浏览器跳到上一张用过的卡 ⇒
    回归脚本看到的向导事件是**自己那张卡**的，不会假失败。

    **服务端配置完全不动** —— 不发 DELETE，也就不会连带清空
    ``compute.autostart``（那才是「跑一次回归就毁掉板子开机自启」的根因）。

    必须在 ``ctx.new_page()`` **之前**调用（``add_init_script`` 对之后
    创建的 page 生效）。

    建议同时调 :func:`install` 拦掉 PUT/DELETE —— 脚本期间万一触发了
    ``_exit()``（用户点 back-home），那也是一次 DELETE ⇒ 同样清空
    autostart。
    """
    ctx.add_init_script(_SPOOF_ACTIVE_APP_JS)


def install_on_page(page) -> None:
    """直接给 **page** 装拦截。

    ⚠️ Playwright 1.49 的 ``Browser`` **没有** ``route()`` 方法
    （只有 ``BrowserContext.route`` 和 ``Page.route``）。而
    ``browser.new_page()`` 会隐式创建一个 context —— 拿不到那个
    context ⇒ 这种脚本只能给 page 装。

    ⚠️ context 上的规则**不会**覆盖 ``browser.new_page()`` 建的独立
    page（Playwright 直接开页时没有独立 context）。⇒ 每个 page 都要
    单独装。
    """
    page.route(ACTIVE_APP_GLOB, _handler)
