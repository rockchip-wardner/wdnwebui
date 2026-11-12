/* translate.js —— 右上角「界面语言」切换。
 *
 * 为什么是「自己画下拉 + 板端代理」而不是挂现成的 translate.js 挂件
 * ----------------------------------------------------------
 * 浏览器在局域网里**访问不了** Google 系域名(translate.google.com /
 * translate.googleapis.com / translate.googleusercontent.com)—— 实测本板到
 * 这三个域名全部连接超时(而阿里云镜像 / 百度 / 有道 / GitHub 都通)。而
 * translate.js 官方挂件正是:从 translate.google.com 加载脚本 + 由**浏览器**
 * 去 Google 后端取译文 —— 两头都断在这套网络里。
 *
 * 所以这里的分工是:
 *   * 译文来源 = 板子上的 POST /api/translate(板子去够真正干活的 provider,
 *     默认 MyMemory;浏览器全程不碰任何境外站点);
 *   * 界面控件 = 本文件自己画的右上角下拉 —— 不依赖任何第三方挂件的 DOM
 *     结构,样式跟本站一致。
 *
 * 翻译对象 = 页面里的**文本节点**:
 *   * 只翻含中日韩字符的文本 —— 纯英文 / 数字 / 标识符一律不动(翻一遍只会
 *     把 "wdnai v0.9.3" 变成看不懂的东西);
 *   * 跳过 script/style/pre/code/表单控件,以及带 translate="no" 的元素和
 *     日志正文(那些是数据,不是界面文案);
 *   * 每个被翻过的节点记住**原文**,「中文(原文)」一项可以原样还原;
 *   * 切语言时**从原文重新翻**,不是从当前译文再翻(否则会出现中→英→日的
 *     二次翻译损耗)。
 *
 * 懒加载进来的内容(卡片 app.js 注入的 DOM)由 MutationObserver 兜住:有新
 * 文本节点进来就补翻一次。
 */
