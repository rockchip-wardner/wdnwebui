# `app/apps/text_chat` 使用点分析报告

> 分析日期：2026-11-12
> 仓库：`/userdata/wdnwebui`（分支 `master`，HEAD `4175400`）
> 范围：`app/apps/text_chat/` 全目录，逐文件 + 全仓引用追踪

---

## 0. 一句话结论

`app/apps/text_chat` 是**卡片插件架构下仓库内仅剩的一张内置卡片**（`tts` / 视频类
卡片已外移到 `wdnapps_*` 插件包）。它对外的价值有**三层**：

1. **它自己是一个消费者** —— 通过 `swcap_text_chat` 能力拿 LLM 会话做多轮对话；
2. **它定义了一个能力词表** —— `swcap_text_chat` 被微信桥接（`im_bridge`）复用，
   两个业务线共用同一批模型实例；
3. **它是共享 chat 子系统 `app/core/chat/` 的主要使用者之一** —— `runner` /
   `agent` / `serial` 三件套由它和 `im_bridge` 各占一半。

真正**反向依赖**它代码的地方很少：只有 `scripts/verify_thinking_router.py`
直接 import 了它的内部类 `_ThinkingRouter`。其余引用都是「卡片注册」级别的
间接引用。

⚠️ 发现一处**遗留死代码**：`app/system/static/system.js` 引用了一个**并不存在的
全局变量 `TextChat`**（详见第 6 节）。

---

## 1. 目录文件清单与职责

| 文件 | 行数 | 职责 |
|---|---:|---|
| `__init__.py` | 64 | 插件入口。`register_api()` → `app.include_router(router, prefix="/api/text-chat")`；`register_shutdown()` → 释放 service 单例 |
| `manifest.json` | 57 | 卡片声明（id / 能力 / 后端入口 / 前端资源 / demo_guard / wizard） |
| `api.py` | 164 | FastAPI router：5 个 REST + 1 个 WebSocket |
| `service.py` | 722 | `_TextChatManager`（单例、worker 线程、状态机、WS 广播）+ `_ThinkingRouter`（thinking/answer 分频） |
| `thinking_9b.py` | 137 | Qwen3.5-VL-9B 的 `Thinking Process:` 输出适配器（9B 不用 `<think>` 标记） |
| `static/app.js` | 703 | 前端 `TextChat` 模块（IIFE），流式渲染 + 向导回填 |
| `static/template.html` | 64 | `#page-text-chat` section，被服务端注入首页 |
| `static/app.css` | 1 | **空文件** —— 真实样式全在 `framework.css` 的 `#page-text-chat` 段 |
| `static/wizard.json` | 18 | 向导配置（env_check → form → finish） |
| `static/preview.mp4` | — | 首页卡片预览视频 |

> ⚠️ `app.css` 是空文件但仍被 `manifest.json` 的 `frontend.css` 引用 —— 卡片
> 样式实际由 `app/static/core/framework.css:4287-4431` 承担（`#page-text-chat`
> 前缀作用域）。这意味着**卡片与框架样式耦合**：`framework.css` 里为
> `text-chat` 写了一大段专属规则，拆卡时这 145 行要跟着走。

---

## 2. 使用点全景（10 类）

### 2.1 卡片插件自动发现 —— `app/core/manifest.py`

```
app/core/manifest.py:11-13
  1. **Built-in** — ``app/apps/<card_id>/manifest.json`` scanned from disk.
     After the v0.25 split this holds only ``text_chat`` (``tts`` moved out
     to the ``wdnapps_audio_chat`` plugin package).
app/core/manifest.py:141
  """Scan ``app/apps/*/manifest.json`` and return validated manifests."""
```

链路：`manifest.json` → `backend.module = "app.apps.text_chat"` →
`app.apps.text_chat.__init__.register_api()` → 挂载 `/api/text-chat`。

**现状**：`ls app/apps/` 只有 `text_chat` 一个子目录 —— 它是**仓库内唯一的
内置卡片**，manifest 扫描器的内置通道实际上只服务它一个。

