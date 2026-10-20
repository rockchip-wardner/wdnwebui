/* video-analysis-search (VAS) card controller.
 *
 * 由 framework 懒加载（apps/video_analysis_search/static/app.js），
 * 加载后立即执行 initVideoAnalysisSearch() 完成页面初始化。
 * 检索侧逻辑在 app.core.video.search，分析侧在卡片 service.py。
 */
(function () {
  "use strict";
  const API_PREFIX = "/api";
  const DemoGuard = window.DemoGuard;
  const VdFsModal = window.VdFsModal;
  const fmtNum = window.fmtNum;
  const fmtEventTime = window.fmtEventTime;

/* ------------------------------------------------------------------ *
 *  video-analysis-search page（监控分析检索：分析 + 检索合并页）                            *
 *                                                                    *
 *  后端: app/core/video/search.py —— PyAV 抽帧 + embedding 切片索引,   *
 *  文本查询余弦检索；REST + WS 拉进度/推送。本模块的归一化层          *
 *  （normalizeHits）和播放器帧源接口与后端契约对齐。                   *
 *                                                                    *
 *  协议字段对齐 demo 语义：hits = {video_path, start_time, end_time,  *
 *  chunk_idx, similarity}；query 参数 = {text, min_results?}（阈值/上限 *
 *  固化为服务端常量）。                                              *
 * ------------------------------------------------------------------ */
const VAS_SEARCH = {
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
  // 但 wizard 内 _vcsSourceStatus 是空(预渲染阶段无后端 status)。
  // 真实接入点是 initVideoSearch 末尾注册,接收后端 /source 快照。
  _vcsSourceStatus: null,

  // ---- 勾选路径（缓存窗口）：selected key = `${md5}|${chunk}|${overlap}`
  cacheSel: new Set(),
  // 统一历史（按 md5 聚合的一行一视频）：数据 + 展开态
  histItems: [],
  histOpen: new Set(),
  // 分析条目**单选**：全局唯一选中的 run_id（底部「加载」/「反查」
  // 按钮作用于它；selRun 缓存其 source/total_frames/fps）。
  selRunId: "",
  selRun: null,

  // 「功能配置」勾选前提：对应 capability 是否已在「算力配置」启用
  // （running）。null = 尚未拉到 / 拉取失败 → 不禁用（乐观放行，由后端
  // 503 兜底），避免网络抖动把功能全锁死。
  modeAvail: { analyze: null, search: null },

  init() { return initVideoSearch(); },
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
  const pre = vcsEl("data-video-analysis-search");
  if (pre) pre.textContent = JSON.stringify(snap, null, 2);
}

function vcsApplySnapshot(snap) {
  if (!snap) return;
  VAS_SEARCH.snap = snap;
  vcsRenderSnapshot(snap);
  // 提取完成（首次现有条目入库）后刷新缓存窗口 —— 让新条目立即可勾选；
  // 同时清空视频源路径文本框（仅成功清空，error/stopped 保留便于重试），
  // 引导后续检索走"已缓存视频"勾选路径。
  if (snap.state === "ready" && VAS_SEARCH._cacheWinOnReady !== snap.slices_embedded
      + "|" + snap.slices_total) {
    VAS_SEARCH._cacheWinOnReady = snap.slices_embedded + "|" + snap.slices_total;
    vcsCacheRefresh().catch(() => {});
    const srcEl = vcsEl("vcs-source");
    if (srcEl && srcEl.value) {
      srcEl.value = "";
      const metaEl = vcsEl("vcs-source-meta");
      if (metaEl) metaEl.textContent = "提取完成，路径已清空 —— 可在\"已缓存视频\"中勾选检索";
      vcsApplyMutualExclusion();
    }
  }
  // 「开始」触发的提取：ready 后自动跑一次检索（支持改问题重查）。
  if (snap.state === "ready" && VAS_SEARCH.pendingQuery) {
    VAS_SEARCH.pendingQuery = false;
    vcsQuery().catch(() => {});
  }
  if (typeof DemoGuard !== "undefined") {
    vasReport();
  }

  const st = snap.state || "idle";
  // 标题栏小字由 vasSyncStatusText（挂在 vasUpdateButtons 末尾）统一维护，
  // 这里不再直接写 #vcs-status（避免与算力可用性双写互相覆盖）。
  vcsSetRuntime({ idle: "未启动", extracting: "提取中", ready: "就绪，可检索", error: snap.error || "错误" }[st] || st);
  // error 状态同时弹顶部红色 banner（对齐 VAS 行为）
  if (st === "error") {
    vcsShowError(snap.error || "处理失败");
  }

  // 进度
  const prog = vcsEl("vcs-progress");
  const fill = vcsEl("vcs-progress-fill");
  const label = vcsEl("vcs-progress-label");
  const pct = vcsEl("vcs-progress-pct");
  // 清空"检索结果"后强制复位：下次开始提取（extracting）前，状态推送
  // 不再把进度条刷回 ready/100%。
  if (VAS_SEARCH._progressForceIdle && st === "extracting") VAS_SEARCH._progressForceIdle = false;
  if (VAS_SEARCH._progressForceIdle) {
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
  vasUpdateButtons();

  // 检索结果
  // 勾选（多视频）路径的结果由 vcsRenderMultiGroups 负责，与服务端单例
  // 快照的 hits 无关 —— 勾选模式下**不能**让快照覆盖结果列表，否则会显示
  // 单例里"最后一次单视频查询"的命中（即"选了第 2 个却看到第 1 个"）。
  const inMulti = VAS_SEARCH.cacheSel && VAS_SEARCH.cacheSel.size > 0;
  if (!inMulti) vcsRenderHits(snap.hits || [], snap.query || null);
}

// 把检索进度条复位到初始态（清空"检索结果"时调用）。置 forceIdle 标志，
// 让后续状态推送不再把进度条刷回 ready/100%，直到下次开始提取。
function vcsResetProgress() {
  VAS_SEARCH._progressForceIdle = true;
  const prog  = vcsEl("vcs-progress");
  const fill  = vcsEl("vcs-progress-fill");
  const label = vcsEl("vcs-progress-label");
  const pct   = vcsEl("vcs-progress-pct");
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
    vasUpdateButtons();
  };
  inputEl.addEventListener("input", sync);
  sync();
}

// 「功能配置」勾选前提：对应 capability 已在「算力配置」启用（running）。
// 数据源 /api/compute/instances-by-capability（与后端 get_session 同语义，
// qwen3.5-vl-4b 也能满足 swcap_video_describe）。
const VAS_MODE_CAP = {
  analyze: "swcap_video_describe",
  search:  "swcap_video_retrieval",
};
const VAS_MODE_LABEL = {
  analyze: "[VLM]视觉描述v3",
  search:  "[VLEmb]视觉信息编码v3",
};

// 算力可用性探测的单调请求序号：5s ticker / hashchange / init / wizard 会
// 并发发起 vasRefreshModeAvail，慢响应可能晚于快响应到达并把新结果覆盖成
// 旧值（stale overwrite）。每次探测领一个序号，回来时只有仍是最新序号才
// 写入 modeAvail，杜绝乱序覆盖。
let _modeAvailSeq = 0;

async function vasRefreshModeAvail() {
  const seq = ++_modeAvailSeq;
  const probe = async (cap) => {
    try {
      const r = await fetch(`${API_PREFIX}/compute/instances-by-capability`
                            + `?capability=${encodeURIComponent(cap)}`,
                            { cache: "no-store" });
      if (!r.ok) return null;
      const d = await r.json();
      return Array.isArray(d.instances) ? d.instances.length > 0 : null;
    } catch (_) {
      return null;
    }
  };
  const [analyze, search] = await Promise.all([
    probe(VAS_MODE_CAP.analyze),
    probe(VAS_MODE_CAP.search),
  ]);
  if (seq !== _modeAvailSeq) return;   // 已有更新的探测在跑 → 丢弃本次
  VAS_SEARCH.modeAvail = { analyze, search };
  vasUpdateButtons();
}

// 标题栏小字：综合「算力可用性 + 任务状态」。
//   算力未加载 → 「未启用」（红）；已加载但未跑 → 「就绪」；运行/完成 → 任务状态。
// 挂在 vasUpdateButtons 末尾 —— 算力刷新（vasRefreshModeAvail）与任务状态推送
// （vcsApplySnapshot / vasRefreshButtons）两条路径都会同步到小字。
function vasSyncStatusText() {
  const avail = VAS_SEARCH.modeAvail || { analyze: null, search: null };

  // 检索（#vcs-status）
  const st = (VAS_SEARCH.snap && VAS_SEARCH.snap.state) || "idle";
  let sText, sErr = false;
  if (avail.search === false) {
    sText = "检索: 未启用"; sErr = true;
  } else {
    const m = {
      idle: avail.search === true ? "检索: 就绪" : "检索: 未启动",
      extracting: "检索: 提取中",
      ready: "检索: 就绪",
      error: "检索: 失败",
    };
    sText = m[st] || ("检索: " + st);
    sErr = st === "error";
  }
  vcsSetStatus(sText, sErr);

  // 分析（#vas-status）
  const vst = VAS.state || "idle";
  let aText, aErr = false;
  if (avail.analyze === false) {
    aText = "分析: 未启用"; aErr = true;
  } else {
    const m = {
      idle: avail.analyze === true ? "分析: 就绪" : "分析: 未启动",
      opening: "分析: 打开中",
      running: "分析: 分析中",
      summarizing: "分析: 综述中",
      done: "分析: 已完成",
      stopped: "分析: 已停止",
      error: "分析: 失败",
    };
    aText = m[vst] || ("分析: " + vst);
    aErr = vst === "error";
  }
  vasSetStatus(aText, aErr);
}

// 统一按钮可用性：功能勾选 / 算力可用性 / 视频源 / 缓存勾选 / 两端运行态
// 共同决定。合并前是 VAS_SEARCH 的 vcsUpdateButtons；现同时管「分析」与「检索」。
function vasUpdateButtons() {
  const st = (VAS_SEARCH.snap && VAS_SEARCH.snap.state) || "idle";
  const startBtn = vcsEl("vas-start");
  const stopBtn = vcsEl("vas-stop");
  // 开始依据前端输入框当前值（浏览/手输路径即点亮），而非后端 snap.source。
  const src = (vcsEl("vcs-source")?.value || "").trim();
  // 勾选路径：勾选 ≥1 个缓存条目时开始走"多视频检索"（无需提取）。
  const anyChecked = VAS_SEARCH.cacheSel && VAS_SEARCH.cacheSel.size > 0;

  // 算力可用性：false = 未启用 → checkbox 置灰，但**不动用户勾选**。
  // 旧实现 `if (analyzeOff && aCb.checked) aCb.checked = false` 有个致命
  // 副作用：modeAvail 的探测结果会抖（list_running_by_capability 只看
  // status=="running"，worker probe 偶发失败即翻 false），每次抖动都清掉
  // 用户刚勾上的功能 → canStart 跟着抖 → 「开始」按钮闪烁（用户实测）。
  // 勾选态归用户掌控；"功能未启用"由 vasStart() 在点开始那一刻校验。
  const avail = VAS_SEARCH.modeAvail || { analyze: null, search: null };
  const analyzeOff = avail.analyze === false;
  const searchOff = avail.search === false;
  const aCb = vcsEl("vas-mode-analyze");
  const sCb = vcsEl("vas-mode-search");
  if (aCb) {
    aCb.disabled = analyzeOff;
    aCb.title = analyzeOff
      ? `需在「算力配置」启用 ${VAS_MODE_LABEL.analyze}` : "";
  }
  if (sCb) {
    sCb.disabled = searchOff;
    sCb.title = searchOff
      ? `需在「算力配置」启用 ${VAS_MODE_LABEL.search}` : "";
  }
  vcsEl("vas-mode-analyze-label")?.classList.toggle("vas-mode--off", analyzeOff);
  vcsEl("vas-mode-search-label")?.classList.toggle("vas-mode--off", searchOff);
  const hintEl = vcsEl("vas-mode-hint");
  if (hintEl) {
    const parts = [];
    if (analyzeOff) parts.push(`分析需在「算力配置」启用 ${VAS_MODE_LABEL.analyze}`);
    if (searchOff) parts.push(`检索需在「算力配置」启用 ${VAS_MODE_LABEL.search}`);
    hintEl.textContent = parts.join("；");
    hintEl.hidden = parts.length === 0;
    hintEl.dataset.error = parts.length ? "1" : "0";
  }

  const wantAnalyze = !!aCb?.checked;
  const wantSearch = !!sCb?.checked;
  // 未勾选「分析」→ 清空分析选中（radio 由 vcsApplyHistDisabled 同步置灰），
  // 放在计算 selRun 之前，避免按钮可用性滞后一拍。
  if (!wantAnalyze && VAS_SEARCH.selRunId) { VAS_SEARCH.selRunId = ""; VAS_SEARCH.selRun = null; }
  // 未勾选「检索」→ 清空检索缓存勾选（checkbox 由 vcsApplyHistDisabled /
  // vcsApplyMutualExclusion 同步置灰），避免残留 key 让 anyChecked 计数失真。
  if (!wantSearch && VAS_SEARCH.cacheSel.size) VAS_SEARCH.cacheSel.clear();
  // 问题文本框：检索问题必填（后端 query_multi 校验非空）。留空时「开始」
  // 不可点。分析提问已固化为服务端常量，前端不再收集。
  const qSearch = (vcsEl("vcs-text")?.value || "").trim();
  const extracting = st === "extracting";
  const vasActive = VAS.state === "opening" || VAS.state === "running"
                 || VAS.state === "summarizing";
  const running = extracting || vasActive;
  // 选中历史分析记录 → 「开始」= 加载该记录（若勾了「分析」），并可在同时
  // 勾了「检索」且检索有可执行路径时并发检索。
  const selRun = !!(VAS_SEARCH.selRunId && VAS_SEARCH.selRun);
  // 按「这次实际会执行的功能」判定可用性 —— 勾了功能但没给它输入时（分析无
  // 视频源、检索无源也无缓存勾选），该功能不会执行，不参与 canStart，也不
  // 要求它的必填项。否则会出现"只勾了检索缓存，却因为勾了分析而被要求视频
  // 源"的假置灰（用户实测）。
  const selSearchRun = wantSearch && (anyChecked || !!src);  // selRun 分支：并发检索
  const analyzeRun   = wantAnalyze && !!src;                 // else 分支：新分析需源
  const searchRun    = wantSearch && (!!src || anyChecked);  // else 分支：检索需源或缓存
  let canStart;
  if (selRun) {
    canStart = (wantAnalyze || selSearchRun) && !running;
    if (selSearchRun && !qSearch) canStart = false;
  } else {
    canStart = (analyzeRun || searchRun) && !running;
    if (searchRun && !qSearch) canStart = false;
  }
  if (startBtn) {
    startBtn.disabled = !canStart;
    // 禁用原因（单条，按优先级）—— 按钮灰掉时 hover 能看到为什么。
    let reason = "";
    if (!canStart && selRun) {
      reason = (!wantAnalyze && !selSearchRun)
        ? "需勾选「分析」以加载历史记录，或勾选「检索」并选择缓存/视频源"
        : (selSearchRun && !qSearch) ? "请填写「检索」的检索问题"
        : extracting ? "检索提取进行中" : "分析进行中";
    } else if (!canStart) {
      if (analyzeOff && searchOff) {
        reason = hintEl?.textContent || "请先在「算力配置」启用所需模型";
      } else if (!wantAnalyze && !wantSearch) {
        reason = "请至少勾选「分析」或「检索」";
        if (hintEl && !hintEl.hidden && hintEl.textContent) {
          reason += `（${hintEl.textContent}）`;
        }
      } else if (!analyzeRun && !searchRun) {
        reason = "请选择视频文件，或在历史记录里勾选已缓存视频";
      } else if (searchRun && !qSearch) {
        reason = "请填写「检索」的检索问题";
      } else if (extracting) {
        reason = "检索提取进行中";
      } else if (vasActive) {
        reason = "分析进行中";
      }
    }
    startBtn.title = reason || (selRun ? "加载选中的分析记录到界面" : "");
  }
  if (stopBtn) stopBtn.disabled = !running;
  // 「分析反查」（操作栏）：目标 = 选中的历史记录，或当前已加载的分析 run
  // （有 run_id 且未被「清空」掉）。**不要求 state==="done"** —— 加载历史
  // 记录时后端 load_run 会把 state 重置为 idle 并广播，若绑定 done 则加载
  // 完历史后反查立即失效。反查是纯读盘也不需要「分析」功能处于勾选态。
  const doneRun = (VAS.snap && VAS.snap.run_id
                   && VAS.snap.run_id !== VAS._clearedRun) ? VAS.snap : null;
  const hasQueryTarget = !!(selRun || doneRun);
  // 反查是分析侧能力，不被检索提取(extracting)阻塞。能否反查由后端
  // snap.queryable 权威判定 —— 仅"完整跑完 (done)"为 true；综述生成中
  // (summarizing，含缓存命中路径) 仍为 false（须等综述落盘、流程结束）。
  // 选中的历史记录(selRun)独立可反查，不受当前 run 状态影响。
  const queryable = !!(VAS.snap && VAS.snap.queryable) || !!selRun;
  const queryReason = !hasQueryTarget ? "请先选中一条分析记录，或完成一次分析"
    : !queryable ? "分析进行中，暂不可反查"
    : "";
  const queryBtn = vcsEl("vas-run-query");
  if (queryBtn) {
    // 不使用 disabled —— disabled 元素点击零反馈且 title 不显示。改用
    // aria-disabled + is-off 视觉置灰，点击时由 handler 弹出原因。
    const queryOff = !!queryReason;
    queryBtn.disabled = false;
    queryBtn.setAttribute("aria-disabled", queryOff ? "true" : "false");
    queryBtn.classList.toggle("is-off", queryOff);
    queryBtn.title = queryReason
      || (selRun ? "按场景 / 人物 / 物体反查选中的分析记录"
                 : "按场景 / 人物 / 物体反查当前视频的分析结果");
  }
  // 未勾选的功能子栏置灰（视觉反馈）
  const subA = vcsEl("vas-sub-analyze");
  const subS = vcsEl("vas-sub-search");
  if (subA) subA.classList.toggle("vas-subsection--off", !wantAnalyze);
  if (subS) subS.classList.toggle("vas-subsection--off", !wantSearch);
  vasSyncStatusText();
  vcsApplyHistDisabled();
}

// 按「功能配置」勾选实时刷历史行的置灰（只改 class，不重建 DOM ——
// 避免与 vcsCacheRender → vcsApplyMutualExclusion → vasUpdateButtons 成环）。
function vcsApplyHistDisabled() {
  const wantA = !!vcsEl("vas-mode-analyze")?.checked;
  const wantS = !!vcsEl("vas-mode-search")?.checked;
  const pathSet = (vcsEl("vcs-source")?.value || "").trim() !== "";
  vcsEl("vcs-cache-window")?.querySelectorAll(".vas-hist-group").forEach((g) => {
    const it = (VAS_SEARCH.histItems || []).find((x) => x.md5 === g.dataset.md5);
    if (!it) return;
    const nR = (it.retrieval || []).length;
    const nA = (it.analysis || []).length;
    g.classList.toggle("vas-hist-disabled", (wantS && !nR) || (wantA && !nA));
  });
  // 分析条目 radio：未勾选「分析」或路径非空时置灰（路径与选中记录互斥），
  // 置灰时同步取消已选中。本函数不回调 vasUpdateButtons（避免成环）——
  // 清 VAS_SEARCH.selRunId 的时机放在 vasUpdateButtons 开头。
  vcsEl("vcs-cache-window")?.querySelectorAll(".vas-run-radio").forEach((rb) => {
    rb.disabled = pathSet || !wantA;
    if (rb.disabled && rb.checked) rb.checked = false;
  });
  // 检索条目 checkbox：同样在未勾选「检索」或路径非空时置灰，置灰时取消
  // 已勾选。清 VAS_SEARCH.cacheSel 的时机同 radio —— 放在 vasUpdateButtons
  // 开头，本函数只负责 DOM 层。
  vcsEl("vcs-cache-window")?.querySelectorAll(".vcs-cache-check").forEach((cb) => {
    cb.disabled = pathSet || !wantS;
    if (cb.disabled && cb.checked) cb.checked = false;
  });
}

// ---- 勾选路径 / 缓存窗口（v0.12.2）----
async function vcsCacheRefresh() {
  const win = vcsEl("vcs-cache-window");
  if (!win) return;
  let enabled = false;
  let items = [];
  let failed = false;
  try {
    const r = await fetch(API_PREFIX + "/video-analysis-search/search/history",
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
  VAS_SEARCH.histItems = items;
  // 清除已失效勾选（条目被删 / 全清后）
  const alive = new Set();
  (items || []).forEach((it) => (it.retrieval || []).forEach((rt) => {
    alive.add(it.md5 + "|" + (rt.chunk_seconds || 0) + "|"
              + (rt.chunk_overlap_seconds || 0));
  }));
  Array.from(VAS_SEARCH.cacheSel).forEach((k) => {
    if (!alive.has(k)) VAS_SEARCH.cacheSel.delete(k);
  });
  // 清除已失效的分析选中（记录被删 / 全清后）；仍有效则刷新 selRun 数据
  if (VAS_SEARCH.selRunId) {
    const runAlive = (items || []).some((it) =>
      (it.analysis || []).some((a) => a.run_id === VAS_SEARCH.selRunId));
    if (runAlive) vasSelectRun(VAS_SEARCH.selRunId);
    else { VAS_SEARCH.selRunId = ""; VAS_SEARCH.selRun = null; }
  }
  vcsCacheRender(items, enabled, failed);
  vcsApplyMutualExclusion();
  vasUpdateRunActions();
}

// 统一历史列表：**按视频（md5）聚合** —— 一个视频一行（可展开子行）：
//   检索子行（可勾选多选检索 / 删除）
//   分析子行（加载 / 反查 / 删除）
// 按功能勾选置灰（仅灰主行做提示，子行操作不受影响）：
//   勾了「检索」但该视频无检索索引 / 勾了「分析」但无分析记录。
function vcsCacheRender(items, enabled, failed) {
  const rows = vcsEl("vcs-cache-rows");
  const empty = vcsEl("vcs-cache-empty");
  const count = vcsEl("vcs-cache-count");
  const win = vcsEl("vcs-cache-window");
  if (!rows || !win) return;
  win.hidden = false;

  const wantA = !!vcsEl("vas-mode-analyze")?.checked;
  const wantS = !!vcsEl("vas-mode-search")?.checked;

  if (count) count.textContent = String((items || []).length);
  if (empty) {
    empty.hidden = (items || []).length > 0;
    if (!(items || []).length) {
      empty.textContent = failed
        ? "历史列表加载失败，点右上 ⭘ 重试"
        : "暂无记录 —— 完成一次分析或检索后自动出现";
    }
  }
  if (!(items || []).length) { rows.innerHTML = ""; return; }

  rows.innerHTML = items.map((it) => vasGroupHtml(it, wantA, wantS)).join("");

  // ---- 主行点击 → 展开 / 折叠子行
  rows.querySelectorAll(".vas-hist-group").forEach((li) => {
    const md5 = li.dataset.md5 || "";
    li.querySelector(".vas-hist-head")?.addEventListener("click", (ev) => {
      if (ev.target.closest("button, input")) return;
      if (VAS_SEARCH.histOpen.has(md5)) VAS_SEARCH.histOpen.delete(md5);
      else VAS_SEARCH.histOpen.add(md5);
      li.classList.toggle("vas-hist-open");
    });
  });

  // ---- 检索子行：勾选 + 删除
  rows.querySelectorAll(".vcs-cache-check").forEach((cb) => {
    cb.addEventListener("change", () => {
      if (cb.checked) VAS_SEARCH.cacheSel.add(cb.dataset.key);
      else VAS_SEARCH.cacheSel.delete(cb.dataset.key);
      vcsApplyMutualExclusion();
    });
  });
  rows.querySelectorAll(".vcs-cache-del[data-key]").forEach((btn) => {
    btn.addEventListener("click", async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const md5 = btn.dataset.md5 || "";
      if (!confirm("从缓存数据库删除该条视频索引？(索引可重新分析重建)")) return;
      try {
        const r = await fetch(API_PREFIX + "/video-analysis-search/search/cache/entry", {
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

  // ---- 分析子行：单选（checkbox 实现"再点取消"）+ 删除
  // 「加载」/「反查」按钮已移到历史窗口底部统一操作栏（作用于选中的
  // 那条），此处只处理选中与删除。
  rows.querySelectorAll(".vas-run-radio").forEach((rb) => {
    rb.addEventListener("change", () => {
      if (rb.checked) {
        // 单条互斥：清掉其它分析条目的勾选（checkbox 不会自动互斥）
        rows.querySelectorAll(".vas-run-radio").forEach((other) => {
          if (other !== rb && other.checked) other.checked = false;
        });
        vasSelectRun(rb.dataset.runId || "");
      } else if (VAS_SEARCH.selRunId === (rb.dataset.runId || "")) {
        // 取消选中当前条目
        vasSelectRun("");
      }
    });
  });
  rows.querySelectorAll(".vcs-cache-del[data-run-id]").forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      vasDeleteRun(btn.dataset.runId || "");
    });
  });
  vasUpdateRunActions();
}

// 分析条目单选：更新全局选中 + 底部操作栏可用性。
function vasSelectRun(runId) {
  VAS_SEARCH.selRunId = runId || "";
  VAS_SEARCH.selRun = null;
  if (VAS_SEARCH.selRunId) {
    for (const it of (VAS_SEARCH.histItems || [])) {
      const an = (it.analysis || []).find((a) => a.run_id === VAS_SEARCH.selRunId);
      if (an) {
        const fps = Number(it.fps) || 0;
        const dur = Number(it.duration_s) || 0;
        VAS_SEARCH.selRun = {
          run_id: an.run_id,
          source: it.source_path || "",
          // 视频总帧数：/history 给的是 duration_s + fps，据此推算
          // （反查 modal 的时间范围 hint 用）；缺失时 0 → "总时长未知"。
          total_frames: (fps > 0 && dur > 0) ? Math.round(dur * fps) : 0,
          fps,
        };
        break;
      }
    }
  }
  vasUpdateRunActions();
}

// 选中分析记录后刷新操作栏按钮（「开始」= 加载 / 「分析反查」可用性）。
// 反查按钮已移入操作栏，其 disabled 逻辑统一由 vasUpdateButtons 维护；
// 本函数保留作兼容转发（vasSelectRun / vasDeleteRun / vcsCacheRefresh 调用）。
function vasUpdateRunActions() {
  vasUpdateButtons();
}

// 聚合主行（文件名 + 「检索 N」/「分析 M」徽章 + 时间）+ 子行
function vasGroupHtml(it, wantA, wantS) {
  const md5 = it.md5 || "";
  const nR = (it.retrieval || []).length;
  const nA = (it.analysis || []).length;
  const open = VAS_SEARCH.histOpen.has(md5);
  const disabled = (wantS && !nR) || (wantA && !nA);
  const file = vasBaseName(it.filename);
  const dur = it.duration_s ? vasFmtDur(it.duration_s) : "";
  const meta = [vasFmtTs(it.updated_at), dur].filter(Boolean).join(" · ");
  const badgeS = nR
    ? `<span class="vas-hist-badge vas-hist-badge-cache">检索 ${nR}</span>`
    : `<span class="vas-hist-badge vas-hist-badge-off">检索 0</span>`;
  const badgeA = nA
    ? `<span class="vas-hist-badge vas-hist-badge-run">分析 ${nA}</span>`
    : `<span class="vas-hist-badge vas-hist-badge-off">分析 0</span>`;
  const subs = [];
  (it.retrieval || []).forEach((rt) => subs.push(vasRetrievalRowHtml(it, rt)));
  (it.analysis || []).forEach((an) => subs.push(vasAnalysisRowHtml(it, an)));
  return (
    `<div class="vas-hist-group${open ? " vas-hist-open" : ""}` +
      `${disabled ? " vas-hist-disabled" : ""}" data-md5="${vcsEsc(md5)}">` +
      `<div class="vas-hist-head" title="${vcsEsc(it.source_path || "")}">` +
        `<span class="vas-hist-caret">▸</span>` +
        `<span class="vcs-cache-name">${vcsEsc(file)}</span>` +
        badgeS + badgeA +
        `<span class="vcs-cache-meta">${vcsEsc(meta)}</span>` +
      `</div>` +
      `<div class="vas-hist-sub">${subs.join("")}</div>` +
    "</div>"
  );
}

// 检索子行（来源徽章「检索」+ 勾选框 + 删除）
function vasRetrievalRowHtml(it, rt) {
  const key = it.md5 + "|" + (rt.chunk_seconds || 0) + "|"
            + (rt.chunk_overlap_seconds || 0);
  const checked = VAS_SEARCH.cacheSel.has(key) ? " checked" : "";
  const meta = `${rt.chunk_seconds || "?"}s/${rt.chunk_overlap_seconds || "?"}s`
             + ` · ${rt.slices || 0}段`;
  return (
    `<label class="vcs-cache-row" title="${vcsEsc(it.source_path || "")}">` +
      `<input type="checkbox" class="vcs-cache-check" data-key="${vcsEsc(key)}"` +
        ` data-md5="${vcsEsc(it.md5)}" data-chunk="${rt.chunk_seconds || 0}"` +
        ` data-overlap="${rt.chunk_overlap_seconds || 0}"${checked} />` +
      `<span class="vas-hist-badge vas-hist-badge-cache">检索</span>` +
      `<span class="vcs-cache-meta">${vcsEsc(meta)}</span>` +
      `<button class="vcs-cache-del" type="button" data-key="${vcsEsc(key)}"` +
        ` data-md5="${vcsEsc(it.md5)}" data-chunk="${rt.chunk_seconds || 0}"` +
        ` data-overlap="${rt.chunk_overlap_seconds || 0}">删除</button>` +
    "</label>"
  );
}

// 分析子行（来源徽章「分析」+ 加载/反查/删除）
function vasAnalysisRowHtml(it, an) {
  const runId = an.run_id || "";
  // 采样方式文案：手动 = "间隔 N 帧"；自动（任一度量）= "判帧 · 最长 N 帧"
  //（自动模式下 every_n 语义是"最长间隔"兜底，不是固定间隔）。
  // 历史行 sample_mode 可能是二值的 "auto"（历史硬编码 z_mad，现迁移到 conc），
  // 照旧按自动显示。
  const mode = an.sample_mode || "manual";
  const isAuto = mode !== "manual" && mode !== "";
  const sampleText = isAuto
    ? `${mode === "auto" ? "自动判帧" : mode}（≤${an.every_n || "?"} 帧）`
    : `间隔 ${an.every_n || "?"} 帧`;
  const meta = `${sampleText} · ${an.frame_count || 0} 帧`
             + ` · ${an.state || "unknown"}`
             + (an.summary ? " · 有综述" : "");
  // 用 checkbox（不是 radio）—— radio 语义上无法"再点一次取消"，用户
  // 选中一条分析记录后就没有退路（实测反馈）。checkbox 允许取消，选中
  // 逻辑仍是"单条"：由 JS 在选中时清掉其它条目的勾选（见 change handler）。
  const checked = (VAS_SEARCH.selRunId && VAS_SEARCH.selRunId === runId) ? " checked" : "";
  return (
    `<div class="vcs-cache-row vas-hist-run" data-run-id="${vcsEsc(runId)}"` +
      ` data-source="${vcsEsc(it.source_path || "")}"` +
      ` data-total-frames="0" data-fps="${it.fps || 0}"` +
      ` title="${vcsEsc(it.source_path || "")}">` +
      `<input type="checkbox" class="vas-run-radio"` +
        ` data-run-id="${vcsEsc(runId)}"${checked} />` +
      `<span class="vas-hist-badge vas-hist-badge-run">分析</span>` +
      `<span class="vcs-cache-meta">${vcsEsc(meta)}</span>` +
      `<button class="vcs-cache-del" type="button" data-run-id="${vcsEsc(runId)}"` +
        ` title="删除此分析记录（不可恢复）">删除</button>` +
    "</div>"
  );
}

// 显示用文件名归一化：历史条目一律只显示 basename。旧版本入库的
// ``filename`` 可能带完整路径，会让检索行/分析行的文字长度与观感不一致。
function vasBaseName(p) {
  const s = String(p || "");
  const i = Math.max(s.lastIndexOf("/"), s.lastIndexOf("\\"));
  return (i >= 0 ? s.slice(i + 1) : s) || "(未命名)";
}

function vasFmtDur(s) {
  const t = Math.max(0, Math.round(Number(s) || 0));
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const sec = t % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

function vcsApplyMutualExclusion() {
  // 双路径互斥：路径文本非空 → 勾选框全部置灰；
  // 勾选 ≥1 → 路径文本/浏览/开始提取 置灰。
  const sourceEl = vcsEl("vcs-source");
  const browseBtn = vcsEl("vcs-browse");
  const pathSet = (sourceEl?.value || "").trim() !== "";
  const wantS = !!vcsEl("vas-mode-search")?.checked;
  const anyChecked = VAS_SEARCH.cacheSel.size > 0;
  vcsEl("vcs-cache-window")?.querySelectorAll(".vcs-cache-check").forEach((cb) => {
    cb.disabled = pathSet || !wantS;
    if (cb.disabled && cb.checked) cb.checked = false;
  });
  // 分析条目 radio 与检索勾选**一并置灰**（路径文本非空时两套选择都不可用）；
  // 同时清空已选中的分析记录 —— 「路径」与「选中记录」互斥，避免路径非空
  // 却仍走「开始 = 加载历史记录」分支。
  vcsEl("vcs-cache-window")?.querySelectorAll(".vas-run-radio").forEach((rb) => {
    rb.disabled = pathSet;
    if (pathSet && rb.checked) rb.checked = false;
  });
  if (pathSet && VAS_SEARCH.selRunId) {
    VAS_SEARCH.selRunId = "";
    VAS_SEARCH.selRun = null;
  }
  if (sourceEl) sourceEl.disabled = anyChecked;
  if (browseBtn) browseBtn.disabled = anyChecked;
  vasUpdateButtons();
}

async function vcsQueryMulti(text, minResults) {
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
  // 合并页没有独立的"检索"按钮（见 vcsQuery 顶部注释）—— 不要在这里操作
  // vcsEl("vas-query")，那是反查 modal 容器。
  const list = vcsEl("vcs-results-list");
  if (list) list.innerHTML = '<li class="vcs-results-empty">检索中…</li>';
  const count = vcsEl("vcs-results-count");
  if (count) count.textContent = "检索中…（" + entries.length + " 个视频）";
  try {
    const r = await fetch(API_PREFIX + "/video-analysis-search/search/query_multi", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text, min_results: minResults || 0, entries,
      }),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      vcsShowError(d.detail || `query_multi 失败 (HTTP ${r.status})`);
      return;
    }
    const d = await r.json();
    vcsRenderMultiGroups(d.groups || [], text);
  } catch (e) {
    vcsShowError("query_multi 请求失败：" + e);
  } finally {
    // busy 态完整恢复 —— multi 渲染不走 vcsApplySnapshot，count 必须在此清掉
    clearTimeout(timer);
    vcsQueryAbort = null;
    // 成功路径 vcsRenderMultiGroups 已写好真实条数；仅失败/异常时 count 会
    // 停在"检索中…（N 个视频）"→ 复位。（此处曾误用未声明的 `meta`，每次
    // 多视频检索都在 finally 抛 ReferenceError，被 Promise.allSettled 吞掉。）
    const cEl = vcsEl("vcs-results-count");
    if (cEl && (cEl.textContent || "").indexOf("检索中…") === 0) {
      cEl.textContent = "0 条";
    }
  }
}

function vcsRenderMultiGroups(groups, text) {
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
          ` data-src="${vcsEsc(g.source_path || "")}"` +
          ` data-fps="${Number(g.fps) || 0}" role="button" tabindex="0">` +
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
    const fps = Number(li.dataset.fps) || 0;
    li.addEventListener("click", () => {
      if (!src) { vcsShowError("该视频无本地文件（微信入库），无法播放/取帧"); return; }
      vcsPlaySegment(h, { source: src, fps: fps || undefined,
                          miIndex: Number(li.dataset.mi) });
    });
    li.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        if (!src) { vcsShowError("该视频无本地文件，无法播放/取帧"); return; }
        vcsPlaySegment(h, { source: src, fps: fps || undefined,
                            miIndex: Number(li.dataset.mi) });
      }
    });
    const thumb = li.querySelector(".vcs-thumb");
    const tSec = thumb && Number(thumb.dataset.mt);
    if (thumb && src && tSec != null) {
      const url = API_PREFIX + "/video-analysis-search/search/frame?source=" +
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
 * 的 source_path（多视频场景下不再读 VAS_SEARCH.snap.source）。 */


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
      const meta = (VAS_SEARCH.snap && VAS_SEARCH.snap.query_meta) || {};
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
  const srcBase = (VAS_SEARCH.snap && VAS_SEARCH.snap.source) || "";
  const fpsBase = (VAS_SEARCH.snap && Number(VAS_SEARCH.snap.fps)) || 25;
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
    // 懒加载缩略图：从源视频取 start_time 对应帧
    const thumb = li.querySelector(".vcs-thumb");
    const tSec = thumb && thumb.dataset.thumbT;
    if (thumb && srcBase && tSec != null) {
      const url = API_PREFIX + "/video-analysis-search/search/frame?source=" +
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
 * @param {object} hit  片段对象。单视频路径：VAS_SEARCH.snap.hits[i]；
 *                      多视频路径（缓存勾选）：multi.hits[i]。
 *                      不再由播放器去 VAS_SEARCH.snap.hits 查 —— 多视频检索
 *                      不更新 VAS_SEARCH.snap，旧版读到 undefined 或脏 hit，
 *                      表现为点不动 / 播错段。
 * @param {object} [opts]
 * @param {string} [opts.source]   覆盖 VAS_SEARCH.snap.source（多视频用条目 source_path）。
 * @param {number} [opts.fps]      帧率；缺省从 VAS_SEARCH.snap.fps 取（多视频无 fps 字段时 fallback 25）。
 * @param {number} [opts.miIndex]  命中卡片 data-mi 索引，用于 active 高亮
 *                                  （多视频 children 混合，children 索引法失效）。
 */
function vcsPlaySegment(hit, opts) {
  opts = opts || {};
  if (!hit) return;
  const source = opts.source || (VAS_SEARCH.snap && VAS_SEARCH.snap.source) || "";
  if (!source) { vcsShowError("缺少视频源路径，无法播放"); return; }
  const start = Number(hit.start_time) || 0;
  const end = Number(hit.end_time) || start + 1;
  const dur = Math.max(0.5, end - start);
  const fps = Number(opts.fps)
    || (VAS_SEARCH.snap && Number(VAS_SEARCH.snap.fps))
    || 25;
  VAS_SEARCH.seg = { source, start, end, dur, fps,
              title: vcsFormatTime(start) + "–" + vcsFormatTime(end) };
  VAS_SEARCH.playing = true;
  VAS_SEARCH.playPaused = false;
  VAS_SEARCH.ended = false;

  const tag = vcsEl("vcs-player-tag");
  if (tag) tag.textContent = "正在播放 " + VAS_SEARCH.seg.title;
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
  vcsClosePreviewWs();   // 抢占 canvas：关掉视频源预览（含其按钮态）
  VAS_SEARCH.previewPlaying = false;
  VAS_SEARCH.previewPaused = false;
  vcsRestoreSegmentUI();  // 预览隐藏过进度条/时间，播片段前还原
  vcsSyncCanvasSize();
  vcsOpenPlayWs(source, start, end);
  if (typeof DemoGuard !== "undefined") {
    vasReport();
  }
}

function vcsOpenPlayWs(source, start, end) {
  vcsClosePlayWs(false);
  const gen = ++VAS_SEARCH.playGen;
  const fps = (VAS_SEARCH.snap && Number(VAS_SEARCH.snap.fps)) || 25;
  if (VAS_SEARCH.seg) {
    VAS_SEARCH.seg.frameCount = 0;
    VAS_SEARCH.seg.totalFrames = Math.max(1, Math.round((end - start) * fps));
  }
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}${API_PREFIX}/video-analysis-search/search/play`);
  VAS_SEARCH.playWs = ws;
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    if (gen !== VAS_SEARCH.playGen) return;
    ws.send(JSON.stringify({ source, start, end }));
  };
  ws.onmessage = (ev) => {
    if (gen !== VAS_SEARCH.playGen) return;
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
    if (gen === VAS_SEARCH.playGen) VAS_SEARCH.playWs = null;
  };
  ws.onerror = () => {
    if (gen !== VAS_SEARCH.playGen) return;
    try { ws.close(); } catch (_) {}
  };
}

function vcsClosePlayWs(settle) {
  VAS_SEARCH.playGen++;   // 作废上一连接的 onmessage/onclose/onerror 在途回调
  if (VAS_SEARCH.playWs) {
    try { VAS_SEARCH.playWs.close(); } catch (_) {}
    VAS_SEARCH.playWs = null;
  }
  if (VAS_SEARCH.lastImg) {
    URL.revokeObjectURL(VAS_SEARCH.lastImg);
    VAS_SEARCH.lastImg = null;
  }
  if (settle) {
    VAS_SEARCH.playing = false;
    VAS_SEARCH.ended = true;
  }
}

// ------------------------------------------------------------------ *
//  向导模式 / 手动预览:打开视频源(后端复用 vd 单例 VideoSession,        *
//  ws 帧流走 /api/video-analysis-search/source/ws)。                  *
//  与 vcsPlaySegment(检索命中片段回放)互斥 — 共用 #vcs-canvas,         *
//  不能同时写。打开预览前关掉检索播放,反之亦然。                       *
// ------------------------------------------------------------------ *
async function vcsOpenPreview(source, loop) {
  if (!source) { vcsShowError("请先选择视频源"); return; }
  vcsClosePlayWs(false);                 // 抢占 canvas:先关掉可能存在的检索播放
  await vcsClosePreviewWs();             // 重新打开前确保上一路 preview WS 已关
  try {
    const r = await fetch(API_PREFIX + "/video-analysis-search/source/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source, loop: !!loop }),
    });
    if (!r.ok) {
      const detail = await r.text().catch(() => "");
      vcsShowError("打开失败: HTTP " + r.status + " " + detail);
      return;
    }
    const snap = await r.json();
    if (snap && snap.error) { vcsShowError("打开失败: " + snap.error); return; }
    vcsShowError("");
    vcsSetSourceMeta("已选: " + source);
    vcsConnectPreviewWs();
    // 打开即播放：启用 暂停/停止，隐藏进度条与时间（实时流无时长）。
    VAS_SEARCH.previewPlaying = true;
    VAS_SEARCH.previewPaused = false;
    vcsSetPreviewUI(true);
    if (VAS_SEARCH._vcsSourceStatus) VAS_SEARCH._vcsSourceStatus(snap);
  } catch (e) {
    vcsShowError("打开失败: " + (e?.message || e));
  }
}

async function vcsClosePreview() {
  await vcsClosePreviewWs();
  try {
    await fetch(API_PREFIX + "/video-analysis-search/source/close", { method: "POST" });
  } catch (_) { /* ignore */ }
  VAS_SEARCH.previewPlaying = false;
  VAS_SEARCH.previewPaused = false;
  vcsClearCanvas();
  vcsSetPreviewUI(false);
  vcsSetSourceMeta("尚未选择");
  if (VAS_SEARCH._vcsSourceStatus) VAS_SEARCH._vcsSourceStatus(null);
}

function vcsConnectPreviewWs() {
  vcsClosePreviewWs().then(() => {
    const gen = ++VAS_SEARCH.previewGen;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}${API_PREFIX}/video-analysis-search/source/ws`);
    VAS_SEARCH.previewWs = ws;
    ws.binaryType = "arraybuffer";
    ws.onmessage = (ev) => {
      if (gen !== VAS_SEARCH.previewGen) return;
      // 检索片段播放激活时,preview 让位 — 让 play WS 独占 vcs-canvas。
      // 否则两路 ws 同时画帧会闪烁。
      if (VAS_SEARCH.playWs) return;
      if (typeof ev.data === "string") {
        let msg;
        try { msg = JSON.parse(ev.data); } catch (_) { return; }
        if (msg.type === "eof" || msg.type === "status") {
          if (VAS_SEARCH._vcsSourceStatus) VAS_SEARCH._vcsSourceStatus(msg);
        }
        return;
      }
      // binary JPEG — 与 vcsOpenPlayWs 共用 vcsDrawJpeg。
      vcsDrawJpeg(new Uint8Array(ev.data));
    };
    ws.onclose = () => {
      if (gen === VAS_SEARCH.previewGen) VAS_SEARCH.previewWs = null;
    };
    ws.onerror = () => {
      if (gen !== VAS_SEARCH.previewGen) return;
      try { ws.close(); } catch (_) {}
    };
  });
}

async function vcsClosePreviewWs() {
  VAS_SEARCH.previewGen++;
  if (VAS_SEARCH.previewWs) {
    try { VAS_SEARCH.previewWs.close(); } catch (_) {}
    VAS_SEARCH.previewWs = null;
  }
  if (VAS_SEARCH.lastImg) {
    URL.revokeObjectURL(VAS_SEARCH.lastImg);
    VAS_SEARCH.lastImg = null;
  }
}

// 清空播放画面（画黑 + 释放上一帧 objectURL）。关闭 / 停止预览与停止
// 片段回放都走这里 —— 之前 preview 路径只关 WS 不擦屏，导致关闭后画面
// 残留最后一帧。
function vcsClearCanvas() {
  const canvas = vcsEl("vcs-canvas");
  const ctx = canvas && canvas.getContext("2d");
  if (ctx) {
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
  }
  if (VAS_SEARCH.lastImg) {
    URL.revokeObjectURL(VAS_SEARCH.lastImg);
    VAS_SEARCH.lastImg = null;
  }
}

// 播放器控制区显隐 —— 视频源预览是实时流、没有天然时长，预览时隐藏
// 进度条与时间，只留「预览中」标签。
function vcsSetPreviewUI(on) {
  const bar = vcsEl("vcs-player-progress");
  const timeEl = vcsEl("vcs-player-time");
  const play = vcsEl("vcs-play");
  const pause = vcsEl("vcs-pause");
  const stop = vcsEl("vcs-stop-play");
  if (bar && bar.parentElement) bar.parentElement.hidden = !!on;
  if (timeEl) timeEl.hidden = !!on;
  if (play) play.disabled = true;          // 打开即播放 → 「播放」暂不可点
  if (pause) pause.disabled = !on;
  if (stop) stop.disabled = !on;
  const tag = vcsEl("vcs-player-tag");
  if (tag) tag.textContent = on ? "预览中" : "未播放";
}

// 恢复片段回放所需的 UI（进度条 / 时间可见）。预览隐藏过它们后，播片段
// 前必须还原，否则片段进度条不显示。
function vcsRestoreSegmentUI() {
  const bar = vcsEl("vcs-player-progress");
  const timeEl = vcsEl("vcs-player-time");
  if (bar && bar.parentElement) bar.parentElement.hidden = false;
  if (timeEl) timeEl.hidden = false;
}

// 视频源预览的播放 / 暂停 / 停止 —— 后端 VideoSession，与 vd 页同一套
// 语义（stop == close）。
async function vcsPreviewControl(action) {
  try {
    await fetch(API_PREFIX + "/video-analysis-search/source/control", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    });
  } catch (_) { /* ignore */ }
}

