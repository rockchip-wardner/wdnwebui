# 应用向导模式开发指南

> 适用于 wdnwebui v0.4+ 插件化架构下,**为任意应用添加开箱即用的引导式配置流程**。
> 与 AGENTS.md 同级,**先读本文档,再按需读音视频描述的 [WIZARD_CHECKLIST](../app/apps/video_detection/WIZARD_CHECKLIST.md) 拿到一份落地实例。**

---

## 1. 概述

### 1.1 目标
让用户**首次打开任意应用就进入引导流程**,按 1-2 分钟节奏完成该应用所需的环境检查 + 必填参数 + 后台启动动作,然后回到原应用布局开始使用。同一框架适用于 8 张应用卡片,**新增应用向导的工作量应控制在 1-2 小时内**(只写 `wizard.json` + 暴露 `applyWizardSnapshot`)。

### 1.2 设计原则
1. **声明式配置**:向导的步骤 / 字段 / 启停映射 / 触发按钮全部写在 `wizard.json` 声明,框架零业务代码
2. **状态机驱动**:5 步生命周期(`mount → step 切换 → finish/skip → unmount`)由 `state.js` 串行处理,UI 是状态的纯函数
3. **错误降级**:环境检查失败、完成配置失败、模型未启完等失败点都有显式兜底,**向导崩溃不影响原应用 init**
4. **完成触发**:向导完成 ≠ 显示一个绿色对勾,而是**主动调用应用页面的 `applyWizardSnapshot()`** 让应用开始工作

### 1.3 零耦合原则(强约束,**违反任一条即不合格**)
> **每个应用要继续保持零耦合,删掉某个应用,不影响其他的应用。**

具体落实:
- framework(`app/core/wizard/`)严禁 `import app.apps.<name>`;app 也严禁 import 其他 app
- framework 不维护"已注册 wizard 应用"清单 —— 唯一入口是 `WizardRegistry.tryMount(app)`
- system.js / app.js 不硬编码应用 ID 列表 —— 所有应用判断都走 manifest 字段
- manifest 缺失 `wizard_config` 字段 → wizard **零开销静默跳过**,不抛错,不污染日志
- wizard 内部行为与任何具体应用 / capability / model_id 解耦 —— capability → 模型映射写在 `wizard.json` 而非 framework
- wizard CSS 全部用 `.wd-wizard-*` 命名空间前缀,不与任何应用 css 类名重叠
- 手动零耦合验证(每次新增应用后必跑):
  ```bash
  rm -rf app/apps/<new_app>/
  # 然后跑 smoke test:进主页 / 点其他应用卡片 / 全部仍能 init,无 pageerror
  ```

---

## 2. 框架架构

### 2.1 目录结构: `app/core/wizard/`(独立子包)
```
app/core/wizard/
├── state.js          # 步骤状态机 (mount/step/finish/skip/unmount)
├── registry.js       # WizardRegistry(只暴露 tryMount / unmount 两个 API)
├── env_check.py      # 运行环境检查 + 一键开启 helper(后端部分)
├── wizard.css        # 视觉规范 + .wd-wizard-* 命名空间
└── README.md         # 内部实现说明(可选)
```

**约束**: 此目录下任何 `.py` / `.js` **不允许**出现 `import app.apps.*` / `require("app.apps.*")`。

### 2.2 Wizard 生命周期
```
用户在 sidebar 点应用卡片
    │
    ▼
system.js ROUTER._loadCardJS(app) 加载应用静态资源 + 调应用 register_api()
    │
    ▼
ROUTER._loadCardJS 末尾 → WizardRegistry.tryMount(app)
    │
    ├─ app.manifest.wizard_config 缺失 ───► return(no-op,零开销)
    │
    ▼ 存在
fetch(app.manifest.wizard_config) → wizard.json
    │
    ▼
state.mount(app, wizardCfg) → 注入 wizard DOM 到 <section id="page-<id>">
    │
    ├─ 用户点"下一步" → state.step(id)
    ├─ 用户点"跳过向导" → state.skip() → unmount → 应用 init
    └─ 用户点"完成配置" → state.finish() → applyWizardSnapshot → unmount → 应用 init
    │
    ▼
unmount → 销毁 wizard DOM,触发应用 init / 让用户操作
```

