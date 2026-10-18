(function () {
  "use strict";

  /* 多路宫格共享组件（视频目标监控 / 视频目标定位 / 视频检测 共用）
   *
   * 只管"画面怎么摆、怎么画"：建格、分列、每路单飞解码上屏、角标与高亮。
   * **事件列表刻意不放进来** —— vtd / vtl 是 bbox、vd 是长文本描述，渲染策略
   * 差太远，硬合只会把三边都别扭。 */

  // 每个 container 一份瓦片表。卡片侧只管拿 container 调函数，不各自维护 ——
  // 结构完全一样，维护两份只会各自漂。
  const _grids = new Map();

  // 诊断计数器（常驻，开销可忽略；供 DevTools / playwright 读取）
  const DIAG = {
    binFrames: 0, decoded: 0, drawNoTile: 0, drawEmpty: 0, drawErr: 0,
    gridBuilds: 0, lastDrawIdx: -1,
    // busyDrop=因上一帧在解码而丢弃的帧数，decodeMs=最近一次解码耗时
    busyDrop: 0, decodeMs: 0,
    log: [],
  };
  window.MultiGridDiag = DIAG;

  function _diag(ev, extra) {
    if (DIAG.log.length > 200) DIAG.log.shift();
    DIAG.log.push(extra ? ev + " " + extra : ev);
  }

  /** 通道号对外一律 1 基（内部 idx 是 0 基）。此前角标透出 idx（#0）而记录列表
   *  用 idx+1（通道 1），用户按角标对记录会差一路。抽成函数防多处再漂。 */
  function chNo(idx) { return (idx || 0) + 1; }

  function _state(container) {
    const el = typeof container === "string"
      ? document.getElementById(container) : container;
    if (!el) return null;
    let g = _grids.get(el);
    if (!g) {
      g = { el, tiles: new Map(), cols: 1, total: 0, zoomed: null };
      _grids.set(el, g);
    }
    return g;
  }

  /** 按通道数调整宫格（1~32 格）。
   *
   *  刻意**增量复用**而不是整格重建：换源若 innerHTML="" 全清，已加载的 canvas
   *  会连同画面一起被丢掉，所有格子退回空白直到下一帧（display_fps 默认 2 →
   *  最长 ~500ms）。复用后索引不变的路（换源最常见的情况）画面完全不中断。 */
  function ensure(container, n) {
    const g = _state(container);
    if (!g) return 1;
    const grid = g.el;
    // grid 里除瓦片还有提示层（绝对定位，不参与网格布局），子节点数**不能**用来
    // 数瓦片，必须只数 .vd-tile。
    const domTiles = grid.querySelectorAll(".vd-tile").length;
    if (g.tiles.size === n && domTiles === n) {
      g.total = n;
      return g.cols;
    }
    const cols = n <= 1 ? 1 : n <= 4 ? 2 : n <= 9 ? 3 : n <= 16 ? 4 : n <= 25 ? 5 : 6;
    // 判据取 **DOM 上的实际生效值**，不能用 JS 侧的 g.cols：后者初值是 1，而
    // 1 路时算出的 cols 也是 1 → 条件恒假 → gridTemplateColumns 从没被设过，
    // grid 退回默认 auto 布局被 canvas 固有宽度（384px）撑开（实测 1102px 宽的
    // 播放区只用了 386px）—— 所以只有「恰好 1 路」时出错。
    const colsCss = `repeat(${cols}, 1fr)`;
    // 放大态下必须写 1 列而不是 cols 列 —— ensure() 会在换源 / 路数变化时重跑，
    // 直接写 colsCss 会把用户的放大状态冲掉。cols 始终更新（哪怕样式没变，
    // 否则退出放大时会用回一个陈旧的列数）。
    const wantCss = (g.zoomed == null) ? colsCss : "1fr";
    if (grid.style.gridTemplateColumns !== wantCss) {
      DIAG.gridBuilds++;
      grid.style.gridTemplateColumns = wantCss;
      grid.dataset.cols = String(cols);
    }
    g.cols = cols;
    // 别在 grid 上设 aspect-ratio：**不生效**（.vd-layout 是 align-items:stretch，
    // grid 作为 flex 子项会被拉满行高，1 路实测 1002x586 而不是 564）。6 路是 3x2，
    // 必然有上下留边 —— 格子数的固有结果，不是 bug，交给 object-fit: contain 居中。
    for (const [i, t] of g.tiles) {
      if (i < n) continue;
      t.dropped = true;          // 让在途解码自己退出，不再往已摘掉的 tile 上画
      t.tile.remove();
      g.tiles.delete(i);
    }
    let created = 0;
    for (let i = 0; i < n; i++) {
      if (g.tiles.has(i)) continue;
      const tile = document.createElement("div");
      tile.className = "vd-tile";
      tile.dataset.idx = String(i);
      const cv = document.createElement("canvas");
      const label = document.createElement("div");
      label.className = "vd-tile-label";
      label.textContent = "#" + chNo(i);
      const badge = document.createElement("div");
      badge.className = "vd-tile-badge empty";   // 启动时还没打分 → 藏起来
      tile.appendChild(cv);
      tile.appendChild(label);
      tile.appendChild(badge);
      // tile click → 选中该路。bindSelect() 会挂 handler；没有 handler 就当
      // 是纯展示（保持向后兼容：旧卡片不绑 handler 时 click 啥也不做）。
      tile.addEventListener("click", (ev) => {
        if (g.onSelect) g.onSelect(i, ev);
      });
      grid.appendChild(tile);
      g.tiles.set(i, {
        tile, canvas: cv, ctx: cv.getContext("2d", { alpha: false }),
        label, badge,
        busy: false,
        pending: null,
        pendingOverlay: null,
        dropped: false,
        w: 0, h: 0,
      });
      created++;
    }
    g.total = n;
    // 新建的格子要立刻套上放大态的隐藏标记 —— ensure() 是唯一会增删 tile
    // 的地方，不在这里补，后面就没有机会了。
    if (g.zoomed != null) _applyZoom(g);
    _diag("grid", `build n=${n} cols=${cols} new=${created}`);
    return cols;
  }

  /** 把一路的画面画进对应瓦片。
   *
   *  `src` 可以是 JPEG 字节（ArrayBuffer / TypedArray / Blob），也可以**已经是可
   *  绘制对象**（canvas / ImageBitmap）—— 后者给"冻结当前画面"用，跳过编解码。
   *
   *  为什么是 canvas + createImageBitmap 而不是 `<img>` + blob URL：`<img>` 每帧换
   *  src 会**掐断上一帧还在进行的加载**（URL loader → 解码 → 上传纹理）。帧小
   *  （384×216≈12KB）看不出来，帧大（960×540≈65KB，实测 25fps 就是每帧 40ms 的
   *  硬预算）就有一批被下次赋值中途取消，表现为**画面疯狂闪** —— 与「<5 路才闪」
   *  吻合：1~4 路 960×540，5 路以上回落 384×216。createImageBitmap 走解码线程，
   *  不经 URL loader，不存在「换源掐断」。 */
  function paint(container, idx, src, overlay) {
    DIAG.binFrames++;
    DIAG.lastDrawIdx = idx;
    const drawable = src instanceof Blob || ArrayBuffer.isView(src)
                    || src instanceof ArrayBuffer;
    if (!drawable && !src) {
      DIAG.drawEmpty++;
      return;
    }
    if (drawable && !src.byteLength) {
      DIAG.drawEmpty++;
      return;
    }
    const g = _state(container);
    const t = g && g.tiles.get(idx);
    if (!t) {
      DIAG.drawNoTile++;
      _diag("draw-no-tile", `idx=${idx}`);
      return;
    }
    // 每路同时只允许一个解码在飞：pending 被占用说明上一帧还没解完，直接覆盖
    // 丢弃（25fps 供 24fps 取，丢一帧不影响连贯性）。overlay 一并带在 pending
    // 上 —— 它和这一帧是配对的（画的是这帧的框），拿上一帧的框配这一帧会错位。
    if (t.busy) {
      DIAG.busyDrop++;
      t.pending = src;
      t.pendingOverlay = overlay;
      return;
    }
    _decode(t, src, overlay);
  }

  async function _decode(t, src, overlay) {
    t.busy = true;
    const t0 = performance.now();
    let owned = null;          // 自己解码出来的要 close，传进来的不能动
    try {
      let bmp = src;
      if (src instanceof Blob || ArrayBuffer.isView(src)
          || src instanceof ArrayBuffer) {
        owned = await createImageBitmap(
          new Blob([src], { type: "image/jpeg" }));
        bmp = owned;
      }
      if (!t.dropped) {
        const w = bmp.width || bmp.naturalWidth;
        const h = bmp.height || bmp.naturalHeight;
        // canvas 尺寸只在真的变了时才写：每帧写 canvas.width 会清空画布并
        // 重置 ctx 状态，是老实现里一个实打实的每帧浪费。
        if (t.canvas.width !== w || t.canvas.height !== h) {
          t.canvas.width = w;
          t.canvas.height = h;
          t.w = w; t.h = h;
        }
        t.ctx.drawImage(bmp, 0, 0);
        // 画完帧再叠卡片自己的标注（vtl 的 bbox / vd 的命中框）；不做成独立
        // 图层是因为标注**跟着帧走**，canvas 一 resize 就全丢。
        if (overlay) overlay(t.ctx, t);
        DIAG.decoded++;
      }
    } catch (e) {
      DIAG.drawErr++;
      console.warn("[multi-grid] 解码失败", e);
    } finally {
      if (owned) owned.close();
      DIAG.decodeMs = Math.round(performance.now() - t0);
      t.busy = false;
      const next = t.pending;
      t.pending = null;
      const nextOverlay = t.pendingOverlay;
      t.pendingOverlay = null;
      if (next && !t.dropped) _decode(t, next, nextOverlay);
    }
  }

  /** 按快照刷新每格的角标 / 高亮 / 错误态，返回 {total, ok, eof}。
   *
   *  `cur` 是"正在推理"的通道，可以是单个 idx（session.vlm.current，单卡）
   *  也可以是数组（session.vlm.inflight_channels，多卡：K 条 feeder 各占一路，
   *  同时有 K 格该打绿框）。传数组是为了多卡并行时每一路都看得见。
   *  `eof` 是已播完（未循环）的路数 —— 文件源播完会停在 EOF 不再产帧，卡片要
   *  据此提示，否则表现为"画面静止 + 全部异常"，用户无从判断。角标正常时**只显示
   *  通道号** —— 帧率是运维信息不是客户要看的，只有出错时才补状态/告警。 */
  function status(container, list, cur) {
    const g = _state(container);
    if (!g) return { total: 0, ok: 0, eof: 0 };
    if (list.length !== g.total) ensure(container, list.length);
    const active = new Set(
      Array.isArray(cur) ? cur
        : (typeof cur === "number" && cur >= 0 ? [cur] : []));
    let ok = 0, total = 0, eof = 0;
    for (const c of list) {
      const t = g.tiles.get(c.idx);
      if (!t) continue;
      total++;
      if (c.has_frame && c.state === "playing") ok++;
      if (c.state === "eof") eof++;
      const src = (c.source || "").split("/").pop() || c.source || "";
      t.label.textContent =
        `#${chNo(c.idx)}` +
        (c.state !== "playing" ? ` · ${c.state}` : "") +
        (c.errors ? ` · ⚠${c.errors}` : "");
      t.label.title = src;
      t.tile.classList.toggle("active", active.has(c.idx));
      t.tile.classList.toggle("bad", !!c.error || c.errors > 0);
      // 不再让 status 写 t.badge 文字 —— 那是判据角标专用位，由
      // frame-judge.js 走 setBadge() 写入，避免两套逻辑抢同一个 DOM 节点。
    }
    return { total, ok, eof };
  }

  /** 写某一路的判据角标。payload 形态：
   *    {state: 'changed' | 'idle' | null, score: number|null}
   *  - state='changed' → 加 .changed 类（橙色高亮）
   *  - state='idle'    → 普通灰底
   *  - state=null/undefined 且 score=null → .empty（角标藏起来）
   *
   *  文案刻意压短到「变化 12.9」「无变化 3.2」两段，4 路宫格每路 200~400px 宽
   *  也要能放下。分数超 99 时切 1 位小数（不让 100.0 这种"刚好"把角标撑炸）。
   */
  function setBadge(container, idx, payload) {
    const g = _state(container);
    const t = g && g.tiles.get(idx);
    if (!t) return;
    const score = payload && payload.score;
    const state = payload && payload.state;
    const b = t.badge;
    if (score == null || state == null) {
      b.classList.add("empty");
      b.classList.remove("changed", "idle");
      b.textContent = "";
      return;
    }
    b.classList.remove("empty");
    const label = state === "changed" ? "变化" : "无变化";
    const s = Number(score);
    const txt = s >= 100 ? `${label} ${s.toFixed(0)}`
                : s >= 10 ? `${label} ${s.toFixed(1)}`
                : `${label} ${s.toFixed(2)}`;
    b.textContent = txt;
    b.classList.toggle("changed", state === "changed");
    b.classList.toggle("idle", state === "idle" && s < 0.5);
  }

  /** 标记某一路为"被用户选中"（蓝色边框）。可与 .active 叠加——
   *  .active 是 VLM 推理中（系统高亮，绿色），.selected 是用户在编辑（蓝色）。
   *  两者正交不互斥。 */
  function setSelected(container, idx) {
    const g = _state(container);
    if (!g) return;
    for (const [i, t] of g.tiles) {
      t.tile.classList.toggle("selected", i === idx);
    }
  }

  /** tile click → 派发 `multi-grid:select` 自定义事件（带 idx, tileId）。
   *  cards 侧 listen 后改自己的 selectedChannel 状态。 */
  function bindSelect(container, handler) {
    const g = _state(container);
    if (!g) return;
    g.onSelect = handler;
  }

  /* ------------------------------------------------------------------
   * 放大态：把某一路等比例放大到整个播放器。**纯布局操作** —— 画面就是 tile
   * 里那块 canvas，内容与 CSS 布局解耦，放大只是「grid 只剩 1 格 1 行 + 其余
   * 格子 display:none」，等比例由 canvas 的 object-fit: contain 保证。
   *
   * 不碰解码 / 送检 / 检测；要换更高分辨率是卡片侧的事（见 zoom-frame.js）。
   * ------------------------------------------------------------------ */

  function _applyZoom(g) {
    const z = g.zoomed;
    g.el.classList.toggle("is-zoomed", z != null);
    g.el.style.gridTemplateColumns =
      (z == null) ? `repeat(${g.cols}, 1fr)` : "1fr";
    for (const [i, t] of g.tiles) {
      t.tile.classList.toggle("is-hidden", z != null && i !== z);
    }
  }

  function isSelected(container, idx) {
    const g = _state(container);
    const t = g && g.tiles.get(idx);
    return !!(t && t.tile.classList.contains("selected"));
  }

  /** 注册 zoom 态变化回调 `(next, prev) => {}`。布局是本模块的事，但被放大
   *  那一路要换更高分辨率是后端的事 —— 只回抛状态，不自己去发请求。 */
  function onZoom(container, handler) {
    const g = _state(container);
    if (g) g.onZoom = handler;
  }

  function _fireZoom(g, prev) {
    if (typeof g.onZoom !== "function") return;
    // 不静默吞：回调里引用错作用域会炸 ReferenceError，布局还是切了、
    // 后端永远收不到通知，而页面上看不出任何异常。
    try { g.onZoom(g.zoomed, prev); }
    catch (e) { console.warn("MultiGrid onZoom 回调抛错", e); }
  }

  /** 点开放大 / 关闭放大。返回 true = 现在是放大了这一路。
   *
   *  `wasSelected` 由调用方传入，**不能在函数里现读**：调用方会先调
   *  FrameJudge.setSelectedChannel() 同步打上 .selected，之后再读恒为 true，
   *  于是首次点击也被当成二次点击直接放大。
   *
   *  点没选中过的路 = 退出放大 + 选中它（想看清别的路时不该被挡在放大态）。 */
  function toggleZoom(container, idx, wasSelected) {
    const g = _state(container);
    if (!g || !g.tiles.has(idx)) return false;
    const prev = g.zoomed;
    if (wasSelected && prev === idx) g.zoomed = null;  // 再点同一路 → 还原宫格
    else if (wasSelected) g.zoomed = idx;               // 二次点击 → 放大
    else g.zoomed = null;                               // 点别的路 → 退出放大
    _applyZoom(g);
    _bindEsc(g);
    _diag("zoom", `idx=${idx} was=${!!wasSelected} -> ` +
                  (g.zoomed == null ? "grid" : "zoom " + g.zoomed));
    _fireZoom(g, prev);
    return g.zoomed === idx;
  }

  /** 放大态下按 ESC 退出。监听器只在放大时挂上、退出时摘掉 —— 常驻的全局
   *  keydown 会在别的卡片抢输入时也生效。 */
  function _bindEsc(g) {
    if (g.zoomed == null) {
      if (g.onEsc) { document.removeEventListener("keydown", g.onEsc); g.onEsc = null; }
      return;
    }
    if (g.onEsc) return;
    g.onEsc = (ev) => {
      if (ev.key !== "Escape") return;
      ev.preventDefault();
      const prev = g.zoomed;
      g.zoomed = null;
      _applyZoom(g);
      _bindEsc(g);
      _diag("zoom", `esc from ${prev} -> grid`);
      _fireZoom(g, prev);
    };
    document.addEventListener("keydown", g.onEsc);
  }

  function zoomedIndex(container) {
    const g = _state(container);
    return g ? g.zoomed : null;
  }

  /** 会话已关闭时清空宫格（含放大态）。由状态快照驱动，不放 close 按钮里 ——
   *  关会话有**四条**路径（按钮 / DemoGuard / beforeunload beacon / 外部
   *  `/close`），只补按钮必然漏；漏掉的后果是下次 open() 的 ensure() 拿残留
   *  的 `g.zoomed` 重新放大，用户看到「刚打开 6 路却只剩一格」。
   *
   *  只认 idle：opening 阶段 channel_list 可能短暂为空，那时清会拆了重建；
   *  error 阶段格子上的红框 / ⚠N 是有用的诊断，不该抹。 */
  function resetIfClosed(container, session) {
    if (!session || session.state !== "idle") return false;
    const g = _state(container);
    if (!g || (!g.total && g.zoomed == null)) return false;
    reset(container);
    return true;
  }

  function reset(container) {
    const g = _state(container);
    if (!g) return;
    // 顺序要紧：先清 zoomed 再摘监听，否则 _bindEsc 看到 zoomed != null 会
    // 直接 return，监听器就永久留在 document 上了。
    g.zoomed = null;
    _bindEsc(g);
    for (const [, t] of g.tiles) {
      t.dropped = true;
      t.tile.remove();
    }
    g.tiles.clear();
    g.total = 0;
    g.el.classList.remove("is-zoomed");
    g.el.style.gridTemplateColumns = "";
  }

  window.MultiGrid = {
    chNo, ensure, paint, status, reset,
    setBadge, setSelected, isSelected, bindSelect,
    toggleZoom, zoomedIndex, onZoom, resetIfClosed,
    diag: DIAG,
    tiles: (container) => (_state(container) || { tiles: new Map() }).tiles,
  };
})();
