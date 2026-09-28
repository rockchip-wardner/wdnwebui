/* model-pick —— 共享的"模型实例选择下拉框"填充工具。
 *
 * v0.4 之前定义在 video_detection 的 app.js 内(IIFE 作用域,只有 VD
 * 自己能用)。VTD / audio_analysis 也调它但 ReferenceError —— 移到
 * core/fs-modal.js 同级的共享资源。
 *
 * 用法:
 *   <script src="/static/core/model-pick.js" defer></script>
 *   loadModelPick("vtd-model-pick", "swcap_video_describe", "vtd.model_id");
 *
 *   行为:
 *   * 拉 /api/compute/instances-by-capability,渲染 dropdown options
 *   * placeholder "自动 (本机优先)" 永远可选
 *   * localStorage 缓存上次选择,模型停掉后回退到 "自动"
 *   * 没有可用实例时下拉被禁用
 */
(function () {
  "use strict";

  const API_PREFIX = "/api";

  async function loadModelPick(selId, capability, storageKey) {
    const sel = document.getElementById(selId);
    if (!sel) return;
    let data = { instances: [] };
    try {
      const url = `${API_PREFIX}/compute/instances-by-capability`
                + `?capability=${encodeURIComponent(capability)}`;
      const r = await fetch(url, { cache: "no-store" });
      if (r.ok) data = await r.json();
    } catch (_) { /* best-effort */ }
    const insts = Array.isArray(data.instances) ? data.instances : [];

    // 重置 dropdown,保留 placeholder 元素("自动" 那条 option 由 HTML
    // 静态给出,这里只 append 真正可用的实例)。
    sel.innerHTML = "";
    const autoOpt = document.createElement("option");
    autoOpt.value = "";
    autoOpt.textContent = "自动 (本机优先)";
    sel.appendChild(autoOpt);

    let savedVal = "";
    try {
      savedVal = window.localStorage?.getItem(storageKey) || "";
    } catch (_) { /* localStorage may be blocked */ }

    for (const inst of insts) {
      const opt = document.createElement("option");
      opt.value = inst.model_id || "";
      const srcText = (inst.source === "spawned"
                    || inst.source === "adopted_local") ? "本机" : "远端";
      const npuText = (typeof inst.npu_index === "number"
                    && inst.npu_index >= 0) ? `NPU${inst.npu_index}` : "NPU?";
      opt.textContent = `${inst.label || inst.model_id} · ${srcText} · ${npuText}`;
      sel.appendChild(opt);
    }

    // 选择回填:优先 localStorage;如果保存的 model_id 现在不在列表里
    // (用户刚刚停了对应实例),回退到 "自动"。
    let chosen = "";
    if (savedVal && Array.from(sel.options).some((o) => o.value === savedVal)) {
      chosen = savedVal;
    }
    sel.value = chosen || "";

    sel.disabled = insts.length === 0;
    sel.onchange = () => {
      try { window.localStorage?.setItem(storageKey, sel.value || ""); }
      catch (_) { /* best-effort */ }
    };
    if (insts.length === 0) {
      autoOpt.textContent = "自动 (本机优先 · 无可用实例)";
    }
  }

  window.loadModelPick = loadModelPick;
})();