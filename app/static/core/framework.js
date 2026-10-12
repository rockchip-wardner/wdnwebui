/* framework.js — core chrome loaded eagerly on every page.
 *
 * Responsibilities:
 *   - Read window.__APP_MANIFESTS__ (server-injected at boot) and build
 *     the app-grid for #page-apps + the sidebar.
 *   - DemoGuard: register / unregister / report — card app.js files
 *     call DemoGuard.register(id, {active, stop, statusPoll}) when they
 *     load. beforeunload + hashchange send stop_sync / await stop().
 *   - Sidebar hover collapse (kept from old base.js).
 *   - "Return to home" button injection (kept from old base.js).
 *   - Router: hash-based page navigation, lazy-loads card JS on first
 *     navigation.
 *
 * Card-specific JS lives in app/apps/<id>/static/app.js — it defines its
 * own DOM logic + calls window.DemoGuard.register(id, ...) at the end.
 * The framework never reads card JS; cards never touch framework internals
 * other than DemoGuard.
 */

(function () {
  "use strict";

  // ─────────────────────────────────────────────────────────────────────
  // DemoGuard — declared first so cards can register as soon as their
  // JS loads. The contract is documented in apps/<id>/manifest.json
  // and in plan.md §5.
  // ─────────────────────────────────────────────────────────────────────

  const DemoGuard = {
    _registry: {},      // { pageId: {active(snap) -> bool, stop() -> Promise, statusPoll?() -> void} }
    _cache: {},         // latest snap per page (set via DemoGuard.report(page, snap))
    _keptPages: new Set(),
    _activeApp: null,
    _loadedApps: new Set(),   // pages whose app.js has been loaded
    _wizardMounted: new Set(),// pages whose wizard is currently mounted。
                              // 与 _loadedApps 解耦：unmount 只清本标记，
                              // 卡片 JS 不再重跑 → 事件监听器不累积 / 模块
                              // 状态不丢（修复 confirm 重复 + 进度条切页复活）
    _loading: {},             // promise per page to dedupe lazy-loads

    register(pageId, handlers) {
      if (!handlers || typeof handlers.active !== "function") {
        console.warn("[DemoGuard] register(" + pageId + ") missing active()");
        return;
      }
      this._registry[pageId] = handlers;
      if (typeof handlers.label === "string") {
        this._labels[pageId] = handlers.label;
      }
    },

    unregister(pageId) {
      delete this._registry[pageId];
      delete this._cache[pageId];
      this._loadedApps.delete(pageId);
    },

    _demoPages() { return Object.keys(this._registry); },

    _isDemoPage(p) {
      return Object.prototype.hasOwnProperty.call(this._registry, p);
    },

    isActive(page) {
      const reg = this._registry[page];
      if (!reg) return false;
      try {
        return !!reg.active.call(this, this._cache[page] || {});
      } catch (_e) {
        return false;
      }
    },

    report(page, snap) {
      this._cache[page] = snap || {};
      this._refreshActive();
    },

    _refreshActive() {
      let active = null;
      for (const p of this._demoPages()) {
        if (this.isActive(p)) { active = p; break; }
      }
      const prev = this._activeApp;
      this._activeApp = active;
      if (prev !== active) {
        try {
          window.dispatchEvent(new CustomEvent("demoguard:active-change", {
            detail: { active: active, previous: prev },
          }));
        } catch (_) { /* old browser */ }
      }
      return active;
    },

    stopSync(page) {
      const reg = this._registry[page];
      if (!reg || typeof reg.stopSync !== "function") return;
      try {
        reg.stopSync();
      } catch (_) { /* beforeunload path, best-effort */ }
      // 发完 stop 立即清掉该 page 的 cache + 刷新 _activeApp。否则
      // _cache[page] 里的 stale session.state (e.g. "playing") 会让
      // _refreshActive 把 _activeApp 钉在上一个 demo,影响下游:
      //   * wireAppsPanel — 应用列表 button 把 stale _activeApp 当目标,
      //     误把用户带回去 (典型:从 音视频描述 切到 视频目标定位 完成配置
      //     后,点应用列表 → 跳回 音视频描述 wizard)。
      //   * updateBackHomeLabels — 错把当前页 back button 标 "退出应用"。
      // 与 stopAndClear 的语义对齐(异步 stop + 清 cache),只是这里是
      // 同步 sendBeacon / fetch keepalive 路径,不 await server reply。
      this._cache[page] = {};
      this._refreshActive();
    },

    async stopAndClear(page) {
      const reg = this._registry[page];
      if (!reg) return "stop";
      try {
        await reg.stop();
      } catch (_) { /* ignore */ }
      this._cache[page] = {};
      this._keptPages.delete(page);
      this._refreshActive();
    },

    _labels: {},
  };

  window.DemoGuard = DemoGuard;

  // ─────────────────────────────────────────────────────────────────────
  // Apps API — utility for cards to register optional callbacks
  // (onPageEnter / onPageLeave / helpers). The original apps.js exposed
  // setCardImage / setCardVideo; the latter is no longer needed since
  // manifest.preview_video is the single source of truth.
  // ─────────────────────────────────────────────────────────────────────

  window.Apps = window.Apps || {};

  // ─────────────────────────────────────────────────────────────────────
  // Router — hash-based navigation, lazy-loads card JS on first visit.
  // ─────────────────────────────────────────────────────────────────────

  const ROUTER = {
    currentPage() {
      const h = (location.hash || "").replace(/^#\/?/, "");
      return h || "apps";
    },

    isCardPage(page) {
      const manifests = (window.__APP_MANIFESTS__ && window.__APP_MANIFESTS__.cards) || [];
      return manifests.some((m) => m.id === page);
    },

    async navigate(page) {
      if (page === this.currentPage()) return;
      const guarded = await this._maybeConfirmLeave(this.currentPage());
      if (guarded === "cancel") {
        // User cancelled — don't navigate. Force hash back.
        const cur = this.currentPage();
        history.replaceState(null, "", "#" + cur);
        return false;
      }
      location.hash = "#" + page;
      return true;
    },

    async _maybeConfirmLeave(fromPage) {
      if (!DemoGuard._isDemoPage(fromPage)) return "stop";
      if (!DemoGuard.isActive(fromPage)) return "stop";
      // Show a small confirm dialog before tearing down.
      const name = DemoGuard._labels[fromPage] || fromPage;
      const ok = window.confirm(
        "【" + name + "】后台正在运行。离开此页面将自动停止当前任务并释放算力。"
      );
      if (!ok) return "cancel";
      try {
        await DemoGuard.stopAndClear(fromPage);
      } catch (_) { /* ignore */ }
      return "stop";
    },

    showPage(page) {
      document.querySelectorAll(".page").forEach((p) => {
        p.classList.toggle("hidden", p.dataset.page !== page);
      });
      document.querySelectorAll(".nav-item").forEach((n) => {
        n.classList.toggle("active", n.dataset.page === page);
      });
      // B 方案:卡片进入前先 dismiss 残留 loading,再按需重挂(B 方案关键)。
      _dismissCardLoading();
      if (this.isCardPage(page)) {
        if (!DemoGuard._loadedApps.has(page)) {
          _prepareCardEntry(page);
          this._loadCardJS(page);
        } else if (!DemoGuard._wizardMounted.has(page)
                   && window.WizardRegistry?.tryMount) {
          // 卡片 JS 已加载但向导未挂载（用户切走触发了 unmount）→ 只重挂
          // 向导，**不**重新注入 <script>：否则卡片 IIFE 重跑，复用的 DOM
          // 上事件监听器累积、模块状态归零（用户实测：清空缓存 confirm
          // 重复 / 进度条切页复活 / 反查按钮闪烁）。
          _prepareCardEntry(page);
          const m = (window.__APP_MANIFESTS__?.cards || [])
            .find((x) => x.id === page);
          if (m) {
            window.WizardRegistry.tryMount(m);
            DemoGuard._wizardMounted.add(page);
          }
        }
      }
      if (DemoGuard._isDemoPage(page)) {
        DemoGuard._refreshActive();
      }
      // 切换页面后把共享滚动容器复位到顶部（.content 是 overflow-y:auto
      // 的滚动区；hash 导航不会自动复位，切到长页面时会停在上一页的
      // 滚动位置）。
      const scroller = document.querySelector("main.content");
      if (scroller) scroller.scrollTop = 0;
      window.scrollTo(0, 0);
    },

    async _loadCardJS(page) {
      const manifests = window.__APP_MANIFESTS__ && window.__APP_MANIFESTS__.cards || [];
      const m = manifests.find((x) => x.id === page);
      if (!m) return Promise.resolve();
      // preload 在途时不早退:等它跑完再补挂向导。早退会让本页永远等不到
      // wizard(而 _prepareCardEntry 已把 page 藏了 → 白屏)。
      if (DemoGuard._loading[page]) {
        try { await DemoGuard._loading[page]; } catch (_) { return; }
        if (!DemoGuard._wizardMounted.has(page) && window.WizardRegistry?.tryMount) {
          window.WizardRegistry.tryMount(m);
          DemoGuard._wizardMounted.add(page);
        }
        return;
      }
      const jsList = (m.frontend && m.frontend.js) || [];
      const chain = jsList.reduce((p, url) => {
        return p.then(() => new Promise((resolve, reject) => {
          const s = document.createElement("script");
          s.src = url;
          s.defer = false;
          s.onload = resolve;
          s.onerror = () => reject(new Error("Failed to load " + url));
          document.head.appendChild(s);
        }));
      }, Promise.resolve());
      DemoGuard._loading[page] = chain;
      try {
        await chain;
        DemoGuard._loadedApps.add(page);
        // v0.4 向导模式:挂载 wizard(读 m.wizard_config;缺失 → 零开销跳过)。
        // 不维护"已注册 wizard 应用"清单,完全 manifest 字段驱动。
        if (window.WizardRegistry?.tryMount) {
          window.WizardRegistry.tryMount(m);
          DemoGuard._wizardMounted.add(page);
        }
      } catch (e) {
        console.error("[router] failed to load card JS:", e);
      } finally {
        delete DemoGuard._loading[page];
      }
    },
  };

  // ─────────────────────────────────────────────────────────────────────
  // B 方案:卡片首次进入的 loading 占位,彻底消除"先到功能页、过几秒才到
  // wizard"的闪窗。详见 state.js:mount 末尾的 wizard:mounted 事件。
  // ─────────────────────────────────────────────────────────────────────
  let _cardLoadingTimeout = null;

  function _prepareCardEntry(page) {
    const m = (window.__APP_MANIFESTS__?.cards || [])
      .find((x) => x.id === page);
    if (!m || !m.wizard_config) return;
    const pageSection = document.getElementById("page-" + page);
    if (pageSection) pageSection.classList.add("hidden");
    const overlay = document.getElementById("wd-loading-section");
    if (overlay) {
      const text = overlay.querySelector("#wd-loading-text");
      if (text) text.textContent = `正在加载 ${m.title || page}…`;
      overlay.classList.remove("hidden");
    }
    if (_cardLoadingTimeout) clearTimeout(_cardLoadingTimeout);
    // 30s 兜底:tryMount 失败 / wizard.json 404 / mount 抛错 等场景不会
    // 触发 wizard:mounted 事件,这里超时清掉 loading 让用户能进功能页。
    _cardLoadingTimeout = setTimeout(() => {
      _cardLoadingTimeout = null;
      const o = document.getElementById("wd-loading-section");
      if (o && !o.classList.contains("hidden")) {
        o.classList.add("hidden");
        if (currentPage() === page) {
          const ps = document.getElementById("page-" + page);
          if (ps) ps.classList.remove("hidden");
        }
      }
    }, 30000);
  }

  function _dismissCardLoading() {
    if (_cardLoadingTimeout) {
      clearTimeout(_cardLoadingTimeout);
      _cardLoadingTimeout = null;
    }
    const overlay = document.getElementById("wd-loading-section");
    if (overlay) overlay.classList.add("hidden");
  }

  window.addEventListener("wizard:mounted", () => {
    _dismissCardLoading();
  });

  // 向导卸载时只清 _wizardMounted 标记，**不**动 _loadedApps —— 否则
  // ROUTER 认为卡片未加载、重新注入 <script>（IIFE 重跑 → 监听器在复用
  // DOM 上累积、模块状态归零）。下次切回该页时 showPage 看到
  // _loadedApps.has && !_wizardMounted.has 会只调 tryMount 重挂向导
  // （"跳过向导后再进入会重弹"的语义保持不变）。wizard.js 在 unmount()
  // 时 dispatch 此事件。
  window.addEventListener("wizard:unmounted", (e) => {
    const appId = e?.detail?.appId;
    if (appId) DemoGuard._wizardMounted.delete(appId);
  });

  window.ROUTER = ROUTER;

  // ─────────────────────────────────────────────────────────────────────
  // Sidebar hover collapse + brand visibility
  // ─────────────────────────────────────────────────────────────────────

  function initSidebar() {
    const sidebar = document.querySelector(".sidebar");
    if (!sidebar) return;
    sidebar.classList.add("collapsed");
    let hoverTimer = null;

    // mouseenter / mouseleave do NOT bubble, so listening on the parent
    // .sidebar only fires when the cursor enters/leaves the *exact* sidebar
    // bounding box. Children like .nav-item / .sidebar-foot block that
    // event propagation. Use mouseover / mouseout (which bubble) and check
    // relatedTarget so we react to child entries but ignore internal moves.
    const expand = () => {
      if (hoverTimer) clearTimeout(hoverTimer);
      hoverTimer = null;
      sidebar.classList.remove("collapsed");
    };
    const collapseSoon = () => {
      if (hoverTimer) clearTimeout(hoverTimer);
      hoverTimer = setTimeout(() => sidebar.classList.add("collapsed"), 250);
    };
    sidebar.addEventListener("mouseover", (e) => {
      if (!sidebar.contains(e.relatedTarget)) expand();
    });
    sidebar.addEventListener("mouseout", (e) => {
      // relatedTarget === null when cursor leaves the window entirely
      if (e.relatedTarget === null || !sidebar.contains(e.relatedTarget)) {
        collapseSoon();
      }
    });
  }

  // ─────────────────────────────────────────────────────────────────────
  // App panel — render the #page-apps grid from __APP_MANIFESTS__.cards
  // ─────────────────────────────────────────────────────────────────────

  function renderAppPanel() {
    const grid = document.querySelector("#page-apps .apps-grid");
    if (!grid) return;
    const data = window.__APP_MANIFESTS__ || { cards: [] };
    // ``hidden: true`` 的卡片从首页应用网格消失 —— 代码 / API / 静态
    // 资源 / 直接 URL #/<id> 都仍可用,只是不在入口网格中渲染。用于
    // "先下线 / 等版本升级"的过渡期,manifest 里一行 ``"hidden": true``
    // 即可,不需要改 framework.js。
    // 渲染顺序按 manifest 的 ``display_order`` 升序(小→上左);未填或
    // 相同者按 id 字典序兜底,确保布局稳定。
    const cards = (data.cards || [])
      .filter((m) => m.status === "ok" && m.hidden !== true)
      .sort((a, b) => {
        const ao = Number.isFinite(a.display_order) ? a.display_order : 9999;
        const bo = Number.isFinite(b.display_order) ? b.display_order : 9999;
        if (ao !== bo) return ao - bo;
        return String(a.id).localeCompare(String(b.id));
      });
    const html = cards.map((m) => {
      const preview = m.preview_video
        ? `<video class="app-card-video" autoplay muted playsinline preload="metadata" loop
                src="${m.preview_video}" aria-hidden="true"></video>`
        : "";
      // description 是一句话功能说明,缺失则不渲染标签
      const desc = m.description
        ? `<p class="app-card-desc">${escapeHtml(m.description)}</p>`
        : "";
      return `<a href="#${m.id}" class="app-card" data-page="${m.id}">
        <div class="app-card-placeholder">${preview}</div>
        <h2 class="app-card-title">${escapeHtml(m.title)}</h2>
        ${desc}
      </a>`;
    }).join("");
    grid.innerHTML = html;
    grid.addEventListener("click", (ev) => {
      const a = ev.target.closest(".app-card");
      if (!a) return;
      ev.preventDefault();
      ROUTER.navigate(a.dataset.page);
    });
    _preloadCardScripts();
  }

  // 预热所有卡片 JS,让首次点击直接命中 _loadedApps,避免 showPage 同步
  // 切 page 可见性 + _loadCardJS 异步加载之间的"先到功能页"竞态。
  // **不**调 tryMount —— 否则 6 个 wizard 会在 boot 时全部自动弹出。
  let _cardsPreloadStarted = false;
  function _preloadCardScripts() {
    if (_cardsPreloadStarted) return;
    _cardsPreloadStarted = true;
    const data = window.__APP_MANIFESTS__ || { cards: [] };
    for (const card of (data.cards || [])) {
      if (!card || !card.id) continue;
      if (DemoGuard._loadedApps.has(card.id)) continue;
      const jsList = (card.frontend && card.frontend.js) || [];
      if (!jsList.length) continue;
      // 串行加载,与 _loadCardJS 保持一致(避免并行 race)
      const chain = jsList.reduce((p, url) => {
        return p.then(() => new Promise((resolve, reject) => {
          const s = document.createElement("script");
          s.src = url;
          s.defer = false;
          s.dataset.cardPreload = card.id;
          s.onload = () => resolve();
          s.onerror = () => {
            console.warn("[router] card preload failed:", card.id, url);
            reject(new Error("Failed to load " + url));
          };
          document.head.appendChild(s);
        }));
      }, Promise.resolve());
      // 必须挂进 DemoGuard._loading —— 否则 preload 期间用户点进该卡片时,
      // _loadCardJS(只查 _loading / _loadedApps)会**再注入一份** <script>。
      // 卡片 IIFE 于是跑两遍,旧闭包的 WebSocket 无人 close,每条帧被两个
      // socket 各收一次 → 每条消息在页面上出现两遍。
      DemoGuard._loading[card.id] = chain;
      chain.then(() => {
        DemoGuard._loadedApps.add(card.id);
      }).catch((e) => {
        console.warn("[router] card preload chain failed for",
                     card.id, e && (e.message || e));
        // _loadedApps 不加,用户点击时会走 _loadCardJS 重试
      }).finally(() => {
        delete DemoGuard._loading[card.id];
      });
    }
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // ─────────────────────────────────────────────────────────────────────
  // "Return to home" button — injected into each card page header.
  // ─────────────────────────────────────────────────────────────────────

  function injectBackHomeButtons() {
    document.querySelectorAll(".back-home-btn").forEach((b) => b.remove());
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
      back.innerHTML = '<span class="back-home-icon" aria-hidden="true">←</span> 返回首页';
      meta.appendChild(back);
      back.addEventListener("click", async (ev) => {
        if (!DemoGuard._isDemoPage(currentPage())) return;
        if (!DemoGuard.isActive(currentPage())) return;
        ev.preventDefault();
        await DemoGuard.stopAndClear(currentPage());
        DemoGuard._activeApp = null;
        location.hash = "#/";
      });
    });
  }

  function updateBackHomeLabels() {
    const cur = currentPage();
    const isDemo = DemoGuard._isDemoPage(cur);
    const active = DemoGuard.isActive(cur);
    document.querySelectorAll(".back-home-btn").forEach((btn) => {
      if (isDemo && active) {
        btn.dataset.mode = "exit";
        btn.setAttribute("aria-label", "退出应用");
        btn.innerHTML = '<span class="back-home-icon" aria-hidden="true">⏏</span> 退出应用';
      } else if (btn.dataset.mode === "exit") {
        btn.dataset.mode = "";
        btn.setAttribute("aria-label", "返回首页");
        btn.innerHTML = '<span class="back-home-icon" aria-hidden="true">←</span> 返回首页';
      }
    });
  }

  // ─────────────────────────────────────────────────────────────────────
  // Hash navigation — router glue
  // ─────────────────────────────────────────────────────────────────────

  function currentPage() { return ROUTER.currentPage(); }

  function onHashChange() {
    const page = currentPage();
    // 导航策略(对齐 c219e71 的"应用切换策略改造"):
    //   * 目标 = 系统页(不在 _registry,也不是 home "apps")→ silent keep:
    //     当前活跃的 demo 页加进 _keptPages,保留后台运行。用户去算力
    //     配置 / 系统信息看一眼,回来时还在跑(配合"应用面板"按钮可一
    //     键回到该 demo)。
    //   * 目标 = 其他 demo 页 → 释放算力:停止之前活跃的 demo(移出
    //     _keptPages)。两个 VLM 同时跑会抢 NPU,必须 stop。
    //   * 目标 = home "apps" → 停止之前活跃的 demo(与 demo 切换同一
    //     语义)。c219e71 也是这个语义,且 home 上有 app-card 可一键
    //     回当前应用。
    // 不再无差别 stop 所有非 _keptPages 的活跃 demo —— 旧逻辑会"去系
    // 统页看一眼 = 杀后台",违反用户的预期。
    const targetIsDemo = DemoGuard._isDemoPage(page);
    const targetIsHome = page === "apps";
    DemoGuard._demoPages().forEach((p) => {
      if (p === page) return;             // 同一页 → 不动
      if (!DemoGuard.isActive(p)) return; // 没在跑 → 不动
      if (!targetIsDemo && !targetIsHome) {
        // 系统页导航 → silent keep,后台保留
        DemoGuard._keptPages.add(p);
      } else {
        // demo → demo / demo → home / home → demo → 释放算力
        DemoGuard._keptPages.delete(p);
        DemoGuard.stopSync(p);
      }
    });
    ROUTER.showPage(page);
    DemoGuard._refreshActive();
    updateBackHomeLabels();
  }

  // beforeunload — fire confirm + sendBeacon stop for active pages
  window.addEventListener("beforeunload", () => {
    let anyActive = false;
    DemoGuard._demoPages().forEach((p) => {
      if (DemoGuard.isActive(p)) {
        DemoGuard.stopSync(p);
        anyActive = true;
      }
    });
    if (anyActive) {
      // Trigger native browser warning dialog
      event.preventDefault();
      event.returnValue = "";
    }
  });

  // ─────────────────────────────────────────────────────────────────────
  // App-panel button (sidebar first item, data-page="apps")
  // ─────────────────────────────────────────────────────────────────────

  function wireAppsPanel() {
    const appsNav = document.querySelector('.nav-item[data-page="apps"]');
    if (!appsNav) return;
    appsNav.addEventListener("click", (ev) => {
      ev.preventDefault();
      const target = DemoGuard._activeApp || "apps";
      if (currentPage() === target) return;
      location.hash = "#" + target;
    });
  }

  // ─────────────────────────────────────────────────────────────────────
  // Bootstrap
  // ─────────────────────────────────────────────────────────────────────

  // 状态值集合 —— "active" 判定时,字符串字段命中这些值也算活跃。
  // 覆盖各 card manifest 里 ``active_state_keys`` 引用的所有"在跑"语义:
  // video_detection / video_target_detection / video_target_localization
  // 的 ``session.state`` ∈ {opening, playing, paused, eof};
  // text_chat / tts 的 ``session.state`` ∈ {loading, running};
  // video_analysis_search / audio_analysis / video_search
  // 的 ``service.state`` / ``vcs.state`` 同样会在跑时落入这套值。
  // 加上 ``running`` / ``loading`` 兼容 video-detection 的 audio 子状态。
  const _ACTIVE_STATE_STRINGS = new Set([
    "opening", "playing", "paused", "eof",
    "loading", "running",
  ]);

  // 走 ``active_state_keys`` 数组(``["session.state", "detector.enabled"]``)
  // 评估 ``snap`` 是否"活跃"。任一键路径取值:
  //   * 布尔 true → active
  //   * 字符串命中 _ACTIVE_STATE_STRINGS → active
  //   * 其他值 → 不 active
  // 数组里全部键都"非 active"才返回 false,任一活跃就 true
  // (—— 与旧 app.js confirmLeave 的"任一后台在跑就拦"语义一致)。
  function _evalActiveByKeys(snap, keys) {
    if (!snap || !Array.isArray(keys) || keys.length === 0) return false;
    for (const key of keys) {
      const parts = String(key).split(".");
      let v = snap;
      for (const p of parts) {
        if (v == null) { v = undefined; break; }
        v = v[p];
      }
      if (v === true) return true;
      if (typeof v === "string" && _ACTIVE_STATE_STRINGS.has(v)) return true;
    }
    return false;
  }

  // 走 ``stop_endpoints``(``[{method, path}, ...]``) 顺序 await 拉停。
  // 任一失败吞掉 —— 用户已经点了"退出",best-effort 跑完所有清理路径。
  async function _runStopEndpoints(endpoints) {
    if (!Array.isArray(endpoints) || endpoints.length === 0) return;
    for (const ep of endpoints) {
      const method = (ep && ep.method) || "POST";
      const path = (ep && ep.path) || "";
      if (!path) continue;
      try {
        await fetch(path, { method: method });
      } catch (_e) { /* beforeunload / navigate-away best-effort */ }
    }
  }

  // 走 ``stop_sync_endpoints`` 同步触发 —— ``navigator.sendBeacon``(POST
  // 支持;其他 method 降级为 fetch + keepalive)。
  function _runStopSyncEndpoints(endpoints) {
    if (!Array.isArray(endpoints) || endpoints.length === 0) return;
    for (const ep of endpoints) {
      const method = (ep && ep.method) || "POST";
      const path = (ep && ep.path) || "";
      if (!path) continue;
      try {
        if (method === "POST" && typeof navigator.sendBeacon === "function") {
          const blob = new Blob(["{}"], { type: "application/json" });
          navigator.sendBeacon(path, blob);
        } else {
          fetch(path, { method: method, keepalive: true }).catch(() => {});
        }
      } catch (_e) { /* beforeunload, ignore */ }
    }
  }

  // 扫 ``window.__APP_MANIFESTS__.cards[].demo_guard`` 自动 register。
  // 必须在 ``injectBackHomeButtons`` 之前调用 —— 后者依赖
  // ``DemoGuard._demoPages()``(== Object.keys(_registry))决定给哪些页
  // 注入"返回首页 / 退出应用"按钮。manifest 是 single source of truth,
  // 各 card 的 app.js 不用再单独 ``DemoGuard.register(...)``(只保留
  // ``DemoGuard.report(...)`` 推快照即可)。
  function _registerCardsFromManifest() {
    const manifests = (window.__APP_MANIFESTS__
                      && window.__APP_MANIFESTS__.cards) || [];
    for (const m of manifests) {
      const dg = m && m.demo_guard;
      if (!dg || !m.id) continue;
      const activeKeys = Array.isArray(dg.active_state_keys)
        ? dg.active_state_keys : [];
      const stopEps = Array.isArray(dg.stop_endpoints)
        ? dg.stop_endpoints : [];
      const syncEps = Array.isArray(dg.stop_sync_endpoints)
        ? dg.stop_sync_endpoints : stopEps;
      DemoGuard.register(m.id, {
        label:  m.title || m.id,
        active: (snap) => _evalActiveByKeys(snap, activeKeys),
        stop:   () => _runStopEndpoints(stopEps),
        stopSync: () => _runStopSyncEndpoints(syncEps),
      });
    }
  }

  function onReady() {
    initSidebar();
    renderAppPanel();
    // 先 register 再 inject —— inject 依赖 _registry 决定注入范围
    _registerCardsFromManifest();
    injectBackHomeButtons();
    wireAppsPanel();
    updateBackHomeLabels();
    onHashChange();
  }

  window.addEventListener("hashchange", () => {
    DemoGuard._refreshActive();
    updateBackHomeLabels();
    onHashChange();
  });
  window.addEventListener("demoguard:active-change", updateBackHomeLabels);

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", onReady);
  } else {
    onReady();
  }
})();
