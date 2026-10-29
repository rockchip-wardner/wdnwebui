(function () {
  "use strict";
  const API_PREFIX = "/api";
  // core helpers (fmtNum, fmtEventTime) via app/static/core/*.js
  const fmtNum = window.fmtNum;
  const fmtEventTime = window.fmtEventTime;

// 显示用标签。音色的**有效性**由后端 /api/tts/voices 给,这里只管
// 怎么写给人看 —— 没有标签就退回显示原始 id。
const VOICE_LABELS = {
  girl_base: "girl_base（参考音色）",
  ahu: "ahu（参考音色）",
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
const DIALECT_LABELS = {
  sichuan_dialect: "四川话",
  beijing_dialect: "北京话",
};

const TTS = {
  initialized: false,
  ws: null,
  backoff: 1.0,
  lastSnap: null,
  _hasBuiltin: null,   // 清单里有没有内置音色(null = 还没拉清单)

  init() {
    if (this.initialized) return;
    this.initialized = true;

    const textEl = document.getElementById("tts-text");
    const voiceEl = document.getElementById("tts-voice");
    const langEl = document.getElementById("tts-language");
    const volEl = document.getElementById("tts-volume");
    const volValEl = document.getElementById("tts-volume-value");
    const startBtn = document.getElementById("tts-start");
    const stopBtn = document.getElementById("tts-stop");
    const audioEl = document.getElementById("tts-audio");

    if (voiceEl) voiceEl.addEventListener("change", () => this._updateVoiceHint());
    if (langEl) langEl.addEventListener("change", () => this._updateVoiceHint());
    this.loadCatalog();

    if (volEl && volValEl) {
      volEl.addEventListener("input", () => {
        volValEl.textContent = volEl.value + "%";
        if (audioEl) audioEl.volume = Number(volEl.value) / 100;
      });
    }
    if (audioEl) {
      audioEl.volume = 1.0;
      audioEl.addEventListener("error", () =>
        this.setStatus("浏览器播放失败 (autoplay 可能被拦截)", true));
    }
    document.querySelectorAll('input[name="tts-play-to"]').forEach((el) => {
      el.addEventListener("change", () => this._showAudioRowIfNeeded());
    });

    // start() 失败会抛错（向导要看见），但按钮这条路只需要状态行 ——
    // 起一个空的 catch，别在控制台刷 unhandled rejection。
    if (startBtn)   startBtn.addEventListener("click", () => { this.start().catch(() => {}); });
    if (stopBtn)    stopBtn.addEventListener("click", () => this.stop());
    if (textEl) {
      textEl.addEventListener("keydown", (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
          e.preventDefault();
          this.start().catch(() => {});
        }
      });
    }
    // 能力变化（去算力页启用/停用模型）→ 页面可见时每 5s 复查；
    // 生成中不打扰。
    if (!this._capTimer) {
      this._capTimer = setInterval(() => {
        if (document.visibilityState !== "visible") return;
        if (this.lastSnap && this.lastSnap.busy) return;
        this.refresh().catch(() => {});
      }, 5000);
    }
  },

  // ------------------------------------------------------------------ //
  // 音色 / 语种清单
  // ------------------------------------------------------------------ //
  async loadCatalog() {
    try {
      const r = await fetch(API_PREFIX + "/tts/voices");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json() || {};
      const voices = data.voices || [];
      this._hasBuiltin = voices.some((v) => v.kind === "builtin");
      this._fillVoices(voices);
      this._fillLanguages(data.languages || []);
      this._updateVoiceHint();
    } catch (err) {
      // 清单拿不到 → 下拉留空,后端回落到默认参考音色,功能不瘫。
      const hintEl = document.getElementById("tts-voice-hint");
      if (hintEl) hintEl.textContent = "音色清单加载失败: " + err.message;
    }
  },

  _fillVoices(voices) {
    const el = document.getElementById("tts-voice");
    if (!el) return;
    el.innerHTML = "";
    const ref = [], builtin = [];
    voices.forEach((v) => (v.kind === "ref" ? ref : builtin).push(v));
    const addGroup = (list, label) => {
      if (!list.length) return;
      const g = document.createElement("optgroup");
      g.label = label;
      list.forEach((v) => {
        const o = document.createElement("option");
        o.value = v.id;
        o.textContent = VOICE_LABELS[v.id] || v.id;
        o.dataset.kind = v.kind || "";
        o.dataset.dialect = v.dialect || "";
        g.appendChild(o);
      });
      el.appendChild(g);
    };
    addGroup(ref, "参考音色（声音克隆）");
    addGroup(builtin, "内置音色（模型自带）");
  },

  _fillLanguages(languages) {
    const el = document.getElementById("tts-language");
    if (!el) return;
    el.innerHTML = "";
    languages.forEach((l) => {
      const o = document.createElement("option");
      o.value = l.id;
      o.textContent = LANGUAGE_LABELS[l.id] || l.id;
      el.appendChild(o);
    });
  },

  _selectedVoice() {
    const el = document.getElementById("tts-voice");
    const opt = el && el.selectedOptions ? el.selectedOptions[0] : null;
    if (!opt || !opt.value) return { id: "", kind: "", dialect: "", label: "" };
    return {
      id: opt.value,
      kind: opt.dataset.kind || "",
      dialect: opt.dataset.dialect || "",
      label: opt.textContent || opt.value,
    };
  },

  // SDK 里「方言音色 + 语种 auto/chinese」会把语言 id 锁成该方言 ——
  // 不说清楚,用户选了 auto 却听到四川话,看着像 bug。
  _updateVoiceHint() {
    const hintEl = document.getElementById("tts-voice-hint");
    if (!hintEl) return;
    // 本模型没有内置音色 → 说清楚原因,免得用户以为那一组是加载失败。
    if (this._hasBuiltin === false) {
      hintEl.textContent = "当前模型不含内置音色（部署的是声音克隆版权重），只有参考音色可选。";
      return;
    }
    const voice = this._selectedVoice();
    const langEl = document.getElementById("tts-language");
    const lang = langEl ? langEl.value : "auto";
    if (voice.dialect && (lang === "auto" || lang === "chinese")) {
      const d = DIALECT_LABELS[voice.dialect] || voice.dialect;
      hintEl.textContent = `「${voice.label}」自带${d}：语种 ${lang} 会锁定为${d}，改选其它语种可解除。`;
    } else if (lang === "auto") {
      hintEl.textContent = "语种 auto = 由模型自动判断文本语种。";
    } else {
      hintEl.textContent = "";
    }
  },

  async refresh() {
    try {
      const r = await fetch(API_PREFIX + "/tts");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const snap = await r.json();
      this.lastSnap = snap;
      this.render(snap);
      this.refreshHistory();
    } catch (err) {
      this.setStatus("加载失败: " + err.message, true);
    }
  },

  async refreshHistory() {
    try {
      const r = await fetch(API_PREFIX + "/tts/history");
      if (!r.ok) return;
      const data = await r.json();
      this.renderHistory(data.items || []);
    } catch (_) {
  // ...
    }
  },

  render(snap) {
    this.lastSnap = snap;
    if (typeof DemoGuard !== "undefined") {
      DemoGuard.report("tts", { session: snap });
    }
    const banner = document.getElementById("tts-unavailable");
    const reasonEl = document.getElementById("tts-unavailable-reason");
    const startBtn = document.getElementById("tts-start");
    const stopBtn = document.getElementById("tts-stop");
    const metaEl = document.getElementById("tts-meta");
    const lineEl = document.getElementById("tts-status-line");
    const dataEl = document.getElementById("data-tts");

    if (dataEl) dataEl.textContent = JSON.stringify(snap, null, 2);

    const isBusy = snap.busy === true || snap.running === true || snap.state === "loading" || snap.state === "running";
    const isRunning = snap.running === true;
    const capReady = snap.capability_ready !== false;
    // 「本机有这个能力的实例但还在加载」—— 与「压根没启用」是两回事，
    // 混成一句话会让用户以为自己没开模型，其实模型正在起来。
    const capLoading = Array.isArray(snap.capability_loading)
      && snap.capability_loading.length > 0;

    if (banner && reasonEl) {
      if (snap.available === false) {
        banner.classList.remove("hidden");
        reasonEl.textContent = snap.init_error || "upstream 模块缺失";
      } else if (!capReady) {
        banner.classList.remove("hidden");
        reasonEl.textContent = capLoading
          ? "模型正在加载中，请稍候（加载完成后即可开始生成）"
          : "模型未启用：请到「算力配置」页启用 [TTS] 文字转语音模型";
      } else {
        banner.classList.add("hidden");
      }
    }

    if (startBtn) {
      startBtn.disabled = isBusy || snap.available === false || !capReady;
      startBtn.title = capReady
        ? ""
        : (capLoading
            ? "模型正在加载中，请稍候"
            : "功能未启用：请先在算力配置启用 [TTS] 文字转语音模型");
    }
    if (stopBtn) {
      stopBtn.disabled = !isBusy;
    }

    if (lineEl) {
      lineEl.textContent = `状态: ${snap.state}` + (snap.error ? ` · ${snap.error}` : "");
    }

    if (metaEl) {
      metaEl.textContent = this.formatMeta(snap);
    }
  },

  formatMeta(snap) {
    if (!snap.available) return "语音生成不可用，请检查上游 Qwen3-TTS 模型路径 / rknn3 驱动";
    if (snap.capability_ready === false) {
      const loading = Array.isArray(snap.capability_loading)
        && snap.capability_loading.length > 0;
      return loading
        ? "模型正在加载中，请稍候…"
        : "模型未启用：请先在算力配置启用 [TTS] 模型";
    }
    if (snap.state === "idle")  return `模型: ${snap.model_dir}`;
    if (snap.state === "loading") return "正在加载模型…";
    if (snap.state === "running") {
      const f = snap.frames || 0;
      const sec = (snap.audio_sec || 0).toFixed(2);
      const wall = (snap.elapsed || 0).toFixed(2);
      return `推理中 · 已生成 ${f} 帧 / ${sec}s · 用时 ${wall}s`;
    }
    if (snap.state === "done") {
      return `已完成 · ${snap.frames} 帧 / ${(snap.audio_sec || 0).toFixed(2)}s · 用时 ${(snap.elapsed || 0).toFixed(2)}s`;
    }
    if (snap.state === "stopped") {
      return `已停止 · 输出: ${snap.output || "--"}`;
    }
    if (snap.state === "error") return `错误: ${snap.error}`;
    return `状态: ${snap.state}`;
  },

  /**
   * 启动失败 → 写状态行 **并抛错**。
   *
   * 为什么必须抛：向导的 finish 步靠捕获异常来显示「完成配置失败」，
   * 只写状态行的话向导会照样显示 ✓ —— 用户看到「配置完成」但什么都没
   * 发生（这正是 2026-10-23 实测到的回归：向导跑完没合成）。
   * 错误码用宿主向导认识的 `start_failed`（见 core/wizard/state.js）。
   */
  _startError(detail) {
    this.setStatus(detail, true);
    const e = new Error("WizardSnapshotError:start_failed:" + detail);
    e.retryable = false;
    return e;
  },

  async start() {
    const textEl = document.getElementById("tts-text");
    const langEl = document.getElementById("tts-language");
    const volEl = document.getElementById("tts-volume");
    const audioEl = this._audioEl();

    const text = (textEl ? textEl.value : "").trim();
    if (!text) {
      throw this._startError("请输入要合成的文本");
    }
    const playTo = this._selectedPlayTo();

    if (audioEl) {
      audioEl.pause();
      audioEl.removeAttribute("src");
      audioEl.load();
      audioEl.classList.add("hidden");
    }

  // ...
    this.audioScheduledUntil = 0;
    this.audioPushedSec = 0;
    this._showAudioRowIfNeeded();

    this.setStatus(
      playTo === "browser" ? "提交中…（将边生成边播浏览器）" :
      playTo === "both"    ? "提交中…（设备 + 浏览器同时边播）" :
                             "提交中…（仅设备播放）",
      false,
    );

  // ...
    if (this._wantsBrowser()) {
      const ctx = await this._ensureAudioCtx();
      if (ctx) {
        try {
          if (ctx.state !== "running") await ctx.resume();
        } catch (_) {}
      }
    }

    const payload = {
      text,
      language: langEl ? langEl.value : "auto",
      volume: volEl ? Number(volEl.value) / 100 : 0.1,
      play_to: playTo,
    };
    // 音色二选一:参考音色走 ref_speaker,内置音色走 speaker。两个都发
    // 会被 SDK 的 ref_speaker 优先级吃掉 —— 选了内置音色也是默认音色。
    const voice = this._selectedVoice();
    if (voice.kind === "ref") payload.ref_speaker = voice.id;
    else if (voice.kind === "builtin") payload.speaker = voice.id;
    // 网络异常单独 catch —— 卡片自己的错误（_startError）不能再被包一层，
    // 否则状态行里会出现 "提交失败: WizardSnapshotError:start_failed:…"。
    let r, body;
    try {
      r = await fetch(API_PREFIX + "/tts/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      body = await r.json().catch(() => ({}));
    } catch (err) {
      throw this._startError("提交失败: " + err.message);
    }
    if (!r.ok) {
      throw this._startError("提交失败: " + (body.detail || r.statusText));
    }
    this.lastSnap = body;
    this.render(body);
    this.ensureWs();
    // ...
    if (this._wantsBrowser()) {
      this._subscribeAudio();
    }
  },

  async stop() {
    try {
      const r = await fetch(API_PREFIX + "/tts/stop", { method: "POST" });
      const body = await r.json().catch(() => ({}));
      this.lastSnap = body;
      this.render(body);
    } catch (err) {
      this.setStatus("停止失败: " + err.message, true);
    }
  },

  setStatus(text, isError) {
    const lineEl = document.getElementById("tts-status-line");
    if (lineEl) {
      lineEl.textContent = text;
      lineEl.style.color = isError ? "#dc2626" : "";
    }
  },

  renderHistory(items) {
    const listEl = document.getElementById("tts-history-list");
    const countEl = document.getElementById("tts-history-count");
    if (!listEl) return;
    listEl.innerHTML = "";
    if (!items.length) {
      const li = document.createElement("li");
      li.className = "vd-event-empty";
      li.textContent = "尚无生成记录，启动合成后结果显示在这里。";
      listEl.appendChild(li);
    } else {
      items.slice().reverse().forEach((it) => {
        const li = document.createElement("li");
        li.className = "vd-event";

        const when = new Date((it.ts || 0) * 1000);
        const whenStr = when.toLocaleString();
        const url = it.output_url || "";
        const filename = (it.output || "").split("/").filter(Boolean).pop()
          || "tts.wav";

  // ...
        const meta = document.createElement("div");
        meta.className = "vd-event-meta";
        [
          whenStr,
          it.speaker || "--",
          (it.audio_sec || 0).toFixed(2) + "s",
          (it.frames || 0) + " 帧",
        ].forEach((txt) => {
          const sp = document.createElement("span");
          sp.className = "tag";
          sp.textContent = txt;
          meta.appendChild(sp);
        });
        const path = document.createElement("span");
        path.className = "vd-event-path";
        path.textContent = it.output || "";
        path.title = it.output || "";
        meta.appendChild(path);

        if (url) {
          const playBtn = document.createElement("button");
          playBtn.type = "button";
          playBtn.className = "vd-btn vd-btn-mini vd-event-action";
          playBtn.textContent = "▶ 试听";
          meta.appendChild(playBtn);

          const player = document.createElement("audio");
          player.className = "tts-history-player hidden";
          player.controls = true;
          player.preload = "none";
          player.src = url;
          meta.appendChild(player);

          const dlBtn = document.createElement("a");
          dlBtn.className = "vd-btn vd-btn-mini vd-event-action";
          dlBtn.href = url;
          dlBtn.download = filename;
          dlBtn.textContent = "⬇ 下载";
          dlBtn.title = "下载 " + filename;
          meta.appendChild(dlBtn);

          playBtn.addEventListener("click", async () => {
            const wasHidden = player.classList.contains("hidden");
            document.querySelectorAll(
              ".tts-history-player:not(.hidden)"
            ).forEach((other) => {
              if (other !== player) {
                try { other.pause(); } catch (_) {}
              }
            });
            const shared = this._audioEl();
            if (shared && !shared.classList.contains("hidden")) {
              try { shared.pause(); } catch (_) {}
            }
            if (wasHidden) {
              player.classList.remove("hidden");
              playBtn.textContent = "■ 停止";
              try {
                await player.play();
              } catch (e) {
                this.setStatus("试听失败: " + e.message, true);
              }
            } else {
              try { player.pause(); } catch (_) {}
              player.classList.add("hidden");
              playBtn.textContent = "▶ 试听";
            }
          });
          player.addEventListener("ended", () => {
            playBtn.textContent = "▶ 试听";
            player.classList.add("hidden");
          });
        }

        const text = document.createElement("div");
        text.className = "vd-event-text";
        text.textContent = it.text || "";

        li.appendChild(meta);
        li.appendChild(text);

        listEl.appendChild(li);
      });
    }
    if (countEl) countEl.textContent = items.length + " 条";
  },

  /* ------------------------------------------------------------------ *
   * WebSocket status stream — auto-reconnect with backoff.
   * ------------------------------------------------------------------ */
  ensureWs() {
    if (this.ws && this.ws.readyState <= WebSocket.OPEN) return;
    try {
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      const url = `${proto}//${location.host}${API_PREFIX}/tts/ws`;
      this.ws = new WebSocket(url);
    } catch (err) {
      this.setStatus("WS 创建失败: " + err.message, true);
      return;
    }
    this.ws.binaryType = "arraybuffer";
    this.ws.onmessage = (ev) => {
      this.audioFramesTotal = (this.audioFramesTotal || 0) + 1;
  // ...
      const data0 = ev.data;
      if (data0 instanceof Blob) {
        data0.text().then((t) => this._handleTextMessage(t, ev));
        return;
      }
      if (data0 instanceof ArrayBuffer) {
        this._handleBinaryChunk(data0);
        return;
      }
  // ...
      this._handleTextMessage(data0, ev);
    };
    this.ws.onopen = () => {
  // ...
      if (this._wantsBrowser()) {
        this._subscribeAudio();
      } else if (this.pendingAudioSubscribe) {
        this._subscribeAudio();
      }
      this._armPing();
    };
    this.ws.onclose = () => {
      this.ws = null;
      if (this.audioPingTimer) {
        clearInterval(this.audioPingTimer);
        this.audioPingTimer = null;
      }
  // ...
      if (this.lastSnap && (this.lastSnap.busy || this.lastSnap.running)) {
        const delay = Math.min(this.backoff, 8.0) * 1000;
        this.backoff = Math.min(this.backoff * 1.6, 8.0);
        setTimeout(() => this.ensureWs(), delay);
      }
    };
    this.ws.onerror = () => {
      try { this.ws && this.ws.close(); } catch (_) {}
    };
    this.backoff = 1.0;
  },

  /* ------------------------------------------------------------------ *
   * Browser-side audio playback (WebAudio, streaming chunks)
   * ------------------------------------------------------------------ */
  audioCtx: null,
  audioActive: false,        // current stream is feeding the context
  audioSampleRate: 24000,
  audioChannels: 1,
  audioScheduledUntil: 0,    // monotonic ctx time for back-to-back chunks
  audioPushedSec: 0,
  /* True when subscribe_audio was sent but the WS wasn't OPEN yet;
   * ``onopen`` re-issues subscribe when the WS finishes opening. */
  pendingAudioSubscribe: false,
  audioPingTimer: null,

  async _handleTextMessage(text) {
    let snap;
    try { snap = JSON.parse(text); } catch (_) { return; }
    if (!snap) return;
    if (snap.type === "audio_start") {
      this._onAudioStreamStart(snap.sample_rate, snap.channels);
      return;
    }
    if (snap.type === "audio_end") {
      this._onAudioStreamEnd();
      return;
    }
  // ...
    if (snap.type === "pong") return;
    this.lastSnap = snap;
    this.render(snap);
    if (snap.state === "done" || snap.state === "stopped" || snap.state === "error") {
      this.refreshHistory();
    }
  },

  async _ensureAudioCtx() {
    if (this.audioCtx) {
  // ...
      if (this.audioCtx.state !== "running") {
        try { await this.audioCtx.resume(); } catch (_) {}
      }
      return this.audioCtx;
    }
    const Ctor = window.AudioContext || window.webkitAudioContext;
    if (!Ctor) return null;
    this.audioCtx = new Ctor();
  // ...
    try { await this.audioCtx.resume(); } catch (_) {}
  // ...
    try {
      const osc = this.audioCtx.createOscillator();
      const gain = this.audioCtx.createGain();
      gain.gain.value = 0;
      osc.connect(gain).connect(this.audioCtx.destination);
      osc.start();
    } catch (_) {}
    return this.audioCtx;
  },

  _subscribeAudio() {
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      this.pendingAudioSubscribe = true;
      return false;
    }
    try {
      ws.send(JSON.stringify({ type: "subscribe_audio" }));
      this.pendingAudioSubscribe = false;
      if (!this.audioActive) {
        this._setAudioChip("订阅已发送", "idle");
      }
      return true;
    } catch (err) {
      this._setAudioChip("订阅失败: " + err.message, "error");
      return false;
    }
  },

  _unsubscribeAudio() {
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    try { ws.send(JSON.stringify({ type: "unsubscribe_audio" })); }
    catch (_) {}
  },

  /* WS-level keep-alive ping; some proxies and NATs reap idle WS
   * connections after ~30-60 s of silence. */
  _armPing() {
    if (this.audioPingTimer) clearInterval(this.audioPingTimer);
    this.audioPingTimer = setInterval(() => {
      const ws = this.ws;
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      try { ws.send(JSON.stringify({ type: "ping" })); } catch (_) {}
    }, 15000);
  },

  async _onAudioStreamStart(sampleRate, channels) {
    const ctx = await this._ensureAudioCtx();
    if (!ctx) return;
    this.audioSampleRate = sampleRate || 24000;
    this.audioChannels = channels || 1;
    this.audioActive = true;
  // ...
    this.audioScheduledUntil = ctx.currentTime;
    this.audioPushedSec = 0;
    this._setAudioChip("浏览器音频流开始", "live");
  },

  _onAudioStreamEnd() {
    if (!this.audioActive) {
      this._setAudioChip("服务端 audio_end · 未播放", "idle");
      return;
    }
    const sec = this.audioPushedSec;
    this.audioActive = false;
    this.audioScheduledUntil = 0;
    this.audioPushedSec = 0;
    this._setAudioChip(`浏览器音频流结束 · 已播 ${sec.toFixed(2)}s`, "idle");
  },

  _handleBinaryChunk(arrayBuf) {
    if (!this.audioCtx) {
      this._setAudioChip("WebAudio ctx 未创建,请刷新页面重试", "error");
      return;
    }
    if (!this.audioActive) return;

    const volEl = document.getElementById("tts-volume");
    const vol = volEl ? Number(volEl.value) / 100 : 1.0;

  // ...
    const view = new DataView(arrayBuf);
    if (arrayBuf.byteLength < 12) return;
    const sr = view.getUint32(0, true);
    const ch = view.getUint32(4, true);
    const n  = view.getUint32(8, true);
    const sampleCount = sr === this.audioSampleRate
      ? n
      : Math.floor(n * (this.audioSampleRate / (sr || this.audioSampleRate)));
    if (sampleCount <= 0) return;
    const expectedBytes = sampleCount * ch * 4;
    if (arrayBuf.byteLength < 12 + expectedBytes) return;

    const ctx = this.audioCtx;
    const f32 = new Float32Array(arrayBuf.slice(12, 12 + expectedBytes));
    for (let i = 0; i < f32.length; i++) f32[i] *= vol;
    const buffer = ctx.createBuffer(ch, sampleCount, this.audioSampleRate);
  // ...
    for (let c = 0; c < ch; c++) {
      const data = buffer.getChannelData(c);
      if (ch === 1) data.set(f32);
      else for (let i = 0; i < sampleCount; i++) data[i] = f32[i * ch + c];
    }

    const src = ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(ctx.destination);
  // ...
    const now = ctx.currentTime;
    const startAt = this.audioScheduledUntil > now
                    ? this.audioScheduledUntil : now;
    src.start(startAt);
    this.audioScheduledUntil = startAt + buffer.duration;
    this.audioPushedSec += buffer.duration;
    this._setAudioChip(`播放 ${this.audioPushedSec.toFixed(2)}s`, "live");
  },

  _setAudioChip(text, state) {
    const chip = document.getElementById("tts-audio-chip");
    if (!chip) return;
    chip.textContent = text;
    if (state) chip.dataset.state = state;
  },

  _showAudioRowIfNeeded() {
    const row = document.getElementById("tts-audio-row");
    if (!row) return;
    row.classList.toggle("hidden", !this._wantsBrowser());
  },

  _audioEl() {
    return document.getElementById("tts-audio");
  },

  _selectedPlayTo() {
    const sel = document.querySelector('input[name="tts-play-to"]:checked');
    return sel ? sel.value : "device";
  },

  _wantsBrowser() {
    const v = this._selectedPlayTo();
    return v === "browser" || v === "both";
  },
};

TTS.init();

/* Sidebar 底栏 popover 互相排他 —— 共享 system.js 的 PowerMenu / ConfigMenu /
 * CoprocessorMenu (挂在 ``window.SystemMenus``)。system.js defer 加载顺序
 * 早于本 IIFE,引用全局是安全的。system.js 未加载时静默 no-op(罕见的离线
 * 场景)。 */
function _wrapOpenToCloseOther(menu, other) {
  const orig = menu.open.bind(menu);
  menu.open = function () {
    if (other && typeof other.close === "function") other.close();
    orig();
  };
}
const _menus = window.SystemMenus
  ? [window.SystemMenus.PowerMenu,
     window.SystemMenus.ConfigMenu,
     window.SystemMenus.CoprocessorMenu]
  : [];
for (const a of _menus) {
  for (const b of _menus) {
    if (a !== b && a && b) _wrapOpenToCloseOther(a, b);
  }
}

// 已移除 root 警告横幅：服务不再要求以 root 启动，本系统也不用网页端
// 的关机/重启（那是 `#power-btn` 弹窗的事，与这里无关）。此前这段代码
// 挂在 TTS 卡的 app.js 里、页面一加载就拉 /api/power 并弹全局横幅，
// 导致不进 TTS 页也会看到，且提示的方向已经与实际部署方式相反。

  // ...

/* ---------------------------------------------------------------- *
 * 算力配置 (Compute Config) page                                   *
 * ---------------------------------------------------------------- */
/* ---------------------------------------------------------------- *
 * 算力配置 (Compute Config) page — v0.6 重构                          *
 * ---------------------------------------------------------------- *
 * v0.6 把布局从"NPU 列 × 模型行"改成"模型卡片 × 目标 checkbox"：
 *
 *  顶部一行：内存条（3588 系统内存 + 每张 RK182x 卡显存），复用
 *           /api/system-monitor 的 memory / npu.devices 字段。
 *  中间几张：按 preset（Qwen3-VL-4B / Qwen3-TTS / …）划分
 *           的 service 卡片 —— 每张卡片列出子模型清单（来自后端
 *           snapshot，字段由 workers/<id>.json 透出），下方用
 *           checkbox 选运行在哪张 NPU（可多选），点 启用 / 停用 走现有的
 *           /api/compute/instances 接口；倒计时复用 snapshot
 *           estimated_init_seconds_per_model。
 */
// 本地算力卡片的元数据与排序全部来自后端 snapshot
// （每个 service 条目自带 label / capability_types / memory_estimate_mb /
// description / available_targets，列表顺序走 snapshot.preset_order）。
// v0.9 之前这里是 CC_PRESET_META / CC_PRESET_ORDER 的第二份抄写。

  // ...
const CC_MAX_RK182X_CARDS = 2;

// 从错误响应里取出**人话**原因。FastAPI 的 HTTPException body 是
// {"detail": "..."} —— 直接摆 raw body 会让用户看到带引号的 JSON 串
// （例：{"detail":"npu_index=1 越界：…"}）。不是 JSON 的 body 原文就是原因。
function computeErrorDetail(status, raw) {
  const text = String(raw || "");
  if (!text) return "HTTP " + status;
  try {
    const j = JSON.parse(text);
    if (j && typeof j.detail === "string") return j.detail;
    if (j && typeof j.message === "string") return j.message;
  } catch (_) { /* 不是 JSON —— 原文就是原因 */ }
  return text;
}

const ComputeConfigPage = {
  initialized: false,
  _tickHandle: null,
  _snapshot: null,
  _system: null,        // /api/system-monitor 缓存
  _loading: false,
  _selectedTarget: {},  // {model_id: [npu_index, ...]} —— 「运行在」勾选的卡（可多张）

  init() {
    if (this.initialized) return;
    this.initialized = true;
    document.getElementById("cc-stop-all")?.addEventListener("click", () => this.stopAll());
  },

  async load() {
    if (this._loading) return;
    this._loading = true;
    try {
      const fetchCompute = () => fetch(API_PREFIX + "/compute", {
        cache: "no-store",
        headers: {"Cache-Control": "no-cache"},
      });
      const fetchSystem = () => fetch(API_PREFIX + "/system-monitor", {
        cache: "no-store",
        headers: {"Cache-Control": "no-cache"},
      });

  // ...
      const firstLoad = this._snapshot == null;
      const rComp = await fetchCompute();
      if (!rComp.ok) throw new Error("compute HTTP " + rComp.status);
      this._snapshot = await rComp.json();
      this._systemTickCount = (this._systemTickCount || 0) + 1;
      if (firstLoad || this._systemTickCount % 3 === 0) {
  // ...
        try {
          const rSys = await fetchSystem();
          if (rSys.ok) this._system = await rSys.json();
        } catch (_e) { /* 容错：不阻塞主路径 */ }
      }
      this.render();
    } catch (e) {
  // ...
      console.error("ComputeConfigPage.load failed:", e);
      if (typeof this.setFoot === "function") {
        this.setFoot("加载失败: " + e.message, "danger");
      }
    } finally {
      this._loading = false;
    }
  },

  startTicking() {
    if (this._tickHandle) return;
  // ...
    const self = this;
    this._tickHandle = setInterval(function () {
      if (!self._snapshot) return;
      self.load();
    }, 1000);
  },

  stopTicking() {
    if (this._tickHandle) {
      clearInterval(this._tickHandle);
      this._tickHandle = null;
    }
  },

  tick() {
    if (!this._snapshot) return;
    this.load();
  },

  async _postInstance(_npu_index, _model) {
  // ...
  },

  async _deleteInstance(_npu_index, _model) {
  // ...
  },

  async toggle(_model, _targetNpu) {
  // ...
  },

  async stopAll() {
    if (!confirm("确定要停止所有本地算力实例吗？\n"
                 + "（adopted peer 的实例不受影响；需要到对端机器操作）")) return;
    try {
      const r = await fetch(API_PREFIX + "/compute/stop-all", {method: "POST"});
      if (!r.ok) {
        const text = await r.text();
        alert("全部停止失败: " + text);
      }
    } catch (e) {
      alert("请求失败: " + e.message);
    }
    await this.load();
  },

  // ...
  render() {
    this._renderMemStrip();
    this._renderBanners();
    this._renderServices();
    this._renderSummary();
  },

  // ...
  _renderMemStrip() {
    const strip = document.getElementById("cc-mem-strip");
    if (!strip) return;
    strip.innerHTML = "";
    const sys = this._system || {};
    const cards = (this._snapshot && this._snapshot.cards) || [];

  // ...
    strip.appendChild(this._makeMemCell({
      title: "RK3588 系统内存",
      sub:   "主控 SoC · LPDDR5 共享",
      usedMb:  sys.memory ? sys.memory.used_mb : null,
      totalMb: sys.memory ? sys.memory.total_mb : null,
      pct:     sys.memory ? sys.memory.percent : null,
      extra:   null,
    }));

  // ...
    const devices = (sys.npu && sys.npu.devices) || [];
    const npuByIdx = new Map(devices.map(d => [d.index, d]));
    const cardByIdx = new Map(cards.map(c => [c.npu_index, c]));

    const MAX_RK182X_CARDS = CC_MAX_RK182X_CARDS;
  // ...
    let rk182xCards = this._sortNpuCards(this._modelCards(cards))
      .slice(0, MAX_RK182X_CARDS);

  // ...
    if (rk182xCards.length === 0 && devices.length > 0) {
      const seen = new Set();
      const fallback = [];
      for (const d of devices) {
        if (seen.has(d.index)) continue;
        seen.add(d.index);
        fallback.push({
          npu_index: d.index,
          device_id: d.id || null,
        });
        if (fallback.length >= MAX_RK182X_CARDS) break;
      }
      rk182xCards = fallback.sort((a, b) => {
        const idxA = (a.npu_index || 0) - (b.npu_index || 0);
        if (idxA !== 0) return idxA;
        const da = (a.device_id || "").toString();
        const db = (b.device_id || "").toString();
        return da.localeCompare(db);
      });
    }

    if (rk182xCards.length === 0) {
  // ...
      const ph = document.createElement("div");
      ph.className = "cc-mem-cell is-missing";
      const t = document.createElement("div");
      t.className = "cc-mem-cell-head";
      const ti = document.createElement("span");
      ti.className = "cc-mem-cell-title";
      ti.textContent = "RK182x #0";
      t.appendChild(ti);
      ph.appendChild(t);
      const m = document.createElement("div");
      m.className = "cc-mem-cell-meta";
      m.textContent = "未检测到协卡";
      ph.appendChild(m);
      strip.appendChild(ph);
    } else {
  // ...
      for (const card of rk182xCards) {
        const idx = card.npu_index;
        const dev = npuByIdx.get(idx);
        strip.appendChild(this._makeMemCell({
          title: `RK182x #${idx}`,
          sub:   card.device_id || `npu #${idx}`,
          usedMb:  dev ? dev.mem_used_mb : null,
          totalMb: dev ? dev.mem_total_mb : null,
          pct:     dev && dev.mem_used_mb != null && dev.mem_total_mb
                    ? (dev.mem_used_mb / dev.mem_total_mb) * 100 : null,
          extra:   dev ? {
            temp:    dev.temp_c,
            power:   dev.power_mw,
            npuPct:  dev.npu_percent,
            status:  dev.status,
          } : null,
        }));
      }
    }
  },

  _makeMemCell({title, sub, usedMb, totalMb, pct, extra}) {
    const cell = document.createElement("div");
    cell.className = "cc-mem-cell";

    const head = document.createElement("div");
    head.className = "cc-mem-cell-head";
    const titleEl = document.createElement("span");
    titleEl.className = "cc-mem-cell-title";
    titleEl.textContent = title;
    head.appendChild(titleEl);
    const pctEl = document.createElement("span");
    pctEl.className = "cc-mem-cell-pct";
    if (pct == null) {
      pctEl.textContent = "--";
    } else {
      pctEl.textContent = fmtNum(pct, 1) + " %";
      if (pct >= 90)      pctEl.classList.add("is-crit");
      else if (pct >= 70) pctEl.classList.add("is-warn");
    }
    head.appendChild(pctEl);
    cell.appendChild(head);

    const bar = document.createElement("div");
    bar.className = "cc-mem-bar";
    const fill = document.createElement("div");
    fill.className = "cc-mem-bar-fill";
    if (pct != null) {
      fill.style.width = Math.min(100, Math.max(0, pct)) + "%";
      if (pct >= 90)      fill.classList.add("is-crit");
      else if (pct >= 70) fill.classList.add("is-warn");
    }
    bar.appendChild(fill);
    cell.appendChild(bar);

    const meta = document.createElement("div");
    meta.className = "cc-mem-cell-meta";
    const usedText  = usedMb  != null ? fmtBytes(usedMb)  : "已用 --";
    const totalText = totalMb != null ? fmtBytes(totalMb) : "--";
    meta.textContent = `${usedText} / ${totalText}`;
    cell.appendChild(meta);

    if (sub) {
      const subEl = document.createElement("div");
      subEl.className = "cc-mem-cell-meta";
      subEl.textContent = sub;
      subEl.title = sub;
      cell.appendChild(subEl);
    }
    if (extra) {
      const ex = document.createElement("div");
      ex.className = "cc-mem-cell-meta";
      const parts = [];
      if (extra.temp != null)   parts.push(extra.temp.toFixed(1) + "°C");
      if (extra.npuPct != null) parts.push("NPU " + fmtNum(extra.npuPct, 0) + "%");
      if (extra.power != null)  parts.push((extra.power / 1000).toFixed(2) + " W");
      if (extra.status)         parts.push(extra.status);
      ex.textContent = parts.join(" · ");
      cell.appendChild(ex);
    }
    return cell;
  },

  // ...
  _renderBanners() {
    const stack = document.getElementById("cc-banner-stack");
    if (!stack) return;
    const snap = this._snapshot || {};
  // ...
    const errored = (snap.instances || []).filter(
      i => i.status === "error");
    const missingDirs = (snap.instances || []).filter(
      i => i.status === "missing_model_dir");
    const stuck = this._detectStuck(snap);

    stack.innerHTML = "";

    if (errored.length) {
  // ...
      const buckets = {
        spawned: [],
        adopted_local: [],
        adopted: [],
      };
      for (const inst of errored) {
        const k = inst._source in buckets ? inst._source : "spawned";
        buckets[k].push(inst);
      }
      const renderBucket = (kind, title, list) => {
        if (!list.length) return;
        const ul = document.createElement("ul");
        ul.className = "cc-banner-list";
        for (const inst of list) {
          const li = document.createElement("li");
          const head = document.createElement("div");
          const label = inst.label || inst.model;
          let line;
          if (inst._source === "adopted_local") {
            const pid = inst.pid ? ` · PID ${inst.pid}` : "";
            line = `${label}${pid} · NPU #${inst.npu_index}`;
            head.title = `本机孤儿 worker（uvicorn reload 残留），无 Popen 句柄；要清理请 kill ${inst.pid}`;
          } else if (inst._source === "adopted") {
            const peerLabel = inst._peer_name || "远端";
            line = `${label} @ ${peerLabel} · NPU #${inst.npu_index}`;
            head.title = `远端 peer 提供的 service，本机无权操作`;
          } else {
            line = `${label} · NPU #${inst.npu_index}`;
          }
          head.textContent = line;
          li.appendChild(head);
          const msg = document.createElement("pre");
          msg.className = "cc-banner-msg";
          msg.textContent = inst.error || "(无错误信息)";
          li.appendChild(msg);
  // ...
          const clearBtn = document.createElement("button");
          clearBtn.type = "button";
          clearBtn.className = "cc-banner-row-clear";
          clearBtn.textContent = "✕";
          clearBtn.title = `清掉 ${inst.model} 的错误`;
          clearBtn.addEventListener("click", (ev) => {
            ev.stopPropagation();
            this._clearErrors(inst.model);
          });
          li.appendChild(clearBtn);
          ul.appendChild(li);
        }
        stack.appendChild(this._makeBanner({
          kind: kind,
          icon: kind === "is-error" ? "✖" : "⚠",
          title: title(list),
          body: ul,
          actions: [
  // ...
            {label: "全部清空", onClick: () => this._clearErrors(null)},
          ],
        }));
      };

  // ...
      renderBucket("is-error",
        (l) => `${l.length} 个算力实例启动失败（用户主动启用）`,
        buckets.spawned);
      renderBucket("is-warn",
        (l) => `${l.length} 个本机孤儿 worker 异常（reload 残留，需 kill PID 清理）`,
        buckets.adopted_local);
      renderBucket("is-warn",
        (l) => `${l.length} 个对端 worker 失联（远端机子，本机无权操作）`,
        buckets.adopted);
    }

    if (missingDirs.length) {
      const ul = document.createElement("ul");
      ul.className = "cc-banner-list";
      for (const inst of missingDirs) {
        const li = document.createElement("li");
        const head = document.createElement("div");
        head.textContent = `${inst.label || inst.model}${inst.model_dir ? " · " + inst.model_dir : ""}`;
        li.appendChild(head);
        ul.appendChild(li);
      }
      stack.appendChild(this._makeBanner({
        kind: "is-warn",
        icon: "⚠",
        title: `${missingDirs.length} 个算力模型目录缺失（不算"启动失败"，补齐后 toggle 一下）`,
        body: ul,
        actions: [
          {label: "刷新", onClick: () => this.load()},
        ],
      }));
    }

    if (stuck.length) {
      const ul = document.createElement("ul");
      ul.className = "cc-banner-list";
      for (const s of stuck) {
        const li = document.createElement("li");
        li.textContent = `${s.label} · NPU #${s.npu_index} · ${s.status} 累计 ${s.elapsed}s`;
        ul.appendChild(li);
      }
      stack.appendChild(this._makeBanner({
        kind: "is-warn",
        icon: "⚠",
        title: `${stuck.length} 个实例加载疑似卡住`,
        body: ul,
        actions: [
          {label: "刷新", onClick: () => this.load()},
        ],
      }));
    }
  },

  _makeBanner({kind, icon, title, body, actions}) {
    const el = document.createElement("div");
    el.className = `cc-banner ${kind}`;
    const iconEl = document.createElement("span");
    iconEl.className = "cc-banner-icon";
    iconEl.textContent = icon;
    const bodyEl = document.createElement("div");
    bodyEl.className = "cc-banner-body";
    const t = document.createElement("div");
    t.className = "cc-banner-title";
    t.textContent = title;
    bodyEl.appendChild(t);
    if (body) bodyEl.appendChild(body);
    el.appendChild(iconEl);
    el.appendChild(bodyEl);
    if (actions && actions.length) {
      const acts = document.createElement("div");
      acts.className = "cc-banner-actions";
      for (const a of actions) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "cc-banner-btn";
        btn.textContent = a.label;
        btn.addEventListener("click", a.onClick);
        acts.appendChild(btn);
      }
      el.appendChild(acts);
    }
    return el;
  },

  _detectStuck(snap) {
    const now = Date.now() / 1000;
    const perModel = snap.estimated_init_seconds_per_model || {};
    const out = [];
    for (const inst of snap.instances || []) {
      if (inst.status !== "queued" && inst.status !== "loading") continue;
      const est = perModel[inst.model_id] ?? snap.default_estimated_init_seconds ?? 10;
      const startTs = inst.status === "queued"
        ? (inst.queued_at || 0)
        : (inst.started_at || inst.queued_at || 0);
      const elapsed = Math.max(0, Math.round(now - startTs));
      const threshold = est * 2 + 30;
      if (elapsed >= threshold) {
        out.push({...inst, elapsed, threshold});
      }
    }
    return out;
  },

  // ...
  async _clearErrors(modelId) {
    try {
      const r = await fetch(API_PREFIX + "/compute/clear-errors", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({model_id: modelId || null}),
      });
      if (!r.ok) {
        const text = await r.text();
        alert(`清空错误失败 (HTTP ${r.status}): ${text}`);
      }
    } catch (e) {
      alert("请求失败: " + e.message);
    }
    await this.load();
  },

  // ...
  _renderServices() {
    const localRoot = document.getElementById("cc-local-cards");
    const peerRoot  = document.getElementById("cc-peer-cards");
    if (!localRoot || !peerRoot) return;
  // ...
    const snap = this._snapshot || {};
    const cards = snap.cards || [];
    const rawServices = snap.services || [];

  // ...
    const services = rawServices;

    if (!services.length) {
      const empty = document.createElement("div");
      empty.className = "cc-services-empty";
      empty.textContent = "未声明任何 compute service —— 检查 MODEL_PRESETS 配置。";
      localRoot.appendChild(empty);
      return;
    }

  // ...
    const ordered = [];
    const seen = new Set();
    const seenKey = (s) => `${s.model_id}__${s._source || ""}`;
    const orderOf = (mid) => {
      const i = (snap.preset_order || []).indexOf(mid);
      return i === -1 ? 9999 : i;
    };
    for (const id of (snap.preset_order || [])) {
      for (const s of services.filter(x => x.model_id === id)) {
        const k = seenKey(s);
        if (!seen.has(k)) { ordered.push(s); seen.add(k); }
      }
    }
    for (const s of services) {
      const k = seenKey(s);
      if (!seen.has(k)) { ordered.push(s); seen.add(k); }
    }

  // ...
    const prevCard = this._renderedCard || {};
    const newCard = {};
    const newOrder = [];   // [{model_id, container: localRoot|<frame grid>}]

  // ...
    const peerFrameOrder = [];          // [{key, name, ip, services: [...]}]
    const peerFrameByKey = new Map();
    for (const s of ordered) {
      if (s._source === "adopted") {
        const name = (s._peer_name || "").trim() || "远端";
        const ip = (s._peer_ip || "").trim();
        const key = `${name}@${ip}`;
        if (!peerFrameByKey.has(key)) {
          const group = { key, name, ip, services: [] };
          peerFrameByKey.set(key, group);
          peerFrameOrder.push(group);
        }
        peerFrameByKey.get(key).services.push(s);
      }
    }
  // ...
    for (const g of peerFrameOrder) {
      g.services.sort((a, b) => {
        const aPos = orderOf(a.model_id);
        const bPos = orderOf(b.model_id);
        if (aPos !== bPos) return aPos - bPos;
        return a.model_id.localeCompare(b.model_id);
      });
    }
    const prevPeerFrame = this._renderedPeerFrame || {};
    const newPeerFrame = {};

  // ...
    for (const group of peerFrameOrder) {
      const prev = prevPeerFrame[group.key];
  // ...
      const memberSig = group.services.map(x => x.model_id).join("|");
      const frameSig = `${group.name}|${group.ip}|${memberSig}`;
      if (prev && prev.frameSig === frameSig && prev.frame.parentNode === peerRoot) {
        newPeerFrame[group.key] = prev;
        continue;
      }
  // ...
      if (prev && prev.frame && prev.frame.parentNode) {
        prev.frame.parentNode.removeChild(prev.frame);
      }
      const frame = document.createElement("section");
      frame.className = "cc-device-frame";
      frame.dataset.deviceKey = group.key;

      const head = document.createElement("header");
      head.className = "cc-device-head";
      const title = document.createElement("h3");
      title.className = "cc-device-title";
      title.textContent = group.ip
        ? `${group.name}@${group.ip}`
        : group.name;
      head.appendChild(title);
      frame.appendChild(head);

      const grid = document.createElement("div");
      grid.className = "cc-device-cards";
      frame.appendChild(grid);

      newPeerFrame[group.key] = { frame, grid, frameSig };
    }

    for (const s of ordered) {
  // ...
      const meta = {
        label:                s.label || s.model_id,
        capability_types:     s.capability_types || [],
        memory_estimate_mb:   s.memory_estimate_mb || 0,
        estimated_init_seconds:
          (snap.estimated_init_seconds_per_model || {})[s.model_id]
          || snap.default_estimated_init_seconds
          || 30,
        // 恒为空：manifest 不声明子模型，无数据源填过它。渲染端仍读此字段。
        submodels:            [],
        description:          s.description || "",
        available_targets:    s.available_targets || ["182x"],
      };
      const isLocal = s._source !== "adopted";

  // ...
      const sig = [
        s._source,
        s.status,
        s.npu_index,
        s.pid,
        s.error || "",
        s.label,
        meta.description,
        meta.available_targets.join(","),
        cards.map(c => c.npu_index).join(","),
  // ...
        this._selectedTargets(s.model_id).join(",") || "_",
        this._modelNpus(s.model_id).join(","),
      ].join("|");
      const prev = prevCard[seenKey(s)];
      let targetContainer;
      if (isLocal) {
        targetContainer = localRoot;
      } else {
  // ...
        const name = (s._peer_name || "").trim() || "远端";
        const ip = (s._peer_ip || "").trim();
        const frameKey = `${name}@${ip}`;
        targetContainer = newPeerFrame[frameKey]?.grid || peerRoot;
      }

      if (prev && prev.card && prev.card.parentNode) {
  // ...
        if (isLocal && prev.card._h) {
          this._updateLocalCardState(prev.card, s, meta, cards);
        } else {
  // ...
          prev.card.parentNode.removeChild(prev.card);
          prev.card = isLocal
            ? this._makeLocalCard(s.model_id, meta, s, cards)
            : this._makePeerCard(s.model_id, meta, s);
          prev.sig = sig;
          prev.isLocal = isLocal;
          newCard[seenKey(s)] = prev;
          newOrder.push({model_id: s.model_id, container: targetContainer, card: prev.card});
          continue;
        }
        prev.sig = sig;
        prev.isLocal = isLocal;
  // ...
        if (prev.card.parentNode !== targetContainer) {
          if (prev.card.parentNode) prev.card.remove();
          targetContainer.appendChild(prev.card);
        }
        newCard[seenKey(s)] = prev;
        newOrder.push({model_id: s.model_id, container: targetContainer, card: prev.card});
        continue;
      }

  // ...
      const card = isLocal
        ? this._makeLocalCard(s.model_id, meta, s, cards)
        : this._makePeerCard(s.model_id, meta, s);
      newCard[seenKey(s)] = { card, sig, isLocal };
      newOrder.push({model_id: s.model_id, container: targetContainer, card});
    }

  // ...

  // ...
    for (const group of peerFrameOrder) {
      const f = newPeerFrame[group.key];
      if (!f) continue;
  // ...
      const idx = peerFrameOrder.indexOf(group);
      const after = idx > 0 ? newPeerFrame[peerFrameOrder[idx - 1].key]?.frame : null;
      const desiredParent = peerRoot;
      const desiredNext = after ? after.nextSibling : null;
      if (f.frame.parentNode !== desiredParent) {
        desiredParent.insertBefore(f.frame, desiredNext);
      } else if (f.frame !== desiredNext) {
        desiredParent.insertBefore(f.frame, desiredNext);
      }
    }

    this._renderedPeerFrame = newPeerFrame;
  // ...
    for (const [key, prev] of Object.entries(prevPeerFrame)) {
      if (!(key in newPeerFrame)) {
        try { prev.frame.remove(); } catch (_e) {}
      }
    }

  // ...
    const byContainer = new Map();
    for (const item of newOrder) {
      if (!byContainer.has(item.container)) byContainer.set(item.container, []);
      byContainer.get(item.container).push(item);
    }
    for (const [container, items] of byContainer) {
      for (let i = items.length - 1; i >= 0; i--) {
        const item = items[i];
        const desiredNext = i + 1 < items.length ? items[i + 1].card : null;
        if (item.card.parentNode !== container) {
          container.insertBefore(item.card, desiredNext);
        } else if (item.card.nextSibling !== desiredNext) {
          container.insertBefore(item.card, desiredNext);
        }
      }
    }

    this._renderedCard = newCard;
  // ...
    for (const [mid, prev] of Object.entries(prevCard)) {
      if (!(mid in newCard)) {
        try { prev.card.remove(); } catch (_e) {}
      }
    }

  // ...
    const peerEmpty = document.getElementById("cc-peer-empty");
    if (peerEmpty) {
      const hasFrames = peerFrameOrder.length > 0;
      peerEmpty.classList.toggle("hidden", hasFrames);
    }
  },

  // ...
  _makeLocalCard(modelId, meta, svc, cards) {
    const card = document.createElement("article");
    card.className = "cc-service-card";
    card.dataset.model = modelId;

  // ...
    const head = document.createElement("header");
    head.className = "cc-service-head";
    card.appendChild(head);

    const titleEl = document.createElement("h3");
    titleEl.className = "cc-service-title";
    head.appendChild(titleEl);

  // ...
    const autostartBadge = document.createElement("span");
    autostartBadge.className = "cc-autostart-badge";
    autostartBadge.style.display = "none";
    head.appendChild(autostartBadge);

    const pidEl = document.createElement("span");
    pidEl.className = "cc-pid";
    pidEl.style.display = "none";
    head.appendChild(pidEl);

    const headActions = document.createElement("div");
    headActions.className = "cc-head-actions";
    head.appendChild(headActions);

  // ...
    const restartBtn = document.createElement("button");
    restartBtn.className = "cc-btn cc-btn-restart";
    restartBtn.textContent = "重启服务";
    restartBtn.title = "Q3 4 步：unload → shutdown → wait/kill → 走 boot 流程（不自动 bind）";
    restartBtn.addEventListener("click", () => {
      if (confirm(`确认重启 ${meta.label}？重启后需重新选择 NPU 才能 inference。`)) {
        this._restartInstance(modelId);
      }
    });
    headActions.appendChild(restartBtn);

  // ...
    const toggle = document.createElement("label");
    toggle.className = "cc-toggle";
    const toggleInput = document.createElement("input");
    toggleInput.type = "checkbox";
    toggleInput.setAttribute("aria-label", `${meta.label}：未启用，点击切换`);
    toggleInput.addEventListener("change", () => {
      if (toggleInput.disabled) return;
  // ...
      const curSvc = this._findSvcInSnapshot(modelId);
      const curCards = (this._snapshot && this._snapshot.cards) || [];
      const targets = this._selectedTargets(modelId);
      const target = (targets && targets.length) ? targets
        : (curSvc && curSvc.npu_index >= 0
            ? curSvc.npu_index
            : this._defaultTargetNpu(curCards));
      if (toggleInput.checked) {
        this._enableInstance(target, modelId);
      } else {
        if (confirm(`确认禁用 ${meta.label}？将卸载模型并关闭 worker 进程。`)) {
          this._disableInstance(modelId);
        } else {
  // ...
          toggleInput.checked = true;
        }
      }
    });
    const toggleTrack = document.createElement("span");
    toggleTrack.className = "cc-toggle-track";
    const toggleThumb = document.createElement("span");
    toggleThumb.className = "cc-toggle-thumb";
    toggleTrack.appendChild(toggleThumb);
    toggle.appendChild(toggleInput);
    toggle.appendChild(toggleTrack);
    headActions.appendChild(toggle);

  // ...
    const body = document.createElement("div");
    body.className = "cc-service-body";
    card.appendChild(body);

  // ...
    const capsEl = document.createElement("div");
    capsEl.className = "cc-service-caps";
    body.appendChild(capsEl);

  // ...
    const missingDirEl = document.createElement("div");
    missingDirEl.className = "cc-missing-dir";
    missingDirEl.style.display = "none";
    body.appendChild(missingDirEl);

  // ...
    const radioFieldsetEl = document.createElement("fieldset");
    radioFieldsetEl.className = "cc-target-pick";
    body.appendChild(radioFieldsetEl);

  // ...
    const countdownEl = document.createElement("div");
    countdownEl.className = "cc-load-countdown";
    countdownEl.style.display = "none";
    body.appendChild(countdownEl);

  // ...
    const errorEl = document.createElement("pre");
    errorEl.className = "cc-service-error-pre";
    errorEl.style.display = "none";
    body.appendChild(errorEl);

    card._h = {
      titleEl, autostartBadge, pidEl, restartBtn, toggle, toggleInput,
      body, capsEl, missingDirEl, radioFieldsetEl,
      countdownEl, errorEl,
  // ...
      lastRadioSig: null,
    };

  // ...
    this._updateLocalCardState(card, svc, meta, cards);

    return card;
  },

  // ...
  _updateLocalCardState(card, svc, meta, cards) {
    const h = card._h;
    if (!h) return;
    const state = svc.status || "not_loaded";
    const isOn     = state === "running" || state === "loading";
    const isLoad   = state === "loading";
    const isErr    = state === "error";
    const isMissingDir = state === "missing_model_dir";
    const noCards  = !cards || cards.length === 0;
    const isOrphan = svc._source === "adopted_local";
    const isUnavail = svc._source === "unavailable";

  // ...
    card.dataset.source = svc._source || "spawned";
    card.classList.toggle("is-orphan", isOrphan);
    card.classList.toggle("is-running", state === "running");
    card.classList.toggle("is-pending", isLoad);
    card.classList.toggle("has-error", isErr);

  // ...
    h.titleEl.textContent = meta.label;

  // ...
    if (svc._autostart === "enabled") {
      h.autostartBadge.textContent = "开机自启";
      h.autostartBadge.title = "已列入 config.jsonc 的 compute.autostart —— boot 时自动 spawn worker 并绑定 NPU 卡；reload / 重启后会自动恢复";
      h.autostartBadge.style.display = "";
    } else {
      h.autostartBadge.style.display = "none";
      h.autostartBadge.textContent = "";
      h.autostartBadge.title = "";
    }

  // ...
    if (svc.pid && svc._source !== "adopted" && svc._source !== "unavailable") {
      h.pidEl.textContent = `PID ${svc.pid}`;
      h.pidEl.title = isOrphan
        ? "uvicorn reload 留下的 worker，registry 没持有 Popen 句柄；要清理请 `kill " + svc.pid + "`"
        : "本机 spawn 的 worker，registry 持有 Popen 句柄；要清理优先用 UI 重启按钮";
      h.pidEl.style.display = "";
    } else {
      h.pidEl.style.display = "none";
    }

  // ...
    const isOff = state === "not_loaded" || state === "missing_model_dir";
    h.restartBtn.style.display = (isOrphan || isUnavail || isOff) ? "none" : "";
    h.restartBtn.disabled = isLoad;

  // ...
    h.toggle.classList.toggle("is-on", isOn);
    h.toggle.classList.toggle("is-load", isLoad);
    h.toggle.classList.toggle("is-err", isErr);
    h.toggle.classList.toggle("is-disabled", noCards);

    const targetPicked = this._hasTargetPicked(card.dataset.model, svc);
    const needsTarget = !isOn && !isErr && !isMissingDir
                     && svc._source !== "adopted"
                     && svc._source !== "unavailable";
    const toggleBlocked = noCards
                       || isLoad
                       || isErr
                       || (needsTarget && !targetPicked);
    h.toggleInput.checked = isOn;
    h.toggleInput.disabled = toggleBlocked;
    h.toggleInput.setAttribute("aria-label",
      `${meta.label}：${isOn ? "已启用" : "未启用"}，点击切换`);

    if (noCards) {
      h.toggle.title = "无可用 NPU 卡";
    } else if (isLoad) {
      h.toggle.title = "加载中，已锁定";
    } else if (isMissingDir) {
      h.toggle.title = svc.model_dir
        ? `模型目录 ${svc.model_dir} 不存在；点 toggle 重试 load`
        : `模型目录不存在；点 toggle 重试 load`;
    } else if (isErr) {
      if (svc._source === "adopted_local") {
        h.toggle.title = svc.pid
          ? `本机孤儿 worker 异常 (PID ${svc.pid})；点 toggle 重发 RPC load，若仍失败请 kill ${svc.pid} 重启`
          : `本机孤儿 worker 异常；点 toggle 重发 RPC load`;
      } else {
        h.toggle.title = "启动失败，点击重试";
      }
    } else if (needsTarget && !targetPicked) {
      h.toggle.title = "请先选择运行 NPU";
    } else {
      h.toggle.title = isOn
        ? `运行中，点击禁用 ${meta.label}`
        : `点击启用 ${meta.label}`;
    }

  // ...
    h.capsEl.innerHTML = "";
    if (meta.capability_types && meta.capability_types.length) {
      h.capsEl.style.display = "";
      for (const cap of meta.capability_types) {
        const c = document.createElement("span");
        c.className = "cc-service-cap";
        c.textContent = cap;
        h.capsEl.appendChild(c);
      }
    } else {
      h.capsEl.style.display = "none";
    }

  // ...
    if (isMissingDir) {
      h.missingDirEl.innerHTML = "";
      const label = document.createElement("span");
      label.className = "cc-missing-dir-label";
      label.textContent = "⚠ 模型目录不存在";
      h.missingDirEl.appendChild(label);
      const path = document.createElement("code");
      path.className = "cc-missing-dir-path";
      path.textContent = svc.model_dir || "(未知路径)";
      h.missingDirEl.appendChild(path);
      const note = document.createElement("span");
      note.className = "cc-missing-dir-note";
      note.textContent = "补齐目录后点 toggle 重试 load，无需重启 worker";
      h.missingDirEl.appendChild(note);
      h.missingDirEl.style.display = "";
    } else {
      h.missingDirEl.style.display = "none";
    }

  // ...
    const radioSig = [
      (cards || []).map(c => `${c.npu_index}:${c.device_id || ""}`).join(","),
      (meta.available_targets || []).join(","),
      String(svc.npu_index),
    ].join("|");
    if (h.lastRadioSig !== radioSig) {
      const newFs = this._makeTargetFieldset(card.dataset.model, meta, svc, cards);
      h.radioFieldsetEl.replaceWith(newFs);
      h.radioFieldsetEl = newFs;
      h.lastRadioSig = radioSig;
    }

  // ...
    if (isLoad) {
      h.countdownEl.textContent = `初始化中… 约 ${this._countdownSec(svc, "loading")}s`;
      h.countdownEl.style.display = "";
    } else {
      h.countdownEl.style.display = "none";
    }

  // ...
    if (isErr && svc.error) {
      h.errorEl.textContent = svc.error;
      h.errorEl.style.display = "";
    } else {
      h.errorEl.style.display = "none";
    }
  },

  // ...
  _findSvcInSnapshot(modelId) {
    const svcs = (this._snapshot && this._snapshot.services) || [];
    return svcs.find(s => s.model_id === modelId) || null;
  },

  // 该模型当前占用的**所有**卡（同一模型可同时跑多张卡）
  _modelNpus(modelId) {
    const svcs = (this._snapshot && this._snapshot.services) || [];
    return Array.from(new Set(
      svcs.filter(s => s.model_id === modelId && s.npu_index >= 0)
           .map(s => s.npu_index))).sort((a, b) => a - b);
  },

  // 「运行在」的勾选集合（数组）。兼容早期单值。
  _selectedTargets(modelId) {
    const raw = this._selectedTarget[modelId];
    if (Array.isArray(raw)) return raw.filter(n => Number.isInteger(n) && n >= 0);
    if (typeof raw === "number" && raw >= 0) return [raw];
    return [];
  },

  // ...
  _makePeerCard(modelId, meta, svc) {
    const card = document.createElement("article");
    card.className = "cc-service-card cc-service-card-peer";
    card.dataset.model = modelId;
    card.dataset.source = svc._source;  // "adopted"

    const state = svc.status || "not_loaded";
    if (state === "running") card.classList.add("is-running");
    if (state === "loading") card.classList.add("is-pending");
    if (state === "error")   card.classList.add("has-error");

  // ...
    const head = document.createElement("header");
    head.className = "cc-service-head";
    const title = document.createElement("h3");
    title.className = "cc-service-title";
  // ...
    title.textContent = svc._peer_name
      ? `${meta.label} @ ${svc._peer_name}`
      : meta.label;
    head.appendChild(title);

    const peerId = document.createElement("span");
    peerId.className = "cc-peer-id";
    const peerLabel = svc._peer_name || (svc._peer_guid || "").slice(0, 8) || "远端";
    peerId.textContent = `peer: ${peerLabel}`;
    if (svc.pid) {
      peerId.textContent += ` · PID ${svc.pid}`;
      peerId.title = `远端 worker 真实 PID ${svc.pid}`;
    }
    head.appendChild(peerId);

  // ...
    const dot = document.createElement("span");
    dot.className = "cc-state-dot";
    dot.dataset.state = state;
    head.appendChild(dot);

    card.appendChild(head);

    const body = document.createElement("div");
    body.className = "cc-service-body";

    const ro = document.createElement("div");
    ro.className = "cc-readonly";
    ro.textContent = "只读 · 本机无权操作 peer service";
    body.appendChild(ro);

    if (state === "error" && svc.error) {
      const pre = document.createElement("pre");
      pre.className = "cc-service-error-pre";
      pre.textContent = svc.error;
      body.appendChild(pre);
    }

    card.appendChild(body);
    return card;
  },

  async _enableInstance(npu_index, model) {
    const cards = Array.isArray(npu_index) ? npu_index : [npu_index];
    try {
      const r = await fetch(API_PREFIX + "/compute/instances", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({npu_index: cards, model}),
      });
      if (!r.ok) {
        const text = await r.text();
        alert(`启用失败 (HTTP ${r.status}): ` +
              computeErrorDetail(r.status, text));
      }
    } catch (e) {
      alert("请求失败: " + e.message);
    }
    await this.load();
  },

  async _disableInstance(model) {
    try {
      const r = await fetch(API_PREFIX + "/compute/instances", {
        method: "DELETE",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({model}),
      });
      if (!r.ok) {
        const text = await r.text();
        alert(`卸载失败 (HTTP ${r.status}): ` +
              computeErrorDetail(r.status, text));
      }
    } catch (e) {
      alert("请求失败: " + e.message);
    }
    await this.load();
  },

  async _restartInstance(model) {
    try {
      const r = await fetch(API_PREFIX + "/compute/restart", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({model}),
      });
      if (!r.ok) {
        const text = await r.text();
        alert(`重启失败 (HTTP ${r.status}): ` +
              computeErrorDetail(r.status, text));
      }
    } catch (e) {
      alert("请求失败: " + e.message);
    }
    await this.load();
  },

  _makeServiceCard(modelId, meta, inst, cards) {
    const card = document.createElement("article");
    card.className = "cc-service-card";
    card.dataset.model = modelId;

  // ...
    const state = inst ? inst.status : "disabled";
    if (state === "running")    card.classList.add("is-running");
    if (state === "loading" || state === "queued")
                                card.classList.add("is-pending");
    if (state === "error")      card.classList.add("has-error");

  // ...
    const head = document.createElement("header");
    head.className = "cc-service-head";
    const title = document.createElement("h3");
    title.className = "cc-service-title";
    title.textContent = meta.label;
    head.appendChild(title);
    for (const cap of meta.capability_types || []) {
      const c = document.createElement("span");
      c.className = "cc-service-cap";
      c.textContent = cap;
      head.appendChild(c);
    }
    const badge = document.createElement("span");
    badge.className = "cc-service-badge";
    badge.dataset.state = state;
    if (state === "running")        { badge.classList.add("is-running");    badge.textContent = "运行中"; }
    else if (state === "loading")   { badge.classList.add("is-loading");    badge.textContent = "加载中…"; }
    else if (state === "queued")    { badge.classList.add("is-queued");     badge.textContent = "排队中"; }
    else if (state === "ready_lazy"){ badge.classList.add("is-ready-lazy"); badge.textContent = "已启用（待加载）"; }
    else if (state === "error")     { badge.classList.add("is-error");      badge.textContent = "启动失败"; }
    else                            { badge.textContent = "未启用"; }
    head.appendChild(badge);
    card.appendChild(head);

    const body = document.createElement("div");
    body.className = "cc-service-body";

  // ...
    if (meta.submodels && meta.submodels.length) {
      const subs = document.createElement("div");
      subs.className = "cc-submodels";
      for (const sm of meta.submodels) {
        const row = document.createElement("div");
        row.className = "cc-submodel-row";
        const role = document.createElement("span");
        role.className = "cc-submodel-role";
        role.textContent = sm.role;
        row.appendChild(role);
        const mem = document.createElement("span");
        mem.className = "cc-submodel-mem";
        mem.textContent = `~${sm.memory_estimate_mb} MB · 加载 ${sm.estimated_init_seconds}s`;
        row.appendChild(mem);
        const tg = document.createElement("span");
        tg.className = "cc-submodel-targets";
        tg.textContent = this._targetsLabel(sm.files);
        tg.title = (sm.files || []).join(", ");
        row.appendChild(tg);
        subs.appendChild(row);
      }
      body.appendChild(subs);
    }

  // ...
    const fieldset = this._makeTargetFieldset(modelId, meta, inst, cards);
    body.appendChild(fieldset);

  // ...
    const actions = document.createElement("div");
    actions.className = "cc-service-actions";
    const sw = document.createElement("button");
    sw.type = "button";
    sw.className = "cc-svc-switch";
    const selectedTarget = this._selectedTargets(modelId)[0]
      ?? (inst ? inst.npu_index : this._defaultTargetNpu(cards));
    sw.dataset.target = String(selectedTarget);

    const noCards = !cards || cards.length === 0;
    const isOn   = state === "running" || state === "loading" || state === "queued";
    const isLoad = state === "loading" || state === "queued";
    const isErr  = state === "error";
    sw.classList.toggle("is-on", isOn);
    sw.classList.toggle("is-loading", isLoad);
    sw.classList.toggle("is-error", isErr);
  // ...
    if (noCards || isLoad) {
      sw.classList.add("is-disabled");
      sw.disabled = true;
    }
    sw.title = noCards
      ? "无可用 NPU 卡"
      : this._switchTitle(meta, inst, isLoad);
    sw.setAttribute("aria-label",
      this._switchAriaLabel(meta, inst, noCards, isLoad));

    const knob = document.createElement("span");
    knob.className = "cc-svc-switch-knob";
    sw.appendChild(knob);

    sw.addEventListener("click", () => {
      if (sw.disabled) return;
      const target = parseInt(sw.dataset.target, 10);
      this.toggle(modelId, target);
    });
    actions.appendChild(sw);
    body.appendChild(actions);

    const statusLine = document.createElement("div");
    statusLine.className = "cc-service-status-line";
    statusLine.dataset.state = state;
    statusLine.textContent = this._statusLineText(modelId, meta, inst);
    body.appendChild(statusLine);

  // ...
    if (inst && inst.status === "error" && inst.error) {
      const pre = document.createElement("pre");
      pre.className = "cc-service-error-pre";
      pre.textContent = inst.error;
      body.appendChild(pre);
    }

    card.appendChild(body);
    return card;
  },

  // ...
  _targetsLabel(files) {
    if (!files || !files.length) return "—";
    const suffixes = new Set();
    for (const f of files) {
      if (f.endsWith(".1828.rknn")) suffixes.add("182x");
      else if (f.endsWith(".3588.rknn")) suffixes.add("3588");
      else if (f.endsWith(".weight"))   suffixes.add("(共享权重)");
    }
    if (!suffixes.size) return files.join(", ");
    return Array.from(suffixes).join(" + ");
  },

  // ...
  _makeTargetFieldset(modelId, meta, inst, cards) {
  // ...
    const fs = document.createElement("fieldset");
    fs.className = "cc-target-pick";
    const legend = document.createElement("legend");
    legend.textContent = "运行在";
    fs.appendChild(legend);

    const opts = document.createElement("div");
    opts.className = "cc-target-options";

  // ...
    const MAX_RK182X_OPTIONS = CC_MAX_RK182X_CARDS;
    const sorted = this._sortNpuCards(this._modelCards(cards));
    const sortedIndices = sorted.map(c => c.npu_index);

  // ...
    const supported = new Set(
      (meta.available_targets && meta.available_targets.length)
        ? meta.available_targets
        : (inst && inst.available_targets && inst.available_targets.length)
            ? inst.available_targets
            : ["182x"]
    );

  // ...
    let defaultIdx = null;
    if (this._selectedTargets(modelId).length
        && supported.has("182x")) {
      defaultIdx = this._selectedTargets(modelId)[0];
    } else if (inst && inst.npu_index >= 0
               && sortedIndices.includes(inst.npu_index)) {
      defaultIdx = inst.npu_index;
    } else if (inst && Number.isInteger(inst._default_npu_index)
               && inst._default_npu_index >= 0
               && sortedIndices.includes(inst._default_npu_index)) {
      defaultIdx = inst._default_npu_index;
    } else {
      for (const idx of sortedIndices) {
        if (supported.has("182x")) { defaultIdx = idx; break; }
      }
    }
    if (defaultIdx != null) {
      this._selectedTarget[modelId] = [defaultIdx];
    }

    let rendered = 0;
    const renderOpt = (npuIndex, title, sub, targetFamily, supportedFlag) => {
  // ...
      const isSup = supportedFlag !== false;
      if (!isSup) return;
      const label = document.createElement("label");
      label.className = "cc-target-opt";
      label.title = sub || "";
      const input = document.createElement("input");
      // checkbox：同一个模型可同时跑在多张卡上
      input.type = "checkbox";
      input.name = `cc-target-${modelId}`;
      input.value = String(npuIndex);
      input.className = "cc-target-radio";
  // ...
      if (npuIndex === defaultIdx) input.checked = true;
      input.addEventListener("change", () => {
        const cur = new Set(this._selectedTargets(modelId));
        if (input.checked) cur.add(npuIndex); else cur.delete(npuIndex);
        this._selectedTarget[modelId] = Array.from(cur);
      });
      label.appendChild(input);
      const txt = document.createElement("span");
      txt.textContent = title;
      label.appendChild(txt);
      opts.appendChild(label);
      rendered += 1;
    };

  // ...
    for (const card of sorted) {
      if (rendered >= MAX_RK182X_OPTIONS) break;
      const idx = card.npu_index;
      renderOpt(idx, `RK182x #${idx}`,
                card.device_id || `npu #${idx}`,
                "182x", supported.has("182x"));
    }
    if (rendered === 0) {
      const ph = document.createElement("span");
      ph.className = "cc-mem-empty";
      ph.style.fontSize = "12px";
      ph.textContent = "未检测到 NPU 卡 —— 等待 enumerate…";
      opts.appendChild(ph);
    }
    fs.appendChild(opts);
    return fs;
  },

  // 只保留「能跑 rknn 模型」的卡。``snapshot.cards`` 里混着 USB 序列号形态
  // 的 RGA/VPU 设备（模型一上去就 vision init_runtime failed）。算力卡位由
  // 后端 ``model_cards`` 给出；老后端没这字段时不过滤。
  _modelCards(cards) {
    const mc = this._snapshot && this._snapshot.model_cards;
    if (!Array.isArray(mc)) return cards;
    const keep = new Set(mc);
    return (cards || []).filter((c) => keep.has(c.npu_index));
  },

  _defaultTargetNpu(cards) {
    if (!cards || !cards.length) return 0;     // 兜底：选 enumerate #0
    const sorted = this._sortNpuCards(this._modelCards(cards));
    return sorted.length ? sorted[0].npu_index : 0;
  },

  // ...
  _sortNpuCards(cards) {
    const seen = new Map();
    for (const c of (cards || [])) {
      if (c && !seen.has(c.npu_index)) seen.set(c.npu_index, c);
    }
    return Array.from(seen.values()).sort((a, b) => {
      const idxA = (a.npu_index || 0) - (b.npu_index || 0);
      if (idxA !== 0) return idxA;
      const da = (a.device_id || "").toString();
      const db = (b.device_id || "").toString();
      return da.localeCompare(db);
    });
  },

  // ...
  _hasTargetPicked(modelId, svc) {
    if (svc && svc.npu_index >= 0) return true;
    if (this._modelNpus(modelId).length) return true;
    return this._selectedTargets(modelId).length > 0;
  },

  // ...
  _countdownSec(svc, kind) {
    const snap = this._snapshot || {};
    const perModel = snap.estimated_init_seconds_per_model || {};
    const est = perModel[svc.model_id] ?? snap.default_estimated_init_seconds ?? 30;
    if (kind === "loading") {
      const elapsed = Math.max(0, Date.now() / 1000 - (svc.started_at || 0));
      return Math.max(0, Math.round(est - elapsed));
    }
    return est;
  },

  _renderSummary() {
    const snap = this._snapshot || {};
    const cards = snap.cards || [];
    const running = snap.running_count || 0;
    const queued  = snap.queued_count  || 0;
    const loading = snap.loading_count || 0;
    const error   = snap.error_count   || 0;
    const rk182xCount = Math.min(
      CC_MAX_RK182X_CARDS,
      cards.length,
    );
    const parts = [
      `${rk182xCount} 张算力卡`,
      `${running} 运行中`,
      error   ? `${error} 失败`  : null,
      loading ? `${loading} 加载中` : null,
      queued  ? `${queued} 排队中`  : null,
    ].filter(Boolean);
    const at = document.getElementById("cc-collected-at");
    if (at) at.textContent = parts.join(" · ");
    const last = snap.last_updated;
    if (last) {
      const ago = Math.max(0, Math.round(Date.now() / 1000 - last));
      this.setFoot(`数据 ${ago} 秒前刷新`, ago > 5 ? "warn" : "");
    }
  },

  setFoot(msg, kind) {
    const el = document.getElementById("cc-foot");
    if (!el) return;
    el.className = "cc-foot " + (kind || "");
    el.textContent = msg || "";
  },
};

