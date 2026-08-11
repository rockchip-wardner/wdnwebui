const API_PREFIX = "/api";
const DEFAULT_PAGE = "system-monitor";

async function fetchVersion() {
  try {
    const r = await fetch("/health");
    if (!r.ok) return;
    const el = document.getElementById("brand-version");
    if (el) el.textContent = "v" + (window.__APP_VERSION__ || "0.4.0");
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
  const grid = document.getElementById("npu-grid");
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

  grid.innerHTML = "";
  const devices = npu.devices || [];
  if (!devices.length) {
    const empty = document.createElement("div");
    empty.className = "npu-empty";
    empty.textContent = "未检测到 NPU 设备";
    grid.appendChild(empty);
    return;
  }
  devices.forEach(d => {
    const tempC = d.temp_c == null ? null : Number(d.temp_c);
    const npuPct = d.npu_percent == null ? null : Number(d.npu_percent);
    const powerMw = d.power_mw == null ? null : Number(d.power_mw);
    const memUsed = d.mem_used_mb == null ? null : Number(d.mem_used_mb);
    const memTotal = d.mem_total_mb == null ? null : Number(d.mem_total_mb);
    const memPct = (memUsed != null && memTotal && memTotal > 0)
      ? Math.min(100, Math.max(0, (memUsed / memTotal) * 100))
      : null;

    // Card-level severity: error if temp >= 85 or NPU% saturated, warn if >= 70 / 80
    let level = "ok";
    if (tempC != null && tempC >= 85) level = "error";
    else if (tempC != null && tempC >= 75) level = "warn";
    else if (npuPct != null && npuPct >= 95) level = "warn";
    if (d.status && /error|fail|down|offline/i.test(d.status) && level === "ok") level = "error";

    const card = document.createElement("div");
    card.className = "npu-card" + (level === "ok" ? "" : " is-" + level);

    // --- head ---
    const head = document.createElement("div");
    head.className = "npu-card-head";
    const title = document.createElement("span");
    title.className = "npu-card-title";
    title.textContent = (d.id || "NPU") + " #" + d.index;
    head.appendChild(title);
    if (d.status) {
      const statusBadge = document.createElement("span");
      statusBadge.className = "badge " + (level === "ok" ? "ok" : (level === "warn" ? "warn" : "err"));
      statusBadge.textContent = d.status;
      head.appendChild(statusBadge);
    }
    const meta = document.createElement("span");
    meta.className = "npu-card-meta";
    meta.textContent = d.bus_id || "";
    meta.title = d.bus_id || "";
    head.appendChild(meta);
    card.appendChild(head);

    // --- 3-stat row ---
    const stats = document.createElement("div");
    stats.className = "npu-stats";
    const mkStat = (label, value, unit, level) => {
      const stat = document.createElement("div");
      stat.className = "npu-stat";
      const num = document.createElement("div");
      num.className = "npu-stat-num";
      const val = document.createElement("span");
      val.className = "npu-stat-value" + (level ? " is-" + level : "");
      if (value == null || Number.isNaN(value)) {
        val.textContent = "--";
      } else {
        val.textContent = fmtNum(value, Number.isInteger(value) ? 0 : 1);
      }
      num.appendChild(val);
      if (unit && value != null && !Number.isNaN(value)) {
        const u = document.createElement("span");
        u.className = "npu-stat-unit";
        u.textContent = unit;
        num.appendChild(u);
      }
      stat.appendChild(num);
      const lbl = document.createElement("span");
      lbl.className = "npu-stat-label";
      lbl.textContent = label;
      stat.appendChild(lbl);
      return stat;
    };
    let tempLevel = null;
    if (tempC != null) {
      if (tempC >= 85) tempLevel = "error";
      else if (tempC >= 75) tempLevel = "warn";
    }
    let npuLevel = null;
    if (npuPct != null && npuPct >= 95) npuLevel = "warn";
    stats.appendChild(mkStat("温度", tempC, "°C", tempLevel));
    stats.appendChild(mkStat("功率", powerMw == null ? null : powerMw / 1000, "W", null));
    stats.appendChild(mkStat("NPU 占用", npuPct, "%", npuLevel));
    card.appendChild(stats);

    // --- memory bar ---
    if (memUsed != null || memTotal != null) {
      const memBlock = document.createElement("div");
      memBlock.className = "npu-mem";
      const memMeta = document.createElement("div");
      memMeta.className = "npu-mem-meta";
      const memLabel = document.createElement("span");
      memLabel.innerHTML = "显存 <strong>" +
        (memUsed != null ? fmtNum(memUsed, 0) : "--") +
        " / " +
        (memTotal != null ? fmtNum(memTotal, 0) : "--") +
        "</strong> MB";
      memMeta.appendChild(memLabel);
      const memPctText = document.createElement("span");
      memPctText.textContent = memPct == null ? "--" : memPct.toFixed(0) + " %";
      memMeta.appendChild(memPctText);
      memBlock.appendChild(memMeta);

      const bar = document.createElement("div");
      bar.className = "npu-mem-bar";
      const fill = document.createElement("div");
      let memLevel = null;
      if (memPct != null) {
        if (memPct >= 90) memLevel = "error";
        else if (memPct >= 70) memLevel = "warn";
      }
      fill.className = "npu-mem-bar-fill" + (memLevel ? " is-" + memLevel : "");
      fill.style.width = (memPct == null ? 0 : memPct) + "%";
      bar.appendChild(fill);
      memBlock.appendChild(bar);
      card.appendChild(memBlock);
    }

    grid.appendChild(card);
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
  tools: [],
  enabledTools: [],
  lastSnap: null,
  detectorSnap: null,
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
  VD.lastSnap = snap;
  if (typeof DemoGuard !== "undefined") {
    DemoGuard.report("video-detection", { session: VD.lastSnap, detector: VD.detectorSnap });
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
 */
function sanitizeEventText(s) {
  if (s == null) return "";
  let out = String(s);
  // <|...|> chatml / rknn special tokens
  out = out.replace(/<\|[^|]*\|>/g, "");
  // ASCII control chars except \n \r \t
  out = out.replace(/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]/g, "");
  // Unicode replacement char (decoding failures)
  out = out.replace(/\uFFFD/g, "");
  // Collapse 3+ consecutive newlines to 2
  out = out.replace(/\n{3,}/g, "\n\n");
  return out.trim();
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
    const toolsCount = (snap.enabled_tools || []).length;
    const toolsHint = toolsCount ? ` · ${toolsCount} 个工具` : "";
    statusEl.textContent =
      `运行中 · 每 ${snap.every_n} 帧 · 队列 ${snap.queue_size}${toolsHint}`;
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
  VD.detectorSnap = snap;
  if (typeof DemoGuard !== "undefined") {
    DemoGuard.report("video-detection", { session: VD.lastSnap, detector: VD.detectorSnap });
  }
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
  const enabled_tools = vdCollectEnabledTools();
  try {
    const r = await fetch(API_PREFIX + "/video-detection/prompt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt, every_n, enabled_tools }),
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
    const check = document.createElement("input");
    check.type = "checkbox";
    check.dataset.toolKey = t.key;
    check.disabled = !t.available;
    if (VD.enabledTools.includes(t.key)) check.checked = true;
    check.addEventListener("change", () => {
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
    const name = document.createElement("span");
    name.className = "vd-tool-card-name";
    name.textContent = t.name;
    const key = document.createElement("span");
    key.className = "vd-tool-card-key";
    key.textContent = t.key;
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

  // --- detection control buttons ---
  const detStartBtn = document.getElementById("vd-detector-start");
  const detStopBtn  = document.getElementById("vd-detector-stop");
  const evClearBtn  = document.getElementById("vd-events-clear");
  if (detStartBtn) detStartBtn.addEventListener("click", vdDetectorStart);
  if (detStopBtn)  detStopBtn.addEventListener("click",  vdDetectorStop);
  if (evClearBtn)  evClearBtn.addEventListener("click",  vdEventsClear);

  // Initial state hydration (when navigating back to the page)
  refreshVdDetector();
  vdLoadTools();

  if (!VD.ws && !VD.wsTimer) connectVdWs();
}

/* ------------------------------------------------------------------ *
 * 视频目标检测 (Video Target Detection) — TargetDoc 结构化输出           *
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
const VTD = {
  ws: null,
  wsRetry: 0,
  wsTimer: null,
  blobUrl: null,
  lastSnap: null,
  detectorSnap: null,
};
const VTDDetector = {
  events: [],
  refresh() { return refreshVtdDetector(); },
};

function setVtdOverlay(text, isError) {
  const el = document.getElementById("vtd-overlay");
  if (!el) return;
  el.textContent = text || "";
  el.classList.toggle("error", !!isError);
  el.style.display = text ? "" : "none";
}

function setVtdMeta(snap) {
  const el = document.getElementById("vtd-meta");
  if (!el) return;
  const parts = [];
  if (snap.width && snap.height) parts.push(snap.width + "x" + snap.height);
  if (snap.fps) parts.push(fmtNum(snap.fps, 2) + " fps");
  if (snap.frame_pos != null) parts.push("帧 " + snap.frame_pos);
  if (snap.is_live) parts.push("LIVE");
  if (snap.state) parts.push("[" + snap.state + "]");
  el.textContent = parts.join(" · ") || "--";
  VTD.lastSnap = snap;
  if (typeof DemoGuard !== "undefined") {
    DemoGuard.report("video-target-detection", { session: VTD.lastSnap, detector: VTD.detectorSnap });
  }
}

function setVtdProgress(snap) {
  const el = document.getElementById("vtd-progress");
  if (!el) return;
  el.classList.toggle("live", !!snap.is_live);
  el.style.width = snap.is_live ? "100%" : "0%";
}

async function drawVtdFrame(jpgBytes) {
  const canvas = document.getElementById("vtd-canvas");
  if (!canvas || !jpgBytes || !jpgBytes.byteLength) return;
  const blob = new Blob([jpgBytes], { type: "image/jpeg" });
  if (VTD.blobUrl) URL.revokeObjectURL(VTD.blobUrl);
  VTD.blobUrl = URL.createObjectURL(blob);
  const img = new Image();
  await new Promise((resolve, reject) => {
    img.onload = resolve;
    img.onerror = reject;
    img.src = VTD.blobUrl;
  });
  const ctx = canvas.getContext("2d");
  canvas.width = img.naturalWidth || canvas.width;
  canvas.height = img.naturalHeight || canvas.height;
  ctx.drawImage(img, 0, 0);
}

function connectVtdWs() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}${API_PREFIX}/video-target-detection/ws`;
  const ws = new WebSocket(url);
  ws.binaryType = "arraybuffer";
  VTD.ws = ws;

  ws.onopen = () => {
    VTD.wsRetry = 0;
    setVtdOverlay("", false);
  };
  ws.onmessage = async (ev) => {
    if (typeof ev.data === "string") {
      let snap;
      try { snap = JSON.parse(ev.data); } catch (_) { return; }
      if (snap.type === "eof") {
        setVtdOverlay("文件已结束 (EOF)，点 播放 可恢复", true);
      } else if (snap.type === "error") {
        setVtdOverlay("WS 错误: " + (snap.detail || ""), true);
      } else if (snap.type === "event") {
        addVtdEvent(snap);
        showVtdEventBadge(snap);
        if (snap.tool_call === "target_detection" && snap.crop_b64) {
          showVtdCropPopup(snap.crop_b64, snap.crop_w, snap.crop_h);
        }
        return;
      } else if (snap.type === "status" && snap.detector) {
        updateVtdDetectorStatus(snap.detector);
      }
      setVtdMeta(snap);
      setVtdProgress(snap);
      refreshVtdButtons(snap.state);
      const payload = document.getElementById("data-video-target-detection");
      if (payload) payload.textContent = JSON.stringify(snap, null, 2);
      return;
    }
    try {
      await drawVtdFrame(new Uint8Array(ev.data));
      setVtdOverlay("", false);
    } catch (e) {
      setVtdOverlay("帧渲染失败: " + e, true);
    }
  };
  ws.onclose = () => {
    VTD.ws = null;
    const wait = Math.min(5000, 500 * Math.pow(2, VTD.wsRetry++));
    VTD.wsTimer = setTimeout(connectVtdWs, wait);
  };
  ws.onerror = () => {
    try { ws.close(); } catch (_) {}
  };
}

async function vtdOpen(payload) {
  const r = await fetch(API_PREFIX + "/video-target-detection/open", {
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
async function vtdAction(action) {
  const r = await fetch(API_PREFIX + "/video-target-detection/control", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action }),
  });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}
async function vtdStatus() {
  const r = await fetch(API_PREFIX + "/video-target-detection", { cache: "no-store" });
  if (!r.ok) return null;
  const snap = await r.json();
  setVtdMeta(snap);
  setVtdProgress(snap);
  refreshVtdButtons(snap.state);
  const loopEl = document.getElementById("vtd-loop");
  if (loopEl && snap.loop != null) loopEl.checked = !!snap.loop;
  const payload = document.getElementById("data-video-target-detection");
  if (payload) payload.textContent = JSON.stringify(snap, null, 2);
  return snap;
}
async function vtdClose() {
  const r = await fetch(API_PREFIX + "/video-target-detection/close", { method: "POST" });
  return r.ok ? r.json() : null;
}
function refreshVtdButtons(state) {
  const playBtn = document.getElementById("vtd-play");
  const pauseBtn = document.getElementById("vtd-pause");
  const stopBtn = document.getElementById("vtd-stop");
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

function addVtdEvent(ev) {
  VTDDetector.events.unshift(ev);
  if (VTDDetector.events.length > 200) VTDDetector.events.length = 200;
  renderVtdEventList();
}
function renderVtdEventList() {
  const list = document.getElementById("vtd-event-list");
  const count = document.getElementById("vtd-events-count");
  if (!list || !count) return;
  count.textContent = VTDDetector.events.length + " 条";
  if (VTDDetector.events.length === 0) {
    list.innerHTML =
      '<li class="vd-event-empty">尚未检测到目标，启动检测后结果显示在这里。</li>';
    return;
  }
  list.innerHTML = "";
  VTDDetector.events.forEach((ev, idx) => {
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
    const body = document.createElement("div");
    body.className = "vd-event-body";
    if (ev.tool_call === "target_detection" && ev.crop_b64) {
      const thumb = document.createElement("img");
      thumb.className = "vtd-crop-thumb";
      thumb.alt = "目标检测剪裁";
      thumb.src = "data:image/jpeg;base64," + ev.crop_b64;
      body.appendChild(thumb);
    }
    const text = document.createElement("div");
    text.className = "vd-event-text";
    // ``ev.desc`` 是模型对 ROI 的描述（target_detection 专用字段）；
    // 若为空回退到 ``ev.text``。前端始终展示模型给出的描述文本。
    const rawText = (ev.desc && String(ev.desc).trim())
                 || (ev.text && String(ev.text).trim())
                 || "(空)";
    text.textContent = sanitizeEventText(rawText);
    body.appendChild(text);
    li.appendChild(meta);
    li.appendChild(body);
    list.appendChild(li);
  });
}
let vtdBadgeHideTimer = null;
function showVtdEventBadge(ev) {
  const badge = document.getElementById("vtd-event-badge");
  const text = document.getElementById("vtd-event-badge-text");
  if (!badge || !text) return;
  // VTD badge 用 desc（target_detection 专用）回退 text，再 sanitize
  const raw = (ev.desc && String(ev.desc).trim())
            || (ev.text && String(ev.text).trim())
            || "(空)";
  text.textContent = sanitizeEventText(raw);
  badge.classList.remove("hidden");
  badge.style.animation = "none";
  badge.offsetHeight;
  badge.style.animation = "";
  if (vtdBadgeHideTimer) clearTimeout(vtdBadgeHideTimer);
  vtdBadgeHideTimer = setTimeout(() => badge.classList.add("hidden"), 6000);
}

let vtdCropHideTimer = null;
function showVtdCropPopup(cropB64, w, h) {
  const overlay = document.getElementById("vtd-overlay");
  if (!overlay || !cropB64) return;
  const existing = overlay.querySelector(".vtd-crop-popup");
  if (existing) {
    try { existing.remove(); } catch (_) { /* ignore */ }
  }
  if (vtdCropHideTimer) {
    clearTimeout(vtdCropHideTimer);
    vtdCropHideTimer = null;
  }
  const img = document.createElement("img");
  img.className = "vtd-crop-popup";
  img.alt = "目标检测剪裁" + (w && h ? ` (${w}×${h})` : "");
  img.src = "data:image/jpeg;base64," + cropB64;
  overlay.appendChild(img);
  vtdCropHideTimer = setTimeout(() => {
    img.classList.add("is-fading");
    setTimeout(() => {
      try { img.remove(); } catch (_) { /* ignore */ }
    }, 600);
  }, 4600);
}
function updateVtdDetectorStatus(snap) {
  const statusEl = document.getElementById("vtd-detector-status");
  const startBtn = document.getElementById("vtd-detector-start");
  const stopBtn  = document.getElementById("vtd-detector-stop");
  if (!snap || !statusEl) return;
  if (!snap.available) {
    statusEl.textContent = "不可用: " + (snap.init_error || "rkai 未安装");
  } else if (snap.init_error) {
    statusEl.textContent = "模型加载失败: " + snap.init_error;
  } else if (snap.enabled && snap.session_ready) {
    statusEl.textContent =
      `运行中 · 每 ${snap.every_n} 帧 · 队列 ${snap.queue_size} · 目标检测`;
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
  VTD.detectorSnap = snap;
  if (typeof DemoGuard !== "undefined") {
    DemoGuard.report("video-target-detection", { session: VTD.lastSnap, detector: VTD.detectorSnap });
  }
}
async function refreshVtdDetector() {
  try {
    const r = await fetch(API_PREFIX + "/video-target-detection/events");
    if (!r.ok) return null;
    const data = await r.json();
    updateVtdDetectorStatus(data.snapshot || {});
    if (VTDDetector.events.length === 0 && Array.isArray(data.events)) {
      VTDDetector.events = data.events.slice();
      renderVtdEventList();
    }
    return data;
  } catch (_) { return null; }
}
async function vtdDetectorStart() {
  const promptEl = document.getElementById("vtd-prompt");
  const everyEl  = document.getElementById("vtd-every-n");
  const prompt = (promptEl?.value || "").trim();
  const every_n = parseInt(everyEl?.value || "30", 10);
  if (!prompt) {
    setVtdOverlay("请先填写检测提示词", true);
    return;
  }
  try {
    const r = await fetch(API_PREFIX + "/video-target-detection/prompt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt, every_n }),
    });
    if (!r.ok) {
      const detail = await r.text().catch(() => "");
      setVtdOverlay("启动检测失败: " + detail, true);
      return;
    }
    const snap = await r.json();
    updateVtdDetectorStatus(snap);
    setVtdOverlay("", false);
  } catch (e) {
    setVtdOverlay("启动检测失败: " + e.message, true);
  }
}
async function vtdDetectorStop() {
  try {
    const r = await fetch(API_PREFIX + "/video-target-detection/prompt/stop", { method: "POST" });
    if (r.ok) updateVtdDetectorStatus(await r.json());
  } catch (e) {
    setVtdOverlay("停止检测失败: " + e.message, true);
  }
}
async function vtdEventsClear() {
  try {
    await fetch(API_PREFIX + "/video-target-detection/events/clear", { method: "POST" });
    VTDDetector.events = [];
    renderVtdEventList();
  } catch (_) {}
}

// 视频目标检测页不再有工具集合概念 —— 模型直接返回
// `TargetDoc` (x1, y1, x2, y2 + desc) 结构化输出，不挂任何
// `@Callable` 工具。因此 ``vtdCollectEnabledTools`` / ``vtdLoadTools``
// / ``renderVtdTools`` / ``updateVtdToolsSummary`` 已删除，原本调用
// `vtdLoadTools()` 的初始化路径只在页面空跑一行 no-op（防止将来
// 误改后端时悄悄引入工具流程）。

function initVideoTargetDetection() {
  if (window.__vtdInit) return;
  window.__vtdInit = true;

  const sourceEl = document.getElementById("vtd-source");
  const loopEl   = document.getElementById("vtd-loop");
  const openBtn = document.getElementById("vtd-open");
  const closeBtn = document.getElementById("vtd-close");
  const browseBtn = document.getElementById("vtd-browse");
  const playBtn = document.getElementById("vtd-play");
  const pauseBtn = document.getElementById("vtd-pause");
  const stopBtn = document.getElementById("vtd-stop");

  sourceEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter") openBtn.click();
  });

  openBtn.addEventListener("click", async () => {
    const src = (sourceEl.value || "").trim();
    if (!src) {
      setVtdOverlay("请输入或选择视频源", true);
      return;
    }
    openBtn.disabled = true;
    setVtdOverlay("正在打开 " + src + " …", false);
    try {
      const snap = await vtdOpen({ source: src, loop: !!(loopEl && loopEl.checked) });
      setVtdMeta(snap);
      setVtdProgress(snap);
      if (loopEl && snap.loop != null) loopEl.checked = !!snap.loop;
    } catch (e) {
      setVtdOverlay("打开失败: " + e.message, true);
    } finally {
      openBtn.disabled = false;
    }
  });

  // Browse button: open the same server-side file picker modal as the
  // description page, but route the picked path back into #vtd-source.
  browseBtn.addEventListener("click", () => {
    VdFsModal._sourceTarget = sourceEl;
    VdFsModal.open("");
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      const m = document.getElementById("vd-modal");
      if (!m.classList.contains("hidden")) VdFsModal.close();
    }
  });

  closeBtn.addEventListener("click", async () => {
    try { await vtdClose(); } catch (_) {}
    setVtdOverlay("已关闭", false);
  });

  playBtn.addEventListener("click", async () => {
    try {
      const r = await vtdAction("play");
      await vtdStatus();
      if (r && r.ok === false) setVtdOverlay("播放失败: " + (r.reason || r.state), true);
      else setVtdOverlay("", false);
    } catch (e) { setVtdOverlay("播放失败: " + e.message, true); }
  });
  pauseBtn.addEventListener("click", async () => {
    try {
      const r = await vtdAction("pause");
      await vtdStatus();
      if (r && r.ok === false) setVtdOverlay("暂停失败: " + (r.reason || r.state), true);
      else setVtdOverlay("", false);
    } catch (e) { setVtdOverlay("暂停失败: " + e.message, true); }
  });
  stopBtn.addEventListener("click", async () => {
    try {
      const r = await vtdAction("stop");
      await vtdStatus();
      if (r && r.ok === false) setVtdOverlay("停止失败: " + (r.reason || r.state), true);
      else setVtdOverlay("已停止", false);
    } catch (e) { setVtdOverlay("停止失败: " + e.message, true); }
  });

  const detStartBtn = document.getElementById("vtd-detector-start");
  const detStopBtn  = document.getElementById("vtd-detector-stop");
  const evClearBtn  = document.getElementById("vtd-events-clear");
  if (detStartBtn) detStartBtn.addEventListener("click", vtdDetectorStart);
  if (detStopBtn)  detStopBtn.addEventListener("click",  vtdDetectorStop);
  if (evClearBtn)  evClearBtn.addEventListener("click",  vtdEventsClear);

  refreshVtdDetector();
  // 视频目标检测页不再有工具集合概念，因此不再调用 ``vtdLoadTools()``。
  if (!VTD.ws && !VTD.wsTimer) connectVtdWs();
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
  // 缓存 HW capability 当前状态（DOM 改 checkbox 时同步到此结构）
  _hwCaps: [],

  init() {
    if (this.initialized) return;
    this.initialized = true;

    const form = document.getElementById("base-config-form");
    const refreshBtn = document.getElementById("base-config-refresh");
    const reloadBtn = document.getElementById("bc-reload");

    if (refreshBtn) refreshBtn.addEventListener("click", () => this.load());
    if (reloadBtn) reloadBtn.addEventListener("click", () => this.load());

    // Track edits so "放弃修改" actually drops them.
    form.addEventListener("input", () => {
      this.dirty = true;
      this.updateFieldStatus();
    });
    form.addEventListener("change", () => {
      this.dirty = true;
    });

    if (form) {
      form.addEventListener("submit", (e) => {
        e.preventDefault();
        this.save();
      });
    }
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
    // 异步拉 HW capability 状态并渲染到系统配置页
    try {
      const r2 = await fetch(API_PREFIX + "/compute/capabilities");
      if (r2.ok) {
        const caps = (await r2.json()).hw_capabilities || [];
        this.renderHwCaps(caps);
      } else {
        this.renderHwCapsError("加载失败: HTTP " + r2.status);
      }
    } catch (e2) {
      this.renderHwCapsError("加载失败: " + e2.message);
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

  renderHwCaps(caps) {
    this._hwCaps = caps.map((c) => ({ ...c }));
    const root = document.getElementById("bc-hw-caps-list");
    if (!root) return;
    if (!caps.length) {
      root.innerHTML = `<div class="bc-status">无可配置项</div>`;
      return;
    }
    root.innerHTML = "";
    for (const c of this._hwCaps) {
      const card = document.createElement("label");
      card.className = "bc-hw-cap";
      card.dataset.cap = c.capability_type;
      card.innerHTML = `
        <input type="checkbox" ${c.enabled ? "checked" : ""} />
        <div class="bc-hw-cap-body">
          <div class="bc-hw-cap-title">${c.label}</div>
          <div class="bc-hw-cap-type">${c.capability_type} · ${c.class_name}</div>
          <div class="bc-hw-cap-desc">${c.description}</div>
        </div>
      `;
      const cb = card.querySelector('input[type="checkbox"]');
      cb.addEventListener("change", () => {
        c.enabled = cb.checked;
        this.dirty = true;
      });
      root.appendChild(card);
    }
  },

  renderHwCapsError(msg) {
    const root = document.getElementById("bc-hw-caps-list");
    if (!root) return;
    root.innerHTML = `<div class="bc-status danger">${msg}</div>`;
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

      // HW capability checkbox 持久化：与“注册发现”页共用同一个 endpoint。
      // 只在有本地缓存时才发请求（首次加载后才有 _hwCaps）。
      if (this._hwCaps && this._hwCaps.length) {
        const updates = {};
        for (const c of this._hwCaps) updates[c.capability_type] = !!c.enabled;
        try {
          const r2 = await fetch(API_PREFIX + "/compute/hardware-capabilities", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ updates }),
          });
          if (r2.ok) {
            const body2 = await r2.json();
            if (Array.isArray(body2.hw_capabilities)) {
              this._hwCaps = body2.hw_capabilities.map((c) => ({ ...c }));
              this.renderHwCaps(this._hwCaps);
            }
            parts.push(`硬件能力已保存（${body2.applied.length} 项）`);
          } else {
            parts.push("⚠ 硬件能力保存失败");
          }
        } catch {
          parts.push("⚠ 硬件能力保存失败");
        }
      }

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

  if (name === "video-target-detection") {
    initVideoTargetDetection();
    await vtdStatus();
    return;
  }

  if (name === "video-file-analysis") {
    VFA.init();
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

  if (name === "video-content-search") {
    VCS.init();
    if (!VCS.ws && !VCS.wsTimer) connectVcsWs();
    return;
  }

  if (name === "compute-config") {
    ComputeConfigPage.init();
    await ComputeConfigPage.load();
    ComputeConfigPage.startTicking();
    return;
  }

  if (name === "discovery") {
    await Discovery.load();
    // Discovery.load() 内部已经 await DiscoveryDds.load()；再启 1Hz 轮询
    // 让 enabled 时实时刷新 discovered / state。
    DiscoveryDds.startTicking();
    return;
  }

  if (name === "im-bridge") {
    ImBridge.init();
    await ImBridge.load();
    ImBridge.ensureWs();
    return;
  }

  if (name === "text-chat") {
    TextChat.init();
    await TextChat.refresh();
    TextChat.ensureWs();
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
  // Track previous page so we can stop page-local resources (e.g.
  // ComputeConfigPage's 1Hz countdown tick) when navigating away.
  const prev = currentPage();
  if (prev === "compute-config" && name !== "compute-config") {
    ComputeConfigPage.stopTicking();
  }
  if (prev === "discovery" && name !== "discovery") {
    DiscoveryDds.stopTicking();
  }
  if (prev === "im-bridge" && name !== "im-bridge") {
    ImBridge.closeWs();
  }
  if (prev === "text-chat" && name !== "text-chat") {
    TextChat.closeWs();
  }
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

/* ------------------------------------------------------------------ *
 *  DemoGuard: 演示页 (音视频描述 / 视频目标检测 / 视频内容检索 /     *
 *  语音生成) 离开时的"必须关闭后台"守卫。                              *
 * ------------------------------------------------------------------ *
 *  三处入口：                                                          *
 *    1. 点击 sidebar 切换页面：confirm + await 后端 stop              *
 *    2. beforeunload（关 tab / 刷新 / 关闭浏览器）：原生对话框 +      *
 *       sendBeacon/keepalive 兜底发停                                  *
 *    3. popstate（前进/后退）：静默 fire-and-forget 停后端            *
 * ------------------------------------------------------------------ */
const DemoGuard = {
  _ACTIVE_STATES: ["opening", "playing", "paused", "eof"],

  // 每页：判定 + 关闭（await 版，给 in-app 导航用）
  _registry: {
    "video-detection": {
      active(snap = {}) {
        const dec = snap.session && this._ACTIVE_STATES.includes(snap.session.state);
        const det = snap.detector && snap.detector.enabled;
        return !!(dec || det);
      },
      async stop() {
        await Promise.allSettled([
          fetch(API_PREFIX + "/video-detection/close",       { method: "POST" }),
          fetch(API_PREFIX + "/video-detection/prompt/stop", { method: "POST" }),
        ]);
      },
    },
    "video-target-detection": {
      active(snap = {}) {
        const dec = snap.session && this._ACTIVE_STATES.includes(snap.session.state);
        const det = snap.detector && snap.detector.enabled;
        return !!(dec || det);
      },
      async stop() {
        await Promise.allSettled([
          fetch(API_PREFIX + "/video-target-detection/close",       { method: "POST" }),
          fetch(API_PREFIX + "/video-target-detection/prompt/stop", { method: "POST" }),
        ]);
      },
    },
    "video-file-analysis": {
      // VFA 后台三阶段：opening → running → summarizing → done。
      // 综述阶段还要继续推 WS 占着 / 调 LLM，离开必须停。
      active(snap = {}) {
        const s = snap.session;
        return !!(s && (s.state === "running" || s.state === "opening" ||
                        s.state === "summarizing"));
      },
      async stop() {
        try {
          await fetch(API_PREFIX + "/video-file-analysis/stop", { method: "POST" });
        } catch (_) { /* beforeunload path: best-effort */ }
      },
    },
    "tts": {
      active(snap = {}) {
        const s = snap.session;
        return !!(s && (s.busy === true || s.running === true ||
                        s.state === "loading" || s.state === "running"));
      },
      async stop() {
        try {
          await fetch(API_PREFIX + "/tts/stop", { method: "POST" });
        } catch (_) { /* beforeunload path: best-effort */ }
      },
    },
    "video-content-search": {
      active(snap = {}) {
        const v = snap.vcs || {};
        const extracting = v.state === "extracting";
        return !!(extracting || snap.playing);
      },
      async stop() {
        try {
          await fetch(API_PREFIX + "/video-content-search/extract/stop", { method: "POST" });
        } catch (_) { /* beforeunload path: best-effort */ }
      },
    },
  },

  // 各页模块主动 push 最新快照
  _cache: {},
  report(page, snap) {
    this._cache[page] = snap || {};
  },

  isActive(page) {
    const reg = this._registry[page];
    if (!reg) return false;
    return !!reg.active.call(this, this._cache[page] || {});
  },

  // 同步版：beforeunload 不能 await，走 sendBeacon + keepalive
  stopSync(page) {
    const map = {
      "video-detection": [
        [API_PREFIX + "/video-detection/close",       "POST"],
        [API_PREFIX + "/video-detection/prompt/stop", "POST"],
      ],
      "video-target-detection": [
        [API_PREFIX + "/video-target-detection/close",       "POST"],
        [API_PREFIX + "/video-target-detection/prompt/stop", "POST"],
      ],
      "video-file-analysis": [[API_PREFIX + "/video-file-analysis/stop", "POST"]],
      "tts": [[API_PREFIX + "/tts/stop", "POST"]],
      "video-content-search": [[API_PREFIX + "/video-content-search/extract/stop", "POST"]],
    };
    const urls = map[page] || [];
    const body = new Blob(["{}"], { type: "application/json" });
    for (const [url, method] of urls) {
      try {
        if (navigator.sendBeacon) {
          navigator.sendBeacon(url, body);
        } else {
          fetch(url, { method, keepalive: true, body: "{}" });
        }
      } catch (_) { /* swallow — best-effort */ }
    }
  },

  async confirmLeave(fromPage) {
    if (!this.isActive(fromPage)) return true;
    const labels = {
      "video-detection":        "音视频描述",
      "video-target-detection": "视频目标检测",
      "video-content-search":   "视频内容检索",
      "video-file-analysis":    "视频文件分析",
      "tts":                    "语音生成",
    };
    const msg =
      `【${labels[fromPage] || fromPage}】后台正在运行。\n\n` +
      `离开此页面将自动停止当前任务并释放算力。\n确定继续吗？`;
    return window.confirm(msg);
  },

  // stop 后清理缓存，避免兜底 hashchange 监听把同一组 POST 再发一遍
  async stopAndClear(page) {
    const reg = this._registry[page];
    if (reg) await reg.stop();
    this._cache[page] = { session: null, detector: null };
  },
};

document.querySelectorAll(".nav-item").forEach(a => {
  a.addEventListener("click", async (e) => {
    const target = a.dataset.page;
    const cur = currentPage();
    if (!target || target === cur) return;
    if (!DemoGuard.isActive(cur)) return;
    if (!await DemoGuard.confirmLeave(cur)) {
      e.preventDefault();
      return;
    }
    e.preventDefault();
    await DemoGuard.stopAndClear(cur);
    location.hash = "#" + target;
  });
});

window.addEventListener("beforeunload", (e) => {
  const cur = currentPage();
  if (!DemoGuard.isActive(cur)) return;
  DemoGuard.stopSync(cur);
  e.preventDefault();
  e.returnValue = "后台任务正在运行；离开将自动停止并释放算力。";
  return e.returnValue;
});

// 浏览器前进/后退（hash 变化但不经过 .nav-item click）：静默 stopOnLeave 兜底
window.addEventListener("hashchange", () => {
  // 仅作为兜底：若当前已切到非演示页，而旧页（现在 currentPage 反映不出来，
  // 但 DemoGuard._cache 仍记着）有后台残留，sendBeacon 兜底。
  // 真正的拦截逻辑已在 nav-item click 路径里。
  const cur = currentPage();
  if (DemoGuard._registry[cur]) return;     // 当前是已注册页，不动
  // 切到非演示页（system-monitor / base-config / ...）：遍历注册表兜底
  for (const page of Object.keys(DemoGuard._registry)) {
    if (DemoGuard.isActive(page)) DemoGuard.stopSync(page);
  }
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
    this.lastSnap = snap;
    if (typeof DemoGuard !== "undefined") {
      DemoGuard.report("tts", { session: snap });
    }
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

// Wire the video-detection file picker modal's own buttons (close /
// pick dir / pick file) at module load so they work no matter which
// page triggered the modal.
{
  const closeBtn = document.getElementById("vd-modal-close");
  const pickDirBtn = document.getElementById("vd-modal-pick-dir");
  const pickFileBtn = document.getElementById("vd-modal-pick-file");
  if (closeBtn) closeBtn.addEventListener("click", () => VdFsModal.close());
  if (pickDirBtn) pickDirBtn.addEventListener("click", () => VdFsModal.pickCurrentDir());
  if (pickFileBtn) pickFileBtn.addEventListener("click", () => VdFsModal.pickSelectedFile());
}

// File picker modal picks into whichever source input set _sourceTarget
// before opening (currently only the vtd page does so); otherwise
// default to the 音视频描述 page's #vd-source.
VdFsModal._sourceTarget = null;
const _vdOrigPickFile = VdFsModal.pickSelectedFile.bind(VdFsModal);
VdFsModal.pickSelectedFile = function () {
  if (VdFsModal._sourceTarget) {
    if (this.selected) {
      VdFsModal._sourceTarget.value = this.selected.path;
      // 程序化赋 .value 不触发 input 事件；补发一个让挂在该输入框
      // 上的 "已选: xxx" meta / 校验逻辑同步刷新（VCS / VFA / VTD）。
      VdFsModal._sourceTarget.dispatchEvent(new Event("input", { bubbles: true }));
      VdFsModal._sourceTarget = null;
      this.close();
    }
    return;
  }
  _vdOrigPickFile();
};
const _vdOrigPickDir = VdFsModal.pickCurrentDir.bind(VdFsModal);
VdFsModal.pickCurrentDir = function () {
  if (VdFsModal._sourceTarget) {
    const btn = document.getElementById("vd-modal-pick-dir");
    const p = btn && btn.dataset.path;
    if (p) {
      VdFsModal._sourceTarget.value = p;
      VdFsModal._sourceTarget.dispatchEvent(new Event("input", { bubbles: true }));
      VdFsModal._sourceTarget = null;
      this.close();
    }
    return;
  }
  _vdOrigPickDir();
};

/* ---------------------------------------------------------------- *
 * 算力配置 (Compute Config) page                                   *
 * ---------------------------------------------------------------- */
const ComputeConfigPage = {
  initialized: false,
  _tickHandle: null,
  _snapshot: null,

  init() {
    if (this.initialized) return;
    this.initialized = true;
    document.getElementById("cc-refresh")?.addEventListener("click", () => this.load());
    document.getElementById("cc-stop-all")?.addEventListener("click", () => this.stopAll());
  },

  async load() {
    if (this._loading) return;
    this._loading = true;
    try {
      const r = await fetch(API_PREFIX + "/compute", {
        cache: "no-store",
        headers: {"Cache-Control": "no-cache"},
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      this._snapshot = await r.json();
      this.render();
    } catch (e) {
      this.setFoot("加载失败: " + e.message, "danger");
    } finally {
      this._loading = false;
    }
  },

  startTicking() {
    if (this._tickHandle) return;
    this._tickHandle = setInterval(() => this.tick(), 1000);
  },

  stopTicking() {
    if (this._tickHandle) {
      clearInterval(this._tickHandle);
      this._tickHandle = null;
    }
  },

  tick() {
    // 1Hz 拉新数据 —— 只有这样才能让 queued/loading → running/error 的
    // 状态变化自动反映到 UI（local render 只重画倒计时秒数，不会感知
    // status 字段变化）。
    if (!this._snapshot) return;
    this.load();
  },

  async toggle(npu, model, enabled) {
    const url = API_PREFIX + "/compute/instances";
    const method = enabled ? "POST" : "DELETE";
    try {
      const r = await fetch(url, {
        method,
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({npu_index: npu, model}),
      });
      if (!r.ok) {
        const text = await r.text();
        alert(`${enabled ? "启用" : "停用"}失败: ${text}`);
      }
    } catch (e) {
      alert("请求失败: " + e.message);
    }
    await this.load();
  },

  async stopAll() {
    if (!confirm("确定要停止所有算力实例吗？")) return;
    try {
      const r = await fetch(API_PREFIX + "/compute/stop-all", {method: "POST"});
      if (!r.ok) {
        const text = await r.text();
        alert("全部停止失败: " + text);
      }
    } catch (e) {
      alert("请求失败: " + e.message);
    }
    await this.load();
  },

  // -------------------------------------------------------------- render
  render() {
    this._renderBanners();
    this._renderCols();
    this._renderSummary();
    this._renderModelStates(this._snapshot);
    this._renderTools(this._snapshot);
  },

  // -------------------------------------------------------------- banners
  // 顶部告警条：错误实例 / 卡住的 loading / worker 死亡 —— 一眼能扫到，
  // 避免被埋在每行的小字 <pre class="cc-model-error"> 里看不到。每秒
  // tick 都重新渲染（_renderSummary 一样），状态变化即时反映。
  _renderBanners() {
    const stack = document.getElementById("cc-banner-stack");
    if (!stack) return;
    const snap = this._snapshot || {};
    const errored = (snap.instances || []).filter(i => i.status === "error");
    const stuck = this._detectStuck(snap);
    const workerDead = snap.worker_alive === false && (snap.loading_count || 0) > 0;

    stack.innerHTML = "";

    if (errored.length) {
      const ul = document.createElement("ul");
      ul.className = "cc-banner-list";
      for (const inst of errored) {
        const li = document.createElement("li");
        const head = document.createElement("div");
        head.textContent = `${inst.label || inst.model} · NPU #${inst.npu_index}`;
        li.appendChild(head);
        const msg = document.createElement("pre");
        msg.className = "cc-banner-msg";
        msg.textContent = inst.error || "(无错误信息)";
        li.appendChild(msg);
        if (inst.stderr_full && inst.stderr_full.trim()) {
          const details = document.createElement("details");
          details.className = "cc-banner-details";
          const summary = document.createElement("summary");
          summary.textContent = "查看完整 stderr";
          details.appendChild(summary);
          const full = document.createElement("pre");
          full.className = "cc-banner-stderr";
          full.textContent = inst.stderr_full;
          details.appendChild(full);
          li.appendChild(details);
        }
        ul.appendChild(li);
      }
      stack.appendChild(this._makeBanner({
        kind: "is-error",
        icon: "✖",
        title: `${errored.length} 个算力实例启动失败`,
        body: ul,
        actions: [
          {label: "全部清空", onClick: () => this.stopAll()},
        ],
      }));
    }

    if (stuck.length) {
      const ul = document.createElement("ul");
      ul.className = "cc-banner-list";
      for (const s of stuck) {
        const li = document.createElement("li");
        li.textContent = `${s.label} · NPU #${s.npu_index} · ${s.status} 累计 ${s.elapsed}s`;
        ul.appendChild(li);
      }
      stack.appendChild(this._makeBanner({
        kind: "is-warn",
        icon: "⚠",
        title: `${stuck.length} 个实例加载疑似卡住`,
        body: ul,
        actions: [
          {label: "刷新", onClick: () => this.load()},
        ],
      }));
    }

    if (workerDead) {
      const body = document.createElement("div");
      body.className = "cc-banner-msg";
      body.textContent =
        "后台加载 worker 已停止响应。当前 loading 实例会一直停在加载中，" +
        "需要重启服务或重新启用实例才会恢复。";
      stack.appendChild(this._makeBanner({
        kind: "is-dead",
        icon: "✖",
        title: "算力加载 worker 已停止",
        body,
      }));
    }
  },

  _makeBanner({kind, icon, title, body, actions}) {
    const el = document.createElement("div");
    el.className = `cc-banner ${kind}`;
    const iconEl = document.createElement("span");
    iconEl.className = "cc-banner-icon";
    iconEl.textContent = icon;
    const bodyEl = document.createElement("div");
    bodyEl.className = "cc-banner-body";
    const t = document.createElement("div");
    t.className = "cc-banner-title";
    t.textContent = title;
    bodyEl.appendChild(t);
    if (body) bodyEl.appendChild(body);
    el.appendChild(iconEl);
    el.appendChild(bodyEl);
    if (actions && actions.length) {
      const acts = document.createElement("div");
      acts.className = "cc-banner-actions";
      for (const a of actions) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "cc-banner-btn";
        btn.textContent = a.label;
        btn.addEventListener("click", a.onClick);
        acts.appendChild(btn);
      }
      el.appendChild(acts);
    }
    return el;
  },

  // 检测疑似卡住：queued/loading 累计时间超过 (2 × estimate + 30s)，且
  // 当前没有错误实例（避免和 error banner 重复提示）。返回数组 [{...}]。
  _detectStuck(snap) {
    const now = Date.now() / 1000;
    const perModel = snap.estimated_init_seconds_per_model || {};
    const out = [];
    for (const inst of snap.instances || []) {
      if (inst.status !== "queued" && inst.status !== "loading") continue;
      const est = perModel[inst.model] ?? snap.default_estimated_init_seconds ?? 10;
      const startTs = inst.status === "queued"
        ? (inst.queued_at || 0)
        : (inst.started_at || inst.queued_at || 0);
      const elapsed = Math.max(0, Math.round(now - startTs));
      const threshold = est * 2 + 30;
      if (elapsed >= threshold) {
        out.push({
          ...inst,
          elapsed,
          threshold,
        });
      }
    }
    return out;
  },

  _renderCols() {
    const root = document.getElementById("cc-panels");
    if (!root) return;
    root.innerHTML = "";
    const cards = this._snapshot?.cards || [];
    const byIdx = new Map(cards.map(c => [c.npu_index, c]));

    // 三栏固定：RK3588（主控 SoC，占位）+ RK182x #0 + RK182x #1
    this._makeCol("rk3588", "RK3588", "主控 SoC", null, root);
    for (const idx of [0, 1]) {
      const card = byIdx.get(idx);
      this._makeCol(
        `rk182x-${idx}`,
        `RK182x #${idx}`,
        card ? "已用 0 MB" : "未检测到",
        card ? idx : null,
        root,
        !card,
      );
    }

    const failed = this._snapshot?.cards_failed || "";
    if (failed) {
      this.setFoot("枚举 NPU 卡失败: " + failed, "danger");
    }
  },

  _makeCol(colKey, title, memText, npuIndex, root, missing) {
    const col = document.createElement("section");
    col.className = "cc-col";
    col.dataset.col = colKey;

    const head = document.createElement("header");
    head.className = "cc-col-head";
    const titleEl = document.createElement("span");
    titleEl.className = "cc-col-title";
    titleEl.textContent = title;
    const memEl = document.createElement("span");
    memEl.className = "cc-col-mem";
    memEl.textContent = memText;
    if (npuIndex !== null && npuIndex !== undefined) {
      memEl.dataset.memFor = String(npuIndex);
    }
    head.appendChild(titleEl);
    head.appendChild(memEl);
    col.appendChild(head);

    const body = document.createElement("div");
    body.className = "cc-col-body";
    if (npuIndex === null || npuIndex === undefined) {
      // RK3588 —— 列出内置工具（默认启用，仅展示依赖状态，不可禁用）。
      const ul = document.createElement("ul");
      ul.className = "cc-tools-list";
      ul.id = "cc-tools-list";
      body.appendChild(ul);
    } else if (missing) {
      // 这张 182x 卡没检测到
      const ph = document.createElement("div");
      ph.className = "cc-placeholder";
      ph.textContent = "未检测到该 NPU 卡";
      body.appendChild(ph);
    } else {
      const ul = document.createElement("ul");
      ul.className = "cc-models";
      ul.id = `cc-models-${npuIndex}`;
      body.appendChild(ul);
    }
    col.appendChild(body);
    root.appendChild(col);

    if (npuIndex !== null && npuIndex !== undefined && !missing) {
      this._renderModels(npuIndex);
    }
  },

  _renderModels(npuIndex) {
    const ul = document.getElementById(`cc-models-${npuIndex}`);
    if (!ul) return;
    ul.innerHTML = "";
    const supported = this._snapshot?.supported_models || [];
    const instances = this._snapshot?.instances || [];
    for (const m of supported) {
      const inst = instances.find(i => i.npu_index === npuIndex && i.model === m.id);
      const li = document.createElement("li");
      li.className = "cc-model";
      li.dataset.npu = npuIndex;
      li.dataset.model = m.id;
      const enabled = !!inst && inst.status !== "stopped";
      const existsFlag = m.exists !== false;
      // 整行状态类：is-running 蓝边 / has-error 红边 —— 视觉上让"toggle ON 但实际 error"
      // 这种坑爹情况一眼能看出来。
      if (inst) {
        if (inst.status === "running") li.classList.add("is-running");
        if (inst.status === "error")   li.classList.add("has-error");
        if (inst.status === "queued" || inst.status === "loading") li.classList.add("is-pending");
      }
      li.innerHTML = `
        <label class="cc-toggle">
          <input type="checkbox" data-role="toggle" ${enabled ? "checked" : ""} ${existsFlag ? "" : "disabled"} />
          <span class="cc-toggle-track"></span>
        </label>
        <div class="cc-model-meta">
          <strong>${m.label}</strong>
          <span class="cc-model-cap">${(m.capability_types || []).join(" · ")}</span>
          <span class="cc-model-mem">~${m.memory_estimate_mb} MB</span>
        </div>
        <div class="cc-model-state" data-state="${inst ? inst.status : "disabled"}">
          <span class="cc-state-text">${this._statusLabel(inst)}</span>
        </div>
      `;
      // error 时把错误信息单独拎一行显眼显示（默认在 state 里只是 inline 文字）
      if (inst && inst.status === "error") {
        li.appendChild(this._makeErrorBlock(inst, npuIndex, m.id));
      }
      if (!existsFlag) {
        const missing = document.createElement("div");
        missing.className = "cc-model-missing";
        missing.textContent = "模型目录不存在";
        li.appendChild(missing);
      }
      const checkbox = li.querySelector("input[type=checkbox]");
      checkbox.addEventListener("change", () => this.toggle(npuIndex, m.id, checkbox.checked));
      ul.appendChild(li);
    }
  },

  _renderModelStates(snapshot) {
    if (!snapshot) return;
    const perCard = snapshot.per_card_memory_mb || {};
    for (const [npuIndex, mb] of Object.entries(perCard)) {
      const el = document.querySelector(`[data-mem-for="${npuIndex}"]`);
      if (el) el.textContent = `已用 ${mb} MB`;
    }
    const ulMap = document.querySelectorAll(".cc-model");
    ulMap.forEach(li => {
      const npu = parseInt(li.dataset.npu, 10);
      const model = li.dataset.model;
      const inst = (snapshot.instances || []).find(i => i.npu_index === npu && i.model === model);
      const stateDiv = li.querySelector(".cc-model-state");
      if (!stateDiv) return;
      const newState = inst ? inst.status : "disabled";
      stateDiv.dataset.state = newState;
      const text = stateDiv.querySelector(".cc-state-text");
      if (text) text.textContent = this._statusLabel(inst);
      // 整行状态类同步（status 变化时给整行换边框颜色）
      li.classList.toggle("is-running", newState === "running");
      li.classList.toggle("has-error", newState === "error");
      li.classList.toggle("is-pending", newState === "queued" || newState === "loading");
      // error 信息：增/删 <pre> 节点
      let errBox = li.querySelector(".cc-model-error-wrap");
      if (inst && inst.status === "error") {
        if (!errBox) {
          errBox = this._makeErrorBlock(inst, npu, model);
          li.appendChild(errBox);
        } else {
          // 错误文本 / 按钮都在 _makeErrorBlock 里重建，避免就地改 text 的细碎 DOM。
          const newBox = this._makeErrorBlock(inst, npu, model);
          errBox.replaceWith(newBox);
        }
      } else if (errBox) {
        errBox.remove();
      }
    });
  },

  // 构造一条实例的错误块：<pre> 错误摘要 + "查看完整 stderr" 折叠 +
  // 重试按钮。重试按钮 = 先 DELETE 再 POST，等同于重新启用。
  _makeErrorBlock(inst, npuIndex, model) {
    const wrap = document.createElement("div");
    wrap.className = "cc-model-error-wrap";
    const errBox = document.createElement("pre");
    errBox.className = "cc-model-error";
    errBox.textContent = inst.error || "(无错误信息)";
    wrap.appendChild(errBox);
    if (inst.stderr_full && inst.stderr_full.trim()) {
      const details = document.createElement("details");
      details.className = "cc-model-error-details";
      const summary = document.createElement("summary");
      summary.textContent = "查看完整 stderr";
      details.appendChild(summary);
      const full = document.createElement("pre");
      full.className = "cc-model-error-stderr";
      full.textContent = inst.stderr_full;
      details.appendChild(full);
      wrap.appendChild(details);
    }
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "cc-banner-btn cc-model-error-retry";
    retry.textContent = "重试";
    retry.addEventListener("click", async () => {
      retry.disabled = true;
      try {
        await this.toggle(npuIndex, model, false);
        await this.toggle(npuIndex, model, true);
      } finally {
        retry.disabled = false;
      }
    });
    wrap.appendChild(retry);
    return wrap;
  },

  _statusLabel(inst) {
    if (!inst || inst.status === "stopped") return "禁用";
    if (inst.status === "queued") {
      const waitSec = this._countdownSec(inst, "queued");
      return `排队中，预计 ${waitSec}s`;
    }
    if (inst.status === "loading") {
      const left = this._countdownSec(inst, "loading");
      return `初始化中... ${left}s`;
    }
    if (inst.status === "running") return "运行中";
    if (inst.status === "error") return "启动失败";
    return inst.status;
  },

  _estimateFor(inst) {
    const perModel = this._snapshot?.estimated_init_seconds_per_model || {};
    return perModel[inst?.model] ?? this._snapshot?.default_estimated_init_seconds ?? 10;
  },

  _countdownSec(inst, kind) {
    const est = this._estimateFor(inst);
    if (kind === "queued") {
      // worker 是单线程串行，同 npu 内严格按 queued_at 顺序。
      // 跨 npu 理论上能并行，但当前 worker 一次只跑一条，所以也算"队列"里。
      // 用 queued_at 升序算"前面还有几条"。
      const inQueue = (this._snapshot?.instances || [])
        .filter(i => i.status === "queued" || i.status === "loading")
        .sort((a, b) => (a.queued_at || 0) - (b.queued_at || 0));
      const idx = inQueue.findIndex(i => i.npu_index === inst.npu_index && i.model === inst.model);
      const ahead = idx < 0 ? 0 : idx;
      return Math.max(0, ahead) * est;
    }
    if (kind === "loading") {
      const elapsed = Math.max(0, Date.now() / 1000 - (inst.started_at || 0));
      return Math.max(0, Math.round(est - elapsed));
    }
    return 0;
  },

  // 渲染 RK3588 列的内置工具列表（从 snapshot.tools 取数据）。
  // 默认全部启用 —— 用户不能从这一列 toggle；可用性仅取决于依赖 capability 是否启用。
  _renderTools(snapshot) {
    const ul = document.getElementById("cc-tools-list");
    if (!ul) return;
    const tools = (snapshot && snapshot.tools) || [];
    ul.innerHTML = "";
    if (!tools.length) {
      const empty = document.createElement("li");
      empty.className = "cc-tools-empty";
      empty.textContent = "当前没有可用工具";
      ul.appendChild(empty);
      return;
    }
    for (const t of tools) {
      const li = document.createElement("li");
      li.className = "cc-tool-card";
      const head = document.createElement("div");
      head.className = "cc-tool-card-head";
      const name = document.createElement("span");
      name.className = "cc-tool-card-name";
      name.textContent = t.name;
      const key = document.createElement("span");
      key.className = "cc-tool-card-key";
      key.textContent = t.key;
      const badge = document.createElement("span");
      badge.className = "cc-tool-card-badge is-on";
      badge.textContent = "已启用";
      head.appendChild(name);
      head.appendChild(key);
      head.appendChild(badge);
      const desc = document.createElement("div");
      desc.className = "cc-tool-card-desc";
      desc.textContent = t.description;
      const state = document.createElement("div");
      if (t.requires_capability) {
        if (t.available) {
          state.className = "cc-tool-card-state is-ok";
          state.textContent = `✓ 可用 · 依赖 ${t.requires_capability}`;
        } else {
          state.className = "cc-tool-card-state is-bad";
          state.textContent = `⚠ 依赖未启用 · ${t.reason || t.requires_capability}`;
          badge.classList.remove("is-on");
          badge.classList.add("is-off");
          badge.textContent = "不可用";
        }
      } else {
        state.className = "cc-tool-card-state is-neutral";
        state.textContent = "无外部依赖";
      }
      li.appendChild(head);
      li.appendChild(desc);
      li.appendChild(state);
      ul.appendChild(li);
    }
  },

  _renderSummary() {
    const snap = this._snapshot || {};
    const cards = snap.cards || [];
    const sup = snap.supported_models || [];
    const running = snap.running_count || 0;
    const queued  = snap.queued_count  || 0;
    const loading = snap.loading_count || 0;
    const error   = snap.error_count   || 0;
    const totalMb = snap.total_memory_mb || 0;
    const parts = [
      `${cards.length} 张算力卡`,
      `${sup.length} 个候选模型`,
      `${running} 运行中`,
      error   ? `${error} 失败`  : null,
      loading ? `${loading} 加载中` : null,
      queued  ? `${queued} 排队中`  : null,
      `已用 ${totalMb} MB`,
    ].filter(Boolean);
    const summary = parts.join(" · ");
    const at = document.getElementById("cc-collected-at");
    if (at) at.textContent = summary;
    const last = snap.last_updated;
    if (last) {
      const ago = Math.max(0, Math.round(Date.now() / 1000 - last));
      this.setFoot(`数据 ${ago} 秒前刷新`, ago > 5 ? "warn" : "");
    }
  },

  setFoot(msg, kind) {
    const el = document.getElementById("cc-foot");
    if (!el) return;
    el.className = "cc-foot " + (kind || "");
    el.textContent = msg || "";
  },
};

/* ---------------------------------------------------------------- *
 * 注册发现 (Discovery) page — 读 registry，不调 wdndds              *
 * ---------------------------------------------------------------- */
const Discovery = {
  // 缓存上一次 HW 渲染状态（用户改 checkbox 但未保存时需要回填 / diff）
  _hwCaps: [],
  _hwDirty: false,

  init() {
    if (this.initialized) return;
    this.initialized = true;

    const enableAll  = document.getElementById("discovery-hw-enable-all");
    const disableAll = document.getElementById("discovery-hw-disable-all");
    const saveBtn    = document.getElementById("discovery-hw-save");

    if (enableAll)  enableAll .addEventListener("click", () => this.setAllHw(true));
    if (disableAll) disableAll.addEventListener("click", () => this.setAllHw(false));
    if (saveBtn)    saveBtn   .addEventListener("click", () => this.saveHw());
  },

  async load() {
    this.init();
    try {
      const r = await fetch(API_PREFIX + "/compute/capabilities");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      this.render(data);
    } catch (e) {
      const list = document.getElementById("discovery-list");
      if (list) list.innerHTML = `<div class="page-placeholder danger">加载失败: ${e.message}</div>`;
    }
    // DDS 面板独立拉，失败也不影响 HW/SW 渲染
    DiscoveryDds.load().catch((e) => {
      const banner = document.getElementById("discovery-dds-runtime-banner");
      if (banner) {
        banner.className = "discovery-runtime-banner is-danger";
        banner.textContent = "DDS 面板加载失败: " + e.message;
      }
    });
  },

  render(data) {
    const swCaps = (data && data.sw_capabilities) || [];
    const hwCaps = (data && data.hw_capabilities) || [];

    this._hwCaps = hwCaps.map((c) => ({ ...c }));
    this._hwDirty = false;
    this._renderHwPane();
    this._renderSwPane(swCaps);
  },

  _renderSwPane(caps) {
    const at = document.getElementById("discovery-collected-at");
    const list = document.getElementById("discovery-list");
    const summary = document.getElementById("discovery-sw-summary");
    const hint = document.getElementById("discovery-sw-hint");
    if (!list) return;
    if (hint) hint.textContent = `${caps.length} 个 SW capability`;
    if (at) at.textContent = `SW ${caps.length} · HW ${this._hwCaps.length}`;
    if (summary) {
      summary.textContent = caps.length === 0
        ? "当前没有 SW capability —— 到 算力配置 启用模型后这里会显示。"
        : "运行中实例暴露的软件能力（SW）。每个能力由一个 RKNN 模型承载。";
    }
    if (caps.length === 0) {
      list.innerHTML = `
        <div class="page-placeholder">
          <div class="page-placeholder-icon" aria-hidden="true">⌖</div>
          <div class="page-placeholder-title">等待算力实例</div>
          <div class="page-placeholder-text">
            启用 算力配置 中的实例后，这里会列出对应的能力。
          </div>
        </div>`;
      return;
    }
    list.innerHTML = "";
    for (const c of caps) {
      const card = document.createElement("article");
      card.className = "discovery-card";
      card.innerHTML = `
        <header class="discovery-card-head">
          <span class="discovery-cap-type">${c.type}</span>
          <code class="discovery-cap-name">${c.name}</code>
        </header>
        <div class="discovery-card-meta">
          <span>来自 <strong>${c.model}</strong></span>
          <span>NPU #${c.npu_index}</span>
          <span>~${c.memory_estimate_mb} MB</span>
        </div>
      `;
      list.appendChild(card);
    }
  },

  _renderHwPane() {
    const list = document.getElementById("discovery-hw-list");
    const summary = document.getElementById("discovery-hw-summary");
    const hint = document.getElementById("discovery-hw-hint");
    const saveBtn = document.getElementById("discovery-hw-save");
    if (!list) return;

    const enabledCount = this._hwCaps.filter((c) => c.enabled).length;
    if (hint) hint.textContent = `${enabledCount} / ${this._hwCaps.length} 已启用`;
    if (summary) {
      summary.textContent = this._hwDirty
        ? "有未保存的修改 —— 点“保存修改”写入 config.json。"
        : "本机硬件能力（HW）配置；勾选/取消勾选后保存即生效。";
    }
    if (saveBtn) {
      saveBtn.disabled = !this._hwDirty;
      saveBtn.classList.toggle("vd-btn-primary", this._hwDirty);
    }

    list.innerHTML = "";
    for (const c of this._hwCaps) {
      const card = document.createElement("article");
      card.className = "discovery-card" + (c.enabled ? "" : " is-disabled");
      card.dataset.cap = c.capability_type;
      card.innerHTML = `
        <header class="discovery-card-head">
          <span class="discovery-cap-type is-hw">${c.capability_type}</span>
          <code class="discovery-cap-name">${c.class_name}</code>
          <label class="toggle" title="${c.enabled ? "点击禁用" : "点击启用"}">
            <input type="checkbox" ${c.enabled ? "checked" : ""} />
            <span class="toggle-track"></span>
          </label>
        </header>
        <div class="discovery-card-meta">
          <span>类别：<strong>${c.category}</strong></span>
          <span class="discovery-card-status ${c.enabled ? "is-on" : "is-off"}">${c.enabled ? "已启用" : "已禁用"}</span>
        </div>
        <div class="discovery-card-desc">${c.description}</div>
      `;
      const cb = card.querySelector('input[type="checkbox"]');
      cb.addEventListener("change", () => {
        c.enabled = cb.checked;
        card.classList.toggle("is-disabled", !c.enabled);
        const status = card.querySelector(".discovery-card-status");
        status.textContent = c.enabled ? "已启用" : "已禁用";
        status.classList.toggle("is-on", c.enabled);
        status.classList.toggle("is-off", !c.enabled);
        cb.parentElement.title = c.enabled ? "点击禁用" : "点击启用";
        this._hwDirty = true;
        this._renderHwPane();
      });
      list.appendChild(card);
    }
  },

  setAllHw(value) {
    for (const c of this._hwCaps) c.enabled = !!value;
    this._hwDirty = true;
    this._renderHwPane();
  },

  async saveHw() {
    const saveBtn = document.getElementById("discovery-hw-save");
    if (saveBtn) saveBtn.disabled = true;
    const updates = {};
    for (const c of this._hwCaps) updates[c.capability_type] = !!c.enabled;
    try {
      const r = await fetch(API_PREFIX + "/compute/hardware-capabilities", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ updates }),
      });
      const text = await r.text();
      let body;
      try { body = JSON.parse(text); } catch { body = { detail: text }; }
      if (!r.ok) {
        const msg = body.detail || ("HTTP " + r.status);
        const summary = document.getElementById("discovery-hw-summary");
        if (summary) {
          summary.textContent = "保存失败: " + msg;
          summary.classList.add("danger");
        }
        return;
      }
      // 用服务端权威响应回填（含 rejected 等）
      if (Array.isArray(body.hw_capabilities)) {
        this._hwCaps = body.hw_capabilities.map((c) => ({ ...c }));
      }
      this._hwDirty = false;
      this._renderHwPane();
      const summary = document.getElementById("discovery-hw-summary");
      if (summary) {
        summary.classList.remove("danger");
        summary.textContent = `已保存 · applied=${body.applied.length} rejected=${body.rejected.length}`;
      }
    } catch (e) {
      const summary = document.getElementById("discovery-hw-summary");
      if (summary) {
        summary.textContent = "保存失败: " + e.message;
        summary.classList.add("danger");
      }
    } finally {
      if (saveBtn) saveBtn.disabled = !this._hwDirty;
    }
  },
};

/* ------------------------------------------------------------------ *
 *  在网注册 (DDS) — Discovery 页内的子面板                              *
 *                                                                     *
 * 单独成模块（不挤进 ``Discovery``）的原因：                              *
 *  1. 拉取路径独立 — `/api/discovery/dds` vs `/api/compute/capabilities`，*
 *     互不影响；HW/SW 拉挂时 DDS 面板还能正常刷新；                     *
 *  2. 轮询节奏独立 — DDS 在 ready 时按 1Hz 拉，让 discovered / state    *
 *     实时刷新，HW/SW 一次性 render；                                   *
 *  3. 状态管理独立 — toggle 跟 HW dirty 状态分开。                     *
 *                                                                     *
 *  FastDDS native bindings 没装时，后端 ``POST /start`` 返回 503，      *
 *  UI 直接渲染 ``discovery-dds-runtime-banner`` 上的红色错误条，         *
 *  toggle 被禁用（避免用户空操作）。                                    *
 * ------------------------------------------------------------------ */
const DiscoveryDds = {
  initialized: false,
  _snapshot: null,
  _runtime: null,
  _tickHandle: null,
  _inflight: false,
  _lastToggle: null,
  _toggleInflight: false,

  init() {
    if (this.initialized) return;
    this.initialized = true;
    const toggle = document.getElementById("discovery-dds-toggle");
    if (toggle) toggle.addEventListener("change", () => this.onToggle());
  },

  async load() {
    this.init();
    if (this._inflight) return;
    this._inflight = true;
    try {
      // 先拉一次 fastdds runtime（不触发 connect），决定 toggle 是否可用
      try {
        const rr = await fetch(API_PREFIX + "/discovery/dds/runtime", {
          cache: "no-store",
          headers: { "Cache-Control": "no-cache" },
        });
        if (rr.ok) this._runtime = await rr.json();
        else this._runtime = { available: false, error: "HTTP " + rr.status };
      } catch (e) {
        this._runtime = { available: false, error: e.message };
      }
      // 再拉 snapshot
      const r = await fetch(API_PREFIX + "/discovery/dds", {
        cache: "no-store",
        headers: { "Cache-Control": "no-cache" },
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      this._snapshot = await r.json();
      this.render();
    } finally {
      this._inflight = false;
    }
  },

  startTicking() {
    if (this._tickHandle) return;
    this._tickHandle = setInterval(() => this.load().catch(() => {}), 1000);
  },

  stopTicking() {
    if (this._tickHandle) {
      clearInterval(this._tickHandle);
      this._tickHandle = null;
    }
  },

  async onToggle() {
    const toggle = document.getElementById("discovery-dds-toggle");
    if (!toggle) return;
    const want = toggle.checked;
    // fastdds 不可用 → 拒绝切换
    if (want && this._runtime && !this._runtime.available) {
      toggle.checked = false;
      this._setSummary(
        "FastDDS Python bindings 未安装，无法启用 DDS 注册。Debian/Ubuntu 上没有 apt 包（不是 libfastdds-dev / fastdds-python3 —— 后者根本不存在），只能从 Fast-DDS-Python 源码构建。",
        true,
      );
      return;
    }
    this._lastToggle = want;
    this._toggleInflight = true;
    try {
      const url = API_PREFIX + "/discovery/dds/" + (want ? "start" : "stop");
      const r = await fetch(url, { method: "POST" });
      const text = await r.text();
      let body;
      try { body = JSON.parse(text); } catch { body = { detail: text }; }
      if (!r.ok) {
        const detail = body && body.detail;
        let msg;
        if (typeof detail === "object" && detail !== null) {
          msg = detail.message || detail.reason || JSON.stringify(detail);
        } else {
          msg = detail || ("HTTP " + r.status);
        }
        this._setSummary("切换失败: " + msg, true);
        toggle.checked = !want;
        this._lastToggle = !want;
        // 503 → fastdds 不可用：刷新 runtime 缓存，确保下次 onToggle 也拒
        if (r.status === 503 && typeof detail === "object") {
          this._runtime = detail.dds_runtime || { available: false, error: msg };
        }
        return;
      }
      if (body && body.snapshot) {
        this._snapshot = body.snapshot;
      } else {
        await this.load();
        return;
      }
      this.render();
    } catch (e) {
      this._setSummary("切换失败: " + e.message, true);
      toggle.checked = !want;
      this._lastToggle = !want;
    } finally {
      this._toggleInflight = false;
    }
  },

  render() {
    const snap = this._snapshot;
    const rt = this._runtime;
    if (!snap) return;

    // ---- runtime banner ----
    const banner = document.getElementById("discovery-dds-runtime-banner");
    if (banner && rt) {
      if (rt.available) {
        const ver = rt.version || "?";
        const path = rt.path || "";
        banner.className = "discovery-runtime-banner is-ok";
        banner.innerHTML =
          `FastDDS 可用 · <code>${this._esc(rt.module || "fastdds")}</code>` +
          ` <code>${this._esc(ver)}</code>` +
          (path ? ` <code>${this._esc(path)}</code>` : "");
      } else {
        banner.className = "discovery-runtime-banner is-danger";
        banner.textContent =
          "FastDDS Python bindings 未安装，DDS 注册功能不可用。" +
          (rt.error ? " 错误：" + rt.error : "");
      }
    }

    // ---- toggle state ----
    const toggle = document.getElementById("discovery-dds-toggle");
    const isReady = snap.state === "ready";
    if (toggle && this._lastToggle === null) {
      toggle.checked = isReady;
      this._lastToggle = isReady;
    } else if (toggle && !this._toggleInflight && toggle.checked !== isReady) {
      toggle.checked = isReady;
      this._lastToggle = isReady;
    }
    if (toggle) {
      const unavailable = !!(rt && !rt.available);
      toggle.disabled = unavailable || snap.state === "starting" || snap.state === "stopping";
      toggle.parentElement.title = unavailable
        ? "FastDDS 未安装，无法启用"
        : "开启后通过 wdndds + FastDDS 广播本机能力";
    }

    // ---- hint + summary ----
    const hint = document.getElementById("discovery-dds-hint");
    if (hint) hint.textContent = `${snap.discovered_count} 个对端`;

    const summary = document.getElementById("discovery-dds-summary");
    if (summary) {
      summary.classList.remove("danger");
      switch (snap.state) {
        case "ready":
          summary.textContent =
            `已注册为「${snap.device_name || "pyrkaiweb"}」 · domain=0 · ` +
            `对端 ${snap.discovered_count} 个`;
          break;
        case "starting":
          summary.textContent = "正在连接 FastDDS…";
          break;
        case "stopping":
          summary.textContent = "正在断开…";
          break;
        case "unavailable":
          summary.textContent =
            "FastDDS Python bindings 未安装。Debian/Ubuntu 上没有 apt 包（不是 libfastdds-dev / fastdds-python3 —— 后者根本不存在），只能从 Fast-DDS-Python 源码构建。";
          summary.classList.add("danger");
          break;
        case "error":
          summary.textContent =
            "上次启动失败：" + (snap.last_error || "未知错误");
          summary.classList.add("danger");
          break;
        case "uninitialized":
        default:
          summary.textContent =
            "未开启。打开开关后通过 wdndds 把本机能力广播到同网段 FastDDS participant。";
          break;
      }
    }

    // ---- device list ----
    const list = document.getElementById("discovery-dds-list");
    if (!list) return;
    const devices = Array.isArray(snap.discovered) ? snap.discovered : [];
    if (snap.state !== "ready") {
      // 非 ready 状态下不展示 discovered（可能在停机中）
      list.innerHTML = "";
      return;
    }
    if (devices.length === 0) {
      list.innerHTML = `
        <div class="page-placeholder">
          <div class="page-placeholder-icon" aria-hidden="true">⌖</div>
          <div class="page-placeholder-title">等待对端</div>
          <div class="page-placeholder-text">
            已注册，但本网段还没有其他 FastDDS participant 出现。
            确认网络允许 UDP 组播 239.255.0.1 / discovery port 7400+。
          </div>
        </div>`;
      return;
    }
    list.innerHTML = "";
    for (const d of devices) {
      const card = document.createElement("article");
      card.className = "discovery-card";
      const capChips = (d.capabilities || []).map((t) =>
        `<span class="discovery-card-lan-cap" title="${this._esc(t)}">${this._esc(t)}</span>`,
      );
      if (capChips.length === 0) {
        capChips.push(`<span class="discovery-card-lan-cap is-empty">无能力</span>`);
      }
      card.innerHTML = `
        <header class="discovery-card-head">
          <code class="discovery-cap-name">${this._esc(d.name)}</code>
        </header>
        <div class="discovery-card-meta">
          <span>首次出现：<strong>${this._fmtTime(d.first_seen)}</strong></span>
          <span>最近出现：<strong>${this._fmtTime(d.last_seen)}</strong></span>
        </div>
        <div class="discovery-card-lan-caps">${capChips.join("")}</div>
      `;
      list.appendChild(card);
    }
  },

  _setSummary(text, danger) {
    const summary = document.getElementById("discovery-dds-summary");
    if (!summary) return;
    summary.textContent = text;
    summary.classList.toggle("danger", !!danger);
  },

  _fmtTime(t) {
    if (!t || t <= 0) return "—";
    const d = new Date(t * 1000);
    const pad = (n) => (n < 10 ? "0" + n : "" + n);
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  },

  _esc(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  },
};

/* ------------------------------------------------------------------ *
 *  im-bridge page — WeChat iLink bot (login + 调试收发)             *
/* ------------------------------------------------------------------ *
 *  im-bridge page — WeChat iLink bot (login + 调试收发)             *
 *                                                                    *
 *  调试收发 把发送简化为"发给本机 user_id" —— 即自动用 bot 自己登录      *
 *  后的 user_id 作为收件人，验证 round-trip。不暴露输入框也避免用户       *
 *  手动输入尚未建立 context_token 的陌生 user_id 后报 NoContextError。 *
 * ------------------------------------------------------------------ */
const ImBridge = {
  ws: null,
  wsRetry: 0,
  wsTimer: null,
  snapshot: null,
  messages: [],          // most recent first (in + out mixed)

  init() {
    const $ = (id) => document.getElementById(id);
    $("imb-login-btn")?.addEventListener("click", () => this.login());
    $("imb-cancel-btn")?.addEventListener("click", () => this.cancelLogin());
    $("imb-logout-btn")?.addEventListener("click", () => this.logout());
    $("imb-send-btn")?.addEventListener("click", () => this.send());
    $("imb-clear-btn")?.addEventListener("click", () => this.clearChat());
    $("imb-text-input")?.addEventListener("keydown", (e) => {
      // Cmd/Ctrl + Enter sends; plain Enter inserts a newline.
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        this.send();
      }
    });
  },

  async load() {
    try {
      const r = await fetch(API_PREFIX + "/wechat");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const snap = await r.json();
      this._applySnapshot(snap);
      // Pull runtime info (SDK version + wechatbot-sdk available flag)
      // for the "SDK" row; failures are non-fatal.
      try {
        const rr = await fetch(API_PREFIX + "/wechat/runtime");
        if (rr.ok) {
          const rt = await rr.json();
          const el = document.getElementById("imb-runtime");
          if (el) {
            el.textContent = rt.available
              ? (rt.module + " " + (rt.version || ""))
              : ("missing: " + (rt.error || "unavailable"));
            el.classList.toggle("imb-muted", !rt.available);
          }
        }
      } catch (_) { /* ignore */ }
    } catch (err) {
      this._setChatStatus("加载失败: " + err.message, true);
    }
  },

  ensureWs() {
    if (this.ws && (this.ws.readyState === WebSocket.OPEN ||
                    this.ws.readyState === WebSocket.CONNECTING)) {
      return;
    }
    this._openWs();
  },

  closeWs() {
    if (this.wsTimer) {
      clearTimeout(this.wsTimer);
      this.wsTimer = null;
    }
    if (this.ws) {
      try { this.ws.close(); } catch (_) { /* ignore */ }
      this.ws = null;
    }
    this.wsRetry = 0;
  },

  _openWs() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = proto + "//" + location.host + API_PREFIX + "/wechat/ws";
    let ws;
    try {
      ws = new WebSocket(url);
    } catch (err) {
      this._scheduleReconnect();
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      this.wsRetry = 0;
    };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (_) { return; }
      this._onWsMessage(msg);
    };
    ws.onclose = () => {
      this.ws = null;
      this._scheduleReconnect();
    };
    ws.onerror = () => {
      // onclose will fire after; reconnect logic lives there.
    };
  },

  _scheduleReconnect() {
    if (this.wsTimer) return;
    this.wsRetry = Math.min(this.wsRetry + 1, 10);
    const delay = Math.min(1000 * (1 << (this.wsRetry - 1)), 30000);
    this.wsTimer = setTimeout(() => {
      this.wsTimer = null;
      this._openWs();
    }, delay);
  },

  _onWsMessage(msg) {
    if (!msg || !msg.type) return;
    if (msg.type === "snapshot") {
      this._applySnapshot(msg.snapshot);
      return;
    }
    if (msg.type === "state") {
      if (!this.snapshot) this.snapshot = {};
      Object.assign(this.snapshot, msg.state || {});
      this._renderState();
      this._renderButtons();
      this._renderQr();
      return;
    }
    if (msg.type === "message") {
      this._recordMessage(msg.message);
      this._renderChat();
      this._renderKnown();
      return;
    }
  },

  _applySnapshot(snap) {
    if (!snap) return;
    this.snapshot = snap;
    this.messages = Array.isArray(snap.recent_messages) ? snap.recent_messages.slice() : [];
    this._renderState();
    this._renderButtons();
    this._renderQr();
    this._renderKnown();
    this._renderChat();
  },

  _renderState() {
    const s = this.snapshot || {};
    const set = (id, val) => {
      const el = document.getElementById(id);
      if (el) el.textContent = (val === null || val === undefined || val === "") ? "--" : String(val);
    };
    set("imb-cred-path", s.cred_path);
    set("imb-user-id", s.user_id);
    set("imb-running", s.started ? "运行中" : "停止");

    const badge = document.getElementById("imb-status-badge");
    if (badge) {
      const state = s.login_state || "idle";
      const labelMap = {
        idle: "未登录",
        awaiting_scan: "等待扫码",
        scanned: "已扫码",
        confirmed: "已登录",
        running: "已登录",
        expired: "二维码已过期",
        error: "错误",
        cancelled: "已取消",
        login_failed: "登录失败",
      };
      badge.dataset.state = state;
      badge.textContent = labelMap[state] || state;
    }

    const errEl = document.getElementById("imb-error");
    if (errEl) {
      if (s.error) {
        errEl.textContent = "· " + s.error;
        errEl.classList.add("visible");
      } else {
        errEl.textContent = "";
        errEl.classList.remove("visible");
      }
    }
  },

  _renderButtons() {
    const s = this.snapshot || {};
    const state = s.login_state || "idle";
    const loginBtn = document.getElementById("imb-login-btn");
    const cancelBtn = document.getElementById("imb-cancel-btn");
    const logoutBtn = document.getElementById("imb-logout-btn");
    const sendBtn = document.getElementById("imb-send-btn");
    const textInput = document.getElementById("imb-text-input");

    const loggedIn = state === "confirmed" || state === "running" || !!s.started;
    const active = state === "awaiting_scan" || state === "scanned";

    if (loginBtn) loginBtn.disabled = loggedIn || active;
    if (cancelBtn) cancelBtn.disabled = !active;
    if (logoutBtn) logoutBtn.disabled = !loggedIn && state !== "error";
    if (sendBtn) sendBtn.disabled = !loggedIn;
    if (textInput) textInput.disabled = !loggedIn;
  },

  _renderQr() {
    const stage = document.getElementById("imb-qr-stage");
    const hint = document.getElementById("imb-qr-hint");
    if (!stage || !hint) return;
    const s = this.snapshot || {};
    const state = s.login_state || "idle";
    const url = s.qr_url;
    const lastEvent = s.last_event || "";

    stage.innerHTML = "";
    // While ``login_state`` is still ``idle`` but ``last_event`` flipped
    // to ``"starting"``, the user just clicked 登录 and the SDK is in
    // the middle of fetching a QR — show a transient hint instead of
    // falling back to the "click 登录" prompt.
    if (state === "idle" && lastEvent === "starting") {
      hint.textContent = "正在向微信申请二维码…";
      hint.classList.remove("imb-qr-hint--muted");
      return;
    }
    if (state === "idle" && lastEvent === "logged_out") {
      hint.textContent = "已退出登录，点「登录」重新扫码";
      hint.classList.remove("imb-qr-hint--muted");
      return;
    }
    if (state === "idle" && lastEvent === "cancelled") {
      hint.textContent = "已取消本次扫码";
      hint.classList.add("imb-qr-hint--muted");
      return;
    }
    if (state === "awaiting_scan" && url) {
      try {
        // Vendored qrcode-generator (Kazuhiko Arase, MIT).
        // ``qrcode(0, 'M')`` auto-picks the smallest type number that fits.
        const qr = window.qrcode(0, "M");
        qr.addData(url);
        qr.make();
        const svg = qr.createSvgTag({ cellSize: 4, margin: 4, scalable: true });
        stage.innerHTML = svg;
        hint.textContent = "请用微信扫描二维码（120 秒内有效）";
        hint.classList.remove("imb-qr-hint--muted");
      } catch (err) {
        hint.textContent = "QR 渲染失败: " + err.message;
        hint.classList.add("imb-qr-hint--muted");
      }
      return;
    }
    if (state === "scanned") {
      hint.textContent = "已扫码，请在手机上点「登录」确认";
      hint.classList.remove("imb-qr-hint--muted");
      return;
    }
    if (state === "confirmed" || state === "running") {
      hint.textContent = "已登录，user_id = " + (s.user_id || "?");
      hint.classList.remove("imb-qr-hint--muted");
      return;
    }
    if (state === "expired") {
      hint.textContent = "二维码已过期，点「登录」重新获取";
      hint.classList.add("imb-qr-hint--muted");
      return;
    }
    if (state === "error" || state === "login_failed") {
      hint.textContent = "登录失败：" + (s.error || state);
      hint.classList.add("imb-qr-hint--muted");
      return;
    }
    if (state === "cancelled") {
      hint.textContent = "已取消本次扫码";
      hint.classList.add("imb-qr-hint--muted");
      return;
    }
    hint.textContent = "点击「登录」开始扫码";
    hint.classList.remove("imb-qr-hint--muted");
  },

  _renderKnown() {
    const el = document.getElementById("imb-known-count");
    if (!el) return;
    const known = (this.snapshot && Array.isArray(this.snapshot.known_users))
      ? this.snapshot.known_users.length
      : 0;
    el.textContent = "已知 " + known + " 人";
  },

  _recordMessage(msg) {
    if (!msg) return;
    // Dedup: the SDK and the poller can both produce the same item;
    // skip if it's already the head of the list.
    if (this.messages.length &&
        this.messages[0].ts_ms === msg.ts_ms &&
        this.messages[0].direction === msg.direction &&
        this.messages[0].user_id === msg.user_id &&
        this.messages[0].text === msg.text) {
      return;
    }
    this.messages.unshift(msg);
    if (this.messages.length > 200) this.messages.length = 200;
  },

  _renderChat() {
    const list = document.getElementById("imb-conv-list");
    const title = document.getElementById("imb-conv-title");
    if (!list || !title) return;
    if (!this.messages.length) {
      title.textContent = "消息记录";
      list.innerHTML = `
        <div class="page-placeholder imb-empty">
          <div class="page-placeholder-text">
            登录后会话与消息会出现在这里。<br/>
            发送默认发给本机 user_id（用于测试 round-trip）。
          </div>
        </div>`;
      return;
    }
    // Show all messages (any user) — the sender is locked to the bot's
    // own user_id, so almost everything will be to/from that account;
    // a real user messaging the bot will still surface here.
    const rows = this.messages.map((m) => {
      const dir = m.direction === "out" ? "out" : "in";
      const errTag = m.error ? `<span class="imb-msg-error">[${this._esc(m.error)}]</span>` : "";
      const time = m.ts ? this._fmtTime(m.ts_ms) : "";
      const media = m.has_media ? " 📎" : "";
      const kindLabel = m.type ? `[${this._esc(m.type)}]` : "";
      return `<li class="imb-msg imb-msg--${dir}">
        <span class="imb-msg-meta">${this._esc(m.user_id)} · ${time}${media}</span>
        <div class="imb-msg-body">${kindLabel} ${this._esc(m.text || "")} ${errTag}</div>
      </li>`;
    });
    const header = (() => {
      const self = this.snapshot && this.snapshot.user_id;
      return self ? `消息记录（发送目标：${this._esc(self)}）` : "消息记录";
    })();
    title.textContent = header;
    list.innerHTML = `<ul class="imb-conv-ul">${rows.join("")}</ul>`;
  },

  refreshChat() {
    this._renderChat();
  },

  clearChat() {
    this.messages = [];
    this._renderChat();
  },

  _setChatStatus(text, danger) {
    const el = document.getElementById("imb-chat-status");
    if (!el) return;
    el.textContent = text || "";
    el.classList.toggle("danger", !!danger);
  },

  async login() {
    const btn = document.getElementById("imb-login-btn");
    if (btn) btn.disabled = true;
    this._setChatStatus("正在发起登录…");
    try {
      const r = await fetch(API_PREFIX + "/wechat/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ force: false }),
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      if (data.snapshot) this._applySnapshot(data.snapshot);
      this._setChatStatus("");
    } catch (err) {
      this._setChatStatus("登录失败: " + err.message, true);
      this._renderButtons();
    }
  },

  async cancelLogin() {
    try {
      const r = await fetch(API_PREFIX + "/wechat/cancel-login", {
        method: "POST",
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      if (data.snapshot) this._applySnapshot(data.snapshot);
    } catch (err) {
      this._setChatStatus("取消失败: " + err.message, true);
    }
  },

  async logout() {
    if (!confirm("确认退出登录？已保存的凭证将被删除，下次需要重新扫码。")) return;
    try {
      const r = await fetch(API_PREFIX + "/wechat/logout", { method: "POST" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      if (data.snapshot) this._applySnapshot(data.snapshot);
      else await this.load();
      this._setChatStatus("已退出登录");
    } catch (err) {
      this._setChatStatus("退出失败: " + err.message, true);
    }
  },

  async send() {
    const textEl = document.getElementById("imb-text-input");
    if (!textEl) return;
    // The receiver is locked to the bot's own user_id — the dialog
    // exists to verify the SDK's round-trip, not to message arbitrary
    // users. We accept whatever the backend has for ``user_id``; the
    // send button is disabled when it's null so this branch is rare.
    const userId = (this.snapshot && this.snapshot.user_id) || "";
    const text = textEl.value;
    if (!userId) {
      this._setChatStatus("未登录，无法发送", true);
      return;
    }
    if (!text.trim()) {
      this._setChatStatus("消息内容不能为空", true);
      textEl.focus();
      return;
    }
    const btn = document.getElementById("imb-send-btn");
    if (btn) btn.disabled = true;
    this._setChatStatus("发送中…");
    try {
      const r = await fetch(API_PREFIX + "/wechat/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: userId, text: text }),
      });
      if (!r.ok) {
        let detail = "HTTP " + r.status;
        try {
          const errBody = await r.json();
          if (errBody && errBody.detail) detail = errBody.detail;
        } catch (_) { /* ignore */ }
        throw new Error(detail);
      }
      textEl.value = "";
      this._setChatStatus("已发送");
    } catch (err) {
      this._setChatStatus("发送失败: " + err.message, true);
    } finally {
      if (btn) btn.disabled = false;
    }
  },

  _fmtTime(ms) {
    if (!ms) return "";
    const d = new Date(ms);
    const pad = (n) => (n < 10 ? "0" + n : "" + n);
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  },

  _esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  },
};

/* ------------------------------------------------------------------ *
 *  TextChat: 文本对话页面模块                                          *
 *                                                                     *
 *  服务端走 ``/api/text-chat/ws`` 推流式 token + state 变更；前端      *
 *  按 request_id 维护"用户问题 → thinking → answer" 三段气泡，         *
 *  流式渲染时附加打字机光标，``done`` 后清理。                         *
 *                                                                     *
 *  WS 帧协议：                                                        *
 *    snapshot / state / user_echo / thinking / answer /               *
 *    done / stopped / error                                           *
 * ------------------------------------------------------------------ */
const TextChat = {
  ws: null,
  wsRetry: 0,
  wsTimer: null,
  snapshot: null,
  pending: new Map(),       // request_id -> {userLi, assistantLi, thinkingEl, answerEl, errorEl, cursorEl}
  messages: [],             // local history mirror: [{role:"user"|"assistant", content, ts}]

  init() {
    const $ = (id) => document.getElementById(id);
    $("text-chat-send-btn")?.addEventListener("click", () => this.send());
    $("text-chat-stop-btn")?.addEventListener("click", () => this.stop());
    $("text-chat-reset-btn")?.addEventListener("click", () => this.reset());
    $("text-chat-refresh")?.addEventListener("click", () => this.refresh());
    $("text-chat-input")?.addEventListener("keydown", (e) => {
      // Cmd/Ctrl + Enter sends; plain Enter inserts a newline.
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        this.send();
      }
    });
  },

  async refresh() {
    const target = document.getElementById("data-text-chat");
    try {
      const r = await fetch(API_PREFIX + "/text-chat");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const snap = await r.json();
      this._applySnapshot(snap);
      if (target) target.textContent = JSON.stringify(snap, null, 2);
    } catch (err) {
      if (target) target.textContent = "刷新失败: " + err.message;
    }
  },

  ensureWs() {
    if (this.ws && (this.ws.readyState === WebSocket.OPEN ||
                    this.ws.readyState === WebSocket.CONNECTING)) {
      return;
    }
    this._openWs();
  },

  closeWs() {
    if (this.wsTimer) {
      clearTimeout(this.wsTimer);
      this.wsTimer = null;
    }
    if (this.ws) {
      try { this.ws.close(); } catch (_) { /* ignore */ }
      this.ws = null;
    }
    this.wsRetry = 0;
  },

  _openWs() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = proto + "//" + location.host + API_PREFIX + "/text-chat/ws";
    let ws;
    try {
      ws = new WebSocket(url);
    } catch (err) {
      this._scheduleReconnect();
      return;
    }
    this.ws = ws;
    ws.onopen = () => { this.wsRetry = 0; };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (_) { return; }
      this._onWsMessage(msg);
    };
    ws.onclose = () => { this.ws = null; this._scheduleReconnect(); };
    ws.onerror = () => { /* onclose fires next */ };
  },

  _scheduleReconnect() {
    if (this.wsTimer) return;
    this.wsRetry = Math.min(this.wsRetry + 1, 10);
    const delay = Math.min(1000 * (1 << (this.wsRetry - 1)), 30000);
    this.wsTimer = setTimeout(() => {
      this.wsTimer = null;
      this._openWs();
    }, delay);
  },

  _onWsMessage(msg) {
    if (!msg || !msg.type) return;
    if (msg.type === "snapshot") {
      this._applySnapshot(msg.snapshot);
      return;
    }
    if (msg.type === "state") {
      this._applySnapshot(msg.state || {});
      return;
    }
    if (msg.type === "user_echo") {
      this._appendUserBubble(msg.request_id, msg.text || "");
      return;
    }
    if (msg.type === "thinking") {
      this._appendThinkingDelta(msg.request_id, msg.delta || "");
      return;
    }
    if (msg.type === "answer") {
      this._appendAnswerDelta(msg.request_id, msg.delta || "");
      return;
    }
    if (msg.type === "done") {
      this._finalizeBubble(msg.request_id, {
        thinking: msg.thinking || "",
        answer: msg.answer || "",
        status: "done",
        elapsed_sec: msg.elapsed_sec,
      });
      this._recordMessage("user",      msg.answer ? "" : "", 0);   // user 已 echo
      if (msg.answer) {
        this._recordMessage("assistant", msg.answer, Date.now());
      }
      return;
    }
    if (msg.type === "stopped") {
      this._finalizeBubble(msg.request_id, {
        thinking: msg.thinking || "",
        answer: msg.answer || "",
        status: "stopped",
        elapsed_sec: msg.elapsed_sec,
      });
      return;
    }
    if (msg.type === "error") {
      this._showInlineError(msg.request_id, msg.detail || "未知错误");
      return;
    }
  },

  _applySnapshot(snap) {
    if (!snap) return;
    this.snapshot = Object.assign({}, this.snapshot || {}, snap);
    this._renderState();
  },

  _renderState() {
    const s = this.snapshot || {};
    const state = s.state || "idle";
    const busy = !!s.busy;

    const badge = document.getElementById("text-chat-state-badge");
    if (badge) {
      const labelMap = {
        idle:    "空闲",
        loading: "排队中",
        running: "生成中",
        done:    "完成",
        error:   "错误",
        stopped: "已停止",
      };
      badge.textContent = labelMap[state] || state;
      badge.dataset.state = state;
    }

    const statusLine = document.getElementById("text-chat-status-line");
    if (statusLine) {
      if (s.error) statusLine.textContent = state === "error" ? "错误" : state;
      else statusLine.textContent = ({
        idle:    "空闲",
        loading: "排队中",
        running: "正在生成",
        done:    "最近一轮已完成",
        error:   "出错",
        stopped: "已停止",
      })[state] || state;
    }

    const tokens = document.getElementById("text-chat-tokens");
    if (tokens) {
      const ht = s.history_tokens || 0;
      const limit = s.context_limit || 32768;
      const maxNew = s.max_new_tokens || 2048;
      tokens.textContent = `${ht} / ${limit} tokens（预留 ${maxNew} 给输出）`;
    }

    const sendBtn = document.getElementById("text-chat-send-btn");
    const stopBtn = document.getElementById("text-chat-stop-btn");
    const input   = document.getElementById("text-chat-input");
    if (sendBtn) sendBtn.disabled = busy;
    if (input)   input.disabled = busy;
    if (stopBtn) stopBtn.disabled = !(state === "running" || state === "loading");

    const banner = document.getElementById("text-chat-unavailable");
    if (banner) {
      const isCapErr = state === "error" && s.error && /功能未启用/.test(s.error);
      banner.classList.toggle("hidden", !isCapErr);
    }

    const hint = document.getElementById("text-chat-hint");
    if (hint) {
      if (s.error) {
        hint.textContent = s.error.length > 80 ? s.error.slice(0, 80) + "…" : s.error;
        hint.classList.add("danger");
      } else {
        hint.textContent = busy ? "模型正在生成…" : "就绪";
        hint.classList.remove("danger");
      }
    }

    const target = document.getElementById("data-text-chat");
    if (target) target.textContent = JSON.stringify(s, null, 2);
  },

  _appendUserBubble(reqId, text) {
    const ul = document.getElementById("text-chat-conv-ul");
    const empty = document.getElementById("text-chat-empty");
    if (empty) empty.style.display = "none";

    const ts = this._fmtTime(Date.now());
    const li = document.createElement("li");
    li.className = "imb-msg imb-msg--out";
    li.dataset.role = "user";
    li.innerHTML = `
      <span class="imb-msg-meta">你 · ${ts}</span>
      <div class="imb-msg-body">${this._esc(text)}</div>
    `;
    ul?.appendChild(li);
    this._scrollToBottom();
    this._recordMessage("user", text, Date.now());

    // initialize pending entry for this request_id (assistant bubble comes next)
    if (!this.pending.has(reqId)) {
      this.pending.set(reqId, { userLi: li, assistantLi: null });
    }
  },

  _ensureAssistantBubble(reqId) {
    let p = this.pending.get(reqId);
    if (p && p.assistantLi) return p;

    const ul = document.getElementById("text-chat-conv-ul");
    const ts = this._fmtTime(Date.now());

    const li = document.createElement("li");
    li.className = "imb-msg imb-msg--in";
    li.dataset.role = "assistant";
    // thinking 默认**展开**显示（不再默认折叠），用户一眼就能看见
    // 思考过程；点 summary 可折叠。
    li.innerHTML = `
      <span class="imb-msg-meta">模型 · ${ts}</span>
      <details class="text-chat-thinking" data-collapsed="false" open>
        <summary>思考过程（点击折叠）</summary>
        <div class="text-chat-thinking-body" data-role="thinking-body"></div>
      </details>
      <div class="imb-msg-body text-chat-answer" data-role="answer-body"></div>
      <div class="imb-msg-error text-chat-inline-error" data-role="inline-error" hidden></div>
    `;
    ul?.appendChild(li);

    p = Object.assign(p || {}, {
      assistantLi: li,
      thinkingEl: li.querySelector('[data-role="thinking-body"]'),
      answerEl:   li.querySelector('[data-role="answer-body"]'),
      errorEl:    li.querySelector('[data-role="inline-error"]'),
      thinkingBuf: "",
      answerBuf:   "",
    });
    this.pending.set(reqId, p);
    return p;
  },

  _appendThinkingDelta(reqId, delta) {
    const p = this._ensureAssistantBubble(reqId);
    if (!p || !p.thinkingEl) return;
    p.thinkingBuf += delta;
    p.thinkingEl.textContent = p.thinkingBuf;
    // expand thinking details once there's content (so user sees live updates)
    const li = p.assistantLi;
    const details = li?.querySelector(".text-chat-thinking");
    if (details && p.thinkingBuf.length > 0 && !details.hasAttribute("open")) {
      details.setAttribute("open", "");
    }
    this._scrollToBottom();
  },

  _appendAnswerDelta(reqId, delta) {
    const p = this._ensureAssistantBubble(reqId);
    if (!p || !p.answerEl) return;
    p.answerBuf += delta;
    // render with blinking cursor at end
    p.answerEl.textContent = p.answerBuf;
    p.answerEl.classList.add("text-chat-cursor");
    this._scrollToBottom();
  },

  _finalizeBubble(reqId, info) {
    const p = this.pending.get(reqId);
    if (!p || !p.assistantLi) return;

    // remove cursor
    p.answerEl?.classList.remove("text-chat-cursor");

    // prefer the cleaner text from done frame (regex split) over the streamed buffer
    if (typeof info.thinking === "string") {
      p.thinkingBuf = info.thinking;
      if (p.thinkingEl) p.thinkingEl.textContent = info.thinking;
    }
    if (typeof info.answer === "string") {
      p.answerBuf = info.answer;
      if (p.answerEl) p.answerEl.textContent = info.answer;
    }

    // append stopped marker
    if (info.status === "stopped") {
      const meta = p.assistantLi.querySelector(".imb-msg-meta");
      if (meta) {
        const span = document.createElement("span");
        span.className = "text-chat-stopped";
        span.textContent = "（已停止）";
        meta.appendChild(span);
      }
    }

    // append elapsed time tag
    if (typeof info.elapsed_sec === "number" && info.elapsed_sec > 0) {
      const meta = p.assistantLi.querySelector(".imb-msg-meta");
      if (meta) {
        const span = document.createElement("span");
        span.className = "text-chat-stopped";
        span.textContent = `· ${info.elapsed_sec.toFixed(1)}s`;
        meta.appendChild(span);
      }
    }

    this._scrollToBottom();
  },

  _showInlineError(reqId, detail) {
    // If we have an assistant bubble for this request, show error inside it;
    // otherwise create a generic red banner above the composer.
    const p = this.pending.get(reqId);
    if (p && p.assistantLi && p.errorEl) {
      p.errorEl.hidden = false;
      p.errorEl.textContent = detail;
    } else {
      const list = document.getElementById("text-chat-conv-list");
      if (!list) return;
      const banner = document.createElement("div");
      banner.className = "text-chat-error";
      banner.textContent = detail;
      list.appendChild(banner);
      this._scrollToBottom();
    }
  },

  _recordMessage(role, content, ts) {
    if (!content) return;
    this.messages.push({ role, content, ts: ts || Date.now() });
    if (this.messages.length > 200) this.messages.length = 200;
  },

  _scrollToBottom() {
    const list = document.getElementById("text-chat-conv-list");
    if (!list) return;
    // only auto-scroll if user is near the bottom (avoid fighting scroll position)
    const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 80;
    if (nearBottom) list.scrollTop = list.scrollHeight;
  },

  async send() {
    const input = document.getElementById("text-chat-input");
    const text = (input?.value || "").trim();
    if (!text) return;
    if (this.snapshot && this.snapshot.busy) {
      this._setHint("上一条还在生成中…", true);
      return;
    }
    try {
      const r = await fetch(API_PREFIX + "/text-chat/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      if (!r.ok) {
        let detail = "HTTP " + r.status;
        try {
          const errBody = await r.json();
          if (errBody && errBody.detail) detail = errBody.detail;
        } catch (_) { /* ignore */ }
        throw new Error(detail);
      }
      if (input) input.value = "";
      this._setHint("已发送，等待模型响应…", false);
      // snapshot is updated by WS state frame
    } catch (err) {
      this._setHint("发送失败: " + err.message, true);
    }
  },

  async stop() {
    this._setHint("正在停止…", false);
    try {
      const r = await fetch(API_PREFIX + "/text-chat/stop", { method: "POST" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      this._setHint("已请求停止", false);
    } catch (err) {
      this._setHint("停止失败: " + err.message, true);
    }
  },

  async reset() {
    if (!confirm("确认清空对话历史？")) return;
    try {
      const r = await fetch(API_PREFIX + "/text-chat/reset", { method: "POST" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      // 清空本地 UI
      const ul = document.getElementById("text-chat-conv-ul");
      if (ul) ul.innerHTML = "";
      const empty = document.getElementById("text-chat-empty");
      if (empty) empty.style.display = "";
      this.pending.clear();
      this.messages = [];
      this._setHint("已清空", false);
    } catch (err) {
      this._setHint("清空失败: " + err.message, true);
    }
  },

  _setHint(text, danger) {
    const hint = document.getElementById("text-chat-hint");
    if (!hint) return;
    hint.textContent = text;
    hint.classList.toggle("danger", !!danger);
  },

  _fmtTime(ms) {
    if (!ms) return "";
    const d = new Date(ms);
    const pad = (n) => (n < 10 ? "0" + n : "" + n);
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  },

  _esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  },
};

/* ------------------------------------------------------------------ *
 *  video-content-search page（视频内容检索）                            *
 *                                                                    *
 *  后端本期为 mock（app/services/video_content_search.py）：extract   *
 *  只推进度、query 返回固定命中；后续按 vl_rag_demo 换真实实现时，    *
 *  本模块的归一化层（normalizeHits）和播放器帧源接口保持接缝。        *
 *                                                                    *
 *  协议字段对齐 demo 语义：hits = {video_path, start_time, end_time,  *
 *  chunk_idx, similarity}；query 参数 = {text, sim_threshold, top_k}。*
 * ------------------------------------------------------------------ */
const VCS = {
  ws: null,
  wsRetry: 0,
  wsTimer: null,
  initialized: false,
  snap: null,
  // 播放器模拟状态
  seg: null,        // { start, end, title }
  playing: false,
  ended: false,     // 播放完毕停在末尾帧（可再次点播放重播）
  rafId: 0,
  segStartAt: 0,

  init() { return initVideoContentSearch(); },
};

function vcsEl(id) {
  return document.getElementById(id);
}

function vcsSetStatus(text, isError) {
  const el = vcsEl("vcs-status");
  if (!el) return;
  el.textContent = text || "";
  el.style.color = isError ? "#b91c1c" : "";
}

function vcsSetRuntime(text) {
  const el = vcsEl("vcs-runtime");
  if (!el) return;
  el.textContent = text || "未启动";
}

function vcsSetSourceMeta(text) {
  const el = vcsEl("vcs-source-meta");
  if (!el) return;
  el.textContent = text || "尚未选择";
}

function vcsShowError(text) {
  const banner = vcsEl("vcs-error-banner");
  const textEl = vcsEl("vcs-error-text");
  if (!banner || !textEl) return;
  textEl.textContent = text || "";
  banner.classList.toggle("hidden", !text);
}

function vcsRenderSnapshot(snap) {
  const pre = vcsEl("data-video-content-search");
  if (pre) pre.textContent = JSON.stringify(snap, null, 2);
}

function vcsApplySnapshot(snap) {
  if (!snap) return;
  VCS.snap = snap;
  vcsRenderSnapshot(snap);
  if (typeof DemoGuard !== "undefined") {
    DemoGuard.report("video-content-search", { vcs: snap, playing: VCS.playing });
  }

  const st = snap.state || "idle";
  vcsSetStatus({ idle: "就绪", extracting: "● 提取中", ready: "● 提取完成", error: "提取失败" }[st] || st, st === "error");
  vcsSetRuntime({ idle: "未启动", extracting: "提取中", ready: "就绪，可检索", error: snap.error || "错误" }[st] || st);

  // 进度
  const prog = vcsEl("vcs-progress");
  const fill = vcsEl("vcs-progress-fill");
  const label = vcsEl("vcs-progress-label");
  const pct = vcsEl("vcs-progress-pct");
  if (st === "extracting") {
    if (prog) prog.hidden = false;
    const total = snap.slices_total || 0;
    const emb = snap.slices_embedded || 0;
    const p = total > 0 ? Math.round((emb / total) * 100) : 0;
    if (fill) fill.style.width = p + "%";
    if (label) label.textContent = `已嵌入 ${emb}/${total} 切片`;
    if (pct) pct.textContent = p + "%";
  } else if (st === "ready") {
    if (prog) prog.hidden = false;
    if (fill) fill.style.width = "100%";
    if (label) label.textContent = `已嵌入 ${snap.slices_total || 0}/${snap.slices_total || 0} 切片`;
    if (pct) pct.textContent = "100%";
  } else {
    if (prog) prog.hidden = true;
  }

  // 按钮可用性
  vcsUpdateButtons(st);

  // 检索结果 + 元信息
  const q = snap.query;
  const meta = vcsEl("vcs-query-meta");
  if (meta) {
    meta.textContent = q ? `命中 ${(snap.hits || []).length} 条` : "";
  }
  vcsRenderHits(snap.hits || [], q || null);
}

// 独立计算按钮可用性：snapshot 应用与视频源 input 变化都要重算
// （选视频只触发 input 事件，不触发 snapshot 推送）。
function vcsUpdateButtons(st) {
  st = st || (VCS.snap && VCS.snap.state) || "idle";
  const startBtn = vcsEl("vcs-start");
  const stopBtn = vcsEl("vcs-stop");
  const queryBtn = vcsEl("vcs-query");
  // 开始提取依据前端输入框当前值（浏览/手输路径即点亮），而非后端
  // snap.source —— 后者只在点按钮后经 /open 才写入。
  const src = (vcsEl("vcs-source")?.value || "").trim();
  if (startBtn) startBtn.disabled = st === "extracting" || !src;
  if (stopBtn) stopBtn.disabled = st !== "extracting";
  if (queryBtn) queryBtn.disabled = st !== "ready";
}

function vcsRenderHits(hits, query) {
  const list = vcsEl("vcs-results-list");
  const count = vcsEl("vcs-results-count");
  if (!list) return;
  if (!hits.length) {
    list.innerHTML = '<li class="vcs-results-empty">' +
      (query ? "没有达到阈值或上限的结果。" : "完成提取后输入问题检索，命中片段显示在这里。") +
      "</li>";
    if (count) count.textContent = "0 条";
    return;
  }
  if (count) count.textContent = hits.length + " 条";
  list.innerHTML = hits.map((h, i) => {
    const t = vcsFormatTime(h.start_time) + " – " + vcsFormatTime(h.end_time);
    const sim = (h.similarity != null) ? Number(h.similarity).toFixed(2) : "--";
    return (
      '<li class="vcs-card" data-i="' + i + '" role="button" tabindex="0">' +
        '<div class="vcs-thumb">缩略图</div>' +
        '<div class="vcs-card-info">' +
          '<span class="vcs-card-time">' + vcsEsc(t) + "</span>" +
          '<span class="vcs-card-score">相似度 <b>' + sim + "</b></span>" +
        "</div>" +
        '<span class="vcs-card-play">▶</span>' +
      "</li>"
    );
  }).join("");
  Array.from(list.children).forEach((li) => {
    li.addEventListener("click", () => vcsPlaySegment(Number(li.dataset.i)));
    li.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        vcsPlaySegment(Number(li.dataset.i));
      }
    });
  });
}

function vcsFormatTime(sec) {
  if (sec == null || !isFinite(sec)) return "--:--";
  sec = Math.max(0, Math.round(Number(sec)));
  const m = String(Math.floor(sec / 60)).padStart(2, "0");
  const s = String(sec % 60).padStart(2, "0");
  return m + ":" + s;
}

function vcsEsc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/* ------------------------- 播放器（mock 动画） ----------------------- */
function vcsPlaySegment(idx) {
  const hits = (VCS.snap && VCS.snap.hits) || [];
  const h = hits[idx];
  if (!h) return;
  const dur = Math.max(0.5, (h.end_time || 0) - (h.start_time || 0));
  VCS.seg = { start: h.start_time || 0, end: h.end_time || 0, dur, title: vcsFormatTime(h.start_time) + "–" + vcsFormatTime(h.end_time) };
  VCS.playing = true;
  VCS.ended = false;
  VCS.segStartAt = performance.now();

  const tag = vcsEl("vcs-player-tag");
  if (tag) tag.textContent = "正在播放 " + VCS.seg.title;
  const overlay = vcsEl("vcs-overlay");
  if (overlay) overlay.textContent = "";
  vcsEl("vcs-play").disabled = true;
  vcsEl("vcs-pause").disabled = false;
  vcsEl("vcs-stop-play").disabled = false;
  Array.from((vcsEl("vcs-results-list") || {}).children || []).forEach((li, i) => {
    li.classList.toggle("active", i === idx);
  });
  vcsDrawPlayerFrame(0);
  VCS.rafId = requestAnimationFrame(vcsPlayerTick);
  if (typeof DemoGuard !== "undefined") {
    DemoGuard.report("video-content-search", { vcs: VCS.snap, playing: true });
  }
}

function vcsDrawPlayerFrame(frac) {
  const canvas = vcsEl("vcs-canvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const t = Math.min(1, Math.max(0, frac));
  // 模拟"画面变化"：按进度做 HSL 色带渐变 + 中央时间水印
  const hue = (t * 280) % 360;
  const g = ctx.createLinearGradient(0, 0, canvas.width, canvas.height);
  g.addColorStop(0, "hsl(" + hue + ", 60%, 30%)");
  g.addColorStop(1, "hsl(" + ((hue + 60) % 360) + ", 65%, 45%)");
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "rgba(255,255,255,0.85)";
  ctx.font = "28px monospace";
  ctx.textAlign = "center";
  if (VCS.seg) {
    const cur = VCS.seg.start + t * (VCS.seg.end - VCS.seg.start);
    ctx.fillText(vcsFormatTime(cur), canvas.width / 2, canvas.height / 2);
  }
  ctx.textAlign = "left";
}

function vcsPlayerTick() {
  if (!VCS.playing || !VCS.seg) return;
  const elapsed = (performance.now() - VCS.segStartAt) / 1000;
  const frac = Math.min(1, elapsed / VCS.seg.dur);
  vcsDrawPlayerFrame(frac);
  const bar = vcsEl("vcs-player-progress");
  if (bar) bar.style.width = (frac * 100) + "%";
  const timeEl = vcsEl("vcs-player-time");
  if (timeEl) {
    const cur = VCS.seg.start + frac * (VCS.seg.end - VCS.seg.start);
    timeEl.textContent = vcsFormatTime(cur) + " / " + vcsFormatTime(VCS.seg.end);
  }
  if (frac >= 1) {
    vcsFinishSegment();
    return;
  }
  VCS.rafId = requestAnimationFrame(vcsPlayerTick);
}

// 播放完毕：画面停在末尾帧、进度条 100%，播放按钮可重播；
// 不关闭播放窗口（关闭由 ⏹ 停止负责）。
function vcsFinishSegment() {
  if (!VCS.seg) return;
  VCS.playing = false;
  VCS.ended = true;
  cancelAnimationFrame(VCS.rafId);
  vcsDrawPlayerFrame(1);
  const bar = vcsEl("vcs-player-progress");
  if (bar) bar.style.width = "100%";
  const timeEl = vcsEl("vcs-player-time");
  if (timeEl) timeEl.textContent = vcsFormatTime(VCS.seg.end) + " / " + vcsFormatTime(VCS.seg.end);
  vcsEl("vcs-play").disabled = false;
  vcsEl("vcs-pause").disabled = true;
  vcsEl("vcs-stop-play").disabled = false;
  if (typeof DemoGuard !== "undefined") {
    DemoGuard.report("video-content-search", { vcs: VCS.snap, playing: false });
  }
}

function vcsPausePlayback() {
  if (!VCS.playing) return;
  VCS.playing = false;
  cancelAnimationFrame(VCS.rafId);
  vcsEl("vcs-play").disabled = false;
  vcsEl("vcs-pause").disabled = true;
  if (typeof DemoGuard !== "undefined") {
    DemoGuard.report("video-content-search", { vcs: VCS.snap, playing: false });
  }
}

function vcsStopPlayback() {
  VCS.playing = false;
  VCS.ended = false;
  cancelAnimationFrame(VCS.rafId);
  VCS.seg = null;
  const bar = vcsEl("vcs-player-progress");
  if (bar) bar.style.width = "0%";
  const timeEl = vcsEl("vcs-player-time");
  if (timeEl) timeEl.textContent = "--";
  const tag = vcsEl("vcs-player-tag");
  if (tag) tag.textContent = "未播放";
  vcsEl("vcs-play").disabled = true;
  vcsEl("vcs-pause").disabled = true;
  vcsEl("vcs-stop-play").disabled = true;
  const canvas = vcsEl("vcs-canvas");
  const ctx = canvas && canvas.getContext("2d");
  if (ctx) {
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
  }
  const overlay = vcsEl("vcs-overlay");
  if (overlay) overlay.textContent = "选择视频并开始提取，检索后点击命中片段在此播放";
  if (typeof DemoGuard !== "undefined") {
    DemoGuard.report("video-content-search", { vcs: VCS.snap, playing: false });
  }
}

/* ------------------------- WS 连接 ------------------------- */
function connectVcsWs() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}${API_PREFIX}/video-content-search/ws`;
  const ws = new WebSocket(url);
  VCS.ws = ws;
  ws.onopen = () => {
    VCS.wsRetry = 0;
    vcsShowError("");
  };
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }
    if (msg.type === "hello" || msg.type === "state") {
      vcsApplySnapshot(msg);
    } else if (msg.type === "progress") {
      const snap = Object.assign({}, VCS.snap || {});
      snap.state = "extracting";
      snap.slices_embedded = msg.embedded;
      snap.slices_total = msg.total;
      vcsApplySnapshot(snap);
    } else if (msg.type === "error") {
      vcsShowError(msg.detail || "WS 错误");
    }
  };
  ws.onclose = () => {
    VCS.ws = null;
    const wait = Math.min(5000, 500 * Math.pow(2, VCS.wsRetry++));
    VCS.wsTimer = setTimeout(connectVcsWs, wait);
  };
  ws.onerror = () => {
    try { ws.close(); } catch (_) {}
  };
}

function closeVcsWs() {
  if (VCS.wsTimer) { clearTimeout(VCS.wsTimer); VCS.wsTimer = null; }
  if (VCS.ws) { try { VCS.ws.close(); } catch (_) {} VCS.ws = null; }
  VCS.wsRetry = 0;
}

/* ------------------------- REST 动作 ------------------------- */
async function vcsRefresh() {
  try {
    const r = await fetch(API_PREFIX + "/video-content-search");
    if (!r.ok) throw new Error("HTTP " + r.status);
    const snap = await r.json();
    vcsApplySnapshot(snap);
  } catch (err) {
    vcsSetStatus("加载失败: " + err.message, true);
  }
}

async function vcsStartExtract() {
  const source = (vcsEl("vcs-source")?.value || "").trim();
  if (!source) { vcsShowError("请先选择视频文件"); return; }
  const chunkSeconds = Number(vcsEl("vcs-chunk-seconds")?.value || 4);
  const overlap = Number(vcsEl("vcs-chunk-overlap")?.value || 1);
  try {
    const r = await fetch(API_PREFIX + "/video-content-search/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "HTTP " + r.status);
    vcsApplySnapshot(data);
  } catch (err) {
    vcsShowError(err.message);
    return;
  }
  try {
    const r = await fetch(API_PREFIX + "/video-content-search/extract", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chunk_seconds: chunkSeconds, chunk_overlap_seconds: overlap }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "HTTP " + r.status);
    vcsApplySnapshot(data);
  } catch (err) {
    vcsShowError(err.message);
  }
}

async function vcsStopExtract() {
  try {
    const r = await fetch(API_PREFIX + "/video-content-search/extract/stop", { method: "POST" });
    const data = await r.json();
    if (r.ok) vcsApplySnapshot(data);
  } catch (_) {}
}

async function vcsQuery() {
  const text = (vcsEl("vcs-text")?.value || "").trim();
  if (!text) { vcsShowError("请输入检索问题"); return; }
  const threshold = Number(vcsEl("vcs-threshold")?.value || 0.5);
  const topK = Number(vcsEl("vcs-top-k")?.value || 10);
  try {
    const r = await fetch(API_PREFIX + "/video-content-search/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, sim_threshold: threshold, top_k: topK }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "HTTP " + r.status);
    vcsApplySnapshot(data);
  } catch (err) {
    vcsShowError(err.message);
  }
}

function initVideoContentSearch() {
  if (VCS.initialized) return;
  VCS.initialized = true;

  const refreshBtn = vcsEl("vcs-refresh");
  const browseBtn = vcsEl("vcs-browse");
  const startBtn = vcsEl("vcs-start");
  const stopBtn = vcsEl("vcs-stop");
  const queryBtn = vcsEl("vcs-query");
  const errClose = vcsEl("vcs-error-close");
  const resultsClear = vcsEl("vcs-results-clear");
  const sourceEl = vcsEl("vcs-source");
  const textEl = vcsEl("vcs-text");

  const syncSourceMeta = () => {
    const v = (sourceEl?.value || "").trim();
    vcsSetSourceMeta(v ? "已选: " + v : "尚未选择");
    vcsUpdateButtons();
  };
  sourceEl?.addEventListener("input", syncSourceMeta);
  sourceEl?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") startBtn?.click();
  });
  syncSourceMeta();

  browseBtn?.addEventListener("click", () => {
    if (!sourceEl) return;
    VdFsModal._sourceTarget = sourceEl;
    VdFsModal.open("");
  });
  refreshBtn?.addEventListener("click", vcsRefresh);
  startBtn?.addEventListener("click", vcsStartExtract);
  stopBtn?.addEventListener("click", vcsStopExtract);
  queryBtn?.addEventListener("click", vcsQuery);
  errClose?.addEventListener("click", () => vcsShowError(""));
  resultsClear?.addEventListener("click", async () => {
    try {
      const r = await fetch(API_PREFIX + "/video-content-search/query", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: "清空占位", sim_threshold: 1.0, top_k: 1 }),
      });
      if (r.ok) vcsApplySnapshot(await r.json());
    } catch (_) {}
  });

  // 50 字计数
  textEl?.addEventListener("input", () => {
    const n = (textEl.value || "").length;
    const c = vcsEl("vcs-ta-count");
    if (c) {
      c.textContent = n + " / 50";
      c.classList.toggle("over", n >= 50);
    }
  });

  // 播放器按钮
  vcsEl("vcs-play").addEventListener("click", () => {
    if (!VCS.seg || VCS.playing) return;
    // 上次已播完：从开头重播（进度条归零）
    if (VCS.ended) {
      VCS.ended = false;
      const bar = vcsEl("vcs-player-progress");
      if (bar) bar.style.width = "0%";
      VCS.segStartAt = performance.now();
    } else {
      VCS.segStartAt = performance.now() - (VCS.seg.dur * 1000 * vcsPlayerFrac());
    }
    VCS.playing = true;
    vcsEl("vcs-play").disabled = true;
    vcsEl("vcs-pause").disabled = false;
    VCS.rafId = requestAnimationFrame(vcsPlayerTick);
    if (typeof DemoGuard !== "undefined") {
      DemoGuard.report("video-content-search", { vcs: VCS.snap, playing: true });
    }
  });
  vcsEl("vcs-pause").addEventListener("click", vcsPausePlayback);
  vcsEl("vcs-stop-play").addEventListener("click", vcsStopPlayback);

  vcsRefresh();
  if (!VCS.ws && !VCS.wsTimer) connectVcsWs();
}

function vcsPlayerFrac() {
  const bar = vcsEl("vcs-player-progress");
  if (!bar || !bar.style.width) return 0;
  const p = parseFloat(bar.style.width);
  return isFinite(p) ? p / 100 : 0;
}

/* ------------------------------------------------------------------ *
 *  video-file-analysis page (后端 VLM 对接)                           *
 *                                                                    *
 *  后端: app/services/video_file_analysis.py —— 独立 rkv.VideoCapture*
 *  + 后台线程,每 N 帧调一次 ComputeRegistry.get_session("video_query")*
 *  出 per-frame 文本,文件走完后跑综述。前端通过 WS 订阅新事件与综述  *
 *  就绪通知,REST 兜底拉取初始快照。                                    *
 * ------------------------------------------------------------------ */
const VFA = {
  ws: null,
  wsRetry: 0,
  wsTimer: null,
  state: "idle",
  events: [],
  summary: "",
  // 缓存最近一次 state 推送里的元信息；进度推进靠它 + VFA.events.length
  // 实时计算 —— 避免每帧 VLM 完成都要等下一次 state 推送（之前是 2s
  // 节流，导致进度条"跟不上"分析）。
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
      // 三个进度分开显示：已读（decoder）/ 已采样（decoder 已 push 进队列）/
      // 已分析（worker 已完成 LLM）。差值 = 队列里/正在 LLM 的样本数。
      // 之前只显示 frame_pos + analyzed，肉眼上看不出为什么事件数远落后于
      // 当前帧号 —— 实际是 LLM 调用周期导致的 pipeline 串行（详细分析见
      // service._decode_loop 的反压注释）。
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
      // 综述阶段：per-frame 已结束，正在跑 Apollo 三段式总结。
      // summary_progress: [done, total]（chunk 小结完成数 / 总数）。
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
  // 服务端 event id 严格递增 —— 客户端也按 id 升序。
  VFA.events.push(ev);
  if (VFA.events.length > 5000) VFA.events.splice(0, VFA.events.length - 5000);
  vfaRenderEventList();
  // 每条 event = 一帧 VLM 完成 → 即时推进进度条（不等 2s state 节流）
  vfaUpdateProgress();
}

// 根据状态自动折叠/展开"逐帧分析" details：
//   running / opening / summarizing   → 展开（用户主动分析中要看事件）
//   done / stopped / error / idle     → 折叠（不再追加新事件，腾出空间给综述）
// 用户后续可以手动点 summary 重新展开看历史结果。
//
// 注意：只在前一个状态和当前状态**不同**时同步，避免 WS 每 ~2s 推一次
// state 推送就把用户手动展开的 details 合上。只在 idle ↔ running 这种
// "语义转换" 上才动 DOM。
function vfaSyncEventsCollapse(state) {
  const details = document.getElementById("vfa-events-details");
  if (!details) return;
  const prev = VFA._lastCollapseState;
  if (prev === state) return;          // 同一状态重复推送：不动
  VFA._lastCollapseState = state;
  // 第一次调用（prev === undefined）：保留 HTML 默认 open 状态，不动。
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
  // 新的在前 —— 视觉上突出最新结果
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
  // 缓存元信息（total_frames / every_n / 时间戳），后续每帧 event 推
  // 送时按这个 + VFA.events.length 推进进度条，不依赖 state 节流。
  VFA.meta.total_frames = Number(snap.total_frames) || 0;
  VFA.meta.every_n      = Number(snap.every_n) || 30;
  VFA.meta.started_at   = Number(snap.started_at) || 0;
  VFA.meta.finished_at  = Number(snap.finished_at) || 0;
  VFA.meta.fps          = Number(snap.fps) || 0;
  vfaSetRuntime(vfaFormatRuntime(snap));
  vfaRefreshButtons(snap);
  vfaRenderSnapshot(snap);
  vfaUpdateProgress(snap);
  // 状态切换时同步"逐帧分析"details的折叠状态：
  // running / opening / summarizing → 展开，done / stopped / error / idle → 折叠
  vfaSyncEventsCollapse(snap.state);
  if (snap.state === "error" && snap.error) vfaShowError(snap.error);
  if (snap.state === "running" || snap.state === "opening") vfaShowError("");
  // DemoGuard：演示页离开守卫需要 VFA 当前快照判定是否在跑。
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
 *   analyzed     = VFA.events.length（每条 event 对应一帧推理）
 *   progress     = analyzed / expected
 */
function vfaUpdateProgress(snap) {
  const root      = document.getElementById("vfa-progress");
  const phaseEl   = document.getElementById("vfa-progress-phase");
  const metaEl    = document.getElementById("vfa-progress-meta");
  const trackEl   = document.getElementById("vfa-progress-track");
  const fillEl    = document.getElementById("vfa-progress-analyze");
  const nowEl     = document.getElementById("vfa-progress-count-now");
  const totalEl   = document.getElementById("vfa-progress-count-total");
  if (!root || !trackEl || !fillEl || !nowEl || !totalEl) return;

  const state = (snap?.state) || VFA.state || "idle";

  // --- 签名缓存：state 推送每 2s 来一次但内容常相同；无变化就完全
  // 跳过 DOM 操作（包括 reflow），避免选视频源时界面抖动。
  // 注意：event 推送没有 snap，必须每次都跑（analyzed 变了）。
  // summarizing 阶段必须把 summary_progress 也纳入签名，否则 chunk 完成时
  // 进度条不刷新（用户看不到 "生成综述中 3/10"）。
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

  // meta 行（fps + 已耗时）—— 只有 state 推送才携带时间戳，所以这里
  // 在 event 推送时跳过，避免跳变；用定时器补一个轻量刷新即可。
  // summarizing 阶段也展示已耗时，让用户感知还在干活（不是卡死）。
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

  // --- 进度：analyzed / expected -------------------------------------
  // summarizing 阶段：把进度比换成 chunk 完成度（[done, total]）。
  const totalFrames = VFA.meta.total_frames;
  const everyN      = VFA.meta.every_n;
  const analyzed    = VFA.events.length;

  let expected = 0;
  let ratio = null;
  let progressLabel = null;
  // 进度条只在"真正在干活或刚干完"的状态下显示数字：
  //   running / opening  → 实跑帧数（analyzed / expected）
  //   summarizing       → chunk 完成度
  //   done              → 100% 满格
  // idle / stopped / error 一律不显示帧数（哪怕 VFA.meta 残留上次运行的值
  // —— 此时 ratio=null → indeterminate 滑动 / 0%，避免用户以为"还在跑"）。
  const showNumbers = state === "running" || state === "opening" ||
                      state === "summarizing" || state === "done";
  if (!showNumbers) {
    expected = 0;
    ratio = null;
    progressLabel = null;
  } else if (state === "summarizing" && snap && Array.isArray(snap.summary_progress)
      && snap.summary_progress[1] > 0) {
    expected = snap.summary_progress[1];
    ratio = Math.min(1, snap.summary_progress[0] / expected);
    progressLabel = "综述 chunk " + snap.summary_progress[0] + "/" + expected;
  } else if (totalFrames > 0 && everyN > 0) {
    expected = Math.max(1, Math.floor(totalFrames / everyN));
    ratio = Math.min(1, analyzed / expected);
  } else if (state === "done") {
    // 不知道总数但已完成 → 用实际数量填到 100%
    expected = analyzed;
    ratio = 1;
  }

  if (progressLabel) {
    nowEl.textContent = progressLabel;
    totalEl.textContent = "已分析 " + analyzed + " 帧";
  } else {
    nowEl.textContent = String(analyzed);
    totalEl.textContent = String(expected);
  }

  if (ratio === null) {
    // 不知道总数 —— 只有正在分析时才显示 indeterminate 滑动；
    // idle / opening / done 等状态下让条静止，避免选视频源时界面抖动。
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

// 兼容旧调用点（vfaApplyProgress 是旧名字，重命名后留个 alias）
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
      // run 进入终态（done/stopped/error）→ 后端已落盘，刷新历史
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

// ---- 历史 run 列表 ----
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
    // 三个操作按钮：颜色区分 + emoji 图标，让"删除"明显是破坏性、
    // 让"加载"和"反查"都是实色主操作（用蓝/青区分），用户扫一眼
    // 就能定位正确按钮。margin-left:auto 把"删除"推到最右，跟主操作
    // 视觉分离，避免误点。
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

// ---- 反查 modal ----
const VFAQuery = {
  runId: null,
  facets: { scenes: [], actors: [], objects: [] },
  selected: { actors: new Set(), objects: new Set(), actions: new Set() },
};

// 严格的 HH:MM:SS 格式（跟 YouTube / Bilibili 等主流视频工具一致）。
//   "00:00:00" / "0:00:00"  / "1:30:45"  / "99:59:59" → 接受
//   "30" / "1:30"            / "abc" / "1:30:45:00"  → 拒绝（返回 null）
// 拒绝的输入：buildQuery 跳过这条 filter + 通过 CSS 红框 + 错误提示告诉用户。
// 不用 `[0-5]\d` 死板地要求 2 位数字（"5" 也合法 —— 但 0-5 单字符的更精确，
// 所以 regex 是 `([0-5]?\d)` 允许 "0" "5" "00" "59" 但拒绝 "60"）。
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
  // regex 已经排除了 MM/SS ≥ 60 的情况，但保险起见再验一次。
  if (min > 59 || s > 59) return null;
  if (h < 0) return null;
  return h * 3600 + min * 60 + s;
}

// 把秒数格式化成 HH:MM:SS 字符串（强制两位 padding）。
// 主要给 hint / placeholder / 截断提示用。formatVideoTimestamp 是另一个
// 包含毫秒的格式（取帧 modal 用），这里用纯秒版让 placeholder 简洁。
function _formatTimeHHMMSS(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "00:00:00";
  const total = Math.floor(seconds);
  const hh = Math.floor(total / 3600);
  const mm = Math.floor((total % 3600) / 60);
  const ss = total % 60;
  const p = (n) => String(n).padStart(2, "0");
  return `${p(hh)}:${p(mm)}:${p(ss)}`;
}

// 校验 input 是否符合 HH:MM:SS 格式 + 写红框 / 错误消息。
// 在 input 的 on-blur 触发（不是 on-input —— 用户输到一半如 "0" 是
// 合法中间态，会闪红框，太烦）。返回 boolean 方便 submit 复用。
function _validateTimeInput(el) {
  if (!el) return true;
  const raw = el.value.trim();
  // 空：合法（用户没填），清掉红框
  if (!raw) {
    el.classList.remove("vfa-query-input-invalid");
    el.removeAttribute("title");
    return true;
  }
  // 格式必须严格 HH:MM:SS
  if (_TIME_HHMMSS_RE.test(raw)) {
    el.classList.remove("vfa-query-input-invalid");
    el.removeAttribute("title");
    return true;
  }
  el.classList.add("vfa-query-input-invalid");
  el.title = "格式错误：请用 HH:MM:SS（例如 00:01:30）";
  return false;
}

// 秒数 → frame 编号。Math.floor 截断到整数（0.999s @ 30fps → frame 29，
// 跟实际解码序号一致；"软"取舍比 round 更可预测）。
// fps<=0 时返回 null（无法转换 —— 调用方跳过该 filter）。
function _secondsToFrame(seconds, fps) {
  if (!Number.isFinite(seconds) || !Number.isFinite(fps) || fps <= 0) return null;
  if (seconds < 0) return null;
  return Math.floor(seconds * fps);
}

function vfaQueryOpen(runId, sourceLabel, totalFrames, fps) {
  VFAQuery.runId = runId;
  // 存 fps / totalFrames 到 VFAQuery，让 vfaQueryBuildQuery 转换时间 → frame
  // 时能拿到（不需要每次开 modal 时重新传参）。
  VFAQuery.fps = fps;
  VFAQuery.totalFrames = totalFrames;
  VFAQuery.selected = { actors: new Set(), objects: new Set(), actions: new Set() };
  document.getElementById("vfa-query-source").textContent = sourceLabel || runId;
  document.getElementById("vfa-query").classList.remove("hidden");
  // "时间范围" 那行加个 hint：总时长。让用户知道能筛的范围上限，
  // 避免填"00:30:00"但视频只有"00:01:13"这种"搜不到也不说为啥"的尴尬。
  // totalFrames / fps 来自 list_runs（落盘 JSON + 探测自愈）。
  const hintEl = document.getElementById("vfa-query-range-hint");
  if (hintEl) {
    if (totalFrames > 0 && fps > 0) {
      const totalSec = totalFrames / fps;
      // placeholder 用 HH:MM:SS（严格格式，validation 见 on-blur handler）。
      // 用 _formatTimeHHMMSS 不用 _formatVideoTimestamp 是因为这里不需要
      // 毫秒（input 不支持小数点 + 用户感受不到 ms 精度），更简洁。
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
  // 清空旧筛选状态
  document.getElementById("vfa-query-scene").innerHTML = '<option value="">不限</option>';
  document.getElementById("vfa-query-actors").innerHTML = '<span class="vfa-query-empty">（加载中…）</span>';
  document.getElementById("vfa-query-objects").innerHTML = '<span class="vfa-query-empty">（加载中…）</span>';
  document.getElementById("vfa-query-actions").innerHTML = '<span class="vfa-query-empty">（加载中…）</span>';
  document.getElementById("vfa-query-time-min").value = "";
  document.getElementById("vfa-query-time-max").value = "";
  document.getElementById("vfa-query-exact").value = "";
  document.getElementById("vfa-query-q").value = "";
  document.getElementById("vfa-query-semantic").value = "";
  document.getElementById("vfa-query-list").innerHTML = "";
  document.getElementById("vfa-query-summary").textContent = "加载候选中…";
  vfaQueryLoadFacets();
}

function vfaQueryClose() {
  document.getElementById("vfa-query").classList.add("hidden");
  VFAQuery.runId = null;
}

// --------------------------------------------------------------------- //
// 原始帧预览弹窗：点击反查结果触发，从源视频取出对应帧显示。
// 关键路径：fetch frame JPEG → 用 blob URL 渲染到 <img>，避免 base64 膨胀。
// 取消：点关闭 / 点遮罩 / Esc 都行。
// --------------------------------------------------------------------- //
let _vfaFrameBlobUrl = null;
let _vfaFrameAbort = null;

function vfaFrameModalClose() {
  const modal = document.getElementById("vfa-frame-modal");
  if (modal) modal.classList.add("hidden");
  // 释放上一张 blob URL
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

// 把秒数格式化成视频时间戳（始终 HH:MM:SS.sss）：
//   3725.123s → "01:02:05.123"
//   48.800s  → "00:00:48.800"
//   0.456s   → "00:00:00.456"
// 不用 MM:SS 简写 —— 30 分钟视频会显示 "30:00"，看起来像 30 小时，歧义。
// 保留 3 位毫秒（mp4 容器典型精度），跨日长视频不会爆（hh 字段 > 24 时仍正确）。
//
// 实现用 totalMs（毫秒）整数化后再取 hh/mm/ss/ms，避免 0.9995s 这种临界值
// 秒字段为 0 但毫秒 = 1000（"00:00:00.1000"）的进位 bug。
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

  // 先填好文本字段（图片还在加载）
  const modal = document.getElementById("vfa-frame-modal");
  if (!modal) return;
  modal.classList.remove("hidden");
  document.getElementById("vfa-frame-modal-meta").textContent =
    "帧 " + framePos;
  document.getElementById("vfa-frame-modal-frame-pos").textContent =
    String(framePos);
  // 该帧时间戳：fps 还没拿到，先清掉（成功回调里会重填）
  const tsEl0 = document.getElementById("vfa-frame-modal-timestamp");
  if (tsEl0) tsEl0.textContent = "--";
  document.getElementById("vfa-frame-modal-source").textContent =
    "(取帧中…)";
  document.getElementById("vfa-frame-modal-text").textContent = text;
  document.getElementById("vfa-frame-modal-structured").textContent =
    _formatStructured(structured);
  _setFrameModalLoading("从原始视频取帧中…（按 every_n 抽样，frame_pos 可能较深）");

  // 释放旧 blob URL
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
      // 尝试解析 error body（HTTPException 返回 {"detail": "..."}）
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
    // 用 X-Frame-Pos header 显示服务端确认的 frame_pos（防 stale）
    const serverPos = r.headers.get("X-Frame-Pos");
    if (serverPos) {
      document.getElementById("vfa-frame-modal-frame-pos").textContent =
        serverPos;
    }
    // 该帧时间戳：frame_pos / fps = 视频内的绝对秒。老 run 可能没存 fps
    // （X-Fps header 是 "0"），那种情况保持 "--"。fps 是 float 字符串，
    // 必须 parseFloat，不能 Number()（后者会容忍垃圾字符串）。
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
    // 用 X-Source header 回填"原始视频"那栏（之前一直显示"取帧中…"是因为
    // openFrameModal 打开 modal 时先填了 loading 态、成功路径里没回填）。
    // 后端 percent-encode 后塞 header（HTTP header 必须 ASCII），前端
    // decodeURIComponent 解码还原中文 / 长路径。失败回退到原值。
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
  // scene dropdown
  const sceneSel = document.getElementById("vfa-query-scene");
  sceneSel.innerHTML = '<option value="">不限</option>';
  VFAQuery.facets.scenes.forEach((s) => {
    const opt = document.createElement("option");
    opt.value = s;
    opt.textContent = s;
    sceneSel.appendChild(opt);
  });
  // chips
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
  // 多值参数 → 多个同名 query（server 端 OR 匹配）
  VFAQuery.selected.actors.forEach((a) => params.append("actor", a));
  VFAQuery.selected.objects.forEach((o) => params.append("object", o));
  VFAQuery.selected.actions.forEach((a) => params.append("action", a));
  // 精准匹配搜索（完整字符串 substring）—— "出租车" 必须完整命中
  const exact = document.getElementById("vfa-query-exact").value.trim();
  if (exact) params.set("exact", exact);
  // 模糊搜索（n-gram 跨字段匹配）—— "雨天穿红衣" 拆 2-gram 命中
  const q = document.getElementById("vfa-query-q").value.trim();
  if (q) params.set("q", q);
  // 语义搜索（LLM 打分排序，1 次 LLM 调用）—— 自然语言理解
  const semantic = document.getElementById("vfa-query-semantic").value.trim();
  if (semantic) params.set("semantic", semantic);
  // 时间范围：用户按 HH:MM:SS 输入（严格格式），前端解析成秒数再 × fps 转
  // 成 frame 传给后端。后端参数名仍叫 frame_min/max（不要后端改）。
  // fps=0 时跳过（无法转换）。
  const tmin = document.getElementById("vfa-query-time-min").value;
  const tmax = document.getElementById("vfa-query-time-max").value;
  const fps = VFAQuery.fps || 0;
  if (tmin || tmax) {
    if (fps <= 0) {
      console.warn("vfa: 视频 fps 未知，时间范围 filter 被跳过");
    } else {
      const totalSec = (VFAQuery.totalFrames || 0) / fps;
      // 截断 end 到视频总长度（用户友好：输超出不会报错也不会返回 0 条）。
      // 把"已截断"的提示写到 #vfa-query-clamp-notice，下次开 modal 自动隐藏。
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
          // 截断：用户输超出总时长不报错（友好），用截断后的值。
          let used = sec;
          if (totalSec > 0 && sec > totalSec) {
            used = totalSec;
            showClamp(`结束时间 ${_formatTimeHHMMSS(sec)} 超过视频总时长 ${_formatTimeHHMMSS(totalSec)}，已截断到 ${_formatTimeHHMMSS(totalSec)}`);
          }
          const f = _secondsToFrame(used, fps);
          if (f !== null) params.set("frame_max", String(f));
        }
      } else {
        // 起始 / 结束任何一个变化都可能让"已截断"失效，下次开 modal 时
        // vfaQueryOpen 会清空输入 + 清 clamp notice。
      }
    }
  } else {
    // 用户没填时间范围：清掉 clamp notice（modal 关闭再重开时残留的）。
    const notice = document.getElementById("vfa-query-clamp-notice");
    if (notice) notice.classList.add("hidden");
  }
  return params.toString();
}

function vfaQueryClear() {
  document.getElementById("vfa-query-scene").value = "";
  document.getElementById("vfa-query-exact").value = "";
  document.getElementById("vfa-query-q").value = "";
  document.getElementById("vfa-query-semantic").value = "";
  document.getElementById("vfa-query-time-min").value = "";
  document.getElementById("vfa-query-time-max").value = "";
  // 清空时也清掉红框 + 截断提示
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

// 语义搜索（多批 LLM）可能 10-30 秒，AbortController 让用户能取消
let _vfaAbort = null;
let _vfaTimeoutId = null;
let _vfaElapseId = null;

async function vfaQueryRun() {
  const runId = VFAQuery.runId;
  if (!runId) return;
  const qs = vfaQueryBuildQuery();
  const summaryEl = document.getElementById("vfa-query-summary");
  const listEl = document.getElementById("vfa-query-list");
  // 收集当前所有筛选条件作为高亮 terms
  VFAQuery._terms = collectQueryTerms();

  // 语义搜索：后端先做字符重叠预过滤（毫秒级），候选 ≤ 15 个走快路径
  // （直接返回，不调 LLM）；候选 > 15 个才调 LLM，最多 2 批 × 30s ≈ 60s。
  // 精准匹配 / 模糊搜索 + 字段过滤都是纯 Python，毫秒级返回。
  // 60s hard timeout 留给"events 多 + 语义匹配少"的边缘 case，提示用户
  // 可缩小范围（时间范围 / 字段过滤 / limit）。
  const isSemantic = qs.includes("semantic=");
  const TIMEOUT_MS = 60_000;
  const runBtn = document.getElementById("vfa-query-run");

  _vfaAbort = new AbortController();
  if (isSemantic) {
    summaryEl.textContent = "语义搜索中…";
    runBtn.disabled = true;
    runBtn.textContent = "取消";
  } else {
    summaryEl.textContent = "查询中…";
  }
  listEl.innerHTML = "";

  let timedOut = false;
  _vfaTimeoutId = setTimeout(() => {
    if (_vfaAbort && !_vfaAbort.signal.aborted) {
      timedOut = true;
      _vfaAbort.abort();
    }
  }, TIMEOUT_MS);

  let elapsed = 0;
  _vfaElapseId = setInterval(() => {
    elapsed += 1;
    if (isSemantic && _vfaAbort && !_vfaAbort.signal.aborted) {
      summaryEl.textContent = `语义搜索中… ${elapsed}s`;
    }
  }, 1000);

  try {
    const r = await fetch(
      API_PREFIX + "/video-file-analysis/runs/" + encodeURIComponent(runId) +
        "/query?" + qs,
      { cache: "no-store", signal: _vfaAbort.signal }
    );
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();
    renderQueryResults(data);
  } catch (e) {
    if (e.name === "AbortError") {
      if (timedOut) {
        summaryEl.textContent =
          `语义搜索超时（>${TIMEOUT_MS/1000}s）。可缩小范围：在「时间范围」里限定起止时间（如 0:30 ~ 1:00），或改用「精准匹配 / 模糊搜索」按字面匹配。`;
      } else {
        summaryEl.textContent = "已取消查询。";
      }
    } else {
      summaryEl.textContent = "查询失败：" + e.message;
    }
  } finally {
    clearTimeout(_vfaTimeoutId);
    clearInterval(_vfaElapseId);
    _vfaAbort = null;
    if (runBtn) {
      runBtn.disabled = false;
      runBtn.textContent = "反查";
    }
  }
}

// 用户点"取消"按钮时中断查询
async function vfaQueryCancel() {
  if (_vfaAbort) {
    _vfaAbort.abort();
  }
}

// 收集所有当前 active 的搜索 terms（用于结果高亮）
function collectQueryTerms() {
  const terms = new Set();
  const sceneEl = document.getElementById("vfa-query-scene");
  if (sceneEl && sceneEl.value) terms.add(sceneEl.value);
  VFAQuery.selected.actors.forEach((a) => terms.add(a));
  VFAQuery.selected.objects.forEach((o) => terms.add(o));
  VFAQuery.selected.actions.forEach((a) => terms.add(a));
  // 精准匹配搜索（完整字符串）
  const exactEl = document.getElementById("vfa-query-exact");
  if (exactEl && exactEl.value.trim()) terms.add(exactEl.value.trim());
  // 模糊搜索（n-gram 切分）
  const qEl = document.getElementById("vfa-query-q");
  if (qEl && qEl.value.trim()) {
    const q = qEl.value.trim();
    for (let i = 0; i < q.length - 1; i++) {
      terms.add(q.slice(i, i + 2));
    }
  }
  const semEl = document.getElementById("vfa-query-semantic");
  if (semEl && semEl.value.trim()) terms.add(semEl.value.trim());
  return [...terms].filter((t) => t.length >= 1);
}

function renderQueryResults(data) {
  const summaryEl = document.getElementById("vfa-query-summary");
  const listEl = document.getElementById("vfa-query-list");
  const events = data.events || [];
  // 语义搜索结果带 rank 标记
  const isRanked = !!data.ranked;
  const isFastPath = !!data.fast_path;
  const isSampled = !!data.sampled;
  // 区分三种语义搜索路径：快路径（字符重叠）/ LLM 评分 / 采样
  let sampledNote = "";
  if (isFastPath) {
    sampledNote = "（快路径：字符重叠匹配，未调 LLM）";
  } else if (isSampled && data.total_events) {
    sampledNote = `（从 ${data.total_events} 个 events 中筛 ${data.prefiltered || data.scored_count || 0} 候选，LLM 分 ${data.batches || 1} 批）`;
  }
  const queryLabel = data.query ? ` · 查询"${data.query.slice(0, 20)}${data.query.length > 20 ? "…" : ""}"` : "";
  summaryEl.textContent = `匹配 ${data.matched} 条${isRanked ? "（语义排序）" : ""}${sampledNote}${data.matched >= data.limit ? "（已截断）" : ""} · limit=${data.limit}${queryLabel}`;
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
    // 把 event 引用挂到 DOM 上，click handler 直接拿来取数据
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
    // 语义搜索 score 显示
    if (typeof ev._score === "number") {
      const score = document.createElement("span");
      score.className = "vfa-query-result-score";
      score.textContent = "相关度 " + Math.round(ev._score * 100) + "%";
      head.appendChild(score);
    }
    head.appendChild(frame);
    head.appendChild(ts);
    li.appendChild(head);
    // 语义搜索 reason 显示
    if (ev._reason) {
      const reason = document.createElement("div");
      reason.className = "vfa-query-result-reason";
      reason.textContent = "📌 " + ev._reason;
      li.appendChild(reason);
    }
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
     // 点击 → 打开原始帧预览
     li.addEventListener("click", () => {
       openFrameModal(li);
     });
     listEl.appendChild(li);
   });
  }

 // 反查结果高亮：把当前所有筛选条件作为 terms 包 <mark>
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
     // 用转义后的 term 匹配转义后的 text（XSS-safe）
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
    VdFsModal._sourceTarget = sourceEl;
    VdFsModal.open("");
  });

  startBtn?.addEventListener("click", vfaStart);
  stopBtn ?.addEventListener("click", vfaStop);
  clearBtn?.addEventListener("click", vfaClear);
  refreshBtn?.addEventListener("click", vfaFetchEvents);
  errClose?.addEventListener("click", () => vfaShowError(""));

  // 反查 modal
  document.getElementById("vfa-query-close")?.addEventListener("click", vfaQueryClose);
  const runBtn = document.getElementById("vfa-query-run");
  runBtn?.addEventListener("click", () => {
    if (_vfaAbort) {
      // 已有查询在跑，按钮变成"取消"
      _vfaAbort.abort();
    } else {
      vfaQueryRun();
    }
  });
  document.getElementById("vfa-query-clear")?.addEventListener("click", vfaQueryClear);
  // "时间范围"输入 on-blur 触发格式校验（非 on-input —— 用户输入到一半
  // 如 "0" 是合法中间态，on-input 会闪红框，太烦）。
  document.getElementById("vfa-query-time-min")
    ?.addEventListener("blur", (e) => _validateTimeInput(e.target));
  document.getElementById("vfa-query-time-max")
    ?.addEventListener("blur", (e) => _validateTimeInput(e.target));
  document.getElementById("vfa-query")?.addEventListener("click", (e) => {
    // 点遮罩关闭
    if (e.target.id === "vfa-query") vfaQueryClose();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !document.getElementById("vfa-query").classList.contains("hidden")) {
      vfaQueryClose();
    }
  });

  // 帧预览弹窗：点关闭 / 点遮罩 / Esc 都关闭
  document.getElementById("vfa-frame-modal-close")?.addEventListener("click", vfaFrameModalClose);
  document.getElementById("vfa-frame-modal")?.addEventListener("click", (e) => {
    if (e.target.id === "vfa-frame-modal") vfaFrameModalClose();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !document.getElementById("vfa-frame-modal").classList.contains("hidden")) {
      vfaFrameModalClose();
    }
  });

  // 历史分析
  const histRefresh = document.getElementById("vfa-history-refresh");
  histRefresh?.addEventListener("click", vfaFetchHistory);

  // 初次进入：拉一次快照 + 事件，UI 立即反映已有数据
  vfaFetchSnapshot();
  vfaFetchEvents();
  vfaFetchHistory();
  // WS 自动重连
  if (!VFA.ws && !VFA.wsTimer) connectVfaWs();
}

showPage(currentPage());
