/* apps.js — 新首页（page-apps）扩展 API */

(function () {
  "use strict";

  /* 未来扩展：填入截图时调用此方法更新占位区。
   * @param {string} hash  如 "#video-detection"
   * @param {string} imageUrl 截图 URL（data: URL 或 HTTP URL）
   */
  function setCardImage(hash, imageUrl) {
    const card = document.querySelector(
      '.page-apps .app-card[href="' + hash + '"]'
    );
    if (!card) return;
    const placeholder = card.querySelector(".app-card-placeholder");
    if (!placeholder) return;
    placeholder.style.backgroundImage = "url('" + imageUrl + "')";
    placeholder.style.backgroundSize = "cover";
    placeholder.style.backgroundPosition = "center";
    placeholder.textContent = "";
  }

  window.Apps = { setCardImage };
})();