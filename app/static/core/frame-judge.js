/* 帧判据共享组件 —— vd / vtd / vtl / vas 四张视频卡共用。
 *
 * 一张卡一个实例（按 prefix 索引）。组件做三件事：
 *
 *   1. **配置区**：算法下拉（手动跳帧 / 判帧算法）+ 阈值 + 帧间隔，
 *      落到卡片模板里的 `<prefix>-judge-config` 容器里。改完 POST 到
 *      `/api/video/judge`（VAS 传自己的 apiBase）—— 真节流，立刻生效。
 *   2. **浮框**：视频右上角显示「这一帧判为变化还是没变化」+ 当前分数。
 *   3. **实时曲线**：视频正下方、与视频等宽的分数曲线。**只在向导里出现**
 *      —— 功能页不画（用户明确要求），进功能页就只剩配置和浮框。
 *
 * 为什么浮框 / 曲线用 position:fixed 跟着画布走，而不是塞进画布容器
 * ----------------------------------------------------------------
 * 向导会把 `<canvas>` **本身**搬进自己的容器（state.js 的
 * `moveCanvasToWizard`，按 id 推导父节点），它只搬 canvas 这一个节点，
 * 兄弟节点留在功能页里。若把浮框做成 canvas 的兄弟，向导打开时浮框就掉
 * 在被隐藏的功能页上、看不见了。这里改成每次按 `canvas.getBoundingClientRect()`
 * 把浮层摆到画布当前所在的位置 —— 画布被搬到哪、浮层就跟到哪，且完全不用
 * 改共享的 wizard 框架。
 */
