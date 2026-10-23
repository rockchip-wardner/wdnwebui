/**
 * 向导「完成配置」→ 功能自动启动 的回归 —— 纯 Node，**不需要浏览器**。
 *
 * 背景（2026-10-23 实测到的回归）：
 *   卡片原本用 `btn.click()` 启动（模拟用户点「启动检测」）。自 6ea16e9
 *   「模型能力未启用时置灰启动按钮」起，按钮的 `disabled` 由卡片的 **1Hz
 *   快照**（`snapshot.session_ready`）决定 —— 快照比 registry 慢一拍。
 *   模型刚起来的那一秒（向导 env 步刚把它拉起来 → finish 立刻点启动），
 *   按钮还是灰的，而 **disabled 按钮的 `click()` 被浏览器静默丢弃**：
 *   不报错、不打日志、不触发 handler。于是向导显示「完成配置」✓，
 *   功能页上却什么都没跑 ✗（音视频描述 / 视频任意目标定位）。
 *
 * 本脚本验三件事：
 *   (a) 宿主 `core/wizard-start.js`：能力没就绪就**不启动**、等到就绪才启动、
 *       启动没成功要报错（不能静默成功）；
 *   (b) 三张视频卡的 `applyWizardSnapshot` 不再用 `btn.click()` 启动
 *       （那是这次回归的根），而是走共享启动器；
 *   (c) 宿主向导框架认识新错误码（capability_not_ready / start_failed）。
 *
 * 用法: node scripts/verify_wizard_start.mjs
 */
import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "..");
const HELPER = path.join(REPO, "app/static/core/wizard-start.js");
const STATE_JS = path.join(REPO, "app/static/core/wizard/state.js");
const TEMPLATE = path.join(REPO, "app/static/index.html.template");

const PY = process.env.WDNWEBUI_PYTHON || path.join(REPO, ".venv/bin/python");
const CARDS = JSON.parse(execFileSync(
  PY, [path.join(REPO, "scripts/card_dirs.py")], { encoding: "utf8" }));
const cardDir = (appId) => {
  const c = CARDS.find((x) => x.id === appId);
  if (!c) throw new Error(`找不到卡片 ${appId}`);
  return c.dir;
};

let failed = 0;
const ok = (cond, msg) => {
  if (cond) {
    console.log("  ✓ " + msg);
  } else {
    failed++;
    console.log("  ✗ " + msg);
  }
};

// ---------------------------------------------------------------- //
// (a) 宿主共享启动器的行为
// ---------------------------------------------------------------- //
console.log("[a] core/wizard-start.js 行为");
{
  const src = fs.readFileSync(HELPER, "utf8");
  const win = {};
  // 纯浏览器 API：只用到 window / setTimeout / Date
  new Function("window", src)(win);
  const W = win.WizardStart;
  ok(W && typeof W.run === "function", "导出了 window.WizardStart.run");

  // 1) 没就绪 → 不启动；等到就绪 → 才启动
  {
    let polls = 0;
    let started = false;
    let readyAtPoll = -1;
    const snap = () => {
      polls++;
      const ready = polls >= 3;
      if (ready && readyAtPoll < 0) readyAtPoll = polls;
      return { session_ready: ready, enabled: ready };
    };
    const res = await W.run({
      label: "启动检测",
      probe: async () => snap(),
      start: async () => {
        started = true;
        ok(readyAtPoll > 0, "启动发生在「就绪」之后（就绪前不启动）");
        ok(polls >= 3, "就绪前轮询过（没有一上来就放弃）");
      },
      timeoutMs: 5000,
      pollMs: 10,
    });
    ok(started, "能力就绪后调用了 start()");
    ok(res && res.session_ready === true, "返回启动后的快照");
  }

  // 2) 一直不就绪 → 抛 capability_not_ready（可重试），且**绝不**启动
  {
    let started = false;
    let err = null;
    try {
      await W.run({
        label: "启动定位",
        probe: async () => ({ session_ready: false, enabled: false }),
        start: async () => { started = true; },
        timeoutMs: 300,
        pollMs: 10,
      });
    } catch (e) { err = e; }
    ok(!!err, "能力不就绪时抛错（不再静默成功）");
    ok(err && /capability_not_ready/.test(err.message || ""),
      "错误码是 capability_not_ready（向导据此显示「重新开启模型」）");
    ok(err && err.retryable === true, "错误标记为可重试");
    ok(!started, "不就绪时**没有**调用 start()");
  }

  // 3) 启动函数没真跑起来 → 抛 start_failed
  {
    let err = null;
    try {
      await W.run({
        label: "启动检测",
        probe: async () => ({ session_ready: true, enabled: false }),
        start: async () => { /* 卡片自己的启动失败：只写 overlay，不抛 */ },
        timeoutMs: 1000,
        pollMs: 10,
      });
    } catch (e) { err = e; }
    ok(!!err && /start_failed/.test(err.message || ""),
      "启动没成功时报 start_failed（而不是显示「完成」）");
  }

  // 4) 自定义判据（音轨面板用 available / extracting，不走 detector 那套）
  {
    let started = false;
    const res = await W.run({
      label: "分析音频",
      probe: async () => ({ available: true, state: "extracting" }),
      isReady: (s) => !!(s && s.available !== false),
      isStarted: (s) => !!(s && s.state === "extracting"),
      start: async () => { started = true; },
      timeoutMs: 1000,
      pollMs: 10,
    });
    ok(started, "自定义 isReady/isStarted 生效（音轨面板那套口径）");
    ok(res && res.state === "extracting", "返回自定义口径下的快照");
  }
}

