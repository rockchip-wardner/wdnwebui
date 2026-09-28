# 音视频描述 (video-detection) 应用向导实现 checklist

> 配套 [《应用向导模式开发指南》](../../docs/WIZARD_DEV_GUIDE.md)。本文件是该应用**专属实施清单** —— 落地一个完整的向导实例所需的所有改动 + 测试 + commit。

---

## 0. 应用概览

| 项 | 值 |
|----|----|
| app id | `video-detection` |
| 应用目录 | `app/apps/video_detection/` |
| 现有静态资源 | `static/app.js` (863 行) + `static/app.css` (1 行) + `static/template.html` (168 行) |
| 主要 DOM ID | `#vd-source` / `#vd-loop` / `#vd-prompt` / `#vd-detect-size` / `#vd-audio-prompt` |
| 启动按钮 | `#vd-open` / `#vd-detector-start` / `#vd-audio-start` |
| 已声明 software_capabilities | `swcap_video_describe` + `swcap_audio_understand` |
| 已声明 hardware_capabilities | `hwcap_video_input` + `hwcap_audio_input` + `hwcap_audio_output` |
| 一键开启模型 | RK182X#0 启 `qwen3.5-vl-4b`(VLLM)/ RK182X#1 启 `qwen2.5-omni-3b`(Omni) |

---

## 1. 文件改动清单

### 1.1 新建 `app/apps/video_detection/wizard.json`(全新)
按开发指南 §3.2 的 schema 填,完整内容如下:
```jsonc
{
  "enabled": true,
  "wizard_title": "音视频描述快速配置",
  "swcap_to_model": {
    "swcap_video_describe":   { "npu_index": 0, "model_id": "qwen3.5-vl-4b" },
    "swcap_audio_understand": { "npu_index": 1, "model_id": "qwen2.5-omni-3b" }
  },
  "open_source_button": "vd-open",
  "start_buttons": ["vd-detector-start", "vd-audio-start"],
  "steps": [
    { "id": "env",    "title": "运行环境检查",                "kind": "env_check" },
    { "id": "source", "title": "视频源",                      "kind": "form",
      "fields": [
        { "id": "vd-source", "label": "视频源 URL / 路径" },
        { "id": "vd-loop",   "label": "循环播放", "type": "checkbox" }
      ]
    },
    { "id": "detect", "title": "检测提示词 + 检测尺寸",       "kind": "form",
      "fields": [
        { "id": "vd-prompt",      "label": "检测提示词", "type": "textarea",
          "placeholder": "例:用中文简洁描述画面,包括场景、人物、动作和环境。" },
        { "id": "vd-detect-size", "label": "检测尺寸",   "type": "select",
          "options": [
            { "value": "auto", "label": "自动选择",     "selected": true },
            { "value": "384",  "label": "384x384 (更快)" },
            { "value": "768",  "label": "768x768 (更准)" }
          ]
        }
      ]
    },
    { "id": "audio",  "title": "音频理解提示词",              "kind": "form",
      "fields": [
        { "id": "vd-audio-prompt", "label": "音频理解提示词", "type": "textarea",
          "placeholder": "例:请转写这段音频里的人在说什么 / 描述音频中的环境音" }
      ]
    },
    { "id": "finish", "title": "完成配置",                    "kind": "finish" }
  ]
}
```

### 1.2 改 `app/apps/video_detection/manifest.json`(1 行新增)
```diff
   "uploads_dir": "uploads/video_detection",
   "display_order": 1
+ "wizard_config": "wizard.json"
 }
```
(也可以插在 `display_order` 之前,顺序不重要;只要存在此字段,wizard 框架就会挂载)

