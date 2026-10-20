/**
 * wizard env_check 步骤回归 —— 纯 Node + 最小 DOM shim，**不需要 playwright**。
 *
 * 背景：空能力列表的卡（任何 software_capabilities=[] 的卡）的 manifest 里
 * ``software_capabilities`` / ``hardware_capabilities`` 都是空数组，
 * env_check 步骤会渲染出两张**只有标题没有内容**的卡。为修这个视觉问题
 * 给 ``_renderEnvStep`` 加了 ``_envNoReqRow()`` 占位说明 ——
 * 这是对**所有 8 张卡共享**的 framework 代码动手，所以必须验证：
 *
 *   (a) 能力列表非空的老卡渲染结果**逐字节不变**（不出现 env-none 行）；
 *   (b) 空能力列表的新卡出现占位行，且「下一步」仍然可点；
 *   (c) 能力缺失的老卡仍然渲染 ✗ 行 + 「一键开启」按钮 + 下一步禁用。
 *
 * 用法: node scripts/verify_wizard_env_step.mjs
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "..");
const STATE_JS = path.join(REPO, "app/static/core/wizard/state.js");

// ---------------------------------------------------------------- //
// 最小 DOM shim —— 只实现 state.js 实际用到的那几个方法
// ---------------------------------------------------------------- //
class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this._className = "";
    this._text = "";
    this.dataset = {};
    this.style = {};
    this.id = "";
    this.hidden = false;
    this.disabled = false;
    this.type = "";
    this.value = "";
    this.checked = false;
    this.listeners = {};
    const self = this;
    this.classList = {
      add(...cs) {
        const cur = new Set(self.className.split(/\s+/).filter(Boolean));
        cs.forEach((c) => cur.add(c));
        self.className = [...cur].join(" ");
      },
      remove(...cs) {
        const cur = new Set(self.className.split(/\s+/).filter(Boolean));
        cs.forEach((c) => cur.delete(c));
        self.className = [...cur].join(" ");
      },
      toggle(c, on) {
        if (on === undefined) { on = !self.classList.contains(c); }
        if (on) self.classList.add(c); else self.classList.remove(c);
        return on;
      },
      contains(c) {
        return self.className.split(/\s+/).includes(c);
      },
    };
  }
  set className(v) { this._className = String(v); }
  get className() { return this._className || ""; }
  set textContent(v) { this._text = v == null ? "" : String(v); this.children = []; }
  get textContent() { return this._text; }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  removeChild(c) {
    const i = this.children.indexOf(c);
    if (i >= 0) this.children.splice(i, 1);
    c.parentNode = null;
    return c;
  }
  insertBefore(n, ref) {
    const i = this.children.indexOf(ref);
    n.parentNode = this;
    if (i < 0) this.children.push(n); else this.children.splice(i, 0, n);
    return n;
  }
  addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }
  removeEventListener() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
  closest() { return null; }
  click() {}
  /** 深度优先收集 className 完全匹配的节点 */
  findByClass(cls) {
    const out = [];
    const walk = (n) => {
      if (n.className && n.className.split(/\s+/).includes(cls)) out.push(n);
      n.children.forEach(walk);
    };
    walk(this);
    return out;
  }
  findByTag(tag) {
    const out = [];
    const walk = (n) => {
      if (n.tagName === tag.toUpperCase()) out.push(n);
      n.children.forEach(walk);
    };
    walk(this);
    return out;
  }
  dump() {
    const cls = this.className ? ` class="${this.className}"` : "";
    if (this._text && !this.children.length) return `${this.tagName}${cls}: ${this._text}`;
    const kids = this.children.map((c) => "  " + c.dump()).join("\n");
    return `${this.tagName}${cls}\n${kids}`;
  }
}

const document = {
  createElement: (t) => new El(t),
  getElementById: () => null,
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener: () => {},
  removeEventListener: () => {},
  body: new El("body"),
  hidden: false,
};
const localStorage = {
  _m: new Map(),
  getItem(k) { return this._m.has(k) ? this._m.get(k) : null; },
  setItem(k, v) { this._m.set(k, String(v)); },
  removeItem(k) { this._m.delete(k); },
};

// ---------------------------------------------------------------- //
// 载入 state.js（IIFE 内部私有函数不导出，这里改写源码把它们暴露出来）
// ---------------------------------------------------------------- //
let src = fs.readFileSync(STATE_JS, "utf8");
// 在 IIFE 结尾前追加导出语句：把私有的 _renderEnvStep / _probeEnv 等挂到 window。
src = src.replace(
  /window\.WizardState = \{ mount, unmount, stepForward, skip \};/,
  `window.WizardState = { mount, unmount, stepForward, skip };
   window.__wiz_priv = { _renderEnvStep, _probeEnv, _envNoReqRow, _fieldKey };`
);

