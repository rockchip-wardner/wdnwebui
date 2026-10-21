/* video-retrieval card controller.
 *
 * 由 framework 懒加载（apps/video_retrieval/static/app.js），
 * 加载后立即执行 initVideoRetrieval() 完成页面初始化。
 * 业务逻辑在 app.core.video.search（切片 embedding + 余弦检索）。
 */
(function () {
  "use strict";
  const API_PREFIX = "/api";
  const DemoGuard = window.DemoGuard;
  const VdFsModal = window.VdFsModal;
  const fmtNum = window.fmtNum;
  const fmtEventTime = window.fmtEventTime;

/* ------------------------------------------------------------------ *
 *  video-retrieval page（监控反查）                                      *
 *                                                                    *
 *  后端: app/core/video/search.py —— PyAV 抽帧 + embedding 切片索引,   *
 *  文本查询余弦检索；REST + WS 拉进度/推送。本模块的归一化层          *
 *  （normalizeHits）和播放器帧源接口与后端契约对齐。                   *
 *                                                                    *
 *  协议字段对齐 demo 语义：hits = {video_path, start_time, end_time,  *
 *  chunk_idx, similarity}；query 参数 = {text, min_results?}（阈值/上限 *
 *  固化为服务端常量）。                                              *
 * ------------------------------------------------------------------ */
const VR = {
  ws: null,
  wsRetry: 0,
  wsTimer: null,
  initialized: false,
  snap: null,
  _progressForceIdle: false,   // 清空"检索结果"后强制复位进度条，直到下次提取
  // 播放器（WS 帧流）
  seg: null,        // { source, start, end, dur, title }
  playing: false,
  ended: false,     // 播放完毕停在末尾帧（可重播）
  playWs: null,
  playGen: 0,       // 播放连接代际：切换片段时自增，作废旧 WS 的在途回调
  playPaused: false,
  lastImg: null,    // 最近一帧 <img> 的 objectURL
  // 向导模式 / 手动预览 — 复用 vd 单例 VideoSession 的 WS 帧流
  previewWs: null,
  previewGen: 0,
  previewPlaying: false,   // preview 的播放/暂停态（true=推流中，false=暂停）
  previewPaused: false,
  // 状态回调:wizard 在 _renderFormStep 里 setSourceMeta 用同款渲染,
  // 但 wizard 内 _vrSourceStatus 是空(预渲染阶段无后端 status)。
  // 真实接入点是 initVideoRetrievalCore 末尾注册,接收后端 /source 快照。
  _vrSourceStatus: null,

  // ---- 勾选路径（缓存窗口）：selected key = `${md5}|${chunk}|${overlap}`
  cacheSel: new Set(),
  // 统一历史（按 md5 聚合的一行一视频）：数据 + 展开态
  histItems: [],
  // 「功能配置」勾选前提：对应 capability 是否已在「算力配置」启用
  // （running）。null = 尚未拉到 / 拉取失败 → 不禁用（乐观放行，由后端
  // 503 兜底），避免网络抖动把功能全锁死。
  modeAvail: { search: null },

  init() { return initVideoRetrievalCore(); },
};

function vrEl(id) {
  return document.getElementById(id);
}

function vrSetStatus(text, isError) {
  const el = vrEl("vr-status");
  if (!el) return;
  el.textContent = text || "";
  el.style.color = isError ? "#b91c1c" : "";
}

function vrSetRuntime(text) {
  const el = vrEl("vr-runtime");
  if (!el) return;
  el.textContent = text || "未启动";
}

function vrSetSourceMeta(text) {
  const el = vrEl("vr-source-meta");
  if (!el) return;
  el.textContent = text || "尚未选择";
}

function vrShowError(text) {
  const banner = vrEl("vr-error-banner");
  const textEl = vrEl("vr-error-text");
  if (!banner || !textEl) return;
  textEl.textContent = text || "";
  banner.classList.toggle("hidden", !text);
}

function vrRenderSnapshot(snap) {
  const pre = vrEl("data-video-retrieval");
  if (pre) pre.textContent = JSON.stringify(snap, null, 2);
}

function vrApplySnapshot(snap) {
  if (!snap) return;
  VR.snap = snap;
  vrRenderSnapshot(snap);
  // 提取完成（首次现有条目入库）后刷新缓存窗口 —— 让新条目立即可勾选；
  // 同时清空视频源路径文本框（仅成功清空，error/stopped 保留便于重试），
  // 引导后续检索走"已缓存视频"勾选路径。
  if (snap.state === "ready" && VR._cacheWinOnReady !== snap.slices_embedded
      + "|" + snap.slices_total) {
    VR._cacheWinOnReady = snap.slices_embedded + "|" + snap.slices_total;
    vrCacheRefresh().catch(() => {});
    vrClearSourceAfterReady();
  }
  // 「开始」触发的提取：ready 后自动跑一次检索（支持改问题重查）。
  if (snap.state === "ready" && VR.pendingQuery) {
    VR.pendingQuery = false;
    vrQuery().catch(() => {});
  }
  if (typeof DemoGuard !== "undefined") {
    vrReport();
  }

  const st = snap.state || "idle";
  // 标题栏小字由 vrSyncStatusText（挂在 vrUpdateButtons 末尾）统一维护，
  // 这里不再直接写 #vr-status（避免与算力可用性双写互相覆盖）。
  vrSetRuntime({ idle: "未启动", extracting: "提取中", ready: "就绪，可检索", error: snap.error || "错误" }[st] || st);
  // error 状态同时弹顶部红色 banner（对齐视频页行为）
  if (st === "error") {
    vrShowError(snap.error || "处理失败");
  }

  // 进度
  const prog = vrEl("vr-progress");
  const fill = vrEl("vr-progress-fill");
  const label = vrEl("vr-progress-label");
  const pct = vrEl("vr-progress-pct");
  // 清空"检索结果"后强制复位：下次开始提取（extracting）前，状态推送
  // 不再把进度条刷回 ready/100%。
  if (VR._progressForceIdle && st === "extracting") VR._progressForceIdle = false;
  if (VR._progressForceIdle) {
    if (prog) prog.hidden = true;
    if (fill) fill.style.width = "0%";
    if (label) label.textContent = "检索: 已嵌入 0/0 切片";
    if (pct) pct.textContent = "0%";
  } else if (st === "extracting") {
    if (prog) prog.hidden = false;
    const total = snap.slices_total || 0;
    const emb = snap.slices_embedded || 0;
    const p = total > 0 ? Math.round((emb / total) * 100) : 0;
    if (fill) fill.style.width = p + "%";
    if (label) label.textContent = `检索: 已嵌入 ${emb}/${total} 切片`;
    if (pct) pct.textContent = p + "%";
  } else if (st === "ready") {
    if (prog) prog.hidden = false;
    if (fill) fill.style.width = "100%";
    if (label) label.textContent = `检索: 已嵌入 ${snap.slices_total || 0}/${snap.slices_total || 0} 切片`;
    if (pct) pct.textContent = "100%";
  } else {
    if (prog) prog.hidden = true;
  }

  // 按钮可用性
  vrUpdateButtons();

  // 检索结果
  // 勾选（多视频）路径的结果由 vrRenderMultiGroups 负责，与服务端单例
  // 快照的 hits 无关 —— 勾选模式下**不能**让快照覆盖结果列表，否则会显示
  // 单例里"最后一次单视频查询"的命中（即"选了第 2 个却看到第 1 个"）。
  const inMulti = VR.cacheSel && VR.cacheSel.size > 0;
  if (!inMulti) vrRenderHits(snap.hits || [], snap.query || null);
}

// 把检索进度条复位到初始态（清空"检索结果"时调用）。置 forceIdle 标志，
// 让后续状态推送不再把进度条刷回 ready/100%，直到下次开始提取。
function vrResetProgress() {
  VR._progressForceIdle = true;
  const prog  = vrEl("vr-progress");
  const fill  = vrEl("vr-progress-fill");
  const label = vrEl("vr-progress-label");
  const pct   = vrEl("vr-progress-pct");
  if (prog) prog.hidden = true;
  if (fill) fill.style.width = "0%";
  if (label) label.textContent = "检索: 已嵌入 0/0 切片";
  if (pct) pct.textContent = "0%";
}

// 文本栏右下角 "N / 50" 字数计数（检索问题 / 分析要求共用）。
function bindTaCounter(inputEl, countEl) {
  if (!inputEl || !countEl) return;
  const sync = () => {
    const n = (inputEl.value || "").length;
    countEl.textContent = n + " / 50";
    countEl.classList.toggle("over", n >= 50);
    // 文本变化 → 刷新「开始」可用性 + 禁用原因（问题为空时不可开始）。
    vrUpdateButtons();
  };
  inputEl.addEventListener("input", sync);
  sync();
}

// 「功能配置」勾选前提：对应 capability 已在「算力配置」启用（running）。
// 数据源 /api/compute/instances-by-capability（与后端 get_session 同语义，
// qwen3.5-vl-4b 也能满足 swcap_video_describe）。
const VR_MODE_CAP = {
  search: "swcap_video_retrieval",
};
const VR_MODE_LABEL = {
  search: "[VLEmb]视觉信息编码v3",
};

// 算力可用性探测的单调请求序号：5s ticker / hashchange / init / wizard 会
// 并发发起 vrRefreshModeAvail，慢响应可能晚于快响应到达并把新结果覆盖成
// 旧值（stale overwrite）。每次探测领一个序号，回来时只有仍是最新序号才
// 写入 modeAvail，杜绝乱序覆盖。
let _modeAvailSeq = 0;

async function vrRefreshModeAvail() {
  const seq = ++_modeAvailSeq;
  let search = null;
  try {
    const r = await fetch(`${API_PREFIX}/compute/instances-by-capability`
                          + `?capability=${encodeURIComponent(VR_MODE_CAP.search)}`,
                          { cache: "no-store" });
    if (r.ok) {
      const d = await r.json();
      search = Array.isArray(d.instances) ? d.instances.length > 0 : null;
    }
  } catch (_) {}
  if (seq !== _modeAvailSeq) return;   // 已有更新的探测在跑 → 丢弃本次
  VR.modeAvail = { search };
  vrUpdateButtons();
}

// 标题栏小字：综合「算力可用性 + 任务状态」。
function vrSyncStatusText() {
  const avail = VR.modeAvail || { search: null };
  const st = (VR.snap && VR.snap.state) || "idle";
  let text, err = false;
  if (avail.search === false) {
    text = "检索: 未启用"; err = true;
  } else {
    const m = {
      idle: avail.search === true ? "检索: 就绪" : "检索: 未启动",
      extracting: "检索: 提取中",
      ready: "检索: 就绪",
      error: "检索: 失败",
    };
    text = m[st] || ("检索: " + st);
    err = st === "error";
  }
  vrSetStatus(text, err);
}

// 统一按钮可用性：算力可用性 / 视频源或缓存勾选 / 检索问题 / 提取运行态。
function vrUpdateButtons() {
  const st = (VR.snap && VR.snap.state) || "idle";
  const startBtn = vrEl("vr-start");
  const stopBtn = vrEl("vr-stop");
  const src = (vrEl("vr-source")?.value || "").trim();
  const anyChecked = vrCheckedEntries().length > 0;

  const avail = VR.modeAvail || { search: null };
  const searchOff = avail.search === false;
  const hintEl = vrEl("vr-mode-hint");
  if (hintEl) {
    hintEl.textContent = searchOff
      ? `检索需在「算力配置」启用 ${VR_MODE_LABEL.search}` : "";
    hintEl.hidden = !searchOff;
    hintEl.dataset.error = searchOff ? "1" : "0";
  }
  const subS = vrEl("vr-sub-search");
  if (subS) subS.classList.toggle("vr-subsection--off", searchOff);
  const textEl = vrEl("vr-text");
  const lenientEl = vrEl("vr-lenient");
  if (textEl) textEl.disabled = searchOff;
  if (lenientEl) lenientEl.disabled = searchOff;

  const qSearch = (textEl?.value || "").trim();
  const extracting = st === "extracting";
  const canStart = !searchOff && !extracting
    && (!!src || anyChecked) && !!qSearch;
  if (startBtn) {
    startBtn.disabled = !canStart;
    let reason = "";
    if (!canStart) {
      if (searchOff) reason = "检索模型未启用，请先在「算力配置」启用";
      else if (!src && !anyChecked) reason = "请选择视频文件，或在历史记录里勾选已缓存视频";
      else if (!qSearch) reason = "请填写检索问题";
      else if (extracting) reason = "检索提取进行中";
    }
    startBtn.title = reason;
  }
  if (stopBtn) stopBtn.disabled = !extracting;
  vrSyncStatusText();
  vrApplyHistDisabled();
}

function vrCheckedEntries() {
  const win = vrEl("vr-cache-window");
  const out = [];
  (win?.querySelectorAll(".vr-cache-check:checked") || []).forEach((cb) => {
    out.push({
      md5: cb.dataset.md5,
      chunk_seconds: Number(cb.dataset.chunk) || 0,
      chunk_overlap_seconds: Number(cb.dataset.overlap) || 0,
    });
  });
  return out;
}

// 路径文本非空时禁用历史缓存勾选（路径与勾选互斥）。
function vrApplyHistDisabled() {
  const pathSet = (vrEl("vr-source")?.value || "").trim() !== "";
  if (pathSet && VR.cacheSel.size) VR.cacheSel.clear();
  vrEl("vr-cache-window")?.querySelectorAll(".vr-cache-check").forEach((cb) => {
    cb.disabled = pathSet;
    if (cb.disabled && cb.checked) cb.checked = false;
  });
}

// ---- 勾选路径 / 缓存窗口（v0.12.2）----
async function vrCacheRefresh() {
  const win = vrEl("vr-cache-window");
  if (!win) return;
  let enabled = false;
  let items = [];
  let failed = false;
  try {
    const r = await fetch(API_PREFIX + "/video-retrieval/history",
                          { cache: "no-store" });
    if (r.ok) {
      const d = await r.json();
      enabled = !!d.enabled;
      items = d.items || [];
    } else {
      failed = true;
    }
  } catch (_) {
    failed = true;
  }
  VR.histItems = items;
  // 清除已失效勾选（条目被删 / 全清后）
  const alive = new Set();
  (items || []).forEach((it) => (it.retrieval || []).forEach((rt) => {
    alive.add(it.md5 + "|" + (rt.chunk_seconds || 0) + "|"
              + (rt.chunk_overlap_seconds || 0));
  }));
  Array.from(VR.cacheSel).forEach((k) => {
    if (!alive.has(k)) VR.cacheSel.delete(k);
  });
  vrCacheRender(items, enabled, failed);
  vrApplyMutualExclusion();
}

// 历史列表：一行 = 一条检索缓存（md5 × 切片参数）。
function vrCacheRender(items, enabled, failed) {
  const rows = vrEl("vr-cache-rows");
  const empty = vrEl("vr-cache-empty");
  const count = vrEl("vr-cache-count");
  const win = vrEl("vr-cache-window");
  if (!rows || !win) return;
  win.hidden = false;

  const flat = [];
  (items || []).forEach((it) => {
    (it.retrieval || []).forEach((rt) => flat.push([it, rt]));
  });

  if (count) count.textContent = String(flat.length);
  if (empty) {
    empty.hidden = flat.length > 0;
    if (!flat.length) {
      empty.textContent = failed
        ? "历史列表加载失败，点右上 ⭘ 重试"
        : "暂无记录 —— 完成一次检索提取后自动出现";
    }
  }
  if (!flat.length) { rows.innerHTML = ""; return; }

  rows.innerHTML = flat.map(([it, rt]) => vrCacheRowHtml(it, rt)).join("");

  rows.querySelectorAll(".vr-cache-check").forEach((cb) => {
    cb.addEventListener("change", () => {
      if (cb.checked) VR.cacheSel.add(cb.dataset.key);
      else VR.cacheSel.delete(cb.dataset.key);
      vrApplyMutualExclusion();
    });
  });
  rows.querySelectorAll(".vr-cache-del[data-key]").forEach((btn) => {
    btn.addEventListener("click", async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const md5 = btn.dataset.md5 || "";
      if (!confirm("从缓存数据库删除该条视频索引？(索引可重新提取重建)")) return;
      try {
        const r = await fetch(API_PREFIX + "/video-retrieval/cache/entry", {
          method: "DELETE",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            md5,
            chunk_seconds: Number(btn.dataset.chunk) || 0,
            chunk_overlap_seconds: Number(btn.dataset.overlap) || 0,
          }),
        });
        if (r.ok) await vrCacheRefresh();
      } catch (_) {}
    });
  });
}