### 2.2 静态资源挂载 —— `app/main.py`

```
app/main.py:257
  # 5. Mount per-card static dirs at /static/apps/<id>
```

→ `/static/apps/text-chat/app.js`、`/static/apps/text-chat/template.html`、
`/static/apps/text-chat/preview.mp4`（`manifest.json` 的 `preview_video`）。

### 2.3 首页卡位占位符 —— `app/static/index.html.template`

```
app/static/index.html.template:623
      {% card_slot "text-chat" %}
```

→ `app/core/html_assembly.py::assemble_index_html` 逐个替换成该卡的
`static/template.html`，产出 `id="page-text-chat"` 的 section。

> 按 AGENTS.md 的硬性要求：加新卡**必须**在 `index.html.template` 加一行
> `{% card_slot %}`，漏了不报错、只静默白屏。`text_chat` 是现存唯一范例。

### 2.4 前端卡片 JS 加载 —— `app/static/core/framework.js`

```
framework.js:486-488   if (!DemoGuard._loadedApps.has(page)) { _prepareCardEntry(page); this._loadCardJS(page); }
framework.js:526-554   _loadCardJS(page) → 按 manifest.frontend.js 逐个注入 <script> → WizardRegistry.tryMount(m)
framework.js:931-969   预热所有卡片 JS（preload），避免首次点击的竞态
framework.js:637-640   wizard:unmounted 只清 _wizardMounted，**不动 _loadedApps**
```

**关键约束**：`app/apps/text_chat/static/app.js` 的 IIFE **永不重跑**
（`_loadedApps` 只增不减）。因此该文件里的**模块级状态**（`this.snapshot`、
`this._sending`、`this.pending`、`this._historyLoaded`、`this._capReady`）
是跨切页持久的单例 —— 改这个文件时必须假定 IIFE 只执行一次。

### 2.5 DemoGuard 后台守卫 —— manifest + framework

`manifest.json`：
```json
"demo_guard": {
  "active_state_keys": ["session.busy", "session.running", "session.state"],
  "stop_endpoints":     [{"method":"POST","path":"/api/text-chat/stop"}],
  "stop_sync_endpoints":[{"method":"POST","path":"/api/text-chat/stop"}]
}
```

框架侧（`framework.js:1320-1337`）从 `window.__APP_MANIFESTS__.cards[].demo_guard`
**自动注册**，卡片不需要自己 `DemoGuard.register(...)`。

卡片侧上报（`app/apps/text_chat/static/app.js:227-228`）：
```js
if (typeof DemoGuard !== "undefined") {
  DemoGuard.report("text-chat", { session: this.snapshot });
}
```

停止链路：`DemoGuard.stopAndClear()` → `POST /api/text-chat/stop`
（`api.py:99-104`）→ `service.stop()`（`service.py:288-309`）
→ `session.stop()` → 经 wdndds 旁路 DDS topic 送到 worker
→ `ServedSession.request_stop()` → `LLMLocalSession.stop()`
→ rknn3 侧**真正中止本轮并释放 NPU**（真中断，不是丢弃结果）。

### 2.6 能力层 —— `swcap_text_chat`

| 位置 | 作用 |
|---|---|
| `tools/worker_manifests/qwen3.5-vl-9b.json:4` | worker 声明 `swcap_text_chat` + `swcap_vision_chat` + `swcap_tool_call` |
| `tools/worker_manifests/qwen3.5-vl-4b.json:4` | worker 声明 `swcap_text_chat`（9B 的回退） |
| `app/apps/text_chat/manifest.json:10` | 卡片声明依赖 `swcap_text_chat` |
| `app/apps/text_chat/service.py:114` | `PREFERRED_MODELS = ("qwen3.5-vl-9b", "qwen3.5-vl-4b")` |
| `app/apps/text_chat/service.py:507-519` | `_select_session()` → `reg.get_session_for_model("swcap_text_chat", model_id)`，全失败抛 `RuntimeError("功能未启用: swcap_text_chat")` |
| `app/apps/text_chat/service.py:188-193` | `_ensure_model_meta()` → `get_registry().list_sessions("swcap_text_chat", include_remote=True)` |
| `app/core/compute/registry.py:5014` | 被调用的 `get_session_for_model()` 实现 |
| `app/core/compute/registry.py:642` | 注释：qwen3-4b worker 已删，qwen3.5-vl-4b 已声明 `swcap_text_chat` |

