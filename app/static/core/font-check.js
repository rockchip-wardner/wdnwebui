/* font-check.js —— 「这段文字板端**渲染得出来**吗」
 *
 * 用途：板端字体覆盖有限 —— 泰语 / 天城文（印地语）/ 藏文 / 高棉文 /
 * 缅甸文 / 古吉拉特文 / 泰卢固文 / 泰米尔文 / 孟加拉文 **都没有字体**
 * （实测：``fc-list :lang=th|hi|bo|km|my|gu|te|ta|bn`` 全空）⇒ 那些本族
 * 文字会渲染成**豆腐块**（□）。
 *
 * ⚠️ 右上角「界面语言」下拉里的「印地语 / 泰语」就是这样 —— 实测截过图：
 * हिन्दी 和 ไทย 都是一排方框。
 *
 * 判据：把字符画到 canvas 上，跟「**一定渲染不出来**的字符」（U+FFFF，
 * 非字符位 ⇒ 所有字体都只能给 .notdef）**逐像素**比 —— 一模一样就是豆腐块。
 *
 * ⚠️ **不用「量宽度」**：豆腐块和正常字符可能**同宽**（都是方块）⇒ 量宽度
 * 判不出来。**必须比像素**。
 *
 * 暴露：``window.WdnFontCheck.canRender(text)``。
 *
 * ⚠️ **这份实现原来在 ASR 卡里**（``wdnapps_audio_chat`` 的
 * ``qwen3_asr/static/app.js::_canRender``）⇒ 同一套判据在两个仓里各写
 * 一份的话，迟早漂移 ⇒ 框架出这份，卡片优先用它（没有就退回自己那份）。
 */
(function () {
  "use strict";

  const _cache = new Map();
  let _canvas = null;

  /**
   * 这段文字里的**每个**非空白字符都渲染得出来吗？
   *
   * @param {string} text
   * @returns {boolean} 空字符串 / 全空白 ⇒ true（没有字符可判）
   */
  function canRender(text) {
    const s = String(text == null ? "" : text);
    if (!s.trim()) return true;
    if (_cache.has(s)) return _cache.get(s);
    let ok = true;
    try {
      if (!_canvas) {
        _canvas = document.createElement("canvas");
        _canvas.width = 40;
        _canvas.height = 40;
      }
      const ctx = _canvas.getContext("2d");
      if (!ctx) throw new Error("no 2d context");
      const cs = getComputedStyle(document.body);
      const font = (cs.fontSize || "16px") + " " + (cs.fontFamily || "sans-serif");
      const draw = (ch) => {
        ctx.clearRect(0, 0, 40, 40);
        ctx.fillStyle = "#000";
        ctx.textBaseline = "top";
        ctx.font = font;
        ctx.fillText(ch, 0, 0);
        return ctx.getImageData(0, 0, 40, 40).data;
      };
      const notdef = draw("\uFFFF");
      for (const ch of s) {
        if (ch === " " || ch === "\u00a0" || ch === "\t") continue;
        const px = draw(ch);
        let same = true;
        for (let i = 3; i < px.length; i += 4) {
          if (px[i] !== notdef[i]) { same = false; break; }
        }
        if (same) { ok = false; break; }
      }
    } catch (e) {
      // canvas 不可用（老浏览器 / 隐私模式）⇒ 保守按「能渲染」处理
      ok = true;
    }
    _cache.set(s, ok);
    return ok;
  }

  window.WdnFontCheck = {
    canRender: canRender,
    /** 清缓存（字体装上之后可以重来 —— 板端一般不装，但留着）。 */
    clearCache: function () { _cache.clear(); },
  };
})();