(function () {
  "use strict";

  // 界面原文语言(本站是中文界面)
  const SOURCE = "zh-CN";
  const API = "/api/translate";
  const STORAGE_KEY = "wdnwebui.translate.lang";
  // 一次请求多少条文案。服务端一批 ≤480 字符 / ≤60 条,这里取 24 —— 一片
  // 基本就是服务端的**一批**,第一片回来就能把当前这一页换掉(不用等整页)。
  const CHUNK_SIZE = 24;

  // 常用语言清单(26 种)。语言码给 provider 用(MyMemory 收 ISO 639-1 /
  // RFC3066,比如 zh-CN / en / ja)。
  const LANGUAGES = [
    { code: "zh-TW", zh: "中文（繁体）", native: "繁體中文" },
    { code: "en",  zh: "英语",       native: "English" },
    { code: "ja",  zh: "日语",       native: "日本語" },
    { code: "ko",  zh: "韩语",       native: "한국어" },
    { code: "fr",  zh: "法语",       native: "Français" },
    { code: "de",  zh: "德语",       native: "Deutsch" },
    { code: "es",  zh: "西班牙语",   native: "Español" },
    { code: "pt",  zh: "葡萄牙语",   native: "Português" },
    { code: "it",  zh: "意大利语",   native: "Italiano" },
    { code: "ru",  zh: "俄语",       native: "Русский" },
    { code: "ar",  zh: "阿拉伯语",   native: "العربية" },
    { code: "hi",  zh: "印地语",     native: "हिन्दी" },
    { code: "th",  zh: "泰语",       native: "ไทย" },
    { code: "vi",  zh: "越南语",     native: "Tiếng Việt" },
    { code: "id",  zh: "印尼语",     native: "Bahasa Indonesia" },
    { code: "ms",  zh: "马来语",     native: "Bahasa Melayu" },
    { code: "tr",  zh: "土耳其语",   native: "Türkçe" },
    { code: "pl",  zh: "波兰语",     native: "Polski" },
    { code: "nl",  zh: "荷兰语",     native: "Nederlands" },
    { code: "sv",  zh: "瑞典语",     native: "Svenska" },
    { code: "da",  zh: "丹麦语",     native: "Dansk" },
    { code: "no",  zh: "挪威语",     native: "Norsk" },
    { code: "fi",  zh: "芬兰语",     native: "Suomi" },
    { code: "cs",  zh: "捷克语",     native: "Čeština" },
    { code: "el",  zh: "希腊语",     native: "Ελληνικά" },
    { code: "he",  zh: "希伯来语",   native: "עברית" },
    { code: "uk",  zh: "乌克兰语",   native: "Українська" },
    { code: "hu",  zh: "匈牙利语",   native: "Magyar" },
  ];

  // 这些标签 / 容器里的文本不翻
  const SKIP_TAGS = new Set([
    "SCRIPT", "STYLE", "NOSCRIPT", "TEXTAREA", "INPUT", "SELECT",
    "OPTION", "PRE", "CODE", "KBD", "SAMP", "VAR",
  ]);
  const SKIP_SELECTOR = [
    "[translate='no']", ".notranslate", ".no-translate",
    // 日志正文 / JSON 原文是**数据**,不是界面文案
    "#logs-list", ".logs-empty", ".raw-output", ".payload",
  ].join(",");
  // 只翻含中日韩字符的文本
  const CJK_RE = /[㐀-䶿一-鿿豈-﫿]/;

  const state = {
    target: SOURCE,   // 当前界面语言(SOURCE = 中文原文)
    source: SOURCE,   // 原文语言(后端可改,默认 zh-CN)
    busy: false,
    provider: "",     // 最后干活的 provider
    providers: [],    // 故障转移顺序
    labels: {},       // provider 名 → 中文显示名(后端给的)
    applied: 0,       // 本次已经翻出去的条数(失败回滚时用)
  };

  let els = null;
  let observer = null;
  let pendingTimer = null;

  // ─────────────────────────────────────────────────────────────────────
  // DOM 小工具
  // ─────────────────────────────────────────────────────────────────────
  function h(tag, cls, text) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text != null) el.textContent = text;
    return el;
  }

  function langByCode(code) {
    if (code === SOURCE) return { code: SOURCE, zh: "中文(原文)", native: "简体中文" };
    return LANGUAGES.find((l) => l.code === code) || null;
  }

  // ─────────────────────────────────────────────────────────────────────
  // 控件
  // ─────────────────────────────────────────────────────────────────────
  function buildUI() {
    const root = h("div", "wdn-lang notranslate");
    root.setAttribute("translate", "no");

    const btn = h("button", "wdn-lang-btn");
    btn.type = "button";
    btn.title = "界面语言";
    btn.setAttribute("aria-haspopup", "listbox");
    btn.setAttribute("aria-expanded", "false");
    btn.innerHTML =
      '<svg class="wdn-lang-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="1.7" stroke-linecap="round" aria-hidden="true">' +
      '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/>' +
      '<path d="M12 3c2.5 2.7 3.8 5.7 3.8 9s-1.3 6.3-3.8 9c-2.5-2.7-3.8-5.7-3.8-9S9.5 5.7 12 3z"/>' +
      "</svg>";
    const name = h("span", "wdn-lang-name", "中文");
    const caret = h("span", "wdn-lang-caret", "▾");
    // 翻译期间显示的百分比(平时隐藏)
    const pct = h("span", "wdn-lang-pct");
    pct.hidden = true;
    btn.append(name, caret, pct);

    // 常驻在按钮下方的进度条 —— 进度不能只写在「要点开才看得到」的下拉里,
    // 用户点完语言就该看见「在翻了、到百分之几」。
    const progress = h("div", "wdn-lang-progress");
    progress.hidden = true;
    const fill = h("div", "wdn-lang-progress-fill");
    progress.appendChild(fill);

    const panel = h("div", "wdn-lang-panel");
    panel.hidden = true;
    panel.setAttribute("role", "listbox");
    const title = h("div", "wdn-lang-title", "界面语言");
    const list = h("ul", "wdn-lang-list");
    const status = h("div", "wdn-lang-status");
    status.hidden = true;

    list.appendChild(languageItem({ code: SOURCE, zh: "中文(原文)", native: "简体中文" }));
    LANGUAGES.forEach((lang) => list.appendChild(languageItem(lang)));

    panel.append(title, list, status);
    root.append(btn, progress, panel);
    document.body.appendChild(root);

    els = { root, btn, name, caret, pct, progress, fill, panel, list, status };

    btn.addEventListener("click", () => togglePanel());
    panel.addEventListener("click", (ev) => {
      const item = ev.target.closest(".wdn-lang-item");
      if (!item) return;
      choose(item.dataset.code);
    });
    document.addEventListener("click", (ev) => {
      if (!root.contains(ev.target)) closePanel();
    });
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape") closePanel();
    });
  }

  function languageItem(lang) {
    const li = h("li");
    const btn = h("button", "wdn-lang-item");
    btn.type = "button";
    btn.dataset.code = lang.code;
    btn.setAttribute("role", "option");
    btn.append(
      h("span", "wdn-lang-item-zh", lang.zh),
      h("span", "wdn-lang-item-native", lang.native)
    );
    li.appendChild(btn);
    return li;
  }

  function togglePanel() {
    if (els.panel.hidden) showPanel();
    else closePanel();
  }

  function showPanel() {
    els.panel.hidden = false;
    els.btn.setAttribute("aria-expanded", "true");
  }

  function closePanel() {
    if (!els) return;
    els.panel.hidden = true;
    els.btn.setAttribute("aria-expanded", "false");
  }

  function updateButton() {
    const lang = langByCode(state.target);
    els.name.textContent = lang ? lang.native : "中文";
    // 翻译期间**不**禁用按钮:禁用了用户就点不开面板,看不到进度 —— 而一次
    // 整页翻译要几十秒,期间必须让人看得见「在翻第几片」。
    els.btn.setAttribute("aria-busy", state.busy ? "true" : "false");
    els.caret.hidden = state.busy;      // 忙的时候位置让给百分比
    let title = "界面语言";
    // provider 名字显示成中文(后端给一张对照表),没有就显示原名
    const labelOf = (name) => state.labels[name] || name;
    if (state.providers && state.providers.length > 1) {
      title += "（译文由板端 " + state.providers.map(labelOf).join(" → ") + " 提供）";
    } else if (state.provider) {
      title += "（译文由板端 " + labelOf(state.provider) + " 提供）";
    }
    els.btn.title = title;
    els.list.querySelectorAll(".wdn-lang-item").forEach((item) => {
      item.classList.toggle("active", item.dataset.code === state.target);
    });
  }

  /** 进度条 + 百分比。``total`` 为 0 时只显示条、不显示数字。 */
  function setProgress(done, total) {
    const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
    els.progress.hidden = false;
    els.fill.style.width = pct + "%";
    if (total > 0) {
      els.pct.hidden = false;
      els.pct.textContent = pct + "%";
    }
  }

  function clearProgress() {
    els.progress.hidden = true;
    els.fill.style.width = "0%";
    els.pct.hidden = true;
    els.pct.textContent = "";
  }

  function setStatus(text, kind) {
    if (!text) {
      els.status.hidden = true;
      els.status.textContent = "";
      return;
    }
    els.status.hidden = false;
    els.status.textContent = text;
    els.status.dataset.kind = kind || "busy";
  }

  // ─────────────────────────────────────────────────────────────────────
  // 翻译
  // ─────────────────────────────────────────────────────────────────────
  function isTranslatable(node) {
    const parent = node.parentElement;
    if (!parent) return false;
    if (SKIP_TAGS.has(parent.tagName)) return false;
    if (parent.closest(SKIP_SELECTOR)) return false;
    return true;
  }

  /** 收集要翻的文本节点。``onlyNew`` = 只收还没翻成当前目标语言的。 */
  function collect(onlyNew, target) {
    // 当前可见的页先收 —— 分片是按收集顺序发的,这样第一片就是「眼前这一页」,
    // 用户几秒内就看到它变了;剩下的页面在后面慢慢跟上。整页 300+ 条一起等
    // (冷缓存时可能好几分钟)是不能接受的体验。
    const active = document.querySelector(".page:not(.hidden)");
    const visible = [];
    const rest = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      const raw = node.nodeValue;
      if (!raw || !raw.trim()) continue;
      // 原文优先:已翻过的节点要从原文重翻,不能拿译文再翻一遍
      const text = node.__wdnOrig !== undefined ? node.__wdnOrig : raw;
      if (!CJK_RE.test(text)) continue;
      if (onlyNew && node.__wdnTarget === target) continue;
      if (!isTranslatable(node)) continue;
      (active && active.contains(node) ? visible : rest).push({ node, text });
    }
    return visible.concat(rest);
  }

  async function requestTranslations(segments, target) {
    const res = await fetch(API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ segments, source: state.source, target }),
    });
    if (!res.ok) {
      let detail = "HTTP " + res.status;
      try {
        const data = await res.json();
        if (data && data.detail) {
          detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
        }
      } catch (_) { /* 非 JSON 错误体,就用状态码 */ }
      throw new Error(detail);
    }
    return await res.json();
  }

  /** 把整页(或新增部分)翻成 ``target``。 */
  async function translateTo(target, onlyNew) {
    const items = collect(!!onlyNew, target);
    if (!items.length) return 0;
    setProgress(0, items.length);
    // 分片请求:一次 300+ 条会变成一次几十秒的长请求,浏览器那边只能干等。
    // 拆成 24 条一片 —— 服务端一批 ≤480 字符 ≈ 24 条,所以基本一片一批,
    // 第一片回来就能把**当前这一页**换掉,不用等整页。
    let done = 0;
    let failed = 0;
    for (let i = 0; i < items.length; i += CHUNK_SIZE) {
      const chunk = items.slice(i, i + CHUNK_SIZE);
      const data = await requestTranslations(chunk.map((it) => it.text), target);
      const translations = data.translations || [];
      if (!translations || translations.length !== chunk.length) {
        throw new Error(
          "翻译结果条数对不上(" + translations.length + "/" + chunk.length + ")"
        );
      }
      failed += data.failed || 0;
      chunk.forEach((item, j) => {
        const out = translations[j];
        if (!out) return;
        const node = item.node;
        node.__wdnOrig = item.text;     // 原文(还原 / 重翻都靠它)
        node.__wdnTrans = out;          // 当前译文(用来认出"这是我们自己写的")
        node.__wdnTarget = target;
        if (node.nodeValue !== out) node.nodeValue = out;
      });
      done += chunk.length;
      state.applied += chunk.length;
      setProgress(done, items.length);
      setStatus("正在翻译… " + done + "/" + items.length, "busy");
    }
    // 少数几条翻不出来(免费接口抽风)不算失败,但得让用户知道
    return failed;
  }

  /** 还原中文原文。 */
  function restoreOriginal() {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      if (node.__wdnOrig === undefined) continue;
      const orig = node.__wdnOrig;
      // 先把"当前译文"标成原文,免得被 MutationObserver 误判成"应用改了内容"
      node.__wdnTrans = orig;
      node.nodeValue = orig;
      delete node.__wdnOrig;
      delete node.__wdnTrans;
      delete node.__wdnTarget;
    }
  }

  async function applyTarget(code) {
    if (state.busy) return;
    const prev = state.target;
    state.busy = true;
    state.applied = 0;
    // **先改状态再翻**:按钮标题、列表高亮立刻变成新语言,不用等整页翻完。
    // 翻失败了再回滚(下面 catch 里)。
    state.target = code;
    updateButton();
    showPanel();
    setStatus(code === SOURCE ? "正在还原中文原文…" : "正在翻译…", "busy");
    try {
      if (code === SOURCE) {
        restoreOriginal();
        persist(code);
        setStatus("", null);
        closePanel();
      } else {
        const failed = await translateTo(code, false);
        persist(code);
        setStatus(
          failed > 0 ? "已翻译,但有 " + failed + " 条没翻出来(仍是中文)" : "",
          failed > 0 ? "busy" : null
        );
        if (failed === 0) closePanel();
      }
    } catch (err) {
      if (state.applied === 0) {
        // 一个字都没翻出去 → 页面内容还是上一个语言的样子,状态跟着回去
        state.target = prev;
      } else {
        // 翻了一半 → 还原成中文,别让用户对着中英混杂的页面猜哪儿出了问题
        restoreOriginal();
        state.target = SOURCE;
      }
      setStatus("翻译失败:" + (err && err.message ? err.message : err), "error");
      throw err;
    } finally {
      state.busy = false;
      clearProgress();
      updateButton();
    }
  }

  async function choose(code) {
    closePanel();
    if (!code || code === state.target || state.busy) return;
    try {
      await applyTarget(code);
    } catch (_) {
      // 失败信息写在面板里 —— 面板是关着的,得弹回来,不然用户只看到"点了
      // 没反应",不知道是配额用尽还是网络不通。
      showPanel();
    }
  }

  // ─────────────────────────────────────────────────────────────────────
  // 新增内容兜底(卡片懒加载注入的 DOM)
  // ─────────────────────────────────────────────────────────────────────
  function scheduleTranslate() {
    if (state.target === SOURCE || state.busy) return;
    clearTimeout(pendingTimer);
    pendingTimer = setTimeout(() => {
      if (state.target === SOURCE) return;
      translateTo(state.target, true).catch((err) => {
        console.warn("[translate] 增量翻译失败:", err);
      });
    }, 900);
  }

  function onMutations(mutations) {
    if (state.target === SOURCE) return;
    let dirty = false;
    for (const m of mutations) {
      if (m.type === "characterData") {
        const node = m.target;
        // 我们自己写的译文 → 忽略
        if (node.__wdnTrans !== undefined && node.nodeValue === node.__wdnTrans) continue;
        // 应用把内容改了 → 重新当成待翻的新文案
        if (node.__wdnOrig !== undefined) {
          delete node.__wdnOrig;
          delete node.__wdnTrans;
          delete node.__wdnTarget;
        }
        if (CJK_RE.test(node.nodeValue || "")) dirty = true;
      } else if (m.type === "childList" && m.addedNodes && m.addedNodes.length) {
        dirty = true;
      }
    }
    if (dirty) scheduleTranslate();
  }

  function startObserver() {
    if (observer) return;
    observer = new MutationObserver(onMutations);
    observer.observe(document.body, {
      childList: true,
      subtree: true,
      characterData: true,
    });
  }

  // ─────────────────────────────────────────────────────────────────────
  // 启动
  // ─────────────────────────────────────────────────────────────────────
  function persist(code) {
    try {
      if (code === SOURCE) localStorage.removeItem(STORAGE_KEY);
      else localStorage.setItem(STORAGE_KEY, code);
    } catch (_) { /* 隐私模式下 localStorage 会抛,忽略 */ }
  }

  function savedTarget() {
    try {
      const v = localStorage.getItem(STORAGE_KEY);
      return v && langByCode(v) ? v : null;
    } catch (_) {
      return null;
    }
  }

  async function loadStatus() {
    try {
      const res = await fetch(API + "/status");
      if (!res.ok) return;
      const data = await res.json();
      if (data && data.source) state.source = data.source;
      if (data && data.provider) state.provider = data.provider;
      if (data && data.providers) state.providers = data.providers;
      if (data && data.provider_labels) state.labels = data.provider_labels;
      updateButton();
    } catch (_) { /* 拿不到就按默认值走 */ }
  }

  async function boot() {
    buildUI();
    updateButton();
    await loadStatus();
    const saved = savedTarget();
    if (saved) {
      // 上次选过语言 → 进来就自动翻(服务端有缓存,第二次起是秒回)
      try {
        await applyTarget(saved);
      } catch (_) { /* 失败状态已在面板里 */ }
    }
    startObserver();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  // 给别的模块用(比如卡片页想知道自己被翻成什么语言)
  window.Translate = {
    get target() { return state.target; },
    get busy() { return state.busy; },
    languages: LANGUAGES,
    setLanguage: choose,
  };
})();
