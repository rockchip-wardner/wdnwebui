const API_PREFIX = "/api";
const DEFAULT_PAGE = "system-monitor";

async function fetchVersion() {
  try {
    const r = await fetch("/health");
    if (!r.ok) return;
    const el = document.getElementById("brand-version");
    if (el) el.textContent = "v" + (window.__APP_VERSION__ || "0.1.0");
  } catch (_) { /* ignore */ }
}

function showError(target, msg) {
  target.textContent = "请求失败: " + msg;
}

function setBar(id, pct, warnAt = 70, critAt = 90) {
  const el = document.getElementById(id);
  if (!el) return;
  el.style.width = (pct == null ? 0 : Math.min(100, Math.max(0, pct))) + "%";
  el.classList.remove("warn", "crit");
  if (pct != null) {
    if (pct >= critAt) el.classList.add("crit");
    else if (pct >= warnAt) el.classList.add("warn");
  }
}

function fmtBytes(mb) {
  if (mb == null) return "--";
  if (mb >= 1024) return (mb / 1024).toFixed(2) + " GB";
  return mb.toFixed(1) + " MB";
}

function fmtNum(n, digits = 1) {
  if (n == null) return "--";
  return Number(n).toFixed(digits);
}

function renderSystemMonitor(data) {
  const json = document.getElementById("data-system-monitor");
  json.textContent = JSON.stringify(data, null, 2);

  document.getElementById("system-monitor-time").textContent =
    data.collected_at ? "采集于 " + data.collected_at : "";

  const cpu = data.cpu || {};
  const cpuPct = cpu.percent;
  document.getElementById("cpu-percent").textContent =
    cpuPct == null ? "--" : fmtNum(cpuPct) + " %";
  setBar("cpu-bar", cpuPct);
  const cpuMeta = [];
  if (cpu.count) cpuMeta.push(cpu.count + " 核");
  if (cpu.load_avg) cpuMeta.push("load " + cpu.load_avg["1"] + " / " + cpu.load_avg["5"] + " / " + cpu.load_avg["15"]);
  document.getElementById("cpu-meta").textContent = cpuMeta.join(" · ") || "";

  const cores = document.getElementById("cpu-cores");
  cores.innerHTML = "";
  (cpu.per_core || []).forEach((v, i) => {
    const cell = document.createElement("div");
    cell.className = "core-cell";
    cell.textContent = "C" + i + "\n" + fmtNum(v, 0) + "%";
    if (v >= 90) cell.style.background = "#fee2e2";
    else if (v >= 70) cell.style.background = "#fef3c7";
    cores.appendChild(cell);
  });

  const mem = data.memory || {};
  document.getElementById("mem-percent").textContent =
    mem.percent == null ? "--" : fmtNum(mem.percent) + " %";
  setBar("mem-bar", mem.percent);
  document.getElementById("mem-meta").textContent =
    (mem.used_mb != null ? "已用 " + fmtBytes(mem.used_mb) : "已用 --") +
    " / " +
    (mem.total_mb != null ? fmtBytes(mem.total_mb) : "--");

  const npu = data.npu || {};
  const badge = document.getElementById("npu-badge");
  const msgEl = document.getElementById("npu-message");
  const tbody = document.getElementById("npu-tbody");
  const rawEl = document.getElementById("npu-raw");

  badge.classList.remove("ok", "warn", "err");
  if (npu.available && (npu.devices || []).length > 0) {
    badge.textContent = npu.devices.length + " device(s) · online";
    badge.classList.add("ok");
    msgEl.classList.add("hidden");
  } else {
    badge.textContent = "offline";
    badge.classList.add("err");
    msgEl.classList.remove("hidden");
    msgEl.textContent = (npu.source || "rknn3lite") + ": " + (npu.reason || "no devices");
  }
  rawEl.textContent = JSON.stringify(npu, null, 2);

  tbody.innerHTML = "";
  const devices = npu.devices || [];
  if (!devices.length) {
    tbody.innerHTML = '<tr><td colspan="9" class="empty-row">未检测到 NPU 设备</td></tr>';
    return;
  }
  devices.forEach(d => {
    const tr = document.createElement("tr");
    const memStr = (d.mem_used_mb != null && d.mem_total_mb != null)
      ? fmtNum(d.mem_used_mb, 0) + " / " + fmtNum(d.mem_total_mb, 0)
      : "--";
    const cells = [
      d.index,
      d.id || "--",
      d.bus_id || "--",
      d.status || "--",
      d.health || "--",
      d.temp_c == null ? "--" : fmtNum(d.temp_c, 1),
      d.power_mw == null ? "--" : d.power_mw,
      d.npu_percent == null ? "--" : fmtNum(d.npu_percent, 1),
      memStr,
    ];
    cells.forEach(v => {
      const td = document.createElement("td");
      td.textContent = v;
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
}

/* ------------------------------------------------------------------ *
 *  video-detection page (WebSocket + Canvas)                         *
 * ------------------------------------------------------------------ */
const VD = {
  ws: null,
  wsRetry: 0,
  wsTimer: null,
  blobUrl: null,
  inflight: 0,
  init() { return initVideoDetection(); },
};

const VDDetector = {
  events: [],
  badgeTimer: null,
  refresh() { return refreshVdDetector(); },
};

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
    // binary frame (JPEG bytes)
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

const VdFsModal = {
  selected: null,
  open(root) {
    const el = document.getElementById("vd-modal");
    el.classList.remove("hidden");
    this.navigate(root || "");
  },
  close() {
    document.getElementById("vd-modal").classList.add("hidden");
    this.selected = null;
  },
  setHint(text) {
    document.getElementById("vd-modal-hint").textContent = text || "";
  },
  setPickFileEnabled(on) {
    const btn = document.getElementById("vd-modal-pick-file");
    btn.disabled = !on;
  },
  renderCrumbs(path, root) {
    const el = document.getElementById("vd-crumbs");
    el.innerHTML = "";
    const segs = path.split("/").filter(Boolean);
    const prefix = document.createElement("a");
    prefix.textContent = "/";
    prefix.addEventListener("click", () => this.navigate(""));
    el.appendChild(prefix);
    let acc = "";
    segs.forEach((seg, i) => {
      acc += "/" + seg;
      const sep = document.createElement("span");
      sep.className = "vd-crumb-sep";
      sep.textContent = "/";
      el.appendChild(sep);
      if (i === segs.length - 1) {
        const cur = document.createElement("span");
        cur.className = "vd-crumb-current";
        cur.textContent = seg;
        el.appendChild(cur);
      } else {
        const a = document.createElement("a");
        a.textContent = seg;
        const target = acc;
        a.addEventListener("click", () => this.navigate(target));
        el.appendChild(a);
      }
    });
  },
  renderEntries(entries) {
    const el = document.getElementById("vd-list");
    el.innerHTML = "";
    if (!entries.length) {
      const e = document.createElement("div");
      e.className = "vd-entry-empty";
      e.textContent = "(空目录)";
      el.appendChild(e);
      return;
    }
    entries.forEach((entry) => {
      const row = document.createElement("div");
      row.className = "vd-entry";
      row.classList.add(entry.type);
      if (isVideoPath(entry.path)) row.classList.add("video");

      const icon = document.createElement("span");
      icon.className = "vd-entry-icon";
      icon.textContent = entry.type === "dir" ? "▸" : "·";

      const name = document.createElement("span");
      name.className = "vd-entry-name";
      name.textContent = entry.name;

      const meta = document.createElement("span");
      meta.className = "vd-entry-meta";
      meta.textContent = entry.type === "file" ? fmtSize(entry.size) : "";

      row.appendChild(icon);
      row.appendChild(name);
      row.appendChild(meta);

      row.addEventListener("click", () => {
        if (entry.type === "dir") {
          this.navigate(entry.path);
        } else {
          // single-select file
          el.querySelectorAll(".vd-entry.selected").forEach((n) => n.classList.remove("selected"));
          row.classList.add("selected");
          this.selected = entry;
          this.setPickFileEnabled(true);
          this.setHint("已选中: " + entry.path);
        }
      });
      el.appendChild(row);
    });
  },
  async navigate(path) {
    this.selected = null;
    this.setPickFileEnabled(false);
    this.setHint("加载中…");
    const list = document.getElementById("vd-list");
    list.innerHTML = '<div class="vd-entry-empty">加载中…</div>';
    try {
      const q = path ? `?path=${encodeURIComponent(path)}` : "";
      const r = await fetch(API_PREFIX + "/video-detection/fs" + q);
      if (!r.ok) {
        const detail = await r.text().catch(() => "");
        list.innerHTML = `<div class="vd-entry-error">${(detail || r.status)}</div>`;
        this.setHint("加载失败");
        return;
      }
      const data = await r.json();
      this.renderCrumbs(data.path, data.root);
      this.renderEntries(data.entries);
      const dirBtn = document.getElementById("vd-modal-pick-dir");
      dirBtn.disabled = !!data.is_root;
      dirBtn.dataset.path = data.path;
      let hint = data.is_root ? "已限制在 " + data.root + " 下" : "点击文件名选择，点击目录进入";
      if (data.truncated) hint += " (结果被截断)";
      this.setHint(hint);
    } catch (e) {
      list.innerHTML = `<div class="vd-entry-error">${e.message}</div>`;
      this.setHint("加载失败");
    }
  },
  pickCurrentDir() {
    const btn = document.getElementById("vd-modal-pick-dir");
    const p = btn.dataset.path;
    if (!p) return;
    document.getElementById("vd-source").value = p;
    this.close();
  },
  pickSelectedFile() {
    if (!this.selected) return;
    document.getElementById("vd-source").value = this.selected.path;
    this.close();
  },
};

/* ------------------------------------------------------------------ *
 *  VLM detection (rkai) — prompt + event history + overlay            *
 * ------------------------------------------------------------------ */
function fmtEventTime(ts) {
  if (!ts) return "--";
  try {
    const d = new Date(ts * 1000);
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  } catch (_) { return "--"; }
}

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
    text.textContent = ev.text || "(空)";
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
  text.textContent = ev.text || "(空)";
  badge.classList.remove("hidden");
  // restart animation by toggling a class
  badge.style.animation = "none";
  // eslint-disable-next-line no-unused-expressions
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
    statusEl.textContent = "不可用: " + (snap.init_error || "rkai 未安装");
  } else if (snap.init_error) {
    statusEl.textContent = "模型加载失败: " + snap.init_error;
  } else if (snap.enabled && snap.session_ready) {
    statusEl.textContent =
      `运行中 · 每 ${snap.every_n} 帧 · 队列 ${snap.queue_size}`;
  } else if (snap.enabled) {
    statusEl.textContent =
      `已启动，加载模型… (首次约 30s，队列 ${snap.queue_size})`;
  } else if (snap.session_ready) {
    statusEl.textContent = `已加载 (${snap.model})`;
  } else {
    statusEl.textContent = "已安装但未启动";
  }
  if (startBtn) startBtn.disabled = !snap.available || !!snap.enabled;
  if (stopBtn)  stopBtn.disabled  = !snap.enabled;
}

async function refreshVdDetector() {
  try {
    const r = await fetch(API_PREFIX + "/video-detection/events");
    if (!r.ok) return null;
    const data = await r.json();
    updateVdDetectorStatus(data.snapshot || {});
    // hydrate history if we just opened the page
    if (VDDetector.events.length === 0 && Array.isArray(data.events)) {
      // events are stored newest-first in detector; copy as-is so the
      // display matches what the server already accumulated
      VDDetector.events = data.events.slice();
      renderVdEventList();
    }
    return data;
  } catch (_) { return null; }
}

async function vdDetectorStart() {
  const promptEl  = document.getElementById("vd-prompt");
  const everyEl   = document.getElementById("vd-every-n");
  const prompt = (promptEl?.value || "").trim();
  const every_n = parseInt(everyEl?.value || "30", 10);
  if (!prompt) {
    setVdOverlay("请先填写检测提示词", true);
    return;
  }
  try {
    const r = await fetch(API_PREFIX + "/video-detection/prompt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt, every_n }),
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

  const sourceEl = document.getElementById("vd-source");
  const openBtn = document.getElementById("vd-open");
  const closeBtn = document.getElementById("vd-close");
  const browseBtn = document.getElementById("vd-browse");
  const playBtn = document.getElementById("vd-play");
  const pauseBtn = document.getElementById("vd-pause");
  const stopBtn = document.getElementById("vd-stop");
  const replayBtn = document.getElementById("vd-replay");

  function refreshReplayBtn() {
    replayBtn.disabled = !(sourceEl.value || "").trim();
  }
  sourceEl.addEventListener("input", refreshReplayBtn);
  refreshReplayBtn();

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
      const snap = await vdOpen({ source: src });
      setVdMeta(snap);
      setVdProgress(snap);
    } catch (e) {
      setVdOverlay("打开失败: " + e.message, true);
    } finally {
      openBtn.disabled = false;
    }
  });

  // Browse button: open the server-side file picker modal (NOT a local
  // upload — the path selected here is the path on the device the
  // server is running on).
  browseBtn.addEventListener("click", () => VdFsModal.open(""));
  document.getElementById("vd-modal-close").addEventListener("click", () => VdFsModal.close());
  document.getElementById("vd-modal-pick-dir").addEventListener("click", () => VdFsModal.pickCurrentDir());
  document.getElementById("vd-modal-pick-file").addEventListener("click", () => VdFsModal.pickSelectedFile());
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      const m = document.getElementById("vd-modal");
      if (!m.classList.contains("hidden")) VdFsModal.close();
    }
  });
  document.getElementById("vd-modal").addEventListener("click", (e) => {
    if (e.target.id === "vd-modal") VdFsModal.close();
  });

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
  replayBtn.addEventListener("click", async () => {
    const src = (sourceEl.value || "").trim();
    if (!src) {
      setVdOverlay("请先输入视频源", true);
      return;
    }
    replayBtn.disabled = true;
    setVdOverlay("正在重播 " + src + " …", false);
    try {
      const snap = await vdOpen({ source: src });
      setVdMeta(snap);
      setVdProgress(snap);
    } catch (e) {
      setVdOverlay("重播失败: " + e.message, true);
    } finally {
      refreshReplayBtn();
    }
  });

  // --- detection control buttons ---
  const detStartBtn = document.getElementById("vd-detector-start");
  const detStopBtn  = document.getElementById("vd-detector-stop");
  const evClearBtn  = document.getElementById("vd-events-clear");
  if (detStartBtn) detStartBtn.addEventListener("click", vdDetectorStart);
  if (detStopBtn)  detStopBtn.addEventListener("click",  vdDetectorStop);
  if (evClearBtn)  evClearBtn.addEventListener("click",  vdEventsClear);

  // Initial state hydration (when navigating back to the page)
  refreshVdDetector();

  if (!VD.ws && !VD.wsTimer) connectVdWs();
}