const computeFixtures = {
  // 本机 spawned 跑着 qwen3-vl-4b（覆盖 describe + retrieval）
  running: {
    instances: [
      { status: "running", _source: "spawned",
        capability_types: ["swcap_video_describe", "swcap_video_retrieval"] },
      // peer adopted —— 按 _probeEnv 语义**不算**本机就绪
      { status: "running", _source: "adopted",
        capability_types: ["swcap_audio_tts"] },
    ],
  },
  empty: { instances: [] },
};

let fetchMode = "running";
globalThis.fetch = async () => ({
  ok: true,
  json: async () => computeFixtures[fetchMode],
});
globalThis.window = globalThis;
globalThis.document = document;
globalThis.localStorage = localStorage;
globalThis.CSS = { escape: (s) => s };
globalThis.Blob = class { constructor() {} };
globalThis.FormData = class { append() {} };
globalThis.console = console;

// eslint-disable-next-line no-new-func
new Function(src)();
const priv = globalThis.__wiz_priv;

// ---------------------------------------------------------------- //
// 断言
// ---------------------------------------------------------------- //
let pass = 0, fail = 0;
function check(name, ok, detail = "") {
  if (ok) { pass++; console.log(`  ✓ ${name}${detail ? "  (" + detail + ")" : ""}`); }
  else { fail++; console.log(`  ✗ ${name}  ${detail}`); }
}

const NS = "wd-wizard-";

const DEFAULT_STEPS = [
  { id: "env", kind: "env_check" }, { id: "finish", kind: "finish" },
];

function renderEnv(app, envCheck, cfg = null) {
  const cfgIn = cfg || { steps: DEFAULT_STEPS };
  const steps = cfgIn.steps || DEFAULT_STEPS;
  const body = new El("div");
  const foot = new El("div");
  priv._renderEnvStep(body, foot, cfgIn, steps[0], {
    envCheck, currentStep: 0, values: {},
    cfg: { steps },
  });
  return { body, foot, text: body.dump(), html: body.dump() };
}

const nextBtn = (foot) => foot.findByTag("button")
  .find((b) => b.textContent === "下一步");

console.log("【A】老卡（能力列表非空）渲染不变 —— 不应出现 env-none 行");
{
  fetchMode = "running";
  const envCheck = await priv._probeEnv(
    { swcap_to_model: { swcap_audio_understand: { model_id: "qwen2.5-omni-3b" } } },
    { software_capabilities: ["swcap_audio_understand"],
      hardware_capabilities: ["hwcap_venc"] },
  );
  // swcap_audio_understand 只挂在 peer adopted 上 → 按语义算「本机未启」
  check("peer adopted 不算本机就绪", JSON.stringify(envCheck.swMissing) === '["swcap_audio_understand"]',
    JSON.stringify(envCheck.swMissing));
  check("hwRequired 原样透传", JSON.stringify(envCheck.hwRequired) === '["hwcap_venc"]');

  const { body, foot } = renderEnv({ }, envCheck);
  check("未渲染 env-none 占位行", body.findByClass(NS + "env-none").length === 0,
    "找到 " + body.findByClass(NS + "env-none").length + " 个");
  check("渲染了 2 条能力行",
    body.findByClass(NS + "env-row").length === 2,
    String(body.findByClass(NS + "env-row").length));
  check("有 ✗ 行（swMissing 非空）", body.findByClass(NS + "env-fail").length === 1);
  check("有「一键开启所需模型」按钮",
    !!body.findByTag("button").find((b) => b.textContent === "一键开启所需模型"));
  // _probeEnv 恒返回 hwMissing: []（board HW 探测留给应用自己 assert），
  // 所以硬件那张卡永远不会有 env-error —— 记在这里防止后人误判。
  check("_probeEnv 的 hwMissing 恒为空（硬件错误条是死代码）",
    JSON.stringify(envCheck.hwMissing) === "[]", JSON.stringify(envCheck.hwMissing));
  check("无硬件错误条", body.findByClass(NS + "env-error").length === 0);
  check("下一步 disabled", nextBtn(foot) && nextBtn(foot).disabled === true);
}

