(function () {
  "use strict";
  const API_PREFIX = "/api";
  // core helpers (fmtNum, fmtEventTime, sanitizeEventText, loadModelPick) via app/static/core/*.js
  const fmtNum = window.fmtNum;
  const fmtEventTime = window.fmtEventTime;
  const sanitizeEventText = window.sanitizeEventText;
  const loadModelPick = window.loadModelPick;

const VD = {
  ws: null,
  wsRetry: 0,
  wsTimer: null,
  blobUrl: null,
  inflight: 0,
  tools: [],
  enabledTools: [],
  lastSnap: null,
  detectorSnap: null,
  audioSnap: null,   // AudioAnalysis 状态快照（{state, progress, ...}）
  init() { return initVideoDetection(); },
};

const VDDetector = {
  events: [],
  badgeTimer: null,
  refresh() { return refreshVdDetector(); },
};

  // ...
const AudioAnalysis = {
  ws: null,
  wsRetry: 0,
  wsTimer: null,
  events: [],
  init() {
    const startBtn = document.getElementById("vd-audio-start");
    const stopBtn  = document.getElementById("vd-audio-stop");
    const clearBtn = document.getElementById("vd-audio-events-clear");
    if (startBtn) startBtn.onclick = () => AudioAnalysis.onStart();
    if (stopBtn)  stopBtn.onclick  = () => AudioAnalysis.onStop();
    if (clearBtn) clearBtn.onclick = () => AudioAnalysis.onClear();
    this.ensureWs();
    this.refresh();
  },

  async refresh() {
    try {
      const r = await fetch(API_PREFIX + "/audio-analysis", { cache: "no-store" });
      if (!r.ok) return;
      const snap = await r.json();
      setVdAudioMeta(snap);
  // ...
      const audioModelEl = document.getElementById("vd-audio-model-pick");
      if (audioModelEl && audioModelEl.disabled) {
        loadModelPick("vd-audio-model-pick",
                      "swcap_audio_understand", "vd.audio.model_id");
      }
      return snap;
    } catch (_) { return null; }
  },

  async hydrateEvents() {
    try {
      const r = await fetch(API_PREFIX + "/audio-analysis/events", { cache: "no-store" });
      if (!r.ok) return;
      const data = await r.json();
      this.events = Array.isArray(data.events) ? data.events.slice() : [];
      this.render();
    } catch (_) { /* best-effort */ }
  },

  async onStart() {
    const sourceEl = document.getElementById("vd-source");
    const promptEl = document.getElementById("vd-audio-prompt");
    const source = (sourceEl?.value || "").trim();
    const prompt = (promptEl?.value || "").trim();
    if (!source) { alert("请先在上方视频源里打开一个视频文件"); return; }
    if (!prompt) { alert("请输入音频理解提示词"); return; }
    this.events = [];
    this.render();
    try {
      const model_id = (document.getElementById("vd-audio-model-pick")?.value || "") || null;
      const r = await fetch(API_PREFIX + "/audio-analysis/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source, prompt, model_id }),
      });
      if (!r.ok) {
        const e = await r.json().catch(() => ({}));
        alert(`启动音频分析失败: ${e.detail || r.status}`);
        return;
      }
      const snap = await r.json();
      setVdAudioMeta(snap);
    } catch (e) {
      alert("启动音频分析失败: " + e.message);
    }
  },

  async onStop() {
    try {
      await fetch(API_PREFIX + "/audio-analysis/stop", { method: "POST" });
    } catch (_) { /* best-effort */ }
    this.refresh();
  },

  async onClear() {
    try {
      await fetch(API_PREFIX + "/audio-analysis/events/clear", { method: "POST" });
    } catch (_) { /* best-effort */ }
    this.events = [];
    this.render();
  },

  ensureWs() {
    if (this.ws && this.ws.readyState <= 1) return;
    this.wsRetry = Math.min(this.wsRetry + 1, 6);
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}${API_PREFIX}/audio-analysis/ws`;
    try {
      this.ws = new WebSocket(url);
    } catch (_) {
      this.wsTimer = setTimeout(() => this.ensureWs(),
        Math.min(1000 * (1 << (this.wsRetry - 1)), 30000));
      return;
    }
    this.ws.onmessage = (ev) => AudioAnalysis.onWsMessage(ev);
    this.ws.onclose = () => {
      AudioAnalysis.ws = null;
      const wait = Math.min(1000 * (1 << (AudioAnalysis.wsRetry - 1)), 30000);
      AudioAnalysis.wsTimer = setTimeout(() => AudioAnalysis.ensureWs(), wait);
    };
    this.ws.onerror = () => { try { this.ws && this.ws.close(); } catch (_) {} };
  },

  onWsMessage(ev) {
    let m;
    try { m = JSON.parse(ev.data); } catch (_) { return; }
    if (m.type === "hello") {
      this.hydrateEvents();
      setVdAudioMeta(m);
    } else if (m.type === "state") {
      setVdAudioMeta(m);
  // ...
      if (m.state === "done" || m.state === "error" || m.state === "stopped") {
        this.hydrateEvents();
      }
    } else if (m.type === "progress") {
      setVdAudioMeta({
        state: VD.audioSnap ? VD.audioSnap.state : "extracting",
        progress: [m.done, m.total],
        source: VD.audioSnap && VD.audioSnap.source,
        events_count: this.events.length,
      });
    } else if (m.type === "event" && m.event) {
      this.events.push(m.event);
      this.render();
      const cntEl = document.getElementById("vd-audio-events-count");
      if (cntEl) cntEl.textContent = `${this.events.length} 段`;
    }
  },

  render() {
    const ol = document.getElementById("vd-audio-events");
    if (!ol) return;
    if (!this.events.length) {
      ol.innerHTML = '<li class="vd-audio-empty">启动音频分析后，按 6 秒切片逐段推理，结果显示在这里。</li>';
      return;
    }
    ol.innerHTML = "";
    this.events.forEach((ev) => {
      const li = document.createElement("li");
      const errClass = ev.level === "error" ? " vd-audio-ev--err" : "";
      li.className = "vd-audio-ev" + errClass;
      const ts = document.createElement("span");
      ts.className = "vd-audio-ts";
      ts.textContent = `[${fmtTime(ev.start_s)}–${fmtTime(ev.end_s)}]`;
      const tx = document.createElement("span");
      tx.className = "vd-audio-text";
      tx.textContent = sanitizeEventText(ev.text || "");
      li.append(ts, tx);
      ol.appendChild(li);
    });
    const cntEl = document.getElementById("vd-audio-events-count");
    if (cntEl) cntEl.textContent = `${this.events.length} 段`;
  },
};

function fmtTime(s) {
  if (s == null || isNaN(s)) return "--:--";
  const m = Math.floor(s / 60);
  const r = Math.round(s % 60);
  return `${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
}

function setVdOverlay(text, isError) {
  const el = document.getElementById("vd-overlay");
  if (!el) return;
  el.textContent = text || "";
  el.classList.toggle("error", !!isError);
  el.style.display = text ? "" : "none";
}

function setVdMeta(snap) {
  const el = document.getElementById("vd-meta");
  if (!el) return;
  const parts = [];
  if (snap.width && snap.height) parts.push(snap.width + "x" + snap.height);
  if (snap.fps) parts.push(fmtNum(snap.fps, 2) + " fps");
  if (snap.frame_pos != null) parts.push("帧 " + snap.frame_pos);
  if (snap.is_live) parts.push("LIVE");
  if (snap.state) parts.push("[" + snap.state + "]");
  el.textContent = parts.join(" · ") || "--";
  VD.lastSnap = snap;
  if (typeof DemoGuard !== "undefined") {
    DemoGuard.report("video-detection", {
      session:  VD.lastSnap,
      detector: VD.detectorSnap,
      audio:    VD.audioSnap,
    });
  }
}

  // ...
function setVdAudioMeta(snap) {
  VD.audioSnap = snap || null;
  const srcEl = document.getElementById("vd-audio-source");
  if (srcEl) {
    srcEl.textContent = (snap && snap.source)
      || document.getElementById("vd-source")?.value?.trim()
      || "—";
  }
  const statusEl = document.getElementById("vd-audio-status");
  const startBtn = document.getElementById("vd-audio-start");
  const stopBtn  = document.getElementById("vd-audio-stop");
  if (statusEl) {
    const s = snap && snap.state;
    if (s === "extracting") {
      const [done, total] = (snap.progress || [0, 0]);
      statusEl.textContent = `分析中 ${done}/${total}`;
    } else if (s === "done") {
      statusEl.textContent = `完成 · ${snap.events_count || 0} 段`;
    } else if (s === "stopped") {
      statusEl.textContent = "已停止";
    } else if (s === "error") {
      statusEl.textContent = "错误: " + (snap.error || "");
    } else {
      statusEl.textContent = (snap && snap.available === false)
        ? "功能未启用"
        : "未启动";
    }
  }
  if (startBtn) startBtn.disabled = !!(snap && snap.state === "extracting");
  if (stopBtn)  stopBtn.disabled  = !(snap && snap.state === "extracting");
  const progEl = document.getElementById("vd-audio-progress");
  if (progEl) {
    if (snap && snap.state === "extracting") {
      const [done, total] = (snap.progress || [0, 0]);
      progEl.textContent = total > 0 ? `已处理 ${done} / ${total} 段（每段 ≤ 6 秒）` : "";
    } else {
      progEl.textContent = "";
    }
  }
  if (typeof DemoGuard !== "undefined") {
    DemoGuard.report("video-detection", {
      session:  VD.lastSnap,
      detector: VD.detectorSnap,
      audio:    VD.audioSnap,
    });
  }
}

function setVdProgress(snap) {
  const el = document.getElementById("vd-progress");
  if (!el) return;
  el.classList.toggle("live", !!snap.is_live);
  el.style.width = snap.is_live ? "100%" : "0%";
}

async function drawVdFrame(jpgBytes) {
  const canvas = document.getElementById("vd-canvas");
  if (!canvas || !jpgBytes || !jpgBytes.byteLength) return;
  const blob = new Blob([jpgBytes], { type: "image/jpeg" });
  if (VD.blobUrl) URL.revokeObjectURL(VD.blobUrl);
  VD.blobUrl = URL.createObjectURL(blob);
  const img = new Image();
  await new Promise((resolve, reject) => {
    img.onload = resolve;
    img.onerror = reject;
    img.src = VD.blobUrl;
  });
  const ctx = canvas.getContext("2d");
  canvas.width = img.naturalWidth || canvas.width;
  canvas.height = img.naturalHeight || canvas.height;
  ctx.drawImage(img, 0, 0);
}

/* Strip chatml / vision / control characters that the model may have
 * leaked into desc / text fields. Mirrors the backend
 * ``_sanitize_event_text`` so legacy in-memory events get cleaned too.
 *
 * v0.4+: 本函数定义在 ``app/static/core/format-utils.js``,IIFE 顶部
 * 取得别名 ``const sanitizeEventText = window.sanitizeEventText;`` 后
 * 在此 IIFE 内继续以裸名引用即可。删除下方本地副本,统一 core 版本,
 * 防止其它卡片(尤其 video_target_detection / video_target_localization)
 * 漏拷贝时 ReferenceError 阻塞 DOM 渲染。
 */

function connectVdWs() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}${API_PREFIX}/video-detection/ws`;
  const ws = new WebSocket(url);
  ws.binaryType = "arraybuffer";
  VD.ws = ws;

  ws.onopen = () => {
    VD.wsRetry = 0;
    setVdOverlay("", false);
  };
  ws.onmessage = async (ev) => {
    if (typeof ev.data === "string") {
      let snap;
      try { snap = JSON.parse(ev.data); } catch (_) { return; }
      if (snap.type === "eof") {
        setVdOverlay("文件已结束 (EOF)，点 播放 可恢复", true);
      } else if (snap.type === "error") {
        setVdOverlay("WS 错误: " + (snap.detail || ""), true);
      } else if (snap.type === "event") {
        addVdEvent(snap);
        showVdEventBadge(snap);
        return;
      } else if (snap.type === "status" && snap.detector) {
        updateVdDetectorStatus(snap.detector);
      }
      setVdMeta(snap);
      setVdProgress(snap);
      refreshVdButtons(snap.state);
      document.getElementById("data-video-detection").textContent =
        JSON.stringify(snap, null, 2);
      return;
    }
  // ...
    try {
      await drawVdFrame(new Uint8Array(ev.data));
      setVdOverlay("", false);
    } catch (e) {
      setVdOverlay("帧渲染失败: " + e, true);
    }
  };
  ws.onclose = () => {
    VD.ws = null;
    const wait = Math.min(5000, 500 * Math.pow(2, VD.wsRetry++));
    VD.wsTimer = setTimeout(connectVdWs, wait);
  };
  ws.onerror = () => {
    try { ws.close(); } catch (_) {}
  };
}

async function vdOpen(payload) {
  const r = await fetch(API_PREFIX + "/video-detection/open", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!r.ok) {
    const detail = await r.text().catch(() => "");
    throw new Error("HTTP " + r.status + " " + detail);
  }
  return r.json();
}

async function vdAction(action) {
  const r = await fetch(API_PREFIX + "/video-detection/control", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action }),
  });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

async function vdStatus() {
  const r = await fetch(API_PREFIX + "/video-detection", { cache: "no-store" });
  if (!r.ok) return null;
  const snap = await r.json();
  setVdMeta(snap);
  setVdProgress(snap);
  refreshVdButtons(snap.state);
  const loopEl = document.getElementById("vd-loop");
  if (loopEl && snap.loop != null) loopEl.checked = !!snap.loop;
  document.getElementById("data-video-detection").textContent =
    JSON.stringify(snap, null, 2);
  return snap;
}

function refreshVdButtons(state) {
  const playBtn = document.getElementById("vd-play");
  const pauseBtn = document.getElementById("vd-pause");
  const stopBtn = document.getElementById("vd-stop");
  if (!playBtn || !pauseBtn || !stopBtn) return;
  const active = state === "playing" || state === "paused" || state === "eof";
  stopBtn.disabled = !active;
  if (state === "playing") {
    playBtn.disabled = true;
    pauseBtn.disabled = false;
  } else if (state === "paused") {
    playBtn.disabled = false;
    pauseBtn.disabled = true;
  } else {
    playBtn.disabled = !active;
    pauseBtn.disabled = !active;
  }
}

async function vdClose() {
  const r = await fetch(API_PREFIX + "/video-detection/close", { method: "POST" });
  return r.ok ? r.json() : null;
}

/* ------------------------------------------------------------------ *
 *  VLM detection (wdnai) — prompt + event history + overlay            *
 * ------------------------------------------------------------------ */

function addVdEvent(ev) {
  VDDetector.events.unshift(ev);
  if (VDDetector.events.length > 200) VDDetector.events.length = 200;
  renderVdEventList();
}

function renderVdEventList() {
  const list = document.getElementById("vd-event-list");
  const count = document.getElementById("vd-events-count");
  if (!list || !count) return;
  count.textContent = VDDetector.events.length + " 条";
  if (VDDetector.events.length === 0) {
    list.innerHTML =
      '<li class="vd-event-empty">尚未检测到事件，启动检测后结果显示在这里。</li>';
    return;
  }
  list.innerHTML = "";
  VDDetector.events.forEach((ev, idx) => {
    const li = document.createElement("li");
    li.className = "vd-event" + (idx === 0 ? " latest" : "");
    const meta = document.createElement("div");
    meta.className = "vd-event-meta";
    const tsSpan = document.createElement("span");
    tsSpan.textContent = fmtEventTime(ev.ts);
    const fpSpan = document.createElement("span");
    fpSpan.textContent = "帧 " + (ev.frame_pos ?? "?");
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = "#" + ev.id;
    meta.appendChild(tsSpan);
    meta.appendChild(fpSpan);
    meta.appendChild(tag);
    const text = document.createElement("div");
    text.className = "vd-event-text";
    text.textContent = sanitizeEventText(ev.text) || "(空)";
    li.appendChild(meta);
    li.appendChild(text);
    list.appendChild(li);
  });
}

let vdBadgeHideTimer = null;
function showVdEventBadge(ev) {
  const badge = document.getElementById("vd-event-badge");
  const text = document.getElementById("vd-event-badge-text");
  if (!badge || !text) return;
  text.textContent = sanitizeEventText(ev.text) || "(空)";
  badge.classList.remove("hidden");
  // ...
  badge.style.animation = "none";
  // ...
  badge.offsetHeight;
  badge.style.animation = "";
  if (vdBadgeHideTimer) clearTimeout(vdBadgeHideTimer);
  vdBadgeHideTimer = setTimeout(() => {
    badge.classList.add("hidden");
  }, 6000);
}

function updateVdDetectorStatus(snap) {
  const statusEl = document.getElementById("vd-detector-status");
  const startBtn = document.getElementById("vd-detector-start");
  const stopBtn  = document.getElementById("vd-detector-stop");
  if (!snap || !statusEl) return;
  if (!snap.available) {
    statusEl.textContent = "不可用: " + (snap.init_error || "wdnai 未安装");
  } else if (snap.init_error) {
    statusEl.textContent = "模型加载失败: " + snap.init_error;
  } else if (snap.enabled && snap.session_ready) {
    const toolsCount = (snap.enabled_tools || []).length;
    const toolsHint = toolsCount ? ` · ${toolsCount} 个工具` : "";
    const sizeHint = (typeof snap.image_size === "number")
      ? ` · 尺寸 ${snap.image_size}`
      : "";
    statusEl.textContent =
      `运行中 · 每 ${snap.every_n} 帧 · 队列 ${snap.queue_size}${toolsHint}${sizeHint}`;
  } else if (snap.enabled) {
    statusEl.textContent =
      `已启动，加载模型… (首次约 30s，队列 ${snap.queue_size})`;
  } else if (snap.session_ready) {
    statusEl.textContent = "已加载";
  } else {
    statusEl.textContent = "已安装但未启动";
  }
  if (startBtn) startBtn.disabled = !snap.available || !!snap.enabled;
  if (stopBtn)  stopBtn.disabled  = !snap.enabled;
  VD.detectorSnap = snap;
  if (typeof DemoGuard !== "undefined") {
    DemoGuard.report("video-detection", {
      session:  VD.lastSnap,
      detector: VD.detectorSnap,
      audio:    VD.audioSnap,
    });
  }
}

async function refreshVdDetector() {
  try {
    const r = await fetch(API_PREFIX + "/video-detection/events");
    if (!r.ok) return null;
    const data = await r.json();
    updateVdDetectorStatus(data.snapshot || {});
  // ...
    if (VDDetector.events.length === 0 && Array.isArray(data.events)) {
  // ...
      VDDetector.events = data.events.slice();
      renderVdEventList();
    }
  // ...
    const modelEl = document.getElementById("vd-model-pick");
    if (modelEl && modelEl.disabled) {
      loadModelPick("vd-model-pick", "swcap_video_describe", "vd.model_id");
    }
    return data;
  } catch (_) { return null; }
}



async function vdDetectorStart() {
  const promptEl  = document.getElementById("vd-prompt");
  const everyEl   = document.getElementById("vd-every-n");
  const sizeEl    = document.getElementById("vd-detect-size");
  const prompt = (promptEl?.value || "").trim();
  const every_n = parseInt(everyEl?.value || "30", 10);
  if (!prompt) {
    setVdOverlay("请先填写检测提示词", true);
    return;
  }
  // ...
  const sizeVal = sizeEl?.value || "";
  let image_size = null;
  if (sizeVal && sizeVal !== "auto") {
    image_size = parseInt(sizeVal, 10);
    if (!Number.isFinite(image_size)) {
      setVdOverlay("检测尺寸值非法: " + sizeVal, true);
      return;
    }
  }
  const enabled_tools = vdCollectEnabledTools();
  const model_id = (document.getElementById("vd-model-pick")?.value || "") || null;
  try {
    const r = await fetch(API_PREFIX + "/video-detection/prompt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        prompt, every_n, enabled_tools,
        image_size: Number.isFinite(image_size) ? image_size : null,
        model_id: model_id,
      }),
    });
    if (!r.ok) {
      const detail = await r.text().catch(() => "");
      setVdOverlay("启动检测失败: " + detail, true);
      return;
    }
    const snap = await r.json();
    updateVdDetectorStatus(snap);
    setVdOverlay("", false);
  } catch (e) {
    setVdOverlay("启动检测失败: " + e.message, true);
  }
}

/**
 * Populate a model-pick ``<select>`` (``#vd-model-pick`` /
 * ``#vd-audio-model-pick`` / ``#vtd-model-pick``) with running compute
 * instances exposing ``capability``.
 *
 * Data flow:
 *   - GET /api/compute/instances-by-capability?capability=X returns
 *     ``{capability, instances: [{model_id, label, source, npu_index,
 *     pid, memory_estimate_mb}, ...]}`` sorted local-first (mirrors
 *     ``get_session`` auto-pick semantics).
 *   - Empty list → dropdown 留 "自动 (本机优先)" 单选项 + 灰色禁用,
 *     提示用户当前 capability 没有 running 实例。
 *   - N entries → "自动" + N 实例选项;
 *     用户保存的选择从 localStorage 读,选中回填。
 *   - 用户切换下拉框时也写 localStorage,刷新页面后保留。
 *
 * 控件语义:
 *   - option.value="" → 服务端 get_session auto-pick (默认行为)。
 *   - option.value=model_id → 服务端 get_session_for_model 锁定。
 */
function vdCollectEnabledTools() {
  const root = document.getElementById("vd-tools-list");
  if (!root) return [];
  const out = [];
  root.querySelectorAll("input[type=checkbox][data-tool-key]").forEach((el) => {
    if (el.checked && !el.disabled) out.push(el.dataset.toolKey);
  });
  return out;
}

async function vdLoadTools() {
  const root = document.getElementById("vd-tools-list");
  const summary = document.getElementById("vd-tools-summary");
  if (!root) return;
  let data = { tools: [] };
  try {
    const r = await fetch(API_PREFIX + "/video-detection/tools");
    if (!r.ok) throw new Error("HTTP " + r.status);
    data = await r.json();
  } catch (e) {
    root.innerHTML = `<div class="vd-tools-empty">加载失败: ${e.message}</div>`;
    if (summary) summary.textContent = "加载失败";
    return;
  }
  VD.tools = data.tools || [];
  renderVdTools();
}

function renderVdTools() {
  const root = document.getElementById("vd-tools-list");
  const summary = document.getElementById("vd-tools-summary");
  if (!root) return;
  const tools = VD.tools || [];
  if (!tools.length) {
    root.innerHTML = `<div class="vd-tools-empty">当前无工具</div>`;
    if (summary) summary.textContent = "0 个工具";
    return;
  }
  root.innerHTML = "";
  tools.forEach((t) => {
    const card = document.createElement("label");
    card.className = "vd-tool-card" + (t.available ? "" : " is-disabled");
    if (VD.enabledTools.includes(t.key)) card.classList.add("is-selected");
    const check = document.createElement("input");
    check.type = "checkbox";
    check.dataset.toolKey = t.key;
  // ...
    check.disabled = false;
    if (VD.enabledTools.includes(t.key)) check.checked = true;
    check.addEventListener("change", () => {
      card.classList.toggle("is-selected", check.checked);
      if (check.checked) {
        if (!VD.enabledTools.includes(t.key)) VD.enabledTools.push(t.key);
      } else {
        VD.enabledTools = VD.enabledTools.filter((k) => k !== t.key);
      }
      updateVdToolsSummary();
    });
    const body = document.createElement("div");
    body.className = "vd-tool-card-body";
    const title = document.createElement("div");
    title.className = "vd-tool-card-title";
    const icon = document.createElement("span");
    icon.className = "vd-tool-card-icon";
    icon.textContent = t.icon || "🛠";
    const name = document.createElement("span");
    name.className = "vd-tool-card-name";
    name.textContent = t.name;
    const key = document.createElement("span");
    key.className = "vd-tool-card-key";
    key.textContent = t.key;
    title.appendChild(icon);
    title.appendChild(name);
    title.appendChild(key);
    const desc = document.createElement("div");
    desc.className = "vd-tool-card-desc";
    desc.textContent = t.description;
    body.appendChild(title);
    body.appendChild(desc);
    if (t.requires_capability) {
      const dep = document.createElement("div");
      dep.className = "vd-tool-card-dep " + (t.available ? "is-ok" : "is-bad");
      dep.textContent = t.available
        ? `依赖 ${t.requires_capability} · ✓ 可用`
        : `依赖 ${t.requires_capability} · ⚠ ${t.reason || "未启用"}`;
      body.appendChild(dep);
    }
    card.appendChild(check);
    card.appendChild(body);
    card.addEventListener("click", (ev) => {
      if (ev.target.tagName === "INPUT") return;
      if (check.disabled) return;
      check.checked = !check.checked;
      check.dispatchEvent(new Event("change"));
    });
    root.appendChild(card);
  });
  updateVdToolsSummary();
}

function updateVdToolsSummary() {
  const summary = document.getElementById("vd-tools-summary");
  if (!summary) return;
  const enabled = vdCollectEnabledTools();
  const total = (VD.tools || []).length;
  summary.textContent = enabled.length
    ? `已启用 ${enabled.length} / ${total}`
    : `未启用（${total} 个可选）`;
}

async function vdDetectorStop() {
  try {
    const r = await fetch(API_PREFIX + "/video-detection/prompt/stop", { method: "POST" });
    if (r.ok) updateVdDetectorStatus(await r.json());
  } catch (e) {
    setVdOverlay("停止检测失败: " + e.message, true);
  }
}

async function vdEventsClear() {
  try {
    await fetch(API_PREFIX + "/video-detection/events/clear", { method: "POST" });
    VDDetector.events = [];
    renderVdEventList();
  } catch (_) {}
}

function initVideoDetection() {
  if (window.__vdInit) return;
  window.__vdInit = true;

  // ...
  loadModelPick("vd-model-pick", "swcap_video_describe", "vd.model_id");
  loadModelPick("vd-audio-model-pick", "swcap_audio_understand", "vd.audio.model_id");

  const sourceEl = document.getElementById("vd-source");
  const loopEl   = document.getElementById("vd-loop");
  const openBtn = document.getElementById("vd-open");
  const closeBtn = document.getElementById("vd-close");
  const browseBtn = document.getElementById("vd-browse");
  const playBtn = document.getElementById("vd-play");
  const pauseBtn = document.getElementById("vd-pause");
  const stopBtn = document.getElementById("vd-stop");

  sourceEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter") openBtn.click();
  });

  openBtn.addEventListener("click", async () => {
    const src = (sourceEl.value || "").trim();
    if (!src) {
      setVdOverlay("请输入或选择视频源", true);
      return;
    }
    openBtn.disabled = true;
    setVdOverlay("正在打开 " + src + " …", false);
    try {
      const snap = await vdOpen({ source: src, loop: !!(loopEl && loopEl.checked) });
      setVdMeta(snap);
      setVdProgress(snap);
      if (loopEl && snap.loop != null) loopEl.checked = !!snap.loop;
    } catch (e) {
      setVdOverlay("打开失败: " + e.message, true);
    } finally {
      openBtn.disabled = false;
    }
  });

  // ...
  browseBtn.addEventListener("click", () => {
    window.VdFsModal._sourceTarget = sourceEl;
    window.VdFsModal.open("");
  });
  // ...

  closeBtn.addEventListener("click", async () => {
    try { await vdClose(); } catch (_) {}
    setVdOverlay("已关闭", false);
  });

  playBtn.addEventListener("click", async () => {
    try {
      const r = await vdAction("play");
      await vdStatus();  // immediate UI feedback so the click feels responsive
      if (r && r.ok === false) {
        setVdOverlay("播放失败: " + (r.reason || r.state), true);
      } else {
        setVdOverlay("", false);
      }
    } catch (e) {
      setVdOverlay("播放失败: " + e.message, true);
    }
  });
  pauseBtn.addEventListener("click", async () => {
    try {
      const r = await vdAction("pause");
      await vdStatus();
      if (r && r.ok === false) {
        setVdOverlay("暂停失败: " + (r.reason || r.state), true);
      } else {
        setVdOverlay("", false);
      }
    } catch (e) {
      setVdOverlay("暂停失败: " + e.message, true);
    }
  });
  stopBtn.addEventListener("click", async () => {
    try {
      const r = await vdAction("stop");
      await vdStatus();
      if (r && r.ok === false) {
        setVdOverlay("停止失败: " + (r.reason || r.state), true);
      } else {
        setVdOverlay("已停止", false);
      }
    } catch (e) {
      setVdOverlay("停止失败: " + e.message, true);
    }
  });

  // ...
  const detStartBtn = document.getElementById("vd-detector-start");
  const detStopBtn  = document.getElementById("vd-detector-stop");
  const evClearBtn  = document.getElementById("vd-events-clear");
  if (detStartBtn) detStartBtn.addEventListener("click", vdDetectorStart);
  if (detStopBtn)  detStopBtn.addEventListener("click",  vdDetectorStop);
  if (evClearBtn)  evClearBtn.addEventListener("click",  vdEventsClear);

  // ...
  refreshVdDetector();
  vdLoadTools();
  AudioAnalysis.init();
  if (!VD.ws && !VD.wsTimer) connectVdWs();
}

/* ------------------------------------------------------------------ *
 * 视频目标监控 (Video Target Detection) — TargetDoc 结构化输出           *
 * ------------------------------------------------------------------ *
 * 与音视频描述页 (`VD`) 结构一致；区别：                               *
 *   - API 前缀 `/api/video-target-detection/*`                         *
 *   - 后端 detector 直接走 `session.respond(generating=TargetDoc)`   *
 *     结构化输出拿 `x1, y1, x2, y2` + `desc`，**不**挂任何           *
 *     `@Callable` 工具，因此本页没有工具集合概念，也没有              *
 *     `/video-target-detection/tools` 端点                            *
 *   - 后端 detector 实例独立 (`get_target_detector()`)，与描述页互不   *
 *     干扰                                                              *
 *   - 事件历史：`ev.tool_call === "target_detection"` 用于识别 TargetDoc *
 *     事件；带 `crop_b64` 的事件同时挂缩略图 + 描述文本               *
 * ------------------------------------------------------------------ */


initVideoDetection();

// ------------------------------------------------------------------ *
// 应用向导模式 API — wizard 框架 finish 时调用
// 契约:opts 字段由 wizard.json 的 steps[].fields[].id 派生(去 app-id 前缀 + camelCase):
//   vd-source       → opts.source
//   vd-loop         → opts.loop
//   vd-prompt       → opts.prompt
//   vd-detect-size  → opts.detectSize
//   vd-audio-prompt → opts.audioPrompt
// 空字段不触发对应启动动作;失败抛 Error("WizardSnapshotError:<code>:<detail>")
//   * open_source_failed    → wizard 回 source 步骤
//   * detector_start_failed → wizard 弹"启动失败"提示
//   * audio_start_failed    → wizard 弹"启动失败"提示
// ------------------------------------------------------------------ *
window.ImApp_vd = window.ImApp_vd || {};
window.ImApp_vd.applyWizardSnapshot = async function (opts) {
  const setVal = (id, v) => { const el = document.getElementById(id); if (el != null) el.value = v; };
  const setChk = (id, v) => { const el = document.getElementById(id); if (el != null) el.checked = !!v; };

  // 1. 写回非空字段到表单(留给用户回看 + 应用自有 init 也用得到)
  if (opts.source)      setVal("vd-source", opts.source);
  if (opts.loop != null) setChk("vd-loop", opts.loop);
  if (opts.prompt)      setVal("vd-prompt", opts.prompt);
  if (opts.detectSize)  setVal("vd-detect-size", opts.detectSize);
  if (opts.audioPrompt) setVal("vd-audio-prompt", opts.audioPrompt);

  const result = { source_opened: false, detector_started: false, audio_started: false };

  // 2. 打开视频源(仅 source 非空)
  if (opts.source) {
    try {
      await vdOpen({ source: opts.source, loop: !!opts.loop });
      result.source_opened = true;
    } catch (e) {
      throw new Error("WizardSnapshotError:open_source_failed:" + (e?.message || e));
    }
  }

  // 3. 启动检测(仅 prompt 非空)— 模拟点击原应用页面的"启动检测"按钮
  if (opts.prompt) {
    const btn = document.getElementById("vd-detector-start");
    if (btn) {
      try { btn.click(); result.detector_started = true; }
      catch (e) { throw new Error("WizardSnapshotError:detector_start_failed:" + (e?.message || e)); }
    }
  }

  // 4. 启动音频理解(仅 audioPrompt 非空)
  if (opts.audioPrompt) {
    const btn = document.getElementById("vd-audio-start");
    if (btn) {
      try { btn.click(); result.audio_started = true; }
      catch (e) { throw new Error("WizardSnapshotError:audio_start_failed:" + (e?.message || e)); }
    }
  }

  return result;
};

})();