### 1.3 改 `app/apps/video_detection/static/app.js`(末尾追加)
在 IIFE 末尾加(全局名固定 `ImApp_<app.id 去掉 hyphen>`—— wizard 按这个约定查找):
```js
// ---------- 应用向导模式 API ----------
// wizard 框架在 finish 时调;空字段不触发对应启动动作
window.ImApp_video_detection = window.ImApp_video_detection || {};
window.ImApp_video_detection.applyWizardSnapshot = async function (opts) {
  const result = { source_opened: false, detector_started: false, audio_started: false };
  const setVal = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
  const setChk = (id, v) => { const el = document.getElementById(id); if (el) el.checked = !!v; };

  // 1. 写回非空字段到表单
  if (opts.source)      setVal("vd-source", opts.source);
  if (opts.loop != null) setChk("vd-loop", opts.loop);
  if (opts.prompt)      setVal("vd-prompt", opts.prompt);
  if (opts.detectSize)  setVal("vd-detect-size", opts.detectSize);
  if (opts.audioPrompt) setVal("vd-audio-prompt", opts.audioPrompt);

  // 2. 打开视频源(仅 source 非空)
  if (opts.source) {
    try {
      await vdOpen({ source: opts.source, loop: !!opts.loop });
      result.source_opened = true;
    } catch (e) {
      throw new Error("WizardSnapshotError:open_source_failed:" + (e?.message || e));
    }
  }

  // 3. 启动检测(仅 prompt 非空)
  if (opts.prompt) {
    try {
      document.getElementById("vd-detector-start")?.click();
      result.detector_started = true;
    } catch (e) {
      throw new Error("WizardSnapshotError:detector_start_failed:" + (e?.message || e));
    }
  }

  // 4. 启动音频理解(仅 audioPrompt 非空)
  if (opts.audioPrompt) {
    try {
      document.getElementById("vd-audio-start")?.click();
      result.audio_started = true;
    } catch (e) {
      throw new Error("WizardSnapshotError:audio_start_failed:" + (e?.message || e));
    }
  }

  return result;
};
```
**注意**:`vdOpen` 是 `app.js` 已有的函数;如果 `open` 走异步,这里要 await 完毕再往下走。

---

## 2. 框架代码改动

> 框架代码 **只写一次**,后续 7 个应用复用。新增/修改时同样遵守 §1.3 零耦合原则。

### 2.1 新建 `app/core/wizard/state.js`(步骤状态机)
- 暴露 `WizardState` 类 + `mount(app, cfg)` / `unmount()` / `step(id)` / `finish()` / `skip()`
- 内部维护 `currentStepId` / `values: Record<fieldId, value>` / `container: HTMLElement`
- 注入 wizard DOM 到 `<section id="page-<id>">` 内部(应用页面 `<section>` 的兄弟节点,不替换原 page)

### 2.2 新建 `app/core/wizard/registry.js`
```js
window.WizardRegistry = {
  async tryMount(app) {
    try {
      if (!app.manifest?.wizard_config) return;  // 零开销跳过
      const cfg = await fetch(app.manifest.wizard_config).then(r => r.json());
      if (cfg.enabled === false) return;
      await WizardState.mount(app, cfg);
    } catch (e) { console.warn("[wizard] tryMount failed:", e); }
  },
  unmount(app) {
    try { WizardState.unmount(); } catch (e) { console.warn("[wizard] unmount:", e); }
  }
};
```
**关键**: 不维护"已注册 wizard 应用"清单;唯一接口是 `tryMount(app)`。

### 2.3 新建 `app/core/wizard/env_check.py`(后端可选)
```python
# 提供 /api/compute/wizard-bring-up POST endpoint,接收:
#   {required_capabilities: [...], npu_assignments: {swcap_xxx: npu_index}}
# 返回 {stopped_ids, started_ids, errors}
# 内部逻辑:停非必需 + 启必需 + 失败回滚
```
**也可以不新建 endpoint**,前端循环调用现有 stop / start API(开发指南 §5.2 给的方案)。

### 2.4 新建 `app/core/wizard/wizard.css`
- 强制浅色主题
- `.wd-wizard-*` 命名空间
- 1024px 容器 + 移动端响应式
- 步骤指示器 / 表单 / 按钮样式

### 2.5 改 `app/system/static/system.js`
在 `_loadCardJS(app)` 函数体末尾追加:
```js
// 向导模式挂载(零开销跳过无 wizard_config 的应用)
if (window.WizardRegistry?.tryMount) {
  Promise.resolve(window.WizardRegistry.tryMount(app)).catch(e =>
    console.warn("[wizard] tryMount:", e));
}
```
**不要写** `APPS_WITH_WIZARD.includes(appId)` 之类硬编码。

