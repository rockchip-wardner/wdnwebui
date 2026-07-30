const API_PREFIX = "/api";
const DEFAULT_PAGE = "system-monitor";

async function fetchVersion() {
  try {
    const r = await fetch("/health");
    if (!r.ok) return;
    const el = document.getElementById("brand-version");
    if (el) el.textContent = "v" + (window.__APP_VERSION__ || "0.3.0");
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

  const thermal = data.thermal || {};
  const tBadge = document.getElementById("thermal-badge");
  const tMsgEl = document.getElementById("thermal-message");
  const tGrid = document.getElementById("thermal-grid");
  tBadge.classList.remove("ok", "warn", "err");
  const sensors = thermal.sensors || [];
  if (thermal.available && sensors.length) {
    tBadge.textContent = sensors.length + " 路温控 · 在线";
    tBadge.classList.add("ok");
    tMsgEl.classList.add("hidden");
  } else {
    tBadge.textContent = "offline";
    tBadge.classList.add("err");
    tMsgEl.classList.remove("hidden");
    tMsgEl.textContent = (thermal.source || "sysfs") + ": " + (thermal.reason || "no thermal sensors");
  }
  tGrid.innerHTML = "";
  if (!sensors.length) {
    const empty = document.createElement("div");
    empty.className = "thermal-empty";
    empty.textContent = "未检测到温控传感器";
    tGrid.appendChild(empty);
  } else {
    const SCALE_MIN = -40;
    const SCALE_MAX = 85;
    const SPAN = SCALE_MAX - SCALE_MIN;
    sensors.forEach(s => {
      const tempC = s.temp_c == null ? null : Number(s.temp_c);
      const card = document.createElement("div");
      let level = "ok";
      if (tempC != null) {
        if (tempC < 0) level = "cold";
        else if (tempC >= 85) level = "crit";
        else if (tempC >= 80) level = "hot";
        else if (tempC >= 70) level = "warn";
      }
      card.className = "thermal-card " + level;

      const head = document.createElement("div");
      head.className = "thermal-card-head";
      const label = document.createElement("span");
      label.className = "thermal-label";
      label.textContent = s.label || s.name || "--";
      label.title = s.label || s.name || "";
      const loc = document.createElement("span");
      loc.className = "thermal-loc";
      loc.textContent = s.name || "";
      loc.title = s.name || "";
      head.appendChild(label);
      head.appendChild(loc);
      card.appendChild(head);

      const val = document.createElement("div");
      val.className = "thermal-value";
      if (tempC == null) {
        val.textContent = "--";
      } else {
        const num = document.createElement("span");
        num.textContent = tempC.toFixed(1);
        val.appendChild(num);
        const unit = document.createElement("span");
        unit.className = "thermal-value-unit";
        unit.textContent = " °C";
        val.appendChild(unit);
      }
      card.appendChild(val);

      const bar = document.createElement("div");
      bar.className = "thermal-bar";
      const fill = document.createElement("div");
      fill.className = "thermal-bar-fill " + (level === "ok" ? "" : level);
      if (tempC != null) {
        const pct = Math.min(100, Math.max(0, (tempC - SCALE_MIN) / SPAN * 100));
        fill.style.width = pct + "%";
      }
      bar.appendChild(fill);
      const track = document.createElement("div");
      track.className = "thermal-bar-track";
      bar.appendChild(track);
      card.appendChild(bar);

      tGrid.appendChild(card);
    });
  }

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
 *  base config page (基础配置) — currently just one field: the RKNN   *
 *  model_path. Read on click via GET /api/config/base; saved with    *
 *  PUT /api/config/base which validates the absolute-path shape and  *
 *  atomically swaps the "base" section in config.json without        *
 *  touching other sections.                                         *
 * ------------------------------------------------------------------ */
const Base = {
  initialized: false,
  dirty: false,

  init() {
    if (this.initialized) return;
    this.initialized = true;

    const form = document.getElementById("base-config-form");
    const refreshBtn = document.getElementById("base-config-refresh");
    const reloadBtn = document.getElementById("bc-reload");

    if (refreshBtn) refreshBtn.addEventListener("click", () => {
      this.load();
      this.loadSurveillance();
    });
    if (reloadBtn) reloadBtn.addEventListener("click", () => {
      this.load();
      this.loadSurveillance();
    });

    // Track edits so "放弃修改" actually drops them.
    form.addEventListener("input", () => {
      this.dirty = true;
      this.updateFieldStatus();
    });

    if (form) {
      form.addEventListener("submit", (e) => {
        e.preventDefault();
        this.save();
      });
    }

    this.initSurveillance();
  },

  async load() {
    try {
      const r = await fetch(API_PREFIX + "/config/base");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      this.render(data);
    } catch (e) {
      this.setFoot("加载失败: " + e.message, "danger");
    }
  },

  render(data) {
    this.dirty = false;
    const section = (data && data.section) || {};
    const deviceNameInput = document.getElementById("bc-device-name");
    if (deviceNameInput) deviceNameInput.value = section.device_name || "";
    const modelPathInput = document.getElementById("bc-model-path");
    if (modelPathInput) modelPathInput.value = section.model_path || "";
    const resourceDirInput = document.getElementById("bc-resource-dir");
    if (resourceDirInput) resourceDirInput.value = section.resource_dir || "";
    this.refreshPathLabel();
    document.getElementById("data-base-config").textContent =
      JSON.stringify({
        section,
        model_path_exists: data.model_path_exists,
        resource_dir_exists: data.resource_dir_exists,
      }, null, 2);
    this.updateFieldStatus(data.model_path_exists);
    this.updateResourceDirStatus(data.resource_dir_exists);
    this.setFoot("", "");
  },

  updateFieldStatus(existsOverride) {
    const v = (document.getElementById("bc-model-path")?.value || "").trim();
    const el = document.getElementById("bc-model-path-status");
    if (!el) return;
    el.classList.remove("ok", "warn", "danger");
    if (!v) {
      el.textContent = "请填写";
      el.classList.add("warn");
      return;
    }
    if (!v.startsWith("/")) {
      el.textContent = "✗ 必须是绝对路径";
      el.classList.add("danger");
      return;
    }
    const exists = (typeof existsOverride === "boolean")
      ? existsOverride
      : false;  // unknown without a server check; refresh for an accurate read
    if (exists) {
      el.textContent = "✓ 目录存在";
      el.classList.add("ok");
    } else {
      el.textContent = "保存后再确认目录是否存在";
      el.classList.add("warn");
    }
  },

  updateResourceDirStatus(existsOverride) {
    const v = (document.getElementById("bc-resource-dir")?.value || "").trim();
    const el = document.getElementById("bc-resource-dir-status");
    if (!el) return;
    el.classList.remove("ok", "warn", "danger");
    if (!v) {
      el.textContent = "请填写";
      el.classList.add("warn");
      return;
    }
    if (!v.startsWith("/")) {
      el.textContent = "✗ 必须是绝对路径";
      el.classList.add("danger");
      return;
    }
    const exists = (typeof existsOverride === "boolean")
      ? existsOverride
      : false;
    if (exists) {
      el.textContent = "✓ 目录存在";
      el.classList.add("ok");
    } else {
      el.textContent = "保存后再确认目录是否存在";
      el.classList.add("warn");
    }
  },

  async save() {
    const deviceName = (document.getElementById("bc-device-name")?.value || "").trim();
    const modelPath = (document.getElementById("bc-model-path")?.value || "").trim();
    const resourceDir = (document.getElementById("bc-resource-dir")?.value || "").trim();
    if (!deviceName) {
      this.setFoot("设备名不能为空", "danger");
      return;
    }
    if (!modelPath) {
      this.setFoot("模型目录不能为空", "danger");
      this.updateFieldStatus();
      return;
    }
    if (!modelPath.startsWith("/")) {
      this.setFoot("模型目录必须是绝对路径 (以 / 开头)", "danger");
      this.updateFieldStatus();
      return;
    }
    if (!resourceDir) {
      this.setFoot("资源存放目录不能为空", "danger");
      this.updateResourceDirStatus();
      return;
    }
    if (!resourceDir.startsWith("/")) {
      this.setFoot("资源存放目录必须是绝对路径 (以 / 开头)", "danger");
      this.updateResourceDirStatus();
      return;
    }

    const saveBtn = document.getElementById("bc-save");
    const reloadBtn = document.getElementById("bc-reload");
    saveBtn.disabled = true;
    if (reloadBtn) reloadBtn.disabled = true;
    this.setFoot("正在保存…", "warn");

    try {
      const r = await fetch(API_PREFIX + "/config/base", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          device_name: deviceName,
          model_path: modelPath,
          resource_dir: resourceDir,
        }),
      });
      const text = await r.text();
      let body;
      try { body = JSON.parse(text); } catch { body = { detail: text }; }
      if (!r.ok) {
        const msg = body.detail || ("HTTP " + r.status);
        this.setFoot("保存失败: " + msg, "danger");
        return;
      }
      this.refreshPathLabel();
      document.getElementById("data-base-config").textContent =
        JSON.stringify(body, null, 2);
      this.updateFieldStatus(body.model_path_exists);
      this.updateResourceDirStatus(body.resource_dir_exists);
      this.dirty = false;
      const parts = [];
      parts.push(body.model_path_exists ? "模型目录存在" : "⚠ 模型目录不存在");
      parts.push(body.resource_dir_exists ? "资源存放目录存在" : "⚠ 资源存放目录不存在");
      this.setFoot("已保存（" + parts.join(" · ") + "）", "ok");
    } catch (e) {
      this.setFoot("保存失败: " + e.message, "danger");
    } finally {
      saveBtn.disabled = false;
      if (reloadBtn) reloadBtn.disabled = false;
    }
  },

  refreshPathLabel() {
    // Pull the on-disk path from /api/config and render it in the page
    // header. Fire-and-forget; the field is purely informational.
    const el = document.getElementById("base-config-path");
    if (!el) return;
    fetch(API_PREFIX + "/config")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (d && d.path) el.textContent = "存储于 " + d.path; })
      .catch(() => { /* leave prior value in place */ });
  },

  setFoot(text, level) {
    const el = document.getElementById("bc-foot");
    if (!el) return;
    el.textContent = text || "";
    el.classList.remove("warn", "danger", "ok");
    if (level) el.classList.add(level);
  },

  // ------------------------------------------------------------------ //
  // 视频监控 (16-channel config table)                                  //
  // ------------------------------------------------------------------ //
  // Shares the same config.json with the 视频监控 page; round-trips
  // through GET/PUT /api/surveillance/config so any edit here shows
  // up there and vice versa on the next refresh.
  survConfigs: [],

  initSurveillance() {
    if (this.survInitialized) return;
    this.survInitialized = true;

    const tbody = document.getElementById("bc-surv-tbody");
    if (!tbody) return;
    tbody.innerHTML = "";
    for (let i = 1; i <= 16; i++) {
      const tr = document.createElement("tr");
      tr.dataset.channelId = String(i);

      const tdId = document.createElement("td");
      tdId.className = "bc-surv-col-id";
      tdId.textContent = String(i);
      tr.appendChild(tdId);

      // Name input
      const tdName = document.createElement("td");
      tdName.className = "bc-surv-col-name";
      const nameInput = document.createElement("input");
      nameInput.type = "text";
      nameInput.className = "bc-surv-input";
      nameInput.id = `bc-surv-name-${i}`;
      nameInput.placeholder = `通道 ${i}`;
      nameInput.spellcheck = false;
      tdName.appendChild(nameInput);
      tr.appendChild(tdName);

      // Source input + browse button
      const tdSource = document.createElement("td");
      tdSource.className = "bc-surv-col-source";
      const srcRow = document.createElement("div");
      srcRow.className = "bc-surv-source-row";
      const srcInput = document.createElement("input");
      srcInput.type = "text";
      srcInput.className = "bc-surv-input";
      srcInput.id = `bc-surv-source-${i}`;
      srcInput.placeholder = "网络流或本地文件/文件夹";
      srcInput.spellcheck = false;
      const browseBtn = document.createElement("button");
      browseBtn.type = "button";
      browseBtn.className = "vd-btn";
      browseBtn.textContent = "浏览…";
      browseBtn.addEventListener("click", () => {
        // Tell the file-picker modal which input to write the
        // selected path back into. The VdFsModal.pickSelectedFile
        // override (installed at module load) reads this target.
        VdFsModal._sourceTarget = srcInput;
        VdFsModal.open("");
      });
      srcRow.appendChild(srcInput);
      srcRow.appendChild(browseBtn);
      tdSource.appendChild(srcRow);
      tr.appendChild(tdSource);

      // Prompt input
      const tdPrompt = document.createElement("td");
      tdPrompt.className = "bc-surv-col-prompt";
      const promptInput = document.createElement("input");
      promptInput.type = "text";
      promptInput.className = "bc-surv-input";
      promptInput.id = `bc-surv-prompt-${i}`;
      promptInput.placeholder = "VLM 检测提示词";
      promptInput.spellcheck = false;
      tdPrompt.appendChild(promptInput);
      tr.appendChild(tdPrompt);

      // Detection size as a fixed dropdown (384 / 768) — see
      // ``app.api.surveillance`` for the validated choices. We
      // store the value in a single ``detect_size`` field per
      // row and reuse it for both ``detect_w`` and ``detect_h``
      // when we PUT it back.
      const tdSize = document.createElement("td");
      tdSize.className = "bc-surv-col-size";
      const sizeSelect = document.createElement("select");
      sizeSelect.className = "bc-surv-input";
      sizeSelect.id = `bc-surv-size-${i}`;
      [["384", "384x384（默认）"], ["768", "768x768"]].forEach(([v, label]) => {
        const opt = document.createElement("option");
        opt.value = v;
        opt.textContent = label;
        sizeSelect.appendChild(opt);
      });
      tdSize.appendChild(sizeSelect);
      tr.appendChild(tdSize);

      // Playback toggle
      const tdPlay = document.createElement("td");
      tdPlay.className = "bc-surv-col-toggle";
      tdPlay.appendChild(this._makeSurvToggle(`bc-surv-play-${i}`, "播放"));
      tr.appendChild(tdPlay);

      // Detection toggle
      const tdDet = document.createElement("td");
      tdDet.className = "bc-surv-col-toggle";
      tdDet.appendChild(this._makeSurvToggle(`bc-surv-det-${i}`, "检测"));
      tr.appendChild(tdDet);

      tbody.appendChild(tr);
    }

    const reloadBtn = document.getElementById("bc-surv-reload");
    if (reloadBtn) reloadBtn.addEventListener("click", () => this.loadSurveillance());

    const saveBtn = document.getElementById("bc-surv-save");
    if (saveBtn) saveBtn.addEventListener("click", () => this.saveSurveillance());

    const allOn = document.getElementById("bc-surv-all-on");
    if (allOn) allOn.addEventListener("click", () => {
      for (let i = 1; i <= 16; i++) {
        const el = document.getElementById(`bc-surv-play-${i}`);
        if (el) el.checked = true;
      }
    });
    const allOff = document.getElementById("bc-surv-all-off");
    if (allOff) allOff.addEventListener("click", () => {
      for (let i = 1; i <= 16; i++) {
        const el = document.getElementById(`bc-surv-play-${i}`);
        if (el) el.checked = false;
      }
    });
  },

  _makeSurvToggle(id, label) {
    const lbl = document.createElement("label");
    lbl.className = "bc-surv-toggle";
    lbl.title = label;
    const input = document.createElement("input");
    input.type = "checkbox";
    input.id = id;
    const track = document.createElement("span");
    track.className = "bc-surv-toggle-track";
    const thumb = document.createElement("span");
    thumb.className = "bc-surv-toggle-thumb";
    track.appendChild(thumb);
    lbl.appendChild(input);
    lbl.appendChild(track);
    return lbl;
  },

  async loadSurveillance() {
    try {
      const r = await fetch(API_PREFIX + "/surveillance/config");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      this.survConfigs = data.channels || [];
      this.renderSurveillance();
      this.setSurvFoot("", "");
    } catch (e) {
      this.setSurvFoot("加载失败: " + e.message, "danger");
    }
  },

  renderSurveillance() {
    const cfgs = this.survConfigs;
    for (let i = 0; i < 16; i++) {
      const cfg = cfgs[i] || {};
      const idx = i + 1;
      const setVal = (id, v) => {
        const el = document.getElementById(id);
        if (el) el.value = v == null ? "" : v;
      };
      const setChecked = (id, v) => {
        const el = document.getElementById(id);
        if (el) el.checked = !!v;
      };
      // Legacy configs may only have ``enabled`` (the pre-split
      // toggle); treat that as playback_enabled.
      setVal(`bc-surv-name-${idx}`, cfg.name || "");
      setVal(`bc-surv-source-${idx}`, cfg.source || "");
      setVal(`bc-surv-prompt-${idx}`, cfg.prompt || "");
      // detect_w / detect_h are kept equal by the backend; only the
      // size dropdown is rendered, so just clamp to the supported
      // set before assigning.
      const sz = (cfg.detect_w === 768 || cfg.detect_h === 768) ? "768" : "384";
      setVal(`bc-surv-size-${idx}`, sz);
      setChecked(`bc-surv-play-${idx}`, cfg.playback_enabled || cfg.enabled);
      setChecked(`bc-surv-det-${idx}`, cfg.detection_enabled);
    }
    const configured = cfgs.filter((c) => c && c.source).length;
    const summary = document.getElementById("bc-surv-summary");
    if (summary) {
      summary.textContent = `${configured}/16 路已配置视频源`;
    }
  },

  async saveSurveillance() {
    const channels = [];
    for (let i = 1; i <= 16; i++) {
      const get = (id) => {
        const el = document.getElementById(id);
        return el ? el.value : "";
      };
      const getChecked = (id) => {
        const el = document.getElementById(id);
        return el ? !!el.checked : false;
      };
      // Detection size: single dropdown, same value in both
      // detect_w and detect_h (the backend enforces square + the
      // choice set {384, 768}).
      const rawSize = get(`bc-surv-size-${i}`);
      const size = (rawSize === "768") ? 768 : 384;
      channels.push({
        name: (get(`bc-surv-name-${i}`) || "").trim(),
        source: (get(`bc-surv-source-${i}`) || "").trim(),
        prompt: get(`bc-surv-prompt-${i}`) || "",
        playback_enabled: getChecked(`bc-surv-play-${i}`),
        detection_enabled: getChecked(`bc-surv-det-${i}`),
        detect_w: size,
        detect_h: size,
      });
    }
    const saveBtn = document.getElementById("bc-surv-save");
    const reloadBtn = document.getElementById("bc-surv-reload");
    if (saveBtn) saveBtn.disabled = true;
    if (reloadBtn) reloadBtn.disabled = true;
    this.setSurvFoot("正在保存 16 路配置…", "warn");
    try {
      const r = await fetch(API_PREFIX + "/surveillance/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ channels }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) {
        this.setSurvFoot("保存失败: " + (body.detail || r.status), "danger");
        return;
      }
      this.survConfigs = body.channels || channels;
      this.renderSurveillance();
      this.setSurvFoot("已保存 16 路配置", "ok");
    } catch (e) {
      this.setSurvFoot("保存失败: " + e.message, "danger");
    } finally {
      if (saveBtn) saveBtn.disabled = false;
      if (reloadBtn) reloadBtn.disabled = false;
    }
  },

  setSurvFoot(text, level) {
    const el = document.getElementById("bc-surv-foot");
    if (!el) return;
    el.textContent = text || "";
    el.classList.remove("warn", "danger", "ok");
    if (level) el.classList.add(level);
  },
};

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

  if (name === "base-config") {
    Base.init();
    await Base.load();
    await Base.loadSurveillance();
    return;
  }

  if (name === "tts") {
TTS.init();
    await TTS.refresh();
    // Open the WS proactively so the page stays live even before the
    // first click on "开始生成". The server only pushes on state change,
    // so this is cheap.
    TTS.ensureWs();
    return;
  }

  if (name === "surveillance") {
    Surveillance.init();
    await Surveillance.refresh();
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

/* ------------------------------------------------------------------ *
 *  power menu: small icon button at the bottom-left of the sidebar.   *
 *  Click opens a popover with two destructive actions (shutdown,    *
 *  reboot). Each action requires a JS confirm() and then POSTs to    *
 *  /api/power. The backend runs the host binary and either returns   *
 *  success or surfaces the OS error message.                        *
 * ------------------------------------------------------------------ */

const PowerMenu = {
  btn: null,
  popover: null,
  status: null,
  busy: false,

  ACTION_LABELS: {
    shutdown:   { verb: "关机", pending: "正在关机…", confirm: "确定要立即关机吗？届时将无法访问该服务。" },
    reboot:     { verb: "重启", pending: "正在重启…", confirm: "确定要立即重启吗？届时连接会短暂中断。" },
  },

  init() {
    this.btn = document.getElementById("power-btn");
    this.popover = document.getElementById("power-popover");
    this.status = document.getElementById("power-popover-status");
    if (!this.btn || !this.popover) return;

    this.btn.addEventListener("click", (e) => {
      e.stopPropagation();
      this.toggle();
    });

    document.getElementById("power-popover-close")
      .addEventListener("click", () => this.close());

    this.popover.querySelectorAll(".power-popover-action").forEach((b) => {
      b.addEventListener("click", () => {
        const action = b.dataset.action;
        if (action) this.runAction(action);
      });
    });

    document.addEventListener("mousedown", (e) => {
      if (this.popover.classList.contains("hidden")) return;
      if (this.popover.contains(e.target) || this.btn.contains(e.target)) return;
      this.close();
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !this.popover.classList.contains("hidden")) {
        this.close();
      }
    });
  },

  isOpen() { return !this.popover.classList.contains("hidden"); },

  open() {
    this.popover.classList.remove("hidden");
    this.btn.setAttribute("aria-expanded", "true");
    this.setStatus("点击操作前请确认", "");
  },

  close() {
    this.popover.classList.add("hidden");
    this.btn.setAttribute("aria-expanded", "false");
  },

  toggle() {
    if (this.isOpen()) this.close(); else this.open();
  },

  setStatus(text, level) {
    if (!this.status) return;
    this.status.textContent = text || "";
    this.status.classList.remove("warn", "danger", "ok");
    if (level) this.status.classList.add(level);
  },

  setButtonsDisabled(disabled) {
    this.popover.querySelectorAll(".power-popover-action").forEach((b) => {
      b.disabled = disabled;
    });
  },

  async runAction(action) {
    if (this.busy) return;
    const meta = this.ACTION_LABELS[action];
    if (!meta) return;
    if (!window.confirm(meta.confirm)) return;

    this.busy = true;
    this.setButtonsDisabled(true);
    this.setStatus(meta.pending, "warn");

    try {
      const r = await fetch(API_PREFIX + "/power", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action }),
      });
      const text = await r.text();
      let body;
      try { body = JSON.parse(text); } catch { body = { detail: text }; }
      if (!r.ok) {
        const msg = body.detail || body.message || ("HTTP " + r.status);
        this.setStatus(`${meta.verb} 失败: ${msg}`, "danger");
        return;
      }
      this.setStatus(body.message || `${meta.verb} 已执行`, "ok");
      setTimeout(() => this.close(), 1500);
    } catch (err) {
      this.setStatus(`${meta.verb} 请求失败: ${err.message}`, "danger");
    } finally {
      this.busy = false;
      this.setButtonsDisabled(false);
    }
  },
};

