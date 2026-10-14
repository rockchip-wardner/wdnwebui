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

// ---- 诊断计数器（常驻，开销可忽略；供 DevTools / playwright 读取）----
const VTD_DIAG = {
  wsOpen: 0, wsClose: 0, wsError: 0,
  frameMeta: 0, binFrames: 0, decoded: 0,
  drawNoTile: 0, drawEmpty: 0, drawErr: 0,
  gridBuilds: 0, lastDrawIdx: -1,
  // decoded=解码并画上 canvas 的帧数，busyDrop=因上一帧在解码而丢弃的帧数，
  // decodeMs=最近一次解码耗时。仅供 DevTools / 自动化测试，不上界面。
  busyDrop: 0, decodeMs: 0,
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
  // 多路宫格：idx -> {tile, img, label}
  tiles: new Map(),
  cols: 1,
  pendingIdx: -1,   // 下一条二进制帧属于哪一路（由 frame_meta 设定）
  total: 0,
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

/** 按通道数调整宫格（1~32 格）。列数按常见监视器比例取。
 *
 *  刻意**增量复用**而不是整格重建：换源 / 改通道数时若 innerHTML="" 全清，
 *  已加载的 <img> 会连同 src 一起被丢掉，所有格子退回空白，直到下一帧
 *  （display_fps 默认 2 → 最长 ~500ms 空白）才重绘。复用已有 tile 可以让
 *  索引不变的路（换源最常见的情况）画面**完全不中断**。 */
function ensureGrid(n) {
  const grid = document.getElementById("vtd-grid");
  if (!grid) return;
  // 注意：grid 里除了瓦片还有提示层 #vtd-overlay（绝对定位，不参与网格布局），
  // 所以子节点数**不能**用来数瓦片，必须只数 .vd-tile。
  const domTiles = grid.querySelectorAll(".vd-tile").length;
  if (VTD.tiles.size === n && domTiles === n) {
    VTD.total = n;
    return;
  }
  const cols = n <= 1 ? 1 : n <= 4 ? 2 : n <= 9 ? 3 : n <= 16 ? 4 : n <= 25 ? 5 : 6;
  // 判据取 **DOM 上的实际生效值**，不能用 JS 侧的 VTD.cols：后者初值是 1，
  // 而 1 路时算出的 cols 也是 1 → 条件恒假 → gridTemplateColumns 从没被
  // 设过，grid 退回默认 auto 布局，单列被图片的固有宽度（384px）撑开，
  // 1 路时只占播放区一小块（实测 1102px 宽的播放区里只用了 386px）。
  // 6 路时 cols 从 1 变 3 才会设置 —— 所以只有「恰好 1 路」时出错。
  const colsCss = `repeat(${cols}, 1fr)`;
  if (grid.style.gridTemplateColumns !== colsCss) {
    VTD_DIAG.gridBuilds++;
    VTD.cols = cols;
    grid.style.gridTemplateColumns = colsCss;
    grid.dataset.cols = String(cols);
  }
  // 别在这里给 grid 设 aspect-ratio 想让格子凑成 16:9：**不生效**。
  // .vd-layout 是 `align-items: stretch`，.vd-stage-col 是 flex column，
  // grid 作为 flex 子项会被拉满行高（实测 1 路 1002x586 而不是 16:9 的 564），
  // 行内 aspect-ratio 直接被覆盖。
  // 结论：格子高度由 stage 决定、格子宽高比 = (stage 宽/cols) : (stage 高/rows)，
  // 只有 1 / 4 / 9 / 16 / 25 路（cols==rows）才正好是 16:9。6 路是 3x2，
  // 必然有上下留边 —— 这是格子数的固有结果，不是 bug，画面交给
  // object-fit: contain 居中显示即可。
  // 多出来的格子直接删掉（连同它的 blob URL，避免泄漏）
  for (const [i, t] of VTD.tiles) {
    if (i < n) continue;
    t.dropped = true;          // 让在途解码自己退出，不再往已摘掉的 tile 上画
    t.tile.remove();
    VTD.tiles.delete(i);
  }
  let created = 0;
  for (let i = 0; i < n; i++) {
    if (VTD.tiles.has(i)) continue;      // 复用，保留已加载的画面
    const tile = document.createElement("div");
    tile.className = "vd-tile";
    tile.dataset.idx = String(i);
    const cv = document.createElement("canvas");
    const label = document.createElement("div");
    label.className = "vd-tile-label";
    label.textContent = "#" + i;
    const badge = document.createElement("div");
    badge.className = "vd-tile-badge";
    tile.appendChild(cv);
    tile.appendChild(label);
    tile.appendChild(badge);
    grid.appendChild(tile);
    VTD.tiles.set(i, {
      tile, canvas: cv, ctx: cv.getContext("2d", { alpha: false }),
      label, badge,
      busy: false,        // 是否有一个解码在飞
      pending: null,      // 在途期间攒下的最新一帧（旧的直接丢）
      dropped: false,     // 格子已被移除
      w: 0, h: 0,
    });
    created++;
  }
  // 补齐顺序（复用的格子已在原位，新增的追加在后面）
  const ordered = grid.querySelectorAll(".vd-tile");
  if (ordered.length !== n || ordered[0]?.dataset.idx !== "0") {
    for (let i = 0; i < n; i++) {
      const t = VTD.tiles.get(i);
      if (t) grid.appendChild(t.tile);
    }
    VTD_DIAG.gridBuilds++;
  }
  VTD.total = n;
  _diag("grid", `build n=${n} cols=${cols} new=${created}`);
}

/** 把一路的 JPEG 画进对应瓦片。
 *
 *  为什么是 canvas + createImageBitmap，而不是 `<img>` + blob URL：
 *
 *  `<img>` 方案每帧都要 `img.src = URL.createObjectURL(blob)`。换 src 会
 *  **掐断上一帧还在进行的加载**（浏览器要走一遍 URL loader → 解码 → 上传
 *  纹理）。帧小的时候（384×216 ≈ 12KB）这整套在 40ms 预算里轻松跑完，
 *  看不出问题；帧大的时候（960×540 ≈ 65KB，实测 25fps 就是每帧 40ms 的
 *  硬预算）就有一批加载被下一次赋值中途取消，表现为**画面疯狂闪**——
 *  与「<5 路才闪」的现象完全吻合：1~4 路显示 960×540，5 路以上回落 384×216。
 *
 *  现在改成：createImageBitmap 在**解码线程**上解，不经过 URL loader，
 *  不存在「换源掐断」；并且**每路同时只允许一个解码在飞**，在途期间到达
 *  的新帧直接丢掉（视频语义本来就允许丢帧）。25fps 供 24fps 取，丢的是
 *  那一帧而不是整帧的连贯性。
 */
function drawVtdFrame(idx, jpgBytes) {
  VTD_DIAG.binFrames++;
  VTD_DIAG.lastDrawIdx = idx;
  if (!jpgBytes || !jpgBytes.byteLength) {
    VTD_DIAG.drawEmpty++;
    return;
  }
  const t = VTD.tiles.get(idx);
  if (!t) {
    VTD_DIAG.drawNoTile++;
    _diag("draw-no-tile", `idx=${idx} have=${VTD.tiles.size}`);
    return;
  }
  // 只保留最新一帧：pending 已被占用说明上一帧还没开始解码，直接覆盖丢弃
  if (t.busy) {
    VTD_DIAG.busyDrop++;
    t.pending = jpgBytes;
    return;
  }
  _vtdDecode(t, jpgBytes);
}

async function _vtdDecode(t, jpgBytes) {
  t.busy = true;
  const t0 = performance.now();
  try {
    const bmp = await createImageBitmap(
      new Blob([jpgBytes], { type: "image/jpeg" }));
    if (!t.dropped) {
      // canvas 尺寸只在真的变了时才写：每帧写 canvas.width 会清空画布并
      // 重置 ctx 状态，是老实现里一个实打实的每帧浪费。
      if (t.canvas.width !== bmp.width || t.canvas.height !== bmp.height) {
        t.canvas.width = bmp.width;
        t.canvas.height = bmp.height;
        t.w = bmp.width; t.h = bmp.height;
      }
      t.ctx.drawImage(bmp, 0, 0);
      VTD_DIAG.decoded++;
    }
    bmp.close();
  } catch (e) {
    VTD_DIAG.drawErr++;
    console.warn("[vtd] 解码失败", e);
  } finally {
    VTD_DIAG.decodeMs = Math.round(performance.now() - t0);
    t.busy = false;
    // 在途期间攒下的最新一帧，接着解（丢掉它之前更旧的那些）
    const next = t.pending;
    t.pending = null;
    if (next && !t.dropped) _vtdDecode(t, next);
  }
}

/** 1Hz 状态刷新：每格显示 fps / 状态，并高亮当前轮到推理的那一路。 */
function updateVtdTiles(session) {
  if (!session) return;
  const list = session.channel_list || [];
  if (list.length !== VTD.total) ensureGrid(list.length);
  const vlm = session.vlm || {};
  const cur = typeof vlm.current === "number" ? vlm.current : -1;
  let ok = 0, total = 0;
  for (const c of list) {
    const t = VTD.tiles.get(c.idx);
    if (!t) continue;
    total++;
    if (c.has_frame && c.state === "playing") ok++;
    const src = (c.source || "").split("/").pop() || c.source || "";
    // 正常时角标只显示通道号 —— 帧率是运维信息，不是客户要看的；
    // 只有出错时才补上状态/告警，客户一眼能看出哪一路有问题。
    t.label.textContent =
      `#${c.idx}` +
      (c.state !== "playing" ? ` · ${c.state}` : "") +
      (c.errors ? ` · ⚠${c.errors}` : "");
    t.label.title = src;
    t.tile.classList.toggle("active", c.idx === cur);
    t.tile.classList.toggle("bad", !!c.error || c.errors > 0);
    t.badge.textContent = c.idx === cur ? "推理中" : "";
  }
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
  // 提示层统一在这里维护：有路全通就清掉；还没路就回到空状态提示
  if (total && ok === total) setVtdOverlay("", false);
  else if (total) setVtdOverlay("部分视频尚未出画面…", false);
  else setVtdOverlay(VTD_EMPTY_HINT, false);
}


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
      if (snap.type === "frame_meta") {
        // 多路协议：二进制帧前先来一条 meta 说明这帧属于哪一路
        VTD.pendingIdx = snap.idx;
        VTD_DIAG.frameMeta++;
        return;
      } else if (snap.type === "event_history") {
        // 连接建立时服务端**一次性**打包发来的历史事件。
        // 关键：必须走"整体重建"，不能逐条 addVtdEvent —— 逐条会做
        // N 次 appendVtdEventLi（建 DOM + insertBefore + trim），
        // 实测 200 条突发能把渲染进程打满一个核、页面直接卡死。
        // 服务端按 id 升序（旧→新）发，本地数组是新→旧，所以要反转。
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
        if (typeof DemoGuard !== "undefined") {
          DemoGuard.report("video-target-detection",
                           { session: snap.session, detector: snap.detector });
        }
      }
  window.FrameJudge?.update("vtd", snap.judge);
      const payload = document.getElementById("data-video-target-detection");
      if (payload) payload.textContent = JSON.stringify(snap, null, 2);
      return;
    }
    try {
      drawVtdFrame(VTD.pendingIdx >= 0 ? VTD.pendingIdx : 0,
                   new Uint8Array(ev.data));
      // 首帧落地 = 会话真的开始出画面了，清掉「正在打开 N 路 …」。
      // open() 成功只代表解码线程已启动（视频还没解出第一帧），所以这条
      // 是让用户确认"画面出来了"的唯一信号。
      if (VTD.pendingIdx >= 0) setVtdOverlay("", false);
    } catch (e) {
      VTD_DIAG.drawErr++;
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

/** 从「视频源」文本框读出 sources 列表（每行一路，去空行去重）。 */
function readVtdSources() {
  const ta = document.getElementById("vtd-sources");
  if (!ta) return [];
  const seen = new Set();
  const out = [];
  for (const line of ta.value.split(/\r?\n/)) {
    const s = line.trim();
    if (!s || seen.has(s)) continue;
    seen.add(s);
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
  ensureGrid(srcs.length);
  setVtdOverlay(`正在打开 ${srcs.length} 路 …`, false);
  try {
    const snap = await vtdOpen({ sources: srcs, loop: !!(loopEl && loopEl.checked) });
    updateVtdTiles(snap);
    if (loopEl && snap.loop != null) loopEl.checked = !!snap.loop;
    // open() 只负责起解码线程；这里先撤掉「正在打开」文案换成"等首帧"，
    // 等第一帧真的到了（WS 分支）才彻底清空。
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
  chMeta.textContent = "通道 " + ((ev.channel || 0) + 1);
  li.appendChild(chMeta);
  const meta = document.createElement("div");
  meta.className = "vd-event-meta";
  const tsSpan = document.createElement("span");
  tsSpan.textContent = fmtEventTime(ev.ts);
  meta.appendChild(tsSpan);
  // 判据分数（共享组件，多路下 judge_score 为空则不渲染）
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
function updateVtdDetectorStatus(snap, session) {
  const statusEl = document.getElementById("vtd-detector-status");
  const pillEl   = document.getElementById("vtd-pill-det");
  const startBtn = document.getElementById("vtd-detector-start");
  const stopBtn  = document.getElementById("vtd-detector-stop");
  if (!snap || !statusEl) return;
  const chCount = ((session && session.channel_list) || []).length || VTD.total || 0;
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
    statusEl.textContent = chCount
      ? `检测中 · 正在巡检 ${chCount} 路`
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
        prompt,
        rr_interval: Number.isFinite(rr_interval) ? rr_interval : 0,
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

  // 空状态：一进页面画面区是黑的，给一句人话告诉用户下一步做什么
  setVtdOverlay(VTD_EMPTY_HINT, false);

  // ...
  loadModelPick("vtd-model-pick", "swcap_video_describe", "vtd.model_id");
  window.FrameJudge?.mount({ prefix: "vtd", canvasId: "vtd-canvas" });

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

// ------------------------------------------------------------------ *
// 应用向导模式 API — wizard 框架 finish 时调用
// 契约:opts 由 wizard.json 的 steps[].fields[].ui_key 决定:
//   vtd-sources      → opts.source
//   vtd-loop         → opts.loop
//   vtd-prompt       → opts.prompt
//   vtd-detect-size  → opts.detectSize
// source 非空 → 写回 #vtd-sources 并**按行拆成多路**后调 openVtdFromTextarea
//（与「打开视频」按钮同一条路径）；prompt 非空 → 模拟点
// #vtd-detector-start。空字段不触发对应动作。
// 全局名约定 ImApp_<app.id 去掉 hyphen>。
// ------------------------------------------------------------------ *
window.ImApp_video_target_detection = window.ImApp_video_target_detection || {};
window.ImApp_video_target_detection.applyWizardSnapshot = async function (opts) {
  const setVal = (id, v) => { const el = document.getElementById(id); if (el != null) el.value = v; };
  const setChk = (id, v) => { const el = document.getElementById(id); if (el != null) el.checked = !!v; };

  // 多路：向导给的 source 填进 sources 文本框（单值也是合法输入）
  if (opts.source)      setVal("vtd-sources", opts.source);
  if (opts.loop != null) setChk("vtd-loop", opts.loop);
  if (opts.prompt)      setVal("vtd-prompt", opts.prompt);
  if (opts.detectSize)  setVal("vtd-detect-size", opts.detectSize);
  // 帧判据 —— 必须在 open 之前落地：open 一开视频，判据已经在解码热路径上跑。
  const judge = await window.FrameJudge?.fromWizard("vtd", opts) ?? null;

  const result = { source_opened: false, detector_started: false, judge_applied: !!judge };

  if (opts.source) {
    // 关键：走 openVtdFromTextarea 而不是 vtdOpen({source: opts.source})。
    // opts.source 是**整个多行文本**，用单数 key 发过去会被后端当成一路，
    // 多路就会变成一条带换行的假路径导致解码失败（单路却正常）。
    const r = await openVtdFromTextarea();
    if (!r.ok) {
      throw new Error("WizardSnapshotError:open_source_failed:" + (r.reason || "unknown"));
    }
    result.source_opened = true;
    result.channel_count = (r.snap && r.snap.channel_list || []).length;
  }

  if (opts.prompt) {
    const btn = document.getElementById("vtd-detector-start");
    if (btn) {
      try { btn.click(); result.detector_started = true; }
      catch (e) { throw new Error("WizardSnapshotError:detector_start_failed:" + (e?.message || e)); }
    }
  }

  return result;
};

})();