console.log("\n【A2】老卡全部能力就绪 —— 渲染不变 + 下一步可点");
{
  fetchMode = "running";
  const envCheck = await priv._probeEnv({},
    { software_capabilities: ["swcap_video_describe"],
      hardware_capabilities: ["hwcap_venc"] });
  check("无缺失", envCheck.swMissing.length === 0);
  const { body, foot } = renderEnv({}, envCheck);
  check("未渲染 env-none 占位行", body.findByClass(NS + "env-none").length === 0);
  check("2 条 ✓ 行", body.findByClass(NS + "env-ok").length === 2);
  check("无「一键开启」按钮",
    !body.findByTag("button").find((b) => b.textContent === "一键开启所需模型"));
  check("下一步 enabled", nextBtn(foot) && nextBtn(foot).disabled === false);
}

console.log("\n【B】空能力列表的卡—— 出现占位说明且不卡住");
{
  fetchMode = "running";
  const envCheck = await priv._probeEnv({},
    { software_capabilities: [], hardware_capabilities: [] });
  check("hwRequired 空", envCheck.hwRequired.length === 0);
  check("swRequired 空", envCheck.swRequired.length === 0);
  const { body, foot } = renderEnv({}, envCheck);
  const noneRows = body.findByClass(NS + "env-none");
  check("渲染 2 条 env-none 占位行", noneRows.length === 2, String(noneRows.length));
  check("硬件占位文案正确",
    noneRows[0].textContent.includes("不依赖特定硬件能力"), noneRows[0].textContent);
  check("软件占位文案正确",
    noneRows[1].textContent.includes("不依赖模型"), noneRows[1].textContent);
  check("占位行不是 env-fail", body.findByClass(NS + "env-fail").length === 0);
  check("无「一键开启」按钮",
    !body.findByTag("button").find((b) => b.textContent === "一键开启所需模型"));
  check("**下一步 enabled（不卡住）**",
    nextBtn(foot) && nextBtn(foot).disabled === false);
  check("保留「跳过向导」",
    !!foot.findByTag("button").find((b) => b.textContent === "跳过向导"));
}

console.log("\n【C】_fieldKey：wizard.json 的 ui_key 优先");
{
  // 真实契约：_fieldKey 只在 field.id 以 "<appId>-" 开头时才剥前缀。
  // 实际 7 张卡里**没有一张**用 "<appId>-" 前缀（都用 2~3 字母的短前缀），
  // 所以剥前缀分支实际是死路 —— opts key 完全由 ui_key 决定，缺失时退化成
  // 带前缀的 camelCase（vcs-source → vcsSource）。各卡因此每个字段都显式
  // 写了 ui_key。
  check("显式 ui_key 优先于 id 推导",
    priv._fieldKey("video-target-localization", { id: "vtl-sources", ui_key: "source" }) === "source");
  check("前缀不匹配时不剥（video-analysis-search 同款短前缀）",
    priv._fieldKey("video-analysis-search", { id: "vcs-source" }) === "vcsSource");
  check("前缀匹配时才剥",
    priv._fieldKey("text-chat", { id: "text-chat-prompt" }) === "prompt");
  // 全卡扫描：任何一张卡的 form 字段漏写 ui_key 都要被抓出来 —— opts key
  // 全靠它，漏一个就静默退化成 sttSource 这种带前缀的名字。
  const appDirs = fs.readdirSync(path.join(REPO, "app/apps"))
    .filter((d) => fs.existsSync(path.join(REPO, "app/apps", d, "static/wizard.json")));
  const bad = [];
  for (const a of appDirs) {
    const wz = JSON.parse(fs.readFileSync(
      path.join(REPO, `app/apps/${a}/static/wizard.json`), "utf8"));
    for (const s of wz.steps.filter((x) => x.kind === "form")) {
      for (const f of s.fields || []) if (!f.ui_key) bad.push(`${a}/${f.id}`);
    }
  }
  check("全部带 wizard 的卡，每个 form 字段都有显式 ui_key",
    bad.length === 0, bad.join(", "));
}