async function vcsPlayPreview() {
  if (!VAS_SEARCH.previewWs || !VAS_SEARCH.previewPaused) return;
  await vcsPreviewControl("play");
  VAS_SEARCH.previewPaused = false;
  VAS_SEARCH.previewPlaying = true;
  vcsEl("vcs-play").disabled = true;
  vcsEl("vcs-pause").disabled = false;
  if (typeof DemoGuard !== "undefined") vasReport();
}

async function vcsPausePreview() {
  if (!VAS_SEARCH.previewWs || !VAS_SEARCH.previewPlaying) return;
  await vcsPreviewControl("pause");
  VAS_SEARCH.previewPaused = true;
  VAS_SEARCH.previewPlaying = false;
  vcsEl("vcs-play").disabled = false;
  vcsEl("vcs-pause").disabled = true;
  if (typeof DemoGuard !== "undefined") vasReport();
}

async function vcsStopPreview() {
  await vcsPreviewControl("stop");
  await vcsClosePreviewWs();
  VAS_SEARCH.previewPlaying = false;
  VAS_SEARCH.previewPaused = false;
  vcsClearCanvas();
  vcsSetPreviewUI(false);
  vcsSetSourceMeta("尚未选择");
  if (VAS_SEARCH._vcsSourceStatus) VAS_SEARCH._vcsSourceStatus(null);
  if (typeof DemoGuard !== "undefined") vasReport();
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
  if (VAS_SEARCH.lastImg) URL.revokeObjectURL(VAS_SEARCH.lastImg);
  VAS_SEARCH.lastImg = URL.createObjectURL(blob);
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
  img.src = VAS_SEARCH.lastImg;
}

