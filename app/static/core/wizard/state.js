/* WizardState: 步骤状态机 (env_check → form[] → env_check → form,最后 finish)
 *
 * 暴露 window.WizardState:
 *   - mount(app, cfg) — 注入 wizard DOM 到 <section id="page-<id>"> 兄弟位置;原 page 隐藏
 *   - unmount()        — 销毁 DOM、释放 listener、清除 interval
 *
 * 零耦合:不 import 任何 app,只通过 cfg (wizard.json) 字段;应用层仅通过
 * window.ImApp_<id>.applyWizardSnapshot(opts) 被调用,不感知 wizard DOM。
 */
(function () {
  "use strict";

  const WIZARD_NS = "wd-wizard-";
  let _active = null;     // {app, cfg, container, currentStep, values, listeners, intervals}

  function _q(sel, root = document) { return root.querySelector(sel); }
  function _ce(tag, cls, text) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text != null) el.textContent = text;
    return el;
  }

  /** 字段 id 是否是「视频源」——决定是否自动加浏览/打开/关闭按钮。
   *
   *  **必须同时接受 `-source` 与 `-sources`**：多路卡字段是复数。这个判定
   *  在 `_renderFormStep` 出现两次（inline 循环开关 / 加浏览按钮），各写一份
   *  正则就一定漏改一处，而漏改是**静默少一个控件** —— 按钮照常渲染，红不了
   *  任何断言。改判定请改这里，不要就地写正则。
   */
  function _isSourceFieldId(id) {
    return /-sources?$/.test(id || "");
  }

  // ------------------------------------------------------------------ #
  // localStorage 持久化
  // ------------------------------------------------------------------ #
  // 同一份 (appId, cfg) 下三块独立键,共享 base + 不同 suffix:
  //   *.values    —— 各步骤字段已填值
  //   *.step      —— 上次停留的步骤 idx(导航中途切走 / reboot 后恢复)
  //   *.completed —— applyWizardSnapshot 成功调过一次,后续自动 mount 跳过
  function _lsBase(appId, cfg) {
    return cfg.localStorage_key || ("wdnwebui.wizard." + appId);
  }
  function _lsKey(appId, cfg, suffix) {
    return _lsBase(appId, cfg) + "." + (suffix || "values");
  }
  function _lsLoad(appId, cfg) {
    try {
      const raw = localStorage.getItem(_lsKey(appId, cfg));
      return raw ? JSON.parse(raw) : {};
    } catch (_) { return {}; }
  }
  function _lsSave(appId, cfg, values) {
    try {
      localStorage.setItem(_lsKey(appId, cfg), JSON.stringify(values));
    } catch (_) { /* ignore quota */ }
  }

  // currentStep 持久化:用户填到第 3 步中途切走 / reboot 后恢复
  // 软件,下次 mount 应该回到第 3 步,而不是第 1 步(把已填值都丢了的
  // 既视感)。
  //
  // 存**步骤 id** 而不是下标(v0.24):单卡二选一会把不适用的步骤裁掉,
  // 下标会整体错位 —— 原来 index 3 = "audio",裁掉后 index 3 = "finish",
  // 按老下标恢复会让用户一进来就落在「完成配置」上。
  function _lsLoadStep(appId, cfg) {
    try {
      return localStorage.getItem(_lsKey(appId, cfg, "step"));
    } catch (_) { return null; }
  }
  function _lsSaveStep(appId, cfg, step) {
    try {
      localStorage.setItem(_lsKey(appId, cfg, "step"), String(step));
    } catch (_) { /* ignore quota */ }
  }
  function _lsClearStep(appId, cfg) {
    try { localStorage.removeItem(_lsKey(appId, cfg, "step")); }
    catch (_) {}
  }

  // 把「上次停在哪个步骤」解析成当前步骤表里的下标。存的是步骤 id →
  // 直接查表（裁剪后的表也能正确定位）；老版本存的是数字 → 按下标兜底；
  // 存的步骤已被裁掉 → 回第 1 步,重新确认环境与二选一。
  function _resolveStepIdx(steps, saved) {
    if (!steps || !steps.length || saved == null) return 0;
    const raw = String(saved);
    const byId = steps.findIndex((s) => s.id === raw);
    if (byId >= 0) return byId;
    const n = parseInt(raw, 10);
    if (Number.isFinite(n) && n > 0 && n < steps.length) return n;
    return 0;
  }

  // 完成标记:_finish() 里 applyWizardSnapshot 成功后置 1。
  // 之后 tryMount 会跳过自动挂载,直到用户点"向导模式"按钮显式
  // 重置(走 force=true 同时清掉 step + completed)。
  function _lsLoadCompleted(appId, cfg) {
    try {
      return localStorage.getItem(_lsKey(appId, cfg, "completed")) === "1";
    } catch (_) { return false; }
  }
  function _lsSaveCompleted(appId, cfg, completed) {
    try {
      localStorage.setItem(_lsKey(appId, cfg, "completed"),
                           completed ? "1" : "0");
    } catch (_) { /* ignore quota */ }
  }
  function _lsClearCompleted(appId, cfg) {
    try { localStorage.removeItem(_lsKey(appId, cfg, "completed")); }
    catch (_) {}
  }

  // "退出应用" 兜底:framework 在 back-home 按钮 handler 里调。
  // 把该 app 的整个 wizard 状态从 localStorage 抹掉,下次进入时
  // wizard 就像首次启动一样 —— 从第 1 步、空字段开始。
  //
  // 实现:扫 localStorage,删所有"默认 base 命名空间"下的 key。
  //  * 默认 base = "wdnwebui.wizard.<appId>"(目前所有卡都走这个)
  //  * 严格匹配 values / step / completed 三把;同时支持 future-proof
  //    前缀匹配 —— 万一未来加了 step-history 之类的 suffix,也不会
  //    漏删
  //  * 不动 cfg.localStorage_key 路径:目前没有任何卡设,如果
  //    未来有,framework 那边需要扩成"先 load cfg 再调 clearCompleted
  //    + clearStep"的两段式清
  function _lsClearAllFor(appId) {
    if (!appId) return;
    const base = "wdnwebui.wizard." + appId;
    const knownSuffixes = new Set(["values", "step", "completed"]);
    try {
      // 倒序遍历 —— 中途 removeItem 不影响未访问的 idx
      for (let i = localStorage.length - 1; i >= 0; i--) {
        const k = localStorage.key(i);
        if (!k || !k.startsWith(base + ".")) continue;
        const suffix = k.slice(base.length + 1);
        if (knownSuffixes.has(suffix) || suffix.length > 0) {
          // 已知 suffix 必删;未知 suffix 也按命名空间删,避免
          // 漏清导致下次进入时残留状态复活。
          localStorage.removeItem(k);
        }
      }
    } catch (_) { /* ignore */ }
  }

  // ------------------------------------------------------------------ //
  // 上传在途守卫 (source step drop-zone)
  //
  // 手填 url/路径与拖放上传写的是同一个 wizard input `el`。上传是异步的,
  // 而「下一步」此前不受上传状态约束,于是用户可在 POST 在途时点下一步:
  // _captureValues 把**上传前**的 el.value(上次填的 rtsp://… 或本地路径)
  // 当作用户填的视频源存进 state.values;随后 _gotoStep 重建 body 使 el
  // 脱离文档,fetch 回来后 `el.value = data.path` 只写在游离节点上,权威值
  // 仍是旧源 → finish 时 applyWizardSnapshot 打开的不是刚上传的视频。
  //
  // 三层防护:锁住导航 / path 直写 state.values / 不对游离 DOM 做副作用。
  // ------------------------------------------------------------------ //
  function _setUploadBusy(state, busy, fieldId) {
    if (!state) return;
    // 用计数而非布尔:drop-zone 在上传期间仍在 DOM 里,用户可能再拖一个
    // 文件,先完成的那个不能把仍在途的上传解锁。
    state.uploadInFlight = Math.max(0, (state.uploadInFlight || 0) + (busy ? 1 : -1));
    state.uploadBusy = state.uploadInFlight > 0;
    state.uploadBusyFieldId = state.uploadBusy ? (fieldId || state.uploadBusyFieldId) : null;
    _applyNavLock(state);
  }

  // 把上传锁同步到「下一步」/「打开」按钮。footer 重建后需重新调用
  // (此时按钮是新节点,disabled 不会继承)。
  function _applyNavLock(state) {
    const ids = [WIZARD_NS + "next-btn"];
    if (state.uploadBusyFieldId) {
      ids.push(WIZARD_NS + "source-open-" + state.uploadBusyFieldId);
    }
    ids.forEach((id) => {
      const btn = _q("#" + id);
      // form step 的「下一步」恒为 enabled(`_renderFootNext(..., true)`),
      // 直接置 false 安全;env_check step 没有 upload UI。
      if (btn) btn.disabled = state.uploadBusy;
    });
  }

  // 按钮已 disabled,这里是键盘 Enter / 脚本旁路的兜底,兼作一次提示。
  function _flashUploadHint() {
    const box = _q("#" + WIZARD_NS + "body .wd-wizard-drop-status");
    if (!box) return;
    box.textContent = "视频正在上传中,请等待上传完成后再进入下一步";
    box.className = WIZARD_NS + "drop-status " + WIZARD_NS + "drop-status-busy";
  }

  // 向导仍存活 + 本步骤节点仍在文档中(未跳步 / 未卸载)。
  function _isStepAlive(state, el) {
    return !!_active && _active === state && !!el && el.isConnected;
  }

  // ------------------------------------------------------------------ #
  // 字段定义 → opts key 的映射(去掉 app id 前缀 + camelCase)
  // ------------------------------------------------------------------ #
  function _fieldKey(appId, field) {
    if (field.ui_key) return field.ui_key;       // 显式覆盖
    let raw = field.id;
    // 去掉 <appId-> 前缀
    const pfx = appId + "-";
    if (raw.startsWith(pfx)) raw = raw.slice(pfx.length);
    // camelCase: vd-detect-size → detectSize; vd-audio-prompt → audioPrompt
    return raw.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
  }

  // ------------------------------------------------------------------ #
  // DOM 渲染
  // ------------------------------------------------------------------ #
  function _renderShell(container, app, cfg) {
    container.innerHTML = "";
    container.classList.add(WIZARD_NS + "container");

    const title = cfg.wizard_title || app.title;
    const head = _ce("div", WIZARD_NS + "head");
    // head 现在是 flex：title 在左、返回按钮在右。h2 单独不设 width，
    // 借助 head flex 把按钮顶到右边。
    const titleEl = _ce("h2", WIZARD_NS + "title", title);
    head.appendChild(titleEl);
    // 「返回」按钮 —— 跳回应用列表（不是回到当前 app 的功能页）。
    //
    // 不要走 skip() 路径：skip 走 unmount(false) 会**显示原 page**（让用户
    // 回到 vd / vtd / vtl 的功能页）。本按钮语义是"放弃向导、回首页" —
    // — 一是方便用户在多卡之间切换，二是让主动离开的路径跟 sidebar 点
    // 应用卡片同源。
    //
    // 实现：先 _captureValues 把已填字段落 localStorage，再 ``location.hash
    // = ""`` 触发 hashchange。state.js mount 末尾已经挂了 hashchange
    // listener（见 _onNavAway），它会调 unmount(true) 自动卸载 wizard
    // —— silent=true 表示用户已经离开，unmount 不再 unhidden 原 page。
    const backBtn = _ce("button", WIZARD_NS + "head-back-btn", "返回");
    backBtn.type = "button";
    backBtn.title = "返回应用列表（已填字段会保留）";
    backBtn.addEventListener("click", () => {
      if (!_active) return;
      _captureValues(_active.cfg, _active);
      location.hash = "";   // → 触发 hashchange → onNavAway → unmount(true)
    });
    head.appendChild(backBtn);
    container.appendChild(head);

    const stepper = _ce("div", WIZARD_NS + "stepper");
    stepper.id = WIZARD_NS + "stepper";
    container.appendChild(stepper);

    const body = _ce("div", WIZARD_NS + "body");
    body.id = WIZARD_NS + "body";
    container.appendChild(body);

    const foot = _ce("div", WIZARD_NS + "foot");
    foot.id = WIZARD_NS + "foot";
    container.appendChild(foot);
  }

  function _renderStepper(stepper, cfg, currentIdx) {
    stepper.innerHTML = "";
    cfg.steps.forEach((s, i) => {
      const dot = _ce("div", WIZARD_NS + "step-dot");
      if (i < currentIdx) dot.classList.add(WIZARD_NS + "step-done");
      else if (i === currentIdx) dot.classList.add(WIZARD_NS + "step-active");
      else dot.classList.add(WIZARD_NS + "step-todo");
      const num = _ce("span", WIZARD_NS + "step-num", i < currentIdx ? "✓" : String(i + 1));
      dot.appendChild(num);
      const lbl = _ce("span", WIZARD_NS + "step-lbl", s.title);
      dot.appendChild(lbl);
      stepper.appendChild(dot);
      if (i < cfg.steps.length - 1) {
        const sep = _ce("div", WIZARD_NS + "step-sep");
        stepper.appendChild(sep);
      }
    });
  }

  // ------------------------------------------------------------------ #
  // 步骤 kind handlers
  // ------------------------------------------------------------------ #
  // ------------------------------------------------------------------ #
  // 应用没声明任何能力时，环境检查用这一行代替空列表 ——「无需检查」与
  // 「检查失败」在视觉上必须区分开（否则卡片只剩一个光秃秃的标题，
  // 看着像环境探测挂了）。
  function _envNoReqRow(text) {
    const row = _ce("div", WIZARD_NS + "env-row " + WIZARD_NS + "env-none");
    row.textContent = "✓ " + text;
    return row;
  }

  // 单卡提示块。kind: "warn"（可二选一）/ "block"（刚需多模型，单卡不可用）。
  function _singleCardNotice(kind, title, lines) {
    const box = _ce("div",
      WIZARD_NS + "single-notice " + WIZARD_NS + "single-notice-" + kind);
    box.appendChild(_ce("div", WIZARD_NS + "single-notice-title", title));
    (lines || []).forEach((t) => {
      box.appendChild(_ce("div", WIZARD_NS + "single-notice-line", t));
    });
    return box;
  }

  function _renderEnvStep(body, foot, cfg, stepDef, state) {
    body.innerHTML = "";
    const hwOK = (state.envCheck.hwMissing || []).length === 0;
    // 单卡策略：双卡（或探测失败）时 single 为 null,下面所有 single
    // 分支都不走 —— 行为与之前逐字一致。
    const single = state.single || null;
    const singleOn = !!(single && single.active);
    const blocked = !!(singleOn && single.blocked);
    // 单卡下 capability 集合可能被裁剪：vd 二选一只留选中的那条；多
    // capability 又没声明二选一时,两条都列出来,说明为什么不可用。
    const swRequired = singleOn
      ? (single.requiredCaps || [])
      : (state.envCheck.swRequired || []);
    const swMissing = singleOn
      ? swRequired.filter((c) => (state.envCheck.swMissing || []).includes(c))
      : (state.envCheck.swMissing || []);
    const swOK = swMissing.length === 0;

    const card = _ce("div", WIZARD_NS + "env-card");
    card.appendChild(_ce("h3", null, "硬件能力"));
    state.envCheck.hwRequired.forEach((cap) => {
      const ok = !state.envCheck.hwMissing.includes(cap);
      const row = _ce("div", WIZARD_NS + "env-row");
      row.classList.add(ok ? WIZARD_NS + "env-ok" : WIZARD_NS + "env-fail");
      row.textContent = (ok ? "✓ " : "✗ ") + cap;
      card.appendChild(row);
    });
    // 空能力列表（如阈值标定卡：纯 CPU 判帧，不碰 NPU 也不声明硬件能力）
    // 会让上面这张卡渲染成一个光秃秃的标题，看着像加载失败。补一行说明，
    // 让「没有要求」读起来是「无需检查」而不是「检查没跑」。
    if (!state.envCheck.hwRequired.length) {
      card.appendChild(_envNoReqRow("本应用不依赖特定硬件能力（纯 CPU 计算）"));
    }
    if (!hwOK) {
      const err = _ce("div", WIZARD_NS + "env-error");
      err.textContent = "硬件能力不满足,无法继续";
      card.appendChild(err);
    }
    body.appendChild(card);

    const card2 = _ce("div", WIZARD_NS + "env-card");
    card2.appendChild(_ce("h3", null, "软件能力 / 模型"));

    // --- 单卡提示块 ---------------------------------------------------- //
    if (singleOn && blocked) {
      // 刚需两个模型同时在位（监控分析检索）→ 单卡承载不了。
      card2.appendChild(_singleCardNotice(
        "block",
        "⚠ 当前设备仅检测到 1 张计算卡，本应用无法使用",
        [
          `「${(state.app && state.app.title) || "本应用"}」需要同时启用 ` +
            `${swRequired.length} 个模型（${swRequired.join("、")}），` +
            "一张计算卡放不下两个模型（同卡叠模型会加载失败）。",
          "请在「算力配置」页或硬件上增加计算卡后再使用本应用。",
        ]));
    } else if (singleOn && single.pickable) {
      // 多 capability 但可二选一（音视频描述）→ 让用户选一边。
      card2.appendChild(_singleCardNotice(
        "warn",
        "⚠ 当前设备仅检测到 1 张计算卡，本应用的完整功能需要 2 张计算卡",
        ["单张计算卡无法同时承载两个模型，请在下面二选一："]));
      const pickBox = _ce("div", WIZARD_NS + "single-pick");
      // 模型名从**原始** cfg 取（``cfg`` 此时已经是裁剪过的 —— 只剩选中的
      // 那条），否则未选中项会显示成「（加载 ?）」。
      const pickSrc = (single.baseCfg && single.baseCfg.swcap_to_model)
        || cfg.swcap_to_model || {};
      single.pickable.forEach((p) => {
        const m = pickSrc[p.cap] || {};
        const label = _ce("label", WIZARD_NS + "single-pick-item");
        const radio = _ce("input");
        radio.type = "radio";
        radio.name = WIZARD_NS + "single-pick";
        radio.value = p.cap;
        radio.checked = (p.cap === single.choice);
        radio.addEventListener("change", () => {
          if (!radio.checked) return;
          _setSingleCardChoice(single, p.cap);
          // 重渲染当前步骤：capability 列表的 ✓/✗ 和「一键开启」按钮
          // 都要跟着这次选择变（换的是要加载的模型）。
          _gotoStep(_active.currentStep);
        });
        label.appendChild(radio);
        label.appendChild(_ce("span", WIZARD_NS + "single-pick-text",
          p.label || p.cap));
        label.appendChild(_ce("span", WIZARD_NS + "single-pick-model",
          `（加载 ${m.model_id || "?"}）`));
        pickBox.appendChild(label);
      });
      card2.appendChild(pickBox);
    }
    // 单 capability 的应用（多数应用）**不显示任何单卡提示** —— 它们在
    // 单卡上的行为与双卡完全一样（模型照常加载，只是落到唯一那张卡），
    // 没必要打扰用户。只有「完整功能需要 2 张卡」的应用才值得提示。
    // ------------------------------------------------------------------ //

    swRequired.forEach((cap) => {
      // 被阻断时两条都显示为未满足（说明为什么不可用），而不是把已跑起
      // 的那条显示成 ✓ —— 那会让人以为还差一点就能用。
      const ok = blocked ? false : !swMissing.includes(cap);
      const row = _ce("div", WIZARD_NS + "env-row");
      row.classList.add(ok ? WIZARD_NS + "env-ok" : WIZARD_NS + "env-fail");
      row.textContent = (ok ? "✓ " : "✗ ") + cap;
      card2.appendChild(row);
    });
    // vd 二选一时把没选中的那条也列出来(中性样式)—— 用户得看到
    // 「另一个功能在单卡下被关掉了」。
    if (singleOn && single.pickable) {
      single.pickable.forEach((p) => {
        if (p.cap === single.choice) return;
        const row = _ce("div", WIZARD_NS + "env-row " + WIZARD_NS + "env-off");
        row.textContent = "— " + p.cap + "（单卡下未启用）";
        card2.appendChild(row);
      });
    }
    if (!swRequired.length) {
      card2.appendChild(_envNoReqRow("本应用不依赖模型 / 软件能力"));
    }
    if (!swOK && !blocked) {
      const bringBtn = _ce("button", WIZARD_NS + "primary-btn", "一键开启所需模型");
      bringBtn.id = WIZARD_NS + "env-bring-btn";
      const status = _ce("div", WIZARD_NS + "env-bring-status");
      status.id = WIZARD_NS + "env-bring-status";
      card2.appendChild(bringBtn);
      card2.appendChild(status);

      // 一键开启 handler(2 次重试 + confirm 兜底)
      let retryCount = 0;
      const MAX_RETRY = 2;
      bringBtn.addEventListener("click", async () => {
        bringBtn.disabled = true;
        const doBringUp = async () => {
          status.textContent = "正在开启模型...";
          const ok = await window._wizardBringUpModels(cfg);
          return ok;
        };
        for (let i = 0; i < MAX_RETRY; i++) {
          const ok = await doBringUp();
          if (ok) {
            // 启动指令已成功发出(后端 200),模型后台加载中。立即放行
            // 到下一步,不阻塞用户填字段。finish 步骤会再等 running。
            status.textContent = "✓ 已开启(后台加载中,可继续下一步)";
            setTimeout(() => stepForward(), 600); // 短延时让用户看到状态
            return;
          }
          retryCount++;
          status.textContent = `开启失败,正在重试 (${retryCount}/${MAX_RETRY})...`;
        }
        // 2 次都失败
        const cont = window.confirm(
          "一键开启连续 " + MAX_RETRY + " 次失败,建议重启板子。\n\n" +
          "点击'确定'保留当前步骤(手动开启后可重试),点击'取消'退出向导。"
        );
        if (!cont) {
          skip(); // 用户选择退出
        } else {
          status.textContent = "已保留当前步骤,请手动开启后重试";
          bringBtn.disabled = false;
        }
      });
    }
    body.appendChild(card2);

    // footer
    // blocked（单卡 + 刚需多模型）→ 「下一步」禁用，用户只能「跳过向导」
    // 离开（后续版本该应用会改造成单 capability，届时阻断自然消失）。
    _renderFootNext(foot, cfg, state, hwOK && swOK && !blocked);
  }

  async function _probeEnv(cfg, app) {
    // HW: 通过 manifest.hardware_capabilities + 已挂载 — 框架不强求 board HW;
    //     此处简化为"manifest.hardware_capabilities 数组非空即视为 OK",
    //     严格 HW 探测留给具体应用在 applyWizardSnapshot 内 assert。
    const hwRequired = (app.software_capabilities || []).length > 0
      ? (app.hardware_capabilities || [])
      : (app.hardware_capabilities || []);
    // SW: 拉 /api/compute 看本机 running instances 是否覆盖 manifest.software_capabilities。
    // 只算本机算力(spawned / adopted_local):peer adopted 是远端板子的
    // NPU,本机未"开启";误把它算进 running 会让用户在未启本地算力
    // 时看到 ✓,与同文件 _wizardBringUpModels 跳过 adopted 的语义不对称
    // —— 后者正确防误关 peer 模型,前者却让 UI 显示误导。
    let swMissing = [];
    let runningCaps = new Set();
    let cardCount = 0;
    let modelCards = [];
    try {
      const r = await fetch("/api/compute", { cache: "no-store" });
      if (r.ok) {
        const data = await r.json();
        for (const inst of (data.instances || [])) {
          if (inst.status === "running" && inst._source !== "adopted") {
            for (const c of (inst.capability_types || [])) runningCaps.add(c);
          }
        }
        // 模型卡位数（能跑 rknn 模型的卡）。1 → 单卡板，2 → 双卡板。
        // 探测失败留 0 —— 0 不等于 1,单卡分支不会被误触发。
        cardCount = (typeof data.card_count === "number") ? data.card_count : 0;
        modelCards = Array.isArray(data.model_cards) ? data.model_cards : [];
      }
    } catch (_) { /* keep all missing */ }
    for (const cap of (app.software_capabilities || [])) {
      if (!runningCaps.has(cap)) swMissing.push(cap);
    }
    return {
      hwRequired,
      hwMissing: [], // 见上
      swRequired: app.software_capabilities || [],
      swMissing,
      cardCount,
      modelCards,
    };
  }

  // 裁掉与当前选择无关的步骤。声明在 wizard.json 的
  // single_card_pick[].skip_steps 里（应用专属知识留在卡内，不进框架）：
  // vd 选「视频描述」→ 裁 "audio" (音频理解提示词)；选「音频描述」→
  // 裁 "detect" (检测提示词 + 检测尺寸)。
  //
  // 被裁步骤的字段不进 _finish 构造的 opts → vd 的 applyWizardSnapshot
  // `if (opts.audioPrompt)` / `if (opts.prompt)` 自然不成立 → 那个功能
  // 压根没被请求（不是把弹窗藏了）。
  function _stepsWithout(cfg, pick) {
    const skip = new Set(((pick && pick.skip_steps) || []));
    const steps = cfg.steps || [];
    if (!skip.size) return steps;
    return steps.filter((s) => !skip.has(s.id));
  }

  // 裁剪后的 cfg:swcap_to_model 只留要用的那条,steps 裁掉无关步骤。
  function _singleEffCfg(cfg, pick, npu) {
    const cap = pick.cap;
    return {
      ...cfg,
      swcap_to_model: {
        [cap]: { ...cfg.swcap_to_model[cap], npu_index: npu },
      },
      steps: _stepsWithout(cfg, pick),
    };
  }

  // ------------------------------------------------------------------ //
  // 单卡策略（v0.24）                                                   //
  // ------------------------------------------------------------------ //
  // 一张 RK1828 协卡放不下两个模型（AGENTS.md v0.17 实测：同卡叠两个
  // 模型 rc=-6）。单卡板上必须降级：
  //
  //   * 单 capability 的应用 —— 模型照常加载，只是落到唯一那张卡上。
  //   * 多 capability 的应用 —— 要么让用户二选一（vd），要么直接不可用
  //     （刚需两个模型同时在位，单卡承载不了）。
  //
  // 实现手法：单卡时把 `_active.cfg` 换成一份**裁剪过的副本** ——
  // `swcap_to_model` 只留本应用单卡下真正要用的那条，`npu_index` 指向
  // 实际卡位，`steps` 裁掉与该选择无关的步骤。下游三处消费点（finish
  // 等待 / 一键开启 / 进度条）全部从 `_active.cfg` 取值，于是**一行都
  // 不用改**就自动按单卡语义工作。
  //
  // 返回 null = 双卡（或探测失败）→ 调用方保持原路径，行为逐字不变。
  function _planSingleCard(cfg, app, envCheck) {
    if (!envCheck || envCheck.cardCount !== 1) return null;
    const declared = Object.keys(cfg.swcap_to_model || {});
    // 实际卡位。正常就是 NPU0；万一枚举顺序变了（可跑模型的卡不是索引 0），
    // 这里跟着走 —— 单卡板上也可能不是 0。
    const npu = (envCheck.modelCards && envCheck.modelCards[0] != null)
      ? envCheck.modelCards[0] : 0;

    // 单 capability（多数应用）：原样保留，只换卡号。
    if (declared.length <= 1) {
      return {
        active: true, blocked: false, baseCfg: cfg, npu,
        requiredCaps: declared,
        pickable: null, choice: null,
        effCfg: declared.length
          ? _singleEffCfg(cfg, { cap: declared[0] }, npu)
          : { ...cfg, swcap_to_model: {} },
      };
    }

    // 多 capability：应用自己声明「单卡下能二选一的是哪两个」（wizard.json
    // 的 single_card_pick，可带 skip_steps）。声明了就让用户选；没声明
    // = 刚需多模型 → 单卡不可用。
    const pick = Array.isArray(cfg.single_card_pick) ? cfg.single_card_pick : [];
    const usable = pick.filter(
      (p) => p && p.cap && cfg.swcap_to_model && cfg.swcap_to_model[p.cap]);
    if (usable.length >= 2) {
      const def = usable[0].cap;          // 默认选第一个（vd = 视频描述）
      const defPick = usable.find((p) => p.cap === def);
      return {
        active: true, blocked: false, baseCfg: cfg, npu,
        requiredCaps: [def],
        pickable: usable, choice: def,
        effCfg: _singleEffCfg(cfg, defPick, npu),
      };
    }

    return {
      active: true, blocked: true, baseCfg: cfg, npu,
      requiredCaps: declared,
      pickable: null, choice: null,
      effCfg: null,
    };
  }

  // 切换二选一（vd）：重建裁剪后的 cfg 并重渲染当前步骤 ——
  // capability 列表的 ✓/✗、「一键开启所需模型」按钮、以及**步骤表**都要
  // 跟着变(选了音频描述 → 「检测提示词 + 检测尺寸」这一步整个消失)。
  function _setSingleCardChoice(single, cap) {
    if (!single || !single.pickable || !single.baseCfg) return;
    const base = single.baseCfg;
    const pick = single.pickable.find((p) => p.cap === cap);
    if (!pick) return;
    single.choice = cap;
    single.requiredCaps = [cap];
    single.effCfg = _singleEffCfg(base, pick, single.npu);
    if (_active) _active.cfg = single.effCfg;
  }

  // 构造 toggle switch 元素(checkbox type)— 抽出来便于 source row 内复用
  function _buildToggle(field, state) {
    const cur = state.values[field.id];
    const defaultChecked = (cur != null) ? !!cur : !!field.default;
    const toggleLbl = _ce("label", WIZARD_NS + "toggle");
    toggleLbl.dataset.fieldId = field.id;
    const cbInput = _ce("input", WIZARD_NS + "toggle-input");
    cbInput.type = "checkbox";
    cbInput.checked = defaultChecked;
    const track = _ce("span", WIZARD_NS + "toggle-track");
    const knob = _ce("span", WIZARD_NS + "toggle-knob");
    track.appendChild(knob);
    const toggleText = _ce("span", WIZARD_NS + "toggle-text", field.label || field.id);
    toggleLbl.appendChild(cbInput);
    toggleLbl.appendChild(track);
    toggleLbl.appendChild(toggleText);
    return toggleLbl;
  }

  function _renderFormStep(body, foot, cfg, stepDef, state) {
    body.innerHTML = "";
    const titleEl = _ce("h3", WIZARD_NS + "step-title", stepDef.title);
    body.appendChild(titleEl);
    const form = _ce("div", WIZARD_NS + "form");
    body.appendChild(form);

    // 找出"会被 inline 进 source 行"的 loop toggle(同 stepDef 内 -loop
    // 字段),避免后续 forEach 重复渲染。判定走 _isSourceFieldId。
    const inlineLoopFieldId = (() => {
      const hasSource = (stepDef.fields || []).some(f =>
        f.type !== "checkbox" && _isSourceFieldId(f.id));
      if (!hasSource) return null;
      const loopField = (stepDef.fields || []).find(f =>
        f.type === "checkbox" && /-loop$/.test(f.id));
      return loopField ? loopField.id : null;
    })();

    (stepDef.fields || []).forEach((field) => {
      // 已经 inline 进 source 行的 loop 字段 → 跳过独立渲染
      if (field.id === inlineLoopFieldId) return;

      const wrap = _ce("div", WIZARD_NS + "field");
      const lbl = _ce("label", WIZARD_NS + "field-label", field.label || field.id);
      wrap.appendChild(lbl);
      let el;
      const cur = state.values[field.id];
      switch (field.type) {
        case "textarea":
          el = _ce("textarea", WIZARD_NS + "input");
          if (field.placeholder) el.placeholder = field.placeholder;
          if (cur != null) el.value = cur;
          break;
        case "number":
          el = _ce("input", WIZARD_NS + "input");
          el.type = "number";
          // 优先级：已存值 > field.default > placeholder（不写死空框）——
          // 之前这里只接 ``cur``，新会话 state.values 是空的，输入框就是个
          // 啥都没有的白框，用户反馈"向导里没默认值"。补 default 之后再
          // _apply 才会把它推给后端。
          if (cur != null) el.value = cur;
          else if (field.default != null) el.value = field.default;
          if (field.placeholder) el.placeholder = field.placeholder;
          if (field.min != null) el.min = field.min;
          if (field.max != null) el.max = field.max;
          if (field.step != null) el.step = field.step;
          break;
        case "checkbox":
          el = _buildToggle(field, state);
          break;
        case "select":
          el = _ce("select", WIZARD_NS + "input");
          (field.options || []).forEach((opt) => {
            const o = _ce("option");
            o.value = opt.value;
            o.textContent = opt.label || opt.value;
            if ((cur != null ? String(cur) : (opt.selected ? opt.value : null)) === opt.value) {
              o.selected = true;
            }
            el.appendChild(o);
          });
          break;
        case "prompt_rows":
          // 多行 prompt 输入 UI(类似 vtl 的"定位提示词"):rows 每行一个
          // text input,初始 1 行,可点"+"加到 max_rows,点"−"删到
          // min_rows。值存为字符串数组,opts[ui_key] = [v1, v2, ...]。
          // 复用 framework.css 已有的 .vtl-prompt-* 类(已在应用页面用),
          // wizard 这边保持视觉一致。
          el = _ce("div", "vtl-prompt-rows");
          el.dataset.fieldId = field.id;
          el.dataset.fieldType = "prompt_rows";
          const minRows = Number.isFinite(field.min_rows) ? field.min_rows : 1;
          const maxRows = Number.isFinite(field.max_rows) ? field.max_rows : 20;
          // 行容器(已有 prompt 数组 → 恢复;否则 1 行)
          const initial = Array.isArray(cur) && cur.length >= minRows
            ? cur.slice(0, maxRows)
            : [""];
          const wizardAddPromptRow = (val) => {
            const row = _ce("div", "vtl-prompt-row is-open");
            row.innerHTML = `
              <div class="vtl-prompt-row-inner">
                <span class="vtl-prompt-row-num"></span>
                <input type="text" class="vtl-prompt-row-input"
                       maxlength="200" spellcheck="false"
                       placeholder="${(field.placeholder || "").replace(/"/g, "&quot;")}" />
                <button type="button" class="vtl-prompt-row-remove vd-btn vd-btn-mini"
                        title="删除该行" aria-label="删除该行">−</button>
              </div>`;
            const input = row.querySelector(".vtl-prompt-row-input");
            if (val) input.value = val;
            el.appendChild(row);
          };
          initial.forEach(wizardAddPromptRow);
          // toolbar: 添加按钮 + 计数
          const toolbar = _ce("div", "vtl-prompt-toolbar");
          const addBtn = _ce("button", "vd-btn vtl-prompt-add", "+ 添加输入框");
          addBtn.type = "button";
          const hintEl = _ce("span", "vtl-prompt-hint");
          toolbar.appendChild(addBtn);
          toolbar.appendChild(hintEl);
          // 容器 wrap(挂载 el + toolbar)— wrap 是 _renderFormStep 后续的
          // 统一容器,带 label;toolbar 紧跟 el。
          wrap.appendChild(toolbar);
          // 行内 helper:重排编号 + 更新 add 按钮 disabled + 更新 hint
          const renumber = () => {
            el.querySelectorAll(".vtl-prompt-row-num").forEach((n, i) => {
              n.textContent = String(i + 1);
            });
            const cnt = el.querySelectorAll(".vtl-prompt-row").length;
            hintEl.textContent = `${cnt} / ${maxRows}`;
            addBtn.disabled = cnt >= maxRows;
          };
          // 行内 helper:同步 remove 按钮 disabled(MIN_ROWS 时禁)
          const syncRemoveBtns = () => {
            const cnt = el.querySelectorAll(".vtl-prompt-row").length;
            el.querySelectorAll(".vtl-prompt-row-remove").forEach((b) => {
              b.disabled = cnt <= minRows;
            });
          };
          // 增行
          addBtn.addEventListener("click", () => {
            const cnt = el.querySelectorAll(".vtl-prompt-row").length;
            if (cnt >= maxRows) return;
            wizardAddPromptRow("");
            renumber();
            syncRemoveBtns();
          });
          // 删行(直接 DOM remove — 跳过应用页 vtlPromptRemoveRow
          // 的折叠动画,wizard 卸载/下一步时 DOM 会整体被替换)
          el.addEventListener("click", (e) => {
            const btn = e.target.closest(".vtl-prompt-row-remove");
            if (!btn) return;
            const row = btn.closest(".vtl-prompt-row");
            const cnt = el.querySelectorAll(".vtl-prompt-row").length;
            if (!row || cnt <= minRows) return;
            row.parentNode.removeChild(row);
            renumber();
            syncRemoveBtns();
          });
          renumber();
          syncRemoveBtns();
          break;
        default: // text
          el = _ce("input", WIZARD_NS + "input");
          el.type = "text";
          if (field.placeholder) el.placeholder = field.placeholder;
          if (cur != null) el.value = cur;
      }
      // toggle / 复合容器(已经设了 data-field-id)无需再设
      if (!el.dataset.fieldId) el.dataset.fieldId = field.id;
      _bindFieldLive(cfg, state, field, el);
      // 视频源字段自动追加"浏览…"按钮 + 同 stepDef 内 -loop toggle。
      //
      // 两个约束缺一不可：
      // * **textarea 也要给按钮** —— 多路视频源是一行一路的文本框，而
      //   FSModal._commit 对 textarea 是**追加一行**不是整体替换，选一个
      //   文件就是往源列表加一路。此前把 textarea 排除在外，等于多路卡的
      //   向导里浏览按钮直接消失。
      // * 复数结尾 —— 见 _isSourceFieldId。
      const isSource = field.type !== "number"
          && field.type !== "checkbox" && field.type !== "select"
          && _isSourceFieldId(field.id);
      if (isSource && window.VdFsModal) {
        const row = _ce("div", WIZARD_NS + "input-row");
        // textarea 占满整行，按钮行排在它下面（塞进同一行会被压成一行宽）。
        (field.type === "textarea" ? wrap : row).appendChild(el);
        const browseBtn = _ce("button", WIZARD_NS + "browse-btn", "浏览…");
        browseBtn.type = "button";
        browseBtn.title = "选择本地文件或目录";
        browseBtn.addEventListener("click", () => {
          try {
            window.VdFsModal._sourceTarget = el;
            window.VdFsModal.open("");
          } catch (e) {
            console.warn("[wizard-state] browse failed:", e);
          }
        });
        row.appendChild(browseBtn);
        wrap.appendChild(row);
        // 视频源下方追加"打开 / 关闭"按钮(wizard.json 的 open_source_button
        // 字段声明应用页面对应的按钮 id;close 按钮 id 按 "*-open → *-close"
        // 约定推断,避免每个 wizard.json 多写一份)。按钮 click 走应用页面
        // 原生按钮(应用页面打开按钮已经把 wizard 的 source/loop 写回 +
        // 调 vdOpen + UI 反馈全套逻辑,wizard 不重写)。
        if (cfg.open_source_button) {
          const closeId = cfg.open_source_button.replace(/-open$/, "-close");
          // 读 wizard 当前 toggle 状态(input 直接读,el 是 wizard source input)
          const readLoopChecked = () => {
            if (!inlineLoopFieldId) return false;
            const tCb = row.querySelector(".wd-wizard-toggle-input");
            return !!(tCb && tCb.checked);
          };
          const actions = _ce("div", WIZARD_NS + "source-actions");
          const openBtn = _ce("button", WIZARD_NS + "source-action-btn", "打开");
          openBtn.type = "button";
          openBtn.id = WIZARD_NS + "source-open-" + field.id;
          // 点击"打开"后,wizard 容器内需要展示视频。应用页面的 canvas 元素
          // (id 形如 <prefix>-canvas；多路卡用 wizard.json 的 canvas_id 显式声明)
          // 在 page section 内、被 wizard hidden;点击"打开"时把 canvas 元素
          // 直接挪到 wizard 容器内,后续 ws callback 通过 getElementById 仍能
          // 找到它(节点对象没变,只是父节点换了)— 不重写 ws 推帧逻辑。
          // 画面容器 id：默认按 open_source_button 去掉 -open 加 -canvas 推导。
          // 多路卡（vtd / vtl / vd）的画面是宫格 div 而不是 canvas，推导出的
          // `*-canvas` 根本不存在 → 画面永远搬不进向导面板（而且不报错，
          // 静默地什么都不发生）。所以允许 wizard.json 用 `canvas_id` 显式
          // 声明，推导只作为单路老卡片的兜底。
          const canvasId = cfg.canvas_id
            || cfg.open_source_button.replace(/-open$/, "-canvas");
          // 「浏览 / 打开 / 关闭」合并进同一个 row —— 与应用页面的按钮行同款。
          // 原先浏览一行、打开/关闭另一行，中间还留上下边距，读起来像两组
          // 不相干的控件。canvas-wrap 仍挂在 wrap 上，排在按钮行下方。
          row.appendChild(actions);
          const canvasWrap = _ce("div", WIZARD_NS + "canvas-wrap");
          wrap.appendChild(canvasWrap);

          // 没播放时:显示拖放区(若 cfg.upload_endpoint 声明了上传端点)
          // 拖动视频文件到此处 → POST 上传 → 把 server 路径写到 source
          //        input + 自动调 vd-open(VDOpen 把 canvas 搬进 wrap 替换)
          const dropZone = _ce("div", WIZARD_NS + "drop-zone");
          const dropIcon = _ce("div", WIZARD_NS + "drop-icon", "📥");
          const dropText = _ce("div", WIZARD_NS + "drop-text", "拖动文件放到此处");
          const dropHint = _ce("div", WIZARD_NS + "drop-hint", "或点击「浏览」选择本地视频");
          dropZone.appendChild(dropIcon);
          dropZone.appendChild(dropText);
          dropZone.appendChild(dropHint);
          const dropStatus = _ce("div", WIZARD_NS + "drop-status");
          dropZone.appendChild(dropStatus);
          // 没有 upload_endpoint 时的静态占位（见下方 else 分支）。也要能被
          // moveCanvasToWizard 清掉，所以提到分支外声明。
          let noUp = null;
          if (cfg.upload_endpoint) {
            canvasWrap.appendChild(dropZone);

            const handleUpload = async (file) => {
              dropStatus.textContent = `正在上传 ${file.name}...`;
              dropStatus.className = WIZARD_NS + "drop-status " + WIZARD_NS + "drop-status-busy";
              // 上传在途:锁住「下一步」/「打开」(见文件头守卫注释)
              _setUploadBusy(state, true, field.id);
              try {
                const fd = new FormData();
                fd.append("file", file);
                const r = await fetch(cfg.upload_endpoint, { method: "POST", body: fd });
                if (!r.ok) {
                  dropStatus.textContent = `上传失败: HTTP ${r.status}`;
                  dropStatus.className = WIZARD_NS + "drop-status " + WIZARD_NS + "drop-status-err";
                  return;
                }
                const data = await r.json();
                if (!data.path) {
                  dropStatus.textContent = `上传失败: 后端未返回 path`;
                  dropStatus.className = WIZARD_NS + "drop-status " + WIZARD_NS + "drop-status-err";
                  return;
                }
                // 权威值直写 state.values —— 即使 DOM 之后被重建/销毁,
                // finish 读到的仍是上传后的路径;localStorage 同步写,
                // 避免下次进向导又回到旧值。
                state.values[field.id] = data.path;
                _publishValues(state, cfg);

                // 向导已卸载 / 已跳步 → 不再对游离 DOM 做副作用
                if (!_isStepAlive(state, el)) return;

                // 把 server 路径写回 wizard input + 自动触发 vd-open
                // vd-open click → 后端 vdOpen → ws 推帧 → ws callback
                // drawVdFrame 通过 canvasId 找到 canvas 画帧(此时 canvas
                // 还在 page section 内)
                el.value = data.path;
                const loopEl = inlineLoopFieldId
                  ? document.getElementById(inlineLoopFieldId) : null;
                if (loopEl) loopEl.checked = readLoopChecked();
                const appSrcEl = document.getElementById(field.id);
                if (appSrcEl) appSrcEl.value = data.path;
                document.getElementById(cfg.open_source_button)?.click();
                // 把 canvas 搬到 wizard 内(替换 drop-zone)
                moveCanvasToWizard();
                dropStatus.textContent = `✓ 已上传,加载中...`;
                dropStatus.className = WIZARD_NS + "drop-status " + WIZARD_NS + "drop-status-ok";
              } catch (e) {
                // 节点已不在文档中时无需刷状态
                if (_isStepAlive(state, el)) {
                  dropStatus.textContent = `上传失败: ${e?.message || e}`;
                  dropStatus.className = WIZARD_NS + "drop-status " + WIZARD_NS + "drop-status-err";
                }
              } finally {
                _setUploadBusy(state, false, field.id);
              }
            };

            // 拖放视觉反馈
            canvasWrap.addEventListener("dragover", (e) => {
              if (!e.dataTransfer?.types?.includes?.("Files")) return;
              e.preventDefault();
              canvasWrap.classList.add(WIZARD_NS + "dragover");
            });
            canvasWrap.addEventListener("dragleave", () => {
              canvasWrap.classList.remove(WIZARD_NS + "dragover");
            });
            canvasWrap.addEventListener("drop", (e) => {
              if (!e.dataTransfer?.files?.length) return;
              e.preventDefault();
              canvasWrap.classList.remove(WIZARD_NS + "dragover");
              handleUpload(e.dataTransfer.files[0]);
            });
            // 点 dropZone 也触发应用页面的浏览按钮(用户也可以点上传)
            dropZone.addEventListener("click", (e) => {
              // 不让 click 冒泡触发 dragover/drop
              if (e.target === dropZone) {
                const browseBtn = row.querySelector("." + WIZARD_NS + "browse-btn");
                browseBtn?.click();
              }
            });
          } else {
            // 没声明 upload_endpoint,给一个静态 placeholder
            noUp = _ce("div", WIZARD_NS + "drop-text", "点击「打开」开始播放");
            canvasWrap.appendChild(noUp);
          }

          const moveCanvasToWizard = () => {
            const canvas = document.getElementById(canvasId);
            if (!canvas || canvas.parentNode === canvasWrap) return;
            // 记下原 parent **和它后面的兄弟**，unmount() 据此按原位搬回
            // （appendChild 只能还原到末尾，见 _restoreCanvas）。不记的话
            // wizard 容器销毁会带走 canvas，应用页画面变黑。
            const originalParent = canvas.parentNode;
            const nextSibling = canvas.nextSibling;
            // 把 canvas 搬到 wrap 顶部,移除拖放区
            canvasWrap.appendChild(canvas);
            // 占位元素一律清掉 —— 画面已经在播了,「点击打开开始播放」还
            // 压在画面上是 bug。此前只在 `cfg.upload_endpoint` 为真时移除
            // dropZone,于是**没有上传端点的卡**(如阈值标定卡)占位文字会
            // 永久残留在 canvas 上面,左右还多出一块灰色空隙。
            if (dropZone.parentNode === canvasWrap) {
              canvasWrap.removeChild(dropZone);
            }
            if (noUp && noUp.parentNode === canvasWrap) {
              canvasWrap.removeChild(noUp);
            }
            // 记录到 _active,unmount() 时按原位还原。
            // **只记第一次**：切到后面几步再切回来时 body 会重渲染出新的
            // canvasWrap，画面还挂在旧的、已脱离文档的 wrap 上，此时覆盖
            // 记录会让 unmount 把画面还原进那个废弃的 wrap，应用页再无画面。
            if (_active && !_active.movedCanvas) {
              _active.movedCanvas = { canvas, originalParent, nextSibling };
            }
          };
          openBtn.addEventListener("click", () => {
            // 上传在途时点「打开」会放旧源,同「下一步」一并锁住
            if (_active && _active.uploadBusy) { _flashUploadHint(); return; }
            try {
              const srcEl = document.getElementById(field.id);
              const loopEl = inlineLoopFieldId
                ? document.getElementById(inlineLoopFieldId) : null;
              if (srcEl) srcEl.value = el.value || "";
              if (loopEl) loopEl.checked = readLoopChecked();
              document.getElementById(cfg.open_source_button)?.click();
              // 等 vd-open 的同步逻辑跑完再搬(下一帧前 canvas 已在 wizard 内,
              // ws 推过来的 jpg 直接画到 wizard 内的 canvas)
              moveCanvasToWizard();
            } catch (e) {
              console.warn("[wizard-state] open failed:", e);
            }
          });
          const closeBtn = _ce("button", WIZARD_NS + "source-action-btn", "关闭");
          closeBtn.type = "button";
          closeBtn.addEventListener("click", () => {
            try {
              document.getElementById(closeId)?.click();
              // 与 unmount 共用 _restoreCanvas。原先这里另写了一套（现查
              // .vd-stage 再插到最前），对多路卡是错的：画面已搬进向导、
              // 根本不在 pageSection 里，querySelector 落空后退化成把画面
              // 塞成 page 的第一个子元素；没点过「打开」时更是无条件把它从
              // .vd-stage-col 拽走。
              // 另：本函数签名是 (body, foot, cfg, stepDef, state)，闭包里
              // 没有 app —— 此前写裸 `app.id` 一进 handler 就 ReferenceError，
              // 被 catch 吞成 warn，表现为「关闭」点了没反应。
              if (_active) _restoreCanvas(_active.movedCanvas);
            } catch (e) {
              console.warn("[wizard-state] close failed:", e);
            }
          });
          actions.appendChild(openBtn);
          actions.appendChild(closeBtn);
        }
        // 循环播放开关**追加在按钮行末尾**（浏览 / 打开 / 关闭 之后），与
        // 应用页面同款顺序。放在 B 之外、B 之后：既要排在 actions 后面，
        // 又要在没有 open_source_button 的卡上照样出现。
        if (inlineLoopFieldId) {
          const loopField = (stepDef.fields || []).find(f => f.id === inlineLoopFieldId);
          if (loopField) row.appendChild(_buildToggle(loopField, state));
        }
      } else {
        // **非视频源字段**（判据算法 / 阈值 / 帧间隔、检索问题…）走这里。
        // 这个 else 归属的是外层 `if (isSource && …)` 而不是
        // `if (cfg.open_source_button)` —— 挂错的话这些字段会被创建却
        // 永远不进 DOM：不报错、元素在、页面上就是没有。
        wrap.appendChild(el);
      }
      form.appendChild(wrap);
    });

    // **多路视频卡 source step 末尾追加 channel hint + 判据分组**：
    // 用户在向导内点 tile (vd-grid / vtd-grid / vtl-grid) 想立刻看到"当前
    // 在编辑 [x, y] 路"，否则 tile 切换没有可见反馈。仅当 stepDef 含视频源
    // 字段（_isSourceFieldId）且声明了 canvas_id 时才挂；单路老卡片、纯
    // 文本 form step 都跳过。
    //
    // **判据字段分组**（mode / threshold / every_n）→ 用 ``<fieldset>`` 把
    // 它们围起来；channel hint 放进 ``<legend>``，浏览器原生"嵌入框上沿"的
    // 视觉，不用 CSS hack 模拟。三步：(1) 找已渲染的 -judge-* wrap；
    // (2) 创建 fieldset + legend + 三个 wrap；(3) fieldset 插到原第一个
    // judge-wrap 的位置 + 删掉原三个 wrap。如果 stepDef 里压根没判据字段
    // （老卡片、字段名自定义），channel hint 不挂；剩下的判据字段留在原
    // 位（不强行搬）。
    const hasSourceField = (stepDef.fields || []).some(
      (f) => f && _isSourceFieldId(f.id));
    if (hasSourceField && cfg.canvas_id) {
      const prefix = String(cfg.canvas_id).replace(/-grid$/, "");
      const judgeFieldIds = [
        prefix + "-judge-mode",
        prefix + "-judge-threshold",
        prefix + "-judge-every-n",
      ];
      // 找已渲染的判据 wrap（按 data-field-id）+ 它们的插入锚点
      const judgeWraps = judgeFieldIds
        .map((fid) => {
          // data-field-id 设在 input/select 自身（不是 wrap），先找到 input
          // 再 closest 到 .wd-wizard-field 拿 wrap。直接搬 input 会让
          // label 留在 form 原位、视觉上 label/input 分离（已踩过这个坑）。
          const inp = form.querySelector("[data-field-id=\"" + fid + "\"]");
          return inp ? inp.closest("." + WIZARD_NS + "field") : null;
        })
        .filter(Boolean);
      const anchorWrap = judgeWraps[0];
      if (judgeWraps.length && anchorWrap) {
        // fieldset + legend（hint 文字放 legend 里 —— 浏览器原生"嵌入框上沿"）
        const fs = _ce("fieldset", WIZARD_NS + "judge-fs");
        // **稳定 id** —— frame-judge.js 的 mountInWizard(prefix) 用这个 id
        // 找到这个 fieldset，把 .vj-curve 元素从 body 搬进来（曲线原本是
        // position:fixed 浮在画布下面，要让它跟着 fieldset 自然流动才
        // 能进 wizard 的判据框）。id 必须有 prefix —— 同页面不会撞车，
        // 多次 mount 也总是最新的那个生效（旧的 wizard 卸载时 DOM 一起没了）。
        fs.id = prefix + "-wizard-judge-fs";
        const lg = _ce("legend", WIZARD_NS + "judge-legend");
        lg.appendChild(_ce("span", WIZARD_NS + "channel-hint-label",
                           "当前在编辑："));
        const val = _ce("span", WIZARD_NS + "channel-hint-value",
                        "未选中（点画面里任一格）");
        val.id = prefix + "-wizard-channel-value";
        lg.appendChild(val);
        fs.appendChild(lg);
        // 关键顺序：先 insertBefore(fs, anchorWrap) —— 此时 anchorWrap 还在
        // form 里（wrap 在兄弟节点位置上的引用有效），fs 被插到 anchorWrap
        // 之前；再 forEach 搬 wrap 进 fs，anchorWrap 被搬走、fs 顺势顶到
        // 原 anchorWrap 的位置。视觉上 fs 替换了 anchorWrap 的"行首位置"。
        // 不能用 anchorWrap.nextSibling 路径：HTML 源码里 wrap 之间有
        // 换行 / 空白 TextNode，TextNode.parentNode 是 form 本应 OK，但
        // anchorWrap 被搬走时 form 节点结构变化会让 nextSibling 校验失败。
        form.insertBefore(fs, anchorWrap);
        judgeWraps.forEach((w) => fs.appendChild(w));
      } else {
        // 没找到任何判据字段（罕见，比如 wizard.json 改了字段名），仍挂一
        // 个独立 hint 行 —— 不要因为判据字段缺失就不显示"当前在编辑"，否则
        // 用户连这个反馈都没有。
        const hint = _ce("div", WIZARD_NS + "channel-hint");
        hint.id = prefix + "-wizard-channel-hint";
        hint.dataset.pfx = prefix;
        hint.appendChild(_ce("span", WIZARD_NS + "channel-hint-label",
                             "当前在编辑："));
        const val = _ce("span", WIZARD_NS + "channel-hint-value",
                        "未选中（点画面里任一格）");
        val.id = prefix + "-wizard-channel-value";
        hint.appendChild(val);
        form.appendChild(hint);
      }
      // 拼 frame-judge 的 setOnSelectChannel —— 用户在向导内点 tile 即时
      // 更新。FrameJudge 是按页面加载的异步模块，wizard 打开时它可能还没
      // 装好 → 短轮询挂上，避免竞态。注意：上面 if/else 分支里 val.id
      // 都设了同一个 prefix + "-wizard-channel-value"，handler 一定能找到
      // 对应节点（fieldset 的 legend 在文档中依然是 #val 节点可被查到）。
      const channelValueEl = document.getElementById(prefix + "-wizard-channel-value");
      if (channelValueEl) {
        const attachOnSelect = () => {
          if (!window.FrameJudge || !window.FrameJudge.setOnSelectChannel) {
            setTimeout(attachOnSelect, 30);
            return;
          }
          window.FrameJudge.setOnSelectChannel(prefix, (idx) => {
            channelValueEl.textContent = idx == null
              ? "未选中（点画面里任一格）"
              : "[" + idx + ", " + (idx + 1) + "] 路";
          });
        };
        attachOnSelect();
      }

      // 把实时分数曲线搬进 fieldset。FrameJudge 的曲线元素原本挂在
      // document.body 末尾 + position:fixed（功能页不需要它，但向导里
      // 用来给用户即时反馈判据效果）—— 现在 wizard 形态变了，曲线要进
      // 判据框里跟 "当前在编辑 [x, y] 路" 一起显示。mountInWizard 把曲线
      // 从 body 搬到 fieldset、改成普通流式布局、随后按 _selectedChannel
      // 过滤 recent 数据。
      const attachCurve = () => {
        if (!window.FrameJudge || !window.FrameJudge.mountInWizard) {
          setTimeout(attachCurve, 30);
          return;
        }
        window.FrameJudge.mountInWizard(prefix);
      };
      attachCurve();
    }

    _renderFootNext(foot, cfg, state, true);
  }

  function _renderFinishStep(body, foot, cfg, stepDef, state) {
    body.innerHTML = "";
    const titleEl = _ce("h3", WIZARD_NS + "step-title", stepDef.title);
    body.appendChild(titleEl);

    const summary = _ce("div", WIZARD_NS + "summary");
    let hasAny = false;
    (cfg.steps || []).forEach((s) => {
      if (s.kind !== "form") return;
      (s.fields || []).forEach((f) => {
        const v = state.values[f.id];
        if (v == null || v === "" || v === false) return;
        hasAny = true;
        const row = _ce("div", WIZARD_NS + "summary-row");
        // 完成配置摘要优先用 summary_label(短标题),否则用 label/form id
        const summaryLabel = f.summary_label || f.label || f.id;
        row.appendChild(_ce("span", null, summaryLabel + ":"));
        const val = (f.type === "checkbox") ? "是" : String(v);
        row.appendChild(_ce("span", WIZARD_NS + "summary-val", val));
        summary.appendChild(row);
      });
    });
    if (!hasAny) {
      summary.appendChild(_ce("div", null, "(未填任何配置)"));
    }
    body.appendChild(summary);

    const errBox = _ce("div", WIZARD_NS + "finish-error");
    errBox.id = WIZARD_NS + "finish-error";
    body.appendChild(errBox);

    const finishBtn = _ce("button", WIZARD_NS + "primary-btn", "完成配置");
    finishBtn.id = WIZARD_NS + "finish-btn";
    // footer 复用 _renderFootNext —— 它按 currentStep 决定给不给
    // 「上一步」,并因为 finish 是最后一步自动不给「下一步」,同时
    // 始终给「跳过向导」。
    //
    // 为什么必须复用(不要再 foot.innerHTML="" 只挂 finishBtn):
    // _gotoStep 会把当前步骤号落 localStorage(`wdnwebui.wizard.<id>.step`),
    // mount 时 _lsLoadStep 恢复到该步。于是"走到过 finish 但没点完成
    // 配置就离开"(比如点右上角返回,那条路径只 captureValues + 改 hash,
    // **故意不清 step**)会让下次进卡直接停在 finish 页 —— 而旧实现
    // 的 finish 页只有一个「完成配置」按钮,没有任何回退出口,用户既回
    // 不到前面的步骤,也去不了应用页,成了死胡同(用户实测)。
    //
    // 注意按钮顺序:skip-btn 带 margin-right:auto 被顶到最左,
    // back-btn 紧贴其右,primary(完成配置)留在最右 —— 与其它步骤一致。
    _renderFootNext(foot, cfg, state, false);
    foot.appendChild(finishBtn);

    // 跑一次 finish：成功 → 向导关闭；失败 → 渲染错误 + 出口。
    // 「完成配置」与「重新开启模型」两个按钮共用它。
    const runFinish = async () => {
      finishBtn.disabled = true;
      errBox.textContent = "";
      try {
        await _finish(state);
      } catch (e) {
        _renderFinishError(errBox, e, cfg, runFinish);
        finishBtn.disabled = false;
      }
    };
    finishBtn.addEventListener("click", runFinish);
  }

  /**
   * finish 失败后的出口：模型没起来时给「重新开启模型」按钮 —— 没有它，
   * 用户唯一的出路是「上一步」连点三次回到 env 步，看上去就像「回不去」。
   */
  function _renderFinishError(errBox, e, cfg, runFinish) {
    errBox.textContent = "";
    const line = _ce("div", null,
      (e && (e.message || e)) || "完成失败");
    errBox.appendChild(line);
    if (!e || !e.retryable) return;
    if (typeof window._wizardBringUpModels !== "function") return;
    const again = _ce("button", WIZARD_NS + "primary-btn", "重新开启模型");
    again.id = WIZARD_NS + "finish-retry-btn";
    again.addEventListener("click", async () => {
      again.disabled = true;
      line.textContent = "正在重新开启模型...";
      const ok = await window._wizardBringUpModels(cfg);
      if (!ok) {
        line.textContent = "重新开启失败 —— 请到「算力配置」页手动开启后重试。";
        again.disabled = false;
        return;
      }
      await runFinish();
    });
    errBox.appendChild(again);
  }

  function _renderFootNext(foot, cfg, state, canNext) {
    foot.innerHTML = "";
    const skipBtn = _ce("button", WIZARD_NS + "skip-btn", "跳过向导");
    skipBtn.addEventListener("click", () => skip());
    foot.appendChild(skipBtn);

    // 「上一步」按钮 —— currentStep > 0 才显示（不能回退到 -1）。
    // 放在跳过向导和下一步之间：视觉顺序 = 左 (上一步) | 中 (跳过) | 右 (下一步)。
    // index 0 时直接不创建 —— DOM 留空比 disabled 更干净（不让用户点
    // 一个永远灰的按钮）。
    if (state.currentStep > 0) {
      const backBtn = _ce("button", WIZARD_NS + "back-btn", "上一步");
      backBtn.id = WIZARD_NS + "back-btn";
      backBtn.addEventListener("click", () => stepBackward());
      foot.appendChild(backBtn);
    }

    if (state.currentStep < cfg.steps.length - 1) {
      const nextBtn = _ce("button", WIZARD_NS + "primary-btn", "下一步");
      nextBtn.id = WIZARD_NS + "next-btn";
      nextBtn.disabled = !canNext;
      nextBtn.addEventListener("click", () => stepForward());
      foot.appendChild(nextBtn);
      // 重建 footer 后重新应用上传守卫
      if (state.uploadBusy) _applyNavLock(state);
    }
  }

  // ------------------------------------------------------------------ #
  // Step navigation
  // ------------------------------------------------------------------ #

  // 落 localStorage + 刷新 debug hook;抽出来是为了让「上传成功直写
  // state.values」这条不经 _captureValues 的路径保持同一落盘口径。
  function _publishValues(state, cfg) {
    _lsSave(state.app.id, cfg, state.values);
    // debug hook for test/e2e visibility(零开销,生产环境无副作用)
    try { window._wd_wizard_values = JSON.parse(JSON.stringify(state.values)); } catch (_) {}
  }

  /**
   * 字段一改动就广播 `wizard:field-change`（**通用机制，不认识任何应用**）。
   *
   * 为什么需要它：有些配置**只有在向导里才看得到效果** —— 帧判据的实时分数
   * 曲线就画在向导的视频源步里（canvas 那时才被搬进来）。把判据配置并到
   * 视频源步之后，用户改阈值就应当立刻看到曲线变化，而不是按「完成配置」
   * 之后才生效 —— 曲线摆在眼前却不动，比没有曲线更让人困惑。
   *
   * 框架只负责**发事件**：谁来接、怎么接由应用 / 共享组件自己决定。框架里
   * 出现任何具体字段名都违反零耦合。目前只有
   * `app/static/core/frame-judge.js` 接这个事件。
   */
  function _bindFieldLive(cfg, state, field, el) {
    // checkbox 藏在 toggle 容器里，监听内层 input；其余直接监听自身
    const target = (el.matches && el.matches('input[type="checkbox"]'))
      ? el
      : ((el.querySelector && el.querySelector('input[type="checkbox"]')) || el);
    const fire = () => {
      // 已经切走 / 卸载的 wizard 不再发（DOM 还挂着但 _active 换人了）
      if (!_active || _active !== state) return;
      _captureValues(cfg, state);
      try {
        window.dispatchEvent(new CustomEvent("wizard:field-change", {
          detail: { appId: state.app.id, values: Object.assign({}, state.values) },
        }));
      } catch (_) { /* 事件订阅方抛异常不影响向导 */ }
    };
    target.addEventListener("change", fire);
    target.addEventListener("blur", fire);
  }

  function _captureValues(cfg, state) {
    const body = _q("#" + WIZARD_NS + "body");
    body.querySelectorAll("[data-field-id]").forEach((el) => {
      const fid = el.dataset.fieldId;
      // prompt_rows:容器内部多个 text input,值合成数组(空值过滤)
      // 写回 state.values[fid] = [v1, v2, ...];空数组则删除 key。
      if (el.dataset.fieldType === "prompt_rows") {
        const vals = Array.from(el.querySelectorAll("input[type=text]"))
          .map((i) => (i.value || "").trim())
          .filter((v) => v.length > 0);
        if (vals.length > 0) state.values[fid] = vals;
        else delete state.values[fid];
        return;
      }
      // toggle 容器(<label> 包 checkbox input)与普通 input 兼容:
      // 先找内嵌 checkbox,有就以 checked 读;否则按 element value 读
      const innerCb = el.matches?.('input[type="checkbox"]')
        ? el : el.querySelector?.('input[type="checkbox"]');
      if (innerCb) {
        state.values[fid] = innerCb.checked;
      } else if (el.value !== "") {
        state.values[fid] = el.value;
      } else {
        delete state.values[fid];
      }
    });
    _publishValues(state, cfg);
  }

  function _gotoStep(idx) {
    if (!_active) return;
    const { cfg, container, app } = _active;
    if (idx < 0 || idx >= cfg.steps.length) return;
    _active.currentStep = idx;
    const stepDef = cfg.steps[idx];
    // 落盘:用户切走 / reboot 后下次 mount 跳回这里。写盘失败不
    // 影响本次 UI(只是个 best-effort 提示)。存**步骤 id**而不是下标
    // —— 单卡下步骤表会被裁剪,下标会错位(见 _lsLoadStep 注释)。
    //
    // **finish 步骤刻意不落盘**。落盘的意义是"用户填到第 N 步中途
    // 切走,下次别把已填的值丢了的既视感" —— 但 finish 步没有任何
    // 输入框,用户停在它上面不代表"填了一半",只代表他走到了最后
    // 一步。把它当"停留位置"存下来,后果是:用户没点完成配置就离开
    // (点右上角返回走的是 captureValues + location.hash,故意不清
    // step)→ 下次进卡 mount 恢复到 finish → 一进来就是完成配置页。
    // 对"填了一半"的表单步恢复是对的,对终点步恢复只是噪音。
    //
    // 注意与"存 id"是叠加关系而不是替代:该存的表单步仍然存 id,
    // 只是终点步改走 clear。
    if ((stepDef || {}).kind === "finish") _lsClearStep(app.id, cfg);
    else _lsSaveStep(app.id, cfg, (stepDef && stepDef.id) || idx);
    _renderStepper(_q("#" + WIZARD_NS + "stepper", container), cfg, idx);
    const body = _q("#" + WIZARD_NS + "body", container);
    const foot = _q("#" + WIZARD_NS + "foot", container);
    const def = stepDef;
    if (def.kind === "env_check") _renderEnvStep(body, foot, cfg, def, _active);
    else if (def.kind === "form") _renderFormStep(body, foot, cfg, def, _active);
    else if (def.kind === "finish") _renderFinishStep(body, foot, cfg, def, _active);
  }

  function stepForward() {
    if (!_active) return;
    const { cfg } = _active;
    // 兜底:上传在途禁止跳步(键盘 Enter / 脚本旁路)
    if (_active.uploadBusy) { _flashUploadHint(); return; }
    _captureValues(cfg, _active);   // 先 capture 当前步骤 body
    _gotoStep(_active.currentStep + 1);
  }

  /**
   * 「上一步」按钮的入口。语义与 stepForward 对称：先 capture 当前步骤
   * 已填的字段（不让用户已经输入的内容在回到前一步时被「重渲染清空」），
   * 再回退一格；index=0 时按钮 disabled（不能回退到「-1」）。
   */
  function stepBackward() {
    if (!_active) return;
    if (_active.uploadBusy) { _flashUploadHint(); return; }
    if (_active.currentStep <= 0) return;
    _captureValues(_active.cfg, _active);
    _gotoStep(_active.currentStep - 1);
  }

  function skip() {
    if (!_active) return;
    _captureValues(_active.cfg, _active); // 保留已填
    // 「跳过向导」= 明确不做了 → 步骤位置也别留着（下次进来从第 1 步）。
    _lsClearStep(_active.app.id, _active.cfg);
    unmount();
  }

  // ------------------------------------------------------------------ #
  // Finish — applyWizardSnapshot + wait for models running
  // ------------------------------------------------------------------ #
  async function _finish(state) {
    const { app, cfg } = state;
    _captureValues(cfg, state);

    // 1. build opts (camelCase, drop empty)
    const opts = {};
    cfg.steps.forEach((s) => {
      if (s.kind !== "form") return;
      (s.fields || []).forEach((f) => {
        const v = state.values[f.id];
        if (v == null || v === "") return;
        opts[_fieldKey(app.id, f)] = v;
      });
    });
    // 调试契约：scripts/test_wizard_vd.py 与 verify_wizard_upload_e2e.py
    // 直接读 window._wd_wizard_opts 断言 finish 构造出的 opts，别删。
    try {
      window._wd_wizard_opts = JSON.parse(JSON.stringify(opts));
      window._wd_wizard_steps_count = cfg.steps.length;
      window._wd_wizard_state_values = JSON.parse(JSON.stringify(state.values));
      window._wd_wizard_cfg_first_step = JSON.stringify(cfg.steps[0]).slice(0, 200);
      window._wd_wizard_cfg_source = JSON.stringify(cfg.steps.find(s => s.id === "source") || {}).slice(0, 500);
    } catch (_) {}

    // 2. wait for models running (if swcap_to_model 声明了)
    if (cfg.swcap_to_model && Object.keys(cfg.swcap_to_model).length > 0) {
      await _waitForModelsRunning(cfg);
    }

    // 3. call applyWizardSnapshot
    const ns = "ImApp_" + (app.id || "").replace(/-/g, "_");
    const api = window[ns];
    if (!api || typeof api.applyWizardSnapshot !== "function") {
      // 应用层未暴露 API:静默跳过启动动作,只完成向导
      console.warn("[wizard-state] window." + ns + ".applyWizardSnapshot not found — skip");
      unmount();
      return;
    }
    try {
      await api.applyWizardSnapshot(opts);
    } catch (e) {
      const msg = (e && (e.message || e)) || "未知错误";
      // 区分错误码
      if (msg.indexOf("open_source_failed") >= 0) {
        // 回 source 步骤
        const srcIdx = cfg.steps.findIndex((s) =>
          s.kind === "form" && (s.fields || []).some((f) => /source/.test(f.id)));
        if (srcIdx >= 0) _gotoStep(srcIdx);
        throw new Error("播放视频失败,请回到向导修改视频源");
      }
      throw e;
    }
    // applyWizardSnapshot 成功落到了下游 → 此向导视为"已完成"。
    // 写盘后下次 tryMount(自动挂载路径)读到 completed=1,不再弹
    // 向导;用户想重新配置必须显式点"向导模式"按钮(force=true)。
    // 故意写在 unmount() 之前,这样即使用户紧接着切走,wizard
    // DOM 销毁也不影响这个标记已落盘。
    _lsSaveCompleted(state.app.id, state.cfg, true);
    unmount();
  }

  /** (model_id, npu_index) 的稳定字符串键 —— 进度行 data 属性 + statusMap 用。
   *  ``npu_index`` 为 null = 任意一张卡起来即可；那种 target 不能拿去和
   *  "model@npu" 比 —— 键永远对不上，finish 会一路等到 90s 超时。 */
  function _targetKey(t) {
    return t.model_id + "@" + t.npu_index;
  }

  /** 等待模型时的「可重试」错误 —— finish 步据此给「重新开启模型」按钮。 */
  function _modelWaitError(msg) {
    const e = new Error(msg);
    e.retryable = true;
    return e;
  }

  async function _waitForModelsRunning(cfg) {
    // 展开成 (model_id, npu_index) 对：只按 model_id 判的话，多卡场景下
    // "任意一张卡起来了"就算齐，用户要两张卡却只等到一张，推理退化成单卡
    // 还不报错。
    const targets = [];
    for (const [, m] of Object.entries(cfg.swcap_to_model || {})) {
      const cards = Array.isArray(m.npu_index) ? m.npu_index : [m.npu_index];
      for (const n of cards) targets.push({ model_id: m.model_id, npu_index: n });
    }
    if (targets.length === 0) return;
    const statusBox = _q("#" + WIZARD_NS + "finish-error");
    if (!statusBox) return;
    const deadline = Date.now() + 90 * 1000; // 90s 兜底超时
    // 缺席宽限：模型不在 registry 里持续这么久 → 判定为没启动。POST 后
    // ~1s 实例才出现，15s 足够宽松。
    const MISSING_GRACE_MS = 15 * 1000;
    const missingSince = {};
    const PROG_CLS = WIZARD_NS + "finish-progress";
    const FAIL_CLS = WIZARD_NS + "finish-error-fail";
    const hasHeader = targets.length > 1;

    // DOM 只构建一次:tick 内只更新 fill.style.width 让 CSS transition
    // (0.5s) 把"数字跳变"变成视觉上"跑"的进度,避免每秒重建节点导致
    // bar 从 0% 重铺的抖动。
    let initialized = false;
    const initDOM = () => {
      statusBox.classList.remove(FAIL_CLS);
      statusBox.classList.add(PROG_CLS);
      statusBox.innerHTML = "";
      if (hasHeader) {
        const h = document.createElement("div");
        h.className = WIZARD_NS + "finish-progress-h";
        h.dataset.role = "header";
        statusBox.appendChild(h);
      }
      for (const m of targets) {
        const row = document.createElement("div");
        row.className = WIZARD_NS + "progress-row";
        row.dataset.modelId = _targetKey(m);
        const head = document.createElement("div");
        head.className = WIZARD_NS + "progress-row-head";
        const label = document.createElement("span");
        label.className = WIZARD_NS + "progress-row-label";
        label.textContent = _targetKey(m);   // 带卡号：看得出是哪张卡在加载
        const pctEl = document.createElement("span");
        pctEl.className = WIZARD_NS + "progress-row-pct";
        head.appendChild(label);
        head.appendChild(pctEl);
        const bar = document.createElement("div");
        bar.className = WIZARD_NS + "progress-bar";
        const fill = document.createElement("div");
        fill.className = WIZARD_NS + "progress-bar-fill";
        fill.style.width = "0%";
        bar.appendChild(fill);
        row.appendChild(head);
        row.appendChild(bar);
        statusBox.appendChild(row);
      }
      initialized = true;
    };

    const updateProgress = (statusMap, doneCount) => {
      if (hasHeader) {
        const h = statusBox.querySelector("[data-role=\"header\"]");
        if (h) h.textContent = `模型加载进度 (${doneCount}/${targets.length})`;
      }
      for (const m of targets) {
        const info = statusMap[_targetKey(m)];
        if (!info) continue;
        // CSS.escape 防御连字符 / 点号(model_id 限定 a-z0-9- 但保留)
        const row = statusBox.querySelector(
          `[data-model-id="${CSS.escape(_targetKey(m))}"]`
        );
        if (!row) continue;
        const fill = row.querySelector("." + WIZARD_NS + "progress-bar-fill");
        const pctEl = row.querySelector("." + WIZARD_NS + "progress-row-pct");
        fill.style.width = info.barPct + "%";
        fill.classList.toggle(WIZARD_NS + "progress-bar-done",  info.status === "done");
        fill.classList.toggle(WIZARD_NS + "progress-bar-error", info.status === "error");
        pctEl.textContent = info.label;
      }
    };

    const clearBox = () => {
      statusBox.classList.remove(PROG_CLS, FAIL_CLS);
      statusBox.textContent = "";
    };

    while (Date.now() - deadline < 0) {
      // 向导已被卸载（上一步 / 跳过向导 / 切页）→ 中止。注意是**抛错**
      // 不是 return：静默 return 等于「等待成功」，_finish 会接着调
      // applyWizardSnapshot，把没就绪的应用拉起来。
      if (!statusBox.isConnected) {
        throw new Error("向导已离开，等待中止。");
      }
      let abort = null;
      try {
        const r = await fetch("/api/compute", { cache: "no-store" });
        if (r.ok) {
          const data = await r.json();
          // 预估耗时复用 /api/compute 快照(与算力配置页 "初始化中…约 Xs" 同源)
          const perModel = (data.estimated_init_seconds_per_model) || {};
          const defaultEst = data.default_estimated_init_seconds || 30;
          // 只算本机算力(spawned / adopted_local),peer adopted 不算 —— 与
          // _probeEnv 一致;_wizardBringUpModels 已保证 POST 成功 = 本机就绪。
          const instances = (data.instances || [])
            .filter((i) => i._source !== "adopted");
          // 就绪判据分两种：声明了卡号的要**那张卡**running；没声明卡号的
          // （多数卡片的旧写法）任意一张卡 running 即可。
          const running = instances.filter((i) => i.status === "running");
          const runningExact = new Set(running.map((i) => _targetKey(
            { model_id: i.model_id, npu_index: i.npu_index })));
          const runningAny = new Set(running.map((i) => i.model_id));
          if (targets.every((t) => (t.npu_index == null
                ? runningAny.has(t.model_id)
                : runningExact.has(_targetKey(t))))) {
            // 模型全部就绪 → 清空 statusBox,避免误导用户"是不是出错了"
            clearBox();
            return;
          }
          if (!initialized) initDOM();
          // 进度 = (now - inst.started_at) / estimated;99% 封顶避免先撞
          // 100% 后 status=running 才到位的小幅抖动。
          const now = Date.now() / 1000;
          const statusMap = {};
          let doneCount = 0;
          for (const m of targets) {
            const key = _targetKey(m);
            const est = perModel[m.model_id] != null
              ? perModel[m.model_id] : defaultEst;
            const inst = instances.find((i) => i.model_id === m.model_id
              && (m.npu_index == null || i.npu_index === m.npu_index));
            if (!inst) {
              // 一直不出现 = 没启动（被关了 / 压根没开），不是「还在启动」。
              // 超过宽限就中止 —— 盯一条不变的进度条等满 90s 比报错更像死机。
              if (missingSince[key] == null) missingSince[key] = Date.now();
              if (Date.now() - missingSince[key] > MISSING_GRACE_MS) {
                abort = `本机未运行「${key}」—— 模型可能已被关闭或从未开启。`;
                break;
              }
              // POST 后 1s 内 worker 还没上报 status;显式 "等待启动…" 而
              // 非 0%,避免和真 0% 加载混淆
              statusMap[key] = { status: "wait", barPct: 0, label: "等待启动…" };
              continue;
            }
            delete missingSince[key];
            if (inst.status === "running") {
              statusMap[key] = { status: "done", barPct: 100, label: "✓ 100%" };
              doneCount++;
              continue;
            }
            if (inst.status === "error") {
              // **终态**：worker 挂了 / 被别处关掉了。立刻中止 —— 继续等满
              // 90s 只会让用户盯一条不再变化的进度条（真机实测：另一个页面
              // 「全部停止」后，这里卡满 90s 才报错）。
              abort = `「${key}」启动失败：${inst.error || "未知错误"}`;
              break;
            }
            const startTs = inst.started_at || now;
            const elapsed = Math.max(0, now - startTs);
            const pct = Math.min(99, Math.max(0, Math.round((elapsed / est) * 100)));
            const remaining = Math.max(0, Math.round(est - elapsed));
            statusMap[key] = {
              status: "loading", barPct: pct,
              label: `${pct}% (~${remaining}s 剩余)`,
            };
          }
          if (!abort) updateProgress(statusMap, doneCount);
        }
      } catch (_) { /* retry */ }
      if (abort) {
        clearBox();
        throw _modelWaitError(abort);
      }
      await new Promise((r) => setTimeout(r, 1000));
    }
    clearBox();
    throw _modelWaitError("等待模型启动超时（90s）—— 模型可能已被关闭或加载失败。");
  }

  // ------------------------------------------------------------------ #
  // Mount / Unmount
  // ------------------------------------------------------------------ #
  async function mount(app, cfg) {
    if (_active) unmount();
    // 隐藏原应用 page
    const pageSection = document.getElementById("page-" + app.id);
    if (pageSection) pageSection.classList.add("hidden");

    // 注入 wizard 容器
    let container = document.getElementById(WIZARD_NS + "root-" + app.id);
    if (!container) {
      container = _ce("section");
      container.id = WIZARD_NS + "root-" + app.id;
      container.classList.add(WIZARD_NS + "section");
      // 放在原 page 之后(兄弟节点)
      pageSection && pageSection.parentNode
        ? pageSection.parentNode.insertBefore(container, pageSection.nextSibling)
        : document.body.appendChild(container);
    }
    container.classList.remove("hidden");

    const values = _lsLoad(app.id, cfg);
    const envCheck = await _probeEnv(cfg, app);
    // 单卡策略：双卡 / 探测失败 → single 为 null，后面保持原路径。
    const single = _planSingleCard(cfg, app, envCheck);
    // 恢复上次停留的步骤:用户填到第 3 步中途切走 / reboot 后再回同一张卡,
    // 应跳回第 3 步而不是强制第 1 步。按**步骤 id**解析(见 _resolveStepIdx)。
    const restored = _lsLoadStep(app.id, cfg);
    const effSteps = (single && single.effCfg && single.effCfg.steps)
      || (cfg.steps || []);
    const startStep = _resolveStepIdx(effSteps, restored);

    _active = {
      app, cfg, container,
      currentStep: startStep,
      values,
      envCheck,
      single,
      intervals: [],
    };
    // 单卡：cfg 换成裁剪后的副本（卡号指向唯一卡位 / 只留选中的
    // capability）。localStorage_key 原样保留 → 步骤 / 完成标记的 key 不变。
    if (single && single.effCfg) _active.cfg = single.effCfg;
    _renderShell(container, app, cfg);
    _gotoStep(startStep);

    // 监听 hashchange / 自定义 pagechange:用户在 wizard 模式下点 sidebar
    // 切到其他应用 / 系统页面时,自动 unmount 避免 wizard DOM 残留
    // silent=true 表示用户已经切走 → unmount 时不显示原 page(用户已经
    // 不在那个 page 了),仅清理 wizard DOM 与事件 listener
    const onNavAway = () => {
      if (!_active) return;
      const cur = (location.hash || "").replace(/^#\/?/, "").split("/")[0];
      if (cur !== app.id) unmount(true);
    };
    window.addEventListener("hashchange", onNavAway);
    _active._onNavAway = onNavAway;

    // B 方案:通知 framework 隐藏 wd-loading-section(同步在 showPage
    // 期间挂上的 loading 占位)。失败路径(tryMount catch / fetch
    // wizard.json 404 / mount 抛错)不会到这里,framework 那边有 30s
    // 兜底 timeout 兜住。
    try {
      window.dispatchEvent(new CustomEvent("wizard:mounted", {
        detail: { appId: app.id, action: "mount" },
      }));
    } catch (_) {}
  }

  /**
   * 把画面搬回应用页的**原位**，而不是"搬回原来的父节点"。
   *
   * 只记 parent 的话还原只能 appendChild —— 那会插到父节点末尾。多路卡的
   * 画面容器自己就是 .vd-stage，.vd-stage-col 里它后面还排着「视频源 /
   * 判据 / 控制」几段，于是回到应用页就是「播放器掉到源配置框下面」。
   *
   * @param {{canvas:HTMLElement, originalParent:HTMLElement,
   *          nextSibling:Node|null}|null} mc moveCanvasToWizard 的记录
   */
  function _restoreCanvas(mc) {
    if (!mc || !mc.canvas || !mc.originalParent) return;
    const { canvas, originalParent, nextSibling } = mc;
    // 停留向导期间用户切过页，原 parent 可能已随页面卸载 —— 无处可放，
    // 后端会自然停，不必抢救。
    if (!originalParent.isConnected) return;
    try {
      // nextSibling 指向的节点可能已被重渲染带走（此时已不是 parent 的孩子），
      // insertBefore 会抛 NotFoundError，退回末尾兜底。
      if (nextSibling && nextSibling.parentNode === originalParent) {
        originalParent.insertBefore(canvas, nextSibling);
      } else {
        originalParent.appendChild(canvas);
      }
    } catch (e) {
      // parent 已不在 DOM(用户切了 page)— 忽略,后端会自然停
    }
  }

  function unmount(silent) {
    if (!_active) return;
    const { app, container, intervals, _onNavAway, movedCanvas, cfg } = _active;
    // 切到别的页面（silent=true）→ 丢掉「上次停在哪一步」：半截的向导 +
    // 外部算力变化（模型被别的页面关掉）会死锁。只清 step 不清 values，
    // 已填的字段回来仍预填。
    if (silent) _lsClearStep(app.id, cfg);
    intervals.forEach(clearInterval);
    if (_onNavAway) window.removeEventListener("hashchange", _onNavAway);
    // 还原被 move 进 canvas-wrap 的画面元素。不还原的话 wizard 容器销毁会
    // 一并带走它，应用页画面变黑（后端解码 + ws 推帧都还在继续）。
    _restoreCanvas(movedCanvas);
    // 彻底移除 wizard DOM(下次 mount 重建,避免残留 class)
    if (container && container.parentNode) {
      container.parentNode.removeChild(container);
    }
    // 显示原 page — 但仅限用户主动跳过/完成 wizard 时(silent=false);
    // silent=true(hashchange 自动卸载)时不动原 page hidden,因为
    // ROUTER 已经把原 page 隐藏了,用户切到了别的页面
    if (!silent) {
      const pageSection = document.getElementById("page-" + app.id);
      if (pageSection) pageSection.classList.remove("hidden");
    }
    _active = null;
    // 让 ROUTER 下次再点同一应用时重新触发 _loadCardJS / tryMount。
    // ROUTER._loadCardJS 内部检查 DemoGuard._loadedApps.has(page),命中就
    // 跳过 → wizard 永不重挂。unmount 时把 app 从已加载集合中清出即可。
    try {
      // DemoGuard 是 framework.js 内的闭包,通过 window 上的 DemoGuard
      // 公开引用拿不到(framework 用 IIFE 不挂全局);改成 dispatch 一个
      // 自定义事件让 framework.js 监听,清掉 _loadedApps 条目。
      window.dispatchEvent(new CustomEvent("wizard:unmounted", {
        detail: { appId: app.id, action: "unmount" },
      }));
    } catch (_) {}
  }

  // isCompleted / clearCompleted / clearStep 由 registry.tryMount 在
  // auto-mount 路径上读、"向导模式"按钮路径上写。三个 API 都以
  // (app, cfg) 形式传入,因为 localStorage key 含 cfg.suffix。
  //
  // clearAll(appId) 是 framework 在 back-home "退出应用" 兜底里
  // 调,把整张卡的 wizard 状态全清(无 cfg,按默认 base 匹配)——
  // 配合 ActivateManager._exit() 让"退出应用" = 完全回到首装状态。
  window.WizardState = {
    mount, unmount, stepForward, stepBackward, skip,
    isCompleted: (app, cfg) => _lsLoadCompleted(app.id, cfg),
    clearCompleted: (app, cfg) => _lsClearCompleted(app.id, cfg),
    clearStep: (app, cfg) => _lsClearStep(app.id, cfg),
    clearAll: (appId) => _lsClearAllFor(appId),
  };

  // ------------------------------------------------------------------ #
  // window.enterWizardMode(appId) — 应用页面左上角"向导模式"按钮调用入口
  // 从 window.__APP_MANIFESTS__ 找 manifest,先 unmount 现有 wizard(以防
  // 重复挂),再调 registry.tryMount 挂新 wizard。若应用未在主页面 manifest
  // 中(极端情况),仅 console.warn 不抛错。
  //
  // 注意:传 {force: true}。用户主动点这个按钮 = 显式想重新配置,
  // 即便之前已完成也要重挂,且要从第 1 步重新走一遍。force=true 由
  // registry.tryMount 兜底:清掉 completed + step。
  // ------------------------------------------------------------------ #
  window.enterWizardMode = function (appId) {
    const cards = (window.__APP_MANIFESTS__ && window.__APP_MANIFESTS__.cards) || [];
    const manifest = cards.find((c) => c.id === appId);
    if (!manifest) {
      console.warn("[wizard-state] enterWizardMode: manifest not found for", appId);
      return;
    }
    try { unmount(); } catch (_) { /* 可能没有挂载,忽略 */ }
    if (window.WizardRegistry && window.WizardRegistry.tryMount) {
      window.WizardRegistry.tryMount(manifest, { force: true });
    } else {
      console.warn("[wizard-state] enterWizardMode: WizardRegistry not loaded");
    }
  };

  // ------------------------------------------------------------------ #
  // _wizardBringUpModels — 闭包 helper,供 env_check.js / registry.js 引用
  // 真正启停实现放 env_check.js (避免在 state.js 里堆 fetch 业务代码)
  // ------------------------------------------------------------------ #
  window._wizardBringUpModels = async function (cfg) {
    if (!cfg.swcap_to_model) return true;
    // 单卡 + 刚需多模型（如监控分析检索）→ 本应用在当前设备上不可用。
    // 明确**一个模型都不启停**：既不加载,也不去停别人正在跑的模型
    // ——「停别人的模型」是破坏性的。
    if (_active && _active.single && _active.single.blocked) {
      console.warn("[wizard-state] 单卡模式：本应用被标记为不可用，"
                   + "不启停任何模型");
      return false;
    }
    try {
      const r = await fetch("/api/compute", { cache: "no-store" });
      if (!r.ok) return false;
      const data = await r.json();
      // 1. 停非必需(后端契约 DELETE + JSON {model};按 capability 是否被 swcap_to_model 命中判断)
      for (const inst of (data.instances || [])) {
        // 安全护栏:_source === "adopted" 是从远端 peer 通过 DDS adopt
        // 来的算力实例(其他板子 spawn 的 worker),本机无权限启停 —
        // 跳过整个循环,不动 DELETE 也不动任何字段。Peer 实例的 NPU
        // 显存 / worker 进程状态完全由对端机器控制,跨板操作是严重
        // 错误(wizard 一键开启曾因缺失此护栏误关 peer 模型)。
        // spawned / adopted_local(本机)继续走原逻辑。
        if (inst._source === "adopted") continue;
        const caps = inst.capability_types || [];
        const isNeeded = caps.some((c) => cfg.swcap_to_model[c]);
        // running / loading 状态的实例才需要停(其他状态后端会拒)
        if (!isNeeded && (inst.status === "running" || inst.status === "loading")) {
          const modelId = inst.model_id;
          if (!modelId) continue;
          const dr = await fetch("/api/compute/instances", {
            method: "DELETE",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ model: modelId }),
          });
          if (!dr.ok) {
            console.warn("[wizard-state] stop", modelId, "→ HTTP", dr.status,
                         await dr.text().catch(() => ""));
            return false;
          }
        }
      }
      // 2. 启必需(后端契约 POST + JSON {model, npu_index})
      for (const [cap, m] of Object.entries(cfg.swcap_to_model)) {
        const sr = await fetch("/api/compute/instances", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ model: m.model_id, npu_index: m.npu_index }),
        });
        if (!sr.ok) {
          console.warn("[wizard-state] start", m.model_id, "→ HTTP", sr.status,
                       await sr.text().catch(() => ""));
          return false;
        }
      }
      return true;
    } catch (e) {
      console.warn("[wizard-state] bringUpModels failed:", e);
      return false;
    }
  };
})();