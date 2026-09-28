/* format-utils —— 通用格式化 helper(全局 ``window.fmtNum`` / ``window.fmtEventTime``
 * / ``window.sanitizeEventText``)。
 *
 * v0.4 之前 ``fmtNum`` 散落在 system.js,``fmtEventTime`` 只在 video_detection 的
 * app.js IIFE 里有。各卡片调用时 ReferenceError —— VFA / VTD 事件渲染
 * 因此静默抛错,DOM 永远为空。``sanitizeEventText`` 起初 VD / audio_analysis 自带,
 * 但 video_target_detection / video_target_localization 直接调用了同名函数却没
 * 复制实现,WS event → addVtdEvent → buildVtdEventLi 抛 ReferenceError,
 * 事件历史 DOM 永远为空(用户报告"VTD 检测事件历史框没内容")。
 *
 * 用法:在 index.html.template 加载一次,各卡片 IIFE 顶部
 * ``const fmtNum = window.fmtNum; const fmtEventTime = window.fmtEventTime;
 * const sanitizeEventText = window.sanitizeEventText;``
 * 即可在内部以裸名调用,避免每个调用点改 ``window.`` 前缀。
 */
(function () {
  "use strict";

  function fmtNum(n, digits) {
    if (n == null) return "--";
    return Number(n).toFixed(typeof digits === "number" ? digits : 1);
  }

  function fmtEventTime(seconds) {
    if (seconds == null || !isFinite(seconds) || seconds < 0) return "--";
    const pad = (n, w) => String(n).padStart(w, "0");
    const total = Math.floor(seconds);
    const ms = Math.floor((seconds - total) * 1000);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    return `${pad(h, 2)}:${pad(m, 2)}:${pad(s, 2)}:${pad(ms, 3)}`;
  }

  function sanitizeEventText(s) {
    if (s == null) return "";
    let out = String(s);
    // <|...|> chatml / rknn special tokens
    out = out.replace(/<\|[^|]*\|>/g, "");
    // ASCII control chars except \n \r \t
    out = out.replace(/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]/g, "");
    // Unicode replacement char (decoding failures)
    out = out.replace(/\uFFFD/g, "");
    // Collapse 3+ consecutive newlines to 2
    out = out.replace(/\n{3,}/g, "\n\n");
    return out.trim();
  }

  window.fmtNum = fmtNum;
  window.fmtEventTime = fmtEventTime;
  window.sanitizeEventText = sanitizeEventText;
})();