// 每收到一帧推进进度条 + 时间显示（按已收帧数 / 预计总帧数）
function vcsTickProgress() {
  if (!VAS_SEARCH.seg) return;
  VAS_SEARCH.seg.frameCount = (VAS_SEARCH.seg.frameCount || 0) + 1;
  const total = VAS_SEARCH.seg.totalFrames || 1;
  const p = Math.min(1, VAS_SEARCH.seg.frameCount / total);
  const bar = vcsEl("vcs-player-progress");
  if (bar) bar.style.width = (p * 100).toFixed(1) + "%";
  const timeEl = vcsEl("vcs-player-time");
  if (timeEl && VAS_SEARCH.seg.end) {
    const t = VAS_SEARCH.seg.start + p * (VAS_SEARCH.seg.end - VAS_SEARCH.seg.start);
    timeEl.textContent = vcsFormatTime(t) + " / " + vcsFormatTime(VAS_SEARCH.seg.end);
  }
}

// 播放完毕：停在末尾帧、进度条 100%，播放按钮可重播（重播需重开 WS）。
function vcsFinishSegment() {
  if (!VAS_SEARCH.seg) return;
  VAS_SEARCH.playing = false;
  VAS_SEARCH.ended = true;
  vcsClosePlayWs(false);
  const bar = vcsEl("vcs-player-progress");
  if (bar) bar.style.width = "100%";
  const timeEl = vcsEl("vcs-player-time");
  if (timeEl) timeEl.textContent = vcsFormatTime(VAS_SEARCH.seg.end) + " / " + vcsFormatTime(VAS_SEARCH.seg.end);
  vcsEl("vcs-play").disabled = false;
  vcsEl("vcs-pause").disabled = true;
  vcsEl("vcs-stop-play").disabled = false;
  if (typeof DemoGuard !== "undefined") {
    vasReport();
  }
}

