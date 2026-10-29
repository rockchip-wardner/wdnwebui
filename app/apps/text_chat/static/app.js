(function () {
  "use strict";
  const API_PREFIX = "/api";

const TextChat = {
  ws: null,
  wsRetry: 0,
  wsTimer: null,
  snapshot: null,
  _sending: false,          // in-flight 防抖：fetch 完成前忽略重复 send()
  pending: new Map(),       // request_id -> {userLi, assistantLi, thinkingEl, answerEl, errorEl, cursorEl}
  messages: [],             // local history mirror: [{role:"user"|"assistant", content, ts}]
  _historyLoaded: false,    // backend history has been rendered into the DOM

  init() {
    if (this._inited) return;
    this._inited = true;
    const $ = (id) => document.getElementById(id);
    $("text-chat-send-btn")?.addEventListener("click", () => this.send());
    $("text-chat-stop-btn")?.addEventListener("click", () => this.stop());
    $("text-chat-reset-btn")?.addEventListener("click", () => this.reset());
    $("text-chat-input")?.addEventListener("keydown", (e) => {
      // Cmd/Ctrl + Enter sends; plain Enter inserts a newline.
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        this.send();
      }
    });
    // v0.4 修复：init 时主动开 WS。之前漏调,导致 backend push 的
    // user_echo / thinking / answer 消息全部丢失 —— 用户发送后 DOM
    // 永远不出现 user / assistant 气泡(典型症状:"输入问题,点发送
    // 没有反应")。其他系统页 system.js 的 loader 会再调一次
    // ensureWs(),内部有 readyState 守卫,重复调用安全。
    const thinkEl = $("text-chat-thinking");
    if (thinkEl) {
      try {
        thinkEl.checked = localStorage.getItem("text_chat_thinking") === "1";
      } catch (_) { /* private mode */ }
      thinkEl.addEventListener("change", () => {
        try {
          localStorage.setItem("text_chat_thinking",
                               thinkEl.checked ? "1" : "0");
        } catch (_) { /* ignore */ }
      });
    }
    // 能力探测：用户去算力页启用模型后回来要自动恢复，不能等发一条
    // 失败的消息才轮询。页面可见时每 5s 复查一次。
    this._probeCap().catch(() => {});
    if (!this._capTimer) {
      this._capTimer = setInterval(() => {
        if (document.visibilityState !== "visible") return;
        this._probeCap().catch(() => {});
      }, 5000);
    }
    this.ensureWs();
    this.loadHistory().catch(() => {});
  },

  async loadHistory(force = false) {
    if (this._historyLoaded && !force) return;
    let data;
    try {
      const r = await fetch(API_PREFIX + "/text-chat/history", { cache: "no-store" });
      if (!r.ok) return;
      data = await r.json();
    } catch (_) {
      return;
    }
    this._historyLoaded = true;
    this._renderHistory((data && data.messages) || []);
  },

  _renderHistory(messages) {
    const ul = document.getElementById("text-chat-conv-ul");
    const empty = document.getElementById("text-chat-empty");
    if (!ul) return;
    ul.innerHTML = "";
    this.pending.clear();
    this.messages = [];
    if (!messages.length) {
      if (empty) empty.style.display = "";
      return;
    }
    if (empty) empty.style.display = "none";
    for (const msg of messages) {
      const role = msg.role === "assistant" ? "assistant" : "user";
      const li = document.createElement("li");
      li.className = "imb-msg " + (role === "user" ? "imb-msg--out" : "imb-msg--in");
      li.dataset.role = role;
      const ts = this._fmtTime(Number(msg.ts || 0) * 1000);
      const who = role === "user" ? "你" : "模型";
      li.innerHTML = `
        <span class="imb-msg-meta">${who}${ts ? " · " + ts : ""}</span>
        <div class="imb-msg-body">${this._esc(msg.content || "")}</div>
      `;
      ul.appendChild(li);
      this._recordMessage(role, msg.content || "", Number(msg.ts || 0) * 1000);
    }
    this._scrollToBottom();
  },

  async refresh() {
    const target = document.getElementById("data-text-chat");
    try {
      const r = await fetch(API_PREFIX + "/text-chat");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const snap = await r.json();
      this._applySnapshot(snap);
      if (target) target.textContent = JSON.stringify(snap, null, 2);
    } catch (err) {
      if (target) target.textContent = "刷新失败: " + err.message;
    }
  },

  ensureWs() {
    if (this.ws && (this.ws.readyState === WebSocket.OPEN ||
                    this.ws.readyState === WebSocket.CONNECTING)) {
      return;
    }
    this._openWs();
  },

  closeWs() {
    if (this.wsTimer) {
      clearTimeout(this.wsTimer);
      this.wsTimer = null;
    }
    if (this.ws) {
      try { this.ws.close(); } catch (_) { /* ignore */ }
      this.ws = null;
    }
    this.wsRetry = 0;
    // 关掉 stale capability poll —— 见 _maybeStartCapPoll。该 timer
    // 只在 snapshot.error 含 "功能未启用" 时跑,正常时是 null。
    if (this._capPollTimer) {
      clearInterval(this._capPollTimer);
      this._capPollTimer = null;
    }
    this._activeThinking = undefined;
  },

  _openWs() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = proto + "//" + location.host + API_PREFIX + "/text-chat/ws";
    let ws;
    try {
      ws = new WebSocket(url);
    } catch (err) {
      this._scheduleReconnect();
      return;
    }
    this.ws = ws;
    ws.onopen = () => { this.wsRetry = 0; };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (_) { return; }
      this._onWsMessage(msg);
    };
    ws.onclose = () => { this.ws = null; this._scheduleReconnect(); };
    ws.onerror = () => { /* onclose fires next */ };
  },

  _scheduleReconnect() {
    if (this.wsTimer) return;
    this.wsRetry = Math.min(this.wsRetry + 1, 10);
    const delay = Math.min(1000 * (1 << (this.wsRetry - 1)), 30000);
    this.wsTimer = setTimeout(() => {
      this.wsTimer = null;
      this._openWs();
    }, delay);
  },

  _onWsMessage(msg) {
    if (!msg || !msg.type) return;
    if (msg.type === "snapshot") {
      this._applySnapshot(msg.snapshot);
      return;
    }
    if (msg.type === "state") {
      this._applySnapshot(msg.state || {});
      return;
    }
    if (msg.type === "user_echo") {
      this._appendUserBubble(msg.request_id, msg.text || "");
      return;
    }
    if (msg.type === "thinking") {
      this._appendThinkingDelta(msg.request_id, msg.delta || "");
      return;
    }
    if (msg.type === "answer") {
      this._appendAnswerDelta(msg.request_id, msg.delta || "");
      return;
    }
    if (msg.type === "done") {
      this._finalizeBubble(msg.request_id, {
        thinking: msg.thinking || "",
        answer: msg.answer || "",
        status: "done",
        elapsed_sec: msg.elapsed_sec,
        tool_call: msg.tool_call,
      });
      this._recordMessage("user",      msg.answer ? "" : "", 0);   // user 已 echo
      if (msg.answer) {
        this._recordMessage("assistant", msg.answer, Date.now());
      }
      return;
    }
    if (msg.type === "stopped") {
      this._finalizeBubble(msg.request_id, {
        thinking: msg.thinking || "",
        answer: msg.answer || "",
        status: "stopped",
        elapsed_sec: msg.elapsed_sec,
      });
      return;
    }
    if (msg.type === "error") {
      this._showInlineError(msg.request_id, msg.detail || "未知错误");
      return;
    }
  },

  _applySnapshot(snap) {
    if (!snap) return;
    this.snapshot = Object.assign({}, this.snapshot || {}, snap);
    if (typeof DemoGuard !== "undefined") {
      DemoGuard.report("text-chat", { session: this.snapshot });
    }
    this._renderState();
    // 能力探测由 init() 的常驻轮询负责（页面可见时每 5s），见 _probeCap。
  },

  async _probeCap() {
    let caps;
    try {
      const r = await fetch(API_PREFIX + "/compute/capabilities", { cache: "no-store" });
      if (!r.ok) return;
      const data = await r.json();
      caps = (data && data.sw_capabilities) || [];
    } catch (_) {
      return;
    }
    // 仅当 ComputeRegistry 同时声明了 swcap_text_chat **且已绑定
    // NPU**(npu_index >= 0)时才认为可用 —— npu_index = -1 表示
    // peer 远端 / adopted 但未 load,get_session 仍会抛 RuntimeError,
    // 不应该清 sticky error 让用户误以为可发。
    const hasTextChat = caps.some(
      (c) => c && c.type === "swcap_text_chat" && Number(c.npu_index) >= 0
    );
    const changed = this._capReady !== hasTextChat;
    this._capReady = hasTextChat;
    if (hasTextChat && this.snapshot && /功能未启用/.test(this.snapshot.error || "")) {
      // ComputeRegistry 已确认 swcap_text_chat 可用,但 text-chat
      // service 内部的 snapshot.error 是上次失败留下的"旧账"。本地
      // 清掉,banner 与 hint 立即恢复"就绪"态,用户无需手动刷页。
      this.snapshot.error = "";
    }
    if (changed) this._renderState();
  },

  _renderState() {
    const s = this.snapshot || {};
    const state = s.state || "idle";
    const busy = !!s.busy;

    const badge = document.getElementById("text-chat-state-badge");
    if (badge) {
      const labelMap = {
        idle:    "空闲",
        loading: "排队中",
        running: "生成中",
        done:    "完成",
        error:   "错误",
        stopped: "已停止",
      };
      badge.textContent = labelMap[state] || state;
      badge.dataset.state = state;
    }

    const statusLine = document.getElementById("text-chat-status-line");
    if (statusLine) {
      if (s.error) statusLine.textContent = state === "error" ? "错误" : state;
      else statusLine.textContent = ({
        idle:    "空闲",
        loading: "排队中",
        running: "正在生成",
        done:    "最近一轮已完成",
        error:   "出错",
        stopped: "已停止",
      })[state] || state;
    }

    const tokens = document.getElementById("text-chat-tokens");
    if (tokens) {
      const ht = s.history_tokens || 0;
      const limit = Number(s.context_limit || 0);
      const maxNew = Number(s.max_new_tokens || 0);
      tokens.textContent = limit > 0
        ? `${ht} / ${limit} tokens（Max tokens ${maxNew}）`
        : "模型未就绪";
    }

    const sendBtn = document.getElementById("text-chat-send-btn");
    const stopBtn = document.getElementById("text-chat-stop-btn");
    const input   = document.getElementById("text-chat-input");
    const capReady = this._capReady;          // undefined = 首次探测未回
    if (sendBtn) sendBtn.disabled = busy || capReady === false;
    if (input)   input.disabled = busy;
    if (stopBtn) stopBtn.disabled = !(state === "running" || state === "loading");

    const banner = document.getElementById("text-chat-unavailable");
    if (banner) {
      const isCapErr = state === "error" && s.error && /功能未启用/.test(s.error);
      // 能力缺失不再等用户点一次失败：探测一回来就显示 banner + 禁发送。
      banner.classList.toggle("hidden", !(isCapErr || capReady === false));
    }

    const hint = document.getElementById("text-chat-hint");
    if (hint) {
      if (s.error) {
        hint.textContent = s.error.length > 80 ? s.error.slice(0, 80) + "…" : s.error;
        hint.classList.add("danger");
      } else {
        hint.textContent = busy ? "模型正在生成…" : "就绪";
        hint.classList.remove("danger");
      }
    }

    const target = document.getElementById("data-text-chat");
    if (target) target.textContent = JSON.stringify(s, null, 2);
  },

  _appendUserBubble(reqId, text) {
    const ul = document.getElementById("text-chat-conv-ul");
    const empty = document.getElementById("text-chat-empty");
    if (empty) empty.style.display = "none";

    const ts = this._fmtTime(Date.now());
    const li = document.createElement("li");
    li.className = "imb-msg imb-msg--out";
    li.dataset.role = "user";
    li.innerHTML = `
      <span class="imb-msg-meta">你 · ${ts}</span>
      <div class="imb-msg-body">${this._esc(text)}</div>
    `;
    ul?.appendChild(li);
    this._scrollToBottom();
    this._recordMessage("user", text, Date.now());

    // initialize pending entry for this request_id (assistant bubble comes next)
    if (!this.pending.has(reqId)) {
      this.pending.set(reqId, { userLi: li, assistantLi: null });
    }
  },

  _ensureAssistantBubble(reqId) {
    let p = this.pending.get(reqId);
    if (p && p.assistantLi) return p;

    const ul = document.getElementById("text-chat-conv-ul");
    const ts = this._fmtTime(Date.now());

    const li = document.createElement("li");
    li.className = "imb-msg imb-msg--in";
    li.dataset.role = "assistant";
    // thinking 默认**折叠**(无 open 属性 → <details> 原生行为隐藏 body),
    // 用户点 summary 展开;流式累积也不再自动展开。
    li.innerHTML = `
      <span class="imb-msg-meta">模型 · ${ts}</span>
      <details class="text-chat-thinking" hidden>
        <summary>思考过程（点击展开）</summary>
        <div class="text-chat-thinking-body" data-role="thinking-body"></div>
      </details>
      <div class="imb-msg-body text-chat-answer" data-role="answer-body"></div>
      <div class="imb-msg-error text-chat-inline-error" data-role="inline-error" hidden></div>
    `;
    ul?.appendChild(li);

    p = Object.assign(p || {}, {
      assistantLi: li,
      thinkingEl: li.querySelector('[data-role="thinking-body"]'),
      answerEl:   li.querySelector('[data-role="answer-body"]'),
      errorEl:    li.querySelector('[data-role="inline-error"]'),
      thinkingBuf: "",
      answerBuf:   "",
    });
    this.pending.set(reqId, p);
    return p;
  },

  _thinkingHidden() {
    if (this._activeThinking !== undefined) {
      return this._activeThinking === false;
    }
    try { return localStorage.getItem("text_chat_thinking") !== "1"; }
    catch (_) { return false; }
  },

  _appendThinkingDelta(reqId, delta) {
    // 关闭思考时后端仍可能返回模型自发的 <think> 内容（Qwen3.5 在
    // enable_thinking=False 下对开放式输入会写英文 Thinking Process）——
    // 前端兜底：OFF 时直接丢弃，不创建/不更新思考块。
    if (this._thinkingHidden()) return;
    const p = this._ensureAssistantBubble(reqId);
    if (!p || !p.thinkingEl) return;
    p.thinkingBuf += delta;
    p.thinkingEl.textContent = p.thinkingBuf;
    const details = p.assistantLi?.querySelector(".text-chat-thinking");
    if (details && p.thinkingBuf.trim()) details.hidden = false;
    // thinking 默认折叠 — 流式累积不自动展开,避免抢戏;
    // 用户想看时点 summary 展开即可。
    this._scrollToBottom();
  },

  _appendAnswerDelta(reqId, delta) {
    const p = this._ensureAssistantBubble(reqId);
    if (!p || !p.answerEl) return;
    p.answerBuf += delta;
    // render with blinking cursor at end
    p.answerEl.textContent = p.answerBuf;
    p.answerEl.classList.add("text-chat-cursor");
    this._scrollToBottom();
  },

  _finalizeBubble(reqId, info) {
    let p = this.pending.get(reqId);
    if (!p) {
      // No streaming delta ever arrived (e.g. worker produced no live
      // tokens); create the bubble now so the done/stopped frame can
      // still render the full reply instead of being dropped.
      this._ensureAssistantBubble(reqId);
      p = this.pending.get(reqId);
    }
    if (!p || !p.assistantLi) return;

    // remove cursor
    p.answerEl?.classList.remove("text-chat-cursor");

    // prefer the cleaner text from done frame (regex split) over the streamed buffer
    // 关闭思考时后端仍可能带回模型自发的思考段 —— 兜底置空，details 随之隐藏。
    if (this._thinkingHidden() && typeof info.thinking === "string") {
      info.thinking = "";
    }
    if (typeof info.thinking === "string") {
      p.thinkingBuf = info.thinking;
      if (p.thinkingEl) p.thinkingEl.textContent = info.thinking;
      const details = p.assistantLi.querySelector(".text-chat-thinking");
      if (details) {
        // 默认折叠；没有思考内容时整块隐藏（空 <think></think> 不显示）。
        details.hidden = !info.thinking.trim();
      }
    }
    if (typeof info.answer === "string") {
      p.answerBuf = info.answer;
      if (p.answerEl) p.answerEl.textContent = info.answer;
    }

    // append stopped marker
    if (info.status === "stopped") {
      const meta = p.assistantLi.querySelector(".imb-msg-meta");
      if (meta) {
        const span = document.createElement("span");
        span.className = "text-chat-stopped";
        span.textContent = "（已停止）";
        meta.appendChild(span);
      }
    }

    // tool-call badge: the agent executed at least one @Callable during
    // this turn (e.g. tts_alert / BeepTool). Show which one ran.
    if (info.tool_call) {
      const meta = p.assistantLi.querySelector(".imb-msg-meta");
      if (meta) {
        const span = document.createElement("span");
        span.className = "text-chat-tool-badge";
        span.textContent = `🔧 已调用 ${info.tool_call}`;
        span.title = "本轮对话通过工具调用完成";
        meta.appendChild(span);
      }
    }

    // append elapsed time tag
    if (typeof info.elapsed_sec === "number" && info.elapsed_sec > 0) {
      const meta = p.assistantLi.querySelector(".imb-msg-meta");
      if (meta) {
        const span = document.createElement("span");
        span.className = "text-chat-stopped";
        span.textContent = `· ${info.elapsed_sec.toFixed(1)}s`;
        meta.appendChild(span);
      }
    }

    this._activeThinking = undefined;
    this._scrollToBottom();
  },

  _showInlineError(reqId, detail) {
    // If we have an assistant bubble for this request, show error inside it;
    // otherwise create a generic red banner above the composer.
    const p = this.pending.get(reqId);
    if (p && p.assistantLi && p.errorEl) {
      p.errorEl.hidden = false;
      p.errorEl.textContent = detail;
    } else {
      const list = document.getElementById("text-chat-conv-list");
      if (!list) return;
      const banner = document.createElement("div");
      banner.className = "text-chat-error";
      banner.textContent = detail;
      list.appendChild(banner);
      this._scrollToBottom();
    }
  },

  _recordMessage(role, content, ts) {
    if (!content) return;
    this.messages.push({ role, content, ts: ts || Date.now() });
    if (this.messages.length > 200) this.messages.length = 200;
  },

  _scrollToBottom() {
    const list = document.getElementById("text-chat-conv-list");
    if (!list) return;
    // only auto-scroll if user is near the bottom (avoid fighting scroll position)
    const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 80;
    if (nearBottom) list.scrollTop = list.scrollHeight;
  },

  async send() {
    const input = document.getElementById("text-chat-input");
    const text = (input?.value || "").trim();
    if (!text) return;
    if (this._sending) return;   // 双击 / Cmd+Enter+按钮 同时触发的防抖
    if (this.snapshot && this.snapshot.busy) {
      this._setHint("上一条还在生成中…", true);
      return;
    }
    const enabled = !!document.getElementById("text-chat-thinking")?.checked;
    this._activeThinking = enabled;   // 本轮（含工具轮）按发送时的开关决定是否展示思考
    this._sending = true;
    try {
      const r = await fetch(API_PREFIX + "/text-chat/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, enable_thinking: enabled }),
      });
      if (!r.ok) {
        let detail = "HTTP " + r.status;
        let body = null;
        try {
          body = await r.json();
          if (body && body.detail) {
            detail = typeof body.detail === "string"
              ? body.detail : JSON.stringify(body.detail);
          }
        } catch (_) { /* ignore */ }
        // 409 等场景：服务端附带最新 snapshot，用它纠正本地 busy/按钮态
        throw Object.assign(new Error(detail), { respBody: body });
      }
      if (input) input.value = "";
      this._setHint("已发送，等待模型响应…", false);
      // snapshot is updated by WS state frame
    } catch (err) {
      this._setHint("发送失败: " + err.message, true);
      if (err.respBody && err.respBody.snapshot) {
        this._applySnapshot(err.respBody.snapshot);
      }
    } finally {
      this._sending = false;
    }
  },

  async stop() {
    this._setHint("正在停止…", false);
    try {
      const r = await fetch(API_PREFIX + "/text-chat/stop", { method: "POST" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      this._setHint("已请求停止", false);
    } catch (err) {
      this._setHint("停止失败: " + err.message, true);
    }
  },

  async _getSnapshot() {
    const r = await fetch(API_PREFIX + "/text-chat", { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    return await r.json();
  },

  _clearConversation() {
    const ul = document.getElementById("text-chat-conv-ul");
    if (ul) ul.innerHTML = "";
    const empty = document.getElementById("text-chat-empty");
    if (empty) empty.style.display = "";
    this.pending.clear();
    this._activeThinking = undefined;
    this.messages = [];
    this._historyLoaded = true;
  },

  async resetForWizard() {
    // Wizard completion starts a fresh conversation. If the previous turn
    // is still running, stop it and wait for the backend to leave busy
    // before clearing history; otherwise the old _run_one could append its
    // result after reset and resurrect the old context.
    let snap = await this._getSnapshot();
    if (snap && snap.busy) {
      await fetch(API_PREFIX + "/text-chat/stop", { method: "POST" });
      const deadline = Date.now() + 70000;
      while (Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 500));
        try { snap = await this._getSnapshot(); } catch (_) { continue; }
        if (!snap.busy) break;
      }
      if (snap && snap.busy) {
        throw new Error("上一轮仍在生成，无法清空对话，请稍后再试");
      }
    }
    const r = await fetch(API_PREFIX + "/text-chat/reset", { method: "POST" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const body = await r.json().catch(() => ({}));
    if (body.snapshot) this._applySnapshot(body.snapshot);
    this._clearConversation();
    this._setHint("已清空", false);
    return body.snapshot || null;
  },

  async reset() {
    if (!confirm("确认清空对话历史？")) return;
    try {
      await this.resetForWizard();
    } catch (err) {
      this._setHint("清空失败: " + err.message, true);
    }
  },

  _setHint(text, danger) {
    const hint = document.getElementById("text-chat-hint");
    if (!hint) return;
    hint.textContent = text;
    hint.classList.toggle("danger", !!danger);
  },

  _fmtTime(ms) {
    if (!ms) return "";
    const d = new Date(ms);
    const pad = (n) => (n < 10 ? "0" + n : "" + n);
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  },

  _esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  },
};

TextChat.init();

// ------------------------------------------------------------------ *
// 应用向导模式 API — wizard 框架 finish 时调用
// 契约:opts 由 wizard.json 的 steps[].fields[].ui_key 决定:
//   tc-prompt  → opts.prompt
// 空字段不触发对应动作(wizard 框架已校验);prompt 非空时把内容
// 写回 #text-chat-input 并**自动发送**——向导完成 = 用户已经明确要开始
// 对话（首次提问就是向导里填的）。原实现只预填、不发送，用户实测要求改。
// 全局名约定 ImApp_<app.id 去掉 hyphen>—— wizard 按此查找。
// ------------------------------------------------------------------ *
window.ImApp_text_chat = window.ImApp_text_chat || {};
window.ImApp_text_chat.applyWizardSnapshot = async function (opts) {
  const result = { prompt_filled: false, sent: false };
  // 向导完成 = 开始一轮全新的文本对话；不能把上一轮未清空的历史带进来。
  await TextChat.resetForWizard();
  // 向导的「深度思考」选择 → 同步页面开关 + localStorage（页面级持久），
  // 让随后的 send() 按它决定 enable_thinking。向导默认关；未传也显式置关，
  // 避免主界面上次开启的残留影响向导结果。
  const wantThinking = opts.enableThinking === true;
  const thinkingCb = document.getElementById("text-chat-thinking");
  if (thinkingCb) thinkingCb.checked = wantThinking;
  try {
    localStorage.setItem("text_chat_thinking", wantThinking ? "1" : "0");
  } catch (_) { /* private mode */ }
  if (opts.prompt) {
    const input = document.getElementById("text-chat-input");
    if (input) {
      input.value = opts.prompt;
      input.dispatchEvent(new Event("input", { bubbles: true }));
      result.prompt_filled = true;
    }
    // 与「发送」按钮同一条路径；send() 内部已处理空文本 / busy / 网络错误，
    // 不会把异常抛回向导（避免 finish 被回退到上一步）。
    try {
      await TextChat.send();
      result.sent = true;
    } catch (_) { /* send() 已给出页面提示 */ }
  }
  return result;
};

})();