**上下文窗口跟随模型**：`service.py:521-527` `_limits_for_session()` 从
`session.spec.max_context_len` 读真实值；`max_new_tokens` 固定取共享的
`chat_runner.MAX_NEW_TOKENS`（2048）。改模型不用改代码。

### 2.7 共享 chat 子系统 —— `app/core/chat/`

```
app/core/chat/__init__.py:3-5
  Used by the text-chat card (apps/text_chat) and the WeChat-bridge card
  (apps/im_bridge). Both wrap an LLMSession with an agent-loop and
  message-stream serialization.
```

| 模块 | 被 text_chat 用的部分 | 另一个使用者 |
|---|---|---|
| `app/core/chat/runner.py` | `MAX_NEW_TOKENS`(2048)、`split_thinking()`（兜底切分） | `agent.py` 内部也用 |
| `app/core/chat/agent.py` | `run_chat_with_tools()` —— 工具调用循环、上下文裁剪、`_last_prompt_tokens` 回填 | `im_bridge/llm.py:326` |
| `app/core/chat/serial.py` | `get_chat_run_lock()` —— **进程级 RLock** | `im_bridge/llm.py:327` |

`serial.py:3-7` 说明了为什么需要这个锁：
> `text_chat` 的 worker 与微信桥接（`wechat_llm`）各自持有一个 worker 线程，
> 二者都可能调用 `session.respond(...)` 往同一个 `compute_worker` 子进程发 RPC
> 推理。worker 端虽然串行消费请求，但主进程侧并发 `chat_run` 的轮询 / 停止
> 逻辑需要跨调用者串行化。

⇒ **text_chat 与微信桥接共享同一个 NPU 推理通道，互相串行。**

### 2.8 工具注册表 —— `app/core/tools/registry.py`

```
service.py:529-544   _resolve_tool_names() → resolve_available_tool_names(channel="chat_web")
registry.py:219      def resolve_available_tool_names(channel: str = "") -> List[str]:
```

**渠道隔离**：text_chat 用 `channel="chat_web"`，微信桥接用 `channel="chat_wechat"`
（`im_bridge/llm.py:540-558`）。`VideoSearch` 只在 `chat_wechat` 生效
（依赖 `ctx.user_id` 关联用户视频，web 端 `user_id` 恒空、必败）⇒
**text_chat 拿不到 VideoSearch 的 schema**，模型看不到就调不到。

⇒ 结论：text_chat 的工具集 ≠ 微信桥接的工具集，**是刻意分开的**。

### 2.9 向导 —— `wizard.json` + `applyWizardSnapshot`

```
manifest.json:56          "wizard_config": "wizard.json"
static/wizard.json:5      "swcap_text_chat": { "npu_index": [0, 1], "model_id": "qwen3.5-vl-9b" }
static/wizard.json        steps: env_check → form(首次提问 / 深度思考开关) → finish
```

向导 finish 回调：`app/apps/text_chat/static/app.js:672-701`

```js
window.ImApp_text_chat.applyWizardSnapshot = async function (opts) {
  await TextChat.resetForWizard();        // 上一轮历史不带进新一轮
  // enableThinking → 页面开关 + localStorage["text_chat_thinking"]
  // prompt → 回填 #text-chat-input 并**自动发送**
};
```

全局名约定 `ImApp_<app.id 去 hyphen>` —— wizard 按此查找。
**向导完成 = 已经明确要开始对话**，所以是「自动发送」而非「仅预填」。

### 2.10 回归脚本

