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
  // ActivateManager — "用户最后进入的卡片页" ↔ 后端 config.jsonc
  //   * 任何"进入卡片页" → 更新 in-memory _current(= last entered card)。
  //     这就是用户视角的"我在这"——和模型 running 状态脱钩,这样:
  //       - 进了 app 还在 wizard 中(模型未 running),sidebar 应用面板
  //         也能跳回这张卡;
  //       - 切到系统页 / home 一眼再回,_current 不丢;
  //       - 只有 back-home 按钮显式 _exit() 才清。
  //   * caps 全 local running 时额外 PUT /api/compute/active-app。后端
  //     把当前承载该 app 的 (model, npu_index) 增量写进 compute.autostart
  //     —— 下次 reboot 时 worker 自动 spawn。caps 未 ready 时**不**PUT
  //     (后端会 409),但 in-memory _current 仍设;轮询 1Hz 跟进 caps
  //     状态,一旦 ready 立刻补 PUT。
  //   * 后端 reboot 后的第一次 GET 看到 active_app 非空 → 前端
  //     拿到该值后直接跳到对应 #<id>,不再让用户重新点卡片。
  //   * beforeunload 不再 DELETE —— 用户关 tab / 刷页是常见操作,不该
  //     把 "我在这" 也一起擦掉;持久化跨 tab 保持,直到用户显式 _exit。
  //
  // 与 DemoGuard 的边界:
  //   * DemoGuard 判定"页面在跑后台"(session.state in {playing,...}),
  //     控制离开时是否弹 confirm。语义是"runtime 状态"。
  //   * ActivateManager 判定"用户最后进入的卡片",决定 sidebar 应用面
  //     板的目标 & reboot 后的恢复目标。语义是"我在这"(用户视角,
  //     不看模型层)。
  // ─────────────────────────────────────────────────────────────────────

  const ActivateManager = {
    _current:   null,        // 用户最后进入的卡片 app_id(用户视角,与模型 running 无关)
    _pollHandle: null,       // 1Hz 轮询 handle,仅在卡片页启用
    _inFlight:  false,       // 防重入（_evaluate 本身是 async）
    _bootDone:  false,       // 启动期 bootRestore 是否已跑过

    _findManifest(appId) {
      const ms = (window.__APP_MANIFESTS__
                  && window.__APP_MANIFESTS__.cards) || [];
      return ms.find((m) => m && m.id === appId) || null;
    },

    // 卡片"可作为 active_app 目标"：status=ok 且非 hidden。
    _isTrackableCard(m) {
      return !!(m && m.status === "ok" && m.hidden !== true);
    },

    _requiredCaps(m) {
      if (!m) return [];
      const caps = m.software_capabilities;
      return Array.isArray(caps) ? caps.slice() : [];
    },

    // 从 /api/compute snapshot 判断"该 app 所需 cap 是否全部有 local
    // running 实例"。adopted (LAN peer) 故意排除：peer reboot 后这
    // 个 autostart 等于没意义,用户得手动在本机重启用对应模型。
    _capsAllLocalRunning(caps, snapshot) {
      if (!caps || caps.length === 0) return false;  // 无 cap 的不进入 active_app
      if (!snapshot || !Array.isArray(snapshot.instances)) return false;
      const localRunning = snapshot.instances.filter(
        (i) => i && i.status === "running" && i.source !== "adopted"
      );
      return caps.every((cap) =>
        localRunning.some((i) => Array.isArray(i.capability_types)
                              && i.capability_types.indexOf(cap) !== -1)
      );
    },

    async _fetchSnapshot() {
      try {
        const r = await fetch("/api/compute", { cache: "no-store" });
        if (!r.ok) return null;
        return await r.json();
      } catch (_) { return null; }
    },

    async _set(appId) {
      // 把 app_id PUT 到后端(后端在 caps 全 local running 时落盘
      // active_app + 增量写 autostart)。失败时不动 _current —— _current
      // 是用户视角的 last entered card,PUT 失败(409 caps 未 ready)不
      // 影响 sidebar / reboot 恢复的语义,等下次 _evaluate caps ready
      // 再补 PUT。
      try {
        const r = await fetch("/api/compute/active-app", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ app_id: appId }),
        });
        if (!r.ok) {
          let detail = "";
          try { detail = (await r.json()).detail || ""; } catch (_) {}
          if (r.status !== 404) {
            // 404 是 app 被 manifest 删了 —— 静默;其它错误值得 console
            console.warn("[active-app] PUT failed for",
                         appId, r.status, detail);
          }
        }
      } catch (_) {
        // 网络挂了 / 浏览器后台节流,下次 _evaluate 再来
      }
    },

    // 显式退出:用户在卡片页点 back-home / "退出应用" 时调用。
    // 同步清掉 _current + 异步发 DELETE。下次 sidebar 应用面板 → apps
    // 列表;reboot 后 bootRestore 也不再跳到这张卡。
    _exit() {
      this._current = null;
      try {
        // keepalive:back-home 紧跟着 location.hash="#/" 切页,普通 fetch
        // 可能被中断;DELETE 是幂等的,即便本次没送达下次 _evaluate 也会
        // 重新 PUT(那时 _current 已经 null,_evaluate 不会再写)。
        fetch("/api/compute/active-app",
              { method: "DELETE", keepalive: true });
      } catch (_) { /* best-effort */ }
    },

    _startPolling() {
      if (this._pollHandle) return;
      this._pollHandle = setInterval(() => this._evaluate(), 1000);
    },

    _stopPolling() {
      if (this._pollHandle) {
        clearInterval(this._pollHandle);
        this._pollHandle = null;
      }
    },

    // 在 ROUTER.showPage 之后由框架调用。每进一张卡片都更新 _current
    // (用户视角的 last entered card),不因切到系统页 / home / 另一张卡
    // 而清除 —— 只有显式 _exit() 才清。
    async _evaluate() {
      if (!this._bootDone) return;
      if (this._inFlight) return;
      this._inFlight = true;
      try {
        const page = ROUTER.currentPage();
        const m = this._isTrackableCard(this._findManifest(page))
          ? this._findManifest(page) : null;
        if (!m) {
          // 非卡片页(home / 系统页 / 不可见卡片):停轮询,**不动 _current**。
          // 用户中途看一眼系统页或回到 home 不算"退出当前 app"——
          // sidebar 应用面板仍应能跳回 _current;reboot 后 bootRestore 也
          // 应能恢复 _current。DemoGuard 的 silent keep 已经在另一层把
          // 模型跑着保留了下来,这里再清 active_app 就是双重惩罚。
          this._stopPolling();
          return;
        }
        // 任何"进入卡片页"都把 _current 推到这张卡。
        // 不论 caps running 与否都设 —— 用户视角的"我在这"和模型层
        // readiness 是两回事;wizard 中 / cap loading 中 sidebar 也应
        // 能跳回这里。
        this._current = m.id;
        const caps = this._requiredCaps(m);
        if (caps.length === 0) {
          // 卡片声明无 software_capabilities(纯静态页 / 仅配置类) →
          // 后端 autostart 不适用(cap 为空写不进 autostart),in-memory
          // _current 已设好,sidebar / reboot 恢复都按 _current 走。
          this._stopPolling();
          return;
        }
        const snap = await this._fetchSnapshot();
        if (!snap) return;  // 网络挂了,下个 tick 再来
        if (this._capsAllLocalRunning(caps, snap)) {
          // caps 已 ready → 落盘 active_app + 增量写 autostart(幂等)。
          await this._set(m.id);
        }
        // cap 未全 running(wizard 中、模型 loading、用户刚停掉):不 PUT
        // 后端会 409,但**不**清 _current。等下个 tick 1s 后再判定,
        // caps 一 ready 立刻补 PUT。
      } finally {
        this._inFlight = false;
      }
    },

    // ROUTER.showPage 完成之后由 framework 调一次 —— 既要立刻
    // 判定一次(避免 1s 等待用户看到错误状态),也要起 1Hz 轮询
    // 跟踪 cap 从 loading → running 的转变。
    onNavigated() {
      this._evaluate();
      const page = ROUTER.currentPage();
      const m = this._isTrackableCard(this._findManifest(page))
        ? this._findManifest(page) : null;
      if (m && this._requiredCaps(m).length > 0) {
        this._startPolling();
      } else {
        this._stopPolling();
      }
    },

    // 关 tab / 刷页时**不**清 active_app —— 用户中途关 tab 是常见操作,
    // 不该把"我在这"也一起擦掉;reboot 后 bootRestore 要靠它把用户跳
    // 回原卡片。显式退出只发生在 back-home 按钮(_exit()),那里才会
    // DELETE 后端。reboot 时 autostart 仍会 spawn 该 app 需要的 worker,
    // 二者配合即"重开软件继续进入这个应用并开启他所需的 worker"。
    onBeforeUnload() {
      this._stopPolling();
    },

    // Boot 时调一次:若后端记着 active_app 且对应卡片仍可追踪 →
    // 直接跳到 #<app_id>。否则保持默认 #apps。
    async bootRestore() {
      if (this._bootDone) return;
      this._bootDone = true;
      try {
        const r = await fetch("/api/compute/active-app");
        if (!r.ok) return;
        const data = await r.json();
        const appId = data && data.active_app;
        if (!appId) return;
        const m = this._findManifest(appId);
        if (!this._isTrackableCard(m)) {
          // 后端记的 app 现在没了 → 清掉,别让 stale 状态一直留着。
          // 同步发 DELETE 不 await(best-effort),但要在本端先清
          // _current(还没填过就 0 effect)。
          try { await fetch("/api/compute/active-app", { method: "DELETE" }); }
          catch (_) { /* ignore */ }
          return;
        }
        // 用户大概率还没动 hash,直接跳过去;但若用户已主动离开
        // 过一次默认页(罕见),尊重现状不抢。
        if (ROUTER.currentPage() === "apps") {
          // 用 location.hash 而不是 ROUTER.navigate:boot 期还没
          // 注册 _keptPages / DemoGuard,走 navigate 会触发一些
          // 还没初始化的回调。直接改 hash 让 hashchange listener
          // 接管(那个 listener 此时已经绑好了)。
          location.hash = "#" + appId;
        }
      } catch (_) { /* boot failure, ignore */ }
    },
  };

  window.ActivateManager = ActivateManager;

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

  // wizard:skipped —— tryMount 早退时的对偶事件。已完成的卡片被 sidebar
  // 应用面板带回时,tryMount 看到 WizardState.isCompleted 直接 return,
  // 不会调 mount,wizard:mounted 永远不会 fire;但 _prepareCardEntry
  // 早已把 page 藏了 + 把 wd-loading-section 亮出来"正在加载 ...",用户
  // 看到的就是"切换其他页回来,卡在加载页"的假死(实测 30s 兜底之前一
  // 直挂着)。这个事件让 tryMount 在每个早退路径上(已完成 / cfg 缺失 /
  // cfg.enabled=false / WizardState 未加载 / 内部异常)都同步通知 framework
  // 把 loading 收掉,并把 _prepareCardEntry 藏起来的 page 还回来。
  // 还原 page 用 currentPage() === appId 守门 —— 用户若在事件到
  // 达前已切到别处,新 page 才是 visible,旧 page 必须保持 hidden。
  window.addEventListener("wizard:skipped", (ev) => {
    _dismissCardLoading();
    const appId = ev?.detail?.appId;
    if (appId && currentPage() === appId) {
      const ps = document.getElementById("page-" + appId);
      if (ps) ps.classList.remove("hidden");
    }
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
        ev.preventDefault();
        // 不再以 DemoGuard.isActive 当门:用户即便没在跑后台、只是
        // 开着 wizard 看了一眼,点这个按钮的语义也是"我要离开这张卡"。
        // 只在真的有 running session 时顺手 stopAndClear 释放算力;
        // 没有的话直接走清状态 + 切回首页。
        if (DemoGuard.isActive(currentPage())) {
          await DemoGuard.stopAndClear(currentPage());
        }
        DemoGuard._activeApp = null;
        // 显式退出:
        //  * _current 清掉、后端 DELETE —— 下次 sidebar 应用面板
        //    → apps 列表;reboot 不再恢复这张卡;
        //  * WizardState.clearAll 把整张卡的 wizard 状态
        //    (values / step / completed) 从 localStorage 抹掉。
        //    没有这一步:用户进卡 → tryMount 仍看到 completed=1
        //    → 不挂 wizard,直接落进功能页。这是 v0.22 修完后的
        //    副作用 —— "退出应用" 应该等于"fresh install"。
        ActivateManager._exit();
        const cp = currentPage();
        if (window.WizardState?.clearAll) {
          window.WizardState.clearAll(cp);
        }
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
      // 用 "用户最后进入的卡片" 而不是 DemoGuard._activeApp:后者只在
      // 模型/session running 时才非空,wizard 中 / 刚打开未加载模型
      // 时是 null,会让 sidebar 跳到 apps 面板而不是回到那张卡。
      // ActivateManager._current 由 _evaluate 在每次进入卡片页时设,
      // 只在显式 back-home 退出时清,正符合"除非点了返回才展示应用列表"
      // 的语义。
      const target = ActivateManager._current || "apps";
      if (currentPage() === target) return;
      location.hash = "#" + target;
    });
  }

  // ─────────────────────────────────────────────────────────────────────
  // "向导模式" 按钮 — 各 app template.html 里
  //   <button class="wizard-mode-btn" data-wizard-mode
  //           data-page-id="<id>">向导模式</button>
  // 之前是 dead UI:DOM 在、样式在、`enterWizardMode` 全局函数也在,
  // 但没有任何 JS 把 click 接到那个函数。后果是用户完成向导后,
  // 该按钮看起来可点、实际无反应;真要重开 wizard 只能走
  // bootRestore 之前的旧路径(已被 v0.22 改后失效)。
  //
  // 事件委托挂 document,而不是每个 button 各挂 —— 模板里 6 张卡都
  // 有这按钮,各 app.js 也不要重复 wire。
  // ─────────────────────────────────────────────────────────────────────

  function wireWizardModeButtons() {
    document.addEventListener("click", (ev) => {
      const btn = ev.target.closest("[data-wizard-mode]");
      if (!btn) return;
      const appId = btn.getAttribute("data-page-id");
      if (!appId) return;
      ev.preventDefault();
      if (typeof window.enterWizardMode === "function") {
        window.enterWizardMode(appId);
      } else {
        // 理论不可能:wizard/registry.js 比 framework 早 boot,
        // WizardState 加载顺位也在 enterWizardMode 之前。但留个
        // 兜底,避免真出现时静默失败。
        console.warn("[framework] enterWizardMode not loaded yet");
      }
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
    // 各 app template.html 里的"向导模式"按钮统一委托(见
    // wireWizardModeButtons 注释):6 张卡都靠这按钮重开 wizard。
    wireWizardModeButtons();
    updateBackHomeLabels();
    onHashChange();
    // Boot 后端记着的 active_app(若有)→ 直接跳过去。必须在第一次
    // onHashChange 之后调,否则会被 hashchange 覆盖成 #apps 状态。
    ActivateManager.bootRestore();
  }

  window.addEventListener("hashchange", () => {
    DemoGuard._refreshActive();
    updateBackHomeLabels();
    onHashChange();
    // 跟踪新页面是否需要更新 _active_app(hash 改完 → evaluate)。
    ActivateManager.onNavigated();
  });
  window.addEventListener("demoguard:active-change", updateBackHomeLabels);
  // 关 tab / 刷页时 best-effort 告诉后端"我已离开"。keepalive 让请求
  // 在 unload 后还能送达(浏览器限制不一定靠谱,见 manifest 注释)。
  window.addEventListener("beforeunload", () => {
    ActivateManager.onBeforeUnload();
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", onReady);
  } else {
    onReady();
  }
})();
