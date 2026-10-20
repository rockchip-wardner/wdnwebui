(function () {
  "use strict";
  const API_PREFIX = "/api";
  // core helpers via app/static/core/*.js
  const fmtEventTime = window.fmtEventTime;
  const sanitizeEventText = window.sanitizeEventText;
  const loadModelPick = window.loadModelPick;

const VD = {
  ws: null,
  wsRetry: 0,
  wsTimer: null,
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
// 音轨分析面板。**实现在 app/static/core/audio-analysis.js** ——
// app/apps/audio_analysis/static/app.js 原来也有一份拷贝，两份必然漂
// （多路改造只改一份，另一份就退回「只分析第 1 路」）。
const AudioAnalysis = window.AudioAnalysisCore
  ? window.AudioAnalysisCore.mount({
      apiPrefix: API_PREFIX,
      getSources: () => readVdSources(),
      onSnapshot: (snap) => {
        VD.audioSnap = snap || null;
        // DemoGuard 靠这个上报判断「本页在跑」——离开页面时要能停掉
        // 音轨分析。共享模块只管渲染，上报留给调用方。
        if (typeof DemoGuard !== "undefined") {
          DemoGuard.report("video-detection", {
            session:  VD.lastSnap,
            detector: VD.detectorSnap,
            audio:    VD.audioSnap,
          });
        }
      },
    })
  : {};

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
  const n = snap.channels || 0;
  // 送检尺寸比显示尺寸更值得让人看见：它决定 VLM 选哪个 vision 变体
  // （送 384 就是掉进便宜档换精度降档），运维排查"识别变差"时先看它。
  if (snap.ana_w && snap.ana_h) parts.push("送检 " + snap.ana_w + "x" + snap.ana_h);
  if (snap.disp_w && snap.disp_h) parts.push(snap.disp_w + "x" + snap.disp_h);
  if (n) parts.push(n + " 路");
  if (snap.total_frames) parts.push("帧 " + snap.total_frames);
  if (snap.total_errors) parts.push("异常 " + snap.total_errors);
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
function setVdProgress(snap) {
  const el = document.getElementById("vd-progress");
  if (!el) return;
  // 直播源没有总帧数，宽度不代表"跑了多少"；单路时看 snap.is_live，
  // 多路下只能逐路看 channel_list[].source。
  const list = snap.channel_list || [];
  const anyFile = list.some(c => !c.source?.includes("://")
                              && !String(c.source || "").startsWith("/dev/"));
  el.classList.toggle("live", !anyFile && list.length > 0);
  el.style.width = (list.length > 0 && !anyFile) ? "100%" : "0%";
}

/* 宫格（建格 / 分列 / 承帧 / 角标）复用 app/static/core/multi-grid.js，
 * 与 vtd / vtl 同款；本页只负责传 idx + 提示层文案。 */
const GRID_EL = "vd-grid";
const MAX_CHANNELS = 32;

function updateVdTiles(session) {
  const list = session.channel_list || [];
  const cur = typeof session.vlm?.current === "number" ? session.vlm.current : -1;
  const { total, ok, eof } = window.MultiGrid.status(GRID_EL, list, cur);
  const hint = document.getElementById("vd-channel-hint");
  if (hint) {
    if (!total) hint.textContent = "尚未打开视频";
    else if (ok === total) hint.textContent = `${total} 路视频 · 全部正常`;
    else hint.textContent = `${total} 路视频 · ${ok} 路正常`;
  }
  // 全部路 eof：画面定格在最后一帧，不说的话用户只会当成"画面卡住"。
  if (eof >= total && total > 0) {
    setVdOverlay("全部路已播完（EOF）", true);
  }
  window.ZoomFrame?.tick(GRID_EL, session, total);
}

// 宫格放大的接线。attach 必须在 IIFE 内：onError 要用私有的 setVdOverlay。
window.ZoomFrame?.attach({
  gridEl: GRID_EL,
  apiBase: API_PREFIX + "/video-detection",
  onError: (msg) => setVdOverlay(msg, true),
});

/** 从视频源文本框读出 sources 列表（每行一路，去空行去重）。
 *
 *  **唯一解析入口**：「打开视频」按钮与向导 applyWizardSnapshot 都走它。
 *  分成两份就会各自漂，然后在某条路径上退回"整个多行当一路"（向导里填多路
 *  就异常，填单路反而正常）。 */
function readVdSources() {
  const ta = document.getElementById("vd-sources");
  if (!ta) return [];
  const out = [];
  for (const raw of (ta.value || "").split(/\r?\n/)) {
    const s = raw.trim();
    if (!s) continue;
    out.push(s);
  }
  return out;
}

/** 路数校验（1~32）。返回 `{ok, sources, reason}`。
 *
 *  reason 同时喂给 overlay 提示与向导的 open_source_failed：前端先拦一次是
 *  为了立刻给可读提示，而不是等一个 400 + JSON detail 回来。 */
function validateVdSources() {
  const sources = readVdSources();
  if (!sources.length) {
    return { ok: false, sources: [], reason: "视频源不能为空" };
  }
  if (sources.length > MAX_CHANNELS) {
    return { ok: false, sources: [],
             reason: `最多 ${MAX_CHANNELS} 路，当前 ${sources.length} 路` };
  }
  return { ok: true, sources, reason: "" };
}

/* sanitizeEventText 定义在 app/static/core/format-utils.js，此处只取别名：
 * 保留本地副本会让别的卡片漏拷贝时 ReferenceError 阻塞 DOM 渲染。 */

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
  ws.onmessage = (ev) => {
    // 二进制 JPEG —— 必须排在 JSON 解析之前。通道号在**头 1 字节**，
    // 不用再等一条 frame_meta（协议与 vtd / vtl 同款）。
    if (typeof ev.data !== "string") {
      const idx = ev.data.byteLength ? new Uint8Array(ev.data, 0, 1)[0] : 0;
      try {
        window.MultiGrid.paint(GRID_EL, idx, new Uint8Array(ev.data, 1));
      } catch (e) {
        setVdOverlay("帧渲染失败: " + e, true);
      }
      return;
    }
    let msg;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }
    if (msg.type === "event") { addVdEvent(msg); showVdEventBadge(msg); return; }
    if (msg.type === "event_history") {
      // 连接时补一段历史。工厂打包成**一条**发：逐条回放实测 200 条能打满
      // 浏览器一个核（每条都带 crop_b64）。这里整段替换而不是 append ——
      // WS 重连会再发一次，append 就成了重复。
      VDDetector.events = (msg.events || []).slice().reverse();
      renderVdEventList();
      return;
    }
    if (msg.type === "error") {
      setVdOverlay("WS 错误: " + (msg.detail || ""), true);
      return;
    }
    // status: {type, session, detector} —— 会话快照在 msg.session 里
    // （旧单路协议是把它摊平的，所以这里必须解一层）。
    const snap = msg.session;
    if (!snap) return;
    if (msg.detector) updateVdDetectorStatus(msg.detector);
    setVdMeta(snap);
    setVdProgress(snap);
    refreshVdButtons(snap.state);
    updateVdTiles(snap);
    // 浮框 / 曲线靠 status 里的 judge 字段刷新（~1Hz）
    window.FrameJudge?.update("vd", snap.judge);
    document.getElementById("data-video-detection").textContent =
      JSON.stringify(msg, null, 2);
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
  const body = await r.json();
  const snap = body.session || body;     // 工厂返回 {session, detector}
  if (body.detector) updateVdDetectorStatus(body.detector);
  setVdMeta(snap);
  setVdProgress(snap);
  refreshVdButtons(snap.state);
  updateVdTiles(snap);
  const loopEl = document.getElementById("vd-loop");
  if (loopEl && snap.loop != null) loopEl.checked = !!snap.loop;
  window.FrameJudge?.update("vd", snap.judge);
  document.getElementById("data-video-detection").textContent =
    JSON.stringify(body, null, 2);
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
    // **判据在这一帧上的分数** —— 没有它就分不清这条事件是"阈值触发的"
    // 还是"兜底间隔触发的"。后者本来就允许阈值以下的帧送检（兜底条件是
    // gap >= every_n 且 score >= 阈值一半），两种情况的处置完全不同：前者
    // 说明阈值偏低该调高，后者说明兜底在正常工作、只是频率比预期高。
    const jsSpan = window.FrameJudge?.scoreSpan?.("vd", ev);
    if (jsSpan) meta.appendChild(jsSpan);
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
      `运行中 · 每 ${snap.judge?.every_n ?? snap.every_n} 帧 · 队列 ${snap.queue_size}${toolsHint}${sizeHint}`;
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
  // 会话判据在自动模式下已经是唯一的门（见 core/video/frame_judge.py），
  // detector 自己的 every_n 必须放开成 1，否则两级节流相乘（30x30=900
  // 帧才送一帧），卡片上写的"间隔 N 帧"就变成了假话。
  const judgeAuto = window.FrameJudge?.get("vd")?.judge
    && !window.FrameJudge.get("vd").judge.manual;
  const effectiveEvery = judgeAuto ? 1 : every_n;
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
        prompt, every_n: effectiveEvery, enabled_tools,
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

  // 帧判据浮层（角标 + 曲线）锚在**画面容器**上，多路宫格里没有单一 canvas。
  // **apiBase 指向本卡**：判据挂在 MultiChannelSession 上，不是
  // /api/video/judge 操作的那个单例 VideoSession。
  window.FrameJudge?.mount({
    prefix: "vd",
    canvasId: GRID_EL,
    apiBase: "/api/video-detection/judge",
  });

  const sourceEl = document.getElementById("vd-sources");
  const loopEl   = document.getElementById("vd-loop");
  const openBtn = document.getElementById("vd-open");
  const closeBtn = document.getElementById("vd-close");
  const browseBtn = document.getElementById("vd-browse");
  const playBtn = document.getElementById("vd-play");
  const pauseBtn = document.getElementById("vd-pause");
  const stopBtn = document.getElementById("vd-stop");

  // 多行框里 Enter 是换行，不是提交 —— 提交用 Ctrl/⌘+Enter（与 vtd / vtl 一致）。
  sourceEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); openBtn.click(); }
  });

  // 源变了 → 音轨面板的「音频源」标签要跟着变（几路、哪一路）。共享模块
  // 不会自己盯源框（core 不该知道具体控件），由本页在这里通知它刷新。
  sourceEl.addEventListener("input", () => { AudioAnalysis.refreshSource?.(); });

  openBtn.addEventListener("click", async () => {
    const check = validateVdSources();
    if (!check.ok) { setVdOverlay(check.reason, true); return; }
    openBtn.disabled = true;
    setVdOverlay("正在打开 " + check.sources.length + " 路 …", false);
    // 先按路数摆好宫格再发请求：失败时用户至少看到"我填的 4 路被认成 4 路"，
    // 而不是画面还停在上一轮。
    window.MultiGrid.ensure(GRID_EL, check.sources.length);
    try {
      const snap = await vdOpen({ sources: check.sources, loop: !!(loopEl && loopEl.checked) });
      setVdMeta(snap);
      setVdProgress(snap);
      updateVdTiles(snap);
      if (loopEl && snap.loop != null) loopEl.checked = !!snap.loop;
      const meta = document.getElementById("vd-source-meta");
      if (meta) meta.textContent = check.sources.length > 1
        ? `已打开 ${check.sources.length} 路` : "已选: " + check.sources[0];
      setVdOverlay("", false);
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
    window.MultiGrid.reset(GRID_EL);
    const meta = document.getElementById("vd-source-meta");
    if (meta) meta.textContent = "尚未选择";
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
  // 音轨面板由共享模块在 mount() 时自装配（按钮 + WS + 首次快照），
  // 这里不再调 init()。
  if (!VD.ws && !VD.wsTimer) connectVdWs();
}

initVideoDetection();

// ------------------------------------------------------------------ *
// 应用向导模式 API — wizard 框架 finish 时调用
// 契约:opts 字段由 wizard.json 的 steps[].fields[].id 派生(显式 ui_key 决定):
//   vd-sources      → opts.source（**整个多行文本框内容**）
//   vd-loop         → opts.loop
//   vd-prompt       → opts.prompt
//   vd-detect-size  → opts.detectSize
//   vd-audio-prompt → opts.audioPrompt
// 空字段不触发对应启动动作;失败抛 Error("WizardSnapshotError:<code>:<detail>")
//   * open_source_failed    → wizard 回 source 步骤
//   * detector_start_failed → wizard 弹"启动失败"提示
//   * audio_start_failed    → wizard 弹"启动失败"提示
// 全局名固定为 ImApp_<app_id 去掉 hyphen>—— wizard 按这个约定查找。
// ------------------------------------------------------------------ *
window.ImApp_video_detection = window.ImApp_video_detection || {};
window.ImApp_video_detection.applyWizardSnapshot = async function (opts) {
  const setVal = (id, v) => { const el = document.getElementById(id); if (el != null) el.value = v; };
  const setChk = (id, v) => { const el = document.getElementById(id); if (el != null) el.checked = !!v; };

  // 1. 写回非空字段到表单(留给用户回看 + 应用自有 init 也用得到)
  if (opts.source)      setVal("vd-sources", opts.source);
  if (opts.loop != null) setChk("vd-loop", opts.loop);
  if (opts.prompt)      setVal("vd-prompt", opts.prompt);
  if (opts.detectSize)  setVal("vd-detect-size", opts.detectSize);
  if (opts.audioPrompt) setVal("vd-audio-prompt", opts.audioPrompt);
  // 帧判据（算法 / 阈值 / 帧间隔）—— 必须在 open 之前落地：open 一开视频，
  // 判据就已经在解码热路径上跑了。
  const judge = await window.FrameJudge?.fromWizard("vd", opts) ?? null;

  const result = { source_opened: false, detector_started: false, audio_started: false,
                 judge_applied: !!judge };

  // 2. 打开视频源(仅 source 非空)。opts.source 是**整个多行文本框**，必须
  //    先按行拆成 sources[] 再发（见 readVdSources 的注释）。
  if (opts.source) {
    const check = validateVdSources();
    if (!check.ok) {
      throw new Error("WizardSnapshotError:open_source_failed:" + check.reason);
    }
    window.MultiGrid.ensure(GRID_EL, check.sources.length);
    try {
      await vdOpen({ sources: check.sources, loop: !!opts.loop });
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

/* v0.23 多路判据 UI 接线 */
function _paintSelectedChannelUi(pfx, idx, per_channel) {
  const idxEl = document.getElementById(pfx + "-judge-selected-idx");
  const noEl = document.getElementById(pfx + "-judge-selected-no");
  const modeEl = document.getElementById(pfx + "-judge-selected-mode");
  const thrEl = document.getElementById(pfx + "-judge-selected-thr");
  const everyEl = document.getElementById(pfx + "-judge-selected-every-n");
  if (!idxEl) return;
  if (idx == null) {
    idxEl.textContent = "--";
    if (noEl) noEl.textContent = "--";
    if (modeEl) modeEl.textContent = "--";
    if (thrEl) thrEl.textContent = "--";
    if (everyEl) everyEl.textContent = "--";
    return;
  }
  // [x, y] 坐标写法：x = 0 基 idx（程序员语境），y = 1 基通道号（用户语境）。
  // 把 x 加粗是因为"哪个 channel"是个常被复述的索引。
  idxEl.textContent = "[" + idx + ", " + (idx + 1) + "]";
  if (noEl) noEl.textContent = String(idx + 1);
  const per = (per_channel || []).find(p => p.channel === idx);
  if (per) {
    if (modeEl) modeEl.textContent = per.mode || "--";
    if (thrEl) thrEl.textContent = per.threshold != null ? (+per.threshold).toFixed(2) : "--";
    if (everyEl) everyEl.textContent = per.every_n != null ? "每 " + per.every_n + " 帧" : "--";
  } else {
    if (modeEl) modeEl.textContent = "--";
    if (thrEl) thrEl.textContent = "--";
    if (everyEl) everyEl.textContent = "--";
  }
}

/**
 * 取当前卡片对应的 judge API base。vd / vtd / vtl 各自一条 endpoint。
 * 卡片 side 跟后端路由前缀对齐——见 app/__init__.py 的 include_router。
 */
function _judgeApiFor(pfx) {
  if (pfx === "vd") return "/api/video-detection";
  if (pfx === "vtd") return "/api/video-target-detection";
  if (pfx === "vtl") return "/api/video-target-localization";
  return null;
}

function _bindCardPerChannelJudge(pfx, gridId) {
  const MG = window.MultiGrid;
  if (!MG) return;
  const apiBase = _judgeApiFor(pfx);
  MG.bindSelect(gridId, async (idx) => {
    // 必须在 setSelectedChannel 之前：它同步写 .selected，之后再判二次点击恒真
    window.ZoomFrame?.onTileClick(gridId, idx);
    if (window.FrameJudge) window.FrameJudge.setSelectedChannel(pfx, idx);
    // **不要等下一条 WS tick**：用户点了 tile 就想立刻看见这路当前的
    // mode / threshold / every_n —— 否则"我改的是哪路"的反馈要拖 1 秒，
    // 体感是判据栏"反应迟钝"。直接 GET /judge 拿最新 per_channel 填 UI。
    if (idx == null || !apiBase) {
      _paintSelectedChannelUi(pfx, idx, null);
      return;
    }
    let per_channel = null;
    try {
      const j = await fetch(apiBase + "/judge", { cache: "no-store" })
                    .then(r => r.json());
      per_channel = j.per_channel || [];
      _paintSelectedChannelUi(pfx, idx, per_channel);
    } catch (_) {
      _paintSelectedChannelUi(pfx, idx, null);
    }
    // 立刻把选中路的当前值推到 selMode/inpThr/inpEvery —— 等 WS tick
    // 要 ~1 秒，且若 _awaitSync 门闩或 activeElement 焦点命中 update()
    // 路径会**根本不推**。tile click 是"我要切这路"最强的信号，不能让
    // 控件卡住。setConfig 只写控件显示、不发 POST，所以"看一眼不动"的
    // 查看意图不会被误当成"修改提交"。
    if (per_channel && window.FrameJudge) {
      const per = per_channel.find((p) => p.channel === idx);
      if (per && per.mode != null) {
        window.FrameJudge.setConfig(pfx, {
          mode: per.mode, every_n: per.every_n, threshold: per.threshold,
        });
        // **同步写到 wizard 内的同名字段**。FrameJudge.setConfig 只动功能页
        // 那组 (selMode/inpThr/inpEvery)，但 wizard source step 里也有
        // ``id="<pfx>-judge-mode/-judge-threshold/-judge-every-n"`` 的
        // 独立 <input>/<select>（wizard state.js 渲染）—— 用户在向导里点
        // tile 看不到任何变化就是因为这组是孤儿。仅在 wizard 容器存在时
        // 同步写一份 DOM value；不动 wizard 的 state.values（避免覆盖
        // 用户手填的字段值，wizard 「完成」会按 state.values 推后端）。
        const wizRoot = document.getElementById("wd-wizard-root-" +
          (pfx === "vd" ? "video-detection"
           : pfx === "vtd" ? "video-target-detection"
           : "video-target-localization"));
        if (wizRoot) {
          // wizard state.js 创建字段元素时只设 class 不设 id，定位靠
          // el.dataset.fieldId（state.js 末尾赋的，匹配 wizard.json 的
          // field.id）。下面三个 query 跨 step 通用：source step 把这些
          // 字段排在「视频源」下、detect / audio / … step 也可能用到。
          const wizMode = wizRoot.querySelector(
            "select[data-field-id=\"" + pfx + "-judge-mode\"]");
          const wizThr  = wizRoot.querySelector(
            "input[data-field-id=\"" + pfx + "-judge-threshold\"]");
          const wizEv   = wizRoot.querySelector(
            "input[data-field-id=\"" + pfx + "-judge-every-n\"]");
          if (wizMode && per.mode != null) {
            if (![...wizMode.options].some((o) => o.value === per.mode)) {
              const o = document.createElement("option");
              o.value = per.mode; o.textContent = per.mode;
              wizMode.appendChild(o);
            }
            wizMode.value = per.mode;
          }
          if (wizThr && per.threshold != null) wizThr.value = String(per.threshold);
          if (wizEv  && per.every_n  != null) wizEv.value  = String(per.every_n);
        }
      }
    }
  });
  _paintSelectedChannelUi(pfx, null, null);
}
_bindCardPerChannelJudge("vd", "vd-grid");