// 一行 = 一条检索缓存（md5 × 切片参数）。
function vrCacheRowHtml(it, rt) {
  const key = it.md5 + "|" + (rt.chunk_seconds || 0) + "|"
            + (rt.chunk_overlap_seconds || 0);
  const checked = VR.cacheSel.has(key) ? " checked" : "";
  const meta = [
    `${rt.chunk_seconds || "?"}s/${rt.chunk_overlap_seconds || "?"}s`,
    `${rt.slices || 0} 段`,
    vrFmtTs(rt.analyzed_at || it.updated_at),
  ].filter(Boolean).join(" · ");
  return (
    `<label class="vr-cache-row" data-key="${vrEsc(key)}"` +
      ` title="${vrEsc(it.source_path || "")}">` +
      `<input type="checkbox" class="vr-cache-check" data-key="${vrEsc(key)}"` +
        ` data-md5="${vrEsc(it.md5)}" data-chunk="${rt.chunk_seconds || 0}"` +
        ` data-overlap="${rt.chunk_overlap_seconds || 0}"${checked} />` +
      `<span class="vr-cache-name">${vrEsc(vrBaseName(it.filename))}</span>` +
      `<span class="vr-cache-meta">${vrEsc(meta)}</span>` +
      `<button class="vr-cache-del" type="button" data-key="${vrEsc(key)}"` +
        ` data-md5="${vrEsc(it.md5)}" data-chunk="${rt.chunk_seconds || 0}"` +
        ` data-overlap="${rt.chunk_overlap_seconds || 0}">删除</button>` +
    "</label>"
  );
}

