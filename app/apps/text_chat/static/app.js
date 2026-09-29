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

  init() {
    if (this._inited) return;
    this._inited = true;
    const $ = (id) => document.getElementById(id);
    $("text-chat-send-btn")?.addEventListener("click", () => this.send());
    $("text-chat-stop-btn")?.addEventListener("click", () => this.stop());
    $("text-chat-reset-btn")?.addEventListener("click", () => this.reset());
    $("text-chat-refresh")?.addEventListener("click", () => this.refresh());
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
    this.ensureWs();
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
    // v0.4 修复：text-chat 在 capability 缺失时不广播给前端 WS,导致
    // 用户在「算力配置」页启用 qwen3-4b 后回到 text-chat,sticky 的
    // snapshot.error = "功能未启用: swcap_text_chat" 还在 hint 上显示
    // —— 必须用户主动点"刷新"或发条消息才会纠正。装一个 poll,只
    // 在 error 匹配 "功能未启用" 时启动,snapshot 转好自动停。
    this._maybeStartCapPoll();
  },

  _maybeStartCapPoll() {
    const err = (this.snapshot && this.snapshot.error) || "";
    const needPoll = /功能未启用/.test(err);
    if (needPoll && !this._capPollTimer) {
      this._capPollTimer = setInterval(() => {
        // 只调 _probeCap —— 不调 refresh()。原因:
        // text-chat service 内部的 snapshot.error 是上次 _run_one 失败
        // 留下的"旧账",只有下一次成功 _run_one 才会清。refresh() 拉
        // 回来的快照 Object.assign 到 this.snapshot 后,error 又被写
        // 回前端 —— 会和 _probeCap 清掉的动作来回反复。绕过 refresh()
        // 直接走 capabilities 这个"独立信源",确认 ComputeRegistry 已
        // 暴露 swcap_text_chat 后本地清掉 sticky error。
        this._probeCap().catch(() => {});
      }, 3000);
    } else if (!needPoll && this._capPollTimer) {
      clearInterval(this._capPollTimer);
      this._capPollTimer = null;
    }
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
    if (hasTextChat && this.snapshot && /功能未启用/.test(this.snapshot.error || "")) {
      // ComputeRegistry 已确认 swcap_text_chat 可用,但 text-chat
      // service 内部的 snapshot.error 是上次失败留下的"旧账"。本地
      // 清掉,banner 与 hint 立即恢复"就绪"态,用户无需手动刷页。
      this.snapshot.error = "";
      this._renderState();
    }
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
      const limit = s.context_limit || 16384;
      const maxNew = s.max_new_tokens || 2048;
      tokens.textContent = `${ht} / ${limit} tokens（预留 ${maxNew} 给输出）`;
    }

    const sendBtn = document.getElementById("text-chat-send-btn");
    const stopBtn = document.getElementById("text-chat-stop-btn");
    const input   = document.getElementById("text-chat-input");
    if (sendBtn) sendBtn.disabled = busy;
    if (input)   input.disabled = busy;
    if (stopBtn) stopBtn.disabled = !(state === "running" || state === "loading");

    const banner = document.getElementById("text-chat-unavailable");
    if (banner) {
      const isCapErr = state === "error" && s.error && /功能未启用/.test(s.error);
      banner.classList.toggle("hidden", !isCapErr);
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
      <details class="text-chat-thinking">
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

  _appendThinkingDelta(reqId, delta) {
    const p = this._ensureAssistantBubble(reqId);
    if (!p || !p.thinkingEl) return;
    p.thinkingBuf += delta;
    p.thinkingEl.textContent = p.thinkingBuf;
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
    if (typeof info.thinking === "string") {
      p.thinkingBuf = info.thinking;
      if (p.thinkingEl) p.thinkingEl.textContent = info.thinking;
      const details = p.assistantLi.querySelector(".text-chat-thinking");
      if (details) {
        // 默认折叠 — 即使 done frame 含 thinking 也不自动展开;
        // 没 thinking 时整个 details 隐藏,折叠与否无关紧要。
        if (!info.thinking) details.hidden = true;
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
    this._sending = true;
    try {
      const r = await fetch(API_PREFIX + "/text-chat/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
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

  async reset() {
    if (!confirm("确认清空对话历史？")) return;
    try {
      const r = await fetch(API_PREFIX + "/text-chat/reset", { method: "POST" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      // 清空本地 UI
      const ul = document.getElementById("text-chat-conv-ul");
      if (ul) ul.innerHTML = "";
      const empty = document.getElementById("text-chat-empty");
      if (empty) empty.style.display = "";
      this.pending.clear();
      this.messages = [];
      this._setHint("已清空", false);
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

/* ------------------------------------------------------------------ *
 *  video-content-search page（视频内容检索）                            *
 *                                                                    *
 *  后端本期为 mock（app/services/video_content_search.py）：extract   *
 *  只推进度、query 返回固定命中；后续按 vl_rag_demo 换真实实现时，    *
 *  本模块的归一化层（normalizeHits）和播放器帧源接口保持接缝。        *
 *                                                                    *
 *  协议字段对齐 demo 语义：hits = {video_path, start_time, end_time,  *
 *  chunk_idx, similarity}；query 参数 = {text, sim_threshold, top_k}。*
 * ------------------------------------------------------------------ */

TextChat.init();

// ------------------------------------------------------------------ *
// 应用向导模式 API — wizard 框架 finish 时调用
// 契约:opts 由 wizard.json 的 steps[].fields[].ui_key 决定:
//   tc-prompt  → opts.prompt
// 空字段不触发对应动作(wizard 框架已校验);prompt 非空时把内容
// 写回 #text-chat-input,**不**自动 send——text_chat 是用户主动发
// 消息型应用,wizard 只负责"配置预填"。
// 全局名约定 ImApp_<app.id 去掉 hyphen>—— wizard 按此查找。
// ------------------------------------------------------------------ *
window.ImApp_text_chat = window.ImApp_text_chat || {};
window.ImApp_text_chat.applyWizardSnapshot = async function (opts) {
  const result = { prompt_filled: false };
  if (opts.prompt) {
    const input = document.getElementById("text-chat-input");
    if (input) {
      input.value = opts.prompt;
      input.dispatchEvent(new Event("input", { bubbles: true }));
      result.prompt_filled = true;
    }
  }
  return result;
};

})();
