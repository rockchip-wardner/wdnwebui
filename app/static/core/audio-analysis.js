/* audio-analysis —— 音轨分析面板的共享实现。
 *
 * 原来这段逻辑在 vd 卡和 audio_analysis 卡各有一份拷贝，两份必然漂（多路
 * 改造只改一份，另一份就退回「只分析第 1 路」）。抽成 core 模块后只有一份
 * 实现，调用方只提供「源从哪来」。
 *
 * 音轨分析吃**全部 N 路**（1~32），跟视频源同一份列表。文件源按顺序扫完
 * 整条音轨后该路完成；RTSP 源一直轮转直到用户停止。
 */
(function () {
  "use strict";

  const DEFAULT_IDS = {
    start:       "vd-audio-start",
    stop:        "vd-audio-stop",
    clear:       "vd-audio-events-clear",
    prompt:      "vd-audio-prompt",
    modelPick:   "vd-audio-model-pick",
    source:      "vd-audio-source",
    status:      "vd-audio-status",
    progress:    "vd-audio-progress",
    events:      "vd-audio-events",
    eventsCount: "vd-audio-events-count",
  };

  const CAPABILITY = "swcap_audio_understand";

  // 同一套 DOM id 只允许挂**一个**实例。重复挂载不是「多画一遍」，而是
  // **两套结果在同一个框里横跳**：每个实例各开一条 WebSocket、各自维护
  // 一份事件列表，每收到一条事件帧就把 `<ol id="vd-audio-events">` 整个
  // innerHTML 重画一次，两个实例轮流覆盖。而「分析音频」按钮只有**最后挂载**
  // 的实例拥有 onclick，也只有它的 onStart 会清空本地列表；另一个实例的
  // 列表留着**上一轮**的旧事件继续 append —— DOM 于是就在「本轮 N 条」和
  // 「上一轮 180+ 条」之间来回跳（实测复现）。
  //
  // 触发路径：hidden 的 audio_analysis 卡片也在 boot 时被 framework 的
  // _preloadCardScripts() 注入执行，而它模板里没有音轨面板，mount 时就
  // 解析到了 vd 卡那个同名元素 → 两个实例（已修，该卡不再 mount）。
  //
  // 键用 id 字符串而不是元素对象：元素被重新渲染后换了对象，但旧实例通过
  // document.getElementById 动态解析、仍会写进新元素。
  const _instances = new Map();

  function fmtTime(s) {
    if (s == null || isNaN(s)) return "--:--";
    const m = Math.floor(s / 60);
    const r = Math.round(s % 60);
    return `${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
  }

  function mount(opts) {
    const o = opts || {};
    const apiPrefix = o.apiPrefix || "/api";
    const ids = Object.assign({}, DEFAULT_IDS, o.ids || {});
    // 幂等保护：同一套 id 已挂过就复用旧实例，不再开第二条 WebSocket。
    const instKey = ids.events || "vd-audio-events";
    if (_instances.has(instKey)) {
      console.warn("[audio-analysis] 面板", instKey,
                  "已挂载，忽略重复 mount（否则事件框会出现两套结果横跳）");
      return _instances.get(instKey);
    }
    const getSources = o.getSources || (() => []);
    const onSnapshot = o.onSnapshot || null;
    const el = (k) => document.getElementById(ids[k]);
    const sanitize = window.sanitizeEventText || ((s) => s || "");

    const ctl = {
      ws: null,
      wsRetry: 0,
      wsTimer: null,
      events: [],
      snap: null,
    };

    // ---- 状态渲染 ---------------------------------------------------- //
    function renderMeta(snap) {
      ctl.snap = snap || null;
      if (onSnapshot) onSnapshot(ctl.snap);

      const srcEl = el("source");
      if (srcEl) {
        // 源标签**实时读** getSources()，不靠快照里的 channels —— 快照
        // 只在分析运行时才有路数，而用户是先填源再点分析，填完就该看到
        // 「N 路」，不能等到分析开始才更新。
        const list = (getSources() || []).filter(Boolean);
        if (list.length > 1) {
          srcEl.textContent = `${list.length} 路（视频源全部）`;
        } else {
          srcEl.textContent = list[0] || (snap && snap.source) || "—";
        }
      }
      const statusEl = el("status");
      if (statusEl) {
        const s = snap && snap.state;
        if (s === "extracting") {
          const [done, total] = (snap.progress || [0, 0]);
          const n = (snap && snap.channels) || 0;
          const cur = (snap && snap.current >= 0) ? snap.current + 1 : 0;
          statusEl.textContent = `分析中 ${done}/${total} 段`
            + (n > 1 ? ` · 第 ${snap.round || 1} 轮 · 通道 ${cur}/${n}` : "");
        } else if (s === "done") {
          statusEl.textContent = `完成 · ${snap.events_count || 0} 段`;
        } else if (s === "stopped") {
          statusEl.textContent = "已停止";
        } else if (s === "error") {
          statusEl.textContent = "错误: " + (snap.error || "");
        } else {
          statusEl.textContent = (snap && snap.available === false)
            ? "功能未启用"
            : "未启动";
        }
      }
      const startBtn = el("start");
      const stopBtn = el("stop");
      if (startBtn) startBtn.disabled = !!(snap && snap.state === "extracting");
      if (stopBtn) stopBtn.disabled = !(snap && snap.state === "extracting");
      const progEl = el("progress");
      if (progEl) {
        if (snap && snap.state === "extracting") {
          const [done, total] = (snap.progress || [0, 0]);
          const per = (snap && snap.per_channel) || [];
          const doneCh = per.filter((c) => c.done).length;
          let txt = total > 0
            ? `已处理 ${done} / ${total} 段（每段 ≤ 6 秒）`
            : "";
          if (per.length > 1) txt += ` · ${doneCh}/${per.length} 路已完成`;
          progEl.textContent = txt;
        } else {
          progEl.textContent = "";
        }
      }
    }

    function renderEvents() {
      const ol = el("events");
      if (!ol) return;
      if (!ctl.events.length) {
        ol.innerHTML = '<li class="vd-audio-empty">启动音频分析后，按 6 秒切片逐段推理，结果显示在这里。</li>';
        return;
      }
      ol.innerHTML = "";
      ctl.events.forEach((ev) => {
        const li = document.createElement("li");
        li.className = "vd-audio-ev"
          + (ev.level === "error" ? " vd-audio-ev--err" : "");
        const ts = document.createElement("span");
        ts.className = "vd-audio-ts";
        ts.textContent = `[${fmtTime(ev.start_s)}–${fmtTime(ev.end_s)}]`;
        // 多路下「哪一路」是定位结果的必要上下文 —— 不标通道，32 路的事件
        // 列表没法读。1 基显示，与视频检测事件一致。
        const ch = document.createElement("span");
        ch.className = "vd-audio-ch";
        ch.textContent = "通道 " + ((ev.channel || 0) + 1);
        const tx = document.createElement("span");
        tx.className = "vd-audio-text";
        tx.textContent = sanitize(ev.text || "");
        li.append(ts, ch, tx);
        ol.appendChild(li);
      });
      const cntEl = el("eventsCount");
      if (cntEl) cntEl.textContent = `${ctl.events.length} 段`;
    }

    // ---- 数据 -------------------------------------------------------- //
    async function refresh() {
      try {
        const r = await fetch(apiPrefix + "/audio-analysis", { cache: "no-store" });
        if (!r.ok) return null;
        const snap = await r.json();
        renderMeta(snap);
        const pick = el("modelPick");
        if (pick && pick.disabled && window.loadModelPick) {
          window.loadModelPick(ids.modelPick, CAPABILITY, "vd.audio.model_id");
        }
        return snap;
      } catch (_) { return null; }
    }

    async function hydrateEvents() {
      try {
        const r = await fetch(apiPrefix + "/audio-analysis/events",
                              { cache: "no-store" });
        if (!r.ok) return;
        const data = await r.json();
        ctl.events = Array.isArray(data.events) ? data.events.slice() : [];
        renderEvents();
      } catch (_) { /* best-effort */ }
    }

    async function onStart() {
      const sources = (getSources() || []).filter(Boolean);
      const prompt = (el("prompt")?.value || "").trim();
      if (!sources.length) {
        alert("请先在上方视频源里填至少一路视频源");
        return;
      }
      if (!prompt) { alert("请输入音频理解提示词"); return; }
      ctl.events = [];
      renderEvents();
      try {
        const model_id = (el("modelPick")?.value || "") || null;
        const r = await fetch(apiPrefix + "/audio-analysis/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ sources, prompt, model_id }),
        });
        if (!r.ok) {
          const e = await r.json().catch(() => ({}));
          alert(`启动音频分析失败: ${e.detail || r.status}`);
          return;
        }
        renderMeta(await r.json());
      } catch (e) {
        alert("启动音频分析失败: " + e.message);
      }
    }

    async function onStop() {
      try {
        await fetch(apiPrefix + "/audio-analysis/stop", { method: "POST" });
      } catch (_) { /* best-effort */ }
      refresh();
    }

    async function onClear() {
      try {
        await fetch(apiPrefix + "/audio-analysis/events/clear", { method: "POST" });
      } catch (_) { /* best-effort */ }
      ctl.events = [];
      renderEvents();
    }

    // ---- WS ---------------------------------------------------------- //
    function ensureWs() {
      if (ctl.ws && ctl.ws.readyState <= 1) return;
      ctl.wsRetry = Math.min(ctl.wsRetry + 1, 6);
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      const url = `${proto}//${location.host}${apiPrefix}/audio-analysis/ws`;
      try {
        ctl.ws = new WebSocket(url);
      } catch (_) {
        ctl.wsTimer = setTimeout(ensureWs,
          Math.min(1000 * (1 << (ctl.wsRetry - 1)), 30000));
        return;
      }
      ctl.ws.onmessage = (ev) => onWsMessage(ev);
      ctl.ws.onclose = () => {
        ctl.ws = null;
        const wait = Math.min(1000 * (1 << (ctl.wsRetry - 1)), 30000);
        ctl.wsTimer = setTimeout(ensureWs, wait);
      };
      ctl.ws.onerror = () => { try { ctl.ws && ctl.ws.close(); } catch (_) {} };
    }

    function onWsMessage(ev) {
      let m;
      try { m = JSON.parse(ev.data); } catch (_) { return; }
      if (m.type === "hello") {
        hydrateEvents();
        renderMeta(m);
      } else if (m.type === "state") {
        renderMeta(m);
        if (m.state === "done" || m.state === "error" || m.state === "stopped") {
          hydrateEvents();
        }
      } else if (m.type === "progress") {
        // progress 帧只带增量字段，其余从上一份快照继承 —— 整份重建会丢掉
        // per_channel（每路进度），状态栏就只剩总数。
        const prev = ctl.snap || {};
        renderMeta(Object.assign({}, prev, {
          state: prev.state || "extracting",
          progress: [m.done, m.total],
          events_count: ctl.events.length,
          current: m.current != null ? m.current : prev.current,
          round: m.round != null ? m.round : prev.round,
          // 路数优先用 progress 帧自带的：state 帧可能在 open 之前就发过
          // （那时 channels=0），只靠继承会把 0 一直传下去。
          channels: m.channels != null ? m.channels
                     : (prev.channels != null ? prev.channels : 0),
          per_channel: prev.per_channel || [],
        }));
      } else if (m.type === "event" && m.event) {
        ctl.events.push(m.event);
        renderEvents();
        const cntEl = el("eventsCount");
        if (cntEl) cntEl.textContent = `${ctl.events.length} 段`;
      }
    }

    // ---- 装配 -------------------------------------------------------- //
    const startBtn = el("start");
    const stopBtn = el("stop");
    const clearBtn = el("clear");
    if (startBtn) startBtn.onclick = () => onStart();
    if (stopBtn) stopBtn.onclick = () => onStop();
    if (clearBtn) clearBtn.onclick = () => onClear();
    ensureWs();
    refresh();

    ctl.refresh = refresh;
    ctl.stop = onStop;
    ctl.clear = onClear;
    ctl.start = onStart;
    // 源列表变了 → 只重画源标签。不走 refresh()：那是拉后端快照，
    // 源框每敲一个字都发一次 GET 太浪费。
    ctl.refreshSource = () => { renderMeta(ctl.snap); };
    _instances.set(instKey, ctl);
    return ctl;
  }

  window.AudioAnalysisCore = { mount };
})();