### 2.6 改 `app/static/index.html`(加载 wizard.js / wizard.css)
在 `<head>` 加:
```html
<link rel="stylesheet" href="/static/core/wizard/wizard.css">
```
在 `<body>` 底部加:
```html
<script src="/static/core/wizard/registry.js"></script>
<script src="/static/core/wizard/state.js"></script>
```
**注意路径**:`app/static/core/wizard/` 是 wizard 静态资源的对外暴露路径,对应源文件 `app/core/wizard/static/`(若用 mount 路由)或直接复制。

---

## 3. 测试用例(10 项)

> 用 playwright headless:`--no-sandbox --disable-gpu`,跑 `/home/rockchip/work/wdnwebui/scripts/test_wizard_vd.py`(新建脚本)。

| # | 场景 | 期望 |
|---|------|------|
| **T1** | 首次打开"音视频描述"应用 | 看到向导(env_check 步骤,顶部步骤指示器 ●─○─○─○─○,HW/SW 清单可见) |
| **T2** | HW 不满足(模拟:把板卡 hwcap_video_input 屏蔽) | 步骤顶部红字 "硬件能力不满足",下一步按钮 disabled |
| **T3** | SW 不满足(模拟:停掉所有 model),点"一键开启所需模型" | 非 VLLM / Omni 实例被 stop;`#0` 启 `qwen3.5-vl-4b`,`#1` 启 `qwen2.5-omni-3b`;步骤自动前进 |
| **T4** | 一键开启 2 次连续失败(模拟:hook 启停 API 返回 500) | 弹原生 `confirm("一键开启连续 2 次失败,建议重启板子...")`;取消 → 退出向导;确定 → 保留当前步骤 |
| **T5** | env 通过后,依次填视频源 / 循环 / 检测提示词 / 检测尺寸 / 音频提示词,点下一步直到 finish |
| **T6** | finish 步骤点"完成配置" | 调 `applyWizardSnapshot` → `vd-open` 触发 → `#vd-source` 显示已填值 → 检测 / 音频按钮被自动 click |
| **T7** | 故意填无效视频源(如 `/not/exist.mp4`),点完成配置 | 弹"播放视频失败,回向导修改" → 回 source 步骤 |
| **T8** | finish 触发时模型未启完(模拟:启停后立刻返回 status=loading) | 步骤底部 spinner "等待模型启动中...";setInterval 1000ms 轮询 `/api/compute` 直到 running |
| **T9** | 任意步骤点"跳过向导" | 已填字段写入 `localStorage["wdnwebui.wizard.video-detection.values"]`;应用页可见,wizard DOM 销毁 |
| **T10** | 关浏览器再开同一应用 | localStorage 预填 → step 1 form 字段已显示上次填的值 |

---

## 4. 零耦合验证(每应用实施后必跑)

```bash
# 1. framework 不引用应用
grep -rE 'app\.apps\.[a-z_]+' /home/rockchip/work/wdnwebui/app/core/wizard/ && echo "❌ 违反" || echo "✅"

# 2. system.js 不硬编码应用 ID
grep -E '(appId|app\.id)\s*===\s*["'\''][a-z-]+["'\'']' /home/rockchip/work/wdnwebui/app/system/static/system.js && echo "❌" || echo "✅"

# 3. 删除应用 smoke test
mv /home/rockchip/work/wdnwebui/app/apps/video_detection /tmp/_vd_backup
# 启动 uvicorn,主页 init + 点其他 7 张卡片,全部无 pageerror
python3 scripts/smoke_no_video_detection.py
mv /tmp/_vd_backup /home/rockchip/work/wdnwebui/app/apps/video_detection
```

**期望**: 全部 ✅,其他 7 张应用卡片功能不受影响。

---

## 5. Commit 计划(沿用 wade.weng 风格:中文 + 三段式 + 子点)

> 作者:`wade.weng <wade.weng@rock-chips.com>`,每个 commit 一条主题,不混多个修复。
> 不 push。