/* ---------------------------------------------------------------- *
 * 注册发现 (Discovery) page — 读 registry，不调 wdndds              *
 * ---------------------------------------------------------------- */
const Discovery = {
  // ...
  _hwCaps: [],
  _hwDirty: false,

  init() {
    if (this.initialized) return;
    this.initialized = true;

    const enableAll  = document.getElementById("discovery-hw-enable-all");
    const disableAll = document.getElementById("discovery-hw-disable-all");
    const saveBtn    = document.getElementById("discovery-hw-save");

    if (enableAll)  enableAll .addEventListener("click", () => this.setAllHw(true));
    if (disableAll) disableAll.addEventListener("click", () => this.setAllHw(false));
    if (saveBtn)    saveBtn   .addEventListener("click", () => this.saveHw());
  },

  async load() {
    this.init();
    try {
      const r = await fetch(API_PREFIX + "/compute/capabilities");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      this.render(data);
    } catch (e) {
      const list = document.getElementById("discovery-list");
      if (list) list.innerHTML = `<div class="page-placeholder danger">加载失败: ${e.message}</div>`;
    }
  },

  render(data) {
    const swCaps = (data && data.sw_capabilities) || [];
    const hwCaps = (data && data.hw_capabilities) || [];

    this._hwCaps = hwCaps.map((c) => ({ ...c }));
    this._hwDirty = false;
    this._renderHwPane();
    this._renderSwPane(swCaps);
  },

  _renderSwPane(caps) {
    const at = document.getElementById("discovery-collected-at");
    const list = document.getElementById("discovery-list");
    const summary = document.getElementById("discovery-sw-summary");
    const hint = document.getElementById("discovery-sw-hint");
    if (!list) return;
    if (hint) hint.textContent = `${caps.length} 个 SW capability`;
    if (at) at.textContent = `SW ${caps.length} · HW ${this._hwCaps.length}`;
    if (summary) {
      summary.textContent = caps.length === 0
        ? "当前没有 SW capability —— 到 算力配置 启用模型后这里会显示。"
        : "运行中实例暴露的软件能力（SW）。每个能力由一个 RKNN 模型承载。";
    }
    if (caps.length === 0) {
      list.innerHTML = `
        <div class="page-placeholder">
          <div class="page-placeholder-icon" aria-hidden="true">⌖</div>
          <div class="page-placeholder-title">等待算力实例</div>
          <div class="page-placeholder-text">
            启用 算力配置 中的实例后，这里会列出对应的能力。
          </div>
        </div>`;
      return;
    }
    list.innerHTML = "";
    for (const c of caps) {
      const card = document.createElement("article");
      card.className = "discovery-card";
      card.innerHTML = `
        <header class="discovery-card-head">
          <span class="discovery-cap-type">${c.type}</span>
          <code class="discovery-cap-name">${c.name}</code>
        </header>
        <div class="discovery-card-meta">
          <span>来自 <strong>${c.model}</strong></span>
          <span>NPU #${c.npu_index}</span>
          <span>~${c.memory_estimate_mb} MB</span>
        </div>
      `;
      list.appendChild(card);
    }
  },

  _renderHwPane() {
    const list = document.getElementById("discovery-hw-list");
    const summary = document.getElementById("discovery-hw-summary");
    const hint = document.getElementById("discovery-hw-hint");
    const saveBtn = document.getElementById("discovery-hw-save");
    if (!list) return;

    const enabledCount = this._hwCaps.filter((c) => c.enabled).length;
    if (hint) hint.textContent = `${enabledCount} / ${this._hwCaps.length} 已启用`;
    if (summary) {
      summary.textContent = this._hwDirty
        ? "有未保存的修改 —— 点“保存修改”写入 config.jsonc。"
        : "本机硬件能力（HW）配置；勾选/取消勾选后保存即生效。";
    }
    if (saveBtn) {
      saveBtn.disabled = !this._hwDirty;
      saveBtn.classList.toggle("vd-btn-primary", this._hwDirty);
    }

    list.innerHTML = "";
    for (const c of this._hwCaps) {
      const card = document.createElement("article");
      card.className = "discovery-card" + (c.enabled ? "" : " is-disabled");
      card.dataset.cap = c.capability_type;
      card.innerHTML = `
        <header class="discovery-card-head">
          <span class="discovery-cap-type is-hw">${c.capability_type}</span>
          <code class="discovery-cap-name">${c.class_name}</code>
          <label class="toggle" title="${c.enabled ? "点击禁用" : "点击启用"}">
            <input type="checkbox" ${c.enabled ? "checked" : ""} />
            <span class="toggle-track"></span>
          </label>
        </header>
        <div class="discovery-card-meta">
          <span>类别：<strong>${c.category}</strong></span>
          <span class="discovery-card-status ${c.enabled ? "is-on" : "is-off"}">${c.enabled ? "已启用" : "已禁用"}</span>
        </div>
        <div class="discovery-card-desc">${c.description}</div>
      `;
      const cb = card.querySelector('input[type="checkbox"]');
      cb.addEventListener("change", () => {
        c.enabled = cb.checked;
        card.classList.toggle("is-disabled", !c.enabled);
        const status = card.querySelector(".discovery-card-status");
        status.textContent = c.enabled ? "已启用" : "已禁用";
        status.classList.toggle("is-on", c.enabled);
        status.classList.toggle("is-off", !c.enabled);
        cb.parentElement.title = c.enabled ? "点击禁用" : "点击启用";
        this._hwDirty = true;
        this._renderHwPane();
      });
      list.appendChild(card);
    }
  },

  setAllHw(value) {
    for (const c of this._hwCaps) c.enabled = !!value;
    this._hwDirty = true;
    this._renderHwPane();
  },

  async saveHw() {
    const saveBtn = document.getElementById("discovery-hw-save");
    if (saveBtn) saveBtn.disabled = true;
    const updates = {};
    for (const c of this._hwCaps) updates[c.capability_type] = !!c.enabled;
    try {
      const r = await fetch(API_PREFIX + "/compute/hardware-capabilities", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ updates }),
      });
      const text = await r.text();
      let body;
      try { body = JSON.parse(text); } catch { body = { detail: text }; }
      if (!r.ok) {
        const msg = body.detail || ("HTTP " + r.status);
        const summary = document.getElementById("discovery-hw-summary");
        if (summary) {
          summary.textContent = "保存失败: " + msg;
          summary.classList.add("danger");
        }
        return;
      }
  // ...
      if (Array.isArray(body.hw_capabilities)) {
        this._hwCaps = body.hw_capabilities.map((c) => ({ ...c }));
      }
      this._hwDirty = false;
      this._renderHwPane();
      const summary = document.getElementById("discovery-hw-summary");
      if (summary) {
        summary.classList.remove("danger");
        summary.textContent = `已保存 · applied=${body.applied.length} rejected=${body.rejected.length}`;
      }
    } catch (e) {
      const summary = document.getElementById("discovery-hw-summary");
      if (summary) {
        summary.textContent = "保存失败: " + e.message;
        summary.classList.add("danger");
      }
    } finally {
      if (saveBtn) saveBtn.disabled = !this._hwDirty;
    }
  },
};