/* ------------------------------------------------------------------ *
 *  system logs page (journalctl-backed)                              *
 * ------------------------------------------------------------------ */
const Logs = {
  ws: null,
  wsRetry: 0,
  wsTimer: null,
  init() { return initLogs(); },
  refresh() { return refreshLogs(); },
};

function setLogsStatus(text, isError) {
  const el = document.getElementById("system-logs-status");
  if (!el) return;
  el.textContent = text || "";
  el.style.color = isError ? "#b91c1c" : "";
}

function fmtLogTs(ts_ms) {
  if (!ts_ms) return "--:--:--";
  const d = new Date(ts_ms);
  if (isNaN(d.getTime())) return "--";
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${String(d.getMilliseconds()).padStart(3, "0")}`;
}

function appendLogEntry(entry) {
  const list = document.getElementById("logs-list");
  if (!list) return;
  // Drop the empty placeholder if it's still there.
  const empty = list.querySelector(".logs-empty");
  if (empty) empty.remove();
  const li = document.createElement("li");
  li.className = "log-entry prio-" + (entry.priority_name || "info");
  const ts = document.createElement("span");
  ts.className = "ts";
  ts.textContent = fmtLogTs(entry.ts_ms);
  const prio = document.createElement("span");
  prio.className = "prio";
  prio.textContent = entry.priority_name || "?";
  prio.title = "priority " + (entry.priority || "?");
  const unit = document.createElement("span");
  unit.className = "unit";
  unit.textContent = (entry.unit || "?") + (entry.pid ? "[" + entry.pid + "]" : "");
  const msg = document.createElement("span");
  msg.className = "msg";
  msg.textContent = entry.message || "";
  li.appendChild(ts);
  li.appendChild(prio);
  li.appendChild(unit);
  li.appendChild(msg);
  list.appendChild(li);

  // Cap the DOM at ~1500 rows so the page stays light under heavy
  // follow traffic. Trim from the front (oldest entries).
  while (list.childElementCount > 1500) {
    list.removeChild(list.firstElementChild);
  }

  // Auto-scroll only if user is parked at (or near) the bottom.
  const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 60;
  if (nearBottom) {
    list.scrollTop = list.scrollHeight;
  }
}

function setLogsCounter(n) {
  const el = document.getElementById("logs-counter");
  if (el) el.textContent = n + " 条";
}

function setLogsEmpty(text, isError) {
  const list = document.getElementById("logs-list");
  if (!list) return;
  list.innerHTML = "";
  const div = document.createElement("div");
  div.className = "logs-empty" + (isError ? " error" : "");
  div.textContent = text;
  list.appendChild(div);
}

async function populateUnits() {
  const sel = document.getElementById("logs-unit");
  if (!sel) return;
  try {
    const r = await fetch(API_PREFIX + "/system-logs/units");
    if (!r.ok) return;
    const data = await r.json();
    if (!data.available) {
      setLogsStatus("journalctl 不可用", true);
      return;
    }
    // Keep the first "全部" option, drop the rest, then append fresh ones.
    while (sel.options.length > 1) sel.remove(1);
    (data.units || []).forEach((u) => {
      const opt = document.createElement("option");
      opt.value = u.unit;
      opt.textContent = `${u.unit} (${u.state})`;
      opt.title = u.description || "";
      sel.appendChild(opt);
    });
  } catch (_) { /* ignore */ }
}

async function refreshLogs() {
  const unitEl = document.getElementById("logs-unit");
  const prioEl = document.getElementById("logs-priority");
  const linesEl = document.getElementById("logs-lines");
  const params = new URLSearchParams();
  if (unitEl && unitEl.value) params.set("unit", unitEl.value);
  if (prioEl && prioEl.value) params.set("priority", prioEl.value);
  if (linesEl && linesEl.value) params.set("lines", linesEl.value);
  setLogsStatus("加载中…");
  setLogsEmpty("加载中…");
  try {
    const r = await fetch(API_PREFIX + "/system-logs?" + params.toString());
    const body = await r.json();
    if (!r.ok) {
      setLogsEmpty("加载失败: " + (body.detail || r.status), true);
      setLogsStatus("加载失败", true);
      return;
    }
    const list = document.getElementById("logs-list");
    if (list) list.innerHTML = "";
    (body.entries || []).forEach(appendLogEntry);
    setLogsCounter(body.entries ? body.entries.length : 0);
    setLogsStatus(body.truncated ? `已显示 ${body.entries.length} 行 (可能更多)` : `已显示 ${body.entries.length} 行`);
    document.getElementById("data-system-logs").textContent =
      JSON.stringify(body, null, 2);
  } catch (e) {
    setLogsEmpty("请求失败: " + e.message, true);
    setLogsStatus("请求失败", true);
  }
}

function logsWsUrl() {
  const unitEl = document.getElementById("logs-unit");
  const prioEl = document.getElementById("logs-priority");
  const params = new URLSearchParams();
  if (unitEl && unitEl.value) params.set("unit", unitEl.value);
  if (prioEl && prioEl.value) params.set("priority", prioEl.value);
  const qs = params.toString();
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}${API_PREFIX}/system-logs/ws${qs ? "?" + qs : ""}`;
}

