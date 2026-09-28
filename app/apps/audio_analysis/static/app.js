(function () {
  "use strict";
  const API_PREFIX = "/api";
  // core helpers (fmtNum, fmtEventTime, sanitizeEventText, loadModelPick) via app/static/core/*.js
  const fmtNum = window.fmtNum;
  const fmtEventTime = window.fmtEventTime;
  const sanitizeEventText = window.sanitizeEventText;
  // ...
  const loadModelPick = window.loadModelPick;

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

const VIDEO_EXTS = new Set([
  ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".ts",
  ".m4v", ".3gp", ".wmv", ".mpg", ".mpeg", ".rmvb",
  ".h264", ".h265", ".hevc",
]);

function fmtSize(n) {
  if (n == null) return "";
  if (n >= 1024 * 1024 * 1024) return (n / 1024 / 1024 / 1024).toFixed(2) + " GB";
  if (n >= 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " MB";
  if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
  return n + " B";
}

function basename(p) {
  const i = String(p).lastIndexOf("/");
  return i < 0 ? String(p) : String(p).slice(i + 1);
}

function isVideoPath(p) {
  const i = String(p).lastIndexOf(".");
  return i >= 0 && VIDEO_EXTS.has(String(p).slice(i).toLowerCase());
}



  // ...

})();