/* ------------------------------------------------------------------ *
 *  im-bridge page — WeChat iLink bot (login + 调试收发)             *
/* ------------------------------------------------------------------ *
 *  im-bridge page — WeChat iLink bot (login + 调试收发)             *
 *                                                                    *
 *  调试收发 把发送简化为"发给本机 user_id" —— 即自动用 bot 自己登录      *
 *  后的 user_id 作为收件人，验证 round-trip。不暴露输入框也避免用户       *
 *  手动输入尚未建立 context_token 的陌生 user_id 后报 NoContextError。 *
 * ------------------------------------------------------------------ */


TTS.init();

// ------------------------------------------------------------------ *
// 应用向导模式 API — wizard 框架 finish 时调用
// 契约:opts 由 wizard.json 的 steps[].fields[].ui_key 决定:
//   tts-text     → opts.text
//   tts-play-to  → opts.playTo("browser" | "device" | "both")
// 文本非空 → 写回 #tts-text,然后**启动合成**(复用本文件 TTS.start()
// 的 payload 装配 + play_to 逻辑,不重写一遍)。
// (不再模拟点按钮 —— 理由见下面 WizardStart 那段注释)
// 合成完成后自动试听逻辑同样复用,不重写 payload 装配。
// 播放目标变更 → 同步勾选对应 radio(应用页面 onStart 内部读
// `input[name=tts-play-to]:checked`)。
// 全局名约定 ImApp_<app.id 去掉 hyphen>。
// ------------------------------------------------------------------ *

