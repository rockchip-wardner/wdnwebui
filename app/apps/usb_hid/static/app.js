(function () {
  "use strict";
  const API_PREFIX = "/api";

  // ---- 状态机 ---- //
  const UsbHid = {
    initialized: false,
    snapshot: null,
    // Host screen size — used for mapping grid-relative click to absolute
    // host coordinates the USB HID gadget will forward to the PC. We
    // hard-code 1920×1080 because (a) it's the most common PC resolution
    // and (b) RK has no way to discover the PC screen size over USB
    // anyway. If the actual PC screen is different the cursor lands off
    // center but still within bounds; the user can re-aim by clicking
    // elsewhere on the grid.
    hostScreenW: 1920,
    hostScreenH: 1080,
    init() {
      if (this.initialized) return;
      this.initialized = true;

      this.hdmiinImg = document.getElementById("usb-hid-grid-img");
      this.hdmiinGrid = document.getElementById("usb-hid-grid");
      const $ = (id) => document.getElementById(id);

      $("usb-hid-refresh")?.addEventListener("click", () => this.refresh());

      // HDMI-IN capture is part of the app's long-running state, not a
      // wizard step. The wizard (env_check → finish) is a one-shot
      // confirmation that doesn't gate hdmiin — once env passes, the
      // user can already see the live preview in the page section.
      // init() kicks the capture / poll right away so refresh / re-entry
      // / back-from-other-page all show the same continuous stream.
      this.refreshHdmiin();

      // The hdmiin live-preview grid doubles as the digitizer surface:
      // clicking it maps the click point to host coordinates and
      // forwards it to /api/usb-hid/digitizer/click; once focused
      // (first click also focuses the div via tabindex="0"), every
      // key press is forwarded to /keyboard/tap. Escape blurs the
      // grid to drop keyboard passthrough. See bindGridInteraction()
      // for the host-screen mapping + key routing logic.
      this.bindGridInteraction();

      // AI 电脑控制卡（服务端循环 + 1Hz 轮询）。
      this.bindTaskCard();

      this.refresh();
    },

    // ------------------------------------------------------------------
    // Grid-as-digitizer-and-keyboard — interactive surface that turns
    // the hdmiin preview into a touchscreen + keyboard for the PC.
    //
    // Coordinate mapping:
    //   * click_x_pixel = (ev.clientX - rect.left) clipped to rect
    //   * click_y_pixel = (ev.clientY - rect.top)  clipped to rect
    //   * host_x = round(click_x_pixel / rect.w * hostScreenW)
    //   * host_y = round(click_y_pixel / rect.h * hostScreenH)
    // The host-side tooling (USB HID gadget + wdnhid.digitizer) takes
    // these as raw absolute coordinates; the host OS maps them onto
    // its own screen size, so the user just sees a faithful cursor
    // when the host screen matches `hostScreenW/H` (default 1920×1080).
    //
    // Drag semantics (pointer events, NOT click):
    //   * pointerdown → POST /digitizer/down  (Tip Switch = 1, no release yet)
    //   * pointermove (with button still pressed) → POST
    //     /digitizer/move?tip_down=true (Tip Switch stays 1, X/Y slides)
    //   * pointerup / pointercancel → POST /digitizer/up  (Tip Switch = 0)
    //   * setPointerCapture() is called on pointerdown so the OS
    //     keeps delivering move/up to this element even when the
    //     cursor (or finger) slides off the grid — critical for any
    //     drag gesture that travels beyond the live-preview area.
    //
    // Why NOT just listen on "click":
    //   Browsers interpret mousedown + mousemove on a draggable child
    //   as the start of an HTML5 drag-and-drop gesture
    //   (dragstart/drag/dragend), not as a click. The <img> in the
    //   grid is draggable by default — drag never fires on the
    //   outer grid, click never fires either once drag begins, and
    //   the OS shows a "ghost image" being moved around instead of
    //   a touchscreen press on the PC. We solve this two ways:
    //     (a) CSS: img { pointer-events: none; -webkit-user-drag: none;
    //         touch-action: none; }, and grid { touch-action: none }.
    //         The first stops the browser handing mousedown to the
    //         <img>, the third stops mobile / RK local touchscreen
    //         page-scroll / pinch-zoom gestures from "stealing" the
    //         touch stream away from our pointer handlers.
    //     (b) Pointer events with preventDefault() in down/move/up
    //         — even if a draggable child somehow got the events,
    //         we'd suppress the default behaviour.
    //
    // Keyboard passthrough semantics:
    //   * only when document.activeElement === hdmiinGrid
    //   * forwards the literal `event.key` (e.g. "a", "Enter", "F5")
    //     to /keyboard/tap; the API accepts the same set as /hotkey
    //   * modifier keys (Shift/Ctrl/Alt/Meta) tracked in a small Set
    //     keyed by event.code so combos work — when the modifier's
    //     keydown fires we record it, on keyup we drop it. The held
    //     modifiers go with each forwarded tap.
    //   * Escape blurs the grid (release focus → ordinary form fields
    //     regain keyboard focus, "smart" passthrough stops).
    bindGridInteraction() {
      const grid = this.hdmiinGrid;
      if (!grid) return;

      // Helper: map a PointerEvent's clientX/Y to host-pixel coords.
      const evToHost = (ev) => {
        const rect = grid.getBoundingClientRect();
        const px = Math.max(0, Math.min(rect.width,  ev.clientX - rect.left));
        const py = Math.max(0, Math.min(rect.height, ev.clientY - rect.top));
        return [
          Math.round(px / rect.width  * this.hostScreenW),
          Math.round(py / rect.height * this.hostScreenH),
        ];
      };
      // Active drag state — captured pointerId so we ignore stray
      // pointermove/up events that aren't ours (multi-touch on RK
      // local touchscreen; or an attached mouse + a finger).
      let activePointerId = null;
      let activeBtn = -1; // remember which button initiated the press
                           // so we don't treat hover-only secondary
                           // touches as a drag.

      const startPress = (ev) => {
        activePointerId = ev.pointerId;
        activeBtn = ev.button;
        // Capture so move/up keep coming to us even when the
        // cursor leaves the grid bounds (critical for drag gestures
        // that travel off the live preview — e.g. dragging a window
        // title-bar off the visible area).
        try { grid.setPointerCapture(ev.pointerId); } catch (_e) { /* no-op */ }
        const [hx, hy] = evToHost(ev);
        // Status: tip_down at (hx, hy)
        this.postDigitizerDown(hx, hy, ev);
        // Give the grid keyboard focus on first press so the user's
        // typing then routes through the keyboard passthrough.
        if (document.activeElement !== grid) grid.focus();
      };
      const movePress = (ev) => {
        if (ev.pointerId !== activePointerId) return;
        const [hx, hy] = evToHost(ev);
        // tip_down stays 1 so the host sees a continuous contact
        // sliding — this is what makes "drag" gestures route
        // through the same Win PT event stream as a held finger.
        this.postDigitizerMove(hx, hy, ev, /*tip_down=*/true);
      };
      const endPress = (ev) => {
        // Always clear state and send tip_up. The pointerId match
        // is best-effort: if for any reason activePointerId has
        // drifted (multi-touch, pointer capture hijack, browser
        // edge case), the host still needs a release to keep its
        // touch state machine in sync. Sending a duplicate tip_up
        // when there was no matching tip_down is harmless — Win PT
        // ignores out-of-order releases.
        const released = activePointerId !== null ? activePointerId : ev.pointerId;
        activePointerId = null;
        activeBtn = -1;
        try { grid.releasePointerCapture(released); } catch (_e) { /* no-op */ }
        // tip_up — sends Contact Count = 0 + Tip Switch = 0 to the
        // host; no X/Y so the OS shows "pen left the tablet" rather
        // than "pen teleported to origin" (which would click whatever
        // happens to live at (0, 0) on the host).
        this.postDigitizerUp(ev);
      };

      grid.addEventListener("pointerdown", (ev) => {
        // button -1 / 0 / 2 are real mouse / touch / pen presses;
        // button 1 (middle) and anything above 2 are secondary
        // gestures we don't forward.
        if (ev.button > 0) return;
        ev.preventDefault();
        startPress(ev);
      });
      grid.addEventListener("pointermove", (ev) => {
        if (activePointerId === null) return;
        ev.preventDefault();
        movePress(ev);
      });
      // pointerup is the normal "release" path. pointercancel fires
      // when the browser yanks the pointer out from under us (e.g.
      // a touch sliding into the system gesture zone, or a system
      // modal dialog popping up mid-drag) — still need to send
      // tip_up so the host doesn't think the finger is stuck down.
      //
      // We don't gate on activePointerId — any pointer that hits
      // release inside the grid should produce a tip_up so the host
      // never ends up with a "stuck contact". The state machine
      // (down → N×move → up) is for batching the in-progress drag
      // only; the release endpoint itself is unconditional.
      const onUp = (ev) => {
        ev.preventDefault();
        endPress(ev);
      };
      grid.addEventListener("pointerup", onUp);
      grid.addEventListener("pointercancel", onUp);

      // Track held modifier keys by event.code (location-independent
      // "ShiftLeft" / "ShiftRight" both become "ShiftLeft" for HID
      // modifier bit purposes). The Set is updated on keydown /
      // keyup at the document level *only* while the grid has focus,
      // so other inputs (e.g. the textarea below) stay usable.
      const heldMods = new Set(); // codes like "ControlLeft"
      const codeToModifier = (code) => {
        if (!code) return null;
        if (code.startsWith("Control")) return "CTRL";
        if (code.startsWith("Shift"))   return "SHIFT";
        if (code.startsWith("Alt"))     return "ALT";
        if (code.startsWith("Meta"))    return "LGUI";
        return null;
      };
      // Forward one keystroke with currently-held modifiers as JSON
      // body. /keyboard/tap (the _KbKey pydantic model) expects
      // { keycode? | key, modifier? } in the body — not query params.
      // We always send the *next* tap as press = keydown; release
      // happens on keyup via /keyboard/release. This gives the PC a
      // real press/release pair for every key, not a "type" string.
      const forwardTap = (key, mods) => {
        const body = { key, modifier: (mods && mods.size) ? [...mods].join(",") : "" };
        fetch(API_PREFIX + "/usb-hid/keyboard/tap", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        })
          .then((r) => {
            this.setDigStatus(r.ok ? `已透传键 ${key}` : `键 ${key} 失败 HTTP ${r.status}`,
                              !r.ok);
          })
          .catch((e) => this.setDigStatus("键透传失败: " + e.message, true));
      };

      // Use capturing phase so we see the event before any focused
      // <input>/<textarea> handles it; gate on document.activeElement
      // being the grid.
      document.addEventListener("keydown", (ev) => {
        if (document.activeElement !== grid) return;
        if (ev.repeat) return; // ignore key-repeat auto-fire
        if (ev.key === "Escape") {
          ev.preventDefault();
          grid.blur();
          return;
        }
        // Record modifier (Shift/Ctrl/Alt/Meta) holds.
        const mod = codeToModifier(ev.code);
        if (mod) {
          heldMods.add(mod);
          ev.preventDefault();
          return; // the actual modified key arrives as a separate event
        }
        // Skip pure modifier keydowns we'd otherwise double-count above.
        if (!ev.key || ev.key === "Unidentified") return;
        ev.preventDefault();
        forwardTap(ev.key, heldMods);
      }, true);

      document.addEventListener("keyup", (ev) => {
        if (document.activeElement !== grid) return;
        const mod = codeToModifier(ev.code);
        if (mod) {
          heldMods.delete(mod);
          ev.preventDefault();
          return;
        }
        // Notify the gadget that the key is released so the PC OS
        // sees a clean press→release pair instead of a stuck key.
        fetch(API_PREFIX + "/usb-hid/keyboard/release", { method: "POST" })
          .catch(() => { /* best-effort */ });
      }, true);
    },

    // Three "split" digitizer posts corresponding to the pointer
    // event triplet (pointerdown / pointermove / pointerup). They
    // are intentionally *not* batched into a single click() — the
    // whole reason for splitting them is so the page can keep Tip
    // Switch = 1 across many move frames during a drag, which is
    // what the Win PT driver uses to route the contact as a single
    // continuous gesture (window drag, slider scrub, file drop)
    // rather than N separate taps.
    postDigitizerDown(hostX, hostY, ev) {
      const el = document.getElementById("usb-hid-pointer-pos");
      if (el) el.textContent = `x=${hostX} y=${hostY}`;
      return fetch(API_PREFIX + "/usb-hid/digitizer/down", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ x: hostX, y: hostY }),
      }).then((r) => {
        if (r.ok) this.setDigStatus(`按下 (${hostX}, ${hostY})`);
        else this.setDigStatus(`按下 失败 HTTP ${r.status}`, true);
      }).catch((e) => {
        this.setDigStatus("按下 失败: " + e.message, true);
      });
    },
    postDigitizerMove(hostX, hostY, ev, tip_down) {
      const el = document.getElementById("usb-hid-pointer-pos");
      if (el) el.textContent = `x=${hostX} y=${hostY}`;
      return fetch(API_PREFIX + "/usb-hid/digitizer/move", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ x: hostX, y: hostY, tip_down: !!tip_down }),
      }).then((r) => {
        if (!r.ok) this.setDigStatus(`拖动 HTTP ${r.status}`, true);
        // Don't spam the status line on every move frame — silently
        // absorb success; the cursor-position span already shows the
        // current coords.
      }).catch(() => { /* best-effort — drop frames on network blip */ });
    },
    postDigitizerUp(ev) {
      return fetch(API_PREFIX + "/usb-hid/digitizer/up", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }).then((r) => {
        if (r.ok) this.setDigStatus("已抬起");
        else this.setDigStatus(`抬起 失败 HTTP ${r.status}`, true);
      }).catch((e) => {
        this.setDigStatus("抬起 失败: " + e.message, true);
      });
    },

    postDigitizerClick(hostX, hostY, label) {
      // Legacy one-shot click kept around in case any external
      // script (e.g. verify scripts, devtools console) wants to
      // fire a single tap; not called from inside this file
      // anymore — the pointer-event flow now drives everything
      // via down/move/up so drag gestures work.
      const el = document.getElementById("usb-hid-pointer-pos");
      if (el) el.textContent = `x=${hostX} y=${hostY}`;
      return fetch(API_PREFIX + "/usb-hid/digitizer/click", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ x: hostX, y: hostY, press_ms: 12.0 }),
      }).then((r) => {
        if (r.ok) this.setDigStatus(`已透传 ${label} (${hostX}, ${hostY})`);
        else this.setDigStatus(`${label} 失败 HTTP ${r.status}`, true);
      }).catch((e) => {
        this.setDigStatus("digitizer 透传失败: " + e.message, true);
      });
    },

    async refresh() {
      const target = document.getElementById("data-usb-hid");
      try {
        const r = await fetch(API_PREFIX + "/usb-hid", { cache: "no-store" });
        if (!r.ok) {
          this.setStatus("刷新失败 HTTP " + r.status, true);
          return;
        }
        const snap = await r.json();
        this.snapshot = snap;
        this.applySnapshot(snap);
        if (target) target.textContent = JSON.stringify(snap, null, 2);
      } catch (e) {
        this.setStatus("刷新失败: " + e.message, true);
      }
      // HDMI-IN status — best-effort; 503 means hwcap_hdmiin disabled
      // (which is a config decision, not a bug). Other errors surface.
      this.refreshHdmiin();
    },

    async refreshHdmiin() {
      try {
        const r = await fetch(API_PREFIX + "/usb-hid/source/hdmiin/status",
                              { cache: "no-store" });
        if (r.status === 503) {
          this.applyHdmiin({ ok: false,
            reason: "hwcap_hdmiin disabled — enable in 系统配置 → 硬件能力" });
          return;
        }
        if (!r.ok) {
          this.applyHdmiin({ ok: false,
            reason: "probe HTTP " + r.status });
          return;
        }
        const s = await r.json();
        this.applyHdmiin(s);
        // Kick off capture + preview polling on first successful probe
        if (s.ok && !this._hdmiinCaptureKicked) {
          this._hdmiinCaptureKicked = true;
          this.startHdmiinCapture();
        }
      } catch (e) {
        this.applyHdmiin({ ok: false, reason: "probe failed: " + e.message });
      }
    },

    async startHdmiinCapture() {
      try {
        await fetch(API_PREFIX + "/usb-hid/source/hdmiin/open",
                    { method: "POST" });
        this._pollHdmiinSnapshot();
      } catch (e) {
        this.applyHdmiin({ ok: false, reason: "open failed: " + e.message });
      }
    },

    async _pollHdmiinSnapshot() {
      if (!this.hdmiinImg) return;
      // Serial pull loop: fetch the next frame only after the previous
      // one has been fully received and its object URL handed to the
      // <img>. That backpressure is what keeps us from queueing frames
      // we can never paint.
      //
      // The delay between ticks is what sets the ceiling, and it is NOT
      // a server-side limit: the backend serves a 272 KB 720p JPEG in
      // ~2.3 ms and answers ~70 pulls/second on loopback. The capture
      // thread, with `target_fps = 30`, produces 30 distinct frames per
      // second. The old hard 120 ms (≈8 fps) was the actual bottleneck
      // and the reason the preview felt like a slideshow.
      //
      // Default 24 ms ≈ 30 fps serial ceiling, which matches the
      // source exactly: faster than that just pulls duplicate frames
      // (same JPEG repeated), slower starts missing source frames. On
      // headless Chromium the bound is the cycle time itself
      // (delay ≈ 24 ms gives ~31 measured fps; full Chromium on the
      // RK3588 A76 lands near 25–30 fps once JPEG decode + composite
      // are included).
      //
      // Overridable for measurement (Playwright / devtools console):
      //   window.__hdmiinPollMs = 0;
      const delay = () => {
        const o = window.__hdmiinPollMs;
        return (typeof o === "number" && o >= 0) ? o : 24;
      };
      const tick = async () => {
        if (!this.initialized) return;
        const t = Date.now();
        try {
          const r = await fetch(API_PREFIX +
                                "/usb-hid/source/hdmiin/snapshot?t=" + t,
                                { cache: "no-store" });
          if (r.status === 200) {
            const blob = await r.blob();
            const url = URL.createObjectURL(blob);
            const old = this.hdmiinImg.src;
            this.hdmiinImg.src = url;
            this.hdmiinImg.classList.add("is-active");
            if (this.hdmiinGrid) this.hdmiinGrid.classList.add("has-image");
            // Revoke the previous object URL after the new one paints
            // so we don't leak. setTimeout(0) lets the new src commit.
            setTimeout(() => { if (old && old.startsWith("blob:")) URL.revokeObjectURL(old); }, 0);
          }
          // 204 = no frame yet (race during boot); 503 = hwcap off.
          // Both are non-fatal — just retry next tick.
        } catch (_e) {
          // network blip; keep retrying
        }
        setTimeout(tick, delay());
      };
      tick();
    },

    applyHdmiin(s) {
      const $ = (id) => document.getElementById(id);
      const devEl   = $("usb-hid-hdmiin-device");
      const drvEl   = $("usb-hid-hdmiin-driver");
      const linkEl  = $("usb-hid-hdmiin-link");
      const resEl   = $("usb-hid-hdmiin-res");
      const fmtEl   = $("usb-hid-hdmiin-pixfmt");
      const sumEl   = $("usb-hid-hdmiin-summary");
      const reasonEl = $("usb-hid-hdmiin-reason");
      const coordEl  = $("usb-hid-grid-coord");
      if (!devEl) return;  // page not yet mounted
      devEl.textContent = s.device || "—";
      drvEl.textContent = s.driver || "—";
      linkEl.textContent = s.hdmi_link || "—";
      resEl.textContent  = s.width && s.height ? `${s.width}×${s.height}` : "—";
      fmtEl.textContent  = s.pixfmt || "—";
      reasonEl.textContent = s.reason || "";
      reasonEl.style.color = s.ok ? "" : "#e74c3c";
      const okClass = s.ok ? "is-ok" : "is-err";
      [devEl, drvEl, linkEl, resEl, fmtEl].forEach((el) => {
        el.classList.remove("is-ok", "is-err");
        el.classList.add(s.ok ? okClass : "is-err");
      });
      sumEl.textContent = s.ok
        ? `已连接 (${s.width}×${s.height} ${s.pixfmt})`
        : "未就绪";
      sumEl.style.color = s.ok ? "#1abc54" : "#e74c3c";
      if (coordEl) {
        coordEl.textContent = s.ok
          ? `HDMI-IN · ${s.width}×${s.height} · ${s.pixfmt}`
          : `HDMI-IN · ${s.hdmi_link || "未连接"}`;
      }
    },

    applySnapshot(snap) {
      const banner = document.getElementById("usb-hid-unavailable");
      const reason = document.getElementById("usb-hid-unavailable-reason");
      const line = document.getElementById("usb-hid-status-line");
      if (snap.ready) {
        banner.classList.add("hidden");
        line.textContent = `已就绪 (UDC=${snap.udc}, state=${snap.state})`;
        line.style.color = "#1abc54";
      } else {
        banner.classList.remove("hidden");
        const missing = [];
        if (!snap.devices.keyboard.exists) missing.push("/dev/hidg0 不存在");
        else if (!snap.devices.keyboard.writable) missing.push("/dev/hidg0 不可写");
        if (!snap.devices.digitizer.exists) missing.push("/dev/hidg1 不存在");
        else if (!snap.devices.digitizer.writable) missing.push("/dev/hidg1 不可写");
        if (snap.state !== "configured") {
          missing.push(`gadget state=${snap.state} (${snap.udc || "unbound"})`);
        }
        reason.textContent = missing.length
          ? "需要 sudo 一次性 setup + udev 规则: " + missing.join("; ")
          : "Gadget 状态异常";
        line.textContent = "未就绪";
        line.style.color = "#e74c3c";
      }
    },

    setStatus(text, isErr) {
      const el = document.getElementById("usb-hid-kb-status");
      if (el) {
        el.textContent = text;
        el.style.color = isErr ? "#e74c3c" : "";
      }
    },
    setDigStatus(text, isErr) {
      const el = document.getElementById("usb-hid-dig-status");
      if (el) {
        el.textContent = text;
        el.style.color = isErr ? "#e74c3c" : "";
      }
    },

    // ---- Actions ---- //
    /* onType / onRelease / onHotkey were removed together with the
     * "键盘" card: they were driven by the textarea + combo-key inputs,
     * which no longer exist. Free-form text injection is intentionally
     * gone — the live-preview grid is the input surface now.
     *
     * Keyboard *passthrough* is unaffected: bindGridInteraction()
     * captures real keydown/keyup at the document level while the grid
     * has focus and forwards those directly, which never touched those
     * three handlers. */

    // ------------------------------------------------------------------
    // 自然语言任务 — AI 自动控制（服务端循环，前端只提交 + 轮询 + 渲染）
    //
    // 循环本身在 app/apps/usb_hid/agent.py：截屏 → VLM 出动作 → 写 HID。
    // 一轮要几秒，所以轮询 1Hz 足够，不是忙轮询。
    //
    // DemoGuard 联动：每次 render 都把服务端快照 **整个** report 上去
    // （manifest.json 的 demo_guard.active_state_keys 就是
    // ["running", "state"]）。必须整体上报而不是只 report 一个精简对象
    // —— active 判定读的就是快照里这两个键，漏报等于告诉框架"没在跑"，
    // 于是用户切页时不会被拦，PC 会在用户已经离开的页面上继续被点。
    bindTaskCard() {
      const $ = (id) => document.getElementById(id);
      this.taskInput = $("usb-hid-task-input");
      this.taskSteps = $("usb-hid-task-max-steps");
      this.taskStartBtn = $("usb-hid-task-start");
      this.taskStopBtn = $("usb-hid-task-stop");
      this.taskBadge = $("usb-hid-task-badge");
      this.taskElapsed = $("usb-hid-task-elapsed");
      this.taskStatus = $("usb-hid-task-status");
      this.taskLog = $("usb-hid-task-log");

      this.taskTimer = null;

      this.taskStartBtn?.addEventListener("click", () => this.startTask());
      this.taskStopBtn?.addEventListener("click", () => this.stopTask());

      // 恢复：刷新页面 / 从别的页切回来时，后端可能还有一个任务在跑
      // （离开时的 sendBeacon 只是 best-effort，浏览器可能丢掉）。
      // 同步拉一次，有任务就接着轮询，没有就把 UI 归位。
      this.syncTask();
    },

    async syncTask() {
      const snap = await this._fetchTaskStatus();
      if (!snap) return;
      this.renderTask(snap);
      if (snap.running) this.startTaskPolling();
      else this.applyTaskIdle(snap);
    },

    async startTask() {
      const task = (this.taskInput?.value || "").trim();
      if (!task) {
        this.setTaskStatus("请先填写要让 AI 完成的任务。", true);
        return;
      }
      const maxSteps = parseInt(this.taskSteps?.value || "12", 10);
      this.taskStartBtn.disabled = true;
      this.setTaskStatus("正在启动…");
      try {
        const r = await fetch(API_PREFIX + "/usb-hid/task/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            task,
            max_steps: Number.isFinite(maxSteps) ? maxSteps : 12,
          }),
        });
        const body = await r.json().catch(() => ({}));
        if (!r.ok) {
          // 502 = 模型没开（功能未启用），503 = hdmiin 没开，409 = 已在跑。
          // 三者可执行动作不同，所以把后端 detail 原样透出。
          this.setTaskStatus(`启动失败：${body.detail || "HTTP " + r.status}`, true);
          this.taskStartBtn.disabled = false;
          return;
        }
        this.setTaskStatus("");
        this.renderTask(body);
        this.startTaskPolling();
      } catch (e) {
        this.setTaskStatus("启动失败：" + (e && e.message ? e.message : e), true);
        this.taskStartBtn.disabled = false;
      }
    },

    async stopTask() {
      this.taskStopBtn.disabled = true;
      try {
        await fetch(API_PREFIX + "/usb-hid/task/stop", { method: "POST" });
      } catch (_e) {
        // 用户已经点了停止；即便请求丢了，1Hz 轮询也会看到 running=false。
      }
      await this.syncTask();
    },

    startTaskPolling() {
      if (this.taskTimer) return;
      this.taskTimer = setInterval(() => this.pollTask(), 1000);
      this.pollTask();
    },

    stopTaskPolling() {
      if (!this.taskTimer) return;
      clearInterval(this.taskTimer);
      this.taskTimer = null;
    },

    async pollTask() {
      const snap = await this._fetchTaskStatus();
      if (!snap) return;
      this.renderTask(snap);
      // 快照即终态时停表 —— 不再有事会发生，继续轮询只是白刷。
      if (!snap.running) {
        this.stopTaskPolling();
        this.applyTaskIdle(snap);
      }
    },

    async _fetchTaskStatus() {
      try {
        const r = await fetch(API_PREFIX + "/usb-hid/task/status", { cache: "no-store" });
        if (!r.ok) return null;
        return await r.json();
      } catch (_e) {
        return null;
      }
    },

    renderTask(snap) {
      if (window.DemoGuard) window.DemoGuard.report("usb-hid", snap);

      const running = !!snap.running;
      if (this.taskBadge) {
        this.taskBadge.hidden = !running;
        this.taskBadge.textContent = "运行中";
        this.taskBadge.className = running ? "badge ok" : "badge";
      }
      if (this.taskElapsed) {
        this.taskElapsed.textContent = snap.step_count
          ? `第 ${snap.step_count}/${snap.max_steps} 步 · ${snap.elapsed_s}s`
          : "—";
      }
      if (this.taskStartBtn) this.taskStartBtn.disabled = running;
      if (this.taskStopBtn) this.taskStopBtn.disabled = !running;

      this.renderTaskLog(snap.steps || []);
      if (snap.error) this.setTaskStatus(snap.error, snap.state === "error");
    },

    applyTaskIdle(snap) {
      if (this.taskStartBtn) this.taskStartBtn.disabled = false;
      if (this.taskStopBtn) this.taskStopBtn.disabled = true;
      if (snap.state === "done") this.setTaskStatus("任务已完成。", false);
      else if (snap.state === "stopped") this.setTaskStatus("已停止。", false);
    },

    renderTaskLog(steps) {
      if (!this.taskLog) return;
      // 每次全量重画：步数上限 60，DOM 量可以忽略，而增量 diff 要额外
      // 记住上一帧的键序，徒增状态。
      // textContent 而不是 innerHTML —— step 里的 thought / text / summary
      // 全是模型或主机拼出来的字符串，拼进 HTML 就是一条注入通道。
      const frag = document.createDocumentFragment();
      for (const s of steps) {
        const li = document.createElement("li");
        if (s.status === "error") li.className = "is-error";
        const idx = document.createElement("span");
        idx.className = "usb-hid-task-idx";
        idx.textContent = "#" + s.idx;
        const act = document.createElement("span");
        act.className = "usb-hid-task-act";
        act.textContent = s.action || "?";
        const target = document.createElement("span");
        target.className = "usb-hid-task-target";
        target.textContent = s.summary || "";
        const thought = document.createElement("span");
        thought.className = "usb-hid-task-thought";
        thought.textContent = s.status === "error"
          ? (s.detail || "执行失败")
          : (s.thought || "");
        li.append(idx, act, target, thought);

        // 越界告警(模型把坐标给到了灰边/黑边上)。单独一行加红边，
        // 因为它是"点不准"最直接的线索，但不该挤占 thought 那行的位置。
        if (s.warn) {
          const warn = document.createElement("div");
          warn.className = "usb-hid-task-warn";
          warn.textContent = s.warn;
          li.appendChild(warn);
        }

        // think 链(模型开 enable_thinking 后的推理过程)。默认折叠：
        // 它是排障依据，但一屏塞满推理链会淹没真正要看的那一行动作。
        // 折叠后标题行仍然显示"有推理"，一眼能看出哪几步模型犹豫过。
        if (s.thinking) {
          const det = document.createElement("details");
          det.className = "usb-hid-task-thinking";
          const sum = document.createElement("summary");
          sum.textContent = "模型推理过程";
          const body = document.createElement("span");
          body.className = "usb-hid-task-thinking-body";
          // 同样是 textContent：think 链也是模型生成的字符串。
          body.textContent = s.thinking;
          det.append(sum, body);
          li.appendChild(det);
        }

        frag.appendChild(li);
      }
      this.taskLog.replaceChildren(frag);
      // 保持在最底部：轮询反复 replaceChildren，视口默认跟着回顶，用户
      // 就永远只能看到第 1 步 —— 长任务最该看的恰恰是最新那一步。
      this.taskLog.scrollTop = this.taskLog.scrollHeight;
    },

    setTaskStatus(text, isErr) {
      if (!this.taskStatus) return;
      this.taskStatus.textContent = text || "";
      this.taskStatus.style.color = isErr ? "#e74c3c" : "";
    },

  };

  // ---- Hook into the global page-init lifecycle ---- //
  // Framework's _preloadCardScripts appends this <script> synchronously
  // during boot (onReady, which fires after DOMContentLoaded) — so by
  // the time we get here, DOMContentLoaded has already passed and the
  // addEventListener would never fire. We can init() immediately
  // because all of #page-usb-hid's elements are already in the DOM
  // (the <section> is part of the main HTML, not lazy-injected).
  //
  // DemoGuard: **不再在这里 register**。
  //
  // 这里原来有一个 `register("usb-hid", {active: () => true, stop: ...})`，
  // 本意是让"hdmiin 一直在跑"体现在导航守卫上。实际效果恰好相反，而且很糟：
  //
  // 谁后跑谁赢 —— `DemoGuard.register` 就是 `this._registry[id] = handlers`。
  // 顺序上 `_registerCardsFromManifest()`(onReady 第 3 步)是**同步**跑的，
  // 而本文件要等 framework 同步注入的 `<script>` 从网络回来才执行
  // (_preloadCardScripts 挂在 Promise 链上，chain.then)，所以**app.js 反而
  // 是后写的那个**，覆盖掉了 manifest 里的配置。于是 active 恒为真：
  //   * 用户在这页点任何 sidebar 链接都会弹"确认离开?"，而 stop 是空
  //     函数 —— 拦住了又什么都不停，纯打扰；
  //   * 返回首页按钮被打上"⏏ 退出应用"字样。
  // 也就是说 manifest 里的 demo_guard 对这张卡一直是无效配置。
  //
  // 现在改成只让 manifest 说话（framework.js 里那句"manifest 是 single
  // source of truth, 各 card 的 app.js 不用再单独 register"说的就是这个）：
  // 空闲时不再误拦，只有 AI 任务真在跑时才拦并真的 /task/stop。
  // 返回首页按钮在空闲时显示"← 返回首页"，点它的**行为不变** ——
  // framework 的 click handler 不看 isActive，只看 isDemoPage，
  // 照样清 wizard 状态 + 回首页（framework.js:749-755 的注释）。

  // Wizard finish hook. The wizard framework calls
  // `ImApp_<id>.applyWizardSnapshot(opts)` to apply the wizard's collected
  // values. USB HID has no per-user backend config (the source is fixed at
  // /dev/video0), so all we do is prefill the task card — the actual task
  // still has to be launched from the page, because starting it means
  // "start driving this person's PC", which should be a deliberate click
  // and not a side effect of finishing a wizard.
  window.ImApp_usb_hid = window.ImApp_usb_hid || {};
  window.ImApp_usb_hid.applyWizardSnapshot = async function (opts) {
    const o = opts || {};
    if (o.task) {
      const el = document.getElementById("usb-hid-task-input");
      if (el) el.value = o.task;
    }
    if (o.maxSteps != null) {
      const el = document.getElementById("usb-hid-task-max-steps");
      if (el) el.value = o.maxSteps;
    }
    return { ok: true };
  };

  UsbHid.init();
  // Re-init on hashchange in case the page was loaded lazily (e.g.
  // back-button from another app lands here without a fresh boot).
  window.addEventListener("hashchange", () => {
    if (location.hash === "#usb-hid" && !UsbHid.initialized) UsbHid.init();
  });
})();
