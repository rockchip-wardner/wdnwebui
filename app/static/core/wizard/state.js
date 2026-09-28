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
    const exitBtn = _ce("button", WIZARD_NS + "exit-btn", "向导模式");
    exitBtn.title = "返回向导重入向导模式";
    exitBtn.addEventListener("click", () => unmount()); // 重入入口:先 unmount,外部 ROUTER 再触发 tryMount
    head.appendChild(exitBtn);
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
    // SW: 拉 /api/compute 看 running instances 是否覆盖 manifest.software_capabilities
    let swMissing = [];
    let runningCaps = new Set();
    try {
      const r = await fetch("/api/compute", { cache: "no-store" });
      if (r.ok) {
        const data = await r.json();
        for (const inst of (data.instances || [])) {
          if (inst.status === "running") {
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

  function _renderFormStep(body, foot, cfg, stepDef, state) {
    body.innerHTML = "";
    const titleEl = _ce("h3", WIZARD_NS + "step-title", stepDef.title);
    body.appendChild(titleEl);
    const form = _ce("div", WIZARD_NS + "form");
    body.appendChild(form);

    (stepDef.fields || []).forEach((field) => {
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
          if (cur != null) el.value = cur;
          break;
        case "checkbox":
          el = _ce("input", WIZARD_NS + "checkbox");
          el.type = "checkbox";
          el.checked = !!cur;
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
        default: // text
          el = _ce("input", WIZARD_NS + "input");
          el.type = "text";
          if (field.placeholder) el.placeholder = field.placeholder;
          if (cur != null) el.value = cur;
      }
      el.dataset.fieldId = field.id;
      wrap.appendChild(el);
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
        row.appendChild(_ce("span", null, (f.label || f.id) + ":"));
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
      nextBtn.disabled = !canNext;
      nextBtn.addEventListener("click", () => stepForward());
      foot.appendChild(nextBtn);
    }
  }

  // ------------------------------------------------------------------ #
  // Step navigation
  // ------------------------------------------------------------------ #
  function _captureValues(cfg, state) {
    const body = _q("#" + WIZARD_NS + "body");
    body.querySelectorAll("[data-field-id]").forEach((el) => {
      const fid = el.dataset.fieldId;
      if (el.type === "checkbox") state.values[fid] = el.checked;
      else if (el.value !== "") state.values[fid] = el.value;
      else delete state.values[fid];
    });
    _lsSave(state.app.id, cfg, state.values);
    // debug hook for test/e2e visibility(零开销,生产环境无副作用)
    try { window._wd_wizard_values = JSON.parse(JSON.stringify(state.values)); } catch (_) {}
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
    _captureValues(cfg, _active);
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
    window._dbg_finish_called = (window._dbg_finish_called || 0) + 1;
    window._dbg_finish_state = state ? "alive" : "null";
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
    // debug hook
    try {
      window._wd_wizard_opts = JSON.parse(JSON.stringify(opts));
      window._wd_wizard_steps_count = cfg.steps.length;
      window._wd_wizard_state_values = JSON.parse(JSON.stringify(state.values));
      window._wd_wizard_cfg_first_step = JSON.stringify(cfg.steps[0]).slice(0, 200);
      window._wd_wizard_cfg_source = JSON.stringify(cfg.steps.find(s => s.id === "source") || {}).slice(0, 500);
      window._dbg_finish_opts_keys = Object.keys(opts);
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
    const deadline = Date.now() + 90 * 1000; // 90s 兜底超时
    let spinnerOn = true;
    while (Date.now() - deadline < 0) {
      try {
        const r = await fetch("/api/compute", { cache: "no-store" });
        if (r.ok) {
          const data = await r.json();
          const running = new Set(
            (data.instances || [])
              .filter((i) => i.status === "running")
              .map((i) => i.model_id)
          );
          if (targets.every((m) => running.has(m))) return;
          if (statusBox) statusBox.textContent = "等待模型启动中... (" + targets.length + " 个)";
        }
      } catch (_) { /* retry */ }
      await new Promise((r) => setTimeout(r, 1000));
    }
    if (statusBox) statusBox.textContent = "模型启动超时,可继续等待或退出";
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
  }

  function unmount() {
    if (!_active) return;
    const { app, container, intervals } = _active;
    intervals.forEach(clearInterval);
    // 彻底移除 wizard DOM(下次 mount 重建,避免残留 class)
    if (container && container.parentNode) {
      container.parentNode.removeChild(container);
    }
    // 显示原 page
    const pageSection = document.getElementById("page-" + app.id);
    if (pageSection) pageSection.classList.remove("hidden");
    _active = null;
    // 通知 registry 卸载(允许外部重新 tryMount)
    try { window.dispatchEvent(new CustomEvent("wizard:unmounted", { detail: { appId: app.id } })); } catch (_) {}
  }

  window.WizardState = { mount, unmount, stepForward, skip };

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