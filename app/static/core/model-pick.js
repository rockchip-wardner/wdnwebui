/* model-pick —— 共享的"模型实例选择下拉框"填充工具。
 *
 * v0.4 之前定义在 video_detection 的 app.js 内(IIFE 作用域,只有 VD
 * 自己能用)。VTD / audio_analysis 也调它但 ReferenceError —— 移到
 * core/fs-modal.js 同级的共享资源。
 *
 * 用法:
 *   <script src="/static/core/model-pick.js" defer></script>
 *   loadModelPick("vtd-model-pick", "swcap_vision_chat", "vtd.model_id");
 *   loadModelPick("usb-model-pick", ["swcap_vision_chat","swcap_tool_call"],
 *                 "usb.model_id");          // ← 组模式：AND 语义
 *
 *   行为:
 *   * 拉 /api/compute/instances-by-capability,渲染 dropdown options
 *   * placeholder "自动 (本机优先)" 永远可选
 *   * localStorage 缓存上次选择,模型停掉后回退到 "自动"
 *   * 没有可用实例时下拉被禁用
 *
 *   **capability 可以是字符串或字符串数组。** 数组 = 能力组：只列**同时**
 *   具备全部能力的实例。前端自己按单个能力分别查再求交集做不到这一点 ——
 *   「一个实例都满足」和「两个实例各满足一半」在前端看都是交集非空,
 *   后者却会在推理时才炸。后端对重复的 capability= 参数按 AND 处理。
 */