function startLogsFollow() {
  if (Logs.ws) return;  // already following
  setLogsStatus("连接追踪…");
  const ws = new WebSocket(logsWsUrl());
  Logs.ws = ws;
  ws.onopen = () => {
    Logs.wsRetry = 0;
    setLogsStatus("追踪中…");
  };
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === "ready") {
      setLogsStatus(`追踪中 (journalctl pid=${msg.pid})`);
    } else if (msg.type === "line" && msg.entry) {
      appendLogEntry(msg.entry);
      const list = document.getElementById("logs-list");
      const n = list ? list.querySelectorAll(".log-entry").length : 0;
      setLogsCounter(n);
    } else if (msg.type === "info" || msg.type === "error") {
      setLogsStatus(msg.detail || msg.type, msg.type === "error");
    } else if (msg.type === "ping") {
      /* keepalive */
    }
  };
  ws.onclose = () => {
    Logs.ws = null;
    if (document.getElementById("logs-follow")?.checked) {
      const wait = Math.min(5000, 500 * Math.pow(2, Logs.wsRetry++));
      Logs.wsTimer = setTimeout(startLogsFollow, wait);
    } else {
      setLogsStatus("");
    }
  };
  ws.onerror = () => {
    try { ws.close(); } catch (_) {}
  };
}

