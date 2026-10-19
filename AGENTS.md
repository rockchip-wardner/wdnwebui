# AGENTS.md

Compact guide for OpenCode sessions in this repo. **Current version: 0.9.2** (见 `app/__init__.py`)。

## What this repo is

Python web service (`wdnwebui`) exposing RK-platform hardware / model SDKs
(`wdnav`, `wdnai`) over HTTP via FastAPI. v0.4 起采用**卡片插件架构**:
每个应用是 `app/apps/<card_id>/` 下独立文件夹,自带 `manifest.json` +
后端 `api.py` + 前端 `static/`(template.html / app.js / app.css /
wizard.json);`app/core/manifest.py` 启动时扫描全部 manifest 装载。

## Stack at a glance

- **Web framework**: FastAPI + Uvicorn (`run.py` boots `app.main:app` on
  `0.0.0.0:8000` with `reload=True`).
- **Internal SDKs** (installed editable from private SSH git, **not** PyPI):
  - `wdnav` — Rockchip MPP / RGA2 / V4L2 video bindings.
    Native libs (`librockchip_mpp`, `librga`) must be installed on the host;
    `import wdnav` only — `import wdnav.video` is what touches the hardware.
  - `wdnai` — RK-chip LLM / VLM SDK (transformers + opencv-python).
  See `.venv/src/wdnav/AGENTS.md` and `.venv/src/wdnai/pyproject.toml` for the
  SDK-side gotchas (they're external code, not in this repo, but the
  constraints propagate here).

## 设计原则

1. 必须通过wdnai的LLMSession调用rknn，不得直接调用
2. 必须通过wdnav调用mpp和rga，做编解码和图片缩放、格式转换等操作，不得直接调用或者通过其他库进行软编解和格式转换、resize等操作。
3. 必须通过wdndds做发现和注册服务。
4. **LLMSession 必须从 `ComputeRegistry` 取，禁止各业务模块自建**。视频检测
   / 视频目标检测 / 视频目标定位 / 视频内容检索 / 视频文件分析 / 文本对话 /
   语音生成等服务只能通过
   `get_registry().get_session("<capability>")` 拿到 session;
   找不到对应 capability 就抛 `RuntimeError("功能未启用: <capability>")`,
   由 HTTP 层 502 透传给前端。(v0.4 起原 16 路视频监控**已下线**,作为
   视频能力的一部分整合进微信视频检索管线,见 `app/system/im_bridge/video_manager.py`。)
实例集合在 算力配置 页配置，存到 `config.jsonc["compute"]["instances"]`，
    `run.py` 启动时**逐个 spawn 独立 compute_worker 子进程**（不阻塞 UI）。
5. **所有演示页（音视频描述 / 视频目标检测 / 视频内容检索 / 语音生成）
   离开时必须主动关闭后台运行**。`app/static/core/framework.js (前端核心) + app/static/apps/<id>/app.js (各 app)` 的 `DemoGuard`
   统一负责：
   * in-app 导航（点击 sidebar）：若当前页后台在跑（解码
     `state ∈ {opening, playing, paused, eof}` 或 detector `enabled=true`，
     TTS `busy=true`），先弹 `confirm()`，用户确认后 await 后端
     `/close` + `/prompt/stop`（或 `/tts/stop`）再放行；用户取消则
    `preventDefault()` 阻止跳转。
   * `beforeunload`（关 tab / 刷新 / 关闭浏览器）：同步触发原生警告
     对话框，并通过 `navigator.sendBeacon` 或 `fetch({keepalive:true})`
     异步发停请求，best-effort 兜底。
   * `hashchange`（浏览器前进/后退）：静默 sendBeacon 兜底停掉所有
     在跑演示页的后端，避免路径绕过 click handler 时残留。
   * 判定由各页模块主动 push：`setVdMeta` / `updateVdDetectorStatus` /
     `setVtdMeta` / `updateVtdDetectorStatus` / `TTS.render` 在更新
     DOM 的同时 `DemoGuard.report(<page>, { session, detector })`。
   **浏览器限制**：beforeunload 期间 JS 不能保证请求送达（用户强杀、
   浏览器崩溃、某些移动浏览器会丢弃 beacon）；AGENTS 不做超出此限制
   的承诺。若可靠性要求更高，需要服务端按 session TTL / 心跳
   自动回收 —— 不在本期范围。

   **卡片 JS 生命周期 —— `_loadedApps` 与 `_wizardMounted` 解耦**
   （`app/static/core/framework.js`）：framework 用 `DemoGuard._loadedApps`
   记忆"卡片 app.js 已注入 DOM"，`showPage` 命中即跳过 `_loadCardJS`。
   向导 `unmount()`（用户切走 / 跳过 / `onNavAway`）会 dispatch
   `wizard:unmounted`。**曾经的写法**是它直接删 `_loadedApps`，于是切回
   该页时 framework 重新 `document.head.appendChild(<script>)` → 卡片
   IIFE **重跑**：新闭包 + 复用 DOM →（a）所有 `addEventListener` 在旧
   元素上**重复累积**（实测：点一次「清空缓存」弹出 N 个 `confirm`，需
   确认 N 次）；（b）模块级内存标志（`VAS_SEARCH._progressForceIdle` /
   `VAS._clearedRun` / `VAS_SEARCH.initialized` 等）全部归零 → 清空过的
   进度条在 WS 快照重放时复活 100%、反查按钮闪烁（用户实测）。

   现改为**两个独立集合**：`_loadedApps`（JS 已加载，**只增不减**）+
   `_wizardMounted`（向导当前挂载）。`wizard:unmounted` 只
   `_wizardMounted.delete(appId)`；`showPage` 对"JS 已加载但向导未挂"
   的页只调 `WizardRegistry.tryMount(m)`（**不重注 `<script>`**）并
   `_wizardMounted.add(page)`。效果：卡片 IIFE 永不重跑（监听器不累积、
   内存标志不丢），而"跳过向导后再进入会重弹向导"的语义保持不变
   （unmount 清标记 → 切回 `tryMount` 重挂）。
   **改路由 / 新增卡片页时勿再让 `wizard:unmounted` 触碰
   `_loadedApps`**。诊断：DevTools `getEventListeners(el).click.length`
   应在多次切页后恒为 1（CDP `DOMDebugger.getEventListeners` 同理）。

6. **worker 进程是薄 RPC server，类云端 AI 调用语义**。
   **一个共享入口 + 一个 per-model manifest**：
   `app/core/compute/workers/compute_worker.py`（**唯一**进程入口，所有
   model 共用）+ `workers/<model_id>.json`（每个模型一个文件，声明身份 +
   `spec_factory`）。进程只负责：
   * 接收 `wdndds.serve_local_session` 定义的
     `{prompt, attachments, options}`；
   * 透传给 `wdnai.LLMLocalSession.run_prompt(...)`（**跳过** prompt 重新
     组装 —— prompt 是调用方拼好的）；
   * 把模型返回的**原始文本**原样发回。

   **加模型 = 放一个 `workers/<id>.json`**，不用再写一份 45 行的入口脚本。
   曾短暂用过「一模型一文件夹」（`workers/<id>/{manifest.json,
   compute_worker.py}`），但 6 份入口脚本只差一个 docstring 和一行
   `mod["SystemLanguageModel"].<factory>()` —— 6 份近似副本正是这次拆分
   本来要消灭的东西，所以压成了单入口 + `spec_factory` 字段。

   **per-model 的行为差异写进 wdnai，不写进 manifest**。
   `spec_factory` 只回答「调 wdnai 的哪个方法造 spec」；特殊推理选项、
   不同的加载策略、额外 backend 一律在 `wdnai.LLMLocalSession` / backend
   层实现 —— 那样所有调用方都受益，而不是每个 worker 各分叉一份。
   manifest 只说「是哪个模型」，绝不说「怎么表现」。
   原先各入口 docstring 里的领域知识（presence_penalty=1.5 的来源、
   omni 单次 6s 上限等）已迁到各 manifest 的 `notes` 字段。

   **入口文件名与 `--model` 参数不可改**：
   `registry` 的 spawn 去重 needle 与 `system_monitor` 的进程扫描都匹配
   `/proc/<pid>/cmdline` 里的 `compute_worker.py\x00--model\x00<id>\x00`
   （`registry.py:_find_workers_by_name`）。改其中任何一个都会**静默**破坏
   孤儿检测与去重。扁平布局只改路径的目录部分，这个 needle 逐字节不变。

   worker **不应做**：
   * schema 字段语义理解（如 bbox 单位归一化、bbox 是否全画面、
     desc 是否"未检测到"语义识别等）—— 这些是业务侧（detector）
     的职责；
   * 图像裁切 / 编码 / base64 内嵌 —— 主进程 `_crop_and_encode`
     负责；
   * 历史事件持久化 / 缓存 / 诊断 ring buffer 维护 —— 主进程
     detector 自己的 `_events` / `_diag_history` 负责；
   * 业务级 schema-version 兼容 —— 跨主/worker schema 兼容应在
     proxy / detector 侧做（rpc_session_proxy 的 schema-version 桥
     接是合规的——它在主进程侧）。

   **判断标准**：worker 端任何对 `result.text` / `result.bbox` /
   `result.desc` 等字段做语义解析 / 转换 / 二次封装的提交都违规。
   worker 只做 `result.model_dump()` 序列化与 RPC 收发。修复
   `73b8808` 误删的 0-1→0-1000 hack 在 v0.14 重新引入主进程侧
   （`_normalize_bbox_to_0_1000`）而不是 worker 侧，正是这条原则
   的应用。

   **历史上下文 → 已闭环（v0.16）**：早期（`73b8808` 之前）worker 端曾把
   "0-1 → 0-1000 隐式校正"塞在 `compute_worker.py:infer_handler` 里
   （~12 行 + LOG.info），看起来无害但违反本原则——业务级语义理解放在
   worker 端会导致：(a) worker 必须知道 calling context（哪条业务线 /
   哪个 schema）才能决定要不要做这步；(b) 一份代码处理多种模型时归一化
   阈值互相干扰；(c) detach 进程不能 reload，bug fix 必须 restart worker
   （50s+ LLMSession 重启）。

   v0.16 把这条从"约定"变成了"物理上做不到"：worker 换成
   `wdndds.serve_local_session`，**`generating` / `tools` 根本不过线**，
   wire 上只有 `{prompt, attachments, options}`。worker 收到的是拼好的
   prompt，交回的是模型原始文本，`generating` 的解析（含
   `_normalize_bbox_to_0_1000`）全在调用方进程。同理 worker 侧的
   `_resolve_tool_classes` / `_parse_qwen_tool_call` /
   `chat_runner.build_chatml_prompt` 也一并消失 —— 不是"不该做"，是
   **没有信息可做**。

## 视频三卡：1~32 路并发（vtd / vtl / vd，v0.21~v0.22）

`video-target-detection` 已从「单路 canvas」改成「1~32 路宫格 + VLM 逐路轮转」。

### 为什么另起 `MultiChannelSession` 而不是改 `VideoSession`

`app/core/video/session.py` 的 `VideoSession` 是**进程级单例且被三页共用**
（`video_detection` / `video_target_detection` / `video_target_localization`），
内部只有 **1 个** `VideoCapture` + **1 个**解码线程。把多路塞进去等于让另外两页
的解码路径也背上 N 路复杂度 —— 违反「改动只触及目标功能及其直接依赖」。

所以新开 `app/core/video/multi_session.py::MultiChannelSession`。`N=1` 是它的
退化情况，**一套代码覆盖 1~32 路**，没有「单路分支 / 多路分支」两套路径。

> **v0.22 起 vtl 与 vd 也搬过来了** —— 三张卡全部走
> `MultiChannelSession`，没有单路例外。共用单例按 **key 注册**
> （`get_multi_session("vtd" / "vtl" / "vd")`）—— 同一个进程里每张卡各占一个
> key，否则 vtl 的 `open()` 会把 vtd 的解码器全 close 掉，表现为「切回上一页
> 画面全黑」。共享的是**代码**不是**实例**。
>
> 三张卡的 HTTP 层合并成了工厂 `app/core/video/multi_api.py`
> （`MultiCardSpec` + `build_multi_router(spec, tag)`）；卡片专属的东西留在卡内：
> vtd / vd 的 `image_size` / `model_id` 校验，vtl 的 `prompts[]` 校验与固定 448
> 送检，vd 的 `enabled_tools` 白名单（`tts_alert`）+ `/tools` +
> `/available-vision-sizes`。

### 帧怎么到 detector：`attach_detector` + 轮转，**不是** frame sink

三张卡的 `__init__.py` 曾经都把 detector 挂成共享 `VideoSession` 的 frame sink
（`add_frame_sink`）。**多路化之后那三份 sink 全部是死代码** —— 本卡的解码早已
换成 `MultiChannelSession`，`VideoSession` 上永远不会再有一帧到达 vd / vtd / vtl。
真正喂帧的是工厂在 `POST /prompt` 里做的三件事：

```python
session.attach_detector(det)          # 帧源
session.set_round_robin_interval(...) # 节奏
session.start_round_robin()           # 逐路取当前帧提交
```

三张卡**都已经删掉**这份接线。它不只是白费：`VideoSession` 现在仍被
`video_analysis_search` 的 `/source/open` 单路预览使用，于是「某张卡开着检测
→ 切到 VAS 点打开」会把 **VAS 的帧以 channel=0 灌进那张卡的检测器**
（`submit_frame` 只在 detector 未启用时才 no-op），事件被错记到第 1 路。
删掉它同时省掉一次无谓的每帧调用。

> `VideoSession` 单例本身还在，但三张多路卡都不再走它 —— 现在只剩
> `video_analysis_search` 的 `/source/*` 单路向导预览在用。

### 音视频描述（vd）的多路化：判据端点跟着搬家

vd 除了 `image_size` / `model_id`，还有两处 vtd 没有的东西：

* `enabled_tools` 白名单（只允许 `tts_alert`）—— 前端 UI 已过滤，后端
  `ALLOWED_TOOLS` 再卡一层是为了直接 curl 越权；
* `/tools` 与 `/available-vision-sizes` 两个页面独有端点（检测尺寸下拉框的数据
  源）。这两条不进工厂，追加在同一个 router 上。

**判据端点从 `/api/video/judge` 迁到 `/api/video-detection/judge`**：前者操作的
是单例 `VideoSession` 上的判据，多路页走 `MultiChannelSession` —— 自管理解码
循环的东西，配置就得落到自己这边。`FrameJudge.mount()` 必须显式传
`apiBase`，否则会静默去够那个已经没人写的单例。

前端还有两个只有 vd 才有的坑：

* **源框是 `#vd-sources`（复数）**，但页面下方的「音轨分析」读的是同一段文本的
  **第 1 路**（`readVdSources()[0]`）—— 音轨分析吃一路，不是 N 路。
* 判据下拉里 `gscore` 是默认选中（判帧算法 = 按画面变化采样）。多路下判据器
  **每路一个**，配置本身仍是服务级（配一次全路共用），逐路分数在
  `session.channel_list[i].judge`。

### 前端共享：`app/static/core/multi-grid.js` / `.css`

宫格（建格 / 分列 / 每路单飞解码上屏 / 角标高亮）抽成共享组件，三张卡共用：
`MultiGrid.{ensure,paint,status,reset,chNo}`，诊断计数在 `window.MultiGridDiag`。
**事件列表刻意不进共享组件** —— vtl 是 bbox、vd 是长文本描述，渲染策略差太远。

三个约定：

* `MultiGrid.paint(container, idx, src, overlay)` 的 `src` 既可以是 JPEG 字节，
  也可以**已经是可绘制对象**（canvas / ImageBitmap）。后者给「冻结当前画面」用：
  帧此刻就在 canvas 上，再走一遍编解码纯属浪费，还会掉画质。`overlay(ctx, tile)`
  在画完帧之后同步调用，让卡片把自己的标注叠上去。
* 通道号对外一律 1 基（`chNo`），内部 idx 是 0 基。
* **宫格 div 自己就是 `.vd-stage`**（`class="vd-stage vd-grid"`），不是套在
  stage 里面。`.vd-stage` 的 16:9 是给单 canvas 的尺寸基准；宫格要靠它给出确定
  高度，再用 `grid-auto-rows:1fr` 撑满行。提示层（`.vd-overlay`）要放在宫格
  **内部** —— absolute 不参与网格布局，`inset:0` 正好罩住画面。

### vtl 的「命中定格」：就地快照，不等新图

事件到达时把该路 tile canvas 的像素拷进离屏 canvas，定格 2s，期间的重绘就是
**反复重贴这张快照 + 叠 bbox**（浏览器渲染管线会把长期不写入的 canvas 降级）。

两个不能省的细节：

* **多路必须按路冻结**。用一份全局冻结态会把所有画面按在同一张旧帧上，看起来
  就是「整个画面卡住不动」。
* **不能等一张新图**。grounding 事件的 `frame_b64` 是**空的** ——
  `_build_grounding_event` 明确去掉了整帧编码（worker_loop 里每事件多编一次要
  30~100ms），只留 `crop_b64`。所以冻结源只能是画面本身。
* bbox 坐标在**送检帧**（448×252）空间，画布是**显示尺寸**（960 或 384 宽），
  必须按 `canvas 实测尺寸 / frame_w·frame_h` 换算，否则框会挤到左上角。

### 向导搬画面：容器 id 必须显式声明

向导按 `open_source_button` 去掉 `-open` 加 `-canvas` 推导画面容器 id。多路卡
的画面是**宫格 div**（`vtd-grid` / `vtl-grid` / `vd-grid`）而不是 canvas，
推导出的 `*-canvas` 根本不存在 —— 向导会把画面搬进面板这件事**静默失败**
（不报错，只是什么都不发生）。所以 `wizard.json` 支持顶层 `canvas_id` 显式
声明，推导只作单路老卡片的兜底。

同源的另一个坑：`isSource` 的正则是 `/-source$/`，而 vtd 的字段叫
**`vtd-sources`（复数）**，匹配不上时整个「浏览 + 打开/关闭」按钮行都不渲染。
现在是 `/-sources?$/`。textarea 也必须给浏览按钮 —— `FSModal._commit` 对
textarea 是**追加一行**（选一个文件 = 往源列表加一路），正是多路想要的。

### 向导的视频源字段必须显式 `"type": "textarea"`

页面上是 `<textarea id="xxx-sources">`，**不等于**向导里也是多行框 ——
`wizard.json` 的字段不写 `"type": "textarea"`，`state.js` 就按默认分支渲染成
**单行 `<input>`**。后果：向导里回车即提交，**一次只能填一路**，且没有任何
报错（浏览 / 打开 / 关闭按钮全都正常渲染，红不了任何断言）。

vtl 就踩过这个：字段声明还停留在单路时代的 `{ "id": "vtl-source",
"label": "视频源 URL / 路径" }`。同一份 `wizard.json` 里 vtd / vd 都带
`"type": "textarea"`，只有它漏了 —— 肉眼扫三张卡的字段几乎看不出差别，
只有真去填多路才会撞上。

三张卡现在必须逐字对齐（改一张就核另外两张）：

| 卡 | `canvas_id` | 字段 id | type |
|---|---|---|---|
| vtd | `vtd-grid` | `vtd-sources` | `textarea` |
| vtl | `vtl-grid` | `vtl-sources` | `textarea` |
| vd  | `vd-grid`  | `vd-sources`  | `textarea` |

字段 id 也用复数：单数 `vtl-source` 配多行 textarea 是自相矛盾的写法，而
下一个读代码的人正是靠 id 判断"这是一路还是 N 路"（vd 页内的音轨分析就是
靠这个区分取第 1 路 vs 全部）。

**验收别只看"渲染成 textarea"** —— 那只是必要条件。要真的往向导字段里填
4 行、点向导的「打开」、确认宫格出 4 格。`scripts/verify_frame_judge_ui.py`
只填了一路，所以红不了这个 bug。

### 线程模型

```
N 个解码线程   各自 1 个 VideoCapture，按 decode_fps(默认5) 节流 read0()
               → latest_bgr（VLM 用）
               → 按 display_fps(默认2) 才做 cv2.imencode → 广播给 WS 订阅者
1 个 VLM 调度   严格轮转 ch0→ch1→…→chN-1，每路 wait_idle() 等上一路推理完
               才提交下一路（见下「为什么要 wait_idle」）
```

### 三个默认值的实测依据

| 参数 | 默认 | 为什么 |
|---|---|---|
| 送检尺寸 | **不设参数**，按源算（`derive_inference_size`） | 与单路 `VideoSession` 逐字同口径 `max(thumb*4, src//4)`：720p/1080p → 640、4K → 960。**必须这样**——送检尺寸决定 VLM 选哪个 vision 变体（见下文「RGA 输出尺寸会决定 VLM 用哪个 vision 变体」），送 384 就是掉进 61MB/0.74s 的便宜档，换来识别精度降档。多路不比单路更该将就。`analysis_size` 仅保留为可选的强制覆盖，默认 `None`=自动 |
| `decode_fps` | 25（**每路上限**） | 实际值按路数在总预算 `MAX_TOTAL_DECODE_FPS=660` 里摊分，详见下文「每路 fps 必须按路数摊分」 |
| `display_fps` | 25（**每路上限**） | 同上在 `MAX_TOTAL_DISPLAY_FPS=400` 里摊分。640×360 JPEG 实测 2.2ms（不是老注释里的 15~100ms 也不是 384 档的 0.77ms） |

### 为什么要 `wait_idle()`（共享模块的增量改动）

`WdnaiDetector._item_q` 是 **maxsize=1 的 drop-oldest** 队列。多路若同时
`submit_frame`，只有最后挤进去的那路能跑，其余**静默丢弃**。所以调度器靠
`detector.wait_idle()` 做背压。

对 `detector_base.py` 的改动全部是**向后兼容的增量**：

* `DetectorEvent.channel` —— 新字段，默认 0，单路事件恒为 0
* `submit_frame(..., channel=0)` —— `channel` 为 0 时仍投 **4 元组**，
  单路代码路径逐字节不变
* `wait_idle(timeout)` —— 新方法，单路页面从不调用
* `_worker_loop` —— 新解包 5 元组；`len(item) in (3,4)` 的历史分支保留

### 真机实测（本机 RK3588，720p 素材）

| 路数 | 解码 | 每路 fps | 轮转 | 事件 | 错误 |
|---|---|---|---|---|---|
| 16 路 | 1504 帧 | 5.0 | channel 序列严格 `0→15` | 46 条全覆盖 | **0** |
| 32 路 | 12008 帧 | 5.0 | 8 轮，覆盖全部 32 路 | 162 条全覆盖 | **0** |

单路 TargetDoc 推理 ~1.88s → **一轮 32 路 ≈ 60s**。这是单模型句柄串行的
固有代价（AGENTS.md 设计原则 4：同一模型句柄不可重入），不是缺陷。
事件序列里偶尔跳号是「该帧未检测到目标」不产事件，不是轮转跳号。

### 每路 fps 必须按路数摊分，不能一刀切写死

原来 `decode_fps=5 / display_fps=2` 是拿**32 路极限场景**的保守值直接给
所有路数的，结果**开 1 路也只有 5fps**，播放肉眼可见地卡 —— 而用户预期
就是源帧率 25fps。降帧的代价是**所有路一起变卡**，但 RGA / JPEG 编码是
**所有路合计**受限的，不该让单路替多路背锅。

→ 改成 `derive_decode_fps(n)` / `derive_display_fps(n)`：N 路共享一个总
预算，路数少时每路跑满 25fps，只有超上限才降。`decode_fps` /
`display_fps` 字段降级为**每路上限**，快照另开 `decode_fps_target`（实际）
/ `decode_fps_cap`（上限）两个字段上报。

| 预算 | 值 | 依据 |
|---|---|---|
| `MAX_TOTAL_DECODE_FPS` | 660 | **跟着送检尺寸走**：热路径每路只产一张图（既送显又是冷路径的源），720P 源整条链单帧成本 640×360 = **4.34ms**（read0 2.10 + jpeg 2.23），32 路合计实测 **659.6** 帧/秒、0 错误 |
| `MAX_TOTAL_DISPLAY_FPS` | 400 | 640×360 JPEG 实测 2.2ms，400 张 ≈ 0.9 核 |

> **归因修正**：这段原先按「RGA 次数 == 解码帧数，RGA 是瓶颈」估预算，实测
> **RGA 只占 0.002ms**，大头是 `read0` 里的 NV12→BGR 转换（1.14→2.10ms）和
> JPEG 编码（0.80→2.23ms）。照旧认知会严重低估抬送检尺寸的真实代价。

> **别为了让 32 路跑满 25fps 把 660 调回 800**：调度器会给每路 25fps 的节流
> 目标但硬件给不出来，结果每帧都「追不上节流」退化成满速空跑，实测 fps 在
> **15.4~21.5 之间乱跳**。主动限到 660/32 = 20.6 后实测稳定在 **19.4~20.2**。

实测（720P 源，640×360 输出）：

| 路数 | 解码合计 | 每路解码 | 错误 |
|---|---|---|---|
| 1 | 25.1 | 24.97 | 0 |
| 6 | 149.3 | ~24.8 | 0 |
| 16 | 399.0 | ~24.7 | 0 |
| 32（640 档，现行） | **659.6** | 20.6 | 0 |
| 32（旧的 384 档，已弃用） | 788.9 | ~24.2 | 0 |

### 踩过的坑：display 与 decode 同周期 → 相位抖动吃掉一半帧

症状：请求 25fps，UI 实际只收到 **15.6fps**（解码是满的 25fps，所以只看
解码会漏掉这个问题）。

根因：解码节流用 `remain = t0 + target*period`，显示节流原先写成
`next_disp = now + disp_period`。低路数下 `display_fps == decode_fps`
是**同一个周期**，帧到达时刻相对阈值有毫秒级抖动，`now >= next_disp`
就会隔帧不成立 —— 白白丢掉约一半的 JPEG。

→ 改成**固定步长 + 追平**，并把 `next_disp` 初始化成解码原点 `t0`
（**不是 0.0**，否则追平循环会跑天文数字次）：

```python
next_disp = t0                      # 与解码同源
...
if disp_period <= 0 or now >= next_disp:
    next_disp += disp_period
    while next_disp <= now:
        next_disp += disp_period    # 追平，防止掉帧后一直欠账
```

修复后 1 路 UI 推送 15.6 → **25.0fps**，6 路 15.5 → **24.9fps**。

> 附带纠正一条被继承的旧注释：老解码循环写「`cv2.imencode` 在 RK3588 上
> 15~100ms/帧」是**错的** —— 384x216 实测只要 **0.77ms/次**。那个数字测的
> 是另一档尺寸/负载，却一直被当成 `display_fps=2` 的依据。

### 踩过的坑：`wait_idle()` 用 `Event.wait()` → 忙等烧核 + 饿死解码

**症状**：不启动检测时 25fps 流畅播放；**一点「启动检测」画面就一卡一卡**。
多路更夸张：1→16 路的合计吞吐全部塌到 ~34fps（与路数无关）。

**根因**（`detector_base.py::wait_idle`）：

```python
while True:
    if not self._pending.is_set():
        return True
    ...
    self._pending.wait(min(0.05, remain))   # ← event 已 set 时是空操作！
```

`threading.Event.wait()` 的语义是「阻塞到**被 set** 为止」，它**不检查
当前状态** —— event 已经是 set 时立即返回。而本方法的正常语义恰恰是
「`_pending` 已 set = 正在推理」，所以整个 `while` 退化成**忙等**：
每次推理的那 1.9~2.2 秒里，轮转线程 100% 空转一个核。

**为什么表现成"解码变慢"而不是"CPU 变高"**：忙等的 Python 线程会持续
抢 GIL。解码线程 `read0()` 是原生代码（GIL 已释放），但每次调用之间都要
拿 GIL，被轮转线程挤到只能偶尔拿到 → 解码被饿死，而它们**没在跑**所以
不耗 CPU，进程总 CPU 看起来几乎没变。这就是之前误判成
「NPU 推理抢 DDR 带宽 / 硬件固有」的原因 —— 证据全部指向「在等」而非
「在抢」，但方向被带偏了。

**诊断手法**（下次同类问题照搬）：把 `/proc/<pid>/task/*/stat` 的
utime+stime 差分和 `py-spy dump` 的 **tid** 对齐，就能直接看出是谁在烧：

```
高 CPU 线程: tid=81098 (92.1%)
--- tid=81098 栈顶 ---
    wait_idle (app/core/llm/detector_base.py:1552)
    _rr_loop (app/core/video/multi_session.py:679)
```

**修复**：改成 `time.sleep(min(0.005, remain))`。`time.sleep` **释放
GIL**，这是与忙等的本质区别；5ms 轮询相对 0.85~1.9s 的推理可忽略。

> 该方法是多路轮转**专用**（单路两页从不调用），所以这个改动对
> `video_detection` / `video_target_localization` 零行为变化。

### 踩过的坑：轮转线程 stop→start 会泄漏成两个线程

**症状**：反复「停止检测 → 启动检测」几轮后，轮转顺序乱成
`1,2,1,3,4,2`，提交频率翻倍。`py-spy dump | grep -c mc-vlm-rr` 打出 **2**。

**根因**：线程退出判据写成了「引用是不是 None」：

```python
def stop_round_robin(self):
    self._rr_thread = None          # ← 只清引用，不等线程退
def start_round_robin(self):
    if self._rr_thread is not None and self._rr_thread.is_alive(): return
    self._rr_thread = Thread(...); self._rr_thread.start()
# 循环里的退出判据：
if self._rr_thread is None: return
```

stop 把引用清成 None，但线程此刻正阻塞在 `wait_idle()`（单路 ~2s）。
这期间又 start：新线程把引用换成**新对象** → 旧线程的
`if self._rr_thread is None` 永远不成立 → **旧线程永不退出**，两个线程
一起轮转。

**修复**两处：

1. 判据改成**线程身份**：`if self._rr_thread is not threading.current_thread(): return`
   —— 被 stop 摘掉或被新一轮取代的线程都会自己退出。
2. 判据**每路都要查**，不能只放在 `while` 顶部：一轮 N 路 × ~2s = 十几秒，
   只在循环外查的话旧线程要跑完整轮才退出，那段时间两个线程同时提交。

`start_round_robin` 相应改成无条件新建 + 先换引用（而不是「旧线程活着就
直接 return」），保证任何时刻只有一个线程拥有轮转权。

实测反复三轮：`mc-vlm-rr` 线程数恒为 1，序列严格
`1,2,3,4,5,0,1,2,3,4,5,0`，每路解码 24.7~24.9fps，0 跳过 0 错误。

### 回归
### 踩过的坑：`close()` 绝不能清订阅者名单（症状极具迷惑性）

**症状**：宫格和左上角 `5fps` 标签都正常，就是**没有画面**；单路也一样；
硬刷新无效。

**根因**：`MultiChannelSession.open()` 开头调 `self.close()`（换源=先拆后建）。
而 `close()` 结尾原本调了 `_drain_subs()` → `self._subscribers.clear()`，
把**所有已连接的 WebSocket 订阅队列从广播名单里抹掉**。WS 协程还活着、
还在 `poll` 自己那条队列，但 `publish_frame()` 遍历名单时已经找不到它了 ——
于是**一帧都收不到**。

迷惑性在于：状态快照是 WS 里 ~1Hz 直接调 `session.snapshot()` 推的
（不经过订阅队列），所以 **fps 标签照常跳动**，只有帧断了。

**修复**：`close()` 只清空每条队列里**残留的旧帧**，
**不动 `_subscribers` 名单**；名单只在会话对象真正被丢弃时清
（`reset_multi_session()`）。`/close` 端点同样不能清 —— 用户「关闭 →
再打开」是同一个坑。

**诊断这类问题的通用判据**：*标签/文字状态在动、只有二进制载荷不动*
→ 几乎一定是订阅/连接层被摘掉，而不是渲染或解码。验证手段是
**先连 WS 再调 open**（用户的真实时序）；如果改成「先 open 再连 WS」
就正常，说明是订阅被重置，不是服务端没数据。

配套的前端防御：`ensureGrid()` 改为**按 idx 增量复用瓦片**，不再
`grid.innerHTML=""` 全量重建 —— 否则换通道数会把已加载的 `<img>`
连同 `src` 一起丢掉，所有格子退回空白直到下一帧（display_fps 默认 2
→最长 ~500ms）。索引不变的路（换源最常见的情况）画面完全不中断。

> 顺带记一个自伤：`let created = 0` 一开始声明在 `for` 循环**体内**，
> 循环外的 `_diag(...)` 引用不到 → `ReferenceError: created is not defined`
> 打断整个 `updateVtdTiles` 链路（症状同样是「点了没反应」）。
> **改完 JS 一定要用 `pageerror` 钩子跑一遍**，别只看截图。

### 轮转语义：多路下 `every_n` 必须锁死为 1

用户要的轮转语义是**逐路巡检**：N 路各自独立解码播放，轮到第 i 路就取它
**此刻最新的一帧**分析，分析完再取第 i+1 路，第 N 路完回到第 1 路，
如此循环 —— 不是多路同步、不是并发。

实现里有两个坑，都会让它「看起来在轮转、其实一帧没推理」：

**坑 1：`every_n` 在 `submit_frame()` 里是硬门槛**（`detector_base.py:1515`）：

```python
self._frame_count += 1
if self._frame_count % self._every_n != 0:
    return          # ← 直接 return，连 _pending 都不设
```

这个「每 N 帧推一次」是给**单路**页面的节流。多路模式下节流职责已经由
轮转调度器承担（本身就是一路一帧）。若沿用默认 `every_n=30`，则
**30 次轮转里只有 1 次真进队列**，其余 29 次 `wait_idle()` 因 `_pending`
从未被 set 而**亚毫秒返回 True** → 轮转退化成 15 次/秒空转，烧满一个核，
而 `vlm.round` 还在涨，看起来一切正常。

→ `api.py:set_prompt()` 对多路页强制 `every_n=1`。
单路两页不受影响（它们仍按用户填的 every_n 节流）。

**坑 2：`wait_idle()` 返回 True 不等于「推理过了」**。它只看 `_pending`
是否清零，而异常路径（`detector_base.py:2781`）也会清 `_pending`。
所以 `_rr_loop` 里额外用 `dt >= RR_MIN_INFER_MS(100ms)` 判定：
亚毫秒返回 = 这一帧根本没进队列 → 计入连续空转并**退避**，且不计入
`round`。正常单路推理 850~1900ms，远高于该阈值，不会误判。

> 判据：*`round` 在涨、`last_infer_ms` 恒为 0、事件数不涨* = 空转，
> 去看 `every_n` 和 detector 是否 enabled。

### 真机实测（6 路，本机 RK3588，720p 素材）

| 指标 | 值 |
|---|---|
| `current` 轮转序列 | `0,1,2,3,4,5,0,1,2,3,4,5…` 严格每轮从第 1 路开始 |
| 单路推理 | 1910ms（6 路一轮 ≈ 11.5s） |
| 「轮转间隔 3s」实测 | 相邻路 4.9~6.0s = 推理 ~2s + 间隔 3s |
| 事件覆盖 | 6 路全覆盖 |
| 解码错误 | 0 |

「解码不受推理影响」是这套设计成立的前提。修掉 `wait_idle()` 忙等后
（见上文「踩过的坑：wait_idle 用 Event.wait」），实测（720P 源）：

| 路数 | 不启动检测 | 启动检测中 | 主进程 CPU |
|---|---|---|---|
| 1 | 每路 24.9 | **每路 24.9** | 30.0% → 32.8% |
| 6 | 合计 148.8 | **合计 148.5** | — |
| 16 | 合计 398.4 | **合计 396.9** | — |

修复前同样条件下分别是 19.3 / 33.5 / 34.9 fps（主进程 112%），
即**合计吞吐与路数无关地塌到 ~34fps**。现在开检测前后完全一致。

## 32 路帧率：RGA 是唯一瓶颈（v0.24）

**先读这节的结论再看任何帧率数字**：32 路 1280x720@25 的帧率**只有一个
真正的约束 —— RGA 每帧被调用几次**。其他所有现象（GIL、事件循环、WS
协议、压缩）都是它的下游表现或测量噪声。

### 判据：RGA 次/帧 是那个唯一自变量

用 `/proc/interrupts` 的 rga3 中断数 ÷ 解码帧数，就能直接读出「每帧打了
几次 RGA」：

| 配置（32 路 / 1280x720@25 / 1 客户端） | RGA 次/帧 | 解码 fps/路 | 客户端实收 fps/路 |
|---|---|---|---|
| 基线 `1a7ddab`（判据在轮转线程里按需做） | **1.000** | **24.54** | **19.08** |
| `71009c5` 干净（`fbf1f79` 之后） | **2.000** | **9.28** | 8.97 |
| `71009c5` + 判据每 25 帧采一次 | 1.040 | **24.23** | **18.83** |

每格 3 轮取中位、每轮重新 `open`、极差 1~6%。**RGA 次/帧 1.000 → 2.000，
解码就塌掉 62%**。反过来把判据挪回按需做，吞吐立刻回到基线水平。

### 根因：`fbf1f79` 把判据搬进解码循环

```
fbf1f79 feat(video): per-channel 判据 — observe 每帧打分 + 按路 configure_judge
```

该 commit 的成本评估是：

> 成本：RGA 缩到 judge.long_side（默认 160）+ cv2 灰度 + gscore 实测
> **< 0.5ms/帧/路**。N=32 路合计 ~16ms/s，**可忽略**（已经在 decode 热路径上）。

**「可忽略」是错的，而且错法有代表性**：

1. 0.5ms 是**单线程微基准**。RGA 是**全机串行队列**且 wdnav 里每个调用都取
   同一把进程级全局锁（`wdnav.rga._rga_lock`），32 线程并发时单次中位
   **17.4ms**（p95 66.8ms）—— **34 倍放大**。
2. 真正该算的不是「ms 数」，而是**每帧多打了几次 RGA**。全局串行队列上，
   1 次 → 2 次就是吞吐腰斩。
3. 「已经在 decode 热路径上」是最危险的一句：热路径上的额外全局串行资源
   调用，代价**不由这条路径自己的耗时决定**。

> 与本文件已有的两条同类教训并读：
> 「RGA 只占 0.002ms」（拿线程 CPU 时间当墙钟延迟）、
> 「拿线程 CPU 时间当墙钟延迟」。**这是第三次栽在同一类估算上。**

### 修复：判据缩略图按需做，不要每帧做

`FrameJudge.should_sample(pos)` 门控（`frame_judge.py`），解码循环在
**动手缩之前**问它。间隔 25 帧 → RGA 1.000 → 1.040 次/帧。

> 门控必须问在 RGA 之前 —— 问晚了就不是「少算一次分」，而是白打一次 RGA。

**但这不是最干净的形态**：基线 `1a7ddab` 是让**轮转线程**在真正要送检时
才缩图（`ch.snapshot_gray_for_judge`），天然每路每次推理只多 1 次 RGA。
`fbf1f79` 为了让「未启动检测时浮框也能刷新」把打分搬进解码循环，才需要
这个采样间隔。两种形态的取舍：

* **按需（基线形态）**：RGA 最省（1.000 次/帧），但浮框/曲线在未启动检测
  时不刷新。
* **解码线程采样（现状）**：1.040 次/帧，浮框 1Hz 刷新，代价 4%。

**注意副作用**：采样间隔 25 帧意味着 `_last_luma` 只在采样点推进，两次
采样间累积 25 帧运动 → **gscore 分数整体偏高**，同阈值下更容易判「变化」。
单路 `VideoSession` 仍逐帧采样，**单路与多路刻度已不一致**。若要恢复
刻度一致，得把「每 N 帧一次」这个 N 做成可配置并同步到单路。

### 第二个瓶颈：显示节流按时间做，32 路下白丢 34% 的帧

修完 RGA 之后，四段对齐量出来的留存是 **83.2%**（解码 779.3 → 广播 648.5），
而 `pub_drops = 0` —— 帧不是被订阅队列丢的，是**根本没进广播**。

在解码循环里加三个计数器（`_disp_skip` 节流未到点 / `_disp_err` 编码失败 /
`_pub_exc` 推流抛异常）一测就现形：

```
_disp_skip (节流跳过)  248.1/s   <- 占解码的 34%
_disp_err             0.0/s
_pub_exc              0.0/s
```

**为什么按时间节流在这里必然错**（两种写法都错，且都是结构性的）：

* 滑动 `next_disp = now + disp_period`：下一帧要发得满足
  `body_{k+1} >= body_k`，body 一抖动就隔帧不成立 —— 这就是本文件早先记的
  「请求 25fps 只推出 15.6fps」。
* 固定步长从 `t0` 推 `t0 + (m+1)*period`：解码循环每轮的**检查时刻**是
  `t0 + k*period + body`，比理想槽点晚一个 `body`。`body < period` 时
  **每一帧都落在下一个槽点之前**，于是被全部跳掉。实测 32 路
  `body ≈ 44.5ms` vs `period ≈ 41.0ms`，跳 34%；body 抖动到小于 period 时跳
  50%。

两条链只差一个 `body` 偏移，相位永远对不齐 —— 时间阈值在这里是错的工具。

**改成按帧数**：`disp_every = max(1, round(decode_fps / display_fps))`，循环里
判 `self.frames % disp_every == 0`。解码被 throttle 限死在 `decode_fps`，
所以「每 N 帧发一次」天然就是 `display_fps`，且没有相位问题。
本仓库 `derive_decode_fps` / `derive_display_fps` 用的是同一个预算
（`MAX_TOTAL_DECODE_FPS == MAX_TOTAL_DISPLAY_FPS == 780`），所以
`disp_every` 恒为 1 —— **原来那个时间节流器从来只丢帧、从不节流**。

修复后 `_disp_skip` 归零，四段对齐：

| | 修复前 | 修复后 |
|---|---|---|
| ① 解码 | 779.3/s（24.35） | 783/s（24.45） |
| ② 广播 | 648.5/s（20.26），留存 83.2% | **783/s（24.45），留存 100.0%** |
| ③ WS 发出 | 648.3/s | 783/s，100.0% |
| ④ 客户端实收 | 649.4/s（**20.29**） | **776.7/s（24.27）**，99.8% |

连跑三轮（客户端实收）：24.43 / 24.27 / 24.27 fps/路。
相对基线 `1a7ddab` 的 19.08 提升 **27%**，达到 25fps 目标的 **97%**。

> 教训：**「节流器」要先问它到底在节流什么**。decode_fps 与 display_fps
> 相等时，任何基于「距上次多久」的时间判断都只会丢帧，不会节流。

### gscore 是**两套规则**，按「向导模式 / 应用页面」切换

同一个 `FrameJudge` 在两种场景下的**比较对象不一样**，混用会算出没意义的
分数：

| | 向导模式（未启动检测） | 应用页面（检测在跑） |
|---|---|---|
| 谁打分 | 解码线程 | **轮转线程**（送检那一刻） |
| 帧A（参照） | 上一次判变的**采样**帧 | **上一次送检的帧** |
| 帧B | 本次**采样**帧 | 当前帧（即将送检的那一帧） |
| 节奏 | 每 `sample_every`(=25) 帧一次 | 每路每次推理一次 |
| 目的 | 让浮框/曲线动起来 | 判「这一轮要不要送 VLM」 |

**切换靠 `Channel.rr_owns_judge` 标志**（`multi_session._set_judge_owner`）：

* `False` → 解码循环里 `observe()`（受 `should_sample` 门控，问在 RGA 之前）
* `True`  → `_run_one_channel` 里 `observe()`（轮转线程，送检前一刻）

**切换时必须把参照帧丢掉重建**（`FrameJudge.reset()`）：两种模式的参照帧
语义不同，混用会算出「上次的采样帧 vs 本次的送检帧」。曲线历史点也一起清
—— 那些点也是按旧口径算的。

`start_round_robin()` 切到 True，`stop_round_robin()` 切回 False；
`start` 内部调 `stop_round_robin(judge_owner=False)` —— 否则归属会来回
toggle，中间解码线程有机会用向导模式语义抢建一个参照帧，白重置两遍。

实测（32 路，1280x720@25，进程内 + 桩 detector）：

| | 解码 fps/路 | RGA 次/帧 | 判据 evaluated/s | 送检/s |
|---|---|---|---|---|
| 向导模式 | 23.11 | 1.040 | 29.9（= 解码帧/24.7） | 0 |
| 应用页面 | **24.00** | **1.012** | 4.7 | 4.3 |
| 回到向导 | 23.34 | 1.040 | 29.7（= 解码帧/25.2） | 0 |

`evaluated/送检 = 1.096 ≈ 1.0` —— **每次推理恰好对应一次打分**，这就是
「帧A = 上一次送检的帧」的直接体现。

**附带收益**：应用页面比向导模式**更快**（24.00 vs 23.11 fps/路），因为
解码线程不再为判据缩缩略图，RGA 从 1.040 回到 1.012 次/帧。

> 代价（要知道）：应用页面里分数曲线的刷新节奏 = 轮转周期。32 路、真实
> 模型单次推理 ~1.9s 时，**一路的分数约 61s 才更新一次**。这是「按送检
> 间隔算分」的必然结果，不是 bug。

> `FrameJudge._lock` 必须是 **RLock** 不是 Lock：切换瞬间两个线程可能各进
> 一次 `observe()`，而 `observe()` 尾部要调 `_commit()`（它自己
> `with self._lock`）→ 普通 Lock 会死锁。

### 测量纪律（本节所有数字都踩过的坑）

1. **一定要测「干净 HEAD」。** 中途我一直在**已经打了补丁**的代码上量，
   于是「RGA 1.040 次/帧」被当成了 HEAD 的现状，**从没看过 2.000**，
   整轮分析因此跑偏。**改性能问题前先量未修改的基线。**
2. **同一配置单次跑没有意义。** 同一格子单次能差 23%（778 vs 630），
   方向都可能反过来。**每格 ≥3 轮取中位**，每轮重新 `open`、统一预热。
3. **py-spy 的栈顶不等于耗时归属。** 解码线程 92% 停在
   `wdnav/rga.py` 的 `wrap_fd` / `wrap_virtual` 上，看着像「导入慢」，
   实际是**排队等 `_rga_lock`**（锁被正在做 1.3ms 硬件 resize 的线程持着）。
   看到 wrap_* 就要想到锁竞争。
4. **进程 CPU 低 ≠ 算力够。** 32 路满载时进程只有 ~190%/800%，因为线程
   阻塞在锁上而不是空转。
5. **仪表自己会骗人**（都踩过）：
   * `pub_drops` 漏统计正常的 drop-oldest 路径 → 显示 `0.0/s`，
     实际每秒丢一两百帧。
   * `self._pub_frames += 1` 在 32 线程上**非原子**（LOAD/ADD/STORE 可被
     抢占）→ 770 次/秒丢 7~15% 更新，凭空造出 15% 的「广播缺口」。
     现按通道分槽（`_pub_per_ch[idx]`），每通道解码线程是唯一写者。
   * CDP 的 `page.on("websocket") framereceived` 在渲染线程饱和时**欠计**。
   * 探针跑 `win+8` 秒却除以 `win` → 帧率凭空多 50%。
   * **测试客户端自己在丢帧**：`websockets` 客户端的 `max_queue` 默认 **16**，
     队列满时**直接丢弃**收到的消息、不施加背压。实测同一个服务端，客户端
     实收在 586/s 与 778/s 之间**双峰跳动**（极差 63%），而服务端 `ws.sent`
     稳定 783/s。`max_queue=None` 后三轮全部稳定在 776~782/s。
     **看到「服务端发出 X、客户端收到 Y<X」且服务端无丢弃计数时，先怀疑
     测量客户端的 `max_queue`。**
   * **不同来源的增量不能相减**。曾把服务端 `ws.sent`（状态消息里自报）
     和客户端二进制计数（另一条时间轴）相除，算出「34% 缺口」并据此追了
     很久。正确做法：四个计数全部从**同一条连接上同一条 status 序列**读出，
     时间戳由客户端自己打。

### permessage-deflate：**不要关**（反直觉，但实测如此）

曾经的判断是「JPEG 本来就是 deflate 产物，再压一遍纯浪费 CPU」，于是加
了 `ws_per_message_deflate=False`。**实测是反的**：

| 配置 | deflate 开 | deflate 关 |
|---|---|---|
| 基线 `1a7ddab` 解码 fps/路 | **24.54** | 19.62 |
| 基线 `1a7ddab` 客户端实收 | **19.08** | 16.76 |
| `71009c5`+判据修复 解码 fps/路 | **24.23** | 18.67 |
| `71009c5`+判据修复 客户端实收 | **18.83** | 15.96 |

**关掉反而慢 23%，在两个互不相干的 commit 上方向一致。** deflate 对 JPEG
几乎没有体积收益（实测 17.24 vs 16.72 KB/帧，差异在噪声内），所以**不是
「省带宽」能解释的，机制尚未查清**。在查清之前**保持默认开启**，别加
`--ws-per-message-deflate False`（systemd 单元和 `run.py` 都别加）。

### sys.setswitchinterval：**当前不设**

`wdnav.rga` 的 ctypes 调用执行期间释放 GIL，而「释放 `_rga_lock`」这个
动作本身要先抢回 GIL，默认 5ms 的 switchinterval 会让等待方最长等 5ms
（期间另外 31 个解码线程全卡在锁上）。于是有调小它的动机。

**但在正确配置下实测毫无差别**（deflate 开、32 路、1 客户端）：

```
5ms（默认）→ 解码 771.2 帧/秒、客户端实收 592.9
0.0005      → 解码 770.5 帧/秒、客户端实收 597.3
```

所以 `app/main.py` **默认不设**（全局进程级设置，没实测收益就不动），
需要 A/B 时设环境变量 `WDNWEBUI_GIL_SWITCH_INTERVAL`。

> 那次「+7%」的读数来自 deflate 被关掉的配置，而那个配置后来被证明本身
> 慢 23%。**别拿单次 A/B 下结论。**

### 还剩的差距：客户端实收 ~18.8 fps/路 < 解码 24.2 fps/路

解码已经到 24.2~24.5 fps/路（`MAX_TOTAL_DECODE_FPS/32 = 24.375` 的 99%），
**但客户端实收只有 18.8**（留存 78%）。这一段还没定位，已排除的：

* 订阅队列满被丢：`pub_drops = 0`。
* WS 循环自身：实测只占墙钟 **0.09%**（事件轮询 0.1ms、快照 0.1ms、
  取帧 22ms、WS 写 1ms / 24 秒）。
* 会话快照抢锁：`session.snapshot()` 实测 0.1ms/24s，`get_section("app")`
  的 JSON 往返只有 14µs。
* GIL 争用：合成基准（32 个「解码线程」+ 2000 次/秒 16KB socket send）
  在 5ms/1ms/0.5ms 三档 switchinterval 下吞吐**完全不变**。
* 环回假象：loopback 与走网卡（172.16.9.173）数字一致（446.8 vs 444.8）。

下一步该查的方向：**客户端实收是 602.7/s，服务端解码 775.5/s，中间 173
帧/秒去哪儿了** —— 浏览器侧另有独立开销（32 格宫格在本机 headless Chromium
上要吃 **321% CPU**，同期服务端只有 158.8%），所以本机 headless 量到的
「浏览器里看到多少 fps」是**下界**；真实客户端在 `172.16.9.12` 上不占
服务端的核。

## App 插件系统 (v0.4 卡片架构)

v0.4 起,wdnwebui 走**卡片插件架构**——新增应用不再需要改 `app/main.py`
或 `app/api/__init__.py`,只需在 `app/apps/<card_id>/` 下放一个完整目录。

### 默认 UI 形态:应用面板 (`#apps`)

v0.4 启动后默认进入**应用面板**(`#apps`),而非之前的某个具体应用。
该页是一个卡片网格(grid),由 `app/static/core/framework.js:renderAppPanel()`
读取 `window.__APP_MANIFESTS__.cards` 渲染:
- 顺序按 `manifest.display_order` 升序(小→上左);缺省值 `9999`,
  相同 `display_order` 时按 id 字典序兜底。
- `status === "ok"` 才显示;`status === "fatal"` / `template_missing`
  的卡片不会出现在网格(直接 `#/<id>` URL 仍可访问)。
- `hidden: true` 的卡片从网格消失但代码 / API / 静态资源 / 直接 URL
  都仍可用,用于"先下线 / 等版本升级"的过渡期 —— 比 `remove_card.py`
  安全,反序列化时想恢复只需把 `hidden` 置 `false`。
- 7 张卡片都从同一份 `__APP_MANIFESTS__` JSON 渲染,该 JSON 由后端
  `_assemble_index_html()` 在每次 `/` 请求时动态拼装 ——
  `/static/index.html.template` 里 3 个占位符:`{% card_slot "<id>" %}`
  (替换为各 app 的 `static/template.html` 片段)、`{{ card_css_links }}`
  (拼装各 app 的 `<link rel="stylesheet">`)、`{{ app_manifests }}`
  (替换为 JSON dump)。

### 侧栏 7 项(左 nav)

| `#hash` | 显示名 | 来源 |
|---|---|---|
| `#apps` | 应用面板 | 内嵌在 `index.html.template`(默认 landing) |
| `#system-monitor` | 系统信息 | `app/system/api/system_monitor.py` |
| `#base-config` | 系统配置 | `app/system/api/config.py` |
| `#compute-config` | 算力配置 | `app/system/api/compute.py` |
| `#discovery` | 注册发现 | `app/system/api/system_monitor.py` (DDS 心跳) |
| `#im-bridge` | 应用对接(微信桥接) | `app/system/api/im_bridge.py` |
| `#system-logs` | 系统日志 | `app/system/api/system_logs.py` |

`#/<card_id>`(7 张卡片)不在侧栏,在 `#apps` 网格里点入。

### Back-home 按钮

每个 demo 卡片(manifest 含 `demo_guard` 块)页面顶栏自动注入
`< 返回首页` 按钮(`injectBackHomeButtons()`),点击先调
`DemoGuard.stopAndClear(currentPage())` 再切回 `#apps`。

### 卡片目录模板

```
app/apps/<card_id>/
├── manifest.json     # 插件声明(必填)
├── api.py            # FastAPI router(必填)
├── service.py        # 单例状态机 + 业务逻辑(必填,如有后端)
├── __init__.py       # register_api(app) / register_shutdown()(必填)
└── static/
    ├── template.html # <section id="page-<card_id>"> 片段
    ├── app.js        # window.ImApp_<card_id> 模块
    ├── app.css       # 该 app 私有样式
    ├── preview.mp4   # 应用卡片预览(可选)
    └── wizard.json   # 向导配置;存在则启用 wizard 模式(可选)
```

### manifest.json 字段

见 `app/core/manifest.py` 的 `CardManifest` dataclass。关键字段:

| 字段 | 必填 | 含义 |
|---|---|---|
| `id` | ✓ | 全局唯一(`a-z0-9-`)。同时也作为 sidebar / static / wizard 命名空间。 |
| `title` | ✓ | UI 显示名 |
| `category` | ✓ | 应用分组:`video` / `chat` / `audio` |
| `software_capabilities` | ✓ | 引用 `MODEL_PRESETS` 的 `swcap_*` 列表 |
| `hardware_capabilities` | | 引用 `HARDWARE_CAPABILITIES` 的 `hwcap_*` |
| `backend.module` | ✓ | Python 模块路径,默认 `app.apps.<id>` |
| `backend.register_api` / `register_shutdown` | ✓ | 入口函数名 |
| `frontend.template` / `frontend.js` / `frontend.css` | | 资源 URL(相对 `/static/apps/<id>/`) |
| `prompts.schema_class` | | wdnai schema class 名(`VLMDoc` / `TargetDoc` / `LocatorDoc` 等) |
| `prompts.editable_fields` | | 暴露给用户在 系统配置 → App 提示词模板 编辑的字段 |
| `prompts.prompt_template` | | 应用预设默认 prompt(用户可覆盖) |
| `requires_core` | | 核心依赖列表;启动时校验 import 可用性 |
| `demo_guard.active_state_keys` | | DemoGuard 判定"页面在跑后台"的字段路径 |
| `demo_guard.stop_endpoints` | | 用户确认离开时调用的 stop 接口 |
| `wizard_config` | | 静态文件名(如 `"wizard.json"`);存在则启用 wizard |
| `display_order` | | `#apps` 网格排序(升序,小→上左,缺省 `9999`,相同按 id 字典序) |
| `hidden` | | `true` 时从 `#apps` 网格消失(代码/API/直接 URL 仍可用) |

### 启动期装载流程 (`app/main.py:create_app()`)

1. `discover_cards()` 扫描 `app/apps/*/manifest.json`,逐条校验后返回
   `List[CardManifest]`(校验失败 → 卡片标 `status="fatal"` 但不阻断其他)。
2. 对每张卡片调 `register_api(m, app)`(来自 `app/core/manifest.py`)→
   `importlib.import_module(m.backend.module)` → 调 `register_api(app)`
   `app.include_router(router, prefix="/api/<id>")`。
3. 调 `register_shutdown()` 注册 app 关闭时的清理 hook。
4. `app.mount("/static/apps/<id>", StaticFiles(...))` 装载每张卡的静态资源。
5. `_assemble_index_html(discover_cards())` 读 `app/static/index.html.template`,
   按 `display_order` 拼装每张卡的 `<template>` 片段、`app.js` `<script>`、
   `app.css` `<link>`,输出最终 `index.html`(动态路径,运行内存)。
6. 调 `system_register_all(app)` 装载系统级 API(算力配置、配置、监控、
   im_bridge、power、coprocessor、versions、fs_browser、system_logs)。
7. **不**直接注册 surveillance(已下线,见 im_bridge 章节)。

### 零耦合铁律(`app/apps/__init__.py` 强制)

- 卡片**只能** import `app.core.*`;**禁止** import `app.system.*` 或
  跨卡片 import。违规会导致循环依赖 + 系统模块被卡片挟持。
- 系统模块不依赖卡片(im_bridge 用 `app.system.im_bridge.video_search`
  注册 `VideoSearch` 工具到 `app.core.tools.registry`,不依赖任何卡片)。
- **例外**:`app.system.im_bridge` 在 `register_all` 时主动调
  `get_tool_registry().register(... VideoSearch ...)`,把微信视频检索
  工具装入全局注册中心 —— 这是允许的单向注入(im_bridge → core tools)。

### 当前卡片清单(v0.4.0)

| `id` | capability | 后端入口 |
|---|---|---|
| `audio-analysis` | `swcap_audio_understand` | `app.apps.audio_analysis` |
| `text-chat` | `swcap_text_chat` | `app.apps.text_chat` |
| `tts` | `swcap_audio_tts` | `app.apps.tts` |
| `video-analysis-search` | `swcap_video_describe` + `swcap_video_retrieval` | `app.apps.video_analysis_search`（分析 + 检索双路由） |
| `video-detection` | `swcap_video_describe` | `app.apps.video_detection` (+ `tts_alert` 工具) |
| `video-target-detection` | `swcap_video_describe` | `app.apps.video_target_detection` (TargetDoc) |
| `video-target-localization` | `swcap_grounding` | `app.apps.video_target_localization` (LocatorDoc) |

`scene-threshold-tuner`(曾用名「阈值标定」,显示名「视频画面变更阈值测试」)
已在 v0.18 **整卡下线**:它的能力(帧间差异判定)不再需要单独一张卡 ——
`app/core/video/frame_judge.py` 把判据做成了 4 张视频卡共用的底层能力,
每张卡的视频源配置区直接调它,曲线也搬进了向导(见「帧判据」段)。
标定台只剩历史价值(它当初用来定下的默认值 6.0 / 长边 160 至今仍是
`FrameJudge` 的默认),代码已删除。

新增卡片示例(如加一个"图像 OCR"应用):只需 `mkdir app/apps/ocr && 写 manifest.json + api.py + service.py + __init__.py + static/`,`display_order` 自动出现在侧栏,**不动** `app/main.py` 或任何 core 模块。

## 帧判据(v0.18 下沉到 4 张视频卡)

「这一帧要不要送去跑推理」曾经有两套:三张卡的 detector 各自 `every_n` 跳帧,
VAS 另有一个二值的「自动判帧」。现在**只有一份实现**:
`app/core/video/frame_judge.py:FrameJudge`,四张卡共用。

| 位置 | 说明 |
|---|---|
| 算法原语 | `wdnav.scene`（RGA NV12-Y 缩略图 + 可注册度量） |
| 判据策略 | `app/core/video/frame_judge.py`（模式 / 阈值 / 兜底间隔 / 分数环形缓冲） |
| vd·vtd·vtl 接线 | 三张卡都 → `app/core/video/multi_session.py`（按 key `"vd"` / `"vtd"` / `"vtl"` 注册的多路会话）。判据实现在 `frame_judge.py` 一份，每路各持一个 `FrameJudge` |
| VAS 接线 | `app/apps/video_analysis_search/service.py`（自管理 decode 循环，判据挂在它自己身上） |
| HTTP | `app/core/video/api.py` → `/api/video/judge*`（三张卡共用）；VAS 走 `/api/video-analysis-search/judge*` |
| 前端 | `app/static/core/frame-judge.{js,css}`，四张卡各 mount 一个实例（按 prefix 索引） |

**模式即算法名,v0.21 起放行两个:`gscore`(推荐) 与 `conc`(对比/待下架)**:
用户可选 `manual`(手动跳帧,逐位保持历史 `pos % n == 0`,零 RGA 开销) +
自动判帧。`wdnav.scene` 里仍注册着 `sad` / `ncc` / `z_mad`,但
`frame_judge.EXPOSED_METRICS = ("gscore", "conc")` **只放行这两个**。

两个算法的实现都在 **`app/core/video/frame_judge.py`**(wdnwebui 侧),通过 SDK
公开的 `wdnav.scene.register_metric` 注册进注册表 —— **不改 SDK 源码**:算法
差异属于调用方,放进 SDK 只能让每个项目各分叉一份。

```
conc(a,b)  = max(8x8块内平均绝对差) / max(块均值的90分位, 1.0)
gscore(a,b) = p99( |B - (g·A + c)| )        # A/B 是 8x8 块均值图
              其中 (g,c) 是 A↔B 的**双向**最小二乘仿射拟合
```

`conc` 测"变化有多**集中**在少数几个块上";`gscore` 先把**整幅亮度/对比度
变化**用仿射拟合整个吃掉,再看剩下的**结构残差**。

**v0.21 换默认算法的原因:conc 对大范围变化结构性失明**。它是**比值**,分子
分母在大范围变化时同步抬升、比值回到 1。真实 1080p 监控流(`rtsp://…192.168.169.25`,
700 帧,180x320 缩略图,与线上判据同一管线)上:

| 场景 | conc 绝对响应 | gscore 绝对响应 | conc 底噪 p95 = 11.95 → 分离度 | gscore 底噪 p95 = 0.109 → 分离度 |
|---|---|---|---|---|
| 曝光 +45 灰阶 | 1.00 | ≈0(归一掉) | **0.08x** | 免疫 |
| 对比度 0.5x | 1.17 | 0.33 | 0.10x | 3.0x |
| 镜头全挡(纯色) | 1.73 | 108.7 | **0.14x** | 999x |
| 手挡镜头(半透软边) | 1.79 | 87.0 | **0.15x** | 800x |
| 下半部 +60 灰阶 | 1.79 | 60.0 | **0.15x** | 551x |
| 整幅右移 8px | 1.87 | 85.3 | **0.16x** | 784x |
| 小目标 2.3% | 86.0 | 70.8 | 7.2x | 651x |

conc 在前六个场景全塌在 1.0~1.9,而底噪是 11.95 —— **分离度只有 0.08~0.16
倍,比底噪还低,阈值再怎么调都测不出来**。用户报的"手遮挡摄像头、画面移动
都做了,浮框始终显示 1.7"就是这个:1.7 就是 conc 在大范围变化下的**天花板**。
闯入监控两类都要(人走过去是局部的,手挡镜头/相机被动是全局的),只保 conc
等于把一半场景交给兜底间隔 —— 而兜底间隔实测从来没生效过。

**为什么不是卷积/滤波类方案**(v0.21 做过完整横评,`scripts/bench_conv_judge.py`
+ `scripts/bench_denoise_judge.py`,同一流同一管线):

* **梯度类卷积(LoG / DoG / Sobel)全线失败** —— 几何平均分离度 2.0~2.5x,
  最差场景 **0.5x**(比底噪还低)。它们是**高通**,而这条流的底噪是 H.264 编码
  残差,主要就在高频,高通不但压不住噪声还把它放大。
* **空间滤波(中值/双边/高斯/形态学)压不动底噪** —— 判据是「降噪比 ÷ 信号
  保留比」,**降噪比全部 ≤1**(中值3x3 = 0.90x、形态学开 = 0.92x、双边 5x5 =
  0.98x)。编码残差是**时间域**抖动,空间滤波管不了。双边滤波还慢到 4.26ms。
* **光流法也不行** —— Farnebach 幅值 11.7x / 最差 0.5x,且 **17.06ms**,
  超预算 20 倍;配准残差 1.6x。
* **文献也一致**:国内(CSDN)DVR-Scan 类的关键参数是"阈值、最小区域、缓冲
  时间",《一种基于时空联合的实时视频降噪算法》明确写"**避免了空域降噪
  带来的图像闪烁问题**"而改用帧间差分+调阈值;国外 shot boundary detection
  领域承认 "Illumination changes, camera motion, fast-moving objects reduce
  accuracy",解法是 **local adaptive threshold**。
* **CNN 特征距离在本机根本跑不了**:整机没有任何 `.onnx`/`.tflite`/`.rknn`
  权重,也没装 torch/onnxruntime/rknn。而且**成本上也不该** —— 这个项目每 30
  帧就要跑一次 VLM(实测 2.3s/帧),用 CNN backbone 做纯判帧等于为了省一次
  推理先跑一次更轻的推理。判据必须是零学习成本的固定算子。

**gscore 的两个设计点,都是实测踩出来的**:

1. **必须双向拟合,不能只算 `B≈g·A+c`**。单向拟合的常数项取 **B 自己的
   均值**,于是当 B 退化成纯色时(镜头被全挡)残差恒等于 `|B-mean(B)| = 0` ——
   分数直接归零,把"整幅纹理彻底丢失"这个**最大的变化**判成"完全没变"。
   实测单向版在「镜头全挡」得 **0.00**,正是用户报的手挡场景。改成双向
   (A≈g·B+c' 再算一次,取两者残差的大者)后得 **60.36 → 4.8x 底噪**,而
   底噪几乎没变(12.40 → 12.60)。整幅变暗两边都拟合得上,免疫保留。
2. **残差聚合取 p99,不是 mean 也不是 max**(同一条流实测,响应 ÷ 各自底噪):
   `mean` 46.4x / 最差 2.7x(小目标被整幅摊薄)、`max` 9.5x / 最差 1.6x
   (被单像素尖刺主导,底噪从 1.58 抬到 6.15)、**`p99` 25.6x / 最差 2.8x /
   小目标 41.1x**。p99 是拐点。

**为什么下架 z_mad**(实测:真实视频 1200 帧相邻帧 + 合成闯入样本):

| 度量 | 目标得分 | 正常相邻帧 p50 | 结论 |
|---|---|---|---|
| `z_mad`,30% 高的人 | **1.88** | **2.19** | 目标比正常帧中位数还低 → **阈值怎么调都抓不到人** |
| `sad`,30% 高的人 | 0.98 | 2.14 | 同上,且曝光跳变下触发率 100% |
| `ncc` | 0.02 | 0.01 | 取值范围只有 [0,2],与灰阶阈值结构性不兼容 |
| `conc`,5% 面积目标 | 32.5~140.6 | 3.23 | 2.2~9.4 倍余量 |

z_mad/sad 算的是**整幅图**的统计量,而闯入监控要抓的"画面里一小块变了"会被
14400 像素平均摊平。**降阈值救不回来**:σ=4 时噪声底就有 3.21,比目标还高。
这条已固化成 `scripts/verify_frame_judge_metrics.py` 的断言。

三个设计点全是实测逼出来的,改任一常数都要重跑标定:

1. **分母用块均值的 90 分位,不是全图平均**。全图平均**会把目标自己算进去**
   —— `conc ≈ 1/目标面积占比`,目标一超过约 5% 面积就掉到阈值以下(实测 5%
   面积 26.2、9% 面积 17.1,阈值 15 根本过不了)。换成分位数之后分母代表"基线
   水平",5% 面积从 26.2 抬到 140.6,衰减拐点推到约 10% 面积。
   **注意自变量是"面积占比"不是"高度"** —— 早期基准里"30% 高的人"其实只占
   2.25% 面积,把结论做窄了;是回归里 220x220 白块测出 17 分才暴露的。
   (写这类测试时还要注意坐标系:尺寸和坐标必须在同一套分辨率下算,否则
   "0.5% 面积"贴到全分辨率帧上只剩 2x1 个缩略图像素。)
2. **分母地板 1.0 给分数封顶**。conc 是比值,两帧几乎全同时分母趋零会让分数
   炸到几百;而"只有极少数像素变了"在监控里很常见(坏点/编码残差/隔行差)。
   实测无地板时「静止画面 + 8 像素变 1 灰阶」得 **14.00**,正好压在阈值 15
   上 —— 静止监控画面会持续误触发。
3. **比值化是为了对全局变化免疫**。相机微抖 1px 用绝对块差是 34.67(余量只剩
   1.1x,等于每帧都送检),比值化后只剩 1.19(12 倍余量)—— 微抖是整幅一起动,
   分子分母同步抬;人闯入是局部暴涨,只有分子抬。

**conc 的阈值 15 操作点**(`DEFAULT_THRESHOLD_BY_MODE` 历史取值,已下架) ——
仅作历史保留。**v0.22 起** conc 已从 `EXPOSED_METRICS` 移除(对大范围变化
结构性失明,见下),这段阈值标定记录留作"为什么换算法"的复盘。

`DEFAULT_THRESHOLD=6.0` 混用** —— 那是 z_mad 的灰阶口径,套过来几乎每帧都触发):

* 自然相邻帧 p50 3.23 / p95 6.96 / p99 9.40 → 触发率 0.083%
* 目标占画面 0.5%~5% 面积 32.5~140.6 → 全部越过(2.2~9.4 倍)
* 最坏误触 4.69(稀疏微变)/ 1.19(微抖)/ 1.54(σ=16 噪声) → 3~12 倍余量
* 操作区间很宽:15~25 的自然触发率都在 0.1% 以内

**已知边界**:目标面积超过约 10% 画面时分数回落(实测 9% 面积 5.11)。这类目标
不可能是"突然闯入"—— 它一路走近时早就被检出并成为参照帧了,之后的连续变化由
`every_n` 兜底间隔覆盖。`conc` 还**与对比度无关**(对比度 8 与 45 的分数几乎
相同),灰衣服/灰墙同样抓得到。**以及大范围变化全瞎**(见上表)。

**gscore 的阈值是按两个素材一起标的 —— 这是"阈值不可跨素材迁移"的第二次
实证,而且这次是同一个算法内部跨素材**。

最初只在监控流上标得出 5.0,搬到车载视频(`carplant_29132868.mp4`,**整幅
平移**的源)上触发率直接冲到 **31.8%**,
每 3 帧送一次,等于判据没开 —— 仿射拟合吸收不了**平移**,只吸收亮度/对比度。
车载源上阈值扫描:5 → 31.8% / 10 → 15.1% / **15 → 0.67%** / 20 → 0.33%。
监控流上 5.0 看似更合适(底噪 p95 = 0.109,小目标 70.8 → 651× 分离度),
但跨素材 31.8% 触发率证明 5.0 在车载上**直接死**。

**v0.22 起默认改为 10.0** —— 用户拍板,要求更敏感(办公室监控流底噪
p95 = 0.10 量级,阈值 10 触发率 0.00%,小目标 7.1× 余量);实测车载源
15.1% 触发率是**已知素材特性**(整幅平移)而不是算法退化,**用户的相机
是固定监控头,不会触发**。`verify_frame_judge_metrics.py` 的 [7] 段
相应地把"底噪 p95 < 默认阈值"改成"大范围变化分离度 > 底噪"——换素材
不假红。

默认 `every_n = 600`(25fps 下 ≈ 24s 兜底间隔),用户拍板。**长间隔的意图**:
判据主要靠"超阈值立即送"工作,兜底只在画面**长期静止**后兜一次,避免
每隔 30 帧送一次"画面没变"的推理请求浪费 NPU;静止画面 24s 送一次
刚好让 VLM 知道"系统还活着"。

15.0 仍是 gscore 的"双素材标定唯一站得住的点":监控流上底噪 max 余量
12.3 倍、小目标 4.7 倍、手挡 5.8 倍、镜头全挡 7.2 倍,触发率 0.00%;
车载上小目标 0.5% 面积只剩 1.4 倍(偏紧)。当用户**希望对低底噪场景
更激进**(例如抓"小目标 0.5% 面积"或"光照微调"),10.0 在监控流上仍
有 4.7~7.1 倍余量,可用;当用户**希望跨素材稳**,15.0 是更保守的选项。
**但 5.0 不可用**(车载 31.8% 触发率,等于判据没开)—— 不论阈值怎么调,
gscore 对**全局位移**不免疫(仿射吸收不了平移),移动相机上**底噪天然高
一个量级**,必须重标。

⚠ **gscore 与 conc 的历史默认值都是 15.0,但数值相同纯属巧合,刻度含义完全无关**:
conc 是比值(无单位),gscore 是归一化后的块级残差(灰阶)。任何"反正都是 15"的
心智捷径都是错的 —— 判断阈值合不合适只能看**该算法自己**的底噪。
v0.22 起 conc 已下架,gscore 默认改为 10.0(用户拍板,见上)。

**存量配置迁移**:z_mad 从下拉下架了,但用户本地存的判据配置里还有它。
`frame_judge.LEGACY_MODE_ALIASES` 把 `z_mad` / `auto` **迁到 gscore 并把阈值
重置成 gscore 的默认**(见 `_migrate`)。三个理由,少一个都会出事:
* 不能回落 `manual` —— 那等于把用户的自动判帧悄悄关掉,表象是"判据突然不生效
  了",比阈值不准更难排查;
* 不能沿用旧阈值 —— z_mad 的阈值是灰阶口径(线上实测值 1.0),套到 gscore 上
  会几乎每帧都触发(自然相邻帧 p95 就有 12.60),那是比漏检更糟的故障;
* 不能迁到 `conc` —— conc 已经是待下架候选,让存量用户落到一个测出结构性缺陷
  的选项上等于继续吃流量。

**下拉顺序必须按 `EXPOSED_METRICS`,不是 SDK 注册顺序**(`metrics()` 里显式
过滤)。两者不一样:注册顺序是 SDK 自带的 z_mad/sad/ncc 在前、后注册的 conc /
gscore 在后,于是 conc 会排到推荐项 gscore **前面**去 —— 用户在下拉里看到的
第一个应该就是默认推荐,而且 `default_threshold()` 的兜底取的是
`EXPOSED_METRICS[0]`,两边顺序反了会对不上。`verify_vas_change_detector.py`
里有这条断言。

**测试不要把"推荐项是谁"写死**(v0.21 换算法时 3 个脚本一起红)。写死
`== ["conc"]` / `== ["manual","conc"]` / `select_option(..., "conc")` 的地方,
算法换默认后会直接失败,而**那不是回归,是产品改了**。三个脚本现在都从服务端
`/judge/metrics` 取 `metrics[0]` 当推荐项:

**兜底不是无条件送检(用户明确要求)**：`FrameJudge.observe` 里兜底条件是
`gap >= every_n **且** score >= threshold * 0.5`,不是 SDK `FrameDiffer`
那套的 `gap >= max_gap`。静止画面分数恒为 0 < 半阈值,所以**只送首帧**,
之后一直安静;人闯入后站立不动也只送闯入那一帧 —— 画面一样,重跑推理
得到的还是同一个答案,纯徒增一次 NPU 调用。实测(阈值 15 / every_n=30):

| 场景 | 150 帧里判据触发 |
|---|---|
| 纯静止 | **1**(只有首帧) |
| 人闯入后站立不动 | **2**(首帧 + 闯入那帧) |
| 人横向走动 | 144(每帧都有区别) |
| 慢漂移 1 灰阶/帧 | 9(约每 16 帧累积越线) |

"变了但没过阈值"的中间地带(分数落在半阈值~阈值之间)仍由兜底兜住 ——
实测 a/b 两态交替(单步 conc=10.00)时送检落在 [0, 31, 62],间隔 ≈ every_n。
**改这块时注意**:判据暂停后 `_last_pos` 不推进,而兜底量的是"距上次送检
满 N 帧",所以交替序列的相位会让命中落在 every_n 或 every_n+1 —— 断言要
写间隔,写死 30/60 会被相位差打穿。

这条判定是**有意偏离** `FrameDiffer.should_sample` 的(改 `observe` 时
别"顺手改回去"),理由:SDK 那套的出发点是保证结果会刷新,而静止画面送出去
的结果与上一次逐位相同,不存在"漏检"。其余判定(首帧必送、超阈值触发、
异常按送检处理、参照帧只在送检时推进)仍与 SDK 一致。

### 阈值标定**不可跨素材迁移**(v0.20 实测推翻"阈值 15 偏保守")

上面那张"操作点"表是在 `carplant_29132868.mp4`(960x540 **文件**)上标的。
拿它去标定**真实监控流**会大幅偏低。实测同一套代码、同一块板、同一个
`conc`,对 `rtsp://…192.168.169.25/…`(1920x1080 **实时** H.264)抓到 900 帧:

| 比较距离 | 本机实时流 | 标定文件 |
|---|---|---|
| d=1(相邻帧)p50 / p95 / p99 | **4.41 / 19.33 / 32.85** | 3.23 / 6.96 / 9.40 |
| d=5 p50 / p95 | 16.78 / 49.91 | — |
| d=20 p50 / p95 | 32.11 / 86.36 | — |

**相邻帧的 p95 就已经是标定视频 p99 的 2.8 倍。** 1080p 实时流的每帧
纹理/压缩噪声比 960x540 文件高得多,而 `conc` 的分子正是"块内像素级
`mean(|a-b|)`"——它吃的就是这项噪声。所以阈值 15 落到这条流上等于
"每 3 帧就送检一次"。

**自动模式还有一个正反馈,会把上面的数字再放大**:`参照帧只在送检时推进`
→ 触发得越多,参照帧越旧 → 比较距离越大 → 分数越高 → 触发更多。实测
阈值 15 下,相邻帧触发率 10.7%,而**完整 auto 语义**(参照帧按送检推进)
是 **22.4%**,送检间隔 p50 只有 2 帧:

| 阈值 | 相邻帧触发率 | 完整 auto 送检率 | 送检间隔 p50 |
|---|---|---|---|
| 10 | 25.8% | 35.6% | 1 帧 |
| 15 | 10.7% | 22.4% | 2 帧 |
| 30 | 1.1% | 5.0% | 4 帧 |
| 40 | 0.2% | 3.1% | 5 帧 |

**结论**:给别的相机制定时**必须重新量**,别套 15;而且要量"完整 auto
语义"那档(上面的 `scripts/measure_live_conc.py` 会同时给出两档)。
工具:`.venv/bin/python scripts/measure_live_conc.py --rtsp <url> --frames 900`。

**量分数必须调 `frame_judge.conc` 本体,不要复刻**。这个坑踩过一次:复刻时
把块内 `mean(|a-b|)` 写成了 `|块均值A - 块均值B|`,后者只看亮度平移,静止
画面恒接近 0 —— 量出来的分数比线上低一个量级,差点得出"判据在空转误报"的
完全相反的结论。两者只差"块内纹理"这一项,而这一项恰好是 conc 的信号本身。

### "判据没生效"的可观测性:浮框必须报**实测送检间隔**

用户报的现象是「浮框显示没有变化,但实际一直在检测」。两件事都没错,
**浮框回答的根本不是同一个问题**:

* 浮框主行 `无变化 / 画面变化` 是**当前这一帧**的判定。阈值低于素材噪声底
  时 89% 的帧都是"无变化",每次"画面变化"只闪一帧(40ms),肉眼看过去就是
  一路"无变化"。
* 用户数的**检测事件**节奏由**推理耗时**决定。实测这台机器单帧 VLM
  **2.3s**,于是事件恒定每 ~3.75 秒一条,跟判据放行率无关(放行率再低,
  事件也不会更慢 —— 但也永远不会更密)。
* 中间还有一层丢帧:`JudgeDetector.submit_frame` 的队列是
  `ITEM_QUEUE_MAX = 1`(latest-only),判据放行 2.8 帧/秒而推理只吃
  0.27 帧/秒,**九成放行的帧被直接丢掉**。

于是 `every_n=300` 在这条流上**一次都没生效**(实测稳态:评估 3906 帧、
送检 140 帧 = 每 **27.9** 帧一次 = 每 1.12 秒一次;阈值 10、每 17.9 帧一次)。
兜底永远轮不上,因为 gap 从来到不了 300。

修法(`snapshot()` 新增 `every_n_eff = evaluated / sampled`,浮框副行 + 控件
提示据此告警):

```
● 无变化 2.23
conc · 阈值 10.00 · 实际每 18.0 帧送检 · 兜底 300 帧形同虚设   ← 琥珀色告警
```

判断条件:`every_n_eff <= max(2, every_n / 5)` 就说明阈值把几乎每帧都放行了,
兜底间隔是摆设,提示里直接告诉用户"调高阈值或改用手动跳帧"。
**别再让用户靠数事件反推判据状态** —— 那要同时知道推理耗时,信息根本不在界面上。

> **`verify_frame_judge_ui.py` 跨卡连跑时曾时绿时红**,根因值得记住:
> 四张卡共用一个 `page`,而 `page.goto` 只改 hash **不重载页面**;上一张卡的
> `select_option` 会把 `document.activeElement` 留在某个判据输入框上,而
> `update()` **刻意不回写聚焦中的输入框**(不能抹掉用户正在输入的数字,这是
> 正确的)。于是那个框保留着进入本卡时的环境值,切算法时又把陈旧值发回去 ——
> 表现是「切 conc 拿到 10、应为 15」。同一份代码单独跑某一张卡永远是对的。
>
> 修法不是去改产品的回写逻辑(那会把"用户正在输入的数字被抹掉"这个真 bug
> 换回来),而是让**测试自己拥有控件状态**:切 conc 之前显式把阈值框置成
> `/judge/metrics` 的 `default_threshold[conc]` 并派发 `change`。
> 教训:**共享一份配置的多个入口 + 浏览器里不重载 = 状态会跨测试残留**;
> 断言前先把自己的输入设成确定值,别依赖"它应该自己同步过来"。

### 事件历史里的「当前帧变化值」= 判断"阈值以下为什么还出事件"的唯一凭据

用户看着事件几秒刷一条、浮框却显示"无变化",合理怀疑是**阈值没生效**。
事件历史原来只有 `frame_pos` 和描述文本,分不清三种完全不同的情况:

| 事件里的分数 | 含义 | 该动什么 |
|---|---|---|
| `score > threshold` | **超阈值触发** | 阈值对这条素材偏低 → 调高阈值 |
| `0.5*thr <= score <= thr` | **兜底间隔触发**(`gap>=every_n` 且过半) | **设计内,不是故障** → 调帧间隔或换算法,继续抬阈值没用 |
| `score < 0.5*thr` 且 `gap < every_n` | 不该出现 | 真 bug,查 `observe()` |

所以分数必须**随帧一起送到事件**(`DetectorEvent.judge_score`),而不是前端
回头去读当前分数 —— 读的是"现在"而不是"这一帧",对慢事件(2.3s 推理)完全
对不上号。链路:`FrameJudge.current_score`(新增轻量 property,**不能**用
`snapshot()`,那会每帧复制 200 条环形缓冲)→ `VideoSession` sink 第 4 参 →
`submit_frame(judge_score=)` → 队列 5 元组 → `_worker_loop` 统一回填。

回填放在 `_worker_loop` 而不是在每个 `DetectorEvent(...)` 构造点加参数:
构造点有 10 处以上,漏一处就会出现"有些事件有分数有些没有"。

**sink 签名加了参数,老签名不能被打挂**:`VideoSession` 调 sink 时捕 `TypeError`
并按 3 参重试 —— 判据分数是附加信息,不该因为它让别人的 sink 失效。

渲染放在共享组件 `FrameJudge.scoreSpan(prefix, ev)` 而不是各卡各写一遍:
vd / vtd / vtl **共用同一份判据实现**（各自端点、同一份 `frame_judge.py`，
每路各持一个判据器），**三张卡都要显示分数** —— 只给一张卡显示、另两张不
显示的话，用户看到同样的配置却拿到不同信息，比不显示更难排查。回归:
`scripts/verify_event_judge_score.py`(7/7,不依赖 NPU —— 真端到端要算力实例)。

**API 层三条踩过的坑**(都在 `app/core/video/api.py::set_judge`，改这块必读):

1. **必须把 `payload.mode` 原样传下去**,只在校验时归一化。曾经先调
   `_normalize_alias` 把 `z_mad` 转成 `conc` 再传给 `FrameJudge`,迁移表就
   认不出原始模式名 → **阈值不重置** → 线上变成 `conc + threshold=1.0`
   (conc 自然相邻帧 p50 有 3.23,1.0 = 几乎每帧都送检)。静默失效比不迁移
   更糟:它看起来"生效了",实际把节流关掉了。
2. **`set_judge` 不要做 400 校验**。未知 / 已下线的算法名一律**回落
   manual**(项目契约,`verify_vas_change_detector` 断言 `normalize_mode("sad")
   == "manual"`)。改成 400 的话 POST 会失败 → 前端拿不到新状态 →
   `_pruneModes()` 不执行 → 下拉里那个补出来的幽灵 option 永远留着选不上。
   曾一度改成 400,就是这条。
3. **`threshold` 的 Pydantic 默认值必须是 `None`,不能填数字**。"没传阈值"
   和"显式设成某个值"必须能区分 —— 填数字默认值的话,客户端只想改 `every_n`
   时也会把阈值冲掉(实测先设 7.5、再发 `{mode:"conc", every_n:60}`,阈值被
   弹回默认)。`None` 由 `FrameJudge.reconfigure` 解释成
   **"阈值属于模式":模式变了用新默认、模式没变保留现值**。

前端同理:`frame-judge.js` 的 `_defaultThreshold()` 从 `/judge/metrics` 的
`default_threshold` 字段取,**前端不存自己的"默认"** —— 刻度按算法不同,写死
必然慢慢与服务端漂移。阈值输入框同理**不预填常数**(曾经写死 `"6"`,换算法后
就变成了一个不报错的错值)。改判据相关代码时 grep 一遍 `|| 6` / `value = "6"`。

**第 4 条坑在测试里,而且它比前三条更难发现**:任何**显式带 threshold 的
POST 都会把那个数值钉进判据**,后续切模式时服务端就不再重置(第 3 条的规则
只对"没传阈值"生效)。`verify_frame_judge_ui.py` 里两处 z_mad 时代遗留的
`threshold: 6`(收尾复位 + 4d 验"已下线算法回落")污染链是这样扩散的:

```
4d 写死 6 → 共享判据 manual/6 → 后面几张卡已挂载的控件从 WS 快照读到 6
→ 用户在那些卡上改算法,控件把陈旧的 6 又发回去 → "切 conc" 拿到 conc@6
```

而「切 conc」断言当时**只看 mode 不看阈值**,所以全程 52/52 全绿 —— 测试自己
把回归基线改坏了还报通过。查这个问题时最容易误判成"迁移没生效":页面上阈值框
灰字显示 6、浮框写「阈值 6.00」,看着像线上状态坏了,实际干净重启后是 15.0,
产品代码没问题。两处现在都**省略 threshold**,断言也补上"`threshold` 必须等于
`/judge/metrics` 的 `default_threshold[conc]`"—— 补完立刻把这条故障抓出来
(vtd/vtl 红,改完才绿)。教训:**断言要盯住"阈值属于模式"这个不变式,不能只盯
mode**;而跨卡共用的配置(判据只有一份,三张卡共用 `VideoSession`),任何写死的
数值都会被下一张卡继承。

下拉里 conc 标着「约 N ms/帧」,这个 N 由 `frame_judge.cost_ms()` **实测**
（进程内第一次调用时真跑 12 帧 960x540 合成帧,之后走缓存),不是写死的常数
—— 同一条代码实测两次拿到 1.51 / 1.95 ms/帧,写死一个数只会在改了缩略图长边
或换了机器之后悄悄过期,而"运行一次的时间"这种标注不准就是在骗人。两个
`/judge/metrics` 端点都返回 `cost_ms` 字典。`wizard.json` 是静态配置,里面的
「约 2 ms/帧」只是同一测量的取整近似,权威数字看功能页下拉。

默认模式是 `manual` —— 三张卡历史上默认每 30 帧送一次,`DEFAULT_EVERY_N = 30`
就是为了保持这个开箱行为不变。

三条**实测踩出来的**规矩，改这块时务必先读:

1. **`snapshot()` 带 `gen`（配置代次），前端必须丢弃过期快照**。WS/HTTP 的
   传输顺序 ≠ 计算顺序:用户在页面上把判帧算法改成 manual 之后,POST **之前**
   就已经发出的那几份旧快照还会陆续到达(快照是发送那一刻算好的)。照单全收
   的表现是控件在两个模式之间来回跳。`gen` 取自
   `frame_judge._GEN`(进程级单调递增,**不能**改成每实例从 0 开始 ——
   VAS 每次 `start()` 都新建判据器,按实例计数会让前端记住的 gen 比新 run
   的还大，之后所有快照都被当过期丢掉，浮框和曲线直接冻住)。
2. **VAS 的判据配置是「下次开始分析生效」**（`configure_judge` 在运行中只
   暂存到 `_pending_judge`）。中途换阈值会让本轮的帧集混用两把尺子，落库的
   `sample_mode` 与缓存键也会按新值写、按旧值采。对应地前端 VAS 实例用
   `syncControls: false` —— 每 tick 推来的是**本轮真实**判据状态，不能拿去
   回写「下次生效」的配置控件。
3. **曲线只在向导里出现**（用户明确要求），功能页只留配置 + 右上角浮框。
   实现上曲线是 `position: fixed` 的浮层，每 500ms 按
   `canvas.getBoundingClientRect()` 跟随摆位 —— 因为向导把 `<canvas>` **本身**
   搬进自己的容器（`state.js:moveCanvasToWizard`），浮框做成 canvas 的兄弟
   节点就会掉在隐藏页里。这样做也就不用改共享 wizard 框架（改共享框架要跑
   全量跨卡回归）。
   **fixed 浮层不占文档流，所以必须配一个占位块**：`.vj-curve-spacer` 插在
   `.wd-wizard-canvas-wrap` **外面**（wrap 本身是 flex 容器，插进去会跟画布
   并排），把视频源步里的判据控件顶到曲线下方。少了它，曲线与下拉框抢同一块
   屏幕 —— 实测 `elementFromPoint` 命中的是曲线 canvas，下拉框点不动。
   两个浮层同时 `pointer-events: none`，`sync()` 也只在画布可见时画。

**浮框贴画布下沿(不是上沿)**：右上角是各卡检测结果的落点(视频目标框 /
描述文案都从那儿起),浮框摆上沿会直接压住它们。`sync()` 里按
`min(canvas.bottom, viewport) - badgeHeight - PAD` 定位,并在画布下沿滚出
视野时退回**可见下沿**、再兜一次视口 —— 否则浮框会飘到画布下方那些无关
控件上面。实测(1600x1000):距右 10px、距下 10px,完全落在画面内。

**判据配置的横排 vs 竖排由"它在哪"决定,不由"哪张卡"决定**：共享组件
`frame-judge.js` 两种布局都有,宿主标了 `data-judge-layout="inline"` 就切
横向(加 `.vj-inline`),否则竖排。用宿主**属性**做开关而不是认某个 class
名 —— 认 class 会把共享组件绑死在某一张卡上:下一张卡把判据栏也摊到视频
下方时,还得再来改组件。另外三张卡的判据控件在自己的配置区里,保持竖排。
**别为了修一张卡把三张卡一起改了** —— 横排 CSS 全挂在 `.vj-inline` 之下。

**vd(音视频描述)的视频源 + 判据两条栏都在视频正下方,不在 340px 右栏里**:
视频源(`.stage-source-bar`,实测 x=104 w=1102,与黑框同起点同宽,高 57px) +
判据(`#vd-judge-config[data-judge-layout=inline]`,高 79px)。原先两者竖排在
右栏里,把那条栏拉得比视频还高(实测右栏 1324px → 搬走后 851px,与左栏齐平
且 scroll-client=0 不溢出);而它们都是"看着画面调"的控件,摆在画面旁边才顺手。
视频源整段**移出了 `<form id="vd-form">`** —— 里面的按钮全是 `type="button"`,
全仓也没有按 `#vd-form` 取值的代码,移出去不影响任何逻辑(浏览器实测填源 + 点
打开照常工作,画布出帧,无 pageerror)。

**对齐基准是黑框 `.vd-stage`,不是内层 `<canvas>`**:`#vd-canvas` 只有
`max-width:100%` 而不强制拉伸,16:9 素材会以 960 宽居中落在 1102 宽的黑框里
(实测 x=175 vs 黑框 x=104)。拿 canvas 当基准量"没对齐"是量错了对象。

**VAS(video-analysis-search)不在此列** —— 它的视频源与判据仍竖排在左侧控制台
里(判据挂在「分析」子节,取代了原来的 `#vas-every-n` 输入框)。改布局时**别
顺手把它也搬走**:那是另一张卡、另一套信息密度,用户要的是 vd。曾按 VAS 做
了一轮改动又整体还原,`verify_judge_bar_layout.py` 现在对两张卡**分别**断言
(一张必须搬到位、另一张必须没被误搬),靠"另一张没变"证明不了自己变了。

缓存指纹 `analysis_sample_key` 把**算法**也进键：`conc`/`z_mad`/`auto` →
`a:<阈值>`（三者都是同一套"默认自动判据"的历史写法，与历史自动行保持一致，
否则老缓存全失配 —— 改算法时这张别名表要一起改），其它度量名 →
`a:<阈值>:<算法>`（只按"非手动"归并会让两个算法同阈值共用一条缓存，而它们选出的
帧集根本不同；现在虽然只放行 conc，规则留着 —— 将来加算法时不必再回头改缓存键）。
`manual` 仍返回空串，保持与迁移前写入的历史手动行一致。

回归（改了本段任一处都要重跑）:

| 脚本 | 覆盖 | 结果 |
|---|---|---|
| `scripts/verify_frame_judge_metrics.py` | **判据选型的不变式**（不靠素材也能跑大半）：目标分必须高于正常帧 p95 / 稀疏微变不炸 / 全局变化被归一 / 面积单调性 + 10% 天花板 / 阈值操作点 | 37/37 |
| `scripts/verify_vas_change_detector.py` | ChangeDetector/FrameJudge 单元 + 真实视频采样密度 + 只暴露 conc + 存量 z_mad/auto 迁移 | 60/60 |
| `scripts/verify_judge_bar_layout.py` | 真浏览器两张卡**分别**断言：vd 的视频源 + 判据是否摊在视频正下方（DOM 归属 / 横排 / 与黑框同起点同宽 / 右栏不溢出）、VAS 是否**保持**侧栏竖排没被误搬；含填源 + 点打开的真实交互 | 20/20 |
| `scripts/verify_frame_judge_ui.py` | 真浏览器 4 张卡：向导内改判据立刻落库 / 曲线与画布等宽 / **曲线不压判据控件** / 下拉只含 manual+conc 且标注实测耗时 / 过期配置不复活 / 无 pageerror | 52/52 |
| `scripts/verify_vas_judge_e2e.py --mode conc` | 真视频 + 真 NPU：WS 判据推送 / 分数曲线 / 真节流 | 5/5（683 帧看 / 26 送检，3.8%） |
| `scripts/verify_vas_judge_e2e.py --mode manual` | 同上，手动模式 ≈ 每 30 帧送一次、无分数曲线 | 3/3（510 帧 → 17 送检） |
| `scripts/verify_wizard_env_step.mjs` | 6 张卡的向导结构（判据并进 source 步后 57/57） | 57/57 |
| `scripts/smoke_wizard_all.py` | 6 张卡向导实际挂载 | 6/6 |

> `verify_vas_judge_e2e.py` **一轮只跑一次分析**，两轮之间要重启服务 +
> 重载模型：wdnav 的 VideoCapture 在「跑完一轮 → 再开一轮」时会踩 mpp
> 缓冲的原生断言（`mpp_buf_slot: Assertion !slot->status.on_used failed`），
> 第二个 run 的 decode 直接死掉。这是 SDK 侧老问题、与判据无关，但写测试
> 的人必须知道，否则会误判成"判帧把解码搞挂了"。

> 这条脚本还要求**本机有跑着的算力实例**：`/analysis/open` 依赖
> `get_registry().get_session(...)`，没有 worker 时直接 **503**。而
> `config.jsonc` 的 `compute.autostart` 目前是**注释掉的**（"开机自启暂时
> 关闭"），所以服务重启后不会有任何模型起来 —— 跑这条脚本前先在
> 「算力配置」页把 `qwen3-vl-4b` 起起来。**别为了跑测试去调
> `PUT /api/compute/config`**：那会走 `ConfigStore._persist()` 重新序列化，
> 把 config.jsonc 里那段解释 autostart 为什么关掉的注释整段抹掉（文件里
> 自己写了这条警告）。要临时开就复制一份配置、用 `WDNWEBUI_CONFIG` 指过去。

## Wizard 框架(v0.4 起统一应用入口)

v0.4 起,6 张带 `wizard.json` 的卡(`text-chat` / `tts` / `video-detection` /
`video-target-detection` / `video-target-localization` /
`video-analysis-search`;`audio-analysis` 没有向导)进入页面时 framework 自动
挂载该 app 的 wizard,引导用户从空白走到可运行状态
(env_check → form → finish)。点 完成配置 后 wizard 调用 app 的
`applyWizardSnapshot(opts)` 并 unmount;用户离开页面再回来时,
`wizard:unmounted` 让向导**重新挂载**(`_wizardMounted` 清标记,卡片 IIFE
**不**重跑 —— 详见设计原则第 5 条),localStorage 持久化字段值,不会让用户重填。

> **未实现"只在首次进入时弹出"**:曾计划只对第一次访问显示 wizard,
> 但用户实测反馈"刷新就丢失引导,记不清配置路径"反而更麻烦 —— v0.4
> 改为每次都挂载 + localStorage 保留已填值。如果未来要"完成过一次就不
> 再弹",需要 (a) wizard 完成时 `localStorage.setItem(<key>.dismissed, 1)`,
> (b) tryMount 检测到该 key 直接 skip。前者已经在 `_finish` 后隐式存在,
> 加 (b) 即可,无需改 framework。

### 框架组件

| 文件 | 职责 |
|---|---|
| `app/static/core/wizard/state.js` | IIFE。导出 `window.WizardState = {mount, unmount, stepForward, skip}`。管理 stepper / env-check / form / finish 步骤状态机。监听 hashchange,在用户切走时自动 unmount。 |
| `app/static/core/wizard/registry.js` | 通用挂载入口 `window.WizardRegistry.{tryMount, unmount}`。读 `manifest.wizard_config` 路径加载 wizard.json,然后调 `WizardState.mount(app, cfg)`。**零硬编码**:不知道任何具体 app,只跟 wizard.json schema 打交道。 |
| `app/static/core/wizard/wizard.css` | wizard 容器 / stepper / env-row 样式 |
| `app/apps/<id>/static/wizard.json` | 应用向导配置(swcap_to_model + steps + fields) |

### wizard.json schema

```jsonc
{
  "enabled": true,                       // 总开关;false 时按钮隐藏
  "wizard_title": "音视频描述快速配置",  // wizard 弹窗标题
  "swcap_to_model": {                    // 应用所需 capability → 目标 model + NPU
    "swcap_video_describe": {"npu_index": 0, "model_id": "qwen3-vl-4b"}
  },
  "open_source_button": "vd-open",       // 触发"打开源"按钮的 id(用于在 wizard 完成后自动填源)
  "upload_endpoint": "/api/video-detection/upload",  // 拖放上传目标
  "start_buttons": ["vd-detector-start"],  // wizard 完成时自动 focus 的启动按钮
  "localStorage_key": "wdnwebui.wizard.<id>.values",  // (可选) 覆盖默认 key
  "steps": [
    {"id": "env",    "title": "运行环境检查", "kind": "env_check"},
    {"id": "source", "title": "视频源",       "kind": "form", "fields": [...]},
    {"id": "ground", "title": "定位提示词",   "kind": "form", "fields": [...]},
    {"id": "finish", "title": "完成配置",      "kind": "finish"}
  ]
}
```

`kind` 取值:
- `env_check` — 拉 `/api/compute` 检查 `manifest.software_capabilities`
  对应 capability 是否在本机运行;只有本机算力(spawned / adopted_local)
  算"已启用",peer adopted 即使 running 也算"未启用"(见"本机算力语义栈")。
  检查通过 → ✓;未通过 → ✗ + "一键开启所需模型" 按钮(调
  `_wizardBringUpModels`)。
- `form` — 通用表单步骤;`fields[]` 每项 `{id, ui_key, label, type?, default?, ...}`,
  支持 `type: "checkbox"` / `"text"` / `"prompt_rows"`(`prompt_rows` 是
  v0.14 加的多行输入,见 `ca7bc64`)。每次 step 切换 / 字段 blur 时
  `_captureValues` 自动把当前值写 `localStorage`。
- `finish` — 收尾步骤,显示摘要 + 等模型 running(`_waitForModelsRunning`
  轮询 `/api/compute` 至所有 `swcap_to_model` 的 model_id 变 running 或 90s 超时)。
  完成后调 `applyWizardSnapshot` 并 unmount。

### 判据配置放在向导的「视频源」步（不单独占一步）

4 张视频卡(vd / vtd / vtl / vas)的 `wizard.json` 都把判据三个字段
(`judgeMode` / `judgeThreshold` / `judgeEveryN`)并进 **source 步**的
`fields`,而不是另开一步 —— 曲线画在向导的视频源步里(canvas 那时才被搬进
向导),配置与它同屏才说得通。改完由 `FrameJudge.fromWizard(prefix, opts)`
在「完成配置」时落库。

三条实测过的规矩:

1. **别把步数写死进测试**。`scripts/verify_wizard_env_step.mjs` 原来断言
   vtl「共 4 步」,结构一变就挂。现在只断言**首步 env_check / 末步 finish /
   source 步含 judgeMode**—— 真正要守的是结构自洽,不是步数。
2. **向导的「打开」按钮读的是向导自己的输入框**(`[data-field-id]`),
   不是页面上的 `#<prefix>-source`;填错了会被 wizard 的空值覆盖,
   表现为「点了打开没反应、canvas 进了向导但一帧不播」。
3. **改判据要立刻生效,不能等「完成配置」**。曲线就在这一步盯着,改阈值却
   要等收尾才看到效果等于没曲线。框架侧因此加了一个**通用**事件:字段一
   改动就 `_captureValues` 并 dispatch `wizard:field-change`(只发事件、
   不认识任何字段名);`frame-judge.js` 按**字段 id 前缀**认领
   (`<prefix>-judge-*`)并 `applyConfig`。判据逻辑留在共享组件里,框架仍然
   零耦合 —— 框架里出现任何具体字段名都算违规。

### 应用页契约:`applyWizardSnapshot(opts)`

wizard 完成(`_finish`)后,`state.js` 调
`window.ImApp_<id-with-underscores>.applyWizardSnapshot(opts)`。
**6 个有 wizard 的 app 都必须实现**这个 hook:

```js
window.ImApp_video_detection = window.ImApp_video_detection || {};
window.ImApp_video_detection.applyWizardSnapshot = async function (opts) {
  // opts: wizard.json form fields 的 camelCase 映射;
  //   来源是 field.ui_key(camelCase 形式) → field.id(kebab-case)。
  //   例如 {"source": "rtsp://...", "loop": true}
  //
  // 通常做:
  //   1) 把 opts 字段填到 DOM input / 调用现有 setter
  //   2) 触发 start_buttons[0] 自动 click
  //   3) 返回 Promise(throw → wizard 回退到 source 步骤)
};
```

未实现 `applyWizardSnapshot` 的 app:wizard 完成时只是 unmount,应用页
不会跑出 demo 页(`console.warn`)。这是 wizard 设计上的"安全降级" —— 让
应用层**可选**地接入自动启动,但不强制。

### `_wizardBringUpModels` 全局入口

`app/static/core/wizard/state.js` 暴露 `window._wizardBringUpModels(cfg)`,
供 env-check 步骤的"一键开启"按钮调用。三步流程:

1. **停非必需**(本机 spawned / adopted_local;**跳过 adopted 远端 peer**):
   `instance._source !== "adopted"` 的 running / loading 实例,如果其
   capability 不在 `cfg.swcap_to_model` 中 → DELETE `/api/compute/instances`。
2. **启必需**:`swcap_to_model` 的每条 → POST `/api/compute/instances
   {model, npu_index}`(allow_adopt=False,peer adopted → server 403 兜底)。
3. 返回 `true` 表示启动指令已发,后台加载中;false 表示失败(env_check 步骤
   会 retry 2 次 + confirm 兜底)。

### `wizard:unmounted` 事件 + 重新挂载

`state.js:unmount()` 末尾 dispatch `window` 上的 `wizard:unmounted` 事件
(detail `{appId, action:"unmount"}`)。`framework.js:232` 监听该事件,
从 `DemoGuard._loadedApps` 删对应项 —— 否则下次再点同一应用卡片时
ROUTER 看到 `_loadedApps.has(page)` 为 true 直接跳过 `_loadCardJS`,
wizard 永远不会再挂载。

### 应用页启用 wizard 模式

应用页(`app/apps/<id>/`)启用 wizard 仅需两步,**无需改 framework.js**:

1. 加 `static/wizard.json`(schema 见上)
2. `manifest.json` 加 `"wizard_config": "wizard.json"` 字段
3. (可选) 在 `<section id="page-<id>">` 顶栏加"向导模式"按钮:
   `<button class="wizard-mode-btn" data-wizard-mode data-page-id="<id>">向导模式</button>`
   —— framework **不**自动注入该按钮,各 app 的 `template.html` 写明即可;
   重新打开 wizard 的"开关"实际是 framework 在 `_loadedApps` 删除后下次
   navigate 自动触发,按钮只是"用户手动重开"的可视入口。

挂载时机:`framework.js:_loadCardJS` 在 app JS 全部 `await load` 完成后
调 `WizardRegistry.tryMount(manifest)`。`enabled: false` 或 wizard.json
缺失 → 静默跳过。

### 强保证:framework 零耦合

`state.js` / `registry.js` / `wizard.css` **不引用任何** `app/static/apps/`
或具体 app 的 DOM id(除通用的 `WIZARD_NS`-前缀 CSS class 命名空间)。
新增 app 加 wizard.json 即可使用,**不动 framework 代码**。

## 本机算力语义栈(v0.4 修复)

wdnwebui 是多板 LAN 集群部署形态:每块板子独立 NPU,通过 wdndds DDS
互相发现彼此的 compute_worker 子进程。本机启动一个模型后,DDS 可能从
peer 也收到同名服务 —— 此时 `_instances` 同时持有:

- `spawned`        本次 boot 由本机 Popen 起的 worker
- `adopted_local`  本机孤儿(uvicorn reload 残留);同 hostname 探到
- `adopted`        LAN peer worker,通过 DDS 收到(主机名不同)
- `unavailable`    全 LAN 都没回应

三层一致语义:用户禁/启/查询的目标永远是**本机算力**(spawned /
adopted_local);peer adopted 即使 running 也算"远端板子的资源,本机未启"。
此约定由 commit **`94f4637`** 一并落地,三处纵深防御:

### Layer 1 — wizard client(`app/static/core/wizard/state.js`)

| 函数 | 过滤 |
|---|---|
| `_wizardBringUpModels` | DELETE/POST 都跳 `inst._source === "adopted"` |
| `_probeEnv` | 只把 `spawned` / `adopted_local` running 算 `runningCaps`,peer adopted running 不算本机已开启 |
| `_waitForModelsRunning` | 同 `_probeEnv`,对称 |

### Layer 2 — server(`app/system/api/compute.py`)

- `DELETE /api/compute/instances` 拒 adopted → **HTTP 403**,detail 明确
  提示"请到目标板子上点禁用,或在本机启动一个新实例后删除本地副本"。
- 即使 client Layer 1 漏掉,server 也兜底拒绝;前端按钮 disable 也是
  Layer 1 兜底,server 兜底是最后防线。
- `POST /api/compute/instances {model, npu_index}` 同样对 adopted 拒
  bind → 403,鼓励用户在本机 spawn 新实例。

### Layer 3 — compute registry(`app/core/compute/registry.py`)

`_find_by_model(model_id)` 同 model_id 多 source 时:

```python
def _find_by_model(self, model_id):
    with self._lock:
        adopted = None
        for inst in self._instances.values():
            if inst.model_id != model_id:
                continue
            if inst._source in ("spawned", "adopted_local"):
                return inst  # 本机优先
            adopted = adopted or inst  # 远端 fallback
        return adopted
```

行为对照:

| 本机 spawned | peer adopted | 旧行为 | 新行为 |
|---|---|---|---|
| not_loaded | running | DELETE → 403(误以为本机关不掉) | DELETE → 200(卸载本机 spawned) |
| running | running | DELETE → 200 | DELETE → 200 |
| not_loaded | not_loaded | DELETE → 404 | DELETE → 404 |

三层一致后:用户操作"禁用 / 启用 / 探测"的 target 永远在本机;peer
adopted 仅作 fallback(用户主动选择)。

### 显示尺寸与送检尺寸必须分开（两个独立 bug）

用户报「一路视频只有 384x216，应该占满播放器」。实际是**两个问题叠着**。

#### bug 1：`cols` 初值撞车 → 只有 1 路时宫格不铺满

`ensureGrid()` 增量重构后加了守卫 `if (VTD.cols !== cols)`，而
`VTD.cols` 初值就是 `1`，单路时算出的 `cols` 也正好是 `1` → **条件恒假
→ `gridTemplateColumns` 从没被设置过** → grid 退回默认 auto 布局，
单列被图片固有宽度（384px）撑开。

实测：1102px 宽的播放区里只用了 **386px**。而且**只有"恰好 1 路"会错**
（6 路时 cols 从 1 变 3 触发设置，看起来正常），所以一直躲过前面的测试。

→ 判据改读 **DOM 上的实际生效值**，不靠 JS 侧状态：
`if (grid.style.gridTemplateColumns !== \`repeat(${cols}, 1fr)\`)`

> 教训：增量重构时新增的"没变就不做"守卫，初值必须选一个**不可能与任何
> 合法值相等**的哨兵，否则第一次调用会静默跳过。读 DOM 实际值最稳。

#### bug 2：把「分析尺寸」当「显示尺寸」用

送检尺寸决定 wdnai 选哪个 vision 变体（384 变体权重 61MB / 单帧 0.74s；
768 变体 252MB / 2.06s），不能和显示尺寸混为一谈。但原先浏览器显示复用了
同一张缩略图，于是单路时被拉伸 2.86 倍填满 1100px，发虚。

→ **显示与送检分离**：
* `_out_w/_out_h` = RGA 输出 / 浏览器显示，按路数分档
  （1~4 路 960 长边，5 路以上回落送检尺寸）
* `_ana_w/_ana_h` = 送检 VLM，**按源尺寸算**（`derive_inference_size`，
  与单路 `VideoSession` 同口径），**不跟随显示分档**
* 轮转提交那一瞬用 `wdnav.rga.resize_bgr` 缩到送检尺寸

**为什么缩放放在轮转里而不是解码线程**：解码每帧都缩会让 RGA 次数翻倍
直接撞吞吐上限；轮转每路 ~2s 才提交一帧，缩放频率低两个数量级。

**不做软 resize 兜底**：RGA 失败返回 None，该路本轮跳过（宁可少一路，
也不要悄悄走软路径 —— 见 AGENTS 铁律 2）。

实测代价（本机 720p 源，单路）：960×540 = 1519KB/帧 vs 640×360 = 691KB/帧
（2.2 倍），但单路仍 25.0fps / 0 错误。路数多时这项才是硬约束，故分档。

> **送检尺寸曾长期错配成 384**（v0.23 修正）。当时的论断是「384 是送检
> 尺寸，不能随手放大」—— 这话把**送检尺寸**和**RGA 输出尺寸**混为一谈了。
> 真正该守的规则是「送检尺寸要让最长边对上你想用的 vision 变体」：想用
> 便宜档就送 384，想用准档就送 640。而**单路 `VideoSession` 送的是 640**
> （`max(thumb*4, src//4)`），多路送 384 等于白降一档精度 —— 是精度降档，
> 不是吞吐优化。改后单路推理 1300ms → 2400ms（换到 768 变体），代价换回来。

实测（1/4 路显示 960×540，5+ 回落 640×360；送检恒 640×360）：

| 路数 | 显示 | 送检 | 每路解码 | 轮转 | 事件 |
|---|---|---|---|---|---|
| 1 | 960×540 | 640×360 | 25.0 | round 20 | 20 条 |
| 4 | 960×540 | 640×360 | 24.8~24.9 | round 26 | 4 路各 5 条 |
| 6 | 640×360 | 640×360 | ~24.8 | — | 6/6 出画面 |
| 16 | 640×360 | 640×360 | ~24.7 | — | 16/16 出画面 |
| 32 | 640×360 | 640×360 | 20.6 | — | 32/32 出画面 |

单路推理 1300ms / 32 路一轮 ~77s，**0 跳过**（跳过数 >0 就是缩放或推理失败
的判据，见上文 `RR_MIN_INFER_MS`）。

### 踩过的坑：`<img>` + blob 换 src → 1~4 路疯狂屏闪（v0.22）

**症状**：用户报「视频源少于 5 个就疯狂屏闪，≥5 个正常」。上个版本
（单路 canvas）不闪，所以第一反应是"我又改坏了什么"。

**关键线索就是那个「5」**：它正好是 `DISPLAY_HIGH_MAX_CHANNELS = 4` 的
分界线 —— 1~4 路显示 **960×540（≈65KB/帧）**，5 路以上回落
**640×360（≈33.5KB/帧）**。所以现象其实是**"大图才闪"**，与路数本身无关。

**先证伪服务端**（这一步很关键，不然会一直怀疑解码/RGA/WS）：
写探针直连 `/ws`，收 200 帧，解析每个 JPEG 的 SOF 段拿真实宽高：

```
尺寸分布: [(960,540) x 200]        相邻帧长跳变(>40KB)=0/199
帧长 min=61647 p50=65131 max=65882
```

稳态**一帧不差**全是 960×540，帧长波动 <7%，无跳变 → 服务端、WS、编码
全部无辜。（首轮探测曾抓到 2 帧 384×216，是 `open()` 刚返回时通道还没
切到显示尺寸的启动瞬间，稳态不出现；**打印帧序号**才把它和"稳态交替"
区分开 —— 只统计"有几个"会被启动瞬态误导。）

**真凶在前端**：`drawVtdFrame()` 每帧做
`t.img.src = URL.createObjectURL(blob)`。换 src 会**掐断上一帧还在进行的
加载** —— blob 也要走完整的 URL loader（取字节 → 解码 → 上传纹理）。
25fps 的预算是每帧 **40ms**，而大图这条路实测 `createImageBitmap` 单张
就要 **13ms**（384 只有 2ms），留给 loader 的余量本就很窄，主线程一忙
就有一批加载被下一次赋值中途取消 → 元素拿不到完整帧 → **画面狂闪**。
小帧（12KB）时整套轻松跑完，所以 5 路以上看不出来。

**修复**：改回 canvas + `createImageBitmap`，并加**每路单飞**：

* `createImageBitmap` 在解码线程上解，**不经过 URL loader**，不存在
  "换源掐断"；
* 每路 `busy` 时到达的新帧只留最新一帧（`t.pending`），旧的直接丢 ——
  视频语义本来就允许丢帧，丢一帧不影响连贯性；
* `canvas.width/height` **只在真的变了时才写**：每帧写会清空画布并重置
  ctx 状态（HEAD 老版 `canvas.width = img.naturalWidth` 每帧都在犯这个）；
* 格子被移除时置 `t.dropped`，在途解码自己退出，不再往已摘掉的 tile 上画。

实测（960×540，25fps）：`decoded=778 / busyDrop=62 (7.4%) / drawErr=0`，
连续 40 次采样 **32 个不同取值、0 次近黑**。6 路 640×360：
`decoded=377 / busyDrop=12 / decodeMs=2`。

> 教训：**"小图没事、大图出事"是一整类竞态的典型指纹**。任何按帧替换
> `img.src` / 重写 `canvas.width` 的绘制路径，都要问一句"上一帧的加载
> 结束了吗" —— 没做在途保护，在小尺寸下永远测不出来。
>
> 诊断这类问题先**证伪服务端**再动前端：探针直接量链路上的帧长 + JPEG
> SOF 尺寸，比截图靠谱得多，也不会被启动瞬态误导。

### 踩过的坑：刷新网页卡死 —— WS 每次连接都重放全部历史事件（v0.22）

**症状**：用户报「刷新网页卡住了」。服务本身完全正常（首页 20ms、
API 9ms、静态资源 6~9ms、WS 握手 0.04s、负载 0.39），所以第一反应
是"服务没挂"，方向就转到浏览器侧。

**定位过程**：直接看渲染进程 CPU —— `ps -eo pcpu,args | grep -- --type=renderer`
发现 **106% 持续近 3 分钟**。同时发现 WS 一连上第一条消息就是
`{"type":"event","id":54,...}`：**历史事件被逐条回放**。

**根因**（`api.py::stream_ws`）：

```python
last_event_id = 0                      # ← 每次连接都从 0 开始
while True:
    for ev in detector.events_since(last_event_id):   # 于是返回**全部**历史
        await ws.send_json({"type": "event", **ev})
```

前端每条都走 `addVtdEvent` → `appendVtdEventLi`：建 DOM 子树 +
`insertBefore(firstChild)` + `trimVtdEventList` 扫描裁剪。实测积累到
200 条时：回放 200 条 / **519KB（79% 是 `crop_b64` base64 缩略图）**，
突发灌进浏览器，**渲染进程打满一个核，页面彻底卡死**。

这是**随使用时间无界增长**的问题：事件越多，每次刷新越慢。

**修复**：
* 服务端：连接时把历史**打包成一条** `{"type":"event_history","events":[...]}`，
  并且只发最近 `WS_EVENT_HISTORY_MAX = 50` 条（前端 `MAX_RENDERED` 本来
  就只渲染 50，多发纯属浪费）。同时把 `last_event_id` 直接推到最新，
  后面不再逐条重发。
* 前端：`event_history` 分支**一次性重建**列表
  （`VTDDetector.events = evs.reverse()` + `renderVtdEventList()`），
  绝不能逐条 `addVtdEvent`。注意服务端按 id **升序（旧→新）**发，
  本地数组是**新→旧**，所以要反转。

实测：回放从「200 条独立消息 / 519KB」变成「1 条消息 / 含 50 条」；
页面首次就绪 0.88s，连续 3 次刷新 DCL 0.16~0.19s、就绪 0.72s，
`err=0`、无 pageerror。

> 教训：**「服务端把整段历史逐条推给新连接」是很容易写出的写法**，
> 平时看不出问题，直到历史攒够才炸。凡是"连上先补历史"的地方，都要问：
> 历史有上限吗？是**打包一条**还是逐条？客户端是**整体重建**还是逐条追加？
>
> 另：这类问题**在服务端毫秒级响应里完全看不出来** —— 接口全绿、CPU 正常、
> 只有浏览器渲染进程在烧。诊断入口是 `ps` 看 `--type=renderer` 的 CPU，
> 不是 curl 服务。

### 踩过的坑：向导填多路后自动打开失败（单数 key 吞掉整个多行文本）

**症状**：向导里视频源填**多路**，点「完成配置」进入页面后自动打开视频异常；
填**单路**却正常。单路正常是最有迷惑性的地方 —— 它让人以为"多路功能本身坏了"。

**根因**（`app.js::applyWizardSnapshot`）：

```js
await vtdOpen({ source: opts.source, loop: !!opts.loop });   // ← source 单数
```

`opts.source` 是向导写回 `#vtd-sources` 的**整个多行文本框内容**。而后端
`/open` 里 `source`（单数）是老前端的兼容写法：

```python
single = payload.get("source")
raw = [single] if isinstance(single, str) else []     # → 整个多行文本变成"一路"
```

于是 6 行路径被拼成**一路带换行符的假路径**。实测该路
`state=idle`、`total_frames=0`、`has_frame=false` —— 永远出不了画面，
但**不报错**，所以页面上只表现为"打开异常"而不是"打开失败"。

单路时多行文本恰好只有一个元素，兼容写法正好等价 → 所以只有多路会坏。

**修复**：把「打开视频」的完整逻辑抽成 `openVtdFromTextarea()`，
**按钮和向导共用同一条路径**：

```js
// 按钮
openBtn.addEventListener("click", () => openVtdFromTextarea());
// 向导
const r = await openVtdFromTextarea();   // 内部读文本框 → 按行拆 → ensureGrid → vtdOpen({sources})
```

顺带修掉向导路径的第二个问题：原来它**只调 `vtdOpen`，从不调
`ensureGrid`**，宫格要等下一次 1Hz 状态快照才建出来。共用后
建格、按钮置灰、提示文案全部一致。

> 教训：**兼容用的单数入参（`source`）和复数入参（`sources`）共存时，
> 调用方极易用错**。凡是"文本框/多值 → 接口"的转换，转换必须发生在
> **调用点**，不能指望后端替你拆 —— 后端只看到字符串，看不出它本意是几路。
>
> 同类信号：**症状只在"多值"时出现、单值正常**，几乎总是调用方把多值
> 整块塞进了单值字段。

### 踩过的坑：两个状态框画在同一个 CSS 属性上 → 静默互相覆盖

**曾有过两个框**：**黄框 = 当前正在被 VLM 推理的那一路**
（`.vd-tile.active`），**绿框 = 这一路刚命中检测目标**（`.vd-tile.hit`，
`flashVtdTile()` 触发，闪 900ms）。用户问「绿框干嘛用的」，顺带暴露了
它们在**互相打架**：

```css
.vd-tile.active { border-color:#ff9f0a; box-shadow:0 0 0 2px rgba(255,159,10,.45); }
.vd-tile.hit    { box-shadow: 0 0 0 2px #30d158; }      /* ← 也写 box-shadow */
```

两条规则**特异性相同**、`.hit` 写在后面 → 同框时**后写的把 box-shadow 整个
覆盖**，没有任何报错。浏览器实测：

| class | 边框 | 外环 |
|---|---|---|
| `active` | 橙 | 半透明橙环 |
| `hit` | 半透明白 | 绿环 |
| **`active hit`** | 橙 | **绿环**（黄环没了）|

**而且这个覆盖是必然发生的**：轮转串行，检测事件**只可能来自"当前正在推理的
那一路"**。所以每次出结果，黄环都被顶掉 —— 「哪一路在推理」这个指示恰好在
最该看见的时候消失。

**最终形态（用户决定）**：**只保留一个高亮边框**，语义就是「正在推理」，
颜色改成**绿色**；命中闪框整个删掉（`flashVtdTile` / `.vd-tile.hit` /
`hitTimer` 全部移除）。"检测到没有"由下面的**检测记录**列表表达，
不需要再在画面上叠一层会消失的提示 —— 瞬时提示本来就是最难看清的东西。

```css
.vd-tile.active { border-color:#30d158; box-shadow:0 0 0 2px rgba(48,209,88,.45); }
```

> 教训：**同一元素上的多个状态指示不要都画在同一个 CSS 属性上**。
> 后写的规则静默覆盖先写的，只有把两个状态同时挂上去读 `getComputedStyle`
> 才能发现。
>
> 附带一条读样式时的坑：`.vd-tile` 有 `transition: .15s`，刚 toggle 完立刻
> 读 `getComputedStyle` 拿到的是**过渡起始值**，必须等 >150ms 再读，否则
> 会误判成"样式没生效"。
>
> 另：中间试过把 `hit` 改成只用 `border-color` 来让两者共存（可行，实测
> `active hit` → 绿边 + 黄环），但用户最终选择**只留一个框** —— 结论是
> 画面上叠多层瞬时提示对终端客户是负担，信息该落到记录列表里。

### 面向终端客户：把技术细节从 UI 上拿掉（v0.22）

本页是给**终端客户**用的，不是给开发者看的。所以这些一律不上页面：

| 内容 | 处理 |
|---|---|
| `诊断 WS开1/关0 · meta … · 已画 … · 解码 1ms …` | **删掉**（`#vtd-diag` + JS 写入 + `.vd-diag-line` 样式） |
| `VLM 逐路巡检：各路独立解码播放…本轮需 0.0 秒（0 路 × 1.3s）` | **删掉**（`#vtd-rr-est` + 整段 `<p>` + `.vd-hint-line` 样式） |
| 检测状态里的 `队列 N` / `尺寸 384` / `轮转第 N 轮` / `跳过 N` | 换成 `检测中 · 正在巡检 N 路` |
| 通道角标 `#0 24.97fps` | 只留 `#0`，**仅在出错时**补 `· error` / `· ⚠N` |
| 事件行的 `ch11` / `帧 161` / `#255` | 换成 `通道 12`，去掉帧号和事件 ID |
| `384x384 (更快)` / `768x768 (更准)` | 换成 `快速` / `精细`（value 不变） |
| `2/6 路正常 · 960x540` | 换成 `6 路视频 · 全部正常`，不暴露分辨率 |

`VTD_DIAG`（`window.__vtdDiag`）**保留** —— 它不在界面上出现，
是留给 DevTools / 自动化测试的调试通道，不是给客户看的东西。

替代这两段删掉内容的是画面上方的**状态药丸**（圆点 + 文字）：
`data-state` 驱动颜色（`idle/ok/run/warn/error`），一眼看出
"视频通没通 / 检测跑没跑"，且不含任何数字细节。

### 踩过的坑：`backdrop-filter` 会把渲染进程打满 1 核

给通道角标写了 `backdrop-filter: blur(2px)` 之后，**headless 浏览器渲染
进程持续占满一个核**，整页跟着变卡，playwright 的跨进程调用也会被拖到
看起来像"卡死"（同一页面 `evaluate` 平时只要 0.1s）。

原因：backdrop-filter 必须**回读并模糊背景**，而画面是 25fps 持续重绘
—— 单路 25 次/秒，6 路 150 次/秒。GPU 合成时无所谓，**软件渲染下就是
实打实的 CPU 开销**。

→ 半透明底色（`rgba(0,0,0,.62)`）已经够看，去掉 `backdrop-filter`。
> 教训：给**持续重绘**的元素加视觉效果（blur / backdrop-filter /
> mix-blend-mode / 大阴影）之前，先问它在软件渲染下每帧要付多少代价。
> 判断方法：`ps -eo pcpu,args | grep -- --type=renderer` 看渲染进程是否
> 长时间贴着 100%。

### 踩过的坑：想给宫格凑 16:9 靠 `aspect-ratio` 是无效的

6 路时格子是 334×293（≈1.14）而画面是 16:9，`object-fit: contain` 会在
**每个格子内部**留上下黑边。试过在 JS 里按 `cols/rows` 反推
`grid.style.aspectRatio` 让格子正好 16:9 —— **完全不生效**：`.vd-layout`
是 `align-items: stretch`、`.vd-stage-col` 是 flex column，grid 作为
flex 子项会被拉满行高（实测 1 路是 1002×586，而不是 16:9 应有的 564），
行内 `aspect-ratio` 直接被覆盖。

**结论**：格子宽高比 = `(stage宽/cols) : (stage高/rows)`，只有
cols==rows（1/4/9/16/25 路）才正好 16:9。6 路是 3×2，**必然有留边，
这是格子数的固有结果，不是 bug**。别再为此加无效代码。

要消除的是**上一层的黑边**（stage 上下），做法是 `grid-auto-rows: 1fr`
+ 格子 `aspect-ratio: auto`（见 app.css），让格子填满 stage：
实测 6 路 `stageBlackPx = 0`，黑边从"stage 上下"挪到"格子内部"，
而格子底色本身是黑的，看起来就是正常的监控墙。

### 本机跑 UI 自动化测试的四个坑（RK3588 + headless chromium）

这几条让测试**假死**了十几次，写下来省得再踩：

1. **取消后台任务不会回收 chromium**。被 kill 的 playwright 会留下
   孤儿浏览器各占一核，几轮下来机器打满，之后每次测试都像卡死。
   跑之前先清：
   `ps -eo pid,comm | awk '$2~/chrome|headless/{print $1}' | xargs -r kill -9`
2. **不要用 `page.query_selector()` / `locator.click()`**。actionability
   的 rAF 轮询在这台机器上不可靠，会无限期挂住；同一页面
   `page.evaluate()` 只要 0.1s。一律用 `evaluate` 派发 DOM click，
   可见性在 Python 侧轮询（`evaluate` 读 classList），别用
   `wait_for_function`。
3. **不要用 CSS 把向导容器藏起来**。framework 靠向导的 `unmount` 事件
   才摘掉 `.page` 的 `hidden`，直接 `display:none` 的话页面永远 0 尺寸 ——
   几何测量全是 0、截图全白，看着像页面坏了，其实是测试写错了。
4. **`kill -9` 浏览器会留下坏会话**。服务端解码线程随之死掉且不复位，
   下一轮 `open()` 出来的通道是 `error` 状态、`out_w/out_h` 还会退化成
   `960x960`（源尺寸没读到的兜底值）。判断是不是真出问题，先用 curl
   干净重开一次：`POST /open` 后 `total_frames` 是否在涨。

另外：**把整段流程放进一个 async `evaluate` 里**（页内 `await sleep`
轮询），跨进程往返越少，卡死机会越少。

### 回归

| 测试 | 文件 | 检查点 |
|---|---|---|
| Layer 1+2 | `scripts/verify_wizard_bringup_skip_adopted.py` | 2/2 |
| Layer 1 UI | `scripts/verify_probe_env_local_only.py` | 5/5 |
| Layer 3 | `scripts/verify_find_by_model_priority.py` | 10/10 |
| 全量 wizard | `scripts/smoke_wizard_all.py` | 6/6 |

如果将来 peer adopted 也算"已就绪"(如多板共享算力场景),三层都得
放宽;别只改一处 —— 前端看到 ✓ 但 server 仍 403 会让用户迷惑。

## 修改守则

改动某个功能的时候，不影响其它功能；如果需要改动到公共功能，需要自动
跑真实测试每个关联的功能项是否都正常。

- 改动只触及目标功能及其直接依赖，不顺手 refactor 无关代码。
- 公共功能改动（`app/core/compute/registry.py`、LLM 会话栈、
  `app/core/llm/detector_base.py` / `app/core/video/session.py`、
  任何 `app/core/**` 共享 service、`wdndds` cshim、`wdnai._librknn` shim
  等被多个调用方共享的模块）必须跑所有关联功能的真实测试（不止单元测试），
  不允许"测试跳过"或"代码改对了就行"。
  (原 surveillance 已下线,见"App 插件系统"段落的"零耦合铁律")
- 失败 → 修到全通过再交；提交前在 PR/commit message 列出跑了哪些
  feature 的 smoke test + 结果。
- **`config.jsonc` 不得 commit** —— 设备级运行时配置，由
  `ConfigStore._seed_defaults()` 在文件缺失时自动落盘（device_name /
  model_path / 算力实例绑定 / 提示词覆盖等）；每台机器独立维护，**不**
  入版本库。已在 `.gitignore` 排除。改动 preset 默认值只改
  `MODEL_PRESETS` / 相关 spec factory / `_seed_defaults`，由运行时
  自动生成新文件；agent 不要"先编辑 config.jsonc 再 commit 进去"。

## wdndds RPC size limits (v0.7+)

方案B (string<65535> → sequence<octet>) 治本了 IDL 字段级别的截断。
但 **per-message 仍有 1 MB 硬上限**：

* `wdndds_cshim.cpp:427-428` 显式设 `max_serialized_type_size = 1048576`
* 与字段类型无关——是整个 CDR 编码后消息的字节数
* **当前 wdnwebui 调用**（6s 16 kHz mono audio chunk = 192 KB raw → 256 KB envelope）
  富余 4×；不会撞
* **如果 `audio_analysis.CHUNK_SECONDS` 调到 ≥ 18 s**（≈ 768 KB envelope）
  或 **单图 > 1 MB base64 inline** 时会撞
* 撞的症状是 **loud**（FastCDR serialize 失败，客户端拿到 `{"ok": false, ...}`），
  不是 v0.7 之前的 silent 截断
* 详见 wdndds 仓库 `docs/limits.md`；要扩到 4 MB / 16 MB 时
  同步调 `apply_bounded_resource_limits()` 的 max_total，否则会触发
  v0.6.0 修过的 RSS 漏问题
* **当前不动**——6s/12s/18s 都安全，撞了再扩

## 单卡 NPU 容量约束（两张卡 ≠ 一张卡装两个）

实测结论（v0.17，RK3588 8G + NPU×2）：

* **同一张 NPU 卡上不能同时加载两个模型**。第二个 worker 会在
  `load` 阶段 `rc=-6`（SIGABRT），stderr 为空。主进程侧只看到
  "failed to load"，`_instances` 里翻成 `error`。
* **两张卡各绑一个模型完全正常**。`qwen3-vl-4b @ npu0` +
  `qwen2.5-omni-3b @ npu1` 双双 `running`，两路真机推理并发跑通
  （见 `scripts/verify_dual_card_smoke.py`，3/3）。
* 这不是 wdnwebui 的 bug，也**不是内存不够**：worker 实际 RSS 只有
  200–400 MB，系统 `MemAvailable` 还有 5+ GB。`memory_estimate_mb`
  （1900–5500）描述的是 **NPU 侧权重 + 激活池**预算，跟 DDR 无关。
  RK3588 单卡的 NPU 内存池装不下两个模型。
* **操作含义**：算力页启第二个模型时**换一张卡**，不要在同一张卡上
  叠加。实测 3500 MB（`qwen3-vl-4b`）+ 2500 MB（omni）同卡必崩。
* **为什么主进程不做预检**：worker 的 abort 没有可读原因码，主进程
  拿不到「NPU 池还剩多少」信号（`/proc/rknpu/load` 在本板为空），
  只能靠 `sum(memory_estimate_mb) vs 单卡容量` 猜。宁可让用户撞到
  一次 loud 的 `rc=-6`，也不加一个会误杀合法配置的猜测性检查。
* **回归**：`scripts/verify_dual_card_smoke.py` —— adopt-only（绝
  不 spawn），按 `capability_types` 分派，跑 UI 已经启好的两个实例。
  `verify_stage3_smoke.py` 则相反：它独占 NPU 且**默认绑 npu 0**，
  所以 UI 上已有模型时它必 `rc=-6` —— 两者是互补的，不是替代。

### 同一个模型跑两张卡（v0.19，算力页「运行在」多选）

**成立且已真机验证**：`qwen3-vl-4b` 同时加载在 npu0 + npu1，两个独立
worker（pid 不同）双双 `running`。约束是「**一卡一模型**」，不是
「一模型一卡」——同一个模型可以占多张卡，不同模型也可以各占一张。

数据层本来就支持：`ModelInstance.key = (npu, model_id, source)` 带卡号，
`--local-only` 让每个 worker 的 DDS service 名带 PID
（`wdndds/rpc/<id>/worker-<pid>`），两个实例不会撞。真正要改的是四处
单例假设，见下。

**`LLMRPCSession` 绑定 worker 身份，所以没有透明负载均衡**。
`_setup_rpc_client` 按 pid 建 Service，session 对象本身带着「我是哪张卡的
哪个 worker」这个身份。想打另一张卡就必须显式指名 —— 这也是
`list_sessions()` 返回**列表**而不是单个 session 的原因：

```python
# 枚举所有能服务该 capability 的端点（含/不含远端由 include_remote 决定）
handles = get_registry().list_sessions("swcap_video_describe", include_remote=False)
handles = get_registry().list_sessions("swcap_video_describe",
                                       include_remote=True,     # 也含 LAN peer
                                       model_id="qwen3-vl-4b")
for h in handles:
    h.model_id, h.npu_index, h.source, h.pid, h.is_remote, h.worker_key
# 不指名时用 get_session / get_session_for_model（可传 npu_index 指名）
```

`include_remote` 默认 `False`（只返回本机）的原因见 `get_session` 的
docstring：视频消费方走 `/dev/shm` path-list 传帧，跨机 worker 读不到。

### 只有 npu0 / npu1 能跑模型（v0.20 实测）

`/api/compute` 会枚举出 **4 张卡**，但 **npu2 / npu3 的 device_id 是
`180B6D6C…` 这种 USB 序列号形态**，在它们上面加载 rknn 模型会在
`VLMBackend._load_vision_variants` 处抛
`RuntimeError: vision init_runtime failed: …vision_*.384x384.1828.rknn`
（LLM 部分的权重 sync 能走完，vision 的 `init_runtime` 才失败）。

**只有 npu0 (`0001:11:00.0`) / npu1 (`0003:31:00.0`) 两张 PCI 卡能跑模型。**
npu2/npu3 只在 RGA / VPU 侧可见，对算力页来说它们是「枚举得到但用不了」的卡。

配合上一节的「一卡一模型」：要在本机额外起一个模型，只能先把目标卡上的实例
停掉，再 `--npu 0` 或 `--npu 1` 指过去。**不存在「随便找个空卡跑」这种可能。**

## 多路 MPP 解码：单进程多线程，不要多进程（v0.20 实测）

为 32 路 720P 监控做的 16 路解码压测（`.venv/src/wdnai/examples/
multi_video_vlm_demo.py`，704x576 H.264 25fps × 16 路）：

| 场景 | 结果 | 失败 |
|---|---|---|
| 16 路 @25fps 节流，单进程多线程 | **397.6 fps**（每路 24.8） | 0 |
| 16 路无节流，单进程 | 751 fps | 0 |
| 16 路无节流，4 进程 × 4 路 | RGA `imresize failed` | 每进程 1 路挂 |
| 16 路无节流，8 进程 × 2 路 | RGA `imresize failed` | 每进程 1 路挂 |

**三条与既有认知相反的结论：**

1. **`wdnav/samples/multi_decoder.py` 的 docstring 说错了。** 它写
   「`_mpp_lock` 会让线程串行，要真并行得跑多进程（一进程一路）」。
   实测不成立：`_mpp_lock`（`wdnav/mpp.py:99`）是**每次 C 调用级别的短临界区**，
   不是整个解码周期的锁。16 路线程稳态 397 fps、零失败。**单进程多线程是唯一
   正确解法**，它推荐的多进程方案反而会让 RGA 硬失败。

2. **瓶颈是 RGA 转换，不是 MPP 解码 —— 这点实测钉死了。**

   把 `_mpp_lock` / `_rga_lock` 换成带计时的包装器，量 64 路 @25fps
   （1280x720 → 384x216）的锁占用：

   | 配置 | 聚合 fps | 每路 | MPP 锁占墙钟 | RGA 锁占墙钟 |
   |---|---|---|---|---|
   | **纯 MPP 32 路（完全不做 RGA）** | 2722 | **85.0** | 47.8% | — |
   | **纯 MPP 64 路（完全不做 RGA）** | **2745** | **42.9** | **45.8%** | — |
   | 解码+RGA 64 路 @384x216 | 672 | 10.5 | 16.9% | **74.0%** |
   | 解码+RGA 64 路 @192x108 | 768 | 12.0 | 18.2% | 73.5% |
   | 解码+RGA 64 路 @96x54 | **1564** | **24.4** | 36.9% | 45.7% |

   * **MPP 侧余量巨大。** 64 路纯解码每路 42.9 fps，是 25fps 目标的 1.7 倍，
     MPP 锁只占 46% 墙钟。RK3588 VPU 本身能远不止这个数（rkvdec 支持 4K@60），
     这里的天花板是 `_mpp_lock` 这个进程级互斥 + GIL，不是硬件。
   * **RGA 是真瓶颈**，而且是**每调用开销**不是固定 ops/s 上限：输出从
     384x216 缩到 96x54（写入字节 243KB→15KB），聚合从 672 涨到 1564 fps，
     RGA 单次持锁从 0.275ms 降到 0.073ms。**RGA 开销随输出字节数走。**
   * **这个瓶颈里有一部分是 wdnav 自己造的**：`rga.py:109` 用一把进程级
     `_rga_lock` 串行化**所有** RGA 调用，而同一处的注释明写着
     "The kernel side does the real work in parallel on the hardware cores" ——
     硬件本来能并行，是 wrapper 为了"简单"给锁死了。所以"能撑几路"取决于
     **输出尺寸**和**是否绕过 `_rga_lock`**，跟 RGA 硬件规格无关。

   ⚠️ 纠正一个曾经的错误结论：早期把「16 线程无节流时跑出 751 ops/s」当成
   了 RGA 上限并线性外推。实际 64 路 @96x54 能跑 1564 ops/s，说明那个数只是
   当时 16 个 Python 线程的管线吞吐，**不是上限**。别再按单点外推。

3. **RGA 次数 == 解码帧数，绕不开。** `VideoCapture.read0()` 在
   drain-first backpressure 补丁里被改成**内部强制做一次 RGA**（`video.py:619`
   写死 `frame._cached_bgr = frame.numpy()`，随后立刻 `release_frame`）。
   `read()` 只是 `read0()` + `numpy()` 的薄封装，所以**两条路都躲不掉**。
   要真上 32 路 720P@25fps（800 RGA ops/s）就会顶到 ~750 的墙上，
   得先给 wdnai 加一条免 RGA 的解码路径（把 `read0` 里的强制转换改成惰性），
   或把每路采样帧率降下来。

**由此得出唯一可行的采样姿势**（demo 已实现）：解码线程用
`ret, frame0 = cap.read0(w, h)` —— RGA 在那一刻做完并缓存成 ndarray；
采样器只从 latest 槽原子取引用再取 `frame0.numpy()`，后者命中
`_cached_bgr` 缓存直接返回（`mpp.py:503`），**不产生第二次 RGA、不产生
额外拷贝**。`ImageAttachment` 原生支持直接吃 `read0()` 返回的
`DecodedFrame`，无需自己 `.numpy()`。

> 既然 RGA 次数 == 解码帧数，那**按需解码**就是唯一出路：每路每 T 秒取 1 帧
> → RGA ops/s = 路数 / T。16 路 T=2s 只有 8 ops/s，32 路 T=1s 只有 32 ops/s，
> 都极其宽松。**不要按 fps 常驻全速解码**——那是 VPU 和 RGA 双双浪费，
> 而且描述一张画面根本不需要 25fps 的新鲜度。

### 16 / 32 / 64 路 720P 实测（v0.20，全部达标，瓶颈在 RGA 不在 MPP）

`/userdata/assets/uploads/rk-*.mp4` 实测全是 **704x576**，不是 720P，用它
测不出 720P 的账。`examples/make_720p_assets.py` 把 rk-0..rk-15 经
**MPP 硬解 → RGA 缩放 → MPP 硬编**转成 1280x720 H.264（全程无软编解，
符合本文件设计原则 2），产出在 `/userdata/assets/720p/`，端到端 124 fps。
⚠️ 源 4:3 → 目标 16:9 是 RGA 直接拉伸，画面横向宽约 1.45 倍 —— 对测吞吐
无影响，但**评估描述文字准确度时要记得这个形变**。

纯解码压测（`multi_video_vlm_demo.py --video-dir /userdata/assets/720p
--channels N --repeat M`，RGA 输出 384x216，节流 25fps）：

| 路数 | 解码合计 | 每路 fps | 达成率 | 错误 | RSS |
|---|---|---|---|---|---|
| 16 路 | 383.1 fps | 23.7~24.4 | 99% | **0** | 855 MB |
| **32 路** | **797.1 fps** | **24.8~25.0** | **99.6%** | **0** | ~1.1 GB |
| 64 路（输出 384x216） | 747.2 fps | **11.4~12.1** | 46% | **0** | 1515 MB |
| 64 路（输出 96x54） | 1564.2 fps | **24.4** | **98%** | **0** | 846 MB |

**32 路 720P@25fps 达标且零错误。**

**64 路也能跑满 25fps —— 但前提是把 RGA 输出缩到 ~96x54。** 同样是 64 路
@25fps：输出 384x216 时每路只有 11.6 fps（不够 25），输出 96x54 时每路
24.4 fps（达标）。**决定 64 路成立与否的是 RGA 输出尺寸，不是路数本身**
—— 因为纯 MPP 解码时 64 路能跑到每路 42.9 fps（见上表锁测量）。

**降级方式是掉帧，不是失败。** 上面所有配置（16/32/64 路、384x216 到 96x54）
错误数一律为 0。超出能力时表现为每路 fps 下降，不会抛 RGA 失败或解码错误。

> **监控场景根本不需要 25fps。** 每路每 T 秒取 1 帧的话，RGA 调用/s =
> 路数 / T。32 路 T=2s 只有 **16 次/秒**，比 720p@25fps 所需的 800 低 50 倍。
> 32/64 路之所以会顶到 RGA 墙，纯粹是因为 demo 为了压测解码在按源帧率持续
> 解码 —— 真实业务不会这么跑。**所以 32 路这个目标非常宽裕；真约束在 VLM
> 推理侧（32 路 × ~0.85s/路 = 一轮 ~27s），不在解码侧。**

**RGA 输出尺寸是决定成败的参数，不是画质参数。** 720P 的读入字节是 SD 的
2.27 倍，但输出压到 96x54 后总搬运量几乎不变。如果当初按 640x360 输出，
1382 + 691 = 2073 KB/帧，32 路就会直接塌到墙上。

> 想突破 32/64 路天花板，**正确的方向是改 wdnav 而不是换硬件**：
> (a) 让 `VideoCapture.read0()` 的 RGA 转换**惰性化**（现在是无条件写死在
> `video.py:619` 的 `frame._cached_bgr = frame.numpy()`），采样时才转；
> (b) 或者收窄 `_rga_lock` 的粒度 —— 硬件本来能并行（见上）。
> 纯 MPP 侧已经证明 VPU 有 1.7 倍以上余量，VPU 不是限制。

### RGA 输出尺寸会决定 VLM 用哪个 vision 变体（v0.20 实测，坑）

`Qwen3-VL-4B/` 下**两个 vision 变体都在**：`vision_*.384x384.*`（权重 61MB）
和 `vision_*.768x768.*`（权重 252MB）。wdnai 的选变体规则是
**「挑 `image_size` 最接近输入图最长边的那个」**（`backends/vlm.py`
`_pick_variant` 的自动分支，注释原文 *minimise |image_size - max(h, w)|*）。

所以**喂多大的图不是随意的，它直接决定推理快慢**：

| RGA 输出 | 最长边 | 选中变体 | 单帧 VLM | RGA 写出/帧 |
|---|---|---|---|---|
| 640×360 | 640 | **768**（\|768−640\|=128 < \|384−640\|=256） | **2.06s** | 691 KB |
| 384×288（4:3 源） | 384 | 384 | **0.90s** | 353 KB |
| 384×216（16:9 源） | 384 | 384 | ~0.9s | 243 KB |

**图给大不但没提高精度，反而两头亏**：VLM 慢 2.2 倍（768 变体权重是 384 的
4 倍），RGA 带宽还多写 2 倍。实测 demo 从 640×360 改到按源宽高比缩到最长边
384，单帧 1.90s → 0.90s，16 路一轮 30.5s → 14.4s。

**规则：RGA 输出尺寸要让最长边正好等于想用的 vision 变体边长**，别随手给。
另外 `letterbox_resize` 是**保比例**的（长边缩放到 size，短边居中、灰边
114 填充），所以**按源视频自己的宽高比缩**比硬编码 16:9 更划算：4:3 的
384×288 在 384×384 画布里有效画面占 75%，而 16:9 的 384×216 只占 56%。

> 那段「640 → 384 让单帧从 1.90s 降到 0.90s」是**纯解码压测 demo**
> （`multi_video_vlm_demo.py`）为了跑满路数故意做的取舍，不是业务默认值。
> **业务路径的送检尺寸必须与单路 `VideoSession` 对齐（720p 源 → 640）**，
> 否则多路就是单跑了一个更差的 vision 变体。
>
> 想更细的描述可以 `--vision-size 768`，但要先接受 ~2.2 倍耗时 ——
> 32 路监控场景下"每路每几秒一句话"比"每路每十几秒一句更细的"重要得多。

> **两件事别再混为一谈**（v0.23 踩过）：
> * **送检尺寸** —— 该对齐的是 *vision 变体边长*，与单路一致就是 640。
> * **热路径输出尺寸**（RGA 一次出图、既送显又是冷路径的源）—— 越大越贵，
>   640×360 单帧整链 4.34ms vs 384×216 1.94ms，这才是 `MAX_TOTAL_DECODE_FPS`
>   从 800 降到 660 的原因。

### 这块踩过的三个坑（都造成 rc=-6 或 500，勿回退）

1. **`/proc` 孤儿扫描会把自己的 worker 当孤儿接管**。
   给卡 1 启第二个实例时，`_find_workers_by_name(<model>)` 必然命中刚给
   卡 0 spawn 的那个（needle 只认 `--model <id>`，分不清是不是我们的）。
   不排除它 → 注册成 `adopted_local` → 把卡 0 的条目从 `_instances` 踢掉
   → 两个条目指向同一进程 → bind 让同一个 worker load 第二份模型。
   实测症状：`POST [0,1]` 返回两个 **pid 相同** 的实例。
   修法：算出 `tracked_pids`（本 registry 已跟踪且存活的 spawned），从
   `orphan_pids` 里剔掉，全是我们的就照常 spawn 新的。

2. **`npu_index` 不能既用来匹配又用来 spawn `--npu`**。
   算力页启用走「spawn（不带 `--npu`）→ RPC `action=load`」。若顺手把
   `npu_index` 透传给 spawn，worker 会带 `--npu` **自己 eager-load**，
   同时 RPC load 又到了 —— 两条 load 争同一个 session slot，worker
   `rc=-6`，而且**所有**模型的启用都会挂（不只双卡场景）。
   修法：拆成 `npu_index`（只管匹配已有实例）+ `eager_load: bool`
   （才决定 `--npu`，仅 autostart 为 True）。算力页显式传
   `eager_load=False`。

3. **新 spawn 的 worker，`load` RPC 的 5 s 超时不够**。
   第一台 worker 的 endpoint 早就被 discovery 匹配上了，所以 5 s 一直够用。
   但第二张卡的 worker 刚 spawn 几秒，它的 per-PID service 还要过
   SEDP/SPDP 才连得上，裸 5 s 会回 `{"ok": false, "error": "timeout"}`——
   worker 本身完全健康。修法：`_call_load_with_warmup()` 有界退避
   5 s → 15 s → 30 s 共 3 次。真实 load 失败返回的是带原因的 `ok: false`，
   不会被误重试。

**诊断提醒**：测试期 `kill -9` 会让板子状态漂移，之后**任何**模型单卡
启用都会 `rc=-6`，看起来像代码坏了。判别办法是跑
`scripts/verify_worker_split.py`（它自己 spawn + load，完全不经过
registry）—— 它通过就说明板子健康，问题在主进程侧。清场请走
`DELETE /api/compute/instances` 或重启服务。

## 算力注册中心 — per-model-subprocess RPC (v0.4)

v0.4 重大重构：每个 (npu_index, model_id) 实例现在跑在**独立
compute_worker 子进程**里，跨进程通过 `wdndds` cshim RPC
（Request/Reply over RELIABLE Pub-Sub）通信。架构改造的关键点：

* **`wdndds 0.3` = C++ cshim + ctypes Python wrapper**。
  抛弃了 v0.2 的 bundled python-fastdds SWIG 绑定；
  C++ 源码在 `wdndds/cshim_src/`，在 `pip install wdndds` 时通过
  `wdndds/_build.py` 的 setuptools `build_py` 子类编译成
  `libwdndds_cshim.so`。运行时链接系统 `libfastrtps2.9` /
  `libfastcdr1`（apt install libfastrtps-dev libfastcdr-dev）。
* **每个 model = 1 个 compute_worker 进程**。`ComputeRegistry.start
`     ` 在主进程 spawn 一个 `python app/core/compute/workers/compute_worker.py
       --model <id>` 子进程 —— **所有模型共用这个入口脚本**，具体造哪个
       spec 由 `workers/<id>.json` 的 `spec_factory` 决定（子进程默认不
       加载模型 —— 等首次 RPC 到达时再 lazy
       `LLMLocalSession(model=spec, npu=k)`）。
* **主进程持 RpcClient**：`get_registry().get_session(cap)` 返回
  `app/core/compute/proxy.RpcSessionProxy`(原 `app/services/rpc_session_proxy.RpcSessionProxy`),内部转发
  `reset()` / `respond()` 到 worker 的 `wdndds/rpc/<model_id>` service
  topic，再从 `<service_name>/reply` shared reply topic 拿回 JSON reply。
  业务侧（detector / tts / vfa / text_chat）调用方式**不变**
  —— `RpcSessionProxy` 保持 `wdnai.LLMSession` 的 `.reset()` /
  `await .respond(prompt, attachment, ...)` 表面。
* **服务端 lock**：worker 进程内一个 cshim pump 线程串行消费请求，
   处理完 reply 完毕后才接下一个；多个 worker 并行是允许的
   （不同 NPU / 不同 model 实例各跑各的）。跨进程互斥锁在 v0.4 不需要
   —— 主进程侧 `RpcSessionProxy.respond` 内部 1 个 ThreadPoolExecutor
   串行化同一 instance 的并发请求。

* **VLM 图像传输：raw BGR via `/dev/shm` path-list**：
  `RpcSessionProxy._encode_attachments()` 把每个
  `Attachment.images` / `Attachment.frames` BGR ndarray 走
  `_bgr_ndarray_to_path()`：
  1. 主进程 `arr.tofile()` 写到 `/dev/shm/wdnwebui_vd_<uuid>.bin`
     （tmpfs RAM，板上 4 GB 充裕）
  2. wire 上只传 `{"data_path": ..., "shape": [h,w,3], "dtype": "uint8"}`
     （~150 字节，远小于 FastDDS builtin per-message 限额）
  3. worker `np.fromfile().reshape()` 还原 byte-exact 帧，立刻 `os.unlink()`
  4. boot + atexit 各扫一次 `/dev/shm/wdnwebui_vd_*.bin` 防泄漏
     （boot sweep 默认 `max_age=1h`，atexit 用 `max_age=0` 全清）

  为什么走 path-list 而不是 base64 inline（之前是 JPEG q=85 + base64）：
  * **无损**：480×640 单帧 max 像素差从 JPEG q=85 的 96/255 → 0。
    VLM 4B 类模型对像素抖动敏感，JPEG 块状 artifact 在 1080p + 768
    variant 上让模型误判 / 输出退化（实测报"泰文 / 乱码"）。
  * **LAN peer 兜底**：内置 FastDDS transport（`use_builtin_transports=true`，
    见 `cshim_src/wdndds_cshim.cpp:312`）自动 SHM（同机）+ UDPv4/TCPv4（跨机）。
    跨机时 proxy 收 `ModelInstance._source == "adopted"` 自动设
    `is_remote=True` → 切回 `_bgr_ndarray_to_b64`（JPEG+base64）。
    path-list 在 caller 自己的 /dev/shm 上才能被同机 worker 读，
    LAN peer 拿不到 → 自动 fallback。
  * **wire 体积 + DDS 开销**：wire 不带图本身，每帧省 6.2 MB DDS
    serialize / 反序列化（builtin TCPv4 兜底虽能塞但每帧一遍不便宜）。

  用途覆盖：`WdnaiDetector`（音视频描述 / 视频目标检测）+ 视频文件分析
  共用同一 `RpcSessionProxy`，都走 path-list。视频内容检索（embed_video，
  VAS 页 + 微信管线）也走同一条 `_bgr_to_path` / `decode_attachments`
  raw-BGR path-list —— **必须传 ndarray 帧列表**；传路径字符串会被 worker
  端 `decode_attachments` 当 base64 解析失败 → `TextAttachment` → 被
  `ImageAttachment` 过滤 → `frames=[]` → `0/N` 全失败（详见检索侧章节）。
  音频走 base64（PCM bytes 不是图）。
  串行化同一 instance 的并发请求。
* **`boot_compute_services` 在 boot 默认开启**。`app/main.py`
  `create_app` 把整个调用挪到 daemon thread
  (``threading.Thread(target=..., name="compute-boot", daemon=True).start()``)，
  防止冷启动时 ``_probe_service`` 的 3 × 5s race-recovery timeout
  把 ``create_app()`` 阻塞几十秒、uvicorn bind 8000 等不到 —— 浏览器
  GET / 立即出，probe/adopt/spawn 在后台慢慢跑（前端 1Hz 轮询
  ``/api/compute`` 自动反映）。函数本体在 v0.10 重写：以
  **DDS RPC `info` 探测 + `/proc` 先行** 为真源，对
  ``MODEL_PRESETS`` 全集做四步走——
  1. **`_find_workers_by_name(model_id)`** 先扫 ``/proc/*/comm``
     看本机有没有 ``worker:<alias>`` comm 的进程；
     **未命中 → 跳过 probe**（避免冷启动 5 × 17s ≈ 85s 的纯
     timeout 等待），按 config 走 spawn / skip；
     **命中 → 走 probe + adopt**（拿 hostname / npu / status）。
  2. 探测 + adopt：``_adopt_existing_worker`` 内部走
     ``_probe_service`` 的 3 × 5s race-recovery 路径，只对真的有
     活 worker 的 model 才会被探测到。
  3. 本机无 worker + config 里有 → ``_spawn_compute_worker``；
     本机无 worker + config 里没 → skip（UI ``unavailable``）。
  4. 本机有 worker + probe 失败 + config 里有 → fall through
     ``_spawn_compute_worker``，它的 dedup 拒绝 spawn sibling 并
     登 errored inst（避免 NPU 显存吃双份）。
  **LAN peer 不在 `/proc` 里** —— boot 不主动探测远端 worker；
  兜底走运行时首次请求 ``_ensure_worker_for_model`` → probe → 若
  peer 响应则 adopt；否则本机 spawn。单板部署无影响，多板 LAN 的
  peer boot-time discover 退化到 runtime discover。worker 起来后
  只注册 DDS endpoint，不 build LLMSession；npu_index 由用户在算力
  配置页选 NPU 后通过 RPC `{"action":"load","npu_index":N}` 传入。
  `_probe_instance` daemon 线程周期性 RPC `info` 同步 worker
  状态（`not_loaded` / `loading` / `running` / `error`）。
* **`get_session_call_lock()` 留作 no-op 兼容旧调用点**
  （仅 `video_analysis_search.py` 还在 import）。

### 已知约束 / 待完善

* **本机 FastDDS 2.9 bug**：在 Debian bookworm 板上的系统
  libfastrtps2.9 包 `DataWriter::enable()` 会 SIGSEGV
  （在 `get_payload_pool()` 里），所以当前主机跨进程 DDS 收发都
  跑不通。这是系统库的问题，**不是 wdndds 架构的问题**——
  改用 SRIO / 共享内存 / Unix socket 等替代传输时，进程间通信照常
  跑得通。架构在协议层是干净的：每个 worker 一个 service，每个
  service 一个 request topic + 一个 shared reply topic。
* **流式 vs 一次性**：`RpcClient.call` 首版只支持一次性 reply
  (`{"ok": True, "data": {...}}`)。API 层面 `call_streaming`
  已经预留签名但未实现；如果加回来需要 cshim 多一条 streaming
  topic 通路。
* **多 NPU 跨子组件**：当前 `ModelInstance.npu_index: int` 单值；
  后续 vision@NPU0 + llm@NPU1 这种「一个 model 跨多 NPU」配置要
  等 wdnai SDK 改 spec + wdnwebui 解析 `npu_assignments: dict[str, int]`
  —— 当前每个 model 一个实例，所以 service_name = `wdndds/rpc
`     <model_id>`（**不带 npu 后缀**），单 model 多 npu 也在子进程
  内部决策。
* **同 model 不允许重复实例**：`compute_registry.start_one` 校验
  `(npu_index, model_id)` 不重复；UI  已经在加「instance_name」
  之前按 (npu, model) 唯一。
* **跨机 LAN peer reply-side /dev/shm 不可达（v0.15 短期降级 + 中/长期待办）**：
  `ComputeReply` IDL 5 个 string 字段（`text` / `level` / `tool_call` /
  `bbox_json` / `crop_b64`）受 FastCDR 默认 255 字节硬截断。worker 端
  reply 用 `/dev/shm` path-list（`/dev/shm/wdnwebui_cr_<uuid>.{jpg,json,txt}`）
  把 > 200 字节的 payload 写**本机** tmpfs、主进程**同机** `/dev/shm` 读回，
  绕开限制。跨机 LAN peer（`_source == "adopted"`，主机 hostname 不同）
  时 worker 写到本机 tmpfs、主进程读不到 → 之前 `FileNotFoundError`
  + VAS 拿到空 `_raw.text` → 占位事件（"不崩但功能全废"）。
  v0.8.30 的 `get_session` 本机优先 / `embed_video` path-list
  无 is_remote 守卫同源（见"get_session 本机优先"段）。

  **v0.15 的 LAN 降级已作废（v0.16）**：当年 `is_remote=True`（跨机
  worker）时，worker 会把 4 个 SHM 路由块（`crop_b64` / `result_data` /
  `structured` / `text`）**全部 drop**，因为对端读不到本机
  `/dev/shm`，而内联又会撞 FastCDR 255 字节硬截断。代价是 VAS Path 1
  garbage 占位、TargetDoc 从 `frame_bgr` 重裁、detector 拿默认值。

  现在没有这条降级：附件与长文本的 tmpfs 路由由 wdndds 两端统一负责，
  跨机自动回退 JPEG+base64（内容不丢，只是有损），不再整块丢弃。
  `scripts/verify_text_shm_path.py` 随 worker 侧 shm 路由一起退役。

  **方案 C（专用 topic 通路）已作废**：这一层现在就是 wdndds 的职责 ——
  `serve_local_session` / `LLMRPCSession` 两端各自把超限内容转成
  `/dev/shm` 描述符，`is_remote=True` 时退回 JPEG+base64。不需要
  wdnwebui 侧再造一条 topic 通路。

  **待办 — 长期（方案 B, 估 3-5 天, 动 wdndds 仓库）**：改
  `.venv/src/wdndds/idl/compute.idl` 把 `ComputeReply` 5 个 string
  字段改成 `sequence<octet>`（无界 bytes）+ 用 vendored fastddsgen
  重新生成 `computePubSubTypes.{cxx,hpp,ipp}` + wdndds cshim
  rebuild（`bootstrap_wdndds_native.sh`，~20s）+ 主进程 / worker
  Python 侧用 `bytes` 代替 `str` 走 wire。**治本**：根治 255 字节限制，
  LAN peer + local 自动支持任意大小 payload。需 wdndds 仓库 review
  节奏配合，独立排期。

* **VAS per-frame timeout 方案B (10s → 15s, 配套 wire RPC 10s → 15s)**:
  - 5c33fb3 设 5s → 后续 10s 都覆盖 qwen3.5-vl-4b schema-echo 健康路径
  - 但线上仍偶发 `(推理失败：超时 10s SDK 未响应)`, 真机 NPU 暂忙 /
    prefill 长 / 其他并发偷 NPU 让单次跑 10-12s
  - 方案B: 默认 15s 给 5s 余量, 跟 wire RPC 同步 (避免 wire 先触底
    留下 `discovery_pending` 误报错误)
  - 下一步: telemetry (commit cd74486 加的 _record_infer_duration 60s
    窗口 p95 告警) 收一晚真实 p50/p95/p99 数据决定 15s 是否够, 不够
    再调 (但若 ≥ 30s 就要查 NPU / 长 context 真因, 不应再加 timeout)
  - 真机 VAS 服务需 restart 才能让新默认值生效

### 内存模型：主进程 RSS 稳态 ~300 MB（修复后）

主进程 RSS 由三部分组成（`/proc/PID/smaps_rollup` 看）：

```
RSS = python_heap (~140 MB)
    + shared_libs  (~50 MB, fastdds.so/cv2/libmali/...)
    + anon rw      (~200 MB, FastDDS TopicPayloadPool + glibc arena)
    + shm files    (~12 MB, /dev/shm/fastdds_*)
```

**anon rw 段是 FastDDS 内部 TopicPayloadPool**（不是 Python 对象），
每个 `DataWriter`/`DataReader` 创建时按
`max_serialized_type_size × allocated_samples` 预分配 payload pool，
delete entity 后池不释放（topic 留在 `Participant::req_topics` map 里，
要等 `wdndds_participant_destroy()` 才整批 free）。

**两层防御把稳态 RSS 从 ~1.35 GB 压到 ~300 MB**：

1. **Service 生命周期**（`compute_registry.stop_instance_local`）—
   `hub.destroy_service(name)` 把 Service 从 `hub._services` 删掉，
   触发 `Service.destroy()` 清掉所有 repliers/requesters + C 侧
   `wdndds_service_destroy`。修复前每次 enable→disable 循环 +80 MB
   主进程 RSS。
2. **resource_limits 上限封顶**（`cshim_src/wdndds_cshim.cpp:213`
   `apply_bounded_resource_limits`）— 每个 writer/Reader 创建时设
   `allocated_samples=8, max_samples=16`（reliable）/
   `4/8`（best_effort），把单 entity pool 从 ~63 MB 压到 ~18 MB。

**剩余 ~200 MB anon rw 不能压**：
* ~265 MB 单段（malloc arena，没回 OS）
* 18 MB × N entities（bounded pool 自身）
* 65 MB + 35 MB × few（FastDDS discovery builtin endpoint cache，
  改不了）

**verifier**：连续 15+ 次 restart cycle 后 RSS 应稳定在 ±50 MB 范围内。
若 RSS 单调上涨，说明 Service 没被 destroy（看 `hub._services` 大小
或 `_instances` 的 `inst._svc` 是不是 None）；若 RSS 上限变高，说明
QoS 上限被改宽（看 `apply_bounded_resource_limits` 调用点）。

## TL;DR commands

```bash
# activate (do NOT use system pip — Debian PEP 668 blocks it)
source .venv/bin/activate

# one-time wdndds native libs (apt + stub + pip) — idempotent, run again is <1s no-op
sudo ./bootstrap_wdndds_native.sh

# run (auto-cleans stale uvicorn / compute_workers from previous unclean shutdowns)
sudo .venv/bin/python run.py             # 0.0.0.0:8000, reload on
sudo .venv/bin/python run.py --no-kill-stale   # opt out of stale cleanup

# sanity
curl http://127.0.0.1:8000/health
```

Endpoints wired so far: `GET /` (version), `GET /health`.

**Stale-process cleanup on startup.** `run.py` reaps leftover
**MAIN** processes from a previous unclean shutdown before binding
port 8000: uvicorn worker holding port 8000 (via `ss -tlnp`), the
`python run.py` ancestor itself, and uvicorn-reloader's
`multiprocessing.spawn` / `resource_tracker` children (defunct
zombies that inherited the LISTEN socket). SIGTERM with 3s grace,
then SIGKILL.

**Orphan `compute_worker.py` subprocesses are intentionally NOT
killed** — ComputeRegistry spawns them with
`Popen(start_new_session=True)` so they detach from the run.py
process group and survive it, and the new main process adopts them
on the next boot via `boot_compute_services()` →
`_adopt_existing_worker()` (DDS RPC `info` probe). Killing them
would force a 60s+ model reload on every restart. Detected orphans
are reported on stderr ("detected N orphan compute_worker
process(es) — preserved, will be adopted") so the user has
visibility.

Enabled by default; opt out via `--no-kill-stale` CLI flag or
`WDNWEBUI_KILL_STALE_ON_BOOT=0` env var. When disabled, both the
MAIN-process kill and the orphan-worker log are skipped (single
gate). Per `install_autostart.sh`, the production systemd unit
invokes `uvicorn` directly (not `run.py`), so the kill-stale
behaviour never runs under systemd.

## One-time setup that an agent will get wrong

Both internal SDKs live behind SSH on `git@10.10.10.10`. They are **not** in
`requirements.txt` (intentional — the host SSH key + network decide whether
the install succeeds). Run them by hand after cloning:

```bash
.venv/bin/pip install -e "git+ssh://git@10.10.10.10/wardner/wdnav.git#egg=wdnav"
.venv/bin/pip install -e "git+ssh://git@10.10.10.10/wardner/wdnai.git#egg=wdnai"
```

If `git clone` fails, the host either lacks SSH access to `10.10.10.10` or
the deploy key isn't loaded. Do **not** fall back to a public mirror — these
SDKs aren't published.

**wdndds also needs native libs built before `run.py` will boot.** Unlike
wdnav/wdnai, `pip install wdndds` does not ship pre-built `.so` files —
the `wdndds/_build.py` `build_py` hook is supposed to compile
`libwdndds_cshim.so` + `libwdndds_idl.so` from `cshim_src/` + `idl/`
into `src/wdndds/dds/_native/` at install time. The hook needs a
FastDDS 3.6 dev install with `libfoonathan_memory.so.0.7.2` reachable.

`run.py` ships a fail-fast preflight (`_ensure_wdndds_built`) that
checks for the two `.so` files at module-import time and `SystemExit(1)`
with a pointer to `bootstrap_wdndds_native.sh` if they're missing —
the web service refuses to come up rather than silently failing in
the daemon child process. Bypass with
`WDNWEBUI_SKIP_WDNDDS_BOOTSTRAP=1` for production images that
pre-stage the .so files out-of-band.

* Symptom when missing — `run.py` aborts with
  `wdndds native libraries not built. Run once on this host:
  sudo ./bootstrap_wdndds_native.sh`. Quick check:

  ```bash
  .venv/bin/python -c "from wdndds.dds._cshim import check_runtime; print(check_runtime())"
  # expect (True, None)
  ```

* `bootstrap_wdndds_native.sh` is the recommended one-shot setup.
  It installs the apt build deps (`libfoonathan-memory-dev`,
  `libfoonathan-memory0.7.2`, `patchelf`), writes a stub for
  `native/bin/nodesize_dbg` (partial native-install workaround for
  foonathan's cmake IMPORTED-target existence check), and runs
  `pip install --force-reinstall --no-deps .venv/src/wdndds` to
  trigger `_build.py` end-to-end. The script refuses to fall back to
  `apt install libfastrtps-dev` — that's FastDDS 2.x and incompatible
  with the v0.5 cshim ABI.

* `native/lib/libfastdds.so.3.6.2.0` + `libfastcdr.so.2.3.6` already
  vendored (typical partial build) — the bootstrap script handles
  this case automatically. Manual equivalent:

  ```bash
  sudo apt install libfoonathan-memory-dev libfoonathan-memory0.7.2 patchelf
  sudo .venv/bin/pip install --force-reinstall --no-deps .venv/src/wdndds
  ls .venv/src/wdndds/src/wdndds/dds/_native/libwdndds_*.so
  ```

  Note: `pip install -e ...` (editable) silently swallows `_build.py`
  hook failures on setuptools ≥64 — so always run the bootstrap
  script (or the manual non-editable `pip install` form) when
  rebuilding. The `WDNDDS_FASTDDS_PREFIX` env var (read by
  `_build.py`) is auto-detected from `native/`, no need to set it.

* **v0.6.1 unconditional rebuild** — both `_build.py::build_py.run()`
  (`.venv/src/wdndds/src/wdndds/_build.py`) and
  `bootstrap_wdndds_native.sh` now unconditionally rebuild on every
  invocation, no fast-path skip. The hook `unlink()`s the two
  `libwdndds_*.so` files at the start of every `pip install` /
  `pip install -e`; bootstrap no longer exits 0 when the `.so` files
  exist. Cost is the cmake build itself (~20s on this dev box).

  **Why**: the previous "if both .so files exist, skip pip" fast path
  in bootstrap was a silent correctness trap. After `git pull` of
  wdndds source that added new bindings to `_cshim.py` (commit
  `871d00f feat(wdndds): 新增 create_topic_writer/reader 流式分片助手`
  — added `wdndds_topic_writer_create` / `wdndds_topic_reader_create`
  + friends), the pre-existing `.so` would still satisfy the
  existence check, bootstrap would exit 0, and the user would later
  hit
  `AttributeError: .../libwdndds_cshim.so: undefined symbol: wdndds_topic_writer_create`
  when `ComputeRegistry._ensure_hub()` tried to instantiate
  `wdndds.dds._rpc.Hub`. End-user symptom was every compute card on
  the 算力配置 page failing with
  `RpcClient: wdndds Hub not available (fastdds init failed)`.

  **Workaround if you want the old behaviour back** — for production
  images that pre-stage the `.so` files out-of-band and don't want to
  pay the ~20s rebuild, gate the bootstrap with
  `WDNWEBUI_SKIP_WDNDDS_BOOTSTRAP=1` (see `run.py`'s
  `_ensure_wdndds_built`). Don't try to bypass bootstrap from inside
  the script itself; the hook in `_build.py` is always invoked by
  `pip install` and there's no environment knob to skip just the
  build step without bypassing the whole bootstrap.

* `native/` empty (fresh clone after `git clean -fd`, or new board) —
  run `bash .venv/src/wdndds/build_native.sh` first (20-30 min,
  builds FastDDS 3.6 + FastCDR 2.3 from source), then the bootstrap
  script.

The wdndds-owned `.so` files (`libwdndds_cshim.so`,
`libwdndds_idl.so`) are **never in git** — gitignored at both
`native/lib/` and `src/wdndds/dds/_native/`, built fresh per install.
A `git clean -fd` that wipes `native/lib/` is recoverable via the
bootstrap script; a `git clean -fdx` that also wipes
`idl/build/` + `cshim_src/build/` needs the full `build_native.sh` +
bootstrap dance.

## SDK native-`.so` rebuild policy — all three `.venv/src/` repos (since v0.3.1)

All three internal SDKs live behind SSH at
`git@10.10.10.10:wardner/{wdnav,wdnai,wdndds}.git`. Two of them
ship in-tree C/C++ that's linked into a `.so` and **dynamically
bound at import time** via ctypes (`_lib.<sym>.argtypes = […]` at
module top). The previous build hooks checked only C-source mtime,
which silently missed the case where a Python wrapper added a new
ctypes binding while the C source was unchanged — leaving a stale
`.so` that `import <sdk>` would crash on at startup with
`AttributeError: undefined symbol`. Two separate instances so far:

| SDK    | hook location                                   | stale symbol that crashed                                | fix commit | rebuild cost |
|--------|-------------------------------------------------|----------------------------------------------------------|------------|--------------|
| wdndds | `.venv/src/wdndds/src/wdndds/_build.py`         | `wdndds_topic_writer_create` (after `git pull` of 871d00f)| v0.6.1     | ~20s cmake   |
| wdnav  | `.venv/src/wdnav/src/wdnav/_build_wdnav.py`     | any new `_lib.rkv_xxx.argtypes` binding                  | v0.3.1     | ~5s gcc      |
| wdnai  | (no build hook — pure Python, no in-tree `.so`) | n/a                                                      | n/a        | 0            |

Both hooks now unconditionally `os.unlink()` the pre-existing
`.so` at the start of every `pip install` / `pip install -e` and
rebuild from scratch. Opt-outs:

* **wdndds** — `WDNWEBUI_SKIP_WDNDDS_BOOTSTRAP=1` gates the whole
  bootstrap script (the only path that triggers a rebuild for
  wdndds is the bootstrap, not the user's own `pip install`).
* **wdnav** — `--skip-wdnav-video-build` CLI flag or
  `SKIP_WDNAV_VIDEO_BUILD=1` env var (CI pre-built images).

The hard rule for users of these SDKs: **after `git pull` on
`.venv/src/<sdk>`, run `./update_rkwhl.sh`.** It `git pull`s all three
SDKs and forces `pip install -e --force-reinstall --no-deps` on each,
which is what actually triggers the rebuild hooks. The previous
heuristic of "only reinstall when `pyproject.toml` changed" missed
wrapper-only commits — root cause of the wdndds
`undefined symbol: wdndds_topic_writer_create` incident after commit
871d00f (`_cshim.py` gained new ctypes bindings, `pyproject.toml`
untouched). `update_rkwhl.sh` now rebuilds unconditionally on any HEAD
advance. For one-off sync of a single SDK, `pip install -e
--force-reinstall --no-deps .venv/src/<sdk>` is the minimum; on a
fresh host, `bootstrap_wdndds_native.sh` is still required for apt
deps + native lib vendoring.

## Layout

```
wdnwebui/
├── run.py                              # uvicorn.run("app.main:app", 0.0.0.0:8000, reload)
├── bootstrap_wdndds_native.sh          # wdndds .so build (apt + stub + pip; since v0.6.1)
├── app/
│   ├── __init__.py                     # __version__ = "0.9.2" (single source)
│   ├── main.py                         # create_app() — discover_cards + register APIs + boot_compute
│   │
│   ├── core/                           # 共享内核(原 app/services/*)
│   │   ├── manifest.py                 # 插件清单加载 + 校验 (v0.4 重构)
│   │   ├── config.py                   # ConfigStore (config.jsonc 原子读写)
│   │   ├── uploads.py                  # 公共 uploads 路径管理
│   │   ├── compute/
│   │   │   ├── registry.py             # ComputeRegistry — 见"算力注册中心"章节
│   │   │   ├── workers/                # 一 model_id 一文件夹 + manifest.json
│   │   │   ├── proxy.py                # LLMRPCSession 装配 + count_tokens 策略
│   │   │   └── jsonc.py                # 注释保留的 JSON 读写
│   │   ├── chat/                       # 文本对话核心
│   │   │   ├── agent.py                # run_chat_with_tools 主进程工具循环
│   │   │   ├── runner.py               # thinking/answer 切分 + 输出清洗
│   │   │   └── serial.py               # 进程级 RLock: 串行化所有 chat
│   │   ├── llm/
│   │   │   └── detector_base.py        # WdnaiDetector 基类 + VLMDoc/TargetDoc/LocatorDoc
│   │   ├── video/                      # 视频通用
│   │   │   ├── session.py              # 单源 VideoSession (VideoCapture + decode 线程)
│   │   │   ├── utils.py                # PyAV 探测 / 取帧 / 切片
│   │   │   └── cache.py                # VCS + 微信共享的 LanceDB 索引缓存
│   │   └── tools/
│   │       └── registry.py             # 全局 @Callable 工具注册中心
│   │
│   ├── apps/                           # 8 个独立 app 卡片(v0.4 重构)
│   │   ├── audio_analysis/             # 音频理解(嵌入音视频描述页)
│   │   ├── text_chat/                  # 文本对话 (swcap_text_chat)
│   │   ├── tts/                        # 语音生成 (swcap_audio_tts)
│   │   ├── video_analysis_search/      # 监控分析检索 (swcap_video_describe + swcap_video_retrieval)
│   │   ├── video_detection/            # 音视频描述 (+ tts_alert 工具)
│   │   ├── video_target_detection/     # 视频目标检测 (TargetDoc)
│   │   └── video_target_localization/  # 视频目标定位 (LocatorDoc)
│   │   # 每个 app 下固定结构:
│   │   #   ├── manifest.json           # 插件声明
│   │   #   ├── api.py                  # FastAPI router
│   │   #   ├── service.py              # 单例状态机 + 业务逻辑
│   │   #   ├── __init__.py             # register_api / register_shutdown 入口
│   │   #   └── static/
│   │   #       ├── template.html       # <section id="page-<id>"> 片段
│   │   #       ├── app.js              # ImApp_<id> 模块(WS + DOM)
│   │   #       ├── app.css             # 该 app 私有样式
│   │   #       └── wizard.json         # (可选) 向导配置;支持则启用 wizard 模式
│   │
│   ├── system/                         # 系统级(非插件,平台 UI)
│   │   ├── __init__.py                 # register_all — 手动装载系统 router
│   │   ├── api/                        # 系统 API(挂 /api/* 前缀)
│   │   │   ├── compute.py              # 算力注册中心 REST: instances / config / capabilities
│   │   │   ├── config.py               # 配置读写 + app-prompts
│   │   │   ├── coprocessor.py          # 协处理器 / NPU 探测
│   │   │   ├── fs_browser.py           # 服务端文件浏览器(各 app 共用)
│   │   │   ├── im_bridge.py            # /api/wechat/* 应用对接
│   │   │   ├── power.py                # 重启 / 关机
│   │   │   ├── system_logs.py          # journalctl → WS
│   │   │   ├── system_monitor.py       # CPU / 内存 / NPU / thermal zones
│   │   │   └── versions.py             # 组件版本探测
│   │   ├── im_bridge/                  # 微信桥接实现
│   │   │   ├── bot.py                  # Wechat 类(同步门面 + daemon thread)
│   │   │   ├── llm.py                  # LLM bridge(微信 ↔ swcap_text_chat)
│   │   │   ├── video_manager.py        # 微信视频分流 → embedding 索引
│   │   │   ├── video_search.py         # VideoSearch @Callable 工具
│   │   │   ├── video_cache.py          # LanceDB 缓存 (md5 + 切片参数 key)
│   │   │   └── service_wechat.py       # 单例 + WS broadcaster
│   │   └── static/                     # 系统前端(非插件部分)
│   │       ├── im_bridge/              # 应用对接页面模块
│   │       └── system.js               # 系统页通用前端
│   │
│   └── static/
│       ├── index.html.template         # 构建模板 — 见 core/manifest.py:_assemble_index_html
│       ├── core/                       # 框架核心 JS / CSS
│       │   ├── framework.{js,css}      # 路由 + DemoGuard + 各 ImApp_<id> 装配
│       │   ├── fs-modal.js             # 服务端文件浏览器模态
│       │   ├── model-pick.js           # 算力配置页模型选择
│       │   ├── qrcode.js               # (vendored) 二维码渲染
│       │   ├── vtl-palette.js          # 视频目标定位调色板
│       │   ├── format-utils.js         # 时间/字节 格式化
│       │   └── wizard/                 # 向导框架
│       │       ├── state.js            # WizardState IIFE:mount/unmount/step/env_check/form/finish
│       │       ├── registry.js         # WizardRegistry.tryMount(manifest) / unmount()
│       │       └── wizard.css
│       └── apps/<id>/                  # 各 app 自带静态(从 manifest.json frontend 装载)
├── requirements.txt                    # web deps + opencv-python-headless + python-multipart + av + lancedb
├── config.jsonc                        # 设备级运行时配置(GITIGNORED — 不要 commit)
├── .gitignore                          # 排除 .venv/, __pycache__/, uploads/, lancedb/, *.jsonc
└── .venv/                              # editable installs of wdnav, wdnai, wdndds
```

**版本号唯一源**:`app/__init__.py` 的 `__version__`。改服务版本只改这一处。

**零耦合规则**(`app/apps/__init__.py` 强制):
- 卡片(`app/apps/<id>/`)只能 import `app.core.*`,禁止跨卡片 import,也禁止 import `app.system.*`。
- 系统(`app.system`)与 core 双向允许(compute / config / video cache / tool registry 都是 core),但不依赖任何 `app.apps.*`。
- 共享资源:视频缓存(`app/core/video/cache.py`)、LLM 工具注册(`app/core/tools/registry.py`)、算力注册中心(`app/core/compute/registry.py`)全部由 core 提供,各卡片走各自入口(例如 video_analysis_search + wechat_video_manager 都用 `get_video_cache()` 单例)。

## Video detection page

The `#page-video-detection` section is wired to:

- **Backend** (`app/apps/video_detection/api.py`):
  - `POST /api/video-detection/upload` — multipart file upload; saves to
    `<resource_root>/uploads` (`/userdata/assets/uploads` on device,
    `<HOME>/assets/uploads` off-device;三个视频 app 共用同一目录,由
    `app/apps/video_detection/api.py:UPLOAD_DIR` 控制)。同名文件直接
    覆盖(``open("wb")`` 截断写),`file.filename` 经 `Path(...).name`
    规整后再写,返回 `{path, name, size}`。
  - `POST /api/video-detection/open` `{source: str}` — opens via
    `wdnav.VideoCapture`, starts decode thread, returns snapshot.
  - `POST /api/video-detection/control` `{action: "play"|"pause"|"stop"}`.
  - `POST /api/video-detection/close`.
  - `GET  /api/video-detection` — current snapshot.
  - `GET  /api/video-detection/fs?path=…` — server-side directory
    listing (used by the in-page file picker modal). Constrained to
    `VD_FS_ROOT` (env var, default `/`) and skips `/proc`, `/sys`,
    `/dev`, `/run`, `/snap`, `/usr/share/{doc,locale,man}`, etc.
  - `POST /api/video-detection/prompt` `{prompt, every_n}` — start VLM
    detection with the user's prompt. `every_n` 这里恒为 1:节流已从
    detector 移到 `FrameJudge`（判据是四张卡共用的唯一旋钮，见「帧判据」
    段),detector 自己不再跳帧。实际送检频率由 `POST /api/video/judge`
    的模式 / 阈值 / 帧间隔决定。
  - `POST /api/video-detection/prompt/stop` — stop detection (worker
    thread exits, session stays loaded for next start).
  - `GET  /api/video-detection/events` — snapshot + history list + latest.
  - `POST /api/video-detection/events/clear` — clear history.
  - `WS   /api/video-detection/ws` — server-pushes binary JPEG frames;
    interleaves JSON `{"type":"status", detector:{…}}` frames at <=1 Hz;
    emits `{"type":"eof"}` once when a file source reaches end;
    emits `{"type":"event", id, frame_pos, ts, prompt, text}` for each
    VLM detection result.
- **Service** (`app/core/video/session.py`):
  - One `VideoSession` singleton, one `wdnav.VideoCapture`, one decode
    `Thread`. Latest-frame-only queue (`maxsize=1`) — drops oldest on
    overflow to keep latency low.
  - Frame sinks: registered callbacks are called on each decoded BGR
    frame (used by the detector). Sinks must not raise.
  - Lazy `from wdnav import VideoCapture` at module top (still behind the
    `try/except` from `wdnav/__init__.py`); never imports `wdnav.video`
    explicitly.
- **Service** (`app/core/llm/detector_base.py`):
  - `WdnaiDetector` singleton. Wraps `wdnai.LLMSession` with
    `SystemLanguageModel.qwen3_vl_4b()` (configurable via
    `WDNAI_MODELS_DIR`).
  - **wdnai is imported lazily** at `start()` time — not at module load.
    Loading the RKNN native libs eagerly at app startup caused a
    segfault on this dev box (RKNN3 init races with wdnav.s mpp
    bring-up). Don't move the import back to module top.
  - One daemon worker thread consumes a `queue.Queue(maxsize=1)`
    (latest-only); the decode-loop sink drops whatever's pending and
    replaces it with the freshest frame so the detector always sees
    the most recent state.
  - **单帧多轮工具循环（agent-loop）**：`_infer_sync` 现在对一帧跑最多
    `compute.tool_call_max_iters`（默认 5，`0` = 无限，受
    `WdnaiDetector._ABSOLUTE_MAX_ITERS = 200` 硬上限兜底）次
    `sess.respond`；每轮若模型返回 `@Callable` 实例就执行副作用
    并把"调用 + 返回"作为**文本 attachment** 喂回下一轮 prompt，
    直到模型返回 `VLMDoc` 或循环上限命中。`sess.reset()` 每帧开头
    调一次（保留帧间 KV 隔离）；循环内不 reset。
    * 工具抛异常 → 直接发 error 事件跳出循环（不再喂回 prompt）。
    * 工具成功 → 写 transcript 继续循环。
    * `tool_call_max_iters=0` → 视为无限；`min(cfg, _ABSOLUTE_MAX_ITERS)`
      兜底，永远不会让 worker 单帧跑超过 200 次（防失控模型锁死）。
    * `_infer_sync` 返回 `List[DetectorEvent]`（一帧可多个事件），
      `worker_loop` 依次分配连续 id + 广播；前端 WS 协议不变。
    * transcript 格式刻意用 prose（不是 JSON），避免模型把 transcript
      里的旧 JSON 抄回去当成自己的工具调用重发。
  - History is an in-memory deque (max 200). Events carry `id`, `frame_pos`,
    `ts`, `prompt`, `text`.
  - On app shutdown, `close()` releases the RKNN session handle.
- **Frontend** (`app/static/core/framework.js (前端核心) + app/static/apps/<id>/app.js (各 app)` + `#page-video-detection` markup):
  - Address input + "浏览设备…" button (opens server-side file picker
    modal) + "打开" / "关闭" buttons. The picker modal drives
    `/api/video-detection/fs` and writes the **device's** absolute path
    back into the address input — paths come from the server's
    filesystem, not the user's laptop.
  - `<canvas>` rendered from binary WS frames; auto-reconnecting WS with
    exponential backoff.
  - Play / Pause / Stop / Replay, status line, animated progress bar for
    live streams.
  - Below the video: prompt textarea + "每隔 N 帧检测一次" input + 启动/停止
    buttons + status text + 历史事件列表. Each new event also pops
    a `vd-event-badge` overlay on top of the canvas (auto-dismiss
    after 6s).

`app/api/__init__.py` must import each submodule (`from . import power, …`)
so `app.include_router(...)` in `main.py` actually sees them — without it
the package object is empty and the routes are silently missing.

## Audio understanding (音频理解) — 嵌入音视频描述页

`#page-video-detection` 在视觉检测区下方新加 `#vd-audio-block` 子区块：
独立的 `audio prompt` textarea + "分析音频" / "停止" 按钮 + 段落事件列表。
复用上方视频源（`#vd-source` 的 value），不另起 file picker；与
`WdnaiDetector` 完全解耦，可与视觉检测同时运行。

### Omni-3B 单次推理上限（必须切片）

`wdnai/backends/omni.py:58-70` 把 audio encoder 的动态 shape 写死两档：

```python
AUDIO_SHAPES = {
    0: {"n_frame": 600, "embeds_dim0": 150},   # ≤ 6.0 s
    1: {"n_frame": 300, "embeds_dim0":  75},   # ≤ 3.0 s
}
```

`omni.py:287-291` 的截断逻辑：

```python
if mel.shape[1] >= n_frame:
    mel_padded = mel[:, :n_frame].astype(np.float16)   # 直接切到 600 帧
```

**结论**：超过 6.0 秒的音频被**静默截断**到前 6 秒；`Attachment` 串联
(`session.py:247` 的 `b"".join(...)`) 也救不了 —— 后端仍按 600 帧切。
所以 60 秒音频必须先切片。`audio_analysis.CHUNK_SECONDS = 6.0` 是硬上限。

### 切片策略（硬切片 + 时间戳列表）

`app/apps/audio_analysis/service.py`：

* **PyAV 抽音**：从当前视频源 `av.open` → `container.streams.audio[0]` →
  `av.AudioResampler(format="s16", layout="mono", rate=16000)` →
  `b"".join(pcm_chunks)`（16 kHz mono s16le bytes）。
* **硬切片**：每 6 秒一段（`samples_per_chunk = 6.0 * 16000 = 96000`），
  无重叠；总段数 `n_chunks = ceil(total_samples / samples_per_chunk)`。
* **逐段推理**：`with get_session_call_lock(): sess.reset() + await sess.respond(prompt, attachment=[Attachment(pcm, kind="audio")], sample_rate=16000, n_channels=1, sample_width=2)`。
* **失败隔离**：单段 SDK / NPU 异常写 `AudioEvent(level="error")` 继续下一段。
* **状态机**：`idle → extracting → done / stopped / error`。

### AudioEvent 形状

```python
@dataclass
class AudioEvent:
    id:        int
    chunk_idx: int       # 0-based 段序号
    start_s:   float     # 段起点秒
    end_s:     float     # 段终点秒
    ts:        float     # 推理完成 wall-clock
    prompt:    str
    text:      str       # omni 输出（已经过 sanitize）
    level:     Optional[str] = None   # "error" 时表示该段失败
```

前端渲染为 `[00:00–00:06] 描述...` 段落时间线（`fmtTime` 把秒数格式化为
`MM:SS`）。

### Capability 与 ComputeRegistry 集成

* `MODEL_PRESETS["qwen2.5-omni-3b"]`：memory_estimate_mb=2500 /
  estimated_init_seconds=50 / capability_types=["swcap_audio_understand"] /
  worker_name="audio-3b"（comm="worker:audio-3b", 13 字符）。
* 该 manifest 的 `spec_factory="qwen_2_5_omni_3b"`，worker 侧据此调
  `SLM.qwen_2_5_omni_3b()`；`comm_name="audio-3b"` 来自同文件
  `workers/qwen2.5-omni-3b.json` 的 `worker_name`（`compute_worker_lazy.py`
  已删除）。
* `_LEGACY_CAPABILITY_TYPE_RENAMES` 加 `"audio_understand" → "swcap_audio_understand"`，
  兼容老 config.jsonc。

### API 端点（`app/apps/audio_analysis/api.py`）

| Method | Path | 用途 |
|---|---|---|
| `GET`  | `/api/audio-analysis`               | 快照 `{state, error, source, prompt, progress, events_count, available}` |
| `GET`  | `/api/audio-analysis/events`        | `{snapshot, events: [AudioEvent...]}` |
| `POST` | `/api/audio-analysis/start` `{source, prompt}` | 启动一次；404 source 不存在 / 400 prompt 空 / 502 capability 未启用 |
| `POST` | `/api/audio-analysis/stop`          | 中途停止（不动 session） |
| `POST` | `/api/audio-analysis/events/clear`  | 清空内存事件 |
| `WS`   | `/api/audio-analysis/ws`            | server-push `{type:"hello"|"event"|"progress"|"state"}` |

错误码与 detector / VAS 对齐：502 + `RuntimeError("功能未启用: swcap_audio_understand")`。

### 前端模块（`app/static/core/framework.js (前端核心) + app/static/apps/<id>/app.js (各 app)`）

* `AudioAnalysis` 模块（仿 `VDDetector` 写法）：
  * `init()` 绑定 start / stop / clear；`ensureWs()` 指数退避重连（上限 30s）。
  * `onStart()` 校验 source ≠ 空（alert）+ prompt ≠ 空。
  * `onWsMessage()` 分发 hello / state / progress / event 四种帧；
    state 终态（done/error/stopped）时主动 `hydrateEvents()` 拉全量列表。
* `setVdAudioMeta(snap)`：把快照写进 `VD.audioSnap`，同时刷新
  `#vd-audio-status` / `#vd-audio-source` / 按钮 enable 状态 / 进度条。
* `setVdMeta` / `updateVdDetectorStatus` 同时把 `audio: VD.audioSnap` 推
  进 `DemoGuard.report("video-detection", ...)`。

### DemoGuard 集成

`video-detection` 已有 DemoGuard。audio 加进去需要：

* `_ACTIVE_STATES` 判定条件追加 `audio.state === "extracting"`。
* `stop()` + `stopSync()` 追加 `POST /audio-analysis/stop`（sendBeacon
  兜底）。
* `stopAndClear` 把 cache 清成 `{session: null, detector: null, audio: null}`。

`beforeunload` / `hashchange` 走已有兜底链路，自动覆盖 audio（sendBeacon
已发 stop）。

### 资源预算

| 项 | 值 | 备注 |
|---|---|---|
| omni 内存 | ~ 2500 MB | audio encoder + llm + embed；按 VL-4B 同档估；真机测后回填 |
| omni init | ~ 50s | 两个 rknn + embed；倒计时同其它 preset |
| 60s 音频切片 | 10 段 × ~ 1s | 单线程串行 ≈ 10s 总耗时 |
| 与 qwen3-vl-4b 共存 | ~ 6 GB | OK |
| NPU 互斥 | 进程级 `_llm_session_call_lock` 串行 | 与 detector / VAS / text_chat 共享同一把锁 |

`_check_npu_memory_budget()`（`compute_registry.py`）自动纳入 omni preset
校验；同卡内存超限在 `boot_compute_services` 时被拒绝。

### 已知限制

* **音频源必须含音轨**：PyAV `streams.audio` 为空 → worker 进入 `_STATE_ERROR`，
  前端 `alert("视频无音轨")`。
* **极短音频**（< 6s）：整段算一段，shape 1 (n=300) 或 shape 0 (n=600)
  补零处理（omni 内部行为）。
* **超时熔断**：单段 SDK 卡死没有 timeout；上层若需要可包一层
  `asyncio.wait_for(...)`，但当前按 detector / VAS 现有风格不做。
* **不做二次聚合**：60 秒以上音频不会自动生成"综述"；如未来需要，
  加一段独立的 LLM 调用（建议复用 `swcap_text_chat`，即 qwen3.5-vl-4b）。

## Surveillance page (视频监控) — **已下线（v0.4 重构）**

> **本节描述的是 v0.4 之前的 16 路 4×4 网格监控页面;v0.4 起已下线。**
> 视频能力整合进微信视频检索管线 (`app/system/im_bridge/video_manager.py`)
> 与 监控分析检索页 (`app/apps/video_analysis_search/`)。
>
> 以下内容作为**历史决策记录**保留 —— 16 路共享一个 LLMSession(避免 16×3.5 GB)
> 的设计仍有参考价值。当前不需要在 v0.4 复现该页面;若未来要重建,
> 本节可作为设计起点。

The `#page-surveillance` section (sits **above** 视频检测 in the sidebar)
shows 16 monitoring cells in a 4x4 grid. Each cell runs its own
decoder / encoder pipeline — one daemon thread per channel, one
`wdnav.VideoCapture` per thread, one `wdnav.VideoWriter` per thread
(zero-copy — the encoder consumes the decoder's DMA-BUF via
`wdnav+ws://`). The VLM interpreter is **shared across all 16 channels**:
the ``SurveillanceManager`` owns ONE ``wdnai.LLMSession`` (the qwen3-vl-4b
RKNN contexts total ~3.5 GB resident; spinning up 16 would need
~56 GB of unified memory, which a single RK3588 board can't supply).
A single ``_InferenceWorker`` thread serialises all VLM calls into the
shared session — see "Resource budget" below.

* **Service** (`(surveillance 已下线; 详见 im-bridge 的 video_manager)`):
  - ``SurveillanceManager`` singleton (lazy), owns 16 ``Channel``
    instances plus the shared ``_InferenceWorker``.
  - Each ``Channel`` carries its own config (name / source / prompt /
    detect_w / detect_h / playback_enabled / detection_enabled), runtime
    state, decode thread, H.264 packet ring buffer, and JPEG
    thumbnail ring buffer. The detection size is fixed to one of
    two presets — ``DETECT_SIZE_CHOICES = (384, 768)`` — chosen to
    match the qwen3-vl-4b model's native input (384) and a
    higher-fidelity option (768). Anything else in stored configs
    snaps back to 384 during ``_merge_default``. The
    ``ChannelConfigModel`` Pydantic schema rejects out-of-set
    values with HTTP 422 and ``detect_w != detect_h`` with HTTP
    422, so the API won't accept non-square or out-of-set sizes.
  - Decode loop (per-channel thread):
    ```text
    wdnav.VideoCapture.read0(detect_w, detect_h, fmt="RGB")
        |
        v
    _submit_vlm(...)                  # non-blocking: enqueue on the
        |                              # shared ``LLMSession`` queue
        v
    SurveillanceManager._InferenceWorker (single daemon thread, persistent asyncio loop)
        |   session.reset()  +  await session.respond(prompt, attachment=[Attachment(frame)])
        |   (always goes through wdnai — never direct rknn3lite / rknntoolkit)
        v
    Future resolves; channel._on_inference_done records the event async
        |
        v
    wdnav.VideoWriter("wdnav+ws://127.0.0.1:8000/api/surveillance/<id>/ingest", codec="h264")
        |   (zero-copy: writer.write(frame0) hands the decoder's
        |    DMA-BUF to MPP's encoder)
        v
    /api/surveillance/<id>/ingest  (server-side WS, wdnav encoder connects as client)
        |   fan-out to per-subscriber queue
        v
    /api/surveillance/<id}/stream  (browser-facing WS, raw H.264 wire format)
        |   WebCodecs (Chrome 94+ / Edge 94+ / Safari 17+ / Firefox 130+)
        v
    <canvas> in the enlarge modal
    ```
  - Thumbnail path runs in parallel: the decode thread downscales the
    decoded BGR frame to 240x135 via OpenCV, JPEG-encodes at quality
    70, and pushes the bytes to all registered
    ``/api/surveillance/<id>/thumb`` WS subscribers at ~5 fps.
  - wdnai is imported lazily (``_load_wdnai()``) inside the inference
    worker — the same module-level cache as
    :mod:`app.services.detector` so the wdnai import only happens once
    across the whole process. wdnav is imported lazily for the same
    reason (non-Rockchip dev boxes still boot).
  - Packets use the wdnav wire format
    ``<type_byte:u8><pts_us:u64 LE><payload>`` with
    ``0x00 DELTA | 0x01 KEY | 0x02 HEADER (avcC) | 0xFF EOS`` (matches
    ``wdnav.encoder._WebSocketSink`` and ``app/static/core/framework.js (前端核心) + app/static/apps/<id>/app.js (各 app)``'s
    ``avccCodecString()``).
  - Each ``Channel`` exposes a thread-safe subscriber queue +
    ``threading.Event`` pair for the H.264 stream and another for the
    thumbnail. Subscribers are added/dropped from the asyncio side via
    ``register_stream_subscriber()`` / ``unregister_stream_subscriber()``;
    the asyncio handler drains via ``loop.run_in_executor``.
* **Config persistence** — every per-channel edit (PATCH
  ``/api/surveillance/config/{id}``) calls
  ``SurveillanceManager.update_config()`` which writes the full
  16-channel array back to ``config.jsonc`` under
  ``surveillance.channels[]``. Reset (POST
  ``/api/surveillance/reset``) restores the 16 default entries.
  Missing fields are back-filled with defaults so older configs keep
  working.
* **API** (`app/api/surveillance.py`):
  - ``GET    /api/surveillance``              — per-channel runtime snapshot + config.
  - ``GET    /api/surveillance/config``       — full 16-channel config.
  - ``PUT    /api/surveillance/config``       — replace the whole list (validated by Pydantic).
  - ``PATCH  /api/surveillance/config/{id}``  — partial update of one channel.
  - ``POST   /api/surveillance/reset``        — restore defaults.
  - ``GET    /api/surveillance/{id}/events``  — VLM event history + snapshot.
  - ``POST   /api/surveillance/{id}/events/clear`` — clear history.
  - ``WS     /api/surveillance/{id}/ingest``  — server-side ingest; the wdnav
    encoder connects here as a client (hand-rolled RFC 6455 handshake,
    uvicorn accepts it as a vanilla client WS upgrade).
  - ``WS     /api/surveillance/{id}/stream``  — server pushes the H.264 wire
    format to the browser; the WebCodecs ``VideoDecoder`` (in
    ``app/static/core/framework.js (前端核心) + app/static/apps/<id>/app.js (各 app)``) consumes the same payload directly.
  - ``WS     /api/surveillance/{id}/thumb``   — server pushes JPEG thumbnails
    at ~5 fps.
* **Frontend** (`app/static/core/framework.js (前端核心) + app/static/apps/<id>/app.js (各 app)` ``Surveillance`` module +
  ``#page-surveillance`` markup):
  - 4x4 grid of ``<canvas>`` cells; each subscribes to
    ``/api/surveillance/<id>/thumb`` while the channel is running.
  - Click any cell → opens the enlarge modal
    (``#surv-modal``) showing:
    * a ``<canvas>`` fed by WebCodecs (``VideoDecoder``) using the H.264
      wire format from ``/api/surveillance/<id>/stream``;
    * a per-channel config form (name / source / prompt / detect size
      / enable toggle) with dirty-state tracking and an
      "放弃修改" button that re-fetches the saved config;
    * a live VLM event history list (incremental updates on every
      ``/events`` poll).
  - The "浏览…" button reuses the video-detection file picker modal
    (``VdFsModal``) — it writes the picked path into the surveillance
    source field when the surveillance modal is open, otherwise back
    into the video-detection source field as before.
  - The "恢复默认" toolbar button POSTs
    ``/api/surveillance/reset`` after a ``confirm()``.
  - WebCodecs is the **preferred** path for the enlarged view, but if
    the browser lacks ``window.VideoDecoder`` (older Chromium, or
    builds with WebCodecs disabled by flag), the modal falls back
    to the per-channel thumbnail WS (``/api/surveillance/{id}/thumb``)
    — JPEG @ ~5 fps painted to ``<canvas>``. Smoother motion needs a
    browser with WebCodecs; the grid cells continue to work either
    way.
  - Browser detection checks ``VideoDecoder`` + ``EncodedVideoChunk``
    + ``DecodedVideoChunk`` so we don't accept a build that exposes
    one without the others.

* **Two independent toggles per channel** — the per-channel config
  exposes ``playback_enabled`` (decodes + encodes the stream, opens
  ``VideoCapture`` and the ``VideoWriter`` to ``wdnav+ws://...``)
  and ``detection_enabled`` (gates the per-frame submission to the
  shared VLM worker). Playback can run without detection, so you
  can monitor 16 streams without ever touching the NPU. Toggling
  detection alone doesn't restart the decode thread — it's only
  consulted inside the loop.
* **Resource budget** — the manager owns ONE shared ``wdnai.LLMSession``
  (~3.5 GB resident: 2.32 GB LLM weights + 0.74 GB embed table +
  0.23 GB vision weights + a few hundred MB KV cache, all on RK3588's
  8 GB unified LPDDR5 — no dedicated VRAM, the NPU DMA reads from the
  same system RAM). All 16 channels share that one interpreter via
  the serialised ``_InferenceWorker`` queue, so the total resident
  footprint stays at ~3.5 GB regardless of how many channels are
  enabled. The trade-off is throughput: with N channels each wanting
  one inference every ``every_n`` frames at 25 fps and ~1 s per
  inference (typical qwen3-vl-4b warm call), the queue depth grows
  as ``N * 1 s * fps / every_n`` — at ``every_n=30`` that's
  ``N * 0.83``, so 16 channels queue ~13 frames deep on average. The
  events timestamp reflects when the inference actually completed,
  not when the frame was captured. If you need faster end-to-end
  latency, sharding channels across multiple ``wdnwebui`` instances
  (each with its own LLMSession) is the answer; the shared-session
  design here makes that a horizontal scaling decision rather than a
  per-channel one.

## 开机自启 `compute.autostart`（v0.18 起取代 `compute.services[]`）

**一句话**：`compute.autostart` 只声明"这台板子开机要跑哪几个 worker、
各绑哪张卡"。不在列表里的模型开机**完全不参与**（不 spawn / 不 adopt /
不占 NPU 显存），算力页仍显示 unavailable 供手动启用。

```jsonc
"compute": {
  "autostart": [
    {"model": "qwen3-vl-4b",     "npu_index": 0},
    {"model": "qwen2.5-omni-3b", "npu_index": 1}
  ]
}
```

字段只有两个，`extra="forbid"`：

| 字段 | 含义 |
|---|---|
| `model` | `workers/<model_id>.json` 的文件名主干（同时是 `MODEL_PRESETS` 的 key） |
| `npu_index` | 物理卡号，直接来自 `list_cards()`，0 起 |

**为什么删掉旧的 `compute.services[]`**：那张表必须把每个模型都列出来
才能表达"这个不要启动"，还得把 label 覆盖和设备号混在同一条记录里。
现在 label 归 manifest 管，卡号直接写真实 `npu_index`，旧字段（`name` /
`label` / `device` / `autostart=enabled`）会被明确拒绝而不是静默忽略。
系统配置页的「算力实例列表」编辑表格已随之删除，但
`GET`/`PUT /api/compute/config` 保留为只读 / 校验过的写入入口，方便脚本
和运维确认配置。

**两条启动路径是有意区分的**：

| 来源 | worker cmdline | 卡号怎么到 |
|---|---|---|
| autostart | `--model X --local-only --npu N` | 命令行参数，worker 自己 load |
| 算力页手动启用 | `--model X --local-only` | 主进程发 RPC `action=load npu_index=N` |

autostart 之所以恢复 `--npu`，是因为 `workers/_base.py:run_worker` 里
replier 注册**先于** eager-load 线程启动，worker 一起来 `info` 就有应答
（`not_loaded` → `loading` → `running`），不会 boot 超时；同时避免了
"spawn 时一个 load + 主进程再 bind 一次 load" 两条路径在同一个 session
slot 上打架。

**autostart adopt 到远端 peer 时不发 load** —— 那是对面板子的卡，发过去
等于抢本机不拥有的 NPU。只把 `npu_index` 记成算力页 checkbox 的默认值。

**每张卡一条 autostart**。同一个模型出现在两条里（不同 `npu_index`）是
合法的，会生成两个 worker 各占一卡；同一个 `npu_index` 出现两次则被
`compute.autostart 含重复 model` 校验拒掉（因为那才是真撞车）。

**踩过的坑（勿回退）**：

* `_cleanup_unregistered_workers()` 必须排在 spawn 循环**之前**。排在
  后面会和 fire-and-forget 的 spawn 线程竞争 `tracked_pids`，把刚
  `Popen` 但还没登记进 `_instances` 的 worker 判成孤儿杀掉 —— 实测首个
  autostart 模型在 spawn 同一秒以 `rc=-15` 挂掉。
* `_spawn_compute_worker` 里的 `/proc` 孤儿扫描必须先扣掉本 registry 已
  跟踪的 spawned pid。autostart 两条同模型不同卡时，第二条必然 `/proc`
  命中第一条刚起的 worker，不扣就会把它抢成 `adopted_local`。详见
  [同一个模型跑两张卡](#同一个模型跑两张卡v019算力页运行在多选) 的坑 1。
* `hidden` 语义已从 `compute.services[].autostart == "hidden"` 迁到
  manifest 的 `hidden` 字段，这样配置写坏了也不会误隐藏模型。

## `compute.active_app` —— 用户"所在卡片"自恢复（v0.4 起）

**一句话**：用户进入某张 demo 卡片、且该卡片所有 `software_capabilities`
都有 **local** running 实例承载时，前端框架把 `app_id` 写到
`config.jsonc["compute"]["active_app"]`，并把承载该 app 的
`(model, npu_index)` **增量写进 `compute.autostart`**。下次 reboot
之后：

1. `boot_compute_services()` 读 autostart，把这些 worker 拉起来
   （autostart 那条机制不变）。
2. 浏览器 boot，前端 `GET /api/compute/active-app` 拿到非空 → 直接
   跳到 `#<app_id>`，**不再让用户重新点卡片走一遍 wizard**。

```jsonc
"compute": {
  "autostart": [
    {"model": "qwen3-vl-4b",     "npu_index": 0},
    {"model": "qwen2.5-omni-3b", "npu_index": 1}
  ],
  "active_app": "video-detection"
}
```

**为什么需要这块机制**：

* 默认行为下 `compute.autostart` 是空的 / 用户没主动配 → 浏览器 boot
  之后用户看到的还是 `#apps` 首页，必须挨个点卡片、走 wizard、等
  30~60s 加载模型，才能"进入 app 模式"。
* 我们想要"开发板进入这个 app 的模式"——一旦用户上次停在某张卡片、
  卡片已经在用，重启后直接恢复到那个状态即可。

**触发与失效**（v0.23 起调整）：

| 时机 | 动作 |
|---|---|
| 框架 hashchange 落到某张 status=ok 的卡片 | in-memory `_current = app_id`(无论 caps running 与否) |
| 1Hz 轮询发现该卡 `software_capabilities` 全部有 local running | `PUT /api/compute/active-app {app_id}`(后端落盘 + 增量写 autostart;幂等) |
| 用户切到 home / 系统页 / 另一张卡 | **不再 DELETE** —— `_current` 与后端 `active_app` 都保留;sidebar 应用面板 / reboot 恢复都用它 |
| back-home 按钮("返回首页" / "退出应用") | 显式 `_exit()` —— `_current = null` + keepalive DELETE 后端 |
| `beforeunload`(关 tab / 刷页) | **不**DELETE —— 跨 tab 保持,直到显式 _exit |
| boot 拉取 | 读 `active_app` → 跳 #<id>;若 manifest 已下架 → 同时清掉,避免 stale 状态永远留着 |

**为什么不再"切走就清"**:之前的语义是"切到 home / 系统页 = 不在卡片 = 清 active_app",
但这与用户预期不符 —— 进了卡片后中途看一眼系统页 / home 不该把"我在这"
丢掉,siderbar 应用面板应还能跳回原卡,reboot 后 bootRestore 也应能恢复。
现在 active_app 是"用户最后所在的卡片",只在 back-home 显式退出时才清。

**关键设计点**:

1. **local only,不接受 adopted (LAN peer)**。peer 板子 reboot 之后
   autostart 等于没意义。前端轮询 `_capsAllLocalRunning` 与后端
   `set_active_app` 校验都排除 `source == "adopted"`。
2. **active_app 与 autostart 是耦合的**:`set_active_app` 会把承载该
   app 的 `(model, npu_index)` 增量写进 autostart;已存在的不重复。
   这意味着"进入视频检测 → 自动加 autostart",用户后续如果想关掉
   这个 worker,得去算力配置页(不会因为切走卡片就被自动关)。
3. **autostart 不被 DELETE 主动撤销** —— active_app 是用户态偏好,
   autostart 是"该启的算力"。用户切走卡片不等于模型不再启;只有当
   用户主动去算力配置页 disable 时 autostart 才会改。
4. **不存在"开了半透明但被人为篡改"**:autostart 解析器(v0.18
   schema,extra=forbid)依然有效;active_app 只是多了一个字段。
5. **manifest 校验**:`PUT` 时服务端用 `discover_cards()` 找对应
   manifest 并要求 `status==ok`、非 hidden;找不到 → 404。
6. **in-memory `_current` 与后端 `active_app` 解耦**:前者追踪
   "用户最后进入的卡片"(与模型 running 无关),后者是"模型就绪且
   autostart 已就位"的持久化标记。sidebar 应用面板、bootRestore 都
   读前者;reboot 时如后者也非空,后端已自动起好 worker,bootRestore
   跳转后立刻看到 running。

**已知边界**：

* 浏览器 `beforeunload` 期间 JS 不能保证请求送达；v0.23 起
  `beforeunload` **不再发** DELETE（之前 best-effort DELETE 反
  而容易把"我在这"擦掉），所以关 tab / 刷页后 `active_app` 总是
  保留。下次 boot 时若该 app 的 worker 已 autostart 起来，前端会
  照常跳过去——这是**预期**而不是 bug。如果用户想"重置"得显式
  点 back-home。
* 若用户改了 autostart（手动删了 active_app 用到的那条），但
  active_app 字段没清，bootRestore 跳到 #app 后框架会发现 cap 不全
  → 自动 DELETE 清掉；下一次 boot 不再跳。
* 卡片无 `software_capabilities`（纯静态 / 仅配置类）仍会进
  `_current`，但**不**触发后端 PUT（autostart 不适用）。这类卡片
  在 sidebar 应用面板记忆里"占位",reboot 后也按 `_current` 跳。
  显式 back-home 才清。

**debug**：

* `GET /api/compute/active-app` 直接读当前 active_app。
* `tail -f logs/app.log | grep active_app` 看服务端的写入 / 清空
  日志（`compute.active_app=%s：autostart 新增 %d 条`）。
* `cat config.jsonc | jq .compute.active_app` 看盘上文件。

## Configuration

> **不要 commit `config.jsonc`** —— 设备级运行时配置，由
> `ConfigStore._seed_defaults()` 自动生成，每台机器独立维护；详见
> [修改守则](#修改守则)。已 `.gitignore` 排除。

The service reads a single JSONC file `config.jsonc` from disk and
holds it in memory. The file is seeded with a `base` section at
`{}` + future sections stacked alongside it.

- **JSONC, not JSON** — the on-disk file is JSONC (JSON with `//`
  line comments and `/* ... */` block comments), parsed via the
  ``json5`` library through ``app/core/compute/jsonc.py``. **User-written
  comments are accepted on read but discarded on write**: any save
  through ``PUT /api/config/...`` or ``POST /api/config/import``
  emits a freshly formatted JSONC file with no comments. Hand-edited
  comments stay intact as long as nothing triggers a backend save;
  once any save fires, they're gone. Rationale: round-tripping
  comments faithfully would need a token-level rebuild that's brittle
  to key reordering / nested array comments / multi-line block comments;
  the simpler contract is "comments are for humans reading the file,
  the backend rewrites it cleanly on save".
- **Where it lives** — resolved from `WDNWEBUI_CONFIG` env var when
  `run.py` starts (set by `--config /path/to/config.jsonc`); if unset,
  defaults to `<repo>/config.jsonc`. Always absolute.
- **Read-write** — `app/core/config.py` `ConfigStore` is a lazy
  singleton (`get_config()`) with an in-memory dict guarded by an
  `RLock`. Missing files / non-object payloads all collapse to `{}`.
- **Atomic writes** — `replace()` / `set_section()` write a sibling
  temp file and `os.replace()`-s into place so a crash mid-write
  can't corrupt the file. Output goes through
  :func:`app.core.compute.jsonc.dump_jsonc` (quoted keys, trailing commas,
  UTF-8 native).
- **Whole-file endpoints** (`app/system/api/config.py`):
  - `GET  /api/config`        — `{path, data}` (current snapshot).
  - `GET  /api/config/export` — `FileResponse` of the on-disk file
    (downloads as `config.jsonc`). If the file doesn't exist yet,
    the endpoint writes `{}` first so the download always succeeds.
  - `POST /api/config/import` — `multipart/form-data` upload (field
    name `file`). Validates UTF-8, JSONC parse, top-level object —
    rejects empty bodies, lists, scalars with HTTP 400. Uploaded
    JSONC with comments is accepted; the persisted file drops the
    comments on write (see "JSONC, not JSON" above).
- **Section endpoints** — `GET /api/config/{section}` reads a
  top-level key (e.g. `base`) and fills in missing fields with
  Pydantic defaults; `PUT /api/config/{section}` replaces just that
  key without disturbing siblings. Other sections (e.g. a future
  `power`) stay intact across `base` edits.
- **`base` section** (Pydantic model `BaseConfig` in
  `app/system/api/config.py`) currently has two fields:
  - `device_name` — human-readable device identifier. The default is
    generated lazily on the first `GET /api/config/base` after a
    missing/empty value (random 8-char alphanumeric, mixed case + digits,
    via `secrets.choice`) and persisted to `config.jsonc` so the value
    stays stable across reads and reboots. Validated as non-empty
    (whitespace stripped).
  - `model_path` — absolute directory where RKNN model weights live
    (default `/userdata/models/`). Validated as non-empty +
    starts-with-`/`; the GET/PUT responses also include a
    `model_path_exists` boolean for the UI to surface a soft warning
    (the save doesn't create the directory).
- **UI** — gear button (`#config-btn`) sits next to the power button
  at the bottom-left of the sidebar. Opens a popover with 导出配置 /
  导入配置 (export / import) and the live config file path at the
  bottom. Import fires a hidden `<input type="file">`; export
  fetches the file as a blob and triggers a download. Popover and
  power popover are mutually exclusive (opening one closes the other
  — they share the same anchor and would otherwise stack).
- **Base config page** — second sidebar entry (`#base-config`),
  driven by the `Base` module in `app/static/core/framework.js (前端核心) + app/static/apps/<id>/app.js (各 app)`. GETs the
  `base` section on entry, PUTs on submit; "放弃修改" re-fetches.
  Services downstream (e.g. the detector's model loader) read
  `get_config().get_section("base")["model_path"]` lazily, not at
- **视频监控 section on the base-config page** — same page also
  carries a sticky-header table of all 16 channels under
  `bc-section-surveillance` (`#bc-surv-tbody`). Each row exposes
  名称 / 视频源 (with "浏览…" reusing the existing server-side
  picker) / 检测提示词 / 检测尺寸 (384 / 768 单选下拉) / 播放 /
  检测 toggles.
  Save button does `PUT /api/surveillance/config` with the full
  16-channel array; the same `config.jsonc` powers the dedicated
  视频监控 sidebar page, so edits round-trip through the shared
  store. The browse button sets `VdFsModal._sourceTarget` to the
  row's source input before opening the modal; the
  `VdFsModal.pickSelectedFile` / `pickCurrentDir` override at module
  load checks this target before falling back to the surveillance-
  modal / video-detection paths. The `Base.init()` call wires both
  the device-name form and the surveillance table; navigating to
  `基础配置` calls `Base.load()` (GET `/api/config/base`) **and**
  `Base.loadSurveillance()` (GET `/api/surveillance/config`); each
  section has its own save button that hits its own endpoint.
- **`WDNAI_MODELS_DIR` propagation** — `app/main.py` reads
  `base.model_path` at `create_app()` time and writes it into
  `os.environ["WDNAI_MODELS_DIR"]` before any `LLMSession` is built.
  wdnai's `_models_root()` reads that env var on first call, so the
  SDK picks up the configured root automatically. Changing the
  field via the UI after the app has booted only takes effect on
  the next uvicorn reload (i.e. after a restart in production) —
  an already-loaded `LLMSession` was bound to the old path.
- **Frontend modules** — `ConfigMenu` (popover), `Base` (form page);
  `PowerMenu` was untouched and is mutually exclusive with
  `ConfigMenu` via the `_wrapOpenToCloseOther` helper.
- **算力配置 (`ComputeConfigPage`) + 算力注册中心 (`ComputeRegistry`)**
  —— 用户在算力配置页启用/禁用/重启实例；每条实例绑定到一张 NPU 卡
  （由 `RKNN3Lite().get_devices_id()` 枚举）+ 一个 model preset。同一
  NPU 上多个不同 model 允许（按内存预算自我约束），但同 NPU 重复同
  model 拒绝（pydantic 校验 + 注册中心运行时校验双层防护）。内存估算
  取自各 worker 的 ``manifest.json``（Qwen3-VL-4B ≈ 3500 MB /
  Qwen3-TTS ≈ 1900 MB / …）。

  **开机自启配置 `config.jsonc["compute"]["autostart"]`**（v0.18 起取代
  旧的 `compute.services[]` 表格，见下方「开机自启 compute.autostart」）：
  ```jsonc
  "compute": {
    "autostart": [
      {"model": "qwen3-vl-4b",      "npu_index": 0},
      {"model": "qwen2.5-omni-3b",  "npu_index": 1}
    ]
  }
  ```
  `boot_compute_services()` 遍历这些条目逐条 adopt-or-spawn。**只有**列在
  这里面的模型参与开机启动：不在列表里的完全不 spawn / 不 adopt / 不占
  NPU 显存，算力页仍显示为 unavailable 可手动启用。`npu_index` 以
  `--npu` 传给 worker，worker 在注册完 replier 后用后台线程自己 load
  （见「开机自启 compute.autostart」）。用户在算力配置页手动启用的模型
  不带 `--npu`，卡号走 RPC `action=load npu_index=N`。

  UI 通过 `GET /api/compute` 每秒轮询状态，
  loading 实例显示预估倒计时（`ESTIMATED_INIT_SECONDS_PER_MODEL`）。
  Capability 概念作为内部 key 使用（`get_session("swcap_video_describe")` /
  `"swcap_audio_tts"`）；capability 路由仅走同进程 `ComputeRegistry`，
  跨机器发现走 FastDDS SIMPLE multicast（v0.6+），`ComputeRegistry`
  的 `Hub` 服务名 `wdndds/rpc/<model_id>` 自动暴露给 peer。

## App 提示词模板 (app.prompts)

`config.jsonc["app"]["prompts"]` 是一个 3 层 schema 驱动的提示词模板
配置 —— 给 wdnai 的 `@Generable` schema class (`VLMDoc` / `TargetDoc`)
的每个 `Field(description=...)` 提供用户可编辑的中文说明。模型根据
`description` 推断字段语义，描述越具体，返回字段越稳。

v0.11 起从 `compute.app_prompts` 挪到独立 `app` section（顶层
`base / app / compute` 三层结构更清晰 —— prompts 跟硬件能力 / 算力
实例是不同抽象层）。

* **路径** — `app.prompts.<app_key>.<schema_cls>.<field>`，
  三段都是字面 key。例如 `app.prompts.video_detection.VLMDoc.text`
  写到 `text: str` 这一个 field 的 description 上。
* **当前可配字段 (3 项)**：
  - `video_detection.VLMDoc.text` — 音视频描述页 `text` 字段描述。
  - `video_detection.VLMDoc.level` — 严重程度标签字段描述。
  - `video_target_detection.TargetDoc.desc` — 视频目标检测页
    `desc` 字段描述。
* **`TargetDoc.x1/y1/x2/y2` 刻意不做配置** —— 4 个 bbox 坐标字段的
  description 写死在 `_build_target_doc_class()` (`app/core/llm/detector_base.py`)
  函数体里，是协议层 0–1000 归一化坐标格式说明，与模型对「画哪里」
  的理解无关，开 UI 编辑只会让用户误改协议格式。`_SCHEMA_CACHE`
  的 cache key 也只覆盖可配字段 (`desc`) 的 description，硬编码坐标
  字面量不进 cache（也不在 `_load_app_prompts` 返回值里）。
* **默认值在 `app/core/llm/detector_base.py` 模块顶层常量**
  (`DEFAULT_VLM_DOC_TEXT_DESC` / `DEFAULT_VLM_DOC_LEVEL_DESC` /
  `DEFAULT_TARGET_DOC_DESC_DESC`)，简体中文一句话。
* **空值兜底** —— 配置里字段缺失或空字符串 fallback 到默认常量，
  不写盘空值；`PUT /api/config/app-prompts` 由
  `AppPromptsConfig.field_validator` 兜底拒绝空字段 (422)。
* **修改提示词后重启检测后生效** —— detector 实例的 schema class 在
  `start()` 时 build 一次，cached 在模块级 `_SCHEMA_CACHE`
  (`app/core/llm/detector_base.py`)；UI 上方有 hint `#bc-prompts-hint`
  提示用户保存后必须停止 + 重新启动检测循环。
* **API** (`app/system/api/config.py`)：
  - `GET  /api/config/app-prompts` — 返回当前生效值（缺字段用默认补）。
  - `PUT  /api/config/app-prompts` — 全量替换；`AppPromptsConfig`
    校验每个字段非空 + strip 后落盘。
* **UI 入口** —— 系统配置页 (`#base-config`) 下新增
  `#bc-section-app-prompts` 子区块（`app/static/index.html.template`）：
  音视频描述 (VLMDoc) 两条 textarea + 视频目标检测 (TargetDoc) 一条
  textarea + 保存/放弃修改按钮。`Base` 模块 (`app/static/core/framework.js (前端核心) + app/static/apps/<id>/app.js (各 app)`)
  的 `loadPrompts()` / `savePrompts()` / `resetPrompts()` 负责
  GET/PUT 三件套。
* **添加新 app 的提示词步骤**：
  1. `app/system/api/config.py` 里定义 `<Schema>Prompts(BaseModel)`，加 field
     description；`AppPromptsConfig` 加同名嵌套字段并配
     `field_validator`。
  2. `app/core/llm/detector_base.py` `_load_app_prompts()` 的 `defaults`
     映射表追加 `(<app_key>, <schema_cls>)` 条目 + 默认常量。
  3. `app/static/index.html.template` 的 `#bc-section-app-prompts` 内追加
     一组 `<h4>` + `<textarea>` 控件。
  4. `app/static/core/framework.js (前端核心) + app/static/apps/<id>/app.js (各 app)` `Base.loadPrompts()` /
     `savePrompts()` 里追加对应字段读写 + `addEventListener`
     绑定。
* **Schema cache 行为** — `_SCHEMA_CACHE: dict[tuple, type] = {}`
  以 `(schema_name, tuple(sorted(field_descs)))` 为 key：
  description 变了 → 新 key → rebuild；description 没变 → 命中旧
  key → 复用。一般用户每次点"保存"最多触发 1 次重建，不在每次
  inference 重建。

## Discovery page (注册发现)

`#page-discovery` 内部按从上到下两个 pane 排布：

1. **硬件能力 (HW)** —— 本机硬件能力开关，源自 `HARDWARE_CAPABILITIES` 静态表 +
   `config.jsonc["base"]["hardware_capabilities"]` 启用状态。checkbox 修改后
   点"保存修改"才落盘，避免误触。
2. **软件能力 (SW)** —— 当前 running 实例暴露的能力（每条来自一个 RKNN 模型实例）。

**v0.10 起下线**：原"在网注册 (DDS)"子面板（含开关 + FastDDS 能力广播 +
对端列表）整个删除 —— `DdsDiscoveryManager` 之前只起一个 placeholder
`RpcHub`，既不广播 capability、也不订阅对端列表（`_discovered` 永远为
0），toggle 是个空壳。跨机器发现改由 FastDDS SIMPLE multicast（v0.6+）
承担，「系统信息 → 在网设备」section 展示主进程 Hub 的真实 peer 列表。

## System logs page

The `#page-system-logs` section is wired to `app/system/api/system_logs.py`,
which shells out to `journalctl` and pipes JSON-lines back:

- `GET /api/system-logs/units` — service list (running / failed /
  exited / activating) + priority whitelist. Populates the unit
  dropdown so the UI never hardcodes a name.
- `GET /api/system-logs?unit=&priority=&lines=` — last N entries.
  `unit` is matched against a strict regex (`[A-Za-z0-9_.@:-]{1,128}`),
  `priority` is whitelisted, `lines` is clamped to 2000 — none of these
  reach a shell.
- `WS /api/system-logs/ws?unit=&priority=` — `journalctl -n 100 -f`
  driven by `asyncio.create_subprocess_exec`. Lines stream as
  `{"type":"line","entry":{...}}`; auto-terminates after 30 minutes
  per connection; pings every 15 s so idle proxies don't reap.

Frontend (`app/static/core/framework.js (前端核心) + app/static/apps/<id>/app.js (各 app)` `Logs` module): filter toolbar (unit +
  priority + lines + follow-tail checkbox + clear), `<ol>` list of color-coded
  log rows on a dark stage, auto-scroll only when the user is parked at
  the bottom (so scrolling up to inspect an older line doesn't get yanked).
  Journalctl outputs `MESSAGE` as a list-of-bytes for some services
  (rknn3, etc.); `_normalize()` decodes with `errors="replace"` so the
  page never 500s on a binary blob.

## Gotchas

- **CI lives at `.github/workflows/regression.yml`** (3 jobs, see file
  for current config). Don't invent a different CI — extend or run
  these instead:
  - `lint` (always, ~30s): py_compile the files touched by the broadcast
    fix + the regression scripts in `scripts/`. No service / SDKs needed.
  - `unit` (always, ~10s): `scripts/verify_video_session_broadcast.py`
    exercises `VideoSession.subscribe() / _broadcast_frame() / _teardown()`
    in pure Python. No service / SDKs / hardware needed.
  - `integration` (self-hosted rk3588 runner only, gated by
    `RUN_INTEGRATION=true` env on the runner + `WDNAV_DEPLOY_KEY`
    secret for the private SDK mirror): runs
    `scripts/verify_video_ws_broadcast.py` and (when `RUN_ALL=true`)
    `scripts/verify_ws_after_page_switch.py`. These hit a live
    `wdnwebui` on `127.0.0.1:8000` and need `/userdata/video_test/rk-0.mp4`.
  - To add a new check: pick the right job (lint / unit / integration),
    add the step, update this gotcha if the job scope changes.
- **No lint / typecheck / test config exists yet** beyond the above CI.
  Do not invent `ruff`/`mypy`/`pytest` invocations — there is nothing
  else to run, and adding config requires a decision the user hasn't
  made.
- **Reload mode picks up `.venv/src/` edits** because the editable installs
  live there. Editing wdnav/wdnai source via Uvicorn reload is fine; the
  installed metadata is in `.venv/lib/python3.11/site-packages/`.
- **`run.py` defaults to `reload=True` and port 8000** — fine for dev, but
  disable reload (`uvicorn.run(..., reload=False)`) before any production
  command, or use `uvicorn app.main:app` directly.
- **wdnav hardware path requires a Rockchip target.** If `import wdnav.video`
  raises on this machine, it's expected — the SDK falls back gracefully for
  bare `import wdnav`. Don't suppress the fallback.
- **Imports should stay lazy** for `wdnav.video` / `wdnai.*` so a non-target
  dev box can still boot the web shell.
- **`WdnaiDetector._load_wdnai()` must stay lazy**. The module-top import of
  `wdnai.Attachment / LLMSession / SystemLanguageModel` will pull in
  RKNN3 native libs at app startup, racing with wdnav's mpp init and
  segfaulting on this dev box. Keep the import behind `_load_wdnai()`,
  which is called from `start()` / `_ensure_session()`.
- **`VideoSession.open()` event ordering matters.** Always `_teardown()`
  before clearing `_stop_event` / setting `_play_event` — teardown flips
  both into the "stopped" state, so doing it the other way leaves the new
  decode thread seeing `_stop_event.is_set() == True` and exiting on
  iteration 1 (silent: state stays "playing", `frame_pos` stays 0, no
  frames ever appear on the WS).
- **RTSP/RTMP/HLS work via the same `/open` endpoint** — `VideoCapture`
  routes anything containing `://` through PyAV. The frontend just shows
  the URL in the address bar; no client-side protocol branching needed.
- **Surveillance sources loop automatically.** When a channel's source
  is a regular file the decode loop wraps around to the start when
  EOF is hit; when it's a directory the loop enumerates video files
  (``.mp4 .mkv .avi .mov .webm .flv .ts .m4v .3gp .wmv .mpg .mpeg
  .rmvb .h264 .h265 .hevc .m2ts .mts``, dotfiles skipped) sorted
  alphabetically and plays them in sequence, looping back to the
  first file when the last one ends. The folder is re-scanned on
  every wrap so newly-dropped files join the loop without a restart.
  Live streams (``rtsp://``, ``rtmp://``, ``/dev/video*``) are
  detected as ``"live"`` and never looped — they shouldn't hit EOF
  anyway. The currently-playing file is exposed in the snapshot as
  ``current_file`` and surfaces in the cell status + modal meta
  strip so users can see which clip is on during a folder loop.
- **Seek/scrub is intentionally not implemented yet.** `VideoCapture`
  doesn't expose a `set(1, frame_pos)` API; plumbing one in means
  touching `self._container` internals. The progress bar is currently
  read-only and animates only for live sources.
- **`ComputeRegistry` 启动顺序 (v0.7→v0.10)**：`app.main.create_app()` 必须先
  `include_router(compute.router)`，**再**fire ``compute-boot`` daemon
  thread 调 `get_registry().boot_compute_services()`，**再**注册 detector 的
  frame sink。daemon-thread 化（v0.10）是为了让 ``create_app()`` 同步返回 →
  uvicorn 立即 bind 8000；probe / adopt / spawn 的耗时挪到后台。

  **`boot_compute_services()` (v0.10 /proc 先行)**：遍历
  ``MODEL_PRESETS`` 全集，逐条先扫 ``/proc/*/comm``：
   - **`_find_workers_by_name(model_id)` 命中**（本机有 worker comm）
     → 走 RPC ``info`` probe（``_probe_service`` 3 × 5s race-recovery）+ 
     ``_adopt_existing_worker``。probe 命中 → ``_source`` 按 info
     ``hostname`` 决定：同 hostname → ``"adopted_local"``（本机孤儿 /
     reload 残留），不同 hostname → ``"adopted"``（真 LAN peer）；
     probe 失败 + config 里有 → fall through ``_spawn_compute_worker``
     让它的 dedup 拒 spawn sibling 登 errored inst；probe 失败 + config
     里没 → log warning + skip。
   - **`_find_workers_by_name(model_id)` 未命中**（冷启动 / 全新板）
     → **跳过 probe**（避免冷启动 5 × 17s ≈ 85s 纯 timeout 阻塞），
     config 里有 → ``_spawn_compute_worker`` 拉新子进程（``python
     compute_worker.py --model <id>``，**不传 --npu**），config 里没
     → skip（UI ``unavailable``）。

  **为什么 `/proc` 先行**：v0.10 之前的 boot 对每个 preset 都跑一遍
  probe；冷启动无 worker 时 5 × 17s timeout 浪费且阻塞 create_app。
  ``/proc`` 先行后冷启动 probe 数 = 0，后台总耗时 < 100ms。

  **v0.8 dedup**：spawn 之前再扫一次 `/proc/*/cmdline` 找同 model 的现存
   worker 进程（`_find_orphan_worker_pid`），命中就走 adopt 接管 —— 防
   reload 后 Popen(start_new_session=True) 的子进程脱离父进程组、reload
   SIGTERM 杀不掉导致的 NPU 内存累积泄漏。详情见 "Orphan worker 处理"
   段落。

  worker 启动后**只**起 Hub + Service + Replier，不 build LLMSession。
  状态停在 ``not_loaded``；用户在算力配置页选 NPU 后触发
  ``POST /api/compute/instances {model, npu_index}`` →
  ``bind_instance`` 走 RPC ``load{npu_index}`` 才 instantiate
  ``LLMSession(model=spec, npu=N)``。这条 boot 流程**不**自动 bind
  任何 NPU —— 完全解耦 web 与 worker：web shutdown 不 kill worker，
  worker 独立进程生命周期由 systemd / 手动 / 跨主机发现共同管理。

- **单帧工具循环硬上限**：`WdnaiDetector._ABSOLUTE_MAX_ITERS = 200`
  是兜底，不论 `config.jsonc["compute"]["tool_call_max_iters"]` 是 0
  （无限）还是用户显式填了超大值，单帧推理都不会超过 200 次
  `sess.respond`。200 次 × 1s/inference ≈ 3 分钟单帧最坏延迟 -
  — 失控模型可以跑很久但不会锁死整个 worker 线程 / 队列。
  配置热生效：每次推理前 `_get_tool_call_max_iters()` 重读 config.jsonc。
- **probe 线程（per-instance daemon）**：每个 spawned / adopted
  ModelInstance 在创建时起一个 daemon ``_probe_instance`` 线程，1Hz
  轮询 ``info`` RPC，把 worker 的 ``not_loaded / loading / running / error``
  同步到 registry。worker 进程意外退出时 probe 把 inst 翻 ``error``
  并捕获 stderr。Reload 模式下 uvicorn 重启父进程会杀掉 daemon thread；
  production 下靠 `close_all`（但 v0.7 起 `close_all` 不杀 worker）。
- **`ModelInstance._source` 4 个取值**（v0.8 起）：
  - ``"spawned"``        —— 本次 boot 由本机 Popen 起的 worker
                            （`proc` 句柄在 registry 里，PID 来自 `proc.pid`）。
  - ``"adopted_local"``  —— uvicorn reload 留下的孤儿 worker：probe 命中
                            的 responder 跟主进程同 hostname。proc 句柄
                            **不在** registry 里（被 SIGTERM 干掉的是父
                            进程而不是 worker），但 worker 进程还在跑、
                            能响应 RPC。`_peer_pid` 字段填 worker 的真实
                            PID，让用户能 `kill <pid>` 手动清理。
  - ``"adopted"``        —— LAN 上另一台机子的 worker 提供的 service。
                            只有本机 _setup_rpc_client 路径，没有 proc；
                            bind / 启停 / 重启全部 403。
  - ``"unavailable"``    —— `MODEL_PRESETS` 里有这个 model，但 daemon
                            全 LAN 都没回应（既没本地孤儿也没 peer）。
  UI 渲染：local 列容纳 spawned + adopted_local（按 PID 来源不同在
  `_makeLocalCard` 区分"自管 PID" 与 "孤儿 PID"，后者 重启/禁用 按钮
  disable 并提示 `kill <pid>`）；peer 列容纳 adopted + unavailable
  （`adopted` 卡片标题为 `<model> @ <device_name>`，device_name 来自
  worker `info_handler` 的 device_name 字段）。
- **Local-only operations**：算力配置页的 "启用 / 禁用 / 重启" 按钮
  可见性 / 行为：
  - **spawed**：启用 / 重启 / 禁用 三按钮全开；禁用 = RPC `unload`
    + `shutdown` + SIGTERM/SIGKILL，**worker 进程真的关掉**（实例
    从 registry 移除，状态变 `unavailable`）。重启用走冷启
    spawn + RPC load（约 40s，**不再有"秒级"这回事**）。
  - **adopted_local**：启用 / 禁用 两按钮可点（重启 disable 提示
    `kill <pid>` 手动清理）；禁用同 spawned，但该实例 `proc is None`
    → SIGTERM 那步跳过，**只能靠 shutdown RPC 让 worker 自退**
    （这正是 `unload_instance` 必须传 `teardown=False` 的原因，见下）。
  - **adopted**：只有 重启 disable；禁用按钮 **403**（关的是别人板子
    的进程，见 Layer 2 纵深防御）。
  - **unavailable**：只有 启用 可点（spawn 新进程）。
  - **孤儿 worker** 仍由下次 boot 的 `_find_workers_by_name` 兜底 ——
    主进程退出（`close_all`）**故意不杀** worker，热重载后靠 adopt
    接管，不要把这个当成泄漏。
  - 后端 `_require_owned` 仅对 **重启** 仍拦 adopted → 403；禁用的
    403 护栏在 `stop_instance` 端点里单独写了（同样的理由）。
  - **与 `POST /compute/stop-all` 有意不同**：那个端点（只有 TTS
    卡片在用）仍只 `unload_instance`，进程留着。别统一"顺手"改掉。
- **禁用必须先 unload 再杀进程（v0.17）**：`stop_instance_local` 的
  顺序是 load-bearing，任何两步反过来都会坏：
  1. `unload_instance(teardown=False)` **先跑**，且**同步阻塞**等
     worker reply —— worker 在 reply 前已执行 `unload_handler` →
     `sess.close()` → `backend.close()` → `rknn3_destroy(ctx)`，
     所以返回时 NPU 池是真的归还了。反过来先 shutdown，worker 会在
     LLMSession 还活着时退出，池子一直被占，**光杀进程没意义**。
  2. `teardown=False` 让 `rpc_client` 活到 shutdown RPC 发完为止。
     **这里曾有一个真 bug**：`_teardown_rpc_client` 会把
     `inst.rpc_client` 置 `None`，而 shutdown RPC 的守卫是
     `if inst.rpc_client is not None` —— 于是 shutdown 永远不执行，
     worker 收不到 stop event，3s 后只能 SIGKILL，优雅清理（audio
     writer close / served session close / side hub close / atexit
     清 `/dev/shm`）全部被跳过。改 `unload_instance` 签名时别把
     `teardown=False` 拿掉。
  3. SIGTERM 是第二保险：worker 侧装了等效 handler（`_base.py`
     SIGTERM/SIGINT），且不依赖 Requester 还能用 —— 这是
     `adopted_local`（`proc is None`）唯一的优雅退出途径。
  4. 3s 宽限 → SIGKILL 兜底。
  - 验收判据不能只看状态，必须**制造 NPU 压力**：禁用 npu0 上的模型
    后，在**同一张卡**启另一个同量级模型。能起来 = 池真释放了。
- **禁用会打断正在进行的推理（已知，不修）**：`LLMLocalSession.close()`
  是 `executor.shutdown(wait=False)`，不等在跑的推理，禁用会让
  `rknn3_destroy` 与推理并发。unload-only 时代同样如此，不是新回归，
  但进程没了之后只能重来，后果更明显。兜底靠各业务自己的
  超时重建（VAS `restart_and_rebind_instance_local`）。
- **Orphan worker 处理（v0.8 dedup → v0.9 强化）**：uvicorn reload 路径下
  `Popen(start_new_session=True)` 起的 worker 脱离原进程组，reload
  SIGTERM 不会传过来 —— 每次 reload 都会留一个孤儿。`_spawn_compute_worker`
  在 spawn 前先扫 worker 进程，命中就走 `_adopt_existing_worker` 接管
  而不 spawn，避免 NPU 内存被无意义地消耗。`_adopt_existing_worker`
  拿到 info 后对比 `info.hostname == socket.gethostname()` 区分本机
  孤儿 vs 远端 peer。Worker 端 `info_handler` (v0.8) 返回
  `pid` / `hostname` / `device_name` 字段供主进程判断。

  v0.9 修了一个老 dedup 的洞：`_find_orphan_worker_pid` 只返**第一个**
  匹配 PID，剩下的孤儿永远没人收。新实现
  `_find_workers_by_name(model_id)` 返 `List[int]`，扫出**所有**同 model
  worker；`_adopt_existing_worker` 接管 1 个后 SIGTERM 其余（5s 宽限 →
  SIGKILL 兜底）；`boot_compute_services` **在 spawn 循环之前**再跑一次
  `_cleanup_unregistered_workers` 兜底扫
  （同时清掉 config.compute.autostart 移除后残留的孤儿 + proc 文件
  异常权限扫不到的边界情况）。**顺序不可调换**：清理若排在 spawn 之后
  会和 fire-and-forget 的 spawn 线程抢 `tracked_pids`，把刚 Popen 还没
  登记进 `_instances` 的 worker 判成孤儿杀掉（实测首个 autostart 模型
  会在 spawn 同一秒以 `rc=-15` 挂掉）。

  **Worker 进程命名**：worker 启动后通过 ``prctl(PR_SET_NAME)`` 把
  自己的 comm（`/proc/<pid>/comm` / `ps -c`）设为 ``worker:<alias>``，
  alias 来自 ``MODEL_PRESETS[model_id]["worker_name"]``。预设：
  - qwen3-vl-4b → `worker:vlm-4b`（13 字符）
  - qwen3-tts → `worker:tts`（10 字符）
  - qwen3-vl-embedding-2b → `worker:emb-2b`（12 字符）
  - qwen3.5-vl-4b → `worker:q5-vl`（12 字符；多模态 LLM+vision）

  受 TASK_COMM_LEN=16 限制 alias 必须在 8 字符内（15 - len("worker:")）。
  命名放在 worker 端 main() 里调 —— preexec_fn 里调 prctl 会被紧随的
  exec() 用新 argv[0] basename 覆盖。`_find_workers_by_name` 同时
  fallback 扫 cmdline 兼容老 worker（reload 后的第一次清理会识别
  comm="python" 的历史 worker，全清之后 cmdline 退化为 no-op）。
- **FastDDS participant_name 命名约定 (v0.8)**：每个 DomainParticipant
  的名字都带 `config.jsonc["base"]["device_name"]` 后缀，方便 LAN daemon
  的 `/peers` 端点和 DDS 调试日志一眼区分板子：
   - 主进程：`wdnwebui-main-{device_name}`
   - discovery Hub（注册发现页）：`wdnwebui-discovery-{device_name}`
   - compute worker：`compute_worker-{device_name}-{model_id}`
  worker 的 `info_handler` 返回的 `device_name` 字段是上述 participant
  name 后缀的来源；主进程 probe 后写到 `ModelInstance._peer_name`，
  算力配置页"网络发现"卡片标题渲染为 `<model> @ <device_name>`。
  device_name 缺失时 fallback 到 `socket.gethostname()`，保证
  participant_name 永远非空。
- **重启 4 步 (Q3)**：`POST /api/compute/restart {model}` →
   ``stop_instance_local(model)`` (unload → RPC shutdown → wait 3s →
   kill -9) → ``ensure_worker_for_model(model)``（probe → adopt /
   spawn）。**不**自动 bind —— 重启后 worker 回到 ``not_loaded``，
   用户需在算力配置页再点"启用" + 选 NPU 才完成 reload。

## RTSP 卡顿根因（2026-09 海康 NVR 实测，结论已落定）

**用户报告**：「视频目标定位」页打开 RTSP
`rtsp://admin:admin123@172.16.9.79/cam/realmonitor?channel=1&subtype=1`，
前 20-40 秒 fps=1-4，之后稳定 25 fps。VLC 同一源流畅。

### 四组对照实验（决定性证据）

| 实验 | 工具 | transport | fps | p50 / p95 / p99 / max | 含义 |
|---|---|---|---|---|---|
| A | webui (wdnav via uvicorn) | TCP | 1-4 → 25 | n/a | strace 显示 video-decode 线程在 `wdnav/mpp.py:1119 send_packet` 等 C 端返回 |
| B | 独立 wdnav 直连 | TCP | **3.9** | 0.2 / 1.3 / 6104 / 12198 ms | 同 pattern，**与 webui 无关** |
| C | 独立 PyAV 直连（**无 mpp decode**） | UDP | **33.1** | 1269 / 3429 / 3589 / 3629 ms | UDP transport 8.5x 提升；前 25 帧瞬时涌入，后续 40ms 稳态 |
| D | 独立 wdnav 直连（**有 mpp decode**） | UDP | **6.4** | 0.3 / 1.2 / **6098** / 6293 ms | UDP 修复**不充分** |

### 根因（已锁定）

**wdnav 的 `_send()` (`wdnav/video.py:855-869`) 在 mpp VPU packet queue 满时进入
BUFFER_FULL → sleep(5ms) → BUFFER_FULL 重试循环**：

```python
while True:
    try:
        self._decoder.send_packet(annexb, pts=pts, eos=False)
        return True
    except RuntimeError as e:
        if _BUFFER_FULL_TOKEN not in str(e):
            raise
        # VPU's packet queue is saturated; pull a frame and try
        # again. If no frame is ready yet (cold start), give the
        # hardware a brief moment to catch up.
        frame = self._drain_one_raw(timeout_ms=_DRAIN_TIMEOUT_MS, ...)
        if frame is not None:
            self._prefetched = frame
            return True
        _t.sleep(0.005)    # 5ms
```

Test D 显示单帧最大耗时 **6293ms** = **1260 次 BUFFER_FULL → sleep 循环**
（6293/5 ≈ 1258），p99 = **6098ms** 与 p50 = **0.3ms** 相差 20000 倍。
demux 是瞬时的（p50=0.3ms），瓶颈在 mpp decoder 端。

### 误判路径（避免重蹈）

| 猜测 | 实测结果 | 原因 |
|---|---|---|
| 服务端 cv2.imencode 软编码 1080p 太慢 | imencode=3.5ms | 流实际是 640×480，1080p 假设错误 |
| 浏览器 canvas 反复重分配 GPU 纹理 | 实际解码线程 fps=3.9 fps | webui CPU 4% 空闲，瓶颈不在前端 |
| detector (NPU) 抢资源 | sinks=0.2ms | 用户没开 detector |
| mpp packet queue (extra_buf_count=24) 太小 | `extra_buf_count` 是**输出 frame buffer pool**，不是 packet queue | `cshim_video_decoder.c:228` 调用 `mpp_buffer_group_limit_config` 控制解码器**输出** buffer pool，与 send_packet BUFFER_FULL 无关 |

### RTSP transport 的影响

- **TCP**（默认）：海康 NVR RTSP over TCP 走 libavformat 标准 keepalive
  (RTCP-RR GET_PARAMETER) 协商路径，前 20-40 秒 ffmpeg 按 server 反馈节
  奏退避 → 客户端看似「卡顿」。协商完成后稳态 25 fps。
- **UDP**（`av.open(rtsp_url, options={'rtsp_transport': 'udp'})`）：绕开
  keepalive 协商，PyAV 直接 demux → **33.1 fps（Test C）**。但 wdnav
  内部 mpp decoder 仍按 5ms 重试循环处理 BUFFER_FULL → **6.4 fps
  （Test D）**。

**结论**：UDP transport 是必要非充分条件——它修了 demux 侧的协商慢启动，
但 mpp decoder 端 BUFFER_FULL 循环仍是上限。**真正修复点在 wdnav SDK
内部**（`video.py:855-869` 的 5ms 重试逻辑），不在 webui。

### 修复路径

1. **wdnav SDK 内部**：把 BUFFER_FULL 重试 sleep 从 5ms 改为更长（如 50ms，
   减少总 wait 时间 = sleep 数 × 单次 sleep）；或者改成事件驱动
   （mpp 解码出帧时唤醒 read0 线程）。需 wdnav 维护者改。
2. **当前可接受**：告诉用户「RTSP 协商需要 20-40 秒，之后稳定 25 fps」，
   这是海康 NVR + libavformat 的**已知行为**而非 bug。
3. **不要做的**：改 `extra_buf_count`（输出 buffer pool，不影响 packet
   queue）；改 `cv2.imencode`（已经在 3.5ms）；改前端 canvas
   重分配（webui CPU 4% 空闲，不是瓶颈）。

### 诊断埋点（保留作为运维工具）

`app/core/video/session.py` 加了 `_TIMING_ENABLED` 探针，
环境变量 `WDNWEBUI_VIDEO_SESSION_TIMING=1` 开启，每 30 帧（≈1.2s）
输出一行到 LOG.info / stderr / `/tmp/wdnwebui_video_session_timing.log`：

```
video_session timing: 30 frames in 1.20s (25.0 fps) read0=30.3ms numpy=1.7ms
imencode=2.7ms sinks=1.8ms put=0.0ms jpg=29KB 640x480 q=0 dropped=0
live=True frame_pos=175
```

字段：`read0`（wdnav 单帧）、`numpy`（RGA）、`imencode`（cv2 JPEG）、
`sinks`（detector）、`put`（queue）、`jpg`（输出体积）、`size`（编码前尺寸）、
`q`（队列深度）、`dropped`（丢帧计数）、`live`（RTSP 标记）、
`frame_pos`（解码器位置）。

**默认关闭**（env var unset 时零开销：仅 1 个分支测试），用于：
- 未来 RTSP / V4L2 卡顿诊断
- 验证 wdnav SDK 修复后的实际 fps 改善
- 比较不同 NVR 的 codec 兼容性

**不要做的事**：
- 不要 commit `/tmp/wdnwebui_video_session_timing.log`（gitignore
  应该已经排除 `/tmp/`）
- 不要在生产环境长开（每 30 帧一行 × 多路 = journalctl 噪声）
- 不要拿这个探针替代 `py-spy` / strace 做深度诊断（这是粗粒度的 stage
  timing，深挖还是看 Python 栈 + syscall trace）

### 调查时间线（保留供后人参考）

- 2026-09 用户报告「视频目标定位」RTSP 卡顿
- Test A（strace）：video-decode 线程在 `wdnav/mpp.py:1119 send_packet` 等
- Test B（独立 wdnav TCP）：3.9 fps → 根因不在 webui
- Test C（独立 PyAV UDP）：33.1 fps → UDP transport 优势明确
- Test D（独立 wdnav UDP）：6.4 fps → UDP 不是充分修复
- 最终根因：`wdnav/video.py:855-869` 的 mpp BUFFER_FULL 重试循环

**wdnav 修复**（如果 SDK 维护者接受）需要改的是：

```python
# wdnav/video.py:855-869 改为：
while True:
    try:
        self._decoder.send_packet(annexb, pts=pts, eos=False)
        return True
    except RuntimeError as e:
        if _BUFFER_FULL_TOKEN not in str(e):
            raise
        frame = self._drain_one_raw(
            timeout_ms=200,  # 5ms → 200ms, 减少重试次数
            out_w=out_w, out_h=out_h, out_fmt=out_fmt,
        )
        if frame is not None:
            self._prefetched = frame
            return True
        # 不再 sleep,直接 return False 让 read0 上层 backoff
        # （read0 在 .venv/src/wdnav/src/wdnav/video.py:573 的外层循环
        # 会重新 demux 拿下一个 packet,自然 backoff）
        return False
```

`return False` 让 read0 在 `self._send()` 返回 False 时退出当前循环,
`read0` 已经在外层 `for raw_packet in self._container.demux(...)` 循环,
下一个 demux 会自然阻塞等 mpp 解码腾出位置。**单帧 800ms BUFFER_FULL 重试
变成 0ms**（不 sleep）+ 自然的 demux backoff（demux 等新 packet,
新 packet 触发 mpp 解码出帧,出帧后 read0 自然返回）。

### 实测补丁效果（2026-09 本地验证 — 部分成功）

**应用**：将 `wdnav/video.py:838` 的 `timeout_ms=_DRAIN_TIMEOUT_MS` (5ms)
改为硬编码 `40ms`，`:852` 的 `_t.sleep(0.005)` 改为 `_t.sleep(0.040)`。
**保留**模块级常量 `_DRAIN_TIMEOUT_MS = 5` 不动——它被
`_drain_one_raw()` 默认参数引用（`cshim_video_decoder.c` 内的
`rkv_dec_get_frame` 也复用 5ms 作 recv_frame 默认值），缩小
改动作用域。

**验证**：打开 RTSP `?channel=1&subtype=1`（640×480@25fps 海康），
打开 `WDNWEBUI_VIDEO_SESSION_TIMING=1` 重跑 timing log。

```
# Baseline (5ms drain + 5ms sleep, 无 patch)         # Patched (40ms + 40ms)
#1  7.48s  4.0 fps  read0= 203ms                       #1  7.52s  4.0 fps  read0= 205ms   ← 启动爆发(相同)
#2 24.54s  1.2 fps  read0= 813ms                       #2 18.51s  1.6 fps  read0= 612ms   ← 改善 ~25%
#3  0.10s 305.0 fps read0= 0.2ms  ← cache drain       #3  0.13s 226.4 fps read0= 0.3ms   ← cache drain
#4 18.44s  1.6 fps  read0= 610ms                       #4 18.46s  1.6 fps  read0= 611ms   ← 改善幅度小
#5 24.54s  1.2 fps  read0= 813ms                       #5 24.58s  1.2 fps  read0= 815ms   ← 没改善!
#6 12.30s  2.4 fps  read0= 407ms                       #6 24.61s  1.2 fps  read0= 816ms   ← 没改善!
#7  0.09s 343.1 fps read0= 0.2ms                       #7 12.29s  2.4 fps  read0= 406ms   ← 改善幅度小
```

**诚实结论：patch 提供部分改善（约 25%），未根治。**

- 中等拥塞窗口：24.54s/813ms → 18.51s/612ms（修复 ~25%）
- 重度拥塞窗口（24.58s/815ms, 24.61s/816ms）**纹丝不动**

**为什么 patch 没根治**：`_DRAIN_TIMEOUT_MS=5`/`sleep(0.005)` 是
**症状**而非**根因**。真正卡住的是 mpp VPU **输入 packet queue**（mpp
内部管理，wdnav Python/C API 都没有暴露设置项）：
1. wdnav 把 H.264 packet 喂进 mpp 输入队列
2. consumer 慢时 wdnav 喂包速度 > mpp 解码 + 消费速度
3. mpp 输入队列满 → `send_packet` 抛 `BUFFER_FULL`
4. C shim `rkv_dec_put_packet` 内部 retry (`RKV_DEC_RETRY_MAX=100`,
   1-50ms 指数退避，≈ 5s 上限)
5. C retry 耗尽返回 -1012 → Python `_send()` 看到 `ret=-1012` 字串含
   "buffer full" → 外层 retry 继续
6. nested retry 总耗时 = C 5s 上限 × Python 外层多次 retry，可达 10+ 秒

`_DRAIN_TIMEOUT_MS` 改 40ms 只缩短了 Python 外层 retry 节奏，**没修
根因**。C shim 1-50ms 退避 + Python 40-40ms 退避 = nested retry 总耗时
仍 200-800ms（vs baseline 813ms）。

### 第二次尝试：`extra_buf_count=96`（也失败）

**假设**：输出 frame pool（`mpp_buffer_group_limit_config`）满了导致 mpp
没法继续解码 → 改大 `extra_buf_count` 应该缓解。9 月先做了纯 Python
改动：把 `MppDecoder.__init__(extra_buf_count=24)` 改成读
`WDNAV_EXTRA_BUF_COUNT` 环境变量，默认改为 96。不需要 rebuild .so。

**实测**：
```
# extra_buf_count=24 (_send() patch only)        # extra_buf_count=96 (this attempt)
#1  7.52s  4.0 fps  read0= 205ms                   #1  7.51s  4.0 fps  read0= 204.7ms
#2 18.51s  1.6 fps  read0= 612ms                   #2 18.52s  1.6 fps  read0= 612.3ms
#3  0.13s 226.4 fps read0= 0.3ms                  #3  0.12s 257.8 fps read0= 0.3ms
#4 18.46s  1.6 fps  read0= 611ms                   #4 12.31s  2.4 fps  read0= 406.7ms
#5  0.10s 297.1 fps read0= 0.3ms                  #5  0.09s 331.1 fps read0= 0.2ms
#6 24.58s  1.2 fps  read0= 815ms                   (same pattern as #24 baseline)
#7  0.09s 343.1 fps read0= 0.2ms
#8 24.61s  1.2 fps  read0= 816ms
#9 12.29s  2.4 fps  read0= 406ms
#10 6.29s  4.8 fps  read0= 205ms
#11 12.31s 2.4 fps  read0= 406ms
```

**逐位相同**——extra_buf_count=96 没改善。**结论**：瓶颈不在输出 frame pool，
而在 mpp **输入 packet queue**（mpp 内部管理，wdnav 不暴露设置项）。

**为什么 96 没用**：C shim 里 `extra_buf_count` 只控制
`mpp_buffer_group_limit_config` 的 max size + `handle_info_change` 里
prime allocation 数量。**不影响** mpp 内部 packet queue size。
`MPP_DEC_SET_CFG` 只设了 `split_parse`，没有 `MPP_DEC_SET_PKT_GROUP`
这类设置。

### 第三次尝试：drain-first backpressure（成功 ✅）

`py-spy dump` 抓到 video-decode 线程在 `send_packet
(wdnav/mpp.py:1143) → _send (wdnav/video.py:829) → read0
(wdnav/video.py:573)` 栈里，确认是 C shim 的 retry 循环
（`RKV_DEC_RETRY_MAX=100` × 1-50ms 退避）热点。检查 `read0()`
（`wdnav/video.py:573-625`）发现流程是 **先 push 再 drain**：
TCP burst 时 PyAV 一次性回送 30+ 个 packet，`read0` 循环
全部推给 mpp，input queue 灌爆，触发 retry cascade。

**修复**：把循环改成 **drain-first**——每个 iteration 先
`_drain_one_raw(timeout_ms=5)` 拉一个 frame 腾出 output pool
slot，再 `_send(packet)` 推 packet。

第一次实现有个 bug：drain 超时（frame is None）时 return
`(False, None)`，外层 `_decode_loop`（`app/core/video/session.py:417-440`）
把它当 EOF 处理杀掉整个解码循环，session 立刻 `state=eof frame_pos=0`。
改成 drain 超时时 **`continue` for 循环 + `time.sleep(0.001)`**
（1ms 节流，足够让下一个 demux 拿新 packet），decode loop 继续
跑。

**实测结果**（开 RTSP 等 35s 看 `/tmp/wdnwebui_video_session_timing.log`）：
```
#  1 30f in  1.59s =  18.9 fps | read0=  5.9ms   ← 启动爆发（接受）
#  2 30f in  1.20s =  25.0 fps | read0= 31.3ms  ← 稳态！
#  3 30f in  1.20s =  25.0 fps | read0= 32.2ms
...（35/36 个 windows 都是 24.9-25.2 fps）
# 36 30f in  1.20s =  25.1 fps | read0= 31.4ms
```

**完美**：
- 35/36 windows 稳态 25.0 fps
- read0 中位 ~31ms（之前 200-800ms）
- **24s 慢窗口完全消失**
- mean=24.87 fps，p99 ≈ 25.2 fps

`session state=playing frame_pos=894`（35 秒后），约 25.5 fps。

**为什么 drain-first 有效**：output pool 有空 slot 时 mpp 接受
新 packet（input queue 不满）→ 解码正常 → 释放 frame。
之前 send-first 让 mpp 的 input queue 满 + C shim 100 次重试
每帧 ~800ms。现在 drain-first 让 mpp output pool 始终 ≤ 24
（每帧 1 in 1 out），input queue 永远 ≤ 1（因为 send_packet 之前
output pool 已空）。

### 清理（已执行 ✅）

诊断完成、修复生效后，按"冗余+无效"标准清理了 wdnav SDK 的两处改动：

| 改动 | 处理 | 原因 |
|---|---|---|
| `_send()` 40ms/40ms patch | ✅ **revert 回 5ms/5ms** | drain-first 在 read0 源头解决了 backpressure，retry 层 5ms vs 40ms 无差异 |
| `MppDecoder.__init__` `WDNAV_EXTRA_BUF_COUNT` env var | ✅ **revert 回 `int=24` 默认** | 96 没改善（输出 pool 不是瓶颈），env var 是无效实验 |

**保留**：
- `wdnav/video.py:read0` 的 drain-first backpressure（**核心修复**）
- `app/core/video/session.py` 诊断埋点（env-gated，env 关闭时零开销）

## TTS (语音生成) page

## TTS (语音生成) page

The `#page-tts` section drives Qwen3-TTS through the SDK's
``TTSBackend`` (defined under
``.venv/src/wdnai/src/wdnai/backends/tts.py`). The full RKNN3 driver
(four ``.1828.rknn`` models, embed tables, prefill construction,
talker/code_predictor/speech_decoder pipeline, ``AudioPlayer``
streaming) is inlined into the same file as ``Qwen3TTSDemo`` —
vendored in-tree so the SDK is self-contained and no longer has to
find ``qwen3_tts_demo.py`` anywhere on the host filesystem. The
model layout, .1828.rknn naming, and AudioPlayer streaming path
are all documented in
``.venv/src/wdnai/src/wdnai/models/README.md`` and
``.venv/src/wdnai/src/wdnai/backends/tts.py``.

* **Service** (`app/apps/tts/service.py`):
  - ``TtsSession`` singleton, lazy-imports wdnai at first use so a
    non-RK dev box still boots (just reports ``state="unavailable"``).
  - State machine mirrors ``VideoSession``/``WdnaiDetector``:
    ``idle → start() → loading → running → done/error/stopped``.
    The state transitions are broadcast over ``/api/tts/ws``.
  - One daemon worker thread per request. The worker forwards
    ``stop_event`` to the SDK so a stop issued during prefill or
    decode unwinds the RKNN sessions cleanly; ``stop()`` flips the
    state immediately so the UI is responsive while the worker
    thread is still draining.
  - Output history is an in-memory deque (max 50). Each entry:
    ``{ts, text, language, speaker, output, size, audio_sec, frames}``.
* **Endpoints** (`app/apps/tts/api.py`):
  - ``POST /api/tts/start`` ``{text, language, speaker, ref_speaker,
    volume, instruct, no_play, non_streaming_mode, output}`` — kicks
    off a synthesis. Refuses with 503 if a run is in flight.
  - ``POST /api/tts/stop`` — flips state to ``stopped`` immediately
    and propagates ``stop_event`` to the SDK.
  - ``GET  /api/tts`` — current snapshot (state / available / frames
    / audio_sec / elapsed / output / error / request / history_count).
  - ``GET  /api/tts/history`` — list of recent output WAVs.
  - ``WS   /api/tts/ws`` — pushes the snapshot on every state /
    frames / error change. Auto-reconnect with exponential backoff
    is handled on the client side (``app/static/core/framework.js (前端核心) + app/static/apps/<id>/app.js (各 app)``
    ``TTS.ensureWs``).
* **Frontend** (``app/static/index.html.template`` + ``#page-tts`` markup):
  - Sidebar entry ``语音生成`` sits between ``视频检测`` and
    ``系统日志`` (mutually exclusive with other pages — the sidebar
    just toggles the ``.hidden`` class on each ``.page``).
  - Textarea + speaker/language selectors + volume slider (default
    30%, range 0–100, step 5; the label mirrors the slider value
    live via the ``input`` event).
  - Start / Stop buttons, status line, history list below. Cmd/Ctrl
    + Enter inside the textarea fires 开始生成.

* **VoiceClone speaker embeddings are bundled, not filesystem-loaded.**
  The upstream C++ demo's ``qwen3tts_speaker_embed.h`` is a static
  ``std::unordered_map<std::string, std::vector<float>>`` holding the
  reference vectors for ``girl_base`` / ``ahu`` (each 2048-dim fp32).
  That file ships only with the rknn3 dev tree and is never installed
  on a clean board. To keep the SDK self-contained, both vectors are
  encoded inline as zlib-compressed fp16 blobs in
  ``_BUNDLED_SPEAKER_EMBEDDINGS_B64`` (``.venv/src/wdnai/src/wdnai/backends/tts.py:209``)
  and decoded by ``_get_bundled_speaker_embeddings()`` on first use.
  ``Qwen3TTSDriver._load_speaker_embeddings()`` (line 635) returns the
  bundled dict directly — there is no runtime walk-up filesystem
  search anymore, so ``ref_speaker="girl_base"`` / ``"ahu"`` always
  resolves correctly regardless of the host's rknn3 layout.

**Volume semantics** — the slider value is passed straight through to
the SDK as the linear amplitude scale (``chunk *= volume`` on the
decoded float32 PCM). A slider of 0.5 is 0.5 amplitude, no
perceptual curve. The web default is 0.1 so the played PCM starts
at ~10% on hardware whose mixers default to 100% — crank the slider
to taste.

**Output WAV format** — 24 kHz, mono, 32-bit IEEE float (format tag
3, matching the C++ demo). Python's stdlib ``wave`` can't read
format-3; for inspection, use ``soundfile.read(...)`` or parse the
RIFF header by hand.

## Video analysis search (监控分析检索) page

`#page-video-analysis-search`（卡片 id `video-analysis-search`，`app/apps/video_analysis_search/`）
是演示页第 4 项，**一张卡片承载两种能力**（页面顶部 checkbox 勾选，可同时开）：

* **Service** (`app/apps/video_analysis_search/service.py`):
  - `VideoAnalysisSearchService` 单例（`get_service()`）。状态机
    `idle → opening → running → summarizing → done`，`stopped` / `error` 是
    旁支终态。
  - **独立 `wdnav.VideoCapture`** —— 不挂 `VideoSession.add_frame_sink`，避免
    抢占音视频描述 / 视频监控的同一源。
  - **LLMSession 必须从 ComputeRegistry 取** —— `get_session("swcap_video_describe")`，
    复用 `qwen3-vl-4b` 实例；找不到 capability 抛 `RuntimeError("功能未启用: ...")`，
    HTTP 502 透传。
  - **进程级 `_llm_session_call_lock`**：detector 与 VAS 共享同一 LLMSession；
    `reset() + respond(...)` 不是线程安全的，必须 `with lock:` 包住。锁不绑
    capability —— VLM 端所有消费者串行化，TTS 用另一组 session 互不干扰。
    `app.core.compute.registry.get_session_call_lock()` 暴露锁对象。
  - **decode / infer pipeline 反压**：`_decode_loop` 用 `queue.Queue(maxsize=1)`
    + `threading.Event` 做 backpressure —— 采样时等 `_worker_ready`，worker
    拿走样本立即 set；避免新样本覆盖尚未处理的旧样本。
  - **per-frame SceneFrame schema**：用 wdnai `@Generable SceneFrame(LMOutput)`
    让模型输出 5 字段结构化 JSON（`text` / `scene` / `actors` / `action` /
    `objects`）；SDK 解析失败时 `_split_scene_frame_result` 五分支兜底
    （dict 归一化 → JSON parse → markdown regex → raw str）。
    **⚠️ class docstring 会进 prompt（分析幻觉陷阱）**：
    `@Generable` 类在 wdnai 里被转成 pydantic BaseModel，**class docstring
    会成为 `model_json_schema()` 的顶层 `description`**，随
    `session._schema_hint_section` → `_safe_schema` 注入每条 prompt。
    SceneFrame 原先那段 48 行 docstring（含一处"❌ 反例"
    `{"text": "scene 小区入口 actors …"}`）经此路径被 4B 模型当模板抄成
    输出 —— 用户报的"小区入口"/"城市街道"等与画面无关的逐帧结果就是这么
    来的。**规矩**：class docstring 只留一句中性描述；
    字段顺序 / 长度约束 / schema-echo 说明一律放函数 `#` 注释，**绝不在
    docstring / Field description 里写具体示例值或反例**。
    schema 大小实测由 2591 → 944 字符（字段/类型/maxLength/required
    逐字段不变，结构化输出填充率正常，scene 74% / text 78%）。
  - **Apollo 三段式综述**：场景检测（cv2 HSV 直方图 + Bhattacharyya 距离）
    → per-scene chunk 小结（每段 ≤120 字，`max_new_tokens=200`）→ 最终综述
    （`max_new_tokens=400`）。每完成一个 chunk 广播 `state` 进度，
    前端用 `summary_progress: [done, total]` 显示 N/M。
  - **持久化**：每次 `start()` 分配新 `run_id`（uuid4 hex），**只有分析成功
    （done）才落盘**到 LanceDB（`app/core/video/cache.py` 的
    `kind='analysis'` 行，`WechatVideoCache.save_analysis` / `list_analysis`）。
    error / stopped 等中断态的逐帧结果不完整：落库会被 `lookup_analysis`
    命中并复用残缺结果，历史列表还会出现"失败但有缓存"的条目 —— 所以
    `service._maybe_persist` / `_persist` 对非 done 状态直接短路，
    `cache.save_analysis` 另校验 `payload["state"]`（缓存层最后防线）。
    JSON 含 `events[]` + `summary` + `fps` + `total_frames`。**老 run 没
    `fps` 字段时** `list_runs` 走 PyAV 探测自愈并回填。
* **API** (`app/apps/video_analysis_search/api.py`):
  - `GET  /api/video-analysis-search`             — 当前快照（state / 总帧 /
    进度 / 综述 / available / init_error）。
  - `GET  /api/video-analysis-search/events`      — per-frame events + summary。
  - `POST /api/video-analysis-search/analysis/open` `{source,
    every_n, sample_mode, change_threshold}` — 启动一次；400
    source/every_n/sample_mode 校验失败（`sample_mode` 收
    `manual` 或 `frame_judge.metrics()` 里的度量名，当前只有 `gscore`；
    **已下架的 `z_mad` / 字面量 `auto` 放行并迁移到 gscore 且丢弃旧阈值**，
    不要给老客户端报 400 —— 升级后一点「开始」就报错比静默失效更难排查）；
    503 wdnai 不可用 / 功能未启用。
    **分析提问已固化为服务端常量** `service.ANALYSIS_PROMPT`
    （`"请对画面内容进行分析总结"`）：body 里即使带 `prompt` 也被忽略，
    前端不再收集（见下「前端」段）。改问法只改该常量 —— prompt_hash
    随之变化，旧分析缓存会失配、同视频重跑一次即可重建。
    503 wdnai 不可用 / 功能未启用。
    历史 run 恢复（`service.py:_restore`）走同一套迁移，否则落库的
    `sample_mode` / 缓存键会与真正在跑的判据对不上。
  - `GET/POST /api/video-analysis-search/judge[ /metrics]` — VAS 自己的
    帧判据配置（与三张卡共用的 `/api/video/judge` **分开**：判据挂在
    VideoAnalysisService 上，不在共用的 VideoSession 里）。运行中改配置
    只暂存，下次 `open` 才生效。
  - `POST /api/video-analysis-search/stop`        — 用户中途停止（不出综述）。
  - `POST /api/video-analysis-search/close`       — 彻底关闭。
  - `POST /api/video-analysis-search/events/clear` — 清空内存 events + summary。
  - `GET  /api/video-analysis-search/runs`        — 历史 run 列表（按 finished_at 倒序）。
  - `GET  /api/video-analysis-search/runs/{run_id}` — 加载历史 run 到内存。
  - `DELETE /api/video-analysis-search/runs/{run_id}` — 删盘上 JSON。
  - `GET  /api/video-analysis-search/runs/{run_id}/facets` — scene/actor/object/action
    去重集合（前端反查 chip 候选）。
  - `GET  /api/video-analysis-search/runs/{run_id}/query?scene=&actor=&object=&action=&exact=&q=&frame_min=&frame_max=&limit=` —
    反查：多维 AND 过滤 + 两种搜索模式（`exact` > `q` 互斥）。
  - `GET  /api/video-analysis-search/runs/{run_id}/frame/{frame_pos}` —
    从原始视频取指定帧的 JPEG（PyAV v4l2m2m seek + wdnav read0 fallback）。
  - `WS   /api/video-analysis-search/ws`          — server-push `{type:"event"|"summary"|"state"|"hello"}`。
* **前端** (`app/apps/video_analysis_search/static/app.js` `VAS` 模块 + `#page-video-analysis-search`):
  - 顶部进度条 + 阶段标签（`未启动 / 正在打开 / 分析中 / 生成综述中 / 已完成 /
    已停止 / 错误`），状态色随 `data-state` 切换；分析中显示"已读 N/M · 已
    分析 X/Y · LLM 队列 Z"。
  - 左侧表单：视频源（"浏览…"复用 `VdFsModal._sourceTarget = sourceEl` 机制，
    current 项目已有，写路径回到 `#vcs-source`）+ 帧判据控件 + 开始 / 停止 /
    清空记录按钮。**分析提问不在此页**：已固化为服务端常量
    `service.ANALYSIS_PROMPT`，页面与向导都不再收集（`#vas-prompt`
    textarea 与向导「分析要求」步骤均已删除）；历史分析子行也不再显示
    「提示词: …」摘要（固化后恒定，无区分意义）。
  - 右侧综述框 + 历史分析列表（每条带状态徽章 + `加载 / 反查 / 删除`
    三个按钮横排，删除用 `margin-left:auto` 推到最右避免误点）。
  - 下方 `<details open>` 逐帧分析列表（默认折叠，新事件抵达时只 push、
    不自动滚动；run 终态自动折叠腾出空间给综述）。
  - **反查 modal** (`#vas-query`)：场景下拉 + 人物/物体/动作 chip 多选 +
    精准匹配 / 模糊搜索 二选一 + 时间范围 `HH:MM:SS`（on-blur 校验，
    超总时长自动截断 + 提示）。结果 list 显示 frame_pos / 时间戳 / 结构化 tag
    / 高亮 `<mark>`。点击结果 → **取帧 modal** (`#vas-frame-modal`)：从原
    视频取对应帧 JPEG + 显示 frame_pos / fps / 该帧时间戳 / 画面描述 / 结构化。
  - 取帧路径：PyAV v4l2m2m `avformat_seek_file` 跳到 keyframe + 顺序 decode 到
    target（实测比 wdnav read0 从头顺序读快 ~150x），失败 fallback 到 wdnav。
* **DemoGuard 注册**：VAS 是演示页第 4 项，离开时弹 confirm 并 POST `/stop`；
  `active()` 判定 `state ∈ {opening, running, summarizing}`；`stopSync` 走
  `sendBeacon(/stop, POST)` 兜底。`beforeunload` 期间 JS 不能保证送达，最
  终兜底依赖服务端的 `get_service().close()` on shutdown。
* **资源预算**：与 detector 共用同一 qwen3-vl-4b 实例（~3.5 GB），典型一次
  VAS 分析 200 帧约 200s（every_n=60 × 1s/inference）。长视频（>10 分钟）
  建议 every_n ≥ 60。
* **依赖**：`requirements.txt` 加 `av>=11.0.0`（PyAV，硬取帧走 `v4l2m2m` 后端
  + `avformat_seek_file`）；`wdnav.VideoCapture` 已在内部 SDK 提供。

### 检索侧

选一个本地视频文件 → 按时间窗切片 → 每片 embedding → 文本余弦检索出
命中片段 → 点命中卡片用真实帧流播放。状态机
`idle → extracting → ready`，`stopped` / `error` 是旁支终态。

* **Service** (`app/core/video/search.py`)：
  - `VideoSearchService` 单例（`get_service()`）。
  - **LLMSession 必须从 ComputeRegistry 取** —— `get_session("swcap_video_retrieval")`，
    复用 qwen3-vl-embedding-2b 实例；找不到 capability 抛
    `RuntimeError("功能未启用: ...")`，HTTP 503 透传。`start_extract`
    加预检 `_ensure_session()`，不用等 worker 探测才报错。
  - **进程级 `_llm_session_call_lock`**：与 detector / VAS 共用同一套锁，
    `embed_text` / `embed_video` 的 `reset() + respond` 类推理必须
    `with lock:` 包住（embedding 的 `embed_*` 也是 NPU 推理，同样要
    与 VLM 端串行化）。
   - **切片 embedding**：PyAV 按 `[start, start+chunk_seconds)` 时间窗
     推进（步进 = chunk_seconds - overlap），每片取 `chunk_seconds` 个
     等距帧（1 秒/帧）缩放到 576×576，`embed_video` 得 2048 维 L2
     归一化向量，存内存 `List[{video_path, start_time, end_time,
     chunk_idx, fps, vec}]`。逐片推进 `slices_embedded` 并 WS 广播
     `progress`。`_probe_video_meta` 用 PyAV 探测 fps / 总帧数 / 时长。
     **⚠️ `embed_video` 必须传 ndarray 帧列表，绝不能传 `/dev/shm` 路径
     字符串**：worker 端 `_base.py` 把 `payload["frames"]` 交给
     `decode_attachments`，而后者对 **str** 的语义是"能 base64 解码就是
     图片，否则当文本前缀" —— 文件路径不是 base64 → `TextAttachment`
     → 被 `isinstance(ImageAttachment)` 过滤 → `frames=[]` →
     `{"status":"error","error":"no decodable frame in payload"}` → 每个
     chunk 都失败 → **`0/N` 切片成功**（用户报的
     "提取完成但 0/10 切片成功 embedding"）。传 ndarray 时 wdndds client
     经 `_bgr_to_path` 写 raw-BGR 描述符、worker `decode_attachments` 的
     dict 分支 `np.frombuffer().reshape(h,w,3)` 正好接住，且在 RPC 返回后
     自动 unlink。**VAS 页（`core/video/search.py`）与微信管线
     （`im_bridge/video_manager.py`）共用同一契约，改一处必须同步另一处。**
     （历史：曾用 `_write_frames_to_shm` 写 JPEG 路径列表，与 worker 契约
     不一致而从没稳定工作过；该函数与其配套 `cleanup_shm_session` /
     `_shm_session_id` 已整体移除。）
  - **检索**：`embed_text` 编码查询 → 遍历索引算余弦相似度 → 阈值
    过滤（默认 `sim_threshold=0.4`）+ `top_k`（默认 10）截断。索引为
    空时直接返回空命中。
  - **`iter_segment_jpeg(source, start_s, end_s, paced=...)`**：按播放
    顺序生成区间内每帧 JPEG（PyAV seek 到 keyframe + 顺序 decode +
    cv2 编码）；`paced=True` 时按源视频 fps 节流（**绝对时钟对齐**，
    sleep 补偿编码耗时，帧间隔精确 = 1/fps，缺失回退 `fallback_fps=25`）。
* **API** (`app/apps/video_analysis_search/search_api.py`)：
  - `GET  /api/video-analysis-search` — 快照（state / 进度 / query /
    hits / fps / source_exists）。
  - `POST /api/video-analysis-search/open` `{source}` — 选单个视频文件。
  - `POST /api/video-analysis-search/extract` `{chunk_seconds,
    chunk_overlap_seconds}` — 开始切片 embedding；chunk_seconds ∈
    {3,4,5,6}、overlap ∈ {1,2}（Pydantic/service 校验，非法 400）；
    实例未启用 503。
  - `POST /api/video-analysis-search/extract/stop` — 中途停止（退回 idle）。
  - `POST /api/video-analysis-search/query` `{text, sim_threshold,
    top_k}` — 文本检索；`embed_text` 同步 NPU 推理挪
    `run_in_executor` + **35s 超时**（SDK 卡死返 504，不阻塞 event
    loop；对齐 VAS 做法）。
  - `WS   /api/video-analysis-search/ws` — server→client：`hello`
    （连接即发快照）/ `progress`（embedded, total）/ `state`。
  - `WS   /api/video-analysis-search/play` — 播放命中片段：客户端首条
    JSON `{source, start, end}` → 服务端 daemon 线程跑
    `iter_segment_jpeg(..., paced=True)` 推二进制 JPEG 帧；客户端
    `{"action":"pause"}` / `{"action":"resume"}` 控制（
    `threading.Event`，pause 时阻塞 reader）；播完 `{"type":"eof"}`、
    出错 `{"type":"error"}`；断开连接即停线程。首条参数 10s 超时，
    非法 source / `end<=start` 直接 1008。
  - `GET  /api/video-analysis-search/frame?source=&t=` — PyAV seek 到
    `t` 秒附近取帧 JPEG（命中卡片缩略图），失败 404。
* **前端** (`app/apps/video_analysis_search/static/app.js` `VAS_SEARCH` 模块 + `#page-video-analysis-search`)：
  - 左侧表单：视频源（"浏览…"复用 `VdFsModal._sourceTarget` 机制）+
    切片档位（3/4/5/6s，重叠 1/2s）+ 提取按钮；中间进度条 + 阶段标签。
  - 右侧命中结果列表（相似度降序，每条带开始/结束时间），点卡片 →
    `vcsPlaySegment` 连 play WS 真实帧播放：等比 contain 填满播放
    窗口（`vcsSyncCanvasSize` 把 canvas 像素同步为 `.vd-stage` 显示
    尺寸，消除元素级黑边）、进度条按已收帧数/总帧数实时推进
    （`VAS_SEARCH.snap.fps` 算总帧数）、暂停/恢复/停止/播完重播。
  - `DemoGuard` 注册 `video-analysis-search`，离开页面 confirm + 停
    play WS + sendBeacon 兜底，与其它演示页一致。
* **ComputeRegistry preset**（`app/core/compute/registry.py`）：
  - `MODEL_PRESETS["qwen3-vl-embedding-2b"]`：`capability_types=
    ["swcap_video_retrieval"]`，`memory_estimate_mb=2000`，
    `estimated_init_seconds=30`。`kind="embedding"` 的 probe /
    `_init_session_subprocess` 分支按 vlm 文件布局校验（rknn_llm +
    rknn_vision + embed + tokenizer）。
* **资源预算**：qwen3-vl-embedding-2b 单实例 ~2 GB（LLM + vision 权重
  + 622MB embed 表）。检索单视频索引在内存，长视频（>1h @ 4s 片 /
  1s 重叠 ≈ 1200 片 × 2048 维 fp32 ≈ 10MB 向量）可忽略。播放走 PyAV
  顺序解码，不占 NPU。

## Text chat (文本对话) page

`#page-text-chat` 暴露纯文本对话（`swcap_text_chat` capability，由
qwen3.5-vl-4b 提供），
流式逐字渲染 ` thinking... response` 与正式回答，单页实例串行推理，
多轮上下文由主进程裁剪、wdnai 拼 ChatML。

**推理走 compute_worker 子进程（RPC）**。`get_session("swcap_text_chat")`
返回 `wdndds.LLMRPCSession`。`chat_agent._run_turn` 调
`session.respond(prompt=…, history=…, tools=[…], on_delta=…, max_new_tokens=…)`：
主进程自己裁剪历史并拼 ChatML（`wdnai.build_chatml_prompt`），工具 JSON
也在主进程解析回 `@Callable` 实例；wire 上只有拼好的 prompt 和
`{attachments, options}`，worker 侧不认识任何 schema 或工具。delta 走
独立 RELIABLE 旁路 topic 直达 `on_delta`，`respond()` 的返回值不因流式
而变，**没有轮询、没有 chat state 机**。

* **Backend** (`app/apps/text_chat/api.py`):
  - `GET /api/text-chat` — snapshot：state / busy / error / history_count /
    history_tokens / context_limit / max_new_tokens / current_request_id /
    last_result。
  - `POST /api/text-chat/send` `{text}` — 发起一轮推理；返回
    `{request_id, snapshot}`。400 text 为空 / 409 还在生成中 /
    502 capability 未启用。
  - `POST /api/text-chat/stop` — 置 `_worker_stop_event` 并调
    `session.stop()`（取消标志发布到旁路 topic，不等 RPC 应答；软停：
    worker 跑完本轮看到后回报 stopped，主进程丢弃本轮，不写历史）。
    本地 event 另负责工具轮之间的提前退出。
  - `POST /api/text-chat/reset` — 清空 history + history_tokens，回到 idle。
  - `WS /api/text-chat/ws` — server-push 流式 token + 状态变更；连接后立即
    推 `{type:"snapshot", snapshot:...}` hydrate。

* **Service** (`app/apps/text_chat/service.py`):
  - `_TextChatManager` 单例（`get_chat_service()`，lazy）；状态机
    `idle → loading → running → done/error/stopped`，与 wdnwebui
    其它服务风格一致。
  - **LLMSession 必须从 ComputeRegistry 取**（`get_session("swcap_text_chat")`）；
    找不到抛 `RuntimeError("功能未启用: swcap_text_chat")`，HTTP 502 透传。
    拿到的是 `LLMRPCSession` —— 本模块**不再访问 `session._backend`**，
    wdnai 的 monkey-patch（`make_result_callback` 包装）已整体移除，
    流式是接口的一部分（`on_delta`），不是补丁。
  - **`_ThinkingRouter`**：`respond` 的 `on_delta` 每次拿到增量时按
    ` response` 切频道推到 `thinking` / `answer` 两条流；`done` 时再用
    `chat_runner.split_thinking` 兜底切一次（处理 token 边界切碎等极端情况）。
  - 单 daemon worker thread（`text-chat-worker`）串行消费 `request_queue`；
    每轮开始前检查 `_worker_stop_event`。
  - **上下文管理在主进程**（`chat_agent._trim`）：一次无推理的
    `count_tokens` 控制面往返拿 worker 真实 tokenizer 的精确计数，从最新
    往回累计，超 `context_limit - reserve` 就裁掉更早的（滚动窗口）；
    reserve = `min(max_new_tokens, context_limit // 8)`（自适应输出预留，
    小 context 测试如 2048 不会误伤）。仅单条用户消息超限才报"单条消息
    过长"。worker 不可达时降级 `len(t)//2`（中文少算约 3×，偏保守）。
  - **多轮上下文**：app 端消息 deque 全量发给 worker；worker 每轮
    `sess.reset()` 清 C 端 KV 后 `_backend.run(prompt, keep_history=0)`。
    **历史中只写 `content:answer`，不写 thinking** —— 下轮 prompt 看不到
    thinking，保证模型下一轮从干净的对话记忆开始。
  - **history_tokens 语义**：每次 done 后覆盖为 worker 返回的
    `prompt_tokens`（最近一次实际喂给模型的 prompt token 数），前端据此
    显示上下文占用。
  - **context_limit 可配置**：`config.jsonc["compute"]["text_chat"]["context_limit"]`，
    默认 16384；每次推理前热读（`_resolve_context_limit`）。测试多轮裁剪
    可设 2048。
  - WS broadcaster pattern：worker 线程 push 到 `asyncio.Queue` 列表，
    WS handler 在主 event loop 里 `await queue.get()` drain；observer
    也走同一条路径，避免第二条 channel。

* **Worker 端（v0.16 起）= `wdndds.serve_local_session`，无自研 chat 通道**：
  worker 只做三件事：把 `state["session"]`（懒建，首次 `load` 时
  `LLMLocalSession`）交给 `serve_local_session`、用 `extra_ops` 挂上
  wdnwebui 自己的生命周期动词、跑主循环。**没有 `chat_run` /
  `chat_poll` / `chat_stop` / `infer_handler`**。
  - `extra_ops`（`_build_extra_ops`）分两类：
    **新增** `load` / `unload` / `shutdown` / `count_tokens`；
    **覆盖**内置 `info`（要报 `not_loaded` / `missing_model_dir` /
    `error` 这套自己的状态机）与 `embed_text` / `embed_video`
    （必须离开 replier pump 线程 + 超时，否则 NPU 卡死会占住串行 pump
    让整个 worker 变聋）。`respond` 不可覆盖（见
    `wdndds.ai_session._handle_request`）。
  - **`count_tokens`**：唯一还留在 worker 的 session 能力。历史裁剪是
    调用方策略，但**只有持有 tokenizer 的进程能精确计数**，而它在
    worker 里。一次无推理、无 NPU 的控制面往返（`_base._session_tokenizer`
    经 `backend.tokenizer` 拿）。降级是 `len(t)//2`，对中文少算约 3×。
  - 两个 service（`.../worker-<pid>` 与标准名）各挂一个 replier，**共用
    同一个 session**：`LLMLocalSession` 自己的单 worker 线程池负责串行，
    delta/stop 旁路 topic 按各自的 service 名派生，客户端天然对上。

* **流式 = `respond(on_delta=...)`，不再有 C 回调猴子补丁**：
  - delta 走 `service_name/delta` 这条**独立 participant** 上的
    **RELIABLE** topic（best_effort 会静默丢样本，表现为流式内容凭空
    缺块且无人报错）。worker 在 handler 返回前发布 stream-end 标记，
    客户端 `finally` 里 `bridge.done.wait(2.0)` 保证 delta 先于
    `respond()` 返回。
  - 顺序保证只有一条是可靠的：**拼接 == 返回值**。中间样本可能被合并，
    不要断言 delta 个数或逐块内容。
  - 历史教训（勿回退）：采集增量**不能靠独立线程轮询 `sink.text`**
    —— `_backend.run` 的 `session_run` 是同步阻塞 C 调用，解码回调持有
    GIL，并行轮询抢不到 GIL，`deltas` 恒为空（真机实测：状态 done 但
    前端无任何回复）。同样**不要再写 `install_patch()` 去包
    `make_result_callback` / `_make_sink_result_cb`**：wrapper 依赖 SDK
    私有符号，SDK 一重构就断（f61dddd 改名事故：算力配置页所有模型加载
    报 `AttributeError`，worker 静默退化成无流式）。

* **跨进程后立刻失效的东西 —— v0.16 实测踩过的坑，别再踩**：
  - **`sess._backend` 恒为 None**。会话在 worker 进程，任何
    `getattr(sess, "_backend").rknn_llm.session_stop()` / `.tokenizer`
    都**静默返回 None**，代码看起来在跑其实什么都没做。已修：
    取消走 `sess.stop()`，token 计数走 `count_tokens` extra op。
    新增任何"从会话摸模型内部"的代码前，先确认它在 RPC 会话上有意义。
  - **callable 过不了 JSON**。`respond(**overrides)` 的 overrides 会
    进 `options` 被 `json.dumps`。TTS 曾把 `on_audio_chunk` /
    `on_step` / `stop_event` 塞进 kwargs → `TypeError: Object of
    type Event is not JSON serializable`，整页 TTS 不可用。
    **规则：RPC 会话只传标量。** 需要回调时，worker 侧根据标量自己
    构造（见 `_base._tts_channel_options`：调用方只给
    `tts_pcm_socket_path`，worker 自己 connect 并装 forwarder）。
    `on_delta` 是唯一的例外 —— 它是 transport 的一等参数。
  - **同名 DomainParticipant 会静默毒化整条泵**。一个 worker 挂两个
    service（`.../worker-<pid>` + 标准名），若两次 `serve_local_session`
    各自新建 side hub，默认 `participant_name=f"wdnai-serve-<pid>"`
    完全相同 → 发现冲突 → replier 泵悄悄不响应，**所有** RPC 超时，
    而进程看起来活得很好。必须共用一个 side hub（topic 名按各自
    service 名派生，本来就不冲突）。
  - **主进程侧同一个坑，换了个受害者（v0.17 修）**。切换到
    `LLMRPCSession` 后，每个 session 的 side channel 默认也叫
    `wdnai-rpc-side-<pid>` —— **同进程内多个 session 就撞名**。症状
    极有迷惑性：第二个 worker 的 `load` RPC **永远 timeout**，实例
    卡在 `not_loaded`，用户看到的是「算力页启动完模型后整页卡住」
    （前端 1Hz 轮询也一起挂）。旧 `_RpcSessionProxy` 没有 side
    participant，所以这是协议切换新引入的回归。
    修法：`LLMRPCSession(side_hub=...)` + `_owns_side_hub`，registry
    用**一个** `_side_channel_hub` 服务所有 instance，`close_all()`
    统一关；默认 participant 名再加 service 尾巴兜底。
    诊断：第二个 worker 收不到任何 load reply，但 `ps` 里进程活着、
    stderr 干净 → 几乎一定是 side channel 撞名，不是 NPU 问题。
  - **backend 的返回形状不统一**。grounding 的 `run_images` 返回
    `GroundingResult` 而不是文本，`run_prompt` 契约是"返回原始文本"
    —— 不归一化就会在 worker 里抛 `TypeError: ... has no len()`。
    wdnai 的 `_normalize_backend_result` 收口（str / `(text, atts)` /
    带 `.text` 的对象），新增 backend 必须过这一关。
  - **shm 文件格式两端必须一致**。曾出现 client 写**原始 BGR**、
    server 用 `cv2.imdecode`（要 JPEG 字节）读 → 每帧都是 None。
    现在图像与 embed_video 帧统一走 `_bgr_to_path` / `decode_attachments`
    一套描述符，不再有第二套解码分支。

* **Proxy**（`app/core/compute/proxy.py`，v0.16 起只剩装配 + 一条策略）：
  - `make_rpc_session(...)` → 返回 `wdndds.LLMRPCSession`，复用 registry
    已有的 `Hub` / `Service` / `Requester`（不再自建 participant）。
    `_INFER_TIMEOUT_MS=120_000`（够 768px VLM 帧的 ~35s，又要能抓住真卡
    死）、`_CONTROL_TIMEOUT_MS=15_000`（控制面毫秒级，不该继承推理预算）。
  - `count_tokens(sess, texts)`：带降级，控制面失败不让整轮对话失败。
  - **不再有** `_ProxyResult` / `_AttachmentProxy` / `_decode_reply_shm` /
    `_encode_attachments`：schema 在调用方解析，附件与长文本的
    `/dev/shm` 路由由 wdndds 两端统一负责。**没有 LAN peer 降级**了 ——
    旧路径对跨机 worker 会把大字段全 drop（VFA 因此有"解析失败"占位
    路径），新路径靠 JPEG+base64 兜底，不丢内容。
  - **`respond()` 解析失败时返回裸 `str`**（SDK `_safe_validate` 的既有
    行为，不是新引入的）。消费方要认这个形态：`detector_base`
    的 `_result_raw_text()` / `_result_schema_parse_failed()` 就是为它
    写的。用 `getattr(result, "text", "")` 读 `str` 会静默得到 `""`，
    把"模型写了但没解析出来"变成"模型什么都没写"。

* **ComputeRegistry 配置 — `locate-anything` grounding preset**
  （`app/core/compute/registry.py`）：纯图像输入 + 文本输出（0–1000
  归一化 bbox + 描述），与 VLM 共用同款文件布局（`rknn_llm_path` +
  `rknn_vision_path` + `embed_path` + `tokenizer_path`），但绑定到
  `LocateAnything` backend（`wdnai.backends.locate_anything.LocateAnythingBackend`）。
  - `MODEL_PRESETS["locate-anything"]`：
    `kind="grounding"`（probe 按 vlm 文件布局校验），
    `capability_types=["swcap_grounding"]`，
    `memory_estimate_mb=5500`，
    `estimated_init_seconds=60`，
    `worker_name="ground"`（comm = `worker:ground`，13 字符；alias 6 字符
    在 TASK_COMM_LEN=16 的 8 字符硬限内）。
  - `_locate_anything_spec()` 工厂直接调
    `wdnai.SystemLanguageModel.locate_anything()`，文件缺失时抛
    `FileNotFoundError`；`_locate_anything_spec_template()` 跳过路径校验
    返回裸 spec，仅供算力配置页 / 启动预检的形态占位用，真实
    `LLMSession(model=spec, npu=K)` init 仍会报缺文件。

* **Frontend** (`app/static/index.html.template` + `app/static/core/framework.js (前端核心) + app/static/apps/<id>/app.js (各 app)` + `style.css`)：
  - 左栏 demo 区第 5 项 `<a data-page="text-chat" data-icon="✎">文本对话</a>`，
    排在 `#tts` 之后。
  - `<section id="page-text-chat">`：复用 `.imb-card` / `.imb-conv` /
    `.imb-composer` / `.imb-msg--in/out` 等现有样式；新增
    `.text-chat-thinking`（折叠块，初始折叠，有内容时自动展开）、
    `.text-chat-cursor`（打字机光标 blink 动画）、`.text-chat-stopped` /
    `.text-chat-error`。
  - JS 模块 `TextChat`（仿 `ImBridge` 模式）：
    - `init()` 绑定 send / stop / reset / Cmd+Enter；
    - `refresh()` 拉 snapshot；
    - `ensureWs()` / `closeWs()` 指数退避重连（`1 << (wsRetry-1)` ms，
      上限 30s）；
    - `_onWsMessage(msg)` 分发 `snapshot` / `state` / `user_echo` /
      `thinking` / `answer` / `done` / `stopped` / `error` 八种帧；
    - `pending: Map<request_id, ...>` 维护每轮推理的"用户气泡 →
      assistant 气泡"三段渲染状态；`_finalizeBubble` 优先用 done 帧的完整
      thinking/answer 覆盖流式 buffer（一次性结果也能完整渲染）；
    - `_scrollToBottom()` 仅在用户靠近底部时自动滚动（避免抢焦点）。
  - `loadPageData("text-chat")` 调 `TextChat.init() + refresh() + ensureWs()`；
    `showPage()` 离开时调 `TextChat.closeWs()`。

* **资源预算** —— 由所选模型的 ``manifest.json`` 声明
  （``memory_estimate_mb``）。多用户并发首版不支持 —— worker 串行。
  注意 `swcap_text_chat` 现在**只有 qwen3.5-vl-4b 一个 provider**（原
  qwen3-4b 专用文本 worker 已删除，权重不在目标板上，且与 qwen3.5-vl-4b
  争同一个 capability）。因此对话与视频描述会落在同一个进程 —— 两者
  都要用时注意显存叠加，不能再像以前那样分开加载。

* **限制 / 已知问题**：
  - stop 为**软停**：`session.stop()` 把取消标志发到旁路 topic，
    `_backend.run` 同步阻塞跑完才返回，worker 回报 stopped、主进程丢弃
    本轮。rknn3 没有协作式取消，跨线程硬中断（C 库 FINISH state=3）
    尚未打通，真机验证后再加。
  - `history` 全量每次 RPC 传输（deque 200 条上限）：同机 SHM 无碍；跨机
    UDP 传输受 FastDDS 单包大小限制，长对话跨机场景需评估。
  - thinking 不写历史 —— 用户"接着问"时下一轮 prompt 看不到上轮思考；
    想要延续 thinking 行为需把 `thinking` 也 push 到 history 的 assistant
    content 字段，但代价是污染 KV 且长对话下 token 增长更快。
  - 重生成按钮首版不做（plan §十三已确认）。
  - 真机首次初始化 ~90s（按经验值，需真机 page-cache-dropped 实测校准）。

## When you change things, also update

| You changed…                          | Then update…                                              |
| ------------------------------------- | --------------------------------------------------------- |
| Service version                       | `app/__init__.py` `__version__` (single source of truth)  |
| Default bind host / port              | `run.py` `main()`                                        |
| Public SDK dependency                 | Re-run the two `pip install -e` lines above              |
| Anything that needs a new endpoint    | Add a route inside `create_app()` in `app/main.py`        |
| Added/removed a model preset          | `MODEL_PRESETS` in `app/core/compute/registry.py` + `register` the new capability in the same table; update `app/static/index.html.template` and `app/static/core/framework.js (前端核心) + app/static/apps/<id>/app.js (各 app)` only if the UI needs new model cards |
| `ComputeRegistry` config schema       | `ComputeConfigPayload` in `app/system/api/compute.py` + `MODEL_PRESETS` keys; reboot-flush config (`PUT /api/compute/config`) carries Pydantic validation |
| Schema hint prompts (`VLMDoc.text`/`level`, `TargetDoc.desc`) | `app.prompts.<app_key>.<schema_cls>.<field>` in `config.jsonc`; UI under 系统配置 → App 提示词模板 section. `TargetDoc.x1/y1/x2/y2` stay hardcoded in `_build_target_doc_class` (`app/core/llm/detector_base.py`) — protocol-level 0–1000 归一化坐标格式说明，刻意不开 UI 编辑 |
| Built-in `@Callable` 工具（`VideoSearch` / `TtsAlert` 等）注册点 | 集中在 `app/core/tools/_builtins.py::register_builtin_tools()`。**主进程**和**compute_worker 子进程**各自在启动时调一次（主进程在 `app/system/__init__.py:register_all()` 与卡片 `register_api()`；worker 在 `app/core/compute/workers/_base.py:run_worker()` 末尾 `hub.start()` 之后）。**不能只放在主进程** — worker 端 `_resolve_tool_classes()` / `parse_classes` 都靠本地 registry 查类，缺类就静默 `WARNING unknown tool name ... skipping`，prompt 缺工具 schema，模型就会"假装在调用"工具(hallucinate)。详见下方"工具注册双进程陷阱"。 |
## Tools (音视频检测页工具)

`app/core/tools/` 装的是给音视频描述页 (`WdnaiDetector` 走
`VLMDoc` schema 时) 用的 wdnai ``@Callable`` 工具集合，**不**进入
视频监控 (16 路) — 那条通路有自己的 surveillance manager，工具
语义不同，这一轮不动。

* **唯一保留的内置工具** — ``tts_alert``：语音提醒，模型给出
  ``text`` / ``language`` / ``ref_speaker``，``TtsSession.start``
  触发本机播放。能力门控
  ``requires_capability="swcap_audio_tts"``。
  工具注册中心 ``app/core/tools/registry.py`` 中
  ``get_tool_registry()`` 启动时只挂载 ``tts_alert`` 一条。

* **页面归属**：
  - ``tts_alert`` 暴露面：**音视频描述页**（``ALLOWED_TOOLS =
    ("tts_alert",)`` 在 ``app/apps/video_detection/api.py``）+ **chat 链路**
    （web 文本对话 / 微信桥接，经 capability 过滤后自动纳入，
    见下方 "Chat 工具调用 (chat-agent)" 章节）。
  - ``video_search`` 暴露面：仅 **chat 链路**（微信视频检索场景，
    见 "微信视频检索" 章节）；音视频描述页不使用。
  - 算力配置页 RK3588 列只读展示全部工具的状态（当前有
    ``tts_alert`` / ``video_search`` 两条）。

* **历史下线 — ``target_detection`` 工具**：原本是给 **视频目标
  检测页** 用的 ``@Callable``，模型给出 0–1000 归一化 bbox + ROI
  描述，后端裁切 + JPEG 编码 + base64 内嵌。本版本已**完全下线**：
  - 工具文件 ``app/core/tools/target_detection.py`` 已删除；
  - 注册表不再有 ``target_detection`` 项；
  - ``app/apps/video_target_detection/api.py`` 不再有
    ``ALLOWED_TOOLS`` 常量与 ``/video-target-detection/tools``
    端点（视频目标检测页没有"工具集合"概念）；
  - 替代实现见下方 "TargetDoc 结构化输出"段。

* **能力门控（双层防护）**：
  - UI：``GET /api/video-detection/tools`` 列表里每条带
    ``available`` / ``reason``；不可用时 checkbox 置灰、显示
    原因。
  - 后端：``WdnaiDetector.start()`` 校验每个 ``enabled_tools``
    的 capability (``get_registry().get_session(spec.requires_capability)``)，
    未启用抛 503。

* **会话级，不持久化**：用户在检测页勾选的 tools 集合只
  存在内存里 (``WdnaiDetector._enabled_tools``)；重启 / 关闭页
  面 / 停止检测都清空，不写 ``config.jsonc``。``tts_alert`` 触发
  的副作用（已播放 / 失败等）只在 ``DetectorEvent`` deque 里活，
  刷新页面 / 重启就丢；不写盘、不另开缓存。

* **单帧多轮工具调用（agent-loop）**：详见上文 detector 段。
  配置入口 ``config.jsonc["compute"]["tool_call_max_iters"``（默认
  5，``0`` = 无限，缺失 / 负值 / 类型错 → 回退默认 5）；任何路径
  都被 ``WdnaiDetector._ABSOLUTE_MAX_ITERS = 200`` 硬上限兜底。
  工具调用 + 返回按 prose 格式写进下一轮 prompt（不是 JSON，避免
  模型复读），由 detector 内部维护。视频目标检测页无工具，循环
  天然只跑一轮就跳出。

### 工具注册双进程陷阱（v0.4 重构回归）

``app.core.tools.registry`` 是**进程级**单例（`_registry` 模块变量）。
v0.4 重构后两个内置工具的注册点散在不同地方：

* ``app/system/__init__.py:register_all()`` → 注册 ``VideoSearch``
  （im-bridge 拥有）
* ``app/apps/video_detection/__init__.py:register_api()`` → 注册
  ``tts_alert`` （音视频描述页拥有）

这两处只在**主进程** uvicorn 里跑。

**这段历史在 v0.16 已成为结构性不可能**：工具 class 的查表、schema
注入 prompt、模型 JSON 解析，**全部发生在主进程**
（``chat_agent._tool_classes`` → ``respond(tools=[...])``）。wire 上只有
拼好的 prompt，worker 既看不到工具名，也就无从"查不到类"。

**故障链路** (2026-09 真机实测复现，v0.16 前的形态) —— 留档说明为什么
"worker 需要自己注册工具"这件事曾经成立：
1. 主进程 ``resolve_available_tool_names()`` 返 ``["VideoSearch"]``
2. proxy 端 ``RpcSessionProxy.chat_run(..., tools=['VideoSearch'])`` 发到 worker
3. worker 端 ``_resolve_tool_classes(['VideoSearch'])`` 在自己**空**的
   registry 里查不到 → ``LOG.warning("unknown tool name ... — skipping")``
4. ``tool_specs = None`` → prompt 完全没有工具 schema（实测从正常的
   ~1100 字符缩到 192 字符）
5. 模型看不到 ``VideoSearch`` 描述，按"普通问答"作答 → 输出
   ``<think>...我将调用VideoSearch工具...</think>`` + 一段假想的
   "模拟调用"，**完全不产生** ``<tool_call>`` 标签
6. 用户最终看到 "我将模拟调用工具并提供答案" 的假回答

**当时修法**（``app/core/tools/_builtins.py::register_builtin_tools()``）：
集中所有内置工具注册到单一函数，主进程 + worker 各自启动时调一次。
新增内置工具在 ``_REGISTRARS`` 追加 ``(label, register_fn)`` 一行。

**现在的调用点**：
* 主进程 ``app/system/__init__.py:register_all()``（VideoSearch）
* 主进程 ``app/apps/video_detection/__init__.py:register_api()``（tts_alert）
* worker ``_base.py:run_worker()`` 仍调一次 —— **只为让两个进程的
  registry 保持一致**（如 im_bridge 直接问 worker capability 可用性），
  worker 推理链路本身已不依赖它。

## Chat 工具调用 (chat-agent)

web 文本对话与微信桥接共用 ``run_chat_with_tools()`
（``app/core/chat/agent.py``）：模型发起 tool_call 后在**主进程**
实例化并执行 @Callable，wrap-up / 回喂继续推理，直到给出自然语言
回答或达到循环上限。done 帧带最终 ``tool_call`` / ``tool_call_args``
+ ``steps[]``（每次执行的 args/outcome/error 明细）；
``TextChat`` 前端在气泡 meta 渲染 ``🔧 已调用 <工具名>`` 徽章
（`.text-chat-tool-badge`）。

* **为什么循环放主进程**：工具副作用依赖主进程资源（TtsSession /
  registry / 未来设备控制等）；compute_worker 是独立子进程，只能做
  schema 注入与输出解析（见 Text chat 章节 worker 端描述），执行
  必须回到主进程。
* **循环分流**：
  - side-effect 型（``__call__`` 返回 dict 带 ``consume_iteration:
    True``，如 tts_alert）→ 成功后跑一轮 wrap-up（``tools=[]``，
    输入"请一句话向用户确认"）生成自然语言确认语 → 出口；
    wrap-up RPC 失败回退固定文案。
  - 结果型（无该标志）→ 结果 JSON 转 prose transcript 作为下一轮
    `current_text` 继续推理，可链式再次 tool_call。
  - 执行失败 / 抛错 → **失败 transcript 回喂**（与结果型同构）：
    `[系统] 工具 X 调用失败：<原因>。请勿提及技术细节，自然语言告知
    用户暂时无法完成` → 模型生成解释后出口；连续失败由 max_iters
    兜底收敛。无特殊分支、无固定文案出口。失败明细写入 steps[].error。
* **capability 前置校验**：``_build_instance`` 对 ``requires_
  capability`` 未加载的工具直接抛 ``ToolExecutionError`` —— 保证
  "已注册但不可用的工具被调用必然返回错误"，未来新工具无需各自在
  ``__call__`` 里重复检查（tts_alert 内部仍保留自己的检查）。
* **history 写回**：调用方只写首条 user 条目，然后 extend agent 返回的
  `history_append` —— 含**自然语言** assistant 占位
  （如"（我已通过语音提醒完成操作：「请喝水」。）"，取 registry 的
  `spec.name` 中文名/英文名 + 结果摘要；语言按本轮用户输入 CJK 占比
  自适应中英）、user 系统指令条目、最终 assistant 回答。占位刻意不
  带 class 名 / JSON 参数等机器痕迹 —— 卸载工具模型后模型凭历史记忆
  复述出的也是无害人话，而非 `[已调用工具 X(参数)]` 机器腔。
  dict 上仍保留 `tool_call` / `tool_call_args` 字段供程序追溯。
  wrap-up / 失败回喂指令统一用展示名且要求模型"语言与用户当前对话
  保持一致"（英文用户场景不再强制中文回复）。
* **max_iters**：复用 ``compute.tool_call_max_iters``，chat 场景默认
  3（detector 默认 5），0 = 无限、200 硬上限兜底。
* **capability 过滤**：``resolve_available_tool_names()``
  （`tools/registry.py`）——``requires_capability`` 对应模型未加载的
  工具直接从 schema 排除，模型看不到就调不到。text_chat /
  wechat_llm 的 `_resolve_tool_names` 均委托它。
* **worker 端解析与注入解耦**：schema 注入按 capability 过滤后的
  `tool_names`；tool_call 解析则**始终按 registry 全量** —— 工具模型
  卸载后 prompt 无该 schema，但模型凭历史记忆自发输出的调用仍被
  识别并返回主进程执行（capability 校验必然抛错 → 失败回喂自然告知）。
  完全编造的工具名 dispatch 校验失败，当纯文本。
* **新增一个工具的三步流程（零框架改动）**：
  1. ``app/core/tools/<tool>.py`` 定义 ``@WdnaiCallable`` 类 +
     ``__call__(self, frame_bgr, ctx)``（纯文本场景 frame 传 None）；
  2. ``get_tool_registry()`` 里 ``register(...)`` 一行（需 NPU 算力的
     标注 ``requires_capability``）;
  3. 完成 —— schema 注入 / 解析 / capability 过滤 / chat-agent 循环 /
     detector 页勾选列表全部自动生效。worker 端无需任何改动
     （`_TOOL_TABLE` 手工映射表已删除，改枚举 registry）。
* **已知边界**：TtsAlert busy 时返回 silent skip dict（不带
  consume_iteration）→ 会走结果型回喂分支，模型将向用户转述
  "因繁忙未播"；语义上更理想是静默终止 + wrap-up，真机接入 TTS
  后按体验决定是否调整（`_execute_tool` 后加 silent 判定约 3 行）。
* **工具模型卸载后的"历史诱导调用"**：工具调用成功会在 history 留下
  `[已调用工具 X(...)]` 语义化占位；对应模型（如 qwen3-tts）被卸载后，
  新一轮 prompt 无该 schema —— **绝不会误执行**（capability 前置
  校验必然抛错）；模型可能凭历史记忆继续发起调用，走标准失败回喂
  路径：`[系统] 工具 X 调用失败：功能未启用…` → 模型自然语言告知用户，
  与其它运行时失败同构，无特殊分支。

## TargetDoc 结构化输出 (视频目标检测页)

取代旧的 ``target_detection`` ``@Callable`` 工具 —— 模型在生成
结构化输出时**直接**给出 ROI 坐标 + 描述文本，不需要再跑工具
链路。

* **Schema 定义** (``app/core/llm/detector_base.py``)：
  ```python
  @Generable
  class TargetDoc(BaseModel):
      x1: float   # 0–1000 归一化
      y1: float   # 0–1000 归一化
      x2: float   # 0–1000 归一化
      y2: float   # 0–1000 归一化
      desc: str   # 对 ROI 框出内容的描述
  ```
  由 ``_get_target_doc_cls()`` 懒加载（首次 start() 时 build 一
  次），存到模块级 ``_TARGET_DOC_CLS`` 缓存。同 ``VLMDoc`` 一样
  继承 ``pydantic.BaseModel`` 而非 ``LMOutput``，避免
  ``attachment: List[Attachment]`` 字段污染 ``model_json_schema``。

* **Detector 实例绑定 schema** — ``WdnaiDetector.__init__`` 接
  ``generating_cls`` 参数；工厂函数按页面分别绑定：
  - ``get_detector()``         → ``generating_cls`` 留空 → ``VLMDoc``
  - ``get_target_detector()``  → ``generating_cls=_get_target_doc_cls``
    → ``TargetDoc``

* **推理分支** (``WdnaiDetector._infer_sync``) — 跑的多轮工具循
  环不变，但 ``generating=`` 按实例配置；拿到结果后三路分发：
  1. ``@Callable`` 工具实例 → ``_run_tool_side_effect``
     （当前只有 ``tts_alert``）；
  2. ``TargetDoc`` 实例      → ``_build_target_event``
     （视频目标检测页走这条）；
  3. 其他（``VLMDoc`` 等）  → ``text`` / ``level`` 兜底分支
     （音视频描述页走这条）。

* **TargetDoc → DetectorEvent 构造**
  (``WdnaiDetector._build_target_event``)：
  - 从 ``x1, y1, x2, y2`` 拼 ``bbox: List[float]``；
  - ``desc`` 过 sanitize（防 chatml token / 控制字符 / 半截 JSON
    漏出）→ 复制到事件 ``text`` 字段（沿用历史展示路径）；
  - 按 bbox 裁原图 + JPEG 编码 (quality 85) + base64 内嵌到
    ``crop_b64``，事件含 ``crop_w`` / ``crop_h``；
  - ``tool_call='target_detection'`` 字段仍然写入事件 JSON —— 作
    为"TargetDoc 事件"的判别字段，前端
    (``ev.tool_call === "target_detection"``) 用来定位带 bbox /
    crop 的目标检测事件。语义上不再是 "工具被调用"，而是
    "TargetDoc schema 产生了事件"；与 ``tts_alert`` 的真实工具
    调用路径区分。
  - 裁切为空时 ``crop_b64=""``，事件仍写出，``level='error'``。

* **bbox 渲染**：后端把 bbox 通过 ``DetectorEvent.bbox`` 字段推到
  WS（事件类型仍是 ``type:"event"``，仅追加字段）。前端**不**
  在画布上绘制矩形框 —— bbox 只在事件历史列表里记录。浮动窗 /
  历史缩略图仍展示裁切图本身。
  ``target_detection`` 在浮动窗 + 历史缩略图里展示裁剪图本身，浮动窗
  5 秒后淡出。
* **工具列表已并入算力配置页 RK3588 列**：原 ``#page-tools`` 页面与
  ``GET /api/tools`` 端点都已移除；工具元数据现通过
  ``GET /api/compute`` snapshot 的 ``tools`` 字段返回（懒导入
  ``app.core.tools``，避免在 compute 模块加载时触发 wdnai 装饰器）。
  RK3588 列只读展示：默认全部"已启用"badge，依赖 capability
  未启用时切到"不可用"红色 badge；该列不允许手动 toggle 工具，
  仅作为状态展示。音视频描述页 / 视频目标检测页各自的 per-detector
  工具勾选（``/api/video-detection/tools`` /
  ``/api/video-target-detection/tools``）不受影响。


## LLMSession init 轻量预检（v0.5.0 起的优化）

`ComputeRegistry._run_one` 启动一条实例的流程：

1. **轻量预检** (`_probe_session_setup`，<1s)：
   * model_id 在 preset 白名单
   * preset 工厂能正常构造 `ModelSpec`（不调 rknn3）
    * **模型文件**按 `spec.kind` 分支校验（v0.5.x 起）：
      - `kind="llm"`：`rknn_path` + `weight_path` + `embed_path` + `tokenizer_path`
      - `kind="vlm"`：`rknn_llm_path` + `rknn_vision_path` + `embed_path` +
        `tokenizer_path`（`weight_llm_path` / `weight_vision_path` 由
        `.1828.rknn` 路径自动 derive，probe 不单独校验）
      - `kind="grounding"`：`rknn_llm_path` + `rknn_vision_path` +
        `embed_path` + `tokenizer_path`（与 `kind="vlm"` 同布局，
        LocateAnything backend `wdnai.backends.locate_anything.LocateAnythingBackend`
        走 RKNN 视觉 encoder + RKNN LLM decoder；输出 0–1000 归一化 bbox + 文本）
        — `MODEL_PRESETS["locate-anything"]` memory_estimate_mb=5500、
        capability_types=["swcap_grounding"]、`worker_name="ground"`（alias 6 字符，
        在 TASK_COMM_LEN=16 的 8 字符硬限内）。
 - `kind="tts"`：`tts_model_dir` 目录存在即可；具体
        `.1828.rknn` / `.weight` / `.fp16.bin` 清单交给 backend 真实 init
        阶段报更精确错误
    * `RKNN3Lite().get_devices_id()` 枚举得到 ≥ `npu_index + 1` 张卡
2. **父进程完整 init** (`LLMSession(model=spec, npu=npu_index)`，~55s)：
   * `try/except` 包住；非 std::terminate 错误都进 `inst.error`
3. **持久化** + LOG 显示 `probe=Xs init=Ys total=Zs`

**历史**：v0.5.0 之前是先在**子进程**里做完整 init 当探针（55s），
父进程再完整 init 一次（55s）—— 总耗时 ~110s，是 demo `summarize_text.py`
（55s）的两倍。优化后实测 56s（probe 1s + init 55s），与 demo 一致。

**权衡**：失去"rknn3 native abort 子进程隔离"保护 —— 但 AGENTS.md
里记载的 `std::terminate()` 现象**仅在非 RK 开发板 / 算力配置错的板
子**上见过，真机 RK3588 极少发生。旧版 `_init_session_subprocess`
完整函数**保留**在 `app/core/compute/registry.py:756+`（当前不
调用），将来真机若出现 abort，可以把 `_run_one` 改回原路径（恢复
双 init）或加 fallback 策略。

## Im-bridge page (应用对接 — 微信 iLink)

`#page-im-bridge` 拆成两块：左侧登录面板 + 右侧调试收发对话
（user_id 下拉 + 文本框 + 消息列表）。底层走 [`wechatbot-sdk` 0.3.0](https://www.wechatbot.dev/zh/python)，
但调用入口是 `app/system/im_bridge/bot.py` 里本地 `Wechat` 类（v0.5+
从 `wdndds.Wechat` 内联过来）的**同步门面**（自带 daemon-thread
asyncio loop），不是直接 `await` SDK。`wechatbot-sdk` 是 wdnwebui
的**直接**依赖（`requirements.txt`），不再是 wdndds 的 transitive
依赖 —— wdndds 0.4 已移除 `Wechat` 模块。

* **服务** (`app/system/im_bridge/service_wechat.py` + `app/system/im_bridge/bot.py`)：
  - `Wechat` 类（`app/system/im_bridge/bot.py`）—— 同步门面，持有
    `wechatbot.WeChatBot` + 私有 asyncio loop + daemon thread。
    暴露与原 `wdndds.Wechat` 一致的 API（`start_login` / `cancel_login` /
    `logout` / `send_text` / `register_message_handler` /
    `login_status` / `recent_messages` / `known_users` /
    `check_runtime`）。
  - 单例 `_WechatManager`（`app/system/im_bridge/service_wechat.py`，lazy，
    ``get_wechat()``），内部持一个 `wechat_bot.Wechat` 实例。
  - 后台线程 1Hz 轮询 `login_status()`，状态变化时 push 给 WS 订阅者；
    新入站消息通过 `register_message_handler` 立即触发推送，不等下一轮。
  - 内存里维护最近 200 条消息（in/out 各保留）+ 已知 user_id 集合。
  - **不自动 start()** —— 必须由 UI 点 登录 才进入 long-poll。凭证存在
    也只存不启动，决策与 `ComputeRegistry` "boot 不自动加载" 一致。
  - ``close_wechat()`` 用于 app shutdown；如果 manager 从未被创建过
    （用户从来没进过 im-bridge 页面）则是 no-op，避免空跑 daemon loop。

* **API** (`app/system/api/im_bridge.py`)：

  | Method | Path | 用途 |
  |---|---|---|
  | `GET`  | `/api/wechat`              | snapshot：state + qr_url + recent + known_users |
  | `GET`  | `/api/wechat/users`        | 已知 user_id 列表 |
  | `GET`  | `/api/wechat/runtime`      | SDK 版本 + available 标志（前端 "SDK" 行展示） |
  | `POST` | `/api/wechat/login` `{force?: bool}` | 触发 QR 登录 |
  | `POST` | `/api/wechat/cancel-login` | 取消正在进行的 QR 流程 |
  | `POST` | `/api/wechat/logout`       | 删凭证 + 停 bot |
  | `POST` | `/api/wechat/send` `{user_id, text}` | 发文本；`NoContextError` → 400 |
  | `WS`   | `/api/wechat/ws`           | 推 `snapshot` / `state` (diff) / `message` 帧 |

  - send 端点对 `wechatbot.NoContextError` 返回 400 并附中文提示（"该用户
    尚未向你发送过消息，context_token 未建立"）；其他错误 502。
  - WS 握手后**立刻**推一帧 `{"type":"snapshot", "snapshot":...}` 让客户端
    无需额外 GET 也能 hydrate。

* **前端** (`app/static/core/framework.js (前端核心) + app/static/apps/<id>/app.js (各 app)` `ImBridge` 模块 + `#page-im-bridge`):
  - 左侧：登录按钮 / 取消 / 退出 + 凭证路径 + user_id + long-poll 状态
    + SDK 版本 + QR `<svg>` 渲染（vendored Kazuhiko Arase
    `qrcode-generator` 1.4.4，MIT，文件 `app/static/js/qrcode.js`，**不**
    引外部 CDN 也不增加 Python 依赖）。
  - 右侧：user_id `<datalist>` 下拉 + 文本框 + 发送按钮（Cmd/Ctrl+Enter
    触发）；消息列表按 user_id 过滤，in/out 分色（`imb-msg--in` /
    `imb-msg--out`）。
  - WS 自动重连 + 指数退避（与 TTS / VD 一致）。
  - 状态徽章按 `login_state` 上色：未登录（灰）/ 等待扫码（黄）/ 已扫码
    （蓝）/ 已登录（绿）/ 过期 / 错误（红）。

* **消息流**：
  ```text
  [微信用户扫码]
      ↓ SDK 推送 QR URL → on_qr_url
  wechat_bot.Wechat._qr_url set, login_state=awaiting_scan
      ↓ 1Hz 轮询 + 回调即推
  _WechatManager._push_state_diff → WS broadcast {type:"state", state:{login_state:..., qr_url:...}}
      ↓ 前端 _renderQr() 用 qrcode-generator 把 URL 渲染成 <svg>
  [用户在手机点确认]
      ↓ SDK 回调 on_scanned → on_confirmed → bot.start() 自动起 long-poll
  login_state=confirmed + started=True
      ↓ [用户A 发消息]
  wechatbot bot._dispatch(msg) → wechat_bot._on_incoming → _WechatManager._handle_incoming
      ↓ _push_state_diff → WS broadcast {type:"message", message:{direction:"in", ...}}
      ↓ 前端 _recordMessage + _renderChat → 追加到列表
  [用户点发送]
      ↓ POST /api/wechat/send
  wechat_bot.Wechat.send_text → wechatbot.bot.send(user_id, text)
      ↓ SDK _send_content + _send_text
      ↓ wechat_bot._record_outbound → _push_state_diff → WS broadcast {type:"message", direction:"out"}
  ```

* **凭证路径** = `~/.wechatbot/credentials.json`（`wechat_bot.DEFAULT_CRED_PATH`），
  也可设 `WDNDDS_WECHAT_CRED_PATH` 环境变量覆盖（向后兼容命名）。
  这是 SDK 默认路径，多 wdnwebui 实例不会冲突。

* **资源预算** —— `wechat_bot.Wechat` 单实例长期驻留一个 daemon thread + 一个
  aiohttp 连接池；未登录时 SDK 不会建任何连接（`bot.login()` 之前
  `_api` 只在 `poll_qr_status` 时拉 HTTP）。登录失败 / QR 过期 → SDK
  内部走 `MAX_QR_REFRESH_COUNT=3` 轮后抛 `AuthError`，wrapper 把
  `login_state` 切到 `error`，UI 看到红色徽章 + 点 登录 重试。

## 微信对接文字对话模型（LLM Bridge）

im-bridge 页「调试收发」卡片顶部新增一个「对接文字对话模型」开关。开启后
微信用户发来的**文字**消息自动进入桥接队列 → 调 `swcap_text_chat`
（qwen3.5-vl-4b）推理 → 把回答（只回 answer，不含 thinking）通过
`Wechat.send_text` 发回给该微信用户，实现微信上的多轮文本对话。

* **Service** (`app/system/im_bridge/llm.py`)：
  - `_LlmBridge` 单例（`get_llm_bridge()`），由 `_WechatManager` 持有并
    在 `_handle_incoming` 里转发 `handle_incoming(msg)`。
  - **开关内存态**：`_enabled` 不落盘（重启即关）。`available()` 通过
    `get_registry().get_session("swcap_text_chat")` 判断对话模型是否
    已加载；未加载时 `set_enabled(True)` 抛 `RuntimeError`（前端置灰 +
    后端 409 双层防护）。
  - **per-user 上下文隔离**：`histories: dict[user_id, deque(maxlen=200)]`
    各自独立，与 web 文本对话的全局历史完全无关；服务重启即清空。
  - **串行保证**：桥接 worker 线程串行消费 `queue.Queue`；`respond` 用
    `chat_serial.get_chat_run_lock()`（进程级 `RLock`）与 text_chat worker
    互斥 —— 同一时刻只有一个会话发往 worker，避免并发停止冲突。
    `text_chat._run_one` 同样包这把锁。
  - **裁剪与 ChatML 都在主进程**：与 web 侧共用 `run_chat_with_tools()`
    （内含 `chat_agent._trim` 的真实 tokenizer 计数 + wdnai 的
    `build_chatml_prompt`）；主进程完成工具执行与回喂循环（成功 wrap-up /
    失败回喂自然告知）后，把最终 answer 直接发微信。
  - **发送复用 `wechat_bot`**：回复走 `Wechat.send_text`，自动出现在
    "调试收发"消息列表（out 方向），与手动发送共用同一发送通道、线程安全。

* **与调试收发的关系**：两者数据流正交、可共存。用户消息（in）和 LLM
  自动回复（out）都显示在调试收发列表。**开关开启时手动发送被禁止**：
  - 前端：`imb-send-btn` 固定置灰（输入框仍可输入）；
  - 后端：`POST /api/wechat/send` 在 `llm_bridge.enabled == true` 时返回
    409「对接文字对话模型已开启，手动发送已禁用」。
  语义：开启时消息流完全交给「用户微信消息 → LLM → 自动回复」闭环，避免
  手动消息未进模型上下文造成上下文错乱。

* **API**（`app/system/api/im_bridge.py`）：
  - `POST /api/wechat/llm-bridge` `{enabled}` —— 开/关桥接；未加载 LLM 时
    开启 → 409；返回 `{ok, snapshot, llm_bridge}`。
  - `GET /api/wechat` snapshot 增加 `llm_bridge: {enabled, available,
    busy, active_sessions}`（`available` 供前端置灰）。

* **前端**（`app/static/index.html.template` + `app.js` `ImBridge`）：
  - 调试收发卡片顶部一行：checkbox + 提示。`available=false` → checkbox
    disabled + 红字提示「文本对话模型未启用，请在算力配置页加载
    [VLLM]视觉文字多模态v3.5」；
    `enabled` + `busy` → 「已对接，生成中…」。
  - `_toggleLlmBridge` 调 `POST /api/wechat/llm-bridge`；409 时 revert +
    提示。`send()` 顶部也兜底拦截（按钮已置灰，防 Cmd+Enter 绕过）。

* **限制**：LLM 单实例串行，多人同时发需排队（每条推理约 40-60s）；只发
  answer 不发 thinking；非文字消息（图/音/文件）回复「当前仅支持文字对话」。
  工具调用经 chat-agent 循环（见 "Chat 工具调用 (chat-agent)" 章节），
  微信用户同样可用。视频消息是例外 —— 分流到微信视频管理器（见下节）。


## 微信视频检索（wechat-video-manager + VideoSearch）

用户通过微信发送视频并提问（如"视频里有消费员吗"），LLM 调用
``VideoSearch`` 工具对该视频做语义检索并结合命中时间段回答。

* **架构分层**：提取不走 LLM 工具链路 —— "收到视频 → 提取"是确定性
  协议行为。``wechat_llm.handle_incoming`` 对 video 消息毫秒级分流到
  ``wechat_video_manager.enqueue_video()``（纯内存入队，CDN 下载在
  后台线程，不阻塞回调）；LLM 只暴露同步查询型工具。
* **管理器**（`app/system/im_bridge/video_manager.py` 单例）：
  - **单槽注册表** `{user_id: VideoEntry}` —— 每用户仅保留最新 1 个
    视频，新视频到达即覆盖旧的（删落盘文件 + 释放旧索引）；串行队列
    中被覆盖的任务按 seq 判定自动跳过。
   - 管线：SDK CDN 下载（`Wechat.download_message()` 同步透传，120s
     上限）→ 落盘 `uploads/wechat_videos/<uid>/` → PyAV 探测 → 逐片
     **4s 片 / 1s 重叠** embedding（复用 VAS_SEARCH 底层函数 `_probe_video_meta`
     / `_read_chunk_frames` / `_write_frames_to_shm` / `_cosine_similarity`
     + `get_session("swcap_video_retrieval")`，SHM 帧传递 + timeout 重试
     与 UI 页同款）→ ready。
   - **md5 缓存命中短路**（下载落盘后、PyAV 探测前）：流式算本地
     md5 → `get_video_cache().lookup(md5, model_id)` 命中即把
     LanceDB 里的切片索引恢复进 entry（秒级 ready，跳过全部
     embedding），播报与正常分析完成**完全一致**（用户无感知）；
     随后删刚下载的文件。所有消息形态（video / file）统一走
     "下载 → 本地 md5 → 查缓存"；file 形态自带的服务端 md5 不使用
     （与实际下发文件存在极小概率不一致，本地复核是唯一真源）。
   - **进度感知**：推送侧节点播报固定文案（📥 收到 / ⏳ 开始分析
     N 段 / ✅ 完成 / ⚠️ 失败）；拉取侧工具返回实时 progress。不做周期
     百分比消息（个人号高频发消息有风控风险，且协议无编辑消息能力、
     无法做原地刷新的进度条）。
   - **全成功（failures==0 且段数满）**：分析结果写入 LanceDB 缓存
     （见下节）+ 删本地视频文件（检索只回时间段，文件无用途；
     200MB/个，板上磁盘稀缺）；**部分成功**：⚠️ 警告播报带失败数 +
     首个失败原因，让用户明确感知不完整；不入缓存、不删文件（重发
     可重试失败段）。播报格式对命中 / 全成功完全一致。
   - shutdown hook `close_wechat_video_manager()` 在 main.py 注册。
* **LanceDB 分析结果缓存**（`app/system/im_bridge/video_cache.py`，
  v0.12）：key = 视频文件本地 md5；value = 切片 embedding 索引，
  **一行一片**（表 `wechat_video_index`：md5 / chunk_idx / 时间窗 /
  `vec` FixedSizeList(2048,float32) / filename / duration / 切片参数 /
  embedding_model / analyzed_at）。数据目录
  `app/uploads/lancedb/`（uploads/ 已 gitignore）。要点：
  - **embedding_model 隔离**：每行记录产生向量的模型 id；模型换代后
    旧缓存自动失配（lookup 按 md5 + embedding_model 过滤查不到），
    不会拿旧向量硬算余弦。
  - **仅全成功的分析入库**；部分成功只驻留内存。save 前 delete 同
    md5 再 add（幂等覆盖）。
  - **LRU 淘汰**：按视频条数（distinct md5）计，超上限删
    `analyzed_at` 最旧条目；`config.compute.wechat_video`
    热读 `cache_max_entries`（默认 100，≈1MB / 10min 视频）与
    `cache_enabled`（默认 true；false 时旁路 → 全部退回无缓存行为）。
  - **故障兜底**：lookup/save 任何异常（lancedb 不可用 / 表损坏 /
    维度不匹配）只 LOG warning 降级 —— 缓存层任何异常都不阻塞
    分析管线；`get_video_cache()` 返回 None 表示旁路。
  - **切片参数进缓存键（v0.12.1）**：`lookup/save` 均以
    `(md5, embedding_model, chunk_seconds, chunk_overlap_seconds)`
    定位条目 —— UI 检索页的 chunk/overlap 是用户可选的
    （3-6s / 1-2s），不同参数的索引时间窗与帧数完全不同，混用会
    时间戳错位。微信管线传固定 4/1；VAS_SEARCH 传用户选值；同视频不同
    参数是**独立条目**（各自占 LRU 槽，命中互不干扰）。
* **VAS_SEARCH 页接入缓存（v0.12.1）**：视频内容检索页与微信管线共享同一
  张表 + 同一单例：
  - `VideoSearchService.start_extract` 参数校验通过后算
    md5（本地文件直接流式算，无需下载）→ `lookup(...)` 命中即
    还原 index（`video_path` 换绑为当前 source basename，帧接口
    走当前源）→ state 秒级 READY（广播 state，前端进度条直接
    满格）；不命中走原有 worker。
  - `_worker_loop` 收尾 `slices_embedded >= slices_total`（全成功
    语义与微信一致）→ `cache.save(...)`；部分成功不存（与微信
    决策一致，重跑可补全）。
   - **"清空缓存"按钮**（`#vcs-clear-cache`，缓存窗口 head）→
     `confirm()`（含 `cache/stats` 返回的条数提示）→
     `POST /api/video-analysis-search/search/cache/clear` → 幂等
     `{videos_cleared}`；`GET .../cache/stats` 返回
     `{enabled, max_entries, videos, rows}` 给确认框展示。**同时作废
     当前会话的内存索引**（端点随后调 `get_service().reset_session()`：
     停提取 worker + `_state→idle` + 清 `_index` / `_md5` / 切片计数 +
     广播 idle）—— 否则持久缓存虽删、前端 `vasStartSearch` 仍会看到
     `state=ready` 短路跳过 `start_extract`，表现为"进度条直接满格 +
     历史无新条目"（用户实测）。保留 `_source`，可重新点开始提取。
     **注意**：清空 LanceDB 是全表删除（含微信条目），会话 reset 只
     针对 VAS_SEARCH 单例（微信 `wechat_video_manager` 用自己的
     VideoEntry，不共用这个单例，不受影响）。
   - **"清空检索结果"按钮**（`#vcs-results-clear`）→
     `POST /search/query/clear` → `clear_query()`：清 `_query` / `_hits`
     **并把 `_state` 从 `ready` 复位为 `idle`（清零切片计数）**，
     `_index` 保留供重查。原因：进度条完全由快照 `state`/`slices_*`
     渲染，`ready` 会在任何一次快照重放（WS 重连 / 卡片 JS 重载 /
     切页返回）时把进度条刷回 100%（用户实测："清空后切页再切回进度条
     复活"）。复位 idle 后前端进度条走 `prog.hidden=true`（**直接隐藏**，
     非停在 0%）。前端 `vcsResetProgress()` 的 `_progressForceIdle`
     内存标志保留作即时反馈，且 `vasStartSearch` 的 `ready+同源` 短路
     额外加了 `!_progressForceIdle` 守卫（防旧 ready 快照刷回后误短路）。
   - **分析侧"清空"对称语义**（`#vas-clear`）→
     `POST /analysis/events/clear` → `VideoAnalysisSearchService.clear()`：
     清 `_events` / `_summary` **并在非运行态下把 `_state` 置 `idle` +
     清 `_run_id`**（`opening`/`running`/`summarizing` 时只清数据，兑现
     api docstring"运行中的分析不会被中断"），随后广播新快照。清
     `_run_id` 让前端 `vasUpdateButtons` 的 `doneRun`（依赖
     `VAS.snap.run_id`）自然为 null → 反查按钮正确置灰、不再依赖
     `VAS._clearedRun` 内存标志；进度条也因 `state=idle` 隐藏。否则与
     检索侧旧 bug 同型：清空后切页往返，后端 done 快照让进度条复活
     100% + 反查按钮变亮闪烁（用户实测）。WS handler 的
     `if new_run_id and ...`（`api.py`）对空 `run_id` 安全跳过。
  - **schema v0.12.2 加列 `source_path`**（入库时的本地视频路径）：
    微信路径固定存 `""` 占位（分析完删文件，无可取帧），VAS_SEARCH 传
    `self._source`；`_migrate_add_source_path` 用 lancedb 0.34
    `add_columns({"source_path": "''"})`（SQL-dict 形状，持久化）
    幂等补列，失败兜底 drop + 新 schema 重建（缓存可再生，旧条目
    frame 置灰不影响检索）。
* **VAS_SEARCH 勾选双路径（v0.12.2）**：缓存窗口让已分析视频可被直接勾选
  检索，无需重新上传/提取：
  - **窗口/数据**：`GET /api/video-analysis-search/cache/list`
    （`cache.list_videos()`：`[{md5, filename, source_path, chunk/
    overlap, slices, duration_s, analyzed_at}]`，按 analyzed_at 真
    倒序）渲染 `#vcs-cache-window`（max-height 滚动）—— 每行=
    (md5 × 切片参数) 三元键条目：checkbox + 文件名 + "Ns/Ms·N段" +
    行删除按钮（`DELETE .../cache/entry` 按行删，同 md5 其它参数档
    保留）。
  - **互斥**（前端 `vcsApplyMutualExclusion`）：路径 text 非空 → 勾
    选框全 disabled；勾选 ≥1 → `#vcs-source` / `#vcs-browse` /
    `#vcs-start` disabled。
  - **多选检索**：检索按钮在勾选 ≥1 时分叉
    `POST /query_multi {text, sim_threshold, top_k, min_results,
    entries[]}` —— **embed_text 只算一次**、逐条目 LanceDB lookup
    向量做余弦（`_score_hits` 与单视频 query 共用）；返回
    `groups[{filename, source_path, chunk params, hits, top_score,
    frame_enabled, miss}]`，前端 `vcsRenderMultiGroups` 按视频小节
    分组渲染（命中卡点击播放 → `vcsPlaySegment(idx, srcOverride)`
    用条目 source；`frame_enabled=false` 时卡片置灰"取帧失败"，
    点击提示）。**无状态批量语义** —— 不污染单例的 `_index` /
    `_source` / 状态机。
  - **宽松匹配全局化（v0.12.4）**：`query_multi` 改**两阶段**评分
    —— 阶段 1 每组严格检索（`min_results=0`，阈值过滤 + 组内
    top_k）累计全组合计达标数；阶段 2 仅当**合计零达标**且
    lenient 时，收集各组低于阈值候选**全局降序**取 top
    `min_results` 条按来源组落位（共享一份配额，标 below）——
    此前每组各自带 min_results 兜底（N 视频全无命中兜出 N×top_k
    条，"每视频各一份"与"一次总的"语义不符，用户决策改为全局
    判定：任一视频有达标命中则全部不兜底）。单视频 `query` 路径
    与微信 `VideoSearch` 工具的兜底不受影响（各自单组语义不变）。
  - **布局（v0.12.4）**：`#vcs-cache-window` 从"视频源"栏移出为
    独立 section（位于"操作"与"检索问题"之间），"清空缓存"按钮
    移入窗口 head（`.vcs-cache-actions` 右侧按钮组，警示 hover）；
    提取完成（state 首次 ready，`_cacheWinOnReady` 防抖块）自动
    清空 `#vcs-source` 路径文本框（仅成功清空，error/stopped 保留
    便于重试）并刷新缓存窗口 —— 引导检索走勾选路径。
  - **source_path 语义**：缓存条目存的入库时路径仅当**该文件仍在
    本机**才允许取帧/播放（`frame_enabled`，端点侧
    `os.path.isfile` 校验）—— 移走/删除的旧文件条目仍可纯文本
    检索（时间段 + 相似度）。
  - **渠道隔离（v0.12.3）**：`source_path` 双语义兼作渠道判别 ——
    微信管线固定 `""` 占位、web（VAS_SEARCH）恒存真实本地路径（均不随
    文件存亡漂移）。`cache/list`（web 窗口/勾选/行删除的数据源）
    调 `list_videos(channel="web")` → 只出 `source_path != ''` 条目；
    **微信条目不进 web 界面**。`lookup`/命中短路/LRU/`clear`（全清
    含微信条目，用户决策）不变 —— 跨入口命中复用保持（表共享、
    展示隔离）。同 md5 同参数两渠道互覆（save 幂等语义，检索等价）。
* **VideoSearch 工具**（`app/core/tools/video_search.py`，
  `requires_capability="swcap_video_retrieval"`）：**无调用时机限制**
  的四态结果型返回 —— `ok`（命中片段）/ `extracting`（附实时进度）/
  `downloading` / `no_video`，状态即合法业务结果而非错误，模型自由
  组织回复。真正的执行失败（会话缺失等）抛错走失败回喂。
  检索参数：query 内部截断 50 字；top_k=5（模型可调 1-10）；
  sim_threshold 固定 0.4 **不暴露给模型**，`min_results=3` 兜底启用
  —— 全低于阈值时返回最接近片段 + `all_below_threshold` / `hint`
  质量信号，由模型谨慎表述"未找到高度匹配的内容"。阈值不暴露的
  原因：模型对余弦分数无语义感知（盲调），且兜底已保证非空。
* **来源透传**：`ToolContext` 新增 `user_id` / `channel` 字段，
  `run_chat_with_tools()` 对应形参由 wechat_llm 传真实值（web 侧
  channel="web"、user_id 空 → 工具返回 no_video 引导走微信渠道）。
* **资源上限（可配置）**：`config.jsonc["compute"]["wechat_video"]`
  的 `max_size_mb`（默认 200）与 `max_duration_min`（默认 10），热读
  即时生效、非法值回退默认。三道校验：下载前按 SDK 元数据预检
  （video 形态 duration_ms / file 形态 size，互补覆盖）；落盘后大小
  复核；PyAV probe 后时长复核 —— 统一 raise → error 态 + ⚠️ 微信
  通知，超限文件不残留。
* **capability 预检（未启用整体不可用）**：`handle_incoming` 分流时
  先 `get_session("swcap_video_retrieval")` —— 未加载则**不入队**
  （加载后需重发视频），回固定文案「视频检索模型功能未被启用，需在
  算力配置页中加载启用后重新发送」，并向该用户 history 注入系统通知
  （此场景 VideoSearch 不在 schema 中，模型无工具可调，注入后才能
  准确解释原因而非含糊其辞；语言自适应）。manager `_process` 内部
  再取一次 session 作为最终防线（防预检后下载期间被卸载的 TOCTOU
  窗口），位置在 CDN 下载之前 —— 未启用时连流量都不浪费。
* **已知边界**：UI 页"视频内容检索"的单例服务与本管理器完全独立
  （互不复用索引）；真机 SDK CDN 下载稳定性需实际微信验证。
* **新视频抢占（协作式取消）**：分析进行中收到同用户的新视频时，
  `enqueue_video` 单槽覆盖即 `old.cancel.set()` + 删落盘文件；正在
  执行的旧管线在四处检查点静默退出（下载重试边界 / 下载完成后 /
  embedding 每片开头 / 终态写回前）—— 不发任何通知、不写终态，
  新视频随即开始分析。SDK 的 `download_message` 同步阻塞无法中途
  打断，最长滞后 = 单次 CDN 传输时长。


## FastDDS discovery — SIMPLE multicast (v0.6+)

v0.5 引入了 wdndds_daemon 走 SERVER/CLIENT 协议绕开 FastDDS 2.9 SPDP
multicast bug。v0.6 改成 **FastDDS SIMPLE discovery** 直接走组播
239.255.0.1:7400 —— 既然 wdnwebui 现已自带源码编译的 FastDDS 3.6.2
(v0.5 时代的 bug 不存在),daemon 这一层就成了纯运维负担,没有功能收益。

### 架构

```
        ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────────┐
        │  main    │  │ worker   │  │ worker   │  │  worker    │  (FastDDS
        │ (wdnwebui│  │ qwen3-vl │  │ qwen3-tts│  │ qwen3.5-vl │   SIMPLE)
        │  main)   │  │  -4b     │  │          │  │   -4b      │
        └────┬─────┘  └────┬─────┘  └────┬─────┘  └─────┬──────┘
             │              │             │             │
             └──────────────┴──── UDP multicast 239.255.0.1:7400 ───────
                                          │
                            (LAN: 任意 L2 节点互通)
```

* **零配置**：每台板子启 `wdnwebui` 即自动被同网段其它板子看到；
  不需要配 daemon 地址、不需要 systemd 单元、不需要端口管理。
* **数据平面不变**：仍走 cshim 的 SHM（同机）+ UDPv4（LAN）+ TCPv4（跨子网）
  builtin transports。
* **请求 / 回复主题**：`wdndds/rpc/req/<model_id>` +
  `wdndds/rpc/rep/<model_id>`，IDL 见 `wdndds/idl/compute.idl`。
* **participant 命名约定**：主进程 = `wdnwebui-main-<device_name>`，
  worker = `compute_worker-<device_name>-<model_id>`，
  comm = `worker:<alias>`（见 `MODEL_PRESETS[model_id]["worker_name"]`）。
  v0.8 起 `device_name` 缺省 fallback 到 `socket.gethostname()`，
  participant_name 永远非空。

### 跨子网 / 组播被屏蔽时的 fallback

消费级 WiFi（AP 隔离、访客网络）、Docker bridge、严格 VLAN 经常丢组播。
每个 wdnwebui 进程读 `WDNDDS_INITIAL_PEERS` 环境变量（逗号分隔的 IP
列表），由 cshim 的 `_install_default_dds_profile()` 写入
FastDDS XML profile 的 `initialPeersList`，作为 unicast fallback：

```bash
# 在每台板子设上对端 IP（v0.6 新增，无需 daemon）
WDNDDS_INITIAL_PEERS="192.168.1.10,192.168.1.11,192.168.1.12" \
    .venv/bin/python run.py
```

XML profile 路径：`$XDG_RUNTIME_DIR/wdndds_default_profile.xml`
（找不到时 fallback 到 `/tmp/`）。除 `initialPeersList` 外,profile 还
显式设了:
* `<discoveryProtocol>SIMPLE</discoveryProtocol>`
* `<initialAnnouncements><count>10</count><period>100ms</period></initialAnnouncements>`
  — 比 FastDDS 默认（5×100ms）更激进,冷启动发现更早收敛。
* `<leaseDuration_announcementperiod><sec>1</sec>…</leaseDuration_announcementperiod>`
  — 比 FastDDS 默认（3s）更短,周期发现滞后更小。
* 默认 `initialPeersList` 永远含 `127.0.0.1:7410` loopback,同机进程
  即使 multicast 坏掉也能互相发现。

### 调试 / 监控

* **「系统信息 → 在网设备」**：前端拉 `GET /api/system-monitor` 中的
  `discovery` 字段（`{protocol, multicast_group, own_guid, peer_count,
  peers:[{guid, name, discovered_at}], wdndds_initial_peers}`）。
  数据源是 `ComputeRegistry.list_peers()`,由 `_ensure_hub()` 启动时
  给 Hub 装的 `set_peer_listener()` 实时回调维护。
* **verify 脚本** (`scripts/verify_simple_discovery.py`)：
  跨机 / 同机纯 SIMPLE 端到端检查,带退出码（0=peer+RPC OK,
  1=peer OK 但 RPC 失败, 2=无 peer, 3=cshim 不可用）。
* **无 daemon HTTP API**：v0.5 时代 daemon 的 `/status` / `/peers`
  /start /stop 端点全部下线。

### 已知限制

* 跨机 cold-start 时延：从 SPDP 首次通告到 peer 可见 < 1s（实测本机
  0.20s / 同 LAN 跨机 0.20s）。lease announcement period = 1s 意味着
  peer 失联最迟 ~3s（lease 20s 之内）后被探测到。
* FastDDS 3.6 rrpc API 仍 stub（参见 Fast-DDS `DomainParticipantImpl.cpp`）：
  wdndds_cshim 仍走 raw `DataReader` / `DataWriter` 模拟 Request/Reply。
* **冷启动 NIC 时序坑（真机实测）**：systemd autostart 拉起的
  compute_worker 若早于网卡就绪启动，cshim 的 interfaceWhiteList pin
  会绑错 interface → 该 worker 进程活着但 DDS 数据面完全不可达
  （probe / bind 全 timeout，worker 日志零请求痕迹）。判别方法：
  手动 kill 后重新 spawn（NIC 已就绪）即恢复。现有防线：① 主进程
  `_ensure_hub` 的 `wait_for_network_ready` / socket fallback；
  ② `bind_instance._run_load` 前调 `_refresh_rpc_clients()` recreate
  handle（对 spawn→bind 的时序 race 兜底，destroy 拿 requester._lock
  防 probe 线程 UAF）。待办：worker systemd 单元加
  `After=network-online.target`。
* **get_session 本机优先（v0.8.30）**：`ComputeRegistry.get_session`
  同 capability 多实例 running 时按 `spawned` / `adopted_local` 优先
  于 `adopted`（LAN peer）选择。原因：视频类消费方
  （VAS_SEARCH / wechat_video_manager）用 `/dev/shm` path-list 传帧，
  **远端 worker 读不到本机 tmpfs** —— 命中远端实例会逐片
  `cv2.imread exists=False` read操作全灭；
  `rpc_session_proxy.embed_video` 的 path-list分支**没有**
  is_remote 守卫（ndarray 分支有 base64 fallback，
  path-list 分支跨机就是不可用）。无本机 running 时才退回
  LAN peer。每次调用打 `get_session(cap): LOCAL/REMOTE` 日志。
* **主进程 fd 防线（v0.8.30）**：`run.py main()` 开头
  `setrlimit(RLIMIT_NOFILE soft→hard)`。reboot 后手动 shell /
  systemd 拉起的主进程默认 soft=1024，而主进程长寿命持有 FastDDS
  endpoint、uvicorn 连接、journalctl WS 等 —— reload 的
  multiprocessing.spawn 子进程与 Popen 出的 compute_worker 都继承
  本 limit（worker main() 另有自己的 setrlimit 兜底，见
  "compute_worker.py" 段）。**本仓库无生产 systemd unit**
  （install_autostart.sh 未部署的主机 reboot 后 run.py 由手动
  shell 拉起），部署环境重启后务必确认主进程 `Max open files`
  为 524288。
* **shim 写端 fail-loud**（`_write_frames_to_shm`，v0.8.30）：每个
  帧文件写后立即 `os.stat` 校验（size>0），失败 raise
  `RuntimeError("/dev/shm 写入失败…/立即消失…")` → 走早前加的
  失败聚合 → 微信用户直接收到真因。此前"写端静默、worker imread
  None"的三层吞错即使修复后仍难以诊断"文件消失"类环境事故。
* **cshim Service 对象必须显式销毁**：`_probe_service` 曾只 destroy
  requester 留下 svc —— boot auto-adopt 期累积约 75 个 FastDDS
  endpoint 集合。已修复为双销毁；新增 probe/临时 requester 路径时
  注意同样问题。
* wdndds_daemon 完全删除；旧版 systemd unit
  `wdndds-daemon.service` 会因 unit 文件不存在报 `Unit not found`，
  `install_autostart.sh --uninstall` 会一并清掉。

## 帧判据（FrameJudge）与多路轮转的共存约定（v0.21）

`submit_frame` 的队列元组被**两个独立特性**先后占用过同一位：

| 特性 | 提交的信息 | 来源 |
|---|---|---|
| 帧判据 | `judge_score` —— 这一帧的画面变化分数 | `core/video/frame_judge.py` |
| 多路轮转 | `channel` —— 第几路 | `core/video/multi_session.py` |

两者都只是"这一帧的附加元数据"，语义正交，**必须各自占一位**。合并后
统一为 **6 元组** `(帧, 帧号, prompt, jpeg, judge_score, channel)`。

**不要再把第 5 位复用给别的东西** —— 曾经的冲突是两个未发布版本各自
把 `channel` / `judge_score` 塞进第 5 位，文本上不冲突（git 能自动合），
但**运行时静默串位**：单路页的 `channel=0` 会被当成 `judge_score=0.0`，
多路页的事件会全部挂到「第 0 路」。这类 bug 不报错、只表现为归属错乱。
新增元数据一律追加到末尾，并同步扩 `_worker_loop` 的解包分支。

`DetectorEvent.to_dict()` 同时带 `channel` 与 `judge_score` 两个键；
前端按 `channel` 显示「通道 N」、按 `judge_score` 显示变化分数，
两者互不覆盖。

### 历史缺口：vtd 多路页的判据控件曾经不生效（已补）

rebase 合入上游判据特性后，vtd 页的判据 UI 曾是**控件在、后端不接**：
`/api/video/judge` 改的是单例 `VideoSession` 的判据，而本页走
`MultiChannelSession`（`grep judge multi_session.py` 为空），配置写进去没人读。
当时留下的临时约定是「向导 `gscore` 虽然 `selected: true` 但不生效」。

**该缺口已由下一节「多路页的帧判据」补齐**：`MultiChannelSession` 现在
每路一个独立判据器，端点挂本卡，向导默认也一并改回 `manual`。
此处保留记录，是想记下「判据接线必须跟着解码路径走」这条教训 —— 
卡片换了会话实现，判据也得跟着换，光把 UI 接上不解决问题。

## 多路页的帧判据：每路一个独立 FrameJudge（v0.21）

`video-target-detection` 多路版给**每路摄像头配一个独立 `FrameJudge`**，
判据实现仍是唯一一份 `core/video/frame_judge.py`。

### 为什么必须每路一个

参照帧（`_last_luma`）的语义是「上一张**送去推理**的本路画面」。多路画面各不相同，
共用一个参照帧等于拿 A 路的图去比 B 路 —— 每帧都得高分，判据退化成「永远放行」。

### 判据挂在轮转里，不在解码循环里 —— 与单路最大的语义差别

单路 `VideoSession` 是 `observe()` **每帧**调用，判据自己决定送检。多路不能照抄：
轮转本身就是节流器（每路 ~2s 才被看一次），若判据也每帧跑，参照帧会跟着「每帧的
判定」推进而**不是**跟着送检推进，「参照帧 = 上一次送检的帧」这条不变量就破了，
而且算出来的分数绝大多数没人消费。

所以 `_rr_loop` 的顺序是：

1. `ch.snapshot_gray_for_judge()` —— RGA 缩到长边 160 + `cvtColor` 转灰度
2. `ch.judge.observe(thumb, judge_pos)` —— 判为「没变」就 `continue`
3. 通过了才 `snapshot_bgr_for_analysis()` 缩到送检尺寸并 `submit_frame()`

**先判据后取送检图**：被挡下时连那次 384 的 RGA 都省掉。手动模式整段跳过，
行为与接入判据前**逐位一致**。

### 帧号必须单调：踩过的坑

判据喂的帧号**不能用 `ch.frame_pos`** —— 那是 `cap.frame_pos`，**循环重播时归零**。
归零后 `gap = pos - last_pos` 变负，兜底间隔要等到重新攒够 `last_pos + every_n`
才恢复；`every_n` 大于视频长度时（共享 UI 默认 600，而 15~30s 素材只有
375~750 帧）就**再也攒不够**，循环之后彻底静默。多路改用单调的 `ch.frames`。

> **单路 `VideoSession` 没有这个问题，不要照着「一起修」去动它。**
> 它在循环重播点已经调了 `self._judge.reset()`（`session.py`，注释是
> 「拿上一段尾帧做参照，算出的变化是人为的」），reset 会把 `_last_pos` 置 -1，
> 紧接着的首帧无条件送检并重新播种成 0 —— 与 `cap.frame_pos` 同步归零，
> gap 始终有效。实测盯两次循环边界，`frame_pos` 与判据 `evaluated`
> （463→38、449→25）逐帧同步。
>
> 曾一度误判「单路同样受影响、影响 vd/vtl/vas 三张卡」。用打桩 gscore 复现
> 两种时序才看清：不 reset 时第 0 轮之后每轮放行 0 次，reset 时每轮都有放行。
> 附带结论 —— reset 后每轮那 1 次放行来自**首帧无条件送检**，不是兜底；
> 看 `sampled` 计数容易误判成「兜底已触发」。

### 端点挂本卡，不共用 /api/video/judge

`/api/video/judge` 操作的是单例 `VideoSession`，vtd / vtl / vd 三张多路卡走
`MultiChannelSession`，所以判据端点分别挂在
`/api/video-target-detection/judge{,/metrics,/reset}`、
`/api/video-target-localization/judge*`、
`/api/video-detection/judge*`。理由与 VAS 一致：
**自管理解码循环的东西，配置就得落到自己的服务上。** 前端
`FrameJudge.mount({apiBase})` 指向本卡端点，其余卡片共用同一套组件。

> `FrameJudge.mount()` 的 `canvasId` 要指向**画面容器元素**。多路宫格里没有
> 单一 canvas，锚在 grid 上（`GRID_EL`）才对。锚一个不存在的 id 不会报错 ——
> `sync()` 取不到矩形就永远 `hide()`，表现是判据角标和曲线**静默不出现**。

### 默认 mode = gscore（与单路一致）

接进来时默认 `gscore`，与单路 `VideoSession` 的
`FrameJudge(mode=EXPOSED_METRICS[0])` 保持一致，向导里也是 gscore 预选。

判据的意义就是「画面没变就不送检」：静态画面送去推理，拿到的是与上一次
**逐位相同**的答案，纯徒增 NPU 调用。默认关掉它等于让这张卡成为四张卡里
唯一的例外，且用户不改配置就拿不到省算力的收益。

> 这里曾一度把默认设成 manual，理由是「默认开启 gscore 会让静态画面不产出
> 事件，用户会看到『明明有人走动却没事件』」。**这个论证是错的**：
> 「有人走动」本身就是「画面有变动」的证据，而 gscore 的判定正是
> 「有变动就放行」—— 两者互相矛盾，描述的场景按定义不会发生。实测也印证：
> 默认阈值 10 跑真实监控素材时 `evaluated=15 / sampled=15`，**零门控**。
> 改动判据默认时别再用这类自相矛盾的「用户会看到 X」句式论证。

真正需要注意的边缘情况只有一个，且**四张卡共有**：目标在画面里占比极小
时，缩到长边 160 之后可能不足 8×8 判据块的显著量，阈值要按现场素材调 ——
这是阈值标定问题，不该用「改默认 mode」来绕。

### every_n 在多路下的单位

`every_n` 的单位仍是**解码帧**（25fps 源 → every_n=30 ≈ 1.2s）。但多路每路
每轮只看一次（3 路时约 4s/轮，32 路时更久），所以兜底在多数配置下根本轮不到
触发，它实际是**双保险**而不是主要节流手段。真要限流用「巡检间隔」秒/路。

这一点与单路不同（单路每帧都判，every_n 是主力），共享前端组件看不出来，
**排查「为什么限流没生效」时别拿单路的直觉套多路**。

## 多路宫格「点开放大到整个播放器」（v0.24，vd / vtd / vtl 三张卡）

单击某一路 = 选中（既有语义：切判据编辑通道），**再点同一路** = 等比例放大
到整个播放器。退出方式三选一都成立：再点同一路 / ESC / 点另一路。

三张多路视频卡（音视频描述 vd / 视频目标监控 vtd / 视频目标定位 vtl）行为
完全一致：**后端**一份 `set_zoom()` + 工厂里的一个 `/zoom` 路由，**布局**
一份 `multi-grid.js`，**卡片侧接线**一份 `static/core/zoom-frame.js`。

### 为什么放大是「纯布局」，后端只换那一路的显示分辨率

画面就是 tile 里那块 `<canvas>`，canvas 内容与 CSS 布局完全解耦 —— 放大
只需要 `gridTemplateColumns: 1fr` + 其余格子 `display:none`，剩下的行高由
`grid-auto-rows:1fr` 撑满 `.vd-stage`，等比例由 canvas 自己的
`object-fit: contain` 保证。**不用重建格子、不用重绘画布**。

分辨率那半段走 `MultiChannelSession.set_zoom()`（通用方法），路由
`POST /zoom` 定义在 `multi_api.build_multi_router` 里，三张卡自动都拿到。
后端只改 `Channel._disp_w/_disp_h`，不动解码 fps、不动送检尺寸
（`_ana_*`）、不动其它路；解码循环每帧读 `self._disp_w`，**下一帧即生效**，
不需要重开解码器。

> `ZoomPayload` 必须**模块级**定义（同 `JudgePayload`）：`from __future__
> import annotations` 下端点签名是字符串，FastAPI 只查模块 globals。

### 卡片侧接线为什么单独一个 core 模块

`notify`（放大态变化 → POST /zoom）与 `reconcile`（刷新后对账）这两段
**三张卡逐字相同**，差别只有「本卡的 API base」和「本卡的 overlay 提示」。
放在 `static/core/zoom-frame.js`（纯逻辑，无对应 css，注册在
`index.html.template` 里 `multi-grid.js` **之后** —— `attach()` 要往
`MultiGrid.onZoom` 上挂回调）。各卡只留三处一行调用：

```js
// 1) IIFE 内，注册回调（onError 要用卡片私有的 setXdOverlay）
ZoomFrame.attach({gridEl: GRID_EL, apiBase: API_PREFIX + "/video-detection",
                  onError: (m) => setVdOverlay(m, true)});
// 2) IIFE 内，状态快照 handler；另在 bindSelect handler 第一行（IIFE 外）
ZoomFrame.tick(GRID_EL, session, total);
ZoomFrame.onTileClick(gridId, idx);
```

`tick()` 收编了「有路 → 对账 / 无路 → 清宫格」这个分支，调用方只管传
`total`，分支本身只留一份。

> 抽出来还有一个直接原因：这几段逻辑的**对账方向曾经写反过**（见下）。三份
> 副本意味着同样的 bug 要修三遍、且必然漏掉一张。与 `window.MultiCard`
> 消灭双卡勾选器的 4 处重复是同一个动机。

### 放大档位：`min(720p, max(源长边, 当前档长边))`

```python
long_edge = min(DISPLAY_LONG_EDGE_ZOOM, max(src_long, cur_long))
```

两个 max/min 各挡一种退化，**两个都不能省**：

* **只按源长边 clamp 会让清晰度不升反降。** 非放大档本身就会放大源 —— 608
  宽的源在 ≤4 路档下已经渲成 960。只按 `min(1280, src_long)` 算的话，
  点开放大反而从 960 掉回 608，用户看到的是「放大 = 更糊」。
* **不封顶则一路能吃掉整个 RGA / JPEG 预算。** 720p 是给「单看一路」够用
  的规格，不是给 32 路宫格全员升档的。

竖屏源按 `derive_analysis_size` 走自己的宽高比（576x1280 → 576x1280），
不硬编码 16:9。

### 踩过的坑：`wasSelected` 必须在 `setSelectedChannel()` **之前**取

单击的既有语义是「选中」，由 `FrameJudge.setSelectedChannel()` 打
`.selected` —— 而它**同步**生效。放在它之后读 `.selected`，读到的永远是
`true`，于是**首次点击就被当成二次点击直接放大**，「先选中、再点开放大」
两步并成了一步，判据编辑通道根本来不及用。

所以 `MultiGrid.toggleZoom(container, idx, wasSelected)` 的第三个参数由调用
方在正确时机取好传进来，**函数内部不许现读**。

### 踩过的坑：`_fireZoom` 静默吞异常 → 通知链路整条失灵

放大接线一开始写在 app.js 的 IIFE **外面**（v0.23
多路判据接线那一片），引用不到 IIFE 私有的 `API_PREFIX` / `GRID_EL` /
`setVtlOverlay` → 运行时 `ReferenceError`。布局照切、画面照放大，**后端一次
都收不到通知**，而 `onZoom` 回调外面裹着 `try/catch {}`，连 pageerror 都不
会有 —— 表现为「功能好像做了，就是不生效」，没有任何线索。

现在 `_fireZoom` 的 catch 打 `console.warn`。**这类"通知失败但界面正常"的
链路，异常必须出声**；静默 catch 只会把一个 5 分钟的 bug 变成两小时。

判据：`app/apps/<card>/static/app.js` 的 IIFE（`})();`）之外那一段能看见的
标识符只有**外层**声明的那些。凡是要用卡片私有常量（`API_PREFIX` /
`GRID_EL` / `setVtlOverlay`）的函数，一律放进 IIFE 内。

### 踩过的坑：对账方向反了 → 用户刚点的放大 1 秒后被自己撤掉

硬刷新会丢本地 zoom 态而后端那一路还停在 720p，所以需要一个对账。但
**方向只能是「后端有、本地没有」**：

* 反向（本地有、后端说还没有）绝不能自动还原 —— `POST /zoom` 是异步的，
  用户刚点开放大、请求还在路上时就可能先收到一条 `zoomed=null` 的旧
  WS 快照，照着还原等于把用户刚要的升档在 1 秒内自己撤掉。
* 正向只在刷新后成立（本地为 null 时用户没有正在进行的 zoom 请求），
  且不会与用户操作打架。

配套：`open()`（换源）和 `close()`（清通道表）都必须把 `_zoomed` 清成
`None` —— 否则会留下指向不存在通道的悬空下标。

### 踩过的坑：关会话要清放大态，而且**四条关闭路径都得覆盖**

放大态是**会话内**的状态。会话关了但 `g.zoomed` 留着，下次 `open()` 的
`ensure()` 会拿残留值重新套上放大 —— 用户看到的是「刚打开 6 路，却只剩一格
放大、另外 5 格不见了」。

关键在于**关会话有四条路径**，不是一条：

1. 卡片自己的「关闭」按钮；
2. 离开页面时的 `DemoGuard.stopAndClear`；
3. `beforeunload` 的 `sendBeacon` / `fetch({keepalive:true})`；
4. 外部（含 `watchdog`、curl）直接打 `POST /close`。

只在按钮里 `MultiGrid.reset()` 覆盖不全，另外三条都会漏。所以清空由**状态
快照**驱动：`MultiGrid.resetIfClosed(gridEl, session)` —— 由
`ZoomFrame.tick()` 在 `total === 0` 时调，三张卡不直接碰它。

只认 `state === "idle"`：`opening` 阶段 `channel_list` 可能短暂为空，那时清
会把刚建好的格子拆了再重建；`error` 阶段格子上的红框 / ⚠N 是有用的诊断
信息，不该被抹掉 —— 真要清 error 会话，点「关闭」走 `close()` 就变 idle 了。

（v0.24 之前只有 vd 在按钮里 reset，vtd / vtl 压根没有 —— 这个不对称就是
本条坑的来源。加新卡片状态时记得跟着加这一行。）

### ESC 监听器按需挂载

`_bindEsc` 只在放大时 `addEventListener`、退出时 `removeEventListener`。
常驻的全局 keydown 会在别的卡片抢输入时也生效。`reset()` 里**必须先清
`g.zoomed` 再摘监听** —— 反了的话 `_bindEsc` 看到 `zoomed != null` 直接
return，监听器就永久留在 document 上了。

### 真机实测（本机 RK3588）

**交互**（`scripts/verify_zoom_tile.py`，6 路，真实浏览器 + 真实 WS）：

| 项 | 结果 |
|---|---|
| 单击 → 只选中不放大 | ✅ 不发 `/zoom` |
| 二次点击 → 放大 | ✅ 其余 5 格隐藏、`gridTemplateColumns: 1fr`、角标 13px→18px |
| 放大 → 通知后端 | ✅ `{channel:0,zoomed:true}` |
| 再点同一路 → 还原 | ✅ 通知 `{channel:0,zoomed:false}` |
| ESC → 退出且**保留选中** | ✅ |
| 点另一路 → 退出并选中那一路 | ✅ 通知的是**被放大的那一路**，不是新选中的 |
| vtd / vd 冒烟（共用 multi-grid） | ✅ 无 zoom 副作用，无 pageerror |

**性能**（`scripts/measure_zoom_perf.py`，6 路 × 576x1280 竖屏素材，
`LocateAnything-3b` 跑在 npu0，检测运行中；被放大那一路 176x384 → 576x1280，
**11 倍像素**）：

| 状态 | 合计解码 fps | 其它路均值 fps | 轮转 | 解码错误 |
|---|---|---|---|---|
| 基线 | 149.07 | 24.84 | 0.050/s | 0 |
| 放大 ch1 | 148.98 | 24.83 | 0.050/s | 0 |
| 还原 | 148.93 | 24.82 | 0.100/s | 0 |

**结论：总吞吐掉 0.09 fps（0.1%），其它路每路帧率与轮转速率均无变化。**
一路升档是**单路多花 CPU**，不会抢走其它路的预算 —— 这与 AGENTS 里
「wait_idle 忙等饿死解码」「每路 fps 按总预算摊分」那两次事故不同：那两次
是**所有路一起塌**，本次实测其它路 24.83fps 一路没掉。

> 但别因此推论「全员升档也无所谓」：6 路各升 11 倍是 66 倍的 RGA + JPEG
> 开销，`MAX_TOTAL_DECODE_FPS` / `MAX_TOTAL_DISPLAY_FPS` 会被直接吃穿。
> 「只升被放大那一路」是**必须**的，不是优化。