function vcsPausePlayback() {
  if (!VAS_SEARCH.playing) return;
  VAS_SEARCH.playing = false;
  VAS_SEARCH.playPaused = true;
  if (VAS_SEARCH.playWs && VAS_SEARCH.playWs.readyState === 1) {
    try { VAS_SEARCH.playWs.send(JSON.stringify({ action: "pause" })); } catch (_) {}
  }
  vcsEl("vcs-play").disabled = false;
  vcsEl("vcs-pause").disabled = true;
  if (typeof DemoGuard !== "undefined") {
    vasReport();
  }
}

function vcsStopPlayback() {
  VAS_SEARCH.playing = false;
  VAS_SEARCH.ended = false;
  vcsClosePlayWs(false);
  VAS_SEARCH.seg = null;
  const bar = vcsEl("vcs-player-progress");
  if (bar) bar.style.width = "0%";
  const timeEl = vcsEl("vcs-player-time");
  if (timeEl) timeEl.textContent = "--";
  const tag = vcsEl("vcs-player-tag");
  if (tag) tag.textContent = "未播放";
  vcsEl("vcs-play").disabled = true;
  vcsEl("vcs-pause").disabled = true;
  vcsEl("vcs-stop-play").disabled = true;
  vcsClearCanvas();
  if (typeof DemoGuard !== "undefined") {
    vasReport();
  }
}

