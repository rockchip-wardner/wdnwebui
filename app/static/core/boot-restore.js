/* ══════════════════════════════════════════════════════════════════════════
 * boot-restore.js —— 开机自启恢复：「模型加载中」的可见反馈 + 就绪后自动启动
 *
 * 背景（v0.27 真机实测）
 * ---------------------------------------------------------------------
 * 开机 autostart 会把 compute_worker 拉起来，但模型要加载 ~35~40s。这段
 * 窗口里 `get_session(capability)` 只认 `status == "running"`，一律抛
 * "功能未启用"，detector snapshot 把它 catch 成 init_error，前端显示：
 *
 *     模型加载失败: 功能未启用: swcap_vision_chat
 *
 * 三件事同时错：
 *   1. 把「还在加载」说成「加载失败」（后端已修：snapshot 加 model_loading）；
 *   2. 「启动检测」按钮此时**可点**，点了必然 502（get_session 同样抛）；
 *   3. 模型加载完之后**没人去启动检测** —— 用户必须回来手点一次。
 *
 * 本模块解决 2 和 3，1 靠 detector snapshot 的 model_loading 字段。
 *
 * 它是三张视频卡（vd / vtd / vtl）共用的。共用的理由不是"少写几行"：
 * 语义本身是通用的 —— 「一次性许可」「上次跑的是什么」「等模型时按钮
 * 长什么样」，这三件事在三个工作流（描述 / 目标检测 / 定位）里完全一致，
 * 各写一份必然漂移（改了一张忘了另外两张）。
 *
 * 用法（每张卡三处调用）
 * ---------------------------------------------------------------------
 *   // 1) 卡片初始化时认领（consume 是一次性的，见下）
 *   const boot = BootRestore.begin("video-detection", { startBtn: "vd-detector-start" });
 *   if (boot && boot.saved) await restoreMyForm(boot.saved);   // 回填 + 开源
 *
 *   // 2) 每次拿到 detector 快照时喂一次
 *   const r = BootRestore.tick(boot, snap);
 *   if (r.startNow) await myDetectorStart();
 *
 *   // 3) 检测成功启动后记录「这次跑的是什么」，下次开机据此复原
 *   BootRestore.save("video-detection", { prompt, sources, ... });
 *
 * consume 为什么要一次性
 * ---------------------------------------------------------------------
 * framework 的 bootRestore() 置一个全局标记后跳到 #<app_id>。如果这个标记
 * 不消费掉，用户**事后手动点进同一张卡**时也会被判成"开机恢复"，于是上次的
 * 视频源被莫名其妙重新打开、检测自动启动 —— 一次无意的点击造成一次意外的
 * 长跑。consume() 取走即失效，把"开机恢复"和"用户自己进来"区分开。
 * ══════════════════════════════════════════════════════════════════════════ */