(function () {
  "use strict";

  const API_PREFIX = "/api";

  function _capQuery(capability) {
    const caps = Array.isArray(capability) ? capability : [capability];
    return caps
      .filter((c) => typeof c === "string" && c)
      .map((c) => "capability=" + encodeURIComponent(c))
      .join("&");
  }

  async function loadModelPick(selId, capability, storageKey) {
    const sel = document.getElementById(selId);
    if (!sel) return;
    let data = { instances: [] };
    try {
      const url = `${API_PREFIX}/compute/instances-by-capability?`
                + _capQuery(capability);
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

  /* ------------------------------------------------------------------
   * loadInstancePickMulti —— 「勾选若干个**实例**」的模型选择器。
   *
   * 不能用上面那个 <select>：option.value 只装 model_id，于是同一个模型在
   * NPU0 / NPU1 上的两项 value 完全相同，选中后卡号根本没传出去（后端
   * 默认只取一张）。所以勾选粒度是**实例**，复合 value 形如
   * "qwen3-vl-4b|0|spawned"。
   */
  const LOCAL_SRC = new Set(["spawned", "adopted_local"]);
  // 远端算力（peer board 的 worker）当前版本不支持直接送检：勾了也没有对应的
  // 本地推理 slot 可用。禁用而不是隐藏 —— 用户需要知道"它存在但选不了"。
  const REMOTE_TIP = "当前版本暂不支持";

  function _instKey(inst) {
    return [inst.model_id, inst.npu_index,
            (inst.source === "adopted_local" ? "spawned" : inst.source)]
      .join("|");
  }

  function _parseKey(k) {
    const [model_id, npu, src] = String(k).split("|");
    return {
      model_id,
      npu_index: Number.isFinite(parseInt(npu, 10)) ? parseInt(npu, 10) : -1,
      source: src || "",
    };
  }

  function _saved(storageKey) {
    try {
      const raw = window.localStorage?.getItem(storageKey);
      const v = raw ? JSON.parse(raw) : null;
      return Array.isArray(v) ? v.filter((x) => typeof x === "string") : null;
    } catch (_) {
      return null;
    }
  }

  async function loadInstancePickMulti(containerId, capability, storageKey) {
    const box = document.getElementById(containerId);
    if (!box) return [];
    let data = { instances: [] };
    try {
      const url = `${API_PREFIX}/compute/instances-by-capability?`
                + _capQuery(capability);
      const r = await fetch(url, { cache: "no-store" });
      if (r.ok) data = await r.json();
    } catch (_) { /* best-effort */ }

    const insts = Array.isArray(data.instances) ? data.instances : [];
    // 同卡同模型重复登记过（老实例没清干净）时后一条覆盖前一条，免得渲染出
    // 两个一模一样的勾选框、勾一个等于勾两个。
    const uniq = new Map();
    for (const i of insts) uniq.set(_instKey(i), i);
    const rows = Array.from(uniq.values());

    box.innerHTML = "";
    // 上次勾选过的实例只认**本机**的（远端已禁用），且必须仍在列表里 ——
    // 否则模型被停过 / 卡号变了会让勾选框一个都不勾，页面直接卡在
    // 「请先勾选至少一个模型实例」。
    const localKeys = new Set(
      rows.filter((i) => LOCAL_SRC.has(i.source)).map((i) => _instKey(i)));
    let saved = _saved(storageKey);
    if (saved) {
      saved = saved.filter((k) => localKeys.has(k));
      if (saved.length === 0) saved = null;
    }

    for (const inst of rows) {
      const key = _instKey(inst);
      const local = LOCAL_SRC.has(inst.source);
      const npuText = (typeof inst.npu_index === "number" && inst.npu_index >= 0)
                    ? `NPU${inst.npu_index}` : "NPU?";
      const row = document.createElement("label");
      row.className = "vtd-inst-row" + (local ? " is-local" : " is-remote");
      if (!local) row.title = REMOTE_TIP;

      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = key;
      cb.checked = local && (saved ? saved.includes(key) : true);
      if (!local) cb.disabled = true;

      const txt = document.createElement("span");
      txt.className = "vtd-inst-label";
      txt.textContent =
        `${inst.label || inst.model_id} · ${local ? "本机" : "远端"} · ${npuText}`;

      row.appendChild(cb);
      row.appendChild(txt);
      box.appendChild(row);

      cb.addEventListener("change", () => {
        try {
          window.localStorage?.setItem(storageKey,
            JSON.stringify(getSelectedInstances(containerId)
              .map((x) => _instKey(x))));
        } catch (_) { /* best-effort */ }
      });
    }

    if (rows.length === 0) {
      const empty = document.createElement("div");
      empty.className = "vtd-inst-empty";
      empty.textContent = "无可用实例 —— 请先用向导的「一键开启所需模型」。";
      box.appendChild(empty);
    } else if (localKeys.size === 0) {
      const tip = document.createElement("div");
      tip.className = "vtd-inst-empty";
      tip.textContent = "本机没有可用实例；远端算力" + REMOTE_TIP + "。";
      box.appendChild(tip);
    }
    return rows;
  }

  function getSelectedInstances(containerId) {
    const box = document.getElementById(containerId);
    if (!box) return [];
    return Array.from(box.querySelectorAll('input[type="checkbox"]'))
      .filter((cb) => cb.checked && !cb.disabled)
      .map((cb) => _parseKey(cb.value));
  }

  /* ------------------------------------------------------------------
   * MultiCard —— 双卡并行推理的前端助手（视频目标监控 / 视频目标定位 共用）。
   *
   * 两张卡的差异只有「勾选器容器 id」，逻辑逐字相同，所以统一放这而不是
   * 各写一份。
   */

  /** 「正在推理」的通道集合，传给 MultiGrid.status 打绿框。
   *
   *  多卡时后端上报 inflight_channels（K 条 feeder 各占一路）；单卡路径没有
   *  这个字段，退回 vlm.current。 */
  function activeChannels(vlm) {
    vlm = vlm || {};
    if (Array.isArray(vlm.inflight_channels) && vlm.inflight_channels.length) {
      return vlm.inflight_channels;
    }
    return typeof vlm.current === "number" ? [vlm.current] : [];
  }

  /** 勾选的实例 → 后端 models[]。一个都没勾返回 null，由调用方提示。
   *
   *  同一个 model_id 出现在两个 npu_index 上就是「同模型跑两张卡并行」，也
   *  就是 models[] 的语义；只传 model_id 的话多卡整个退化成单卡。 */
  function selectedModels(containerId) {
    const models = (window.getSelectedInstances?.(containerId) || [])
      .map((m) => ({ model_id: m.model_id, npu_index: m.npu_index }));
    return models.length ? models : null;
  }

  /** 多卡模式？slot_count 是后端权威值；还没启动检测时退回数勾选框，
   *  让用户在点启动之前就知道自己是多卡模式。 */
  function isMultiCard(snap, containerId) {
    const n = snap?.slot_count;
    if (typeof n === "number" && n > 0) return n > 1;
    return (window.getSelectedInstances?.(containerId) || []).length > 1;
  }

  /** 事件行的 NPU 角标元素；单卡返回 null（调用方直接不 append）。
   *
   *  通道号只说明"看的是第几路"，看不出"谁算的" —— 排查"是不是只有一张卡
   *  在干活"时这行是唯一线索。 */
  function npuBadge(ev, snap, containerId) {
    if (!isMultiCard(snap, containerId)) return null;
    const el = document.createElement("span");
    el.className = "vd-event-npu";
    el.textContent = (typeof ev.npu_index === "number" && ev.npu_index >= 0)
      ? "NPU" + ev.npu_index : "NPU?";
    return el;
  }

  window.loadInstancePickMulti = loadInstancePickMulti;
  window.getSelectedInstances = getSelectedInstances;
  window.MultiCard = { activeChannels, selectedModels, isMultiCard, npuBadge };
})();