console.log("\n【D】4 步向导卡的 wizard.json 与 manifest 自洽（以 video-target-localization 为样本）");
{
  const APP = "video_target_localization";
  const mf = JSON.parse(fs.readFileSync(
    path.join(REPO, `app/apps/${APP}/manifest.json`), "utf8"));
  const wj = JSON.parse(fs.readFileSync(
    path.join(REPO, `app/apps/${APP}/static/wizard.json`), "utf8"));
  check("manifest 声明 wizard_config", mf.wizard_config === "wizard.json");
  check("wizard.enabled=true", wj.enabled === true);
  // 断言刻意写成"首步 env_check / 末步 finish"而不是"正好 4 步"—— 卡片
  // 之后又加了「帧判据」步骤（env→source→ground→judge→finish），把步数
  // 写死会让每次加工具步骤都要来改测试，而这里真正要守的是**结构自洽**。
  check("第 1 步是 env_check", wj.steps[0].kind === "env_check");
  check("最后一步是 finish",
    wj.steps[wj.steps.length - 1].kind === "finish",
    wj.steps[wj.steps.length - 1].id);
  check("含帧判据配置（曲线只在向导里出现，配置得能在向导里改）",
    wj.steps.some((s) => s.fields
      && s.fields.some((f) => f.ui_key === "judgeMode")),
    wj.steps.map((s) => s.id).join("→"));
  check("判据配置在**视频源**步里（与曲线同屏，不单独占一步）",
    (wj.steps.find((s) => s.id === "source")?.fields || [])
      .some((f) => f.ui_key === "judgeMode"));
  check("open_source_button 指向页面按钮", wj.open_source_button === "vtl-open",
    wj.open_source_button);
  check("canvas id 可由 open_source_button 推导",
    wj.open_source_button.replace(/-open$/, "-canvas") === "vtl-canvas");
  const tpl = fs.readFileSync(
    path.join(REPO, `app/apps/${APP}/static/template.html`), "utf8");
  check("模板里有 #vtl-open", tpl.includes('id="vtl-open"'));
  check("模板里有 #vtl-canvas", tpl.includes('id="vtl-canvas"'));
  // wizard 关闭按钮会按 .vd-stage/.vtd-stage/.vtl-stage 把 canvas 搬回页面
  check("stage 复用了 wizard 认识的选择器类",
    /class="[^"]*\b(vd|vtd|vtl)-stage\b[^"]*"/.test(tpl));
  check("source 步骤字段 id 以 -source 结尾（触发浏览/打开按钮）",
    wj.steps[1].fields.some((f) => /-source$/.test(f.id)));
  check("source 步骤含 -loop（inline 到 source 行）",
    wj.steps[1].fields.some((f) => /-loop$/.test(f.id)));
  const js = fs.readFileSync(
    path.join(REPO, `app/apps/${APP}/static/app.js`), "utf8");
  check("app.js 导出到 WizardState 约定的全局名",
    new RegExp(`window\\.ImApp_${APP}\\s*=\\s*`).test(js), APP);
  // WizardState 按 "ImApp_" + id.replace(/-/g,"_") 查找并调用 applyWizardSnapshot
  check("app.js 定义 applyWizardSnapshot（对象方法或赋值两种写法都认）",
    /applyWizardSnapshot\s*[(:=]/.test(js));
  check("applyWizardSnapshot 是异步的（框架 await 它）",
    /applyWizardSnapshot\s*=\s*async\s+function/.test(js)
    || /async\s+applyWizardSnapshot\s*\(/.test(js));
  check("app.js 处理二进制帧（WS 文本 vs 二进制分流）",
    /typeof\s+\w+\.data\s*[!=]==?\s*"string"/.test(js));
}

console.log("\n【E】全部带 wizard 的卡仍以 env_check 开头（env 步骤回归）");
{
  const apps = fs.readdirSync(path.join(REPO, "app/apps"))
    .filter((d) => fs.existsSync(path.join(REPO, "app/apps", d, "manifest.json")));
  let n = 0;
  for (const a of apps) {
    const wp = path.join(REPO, "app/apps", a, "static/wizard.json");
    if (!fs.existsSync(wp)) continue;
    const cfg = JSON.parse(fs.readFileSync(wp, "utf8"));
    const manifest = JSON.parse(fs.readFileSync(
      path.join(REPO, "app/apps", a, "manifest.json"), "utf8"));
    if (manifest.wizard_config !== "wizard.json") {
      check(`${a}: manifest.wizard_config 与文件不符`, false, manifest.wizard_config);
      continue;
    }
    check(`${a}: 第 1 步仍是 env_check`,
      cfg.steps[0].kind === "env_check", cfg.steps[0].kind);
    // 能力列表非空的卡**不能**被 env-none 影响（渲染层已由【A】覆盖），
    // 这里保证它们的声明没被清空。
    const caps = manifest.software_capabilities || [];
    check(`${a}: software_capabilities 未被清空（${caps.length} 项）`, caps.length > 0,
      JSON.stringify(caps));
    n++;
  }
  check(`共检查 ${n} 张带 wizard 的卡（audio-analysis 无向导）`, n === 6, String(n));
}

console.log("\n" + "=".repeat(56));
console.log(`通过 ${pass} / ${pass + fail}`);
if (fail) console.log(`失败 ${fail}`);
process.exit(fail ? 1 : 0);