(() => {
  "use strict";

  /** 供框架写、供本模块读的全局标记名。 */
  const FLAG = "__WDN_BOOT_RESTORE__";

  const store = {
    key(appId) { return "wdn.lastrun." + appId; },
    load(appId) {
      try {
        const raw = localStorage.getItem(store.key(appId));
        if (!raw) return null;
        const v = JSON.parse(raw);
        return (v && typeof v === "object") ? v : null;
      } catch (_) {
        // localStorage 不可用（隐私模式 / 配额满 / 跨域 iframe）时不能让
        // 整张卡崩掉 —— 开机恢复只是锦上添花，拿不到就退化成老行为。
        return null;
      }
    },
    save(appId, payload) {
      try {
        localStorage.setItem(store.key(appId), JSON.stringify(payload));
        return true;
      } catch (_) {
        return false;
      }
    },
    clear(appId) {
      try { localStorage.removeItem(store.key(appId)); return true; }
      catch (_) { return false; }
    },
  };

  /** 一次性领取开机恢复许可。没被 bootRestore arm 过就返回 false。 */
  function claim(appId) {
    const f = window[FLAG];
    if (!f || f.app_id !== appId) return false;
    // 取走即失效（含非目标卡）：标记是"本次启动只恢复一张卡"的语义。
    try { delete window[FLAG]; } catch (_) { window[FLAG] = null; }
    return true;
  }

  /** appId -> handle。让"标记比卡片晚到"这条路径能找到对应的 handle。 */
  const _handles = new Map();

  /** 从 handle 上跑恢复流程（供 begin / 事件两条路径共用）。 */
  function _run(h) {
    if (!h.saved || typeof h.restore !== "function") return;
    try {
      h.restore(h.saved, h);
    } catch (e) {
      // 恢复失败**不能**打断页面:退化成"用户自己点启动"的老行为。
      console.warn("[boot-restore] " + h.appId + " 恢复失败", e);
    }
  }

  function _tryClaim(h) {
    if (!claim(h.appId)) return false;
    h.claimed = true;
    h.saved = store.load(h.appId);
    return true;
  }

  // 标记晚到的那条路径:卡片 IIFE 先跑完 begin()，bootRestore 的 fetch 才回来
  // (刷新时 URL 已带 #<app_id> 就是这个顺序)。事件把卡片捞回来补一次 claim。
  window.addEventListener("compute:boot-restore", (e) => {
    const appId = e && e.detail && e.detail.app_id;
    if (!appId) return;
    const h = _handles.get(appId);
    if (!h || h.claimed) return;      // 没登记 = 卡片还没 begin，走 begin 里的同步 claim
    if (_tryClaim(h)) _run(h);
  });

  /** 把按钮切成"转圈 + 文案"或复原。label 为 null 时不改文案。
   *
   *  **等待期间按钮必须不可点** —— get_session() 只认 status=="running"，
   *  此刻点「启动检测」必然 502「功能未启用」。这条不变量放在这里（而不是
   *  各卡片自己写一遍）是因为它是共享语义：谁忘了处理就是"一个可点但必失败的
   *  按钮"，而且这个 bug 只在开机那几十秒出现，手测极易漏。
   *
   *  复原时把 disabled 还原成**转圈前**的值，而不是简单置 false：
   *  按钮本来就可能是禁用的（比如检测已在跑），还原成 false 会把它错误地放开。
   */
  function paint(btn, loading, label) {
    if (!btn) return;
    if (loading) {
      if (btn.dataset.bootLabel === undefined) {
        btn.dataset.bootLabel = (btn.textContent || "").trim();
        btn.dataset.bootWasDisabled = btn.disabled ? "1" : "0";
      }
      btn.classList.add("is-boot-wait");
      btn.disabled = true;
      if (label) btn.textContent = label;
    } else {
      btn.classList.remove("is-boot-wait");
      if (btn.dataset.bootLabel !== undefined) {
        btn.textContent = btn.dataset.bootLabel;
        btn.disabled = btn.dataset.bootWasDisabled === "1";
        delete btn.dataset.bootLabel;
        delete btn.dataset.bootWasDisabled;
      }
    }
  }

  const BootRestore = {
    /**
     * 卡片初始化时调用一次。
     *
     * @param {string} appId   manifest 里的卡片 id
     * @param {object} opts    { startBtn: string|Element,
     *                          restore: (saved, handle) => void|Promise }
     *                         restore 是"确认要恢复"后被调用的入口，由卡片自己
     *                         填表单 / 开视频源；本模块只管**何时**该恢复。
     * @returns {object} handle；不是开机恢复场景时也返回 handle，
     *          但 claimed=false（卡片据此跳过整段恢复逻辑）。
     */
    begin(appId, opts) {
      opts = opts || {};
      const btn = typeof opts.startBtn === "string"
        ? document.getElementById(opts.startBtn) : opts.startBtn;
      const h = {
        appId,
        btn,
        restore: opts.restore,
        claimed: false,
        // 只有真的被 arm 了才读存档：用户手动进卡时不必付JSON.parse 的钱，
        // 也不会因为读到一个陈旧的存档就去覆盖用户刚填的表单。
        saved: null,
        // pending = "我正在等模型就绪，就绪后要自动启动"。
        pending: false,
        fired: false,
      };
      _handles.set(appId, h);
      // 标记先到（bootRestore 跳过来）→ 当场认领；标记后到 → 事件里补认领。
      if (_tryClaim(h)) _run(h);
      return h;
    },

    /**
     * 喂一次 detector 快照，推进等待状态机。
     *
     * @returns {{startNow: boolean}} startNow=true 表示"现在可以启动检测了"
     *          （且只会返回一次 —— pending 已在此之前被清掉）。
     */
    tick(h, snap) {
      if (!h || !h.pending || !snap) return { startNow: false };
      if (!snap.session_ready) {
        // model_loading=false 却还没就绪 → 没有任何实例在加载，即真的失败 /
        // 能力缺失。继续转圈会把用户永远挂在一个不会停的进度条上（这是
        // 比"不转圈"更糟的失败模式），所以就地放弃并把按钮还回去，
        // 让 updateXxxDetectorStatus 的错误分支去显示真正的信息。
        if (!snap.model_loading) {
          h.pending = false;
          paint(h.btn, false);
        } else {
          paint(h.btn, true, "正在等待模型加载…");
        }
        return { startNow: false };
      }
      // 就绪了。
      h.pending = false;
      paint(h.btn, false);
      if (h.fired) return { startNow: false };
      // saved 里有 prompt 才起得来（vd/vtd 的检测提示词、vtl 的查询词）。
      // 没有就不硬起 —— 编一句提示词去跑推理是比"不自动启动"更糟的行为。
      const s = h.saved;
      const hasPrompt = !!(s && (
        (typeof s.prompt === "string" && s.prompt.trim()) ||
        (Array.isArray(s.prompts) && s.prompts.some((p) => (p || "").trim()))
      ));
      if (!hasPrompt) return { startNow: false };
      h.fired = true;
      return { startNow: true };
    },

    /** 进入等待态。卡片应在恢复表单 / 开源**之前**调用，让按钮立刻转圈。 */
    arm(h) {
      if (!h || !h.claimed || !h.saved) return false;
      h.pending = true;
      return true;
    },

    /** 检测成功启动后记录这次的输入，下次开机据此复原。 */
    save(appId, payload) { return store.save(appId, payload); },
    load(appId) { return store.load(appId); },
    clear(appId) { return store.clear(appId); },
  };

  window.BootRestore = BootRestore;
})();