PowerMenu.init();

/* ------------------------------------------------------------------ *
 *  config menu: gear icon next to the power button. Opens a popover   *
 *  with two actions — export (download the current config.json) and   *
 *  import (upload a new config.json to overwrite). The hidden file    *
 *  input is clicked programmatically when the user picks "导入配置".  *
 * ------------------------------------------------------------------ */

const ConfigMenu = {
  btn: null,
  popover: null,
  closeBtn: null,
  pathEl: null,
  statusEl: null,
  fileInput: null,
  busy: false,
  configPath: "--",

  init() {
    this.btn = document.getElementById("config-btn");
    this.popover = document.getElementById("config-popover");
    this.closeBtn = document.getElementById("config-popover-close");
    this.pathEl = document.getElementById("config-popover-path");
    this.statusEl = document.getElementById("config-popover-status");
    this.fileInput = document.getElementById("config-file-input");
    if (!this.btn || !this.popover) return;

    this.btn.addEventListener("click", (e) => {
      e.stopPropagation();
      this.toggle();
    });

    if (this.closeBtn) {
      this.closeBtn.addEventListener("click", () => this.close());
    }

    this.popover.querySelectorAll(".config-popover-action").forEach((b) => {
      b.addEventListener("click", () => {
        const action = b.dataset.configAction;
        if (action) this.runAction(action);
      });
    });

    if (this.fileInput) {
      this.fileInput.addEventListener("change", (e) => {
        const file = e.target.files && e.target.files[0];
        // Always reset so the user can re-pick the same file.
        e.target.value = "";
        if (file) this.importFile(file);
      });
    }

    document.addEventListener("mousedown", (e) => {
      if (this.popover.classList.contains("hidden")) return;
      if (this.popover.contains(e.target) || this.btn.contains(e.target)) return;
      this.close();
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !this.popover.classList.contains("hidden")) {
        this.close();
      }
    });

    this.refreshPath();
  },

  isOpen() { return !this.popover.classList.contains("hidden"); },

  open() {
    this.refreshPath();
    this.popover.classList.remove("hidden");
    this.btn.setAttribute("aria-expanded", "true");
  },

  close() {
    this.popover.classList.add("hidden");
    this.btn.setAttribute("aria-expanded", "false");
  },

  toggle() {
    if (this.isOpen()) this.close(); else this.open();
  },

  setStatus(text, level) {
    if (!this.statusEl) return;
    this.statusEl.textContent = text || "";
    this.statusEl.classList.remove("warn", "danger", "ok");
    if (level) this.statusEl.classList.add(level);
  },

  setActionsDisabled(disabled) {
    this.popover.querySelectorAll(".config-popover-action").forEach((b) => {
      b.disabled = disabled;
    });
  },

  async refreshPath() {
    if (!this.pathEl) return;
    try {
      const r = await fetch(API_PREFIX + "/config");
      if (!r.ok) {
        this.pathEl.textContent = "(无法读取配置路径)";
        return;
      }
      const data = await r.json();
      this.configPath = (data && data.path) || "--";
      this.pathEl.textContent = this.configPath;
      this.pathEl.title = this.configPath;
    } catch (_) {
      this.pathEl.textContent = "(无法读取配置路径)";
    }
  },

  async runAction(action) {
    if (this.busy) return;
    if (action === "export") {
      this.exportNow();
    } else if (action === "import") {
      if (this.fileInput) this.fileInput.click();
    }
  },

  async exportNow() {
    this.busy = true;
    this.setActionsDisabled(true);
    this.setStatus("正在导出…", "warn");
    try {
      const r = await fetch(API_PREFIX + "/config/export");
      if (!r.ok) {
        const detail = await r.text().catch(() => "");
        this.setStatus(`导出失败: ${detail || r.status}`, "danger");
        return;
      }
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "config.json";
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      this.setStatus("已导出 config.json", "ok");
    } catch (err) {
      this.setStatus(`导出失败: ${err.message}`, "danger");
    } finally {
      this.busy = false;
      this.setActionsDisabled(false);
    }
  },

  async importFile(file) {
    this.busy = true;
    this.setActionsDisabled(true);
    this.setStatus(`正在导入 ${file.name}…`, "warn");
    try {
      const fd = new FormData();
      fd.append("file", file, file.name);
      const r = await fetch(API_PREFIX + "/config/import", {
        method: "POST",
        body: fd,
      });
      const text = await r.text();
      let body;
      try { body = JSON.parse(text); } catch { body = { detail: text }; }
      if (!r.ok) {
        const msg = body.detail || body.message || ("HTTP " + r.status);
        this.setStatus(`导入失败: ${msg}`, "danger");
        return;
      }
      this.setStatus("已导入并覆盖当前配置", "ok");
      this.refreshPath();
    } catch (err) {
      this.setStatus(`导入失败: ${err.message}`, "danger");
    } finally {
      this.busy = false;
      this.setActionsDisabled(false);
    }
  },
};