// 分析子行（来源徽章「分析」+ 加载/反查/删除）
// 显示用文件名归一化：历史条目一律只显示 basename。旧版本入库的
// ``filename`` 可能带完整路径，会让检索行/分析行的文字长度与观感不一致。
function vrBaseName(p) {
  const s = String(p || "");
  const i = Math.max(s.lastIndexOf("/"), s.lastIndexOf("\\"));
  return (i >= 0 ? s.slice(i + 1) : s) || "(未命名)";
}

function vrFmtTs(ts) {
  if (!ts) return "--";
  try {
    const d = new Date(ts * 1000);
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  } catch (_) { return "--"; }
}

function vrFmtDur(s) {
  const t = Math.max(0, Math.round(Number(s) || 0));
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const sec = t % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

function vrClearSourceAfterReady() {
  const srcEl = vrEl("vr-source");
  if (!srcEl || !srcEl.value) return;
  srcEl.value = "";
  const metaEl = vrEl("vr-source-meta");
  if (metaEl) {
    metaEl.textContent =
      '提取完成，路径已清空 —— 可在"已缓存视频"中勾选检索';
  }
  vrApplyMutualExclusion();
}

function vrApplyMutualExclusion() {
  // 双路径互斥：路径文本非空 → 缓存勾选全部置灰；
  // 勾选 ≥1 → 路径文本/浏览 置灰。
  const sourceEl = vrEl("vr-source");
  const browseBtn = vrEl("vr-browse");
  const pathSet = (sourceEl?.value || "").trim() !== "";
  if (pathSet && VR.cacheSel.size) VR.cacheSel.clear();
  const anyChecked = VR.cacheSel.size > 0;
  vrEl("vr-cache-window")?.querySelectorAll(".vr-cache-check").forEach((cb) => {
    cb.disabled = pathSet;
    if (cb.disabled && cb.checked) cb.checked = false;
  });
  if (sourceEl) sourceEl.disabled = anyChecked;
  if (browseBtn) browseBtn.disabled = anyChecked;
  vrUpdateButtons();
}

async function vrQueryMulti(text, minResults) {
  // 勾选路径：按窗口选中顺序（窗口行序 = analyzed_at 倒序）检索。
  const entries = [];
  const win = vrEl("vr-cache-window");
  (win?.querySelectorAll(".vr-cache-check:checked") || []).forEach((cb) => {
    entries.push({
      md5: cb.dataset.md5,
      chunk_seconds: Number(cb.dataset.chunk) || 0,
      chunk_overlap_seconds: Number(cb.dataset.overlap) || 0,
    });
  });
  if (!entries.length) {
    VR.cacheSel.clear();
    return;
  }
  // 防重复点击：与单视频路径共用 vrQueryAbort 守卫（本路径不携带
  // abort signal，仅作互斥 + 40s 卡死兜底计时）。
  if (vrQueryAbort) return;
  vrQueryAbort = new AbortController();
  const timer = setTimeout(() => {
    try { vrQueryAbort.abort(); } catch (_) {}
  }, 40000);
  const list = vrEl("vr-results-list");
  if (list) list.innerHTML = '<li class="vr-results-empty">检索中…</li>';
  const count = vrEl("vr-results-count");
  if (count) count.textContent = "检索中…（" + entries.length + " 个视频）";
  try {
    const r = await fetch(API_PREFIX + "/video-retrieval/query_multi", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text, min_results: minResults || 0, entries,
      }),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      vrShowError(d.detail || `query_multi 失败 (HTTP ${r.status})`);
      return;
    }
    const d = await r.json();
    vrRenderMultiGroups(d.groups || [], text);
  } catch (e) {
    vrShowError("query_multi 请求失败：" + e);
  } finally {
    // busy 态完整恢复 —— multi 渲染不走 vrApplySnapshot，count 必须在此清掉
    clearTimeout(timer);
    vrQueryAbort = null;
    // 成功路径 vrRenderMultiGroups 已写好真实条数；仅失败/异常时 count 会
    // 停在"检索中…（N 个视频）"→ 复位。（此处曾误用未声明的 `meta`，每次
    // 多视频检索都在 finally 抛 ReferenceError，被 Promise.allSettled 吞掉。）
    const cEl = vrEl("vr-results-count");
    if (cEl && (cEl.textContent || "").indexOf("检索中…") === 0) {
      cEl.textContent = "0 条";
    }
  }
}

