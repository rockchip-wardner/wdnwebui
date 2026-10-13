(function () {
  "use strict";
  const API_PREFIX = "/api";
  // core helpers (fmtNum, fmtEventTime, vtlPaletteRGB, vtlLabelColor) via app/static/core/*.js
  const fmtNum = window.fmtNum;
  const fmtEventTime = window.fmtEventTime;
  const vtlPaletteRGB = window.vtlPaletteRGB;
  const vtlLabelColor = window.vtlLabelColor;

const VTL = {
  ws: null,
  wsRetry: 0,
  wsTimer: null,
  blobUrl: null,
  lastSnap: null,
  detectorSnap: null,
  source: "",
  prompt: "",
  everyN: 30,
  lastEventId: 0,
  frozenFrameUrl: null,
  frozenBoxes: null,
  frozenUntil: 0,
};
const VTLDetector = {
  events: [],
  renderedIds: new Set(),
  eventNodes: new Map(),
  MAX_EVENTS: 200,
  MAX_RENDERED: 50,
  palette: vtlPaletteRGB,
};

function setVtlOverlay(text, isError) {
  const el = document.getElementById("vtl-overlay");
  if (!el) return;
  el.textContent = text || "";
  el.classList.toggle("error", !!isError);
  el.style.display = text ? "" : "none";
}

function setVtlMeta(snap) {
  const el = document.getElementById("vtl-meta");
  if (!el) return;
  const parts = [];
  if (snap.width && snap.height) parts.push(snap.width + "x" + snap.height);
  if (snap.fps) parts.push(fmtNum(snap.fps, 2) + " fps");
  if (snap.frame_pos != null) parts.push("帧 " + snap.frame_pos);
  if (snap.is_live) parts.push("LIVE");
  if (snap.state) parts.push("[" + snap.state + "]");
  el.textContent = parts.join(" · ") || "--";
  VTL.lastSnap = snap;
  if (typeof DemoGuard !== "undefined") {
    DemoGuard.report("video-target-localization", {
      session: VTL.lastSnap,
      detector: VTL.detectorSnap,
    });
  }
}

function setVtlProgress(snap) {
  const el = document.getElementById("vtl-progress");
  if (!el) return;
  el.classList.toggle("live", !!snap.is_live);
  el.style.width = snap.is_live ? "100%" : "0%";
}

async function drawVtlFrozenFrame() {
  // 把被定位的那一帧 + bbox 画到 canvas。两个调用点:
  //   1. 事件到达瞬间 (onmessage event 分支) —— 消除 5ms 视觉跳变。
  //   2. 冻结期内每个 binary frame (drawVtlFrame frozen 分支) ——
  //      保持 canvas 持续刷新,避免浏览器对长期静止的 canvas 做
  //      渲染管线优先级降级。
  const canvas = document.getElementById("vtl-canvas");
  if (!canvas || !VTL.frozenFrameUrl) return;
  const img = new Image();
  await new Promise((resolve) => {
    img.onload = resolve;
    img.onerror = resolve;
    img.src = VTL.frozenFrameUrl;
  });
  const ctx = canvas.getContext("2d");
  canvas.width = img.naturalWidth || canvas.width;
  canvas.height = img.naturalHeight || canvas.height;
  ctx.drawImage(img, 0, 0);
  if (VTL.frozenBoxes && VTL.frozenBoxes.length) {
    drawVtlBoxes(canvas, VTL.frozenBoxes, VTLDetector.palette);
  }
}

async function drawVtlFrame(jpgBytes) {
  // 冻结期内重画冻结帧 (与原始行为一致:canvas 在 2s 冻结期持续刷新,
  // 避免浏览器渲染管线对长期不写入的 canvas 做优先级降级)。
  // 事件到达瞬间 drawVtlFrozenFrame 已画过一次,这里把 frozen 期内
  // 每个 binary frame 都走 frozen 分支再画一次 (image 是 data URL,
  // load 是同步的,实际 CPU 开销很小)。
  const canvas = document.getElementById("vtl-canvas");
  if (!canvas) return;
  if (VTL.frozenUntil > Date.now() && VTL.frozenFrameUrl) {
    await drawVtlFrozenFrame();
    return;
  }
  if (!jpgBytes || !jpgBytes.byteLength) return;
  const blob = new Blob([jpgBytes], { type: "image/jpeg" });
  if (VTL.blobUrl) URL.revokeObjectURL(VTL.blobUrl);
  VTL.blobUrl = URL.createObjectURL(blob);
  const img = new Image();
  await new Promise((resolve, reject) => {
    img.onload = resolve;
    img.onerror = reject;
    img.src = VTL.blobUrl;
  });
  const ctx = canvas.getContext("2d");
  canvas.width = img.naturalWidth || canvas.width;
  canvas.height = img.naturalHeight || canvas.height;
  ctx.drawImage(img, 0, 0);
}

function connectVtlWs() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}${API_PREFIX}/video-target-localization/ws`;
  const ws = new WebSocket(url);
  ws.binaryType = "arraybuffer";
  VTL.ws = ws;

  ws.onopen = () => {
    VTL.wsRetry = 0;
    setVtlOverlay("", false);
  };
  ws.onmessage = async (ev) => {
    if (typeof ev.data === "string") {
      let snap;
      try { snap = JSON.parse(ev.data); } catch (_) { return; }
      if (snap.type === "eof") {
        setVtlOverlay("文件已结束 (EOF)，点 播放 可恢复", true);
      } else if (snap.type === "error") {
        setVtlOverlay("WS 错误: " + (snap.detail || ""), true);
      } else if (snap.type === "event") {
        addVtlEvent(snap);
        showVtlEventBadge(snap);
        // 只有定位到目标时才冻结画布 2 秒,让 bbox 与文字提示对得上;
        // 没有 box 时放过,实时流不被冻。冻结期内 binary frame 由
        // drawVtlFrame 内部的 frozen 分支重画冻结帧,保持 canvas
        // 持续刷新 —— 见下方 onmessage 的注释。
        if (snap.frame_b64 && snap.boxes && snap.boxes.length) {
          if (VTL.frozenFrameUrl) URL.revokeObjectURL(VTL.frozenFrameUrl);
          VTL.frozenFrameUrl =
            "data:image/jpeg;base64," + snap.frame_b64;
          VTL.frozenBoxes = snap.boxes;
          VTL.frozenUntil = Date.now() + 2000;
          // 事件到达瞬间画一次冻结帧 (消除 5ms 视觉跳变),后续 binary
          // frame 由 drawVtlFrame frozen 分支接力重画冻结帧。
          await drawVtlFrozenFrame();
        }
        return;
      } else if (snap.type === "status" && snap.detector) {
        updateVtlDetectorStatus(snap.detector);
      }
      setVtlMeta(snap);
      setVtlProgress(snap);
      refreshVtlButtons(snap.state);
      window.FrameJudge?.update("vtl", snap.judge);
      const payload = document.getElementById("data-video-target-localization");
      if (payload) payload.textContent = JSON.stringify(snap, null, 2);
      return;
    }
    // 冻结期内的 binary frame 由 drawVtlFrame 内部的 frozen 分支
    // 处理 (重画冻结帧),保证 canvas 在 2s 冻结期内持续刷新 —— 避免
    // 浏览器对长期静止的 canvas 做渲染管线优先级降级。
    try {
      await drawVtlFrame(new Uint8Array(ev.data));
      setVtlOverlay("", false);
    } catch (e) {
      setVtlOverlay("帧渲染失败: " + e, true);
    }
  };
  ws.onclose = () => {
    VTL.ws = null;
    const wait = Math.min(5000, 500 * Math.pow(2, VTL.wsRetry++));
    VTL.wsTimer = setTimeout(connectVtlWs, wait);
  };
  ws.onerror = () => {
    try { ws.close(); } catch (_) {}
  };
}

async function vtlOpen(payload) {
  const r = await fetch(API_PREFIX + "/video-target-localization/open", {
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

async function vtlAction(action) {
  const r = await fetch(API_PREFIX + "/video-target-localization/control", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action }),
  });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

async function vtlStatus() {
  const r = await fetch(API_PREFIX + "/video-target-localization", { cache: "no-store" });
  if (!r.ok) return null;
  const snap = await r.json();
  setVtlMeta(snap);
  setVtlProgress(snap);
  refreshVtlButtons(snap.state);
  const loopEl = document.getElementById("vtl-loop");
  if (loopEl && snap.loop != null) loopEl.checked = !!snap.loop;
  const payload = document.getElementById("data-video-target-localization");
  if (payload) payload.textContent = JSON.stringify(snap, null, 2);
  return snap;
}

async function vtlClose() {
  const r = await fetch(API_PREFIX + "/video-target-localization/close", { method: "POST" });
  return r.ok ? r.json() : null;
}

function refreshVtlButtons(state) {
  const playBtn = document.getElementById("vtl-play");
  const pauseBtn = document.getElementById("vtl-pause");
  const stopBtn = document.getElementById("vtl-stop");
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

// vtlRenderHistoryItem: draw the full frame onto the overlay canvas,
// then call drawVtlBoxes to overlay all bbox rectangles + labels.
// Used both for the history list (small) and the frame modal (full size).
async function vtlRenderHistoryItem(canvas, frameImg, boxes, palette) {
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  const w = frameImg.naturalWidth || frameImg.width || canvas.width;
  const h = frameImg.naturalHeight || frameImg.height || canvas.height;
  canvas.width = w;
  canvas.height = h;
  ctx.clearRect(0, 0, w, h);
  try {
    ctx.drawImage(frameImg, 0, 0, w, h);
  } catch (_) { /* ignore draw errors */ }
  drawVtlBoxes(canvas, boxes || [], palette || vtlPaletteRGB);
}

// drawVtlBoxes: paint each box's rectangle + label tag at the top.
// Boxes arrive as `[{label, bbox:[x1,y1,x2,y2], ...}, ...]` (already
// in pixel coords from the backend). Empty array → canvas just shows
// the frame.
function drawVtlBoxes(canvasEl, boxes, palette) {
  if (!canvasEl || !boxes || !boxes.length) return;
  const ctx = canvasEl.getContext("2d");
  if (!ctx) return;
  // Scale stroke / font / tag box with canvas natural width so annotations
  // stay legible on 1080p+ frames. Reference 960px keeps base values for
  // small thumbnails; cap at 3x to bound 4K/8K overlays.
  const scale = Math.min(3, Math.max(1, canvasEl.width / 960));
  const lineW = Math.max(2, Math.round(3 * scale));
  const fontPx = Math.max(11, Math.round(13 * scale));
  const tagH = Math.max(14, Math.round(16 * scale));
  const pad = Math.max(3, Math.round(4 * scale));
  const textOffset = Math.max(2, Math.round(3 * scale));
  ctx.lineWidth = lineW;
  ctx.font = `${fontPx}px sans-serif`;
  ctx.textBaseline = "bottom";
  for (const box of boxes) {
    const label = (box && box.label) ? String(box.label) : "";
    const bb = (box && Array.isArray(box.bbox)) ? box.bbox : null;
    if (!bb || bb.length !== 4) continue;
    const [x1, y1, x2, y2] = bb;
    const w = Math.max(1, x2 - x1);
    const h = Math.max(1, y2 - y1);
    const [r, g, b] = vtlLabelColor(label);
    ctx.strokeStyle = `rgb(${r},${g},${b})`;
    ctx.strokeRect(x1 + 0.5, y1 + 0.5, w, h);
    if (label) {
      const tag = label;
      const metrics = ctx.measureText(tag);
      const tagW = Math.ceil(metrics.width) + pad * 2;
      const tagX = Math.max(0, x1);
      const tagY = Math.max(0, y1 - tagH);
      ctx.fillStyle = `rgb(${r},${g},${b})`;
      ctx.fillRect(tagX, tagY, tagW, tagH);
      ctx.fillStyle = "#fff";
      ctx.fillText(tag, tagX + pad, tagY + tagH - textOffset);
    }
  }
}

// Build one <li> element for the history list.
// Layout:
//   <li class="vd-event">
//     <div class="vd-event-meta">ts · 帧 pos · #id</div>
//     <div class="vtl-crop-thumb-wrap">
//       <img class="vtl-crop-thumb" />           <!-- frame_b64 -->
//       <canvas class="vtl-crop-overlay" />      <!-- bbox overlay -->
//     </div>
//     <div class="vtl-event-meta">
//       <span class="vtl-event-bbox-label" /> ... <!-- one per box -->
//       <span class="vd-event-text"> …text… </span>
//       <span class="vtl-dblclick-hint">双击查看</span>
//     </div>
//   </li>
function buildVtlEventLi(ev, isLatest) {
  const li = document.createElement("li");
  li.className = "vd-event" + (isLatest ? " latest" : "");
  li.dataset.evId = String(ev.id);

  const meta = document.createElement("div");
  meta.className = "vd-event-meta";
  const tsSpan = document.createElement("span");
  tsSpan.textContent = fmtEventTime(
    ev.video_ts != null ? ev.video_ts : ev.ts
  );
  const fpSpan = document.createElement("span");
  fpSpan.textContent = "帧 " + (ev.frame_pos ?? "?");
  const tag = document.createElement("span");
  tag.className = "tag";
  tag.textContent = "#" + ev.id;
  meta.appendChild(tsSpan);
  meta.appendChild(fpSpan);
  // 判据分数：区分「超阈值触发」和「兜底间隔触发」。三张卡共用同一份判据，
  // 所以三处都要显示 —— 只给一张卡加就是"同样的配置、不同的信息"。
  const jsSpan = window.FrameJudge?.scoreSpan?.("vtl", ev);
  if (jsSpan) meta.appendChild(jsSpan);
  meta.appendChild(tag);
  li.appendChild(meta);

  const boxes = Array.isArray(ev.boxes) ? ev.boxes : [];

  // 缩略图 + bbox overlay canvas
  if (ev.frame_b64) {
    const wrap = document.createElement("div");
    wrap.className = "vtl-crop-thumb-wrap vtl-event-frame";
    wrap.title = "双击查看大图";

    const img = document.createElement("img");
    img.className = "vtl-crop-thumb";
    img.alt = "定位帧";
    img.src = "data:image/jpeg;base64," + ev.frame_b64;

    const overlay = document.createElement("canvas");
    overlay.className = "vtl-crop-overlay";

    wrap.appendChild(img);
    wrap.appendChild(overlay);
    li.appendChild(wrap);

    // Wait for image load, then draw frame + bbox overlay.
    img.addEventListener("load", () => {
      vtlRenderHistoryItem(overlay, img, boxes, VTLDetector.palette);
    });
    // 双击打开 modal
    wrap.addEventListener("dblclick", () => vtlOpenFrameModal(ev));
  }

    if (boxes.length > 0) {
    const counts = {};
    for (const box of boxes) {
      const label = box.label || "(未命名)";
      counts[label] = (counts[label] || 0) + 1;
    }
    const parts = [`定位到 ${boxes.length} 个目标`];
    for (const [label, count] of Object.entries(counts)) {
      parts.push(`"${label}" 数量：${count}`);
    }
    const summary = document.createElement("div");
    summary.className = "vtl-event-summary";
    summary.textContent = parts.join("; ");
    li.appendChild(summary);
  }

  // 双击提示
  const dblHint = document.createElement("span");
  dblHint.className = "vtl-dblclick-hint";
  dblHint.textContent = "双击查看";
  li.appendChild(dblHint);

  // li 整体也支持双击（兜底：点缩略图之外也触发 modal）
  li.addEventListener("dblclick", () => vtlOpenFrameModal(ev));

  return li;
}

function trimVtlEventList(list) {
  while (VTLDetector.renderedIds.size > VTLDetector.MAX_RENDERED) {
    const oldestId = list.lastElementChild
      ? Number(list.lastElementChild.dataset.evId)
      : null;
    if (oldestId == null || Number.isNaN(oldestId)) break;
    const node = VTLDetector.eventNodes.get(oldestId);
    if (node) {
      try { node.remove(); } catch (_) { /* ignore */ }
    }
    VTLDetector.eventNodes.delete(oldestId);
    VTLDetector.renderedIds.delete(oldestId);
  }
}

function renderVtlEventList() {
  const list = document.getElementById("vtl-event-list");
  const count = document.getElementById("vtl-events-count");
  if (!list || !count) return;
  count.textContent = VTLDetector.events.length + " 条";
  list.innerHTML = "";
  VTLDetector.renderedIds.clear();
  VTLDetector.eventNodes.clear();
  if (VTLDetector.events.length === 0) {
    list.innerHTML =
      '<li class="vd-event-empty">尚未定位到目标，启动定位后结果显示在这里。</li>';
    return;
  }
  const slice = VTLDetector.events.slice(0, VTLDetector.MAX_RENDERED);
  for (const ev of slice) {
    const li = buildVtlEventLi(ev, ev === slice[0]);
    list.appendChild(li);
    VTLDetector.renderedIds.add(ev.id);
    VTLDetector.eventNodes.set(ev.id, li);
  }
}

function appendVtlEventLi(ev) {
  const list = document.getElementById("vtl-event-list");
  if (!list) return;
  const emptyLi = list.querySelector(".vd-event-empty");
  if (emptyLi) {
    list.innerHTML = "";
    VTLDetector.renderedIds.clear();
    VTLDetector.eventNodes.clear();
  }
  const prevLatest = list.querySelector(".vd-event.latest");
  if (prevLatest) prevLatest.classList.remove("latest");
  const li = buildVtlEventLi(ev, true);
  list.insertBefore(li, list.firstChild);
  VTLDetector.renderedIds.add(ev.id);
  VTLDetector.eventNodes.set(ev.id, li);
  trimVtlEventList(list);
}

function addVtlEvent(ev) {
  VTLDetector.events.unshift(ev);
  if (VTLDetector.events.length > VTLDetector.MAX_EVENTS) {
    VTLDetector.events.length = VTLDetector.MAX_EVENTS;
  }
  VTL.lastEventId = ev.id;
  appendVtlEventLi(ev);
  const count = document.getElementById("vtl-events-count");
  if (count) count.textContent = VTLDetector.events.length + " 条";
}

let vtlBadgeHideTimer = null;
function showVtlEventBadge(ev) {
  // bbox 框就是事件反馈; 历史/双击 modal 里有详情, 不弹空文字黑框。
  const badge = document.getElementById("vtl-event-badge");
  if (badge) badge.classList.add("hidden");
  if (vtlBadgeHideTimer) { clearTimeout(vtlBadgeHideTimer); vtlBadgeHideTimer = null; }
}

// 双击历史项 → 弹出 #vtl-frame-modal 大图预览。
// 写入 frame_b64 到 <img> + frame_pos / ts / level / text / boxes 列表。
function vtlOpenFrameModal(ev) {
  if (!ev) return;
  const modal = document.getElementById("vtl-frame-modal");
  if (!modal) return;
  modal.classList.remove("hidden");

  const boxes = Array.isArray(ev.boxes) ? ev.boxes : [];

  const imgEl = document.getElementById("vtl-frame-modal-img");
  if (imgEl) {
    imgEl.onload = null;
    imgEl.src = ev.frame_b64
      ? "data:image/jpeg;base64," + ev.frame_b64
      : "";
    imgEl.onload = () => {
      const wrap = imgEl.parentElement;
      if (!wrap) return;
      let overlay = wrap.querySelector("canvas.vfa-frame-modal-img-overlay");
      if (!overlay) {
        overlay = document.createElement("canvas");
        overlay.className = "vfa-frame-modal-img-overlay";
        wrap.appendChild(overlay);
      }
      vtlRenderHistoryItem(overlay, imgEl, boxes, VTLDetector.palette);
    };
  }

  const fpEl = document.getElementById("vtl-frame-modal-frame-pos");
  if (fpEl) fpEl.textContent = ev.frame_pos != null ? String(ev.frame_pos) : "--";

  const tsEl = document.getElementById("vtl-frame-modal-ts");
  if (tsEl) {
    const ts = (ev.video_ts != null && ev.video_ts >= 0)
      ? ev.video_ts : ev.ts;
    tsEl.textContent = (ts != null) ? fmtEventTime(ts) : "--";
  }

  const textEl = document.getElementById("vtl-frame-modal-text");
  if (textEl) textEl.textContent = (ev.text && String(ev.text).trim()) || "(空)";

  const boxesEl = document.getElementById("vtl-frame-modal-boxes");
  if (boxesEl) {
    boxesEl.innerHTML = "";
    if (boxes.length === 0) {
      const empty = document.createElement("div");
      empty.className = "vtl-frame-modal-box-row";
      empty.textContent = "尚未定位到目标";
      boxesEl.appendChild(empty);
    } else {
      for (const box of boxes) {
        const row = document.createElement("div");
        row.className = "vtl-frame-modal-box-row";
        const [r, g, b] = vtlLabelColor(box.label || "");
        const swatch = document.createElement("span");
        swatch.style.cssText = `display:inline-block;width:10px;height:10px;background:rgb(${r},${g},${b});margin-right:6px;border-radius:2px;vertical-align:middle;`;
        const labelSpan = document.createElement("strong");
        labelSpan.textContent = box.label || "(未命名)";
        const bb = Array.isArray(box.bbox) ? box.bbox : [0, 0, 0, 0];
        row.appendChild(swatch);
        row.appendChild(labelSpan);
        row.appendChild(
          document.createTextNode(` @ [${bb[0]}, ${bb[1]}, ${bb[2]}, ${bb[3]}]`),
        );
        boxesEl.appendChild(row);
      }
    }
  }
}

function closeVtlFrameModal() {
  const modal = document.getElementById("vtl-frame-modal");
  if (modal) modal.classList.add("hidden");
  const imgEl = document.getElementById("vtl-frame-modal-img");
  if (imgEl) {
    imgEl.src = "";
    imgEl.onload = null;
  }
}

function updateVtlDetectorStatus(snap) {
  const statusEl = document.getElementById("vtl-detector-status");
  const startBtn = document.getElementById("vtl-detector-start");
  const stopBtn  = document.getElementById("vtl-detector-stop");
  if (!snap || !statusEl) return;
  if (!snap.available) {
    statusEl.textContent = "不可用: " + (snap.init_error || "wdnai 未安装");
  } else if (snap.init_error) {
    statusEl.textContent = "模型加载失败: " + snap.init_error;
  } else if (snap.enabled && snap.session_ready) {
    statusEl.textContent =
      `运行中 · 每 ${snap.judge?.every_n ?? snap.every_n} 帧 · 队列 ${snap.queue_size} · 视觉定位`;
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
  VTL.detectorSnap = snap;
  if (typeof DemoGuard !== "undefined") {
    DemoGuard.report("video-target-localization", {
      session: VTL.lastSnap,
      detector: VTL.detectorSnap,
    });
  }
}

async function refreshVtlDetector() {
  try {
    const r = await fetch(API_PREFIX + "/video-target-localization/events");
    if (!r.ok) return null;
    const data = await r.json();
    updateVtlDetectorStatus(data.snapshot || {});
    if (VTLDetector.events.length === 0 && Array.isArray(data.events)) {
      VTLDetector.events = data.events.slice();
      renderVtlEventList();
    }
    return data;
  } catch (_) { return null; }
}

async function vtlDetectorStart() {
  // 节流已上移到共享判据器，detector 一律 every_n=1（避免两级节流相乘）。
  const every_n = 1;

  const items = vtlPromptCollectItems();
  if (items.length === 0) {
    setVtlOverlay("至少需要一条非空的定位描述", true);
    return;
  }
  if (!Number.isFinite(every_n) || every_n < 1) {
    setVtlOverlay("每隔帧数值非法", true);
    return;
  }

  VTL.prompt = items.join("</c>");   // best-effort mirror of the backend-built prompt
  VTL.everyN = every_n;
  try {
    // Send raw `prompts: list[str]` to the backend; backend owns the prefix + separator.
    // The legacy `prompt: str` field is NOT sent here (mutual exclusivity enforced server-side).
    const r = await fetch(API_PREFIX + "/video-target-localization/prompt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompts: items, every_n }),
    });
    if (!r.ok) {
      const detail = await r.text().catch(() => "");
      setVtlOverlay("启动定位失败: " + detail, true);
      return;
    }
    const snap = await r.json();
    updateVtlDetectorStatus(snap);
    setVtlOverlay("", false);
  } catch (e) {
    setVtlOverlay("启动定位失败: " + e.message, true);
  }
}

async function vtlDetectorStop() {
  try {
    const r = await fetch(API_PREFIX + "/video-target-localization/prompt/stop", { method: "POST" });
    if (r.ok) updateVtlDetectorStatus(await r.json());
  } catch (e) {
    setVtlOverlay("停止定位失败: " + e.message, true);
  }
}

async function vtlEventsClear() {
  try {
    await fetch(API_PREFIX + "/video-target-localization/events/clear", { method: "POST" });
    VTLDetector.events = [];
    VTLDetector.renderedIds.clear();
    VTLDetector.eventNodes.clear();
    renderVtlEventList();
  } catch (_) {}
}

function initVideoTargetLocalization() {
  window.FrameJudge?.mount({ prefix: "vtl", canvasId: "vtl-canvas" });
  if (window.__vtlInit) return;
  window.__vtlInit = true;

  const sourceEl = document.getElementById("vtl-source");
  const loopEl   = document.getElementById("vtl-loop");
  const openBtn = document.getElementById("vtl-open");
  const closeBtn = document.getElementById("vtl-close");
  const browseBtn = document.getElementById("vtl-browse");
  const playBtn = document.getElementById("vtl-play");
  const pauseBtn = document.getElementById("vtl-pause");
  const stopBtn = document.getElementById("vtl-stop");
  if (sourceEl) VTL.source = sourceEl.value || "";

  if (openBtn) {
    openBtn.addEventListener("click", async () => {
      const src = (sourceEl?.value || "").trim();
      if (!src) {
        setVtlOverlay("请先填写视频源", true);
        return;
      }
      openBtn.disabled = true;
      setVtlOverlay("正在打开 " + src + " …", false);
      try {
        const snap = await vtlOpen({
          source: src,
          loop: !!(loopEl && loopEl.checked),
        });
        setVtlMeta(snap);
        setVtlProgress(snap);
        if (loopEl && snap.loop != null) loopEl.checked = !!snap.loop;
      } catch (e) {
        setVtlOverlay("打开失败: " + e.message, true);
      } finally {
        openBtn.disabled = false;
      }
    });
  }

  // Browse button: open the shared file picker modal (NOT a local
  // upload — the path selected here is the path on the device the
  // server is running on).v0.4 后 modal 在 app/static/core/fs-modal.js
  // 自挂 DOM(``window.VdFsModal`` 是 ``window.FSModal`` 的兼容别名);
  // Escape / 点背景关闭逻辑已迁到 fs-modal.js 内部统一处理。
  if (browseBtn) {
    browseBtn.addEventListener("click", () => {
      window.VdFsModal._sourceTarget = sourceEl;
      window.VdFsModal.open("");
    });
  }

  if (closeBtn) {
    closeBtn.addEventListener("click", async () => {
      try { await vtlClose(); } catch (_) {}
      setVtlOverlay("已关闭", false);
    });
  }

  if (playBtn) {
    playBtn.addEventListener("click", async () => {
      try {
        const r = await vtlAction("play");
        await vtlStatus();
        if (r && r.ok === false) setVtlOverlay("播放失败: " + (r.reason || r.state), true);
        else setVtlOverlay("", false);
      } catch (e) { setVtlOverlay("播放失败: " + e.message, true); }
    });
  }
  if (pauseBtn) {
    pauseBtn.addEventListener("click", async () => {
      try {
        const r = await vtlAction("pause");
        await vtlStatus();
        if (r && r.ok === false) setVtlOverlay("暂停失败: " + (r.reason || r.state), true);
        else setVtlOverlay("", false);
      } catch (e) { setVtlOverlay("暂停失败: " + e.message, true); }
    });
  }
  if (stopBtn) {
    stopBtn.addEventListener("click", async () => {
      try {
        const r = await vtlAction("stop");
        await vtlStatus();
        if (r && r.ok === false) setVtlOverlay("停止失败: " + (r.reason || r.state), true);
        else setVtlOverlay("已停止", false);
      } catch (e) { setVtlOverlay("停止失败: " + e.message, true); }
    });
  }

  const detStartBtn = document.getElementById("vtl-detector-start");
  const detStopBtn  = document.getElementById("vtl-detector-stop");
  const evClearBtn  = document.getElementById("vtl-events-clear");
  if (detStartBtn) detStartBtn.addEventListener("click", vtlDetectorStart);
  if (detStopBtn)  detStopBtn.addEventListener("click",  vtlDetectorStop);
  if (evClearBtn)  evClearBtn.addEventListener("click",  vtlEventsClear);

  // 关闭 modal 按钮 + 点遮罩关闭
  const modalCloseBtn = document.getElementById("vtl-frame-modal-close");
  const modal = document.getElementById("vtl-frame-modal");
  if (modalCloseBtn) modalCloseBtn.addEventListener("click", closeVtlFrameModal);
  if (modal) {
    modal.addEventListener("click", (e) => {
      if (e.target === modal) closeVtlFrameModal();
    });
  }
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      const m = document.getElementById("vtl-frame-modal");
      if (m && !m.classList.contains("hidden")) closeVtlFrameModal();
    }
  });

  refreshVtlDetector();
  if (!VTL.ws && !VTL.wsTimer) connectVtlWs();
  initVtlPromptRows();
}

/* ------------------------------------------------------------------ *
 *  vtl prompt row management (Todo 4)                                *
 * ------------------------------------------------------------------ */
const VTL_PROMPT_CONST = {
  MAX_ROWS: 20,
  MIN_ROWS: 1,
  DEFAULT_PREFIX: "找出所有符合以下描述的对象：",
  SEPARATOR: "</c>",
};

function initVtlPromptRows() {
  if (window.__vtlPromptRowsInited) return;
  window.__vtlPromptRowsInited = true;

  const addBtn = document.getElementById("vtl-prompt-add");
  const rowsEl = document.getElementById("vtl-prompt-rows");
  if (!addBtn || !rowsEl) return;

  // Event delegation: one click listener handles all current + future remove buttons.
  rowsEl.addEventListener("click", (e) => {
    const btn = e.target.closest(".vtl-prompt-row-remove");
    if (!btn) return;
    const row = btn.closest(".vtl-prompt-row");
    if (!row) return;
    vtlPromptRemoveRow(row);
  });

  addBtn.addEventListener("click", () => vtlPromptAddRow());

  // Initial state — page already has 1 row from HTML, just sync the counters.
  vtlPromptRenumber();
  vtlPromptUpdateAddBtn();
}

function vtlPromptAddRow(focus = true) {
  const rowsEl = document.getElementById("vtl-prompt-rows");
  if (!rowsEl) return;
  if (rowsEl.children.length >= VTL_PROMPT_CONST.MAX_ROWS) return;

  const row = document.createElement("div");
  row.className = "vtl-prompt-row";  // starts collapsed (grid-template-rows: 0fr, opacity: 0)
  row.innerHTML = `
    <div class="vtl-prompt-row-inner">
      <span class="vtl-prompt-row-num"></span>
      <input type="text" class="vtl-prompt-row-input surv-input" maxlength="200" spellcheck="false" placeholder="例如：穿着红衬衫的人" />
      <button type="button" class="vtl-prompt-row-remove vd-btn vd-btn-mini" title="删除该行" aria-label="删除该行">\u2212</button>
    </div>
  `;
  rowsEl.appendChild(row);

  // Force layout flush so the browser registers the initial collapsed state, then swap to open
  // to trigger the transition.
  void row.offsetHeight;
  requestAnimationFrame(() => row.classList.add("is-open"));

  const input = row.querySelector(".vtl-prompt-row-input");
  if (focus && input) input.focus();

  vtlPromptRenumber();
  vtlPromptUpdateAddBtn();
}

function vtlPromptRemoveRow(rowEl) {
  if (!rowEl) return;
  const rowsEl = document.getElementById("vtl-prompt-rows");
  if (!rowsEl) return;
  if (rowsEl.children.length <= VTL_PROMPT_CONST.MIN_ROWS) return;

  // Trigger collapse animation, then remove on transitionend (with timeout fallback).
  rowEl.classList.remove("is-open");
  const cleanup = () => {
    if (rowEl.parentNode) rowEl.parentNode.removeChild(rowEl);
    vtlPromptRenumber();
    vtlPromptUpdateAddBtn();
  };
  let fired = false;
  const onEnd = () => { if (!fired) { fired = true; cleanup(); } };
  rowEl.addEventListener("transitionend", onEnd, { once: true });
  setTimeout(onEnd, 220);  // Fallback if transitionend never fires
}

function vtlPromptRenumber() {
  const rowsEl = document.getElementById("vtl-prompt-rows");
  if (!rowsEl) return;
  const nums = rowsEl.querySelectorAll(".vtl-prompt-row-num");
  nums.forEach((n, i) => { n.textContent = String(i + 1); });
}

function vtlPromptUpdateAddBtn() {
  const rowsEl = document.getElementById("vtl-prompt-rows");
  const addBtn = document.getElementById("vtl-prompt-add");
  const hint   = document.getElementById("vtl-prompt-hint");
  if (!rowsEl || !addBtn) return;
  const n = rowsEl.children.length;
  addBtn.disabled = n >= VTL_PROMPT_CONST.MAX_ROWS;
  if (hint) hint.textContent = n + " / " + VTL_PROMPT_CONST.MAX_ROWS;

  // Also gate the remove buttons: disable the "\u2212" on every row when only 1 row remains.
  const removes = rowsEl.querySelectorAll(".vtl-prompt-row-remove");
  const atMin = n <= VTL_PROMPT_CONST.MIN_ROWS;
  removes.forEach((b) => { b.disabled = atMin; });
}

function vtlPromptCollectItems() {
  const rowsEl = document.getElementById("vtl-prompt-rows");
  if (!rowsEl) return [];
  const inputs = rowsEl.querySelectorAll(".vtl-prompt-row-input");
  const items = [];
  inputs.forEach((inp) => {
    const v = (inp.value || "").trim();
    if (v) items.push(v);
  });
  return items;
}

/* ------------------------------------------------------------------ *
 *  system logs page (journalctl-backed)                              *
 * ------------------------------------------------------------------ */

initVideoTargetLocalization();

// ------------------------------------------------------------------ *
// 应用向导模式 API — wizard 框架 finish 时调用
// 契约:opts 由 wizard.json 的 steps[].fields[].ui_key 决定:
//   vtl-source       → opts.source
//   vtl-loop         → opts.loop
//   vtl-prompt-rows  → opts.prompts (string[] — 与 wizard 的 prompt_rows
//                                      字段类型对应,每个元素对应应用页
//                                      一行定位提示词)
// source 非空 → 写回 #vtl-source 并调 vtlOpen;
// prompts 数组非空 → 把每个元素写回应用页 #vtl-prompt-rows 的对应
// 输入框(已有行复用,不足时调 vtlPromptAddRow 补齐),再点
// #vtl-detector-start。空字段不触发对应动作。
// 全局名约定 ImApp_<app.id 去掉 hyphen>。
// ------------------------------------------------------------------ *

// 把 wizard 给的 prompts 数组同步到应用页的 #vtl-prompt-rows:
//   * 已有行复用(超出值留空)
//   * 行数不够时调 vtlPromptAddRow 补齐(focus=false 不抢焦点)
//   * 不删应用页已有的多余空行(用户可在应用页面手动 × 删)
function _vtlWizardSyncPrompts(prompts) {
  const rowsEl = document.getElementById("vtl-prompt-rows");
  if (!rowsEl || !Array.isArray(prompts) || prompts.length === 0) return false;
  const inputs = () => Array.from(rowsEl.querySelectorAll(".vtl-prompt-row-input"));
  // 已有行先写值(后续 addRow 后再补 index >= 现有行数 的)
  const existing = inputs();
  for (let i = 0; i < prompts.length; i++) {
    if (i < existing.length) existing[i].value = prompts[i];
  }
  // 行数不够 → vtlPromptAddRow 补齐
  for (let i = existing.length; i < prompts.length; i++) {
    if (typeof vtlPromptAddRow === "function") {
      vtlPromptAddRow(false);
      const after = inputs();
      if (after[i]) after[i].value = prompts[i];
    }
  }
  if (typeof vtlPromptUpdateAddBtn === "function") vtlPromptUpdateAddBtn();
  if (typeof vtlPromptRenumber === "function") vtlPromptRenumber();
  return true;
}

window.ImApp_video_target_localization = window.ImApp_video_target_localization || {};
window.ImApp_video_target_localization.applyWizardSnapshot = async function (opts) {
  const setVal = (id, v) => { const el = document.getElementById(id); if (el != null) el.value = v; };
  const setChk = (id, v) => { const el = document.getElementById(id); if (el != null) el.checked = !!v; };

  if (opts.source)      setVal("vtl-source", opts.source);
  if (opts.loop != null) setChk("vtl-loop", opts.loop);
  // 帧判据 —— 必须在 open 之前落地：open 一开视频，判据已经在解码热路径上跑。
  const judge = await window.FrameJudge?.fromWizard("vtl", opts) ?? null;

  // prompts:数组(来自 wizard 的 prompt_rows 字段类型),过滤空白后同步
  let promptsApplied = false;
  if (Array.isArray(opts.prompts)) {
    const cleaned = opts.prompts.map((p) => (p || "").trim()).filter((p) => p.length > 0);
    if (cleaned.length > 0) promptsApplied = _vtlWizardSyncPrompts(cleaned);
  }

  const result = { source_opened: false, detector_started: false, prompts_applied: promptsApplied,
                 judge_applied: !!judge };

  if (opts.source) {
    try {
      await vtlOpen({ source: opts.source, loop: !!opts.loop });
      result.source_opened = true;
    } catch (e) {
      throw new Error("WizardSnapshotError:open_source_failed:" + (e?.message || e));
    }
  }

  // prompts 写入后再点启动按钮(start handler 读 #vtl-prompt-rows 当前内容)
  if (promptsApplied) {
    const btn = document.getElementById("vtl-detector-start");
    if (btn) {
      try { btn.click(); result.detector_started = true; }
      catch (e) { throw new Error("WizardSnapshotError:detector_start_failed:" + (e?.message || e)); }
    }
  }

  return result;
};

})();