// 向导启动前的「能力就绪」探测 —— **直接问后端快照**,不等卡片自己的
// 5s 轮询那一拍。为什么不能靠卡片快照:快照里的 capability_ready 是
// 「registry 里有没有 running 的 TTS 实例」,模型刚就绪的那 1~5s 里
// 卡片还拿着旧快照 → capability_ready=false → 启动按钮是 disabled。
// 而 **disabled 按钮的 click() 被浏览器静默丢弃**(不报错、不打日志、
// 不触发 handler)—— 向导显示「完成配置」、语音却根本没生成
// (2026-10-23 实测:第一次跑完向导没出声,模型加载完再跑一遍就好了)。
// 详见宿主 core/wizard-start.js 的文件头。
async function ttsCapabilitySnapshot() {
  const r = await fetch(API_PREFIX + "/tts", { cache: "no-store" });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return await r.json();
}

// 调宿主共享的启动器(core/wizard-start.js)。宿主版本太老、没有这个
// 模块时给一句人话错误,而不是 "Cannot read properties of undefined"。
function _wizardStart(opts) {
  const ws = window.WizardStart;
  if (!ws || typeof ws.run !== "function") {
    throw new Error("WizardSnapshotError:helper_missing:宿主缺少 /static/core/wizard-start.js,请更新 wdnwebui 后重试");
  }
  return ws.run(opts);
}