function stopLogsFollow() {
  Logs.wsRetry = 0;
  if (Logs.wsTimer) {
    clearTimeout(Logs.wsTimer);
    Logs.wsTimer = null;
  }
  if (Logs.ws) {
    try { Logs.ws.close(); } catch (_) {}
    Logs.ws = null;
  }
}

function initLogs() {
  if (window.__logsInit) return;
  window.__logsInit = true;

  const refreshBtn = document.getElementById("system-logs-refresh");
  const followEl   = document.getElementById("logs-follow");
  const clearBtn   = document.getElementById("logs-clear");
  const unitEl     = document.getElementById("logs-unit");
  const prioEl     = document.getElementById("logs-priority");

  if (refreshBtn) refreshBtn.addEventListener("click", () => {
    Logs.refresh().then(() => {
      if (followEl && followEl.checked) {
        stopLogsFollow();
        startLogsFollow();
      }
    });
  });
  if (followEl) followEl.addEventListener("change", () => {
    if (followEl.checked) {
      Logs.refresh().then(startLogsFollow);
    } else {
      stopLogsFollow();
      setLogsStatus("");
    }
  });
  if (clearBtn) clearBtn.addEventListener("click", () => {
    const list = document.getElementById("logs-list");
    if (list) list.innerHTML = "";
    setLogsCounter(0);
    document.getElementById("data-system-logs").textContent = "";
  });
  // When filters change, re-fetch and (if following) restart the WS.
  if (unitEl)   unitEl.addEventListener("change",   onLogsFilterChanged);
  if (prioEl)   prioEl.addEventListener("change",   onLogsFilterChanged);
  document.getElementById("logs-lines")?.addEventListener("change", onLogsFilterChanged);

  populateUnits();
}

