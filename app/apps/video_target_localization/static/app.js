(function () {
  "use strict";
  const API_PREFIX = "/api";
  // core helpers (fmtEventTime, vtlPaletteRGB, vtlLabelColor) via app/static/core/*.js
  const fmtEventTime = window.fmtEventTime;
  const vtlPaletteRGB = window.vtlPaletteRGB;
  const vtlLabelColor = window.vtlLabelColor;

const VTL = {
  ws: null,
  wsRetry: 0,
  wsTimer: null,
  lastSnap: null,
  detectorSnap: null,
  pendingIdx: -1,   // 下一条二进制帧属于哪一路（由 frame_meta 设定）
  // 每路各自冻结：idx -> {snap, boxes, frameW, frameH, until}。
  // 不能是单份全局状态 —— 32 路轮转时每 ~2.4s 就有一路命中，
  // 全局冻结会把**所有**画面按在同一张旧帧上，看起来就是"整个画面卡住不动"。
  frozen: new Map(),
};
const VTLDetector = {
  events: [],
  renderedIds: new Set(),
  eventNodes: new Map(),
  MAX_EVENTS: 200,
  MAX_RENDERED: 50,
  palette: vtlPaletteRGB,
};

// 宫格实现（建格 / 分列 / 每路单飞解码上屏 / 角标高亮）在共享组件里；
// bbox 叠加通过 paint 的 overlay 回调交回本页。
const GRID_EL = "vtl-grid";
const chNo = window.MultiGrid.chNo;

function setVtlOverlay(text, isError) {
  const el = document.getElementById("vtl-overlay");
  if (!el) return;
  el.textContent = text || "";
  el.classList.toggle("error", !!isError);
  el.style.display = text ? "" : "none";
}

function setVtlMeta(session) {
  const el = document.getElementById("vtl-meta");
  VTL.lastSnap = session;
  if (el) {
    const n = ((session && session.channel_list) || []).length;
    const parts = [];
    if (n) parts.push(`${n} 路`);
    if (session && session.state) parts.push(`[${session.state}]`);
    if (session && session.loop) parts.push("循环");
    el.textContent = parts.join(" · ") || "--";
  }
  if (typeof DemoGuard !== "undefined") {
    DemoGuard.report("video-target-localization", {
      session: VTL.lastSnap,
      detector: VTL.detectorSnap,
    });
  }
}

function setVtlProgress(session) {
  const el = document.getElementById("vtl-progress");
  if (!el) return;
  // 多路下"播放进度"没有单一含义（每路各自 frame_pos），改成
  // "已经出画面的路数 / 总路数" —— 这个聚合是真能回答用户的问题的。
  const list = (session && session.channel_list) || [];
  const live = list.filter((c) => c.has_frame).length;
  const total = list.length;
  el.classList.toggle("live", total > 0 && live === total);
  el.style.width = total ? `${Math.round((live / total) * 100)}%` : "0%";
}

/** 某一格上叠 bbox。
 *
 *  box 坐标是**送检帧**（448×252）的像素，而画面画布是**显示尺寸**
 *  （1~4 路 960×540、5+ 路 384×216），两者不等大 —— 必须按 canvas 实测
 *  尺寸 / frame_w·frame_h 换算，否则框会整体缩到左上角。
 *
 *  这个缩放比例只能在**画完帧之后**才算出来（canvas 尺寸这一刻才定），
 *  所以换算放在 overlay 回调里而不是调用处。 */
function vtlBoxesOverlay(boxes, frameW, frameH) {
  return (ctx, tile) => {
    const c = tile.canvas;
    if (!frameW || !frameH) return drawVtlBoxes(c, boxes, VTLDetector.palette);
    drawVtlBoxes(c, boxes, VTLDetector.palette, c.width / frameW, c.height / frameH);
  };
}

/** 把一路的 JPEG 画进对应瓦片；冻结期内改为重贴「命中那一帧」。
 *
 *  冻结为什么是**就地快照**而不是等一张新图：后端的 grounding 事件不带
 *  整帧（`frame_b64` 为空，见 detector_base._build_grounding_event ——
 *  worker_loop 里每事件多编一次整帧要 30~100ms），而事件到达的那一刻
 *  该路的画布上本来就有画面。把当时的像素拷进离屏 canvas 即可，
 *  零额外编码、零画质损失，也不会让 canvas 尺寸在冻结期来回跳。
 *
 *  冻结期内**仍然持续重贴**（内容不变）：浏览器渲染管线会把长期不写入的
 *  canvas 降级，冻结帧会比周围卡顿一档；反复写同一帧能把优先级提回来。
 *  共享宫格的每路单飞会丢掉中间的重复帧，不影响效果。 */
function paintVtlFrame(idx, jpgBytes) {
  const fz = VTL.frozen.get(idx);
  if (fz) {
    if (fz.until > Date.now() && fz.snap) {
      window.MultiGrid.paint(GRID_EL, idx, fz.snap,
                             vtlBoxesOverlay(fz.boxes, fz.frameW, fz.frameH));
      return;
    }
    VTL.frozen.delete(idx);
  }
  window.MultiGrid.paint(GRID_EL, idx, jpgBytes);
}

function freezeVtlChannel(idx, ev) {
  const tile = window.MultiGrid.tiles(GRID_EL).get(idx);
  if (!tile || !tile.canvas.width) return;   // 还没出画面，冻不了
  const snap = document.createElement("canvas");
  snap.width = tile.canvas.width;
  snap.height = tile.canvas.height;
  snap.getContext("2d").drawImage(tile.canvas, 0, 0);
  VTL.frozen.set(idx, {
    snap,
    boxes: ev.boxes || [],
    frameW: ev.frame_w || 0,
    frameH: ev.frame_h || 0,
    until: Date.now() + 2000,
  });
}

function updateVtlTiles(session) {
  if (!session) return;
  const list = session.channel_list || [];
  const vlm = session.vlm || {};
  const cur = typeof vlm.current === "number" ? vlm.current : -1;
  const { total, ok, eof } = window.MultiGrid.status(GRID_EL, list, cur);
  // 通道数缩小时清掉多出来的冻结态，否则残留的 base64 会一直占着内存。
  for (const idx of Array.from(VTL.frozen.keys())) {
    if (idx >= total) VTL.frozen.delete(idx);
  }
  const hint = document.getElementById("vtl-channel-hint");
  if (hint) {
    if (!total) hint.textContent = "尚未打开视频";
    else if (ok === total) hint.textContent = `${total} 路视频 · 全部正常`;
    else hint.textContent = `${total} 路视频 · ${ok} 路正常`;
  }
  // 提示层。文件源播完（loop 未勾）会停在 eof 不再产帧，画面定格在最后一帧 ——
  // 不说的话就是「画面卡住 + 提示条报 0 路正常」，用户完全无从判断。
  if (total && eof === total) setVtlOverlay("视频已播完\n\n勾选「循环播放」可自动重播，或点 播放 继续", false);
  else if (eof) setVtlOverlay(`${eof} 路视频已播完`, false);
  else if (total && ok === total) setVtlOverlay("", false);
  else if (total) setVtlOverlay("部分视频尚未出画面…", false);
  else setVtlOverlay("尚未打开视频\n\n在右侧填入视频地址后点击「打开」", false);
}

function connectVtlWs() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}${API_PREFIX}/video-target-localization/ws`;
  const ws = new WebSocket(url);
  ws.binaryType = "arraybuffer";
  VTL.ws = ws;

  ws.onopen = () => {
    VTL.wsRetry = 0;
  };
  ws.onmessage = async (ev) => {
    if (typeof ev.data === "string") {
      let snap;
      try { snap = JSON.parse(ev.data); } catch (_) { return; }
      if (snap.type === "frame_meta") {
        // 多路协议：二进制帧前先来一条 meta 说明这帧属于哪一路
        VTL.pendingIdx = snap.idx;
        return;
      } else if (snap.type === "event_history") {
        // 连接建立时服务端一次性打包发来的历史事件。必须走**整体重建**：
        // 逐条 addVtlEvent 实测 200 条突发能把渲染进程打满一个核。
        // 服务端按 id 升序（旧→新）发，本地数组是新→旧，所以要反转。
        VTLDetector.events = (Array.isArray(snap.events) ? snap.events : []).slice().reverse();
        renderVtlEventList();
        return;
      } else if (snap.type === "event") {
        addVtlEvent(snap);
        // 定位到目标时把**该路**画面定格 2 秒，让 bbox 与画面严格对得上；
        // 没有 box 时放过，实时流不被冻。多路下必须按路冻结 —— 用一份全局
        // 冻结态会把所有画面按在同一张旧帧上，看起来就是"整个画面卡住"。
        if (snap.boxes && snap.boxes.length) {
          const ch = typeof snap.channel === "number" ? snap.channel : 0;
          freezeVtlChannel(ch, snap);
          // 事件到达瞬间画一次（消除视觉跳变），后续由 paintVtlFrame 接力。
          paintVtlFrame(ch, null);
        }
        return;
      } else if (snap.type === "status") {
        setVtlMeta(snap.session);
        setVtlProgress(snap.session);
        updateVtlTiles(snap.session);
        refreshVtlButtons(snap.session && snap.session.state);
        if (snap.detector) {
          VTL.detectorSnap = snap.detector;
          updateVtlDetectorStatus(snap.detector);
        }
        window.FrameJudge?.update("vtl", snap.session && snap.session.judge);
        const payload = document.getElementById("data-video-target-localization");
        if (payload) payload.textContent = JSON.stringify(snap, null, 2);
        return;
      }
      setVtlOverlay("", false);
      return;
    }
    try {
      paintVtlFrame(VTL.pendingIdx >= 0 ? VTL.pendingIdx : 0,
                    new Uint8Array(ev.data));
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
  const session = snap.session || {};
  setVtlMeta(session);
  setVtlProgress(session);
  updateVtlTiles(session);
  refreshVtlButtons(session.state);
  if (snap.detector) {
    VTL.detectorSnap = snap.detector;
    updateVtlDetectorStatus(snap.detector);
  }
  const loopEl = document.getElementById("vtl-loop");
  if (loopEl && session.loop != null) loopEl.checked = !!session.loop;
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

/** 把整帧画到 overlay canvas 上，再叠 bbox —— 历史缩略图与大图弹窗共用。 */
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

/** 逐框画矩形 + 顶部标签。
 *
 *  box 坐标是**送检帧**（固定 448×252）的像素，多路宫格里画布却是**显示尺寸**
 *  （960 或 384 宽），所以必须传 `sx`/`sy` =「画布尺寸 / 送检帧尺寸」，
 *  不缩放框会全挤在左上角。不传时按 1:1（历史缩略图那条路径本就同尺寸）。 */
function drawVtlBoxes(canvasEl, boxes, palette, sx, sy) {
  if (!canvasEl || !boxes || !boxes.length) return;
  const ctx = canvasEl.getContext("2d");
  if (!ctx) return;
  sx = sx || 1;
  sy = sy || 1;
  // 线宽 / 字号 / 标签框按画布宽度缩放，1080p 以上仍然看得清；
  // 以 960px 为基准，上限 3x 防止 4K/8K 上糊成一片。
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
    const x1 = bb[0] * sx, y1 = bb[1] * sy;
    const x2 = bb[2] * sx, y2 = bb[3] * sy;
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
  // 多路下"哪一路"是定位结果的必要上下文 —— 不标通道，32 路的事件列表没法读。
  const chSpan = document.createElement("span");
  chSpan.className = "tag";
  chSpan.textContent = "通道 " + chNo(ev.channel);
  meta.appendChild(chSpan);
  // 判据分数：区分「超阈值触发」和「兜底间隔触发」。三张卡共用同一份判据，
  // 所以三处都要显示 —— 只给一张卡加就是"同样的配置、不同的信息"。
  const jsSpan = window.FrameJudge?.scoreSpan?.("vtl", ev);
  if (jsSpan) meta.appendChild(jsSpan);
  meta.appendChild(tag);
  li.appendChild(meta);

  const boxes = Array.isArray(ev.boxes) ? ev.boxes : [];

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

    img.addEventListener("load", () => {
      vtlRenderHistoryItem(overlay, img, boxes, VTLDetector.palette);
    });
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
  appendVtlEventLi(ev);
  const count = document.getElementById("vtl-events-count");
  if (count) count.textContent = VTLDetector.events.length + " 条";
}

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
  // 节流已上移到共享判据器，detector 一律 every_n=1（避免两级节流相乘）；
  // 这个 1 同时被后端解读成「秒/路」的轮转间隔。
  const every_n = 1;

  const items = vtlPromptCollectItems();
  if (items.length === 0) {
    setVtlOverlay("至少需要一条非空的定位描述", true);
    return;
  }
  try {
    // 只发原始 `prompts` 条目，前缀与分隔符由后端拼；老的单串 `prompt`
    // 与它互斥（后端强制），所以这里不发。
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

function readVtlSources() {
  const ta = document.getElementById("vtl-sources");
  if (!ta) return [];
  const seen = new Set();
  const out = [];
  for (const raw of (ta.value || "").split(/\r?\n/)) {
    const s = raw.trim();
    if (!s || seen.has(s)) continue;
    seen.add(s);
    out.push(s);
  }
  return out;
}

/** 打开视频的唯一入口 —— 「打开」按钮与向导的 applyWizardSnapshot 共用。
 *
 *  曾踩过的坑（vtd 那边先踩）：向导里写的是 `vtlOpen({ source: opts.source })`，
 *  而 `opts.source` 是**整个多行文本框的内容**。后端 `source`（单数）是老前端的
 *  兼容写法，等价于 `sources: [source]`，于是多路文本被当成**一路带换行的
 *  假路径**，解码必然失败 —— 表现是「向导里填多路，完成配置后自动打开异常」，
 *  而填单路反而正常。正确做法是先按行拆成列表，再走 `sources`（复数）。 */
async function openVtlFromTextarea() {
  const srcs = readVtlSources();
  if (!srcs.length) {
    setVtlOverlay("请在文本框里输入至少一路视频源（每行一路）", true);
    return { ok: false, reason: "empty" };
  }
  if (srcs.length > 32) {
    setVtlOverlay(`最多 32 路，当前 ${srcs.length} 路`, true);
    return { ok: false, reason: "too_many" };
  }
  const loopEl = document.getElementById("vtl-loop");
  const openBtn = document.getElementById("vtl-open");
  if (openBtn) openBtn.disabled = true;
  window.MultiGrid.ensure(GRID_EL, srcs.length);
  setVtlOverlay(`正在打开 ${srcs.length} 路 …`, false);
  try {
    const snap = await vtlOpen({
      sources: srcs,
      loop: !!(loopEl && loopEl.checked),
    });
    const session = snap.session || snap;
    setVtlMeta(session);
    setVtlProgress(session);
    updateVtlTiles(session);
    if (loopEl && session.loop != null) loopEl.checked = !!session.loop;
    return { ok: true, snap };
  } catch (e) {
    setVtlOverlay("打开失败: " + e.message, true);
    return { ok: false, reason: String((e && e.message) || e) };
  } finally {
    if (openBtn) openBtn.disabled = false;
  }
}

function initVideoTargetLocalization() {
  // 判据浮层（角标 + 曲线）锚在**画面容器**上。多路宫格里没有单一
  // canvas，锚到 grid 元素才是对的矩形。
  window.FrameJudge?.mount({ prefix: "vtl", canvasId: GRID_EL,
                             apiBase: API_PREFIX + "/video-target-localization/judge" });
  if (window.__vtlInit) return;
  window.__vtlInit = true;

  const sourceEl = document.getElementById("vtl-sources");
  const loopEl   = document.getElementById("vtl-loop");
  const openBtn = document.getElementById("vtl-open");
  const closeBtn = document.getElementById("vtl-close");
  const browseBtn = document.getElementById("vtl-browse");
  const playBtn = document.getElementById("vtl-play");
  const pauseBtn = document.getElementById("vtl-pause");
  const stopBtn = document.getElementById("vtl-stop");

  if (openBtn) {
    openBtn.addEventListener("click", openVtlFromTextarea);
  }

  // 打开共享文件选择器（不是本页上传：选到的是**服务器所在设备**上的路径）。
  // 目标换成 textarea 后，fs-modal 是**追加一行**而不是整体替换 —— 正好是
  // 多路要的语义。Escape / 点背景关闭由 fs-modal 自己处理。
  if (browseBtn) {
    browseBtn.addEventListener("click", () => {
      window.VdFsModal._sourceTarget = sourceEl;
      window.VdFsModal.open("");
    });
  }

  if (closeBtn) {
    closeBtn.addEventListener("click", async () => {
      try { await vtlClose(); } catch (_) {}
      await vtlStatus();
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

/* ---- 定位提示词行：增删 / 编号 / 收集 ---- */
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

  // 事件委托：一个监听管住当前与后续所有删除按钮
  rowsEl.addEventListener("click", (e) => {
    const btn = e.target.closest(".vtl-prompt-row-remove");
    if (!btn) return;
    const row = btn.closest(".vtl-prompt-row");
    if (!row) return;
    vtlPromptRemoveRow(row);
  });

  addBtn.addEventListener("click", () => vtlPromptAddRow());

  // HTML 里已有 1 行，这里只同步编号与按钮可用态
  vtlPromptRenumber();
  vtlPromptUpdateAddBtn();
}

function vtlPromptAddRow(focus = true) {
  const rowsEl = document.getElementById("vtl-prompt-rows");
  if (!rowsEl) return;
  if (rowsEl.children.length >= VTL_PROMPT_CONST.MAX_ROWS) return;

  const row = document.createElement("div");
  row.className = "vtl-prompt-row";  // 先收拢（grid-template-rows: 0fr, opacity: 0）
  row.innerHTML = `
    <div class="vtl-prompt-row-inner">
      <span class="vtl-prompt-row-num"></span>
      <input type="text" class="vtl-prompt-row-input surv-input" maxlength="200" spellcheck="false" placeholder="例如：穿着红衬衫的人" />
      <button type="button" class="vtl-prompt-row-remove vd-btn vd-btn-mini" title="删除该行" aria-label="删除该行">\u2212</button>
    </div>
  `;
  rowsEl.appendChild(row);

  // 强制回流一次，让浏览器先认下收拢态，再换 is-open 触发过渡动画
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

  // 先收起，等过渡结束再真正摘掉节点（timeout 兜底：transitionend 可能不来）
  rowEl.classList.remove("is-open");
  const cleanup = () => {
    if (rowEl.parentNode) rowEl.parentNode.removeChild(rowEl);
    vtlPromptRenumber();
    vtlPromptUpdateAddBtn();
  };
  let fired = false;
  const onEnd = () => { if (!fired) { fired = true; cleanup(); } };
  rowEl.addEventListener("transitionend", onEnd, { once: true });
  setTimeout(onEnd, 220);
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

  // 只剩 1 行时，所有删除按钮一起禁用
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

initVideoTargetLocalization();

// ------------------------------------------------------------------ *
// 应用向导模式 API — wizard 框架 finish 时调用。
// 契约：opts 由 wizard.json 的 steps[].fields[].ui_key 决定
//   vtl-sources      → opts.source
//   vtl-loop         → opts.loop
//   vtl-prompt-rows  → opts.prompts（string[]，对应 prompt_rows 字段类型，
//                                      每个元素一行定位提示词）
// source 非空 → 写回 #vtl-sources 后调 openVtlFromTextarea；prompts 非空 →
// 写回 #vtl-prompt-rows 再点 #vtl-detector-start。空字段不触发对应动作。
// 全局名约定 ImApp_<app.id 去掉 hyphen>。
// ------------------------------------------------------------------ *

// 把 wizard 给的 prompts 数组同步到应用页的 #vtl-prompt-rows：
// 已有行复用（超出值留空），行数不够时调 vtlPromptAddRow 补齐
// （focus=false 不抢焦点）；不删应用页已有的多余空行。
function _vtlWizardSyncPrompts(prompts) {
  const rowsEl = document.getElementById("vtl-prompt-rows");
  if (!rowsEl || !Array.isArray(prompts) || prompts.length === 0) return false;
  const inputs = () => Array.from(rowsEl.querySelectorAll(".vtl-prompt-row-input"));
  const existing = inputs();
  for (let i = 0; i < prompts.length; i++) {
    if (i < existing.length) existing[i].value = prompts[i];
  }
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

  if (opts.source)      setVal("vtl-sources", opts.source);
  if (opts.loop != null) setChk("vtl-loop", opts.loop);
  // 帧判据 —— 必须在 open 之前落地：open 一开视频，判据已经在解码热路径上跑。
  const judge = await window.FrameJudge?.fromWizard("vtl", opts) ?? null;

  // 向导的 prompt_rows 可能是空串，先滤掉再同步
  let promptsApplied = false;
  if (Array.isArray(opts.prompts)) {
    const cleaned = opts.prompts.map((p) => (p || "").trim()).filter((p) => p.length > 0);
    if (cleaned.length > 0) promptsApplied = _vtlWizardSyncPrompts(cleaned);
  }

  const result = { source_opened: false, detector_started: false, prompts_applied: promptsApplied,
                 judge_applied: !!judge };

  if (opts.source) {
    try {
      // 必须走 openVtlFromTextarea：opts.source 是整个多行文本，单数 key 会被
      // 后端当成一路假路径（坑见 openVtlFromTextarea 的注释）。
      const r = await openVtlFromTextarea();
      if (!r.ok) throw new Error(r.reason || "open failed");
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