| 脚本 | 覆盖 |
|---|---|
| `scripts/verify_thinking_router.py:25` | **唯一直接 import text_chat 内部类**：`from app.apps.text_chat.service import _ThinkingRouter`。纯逻辑，测 thinking 分频 + `<answer>` 标签剥离 + 标签被 delta 切开的情形 |
| `scripts/verify_stage3_smoke.py:327-367, 500-505` | 真机 smoke：`swcap_text_chat` 纯文本轮 + 工具轮 |
| `scripts/verify_dual_card_smoke.py:191` | 双卡场景下 `swcap_text_chat` 的分配 |
| `scripts/verify_single_card_ui.py:366-391` | 单卡 UI：text_chat 正常、模型 POST 到 NPU0 |
| `scripts/smoke_wizard_all.py:14` | `APPS` 列表含 `"text-chat"` |
| `scripts/verify_9b_thinking_tool_long.py:94` | 9B 长思考 + 工具调用 |
| `scripts/verify_chat_retry_empty_answer.py:32` | 空回答自动重试 |
| `scripts/verify_thinking_default_collapse.py` | 思考块默认折叠 |
| `scripts/verify_capability_groups.py` / `verify_model_packaging.py` / `verify_app_tile_ui.py` | 能力分组 / 模型打包 / 卡片角标 |
| `.github/workflows/regression.yml:60` | CI 对 `app/apps/text_chat/api.py` 做 `py_compile` |

---

## 3. 谁真正「用到」text_chat 的代码

按耦合强度从高到低：

### A. 直接 import 内部实现（1 处）

```
scripts/verify_thinking_router.py:25
  from app.apps.text_chat.service import _ThinkingRouter
```

只有测试脚本。生产代码**零外部直接 import** —— 符合 AGENTS.md
「卡片不得跨卡 import，只能 import `app.core.*`」的约束。

### B. 通过 manifest 约定间接引用（生产路径的主通道）

| 引用方 | 引用点 |
|---|---|
| `app/core/manifest.py` | 扫描 `manifest.json` → 调 `register_api` / `register_shutdown` |
| `app/main.py:257` | 挂载 `/static/apps/text-chat/` |
| `app/static/index.html.template:623` | `{% card_slot "text-chat" %}` |
| `app/static/core/framework.js` | 按 `manifest.frontend.js/css` 注入卡片脚本 |
| `app/static/core/framework.css:4287-4431` | `#page-text-chat` 专属样式 |
| `app/system/static/system.js:1258-1263, 1289-1291` | **死代码，见第 6 节** |

### C. 共享能力 `swcap_text_chat`（最实质的对外价值）

```
app/system/im_bridge/llm.py:87    get_registry().get_session("swcap_text_chat")   ← 可用性检查
app/system/im_bridge/llm.py:331   get_registry().get_session("swcap_text_chat")   ← 实际推理
app/system/api/im_bridge.py:126   开启微信桥接的开关要求 swcap_text_chat 已 running，否则 409
```

**这是 text_chat 最大的外溢价值**：微信文字对话桥接完全靠
`swcap_text_chat` 能力，不需要 text_chat 的代码 —— 两条业务线只共享
`app/core/chat/*` 和模型实例，不共享卡片代码。

### D. 共享 `app/core/chat/*` 子系统

`text_chat` 和 `im_bridge` 各占一半。**text_chat 不是唯一用户**，
但它决定了 `app/core/chat/` 的接口形态（`_last_prompt_tokens` 回填、
`history_append` 语义等，都是为 text_chat 的 UI 服务的）。

### E. 配置命名空间 `compute.text_chat`

```
app/system/im_bridge/llm.py:527-535
  cfg = get_config().get_section("compute", {})
  tc = cfg.get("text_chat")
  v = int(tc.get("context_limit") or 16384)
```

配置键 `compute.text_chat.context_limit` **以 text_chat 命名**，但读它的是
**微信桥接**。当前 `config.jsonc` 里没有这一段 ⇒ 走默认 16384。
（text_chat 自己**不用**这个值 —— 它从 `spec.max_context_len` 读真实值。）