function vrRenderMultiGroups(groups, text) {
  const list = vrEl("vr-results-list");
  const count = vrEl("vr-results-count");
  if (!list) return;
  let html = "";
  let cardSeq = 0;
  const multi = { hits: [], sourceMap: [] };
  groups.forEach((g) => {
    if (g.miss) {
      html += `<li class="vr-results-empty">${vrEsc(g.md5.slice(0,12)
        )}：缓存索引不可读（可能已被删除或清空）</li>`;
      return;
    }
    const params = `${g.chunk_seconds || "?"}s/${g.chunk_overlap_seconds || "?"}s`;
    const frameTag = g.frame_enabled ? "可取帧回看" : "（无本地文件，仅时间段）";
    html += `<li class="vr-group-head">${vrEsc(g.filename || "(未命名)")}` +
      `<span class="vr-g-meta">${vrEsc(params)}` +
      ` · ${g.slices}段 · top ${g.top_score != null ? Number(g.top_score).toFixed(2) : "--"}` +
      `</span><span class="vr-g-frame-tag">${!g.frame_enabled
        ? "仅文本" : "可取帧回看"}</span></li>`;
    const list0 = g.hits || [];
    if (!list0.length) {
      html += '<li class="vr-results-empty">该视频无达到阈值或上限的命中。</li>';
      return;
    }
    html += list0.map((h) => {
      const t = vrFormatTime(h.start_time) + " – " + vrFormatTime(h.end_time);
      const sim = (h.similarity != null) ? Number(h.similarity).toFixed(2) : "--";
      const aboveTh = h.above_threshold !== false;
      const badgeCls = aboveTh ? "vr-badge vr-badge-ok" : "vr-badge vr-badge-low";
      const badgeTxt = aboveTh ? "达标" : "低于阈值";
      const idx = cardSeq++;
      multi.hits.push(h);
      multi.sourceMap.push(g.source_path || "");
      return (
        `<li class="vr-card${g.frame_enabled ? "" : " vr-card-frame-disabled"}` +
          (aboveTh ? "" : " vr-card-low") +
          `" data-mi="${idx}" data-gi="${multi.hits.length - 1}"` +
          ` data-src="${vrEsc(g.source_path || "")}"` +
          ` data-fps="${Number(g.fps) || 0}" role="button" tabindex="0">` +
          `<div class="vr-thumb" data-mt="${Number(h.start_time) || 0}">缩略图</div>` +
          `<div class="vr-card-info">` +
            `<span class="vr-card-time">${vrEsc(t)}</span>` +
            `<span class="vr-card-score">相似度 <b>${sim}</b></span>` +
          "</div>" +
          `<span class="${badgeCls}">${badgeTxt}</span>` +
          `<span class="vr-card-play">▶</span>` +
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
    const fps = Number(li.dataset.fps) || 0;
    li.addEventListener("click", () => {
      if (!src) { vrShowError("该视频无本地文件（微信入库），无法播放/取帧"); return; }
      vrPlaySegment(h, { source: src, fps: fps || undefined,
                          miIndex: Number(li.dataset.mi) });
    });
    li.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        if (!src) { vrShowError("该视频无本地文件，无法播放/取帧"); return; }
        vrPlaySegment(h, { source: src, fps: fps || undefined,
                            miIndex: Number(li.dataset.mi) });
      }
    });
    const thumb = li.querySelector(".vr-thumb");
    const tSec = thumb && Number(thumb.dataset.mt);
    if (thumb && src && tSec != null) {
      const url = API_PREFIX + "/video-retrieval/frame?source=" +
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
 * ``vrPlaySegment(hit, opts)`` 的 ``opts.source`` 位索选定 video
 * 的 source_path（多视频场景下不再读 VR.snap.source）。 */


function vrRenderHits(hits, query) {
  const list = vrEl("vr-results-list");
  const count = vrEl("vr-results-count");
  if (!list) return;
  if (!hits.length) {
    let msg;
    if (!query) {
      msg = "完成提取后输入问题检索，命中片段显示在这里。";
    } else {
      // 区分"完全没结果"vs"问题已查询过但没达阈值"。
      // query_meta.above_threshold_count 用于精确提示：若索引里有
      // 数据但全低于阈值，引导用户降低阈值 / 开宽松匹配。
      const meta = (VR.snap && VR.snap.query_meta) || {};
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
    list.innerHTML = '<li class="vr-results-empty">' + vrEsc(msg) + "</li>";
    if (count) count.textContent = "0 条";
    return;
  }
  if (count) count.textContent = hits.length + " 条";
  const srcBase = (VR.snap && VR.snap.source) || "";
  const fpsBase = (VR.snap && Number(VR.snap.fps)) || 25;
  list.innerHTML = hits.map((h, i) => {
    const t = vrFormatTime(h.start_time) + " – " + vrFormatTime(h.end_time);
    const sim = (h.similarity != null) ? Number(h.similarity).toFixed(2) : "--";
    const t0 = (h.start_time != null) ? Number(h.start_time) : 0;
    // above_threshold=false 来自 fallback：阈值过滤完后无命中时
    // 补回的 top-N（"宽松匹配"模式），标红 badge 提示用户。
    const aboveTh = h.above_threshold !== false;
    const badgeCls = aboveTh ? "vr-badge vr-badge-ok" : "vr-badge vr-badge-low";
    const badgeTxt = aboveTh ? "达标" : "低于阈值";
    return (
      '<li class="vr-card' + (aboveTh ? "" : " vr-card-low") + '" data-mi="' + i + '" role="button" tabindex="0">' +
        '<div class="vr-thumb" data-thumb-t="' + t0 + '">缩略图</div>' +
        '<div class="vr-card-info">' +
          '<span class="vr-card-time">' + vrEsc(t) + "</span>" +
          '<span class="vr-card-score">相似度 <b>' + sim + "</b></span>" +
        '</div>' +
        '<span class="' + badgeCls + '">' + badgeTxt + "</span>" +
        '<span class="vr-card-play">▶</span>' +
      "</li>"
    );
  }).join("");
  Array.from(list.children).forEach((li, i) => {
    const h = hits[i];
    if (!h) return;
    li.addEventListener("click", () => vrPlaySegment(h, {
      source: srcBase, fps: fpsBase, miIndex: i,
    }));
    li.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        vrPlaySegment(h, { source: srcBase, fps: fpsBase, miIndex: i });
      }
    });
    // 懒加载缩略图：从源视频取 start_time 对应帧
    const thumb = li.querySelector(".vr-thumb");
    const tSec = thumb && thumb.dataset.thumbT;
    if (thumb && srcBase && tSec != null) {
      const url = API_PREFIX + "/video-retrieval/frame?source=" +
        encodeURIComponent(srcBase) + "&t=" + encodeURIComponent(tSec);
      const img = document.createElement("img");
      img.alt = "";
      img.onload = () => { thumb.textContent = ""; thumb.appendChild(img); };
      img.onerror = () => { thumb.textContent = "取帧失败"; };
      img.src = url;
    }
  });
}

function vrFormatTime(sec) {
  if (sec == null || !isFinite(sec)) return "--:--";
  sec = Math.max(0, Math.round(Number(sec)));
  const m = String(Math.floor(sec / 60)).padStart(2, "0");
  const s = String(sec % 60).padStart(2, "0");
  return m + ":" + s;
}

function vrEsc(s) {
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
 * @param {object} hit  片段对象。单视频路径：VR.snap.hits[i]；
 *                      多视频路径（缓存勾选）：multi.hits[i]。
 *                      不再由播放器去 VR.snap.hits 查 —— 多视频检索
 *                      不更新 VR.snap，旧版读到 undefined 或脏 hit，
 *                      表现为点不动 / 播错段。
 * @param {object} [opts]
 * @param {string} [opts.source]   覆盖 VR.snap.source（多视频用条目 source_path）。
 * @param {number} [opts.fps]      帧率；缺省从 VR.snap.fps 取（多视频无 fps 字段时 fallback 25）。
 * @param {number} [opts.miIndex]  命中卡片 data-mi 索引，用于 active 高亮
 *                                  （多视频 children 混合，children 索引法失效）。
 */
function vrPlaySegment(hit, opts) {
  opts = opts || {};
  if (!hit) return;
  const source = opts.source || (VR.snap && VR.snap.source) || "";
  if (!source) { vrShowError("缺少视频源路径，无法播放"); return; }
  const start = Number(hit.start_time) || 0;
  const end = Number(hit.end_time) || start + 1;
  const dur = Math.max(0.5, end - start);
  const fps = Number(opts.fps)
    || (VR.snap && Number(VR.snap.fps))
    || 25;
  VR.seg = { source, start, end, dur, fps,
              title: vrFormatTime(start) + "–" + vrFormatTime(end) };
  VR.playing = true;
  VR.playPaused = false;
  VR.ended = false;

  const tag = vrEl("vr-player-tag");
  if (tag) tag.textContent = "正在播放 " + VR.seg.title;
  const overlay = vrEl("vr-overlay");
  if (overlay) overlay.textContent = "";
  vrEl("vr-play").disabled = true;
  vrEl("vr-pause").disabled = false;
  vrEl("vr-stop-play").disabled = false;
  const bar = vrEl("vr-player-progress");
  if (bar) bar.style.width = "0%";
  const timeEl = vrEl("vr-player-time");
  if (timeEl) timeEl.textContent = vrFormatTime(start) + " / " + vrFormatTime(end);
  if (typeof opts.miIndex === "number") {
    document.querySelectorAll("#vr-results-list li[data-mi]").forEach((li) => {
      li.classList.toggle("active", Number(li.dataset.mi) === opts.miIndex);
    });
  }
  vrClosePreviewWs();   // 抢占 canvas：关掉视频源预览（含其按钮态）
  VR.previewPlaying = false;
  VR.previewPaused = false;
  vrRestoreSegmentUI();  // 预览隐藏过进度条/时间，播片段前还原
  vrSyncCanvasSize();
  vrOpenPlayWs(source, start, end);
  if (typeof DemoGuard !== "undefined") {
    vrReport();
  }
}

function vrOpenPlayWs(source, start, end) {
  vrClosePlayWs(false);
  const gen = ++VR.playGen;
  const fps = (VR.snap && Number(VR.snap.fps)) || 25;
  if (VR.seg) {
    VR.seg.frameCount = 0;
    VR.seg.totalFrames = Math.max(1, Math.round((end - start) * fps));
  }
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}${API_PREFIX}/video-retrieval/play`);
  VR.playWs = ws;
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    if (gen !== VR.playGen) return;
    ws.send(JSON.stringify({ source, start, end }));
  };
  ws.onmessage = (ev) => {
    if (gen !== VR.playGen) return;
    if (typeof ev.data === "string") {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (_) { return; }
      if (msg.type === "eof") {
        vrFinishSegment();
      } else if (msg.type === "error") {
        vrShowError(msg.detail || "播放错误");
        vrFinishSegment();
      }
      return;
    }
    // binary JPEG 帧
    vrDrawJpeg(new Uint8Array(ev.data));
    vrTickProgress();
  };
  ws.onclose = () => {
    if (gen === VR.playGen) VR.playWs = null;
  };
  ws.onerror = () => {
    if (gen !== VR.playGen) return;
    try { ws.close(); } catch (_) {}
  };
}

function vrClosePlayWs(settle) {
  VR.playGen++;   // 作废上一连接的 onmessage/onclose/onerror 在途回调
  if (VR.playWs) {
    try { VR.playWs.close(); } catch (_) {}
    VR.playWs = null;
  }
  if (VR.lastImg) {
    URL.revokeObjectURL(VR.lastImg);
    VR.lastImg = null;
  }
  if (settle) {
    VR.playing = false;
    VR.ended = true;
  }
}

// ------------------------------------------------------------------ *
//  向导模式 / 手动预览:打开视频源(后端复用 vd 单例 VideoSession,        *
//  ws 帧流走 /api/video-retrieval/source/ws)。                  *
//  与 vrPlaySegment(检索命中片段回放)互斥 — 共用 #vr-canvas,         *
//  不能同时写。打开预览前关掉检索播放,反之亦然。                       *
// ------------------------------------------------------------------ *
async function vrOpenPreview(source, loop) {
  if (!source) { vrShowError("请先选择视频源"); return; }
  vrClosePlayWs(false);                 // 抢占 canvas:先关掉可能存在的检索播放
  await vrClosePreviewWs();             // 重新打开前确保上一路 preview WS 已关
  try {
    const r = await fetch(API_PREFIX + "/video-retrieval/source/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source, loop: !!loop }),
    });
    if (!r.ok) {
      const detail = await r.text().catch(() => "");
      vrShowError("打开失败: HTTP " + r.status + " " + detail);
      return;
    }
    const snap = await r.json();
    if (snap && snap.error) { vrShowError("打开失败: " + snap.error); return; }
    vrShowError("");
    vrSetSourceMeta("已选: " + source);
    vrConnectPreviewWs();
    // 打开即播放：启用 暂停/停止，隐藏进度条与时间（实时流无时长）。
    VR.previewPlaying = true;
    VR.previewPaused = false;
    vrSetPreviewUI(true);
    if (VR._vrSourceStatus) VR._vrSourceStatus(snap);
  } catch (e) {
    vrShowError("打开失败: " + (e?.message || e));
  }
}

async function vrClosePreview() {
  await vrClosePreviewWs();
  try {
    await fetch(API_PREFIX + "/video-retrieval/source/close", { method: "POST" });
  } catch (_) { /* ignore */ }
  VR.previewPlaying = false;
  VR.previewPaused = false;
  vrClearCanvas();
  vrSetPreviewUI(false);
  vrSetSourceMeta("尚未选择");
  if (VR._vrSourceStatus) VR._vrSourceStatus(null);
}

function vrConnectPreviewWs() {
  vrClosePreviewWs().then(() => {
    const gen = ++VR.previewGen;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}${API_PREFIX}/video-retrieval/source/ws`);
    VR.previewWs = ws;
    ws.binaryType = "arraybuffer";
    ws.onmessage = (ev) => {
      if (gen !== VR.previewGen) return;
      // 检索片段播放激活时,preview 让位 — 让 play WS 独占 vr-canvas。
      // 否则两路 ws 同时画帧会闪烁。
      if (VR.playWs) return;
      if (typeof ev.data === "string") {
        let msg;
        try { msg = JSON.parse(ev.data); } catch (_) { return; }
        if (msg.type === "eof" || msg.type === "status") {
          if (VR._vrSourceStatus) VR._vrSourceStatus(msg);
        }
        return;
      }
      // binary JPEG — 与 vrOpenPlayWs 共用 vrDrawJpeg。
      vrDrawJpeg(new Uint8Array(ev.data));
    };
    ws.onclose = () => {
      if (gen === VR.previewGen) VR.previewWs = null;
    };
    ws.onerror = () => {
      if (gen !== VR.previewGen) return;
      try { ws.close(); } catch (_) {}
    };
  });
}