### 2.3 与 system.js ROUTER 集成
在 `app/system/static/system.js` 的 `_loadCardJS(app)` 末尾追加:
```js
// 不写 APPS_WITH_WIZARD 列表!直接调通用入口
if (window.WizardRegistry?.tryMount) {
  try { window.WizardRegistry.tryMount(app); }
  catch (e) { console.warn("[wizard] tryMount failed, skip:", e); }
}
```
- `try/catch` 兜底:**wizard 框架异常不能阻塞应用 init**
- `WizardRegistry` 是 window 全局,wizard.css / wizard.js 通过 `<script>` 注入(放在 `app/static/index.html` 头部一次加载)

### 2.4 零耦合校验清单(每应用实施后手动跑)
```bash
# 1. 框架不引用应用
grep -rE 'app\.apps\.[a-z_]+' app/core/wizard/ && echo "❌ 违反零耦合" || echo "✅"

# 2. system.js 不硬编码应用 ID
grep -E '(appId|app\.id)\s*===\s*["'\''][a-z-]+["'\'']' app/system/static/system.js && echo "❌" || echo "✅"

# 3. 删除应用 smoke test(选一种)
rm -rf app/apps/<app>/  # 删前先备份
# 跑主页 + 其他 7 个应用,全部 init 无 pageerror
```

---

## 3. 配置文件

### 3.1 manifest.json:加 `wizard_config` 字段
```jsonc
{
  "id": "video-detection",
  "title": "音视频描述",
  // ... 原有字段不动 ...
  "wizard_config": "wizard.json"     // 新增:指向同目录下的 wizard.json
}
```
- **没有 `wizard_config` 字段的应用 → wizard 框架零开销跳过**(不加载 / 不渲染 / 不报错)
- 删除应用时 `manifest.json` 一起被删 → wizard 自动跳过该应用

### 3.2 wizard.json schema(完整字段表)
```jsonc
{
  "enabled": true,                              // 冗余但保留(可临时关闭向导而不删 wizard.json)
  "wizard_title": "音视频描述快速配置",          // 可选,默认 = manifest.title
  "localStorage_key": "wdnwebui.wizard.video-detection.values",  // 可选,默认按此规则生成
  "swcap_to_model": {                            // 应用层声明:一键开启所需模型映射
    "swcap_vision_chat":   { "npu_index": 0, "model_id": "vllm-q35-4b" },
    "swcap_audio_chat": { "npu_index": 1, "model_id": "omni-q25-3b" }
  },
  "open_source_button": "vd-open",               // 完成配置时 applyWizardSnapshot 内部用于打开视频源的按钮 ID
  "start_buttons": [                             // 完成配置时按顺序模拟启动的按钮 ID 列表
    "vd-detector-start",
    "vd-audio-start"
  ],
  "steps": [
    {
      "id": "env",                                // 步骤 ID,全局唯一
      "title": "运行环境检查",
      "kind": "env_check"                         // 内置步骤类型
    },
    {
      "id": "source",
      "title": "视频源",
      "kind": "form",
      "fields": [
        { "id": "vd-source", "label": "视频源 URL / 路径" },
        { "id": "vd-loop",   "label": "循环播放", "type": "checkbox" }
      ]
    },
    {
      "id": "detect",
      "title": "检测提示词 + 检测尺寸",
      "kind": "form",
      "fields": [
        { "id": "vd-prompt",      "label": "检测提示词", "type": "textarea" },
        { "id": "vd-detect-size", "label": "检测尺寸",   "type": "select",
          "options": [
            { "value": "auto", "label": "自动选择", "selected": true },
            { "value": "384",  "label": "384x384 (更快)" },
            { "value": "768",  "label": "768x768 (更准)" }
          ]
        }
      ]
    },
    {
      "id": "audio",
      "title": "音频理解提示词",
      "kind": "form",
      "fields": [
        { "id": "vd-audio-prompt", "label": "音频理解提示词", "type": "textarea" }
      ]
    },
    {
      "id": "finish",
      "title": "完成配置",
      "kind": "finish"
    }
  ]
}
```

