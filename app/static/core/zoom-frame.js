/* ==========================================================================
 * 宫格「点开放大到整个播放器」的卡片侧接线（vd / vtd / vtl 三张卡共用）
 *
 * 布局在 multi-grid.js（纯 CSS，三卡共享），本模块只负责「后端那半段」：
 * 被放大那一路要换更高分辨率（POST /zoom），以及刷新后本地态与后端对账。
 * 三张卡这部分逐字相同，差别只有本卡的 API base 与 overlay 提示 ——
 * 抽出来是为了不写三份等着各自漂。踩过的坑见 AGENTS.md 对应章节。
 *
 * 卡片侧三处调用：
 *   1) IIFE 内    ZoomFrame.attach({gridEl, apiBase, onError})
 *   2) 状态快照里  ZoomFrame.tick(gridEl, session, total)
 *   3) bindSelect handler 第一行（IIFE 外） ZoomFrame.onTileClick(grid, idx)
 * ========================================================================== */
(function () {
  "use strict";

  /** gridEl → {apiBase, onError, reconciled, lastSent}。后两个分别是对账的
   *  一次性闩与「同一条请求不重发」的去重闩。 */
  const _cfg = new Map();

  /**
   * 注册本卡接线。**必须在卡片 IIFE 内调用** —— onError 一般要传本卡私有的
   * setXdOverlay。
   *
   * @param {{gridEl: string, apiBase: string, onError?: (msg: string) => void}} opt
   */
  function attach(opt) {
    const gridEl = opt && opt.gridEl;
    if (!gridEl) return false;
    if (!window.MultiGrid) {
      // 不报就会静默失效：布局照切、画面照放大，后端一次通知都收不到。
      console.warn("ZoomFrame.attach: MultiGrid 未就绪，放大通知不会送达后端");
      return false;
    }
    _cfg.set(gridEl, {
      apiBase: opt.apiBase || "",
      onError: typeof opt.onError === "function" ? opt.onError : null,
      reconciled: false,
      lastSent: "",
    });
    window.MultiGrid.onZoom(gridEl, (next, prev) => notify(gridEl, next, prev));
    return true;
  }

  /**
   * tile 点击的放大部分。**必须在打 .selected 之前调用** ——
   * FrameJudge.setSelectedChannel() 同步写 .selected，之后再读恒为 true，
   * 于是首次点击就被当成二次点击直接放大。顺序是这个 API 的隐含契约。
   *
   * @param {string} gridEl
   * @param {number} idx 0 基通道号
   */
  function onTileClick(gridEl, idx) {
    const MG = window.MultiGrid;
    if (!MG) return;
    MG.toggleZoom(gridEl, idx, MG.isSelected(gridEl, idx));
  }

  /** 每条状态快照都调一次：`total` 是本会话还有几路。 */
  function tick(gridEl, session, total) {
    const c = _cfg.get(gridEl);
    if (total) { reconcile(gridEl, session); return; }
    if (window.MultiGrid?.resetIfClosed(gridEl, session) && c) {
      // 会话已结束：清掉去重闩，下一轮开新会话才允许重发同样的放大请求
      c.lastSent = "";
    }
  }

  /**
   * 放大态变化 → 通知后端切那一路的显示分辨率。
   *
   * 退出时**必须带 prev**：后端还原的是「之前被升到 720p 的那一路」，而不是
   * 「当前选中的那一路」—— 用户点开第 2 路是为了离开第 1 路。
   */
  function notify(gridEl, next, prev) {
    const c = _cfg.get(gridEl);
    if (!c || !c.apiBase || (next == null && prev == null)) return;
    const payload = { channel: next != null ? next : prev, zoomed: next != null };
    // 同一条不重发：退出放大后后端还有一两拍 WS 快照停在旧 zoomed 上，
    // reconcile 会把完全相同的还原请求再补一次。无害但是纯冗余 chatter。
    const key = payload.channel + ":" + payload.zoomed;
    if (c.lastSent === key) return;
    c.lastSent = key;
    fetch(c.apiBase + "/zoom", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then((r) => (r.ok ? null : r.text().catch(() => "")))
      .then((d) => { if (d && c.onError) c.onError("切换放大分辨率失败: " + d); })
      // 网络抖动 / 会话已关：布局已切完，不打断用户
      .catch(() => {});
  }

  /** 刷新丢了本地 zoom 态、后端还停在那路 720p 时，还原一次。
   *
   *  只认「后端有、本地没有」这一个方向。反过来（本地有、后端说没有）绝不能
   *  自动还原 —— POST /zoom 是异步的，用户刚点开放大、请求还在路上时就可能
   *  先收到一条 zoomed=null 的旧快照，照着还原等于把用户刚要的升档自己撤掉。 */
  function reconcile(gridEl, session) {
    const c = _cfg.get(gridEl);
    if (!c || !session) return;
    const mine = window.MultiGrid?.zoomedIndex(gridEl);
    if (session.zoomed == null || mine != null) { c.reconciled = false; return; }
    if (c.reconciled) return;
    c.reconciled = true;
    notify(gridEl, null, session.zoomed);
  }

  window.ZoomFrame = { attach, onTileClick, tick };
})();
