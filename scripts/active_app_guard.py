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
/api/compute/active-app``」，但一直只有 ``verify_usb_hid_wizard.py``
做了「真调一次 DELETE」—— 那**同样**会清空 autostart，不算 mock。

做法
====

在 Playwright 的 **BrowserContext** 层拦下这两个写方法，直接返回 200：

* 前端拿到 200 ⇒ 不报 pageerror，脚本断言不受影响；
* 请求**根本没到服务端** ⇒ 生产配置原样不动。

**只拦写方法** —— ``GET`` 必须放行：``bootRestore()`` 靠它读
``active_app`` 来决定开机跳哪张卡，拦掉会让依赖开机恢复的脚本
（``verify_boot_restore*.py``）行为失真。

用法
====

::

    from active_app_guard import install as install_active_app_guard

    ctx = browser.new_context(...)
    install_active_app_guard(ctx)      # ← 必须在 new_page() 之前
    page = ctx.new_page()
"""
from __future__ import annotations

#: 匹配 active-app 端点（含 query string）。
ACTIVE_APP_GLOB = "**/api/compute/active-app*"

#: 只拦这些方法 —— GET/HEAD/OPTIONS 放行（读操作不伤配置）。
_WRITE_METHODS = frozenset({"PUT", "POST", "PATCH", "DELETE"})

_NOOP_BODY = '{"ok": true, "active_app": null}'


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