(function () {
  "use strict";

  const DEFAULT_API = "/api/video/judge";
  const CURVE_H = 88;          // 曲线像素高
  const CURVE_GAP = 6;         // 曲线与画布底边的间距
  const BADGE_PAD = 10;        // 浮框距画布边缘

  /** prefix -> 实例 */
  const registry = new Map();

  const $ = (id) => document.getElementById(id);

  function ce(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  class JudgeUI {
    constructor(opts) {
      this.prefix = opts.prefix;
      this.canvasId = opts.canvasId;
      this.api = opts.apiBase || DEFAULT_API;
      this.judge = null;          // 后端判据快照
      // 配置区是否由 update() 回写。VAS 传 false：它的配置是"下次运行生效"，
      // 而每 tick 推来的是**本轮真实**判据状态 —— 两者短暂不一致是设计
      // 使然，若让 tick 覆盖控件，用户改完的阈值会被本轮的旧值悄悄抹掉。
      this.syncControls = opts.syncControls !== false;
      // 配置区是否渲染。VAS 的判据参数已固化为服务端常量（gscore/10/600），
      // 它传 false 跳过 _buildConfig()，只保留分数浮框。
      this.showConfig = opts.showConfig !== false;
      // 曲线是否创建。VAS 传 false：它的判据参数不可调，曲线没有调参价值；
      // 而向导「视频源」步打开的是共享 VideoSession 的预览（不建 VAS 的
      // 判据器），曲线只会停在「采集中…」。vd / vtd / vtl 保持默认 true。
      this.showCurve = opts.showCurve !== false;
      this.applied = true;        // 配置是否已真正作用在当前 run 上
      this._awaitSync = null;     // 待后端确认的配置（门闩，见 _acceptSync）
      this._gen = null;            // 已见过的最新配置代次（过期快照直接丢）
      this._appliedSig = null;     // 上次 POST 出去的配置，相同则不重复发
      this._metrics = [];          // 服务端放行的算法名（下拉据此剪枝）
      this._built = false;
    }

    // ---------------------------------------------------------------- //
    mount() {
      if (this._built) return;
      this._built = true;
      if (this.showConfig) this._buildConfig();
      this._buildFloats();
      this._load();
      // 画布位置会因向导搬运 / 窗口缩放 / 滚动而变，三路都重摆一次。
      window.addEventListener("resize", () => this.sync());
      window.addEventListener("scroll", () => this.sync(), true);
      this._timer = setInterval(() => this.sync(), 500);
    }

    destroy() {
      clearInterval(this._timer);
      this._built = false;
    }

    // ---------------------------------------------------------------- //
    // 1. 配置区
    // ---------------------------------------------------------------- //
    _buildConfig() {
      const host = $(this.prefix + "-judge-config");
      if (!host) return;
      host.innerHTML = "";
      host.classList.add("vj-config");
      // 宿主标了 `data-judge-layout="inline"` 就启用横向模式。用宿主自己的
      // **属性**做开关，而不是认某张卡的 class 名（认 class 会把共享组件
      // 绑死在某一张卡上：下一张卡把判据栏也摊到视频下方时，还得再来改这里）。
      // 判据长什么样由**它所在的位置**决定 —— 塞在窄配置栏里的竖排，摊在
      // 视频正下方有一整行宽度的横排。
      if (host.dataset.judgeLayout === "inline") {
        host.classList.add("vj-inline");
      }

      const row1 = ce("div", "vj-row");
      const mLabel = ce("label", "vj-label");
      mLabel.appendChild(ce("span", null, "判帧算法"));
      this.selMode = ce("select", "vj-select");
      this.selMode.id = this.prefix + "-judge-mode";
      mLabel.appendChild(this.selMode);
      row1.appendChild(mLabel);
      host.appendChild(row1);

      const row2 = ce("div", "vj-row");
      this.inpThr = ce("input", "vj-input");
      this.inpThr.type = "number";
      this.inpThr.id = this.prefix + "-judge-threshold";
      this.inpThr.min = "0.1"; this.inpThr.step = "0.1";
      // **不预填常数**。阈值刻度按算法不同（z_mad 是灰阶口径 6.0、conc 是
      // 比值口径 15），这里写死一个数就会在换了度量之后悄悄失效 —— 而失效
      // 表现为"阈值不对"，不报错。真正的值由 /judge/metrics 的
      // default_threshold 回填（见 _fillModes），或由服务端快照覆盖
      // （见 setConfig）；两者都没到之前保持空白，_apply() 会用
      // _defaultThreshold() 兜底。
      this.inpThr.placeholder = "默认由后端给定";
      const tLabel = ce("label", "vj-label");
      tLabel.appendChild(ce("span", null, "阈值"));
      tLabel.appendChild(this.inpThr);
      row2.appendChild(tLabel);

      this.inpEvery = ce("input", "vj-input");
      this.inpEvery.type = "number";
      this.inpEvery.id = this.prefix + "-judge-every-n";
      this.inpEvery.min = "1"; this.inpEvery.max = "3600"; this.inpEvery.step = "1";
      // 与后端 ``FrameJudge.__init__`` 默认 600 保持一致 —— 之前是 30，与后端
      // 不一致的后果是：新会话走 ``_fallbackCfg`` 兜底时拿到 30 而不是后端
      // 想要的 600，而用户没改配置就提交，服务端用 default 600，前端却显示 30，
      // 两边对不上浮框上 every_n_eff 的口径。
      this.inpEvery.value = "600";
      this._everyN = 600;                 // 提示文案要用，跟控件保持一致
      const eLabel = ce("label", "vj-label");
      eLabel.appendChild(ce("span", null, "帧间隔"));
      eLabel.appendChild(this.inpEvery);
      row2.appendChild(eLabel);
      host.appendChild(row2);

      this.hint = ce("div", "vj-hint", "手动跳帧：固定间隔送检，不做画面比对。");
      host.appendChild(this.hint);

      this.selMode?.addEventListener("change", () => this._apply());
      this.inpThr?.addEventListener("change", () => this._apply());
      this.inpEvery?.addEventListener("change", () => this._apply());
    }

    async _load() {
      try {
        const r = await fetch(this.api + "/metrics", { cache: "no-store" });
        if (r.ok) this._fillModes(await r.json());
      } catch (_) { /* 后端没起时留空下拉 */ }
      try {
        const r = await fetch(this.api, { cache: "no-store" });
        if (r.ok) this.update(await r.json());
      } catch (_) { /* 同上 */ }
    }

    _fillModes(d) {
      if (!this.selMode) return;
      const metrics = d.metrics || [];
      const cost = d.cost_ms || {};
      // 每个度量的默认阈值由后端给（刻度按算法不同，前端写死必然过期）
      this._defThr = d.default_threshold || {};
      this._metrics = metrics;
      // 阈值控件留空时用后端给的默认值回填，免得用户什么都没动就看到一个
      // 空框然后误以为是没配好。
      if (this.inpThr && !String(this.inpThr.value).trim() && metrics.length) {
        this.inpThr.placeholder = `默认 ${this._defaultThreshold(metrics[0])}`;
      }
      this.selMode.innerHTML = "";
      const manual = ce("option", null, "手动跳帧（固定间隔）");
      manual.value = "manual";
      this.selMode.appendChild(manual);
      metrics.forEach((m) => {
        // 标注**实测**的单帧成本。写死一个数会在改了缩略图长边 / 换分辨率
        // 之后悄悄过期，而"运行一次的时间"这种标注一旦不准就是在骗人 ——
        // 所以数字由后端实测（frame_judge.cost_ms），前端只负责显示。
        const ms = cost[m];
        const o = ce("option", null,
          ms == null ? m : `${m}（约 ${(+ms).toFixed(ms < 1 ? 2 : 1)} ms/帧）`);
        o.value = m;
        this.selMode.appendChild(o);
      });
      this._pruneModes();
    }

    /**
     * 某模式的默认阈值。
     *
     * `/judge/metrics` 没回来之前（后端没起 / 请求失败）只能退到一个
     * 常数兜底，但**兜底值只用于那几毫秒的窗口**，正常路径一律以服务端
     * `snapshot().threshold` 为准 —— 前端存一个自己的"默认"只会和服务端
     * 慢慢漂移。阈值刻度按度量不同（z_mad 灰阶 6.0 / conc 比值 15.0）。
     */
    _defaultThreshold(mode) {
      const m = String(mode || "");
      if (m !== "manual" && this._defThr && this._defThr[m] != null) {
        return this._defThr[m];
      }
      const first = this._metrics && this._metrics.length ? this._metrics[0] : null;
      if (first && this._defThr && this._defThr[first] != null) {
        return this._defThr[first];
      }
      return 15;
    }

    /**
     * 摘掉下拉里"已下线的算法"选项。
     *
     * 用户 localStorage 里可能存着上一版选过的算法（`z_mad` / `sad` /
     * `ncc`；wizard 的字段值按 field.id 持久化，配置又收窄过）。`setConfig`
     * 为了兼容"下拉还没被 /metrics 填完"的竞态会临时补同名 option，
     * 补完就没人清理了 —— 服务端早已把未知算法回落到 manual，下拉里
     * 却还挂着一个选不上的幽灵项。
     */
    _pruneModes() {
      if (!this.selMode || !this._metrics || !this._metrics.length) return;
      const keep = ["manual"].concat(this._metrics);
      Array.from(this.selMode.options).forEach((o) => {
        if (keep.indexOf(o.value) < 0) o.remove();
      });
    }

    /** 把控件当前值推到后端（真节流）。 */
    async _apply() {
      // 无配置区（showConfig:false，如 VAS 固化判据）时不要 POST ——
      // 否则会用这里的兜底值（manual/30）把服务端判据改掉。
      if (!this.selMode) return;
      const mode = this.selMode?.value || "manual";
      const every = Math.max(1, parseInt(this.inpEvery?.value || "30", 10) || 30);
      const dflt = this._defaultThreshold(mode);
      const thr = Math.max(0.1, parseFloat(this.inpThr?.value) || dflt);
      if (this.inpEvery) {
        this.inpEvery.value = String(every);
        this._everyN = every;
      }
      if (this.inpThr) this.inpThr.value = String(thr);
      this._paintMode(mode);
      // 上门闩：POST 之前飞出去的旧快照不能把刚设的值改回去（见 _acceptSync）。
      this._awaitSync = { mode, every_n: every, threshold: thr };
      // change 与 blur 各触发一次同一个值；配置没变就别重复 POST。
      // v0.23 多路：选了一路 → 只发 channels[idx]；没选 → 顶层下发到所有路。
      // 顶层字段**绝对不能**和 channels 同时发：后端把顶层当成"所有路默认"，
      // 选了一路后只想改 ch0 的 threshold=5.5，却把 5.5 广播给了 ch1/2/3 —— 实测
      // 4 路下改 ch0 一并把另外 3 路也改了的根因。
      const ch = this._selectedChannel;
      const sig = (ch == null ? "*" : "#" + ch) + "|" + mode + "|" + every + "|" + thr;
      if (sig === this._appliedSig) return;
      this._appliedSig = sig;
      const body = ch != null
        ? { channels: { [ch]: { mode, every_n: every, threshold: thr } } }
        : { mode, every_n: every, threshold: thr };
      try {
        const r = await fetch(this.api, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (r.ok) {
          this.update(await r.json());
          // 服务端可能把已下线的算法回落成 manual；顺手把幽灵 option 清掉
          this._pruneModes();
        }
      } catch (_) { /* 离线：控件仍保持用户填的值 */ }
    }

    /** 向导 / 外部直接给一份配置（不触发 POST，只同步控件）。 */
    setConfig(o) {
      if (!o) return;
      if (o.mode != null && this.selMode) {
        // 下拉可能还没被 /metrics 填完，先补一个同名 option
        if (![...this.selMode.options].some((x) => x.value === o.mode)) {
          const opt = ce("option", null, o.mode);
          opt.value = o.mode;
          this.selMode.appendChild(opt);
        }
        this.selMode.value = o.mode;
      }
      if (o.every_n != null && this.inpEvery) {
        this.inpEvery.value = String(o.every_n);
        this._everyN = o.every_n;
      }
      if (o.threshold != null && this.inpThr) this.inpThr.value = String(o.threshold);
      this._paintMode(this.selMode?.value || "manual");
    }

    _paintMode(mode) {
      const manual = mode === "manual";
      if (this.inpThr) this.inpThr.disabled = manual;
      this._mode = manual ? "manual" : mode;
      this._paintHint();
    }

    _paintHint() {
      if (!this.hint) return;
      const mode = this._mode || "manual";
      // 措辞跟着实际判定走。兜底**不是**无条件送检：要求分数至少到阈值
      // 的一半，所以"帧间隔"是"有变化时的最长间隔"，不是"最长 N 帧
      // 一定送一次" —— 静止画面会一直安静下去（省电）。
      let txt = mode === "manual"
        ? "手动跳帧：固定间隔送检，不做画面比对。"
        : `按「${mode}」比对画面，超过阈值立刻送检；变化达到阈值一半时，`
          + `最长 ${this._everyN || 30} 帧兜底送一次，画面完全没变就一直不送。`;
      if (!this.applied) txt += "（本轮分析进行中，本次修改从下次开始生效）";
      this.hint.textContent = txt;
    }

    // ---------------------------------------------------------------- //
    // 2. 浮框 + 3. 曲线（都挂在 body 上，跟随画布矩形）
    // ---------------------------------------------------------------- //
    _buildFloats() {
      // v0.23 起判据角标走每路 tile.badge（multi-grid.js 提供）。
      // 全局 vj-badge 仍保留 DOM 节点（兼容旧卡片引用），但默认隐藏、
      // 不再 sync() 摆位 —— 否则会盖在画面右下与 tile.badge 重叠。
      const badge = ce("div", "vj-badge");
      badge.id = this.prefix + "-judge-badge";
      badge.style.display = "none";
      badge.innerHTML =
        '<span class="vj-badge-main">' +
        '<span class="vj-dot"></span>' +
        '<span class="vj-badge-txt">--</span>' +
        '<span class="vj-badge-num"></span>' +
        '</span><span class="vj-badge-sub"></span>';
      badge.style.display = "none";
      document.body.appendChild(badge);
      this.badge = badge;
      this.badgeTxt = badge.querySelector(".vj-badge-txt");
      this.badgeNum = badge.querySelector(".vj-badge-num");
      this.badgeSub = badge.querySelector(".vj-badge-sub");

      if (this.showCurve) {
        const curve = ce("div", "vj-curve");
        curve.id = this.prefix + "-judge-curve-wrap";
        curve.style.display = "none";
        const cv = ce("canvas", "vj-curve-canvas");
        curve.appendChild(cv);
        curve.appendChild(ce("div", "vj-curve-hint", "实时分数曲线"));
        document.body.appendChild(curve);
        this.curve = curve;
        this.curveCanvas = cv;
        this.curveCtx = cv.getContext("2d");
      }
    }

    /**
     * 曲线是 `position: fixed`，**不占文档流** —— 向导里视频正下方紧跟着
     * 就是判据控件（算法下拉 / 阈值 / 帧间隔），两者抢同一块屏幕：曲线画在
     * 控件上面，控件被盖住点不动（实测 `elementFromPoint` 命中的是曲线
     * canvas）。
     *
     * 在 canvas 后面插一个等高占位块，把后面的控件顶到曲线下方，两者各就
     * 各位。只在向导里需要（功能页不画曲线），所以占位块也只在向导里出现。
     */
    _reserve(on, canvas) {
      const c_closestWrap = (cv) => cv.closest && cv.closest(".wd-wizard-canvas-wrap");
      if (!this.spacer) {
        this.spacer = document.querySelector(".vj-curve-spacer");
        if (!this.spacer) this.spacer = ce("div", "vj-curve-spacer");
      }
      if (!on) { this.spacer.style.display = "none"; return; }
      // 占位块插在 **canvas-wrap 外面**：canvas-wrap 是 flex 容器
      // (`display:flex; align-items:center`)，插进去会跟画布并排排成一行，
      // 一点高度都顶不出来。放到它的父级里才是正常的块级占位。
      const wrap = c_closestWrap(canvas);
      const parent = wrap ? wrap.parentNode : canvas.parentNode;
      if (!parent) { this.spacer.style.display = "none"; return; }
      const anchor = wrap || canvas;
      // 重复 mount（切页再进）时先摘掉旧的，避免占位块越堆越多
      document.querySelectorAll(".vj-curve-spacer").forEach((n) => {
        if (n !== this.spacer) n.remove();
      });
      if (this.spacer.parentNode !== parent || this.spacer.previousElementSibling !== anchor) {
        parent.insertBefore(this.spacer, anchor.nextSibling);
      }
      this.spacer.style.height = (CURVE_H + CURVE_GAP) + "px";
      this.spacer.style.display = "";
    }

    /**
     * 把浮层摆到画布当前位置。
     *
     * 关键：**只认画布在视口里可见的那一段**。两个浮层都是 fixed、按画布
     * 矩形摆位，画布一旦滚出视野，它们就会飘到画布下方那些无关控件上面
     * —— 实测中曲线正好压住了视频源步里的判据下拉框，点都点不动。所以：
     *   * 画布可见高度不足 / 横向出屏 → 两个都藏；
     *   * 画布上沿滚出视野 → 浮框贴着**可见上沿**摆，不飘到视口外；
     *   * 画布下沿之下放不下整条曲线 → 曲线藏起来，绝不画到别人的位置上。
     */
    sync() {
      const c = $(this.canvasId);
      const r = c ? c.getBoundingClientRect() : null;
      const hide = () => {
        if (this.badge) this.badge.style.display = "none";
        if (this.curve) this.curve.style.display = "none";
      };
      // **fieldset 内曲线**：不论 canvas 0 高度都显示。frame-judge 在
      // wizard 里**唯一**关心的是"曲线当前在哪"，跟画布是否可见无关
      // —— 因为它的容器已经跟画布解耦了（fieldset 始终在文档流里）。
      // canvas 不存在 / 0 尺寸时只调 _drawCurve（自适应容器宽度），
      // 不 hide。tag: v0.25 wizard 曲线搬入判据框。
      const inFs = this.curve && this.curve.parentNode &&
        this.curve.parentNode.id &&
        this.curve.parentNode.id.endsWith("-wizard-judge-fs");
      if (inFs) {
        if (this.curveCanvas.height !== CURVE_H) {
          this.curveCanvas.height = CURVE_H;
        }
        this.curve.style.display = "";
        this._drawCurve();
        return;
      }
      if (!r || r.width < 4 || r.height < 4) { hide(); return; }
      const vw = window.innerWidth, vh = window.innerHeight;
      if (r.right <= 0 || r.left >= vw) { hide(); return; }     // 横向滚出屏幕
      const visTop = Math.max(r.top, 0);
      const visBottom = Math.min(r.bottom, vh);
      if (visBottom - visTop < 24) { hide(); return; }          // 几乎滚没了

      // v0.23: 不再 show 全局 badge —— 每路 tile.badge 接管。
      // 保留 this.badge DOM 节点是为了 setBadge(idx) 旧卡片 / 测试仍能拿到元素。
      if (false && this.badge) {  // disabled
        this.badge.style.display = "";
        this.badge.style.left = (r.right - this.badge.offsetWidth - BADGE_PAD) + "px";
        // 贴画布**下沿**而不是上沿：右上角是各卡检测结果的落点（视频目标
        // 框 / 描述文案都从那儿起），浮框摆上沿会直接压住它们。下沿是
        // 画面本体，通常是空的；压到视频上也只是半透明浮层，不像上沿
        // 那样和结果文字抢位置。
        //
        // 画布下沿滚出视野时退到**可见下沿**，再兜一次视口 —— 否则浮框
        // 会飘到画布下方那些无关控件上面。
        const visBot = Math.min(r.bottom, vh);
        let top = visBot - this.badge.offsetHeight - BADGE_PAD;
        if (top < visTop + BADGE_PAD) top = visTop + BADGE_PAD;
        this.badge.style.top = top + "px";
      }
      // 曲线**只在向导里**：向导会把 canvas 搬进 .wd-wizard-canvas-wrap。
      if (this.curve) {
        const inWizard = !!c.closest(".wd-wizard-canvas-wrap");
        if (!inWizard) { this.curve.style.display = "none"; this._reserve(false, c); return; }
        // **已搬到 fieldset 里**（曲线 parent === 该 prefix 的 -wizard-judge-fs）：
        // 不再做 fixed 摆位。容器宽度 = fieldset 宽度，跟着页面流式布局走；
        // 高度固定 CURVE_H。每次 sync 只重画 + 自适应容器宽。_reserve 也不
        // 需要了 —— fieldset 已经把位置管好了。
        const fs = c.closest(".wd-wizard-root-video-detection, .wd-wizard-root-video-target-detection, .wd-wizard-root-video-target-localization");
        const inFieldset = fs && this.curve.parentNode
          && this.curve.parentNode.id && this.curve.parentNode.id.endsWith("-wizard-judge-fs");
        if (inFieldset) {
          if (this.curveCanvas.height !== CURVE_H) {
            this.curveCanvas.height = CURVE_H;
          }
          this.curve.style.display = "";
          this._drawCurve();
          return;
        }
        // **legacy 路径**：曲线还在 body（早期 wizard state.js 没搬到 fieldset
        // 之前的中间状态；以及非 multi-card 的老 wizard 用法 —— VAS 等）——
        // 仍按 fixed 摆位，跟 canvas 下沿贴合。
        if (r.bottom <= 0) {
          this.curve.style.display = "none";
          this._reserve(true, c);
          return;
        }
        const top = r.bottom + CURVE_GAP;
        this.curve.style.display = "";
        const w = Math.round(r.width);
        if (this.curveCanvas.width !== w) this.curveCanvas.width = w;
        if (this.curveCanvas.height !== CURVE_H) this.curveCanvas.height = CURVE_H;
        this.curve.style.left = r.left + "px";
        this.curve.style.top = top + "px";
        this.curve.style.width = w + "px";
        this._reserve(true, c);
        this._drawCurve();
      }
    }

    // ---------------------------------------------------------------- //
    /** 后端每次 WS tick 带来一份判据快照。 */
    update(j) {
      if (!j) return;
      // **过期快照直接丢**。WS / HTTP 的传输顺序不等于计算顺序：用户在
      // 页面上把判帧算法改成 manual 之后，POST 之前就已经发出的那几份
      // 旧快照还会陆续到达（快照是在发送那一刻算好的）。若照单全收，
      // 控件会在两个模式之间来回跳。判据状态带 gen（配置代次，
      // 服务端单调递增），比本地已知的旧就直接 return。
      if (typeof j.gen === "number") {
        if (this._gen != null && j.gen < this._gen) return;
        this._gen = j.gen;
      }
      this.judge = j;
      if (j.applied === false) this.applied = false;

      // v0.23 多路：按路把分值喂到 tile.badge。
      // 顶层 j 仍是聚合视图（cur / changed / per_channel 数组都存在）；
      // 兼容 v0.22 单路：per_channel 空 → 用顶层。
      const per = Array.isArray(j.per_channel) && j.per_channel.length
        ? j.per_channel : [{channel: 0, cur: j.cur, changed: j.changed,
                            mode: j.mode, every_n: j.every_n, threshold: j.threshold}];
      this._paintPerChannel(per);

      // 控件只反映**当前选中路**的状态。v0.22 单路 / 未选路时用顶层。
      const sel = this._selectedChannel;
      const src = sel != null
        ? per.find((p) => p.channel === sel) || j
        : j;
      const jsel = {
        mode: src.mode || j.mode,
        every_n: src.every_n || j.every_n,
        threshold: src.threshold != null ? src.threshold : j.threshold,
        cur: src.cur,
        changed: src.changed,
      };
      if (this.syncControls &&
          document.activeElement !== this.selMode &&
          document.activeElement !== this.inpThr &&
          document.activeElement !== this.inpEvery &&
          this._acceptSync(jsel)) {
        this.setConfig({ mode: jsel.mode, every_n: jsel.every_n,
                         threshold: jsel.threshold });
      }
      this._paintHint();
      // 不再调 sync() 摆全局 badge —— v0.23 起 badge 走 per-tile DOM
    }

    /**
     * v0.23 多路：cards 侧 tile click → setSelectedChannel(idx)
     * 1) 调 MultiGrid.setSelected 加 .selected 蓝框
     * 2) 设 this._selectedChannel，下一次 update() 会按这路回写控件
     * 3) 触发 onSelectChannel 钩子（cards 侧更新"当前是 [x, y] 路"提示）
     */
    setSelectedChannel(idx) {
      this._selectedChannel = (idx == null) ? null : idx;
      const MG = window.MultiGrid;
      if (MG && this.canvasId) MG.setSelected(this.canvasId, idx);
      if (typeof this._onSelectChannel === "function") {
        this._onSelectChannel(idx);
      }
      // 选路切了 → 立刻按新路重画曲线（_drawCurve 已按 _selectedChannel 过滤
      // recent）。update() 也会调 _drawCurve 但那是 ~1Hz WS tick，用户点完
      // tile 不想等那一秒。
      if (this.curve && this.curve.style.display !== "none" &&
          typeof this._drawCurve === "function") {
        this._drawCurve();
      }
    }

    /**
     * 把 per_channel 数据写到对应 tile 的 badge 上。
     * 通过 window.MultiGrid.setBadge 注入。per_channel 是 [{channel,
     * cur, changed, ...}, ...]；空 / 未启动判据 → 设 null 让 tile 角标藏起来。
     */
    _paintPerChannel(per) {
      const MG = window.MultiGrid;
      if (!MG) return;
      const gridId = this.canvasId;  // 多路下 canvasId 是 grid id
      for (const p of per) {
        const state = p.cur == null ? null
                    : p.changed ? "changed" : "idle";
        MG.setBadge(gridId, p.channel, { state, score: p.cur });
      }
    }

    /**
     * `_awaitSync` 期间**不**用 tick 回写控件 —— 防的是"刚 POST 的新值被
     * 同一 gen 的旧快照覆盖"。带 gen 的过期快照已在 update() 开头拦掉；
     * 这里补的是 gen 缺席的场景（VAS 的配置视图不带 gen）。
     */
    _acceptSync(j) {
      if (!this._awaitSync) return true;
      const a = this._awaitSync;
      if (j.mode === a.mode && Number(j.every_n) === Number(a.every_n)
          && Math.abs(Number(j.threshold) - Number(a.threshold)) < 1e-6) {
        this._awaitSync = null;      // 后端已确认，清门闩
        return true;
      }
      return false;
    }

    _paintBadge(j) {
      const manual = j.manual;
      const on = !manual && j.changed;
      const cur = j.cur;
      this.badge.classList.toggle("on", !!on);
      this.badge.classList.toggle("manual", !!manual);
      if (manual) {
        this.badgeTxt.textContent = "手动跳帧";
        this.badgeNum.textContent = `每 ${j.every_n} 帧送检`;
        this.badgeSub.textContent =
          `${j.mode} · 固定间隔 · 不比对画面`;
        return;
      }
      this.badgeTxt.textContent = on ? "画面变化" : "无变化";
      this.badgeNum.textContent = cur != null ? (+cur).toFixed(2) : "--";
      // **主行是"当前这一帧"的判定，绝大多数帧都是"无变化"** —— 用户盯着
      // 浮框看只会得到"判据没生效"的错误结论，而实际上检测器可能正忙成一团。
      // 所以副行必须报**实测送检间隔**：它直接回答"我把帧间隔设成 300，
      // 为什么还在一秒钟一次"。实测间隔远小于兜底间隔 = 阈值把几乎每一帧
      // 都放行了、兜底根本轮不上（材料噪声底高于阈值时就是这个形态）。
      const eff = j.every_n_eff;
      const effTxt = (eff != null && eff < 1e9)
        ? `实际每 ${eff >= 100 ? Math.round(eff) : eff.toFixed(1)} 帧送检`
        : "实际每 -- 帧送检";
      const starve = (eff != null && j.every_n > 0 && eff <= Math.max(2, j.every_n / 5));
      this.badge.classList.toggle("warn", !!starve);
      this.badgeSub.textContent =
        `${j.mode} · 阈值 ${(+j.threshold).toFixed(2)} · ${effTxt}`
        + (starve ? ` · 兜底 ${j.every_n} 帧形同虚设` : ` · 兜底 ${j.every_n} 帧`);
    }

    _drawCurve() {
      // showCurve:false（VAS）时不创建曲线 DOM —— 这里及静态 draw() 的
      // 调用点都可能是无条件的，统一在入口短路。
      if (!this.curve || !this.curveCanvas || !this.curveCtx) return;
      const ctx = this.curveCtx;
      const cv = this.curveCanvas;
      // 自适应容器宽度：曲线在 fieldset 里时容器是 fieldset，在画布下
      // 时容器宽 = 画布宽。优先读 offsetParent（fieldset），fallback 到
      // canvasId 元素本身（功能页路径）。
      const host = this.curve.offsetParent && this.curve.offsetParent.id
        ? this.curve.offsetParent
        : ($(this.canvasId) || this.curve.offsetParent);
      const targetW = host ? host.clientWidth : cv.width;
      const W = (targetW > 0 ? targetW : 640) - 12;  // 留 6px padding
      const H = cv.height;
      if (cv.width !== W) cv.width = W;
      ctx.clearRect(0, 0, W, H);
      const j = this.judge;
      if (!j) return;
      let pts = j.recent || [];
      // **按选中路过滤**：recent 形如 [pos, score, ch_idx]（multi_session
      // judge_snapshot 里 extend 时加的 ch.idx），点哪路就画哪路的曲线。
      // 未选路（v0.22 单路兼容 / 还没点 tile）→ 用顶层（ch_idx === undefined
      // 兜底：全显示 —— 旧 v0.22 模式 per_channel 是空数组，recent 也没
      // ch_idx，过滤后剩空，画 "采集中…" 提示就行）。
      const ch = this._selectedChannel;
      if (ch != null) {
        pts = pts.filter((p) => Number(p[2]) === Number(ch));
      } else {
        // 未选路：只保留没有 ch_idx 标记的（旧格式 / 测试 stub）
        pts = pts.filter((p) => p[2] == null);
      }
      if (j.manual || pts.length < 2) {
        ctx.fillStyle = "#94a3b8";
        ctx.font = "12px sans-serif";
        const label = j.manual
          ? "手动跳帧：不比对画面，无分数曲线"
          : (ch != null ? "通道 " + (ch + 1) + " 采集中…" : "采集中…");
        ctx.fillText(label, 10, H / 2);
        return;
      }
      // 阈值参考线用 **当前选中路** 的阈值（如果 per 里能找到）；找不到
      // 退到顶层 proto 阈值。
      const perSrc = (Array.isArray(j.per_channel) ? j.per_channel : [])
        .find((p) => Number(p.channel) === Number(ch));
      const thr = perSrc && perSrc.threshold != null ? perSrc.threshold
              : (j.threshold != null ? j.threshold : 0);
      let ymax = Math.max(...pts.map((p) => p[1]), thr * 1.2, 0.5) * 1.1;
      const xOf = (i) => (i / Math.max(pts.length - 1, 1)) * (W - 6) + 3;
      const yOf = (v) => H - 16 - (v / ymax) * (H - 26);

      // 阈值参考线
      ctx.strokeStyle = "#dc2626";
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(0, yOf(thr));
      ctx.lineTo(W, yOf(thr));
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = "#dc2626";
      ctx.font = "10px sans-serif";
      ctx.fillText("阈值 " + (+thr).toFixed(2),
                   2, Math.max(9, yOf(thr) - 3));

      // 分数折线
      ctx.strokeStyle = "#2563eb";
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      pts.forEach((p, i) => {
        const x = xOf(i), y = yOf(p[1]);
        i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      });
      ctx.stroke();

      ctx.fillStyle = "#64748b";
      ctx.font = "10px sans-serif";
      ctx.fillText("通道 " + (ch + 1) + " · " + pts[0][0] + " 帧", 3, H - 3);
      const last = pts[pts.length - 1];
      ctx.fillText(pts.length + " 点 · 当前 " + (+last[1]).toFixed(2), W - 130, H - 3);
    }
  }

  /**
   * 向导里改了判据字段 → 立刻落库（曲线就在向导的视频源步里，用户改阈值
   * 却要等「完成配置」才看到效果，说不通）。
   *
   * 靠 **字段 id 前缀** 认领：向导字段 id 是 `<prefix>-judge-*`，谁的前缀
   * 对上就是谁的配置。同一时刻只有一张卡的向导挂着，所以不会串味；而组件
   * 因此完全不需要知道 appId / 卡片清单（保持零耦合）。
   */
  /**
   * 控件还没填过时的兜底配置。
   *
   * 阈值必须在这里就解析出具体数值，不能留 undefined —— 上游会把它原样
   * 序列化进启动请求体，一个 `threshold: undefined` 到后端就变成"用默认"
   * 或校验失败，而调用方拿不到任何提示。
   */
  function _fallbackCfg(prefix, mode) {
    const m = mode || "manual";
    const inst = registry.get(prefix);
    const thr = inst && inst._defaultThreshold
      ? inst._defaultThreshold(m) : 20;
    return { mode: m, every_n: 600, threshold: thr };
  }

  function _onWizardFieldChange(e) {
    const d = e && e.detail;
    if (!d || !d.values) return;
    const v = d.values;
    registry.forEach((inst, prefix) => {
      const key = (k) => prefix + "-judge-" + k;
      const pick = (x) => (x == null || x === "" ? null : x);
      const mode = pick(v[key("mode")]);
      const thr = pick(v[key("threshold")]);
      const every = pick(v[key("every-n")]);
      if (mode == null && thr == null && every == null) return;
      const cur = window.FrameJudge.config(prefix) || _fallbackCfg(prefix, mode);
      window.FrameJudge.applyConfig(prefix, {
        mode: mode || cur.mode,
        every_n: every ?? cur.every_n,
        threshold: thr ?? cur.threshold,
      }).catch(() => { /* applyConfig 内部已兜 */ });
    });
  }
  window.addEventListener("wizard:field-change", _onWizardFieldChange);

  // ---------------------------------------------------------------- //
  // 对外接口（框架无关，供各卡片 app.js 直接调）
  // ---------------------------------------------------------------- //
  window.FrameJudge = {
    mount(opts) {
      const inst = new JudgeUI(opts);
      registry.set(opts.prefix, inst);
      inst.mount();
      return inst;
    },
    /** WS tick 调：把 snap.judge 喂进来。 */
    update(prefix, judge) {
      const inst = registry.get(prefix);
      if (inst) inst.update(judge);
    },
    /** applyWizardSnapshot 调：把 wizard 字段同步到控件。 */
    setConfig(prefix, cfg) {
      const inst = registry.get(prefix);
      if (inst) inst.setConfig(cfg);
    },
    /** 向导完成时用：直接推一份配置到后端（真节流）。 */
    async applyConfig(prefix, cfg) {
      const inst = registry.get(prefix);
      if (!inst) return false;
      inst.setConfig(cfg);
      await inst._apply();
      return true;
    },
    /**
     * v0.23 多路：cards 侧 tile click → 切"当前在设置的路"。
     * 1) inst.setSelectedChannel(idx) 调 MultiGrid.setSelected 高亮蓝框
     * 2) 设 _selectedChannel，下一次 update() 按这路回写控件
     * 3) 触发 _onSelectChannel 钩子（cards 侧更新"当前是 [x, y] 路"提示）
     *
     * `null` = 清除选中（v0.22 单路兼容模式）
     */
    setSelectedChannel(prefix, idx) {
      const inst = registry.get(prefix);
      if (inst) inst.setSelectedChannel(idx);
    },
    /** 注册 tile click 钩子（cards 侧传一个 (idx)=>void） */
    setOnSelectChannel(prefix, handler) {
      const inst = registry.get(prefix);
      if (inst) inst._onSelectChannel = handler;
    },
    /** 取当前选中路 idx（cards 侧读用） */
    getSelectedChannel(prefix) {
      const inst = registry.get(prefix);
      return inst ? inst._selectedChannel : null;
    },
    /**
     * 把实时分数曲线搬进 wizard 的判据 fieldset 里。
     *
     * 调用时机：wizard state.js 在 fieldset 挂上 ``<prefix>-wizard-judge-fs``
     * id 之后调一次（短轮询保证 FrameJudge 已就绪）。曲线原本是
     * position:fixed 浮在画布下面，进了 wizard 之后画布是 wizard canvas-wrap
     * 里的多路 grid，曲线不能再贴画布下沿 —— 改成判据框内的"普通流式布局"，
     * 跟"当前在编辑 [x, y] 路" 一起显示。
     *
     * 搬完之后 _drawCurve 会按 inst._selectedChannel 过滤 recent
     * （每条 recent 形如 ``[pos, score, ch_idx]``，后端 judge_snapshot 里
     * 已经把通道号塞进点里），切路时 setSelectedChannel 触发重画。
     */
    mountInWizard(prefix) {
      const inst = registry.get(prefix);
      if (!inst || !inst.curve) return false;
      const fs = document.getElementById(prefix + "-wizard-judge-fs");
      if (!fs) return false;
      if (inst.curve.parentNode === fs) {
        inst.curve.style.display = "";
        return true;
      }
      // 从 body 搬到 fieldset。position/left/top/width 由 frame-judge.css 里
      // 的 ``.wd-wizard-judge-fs > .vj-curve`` 选择器统一覆盖为 static +
      // 100% 宽 —— 不在这里写 inline style，避免内联样式压过 CSS 选择器
      // （外部调试 / 改皮肤时只动 CSS 即可）。
      fs.appendChild(inst.curve);
      inst.curve.style.display = "";
      // 重画一次 —— 容器宽度变了 + 已有 j.recent 数据
      if (typeof inst._drawCurve === "function") inst._drawCurve();
      return true;
    },
    /**
     * 向导完成时调用：把 wizard 的判据字段（judgeMode / judgeThreshold /
     * judgeEveryN）推给后端。没填的字段沿用控件当前值 —— 向导里留空不是
     * "用 0"，而是"用户没管这项"。
     *
     * 抽成共享方法而不是三张卡各写一遍：vd / vtd / vtl 共用同一个
     * VideoSession，判据只有一份，写三遍必然漂移。
     */
    async fromWizard(prefix, opts) {
      if (!opts) return null;
      const pick = (v) => (v == null || v === "" ? null : v);
      const mode = pick(opts.judgeMode);
      const every = pick(opts.judgeEveryN);
      const thr = pick(opts.judgeThreshold);
      if (mode == null && every == null && thr == null) return null;
      const cur = this.config(prefix) || _fallbackCfg(prefix, mode);
      const cfg = {
        mode: mode || cur.mode,
        every_n: every ?? cur.every_n,
        threshold: thr ?? cur.threshold,
      };
      await this.applyConfig(prefix, cfg);
      return cfg;
    },
    get(prefix) { return registry.get(prefix); },
    /**
     * 当前控件上的判据配置。卡片把它拼进自己的启动请求体
     * （VAS 的 /analyze 就是这么拿到 sample_mode / every_n /
     * change_threshold 的）—— 组件自己已经在 POST 时落库，这里只是让
     * 调用方不用再各自去读 DOM id。
     */
    config(prefix) {
      const inst = registry.get(prefix);
      if (!inst) return null;
      const mode = inst.selMode?.value || "manual";
      const every = Math.max(1, parseInt(inst.inpEvery?.value || "30", 10) || 30);
      const thr = Math.max(0.1, parseFloat(inst.inpThr?.value)
        || (registry.get(prefix)?._defaultThreshold(mode) ?? 15));
      return { mode, every_n: every, threshold: thr };
    },

    /**
     * 事件历史行里的「当前帧变化值」节点（无分数时返回 null）。
     *
     * 放在共享组件里而不是各卡各写一遍：vd / vtd / vtl **共用同一份判据**
     * （同一个 VideoSession），只在一张卡显示分数、另两张不显示，用户看到
     * 同样的配置却在不同页面拿到不同信息，那比不显示更难排查。
     *
     * 按**当前阈值**分成两类，因为这是回答"阈值以下为什么还出事件"的唯一
     * 依据，两种情况的处置完全不同：
     *   * `score > threshold` → 超阈值触发（橙）：阈值对这条素材偏低。
     *   * `score <= threshold` → 兜底触发（灰）：兜底条件是
     *     `gap >= every_n && score >= threshold/2`，**本来就允许阈值以下的
     *     帧送检**，属设计内。看到灰色该调的是「帧间隔」或换算法，而不是
     *     继续抬阈值。
     *
     * 阈值取自 `config(prefix)`（控件当前值），不另存一份 —— 存一份就会
     * 和用户刚改的值对不上。
     */
    scoreSpan(prefix, ev) {
      const s = (ev && typeof ev.judge_score === "number") ? ev.judge_score : null;
      if (s == null) return null;
      const cfg = this.config(prefix);
      const thr = cfg ? Number(cfg.threshold) : null;
      const over = (thr == null) ? true : (s > thr);
      const el = document.createElement("span");
      el.className = "vj-score " + (over ? "over" : "fallback");
      el.textContent = "变化 " + s.toFixed(2);
      el.title = (thr == null)
        ? `judge_score=${s.toFixed(4)}`
        : (over
          ? `judge_score=${s.toFixed(4)} > 阈值 ${thr} → 超阈值触发`
          : `judge_score=${s.toFixed(4)} ≤ 阈值 ${thr} → 兜底间隔触发`
            + `（有变化但没过阈值，属设计内）`);
      return el;
    },
  };
})();