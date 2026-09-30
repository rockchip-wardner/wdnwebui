(function () {
  "use strict";
  const API_PREFIX = "/api";

window.ImBridge = {
  ws: null,
  wsRetry: 0,
  wsTimer: null,
  snapshot: null,
  messages: [],          // most recent first (in + out mixed)
  chatCleared: false,    // true after 清屏: don't re-fill from backend on page re-entry
  // Smart-follow state: _pendingNewCount tracks messages received while
  // scrolled away from the bottom (shown via "↓ N 条新消息");
  // _lastRenderedCount captures messages.length at last render to compute
  // the diff.
  _pendingNewCount: 0,
  _lastRenderedCount: 0,

  init() {
    if (this._inited) return;
    this._inited = true;
    const $ = (id) => document.getElementById(id);
    $("imb-login-btn")?.addEventListener("click", () => this.login());
    $("imb-cancel-btn")?.addEventListener("click", () => this.cancelLogin());
    $("imb-logout-btn")?.addEventListener("click", () => this.logout());
    $("imb-send-btn")?.addEventListener("click", () => this.send());
    $("imb-clear-btn")?.addEventListener("click", () => this.clearChat());
    $("imb-history-btn")?.addEventListener("click", () => this.showHistory());
    $("imb-llm-bridge")?.addEventListener("change", (e) => {
      this._toggleLlmBridge(e.target.checked);
    });
    $("imb-jump-new")?.addEventListener("click", () => this._jumpToNewMessages());
    const list = $("imb-conv-list");
    if (list) {
      list.addEventListener("scroll", () => this._onConvScroll(), { passive: true });
    }
    $("imb-text-input")?.addEventListener("keydown", (e) => {
      // Cmd/Ctrl + Enter sends; plain Enter inserts a newline.
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        this.send();
      }
    });
  },

  async load() {
    try {
      const r = await fetch(API_PREFIX + "/wechat");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const snap = await r.json();
      this._applySnapshot(snap);
      // Pull runtime info (SDK version + wechatbot-sdk available flag)
      // for the "SDK" row; failures are non-fatal.
      try {
        const rr = await fetch(API_PREFIX + "/wechat/runtime");
        if (rr.ok) {
          const rt = await rr.json();
          const el = document.getElementById("imb-runtime");
          if (el) {
            el.textContent = rt.available
              ? (rt.module + " " + (rt.version || ""))
              : ("missing: " + (rt.error || "unavailable"));
            el.classList.toggle("imb-muted", !rt.available);
          }
        }
      } catch (_) { /* ignore */ }
    } catch (err) {
      this._setChatStatus("加载失败: " + err.message, true);
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
  },

  _openWs() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = proto + "//" + location.host + API_PREFIX + "/wechat/ws";
    let ws;
    try {
      ws = new WebSocket(url);
    } catch (err) {
      this._scheduleReconnect();
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      this.wsRetry = 0;
    };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (_) { return; }
      this._onWsMessage(msg);
    };
    ws.onclose = () => {
      this.ws = null;
      this._scheduleReconnect();
    };
    ws.onerror = () => {
      // onclose will fire after; reconnect logic lives there.
    };
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
      if (!this.snapshot) this.snapshot = {};
      Object.assign(this.snapshot, msg.state || {});
      this._renderState();
      this._renderButtons();
      this._renderQr();
      this._renderLlmBridge();
      return;
    }
    if (msg.type === "message") {
      this._recordMessage(msg.message);
      this._renderChat({ isNewMessageRender: true });
      this._renderKnown();
      return;
    }
  },

  _applySnapshot(snap) {
    if (!snap) return;
    this.snapshot = snap;
    // Only (re)fill from the backend's recent_messages if the user hasn't
    // cleared the view. After 清屏, keep the cleared state across page
    // switches (until re-login); new incoming messages still append.
    if (!this.chatCleared) {
      this.messages = Array.isArray(snap.recent_messages) ? snap.recent_messages.slice() : [];
    }
    this._renderState();
    this._renderButtons();
    this._renderQr();
    this._renderKnown();
    this._renderChat();
    this._renderLlmBridge();
  },

  _renderState() {
    const s = this.snapshot || {};
    const set = (id, val) => {
      const el = document.getElementById(id);
      if (el) el.textContent = (val === null || val === undefined || val === "") ? "--" : String(val);
    };
    set("imb-cred-path", s.cred_path);
    set("imb-user-id", s.user_id);
    set("imb-running", s.started ? "运行中" : "停止");

    const badge = document.getElementById("imb-status-badge");
    if (badge) {
      const state = s.login_state || "idle";
      const labelMap = {
        idle: "未登录",
        awaiting_scan: "等待扫码",
        scanned: "已扫码",
        confirmed: "已登录",
        running: "已登录",
        expired: "二维码已过期",
        error: "错误",
        cancelled: "已取消",
        login_failed: "登录失败",
      };
      badge.dataset.state = state;
      badge.textContent = labelMap[state] || state;
    }

    const errEl = document.getElementById("imb-error");
    if (errEl) {
      if (s.error) {
        errEl.textContent = "· " + s.error;
        errEl.classList.add("visible");
      } else {
        errEl.textContent = "";
        errEl.classList.remove("visible");
      }
    }
  },

  _renderButtons() {
    const s = this.snapshot || {};
    const state = s.login_state || "idle";
    const loginBtn = document.getElementById("imb-login-btn");
    const cancelBtn = document.getElementById("imb-cancel-btn");
    const logoutBtn = document.getElementById("imb-logout-btn");
    const sendBtn = document.getElementById("imb-send-btn");
    const textInput = document.getElementById("imb-text-input");

    const loggedIn = state === "confirmed" || state === "running" || !!s.started;
    const active = state === "awaiting_scan" || state === "scanned";

    if (loginBtn) loginBtn.disabled = loggedIn || active;
    if (cancelBtn) cancelBtn.disabled = !active;
    if (logoutBtn) logoutBtn.disabled = !loggedIn && state !== "error";
    const bridge = (s.llm_bridge && s.llm_bridge.enabled) || false;
    if (sendBtn) sendBtn.disabled = !loggedIn || bridge;
    if (textInput) textInput.disabled = !loggedIn;
  },

  _renderLlmBridge() {
    const s = this.snapshot || {};
    const b = (s.llm_bridge) || {};
    const cb = document.getElementById("imb-llm-bridge");
    const hint = document.getElementById("imb-llm-bridge-hint");
    if (!cb) return;

    const enabled = !!b.enabled;
    const available = b.available !== false;

    cb.checked = enabled;
    cb.disabled = !available;

    if (hint) {
      if (!available) {
        hint.textContent = "文本对话模型未启用，请在算力配置页启用 [LLM]文字对话v3 (qwen3-4b)";
        hint.className = "imb-bridge-hint danger";
      } else if (enabled) {
        hint.textContent = b.busy ? "已对接，生成中…" : "已对接，微信消息将自动回复";
        hint.className = "imb-bridge-hint";
      } else {
        hint.textContent = "";
        hint.className = "imb-bridge-hint";
      }
    }
  },

  async _toggleLlmBridge(checked) {
    const cb = document.getElementById("imb-llm-bridge");
    const hint = document.getElementById("imb-llm-bridge-hint");
    if (cb) cb.disabled = true;
    if (hint) hint.textContent = "处理中…";
    try {
      const r = await fetch(API_PREFIX + "/wechat/llm-bridge", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: checked }),
      });
      if (!r.ok) {
        let detail = "HTTP " + r.status;
        try { detail = (await r.json()).detail || detail; } catch (_) {}
        throw new Error(detail);
      }
      const data = await r.json();
      if (data.snapshot) this._applySnapshot(data.snapshot);
      else this._renderLlmBridge();
      this._setChatStatus(checked ? "已开启文字对话模型对接" : "已关闭文字对话模型对接");
    } catch (err) {
      if (cb) cb.checked = !checked;   // revert
      this._renderLlmBridge();
      this._setChatStatus("操作失败: " + err.message, true);
    }
  },

  _renderQr() {
    const stage = document.getElementById("imb-qr-stage");
    const hint = document.getElementById("imb-qr-hint");
    if (!stage || !hint) return;
    const s = this.snapshot || {};
    const state = s.login_state || "idle";
    const url = s.qr_url;
    const lastEvent = s.last_event || "";

    stage.innerHTML = "";
    // While ``login_state`` is still ``idle`` but ``last_event`` flipped
    // to ``"starting"``, the user just clicked 登录 and the SDK is in
    // the middle of fetching a QR — show a transient hint instead of
    // falling back to the "click 登录" prompt.
    if (state === "idle" && lastEvent === "starting") {
      hint.textContent = "正在向微信申请二维码…";
      hint.classList.remove("imb-qr-hint--muted");
      return;
    }
    if (state === "idle" && lastEvent === "logged_out") {
      hint.textContent = "已退出登录，点「登录」重新扫码";
      hint.classList.remove("imb-qr-hint--muted");
      return;
    }
    if (state === "idle" && lastEvent === "cancelled") {
      hint.textContent = "已取消本次扫码";
      hint.classList.add("imb-qr-hint--muted");
      return;
    }
    if (state === "awaiting_scan" && url) {
      try {
        // Vendored qrcode-generator (Kazuhiko Arase, MIT).
        // ``qrcode(0, 'M')`` auto-picks the smallest type number that fits.
        const qr = window.qrcode(0, "M");
        qr.addData(url);
        qr.make();
        const svg = qr.createSvgTag({ cellSize: 4, margin: 4, scalable: true });
        stage.innerHTML = svg;
        hint.textContent = "请用微信扫描二维码（120 秒内有效）";
        hint.classList.remove("imb-qr-hint--muted");
      } catch (err) {
        hint.textContent = "QR 渲染失败: " + err.message;
        hint.classList.add("imb-qr-hint--muted");
      }
      return;
    }
    if (state === "scanned") {
      hint.textContent = "已扫码，请在手机上点「登录」确认";
      hint.classList.remove("imb-qr-hint--muted");
      return;
    }
    if (state === "confirmed" || state === "running") {
      hint.textContent = "已登录，user_id = " + (s.user_id || "?");
      hint.classList.remove("imb-qr-hint--muted");
      return;
    }
    if (state === "expired") {
      hint.textContent = "二维码已过期，点「登录」重新获取";
      hint.classList.add("imb-qr-hint--muted");
      return;
    }
    if (state === "error" || state === "login_failed") {
      hint.textContent = "登录失败：" + (s.error || state);
      hint.classList.add("imb-qr-hint--muted");
      return;
    }
    if (state === "cancelled") {
      hint.textContent = "已取消本次扫码";
      hint.classList.add("imb-qr-hint--muted");
      return;
    }
    hint.textContent = "点击「登录」开始扫码";
    hint.classList.remove("imb-qr-hint--muted");
  },

  _renderKnown() {
    const el = document.getElementById("imb-known-count");
    if (!el) return;
    const known = (this.snapshot && Array.isArray(this.snapshot.known_users))
      ? this.snapshot.known_users.length
      : 0;
    el.textContent = "已知 " + known + " 人";
  },

  _recordMessage(msg) {
    if (!msg) return;
    // Dedup: the SDK and the poller can both produce the same item;
    // skip if it's already the head of the list.
    if (this.messages.length &&
        this.messages[0].ts_ms === msg.ts_ms &&
        this.messages[0].direction === msg.direction &&
        this.messages[0].user_id === msg.user_id &&
        this.messages[0].text === msg.text) {
      return;
    }
    this.messages.unshift(msg);
    if (this.messages.length > 200) this.messages.length = 200;
  },

  _renderChat({ isNewMessageRender = false } = {}) {
    const list = document.getElementById("imb-conv-list");
    const msgs = document.getElementById("imb-conv-msgs");
    const title = document.getElementById("imb-conv-title");
    if (!list || !msgs || !title) return;

    // Capture "was user near the bottom?" BEFORE innerHTML reset, otherwise
    // the reset zeroes scrollTop and the near-bottom check below is meaningless.
    // The captured value drives both auto-follow (at-bottom) and the
    // "↓ N 条新消息" button (scrolled-away).
    const wasNearBottom = this._isNearBottom(list);

    // New-message delta only counts when this render was triggered by an
    // actual incoming WS message (vs. an initial snapshot / re-fill).
    // The diff is computed against _lastRenderedCount, which is the
    // messages.length at the previous successful render — 0 on a fresh init.
    if (isNewMessageRender) {
      const newCount = Math.max(
        0,
        this.messages.length - this._lastRenderedCount,
      );
      if (newCount > 0 && !wasNearBottom) {
        this._pendingNewCount += newCount;
      } else if (wasNearBottom) {
        // User was at the bottom → consuming the new messages directly.
        this._pendingNewCount = 0;
      }
    }

    if (!this.messages.length) {
      title.textContent = "消息记录";
      msgs.innerHTML = `
        <div class="page-placeholder imb-empty">
          <div class="page-placeholder-text">
            登录后会话与消息会出现在这里。<br/>
            发送默认发给本机 user_id（用于测试 round-trip）。
          </div>
        </div>`;
      this._updateJumpNewButton();
      this._lastRenderedCount = 0;
      this._pendingNewCount = 0;
      return;
    }
    // Show all messages (any user) — the sender is locked to the bot's
    // own user_id, so almost everything will be to/from that account;
    // a real user messaging the bot will still surface here.
    // ``this.messages`` is newest-first (``_recordMessage`` unshifts),
    // so reverse to render oldest-at-top / newest-at-bottom (chat flow).
    const rows = this.messages.slice().reverse().map((m) => {
      const dir = m.direction === "out" ? "out" : "in";
      const errTag = m.error ? `<span class="imb-msg-error">[${this._esc(m.error)}]</span>` : "";
      const time = m.ts ? this._fmtTime(m.ts_ms) : "";
      const media = m.has_media ? " 📎" : "";
      const kindLabel = m.type ? `[${this._esc(m.type)}]` : "";
      return `<li class="imb-msg imb-msg--${dir}">
        <span class="imb-msg-meta">${this._esc(m.user_id)} · ${time}${media}</span>
        <div class="imb-msg-body">${kindLabel} ${this._esc(m.text || "")} ${errTag}</div>
      </li>`;
    });
    const header = (() => {
      const self = this.snapshot && this.snapshot.user_id;
      return self ? `消息记录（发送目标：${this._esc(self)}）` : "消息记录";
    })();
    title.textContent = header;
    msgs.innerHTML = `<ul class="imb-conv-ul">${rows.join("")}</ul>`;

    if (wasNearBottom || this._lastRenderedCount === 0) {
      // First-ever render (lastRenderedCount=0) — always start at the
      // bottom so the user sees the latest message, mirroring standard
      // chat UIs. Subsequent renders follow smart-follow rules.
      list.scrollTop = list.scrollHeight;
    }
    this._updateJumpNewButton();
    this._lastRenderedCount = this.messages.length;
  },

  // Threshold (px) below the bottom: if the user is scrolled within this
  // distance of the bottom, treat them as "following the tail".
  _CHAT_BOTTOM_TOLERANCE: 40,

  _isNearBottom(list) {
    if (!list) return true;
    return list.scrollHeight - list.scrollTop - list.clientHeight <=
      this._CHAT_BOTTOM_TOLERANCE;
  },

  _onConvScroll() {
    const list = document.getElementById("imb-conv-list");
    if (!list) return;
    if (this._isNearBottom(list)) {
      // User just caught up to the tail → drop the badge.
      this._pendingNewCount = 0;
      this._updateJumpNewButton();
    }
  },

  _jumpToNewMessages() {
    const list = document.getElementById("imb-conv-list");
    if (!list) return;
    list.scrollTop = list.scrollHeight;
    this._pendingNewCount = 0;
    this._updateJumpNewButton();
  },

  _updateJumpNewButton() {
    const btn = document.getElementById("imb-jump-new");
    const count = document.getElementById("imb-jump-new-count");
    if (!btn || !count) return;
    if (this._pendingNewCount > 0) {
      btn.classList.remove("hidden");
      count.textContent = `${this._pendingNewCount} 条新消息`;
    } else {
      btn.classList.add("hidden");
    }
  },

  refreshChat() {
    this._renderChat();
  },

  clearChat() {
    this.messages = [];
    this.chatCleared = true;
    this._renderChat();
  },

  showHistory() {
    // Restore the full recent history. chatCleared=false so the backend
    // snapshot (freshly fetched) fills this.messages again.
    this.chatCleared = false;
    this._refreshHistory();
  },

  async _refreshHistory() {
    try {
      const r = await fetch(API_PREFIX + "/wechat");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const snap = await r.json();
      // chatCleared is now false, so _applySnapshot fills from backend.
      this._applySnapshot(snap);
    } catch (err) {
      this._setChatStatus("加载历史失败: " + err.message, true);
    }
  },

  _setChatStatus(text, danger) {
    const el = document.getElementById("imb-chat-status");
    if (!el) return;
    el.textContent = text || "";
    el.classList.toggle("danger", !!danger);
  },

  async login() {
    const btn = document.getElementById("imb-login-btn");
    if (btn) btn.disabled = true;
    this._setChatStatus("正在发起登录…");
    try {
      const r = await fetch(API_PREFIX + "/wechat/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ force: false }),
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      // Re-login: reset the cleared-view flag so the new session shows history.
      if (data.snapshot) {
        this.chatCleared = false;
        this._applySnapshot(data.snapshot);
      }
      this._setChatStatus("");
    } catch (err) {
      this._setChatStatus("登录失败: " + err.message, true);
      this._renderButtons();
    }
  },

  async cancelLogin() {
    try {
      const r = await fetch(API_PREFIX + "/wechat/cancel-login", {
        method: "POST",
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      if (data.snapshot) this._applySnapshot(data.snapshot);
    } catch (err) {
      this._setChatStatus("取消失败: " + err.message, true);
    }
  },

  async logout() {
    if (!confirm("确认退出登录？已保存的凭证将被删除，下次需要重新扫码。")) return;
    try {
      const r = await fetch(API_PREFIX + "/wechat/logout", { method: "POST" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      // Logout resets the cleared-view flag; a fresh login will re-show history.
      this.chatCleared = false;
      if (data.snapshot) this._applySnapshot(data.snapshot);
      else await this.load();
      this._setChatStatus("已退出登录");
    } catch (err) {
      this._setChatStatus("退出失败: " + err.message, true);
    }
  },

  async send() {
    const textEl = document.getElementById("imb-text-input");
    if (!textEl) return;
    // 开关开启时禁止手动"插话"（与后端一致；按钮已置灰，此处兜底键盘触发）
    if (this.snapshot && this.snapshot.llm_bridge && this.snapshot.llm_bridge.enabled) {
      this._setChatStatus("对接文字对话模型已开启，手动发送已禁用", true);
      return;
    }
    // The receiver is locked to the bot's own user_id — the dialog
    // exists to verify the SDK's round-trip, not to message arbitrary
    // users. We accept whatever the backend has for ``user_id``; the
    // send button is disabled when it's null so this branch is rare.
    const userId = (this.snapshot && this.snapshot.user_id) || "";
    const text = textEl.value;
    if (!userId) {
      this._setChatStatus("未登录，无法发送", true);
      return;
    }
    if (!text.trim()) {
      this._setChatStatus("消息内容不能为空", true);
      textEl.focus();
      return;
    }
    const btn = document.getElementById("imb-send-btn");
    if (btn) btn.disabled = true;
    this._setChatStatus("发送中…");
    try {
      const r = await fetch(API_PREFIX + "/wechat/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: userId, text: text }),
      });
      if (!r.ok) {
        let detail = "HTTP " + r.status;
        try {
          const errBody = await r.json();
          if (errBody && errBody.detail) detail = errBody.detail;
        } catch (_) { /* ignore */ }
        throw new Error(detail);
      }
      textEl.value = "";
      this._setChatStatus("已发送");
    } catch (err) {
      this._setChatStatus("发送失败: " + err.message, true);
    } finally {
      if (btn) btn.disabled = false;
    }
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
 *  TextChat: 文本对话页面模块                                          *
 *                                                                     *
 *  服务端走 ``/api/text-chat/ws`` 推流式 token + state 变更；前端      *
 *  按 request_id 维护"用户问题 → thinking → answer" 三段气泡，         *
 *  流式渲染时附加打字机光标，``done`` 后清理。                         *
 *                                                                     *
 *  WS 帧协议：                                                        *
 *    snapshot / state / user_echo / thinking / answer /               *
 *    done / stopped / error                                           *
 * ------------------------------------------------------------------ */

ImBridge.init();

})();