ConfigMenu.init();

/* ------------------------------------------------------------------ *
 *  coprocessor menu: third sidebar-foot button. Opens a popover with   *
 *  one action — "重启所有协处理器" — which POSTs to /api/coprocessor/ *
 *  restart and runs the same steps as /userdata/samples/reset_182x.sh *
 *  on the host (four NPU hardware resets + rknn3_startup restart).    *
 * ------------------------------------------------------------------ */

const CoprocessorMenu = {
  btn: null,
  popover: null,
  status: null,
  busy: false,
  ws: null,

  ACTION_LABELS: {
    restart: {
      verb:   "重启协处理器",
      pending: "正在重启所有协处理器…",
      confirm: "确定要立即重启所有协处理器吗？期间 NPU 推理将不可用。",
    },
  },

  init() {
    this.btn = document.getElementById("coprocessor-btn");
    this.popover = document.getElementById("coprocessor-popover");
    this.status = document.getElementById("coprocessor-popover-foot");
    if (!this.btn || !this.popover) return;

    this.btn.addEventListener("click", (e) => {
      e.stopPropagation();
      this.toggle();
    });

    document.getElementById("coprocessor-popover-close")
      .addEventListener("click", () => this.close());

    this.popover.querySelectorAll(".coprocessor-popover-action").forEach((b) => {
      b.addEventListener("click", () => {
        const action = b.dataset.coprocessorAction;
        if (action) this.runAction(action);
      });
    });

    document.addEventListener("mousedown", (e) => {
      if (this.popover.classList.contains("hidden")) return;
      if (this.popover.contains(e.target) || this.btn.contains(e.target)) return;
      this.close();
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !this.popover.classList.contains("hidden")) {
        this.close();
      }
    });
  },

  isOpen() { return !this.popover.classList.contains("hidden"); },

  open() {
    this.popover.classList.remove("hidden");
    this.btn.setAttribute("aria-expanded", "true");
    if (!this.busy) this.setStatus("点击操作前请确认", "");
  },

  close() {
    this.popover.classList.add("hidden");
    this.btn.setAttribute("aria-expanded", "false");
  },

  toggle() {
    if (this.isOpen()) this.close(); else this.open();
  },

  setStatus(text, level) {
    if (!this.status) return;
    this.status.textContent = text || "";
    this.status.classList.remove("warn", "danger", "ok");
    if (level) this.status.classList.add(level);
  },

  appendStatus(line) {
    if (!this.status) return;
    const cur = this.status.textContent || "";
    this.status.textContent = cur ? (cur + "\n" + line) : line;
    // Keep the latest line visible while the user is watching.
    this.status.scrollTop = this.status.scrollHeight;
  },

  setActionsDisabled(disabled) {
    this.popover.querySelectorAll(".coprocessor-popover-action").forEach((b) => {
      b.disabled = disabled;
    });
  },

  async runAction(action) {
    if (this.busy) return;
    const meta = this.ACTION_LABELS[action];
    if (!meta) return;
    if (!window.confirm(meta.confirm)) return;

    this.busy = true;
    this.setActionsDisabled(true);
    this.setStatus(meta.pending, "warn");

    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${window.location.host}${API_PREFIX}/coprocessor/ws`;
    let ws;
    try {
      ws = new WebSocket(url);
    } catch (err) {
      this.setStatus(`${meta.verb} 请求失败: ${err.message}`, "danger");
      this.busy = false;
      this.setActionsDisabled(false);
      return;
    }
    this.ws = ws;

    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === "step_start") {
        this.setStatus(msg.message || `步骤 ${msg.index}/${msg.total}`, "warn");
      } else if (msg.type === "step_end") {
        const tag = `[${msg.index}/${msg.total}] ${(msg.command || []).join(" ")}`;
        const out = (msg.stdout || "").trim();
        const err = (msg.stderr || "").trim();
        const accepted = msg.accepted !== false;
        this.appendStatus(
          `${tag} 退出码=${msg.exit} (${msg.duration_ms}ms)` +
          (out ? `\n  stdout: ${out}` : "") +
          (err ? `\n  stderr: ${err}` : "")
        );
        this.status.classList.remove("warn", "ok", "danger");
        if (!accepted) {
          this.status.classList.add("danger");
        } else if (msg.exit !== 0) {
          this.status.classList.add("warn");
        } else {
          this.status.classList.add("ok");
        }
      } else if (msg.type === "done") {
        if (msg.ok) {
          this.setStatus(
            (this.status.textContent || "") +
            `\n✓ ${meta.verb} 已完成`,
            "ok"
          );
        } else {
          this.setStatus(
            (this.status.textContent || "") +
            `\n✗ ${meta.verb} 在第 ${msg.failed_step} 步失败`,
            "danger"
          );
        }
      } else if (msg.type === "error") {
        this.setStatus(
          (this.status.textContent || "") +
          `\n✗ ${meta.verb} 服务端错误: ${msg.detail || ""}`,
          "danger"
        );
      }
    };

    ws.onerror = () => {
      this.appendStatus(`✗ ${meta.verb} 连接出错`);
      this.status.classList.remove("warn", "ok");
      this.status.classList.add("danger");
    };

    ws.onclose = () => {
      this.ws = null;
      this.busy = false;
      this.setActionsDisabled(false);
    };
  },
};

CoprocessorMenu.init();

/* ---------------------------------------------------------------- *
 * TTS (语音生成) page                                              *
 * ---------------------------------------------------------------- */
const TTS = {
  initialized: false,
  ws: null,
  backoff: 1.0,
  lastSnap: null,

  init() {
    if (this.initialized) return;
    this.initialized = true;

    const textEl = document.getElementById("tts-text");
    const refEl = document.getElementById("tts-ref-speaker");
    const langEl = document.getElementById("tts-language");
    const volEl = document.getElementById("tts-volume");
    const volValEl = document.getElementById("tts-volume-value");
    const startBtn = document.getElementById("tts-start");
    const stopBtn = document.getElementById("tts-stop");
    const refreshBtn = document.getElementById("tts-refresh");
    const audioEl = document.getElementById("tts-audio");

    if (volEl && volValEl) {
      volEl.addEventListener("input", () => {
        volValEl.textContent = volEl.value + "%";
      });
    }
    if (audioEl) {
      audioEl.volume = 1.0;
      audioEl.addEventListener("error", () =>
        this.setStatus("浏览器播放失败 (autoplay 可能被拦截)", true));
    }
    document.querySelectorAll('input[name="tts-play-to"]').forEach((el) => {
      el.addEventListener("change", () => this._showAudioRowIfNeeded());
    });

    if (refreshBtn) refreshBtn.addEventListener("click", () => this.refresh());
    if (startBtn)   startBtn.addEventListener("click", () => this.start());
    if (stopBtn)    stopBtn.addEventListener("click", () => this.stop());
    if (textEl) {
      textEl.addEventListener("keydown", (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
          e.preventDefault();
          this.start();
        }
      });
    }
  },

  async refresh() {
    try {
      const r = await fetch(API_PREFIX + "/tts");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const snap = await r.json();
      this.lastSnap = snap;
      this.render(snap);
      this.refreshHistory();
    } catch (err) {
      this.setStatus("加载失败: " + err.message, true);
    }
  },

  async refreshHistory() {
    try {
      const r = await fetch(API_PREFIX + "/tts/history");
      if (!r.ok) return;
      const data = await r.json();
      this.renderHistory(data.items || []);
    } catch (_) {
      // history is best-effort
    }
  },

  render(snap) {
    const banner = document.getElementById("tts-unavailable");
    const reasonEl = document.getElementById("tts-unavailable-reason");
    const startBtn = document.getElementById("tts-start");
    const stopBtn = document.getElementById("tts-stop");
    const metaEl = document.getElementById("tts-meta");
    const lineEl = document.getElementById("tts-status-line");
    const dataEl = document.getElementById("data-tts");

    if (dataEl) dataEl.textContent = JSON.stringify(snap, null, 2);

    const isBusy = snap.busy === true || snap.running === true || snap.state === "loading" || snap.state === "running";
    const isRunning = snap.running === true;

    if (banner && reasonEl) {
      if (snap.available === false) {
        banner.classList.remove("hidden");
        reasonEl.textContent = snap.init_error || "upstream 模块缺失";
      } else {
        banner.classList.add("hidden");
      }
    }

    if (startBtn) {
      startBtn.disabled = isBusy || snap.available === false;
    }
    if (stopBtn) {
      stopBtn.disabled = !isBusy;
    }

    if (lineEl) {
      lineEl.textContent = `状态: ${snap.state}` + (snap.error ? ` · ${snap.error}` : "");
    }

    if (metaEl) {
      metaEl.textContent = this.formatMeta(snap);
    }

    // After the run finishes, expose the saved WAV via the <audio>
    // element so the user can re-play the synthesis without
    // re-running the model. Live chunks already played through the
    // AudioContext; this is the post-run scrubber.
    if (snap.output_url && (snap.state === "done" || snap.state === "stopped")) {
      this._feedBrowserAudio(snap.output_url);
    }
  },

  _feedBrowserAudio(url) {
    const audioEl = this._audioEl();
    if (!audioEl) return;
    if (!this._wantsBrowser()) {
      audioEl.classList.add("hidden");
      return;
    }
    // Cache-bust so a fresh run doesn't replay the previous bytes.
    const bust = (url.indexOf("?") >= 0 ? "&" : "?") + "t=" + Date.now();
    audioEl.src = url + bust;
    audioEl.classList.remove("hidden");
  },

  formatMeta(snap) {
    if (!snap.available) return "语音生成不可用，请检查上游 Qwen3-TTS 模型路径 / rknn3 驱动";
    if (snap.state === "idle")  return `模型: ${snap.model_dir}`;
    if (snap.state === "loading") return "正在加载模型…";
    if (snap.state === "running") {
      const f = snap.frames || 0;
      const sec = (snap.audio_sec || 0).toFixed(2);
      const wall = (snap.elapsed || 0).toFixed(2);
      return `推理中 · 已生成 ${f} 帧 / ${sec}s · 用时 ${wall}s`;
    }
    if (snap.state === "done") {
      return `已完成 · ${snap.frames} 帧 / ${(snap.audio_sec || 0).toFixed(2)}s · 用时 ${(snap.elapsed || 0).toFixed(2)}s`;
    }
    if (snap.state === "stopped") {
      return `已停止 · 输出: ${snap.output || "--"}`;
    }
    if (snap.state === "error") return `错误: ${snap.error}`;
    return `状态: ${snap.state}`;
  },

  async start() {
    const textEl = document.getElementById("tts-text");
    const refEl = document.getElementById("tts-ref-speaker");
    const langEl = document.getElementById("tts-language");
    const volEl = document.getElementById("tts-volume");
    const audioEl = this._audioEl();

    const text = (textEl ? textEl.value : "").trim();
    if (!text) {
      this.setStatus("请输入要合成的文本", true);
      return;
    }
    const playTo = this._selectedPlayTo();

    if (audioEl) {
      audioEl.pause();
      audioEl.removeAttribute("src");
      audioEl.load();
      audioEl.classList.add("hidden");
    }

    // ``start()`` may be entered more than once per run (double-click,
    // Ctrl+Enter, onopen re-subscription). Don't reset audio state
    // here — only ``_onAudioStreamEnd`` resets it when the run
    // truly finishes.
    this.audioScheduledUntil = 0;
    this.audioPushedSec = 0;
    this._showAudioRowIfNeeded();

    this.setStatus(
      playTo === "browser" ? "提交中…（将边生成边播浏览器）" :
      playTo === "both"    ? "提交中…（设备 + 浏览器同时边播）" :
                             "提交中…（仅设备播放）",
      false,
    );

    // Eagerly resume the AudioContext under this user gesture. Chrome
    // may have suspended the audio thread while the user was typing,
    // and waking it up at audio_start time costs 1-3 s of cold-start
    // tax. Doing it here, synchronously, runs under the gesture.
    if (this._wantsBrowser()) {
      const ctx = this._ensureAudioCtx();
      if (ctx) {
        try {
          if (ctx.state !== "running") await ctx.resume();
        } catch (_) {}
        if (ctx.state === "running") this.audioReady = true;
      }
    }

    const payload = {
      text,
      ref_speaker: refEl ? refEl.value : "girl_base",
      language: langEl ? langEl.value : "auto",
      volume: volEl ? Number(volEl.value) / 100 : 0.1,
      play_to: playTo,
    };
    try {
      const r = await fetch(API_PREFIX + "/tts/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) {
        this.setStatus("提交失败: " + (body.detail || r.statusText), true);
        return;
      }
      this.lastSnap = body;
      this.render(body);
      this.ensureWs();
      // onopen doesn't fire when the WS is already OPEN (server keeps
      // it alive between runs) — resubscribe explicitly so the server
      // recreates the audio pump for this run.
      if (this._wantsBrowser() && this.audioReady) {
        this._subscribeAudio();
      }
    } catch (err) {
      this.setStatus("提交失败: " + err.message, true);
    }
  },

  async stop() {
    try {
      const r = await fetch(API_PREFIX + "/tts/stop", { method: "POST" });
      const body = await r.json().catch(() => ({}));
      this.lastSnap = body;
      this.render(body);
    } catch (err) {
      this.setStatus("停止失败: " + err.message, true);
    }
  },

  setStatus(text, isError) {
    const lineEl = document.getElementById("tts-status-line");
    if (lineEl) {
      lineEl.textContent = text;
      lineEl.style.color = isError ? "#dc2626" : "";
    }
  },

  renderHistory(items) {
    const listEl = document.getElementById("tts-history-list");
    const countEl = document.getElementById("tts-history-count");
    if (!listEl) return;
    listEl.innerHTML = "";
    if (!items.length) {
      const li = document.createElement("li");
      li.className = "vd-event-empty";
      li.textContent = "尚无生成记录，启动合成后结果显示在这里。";
      listEl.appendChild(li);
    } else {
      items.slice().reverse().forEach((it) => {
        const li = document.createElement("li");
        li.className = "vd-event";

        const when = new Date((it.ts || 0) * 1000);
        const whenStr = when.toLocaleString();
        const url = it.output_url || "";
        const filename = (it.output || "").split("/").filter(Boolean).pop()
          || "tts.wav";

        // textContent throughout so path / speaker never go through
        // innerHTML (XSS). Action buttons sit at the right edge of this
        // row via .vd-event-action { margin-left: auto } in CSS.
        const meta = document.createElement("div");
        meta.className = "vd-event-meta";
        [
          whenStr,
          it.speaker || "--",
          (it.audio_sec || 0).toFixed(2) + "s",
          (it.frames || 0) + " 帧",
        ].forEach((txt) => {
          const sp = document.createElement("span");
          sp.className = "tag";
          sp.textContent = txt;
          meta.appendChild(sp);
        });
        const path = document.createElement("span");
        path.className = "vd-event-path";
        path.textContent = it.output || "";
        path.title = it.output || "";
        meta.appendChild(path);

        if (url) {
          const playBtn = document.createElement("button");
          playBtn.type = "button";
          playBtn.className = "vd-btn vd-btn-mini vd-event-action";
          playBtn.textContent = "▶ 试听";
          meta.appendChild(playBtn);

          const player = document.createElement("audio");
          player.className = "tts-history-player hidden";
          player.controls = true;
          player.preload = "none";
          player.src = url;
          meta.appendChild(player);

          const dlBtn = document.createElement("a");
          dlBtn.className = "vd-btn vd-btn-mini vd-event-action";
          dlBtn.href = url;
          dlBtn.download = filename;
          dlBtn.textContent = "⬇ 下载";
          dlBtn.title = "下载 " + filename;
          meta.appendChild(dlBtn);

          playBtn.addEventListener("click", async () => {
            const wasHidden = player.classList.contains("hidden");
            document.querySelectorAll(
              ".tts-history-player:not(.hidden)"
            ).forEach((other) => {
              if (other !== player) {
                try { other.pause(); } catch (_) {}
              }
            });
            const shared = this._audioEl();
            if (shared && !shared.classList.contains("hidden")) {
              try { shared.pause(); } catch (_) {}
            }
            if (wasHidden) {
              player.classList.remove("hidden");
              playBtn.textContent = "■ 停止";
              try {
                await player.play();
              } catch (e) {
                this.setStatus("试听失败: " + e.message, true);
              }
            } else {
              try { player.pause(); } catch (_) {}
              player.classList.add("hidden");
              playBtn.textContent = "▶ 试听";
            }
          });
          player.addEventListener("ended", () => {
            playBtn.textContent = "▶ 试听";
            player.classList.add("hidden");
          });
        }

        const text = document.createElement("div");
        text.className = "vd-event-text";
        text.textContent = it.text || "";

        li.appendChild(meta);
        li.appendChild(text);

        listEl.appendChild(li);
      });
    }
    if (countEl) countEl.textContent = items.length + " 条";
  },

  /* ------------------------------------------------------------------ *
   * WebSocket status stream — auto-reconnect with backoff.
   * ------------------------------------------------------------------ */
  ensureWs() {
    if (this.ws && this.ws.readyState <= WebSocket.OPEN) return;
    try {
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      const url = `${proto}//${location.host}${API_PREFIX}/tts/ws`;
      this.ws = new WebSocket(url);
    } catch (err) {
      this.setStatus("WS 创建失败: " + err.message, true);
      return;
    }
    this.ws.binaryType = "arraybuffer";
    this.ws.onmessage = (ev) => {
      this.audioFramesTotal = (this.audioFramesTotal || 0) + 1;
      // Some browsers deliver text frames as Blob instead of string
      // (default for binaryType="blob"). Read it as text first.
      const data0 = ev.data;
      if (data0 instanceof Blob) {
        data0.text().then((t) => this._handleTextMessage(t, ev));
        return;
      }
      if (data0 instanceof ArrayBuffer) {
        this._handleBinaryChunk(data0);
        return;
      }
      // Assume string.
      this._handleTextMessage(data0, ev);
    };
    this.ws.onopen = () => {
      // Per-connection subscription: the server resets ``sub_id`` at
      // end-of-run, so re-subscribing on every WS open is cheap and
      // survives WS bounces mid-run.
      if (this._wantsBrowser() && this.audioReady) {
        this._subscribeAudio();
      } else if (this.pendingAudioSubscribe) {
        this._subscribeAudio();
      }
      this._armPing();
    };
    this.ws.onclose = () => {
      this.ws = null;
      if (this.audioPingTimer) {
        clearInterval(this.audioPingTimer);
        this.audioPingTimer = null;
      }
      // Reconnect only while a run is in flight; otherwise wait until
      // the user clicks "start" again to open a fresh connection.
      if (this.lastSnap && (this.lastSnap.busy || this.lastSnap.running)) {
        const delay = Math.min(this.backoff, 8.0) * 1000;
        this.backoff = Math.min(this.backoff * 1.6, 8.0);
        setTimeout(() => this.ensureWs(), delay);
      }
    };
    this.ws.onerror = () => {
      try { this.ws && this.ws.close(); } catch (_) {}
    };
    this.backoff = 1.0;
  },

  /* ------------------------------------------------------------------ *
   * Browser-side audio playback (WebAudio, streaming chunks)
   * ------------------------------------------------------------------ */
  audioCtx: null,
  audioReady: false,         // AudioContext created + resumed
  audioActive: false,        // current stream is feeding the context
  audioSampleRate: 24000,
  audioChannels: 1,
  audioScheduledUntil: 0,    // monotonic ctx time for back-to-back chunks
  audioPushedSec: 0,
  /* True when subscribe_audio was sent but the WS wasn't OPEN yet;
   * ``onopen`` re-issues subscribe when the WS finishes opening. */
  pendingAudioSubscribe: false,
  audioPingTimer: null,

  async _handleTextMessage(text) {
    let snap;
    try { snap = JSON.parse(text); } catch (_) { return; }
    if (!snap) return;
    if (snap.type === "audio_start") {
      this._onAudioStreamStart(snap.sample_rate, snap.channels);
      return;
    }
    if (snap.type === "audio_end") {
      this._onAudioStreamEnd();
      return;
    }
    // Drop WS-heartbeat acks; otherwise the server's pong overwrites
    // the snapshot in #data-tts every 15 s.
    if (snap.type === "pong") return;
    this.lastSnap = snap;
    this.render(snap);
    if (snap.state === "done" || snap.state === "stopped" || snap.state === "error") {
      this.refreshHistory();
    }
  },

  _ensureAudioCtx() {
    if (this.audioCtx) return this.audioCtx;
    const Ctor = window.AudioContext || window.webkitAudioContext;
    if (!Ctor) return null;
    this.audioCtx = new Ctor();
    // Keep the audio thread spinning. Chrome parks it when no source
    // is active, and a 1-3s cold-start tax hits the first real chunk
    // after a long idle. A 0-gain oscillator runs until ctx.close()
    // and is effectively free in WebAudio.
    try {
      const osc = this.audioCtx.createOscillator();
      const gain = this.audioCtx.createGain();
      gain.gain.value = 0;
      osc.connect(gain).connect(this.audioCtx.destination);
      osc.start();
    } catch (_) {}
    return this.audioCtx;
  },

  _subscribeAudio() {
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      this.pendingAudioSubscribe = true;
      return false;
    }
    try {
      ws.send(JSON.stringify({ type: "subscribe_audio" }));
      this.pendingAudioSubscribe = false;
      if (!this.audioActive) {
        this._setAudioChip("订阅已发送", "idle");
      }
      return true;
    } catch (err) {
      this._setAudioChip("订阅失败: " + err.message, "error");
      return false;
    }
  },

  _unsubscribeAudio() {
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    try { ws.send(JSON.stringify({ type: "unsubscribe_audio" })); }
    catch (_) {}
  },

  /* WS-level keep-alive ping; some proxies and NATs reap idle WS
   * connections after ~30-60 s of silence. */
  _armPing() {
    if (this.audioPingTimer) clearInterval(this.audioPingTimer);
    this.audioPingTimer = setInterval(() => {
      const ws = this.ws;
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      try { ws.send(JSON.stringify({ type: "ping" })); } catch (_) {}
    }, 15000);
  },

  async _onAudioStreamStart(sampleRate, channels) {
    const ctx = this._ensureAudioCtx();
    if (!ctx || !this.audioReady) {
      this._setAudioChip("AudioContext 未就绪", "error");
      return;
    }
    // Defensive resume: state can flip back to "suspended" after long
    // idle. If still not running, skip and surface the error.
    if (ctx.state !== "running") {
      try { await ctx.resume(); } catch (_) {}
      if (ctx.state !== "running") {
        this._setAudioChip("AudioContext 已挂起", "error");
        return;
      }
    }
    this.audioSampleRate = sampleRate || 24000;
    this.audioChannels = channels || 1;
    this.audioActive = true;
    // No pre-roll: the keep-alive oscillator + eager resume() in
    // start() already paid the AudioContext cold-start tax.
    this.audioScheduledUntil = ctx.currentTime;
    this.audioPushedSec = 0;
    this._setAudioChip("浏览器音频流开始", "live");
  },

  _onAudioStreamEnd() {
    if (!this.audioActive) {
      this._setAudioChip("服务端 audio_end · 未播放", "idle");
      return;
    }
    const sec = this.audioPushedSec;
    this.audioActive = false;
    this.audioScheduledUntil = 0;
    this.audioPushedSec = 0;
    this._setAudioChip(`浏览器音频流结束 · 已播 ${sec.toFixed(2)}s`, "idle");
  },

  _handleBinaryChunk(arrayBuf) {
    if (!this.audioReady) {
      this._setAudioChip("已收 chunk · 请先点 ▶ 启用浏览器音频", "error");
      return;
    }
    if (!this.audioActive) return;

    // 12-byte header (sr:u32, ch:u32, n:u32 little-endian) + float32 LE samples.
    const view = new DataView(arrayBuf);
    if (arrayBuf.byteLength < 12) return;
    const sr = view.getUint32(0, true);
    const ch = view.getUint32(4, true);
    const n  = view.getUint32(8, true);
    const sampleCount = sr === this.audioSampleRate
      ? n
      : Math.floor(n * (this.audioSampleRate / (sr || this.audioSampleRate)));
    if (sampleCount <= 0) return;
    const expectedBytes = sampleCount * ch * 4;
    if (arrayBuf.byteLength < 12 + expectedBytes) return;

    const ctx = this.audioCtx;
    if (!ctx) return;

    const f32 = new Float32Array(arrayBuf.slice(12, 12 + expectedBytes));
    const buffer = ctx.createBuffer(ch, sampleCount, this.audioSampleRate);
    // qwen3 is always mono; the ch>0 branch is here for future use.
    for (let c = 0; c < ch; c++) {
      const data = buffer.getChannelData(c);
      if (ch === 1) data.set(f32);
      else for (let i = 0; i < sampleCount; i++) data[i] = f32[i * ch + c];
    }

    const src = ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(ctx.destination);
    // Back-to-back when the model keeps up; play immediately when
    // the model lags (no 20ms lookahead).
    const now = ctx.currentTime;
    const startAt = this.audioScheduledUntil > now
                    ? this.audioScheduledUntil : now;
    src.start(startAt);
    this.audioScheduledUntil = startAt + buffer.duration;
    this.audioPushedSec += buffer.duration;
    this._setAudioChip(`播放 ${this.audioPushedSec.toFixed(2)}s`, "live");
  },

  _setAudioChip(text, state) {
    const chip = document.getElementById("tts-audio-chip");
    if (!chip) return;
    chip.textContent = text;
    if (state) chip.dataset.state = state;
  },

  _showAudioRowIfNeeded() {
    const row = document.getElementById("tts-audio-row");
    if (!row) return;
    row.classList.toggle("hidden", !this._wantsBrowser());
  },

  _audioEl() {
    return document.getElementById("tts-audio");
  },

  _selectedPlayTo() {
    const sel = document.querySelector('input[name="tts-play-to"]:checked');
    return sel ? sel.value : "device";
  },

  _wantsBrowser() {
    const v = this._selectedPlayTo();
    return v === "browser" || v === "both";
  },
};