### F. 其它零散引用

| 位置 | 性质 |
|---|---|
| `app/core/compute/workers/_base.py:1607-1611` | 注释：worker 侧与主进程共用工具 registry 真源；`text_chat` / `im_bridge` 在**主进程**查 registry 拿真实 tool class 传给 `respond(tools=…)` |
| `app/core/chat/agent.py:285-287` | 注释：`_last_prompt_tokens` 曾未回填，导致 **text-chat 上下文计数器恒显 `0 / context_limit`** |
| `app/core/chat/agent.py:36-37` | 注释：调用方必须持 `get_chat_run_lock()`（与 text_chat / wechat_llm 相同） |
| `app/core/tools/_builtins.py:163-168` | 注释：reload 后工具 registry 空 → chat 流程返空 answer → 前端报「（模型未输出有效内容）」 |
| `docs/QWEN3_ASR_STREAMING_REPORT.md:762-764` | 文档：`text_chat/static/preview.mp4` 与 qwen3-asr 预览视频同规格 |
| `run.py:708` | 注释：浏览器会同时挂着 8 张卡的 WS（`text_chat` / `tts` / …） |

---

## 4. 依赖方向图

```
                    ┌──────────────────────────────┐
                    │   app/core/  (平台层)         │
                    │  manifest.py   卡片扫描/注册   │
                    │  main.py       静态挂载        │
                    │  html_assembly  卡位注入       │
                    │  compute/registry  能力会话   │
                    │  chat/{runner,agent,serial}   │
                    │  tools/registry   工具能力门控 │
                    └───────────────┬──────────────┘
                                    │  (只允许 core → app 反向不依赖)
        ┌───────────────────────────┼───────────────────────────┐
        │                           │                           │
        ▼                           ▼                           ▼
┌───────────────────┐    ┌──────────────────────┐    ┌────────────────────┐
│ app/apps/text_chat│    │ app/system/im_bridge │    │ scripts/verify_*   │
│  （卡片, 内置唯一）│◄───┤  wechat 桥接          │    │ verify_thinking_   │
│                   │ 能力│  ← 复用 swcap_text_chat│    │ router.py 直接 import│
└───────────────────┘    └──────────────────────┘    └────────────────────┘
        ▲
        │ manifest / card_slot / frontend.js
┌────────────────────────────────────────────────┐
│ app/static/  index.html.template + framework.js │
│                + framework.css(#page-text-chat) │
│                + im_bridge/app.js(注释残留)     │
└────────────────────────────────────────────────┘
```

**不变式**：
- `text_chat` → 只 import `app.core.*`（`service.py` 里全是函数内延迟 import）
- `im_bridge` → **不 import text_chat**，只共享 `app.core.chat.*` + 能力
- 卡片间**零代码耦合**，耦合点只有 `swcap_text_chat` 能力词表

---

## 5. 这张卡目前承担的产品职责（实况）

| 能力 | 实现位置 | 说明 |
|---|---|---|
| 多轮对话 + 上下文滚动裁剪 | `service.py` + `chat/agent.py` | 用 worker tokenizer 精确计数，超 `context_limit - max_new_tokens` 丢最旧历史 |
| 刷新后恢复气泡 | `api.py:70-74` `GET /history` | `service.history()` 过滤掉 `_system_note` / `_placeholder` 内部条目 |
| 流式 thinking / answer 双频道 | `_ThinkingRouter` / `Thinking9BParser` | 4B 用 `<think>`，9B 用 `Thinking Process:` + `\n\n\n` 边界 |
| 真中断 | `service.stop()` → `session.stop()` | rknn3 侧中止并释放 NPU |
| 工具调用 | `chat/agent.py::run_chat_with_tools` | 工具集按 capability + `channel="chat_web"` 过滤 |
| 向导一键起步 | `wizard.json` + `applyWizardSnapshot` | 填完即发 |
| 后台守卫 | `demo_guard` + `DemoGuard.report` | 切页/关页自动停 |

