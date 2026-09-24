/* base.js — 新增功能覆盖层（按方案 A：不修改 app.js，独立文件）
 *
 * 加载顺序：必须在 app.js 之后（defer），覆盖 app.js 顶层定义。
 * 不引入新的顶层 const，全部用 IIFE 包裹。
 */

(function () {
  "use strict";

  /* 1. 覆盖 router 默认值：
   *    "" / "#" / "#/" → "apps"（新首页）；"#xxx" → "xxx"；"#/xxx" → "xxx"
   *    原 app.js: const DEFAULT_PAGE = "system-monitor";
   */
  window.currentPage = function () {
    const hash = location.hash.replace(/^#\/?/, "");
    return hash || "apps";
  };

  /* 立即切到 apps：app.js 顶层默认切到 system-monitor，立即纠正避免闪屏
   * 触发条件：hash 为空 / "#" / "#/"（含"返回首页"按钮后的状态） */
  function _forceAppsImmediate() {
    const h = location.hash;
    if (h && h !== "#" && h !== "#/") return;
    document.querySelectorAll(".page").forEach((p) => p.classList.add("hidden"));
    const apps = document.getElementById("page-apps");
    if (apps) apps.classList.remove("hidden");
  }
  _forceAppsImmediate();

  /* 2. sidebar hover 展开 / 收起（默认折叠 64px；hover 展开；离开发知 250ms 收起） */
  const HOVER_LEAVE_DELAY = 250;

  function _initSidebar() {
    const sidebar = document.querySelector(".sidebar");
    if (!sidebar) return;
    sidebar.classList.add("collapsed");

    let hoverTimer = null;
    sidebar.addEventListener("mouseenter", () => {
      if (hoverTimer) clearTimeout(hoverTimer);
      hoverTimer = null;
      sidebar.classList.remove("collapsed");
    });
    sidebar.addEventListener("mouseleave", () => {
      if (hoverTimer) clearTimeout(hoverTimer);
      hoverTimer = setTimeout(
        () => sidebar.classList.add("collapsed"),
        HOVER_LEAVE_DELAY
      );
    });
  }

  /* 3. 注入"返回首页 / 退出应用"按钮到 demo 页（DemoGuard._registry 内）；
   *    6 个系统设置页不再注入（UI 已删除）。文案按 _activeApp 切换：
   *      当前页活跃 + 后端在跑 → "退出应用"（红框变体）；否则 "返回首页"。
   */
  function _injectBackHome() {
    document.querySelectorAll(".back-home-btn").forEach((btn) => btn.remove());
    if (typeof DemoGuard === "undefined") return;

    DemoGuard._demoPages().forEach((page) => {
      const pageEl = document.getElementById("page-" + page);
      if (!pageEl) return;
      const header = pageEl.querySelector(".page-header");
      if (!header || header.querySelector(".back-home-btn")) return;

      let meta = header.querySelector(".header-meta");
      if (!meta) {
        meta = document.createElement("div");
        meta.className = "header-meta";
        Array.from(header.children).forEach((child) => {
          if (!child.matches("h1, h2, h3, h4, h5")) meta.appendChild(child);
        });
        header.appendChild(meta);
      }

      const back = document.createElement("a");
      back.href = "#/";
      back.className = "back-home-btn";
      back.setAttribute("role", "button");
      back.setAttribute("aria-label", "返回首页");
      back.innerHTML =
        '<span class="back-home-icon" aria-hidden="true">←</span> 返回首页';
      meta.appendChild(back);

      back.addEventListener("click", async (e) => {
        if (typeof DemoGuard === "undefined") return;
        if (!DemoGuard._isDemoPage(currentPage())) return;
        if (!DemoGuard.isActive(currentPage())) return;
        e.preventDefault();
        const cur = currentPage();
        await DemoGuard.stopAndClear(cur);
        DemoGuard._keptPages.delete(cur);
        DemoGuard._activeApp = null;
        location.hash = "#/";
      });
    });
  }

  /* 4. 应用面板按钮（sidebar 左上 .nav-item[data-page="apps"]）：
   *    无活跃应用 → #/ ；有活跃应用 → 跳回该应用页。
   */
  function _wireAppsPanel() {
    const appsNav = document.querySelector('.nav-item[data-page="apps"]');
    if (!appsNav) return;
    appsNav.addEventListener("click", (e) => {
      if (typeof DemoGuard === "undefined") return;
      e.preventDefault();
      DemoGuard._refreshActive();
      const target = DemoGuard._activeApp || "apps";
      if (currentPage() === target) return;
      location.hash = "#" + target;
    });
  }

  /* 5. 退出应用 / 返回首页按钮文字 + data-mode 切换 */
  function _updateBackHomeLabels() {
    if (typeof DemoGuard === "undefined") return;
    const cur = currentPage();
    const isDemo = DemoGuard._isDemoPage(cur);
    const active = DemoGuard.isActive(cur);
    document.querySelectorAll(".back-home-btn").forEach((btn) => {
      if (isDemo && active) {
        btn.dataset.mode = "exit";
        btn.setAttribute("aria-label", "退出应用");
        btn.innerHTML =
          '<span class="back-home-icon" aria-hidden="true">⏏</span> 退出应用';
      } else if (btn.dataset.mode === "exit") {
        btn.dataset.mode = "";
        btn.setAttribute("aria-label", "返回首页");
        btn.innerHTML =
          '<span class="back-home-icon" aria-hidden="true">←</span> 返回首页';
      }
    });
  }

  function _onReady() {
    _initSidebar();
    _injectBackHome();
    _wireAppsPanel();
    _updateBackHomeLabels();
  }

  window.addEventListener("hashchange", () => {
    if (typeof DemoGuard === "undefined") return;
    DemoGuard._refreshActive();
    _updateBackHomeLabels();
  });

  window.addEventListener("demoguard:active-change", () => {
    _updateBackHomeLabels();
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _onReady);
  } else {
    _onReady();
  }
})();