TTS.init();

/* The popovers share the same sidebar-foot anchor and would stack on
 * top of each other if more than one were visible. For each pair, wrap
 * the left-hand menu's .open() so it closes the right-hand one before
 * showing itself. */
function _wrapOpenToCloseOther(menu, other) {
  const orig = menu.open.bind(menu);
  menu.open = function () {
    if (other && typeof other.close === "function") other.close();
    orig();
  };
}
const _menus = [PowerMenu, ConfigMenu, CoprocessorMenu];
for (const a of _menus) {
  for (const b of _menus) {
    if (a !== b) _wrapOpenToCloseOther(a, b);
  }
}

/* Surface a top-of-page warning when the service isn't running as root
 * — power actions (shutdown / reboot) need root and will fail silently
 * or noisily otherwise. */
async function showRootWarning() {
  const el = document.getElementById("root-warning");
  if (!el) return;
  try {
    const r = await fetch(API_PREFIX + "/power");
    if (!r.ok) return;
    const data = await r.json();
    if (data && data.is_root === false) {
      el.classList.remove("hidden");
    }
  } catch (_) { /* network down at boot — skip silently */ }
}
showRootWarning();

fetchVersion();

/* ---------------------------------------------------------------- *
 * 视频监控 (Surveillance) page                                       *
 * ---------------------------------------------------------------- *
 * 16 channels in a 4x4 grid. Each cell renders a server-pushed    *
 * JPEG thumbnail (canvas). Click a cell to open the enlarge modal *
 * which shows:                                                     *
 *   • WebCodecs-decoded H.264 video (same wire format the rkv     *
 *     VideoWriter pushes via rkv+ws:// — server forwards verbatim) *
 *   • Per-channel config form (name / source / prompt / size /    *
 *     enabled toggle)                                               *
 *   • Live VLM event history                                       *
 *                                                                *
 * Wire format shared with rkv.encoder._WebSocketSink:             *
 *   <type_byte:u8><pts_us:u64 LE><payload>                        *
 *     0x00 PKT_TYPE_DELTA, 0x01 KEY, 0x02 HEADER (avcC), 0xFF EOS *
 * ---------------------------------------------------------------- */
