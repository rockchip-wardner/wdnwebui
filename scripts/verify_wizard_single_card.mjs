/**
 * 单计算卡降级策略回归 —— 纯 Node + 最小 DOM shim，**不需要 playwright**。
 *
 * 背景：一张 RK1828 协卡放不下两个模型（AGENTS.md v0.17 实测：同卡叠两个
 * 模型 rc=-6）。双卡板上 vd 跑两个模型；单卡板上必须降级：
 *
 *   * 单 capability 的应用 —— 模型照常加载，只是落到唯一那张卡上（NPU0）。
 *   * 多 capability 的应用 —— 让用户二选一（vd）。
 *   * 多 capability 且没声明 single_card_pick —— 刚需多模型，单卡不可用。
 *
 * **双卡路径必须逐字不变** —— 这是本次改动最硬的约束，所以每个用例都
 * 成对断言「双卡 vs 单卡」两套结果。
 *
 * 用法: node scripts/verify_wizard_single_card.mjs
 */
import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "..");
const STATE_JS = path.join(REPO, "app/static/core/wizard/state.js");

// 卡片源码目录 —— 内置卡在 app/apps/，插件卡在 .venv/src/ 的插件包里，
// 不能硬编码 app/apps/<id>/（6 张卡已迁走）。统一问 scripts/card_dirs.py。
const PY = process.env.WDNWEBUI_PYTHON || path.join(REPO, ".venv/bin/python");
const CARDS = JSON.parse(execFileSync(
  PY, [path.join(REPO, "scripts/card_dirs.py")], { encoding: "utf8" }));
const CARD_BY_NAME = new Map(CARDS.map((c) => [c.name, c]));
const CARD_BY_ID = new Map(CARDS.map((c) => [c.id, c]));
function cardDir(appId) {
  const c = CARD_BY_NAME.get(appId) || CARD_BY_ID.get(appId);
  if (!c) throw new Error(`找不到卡片 ${appId}（已发现：${CARDS.map((x) => x.id)}）`);
  return c.dir;
}

// ---------------------------------------------------------------- //
// 最小 DOM shim
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
  /** 只支持 "#id" 选择器 —— state.js 的 _q() 只用它取容器内的几个固定节点 */
  querySelector(sel) {
    if (typeof sel === "string" && sel.startsWith("#")) {
      const want = sel.slice(1);
      let found = null;
      const walk = (n) => {
        if (found) return;
        if (n.id === want) { found = n; return; }
        n.children.forEach(walk);
      };
      walk(this);
      return found;
    }
    return null;
  }
  querySelectorAll() { return []; }
  closest() { return null; }
  click() {}
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
  findById(id) {
    let found = null;
    const walk = (n) => {
      if (found) return;
      if (n.id === id) { found = n; return; }
      n.children.forEach(walk);
    };
    walk(this);
    return found;
  }
  /** 深度优先收集文本 */
  text() {
    const out = [];
    const walk = (n) => {
      if (n._text) out.push(n._text);
      n.children.forEach(walk);
    };
    walk(this);
    return out.join("\n");
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
  // state.js 的 _q(sel) 默认在 document 上查（_captureValues 取 body）。
  // shim 没有真 DOM，用 __root 指向当前测试容器来顶替。
  querySelector: (sel) => (document.__root ? document.__root.querySelector(sel) : null),
  querySelectorAll: () => [],
  addEventListener: () => {},
  removeEventListener: () => {},
  body: new El("body"),
  hidden: false,
  __root: null,
};
const localStorage = {
  _m: new Map(),
  getItem(k) { return this._m.has(k) ? this._m.get(k) : null; },
  setItem(k, v) { this._m.set(k, String(v)); },
  removeItem(k) { this._m.delete(k); },
};