```
C1 feat(core/wizard): 框架代码 + 视觉规范 — 新增独立子包
    文件: app/core/wizard/{state.js, registry.js, wizard.css, env_check.py}
    修改: app/static/index.html (加载 wizard.js / wizard.css)
    内容:
      * 根因 — 8 张应用卡片都需要开箱即用的引导配置,但重复实现会导致零耦合被破坏
      * 修 — 新建 app/core/wizard/ 子包,提供声明式 step 状态机 + 通用 tryMount 入口
      * 验证 — 手动跑主页 + 点其他 7 张卡片,全部 init 无 pageerror

C2 feat(system): ROUTER 末尾挂载 wizard,不改应用列表硬编码
    文件: app/system/static/system.js
    修改: _loadCardJS 末尾加 tryMount(app) 调用;零开销跳过无 wizard_config 的应用
    内容:
      * 根因 — wizard 框架已就绪,但 ROUTER 没有调用入口,应用不会自动挂载
      * 修 — _loadCardJS 末尾调 WizardRegistry.tryMount(app);manifest 字段缺失 → 零开销跳过
      * 验证 — 主页进任意应用,wizard 都正确出现;删除 manifest.wizard_config,wizard 自动消失

C3 feat(apps/video-detection): 加 wizard.json + manifest 引用 + applyWizardSnapshot API
    文件: app/apps/video_detection/{wizard.json, manifest.json, static/app.js}
    内容:
      * 根因 — 音视频描述首次打开没有引导,用户面对 4 个配置区不知道从何下手
      * 修 — wizard.json 声明 5 步流程(env/source/detect/audio/finish);
            manifest 加 wizard_config 字段;app.js 暴露 applyWizardSnapshot(空字段不触发对应动作)
      * 验证 — 打开应用看到向导;env 通过后填字段点完成,视频打开 + 检测启动 + 音频启动;

C4 test(apps/video-detection): wizard 10 个 playwright 用例全过
    文件: scripts/test_wizard_vd.py + 测试截图
    内容:
      * 根因 — 向导涉及环境检查/启停模型/模拟点击,纯单元测试覆盖不到
      * 验 — 10 个端到端场景(T1-T10)用 playwright headless 跑,全部通过

C5 docs: 《应用向导模式开发指南.md》+ 音视频描述 checklist
    文件: docs/WIZARD_DEV_GUIDE.md + app/apps/video_detection/WIZARD_CHECKLIST.md
    内容:
      * 根因 — 框架设计已定,需要沉淀文档供后续 7 张卡片向导开发复用
      * 修 — 写跨应用开发指南(零耦合原则 / 框架架构 / 配置文件 schema / API 规范 / 错误处理 / 视觉规范);
            写音视频描述专属 checklist(落地实例)
      * 验证 — review checklist 是否覆盖所有 10 个测试用例;grep 框架代码是否违反零耦合
```

---

## 6. 实施顺序(估时)

| 步骤 | 工作量 | 阻塞 |
|------|--------|------|
| **C1 框架代码** | 4-6 小时 | 无 |
| **C2 system.js 集成** | 30 分钟 | C1 完成 |
| **C3 音视频描述向导** | 1.5-2 小时 | C1 + C2 完成 |
| **C4 测试用例** | 2-3 小时 | C3 完成 |
| **C5 文档** | 1 小时(已在写) | C1-C4 完成 |
| **零耦合验证** | 15 分钟 | C1-C3 完成 |

**总计**: ~10-13 小时 → 拆成 5 个 commit,每个 commit 后跑一次 `零耦合验证` + `smoke test`。

---

## 7. 已知风险 / 边界

- **`applyWizardSnapshot` 调 `vdOpen` 是异步** —— 如果 `vdOpen` 内部有 setTimeout 异步开流,await 不一定能等到 ready;建议先 await 返回值,再点启动检测
- **vwOpen 失败时已经写回了 source 值到 DOM** —— 用户重试时不需要重填;失败回退路径"回向导修改"能让用户改 source
- **localStorage 预填只在 form 步骤前** —— env_check 步骤不预填;finish 步骤不预填
- **模型启动超时** —— 目前用 `/api/compute` 轮询 status=running,默认无超时(由应用页面已有的 loading UI 兜底);如果等待超过 90s,wizard 应弹"启动超时,继续等待 / 退出向导"