/* ------------------------- WS 连接 ------------------------- */
function connectVcsWs() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}${API_PREFIX}/video-analysis-search/search/ws`;
  const ws = new WebSocket(url);
  VAS_SEARCH.ws = ws;
  ws.onopen = () => {
    VAS_SEARCH.wsRetry = 0;
    vcsShowError("");
  };
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }
    if (msg.type === "hello" || msg.type === "state") {
      vcsApplySnapshot(msg);
    } else if (msg.type === "progress") {
      const snap = Object.assign({}, VAS_SEARCH.snap || {});
      snap.state = "extracting";
      snap.slices_embedded = msg.embedded;
      snap.slices_total = msg.total;
      vcsApplySnapshot(snap);
    } else if (msg.type === "error") {
      vcsShowError(msg.detail || "WS 错误");
    }
  };
  ws.onclose = () => {
    VAS_SEARCH.ws = null;
    const wait = Math.min(5000, 500 * Math.pow(2, VAS_SEARCH.wsRetry++));
    VAS_SEARCH.wsTimer = setTimeout(connectVcsWs, wait);
  };
  ws.onerror = () => {
    try { ws.close(); } catch (_) {}
  };
}

function closeVcsWs() {
  if (VAS_SEARCH.wsTimer) { clearTimeout(VAS_SEARCH.wsTimer); VAS_SEARCH.wsTimer = null; }
  if (VAS_SEARCH.ws) { try { VAS_SEARCH.ws.close(); } catch (_) {} VAS_SEARCH.ws = null; }
  VAS_SEARCH.wsRetry = 0;
}

/* ------------------------- REST 动作 ------------------------- */
async function vcsRefresh() {
  try {
    const r = await fetch(API_PREFIX + "/video-analysis-search/search");
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
  try {
    const r = await fetch(API_PREFIX + "/video-analysis-search/search/open", {
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
    // 切片参数（4s / 重叠 1s）固化为服务端常量，前端不收集。
    const r = await fetch(API_PREFIX + "/video-analysis-search/search/extract", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
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
    const r = await fetch(API_PREFIX + "/video-analysis-search/search/extract/stop", { method: "POST" });
    const data = await r.json();
    if (r.ok) vcsApplySnapshot(data);
  } catch (_) {}
}

// ---- 合并页统一入口（分析 / 检索按「功能配置」勾选并行发起） ----

// 统一刷新：VAS_SEARCH 快照 + VAS 快照/事件 + 统一历史列表。
function vasRefreshAll() {
  vcsRefresh();
  vasFetchSnapshot();
  vasFetchEvents();
  vcsCacheRefresh().catch(() => {});
  vasRefreshModeAvail().catch(() => {});
}

// DemoGuard 上报：合并页需要 VAS_SEARCH + VAS 两侧运行态。
function vasReport() {
  if (typeof DemoGuard === "undefined") return;
  DemoGuard.report("video-analysis-search", {
    search: VAS_SEARCH.snap || {},
    analysis: VAS.snap || {},
    busy: !!VAS_SEARCH.playing || (VAS_SEARCH.snap && VAS_SEARCH.snap.state === "extracting"),
  });
}

// 「开始」：按勾选并行发起分析 / 检索。选中分析历史记录时优先加载该记录，
// 并可在同时勾了「检索」且检索有可执行路径时并发检索。
async function vasStart() {
  // 输入快照：必须在最前面取 —— vasLoadRun → vasApplySnapshot 可能清空
  // #vcs-source（done 分支），之后再读就拿不到源了。
  const src = (vcsEl("vcs-source")?.value || "").trim();
  const wantAnalyze = !!vcsEl("vas-mode-analyze")?.checked;
  const wantSearch = !!vcsEl("vas-mode-search")?.checked;
  const anyChecked = VAS_SEARCH.cacheSel && VAS_SEARCH.cacheSel.size > 0;
  const qSearch = (vcsEl("vcs-text")?.value || "").trim();
  // 勾选态不再被 modeAvail 抖动自动清掉（见 vasUpdateButtons），故在点开始
  // 这一刻校验"选中的功能是否真的可用"——算力未启用时明确提示，而不是让
  // 请求打到后端 502/503 才报。
  const avail = VAS_SEARCH.modeAvail || { analyze: null, search: null };

  // ---- 分支 1：选中分析历史记录 → 加载（若勾了分析）+ 并发检索（若可执行）
  if (VAS_SEARCH.selRunId && VAS_SEARCH.selRun) {
    const searchRun = wantSearch && (anyChecked || !!src);
    if (!wantAnalyze && !searchRun) {
      vcsShowError("请勾选「分析」以加载历史记录，或勾选「检索」并选择缓存/视频源");
      return;
    }
    if (searchRun && !qSearch) {
      vcsShowError("请填写「检索」的检索问题"); return;
    }
    if (searchRun && avail.search === false) {
      vcsShowError(`「检索」功能未启用 —— 请先在「算力配置」启用 ${VAS_MODE_LABEL.search}`);
      return;
    }
    vcsShowError("");
    const tasks = [];
    if (wantAnalyze) tasks.push(vasLoadRun(VAS_SEARCH.selRunId));
    if (searchRun) tasks.push(vasStartSearch(src, anyChecked));
    await Promise.allSettled(tasks);
    vasUpdateButtons();
    return;
  }

  // ---- 分支 2：新任务 —— 按「实际会执行的功能」判定（与 vasUpdateButtons 同源）
  const analyzeRun = wantAnalyze && !!src;
  const searchRun = wantSearch && (!!src || anyChecked);
  if (!analyzeRun && !searchRun) {
    vcsShowError(wantAnalyze || wantSearch
      ? "请先选择视频文件，或在历史记录里勾选已缓存视频"
      : "请至少勾选「分析」或「检索」");
    return;
  }
  if (analyzeRun && avail.analyze === false) {
    vcsShowError(`「分析」功能未启用 —— 请先在「算力配置」启用 ${VAS_MODE_LABEL.analyze}`);
    return;
  }
  if (searchRun && avail.search === false) {
    vcsShowError(`「检索」功能未启用 —— 请先在「算力配置」启用 ${VAS_MODE_LABEL.search}`);
    return;
  }
  if (searchRun && !qSearch) {
    vcsShowError("请填写「检索」的检索问题"); return;
  }
  vcsShowError("");
  const tasks = [];
  if (analyzeRun) tasks.push(vasStartAnalyze());
  if (searchRun) tasks.push(vasStartSearch(src, anyChecked));
  await Promise.allSettled(tasks);
  vasUpdateButtons();
}

// 「检索」分支：勾选缓存 → 直接多视频检索；索引已就绪且源未变 → 只重检索；
// 否则先提取，提取完成（ready）后自动检索一次（见 vcsApplySnapshot）。
async function vasStartSearch(src, anyChecked) {
  if (anyChecked) { await vcsQuery(); return; }
  const st = (VAS_SEARCH.snap && VAS_SEARCH.snap.state) || "idle";
  // _progressForceIdle：刚清空检索结果 / 清空缓存，即使有旧 ready 快照被
  // WS 刷回也不能短路跳过提取（该标志在下次 extracting 时自动清除）。
  if (st === "ready" && !VAS_SEARCH._progressForceIdle
      && VAS_SEARCH.snap && VAS_SEARCH.snap.source === src) {
    await vcsQuery(); return;
  }
  VAS_SEARCH.pendingQuery = true;
  await vcsStartExtract();
}

// 「停止」：切片提取与逐帧分析都停（幂等）。
async function vasStop() {
  await Promise.allSettled([vcsStopExtract(), vasStopAnalyze()]);
  vasUpdateButtons();
}

// 检索进行中可取消（40s 超时兜底，即使后端极端卡死也能反馈）
let vcsQueryAbort = null;

// 匹配阈值(0.40) / 检索上限(10) / 切片参数(4s, 重叠 1s) 固化为服务端
// 常量（app/core/video/search.py 的 FIXED_*），页面不收集。这里只保留
// 「宽松匹配」的兜底条数，与服务端检索上限保持一致。
const VCS_LENIENT_TOP_K = 10;

async function vcsQuery() {
  const text = (vcsEl("vcs-text")?.value || "").trim();
  // 兜底校验：「开始」在问题为空时已被 vasUpdateButtons 禁用，但本函数
  // 还有第二条入口 —— 提取完成后 vcsApplySnapshot 的 pendingQuery 自动
  // 触发；提取期间（~90s）用户可能把问题清空，此守卫避免把空问题打到
  // 后端（否则提示退化成 "启动失败: HTTP 400 检索问题不能为空"）。
  if (!text) { vcsShowError("请输入检索问题"); return; }
  if (vcsQueryAbort) return;  // 已在检索中，防重复点击
  // 注意：合并页没有独立的"检索"按钮（检索由统一「开始」触发，#vas-start
  // 的可用性 / 文案由 vasUpdateButtons 统一维护）。这里曾误用
  // vcsEl("vas-query") —— 那是「分析反查」的 modal 容器 div，给它赋
  // textContent 会把整个 modal 的内容清空，导致之后点「分析反查」时内部
  // 元素（#vas-query-source 等）不存在、modal 打不开（"点反查没反应"）。
  const countEl = vcsEl("vcs-results-count");
  // 宽松匹配在两条路径里都要读（分叉后各自携带 min_results 语义）
  const lenient = !!vcsEl("vcs-lenient")?.checked;
  // ---- 双路径分叉（在设置全局 busy 态之前）：勾选 ≥1 缓存条目 →
  // 多视频检索（query_multi，自带完整的 busy 态生命周期）；否则走
  // 原单视频 query。两路排他（勾选优先）且数据面隔离 —— multi 只读
  // LanceDB、不触碰单例的内存索引；ready 状态下取消勾选再检索，
  // 单例索引原样可用。
  if (VAS_SEARCH.cacheSel && VAS_SEARCH.cacheSel.size > 0) {
    await vcsQueryMulti(text, lenient ? VCS_LENIENT_TOP_K : 0);
    return;
  }
  vcsQueryAbort = new AbortController();
  const timer = setTimeout(() => vcsQueryAbort.abort(), 40000);
  if (countEl) countEl.textContent = "检索中…";
  try {
    const body = { text };
    // 严格模式（默认）：固定阈值 0.40 过滤后无命中就返回空。
    // 勾选"宽松匹配" → min_results=10，启用 fallback：
    //   阈值过滤完后无命中时补回 top-10（无视阈值，标"低于阈值"）。
    if (lenient) body.min_results = VCS_LENIENT_TOP_K;
    const r = await fetch(API_PREFIX + "/video-analysis-search/search/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: vcsQueryAbort.signal,
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "HTTP " + r.status);
    vcsApplySnapshot(data);
  } catch (err) {
    if (countEl) countEl.textContent = "--";
    if (err && err.name === "AbortError") {
      vcsShowError("检索超时：40s 内未完成，请重试");
    } else {
      vcsShowError(err.message);
    }
  } finally {
    clearTimeout(timer);
    vcsQueryAbort = null;
    vasUpdateButtons();
  }
}

function initVideoSearch() {
  if (VAS_SEARCH.initialized) return;
  VAS_SEARCH.initialized = true;

  const refreshBtn = vcsEl("vcs-refresh");
  const browseBtn = vcsEl("vcs-browse");
  const startBtn = vcsEl("vas-start");
  const stopBtn = vcsEl("vas-stop");
  const errClose = vcsEl("vcs-error-close");
  const resultsClear = vcsEl("vcs-results-clear");
  const sourceEl = vcsEl("vcs-source");
  const textEl = vcsEl("vcs-text");

  const syncSourceMeta = () => {
    const v = (sourceEl?.value || "").trim();
    vcsSetSourceMeta(v ? "已选: " + v : "尚未选择");
    vasUpdateButtons();
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
  refreshBtn?.addEventListener("click", vasRefreshAll);
  startBtn?.addEventListener("click", vasStart);
  stopBtn?.addEventListener("click", vasStop);

  // 帧判据状态接入（VAS 不渲染判据 UI）—— 与 vd / vtd / vtl 共用同一份组件
  // （app/static/core/frame-judge.js）。VAS 走自己的判据端点，因为它的
  // 判据挂在 VideoAnalysisService 上而不是共用的 VideoSession。
  // VAS 的判据参数（算法 / 阈值 / 帧间隔）已固化为服务端常量：
  //   * showConfig:false → 不渲染配置控件
  //   * showCurve:false  → 不渲染「实时分数曲线」（参数不可调，曲线没有
  //     调参价值；且向导视频源步的「打开」只是共享 VideoSession 的预览，
  //     不创建 VAS 判据器，曲线只会停在「采集中…」）
  // 浮框同样不存在：v0.23 起浮框由宫格 tile 角标（MultiGrid.setBadge）
  // 承载，而 VAS 是单块 canvas、不是宫格。这里仍 mount 只为让 update()
  // 继续接收本轮判据状态（供后续诊断 / 扩展），不产生可见 UI。
  // syncControls:false —— 每 tick 推来的是**本轮真实**判据状态，不能回写。
  window.FrameJudge?.mount({
    prefix: "vas",
    canvasId: "vcs-canvas",
    apiBase: "/api/video-analysis-search/judge",
    syncControls: false,
    showConfig: false,
    showCurve: false,
  });

  // 向导模式 / 手动预览 — 复用 vd 单例 VideoSession 跑连续预览帧流。
  // 与 vd / vtd / vtl 的「打开 / 关闭」按钮行为一致:open 把 source/loop
  // 写回后端,然后把后端 WS 帧流接到 #vcs-canvas(已被 wizard 搬到
  // canvasWrap 内,getElementById 不变,直接画就行)。
  const openBtn  = vcsEl("vcs-open");
  const closeBtn = vcsEl("vcs-close");
  const loopEl   = vcsEl("vcs-loop");
  openBtn?.addEventListener("click", () => {
    const src = (sourceEl?.value || "").trim();
    vcsOpenPreview(src, !!(loopEl && loopEl.checked));
  });
  closeBtn?.addEventListener("click", () => {
    vcsClosePreview();
  });
  // loop 同步到后端:不重 open(避免断流),走独立控制端点 (暂用 close+open)
  loopEl?.addEventListener("change", async () => {
    if (!VAS_SEARCH.previewWs) return;
    const src = (sourceEl?.value || "").trim();
    if (!src) return;
    try { await fetch(API_PREFIX + "/video-analysis-search/source/close", { method: "POST" }); }
    catch (_) {}
    vcsOpenPreview(src, !!loopEl.checked);
  });
  // 功能勾选切换 → 重算按钮 / 子栏启用态
  vcsEl("vas-mode-analyze")?.addEventListener("change", vasUpdateButtons);
  vcsEl("vas-mode-search")?.addEventListener("change", vasUpdateButtons);
  // 勾选路径（缓存窗口）：刷新按钮 + 进入页面即拉一次列表 +
  // 路径输入变化触发互斥重算（vcsApplyMutualExclusion 内部含
  // checkbox disabled 反向）。
  vcsEl("vcs-cache-refresh")?.addEventListener("click", vcsCacheRefresh);
  vcsEl("vcs-source")?.addEventListener("input", vcsApplyMutualExclusion);
  // 「分析反查」（操作栏）：目标 = 选中的历史记录优先，其次当前已加载的
  // 分析 run（有 run_id 且未被「清空」）。与 vasUpdateButtons 判定同源；
  // 不可用时点击给出明确原因，不再静默 return。
  // **不做 VAS.state 拦截** —— 加载历史记录时后端 load_run 会把 state 重置
  // 为 idle，且 /analysis 快照与 WS 推送交替写 VAS.state 可能有瞬时抖动，
  // 若依赖 state 判定会出现"点一下没反应"。反查走 /runs/{id}/query 纯读盘，
  // 不需要当前时刻处于非运行态。
  vcsEl("vas-run-query")?.addEventListener("click", () => {
    const t = (VAS_SEARCH.selRunId && VAS_SEARCH.selRun) ? VAS_SEARCH.selRun
      : (VAS.snap && VAS.snap.run_id
         && VAS.snap.run_id !== VAS._clearedRun) ? VAS.snap : null;
    if (!t) {
      vcsShowError("暂无可反查的分析记录 —— 请先完成一次分析，或在历史记录中选中一条");
      return;
    }
    vasQueryOpen(t.run_id, t.source || "",
                 t.total_frames || 0, t.fps || 0);
  });
  vcsCacheRefresh().catch(() => {});
  const clearCacheBtn = vcsEl("vcs-clear-cache");
  clearCacheBtn?.addEventListener("click", async () => {
    let stats = null;
    try {
      const r = await fetch(API_PREFIX + "/video-analysis-search/search/cache/stats");
      if (r.ok) stats = await r.json();
    } catch (_) {}
    const nR = stats?.videos || 0;
    const nA = stats?.analysis_videos || 0;
    const n = nR + nA;
    if (!confirm(n > 0
        ? `确认清空全部缓存？(检索 ${nR} 条 + 分析 ${nA} 条；清空后需重新分析)`
        : "确认清空全部缓存？")) return;
    try {
      const r = await fetch(API_PREFIX + "/video-analysis-search/search/cache/clear",
                            { method: "POST" });
      const d = await r.json();
      const k = d.videos_cleared || 0;
      alert(k > 0
            ? `已清空 ${k} 条视频的缓存（检索 + 分析）。`
            : "缓存已空（或缓存未启用），无需清理。");
      // 清空缓存 = 连会话索引一并作废（后端 reset_session 已广播 idle）。
      // 本地立即复位：丢弃 ready 快照与"缓存窗口已刷新"标记，让下次
      // 「开始」不再被 vasStartSearch 的 ready+同源短路，真正重新提取。
      VAS_SEARCH.snap = null;
      VAS_SEARCH._cacheWinOnReady = null;
      VAS_SEARCH.pendingQuery = false;
      vcsResetProgress();
      vcsCacheRefresh().catch(() => {});
    } catch (e) {
      alert("清空缓存失败：" + e);
    }
  });
  errClose?.addEventListener("click", () => vcsShowError(""));
  resultsClear?.addEventListener("click", async () => {
    // 清空检索结果：调后端清单例的 query / hits —— 单视频结果存在服务端
    // 单例里，只清前端列表的话下一次 WS 快照又会把它渲染回来。
    // （旧实现用 POST /query {text:"清空占位", threshold:1.0} 取巧"无命中"，
    // 既会白跑一次 NPU embedding，又把 _query 留成"清空占位"。）
    try {
      const r = await fetch(API_PREFIX + "/video-analysis-search/search/query/clear",
                            { method: "POST" });
      if (r.ok) vcsApplySnapshot(await r.json());
    } catch (_) {}
    // 勾选（多视频）路径下 vcsApplySnapshot 不覆盖结果列表 → 显式清一次
    const list = vcsEl("vcs-results-list");
    if (list) {
      list.innerHTML =
        '<li class="vcs-results-empty">完成提取后输入问题检索，命中片段显示在这里。</li>';
    }
    const count = vcsEl("vcs-results-count");
    if (count) count.textContent = "0 条";
    // 清空检索结果 = 复位检索面板：进度条回初始态（forceIdle 保持到下次提取）。
    vcsResetProgress();
    // 顺带清空上方播放窗口 —— 正在播放 / 暂停的片段一并停止并复位
    // （停 WS + 涂黑 canvas + overlay 恢复提示 + 三按钮置灰），复用「停止」
    // 按钮的同一函数，天然幂等（没在播时调用无害）。
    vcsStopPlayback();
  });

  // 50 字计数（检索问题）
  bindTaCounter(textEl, vcsEl("vcs-ta-count"));

  // 播放器按钮 —— 按当前模式分派：视频源预览（previewWs）走 VideoSession
  // 控制（播放/暂停/停止，与 vd 页同款），检索片段回放走 /search/play。
  vcsEl("vcs-play").addEventListener("click", () => {
    if (VAS_SEARCH.previewWs) { vcsPlayPreview(); return; }
    if (!VAS_SEARCH.seg || VAS_SEARCH.playing) return;
    // 暂停中恢复：发 WS resume
    if (VAS_SEARCH.playPaused && VAS_SEARCH.playWs && VAS_SEARCH.playWs.readyState === 1) {
      VAS_SEARCH.playPaused = false;
      VAS_SEARCH.playing = true;
      try { VAS_SEARCH.playWs.send(JSON.stringify({ action: "resume" })); } catch (_) {}
      vcsEl("vcs-play").disabled = true;
      vcsEl("vcs-pause").disabled = false;
      if (typeof DemoGuard !== "undefined") {
        vasReport();
      }
      return;
    }
    // 播完重播：从头重开 WS
    if (VAS_SEARCH.seg && VAS_SEARCH.seg.source) {
      VAS_SEARCH.ended = false;
      const bar = vcsEl("vcs-player-progress");
      if (bar) bar.style.width = "0%";
      const timeEl = vcsEl("vcs-player-time");
      if (timeEl) timeEl.textContent = vcsFormatTime(VAS_SEARCH.seg.start) + " / " + vcsFormatTime(VAS_SEARCH.seg.end);
      vcsEl("vcs-play").disabled = true;
      vcsEl("vcs-pause").disabled = false;
      vcsOpenPlayWs(VAS_SEARCH.seg.source, VAS_SEARCH.seg.start, VAS_SEARCH.seg.end);
      VAS_SEARCH.playing = true;
      if (typeof DemoGuard !== "undefined") {
        vasReport();
      }
    }
  });
  vcsEl("vcs-pause").addEventListener("click", () => {
    if (VAS_SEARCH.previewWs) { vcsPausePreview(); return; }
    vcsPausePlayback();
  });
  vcsEl("vcs-stop-play").addEventListener("click", () => {
    if (VAS_SEARCH.previewWs) { vcsStopPreview(); return; }
    vcsStopPlayback();
  });

  vcsRefresh();
  if (!VAS_SEARCH.ws && !VAS_SEARCH.wsTimer) connectVcsWs();
}

/* ------------------------------------------------------------------ *
 *  video-analysis-search page — 分析侧 (后端 VLM 对接)                           *
 *                                                                    *
 *  后端: app/apps/video_analysis_search/service.py —— 独立 wdnav.VideoCapture*
 *  + 后台线程,每 N 帧调一次 ComputeRegistry.get_session("swcap_video_describe")*
 *  出 per-frame 文本,文件走完后跑综述。前端通过 WS 订阅新事件与综述  *
 *  就绪通知,REST 兜底拉取初始快照。                                    *
 * ------------------------------------------------------------------ */
const VAS = {
  ws: null,
  wsRetry: 0,
  wsTimer: null,
  state: "idle",
  snap: null,                  // 最近一次 VAS state 快照（反查当前视频的数据源）
  events: [],
  summary: "",
  // 缓存最近一次 state 推送里的元信息；进度推进靠它 + VAS.events.length
  // 实时计算 —— 避免每帧 VLM 完成都要等下一次 state 推送（之前是 2s
  // 节流，导致进度条"跟不上"分析）。
  meta: { total_frames: 0, every_n: 30, started_at: 0, finished_at: 0, fps: 0,
          sampleMode: "manual", framePos: 0 },
  _progressForceIdle: false,   // 清空"分析结果"后强制复位进度条，直到下次运行
  _srcClearedRun: "",          // 已清空视频源的 run_id（防止同一 run 重复清空）
  _clearedRun: "",             // 已清空"分析结果"的 run_id（反查目标排除它）
  init() { return initVideoFileAnalysis(); },
};

function vasSetStatus(text, isError) {
  const el = document.getElementById("vas-status");
  if (!el) return;
  el.textContent = text || "";
  el.style.color = isError ? "#b91c1c" : "";
}

// 顶部「分析: xxx」简洁状态（详细进度见操作区的 vasFormatRuntime）。
function vasFormatStatusShort(snap) {
  if (!snap) return "分析: 未启动";
  switch (snap.state) {
    case "idle":        return "分析: 未启动";
    case "opening":     return "分析: 打开中";
    case "running":     return "分析: 分析中";
    case "summarizing": return "分析: 综述中";
    case "done":        return "分析: 已完成";
    case "stopped":     return "分析: 已停止";
    case "error":       return "分析: 错误";
    default:            return "分析: " + snap.state;
  }
}

function vasSetRuntime(text) {
  const el = document.getElementById("vas-runtime");
  if (!el) return;
  el.textContent = text || "未启动";
}

function vasSetSourceMeta(text) {
  const el = document.getElementById("vcs-source-meta");
  if (!el) return;
  el.textContent = text || "尚未选择";
}

function vasShowError(text) {
  // 合并页共用顶部错误 banner（原 VAS 独立 banner 已并入 #vcs-error-banner）。
  vcsShowError(text);
}

function vasRenderSnapshot(snap) {
  const pre = document.getElementById("data-video-analysis-search-analysis");
  if (pre) pre.textContent = JSON.stringify(snap, null, 2);
}

function vasFormatRuntime(snap) {
  // 操作区 header 的「分析: xxx」**短状态**。详细帧进度（已读/已分析/
  // 队列）在进度条 meta 行（vasUpdateProgress），避免长文案撑破标题行。
  if (!snap) return "未启动";
  switch (snap.state) {
    case "idle":        return "未启动";
    case "opening":     return "打开中";
    case "running":     return "分析中";
    case "summarizing": return "综述中";
    case "done":        return "已完成";
    case "stopped":     return "已停止";
    case "error":       return "错误";
    default:            return snap.state || "未启动";
  }
}

function vasRefreshButtons(snap) {
  // 按钮已合并到统一的「开始」/「停止」（由 vasUpdateButtons 计算），
  // 这里只把 VAS 运行态同步给统一按钮。
  VAS.state = (snap && snap.state) || VAS.state;
  vasUpdateButtons();
}

function vasAppendEvent(ev) {
  // 服务端 event id 严格递增 —— 客户端也按 id 升序。
  VAS.events.push(ev);
  if (VAS.events.length > 5000) VAS.events.splice(0, VAS.events.length - 5000);
  vasRenderEventList();
  // 每条 event = 一帧 VLM 完成 → 即时推进进度条（不等 2s state 节流）
  vasUpdateProgress();
}

// 根据状态自动折叠/展开"逐帧分析" details：
//   running / opening / summarizing   → 展开（用户主动分析中要看事件）
//   done / stopped / error / idle     → 折叠（不再追加新事件，腾出空间给综述）
// 用户后续可以手动点 summary 重新展开看历史结果。
//
// 注意：只在前一个状态和当前状态**不同**时同步，避免 WS 每 ~2s 推一次
// state 推送就把用户手动展开的 details 合上。只在 idle ↔ running 这种
// "语义转换" 上才动 DOM。
function vasSyncEventsCollapse(state) {
  const details = document.getElementById("vas-events-details");
  if (!details) return;
  const prev = VAS._lastCollapseState;
  if (prev === state) return;          // 同一状态重复推送：不动
  VAS._lastCollapseState = state;
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

function vasRenderEventList() {
  const list = document.getElementById("vas-event-list");
  const count = document.getElementById("vas-events-count");
  if (!list || !count) return;
  count.textContent = VAS.events.length + " 条";
  if (VAS.events.length === 0) {
    list.innerHTML = '<li class="vas-event-empty">尚未开始分析。</li>';
    return;
  }
  list.innerHTML = "";
  // 新的在前 —— 视觉上突出最新结果
  for (let i = VAS.events.length - 1; i >= 0; i--) {
    const ev = VAS.events[i];
    const li = document.createElement("li");
    li.className = "vas-event" + (i === VAS.events.length - 1 ? " latest" : "");
    const head = document.createElement("div");
    head.className = "vas-event-head";
    const fp = document.createElement("span");
    fp.className = "vas-event-frame";
    fp.textContent = "帧 " + ev.frame_pos;
    const ts = document.createElement("span");
    ts.className = "vas-event-ts";
    ts.textContent = fmtEventTime(ev.ts);
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = "#" + ev.id;
    head.appendChild(fp);
    head.appendChild(ts);
    head.appendChild(tag);
    const text = document.createElement("div");
    text.className = "vas-event-text";
    text.textContent = ev.text || "(空)";
    li.appendChild(head);
    li.appendChild(text);
    list.appendChild(li);
  }
}

function vasSetSummary(text) {
  const emptyEl = document.getElementById("vas-summary-empty");
  const textEl  = document.getElementById("vas-summary-text");
  const metaEl  = document.getElementById("vas-summary-meta");
  VAS.summary = text || "";
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

function vasApplySnapshot(snap) {
  if (!snap) return;
  VAS.snap = snap;      // 「分析反查」当前视频目标的数据源（run_id/source/total_frames/fps）
  VAS.state = snap.state;
  // 缓存元信息（total_frames / every_n / 时间戳），后续每帧 event 推
  // 送时按这个 + VAS.events.length 推进进度条，不依赖 state 节流。
  VAS.meta.total_frames = Number(snap.total_frames) || 0;
  VAS.meta.every_n      = Number(snap.every_n) || 30;
  // 采样模式 + 当前解码位置：自动模式下进度条用 framePos/totalFrames
  // 作分母（采样数事前不可知，见 vasUpdateProgress 的 auto 分支）。
  VAS.meta.sampleMode  = String(snap.sample_mode || "manual");
  VAS.meta.framePos    = Number(snap.frame_pos) || 0;
  // judge 快照：**本轮真实**的判据状态（分数环形缓冲 + 当前判定），
  // 与 vd / vtd / vtl 的 snap.judge 同结构。VAS 当前不渲染曲线 / 浮框，
  // 仍喂给组件以保持状态同步（不产生可见 UI）。
  window.FrameJudge?.update("vas", snap.judge);
  VAS.meta.started_at   = Number(snap.started_at) || 0;
  VAS.meta.finished_at  = Number(snap.finished_at) || 0;
  VAS.meta.fps          = Number(snap.fps) || 0;
  vasSetRuntime(vasFormatRuntime(snap));
  // 标题栏小字由 vasSyncStatusText 统一维护（见 vcsApplySnapshot 同款注释）；
  // vasRefreshButtons → vasUpdateButtons 末尾会同步。
  vasRefreshButtons(snap);
  vasRenderSnapshot(snap);
  vasUpdateProgress(snap);
  // 分析完成（done）后清空视频源路径文本框（仅成功清空，stopped/error
  // 保留便于重试），与检索提取完成后的行为一致，引导后续走历史记录。
  if (snap.state === "done" && snap.run_id
      && VAS._srcClearedRun !== snap.run_id) {
    VAS._srcClearedRun = snap.run_id;
    const srcEl = document.getElementById("vcs-source");
    if (srcEl && srcEl.value) {
      srcEl.value = "";
      vcsSetSourceMeta("分析完成，路径已清空 —— 可在历史记录中加载/检索");
      if (typeof vcsApplyMutualExclusion === "function") vcsApplyMutualExclusion();
    }
  }
  // 状态切换时同步"逐帧分析"details的折叠状态：
  // running / opening / summarizing → 展开，done / stopped / error / idle → 折叠
  vasSyncEventsCollapse(snap.state);
  if (snap.state === "error" && snap.error) vasShowError(snap.error);
  if (snap.state === "running" || snap.state === "opening") vasShowError("");
  // DemoGuard：合并页离开守卫需要 VAS_SEARCH + VAS 两侧运行态。
  vasReport();
}

const _VAS_PHASE_LABEL = {
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
 *   total_frames = VAS.meta.total_frames（来自最近一次 state 推送）
 *   expected     = 分母，按判据模式分两种（**v0.18 判据下沉**）：
 *                 * manual（默认，旧行为不变）
 *                   expected = ⌊ total_frames / every_n ⌋
 *                   后端 _decode_loop 只在 cur_pos % every_n == 0 且
 *                   cur_pos <= total_frames 时采样（位置 every_n, 2*every_n,
 *                   ..., floor(total_frames/every_n)*every_n），所以样本数
 *                   是 floor 而不是 ceil。之前用 ceil 导致 totalFrames 不是
 *                   every_n 整数倍时进度条永远停在 (N-1)/N。
 *                 * 判帧模式（conc 等判帧算法名，按画面变化采样）
 *                   expected = total_frames，分子用 frame_pos 而非
 *                   analyzed —— 采样数事前不可知（由画面变化决定），
 *                   floor(total/every_n) 只是兜底间隔的上限，用它当分母
 *                   会让进度条长期趴在低位。
 *   analyzed     = 优先用 ``snap.analyzed``（state API 的权威计数），
 *                 没有 snap 时退到 ``VAS.events.length``。
 *                 **不要直接用 events.length**：events API 与 state API 是
 *                 两次请求，在 done 状态切换瞬间 events 端可能延迟一拍，
 *                 导致用户多次刷新看到 ratio 在 70%→86%→100% 之间
 *                 跳变（bar 一会儿不满格一会儿满格）。用 snap.analyzed
 *                 与 state 同源，永远不会出现这种 flicker。
 *   progress     = analyzed / expected（done 状态下强制 ratio=1）
 */
function vasUpdateProgress(snap) {
  const root      = document.getElementById("vas-progress");
  const phaseEl   = document.getElementById("vas-progress-phase");
  const metaEl    = document.getElementById("vas-progress-meta");
  const trackEl   = document.getElementById("vas-progress-track");
  const fillEl    = document.getElementById("vas-progress-analyze");
  const nowEl     = document.getElementById("vas-progress-count-now");
  if (!root || !trackEl || !fillEl || !nowEl) return;

  const state = (snap?.state) || VAS.state || "idle";

  // 清空"分析结果"后强制复位：在下次进入运行态（点「开始」）之前，
  // 状态推送不再把进度条刷回 done/100%。running/opening/summarizing
  // 视为新 run 开始 → 解除标志，恢复正常渲染。
  if (VAS._progressForceIdle) {
    if (state === "running" || state === "opening" || state === "summarizing") {
      VAS._progressForceIdle = false;
    } else {
      return;
    }
  }

  // --- 签名缓存：state 推送每 2s 来一次但内容常相同；无变化就完全
  // 跳过 DOM 操作（包括 reflow），避免选视频源时界面抖动。
  // 注意：event 推送没有 snap，必须每次都跑（analyzed 变了）。
  // summarizing 阶段必须把 summary_progress 也纳入签名，否则 chunk 完成时
  // 进度条不刷新（用户看不到 "生成综述中 3/10"）。
  const summaryProgress = snap && Array.isArray(snap.summary_progress)
    ? snap.summary_progress.join(",")
    : (VAS.meta.summaryProgress || "0,0");
  if (snap && !VAS._lastSummaryProgress) VAS._lastSummaryProgress = "0,0";
  const sig = [
    state,
    VAS.meta.total_frames,
    VAS.meta.every_n,
    // 模式进签名：切模式会换分母公式（analyzed/expected vs
    // frame_pos/total_frames），不进签名会导致进度条停在旧公式的结果上。
    VAS.meta.sampleMode,
    VAS.events.length,
    VAS.meta.fps,
    VAS.meta.started_at,
    VAS.meta.finished_at,
    summaryProgress,
    snap ? snap.frame_pos : "",
    snap ? snap.sampled : "",
  ].join("|");
  if (snap && sig === VAS._progressSig) return;
  VAS._progressSig = sig;
  if (snap) VAS._lastSummaryProgress = summaryProgress;

  root.dataset.state = state;
  if (phaseEl) phaseEl.textContent = "分析: " + (_VAS_PHASE_LABEL[state] || state);

  // meta 行（fps + 帧进度 + 已耗时）—— 只有 state 推送才携带时间戳，所以
  // 这里在 event 推送时跳过，避免跳变；用定时器补一个轻量刷新即可。
  // summarizing 阶段也展示已耗时，让用户感知还在干活（不是卡死）。
  // 「已读 / 已分析 / 队列」原在操作区 header 的长文案里，会撑破标题行，
  // 现移到本行（信息不丢，且 header 只留短状态）。
  if (metaEl && snap) {
    const metaParts = [];
    if (VAS.meta.fps > 0) metaParts.push(fmtNum(VAS.meta.fps, 1) + " fps");
    if (state === "running" || state === "opening") {
      const total = snap.total_frames > 0
        ? snap.frame_pos + "/" + snap.total_frames
        : "帧 " + snap.frame_pos;
      const sampled  = Number(snap.sampled)  || 0;
      const analyzed = Number(snap.analyzed) || 0;
      const inflight = Math.max(0, sampled - analyzed);
      metaParts.push("已读 " + total);
      metaParts.push(inflight > 0
        ? `已分析 ${analyzed}/${sampled}（队列 ${inflight}）`
        : `已分析 ${analyzed}/${sampled}`);
    }
    if (state === "summarizing") {
      const sp = snap.summary_progress || [0, 0];
      if (sp[1] > 0) metaParts.push(`chunk ${sp[0]}/${sp[1]}`);
      const an = Number(snap.analyzed) || 0;
      if (an > 0) metaParts.push(`共分析 ${an} 帧`);
    }
    if ((state === "done" || state === "stopped")
        && Number(snap.analyzed) > 0) {
      metaParts.push(`共分析 ${snap.analyzed} 帧`);
    }
    if (VAS.meta.started_at > 0 &&
        (state === "running" || state === "opening" ||
         state === "summarizing" ||
         state === "done" || state === "stopped" || state === "error")) {
      const end = VAS.meta.finished_at > 0
        ? VAS.meta.finished_at
        : Date.now() / 1000;
      const elapsed = end - VAS.meta.started_at;
      if (elapsed > 0) metaParts.push("已耗时 " + Math.round(elapsed) + "s");
    }
    metaEl.textContent = metaParts.join(" · ") || "--";
  }

  // --- 进度：analyzed / expected -------------------------------------
  // summarizing 阶段：把进度比换成 chunk 完成度（[done, total]）。
  const totalFrames = VAS.meta.total_frames;
  const everyN      = VAS.meta.every_n;
  const sampleMode  = (snap?.sample_mode || VAS.meta.sampleMode || "manual");
  const framePosNow = (snap && Number.isFinite(snap.frame_pos))
    ? Number(snap.frame_pos)
    : (VAS.meta.framePos || 0);
  // 优先用 snap.analyzed（state API 的权威计数）。没有 snap 时退到 events.length。
  // 关键：events API 与 state API 是两次独立 HTTP 请求，在 done 状态切换瞬间
  // events 端可能延迟 1-2 帧，导致 ratio 在 70%→86%→100% 跳变（用户多次刷新
  // 看到进度条一会儿不满格一会儿满格）。用 snap.analyzed 与 state 同源 → 一次
  // state 推送期间 analyzed 值稳定，进度条就不会 flicker。
  const analyzed    = (snap && Number.isFinite(snap.analyzed))
    ? Number(snap.analyzed)
    : VAS.events.length;

  let expected = 0;
  let ratio = null;
  // 进度条只在"真正在干活或刚干完"的状态下显示百分比：
  //   running / opening  → 实跑帧数（analyzed / expected）
  //   summarizing       → chunk 完成度
  //   done              → 100% 满格（强制 ratio=1，与 events.length 解耦）
  // idle / stopped / error 一律不显示百分比（哪怕 VAS.meta 残留上次运行的
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
  } else if (sampleMode && sampleMode !== "manual") {
    // **判帧模式不能用 analyzed / floor(totalFrames/everyN)**：
    // 采样数由"画面是否变化"决定，事前不可知 —— floor(total/every_n)
    // 只是兜底间隔对应的**上限**，绝大多数时候远大于实际采样数，用它
    // 做分母会让进度条长期趴在低位（甚至分析完了才 20%）。
    // 改用**解码进度**（frame_pos / total_frames）作为分母：判帧模式
    // 下"处理了多少帧"与"分析了多少帧"是两个不同的问题，而用户想看到
    // 的恰恰是"整段视频扫到哪儿了"。
    //（历史行的 sample_mode 可能是字面量 "auto"，同样走这条分支。）
    if (totalFrames > 0 && framePosNow > 0) {
      expected = totalFrames;
      ratio = Math.min(1, framePosNow / expected);
    } else {
      ratio = null;  // 拿不到总量 → indeterminate（live 流同理）
    }
  } else if (totalFrames > 0 && everyN > 0) {
    // 手动模式：采样数事前可知 = floor(total / every_n)，原公式不变。
    expected = Math.max(1, Math.floor(totalFrames / everyN));
    ratio = Math.min(1, analyzed / expected);
  }

  // 只输出数值 —— "%" 由 #vas-progress-count-unit 的静态 span 提供，
  // 若这里也带 "%" 会拼成 "0%%"。
  let percentText;
  if (!showPercent) {
    percentText = "0";
  } else if (ratio !== null) {
    percentText = String(Math.round(ratio * 100));
  } else {
    percentText = "--";
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

// 兼容旧调用点（vasApplyProgress 是旧名字，重命名后留个 alias）
const vasApplyProgress = vasUpdateProgress;

async function vasFetchSnapshot() {
  try {
    const r = await fetch(API_PREFIX + "/video-analysis-search/analysis", { cache: "no-store" });
    if (!r.ok) return;
    vasApplySnapshot(await r.json());
  } catch (_) { /* ignore */ }
}

async function vasFetchEvents() {
  try {
    const r = await fetch(API_PREFIX + "/video-analysis-search/analysis/events", { cache: "no-store" });
    if (!r.ok) return;
    const body = await r.json();
    VAS.events = (body.events || []).slice().sort((a, b) => a.id - b.id);
    vasRenderEventList();
    vasSetSummary(body.summary?.summary || "");
    if (body.snapshot) vasApplySnapshot(body.snapshot);
  } catch (_) { /* ignore */ }
}

function connectVfaWs() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}${API_PREFIX}/video-analysis-search/analysis/ws`;
  const ws = new WebSocket(url);
  VAS.ws = ws;

  ws.onopen = () => {
    VAS.wsRetry = 0;
    vasShowError("");
  };
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }
    if (msg.type === "event" && msg.event) {
      vasAppendEvent(msg.event);
    } else if (msg.type === "summary") {
      vasSetSummary(msg.summary || "");
    } else if (msg.type === "judge") {
      // 判据专用推送（decode 侧 0.5s 节流）。state 要等 VLM 回来才推，
      // 解码却早就跑出去几百帧 —— 判据状态（曲线 / 角标）靠这一路才跟得上。
      window.FrameJudge?.update("vas", msg.judge);
    } else if (msg.type === "state" || msg.type === "hello") {
      vasApplySnapshot(msg);
      // run 进入终态（done/stopped/error）→ 后端已落盘，刷新历史
      if (msg.state && ["done", "stopped", "error"].includes(msg.state)) {
        vasFetchHistory();
      }
    } else if (msg.type === "error") {
      vasShowError(msg.detail || "WS 错误");
    }
  };
  ws.onclose = () => {
    VAS.ws = null;
    const wait = Math.min(5000, 500 * Math.pow(2, VAS.wsRetry++));
    VAS.wsTimer = setTimeout(connectVfaWs, wait);
  };
  ws.onerror = () => {
    try { ws.close(); } catch (_) {}
  };
}