function onLogsFilterChanged() {
  stopLogsFollow();
  Logs.refresh().then(() => {
    if (document.getElementById("logs-follow")?.checked) startLogsFollow();
  });
}

/* ------------------------------------------------------------------ *
 *  page loader / router                                              *
 * ------------------------------------------------------------------ */

async function loadPageData(name) {
  if (name === "system-monitor") {
    const json = document.getElementById("data-system-monitor");
    json.textContent = "加载中…";
    try {
      const r = await fetch(API_PREFIX + "/" + name);
      if (!r.ok) throw new Error("HTTP " + r.status);
      renderSystemMonitor(await r.json());
    } catch (err) {
      showError(json, err.message);
    }
    return;
  }

  if (name === "video-detection") {
    VD.init();
    // initial state sync — drives button enable/disable so the user
    // can tell which control is currently valid
    await vdStatus();
    return;
  }

  if (name === "system-logs") {
    Logs.init();
    await Logs.refresh();
    return;
  }

  const target = document.getElementById("data-" + name);
  if (!target) return;
  target.textContent = "加载中…";
  try {
    const r = await fetch(API_PREFIX + "/" + name);
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();
    target.textContent = JSON.stringify(data, null, 2);
  } catch (err) {
    showError(target, err.message);
  }
}

function showPage(name) {
  if (!document.getElementById("page-" + name)) name = DEFAULT_PAGE;
  document.querySelectorAll(".page").forEach(p => {
    p.classList.toggle("hidden", p.dataset.page !== name);
  });
  document.querySelectorAll(".nav-item").forEach(a => {
    a.classList.toggle("active", a.dataset.page === name);
  });
  loadPageData(name);
}

function currentPage() {
  const hash = (location.hash || "").replace(/^#/, "");
  return hash || DEFAULT_PAGE;
}

document.querySelectorAll(".nav-item").forEach(a => {
  a.addEventListener("click", () => { /* hashchange handles the rest */ });
});

document.querySelectorAll("[data-refresh]").forEach(btn => {
  btn.addEventListener("click", () => loadPageData(currentPage()));
});

window.addEventListener("hashchange", () => showPage(currentPage()));

fetchVersion();
showPage(currentPage());
