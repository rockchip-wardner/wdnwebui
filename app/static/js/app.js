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
        setVdOverlay("文件已结束 (EOF)，重新打开即可恢复", true);
      } else if (snap.type === "error") {
        setVdOverlay("WS 错误: " + (snap.detail || ""), true);
      }
      setVdMeta(snap);
      setVdProgress(snap);
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
    try { await vdAction("play"); } catch (e) { setVdOverlay("播放失败: " + e.message, true); }
  });
  pauseBtn.addEventListener("click", async () => {
    try { await vdAction("pause"); } catch (e) { setVdOverlay("暂停失败: " + e.message, true); }
  });
  stopBtn.addEventListener("click", async () => {
    try { await vdAction("stop"); } catch (e) { setVdOverlay("停止失败: " + e.message, true); }
  });

  if (!VD.ws && !VD.wsTimer) connectVdWs();
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
    // refresh JSON panel once
    const target = document.getElementById("data-video-detection");
    target.textContent = "加载中…";
    try {
      const r = await fetch(API_PREFIX + "/video-detection");
      if (r.ok) target.textContent = JSON.stringify(await r.json(), null, 2);
      else target.textContent = "(等待视频源)";
    } catch (err) {
      target.textContent = "请求失败: " + err.message;
    }
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
