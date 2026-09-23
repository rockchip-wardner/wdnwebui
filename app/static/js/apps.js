/* apps.js — 新首页（page-apps）扩展 API */

(function () {
  "use strict";

  /* 给指定卡片填静态截图（仍保留旧 API 兼容后续截图接入）
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

  /* 给指定卡片挂视频元素。
   * muted + autoplay + loop：浏览器允许 muted autoplay，常驻循环无需 JS 干预。
   * @param {string} hash  如 "#video-detection"
   * @param {string} videoUrl  视频 URL（如 "/static/videos/foo.mp4"）
   * @param {Object} [opts]  { loop: boolean = true }
   */
  function setCardVideo(hash, videoUrl, opts) {
    const card = document.querySelector(
      '.page-apps .app-card[href="' + hash + '"]'
    );
    if (!card) return;
    const placeholder = card.querySelector(".app-card-placeholder");
    if (!placeholder) return;

    opts = opts || {};
    const video = document.createElement("video");
    video.className = "app-card-video";
    video.muted = true;
    video.playsInline = true;
    video.preload = "metadata";
    video.autoplay = true;
    video.loop = opts.loop !== false;
    video.src = videoUrl;
    video.setAttribute("aria-hidden", "true");
    placeholder.insertBefore(video, placeholder.firstChild);
  }

  window.Apps = { setCardImage, setCardVideo };
})();