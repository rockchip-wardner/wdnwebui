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

  /* 3. 注入「返回首页」按钮到所有非首页 */
  function _injectBackHome() {
    document
      .querySelectorAll(".page:not(#page-apps) .page-header")
      .forEach((header) => {
        if (header.querySelector(".back-home-btn")) return;

        /* 没有 .header-meta 时创建，把非标题子元素移进去；
         * 让 page-header 保持 2 个 flex 项目（h2 + meta），
         * space-between 对齐：h2 左 / meta 右，refresh 不再跑到中间。 */
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
      });
  }

  function _onReady() {
    _initSidebar();
    _injectBackHome();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _onReady);
  } else {
    _onReady();
  }
})();