**字段类型** (`field.type`):
| type | HTML 元素 | 值类型 |
|------|-----------|--------|
| `text`(默认) | `<input type="text">` | string |
| `number` | `<input type="number">` | number |
| `textarea` | `<textarea>` | string |
| `checkbox` | `<input type="checkbox">` | boolean |
| `select` | `<select>` | string(取 option value) |

### 3.3 字段契约对齐(关键!)

> `wizard.json` 的 `steps[].fields[].id` **必须严格对应** `applyWizardSnapshot(opts)` 接受的字段名。

示例:音视频描述的 `wizard.json` 里有 `vd-source` / `vd-prompt` / `vd-audio-prompt` 三个字段 → `applyWizardSnapshot` 必须接收 `source` / `prompt` / `audioPrompt` 这三个字段(或同等命名)。wizard 框架在 `finish` 时把 wizard 收集的 `{id: value}` 映射成 `{optsKey: value}`,**映射规则**是字段 id 去掉 `vd-` 前缀 + camelCase(具体规则见 §5.1)。

---

## 5. API 规范

### 5.1 应用层 `applyWizardSnapshot(opts)`(必须暴露)

每个应用在自己的 `static/app.js` 里:
```js
window.ImApp_video_detection = window.ImApp_video_detection || {};
Object.assign(window.ImApp_video_detection, {
  /**
   * wizard 完成时由框架调用。
   * @param {Object} opts - 来自 wizard 收集的字段值(已是 camelCase)
   * @param {string} [opts.source]       - 视频源 URL / 路径(可能为空 → 跳过打开)
   * @param {boolean} [opts.loop]        - 是否循环播放
   * @param {string} [opts.prompt]       - 检测提示词(可能为空 → 跳过启动检测)
   * @param {string} [opts.detectSize]   - 检测尺寸("auto" / "384" / "768")
   * @param {string} [opts.audioPrompt]  - 音频理解提示词(可能为空 → 跳过启动音频)
   * @returns {Promise<{source_opened:boolean, detector_started:boolean, audio_started:boolean}>}
   */
  async applyWizardSnapshot(opts) {
    // 1. 把非空字段写回表单 DOM
    if (opts.source)      setElValue("vd-source", opts.source);
    if (opts.loop != null) document.getElementById("vd-loop").checked = !!opts.loop;
    if (opts.prompt)      setElValue("vd-prompt", opts.prompt);
    if (opts.detectSize)  setElValue("vd-detect-size", opts.detectSize);
    if (opts.audioPrompt) setElValue("vd-audio-prompt", opts.audioPrompt);

    const result = { source_opened: false, detector_started: false, audio_started: false };

    // 2. 打开视频源(仅 source 非空)
    if (opts.source) {
      try {
        await vdOpen({ source: opts.source, loop: !!opts.loop });
        result.source_opened = true;
      } catch (e) {
        throw new WizardSnapshotError("open_source_failed", e);
      }
    }

    // 3. 启动检测(仅 prompt 非空)
    if (opts.prompt) {
      try { clickAndAwait("vd-detector-start"); result.detector_started = true; }
      catch (e) { throw new WizardSnapshotError("detector_start_failed", e); }
    }

    // 4. 启动音频理解(仅 audioPrompt 非空)
    if (opts.audioPrompt) {
      try { clickAndAwait("vd-audio-start"); result.audio_started = true; }
      catch (e) { throw new WizardSnapshotError("audio_start_failed", e); }
    }

    return result;
  }
});
```

**字段映射规则**(wizard 框架内):
| wizard.json field id | applyWizardSnapshot opts key |
|---------------------|------------------------------|
| `vd-source` | `source` |
| `vd-loop` | `loop` |
| `vd-prompt` | `prompt` |
| `vd-detect-size` | `detectSize` |
| `vd-audio-prompt` | `audioPrompt` |

规则:**去掉应用 id 前缀(`vd-` / `tts-` / `tc-` ...) + camelCase**。如果应用有特殊需求,可在 `wizard.json` 给字段加 `"ui_key": "source"` 显式指定。