const Surveillance = {
  initialized: false,
  configs: [],          // [{id,name,source,prompt,enabled,detect_w,detect_h}]
  snapshots: [],        // [{id,state,error,frame_pos,fps,...}]
  cellStates: [],       // per-cell thumbnail canvas + WebSocket
  modalChannelId: null,
  modalDirty: false,
  modalStream: null,        // { ws, img } for the enlarge-modal MJPEG WS

  init() {
    if (this.initialized) return;
    this.initialized = true;

    const refreshBtn = document.getElementById("surveillance-refresh");
    if (refreshBtn) refreshBtn.addEventListener("click", () => this.refresh());

    const resetBtn = document.getElementById("surveillance-reset");
    if (resetBtn) resetBtn.addEventListener("click", () => this.resetAll());

    const closeBtn = document.getElementById("surv-modal-close");
    if (closeBtn) closeBtn.addEventListener("click", () => this.closeModal());
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        const m = document.getElementById("surv-modal");
        if (m && !m.classList.contains("hidden")) this.closeModal();
      }
    });
    const modal = document.getElementById("surv-modal");
    if (modal) modal.addEventListener("click", (e) => {
      if (e.target.id === "surv-modal") this.closeModal();
    });

    const form = document.getElementById("surv-modal-form");
    if (form) form.addEventListener("submit", (e) => {
      e.preventDefault();
      this.saveModal();
    });

    const reloadBtn = document.getElementById("surv-reload");
    if (reloadBtn) reloadBtn.addEventListener("click", () => this.reloadModal());

    const browseBtn = document.getElementById("surv-browse");
    if (browseBtn) browseBtn.addEventListener("click", () => {
      VdFsModal.open("");
    });

    const playbackEl = document.getElementById("surv-playback");
    if (playbackEl) playbackEl.addEventListener("change", () => {
      this.modalDirty = true;
      this.updateModalToggleLabel();
    });
    const detectionEl = document.getElementById("surv-detection");
    if (detectionEl) detectionEl.addEventListener("change", () => {
      this.modalDirty = true;
      this.updateModalToggleLabel();
    });
    ["surv-name", "surv-source", "surv-prompt", "surv-detect-size"]
      .forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.addEventListener("input", () => { this.modalDirty = true; });
      });

    const evClearBtn = document.getElementById("surv-events-clear");
    if (evClearBtn) evClearBtn.addEventListener("click", () => this.clearEvents());

    this.buildGrid();
  },

  buildGrid() {
    const grid = document.getElementById("surveillance-grid");
    if (!grid) return;
    grid.innerHTML = "";
    this.cellStates = [];
    for (let i = 1; i <= 16; i++) {
      const cell = document.createElement("div");
      cell.className = "surv-cell";
      cell.dataset.channelId = String(i);

      const canvas = document.createElement("canvas");
      canvas.className = "surv-cell-canvas";
      // The thumb WS now pushes detection-resolution JPEG frames
      // (384×384 by default, or 768×768 if the channel is configured
      // for the higher-fidelity preset). We start with a 1×1 backing
      // store and resize to the JPEG's natural size on the first
      // frame that arrives — avoids a stretched render before the
      // channel has produced a frame yet.
      canvas.width = 1;
      canvas.height = 1;
      const ctx = canvas.getContext("2d");
      ctx.fillStyle = "#0f172a";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = "#94a3b8";
      ctx.font = "13px system-ui";
      ctx.textAlign = "center";
      ctx.fillText("等待画面…", canvas.width / 2, canvas.height / 2);
      cell.appendChild(canvas);

      const overlay = document.createElement("div");
      overlay.className = "surv-cell-overlay";
      const name = document.createElement("span");
      name.className = "surv-cell-name";
      name.textContent = `通道 ${i}`;
      overlay.appendChild(name);

      const status = document.createElement("span");
      status.className = "surv-cell-status";
      status.textContent = "未启用";
      overlay.appendChild(status);

      cell.appendChild(overlay);
      cell.addEventListener("click", () => this.openModal(i));

      grid.appendChild(cell);
      this.cellStates.push({
        cell, canvas, ctx, nameEl: name, statusEl: status,
        ws: null, retry: 0, retryTimer: null,
      });
    }
  },

  async refresh() {
    try {
      const r = await fetch(API_PREFIX + "/surveillance");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      this.configs = data.configs || [];
      this.snapshots = data.channels || [];
      this.renderCells();
      this.renderStatus();
      this.updateRawJson(data);
    } catch (e) {
      this.setStatus("加载失败: " + e.message, true);
    }
  },

  renderStatus() {
    const enabled = this.snapshots.filter((s) => s && s.state === "running").length;
    const total = this.snapshots.length;
    this.setStatus(`共 ${total} 路 · 运行中 ${enabled} 路`, false);
  },

  setStatus(text, isError) {
    const el = document.getElementById("surveillance-status");
    if (!el) return;
    el.textContent = text || "";
    el.style.color = isError ? "#dc2626" : "";
  },

  updateRawJson(data) {
    const el = document.getElementById("data-surveillance");
    if (el) el.textContent = JSON.stringify(data, null, 2);
  },

  renderCells() {
    for (let i = 0; i < this.cellStates.length; i++) {
      const cs = this.cellStates[i];
      const cfg = this.configs[i];
      const snap = this.snapshots[i];
      if (cfg) {
        cs.nameEl.textContent = cfg.name || `通道 ${i + 1}`;
      }
      const playbackOn = !!(cfg && (cfg.playback_enabled || cfg.enabled));
      const detectionOn = !!(cfg && cfg.detection_enabled);
      let stateLabel = "未启用";
      let cssClass = (snap ? snap.state : "idle");
      if (snap) {
        if (snap.state === "running") {
          stateLabel = detectionOn ? "播放+检测" : "仅播放";
        } else if (snap.state === "opening") {
          stateLabel = "打开中…";
        } else if (snap.state === "eof") {
          stateLabel = "已结束";
        } else if (snap.state === "error") {
          stateLabel = "错误";
        } else if (snap.state === "paused") {
          stateLabel = "已暂停";
        } else if (snap.state === "idle") {
          if (!cfg || !cfg.source) stateLabel = "未配置";
          else if (playbackOn) stateLabel = "启动中";
          else stateLabel = detectionOn ? "检测(无画面)" : "未启用";
        }
        // Append the currently-playing file name for folder / loop
        // sources so the user can see which clip is on.
        if (snap.state === "running" && snap.current_file) {
          stateLabel += " · " + snap.current_file;
        }
        if (snap.error) stateLabel = "错误: " + (snap.error || "").slice(0, 12);
      }
      cs.statusEl.textContent = stateLabel;
      cs.statusEl.className = "surv-cell-status state-" + cssClass;

      // Ensure the thumbnail WS is connected whenever the channel is
      // running or has a configured source; the server only pushes
      // when a frame arrives, so idle WS connections are essentially
      // free.
      const shouldThumb = snap && (
        snap.state === "running" || snap.state === "opening"
      );
      if (shouldThumb && !cs.ws) this.connectThumb(i);
      if (!shouldThumb && cs.ws) this.disconnectThumb(i);
    }
  },

  connectThumb(idx) {
    const cs = this.cellStates[idx];
    if (!cs) return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}${API_PREFIX}/surveillance/${idx + 1}/thumb`;
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    cs.ws = ws;
    ws.onmessage = (ev) => {
      if (!(ev.data instanceof ArrayBuffer)) return;
      const blob = new Blob([ev.data], { type: "image/jpeg" });
      const img = new Image();
      img.onload = () => {
        // Resize the canvas backing store to the JPEG's natural
        // dimensions on first frame so the cell aspect ratio tracks
        // the detection size (square). Subsequent frames keep the
        // same backing store — ``drawImage`` will then scale the
        // JPEG to whatever the CSS-driven display size is.
        if (img.naturalWidth && img.naturalHeight) {
          if (cs.canvas.width !== img.naturalWidth) cs.canvas.width = img.naturalWidth;
          if (cs.canvas.height !== img.naturalHeight) cs.canvas.height = img.naturalHeight;
        }
        cs.ctx.drawImage(img, 0, 0, cs.canvas.width, cs.canvas.height);
        URL.revokeObjectURL(img.src);
      };
      img.src = URL.createObjectURL(blob);
    };
    ws.onclose = () => {
      cs.ws = null;
      // Slow reconnect — server pushes only when a frame arrives, so
      // a closed connection doesn't necessarily mean anything is wrong.
      const wait = Math.min(5000, 500 * Math.pow(2, cs.retry++));
      cs.retryTimer = setTimeout(() => {
        cs.retryTimer = null;
        this.connectThumb(idx);
      }, wait);
    };
    ws.onerror = () => {
      try { ws.close(); } catch (_) {}
    };
  },

  disconnectThumb(idx) {
    const cs = this.cellStates[idx];
    if (!cs) return;
    if (cs.retryTimer) {
      clearTimeout(cs.retryTimer);
      cs.retryTimer = null;
    }
    cs.retry = 0;
    if (cs.ws) {
      try { cs.ws.close(); } catch (_) {}
      cs.ws = null;
    }
  },

  async resetAll() {
    if (!window.confirm("确定要恢复所有 16 路通道为默认配置吗？")) return;
    try {
      const r = await fetch(API_PREFIX + "/surveillance/reset", { method: "POST" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      this.configs = data.channels || [];
      await this.refresh();
    } catch (e) {
      window.alert("重置失败: " + e.message);
    }
  },

  // ------------------------------------------------------------------ //
  // Enlarge modal                                                       //
  // ------------------------------------------------------------------ //
  openModal(channelId) {
    this.modalChannelId = channelId;
    const modal = document.getElementById("surv-modal");
    if (!modal) return;
    modal.classList.remove("hidden");
    this.reloadModal();
    this.startModalStream(channelId);
    this.refreshModalEvents();
  },

  closeModal() {
    const modal = document.getElementById("surv-modal");
    if (!modal) return;
    if (this.modalDirty) {
      if (!window.confirm("有未保存的修改，确定要关闭吗？")) return;
    }
    modal.classList.add("hidden");
    this.modalChannelId = null;
    this.stopModalStream();
    this.modalDirty = false;
  },

  reloadModal() {
    const id = this.modalChannelId;
    if (id == null) return;
    const cfg = this.configs[id - 1];
    if (!cfg) return;
    document.getElementById("surv-name").value = cfg.name || "";
    document.getElementById("surv-source").value = cfg.source || "";
    document.getElementById("surv-prompt").value = cfg.prompt || "";
    // detect_w / detect_h are kept equal by the backend; the
    // modal renders a single ``detect_size`` dropdown.
    const size = (cfg.detect_w === 768 || cfg.detect_h === 768) ? "768" : "384";
    document.getElementById("surv-detect-size").value = size;
    // Legacy configs only have ``enabled``; treat that as
    // playback_enabled for backwards compat.
    const playback = !!(cfg.playback_enabled || cfg.enabled);
    document.getElementById("surv-playback").checked = playback;
    document.getElementById("surv-detection").checked = !!cfg.detection_enabled;
    document.getElementById("surv-modal-title").textContent = cfg.name || `通道 ${id}`;
    const subParts = [`#${id}`];
    if (cfg.source) subParts.push(cfg.source);
    document.getElementById("surv-modal-sub").textContent = subParts.join(" · ");
    this.updateModalToggleLabel();
    this.renderModalMeta();
    this.modalDirty = false;
    this.setFoot("", "");
  },

  updateModalToggleLabel() {
    const playbackEl = document.getElementById("surv-playback");
    const detectionEl = document.getElementById("surv-detection");
    const playbackLabel = document.getElementById("surv-playback-label");
    const detectionLabel = document.getElementById("surv-detection-label");
    if (playbackEl && playbackLabel) {
      playbackLabel.textContent = playbackEl.checked ? "播放中" : "播放关闭";
    }
    if (detectionEl && detectionLabel) {
      detectionLabel.textContent = detectionEl.checked ? "检测中" : "检测关闭";
    }
  },

  renderModalMeta() {
    const id = this.modalChannelId;
    if (id == null) return;
    const snap = this.snapshots[id - 1];
    const cfg = this.configs[id - 1];
    const el = document.getElementById("surv-modal-meta");
    if (!el) return;
    if (!snap) { el.textContent = "--"; return; }
    const parts = [];
    parts.push(`状态 ${snap.state || "--"}`);
    if (snap.current_file) parts.push(`▶ ${snap.current_file}`);
    if (snap.fps) parts.push(`${snap.fps} fps`);
    if (snap.frame_pos) parts.push(`帧 ${snap.frame_pos}`);
    const playback = !!(cfg && (cfg.playback_enabled || cfg.enabled));
    const detection = !!(cfg && cfg.detection_enabled);
    parts.push(`播放 ${playback ? "开" : "关"}`);
    parts.push(`检测 ${detection ? "开" : "关"}`);
    if (snap.session_error) parts.push("VLM 错误: " + snap.session_error);
    el.textContent = parts.join(" · ");
  },

  setFoot(text, level) {
    const el = document.getElementById("surv-foot");
    if (!el) return;
    el.textContent = text || "";
    el.classList.remove("warn", "danger", "ok");
    if (level) el.classList.add(level);
  },

  async saveModal() {
    const id = this.modalChannelId;
    if (id == null) return;
    // Single dropdown → mirrored into both detect_w and detect_h
    // so the backend's "always square" invariant is preserved.
    const rawSize = document.getElementById("surv-detect-size").value;
    const size = (rawSize === "768") ? 768 : 384;
    const payload = {
      name: (document.getElementById("surv-name").value || "").trim(),
      source: (document.getElementById("surv-source").value || "").trim(),
      prompt: document.getElementById("surv-prompt").value || "",
      playback_enabled: !!document.getElementById("surv-playback").checked,
      detection_enabled: !!document.getElementById("surv-detection").checked,
      detect_w: size,
      detect_h: size,
    };
    this.setFoot("正在保存…", "warn");
    try {
      const r = await fetch(API_PREFIX + `/surveillance/config/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) {
        this.setFoot("保存失败: " + (body.detail || r.status), "danger");
        return;
      }
      this.modalDirty = false;
      this.configs[id - 1] = { ...this.configs[id - 1], ...body.channel };
      this.reloadModal();
      this.renderCells();
      this.setFoot("已保存", "ok");
      this.refresh();
    } catch (e) {
      this.setFoot("保存失败: " + e.message, "danger");
    }
  },

  async refreshModalEvents() {
    const id = this.modalChannelId;
    if (id == null) return;
    try {
      const r = await fetch(API_PREFIX + `/surveillance/${id}/events`);
      if (!r.ok) return;
      const data = await r.json();
      this.renderEventList(data.events || []);
      this.renderModalMeta();
    } catch (_) {}
  },

  renderEventList(events) {
    const list = document.getElementById("surv-event-list");
    const count = document.getElementById("surv-events-count");
    if (!list || !count) return;
    count.textContent = (events.length || 0) + " 条";
    list.innerHTML = "";
    if (!events.length) {
      const li = document.createElement("li");
      li.className = "vd-event-empty";
      li.textContent = "尚未检测到事件，启动通道后结果显示在这里。";
      list.appendChild(li);
      return;
    }
    events.forEach((ev) => {
      const li = document.createElement("li");
      li.className = "vd-event";
      const meta = document.createElement("div");
      meta.className = "vd-event-meta";
      const ts = document.createElement("span");
      ts.textContent = fmtEventTime(ev.ts);
      const fp = document.createElement("span");
      fp.textContent = "帧 " + (ev.frame_pos ?? "?");
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = "#" + ev.id;
      meta.appendChild(ts);
      meta.appendChild(fp);
      meta.appendChild(tag);
      const text = document.createElement("div");
      text.className = "vd-event-text";
      text.textContent = ev.text || "(空)";
      li.appendChild(meta);
      li.appendChild(text);
      list.appendChild(li);
    });
  },

  async clearEvents() {
    const id = this.modalChannelId;
    if (id == null) return;
    try {
      await fetch(API_PREFIX + `/surveillance/${id}/events/clear`, { method: "POST" });
      this.renderEventList([]);
    } catch (_) {}
  },

  // ------------------------------------------------------------------ //
  // Enlarge-modal MJPEG playback                                        //
  // ------------------------------------------------------------------ //
  // The server pushes one native-resolution JPEG per WS message at the
  // source's full fps. The browser is just a dumb consumer: we wrap
  // each ``ArrayBuffer`` in a ``Blob`` and point an off-screen
  // ``<img>`` at the resulting object URL; on each ``onload`` we
  // ``drawImage`` the freshly-decoded picture into the modal canvas.
  // No WebCodecs, no decoder on the client — works on every browser,
  // including Edge over http:// non-secure contexts where
  // ``window.VideoDecoder`` is undefined.
  startModalStream(channelId) {
    this.stopModalStream();
    const overlay = document.getElementById("surv-modal-overlay");
    const canvas = document.getElementById("surv-modal-canvas");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    // Reflect channel state in the overlay so the user knows why the
    // picture might be empty.
    const cfg = this.configs[channelId - 1];
    const snap = this.snapshots[channelId - 1];
    const playbackOn = !!(cfg && (cfg.playback_enabled || cfg.enabled));
    if (!cfg || !cfg.source) {
      this._setModalOverlay(overlay, "请先在「视频源」中填写路径并保存", false);
    } else if (!playbackOn) {
      this._setModalOverlay(overlay, "通道已配置但未启用播放（打开右上角「播放中」开关后保存）", false);
    } else if (snap && snap.error) {
      this._setModalOverlay(overlay, "通道错误: " + snap.error, true);
    } else if (snap && snap.state !== "running") {
      this._setModalOverlay(overlay, "正在启动解码…", false);
    } else {
      this._setModalOverlay(overlay, "正在连接视频流…", false);
    }

    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}${API_PREFIX}/surveillance/${channelId}/stream`;
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    this.modalStream = { ws, img: null };

    // Re-use a single off-screen <img> so GC isn't triggered for
    // every JPEG. We swap the .src on each frame.
    const img = new Image();
    this.modalStream.img = img;
    img.onload = () => {
      // Resize the canvas to the native source size on first
      // frame. Subsequent frames skip the canvas resize — ``drawImage``
      // clips to the destination canvas dimensions.
      if (img.naturalWidth && img.naturalHeight) {
        if (canvas.width !== img.naturalWidth) canvas.width = img.naturalWidth;
        if (canvas.height !== img.naturalHeight) canvas.height = img.naturalHeight;
      }
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      this._setModalOverlay(overlay, "", false);
      // Revoke the blob URL we created in onmessage — by the time
      // ``onload`` fires the browser has already decoded the frame
      // so the URL is no longer needed.
      try { URL.revokeObjectURL(img.src); } catch (_) {}
    };
    img.onerror = () => {
      this._setModalOverlay(overlay, "视频帧解码失败", true);
    };

    ws.onmessage = (ev) => {
      if (!(ev.data instanceof ArrayBuffer)) return;
      const blob = new Blob([ev.data], { type: "image/jpeg" });
      img.src = URL.createObjectURL(blob);
    };
    ws.onopen = () => {
      this._setModalOverlay(overlay, "等待画面…", false);
    };
    ws.onerror = () => {
      this._setModalOverlay(overlay, "视频流连接失败", true);
    };
    ws.onclose = () => {
      if (this.modalStream && this.modalStream.ws === ws) {
        this.modalStream = null;
      }
    };
  },

  _setModalOverlay(el, text, isError) {
    if (!el) return;
    el.textContent = text || "";
    el.classList.toggle("error", !!isError);
    if (text) {
      el.style.display = "";
    } else {
      el.style.display = "none";
    }
  },

  stopModalStream() {
    const ms = this.modalStream;
    if (ms) {
      if (ms.ws) {
        try { ms.ws.close(); } catch (_) {}
      }
      if (ms.img) {
        try { ms.img.src = ""; } catch (_) {}
      }
      this.modalStream = null;
    }
    const overlay = document.getElementById("surv-modal-overlay");
    if (overlay) {
      overlay.textContent = "";
      overlay.classList.remove("error");
    }
  },
};



// The video-detection file picker modal knows about /api/video-detection/fs.
// When the user picks a file from the 视频监控 modal, we want the same
// destination. Wire VdFsModal to write into #surv-source when the
// surveillance modal is open. Also covers the base-config 视频监控
// table — there, each row's "浏览…" button sets
// ``VdFsModal._sourceTarget`` to its source input before opening the
// modal; the override below writes the picked path into that input.
VdFsModal._sourceTarget = null;
const _vdOrigPickFile = VdFsModal.pickSelectedFile.bind(VdFsModal);
VdFsModal.pickSelectedFile = function () {
  if (!this.selected) return;
  // 1. Explicit target (e.g. base-config row's source input)
  if (VdFsModal._sourceTarget) {
    VdFsModal._sourceTarget.value = this.selected.path;
    VdFsModal._sourceTarget = null;
    this.close();
    return;
  }
  // 2. Surveillance enlarge modal
  const survModal = document.getElementById("surv-modal");
  const survSource = document.getElementById("surv-source");
  if (survModal && !survModal.classList.contains("hidden") && survSource) {
    survSource.value = this.selected.path;
    if (typeof Surveillance !== "undefined") {
      Surveillance.modalDirty = true;
    }
    this.close();
    return;
  }
  // 3. Default: video-detection page
  _vdOrigPickFile();
};
const _vdOrigPickDir = VdFsModal.pickCurrentDir.bind(VdFsModal);
VdFsModal.pickCurrentDir = function () {
  const btn = document.getElementById("vd-modal-pick-dir");
  const p = btn.dataset.path;
  if (!p) return;
  // 1. Explicit target (base-config row)
  if (VdFsModal._sourceTarget) {
    VdFsModal._sourceTarget.value = p;
    VdFsModal._sourceTarget = null;
    this.close();
    return;
  }
  // 2. Surveillance enlarge modal
  const survModal = document.getElementById("surv-modal");
  const survSource = document.getElementById("surv-source");
  if (survModal && !survModal.classList.contains("hidden") && survSource) {
    survSource.value = p;
    if (typeof Surveillance !== "undefined") {
      Surveillance.modalDirty = true;
    }
    this.close();
    return;
  }
  // 3. Default: video-detection page
  _vdOrigPickDir();
};

// Wire the file-picker modal's own buttons (close / pick dir / pick file)
// at module load so they work no matter which page triggered the modal —
// previously they were registered inside ``initVideoDetection``, which
// only ran when the user visited the 视频检测 page; users who opened
// the picker from 视频监控 directly hit dead buttons.
{
  const closeBtn = document.getElementById("vd-modal-close");
  const pickDirBtn = document.getElementById("vd-modal-pick-dir");
  const pickFileBtn = document.getElementById("vd-modal-pick-file");
  if (closeBtn) closeBtn.addEventListener("click", () => VdFsModal.close());
  if (pickDirBtn) pickDirBtn.addEventListener("click", () => VdFsModal.pickCurrentDir());
  if (pickFileBtn) pickFileBtn.addEventListener("click", () => VdFsModal.pickSelectedFile());
}
showPage(currentPage());
