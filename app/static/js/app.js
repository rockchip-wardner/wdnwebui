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

/* Draw a red bbox overlay (5s fade) on the page whose SVG element
 * matches the given id. Each page (vd / vtd) keeps its own SVG; if
 * the requested id is missing, the call is a no-op so the same hook
 * can be reused from both pages' WS handlers.
 */
function pushVdBbox(svgId, bbox, toolKey) {
  const svg = document.getElementById(svgId);
  if (!svg || !Array.isArray(bbox) || bbox.length !== 4) return;
  const [x1, y1, x2, y2] = bbox.map((v) => Number(v));
  if (![x1, y1, x2, y2].every((v) => Number.isFinite(v))) return;
  const ns = "http://www.w3.org/2000/svg";
  const rect = document.createElementNS(ns, "rect");
  const x = Math.max(0, Math.min(1000, Math.min(x1, x2)));
  const y = Math.max(0, Math.min(1000, Math.min(y1, y2)));
  const w = Math.max(0, Math.abs(x2 - x1));
  const h = Math.max(0, Math.abs(y2 - y1));
  rect.setAttribute("x", x);
  rect.setAttribute("y", y);
  rect.setAttribute("width", w);
  rect.setAttribute("height", h);
  rect.setAttribute("rx", 8);
  rect.setAttribute("class", "vd-bbox-rect");
  if (toolKey) rect.setAttribute("data-tool", toolKey);
  svg.appendChild(rect);
  setTimeout(() => {
    try { rect.remove(); } catch (_) { /* already gone */ }
  }, 5200);
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
        if (Array.isArray(snap.bbox) && snap.tool_call === "annotate_roi") {
          pushVdBbox("vd-bbox-svg", snap.bbox, snap.tool_call);
        }
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
 * 视频目标检测 (Video Target Detection) — annotate_roi                *
 * ------------------------------------------------------------------ *
 * 与音视频描述页 (`VD`) 结构一致；区别：                               *
 *   - API 前缀 `/api/video-target-detection/*`                         *
 *   - 工具固定为 `annotate_roi`（前端不暴露工具选择）                   *
 *   - canvas 之上叠 `#vtd-bbox-svg`，annotate_roi 返回 bbox 时画红框  *
 *   - 后端 detector 实例独立 (`get_target_detector()`)，与描述页互不干扰 *
 * ------------------------------------------------------------------ */
const VTD = {
  ws: null,
  wsRetry: 0,
  wsTimer: null,
  blobUrl: null,
  tools: [],
  enabledTools: [],
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
        if (Array.isArray(snap.bbox) && snap.tool_call === "annotate_roi") {
          pushVdBbox("vtd-bbox-svg", snap.bbox, snap.tool_call);
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
    const text = document.createElement("div");
    text.className = "vd-event-text";
    text.textContent = ev.text || "(空)";
    li.appendChild(meta);
    li.appendChild(text);
    list.appendChild(li);
  });
}
let vtdBadgeHideTimer = null;
function showVtdEventBadge(ev) {
  const badge = document.getElementById("vtd-event-badge");
  const text = document.getElementById("vtd-event-badge-text");
  if (!badge || !text) return;
  text.textContent = ev.text || "(空)";
  badge.classList.remove("hidden");
  badge.style.animation = "none";
  badge.offsetHeight;
  badge.style.animation = "";
  if (vtdBadgeHideTimer) clearTimeout(vtdBadgeHideTimer);
  vtdBadgeHideTimer = setTimeout(() => badge.classList.add("hidden"), 6000);
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
      `运行中 · 每 ${snap.every_n} 帧 · 队列 ${snap.queue_size} · 目标标注`;
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
  const enabled_tools = vtdCollectEnabledTools();
  try {
    const r = await fetch(API_PREFIX + "/video-target-detection/prompt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt, every_n, enabled_tools }),
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

function vtdCollectEnabledTools() {
  const root = document.getElementById("vtd-tools-list");
  if (!root) return [];
  const out = [];
  root.querySelectorAll("input[type=checkbox][data-tool-key]").forEach((el) => {
    if (el.checked && !el.disabled) out.push(el.dataset.toolKey);
  });
  return out;
}

async function vtdLoadTools() {
  const root = document.getElementById("vtd-tools-list");
  const summary = document.getElementById("vtd-tools-summary");
  if (!root) return;
  let data = { tools: [] };
  try {
    const r = await fetch(API_PREFIX + "/video-target-detection/tools");
    if (!r.ok) throw new Error("HTTP " + r.status);
    data = await r.json();
  } catch (e) {
    root.innerHTML = `<div class="vd-tools-empty">加载失败: ${e.message}</div>`;
    if (summary) summary.textContent = "加载失败";
    return;
  }
  VTD.tools = data.tools || [];
  renderVtdTools();
}

function renderVtdTools() {
  const root = document.getElementById("vtd-tools-list");
  const summary = document.getElementById("vtd-tools-summary");
  if (!root) return;
  const tools = VTD.tools || [];
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
    if (VTD.enabledTools.includes(t.key)) check.checked = true;
    check.addEventListener("change", () => {
      if (check.checked) {
        if (!VTD.enabledTools.includes(t.key)) VTD.enabledTools.push(t.key);
      } else {
        VTD.enabledTools = VTD.enabledTools.filter((k) => k !== t.key);
      }
      updateVtdToolsSummary();
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
  updateVtdToolsSummary();
}

function updateVtdToolsSummary() {
  const summary = document.getElementById("vtd-tools-summary");
  if (!summary) return;
  const enabled = vtdCollectEnabledTools();
  const total = (VTD.tools || []).length;
  summary.textContent = enabled.length
    ? `已启用 ${enabled.length} / ${total}`
    : `未启用（${total} 个可选）`;
}

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
  vtdLoadTools();
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
    // 占位页 —— 没有后端 API 也不需要 WS，仅展示静态 placeholder
    // 文案；提前 return 避免走 default 分支去 fetch 不存在的
    // ``/api/video-content-search`` 而打 log 404。
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
    // Discovery.load() 内部已经 await DiscoveryLan.load()；再启 1Hz 轮询
    // 让 enabled 时实时刷新 last_seen / scanning / 设备增删。
    DiscoveryLan.startTicking();
    return;
  }

  if (name === "im-bridge") {
    ImBridge.init();
    await ImBridge.load();
    ImBridge.ensureWs();
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
    DiscoveryLan.stopTicking();
  }
  if (prev === "im-bridge" && name !== "im-bridge") {
    ImBridge.closeWs();
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
    this._renderCols();
    this._renderSummary();
    this._renderModelStates(this._snapshot);
    this._renderTools(this._snapshot);
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
      if (inst && inst.status === "error" && inst.error) {
        const errBox = document.createElement("pre");
        errBox.className = "cc-model-error";
        errBox.textContent = inst.error;
        li.appendChild(errBox);
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
      let errBox = li.querySelector(".cc-model-error");
      if (inst && inst.status === "error" && inst.error) {
        if (!errBox) {
          errBox = document.createElement("pre");
          errBox.className = "cc-model-error";
          li.appendChild(errBox);
        }
        errBox.textContent = inst.error;
      } else if (errBox) {
        errBox.remove();
      }
    });
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
 * 注册发现 (Discovery) page — 读 registry，不调 wdiot              *
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
    // LAN 面板独立拉，失败也不影响 HW/SW 渲染
    DiscoveryLan.load().catch((e) => {
      const list = document.getElementById("discovery-lan-list");
      if (list) list.innerHTML = `<div class="page-placeholder danger">加载失败: ${e.message}</div>`;
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

/* ---------------------------------------------------------------- *
 * 局域网设备 (LAN Discovery) — Discovery 页内的子面板            *
 *                                                                    *
 * 单独成模块（不挤进 ``Discovery``）的原因：                             *
 *  1. 拉取路径独立 — `/api/lan-discovery` vs `/api/compute/capabilities`，*
 *     互不影响；HW/SW 拉挂时 LAN 还能正常刷新；                          *
 *  2. 轮询节奏独立 — LAN 在 enabled 时按 1Hz 拉（用户需要看到          *
 *     last_seen/扫描状态实时刷新），HW/SW 一次性 render；                 *
 *  3. 状态管理独立 — toggle / port / interval 跟 HW/SW dirty 状态分开。  *
 * ---------------------------------------------------------------- */
const DiscoveryLan = {
  initialized: false,
  _snapshot: null,
  _tickHandle: null,
  _inflight: false,
  _lastToggle: null,        // 防止 toggle onchange 竞态：记住"用户最后操作意图"
  _toggleInflight: false,   // PUT /api/lan-discovery 进行中 —— 期间禁止从服务端
                            // snapshot 回写 toggle.checked，避免乐观 UI
                            // 被下一轮 1Hz poll 提前拉回的旧值覆盖。
  _busyScan: false,

  init() {
    if (this.initialized) return;
    this.initialized = true;

    const toggle = document.getElementById("discovery-lan-toggle");
    const scanBtn = document.getElementById("discovery-lan-scan-now");
    const clearBtn = document.getElementById("discovery-lan-clear");
    if (toggle) toggle.addEventListener("change", () => this.onToggle());
    if (scanBtn) scanBtn.addEventListener("click", () => this.scanNow());
    if (clearBtn) clearBtn.addEventListener("click", () => this.clear());
  },

  async load() {
    this.init();
    if (this._inflight) return;
    this._inflight = true;
    try {
      const r = await fetch(API_PREFIX + "/lan-discovery", {
        cache: "no-store",
        headers: {"Cache-Control": "no-cache"},
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
    // 1Hz 刷新：enabled 时才能感知 scanning / last_seen 实时变化；
    // disabled 时也保持 ticking，让本机 IP / 子网配置变化也能反映出来。
    this._tickHandle = setInterval(() => this.load().catch(() => {}), 1000);
  },

  stopTicking() {
    if (this._tickHandle) {
      clearInterval(this._tickHandle);
      this._tickHandle = null;
    }
  },

  // ------------------------------------------------------------ toggle
  async onToggle() {
    const toggle = document.getElementById("discovery-lan-toggle");
    if (!toggle) return;
    const want = toggle.checked;
    this._lastToggle = want;
    this._toggleInflight = true;
    try {
      const r = await fetch(API_PREFIX + "/lan-discovery", {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({enabled: want}),
      });
      const text = await r.text();
      let body;
      try { body = JSON.parse(text); } catch { body = {detail: text}; }
      if (!r.ok) {
        const msg = body.detail || ("HTTP " + r.status);
        this._setSummary("切换失败: " + msg, "danger");
        // 回滚 UI
        toggle.checked = !want;
        this._lastToggle = !want;
        return;
      }
      if (body && body.snapshot) {
        this._snapshot = body.snapshot;
      }
      this.render();
    } catch (e) {
      this._setSummary("切换失败: " + e.message, "danger");
      toggle.checked = !want;
      this._lastToggle = !want;
    } finally {
      this._toggleInflight = false;
    }
  },

  async scanNow() {
    if (this._busyScan) return;
    const btn = document.getElementById("discovery-lan-scan-now");
    if (btn) btn.disabled = true;
    this._busyScan = true;
    try {
      const r = await fetch(API_PREFIX + "/lan-discovery/scan", {
        method: "POST",
      });
      if (!r.ok) {
        const text = await r.text();
        this._setSummary("触发失败: " + text, "danger");
      } else {
        this._setSummary("已触发扫描，等待结果…");
      }
    } catch (e) {
      this._setSummary("触发失败: " + e.message, "danger");
    } finally {
      if (btn) btn.disabled = false;
      this._busyScan = false;
      // 扫描大约 2-5s 完成，期间 load() 会带回新设备
      setTimeout(() => this.load().catch(() => {}), 1500);
      setTimeout(() => this.load().catch(() => {}), 4000);
    }
  },

  async clear() {
    if (!confirm("确定要清空已发现的局域网设备列表吗？\n（仅清内存，下次扫描会重新填充）")) return;
    try {
      const r = await fetch(API_PREFIX + "/lan-discovery/devices", {
        method: "DELETE",
      });
      const text = await r.text();
      let body;
      try { body = JSON.parse(text); } catch { body = {detail: text}; }
      if (!r.ok) {
        this._setSummary("清空失败: " + (body.detail || ("HTTP " + r.status)), "danger");
        return;
      }
      if (body && body.snapshot) this._snapshot = body.snapshot;
      this.render();
    } catch (e) {
      this._setSummary("清空失败: " + e.message, "danger");
    }
  },

  // ------------------------------------------------------------ render
  render() {
    const snap = this._snapshot;
    if (!snap) return;
    const toggle = document.getElementById("discovery-lan-toggle");
    const hint = document.getElementById("discovery-lan-hint");
    const summary = document.getElementById("discovery-lan-summary");
    const meta = document.getElementById("discovery-lan-meta");
    const list = document.getElementById("discovery-lan-list");
    const scanBtn = document.getElementById("discovery-lan-scan-now");
    const clearBtn = document.getElementById("discovery-lan-clear");

    if (toggle && this._lastToggle === null) {
      toggle.checked = !!snap.enabled;
      this._lastToggle = !!snap.enabled;
    } else if (toggle && !this._toggleInflight && toggle.checked !== snap.enabled) {
      // 服务端状态跟 UI 不一致：只在非"用户刚刚主动切"的瞬间才同步
      // （onToggle 内部已经维护 _lastToggle 与服务端对齐，不会跑到这里）。
      // _toggleInflight 期间跳过同步，否则 1Hz 轮询可能在 PUT 完成前
      // 拉回旧 enabled 值把用户的乐观 UI 覆盖掉。
      toggle.checked = !!snap.enabled;
      this._lastToggle = !!snap.enabled;
    }

    const enabled = !!snap.enabled;
    const devices = snap.devices || [];
    const online = devices.filter((d) => d.online).length;

    if (hint) {
      hint.textContent = enabled
        ? `在线 ${online} · 总计 ${devices.length}`
        : `未启用 · 列表 ${devices.length}`;
    }

    if (scanBtn) scanBtn.disabled = this._busyScan;
    if (clearBtn) clearBtn.disabled = devices.length === 0;

    if (summary) {
      summary.classList.remove("danger");
      if (!enabled) {
        summary.textContent = "在网注册已关闭。打开开关后，浏览器会周期性拉取本机所在 /24 子网的设备列表。";
      } else if (snap.scanning) {
        const subs = (snap.subnets || []).join(", ") || "—";
        summary.textContent = `扫描中… 候选子网 ${subs}（每 ${snap.config?.scan_interval_seconds ?? "—"} 秒自动复扫）`;
      } else if (devices.length === 0) {
        summary.textContent = `已启用但暂未发现设备（最近一次扫描：${this._fmtTime(snap.last_scan_finished)}）`;
      } else {
        summary.textContent = `已发现 ${devices.length} 台设备，在线 ${online}（最近一次扫描：${this._fmtTime(snap.last_scan_finished)}）`;
      }
    }

    if (meta) {
      const cfg = snap.config || {};
      const localIps = snap.local_ips || [];
      const subnets = snap.subnets || [];
      const subList = subnets.length
        ? subnets.map((s) => `<span class="discovery-lan-meta-item"><strong>${this._esc(s)}</strong> · ${snap.hosts_per_subnet?.[s] ?? "?"} 主机</span>`).join("")
        : `<span class="discovery-lan-meta-item is-error">无可用私有子网</span>`;
      meta.innerHTML = `
        <span class="discovery-lan-meta-item">本机 IP：${localIps.length ? localIps.map((ip) => `<strong>${this._esc(ip)}</strong>`).join(" · ") : `<strong class="is-error">未取到</strong>`}</span>
        <span class="discovery-lan-meta-item">扫描端口：<strong>${cfg.port ?? snap.default_port}</strong></span>
        <span class="discovery-lan-meta-item">扫描间隔：<strong>${cfg.scan_interval_seconds ?? "—"}s</strong></span>
        ${subList}
      `;
    }

    if (!list) return;
    if (devices.length === 0) {
      list.innerHTML = `
        <div class="page-placeholder">
          <div class="page-placeholder-icon" aria-hidden="true">⌖</div>
          <div class="page-placeholder-title">${enabled ? "尚未发现设备" : "未开启在网注册"}</div>
          <div class="page-placeholder-text">
            ${enabled
              ? "扫描正在进行中；如果长时间没有设备出现，请确认其它 pyrkaiweb 实例在同一网段且端口可访问。"
              : "打开\"在网注册\"开关后，会周期性扫描本机所在 /24 子网内的其他 pyrkaiweb 实例，并把它们的设备名与能力列出来。"}
          </div>
        </div>`;
      return;
    }
    list.innerHTML = "";
    for (const d of devices) {
      const card = document.createElement("article");
      const cls = ["discovery-card"];
      if (!d.online) cls.push("is-offline");
      card.className = cls.join(" ");

      const swCaps = Array.isArray(d.sw_capabilities) ? d.sw_capabilities : [];
      const hwCaps = Array.isArray(d.hw_capabilities) ? d.hw_capabilities : [];
      const capChips = [];
      for (const c of swCaps) {
        const t = this._esc(c.type || "?");
        const n = c.model ? ` · ${this._esc(c.model)}` : "";
        capChips.push(`<span class="discovery-card-lan-cap" title="${t}${n}">${t}</span>`);
      }
      for (const c of hwCaps) {
        const t = this._esc(c.capability_type || "?");
        capChips.push(`<span class="discovery-card-lan-cap is-hw" title="${t}">${t}</span>`);
      }
      if (capChips.length === 0) {
        capChips.push(`<span class="discovery-card-lan-cap is-empty">无能力</span>`);
      }

      card.innerHTML = `
        <header class="discovery-card-head">
          <code class="discovery-cap-name">${this._esc(d.device_name || d.ip)}</code>
          <span class="discovery-card-lan-ip">${this._esc(d.ip)}:${d.port}</span>
        </header>
        <div class="discovery-card-meta">
          <span>${d.online ? '<span class="discovery-card-status is-on">在线</span>' : '<span class="discovery-card-status is-off">离线</span>'}</span>
          <span>RTT <strong>${d.last_rtt_ms ?? "—"}</strong> ms</span>
          <span>最近探测：<strong>${this._fmtTime(d.last_seen)}</strong></span>
          ${d.last_error ? `<span style="color:#b91c1c">${this._esc(d.last_error)}</span>` : ""}
        </div>
        <div class="discovery-card-lan-caps">${capChips.join("")}</div>
      `;
      list.appendChild(card);
    }
  },

  _setSummary(text, danger) {
    const summary = document.getElementById("discovery-lan-summary");
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

showPage(currentPage());