async function vrClosePreviewWs() {
  VR.previewGen++;
  if (VR.previewWs) {
    try { VR.previewWs.close(); } catch (_) {}
    VR.previewWs = null;
  }
  if (VR.lastImg) {
    URL.revokeObjectURL(VR.lastImg);
    VR.lastImg = null;
  }
}

// 清空播放画面（画黑 + 释放上一帧 objectURL）。关闭 / 停止预览与停止
// 片段回放都走这里 —— 之前 preview 路径只关 WS 不擦屏，导致关闭后画面
// 残留最后一帧。
function vrClearCanvas() {
  const canvas = vrEl("vr-canvas");
  const ctx = canvas && canvas.getContext("2d");
  if (ctx) {
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
  }
  if (VR.lastImg) {
    URL.revokeObjectURL(VR.lastImg);
    VR.lastImg = null;
  }
}

// 播放器控制区显隐 —— 视频源预览是实时流、没有天然时长，预览时隐藏
// 进度条与时间，只留「预览中」标签。
function vrSetPreviewUI(on) {
  const bar = vrEl("vr-player-progress");
  const timeEl = vrEl("vr-player-time");
  const play = vrEl("vr-play");
  const pause = vrEl("vr-pause");
  const stop = vrEl("vr-stop-play");
  if (bar && bar.parentElement) bar.parentElement.hidden = !!on;
  if (timeEl) timeEl.hidden = !!on;
  if (play) play.disabled = true;          // 打开即播放 → 「播放」暂不可点
  if (pause) pause.disabled = !on;
  if (stop) stop.disabled = !on;
  const tag = vrEl("vr-player-tag");
  if (tag) tag.textContent = on ? "预览中" : "未播放";
}

// 恢复片段回放所需的 UI（进度条 / 时间可见）。预览隐藏过它们后，播片段
// 前必须还原，否则片段进度条不显示。
function vrRestoreSegmentUI() {
  const bar = vrEl("vr-player-progress");
  const timeEl = vrEl("vr-player-time");
  if (bar && bar.parentElement) bar.parentElement.hidden = false;
  if (timeEl) timeEl.hidden = false;
}

// 视频源预览的播放 / 暂停 / 停止 —— 后端 VideoSession，与 vd 页同一套
// 语义（stop == close）。
async function vrPreviewControl(action) {
  try {
    await fetch(API_PREFIX + "/video-retrieval/source/control", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    });
  } catch (_) { /* ignore */ }
}

async function vrPlayPreview() {
  if (!VR.previewWs || !VR.previewPaused) return;
  await vrPreviewControl("play");
  VR.previewPaused = false;
  VR.previewPlaying = true;
  vrEl("vr-play").disabled = true;
  vrEl("vr-pause").disabled = false;
  if (typeof DemoGuard !== "undefined") vrReport();
}

