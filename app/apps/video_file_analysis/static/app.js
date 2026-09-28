(function () {
  "use strict";
  const API_PREFIX = "/api";
  // core helpers (fmtNum, fmtEventTime) via app/static/core/*.js
  const fmtNum = window.fmtNum;
  const fmtEventTime = window.fmtEventTime;


const VFA = {
  ws: null,
  wsRetry: 0,
  wsTimer: null,
  state: "idle",
  events: [],
  summary: "",
  // ...
  meta: { total_frames: 0, every_n: 30, started_at: 0, finished_at: 0, fps: 0 },
  init() { return initVideoFileAnalysis(); },
};

function vfaSetStatus(text, isError) {
  const el = document.getElementById("vfa-status");
  if (!el) return;
  el.textContent = text || "";
  el.style.color = isError ? "#b91c1c" : "";
}

function vfaSetRuntime(text) {
  const el = document.getElementById("vfa-runtime");
  if (!el) return;
  el.textContent = text || "未启动";
}

function vfaSetSourceMeta(text) {
  const el = document.getElementById("vfa-source-meta");
  if (!el) return;
  el.textContent = text || "尚未选择";
}

function vfaShowError(text) {
  const banner = document.getElementById("vfa-error-banner");
  const textEl = document.getElementById("vfa-error-text");
  if (!banner || !textEl) return;
  textEl.textContent = text || "";
  banner.classList.toggle("hidden", !text);
}

function vfaRenderSnapshot(snap) {
  const pre = document.getElementById("data-video-file-analysis");
  if (pre) pre.textContent = JSON.stringify(snap, null, 2);
}

function vfaFormatRuntime(snap) {
  if (!snap) return "未启动";
  switch (snap.state) {
    case "idle":     return "未启动";
    case "opening":  return "正在打开…";
    case "running": {
  // ...
      const total = snap.total_frames > 0
        ? snap.frame_pos + "/" + snap.total_frames
        : "帧 " + snap.frame_pos;
      const sampled  = Number(snap.sampled)  || 0;
      const analyzed = Number(snap.analyzed) || 0;
      const inflight = Math.max(0, sampled - analyzed);
      const tail = inflight > 0
        ? ` · 已分析 ${analyzed}/${sampled}（LLM 队列 ${inflight}）`
        : ` · 已分析 ${analyzed}/${sampled}`;
      return "分析中 · 已读 " + total + tail;
    }
    case "summarizing": {
  // ...
      const sp = snap.summary_progress || [0, 0];
      if (sp[1] > 0) {
        return "生成综述中 · chunk " + sp[0] + "/" + sp[1]
          + " · 共分析 " + snap.analyzed + " 帧";
      }
      return "生成综述中 · 共分析 " + snap.analyzed + " 帧";
    }
    case "done":     return "已完成 · 共分析 " + snap.analyzed + " 帧";
    case "stopped":  return "已停止 · 已分析 " + snap.analyzed + " 帧";
    case "error":    return "错误: " + (snap.error || "未知");
    default:         return snap.state || "未启动";
  }
}

function vfaRefreshButtons(snap) {
  const startBtn = document.getElementById("vfa-start");
  const stopBtn  = document.getElementById("vfa-stop");
  if (!startBtn || !stopBtn) return;
  const active = snap && (snap.state === "running" || snap.state === "opening");
  startBtn.disabled = !!active;
  stopBtn.disabled = !active;
}

function vfaAppendEvent(ev) {
  // ...
  VFA.events.push(ev);
  if (VFA.events.length > 5000) VFA.events.splice(0, VFA.events.length - 5000);
  vfaRenderEventList();
  // ...
  vfaUpdateProgress();
}

  // ...
function vfaSyncEventsCollapse(state) {
  const details = document.getElementById("vfa-events-details");
  if (!details) return;
  const prev = VFA._lastCollapseState;
  if (prev === state) return;          // 同一状态重复推送：不动
  VFA._lastCollapseState = state;
  // ...
  if (prev === undefined) return;
  const shouldOpen = state === "running" ||
                    state === "opening" ||
                    state === "summarizing";
  if (shouldOpen) {
    if (!details.open) details.open = true;
  } else {
    if (details.open) details.open = false;
  }
}

function vfaRenderEventList() {
  const list = document.getElementById("vfa-event-list");
  const count = document.getElementById("vfa-events-count");
  if (!list || !count) return;
  count.textContent = VFA.events.length + " 条";
  if (VFA.events.length === 0) {
    list.innerHTML = '<li class="vfa-event-empty">尚未开始分析。</li>';
    return;
  }
  list.innerHTML = "";
  // ...
  for (let i = VFA.events.length - 1; i >= 0; i--) {
    const ev = VFA.events[i];
    const li = document.createElement("li");
    li.className = "vfa-event" + (i === VFA.events.length - 1 ? " latest" : "");
    const head = document.createElement("div");
    head.className = "vfa-event-head";
    const fp = document.createElement("span");
    fp.className = "vfa-event-frame";
    fp.textContent = "帧 " + ev.frame_pos;
    const ts = document.createElement("span");
    ts.className = "vfa-event-ts";
    ts.textContent = fmtEventTime(ev.ts);
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = "#" + ev.id;
    head.appendChild(fp);
    head.appendChild(ts);
    head.appendChild(tag);
    const text = document.createElement("div");
    text.className = "vfa-event-text";
    text.textContent = ev.text || "(空)";
    li.appendChild(head);
    li.appendChild(text);
    list.appendChild(li);
  }
}

function vfaSetSummary(text) {
  const emptyEl = document.getElementById("vfa-summary-empty");
  const textEl  = document.getElementById("vfa-summary-text");
  const metaEl  = document.getElementById("vfa-summary-meta");
  VFA.summary = text || "";
  if (text) {
    if (emptyEl) emptyEl.classList.add("hidden");
    if (textEl) {
      textEl.textContent = text;
      textEl.classList.remove("hidden");
    }
    if (metaEl) metaEl.textContent = "已完成 · " + new Date().toLocaleTimeString();
  } else {
    if (emptyEl) emptyEl.classList.remove("hidden");
    if (textEl) {
      textEl.textContent = "";
      textEl.classList.add("hidden");
    }
    if (metaEl) metaEl.textContent = "尚无综述";
  }
}

function vfaApplySnapshot(snap) {
  if (!snap) return;
  VFA.state = snap.state;
  // ...
  VFA.meta.total_frames = Number(snap.total_frames) || 0;
  VFA.meta.every_n      = Number(snap.every_n) || 30;
  VFA.meta.started_at   = Number(snap.started_at) || 0;
  VFA.meta.finished_at  = Number(snap.finished_at) || 0;
  VFA.meta.fps          = Number(snap.fps) || 0;
  vfaSetRuntime(vfaFormatRuntime(snap));
  vfaRefreshButtons(snap);
  vfaRenderSnapshot(snap);
  vfaUpdateProgress(snap);
  // ...
  vfaSyncEventsCollapse(snap.state);
  if (snap.state === "error" && snap.error) vfaShowError(snap.error);
  if (snap.state === "running" || snap.state === "opening") vfaShowError("");
  // ...
  if (typeof DemoGuard !== "undefined") {
    DemoGuard.report("video-file-analysis", { session: snap });
  }
}

const _VFA_PHASE_LABEL = {
  idle:        "未启动",
  opening:     "正在打开视频源…",
  running:     "分析中",
  summarizing: "生成综述中",
  done:        "已完成",
  stopped:     "已停止",
  error:       "错误",
};

/**
 * 推进进度条。无 snap 时只更新数字（用于 event 推送触发的即时刷新）；
 * 有 snap 时同时更新 phase / meta / 状态色。
 *
 * 算法：
 *   total_frames = VFA.meta.total_frames（来自最近一次 state 推送）
 *   expected     = ⌊ total_frames / every_n ⌋  ← 后端采样数
 *                 后端 _decode_loop 只在 cur_pos % every_n == 0 且
 *                 cur_pos <= total_frames 时采样（位置 every_n, 2*every_n,
 *                 ..., floor(total_frames/every_n)*every_n），所以样本数
 *                 是 floor 而不是 ceil。之前用 ceil 导致 totalFrames 不是
 *                 every_n 整数倍时进度条永远停在 (N-1)/N。
 *   analyzed     = 优先用 ``snap.analyzed``（state API 的权威计数），
 *                 没有 snap 时退到 ``VFA.events.length``。
 *                 **不要直接用 events.length**：events API 与 state API 是
 *                 两次请求，在 done 状态切换瞬间 events 端可能延迟一拍，
 *                 导致用户多次刷新看到 ratio 在 70%→86%→100% 之间
 *                 跳变（bar 一会儿不满格一会儿满格）。用 snap.analyzed
 *                 与 state 同源，永远不会出现这种 flicker。
 *   progress     = analyzed / expected（done 状态下强制 ratio=1）
 */
function vfaUpdateProgress(snap) {
  const root      = document.getElementById("vfa-progress");
  const phaseEl   = document.getElementById("vfa-progress-phase");
  const metaEl    = document.getElementById("vfa-progress-meta");
  const trackEl   = document.getElementById("vfa-progress-track");
  const fillEl    = document.getElementById("vfa-progress-analyze");
  const nowEl     = document.getElementById("vfa-progress-count-now");
  if (!root || !trackEl || !fillEl || !nowEl) return;

  const state = (snap?.state) || VFA.state || "idle";

  // ...
  const summaryProgress = snap && Array.isArray(snap.summary_progress)
    ? snap.summary_progress.join(",")
    : (VFA.meta.summaryProgress || "0,0");
  if (snap && !VFA._lastSummaryProgress) VFA._lastSummaryProgress = "0,0";
  const sig = [
    state,
    VFA.meta.total_frames,
    VFA.meta.every_n,
    VFA.events.length,
    VFA.meta.fps,
    VFA.meta.started_at,
    VFA.meta.finished_at,
    summaryProgress,
  ].join("|");
  if (snap && sig === VFA._progressSig) return;
  VFA._progressSig = sig;
  if (snap) VFA._lastSummaryProgress = summaryProgress;

  root.dataset.state = state;
  if (phaseEl) phaseEl.textContent = _VFA_PHASE_LABEL[state] || state;

  // ...
  if (metaEl && snap) {
    const metaParts = [];
    if (VFA.meta.fps > 0) metaParts.push(fmtNum(VFA.meta.fps, 1) + " fps");
    if (VFA.meta.started_at > 0 &&
        (state === "running" || state === "opening" ||
         state === "summarizing" ||
         state === "done" || state === "stopped" || state === "error")) {
      const end = VFA.meta.finished_at > 0
        ? VFA.meta.finished_at
        : Date.now() / 1000;
      const elapsed = end - VFA.meta.started_at;
      if (elapsed > 0) metaParts.push("已耗时 " + Math.round(elapsed) + "s");
    }
    metaEl.textContent = metaParts.join(" · ") || "--";
  }

  // ...
  const totalFrames = VFA.meta.total_frames;
  const everyN      = VFA.meta.every_n;
  // ...
  const analyzed    = (snap && Number.isFinite(snap.analyzed))
    ? Number(snap.analyzed)
    : VFA.events.length;

  let expected = 0;
  let ratio = null;
  // ...
  const showPercent = state === "running" || state === "opening" ||
                      state === "summarizing" || state === "done";
  if (!showPercent) {
    expected = 0;
    ratio = null;
  } else if (state === "summarizing" && snap && Array.isArray(snap.summary_progress)
      && snap.summary_progress[1] > 0) {
    expected = snap.summary_progress[1];
    ratio = Math.min(1, snap.summary_progress[0] / expected);
  } else if (state === "done") {
  // ...
    expected = analyzed;
    ratio = 1;
  } else if (totalFrames > 0 && everyN > 0) {
    expected = Math.max(1, Math.floor(totalFrames / everyN));
    ratio = Math.min(1, analyzed / expected);
  }

  let percentText;
  if (!showPercent) {
    percentText = "0%";
  } else if (ratio !== null) {
    percentText = Math.round(ratio * 100) + "%";
  } else {
    percentText = "--%";
  }
  nowEl.textContent = percentText;

  if (ratio === null) {
  // ...
    if (state === "running") {
      fillEl.classList.add("indeterminate");
      trackEl.style.setProperty("--p", "100%");
    } else {
      fillEl.classList.remove("indeterminate");
      trackEl.style.setProperty("--p", "0%");
    }
  } else {
    fillEl.classList.remove("indeterminate");
    trackEl.style.setProperty("--p", (ratio * 100).toFixed(2) + "%");
  }
}

  // ...
const vfaApplyProgress = vfaUpdateProgress;

async function vfaFetchSnapshot() {
  try {
    const r = await fetch(API_PREFIX + "/video-file-analysis", { cache: "no-store" });
    if (!r.ok) return;
    vfaApplySnapshot(await r.json());
  } catch (_) { /* ignore */ }
}

async function vfaFetchEvents() {
  try {
    const r = await fetch(API_PREFIX + "/video-file-analysis/events", { cache: "no-store" });
    if (!r.ok) return;
    const body = await r.json();
    VFA.events = (body.events || []).slice().sort((a, b) => a.id - b.id);
    vfaRenderEventList();
    vfaSetSummary(body.summary?.summary || "");
    if (body.snapshot) vfaApplySnapshot(body.snapshot);
  } catch (_) { /* ignore */ }
}

function connectVfaWs() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}${API_PREFIX}/video-file-analysis/ws`;
  const ws = new WebSocket(url);
  VFA.ws = ws;

  ws.onopen = () => {
    VFA.wsRetry = 0;
    vfaShowError("");
  };
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }
    if (msg.type === "event" && msg.event) {
      vfaAppendEvent(msg.event);
    } else if (msg.type === "summary") {
      vfaSetSummary(msg.summary || "");
    } else if (msg.type === "state" || msg.type === "hello") {
      vfaApplySnapshot(msg);
  // ...
      if (msg.state && ["done", "stopped", "error"].includes(msg.state)) {
        vfaFetchHistory();
      }
    } else if (msg.type === "error") {
      vfaShowError(msg.detail || "WS 错误");
    }
  };
  ws.onclose = () => {
    VFA.ws = null;
    const wait = Math.min(5000, 500 * Math.pow(2, VFA.wsRetry++));
    VFA.wsTimer = setTimeout(connectVfaWs, wait);
  };
  ws.onerror = () => {
    try { ws.close(); } catch (_) {}
  };
}

async function vfaStart() {
  const sourceEl = document.getElementById("vfa-source");
  const promptEl = document.getElementById("vfa-prompt");
  const everyNEl = document.getElementById("vfa-every-n");
  const source = (sourceEl?.value || "").trim();
  const prompt = (promptEl?.value || "").trim();
  const everyN = Number(everyNEl?.value);
  if (!source)  { vfaShowError("请先选择视频源"); return; }
  if (!prompt)  { vfaShowError("请填写分析要求"); return; }
  if (!Number.isInteger(everyN) || everyN < 1) {
    vfaShowError("采样间隔必须为正整数"); return;
  }
  vfaShowError("");
  VFA.events = [];
  vfaRenderEventList();
  vfaSetSummary("");
  try {
    const r = await fetch(API_PREFIX + "/video-file-analysis/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source, prompt, every_n: everyN }),
    });
    if (!r.ok) {
      const detail = await r.text().catch(() => "");
      vfaShowError("启动失败: HTTP " + r.status + " " + detail);
      return;
    }
    vfaApplySnapshot(await r.json());
  } catch (e) {
    vfaShowError("启动失败: " + e.message);
  }
}

async function vfaStop() {
  try {
    const r = await fetch(API_PREFIX + "/video-file-analysis/stop", { method: "POST" });
    if (r.ok) vfaApplySnapshot(await r.json());
  } catch (e) {
    vfaShowError("停止失败: " + e.message);
  }
}

async function vfaClear() {
  try {
    await fetch(API_PREFIX + "/video-file-analysis/events/clear", { method: "POST" });
    VFA.events = [];
    vfaSetSummary("");
    vfaRenderEventList();
  } catch (_) { /* ignore */ }
}

  // ...
function vfaFmtTs(ts) {
  if (!ts) return "--";
  try {
    const d = new Date(ts * 1000);
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  } catch (_) { return "--"; }
}

function vfaRenderHistory(runs) {
  const list = document.getElementById("vfa-history-list");
  const count = document.getElementById("vfa-history-count");
  if (!list || !count) return;
  count.textContent = (runs?.length || 0) + " 条";
  if (!runs || runs.length === 0) {
    list.innerHTML = '<li class="vfa-history-empty">暂无历史分析记录。每次分析完成或中途停止后会自动落盘。</li>';
    return;
  }
  list.innerHTML = "";
  runs.forEach((run) => {
    const li = document.createElement("li");
    li.className = "vfa-history-row";
    li.dataset.runId = run.run_id;

    const head = document.createElement("div");
    head.className = "vfa-history-head";
    const fp = document.createElement("span");
    fp.className = "vfa-history-source";
    fp.textContent = run.source || "(无源)";
    fp.title = run.source || "";
    const st = document.createElement("span");
    st.className = "vfa-history-state vfa-state-" + run.state;
    st.textContent = run.state;
    head.appendChild(fp);
    head.appendChild(st);

    const meta = document.createElement("div");
    meta.className = "vfa-history-meta";
    const ev = document.createElement("span");
    ev.textContent = run.events_count + " 帧";
    const ts = document.createElement("span");
    ts.textContent = vfaFmtTs(run.finished_at || run.started_at);
    const sum = document.createElement("span");
    sum.textContent = run.has_summary ? "有综述" : "无综述";
    meta.appendChild(ev);
    meta.appendChild(ts);
    meta.appendChild(sum);

    const actions = document.createElement("div");
    actions.className = "vfa-history-actions";
  // ...
    const loadBtn = document.createElement("button");
    loadBtn.type = "button";
    loadBtn.className = "vda-action-btn vda-action-btn-primary";
    loadBtn.innerHTML = '<span class="vda-action-btn-icon">📂</span>加载';
    loadBtn.title = "加载此 run 到内存（替换当前活跃会话）";
    loadBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      vfaLoadRun(run.run_id);
    });
    const queryBtn = document.createElement("button");
    queryBtn.type = "button";
    queryBtn.className = "vda-action-btn vda-action-btn-info";
    queryBtn.innerHTML = '<span class="vda-action-btn-icon">🔍</span>反查';
    queryBtn.title = "按场景 / 人物 / 物体反查事件";
    queryBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      vfaQueryOpen(run.run_id, run.source, run.total_frames, run.fps);
    });
    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "vda-action-btn vda-action-btn-danger";
    delBtn.innerHTML = '<span class="vda-action-btn-icon">🗑</span>删除';
    delBtn.title = "从磁盘删除此 run 的 JSON（不可恢复）";
    delBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      vfaDeleteRun(run.run_id);
    });
    actions.appendChild(loadBtn);
    actions.appendChild(queryBtn);
    actions.appendChild(delBtn);

    li.appendChild(head);
    li.appendChild(meta);
    li.appendChild(actions);
    li.addEventListener("click", () => vfaLoadRun(run.run_id));
    list.appendChild(li);
  });
}

async function vfaFetchHistory() {
  try {
    const r = await fetch(API_PREFIX + "/video-file-analysis/runs", { cache: "no-store" });
    if (!r.ok) return;
    const body = await r.json();
    vfaRenderHistory(body.runs || []);
  } catch (_) { /* ignore */ }
}

async function vfaLoadRun(runId) {
  if (!confirm("加载历史分析会覆盖当前界面显示，确定吗？")) return;
  try {
    const r = await fetch(
      API_PREFIX + "/video-file-analysis/runs/" + encodeURIComponent(runId),
      { cache: "no-store" }
    );
    if (!r.ok) {
      vfaShowError("加载失败: HTTP " + r.status);
      return;
    }
    const body = await r.json();
    VFA.events = (body.events || []).slice().sort((a, b) => a.id - b.id);
    vfaRenderEventList();
    vfaSetSummary(body.summary?.summary || "");
    vfaApplySnapshot(body.snapshot || {});
    vfaShowError("");
  } catch (e) {
    vfaShowError("加载失败: " + e.message);
  }
}

async function vfaDeleteRun(runId) {
  if (!confirm("确定删除这条历史分析记录吗？")) return;
  try {
    const r = await fetch(
      API_PREFIX + "/video-file-analysis/runs/" + encodeURIComponent(runId),
      { method: "DELETE" }
    );
    if (!r.ok) {
      vfaShowError("删除失败: HTTP " + r.status);
      return;
    }
    await vfaFetchHistory();
  } catch (e) {
    vfaShowError("删除失败: " + e.message);
  }
}

  // ...
const VFAQuery = {
  runId: null,
  facets: { scenes: [], actors: [], objects: [] },
  selected: { actors: new Set(), objects: new Set(), actions: new Set() },
};

  // ...
const _TIME_HHMMSS_RE = /^(\d{1,2}):([0-5]?\d):([0-5]?\d)$/;
function _parseTimeToSeconds(str) {
  if (str == null) return null;
  str = String(str).trim();
  if (!str) return null;
  const m = str.match(_TIME_HHMMSS_RE);
  if (!m) return null;
  const h = parseInt(m[1], 10);
  const min = parseInt(m[2], 10);
  const s = parseInt(m[3], 10);
  // ...
  if (min > 59 || s > 59) return null;
  if (h < 0) return null;
  return h * 3600 + min * 60 + s;
}

  // ...
function _formatTimeHHMMSS(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "00:00:00";
  const total = Math.floor(seconds);
  const hh = Math.floor(total / 3600);
  const mm = Math.floor((total % 3600) / 60);
  const ss = total % 60;
  const p = (n) => String(n).padStart(2, "0");
  return `${p(hh)}:${p(mm)}:${p(ss)}`;
}

  // ...
function _validateTimeInput(el) {
  if (!el) return true;
  const raw = el.value.trim();
  // ...
  if (!raw) {
    el.classList.remove("vfa-query-input-invalid");
    el.removeAttribute("title");
    return true;
  }
  // ...
  if (_TIME_HHMMSS_RE.test(raw)) {
    el.classList.remove("vfa-query-input-invalid");
    el.removeAttribute("title");
    return true;
  }
  el.classList.add("vfa-query-input-invalid");
  el.title = "格式错误：请用 HH:MM:SS（例如 00:01:30）";
  return false;
}

  // ...
function _secondsToFrame(seconds, fps) {
  if (!Number.isFinite(seconds) || !Number.isFinite(fps) || fps <= 0) return null;
  if (seconds < 0) return null;
  return Math.floor(seconds * fps);
}

function vfaQueryOpen(runId, sourceLabel, totalFrames, fps) {
  VFAQuery.runId = runId;
  // ...
  VFAQuery.fps = fps;
  VFAQuery.totalFrames = totalFrames;
  VFAQuery.selected = { actors: new Set(), objects: new Set(), actions: new Set() };
  document.getElementById("vfa-query-source").textContent = sourceLabel || runId;
  document.getElementById("vfa-query").classList.remove("hidden");
  // ...
  const hintEl = document.getElementById("vfa-query-range-hint");
  if (hintEl) {
    if (totalFrames > 0 && fps > 0) {
      const totalSec = totalFrames / fps;
  // ...
      const tsHHMMSS = _formatTimeHHMMSS(totalSec);
      hintEl.textContent = `（总时长 ${tsHHMMSS}）`;
      const minEl = document.getElementById("vfa-query-time-min");
      const maxEl = document.getElementById("vfa-query-time-max");
      if (minEl) minEl.placeholder = `起始 (00:00:00)`;
      if (maxEl) maxEl.placeholder = `结束 (${tsHHMMSS})`;
    } else {
      hintEl.textContent = "（总时长未知）";
      const minEl = document.getElementById("vfa-query-time-min");
      const maxEl = document.getElementById("vfa-query-time-max");
      if (minEl) minEl.placeholder = "起始 (00:00:00)";
      if (maxEl) maxEl.placeholder = "结束 (HH:MM:SS)";
    }
  }
  // ...
  document.getElementById("vfa-query-scene").innerHTML = '<option value="">不限</option>';
  document.getElementById("vfa-query-actors").innerHTML = '<span class="vfa-query-empty">（加载中…）</span>';
  document.getElementById("vfa-query-objects").innerHTML = '<span class="vfa-query-empty">（加载中…）</span>';
  document.getElementById("vfa-query-actions").innerHTML = '<span class="vfa-query-empty">（加载中…）</span>';
  document.getElementById("vfa-query-time-min").value = "";
  document.getElementById("vfa-query-time-max").value = "";
  document.getElementById("vfa-query-exact").value = "";
  document.getElementById("vfa-query-q").value = "";
  document.getElementById("vfa-query-list").innerHTML = "";
  document.getElementById("vfa-query-summary").textContent = "加载候选中…";
  vfaQueryLoadFacets();
}

function vfaQueryClose() {
  document.getElementById("vfa-query").classList.add("hidden");
  VFAQuery.runId = null;
}

  // ...
let _vfaFrameBlobUrl = null;
let _vfaFrameAbort = null;

function vfaFrameModalClose() {
  const modal = document.getElementById("vfa-frame-modal");
  if (modal) modal.classList.add("hidden");
  // ...
  if (_vfaFrameBlobUrl) {
    URL.revokeObjectURL(_vfaFrameBlobUrl);
    _vfaFrameBlobUrl = null;
  }
  if (_vfaFrameAbort) {
    try { _vfaFrameAbort.abort(); } catch (_) {}
    _vfaFrameAbort = null;
  }
}

function _setFrameModalLoading(text) {
  const loading = document.getElementById("vfa-frame-modal-loading");
  const img = document.getElementById("vfa-frame-modal-img");
  const err = document.getElementById("vfa-frame-modal-error");
  if (loading) {
    loading.classList.remove("hidden");
    loading.textContent = text || "从原始视频取帧中…";
  }
  if (img) img.classList.add("hidden");
  if (err) err.classList.add("hidden");
}

function _setFrameModalError(text) {
  const loading = document.getElementById("vfa-frame-modal-loading");
  const img = document.getElementById("vfa-frame-modal-img");
  const err = document.getElementById("vfa-frame-modal-error");
  if (loading) loading.classList.add("hidden");
  if (img) img.classList.add("hidden");
  if (err) {
    err.classList.remove("hidden");
    err.textContent = text || "取帧失败";
  }
}

function _setFrameModalImage(blobUrl) {
  const loading = document.getElementById("vfa-frame-modal-loading");
  const img = document.getElementById("vfa-frame-modal-img");
  const err = document.getElementById("vfa-frame-modal-error");
  if (loading) loading.classList.add("hidden");
  if (err) err.classList.add("hidden");
  if (img) {
    img.src = blobUrl;
    img.classList.remove("hidden");
  }
}

function _formatStructured(structured) {
  if (!structured || typeof structured !== "object") return "(无)";
  const parts = [];
  if (structured.scene)   parts.push("场景：" + structured.scene);
  if (structured.action)  parts.push("动作：" + structured.action);
  if (structured.actors && structured.actors.length)
    parts.push("人物：" + structured.actors.join("、"));
  if (structured.objects && structured.objects.length)
    parts.push("物体：" + structured.objects.join("、"));
  if (structured.note)    parts.push("备注：" + structured.note);
  return parts.length ? parts.join("\n") : "(无)";
}

  // ...
function _formatVideoTimestamp(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "--";
  const totalMs = Math.round(Math.max(0, seconds) * 1000);
  const hh = Math.floor(totalMs / 3_600_000);
  const mm = Math.floor((totalMs % 3_600_000) / 60_000);
  const ss = Math.floor((totalMs % 60_000) / 1000);
  const ms = totalMs % 1000;
  const p = (n, w = 2) => String(n).padStart(w, "0");
  return `${p(hh)}:${p(mm)}:${p(ss)}.${p(ms, 3)}`;
}

async function openFrameModal(li) {
  const runId = VFAQuery.runId;
  if (!runId) return;
  const framePos = parseInt(li.dataset.framePos || "0", 10);
  if (framePos < 0 || Number.isNaN(framePos)) {
    return;
  }
  const text = li.dataset.text || "(空)";
  let structured = {};
  try {
    structured = JSON.parse(li.dataset.structured || "{}");
  } catch (_) {
    structured = {};
  }

  // ...
  const modal = document.getElementById("vfa-frame-modal");
  if (!modal) return;
  modal.classList.remove("hidden");
  document.getElementById("vfa-frame-modal-meta").textContent =
    "帧 " + framePos;
  document.getElementById("vfa-frame-modal-frame-pos").textContent =
    String(framePos);
  // ...
  const tsEl0 = document.getElementById("vfa-frame-modal-timestamp");
  if (tsEl0) tsEl0.textContent = "--";
  document.getElementById("vfa-frame-modal-source").textContent =
    "(取帧中…)";
  document.getElementById("vfa-frame-modal-text").textContent = text;
  document.getElementById("vfa-frame-modal-structured").textContent =
    _formatStructured(structured);
  _setFrameModalLoading("从原始视频取帧中…（按 every_n 抽样，frame_pos 可能较深）");

  // ...
  if (_vfaFrameBlobUrl) {
    URL.revokeObjectURL(_vfaFrameBlobUrl);
    _vfaFrameBlobUrl = null;
  }
  if (_vfaFrameAbort) {
    try { _vfaFrameAbort.abort(); } catch (_) {}
  }
  _vfaFrameAbort = new AbortController();

  try {
    const url = API_PREFIX + "/video-file-analysis/runs/" +
      encodeURIComponent(runId) + "/frame/" + framePos;
    const r = await fetch(url, {
      cache: "no-store",
      signal: _vfaFrameAbort.signal,
    });
    if (!r.ok) {
  // ...
      let detail = "HTTP " + r.status;
      try {
        const body = await r.json();
        if (body && body.detail) detail = body.detail;
      } catch (_) {}
      _setFrameModalError(detail);
      return;
    }
    const blob = await r.blob();
    _vfaFrameBlobUrl = URL.createObjectURL(blob);
    _setFrameModalImage(_vfaFrameBlobUrl);
  // ...
    const serverPos = r.headers.get("X-Frame-Pos");
    if (serverPos) {
      document.getElementById("vfa-frame-modal-frame-pos").textContent =
        serverPos;
    }
  // ...
    const fpsStr = r.headers.get("X-Fps") || "0";
    const fps = parseFloat(fpsStr);
    const posForTs = serverPos ? parseInt(serverPos, 10) : framePos;
    const tsEl = document.getElementById("vfa-frame-modal-timestamp");
    if (tsEl) {
      if (fps > 0 && posForTs >= 0 && Number.isFinite(fps)) {
        tsEl.textContent = _formatVideoTimestamp(posForTs / fps);
      } else {
        tsEl.textContent = "--";
      }
    }
  // ...
    const serverSource = r.headers.get("X-Source");
    if (serverSource) {
      let decoded = serverSource;
      try { decoded = decodeURIComponent(serverSource); } catch (_) {}
      const sourceEl = document.getElementById("vfa-frame-modal-source");
      if (sourceEl) sourceEl.textContent = decoded;
    }
  } catch (e) {
    if (e.name === "AbortError") {
      _setFrameModalError("取帧已取消");
    } else {
      _setFrameModalError("取帧失败：" + (e.message || e));
    }
  }
}

async function vfaQueryLoadFacets() {
  const runId = VFAQuery.runId;
  if (!runId) return;
  try {
    const r = await fetch(
      API_PREFIX + "/video-file-analysis/runs/" + encodeURIComponent(runId) + "/facets",
      { cache: "no-store" }
    );
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();
    VFAQuery.facets = {
      scenes:  data.scenes  || [],
      actors:  data.actors  || [],
      objects: data.objects || [],
      actions: data.actions || [],
    };
    renderQueryFacets();
    document.getElementById("vfa-query-summary").textContent =
      `共 ${VFAQuery.facets.scenes.length} 场景 / ${VFAQuery.facets.actors.length} 人物 / ${VFAQuery.facets.objects.length} 物体 / ${VFAQuery.facets.actions.length} 动作。点击「反查」开始筛选。`;
  } catch (e) {
    document.getElementById("vfa-query-summary").textContent =
      "加载候选失败：" + e.message;
  }
}

function renderQueryFacets() {
  // ...
  const sceneSel = document.getElementById("vfa-query-scene");
  sceneSel.innerHTML = '<option value="">不限</option>';
  VFAQuery.facets.scenes.forEach((s) => {
    const opt = document.createElement("option");
    opt.value = s;
    opt.textContent = s;
    sceneSel.appendChild(opt);
  });
  // ...
  renderChips("vfa-query-actors", VFAQuery.facets.actors, "actors");
  renderChips("vfa-query-objects", VFAQuery.facets.objects, "objects");
  renderChips("vfa-query-actions", VFAQuery.facets.actions, "actions");
}

function renderChips(elId, items, kind) {
  const host = document.getElementById(elId);
  host.innerHTML = "";
  if (!items.length) {
    const empty = document.createElement("span");
    empty.className = "vfa-query-empty";
    empty.textContent = "（无）";
    host.appendChild(empty);
    return;
  }
  items.forEach((s) => {
    const chip = document.createElement("span");
    chip.className = "vfa-query-chip";
    chip.dataset.kind = kind;
    chip.dataset.value = s;
    chip.textContent = s;
    chip.title = "点击切换：选中=AND 过滤";
    chip.addEventListener("click", () => {
      const set = VFAQuery.selected[kind];
      if (set.has(s)) {
        set.delete(s);
        chip.classList.remove("is-on");
      } else {
        set.add(s);
        chip.classList.add("is-on");
      }
    });
    host.appendChild(chip);
  });
}

function vfaQueryBuildQuery() {
  const params = new URLSearchParams();
  const scene = document.getElementById("vfa-query-scene").value;
  if (scene) params.set("scene", scene);
  // ...
  VFAQuery.selected.actors.forEach((a) => params.append("actor", a));
  VFAQuery.selected.objects.forEach((o) => params.append("object", o));
  VFAQuery.selected.actions.forEach((a) => params.append("action", a));
  // ...
  const exact = document.getElementById("vfa-query-exact").value.trim();
  if (exact) params.set("exact", exact);
  // ...
  const q = document.getElementById("vfa-query-q").value.trim();
  if (q) params.set("q", q);
  // ...
  const tmin = document.getElementById("vfa-query-time-min").value;
  const tmax = document.getElementById("vfa-query-time-max").value;
  const fps = VFAQuery.fps || 0;
  if (tmin || tmax) {
    if (fps <= 0) {
      console.warn("vfa: 视频 fps 未知，时间范围 filter 被跳过");
    } else {
      const totalSec = (VFAQuery.totalFrames || 0) / fps;
  // ...
      const showClamp = (msg) => {
        const notice = document.getElementById("vfa-query-clamp-notice");
        const text  = document.getElementById("vfa-query-clamp-text");
        if (notice && text) {
          if (msg) {
            text.textContent = msg;
            notice.classList.remove("hidden");
          } else {
            notice.classList.add("hidden");
          }
        }
      };
      showClamp("");  // 重置，开 modal 第一次不会有遗留提示

      if (tmin.trim()) {
        const sec = _parseTimeToSeconds(tmin);
        if (sec === null) {
          console.warn(`vfa: 起始时间 "${tmin}" 格式错误（需要 HH:MM:SS），filter 跳过`);
        } else {
          const f = _secondsToFrame(sec, fps);
          if (f !== null) params.set("frame_min", String(f));
        }
      }
      if (tmax.trim()) {
        const sec = _parseTimeToSeconds(tmax);
        if (sec === null) {
          console.warn(`vfa: 结束时间 "${tmax}" 格式错误（需要 HH:MM:SS），filter 跳过`);
        } else {
  // ...
          let used = sec;
          if (totalSec > 0 && sec > totalSec) {
            used = totalSec;
            showClamp(`结束时间 ${_formatTimeHHMMSS(sec)} 超过视频总时长 ${_formatTimeHHMMSS(totalSec)}，已截断到 ${_formatTimeHHMMSS(totalSec)}`);
          }
          const f = _secondsToFrame(used, fps);
          if (f !== null) params.set("frame_max", String(f));
        }
      } else {
  // ...
      }
    }
  } else {
  // ...
    const notice = document.getElementById("vfa-query-clamp-notice");
    if (notice) notice.classList.add("hidden");
  }
  return params.toString();
}

function vfaQueryClear() {
  document.getElementById("vfa-query-scene").value = "";
document.getElementById("vfa-query-exact").value = "";
  document.getElementById("vfa-query-q").value = "";
  document.getElementById("vfa-query-time-min").value = "";
  document.getElementById("vfa-query-time-max").value = "";
  // ...
  ["vfa-query-time-min", "vfa-query-time-max"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) {
      el.classList.remove("vfa-query-input-invalid");
      el.removeAttribute("title");
    }
  });
  const notice = document.getElementById("vfa-query-clamp-notice");
  if (notice) notice.classList.add("hidden");
  VFAQuery.selected.actors.clear();
  VFAQuery.selected.objects.clear();
  VFAQuery.selected.actions.clear();
  document.querySelectorAll(".vfa-query-chip.is-on").forEach((c) => c.classList.remove("is-on"));
}

  // ...
async function vfaQueryRun() {
  const runId = VFAQuery.runId;
  if (!runId) return;
  const qs = vfaQueryBuildQuery();
  const summaryEl = document.getElementById("vfa-query-summary");
  const listEl = document.getElementById("vfa-query-list");
  // ...
  VFAQuery._terms = collectQueryTerms();

  summaryEl.textContent = "查询中…";
  listEl.innerHTML = "";

  try {
    const r = await fetch(
      API_PREFIX + "/video-file-analysis/runs/" + encodeURIComponent(runId) +
        "/query?" + qs,
      { cache: "no-store" }
    );
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();
    renderQueryResults(data);
  } catch (e) {
    summaryEl.textContent = "查询失败：" + e.message;
  }
}

  // ...
function collectQueryTerms() {
  const terms = new Set();
  const sceneEl = document.getElementById("vfa-query-scene");
  if (sceneEl && sceneEl.value) terms.add(sceneEl.value);
  VFAQuery.selected.actors.forEach((a) => terms.add(a));
  VFAQuery.selected.objects.forEach((o) => terms.add(o));
  VFAQuery.selected.actions.forEach((a) => terms.add(a));
  // ...
  const exactEl = document.getElementById("vfa-query-exact");
  if (exactEl && exactEl.value.trim()) terms.add(exactEl.value.trim());
  // ...
  const qEl = document.getElementById("vfa-query-q");
  if (qEl && qEl.value.trim()) {
    const q = qEl.value.trim();
    for (let i = 0; i < q.length - 1; i++) {
      terms.add(q.slice(i, i + 2));
    }
  }
  return [...terms].filter((t) => t.length >= 1);
}

function renderQueryResults(data) {
  const summaryEl = document.getElementById("vfa-query-summary");
  const listEl = document.getElementById("vfa-query-list");
  const events = data.events || [];
  summaryEl.textContent = `匹配 ${data.matched} 条${data.matched >= data.limit ? "（已截断）" : ""} · limit=${data.limit}`;
  if (!events.length) {
    listEl.innerHTML = '<li class="vd-event-empty" style="padding:0.6rem;text-align:center;">没有匹配的事件。</li>';
    return;
  }
listEl.innerHTML = "";
  events.forEach((ev) => {
    const s = ev.structured || {};
    const li = document.createElement("li");
    li.className = "vfa-query-result";
    li.title = "点击查看原始帧";
  // ...
    li.dataset.framePos = String(ev.frame_pos || 0);
    li.dataset.text = ev.text || "";
    li.dataset.structured = JSON.stringify(s || {});
    const head = document.createElement("div");
    head.className = "vfa-query-result-head";
    const frame = document.createElement("span");
    frame.className = "vfa-query-result-frame";
    frame.textContent = "帧 " + ev.frame_pos;
    const ts = document.createElement("span");
    ts.textContent = new Date((ev.ts || 0) * 1000).toLocaleTimeString();
    head.appendChild(frame);
    head.appendChild(ts);
    li.appendChild(head);
    const tags = document.createElement("div");
    tags.className = "vfa-query-result-tags";
    if (s.scene)   tags.appendChild(makeTag(s.scene, "scene"));
    if (s.action)  tags.appendChild(makeTag(s.action, "action"));
    (s.actors  || []).forEach((a) => tags.appendChild(makeTag("人:" + a, "actor")));
    (s.objects || []).forEach((o) => tags.appendChild(makeTag("物:" + o, "object")));
    li.appendChild(tags);
    if (s.note) {
      const note = document.createElement("div");
      note.className = "vfa-query-result-note";
      note.textContent = "注：" + s.note;
      li.appendChild(note);
    }
     const text = document.createElement("div");
     text.className = "vfa-query-result-text";
     text.innerHTML = highlightTerms(ev.text || "(空)", VFAQuery._terms || []);
     li.appendChild(text);
  // ...
     li.addEventListener("click", () => {
       openFrameModal(li);
     });
     listEl.appendChild(li);
   });
  }

  // ...
 function _escHtml(s) {
   return s.replace(/&/g, "&amp;")
           .replace(/</g, "&lt;")
           .replace(/>/g, "&gt;")
           .replace(/"/g, "&quot;")
           .replace(/'/g, "&#39;");
 }
 function _escRe(s) {
   return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
 }
 function highlightTerms(text, terms) {
   if (!text) return "";
   let out = _escHtml(text);
   for (const t of terms) {
     if (!t || t.length < 1) continue;
  // ...
     const pattern = _escRe(_escHtml(t));
     out = out.replace(new RegExp(pattern, "g"), `<mark>$&</mark>`);
   }
   return out;
 }

function makeTag(text, kind) {
  const t = document.createElement("span");
  t.className = "vfa-query-tag vfa-query-tag-" + kind;
  t.textContent = text;
  return t;
}

function initVideoFileAnalysis() {
  if (window.__vfaInit) return;
  window.__vfaInit = true;

  const sourceEl   = document.getElementById("vfa-source");
  const browseBtn  = document.getElementById("vfa-browse");
  const startBtn   = document.getElementById("vfa-start");
  const stopBtn    = document.getElementById("vfa-stop");
  const clearBtn   = document.getElementById("vfa-clear");
  const refreshBtn = document.getElementById("vfa-refresh");
  const errClose   = document.getElementById("vfa-error-close");

  function syncSourceMeta() {
    const v = (sourceEl?.value || "").trim();
    vfaSetSourceMeta(v ? "已选: " + v : "尚未选择");
  }
  sourceEl?.addEventListener("input", syncSourceMeta);
  sourceEl?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") startBtn?.click();
  });
  syncSourceMeta();

  browseBtn?.addEventListener("click", () => {
    if (!sourceEl) return;
  // ...
    window.VdFsModal._sourceTarget = sourceEl;
    window.VdFsModal.open("");
  });

  startBtn?.addEventListener("click", vfaStart);
  stopBtn ?.addEventListener("click", vfaStop);
  clearBtn?.addEventListener("click", vfaClear);
  refreshBtn?.addEventListener("click", vfaFetchEvents);
  errClose?.addEventListener("click", () => vfaShowError(""));

  // ...
  document.getElementById("vfa-query-close")?.addEventListener("click", vfaQueryClose);
  const runBtn = document.getElementById("vfa-query-run");
  runBtn?.addEventListener("click", vfaQueryRun);
  document.getElementById("vfa-query-clear")?.addEventListener("click", vfaQueryClear);
  // ...
  document.getElementById("vfa-query-time-min")
    ?.addEventListener("blur", (e) => _validateTimeInput(e.target));
  document.getElementById("vfa-query-time-max")
    ?.addEventListener("blur", (e) => _validateTimeInput(e.target));
  document.getElementById("vfa-query")?.addEventListener("click", (e) => {
  // ...
    if (e.target.id === "vfa-query") vfaQueryClose();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !document.getElementById("vfa-query").classList.contains("hidden")) {
      vfaQueryClose();
    }
  });

  // ...
  document.getElementById("vfa-frame-modal-close")?.addEventListener("click", vfaFrameModalClose);
  document.getElementById("vfa-frame-modal")?.addEventListener("click", (e) => {
    if (e.target.id === "vfa-frame-modal") vfaFrameModalClose();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !document.getElementById("vfa-frame-modal").classList.contains("hidden")) {
      vfaFrameModalClose();
    }
  });

  // ...
  const histRefresh = document.getElementById("vfa-history-refresh");
  histRefresh?.addEventListener("click", vfaFetchHistory);

  // ...
  vfaFetchSnapshot();
  vfaFetchEvents();
  vfaFetchHistory();
  // ...
  if (!VFA.ws && !VFA.wsTimer) connectVfaWs();
}



initVideoFileAnalysis();

})();
