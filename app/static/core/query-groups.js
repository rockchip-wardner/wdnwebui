/* Repeatable text + image query groups.
 *
 * Two presentations share one value model:
 *   - "entries": compact rows on the app page; add/edit opens a modal.
 *   - "inline":  compact collapsible rows in the wizard (VTL prompt_rows
 *                style); clicking a row expands it for editing.
 *
 * Value shape: [{ text: "...", imagePath: "..." }].
 */
(function () {
  "use strict";

  function ce(tag, cls, text) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text != null) el.textContent = text;
    return el;
  }

  function normalizeGroup(g) {
    if (!g || typeof g !== "object") return { text: "", imagePath: "" };
    return {
      text: String(g.text != null ? g.text : g.query != null ? g.query : ""),
      imagePath: String(
        g.imagePath != null ? g.imagePath
          : g.image_path != null ? g.image_path
          : g.image != null ? g.image : ""
      ),
    };
  }

  function controllerOf(el) {
    if (!el) return null;
    return el.__queryGroups || (el.__isQueryGroups ? el : null);
  }

  function valuesOf(el) {
    const ctl = controllerOf(el);
    if (!ctl) return [];
    return ctl.items
      .map((it) => it.data)
      .filter((g) => g.text || g.imagePath);
  }

  function mount(container, opts) {
    if (!container) return null;
    if (container.__queryGroups) return container.__queryGroups;
    opts = opts || {};

    const mode = opts.presentation === "entries" ? "entries" : "inline";
    const maxGroups = Math.max(1, Number(opts.maxGroups) || 5);
    const maxTextLength = Math.max(1, Number(opts.maxTextLength) || 50);
    const emptyRows = Math.max(0, Number(opts.emptyRows) || 0);
    const thumbEndpoint = opts.thumbnailEndpoint || "";
    const uploadEndpoint = opts.uploadEndpoint || "";
    const multiline = mode === "entries";

    const ctl = {
      __isQueryGroups: true,
      container,
      items: [],
      disabled: false,
      list: null,
      addBtn: null,
      countEl: null,
      emptyEl: null,
      modal: null,
      modalRec: null,
      modalNew: false,
      modalEditor: null,
    };

    container.innerHTML = "";
    container.classList.add("qg-root", "qg-mode-" + mode);
    ctl.list = ce("div", "qg-list");
    ctl.emptyEl = ce("div", "qg-empty", opts.emptyText || "暂无描述");
    const toolbar = ce("div", "qg-toolbar");
    ctl.addBtn = ce("button", "vd-btn qg-add", opts.addLabel || "+ 添加描述");
    ctl.addBtn.type = "button";
    ctl.countEl = ce("span", "qg-count");
    toolbar.appendChild(ctl.addBtn);
    toolbar.appendChild(ctl.countEl);
    ctl.list.appendChild(ctl.emptyEl);
    container.appendChild(ctl.list);
    container.appendChild(toolbar);

    const emit = () => {
      ctl.sync();
      if (typeof opts.onChange === "function") {
        try { opts.onChange(valuesOf(ctl)); } catch (_) { /* ignore */ }
      }
      try {
        container.dispatchEvent(new CustomEvent("qg:change", {
          bubbles: true,
          detail: { values: valuesOf(ctl) },
        }));
        container.dispatchEvent(new Event("change", { bubbles: true }));
      } catch (_) { /* ignore */ }
    };

    const syncPreview = (imageInput, preview, img, name) => {
      const path = (imageInput?.value || "").trim();
      if (name) name.textContent = path ? path.split(/[\\/]/).pop() : "";
      if (preview) preview.hidden = !path;
      if (!img) return;
      if (!path) {
        img.removeAttribute("src");
        img.dataset.path = "";
        return;
      }
      if (img.dataset.path === path) return;
      img.dataset.path = path;
      if (!thumbEndpoint) {
        img.removeAttribute("src");
        return;
      }
      const sep = thumbEndpoint.includes("?") ? "&" : "?";
      img.src = thumbEndpoint + sep + "path=" + encodeURIComponent(path);
      img.alt = "检索图片预览";
    };

    function buildEditor(initial, onChange) {
      const data = normalizeGroup(initial);
      const wrap = ce("div", "qg-editor");
      const textWrap = ce("div", "qg-text-wrap");
      const text = multiline
        ? ce("textarea", "qg-text qg-textarea")
        : ce("input", "qg-text qg-input");
      if (!multiline) text.type = "text";
      text.maxLength = maxTextLength;
      text.placeholder = opts.textPlaceholder || "输入描述，例如：有人在搬箱子";
      text.value = data.text;
      const counter = ce("span", "qg-text-count");
      textWrap.appendChild(text);
      textWrap.appendChild(counter);

      const imageRow = ce("div", "qg-image-row");
      const image = ce("input", "qg-image");
      image.type = "text";
      image.placeholder = opts.imagePlaceholder || "图片路径（可选）";
      image.spellcheck = false;
      image.value = data.imagePath;
      const browse = ce("button", "vd-btn qg-browse", "浏览");
      const upload = ce("button", "vd-btn qg-upload", "上传");
      const clear = ce("button", "vd-btn qg-clear", "清除");
      const file = ce("input", "qg-file");
      file.type = "file";
      file.accept = "image/*";
      file.hidden = true;
      browse.type = upload.type = clear.type = "button";
      imageRow.appendChild(image);
      imageRow.appendChild(browse);
      imageRow.appendChild(upload);
      imageRow.appendChild(clear);
      imageRow.appendChild(file);

      const preview = ce("div", "qg-preview");
      const img = ce("img", "qg-preview-img");
      img.alt = "";
      const name = ce("span", "qg-preview-name");
      preview.appendChild(img);
      preview.appendChild(name);
      preview.hidden = true;

      wrap.appendChild(textWrap);
      wrap.appendChild(imageRow);
      wrap.appendChild(preview);

      const syncCounter = () => {
        counter.textContent = text.value.length + " / " + maxTextLength;
        counter.classList.toggle("over", text.value.length >= maxTextLength);
      };
      const fire = () => {
        syncCounter();
        syncPreview(image, preview, img, name);
        if (typeof onChange === "function") onChange(getData());
      };
      const getData = () => ({
        text: text.value.trim(),
        imagePath: image.value.trim(),
      });

      text.addEventListener("input", fire);
      image.addEventListener("input", fire);
      image.addEventListener("change", fire);
      browse.addEventListener("click", () => {
        if (!window.VdFsModal) return;
        window.VdFsModal._sourceTarget = image;
        window.VdFsModal.open("");
      });
      upload.addEventListener("click", () => file.click());
      file.addEventListener("change", async () => {
        const f = file.files && file.files[0];
        if (!f || !uploadEndpoint) return;
        upload.disabled = true;
        try {
          const fd = new FormData();
          fd.append("file", f);
          const r = await fetch(uploadEndpoint, { method: "POST", body: fd });
          const d = await r.json().catch(() => ({}));
          if (!r.ok || !d.path) throw new Error(d.detail || ("HTTP " + r.status));
          image.value = d.path;
          image.dispatchEvent(new Event("change", { bubbles: true }));
        } catch (e) {
          if (typeof opts.onError === "function") {
            opts.onError("图片上传失败: " + (e && e.message ? e.message : e));
          }
        } finally {
          upload.disabled = ctl.disabled;
          file.value = "";
        }
      });
      clear.addEventListener("click", () => {
        image.value = "";
        image.dispatchEvent(new Event("change", { bubbles: true }));
      });
      if (!window.VdFsModal) browse.hidden = true;
      if (!uploadEndpoint) upload.hidden = true;
      syncCounter();
      syncPreview(image, preview, img, name);

      return {
        el: wrap,
        getData,
        focus() { text.focus(); },
        setDisabled(on) {
          wrap.querySelectorAll("input, textarea, button").forEach((el) => {
            el.disabled = !!on;
          });
        },
      };
    }

    function summaryOf(rec) {
      const text = rec.data.text.trim();
      const img = rec.data.imagePath.trim();
      const imgName = img ? img.split(/[\\/]/).pop() : "";
      if (text) return imgName ? text + " · 图片：" + imgName : text;
      if (imgName) return "以图检索 · " + imgName;
      return "未填写描述";
    }

    function updateRow(rec) {
      rec.data = rec.editor ? rec.editor.getData()
        : (rec.data || { text: "", imagePath: "" });
      const index = ctl.items.indexOf(rec);
      const summary = summaryOf(rec);
      if (mode === "entries") {
        rec.row.querySelector(".qg-entry-num").textContent = String(index + 1);
        rec.row.querySelector(".qg-entry-summary").textContent = summary;
      } else {
        rec.row.querySelector(".qg-inline-num").textContent = String(index + 1);
        rec.row.querySelector(".qg-inline-summary").textContent = summary;
        rec.row.querySelector(".qg-inline-toggle").textContent =
          rec.expanded ? "▾" : "▸";
        rec.body.hidden = !rec.expanded;
        rec.row.classList.toggle("is-open", rec.expanded);
      }
    }

    function renumber() {
      ctl.items.forEach((it, i) => {
        const num = it.row.querySelector(".qg-entry-num, .qg-inline-num");
        if (num) num.textContent = String(i + 1);
      });
      if (ctl.countEl) {
        ctl.countEl.textContent = ctl.items.length + " / " + maxGroups + " 组";
      }
      if (ctl.addBtn) ctl.addBtn.disabled = ctl.disabled || ctl.items.length >= maxGroups;
      if (ctl.emptyEl) ctl.emptyEl.hidden = ctl.items.length > 0;
    }

    ctl.sync = () => {
      ctl.items.forEach(updateRow);
      renumber();
    };

    function removeItem(rec) {
      const idx = ctl.items.indexOf(rec);
      if (idx < 0) return;
      ctl.items.splice(idx, 1);
      rec.row.remove();
      ctl.sync();
      emit();
    }

    function addItem(initial, expanded) {
      if (ctl.items.length >= maxGroups) return null;
      const data = normalizeGroup(initial);
      const rec = { data, editor: null, row: null, body: null,
                    expanded: mode === "entries" ? false : !!expanded };
        if (mode === "entries") {
          const row = ce("div", "qg-entry");
          row.tabIndex = 0;
          row.innerHTML =
            '<span class="qg-entry-num"></span>' +
            '<span class="qg-entry-summary"></span>' +
            '<button type="button" class="qg-entry-edit">编辑</button>' +
            '<button type="button" class="qg-entry-remove" title="删除该条目" aria-label="删除该条目">×</button>';
          rec.row = row;
          const edit = () => { if (!ctl.disabled) openModal(rec, false); };
          row.addEventListener("click", (e) => {
            if (e.target.closest("button")) return;
            edit();
          });
          row.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              edit();
            }
          });
          row.querySelector(".qg-entry-edit").addEventListener("click", edit);
          row.querySelector(".qg-entry-remove").addEventListener("click", (e) => {
            e.stopPropagation();
            removeItem(rec);
          });
          ctl.list.appendChild(row);
        } else {
          const row = ce("div", "qg-inline-item");
          row.innerHTML =
            '<div class="qg-inline-head">' +
              '<span class="qg-inline-num"></span>' +
              '<span class="qg-inline-summary"></span>' +
              '<button type="button" class="qg-inline-toggle" aria-label="展开或折叠">▸</button>' +
              '<button type="button" class="qg-inline-remove" title="删除该条目" aria-label="删除该条目">×</button>' +
            '</div>';
          const body = ce("div", "qg-inline-body");
          const editor = buildEditor(data, (next) => {
            rec.data = next;
            updateRow(rec);
            emit();
          });
          rec.row = row;
          rec.body = body;
          rec.editor = editor;
          body.appendChild(editor.el);
          const done = ce("button", "vd-btn qg-inline-done", "完成");
          done.type = "button";
          done.addEventListener("click", () => {
            rec.expanded = false;
            updateRow(rec);
          });
          body.appendChild(done);
          row.appendChild(body);
          row.querySelector(".qg-inline-head").addEventListener("click", (e) => {
            if (e.target.closest("button")) return;
            rec.expanded = !rec.expanded;
            updateRow(rec);
          });
          row.querySelector(".qg-inline-toggle").addEventListener("click", () => {
            rec.expanded = !rec.expanded;
            updateRow(rec);
          });
          row.querySelector(".qg-inline-remove").addEventListener("click", (e) => {
            e.stopPropagation();
            removeItem(rec);
          });
          ctl.list.appendChild(row);
        }
      ctl.items.push(rec);
      updateRow(rec);
      ctl.sync();
      return rec;
    }

    function ensureModal() {
      if (ctl.modal) return ctl.modal;
      const modal = ce("div", "qg-modal hidden");
      modal.setAttribute("role", "dialog");
      modal.setAttribute("aria-modal", "true");
      const card = ce("div", "qg-modal-card");
      const head = ce("div", "qg-modal-head");
      const title = ce("span", "qg-modal-title");
      const close = ce("button", "qg-modal-close", "×");
      close.type = "button";
      head.appendChild(title);
      head.appendChild(close);
      const body = ce("div", "qg-modal-body");
      const hint = ce("div", "qg-modal-hint");
      const foot = ce("div", "qg-modal-foot");
      const cancel = ce("button", "vd-btn qg-modal-cancel", "取消");
      const save = ce("button", "vd-btn vd-btn-primary qg-modal-save", "保存");
      cancel.type = save.type = "button";
      foot.appendChild(cancel);
      foot.appendChild(save);
      card.appendChild(head);
      card.appendChild(body);
      card.appendChild(hint);
      card.appendChild(foot);
      modal.appendChild(card);
      document.body.appendChild(modal);
      ctl.modal = modal;
      ctl.modalTitle = title;
      ctl.modalBody = body;
      ctl.modalHint = hint;
      const closeModal = (cancelNew) => {
        modal.classList.add("hidden");
        if (cancelNew && ctl.modalNew && ctl.modalRec) {
          removeItem(ctl.modalRec);
        }
        ctl.modalRec = null;
        ctl.modalNew = false;
        ctl.modalEditor = null;
      };
      ctl.closeModal = closeModal;
      close.addEventListener("click", () => closeModal(true));
      cancel.addEventListener("click", () => closeModal(true));
      modal.addEventListener("click", (e) => {
        if (e.target === modal) closeModal(true);
      });
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && !modal.classList.contains("hidden")) {
          closeModal(true);
        }
      });
      save.addEventListener("click", () => {
        if (!ctl.modalRec || !ctl.modalEditor) return;
        const data = ctl.modalEditor.getData();
        if (!data.text && !data.imagePath) {
          hint.textContent = "请至少填写描述文字或选择一张图片";
          return;
        }
        ctl.modalRec.data = data;
        updateRow(ctl.modalRec);
        ctl.sync();
        emit();
        closeModal(false);
      });
      return modal;
    }

    function openModal(rec, isNew) {
      if (ctl.disabled) return;
      const modal = ensureModal();
      ctl.modalRec = rec;
      ctl.modalNew = !!isNew;
      ctl.modalTitle.textContent = isNew ? "添加描述" : "编辑描述";
      ctl.modalBody.innerHTML = "";
      ctl.modalHint.textContent = "";
      ctl.modalEditor = buildEditor(rec.data, null);
      ctl.modalBody.appendChild(ctl.modalEditor.el);
      modal.classList.remove("hidden");
      setTimeout(() => ctl.modalEditor.focus(), 0);
    }

    ctl.add = () => {
      if (ctl.disabled || ctl.items.length >= maxGroups) return;
      if (mode === "entries") {
        const rec = addItem({}, false);
        if (rec) openModal(rec, true);
      } else {
        const rec = addItem({}, true);
        if (rec) {
          rec.expanded = true;
          updateRow(rec);
          rec.editor.focus();
          emit();
        }
      }
    };

    ctl.addBtn.addEventListener("click", ctl.add);

    ctl.setValues = (groups) => {
      ctl.items.forEach((it) => it.row.remove());
      ctl.items = [];
      const arr = Array.isArray(groups) ? groups.slice(0, maxGroups) : [];
      arr.forEach((g) => addItem(g, false));
      if (!arr.length && emptyRows > 0) {
        for (let i = 0; i < emptyRows; i++) addItem({}, true);
      }
      ctl.sync();
    };

    ctl.setDisabled = (disabled) => {
      ctl.disabled = !!disabled;
      container.classList.toggle("qg-disabled", ctl.disabled);
      ctl.items.forEach((it) => {
        if (it.editor) it.editor.setDisabled(ctl.disabled);
        it.row.querySelectorAll("button").forEach((el) => {
          el.disabled = ctl.disabled;
        });
      });
      if (ctl.addBtn) ctl.addBtn.disabled = ctl.disabled;
      renumber();
    };

    ctl.clear = () => ctl.setValues([]);
    ctl.values = () => valuesOf(ctl);

    container.__queryGroups = ctl;
    ctl.setValues(opts.initial || []);
    return ctl;
  }

  window.QueryGroups = {
    mount,
    getValues: valuesOf,
    setValues(el, groups) {
      const ctl = controllerOf(el);
      if (ctl) ctl.setValues(groups);
      return ctl;
    },
    clear(el) {
      const ctl = controllerOf(el);
      if (ctl) ctl.clear();
    },
    setDisabled(el, disabled) {
      const ctl = controllerOf(el);
      if (ctl) ctl.setDisabled(disabled);
    },
  };
})();