async function vasStartAnalyze() {
  // 合并页共用视频源输入框（#vcs-source）。
  const sourceEl = document.getElementById("vcs-source");
  const source = (sourceEl?.value || "").trim();
  // 分析提问与判据参数（算法 / 阈值 / 帧间隔）均已固化为服务端常量，
  // 请求体只带视频源。
  if (!source)  { vasShowError("请先选择视频源"); return; }
  const body = { source };
  vasShowError("");
  VAS.events = [];
  vasRenderEventList();
  vasSetSummary("");
  try {
    const r = await fetch(API_PREFIX + "/video-analysis-search/analysis/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const detail = await r.text().catch(() => "");
      vasShowError("启动失败: HTTP " + r.status + " " + detail);
      return;
    }
    vasApplySnapshot(await r.json());
  } catch (e) {
    vasShowError("启动失败: " + e.message);
  }
}

async function vasStopAnalyze() {
  try {
    const r = await fetch(API_PREFIX + "/video-analysis-search/analysis/stop", { method: "POST" });
    if (r.ok) vasApplySnapshot(await r.json());
  } catch (e) {
    vasShowError("停止失败: " + e.message);
  }
}

async function vasClear() {
  try {
    await fetch(API_PREFIX + "/video-analysis-search/analysis/events/clear", { method: "POST" });
    // 只清"当前视频"的反查目标（不动 VAS_SEARCH.selRunId —— 若用户选中了历史
    // 记录，反查仍指向那条记录，方案 B）。记录被清空的 run：状态推送
    // 每 2s 会把 VAS.snap 重新填回同一 run，用 _clearedRun 排除它。
    VAS._clearedRun = (VAS.snap && VAS.snap.run_id) || "";
    VAS.snap = null;
    VAS.events = [];
    vasSetSummary("");
    vasRenderEventList();
    vasResetProgress();
    vasUpdateButtons();
  } catch (_) { /* ignore */ }
}