window.ImApp_tts = window.ImApp_tts || {};
window.ImApp_tts.applyWizardSnapshot = async function (opts) {
  const result = { text_filled: false, started: false };
  if (opts.text) {
    const textEl = document.getElementById("tts-text");
    if (textEl) textEl.value = opts.text;
    result.text_filled = true;
  }
  if (opts.playTo) {
    document.querySelectorAll('input[name="tts-play-to"]').forEach((r) => {
      r.checked = (r.value === opts.playTo);
    });
  }
  // 文本非空 → 启动合成。**不能 btn.click()**:启动按钮的 disabled 由
  // 卡片快照的 capability_ready 决定,模型刚就绪时按钮还是灰的,而
  // disabled 按钮的 click() 会被浏览器**静默丢弃** → 向导显示「完成
  // 配置」、语音却没生成(2026-10-23 实测)。改成宿主共享的 WizardStart:
  // 先等能力真就绪(直接问后端),再直接调 TTS.start();等不到就抛错让
  // 向导显示「重新开启模型」。
  if (opts.text) {
    await _wizardStart({
      label: "语音合成",
      probe: ttsCapabilitySnapshot,
      // 就绪判据:后端说这个能力有 running 的实例(和 get_session 同口径)
      isReady: (s) => !!(s && s.available !== false && s.capability_ready === true),
      // 启动判据:服务已进入 loading/running(= 合成真的开跑了)
      isStarted: (s) => !!(s && (s.busy === true
        || s.state === "loading" || s.state === "running")),
      start: async () => {
        await TTS.start();
      },
      // 模型刚起来时 registry 可能还在收敛,给足等待再判定失败
      timeoutMs: 30000,
    });
    result.started = true;
  }
  return result;
};

})();