---

## 6. ⚠️ 发现的问题

### 6.1 `system.js` 引用了不存在的全局 `TextChat`（遗留死代码）

`app/system/static/system.js`：

```js
1258:  if (name === "text-chat") {
1259:    TextChat.init();
1260:    await TextChat.refresh();
1261:    TextChat.ensureWs();
1262:    return;
1263:  }
...
1289:  if (prev === "text-chat" && name !== "text-chat") {
1290:    TextChat.closeWs();
1291:  }
```

但全仓唯一的 `TextChat` 定义在
`app/apps/text_chat/static/app.js:5`：

```js
(function () {
  "use strict";
  const TextChat = { ... };      // ← IIFE 作用域内的 const，不是全局
```

`grep -rn "TextChat" app/` 的完整结果确认：没有任何 `window.TextChat =` 赋值。

**为什么现在没炸**：`system.js` 的 `showPage(name)`（第 1278 行）是
**死函数** —— 全仓没有任何调用者。框架用的是 `ROUTER.showPage(page)`
（`framework.js:1158`），不是 `system.js` 的 `showPage`。因此
`loadPageData("text-chat")`（第 1173 行定义、第 1313 行调用）**永远不会被
text-chat 页触发**，那 3 行 `TextChat.*` 也永远不会执行。

卡片真正能工作，靠的是自己的 `app.js`：
`app/apps/text_chat/static/app.js:661` 在 IIFE 末尾直接 `TextChat.init();`，
WS 也由卡片自己管理。

**风险**：任何时候有人「修复」system.js 的 `showPage` 让它重新可达（比如
让框架的路由回调到它），这 3 行就会抛 `ReferenceError: TextChat is not defined`
—— 而因为它在 `async function loadPageData` 里，异常会被静默吞掉或只打断
数据加载，症状是「进卡片页 WS 不连 / 状态栏不刷新」这种极难定位的问题。

**同类残留**：`app/system/static/im_bridge/app.js:652-660` 有一整段
`TextChat: 文本对话页面模块` 的注释块，但模块本体已不存在 —— 卡片外移时
正文删了、注释留下了。

**建议**：删掉 `system.js:1258-1263` 和 `:1289-1291` 两段（连同
`im_bridge/app.js:652-660` 的注释残骸）。若要保留 `system.js` 的入口，
正确做法是让 text_chat 的 `app.js` 显式导出全局，例如
`window.TextChat = TextChat;`（与 `window.ImApp_text_chat` 同一模式）。

### 6.2 `app.css` 是空文件

`manifest.json` 的 `frontend.css` 仍引用 `/static/apps/text-chat/app.css`，
但该文件只有 1 行（空）。所有样式在
`app/static/core/framework.css:4287-4431`（约 145 行 `#page-text-chat` 规则）。

⇒ 卡片样式**不在卡片目录里**，拆卡/外移时这 145 行必须一起搬走，否则新卡
会样式全失。框架 CSS 里出现卡片专属规则，本身也违反
「`framework.css` 是平台层、不该知道具体卡片」的边界。

### 6.3 卡片 JS 的 IIFE 永不重跑 —— 模块状态是跨页单例

`framework.js:637-640` 明确：`wizard:unmounted` 只清 `_wizardMounted`，
**不动 `_loadedApps`** ⇒ `app/apps/text_chat/static/app.js` 的 IIFE
在整场会话里只执行一次。

因此该文件里 `TextChat` 对象上的所有可变字段（`snapshot`、`_sending`、
`pending`、`messages`、`_historyLoaded`、`_capReady`、`ws`、`wsRetry`）
都是**跨切页持久的单例状态**。改这个文件时必须假定：
「`init()` 只会跑一次，但 `refresh()` / `send()` 会被反复调用」。

### 6.4 `compute.text_chat.context_limit` 命名与归属错位

`app/system/im_bridge/llm.py:527-535` 读 `compute.text_chat.context_limit`，
但这段配置**只影响微信桥接**，text_chat 自己完全不用它
（text_chat 从 `spec.max_context_len` 取真实值）。