// ---------------------------------------------------------------- //
// (b) 三张视频卡不再用 btn.click() 启动
// ---------------------------------------------------------------- //
console.log("[b] 卡片的向导启动路径");

// 去掉注释再查 —— 注释里写着"不能 btn.click()"（说明坑），代码里才是行为。
const stripComments = (s) => s
  .replace(/\/\*[\s\S]*?\*\//g, "")
  .replace(/(^|[^:])\/\/.*$/gm, "$1");

for (const appId of ["video-detection", "video-target-detection",
                     "video-target-localization", "video-retrieval",
                     "tts"]) {
  const js = fs.readFileSync(path.join(cardDir(appId), "static/app.js"), "utf8");
  const fn = js.match(/applyWizardSnapshot\s*=\s*async function[\s\S]*?\n};/);
  ok(!!fn, `${appId}: 找到 applyWizardSnapshot`);
  const body = fn ? stripComments(fn[0]) : "";
  ok(!/\.click\(\)/.test(body),
    `${appId}: 向导启动路径里没有 btn.click()（disabled 按钮的 click 会被静默丢弃）`);
  ok(/_wizardStart\(/.test(body),
    `${appId}: 走宿主共享启动器 window.WizardStart.run()`);
}

// ---------------------------------------------------------------- //
// (d) 语音生成卡片（tts）—— 2026-10-23 实测到的同类回归
// ---------------------------------------------------------------- //
// 症状：第一次跑完「语音生成快速配置」向导，**没有出声**；等模型加载完
// 再跑一遍向导就能合成。原因和视频卡一模一样：tts 卡片用
// `btn.click()` 启动，而启动按钮的 `disabled` 由**卡片快照**的
// `capability_ready` 决定 —— 模型刚就绪时卡片还拿着旧快照（最多滞后
// 一次 5s 轮询），按钮是灰的，**disabled 按钮的 click() 被浏览器静默
// 丢弃** → 向导显示「完成配置」、合成根本没发生。
//
// 这里跑一遍真实代码路径（把卡片里的三个函数抠出来在沙箱里跑），
// 断言：等就绪 → 才启动；不点按钮；不就绪 → 抛错且**不**启动；
// 启动失败 → 抛错（向导不能显示「完成」）。
console.log("[d] 语音生成卡片（tts）的向导启动路径");
{
  const js = fs.readFileSync(path.join(cardDir("tts"), "static/app.js"), "utf8");
  const grab = (re, what) => {
    const m = js.match(re);
    ok(!!m, `tts: 找到 ${what}`);
    return m ? m[0] : "";
  };
  const probeSrc = grab(
    /async function ttsCapabilitySnapshot\(\) \{[\s\S]*?\n\}/, "ttsCapabilitySnapshot");
  const helperSrc = grab(
    /function _wizardStart\(opts\) \{[\s\S]*?\n\}/, "_wizardStart");
  const applySrc = grab(
    /window\.ImApp_tts\.applyWizardSnapshot = async function \(opts\) \{[\s\S]*?\n\};/,
    "applyWizardSnapshot");

  // 沙箱：真实宿主启动器 + 卡片真函数，DOM/fetch/TTS 全是桩。
  const hostWin = {};
  new Function("window", fs.readFileSync(HELPER, "utf8"))(hostWin);

  const runTtsWizard = async ({ readyAfter = 0, startThrows = null } = {}) => {
    let probes = 0;
    const calls = { start: 0, click: 0 };
    let readySeenAt = -1;
    // 模拟后端：合成一旦启动，服务状态从 idle → loading/running。
    // （真实 POST /api/tts/start 返回的快照就是 state="loading"）
    let svcState = "idle";
    const btnStub = { click: () => { calls.click++; } };
    const documentStub = {
      getElementById: (id) => (id === "tts-start" ? btnStub : { value: "" }),
      querySelectorAll: () => [],
    };
    const fetchStub = async () => {
      probes++;
      const ready = probes > readyAfter;
      if (ready && readySeenAt < 0) readySeenAt = probes;
      return {
        ok: true,
        json: async () => ({
          available: true,
          capability_ready: ready,
          capability_loading: ready ? [] : ["qwen3-tts"],
          state: svcState,
          busy: svcState === "loading" || svcState === "running",
        }),
      };
    };
    const ttsStub = {
      start: async () => {
        calls.start++;
        svcState = "running";
        if (startThrows) throw startThrows;
      },
    };
    const win = { WizardStart: hostWin.WizardStart, ImApp_tts: {} };
    const factory = new Function(
      "window", "document", "fetch", "TTS", "API_PREFIX",
      probeSrc + "\n" + helperSrc + "\n" + applySrc);
    factory(win, documentStub, fetchStub, ttsStub, "/api");
    let err = null;
    let res = null;
    try {
      res = await win.ImApp_tts.applyWizardSnapshot({ text: "你好" });
    } catch (e) { err = e; }
    return { calls, err, res, probes, readySeenAt };
  };

  // 1) 能力就绪前**不启动**，就绪后才启动；全程不点按钮
  {
    const r = await runTtsWizard({ readyAfter: 2 });
    ok(!r.err, "tts: 就绪后向导正常完成");
    ok(r.calls.start === 1, "tts: 能力就绪后启动了合成（且只启动一次）");
    ok(r.calls.click === 0,
      "tts: 全程没有 btn.click()（disabled 按钮的 click 会被静默丢弃）");
    ok(r.readySeenAt > 0 && r.probes > 1,
      "tts: 不就绪时先轮询等待，而不是立刻开跑");
    ok(r.res && r.res.started === true, "tts: 返回 started=true");
  }

  // 2) 一直不就绪 → 抛 capability_not_ready，且**绝不**启动
  {
    const r = await runTtsWizard({ readyAfter: Number.MAX_SAFE_INTEGER });
    ok(!!r.err && /capability_not_ready/.test(r.err.message || ""),
      "tts: 能力不就绪时抛 capability_not_ready（向导给「重新开启模型」）");
    ok(r.calls.start === 0, "tts: 不就绪时**没有**启动合成");
    ok(r.calls.click === 0, "tts: 不就绪时也没有点按钮");
  }

  // 3) 启动本身失败 → 错误要冒到向导（不能静默显示「完成配置」）
  {
    const r = await runTtsWizard({
      readyAfter: 0,
      startThrows: new Error("WizardSnapshotError:start_failed:提交失败: 功能未启用"),
    });
    ok(!!r.err && /start_failed/.test(r.err.message || ""),
      "tts: 启动失败时错误冒给向导（不再「静默成功」）");
  }
}

// ---------------------------------------------------------------- //
// (c) 宿主向导框架认识新错误码 + 脚本已注册
// ---------------------------------------------------------------- //
console.log("[c] 宿主向导框架");
{
  const state = fs.readFileSync(STATE_JS, "utf8");
  ok(/capability_not_ready/.test(state), "state.js 认识 capability_not_ready");
  ok(/start_failed/.test(state), "state.js 认识 start_failed");
  const tpl = fs.readFileSync(TEMPLATE, "utf8");
  ok(/core\/wizard-start\.js/.test(tpl), "index.html.template 注册了 core/wizard-start.js");
  ok(fs.existsSync(HELPER), "core/wizard-start.js 存在");
}

console.log(failed ? `\nFAIL: ${failed} 项不通过` : "\nPASS: 全部通过");
process.exit(failed ? 1 : 0);
