(function () {
  "use strict";
  const API_PREFIX = "/api";
  const STATIC_PREFIX = "/static";

  /* ------------------------------------------------------------------ *
   * 实时识别（麦克风）的常量
   *
   * CHUNK_MS —— 采集端攒块粒度。500 ms 兼顾「首字延迟」与「上行开销」：
   * 攒得越大首字越晚，越小上行越频繁。
   *
   * MAX_PENDING —— L1 采集层背压：发送队列上限（块）。满了丢**最旧**的
   * 块。理由：实时场景下「晚到的旧音频」价值低于「最新的音频」——
   * 宁可丢字，不可延迟膨胀。
   * ------------------------------------------------------------------ */
  const LIVE_CHUNK_MS = 500;
  const LIVE_CHUNK_SAMPLES = (16000 * LIVE_CHUNK_MS) / 1000;   // 8000
  const LIVE_MAX_PENDING = 2;

  /* ------------------------------------------------------------------ *
   * 节奏字幕（YouTube 式实时字幕）的常量
   *
   * 字幕不是「推」出来的，是**按播放头的位置渲染**出来的 —— 服务端是
   * 整文件快转，识别结果在播放开始前就全到手了。所以这些常量描述的
   * 是「怎么把一段文本摆到画面上」，不是「什么时候推」。
   * ------------------------------------------------------------------ */
  const SUB_MAX_VISIBLE_LINES = 3;    // 画面上最多同时显示几行
  const SUB_PREV_HOLD_SEC = 3.2;      // 上一行说完后还留多久（然后淡出）
  const SUB_MAX_LINE_COLS = 42;       // 一行最多多少「列」（CJK 占两列）
  const SUB_MIN_SOFT_CHARS = 10;      // 逗号断行至少要攒这么多字
  const SUB_MAX_LINE_SEC = 7.0;       // 一行最多跨多少秒
  const SUB_MIN_LINE_SEC = 0.5;       // 一行至少占多久（防「闪过去」）

  /* ------------------------------------------------------------------ *
   * 「拖动进度条 → 识别跟过去」的三个参数
   * ------------------------------------------------------------------ */

  //: 往回找多少秒作为**引导**（lead-in）。
  //:
  //: 新会话的头 4 轮**不允许提交**（SDK ``asr.py:1067`` 的
  //: ``chunk_idx < rollback_rounds``）→ 从拖动点**正好**开始识别的话，
  //: 前 4 秒是模型的纯猜测，committed 恒为空 → 字幕在那里是空的。
  //: 往前多带 8 秒，模型就有上下文了（不会「零上下文 + 半个句子」→
  //: 语法不通的碎片），而且这 8 秒正好把热身期吃掉 → 拖动点本身就有字幕。
  const SUB_SEEK_LEAD_IN_SEC = 8.0;

  //: 判定「这个位置识别过了」的**余量**（秒）。
  //:
  //: 已确认文本比音频**滞后** 4~5 轮（回退策略）→ 识别区间虽然到了
  //: 某个时间，字幕（committed 派生的）其实还差几秒。留 2 秒余量，
  //: 免得在**边界**上反复重开识别。
  const SUB_SEEK_COVER_MARGIN_SEC = 4.0;

  //: 两次「为拖动重开识别」之间的**静默期**（毫秒）—— 防抖。
  const SUB_SEEK_DEBOUNCE_MS = 1500;

  /** CJK（含日文假名 / 韩文）—— 逐字点亮，不按空格切。 */
  const CJK_RE = /[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]/;

  /** 句末标点：断句。 */
  const SUB_HARD_END = /[.!?。！？…]["'”’)\]】」』]*$/;
  /** 句中标点：够长就断句。 */
  const SUB_SOFT_END = /[,;:，；、、]$/;

  /* ------------------------------------------------------------------ *
   * 节奏字幕的两个小工具
   * ------------------------------------------------------------------ */

  /**
   * 最长公共前缀长度（按 UTF-16 码元比）。
   *
   * 用来回答：「这一轮模型**猜**的文本，和最终确认下来的文本，前缀
   * 能对上多少字？」—— 也就是「到此刻为止，最终字幕里已经被说出来
   * 多少字」。见 ``_epochTable``。
   */
  function _lcp(a, b) {
    const n = Math.min(a.length, b.length);
    let i = 0;
    while (i < n && a[i] === b[i]) i += 1;
    // 别把一个代理对劈成两半（emoji / 少数字的扩展汉字）。
    if (i > 0 && i < n) {
      const c = a.charCodeAt(i - 1);
      if (c >= 0xd800 && c <= 0xdbff) i -= 1;
    }
    return i;
  }

  /**
   * 把文本切成「词」。
   *
   * 拉丁文按空格切；CJK 逐字切（中文没有空格，逐字点亮才对得上
   * 说话的节奏）。词里**带上后面的空白**，渲染时原样拼回去 —— 不用
   * 自己决定词间要不要补空格，中英混排也就不会串版。
   */
  function _splitWords(text) {
    const out = [];
    const n = text.length;
    let i = 0;
    while (i < n) {
      if (/\s/.test(text[i])) { i += 1; continue; }
      const start = i;
      if (CJK_RE.test(text[i])) {
        i += 1;
      } else {
        while (i < n && !/\s/.test(text[i]) && !CJK_RE.test(text[i])) i += 1;
      }
      // 带上尾随空白（渲染时原样输出）
      while (i < n && /\s/.test(text[i])) i += 1;
      out.push({ text: text.slice(start, i), trim: text.slice(start, i).trim(), i0: start, i1: i });
    }
    return out;
  }

  /* ------------------------------------------------------------------ *
   * 与 Python 对齐的两个小函数
   *
   * numpy 那边用的是 ``round()``（**银行家舍入**，半数取偶）和
   * ``//``（向下取整除法）。JS 的 ``Math.round`` 是「半数向上」，
   * 直接用会在极少数采样上差 1 → 两端 PCM 不再逐字节相同。
   * ------------------------------------------------------------------ */

  /** 银行家舍入（Python ``round(float)`` 的语义）。 */
  function _pyRoundToEven(x) {
    const f = Math.floor(x);
    const diff = x - f;
    if (diff > 0.5) return f + 1;
    if (diff < 0.5) return f;
    return (f % 2 === 0) ? f : f + 1;
  }

  /** 向下取整除法（numpy 的 ``//``；这里只用于非负分子）。 */
  function _pyFloorDiv(a, b) {
    const mod = a % b;
    return Math.floor((a - mod) / b);
  }

  /** float32 数组 → s16le 字节（向零截断，与 numpy ``astype("<i2")`` 一致）。 */
  function _quantizeS16(values) {
    const out = new Uint8Array(values.length * 2);
    const view = new DataView(out.buffer);
    for (let i = 0; i < values.length; i++) {
      let v = values[i];
      if (v > 32767) v = 32767;
      else if (v < -32768) v = -32768;
      v = Math.trunc(v);
      view.setInt16(i * 2, v, true);
    }
    return out;
  }

/**
 * Qwen3-ASR 流式语音识别 —— 前端。
 *
 * 两种显示模式吃的是**同一份**数据（每轮的 committed / unfixed），
 * 跟 C demo 的两种输出方式一一对应：
 *
 *   UI「实时结果」  ←→  内部 single  ←→  rknn_qwen3_asr_demo_online ... -s
 *                 一行里：已确认文本（黑）+ 未确认尾巴（蓝），整行原地刷新
 *   UI「识别过程」  ←→  内部 rounds  ←→  rknn_qwen3_asr_demo_online ...
 *                 每轮一行：该轮确认的增量 + 该轮未确认的尾巴
 *
 * ⚠️ **UI 文案用大白话**（「实时结果」/「识别过程」），内部注释与
 * C demo 的说法（实时单行 / 逐轮明细）保持原样 —— 后者是机制术语，
 * 写在这里是为了对得上 C demo 的两种输出方式。用户看不懂「未确认尾巴」
 * 这种词，所以界面上不出现。
 *
 * 两种音频来源：
 *
 *   本机上传    → POST /transcribe 的 multipart `file`
 *   设备端路径  → POST /transcribe 的 form `path`（向导「浏览…」选出来的）
 *
 * 两种**识别模式**（互不干扰）：
 *
 *   文件 / 视频 → 一次性送整段，服务端按 1 秒一块增量解码
 *   实时麦克风  → 浏览器采集 → 逐块上行 → 服务端增量解码
 *
 * WS 帧：
 *   snapshot / state / round / done / error
 *   round 帧的 committed 是**累计**的已确认文本，unfixed 是本轮未确认的尾巴。
 *   实时模式另有 gap（音频丢失）/ endpoint（一句话结束）/ live_done。
 */
const Qwen3Asr = {
  ws: null,
  wsRetry: 0,
  wsTimer: null,
  snapshot: null,
  rounds: [],
  // 当前 rounds 是**哪一次识别**产生的（file / video / board / browser）。
  //
  // 为什么必须记：视频字幕是按播放头去 rounds 里取轮的，而 rounds 在
  // 「切换来源」时**不会**自动清 —— 于是切到「含音轨的视频」后，字幕会
  // 拿**上一来源**（比如「音频文件」）的轮次来渲染。实测：音频文件识别
  // 完再切到视频，画面下方凭空多出一句「嗯哼。」—— 而那是音频文件
  // 第 1 轮那个**会被回滚**的瞬时猜测，视频当时根本还没开始识别。
  //
  // 有了这个标记，字幕只在「轮次确实来自视频这次识别」时才渲染。
  _roundsSource: "",
  // 轮次「代」计数器 —— 每次发起新识别 / 清空就 +1。
  // 用途：刷新恢复（``_restoreFromServer``）拉轮次的过程中，用户要是
  // 又点了「开始识别」→ 代数变了 → **丢弃这次恢复**（旧轮次不能盖掉
  // 新识别刚起的轮次 = 串台）。
  _roundGen: 0,
  // 「停止」是**本页面**发起的吗？—— 用来区分两种「stopped」帧：
  //   * 自己点的停止 → 画面在 click 的那一刻就冻住了（见 ``stop()``），
  //     1 秒后到达的 stopped 帧**不该**再冻一次（那时用户可能已经
  //     自己按了播放）；
  //   * 别的页面 / 别的标签页发的停止 → 本页面没冻过 → 要冻。
  _stopInitiated: false,
  // 「清空」之后忽略迟到的轮次帧（清空前广播出去的那几帧）。
  // 新一轮识别开始时归零。
  _ignoreRounds: false,
  _file: null,
  _path: "",
  _starting: false,
  _source: "file",        // file / video / board / browser
  _boardState: null,      // 板载采集进程状态（后端返回）

  /* ---------------- 视频播放（含音轨的视频来源） ---------------- */
  _fileBlobUrl: "",        // 本机上传的 blob URL（要 revoke）
  _videoSrc: "",          // 当前 <video> 的 src（避免重复 load）
  _audioOriginSec: 0,     // 音轨在容器里的起点（后端给）

  /* ---------------- 节奏字幕（YouTube 式） ---------------- */
  _subEpochCache: [],     // 每个 epoch 的行缓存（key = 该 epoch 的轮数）
  _subTl: null,          // 整条时间轴的缓存
  _subTlKey: -1,         // 时间轴缓存对应的轮次数
  _subSlots: [],          // 常驻的行槽（3 个），便于做淡出/淡入
  _subSig: "",            // 上次渲染的签名 —— 没变就不碰 DOM
  _subRaf: 0,             // 逐帧刷新字幕的 rAF 句柄
  // 已识别的**音频时间区间**（秒，音轨相对起点）。拖动进度条时靠它判断
  // 「这个位置识别过没有」—— 没识别过就要把识别**开过去**（见
  // ``_maybeFollowSeek``）。
  _subRanges: [],
  _seekBusy: false,       // 正在为拖动重开识别（别重复发起）
  _seekGuardUntil: 0,     // 重开后的静默期（毫秒时间戳）
  _seekTarget: -1,        // 最近一次请求识别的位置（去抖）
  _seekPending: null,     // 重开**过程中**用户又拖到的位置（排队跟上）

  /* ---------------- 「识别过程」面板（逐轮明细） ----------------
   * 面板是**追加**的：每来一轮就多一行。老实现每帧重建整张表 →
   * 几千轮的片子里每帧几十毫秒，把浏览器主线程占死（识别都被拖慢）。
   * 现在：面板隐藏时不重建；可见时只补新增的行。 */
  _roundsShown: 0,        // DOM 里已经画到第几轮
  _roundsPrev: "",        // 上一轮的有效 committed（算本轮增量用）
  _roundsDirty: false,    // DOM 跟 ``this.rounds`` 对不上了（要整表重建）

  /* ---------------- 字幕同款转写框（视频来源下的「文字显示框」） ----------------
   * 画面字幕只留当前那几行；文字框留**整条时间轴**，可上拉回看。
   * 两者共用同一份时间轴 + 同一个词状态算法（``_revealState``）——
   * 「显示效果一样」必须是同一份代码算出来的，否则两边迟早对不上。 */
  _trRows: [],            // 转写框的行元素（与时间轴同序）
  _trTl: null,            // 已同步的那份时间轴（对象同一性 → 该重排了）
  _trCur: -1,             // 当前行下标（按播放头）
  _trFollow: true,        // 是否跟随播放头滚动（用户上翻时暂停跟随）
  _trAutoAt: 0,           // 上次自动滚动的时间戳（用来分辨「谁滚的」）

  /* ---------------- 实时识别状态 ---------------- */
  _live: {
    active: false,
    starting: false,
    streamId: "",
    ctx: null,
    stream: null,
    node: null,
    source: null,
    seq: 0,
    queue: [],
    sending: false,
    dropped: 0,        // 客户端丢掉的块（L1）
    serverDropped: 0,  // 服务端丢掉的块（L3）
    buf: null,         // 攒块缓冲（Uint8Array）
    resampler: null,   // 整块线性插值器的状态（与板载同算法）
    segments: [],      // 已完成的句子（端点切分）
    committed: "",
    unfixed: "",
    language: "",
  },

  init() {
    if (this._inited) return;
    this._inited = true;
    const $ = (id) => document.getElementById(id);

    $("asr-file")?.addEventListener("change", (e) => {
      const f = e.target.files && e.target.files[0];
      this._file = f || null;
      if (f) this._path = "";          // 两者互斥：选了本机文件就不再用路径
      const nameEl = $("asr-file-name");
      // 没选文件时**留空**：浏览器自带的 file input 已经显示了
      // 「未选择任何文件」，再补一句「未选择文件」是同一句话说两遍。
      if (nameEl) nameEl.textContent = f ? f.name : "";
      this._setFileBlob(f);            // 视频来源要用 blob URL 播
      this._syncSourceUi();
      this._syncVideoPlayer();
    });

    $("asr-path")?.addEventListener("input", (e) => {
      this._path = (e.target.value || "").trim();
      if (this._path) this._file = null;   // 反向：填了路径就不再用本机文件
      this._setFileBlob(null);              // 改成设备端路径 → 释放 blob
      this._syncSourceUi();
      this._syncVideoPlayer();
    });

    // 设备端文件浏览（与 vtl / vd 共用 FSModal）
    $("asr-browse")?.addEventListener("click", () => {
      const target = $("asr-path");
      try {
        window.VdFsModal._sourceTarget = target;
        window.VdFsModal.open("");
      } catch (e) {
        console.warn("[qwen3-asr] browse failed:", e);
      }
    });

    $("asr-start-btn")?.addEventListener("click", () => this.start());
    $("asr-stop-btn")?.addEventListener("click", () => this.stop());
    $("asr-reset-btn")?.addEventListener("click", () => this.reset());
    $("asr-mode")?.addEventListener("change", () => this._applyMode());
    $("asr-mic-btn")?.addEventListener("click", () => this._liveToggle());
    $("asr-mic-stop-btn")?.addEventListener("click", () => this._liveAbort());
    $("asr-board-start")?.addEventListener("click", () => this._boardStart());
    $("asr-board-stop")?.addEventListener("click", () => this._boardStop());

    // 输入来源切换（音频文件 / 含音轨的视频 / 板载麦克风 / 浏览器麦克风）
    document.querySelectorAll("#asr-source-tabs .asr-source-tab")
      .forEach((btn) => {
        btn.addEventListener("click", () => {
          this._switchSource(btn.dataset.source || "file");
        });
      });

    // 视频播放头 → 字幕跟着画面走
    const video = $("asr-video");
    if (video) {
      video.addEventListener("timeupdate", () => this._renderVideoSubtitle());
      video.addEventListener("loadedmetadata", () => this._renderVideoSubtitle());
      // ⚠️ 拖动进度条 → 字幕要跟到**新位置**。而那个位置很可能**还没识别**
      // （服务端是从头顺序识别的）→ 见 ``_maybeFollowSeek``。
      video.addEventListener("seeked", () => {
        this._renderVideoSubtitle();
        this._maybeFollowSeek();
      });
      // ⚠️ ``timeupdate`` 只有 4Hz —— 撑不起「逐词点亮」，高亮会一跳一跳。
      // 播放期间挂一个 rAF 循环，每帧算一次；DOM 只在**签名变了**时才动。
      video.addEventListener("play", () => this._startSubLoop());
      video.addEventListener("pause", () => this._stopSubLoop());
      video.addEventListener("ended", () => this._stopSubLoop());
    }

    // 转写框的滚动：用户上翻回看时**暂停跟随**播放头，滚回来再恢复。
    const trBody = $("asr-transcript-body");
    if (trBody) {
      trBody.addEventListener("scroll", () => this._onTranscriptScroll());
    }

    this._bindLiveGuards();
    this._applyMode();
    this._switchSource(this._source || "file");
    this.ensureWs();
    // 刷新恢复：把服务端的轮次 + 归属拉回来（转写框 / 逐轮明细 /
    // 来源页签）。**异步** —— 不阻塞首屏，WS 照常先连上。
    this._restoreFromServer();
    this._probeCap().catch(() => {});
    if (!this._capTimer) {
      this._capTimer = setInterval(() => {
        if (document.visibilityState !== "visible") return;
        this._probeCap().catch(() => {});
      }, 5000);
    }
    this._installLiveHooks();
  },

  /* ------------------------------------------------------------------ *
   * 输入来源切换
   *
   * 四种来源：
   *   file    音频文件（本机上传 / 设备端路径）
   *   video   含音轨的视频（同一套控件，accept 换成视频）
   *   board   板载麦克风（设备端 ALSA，独立进程采集）
   *   browser 浏览器麦克风（getUserMedia）
   * ------------------------------------------------------------------ */
  _switchSource(kind) {
    const valid = ["file", "video", "board", "browser"];
    const prev = this._source;
    this._source = valid.includes(kind) ? kind : "file";
    const kind_ = this._source;
    const $ = (id) => document.getElementById(id);

    // ⚠️ **来源变了 → 上一来源的识别结果不能留在界面上**。
    //
    // 症状（用户实测）：「音频文件」识别结束后切到「含音轨的视频」，
    // 画面下方凭空多出一句「嗯哼。」—— 那是**音频文件**第 1 轮的
    // 瞬时猜测（committed='' / unfixed='嗯哼。'），第 2 轮就被回滚
    // 推翻了，最终文本里根本没有这句；而这个视频**还没开始识别**。
    //
    // 根因有两层：
    //   1. ``this.rounds`` 跨来源保留 → 视频字幕按播放头取轮时取到了
    //      上一来源的轮次；
    //   2. 字幕取的是那一轮的 ``unfixed`` —— 那是**会被回滚**的猜测。
    //
    // 修法：来源一变就清掉上一来源的结果（正在跑的那次不能丢，见下）。
    if (prev && prev !== kind_) {
      this._onSourceChanged();
    }

    document.querySelectorAll("#asr-source-tabs .asr-source-tab")
      .forEach((btn) => {
        const on = btn.dataset.source === kind_;
        btn.classList.toggle("is-active", on);
        btn.setAttribute("aria-selected", on ? "true" : "false");
      });

    const isFile = kind_ === "file" || kind_ === "video";
    const filePanel = $("asr-panel-file");
    if (filePanel) filePanel.classList.toggle("hidden", !isFile);

    const boardPanel = $("asr-panel-board");
    if (boardPanel) {
      boardPanel.classList.toggle("hidden", kind_ !== "board");
    }
    const browserPanel = $("asr-panel-browser");
    if (browserPanel) {
      browserPanel.classList.toggle("hidden", kind_ !== "browser");
    }

    // 文件与视频：只差「接受什么类型」和一句说明
    const fileInput = $("asr-file");
    if (fileInput) {
      fileInput.accept = kind_ === "video"
        ? "video/*,.mp4,.mov,.mkv,.avi,.flv,.webm,.ts,.m4v,.wmv,.mpg,.mpeg,.3gp"
        : "audio/*,.wav,.mp3,.m4a,.aac,.flac,.ogg,.opus,.wma,.amr,.aiff";
    }
    const hint = $("asr-source-hint");
    if (hint) {
      hint.textContent = kind_ === "video"
        ? "选择含音轨的视频（只取它的声音）；或填写 / 浏览设备端的文件路径。"
        : "选择音频文件，或填写 / 浏览设备端的文件路径。";
    }
    const pathInput = $("asr-path");
    if (pathInput) {
      pathInput.placeholder = kind_ === "video"
        ? "/userdata/assets/uploads/movie.mp4"
        : "/userdata/assets/uploads/asr_en.wav";
    }

    this._syncSourceUi();
    this._syncLiveUi();          // 实时区块的显隐由「flag + 当前来源」共同决定
    this._syncVideoPlayer();     // 视频来源才有播放器
    // 显示模式要跟着来源走：视频来源下「文字显示框」是字幕同款转写框，
    // 音频文件来源下才是原来的单行「实时结果」。
    this._applyMode();
    if (kind_ !== "video") this._stopSubLoop();
    if (kind_ === "board") {
      this._boardRefresh();
      this._boardLoadDevices();
    }
  },

  /* ------------------------------------------------------------------ *
   * 视频播放（含音轨的视频来源）
   *
   * 两种来源走两条路：
   *   本机上传   → ``blob:`` URL（浏览器本地，不经服务端）
   *   设备端路径 → ``/api/qwen3-asr/media``（Range 支持，可拖进度条）
   *
   * 字幕按**播放头时间**对齐：识别轮次带 ``chunk_start_sec`` /
   * ``chunk_end_sec``（相对**音轨**起点），播放头是相对**容器**起点 ——
   * 两者差一个 ``audio_origin_sec``（音轨在容器里的起点）。
   * ------------------------------------------------------------------ */
  _setFileBlob(file) {
    // 换文件 → 旧的 blob URL 必须 revoke，否则内存一直涨。
    if (this._fileBlobUrl) {
      try { URL.revokeObjectURL(this._fileBlobUrl); } catch (e) { /* noop */ }
      this._fileBlobUrl = "";
    }
    if (file && typeof URL.createObjectURL === "function") {
      try {
        this._fileBlobUrl = URL.createObjectURL(file);
      } catch (e) {
        this._fileBlobUrl = "";
      }
    }
  },

  _releaseVideo() {
    const video = document.getElementById("asr-video");
    if (video) {
      try {
        video.pause();
        video.removeAttribute("src");
        video.load();
      } catch (e) { /* noop */ }
    }
    this._videoSrc = "";
  },

  _videoUrl() {
    if (this._file && this._fileBlobUrl) return this._fileBlobUrl;
    const p = (this._path || "").trim();
    if (!p) return "";
    return API_PREFIX + "/qwen3-asr/media?path=" + encodeURIComponent(p);
  },

  _syncVideoPlayer() {
    const wrap = document.getElementById("asr-video-wrap");
    const video = document.getElementById("asr-video");
    const hint = document.getElementById("asr-video-hint");
    if (!wrap || !video) return;

    const isVideo = this._source === "video";
    wrap.classList.toggle("hidden", !isVideo);
    if (!isVideo) {
      this._releaseVideo();
      if (hint) hint.textContent = "";
      return;
    }

    const url = this._videoUrl();
    if (!url) {
      this._releaseVideo();
      if (hint) {
        hint.textContent = "选一个含音轨的视频（上传或填设备端路径），"
          + "这里会同步播放，字幕跟着画面走。";
      }
      return;
    }

    if (url !== this._videoSrc) {
      this._videoSrc = url;
      video.src = url;
      try { video.load(); } catch (e) { /* noop */ }
    }
    if (hint) {
      hint.textContent = this._file
        ? "正在播本机上传的视频（识别走它的音轨）。"
        : "正在播设备端的视频（识别走它的音轨）。";
    }
    this._renderVideoSubtitle();
  },

  /**
   * **来源变了**时的清理。
   *
   * 字幕是**视频专属**的展示位（音频文件来源下压根没有这个元素），
   * 所以「切到含音轨的视频」的那一刻，字幕条会**凭空冒出来** ——
   * 而它渲染的是**上一来源**留下的轮次。
   *
   * 实测（用户报的 bug）：「音频文件」识别结束后切到「含音轨的视频」，
   * 画面下方多出一句「嗯哼。」—— 那是音频文件第 1 轮的瞬时猜测
   * （``committed='' / unfixed='嗯哼。'``），第 2 轮就被回滚推翻了，
   * 最终文本里根本没有这句；而这个视频**还没开始识别**。
   *
   * 这里只清**字幕**（以及音轨起点偏移）：
   * * 轮次 / 主文本**不清** —— 那是上一次识别的结果，用户可能还要看，
   *   真要清有「清空」按钮；
   * * 但字幕**必须**清 —— 它会把别的媒体的文字画到当前画面上。
   *
   * 双保险：``_renderVideoSubtitle()`` 里还有一道来源守卫
   * （``_roundsSource !== "video"`` 就清空），所以即使识别中途切来源，
   * 字幕也不会串台。
   */
  _onSourceChanged() {
    const sub = document.getElementById("asr-video-sub");
    if (sub) this._subClear(sub);
    // 时间轴缓存也跟着作废 —— 轮次是**上一来源**的。
    this._subInvalidate();
    // 音轨在容器里的起点是**上一个文件**的 → 必须归零，否则播放头换算
    // 播放头 = 容器时间 - 音轨起点
    // 会算错（切到视频后按旧偏移取值 → 字幕取到错误的轮次）。
    this._audioOriginSec = 0;
  },

  /* ------------------------------------------------------------------ *
   * 节奏字幕（YouTube 式实时字幕）
   *
   * 目标：字幕**跟着播放头的位置**走 —— 一个字一个字地点亮，当前
   * 正在说的那个词高亮，说完的行淡出上移。
   *
   * 为什么不需要「实时推送」：服务端是**整文件快转**（1 秒一块），
   * 识别结果在播放开始前就全到手了。所以「实时字幕」= 一张
   * 「字符 → 音频时间」的对照表 + 播放头位置。这里做的全是**本地
   * 时间轴计算**，不碰后端契约。
   * ------------------------------------------------------------------ */

  /** 来源 / 识别批次变了 → 字幕的时间轴缓存作废。 */
  _subInvalidate() {
    this._subEpochCache = [];
    this._subTl = null;
    this._subTlKey = -1;
    this._subSig = "";
    this._subRanges = [];
    this._seekTarget = -1;
  },

  /* ------------------------------------------------------------------ *
   * 「拖动进度条 → 识别跟过去」
   *
   * 背景
   * ----
   * 服务端是**从头顺序**识别整个文件的。识别速度（RTF≈0.18）比播放快
   * 5~6 倍，所以**顺着播**时字幕总是有内容；但用户把进度条**拖到后面**
   * 就会拖到「还没识别到」的位置 → 那里没有字幕（画面在播第 40 分钟的
   * 声音，字幕却还停在第 5 分钟）。
   *
   * 做法
   * ----
   * 1. 记住**已识别的音频时间区间**（``_subRanges``）；
   * 2. 拖动落点不在已识别区间里 → **重开识别**：停掉当前这一轮，
   *    从「拖动点往前 8 秒」重新开始（``start_sec`` + ``append``）；
   * 3. 新一轮的轮次**追加**到已有的轮次后面 → 字幕时间轴变成
   *    「几段拼起来」，按绝对时间排序后照常渲染。
   *
   * ⚠️ 一次只能跑一个识别（RPC session 只有一条 delta 通道，见
   * ``service.py::_ASR_RPC_LOCK``）→ 所以要**先停再开**。
   * ------------------------------------------------------------------ */

  /** 记一段已识别的音频时间（秒，音轨相对起点）。 */
  _subNoteRange(start, end) {
    const s = Number(start);
    const e = Number(end);
    if (!Number.isFinite(s) || !Number.isFinite(e) || e <= s) return;
    const rs = this._subRanges || (this._subRanges = []);
    rs.push({ start: s, end: e });
    // 合并相邻 / 重叠的区间（识别是连续推进的，区间会连成一片）
    rs.sort((a, b) => a.start - b.start);
    const out = [];
    for (const r of rs) {
      const last = out[out.length - 1];
      if (last && r.start <= last.end + 0.5) {
        if (r.end > last.end) last.end = r.end;
      } else {
        out.push({ start: r.start, end: r.end });
      }
    }
    this._subRanges = out;
  },

  /** 这个音频时间**识别过**吗？（字幕已经有内容） */
  _subCovered(t) {
    const x = Number(t);
    if (!Number.isFinite(x)) return true;
    for (const r of (this._subRanges || [])) {
      if (x >= r.start - 0.2 && x <= r.end - SUB_SEEK_COVER_MARGIN_SEC) {
        return true;
      }
    }
    return false;
  },

  /**
   * 拖动 → 必要时把识别**开过去**。
   *
   * 只在「含音轨的视频」来源下做（只有视频有播放头 / 进度条）。
   */
  _maybeFollowSeek() {
    if (this._source !== "video") return;
    if (!this._path && !this._file) return;
    const video = document.getElementById("asr-video");
    if (!video) return;
    const t = (video.currentTime || 0) - (this._audioOriginSec || 0);
    if (!Number.isFinite(t)) return;
    // 已经识别过 → 字幕本来就有，什么都不用做。
    if (this._subCovered(t)) { this._seekPending = null; return; }
    // 正在重开识别 → 记下这次的位置，等当前这次做完再跟过去
    //（用户拖一下不够，还要能「拖着拖着换个地方」）。
    if (this._seekBusy) { this._seekPending = t; return; }
    const now = Date.now();
    if (now < (this._seekGuardUntil || 0)) {
      // 静默期内：只有**换了个位置**才排队。
      if (Math.abs(t - (this._seekTarget ?? -1)) > 1.0) this._seekPending = t;
      return;
    }
    // 同一个位置别重复开（seeked 可能连着触发好几个）。
    if (Math.abs(t - (this._seekTarget ?? -1)) < 1.0) return;
    this._seekTarget = t;
    this._followSeek(t);
  },

  /** 停掉当前识别 → 从 ``audioSec`` 重新开始（保留已识别的轮次）。 */
  async _followSeek(audioSec) {
    this._seekBusy = true;
    const now = Date.now();
    this._seekGuardUntil = now + SUB_SEEK_DEBOUNCE_MS;
    try {
      const hint = document.getElementById("asr-video-hint");
      if (hint) hint.textContent = "正在识别拖到的位置…";
      // ① 停掉正在跑的那一轮（一次只能跑一个识别）
      //
      // ⚠️ **reason=internal** —— 这次停**不是**用户喊停，是前端为了
      // 「重开识别」而停的 ⇒ **画面不能停**（用户正在拖进度条看视频，
      // 停了就没法看了，字幕也得继续跟着播放头走）。后端把 reason
      // 带在 stopped 帧里回来，前端据此**不冻画面**。
      //
      // 踩过的坑：没带 reason 时，stopped 帧一律冻画面 → 拖一下进度条
      // 视频就停了（用户报「拖动进度条后视频和字幕都暂停了」）。
      if (this.snapshot && this.snapshot.busy) {
        await fetch(API_PREFIX + "/qwen3-asr/stop?reason=internal",
                    { method: "POST" })
          .catch(() => {});
        await this._waitIdle(20000);
      }
      // ② 从拖动点往前 8 秒重新开始（容器时间 = 播放头时间）
      const video = document.getElementById("asr-video");
      const containerSec = Math.max(
        0, (video ? (video.currentTime || 0) : audioSec)
           - SUB_SEEK_LEAD_IN_SEC);
      const fd = new FormData();
      if (this._file) {
        fd.append("file", this._file, this._file.name);
      } else {
        fd.append("path", this._path);
      }
      fd.append("start_sec", String(containerSec));
      fd.append("append", "true");
      const r = await fetch(API_PREFIX + "/qwen3-asr/transcribe", {
        method: "POST", body: fd,
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        throw new Error(d.detail || ("HTTP " + r.status));
      }
      // ③ 新一轮的轮次是**追加**的 → 字幕时间轴要重建
      this._subTl = null;
      this._subTlKey = -1;
      this._renderVideoSubtitle();
    } catch (e) {
      const hint = document.getElementById("asr-video-hint");
      if (hint) hint.textContent = "识别拖到的位置失败: " + e;
      console.warn("[qwen3-asr] follow seek failed:", e);
    } finally {
      this._seekBusy = false;
      // 提示文案还原（_syncVideoPlayer 会写回默认的那句）
      this._syncVideoPlayer();
      // 用户在「重开识别」的过程中又拖了别处 → 接着跟过去
      // （否则识别会停在旧的那个位置）。
      const pend = this._seekPending;
      this._seekPending = null;
      if (pend !== null
          && Math.abs(pend - (this._seekTarget ?? -1)) > 1.0) {
        this._seekTarget = pend;
        this._followSeek(pend);
      }
    }
  },

  /** 等服务端**不再忙**（识别线程收尾）。 */
  _waitIdle(timeoutMs) {
    const t0 = Date.now();
    return new Promise((resolve) => {
      const tick = () => {
        if (!this.snapshot || !this.snapshot.busy) { resolve(true); return; }
        if (Date.now() - t0 > timeoutMs) { resolve(false); return; }
        setTimeout(tick, 150);
      };
      tick();
    });
  },

  /**
   * 把 round 帧切成 **epoch**（SDK 会话）。
   *
   * 服务端每 60 秒切一个新会话（``_FILE_SESSION_MAX_SEC``），切的时候
   * ``committed`` 归零、``round`` 编号回到 1 —— 但**音频时间轴是连续
   * 的**（``chunk_start_sec`` 接着上一段往下数）。所以：
   *
   *   * 用「round 编号回退」或「committed 长度变短」认出 epoch 边界；
   *   * 每个 epoch **单独**建时间轴（committed 是段内累积的，跨段
   *     直接拼会错位），再按时间顺序把各段的行拼起来。
   */
  _splitEpochs(rounds) {
    const epochs = [];
    let cur = null;
    let prevRound = null;
    let prevLen = 0;
    for (const r of rounds) {
      const cLen = (r.committed || "").length;
      const rn = typeof r.round === "number" ? r.round : null;
      const isNew = !cur
        || (rn !== null && prevRound !== null && rn <= prevRound)
        || cLen < prevLen;
      if (isNew) { cur = []; epochs.push(cur); }
      cur.push(r);
      if (rn !== null) prevRound = rn;
      prevLen = cLen;
    }
    return epochs;
  },

  /**
   * 一个 epoch 的「字符数 → 音频时间」插值表。
   *
   * 锚点取 **(假设文本与最终文本的最长公共前缀长度, 该轮 chunk 终点)**。
   *
   * 为什么不是「假设文本的长度」：模型每轮的**假设**（committed+unfixed）
   * 会被回滚、也会改标点 —— 第 1 轮在英文音频上先猜了个中文填充词
   * 「嗯哼。」，第 2 轮拿到上下文后整个推翻。而**最终字幕**用的是最后
   * 确认下来的文本 F。所以真正要问的是「到此刻为止，**F 里**已经被说
   * 出来多少字」。
   *
   * committed 一定是 F 的前缀（只增不减），unfixed 不一定 → 公共前缀
   * 长度正好就是这个数。
   *
   * 实测（15 秒英文样本 / 90 秒样本，各按音频能量包络打分）：
   *   假设长度    浊音覆盖 0.728 / 0.689
   *   公共前缀    浊音覆盖 0.750 / 0.717   ← 采用
   * 「浊音覆盖」= 每个词的时间窗有多少落在真实语音段里（1.0 = 完全对上）。
   */
  _epochTable(ep, finalText) {
    const raw = [];
    for (const f of ep) {
      const te = f.chunk_end_sec;
      if (te === undefined || te === null) continue;
      const t = Number(te);
      if (!Number.isFinite(t)) continue;      // 坏时间戳 → 当这一轮不存在
      const hyp = (f.committed || "") + (f.unfixed || "");
      raw.push([_lcp(hyp, finalText), t]);
    }
    // 高水位线：回滚时计数不许倒退（否则插值会出现负斜率）。
    const mono = [];
    let hi = -1;
    for (const pair of raw) {
      const x = pair[0] < hi ? hi : pair[0];
      if (mono.length && mono[mono.length - 1][0] === x) continue;
      hi = x;
      mono.push([x, pair[1]]);
    }
    return mono.length ? mono : null;
  },

  /** 字符下标 → 音频时间（表内线性插值）。 */
  _timeAt(table, idx) {
    if (!table || !table.length) return null;
    if (idx <= table[0][0]) return table[0][1];
    for (let i = 1; i < table.length; i++) {
      const x0 = table[i - 1][0], t0 = table[i - 1][1];
      const x1 = table[i][0], t1 = table[i][1];
      if (idx <= x1) {
        if (x1 <= x0) return t1;
        return t0 + (idx - x0) * (t1 - t0) / (x1 - x0);
      }
    }
    return table[table.length - 1][1];
  },

  /**
   * 一个 epoch 的字幕行。
   *
   * 先把最终文本切成词、给每个词算出时间，再按 YouTube 的习惯分行：
   * 句末标点必断、句中标点够长就断、太长或太长时段也断。
   */
  _epochLines(ep, epochIndex) {
    // 这个 epoch 的最终文本 = 最后一个非空 committed（段内只增不减）
    let finalText = "";
    for (const f of ep) if (f.committed) finalText = f.committed;
    if (!finalText.trim()) return [];
    const table = this._epochTable(ep, finalText);
    if (!table) {
      // 拿不到时间戳（round 帧里没有 chunk 边界）→ 没法做节奏字幕，
      // 退化成「整句一次性显示」（旧行为），而不是什么都不显示。
      const words = _splitWords(finalText).map((w) => ({
        text: w.text, start: 0, end: 0,
      }));
      if (!words.length) return [];
      return [{
        id: epochIndex + ":0:" + words.length + ":0",
        words: words, start: 0, end: 0,
      }];
    }

    const timed = [];
    for (const w of _splitWords(finalText)) {
      const s = this._timeAt(table, w.i0);
      const e = this._timeAt(table, w.i1);
      if (s === null || e === null) continue;
      timed.push({ text: w.text, start: s, end: e < s ? s : e });
    }
    if (!timed.length) return [];

    const lines = [];
    let cur = [];
    let cols = 0;
    const flush = () => {
      if (!cur.length) return;
      lines.push({
        // id = 这行的**内容指纹**：行号 + 词数 + 首尾时间 + **文本**。
        //
        // ⚠️ 文本必须进 id：还在长的那个 epoch 里，模型可能给某个词
        // **补上标点或空格**（词数不变、时间也不变）—— 只拿「词数 +
        // 起始时间」判不出内容变了 → 那一行会永远停在旧文本上
        // （实测：转写框里少了一个空格，画面字幕也是旧的那一版）。
        // 「画面字幕」和「文字转写框」都靠这个 id 判断「要不要重建」，
        // 指纹不准就等于两边显示的不是同一份内容。
        id: epochIndex + ":" + lines.length + ":" + cur.length + ":"
            + cur[0].start.toFixed(3) + ":"
            + cur[cur.length - 1].end.toFixed(3) + ":"
            + cur.map((w) => w.text).join(""),
        words: cur,
        start: cur[0].start,
        end: cur[cur.length - 1].end,
      });
      cur = [];
      cols = 0;
    };
    for (const w of timed) {
      cur.push(w);
      const trim = w.text.trim();
      cols += trim.length + (CJK_RE.test(trim) ? 1 : 0);   // CJK 占两列
      const dur = cur[cur.length - 1].end - cur[0].start;
      if (SUB_HARD_END.test(trim)) flush();
      else if (SUB_SOFT_END.test(trim) && cols >= SUB_MIN_SOFT_CHARS) flush();
      else if (cols >= SUB_MAX_LINE_COLS) flush();
      else if (dur >= SUB_MAX_LINE_SEC) flush();
    }
    flush();
    this._spaceOutLines(lines);
    return lines;
  },

  /**
   * 给每一行兜一个**最短占位时长**。
   *
   * 什么时候会需要：模型偶尔会**卡一下**（同一段音频反复转写），然后
   * 在一秒里把积压的一大段一次性提交。实测（90 秒重复音频）出现
   * 「7 行挤在 1 秒里」→ 字幕一帧一帧地闪过去，根本来不及看。
   *
   * 做法：把后面几行的**开始时间**往后推，保证每行至少占
   * ``SUB_MIN_LINE_SEC``；行内的词整体跟着平移（保持相对关系），
   * 所以「词的时间」和「行的时间」始终一致。
   *
   * 只在本段（epoch）内推 —— 段与段之间本来就是连续的音频，不该被
   * 上一段的排版影响。
   */
  _spaceOutLines(lines) {
    let prevStart = null;
    let prevEnd = null;
    for (const ln of lines) {
      let want = ln.start;
      if (prevStart !== null) {
        want = Math.max(want, prevEnd, prevStart + SUB_MIN_LINE_SEC);
      }
      if (want > ln.start + 1e-9) {
        const d = want - ln.start;
        ln.start = want;
        ln.end += d;
        for (const w of ln.words) { w.start += d; w.end += d; }
      }
      prevStart = ln.start;
      prevEnd = ln.end;
    }
  },

  /**
   * 整条字幕时间轴（各 epoch 的行按时间拼起来）。
   *
   * 缓存粒度 = **epoch**：已结束的 epoch 不会再变（文本已定），只有
   * 最后一段还在长。所以每来一条 round 帧只需重算最后一段 —— 长音频
   * 也不会越跑越卡。
   */
  _buildSubTimeline() {
    const rounds = this.rounds || [];
    const key = rounds.length;
    if (this._subTl && this._subTlKey === key) return this._subTl;
    const epochs = this._splitEpochs(rounds);
    const cache = this._subEpochCache;
    const lines = [];
    // ⚠️ **多段识别**（拖动后重开）→ epoch 的时间**不一定**按顺序来：
    // 用户往回拖过一次，两段的时间就会**倒序**（后一段的起点比前一段早）。
    // 时间轴是按「最后一个 start ≤ 播放头的行」找当前行的 → 必须先按
    // **绝对时间**排序，否则会停在错误的行上。
    const order = epochs.map((ep, i) => ({
      i: i, ep: ep,
      start: (ep.length && Number(ep[0].chunk_start_sec)) || 0,
    }));
    order.sort((a, b) => (a.start - b.start) || (a.i - b.i));
    let guardEnd = -Infinity;
    for (const o of order) {
      const ep = o.ep;
      let hit = cache[o.i];
      if (!hit || hit.key !== ep.length) {
        hit = { key: ep.length, lines: this._epochLines(ep, o.i) };
        cache[o.i] = hit;
      }
      for (const ln of hit.lines) {
        // 重叠区（两段识别到了同一段音频）：保留**先开始**的那一段 ——
        // 它是在有上下文的时候转写的（后一段从中间开始，起点是零上下文）。
        if (ln.start < guardEnd - 1e-6) continue;
        lines.push(ln);
      }
      const last = hit.lines[hit.lines.length - 1];
      if (last && last.end > guardEnd) guardEnd = last.end;
    }
    if (cache.length > epochs.length) cache.length = epochs.length;
    this._subTl = { lines };
    this._subTlKey = key;
    return this._subTl;
  },

  /** 常驻 3 个行槽（内容变了才重建内部 span，淡出动画才不会断）。 */
  _subEnsureSlots(sub) {
    if (this._subSlots.length === SUB_MAX_VISIBLE_LINES
        && sub.children.length === SUB_MAX_VISIBLE_LINES) {
      return this._subSlots;
    }
    sub.innerHTML = "";
    this._subSlots = [];
    for (let i = 0; i < SUB_MAX_VISIBLE_LINES; i++) {
      const el = document.createElement("div");
      el.className = "asr-sub-line";
      el.style.opacity = "0";
      el.style.visibility = "hidden";
      sub.appendChild(el);
      this._subSlots.push({ el, id: "", spans: [] });
    }
    return this._subSlots;
  },

  _subClear(sub) {
    if (!sub.firstChild && this._subSig === "") return;
    this._subSig = "";
    sub.innerHTML = "";
    this._subSlots = [];
  },

  /**
   * 逐帧刷新字幕。
   *
   * ``timeupdate`` 只有 4Hz，撑不起「逐词点亮」—— 高亮会一跳一跳的。
   * 所以播放期间挂一个 rAF 循环，每帧算一次；真正改 DOM 只在**签名
   * 变了**的时候（见 ``_renderVideoSubtitle``）。
   */
  _startSubLoop() {
    if (this._subRaf) return;
    const tick = () => {
      this._subRaf = requestAnimationFrame(tick);
      this._renderVideoSubtitle();
    };
    this._subRaf = requestAnimationFrame(tick);
  },

  _stopSubLoop() {
    if (this._subRaf) {
      cancelAnimationFrame(this._subRaf);
      this._subRaf = 0;
    }
  },

  /**
   * 一行在时刻 ``t`` 的**词状态**：已经点亮到第几个词、当前正在说的
   * 是第几个词。
   *
   * 画面字幕和文字转写框**共用**这一个函数 —— 「显示效果一样」不是
   * 「看起来像」，是**同一份算法算出来的**；两边各写一份的话，早晚
   * 会对不上（高亮的位置差一帧这种问题最难查）。
   */
  _revealState(ln, t) {
    let revealed = 0;
    let current = -1;
    for (let i = 0; i < ln.words.length; i++) {
      const w = ln.words[i];
      if (w.start <= t) {
        revealed = i + 1;
        if (t < w.end) current = i;
      }
    }
    return { revealed: revealed, current: current };
  },

  /**
   * 按播放头渲染字幕。
   *
   * 渲染规则（YouTube 实时字幕那套）：
   *   * 当前行 = 最后一个「开始时间 ≤ 播放头」的行；
   *   * 行内的词**逐个点亮**（还没说到的词占位但不可见）；
   *   * 正在说的那个词高亮；
   *   * 说完的行保留一小会儿（淡出），再上面就没了。
   */
  _renderVideoSubtitle() {
    // 文字转写框（视频来源下的「文字显示框」）跟画面字幕**同步**渲染
    // —— 同一帧、同一份时间轴、同一份词状态算法。
    this._renderTranscript();
    const video = document.getElementById("asr-video");
    const sub = document.getElementById("asr-video-sub");
    if (!video || !sub) { this._stopSubLoop(); return; }
    // ⚠️ 轮次必须来自**视频**这次识别。上一来源（音频文件）的轮次在
    // 切换来源时仍然留在 ``this.rounds`` 里 —— 拿它渲染字幕，就会把
    // 别的媒体的轮次画到这个视频画面上（实测：切到视频后字幕显示
    // 「嗯哼。」，而视频当时还没开始识别）。
    if (this._source !== "video" || this._roundsSource !== "video") {
      this._subClear(sub);
      return;
    }
    const lines = this._buildSubTimeline().lines;
    if (!lines.length) { this._subClear(sub); return; }

    // 播放头（容器时间）→ 音频时间（chunk 时间戳也是音轨相对时间）
    const t = (video.currentTime || 0) - (this._audioOriginSec || 0);

    let cur = -1;
    for (let i = 0; i < lines.length; i++) {
      if (lines[i].start <= t) cur = i; else break;
    }
    if (cur < 0) { this._subClear(sub); return; }

    // 可见行：当前行 + 上面还没淡出的行
    const want = [];
    for (let i = cur; i >= 0 && want.length < SUB_MAX_VISIBLE_LINES; i--) {
      const ln = lines[i];
      if (i < cur && t - ln.end > SUB_PREV_HOLD_SEC) break;
      want.unshift(ln);
    }

    // 每行：已经点亮到第几个词 + 当前正在说的词
    const states = want.map((ln) => {
      const rv = this._revealState(ln, t);
      return { ln: ln, revealed: rv.revealed, current: rv.current };
    });

    // 签名没变 → 什么都不用动（rAF 每秒 60 次，别白折腾 DOM）
    const sig = states.map((s) => s.ln.id + ":" + s.revealed + ":" + s.current).join(";");
    if (sig === this._subSig) return;
    this._subSig = sig;

    const slots = this._subEnsureSlots(sub);
    const n = states.length;
    const offset = slots.length - n;
    for (let i = 0; i < slots.length; i++) {
      const slot = slots[i];
      const st = i >= offset ? states[i - offset] : null;
      if (!st) {
        slot.el.style.opacity = "0";
        slot.el.style.visibility = "hidden";
        continue;
      }
      slot.el.style.visibility = "visible";
      // 越旧越淡（当前行最亮）
      const age = n - 1 - (i - offset);
      slot.el.style.opacity = age === 0 ? "1" : (age === 1 ? "0.7" : "0.42");
      if (slot.id !== st.ln.id) {
        slot.id = st.ln.id;
        slot.el.innerHTML = "";
        slot.spans = st.ln.words.map((w) => {
          const sp = document.createElement("span");
          sp.className = "asr-sub-word";
          sp.textContent = w.text;
          slot.el.appendChild(sp);
          return sp;
        });
      }
      for (let k = 0; k < slot.spans.length; k++) {
        const spoken = k < st.revealed;
        slot.spans[k].classList.toggle("asr-sub-pending", !spoken);
        slot.spans[k].classList.toggle("asr-sub-current", spoken && k === st.current);
      }
    }
  },

  /* ------------------------------------------------------------------ *
   * 字幕同款转写框（视频来源下的「文字显示框」）
   *
   * 用户要求：「含音轨的视频」来源下，主文字框也按**实时字幕的格式**
   * 输出 —— 同样的分行、同样的逐词点亮、同样的当前词高亮 ——
   * 而且**历史全部保留**，框可以**上拉回看**。
   *
   * 和画面字幕的差别只有一个：**范围**
   *   * 画面字幕：当前那几行，说完就淡出（历史不留）；
   *   * 转写框：整条时间轴的**所有行**都在，按播放头标出「当前行」。
   *
   * 所以两者共用同一份时间轴（``_buildSubTimeline``）和同一个词状态
   * 算法（``_revealState``）——「显示效果一样」不是「看起来像」，是
   * **同一份代码算出来的**。
   *
   * 性能
   * ----
   * 98 分钟的片子时间轴有几千行，每帧重建整个 DOM 是不可接受的。
   * 所以：① 行按 **id** 增量维护（只重建变了的行）；② 每帧只更新
   * **当前行**的词状态（几十个 span），其余行不动。
   * ------------------------------------------------------------------ */

  /**
   * 渲染文字转写框。
   *
   * 只在「含音轨的视频」来源下生效；其它来源（音频文件 / 麦克风）
   * 走原来的单行「实时结果」。
   */
  _renderTranscript() {
    const box = document.getElementById("asr-transcript");
    if (!box) return;
    const live = document.getElementById("asr-live");
    const isVideo = this._source === "video" && this._roundsSource === "video";
    if (live) {
      live.classList.toggle("asr-live-video", isVideo);
      // ⚠️ **实时来源的归属守卫** —— 单行文字框显示的是「**最近一批**」
      // 的文本。切到**实时来源**（麦克风）时，那一行是**文件批次**的历史，
      // 而麦克风的文字在它自己的框里（``#asr-rt-committed``）→ 藏起来，
      // 别让两段文字并排出现。
      //
      // ⚠️ **文件 ↔ 视频之间不藏**：那两栏是同一批结果的两种视图
      // （视频来源多一个「字幕同款转写框」）—— 藏了的话，视频的识别
      // 结果在切回文件页签后就**彻底看不见**了（U18 的契约）。
      // 真正要挡住的是「画到画面上」的那几处：视频字幕 / 转写框靠
      // ``_roundsSource`` 把关（见 ``_renderVideoSubtitle``）。
      live.classList.toggle("asr-live-notfile", !this._isFileSource());
    }
    box.classList.toggle("hidden", !isVideo);
    if (!isVideo) { this._transcriptClear(); return; }

    const tl = this._buildSubTimeline();
    const lines = tl.lines || [];
    if (!lines.length) { this._transcriptClear(); return; }

    // 时间轴换了 → 行要重排（只重建**变了的**那几行）
    let rebuilt = false;
    if (this._trTl !== tl) {
      this._trTl = tl;
      this._transcriptSync(lines);
      rebuilt = true;
    }

    // 播放头（容器时间）→ 音频时间
    const video = document.getElementById("asr-video");
    const t = (video ? (video.currentTime || 0) : 0) - (this._audioOriginSec || 0);

    // 当前行 = 最后一个「开始时间 ≤ 播放头」的行
    let cur = -1;
    for (let i = 0; i < lines.length; i++) {
      if (lines[i].start <= t) cur = i; else break;
    }
    this._transcriptPaint(lines, cur, t, rebuilt);
    this._transcriptScroll(cur);
  },

  /**
   * 让 DOM 里的行与时间轴**对齐**（增量）。
   *
   * 时间轴是「往后长」的（每来一条 round 帧多几行），所以**公共前缀**
   * 直接复用，只重建后面变掉的那几行 —— 长片子也是 O(变化量)。
   */
  _transcriptSync(lines) {
    const body = document.getElementById("asr-transcript-body");
    if (!body) return;
    const rows = this._trRows;
    let keep = 0;
    const n = Math.min(rows.length, lines.length);
    while (keep < n && rows[keep].id === lines[keep].id) keep += 1;
    // 变掉的后缀：先拆掉旧的，再按新顺序追加（顺序自然对齐）
    for (let i = keep; i < rows.length; i++) {
      if (rows[i].el.parentNode === body) body.removeChild(rows[i].el);
    }
    rows.length = keep;
    for (let i = keep; i < lines.length; i++) {
      const ln = lines[i];
      const el = document.createElement("div");
      el.className = "asr-sub-line asr-tr-future";
      el.dataset.lineId = ln.id;
      const spans = [];
      for (const w of ln.words) {
        const sp = document.createElement("span");
        sp.className = "asr-sub-word";
        sp.textContent = w.text;
        el.appendChild(sp);
        spans.push(sp);
      }
      body.appendChild(el);
      rows.push({ id: ln.id, el: el, spans: spans, state: "future" });
    }
  },

  /**
   * 给行标状态（过去 / 当前 / 未来），并更新**当前行**的词状态。
   *
   * 平时只碰「状态发生变化的那一段」—— 当前行往前挪一步，就只改这两行；
   * 往回拖很多秒也只是多改几行，不会全量重刷。
   *
   * 但**时间轴重排过**（拖动重开识别 → 新的一段插到中间）时，行的
   * 下标全变了 → 得**全量**刷一遍状态，否则会出现「已经说过的行还
   * 标着未来（压暗）」的错位。
   */
  _transcriptPaint(lines, cur, t, full) {
    const rows = this._trRows;
    if (!rows.length) return;
    const lo = full ? 0 : Math.min(cur, this._trCur);
    const hi = full ? rows.length - 1 : Math.max(cur, this._trCur);
    for (let i = lo; i <= hi; i++) {
      if (i < 0 || i >= rows.length) continue;
      const st = i < cur ? "past" : (i === cur ? "current" : "future");
      this._transcriptSetState(rows[i], st);
    }
    // 当前行：逐词点亮 + 当前词高亮（与画面字幕同一份算法）
    if (cur >= 0 && cur < rows.length) {
      const rv = this._revealState(lines[cur], t);
      const spans = rows[cur].spans;
      for (let k = 0; k < spans.length; k++) {
        const spoken = k < rv.revealed;
        spans[k].classList.toggle("asr-sub-pending", !spoken);
        spans[k].classList.toggle("asr-sub-current", spoken && k === rv.current);
      }
    }
    this._trCur = cur;
  },

  /** 切换一行的状态（过去 / 当前 / 未来）。 */
  _transcriptSetState(row, st) {
    if (row.state === st) return;
    const was = row.state;
    row.state = st;
    row.el.classList.toggle("asr-tr-past", st === "past");
    row.el.classList.toggle("asr-tr-future", st === "future");
    row.el.classList.toggle("asr-tr-current", st === "current");
    // 离开「当前行」→ 把逐词点亮的状态收掉（整行都算「已说过」）
    if (was === "current" && st !== "current") {
      for (const sp of row.spans) {
        sp.classList.remove("asr-sub-pending");
        sp.classList.remove("asr-sub-current");
      }
    }
  },

  /**
   * 滚动到当前行（只在它**看不见**的时候才动）。
   *
   * 已经在视野里 → 一点不动（用户正在看它，别把画面推走）；
   * 看不见了 → 把它带回来（这是「跟着播放头走」）。
   */
  _transcriptScroll(cur) {
    if (!this._trFollow) return;
    const body = document.getElementById("asr-transcript-body");
    if (!body || cur < 0 || cur >= this._trRows.length) return;
    // 框没布局（隐藏 / 高度为 0）→ 别乱滚
    if (!body.clientHeight) return;
    const row = this._trRows[cur].el;
    const top = row.offsetTop;
    const h = row.offsetHeight;
    const viewTop = body.scrollTop;
    const viewBottom = viewTop + body.clientHeight;
    if (top >= viewTop && top + h <= viewBottom) return;
    this._trAutoAt = Date.now();
    body.scrollTop = (top < viewTop)
      ? Math.max(0, top - 4)
      : Math.max(0, top + h - body.clientHeight + 4);
  },

  /**
   * 用户**手动**滚动时的处理。
   *
   * 用户上翻回看 → 当前行看不见了 → **暂停跟随**（别把人拽回来）；
   * 滚回当前行附近 → 恢复跟随。
   */
  _onTranscriptScroll() {
    // 自己触发的滚动不算「用户滚动」
    if (Date.now() - (this._trAutoAt || 0) < 250) return;
    const body = document.getElementById("asr-transcript-body");
    if (!body) return;
    if (!body.clientHeight) return;      // 没布局 → 不动跟随状态
    const cur = this._trCur;
    if (cur < 0 || cur >= this._trRows.length) { this._trFollow = false; return; }
    const row = this._trRows[cur].el;
    const top = row.offsetTop;
    const h = row.offsetHeight;
    const visible = (top < body.scrollTop + body.clientHeight)
                 && (top + h > body.scrollTop);
    this._trFollow = visible;
  },

  /** 清空转写框（切来源 / 清空按钮 / 还没识别）。 */
  _transcriptClear() {
    const body = document.getElementById("asr-transcript-body");
    if (body && body.firstChild) body.innerHTML = "";
    this._trRows = [];
    this._trTl = null;
    this._trCur = -1;
    this._trFollow = true;
  },

  _playVideoIfAny() {
    if (this._source !== "video") return;
    const video = document.getElementById("asr-video");
    if (!video || !video.src) return;
    const p = video.play();
    if (p && typeof p.catch === "function") {
      p.catch((e) => {
        // 自动播被浏览器策略拦下（少见：点「开始识别」已是用户手势）
        // → 不算错误，用户手动点一下播放键即可。
        console.info("[qwen3-asr] 视频自动播放未执行:", e && e.name);
      });
    }
  },

  /* ------------------------------------------------------------------ *
   * 板载麦克风（设备端 ALSA → 独立采集进程）
   * ------------------------------------------------------------------ */
  async _boardRefresh() {
    try {
      const r = await fetch(API_PREFIX + "/qwen3-asr/live/board/status");
      const d = await r.json().catch(() => ({}));
      if (!r.ok) return;
      this._boardState = d;
      this._boardRender();
    } catch (e) { /* best-effort */ }
  },

  _boardRender() {
    const $ = (id) => document.getElementById(id);
    const st = this._boardState || {};
    const running = !!st.running;
    const startBtn = $("asr-board-start");
    const stopBtn = $("asr-board-stop");
    const status = $("asr-board-status");
    const sel = $("asr-board-device");
    if (startBtn) startBtn.disabled = running;
    if (stopBtn) stopBtn.disabled = !running;
    if (sel) sel.disabled = running;
    if (status) {
      if (running) {
        status.textContent = `采集中（${st.device || ""} · pid ${st.pid || 0}）`;
      } else if (st.last_exit !== undefined && st.last_exit !== null) {
        status.textContent = `已停止（退出码 ${st.last_exit}）`;
      } else {
        status.textContent = "未开始";
      }
    }
    const hint = $("asr-board-hint");
    if (hint && !running && st.log) {
      hint.textContent = `采集日志：${st.log}`;
    }
  },

  async _boardLoadDevices() {
    const $ = (id) => document.getElementById(id);
    const sel = $("asr-board-device");
    if (!sel) return;
    try {
      const r = await fetch(API_PREFIX + "/qwen3-asr/live/board/devices");
      const d = await r.json().catch(() => ({}));
      if (!r.ok || !Array.isArray(d.devices)) return;
      sel.innerHTML = "";
      d.devices.forEach((dev) => {
        const opt = document.createElement("option");
        opt.value = dev.name;
        opt.textContent = `${dev.name} — ${dev.description || ""}`;
        if (dev.default) opt.selected = true;
        sel.appendChild(opt);
      });
    } catch (e) { /* best-effort */ }
  },

  async _boardStart() {
    const $ = (id) => document.getElementById(id);
    const sel = $("asr-board-device");
    const device = sel ? sel.value : "";
    const status = $("asr-board-status");
    if (status) status.textContent = "启动中…";
    try {
      const r = await fetch(API_PREFIX + "/qwen3-asr/live/board/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ device: device }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) {
        if (status) status.textContent = "启动失败：" + (d.detail || `HTTP ${r.status}`);
        return;
      }
      this._boardState = d;
      this._boardRender();
    } catch (e) {
      if (status) status.textContent = "启动失败：" + e;
    }
  },

  async _boardStop() {
    const $ = (id) => document.getElementById(id);
    const status = $("asr-board-status");
    try {
      const r = await fetch(API_PREFIX + "/qwen3-asr/live/board/stop",
                           { method: "POST" });
      const d = await r.json().catch(() => ({}));
      if (status) {
        status.textContent = r.ok ? "已停止" : ("停止失败：" + (d.detail || r.status));
      }
      await this._boardRefresh();
    } catch (e) {
      if (status) status.textContent = "停止失败：" + e;
    }
  },

  /**
   * 诊断钩子：把重采样器暴露给回归脚本。
   *
   * ``window.__asrLiveResample(float32, srcRate)`` → s16le 16 kHz 字节。
   * ``scripts/verify_asr_wizard.py`` 的 **V6a** 用它和 Python 的
   * ``StreamResampler`` 做**逐字节**比对 —— 「跨端一致」（清单 B3）
   * 的可执行判据就落在这里。
   */
  _installLiveHooks() {
    if (window.__asrLiveResample) return;
    window.__asrLiveResample = (f32, srcRate) => {
      const st = {
        carry: new Int32Array(0),
        outIndex: 0,
        totalIn: 0,
        ratio: srcRate / 16000,
      };
      const a = this._resampleTo16k(new Float32Array(f32), srcRate, st);
      const b = this._resampleFlush(st, srcRate);
      const out = new Uint8Array(a.length + b.length);
      out.set(a, 0);
      out.set(b, a.length);
      return out;
    };
  },

  _busy() {
    return !!(this.snapshot && this.snapshot.busy);
  },

  _hasSource() {
    return !!(this._file || (this._path && this._path.trim()));
  },

  /** 当前来源的描述（给提示行用）。 */
  _sourceLabel() {
    if (this._file) return "本机上传：" + this._file.name;
    if (this._path) return "设备端：" + this._path;
    return "";
  },

  _syncSourceUi() {
    const $ = (id) => document.getElementById(id);
    const fileName = $("asr-file-name");
    if (fileName && this._path) fileName.textContent = "";
    // 「开始识别」只对**文件 / 视频**来源有意义 —— 实时两种来源各有各
    // 的按钮（板载采集 / 浏览器麦克风）。
    const startBtn = $("asr-start-btn");
    if (startBtn) {
      startBtn.disabled = !this._isFileSource() || !this._hasSource()
        || this._busy() || this._starting;
    }
  },

  /** 当前来源是不是「文件 / 视频」（决定「开始识别」按钮是否可用）。 */
  _isFileSource() {
    return this._source === "file" || this._source === "video";
  },

  /**
   * 流式参数**不再由前端传**。
   *
   * 分块长度 / 每块 token 上限 / 回退 token / 前几轮不提交这四个值
   * 等于 C++ demo 的 ONLINE_CHUNK_MS / ONLINE_MAX_NEW_TOKENS /
   * ONLINE_ROLLBACK_TOKENS / ONLINE_ROLLBACK_ROUNDS —— 它们是
   * 「与 C demo 对齐」的**内部契约**，不是用户该调的东西（每一段音频
   * 都要解析，没有「跳过某些段」这回事），所以既不在向导里、也不在
   * 功能页里露出来。
   *
   * 不传 → SDK 用自己的 ONLINE_*_DEFAULT（1.0 / 50 / 5 / 4，与 C demo
   * 一致）。**单一真值源留在 SDK**，前端不再复制一份常量。
   */
  _applyMode() {
    const mode = document.getElementById("asr-mode")?.value || "single";
    const live = document.getElementById("asr-live");
    const rounds = document.getElementById("asr-rounds");
    if (live) live.classList.toggle("hidden", mode !== "single");
    if (rounds) rounds.classList.toggle("hidden", mode !== "rounds");
    // 面板刚露出来 → 把这段时间攒下的轮次补上（隐藏时是**不重建**的）
    if (mode === "rounds") this._renderRounds();
    // 视频来源下「文字显示框」= 字幕同款转写框 → 切模式时要重画。
    this._renderTranscript();
  },

  async _probeCap() {
    let data;
    try {
      const r = await fetch(API_PREFIX + "/qwen3-asr/", { cache: "no-store" });
      if (!r.ok) return null;
      data = await r.json();
    } catch (_) {
      return null;
    }
    this._applySnapshot(data);
    const banner = document.getElementById("asr-unavailable");
    if (banner) {
      // capability 未启用时后端会返回 502；这里用 model_id 判断。
      const missing = !data || !data.model_id;
      banner.classList.toggle("hidden", !missing);
    }
    return data;
  },

  _applySnapshot(snap) {
    if (!snap) return;
    this.snapshot = snap;
    // 音轨在容器里的起点 —— 视频来源要靠它换算播放头 ↔ 音频时间。
    if (snap.audio_origin_sec !== undefined && snap.audio_origin_sec !== null) {
      this._audioOriginSec = Number(snap.audio_origin_sec) || 0;
    }
    const state = snap.state || "idle";
    const badge = document.getElementById("asr-state-badge");
    if (badge) {
      badge.dataset.state = state;
      badge.textContent = {
        idle: "空闲", running: "识别中", done: "完成", error: "出错", stopped: "已停止",
      }[state] || state;
    }
    const line = document.getElementById("asr-status-line");
    if (line) {
      line.textContent = snap.busy
        ? `识别中 · 第 ${snap.round_count || 0} 轮`
        : (snap.error ? "出错" : (snap.language ? `语种 ${snap.language}` : "空闲"));
    }
    const lang = document.getElementById("asr-language");
    if (lang) lang.textContent = snap.language ? `语种：${snap.language}` : "";

    const startBtn = document.getElementById("asr-start-btn");
    if (startBtn) startBtn.disabled = !this._hasSource() || snap.busy || this._starting;
    const stopBtn = document.getElementById("asr-stop-btn");
    if (stopBtn) stopBtn.disabled = !snap.busy;

    const empty = document.getElementById("asr-empty");
    if (empty) empty.classList.toggle("hidden", this.rounds.length > 0 || !!snap.last_result);

    this._updateHint();
    this._syncSourceUi();
    this._syncLiveUi();
    if (typeof DemoGuard !== "undefined") {
      // 实时会话在跑时，快照的 busy 必须是 true —— DemoGuard 靠它决定
      // 「离开这张卡要不要拦一下」。
      const snap = { ...(this.snapshot || {}) };
      if (this._live.active) {
        snap.busy = true;
        snap.state = "running";
      }
      DemoGuard.report("qwen3-asr", { session: snap });
    }
  },

  /**
   * 实时区块的显隐 —— 由后端的 feature flag 决定。
   *
   * flag 关掉时后端返回 404，前端**整块隐藏**（B11：旧路径零影响）。
   * 快照里带 ``live_enabled``，前端不用再单独探测一个端点。
   */
  _syncLiveUi() {
    const box = document.getElementById("asr-live-block");
    if (!box) return;
    const enabled = !!(this.snapshot && this.snapshot.live_enabled);
    // flag 关掉 → 整块隐藏；当前来源是文件 / 视频 → 也隐藏（实时区块
    // 只在选了实时来源时出现）。
    box.classList.toggle("hidden", !enabled || this._isFileSource());
    const btn = document.getElementById("asr-mic-btn");
    if (btn) {
      btn.disabled = !enabled || this._live.active || this._live.starting;
      btn.textContent = this._live.active ? "结束实时识别" : "开始实时识别";
    }
    const stop = document.getElementById("asr-mic-stop-btn");
    if (stop) stop.disabled = !this._live.active;
    const boardStart = document.getElementById("asr-board-start");
    if (boardStart && this._boardState) {
      boardStart.disabled = !enabled || !!this._boardState.running;
    }
    // 非安全上下文 → 浏览器不给麦克风接口。提前说清楚，别让用户点了
    // 才发现（点了也只会看到一句没头没尾的「已停止」）。
    const micHint = document.getElementById("asr-mic-hint");
    if (micHint) {
      const insecure = !window.isSecureContext;
      micHint.classList.toggle("hidden", !insecure);
      if (insecure) {
        micHint.textContent =
          "当前地址不是安全上下文（" + location.protocol + "//"
          + location.hostname + "），浏览器不会提供麦克风接口。"
          + "请改用 https://" + location.hostname + ":8443 访问，"
          + "或改用「板载麦克风」。";
      }
    }
  },

  _liveStatus(text) {
    const el = document.getElementById("asr-live-status");
    if (el) el.textContent = text || "";
  },

  _liveGapHint(msg) {
    const el = document.getElementById("asr-live-gap");
    if (!el) return;
    if (msg && msg.dropped_chunks) {
      el.textContent =
        `⚠ 有音频丢失（${msg.dropped_chunks} 块），识别结果可能有缺口`;
    } else {
      el.textContent = "⚠ 有音频丢失，识别结果可能有缺口";
    }
    el.classList.remove("hidden");
  },

  _liveClearGap() {
    const el = document.getElementById("asr-live-gap");
    if (el) el.classList.add("hidden");
  },

  _liveRender() {
    const L = this._live;
    const c = document.getElementById("asr-rt-committed");
    const u = document.getElementById("asr-rt-unfixed");
    const head = L.segments.length ? L.segments.join(" ") + " " : "";
    if (c) c.textContent = head + (L.committed || "");
    if (u) u.textContent = L.unfixed || "";
  },

  _updateHint() {
    const hint = document.getElementById("asr-hint");
    if (!hint) return;
    const st = (this.snapshot && this.snapshot.state) || "";
    if (!this._hasSource()) {
      hint.textContent = "选择一个音频来源后点「开始识别」。模型按 1 秒一块增量解码，输出实时刷新。";
    } else if (this._busy()) {
      hint.textContent = "识别中…… 已确认的文本会实时出现，蓝色是尚未确认的部分。";
    } else if (st === "stopped" && !this._seekBusy) {
      // ⚠️ 「已停止」和「完成」要说清楚区别 —— 用户点「停止」是**提前
      // 收尾**：已识别的那部分留下了，画面也停住了（字幕不再滚）。
      hint.textContent = "已停止 —— 已识别的内容保留在下方转写框里；"
        + "要重跑点「开始识别」。";
    } else {
      hint.textContent = "点击「开始识别」重新跑一遍。";
    }
  },

  // ------------------------------------------------------------------ 实时
  /**
   * 离开页面 / 切走这张卡 → 停掉麦克风 + 停掉后端实时流。
   *
   * ⚠️ **不动 framework 的注入逻辑**（AGENTS.md 明令）—— 这里只在自己
   * 的 app.js 里挂两个监听器，不碰 ``_loadedApps`` / ``_wizardMounted``。
   *
   * 后端那条 ``DELETE /live/stop`` 由 manifest 的 ``demo_guard`` 兜底
   * （页面关闭 / 刷新时走 ``sendBeacon``）；这里负责的是**浏览器侧**
   * 的设备资源（麦克风轨道 + AudioContext）—— 那些是本浏览器的东西，
   * 服务端关不掉。
   */
  _bindLiveGuards() {
    if (this._liveGuardsBound) return;
    this._liveGuardsBound = true;
    window.addEventListener("hashchange", () => {
      const cur = (location.hash || "").replace(/^#/, "");
      if (cur !== "qwen3-asr") {
        if (this._live.active) this._liveAbort();
        // 板载采集是**设备端进程** —— 离开页面必须把它停掉，
        // 否则声卡一直被占着。
        if (this._boardState && this._boardState.running) {
          this._boardStop();
        }
      }
    });
    window.addEventListener("pagehide", () => {
      if (this._live.active) this._liveAbort();
      if (this._boardState && this._boardState.running) {
        // pagehide 里 fetch 不一定送达 → 用 keepalive 兜底
        try {
          fetch(API_PREFIX + "/qwen3-asr/live/board/stop", {
            method: "POST", keepalive: true,
          });
        } catch (e) { /* best-effort */ }
      }
    });
  },

  async _liveToggle() {
    if (this._live.active) {
      await this._liveFinish();
    } else {
      await this._liveStart();
    }
  },

  async _liveStart() {
    const L = this._live;
    if (L.active || L.starting) return;
    L.starting = true;
    this._liveStatus("正在打开麦克风…");
    this._syncLiveUi();
    try {
      const r = await fetch(API_PREFIX + "/qwen3-asr/live/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source: "browser",
          sample_rate: 16000,
          n_channels: 1,
          sample_width: 2,
          chunk_ms: LIVE_CHUNK_MS,
        }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) {
        this._liveStatus("无法开始：" + (data.detail || `HTTP ${r.status}`));
        return;
      }
      L.streamId = data.stream_id || "";
      L.seq = 0;
      L.queue.length = 0;
      L.dropped = 0;
      L.serverDropped = 0;
      L.segments = [];
      L.committed = "";
      L.unfixed = "";
      L.buf = null;
      L.resampler = null;
      this._liveClearGap();
      this._liveRender();
      await this._liveOpenMic();
      L.active = true;
      this._liveStatus("识别中 · 说句话试试");
    } catch (e) {
      // 失败原因要**留住** —— ``_liveAbort(status)`` 不会再用「已停止」盖掉它。
      this._liveStatus("无法开始：" + e);
      await this._liveAbort("无法开始：" + e);
    } finally {
      L.starting = false;
      this._syncLiveUi();
    }
  },

  /** 打开麦克风 + 建音频图。优先 AudioWorklet，ScriptProcessor 仅作回退。 */
  async _liveOpenMic() {
    const L = this._live;
    // ⚠️ **非安全上下文 → 浏览器根本不提供麦克风 API**。
    //
    // ``navigator.mediaDevices`` 只在**安全上下文**里存在：HTTPS，或
    // http://localhost / 127.0.0.1。用局域网 IP 走明文 http:// 访问时，
    // 它是 ``undefined`` —— 点「开始实时识别」什么都不会发生。
    //
    // 所以这一条要**单独报**，不能跟「浏览器不支持」混为一谈：后者要
    // 换浏览器，前者只要换访问方式（https://…:8443）或改用板载麦克风。
    if (!window.isSecureContext) {
      throw new Error(
        "当前不是安全上下文（" + location.protocol + "//" + location.host
        + "），浏览器不提供麦克风接口；"
        + "请改用 https://" + location.hostname + ":8443 访问，"
        + "或用「板载麦克风」");
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      throw new Error("这个浏览器不支持麦克风采集");
    }
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    L.stream = stream;
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) throw new Error("这个浏览器不支持 WebAudio");
    // ⚠️ **不指定 sampleRate** —— 让 AudioContext 跑在设备的原生采样率
    // （通常 44.1/48 kHz），重采样由**我们自己的**线性插值做。
    //
    // 为什么不用 ``new Ctx({sampleRate: 16000})``：那样 WebAudio 会用
    // **它自己的**重采样器（带滤波和延迟），而板载路径用的是我们的
    // 线性插值 —— 两端算法不同 → 同一段录音从两端进来可能得到不同的
    // 16 kHz PCM → 文本分叉（清单 B3「跨端一致」）。
    //
    // 代价：线性插值比 WebAudio 的 sinc 重采样器**质量略差**。但板载
    // 路径本来就用它（要和 SDK 的 ``_resample_linear`` 同款），**统一
    // 算法比统一质量更重要**。
    const ctx = new Ctx();
    L.ctx = ctx;
    // 诊断钩子：浏览器**可能**拿不到 48 kHz（某些设备是 44.1 kHz），
    // 现场排查时看一眼这个值就知道重采样比是多少。
    window.__asrLiveCtx = ctx;
    if (ctx.state === "suspended") await ctx.resume();
    const src = ctx.createMediaStreamSource(stream);
    L.source = src;

    // 节点必须连到 destination 才会被音频图驱动；用 gain=0 的静音支路
    // 免得麦克风声音从扬声器放出来（啸叫）。
    const mute = ctx.createGain();
    mute.gain.value = 0;
    mute.connect(ctx.destination);

    let node = null;
    if (ctx.audioWorklet) {
      try {
        await ctx.audioWorklet.addModule(
          `${STATIC_PREFIX}/apps/qwen3_asr/capture-worklet.js`);
        node = new AudioWorkletNode(ctx, "asr-capture");
        node.port.onmessage = (ev) => {
          if (ev.data && ev.data.samples) this._liveOnSamples(ev.data.samples);
        };
      } catch (e) {
        console.warn("[qwen3-asr] AudioWorklet 不可用，回退 ScriptProcessor:", e);
        node = null;
      }
    }
    if (!node) {
      // ScriptProcessor **已废弃**，但仍是唯一回退。
      node = ctx.createScriptProcessor(4096, 1, 1);
      node.onaudioprocess = (ev) => {
        this._liveOnSamples(ev.inputBuffer.getChannelData(0));
      };
    }
    src.connect(node);
    node.connect(mute);
    L.node = node;
  },

  /** 收到一段 Float32 采样 → 电平表 + 重采样 + 攒块 + 上行。 */
  _liveOnSamples(samples) {
    const L = this._live;
    if (!samples || !samples.length) return;
    this._liveLevel(samples);
    const pcm = this._liveResample(samples);
    this._liveAppend(pcm);
  },

  /** 把 16 kHz 的 PCM 攒成 500 ms 一块并入队。 */
  _liveAppend(pcm) {
    const L = this._live;
    if (!pcm || !pcm.length) return;
    const need = LIVE_CHUNK_SAMPLES * 2;
    if (!L.buf) L.buf = pcm;
    else {
      const merged = new Uint8Array(L.buf.length + pcm.length);
      merged.set(L.buf, 0);
      merged.set(pcm, L.buf.length);
      L.buf = merged;
    }
    while (L.buf.length >= need) {
      const chunk = L.buf.slice(0, need);
      L.buf = L.buf.slice(need);
      this._liveEnqueue(chunk);
    }
  },

  /**
   * 线性插值重采样：Float32（源采样率）→ s16le 16 kHz。
   *
   * ⚠️ 与 ``app/core/audio/capture.py::StreamResampler`` **逐样本等价**
   *   —— 这是「跨端一致」（清单 B3）的地基：板载路径用 Python 的
   *   StreamResampler，浏览器路径用这里；同一段录音从两端进来必须得到
   *   **逐字节相同**的 16 kHz PCM，否则识别文本可能在边缘处分叉。
   *
   * 等价的四个要点（改了任何一条都会分叉）：
   *
   *   1. **整数域插值** —— 先把 float 还原成 s16 数值再插值
   *      （numpy 那边输入本来就是 s16 整数）；
   *   2. **全局输出索引 + carry** —— 流式相位连续
   *      （逐块独立插值会让每块相位从 0 重来 → 累积漂移）；
   *   3. **末尾外插推迟到 flush** —— 最后一个采样等收尾时才产出；
   *   4. **量化向零截断** —— numpy ``astype("<i2")`` 的语义
   *      （**不是**四舍五入）。
   *
   * 回归脚本 ``scripts/verify_asr_wizard.py`` 的 **V6a** 会把这里的输出
   * 和 Python 的输出**逐字节**比对 —— 改了这个函数必须重跑那条用例。
   */
  _resampleTo16k(samples, srcRate, state) {
    const DST = 16000;
    // 同采样率 → 直通（但仍要量化成 s16）
    if (srcRate === DST) return this._f32ToS16(samples);
    if (!state) {
      state = {
        carry: new Int32Array(0),
        outIndex: 0,
        totalIn: 0,
        ratio: srcRate / DST,
      };
    }
    const n = samples.length;
    if (!n) return new Uint8Array(0);

    // 1) float → 整数域
    const ints = new Int32Array(n);
    for (let i = 0; i < n; i++) {
      let v = Math.trunc(Math.fround(samples[i] * 32768));
      if (v > 32767) v = 32767;
      else if (v < -32768) v = -32768;
      ints[i] = v;
    }

    // 2) carry + 新数据
    let x = ints;
    if (state.carry.length) {
      x = new Int32Array(state.carry.length + n);
      x.set(state.carry, 0);
      x.set(ints, state.carry.length);
    }
    state.totalIn += n;

    // 3) 这次能产出多少个输出采样
    //    「右邻居是真实样本」才产出 —— 末尾那一格推迟到 flush。
    const offline = _pyRoundToEven((state.totalIn * DST) / srcRate);
    let nTarget = 0;
    if (state.totalIn >= 2) {
      const jLimit = _pyFloorDiv(state.totalIn - 2, state.ratio) + 1;
      nTarget = Math.min(jLimit, offline);
    }
    const nNew = nTarget - state.outIndex;
    if (nNew <= 0) {
      state.carry = x;
      return new Uint8Array(0);
    }

    // 4) 渲染（numpy 那边是 float32 → 这里用 Math.fround 对齐）
    const baseGlobal = state.totalIn - x.length;
    const out = new Float64Array(nNew);
    for (let i = 0; i < nNew; i++) {
      const j = state.outIndex + i;
      const idx = (j * srcRate) / DST;
      const leftG = Math.floor(idx);
      const left = leftG - baseGlobal;
      const right = Math.min(left + 1, x.length - 1);
      const frac = idx - leftG;
      out[i] = Math.fround(x[left] * (1 - frac) + x[right] * frac);
    }
    state.outIndex += nNew;
    const consumedGlobal = Math.floor((state.outIndex - 1) * state.ratio) + 1;
    state.carry = x.slice(consumedGlobal - baseGlobal);
    return _quantizeS16(out);
  },

  /**
   * 重采样收尾：补上整段路径会外插出来的最后几个采样。
   *
   * 不 flush 的话浏览器喂的音频会比板载**少最后 1~2 个采样** ——
   * 文本可能因此在末尾分叉（清单 B3）。
   */
  _resampleFlush(state, srcRate) {
    const DST = 16000;
    if (!state || !state.totalIn || srcRate === DST) {
      if (state) state.carry = new Int32Array(0);
      return new Uint8Array(0);
    }
    const offline = _pyRoundToEven((state.totalIn * DST) / srcRate);
    const nNew = offline - state.outIndex;
    if (nNew <= 0 || !state.carry.length) {
      state.carry = new Int32Array(0);
      return new Uint8Array(0);
    }
    const baseGlobal = state.totalIn - state.carry.length;
    const carry = state.carry;
    const out = new Float64Array(nNew);
    for (let i = 0; i < nNew; i++) {
      const j = state.outIndex + i;
      const idx = (j * srcRate) / DST;
      const leftG = Math.floor(idx);
      const left = leftG - baseGlobal;
      const right = Math.min(left + 1, carry.length - 1);
      const frac = idx - leftG;
      out[i] = Math.fround(carry[left] * (1 - frac) + carry[right] * frac);
    }
    state.outIndex += nNew;
    state.carry = new Int32Array(0);
    return _quantizeS16(out);
  },

  _liveResample(samples) {
    const L = this._live;
    const sr = L.ctx ? L.ctx.sampleRate : 16000;
    if (!L.resampler) {
      L.resampler = {
        carry: new Int32Array(0),
        outIndex: 0,
        totalIn: 0,
        ratio: sr / 16000,
      };
    }
    return this._resampleTo16k(samples, sr, L.resampler);
  },

  _f32ToS16(samples) {
    // ⚠️ 用 **32768** 而不是 32767：WebAudio 把 s16 采样除以 32768
    // 变成 float，这里再乘回去才能**逐位还原**原始的 s16 采样。
    // 用 32767 的话每个采样都会被压低约 3e-5 —— 幅度上看不出来，
    // 但 mel 频谱的末位比特会变，token 可能翻转（SDK 那边就是这么
    // 敏感的）。
    //
    // ⚠️ 量化用**向零截断**（Math.trunc）而不是四舍五入 —— numpy 那边
    // ``astype("<i2")`` 就是截断，两边必须一样，否则重采样结果差 1 LSB。
    const out = new Uint8Array(samples.length * 2);
    const view = new DataView(out.buffer);
    for (let i = 0; i < samples.length; i++) {
      let v = Math.trunc(Math.fround(samples[i] * 32768));
      if (v > 32767) v = 32767;
      else if (v < -32768) v = -32768;
      view.setInt16(i * 2, v, true);
    }
    return out;
  },

  _liveLevel(samples) {
    let sum = 0;
    for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
    const rms = Math.sqrt(sum / Math.max(1, samples.length));
    const db = rms > 0 ? 20 * Math.log10(rms) : -120;
    const pct = Math.max(0, Math.min(100, ((db + 60) / 60) * 100));
    const fill = document.getElementById("asr-meter-fill");
    if (fill) fill.style.width = pct.toFixed(1) + "%";
  },

  /** L1 背压：入队；队列满就丢**最旧**的块。 */
  _liveEnqueue(bytes) {
    const L = this._live;
    const seq = L.seq++;
    L.queue.push({ seq, bytes });
    while (L.queue.length > LIVE_MAX_PENDING) {
      L.queue.shift();          // 丢最旧
      L.dropped++;
      this._liveGapHint();
    }
    this._livePump();
  },

  /** L2 传输层：串行 ``await`` 响应 → 发送速率被推理速率钳住。 */
  async _livePump() {
    const L = this._live;
    if (L.sending) return;
    L.sending = true;
    try {
      while (L.queue.length) {
        const item = L.queue.shift();
        try {
          const r = await fetch(API_PREFIX + "/qwen3-asr/live/chunk", {
            method: "POST",
            headers: {
              "Content-Type": "application/octet-stream",
              "X-Asr-Stream-Id": L.streamId,
              "X-Asr-Seq": String(item.seq),
            },
            body: item.bytes,
          });
          const data = await r.json().catch(() => ({}));
          if (!r.ok) {
            this._liveStatus("上行失败：" + (data.detail || `HTTP ${r.status}`));
            if (r.status === 404 || r.status === 409) {
              await this._liveAbort();
              return;
            }
            continue;
          }
          if ((data.dropped_chunks || 0) > L.serverDropped) {
            L.serverDropped = data.dropped_chunks;
            this._liveGapHint();
          }
          this._liveStatus(
            `识别中 · 丢块 ${L.dropped} · 延迟 ${data.lag_ms || 0} ms`);
        } catch (e) {
          this._liveStatus("上行失败：" + e);
        }
      }
    } finally {
      L.sending = false;
    }
  },

  /** 只停浏览器侧的设备（麦克风 + AudioContext）。 */
  async _liveStopCapture() {
    const L = this._live;
    // 收尾：把重采样器末尾外插的采样补上（板载路径也 flush）——
    // 否则浏览器喂的音频比板载**少最后 1~2 个采样**。
    if (L.resampler) {
      const sr = L.ctx ? L.ctx.sampleRate : 16000;
      const tail = this._resampleFlush(L.resampler, sr);
      if (tail && tail.length) this._liveAppend(tail);
    }
    try { if (L.node) L.node.disconnect(); } catch (e) { /* noop */ }
    try { if (L.source) L.source.disconnect(); } catch (e) { /* noop */ }
    try {
      if (L.stream) L.stream.getTracks().forEach((t) => t.stop());
    } catch (e) { /* noop */ }
    try { if (L.ctx) await L.ctx.close(); } catch (e) { /* noop */ }
    L.node = null;
    L.source = null;
    L.stream = null;
    L.ctx = null;
    L.buf = null;
    L.resampler = null;
    L.active = false;
    // ⚠️ **不清空上传队列** —— 收尾（_liveFinish）要把队列里剩下的块
    // 发完再 POST /live/finish，否则最后半秒音频会丢。硬中止
    // （_liveAbort）才清队列。
  },

  /** 等上传队列排空（L2 串行 await）。 */
  async _liveDrain() {
    const L = this._live;
    for (let i = 0; i < 200; i++) {
      if (!L.sending && !L.queue.length) return;
      await new Promise((r) => setTimeout(r, 50));
    }
  },

  /** 结束（收尾）：停设备 → 等队列排空 → POST /live/finish → 拿最终文本。 */
  async _liveFinish() {
    const L = this._live;
    const streamId = L.streamId;
    if (!streamId) return;
    await this._liveStopCapture();
    // ⚠️ **先把队列发完再收尾** —— 否则最后半秒的音频还在队列里，
    // 服务端先收尾 → 尾巴丢了（文本末尾可能少几个词）。
    await this._liveDrain();
    try {
      const r = await fetch(API_PREFIX + "/qwen3-asr/live/finish", {
        method: "POST",
        headers: { "X-Asr-Stream-Id": streamId },
      });
      const data = await r.json().catch(() => ({}));
      if (r.ok) {
        const text = (data.text || "").trim();
        if (text) {
          // 服务端返回的是**累积**转写（端点切出的各句 + 当前句）。
          // 直接**替换** —— 各句已经通过 endpoint 帧拼过一次了，
          // 再追加就会重复。
          L.segments = [text];
          L.committed = "";
          L.unfixed = "";
          this._liveRender();
        }
        this._liveStatus(
          `已结束 · ${(data.elapsed_sec || 0).toFixed(1)} s · 丢块 ${L.dropped}`);
      } else {
        this._liveStatus("结束失败：" + (data.detail || `HTTP ${r.status}`));
      }
    } catch (e) {
      this._liveStatus("结束失败：" + e);
    }
    L.streamId = "";
    this._syncLiveUi();
  },

  /** 立即中止：停设备 + DELETE /live/stop（不产最终文本）。 */
  async _liveAbort(status) {
    const L = this._live;
    const streamId = L.streamId;
    await this._liveStopCapture();
    L.queue.length = 0;         // 硬中止 → 队列里的块直接丢
    if (streamId) {
      try {
        await fetch(API_PREFIX + "/qwen3-asr/live/stop", {
          method: "DELETE",
          headers: { "X-Asr-Stream-Id": streamId },
        });
      } catch (e) { /* best-effort */ }
    }
    L.streamId = "";
    // ⚠️ **不要覆盖调用方给的文案** —— 失败路径会先写「无法开始：xxx」，
    // 要是这里无条件写「已停止」，用户就只看到「已停止」，完全不知道
    // 出了什么事（实测：非安全上下文时点按钮，状态行只显示「已停止」）。
    this._liveStatus(status || "已停止");
    this._syncLiveUi();
  },

  /* ------------------------------------------------------------------ *
   * 刷新页面后恢复
   *
   * 为什么必须有：轮次只存在**服务端的内存**里（``service.py::_rounds``），
   * 浏览器一刷新 ``this.rounds`` 就空了 → 视频来源的「字幕同款转写框」
   * 被清空 → 用户看到「刚才识别出来的内容全没了」。
   *
   * 归属由**服务端**说了算（``GET /rounds`` 一起返回 ``source``）：
   * 前端自己猜会把「音频文件」的轮次画到视频画面上（U15 那个串台坑）。
   * ------------------------------------------------------------------ */
  async _restoreFromServer() {
    // 拉取期间用户又发起了识别 → 丢弃这次恢复（否则旧轮次会盖掉新轮次）。
    const gen0 = this._roundGen;
    let data = null;
    try {
      const r = await fetch(API_PREFIX + "/qwen3-asr/rounds");
      if (!r.ok) return;
      data = await r.json();
    } catch (e) { return; }
    if (!data || this._roundGen !== gen0) return;

    const server = Array.isArray(data.rounds) ? data.rounds : [];
    const src = data.source || {};
    // 服务端什么都没有 → 不动界面（首次进入页面就是这个分支）。
    if (!server.length && !src.kind) return;

    // 服务端那份是**同一批轮次的前缀**（服务端先把轮次记进 ``_rounds``
    // 再广播给 WS → 浏览器收到的每一条服务端都已经有了）；本地多出来
    // 的是「拉取窗口内」新到的 → 合并，别覆盖也别重复。
    const merged = this._mergeRounds(server, this.rounds);

    const kind = (src.kind === "video") ? "video" : "file";
    const path = (typeof src.path === "string") ? src.path.trim() : "";
    if (path) {
      // 设备端路径 → 播放器能重新加载。浏览器上传的 File 对象**刷新后
      // 拿不回来** → path 为空，轮次 / 文本照常恢复，只是放不了片子。
      this._path = path;
      this._file = null;
      const el = document.getElementById("asr-path");
      if (el) el.value = path;
    }

    // 先切页签（会清字幕 / 作废时间轴），再摆轮次 —— 顺序反了会被
    // ``_switchSource`` 里的 ``_renderTranscript`` 覆盖掉。
    if (this._source !== kind) this._switchSource(kind);

    // 浏览器上传的文件**恢复不了**（File 对象刷新后就没了）→ 轮次和
    // 文本恢复了，但来源框是空的。把原因说清楚，别让用户对着一个空
    // 来源框发愣（来源框空了 → 「开始识别」也是灰的）。
    if (!path && src.name) {
      const hint = document.getElementById("asr-source-hint");
      if (hint) {
        hint.textContent = "已恢复上次识别的结果（" + src.name
          + "）—— 浏览器上传的文件刷新后无法恢复，要重跑请重新选择文件。";
      }
    }

    this.rounds = merged;
    // 归属：**只认服务端说的那份**（file / video）。前端猜不得。
    this._roundsSource = kind;
    this._subInvalidate();
    this._roundsDirty = true;      // 轮次表整体换过 → 重建面板
    this._renderRounds();
    this._renderTranscript();

    // 单行「实时结果」（音频文件来源用；视频来源下它是隐藏的镜像 ——
    // 回归脚本要从 ``#asr-live-committed`` 读最终文本）。
    const res = this.snapshot && this.snapshot.last_result;
    const last = merged.length ? merged[merged.length - 1] : null;
    if (res && res.text) this._renderLive(res.text, "");
    else if (last) this._renderLive(last.committed || "", last.unfixed || "");

    if (this.snapshot) this._applySnapshot(this.snapshot);
  },

  /**
   * 合并「服务端恢复的轮次」与「本地已收到的轮次」。
   *
   * 去重靠 ``chunk_start_sec``（**绝对音频时间**，跨 append 也单调）：
   * ⚠️ 不能靠 ``round`` 序号 —— 拖动重开（append）会开新会话，序号
   * 从 0 重来，拿序号去重会把新的一段认成旧的。
   */
  _mergeRounds(serverRounds, localRounds) {
    const out = Array.isArray(serverRounds) ? serverRounds.slice() : [];
    const seen = new Set();
    let maxStart = -Infinity;
    for (const r of out) {
      const s = Number(r && r.chunk_start_sec);
      if (Number.isFinite(s)) {
        seen.add(s);
        if (s > maxStart) maxStart = s;
      }
    }
    for (const r of (localRounds || [])) {
      const s = Number(r && r.chunk_start_sec);
      if (!Number.isFinite(s)) { out.push(r); continue; }
      if (seen.has(s)) continue;    // 服务端已经有了 → 丢弃本地这份
      if (s < maxStart) continue;   // 服务端更新 → 本地这份是旧的
      out.push(r);
      seen.add(s);
      maxStart = s;
    }
    return out;
  },

  // ------------------------------------------------------------------ REST
  async start() {
    // 只有「文件 / 视频」来源走这条路径（实时两种来源各有各的按钮）。
    // 向导走完会直接调 start() —— 那时若当前停在实时来源上，就**切回
    // 文件来源**（向导填的是设备端路径）。
    if (!this._isFileSource()) {
      if (!this._hasSource()) return false;
      this._switchSource("file");
    }
    if (!this._hasSource() || this._starting || this._busy()) return false;
    this._starting = true;
    this.rounds = [];
    this._roundGen++;            // 新的一批轮次 → 作废在途的「刷新恢复」
    this._stopInitiated = false;  // 新的一批 → 「谁停的」重新算
    this._ignoreRounds = false;  // 新的一批 → 轮次帧照收
    // 记下这批轮次来自**哪个来源** —— 视频字幕靠它判断「该不该渲染」
    // （否则会拿上一来源的轮次画到当前视频上，见 _onSourceChanged）。
    this._roundsSource = this._source;
    // 轮次换了 → 字幕时间轴重建
    this._subInvalidate();
    this._renderRounds();
    this._renderLive("", "");
    this._renderTranscript();
    const empty = document.getElementById("asr-empty");
    if (empty) empty.classList.remove("hidden");
    this._applySnapshot({ ...(this.snapshot || {}), busy: true, state: "running" });

    try {
      const fd = new FormData();
      if (this._file) {
        fd.append("file", this._file, this._file.name);
      } else {
        fd.append("path", this._path);
      }
      // 不带 online_* 参数 → SDK 用 ONLINE_*_DEFAULT（= C demo 常量）

      const r = await fetch(API_PREFIX + "/qwen3-asr/transcribe", {
        method: "POST", body: fd,
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) {
        const detail = data.detail || `识别请求失败（HTTP ${r.status}）`;
        this._showError(detail);
        if (data.snapshot) this._applySnapshot(data.snapshot);
        return false;
      }
      if (data.snapshot) this._applySnapshot(data.snapshot);
      // 视频来源 → 跟着播起来。
      //
      // 点「开始识别」本身就是**用户手势** → 浏览器允许带声音自动播
      // （没有手势的话会被 autoplay 策略拦掉）。
      this._playVideoIfAny();
      return true;
    } catch (e) {
      this._showError("识别请求失败: " + e);
      return false;
    } finally {
      this._starting = false;
      const startBtn = document.getElementById("asr-start-btn");
      if (startBtn) {
        startBtn.disabled = !this._isFileSource() || !this._hasSource()
          || this._busy();
      }
    }
  },

  async stop() {
    try {
      const r = await fetch(API_PREFIX + "/qwen3-asr/stop", { method: "POST" });
      const data = await r.json().catch(() => ({}));
      if (data.snapshot) this._applySnapshot(data.snapshot);
      // ⚠️ **画面也要停住** —— 字幕是**播放头**的渲染（YouTube 式：字幕
      // 跟着画面走，**跟识别进度无关**）。只停后端识别的话，视频还在播
      // → 字幕的 rAF 循环继续跑 → **字幕继续按播放头滚动** → 用户以为
      // 「识别还在跑」（这正是用户报的 bug）。
      //
      // 「停止」的语义 = 停下来（识别 + 同步播放）。想继续看视频可以自己
      // 按播放键 —— 那时字幕会跟着**已识别**的那部分继续走（正常）。
      this._stopInitiated = true;
      this._freezePlayback();
    } catch (e) {
      this._showError("停止失败: " + e);
    }
  },

  /**
   * 冻结画面：暂停视频 + 停掉字幕 rAF 循环。
   *
   * 字幕的驱动源是**播放头**（``_renderVideoSubtitle`` 按
   * ``video.currentTime`` 取当前行）→ 播放头不动，字幕自然停住。
   *
   * ``video.pause()`` 会派发 'pause' 事件 → ``_stopSubLoop``；这里再
   * 显式调一次是**兜底**（元素不在 / 事件没派发时也保证循环停了）。
   */
  _freezePlayback() {
    const video = document.getElementById("asr-video");
    if (video && !video.paused) {
      try { video.pause(); } catch (e) { /* noop */ }
    }
    this._stopSubLoop();
  },

  async reset() {
    // 识别**进行中**点「清空」= 停止 + 清空（后端 /reset 会把正在跑的那
    // 一轮打断，见 service.py::reset）⇒ **画面也要停**：不然视频还在
    // 播、字幕还在滚，而识别已经被清空了 —— 那就是「点停止后字幕还在
    // 滚」那个 bug 的翻版。
    if (this.snapshot && this.snapshot.busy) this._freezePlayback();

    // ⚠️ **本地先清，不等服务器** —— 点了就该立刻看到「清空了」。
    // ``POST /reset`` 要等在飞的那次 RPC 被打断才返回（实测 ~1 s），
    // 等它才清 → 用户会觉得「点了没反应」。
    this.rounds = [];
    this._roundGen++;          // 清空 → 作废在途的「刷新恢复」
    this._roundsSource = "";
    // 清空之后**忽略迟到的轮次帧**：服务端已经在清空时断了那一轮，
    // 但清空前广播出去的几帧还会到 —— 收到就又长回来。
    this._ignoreRounds = true;
    this._renderRounds();
    this._renderLive("", "");
    this._audioOriginSec = 0;
    this._subInvalidate();
    const sub = document.getElementById("asr-video-sub");
    if (sub) this._subClear(sub);
    this._transcriptClear();
    const empty = document.getElementById("asr-empty");
    if (empty) empty.classList.remove("hidden");

    try {
      const r = await fetch(API_PREFIX + "/qwen3-asr/reset", { method: "POST" });
      const data = await r.json().catch(() => ({}));
      if (data.snapshot) this._applySnapshot(data.snapshot);
    } catch (e) {
      this._showError("清空失败: " + e);
    }
  },

  _showError(detail) {
    const hint = document.getElementById("asr-hint");
    if (hint) hint.textContent = detail;
    const pre = document.getElementById("data-qwen3-asr");
    if (pre) pre.textContent = JSON.stringify({ error: detail }, null, 2);
  },

  // ------------------------------------------------------------------ 渲染
  _renderLive(committed, unfixed) {
    const c = document.getElementById("asr-live-committed");
    const u = document.getElementById("asr-live-unfixed");
    if (c) c.textContent = committed || "";
    if (u) u.textContent = unfixed || "";
  },

  /**
   * 渲染「识别过程」面板（逐轮明细）。
   *
   * ⚠️ **不能每来一条 round 帧就重建整张表**。
   *
   * 面板是**逐轮追加**的（每轮一行），而一轮 = 一秒音频。98 分钟的片子
   * 就是 **5869 轮** → 每帧重建 5869 行 × 4 个元素 ≈ 2.3 万个 DOM 节点。
   * 实测（板端 headless Chromium，5869 轮）：**每帧中位 59 ms / 最大
   * 74 ms** —— 浏览器主线程被占死，**识别本身都被拖慢了**（同样这片子，
   * 开着页面识别 1077 s，不开页面约 480 s）。
   *
   * 所以两条规矩：
   *   1. **面板看不见的时候不重建**（数据还在 ``this.rounds`` 里，切回
   *      「识别过程」模式再补上 —— 见 ``_applyMode``）；
   *   2. 面板可见时**只追加新增的那几轮**，不重画老行。
   */
  _renderRounds() {
    const box = document.getElementById("asr-rounds");
    if (!box) return;
    // ① 面板隐藏 → 跳过（并记下「DOM 跟不上了」）
    if (box.classList.contains("hidden")) {
      this._roundsDirty = true;
      return;
    }
    const n = this.rounds.length;
    // ② 轮次被换过（重新识别 / 清空）→ 整表重建
    if (this._roundsDirty || this._roundsShown > n) {
      box.innerHTML = "";
      this._roundsShown = 0;
      this._roundsPrev = "";
      this._roundsDirty = false;
    }
    if (this._roundsShown === n) {
      box.scrollTop = box.scrollHeight;
      return;
    }
    // ③ 只追加新增的轮次
    let prev = this._roundsPrev || "";
    for (let i = this._roundsShown; i < n; i++) {
      const r = this.rounds[i];
      const row = document.createElement("div");
      row.className = "asr-round";
      const no = document.createElement("span");
      no.className = "asr-round-no";
      no.textContent = `#${r.round}`;
      const add = document.createElement("span");
      add.className = "asr-round-add";
      // committed 是累计的，这里只显示本轮的增量。
      add.textContent = r.committed && r.committed.startsWith(prev)
        ? r.committed.slice(prev.length)
        : (r.committed || "");
      const tail = document.createElement("span");
      tail.className = "asr-round-tail";
      tail.textContent = r.unfixed || "";
      row.appendChild(no);
      row.appendChild(add);
      row.appendChild(tail);
      box.appendChild(row);
      prev = r.committed || prev;
    }
    this._roundsShown = n;
    this._roundsPrev = prev;
    box.scrollTop = box.scrollHeight;
  },

  // ------------------------------------------------------------------ WS
  ensureWs() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}${API_PREFIX}/qwen3-asr/ws`;
    try {
      this.ws = new WebSocket(url);
    } catch (_) {
      this._scheduleReconnect();
      return;
    }
    this.ws.onopen = () => {
      this.wsRetry = 0;
      const startBtn = document.getElementById("asr-start-btn");
      if (startBtn) startBtn.disabled = !this._hasSource() || this._busy();
    };
    this.ws.onmessage = (ev) => {
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch (_) {
        return;
      }
      this._onFrame(msg);
    };
    this.ws.onclose = () => {
      this.ws = null;
      this._scheduleReconnect();
    };
    this.ws.onerror = () => { /* onclose 会接手重连 */ };
  },

  _scheduleReconnect() {
    if (this.wsTimer) return;
    const delay = Math.min(1000 * Math.pow(2, this.wsRetry++), 15000);
    this.wsTimer = setTimeout(() => {
      this.wsTimer = null;
      this.ensureWs();
    }, delay);
  },

  _onFrame(msg) {
    const pre = document.getElementById("data-qwen3-asr");
    if (pre) pre.textContent = JSON.stringify(msg, null, 2);

    switch (msg.type) {
      case "snapshot":
      case "state":
        this._applySnapshot(msg.state || msg.snapshot);
        break;
      case "round": {
        // 实时模式的 round 帧带 stream_id → 走实时显示，不进文件模式的列表。
        if (msg.stream_id) {
          const L = this._live;
          L.committed = msg.committed || "";
          L.unfixed = msg.unfixed || "";
          if (msg.language) L.language = msg.language;
          this._liveRender();
          break;
        }
        // ⚠️ 「清空」之后迟到的轮次帧 → 忽略（否则清完又长回来）。
        if (this._ignoreRounds) break;
        this.rounds.push({
          round: msg.round,
          committed: msg.committed || "",
          unfixed: msg.unfixed || "",
          language: msg.language || "",
          final: !!msg.final,
          // ⚠️ 时间戳**必须留着** —— 视频来源要靠它把字幕对齐到画面
          // （``chunk_start_sec`` / ``chunk_end_sec`` 相对**音轨**起点）。
          chunk_start_sec: msg.chunk_start_sec,
          chunk_end_sec: msg.chunk_end_sec,
        });
        // 来源还没标过（**刷新页面时识别正在跑**的场景：轮次是 WS 推来的，
        // 本地没有走过 start()）→ 按当前来源认领。
        // ⚠️ 用「为空才补」而不是「每次覆盖」：识别中途切来源时，
        // 轮次仍属于**原来源**，覆盖会把字幕串到新画面上。
        if (!this._roundsSource) this._roundsSource = this._source;
        // 音轨在容器里的起点（容器里音轨可能晚于画面起点）
        if (msg.audio_origin_sec !== undefined
            && msg.audio_origin_sec !== null) {
          this._audioOriginSec = Number(msg.audio_origin_sec) || 0;
        }
        // 记下「这一秒已经识别过了」→ 拖动进度条时靠它判断要不要
        // 把识别**开过去**（见 _maybeFollowSeek）。
        if (msg.chunk_start_sec !== undefined && msg.chunk_start_sec !== null) {
          this._subNoteRange(msg.chunk_start_sec, msg.chunk_end_sec);
        }
        this._renderLive(msg.committed || "", msg.unfixed || "");
        this._renderRounds();
        this._renderVideoSubtitle();
        const empty = document.getElementById("asr-empty");
        if (empty) empty.classList.add("hidden");
        if (this.snapshot) {
          this.snapshot.round_count = this.rounds.length;
          this.snapshot.language = msg.language || this.snapshot.language;
          this._applySnapshot(this.snapshot);
        }
        break;
      }
      case "gap": {
        // 音频有丢失 → 识别结果可能有缺口。
        if (msg.stream_id) this._liveGapHint(msg);
        break;
      }
      case "endpoint": {
        // 一句话结束（端点检测命中）→ 拼到已完成的句子里。
        if (!msg.stream_id) break;
        const L = this._live;
        const text = (msg.text || "").trim();
        if (text) L.segments.push(text);
        L.committed = "";
        L.unfixed = "";
        this._liveRender();
        this._liveStatus(`又识别了一句（第 ${msg.index || 0} 句）`);
        break;
      }
      case "live_done": {
        if (!msg.stream_id) break;
        this._liveStatus(
          `实时会话结束（${msg.reason || "done"}）· 丢块 ${msg.dropped_chunks || 0}`);
        break;
      }
      case "done": {
        this._renderLive(msg.text || "", "");
        this._renderRounds();
        const hint = document.getElementById("asr-hint");
        if (hint) {
          // 音频时长 + 耗时都报出来 —— 用户能看出「多长的音频被识别了」
          // （不再有 60 秒截断，所以这个数字可能远大于 60）。
          const audio = msg.audio_sec ? ` · 音频 ${msg.audio_sec.toFixed(1)} s` : "";
          hint.textContent = `识别完成（${(msg.elapsed_sec || 0).toFixed(2)} s${audio}）`
            + `· 语种 ${msg.language || "未知"}`;
        }
        break;
      }
      case "stopped": {
        // 停止 → **正常收尾**，不是失败：把已识别的文本交回界面。
        // （后端在取消时不再广播 error 帧。）
        this._renderLive(msg.text || "", "");
        this._renderRounds();
        // ⚠️ **要不要把画面也停住，取决于「谁喊的停」**：
        //   * ``internal`` —— 前端为了「拖动进度条 → 重开识别」而停的
        //     ⇒ **画面继续**（用户在拖进度条看视频，冻了就看不了）。
        //   * ``user`` —— 用户点了「停止」⇒ 画面停（字幕别再滚）。
        //     本页面点的 → click 那一刻就冻过了 → 这里**别再冻**
        //     （用户可能已经自己按了播放）；别的标签页点的 → 本页面
        //     没冻过 → 要冻（否则那边停了这边字幕还在滚）。
        const internal = (msg.reason === "internal");
        if (!internal && !this._stopInitiated) this._freezePlayback();
        const hint = document.getElementById("asr-hint");
        if (hint && !internal) {
          const audio = msg.audio_sec
            ? ` · 已识别 ${msg.audio_sec.toFixed(1)} s` : "";
          hint.textContent = `已停止${audio} —— 已识别的内容保留在下方，`
            + "要重跑点「开始识别」。";
        }
        break;
      }
      case "error":
        if (msg.stream_id) {
          // 实时会话的错（熔断 / 能力不可用）→ 落在实时区块里，
          // 不要覆盖文件模式的状态行。
          this._liveStatus("出错：" + (msg.detail || "识别出错"));
          break;
        }
        this._showError(msg.detail || "识别出错");
        break;
      default:
        break;
    }
  },
};

/**
 * 卡片 JS 由 framework 注入；暴露给向导（applyWizardSnapshot）。
 *
 * 向导给的是**设备端路径**（向导「上传…」落到设备端、或「浏览…」选出来的），
 * 所以这里把 source 写进路径框并**直接发起识别** —— 向导走完就该有结果，
 * 而不是「向导完成了但还得自己去页面点开始」。
 */
window.ImApp_qwen3_asr = window.ImApp_qwen3_asr || {};
window.ImApp_qwen3_asr.applyWizardSnapshot = async function (opts) {
  Qwen3Asr.init();
  const o = opts || {};

  // 1) 来源 —— 设备端路径
  if (o.source) {
    const path = String(o.source).trim();
    if (path) {
      Qwen3Asr._path = path;
      Qwen3Asr._file = null;
      const el = document.getElementById("asr-path");
      if (el) el.value = path;
      const fileInput = document.getElementById("asr-file");
      if (fileInput) fileInput.value = "";
      const nameEl = document.getElementById("asr-file-name");
      if (nameEl) nameEl.textContent = "";
      Qwen3Asr._syncSourceUi();
    }
  }

  // 2) 显示模式
  if (o.mode) {
    const sel = document.getElementById("asr-mode");
    if (sel) sel.value = o.mode;
    Qwen3Asr._applyMode();
  }

  // 3) 流式参数 —— **不传**。
  //    分块长度 / token 上限 / 回退策略等 = C++ demo 的 ONLINE_* 常量，
  //    属于内部契约，不暴露给用户；不传即用 SDK 的 ONLINE_*_DEFAULT。

  if (!Qwen3Asr._hasSource()) {
    throw new Error("WizardSnapshotError:start_failed:没有音频来源，请在向导里填写或上传音频文件");
  }

  // 4) 等能力就绪 → 真启动。
  //    **不能 btn.click()**：启动按钮的 disabled 由快照的 busy 决定，
  //    disabled 按钮的 click() 会被浏览器**静默丢弃** —— 向导显示"完成配置"、
  //    识别却没跑起来（与视频卡同一个坑，见 core/wizard-start.js）。
  if (window.WizardStart) {
    await window.WizardStart.run({
      label: "开始识别",
      probe: () => Qwen3Asr._probeCap(),
      start: async () => {
        const ok = await Qwen3Asr.start();
        if (!ok) throw new Error("start_failed:识别请求没被接受");
      },
      // 能力就绪 = 有 model_id（模型在 registry 里且会话建好了）
      isReady: (s) => !!(s && s.model_id),
      // 已经在跑 = busy，或者已经有轮次到达（极短音频可能跑得比探测还快）
      isStarted: (s) => !!(s && (s.busy || (s.round_count || 0) > 0)),
    });
  } else {
    const ok = await Qwen3Asr.start();
    if (!ok) throw new Error("WizardSnapshotError:start_failed:识别请求没被接受");
  }
};

// 暴露给控制台 / 回归脚本（别的卡片也这么做，比如 VdFsModal）——
// 节奏字幕的时间轴在 ``Qwen3Asr._buildSubTimeline()`` 里，回归要直接读它。
window.Qwen3Asr = Qwen3Asr;

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => Qwen3Asr.init());
} else {
  Qwen3Asr.init();
}
})();