⇒ 配置项名指向一个「不是它的主人」的模块。当前 `config.jsonc` 无此段，
走默认 16384。改名或补文档时容易踩坑。

### 6.5 `_ThinkingRouter` 被测试直接 import

`scripts/verify_thinking_router.py:25` 从
`app.apps.text_chat.service` import `_ThinkingRouter` —— 唯一一处对卡片
**内部私有类**的外部依赖。它是全仓唯一有专门回归覆盖的 text_chat 内部逻辑
（4B 的 `<think>` 分频 + `<answer>` 标签剥离 + 标签被 delta 切开）。

⇒ 若要外移卡片，`_ThinkingRouter` 需要一并搬走或提升为 `app/core/chat/`
下的公开模块，否则这条回归会断。

---

## 7. 结论与建议

### 结论

- `app/apps/text_chat` 是**仓库内唯一仍内置的卡片**，`manifest.py` 的内置
  扫描通道实际只服务它一个。
- 它的**对外价值主要在 `swcap_text_chat` 能力词表** —— 微信文字桥接
  （`im_bridge`）复用同一批模型实例，两条业务线只共享 `app/core/chat/*`，
  **不共享卡片代码**。
- 它是 `app/core/chat/{runner,agent,serial}` 的**两个使用者之一**
  （另一个是 `im_bridge`），并且是这两个模块接口形态的「需求方」
  （例如 `_last_prompt_tokens` 回填就是为 text_chat 的上下文计数器修的）。
- 生产代码**没有**外部直接 import text_chat 的实现；唯一的外部 import 在
  测试脚本 `verify_thinking_router.py`，且只碰 `_ThinkingRouter`。
- 前端通过 `manifest.json` + `{% card_slot %}` + `frontend.js/css` 声明式
  接入，**没有**跨卡的硬编码引用。

### 建议（按优先级）

1. **清理 `system.js` 的 `TextChat` 死代码**（`system.js:1258-1263`、
   `:1289-1291`）与 `im_bridge/app.js:652-660` 的注释残骸 —— 留着是个
   定时炸弹（§6.1）。
2. **把 `#page-text-chat` 的 145 行样式搬回卡片目录**，或至少在
   `manifest.json` / AGENTS.md 里写明「卡片专属样式当前寄生在
   `framework.css`」这一事实（§6.2）。
3. **`compute.text_chat.context_limit` 改名**（如 `compute.chat.context_limit`）
   或在配置说明里标注它**只作用于微信桥接**（§6.4）。
4. **外移卡片前**，先决定 `_ThinkingRouter` 的归属：搬到
   `app/core/chat/` 下，或随卡一起走 —— 否则 `verify_thinking_router.py`
   会断（§6.5）。
5. 改 `app/apps/text_chat/static/app.js` 时，**始终假定 IIFE 只执行一次**
   （§6.3）。

---

## 附：本次分析用到的检索命令

```bash
# 目录清单与行数
find app/apps/text_chat -type f
wc -l app/apps/text_chat/*.py app/apps/text_chat/static/*

# 全仓引用（50 个命中文件）
grep -rn "text_chat|text-chat|textChat|TEXT_CHAT" .

# 能力词表
grep -rn "swcap_text_chat" --include="*.json" --include="*.py" .

# 共享 chat 子系统的消费者
grep -rn "from app.core.chat" --include="*.py" .

# 死代码验证
grep -rn "TextChat" --include="*.js" --include="*.html" --include="*.template" app/
grep -rn "loadPageData|showPage" --include="*.js" app/
```

**未验证项**（需要真机 / 运行环境）：

- `swcap_text_chat` 在真机上的端到端推理与 NPU 释放时序；
- `stop()` 经 wdndds 旁路 topic 触达 `LLMLocalSession.stop()` 的实际中断效果；
- `verify_stage3_smoke.py` / `verify_9b_thinking_tool_long.py` 等需 NPU 的脚本
  在当前板子上的通过情况。