### 5.2 后端 helper: `core/compute/registry.py::bring_up_models()`
```python
async def bring_up_models(
    required_capabilities: list[str],
    npu_assignments: dict[str, int],   # {swcap_xxx: npu_index}
) -> dict:
    """
    一键开启所需模型 — 关闭所有非必需实例 + 启动必需实例。
    返回 {stopped_ids: [...], started_ids: [...], errors: [...]}

    约束:本函数不知道任何具体应用 / capability / model_id 名字,
          只接收参数,内部按 capability 字符串匹配。
    """
    # 1. 列当前所有 running instances,过滤掉 capability ⊆ required_capabilities 的
    # 2. 逐个 stop(原子失败回滚:已 stop 的要 restart)
    # 3. 按 npu_assignments 启动必需 instances
    # 4. 返回结果
```

**前端调用**: 直接用现有 `/api/compute/instances/{id}/stop` 和 `/api/compute/instances` POST start,前端编排:
```js
async function bringUpModels(wizardCfg) {
  // 1. GET /api/compute 列 running instances
  const snap = await fetch("/api/compute").then(r => r.json());
  // 2. 关闭非必需
  for (const inst of snap.instances) {
    if (!inst.capability_types.some(c => wizardCfg.swcap_to_model[c])) {
      await fetch(`/api/compute/instances/${inst.id}/stop`, { method: "POST" });
    }
  }
  // 3. 启动必需
  for (const [swcap, { npu_index, model_id }] of Object.entries(wizardCfg.swcap_to_model)) {
    await fetch("/api/compute/instances", {
      method: "POST",
      body: JSON.stringify({ action: "start", model_id, npu_index })
    });
  }
}
```
**回退策略**: 启动失败 → 自动重启被 stop 的实例;连续 2 次失败 → `confirm("一键开启失败,建议重启板子。")`

---

## 6. 错误处理范式

### 6.1 env_check 失败
| 失败类型 | UI 表现 | 用户可做 |
|----------|---------|----------|
| HW 不满足 | 步骤顶部红字 "硬件能力 XXX 不满足",**禁用下一步按钮** | 无,锁死当前步骤 |
| SW 不满足(模型未启) | 步骤顶部黄字 "模型未启动"+ "一键开启所需模型"按钮 | 点按钮 → 启模型 → 继续 |
| 一键开启第 1 次失败 | 步骤顶部红字 "开启失败,正在重试...",自动重试 | 等 |
| 一键开启第 2 次失败 | `confirm("一键开启连续 2 次失败,建议重启板子。\n\n点击'确定'保留当前步骤手动开启,点击'取消'退出向导。")` | 确定 → 保留当前步骤;取消 → 退出向导 |

### 6.2 finish 失败
| 失败类型 | UI 表现 | 用户可做 |
|----------|---------|----------|
| `applyWizardSnapshot` 抛 `WizardSnapshotError("open_source_failed")` | 步骤顶部红字 "播放视频失败"+ "回向导修改"按钮 | 点按钮 → 回 source 步骤 |
| `applyWizardSnapshot` 抛 `detector_start_failed` / `audio_start_failed` | 步骤顶部红字 "启动失败" | 等待模型启动 / 重试 |
| 模型未启完(检测/分析启动后端还在 loading) | 步骤底部 spinner + "等待模型启动中..." | 等 setInterval 轮询 status=running |

### 6.3 跳过向导
- 已填字段**全部写入 localStorage**(即使没点完成)
- 不触发任何 `applyWizardSnapshot` 调用
- 直接 unmount + 进应用页(应用页面初始为空,用户自行配置)

---

## 7. 持久化策略

### 7.1 localStorage key 格式
默认: `wdnwebui.wizard.<app_id>.values`(可在 `wizard.json` 用 `localStorage_key` 字段覆盖)

### 7.2 写入时机
- 每步"下一步"按钮按下时,**序列化当前已填字段**到 localStorage
- 即使中途"跳过向导",已填字段也保留

### 7.4 预填时机
- 进入向导**第 1 个 form 步骤**前,从 localStorage 读 → 反序列化 → 填到对应字段
- "完成配置"成功(applyWizardSnapshot 成功返回) → 保留 localStorage 值(供下次预填)
- "完成配置"失败 → 保留 localStorage 值(供用户回到向导后看到原值)

