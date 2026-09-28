(function () {
  "use strict";
  const API_PREFIX = "/api";
  // core helpers (fmtNum, fmtEventTime) via app/static/core/*.js
  const fmtNum = window.fmtNum;
  const fmtEventTime = window.fmtEventTime;

const VCS = {
  ws: null,
  wsRetry: 0,
  wsTimer: null,
  initialized: false,
  snap: null,
  // ...
  seg: null,        // { source, start, end, dur, title }
  playing: false,
  ended: false,     // 播放完毕停在末尾帧（可重播）
  playWs: null,
  playPaused: false,
  lastImg: null,    // 最近一帧 <img> 的 objectURL

  // ...
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
  // ...
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
  // ...
  if (st === "error") {
    vcsShowError(snap.error || "处理失败");
  }

  // ...
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

  // ...
  vcsUpdateButtons(st);

  // ...
  const q = snap.query;
  const meta = vcsEl("vcs-query-meta");
  if (meta) {
    meta.textContent = q ? `命中 ${(snap.hits || []).length} 条` : "";
  }
  vcsRenderHits(snap.hits || [], q || null);
}

  // ...
function vcsUpdateButtons(st) {
  st = st || (VCS.snap && VCS.snap.state) || "idle";
  const startBtn = vcsEl("vcs-start");
  const stopBtn = vcsEl("vcs-stop");
  const queryBtn = vcsEl("vcs-query");
  // ...
  const src = (vcsEl("vcs-source")?.value || "").trim();
  // ...
  const anyChecked = VCS.cacheSel && VCS.cacheSel.size > 0;
  if (startBtn) startBtn.disabled = st === "extracting" || !src || anyChecked;
  if (stopBtn) stopBtn.disabled = st !== "extracting";
  // ...
  if (queryBtn) queryBtn.disabled = !(st === "ready" || anyChecked);
}

  // ...
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
  // ...
  const alive = new Set(videos.map(vcsCacheKey));
  Array.from(VCS.cacheSel).forEach((k) => {
    if (!alive.has(k)) VCS.cacheSel.delete(k);
  });
  vcsCacheRender(videos, enabled);
  if (failed) {
  // ...
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
  // ...
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
  // ...
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
  // ...
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
  // ...
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
  // ...
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
  // ...
  Array.from(list.querySelectorAll("li[data-mi]")).forEach((li) => {
    const h = multi.hits[Number(li.dataset.mi)];
    if (!h) return;
    const src = li.dataset.src || "";
    li.addEventListener("click", () => {
      if (!src) { vcsShowError("该视频无本地文件（微信入库），无法播放/取帧"); return; }
      vcsPlaySegment(h, { source: src, miIndex: Number(li.dataset.mi) });
    });
    li.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        if (!src) { vcsShowError("该视频无本地文件，无法播放/取帧"); return; }
        vcsPlaySegment(h, { source: src, miIndex: Number(li.dataset.mi) });
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
 * ``vcsPlaySegment(hit, opts)`` 的 ``opts.source`` 位索选定 video
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
  // ...
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
  // ...
    const aboveTh = h.above_threshold !== false;
    const badgeCls = aboveTh ? "vcs-badge vcs-badge-ok" : "vcs-badge vcs-badge-low";
    const badgeTxt = aboveTh ? "达标" : "低于阈值";
    return (
      '<li class="vcs-card' + (aboveTh ? "" : " vcs-card-low") + '" data-mi="' + i + '" role="button" tabindex="0">' +
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
  Array.from(list.children).forEach((li, i) => {
    const h = hits[i];
    if (!h) return;
    li.addEventListener("click", () => vcsPlaySegment(h, {
      source: srcBase, fps: fpsBase, miIndex: i,
    }));
    li.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        vcsPlaySegment(h, { source: srcBase, fps: fpsBase, miIndex: i });
      }
    });
  // ...
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
/**
 * 播放命中片段。
 *
 * @param {object} hit  片段对象。单视频路径：VCS.snap.hits[i]；
 *                      多视频路径（缓存勾选）：multi.hits[i]。
 *                      不再由播放器去 VCS.snap.hits 查 —— 多视频检索
 *                      不更新 VCS.snap，旧版读到 undefined 或脏 hit，
 *                      表现为点不动 / 播错段。
 * @param {object} [opts]
 * @param {string} [opts.source]   覆盖 VCS.snap.source（多视频用条目 source_path）。
 * @param {number} [opts.fps]      帧率；缺省从 VCS.snap.fps 取（多视频无 fps 字段时 fallback 25）。
 * @param {number} [opts.miIndex]  命中卡片 data-mi 索引，用于 active 高亮
 *                                  （多视频 children 混合，children 索引法失效）。
 */
function vcsPlaySegment(hit, opts) {
  opts = opts || {};
  if (!hit) return;
  const source = opts.source || (VCS.snap && VCS.snap.source) || "";
  if (!source) { vcsShowError("缺少视频源路径，无法播放"); return; }
  const start = Number(hit.start_time) || 0;
  const end = Number(hit.end_time) || start + 1;
  const dur = Math.max(0.5, end - start);
  const fps = Number(opts.fps)
    || (VCS.snap && Number(VCS.snap.fps))
    || 25;
  VCS.seg = { source, start, end, dur, fps,
              title: vcsFormatTime(start) + "–" + vcsFormatTime(end) };
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
  if (typeof opts.miIndex === "number") {
    document.querySelectorAll("#vcs-results-list li[data-mi]").forEach((li) => {
      li.classList.toggle("active", Number(li.dataset.mi) === opts.miIndex);
    });
  }
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
  // ...
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

  // ...
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

  // ...
function vcsDrawJpeg(jpgBytes) {
  const canvas = vcsEl("vcs-canvas");
  if (!canvas) return;
  // ...
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
  // ...
    const scale = Math.min(vw / img.naturalWidth, vh / img.naturalHeight);
    const dw = img.naturalWidth * scale;
    const dh = img.naturalHeight * scale;
    ctx.drawImage(img, (vw - dw) / 2, (vh - dh) / 2, dw, dh);
  };
  img.src = VCS.lastImg;
}

  // ...
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

  // ...
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

  // ...
let vcsQueryAbort = null;

async function vcsQuery() {
  const text = (vcsEl("vcs-text")?.value || "").trim();
  if (!text) { vcsShowError("请输入检索问题"); return; }
  if (vcsQueryAbort) return;  // 已在检索中，防重复点击
  const threshold = Number(vcsEl("vcs-threshold")?.value || 0.4);
  const topK = Number(vcsEl("vcs-top-k")?.value || 10);
  const btn = vcsEl("vcs-query");
  const meta = vcsEl("vcs-query-meta");
  // ...
  const lenient = !!vcsEl("vcs-lenient")?.checked;
  // ...
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
  // ...
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
  // ...
    window.VdFsModal._sourceTarget = sourceEl;
    window.VdFsModal.open("");
  });
  refreshBtn?.addEventListener("click", vcsRefresh);
  startBtn?.addEventListener("click", vcsStartExtract);
  stopBtn?.addEventListener("click", vcsStopExtract);
  queryBtn?.addEventListener("click", vcsQuery);
  // ...
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

  // ...
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

  // ...
  vcsEl("vcs-play").addEventListener("click", () => {
    if (!VCS.seg || VCS.playing) return;
  // ...
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
  // ...
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
  // ...
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
  // ...
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
  // ...
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
  // ...
  VFA.events.push(ev);
  if (VFA.events.length > 5000) VFA.events.splice(0, VFA.events.length - 5000);
  vfaRenderEventList();
  // ...
  vfaUpdateProgress();
}

  // ...
function vfaSyncEventsCollapse(state) {
  const details = document.getElementById("vfa-events-details");
  if (!details) return;
  const prev = VFA._lastCollapseState;
  if (prev === state) return;          // 同一状态重复推送：不动
  VFA._lastCollapseState = state;
  // ...
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
  // ...
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
  // ...
  VFA.meta.total_frames = Number(snap.total_frames) || 0;
  VFA.meta.every_n      = Number(snap.every_n) || 30;
  VFA.meta.started_at   = Number(snap.started_at) || 0;
  VFA.meta.finished_at  = Number(snap.finished_at) || 0;
  VFA.meta.fps          = Number(snap.fps) || 0;
  vfaSetRuntime(vfaFormatRuntime(snap));
  vfaRefreshButtons(snap);
  vfaRenderSnapshot(snap);
  vfaUpdateProgress(snap);
  // ...
  vfaSyncEventsCollapse(snap.state);
  if (snap.state === "error" && snap.error) vfaShowError(snap.error);
  if (snap.state === "running" || snap.state === "opening") vfaShowError("");
  // ...
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

  // ...
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

  // ...
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

  // ...
  const totalFrames = VFA.meta.total_frames;
  const everyN      = VFA.meta.every_n;
  // ...
  const analyzed    = (snap && Number.isFinite(snap.analyzed))
    ? Number(snap.analyzed)
    : VFA.events.length;

  let expected = 0;
  let ratio = null;
  // ...
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
  // ...
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
  // ...
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

  // ...
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
  // ...
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

  // ...
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
  // ...
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

  // ...


initVideoContentSearch();

})();
