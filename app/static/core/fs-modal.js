/* FSModal —— 设备端文件浏览模态。
 *
 * 共享基础设施(framework 加载一次):
 *   * 任何卡片只要 "浏览..." 按钮需要弹出设备端目录浏览器,都调:
 *       window.FSModal._sourceTarget = sourceInputEl;
 *       window.FSModal.open("");
 *   * 选中后,模态把路径写到 _sourceTarget.value。
 *   * 自挂 DOM:`DOMContentLoaded` 时若 #fs-modal 不存在则注入;
 *     这样各卡片 template.html 不用重复塞 modal 节点。
 *   * 数据源:``GET /api/fs?path=...``(全局 endpoint,见
 *     app/system/api/fs_browser.py)。
 *
 * 原 VdFsModal(v0.4 之前)仅在 video_detection 卡片内部定义,
 * VCS / VFA 引用但 ReferenceError,browse 按钮 click 无反应。本文件
 * 把 modal 抽到 core,3 个卡片共用同一份实现。
 */
(function () {
  "use strict";
  const API_PREFIX = "/api";

  function isVideoPath(p) {
    if (!p) return false;
    const exts = [".mp4", ".mov", ".mkv", ".avi", ".flv", ".m4v", ".webm", ".ts"];
    const lower = p.toLowerCase();
    for (const e of exts) if (lower.endsWith(e)) return true;
    return false;
  }

  function fmtSize(n) {
    if (n == null) return "";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n.toString() : n.toFixed(1)) + " " + units[i];
  }

  const FSModal = {
    selected: null,
    _wired: false,
    // 默认起点:空字符串 —— 服务端 ``browse_fs`` 收到 ``path=""`` 时
    // 自动回退到 ``FS_ROOT``(算力配置里 ``base.resource_dir``,默认
    // ``/userdata``),避开 ``/proc /sys /dev`` 等黑名单 + 越界检查。
    // 之前 ``_root="/"`` 会被服务端 400("path / 超出允许的根目录
    // /userdata")挡住,modal 一直显示"加载失败"。
    _root: "",

    _ensureDom() {
      if (document.getElementById("fs-modal")) return;
      const wrap = document.createElement("div");
      wrap.id = "fs-modal";
      wrap.className = "vd-modal hidden";
      wrap.setAttribute("role", "dialog");
      wrap.setAttribute("aria-modal", "true");
      wrap.innerHTML = `
        <div class="vd-modal-card">
          <div class="vd-modal-head">
            <span class="vd-modal-title">设备端文件</span>
            <button class="vd-btn vd-modal-close" id="fs-modal-close" aria-label="关闭">×</button>
          </div>
          <div class="vd-modal-crumbs" id="fs-crumbs"></div>
          <div class="vd-modal-list" id="fs-list"></div>
          <div class="vd-modal-foot">
            <span class="vd-hint" id="fs-modal-hint"></span>
            <span class="vd-spacer"></span>
            <button class="vd-btn" id="fs-modal-pick-dir">选择当前目录</button>
            <button class="vd-btn vd-btn-primary" id="fs-modal-pick-file" disabled>选择此文件</button>
          </div>
        </div>`;
      document.body.appendChild(wrap);
    },

    _ensureWired() {
      if (this._wired) return;
      this._wired = true;
      const closeBtn = document.getElementById("fs-modal-close");
      const pickDirBtn = document.getElementById("fs-modal-pick-dir");
      const pickFileBtn = document.getElementById("fs-modal-pick-file");
      if (closeBtn) closeBtn.addEventListener("click", () => this.close());
      if (pickDirBtn) pickDirBtn.addEventListener("click", () => this.pickCurrentDir());
      if (pickFileBtn) pickFileBtn.addEventListener("click", () => this.pickSelectedFile());
      const overlay = document.getElementById("fs-modal");
      if (overlay) overlay.addEventListener("click", (e) => {
        if (e.target.id === "fs-modal") this.close();
      });
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") {
          const m = document.getElementById("fs-modal");
          if (m && !m.classList.contains("hidden")) this.close();
        }
      });
    },

    open(root) {
      this._ensureDom();
      this._ensureWired();
      const el = document.getElementById("fs-modal");
      el.classList.remove("hidden");
      this.navigate(root || this._root || "");
    },

    close() {
      const el = document.getElementById("fs-modal");
      if (el) el.classList.add("hidden");
      this.selected = null;
    },

    setHint(text) {
      const el = document.getElementById("fs-modal-hint");
      if (el) {
        el.textContent = text || "";
        // CSS 单行 ellipsis 截断后,鼠标悬停 tooltip 仍可看完整路径。
        el.title = text || "";
      }
    },

    setPickFileEnabled(on) {
      const btn = document.getElementById("fs-modal-pick-file");
      if (btn) btn.disabled = !on;
    },

    renderCrumbs(path, root) {
      const el = document.getElementById("fs-crumbs");
      el.innerHTML = "";
      const segs = path.split("/").filter(Boolean);
      const prefix = document.createElement("a");
      prefix.textContent = "/";
      prefix.addEventListener("click", () => this.navigate(""));
      el.appendChild(prefix);
      let acc = "";
      segs.forEach((seg, i) => {
        acc += "/" + seg;
        const sep = document.createElement("span");
        sep.className = "vd-crumb-sep";
        sep.textContent = "/";
        el.appendChild(sep);
        if (i === segs.length - 1) {
          const cur = document.createElement("span");
          cur.className = "vd-crumb-current";
          cur.textContent = seg;
          el.appendChild(cur);
        } else {
          const a = document.createElement("a");
          a.textContent = seg;
          const target = acc;
          a.addEventListener("click", () => this.navigate(target));
          el.appendChild(a);
        }
      });
    },

    renderEntries(entries) {
      const el = document.getElementById("fs-list");
      el.innerHTML = "";
      if (!entries.length) {
        const e = document.createElement("div");
        e.className = "vd-entry-empty";
        e.textContent = "(空目录)";
        el.appendChild(e);
        return;
      }
      entries.forEach((entry) => {
        const row = document.createElement("div");
        row.className = "vd-entry";
        row.classList.add(entry.type);
        if (isVideoPath(entry.path)) row.classList.add("video");

        const icon = document.createElement("span");
        icon.className = "vd-entry-icon";
        icon.textContent = entry.type === "dir" ? "▸" : "·";

        const name = document.createElement("span");
        name.className = "vd-entry-name";
        name.textContent = entry.name;

        const meta = document.createElement("span");
        meta.className = "vd-entry-meta";
        meta.textContent = entry.type === "file" ? fmtSize(entry.size) : "";

        row.appendChild(icon);
        row.appendChild(name);
        row.appendChild(meta);

        row.addEventListener("click", () => {
          if (entry.type === "dir") {
            this.navigate(entry.path);
          } else {
            el.querySelectorAll(".vd-entry.selected").forEach((n) => n.classList.remove("selected"));
            row.classList.add("selected");
            this.selected = entry;
            this.setPickFileEnabled(true);
            this.setHint("已选中: " + entry.path);
          }
        });
        el.appendChild(row);
      });
    },

    async navigate(path) {
      this.selected = null;
      this.setPickFileEnabled(false);
      this.setHint("加载中…");
      const list = document.getElementById("fs-list");
      list.innerHTML = '<div class="vd-entry-empty">加载中…</div>';
      try {
        const q = path ? `?path=${encodeURIComponent(path)}` : "";
        const r = await fetch(API_PREFIX + "/fs" + q);
        if (!r.ok) {
          const detail = await r.text().catch(() => "");
          list.innerHTML = `<div class="vd-entry-error">${(detail || r.status)}</div>`;
          this.setHint("加载失败");
          return;
        }
        const data = await r.json();
        this.renderCrumbs(data.path, data.root);
        this.renderEntries(data.entries);
        const dirBtn = document.getElementById("fs-modal-pick-dir");
        if (dirBtn) {
          dirBtn.disabled = !!data.is_root;
          dirBtn.dataset.path = data.path;
        }
        let hint = data.is_root ? "已限制在 " + data.root + " 下" : "点击文件名选择，点击目录进入";
        if (data.truncated) hint += " (结果被截断)";
        this.setHint(hint);
      } catch (e) {
        list.innerHTML = `<div class="vd-entry-error">${e.message}</div>`;
        this.setHint("加载失败");
      }
    },

    pickCurrentDir() {
      const btn = document.getElementById("fs-modal-pick-dir");
      const p = btn && btn.dataset.path;
      if (!p) return;
      if (this._sourceTarget) this._sourceTarget.value = p;
      this.close();
    },

    pickSelectedFile() {
      if (!this.selected) return;
      if (this._sourceTarget) this._sourceTarget.value = this.selected.path;
      this.close();
    },
  };

  // 全局暴露 —— 各卡片 IIFE 不挂 window,故 rename + 暴露两个常用名
  // 兼容旧 VdFsModal 引用。统一收口到 FSModal。
  window.FSModal = FSModal;
  window.VdFsModal = FSModal;  // 兼容旧引用(vd / vcs / vfa app.js 里的 VdFsModal.open)
})();