async function vrPausePreview() {
  if (!VR.previewWs || !VR.previewPlaying) return;
  await vrPreviewControl("pause");
  VR.previewPaused = true;
  VR.previewPlaying = false;
  vrEl("vr-play").disabled = false;
  vrEl("vr-pause").disabled = true;
  if (typeof DemoGuard !== "undefined") vrReport();
}

async function vrStopPreview() {
  await vrPreviewControl("stop");
  await vrClosePreviewWs();
  VR.previewPlaying = false;
  VR.previewPaused = false;
  vrClearCanvas();
  vrSetPreviewUI(false);
  vrSetSourceMeta("尚未选择");
  if (VR._vrSourceStatus) VR._vrSourceStatus(null);
  if (typeof DemoGuard !== "undefined") vrReport();
}

// 把 vr-canvas 像素尺寸同步为播放器区域（.vd-stage）显示尺寸，
// 使 canvas 填满窗口、contain 绘制能放大到最大（消除元素级黑边）。
function vrSyncCanvasSize() {
  const canvas = vrEl("vr-canvas");
  if (!canvas || !canvas.parentElement) return;
  const w = Math.max(2, Math.floor(canvas.parentElement.clientWidth));
  const h = Math.max(2, Math.floor(canvas.parentElement.clientHeight));
  if (canvas.width !== w || canvas.height !== h) {
    canvas.width = w;
    canvas.height = h;
  }
}

// 用 <img> 承帧后画到 canvas（对齐音视频描述页的 WS 帧流渲染模式）
function vrDrawJpeg(jpgBytes) {
  const canvas = vrEl("vr-canvas");
  if (!canvas) return;
  // 窗口 resize 后显示尺寸漂移则重新同步
  if (Math.abs(canvas.clientWidth - canvas.width) > 1 ||
      Math.abs(canvas.clientHeight - canvas.height) > 1) {
    vrSyncCanvasSize();
  }
  const blob = new Blob([jpgBytes], { type: "image/jpeg" });
  if (VR.lastImg) URL.revokeObjectURL(VR.lastImg);
  VR.lastImg = URL.createObjectURL(blob);
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
  img.src = VR.lastImg;
}

// 每收到一帧推进进度条 + 时间显示（按已收帧数 / 预计总帧数）
function vrTickProgress() {
  if (!VR.seg) return;
  VR.seg.frameCount = (VR.seg.frameCount || 0) + 1;
  const total = VR.seg.totalFrames || 1;
  const p = Math.min(1, VR.seg.frameCount / total);
  const bar = vrEl("vr-player-progress");
  if (bar) bar.style.width = (p * 100).toFixed(1) + "%";
  const timeEl = vrEl("vr-player-time");
  if (timeEl && VR.seg.end) {
    const t = VR.seg.start + p * (VR.seg.end - VR.seg.start);
    timeEl.textContent = vrFormatTime(t) + " / " + vrFormatTime(VR.seg.end);
  }
}

// 播放完毕：停在末尾帧、进度条 100%，播放按钮可重播（重播需重开 WS）。
function vrFinishSegment() {
  if (!VR.seg) return;
  VR.playing = false;
  VR.ended = true;
  vrClosePlayWs(false);
  const bar = vrEl("vr-player-progress");
  if (bar) bar.style.width = "100%";
  const timeEl = vrEl("vr-player-time");
  if (timeEl) timeEl.textContent = vrFormatTime(VR.seg.end) + " / " + vrFormatTime(VR.seg.end);
  vrEl("vr-play").disabled = false;
  vrEl("vr-pause").disabled = true;
  vrEl("vr-stop-play").disabled = false;
  if (typeof DemoGuard !== "undefined") {
    vrReport();
  }
}

function vrPausePlayback() {
  if (!VR.playing) return;
  VR.playing = false;
  VR.playPaused = true;
  if (VR.playWs && VR.playWs.readyState === 1) {
    try { VR.playWs.send(JSON.stringify({ action: "pause" })); } catch (_) {}
  }
  vrEl("vr-play").disabled = false;
  vrEl("vr-pause").disabled = true;
  if (typeof DemoGuard !== "undefined") {
    vrReport();
  }
}

function vrStopPlayback() {
  VR.playing = false;
  VR.ended = false;
  vrClosePlayWs(false);
  VR.seg = null;
  const bar = vrEl("vr-player-progress");
  if (bar) bar.style.width = "0%";
  const timeEl = vrEl("vr-player-time");
  if (timeEl) timeEl.textContent = "--";
  const tag = vrEl("vr-player-tag");
  if (tag) tag.textContent = "未播放";
  vrEl("vr-play").disabled = true;
  vrEl("vr-pause").disabled = true;
  vrEl("vr-stop-play").disabled = true;
  vrClearCanvas();
  if (typeof DemoGuard !== "undefined") {
    vrReport();
  }
}