// 把分析进度条复位到 idle（清空"分析结果"时调用）。置 forceIdle 标志，
// 让后续状态推送不再把进度条刷回 done/100%，直到下次点「开始」。
function vasResetProgress() {
  VAS._progressForceIdle = true;
  VAS._progressSig = "";
  const root    = document.getElementById("vas-progress");
  const phaseEl = document.getElementById("vas-progress-phase");
  const metaEl  = document.getElementById("vas-progress-meta");
  const trackEl = document.getElementById("vas-progress-track");
  const fillEl  = document.getElementById("vas-progress-analyze");
  const nowEl   = document.getElementById("vas-progress-count-now");
  if (root) root.dataset.state = "idle";
  if (phaseEl) phaseEl.textContent = "分析: 未启动";
  if (metaEl) metaEl.textContent = "--";
  if (fillEl) {
    fillEl.style.width = "0%";
    fillEl.classList.remove("indeterminate");
  }
  if (nowEl) nowEl.textContent = "0";
  if (trackEl) trackEl.style.setProperty("--p", "0%");
}

// ---- 历史 run 列表 ----
function vasFmtTs(ts) {
  if (!ts) return "--";
  try {
    const d = new Date(ts * 1000);
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  } catch (_) { return "--"; }
}

// 历史列表已合并到统一渲染（vcsCacheRender 渲染两类行）；此入口保留
// 做兼容转发，供 vasLoadRun / vasDeleteRun 等操作后刷新统一列表。
async function vasFetchHistory() {
  return vcsCacheRefresh();
}

async function vasLoadRun(runId) {
  try {
    const r = await fetch(
      API_PREFIX + "/video-analysis-search/analysis/runs/" + encodeURIComponent(runId),
      { cache: "no-store" }
    );
    if (!r.ok) {
      vasShowError("加载失败: HTTP " + r.status);
      return;
    }
    const body = await r.json();
    VAS._clearedRun = "";   // 主动加载的历史记录 → 允许对其反查
    VAS.events = (body.events || []).slice().sort((a, b) => a.id - b.id);
    vasRenderEventList();
    vasSetSummary(body.summary?.summary || "");
    vasApplySnapshot(body.snapshot || {});
    vasShowError("");
  } catch (e) {
    vasShowError("加载失败: " + e.message);
  }
}

async function vasDeleteRun(runId) {
  if (!confirm("确定删除这条历史分析记录吗？")) return;
  try {
    const r = await fetch(
      API_PREFIX + "/video-analysis-search/analysis/runs/" + encodeURIComponent(runId),
      { method: "DELETE" }
    );
    if (!r.ok) {
      vasShowError("删除失败: HTTP " + r.status);
      return;
    }
    // 删的是当前选中的分析条目 → 清空选中 + 底部按钮置灰
    if (VAS_SEARCH.selRunId === runId) {
      VAS_SEARCH.selRunId = "";
      VAS_SEARCH.selRun = null;
      vasUpdateRunActions();
    }
    // 统一历史列表（按 md5 聚合）也要刷新 —— 否则删掉的分析子行仍显示
    vcsCacheRefresh().catch(() => {});
    await vasFetchHistory();
  } catch (e) {
    vasShowError("删除失败: " + e.message);
  }
}

// ---- 反查 modal ----
const VASQuery = {
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
    el.classList.remove("vas-query-input-invalid");
    el.removeAttribute("title");
    return true;
  }
  // 格式必须严格 HH:MM:SS
  if (_TIME_HHMMSS_RE.test(raw)) {
    el.classList.remove("vas-query-input-invalid");
    el.removeAttribute("title");
    return true;
  }
  el.classList.add("vas-query-input-invalid");
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

function vasQueryOpen(runId, sourceLabel, totalFrames, fps) {
  VASQuery.runId = runId;
  // 存 fps / totalFrames 到 VASQuery，让 vasQueryBuildQuery 转换时间 → frame
  // 时能拿到（不需要每次开 modal 时重新传参）。
  VASQuery.fps = fps;
  VASQuery.totalFrames = totalFrames;
  VASQuery.selected = { actors: new Set(), objects: new Set(), actions: new Set() };
  document.getElementById("vas-query-source").textContent = sourceLabel || runId;
  document.getElementById("vas-query").classList.remove("hidden");
  // "时间范围" 那行加个 hint：总时长。让用户知道能筛的范围上限，
  // 避免填"00:30:00"但视频只有"00:01:13"这种"搜不到也不说为啥"的尴尬。
  // totalFrames / fps 来自 list_runs（落盘 JSON + 探测自愈）。
  const hintEl = document.getElementById("vas-query-range-hint");
  if (hintEl) {
    if (totalFrames > 0 && fps > 0) {
      const totalSec = totalFrames / fps;
      // placeholder 用 HH:MM:SS（严格格式，validation 见 on-blur handler）。
      // 用 _formatTimeHHMMSS 不用 _formatVideoTimestamp 是因为这里不需要
      // 毫秒（input 不支持小数点 + 用户感受不到 ms 精度），更简洁。
      const tsHHMMSS = _formatTimeHHMMSS(totalSec);
      hintEl.textContent = `（总时长 ${tsHHMMSS}）`;
      const minEl = document.getElementById("vas-query-time-min");
      const maxEl = document.getElementById("vas-query-time-max");
      if (minEl) minEl.placeholder = `起始 (00:00:00)`;
      if (maxEl) maxEl.placeholder = `结束 (${tsHHMMSS})`;
    } else {
      hintEl.textContent = "（总时长未知）";
      const minEl = document.getElementById("vas-query-time-min");
      const maxEl = document.getElementById("vas-query-time-max");
      if (minEl) minEl.placeholder = "起始 (00:00:00)";
      if (maxEl) maxEl.placeholder = "结束 (HH:MM:SS)";
    }
  }
  // 清空旧筛选状态
  document.getElementById("vas-query-scene").innerHTML = '<option value="">不限</option>';
  document.getElementById("vas-query-actors").innerHTML = '<span class="vas-query-empty">（加载中…）</span>';
  document.getElementById("vas-query-objects").innerHTML = '<span class="vas-query-empty">（加载中…）</span>';
  document.getElementById("vas-query-actions").innerHTML = '<span class="vas-query-empty">（加载中…）</span>';
  document.getElementById("vas-query-time-min").value = "";
  document.getElementById("vas-query-time-max").value = "";
  document.getElementById("vas-query-exact").value = "";
  document.getElementById("vas-query-q").value = "";
  document.getElementById("vas-query-list").innerHTML = "";
  document.getElementById("vas-query-summary").textContent = "加载候选中…";
  vasQueryLoadFacets();
}

function vasQueryClose() {
  document.getElementById("vas-query").classList.add("hidden");
  VASQuery.runId = null;
}

// --------------------------------------------------------------------- //
// 原始帧预览弹窗：点击反查结果触发，从源视频取出对应帧显示。
// 关键路径：fetch frame JPEG → 用 blob URL 渲染到 <img>，避免 base64 膨胀。
// 取消：点关闭 / 点遮罩 / Esc 都行。
// --------------------------------------------------------------------- //
let _vasFrameBlobUrl = null;
let _vasFrameAbort = null;

function vasFrameModalClose() {
  const modal = document.getElementById("vas-frame-modal");
  if (modal) modal.classList.add("hidden");
  // 释放上一张 blob URL
  if (_vasFrameBlobUrl) {
    URL.revokeObjectURL(_vasFrameBlobUrl);
    _vasFrameBlobUrl = null;
  }
  if (_vasFrameAbort) {
    try { _vasFrameAbort.abort(); } catch (_) {}
    _vasFrameAbort = null;
  }
}

function _setFrameModalLoading(text) {
  const loading = document.getElementById("vas-frame-modal-loading");
  const img = document.getElementById("vas-frame-modal-img");
  const err = document.getElementById("vas-frame-modal-error");
  if (loading) {
    loading.classList.remove("hidden");
    loading.textContent = text || "从原始视频取帧中…";
  }
  if (img) img.classList.add("hidden");
  if (err) err.classList.add("hidden");
}

function _setFrameModalError(text) {
  const loading = document.getElementById("vas-frame-modal-loading");
  const img = document.getElementById("vas-frame-modal-img");
  const err = document.getElementById("vas-frame-modal-error");
  if (loading) loading.classList.add("hidden");
  if (img) img.classList.add("hidden");
  if (err) {
    err.classList.remove("hidden");
    err.textContent = text || "取帧失败";
  }
}