// ---------------------------------------------------------------- //
// 载入 state.js（私有函数改写源码暴露出来）
// ---------------------------------------------------------------- //
let src = fs.readFileSync(STATE_JS, "utf8");
// 注入锚点用 ``window.WizardState = {`` 的开头 —— 导出对象的成员列表会随
// 功能增删变，按整句匹配会在形状变化后静默失配（详见
// verify_wizard_env_step.mjs 里的同一段说明）。
// 额外暴露 _planSingleCard / _setSingleCardChoice / _setActive —— 单卡
// 策略的三个入口，测试要直接驱动它们。
src = src.replace(
  /window\.WizardState = \{/,
  `window.__wiz_priv = {
     _renderEnvStep, _probeEnv, _envNoReqRow, _fieldKey,
     _planSingleCard, _setSingleCardChoice,
     _lsSaveStep, _lsLoadStep, _resolveStepIdx, _finish,
     _setActive: (a) => { _active = a; },
   };
   window.WizardState = {`
);

// ---------------------------------------------------------------- //
// fetch mock —— 记录 POST /api/compute/instances 的 body
// ---------------------------------------------------------------- //
let computeFixture = { card_count: 2, model_cards: [0, 1], instances: [] };
let posts = [];
let deletes = [];

globalThis.fetch = async (url, opts = {}) => {
  const u = String(url);
  const method = (opts.method || "GET").toUpperCase();
  if (method === "POST" && u.includes("/api/compute/instances")) {
    posts.push(JSON.parse(opts.body));
    return { ok: true, json: async () => ({}), text: async () => "" };
  }
  if (method === "DELETE" && u.includes("/api/compute/instances")) {
    deletes.push(JSON.parse(opts.body));
    return { ok: true, json: async () => ({}), text: async () => "" };
  }
  if (u.includes("/api/compute")) {
    return { ok: true, json: async () => computeFixture, text: async () => "" };
  }
  return { ok: true, json: async () => ({}), text: async () => "" };
};
globalThis.window = globalThis;
globalThis.document = document;
globalThis.localStorage = localStorage;
globalThis.CSS = { escape: (s) => s };
globalThis.Blob = class { constructor() {} };
globalThis.FormData = class { append() {} };
globalThis.confirm = () => true;
globalThis.alert = () => {};
globalThis.setTimeout = (fn) => fn();
globalThis.clearTimeout = () => {};

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
const DEFAULT_STEPS = [{ id: "env", kind: "env_check" }, { id: "finish", kind: "finish" }];

// 真实应用的 wizard.json 声明（不 mock —— 声明本身就是要守的契约）
function loadWizard(appId) {
  const p = path.join(cardDir(appId), "static/wizard.json");
  return JSON.parse(fs.readFileSync(p, "utf8"));
}
function loadManifest(appId) {
  const p = path.join(cardDir(appId), "manifest.json");
  return JSON.parse(fs.readFileSync(p, "utf8"));
}

/** 构造一个能跑 _renderEnvStep / _gotoStep 的容器 */
function makeContainer() {
  const container = new El("section");
  container.id = NS + "root-test";
  const stepper = new El("div"); stepper.id = NS + "stepper";
  const body = new El("div"); body.id = NS + "body";
  const foot = new El("div"); foot.id = NS + "foot";
  container.appendChild(stepper);
  container.appendChild(body);
  container.appendChild(foot);
  return { container, body, foot };
}

/**
 * 造「本机正在跑该卡所需模型」的实例列表。
 *
 * env 步的就绪判据是 **(模型, 卡位) 对**级（vtd 要 qwen3-vl-4b 同时占
 * npu0 + npu1）—— 快照里没有对应实例时 env 步会正确地判成「未就绪」
 * （下一步禁用 + 出现「一键开启所需模型」按钮）。所以「下一步不被
 * 禁用」这条断言的前提是**模型确实在跑**。
 */
function runningInstancesFor(cfg) {
  const out = [];
  for (const [cap, m] of Object.entries(cfg.swcap_to_model || {})) {
    if (!m || !m.model_id) continue;
    const npus = (m.npu_index == null) ? []
      : (Array.isArray(m.npu_index) ? m.npu_index : [m.npu_index]);
    for (const n of npus) {
      if (n == null) continue;
      out.push({
        model_id: m.model_id, npu_index: n, status: "running",
        _source: "spawned", capability_types: [cap], transition: "",
      });
    }
  }
  return out;
}

/**
 * 跑一遍「挂载 → 渲染环境检查页」。
 * @param appId  应用 id（读它的 wizard.json + manifest.json）
 * @param cardCount  后端报告的模型卡位数
 * @param modelCards 后端报告的模型卡位
 */
async function mountEnv(appId, cardCount, modelCards) {
  const cfg = loadWizard(appId);
  const manifest = loadManifest(appId);
  // app stub 要带 manifest 的能力声明 —— _probeEnv 的 capability 级判据
  // 读的就是它。早年的 stub 没带，capability 判据在测试里是**空转**的，
  // 掩盖了「模型没在本机跑 → env 步判未就绪」这条真实语义。
  const app = {
    id: appId, title: manifest.title || appId,
    software_capabilities: manifest.software_capabilities || [],
    hardware_capabilities: manifest.hardware_capabilities || [],
  };
  computeFixture = {
    card_count: cardCount,
    model_cards: modelCards,
    instances: [],
  };
  let envCheck = await priv._probeEnv(cfg, app);
  const single = priv._planSingleCard(cfg, app, envCheck);
  // 实例按**裁剪后**的 cfg 造 —— 单卡下卡号已被换成唯一卡位，
  // 按原始 cfg 造会落在一张不存在的卡上，卡位级判据会判成未就绪。
  const effCfg = (single && single.effCfg) ? single.effCfg : cfg;
  computeFixture = {
    card_count: cardCount,
    model_cards: modelCards,
    instances: runningInstancesFor(effCfg),
  };
  envCheck = await priv._probeEnv(cfg, app);
  const { container, body, foot } = makeContainer();
  const state = {
    app, cfg: single && single.effCfg ? single.effCfg : cfg,
    container, currentStep: 0, values: {}, envCheck, single,
  };
  priv._setActive(state);
  // 走 _gotoStep 的等价路径：直接渲染 env 步
  priv._renderEnvStep(body, foot, state.cfg, cfg.steps[0], state);
  return { cfg, manifest, app, envCheck, single, state, body, foot, container };
}

function nextBtnDisabled(foot) {
  const btn = foot.findByTag("button").find((b) => b.textContent === "下一步");
  return btn ? !!btn.disabled : null;
}
function bringBtn(body) {
  return body.findById(NS + "env-bring-btn");
}
function noticeBlocks(body) {
  return body.findByClass(NS + "single-notice");
}
function noticeText(body) {
  return noticeBlocks(body).map((n) => n.text()).join("\n");
}
function radios(body) {
  return body.findByClass(NS + "single-pick-item")
    .map((l) => l.findByTag("input")[0])
    .filter(Boolean);
}

// ================================================================== //
console.log("【A】双卡路径 —— 必须与改动前逐字一致（无单卡提示、卡号不变）");
{
  for (const appId of ["text_chat", "tts", "video_target_detection",
                       "video_target_localization", "video_detection",
                       "video_retrieval"]) {
    const r = await mountEnv(appId, 2, [0, 1]);
    check(`${appId}: 无单卡提示块`, noticeBlocks(r.body).length === 0,
      `count=${noticeBlocks(r.body).length}`);
    check(`${appId}: single 策略不生效（走原路径）`, r.single === null);
    check(`${appId}: swcap_to_model 未被裁剪`,
      Object.keys(r.state.cfg.swcap_to_model).length
      === Object.keys(r.cfg.swcap_to_model).length);
  }
}

console.log("\n【B】单卡 —— 一键开启的模型必须落到唯一卡位 NPU0");
{
  const r = await mountEnv("text_chat", 1, [0]);
  check("text_chat: 单 capability 应用**不显示**任何单卡提示块",
    noticeBlocks(r.body).length === 0,
    `count=${noticeBlocks(r.body).length}`);
  check("text_chat: single 策略生效（cfg 仍被裁成单卡）",
    r.single !== null && r.single.active === true);
  check("text_chat: swcap_to_model 被裁成单卡（卡号=0）",
    JSON.stringify(r.state.cfg.swcap_to_model["swcap_text_chat"].npu_index) === "0",
    JSON.stringify(r.state.cfg.swcap_to_model));

  posts = []; deletes = [];
  await globalThis.window._wizardBringUpModels(r.state.cfg);
  check("text_chat: 只 POST 一个模型",
    posts.length === 1 && posts[0].model === "qwen3.5-vl-4b",
    JSON.stringify(posts));
  check("text_chat: npu_index = 0", posts[0] && posts[0].npu_index === 0,
    JSON.stringify(posts));
}

console.log("\n【C】单卡 —— vtd 原本声明 npu [0,1]，应收敛到单卡位");
{
  const r = await mountEnv("video_target_detection", 1, [0]);
  const m = r.state.cfg.swcap_to_model["swcap_vision_chat"];
  check("vtd: npu_index 从 [0,1] 收敛为 0", JSON.stringify(m.npu_index) === "0",
    JSON.stringify(m));
  check("vtd: 模型仍是 qwen3-vl-4b", m.model_id === "qwen3-vl-4b", m.model_id);
  posts = [];
  await globalThis.window._wizardBringUpModels(r.state.cfg);
  check("vtd: POST 只有一个实例且 npu=0",
    posts.length === 1 && posts[0].npu_index === 0, JSON.stringify(posts));
}

console.log("\n【D】单卡 —— vd 多 capability 二选一");
{
  const r = await mountEnv("video_detection", 1, [0]);
  check("vd: 出现醒目提示块", noticeBlocks(r.body).length === 1);
  const txt = noticeText(r.body);
  check("vd: 提示写明完整功能需要 2 张计算卡",
    txt.includes("2 张计算卡"), txt.replace(/\n/g, " | "));
  check("vd: 提供二选一（2 个 radio）", radios(r.body).length === 2,
    `count=${radios(r.body).length}`);
  check("vd: 默认选中视频描述",
    radios(r.body)[0] && radios(r.body)[0].checked === true
    && radios(r.body)[0].value === "swcap_vision_chat",
    JSON.stringify(radios(r.body).map((x) => [x.value, x.checked])));
  check("vd: 默认只加载视频描述对应的模型",
    Object.keys(r.state.cfg.swcap_to_model).join() === "swcap_vision_chat",
    Object.keys(r.state.cfg.swcap_to_model).join());
  check("vd: 未选中的 capability 以中性行展示",
    r.body.text().includes("单卡下未启用"),
    r.body.text().split("\n").filter((l) => l.includes("未启用")).join(" | "));

  // 默认选择 → 一键开启只起 VLM
  posts = [];
  await globalThis.window._wizardBringUpModels(r.state.cfg);
  check("vd: 默认只 POST qwen3-vl-4b @ npu0",
    posts.length === 1 && posts[0].model === "qwen3-vl-4b"
    && posts[0].npu_index === 0, JSON.stringify(posts));

  // 切到「音频描述」→ 只起 omni
  const audioRadio = radios(r.body).find((x) => x.value === "swcap_audio_chat");
  check("vd: 音频描述 radio 存在且未选中", !!audioRadio && audioRadio.checked === false);
  audioRadio.checked = true;
  for (const fn of (audioRadio.listeners.change || [])) fn();
  check("vd: 切换后 swcap_to_model 只剩音频描述",
    Object.keys(r.state.cfg.swcap_to_model).join() === "swcap_audio_chat",
    Object.keys(r.state.cfg.swcap_to_model).join());
  check("vd: 切换后卡号仍为 0",
    r.state.cfg.swcap_to_model["swcap_audio_chat"].npu_index === 0);
  posts = [];
  await globalThis.window._wizardBringUpModels(r.state.cfg);
  check("vd: 切到音频后只 POST qwen2.5-omni-3b @ npu0",
    posts.length === 1 && posts[0].model === "qwen2.5-omni-3b"
    && posts[0].npu_index === 0, JSON.stringify(posts));
}

console.log("\n【E】单卡 —— 单 capability 的检索卡（监控反查）不提示、不阻断");
{
  // 监控反查（video_retrieval）只有 1 个 capability（swcap_vision_embedding）
  // —— 单卡下它跟普通单 capability 应用一样：模型落到唯一卡位，功能不变。
  // 早期版本的「监控分析检索」是刚需两个模型（分析+检索）才会被单卡阻断，
  // 卡片改名后那个前提已不存在。
  const r = await mountEnv("video_retrieval", 1, [0]);
  check("vr: 单 capability 应用**不显示**任何单卡提示块",
    noticeBlocks(r.body).length === 0,
    `count=${noticeBlocks(r.body).length}`);
  check("vr: 单卡策略生效（模型落到唯一卡位）",
    r.single !== null && r.single.active === true
    && r.single.blocked === false);
  check("vr: swcap_to_model 被裁成单卡（卡号=0）",
    r.state.cfg.swcap_to_model["swcap_vision_embedding"].npu_index === 0,
    JSON.stringify(r.state.cfg.swcap_to_model));
  check("vr: 「下一步」不被禁用", nextBtnDisabled(r.foot) !== true,
    String(nextBtnDisabled(r.foot)));
  posts = [];
  await globalThis.window._wizardBringUpModels(r.state.cfg);
  check("vr: 一键开启只 POST 检索模型 @ npu0",
    posts.length === 1 && posts[0].model === "qwen3-vl-embedding-2b"
    && posts[0].npu_index === 0, JSON.stringify(posts));
}

console.log("\n【F】探测失败 / 后端没给 card_count —— 不得误判成单卡");
{
  computeFixture = { instances: [] };   // 老后端：没有 card_count 字段
  const cfg = loadWizard("video_detection");
  const manifest = loadManifest("video_detection");
  const app = { id: "video_detection", title: manifest.title };
  const envCheck = await priv._probeEnv(cfg, app);
  check("card_count 缺失时视为 0", envCheck.cardCount === 0, String(envCheck.cardCount));
  check("card_count 缺失时不触发单卡分支",
    priv._planSingleCard(cfg, app, envCheck) === null);
}

console.log("\n【G】单卡二选一 —— 裁掉不适用的步骤");
{
  const stepIds = (cfg) => (cfg.steps || []).map((s) => s.id);

  // G1 双卡：步骤表逐字不变（5 步，含 audio）
  const dual = await mountEnv("video_detection", 2, [0, 1]);
  check("vd 双卡: 步骤表逐字不变（env/source/detect/audio/finish）",
    stepIds(dual.state.cfg).join() === "env,source,detect,audio,finish",
    stepIds(dual.state.cfg).join());

  // G2 单卡默认（视频描述）→ 没有「音频理解提示词」步
  const r = await mountEnv("video_detection", 1, [0]);
  check("vd 单卡默认（视频描述）: 步骤表 = env/source/detect/finish",
    stepIds(r.state.cfg).join() === "env,source,detect,finish",
    stepIds(r.state.cfg).join());
  check("vd 单卡默认: 确实裁掉了 audio 步",
    !stepIds(r.state.cfg).includes("audio"), stepIds(r.state.cfg).join());

  // G3 切到「音频描述」→ 没有「检测提示词 + 检测尺寸」步
  const audioRadio = radios(r.body).find((x) => x.value === "swcap_audio_chat");
  audioRadio.checked = true;
  for (const fn of (audioRadio.listeners.change || [])) fn();
  check("vd 单卡选音频描述: 步骤表 = env/source/audio/finish",
    stepIds(r.state.cfg).join() === "env,source,audio,finish",
    stepIds(r.state.cfg).join());
  check("vd 单卡选音频描述: 确实裁掉了 detect 步",
    !stepIds(r.state.cfg).includes("detect"), stepIds(r.state.cfg).join());

  // G4 其他卡的步骤表不受影响（单卡只换卡号，不裁步骤）
  const tc = await mountEnv("text_chat", 1, [0]);
  check("text_chat 单卡: 步骤表不变（env/prompt/finish）",
    stepIds(tc.state.cfg).join() === "env,prompt,finish",
    stepIds(tc.state.cfg).join());
  const vtd = await mountEnv("video_target_detection", 1, [0]);
  check("vtd 单卡: 步骤表不变（env/source/detect/finish）",
    stepIds(vtd.state.cfg).join() === "env,source,detect,finish",
    stepIds(vtd.state.cfg).join());
}

console.log("\n【H】finish 构造的 opts —— 被裁掉的字段不得出现");
{
  // 造一个「没有 finish-error 节点」的容器：_waitForModelsRunning 找不到
  // statusBox 会直接返回（不轮询 /api/compute，避免测试里死等 90s）。
  async function runFinish(cardCount, modelCards, choice) {
    computeFixture = { card_count: cardCount, model_cards: modelCards, instances: [] };
    const cfg = loadWizard("video_detection");
    const manifest = loadManifest("video_detection");
    const app = { id: "video_detection", title: manifest.title };
    const envCheck = await priv._probeEnv(cfg, app);
    const single = priv._planSingleCard(cfg, app, envCheck);
    if (choice) priv._setSingleCardChoice(single, choice);
    const { container } = makeContainer();
    const state = {
      app, cfg: single && single.effCfg ? single.effCfg : cfg,
      container, currentStep: 0,
      intervals: [],
      // 两个提示词都预置 —— 模拟 localStorage 里还留着上一轮双卡填的值。
      // 被裁掉的步骤的字段**不得**因此漏进 opts。
      values: {
        "vd-sources": "/tmp/a.mp4", "vd-loop": true,
        "vd-prompt": "描述画面", "vd-audio-prompt": "转写音频",
      },
      envCheck, single,
    };
    priv._setActive(state);
    let captured = null;
    window.ImApp_video_detection = {
      applyWizardSnapshot: async (opts) => { captured = opts; return {}; },
    };
    document.__root = container;   // 让 _captureValues 找得到 body
    await priv._finish(state);
    document.__root = null;
    return captured;
  }

  const dualOpts = await runFinish(2, [0, 1], null);
  check("双卡: opts 同时带 prompt 与 audioPrompt",
    dualOpts && dualOpts.prompt === "描述画面" && dualOpts.audioPrompt === "转写音频",
    JSON.stringify(dualOpts));

  const videoOpts = await runFinish(1, [0], null);
  check("单卡选视频描述: opts 带 prompt",
    videoOpts && videoOpts.prompt === "描述画面", JSON.stringify(videoOpts));
  check("单卡选视频描述: opts **不带** audioPrompt（不再自动分析音频）",
    videoOpts && videoOpts.audioPrompt === undefined, JSON.stringify(videoOpts));
  check("单卡选视频描述: opts 不带 detectSize（随 detect 步一起裁掉）",
    videoOpts && videoOpts.detectSize === undefined, JSON.stringify(videoOpts));

  const audioOpts = await runFinish(1, [0], "swcap_audio_chat");
  check("单卡选音频描述: opts 带 audioPrompt",
    audioOpts && audioOpts.audioPrompt === "转写音频", JSON.stringify(audioOpts));
  check("单卡选音频描述: opts **不带** prompt（不启动画面检测）",
    audioOpts && audioOpts.prompt === undefined, JSON.stringify(audioOpts));
  check("单卡选音频描述: opts 不带 detectSize",
    audioOpts && audioOpts.detectSize === undefined, JSON.stringify(audioOpts));
}

console.log("\n【I】步骤恢复 —— 存步骤 id（单卡裁剪后下标会错位）");
{
  const cfg = loadWizard("video_detection");
  const effSteps = [
    { id: "env" }, { id: "source" }, { id: "detect" }, { id: "finish" },
  ];
  localStorage.removeItem("wdnwebui.wizard.video-detection.step");
  priv._lsSaveStep("video-detection", cfg, "detect");
  check("存的是步骤 id 而不是下标",
    localStorage.getItem("wdnwebui.wizard.video-detection.step") === "detect",
    localStorage.getItem("wdnwebui.wizard.video-detection.step"));
  check("按 id 解析回下标（裁剪后的表）",
    priv._resolveStepIdx(effSteps, priv._lsLoadStep("video-detection", cfg)) === 2,
    String(priv._resolveStepIdx(effSteps, priv._lsLoadStep("video-detection", cfg))));
  check("老版本存的下标仍能兜底解析",
    priv._resolveStepIdx(effSteps, "1") === 1, String(priv._resolveStepIdx(effSteps, "1")));
  check("存的步骤已被裁掉 → 回第 1 步",
    priv._resolveStepIdx(effSteps, "audio") === 0,
    String(priv._resolveStepIdx(effSteps, "audio")));
  check("越界的旧下标 → 回第 1 步",
    priv._resolveStepIdx(effSteps, "9") === 0,
    String(priv._resolveStepIdx(effSteps, "9")));
}

console.log("\n" + "=".repeat(56));
console.log(`通过 ${pass} / ${pass + fail}`);
if (fail) console.log(`失败 ${fail}`);
process.exit(fail ? 1 : 0);
