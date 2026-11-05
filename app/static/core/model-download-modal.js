/* ModelDownloadModal —— 「模型下载」对话框。
 *
 * 挂在算力配置页的「模型下载」卡片上（app/system/static/system.js）。
 *
 * 流程：
 *   1. 打开 → GET /api/compute/model-repos —— 远端 wardner 仓清单 +
 *      本板已装标记（含本地绝对路径）。
 *   2. 已装的行显示「已下载」徽章，**鼠标悬停显示本板路径**（title）；
 *      没装的行给复选框，可多选。
 *   3. 底部选下载目录 —— **3 选 1**（当前配置的模型根 / /userdata/models /
 *      /mnt/oem/models / <HOME>/models），默认 = 系统配置里的模型目录。
 *      不能选子目录：模型落地是 <dest>/<模型名>/，而模型根扫描只有一层，
 *      子目录里的模型永远扫不到。
 *   4. 「一键下载」→ POST /api/compute/model-download → 后台串行下载，
 *      进度由算力页那张卡片显示（1Hz 轮询 /api/compute/model-download/status）。
 */
(function () {
  "use strict";
  const API_PREFIX = "/api";

  function fmtSize(n) {
    if (n == null) return "";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    let v = Number(n);
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return (i === 0 ? v.toString() : v.toFixed(2)) + " " + units[i];
  }

  const ModelDownloadModal = {
    _wired: false,
    _inventory: null,
    _selected: new Set(),
    _loading: false,

    _ensureDom() {
      if (document.getElementById("model-dl-modal")) return;
      const wrap = document.createElement("div");
      wrap.id = "model-dl-modal";
      wrap.className = "vd-modal hidden";
      wrap.setAttribute("role", "dialog");
      wrap.setAttribute("aria-modal", "true");
      wrap.innerHTML = `
        <div class="vd-modal-card mdl-card">
          <div class="vd-modal-head">
            <span class="vd-modal-title">模型下载</span>
            <button class="vd-btn vd-modal-close" id="mdl-close" aria-label="关闭">×</button>
          </div>
          <div class="mdl-toolbar">
            <span class="vd-hint" id="mdl-hint">加载中…</span>
            <span class="vd-spacer"></span>
            <button class="vd-btn" id="mdl-refresh" type="button">刷新列表</button>
          </div>
          <div class="mdl-list" id="mdl-list"></div>
          <div class="vd-modal-foot mdl-foot">
            <label class="mdl-dest-label" for="mdl-dest">下载目录</label>
            <select class="mdl-dest-select" id="mdl-dest"></select>
            <span class="vd-spacer"></span>
            <span class="vd-hint" id="mdl-selinfo"></span>
            <button class="vd-btn vd-btn-primary" id="mdl-start" type="button" disabled>
              一键下载
            </button>
          </div>
          <div class="mdl-dest-warn" id="mdl-dest-warn"></div>
        </div>`;
      document.body.appendChild(wrap);
    },

    _ensureWired() {
      if (this._wired) return;
      this._wired = true;
      const closeBtn = document.getElementById("mdl-close");
      const refreshBtn = document.getElementById("mdl-refresh");
      const startBtn = document.getElementById("mdl-start");
      const destSelect = document.getElementById("mdl-dest");
      if (closeBtn) closeBtn.addEventListener("click", () => this.close());
      if (refreshBtn) refreshBtn.addEventListener("click", () => this.load());
      if (destSelect) {
        destSelect.addEventListener("change", () => this._updateDestWarn());
      }
      if (startBtn) startBtn.addEventListener("click", () => this._start());
      const overlay = document.getElementById("model-dl-modal");
      if (overlay) {
        overlay.addEventListener("click", (e) => {
          if (e.target.id === "model-dl-modal") this.close();
        });
      }
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") {
          const m = document.getElementById("model-dl-modal");
          if (m && !m.classList.contains("hidden")) this.close();
        }
      });
    },

    async open() {
      this._ensureDom();
      this._ensureWired();
      const el = document.getElementById("model-dl-modal");
      el.classList.remove("hidden");
      this._selected.clear();
      await this.load();
    },

    close() {
      const el = document.getElementById("model-dl-modal");
      if (el) el.classList.add("hidden");
    },

    setHint(text) {
      const el = document.getElementById("mdl-hint");
      if (el) {
        el.textContent = text || "";
        el.title = text || "";
      }
    },

    async load() {
      if (this._loading) return;
      this._loading = true;
      const list = document.getElementById("mdl-list");
      if (list) list.innerHTML = '<div class="mdl-empty">加载中…</div>';
      this.setHint("加载中…");
      try {
        const r = await fetch(API_PREFIX + "/compute/model-repos", { cache: "no-store" });
        if (!r.ok) {
          const detail = await r.text().catch(() => "");
          throw new Error(detail || ("HTTP " + r.status));
        }
        this._inventory = await r.json();
        // 把清单推给算力页 —— 「已装 N/M」角标用的是同一个口径，
        // 弹框一打开卡片上的计数就同步了，不用等 30s 轮询。
        if (window.ComputeConfigPage
            && typeof window.ComputeConfigPage.setRepoInventory === "function") {
          window.ComputeConfigPage.setRepoInventory(this._inventory);
        }
        this._render();
        // 远端仓的大小统计是后台补的（镜像 tree API 慢且抖动）——
        // 缺大小的行过几秒再拉一次，弹框开着的时候自己会补上。
        this._scheduleSizeRefresh();
      } catch (e) {
        if (list) list.innerHTML = `<div class="mdl-error">${e.message}</div>`;
        this.setHint("加载失败：" + e.message);
      } finally {
        this._loading = false;
      }
    },

    _scheduleSizeRefresh() {
      const models = (this._inventory && this._inventory.models) || [];
      if (!models.some((m) => !m.size_ok)) return;
      if (this._sizeTimer) clearTimeout(this._sizeTimer);
      this._sizeTimer = setTimeout(async () => {
        const modal = document.getElementById("model-dl-modal");
        if (!modal || modal.classList.contains("hidden")) return;
        try {
          const r = await fetch(API_PREFIX + "/compute/model-repos", { cache: "no-store" });
          if (!r.ok) return;
          this._inventory = await r.json();
          // ⚠️ **只更新大小文本，不重建列表** —— 重建会把用户正在点的
          // 复选框换掉（元素 detach → 点击落空 / 勾选状态闪一下）。
          this._applySizes();
          this._updateSelInfo();
          this._scheduleSizeRefresh();
        } catch (_e) { /* 静默 */ }
      }, 3000);
    },

    _applySizes() {
      const models = (this._inventory && this._inventory.models) || [];
      models.forEach((m) => {
        const entry = this._rowMap && this._rowMap.get(m.repo_id);
        if (!entry) return;
        entry.meta.textContent = m.size_ok
          ? `${fmtSize(m.bytes)} · ${m.files} 文件`
          : "大小统计中…";
      });
    },

    _render() {
      const inv = this._inventory;
      if (!inv) return;
      const list = document.getElementById("mdl-list");
      if (!list) return;
      list.innerHTML = "";

      const models = inv.models || [];
      const total = models.length;
      const installed = models.filter((m) => m.installed).length;
      this.setHint(
        `共 ${total} 个仓库 · 本板已装 ${installed} 个 · 源 ${inv.endpoint}`
      );

      if (!models.length) {
        list.innerHTML = '<div class="mdl-empty">远端没有查到任何仓库</div>';
        return;
      }

      this._rowMap = new Map();
      models.forEach((m) => {
        const row = document.createElement("div");
        row.className = "mdl-row";
        if (m.installed) row.classList.add("done");

        const main = document.createElement("label");
        main.className = "mdl-row-main";

        const check = document.createElement("input");
        check.type = "checkbox";
        check.className = "mdl-check";
        check.disabled = !!m.installed;
        // 列表会因「大小后台补齐」而重绘（每 3s 一次，直到大小齐了为止）——
        // 重绘后必须**把已勾状态恢复回来**，否则用户勾了等几秒就变成未勾
        // （this._selected 里还在，但界面撒谎了）。
        if (m.installed) {
          // 已装的不给勾 —— 下载会覆盖板上模型，交给用户手动处理
          main.title = "本板已安装：" + (m.local_path || "");
        } else if (this._selected.has(m.repo_id)) {
          check.checked = true;
        }
        check.addEventListener("change", () => {
          if (check.checked) this._selected.add(m.repo_id);
          else this._selected.delete(m.repo_id);
          this._updateSelInfo();
        });
        main.appendChild(check);

        const name = document.createElement("span");
        name.className = "mdl-name";
        name.textContent = m.name;
        main.appendChild(name);

        const repo = document.createElement("span");
        name.title = m.repo_id || m.name;
        repo.className = "mdl-repo";
        repo.textContent = m.repo_id || "（本地已有，远端清单未列出）";
        main.appendChild(repo);

        row.appendChild(main);

        const meta = document.createElement("span");
        meta.className = "mdl-meta";
        if (m.size_ok) {
          meta.textContent = `${fmtSize(m.bytes)} · ${m.files} 文件`;
        } else {
          meta.textContent = "大小统计中…";
        }
        row.appendChild(meta);
        this._rowMap.set(m.repo_id, { row, meta });

        const badge = document.createElement("span");
        badge.className = "mdl-badge";
        if (m.installed) {
          badge.classList.add("mdl-badge-done");
          badge.textContent = "已下载";
          // 需求：鼠标放上去显示位于本板子的路径
          badge.title = m.local_path || "";
          row.title = m.local_path || "";
        } else if (m.local_only) {
          badge.classList.add("mdl-badge-local");
          badge.textContent = "本地目录";
          badge.title = m.local_path || "";
          row.title = m.local_path || "";
        } else {
          badge.classList.add("mdl-badge-remote");
          badge.textContent = "未下载";
        }
        row.appendChild(badge);

        list.appendChild(row);
      });

      this._updateSelInfo();
      this._renderDestCandidates();
      this._updateDestWarn();
    },

    // ---- 下载目录选择（3 选 1）----
    // 候选 = 当前配置的模型根 / /userdata/models / /mnt/oem/models /
    //        <HOME>/models，后端给的，和基础配置里「RKNN 模型权重存放路径」
    //        的说明同源。
    // **不能选子目录** —— 模型落地是 <dest>/<模型名>/，而模型根扫描只有
    // 一层（root.iterdir()，不递归），子目录里的模型永远扫不到。
    _renderDestCandidates() {
      const sel = document.getElementById("mdl-dest");
      if (!sel) return;
      const inv = this._inventory || {};
      const cands = inv.dest_candidates || [];
      const prev = sel.value;
      sel.innerHTML = "";
      cands.forEach((c) => {
        const opt = document.createElement("option");
        opt.value = c.path;
        const tags = [];
        if (c.is_model_root) tags.push("当前模型目录");
        else if (!c.exists) tags.push("将自动创建");
        if (!c.writable) tags.push("不可写");
        opt.textContent = c.path + (tags.length ? "（" + tags.join(" · ") + "）" : "");
        if (c.free_bytes) {
          opt.title = `可用空间 ${fmtSize(c.free_bytes)}`
            + (c.exists ? "" : "（目录还不存在，将自动创建）");
        }
        sel.appendChild(opt);
      });
      // 默认 = 系统配置里的模型目录；用户改过就保留用户的
      const def = inv.default_dest || "";
      const hit = cands.find((c) => c.path === prev);
      sel.value = hit ? hit.path
        : (cands.some((c) => c.path === def) ? def
          : (cands[0] ? cands[0].path : ""));
    },

    // 下载目录 ≠ 当前模型根 → 明确警告（模型根只有一个）
    _updateDestWarn() {
      const el = document.getElementById("mdl-dest-warn");
      const sel = document.getElementById("mdl-dest");
      // select 收起态会把文本裁掉 → title 给出完整路径（hover 可见）
      if (sel) {
        const cands = (this._inventory && this._inventory.dest_candidates) || [];
        const hit = cands.find((c) => c.path === sel.value);
        sel.title = hit
          ? `${hit.path}${hit.is_model_root ? "（当前模型目录）" : ""}`
            + (hit.free_bytes ? ` · 可用 ${fmtSize(hit.free_bytes)}` : "")
          : (sel.value || "");
      }
      if (!el) return;
      const inv = this._inventory || {};
      const dest = (sel && sel.value || "").trim();
      const root = (inv.models_root || "").replace(/\/+$/, "");
      if (!dest || !root) { el.textContent = ""; el.classList.remove("mdl-warn"); return; }
      if (dest.replace(/\/+$/, "") === root) {
        el.textContent = "";
        el.classList.remove("mdl-warn");
        return;
      }
      el.textContent =
        `⚠ 下载目录不是当前模型目录（${root}）—— 模型不会被算力页识别。`
        + `请到「基础配置」把「RKNN 模型权重存放路径」也改成 ${dest}，并重启服务。`;
      el.classList.add("mdl-warn");
    },

    _updateSelInfo() {
      const inv = this._inventory || {};
      const models = inv.models || [];
      let bytes = 0;
      let n = 0;
      this._selected.forEach((repo) => {
        const m = models.find((x) => x.repo_id === repo);
        if (m) { bytes += m.bytes || 0; n += 1; }
      });
      const info = document.getElementById("mdl-selinfo");
      const free = inv.free_bytes || 0;
      if (info) {
        if (!n) {
          info.textContent = "";
        } else {
          const enough = free && bytes > free;
          info.textContent =
            `已选 ${n} 个 · ${fmtSize(bytes)}` +
            (free ? ` · 可用 ${fmtSize(free)}` : "") +
            (enough ? "（空间不足）" : "");
          info.classList.toggle("mdl-warn", !!enough);
          info.title = info.textContent;
        }
      }
      const startBtn = document.getElementById("mdl-start");
      if (startBtn) startBtn.disabled = n === 0;
    },

    async _start() {
      const repos = Array.from(this._selected);
      if (!repos.length) return;
      const destInput = document.getElementById("mdl-dest");
      const dest = (destInput && destInput.value) || "";
      const startBtn = document.getElementById("mdl-start");
      const hint = document.getElementById("mdl-hint");
      if (startBtn) { startBtn.disabled = true; startBtn.textContent = "提交中…"; }
      try {
        const r = await fetch(API_PREFIX + "/compute/model-download", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repos: repos, dest_dir: dest }),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) {
          throw new Error(data.detail || ("HTTP " + r.status));
        }
        this.close();
        // 进度由算力页的「模型下载」卡片显示，这里只提示一下
        if (window.ComputeConfigPage && window.ComputeConfigPage.onDownloadStarted) {
          window.ComputeConfigPage.onDownloadStarted(data);
        }
      } catch (e) {
        if (hint) hint.textContent = "启动失败：" + e.message;
        if (startBtn) { startBtn.disabled = false; startBtn.textContent = "一键下载"; }
      }
    },
  };

  window.ModelDownloadModal = ModelDownloadModal;
})();