function _setFrameModalImage(blobUrl) {
  const loading = document.getElementById("vas-frame-modal-loading");
  const img = document.getElementById("vas-frame-modal-img");
  const err = document.getElementById("vas-frame-modal-error");
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
  const runId = VASQuery.runId;
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
  const modal = document.getElementById("vas-frame-modal");
  if (!modal) return;
  modal.classList.remove("hidden");
  document.getElementById("vas-frame-modal-meta").textContent =
    "帧 " + framePos;
  document.getElementById("vas-frame-modal-frame-pos").textContent =
    String(framePos);
  // 该帧时间戳：fps 还没拿到，先清掉（成功回调里会重填）
  const tsEl0 = document.getElementById("vas-frame-modal-timestamp");
  if (tsEl0) tsEl0.textContent = "--";
  document.getElementById("vas-frame-modal-source").textContent =
    "(取帧中…)";
  document.getElementById("vas-frame-modal-text").textContent = text;
  document.getElementById("vas-frame-modal-structured").textContent =
    _formatStructured(structured);
  _setFrameModalLoading("从原始视频取帧中…（按 every_n 抽样，frame_pos 可能较深）");

  // 释放旧 blob URL
  if (_vasFrameBlobUrl) {
    URL.revokeObjectURL(_vasFrameBlobUrl);
    _vasFrameBlobUrl = null;
  }
  if (_vasFrameAbort) {
    try { _vasFrameAbort.abort(); } catch (_) {}
  }
  _vasFrameAbort = new AbortController();

  try {
    const url = API_PREFIX + "/video-analysis-search/analysis/runs/" +
      encodeURIComponent(runId) + "/frame/" + framePos;
    const r = await fetch(url, {
      cache: "no-store",
      signal: _vasFrameAbort.signal,
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
    _vasFrameBlobUrl = URL.createObjectURL(blob);
    _setFrameModalImage(_vasFrameBlobUrl);
    // 用 X-Frame-Pos header 显示服务端确认的 frame_pos（防 stale）
    const serverPos = r.headers.get("X-Frame-Pos");
    if (serverPos) {
      document.getElementById("vas-frame-modal-frame-pos").textContent =
        serverPos;
    }
    // 该帧时间戳：frame_pos / fps = 视频内的绝对秒。老 run 可能没存 fps
    // （X-Fps header 是 "0"），那种情况保持 "--"。fps 是 float 字符串，
    // 必须 parseFloat，不能 Number()（后者会容忍垃圾字符串）。
    const fpsStr = r.headers.get("X-Fps") || "0";
    const fps = parseFloat(fpsStr);
    const posForTs = serverPos ? parseInt(serverPos, 10) : framePos;
    const tsEl = document.getElementById("vas-frame-modal-timestamp");
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
      const sourceEl = document.getElementById("vas-frame-modal-source");
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

async function vasQueryLoadFacets() {
  const runId = VASQuery.runId;
  if (!runId) return;
  try {
    const r = await fetch(
      API_PREFIX + "/video-analysis-search/analysis/runs/" + encodeURIComponent(runId) + "/facets",
      { cache: "no-store" }
    );
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();
    VASQuery.facets = {
      scenes:  data.scenes  || [],
      actors:  data.actors  || [],
      objects: data.objects || [],
      actions: data.actions || [],
    };
    renderQueryFacets();
    document.getElementById("vas-query-summary").textContent =
      `共 ${VASQuery.facets.scenes.length} 场景 / ${VASQuery.facets.actors.length} 人物 / ${VASQuery.facets.objects.length} 物体 / ${VASQuery.facets.actions.length} 动作。点击「反查」开始筛选。`;
  } catch (e) {
    document.getElementById("vas-query-summary").textContent =
      "加载候选失败：" + e.message;
  }
}

function renderQueryFacets() {
  // scene dropdown
  const sceneSel = document.getElementById("vas-query-scene");
  sceneSel.innerHTML = '<option value="">不限</option>';
  VASQuery.facets.scenes.forEach((s) => {
    const opt = document.createElement("option");
    opt.value = s;
    opt.textContent = s;
    sceneSel.appendChild(opt);
  });
  // chips
  renderChips("vas-query-actors", VASQuery.facets.actors, "actors");
  renderChips("vas-query-objects", VASQuery.facets.objects, "objects");
  renderChips("vas-query-actions", VASQuery.facets.actions, "actions");
}

function renderChips(elId, items, kind) {
  const host = document.getElementById(elId);
  host.innerHTML = "";
  if (!items.length) {
    const empty = document.createElement("span");
    empty.className = "vas-query-empty";
    empty.textContent = "（无）";
    host.appendChild(empty);
    return;
  }
  items.forEach((s) => {
    const chip = document.createElement("span");
    chip.className = "vas-query-chip";
    chip.dataset.kind = kind;
    chip.dataset.value = s;
    chip.textContent = s;
    chip.title = "点击切换：选中=AND 过滤";
    chip.addEventListener("click", () => {
      const set = VASQuery.selected[kind];
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

function vasQueryBuildQuery() {
  const params = new URLSearchParams();
  const scene = document.getElementById("vas-query-scene").value;
  if (scene) params.set("scene", scene);
  // 多值参数 → 多个同名 query（server 端 OR 匹配）
  VASQuery.selected.actors.forEach((a) => params.append("actor", a));
  VASQuery.selected.objects.forEach((o) => params.append("object", o));
  VASQuery.selected.actions.forEach((a) => params.append("action", a));
  // 精准匹配搜索（完整字符串 substring）—— "出租车" 必须完整命中
  const exact = document.getElementById("vas-query-exact").value.trim();
  if (exact) params.set("exact", exact);
  // 模糊搜索（n-gram 跨字段匹配）—— "雨天穿红衣" 拆 2-gram 命中
  const q = document.getElementById("vas-query-q").value.trim();
  if (q) params.set("q", q);
  // 时间范围：用户按 HH:MM:SS 输入（严格格式），前端解析成秒数再 × fps 转
  // 成 frame 传给后端。后端参数名仍叫 frame_min/max（不要后端改）。
  // fps=0 时跳过（无法转换）。
  const tmin = document.getElementById("vas-query-time-min").value;
  const tmax = document.getElementById("vas-query-time-max").value;
  const fps = VASQuery.fps || 0;
  if (tmin || tmax) {
    if (fps <= 0) {
      console.warn("vas: 视频 fps 未知，时间范围 filter 被跳过");
    } else {
      const totalSec = (VASQuery.totalFrames || 0) / fps;
      // 截断 end 到视频总长度（用户友好：输超出不会报错也不会返回 0 条）。
      // 把"已截断"的提示写到 #vas-query-clamp-notice，下次开 modal 自动隐藏。
      const showClamp = (msg) => {
        const notice = document.getElementById("vas-query-clamp-notice");
        const text  = document.getElementById("vas-query-clamp-text");
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
          console.warn(`vas: 起始时间 "${tmin}" 格式错误（需要 HH:MM:SS），filter 跳过`);
        } else {
          const f = _secondsToFrame(sec, fps);
          if (f !== null) params.set("frame_min", String(f));
        }
      }
      if (tmax.trim()) {
        const sec = _parseTimeToSeconds(tmax);
        if (sec === null) {
          console.warn(`vas: 结束时间 "${tmax}" 格式错误（需要 HH:MM:SS），filter 跳过`);
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
        // vasQueryOpen 会清空输入 + 清 clamp notice。
      }
    }
  } else {
    // 用户没填时间范围：清掉 clamp notice（modal 关闭再重开时残留的）。
    const notice = document.getElementById("vas-query-clamp-notice");
    if (notice) notice.classList.add("hidden");
  }
  return params.toString();
}

function vasQueryClear() {
  document.getElementById("vas-query-scene").value = "";
document.getElementById("vas-query-exact").value = "";
  document.getElementById("vas-query-q").value = "";
  document.getElementById("vas-query-time-min").value = "";
  document.getElementById("vas-query-time-max").value = "";
  // 清空时也清掉红框 + 截断提示
  ["vas-query-time-min", "vas-query-time-max"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) {
      el.classList.remove("vas-query-input-invalid");
      el.removeAttribute("title");
    }
  });
  const notice = document.getElementById("vas-query-clamp-notice");
  if (notice) notice.classList.add("hidden");
  VASQuery.selected.actors.clear();
  VASQuery.selected.objects.clear();
  VASQuery.selected.actions.clear();
  document.querySelectorAll(".vas-query-chip.is-on").forEach((c) => c.classList.remove("is-on"));
}

// 精准匹配 / 模糊搜索 + 字段过滤都是纯 Python，毫秒级返回。
async function vasQueryRun() {
  const runId = VASQuery.runId;
  if (!runId) return;
  const qs = vasQueryBuildQuery();
  const summaryEl = document.getElementById("vas-query-summary");
  const listEl = document.getElementById("vas-query-list");
  // 收集当前所有筛选条件作为高亮 terms
  VASQuery._terms = collectQueryTerms();

  summaryEl.textContent = "查询中…";
  listEl.innerHTML = "";

  try {
    const r = await fetch(
      API_PREFIX + "/video-analysis-search/analysis/runs/" + encodeURIComponent(runId) +
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
  const sceneEl = document.getElementById("vas-query-scene");
  if (sceneEl && sceneEl.value) terms.add(sceneEl.value);
  VASQuery.selected.actors.forEach((a) => terms.add(a));
  VASQuery.selected.objects.forEach((o) => terms.add(o));
  VASQuery.selected.actions.forEach((a) => terms.add(a));
  // 精准匹配搜索（完整字符串）
  const exactEl = document.getElementById("vas-query-exact");
  if (exactEl && exactEl.value.trim()) terms.add(exactEl.value.trim());
  // 模糊搜索（n-gram 切分）
  const qEl = document.getElementById("vas-query-q");
  if (qEl && qEl.value.trim()) {
    const q = qEl.value.trim();
    for (let i = 0; i < q.length - 1; i++) {
      terms.add(q.slice(i, i + 2));
    }
  }
  return [...terms].filter((t) => t.length >= 1);
}

function renderQueryResults(data) {
  const summaryEl = document.getElementById("vas-query-summary");
  const listEl = document.getElementById("vas-query-list");
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
    li.className = "vas-query-result";
    li.title = "点击查看原始帧";
    // 把 event 引用挂到 DOM 上，click handler 直接拿来取数据
    li.dataset.framePos = String(ev.frame_pos || 0);
    li.dataset.text = ev.text || "";
    li.dataset.structured = JSON.stringify(s || {});
    const head = document.createElement("div");
    head.className = "vas-query-result-head";
    const frame = document.createElement("span");
    frame.className = "vas-query-result-frame";
    frame.textContent = "帧 " + ev.frame_pos;
    const ts = document.createElement("span");
    ts.textContent = new Date((ev.ts || 0) * 1000).toLocaleTimeString();
    head.appendChild(frame);
    head.appendChild(ts);
    li.appendChild(head);
    const tags = document.createElement("div");
    tags.className = "vas-query-result-tags";
    if (s.scene)   tags.appendChild(makeTag(s.scene, "scene"));
    if (s.action)  tags.appendChild(makeTag(s.action, "action"));
    (s.actors  || []).forEach((a) => tags.appendChild(makeTag("人:" + a, "actor")));
    (s.objects || []).forEach((o) => tags.appendChild(makeTag("物:" + o, "object")));
    li.appendChild(tags);
    if (s.note) {
      const note = document.createElement("div");
      note.className = "vas-query-result-note";
      note.textContent = "注：" + s.note;
      li.appendChild(note);
    }
     const text = document.createElement("div");
     text.className = "vas-query-result-text";
     text.innerHTML = highlightTerms(ev.text || "(空)", VASQuery._terms || []);
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
  t.className = "vas-query-tag vas-query-tag-" + kind;
  t.textContent = text;
  return t;
}

function initVideoFileAnalysis() {
  if (window.__vasInit) return;
  window.__vasInit = true;

  // 视频源 / 开始 / 停止 / 刷新 / 错误关闭 已由 initVideoSearch
  // 统一绑定（合并页共用控件）；这里只保留 VAS 专有部分。
  const clearBtn   = document.getElementById("vas-clear");
  clearBtn?.addEventListener("click", vasClear);

  // 反查 modal
  document.getElementById("vas-query-close")?.addEventListener("click", vasQueryClose);
  const runBtn = document.getElementById("vas-query-run");
  runBtn?.addEventListener("click", vasQueryRun);
  document.getElementById("vas-query-clear")?.addEventListener("click", vasQueryClear);
  // "时间范围"输入 on-blur 触发格式校验（非 on-input —— 用户输入到一半
  // 如 "0" 是合法中间态，on-input 会闪红框，太烦）。
  document.getElementById("vas-query-time-min")
    ?.addEventListener("blur", (e) => _validateTimeInput(e.target));
  document.getElementById("vas-query-time-max")
    ?.addEventListener("blur", (e) => _validateTimeInput(e.target));
  document.getElementById("vas-query")?.addEventListener("click", (e) => {
    // 点遮罩关闭
    if (e.target.id === "vas-query") vasQueryClose();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !document.getElementById("vas-query").classList.contains("hidden")) {
      vasQueryClose();
    }
  });

  // 帧预览弹窗：点关闭 / 点遮罩 / Esc 都关闭
  document.getElementById("vas-frame-modal-close")?.addEventListener("click", vasFrameModalClose);
  document.getElementById("vas-frame-modal")?.addEventListener("click", (e) => {
    if (e.target.id === "vas-frame-modal") vasFrameModalClose();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !document.getElementById("vas-frame-modal").classList.contains("hidden")) {
      vasFrameModalClose();
    }
  });

  // 初次进入：拉一次分析快照 + 事件（历史列表由统一历史刷新负责）
  vasFetchSnapshot();
  vasFetchEvents();
}


// 卡片入口：framework 懒加载本 JS 后立即执行。
let _modeAvailBound = false;
let _modeAvailTicker = false;

function initVideoAnalysisSearch() {
  VAS_SEARCH.init();
  VAS.init();
  if (!VAS_SEARCH.ws && !VAS_SEARCH.wsTimer) connectVcsWs();
  if (!VAS.ws && !VAS.wsTimer) connectVfaWs();

  // 算力能力可用性 —— 决定「分析」/「检索」勾选框是否置灰。卡片 JS
  // 只在首次进入时加载一次（framework 的 _loadedApps 记忆），所以除了
  // 首次拉取，还需在切回本页时重拉（用户可能刚在「算力配置」启用模型）。
  vasRefreshModeAvail().catch(() => {});
  if (!_modeAvailBound) {
    _modeAvailBound = true;
    window.addEventListener("hashchange", () => {
      const h = (location.hash || "").replace(/^#\/?/, "") || "apps";
      if (h === "video-analysis-search") {
        vasRefreshModeAvail().catch(() => {});
      }
    });
  }
  // 周期兜底：算力可能在另一个标签页/页面被启用或卸载，仅靠首次进入 +
  // hashchange 会漏掉（页面一直开着不重新导航时状态不刷新）。5s 轮询
  // 让「分析/检索」勾选框与状态小字自愈；页面不可见时跳过不发请求。
  if (!_modeAvailTicker) {
    _modeAvailTicker = true;
    setInterval(() => {
      if (document.visibilityState !== "visible") return;
      vasRefreshModeAvail().catch(() => {});
    }, 5000);
  }
}

window.VAS = { init: initVideoAnalysisSearch, refresh: vasRefreshAll };

initVideoAnalysisSearch();

// ------------------------------------------------------------------ *
//  应用向导模式 API — wizard 框架 finish 时调用
// 契约:opts 由 wizard.json 的 steps[].fields[].ui_key 决定:
//   vcs-source → opts.source   vcs-text → opts.searchText
// source 齐全才点 #vas-start（统一入口按功能勾选并行发起分析/检索）；
// searchText 非空时额外勾选「检索」功能并写入问题框。分析提问与判据参数
// （算法 / 阈值 / 帧间隔）均已固化为服务端常量，不再由向导收集。
// 写值后统一补发 input/change —— 卡片内源框 / 勾选框的监听依赖它们。
// 全局名约定 ImApp_<app.id 去掉 hyphen>。
// ------------------------------------------------------------------ *
window.ImApp_video_analysis_search = window.ImApp_video_analysis_search || {};
window.ImApp_video_analysis_search.applyWizardSnapshot = async function (opts) {
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

  if (opts.source) setVal("vcs-source", opts.source);
  if (opts.loop != null) setChecked("vcs-loop", !!opts.loop);
  // 分析提问与判据参数（算法 / 阈值 / 帧间隔）均已固化为服务端常量，
  // 向导不再收集（老 wizard.json 的 judgeMode 等字段即使存在也会被忽略）。
  // 检索问题可选:填了才启用「检索」功能并写入问题框(勾选动作统一放到
  // 算力刷新之后,见下)
  const hasSearch = !!(opts.searchText && String(opts.searchText).trim());
  if (hasSearch) setVal("vcs-text", String(opts.searchText));
  // 切片参数固化为服务端常量（4s / 重叠 1s），向导不再收集。
  const baseReady = !!opts.source;
  if (!baseReady) {
    return { source_set: !!opts.source,
             search_set: hasSearch, started: false };
  }
  // 分析是向导主路径 —— 勾选前先主动刷新一次算力可用性。向导在
  // _waitForModelsRunning 之后立即回调,而前端 modeAvail 走 5s 周期,此刻
  // 可能仍是"未启用"旧值 → setChecked 会被 vasUpdateButtons 当作 disabled
  // 清掉(典型现象:向导完成只跑了检索、分析没开始)。
  await vasRefreshModeAvail().catch(() => {});
  setChecked("vas-mode-analyze", true);
  if (hasSearch) setChecked("vas-mode-search", true);
  const btn = document.getElementById("vas-start");
  if (btn) {
    try { btn.click(); }
    catch (e) { throw new Error("WizardSnapshotError:vas_start_failed:" + (e?.message || e)); }
    return { source_set: true,
             search_set: hasSearch, started: true };
  }
  return { source_set: true,
           search_set: hasSearch, started: false };
};

})();