/* ------------------------- WS 连接 ------------------------- */
function connectVrWs() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}${API_PREFIX}/video-retrieval/ws`;
  const ws = new WebSocket(url);
  VR.ws = ws;
  ws.onopen = () => {
    VR.wsRetry = 0;
    vrShowError("");
  };
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }
    if (msg.type === "hello" || msg.type === "state") {
      vrApplySnapshot(msg);
    } else if (msg.type === "progress") {
      const snap = Object.assign({}, VR.snap || {});
      snap.state = "extracting";
      snap.slices_embedded = msg.embedded;
      snap.slices_total = msg.total;
      vrApplySnapshot(snap);
    } else if (msg.type === "error") {
      vrShowError(msg.detail || "WS 错误");
    }
  };
  ws.onclose = () => {
    VR.ws = null;
    const wait = Math.min(5000, 500 * Math.pow(2, VR.wsRetry++));
    VR.wsTimer = setTimeout(connectVrWs, wait);
  };
  ws.onerror = () => {
    try { ws.close(); } catch (_) {}
  };
}

function closeVrWs() {
  if (VR.wsTimer) { clearTimeout(VR.wsTimer); VR.wsTimer = null; }
  if (VR.ws) { try { VR.ws.close(); } catch (_) {} VR.ws = null; }
  VR.wsRetry = 0;
}

/* ------------------------- REST 动作 ------------------------- */
async function vrRefresh() {
  try {
    const r = await fetch(API_PREFIX + "/video-retrieval/");
    if (!r.ok) throw new Error("HTTP " + r.status);
    const snap = await r.json();
    vrApplySnapshot(snap);
  } catch (err) {
    vrSetStatus("加载失败: " + err.message, true);
  }
}

async function vrStartExtract() {
  const source = (vrEl("vr-source")?.value || "").trim();
  if (!source) { vrShowError("请先选择视频文件"); return; }
  try {
    const r = await fetch(API_PREFIX + "/video-retrieval/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "HTTP " + r.status);
    vrApplySnapshot(data);
  } catch (err) {
    vrShowError(err.message);
    return;
  }
  try {
    // 切片参数（4s / 重叠 1s）固化为服务端常量，前端不收集。
    const r = await fetch(API_PREFIX + "/video-retrieval/extract", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "HTTP " + r.status);
    vrApplySnapshot(data);
  } catch (err) {
    vrShowError(err.message);
  }
}

async function vrStopExtract() {
  try {
    const r = await fetch(API_PREFIX + "/video-retrieval/extract/stop", { method: "POST" });
    const data = await r.json();
    if (r.ok) vrApplySnapshot(data);
  } catch (_) {}
}

// ---- 合并页统一入口（分析 / 检索按「功能配置」勾选并行发起） ----

// 统一刷新：检索快照 + 历史列表 + 算力可用性。
function vrRefreshAll() {
  vrRefresh();
  vrCacheRefresh().catch(() => {});
  vrRefreshModeAvail().catch(() => {});
}

// DemoGuard 上报：只上报检索运行态。
function vrReport() {
  if (typeof DemoGuard === "undefined") return;
  DemoGuard.report("video-retrieval", {
    search: VR.snap || {},
    busy: !!VR.playing || (VR.snap && VR.snap.state === "extracting"),
  });
}

// 「开始」：只走检索（缓存勾选优先，否则先提取再检索）。
async function vrStart() {
  // 新一轮任务：允许 ready 快照再次触发"路径已清空"（防抖只用于同轮去重）。
  VR._cacheWinOnReady = null;
  const src = (vrEl("vr-source")?.value || "").trim();
  const anyChecked = vrCheckedEntries().length > 0;
  const qSearch = (vrEl("vr-text")?.value || "").trim();
  const avail = VR.modeAvail || { search: null };

  if (!src && !anyChecked) {
    vrShowError("请先选择视频文件，或在历史记录里勾选已缓存视频"); return;
  }
  if (avail.search === false) {
    vrShowError(`「检索」功能未启用 —— 请先在「算力配置」启用 ${VR_MODE_LABEL.search}`);
    return;
  }
  if (!qSearch) { vrShowError("请填写「检索」的检索问题"); return; }
  vrShowError("");
  await vrStartSearch(src, anyChecked);
  vrUpdateButtons();
}

// 「检索」分支：勾选缓存 → 直接多视频检索；索引已就绪且源未变 → 只重检索；
// 否则先提取，提取完成（ready）后自动检索一次（见 vrApplySnapshot）。
async function vrStartSearch(src, anyChecked) {
  if (anyChecked) { await vrQuery(); return; }
  const st = (VR.snap && VR.snap.state) || "idle";
  // _progressForceIdle：刚清空检索结果 / 清空缓存，即使有旧 ready 快照被
  // WS 刷回也不能短路跳过提取（该标志在下次 extracting 时自动清除）。
  if (st === "ready" && !VR._progressForceIdle
      && VR.snap && VR.snap.source === src) {
    await vrQuery();
    vrClearSourceAfterReady();
    return;
  }
  VR.pendingQuery = true;
  await vrStartExtract();
}

// 「停止」：停止切片提取。
async function vrStop() {
  await vrStopExtract();
  vrUpdateButtons();
}

// 检索进行中可取消（40s 超时兜底，即使后端极端卡死也能反馈）
let vrQueryAbort = null;

// 匹配阈值(0.40) / 检索上限(10) / 切片参数(4s, 重叠 1s) 固化为服务端
// 常量（app/core/video/search.py 的 FIXED_*），页面不收集。这里只保留
// 「宽松匹配」的兜底条数，与服务端检索上限保持一致。
const VR_LENIENT_TOP_K = 10;

async function vrQuery() {
  const text = (vrEl("vr-text")?.value || "").trim();
  // 兜底校验：「开始」在问题为空时已被 vrUpdateButtons 禁用，但本函数
  // 还有第二条入口 —— 提取完成后 vrApplySnapshot 的 pendingQuery 自动
  // 触发；提取期间（~90s）用户可能把问题清空，此守卫避免把空问题打到
  // 后端（否则提示退化成 "启动失败: HTTP 400 检索问题不能为空"）。
  if (!text) { vrShowError("请输入检索问题"); return; }
  if (vrQueryAbort) return;  // 已在检索中，防重复点击
  const countEl = vrEl("vr-results-count");
  // 宽松匹配在两条路径里都要读（分叉后各自携带 min_results 语义）
  const lenient = !!vrEl("vr-lenient")?.checked;
  // ---- 双路径分叉（在设置全局 busy 态之前）：勾选 ≥1 缓存条目 →
  // 多视频检索（query_multi，自带完整的 busy 态生命周期）；否则走
  // 原单视频 query。两路排他（勾选优先）且数据面隔离 —— multi 只读
  // LanceDB、不触碰单例的内存索引；ready 状态下取消勾选再检索，
  // 单例索引原样可用。
  if (vrCheckedEntries().length > 0) {
    await vrQueryMulti(text, lenient ? VR_LENIENT_TOP_K : 0);
    return;
  }
  vrQueryAbort = new AbortController();
  const timer = setTimeout(() => vrQueryAbort.abort(), 40000);
  if (countEl) countEl.textContent = "检索中…";
  try {
    const body = { text };
    // 严格模式（默认）：固定阈值 0.40 过滤后无命中就返回空。
    // 勾选"宽松匹配" → min_results=10，启用 fallback：
    //   阈值过滤完后无命中时补回 top-10（无视阈值，标"低于阈值"）。
    if (lenient) body.min_results = VR_LENIENT_TOP_K;
    const r = await fetch(API_PREFIX + "/video-retrieval/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: vrQueryAbort.signal,
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "HTTP " + r.status);
    vrApplySnapshot(data);
  } catch (err) {
    if (countEl) countEl.textContent = "--";
    if (err && err.name === "AbortError") {
      vrShowError("检索超时：40s 内未完成，请重试");
    } else {
      vrShowError(err.message);
    }
  } finally {
    clearTimeout(timer);
    vrQueryAbort = null;
    vrUpdateButtons();
  }
}

function initVideoRetrievalCore() {
  if (VR.initialized) return;
  VR.initialized = true;

  const refreshBtn = vrEl("vr-refresh");
  const browseBtn = vrEl("vr-browse");
  const startBtn = vrEl("vr-start");
  const stopBtn = vrEl("vr-stop");
  const errClose = vrEl("vr-error-close");
  const resultsClear = vrEl("vr-results-clear");
  const sourceEl = vrEl("vr-source");
  const textEl = vrEl("vr-text");

  const syncSourceMeta = () => {
    const v = (sourceEl?.value || "").trim();
    vrSetSourceMeta(v ? "已选: " + v : "尚未选择");
    vrUpdateButtons();
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
  refreshBtn?.addEventListener("click", vrRefreshAll);
  startBtn?.addEventListener("click", vrStart);
  stopBtn?.addEventListener("click", vrStop);

  // 向导模式 / 手动预览 — 复用 vd 单例 VideoSession 跑连续预览帧流。
  // 与 vd / vtd / vtl 的「打开 / 关闭」按钮行为一致:open 把 source/loop
  // 写回后端,然后把后端 WS 帧流接到 #vr-canvas(已被 wizard 搬到
  // canvasWrap 内,getElementById 不变,直接画就行)。
  const openBtn  = vrEl("vr-open");
  const closeBtn = vrEl("vr-close");
  const loopEl   = vrEl("vr-loop");
  openBtn?.addEventListener("click", () => {
    const src = (sourceEl?.value || "").trim();
    vrOpenPreview(src, !!(loopEl && loopEl.checked));
  });
  closeBtn?.addEventListener("click", () => {
    vrClosePreview();
  });
  // loop 同步到后端:不重 open(避免断流),走独立控制端点 (暂用 close+open)
  loopEl?.addEventListener("change", async () => {
    if (!VR.previewWs) return;
    const src = (sourceEl?.value || "").trim();
    if (!src) return;
    try { await fetch(API_PREFIX + "/video-retrieval/source/close", { method: "POST" }); }
    catch (_) {}
    vrOpenPreview(src, !!loopEl.checked);
  });
  // 勾选路径（缓存窗口）：刷新按钮 + 进入页面即拉一次列表 +
  // 路径输入变化触发互斥重算（vrApplyMutualExclusion 内部含
  // checkbox disabled 反向）。
  vrEl("vr-cache-refresh")?.addEventListener("click", vrCacheRefresh);
  vrEl("vr-source")?.addEventListener("input", vrApplyMutualExclusion);
  vrCacheRefresh().catch(() => {});
  const clearCacheBtn = vrEl("vr-clear-cache");
  clearCacheBtn?.addEventListener("click", async () => {
    let stats = null;
    try {
      const r = await fetch(API_PREFIX + "/video-retrieval/cache/stats");
      if (r.ok) stats = await r.json();
    } catch (_) {}
    const nR = stats?.videos || 0;
    if (!confirm(nR > 0
        ? `确认清空全部缓存？(检索 ${nR} 条；清空后需重新提取)`
        : "确认清空全部缓存？")) return;
    try {
      const r = await fetch(API_PREFIX + "/video-retrieval/cache/clear",
                            { method: "POST" });
      const d = await r.json();
      const k = d.videos_cleared || 0;
      alert(k > 0
            ? `已清空 ${k} 条视频的缓存。`
            : "缓存已空（或缓存未启用），无需清理。");
      // 清空缓存 = 连会话索引一并作废（后端 reset_session 已广播 idle）。
      // 本地立即复位：丢弃 ready 快照与"缓存窗口已刷新"标记，让下次
      // 「开始」不再被 vrStartSearch 的 ready+同源短路，真正重新提取。
      VR.snap = null;
      VR._cacheWinOnReady = null;
      VR.pendingQuery = false;
      vrResetProgress();
      vrCacheRefresh().catch(() => {});
    } catch (e) {
      alert("清空缓存失败：" + e);
    }
  });
  errClose?.addEventListener("click", () => vrShowError(""));
  resultsClear?.addEventListener("click", async () => {
    // 先停正在播放 / 暂停中的命中片段，再清空结果。
    if (VR.seg && (VR.playing || VR.playPaused || VR.playWs)) {
      vrStopPlayback();
    }
    // 清空检索结果：调后端清单例的 query / hits —— 单视频结果存在服务端
    // 单例里，只清前端列表的话下一次 WS 快照又会把它渲染回来。
    // （旧实现用 POST /query {text:"清空占位", threshold:1.0} 取巧"无命中"，
    // 既会白跑一次 NPU embedding，又把 _query 留成"清空占位"。）
    try {
      const r = await fetch(API_PREFIX + "/video-retrieval/query/clear",
                            { method: "POST" });
      if (r.ok) vrApplySnapshot(await r.json());
    } catch (_) {}
    // 勾选（多视频）路径下 vrApplySnapshot 不覆盖结果列表 → 显式清一次
    const list = vrEl("vr-results-list");
    if (list) {
      list.innerHTML =
        '<li class="vr-results-empty">完成提取后输入问题检索，命中片段显示在这里。</li>';
    }
    const count = vrEl("vr-results-count");
    if (count) count.textContent = "0 条";
    // 清空检索结果 = 复位检索面板：进度条回初始态（forceIdle 保持到下次提取）。
    vrResetProgress();
    // 播完停在末帧时也复位画布（正在播放的场景已在上面先停过）。
    if (VR.seg) vrStopPlayback();
  });

  // 50 字计数（检索问题）
  bindTaCounter(textEl, vrEl("vr-ta-count"));

  // 播放器按钮 —— 按当前模式分派：视频源预览（previewWs）走 VideoSession
  // 控制（播放/暂停/停止，与 vd 页同款），检索片段回放走 /search/play。
  vrEl("vr-play").addEventListener("click", () => {
    if (VR.previewWs) { vrPlayPreview(); return; }
    if (!VR.seg || VR.playing) return;
    // 暂停中恢复：发 WS resume
    if (VR.playPaused && VR.playWs && VR.playWs.readyState === 1) {
      VR.playPaused = false;
      VR.playing = true;
      try { VR.playWs.send(JSON.stringify({ action: "resume" })); } catch (_) {}
      vrEl("vr-play").disabled = true;
      vrEl("vr-pause").disabled = false;
      if (typeof DemoGuard !== "undefined") {
        vrReport();
      }
      return;
    }
    // 播完重播：从头重开 WS
    if (VR.seg && VR.seg.source) {
      VR.ended = false;
      const bar = vrEl("vr-player-progress");
      if (bar) bar.style.width = "0%";
      const timeEl = vrEl("vr-player-time");
      if (timeEl) timeEl.textContent = vrFormatTime(VR.seg.start) + " / " + vrFormatTime(VR.seg.end);
      vrEl("vr-play").disabled = true;
      vrEl("vr-pause").disabled = false;
      vrOpenPlayWs(VR.seg.source, VR.seg.start, VR.seg.end);
      VR.playing = true;
      if (typeof DemoGuard !== "undefined") {
        vrReport();
      }
    }
  });
  vrEl("vr-pause").addEventListener("click", () => {
    if (VR.previewWs) { vrPausePreview(); return; }
    vrPausePlayback();
  });
  vrEl("vr-stop-play").addEventListener("click", () => {
    if (VR.previewWs) { vrStopPreview(); return; }
    vrStopPlayback();
  });

  vrRefresh();
  if (!VR.ws && !VR.wsTimer) connectVrWs();
}

let _modeAvailBound = false;
let _modeAvailTicker = false;

function initVideoRetrieval() {
  VR.init();
  if (!VR.ws && !VR.wsTimer) connectVrWs();

  // 算力能力可用性 —— 决定「检索」勾选框是否置灰。卡片 JS 只在首次进入时
  // 加载一次（framework 的 _loadedApps 记忆），所以除了首次拉取，还需在
  // 切回本页时重拉（用户可能刚在「算力配置」启用模型）。
  vrRefreshModeAvail().catch(() => {});
  if (!_modeAvailBound) {
    _modeAvailBound = true;
    window.addEventListener("hashchange", () => {
      const h = (location.hash || "").replace(/^#\/?/, "") || "apps";
      if (h === "video-retrieval") {
        vrRefreshModeAvail().catch(() => {});
      }
    });
  }
  // 周期兜底：算力可能在另一个标签页/页面被启用或卸载，仅靠首次进入 +
  // hashchange 会漏掉（页面一直开着不重新导航时状态不刷新）。
  if (!_modeAvailTicker) {
    _modeAvailTicker = true;
    setInterval(() => {
      if (document.visibilityState !== "visible") return;
      vrRefreshModeAvail().catch(() => {});
    }, 5000);
  }
}

window.VR = { init: initVideoRetrieval, refresh: vrRefreshAll };

initVideoRetrieval();

// ------------------------------------------------------------------ *
//  应用向导模式 API — wizard 框架 finish 时调用
// 契约:opts 由 wizard.json 的 steps[].fields[].ui_key 决定:
//   vr-source → opts.source   vr-text → opts.searchText
// source 齐全时勾选「检索」并点 #vr-start（先提取再检索）；
// searchText 非空时写入检索问题框。切片参数固化为服务端常量，向导不收集。
// 写值后统一补发 input/change —— 卡片内源框 / 勾选框的监听依赖它们。
// 全局名约定 ImApp_<app.id 去掉 hyphen>。
// ------------------------------------------------------------------ *
window.ImApp_video_retrieval = window.ImApp_video_retrieval || {};
window.ImApp_video_retrieval.applyWizardSnapshot = async function (opts) {
  const setVal = (id, v) => {
    const el = document.getElementById(id);
    if (el == null) return;
    el.value = v;
    el.dispatchEvent(new Event("input", { bubbles: true }));
  };
  const setChecked = (id, on) => {
    const el = document.getElementById(id);
    if (el == null) return;
    el.checked = !!on;
    el.dispatchEvent(new Event("change", { bubbles: true }));
  };

  // 向导是"新任务"：清掉页面上残留的缓存勾选（否则会被误判为多视频检索）。
  VR.cacheSel.clear();
  document.querySelectorAll(".vr-cache-check").forEach((cb) => { cb.checked = false; });

  if (opts.source) setVal("vr-source", opts.source);
  if (opts.loop != null) setChecked("vr-loop", !!opts.loop);
  // 检索问题可选：填了才写入问题框（勾选动作统一放到算力刷新之后）。
  const hasSearch = !!(opts.searchText && String(opts.searchText).trim());
  if (hasSearch) setVal("vr-text", String(opts.searchText));
  // 切片参数固化为服务端常量（4s / 重叠 1s），向导不再收集。
  const baseReady = !!opts.source;
  if (!baseReady) {
    return { source_set: !!opts.source,
             search_set: hasSearch, started: false };
  }
  // 勾选前先主动刷新一次算力可用性。向导在 _waitForModelsRunning 之后
  // 立即回调，而前端 modeAvail 走 5s 周期，此刻可能仍是"未启用"旧值。
  await vrRefreshModeAvail().catch(() => {});
  const btn = document.getElementById("vr-start");
  if (btn) {
    try { btn.click(); }
    catch (e) { throw new Error("WizardSnapshotError:vr_start_failed:" + (e?.message || e)); }
    return { source_set: true,
             search_set: hasSearch, started: true };
  }
  return { source_set: true,
           search_set: hasSearch, started: false };
};

})();
