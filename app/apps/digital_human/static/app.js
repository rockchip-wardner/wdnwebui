/* 数字人卡片前端。
 *
 * 流程：写台词 → 板端 TTS 合成语音（落盘 wav）→ POST /speak 起作业 →
 * 轮询作业状态 → 出帧后预加载 → 播放音频并按 30 fps 换帧。
 *
 * 语音来源只有一条路：**文本 → TTS**。不录音、不上传音频 ——
 * TTS 合成完把 wav 的板子绝对路径交给 /speak 的 path 参数，数字人后端
 * 对音频来路无感知（那条路和设备端已有的 wav 完全一样）。
 */
(function () {
  "use strict";

  const API = "/api/digital-human";
  const TTS_API = "/api/tts";
  const FPS = 30;

  const $ = (id) => document.getElementById(id);

  const el = {
    statusLine: $("dh-status-line"),
    frame: $("dh-frame"),
    placeholder: $("dh-placeholder"),
    stagePreview: $("dh-stage-preview"),
    stageWrap: $("dh-stage-wrap"),
    frameInfo: $("dh-frame-info"),
    audio: $("dh-audio"),
    text: $("dh-text"),
    textCount: $("dh-text-count"),
    ttsHint: $("dh-tts-hint"),
    voice: $("dh-voice"),
    voicePreview: $("dh-voice-preview"),
    voicePreviewAudio: $("dh-voice-preview-audio"),
    language: $("dh-language"),
    volume: $("dh-volume"),
    volumeValue: $("dh-volume-value"),
    stride: $("dh-stride"),
    speakBtn: $("dh-speak-btn"),
    cancelBtn: $("dh-cancel-btn"),
    progress: $("dh-progress"),
    progressBar: $("dh-progress-bar"),
    log: $("dh-log"),
    runtime: $("dh-runtime"),
    avatarPicker: $("dh-avatar-picker"),
    avatarCount: $("dh-avatar-count"),
    avatarHint: $("dh-avatar-hint"),
    galleryBtn: $("dh-gallery-btn"),
    gallery: $("dh-gallery"),
    galleryGrid: $("dh-gallery-grid"),
    galleryHint: $("dh-gallery-hint"),
    galleryClose: $("dh-gallery-close"),
    galleryRefresh: $("dh-gallery-refresh"),
  };

  if (!el.frame) return; // 卡片模板没挂上

  const AVATAR_STORE_KEY = "dh.avatar.id";
  const VOICE_STORE_KEY = "dh.voice.id";
  const LANGUAGE_STORE_KEY = "dh.language.id";

  const state = {
    job: null,
    pollTimer: null,
    statusTimer: null,
    ttsTimer: null,
    rafId: null,
    playing: false,
    frameCache: new Map(),
    liveFrame: -1,
    live: false,
    renderSamples: [],
    // 最近一次算出的出帧速率（帧/秒）—— 诊断「边渲边播为什么没开播」用。
    lastRate: 0,
    lastRateFps: 0,
    // 边渲边播时音频被暂停（等渲染追帧）的状态
    audioHeld: false,
    heldSince: 0,
    // 流式模式：音频已经放完，但最后一段帧可能还在渲（等它渲完再收摊）
    audioEnded: false,
    voices: [],
    languages: [],
    voiceId: "",
    canceled: false,
    busy: false,
    ttsReady: false,
    ttsLoading: false,
    ttsKnown: false,
    ttsMissing: false,
    lastStatus: null,
    avatars: [],
    avatarId: "",
    galleryItems: [],
    galleryLoaded: false,
    galleryTimer: null,
  };

  // ------------------------------------------------------------------ //
  // 小工具
  // ------------------------------------------------------------------ //
  function log(msg, isErr) {
    const line = document.createElement("div");
    line.textContent = msg;
    if (isErr) line.className = "dh-err";
    el.log.prepend(line);
    while (el.log.childElementCount > 40) el.log.lastChild.remove();
  }

  function setStatus(text) {
    if (el.statusLine) el.statusLine.textContent = text;
  }

  function fmtMs(v) {
    if (v === undefined || v === null) return "-";
    return v >= 1000 ? (v / 1000).toFixed(2) + " s" : Math.round(v) + " ms";
  }

  // ---------------------------------------------------------------------------
  // POST 请求 —— 用 XMLHttpRequest，不用 fetch。
  //
  // 为什么不用 fetch(url, body)：规范上 body 是 FormData 时 fetch 一定发 POST，
  // 但**实测有浏览器（板子上用的那个嵌入式内核）把 fetch 的第二个参数吃掉**，
  // 请求退化成 GET —— 服务端直接回 405 "Method Not Allowed"，页面只显示一句
  // 看不懂的英文。XHR 的 open("POST", …) 是显式声明方法，没有这种歧义。
  // ---------------------------------------------------------------------------
  function post(url, body, headers) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", url, true);
      if (headers) {
        Object.keys(headers).forEach((k) => {
          try { xhr.setRequestHeader(k, headers[k]); } catch (e) { /* ignore */ }
        });
      }
      xhr.onload = () => {
        let payload = null;
        try {
          payload = JSON.parse(xhr.responseText);
        } catch (e) { /* 非 JSON 响应体 */ }
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(payload);
          return;
        }
        const detail = (payload && payload.detail) || xhr.statusText ||
          ("HTTP " + xhr.status);
        const err = new Error(detail);
        err.status = xhr.status;
        reject(err);
      };
      xhr.onerror = () => reject(new Error("网络错误"));
      xhr.send(body === undefined ? null : body);
    });
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  // ------------------------------------------------------------------ //
  // TTS —— 语音来源只有这一条路：文本 → 板端合成 → wav
  //
  // 抄「语音生成」卡的那一套：音色 / 语种清单来自 /api/tts/voices（不在页面里
  // 硬编码），合成走 POST /api/tts/start。
  //
  // 合成完的 wav 落在板子上，把**绝对路径**交给数字人的 /speak —— 数字人
  // 后端对音频来路无感知（和设备端已有的 wav 完全同一条路）。
  //
  // play_to 固定选 "browser"：SDK 那边静默（no_play=True），不往板子喇叭
  // 出声，但 wav 照样落盘。数字人只要文件，不需要现场播。
  // ------------------------------------------------------------------ //

  // 显示用标签。音色的**有效性**由后端给，这里只管怎么写给人看。
  const VOICE_LABELS = {
    news_female: "知性女声",
    baby_girl: "萌娃奶音",
    lovely_girl: "甜美萝莉音",
    cool_lady: "高冷御姐音",
    male_announcer: "男播音员音",
    uncle_bass: "磁性大叔音",
    vivian: "Vivian · 中文女声",
    serena: "Serena · 中文女声",
    uncle_fu: "Uncle Fu · 中文男声",
    dylan: "Dylan · 北京话男声",
    eric: "Eric · 四川话男声",
    ryan: "Ryan · 英文男声",
    aiden: "Aiden · 英文男声",
    ono_anna: "Ono Anna · 日文女声",
    sohee: "Sohee · 韩文女声",
  };

  const LANGUAGE_LABELS = {
    auto: "auto（模型自动判断）",
    chinese: "中文",
    english: "英文",
    japanese: "日文",
    korean: "韩文",
    german: "德文",
    french: "法文",
    russian: "俄文",
    portuguese: "葡萄牙文",
    spanish: "西班牙文",
    italian: "意大利文",
  };

  // 嘴型模型前端的 fbank 桶是定长的（160000 采样 ≈ 10 秒 @16 kHz）——
  // 超过 10 秒的音频**后端会自动切段渲染**（见 service.py 的 ``SEGMENT_SEC``），
  // 所以这里不是硬上限，只是提醒用户「会分段」。
  const MAX_SPEECH_SEC = 10;
  const CJK_PER_SEC = 4.5;     // 中文播报 ~4.5 字/秒
  const LATIN_PER_SEC = 14;    // 英文播报 ~14 字符/秒

  function estimateSeconds(text) {
    let cjk = 0;
    let other = 0;
    for (const ch of String(text || "")) {
      if (/[一-鿿　-〿＀-￯]/.test(ch)) cjk++;
      else if (!/\s/.test(ch)) other++;
    }
    return cjk / CJK_PER_SEC + other / LATIN_PER_SEC;
  }

  function updateTextCount() {
    if (!el.textCount || !el.text) return;
    const text = el.text.value || "";
    const sec = estimateSeconds(text);
    const over = sec > MAX_SPEECH_SEC - 0.5;
    el.textCount.textContent = text.length
      ? (text.length + " 字 · 约 " + sec.toFixed(1) + " 秒" +
         (over ? "（超过 " + MAX_SPEECH_SEC + " 秒会自动分段渲染）" : ""))
      : "0 字";
    el.textCount.classList.remove("is-over");
    el.textCount.classList.toggle("is-long", over);
  }

  function currentVoice() {
    return state.voices.filter((v) => v.id === state.voiceId)[0] || null;
  }

  // TTS 模型是在**算力页**启用的 —— 没启用就合成不了（/api/tts/start 会 503）。
  // 把这件事摆在明面上，别让用户点完「开始说话」才吃一句看不懂的 503。
  async function refreshTtsState() {
    try {
      const r = await fetch(TTS_API + "/");
      if (r.status === 404) {
        state.ttsKnown = true;
        state.ttsReady = false;
        state.ttsLoading = false;
        state.ttsMissing = true;
        renderTtsHint();
        syncSpeakButton();
        return;
      }
      if (!r.ok) return;
      const snap = await r.json();
      state.ttsKnown = true;
      state.ttsMissing = false;
      state.ttsReady = snap.capability_ready === true;
      state.ttsLoading = (snap.capability_loading || []).length > 0;
      renderTtsHint();
      syncSpeakButton();
      // 运行状态表里也有一行「语音合成」—— 状态变了要一起翻，别让两处说法打架。
      if (state.lastStatus) renderRuntime(state.lastStatus);
    } catch (e) { /* 后端没起来，忽略 —— 按钮保持可用，点了再报错 */ }
  }

  function renderTtsHint() {
    if (!el.ttsHint) return;
    if (state.ttsReady) {
      el.ttsHint.textContent = "文本合成 · 板端 TTS";
      el.ttsHint.classList.remove("is-bad");
    } else if (state.ttsLoading) {
      el.ttsHint.textContent = "TTS 模型加载中…";
      el.ttsHint.classList.remove("is-bad");
    } else if (state.ttsMissing) {
      el.ttsHint.textContent = "语音合成不可用（没装语音生成应用）";
      el.ttsHint.classList.add("is-bad");
    } else {
      el.ttsHint.textContent = "TTS 未启用（算力页启用「[TTS]文字转语音v3」）";
      el.ttsHint.classList.add("is-bad");
    }
  }

  // 「开始说话」能不能点 = 没事在跑 **且** TTS 够用。
  function syncSpeakButton() {
    if (!el.speakBtn) return;
    // 播放中也禁用 —— 一次只让一个形象说话，播完再说下一句
    const blocked = state.busy || state.playing ||
      (state.ttsKnown && !state.ttsReady && !state.ttsLoading);
    el.speakBtn.disabled = blocked;
    el.speakBtn.title = state.playing
      ? "正在播放，播完再说下一句"
      : (blocked && !state.busy
        ? "语音合成模型没启用 —— 去「算力」页启用「[TTS]文字转语音v3」"
        : "");
  }

  function storeValue(key, value) {
    try {
      if (value) window.localStorage.setItem(key, value);
      else window.localStorage.removeItem(key);
    } catch (e) { /* 隐私模式 / 无 localStorage */ }
  }

  function storedValue(key) {
    try { return window.localStorage.getItem(key) || ""; }
    catch (e) { return ""; }
  }

  async function loadVoices() {
    try {
      const r = await fetch(TTS_API + "/voices");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = (await r.json()) || {};
      state.voices = data.voices || [];
      state.languages = data.languages || [];
      renderVoices();
      renderLanguages();
    } catch (e) {
      log("音色清单加载失败：" + e.message, true);
    }
  }

  function renderVoices() {
    if (!el.voice) return;
    // 清单重拉前先记住当前选择 —— 用户刚挑好的音色不能被无声地冲掉。
    const prev = state.voiceId || storedValue(VOICE_STORE_KEY);
    const groups = [
      ["默认音色", state.voices.filter((v) => v.kind === "ref")],
      ["内置音色（模型自带）", state.voices.filter((v) => v.kind === "builtin")],
      ["我的音色", state.voices.filter((v) => v.kind === "user")],
    ];
    el.voice.innerHTML = "";
    groups.forEach(([label, list]) => {
      if (!list.length) return;
      const g = document.createElement("optgroup");
      g.label = label;
      list.forEach((v) => {
        const o = document.createElement("option");
        o.value = v.id;
        o.textContent = VOICE_LABELS[v.id] || v.id;
        o.dataset.kind = v.kind || "";
        o.dataset.preview = v.preview || "";
        g.appendChild(o);
      });
      el.voice.appendChild(g);
    });
    const ids = state.voices.map((v) => v.id);
    const want = ids.indexOf(prev) >= 0 ? prev : (ids[0] || "");
    if (want) el.voice.value = want;
    state.voiceId = el.voice.value || "";
    storeValue(VOICE_STORE_KEY, state.voiceId);
    updateVoicePreviewButton();
  }

  function renderLanguages() {
    if (!el.language) return;
    const prev = el.language.value || storedValue(LANGUAGE_STORE_KEY);
    el.language.innerHTML = "";
    state.languages.forEach((l) => {
      const o = document.createElement("option");
      o.value = l.id;
      o.textContent = LANGUAGE_LABELS[l.id] || l.id;
      el.language.appendChild(o);
    });
    const ids = state.languages.map((l) => l.id);
    const want = ids.indexOf(prev) >= 0 ? prev : (ids[0] || "auto");
    if (want) el.language.value = want;
    storeValue(LANGUAGE_STORE_KEY, el.language.value || "");
  }

  function updateVoicePreviewButton() {
    if (!el.voicePreview) return;
    const v = currentVoice();
    const has = !!(v && v.preview);
    el.voicePreview.disabled = !has;
    el.voicePreview.title = has
      ? "播放这个音色的试听样例"
      : "这个音色没有试听样例";
  }

  function previewVoice() {
    const v = currentVoice();
    if (!v || !v.preview) {
      log("这个音色没有试听样例", true);
      return;
    }
    if (!el.voicePreviewAudio) return;
    el.voicePreviewAudio.src = v.preview;
    el.voicePreviewAudio.play().catch(
      (e) => log("试听播放失败：" + e.message, true));
  }

  // 合成一段语音 → 返回板子上的 wav 绝对路径。
  async function synthesize(text) {
    const payload = {
      text: text,
      language: (el.language && el.language.value) || "auto",
      volume: el.volume ? Number(el.volume.value) / 100 : 0.1,
      play_to: "browser",
    };
    const v = currentVoice();
    if (v) {
      // 内置音色走 speaker，参考音色 / 用户音色走 ref_speaker —— 两者同在
      // SDK 的 speaker_embeddings 表里。两个都发会被 ref_speaker 吃掉，
      // 选了内置音色也出来默认音色。
      if (v.kind === "builtin") payload.speaker = v.id;
      else payload.ref_speaker = v.id;
    }
    await post(TTS_API + "/start", JSON.stringify(payload),
      { "Content-Type": "application/json" });
    return waitForTts();
  }

  // TTS 是异步的：start 只是起了一个 worker 线程，wav 得等合成完才落盘。
  async function waitForTts(timeoutMs) {
    const deadline = Date.now() + (timeoutMs || 120000);
    while (Date.now() < deadline) {
      if (state.canceled) throw new Error("已取消");
      let snap = null;
      try {
        const r = await fetch(TTS_API);
        if (r.ok) snap = await r.json();
      } catch (e) { /* 网络抖一下，下一轮再试 */ }
      if (snap) {
        if (snap.state === "done") {
          const path = snap.output || "";
          if (!path) throw new Error("TTS 合成完成但没有产出音频");
          return path;
        }
        if (snap.state === "error") throw new Error(snap.error || "TTS 合成失败");
        if (snap.state === "stopped") throw new Error("TTS 合成被中断");
        if (snap.state === "unavailable") {
          throw new Error("TTS 不可用：" + (snap.init_error || "未知原因"));
        }
      }
      await sleep(400);
    }
    throw new Error("TTS 合成超时");
  }

  // ------------------------------------------------------------------ //
  // 形象
  //
  // 嘴型模型是**形象无关**的 —— 同一个 RKNN 模型驱动任意形象，差异全在
  // 素材（参考帧 / 背景视频 / 生成器权重）里。所以「换形象」= 换一套素材，
  // 不需要重新转模型、也不需要重新跑 NPU。
  // ------------------------------------------------------------------ //
  function avatarPreviewUrl(id) {
    return API + "/avatars/" + encodeURIComponent(id) + "/preview";
  }

  // 舞台占位图：优先用**等比首帧**（从 bg_video.mp4 抽的第一帧）——
  // 它跟形象的真实比例一致，跟渲染出来的第一帧也一模一样。
  // 抽不到（老形象 / 抽帧失败）就退回官方图库的 3:4 立绘。
  function avatarFramePreviewUrl(id) {
    return API + "/avatars/" + encodeURIComponent(id) + "/frame-preview";
  }

  // 舞台盒子的比例跟着**当前形象**走。
  //
  // 各形象的帧尺寸不一样（多数 1024×1408，sample 是 890×1920）——
  // 舞台要是固定 16:9，竖版形象就只占 40% 宽度，两边全是黑边。
  function applyStageRatio(width, height) {
    if (!el.stageWrap) return;
    const w = Number(width) || 0;
    const h = Number(height) || 0;
    if (w > 0 && h > 0) {
      el.stageWrap.style.setProperty("--dh-ar", String(w / h));
    }
  }

  function selectAvatar(id) {
    state.avatarId = id || "";
    try {
      if (state.avatarId) {
        window.localStorage.setItem(AVATAR_STORE_KEY, state.avatarId);
      } else {
        window.localStorage.removeItem(AVATAR_STORE_KEY);
      }
    } catch (e) { /* 隐私模式 / 无 localStorage */ }
    renderAvatarPicker();
    updateStagePreview();
    // 舞台比例跟着换
    const a = state.avatars.filter((v) => v.id === state.avatarId)[0];
    if (a) applyStageRatio(a.width, a.height);
  }

  function updateStagePreview() {
    if (!el.stagePreview) return;
    if (state.avatarId) {
      const id = state.avatarId;
      // 先试等比首帧，404 就退回官方立绘
      el.stagePreview.onerror = function () {
        el.stagePreview.onerror = null;
        // 退回官方立绘 —— 那张是 3:4，比例和舞台对不上，铺满更好看
        el.stagePreview.classList.add("is-fallback");
        el.stagePreview.src = avatarPreviewUrl(id);
      };
      el.stagePreview.classList.remove("is-fallback");
      el.stagePreview.src = avatarFramePreviewUrl(id);
      el.stagePreview.classList.add("is-visible");
    } else {
      el.stagePreview.classList.remove("is-visible");
      el.stagePreview.removeAttribute("src");
    }
  }

  function renderAvatarPicker() {
    if (!el.avatarPicker) return;
    el.avatarPicker.innerHTML = "";
    if (!state.avatars.length) {
      const empty = document.createElement("div");
      empty.className = "dh-avatar-empty";
      empty.textContent = "本地还没有形象，点「形象库」下载一个";
      el.avatarPicker.appendChild(empty);
      el.avatarCount.textContent = "0 个";
      return;
    }
    state.avatars.forEach((a) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "dh-avatar-item";
      if (a.id === state.avatarId) btn.classList.add("is-active");
      // decoder 只有 NPU 一条路 —— 没转出 net_decode.rknn 的形象用不了，
      // 直接置灰，不让用户点进去再报错。
      if (a.npu_ready === false) {
        btn.classList.add("is-unready");
        btn.disabled = true;
        btn.title = a.name + "：缺少 NPU decoder 模型（需先在 119 上转换）";
      } else {
        btn.title = a.name + (a.collection ? "（" + a.collection + "）" : "");
      }
      const img = document.createElement("img");
      img.className = "dh-avatar-thumb";
      img.loading = "lazy";
      img.alt = a.name;
      if (a.has_preview) {
        img.src = avatarPreviewUrl(a.id);
      } else {
        img.classList.add("dh-avatar-thumb-blank");
      }
      const label = document.createElement("span");
      label.className = "dh-avatar-label";
      label.textContent = a.name;
      btn.appendChild(img);
      btn.appendChild(label);
      btn.addEventListener("click", () => {
        selectAvatar(a.id);
        log("已选择形象：" + a.name);
      });
      el.avatarPicker.appendChild(btn);
    });
    const ready = state.avatars.filter((a) => a.npu_ready !== false).length;
    el.avatarCount.textContent = ready === state.avatars.length
      ? state.avatars.length + " 个"
      : ready + " / " + state.avatars.length + " 个可用";
  }

  async function loadAvatars() {
    try {
      const data = await (await fetch(API + "/avatars")).json();
      state.avatars = data.avatars || [];
      const stored = (function () {
        try { return window.localStorage.getItem(AVATAR_STORE_KEY) || ""; }
        catch (e) { return ""; }
      })();
      const known = state.avatars.some((a) => a.id === stored);
      if (!known) {
        selectAvatar(data.default || (state.avatars[0] && state.avatars[0].id) || "");
      } else if (state.avatarId !== stored) {
        selectAvatar(stored);
      } else {
        renderAvatarPicker();
        updateStagePreview();
        // 舞台比例也跟着当前形象走
        const cur = state.avatars.filter((a) => a.id === state.avatarId)[0];
        if (cur) applyStageRatio(cur.width, cur.height);
      }
    } catch (e) {
      /* 后端还没起来，忽略 */
    }
  }

  // ---- 官方形象库 --------------------------------------------------- //
  async function openGallery(force) {
    if (!el.gallery) return;
    el.gallery.classList.remove("hidden");
    el.galleryHint.textContent = "加载中…";
    if (force || !state.galleryLoaded) {
      try {
        const url = API + "/avatars/gallery" + (force ? "?refresh=true" : "");
        const data = await (await fetch(url)).json();
        state.galleryItems = data.items || [];
        state.galleryLoaded = true;
      } catch (e) {
        el.galleryHint.textContent = "形象库拉取失败：" + e.message;
        return;
      }
    }
    renderGallery();
  }

  function closeGallery() {
    if (el.gallery) el.gallery.classList.add("hidden");
  }

  function renderGallery() {
    if (!el.galleryGrid) return;
    el.galleryGrid.innerHTML = "";
    const items = state.galleryItems || [];
    if (!items.length) {
      el.galleryHint.textContent = "形象库是空的";
      return;
    }
    const localIds = {};
    state.avatars.forEach((a) => { localIds[a.id] = a.npu_ready !== false; });
    let count = 0;
    items.forEach((item) => {
      const wrap = document.createElement("div");
      wrap.className = "dh-gallery-item";
      if (item.id in localIds) wrap.classList.add("is-local");
      const img = document.createElement("img");
      img.className = "dh-gallery-thumb";
      img.loading = "lazy";
      img.alt = item.id;
      img.src = avatarPreviewUrl(item.id);
      const name = document.createElement("div");
      name.className = "dh-gallery-name";
      name.textContent = item.id;
      const meta = document.createElement("div");
      meta.className = "dh-gallery-meta";
      meta.textContent = item.collection + (item.zip_size
        ? " · " + (item.zip_size / 1048576).toFixed(1) + " MB" : "");
      wrap.appendChild(img);
      wrap.appendChild(name);
      wrap.appendChild(meta);

      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "vd-btn dh-gallery-btn";
      if (item.id in localIds) {
        if (localIds[item.id]) {
          btn.textContent = "已下载";
          btn.classList.add("is-local");
          btn.addEventListener("click", () => {
            selectAvatar(item.id);
            closeGallery();
            log("已切换到形象：" + item.id);
          });
        } else {
          // 素材下下来了，但还没转 NPU decoder —— 用不了，路径说清楚
          btn.textContent = "待转换";
          btn.classList.add("is-unready");
          btn.addEventListener("click", () => {
            log("形象 " + item.id + " 还没转 NPU decoder（net_decode.rknn）—— "
              + "decoder 只有 NPU 一条路，需要先在 119 上转换。", true);
          });
        }
      } else {
        btn.textContent = "下载";
        btn.addEventListener("click", () => pullAvatar(item, btn));
      }
      wrap.appendChild(btn);
      el.galleryGrid.appendChild(wrap);
      count++;
    });
    el.galleryHint.textContent = count + " 个官方形象 · 点击「下载」取素材包";
  }

  async function pullAvatar(item, btn) {
    btn.disabled = true;
    btn.textContent = "下载中…";
    const form = new FormData();
    form.append("id", item.id);
    form.append("collection", item.collection || "");
    try {
      await post(API + "/avatars/pull", form);
      log("已开始下载形象：" + item.id);
    } catch (e) {
      btn.disabled = false;
      btn.textContent = "下载";
      log("下载请求失败：" + e.message, true);
    }
    if (state.galleryTimer) clearInterval(state.galleryTimer);
    state.galleryTimer = setInterval(pollDownloads, 1500);
  }

  async function pollDownloads() {
    try {
      const data = await (await fetch(API + "/avatars")).json();
      state.avatars = data.avatars || [];
      const downloads = data.downloads || {};
      const running = Object.keys(downloads).filter(
        (k) => downloads[k].state === "running");
      const failed = Object.keys(downloads).filter(
        (k) => downloads[k].state === "error");
      renderAvatarPicker();
      if (failed.length) {
        failed.forEach((id) => {
          if (state.avatars.some((a) => a.id === id)) return;
          log("形象 " + id + " 下载失败：" + (downloads[id].error || ""), true);
        });
      }
      if (!running.length) {
        if (state.galleryTimer) {
          clearInterval(state.galleryTimer);
          state.galleryTimer = null;
        }
        if (state.galleryLoaded) renderGallery();
      }
    } catch (e) { /* 忽略 */ }
  }

  function renderRuntime(status) {
    const rows = [];
    // ⚠️ 算力实例 —— 数字人的模型生命周期归算力层管（2026-11-26 起）。
    // 没启用就把「去哪儿启用」直接写出来，别让用户猜。
    const comp = status.compute || {};
    if (comp.enabled) {
      rows.push(["算力实例", "运行中（pid " + (comp.pid || "?") + "）", "ok"]);
    } else {
      rows.push(["算力实例", comp.status || "未启用", "bad"]);
      if (comp.hint) rows.push(["  └ 提示", comp.hint, "bad"]);
    }
    if (comp.error) rows.push(["算力实例错误", comp.error, "bad"]);
    // 语音合成（板端 TTS）—— 数字人的输入就靠它
    if (state.ttsReady) {
      rows.push(["语音合成", "就绪", "ok"]);
    } else if (state.ttsLoading) {
      rows.push(["语音合成", "模型加载中", ""]);
    } else if (state.ttsMissing) {
      rows.push(["语音合成", "不可用（未装语音生成应用）", "bad"]);
    } else if (state.ttsKnown) {
      rows.push(["语音合成", "未启用（见算力页）", "bad"]);
    }
    const npu = status.npu || {};
    rows.push(["嘴型模型", npu.model_exists ? (npu.loaded ? "已加载" : "待加载") : "模型缺失",
      npu.model_exists && npu.loaded ? "ok" : (npu.model_exists ? "" : "bad")]);
    const dec = status.decoder || {};
    rows.push(["decoder 后端", (dec.backend || "npu").toUpperCase(),
      dec.backend === "npu" ? "ok" : "bad"]);
    if (dec.error) {
      rows.push(["decoder 错误", dec.error, "bad"]);
    }
    // encoder（Paraformer SANM）—— 也跑在内置 NPU 上
    const enc = status.encoder || {};
    if (enc.backend) {
      rows.push(["encoder 后端", enc.backend.toUpperCase(),
        enc.backend === "npu" ? "ok" : "bad"]);
      if (enc.ms) {
        rows.push(["encoder 耗时", enc.ms + " ms（NPU）", "ok"]);
      }
    }
    const ready = (dec.npu_models || 0);
    if (dec.avatar_count) {
      rows.push(["可渲染形象", ready + " / " + dec.avatar_count + " 个",
        ready === dec.avatar_count ? "ok" : "bad"]);
    }
    const current = state.avatars.filter((a) => a.id === state.avatarId)[0];
    rows.push(["当前形象", current ? current.name : (state.avatarId || "未选择"),
      current ? "ok" : "bad"]);
    rows.push(["形象素材", status.avatar_exists ? "就绪" : "缺失", status.avatar_exists ? "ok" : "bad"]);
    // 当前作业 —— 流式模式下最想看的是「合成到哪了 / 渲到哪了」
    const job = status.job;
    if (job && job.streaming) {
      const sec = job.audio_rate ? job.pcm_bytes / (job.audio_rate * 2) : 0;
      if (job.state === "running") {
        rows.push(["作业", "合成 " + sec.toFixed(1) + " 秒 · 已渲 " +
          job.frame_count + " 帧", ""]);
      } else if (job.state === "done") {
        rows.push(["作业", "完成 · " + job.frame_count + " 帧 · " +
          (job.duration_s || 0).toFixed(1) + " 秒", "ok"]);
      }
      rows.push(["模式", "边合成边播 · " + job.fps + " fps", "ok"]);
    }
    el.runtime.innerHTML = "";
    rows.forEach(([k, v, cls]) => {
      const dt = document.createElement("dt");
      dt.textContent = k;
      const dd = document.createElement("dd");
      dd.textContent = v;
      if (cls) dd.className = cls;
      el.runtime.appendChild(dt);
      el.runtime.appendChild(dd);
    });
  }

  async function refreshStatus() {
    try {
      const status = await (await fetch(API)).json();
      state.lastStatus = status;
      renderRuntime(status);
      // ⚠️ **播放中优先** —— 边渲染边播时作业状态也是 "running"，
      // 先判 running 会把「播放中」覆盖成「处理中（render）」，
      // 用户就看不出正在播了。
      if (state.playing) {
        setStatus(state.live ? "播放中（边合成边播）" : "播放中");
      } else if (status.job && status.job.state === "running") {
        if (status.job.streaming) {
          setStatus(status.job.step === "render" ? "边合成边渲染中" : "合成语音中");
        } else {
          setStatus("处理中（" + (status.job.step || "") + "）");
        }
      } else if (status.job && status.job.state === "done") {
        setStatus("就绪");
      } else {
        setStatus("空闲");
      }
    } catch (e) {
      setStatus("后端未响应");
    }
  }

  // ------------------------------------------------------------------ //
  // 作业
  // ------------------------------------------------------------------ //
  function startJobPolling(jobId) {
    stopJobPolling();
    state.pollTimer = setInterval(async () => {
      try {
        const data = await (await fetch(API + "/jobs/" + jobId)).json();
        const job = data.job;
        state.job = job;
        if (job.state === "running") {
          el.progress.classList.remove("hidden");
          if (job.streaming) {
            // 流式：进度 = 已合成的音频秒数 / 预估总时长
            const got = job.audio_rate ? job.pcm_bytes / (job.audio_rate * 2) : 0;
            const est = Math.max(1, estimateSeconds(el.text ? el.text.value : ""));
            el.progressBar.style.width =
              Math.min(100, (got / est * 100).toFixed(0)) + "%";
            if (!state.playing) {
              setStatus(job.step === "render" ? "边合成边渲染中" : "合成语音中");
            }
            // 帧一出来就能看；够开播门槛了就开播
            if (job.frame_count) {
              showLiveFrame(job);
              maybeStartStreamPlayback(job);
            }
          } else {
            const steps = ["features", "mouth", "smooth", "render"];
            const idx = steps.indexOf(job.step);
            el.progressBar.style.width = ((idx + 1) / steps.length * 100).toFixed(0) + "%";
            // ⚠️ 已经在播了就别把状态改回「处理中」—— 播才是用户看到的主状态
            if (!state.playing) {
              setStatus("处理中（" + (job.step || "") + "）");
            }
            // 渲染阶段：已经渲出来的帧直接显示到舞台上
            if (job.step === "render" && job.frame_count) {
              showLiveFrame(job);
              maybeStartLivePlayback(job);
            }
          }
        } else if (job.state === "done") {
          stopJobPolling();
          el.progress.classList.add("hidden");
          el.progressBar.style.width = "100%";
          state.busy = false;
          syncSpeakButton();
          el.cancelBtn.classList.add("hidden");
          log("渲染完成：" + job.frame_count + " 帧 / " +
              (job.duration_s || 0).toFixed(2) + " 秒音频" +
              (job.timings ? "（特征 " + fmtMs(job.timings.features_ms) +
                 " / NPU " + fmtMs(job.timings.mouth_ms) +
                 " / 渲染 " + fmtMs(job.timings.render_ms) + "）" : ""));
          // 渲染出来的帧就是形象的真实尺寸 —— 用它校正舞台比例
          // （清单里没带尺寸时的兜底）。
          if (job.width && job.height) applyStageRatio(job.width, job.height);
          if (job.streaming) {
            el.frameInfo.textContent = job.frame_count + " 帧 · " +
              (job.duration_s || 0).toFixed(2) + " 秒";
            // 流式：最后一段渲完了 —— 该收摊的收摊
            if (!state.playing) {
              maybeStartStreamPlayback(job);   // 短台词：这时才开播
            } else if (state.audioEnded) {
              stopPlayback();
            }
          } else if (state.playing) {
            // 已经在边渲染边播了 —— 播完就行，别重来一遍
            el.frameInfo.textContent = job.frame_count + " 帧 · " +
              (job.duration_s || 0).toFixed(2) + " 秒";
          } else {
            await prepareAndPlay(job);
          }
        } else if (job.state === "error" || job.state === "stopped") {
          stopJobPolling();
          el.progress.classList.add("hidden");
          state.busy = false;
          syncSpeakButton();
          el.cancelBtn.classList.add("hidden");
          // 用户自己按了取消 —— 别报成失败
          if (state.canceled) {
            log("已取消");
          } else {
            // 流式作业里，TTS 的失败是以作业错误的形式回来的 —— 翻译成人话。
            log(explainJobError(job), true);
          }
          stopPlayback();
        }
      } catch (e) {
        stopJobPolling();
        state.busy = false;
        syncSpeakButton();
      }
    }, 400);
  }

  function stopJobPolling() {
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  // 「开始说话」= **流式**：文本 → 板端 TTS → 边合成边渲染 → 边下边播。
  //
  // ⚠️ 以前是「先合成完（35 秒）再提交渲染（40 秒）」⇒ 40 多秒才开口。
  // 现在由**后端**驱动 TTS：PCM 一到就分段送渲染，浏览器同时在下载音频
  // ⇒ 台词再长也只等几秒就开口。
  async function speak() {
    const text = (el.text && el.text.value || "").trim();
    if (!text) {
      log("先写一句要说的台词", true);
      if (el.text) el.text.focus();
      return;
    }
    state.canceled = false;
    state.liveFrame = -1;
    state.live = false;
    state.audioEnded = false;
    state.renderSamples = [];
    state.busy = true;
    syncSpeakButton();
    el.cancelBtn.classList.remove("hidden");
    el.progress.classList.remove("hidden");
    el.progressBar.style.width = "5%";
    setStatus("合成语音中");

    const form = new FormData();
    form.append("text", text);
    if (state.avatarId) {
      form.append("avatar", state.avatarId);
    }
    if (el.stride) {
      form.append("stride", el.stride.value || "3");
    }
    if (el.language) {
      form.append("language", el.language.value || "auto");
    }
    if (el.volume) {
      form.append("volume", String(Number(el.volume.value) / 100));
    }
    const v = currentVoice();
    if (v) {
      // 内置音色走 speaker，参考音色 / 用户音色走 ref_speaker —— 两者同在
      // SDK 的 speaker_embeddings 表里。两个都发会被 ref_speaker 吃掉，
      // 选了内置音色也出来默认音色。
      if (v.kind === "builtin") form.append("speaker", v.id);
      else form.append("ref_speaker", v.id);
    }

    try {
      const data = await post(API + "/speak-text", form);
      state.job = data.job;
      log("作业已启动：" + data.job.id);
      // 音频流：立刻挂上 src —— 浏览器从这一刻开始边下边缓冲，
      // 等第一段帧渲好就能直接开播（不用重新下载）。
      el.audio.src = data.job.audio_url;
      el.audio.load();
      startJobPolling(data.job.id);
    } catch (e) {
      state.busy = false;
      syncSpeakButton();
      el.cancelBtn.classList.add("hidden");
      el.progress.classList.add("hidden");
      setStatus("空闲");
      log("提交失败：" + e.message, true);
    }
  }

  async function cancel() {
    state.canceled = true;
    // 正在播的话也停掉 —— 取消就是「都别说了」
    stopPlayback();
    // TTS 也得停 —— 合成是异步的，卡在第一步时数字人那边还没活可停。
    try {
      await post(TTS_API + "/stop", undefined);
    } catch (e) { /* 没在跑就算了 */ }
    try {
      await post(API + "/stop", undefined);
      log("已请求停止");
    } catch (e) {
      log("停止失败：" + e.message, true);
    }
  }

  // ------------------------------------------------------------------ //
  // 播放
  // ------------------------------------------------------------------ //
  function loadFrame(job, index) {
    let img = state.frameCache.get(index);
    if (!img) {
      img = new Image();
      img.src = job.frame_url + index;
      state.frameCache.set(index, img);
    }
    return img;
  }

  // 并发预取。串行拉 171 张图要 3.4 秒，并发 4 路只要 1.4 秒。
  const PRELOAD_CONCURRENCY = 4;

  async function preloadFrames(job, total, onProgress) {
    let loaded = 0;
    let next = 0;
    async function worker() {
      while (true) {
        const i = next++;
        if (i >= total) return;
        const img = loadFrame(job, i);
        if (!img.complete) {
          await new Promise((resolve) => {
            img.onload = resolve;
            img.onerror = resolve;
          });
        }
        loaded++;
        if (onProgress) onProgress(loaded, total);
      }
    }
    const workers = [];
    for (let k = 0; k < Math.min(PRELOAD_CONCURRENCY, total); k++) {
      workers.push(worker());
    }
    await Promise.all(workers);
  }

  async function prepareAndPlay(job) {
    if (!job.frame_count) {
      log("没有渲染出帧", true);
      return;
    }
    state.frameCache.clear();
    el.placeholder.classList.add("is-hidden");
    el.frame.classList.add("is-visible");
    el.frameInfo.textContent = job.frame_count + " 帧 · " + (job.duration_s || 0).toFixed(2) + " 秒";

    // 预加载：把帧拉进浏览器缓存，避免播放时闪。
    // ⚠️ **不挡住开播** —— 预加载在后台跑，播完自然就都齐了。
    const total = job.frame_count;
    el.progress.classList.remove("hidden");
    const preload = preloadFrames(job, total, (loaded, n) => {
      el.progressBar.style.width = ((loaded / n) * 100).toFixed(0) + "%";
    });
    play(job);
    await preload;
    el.progress.classList.add("hidden");
  }

  // 边渲染边播：只有当**渲染速度追得上播放速度**时才启动，否则音画会错位。
  // 判据：出帧速率 ≥ 目标帧率。
  //
  // ⚠️ 速率窗口必须**够长**：渲染一启动就能凑出 3 个采样（0.8 s），那么点
  // 估计只覆盖 0.8 s —— 一次 GC / 调度卡顿就把它打到门槛以下 ⇒ 明明追得上
  // 却不开播（实测时有时无）。现在要求窗口 ≥ 1.5 s。
  const LIVE_MIN_WINDOW_SEC = 1.5;   // 速率统计窗口至少多长
  const LIVE_START_BUFFER_SEC = 1.0;  // 开播前先攒够多少秒的帧
  const LIVE_RATE_SLACK = 1.0;        // 速率余量：跟得上就行，不必超出
  //: 暂停音频等帧的最长时间（毫秒）—— 超了就放弃，让画面停在最后一帧。
  const LIVE_HOLD_MAX_MS = 1500;

  function maybeStartLivePlayback(job) {
    if (state.playing || !job.expected_frames) return;
    const now = performance.now();
    state.renderSamples.push({ t: now, n: job.frame_count });
    if (state.renderSamples.length > 10) state.renderSamples.shift();
    if (state.renderSamples.length < 3) return;
    const first = state.renderSamples[0];
    const last = state.renderSamples[state.renderSamples.length - 1];
    const dt = (last.t - first.t) / 1000;
    if (dt < LIVE_MIN_WINDOW_SEC) return;     // 采样窗口太短，速率不可信
    const rate = (last.n - first.n) / dt;     // 帧/秒
    const fps = job.fps || FPS;
    state.lastRate = rate;
    state.lastRateFps = fps;
    if (rate < fps * LIVE_RATE_SLACK) return;  // 渲染跟不上，别开播
    // 缓冲：攒够 LIVE_START_BUFFER_SEC 秒的帧再开播（第一帧立刻有画面）
    const need = Math.min(Math.ceil(fps * LIVE_START_BUFFER_SEC),
                          job.expected_frames);
    if (job.frame_count - first.n < need) return;

    state.live = true;
    state.playing = true;
    el.placeholder.classList.add("is-hidden");
    el.frame.classList.add("is-visible");
    el.frameInfo.textContent = "边渲染边播 " + job.frame_count + "/" +
      job.expected_frames + " 帧";
    log("渲染速度 " + rate.toFixed(1) + " fps ≥ " + fps +
        " fps，开始边渲染边播");
    play(job, job.expected_frames, true);
  }

  // 渲染还没结束时的实时预览：把最新渲出来的那一帧显示到舞台上。
  // （完整播放仍然要等全部渲完 —— 那样音画才对得上。）
  function showLiveFrame(job) {
    const n = job.frame_count;
    if (!n) return;
    const idx = Math.min(n - 1, state.liveFrame + 1);
    if (idx <= state.liveFrame) return;
    state.liveFrame = idx;
    const img = new Image();
    img.onload = () => {
      if (!state.playing) {
        el.placeholder.classList.add("is-hidden");
        el.frame.classList.add("is-visible");
        el.frame.src = img.src;
      }
    };
    img.src = job.frame_url + idx;
    // 播放中就让「边渲染边播」的进度留着，别抢状态文字
    if (!state.playing) {
      el.frameInfo.textContent = "渲染中 " + n + " 帧…";
    }
  }

  // 流式作业的错误（尤其是 TTS 的）翻译成人话。
  function explainJobError(job) {
    const detail = String((job && job.error) || "");
    if (/正在运行|稍候/.test(detail)) {
      return "语音合成服务正忙（可能被「语音生成」页占着），稍后再试";
    }
    if (/未启用|功能/.test(detail)) {
      return "语音合成模型没启用 —— 去「算力」页启用「[TTS]文字转语音v3」";
    }
    if (/不可用/.test(detail)) {
      return "语音合成不可用：" + detail;
    }
    return "作业" + (job && job.state === "stopped" ? "已停止" : "失败") +
           "：" + detail;
  }

  // 流式模式什么时候开播？
  //
  // 帧是**成段**出来的：一段音频先花 ~0.75 s 算特征，然后才逐帧出图。
  // 渲出第一帧就开播的话，每到段边界画面就追不上声音（声音会顿一下）。
  //
  // ⇒ 等**第一段整段渲完**再播：缓冲攒到 ~1.6 s 余量，段边界就不会卡。
  // 后端把「第一段有多少帧」放在 playable_frames 里。
  function maybeStartStreamPlayback(job) {
    if (state.playing || !job.frame_count) return;
    const need = job.playable_frames || 1;
    if (job.frame_count < need && job.state !== "done") return;
    state.live = true;
    state.playing = true;
    el.placeholder.classList.add("is-hidden");
    el.frame.classList.add("is-visible");
    el.frameInfo.textContent = "边合成边播 " + job.frame_count + " 帧…";
    log("第一段渲好（" + job.frame_count + " 帧），开始边合成边播");
    play(job, 0, true, true);
  }

  function play(job, totalFrames, live, stream) {
    state.playing = true;
    state.live = !!live;
    state.audioHeld = false;
    // 渲染可能抽了帧（stride>1）→ 实际画面帧率不是 30
    const fps = job.fps || FPS;
    // 流式：总帧数事先不知道（音频还在合成）⇒ 用个大数当上限，
    // 「已经渲好多少帧」由 job.frame_count 说了算。
    const total = stream ? Number.MAX_SAFE_INTEGER
                         : (totalFrames || job.frame_count);
    if (!stream) {
      // 流式模式下 src 早就挂好了（浏览器一直在下载）—— 重挂会把缓冲清掉。
      el.audio.src = job.audio_url;
      el.audio.currentTime = 0;
    }
    const started = performance.now();
    // 正在等加载的那一帧（-1 = 没有）
    let waiting = -1;
    const resumeAudio = () => {
      if (el.audio.ended) return;          // 放完了就别再从头来
      el.audio.play().catch((e) => log("音频续播失败：" + e.message, true));
    };
    const tick = () => {
      if (!state.playing) return;
      // 用**音频时钟**推进画面 —— 音频是唯一的主时钟。
      // （用 performance.now() 的话，音频真正开播前时间就在走 ⇒ 画面超前。）
      const elapsed = el.audio.currentTime > 0
        ? el.audio.currentTime
        : (performance.now() - started) / 1000;
      let idx = Math.floor(elapsed * fps);
      // 边渲染边播：只能播到"已经渲出来"的那一帧
      const avail = state.live
        ? Math.min(total, (state.job && state.job.frame_count) || 0)
        : total;
      if (idx >= total) {
        // ⚠️ 画面停在最后一帧，**不切音频** —— 播完才停
        // （这里直接 stopPlayback 会把音频截断：实际帧数常常比
        //  expected_frames 多几帧，最后一帧要停留到音频结束。）
        idx = total - 1;
      }
      // 渲染跟不上 ⇒ 画面会落在音频后面。
      //
      // ⚠️ 判据只能是「**这一帧还没渲出来**」（idx ≥ avail）。不能写成
      // 「落后 N 帧」—— 音频快结束时 idx 逼近 avail-1 而 avail 停在
      // 最后几帧，那个条件会一直成立 ⇒ 音频被**永久暂停**，永远播不完。
      //
      // 另加两道保险：音频快到结尾时不暂停（否则等不到最后一帧）；
      // 暂停超过 1.5 秒就放弃（渲染真卡住时不让音频陪着一起卡）。
      //
      // ⚠️ 流式音频的 ``duration`` 是 null（长度未知）⇒ 永远不算「快到结尾」。
      const dur = el.audio.duration;
      const nearEnd = dur != null && dur > 0 &&
                      el.audio.currentTime >= dur - 0.15;
      if (state.live && idx >= avail && !nearEnd && !el.audio.ended) {
        if (!state.audioHeld) {
          el.audio.pause();
          state.audioHeld = true;
          state.heldSince = performance.now();
        } else if (performance.now() - state.heldSince > 1500) {
          resumeAudio();
          state.audioHeld = false;
        }
        state.rafId = requestAnimationFrame(tick);
        return;
      }
      if (state.audioHeld) {
        resumeAudio();
        state.audioHeld = false;
      }
      if (idx >= avail) idx = Math.max(0, avail - 1);
      const img = loadFrame(job, idx);
      if (img.complete && img.naturalWidth) {
        waiting = -1;
        el.frame.src = img.src;
      } else if (waiting !== idx) {
        // 这一帧还没拉下来 —— 挂个监听，到了就补上（别让画面卡住）
        waiting = idx;
        img.addEventListener("load", () => {
          if (state.playing && waiting === idx) {
            waiting = -1;
            el.frame.src = img.src;
          }
        }, { once: true });
      }
      state.rafId = requestAnimationFrame(tick);
    };
    el.audio.play().catch((e) => log("音频播放失败：" + e.message, true));
    state.rafId = requestAnimationFrame(tick);
    setStatus(state.live ? "播放中（边合成边播）" : "播放中");
    syncSpeakButton();
  }

  function stopPlayback() {
    state.playing = false;
    state.live = false;
    state.audioHeld = false;
    state.audioEnded = false;
    if (state.rafId) {
      cancelAnimationFrame(state.rafId);
      state.rafId = null;
    }
    if (el.audio) {
      el.audio.pause();
      // 流式音频（长度未知）seek 会抛错 —— 干脆别 seek
      try {
        if (el.audio.duration != null) el.audio.currentTime = 0;
      } catch (e) { /* 忽略 */ }
    }
    setStatus("就绪");
    syncSpeakButton();
  }

  // ------------------------------------------------------------------ //
  // 事件绑定
  // ------------------------------------------------------------------ //
  el.speakBtn.addEventListener("click", speak);
  el.cancelBtn.addEventListener("click", cancel);
  // 播放没有手动按钮：点「开始说话」就自动播，播完才停。
  el.audio.addEventListener("ended", onAudioEnded);

  function onAudioEnded() {
    // 流式：音频放完了，但最后一段帧可能还在渲 —— 等它渲完再收摊
    // （不然最后一句的嘴型就停在半路上）。
    if (state.live && state.job && state.job.state === "running") {
      state.audioEnded = true;
      return;
    }
    stopPlayback();
  }

  if (el.text) {
    el.text.addEventListener("input", updateTextCount);
    // Ctrl+Enter = 合成并开讲
    el.text.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
        e.preventDefault();
        speak();
      }
    });
  }
  if (el.voice) {
    el.voice.addEventListener("change", () => {
      state.voiceId = el.voice.value || "";
      storeValue(VOICE_STORE_KEY, state.voiceId);
      updateVoicePreviewButton();
    });
  }
  if (el.voicePreview) {
    el.voicePreview.addEventListener("click", previewVoice);
  }
  if (el.language) {
    el.language.addEventListener("change", () => {
      storeValue(LANGUAGE_STORE_KEY, el.language.value || "");
    });
  }
  if (el.volume && el.volumeValue) {
    el.volume.addEventListener("input", () => {
      el.volumeValue.textContent = el.volume.value + "%";
    });
  }

  if (el.galleryBtn) {
    el.galleryBtn.addEventListener("click", () => openGallery(false));
  }
  if (el.galleryClose) {
    el.galleryClose.addEventListener("click", closeGallery);
  }
  if (el.galleryRefresh) {
    el.galleryRefresh.addEventListener("click", () => openGallery(true));
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    refreshStatus();
    loadAvatars();
    loadVoices();
    refreshTtsState();
  });

  refreshStatus();
  loadAvatars();
  loadVoices();
  refreshTtsState();
  updateTextCount();
  state.statusTimer = setInterval(refreshStatus, 3000);
  // TTS 能力就绪状态单独轮询（5 s）—— 用户可能在算力页随时启用/停用
  state.ttsTimer = setInterval(refreshTtsState, 5000);

  // 暴露给调试 / 自动化脚本
  window.ImApp_digital_human = {
    speak: speak,
    stop: stopPlayback,
    refreshStatus: refreshStatus,
    synthesize: synthesize,
    loadVoices: loadVoices,
    refreshTtsState: refreshTtsState,
    getState: () => ({
      job: state.job,
      playing: state.playing,
      live: state.live,
      avatar: state.avatarId,
      voice: state.voiceId,
      language: el.language ? el.language.value : "",
      text: el.text ? el.text.value : "",
      ttsReady: state.ttsReady,
      ttsLoading: state.ttsLoading,
      busy: state.busy,
      frames: state.job ? state.job.frame_count : 0,
      lastRate: state.lastRate,
      lastRateFps: state.lastRateFps,
      rateSamples: state.renderSamples.length,
    }),
  };
})();