---

## 8. 视觉规范

### 8.1 容器
- `max-width: 1024px`(桌面端)/ `max-width: 100vw`(移动端)
- `margin: 80px auto 0`(顶部留 80px 让 wizard 不贴 sidebar 底)
- 背景:`#ffffff`(强制浅色,不跟系统主题走)
- 内边距:`40px 56px`

### 8.2 步骤指示器
- 顶部水平居中,横向 dot + 数字(●─●─○─○─○)
- 完成态:`#10b981`(绿色) + `✓` 标记
- 当前态:`#2563eb`(蓝色) + 外圈
- 未到达:`#d1d5db`(灰色)

### 8.3 CSS 命名空间(零耦合硬要求)
所有 wizard 类名必须以 `.wd-wizard-` 前缀开头。例:
- `.wd-wizard-container`
- `.wd-wizard-step-dot`
- `.wd-wizard-next-btn`
- `.wd-wizard-env-check-item`
- `.wd-wizard-skip-btn`

**禁止**使用通用类名(`.btn` / `.form` / `.next` / `.finish` 等)

### 8.4 移动端响应式(1024px 以下)
- 容器 `max-width: 100vw`,内边距 `24px 16px`
- 字号缩小到 `14px`
- 字段垂直堆叠(单列布局)
- 步骤指示器 dot 缩小到 `24px`

---

## 9. 性能与质量

### 9.1 已知边界
- **不做 a11y**:不接键盘导航 / 不接 ARIA / 不接焦点跳转,业务方按需后续追加
- **桌面优先**:移动端基本能用就行,不专门做断点测试

### 9.2 DOM 清理
- unmount 时**彻底销毁** wizard 注入的所有 DOM 节点
- 释放所有 event listener
- 取消所有 setInterval / setTimeout / fetch
- 不残留 ghost 节点 / 内存泄漏

### 9.3 跨应用一致性
- 每个应用只写 `wizard.json` + 暴露 `applyWizardSnapshot`,framework 代码零修改
- 8 张卡片应有**统一的视觉 / 交互 / 错误范式**,用户体验一致

### 9.4 异常隔离
- wizard 任何代码 try/catch,异常**静默吞掉** + console.warn(不让 wizard bug 阻塞应用 init)
- 应用 register_api / app.js 报错 → 走应用自己的错误兜底,wizard 不感知

---

## 10. 跨应用复用清单

### 10.1 新增应用向导的工作量(估 1-2 小时)
```
1. 写 app/apps/<app>/wizard.json           (15 分钟,字段填表)
2. 改 app/apps/<app>/manifest.json          (1 分钟,加 wizard_config 字段)
3. 改 app/apps/<app>/static/app.js          (30 分钟,暴露 applyWizardSnapshot)
4. playwright smoke test                    (30 分钟,5-10 个用例)
5. 跑零耦合验证                            (5 分钟)
   └─ rm -rf app/apps/<app>/ 后其他应用仍能 init
```

### 10.2 验证清单(每应用实施后跑)
- [ ] manifest 字段:`wizard_config` 已加
- [ ] wizard.json: `swcap_to_model` 字段映射的 model_id 在 MODEL_PRESETS 里存在
- [ ] applyWizardSnapshot:字段名与 wizard.json steps.fields 对齐
- [ ] localStorage:刷新浏览器后,wizard 字段预填正确
- [ ] 一键开启:SW 不满足时点击按钮,模型正确启停
- [ ] 完成配置:视频源打开 + 检测启动 + 音频启动(每项独立可空)
- [ ] 跳过向导:已填字段保留到 localStorage
- [ ] 错误兜底:vd-open 失败 / 模型未启完 / HW 不满足 三种错误都有显式 UI
- [ ] 零耦合:删除本应用目录,其他 7 个应用仍能 init
- [ ] grep 检查:framework / system.js 无应用 ID 硬编码

---

## 11. 后续可选增强(本期**不做**)
- a11y / 键盘导航 / 焦点管理
- 完整移动端响应式(多个断点)
- 向导内嵌套 sub-wizard(应用内子流程)
- 向导回放(用户操作录像)
- 国际化 i18n(目前只中文)