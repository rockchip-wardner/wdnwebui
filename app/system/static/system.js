/* system.js — non-plugin system pages (system-monitor / system-logs /
 *              base-config / compute-config / discovery / power / coprocessor).
 *
 * Extracted from the legacy monolithic app.js during the v0.4 plugin refactor.
 * Each section preserves its original semantics; only the bootstrap (refresh-btn
 * click + hashchange routing) is rewritten so it integrates with framework.js.
 *
 * Loaded eagerly via <script src="/static/system/system.js" defer> in
 * index.html.template. No DemoGuard interaction — system pages don't have
 * background tasks.
 */

(function () {
  "use strict";

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

      // compute.autostart（开机自启模型）不在这里保存：它决定启动哪些
      // worker、绑哪张卡，属于部署配置，由 config.jsonc 直接编辑。

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

  if (name === "video-target-localization") {
    initVideoTargetLocalization();
    await vtlStatus();
    await refreshVtlDetector();
    return;
  }

  if (name === "video-retrieval") {
    if (window.VR && typeof window.VR.refresh === "function") {
      window.VR.refresh();
    }
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
 *  DemoGuard: 演示页 (音视频描述 / 视频目标监控 / 视频内容检索 /     *
 *  语音生成) 离开时的"必须关闭后台"守卫。                              *
 * ------------------------------------------------------------------ *
 *  三处入口：                                                          *
 *    1. 点击 sidebar 切换页面：confirm + await 后端 stop              *
 *    2. beforeunload（关 tab / 刷新 / 关闭浏览器）：原生对话框 +      *
 *       sendBeacon/keepalive 兜底发停                                  *
 *    3. popstate（前进/后退）：静默 fire-and-forget 停后端            *
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

// --------------------------------------------------------------------------- //
// 本地算力卡片的元数据与排序 **全部来自后端**：
//   * 每个 service 条目自带 label / capability_types / instance_name /
//     memory_estimate_mb / description / available_targets / display_order
//     （由 workers/<id>.json 扫描而来，见 registry.to_dict / _unavailable_entry）；
//   * estimated_init_seconds 走 snapshot.estimated_init_seconds_per_model；
//   * 列表顺序走 snapshot.preset_order（manifest 的 display_order 升序）。
// v0.9 之前这里有两份硬编码表（CC_PRESET_META + CC_PRESET_ORDER，且
// system.js 与 tts/app.js 各抄一份）。它们早已和 manifest 漂移 ——
// qwen3.5-vl-4b 的 description 两边文案都不一样，且新增模型时漏改
// 就静默不显示。现在没有任何一份副本。
// --------------------------------------------------------------------------- //

// 系统拓扑：1× RK3588 主控（不占"算力卡"位置） + N× RK182x 协卡。
// wdnai 当前会把单个 RK1828 物理卡拆成多个 context 索引（同卡多
// npu_index）返回；按物理协卡数 cap 住，避免 UI 出现重复 option。
const CC_MAX_RK182X_CARDS = 2;

// v0.15: banner / 顶部汇总只对"本机算力"响。adopted 远端 peer 状态本机
// 无权干预,告警 = 误导。_source 缺失按 spawned 兜底(与 _renderBanners
// 错误桶 fallback 一致);unavailable 进本机桶以对齐"本机声明"语义,
// 但 status="unavailable" 实际不触发任何 banner。
function isLocalComputeSource(s) {
  return s === "spawned" || s === "adopted_local" || s === "unavailable" || !s;
}

// 判断响应是不是「远端 peer，本机无权操作」这一**预期内**的拒绝。
// 后端两处会这么回：403（compute.py::_require_owned 的安全护栏）与
// 400（registry.stop_instance_local 的 RuntimeError）。两个码都要留
// —— 真正的手动禁用失败（进程 kill 失败等）也走 400，所以必须再匹配
// 文案，不能只看 status。命中时调用方静默：远端算力本就不可操作，
// 弹窗只会让用户以为点错了。
const PEER_REJECT_HINT = /远端\s*peer|本机无权(停止|bind|操作)|来自\s*\S+/;
function isRemotePeerRejection(status, text) {
  if (status !== 400 && status !== 403) return false;
  return PEER_REJECT_HINT.test(String(text || ""));
}


const ComputeConfigPage = {
  initialized: false,
  _tickHandle: null,
  _snapshot: null,
  _system: null,        // /api/system-monitor 缓存
  _loading: false,
  _selectedTarget: {},  // {model_id: [npu_index, ...]} —— 用户勾选的卡（可多张）

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
      // 再高频轮询 —— 每次都会过 librknnsmi.so + libfastdds，3s 一
      // 次即可（v0.5 之前 fork rknn-smi 时它更扛不住，被砍到 3s）。
      const firstLoad = this._snapshot == null;
      const rComp = await fetchCompute();
      if (!rComp.ok) throw new Error("compute HTTP " + rComp.status);
      this._snapshot = await rComp.json();
      this._systemTickCount = (this._systemTickCount || 0) + 1;
      if (firstLoad || this._systemTickCount % 3 === 0) {
        // 每 3 个 compute tick（约 3s）刷新一次 system-monitor，
        // 顶部 3588 内存 + 每张 182x 卡显存 / 温度 / 功耗才能动。
        // librknnsmi.so ctypes 调用虽然比 fork rknn-smi 子进程轻，
        // 但 PCIe / sysfs 读仍然随每次拉取递增。3s 节拍已经够用。
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
    //
    // **只列模型卡位**（``_modelCards``）：多画一格跑不了模型的卡，会让
    // 用户以为还有第二张卡可用（选它就会失败）。
    let rk182xCards = this._sortNpuCards(this._modelCards(cards))
      .slice(0, MAX_RK182X_CARDS);

    // wdnai 完全枚举不到 NPU 时，librknnsmi 兜底一次（host 没有 wdnai
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
      // 算力卡「运行在」的 #N 与系统信息页 NPU #N 完全对齐。
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
    // v0.15: 三个 banner 入口都按 isLocalComputeSource 过滤,本机无权
    // 干预的 adopted 远端 peer 不产生告警(详情见 _detectStuck / 错误桶)。
    const errored = (snap.instances || []).filter(
      i => i.status === "error" && isLocalComputeSource(i._source));
    const missingDirs = (snap.instances || []).filter(
      i => i.status === "missing_model_dir" && isLocalComputeSource(i._source));
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
      // v0.15: 删除 buckets.adopted 整桶 —— 远端 peer 本机无权操作,
      // "对端失联"banner 违反"banner 只对本机算力响"原则(per-card 卡片
      // 仍会显示 adopted state dot / error 文案,信息保留)。
      renderBucket("is-error",
        (l) => `${l.length} 个算力实例启动失败（用户主动启用）`,
        buckets.spawned);
      renderBucket("is-warn",
        (l) => `${l.length} 个本机孤儿 worker 异常（reload 残留，需 kill PID 清理）`,
        buckets.adopted_local);
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
      // v0.15: 过滤 adopted —— 它的 started_at 是 adopt 时刻(进程内全程
      // 不变),now - started_at 必然累积上千秒,触发假阳性卡住告警。
      if (!isLocalComputeSource(inst._source)) continue;
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

    // 按 snapshot.preset_order 排序（= manifest 的 display_order 升序），
    // 保证 UI 默认视觉顺序稳定。同 model 可以出现多条（例如 adopted 一条
    // + unavailable 一条），dedup 用 (model_id, _source) 复合 key 让两张
    // 卡都进 ordered。preset_order 里没有的 model 排到末尾（display_order
    // 缺失兜底 9999），不会因为 manifest 刚加就静默消失。
    const orderOf = (mid) => {
      const i = (snap.preset_order || []).indexOf(mid);
      return i === -1 ? 9999 : i;
    };
    const ordered = [];
    const seen = new Set();
    const seenKey = (s) => `${s.model_id}__${s._source || ""}`;
    // 卡号**不进** key：同一个模型同时跑在卡 0 / 卡 1 时，两条 service
    // 必须折叠成同一张模型卡片（用户视角是一个模型、一组「运行在」
    // 勾选），而不是并排两张卡。同理，勾选状态以整个模型为单位。
    for (const id of (snap.preset_order || [])) {
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
    // preset_order 排序保证视觉一致。unavailable 是本机 preset
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
    // 组内按 preset_order（manifest display_order）排序
    for (const g of peerFrameOrder) {
      g.services.sort((a, b) => {
        const aPos = orderOf(a.model_id);
        const bPos = orderOf(b.model_id);
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
      // meta 全部来自后端：service 条目自带 label / capability_types /
      // memory_estimate_mb / description / available_targets（registry 从
      // workers/<id>.json 透出），estimated_init_seconds 走 snapshot 的
      // estimated_init_seconds_per_model。没有前端副本可 fallback。
      const meta = {
        label:                s.label || s.model_id,
        capability_types:     s.capability_types || [],
        memory_estimate_mb:   s.memory_estimate_mb || 0,
        estimated_init_seconds:
          (snap.estimated_init_seconds_per_model || {})[s.model_id]
          || snap.default_estimated_init_seconds
          || 30,
        // 恒为空：manifest 不声明子模型，历史上也没有任何数据源填过它。
        // 渲染端 (_makeLocalCard) 仍读这个字段，故保留。
        submodels:            [],
        description:          s.description || "",
        available_targets:    s.available_targets || ["182x"],
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
        // v0.8 patch: 把用户的 NPU 选取也放进 sig —— 不然 checkbox change
        // 后 _selectedTarget 更新了，但 sig 没变 → 节点复用 → toggle.disabled
        // 还是旧值（needsTarget && !targetPicked）。sig 包含 _selectedTarget
        // 后切勾选会触发重建，toggle 立即变可用。
        this._selectedTargets(s.model_id).join(",") || "_",
        // 同模型可能同时占多张卡，一并进 sig（禁用全部 / 启一张都会变）。
        this._modelNpus(s.model_id).join(","),
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
        // （preset_order 末位）在循环中途会被推到位置 0，浏览器若
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
      // 「运行在」是多选：用户可能勾了两张卡，启用时两张都起 worker。
      const targets = this._selectedTargets(modelId);
      const finalTargets = (targets && targets.length)
        ? targets
        : [ (curSvc && curSvc.npu_index >= 0)
              ? curSvc.npu_index
              : this._defaultTargetNpu(curCards) ];
      if (toggleInput.checked) {
        this._enableInstance(finalTargets, modelId);
      } else {
        if (confirm(`确认禁用 ${meta.label}？将卸载模型并关闭 worker 进程。`)) {
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

    // SW capability chips（swcap_vision_chat / swcap_text_to_speech / …）
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
    // 远端 peer（其他板子的 worker）：本机无权启停，toggle 直接禁用，
    // 用户不必点得到、也不会再撞弹窗。
    const isPeer   = svc._source === "adopted";

    // ---- card 整体 class
    card.dataset.source = svc._source || "spawned";
    card.classList.toggle("is-orphan", isOrphan);
    card.classList.toggle("is-running", state === "running");
    card.classList.toggle("is-pending", isLoad);
    card.classList.toggle("has-error", isErr);

    // ---- title
    h.titleEl.textContent = meta.label;

    // ---- autostart 徽章：在 compute.autostart[] 里声明 → 显示"开机自启"
    if (svc._autostart === "enabled") {
      h.autostartBadge.textContent = "开机自启";
      h.autostartBadge.title = "已列入 config.jsonc 的 compute.autostart —— boot 时自动 spawn worker 并绑定 NPU 卡；reload / 重启后会自动恢复";
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
    h.toggle.classList.toggle("is-disabled", noCards || isPeer);

    const targetPicked = this._hasTargetPicked(card.dataset.model, svc);
    const needsTarget = !isOn && !isErr && !isMissingDir
                     && !isPeer && !isUnavail;
    const toggleBlocked = noCards
                       || isLoad
                       || isErr
                       || isPeer
                       || (needsTarget && !targetPicked);
    h.toggleInput.checked = isOn;
    h.toggleInput.disabled = toggleBlocked;
    if (isPeer) {
        const peerLabel = svc._peer_name || "其他板子";
        h.toggleInput.setAttribute("aria-label",
          `${meta.label}：由远端 ${peerLabel} 提供，本机无权启停`);
    } else {
      h.toggleInput.setAttribute("aria-label",
        `${meta.label}：${isOn ? "已启用" : "未启用"}，点击切换`);
    }

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

    // ---- 运行在 fieldset：cards / available_targets / 本模型占用的卡 任一
    // 变化才换节点。勾选集合也进 sig —— 用户点 checkbox 后要立刻反映
    // 到 toggle 的可用状态上。
    const radioSig = [
      (cards || []).map(c => `${c.npu_index}:${c.device_id || ""}`).join(","),
      (meta.available_targets || []).join(","),
      this._modelNpus(card.dataset.model).join(","),
      this._selectedTargets(card.dataset.model).join(","),
    ].join("|");
    if (h.lastRadioSig !== radioSig) {
      const newFs = this._makeTargetFieldset(
        card.dataset.model, meta,
        this._findSvcInSnapshot(card.dataset.model), cards);
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

  // 该模型当前**所有**实例占用的卡（同一个模型可能同时跑在多张卡上）。
  _modelNpus(modelId) {
    const svcs = (this._snapshot && this._snapshot.services) || [];
    return Array.from(new Set(
      svcs.filter(s => s.model_id === modelId && s.npu_index >= 0)
           .map(s => s.npu_index))).sort((a, b) => a - b);
  },

  // 「运行在」的勾选集合。v0.19 起是**数组**（同模型可多卡），早期版本
  // 是单个数字 —— 这里做一次兼容读取，免得热更新后老状态把 toggle 卡死。
  _selectedTargets(modelId) {
    const raw = this._selectedTarget[modelId];
    if (Array.isArray(raw)) return raw.filter(n => Number.isInteger(n) && n >= 0);
    if (typeof raw === "number" && raw >= 0) return [raw];
    return [];
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

    // v0.15: adopted peer 卡 loading 时给中性文案(route A) —— 本机无权
    // 操作,精确秒数无意义且 adoption 后 locked_at - start_at 必是错的,
    // 只解释 state-dot 脉冲即可。其他状态不动。
    if (state === "loading") {
      const note = document.createElement("div");
      note.className = "cc-peer-loading-note";
      note.textContent = "远端正在加载…";
      body.appendChild(note);
    }

    card.appendChild(body);
    return card;
  },

  // 启用/禁用/重启三条路径的公共骨架：发请求 → 失败则按语义决定是否
  // 弹窗 → 刷新。抽出来是因为三者除 method/path/body/文案外完全同构。
  async _computeAction(path, method, body, actionLabel) {
    try {
      const r = await fetch(API_PREFIX + path, {
        method,
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const text = await r.text();
        if (isRemotePeerRejection(r.status, text)) return;
        alert(`${actionLabel}失败 (HTTP ${r.status}): ${text}`);
      }
    } catch (e) {
      alert("请求失败: " + e.message);
    }
    await this.load();
  },

  // npu_index 可以是单卡或卡数组 —— 「运行在」是多选，一个模型可以
  // 同时跑在多张卡上（后端为每张卡各 spawn 一个 worker）。
  _enableInstance(npu_index, model) {
    const cards = Array.isArray(npu_index) ? npu_index : [npu_index];
    return this._computeAction("/compute/instances", "POST",
                               {npu_index: cards, model}, "启用");
  },

  _disableInstance(model) {
    return this._computeAction("/compute/instances", "DELETE", {model}, "卸载");
  },

  _restartInstance(model) {
    return this._computeAction("/compute/restart", "POST", {model}, "重启");
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

    // ----- 目标 checkbox（3588 / 182x#0 / 182x#1 / ...，可多选）
    const fieldset = this._makeTargetFieldset(modelId, meta, inst, cards);
    body.appendChild(fieldset);

    // ----- 操作控件：二元开关（左 off / 右 on）+ 状态行
    const actions = document.createElement("div");
    actions.className = "cc-service-actions";
    const sw = document.createElement("button");
    sw.type = "button";
    sw.className = "cc-svc-switch";
    // 已废弃的 switch 节点（v0.7 起被启用/禁用/重启三个显式按钮取代），
    // 这里保留 dataset.target 只是为了不破坏任何残留样式选择器。
    const selectedTarget = this._selectedTargets(modelId)[0]
      ?? (inst ? inst.npu_index : this._defaultTargetNpu(cards));
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
    // 「运行在」的 #K 与系统其它地方（比如 librknnsmi / 系统
    // 信息页）对不上 —— 选 #0 实际跑在 #1 上。
    //
    // 真相：``RKNN3Lite().get_devices_id()`` 枚举的是 RK182x 协卡
    // （wdnai SDK 把 3588 走 ``device_id="rk3588"`` 路径，不在
    // npu 枚举里）。enumerate index 就是真实的 NPU 编号，
    // "RK182x #N" 的 N 直接等于 enumerate index，不再 offset。
    const MAX_RK182X_OPTIONS = CC_MAX_RK182X_CARDS;
    const sorted = this._sortNpuCards(this._modelCards(cards));
    const sortedIndices = sorted.map(c => c.npu_index);

    // v0.8: 模型本身只支持某些 target（available_targets，如 ["182x"]）；
    // 不支持的卡灰掉 + 不可点。
    const supported = new Set(
      (meta.available_targets && meta.available_targets.length)
        ? meta.available_targets
        : (inst && inst.available_targets && inst.available_targets.length)
            ? inst.available_targets
            : ["182x"]
    );

    // 默认勾选：用户在「运行在」里选过的那些卡；其次是当前实例实际占用的
    // 卡（多卡时全部勾上）；再其次 config.compute.autostart 里声明的
    // npu_index；最后兜底第一个 supported target。
    let defaults = new Set(this._selectedTargets(modelId));
    if (defaults.size === 0) {
      // 模型当前实际占用的卡（同时跑两张卡时两张都勾上）
      const running = this._modelNpus(modelId);
      if (running.length) defaults = new Set(running);
    }
    if (defaults.size === 0 && inst && inst.npu_index >= 0) {
      defaults.add(inst.npu_index);
    }
    if (defaults.size === 0 && inst
        && Number.isInteger(inst._default_npu_index)
        && inst._default_npu_index >= 0) {
      defaults.add(inst._default_npu_index);
    }
    if (defaults.size === 0) {
      for (const idx of sortedIndices) {
        if (supported.has("182x")) { defaults.add(idx); break; }
      }
    }
    // 只保留真实枚举出来的卡，避免 snapshot 里的陈旧卡号留下幽灵勾选。
    for (const n of Array.from(defaults)) {
      if (!sortedIndices.includes(n)) defaults.delete(n);
    }
    this._selectedTarget[modelId] = Array.from(defaults);

    let rendered = 0;
    const renderOpt = (npuIndex, title, sub, targetFamily, supportedFlag) => {
      // 不支持的 target 不渲染（直接跳过）—— 用户只看到能用的 NPU 卡。
      const isSup = supportedFlag !== false;
      if (!isSup) return;
      const label = document.createElement("label");
      label.className = "cc-target-opt";
      label.title = sub || "";
      const input = document.createElement("input");
      // checkbox 而非 radio：同一个模型可以同时跑在多张卡上（每个勾选的
      // 卡各起一个 worker，各占一张卡）。
      input.type = "checkbox";
      input.value = String(npuIndex);
      input.className = "cc-target-radio";
      if (defaults.has(npuIndex)) input.checked = true;
      input.addEventListener("change", () => {
        const cur = new Set(this._selectedTargets(modelId));
        if (input.checked) cur.add(npuIndex); else cur.delete(npuIndex);
        this._selectedTarget[modelId] = Array.from(cur);
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
    // "RK182x #0"，input value 也是 0（直接给 worker 用）。
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
    const sorted = this._sortNpuCards(this._modelCards(cards));
    return sorted.length ? sorted[0].npu_index : 0;
  },

  // 只保留「能跑 rknn 模型」的卡。``snapshot.cards`` 里混着 USB 序列号形态
  // 的 RGA/VPU 设备（模型一上去就 vision init_runtime failed）。模型卡位由
  // 后端 ``model_cards`` 给出；老后端没这字段时不过滤。
  _modelCards(cards) {
    const mc = this._snapshot && this._snapshot.model_cards;
    if (!Array.isArray(mc)) return cards;
    const keep = new Set(mc);
    return (cards || []).filter((c) => keep.has(c.npu_index));
  },

  // NPU 卡片按 enumerate index (npu_index) 升序，与 label "RK182x #N"
  // 完全对齐。早期版本按 device_id 字符串排序，会让 Position 1 显示
  // "RK182x #1" 而 Position 2 显示 "RK182x #0"（label 与位置错位），
  // 跟系统信息页 / 勾选 #N 实际跑在 #N 上对不上。
  //
  // 真相：wdnai 枚举的 npu_index 本身就是 canonical 编号（与系统
  // 信息页 / librknnsmi 对齐后的 index 同源），按 npu_index 排即按
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

  // v0.8: 校验"已选 NPU"——用户必须先在「运行在」里勾至少一张卡，
  // toggle 才能启用。已 bind 的实例也算（多卡时任一张占用即算）。
  _hasTargetPicked(modelId, svc) {
    if (svc && svc.npu_index >= 0) return true;
    if (this._modelNpus(modelId).length) return true;
    return this._selectedTargets(modelId).length > 0;
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
    // v0.15: 双段汇总"本机 X / 远端 Y",不再合并。snapshot 端产出 6 个
    // per-source 字段(running/loading/error),客户端直接读。not_loaded
    // 不计入 —— 是默认态,per-card "未启用" badge 已显示。
    const rk182xCount = Math.min(
      CC_MAX_RK182X_CARDS,
      cards.length,
    );
    const L = {
      run:  snap.local_running_count  || 0,
      load: snap.local_loading_count || 0,
      err:  snap.local_error_count   || 0,
    };
    const R = {
      run:  snap.remote_running_count  || 0,
      load: snap.remote_loading_count || 0,
      err:  snap.remote_error_count   || 0,
    };
    const localParts = [];
    if (L.run)  localParts.push(`${L.run} 运行`);
    if (L.load) localParts.push(`${L.load} 加载`);
    if (L.err)  localParts.push(`${L.err} 失败`);
    const remoteParts = [];
    if (R.run)  remoteParts.push(`${R.run} 运行`);
    if (R.load) remoteParts.push(`${R.load} 加载`);
    if (R.err)  remoteParts.push(`${R.err} 失败`);
    const parts = [`${rk182xCount} 张算力卡`];
    if (localParts.length)  parts.push(`本机 ${localParts.join("/")}`);
    if (remoteParts.length) parts.push(`远端 ${remoteParts.join("/")}`);
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

/* ─────────────────────────────────────────────────────────────────────
 * Bootstrap: refresh-btn click + hashchange routing for system pages
 * ───────────────────────────────────────────────────────────────────── */

const SYSTEM_PAGES = new Set([
  "system-monitor", "system-logs", "base-config",
  "compute-config", "discovery", "im-bridge", "apps",
]);

// Inline copy of framework.js's currentPage() — the original lives inside
// framework.js's IIFE so it's not visible from here.
function currentPage() {
  const h = (location.hash || "").replace(/^#\/?/, "");
  return h || "apps";
}

/* Per-page loaders. Each entry is invoked on first navigation to the page
 * (and on every [data-refresh] click). Each function is idempotent — modules
 * guard with `initialized` flags so repeat calls are cheap.
 */
const SYSTEM_PAGE_LOADERS = {
  "system-monitor": async () => {
    const json = document.getElementById("data-system-monitor");
    if (json) json.textContent = "加载中…";
    try {
      const r = await fetch(API_PREFIX + "/system-monitor", { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      if (typeof renderSystemMonitor === "function") {
        renderSystemMonitor(await r.json());
      }
    } catch (err) {
      if (json) json.textContent = "请求失败: " + err.message;
    }
  },
  "system-logs": () => {
    if (typeof Logs !== "undefined" && Logs.init) {
      Logs.init();
      Logs.refresh();
    }
  },
  "base-config": () => {
    if (typeof Base !== "undefined" && Base.init) {
      Base.init();
      Base.load();
    }
  },
  "compute-config": () => {
    if (typeof ComputeConfigPage !== "undefined" && ComputeConfigPage.init) {
      ComputeConfigPage.init();
      ComputeConfigPage.load();
      // v0.4 修复：算力配置页 enable 加载中模型时,倒计时 / RK182X 卡
      // 内存 / toggle 状态全靠 1Hz 轮询更新 —— 不开 setInterval 的话
      // 倒计时永远停在首次 load 的快照值(典型现象:"初始化中… 约 59s"
      // 卡 59s 不动,刷新网页后才看到已加载)。startTicking 内部有
      // _tickHandle 防重复,多次调用安全。
      ComputeConfigPage.startTicking();
    }
  },
  "discovery": () => {
    if (typeof Discovery !== "undefined" && Discovery.init) {
      Discovery.init();
      Discovery.load();
    }
  },
  // PowerMenu / ConfigMenu / CoprocessorMenu are sidebar-bottom popovers
  // (always-visible buttons). They self-init on first DOMContentLoaded but
  // we also call init() here for completeness in case the user navigates
  // around before they self-bootstrap.
  "power":       () => { /* PowerMenu already auto-inited by chunk 4 module-load */ },
  "coprocessor": () => { /* CoprocessorMenu already auto-inited by chunk 6 module-load */ },
  // im-bridge 是系统配置页(应用对接 / 微信机器人 = 同一页)。原本在
  // app/apps/im_bridge/ 作为卡片加载,v0.4 整理后归到 system/ —— 单独
  // 静态文件,首次进入页面时按需加载并 init。
  // v0.4 回归:loader 必须 ensureWs(),否则 WS 不建、server broadcast
  // 找不到 subscriber,登录按钮 click 后卡"正在向微信申请二维码"。
  "im-bridge": () => {
    if (typeof ImBridge !== "undefined" && ImBridge.init) {
      ImBridge.init();
      ImBridge.ensureWs();
      return;
    }
    const s = document.createElement("script");
    s.src = "/static/system/im_bridge/app.js";
    s.defer = false;
    s.onload = () => {
      if (typeof ImBridge !== "undefined" && ImBridge.init) {
        ImBridge.init();
        ImBridge.ensureWs();
      }
    };
    document.head.appendChild(s);
  },
  // apps is the home grid; framework.js renders it on boot.
};

async function loadSystemPageData(name) {
  const loader = SYSTEM_PAGE_LOADERS[name];
  if (!loader) return;
  try {
    await loader();
  } catch (err) {
    console.error("[system.js]", name, "load failed:", err);
  }
}

document.querySelectorAll("[data-refresh]").forEach((btn) => {
  btn.addEventListener("click", () => loadSystemPageData(currentPage()));
});

window.addEventListener("hashchange", () => {
  const name = currentPage();
  // v0.4 修复:离开算力配置页时关掉 1Hz 轮询 —— 否则 setInterval 持续
  // 每秒打 /api/compute,即便页面已经 hidden。loader 内部 startTicking
  // 的 _tickHandle 防重复,这里 stopTicking 必须放 hashchange 全局,
  // 因为框架 ROUTER.showPage (framework.js) 不会通知本页资源清理。
  if (name !== "compute-config"
      && typeof ComputeConfigPage !== "undefined"
      && ComputeConfigPage.stopTicking) {
    ComputeConfigPage.stopTicking();
  }
  if (SYSTEM_PAGES.has(name)) {
    loadSystemPageData(name);
  }
});

if (SYSTEM_PAGES.has(currentPage())) {
  loadSystemPageData(currentPage());
}

if (typeof fetchVersion === "function") fetchVersion();

// Sidebar 底栏 popover 互相排他(TTS 卡片需要引用)—— TTS 在 defer 加载后
// 找到 ``window.SystemMenus`` 即把三菜单的 .open() wrap 上 "打开前先关别人"。
window.SystemMenus = { PowerMenu, ConfigMenu, CoprocessorMenu };

})();
