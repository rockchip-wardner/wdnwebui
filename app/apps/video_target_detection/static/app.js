(function () {
  "use strict";
  const API_PREFIX = "/api";
  // core helpers (fmtEventTime, sanitizeEventText, loadModelPick) via app/static/core/*.js
  const fmtEventTime = window.fmtEventTime;
  const sanitizeEventText = window.sanitizeEventText;
  const loadModelPick = window.loadModelPick;

// ---- 诊断计数器（常驻，开销可忽略；供 DevTools / playwright 读取）----
// 宫格/解码侧的计数在 window.MultiGridDiag（app/static/core/multi-grid.js），
// 这里只留本页自己产生的：WS 生命周期 + 二进制帧元数据。
const VTD_DIAG = {
  wsOpen: 0, wsClose: 0, wsError: 0,
  log: [],
};
window.__vtdDiag = VTD_DIAG;
// 画面为空时的提示文案（提示层唯一真相源，避免多处各写一份）
const VTD_EMPTY_HINT = "尚未打开视频\n\n在右侧填入视频地址后点击「打开视频」";
function _diag(ev, extra) {
  if (VTD_DIAG.log.length > 200) VTD_DIAG.log.shift();
  VTD_DIAG.log.push(extra ? ev + " " + extra : ev);
}

const VTD = {
  ws: null,
  wsRetry: 0,
  wsTimer: null,
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

// 宫格实现（建格 / 分列 / 每路单飞解码上屏 / 角标高亮）在共享组件
// multi-grid.js 里，vd / vtl 共用同一份；本页只负责喂帧和渲染事件列表。
const GRID_EL = "vtd-grid";
const PICK_ID = "vtd-model-pick";
const chNo = window.MultiGrid.chNo;

function setVtdOverlay(text, isError) {
  const el = document.getElementById("vtd-overlay");
  if (!el) return;
  el.textContent = text || "";
  el.classList.toggle("error", !!isError);
  el.style.display = text ? "" : "none";
}

function updateVtdTiles(session) {
  if (!session) return;
  const list = session.channel_list || [];
  const vlm = session.vlm || {};
  const { total, ok, eof } = window.MultiGrid.status(
    GRID_EL, list, window.MultiCard.activeChannels(vlm));
  const hint = document.getElementById("vtd-channel-hint");
  const pill = document.getElementById("vtd-pill-video");
  if (hint) {
    // 面向终端客户：只说"几路、通没通"，不暴露分辨率 / 帧率等技术细节
    if (!total) {
      hint.textContent = "尚未打开视频";
      if (pill) pill.dataset.state = "idle";
    } else if (ok === total) {
      hint.textContent = `${total} 路视频 · 全部正常`;
      if (pill) pill.dataset.state = "ok";
    } else {
      hint.textContent = `${total} 路视频 · ${ok} 路正常`;
      if (pill) pill.dataset.state = "warn";
    }
  }
  // 提示层。文件源播完（loop 未勾）会停在 eof 不再产帧，画面定格在最后一帧 ——
  // 不说的话就是「画面卡住 + 提示条报 0 路正常」，用户完全无从判断。
  if (total && eof === total) setVtdOverlay("视频已播完\n\n勾选「循环播放」可自动重播，或点 播放 继续", false);
  else if (eof) setVtdOverlay(`${eof} 路视频已播完`, false);
  else if (total && ok === total) setVtdOverlay("", false);
  else if (total) setVtdOverlay("部分视频尚未出画面…", false);
  else setVtdOverlay(VTD_EMPTY_HINT, false);
  window.ZoomFrame?.tick(GRID_EL, session, total);
}

// 宫格放大的接线。attach 必须在 IIFE 内：onError 要用私有的 setVtdOverlay。
window.ZoomFrame?.attach({
  gridEl: GRID_EL,
  apiBase: API_PREFIX + "/video-target-detection",
  onError: (msg) => setVtdOverlay(msg, true),
});

function connectVtdWs() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}${API_PREFIX}/video-target-detection/ws`;
  const ws = new WebSocket(url);
  ws.binaryType = "arraybuffer";
  VTD.ws = ws;

  ws.onopen = () => {
    VTD.wsRetry = 0;
    VTD_DIAG.wsOpen++;
    _diag("ws", "open " + url);
    console.log("[vtd] WS open", url);
    // 注意：**不要**在这里清空提示层。WS 一连上就会来状态快照，
    // 若此时还没打开视频，清空会把空状态提示吃掉、画面只剩一片黑。
    // 提示层由 updateVtdTiles 统一按"有没有路出画面"来维护。
  };
  ws.onmessage = async (ev) => {
    if (typeof ev.data === "string") {
      let snap;
      try { snap = JSON.parse(ev.data); } catch (_) { return; }
      if (snap.type === "event_history") {
        // 连接建立时服务端**一次性**打包发来的历史事件。必须走"整体重建"，
        // 不能逐条 addVtdEvent —— 实测 200 条突发能把渲染进程打满一个核、
        // 页面直接卡死。服务端按 id 升序（旧→新）发，本地数组是新→旧，
        // 所以要反转。
        const evs = Array.isArray(snap.events) ? snap.events : [];
        VTDDetector.events = evs.slice().reverse();
        VTDDetector.renderedIds.clear();
        VTDDetector.eventNodes.clear();
        renderVtdEventList();
        return;
      } else if (snap.type === "event") {
        addVtdEvent(snap);
        showVtdEventBadge(snap);
        if (snap.tool_call === "target_detection" && snap.crop_b64) {
          showVtdCropPopup(snap.crop_b64, snap.crop_w, snap.crop_h);
        }
        return;
      } else if (snap.type === "status" && snap.detector) {
        updateVtdDetectorStatus(snap.detector, snap.session);
      }
      if (snap.session) {
        updateVtdTiles(snap.session);
        VTD.lastSnap = snap.session;
        // 判据快照挂在 session 下（归 MultiChannelSession 管），不是 snap.judge ——
        // WS tick 的形状是 {session, detector}。
        window.FrameJudge?.update("vtd", snap.session.judge);
        if (typeof DemoGuard !== "undefined") {
          DemoGuard.report("video-target-detection",
                           { session: snap.session, detector: snap.detector });
        }
      }
      const payload = document.getElementById("data-video-target-detection");
      if (payload) payload.textContent = JSON.stringify(snap, null, 2);
      return;
    }
    try {
      // 通道号在二进制头第 1 字节（见 multi_api 的 stream_ws）
      const idx = ev.data.byteLength ? new Uint8Array(ev.data, 0, 1)[0] : 0;
      window.MultiGrid.paint(GRID_EL, idx, new Uint8Array(ev.data, 1));
    } catch (e) {
      window.MultiGridDiag.drawErr++;
      console.error("[vtd] draw 异常", e);
      setVtdOverlay("帧渲染失败: " + e, true);
    }
  };
  ws.onclose = (e) => {
    VTD_DIAG.wsClose++;
    _diag("ws", `close code=${e && e.code}`);
    console.warn("[vtd] WS closed", e && e.code);
    VTD.ws = null;
    const wait = Math.min(5000, 500 * Math.pow(2, VTD.wsRetry++));
    VTD.wsTimer = setTimeout(connectVtdWs, wait);
  };
  ws.onerror = () => {
    try { ws.close(); } catch (_) {}
  };
}

function readVtdSources() {
  const ta = document.getElementById("vtd-sources");
  if (!ta) return [];
  const out = [];
  for (const line of ta.value.split(/\r?\n/)) {
    const s = line.trim();
    if (!s) continue;
    out.push(s);
  }
  return out;
}

/** 读文本框 → 校验 → 建宫格 → 调后端打开。返回 {ok, snap} 或 {ok:false, reason}。
 *
 *  **「打开视频」按钮和向导的 applyWizardSnapshot 必须共用这一条路径。**
 *
 *  曾踩的坑：向导那边写的是
 *      await vtdOpen({ source: opts.source, loop });
 *  而 `opts.source` 是**整个多行文本框的内容**。后端 `source`（单数）是
 *  老前端的兼容写法，等价于 `sources: [source]`，于是多路文本被当成
 *  **一路带换行的假路径**，解码必然失败 —— 表现就是「向导里填多路，
 *  完成配置后自动打开视频异常」，而填单路反而正常。
 *  正确做法是先按行拆成列表，再走 `sources`（复数）。
 */
async function openVtdFromTextarea() {
  const srcs = readVtdSources();
  if (!srcs.length) {
    setVtdOverlay("请在文本框里输入至少一路视频源（每行一路）", true);
    return { ok: false, reason: "empty" };
  }
  if (srcs.length > 32) {
    setVtdOverlay(`最多 32 路，当前 ${srcs.length} 路`, true);
    return { ok: false, reason: "too_many" };
  }
  const loopEl = document.getElementById("vtd-loop");
  const openBtn = document.getElementById("vtd-open");
  if (openBtn) openBtn.disabled = true;
  window.MultiGrid.ensure(GRID_EL, srcs.length);
  setVtdOverlay(`正在打开 ${srcs.length} 路 …`, false);
  try {
    const snap = await vtdOpen({ sources: srcs, loop: !!(loopEl && loopEl.checked) });
    updateVtdTiles(snap);
    if (loopEl && snap.loop != null) loopEl.checked = !!snap.loop;
    // open() 成功 ≠ 已有画面：这里只把文案换成"等首帧"，真正清空交给 WS
    // 首帧分支（见 connectVtdWs 的二进制帧分支）。
    const nOpen = (snap.channel_list || []).filter((c) => c.has_frame).length;
    setVtdOverlay(nOpen ? "" : `已启动 ${srcs.length} 路解码，等待首帧…`, false);
    return { ok: true, snap };
  } catch (e) {
    setVtdOverlay("打开失败: " + e.message, true);
    return { ok: false, reason: String(e && e.message || e) };
  } finally {
    if (openBtn) openBtn.disabled = false;
  }
}

async function vtdClose() {
  const r = await fetch(API_PREFIX + "/video-target-detection/close",
                        { method: "POST" });
  return r.ok ? r.json() : null;
}

/** 拉一次全量状态并刷新 UI。返回体是 ``{session, detector}``。 */
async function vtdStatus() {
  const r = await fetch(API_PREFIX + "/video-target-detection",
                        { cache: "no-store" });
  if (!r.ok) return null;
  const snap = await r.json();
  updateVtdDetectorStatus(snap.detector || {}, snap.session);
  updateVtdTiles(snap.session);
  VTD.lastSnap = snap.session || null;
  const loopEl = document.getElementById("vtd-loop");
  if (loopEl && snap.session && snap.session.loop != null) {
    loopEl.checked = !!snap.session.loop;
  }
  const payload = document.getElementById("data-video-target-detection");
  if (payload) payload.textContent = JSON.stringify(snap, null, 2);
  return snap;
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

function buildVtdEventLi(ev, isLatest) {
  const li = document.createElement("li");
  li.className = "vd-event" + (isLatest ? " latest" : "");
  li.dataset.evId = String(ev.id);
  // 给客户看的是"通道 N"，不是内部 ch 索引；帧号 / 事件自增 ID 不展示。
  const chMeta = document.createElement("span");
  chMeta.className = "vd-event-ch";
  chMeta.textContent = "通道 " + chNo(ev.channel);
  li.appendChild(chMeta);
  const meta = document.createElement("div");
  meta.className = "vd-event-meta";
  const tsSpan = document.createElement("span");
  tsSpan.textContent = fmtEventTime(ev.ts);
  meta.appendChild(tsSpan);
  const npuSpan = window.MultiCard.npuBadge(ev, VTD.detectorSnap, PICK_ID);
  if (npuSpan) meta.appendChild(npuSpan);
  // 判据分数（共享组件算，多路下没有 judge_score 就整块不渲染）
  const jsSpan = window.FrameJudge?.scoreSpan?.("vtd", ev);
  if (jsSpan) meta.appendChild(jsSpan);
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
function updateVtdDetectorStatus(snap, session) {
  const statusEl = document.getElementById("vtd-detector-status");
  const pillEl   = document.getElementById("vtd-pill-det");
  const startBtn = document.getElementById("vtd-detector-start");
  const stopBtn  = document.getElementById("vtd-detector-stop");
  if (!snap || !statusEl) return;
  const chCount = ((session && session.channel_list) || []).length;
  const setState = (s) => { if (pillEl) pillEl.dataset.state = s; };
  if (!snap.available) {
    statusEl.textContent = "模型不可用";
    setState("error");
  } else if (snap.init_error) {
    statusEl.textContent = "模型加载失败";
    setState("error");
    console.warn("[vtd] 模型加载失败:", snap.init_error);
  } else if (snap.enabled && snap.session_ready) {
    // 面向终端客户：只说"在跑 / 正在看几路"，不暴露队列、尺寸、轮次等内部状态
    const slots = snap.slot_count > 1 ? `（${snap.slot_count} 张卡并行）` : "";
    statusEl.textContent = chCount
      ? `检测中 · 正在巡检 ${chCount} 路${slots}`
      : "检测中 · 等待视频";
    setState(chCount ? "run" : "warn");
  } else if (snap.enabled) {
    statusEl.textContent = "正在加载模型…";
    setState("warn");
  } else {
    statusEl.textContent = "检测未启动";
    setState("idle");
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
  // 「轮转间隔」语义是**秒/路**（允许 0 = 上一路结束立刻轮下一路），
  // 所以走 rr_interval，不再当 every_n 发 —— every_n 在多路页被后端
  // 强制为 1，若照发会把 0 判成非法（every_n 必须 >= 1）直接 400。
  const rr_interval = parseInt(everyEl?.value || "0", 10);
  if (!prompt) {
    setVtdOverlay("请先填写检测提示词", true);
    return;
  }
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
    const models = window.MultiCard.selectedModels(PICK_ID);
    if (!models) {
      setVtdOverlay("请先勾选至少一个模型实例", true);
      return;
    }
    const r = await fetch(API_PREFIX + "/video-target-detection/prompt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        prompt,
        rr_interval: Number.isFinite(rr_interval) ? rr_interval : 0,
        image_size: Number.isFinite(image_size) ? image_size : null,
        models: models,
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

function initVideoTargetDetection() {
  if (window.__vtdInit) return;
  window.__vtdInit = true;

  // 空状态：一进页面画面区是黑的，给一句人话告诉用户下一步做什么
  setVtdOverlay(VTD_EMPTY_HINT, false);

  loadInstancePickMulti(PICK_ID, "swcap_video_describe", "vtd.instances");
  // apiBase 指向本卡判据端点：/api/video/judge 操作的是单例 VideoSession
  // （vd / vtl 共用），本页走 MultiChannelSession，同 VR 挂自己的 judge_api。
  window.FrameJudge?.mount({ prefix: "vtd", canvasId: GRID_EL,
                             apiBase: API_PREFIX + "/video-target-detection/judge" });

  const loopEl   = document.getElementById("vtd-loop");
  const openBtn = document.getElementById("vtd-open");
  const closeBtn = document.getElementById("vtd-close");

  openBtn.addEventListener("click", () => openVtdFromTextarea());

  closeBtn.addEventListener("click", async () => {
    try { await vtdClose(); } catch (_) {}
    await vtdStatus();
    setVtdOverlay("已关闭", false);
  });

  const detStartBtn = document.getElementById("vtd-detector-start");
  const detStopBtn  = document.getElementById("vtd-detector-stop");
  const evClearBtn  = document.getElementById("vtd-events-clear");
  if (detStartBtn) detStartBtn.addEventListener("click", vtdDetectorStart);
  if (detStopBtn)  detStopBtn.addEventListener("click",  vtdDetectorStop);
  if (evClearBtn)  evClearBtn.addEventListener("click",  vtdEventsClear);

  refreshVtdDetector();
  if (!VTD.ws && !VTD.wsTimer) connectVtdWs();
}

initVideoTargetDetection();

// ------------------------------------------------------------------ *
// 应用向导模式 API — wizard 框架 finish 时调用。
// 契约：opts 由 wizard.json 的 steps[].fields[].ui_key 决定
//   vtd-sources      → opts.source
//   vtd-loop         → opts.loop
//   vtd-prompt       → opts.prompt
//   vtd-detect-size  → opts.detectSize
// source 非空 → 写回 #vtd-sources 后按行拆成多路再打开；prompt 非空 →
// 模拟点 #vtd-detector-start。空字段不触发对应动作。
// 全局名约定 ImApp_<app.id 去掉 hyphen>。
// ------------------------------------------------------------------ *
window.ImApp_video_target_detection = window.ImApp_video_target_detection || {};
window.ImApp_video_target_detection.applyWizardSnapshot = async function (opts) {
  const setVal = (id, v) => { const el = document.getElementById(id); if (el != null) el.value = v; };
  const setChk = (id, v) => { const el = document.getElementById(id); if (el != null) el.checked = !!v; };

  if (opts.source)      setVal("vtd-sources", opts.source);
  if (opts.loop != null) setChk("vtd-loop", opts.loop);
  if (opts.prompt)      setVal("vtd-prompt", opts.prompt);
  if (opts.detectSize)  setVal("vtd-detect-size", opts.detectSize);
  // 帧判据 —— 必须在 open 之前落地：open 一开视频，判据已经在解码热路径上跑。
  const judge = await window.FrameJudge?.fromWizard("vtd", opts) ?? null;

  const result = { source_opened: false, detector_started: false, judge_applied: !!judge };

  if (opts.source) {
    // 必须走 openVtdFromTextarea：opts.source 是整个多行文本，单数 key 会被
    // 后端当成一路假路径（坑见 openVtdFromTextarea 的注释）。
    const r = await openVtdFromTextarea();
    if (!r.ok) {
      throw new Error("WizardSnapshotError:open_source_failed:" + (r.reason || "unknown"));
    }
    result.source_opened = true;
    result.channel_count = (r.snap && r.snap.channel_list || []).length;
  }

  if (opts.prompt) {
    // 必须先重拉勾选器：页面初始化时模型还没起，列表是空的，直接点会因
    // "一个都没勾"静默中止（向导 finish 的顺序是「等模型 ready → 这里」）。
    try {
      await window.loadInstancePickMulti?.(PICK_ID, "swcap_video_describe",
                                           "vtd.instances");
    } catch (e) {
      throw new Error("WizardSnapshotError:detector_start_failed:" + (e?.message || e));
    }
    const btn = document.getElementById("vtd-detector-start");
    if (btn) {
      try { btn.click(); result.detector_started = true; }
      catch (e) { throw new Error("WizardSnapshotError:detector_start_failed:" + (e?.message || e)); }
    }
  }

  return result;
};

})();

/* v0.23 多路判据 UI 接线 */
function _paintSelectedChannelUi(pfx, idx, per_channel) {
  const idxEl = document.getElementById(pfx + "-judge-selected-idx");
  const noEl = document.getElementById(pfx + "-judge-selected-no");
  const modeEl = document.getElementById(pfx + "-judge-selected-mode");
  const thrEl = document.getElementById(pfx + "-judge-selected-thr");
  const everyEl = document.getElementById(pfx + "-judge-selected-every-n");
  if (!idxEl) return;
  if (idx == null) {
    idxEl.textContent = "--";
    if (noEl) noEl.textContent = "--";
    if (modeEl) modeEl.textContent = "--";
    if (thrEl) thrEl.textContent = "--";
    if (everyEl) everyEl.textContent = "--";
    return;
  }
  // [x, y] 坐标写法：x = 0 基 idx（程序员语境），y = 1 基通道号（用户语境）。
  // 把 x 加粗是因为"哪个 channel"是个常被复述的索引。
  idxEl.textContent = "[" + idx + ", " + (idx + 1) + "]";
  if (noEl) noEl.textContent = String(idx + 1);
  const per = (per_channel || []).find(p => p.channel === idx);
  if (per) {
    if (modeEl) modeEl.textContent = per.mode || "--";
    if (thrEl) thrEl.textContent = per.threshold != null ? (+per.threshold).toFixed(2) : "--";
    if (everyEl) everyEl.textContent = per.every_n != null ? "每 " + per.every_n + " 帧" : "--";
  } else {
    if (modeEl) modeEl.textContent = "--";
    if (thrEl) thrEl.textContent = "--";
    if (everyEl) everyEl.textContent = "--";
  }
}

/**
 * 取当前卡片对应的 judge API base。vd / vtd / vtl 各自一条 endpoint。
 * 卡片 side 跟后端路由前缀对齐——见 app/__init__.py 的 include_router。
 */
function _judgeApiFor(pfx) {
  if (pfx === "vd") return "/api/video-detection";
  if (pfx === "vtd") return "/api/video-target-detection";
  if (pfx === "vtl") return "/api/video-target-localization";
  return null;
}

function _bindCardPerChannelJudge(pfx, gridId) {
  const MG = window.MultiGrid;
  if (!MG) return;
  const apiBase = _judgeApiFor(pfx);
  MG.bindSelect(gridId, async (idx) => {
    // 必须在 setSelectedChannel 之前：它同步写 .selected，之后再判二次点击恒真
    window.ZoomFrame?.onTileClick(gridId, idx);
    if (window.FrameJudge) window.FrameJudge.setSelectedChannel(pfx, idx);
    // **不要等下一条 WS tick**：用户点了 tile 就想立刻看见这路当前的
    // mode / threshold / every_n —— 否则"我改的是哪路"的反馈要拖 1 秒，
    // 体感是判据栏"反应迟钝"。直接 GET /judge 拿最新 per_channel 填 UI。
    if (idx == null || !apiBase) {
      _paintSelectedChannelUi(pfx, idx, null);
      return;
    }
    let per_channel = null;
    try {
      const j = await fetch(apiBase + "/judge", { cache: "no-store" })
                    .then(r => r.json());
      per_channel = j.per_channel || [];
      _paintSelectedChannelUi(pfx, idx, per_channel);
    } catch (_) {
      _paintSelectedChannelUi(pfx, idx, null);
    }
    // 立刻把选中路的当前值推到 selMode/inpThr/inpEvery —— 等 WS tick
    // 要 ~1 秒，且若 _awaitSync 门闩或 activeElement 焦点命中 update()
    // 路径会**根本不推**。tile click 是"我要切这路"最强的信号，不能让
    // 控件卡住。setConfig 只写控件显示、不发 POST，所以"看一眼不动"的
    // 查看意图不会被误当成"修改提交"。
    if (per_channel && window.FrameJudge) {
      const per = per_channel.find((p) => p.channel === idx);
      if (per && per.mode != null) {
        window.FrameJudge.setConfig(pfx, {
          mode: per.mode, every_n: per.every_n, threshold: per.threshold,
        });
        // **同步写到 wizard 内的同名字段**。FrameJudge.setConfig 只动功能页
        // 那组 (selMode/inpThr/inpEvery)，但 wizard source step 里也有
        // ``id="<pfx>-judge-mode/-judge-threshold/-judge-every-n"`` 的
        // 独立 <input>/<select>（wizard state.js 渲染）—— 用户在向导里点
        // tile 看不到任何变化就是因为这组是孤儿。仅在 wizard 容器存在时
        // 同步写一份 DOM value；不动 wizard 的 state.values（避免覆盖
        // 用户手填的字段值，wizard 「完成」会按 state.values 推后端）。
        const wizRoot = document.getElementById("wd-wizard-root-" +
          (pfx === "vd" ? "video-detection"
           : pfx === "vtd" ? "video-target-detection"
           : "video-target-localization"));
        if (wizRoot) {
          // wizard state.js 创建字段元素时只设 class 不设 id，定位靠
          // el.dataset.fieldId（state.js 末尾赋的，匹配 wizard.json 的
          // field.id）。下面三个 query 跨 step 通用：source step 把这些
          // 字段排在「视频源」下、detect / audio / … step 也可能用到。
          const wizMode = wizRoot.querySelector(
            "select[data-field-id=\"" + pfx + "-judge-mode\"]");
          const wizThr  = wizRoot.querySelector(
            "input[data-field-id=\"" + pfx + "-judge-threshold\"]");
          const wizEv   = wizRoot.querySelector(
            "input[data-field-id=\"" + pfx + "-judge-every-n\"]");
          if (wizMode && per.mode != null) {
            if (![...wizMode.options].some((o) => o.value === per.mode)) {
              const o = document.createElement("option");
              o.value = per.mode; o.textContent = per.mode;
              wizMode.appendChild(o);
            }
            wizMode.value = per.mode;
          }
          if (wizThr && per.threshold != null) wizThr.value = String(per.threshold);
          if (wizEv  && per.every_n  != null) wizEv.value  = String(per.every_n);
        }
      }
    }
  });
  _paintSelectedChannelUi(pfx, null, null);
}
_bindCardPerChannelJudge("vtd", "vtd-grid");

