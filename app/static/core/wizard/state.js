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
  function _lsKey(appId, cfg) {
    return cfg.localStorage_key || ("wdnwebui.wizard." + appId + ".values");
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
    head.appendChild(_ce("h2", WIZARD_NS + "title", title));
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

  function _renderEnvStep(body, foot, cfg, stepDef, state) {
    body.innerHTML = "";
    const hwOK = (state.envCheck.hwMissing || []).length === 0;
    const swOK = (state.envCheck.swMissing || []).length === 0;

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
    state.envCheck.swRequired.forEach((cap) => {
      const ok = !state.envCheck.swMissing.includes(cap);
      const row = _ce("div", WIZARD_NS + "env-row");
      row.classList.add(ok ? WIZARD_NS + "env-ok" : WIZARD_NS + "env-fail");
      row.textContent = (ok ? "✓ " : "✗ ") + cap;
      card2.appendChild(row);
    });
    if (!state.envCheck.swRequired.length) {
      card2.appendChild(_envNoReqRow("本应用不依赖模型 / 软件能力"));
    }
    if (!swOK) {
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
    _renderFootNext(foot, cfg, state, hwOK && swOK);
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
    try {
      const r = await fetch("/api/compute", { cache: "no-store" });
      if (r.ok) {
        const data = await r.json();
        for (const inst of (data.instances || [])) {
          if (inst.status === "running" && inst._source !== "adopted") {
            for (const c of (inst.capability_types || [])) runningCaps.add(c);
          }
        }
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
    };
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
    foot.innerHTML = "";
    foot.appendChild(finishBtn);

    finishBtn.addEventListener("click", async () => {
      finishBtn.disabled = true;
      errBox.textContent = "";
      try {
        await _finish(state);
      } catch (e) {
        errBox.textContent = (e && (e.message || e)) || "完成失败";
        finishBtn.disabled = false;
      }
    });
  }

  function _renderFootNext(foot, cfg, state, canNext) {
    foot.innerHTML = "";
    const skipBtn = _ce("button", WIZARD_NS + "skip-btn", "跳过向导");
    skipBtn.addEventListener("click", () => skip());
    foot.appendChild(skipBtn);

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
    _renderStepper(_q("#" + WIZARD_NS + "stepper", container), cfg, idx);
    const body = _q("#" + WIZARD_NS + "body", container);
    const foot = _q("#" + WIZARD_NS + "foot", container);
    const def = cfg.steps[idx];
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

  function skip() {
    if (!_active) return;
    _captureValues(_active.cfg, _active); // 保留已填
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
    unmount();
  }

  async function _waitForModelsRunning(cfg) {
    const targets = Object.entries(cfg.swcap_to_model).map(([cap, m]) => m.model_id);
    if (targets.length === 0) return;
    const statusBox = _q("#" + WIZARD_NS + "finish-error");
    if (!statusBox) return;
    const deadline = Date.now() + 90 * 1000; // 90s 兜底超时
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
        row.dataset.modelId = m;
        const head = document.createElement("div");
        head.className = WIZARD_NS + "progress-row-head";
        const label = document.createElement("span");
        label.className = WIZARD_NS + "progress-row-label";
        label.textContent = m;
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
        const info = statusMap[m];
        if (!info) continue;
        // CSS.escape 防御连字符 / 点号(model_id 限定 a-z0-9- 但保留)
        const row = statusBox.querySelector(
          `[data-model-id="${CSS.escape(m)}"]`
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

    const setError = (msg) => {
      statusBox.classList.remove(PROG_CLS);
      statusBox.classList.add(FAIL_CLS);
      statusBox.textContent = msg;
    };
    const clearBox = () => {
      statusBox.classList.remove(PROG_CLS, FAIL_CLS);
      statusBox.textContent = "";
    };

    while (Date.now() - deadline < 0) {
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
          const runningSet = new Set(
            instances.filter((i) => i.status === "running").map((i) => i.model_id)
          );
          if (targets.every((m) => runningSet.has(m))) {
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
            const est = perModel[m] != null ? perModel[m] : defaultEst;
            const inst = instances.find((i) => i.model_id === m);
            if (!inst) {
              // POST 后 1s 内 worker 还没上报 status;显式 "等待启动…" 而
              // 非 0%,避免和真 0% 加载混淆
              statusMap[m] = { status: "wait", barPct: 0, label: "等待启动…" };
              continue;
            }
            if (inst.status === "running") {
              statusMap[m] = { status: "done", barPct: 100, label: "✓ 100%" };
              doneCount++;
              continue;
            }
            if (inst.status === "error") {
              statusMap[m] = {
                status: "error", barPct: 0,
                label: `✗ ${inst.error || "未知错误"}`,
              };
              continue;
            }
            const startTs = inst.started_at || now;
            const elapsed = Math.max(0, now - startTs);
            const pct = Math.min(99, Math.max(0, Math.round((elapsed / est) * 100)));
            const remaining = Math.max(0, Math.round(est - elapsed));
            statusMap[m] = {
              status: "loading", barPct: pct,
              label: `${pct}% (~${remaining}s 剩余)`,
            };
          }
          updateProgress(statusMap, doneCount);
        }
      } catch (_) { /* retry */ }
      await new Promise((r) => setTimeout(r, 1000));
    }
    setError("模型启动超时,可继续等待或退出");
    throw new Error("等待模型启动超时");
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

    _active = {
      app, cfg, container,
      currentStep: 0,
      values,
      envCheck,
      intervals: [],
    };
    _renderShell(container, app, cfg);
    _gotoStep(0);

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
    const { app, container, intervals, _onNavAway, movedCanvas } = _active;
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

  window.WizardState = { mount, unmount, stepForward, skip };

  // ------------------------------------------------------------------ #
  // window.enterWizardMode(appId) — 应用页面左上角"向导模式"按钮调用入口
  // 从 window.__APP_MANIFESTS__ 找 manifest,先 unmount 现有 wizard(以防
  // 重复挂),再调 registry.tryMount 挂新 wizard。若应用未在主页面 manifest
  // 中(极端情况),仅 console.warn 不抛错。
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
      window.WizardRegistry.tryMount(manifest);
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