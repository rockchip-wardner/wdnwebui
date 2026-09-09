const API_PREFIX = "/api";
const DEFAULT_PAGE = "system-monitor";

async function fetchVersion() {
  try {
    const r = await fetch("/api/versions");
    if (!r.ok) return;
    const data = await r.json();
    const el = document.getElementById("brand-version");
    if (el && data && data.wdnwebui) el.textContent = "v" + data.wdnwebui;
    if (data && typeof data === "object") {
      document.querySelectorAll(".brand-component-version[data-component]").forEach((node) => {
        const key = node.getAttribute("data-component");
        const v = data[key];
        if (v == null) {
          node.textContent = "missing";
          node.setAttribute("data-missing", "true");
        } else {
          node.textContent = "v" + v;
          node.removeAttribute("data-missing");
        }
      });
    }
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

  // 磁盘剩余（与内存并排第三格）
  const disk = data.disk || {};
  const diskEl = document.getElementById("disk-percent");
  const diskBar = document.getElementById("disk-bar");
  const diskMeta = document.getElementById("disk-meta");
  if (disk.available) {
    const usedPct = disk.percent == null ? null : Number(disk.percent);
    // 仍按"已用%"显示（与上方内存卡一致），让用户一眼看出"快满了"。
    diskEl.textContent = usedPct == null ? "--" : "已用 " + fmtNum(usedPct) + " %";
    setBar("disk-bar", usedPct);
    const freeTxt = disk.free_mb != null ? fmtBytes(disk.free_mb) : "--";
    const totalTxt = disk.total_mb != null ? fmtBytes(disk.total_mb) : "--";
    diskMeta.textContent =
      "剩余 " + freeTxt + " / " + totalTxt + " （" + (disk.path || "/") + "）";
  } else {
    diskEl.textContent = "offline";
    setBar("disk-bar", null);
    diskMeta.textContent = (disk.path || "/") + ": " + (disk.reason || "unavailable");
  }

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

  // --- Worker 进程枚举（OS 真相，与 ComputeRegistry 无关）---------------
  const w = data.workers || {};
  const wBadge = document.getElementById("worker-badge");
  const wMsgEl = document.getElementById("worker-message");
  const wBody = document.getElementById("worker-tbody");
  const wRaw = document.getElementById("worker-raw");
  wBadge.classList.remove("ok", "warn", "err");
  if (!w.available) {
    wBadge.textContent = "offline";
    wBadge.classList.add("err");
    wMsgEl.classList.remove("hidden");
    wMsgEl.textContent = (w.source || "/proc") + ": " + (w.reason || "unavailable");
    wBody.innerHTML = '<tr class="worker-empty"><td colspan="7">无法枚举 worker 进程</td></tr>';
    wRaw.textContent = JSON.stringify(w, null, 2);
    return;
  }
  const list = w.workers || [];
  // 按 model_id 分组，看每组是否都在 registry 里（registry 视角的镜像见
  // /api/compute）。这里只看 OS 真相：PPID=1 = 孤儿（reload 残留）。
  const byModel = {};
  for (const wk of list) {
    const k = wk.model_id || "?";
    (byModel[k] = byModel[k] || []).push(wk);
  }
  const dupModels = Object.entries(byModel).filter(([_, arr]) => arr.length > 1);
  if (dupModels.length === 0) {
    wBadge.textContent = list.length + " 个 · 在线";
    wBadge.classList.add("ok");
    wMsgEl.classList.add("hidden");
  } else {
    wBadge.textContent = list.length + " 个 · 重名 " + dupModels.length + " 组";
    wBadge.classList.add("warn");
    wMsgEl.classList.remove("hidden");
    wMsgEl.textContent =
      "发现重名 worker：每组应留 1 个。否则 reload 漏了 dedup。可执行 "
      + "`sudo kill -KILL <pid>` 清理。";
  }
  wBody.innerHTML = "";
  if (!list.length) {
    wBody.innerHTML = '<tr class="worker-empty"><td colspan="7">当前没有任何 worker 进程</td></tr>';
  } else {
    for (const wk of list) {
      const tr = document.createElement("tr");
      // 标记「孤儿」行：PPID=1 表示脱离父进程（reload 残留），或者同 model 出现 2+ 个
      const siblings = (byModel[wk.model_id] || []).length;
      if (wk.ppid === 1 || siblings > 1) tr.className = "worker-orphan";

      const tdPid = document.createElement("td");
      tdPid.textContent = wk.pid;
      tr.appendChild(tdPid);

      const tdPpid = document.createElement("td");
      tdPpid.className = wk.ppid === 1 ? "col-ppid-1" : "";
      tdPpid.textContent = wk.ppid == null ? "--" : wk.ppid;
      tr.appendChild(tdPpid);

      const tdComm = document.createElement("td");
      tdComm.textContent = wk.comm || "--";
      tdComm.title = wk.comm || "";
      tr.appendChild(tdComm);

      const tdModel = document.createElement("td");
      tdModel.textContent = wk.model_id || "--";
      tr.appendChild(tdModel);

      const tdState = document.createElement("td");
      const state = (wk.state || "").toLowerCase();
      tdState.className = "col-state-" + state;
      tdState.textContent = wk.state || "--";
      tr.appendChild(tdState);

      const tdRss = document.createElement("td");
      tdRss.textContent = wk.rss_mb == null ? "--" : fmtBytes(wk.rss_mb);
      tr.appendChild(tdRss);

      const tdStarted = document.createElement("td");
      tdStarted.textContent = wk.started_at
        ? new Date(wk.started_at * 1000).toLocaleString()
        : "--";
      tr.appendChild(tdStarted);

      wBody.appendChild(tr);
    }
  }
  wRaw.textContent = JSON.stringify(w, null, 2);

  // SIMPLE 在网设备 — peer 列表直接拉主进程 Hub;组播不通时设
  // WDNDDS_INITIAL_PEERS 走单播 fallback。
  const dc = data.discovery || {};
  const dBadge = document.getElementById("discovery-badge");
  const dMsgEl = document.getElementById("discovery-message");
  const dBody  = document.getElementById("discovery-tbody");
  const dPeersBody = document.getElementById("discovery-peers-tbody");
  const dRaw   = document.getElementById("discovery-raw");

  const peerCount = dc.peer_count || 0;
  dBadge.classList.remove("ok", "warn", "err");
  dMsgEl.classList.add("hidden");
  if (dc.protocol === "SIMPLE" && dc.own_guid) {
    dBadge.textContent = `SIMPLE · ${peerCount} 个 peer`;
    dBadge.classList.add("ok");
    if (peerCount === 0) {
      dMsgEl.classList.remove("hidden");
      const ip = (dc.wdndds_initial_peers || []).join(", ") || "<未设置>";
      dMsgEl.textContent =
        `组播 ${dc.multicast_group} 上尚未看到任何 peer —— ` +
        `若 LAN 组播被防火墙 / AP 隔离阻断，可在本机设环境变量 ` +
        `WDNDDS_INITIAL_PEERS="${ip}" 后重启 web 服务，走单播 fallback。`;
    }
  } else if (dc.protocol === "SIMPLE") {
    dBadge.textContent = "Hub 未就绪";
    dBadge.classList.add("warn");
    dMsgEl.classList.remove("hidden");
    dMsgEl.textContent = dc.reason || "ComputeRegistry Hub 未启动";
  } else {
    dBadge.textContent = dc.protocol || "未知";
    dBadge.classList.add("warn");
  }

  const baseRows = [
    ["协议",         dc.protocol || "--"],
    ["组播地址",     dc.multicast_group || "--"],
    ["本机 GUID",    dc.own_guid ? dc.own_guid.slice(0, 24) + "…" : "--"],
    ["peer 数量",    peerCount],
    ["WDNDDS_INITIAL_PEERS",
      (dc.wdndds_initial_peers || []).join(", ") || "<未设置>"],
  ];
  dBody.innerHTML = "";
  baseRows.forEach(([k, v]) => {
    const tr = document.createElement("tr");
    const tdK = document.createElement("td"); tdK.className = "daemon-key"; tdK.textContent = k;
    const tdV = document.createElement("td"); tdV.className = "daemon-val"; tdV.textContent = String(v);
    tdV.title = String(v);
    tr.appendChild(tdK); tr.appendChild(tdV);
    dBody.appendChild(tr);
  });

  dPeersBody.innerHTML = "";
  const peers = dc.peers || [];
  if (peers.length === 0) {
    const tr = document.createElement("tr");
    tr.className = "daemon-empty";
    const td = document.createElement("td");
    td.colSpan = 4;
    td.textContent = peerCount === 0 ? "暂无 peer" : "加载中…";
    tr.appendChild(td);
    dPeersBody.appendChild(tr);
  } else {
    peers.forEach(p => {
      const tr = document.createElement("tr");
      const tdG = document.createElement("td");
      tdG.className = "daemon-val";
      tdG.textContent = (p.guid || "").slice(0, 24) + (p.guid && p.guid.length > 24 ? "…" : "");
      tdG.title = p.guid || "";
      const tdN = document.createElement("td");
      tdN.className = "daemon-val";
      tdN.textContent = p.name || "(未命名)";
      const tdI = document.createElement("td");
      tdI.className = "daemon-val";
      tdI.textContent = p.ip || "--";
      const tdT = document.createElement("td");
      tdT.className = "daemon-val";
      tdT.textContent = p.discovered_at
        ? new Date(p.discovered_at * 1000).toLocaleTimeString()
        : "--";
      tr.appendChild(tdG); tr.appendChild(tdN); tr.appendChild(tdI); tr.appendChild(tdT);
      dPeersBody.appendChild(tr);
    });
  }
  dRaw.textContent = JSON.stringify(dc, null, 2);
}

async function refreshSystemMonitor() {
  const r = await fetch(API_PREFIX + "/system-monitor", { cache: "no-store" });
  if (!r.ok) return;
  renderSystemMonitor(await r.json());
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
  audioSnap: null,   // AudioAnalysis 状态快照（{state, progress, ...}）
  init() { return initVideoDetection(); },
};

const VDDetector = {
  events: [],
  badgeTimer: null,
  refresh() { return refreshVdDetector(); },
};

// AudioAnalysis — 音频理解前端模块（嵌入音视频描述页）。
// WS 协议：连接即推 hello（含 snapshot）；每段推理完推 event +
// progress；state 变化推 state；终态时前端再 hydrate 一次 events 兜底。
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
      // 模型下拉框:running 实例可能刚加载完,重试拉一次。
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
      // 终态再 hydrate 一次避免 WS 漏推
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

// 把最新 AudioAnalysis 快照推到 DemoGuard + audio-block UI。
// 快照形状：{state, error, source, prompt, progress:[done,total],
// events_count, available} —— 与后端 AudioAnalysisService.snapshot() 对齐。
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
 *  VLM detection (wdnai) — prompt + event history + overlay            *
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
    // hydrate history if we just opened the page
    if (VDDetector.events.length === 0 && Array.isArray(data.events)) {
      // events are stored newest-first in detector; copy as-is so the
      // display matches what the server already accumulated
      VDDetector.events = data.events.slice();
      renderVdEventList();
    }
    // Vision sizes may not have been ready on first page open
    // (worker still loading). Re-poll on every detector refresh until
    // the dropdown is enabled at least once.
    const sizeEl = document.getElementById("vd-detect-size");
    if (sizeEl && sizeEl.disabled) {
      vdLoadVisionSizes();
    }
    // 模型下拉框同理:running 实例可能刚加载完 / 刚停掉,定时再拉
    // 一次保证用户能看到最新的可选实例。
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
  // image_size：用户从 ``#vd-detect-size`` 选的尺寸。``null``/空
  // /disabled 时表示"按帧自动选最近变体"，让 backend 走 legacy
  // auto-pick；服务端校验合法性（必须是 worker 已加载的某个变体
  // 尺寸，否则 400）。
  const sizeVal = sizeEl && !sizeEl.disabled ? sizeEl.value : "";
  const image_size = sizeVal ? parseInt(sizeVal, 10) : null;
  if (sizeVal && !Number.isFinite(image_size)) {
    setVdOverlay("检测尺寸值非法: " + sizeVal, true);
    return;
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
async function loadModelPick(selId, capability, storageKey) {
  const sel = document.getElementById(selId);
  if (!sel) return;
  let data = { instances: [] };
  try {
    const url = `${API_PREFIX}/compute/instances-by-capability`
              + `?capability=${encodeURIComponent(capability)}`;
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
    // source: spawned/adopted_local 显示 "本机",
    //         adopted (LAN peer) 显示 "远端"
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

  // 启用 /禁用取决于是否有 running 实例。"自动" 永远可选 (auto-pick
  // 失败时由后端 RuntimeError → 503 透传)。
  sel.disabled = insts.length === 0;
  sel.onchange = () => {
    try { window.localStorage?.setItem(storageKey, sel.value || ""); }
    catch (_) { /* best-effort */ }
  };
  if (insts.length === 0) {
    autoOpt.textContent = "自动 (本机优先 · 无可用实例)";
  }
}

/**
 * Populate the ``#vd-detect-size`` dropdown from the worker's
 * currently-loaded vision variants.
 *
 * Data flow:
 *   - GET /api/video-detection/available-vision-sizes returns
 *     ``{"sizes": [...], "default": N}`` from the running qwen3-vl-4b
 *     compute instance (queried via ComputeRegistry → worker info
 *     RPC, never hardcoded on the client).
 *   - Empty list → dropdown disabled + "无可用变体" hint.
 *   - One entry → single option, marked default.
 *   - Multiple entries → all options; first one selected (= smallest
 *     = matches the prior hardcoded 384×384 default).
 */
async function vdLoadVisionSizes() {
  const sel  = document.getElementById("vd-detect-size");
  const hint = document.getElementById("vd-detect-size-hint");
  if (!sel) return;
  let data = { sizes: [], default: null };
  try {
    const r = await fetch(API_PREFIX + "/video-detection/available-vision-sizes",
                         { cache: "no-store" });
    if (r.ok) data = await r.json();
  } catch (_) { /* best-effort: leave dropdown disabled */ }
  const sizes = Array.isArray(data.sizes) ? data.sizes.slice().sort((a, b) => a - b) : [];
  sel.innerHTML = "";
  if (!sizes.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "— 模型未加载 —";
    sel.appendChild(opt);
    sel.disabled = true;
    if (hint) hint.textContent = "无可用变体（等待模型加载）";
    return;
  }
  for (const s of sizes) {
    const opt = document.createElement("option");
    opt.value = String(s);
    opt.textContent = `${s}×${s}`;
    sel.appendChild(opt);
  }
  const def = (typeof data.default === "number" && sizes.includes(data.default))
    ? String(data.default)
    : String(sizes[0]);
  sel.value = def;
  sel.disabled = false;
  if (hint) {
    hint.textContent = sizes.length > 1
      ? sizes.join(" / ")
      : `${sizes[0]}×${sizes[0]}（单变体）`;
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
    // 始终可勾选 —— capability 未就绪时也允许预选，让用户能提前
    // 配置好工具，等 bind 了 qwen3-tts 后再启动检测。capability
    // 缺失由 detector.start() 的 tool-capability 校验兜底（HTTP 503）。
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

  // 数据驱动填充 ``#vd-detect-size`` 下拉框 —— 永远不要在
  // 客户端硬编码 384/768 列表；大小由 qwen3-vl-4b 实例的 worker
  // info RPC 动态返回。
  vdLoadVisionSizes();
  // 模型选择下拉框（音视频描述 + 音频理解都走这里）
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
  AudioAnalysis.init();
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
  // 模型下拉框:running 实例可能刚加载完,重试拉一次。
  const modelEl = document.getElementById("vtd-model-pick");
  if (modelEl && modelEl.disabled) {
    loadModelPick("vtd-model-pick",
                  "swcap_video_describe", "vtd.model_id");
  }
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
    statusEl.textContent = "不可用: " + (snap.init_error || "wdnai 未安装");
  } else if (snap.init_error) {
    statusEl.textContent = "模型加载失败: " + snap.init_error;
  } else if (snap.enabled && snap.session_ready) {
    const sizeHint = (typeof snap.image_size === "number")
      ? ` · 尺寸 ${snap.image_size}`
      : "";
    statusEl.textContent =
      `运行中 · 每 ${snap.every_n} 帧 · 队列 ${snap.queue_size} · 目标检测${sizeHint}`;
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
    // Vision sizes may not have been ready on first page open
    // (worker still loading). Re-poll on every detector refresh until
    // the dropdown is enabled at least once.
    const sizeEl = document.getElementById("vtd-detect-size");
    if (sizeEl && sizeEl.disabled) {
      vtdLoadVisionSizes();
    }
    return data;
  } catch (_) { return null; }
}
async function vtdDetectorStart() {
  const promptEl = document.getElementById("vtd-prompt");
  const everyEl  = document.getElementById("vtd-every-n");
  const sizeEl   = document.getElementById("vtd-detect-size");
  const prompt = (promptEl?.value || "").trim();
  const every_n = parseInt(everyEl?.value || "30", 10);
  if (!prompt) {
    setVtdOverlay("请先填写检测提示词", true);
    return;
  }
  // image_size：用户从 ``#vtd-detect-size`` 选的尺寸。``null``/空
  // /disabled 时表示"按帧自动选最近变体"，让 backend 走 legacy
  // auto-pick；服务端校验合法性（必须是 worker 已加载的某个变体
  // 尺寸，否则 400）。
  const sizeVal = sizeEl && !sizeEl.disabled ? sizeEl.value : "";
  const image_size = sizeVal ? parseInt(sizeVal, 10) : null;
  if (sizeVal && !Number.isFinite(image_size)) {
    setVtdOverlay("检测尺寸值非法: " + sizeVal, true);
    return;
  }
  try {
    const model_id = (document.getElementById("vtd-model-pick")?.value || "") || null;
    const r = await fetch(API_PREFIX + "/video-target-detection/prompt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        prompt, every_n,
        image_size: Number.isFinite(image_size) ? image_size : null,
        model_id: model_id,
      }),
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

/**
 * Populate the ``#vtd-detect-size`` dropdown from the worker's
 * currently-loaded vision variants.
 *
 * Data flow:
 *   - GET /api/video-detection/available-vision-sizes returns
 *     ``{"sizes": [...], "default": N}`` from the running qwen3-vl-4b
 *     compute instance (queried via ComputeRegistry → worker info
 *     RPC, never hardcoded on the client).
 *   - Empty list → dropdown disabled + "无可用变体" hint.
 *   - One entry → single option, marked default.
 *   - Multiple entries → all options; first one selected (= smallest
 *     = matches the prior hardcoded 384×384 default).
 */
async function vtdLoadVisionSizes() {
  const sel  = document.getElementById("vtd-detect-size");
  const hint = document.getElementById("vtd-detect-size-hint");
  if (!sel) return;
  let data = { sizes: [], default: null };
  try {
    const r = await fetch(API_PREFIX + "/video-detection/available-vision-sizes",
                         { cache: "no-store" });
    if (r.ok) data = await r.json();
  } catch (_) { /* best-effort: leave dropdown disabled */ }
  const sizes = Array.isArray(data.sizes) ? data.sizes.slice().sort((a, b) => a - b) : [];
  sel.innerHTML = "";
  if (!sizes.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "— 模型未加载 —";
    sel.appendChild(opt);
    sel.disabled = true;
    if (hint) hint.textContent = "无可用变体（等待模型加载）";
    return;
  }
  for (const s of sizes) {
    const opt = document.createElement("option");
    opt.value = String(s);
    opt.textContent = `${s}×${s}`;
    sel.appendChild(opt);
  }
  const def = (typeof data.default === "number" && sizes.includes(data.default))
    ? String(data.default)
    : String(sizes[0]);
  sel.value = def;
  sel.disabled = false;
  if (hint) {
    hint.textContent = sizes.length > 1
      ? sizes.join(" / ")
      : `${sizes[0]}×${sizes[0]}（单变体）`;
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

  // 数据驱动填充 ``#vtd-detect-size`` 下拉框 —— 永远不要在
  // 客户端硬编码 384/768 列表；大小由 qwen3-vl-4b 实例的 worker
  // info RPC 动态返回。
  vtdLoadVisionSizes();
  // 模型选择下拉框
  loadModelPick("vtd-model-pick", "swcap_video_describe", "vtd.model_id");

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
 *  atomically swaps the "base" section in config.jsonc without       *
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

    const promptsResetBtn = document.getElementById("bc-prompts-reset");
    if (promptsResetBtn) promptsResetBtn.addEventListener("click", () => this.resetPrompts());

    // 算力实例列表固定列（所有 MODEL_PRESETS），无需添加按钮。
  },

  // ----- 算力实例列表（compute.services[]，枚举所有 preset）----- #
  // 系统配置页的算力实例区块：表格覆盖全部 MODEL_PRESETS，
  // 不论 config.jsonc 里 services[] 是否已经写到它。每行可独立
  // 编辑 label / autostart / device；不在 services[] 里的行用
  // 默认值（label = preset.label, autostart = disabled, device = 1）
  // —— 用户可以直接编辑它，省掉「+ 添加」按钮的中间步骤。
  // 保存路径：收集所有行全量 PUT 给后端（不再是局部差集），
  // 后端 Pydantic 校验通过后写盘。

  /** Render the table: one row per MODEL_PRESETS key, with values from
   *  ``services`` (the config.jsonc contents) or defaults.
   *
   * ``services```` is [{name, label, autostart, device}, ...] from
   * GET /api/compute/config. If a preset isn't in there we still render
   * a row with defaults — that's the whole point of this section.
   */
  renderServices(services) {
    const tbody = document.getElementById("bc-cs-tbody");
    if (!tbody) return;
    tbody.textContent = "";

    // Index current services by name so we can fast-lookup each preset.
    const byName = new Map();
    for (const s of (services || [])) {
      if (s && s.name) byName.set(s.name, s);
    }

    // Stable order: walk CC_PRESET_META keys (mirrors MODEL_PRESETS).
    const presetIds = Object.keys(CC_PRESET_META || {});
    if (!presetIds.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 4;
      td.className = "bc-cs-empty";
      td.textContent = "（未检测到任何 MODEL_PRESETS —— 这通常是 SDK 加载失败，请检查 wdnai 安装）";
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }

    for (const name of presetIds) {
      const meta = CC_PRESET_META[name] || {};
      const existing = byName.get(name);
      const entry = existing || {
        name,
        label: meta.label || name,
        autostart: "disabled",
        device: 1,
      };
      tbody.appendChild(this._renderServiceRow(entry));
    }
  },

  /** Build one <tr> for the services table. */
  _renderServiceRow(entry) {
    const tr = document.createElement("tr");
    tr.dataset.name = entry.name || "";
    if (entry.autostart === "hidden") tr.classList.add("is-hidden");

    // --- name (read-only — preset id; 不能从 UI 改名) ---
    const tdName = document.createElement("td");
    const nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.className = "bc-input bc-input-name";
    nameInput.value = entry.name || "";
    nameInput.readOnly = true;
    nameInput.title = "name 锁定为 MODEL_PRESETS key；如需更换请改 MODEL_PRESETS";
    tdName.appendChild(nameInput);
    tr.appendChild(tdName);

    // --- label ---
    const tdLabel = document.createElement("td");
    const labelInput = document.createElement("input");
    labelInput.type = "text";
    labelInput.className = "bc-input";
    labelInput.value = entry.label || "";
    labelInput.placeholder = "算力配置卡显示的名字";
    labelInput.dataset.field = "label";
    labelInput.maxLength = 64;
    tdLabel.appendChild(labelInput);
    tr.appendChild(tdLabel);

    // --- autostart ---
    const tdAuto = document.createElement("td");
    const autoSel = document.createElement("select");
    autoSel.className = "bc-input";
    autoSel.dataset.field = "autostart";
    for (const v of ["disabled", "enabled", "hidden"]) {
      const opt = document.createElement("option");
      opt.value = v;
      opt.textContent = v;
      if (v === (entry.autostart || "disabled")) opt.selected = true;
      autoSel.appendChild(opt);
    }
    tdAuto.appendChild(autoSel);
    tr.appendChild(tdAuto);

    // --- device ---
    const tdDev = document.createElement("td");
    const devSel = document.createElement("select");
    devSel.className = "bc-input";
    devSel.dataset.field = "device";
    for (const v of [0, 1, 2]) {
      const opt = document.createElement("option");
      opt.value = String(v);
      opt.textContent = v === 0 ? "0 (主控)" : `${v} (182x#${v - 1})`;
      if (Number(v) === Number(entry.device ?? 1)) opt.selected = true;
      devSel.appendChild(opt);
    }
    tdDev.appendChild(devSel);
    tr.appendChild(tdDev);

    return tr;
  },

  /** Read all rows in the table into [{name, label, autostart, device}, ...].
   *  Always emits one row per MODEL_PRESETS (the table always shows all
   *  of them, so collection is symmetric).
   */
  _collectServices() {
    const tbody = document.getElementById("bc-cs-tbody");
    if (!tbody) return [];
    const out = [];
    for (const tr of tbody.querySelectorAll("tr[data-name]")) {
      const name = tr.dataset.name || "";
      const label = tr.querySelector('input[data-field="label"]')?.value.trim() || "";
      const autostart = tr.querySelector('select[data-field="autostart"]')?.value || "disabled";
      const device = parseInt(
        tr.querySelector('select[data-field="device"]')?.value || "1",
        10,
      );
      if (!name) continue;
      out.push({ name, label, autostart, device });
    }
    return out;
  },

  /** Load compute.services from GET /api/compute/config. */
  async loadServices() {
    try {
      const r = await fetch(API_PREFIX + "/compute/config");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      this.renderServices(data.services || []);
    } catch (e) {
      console.error("loadServices failed:", e);
      this.setFoot("加载算力实例列表失败: " + e.message, "danger");
    }
  },

  /** Persist the table to PUT /api/compute/config. Returns a structured
   *  result so Base.save can compose the success/error message:
   *
   *      { ok: true } | { ok: false, msg: "..." }
   */
  async saveServices() {
    const services = this._collectServices();
    // Client-side dedup (server validates too, but cheaper to fail fast).
    const seen = new Set();
    for (const s of services) {
      if (!s.name) return { ok: false, msg: "算力实例 name 不能为空" };
      if (!s.label) return { ok: false, msg: `${s.name}: label 不能为空` };
      if (seen.has(s.name)) {
        return { ok: false, msg: `同名 entry 重复: ${s.name}` };
      }
      seen.add(s.name);
    }
    try {
      const r = await fetch(API_PREFIX + "/compute/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ services }),
      });
      const text = await r.text();
      let body;
      try { body = JSON.parse(text); } catch { body = { detail: text }; }
      if (!r.ok) {
        const msg = (body.detail && JSON.stringify(body.detail))
          || ("HTTP " + r.status);
        return { ok: false, msg: `算力实例保存失败: ${msg}` };
      }
      return { ok: true, count: services.length };
    } catch (e) {
      return { ok: false, msg: `算力实例保存异常: ${e.message}` };
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
    this.loadPrompts();
    this.loadServices();
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

      try {
        const textVal = (document.getElementById("bc-prompts-vd-text")?.value || "").trim();
        const levelVal = (document.getElementById("bc-prompts-vd-level")?.value || "").trim();
        const descVal = (document.getElementById("bc-prompts-vtd-desc")?.value || "").trim();
        const r3 = await fetch(API_PREFIX + "/config/app-prompts", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            video_detection: { VLMDoc: { text: textVal, level: levelVal } },
            video_target_detection: { TargetDoc: { desc: descVal } },
          }),
        });
        if (r3.ok) {
          parts.push("提示词已保存");
        } else {
          let msg = "HTTP " + r3.status;
          try {
            const respText = await r3.text();
            const body3 = JSON.parse(respText);
            if (Array.isArray(body3.detail)) {
              msg = body3.detail
                .map((d) => (d && d.msg) ? d.msg : JSON.stringify(d))
                .join("; ");
            } else if (typeof body3.detail === "string") {
              msg = body3.detail;
            }
          } catch { /* keep HTTP status as fallback */ }
          parts.push("⚠ 提示词保存失败: " + msg);
        }
      } catch (e3) {
        parts.push("⚠ 提示词保存失败: " + e3.message);
      }

      // 算力实例列表（compute.services）：写在 app.prompts 之后；
      // 任何一项失败都不阻塞其它项的结果汇总。
      const svcs = await this.saveServices();
      if (svcs.ok) {
        parts.push(`算力实例已保存（${svcs.count} 条）`);
      } else {
        parts.push("⚠ " + svcs.msg);
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

  async loadPrompts() {
    try {
      const r = await fetch(API_PREFIX + "/config/app-prompts");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      const section = (data && data.section) || {};
      const vd = section.video_detection && section.video_detection.VLMDoc;
      const vtd = section.video_target_detection && section.video_target_detection.TargetDoc;
      const textEl = document.getElementById("bc-prompts-vd-text");
      if (textEl) textEl.value = (vd && vd.text) || "";
      const levelEl = document.getElementById("bc-prompts-vd-level");
      if (levelEl) levelEl.value = (vd && vd.level) || "";
      const descEl = document.getElementById("bc-prompts-vtd-desc");
      if (descEl) descEl.value = (vtd && vtd.desc) || "";
    } catch (e) {
      console.error("loadPrompts failed:", e);
      this.setFoot("加载提示词失败: " + e.message, "danger");
    }
  },

  async resetPrompts() {
    if (!confirm("确认还原为默认提示词？此操作会清除已保存的自定义内容。")) return;
    this.setFoot("正在还原…", "warn");
    try {
      const r = await fetch(API_PREFIX + "/config/app-prompts", {
        method: "DELETE",
      });
      if (r.ok) {
        await this.loadPrompts();
        this.setFoot("已还原为默认提示词", "ok");
      } else {
        let msg = "HTTP " + r.status;
        try {
          const respText = await r.text();
          const body = JSON.parse(respText);
          if (Array.isArray(body.detail)) {
            msg = body.detail
              .map((d) => (d && d.msg) ? d.msg : JSON.stringify(d))
              .join("; ");
          } else if (typeof body.detail === "string") {
            msg = body.detail;
          }
        } catch { /* keep HTTP status as fallback */ }
        this.setFoot("还原失败: " + msg, "danger");
      }
    } catch (e) {
      this.setFoot("还原失败: " + e.message, "danger");
    }
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
  if (prev === "im-bridge" && name !== "im-bridge") {
    ImBridge.closeWs();
  }
  if (prev === "text-chat" && name !== "text-chat") {
    TextChat.closeWs();
  }
  // 离开音视频描述页：关掉 AudioAnalysis WS（DemoGuard 才是 stop 入口）
  if (prev === "video-detection" && name !== "video-detection") {
    if (AudioAnalysis.ws) {
      try { AudioAnalysis.ws.close(); } catch (_) {}
      AudioAnalysis.ws = null;
    }
    if (AudioAnalysis.wsTimer) {
      clearTimeout(AudioAnalysis.wsTimer);
      AudioAnalysis.wsTimer = null;
    }
  }
  // 回到音视频描述页：兜底 ensureWs（防止 init 被 window.__vdInit 短路）
  if (name === "video-detection" && prev !== "video-detection") {
    AudioAnalysis.ensureWs();
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
        const aud = snap.audio && snap.audio.state === "extracting";
        return !!(dec || det || aud);
      },
      async stop() {
        await Promise.allSettled([
          fetch(API_PREFIX + "/video-detection/close",       { method: "POST" }),
          fetch(API_PREFIX + "/video-detection/prompt/stop", { method: "POST" }),
          fetch(API_PREFIX + "/audio-analysis/stop",         { method: "POST" }),
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
  // 用户在导航弹窗里选了"保持后台运行"的页面 —— hashchange 兜底不要把它们 stop
  _keptPages: new Set(),
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
        [API_PREFIX + "/audio-analysis/stop",         "POST"],
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
    if (!this.isActive(fromPage)) return "stop";
    const labels = {
      "video-detection":        "音视频描述",
      "video-target-detection": "视频目标检测",
      "video-content-search":   "视频内容检索",
      "video-file-analysis":    "视频文件分析",
      "tts":                    "语音生成",
    };
    const name = labels[fromPage] || fromPage;
    return new Promise((resolve) => {
      const modal = document.getElementById("dg-modal");
      const msgEl = document.getElementById("dg-modal-msg");
      const btnStop = document.getElementById("dg-btn-stop");
      const btnKeep = document.getElementById("dg-btn-keep");
      const btnCancel = document.getElementById("dg-btn-cancel");
      msgEl.textContent = `【${name}】后台正在运行。离开此页面将自动停止当前任务并释放算力。`;
      modal.classList.remove("hidden");
      function cleanup() {
        modal.classList.add("hidden");
        btnStop.removeEventListener("click", onStop);
        btnKeep.removeEventListener("click", onKeep);
        btnCancel.removeEventListener("click", onCancel);
        modal.removeEventListener("click", onBg);
      }
      function onStop()  { cleanup(); resolve("stop"); }
      function onKeep()  { cleanup(); resolve("keep"); }
      function onCancel(){ cleanup(); resolve("cancel"); }
      function onBg(e)   { if (e.target === modal) onCancel(); }
      btnStop.addEventListener("click", onStop);
      btnKeep.addEventListener("click", onKeep);
      btnCancel.addEventListener("click", onCancel);
      modal.addEventListener("click", onBg);
    });
  },

  // stop 后清理缓存，避免兜底 hashchange 监听把同一组 POST 再发一遍
  async stopAndClear(page) {
    const reg = this._registry[page];
    if (reg) await reg.stop();
    this._cache[page] = { session: null, detector: null, audio: null };
  },
};

document.querySelectorAll(".nav-item").forEach(a => {
  a.addEventListener("click", async (e) => {
    const target = a.dataset.page;
    const cur = currentPage();
    if (!target || target === cur) return;
    if (!DemoGuard.isActive(cur)) return;
    const decision = await DemoGuard.confirmLeave(cur);
    if (decision === "cancel") {
      e.preventDefault();
      return;
    }
    e.preventDefault();
    if (decision === "stop") {
      await DemoGuard.stopAndClear(cur);
    } else if (decision === "keep") {
      DemoGuard._keptPages.add(cur);
    }
    location.hash = "#" + target;
  });
});

window.addEventListener("beforeunload", (e) => {
  const cur = currentPage();
  if (!DemoGuard.isActive(cur)) return;
  // 只弹原生对话框；真正的 stop 在 pagehide 里 —— 用户点"否/留下"就不会触发
  e.preventDefault();
  e.returnValue = "后台任务正在运行；离开将自动停止并释放算力。";
  return e.returnValue;
});

window.addEventListener("pagehide", () => {
  // pagehide 在 beforeunload 确认放行之后才触发；用户取消则不触发
  const cur = currentPage();
  if (DemoGuard.isActive(cur)) DemoGuard.stopSync(cur);
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
    if (DemoGuard._keptPages.has(page)) continue;   // 用户已选"保持后台运行"
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
 *  with two actions — export (download the current config.jsonc) and  *
 *  import (upload a new config.jsonc to overwrite). The hidden file   *
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
      a.download = "config.jsonc";
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      this.setStatus("已导出 config.jsonc", "ok");
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
      pending: "正在重启所有协处理器 + 重启算力 worker…",
      confirm: "确定要立即重启所有协处理器与本地算力 worker 吗？"
              + "协处理器重启期间 NPU 推理不可用；worker 重启后需在"
              + "「算力配置」页重新选择 NPU 才能 inference。",
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
            `\n✓ ${meta.verb} 已完成（含算力 worker 重启）`,
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
        if (audioEl) audioEl.volume = Number(volEl.value) / 100;
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
      const ctx = await this._ensureAudioCtx();
      if (ctx) {
        try {
          if (ctx.state !== "running") await ctx.resume();
        } catch (_) {}
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
      if (this._wantsBrowser()) {
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
      if (this._wantsBrowser()) {
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

  async _ensureAudioCtx() {
    if (this.audioCtx) {
      // Chrome 后台标签会把 ctx 翻回 "suspended"，重新 resume 即可。
      if (this.audioCtx.state !== "running") {
        try { await this.audioCtx.resume(); } catch (_) {}
      }
      return this.audioCtx;
    }
    const Ctor = window.AudioContext || window.webkitAudioContext;
    if (!Ctor) return null;
    this.audioCtx = new Ctor();
    // 必须在 osc.start() 之前 resume —— 否则 keep-alive 振荡器静默 no-op。
    try { await this.audioCtx.resume(); } catch (_) {}
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
    const ctx = await this._ensureAudioCtx();
    if (!ctx) return;
    this.audioSampleRate = sampleRate || 24000;
    this.audioChannels = channels || 1;
    this.audioActive = true;
    // No pre-roll: the keep-alive oscillator + eager resume() in
    // _ensureAudioCtx already paid the AudioContext cold-start tax.
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
    if (!this.audioCtx) {
      this._setAudioChip("WebAudio ctx 未创建,请刷新页面重试", "error");
      return;
    }
    if (!this.audioActive) return;

    const volEl = document.getElementById("tts-volume");
    const vol = volEl ? Number(volEl.value) / 100 : 1.0;

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
    const f32 = new Float32Array(arrayBuf.slice(12, 12 + expectedBytes));
    for (let i = 0; i < f32.length; i++) f32[i] *= vol;
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
/* ---------------------------------------------------------------- *
 * 算力配置 (Compute Config) page — v0.6 重构                          *
 * ---------------------------------------------------------------- *
 * v0.6 把布局从"NPU 列 × 模型行"改成"模型卡片 × 目标 radio"：
 *
 *  顶部一行：内存条（3588 系统内存 + 每张 RK182x 卡显存），复用
 *           /api/system-monitor 的 memory / npu.devices 字段。
 *  中间三张：按 preset（Qwen3-VL-4B / Qwen3-TTS / Qwen3-4B）划分
 *           的 service 卡片 —— 每张卡片列出子模型清单（来自本地
 *           CC_PRESET_META，按文件后缀标注可用 target），下方用
 *           radio 选运行在哪张 NPU，点 启用 / 停用 走现有的
 *           /api/compute/instances 接口；倒计时复用 snapshot
 *           estimated_init_seconds_per_model。
 */
const CC_PRESET_META = {
  "qwen3-vl-4b": {
    label: "[VLM]视觉描述模型",
    capability_types: ["swcap_video_describe"],
    instance_name: "llm-vl",
    memory_estimate_mb: 3500,
    estimated_init_seconds: 60,
    description: "视频描述模型",
    available_targets: ["182x"],
  },
  "qwen3-tts": {
    label: "[TTS]文字转语音模型",
    capability_types: ["swcap_audio_tts"],
    instance_name: "llm-tts",
    memory_estimate_mb: 1900,
    estimated_init_seconds: 30,
    description: "文字转语音模型",
    available_targets: ["182x"],
  },
  "qwen3-4b": {
    label: "[LLM]文字对话模型",
    capability_types: ["swcap_text_chat"],
    instance_name: "llm-chat",
    memory_estimate_mb: 4950,
    estimated_init_seconds: 60,
    description: "文字对话模型",
    available_targets: ["182x"],
  },
  "qwen3-vl-embedding-2b": {
    label: "[VLEmb]视觉信息编码模型",
    capability_types: ["swcap_video_retrieval"],
    instance_name: "llm-embed",
    memory_estimate_mb: 2000,
    estimated_init_seconds: 30,
    description: "视觉信息编码模型",
    available_targets: ["182x"],
  },
  "qwen2.5-omni-3b": {
    label: "[Omni]多感知描述模型",
    capability_types: ["swcap_audio_understand"],
    instance_name: "llm-audio",
    memory_estimate_mb: 2500,
    estimated_init_seconds: 50,
    description: "多感知描述模型",
    available_targets: ["182x"],
  },
  "qwen3-5-vl-4b": {
    // Qwen3.5-VL-4B 同时声明 swcap_text_chat + swcap_video_describe
    // ——VLM-mode RKNN 既能做纯文本也能消费 vision token，一张一张卡
    // 同时满足对话页 + 视频描述页。启用后文本对话、视频描述、视频
    // 内容检索、视频文件分析、音视频描述等页均直接可用。
    label: "[VLLM]视觉和文字多模态模型",
    capability_types: ["swcap_text_chat", "swcap_video_describe"],
    instance_name: "llm-q5-vl",
    memory_estimate_mb: 3500,
    estimated_init_seconds: 60,
    description: "Qwen3.5-VL-4B 多模态模型，同时支持文本对话和视频描述",
    available_targets: ["182x"],
  },
  "LocateAnything-3b": {
    label: "[GD]视觉定位模型",
    capability_types: ["swcap_grounding"],
    instance_name: "llm-ground",
    memory_estimate_mb: 5500,
    estimated_init_seconds: 60,
    description: "视觉定位模型，给定文本描述输出图像中目标区域",
    available_targets: ["182x"],
  },
};
const CC_PRESET_ORDER = [
  "qwen3-vl-4b",
  "qwen3-5-vl-4b",     // 多模态卡（同时支持文本对话 + 视频描述）
  "qwen3-4b",          // Qwen3 纯文本
  "qwen3-tts",
  "qwen3-vl-embedding-2b",
  "qwen2.5-omni-3b",
  "LocateAnything-3b",
];

// 系统拓扑：1× RK3588 主控（不占"算力卡"位置） + N× RK182x 协卡。
// wdnai 当前会把单个 RK1828 物理卡拆成多个 context 索引（同卡多
// npu_index）返回；按物理协卡数 cap 住，避免 UI 出现重复 option。
// 增/减协卡时同步改这里 + 各处"渲染 cap" 即可。
const CC_MAX_RK182X_CARDS = 2;

const ComputeConfigPage = {
  initialized: false,
  _tickHandle: null,
  _snapshot: null,
  _system: null,        // /api/system-monitor 缓存
  _loading: false,
  _selectedTarget: {},  // {model_id: npu_index} —— 用户在 radio 上选的目标

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
      const fetchCompute = () => fetch(API_PREFIX + "/compute", {
        cache: "no-store",
        headers: {"Cache-Control": "no-cache"},
      });
      const fetchSystem = () => fetch(API_PREFIX + "/system-monitor", {
        cache: "no-store",
        headers: {"Cache-Control": "no-cache"},
      });

      // 首屏：并行拉 compute + system-monitor。
      // 之后每 tick 只拉 compute（status 变化快）。system-monitor 不
      // 再高频轮询 —— 会把 rknn-smi 打挂，3s 一次即可。
      const firstLoad = this._snapshot == null;
      const rComp = await fetchCompute();
      if (!rComp.ok) throw new Error("compute HTTP " + rComp.status);
      this._snapshot = await rComp.json();
      this._systemTickCount = (this._systemTickCount || 0) + 1;
      if (firstLoad || this._systemTickCount % 3 === 0) {
        // 每 3 个 compute tick（约 3s）刷新一次 system-monitor，
        // 顶部 3588 内存 + 每张 182x 卡显存 / 温度 / 功耗才能动。
        // rknn-smi 不扛高频轮询。
        try {
          const rSys = await fetchSystem();
          if (rSys.ok) this._system = await rSys.json();
        } catch (_e) { /* 容错：不阻塞主路径 */ }
      }
      this.render();
    } catch (e) {
      // v0.8 调试兜底：this 在 tick 链路里偶尔丢失（setInterval 调
      // 裸 method 时 this 不是 ComputeConfigPage）。setFoot 不存在时
      // 直接 console.error，不让异常覆盖住 UI。
      console.error("ComputeConfigPage.load failed:", e);
      if (typeof this.setFoot === "function") {
        this.setFoot("加载失败: " + e.message, "danger");
      }
    } finally {
      this._loading = false;
    }
  },

  startTicking() {
    if (this._tickHandle) return;
    // v0.8 修复：setInterval callback 的 `this` 在 strict / 非严格
    // 模式下分别是 undefined / window，原来的箭头 form 在对象字面量
    // 里取 lexical `this` 也变成 undefined。改用 self capture 明确
    // 绑定到 ComputeConfigPage，避免 tick → load → render 链路里
    // `this` 丢失导致 setFoot 报 "not a function"。
    const self = this;
    this._tickHandle = setInterval(function () {
      if (!self._snapshot) return;
      self.load();
    }, 1000);
  },

  stopTicking() {
    if (this._tickHandle) {
      clearInterval(this._tickHandle);
      this._tickHandle = null;
    }
  },

  tick() {
    if (!this._snapshot) return;
    this.load();
  },

  async _postInstance(_npu_index, _model) {
    // Deprecated: replaced by _enableInstance. Kept as no-op stub so any
    // lingering caller doesn't crash. New callers should use the buttons
    // wired in _makeLocalCard.
  },

  async _deleteInstance(_npu_index, _model) {
    // Deprecated: replaced by _disableInstance.
  },

  async toggle(_model, _targetNpu) {
    // Deprecated: old switch UI removed in v0.7; replaced by three
    // explicit buttons (启用 / 重启 / 禁用) on _makeLocalCard.
  },

  async stopAll() {
    if (!confirm("确定要停止所有本地算力实例吗？\n"
                 + "（adopted peer 的实例不受影响；需要到对端机器操作）")) return;
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
    this._renderMemStrip();
    this._renderBanners();
    this._renderServices();
    this._renderSummary();
  },

  // ---------- 顶部内存条（3588 系统内存 + 每张 RK182x 卡显存） ----------
  // 卡片选择规则：
  //   - 第一格固定显示 RK3588 系统内存（即使没有 NPU 卡也展示 psutil）。
  //   - 后续每张 RK182x 卡占一格，按 npu_index 升序。
  //   - /api/system-monitor 没拉到时只显示 RK3588，182x 显示 placeholder。
  _renderMemStrip() {
    const strip = document.getElementById("cc-mem-strip");
    if (!strip) return;
    strip.innerHTML = "";
    const sys = this._system || {};
    const cards = (this._snapshot && this._snapshot.cards) || [];

    // 3588 cell：永远先出。数据源 = /api/system-monitor.memory。
    strip.appendChild(this._makeMemCell({
      title: "RK3588 系统内存",
      sub:   "主控 SoC · LPDDR5 共享",
      usedMb:  sys.memory ? sys.memory.used_mb : null,
      totalMb: sys.memory ? sys.memory.total_mb : null,
      pct:     sys.memory ? sys.memory.percent : null,
      extra:   null,
    }));

    // 182x cells：以 wdnai 枚举（cards）作为唯一可信源。系统拓扑
    // 是 1× RK3588 主控（不占"算力卡"位置）+ N× RK182x 协卡；
    // wdnai 当前会把单个 RK1828 物理卡拆成多个 context 索引（同卡
    // 多个 npu_index），按物理卡数 cap 住。后续如增减协卡，调这里的
    // MAX_RK182X_CARDS 即可。
    const devices = (sys.npu && sys.npu.devices) || [];
    const npuByIdx = new Map(devices.map(d => [d.index, d]));
    const cardByIdx = new Map(cards.map(c => [c.npu_index, c]));

    const MAX_RK182X_CARDS = CC_MAX_RK182X_CARDS;
    // 按 npu_index 升序固定位置 —— 与 label "RK182x #N" 完全对齐，
    // 位置 1 ↔ #0、位置 2 ↔ #1，跨刷新稳定。详见 _sortNpuCards。
    let rk182xCards = this._sortNpuCards(cards).slice(0, MAX_RK182X_CARDS);

    // wdnai 完全枚举不到 NPU 时，rknn-smi 兜底一次（host 没有 wdnai
    // / rknn3 的开发板情况），同样按 name 排序后再 cap。
    if (rk182xCards.length === 0 && devices.length > 0) {
      const seen = new Set();
      const fallback = [];
      for (const d of devices) {
        if (seen.has(d.index)) continue;
        seen.add(d.index);
        fallback.push({
          npu_index: d.index,
          device_id: d.id || null,
        });
        if (fallback.length >= MAX_RK182X_CARDS) break;
      }
      rk182xCards = fallback.sort((a, b) => {
        const idxA = (a.npu_index || 0) - (b.npu_index || 0);
        if (idxA !== 0) return idxA;
        const da = (a.device_id || "").toString();
        const db = (b.device_id || "").toString();
        return da.localeCompare(db);
      });
    }

    if (rk182xCards.length === 0) {
      // 没有 182x —— 给一个 placeholder 占位，避免整行塌陷。
      const ph = document.createElement("div");
      ph.className = "cc-mem-cell is-missing";
      const t = document.createElement("div");
      t.className = "cc-mem-cell-head";
      const ti = document.createElement("span");
      ti.className = "cc-mem-cell-title";
      ti.textContent = "RK182x #0";
      t.appendChild(ti);
      ph.appendChild(t);
      const m = document.createElement("div");
      m.className = "cc-mem-cell-meta";
      m.textContent = "未检测到协卡";
      ph.appendChild(m);
      strip.appendChild(ph);
    } else {
      // enumerate index 就是 NPU 编号 —— 第 1 张 182x 是 #0，跟下面
      // 算力卡 "运行在" radio / 系统信息页 NPU #N 完全对齐。
      // 排序已固定（见上方 _sortNpuCards），每张物理卡始终落在同一格。
      for (const card of rk182xCards) {
        const idx = card.npu_index;
        const dev = npuByIdx.get(idx);
        strip.appendChild(this._makeMemCell({
          title: `RK182x #${idx}`,
          sub:   card.device_id || `npu #${idx}`,
          usedMb:  dev ? dev.mem_used_mb : null,
          totalMb: dev ? dev.mem_total_mb : null,
          pct:     dev && dev.mem_used_mb != null && dev.mem_total_mb
                    ? (dev.mem_used_mb / dev.mem_total_mb) * 100 : null,
          extra:   dev ? {
            temp:    dev.temp_c,
            power:   dev.power_mw,
            npuPct:  dev.npu_percent,
            status:  dev.status,
          } : null,
        }));
      }
    }
  },

  _makeMemCell({title, sub, usedMb, totalMb, pct, extra}) {
    const cell = document.createElement("div");
    cell.className = "cc-mem-cell";

    const head = document.createElement("div");
    head.className = "cc-mem-cell-head";
    const titleEl = document.createElement("span");
    titleEl.className = "cc-mem-cell-title";
    titleEl.textContent = title;
    head.appendChild(titleEl);
    const pctEl = document.createElement("span");
    pctEl.className = "cc-mem-cell-pct";
    if (pct == null) {
      pctEl.textContent = "--";
    } else {
      pctEl.textContent = fmtNum(pct, 1) + " %";
      if (pct >= 90)      pctEl.classList.add("is-crit");
      else if (pct >= 70) pctEl.classList.add("is-warn");
    }
    head.appendChild(pctEl);
    cell.appendChild(head);

    const bar = document.createElement("div");
    bar.className = "cc-mem-bar";
    const fill = document.createElement("div");
    fill.className = "cc-mem-bar-fill";
    if (pct != null) {
      fill.style.width = Math.min(100, Math.max(0, pct)) + "%";
      if (pct >= 90)      fill.classList.add("is-crit");
      else if (pct >= 70) fill.classList.add("is-warn");
    }
    bar.appendChild(fill);
    cell.appendChild(bar);

    const meta = document.createElement("div");
    meta.className = "cc-mem-cell-meta";
    const usedText  = usedMb  != null ? fmtBytes(usedMb)  : "已用 --";
    const totalText = totalMb != null ? fmtBytes(totalMb) : "--";
    meta.textContent = `${usedText} / ${totalText}`;
    cell.appendChild(meta);

    if (sub) {
      const subEl = document.createElement("div");
      subEl.className = "cc-mem-cell-meta";
      subEl.textContent = sub;
      subEl.title = sub;
      cell.appendChild(subEl);
    }
    if (extra) {
      const ex = document.createElement("div");
      ex.className = "cc-mem-cell-meta";
      const parts = [];
      if (extra.temp != null)   parts.push(extra.temp.toFixed(1) + "°C");
      if (extra.npuPct != null) parts.push("NPU " + fmtNum(extra.npuPct, 0) + "%");
      if (extra.power != null)  parts.push((extra.power / 1000).toFixed(2) + " W");
      if (extra.status)         parts.push(extra.status);
      ex.textContent = parts.join(" · ");
      cell.appendChild(ex);
    }
    return cell;
  },

  // ---------- 顶部告警条 ----------
  _renderBanners() {
    const stack = document.getElementById("cc-banner-stack");
    if (!stack) return;
    const snap = this._snapshot || {};
    // missing_model_dir 单独在算力卡上提示，不进红色 banner —— 它
    // 是资源/环境缺失，不是真正的启动错误。
    const errored = (snap.instances || []).filter(
      i => i.status === "error");
    const missingDirs = (snap.instances || []).filter(
      i => i.status === "missing_model_dir");
    const stuck = this._detectStuck(snap);

    stack.innerHTML = "";

    if (errored.length) {
      // 按 source 分桶 —— 用户主动启的（spawned）、本机孤儿
      // （adopted_local，从上次 reload 留下的 worker）、远端 peer
      // （adopted，本机无权操作）三类的"出错了"含义完全不同。混在
      // 一起报"X 个算力实例启动失败"会让用户误以为是自己点的启用失败。
      const buckets = {
        spawned: [],
        adopted_local: [],
        adopted: [],
      };
      for (const inst of errored) {
        const k = inst._source in buckets ? inst._source : "spawned";
        buckets[k].push(inst);
      }
      const renderBucket = (kind, title, list) => {
        if (!list.length) return;
        const ul = document.createElement("ul");
        ul.className = "cc-banner-list";
        for (const inst of list) {
          const li = document.createElement("li");
          const head = document.createElement("div");
          const label = inst.label || inst.model;
          let line;
          if (inst._source === "adopted_local") {
            const pid = inst.pid ? ` · PID ${inst.pid}` : "";
            line = `${label}${pid} · NPU #${inst.npu_index}`;
            head.title = `本机孤儿 worker（uvicorn reload 残留），无 Popen 句柄；要清理请 kill ${inst.pid}`;
          } else if (inst._source === "adopted") {
            const peerLabel = inst._peer_name || "远端";
            line = `${label} @ ${peerLabel} · NPU #${inst.npu_index}`;
            head.title = `远端 peer 提供的 service，本机无权操作`;
          } else {
            line = `${label} · NPU #${inst.npu_index}`;
          }
          head.textContent = line;
          li.appendChild(head);
          const msg = document.createElement("pre");
          msg.className = "cc-banner-msg";
          msg.textContent = inst.error || "(无错误信息)";
          li.appendChild(msg);
          // 单条错误右侧加 ✕，清掉这一条
          const clearBtn = document.createElement("button");
          clearBtn.type = "button";
          clearBtn.className = "cc-banner-row-clear";
          clearBtn.textContent = "✕";
          clearBtn.title = `清掉 ${inst.model} 的错误`;
          clearBtn.addEventListener("click", (ev) => {
            ev.stopPropagation();
            this._clearErrors(inst.model);
          });
          li.appendChild(clearBtn);
          ul.appendChild(li);
        }
        stack.appendChild(this._makeBanner({
          kind: kind,
          icon: kind === "is-error" ? "✖" : "⚠",
          title: title(list),
          body: ul,
          actions: [
            // v0.8 修复：之前误调 stopAll；banner 应该只清错误状态，
            // 不影响正在跑的 worker / capability。
            {label: "全部清空", onClick: () => this._clearErrors(null)},
          ],
        }));
      };

      // 三类按顺序渲染；标题文案区分清楚。
      renderBucket("is-error",
        (l) => `${l.length} 个算力实例启动失败（用户主动启用）`,
        buckets.spawned);
      renderBucket("is-warn",
        (l) => `${l.length} 个本机孤儿 worker 异常（reload 残留，需 kill PID 清理）`,
        buckets.adopted_local);
      renderBucket("is-warn",
        (l) => `${l.length} 个对端 worker 失联（远端机子，本机无权操作）`,
        buckets.adopted);
    }

    if (missingDirs.length) {
      const ul = document.createElement("ul");
      ul.className = "cc-banner-list";
      for (const inst of missingDirs) {
        const li = document.createElement("li");
        const head = document.createElement("div");
        head.textContent = `${inst.label || inst.model}${inst.model_dir ? " · " + inst.model_dir : ""}`;
        li.appendChild(head);
        ul.appendChild(li);
      }
      stack.appendChild(this._makeBanner({
        kind: "is-warn",
        icon: "⚠",
        title: `${missingDirs.length} 个算力模型目录缺失（不算"启动失败"，补齐后 toggle 一下）`,
        body: ul,
        actions: [
          {label: "刷新", onClick: () => this.load()},
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

  _detectStuck(snap) {
    const now = Date.now() / 1000;
    const perModel = snap.estimated_init_seconds_per_model || {};
    const out = [];
    for (const inst of snap.instances || []) {
      if (inst.status !== "queued" && inst.status !== "loading") continue;
      const est = perModel[inst.model_id] ?? snap.default_estimated_init_seconds ?? 10;
      const startTs = inst.status === "queued"
        ? (inst.queued_at || 0)
        : (inst.started_at || inst.queued_at || 0);
      const elapsed = Math.max(0, Math.round(now - startTs));
      const threshold = est * 2 + 30;
      if (elapsed >= threshold) {
        out.push({...inst, elapsed, threshold});
      }
    }
    return out;
  },

  // 清空错误 banner —— POST /api/compute/clear-errors
  // ``modelId=null`` 清全部；'' 也走清全部
  async _clearErrors(modelId) {
    try {
      const r = await fetch(API_PREFIX + "/compute/clear-errors", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({model_id: modelId || null}),
      });
      if (!r.ok) {
        const text = await r.text();
        alert(`清空错误失败 (HTTP ${r.status}): ${text}`);
      }
    } catch (e) {
      alert("请求失败: " + e.message);
    }
    await this.load();
  },

  // ---------- 两栏 compute service 卡片 (v0.7) ----------
  // 数据源：``/api/compute`` snapshot.services —— 后端从
  // ``list_with_source`` 拼出来，按 source 分组：
  //   * 本机算力栏（平铺）：spawned / adopted_local / unavailable
  //     - spawned        ：本机 Popen 起的 worker（持有 proc 句柄）
  //     - adopted_local  ：uvicorn reload 留下的孤儿 worker（同 hostname，
  //                         registry 没 Popen 句柄但能 RPC 触达）
  //     - unavailable    ：``MODEL_PRESETS`` 里声明但本机 + LAN 都没回应
  //                         —— 仍是**本机** model，UI 显示在本地栏并提供
  //                         「启用」按钮让用户 spawn 新进程
  //   * 网络算力栏（按 ``(device_name, peer_ip)`` 分组，每设备一框）：
  //     - adopted        ：LAN 上另一台机子的 worker 提供的 service
  // 下栏卡片只读（peer 不可操作）。
  _renderServices() {
    const localRoot = document.getElementById("cc-local-cards");
    const peerRoot  = document.getElementById("cc-peer-cards");
    if (!localRoot || !peerRoot) return;
    // v0.8: 不再 innerHTML="" 清空！会让 `<select>` 失焦 dropdown 关。
    // 改成 patch 算法：每个 model 一个 card 节点，按需 add/replace/remove，
    // 数据不变直接复用旧节点。
    const snap = this._snapshot || {};
    const cards = snap.cards || [];
    const rawServices = snap.services || [];

    // v0.8.1: 不再过滤 adopted peer —— 用户需要看到对端算力存在与否,
    // 即使 not_loaded / error 也显示 (peer 不可操作,但 visibility 重要
    // ——「在网设备」有 peer,「网络算力」看不到会让人困惑)。
    const services = rawServices;

    if (!services.length) {
      const empty = document.createElement("div");
      empty.className = "cc-services-empty";
      empty.textContent = "未声明任何 compute service —— 检查 MODEL_PRESETS 配置。";
      localRoot.appendChild(empty);
      return;
    }

    // 按 CC_PRESET_ORDER 排序，保证 UI 默认视觉顺序稳定。
    // 同 model 可以出现多条(例如 adopted 一条 + unavailable 一条),
    // dedup 用 (model_id, _source) 复合 key 让两张卡都进 ordered。
    const ordered = [];
    const seen = new Set();
    const seenKey = (s) => `${s.model_id}__${s._source || ""}`;
    for (const id of CC_PRESET_ORDER) {
      for (const s of services.filter(x => x.model_id === id)) {
        const k = seenKey(s);
        if (!seen.has(k)) { ordered.push(s); seen.add(k); }
      }
    }
    for (const s of services) {
      const k = seenKey(s);
      if (!seen.has(k)) { ordered.push(s); seen.add(k); }
    }

    // v0.8: 1Hz tick 时整张卡片重建会让 `<select>` 失去焦点
    // （浏览器原生气泡收起，用户还没选 NPU 就关闭）。
    // 修复：保留上次 render 的 card 节点缓存（this._renderedCard），
    // 数据完全没变则复用 —— select / toggle 焦点保留。
    const prevCard = this._renderedCard || {};
    const newCard = {};
    const newOrder = [];   // [{model_id, container: localRoot|<frame grid>}]

    // ---------- peer 设备框 ----------
    // 只 adopted 进网络栏，按 (device_name, peer_ip) 分组；组内按
    // CC_PRESET_ORDER 排序保证视觉一致。unavailable 是本机 preset
    // model，进本地栏（见 _renderServices 顶部 docstring）。
    const peerFrameOrder = [];          // [{key, name, ip, services: [...]}]
    const peerFrameByKey = new Map();
    for (const s of ordered) {
      if (s._source === "adopted") {
        const name = (s._peer_name || "").trim() || "远端";
        const ip = (s._peer_ip || "").trim();
        const key = `${name}@${ip}`;
        if (!peerFrameByKey.has(key)) {
          const group = { key, name, ip, services: [] };
          peerFrameByKey.set(key, group);
          peerFrameOrder.push(group);
        }
        peerFrameByKey.get(key).services.push(s);
      }
    }
    // 组内按 CC_PRESET_ORDER 排序
    for (const g of peerFrameOrder) {
      g.services.sort((a, b) => {
        const ai = CC_PRESET_ORDER.indexOf(a.model_id);
        const bi = CC_PRESET_ORDER.indexOf(b.model_id);
        const aPos = ai === -1 ? 9999 : ai;
        const bPos = bi === -1 ? 9999 : bi;
        if (aPos !== bPos) return aPos - bPos;
        return a.model_id.localeCompare(b.model_id);
      });
    }
    const prevPeerFrame = this._renderedPeerFrame || {};
    const newPeerFrame = {};

    // ---------- peer frame 渲染 / patch（**先于** 卡片创建）----------
    // 历史 bug：之前这段在卡片创建 loop 之后，导致
    // ``newPeerFrame[frameKey]?.grid`` 在卡片需要解析 targetContainer 时
    // 还是 ``undefined``，所有 peer / unavailable 卡片被 fallback 放到
    // ``peerRoot`` 里，frame 的 grid 一直是空的 —— peer 名单微微抖动
    // （member 增减）frame 就会被重建，整个块看上去"没一下"。
    // 修：先把 frame DOM 建好，卡片创建时直接拿到 grid。
    for (const group of peerFrameOrder) {
      const prev = prevPeerFrame[group.key];
      // frame signature 只看成员 identity（model_id 顺序）+ title
      // 字段。同 key 成员集合 + 顺序一致 → 复用 frame。
      const memberSig = group.services.map(x => x.model_id).join("|");
      const frameSig = `${group.name}|${group.ip}|${memberSig}`;
      if (prev && prev.frameSig === frameSig && prev.frame.parentNode === peerRoot) {
        newPeerFrame[group.key] = prev;
        continue;
      }
      // 重建 frame：把旧 frame 从 DOM 移除（如果残留）
      if (prev && prev.frame && prev.frame.parentNode) {
        prev.frame.parentNode.removeChild(prev.frame);
      }
      const frame = document.createElement("section");
      frame.className = "cc-device-frame";
      frame.dataset.deviceKey = group.key;

      const head = document.createElement("header");
      head.className = "cc-device-head";
      const title = document.createElement("h3");
      title.className = "cc-device-title";
      title.textContent = group.ip
        ? `${group.name}@${group.ip}`
        : group.name;
      head.appendChild(title);
      frame.appendChild(head);

      const grid = document.createElement("div");
      grid.className = "cc-device-cards";
      frame.appendChild(grid);

      newPeerFrame[group.key] = { frame, grid, frameSig };
    }

    for (const s of ordered) {
      // v0.8: meta 优先从服务实例（snapshot.services[i]）拿 description /
      // available_targets，再 fallback 到前端 CC_PRESET_META（submodels /
      // estimated_init_seconds 等只在前端有的字段）。
      const cm = CC_PRESET_META[s.model_id] || {};
      const meta = {
        label:                s.label || cm.label || s.model_id,
        capability_types:     s.capability_types || cm.capability_types || [],
        memory_estimate_mb:   s.memory_estimate_mb || cm.memory_estimate_mb || 0,
        estimated_init_seconds:
          cm.estimated_init_seconds
          || (snap.estimated_init_seconds_per_model || {})[s.model_id]
          || 30,
        submodels:            cm.submodels || [],
        description:          s.description || cm.description || "",
        available_targets:    s.available_targets || cm.available_targets || ["182x"],
      };
      const isLocal = s._source !== "adopted";

      // 计算关键字段 hash；任一变化 → 重建 card。
      const sig = [
        s._source,
        s.status,
        s.npu_index,
        s.pid,
        s.error || "",
        s.label,
        meta.description,
        meta.available_targets.join(","),
        cards.map(c => c.npu_index).join(","),
        // v0.8 patch: 把用户的 NPU 选取也放进 sig —— 不然 radio change
        // 后 _selectedTarget 更新了，但 sig 没变 → 节点复用 → toggle.disabled
        // 还是旧值（needsTarget && !targetPicked）。sig 包含 _selectedTarget
        // 后切 radio 会触发重建，toggle 立即变可用。
        this._selectedTarget[s.model_id] != null
          ? String(this._selectedTarget[s.model_id]) : "_",
      ].join("|");
      const prev = prevCard[seenKey(s)];
      let targetContainer;
      if (isLocal) {
        targetContainer = localRoot;
      } else {
        // adopted — 容器是 peer frame 的 grid。frame 已先创建好
        // （见上方帧循环），保证引用有效。
        const name = (s._peer_name || "").trim() || "远端";
        const ip = (s._peer_ip || "").trim();
        const frameKey = `${name}@${ip}`;
        targetContainer = newPeerFrame[frameKey]?.grid || peerRoot;
      }

      if (prev && prev.card && prev.card.parentNode) {
        // v0.10: 复用旧 card + 原地 update（不 remove / 不重建）。
        // 即使 sig 变化也只调 _updateLocalCardState 刷新节点属性 /
        // class / text —— 卡片始终在 DOM 里，"点启动后卡片消失了一
        // 下"的视觉抖动彻底消失。radio fieldset 是唯一会结构变化
        // 的部分，update 内部按 lastRadioSig 检测后 replaceWith。
        if (isLocal && prev.card._h) {
          this._updateLocalCardState(prev.card, s, meta, cards);
        } else {
          // adopted peer card，或者 prev 是 peer card 现在要变 local
          // （_h 不存在）—— 这些 case 涉及 card 类型切换，结构差别
          // 太大不能 in-place update。重建代价小（peer card 简单 /
          // local card 切换是罕见的 transition），保留旧的 remove +
          // recreate 路径。SIG 不变的 hot path 不会进这里。
          prev.card.parentNode.removeChild(prev.card);
          prev.card = isLocal
            ? this._makeLocalCard(s.model_id, meta, s, cards)
            : this._makePeerCard(s.model_id, meta, s);
          prev.sig = sig;
          prev.isLocal = isLocal;
          newCard[seenKey(s)] = prev;
          newOrder.push({model_id: s.model_id, container: targetContainer, card: prev.card});
          continue;
        }
        prev.sig = sig;
        prev.isLocal = isLocal;
        // 容器错了才 detach + appendChild；ordering 交由末尾 newOrder
        // loop 用 insertBefore 完成 —— 不要在这里做 reorder，否则 omni
        // （CC_PRESET_ORDER 末位）在循环中途会被推到位置 0，浏览器若
        // 中途 commit 就会先看到 omni 再看到真正点击的目标。
        if (prev.card.parentNode !== targetContainer) {
          if (prev.card.parentNode) prev.card.remove();
          targetContainer.appendChild(prev.card);
        }
        newCard[seenKey(s)] = prev;
        newOrder.push({model_id: s.model_id, container: targetContainer, card: prev.card});
        continue;
      }

      // 首次渲染（prev.card 不在 DOM 里）—— 调 _makeLocalCard 创建，
      // 内部 _updateLocalCardState 写入初始状态。
      const card = isLocal
        ? this._makeLocalCard(s.model_id, meta, s, cards)
        : this._makePeerCard(s.model_id, meta, s);
      newCard[seenKey(s)] = { card, sig, isLocal };
      newOrder.push({model_id: s.model_id, container: targetContainer, card});
    }

    // ---------- peer frame 渲染 / patch（已上移到卡片 loop 之前）----------

    // 按 peerFrameOrder 把 frame append 到 peerRoot（保留旧 frame
    // 节点，整体 move 顺序以贴合最新顺序）。
    for (const group of peerFrameOrder) {
      const f = newPeerFrame[group.key];
      if (!f) continue;
      // 找到目标位置（上一个 frame 之后）以保证视觉顺序
      const idx = peerFrameOrder.indexOf(group);
      const after = idx > 0 ? newPeerFrame[peerFrameOrder[idx - 1].key]?.frame : null;
      const desiredParent = peerRoot;
      const desiredNext = after ? after.nextSibling : null;
      if (f.frame.parentNode !== desiredParent) {
        desiredParent.insertBefore(f.frame, desiredNext);
      } else if (f.frame !== desiredNext) {
        desiredParent.insertBefore(f.frame, desiredNext);
      }
    }

    this._renderedPeerFrame = newPeerFrame;
    // 清理掉已不在 peerFrameOrder 里的旧 frame
    for (const [key, prev] of Object.entries(prevPeerFrame)) {
      if (!(key in newPeerFrame)) {
        try { prev.frame.remove(); } catch (_e) {}
      }
    }

    // 按 ordered 顺序把卡片放到对应 container 的正确位置（不 detach
    // 已存在的）。insertBefore + nextSibling 比较：已在正确位置则不动
    // （focus / select dropdown 保留），否则只移动一次。
    //
    // **必须按 container 分组 + 反向迭代**：
    //   * 跨 container 调用 insertBefore 真实 DOM 会抛 NotFoundError，
    //     render 抛错后 _renderedCard 不更新、cleanup 不跑，下一 tick
    //     又创建新卡片 —— 本地算力条目无限累积（peer 与本地共存即触发）。
    //   * 反向迭代保证后继链不被打乱。
    const byContainer = new Map();
    for (const item of newOrder) {
      if (!byContainer.has(item.container)) byContainer.set(item.container, []);
      byContainer.get(item.container).push(item);
    }
    for (const [container, items] of byContainer) {
      for (let i = items.length - 1; i >= 0; i--) {
        const item = items[i];
        const desiredNext = i + 1 < items.length ? items[i + 1].card : null;
        if (item.card.parentNode !== container) {
          container.insertBefore(item.card, desiredNext);
        } else if (item.card.nextSibling !== desiredNext) {
          container.insertBefore(item.card, desiredNext);
        }
      }
    }

    this._renderedCard = newCard;
    // 清理掉已不再 services 列表里的旧 card
    for (const [mid, prev] of Object.entries(prevCard)) {
      if (!(mid in newCard)) {
        try { prev.card.remove(); } catch (_e) {}
      }
    }

    // v0.9: 没有任何 peer 设备框时显示引导文案，否则隐藏
    const peerEmpty = document.getElementById("cc-peer-empty");
    if (peerEmpty) {
      const hasFrames = peerFrameOrder.length > 0;
      peerEmpty.classList.toggle("hidden", hasFrames);
    }
  },

  // 本机 worker —— spawned（本次启动由 Popen 起的）或 adopted_local
  // （uvicorn reload 留下的 worker）。adopted_local 可用 toggle 启用 /
  // bind，但 重启 按钮灰掉（worker 不在我们 Popen 控制内）。
  //
  // v0.10: 整个 card 结构（head/body 内部全部元素）一次性创建，元素
  // 全部挂到 ``card._h`` 钩子上；状态相关的内容统一交给
  // ``_updateLocalCardState`` 写入。这样 ``_renderServices`` 在 sig
  // 变化时只调 update 就能原地刷新卡片，**不会 removeChild / createElement
  // 整张卡** —— 彻底消除"点启动后卡片消失了一下"的视觉抖动。
  // radio fieldset 是唯一会结构变化的部分（cards 列表变 / available_targets
  // 变），通过 ``_h.lastRadioSig`` 检测后用 ``replaceWith`` 原地换掉，
  // 保留 body / head 不动。
  _makeLocalCard(modelId, meta, svc, cards) {
    const card = document.createElement("article");
    card.className = "cc-service-card";
    card.dataset.model = modelId;

    // ---- head: title + (optional) pid + headActions(restart / toggle)
    const head = document.createElement("header");
    head.className = "cc-service-head";
    card.appendChild(head);

    const titleEl = document.createElement("h3");
    titleEl.className = "cc-service-title";
    head.appendChild(titleEl);

    // 策略徽章：autostart=enabled 时显示"开机自启"提示用户这个实例
    // 是 boot 阶段就 spawn+bind 的，关掉只是 unload（worker 还在）；
    // 重启 / 进程退出后下次 boot 还会自动拉起。
    const autostartBadge = document.createElement("span");
    autostartBadge.className = "cc-autostart-badge";
    autostartBadge.style.display = "none";
    head.appendChild(autostartBadge);

    const pidEl = document.createElement("span");
    pidEl.className = "cc-pid";
    pidEl.style.display = "none";
    head.appendChild(pidEl);

    const headActions = document.createElement("div");
    headActions.className = "cc-head-actions";
    head.appendChild(headActions);

    // 重启按钮：永远挂 DOM 上，hidden via display:none（adopted_local /
    // unavailable 不显示）；click handler 永远绑定（display:none 期间不会
    // 被触发，避免 update 时还得重绑 listener）。
    const restartBtn = document.createElement("button");
    restartBtn.className = "cc-btn cc-btn-restart";
    restartBtn.textContent = "重启服务";
    restartBtn.title = "Q3 4 步：unload → shutdown → wait/kill → 走 boot 流程（不自动 bind）";
    restartBtn.addEventListener("click", () => {
      if (confirm(`确认重启 ${meta.label}？重启后需重新选择 NPU 才能 inference。`)) {
        this._restartInstance(modelId);
      }
    });
    headActions.appendChild(restartBtn);

    // toggle：label + hidden input + track + thumb。所有 listener / 属性
    // 一开始就绑好，后续只更新属性 & class（不重建节点）。
    const toggle = document.createElement("label");
    toggle.className = "cc-toggle";
    const toggleInput = document.createElement("input");
    toggleInput.type = "checkbox";
    toggleInput.setAttribute("aria-label", `${meta.label}：未启用，点击切换`);
    toggleInput.addEventListener("change", () => {
      if (toggleInput.disabled) return;
      // 用当前 snapshot 里的 svc，避免 update 后闭包 svc 过时。
      const curSvc = this._findSvcInSnapshot(modelId);
      const curCards = (this._snapshot && this._snapshot.cards) || [];
      const target = this._selectedTarget[modelId] != null
        ? this._selectedTarget[modelId]
        : (curSvc && curSvc.npu_index >= 0
            ? curSvc.npu_index
            : this._defaultTargetNpu(curCards));
      if (toggleInput.checked) {
        this._enableInstance(target, modelId);
      } else {
        if (confirm(`确认禁用 ${meta.label}？将卸载模型，worker 进程保留。`)) {
          this._disableInstance(modelId);
        } else {
          // 用户取消：回滚 toggle 视觉（后端 API 调用还没真正发起）
          toggleInput.checked = true;
        }
      }
    });
    const toggleTrack = document.createElement("span");
    toggleTrack.className = "cc-toggle-track";
    const toggleThumb = document.createElement("span");
    toggleThumb.className = "cc-toggle-thumb";
    toggleTrack.appendChild(toggleThumb);
    toggle.appendChild(toggleInput);
    toggle.appendChild(toggleTrack);
    headActions.appendChild(toggle);

    // ---- body: caps + missing-dir-hint + radio + countdown + error-pre
    const body = document.createElement("div");
    body.className = "cc-service-body";
    card.appendChild(body);

    // SW capability chips（swcap_video_describe / swcap_audio_tts / …）
    const capsEl = document.createElement("div");
    capsEl.className = "cc-service-caps";
    body.appendChild(capsEl);

    // missing-dir 提示块：永远占位，hidden via display:none
    const missingDirEl = document.createElement("div");
    missingDirEl.className = "cc-missing-dir";
    missingDirEl.style.display = "none";
    body.appendChild(missingDirEl);

    // radio fieldset 占位（_updateLocalCardState 第一次调用会替换成真 fieldset）
    const radioFieldsetEl = document.createElement("fieldset");
    radioFieldsetEl.className = "cc-target-pick";
    body.appendChild(radioFieldsetEl);

    // 加载倒计时：永远占位
    const countdownEl = document.createElement("div");
    countdownEl.className = "cc-load-countdown";
    countdownEl.style.display = "none";
    body.appendChild(countdownEl);

    // error pre：永远占位（state==="error" 时显示 svc.error）
    const errorEl = document.createElement("pre");
    errorEl.className = "cc-service-error-pre";
    errorEl.style.display = "none";
    body.appendChild(errorEl);

    card._h = {
      titleEl, autostartBadge, pidEl, restartBtn, toggle, toggleInput,
      body, capsEl, missingDirEl, radioFieldsetEl,
      countdownEl, errorEl,
      // radioSig：上一次 _makeTargetFieldset 的输入签名；
      // 变化时 replaceWith 替换 fieldset，未变则完全不动 —— 保证用户
      // 已选 radio / focus 不会丢。
      lastRadioSig: null,
    };

    // 一次性写入初始状态（state class / text / toggle / countdown …）。
    this._updateLocalCardState(card, svc, meta, cards);

    return card;
  },

  // 原地刷新 local card 的所有状态相关字段。调用时机：
  //   - 首次创建 _makeLocalCard 时
  //   - _renderServices 命中 sig 变化 / container 切换时
  // 不创建/销毁任何节点（除 radio fieldset 在 sig 变化时 replaceWith 替换）。
  _updateLocalCardState(card, svc, meta, cards) {
    const h = card._h;
    if (!h) return;
    const state = svc.status || "not_loaded";
    const isOn     = state === "running" || state === "loading";
    const isLoad   = state === "loading";
    const isErr    = state === "error";
    const isMissingDir = state === "missing_model_dir";
    const noCards  = !cards || cards.length === 0;
    const isOrphan = svc._source === "adopted_local";
    const isUnavail = svc._source === "unavailable";

    // ---- card 整体 class
    card.dataset.source = svc._source || "spawned";
    card.classList.toggle("is-orphan", isOrphan);
    card.classList.toggle("is-running", state === "running");
    card.classList.toggle("is-pending", isLoad);
    card.classList.toggle("has-error", isErr);

    // ---- title
    h.titleEl.textContent = meta.label;

    // ---- autostart 徽章：enabled → 显示"开机自启"
    if (svc._autostart === "enabled") {
      h.autostartBadge.textContent = "开机自启";
      h.autostartBadge.title = "compute.services[] 的 autostart=enabled —— boot 时自动 spawn + bind NPU；reload / 重启后会自动恢复";
      h.autostartBadge.style.display = "";
    } else {
      h.autostartBadge.style.display = "none";
      h.autostartBadge.textContent = "";
      h.autostartBadge.title = "";
    }

    // ---- PID
    if (svc.pid && svc._source !== "adopted" && svc._source !== "unavailable") {
      h.pidEl.textContent = `PID ${svc.pid}`;
      h.pidEl.title = isOrphan
        ? "uvicorn reload 留下的 worker，registry 没持有 Popen 句柄；要清理请 `kill " + svc.pid + "`"
        : "本机 spawn 的 worker，registry 持有 Popen 句柄；要清理优先用 UI 重启按钮";
      h.pidEl.style.display = "";
    } else {
      h.pidEl.style.display = "none";
    }

    // ---- 重启按钮：仅 spawned 且模型已加载（running / loading / error）
    // 时显示；loading 时禁用；卸载后（not_loaded / missing_model_dir）
    // 必须隐藏 —— 否则用户在禁用态残留看到按钮是 UX bug。adopted_local
    // / unavailable 本就无 Popen 句柄，always 隐藏。
    const isOff = state === "not_loaded" || state === "missing_model_dir";
    h.restartBtn.style.display = (isOrphan || isUnavail || isOff) ? "none" : "";
    h.restartBtn.disabled = isLoad;

    // ---- toggle 状态
    h.toggle.classList.toggle("is-on", isOn);
    h.toggle.classList.toggle("is-load", isLoad);
    h.toggle.classList.toggle("is-err", isErr);
    h.toggle.classList.toggle("is-disabled", noCards);

    const targetPicked = this._hasTargetPicked(card.dataset.model, svc);
    const needsTarget = !isOn && !isErr && !isMissingDir
                     && svc._source !== "adopted"
                     && svc._source !== "unavailable";
    const toggleBlocked = noCards
                       || isLoad
                       || isErr
                       || (needsTarget && !targetPicked);
    h.toggleInput.checked = isOn;
    h.toggleInput.disabled = toggleBlocked;
    h.toggleInput.setAttribute("aria-label",
      `${meta.label}：${isOn ? "已启用" : "未启用"}，点击切换`);

    if (noCards) {
      h.toggle.title = "无可用 NPU 卡";
    } else if (isLoad) {
      h.toggle.title = "加载中，已锁定";
    } else if (isMissingDir) {
      h.toggle.title = svc.model_dir
        ? `模型目录 ${svc.model_dir} 不存在；点 toggle 重试 load`
        : `模型目录不存在；点 toggle 重试 load`;
    } else if (isErr) {
      if (svc._source === "adopted_local") {
        h.toggle.title = svc.pid
          ? `本机孤儿 worker 异常 (PID ${svc.pid})；点 toggle 重发 RPC load，若仍失败请 kill ${svc.pid} 重启`
          : `本机孤儿 worker 异常；点 toggle 重发 RPC load`;
      } else {
        h.toggle.title = "启动失败，点击重试";
      }
    } else if (needsTarget && !targetPicked) {
      h.toggle.title = "请先选择运行 NPU";
    } else {
      h.toggle.title = isOn
        ? `运行中，点击禁用 ${meta.label}`
        : `点击启用 ${meta.label}`;
    }

    // ---- caps（SW capability chips）：内容可能变（少见），每次重渲染
    h.capsEl.innerHTML = "";
    if (meta.capability_types && meta.capability_types.length) {
      h.capsEl.style.display = "";
      for (const cap of meta.capability_types) {
        const c = document.createElement("span");
        c.className = "cc-service-cap";
        c.textContent = cap;
        h.capsEl.appendChild(c);
      }
    } else {
      h.capsEl.style.display = "none";
    }

    // ---- missing-dir 提示（state==="missing_model_dir" 时显示）
    if (isMissingDir) {
      h.missingDirEl.innerHTML = "";
      const label = document.createElement("span");
      label.className = "cc-missing-dir-label";
      label.textContent = "⚠ 模型目录不存在";
      h.missingDirEl.appendChild(label);
      const path = document.createElement("code");
      path.className = "cc-missing-dir-path";
      path.textContent = svc.model_dir || "(未知路径)";
      h.missingDirEl.appendChild(path);
      const note = document.createElement("span");
      note.className = "cc-missing-dir-note";
      note.textContent = "补齐目录后点 toggle 重试 load，无需重启 worker";
      h.missingDirEl.appendChild(note);
      h.missingDirEl.style.display = "";
    } else {
      h.missingDirEl.style.display = "none";
    }

    // ---- radio fieldset：cards / available_targets / npu_index 任一变化才换
    const radioSig = [
      (cards || []).map(c => `${c.npu_index}:${c.device_id || ""}`).join(","),
      (meta.available_targets || []).join(","),
      String(svc.npu_index),
    ].join("|");
    if (h.lastRadioSig !== radioSig) {
      const newFs = this._makeTargetFieldset(card.dataset.model, meta, svc, cards);
      h.radioFieldsetEl.replaceWith(newFs);
      h.radioFieldsetEl = newFs;
      h.lastRadioSig = radioSig;
    }

    // ---- 倒计时
    if (isLoad) {
      h.countdownEl.textContent = `初始化中… 约 ${this._countdownSec(svc, "loading")}s`;
      h.countdownEl.style.display = "";
    } else {
      h.countdownEl.style.display = "none";
    }

    // ---- error pre
    if (isErr && svc.error) {
      h.errorEl.textContent = svc.error;
      h.errorEl.style.display = "";
    } else {
      h.errorEl.style.display = "none";
    }
  },

  // 找当前 snapshot 里 modelId 对应的 svc（toggle handler 用，
  // 避免闭包 svc 过时）。
  _findSvcInSnapshot(modelId) {
    const svcs = (this._snapshot && this._snapshot.services) || [];
    return svcs.find(s => s.model_id === modelId) || null;
  },

  // adopted peer —— 只读
  _makePeerCard(modelId, meta, svc) {
    const card = document.createElement("article");
    card.className = "cc-service-card cc-service-card-peer";
    card.dataset.model = modelId;
    card.dataset.source = svc._source;  // "adopted"

    const state = svc.status || "not_loaded";
    if (state === "running") card.classList.add("is-running");
    if (state === "loading") card.classList.add("is-pending");
    if (state === "error")   card.classList.add("has-error");

    // ---- head: title + peer-id 行 + 状态 dot
    const head = document.createElement("header");
    head.className = "cc-service-head";
    const title = document.createElement("h3");
    title.className = "cc-service-title";
    // 远端 peer：<model> @ <device_name>（device_name 来自 worker 的
    // info_handler.device_name 字段；hostname 放 peer-id 行）
    title.textContent = svc._peer_name
      ? `${meta.label} @ ${svc._peer_name}`
      : meta.label;
    head.appendChild(title);

    const peerId = document.createElement("span");
    peerId.className = "cc-peer-id";
    const peerLabel = svc._peer_name || (svc._peer_guid || "").slice(0, 8) || "远端";
    peerId.textContent = `peer: ${peerLabel}`;
    if (svc.pid) {
      peerId.textContent += ` · PID ${svc.pid}`;
      peerId.title = `远端 worker 真实 PID ${svc.pid}`;
    }
    head.appendChild(peerId);

    // v0.8: 状态用一个小色点代替整块 badge —— title 旁的 toggle 旁边
    // 也是同样的小点。状态文案让用户从 model-desc 上方的 meta 行拿。
    const dot = document.createElement("span");
    dot.className = "cc-state-dot";
    dot.dataset.state = state;
    head.appendChild(dot);

    card.appendChild(head);

    const body = document.createElement("div");
    body.className = "cc-service-body";

    const ro = document.createElement("div");
    ro.className = "cc-readonly";
    ro.textContent = "只读 · 本机无权操作 peer service";
    body.appendChild(ro);

    if (state === "error" && svc.error) {
      const pre = document.createElement("pre");
      pre.className = "cc-service-error-pre";
      pre.textContent = svc.error;
      body.appendChild(pre);
    }

    card.appendChild(body);
    return card;
  },

  async _enableInstance(npu_index, model) {
    try {
      const r = await fetch(API_PREFIX + "/compute/instances", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({npu_index, model}),
      });
      if (!r.ok) {
        const text = await r.text();
        alert(`启用失败 (HTTP ${r.status}): ${text}`);
      }
    } catch (e) {
      alert("请求失败: " + e.message);
    }
    await this.load();
  },

  async _disableInstance(model) {
    try {
      const r = await fetch(API_PREFIX + "/compute/instances", {
        method: "DELETE",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({model}),
      });
      if (!r.ok) {
        const text = await r.text();
        alert(`卸载失败 (HTTP ${r.status}): ${text}`);
      }
    } catch (e) {
      alert("请求失败: " + e.message);
    }
    await this.load();
  },

  async _restartInstance(model) {
    try {
      const r = await fetch(API_PREFIX + "/compute/restart", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({model}),
      });
      if (!r.ok) {
        const text = await r.text();
        alert(`重启失败 (HTTP ${r.status}): ${text}`);
      }
    } catch (e) {
      alert("请求失败: " + e.message);
    }
    await this.load();
  },

  _makeServiceCard(modelId, meta, inst, cards) {
    const card = document.createElement("article");
    card.className = "cc-service-card";
    card.dataset.model = modelId;

    // 状态 → 卡片整体边框色 + badge 文字
    const state = inst ? inst.status : "disabled";
    if (state === "running")    card.classList.add("is-running");
    if (state === "loading" || state === "queued")
                                card.classList.add("is-pending");
    if (state === "error")      card.classList.add("has-error");

    // ----- head
    const head = document.createElement("header");
    head.className = "cc-service-head";
    const title = document.createElement("h3");
    title.className = "cc-service-title";
    title.textContent = meta.label;
    head.appendChild(title);
    for (const cap of meta.capability_types || []) {
      const c = document.createElement("span");
      c.className = "cc-service-cap";
      c.textContent = cap;
      head.appendChild(c);
    }
    const badge = document.createElement("span");
    badge.className = "cc-service-badge";
    badge.dataset.state = state;
    if (state === "running")        { badge.classList.add("is-running");    badge.textContent = "运行中"; }
    else if (state === "loading")   { badge.classList.add("is-loading");    badge.textContent = "加载中…"; }
    else if (state === "queued")    { badge.classList.add("is-queued");     badge.textContent = "排队中"; }
    else if (state === "ready_lazy"){ badge.classList.add("is-ready-lazy"); badge.textContent = "已启用（待加载）"; }
    else if (state === "error")     { badge.classList.add("is-error");      badge.textContent = "启动失败"; }
    else                            { badge.textContent = "未启用"; }
    head.appendChild(badge);
    card.appendChild(head);

    const body = document.createElement("div");
    body.className = "cc-service-body";

    // ----- 子模型清单（informational）
    if (meta.submodels && meta.submodels.length) {
      const subs = document.createElement("div");
      subs.className = "cc-submodels";
      for (const sm of meta.submodels) {
        const row = document.createElement("div");
        row.className = "cc-submodel-row";
        const role = document.createElement("span");
        role.className = "cc-submodel-role";
        role.textContent = sm.role;
        row.appendChild(role);
        const mem = document.createElement("span");
        mem.className = "cc-submodel-mem";
        mem.textContent = `~${sm.memory_estimate_mb} MB · 加载 ${sm.estimated_init_seconds}s`;
        row.appendChild(mem);
        const tg = document.createElement("span");
        tg.className = "cc-submodel-targets";
        tg.textContent = this._targetsLabel(sm.files);
        tg.title = (sm.files || []).join(", ");
        row.appendChild(tg);
        subs.appendChild(row);
      }
      body.appendChild(subs);
    }

    // ----- 目标 radio（3588 / 182x#0 / 182x#1 / ...）
    const fieldset = this._makeTargetFieldset(modelId, meta, inst, cards);
    body.appendChild(fieldset);

    // ----- 操作控件：二元开关（左 off / 右 on）+ 状态行
    const actions = document.createElement("div");
    actions.className = "cc-service-actions";
    const sw = document.createElement("button");
    sw.type = "button";
    sw.className = "cc-svc-switch";
    const selectedTarget = this._selectedTarget[modelId] != null
      ? this._selectedTarget[modelId]
      : (inst ? inst.npu_index : this._defaultTargetNpu(cards));
    sw.dataset.target = String(selectedTarget);

    const noCards = !cards || cards.length === 0;
    const isOn   = state === "running" || state === "loading" || state === "queued";
    const isLoad = state === "loading" || state === "queued";
    const isErr  = state === "error";
    sw.classList.toggle("is-on", isOn);
    sw.classList.toggle("is-loading", isLoad);
    sw.classList.toggle("is-error", isErr);
    // 加载期间锁定不可点（避免误取消）；无卡时也锁定。
    if (noCards || isLoad) {
      sw.classList.add("is-disabled");
      sw.disabled = true;
    }
    sw.title = noCards
      ? "无可用 NPU 卡"
      : this._switchTitle(meta, inst, isLoad);
    sw.setAttribute("aria-label",
      this._switchAriaLabel(meta, inst, noCards, isLoad));

    const knob = document.createElement("span");
    knob.className = "cc-svc-switch-knob";
    sw.appendChild(knob);

    sw.addEventListener("click", () => {
      if (sw.disabled) return;
      const target = parseInt(sw.dataset.target, 10);
      this.toggle(modelId, target);
    });
    actions.appendChild(sw);
    body.appendChild(actions);

    const statusLine = document.createElement("div");
    statusLine.className = "cc-service-status-line";
    statusLine.dataset.state = state;
    statusLine.textContent = this._statusLineText(modelId, meta, inst);
    body.appendChild(statusLine);

    // error 时挂错误信息块
    if (inst && inst.status === "error" && inst.error) {
      const pre = document.createElement("pre");
      pre.className = "cc-service-error-pre";
      pre.textContent = inst.error;
      body.appendChild(pre);
    }

    card.appendChild(body);
    return card;
  },

  // 把 submodel.files（["vision_Qwen3-VL-4B.1828.rknn", ...]）解析成可读标签
  _targetsLabel(files) {
    if (!files || !files.length) return "—";
    const suffixes = new Set();
    for (const f of files) {
      if (f.endsWith(".1828.rknn")) suffixes.add("182x");
      else if (f.endsWith(".3588.rknn")) suffixes.add("3588");
      else if (f.endsWith(".weight"))   suffixes.add("(共享权重)");
    }
    if (!suffixes.size) return files.join(", ");
    return Array.from(suffixes).join(" + ");
  },

  // v0.8 patch: 改回 radio + fieldset（v0.7 形态）。用户反馈
  _makeTargetFieldset(modelId, meta, inst, cards) {
    // v0.8 patch: 改回 radio + fieldset（v0.7 形态）。用户反馈
    // <select> 在 tick patch 路径下 dropdown 还是会被关闭（即使我们
    // 不再 detach 节点，浏览器对 <select> 的 open 状态机很敏感），
    // radio 没有 open 状态机，patch 算法能完全保留用户的勾选。
    // v0.8 的 patch（_renderedCard 复用节点）+ radio 组合：用户点了
    // 某个 radio 后，sig 在 _selectedTarget 变化时才会重建 card，
    // 重建前 radio 节点保持，焦点与勾选不变。
    const fs = document.createElement("fieldset");
    fs.className = "cc-target-pick";
    const legend = document.createElement("legend");
    legend.textContent = "运行在";
    fs.appendChild(legend);

    const opts = document.createElement("div");
    opts.className = "cc-target-options";

    // 历史 bug：之前假设 ``npu_index == 0`` 是 RK3588 主控 SoC，把
    // enumerate 第 1 张 182x 显示成 "RK182x #0"，导致顶部内存条
    // 跟运行在 radio 的 #K 跟系统其它地方（比如 rknn-smi / 系统
    // 信息页）对不上 —— 选 #0 实际跑在 #1 上。
    //
    // 真相：``RKNN3Lite().get_devices_id()`` 枚举的是 RK182x 协卡
    // （wdnai SDK 把 3588 走 ``device_id="rk3588"`` 路径，不在
    // npu 枚举里）。enumerate index 就是真实的 NPU 编号，
    // "RK182x #N" 的 N 直接等于 enumerate index，不再 offset。
    const MAX_RK182X_OPTIONS = CC_MAX_RK182X_CARDS;
    const sorted = this._sortNpuCards(cards);
    const sortedIndices = sorted.map(c => c.npu_index);

    // v0.8: 模型本身只支持某些 target（available_targets，如 ["182x"]）；
    // 不支持的 radio 灰掉 + 不可点。
    const supported = new Set(
      (meta.available_targets && meta.available_targets.length)
        ? meta.available_targets
        : (inst && inst.available_targets && inst.available_targets.length)
            ? inst.available_targets
            : ["182x"]
    );

    // 默认选中：用户已记录 → 那个；否则 instance.npu_index >= 0 → 那个；
    // 否则从 config.compute.services[].device 推出的 default_npu_index
    // （算力配置 autostart=enabled/disabled 都靠这个绑 NPU）；
    // 否则按 npu_index 升序排好后的第一个 supported 的 target。
    let defaultIdx = null;
    if (this._selectedTarget[modelId] != null
        && sortedIndices.includes(this._selectedTarget[modelId])
        && supported.has("182x")) {
      defaultIdx = this._selectedTarget[modelId];
    } else if (inst && inst.npu_index >= 0
               && sortedIndices.includes(inst.npu_index)) {
      defaultIdx = inst.npu_index;
    } else if (inst && Number.isInteger(inst._default_npu_index)
               && inst._default_npu_index >= 0
               && sortedIndices.includes(inst._default_npu_index)) {
      defaultIdx = inst._default_npu_index;
    } else {
      for (const idx of sortedIndices) {
        if (supported.has("182x")) { defaultIdx = idx; break; }
      }
    }
    if (defaultIdx != null) {
      this._selectedTarget[modelId] = defaultIdx;
    }

    let rendered = 0;
    const renderOpt = (npuIndex, title, sub, targetFamily, supportedFlag) => {
      // 不支持的 target 不渲染（直接跳过）—— 用户只看到能用的 NPU 卡。
      const isSup = supportedFlag !== false;
      if (!isSup) return;
      const label = document.createElement("label");
      label.className = "cc-target-opt";
      label.title = sub || "";
      const input = document.createElement("input");
      input.type = "radio";
      input.name = `cc-target-${modelId}`;
      input.value = String(npuIndex);
      input.className = "cc-target-radio";
      // 默认勾选：当前 defaultIdx
      if (npuIndex === defaultIdx) input.checked = true;
      input.addEventListener("change", () => {
        if (input.checked) this._selectedTarget[modelId] = npuIndex;
      });
      label.appendChild(input);
      const txt = document.createElement("span");
      txt.textContent = title;
      label.appendChild(txt);
      opts.appendChild(label);
      rendered += 1;
    };

    // enumerate index 本身就是 NPU 编号。3588 走 device_id 路径不在
    // npu 枚举里 —— enumerate 0 就是第一张 182x 协卡，label 写
    // "RK182x #0"，radio value 也是 0（直接给 worker 用）。
    // 排序按 npu_index 升序，与顶部 182x 内存条位置完全对齐。
    for (const card of sorted) {
      if (rendered >= MAX_RK182X_OPTIONS) break;
      const idx = card.npu_index;
      renderOpt(idx, `RK182x #${idx}`,
                card.device_id || `npu #${idx}`,
                "182x", supported.has("182x"));
    }
    if (rendered === 0) {
      const ph = document.createElement("span");
      ph.className = "cc-mem-empty";
      ph.style.fontSize = "12px";
      ph.textContent = "未检测到 NPU 卡 —— 等待 enumerate…";
      opts.appendChild(ph);
    }
    fs.appendChild(opts);
    return fs;
  },

  _defaultTargetNpu(cards) {
    if (!cards || !cards.length) return 0;     // 兜底：选 enumerate #0
    const sorted = this._sortNpuCards(cards);
    return sorted.length ? sorted[0].npu_index : 0;
  },

  // NPU 卡片按 enumerate index (npu_index) 升序，与 label "RK182x #N"
  // 完全对齐。早期版本按 device_id 字符串排序，会让 Position 1 显示
  // "RK182x #1" 而 Position 2 显示 "RK182x #0"（label 与位置错位），
  // 跟系统信息页 / radio 选 #N 实际跑在 #N 上对不上。
  //
  // 真相：wdnai 枚举的 npu_index 本身就是 canonical 编号（与系统
  // 信息页 / rknn-smi 对齐后的 index 同源），按 npu_index 排即按
  // 物理编号从左到右。每张物理卡始终落在同一格，位置 = label。
  // ties 时按 device_id 字符串作 tiebreaker 稳定顺序。
  // 入参可能是 [{npu_index, device_id, ...}, ...]；按 npu_index 去重。
  _sortNpuCards(cards) {
    const seen = new Map();
    for (const c of (cards || [])) {
      if (c && !seen.has(c.npu_index)) seen.set(c.npu_index, c);
    }
    return Array.from(seen.values()).sort((a, b) => {
      const idxA = (a.npu_index || 0) - (b.npu_index || 0);
      if (idxA !== 0) return idxA;
      const da = (a.device_id || "").toString();
      const db = (b.device_id || "").toString();
      return da.localeCompare(db);
    });
  },

  // v0.8: 校验"已选 NPU"——用户必须先在 select 里选一个目标，toggle
  // 才能启用。spawned/adopted_local 已 bind 的也认。
  _hasTargetPicked(modelId, svc) {
    if (svc && svc.npu_index >= 0) return true;
    const t = this._selectedTarget[modelId];
    return typeof t === "number" && t >= 0;
  },

  // v0.8: 加载中倒计时（按 estimated_init_seconds - (now - started_at)）
  _countdownSec(svc, kind) {
    const snap = this._snapshot || {};
    const perModel = snap.estimated_init_seconds_per_model || {};
    const est = perModel[svc.model_id] ?? snap.default_estimated_init_seconds ?? 30;
    if (kind === "loading") {
      const elapsed = Math.max(0, Date.now() / 1000 - (svc.started_at || 0));
      return Math.max(0, Math.round(est - elapsed));
    }
    return est;
  },

  _renderSummary() {
    const snap = this._snapshot || {};
    const cards = snap.cards || [];
    const running = snap.running_count || 0;
    const queued  = snap.queued_count  || 0;
    const loading = snap.loading_count || 0;
    const error   = snap.error_count   || 0;
    const rk182xCount = Math.min(
      CC_MAX_RK182X_CARDS,
      cards.length,
    );
    const parts = [
      `${rk182xCount} 张算力卡`,
      `${running} 运行中`,
      error   ? `${error} 失败`  : null,
      loading ? `${loading} 加载中` : null,
      queued  ? `${queued} 排队中`  : null,
    ].filter(Boolean);
    const at = document.getElementById("cc-collected-at");
    if (at) at.textContent = parts.join(" · ");
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
    const refreshBtn = document.getElementById("discovery-refresh");

    if (enableAll)  enableAll .addEventListener("click", () => this.setAllHw(true));
    if (disableAll) disableAll.addEventListener("click", () => this.setAllHw(false));
    if (saveBtn)    saveBtn   .addEventListener("click", () => this.saveHw());
    if (refreshBtn) refreshBtn.addEventListener("click", () => this.load());
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
        ? "有未保存的修改 —— 点“保存修改”写入 config.jsonc。"
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
  chatCleared: false,    // true after 清屏: don't re-fill from backend on page re-entry

  init() {
    if (this._inited) return;
    this._inited = true;
    const $ = (id) => document.getElementById(id);
    $("imb-login-btn")?.addEventListener("click", () => this.login());
    $("imb-cancel-btn")?.addEventListener("click", () => this.cancelLogin());
    $("imb-logout-btn")?.addEventListener("click", () => this.logout());
    $("imb-send-btn")?.addEventListener("click", () => this.send());
    $("imb-clear-btn")?.addEventListener("click", () => this.clearChat());
    $("imb-history-btn")?.addEventListener("click", () => this.showHistory());
    $("imb-llm-bridge")?.addEventListener("change", (e) => {
      this._toggleLlmBridge(e.target.checked);
    });
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
      this._renderLlmBridge();
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
    // Only (re)fill from the backend's recent_messages if the user hasn't
    // cleared the view. After 清屏, keep the cleared state across page
    // switches (until re-login); new incoming messages still append.
    if (!this.chatCleared) {
      this.messages = Array.isArray(snap.recent_messages) ? snap.recent_messages.slice() : [];
    }
    this._renderState();
    this._renderButtons();
    this._renderQr();
    this._renderKnown();
    this._renderChat();
    this._renderLlmBridge();
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
    const bridge = (s.llm_bridge && s.llm_bridge.enabled) || false;
    if (sendBtn) sendBtn.disabled = !loggedIn || bridge;
    if (textInput) textInput.disabled = !loggedIn;
  },

  _renderLlmBridge() {
    const s = this.snapshot || {};
    const b = (s.llm_bridge) || {};
    const cb = document.getElementById("imb-llm-bridge");
    const hint = document.getElementById("imb-llm-bridge-hint");
    if (!cb) return;

    const enabled = !!b.enabled;
    const available = b.available !== false;

    cb.checked = enabled;
    cb.disabled = !available;

    if (hint) {
      if (!available) {
        hint.textContent = "文本对话模型未启用，请在算力配置页启用 qwen3-4b / qwen3-5-vl-4b 任一文本对话实例";
        hint.className = "imb-bridge-hint danger";
      } else if (enabled) {
        hint.textContent = b.busy ? "已对接，生成中…" : "已对接，微信消息将自动回复";
        hint.className = "imb-bridge-hint";
      } else {
        hint.textContent = "";
        hint.className = "imb-bridge-hint";
      }
    }
  },

  async _toggleLlmBridge(checked) {
    const cb = document.getElementById("imb-llm-bridge");
    const hint = document.getElementById("imb-llm-bridge-hint");
    if (cb) cb.disabled = true;
    if (hint) hint.textContent = "处理中…";
    try {
      const r = await fetch(API_PREFIX + "/wechat/llm-bridge", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: checked }),
      });
      if (!r.ok) {
        let detail = "HTTP " + r.status;
        try { detail = (await r.json()).detail || detail; } catch (_) {}
        throw new Error(detail);
      }
      const data = await r.json();
      if (data.snapshot) this._applySnapshot(data.snapshot);
      else this._renderLlmBridge();
      this._setChatStatus(checked ? "已开启文字对话模型对接" : "已关闭文字对话模型对接");
    } catch (err) {
      if (cb) cb.checked = !checked;   // revert
      this._renderLlmBridge();
      this._setChatStatus("操作失败: " + err.message, true);
    }
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
    // ``this.messages`` is newest-first (``_recordMessage`` unshifts),
    // so reverse to render oldest-at-top / newest-at-bottom (chat flow).
    const rows = this.messages.slice().reverse().map((m) => {
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
    // Capture scroll position before rebuilding (innerHTML reset resets it);
    // pass it so the smart-follow can restore it when not near the bottom.
    const prevScrollTop = list.scrollTop;
    list.innerHTML = `<ul class="imb-conv-ul">${rows.join("")}</ul>`;
    this._scrollChatToBottomIfNear(list, prevScrollTop);
  },

  // Threshold (px) below the bottom: if the user is scrolled within this
  // distance of the bottom, keep auto-following new messages down. If they
  // scrolled up to read history, don't yank the view back down.
  _CHAT_BOTTOM_TOLERANCE: 40,

  _scrollChatToBottomIfNear(list, prevScrollTop) {
    if (!list) return;
    const nearBottom =
      list.scrollHeight - list.scrollTop - list.clientHeight <=
      this._CHAT_BOTTOM_TOLERANCE;
    if (nearBottom) {
      list.scrollTop = list.scrollHeight;
    } else if (typeof prevScrollTop === "number") {
      list.scrollTop = prevScrollTop;
    }
  },

  refreshChat() {
    this._renderChat();
  },

  clearChat() {
    this.messages = [];
    this.chatCleared = true;
    this._renderChat();
  },

  showHistory() {
    // Restore the full recent history. chatCleared=false so the backend
    // snapshot (freshly fetched) fills this.messages again.
    this.chatCleared = false;
    this._refreshHistory();
  },

  async _refreshHistory() {
    try {
      const r = await fetch(API_PREFIX + "/wechat");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const snap = await r.json();
      // chatCleared is now false, so _applySnapshot fills from backend.
      this._applySnapshot(snap);
    } catch (err) {
      this._setChatStatus("加载历史失败: " + err.message, true);
    }
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
      // Re-login: reset the cleared-view flag so the new session shows history.
      if (data.snapshot) {
        this.chatCleared = false;
        this._applySnapshot(data.snapshot);
      }
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
      // Logout resets the cleared-view flag; a fresh login will re-show history.
      this.chatCleared = false;
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
    // 开关开启时禁止手动"插话"（与后端一致；按钮已置灰，此处兜底键盘触发）
    if (this.snapshot && this.snapshot.llm_bridge && this.snapshot.llm_bridge.enabled) {
      this._setChatStatus("对接文字对话模型已开启，手动发送已禁用", true);
      return;
    }
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
  _sending: false,          // in-flight 防抖：fetch 完成前忽略重复 send()
  pending: new Map(),       // request_id -> {userLi, assistantLi, thinkingEl, answerEl, errorEl, cursorEl}
  messages: [],             // local history mirror: [{role:"user"|"assistant", content, ts}]

  init() {
    if (this._inited) return;
    this._inited = true;
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
        tool_call: msg.tool_call,
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
      const limit = s.context_limit || 16384;
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
    let p = this.pending.get(reqId);
    if (!p) {
      // No streaming delta ever arrived (e.g. worker produced no live
      // tokens); create the bubble now so the done/stopped frame can
      // still render the full reply instead of being dropped.
      this._ensureAssistantBubble(reqId);
      p = this.pending.get(reqId);
    }
    if (!p || !p.assistantLi) return;

    // remove cursor
    p.answerEl?.classList.remove("text-chat-cursor");

    // prefer the cleaner text from done frame (regex split) over the streamed buffer
    if (typeof info.thinking === "string") {
      p.thinkingBuf = info.thinking;
      if (p.thinkingEl) p.thinkingEl.textContent = info.thinking;
      const details = p.assistantLi.querySelector(".text-chat-thinking");
      if (details) {
        if (info.thinking) details.setAttribute("open", "");
        else details.hidden = true;
      }
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

    // tool-call badge: the agent executed at least one @Callable during
    // this turn (e.g. tts_alert / BeepTool). Show which one ran.
    if (info.tool_call) {
      const meta = p.assistantLi.querySelector(".imb-msg-meta");
      if (meta) {
        const span = document.createElement("span");
        span.className = "text-chat-tool-badge";
        span.textContent = `🔧 已调用 ${info.tool_call}`;
        span.title = "本轮对话通过工具调用完成";
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
    if (this._sending) return;   // 双击 / Cmd+Enter+按钮 同时触发的防抖
    if (this.snapshot && this.snapshot.busy) {
      this._setHint("上一条还在生成中…", true);
      return;
    }
    this._sending = true;
    try {
      const r = await fetch(API_PREFIX + "/text-chat/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      if (!r.ok) {
        let detail = "HTTP " + r.status;
        let body = null;
        try {
          body = await r.json();
          if (body && body.detail) {
            detail = typeof body.detail === "string"
              ? body.detail : JSON.stringify(body.detail);
          }
        } catch (_) { /* ignore */ }
        // 409 等场景：服务端附带最新 snapshot，用它纠正本地 busy/按钮态
        throw Object.assign(new Error(detail), { respBody: body });
      }
      if (input) input.value = "";
      this._setHint("已发送，等待模型响应…", false);
      // snapshot is updated by WS state frame
    } catch (err) {
      this._setHint("发送失败: " + err.message, true);
      if (err.respBody && err.respBody.snapshot) {
        this._applySnapshot(err.respBody.snapshot);
      }
    } finally {
      this._sending = false;
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
  // 播放器（WS 帧流）
  seg: null,        // { source, start, end, dur, title }
  playing: false,
  ended: false,     // 播放完毕停在末尾帧（可重播）
  playWs: null,
  playPaused: false,
  lastImg: null,    // 最近一帧 <img> 的 objectURL

  // ---- 勾选路径（缓存窗口）：selected key = `${md5}|${chunk}|${overlap}`
  cacheSel: new Set(),
  cacheEntries: [],

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
  // 提取完成（首次现有条目入库）后刷新缓存窗口 —— 让新条目立即可勾选；
  // 同时清空视频源路径文本框（仅成功清空，error/stopped 保留便于重试），
  // 引导后续检索走"已缓存视频"勾选路径。
  if (snap.state === "ready" && VCS._cacheWinOnReady !== snap.slices_embedded
      + "|" + snap.slices_total) {
    VCS._cacheWinOnReady = snap.slices_embedded + "|" + snap.slices_total;
    vcsCacheRefresh().catch(() => {});
    const srcEl = vcsEl("vcs-source");
    if (srcEl && srcEl.value) {
      srcEl.value = "";
      const metaEl = vcsEl("vcs-source-meta");
      if (metaEl) metaEl.textContent = "提取完成，路径已清空 —— 可在\"已缓存视频\"中勾选检索";
      vcsApplyMutualExclusion();
    }
  }
  if (typeof DemoGuard !== "undefined") {
    DemoGuard.report("video-content-search", { vcs: snap, playing: VCS.playing });
  }

  const st = snap.state || "idle";
  vcsSetStatus({ idle: "就绪", extracting: "● 提取中", ready: "● 提取完成", error: "提取失败" }[st] || st, st === "error");
  vcsSetRuntime({ idle: "未启动", extracting: "提取中", ready: "就绪，可检索", error: snap.error || "错误" }[st] || st);
  // error 状态同时弹顶部红色 banner（对齐 VFA 行为）
  if (st === "error") {
    vcsShowError(snap.error || "处理失败");
  }

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
  // 勾选路径互斥：勾选 ≥1 个缓存条目时"开始提取/停止"链路禁用
  //（缓存已就绪无需提取），检索按钮保持可用（走 query_multi）。
  const anyChecked = VCS.cacheSel && VCS.cacheSel.size > 0;
  if (startBtn) startBtn.disabled = st === "extracting" || !src || anyChecked;
  if (stopBtn) stopBtn.disabled = st !== "extracting";
  // 检索可用性：后端 state=ready（单例内存索引存活，与路径输入框
  // 无关 —— 输入框只是提取入口，提取完成后会被清空）或勾选 ≥1
  // 缓存条目（走 query_multi），二者满足其一即可检索。
  if (queryBtn) queryBtn.disabled = !(st === "ready" || anyChecked);
}

// ---- 勾选路径 / 缓存窗口（v0.12.2）----
function vcsCacheKey(e) {
  return `${e.md5}|${e.chunk_seconds || 0}|${e.chunk_overlap_seconds || 0}`;
}

async function vcsCacheRefresh() {
  const win = vcsEl("vcs-cache-window");
  if (!win) return;
  let enabled = false;
  let videos = [];
  let failed = false;
  try {
    const r = await fetch(API_PREFIX + "/video-content-search/cache/list");
    if (r.ok) {
      const d = await r.json();
      enabled = !!d.enabled;
      videos = d.videos || [];
    } else {
      failed = true;
    }
  } catch (_) {
    failed = true;
  }
  VCS.cacheEntries = videos;
  // 清除已失效勾选（条目被删 / 全清后）
  const alive = new Set(videos.map(vcsCacheKey));
  Array.from(VCS.cacheSel).forEach((k) => {
    if (!alive.has(k)) VCS.cacheSel.delete(k);
  });
  vcsCacheRender(videos, enabled);
  if (failed) {
    // 请求失败时窗口空态不能停留在"加载中…"（骨架文案）——
    // 提示可重试（⭘ = 窗口头部的刷新按钮）。
    const empty = vcsEl("vcs-cache-empty");
    if (empty) empty.textContent = "缓存列表加载失败，点右上 ⭘ 重试";
  }
  vcsApplyMutualExclusion();
}

function vcsCacheRender(videos, enabled) {
  const rows = vcsEl("vcs-cache-rows");
  const empty = vcsEl("vcs-cache-empty");
  const count = vcsEl("vcs-cache-count");
  const win = vcsEl("vcs-cache-window");
  if (!rows || !win) return;
  win.hidden = false;
  if (count) count.textContent = String(videos.length);
  if (empty) {
    empty.hidden = videos.length > 0;
    // 空态文案恢复（HTML 初始是骨架"加载中…"，fetch 返回后按实际
    // 状态回填；空表 → 引导文案，避免"加载中…"常驻。请求失败时
    // 由 vcsCacheRefresh 在本函数之后覆盖为"加载失败"提示）。
    if (!videos.length) {
      empty.textContent = "暂无缓存条目 —— 完成一次视频分析后自动出现";
    }
  }
  rows.innerHTML = videos.map((e) => {
    const key = vcsCacheKey(e);
    const checked = VCS.cacheSel.has(key) ? " checked" : "";
    const meta = `${e.chunk_seconds || "?"}s/${e.chunk_overlap_seconds || "?"}s`
               + ` · ${e.slices || 0}段`;
    const tip = `md5=${(e.md5 || "").slice(0, 12)}… ${e.source_path || "(微信入库，无本地路径)"}`;
    return (
      `<label class="vcs-cache-row" title="${vcsEsc(tip)}">` +
        `<input type="checkbox" class="vcs-cache-check" data-key="${vcsEsc(key)}"` +
          ` data-md5="${vcsEsc(e.md5)}" data-chunk="${e.chunk_seconds || 0}"` +
          ` data-overlap="${e.chunk_overlap_seconds || 0}"${checked} />` +
        `<span class="vcs-cache-name">${vcsEsc(e.filename || "(未命名)")}</span>` +
        `<span class="vcs-cache-meta">${vcsEsc(meta)}</span>` +
        `<button class="vcs-cache-del" type="button" data-key="${vcsEsc(key)}"` +
          ` data-md5="${vcsEsc(e.md5)}" data-chunk="${e.chunk_seconds || 0}"` +
          ` data-overlap="${e.chunk_overlap_seconds || 0}">删除</button>` +
      "</label>"
    );
  }).join("");
  rows.querySelectorAll(".vcs-cache-check").forEach((cb) => {
    cb.addEventListener("change", () => {
      if (cb.checked) VCS.cacheSel.add(cb.dataset.key);
      else VCS.cacheSel.delete(cb.dataset.key);
      vcsApplyMutualExclusion();
    });
  });
  rows.querySelectorAll(".vcs-cache-del").forEach((btn) => {
    btn.addEventListener("click", async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const md5 = btn.dataset.md5 || "";
      if (!confirm("从缓存数据库删除该条视频索引？(索引可重新分析重建)")) return;
      try {
        const r = await fetch(API_PREFIX + "/video-content-search/cache/entry", {
          method: "DELETE",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            md5,
            chunk_seconds: Number(btn.dataset.chunk) || 0,
            chunk_overlap_seconds: Number(btn.dataset.overlap) || 0,
          }),
        });
        if (r.ok) await vcsCacheRefresh();
      } catch (_) {}
    });
  });
}

function vcsApplyMutualExclusion() {
  // 双路径互斥：路径文本非空 → 勾选框全部置灰；
  // 勾选 ≥1 → 路径文本/浏览/开始提取 置灰。
  const sourceEl = vcsEl("vcs-source");
  const browseBtn = vcsEl("vcs-browse");
  const pathSet = (sourceEl?.value || "").trim() !== "";
  const anyChecked = VCS.cacheSel.size > 0;
  vcsEl("vcs-cache-window")?.querySelectorAll(".vcs-cache-check").forEach((cb) => {
    cb.disabled = pathSet;
  });
  if (sourceEl) sourceEl.disabled = anyChecked;
  if (browseBtn) browseBtn.disabled = anyChecked;
  vcsUpdateButtons();
}

async function vcsQueryMulti(text, simThreshold, topK, minResults) {
  // 勾选路径：按窗口选中顺序（窗口行序 = analyzed_at 倒序）检索。
  const entries = [];
  const win = vcsEl("vcs-cache-window");
  (win?.querySelectorAll(".vcs-cache-check:checked") || []).forEach((cb) => {
    entries.push({
      md5: cb.dataset.md5,
      chunk_seconds: Number(cb.dataset.chunk) || 0,
      chunk_overlap_seconds: Number(cb.dataset.overlap) || 0,
    });
  });
  if (!entries.length) {
    vcsShowError("请先勾选至少一个缓存条目");
    return;
  }
  // 防重复点击：与单视频路径共用 vcsQueryAbort 守卫（本路径不携带
  // abort signal，仅作互斥 + 40s 卡死兜底计时）。
  if (vcsQueryAbort) return;
  vcsQueryAbort = new AbortController();
  const timer = setTimeout(() => {
    try { vcsQueryAbort.abort(); } catch (_) {}
  }, 40000);
  const btn = vcsEl("vcs-query");
  const meta = vcsEl("vcs-query-meta");
  if (btn) { btn.disabled = true; btn.textContent = "检索中"; }
  if (meta) meta.textContent = "检索中…（" + entries.length + " 个视频）";
  const list = vcsEl("vcs-results-list");
  if (list) list.innerHTML = '<li class="vcs-results-empty">检索中…</li>';
  const count = vcsEl("vcs-results-count");
  if (count) count.textContent = "";
  try {
    const r = await fetch(API_PREFIX + "/video-content-search/query_multi", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text, sim_threshold: simThreshold, top_k: topK,
        min_results: minResults || 0, entries,
      }),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      vcsShowError(d.detail || `query_multi 失败 (HTTP ${r.status})`);
      return;
    }
    const d = await r.json();
    vcsRenderMultiGroups(d.groups || [], text, topK, simThreshold);
  } catch (e) {
    vcsShowError("query_multi 请求失败：" + e);
  } finally {
    // busy 态完整恢复 —— multi 渲染不走 vcsApplySnapshot，meta / btn 必须在此清掉
    clearTimeout(timer);
    vcsQueryAbort = null;
    if (btn) { btn.disabled = false; btn.textContent = "检索"; }
    if (meta && meta.textContent === "检索中…（" + entries.length + " 个视频）") {
      meta.textContent = "";
    }
  }
}

function vcsRenderMultiGroups(groups, text, topK, threshold) {
  const list = vcsEl("vcs-results-list");
  const count = vcsEl("vcs-results-count");
  if (!list) return;
  let html = "";
  let cardSeq = 0;
  const multi = { hits: [], sourceMap: [] };
  groups.forEach((g) => {
    if (g.miss) {
      html += `<li class="vcs-results-empty">${vcsEsc(g.md5.slice(0,12)
        )}：缓存索引不可读（可能已被删除或清空）</li>`;
      return;
    }
    const params = `${g.chunk_seconds || "?"}s/${g.chunk_overlap_seconds || "?"}s`;
    const frameTag = g.frame_enabled ? "可取帧回看" : "（无本地文件，仅时间段）";
    html += `<li class="vcs-group-head">${vcsEsc(g.filename || "(未命名)")}` +
      `<span class="vcs-g-meta">${vcsEsc(params)}` +
      ` · ${g.slices}段 · top ${g.top_score != null ? Number(g.top_score).toFixed(2) : "--"}` +
      `</span><span class="vcs-g-frame-tag">${!g.frame_enabled
        ? "仅文本" : "可取帧回看"}</span></li>`;
    const list0 = g.hits || [];
    if (!list0.length) {
      html += '<li class="vcs-results-empty">该视频无达到阈值或上限的命中。</li>';
      return;
    }
    html += list0.map((h) => {
      const t = vcsFormatTime(h.start_time) + " – " + vcsFormatTime(h.end_time);
      const sim = (h.similarity != null) ? Number(h.similarity).toFixed(2) : "--";
      const aboveTh = h.above_threshold !== false;
      const badgeCls = aboveTh ? "vcs-badge vcs-badge-ok" : "vcs-badge vcs-badge-low";
      const badgeTxt = aboveTh ? "达标" : "低于阈值";
      const idx = cardSeq++;
      multi.hits.push(h);
      multi.sourceMap.push(g.source_path || "");
      return (
        `<li class="vcs-card${g.frame_enabled ? "" : " vcs-card-frame-disabled"}` +
          (aboveTh ? "" : " vcs-card-low") +
          `" data-mi="${idx}" data-gi="${multi.hits.length - 1}"` +
          ` data-src="${vcsEsc(g.source_path || "")}" role="button" tabindex="0">` +
          `<div class="vcs-thumb" data-mt="${Number(h.start_time) || 0}">缩略图</div>` +
          `<div class="vcs-card-info">` +
            `<span class="vcs-card-time">${vcsEsc(t)}</span>` +
            `<span class="vcs-card-score">相似度 <b>${sim}</b></span>` +
          "</div>" +
          `<span class="${badgeCls}">${badgeTxt}</span>` +
          `<span class="vcs-card-play">▶</span>` +
        "</li>"
      );
    }).join("");
  });
  list.innerHTML = html;
  if (count) count.textContent =
    `${groups.filter((g) => !g.miss).length} 个视频 · ${multi.hits.length} 条`;
  // 事件绑定：点击 → 播放（source 用条目路径；无路径时仅提示）
  Array.from(list.querySelectorAll("li[data-mi]")).forEach((li) => {
    const h = multi.hits[Number(li.dataset.mi)];
    if (!h) return;
    const src = li.dataset.src || "";
    li.addEventListener("click", () => {
      if (!src) { vcsShowError("该视频无本地文件（微信入库），无法播放/取帧"); return; }
      vcsPlaySegment(Number(li.dataset.mi), src);
    });
    li.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        if (!src) { vcsShowError("该视频无本地文件，无法播放/取帧"); return; }
        vcsPlaySegment(Number(li.dataset.mi), src);
      }
    });
    const thumb = li.querySelector(".vcs-thumb");
    const tSec = thumb && Number(thumb.dataset.mt);
    if (thumb && src && tSec != null) {
      const url = API_PREFIX + "/video-content-search/frame?source=" +
        encodeURIComponent(src) + "&t=" + encodeURIComponent(tSec);
      const img = document.createElement("img");
      img.alt = "";
      img.onload = () => { thumb.textContent = ""; thumb.appendChild(img); };
      img.onerror = () => { thumb.textContent = "取帧失败"; };
      img.src = url;
    }
  });
}

/** 勾选检索入口：score+多组渲染复用单视频播放器 ——
 * ``vcsPlaySegment(idx, srcOverride)`` 的 srcOverride 位索选定 video
 * 的 source_path（多视频场景下不再读 VCS.snap.source）。 */


function vcsRenderHits(hits, query) {
  const list = vcsEl("vcs-results-list");
  const count = vcsEl("vcs-results-count");
  if (!list) return;
  if (!hits.length) {
    let msg;
    if (!query) {
      msg = "完成提取后输入问题检索，命中片段显示在这里。";
    } else {
      // 区分"完全没结果"vs"问题已查询过但没达阈值"。
      // query_meta.above_threshold_count 用于精确提示：若索引里有
      // 数据但全低于阈值，引导用户降低阈值 / 开宽松匹配。
      const meta = (VCS.snap && VCS.snap.query_meta) || {};
      const above = meta.above_threshold_count;
      const below = meta.below_threshold_count;
      if (typeof above === "number" && above === 0 && below > 0) {
        msg = "没有片段达到阈值；可降低阈值或勾选「宽松匹配」查看最佳猜测"
              + "（索引 " + meta.index_size + " 条，最高分 " +
              (meta.top_score != null ? Number(meta.top_score).toFixed(2) : "--") + "）。";
      } else {
        msg = "没有达到阈值或上限的结果。";
      }
    }
    list.innerHTML = '<li class="vcs-results-empty">' + vcsEsc(msg) + "</li>";
    if (count) count.textContent = "0 条";
    return;
  }
  if (count) count.textContent = hits.length + " 条";
  const srcBase = (VCS.snap && VCS.snap.source) || "";
  list.innerHTML = hits.map((h, i) => {
    const t = vcsFormatTime(h.start_time) + " – " + vcsFormatTime(h.end_time);
    const sim = (h.similarity != null) ? Number(h.similarity).toFixed(2) : "--";
    const t0 = (h.start_time != null) ? Number(h.start_time) : 0;
    // above_threshold=false 来自 fallback：阈值过滤完后无命中时
    // 补回的 top-N（"宽松匹配"模式），标红 badge 提示用户。
    const aboveTh = h.above_threshold !== false;
    const badgeCls = aboveTh ? "vcs-badge vcs-badge-ok" : "vcs-badge vcs-badge-low";
    const badgeTxt = aboveTh ? "达标" : "低于阈值";
    return (
      '<li class="vcs-card' + (aboveTh ? "" : " vcs-card-low") + '" data-i="' + i + '" role="button" tabindex="0">' +
        '<div class="vcs-thumb" data-thumb-t="' + t0 + '">缩略图</div>' +
        '<div class="vcs-card-info">' +
          '<span class="vcs-card-time">' + vcsEsc(t) + "</span>" +
          '<span class="vcs-card-score">相似度 <b>' + sim + "</b></span>" +
        '</div>' +
        '<span class="' + badgeCls + '">' + badgeTxt + "</span>" +
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
    // 懒加载缩略图：从源视频取 start_time 对应帧
    const thumb = li.querySelector(".vcs-thumb");
    const tSec = thumb && thumb.dataset.thumbT;
    if (thumb && srcBase && tSec != null) {
      const url = API_PREFIX + "/video-content-search/frame?source=" +
        encodeURIComponent(srcBase) + "&t=" + encodeURIComponent(tSec);
      const img = document.createElement("img");
      img.alt = "";
      img.onload = () => { thumb.textContent = ""; thumb.appendChild(img); };
      img.onerror = () => { thumb.textContent = "取帧失败"; };
      img.src = url;
    }
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

/* ------------------------- 播放器（WS 帧流） ----------------------- */
function vcsPlaySegment(idx, srcOverride) {
  const hits = (VCS.snap && VCS.snap.hits) || [];
  const h = (typeof idx === "number") ? hits[idx] : idx;
  if (!h) return;
  const source = srcOverride || (VCS.snap && VCS.snap.source) || "";
  if (!source) { vcsShowError("缺少视频源路径，无法播放"); return; }
  const start = Number(h.start_time) || 0;
  const end = Number(h.end_time) || start + 1;
  const dur = Math.max(0.5, end - start);
  VCS.seg = { source, start, end, dur, title: vcsFormatTime(start) + "–" + vcsFormatTime(end) };
  VCS.playing = true;
  VCS.playPaused = false;
  VCS.ended = false;

  const tag = vcsEl("vcs-player-tag");
  if (tag) tag.textContent = "正在播放 " + VCS.seg.title;
  const overlay = vcsEl("vcs-overlay");
  if (overlay) overlay.textContent = "";
  vcsEl("vcs-play").disabled = true;
  vcsEl("vcs-pause").disabled = false;
  vcsEl("vcs-stop-play").disabled = false;
  const bar = vcsEl("vcs-player-progress");
  if (bar) bar.style.width = "0%";
  const timeEl = vcsEl("vcs-player-time");
  if (timeEl) timeEl.textContent = vcsFormatTime(start) + " / " + vcsFormatTime(end);
  Array.from((vcsEl("vcs-results-list") || {}).children || []).forEach((li, i) => {
    li.classList.toggle("active", i === idx);
  });
  vcsSyncCanvasSize();
  vcsOpenPlayWs(source, start, end);
  if (typeof DemoGuard !== "undefined") {
    DemoGuard.report("video-content-search", { vcs: VCS.snap, playing: true });
  }
}

function vcsOpenPlayWs(source, start, end) {
  vcsClosePlayWs(false);
  const fps = (VCS.snap && Number(VCS.snap.fps)) || 25;
  if (VCS.seg) {
    VCS.seg.frameCount = 0;
    VCS.seg.totalFrames = Math.max(1, Math.round((end - start) * fps));
  }
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}${API_PREFIX}/video-content-search/play`);
  VCS.playWs = ws;
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    ws.send(JSON.stringify({ source, start, end }));
  };
  ws.onmessage = (ev) => {
    if (typeof ev.data === "string") {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (_) { return; }
      if (msg.type === "eof") {
        vcsFinishSegment();
      } else if (msg.type === "error") {
        vcsShowError(msg.detail || "播放错误");
        vcsFinishSegment();
      }
      return;
    }
    // binary JPEG 帧
    vcsDrawJpeg(new Uint8Array(ev.data));
    vcsTickProgress();
  };
  ws.onclose = () => {
    VCS.playWs = null;
  };
  ws.onerror = () => {
    try { ws.close(); } catch (_) {}
  };
}

function vcsClosePlayWs(settle) {
  if (VCS.playWs) {
    try { VCS.playWs.close(); } catch (_) {}
    VCS.playWs = null;
  }
  if (VCS.lastImg) {
    URL.revokeObjectURL(VCS.lastImg);
    VCS.lastImg = null;
  }
  if (settle) {
    VCS.playing = false;
    VCS.ended = true;
  }
}

// 把 vcs-canvas 像素尺寸同步为播放器区域（.vd-stage）显示尺寸，
// 使 canvas 填满窗口、contain 绘制能放大到最大（消除元素级黑边）。
function vcsSyncCanvasSize() {
  const canvas = vcsEl("vcs-canvas");
  if (!canvas || !canvas.parentElement) return;
  const w = Math.max(2, Math.floor(canvas.parentElement.clientWidth));
  const h = Math.max(2, Math.floor(canvas.parentElement.clientHeight));
  if (canvas.width !== w || canvas.height !== h) {
    canvas.width = w;
    canvas.height = h;
  }
}

// 用 <img> 承帧后画到 canvas（对齐音视频描述页的 WS 帧流渲染模式）
function vcsDrawJpeg(jpgBytes) {
  const canvas = vcsEl("vcs-canvas");
  if (!canvas) return;
  // 窗口 resize 后显示尺寸漂移则重新同步
  if (Math.abs(canvas.clientWidth - canvas.width) > 1 ||
      Math.abs(canvas.clientHeight - canvas.height) > 1) {
    vcsSyncCanvasSize();
  }
  const blob = new Blob([jpgBytes], { type: "image/jpeg" });
  if (VCS.lastImg) URL.revokeObjectURL(VCS.lastImg);
  VCS.lastImg = URL.createObjectURL(blob);
  const img = new Image();
  img.onload = () => {
    const ctx = canvas.getContext("2d");
    const vw = canvas.width, vh = canvas.height;
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, vw, vh);
    // 等比 contain：完整画面居中，剩余区域黑边
    const scale = Math.min(vw / img.naturalWidth, vh / img.naturalHeight);
    const dw = img.naturalWidth * scale;
    const dh = img.naturalHeight * scale;
    ctx.drawImage(img, (vw - dw) / 2, (vh - dh) / 2, dw, dh);
  };
  img.src = VCS.lastImg;
}

// 每收到一帧推进进度条 + 时间显示（按已收帧数 / 预计总帧数）
function vcsTickProgress() {
  if (!VCS.seg) return;
  VCS.seg.frameCount = (VCS.seg.frameCount || 0) + 1;
  const total = VCS.seg.totalFrames || 1;
  const p = Math.min(1, VCS.seg.frameCount / total);
  const bar = vcsEl("vcs-player-progress");
  if (bar) bar.style.width = (p * 100).toFixed(1) + "%";
  const timeEl = vcsEl("vcs-player-time");
  if (timeEl && VCS.seg.end) {
    const t = VCS.seg.start + p * (VCS.seg.end - VCS.seg.start);
    timeEl.textContent = vcsFormatTime(t) + " / " + vcsFormatTime(VCS.seg.end);
  }
}

// 播放完毕：停在末尾帧、进度条 100%，播放按钮可重播（重播需重开 WS）。
function vcsFinishSegment() {
  if (!VCS.seg) return;
  VCS.playing = false;
  VCS.ended = true;
  vcsClosePlayWs(false);
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
  VCS.playPaused = true;
  if (VCS.playWs && VCS.playWs.readyState === 1) {
    try { VCS.playWs.send(JSON.stringify({ action: "pause" })); } catch (_) {}
  }
  vcsEl("vcs-play").disabled = false;
  vcsEl("vcs-pause").disabled = true;
  if (typeof DemoGuard !== "undefined") {
    DemoGuard.report("video-content-search", { vcs: VCS.snap, playing: false });
  }
}

function vcsStopPlayback() {
  VCS.playing = false;
  VCS.ended = false;
  vcsClosePlayWs(false);
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

// 检索进行中可取消（40s 超时兜底，即使后端极端卡死也能反馈）
let vcsQueryAbort = null;

async function vcsQuery() {
  const text = (vcsEl("vcs-text")?.value || "").trim();
  if (!text) { vcsShowError("请输入检索问题"); return; }
  if (vcsQueryAbort) return;  // 已在检索中，防重复点击
  const threshold = Number(vcsEl("vcs-threshold")?.value || 0.5);
  const topK = Number(vcsEl("vcs-top-k")?.value || 10);
  const btn = vcsEl("vcs-query");
  const meta = vcsEl("vcs-query-meta");
  // 宽松匹配在两条路径里都要读（分叉后各自携带 min_results 语义）
  const lenient = !!vcsEl("vcs-lenient")?.checked;
  // ---- 双路径分叉（在设置全局 busy 态之前）：勾选 ≥1 缓存条目 →
  // 多视频检索（query_multi，自带完整的 busy 态生命周期）；否则走
  // 原单视频 query。两路排他（勾选优先）且数据面隔离 —— multi 只读
  // LanceDB、不触碰单例的内存索引；ready 状态下取消勾选再检索，
  // 单例索引原样可用。
  if (VCS.cacheSel && VCS.cacheSel.size > 0) {
    await vcsQueryMulti(text, threshold, topK, lenient ? topK : 0);
    return;
  }
  vcsQueryAbort = new AbortController();
  const timer = setTimeout(() => vcsQueryAbort.abort(), 40000);
  if (btn) { btn.disabled = true; btn.textContent = "检索中"; }
  if (meta) meta.textContent = "检索中…";
  try {
    const body = { text, sim_threshold: threshold, top_k: topK };
    // 严格模式（默认）：阈值过滤后无命中就返回空。
    // 勾选"宽松匹配" → min_results=topK，启用 fallback：
    //   阈值过滤完后无命中时补回 top-N（无视阈值，标"低于阈值"）。
    if (lenient) body.min_results = topK;
    const r = await fetch(API_PREFIX + "/video-content-search/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: vcsQueryAbort.signal,
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "HTTP " + r.status);
    vcsApplySnapshot(data);
  } catch (err) {
    if (meta) meta.textContent = "";
    if (err && err.name === "AbortError") {
      vcsShowError("检索超时：40s 内未完成，请重试");
    } else {
      vcsShowError(err.message);
    }
  } finally {
    clearTimeout(timer);
    vcsQueryAbort = null;
    if (btn) { btn.disabled = false; btn.textContent = "检索"; }
    vcsUpdateButtons();
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
  // 勾选路径（缓存窗口）：刷新按钮 + 进入页面即拉一次列表 +
  // 路径输入变化触发互斥重算（vcsApplyMutualExclusion 内部含
  // checkbox disabled 反向）。
  vcsEl("vcs-cache-refresh")?.addEventListener("click", vcsCacheRefresh);
  vcsEl("vcs-source")?.addEventListener("input", vcsApplyMutualExclusion);
  vcsCacheRefresh().catch(() => {});
  const clearCacheBtn = vcsEl("vcs-clear-cache");
  clearCacheBtn?.addEventListener("click", async () => {
    let stats = null;
    try {
      const r = await fetch(API_PREFIX + "/video-content-search/cache/stats");
      if (r.ok) stats = await r.json();
    } catch (_) {}
    const n = stats?.videos || 0;
    if (!confirm(n > 0
        ? `确认清空全部已缓存的视频分析结果？(当前 ${n} 条，清空后重发/重选视频需重新分析)`
        : "确认清空全部已缓存的视频分析结果？")) return;
    try {
      const r = await fetch(API_PREFIX + "/video-content-search/cache/clear",
                            { method: "POST" });
      const d = await r.json();
      const k = d.videos_cleared || 0;
      alert(k > 0
            ? `已清空 ${k} 条缓存。`
            : "缓存已空（或缓存未启用），无需清理。");
      vcsCacheRefresh().catch(() => {});
    } catch (e) {
      alert("清空缓存失败：" + e);
    }
  });
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

  const topKEl = vcsEl("vcs-top-k");
  const lenKEl = vcsEl("vcs-lenient-k");
  const syncLenientK = () => {
    const k = Number(topKEl?.value || 10);
    if (lenKEl) lenKEl.textContent = String(k);
  };
  topKEl?.addEventListener("input", syncLenientK);
  syncLenientK();

  // 播放器按钮
  vcsEl("vcs-play").addEventListener("click", () => {
    if (!VCS.seg || VCS.playing) return;
    // 暂停中恢复：发 WS resume
    if (VCS.playPaused && VCS.playWs && VCS.playWs.readyState === 1) {
      VCS.playPaused = false;
      VCS.playing = true;
      try { VCS.playWs.send(JSON.stringify({ action: "resume" })); } catch (_) {}
      vcsEl("vcs-play").disabled = true;
      vcsEl("vcs-pause").disabled = false;
      if (typeof DemoGuard !== "undefined") {
        DemoGuard.report("video-content-search", { vcs: VCS.snap, playing: true });
      }
      return;
    }
    // 播完重播：从头重开 WS
    if (VCS.seg && VCS.seg.source) {
      VCS.ended = false;
      const bar = vcsEl("vcs-player-progress");
      if (bar) bar.style.width = "0%";
      const timeEl = vcsEl("vcs-player-time");
      if (timeEl) timeEl.textContent = vcsFormatTime(VCS.seg.start) + " / " + vcsFormatTime(VCS.seg.end);
      vcsEl("vcs-play").disabled = true;
      vcsEl("vcs-pause").disabled = false;
      vcsOpenPlayWs(VCS.seg.source, VCS.seg.start, VCS.seg.end);
      VCS.playing = true;
      if (typeof DemoGuard !== "undefined") {
        DemoGuard.report("video-content-search", { vcs: VCS.snap, playing: true });
      }
    }
  });
  vcsEl("vcs-pause").addEventListener("click", vcsPausePlayback);
  vcsEl("vcs-stop-play").addEventListener("click", vcsStopPlayback);

  vcsRefresh();
  if (!VCS.ws && !VCS.wsTimer) connectVcsWs();
}

/* ------------------------------------------------------------------ *
 *  video-file-analysis page (后端 VLM 对接)                           *
 *                                                                    *
 *  后端: app/services/video_file_analysis.py —— 独立 wdnav.VideoCapture*
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
  // 优先用 snap.analyzed（state API 的权威计数）。没有 snap 时退到 events.length。
  // 关键：events API 与 state API 是两次独立 HTTP 请求，在 done 状态切换瞬间
  // events 端可能延迟 1-2 帧，导致 ratio 在 70%→86%→100% 跳变（用户多次刷新
  // 看到进度条一会儿不满格一会儿满格）。用 snap.analyzed 与 state 同源 → 一次
  // state 推送期间 analyzed 值稳定，进度条就不会 flicker。
  const analyzed    = (snap && Number.isFinite(snap.analyzed))
    ? Number(snap.analyzed)
    : VFA.events.length;

  let expected = 0;
  let ratio = null;
  // 进度条只在"真正在干活或刚干完"的状态下显示百分比：
  //   running / opening  → 实跑帧数（analyzed / expected）
  //   summarizing       → chunk 完成度
  //   done              → 100% 满格（强制 ratio=1，与 events.length 解耦）
  // idle / stopped / error 一律不显示百分比（哪怕 VFA.meta 残留上次运行的
  // 值 —— 此时 ratio=null → indeterminate 滑动 / 0%，避免用户以为"还在跑"）。
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
    // **done 状态下强制 ratio=1**，且 **expected = analyzed** 让百分比显示
    // "100%" 而不是 "75%" 这种看上去很怪的"已完成但只填到 75%"。原因：
    // 1) events API 可能比 state 慢 1-2 帧（events 端没追上）
    // 2) analyzed 本身可能因 dirty / timeout 大于实际成功 event 数
    // 3) floor(totalFrames/everyN) **是上限估计不是真实需求**——
    //    视频最后一帧可能不整除、也可能有 B 帧 / 关键帧让后端
    //    实际只采了 7 帧（10 帧的 70%），这 7 帧是"我们能采的
    //    全部"不是"7/10 必须采满"，已完成就显示 100%。
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

// 精准匹配 / 模糊搜索 + 字段过滤都是纯 Python，毫秒级返回。
async function vfaQueryRun() {
  const runId = VFAQuery.runId;
  if (!runId) return;
  const qs = vfaQueryBuildQuery();
  const summaryEl = document.getElementById("vfa-query-summary");
  const listEl = document.getElementById("vfa-query-list");
  // 收集当前所有筛选条件作为高亮 terms
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
  runBtn?.addEventListener("click", vfaQueryRun);
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
