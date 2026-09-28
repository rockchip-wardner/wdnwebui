(function () {
  "use strict";
  const API_PREFIX = "/api";
  // core helpers (fmtNum, fmtEventTime, sanitizeEventText, loadModelPick, vtlPaletteRGB, vtlLabelColor) via app/static/core/*.js
  const fmtNum = window.fmtNum;
  const fmtEventTime = window.fmtEventTime;
  const sanitizeEventText = window.sanitizeEventText;
  const loadModelPick = window.loadModelPick;
  const vtlPaletteRGB = window.vtlPaletteRGB;
  const vtlLabelColor = window.vtlLabelColor;

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
  renderedIds: new Set(),
  eventNodes: new Map(),
  MAX_RENDERED: 50,
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
  // ...
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

function buildVtdEventLi(ev, isLatest) {
  const li = document.createElement("li");
  li.className = "vd-event" + (isLatest ? " latest" : "");
  li.dataset.evId = String(ev.id);
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
    thumb.dataset.src = "data:image/jpeg;base64," + ev.crop_b64;
    body.appendChild(thumb);
  }
  const text = document.createElement("div");
  text.className = "vd-event-text";
  const rawText = (ev.desc && String(ev.desc).trim())
               || (ev.text && String(ev.text).trim())
               || "(空)";
  text.textContent = sanitizeEventText(rawText);
  body.appendChild(text);
  li.appendChild(meta);
  li.appendChild(body);
  return li;
}

const vtdThumbObserver = ("IntersectionObserver" in window)
  ? new IntersectionObserver((entries, obs) => {
      for (const ent of entries) {
        if (ent.isIntersecting) {
          const img = ent.target;
          if (img.dataset.src && !img.src) {
            img.src = img.dataset.src;
            delete img.dataset.src;
          }
          obs.unobserve(img);
        }
      }
    }, { rootMargin: "120px" })
  : null;

function mountVtdThumb(img) {
  if (!img) return;
  if (!img.dataset.src) return;
  if (vtdThumbObserver) {
    vtdThumbObserver.observe(img);
    return;
  }
  if (!img.src) img.src = img.dataset.src;
  delete img.dataset.src;
}

function trimVtdEventList(list) {
  while (VTDDetector.renderedIds.size > VTDDetector.MAX_RENDERED) {
    const oldestId = list.lastElementChild
      ? Number(list.lastElementChild.dataset.evId)
      : null;
    if (oldestId == null || Number.isNaN(oldestId)) break;
    const node = VTDDetector.eventNodes.get(oldestId);
    if (node) {
      try { node.remove(); } catch (_) { /* ignore */ }
    }
    VTDDetector.eventNodes.delete(oldestId);
    VTDDetector.renderedIds.delete(oldestId);
  }
}

function renderVtdEventList() {
  const list = document.getElementById("vtd-event-list");
  const count = document.getElementById("vtd-events-count");
  if (!list || !count) return;
  count.textContent = VTDDetector.events.length + " 条";
  list.innerHTML = "";
  VTDDetector.renderedIds.clear();
  VTDDetector.eventNodes.clear();
  if (VTDDetector.events.length === 0) {
    list.innerHTML =
      '<li class="vd-event-empty">尚未检测到目标，启动检测后结果显示在这里。</li>';
    return;
  }
  const slice = VTDDetector.events.slice(0, VTDDetector.MAX_RENDERED);
  for (const ev of slice) {
    const li = buildVtdEventLi(ev, ev === slice[0]);
    list.appendChild(li);
    VTDDetector.renderedIds.add(ev.id);
    VTDDetector.eventNodes.set(ev.id, li);
    const thumb = li.querySelector("img.vtd-crop-thumb");
    if (thumb) mountVtdThumb(thumb);
  }
}

function appendVtdEventLi(ev) {
  const list = document.getElementById("vtd-event-list");
  if (!list) return;
  const emptyLi = list.querySelector(".vd-event-empty");
  if (emptyLi) {
    list.innerHTML = "";
    VTDDetector.renderedIds.clear();
    VTDDetector.eventNodes.clear();
  }
  const prevLatest = list.querySelector(".vd-event.latest");
  if (prevLatest) prevLatest.classList.remove("latest");
  const li = buildVtdEventLi(ev, true);
  list.insertBefore(li, list.firstChild);
  VTDDetector.renderedIds.add(ev.id);
  VTDDetector.eventNodes.set(ev.id, li);
  const thumb = li.querySelector("img.vtd-crop-thumb");
  if (thumb) mountVtdThumb(thumb);
  trimVtdEventList(list);
}

function addVtdEvent(ev) {
  VTDDetector.events.unshift(ev);
  if (VTDDetector.events.length > 200) VTDDetector.events.length = 200;
  appendVtdEventLi(ev);
  const count = document.getElementById("vtd-events-count");
  if (count) count.textContent = VTDDetector.events.length + " 条";
}
let vtdBadgeHideTimer = null;
function showVtdEventBadge(ev) {
  const badge = document.getElementById("vtd-event-badge");
  const text = document.getElementById("vtd-event-badge-text");
  if (!badge || !text) return;
  // ...
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
let vtdCropLastShownTs = 0;
const VTD_CROP_POPUP_MIN_INTERVAL_MS = 2500;
function showVtdCropPopup(cropB64, w, h) {
  const overlay = document.getElementById("vtd-overlay");
  if (!overlay || !cropB64) return;
  const now = performance.now();
  if (now - vtdCropLastShownTs < VTD_CROP_POPUP_MIN_INTERVAL_MS) {
    return;
  }
  vtdCropLastShownTs = now;
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
  // ...
  const sizeVal = sizeEl?.value || "";
  let image_size = null;
  if (sizeVal && sizeVal !== "auto") {
    image_size = parseInt(sizeVal, 10);
    if (!Number.isFinite(image_size)) {
      setVtdOverlay("检测尺寸值非法: " + sizeVal, true);
      return;
    }
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
    VTDDetector.renderedIds.clear();
    VTDDetector.eventNodes.clear();
    renderVtdEventList();
  } catch (_) {}
}

  // ...

function initVideoTargetDetection() {
  if (window.__vtdInit) return;
  window.__vtdInit = true;

  // ...
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

  // ...
  browseBtn.addEventListener("click", () => {
    window.VdFsModal._sourceTarget = sourceEl;
    window.VdFsModal.open("");
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
  // ...
  if (!VTD.ws && !VTD.wsTimer) connectVtdWs();
}

/* ------------------------------------------------------------------ *
 *  视频目标定位 page (LocateAnything-3b grounding 演示)               *
 * ------------------------------------------------------------------ *
 *   - API 前缀 `/api/video-target-localization/*`                      *
 *   - 后端 detector 直接走 `session.respond(generating=LocatorDoc)`   *
 *     结构化输出拿 `boxes: List[BoundingBox]` (0–1000 归一化) +       *
 *     `text` + `level`，**不**挂任何 `@Callable` 工具                  *
 *   - 后端 detector 实例独立 (`get_localizer_detector()`)，与 VD / VTD  *
 *     互不干扰                                                          *
 *   - 事件：`ev.tool_call === "grounding"` 用于识别多 bbox 定位事件;  *
 *     每事件附 `frame_b64` 整帧 + `boxes[].bbox` 像素坐标 + `label`   *
 *     + `crop_b64` 单独裁图                                            *
 *   - 视觉定位页**不**暴露模型选择 / 尺寸选择（LocateAnything 是单    *
 *     448×448 单变体，单 capability），POST body 只含 `{prompt,        *
 *     every_n}` 两字段                                                  *
 *   - 历史列表项画 bbox overlay（canvas 叠加在原图缩略图上）+ 双击弹   *
 *     `#vtl-frame-modal` 大图预览                                       *
 * ------------------------------------------------------------------ */

initVideoTargetDetection();

})();
