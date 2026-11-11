# AGENTS.md

Compact guide for OpenCode sessions in this repo. **Current version: 0.9.3** (见 `app/__init__.py`)。

## What this repo is

Python web service (`wdnwebui`) exposing RK-platform hardware / model SDKs
(`wdnav`, `wdnai`) over HTTP via FastAPI. v0.4 起采用**卡片插件架构**:
每个应用是 `app/apps/<card_id>/` 下独立文件夹,自带 `manifest.json` +
后端 `api.py` + 前端 `static/`(template.html / app.js / app.css /
wizard.json);`app/core/manifest.py` 启动时扫描全部 manifest 装载。

## 仓库地址与同步策略（2026-11-10）

同一批仓库在三个平台上**同名对应**（`wdnwebui` / `wdnav` / `wdnai` /
`wdndds`，另有 `wdnapps_*` 等应用仓库）：

| 角色 | 地址 | 允许的操作 |
|---|---|---|
| **局域网主仓库** | http://10.10.10.10:8080/wardner | **pull + push** —— 唯一权威源，所有更新的第一站 |
| **GitHub** | https://github.com/rockchip-wardner | **只允许 push** —— 发布镜像，不从它 pull |
| **Gitee 国内镜像** | https://gitee.com/rockchip-wardner | **不允许任何 git 操作** —— 公开只读镜像，内容由上游自动同步 |

各仓的远端命名统一成：

```
origin = git@10.10.10.10:wardner/<repo>.git           ← 局域网主仓库（pull + push）
github = git@github.com:rockchip-wardner/<repo>.git    ← 只 push
```

**每次提交更新的顺序（不可颠倒）：**

1. **先推局域网主仓库**（`git push origin master`）—— 它是权威源；
2. **再推 GitHub**（`git push github master`）—— 只出不进；
3. **Gitee 一概不碰** —— 不 clone、不 pull、不 push。

⚠️ **Gitee 不做任何操作。** 它是上游自动同步出来的**公开只读镜像**：推上去的内容
会被下一次同步覆盖，且没有任何提示；连 `git pull` 也不该从它走 —— 它的内容滞后于
局域网主仓库，拉下来只会制造分叉。

⚠️ **GitHub 只出不进。** 它是**发布镜像**，不是开发源。`git pull` 拉的是「已经
发布出去的历史」，不是最新的开发状态 —— 拉它会让本地与局域网主仓库分叉。要同步
最新代码，只从局域网主仓库拉（`git pull origin master`）。

> **拉取代码只走局域网主仓库。** 各仓 `master` 的跟踪分支都指向 `origin`
> （局域网），所以裸 `git pull` / `git push` 操作的就是主仓库；推 GitHub 必须
> 显式写 `git push github master`。
>
> 本仓库当前**没有**配 gitee 远端（`git remote -v` 只有 `origin` / `github`）
> —— 保持这样。

## 提交信息规范（2026-11-11）

**commit message 必须用英文。**

- 格式：Conventional Commits —— `type(scope): subject`，正文（若有）也用英文。
  例：`fix(video-detection): keep the event badge above the canvas`
- 原因：本仓发布到公开的 GitHub 镜像（发布镜像 = 国际可见），提交历史是给国际读者
  看的 —— 中文提交信息让公开历史没法读。
- **只约束提交信息**：代码注释、docstring、仓内文档（AGENTS.md / README）仍按各仓
  现有的写法（中文没问题）。README 是中英双语，AGENTS.md 允许中文。

## Stack at a glance

- **Web framework**: FastAPI + Uvicorn (`run.py` boots `app.main:app` on
  `0.0.0.0:8000` with `reload=True`).
  - **手动起服务时必须用 `run.py`**（不能直接 `uvicorn app.main:app`）：`run.py` 的
    `_ensure_wdndds_native_path()` 会把 wdndds 的原生库目录加进
    `LD_LIBRARY_PATH`；缺了它，spawn 出去的 `compute_worker` 加载不了
    cshim，会**秒退**（rc=0，表现为「功能未启用: swcap_vision_grounding」）。
  - **生产 = systemd 单元 `wdnwebui.service`**，`ExecStart` 直接跑
    `uvicorn app.main:app --host 0.0.0.0 --port 8000`，**不带 `--reload`**
    → 改了后端代码必须 `sudo systemctl restart wdnwebui` 才生效。
  - ⚠️ **重启会连坐杀掉 worker**：systemd 的 `KillMode=control-group` 把
    cgroup 里的 `compute_worker` 一起 SIGTERM 掉 → 重启前确认
    `compute.autostart` 有值，否则重启后板子没有任何算力，得手工
    `POST /api/compute/instances` 把模型拉回来。
  - **别和 systemd 单元 `wdnwebui.service` 同时跑**：该单元
    `Restart=on-failure` + `RestartSec=5`，抢不到 8000 端口就每 5 秒
    重启一轮，而**每轮重启都会把 cgroup 里的 `compute_worker` 一起
    SIGTERM 掉** —— 症状是「worker 加载完（~33s）后 ~9s 就死」，
    很容易被误判成「NPU 加载有问题」。`strace -f -e trace=kill,tgkill`
    能直接看到发送方（`si_pid` 指向 systemd，不是 registry）。
    排查前先 `systemctl status wdnwebui.service`，冲突就
    `systemctl stop wdnwebui`。
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
   确认 N 次）；（b）模块级内存标志（`VR._progressForceIdle` /
   `VR.initialized` 等）全部归零 → 清空过的
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
   model 共用）+ `<模型目录>/<model_id>.json`（每个模型一个文件，**随模型
   权重一起分发**，声明身份 + `spec_factory`）。进程只负责：
   * 接收 `wdndds.serve_local_session` 定义的
     `{prompt, attachments, options}`；
   * 透传给 `wdnai.LLMLocalSession.run_prompt(...)`（**跳过** prompt 重新
     组装 —— prompt 是调用方拼好的）；
   * 把模型返回的**原始文本**原样发回。

   **加模型 = 放一个 `<模型目录>/<id>.json`**，不用再写一份 45 行的入口脚本。
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
   > **manifest 搬出仓库后，入口脚本仍留在 `app/core/compute/workers/`**
   > —— 它是**代码**不是模型的一部分，且上面那个 needle 依赖它的路径。

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

5. **本地算力与网络算力(peer)的权限边界：每块板子只管自己的算力**

   实例 `_source`：`spawned` / `adopted_local` 是本机的，`adopted` 是**别的
   板子**通过 LAN 提供的 service。卸载/停止 peer 的实例等于去动另一块板子
   的 NPU 显存。

   | 端点 | 本机实例 | peer(adopted) |
   |---|---|---|
   | `POST /compute/instances` | 启用（`allow_adopt=False`） | 403 |
   | `DELETE /compute/instances` | 停该模型本机全部实例 | 403 |
   | `POST /compute/restart` | 重启 → 起**本机** worker | 403 |
   | `POST /compute/stop-all` | 逐实例 unload，进程保留 | **跳过**（进 `skipped`） |

   两条实现约束：

   - **按实例卸，不按模型名**。`unload_instance(model_id)` 按模型名**全量**
     遍历 —— 卸一张卡会连带卸掉同模型在其它卡上的实例（含 peer）。要「每个
     本机实例各卸一次」走 `unload_one_instance(inst)`。
   - **重启不许 adopt peer**。`restart_instance_local` 调
     `ensure_worker_for_model(model_id)` 必须带 `allow_adopt=False` ——
     默认 True 会 adopt 同名 peer、或把 `_instances` 里已有的 peer 实例交出去
     →「重启本机服务」变成「本机改用 peer 的算力」。

   回归：`scripts/verify_peer_isolation.py`（API 层）+
   `scripts/verify_stop_local_skips_adopted.py`（registry 层）。

   > ⚠️ **回归脚本必须 mock `PUT`/`DELETE /api/compute/active-app`** ——
   > `DELETE` 会把 `compute.autostart` **一起清空**（`clear_active_app`），
   > 回归脚本不该改生产配置。`PUT` 会把承载该卡 caps 的 `(model, npu)`
   > 增量写进 `compute.autostart`。

## 加一张新卡：卡位占位符是**硬要求**（2026-11-06）

每张卡的页面模板是**服务端注入**进首页 HTML 的，不是前端拉的：

```
app/static/index.html.template   ←  每张卡一行 {% card_slot "<id>" %}
app/core/html_assembly.py::assemble_index_html  ←  逐个替换成该卡的 static/template.html
```

⇒ **加一张新卡，必须在 `index.html.template` 里加一行**
`{% card_slot "<card_id>" %}`。**漏了不会报错**，只会静默错乱：

* `#page-<id>` 这个 section **从来没被注入** ⇒ `ROUTER.showPage()` 找不到
  page section ⇒ 内容区一片空白；
* `wizard/state.js::mount()` 拿不到 pageSection ⇒ 向导容器 fallback 到
  `document.body.appendChild()` ⇒ **向导面板被挤到 `main.content` 外面**，
  落在屏幕下沿 ⇒ 用户看到「一大片空白 + 面板在下面」（2026-11-06 实测，
  `image-editor` 卡）；
* 运行期只有一条 WARNING（`no {{card_slot}} placeholder`）⇒ 没人看日志。

> ⚠️ 注意顺序：`showPage` 的 page 切换在 `framework.js` 里注册，
> 向导的 `onNavAway`（hashchange）在 mount 时才注册 ⇒ **framework 的先跑**。
> 所以「切到另一张卡」时，旧向导是被 `mount()` 里的 `unmount()` 拆掉的。
> ⇒ **`mount()` 里的跨卡卸载必须传 `silent=true`**，否则 `unmount()` 的
> 「非静默 ⇒ 把原 page 重新显示出来」语义会把**上一张卡**的页面留在屏幕上
> （两张卡叠在一起）。这条 2026-11-06 已修（`state.js::mount`）。

### 回归

* `python -m scripts.verify_card_slots` —— 断言每张已发现的卡都有卡位占位符，
  且渲染出的首页里都有 `id="page-<id>"`；
* `python -m scripts.smoke_wizard_all` —— 8 张带向导的卡逐个 mount 一遍
  （含 `media-generator` / `image-editor`）；
* `python -m scripts.verify_image_editor_ui` —— **真实浏览器**点一遍
  「生成图片」，断言无 JS 错误 + 请求体带 `strength`。
  ⚠️ 只走 HTTP API 的验收（`scripts/verify_image_editor.py`）**抓不到前端
  错误** —— 那次 `state is not defined` 就是这么漏过去的。
* `python -m scripts.verify_wizard_image_field` —— 向导的 `type: "image"`
  拖拽上传全链路（见下）。

> ⚠️ **向导类回归脚本对「后端记的 active_app」敏感**：
> 开机时 `ActivateManager.bootRestore()` 会把它还原成 `location.hash`
> ⇒ **上一张卡**（谁刚用过就留谁）的向导会先挂上，再在切页时被卸载
> ⇒ 脚本断言的「第一个向导事件」看到的是别的卡的 `wizard:unmounted`
> ⇒ **假失败**。⇒ 跑之前 `DELETE /api/compute/active-app`（`verify_usb_hid_wizard.py`
> 已经这么加固了）。

### 向导的 `type: "image"` 字段（拖拽上传一张图）

图生图这类卡片**必须有输入图**。向导原本只能填文本/数字/勾选 —— 视频卡的
「源」是另一套机制（值是**路径**，还要 `upload_endpoint` + `thumbnail_endpoint`）。
⇒ 用户被卡在「向导里没法给图 ⇒ 必须先跳过向导、去功能页上传」。

2026-11-06 给宿主向导加了 **`"type": "image"`**：一个**拖拽上传区**
（`_buildImageField()`，`app/static/core/wizard/state.js`）。

| 口径 | 值 | 备注 |
|---|---|---|
| 字段值 | **data URL**（`data:image/png;base64,....`） | 预览直接拿它当 `<img src>` |
| 落盘 | **不落 localStorage** | 字段声明 `"transient": true` |
| 取值 key | `opts[ui_key]` | 卡片剥掉 `data:...;base64,` 前缀拿裸 base64 |
| 文件名 | `state.values[<fieldId>__name]` | 完成页摘要显示它，**不是** base64 |

⚠️ **为什么不落盘**：`localStorage.setItem` 是**整体覆盖** —— 一张图的
base64 就几百 KB~几 MB，配额爆了会抛异常 ⇒ **整个 values 对象都存不进去**
（连提示词、步数这些小字段一起丢）。⇒ `_publishValues()` 落盘前把
`transient` 字段剔掉（`_persistableValues()`）。

⚠️ **`el.value` 用 `Object.defineProperty` 代理到 hidden input** ——
这样 `_formStepCanNext()`（required 判定）和 `_captureValues()`（取值）
两条**通用**路径**零改动**就能用（它们只看 `el.value`）。

⚠️ **别把整段 base64 打进完成页摘要** —— `_renderFinishStep()` 对
`type: "image"` 特判，只显示文件名。

**卡片侧怎么用**（`image-editor` 为例）：

```json
{ "id": "image-editor-original", "ui_key": "original", "type": "image",
  "label": "要处理的原图", "summary_label": "原图",
  "required": true, "transient": true,
  "accept": "image/png,image/jpeg,image/webp", "max_bytes": 12582912 }
```

`applyWizardSnapshot(opts)` 里剥前缀回填：

```js
if (opts.original) {
  const dataUrl = String(opts.original);
  const bare = dataUrl.slice(dataUrl.indexOf(",") + 1);
  setSourceImage(bare, "原图（向导上传）", dataUrl);
}
```

回归：`python -m scripts.verify_wizard_image_field` —— 拖拽上传 → 断言
**localStorage 里没有 base64** → 完成页摘要显示文件名 → 回填并自动开跑。

### 板端**没有 emoji / 符号字体**

`🖼 ✕ ⚠ ✓ ☑ ▯ 🗑 📁` 在板端全部渲染成**豆腐块 □**（实测：同宽 21px 的
同一字形）。板端可用的只有 `× ● ■ ▶ ← → ↑ ↓`。
⇒ 卡片模板里**不要用 emoji**；要图标就用**内联 SVG**。

## 模型分发：worker 配置随模型文件夹（v0.9.2 之后）

worker manifest 从**代码仓**搬到了**模型文件夹里**：

```
/userdata/models/
└── Qwen3-VL-4B/               ← 一个模型文件夹 = 一个分发单元
    ├── qwen3-vl-4b.json       ← worker 配置（随模型一起分发）
    ├── llm_Qwen3-VL-4B.1828.rknn
    └── ...
```

于是三件事同时成立：

1. **一个模型文件夹自描述** —— 权重和描述它的配置同住，HuggingFace 上
   一个 repo 就是完整安装包，用户 `git clone` 即装好。
2. **「本板装了哪些模型」是板子状态** —— 模型文件夹不在 → 该 worker 在
   算力配置页的「本地算力」里**不出现**（不是灰掉，是根本没有卡片）。
3. **clone 完不用重启** —— 后台每 ~2s 重扫一次模型根目录（`registry.py::
   _model_rescan_loop`），算力页另有「刷新模型」按钮
   （`POST /api/compute/refresh-models`）。

### 发现机制

`app/core/compute/workers/__init__.py`：

* `models_root()` —— `WDNAI_MODELS_DIR`（boot 时由 `base.model_path`
  写入）→ `default_models_root()`（`/userdata/models` →
  `/mnt/oem/models` → `~/models`）。**必须与 `wdnai.model._models_root()`
  同源**，否则 worker 找权重的地方和 SDK 找的不是同一个目录。
* `discover()` —— 遍历模型根下的子目录，每个目录里找 worker manifest：
  `<model_id>.json`（出厂约定）或 `worker.json`（id 从 body 取）。
  模型目录里的**其它 json**（tokenizer config 之类）不会被误认 —— 只有
  带 `spec_factory` 的才算。
* `find_manifest_path(model_id)` —— 定位单个 manifest（worker 侧热路径用，
  不解析全部 manifest）。

### 目录名不再由 SDK 决定

改造前，模型目录名在**两处**各写了一遍：manifest 的 `model_dir_name`
和 SDK 里硬编码的 `_models_root() / "Qwen3-VL-4B"`。两份漂移了就是
「文件明明在，模型却报 missing_model_dir」。

现在：**manifest 所在目录就是模型目录**，SDK 通过 per-model 环境变量
（`WDNAI_QWEN3_VL_4B_DIR` 等）跟随它 —— env 名由 manifest 的
`model_dir_env` 字段声明，所以**模型文件夹可以任意命名**（HF repo 名
不受 SDK 内部常量约束）。

`build_model_spec()` 在造 spec **之前**设 env，造完**之后**校验
spec 的所有路径字段都落在该模型目录内（`_assert_spec_under_model_dir`）。
老版本 SDK 不认这个 env 时会**立刻报错**，而不是静默去另一个目录找文件。

> SDK 侧为 `Qwen3-VL-Embedding-2B` / `Qwen3-TTS` 补了这两个 env 覆盖
> （此前 9 个模型里只有 7 个有）。env 名由 manifest 自带，SDK 不反向
> 依赖 webui。

### 「能力词表」与「provider」是两件事（本次拆开的硬约束）

`app/core/manifest.py::_known_software_capabilities()` 现在返回
**SDK 封闭枚举**（`wdnai.SoftwareCapability`），**不再**是
「本板已安装 worker 声明的能力并集」。

> 改造前用并集 → 板子少装一个模型 → 依赖它的**卡片**被判 FATAL →
> 卡片从应用网格消失、API 路由不注册。现在词表归词表、provider 归
> registry：卡片始终在，缺模型时运行期报「功能未启用」。
>
> `workers/__init__.py::known_capability_types()` 早就写好了这个函数，
> 但**一直没有调用点** —— 这次终于接上。

### autostart：一条坏的不再拖垮整份

`compute.autostart` 里指向**未安装**模型的条目 → **跳过该条 + 记日志**，
其余条目照常自启。改造前是 raise → boot 时 catch → **整份 autostart 作废**，
其他模型也一起不开机自启（`_parse_autostart_config` 现在逐条容错，
`_validate_autostart_entry` 负责单条校验）。

### 不动的三样东西

1. **worker 入口脚本** `app/core/compute/workers/compute_worker.py`
   留在原位（`/proc` needle 依赖它的路径）。
2. **`--model <id>` 参数**不变。
3. **`MODEL_PRESETS` 的引用方式**不变 —— 它仍是**同一个 dict 对象**，
   动态刷新只做原地 mutate（`registry.py::refresh_model_presets`），
   所以 ~60 处 `from ... import MODEL_PRESETS` 的调用点零改动。
   原地 mutate 用「先 update 后删」的顺序，避免并发读看到空 dict。

### 回归

`python -m scripts.verify_model_packaging` —— 发现 / 命名 / SDK 跟随 /
词表解耦 / autostart 容错 / 动态刷新 / 静态审计，21 条断言。

`python scripts/verify_model_download_card.py` —— **模型下载卡片**的角标 /
正文语义 + 续传磁盘预算，20 条断言（需要服务在跑）：
* 角标四态：下载中（模型名）/ 失败（红）/ 空闲（`已装 N/M`）/ 无清单；
* 正文行 `#cc-dl-last` **只在失败 / 取消时出现**，成功态 `display:none`；
* DOM 里**没有**库存计数行（`#cc-dl-stock` 已删）；
* `_staged_bytes()` 与「续传预算扣掉已暂存字节」。

> ⚠️ 页面断言必须**注入 + 读回在同一个 `evaluate` 里**完成 ——
> 算力配置页有 1Hz tick，每次 tick 都重新拉 `/model-download/status`
> 并覆盖 `_downloadStatus`；分成「注入 → 等 200ms → 读」两步的话，
> 假状态会被 tick 冲掉，读回来的是真实状态（第一次写脚本时就踩了）。

### 网页端「模型下载」功能（算力配置页）

用户在**算力配置页 → 本地算力栏的最后一张卡片**（「模型下载」）点开弹框，
从 HuggingFace 的 `wardner` 名下拉模型仓到本板；下载进度**显示在那张卡片上**。

```
app/core/compute/hf_transfer.py     传输内核（curl / Range / 看门狗 / sha256）
app/core/compute/model_download.py  模型语义（远端清单 / 本地扫描 / 任务队列）
app/system/api/model_download.py    4 个 REST 端点（/api/compute/model-*）
app/static/core/model-download-modal.js  弹框（自挂 DOM，仿 fs-modal.js）
app/system/static/system.js         卡片（_appendDownloadCard / _updateDownloadCard）
```

**传输方案的实测依据**（下面那张表的每一行都来自这里）：

* **不用 `hf download`**：它默认走 `hf_xet`（Rust 扩展），**突发式**传
  （19~52 MB/s 突刺）但**夹 40s 级停顿**，曾观测到 5 分钟零增长 —— 客户端
  只能干等。判别：`.incomplete` 字节数冻结 + `top -H -p <pid>` 看
  `hf-xet-*` 线程空转。⚠️ **不是体积门槛** —— 2.27 GiB 的文件也照样下成功。
* **并发不放大带宽**：4 条并发 Range 聚合 6.19 MB/s ≈ 单流峰值
  （8.08 MB/s）→ 全部串行，反而更不容易打爆镜像配额。
* **速率不由客户端路径决定**：同一条命令重复测量拿到过 1.95 和 8.08 MB/s
  两种全程均值（差 4 倍，落点不同 CDN 边缘节点）→ 本模块**不承诺速率**，
  只保证「不卡死 + 可续传」。
* **429 是配额**（5 分钟 10000 次 API 请求），退避重试，**不是失败**。

| 决策 | 理由 |
|---|---|
| **curl**，不用 `hf download` | xet 会 40s 级停顿甚至冻结；curl 能自愈续传 |
| **串行**（仓内文件逐个、仓与仓排队） | 并发不放大带宽，却更快打爆配额 |
| **暂存目录 + 原子改名** | 见下 |
| **每次重试重解析签名 URL** | CDN 签名会过期 |
| `--speed-limit/--speed-time` 看门狗 | 60s 内低于 100KB/s → curl 中止 → 重连 |
| **429 退避重试** | 配额是 5 分钟窗口，等一等就好，**不是失败** |
| sha256 对仓内 `SHA256SUMS` | 唯一权威摘要 |

> **暂存目录 + 原子改名是硬要求**：模型根目录每 ~2s 被重扫一次
> （`registry._model_rescan_loop`），而 `workers._model_dirs()` 只过滤
> `.`/`_` 开头的**目录**。所以下载落到 `<dest>/.dl-<name>/`（隐藏 →
> 扫描器看不见），**全部文件校验通过后**才 `rename` 成 `<dest>/<name>/`。
> 直接下到最终目录会有致命中间态：worker manifest（小 json）先落地、
> 权重还没下完 → 模型已被判定「已安装」→ 用户点启用必然加载失败。
>
> 下载日志也放 `<staging>/.cache/download.log` —— 不能留在模型目录里
> 当普通文件（发布侧的载荷比对只跳过 `.cache` 这一层）。

> **失败 / 取消时暂存目录故意保留** —— 它是**续传的落点**：
> `.dl-<name>` 路径是**确定性**的，重试同一个模型会落到同一个目录，
> 于是完整且 sha256 对得上的文件直接跳过、半截文件被 curl `-C -` 续传。
> 代价是「用户再也不重试」时那半个模型会一直占着盘（隐藏目录，扫描器
> 看不见，`/userdata` 上要靠 `ls -a` 才看得见）。
>
> **续传时的磁盘预算必须扣掉已暂存字节**（`model_download._staged_bytes`
> + `_run_job` 里的 `need = total - staged + 256MB`）：不扣的话，
> 「半个 9B 模型已经躺在暂存目录里」会把可用空间算成不够，于是重试
> 报「可用空间不足」—— **空间明明够**，而且够的那部分就是它自己的
> 半成品。量的是 `st_size`（**已占用的磁盘字节**）而不是逻辑大小：
> 半截文件只占已下载的那部分，正好等于「还要再下多少」。

**目录安全**：下载目录**必须是候选模型根之一**（`resolve_dest()` 严格
匹配候选清单，子目录直接拒）—— 网页端没有鉴权，不能让 UI 往盘外写。

#### 下载目录：**3 选 1**，而且必须是**模型根**

弹框底部是个**下拉框**，只有三个候选（没有自由输入、没有「浏览」按钮）：

| 候选 | 来源 |
|---|---|
| 当前配置的模型根 | `base.model_path`（**默认选中**） |
| `/userdata/models` | `_MODEL_ROOT_CANDIDATES[0]` |
| `/mnt/oem/models` | `_MODEL_ROOT_CANDIDATES[1]` |
| `<HOME>/models` | `_user_home() / "models"` |

> 顺序 = `app.core.config.model_root_candidates()`，和基础配置里
> 「RKNN 模型权重存放路径」那段说明**同源**。当前配置的模型根排第一，
> 且会被**去重** —— operator 把 `model_path` 改到 `/data/models` 时，
> 它必须仍然在列表里，否则「默认 = 系统配置里的模型目录」就落空了。

**为什么不能选子目录**：模型落地是 `<dest>/<模型名>/`，而模型根扫描
**只有一层**（`workers._model_dirs()` 用 `root.iterdir()`，不递归）——
把模型放进 `/userdata/models/test/X` 就永远扫不到。所以下载目录必须
**就是模型根**。后端 `resolve_dest()` 因此是**严格匹配**候选清单
（归一化尾斜杠后逐个比），子目录直接拒。

#### `<HOME>` 是 `/home/<用户名>`，**不是 `/root`**

服务（systemd `wdnwebui.service`）以 **root** 跑 → `$HOME=/root`。
但 `/root` 通常在**小分区**上（根分区 14G），而有些板子会把 `/home`
挂到固态硬盘/外接盘上 —— 模型应该放那儿。所以 `<HOME>` 的含义是
**操作员（装机用户）的家目录**，`/home/<用户名>`。

`config._user_home()` 的解析顺序（先准后粗）：

1. `SUDO_USER` —— sudo 之前的那个用户（手动 sudo 跑时最准）；
2. **装机目录的属主** —— 服务进程的 cwd（`WorkingDirectory`）归谁所有，
   那人的家目录就是操作员的家目录。**systemd 场景下最可靠**
   （`/home/<user>/work/wdnwebui` 归 `<user>` 所有）；
3. 系统用户库里**唯一一个** uid≥1000 且家在 `/home` 下的用户；
4. 兜底 `Path.home()`。

> 改之前第 4 步是**唯一**的兜底 → systemd 环境下解析成 `/root`，
> 候选清单里就出现了 `/root/models` —— 那是**错的位置**。

**模型根只有一个**：`WDNAI_MODELS_DIR` 在 **boot 时**从 `base.model_path`
写进环境变量（`app/main.py`），`workers.models_root()` 只认它。
所以下载到 `/mnt/oem/models` **不会**被算力页识别 —— 除非把基础配置里的
「RKNN 模型权重存放路径」也改成那个目录**并重启服务**。
弹框在选中非模型根目录时会明确警告这一点（否则用户会以为下载完就能用）。

> **下拉框的措辞**：候选标签用「**当前模型目录**」而不是「当前模型根」——
> 前者跟基础配置里「RKNN 模型权重存放路径」的说法对齐。
>
> ⚠️ **`<select>` 收起态会把选中的 option 文本裁掉**（浏览器原生行为，
> `scrollWidth` 看不出来 —— 它只反映元素自身，不反映 option 文本）。
> 所以 `.mdl-dest-select` 必须：
> * **不许设死宽度上限**（原来的 `max-width: 17rem` 是个硬顶）；
> * **不许被 flex 挤扁**（`flex: 0 1 auto` → 改成 `0 0 auto`）；
> * footer 空间不够时让它**换行**（`.mdl-foot` 有 `flex-wrap: wrap`）。
> 外加 `title` 属性给出完整路径，hover 可见。

**远端清单的坑**：镜像的 tree API **慢且抖动**（同一接口实测 0.9s /
1.3s / 11.4s 都出现过），且 list 接口的 `siblings` **不带 size**（只有
tree API 带）。故：清单只等 1 个 API 调用，**大小后台补齐 + 缓存 6h**；
清单本身也缓存 60s。⚠️ 这个端点必须写成**同步 `def`** —— 写成 `async def`
会在里面做阻塞 I/O，**把整个事件循环卡住**（实测一次 22s，期间整个
Web 服务无响应）。

**前端两个坑**：

* 下载卡片必须**在排序循环之前**塞进 `newOrder` —— 排序循环消费完
  `newOrder` 才跑，事后 push 等于没插进 DOM（卡片永远不出现）。
* 列表因「大小后台补齐」而刷新时**只更新大小文本，不重建 DOM** ——
  重建会把用户正在点的复选框换掉（元素 detach → 点击落空 / 勾选状态闪一下）。

#### 踩过的坑：curl 的 `--retry` 会**破坏 `-C -` 的续传语义**

症状是**下载进度条会倒退**（308 MiB → 2.7 MiB → 52 MiB），看起来像
「下载器把已下的数据扔了」。

根因：`_curl_cmd` 里同时给了 `--retry 5 --retry-all-errors` 和 `-C -`。
curl 的**内部重试**在重发请求时会把输出文件**截短到某个更小的偏移**，
于是同一段数据被反复重传。实测（同一个 2.27 GiB 文件，用 `--max-time 5`
人为制造中途超时，连续 3 次调用同一个目标文件）：

| 命令 | 3 次调用后文件大小 | 每次耗时 |
|---|---|---|
| `--retry 5` + `-C -` | 15.7 → 59.6 → **89.6 MiB** | 55.2 s（6 次内部尝试） |
| 去掉 `--retry` + `-C -` | 36.9 → 69.4 → **127.7 MiB** | 5.0 s（干净续传） |

curl 报错里的 offset 能自证续传是否精确：无 `--retry` 那组三次分别是
offset 0、38728376（= 上一次的传输量）、72878952（= 前两次之和）
—— **逐次累加，零重传**。

修法：`_curl_cmd` **去掉 `--retry` / `--retry-all-errors` /
`--retry-delay`**，重试全部交给上层 `download_repo` 的
`for attempt in range(tries)` 循环（`tries` 默认 12）。上层做的是 curl
内部重试**做不到**的两件事：① 每次重试**重新解析签名 URL**（CDN 签名会过期）；
② 用 `-C -` 从**当前**文件大小精确续传。

> ⚠️ 曾经怀疑是「服务器不认 Range / 返回 200 导致截断」—— **不是**。
> curl 报错里的 offset 累加关系才是判据，别凭猜。

#### 踩过的坑：网页下载的产物属主是 `root`（systemd 服务以 root 跑）

curl 写出来的模型文件是 `root:root`，而板上其它模型是 `rockchip:rockchip`。
功能上读不受阻（644/755 对 others 开放），但板子状态不一致，rockchip 侧的
运维脚本也写不动这些文件。

修法：`model_download._align_owner()` 在**原子改名之前**把暂存目录整棵
树的属主对齐到**最近的非 root 属主祖先目录**（`/userdata/models`
→ rockchip）。取「祖先」而不是 dest 本身：dest 可能是服务刚建出来的
（root 属主），真正的参考是它上面的模型根。改名之后再改就晚了 ——
目录已经在模型根里，扫描器会把它当成已安装模型。

> 附带好处：下载件属主变成 rockchip 后，**普通用户也能删**
> （`shutil.rmtree` 不再因权限失败）。用 root 跑的服务 +
> `rm` 被 shim 成 `mavis-trash` 的环境下，这条影响清理脚本。

#### 踩过的坑：进度条显示「15/14 文件」

`DownloadManager._refresh_progress` 数暂存目录里的文件时，**把
`.cache/download.log` 也算成了一个已下载文件** → 文件数比远端清单多 1。
`xfer.dir_size` 早就跳过 `.cache` 了，进度统计没跟上。修法同上：
`relative_to(staging)` 后跳过首段为 `.cache` 的路径。

#### 踩过的坑：角标「✓ 已安装」会**撒谎**（报的是任务流水，不是库存）

**症状**：用户把 `/userdata/models/Depth-Anything-V3` 删了，算力配置页
「模型下载」卡片右上角**仍然显示绿色「✓ 已安装」**；而本地算力栏里
`[Depth]视觉深度信息v3` 卡片已经消失，弹框里它也是「未下载」。三处互相矛盾。

**根因**：角标的唯一数据源是**下载任务 history**（`system.js`
`_updateDownloadCard`），不是「模型在不在盘上」：

```js
const lastDone = history.find((h) => h.state === "done");
if (lastDone) { badge.textContent = "✓ 已安装"; ... }   // ← 只看任务流水
```

`history` 是 `DownloadManager` **进程内存**里的流水（只留最近 10 条，
重启服务就没了）。任务成功过一次，流水就永远留着 `state=done` ——
用户把模型目录删了，流水**不会**跟着作废，于是角标一直绿。

**语义错配**：作者原意是「**刚才那次下载任务怎么样了**」，但文案写成
「已安装」—— 读起来像**库存陈述**。两者混在一起就必然出现「板上没有、
角标说有」。

**修法**：角标改成报**真实库存**「已装 N/M」，任务流水只保留**异常**
（失败 / 取消）写到卡片正文。

| 状态 | 角标 | class | 数据源 |
|---|---|---|---|
| 下载中 | 模型名 | `…-active`（蓝） | `status.active` |
| 最近一次任务失败 | `✗ 下载失败` | `…-err`（红） | `status.history[0]` |
| 空闲 | `已装 N/M`（全齐才绿） | `…-ok` / 中性 | `/api/compute/model-repos` |

> **卡片正文只剩异常行**（`#cc-dl-last`，正常态 `display:none`）：
> * **成功流水不显示** —— 「上次：X 下载完成 · <path>」是**操作流水**不是
>   **当前状态**，模型目录被用户删了它也照样留着；而且它是纯历史，对
>   「现在板子上有什么」没有任何贡献（那个信息在角标「已装 N/M」里）。
> * **库存计数不显示** —— 角标已经报了「已装 N/M」，正文再写一遍
>   「本板已装 8/8 个 wardner 模型」是**同一句话说两遍**。
> * 只有**失败 / 取消**才留一行（失败原因放正文而不是只藏 hover，
>   角标的 `title` 里也带一份）。

* N/M 的口径必须用 `model-repos` 的 `installed` 标志（和下载弹框同源），
  **不能**用 `snapshot.services.length` —— 后者会把「本地自有模型」
  （不在远端清单里的）算进分子，分子分母不同源。
* **顺带修掉一个死代码**：原来先判 `lastDone` 再判 `lastFail`，只要
  history 里有**任意一条** done，红色「失败」分支就永远轮不到。
  现在只看 `history[0]`（history 按 `finished_at` 倒序 = 最近一次终态任务）。
* 清单**不能 1Hz 拉**（那个端点要扫本地模型目录 + 查镜像）→ 30s 节流；
  下载刚结束时强制刷一次；弹框打开时把清单推给卡片（`setRepoInventory`）。

> **教训**：**任务态 ≠ 库存态**。凡是「N 个已完成 / N 个共 M」的指示器，
> 数字必须来自**被计数对象的当前状态**，不能来自**操作流水**。
> 凡是文案会被读成库存陈述的（「已安装」「已就绪」），就别拿任务事件驱动。

## 设备拓扑：模型要占几个 NPU 设备（`requires_cards` 不够）

`requires_cards` 数的是**本机 EP 协卡**（`cards.py::card_no` 口径）。
但有些模型还要占住**第三个 NPU 设备**，而那个设备**不是 EP 协卡** ——
算力卡列表里**看不见**它。

Qwen-Image-2.1 就是：TE/DiT 占 2 张 RK1828，**VAE 必须独占**第三个 NPU：

* **A：2×RK1828 + 本机 RK3588** —— VAE 跑本机 rknn2 NPU（模型包里有
  `vae_qwenimage21-512.3588.rknn`）；
* **B：3×RK1828** —— VAE 独占第三张协卡（模型包里有
  `vae_qwenimage21-512.seg{0..3}.1828.rknn`）。

两者都没有 ⇒ **不允许运行这个模型**（不是"运行时报错"，是**禁用 + 400**）。

### manifest 怎么声明

worker manifest 多一个 `card_requirements` 字段（**可选**，没声明的模型
行为完全不变）：

```json
"card_requirements": {
  "total_devices": 3,
  "layouts": [
    {"id": "2x182x+3588", "label": "2×RK1828 协卡 + 本机 RK3588 NPU",
     "ep_cards": 2, "local_npu": 1,
     "local_label": "本机 RK3588 NPU（VAE 解码）",
     "required_files": ["vae_qwenimage21-512.3588.rknn"]},
    {"id": "3x182x", "label": "3×RK1828 协卡",
     "ep_cards": 3, "local_npu": 0,
     "required_files": ["vae_qwenimage21-512.seg0.1828.rknn", "..."]}
  ]
}
```

`layouts` 是**可选布局**，按顺序取第一个本机满足的；一个都不满足 ⇒
禁止运行。`required_files` 检查模型包里对应文件在不在（布局 A 要
3588 的 VAE、布局 B 要 4 段 1828 的 VAE —— **模型包的文件名就是拓扑
声明**）。

### ⚠️ `requires_cards` 是**动态**的，不是 manifest 里的静态值

EP 协卡要绑几张**取决于选中的布局**：A 绑 2 张、B 绑 3 张。静态
`requires_cards: 2` 只能表达 A。

⇒ **真正的口径**是 `registry.effective_requires_cards(model_id)` ——
取选中布局的 `ep_cards`，没有布局声明才回退静态值。这个值被三处共用：

| 位置 | 用途 |
|---|---|
| `api/compute.py::start_instance` + `InstanceRequest` 校验 | 绑定几张卡 |
| `registry.snapshot()` → `svc.requires_cards` | 前端「运行在」列几张卡 |
| `workers/_base.py::allowed_card_counts` | worker 侧校验传入卡数 |

worker 侧校验的是**集合**（`{2, 3}`）而不是单一数字 —— 绑错卡数会让
VAE 找不到独占设备（2 张 → VAE 跑本机 NPU；3 张 → VAE 跑第三张协卡）。

### 三道闸门，缺一不可

1. **主进程启动闸门** —— `registry.check_device_requirements()`，
   `ensure_worker_for_model()` **第一行**就调它；不满足抛 `ValueError`
   ⇒ API 转 **400**（`api/compute.py` 两条分支都有 `except ValueError`）。
   autostart 路径已有 `except Exception` 兜底 ⇒ 只记 warning，不拖垮
   其它模型。
2. **前端禁用** —— 算力卡页**只在硬件不满足（或没法确认）时**显示
   「需要 N 个 NPU 设备 + 缺什么」（逐条列出每个布局的缺项）并把 toggle
   锁掉。**满足时不显示**那块 —— 同一个信息「运行在」那一行的固定条目
   （▣ 本机 RK3588 NPU（VAE 解码）[自动]）已经说过一遍，重复一遍只是把
   卡片撑得很奇怪。
3. **worker 侧兜底** —— 收到的卡数不在允许集合里 ⇒ 拒绝 load。

### 「运行在」要把**第三个设备**显示出来

本机 NPU **不在算力卡列表里**（那列表只列 EP 协卡）⇒ 光看
「RK182x #0/#1」用户会以为模型只占 2 张卡。

所以「运行在」里追加一个**固定条目**（`.cc-target-fixed`，不可勾选 ——
它不是通过 `npu_index` 绑定的，由 worker 在 open 阶段自己挑）：

```
运行在  [☑ RK182x #0] [☑ RK182x #1]  ▣ 本机 RK3588 NPU（VAE 解码）[自动]
```

文案来自布局的 `local_label`（manifest 声明，前端不写死）。

### 本机 NPU 探测只做**文件存在性**检查

`cards.py::local_npu_ready()` 查 `librknnrt.so*` 在不在
（**不 dlopen**）—— 不在 webui 主进程里加载 NPU 运行时。结果**缓存 10 秒**
（算力页每秒拉一次 snapshot，`ctypes.util.find_library` 会在热路径上
反复跑 `ldconfig`）。

### 回归

`python -m scripts.verify_compute_device_req.py` —— 算力卡页四个场景：
满足（**不显示**设备要求块 + toggle 可用 + 「运行在」有第三个设备）/
不满足（显示缺什么 + toggle 禁用）/ 单卡模型（不显示）/ 枚举失败
（如实说「无法确认」，但不拦）。

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
`video_retrieval` 的 `/source/open` 单路预览使用，于是「某张卡开着检测
→ 切到 VR 点打开」会把 **VR 的帧以 channel=0 灌进那张卡的检测器**
（`submit_frame` 只在 detector 未启用时才 no-op），事件被错记到第 1 路。
删掉它同时省掉一次无谓的每帧调用。

> `VideoSession` 单例本身还在，但三张多路卡都不再走它 —— 现在只剩
> `video_retrieval` 的 `/source/*` 单路向导预览在用。

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
* **浮层必须显式给 `z-index`，否则会被画面盖死。** 宫格那一版把画面从「非定位的
  `<canvas>`」换成了「`position: relative` 的 `.vd-tile`」，而 `.vd-tile` 是
  `MultiGrid.paint()` **追加**到宫格 div 上的 —— 排在提示层之后。CSS 2.1 附录 E
  第 8 步：定位元素且 `z-index: auto` 的按 **DOM 顺序**绘制 → `.vd-tile` /
  `.vd-overlay` / `.vd-event-badge` 三者同一层叠组 → 后来的 tile 赢，而 tile 有不透明
  的 `background:#000` → **浮框整个被盖住**（用户现象：音视频描述页右上角那个显示
  检测内容的浮框不见了；EOF / 帧渲染失败 / WS 错误 的提示层同样看不见）。
  层级：`tile(0) < .vd-overlay(5) < .vd-event-badge(6)`，`.vtd-crop-popup` 的 6 同理。
  改 DOM 没用 —— tile 是运行期 append 的，静态 DOM 里排不到它后面。
  自检：`scripts/check_canvas_overlay_stacking.py`（不依赖浏览器，直接解析 CSS 算层叠）。

### vtl 的「命中定格」：定格**算框那一帧**，不是定格「事件到达时」的画布

事件到达时把**该路 tile canvas** 换成「模型算框时看到的那一帧」，定格 2s，
期间的重绘就是**反复重贴这张帧 + 叠 bbox**（浏览器渲染管线会把长期不写入的
canvas 降级）。

> **踩过的坑：定格「事件到达时的画布」= 画框必然偏移**
>
> 框是在**送检帧**上算的，而事件要等模型推理完才到 —— 本机实测 grounding
> 单次 **1.66s ≈ 41 帧 @25fps**。等事件到浏览器，实时流早就往前跑了 41 帧，
> 目标（人）已经走开；把那一刻的画布定格，框就落在**别的时刻**的目标上。
> 这就是「定位到目标后画框在实时画面里偏移」的全部原因。
>
> 实证（`scripts/verify_vtl_box_alignment.py`）：同一批框画在
> 「送检帧」上像素级贴合；画在「事件到达时的画布」上整体错开一人身位。
>
> **修法**：后端把**算框用的那一帧**随事件一起下发（`frame_b64`），
> 前端定格它。关键在于**零额外编码**：`read0_multi` 的 `outs[0]`（显示尺寸）
> 与 `outs[1]`（送检尺寸）是**同一次解码**的两次 RGA，内容完全一致；解码
> 线程本来就要把 `outs[0]` 编成 JPEG 推给 WS，多留一个引用即可。
>
> 三个不能省的细节：
>
> * **多路必须按路冻结**。用一份全局冻结态会把所有画面按在同一张旧帧上，看起来
>   就是「整个画面卡住不动」。
> * **整帧有内存账**。显示尺寸 JPEG 单条 ~130KB，事件历史 200 条就是 26MB。
>   所以：事件只给最近 12 条留整帧（`_trim_event_frames`）；WS 建连时的
>   `event_history` 包与 `/events` REST 都**去掉整帧**（`?frames=1` 可要回来）。
>   实时事件（WS 的 `event`）必须带整帧 —— 前端定格靠的就是它。
> * bbox 坐标在**送检帧**（448×252）空间，画布是**显示尺寸**（960 或 384 宽），
>   必须按 `canvas 实测尺寸 / frame_w·frame_h` 换算，否则框会挤到左上角。
>   整帧图与框空间不同尺寸时，历史缩略图那条路径也要按
>   `图尺寸 / frame_w·frame_h` 换算（`vtlRenderHistoryItem`）。

### vtl 的 grounding prompt：**不能带 JSON Schema**

vtl 走 `swcap_vision_grounding`（LocateAnything-3b，`kind="grounding"`）。detector
照例 `sess.respond(prompt=..., generating=LocatorDoc, ...)`，而 `LocatorDoc`
的 JSON Schema 有 2KB 多。

> **踩过的坑：schema 进了 grounding 的 prompt → 「定位到 41 个目标」**
>
> 症状：事件里目标数暴涨（40~112 个），标签是
> `description` / `ROI 左上角 x，0–1000 归一化。` / `ROI 右下角 y，0–1000 归一化。`
> 之类 —— 看着像「模型输出质量波动」，其实**错误在输入侧拼接**。
>
> 那些字符串就是 `LocatorDoc` / `BoundingBox` 的 **JSON Schema 字段名和
> Field description**（`wdnai/grounding.py` + `app/core/llm/detector_base.py`
> 的 `_build_grounding_doc_class`）。LocateAnything 是 grounding 模型，
> **把 prompt 里的每个名词短语都当成要定位的目标** → schema 的字段被逐个
> ground 出来。
>
> 真机 A/B 实测（广州地铁人流素材 + 查询「穿黄色衣服的人」，6 帧）：
>
> | | 每帧目标数 | 最慢一帧 |
> |---|---|---|
> | schema 进 prompt（修复前） | `[1, 0, 144, 5, 2, 1]` | **13.7 s** |
> | 干净 prompt（修复后） | `[1, 0, 1, 1, 1, 1]` | ~0.5 s |
>
> **「污染」是逐帧随机的** —— 某些帧看着正常、某些帧直接炸成上百个框。
> 所以「12 条事件里 8 条干净」**不能**当成「8 条真的干净」；那 8 条只是
> 这一帧模型没跑偏。
>
> **修法是两层，缺一不可**：
>
> 1. **wdnai**：`LLMSession._suppress_generating_in_prompt` 对
>    `kind in ("tts", "grounding")` 返回 `True` —— grounding 的 prompt 里
>    只有用户查询，`generating=` 仍然承载输出侧解析
>    （`_parse_locate_output` 照旧从 raw text 抽 `<box>`）。
>    符合「per-model 行为差异写进 wdnai」。
> 2. **wdnwebui**：主进程给 `LLMRPCSession` 传**真正的 `ModelSpec`**
>    （`app/core/compute/workers.build_model_spec(model_id)`），而不是
>    worker manifest 的 preset dict。
>
> **第 2 层是被第 1 层的一个坑逼出来的**：SDK 判断模型族靠
> `spec.kind`，而 manifest 的 preset dict **没有 `kind`** → 判定拿到
> `None` → 抑制**静默失效** → schema 照旧进 prompt。
> 只改 wdnai 而不改这里，真机跑起来**完全没效果**（`WDNAI_TRACE_PROMPT`
> 打印的 `kind=None` 就是现场证据）。
> 现在两边都堵上了：SDK 侧 `_model_kind()` 兼容 dict / ModelSpec 两种
> 形状，**解析不到时打一条 warning**（不再静默）；webui 侧统一走
> `build_model_spec()`，worker 和主进程用**同一份**解析逻辑。
>
> 回归防护：
> * `python -m scripts.verify_grounding_prompt`（不占 NPU）断言
>   grounding prompt 里没有 schema、且 vlm 的 schema hint 没被误伤；
> * `python -m scripts.verify_grounding_live`（占 NPU）跑真实 vtl 卡片，
>   断言**每帧目标数稳定** —— 只查标签会漏判：模型没跑偏的那些帧看起来
>   是干净的（真机复现：12 条里 11 条是 1 个目标，第 11 条炸成 40 个假框
>   + 1 个真框 = 用户报的「41 个目标」）。
>
> 排查同类问题先看**真正发出去的 prompt**：`WDNAI_TRACE_PROMPT=1` 会把每次
> 推理实际发出的 prompt 打到 stderr（含 `kind=` 和长度）。


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
  `assemble_index_html()`(`app/core/html_assembly.py`)在每次 `/` 请求时动态
  拼装 —— `/static/index.html.template` 里 3 个占位符:`{% card_slot "<id>" %}`
  (替换为各 app 的 `static/template.html` 片段)、`{{ card_css_links }}`
  (拼装各 app 的 `<link rel="stylesheet">`)、`{{ app_manifests }}`
  (替换为 JSON dump)。

### 卡位占位符:卡片缺席时必须**摘掉**(模板标记不许漏给用户)

`{% card_slot "<id>" %}` 只对**已发现**的卡生效。插件包被卸载、源码被删、
manifest 校验不过(插件 import 失败)时,模板里那一行占位符**没人替换** ——
而 `{% ... %}` 在 HTML 里**没有任何特殊含义**,浏览器会把它当**普通文本**
原样显示在页面上。

> **真机复现**:删掉 `wdnapps_hdmi_processor` / `wdnhid` 两个仓库源码后,
> 首页上凭空多出一行 `{% card_slot "usb-hid" %}`。插件包没了 → `usb-hid`
> 卡不在 `discover()` 结果里 → 占位符没人替换 → 模板语法泄漏到 UI。

所以 `assemble_index_html()` 在注入完已发现卡的片段之后,**必须**再跑一遍
`_drop_unmatched_card_slots()` 把剩下的占位符摘掉:

- 替换成 HTML 注释 `<!-- card_slot "<id>": card not installed -->` ——
  浏览器不可见(问题消失),源码里留一行线索(将来把插件装回来知道往哪儿放);
  删行则连线索都不剩。
- 同时 `LOG.warning` 打出来,便于发现"卡片没装上"。

**别只删模板里那行占位符了事** —— 插件装回来时那张卡要能自动回来。
模板里保留占位符 + 拼装时摘掉没匹配上的,是唯一同时满足两者的做法。

回归防护:`python -m scripts.verify_card_slot_missing`(离线,不占 NPU)
断言拼装结果里没有任何残留的 `{% card_slot ... %}`。

> **顺带**:插件包被删后,venv 里常留下**孤儿 editable 元数据**
> (`__editable__.<pkg>.pth` + `<pkg>-<ver>.dist-info/`,entry point 仍登记着
> 那个卡)。后果是 `discover()` 每次请求都 import 一次不存在的模块并
> `LOG.error`。**连元数据一起清掉**:`pip uninstall -y <pkg>`。

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
5. `assemble_index_html(discover_cards())`(`app/core/html_assembly.py`)读
   `app/static/index.html.template`,
   按 `display_order` 拼装每张卡的 `<template>` 片段、`app.js` `<script>`、
   `app.css` `<link>`,输出最终 `index.html`(动态路径,运行内存)。
   缺席卡的 `{% card_slot %}` 占位符在这一步被摘掉(见「卡位占位符」章节)。
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

### 当前卡片清单(v0.25 插件化)

卡片分两个来源，宿主 `app/core/manifest.py::discover()` 两条通道并成同一
条校验管线：

1. **内置** —— 扫 `app/apps/*/manifest.json`（磁盘）
2. **插件** —— 扫 `wdnwebui.cards` entry point 组（已安装的 wheel）。
   entry point 名 = 卡 id（**连字符**，须与 manifest `"id"` 逐字相同），
   值 = 可 import 的卡模块；宿主据此定位卡片目录（`模块目录/manifest.json`
   + `模块目录/static/`）。因此插件包**不需要任何特殊支持**，保持与在树
   时完全相同的目录结构即可，所有 URL / 路由 / wizard 字段 id 都不用改。

依赖方向是单向的：插件 → `app.core.*`。core 不得依赖插件，否则插件没装
时 core 会静默降级。

> **脚本别再硬编码 `app/apps/<id>/`** —— 7 张卡的源码在插件包里，不在仓库
> 里。verify 脚本要取卡片的 wizard.json / manifest.json / app.js，走
> `scripts/card_dirs.py`（内置 + 插件双通道解析，返回 id / 目录名 / 路径 /
> import 命名空间）。否则轻则 ENOENT 崩掉，重则**扫不到任何东西却照样报
> 绿** —— `verify_apps_isolation` 的跨卡 import 检查只剩 2 张内置卡时就是
> 这个下场。

| `id` | capability（能力组） | 后端入口 | 来源 |
|---|---|---|---|
| `text-chat` | `swcap_text_chat` | `app.apps.text_chat` | 内置 |
| `tts` | `swcap_text_to_speech` | `wdnapps_audio_chat.cards.tts` | `wdnapps_audio_chat` |
| `qwen3-asr` | `swcap_audio_asr` | `wdnapps_audio_chat.cards.qwen3_asr` | `wdnapps_audio_chat` |
| `audio-analysis` | `swcap_audio_chat` | `wdnapps_video_analyzer.cards.audio_analysis` | `wdnapps_video_analyzer` |
| `video-retrieval` | `swcap_vision_embedding` | `wdnapps_video_analyzer.cards.video_retrieval`（检索） | `wdnapps_video_analyzer` |
| `video-detection` | `swcap_vision_chat` + `swcap_audio_chat`（两个模型） | `wdnapps_video_analyzer.cards.video_detection` | `wdnapps_video_analyzer` |
| `video-target-detection` | `swcap_vision_chat` | `wdnapps_video_analyzer.cards.video_target_detection` (TargetDoc) | `wdnapps_video_analyzer` |
| `video-target-localization` | `swcap_vision_grounding` | `wdnapps_video_analyzer.cards.video_target_localization` (LocatorDoc) | `wdnapps_video_analyzer` |
| `usb-hid` | `swcap_vision_chat` + `swcap_tool_call`（**同组 → 同一模型**） | `wdnapps_hdmi_processor.cards.usb_hid`（手动透传 + AI 任务循环 `agent.py`） | `wdnapps_hdmi_processor` |

### 能力词表由 wdnai 定义，不许自造

7 个 `swcap_*` 全部是 `wdnai.SoftwareCapability` 的成员（`wdnai/capability.py`），
命名轴统一为 `<输入模态>_<输出形态或功能>`：

| 成员 | 值 | 含义 | provider |
|---|---|---|---|
| `TEXT_CHAT` | `swcap_text_chat` | 文本→文本（多轮 / 流式） | qwen3.5-vl-4b |
| `VISION_CHAT` | `swcap_vision_chat` | 图像→文本 | qwen3-vl-4b、qwen3.5-vl-4b |
| `VISION_GROUNDING` | `swcap_vision_grounding` | 图像→0–1000 归一化 bbox | LocateAnything-3b |
| `AUDIO_CHAT` | `swcap_audio_chat` | 音频→文本 | qwen2.5-omni-3b |
| `VISION_EMBEDDING` | `swcap_vision_embedding` | 图像→向量 | qwen3-vl-embedding-2b |
| `TEXT_TO_SPEECH` | `swcap_text_to_speech` | 文本→语音 | qwen3-tts |
| `TOOL_CALL` | `swcap_tool_call` | →**可执行动作 / 工具调用** | qwen3.5-vl-4b |

**能力描述的是模型能做什么，不是哪个业务用它。** 业务归属走绑定
（`swcap_to_model`），两者混为一谈就会长出 `swcap_tool_call` 这种
「绑定伪装成能力」的东西（见下节）。

两个容易踩的判定：

* **`TOOL_CALL` 按「输出可执行动作」判定，不是按「走没走 function call」**。
  usb_hid 的 `AgentAction` 是 `@Generable` 结构化 schema（click/type/key/…），
  而 `tts_alert` / `video_search` 是 `@Callable` 调度 —— 两者都算。
  反过来，vtd 的 `TargetDoc` 虽然也是 `@Generable`，但产出的是**检测结果描述**
  不是可执行动作，所以只挂 `VISION_CHAT`：schema 属调用方业务
  （设计原则 6）。
* **枚举描述本机 RKNN 端口真实现的通路，不是上游权重的理论能力**。上游
  Qwen2.5-Omni 是全模态，但本机这份 RKNN 只导出了音频通路
  （`wdnai/backends/omni.py` 首行自述 `wav-in, text-out`，`run_images()`
  直接 `raise`），所以它只声明 `AUDIO_CHAT`。声明一个只有运行时才会炸的
  能力，比不声明更糟。

`wdnai` 侧还提供 `coerce()`（严格，只认当前名）、`migrate_legacy()`
（读老配置，认历史别名）、`LEGACY_ALIASES` / `RETIRED_CAPABILITIES`。
`RETIRED` 是**报错**而不是静默映射 —— 老迁移表里
`video_query` / `video_analyse` 迁到两个从未上线的能力名，把老配置变成了
永久的「功能未启用」且毫无线索，现在 boot 迁移会丢弃并告警。

> `tts_alert` 工具住在 `app/core/tools/tts_alert.py` 而不是音视频描述卡里：
> `core/tools/_builtins.py` 要 import 它，而 `_builtins` 同时被 detach 的
> `compute_worker` 子进程 import —— 它一旦指向可选插件，插件没装时该工具
> 会**静默消失**（那次 import 是故意软失败的，见 `_register_tts_alert`）。

`scene-threshold-tuner`(曾用名「阈值标定」,显示名「视频画面变更阈值测试」)
已在 v0.18 **整卡下线**:它的能力(帧间差异判定)不再需要单独一张卡 ——
`app/core/video/frame_judge.py` 把判据做成了 4 张视频卡共用的底层能力,
每张卡的视频源配置区直接调它,曲线也搬进了向导(见「帧判据」段)。
标定台只剩历史价值(它当初用来定下的默认值 6.0 / 长边 160 至今仍是
`FrameJudge` 的默认),代码已删除。

新增卡片示例(如加一个"图像 OCR"应用)有两条路：**内置卡** = `mkdir app/apps/ocr && 写 manifest.json + api.py + service.py + __init__.py + static/`；**插件卡** = 在某个插件包里加 `cards/ocr/`(同样一套文件)并在该包 `pyproject.toml` 的 `[project.entry-points."wdnwebui.cards"]` 注册 `"ocr" = "<包>.cards.ocr"` + `package-data`。两条路都**不动** `app/main.py` 或任何 core 模块。

### 图片生成卡（`media-generator`，插件卡形态的第一个实例）

插件包 `wdnapps_media_generator`（源码在 `.venv/src/wdnapps_media_generator/`）里的
一张卡：**输入一段文字 → 生成并展示 512×512 图片**。后端是 Qwen-Image-2.1。

| 项 | 值 |
|---|---|
| 卡片 id | `media-generator`（前端 JS 命名空间 `window.ImApp_media_generator`） |
| 能力 | `swcap_text_to_image` |
| 模型 | `qwenimage21`（`tools/worker_manifests/qwenimage21.json`） |
| 产物目录 | `<resource_dir>/media_generator/runs/<run_id>/img01.png`（默认 `/home/rockchip/assets/...`，在 NVMe 上） |

```
浏览器 ──POST /api/media-generator/generate──▶ Web UI 进程（插件 service.py）
                                                   │ DDS RPC run_prompt(prompt, output_dir=…)
                                                   ▼
                                    worker 进程（compute_worker.py --model qwenimage21）
                                                   │ cshim → TE(卡0) + DiT(卡1) + VAE(本机 3588 NPU)
                                                   ▼
                                    PNG 落盘（worker 自己写 ⇒ 与运行时基线逐字节一致）
```

模型**不在 Web UI 进程里跑** —— NPU 由算力注册中心统一分配，worker 崩了不影响 UI。

⭐ **VAE 跑在本机 RK3588 的 NPU 上**（rknn2 / RKNN2 runtime 2.5.0），
TE + DiT 仍在 2×RK1828 上（rk1820 / RKNN3）⇒ 两者**物理分开**
⇒ **DiT 全程常驻 ⇒ 生成过程零 model_init**。
拓扑**由模型包的文件名声明**（只有「2×1828+3588」和「3×1828」两种布局），
**没有独占设备就在 open 阶段 fail fast**，推理过程中**不卸载/不重装载**任何模型。
详见 §「图片生成卡 → VAE 走本机 rknn2 NPU」与 §「拓扑重构」。

#### 装配清单（少任何一条都表现为「卡片能开但不出图 / 出不了图」）

1. **插件包装上并注册**：`pip install -e .`（或装 wheel）⇒
   `[project.entry-points."wdnwebui.cards"]` 里有
   `"media-generator" = "wdnapps_media_generator.cards.media_generator"`。
2. **`package-data` 必须含 `manifest.json` + `static/*`**。漏了 `static/*` ⇒
   卡片**仍会被发现**（manifest 在包外层），但 `/static/apps/media-generator/`
   是空的 ⇒ 前端 404。**editable 装法看不出来，只有真 wheel 才暴露。**
3. **`app/static/index.html.template` 要有 `{% card_slot "media-generator" %}`**。
   没有占位符 ⇒ 卡片在应用列表里正常显示，**点开是空白页**。
4. **模型目录在 `models_root` 下**：`/mnt/oem/models/Qwen-Image-2.1/`
   （目录名 = **HF 仓库名**，与 `Qwen3-VL-4B` / `Qwen3-TTS` 等同规则；2026-11-05
   由 `qwenimage21` 改名而来）。
   本板 `/mnt/oem` 只有 558 MB 空闲（放不下 9.4 GB 的模型包）⇒ 用**软链**：
   `ln -s /home/rockchip/models/Qwen-Image-2.1 /mnt/oem/models/Qwen-Image-2.1`。
   ⭐ **目录名可以随便改**：发现规则是「**manifest 所在目录 = 模型目录**」
   （`workers/__init__.py::discover()` 扫模型根下每个子目录找
   `<model_id>.json`），SDK 通过 manifest 的 `model_dir_env`
   （`WDNAI_QWEN_IMAGE_21_DIR`）跟随 ⇒ 改名不需要改代码，只要同步 manifest 的
   `model_dir_name`。
5. **worker manifest 在模型目录里**：`/mnt/oem/models/Qwen-Image-2.1/qwenimage21.json`
   （出厂清单在 `tools/worker_manifests/qwenimage21.json`，用
   `scripts/migrate_worker_manifests.py` 拷过去）。⚠️ **manifest 的文件名 =
   model_id**（`qwenimage21`）⇒ 它同时是 worker 的 `--model qwenimage21`
   参数和 `/proc` 里的去重 needle ⇒ **改名会连带改 worker 参数与去重逻辑**，
   目录改名**不需要**动它。`requires_cards: 2` ——
   TE 常驻卡 0、DiT 在卡 1；**VAE 不占 1828 卡**（在本机 3588 的 NPU 上，见下）。

#### ⭐ VAE 走本机 rknn2 NPU（2026-11-04）

旧拓扑里 **VAE 和 DiT 同在 1828 卡上**，而两者**物理上不能共卡**
（VAE 4 段 internal 合计 1471.2 MiB/核 > 池 639.5 MiB）
⇒ 每批图跑完必须**释放 DiT**，下一批重新 init（**39 s**）。

改成：

```
卡0  0000:01:00.0 :  TE 常驻（486.0 MiB/核）+ 头（6.4 MiB/核）        [RKNN3/rk1820]
卡1  0003:31:00.0 :  DiT 4 段（--share-internal，525.0 MiB/核）常驻   [RKNN3/rk1820]
本机  RK3588 NPU   :  VAE 整图（rknn2，**纯 fp16**）常驻，3 核全用  [RKNN2/本地]
```

⇒ **DiT 全程常驻 ⇒ 生成过程零 model_init**（flow 每步都打「零 load / 零 model_init」）。

**cshim 侧**（`wdnai/_cshim/qi/{rknn2_runner,vae_rknn2}.{h,cpp}` +
`api.cpp::find_vae2_models`）：

* **dlopen** `librknnrt.so.2.5.0`（不是链接）⇒ 没装 rknn2 runtime 的板子
  **照常编译运行**（不会因为缺 so 而编不过）。
  ⚠️ 但**不再自动退回 rknn3 的共卡慢路径** —— 现在是 **open 阶段直接失败**
  并说清「本机 3588 的 NPU 跑不了这个 VAE；请检查 rknn2-runtime 是否装好」。
  要走 rknn3 就走**布局 B**（第三张 1828 卡 + `.seg{0..3}.1828.rknn`）。
* `core_mask=0_1_2` ⇒ **本机 3 个 NPU 核全用**（板端实测 1/2/3 核
  = 13490.7 / 10601.4 / 9872.4 ms ⇒ 3 核比 1 核快 **26.8%**，
  且三种核数下数值**逐位相同**）。
* 模型名：`vae_qwenimage21-512.3588.rknn`（整图，优先；后缀 = 跑在哪块 SoC，
  与 DiT/TE 的 `.1828.rknn` 同一套规则）。旧名 `vae_qwenimage21-512.rknn2.rknn`
  仍兼容；没有整图就找 `vae_qwenimage21-512.seg{0..7}.3588.rknn`（分段流式）。
* 整图装不下 ⇒ **必须返回失败**（分段流式在这里毫无意义：流式只是把 init
  推迟到解码那一刻，那一刻**同样装不下**）。
  ⚠️ 失败**不会**再「退回 rknn3」—— 布局是**模型包说了算**，不是运行时猜。

**量化侧**（在 119 转换服务器做，板端无法转换）：

* ⭐⭐⭐ **5D「通道族」张量让工具链把通道轴放大 16~32 倍** ——
  RKNN-Toolkit2 把 5D 按 NCHW 解释成 `(d0*d1, d2, d3, d4)`，
  于是 `[1,C,1,H,W]`（D=1）被解释成 `(C,1,H,W)` ⇒ C=1 ⇒ NC1HWC2 补到 16。
  修法是**换标签**：挪动的轴长度=1 ⇒ 扁平下标逐字节不变 ⇒ 数值逐位不变。
  ⇒ **internal 3852 → 1980 MiB（-48.6%）**。手术脚本 `tools/rknn2_vae/to4d.py`。
* **纯 w8a8 会彻底毁掉 VAE**（输出塌成平色，rel_L2 0.715）⇒ 必须
  `auto_hybrid`（阈值语义是「相似度」，越高 ⇒ 越多层留 fp16）。
  ⚠️ **换标定算法救不回来**：`normal` 0.715、`kl_divergence` 0.715（输出 std
  0.0140 vs 参考 0.5102 ⇒ 特征被压成常数）。
* ⭐⭐⭐ **手动混合量化（文档正路）全流程实测 ⇒ 3588 上 int8 不可得**
  （工具：`tools/rknn2_vae/hyb_{step1,cfg,step2}.py` + `hyb_run.sh`；
  流程 = User_Guide 6.3.2 的三步：`hybrid_quantization_step1` → 手改
  `<name>.quantization.cfg` 的 `custom_quantize_layers` → `hybrid_quantization_step2`）：

  | 留 fp16 的张量数 | 119 模拟器 rel_L2 | cosine |
  |---|---|---|
  | 0（纯 int8 对照） | 0.714497 | 0.734726 |
  | 2（最前面 2 个 conv） | 0.714207 | 0.735059 |
  | 12（逐层分析挑的最差 12 层） | 0.714455 | 0.734555 |
  | 36（**全部** exNorm/GroupNorm） | 0.714701 | 0.734399 |
  | 200 | 0.232147 | 0.974974 |
  | 300 | 0.102505 | 0.994799 |
  | **403（全部可标记张量）** | **0.000831** | **1.000008** |

  ⇒ **机制本身完全正常**（403 层 fp16 逐位复现纯 fp16），
  **但精度损失沿全图弥散累积** ⇒ 前端留 fp16 没用、**全部 GroupNorm 留 fp16
  也没用** ⇒ **3588 上不存在「可用的 int8 VAE 解码器」**。
  ⚠️ 顺带**证伪**了「量化毁在 GroupNorm 上」这条旧结论（单层误差最大确实是
  GroupNorm，但把它们全留回 fp16 对端到端精度毫无改善）。
* ⭐⭐ **平台硬限制：3588 上没有「int8 权重 + fp16 激活」档位** ——
  `quantized_dtype='w8a16'` / `'w4a16'` 均被拒（`not support in 'rk3588'`），
  而 `config(quantize_weight=True)`（配 `do_quantization=False`）**内部就是设
  `w8a16`** ⇒ 同样被拒。`w16a16i_dfp` 配置层放行但那是 int16 NPU 档、权重 16bit
  ⇒ 无体积收益。**唯一可用的量化档位就是 `w8a8`**。
* **阈值扫描（出货件 = 0.9999）**：

  | cos_thresh | internal | weight | 合计 | 模拟器 rel_L2 | cosine |
  |---|---|---|---|---|---|
  | 0.999 | 1825 MiB | 483 | 2308 MiB | 0.0351 | 0.99939 |
  | 0.9995 | 2095 MiB | 486 | 2581 MiB | 0.0343 | 0.99942 |
  | **0.9999** | **1825 MiB** | 487 | **2311 MiB** | **8.31e-4** | **1.000008** |

  ⇒ 0.9995→0.9999 有明显拐点，直接跳到 fp16 水平。
  **手术之后内存不再是约束 ⇒ 精度可以白拿**。
* ⚠️ **诚实说明：0.9999 出来的就是纯 fp16 图** ——
  逐算子表实测 **268/268 全 FLOAT16、零个 int8**（`auto_hybrid 0.9995` 才有
  8 个 int8 算子：输入 + 前两个 conv + 2 个 exNorm + 3 个转换节点，
  那 8 个就把精度从 8.3e-4 拉到 0.0343）。
  **别把它叫「int8 混合量化」** —— 它是 fp16 图。
  真正的全 int8 权重那份（纯 w8a8）只有 242~244 MiB，但精度塌陷。
* 板端实测（出货件）：init 1.88 s、解码 9.5~9.9 s（3 核）、
  rel_L2 **0.0178823** / cosine **0.99984105**（追平纯 fp16 的 0.0179853）。
* ⚠️ **板端 fp16 与模拟器 fp16 也差 1.79%**（NPU 的 fp16 kernel vs 模拟器用
  float32 模拟 fp16 的固有差）⇒ 读板端精度数字必须带这个基线。
* ⭐ **「3 核只给 1.37x」的原因**（`tools/rknn2_vae/op_table.py` 量出来的）：
  核数标度拟合 `t(k)=A/k+B` ⇒ A = 5779 ms（可并行 **42.8%**）、
  B = 7712 ms（固定 **57.2%**）。手术把 CPU 回落算子从 **20 个砍到 12 个**
  （周期 3.006 M → 0.423 M，**-86%**；占全图 17.6% → 5.0%）⇒ 手术是**净收益**。
  剩下 57% 的固定开销主要是 **NPU 算子间的串行依赖** + CPU 回落前后的搬运停顿。
  ⇒ **int8 不比 fp16 快**（9872 vs 9548 ms）—— 瓶颈不在 NPU 算力。


⇒ 完整构建/验收流水线在 **`tools/rknn2_vae/`**（含 README 与踩坑清单）。

**验收（2026-11-04，全部板端实机）**：webui app 路径
`POST /api/media-generator/generate` —— **8 步 33.61 s / 20 步 69.32 s /
28 步 93.04 s / 40 步 128.91 s**（40 步 = 官方 Qwen-Image-2.1 README 三个示例
统一使用的推荐值，2026-11-05 加入 `VALID_STEPS`），进度流（flow 1→N → vae →
save）完整；
`fixed_noise=true` 同提示词两次独立请求 ⇒ PNG **逐字节相同**（md5 `2e9251f0…`）
⇒ 可当回归基线用。两张图之间 VAE 段 9481 → 9747 ms ⇒ **零 model_init**。

**⚠️ 旧的「rknn3 兜底路径」已删除**（2026-11-05 拓扑重构）：把 rknn2 的 VAE
挪走**不再**退回 rknn3 的共卡慢路径，而是 **open 阶段 fail fast**：

```
LLMSession init failed for qwenimage21 @ npu=0: RuntimeError: wdnai cshim 打开会话失败：
VAE 没有可独占的 NPU 设备：模型包里既没有 vae_qwenimage21-512.3588.rknn（本机 3588 跑 rknn2），
也没给第三张 1828 卡（跑 rknn3 的 4 段 VAE）。**不再退回「与 DiT 共卡」** —— 那会让每批图多 39 s 的 DiT 重建。
```

⇒ 算力页上该实例 `capability_ready=false`，前端按钮禁用。
板端实测（藏起 `.3588.rknn`）：**4 s 内失败、零模型加载日志**。

**⭐ 跨路径交叉验证**：同提示词同潜变量，rknn2 路径（3588/auto_hybrid/整图）
与 rknn3 路径（1828/w4a16/4 段）端到端像素差 **mean 1.315/255、p99 = 5**
⇒ 两条**完全独立**的量化路径输出实质一致 ⇒ 对 rknn2 路径数值正确性的
**独立佐证**（不是自证）。

**⭐ 手术在速度上的收益**（不只是内存）：旧图 5D w8a8 vs 新图 C2，
1 核 18519.6 → 13490.7 ms（**-27%**）、3 核 15923.3 → 9872.4 ms（**-38%**）；
核数标度拟合的固定开销 B 从 15739 ms **砍半**到 7712 ms，可并行部分
A 从 2781 ms **翻倍**到 5779 ms。

⇒ **这同时否掉了「宿主侧做像素 shuffle」**：剩下的 12 个 CPU 回落算子只占
成本模型 **5%** ⇒ 上限被框死；而代价是「图切 5 段 ⇒ 常驻显存按段数膨胀
（旧图实测 4 段 = 整图的 2.20×）⇒ 装不进 4080 MiB ⇒ 退化成流式
⇒ 每张图多 5 次 init」⇒ **净收益大概率为负**，不值得动已验证的链路。



0. ⭐⭐⭐ **「点了没反应」= 三个反馈空窗叠在一起**（2026-11-04 用户反馈：
   「点了生成图片就一直卡在那，进度条要非常久之后才出来，也没提示在做什么」）。
   查下来后端进度其实是**及时的**（VAE 每段都推），空窗全在前端：
   1. worker 的第一条 `qi|` 事件要等它真开始干活才到（最快 1~4 s）⇒ 这之前
      快照里 `progress` 是 `None` ⇒ **前端把进度条整个 `hidden` 了**
      （`renderProgress(null)`）⇒ 用户看着像「点了没反应」；
   2. `doGenerate()` 里 `await postJSON()` 之后**直接返回** ⇒ 界面一动不动，
      要等下一次 WS 帧（最快 1 s）或兜底轮询（最慢 5 s）才知道「点了」
      ⇒ 修：点击即刻禁用按钮 + 变「提交中…」+ 进度条亮起来，POST 成功后
      **立刻** `pollSnapshot()`；
   3. 向导模式里自动出图时**卡片页是隐藏的** ⇒ 向导界面自己又没有进度显示
      ⇒ 修：向导完成后明确提示「已开始生成，请到「图片生成」页看实时进度」。
   ⇒ 后端也补了一条：`start()` 立刻塞 `stage="start"` 事件（带总步数），
   让「正在跑」这件事**零延迟**可见。
   ⇒ 复现/回归脚本：`scripts/probe_click_timeline.py`（headless chromium
   点一次，每 100 ms 抓 DOM + 记 WS 帧时刻）。实测 **t = 0.22 s** 就有反馈。
1. ⭐⭐⭐ **出图产物过不去 RPC**：`wdndds` 的 `encode_attachments_out` 只认
   `item.data`（**音频专有属性**），于是 `ImageAttachment` 被整条丢掉 ——
   reply 的 `attachments_out` 是空的，调用方只拿到一句 prompt 回显，
   **看不出「图丢了」**。⇒ wdndds 侧补了图像分支（`/dev/shm` 原样搬 BGR
   ndarray + shape，回程还原）。**任何「产物型」附件都要在这里显式列出。**
2. ⭐⭐⭐ **第一次出图必然超时**：cshim 是**懒加载**的 ⇒ 第一次推理 = 90 s init
   + 34 s 生成 = 124 s > webui 的 **120 s RPC 推理超时**（`proxy._INFER_TIMEOUT_MS`）。
   ⇒ wdnai 侧给 backend 加了可选的 `warmup()` 钩子，`LLMLocalSession` 构造完
   就调 ⇒ init 挪到 worker 的 **load** 阶段（UI 显示「加载中」），推理只剩 34 s。
3. ⭐ **模型包不能直接搬进 `/mnt/oem`**：emmc 只剩 558 MB。软链既满足
   「manifest 所在目录 = 模型目录」的发现规则，又不占 emmc。
4. ⭐⭐⭐ **一次推理的预算是「每模型」的，不能是全局常量**：
   worker 侧原来写死 `_INFER_TIMEOUT_SEC = 60.0`（`app/core/compute/workers/_base.py`），
   客户端写死 `proxy._INFER_TIMEOUT_MS = 120_000` ⇒ **20/28 步出图（实测
   103 s / 124 s）必然被砍掉**，报
   `TimeoutError: inference exceeded 60.0s`。
   ⇒ 改成 **manifest 声明 `infer_timeout_s`**（文生图 = 300 s）：
   - worker：`manifest_infer_timeout_s(model_id)` → 传给 `serve_local_session`
     的 `infer_timeout_s`（这是 replier pump 的上限，超了就回一条带原因的
     错误，而不是把 pump 焊死）；
   - 客户端：`proxy._infer_timeout_ms_for(model_id)` = **worker 预算 + 30 s
     margin**（客户端必须比 worker **活得久**，否则先超时的是客户端 ⇒ 报错
     没有原因可查）；
   - 两边读**同一个 manifest 文件** ⇒ 预算不会漂移。
   ⇒ 验证脚本：`scripts/test_infer_timeout.py`（不碰 NPU）。
   ⛔ 新增 manifest 字段时记得同步 `workers/__init__.py` 的 `known` 集合
   （决定哪些键会掉进 `extra`）。

#### ⭐⭐ 拓扑重构：两种固定布局，**推理过程中不卸载/不重装载**（2026-11-05）

**旧行为**（2026-11-04 之前）：模型包里有 rknn2 的 VAE 就走 3588，没有就**自动退回**
rknn3 的 4 段 VAE 链 —— 而 rknn3 的 VAE 与 DiT **物理上不能共卡**
（VAE 4 段 internal 合计 1471.2 MiB/核 > 池 639.5 MiB）⇒ 每批图跑完**释放 DiT**、
下一批重新 init（**+39 s**），进度流里会看到 `release → vae_init → vae 1/4..4/4`。
「运行时调度」= 每次推理都得判断当前哪块卡空着。

**现在**：拓扑由**模型包里的文件名**声明，**只有两种合法布局**，
全部模型在 `open` 阶段**一次装好并全程常驻**：

| 布局 | 模型包里的 VAE 文件 | VAE 跑在哪 | DiT 跑在哪 |
|---|---|---|---|
| **A：2×1828 + 3588** | `vae_qwenimage21-512.3588.rknn`（rknn2 整图） | **本机 RK3588 的 NPU**（3 核全用） | 卡1（1828） |
| **B：3×1828** | `vae_qwenimage21-512.seg{0..3}.1828.rknn`（rknn3 4 段） | **第三张 1828 卡**（独占） | 卡1（1828） |

* 布局 A 由 `api.cpp::find_vae2_models()` 命中 `.3588.rknn` 决定；
  布局 B 由 `card_vae`（第三张卡 BDF）非空 + 4 个 `.seg{0..3}.1828.rknn` 都在决定。
* **两者都没有 ⇒ `open` 直接失败**（不再退回共卡慢路径），错误信息是中文的、
  说清楚缺什么（见上面「旧的 rknn3 兜底路径已删除」）。
* `generate_batch` 里**删掉了** `s->dit.release()` 与「释放后重建 DiT」的分支，
  改成**常驻断言**：`dit_ready` / (`vae2_ready` || `vae_ready`) 为假 ⇒ 直接报错，
  **拒绝推理中重建模型**。
* C 入口从 `qi_qwen_open_ex2` 扩成 **`qi_qwen_open_ex3`**（多一个 `card_vae`）；
  `ex2` 保留成 wrapper（`card_vae=""` ⇒ 只能走布局 A）。

**fail-fast 的检查必须放在 `open` 的最前面**（`api.cpp` 的「0.」段）：
VAE 的可用性只取决于「模型包里有没有那个文件」⇒ 几个 `access()` 就够。
⚠️ 若把检查放在 TE/DiT 加载**之后**，用户要**白等 2 分钟**（TE 37 s + DiT 60 s）
才知道开不了工 —— 板端实测：检查前 **125 s** 才失败，检查后 **4 s** 失败。

**卡分配按运行时枚举**（`backends/image.py::_ep_card_ids()`）：
用 `RKNN3Lite().get_devices_id()` 枚举本机 EP 协卡，只留 **PCI BDF 形态**
（`^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.\d+$`；USB 序列号形态的 RGA/VPU 设备
**跑不了 rknn 模型**），按 BDF 升序：`[0]` = TE + 头、`[1]` = DiT 4 段、
`[2]` = VAE(rknn3) 独占第三张卡。
⇒ **3 卡板子不用改代码**。枚举不到 ⇒ **直接报错**（没有可兜底的卡号 ——
拿别的板子的 BDF 去初始化只会得到「device id not found」）。
本板实测枚举 = `['0001:11:00.0','0003:31:00.0']`。

**验收（2026-11-05，板端实机，重构后）**：
连续两张 8 步图 **各 36.57 s**（cshim 内 33.5 s）、**真实 `model_init` 次数 = 0**
（worker 日志里 28 处 "model_init" 全是「零 load / 零 model_init」横幅里的字样）；
20 步 70.07 s。PNG md5 `240286b5a7a494f01e7231e66ef265a2` —— 与**重构前**的
历史基线**逐字节相同** ⇒ 拓扑重构没有改变数值行为。

#### ⚠️ ctypes 的 `restype` 必须显式声明（踩过的坑）

新增 C 入口 `qi_qwen_open_ex3` 时**忘了**在 `_qwen_image_c.py` 里写
`lib.qi_qwen_open_ex3.restype = ctypes.c_void_p` ⇒ ctypes 默认 `restype=c_int`
⇒ **64 位会话指针被截断成 32 位**。

症状极有欺骗性：

* `open` 阶段**看起来完全正常**（C 侧把模型全加载完、打印
  「VAE 走本机 rknn2 ⇒ DiT 全程常驻」、worker 报 ready）；
* 一直到**第一次推理**才 **SIGSEGV**（`/proc/<pid>/stat` 的 exit_code = 11）
  —— 因为 `generate_batch` 拿到的是**截断过的指针**，一解引用就炸；
* worker 变成僵尸（`<defunct>`），**日志里一行新东西都没有**（见下一节）。

⇒ **凡是通过 ctypes 调 C 入口，一律显式写 `restype` + `argtypes`**；
新增 C 符号后第一件事是 `nm -D --defined-only lib*.so | grep <新符号>` 确认导出，
再核对 ctypes 声明的**参数个数与顺序**。

#### ⚠️ worker 日志的 stdout 缓冲会把崩溃现场吃掉

worker 的 stdout/stderr 重定向到 `/tmp/wdnwebui-worker/<model>.log`（v0.8.6 起，
为了避免 parent 死掉时 worker 收到 SIGPIPE）。但 **stdout 重定向到文件时默认是
全缓冲（4~8 KB）** ⇒ 进程被 SIGSEGV / SIGKILL 时**缓冲区里的日志全部丢失**
⇒ 现场看起来「worker 莫名其妙就死了，没有任何线索」。

修法：`api.cpp` 里加一个静态初始化对象，把 stdout 改成**行缓冲**：

```cpp
struct StdoutLineBuffer {
  StdoutLineBuffer() { setvbuf(stdout, nullptr, _IOLBF, 0); }
} g_stdout_line_buffer;
```

⇒ 每行 `printf` 立刻落盘，**崩溃点前最后一行就是断点**。

#### 进度条（每一步在做什么 / 每一步多久 / 还需多久）

出图是重操作（首次 load ~90 s、每张 8 步 ~34 s），只给一个黑转圈等于没有
反馈 ⇒ 这张卡有**实时进度条**。链路（每一环都已打通）：

```
cshim(C)  qi_progress_cb  ──►  backend.sink.emit("qi|stage=flow|step=3|…")
   ▲ on_delta（worker 侧）      │
   │                            ▼
   └── DDS delta 通道 ◄── 卡片 service.py 的 parse_progress → ProgressTracker
                                        │  快照的 progress 字段
                                        ▼
                                 前端进度条（阶段 / 步序 / 本步耗时 / ETA）
```

* **C 侧**（wdnai `_cshim/qi/progress.h`）：阶段 = init / te / flow / vae /
  vae_init / release / save / done；flow 每跑完一步报一次。
* **口径**：`elapsed_ms` = **距上一次计时基准**的毫秒数；换阶段时用
  `qi_progress_stage()` 重置基准 ⇒ 「这一步花了多久」是真的
  （不重置的话，新请求的第一条会把「worker 空等了多久」当成耗时）。
* **⚠️ 全局只有一个时钟**：`qi_progress_ctx` 由会话持有、各层共享指针 ——
  每层各自计时的版本会让跨层事件（release 报 21 s）对不上。
* **卡片侧**：`service.py` 的 `ProgressTracker` 把事件折成
  `{stage, step, total, percent, step_ms, step_avg_ms, eta, detail}`；
  `on_delta` 回调跑在 **DDS 读线程**上 ⇒ 只改状态、**不**在里面广播
  （前端靠 WS 的 1 秒节拍取）。
* **ETA** = 本阶段剩余步数 × 本阶段平均每步 + 后续阶段的固定预估值。

#### ⚠️ 关停挂死（2026-11-04 定位，宿主侧兜底）

浏览器会**同时**挂着 8 张卡的 WS。只要有一条 WS 的服务端协程不返回，
uvicorn 的优雅关停就**永久挂死**（日志停在 "Waiting for background
tasks to complete"，端口 8000 起不来）。两个已知触发条件：

* 只在「状态变化时」才发帧 ⇒ 客户端断开后它**永远发现不了**
  （7 张卡都是这个写法；图片生成卡已改成「读一次 + send 超时 + 心跳」）。
* 对端「消失但不干净关闭」⇒ 服务端 send 卡在写缓冲上
  （socket 停在 CLOSE-WAIT 且还有数据没发出去）⇒ 协程不返回。

⇒ **宿主侧必须兜一个上限**：`run.py` 里给 uvicorn 传
`timeout_graceful_shutdown=10`（默认 None = 无限等）⇒ 超时后 uvicorn
取消这些任务继续关停（日志会打一行 "Cancel N running task(s)"）。
**任何新增的 WS 卡片都该自己处理断开**，但宿主这个上限不能省。

## AI 电脑控制（usb-hid 的任务循环）

`usb-hid` 卡从 v0.25 起不只是「手动把点击/按键透传给 PC」，还多了一条
**自然语言 → AI 自主操作 PC** 的回路（`app/apps/usb_hid/agent.py`）：

```
截屏(HDMI-IN) → prompt(任务 + 已执行动作历史) → VLM 输出一个动作
              → 0-1000 → 帧像素 → PC 虚拟桌面像素 → 写 HID
              → 等 PC 反应(1.2s) → 下一轮，直到 done / 停止 / 步数上限
```

### 踩过的坑：HDMI-IN 的 `v4l2` 依赖装不上就整条链 503

`wdnav/src/wdnav/v4l2.py` 在**模块级**写死 `import v4l2 as _v4l2`（第三方
PyPI 包，纯 ctypes 绑定，**不是**标准库）。`wdnav` 的 pyproject 里声明了
`v4l2>=0.2`，但 SDK 一律用 `pip install --no-deps -e` 装 —— **--no-deps
直接跳过依赖解析**，这个包永远不会自己进 venv。缺它时：

```
[ERROR] core.hdmiin.capture: wdnav.v4l2 import failed: No module named 'v4l2'
GET /api/usb-hid/source/hdmiin/snapshot → 503
```

页面轮询多快，错误日志就刷多快（每 30ms 一条）。修法是把它显式写进
`requirements.txt`（`v4l2>=0.2`），和 `transformers` / `tokenizers` 同一类
「SDK 声明了但 --no-deps 装不进来、只能由 wdnwebui 兜」的依赖。

> ⚠️ **装上 ≠ 能用**：上游 0.2（2014）留着 Python 2 惯用法
> `) = range(1, 9) + [0x80]`（`v4l2.py:197`）和
> `) = range(0, 4) + [2]`（`v4l2.py:248`），py3 下 **import 阶段**就抛
> `TypeError: unsupported operand type(s) for +: 'range' and 'list'`。
> 关键点：那是 **TypeError 不是 ImportError** —— 原来
> `capture.py` 只 catch `ImportError` 的分支接不住，故障会从「干净的 503」
> 退化成「未捕获的 500 堆栈」。修法是每行包一层 `list()`，由
> `scripts/fix_v4l2_py3.py` 幂等完成（`update_rkwhl.sh` /
> `bootstrap_wdndds_native.sh` 已自动调用）。
>
> 现在 `app/core/hdmiin/__init__.py::load_v4l2_capture()` 统一装载并把
> 两种失败都变成带「怎么办」的可执行提示，`capture.py` / `probe.py` 都走
> 它 —— **别再各自 `from wdnav.v4l2 import V4L2Capture`**，那样漏 catch
> 又会退回 500。

> 📌 **第二条链：驱动只认一种格式 → S_FMT 直接 EINVAL**（已修，见下）
>
> 依赖装好后，打开设备仍可能 `OSError: [Errno 22] Invalid argument`。
> 本机 rk_hdmirx 只接受 `type=CAPTURE_MPLANE + 'NV24'`，bgr3 / nv12 /
> rgb3 / yuyv / uyvy 一律被拒。
>
> **wdnav 侧已补 G_FMT 兜底**（`_negotiate_format()`）：S_FMT 失败 →
> `VIDIOC_G_FMT` 问驱动当前格式 → 用它重试一次。所以 app 侧请求什么都能
> 打开，最终格式看 `cap.pixel_format`（**协商结果**，不是请求值）。
> 改这个之前，`app/core/hdmiin/__init__.py` 文档里写的
> 「G_FMT-then-S_FMT fallback added in v0.5」是**假的**（`git log -S
> "VIDIOC_G_FMT"` 显示从未实现），别再照着它假设。
>
> ⚠️ 驱动报的四cc**可能是撒谎的**：rk_hdmirx 报 'NV24'（V4L2 规范里是
> 12bit YUV420SP），实际写的是 **8bit YUV444**（Y 平面 W*H + **全宽**交错
> CbCr 平面 2*W*H，合计正好 sizeimage=3*W*H，**没有空洞**）。按 fourcc
> 猜布局会读成满屏竖条纹。**布局一律问 `wdnav.v4l2.frame_layout`**（它认
> 这个驱动的 quirk），别在 wdnwebui 里自己发 ioctl 绕过去。
>
> > 早先这里记的是「8bit YUV422SP + 尾部 W*H 空洞」，是**误判**。实测
> > （HDMI-RX 回环本板 HDMI-OUT，X 抓屏当地面真相）：把缓冲区按
> > (H,W,2) 拆成交错 Cb/Cr，与真值逐像素算相关性 **0.988 / 0.998**；
> > 按 4:2:2 读只有 **0.07**；Y 平面与真值亮度相关性 **0.997**。驱动
> > 自己的 `v4l2-ctl --list-formats-ext` 也把 NV24 写成
> > "'NV24' (Y/UV 4:4:4)"。
>
> 另有一条 librga 的坑（本机 1.10.5 legacy API）：**YUV 源的缩放是坏的**
> —— 亮度对（MAD 0.07）色度完全错位（MAD 57）；BGR 源的缩放是对的
> （MAD 1.7）。所以 `capture.py::_frame_bgr()` 把 YUV 源统一转成 BGR888
> 再走 BGR 那条缩放路径，三段全在 RGA 硬件里跑。

### 踩过的坑：HDMI-IN 画面「颜色不对」和「看着发糊」

同一个症状（UI 上显示的画面不对）底下压着**三个独立的错**，修一个不见好，
必须一起修。以 HDMI-RX 回环本板 HDMI-OUT + X 抓屏当地面真相来量化：

| # | 错 | 判据 | 修法 |
|---|---|---|---|
| 1 | 驱动给的是 **8bit YUV444**，代码按 4:2:2 读 | 色度按 (H,W,2) 拆与真值相关 0.988，按 4:2:2 读只有 0.07 | `wdnav.v4l2` 把 `rk_hdmirx`/NV24 的 payload 改成 `yuv444sp`；app 侧新增 `yuv444sp` 分支 |
| 2 | 色彩空间没声明，RGA 默认按 BT.601 解 | 驱动自报 `xvYCC 709` + `Limited Range`；默认 BT.601 时 MAE 4.16，声明 709 后 2.39 | `wdnav.rga` 加 per-buffer `color_space_mode`（`wrap_virtual(color_space_mode=…)`），app 侧声明 `IM_YUV_BT709_LIMIT_RANGE` |
| 3 | RGA 产出**限幅** YUV，JPEG 按规范**永远按全范围解码** | 理论偏差 R+12.2/G+4.3/B+1.85，实测 R+11.4/G+4.5/B+2.0 | `_encode_jpeg` 的 RGA 目标 buffer 声明 `IM_YUV_BT601_FULL_RANGE`（**不是 709** —— 浏览器解 JPEG 只用 601） |

> **错 #3 特别容易误判成「MPP 编码器有问题」**：MPP 侧实测 Y 透传
> 1.0001×、色度 corr 0.98/0.99，编码器完全忠实；偏差全在**喂进去的
> 范围**上。cshim 的 `prep:range = 2 /* full range */` 本身没错 —— 错
> 的是喂给它的数据是限幅的，数据侧改成全范围后两者就自洽了，**cshim
> 不用改**。

> **「限幅/全范围」只会错一次**：RGA 默认给限幅，而 JPEG 没有可被浏览器
> 尊重的范围标记。若哪天改成喂**视频流**给 VLM，务必分清那条链上
> 到底是谁在解。

**「发糊」是另一回事，与颜色无关**：预览尺寸被硬编码成 1280×720，而源是
1920×1080，前端 `object-fit: contain` 铺满一个**比 1280 更宽**的显示区 →
浏览器把 720p **放大 1.4~1.5 倍**。以真值为准的观感锐度：720p 在显示宽
1400/1600/1800/1920 时只剩 82.6% / 77.4% / 74.4% / **56.7%**；原生 1080p
是 108.8% / 104.5% / 103.3% / **101.6%**。**窗口越宽，720p 越糊**。

> JPEG 质量不是主因（q=80 → 94.9%、q=95 → 95.0%）；4:2:0 色度是架构
> 决定的（MPP cshim 只接受 4:2:0 喂入，JPEG 必然 4:2:0）。
>
> 原生分辨率**并不贵**：1080p 走 RGA `cvtcolor`（3.96 ms）而不是
> `resize`，MPP 编码 1080p（5.7 ms）反而比 720p（7.3 ms）快。整链路
> 5.9 ms/帧，30 fps 只占 2% 单核；字节 42 → 82 KB（局域网无感）。
> 同一环境 A/B（只改环境变量）：720p 采集线程 56.1% / 原生 59.5%。
>
> 预览尺寸可用 `WDNWEBUI_HDMIIN_PREVIEW_SIZE=1280x720` 覆盖，写错会
> warning 并回落原生。

**两个自己踩的坑，别再犯**：

* **别用三维花式赋值做色度抽点**。4:4:4 → 4:2:2 写成
  `out.reshape(h, w//2, 2)[:] = src.reshape(h, w, 2)[:, ::2, :]` 时 numpy
  走逐元素 gather，**9.97 ms/帧**（30 fps = 30% 单核）。把色度看成
  **uint16**（一个 CbCr 像素正好 2 字节）再取步长 2，numpy 走按行
  memcpy，**0.57 ms/帧**，快 17 倍且字节完全一致。
* **判断「预览卡了」别用「快照内容 md5 有几种」**。桌面画面是**静止**的
  时，每帧像素完全相同，md5 只有一种 —— 那不是卡，是没变化。要看
  帧率得看 `HdmiInCapture._captured` 计数。

### 为什么不单独开一个 swcap（历史包袱已拆除）

原来这里有个 `swcap_computer_control`，理由写的是「`get_session` 命中多实例时按
「本机优先 + 卡号升序」取第一个（`registry.py::get_session`），而
`swcap_video_describe` 同时被 qwen3-vl-4b 和 qwen3.5-vl-4b 声明 —— 电脑控制
拿到哪个模型取决于卡号排列，用户不可控」。

看清楚就知道：**那是绑定问题，被伪装成了能力。** 能力回答「模型能做什么」，
选哪个模型回答「绑定」；混在一起时，想锁 provider 就只能另起一个业务能力名，
而那个名字立刻开始承担它不该承担的事（一个新业务想用同款能力就发现名字不对）。

现在能力与绑定解耦：

* 能力如实声明 —— usb_hid 需要 `swcap_vision_chat`（截图要视觉）+
  `swcap_tool_call`（动作要可执行输出），两项都在 `wizard.json` 的
  `swcap_to_model` 条目上带 `"group": "agent"`；
* 「必须同一个模型」由**组绑定**保证 —— 组感知的三处判定
  （`registry.set_active_app` 按组校验、`list_running_by_capabilities` 的 AND
  查询、前端 `_planSingleCard` **按组数**而非能力数判单卡）都不再出现
  「两个实例各满足一半」被算成就绪的情况。

不新开 worker manifest：电脑控制要的也是「吃一张图 + 输出动作」，与
qwen3.5-vl-4b 能力同构。

> `config.jsonc` 已在 `compute.autostart` 里加上 `qwen3.5-vl-4b`（npu0），
> 但该文件是 **.gitignore 的**（per-user 配置），换机器要手动加一行。

### worker 端一行语义都不能有

action 的 schema、0-1000→帧像素→PC 像素的两次换算、键名解析、所有 HID 副作用
**全部在主进程**（设计原则 6）。除了「worker 是 detach 进程不能 reload」这个
老理由，还因为同一份 RKNN 会挂多个 capability，action 语义必须跟着**调用方**
走。相应地**不要调 `sess.reset()`**：qwen3.5-vl-4b 这个实例同时挂着
`swcap_text_chat`（微信对话）和 `swcap_tool_call`，每轮 prompt 都是
自包含的不依赖 worker 侧会话历史，reset 只会把并发的微信对话历史一起清掉。

### `@Generable` schema 的两条老规矩（沿用 TargetDoc）

1. 继承 pydantic `BaseModel`，**不继承 `LMOutput`** —— 后者带
   `attachment: List[Attachment]` 字段，`model_json_schema()` 会抛
   `PydanticInvalidForJsonSchema`，SDK catch 后返回**空串**，模型看不到任何
   字段说明。
2. **不能有 class docstring** —— pydantic 会把它渲染进 schema 顶层
   `description`，模型就把真字段整个包进 `{"description": {...}}`。

另外 `action` 用 `Literal[...]` 而不是自由字符串：enum 原样进 JSON schema，
4B 模型在 enum 上的服从率远高于"自己挑一个动词"。

### 踩过的坑：`np.ascontiguousarray` 不复制（症状：截图在推理中途自己变了）

`HdmiInCapture._maybe_snapshot_bgr` 要把 V4L2 的 mmap 帧拷一份给 agent。
最初写成 `np.ascontiguousarray(plane)` —— 它文档里明写
"**no copy is performed if the array is already C-contiguous**"，而
`plane` 是 mmap 的 flat view，`reshape(h, w, 3)` 之后**本来就是 C-contiguous**，
于是它原样返回同一个 view。`cap.requeue(buf)` 之后驱动随时可以覆写这块内存，
agent 于是拿着**推理过程中被改写的帧**去算坐标 —— 不崩溃、不报错，只是点击
位置莫名其妙漂几像素。

**必须写 `plane.copy()`。** 单测里唯一能抓住它的断言是
`np.shares_memory(got, mmap)` 必须为 False：形状/类型/内容全对，只有这个能证
明"拷了"。

### 坐标三次空间

模型看到的是 `letterbox_resize_with_meta` 产出的**方形画布（含灰边）**，输出
0–1000 —— 与 TargetDoc 契约完全一致：

```
0-1000(方画布) --unletterbox_point--> 帧像素 --×host_w/frame_w--> PC 虚拟桌面像素 --chardev--> 0..32767
```

第 2 步**按实测帧尺寸算，不写死 ×1920/1000**：截图 1920×1080、PC 也 1920×1080
时两者恰好相等，但 PC 换分辨率后写死的系数会让每次点击系统性偏移。
实测 1080p 全图往返误差 **0 px**（`scripts/` 外的自测脚本已覆盖）。

### 防跑飞三道闸

真实 PC 上一个 4B 模型会卡在同一个按钮上反复点：

* 步数上限（默认 12，硬上限 60）；
* 连续 3 次**完全相同**的动作（action + 坐标 + 文本都一致）→ 判卡死中止，
  并把**中止的那一步**也记进 step log（用户能看到是哪一步断的）；
* `stop()` / `register_shutdown()` / 循环 `finally` 都调
  `keyboard.release()` + `digitizer.up()`，保证宿主机不会看到一根卡住的键
  或一根停在半空的触点。

### 踩过的坑：AI 的点击打在主屏，网页手动点击打在副屏

坐标换算两边**已经**一致（见上文"坐标三次空间"），但同一次点击写进 HID 的
**字节不一样** —— 差异只在 release 帧：

```
网页:  down -> 010100400040   (count=1 tip=1 X=16384 Y=16384)
       up   -> 000000000000   (count=0 tip=0 X=0     Y=0    )
AI:    down -> 010100400040   同上
       up   -> 000000400040   (count=0 tip=0 X=16384 Y=16384)  ← 只有这里不同
```

AI 当时用的是 `DigitizerWriter.click()`，它**保留 release 帧的 X/Y**
（[service.py:271](/home/rockchip/wdnwebui/app/apps/usb_hid/service.py)），
而网页路径走 `down()` + `up()`，`up()` 发的是 0×0。

`up()` 的 docstring 早就写明为什么要这样：

> Sends 0x0 so the host sees "pen left the tablet" instead of "**pen
> teleported to origin**" — a teleport **spuriously fires a click on
> whichever screen element happens to be at (0, 0) on the host**.

也就是说 **release 帧是宿主要按位置处理的一份报告**。抬手时再报一遍
真实坐标，宿主就按那个点去路由 —— 双屏下自然落到另一块屏。

→ AI 现在走 `_tap()` = `down()` + `sleep` + `up()`，与网页路径
**逐字节相同**（回归里直接比对 byte 序列，不只是比对坐标）。

**教训**：这条 bug 的症状是"打到另一块屏"，很容易被误判成坐标算错，
于是去查 letterbox / 坐标系 —— 而坐标系当时是对的。**多屏环境下"落点
不对"要先把两条路径的字节 dump 出来比**，坐标对了就往 HID 报告内容上
想。`click()` 这种"看起来等价"的便利封装，正是这类问题的来源。

### 动作集里没有 `move`（只移动不点击）

`DigitizerWriter.move(tip_down=False)` 的 docstring 写着 "Win PT drivers
silently discard this" —— 宿主**不认悬停帧**。保留它只有一个后果：白白
烧掉一步推理，模型还以为鼠标动了。网页路径也不发悬停帧（没有按键时
`movePress` 直接 return）。触屏数字板表达不了"移动但不按"，所以动作集
里**删掉**了 `move`，要移动就用 `drag`。

### 踩过的坑：`click` 动作点了 (0,0)，看起来"点了但没反应"

实测：输「打开记事本」，第 1 步就点在 host `(0, 0)`。`(0,0)` 在 Windows
桌面上是屏幕左上角，那里**什么都没有**，所以表现就是"点了没反应"，
而步日志里 click 的 status 恰恰是 `ok`。

成因不是一个而是**两个**，而且第二个一直在掩盖第一个：

**① schema 让"没有坐标的 click"合法。** 原来的
``AgentAction`` 是 `required: ["action"]`、`x/y` 带 `default=0.0`，于是模型
吐一个 `{"action":"click","thought":"点开始菜单"}` 完全通过校验，pydantic
**静默补 0**。→ 现在 `x` / `y` 必填，且 JSON 恢复路径
（`_normalize_action`）对"缺失"和"0"也必须分开判
（`_as_float(v, default=None)`，默认 None 而不是 0）。

> 注意 `_as_float` / `_as_int` 的 default 只能是 **None**。0 本身是合法
> 坐标，一旦把缺省写成 0，"没说"和"说了 0"就再也分不开 —— 而这正是
> 下面 `(0,0)` 三条来路无法区分的根源。

**② 从来没告诉模型它看的是带灰边的正方形。** 1920×1080 letterbox 进
768×768 的 vision 变体后，**只有 y∈[219, 781] 这一段是真实画面**（上下各
168 行灰边）。不说明的话模型就有两个同样说得通的"0-1000"：

| 模型给的 y（按"这就是块屏幕"理解） | 本意 host y | 按方图理解的 host y | 误差 |
|---|---|---|---|
| 100 | 108 | 0 | -108 |
| 250 | 270 | 60 | **-210** |
| 500 | 500 | 540 | 0 |
| 750 | 810 | 1020 | **+210** |

顶部/底部因为后面会 clamp 看起来"还挺准"，把中部的错误完全盖住 ——
症状是"有时候点得准有时候差很远"，几乎无法归因。
→ 现在 `_geometry_block(meta)` 从当帧的 meta **现算**这条 band 并写进
prompt（换 384 变体时数字自动跟着变，不写死）。

### `(0, 0)` 的三条来路必须一起堵

它们 clamp 到同一个像素，步日志里长得一模一样：

1. 模型漏了 x/y，被 schema 补成 0 → **已堵**（x/y 必填）
2. 模型字面输出 0,0 → **已堵**（`_is_degenerate_origin`，≤8px 判为退化）
3. 模型按桌面坐标系把"灰色留边顶部"当成 y=0 → **已堵**（prompt 写明 band）

代价：真要点屏幕左上角 8px 内也会被拒。值得 —— 丢一次边缘点击远比
静默空点三步强，而且 [app.js:160-163](/home/rockchip/wdnwebui/app/apps/usb_hid/static/app.js)
早就写明 tip-up 不带 X/Y 会"把笔瞬移到原点，点到 (0,0) 处的任何东西"，
全卡对 (0,0) 的处理本来就是一致的。

### 失败必须写进步日志，不能只有一句总括

`{"action":"click"}`（漏坐标）和 `I cannot help with that`（吐废话）
如果都收敛成"模型没有给出可执行的动作"，页面就是一个**空 step 列表**
加一句通用报错 —— 上面那个 bug 查不动，正是因为两者从外面看一模一样。
现在 `_ask_model` 返回 `(action, reason)`，`reason` 由
`_parse_action_json` / `_normalize_action` 沿 `why` 列表收集
（区分"没有坐标"/"没有 JSON"/"不认识的动作"），`_loop` 先 append 一条
error step 再收工。

### 全黑 / 纯色画面必须在送推理之前停

没信号时 `std ≈ 0`，此时 **(0,0) 是模型唯一能给的答案**，于是每一步都点
在桌面左上角 —— 和上面的 bug 症状一模一样，排查方向却完全不同（该动
的是 HDMI 源，不是 prompt 或模型）。

→ `_is_blank_frame(frame)` 在 `_loop` 里、送检之前拦，报"PC 没有真正输出
到 HDMI-IN"。判据是 `max <= 3`（近黑）**或 `std < 2`**（任何亮度的纯色：
HDMI 的 No Signal OSD 常常是纯蓝/纯白，只判黑会漏掉一整类）。真实桌面
哪怕是纯黑壁纸 + 小窗口，std 也在 20~80，离 2 差两个数量级，不会误判。
抽样算 + uint8，单帧约 1.4 ms。

> 这个检查**必须放在 `_loop` 而不是 `_grab_screenshot` 里**：后者是可以被
> 替换/打桩的，守卫跟着一起消失，测试和将来任何新的取帧方式都绕得过去。
> "帧从哪来"和"帧能不能用"是两件事。

### DemoGuard：这张卡的 `active` 曾经是恒真的

usb-hid 的 app.js 里原来有 `DemoGuard.register(id, {active: () => true, stop: noop})`，
**它覆盖了 manifest 里的 demo_guard** —— 因为
`DemoGuard.register` 就是 `_registry[id] = handlers`，而 `_registerCardsFromManifest()`
在 onReady 里是**同步**跑的，卡片 IIFE 要等 framework 注入的 `<script>` 从网络
回来才执行（`_preloadCardScripts` 挂在 Promise 链上）。结果：用户在这页点任何
sidebar 链接都弹"确认离开?"，而 stop 是空函数 —— 拦住了又什么都不停。

现在卡片只 `DemoGuard.report("usb-hid", snapshot)`，配置一律以 manifest 为准
（`active_state_keys: ["running","state"]` + `stop_endpoints: /task/stop`）。
**report 必须整体上报服务端快照**：active 判定读的就是 `running` / `state`
这两个键，漏报等于告诉框架"没在跑"，于是用户切页时不会被拦，PC 会在用户已经
离开的页面上继续被点。

`hdmiin` 采集本身**不**算 active —— 那是跨页保持的预览流，正是用户想要的，
拦它反而是回归。

## 帧判据(v0.18 下沉到 4 张视频卡)

「这一帧要不要送去跑推理」曾经有两套:三张卡的 detector 各自 `every_n` 跳帧,
VR 另有一个二值的「自动判帧」。现在**只有一份实现**:
`app/core/video/frame_judge.py:FrameJudge`,四张卡共用。

| 位置 | 说明 |
|---|---|
| 算法原语 | `wdnav.scene`（RGA NV12-Y 缩略图 + 可注册度量） |
| 判据策略 | `app/core/video/frame_judge.py`（模式 / 阈值 / 兜底间隔 / 分数环形缓冲） |
| vd·vtd·vtl 接线 | 三张卡都 → `app/core/video/multi_session.py`（按 key `"vd"` / `"vtd"` / `"vtl"` 注册的多路会话）。判据实现在 `frame_judge.py` 一份，每路各持一个 `FrameJudge` |
| video-retrieval | `app/core/video/search.py::VideoSearchService._judge`（单实例；每次 `start_extract` / `open` reset，切片时逐秒 `observe`） |
| HTTP | 三卡共用 `/api/video/judge*`；VR 挂自己的 `/api/video-retrieval/judge{,/metrics,/reset}` |
| 前端 | `app/static/core/frame-judge.{js,css}`，四张卡各 mount 一个实例（按 prefix 索引）；VR 用 `configOnly: true`（不建浮框/曲线） |

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
`EXPOSED_METRICS[0]`,两边顺序反了会对不上。`verify_frame_judge_metrics.py`
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
   manual**(项目契约,`verify_frame_judge_metrics` 断言 `normalize_mode("sad")
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

默认模式是 `gscore`(v0.22 起,`EXPOSED_METRICS[0]`),默认帧间隔
`DEFAULT_EVERY_N = 600`(25fps 下 ≈ 24s 兜底间隔)。**这两个默认值三处必须
一致**:后端常量 `frame_judge.DEFAULT_EVERY_N`、共享前端组件
`frame-judge.js` 的 `_fallbackCfg()` / `inpEvery.value`、以及三张卡的
`wizard.json` 里 `<prefix>-judge-every-n` 的 `default`。任一处漂移都会出现
「向导显示 A、功能页显示 B、浮框 every_n_eff 口径又对不上 B」的三方打架。

> **别再用 detector 时代的老直觉解释 30**:那个 30 是"每 30 帧送一次"的
> **固定跳帧节流**,而多路卡的 detector every_n 早已锁死为 1(节流归轮转
> 调度器),那个节流**不存在了**。判据换到 gscore 之后 every_n 的语义变成
> **兜底间隔**,而兜底条件是 `gap >= every_n and score >= threshold*0.5`:
> 单路每 25 帧评估一次、多路每轮转评估一次,gap 一次就涨 25~50+ 帧 ——
> **every_n=30 时兜底条件恒成立**,gscore 门形同虚设,画面没变也照送 NPU。
> 600 才是"双保险"该有的量级(见 §默认 every_n = 600)。

### 踩过的坑:vtd 向导的帧间隔默认 30(其它卡 600)

**症状**:「视频目标监控」快速配置里帧间隔默认 **30**,而音视频描述 /
视频目标定位都是 **600**。

**根因**:`a0a98cb`(vtd 多路页接通帧判据)把 vtd 的 `wizard.json` 从
`default: 600` 改成了 `30`,**只动了 vtd 一张卡**。那个 30 不是设计决定,
是抄了后端当时的 `DEFAULT_EVERY_N = 30`(=detector 时代"每 30 帧送一次"的
固定节流)。同一批 commit 里 vtl / vd 的 600 没动,于是差异一直留着;v0.25
把视频卡拆成插件包时又被原样冻结进 `wdnapps_video_analyzer`。

**危害不只是"数字不一样"**:多路页的判据是**每轮转评估一次**
(`multi_session._run_one_channel` → `judge.observe(thumb, ch.frames)`),
两次评估之间该路已经解了 25~50+ 帧,`gap` 一次就涨过 30 → 兜底条件
`gap >= every_n` 恒成立 → **gscore 门被架空**,画面没变也照送 NPU。600 才
是"兜底间隔"该有的量级(≈24s)。

**修法**:vtd `wizard.json` 的 `default` 回到 600,并把后端
`DEFAULT_EVERY_N` 一起从 30 改成 600 —— 后端常量是**初始态**,不改它的话
「直接进功能页(不过向导)」的用户看到的仍是 30,和向导里的 600 打架。
共享前端组件 `frame-judge.js` 里残留的 `|| "30"` 兜底(输入框空时的
fallback)一并改成 600。

> **三处默认值必须一致**:后端 `frame_judge.DEFAULT_EVERY_N`、共享组件
> `frame-judge.js`(`_fallbackCfg()` / `inpEvery.value` / `config()` 里的
> fallback)、各卡 `wizard.json` 的 `<prefix>-judge-every-n.default`。
> 改任何一处,grep 一遍另外两处。
>
> 回归防护:`python -m scripts.verify_judge_every_n_defaults`(不占 NPU)——
> 断言三处默认值都等于 600 **且** 钉住行为:用真实 `FrameJudge` 跑一轮
> 「多路轮转」模拟(每轮之间解 47 帧),every_n=30 时兜底恒成立(20/20 轮
> 全放行 = gscore 门被架空),every_n=600 时门只放行 2/20 轮。
>
> 卡片源码现在在插件包里(`.venv/src/wdnapps-video-analyzer/`),**改卡片的
> `wizard.json` 不在本仓库** —— 用 `scripts/card_dirs.py` 查源目录。

三条**实测踩出来的**规矩，改这块时务必先读:

1. **`snapshot()` 带 `gen`（配置代次），前端必须丢弃过期快照**。WS/HTTP 的
   传输顺序 ≠ 计算顺序:用户在页面上把判帧算法改成 manual 之后,POST **之前**
   就已经发出的那几份旧快照还会陆续到达(快照是发送那一刻算好的)。照单全收
   的表现是控件在两个模式之间来回跳。`gen` 取自
   `frame_judge._GEN`(进程级单调递增,**不能**改成每实例从 0 开始 ——
   按实例计数会让前端记住的 gen 比新 run 的还大，之后所有快照都被当
   过期丢掉，浮框和曲线直接冻住)。
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
  //   2) **用宿主共享启动器 window.WizardStart.run() 启动**(等能力就绪 →
  //      直接调启动函数 → 确认真的在跑);**不要 btn.click()** ——
  //      disabled 按钮的 click 会被浏览器静默丢弃,详见下一节
  //   3) 返回 Promise(throw → wizard 回退到 source 步骤)
};
```

未实现 `applyWizardSnapshot` 的 app:wizard 完成时只是 unmount,应用页
不会跑出 demo 页(`console.warn`)。这是 wizard 设计上的"安全降级" —— 让
应用层**可选**地接入自动启动,但不强制。

### 踩过的坑:启动按钮的 `disabled` 会把 `btn.click()` **静默吞掉**(v0.29 修)

**症状**:向导配完点「完成配置」,向导显示"配置完成"、功能页上却**什么都没
跑** —— 音视频描述(vd)的视频描述没启动、视频任意目标定位(vtl)没定位。
「视频目标检测」(vtd)有时能跑 —— **纯看运气**。

**根因(2026-10-23 真机 journal 证据链)**:

1. `6ea16e9 feat(cards): 模型能力未启用时置灰「启动检测」` 给启动按钮加了
   `disabled` 判据:`startBtn.disabled = !snap.available || !snap.session_ready
   || snap.model_loading || snap.enabled || booting`。`session_ready` 来自**卡片
   自己的 1Hz WS 快照**,而 registry 那边模型可能**刚刚**才变成 running ——
   **快照比 registry 慢一拍(≤1s)**。
2. 卡片原本的向导启动方式是 `btn.click()`(模拟用户点「启动检测」)。
   **disabled 按钮的 `click()` 被浏览器静默丢弃**:不报错、不打日志、不触发
   handler。于是向导的 `applyWizardSnapshot` "成功"返回 → 向导 unmount →
   功能页上什么都没跑。
3. 真机日志坐实(单机板,`/api/compute` 轮询 + WS 快照两条线):
   - vtl:`08:19:35 instance LocateAnything-3b → running` 与
     `08:19:35 POST /api/video-target-localization/open` **同一秒**,
     而卡片的 WS 快照 `08:19:36` 才送来 `session_ready=true` → 点击被吞,
     **`POST .../prompt` 从未发生**。
   - vd:`08:24:34 qwen3-vl-4b → running` / `08:24:35 open` / **无
     `POST .../prompt`** —— 同一个坑。
   - vtd:`08:14:34 → running`、点击发生在 `08:14:35`(模型提前 ≥1s 就绪)
     → 侥幸赢下这一拍 → 看得出"能不能跑"取决于模型就绪与点击的相对时序。

**修法(宿主 `app/static/core/wizard-start.js` + 四张卡的 hook)**:

* 新增共享启动器 `window.WizardStart.run({label, probe, start, isReady,
  isStarted})`:**等能力真就绪 → 直接调卡片自己的启动函数 → 再问一次后端
  确认"真的在跑"**。三步缺一不可:
  - *等就绪*:`probe` **直接问后端快照**(卡片自己的 `refresh*Detector()`),
    不等 WS 那一拍;等不到(20s)→ 抛 `capability_not_ready`(retryable),
    向导显示错误 + 「重新开启模型」。
  - *直接启动*:绕开按钮的 `disabled` —— 不能再用 `btn.click()`。
  - *确认*:卡片的启动函数失败时只往 overlay 写一句话、不抛异常;不确认
    就会把"没启动"又变成"静默成功"。没跑起来 → 抛 `start_failed`。
* 四张卡(vd / vtd / vtl / **vr**)的 `applyWizardSnapshot` 都改用它;
  vtl / vtd 的 `loadInstancePickMulti` 要挪到**就绪之后**(等就绪前模型还没
  起,实例列表是空的 → "一个都没勾"静默中止)。
* vd 的**音轨分析**同坑(「分析音频」按钮的 `disabled` 来自音轨面板快照的
  `available`;Omni 刚就绪时按钮还是灰的 → click 被吞 → 音轨没跑起来),
  一并改用 `isReady: available !== false` / `isStarted: state === "extracting"`。
* vr(视频内容检索)的就绪口径与三张视频卡不同 —— 它的按钮 `disabled` 判据是
  `modeAvail.search`(检索模型有没有实例)+ **检索问题非空**;它靠点击前那次
  `vrRefreshModeAvail()`(查活的实例表)兜住了前者,但**问题空着时按钮必灰**
  → click 被吞 → 静默不启动。改用同一启动器,但判据换成卡片自己的通道:
  `isReady: available !== false` / `isStarted: 错误横幅为空`
  (`vrStart()` 的每个"没开始"分支都写横幅,成功开跑时清空 —— 比看 state 稳,
  state 可能是上一轮的)。

**回归**:`node scripts/verify_wizard_start.mjs`(纯 Node,不占 NPU)——
验共享启动器的三种行为(不就绪不启动 / 等到就绪才启动 / 启动没成功要报错)
+ 四张卡的 hook 里不再有 `btn.click()` + 框架认识新错误码。

#### 同一个坑的第 5 张卡:语音生成(tts)—— v0.30 修

**症状**:第一次跑完「语音生成快速配置」向导,**没有出声**;等模型加载完再
重复一遍向导,就能正常合成。

**根因(2026-10-23 真机 journal 坐实,单机板)**:

* `92dd883` 把宿主共享启动器 `core/wizard-start.js` 落地时,只改了**插件包里
  的四张视频卡**(vd / vtd / vtl / vr)。**仓库内的 tts 卡漏改**,`applyWizardSnapshot`
  还在用 `btn.click()`(见 `wdnapps_audio_chat` 包的 `cards/tts/static/app.js`)。
* tts 的启动按钮 `disabled` 判据是 `!capReady`,而 `capReady` 来自
  **卡片自己的快照** `snapshot.capability_ready`(后端 = registry 里有没有
  running 的 TTS 实例)。卡片快照的刷新周期是 **5s**,而向导的
  `_waitForModelsRunning` 轮询 `/api/compute` 是 **1s** —— **快照比 registry
  慢的不是一拍,是整整一个轮询周期**。
* 于是:registry 在 `13:04:27` 把实例翻成 `running` → 向导 `13:04:28` 的
  `/api/compute` 轮询看到 running → 立刻 `applyWizardSnapshot` → `btn.click()`
  → 但卡片手里最新的快照是 `13:04:24` 的(`capability_ready=false`,按钮还是
  灰的)→ **disabled 按钮的 click 被静默丢弃** → `POST /api/tts/start`
  **从未发生**(journal 里 12:56~13:06 有 8 次向导的
  `POST /api/compute/instances`,`POST /api/tts/start` 只有模型加载完之后的
  2 次)。向导照样显示「完成配置」✓。
* tts 的快照还有第二层放大:WS 的变化判据只看 `state / frames / error`,
  **不含 `capability_ready`** → 模型被启用/停用时**会话状态不变**(还是
  `idle`)→ WS **不推** → 卡片只能等下一次 5s 轮询。视频卡的 `session_ready`
  同理,但它们的轮询是 1Hz,所以只差一拍。

**修法**:

* tts 的 `applyWizardSnapshot` 改用 `window.WizardStart.run()`:
  `probe` = `GET /api/tts`(直接问后端,不等卡片那一拍),
  `isReady` = `capability_ready === true`,
  `isStarted` = `busy || state ∈ {loading, running}`。
* `TTS.start()` 失败**要抛错**(`WizardSnapshotError:start_failed:…`),
  不能再只往状态行写一句话 —— 只写状态行的话向导照样显示「完成配置」。
* 后端 `/api/tts/ws` 的变化判据加上 `capability_ready`:模型启停时
  **立刻**推一帧,按钮 200ms 内解禁,不必等 5s 轮询。
* 后端快照补 `capability_loading`(本机有该能力但**还在加载**的 model id,
  口径 = `registry.pending_models_for_capability`)。有了它,前端才能把
  「模型正在加载中」和「压根没启用」分开说 —— 混成一句「模型未启用」会让
  用户以为是自己没开模型。`get_session` 失败时同理:在加载就说「正在加载中」,
  否则说「功能未启用」。

**新增规矩**:

* **任何卡片的向导启动路径都不许出现 `btn.click()`** —— 一律走
  `window.WizardStart.run()`。新增卡片时照抄 vtd 的 hook 形状。
* 卡片的启动函数**失败必须抛错**(带 `start_failed` / `capability_not_ready`
  错误码),不能只写横幅 —— 否则向导永远显示「完成配置」。
* 「就绪判据」必须**直接问后端**,不能用卡片自己的快照;卡片快照的刷新周期
  (tts 5s / 视频卡 1s)就是「那一拍」的长度。

**回归**:`scripts/verify_wizard_start.mjs` 的 (d) 段 —— 把 tts 卡里的
`ttsCapabilitySnapshot` / `_wizardStart` / `applyWizardSnapshot` 抠出来在
Node 沙箱里跑真代码,断言:不就绪不启动、就绪后才启动、**全程不点按钮**、
不就绪抛 `capability_not_ready`、启动失败抛 `start_failed`。

### `_wizardBringUpModels` 全局入口

`app/static/core/wizard/state.js` 暴露 `window._wizardBringUpModels(cfg)`,
供 env-check 步骤的"一键开启"按钮调用。两步流程:

1. **停非必需**(本机 spawned / adopted_local;**跳过 adopted 远端 peer**):
   `instance._source !== "adopted"` 的 running 实例,如果它**不在本应用
   需要的 (模型, 卡位) 对集合里** → DELETE `/api/compute/instances
   {model, npu_index}`(**必须带卡号**)。
2. **启必需**:`swcap_to_model` 的每条 → POST `/api/compute/instances
   {model, npu_index}`(allow_adopt=False,peer adopted → server 403 兜底)。
3. 返回 `true` 表示启动指令已发,后台加载中;false 表示失败(env_check 步骤
   会 retry 2 次 + confirm 兜底)。

#### 「停非必需」的判据必须是 **(模型, 卡位) 对**,不是模型级（v0.28 修）

用户实测 BUG(音视频描述 / vd 卡):两张 RK182X 都加载了 [VLM]视觉描述v3
(`qwen3-vl-4b`) 时,点「一键开启所需模型」→ [Omni]多感知描述v2.5
(`qwen2.5-omni-3b`)**起不来**。

vd 卡的 `swcap_to_model` 要的是**两张不同的卡**:

```
swcap_vision_chat  → qwen3-vl-4b        @ npu0
swcap_audio_chat → qwen2.5-omni-3b   @ npu1
```

原判据按 **capability(模型级)** 算:`qwen3-vl-4b` 的 capability 是
`swcap_vision_chat`,在 `swcap_to_model` 里 → **两张卡上的都判成
「需要」→ 都不停** → npu1 仍被 qwen3-vl-4b 占着(3500MB),omni 要
2500MB,5120 装不下 → worker **rc=-6**。

> 同模型跑两张卡是**合法形态**(vtd 卡就是 `qwen3-vl-4b @ npu[0,1]`),
> 但「需要哪张卡」逐张不同。判据粒度错了就会腾不出卡。

* **需要集合**:`swcap_to_model` 的每条按 `npu_index` 展开成
  `(model_id, npu)` 对;`npu_index` 为 null 的卡 → 该模型**任意卡**都算需要。
* **DELETE 必须带 `npu_index`** —— 不带就是「停该模型的所有卡」,会连本
  应用需要的那张一起停掉。
* **卡号未知(`npu_index` 缺失 / -1)的实例一律不动** —— 判不出它是不是
  「要的那张卡」;而 DELETE 不带卡号 = 停该模型**所有卡**,等于把需要的
  那张也停掉(同款事故的反方向)。真机快照里 `npu_index` 一定有值,这条
  护栏是给「字段缺失 / 乐观写重置成 -1」的快照兜底。

#### `stop_instance_local(npu_index=N)` 只停那一张,连带 unload 也一样（v0.28 修）

`stop_instance_local` 的文档语义是「`npu_index` 缺省停该模型全部实例;
给了卡号只停那一张」。但 `_stop_inst` 内部调的是
`unload_instance(model_id, teardown=False)` —— 那是**按模型全量遍历**的,
于是**停 npu0 的 qwen3-vl-4b 会把 npu1 上的同模型实例也 unload 掉**
(真机实测:npu1 的那个跟着变 `not_loaded`),与文档语义相悖,也会把
「腾出来的卡」重新弄脏。

→ `_stop_inst` 改调 `_unload_one(inst, teardown=False)`(单实例版),
只回收目标实例。

回归:`scripts/verify_wizard_multicard_bringup.py`(5 断言,mock 驱动,
断言「停的是多余那张卡、不是需要的那张、DELETE 带卡号」)。

### 向导 env 步的能力判定与模型启动等待

**判定链路**(`app/static/core/wizard/state.js`):

| 位置 | 作用 |
|---|---|
| `_modelSlots(cfg, instances)` | 纯函数:把 `swcap_to_model` 展开成 (模型, 卡位) 对,逐个标 `up`(running **或** loading —— 启动中算就绪在途)与 `err`(该槽位上的实例进了 error 终态);`npu_index` 为 null 的模型 → 任意卡即可 |
| `_envView(state, cfg)` | 判定结果:每条能力一个四态 `capStates`,`swOK = 无 fail`;渲染与后台刷新共用同一份口径 |
| `_localInstances` / `_capJudgement` | 本机实例过滤 + capability 缺失判定(peer adopt 不算本机算力,与 finish 步同口径) |
| `_renderEnvStep` | 能力行四态;**不外露模型名与卡号** |
| `_refreshEnv` | env 步后台刷新一次:结论**变了**才重绘(否则会擦掉「一键开启」刚写的状态文案) |

> **卡位级判据必须用「裁剪后」的 cfg** —— 单卡模式下 `_planSingleCard` 把
> `npu_index` 换成唯一卡号;按原始 cfg 算会算出**不存在的卡位**。所以
> slots 在**渲染时**算(`_envView` 收 `cfg` 参数),不在 `mount` 里算。

**env 步的「软件能力」行按「能力是否可用」分四态** —— 判据是「本机有没有
running/loading 实例提供这个 cap」,不是「cfg 里指定的模型起没起」(卡片按
capability 取 session,`get_session`,不认 model_id):

| 态 | 条件 | 图形 | 阻塞下一步 |
|---|---|---|---|
| `ok` | 配置的槽位全 up | ✓ 绿 | 否 |
| `alt` | 配置的槽位都没 up,但别人提供该能力 | ⚠ 黄 | 否 |
| `warn` | 配置的模型开了但卡位缺 | ⚠ 黄 | 否 |
| `fail` | 没人提供该能力,或配置槽位上的 worker 挂了 | ✗ 红 | 是 |

- ok / alt / warn 都不挡「下一步」,只有 fail 挡;alt 与 warn 都画 ⚠ 黄,
  区别只在配置匹配度。
- 能力组(`groupFail`)优先于 alt —— 组要求同一个实例满足整组能力。
- alt 态不额外弹说明块;「一键开启所需模型」与「下一步」是否可用解耦
  (能力没配齐就出该按钮);「本板未安装所需模型」提示排除 alt 态的 cap。

**finish 步等模型就绪**(`_waitForModelsRunning`)的目标带 `cap`,按目标
自己的状态判:

| 目标状态 | 处理 |
|---|---|
| `running` | 就绪 |
| `loading` | 还在起,等它(不能被别人顶替) |
| `error` | 终态故障 → 立刻中止报「启动失败」(不能被顶替掩盖) |
| 缺席 / `not_loaded` | 「没起」→ 本机有人提供该 cap 就顶替 |

三条不变量:

1. **终态立即中止**:`status === "error"` → 立刻抛错,不等宽限期。`abort`
   必须在 `try/catch` **外面**抛 —— `catch (_) { /* retry */ }` 会把中止吞掉。
2. **缺席有宽限**:`MISSING_GRACE_MS = 15s`。POST 之后实例 ~1s 才注册,
   宽限只用来区分「刚 POST 还没注册」和「压根没跑」。卡号要严格匹配 ——
   `unload_instance` 会把 `npu_index` 重置成 -1,所以「被停掉的实例」对
   `npu_index: 0` 的 target 就是**缺席**。
3. **等待可被打断**:向导被卸载时 `statusBox.isConnected` 为 false →
   **抛错**中止,不能 `return`(静默 return 等于「等待成功」,`_finish` 会
   接着调 `applyWizardSnapshot`,模型没就绪就把应用拉起来)。

**失败必须给出口**:finish 失败时渲染错误 + 「重新开启模型」按钮
(`#wd-wizard-finish-retry-btn`,调 `window._wizardBringUpModels(cfg)` 后
重试 `_finish`)。

**缓存语义**:`unmount(silent=true)`(切页路径)与「跳过向导」都清掉 `.step`
(**保留 `.values`**,回来仍预填)→ 再次进入该卡从第 1 步开始。**reload
不清**(不走 `onNavAway`)—— 「刷新后回到上次停留的步骤」保持不变。

**找实例统一走 `_pickInst(cands)`**:优先 running/loading,其次 error 终态 ——
同模型可能有多个卡位的实例,取首个未必是活的那个。

### `wizard:unmounted` 事件 + 重新挂载

`state.js:unmount()` 末尾 dispatch `window` 上的 `wizard:unmounted` 事件
(detail `{appId, action:"unmount"}`)。`framework.js:232` 监听该事件,
从 `DemoGuard._loadedApps` 删对应项 —— 否则下次再点同一应用卡片时
ROUTER 看到 `_loadedApps.has(page)` 为 true 直接跳过 `_loadCardJS`,
wizard 永远不会再挂载。

### `wizard:step-rendered` 事件 —— 运行时才知道选项的字段

`wizard.json` 是静态的,但有些字段的选项**运行时**才知道(tts 的音色
清单来自后端,用户音色随时会变)。`_renderFormStep` 渲染完字段后 dispatch
`window` 上的 `wizard:step-rendered`(detail `{appId, stepId}`),卡片收到
后往自己的字段里塞选项 —— 框架仍然不认识任何字段名,零耦合不变。

**卡片自己填选项时必须记住自己选的值**。换步会 `body.innerHTML=""`
重建整个 step,新的 `<select>` 是空的 —— 框架恢复 `state.values` 只能
恢复**已经存在的选项**,而选项是卡片后填的。卡片重建选项后若回落成
「功能页当前值」,用户在向导里挑的音色会被无声冲掉:实测选「高冷御姐音」
→ 下一步 → 上一步 → 变回第一个音色,合成请求也跟着用错音色。

解法:用 `wizard:field-change`(已有事件,detail 带 `values` 快照)记住
向导里自己的选择,重建时优先还原。

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

### 踩过的坑：`#wd-wizard-stepper` 是**全局 id** → document 级选择器会读到另一个向导

向导容器是 `#wd-wizard-root-<appId>`，但它内部的 stepper / body 用的是
**不带 app id 的全局 id**（`#wd-wizard-stepper` / `#wd-wizard-body`）。

框架内部没事 —— `state.js` 一律用 `container.querySelector("#wd-wizard-stepper")`
**限定在本卡容器内**（`_gotoStep` / `_renderStepper` 都这么写）。

**页面里可能同时存在两个向导容器**：框架在页面加载时会自动进入「上次
打开的卡片」并 mount 它，你再切到另一张卡 → 两个容器短暂并存 →
**同名 id 重复**。

于是**外部**用 document 级选择器就会串台：

```js
// ❌ 读到的是另一个向导的 stepper —— 步骤号恒为 0
document.querySelectorAll('#wd-wizard-stepper .wd-wizard-step-dot')
// ✅ 限定在本卡容器内
document.getElementById('wd-wizard-root-' + id)
      .querySelectorAll('.wd-wizard-step-dot')
```

症状：**回归脚本偶发失败、重跑又过**（是否串台取决于框架自动进的是
哪张卡）。`verify_wizard_finish_abort.py` 的 `step_index()` 就踩过这个。

> 视觉上没有影响（另一个向导所在的 page section 是 hidden 的），改 id
> 化会波及 `framework.js` 的 `showPage` 定位，**别动** —— 写选择器时
> 限定容器就行。

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
| Layer 1 UI | `scripts/verify_probe_env_local_only.py` | 6/6 |
| Layer 3 | `scripts/verify_find_by_model_priority.py` | 10/10 |
| 全量 wizard | `scripts/smoke_wizard_all.py` | 6/6 |
| 启停过渡态 | `scripts/verify_compute_transition_guard.py` | 23/23 |
| 一键开启腾卡 | `scripts/verify_wizard_multicard_bringup.py` | 5/5 |
| env 就绪卡位级 | `scripts/verify_wizard_slot_readiness.py` | 13/13 |

> **mock `/api/compute` 必须在 `page.goto()` 之前装好**。框架在页面加载
> 时就会进卡片并 mount，向导的 `_probeEnv` 立刻拉 `/api/compute` —— route
> 装晚了那次请求就打到**真后端**，板子上真在跑的模型会污染 `swMissing` /
> `swOK`（「一键开启所需模型」按钮干脆不渲染）。症状是「脚本偶发失败、
> 重跑又过了」—— 板子状态一变就复现。

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

> ⚠️ 「数据层本来就支持」这句话曾经把人带沟里：key 的**形状**带卡号，
> 但 `bind_instance` 直到异步 load 跑完（~35s）才把占位 key 换成带卡号的
> 那个 —— 同一模型开第二张卡时，第二张卡的 spawn 就在这个窗口里用同一个
> 占位 key 把第一张卡覆盖掉了。实测症状：第二张卡永远加载不上 + 算力页
> 多出一张重复卡。详见
> [同模型多卡启用：实例 key 必须当场带卡号](#同模型多卡启用实例-key-必须当场带卡号v027-修)。

**`LLMRPCSession` 绑定 worker 身份，所以没有透明负载均衡**。
`_setup_rpc_client` 按 pid 建 Service，session 对象本身带着「我是哪张卡的
哪个 worker」这个身份。想打另一张卡就必须显式指名 —— 这也是
`list_sessions()` 返回**列表**而不是单个 session 的原因：

```python
# 枚举所有能服务该 capability 的端点（含/不含远端由 include_remote 决定）
handles = get_registry().list_sessions("swcap_vision_chat", include_remote=False)
handles = get_registry().list_sessions("swcap_vision_chat",
                                       include_remote=True,     # 也含 LAN peer
                                       model_id="qwen3-vl-4b")
for h in handles:
    h.model_id, h.npu_index, h.source, h.pid, h.is_remote, h.worker_key
# 不指名时用 get_session / get_session_for_model（可传 npu_index 指名）
```

`include_remote` 默认 `False`（只返回本机）的原因见 `get_session` 的
docstring：视频消费方走 `/dev/shm` path-list 传帧，跨机 worker 读不到。

### 同模型多卡启用：实例 key 必须当场带卡号（v0.27 修）

**症状**（真机，用户在「视频目标定位」向导点「一键开启所有模型」）：
`LocateAnything-3b` 只加载到 NPU0，NPU1 一直不加载；算力配置页出现**两张**
[GD] 卡片（同一个 pid）。

**根因是一串三步**，`POST /api/compute/instances {npu_index:[0,1]}` 触发：

1. **实例 key 撞车**。`_spawn_compute_worker` 给每次 spawn 都用占位 key
   `(-1, model_id, "spawned")`，而 `bind_instance` 直到**异步** load 跑完
   （实测 ~35s）才把 key 换成 `(npu, model_id, source)`。第二张卡的 spawn
   在这 35s 窗口里用**同一个占位 key** 注册 → dict 覆盖 → 第一张卡的实例
   被挤出 `_instances`。
2. **adopt 找不到它 → 另建一张卡**。LAN peer 的 SPDP 事件触发
   `_maybe_adopt_from_peer()` → `_adopt_existing_worker()` → probe 命中本机
   worker。因为 1. 把它挤出了，`_find_inst_by_worker_pid()` 找不到 → 新建
   一条 `adopted_local` → 算力页多出第二张 [GD] 卡。
3. **adopt 把第二张卡的 worker 当孤儿杀掉**。「清理多余孤儿」把 `/proc` 里
   所有**不是**被 adopt 的同 model worker SIGTERM 掉 → NPU1 的 worker 死了
   → 那张卡永远加载不上。

真机日志原文（修之前）：

```
spawned compute_worker for LocateAnything-3b (pid=12265)     # 卡 0
spawned compute_worker for LocateAnything-3b (pid=12268)     # 卡 1
adopted LocateAnything-3b from local pid=12265; cleaning 1 extra orphan(s): [12268]
sent SIGTERM to orphan worker pid=12268
bind LocateAnything-3b RPC fail: err='timeout' rc=0
```

**改法**（四处，全在 `registry.py`）：

* **spawn 时就把卡号写进 key** —— `_spawn_compute_worker` 建实例时用
  `(npu_index, model_id, "spawned")`（卡号未知才退回 `-1`）。**boot(autostart)
  路径不走 `bind_instance`**，光靠 bind 时改 key 救不了它。
* `bind_instance()` 的乐观更新里**当场**把 key 换成
  `(npu_index, model_id, source)` —— 卡号在 bind 时已经确定，不该等 35s
  后的异步 load。key 与 `npu_index` 从此同步。
* `_adopt_existing_worker()` 的「清理多余孤儿」只杀**不在 `_instances` 里**
  的同 model worker。registry 跟踪的那些（一个模型跑 N 张卡 → N 个本机
  worker）全都是合法的。
* 顺带修 `spawn: %d existing %s worker(s)` 的 log 参数错位（`%d` 收到
  `model_id` 字符串 → logging 内部 TypeError，**整条消息连同 traceback
  一起丢掉**）。

**为什么 key 必须带卡号**：`_instances` 是 `dict[key → instance]`，key 撞车
就是**静默覆盖** —— 后一次注册把前一个实例挤出去，那张卡的 worker 变成没人
管的进程。没有「多了一张卡」的提示，只有一张卡莫名其妙加载不上。

**autostart 要记两张卡**（`set_active_app`）：同一个模型跑两张卡时，
承载该 capability 的 local running 实例**有两个**，两个都要写进
`compute.autostart`。只记第一个的话，下次开机另一张卡空着，用户以为
「两张卡都开着」其实只剩一张。

**修之后**同一场景的日志 —— adopt 走 in-place 更新，既不新建卡也不杀 worker：

```
spawned compute_worker for LocateAnything-3b (pid=17783)   # 卡 0
spawned compute_worker for LocateAnything-3b (pid=17786)   # 卡 1
v0.8.28: in-place updated spawned for LocateAnything-3b (pid=0 npu=1 status=loading)
```

实测（真机 RK3588 + 2×RK1828）：

* 向导路径：`POST /api/compute/instances {npu_index:[0,1]}` → 两个 worker
  分别 `building LLMSession(model=LocateAnything-3b, npu=0/1)`，
  `rknnsmi` 两张卡都是 `3203 / 5120 MB`，算力页 `[GD]` 卡片 **2 张、卡号
  0 和 1**，没有重复卡。
* boot 路径：`autostart = [{LA,0},{LA,1}]` → 两个 worker 命令行分别带
  `--npu 0` / `--npu 1`，都 `running`，算力页同样 2 张卡。

回归：`scripts/verify_multicard_enable.py`（29/29）。桩件里的 **load RPC 必须
挂住**（`threading.Event`）—— 真机上模型加载要 35s，bug 就发生在这个窗口
里；桩件秒回的话异步 key 迁移会在第二张卡 spawn 之前跑完，race 被抹掉，
测试变成「永远绿」的假回归。反向验证：把修复回退，该脚本 8 项失败。

**勿回退**：

* **实例 key 必须带卡号** —— spawn 时带（boot 路径靠它），bind 时再确认一次
  （算力页路径靠它）。挪回「统一用 `(-1, model_id, source)` 占位」就退回
  「第二张卡的 spawn 覆盖第一张卡」。
* **adopt 的孤儿清理不能杀 registry 已跟踪的 worker** —— 一个模型跑多张
  卡时 N 个本机 worker 都合法。
* **`_find_inst_by_worker_pid()` 的 in-place 更新是「不重复建卡」的保证**
  —— adopt 命中本机 worker 时必须复用已有 entry，别新建 `adopted_local`。
* **`set_active_app()` 要记承载该 capability 的全部 local 实例** —— 只记
  第一个会让「两张卡」在下次开机退化成一张。

### 模型启停的过渡态：启停前必须先判状态（v0.28 修）

用户报的四类竞态 —— 「再次开启一个正在启动中的模型」「开启一个正在
关闭中的模型」「关闭一个正在启动中的模型」「关闭一个正在关闭中的
模型」。四个都是 `rc=-6`（SIGABRT）的来源。

**根因不是「没加锁」，是状态机缺了「关闭中」这一态。**
`status` 的值域只有 `not_loaded / loading / running / error`，
**关闭过程中对外仍报 running/loading** —— 并发请求看不出「这个实例
正在被拆」，于是当成可复用的再 load 一次。worker 侧 `load_handler`
只在 **running** 时幂等；**loading 时不命中那个分支** → 会**再起一个
init 线程** → 两个 init 线程同时申请同一份 NPU 内存 → rc=-6。

**判据**（`registry.transition_of()`，纯函数，单测友好）：

| 过渡态 | 判据 | 说明 |
|---|---|---|
| `stopping` | `inst._stop_depth > 0` | unload / stop 期间置位；用**计数**不用 bool —— `_stop_inst` 内部会调 `unload_instance`（嵌套），内层 finally 减 1 不能把外层的「关闭中」提前清掉 |
| `loading` | `inst.status == "loading"` | 含 worker 内部 init 线程那段（那段里 `_bind_inflight` 已清零，只有 status 还留着 loading） |

`transition` 作为**独立字段**进快照，**不改 `status` 的值域** ——
status 的消费方太多（wizard 等待 / probe / 算力页 badge / `_probe_env`），
塞进 `"stopping"` 会波及一大片。

**语义**（`app/system/api/compute.py`）：

| 目标状态 | 操作 | 行为 |
|---|---|---|
| loading | 启用 | **排队**等它加载完 → 重新求值再执行（worker 侧 running 幂等，安全） |
| loading | 禁用 | **排队**等它加载完 → 再停。等它**加载完**是安全的：init 线程正常跑完、显存已归还；不安全的反而是**硬** unload（init 线程带不走 `rknn3_destroy` → 显存不归还）。**不能拒绝** —— 见下方「向导」段 |
| stopping | 启用 | **排队**等它关完 → 重新求值再执行 |
| stopping | 禁用 | **排队**等它关完 → 幂等 200 |

**排队必须 async 轮询**：服务是**单进程** uvicorn（单事件循环），同步
sleep 会把整个服务卡住 —— 而 DELETE 本身就在同步阻塞事件循环（unload
RPC + SIGTERM + 3s 宽限 + SIGKILL ≈ 4–5s）。

**排队醒了之后要持 per-model 锁 + 重新求值**：多个请求可能同时醒来，
一起 bind 又变成「两个 init 线程抢 NPU 内存」。且**已经 running 的
实例再「启用」必须幂等跳过**（不再 bind）—— worker 侧虽然 running
时幂等，但多一次 RPC 没意义。

**「刚停过」要记一笔（`_recently_stopped`）**：单进程下并发 DELETE
只能在事件循环里排队，等它轮到时「关闭中」早已结束、实例的
`npu_index` 也被乐观写重置成 -1 → 按卡号查不到 → **误报 404**。
只有记住「刚停过」才能把它判成幂等成功。DELETE 本来就是幂等语义。

**前端**（`system.js`）：`transition` 非空 → toggle / 重启按钮禁用 +
提示「正在关闭中」；`_computeAction` 加 per-model pending 锁防连点。
**向导**（`wizard/state.js`）：「一键开启」第 1 步**停非必需**时，
**正在启动中的实例也要停**（DELETE 带 `npu_index`，后端排队等它加载完
再停，返回时卡已腾空）。不能跳过 —— 跳过的话那份继续占显存 → 新模型
加载时显存不足 → worker rc=-6 起不来（真机实测：vd 的 omni 正在 npu1
启动，vtd 要把 qwen3-vl-4b 启到 npu1 → 起不来，最后一步报失败）。
**「下一步」之前的腾卡顺序也是保证**：DELETE 全部返回后才 POST 启新模型。

**POST 返回 200 ≠ 成功**：后端 `POST /compute/instances` 单张失败不影响
其余卡，仍返 200 + `{ok: false, errors: [...]}`。调用方**必须看 body.ok**
——只看 HTTP 状态码会把「某张卡没起来」当成成功，用户一路走到完成配置
页才炸。

#### 过渡态守卫**只看本机实例** —— peer 的状态不该挡住本机启停（v0.29 修）

**症状**：单板机（1 张 RK1828 协卡）上，向导「一键开启所需模型」一直卡在
**「正在启动模型」**，音视频描述（视频描述功能）、视频目标检测（两者都要
`qwen3-vl-4b`）都复现。看起来像模型永远起不来。

**根因（不是 `05b53de` 引入的 —— 那个提交只是把 UI 的进度文案从
「正在开启模型…」细分成「正在释放计算卡 #N…」/「正在启动模型…」，
把真正卡住的那一步**显示出来**了；卡住本身更早就有）**：

`POST /compute/instances` 排队等过渡态时，问的是
`registry.transition_cards(model_id)` —— 它把**远端 peer（`_source ==
"adopted"`）的实例**也算进去。而 peer 实例的 `npu_index` 是**对端板子
的卡号**，和本机卡号数字相同纯属巧合。于是：

- 本机要启 `qwen3-vl-4b@card0`，peer（172.16.9.109）的 qwen3-vl-4b 恰好
  卡在 `loading` → 本机被挡住 → 排队满 `_QUEUE_WAIT_LOADING_S`(120s) →
  **409「仍在启动/关闭中，请稍后重试」** → 向导重试 → 再烧 120s →
  **看起来永远卡住**。
- 本机卡上其实**什么都没占**（peer 的显存在对端板上）—— 白白卡死。
- DELETE（腾卡）路径同样中招：`_transitions_of()` 也含 peer → 本机停
  同名模型时先等 peer 的 loading 结束 → 永远等不到 → 409 → 腾卡失败。

**修法**：`transition_cards(model_id, local_only=True)` 只看本机实例
（`spawned` / `adopted_local`），API 层的 `_await_quiet()` /
`_transitions_of()` 一律用 `local_only=True`。本机自己的过渡态**仍然要
挡**（那才是 rc=-6 的来源，别把守卫整个关掉）。

> **前端早就 peer-aware，后端不是**：`wizard/state.js` 的腾卡循环有
> `if (inst._source === "adopted") continue;`（peer 的算力本机无权启停），
> 后端的过渡态守卫却没做同样的区分 —— 这次修的就是这个不对称。

**顺带把失败原因摆到 UI 上**：后端的 409 文案（「仍在启动/关闭中…」）和
腾卡/启动失败的 detail 以前只进 `console.warn`，UI 上只剩「正在开启
模型…」/「开启失败,正在重试」——用户看到的是"卡住"而不是"为什么"。现在
`say()` 会把原因递到状态行，重试时也保留上一次的原因。

**回归**：`scripts/verify_compute_transition_guard.py` 的 **S6**（6 条断言：
peer 的过渡态不进本机视图 / API 层确实按 `local_only` 问 / 本机自己的
loading 仍然排队等）。

#### bind 之后必须重新拉起 probe —— 否则实例永久卡在 `loading`（v0.29 修）

**症状**：同一条「一键开启所需模型」卡在「正在启动模型」的**第二条路径**
（与上面 peer 那条无关，纯本机、自伤）。

**根因**：`_probe_instance` 在**终态**（worker 报 error / worker 进程退出）
会 `return` —— 它是"一次性"的。而 `bind_instance` 把 status 乐观写成
`loading` 之后是**靠 probe 接管**的（worker 的 init 跑在它自己的线程里，
load RPC 立即返 loading，状态全靠 probe 翻 running/error）。

于是：模型加载失败过一次（显存不足 rc=-6 / 卡号越界 / 模型目录缺失）→
probe 退出 → 用户再点「启用」（或向导「一键开启」重试）→ bind 又把 status
写成 loading → **probe 已经死了，没人再探** → **实例永久卡在 loading**。

后果和 peer 那条一样致命：启/停它的请求被过渡态守卫挡住（排队 120s →
409「仍在启动/关闭中」）→ 向导「一键开启」永远卡在"正在启动模型"。

**修法**：`registry._ensure_probe_thread(inst)` —— bind 之后保证有一个
**活着的** probe（已活着就不重复起）。实测（2026-10-23）：
第 1 次启 npu=1 → 加载失败 → `error`（probe 退出）；第 2 次启 → probe 被
重新拉起 → 状态正确落到 `error`（修复前永久 `loading`，DELETE 连试 6 次
全是 409，最后只能 kill 进程才收掉）。

**回归**：同一个脚本的 **S7**（6 条断言：无 probe 时新起 / 还活着时不重复
起 / 已死时换新的 / bind 后 status=loading 且有活 probe 接管 / load RPC
已发出）。

回归：`scripts/verify_compute_transition_guard.py`（25 断言，假 registry
驱动 API handler，不起服务不占 NPU）；`scripts/verify_wizard_bringup_loading_card.py`
（7 断言，mock 后端扫前端一键开启的 DELETE/POST 序列）；真机四场景见下节。

#### 探针把「上一轮残留的 error」当成本次终态 —— 重试后永远停在「启动失败」（v0.30 修）

**症状**：worker 侧某次加载失败（缺包 / rknn init 错 / 模型文件缺失）→
用户把原因修掉后**重试** → worker 其实加载成功了（真机实测 34.2s 就绪），
但算力页一直显示「启动失败」，错误文案还是**上一轮**的那句；能力也取不到
（`功能未启用: swcap_vision_chat`）。更糟的是 NPU 内存被白占着。

> 这是上面 `_ensure_probe_thread` 那次修的**同一个坑的另一半**：上次修的是
> 「probe 早已退出 → bind 后没人探 → 永久卡 loading」；这次是「重新起的
> probe 抢在 load RPC 前面读到旧 error → 写完 error 就自杀 → 永久卡 error」。
> 两个症状同源：probe 是**一次性**线程，终态就 `return`。

**根因**：三个动作凑在一起 ——

1. worker 侧 load 失败但**进程还活着** → state 停在
   `("error", 旧错误)`（`workers/_base.py:555-556`）；
2. 重试复用同一个 worker → `bind_instance` 乐观写 `status="loading"`，
   然后**先**起 probe（`registry.py:2742`）、**后**发 load RPC
   （`registry.py:2893`）。probe 的 info 调用持着 `inst._probe_lock`，
   load RPC 也要抢这把锁 → **probe 那条 info 先到 worker**，worker 回的是
   上一轮的 error；
3. probe 把它当成本次终态 → 写 `status="error"` 并 `return` → 没人再探。

修复前的 journal（同一秒内两条，顺序就是证据）：

```
[WARNING] instance qwen3-vl-4b @ npu=0 → error: ModuleNotFoundError: No module named 'tokenizers'
[INFO]    bind qwen3-vl-4b @ npu=0: async load started, deferring to probe
```

**修法**：`registry.ModelInstance._load_pending` —— 「本次 load 已发起、但
**还没被 worker 确认**」的标志。worker 的 `load_handler` 是**同步**把
`state["status"]="loading"` 写好才回 load 的 RPC
（`workers/_base.py:494` → `:562`），所以「load 被确认」≡「worker 侧状态
已经是本轮的」→ **ack 之前 worker 报上来的 error 必然属于上一轮**。

- `bind_instance` 乐观写时置 `True`（**必须在起 probe 之前**，否则新 probe
  的第一次 info 读就看不到）；
- `_run_load` 的 `finally` 里清 `False`（ack / 失败 / 异常都覆盖）；
- probe 收到 `error` 时：`_load_pending` 为 True → **忽略**这次陈旧 error
  并继续探；为 False → 照旧当终态（写 error + 退出探针）。

**别把 `_probe_gen` 当成解法**：load 路径没有 bump 它，且 probe 的 gen 快照
取在发调用**之前**（`registry.py:4213`）—— bump 早于快照就完全无效，
还是竞态。

修复后同一场景的 journal（重试后**没有** "→ error" 那一行）：

```
08:06:39  bind qwen3-vl-4b @ npu=0: async load started, deferring to probe
08:06:40  instance qwen3-vl-4b @ npu=0 → error: FileNotFoundError: ...   ← 真错误，终态
08:06:42  bind qwen3-vl-4b @ npu=0: async load started, deferring to probe ← 重试
08:07:16  instance qwen3-vl-4b @ npu=0 → running (load=34.2s)             ← 收敛
```

回归：`scripts/verify_probe_stale_error.py`（14 断言，不占 NPU）—— T1 探针
忽略陈旧 error 且能收敛到 running / T2 真 error 仍是终态（防修过头）/
T3 标志生命周期（ack 前 True、ack 后归零、起探针前已置位）/ T4 走真
`bind_instance` 的端到端。**该脚本在修复前是 4 通过 10 失败**（T1、T4 全红，
症状与真机一致）。

#### 启模型前必须校验卡位 —— 越界的 npu 会造出「僵尸实例」（v0.29 修）

**症状**：`POST /api/compute/instances` 传一个**本机没有的卡号**（单卡板传
`npu_index=1`）→ 200 → 实例起来却是 `error`（worker 侧
`IndexError: npu=1 out of range`）→ 而且这个实例**删不掉**（DELETE 连试
6 次全 409），后续所有启停都被它堵在 120s 排队上。

**根因**：`InstanceRequest._npu_list` 只校验 `>= 0`，**卡号是否真的存在**
从没查过 —— docstring 早就承诺「npu_index 越界 → 400」，代码没兑现。请求
一路走到 worker → `LLMSession(model, npu=1)` 抛 IndexError → 进程秒退 →
registry 里留下一个 `status=error` 却**占着卡位**的实例（过渡态守卫把它
当成「占着」→ 启停排队 120s → 409）。

**修法**：`registry.npu_card_error(npu_index)` —— 在**过渡态排队之前**
校验（排队要白等 120s，越界的卡号等再久也起不来）：

| 情况 | 判定 | 提示 |
|---|---|---|
| 卡号在 `model_cards` 里 | 通过 | — |
| 卡号超出探测到的卡数 | 400 越界 | 「本机只有 N 张卡（可用卡号 0~N-1）」 |
| 卡在，但不是 PCI 形态 | 400 非算力卡位 | 「能承载 RKNN 模型的卡号是 [...]」 |
| 被 `compute.card_count` 限掉 | 400 | 「card_count=1 只允许使用卡号 [...]」 |
| **卡位探测失败**（`/dev/rknn3` 被 worker 占着） | **放行** | 未知 ≠ 越界 |

**探测失败时放行**是刻意的：`list_cards()` 的探测有 5s 超时，而 worker 占着
`/dev/rknn3` 是常态 —— 那时卡位是**未知**的，当成「越界」会把正常的启用
请求全拒掉（整机都启不了模型）。

实测（2026-10-23，单卡板）：同一个 `{"model":"qwen3-vl-4b","npu_index":1}`
—— 修复前排队 120s 后 409；修复后 **7ms 返回 400**，detail =
「npu_index=1 越界：本机只有 1 张卡（可用卡号 0~0），算力卡位 [0]」，
registry 里**没有**留下任何实例。合法卡号（npu0）照常 200 → 42s → running。

> 400 的 detail 会摆到 UI 上（向导的「启动失败: …」/ 算力页的 alert）。
> FastAPI 的 body 是 `{"detail": "..."}` —— 前端要取 `.detail` 再显示，
> 直接摆 raw body 会让用户看到带引号的 JSON 串。

**回归**：`scripts/verify_npu_card_guard.py`（24 断言：U1 registry 判定的 6
种卡位布局 + U2 API 层「越界立刻 400，且不排队 / 不 spawn / 不 bind」）。

> **假 registry 的脚本要一并 fake 卡位**：`scripts/verify_multicard_enable.py`
> 的 harness 用的是**真的** `ComputeRegistry` 类（只 fake 了 Popen / probe /
> rpc / `/proc` 扫描）→ 新校验会真去探硬件 → 单卡板上 npu=1 被判越界 →
> 用例直接崩。已在那里的 `_install()` 里补上 `reg._cards`（两张 PCI 形态的
> 假卡）。凡是**用真 registry 类**做假硬件的脚本，都要记得这一点。

#### 真机验证脚本必须按**实际卡位**选卡（v0.29 修）

`scripts/verify_compute_transition_real.py` 以前默认 `--npu 1` —— 那是
**双卡板**的默认值。在**单卡板**上 npu1 根本不存在，而脚本照跑：POST
`{"npu_index": 1}` → 造出上面那个僵尸实例 → 脚本后面的断言全在测一个
**永远起不来的实例**（「等它加载完」必然超时）。

**修法**：

* 默认**自动挑卡** —— 读 `/api/compute` 的 `model_cards`，优先挑一张
  **没有别的模型占着**的卡位（一卡一模型，同卡叠模型 rc=-6）；
* 显式 `--npu N` 而 N 不在 `model_cards` → **直接退出**（退出码 1），并
  打印本机可用卡位。宁可不跑，也不要造僵尸。

> 写这类「会真启停模型」的脚本时，默认值必须**按实际硬件推导**，不能写死
> 某一块板子的布局 —— 脚本换块板子跑时，写死的卡号就是「不存在的卡」。

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

## 单计算卡降级策略（v0.24）

**枚举到的卡 ≠ 能跑模型的卡。** `list_cards()` 返回的是
`RKNN3Lite().get_devices_id()` 的下标序列，里面混着两类东西：

| device_id 形态 | 是什么 | 能不能跑 rknn 模型 |
|---|---|---|
| `0003:31:00.0`（PCI BDF） | 真正的 RK1828 协卡 | ✅ |
| `180B6D6C…`（USB 序列号） | 只在 RGA / VPU 侧可见的设备 | ❌ `vision init_runtime failed` |

所以「**算力卡位**」= 探测到的 PCI 形态卡数 = 本机能同时跑几个模型的硬上限
（每卡一个模型，见上文「一卡一模型」）。

```
registry.py::_PCI_BDF_RE          判定 PCI BDF 形态
registry.py::ComputeRegistry.model_cards()   → [npu_index, ...] 算力卡位
snapshot() 新增 model_cards / card_count 两个字段
```

**`compute.card_count`**（config.jsonc）是对探测结果的**上限**，不是强制值：

| 配置 | 探测到 | 实际卡位数 | 效果 |
|---|---|---|---|
| 不配 | 2 | 2 | 双卡 |
| `card_count = 1` | 2 | **1** | **双卡板模拟单卡板** |
| 不配 | 1 | 1 | 单卡 |
| `card_count = 2` | 1 | 1 | 仍是 1 —— 不能凭空变出卡 |

> `ComputeConfigPayload` **必须**声明 `card_count`：`PUT /api/compute/config`
> 是全量替换 `compute` section，未声明的字段会被 `extra="ignore"` 丢掉，
> 运维手写的 `card_count` 会在任何一次保存后静默消失（与当年
> `services` 那条静默数据丢失同类）。

### 各应用的模型需求与单卡行为

| 应用 | 能力组数 | 单卡行为 |
|---|---|---|
| 文本对话 / 语音生成 / 视频目标检测 / 视频目标定位 / 监控反查 `vr` | 1 | 模型落到唯一卡位，功能不变 |
| 智能电脑控制 `usb_hid` | 0 | 纯硬件，不碰 NPU |
| **音视频描述 `vd`** | **2** | **二选一**（视频描述 / 音频描述） |

> 组数 ≠ 能力数：`usb_hid` 若启用 AI 任务循环需要两项能力，但它们同组，
> 仍是**一个**模型需求。
> 「监控分析检索」已改名为「监控反查 `vr`」并**删掉分析功能只保留检索**
> （v0.25），组数从 2 降到 1 —— 它不再是「刚需两模型」的卡，
> 单卡下与其它单组应用一样。`vd` 是目前唯一需要二选一的卡。

### 实现手法：裁剪后的 effective cfg

单卡策略**全收在向导层**（前端）；后端只加只读探测字段 + autostart 裁剪。

`state.js::_planSingleCard(cfg, app, envCheck)` 在单卡时返回一份**裁剪过的
`_active.cfg`**：`swcap_to_model` 只留本应用单卡下真正要用的那一**组**
（整组保留，不拆散），`npu_index` 指向实际卡位。下游三处消费点全部从
`_active.cfg` 取值，**一行都不用改**就自动按单卡语义工作：

> **判定单位是「组」不是「能力」。** 一个组 = 一个模型。usb_hid 声明两项能力
> 但同组，所以是**单组 = 单模型**需求。若按能力数（`declared.length <= 1`）
> 判定，它会被误判成「刚需多模型」→ 单卡板上直接 blocked，整张卡不可用，
> 而它本来只需要一个模型 —— 这是本次重构里最容易踩的一处。
> `video-detection` 是两个独立组，所以仍然需要 `single_card_pick` 二选一。

| 位置 | 用途 |
|---|---|
| `state.js:1359` `_finish` | 等待模型 running |
| `state.js:1427` `_waitForModelsRunning` | 进度条 target 展开 |
| `state.js:1767` `_wizardBringUpModels` | 一键开启（停非必需 + 启必需） |

> 「停非必需」那一步在单卡下自动变成「先把旧模型停掉再加载新的」——
> 这正是单卡要的语义，不需要额外代码。

**双卡路径逐字不变**：`card_count !== 1` 时 `_planSingleCard` 返回 `null`，
`single` 分支一条都不走。探测失败（`card_count` 缺失 = 0）也走原路径。

### 二选一怎么声明

应用在自己的 `wizard.json` 里声明单卡下能二选一的是哪两个，以及选了它
之后**哪些步骤不再需要**（`skip_steps`）：

```json
"single_card_pick": [
  { "cap": "swcap_vision_chat",   "label": "视频描述功能",
    "skip_steps": ["audio"] },
  { "cap": "swcap_audio_chat", "label": "音频描述功能",
    "skip_steps": ["detect"] }
]
```

没声明 = 刚需多模型 → 单卡直接判定为不可用：环境检查页显示红色
阻断提示、**不渲染**「一键开启」按钮、「下一步」禁用（但保留「跳过向导」，
让用户能离开去看应用页的错误提示）。

**`skip_steps` 为什么必要**：vd 的两个 capability 各自对应向导里的一步。
选了「视频描述」却还要问用户「音频理解提示词」，用户填了 → 完成配置 →
`applyWizardSnapshot` 里 `if (opts.audioPrompt)` 成立 → 去点「分析音频」→
单卡上那个模型压根没加载 → 后端 502「功能未启用: swcap_audio_chat」
→ 弹窗。

裁剪落在**同一份 effective cfg** 里（`state.js::_stepsWithout`）：
`swcap_to_model` 裁 capability，`steps` 裁步骤。下游全是现成逻辑：

* 被裁步骤的字段不进 `_finish` 构造的 opts → `if (opts.audioPrompt)` /
  `if (opts.prompt)` 自然不成立 → 那个功能**压根没被请求**
  （不是把弹窗藏起来，是请求本身消失了）
* 步骤指示器 / 完成摘要 / 「上一步」「下一步」全部按裁剪后的步骤表走

> **步骤恢复必须存「步骤 id」而不是下标。** 裁剪会让下标整体错位：
> 原来 index 3 = "audio"，裁掉 audio 后 index 3 = "finish" —— 按旧下标
> 恢复会让用户一进来就落在「完成配置」上，前面的步骤直接被跳过。
> `_lsSaveStep` 改存 step id，`_resolveStepIdx` 按 id 解析
> （旧版本 localStorage 里的数字按下标兜底；存的步骤已被裁掉 → 回第 1 步
> 重新确认环境与二选一）。

`skip_steps` 只在单卡分支生效，双卡时 `cfg.steps` 一个都不动
（`verify_wizard_single_card.mjs`【G】成对断言双卡 5 步 / 单卡 4 步）。

**单 capability 的应用（其余 6 张卡）在单卡下不显示任何提示** —— 它们
的行为与双卡完全一样（模型照常加载，只是落到唯一那张卡），没必要打扰
用户。只有「完整功能需要 2 张卡」的应用才值得提示。

### 已知竞态（既有缺陷，v0.24 未修）

`mount()` 在 `await _probeEnv()` **之前**就把容器插进 DOM，而 `_active`
要等 await **之后**才赋值。这个窗口期（约一次 `/api/compute` 往返）内：

* `unmount()` 看到 `_active` 还是 null → 直接 return → **摘不掉上一个
  容器**；
* 于是交叠的两次挂载会留下**两个**向导容器，旧的那个还会把内容渲染到
  新页面上。

触发路径：框架的 home 页会**异步**跳到默认应用
（`location.hash = "#" + appId`）并挂它的向导；用户在这段窗口里点了
另一张卡，两次挂载就会交叠。

**v0.24 试过在 `mount()` 里加挂载序号 + hash 校验来作废过期挂载，
但两者会互相打架**（框架对同一页有**两个** tryMount 调用点
`framework.js:426` 和 `framework.js:496`，`_wizardMounted` 要等
tryMount 返回才标记 → 同一页可能并发挂两次 → 序号守卫会把**正确**
的那次也作废掉，向导直接不出现）。已回退，**保持原行为**。

要修的话需要重新设计：正确的方向是「**晚到的挂载不能顶掉已经就位的
向导**」—— 挂载完成前不碰 `_active`、不卸载前一个，等自己被确认是当前
页面的向导之后再顶替。**别用序号守卫硬做**，那会把「同一页并发两次
挂载」误判成「有人比我新」。

`_wizardBringUpModels` 另有一道显式护栏：被阻断的应用**一个模型都不启停**
—— 既不加载，也不去停别人正在跑的模型（「停别人的模型」是破坏性的）。

### 开机自启也要裁

`boot_compute_services()` 按算力卡位裁 `compute.autostart`：

* 指向「跑不了模型的卡」的条目 → 跳过（否则开机就是一屏 rc=-6 的 error）
* **每张卡只保留第一条** —— 一卡一模型是硬约束（[见「一卡一模型」是不变量](#一卡一模型是不变量由三层共同保证v025-修)）。
  这条**不限于单卡**：双卡板上同卡挂多条同样要裁（配置是
  `set_active_app` 增量追加来的历史沉积）。

### 回归

| 脚本 | 覆盖 |
|---|---|
| `scripts/verify_wizard_single_card.mjs` | 单卡策略的纯逻辑（63/63）：双卡零变化、vd 二选一、**步骤裁剪**、finish 的 opts 契约、vr 单卡不阻断、探测失败不误判 |
| `scripts/verify_single_card_ui.py` | 真实浏览器（36/36）：提示块、二选一 radio 切换、一键开启的 POST 落 NPU0、vr 不阻断、**vd 步骤裁剪 + 完成配置后不碰「分析音频」**、无 JS 报错 |
| `scripts/verify_wizard_env_step.mjs` | 向导结构回归（59/59） |

> `verify_wizard_env_step.mjs` 的私有函数注入锚点用
> `window.WizardState = {` 的**开头**而不是整个导出语句 —— 导出对象的成员
> 列表随功能增删变过好几轮，按整句匹配会在形状变化后**静默失配**，
> 脚本报「Cannot read properties of undefined」看起来像被测代码坏了，
> 其实是脚手架自己过期了。

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
  VR 页 + 微信管线）也走同一条 `_bgr_to_path` / `decode_attachments`
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
  （仅 `video/search.py` 还在 import）。

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
  + VR 拿到空 `_raw.text` → 占位事件（"不崩但功能全废"）。
  v0.8.30 的 `get_session` 本机优先 / `embed_video` path-list
  无 is_remote 守卫同源（见"get_session 本机优先"段）。

  **v0.15 的 LAN 降级已作废（v0.16）**：当年 `is_remote=True`（跨机
  worker）时，worker 会把 4 个 SHM 路由块（`crop_b64` / `result_data` /
  `structured` / `text`）**全部 drop**，因为对端读不到本机
  `/dev/shm`，而内联又会撞 FastCDR 255 字节硬截断。代价是 VR Path 1
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

* **VR per-frame timeout 方案B (10s → 15s, 配套 wire RPC 10s → 15s)**:
  - 5c33fb3 设 5s → 后续 10s 都覆盖 qwen3.5-vl-4b schema-echo 健康路径
  - 但线上仍偶发 `(推理失败：超时 10s SDK 未响应)`, 真机 NPU 暂忙 /
    prefill 长 / 其他并发偷 NPU 让单次跑 10-12s
  - 方案B: 默认 15s 给 5s 余量, 跟 wire RPC 同步 (避免 wire 先触底
    留下 `discovery_pending` 误报错误)
  - 下一步: telemetry (commit cd74486 加的 _record_infer_duration 60s
    窗口 p95 告警) 收一晚真实 p50/p95/p99 数据决定 15s 是否够, 不够
    再调 (但若 ≥ 30s 就要查 NPU / 长 context 真因, 不应再加 timeout)
  - 真机 VR 服务需 restart 才能让新默认值生效

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

Both internal SDKs live behind SSH on `git@github.com:rockchip-wardner/`.
They are **not** in `requirements.txt` (intentional — the host SSH key +
network decide whether the install succeeds). Run them by hand after
cloning:

```bash
.venv/bin/pip install -e "git+ssh://git@github.com/rockchip-wardner/wdnav.git#egg=wdnav"
.venv/bin/pip install -e "git+ssh://git@github.com/rockchip-wardner/wdnai.git#egg=wdnai"
```

If `git clone` fails, the host either lacks SSH access to `github.com` or
the deploy key isn't loaded. On an isolated board with no internet, use
the internal mirror instead (`git+ssh://git@10.10.10.10/wardner/<pkg>.git`)
— that's the path `update_rkwhl.sh` takes for field deployments.
国内网络拉 GitHub 慢时，还可以从 Gitee 镜像拉
(`git+https://gitee.com/rockchip-wardner/<pkg>.git`) —— **只拉不推**，
三个平台的完整地址见上文「仓库地址」。

**wdnai 的依赖不会自己进 venv —— `transformers` / `tokenizers` 必须由
wdnwebui 的 `requirements.txt` 提供。** `update_rkwhl.sh`（以及上面那条
手工安装）用的是 `pip install --no-deps -e`，`--no-deps` 直接跳过依赖安装，
所以 wdnai 的 pyproject 里声明的 `transformers>=4.40` / `tokenizers>=0.15`
**永远**不会自己被装进 venv。缺任一个的典型症状：

* 算力页报「`「qwen3-vl-4b@0」启动失败：ModuleNotFoundError: No module
  named 'tokenizers'`」；
* worker 日志（`/tmp/wdnwebui-worker/<model>.log`）里是
  `LLMSession init failed for qwen3-vl-4b @ npu=0: ModuleNotFoundError: ...`。

故障链：每个 RKNN 模型目录都自带 `*.tokenizer.gguf` →
`wdnai.backends._common.load_tokenizer()` 对 `.gguf` 走
`wdnai.gguf_tokenizer.load_tokenizer_from_gguf()`，那里**先**
`from tokenizers import Tokenizer`、**再**
`from transformers import PreTrainedTokenizerFast` —— 所以先炸的永远是
`tokenizers`，`transformers` 只是紧随其后（两个都缺，但报错只暴露第一个）。
`gguf` 是同一类问题（已声明在 `requirements.txt`）。
修法：`.venv/bin/pip install -r requirements.txt`（装的是 transformers +
tokenizers + 它们的传递依赖，**不需要** torch）。

**别再靠「症状反推缺哪个包」了 —— `scripts/check_sdk_deps.py` 把这整类故障
提前到安装期。** 判据源是 **SDK 自己的 pyproject**（不是 requirements.txt）：
读 `.venv/src/*/pyproject.toml` 的 `[project].dependencies`，逐条查**已装**
发行版（`importlib.metadata`，**不查索引** → 离线可用），并**真的 import
一次**。

> **为什么必须真 import**：`find_spec` 只能回答「装没装」，答不了「装了能不能
> 用」。上游 `v4l2` 0.2 就是这一类 —— 装了，py3 下 import 阶段抛 **TypeError**
> （见 `scripts/fix_v4l2_py3.py`）。审计的判据是「能 import」，不是「有 dist-info」。

> **为什么不让 pip 去解析依赖**（即 `--no-deps` → 强制解析是否合理）：
> 私有包不在 PyPI（`wdndds` → `wdnai`、插件包 → `wdnhid` / `wdnav`），fresh
> venv 必挂；实测 `pip install --dry-run -e .venv/src/wdndds` 会
> **`Would install opencv-python-5.0.0.93`** —— GUI 版 opencv 和 headless 版
> 抢同一个 `cv2` 包；`av` 需手挑版本；本机连不上 PyPI，强制解析会卡住。
> `--no-deps` 是**选过的**，审计是对冲，不是要改它。

挂载点：`update_rkwhl.sh`（所有包跑完之后，审计不过则整脚本 exit 1）、
`bootstrap_wdndds_native.sh`（Step 7，只 warn 不 die —— 它自己的活已经干完）。
退出码：0=全就绪 / 1=有缺失或装坏 / 2=找不到 SDK 源码树。

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
`git@github.com:rockchip-wardner/{wdnav,wdnai,wdndds}.git`. Two of them
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
`.venv/src/<sdk>`, run `./update_rkwhl.sh`.** It `git pull`s every
editable package under `.venv/src/` and forces
`pip install -e --no-deps` on each whose HEAD advanced, which is what
actually triggers the rebuild hooks. The previous
heuristic of "only reinstall when `pyproject.toml` changed" missed
wrapper-only commits — root cause of the wdndds
`undefined symbol: wdndds_topic_writer_create` incident after commit
871d00f (`_cshim.py` gained new ctypes bindings, `pyproject.toml`
untouched). `update_rkwhl.sh` now rebuilds unconditionally on any HEAD
advance. For one-off sync of a single SDK, `pip install -e
--no-deps .venv/src/<sdk>` is the minimum; on a
fresh host, `bootstrap_wdndds_native.sh` is still required for apt
deps + native lib vendoring.

`update_rkwhl.sh` 同样驱动三个**卡片插件包**（`wdnapps_video_analyzer` /
`wdnapps_hdmi_processor` / `wdnapps_audio_chat`），但它们是 pure-Python、没有 `.so` 可断言，改由
`verify_plugin_cards` → `scripts/check_plugin_entrypoints.py` 校验
「entry point 能解析 → manifest `"id"` 与 entry point 名逐字相同 → 卡片
目录有 `static/`」。这组断言挡的是另一个静默失败：entry point 名照着包目录
写下划线、而卡 id 是连字符时，pip 全程报成功，宿主启动却把这几张卡 FATAL
跳过。

> 包名一律传**下划线**（`wdnapps_video_analyzer`）。pip 规范名是连字符的
> `wdnapps-video-analyzer`，但 site-packages 上实际生成的是
> `wdnapps_video_analyzer-0.1.0.dist-info` 和
> `__editable__.wdnapps_video_analyzer-0.1.0.pth`——实测传连字符名时
> `purge_install_traces` 的 glob **一个都命中不到**。

## `vrepo` —— 工作区多仓库统筹（对标 Android `repo`）

工作区里其实有 **8 个独立 git 仓库**（不是一个 submodule 树）：

| 仓库 | 路径 | 远端 | 说明 |
|---|---|---|---|
| wdnwebui | `.` | `origin` | 本仓（.venv/ 已被 .gitignore 排除） |
| wdnai | `.venv/src/wdnai` | `origin` | editable 安装，VLM/LLM SDK |
| wdnapps_audio_chat | `.venv/src/wdnapps_audio_chat` | `origin` | editable 安装，音频卡片插件（语音生成 + 流式语音识别） |
| wdnapps_hdmi_processor | `.venv/src/wdnapps_hdmi_processor` | `origin` | editable 安装，HDMI 卡片插件 |
| wdnapps_video_analyzer | `.venv/src/wdnapps_video_analyzer` | `origin` | editable 安装，视频卡片插件 |
| wdnav | `.venv/src/wdnav` | `origin` | editable 安装，MPP/RGA/V4L2 |
| wdndds | `.venv/src/wdndds` | `origin` | editable 安装，DDS/RPC |
| wdnhid | `.venv/src/wdnhid` | **`rk`** | editable 安装，USB Gadget HID |

根目录的 `vrepo`（纯标准库、单文件、可执行）把这 8 个仓当**一个 manifest**
统筹，避免「逐个 cd 进去手工来一遍」：

```bash
./vrepo status            # 一屏看清 8 个仓的状态（最常用）
./vrepo status --fetch    # 先刷新远端跟踪引用再看
./vrepo sync -j 4         # 并行 fetch + ff-only 前进
./vrepo sync --reinstall  # 顺带重装 HEAD 变动 / 版本漂移的 editable 包
./vrepo doctor            # 一致性体检
./vrepo forall -c 'git log -1 --oneline'
./vrepo log -n 30         # 跨仓库合并的提交流
./vrepo grep 'LLMSession' -i
```

零配置：没有 manifest 时自动扫描 `.` + `.venv/src/*`；`./vrepo init` 才落盘
`.vrepo/manifest.json`（该目录已 gitignore —— 里面记的是各机器的本地路径）。

**四个必须知道的坑**（都在代码里处理了，但改脚本时别改回去）：

1. **远端名不一定是 `origin`** —— wdnhid 用的是 `rk`。工具从 upstream
   反推远端名，取不到再回落到第一个已配置远端。写死 `origin` 会让 wdnhid
   的 fetch/push 直接失败。
2. **ahead/behind 可能是旧账** —— 它们来自**本地**远端跟踪引用。`fetch`
   之后才准；`status` 默认不联网（快），要准就加 `--fetch`。
3. **本地有提交时 push 会被拒** —— 非快进失败时工具提示
   `vrepo sync --rebase`（把本地提交 rebase 到远端之上），而不是干巴巴报错。
   `sync` 默认 `merge --ff-only`，**绝不**自动产生 merge commit。
4. **editable 安装会漂移** —— 源码 `pyproject.toml` 的版本和 site-packages
   里装的可能对不上（本仓 wdnai 就出现过装 0.6.0 / 源码 0.7.0）。
   `doctor` 点名，`sync --reinstall` 重装。

**与 `update_rkwhl.sh` 的分工**：`vrepo sync --reinstall` 覆盖的是
「拉最新 + `pip install -e --no-deps`」这一步，且**跨全部 7 个仓**
（含本仓与两个卡片插件包）。但 `update_rkwhl.sh` 仍独占三件事 vrepo 不做：
`verify_outputs`（装完校验 `.so` 真的在位）、`purge_install_traces`
（清旧 `.pth` 痕迹）、目录缺失时从 SSH URL 装。**新机器**仍必须先跑
`bootstrap_wdndds_native.sh`（apt 依赖 + native lib 铺开）——
`vrepo` 不碰这些。

`doctor` 另外会查：`index.lock` 残留（上次 git 操作崩过）、没有 upstream
的仓（`sync`/`push` 会静默跳过它）、`.venv/bin/python` 是否存在。

## Layout

```
wdnwebui/
├── run.py                              # uvicorn.run("app.main:app", 0.0.0.0:8000, reload)
├── bootstrap_wdndds_native.sh          # wdndds .so build (apt + stub + pip; since v0.6.1)
├── app/
│   ├── __init__.py                     # __version__ = "0.9.3" (single source)
│   ├── main.py                         # create_app() — discover_cards + register APIs + boot_compute
│   │
│   ├── core/                           # 共享内核(原 app/services/*)
│   │   ├── manifest.py                 # 插件清单加载 + 校验 (v0.4 重构)
│   │   ├── html_assembly.py            # 首页 index.html 拼装(纯函数,缺卡位自动摘除)
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
│   │   ├── tts/                        # 语音生成 (swcap_text_to_speech)
│   │   ├── video_retrieval/            # 监控反查（只检索 swcap_vision_embedding）
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
│       ├── index.html.template         # 构建模板 — 见 core/html_assembly.py:assemble_index_html
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
- 共享资源:视频缓存(`app/core/video/cache.py`)、LLM 工具注册(`app/core/tools/registry.py`)、算力注册中心(`app/core/compute/registry.py`)全部由 core 提供,各卡片走各自入口(例如 video_retrieval + wechat_video_manager 都用 `get_video_cache()` 单例)。

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
  estimated_init_seconds=50 / capability_types=["swcap_audio_chat"] /
  worker_name="audio-3b"（comm="worker:audio-3b", 13 字符）。
* 该 manifest 的 `spec_factory="qwen_2_5_omni_3b"`，worker 侧据此调
  `SLM.qwen_2_5_omni_3b()`；`comm_name="audio-3b"` 来自同文件
  `workers/qwen2.5-omni-3b.json` 的 `worker_name`（`compute_worker_lazy.py`
  已删除）。
* `_LEGACY_CAPABILITY_TYPE_RENAMES` 加 `"audio_understand" → "swcap_audio_chat"`，
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

错误码与 detector / VR 对齐：502 + `RuntimeError("功能未启用: swcap_audio_chat")`。

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
| NPU 互斥 | 进程级 `_llm_session_call_lock` 串行 | 与 detector / VR / text_chat 共享同一把锁 |

`_check_npu_memory_budget()`（`compute_registry.py`）自动纳入 omni preset
校验；同卡内存超限在 `boot_compute_services` 时被拒绝。

### 已知限制

* **音频源必须含音轨**：PyAV `streams.audio` 为空 → worker 进入 `_STATE_ERROR`，
  前端 `alert("视频无音轨")`。
* **极短音频**（< 6s）：整段算一段，shape 1 (n=300) 或 shape 0 (n=600)
  补零处理（omni 内部行为）。
* **超时熔断**：单段 SDK 卡死没有 timeout；上层若需要可包一层
  `asyncio.wait_for(...)`，但当前按 detector / VR 现有风格不做。
* **不做二次聚合**：60 秒以上音频不会自动生成"综述"；如未来需要，
  加一段独立的 LLM 调用（建议复用 `swcap_text_chat`，即 qwen3.5-vl-4b）。

## Surveillance page (视频监控) — **已下线（v0.4 重构）**

> **本节描述的是 v0.4 之前的 16 路 4×4 网格监控页面;v0.4 起已下线。**
> 视频能力整合进微信视频检索管线 (`app/system/im_bridge/video_manager.py`)
> 与 监控反查页 (`app/apps/video_retrieval/`)。
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

**autostart 一律本板自己起，不借 peer 的算力** —— `npu_index` 是本板物理
卡号，声明了就要在这张卡上跑。借 peer 是**运行期**的特性（`get_session`
找不到本机实例时才用 peer 的 worker，见「多板 LAN 集群」），不是开机自启
的语义。

**每张卡一条 autostart**。同一个模型出现在两条里（不同 `npu_index`）是
合法的，会生成两个 worker 各占一卡；同一个 `npu_index` 出现两次则被
`compute.autostart 含重复 model` 校验拒掉（因为那才是真撞车）。

### 「autostart 声明的是本板跑」—— peer 空壳不能截胡（v0.26 修）

`compute.autostart` 声明的是「**这台板子**开机要跑哪几个 worker、各绑哪张
卡」，`npu_index` 是**本板**物理卡号。boot 时只有一种情况可以不自己起：
**本板这张卡上已经在跑**（running / loading）。

判定收敛成一个纯函数 `_autostart_plan()`，返回 `skip` / `spawn_local`。
判据：**本机实例**（spawned / adopted_local）+ 状态 running / loading。

**踩过的坑（勿回退）**：

* **「借 peer 的算力」在开机自启上是脆的** —— 旧逻辑在 peer 回应时就
  判成「算力归对方，本板不动」。但借来的算力会**静默失效**：peer 那边
  卸载了（退出卡片 / 重启）本板就没人跑，而本板声明的那张卡一直空着 ——
  症状和「没自启」一模一样（真机实测：借了 .109 的 qwen3-vl-4b，几分钟后
  对方卸了，模型谁都没跑）。autostart 要的是**确定**：本板自己起，一定
  起来。借 peer 是运行期 `get_session` 的事。
* **peer 的「空壳」不能算「算力归对方」** —— peer 那边**进程在、模型没
  加载**（`not_loaded`）时，`_adopt_existing_worker()` 照样会把它 adopt
  过来（它只在 `status == "error"` 时才拒绝）。旧代码因此判成「算力归
  对方，本板不动」而**放弃本板启动** —— 真机症状：重启后两个 autostart
  模型一个都没起来，日志是
  `boot: autostart X → adopted adopted (peer ... status=not_loaded); 不发 load`。
  多板还会互相 adopt 对方的空壳 → **谁都不加载的死锁**。
  现在 autostart 路径**一律本板自己起**，peer 完全不参与判定。
* **「peer 已经 loaded」是前提，不是结论** —— `boot_compute_services()`
  的 docstring 写着「a model already **loaded** by a LAN peer is not
  spawned again」，但代码从没检查过这个前提。docstring 写前提、代码不兑现，
  是最难查的一类 bug；改判据时先确认 docstring 的前提真的被代码 enforce。
  （v0.26 起这条前提本身也取消了：autostart 只管本板。）
* **`info` 报 `npu_index` 只在 load 之后** —— worker 没加载时
  `npu_index` 是 `None`（主进程侧记成 -1），cmdline 上的卡号在另一个字段
  `npu_index_startup` 里。所以「按卡号找本机实例」时，没加载的 worker
  一律落在 npu=-1 上，别把它当成「绑在别的卡」。
* **boot 路径不许 adopt peer** —— `_boot_spawn_async()` 内部固定
  `allow_adopt=False`：autostart 声明的是本板这张卡，捡回 peer 的实例
  （哪怕它正在跑）也会让本板这张卡空着，而且借来的算力随时会消失。
* **复用到的残留 worker 要补 load —— 但先问 worker** —— `/proc` 孤儿被
  登记成 `adopted_local` 时状态是**硬编码**的 `not_loaded`（主进程不知道它
  有没有 `--npu`），它不会自己加载 → 不补一次 load 的话，autostart 声明
  的模型永远停在 `not_loaded`。但补之前**必须 `info` 问 worker**：已经在
  loading/running 就别插手（两条 load 抢同一个 session slot → worker
  rc=-6），问不到就别发。判定抽成 `_autostart_needs_load()`。
* **徽章的真源是 config，不是实例上缓存的标志** —— 算力页的「开机自启」
  徽章（连同算力页 radio 的默认卡位）一律从 `config.compute.autostart`
  **现算**（`_autostart_npu_map()` + `_with_autostart_markers()`）。实例上
  的 `_autostart` / `_default_npu_index` 只是 **boot 时的快照**：运行期进
  卡片时 `set_active_app()` 把模型追加进 config，实例上的标志还是空的 →
  页面不显示「开机自启」（真机症状：跑完音视频描述，config 里明明两条，
  页面只有 [VLM] 视觉描述v3 有徽章，[Omni] 多感知描述v2.5 没有）。同一
  模型的两张卡还会一个有徽章一个没有 —— 取决于 boot 循环有没有扫到它，
  纯时序问题。
  **peer（网络算力栏）的卡不显示徽章** —— 开机自启是**本板**的声明，跟
  对端板子的卡无关。

### 「一卡一模型」是不变量，由三层共同保证（v0.25 修）

一张 RK1828 协卡装不下两个模型 —— 同卡叠第二个模型 `rc=-6`，stderr 为空
（见 [单卡 NPU 容量约束](#单卡-npu-容量约束两张卡--一张卡装两个)）。
**这是物理约束，不是配置偏好**，所以「每张卡最多一条 autostart」是硬
不变量，三层各管一段：

| 层 | 谁 | 做什么 |
|---|---|---|
| 配置生成 | `set_active_app()` | 追加 `(model, npu)` 时把**同卡的其它条目替换掉**（旧条目必然陈旧：运行期同卡只可能有一个 running 实例） |
| 配置解析 | `_parse_autostart_config()` | 拒掉「同模型同卡」重复 |
| boot | `boot_compute_services()` | `_crop_autostart_for_cards()`：非算力卡的条目丢弃 + **每卡只留第一条** |

> boot 那层的裁剪**不是「猜测性预检」**：卡位唯一性是已实测确认的硬
> 事实，不是按 `memory_estimate_mb` 猜显存还剩多少（后者会误杀合法
> 配置，上文 v0.17 明确反对）。被裁掉的条目**只记日志不改 config** ——
> config 是用户的意图，boot 无权替他改。

**踩过的坑（勿回退）**：

* **`--npu` 的卡号 0 是 falsy** —— 判定「要不要带 `--npu`」时**不能拿
  `npu_index` 自己的值当布尔用**。写成
  `(npu_index if eager_load is None else eager_load)` 时，卡号 0 的
  autostart 条目会静默变成「不带 `--npu`」→ worker 起来了却**永远不
  加载模型**（`npu_index: null` + `session not loaded`），而 boot 日志
  照样打印 `spawned with --npu=0`。真机实测：npu1 的条目正常加载，
  npu0 的条目全部 `not_loaded`。现在命令行的 `--npu` 参数一律由
  `_worker_npu_arg(npu_index, eager_load)` 生成，「要不要 eager-load」
  的判定集中在 `_wants_eager_load(eager_load)`。
* **boot 日志不能说「spawned」除非真的是新建进程** —— `ensure_worker_for_model()`
  可能返回一个**复用**的实例。原来那句硬编码的
  `boot autostart: %s spawned with --npu=%d` 把复用说成新建，正好盖住了
  上面那个 bug。现在直接读 worker 进程的真实 argv，报**实际**的
  `--npu` 值（复用实例则报「复用已绑定 npu=N 的实例」）。
* **adopted（LAN peer）实例会截胡「同模型跑两张卡」** —— peer 上有该
  模型时，第二个卡位直接复用 peer 的实例（`pid=0`），本机**不会**为它
  再起一个 worker。这不是 bug：peer 的算力是共享的（它不占本机的卡），
  本机重复加载反而浪费一张卡。只有 **spawned / adopted_local** 实例才
  参与卡位判定（`_instance_reusable()`）。
* `_cleanup_unregistered_workers()` 必须排在 spawn 循环**之前**。排在
  后面会和 fire-and-forget 的 spawn 线程竞争 `tracked_pids`，把刚
  `Popen` 但还没登记进 `_instances` 的 worker 判成孤儿杀掉 —— 实测首个
  autostart 模型在 spawn 同一秒以 `rc=-15` 挂掉。
* `_spawn_compute_worker` 里的 `/proc` 孤儿扫描必须先扣掉本 registry 已
  跟踪的 spawned pid。autostart 两条同模型不同卡时，第二条必然 `/proc`
  命中第一条刚起的 worker，不扣就会把它抢成 `adopted_local`。详见
  [同一个模型跑两张卡](#同一个模型跑两张卡v019算力页运行在多选) 的坑 1。
* **未绑卡（`npu_index == -1`）的实例只能在「算力页 spawn→bind」路径上
  复用**。autostart 路径要的是「worker 带着 `--npu N` 自己加载」，复用
  一个没带 `--npu` 的 worker 达不到效果，而 autostart 路径**不会再补
  一次 bind**（补了就是两次 load 抢同一个 session slot，worker rc=-6）。
  漏了这条就是「同模型配两张卡，第二条卡位被静默吞掉」。
* `hidden` 语义已从 `compute.services[].autostart == "hidden"` 迁到
  manifest 的 `hidden` 字段，这样配置写坏了也不会误隐藏模型。

回归：`scripts/verify_autostart_card_uniqueness.py`（32/32）—— 覆盖
`_wants_eager_load` / `_worker_npu_arg` / `_crop_autostart_for_cards` /
`_merge_autostart_entry` / `_instance_reusable` 五个纯函数（含「0 号卡
到底带不带 `--npu`」的命令行级断言），以及「任意追加序列后每卡仍只有
一条」的不变量。

`scripts/verify_autostart_boot_plan.py`（20/20）—— 覆盖
`_autostart_plan()`：本板这张卡在跑 → skip；其余一律本板自己起。重点是
「peer 不参与判定（不借 peer 的算力）」「peer 空壳（not_loaded）不能截胡
本板启动」「本板已有 not_loaded 实例时本板自己起」，外加
`_autostart_needs_load()` 的补 load 判据。

`scripts/verify_autostart_badge.py`（13/13）—— 覆盖「开机自启」徽章按
config 派生（实例上的快照会过期）、peer 卡不显示徽章、
`_autostart_npu_map()` 对残缺/非法配置的容错。
`scripts/verify_autostart_badge_ui.py`（7/7）—— 页面级：本机算力栏每张
卡的徽章可见性必须与 `config.compute.autostart` 一致。

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
  Capability 概念作为内部 key 使用（`get_session("swcap_vision_chat")` /
  `"swcap_text_to_speech"`）；capability 路由仅走同进程 `ComputeRegistry`，
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
  超时重建（VR `restart_and_rebind_instance_local`）。
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
`rtsp://admin:<password>@172.16.9.79/cam/realmonitor?channel=1&subtype=1`，
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

* **Service** (`wdnapps_audio_chat` 包的 `cards/tts/service.py`):
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
* **Endpoints** (`wdnapps_audio_chat` 包的 `cards/tts/api.py`):
  - ``POST /api/tts/start`` ``{text, language, speaker, ref_speaker,
    volume, instruct, no_play, non_streaming_mode, output}`` — kicks
    off a synthesis. Refuses with 503 if a run is in flight.
  - ``POST /api/tts/stop`` — flips state to ``stopped`` immediately
    and propagates ``stop_event`` to the SDK.
  - ``GET  /api/tts`` — current snapshot (state / available / frames
    / audio_sec / elapsed / output / error / request / history_count).
  - ``GET  /api/tts/history`` — list of recent output WAVs.
  - ``GET  /api/tts/voices`` — 可用音色 + 语种清单(前端下拉的数据源)。
  - ``WS   /api/tts/ws`` — pushes the snapshot on every state /
    frames / error change. Auto-reconnect with exponential backoff
    is handled on the client side (``app/static/core/framework.js (前端核心) + app/static/apps/<id>/app.js (各 app)``
    ``TTS.ensureWs``).
* **Frontend** (``app/static/index.html.template`` + ``#page-tts`` markup):
  - Sidebar entry ``语音生成`` sits between ``视频检测`` and
    ``系统日志`` (mutually exclusive with other pages — the sidebar
    just toggles the ``.hidden`` class on each ``.page``).
  - Textarea + voice / language selectors (both rendered from
    ``GET /api/tts/voices``) + volume slider (default 10%, range
    0–100, step 5; the label mirrors the slider value live via the
    ``input`` event).
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

* **两条发声路径,别混。** ``ref_speaker`` = SDK 内嵌的参考音色
  (``girl_base`` / ``ahu``,voice-clone x_vector,与权重无关);
  ``speaker`` = 模型自带的内置音色(``SPK_ID`` 的 9 行)。**两者都发
  等于只发 ``ref_speaker``** → ``POST /api/tts/start`` 只在**两个都
  不给**时回落 ``girl_base``。
* **本板部署的是 Base 权重,内置音色那 9 行是未训练的噪声** ——
  塞进 talker 等于没给音色条件,每次生成自由发挥(2026-10-26 实测:
  同一 Serena 时而男声时而女声)。清单只列 SDK
  ``available_config_speakers()`` **实测可用**的音色(判据与实测
  数字见该函数 docstring),API 也按清单**拒掉**不可用音色
  (HTTP 400);换 CustomVoice 权重后自动放出,不改代码。
* **音色/语种清单不许在前端硬编码** —— 取自 SDK 的表;10 个语种的
  embedding 行在 Base 权重里都训练过,语种下拉扩到 10 个是真功能。
* **instruct 页面不暴露** —— 官方能力表里 Base 的 "Instruction
  Control" 列为空(只有 CustomVoice / VoiceDesign 有),填了不改变语气。
* **方言音色会锁语种** —— ``eric`` / ``dylan`` 配 ``auto`` /
  ``chinese`` 时 SDK 锁死语言 id,前端 ``#tts-voice-hint`` 明示。
* **回归**:``scripts/verify_tts_voice_catalog.py``(不占 NPU)、
  ``scripts/verify_tts_voices_real.py``(占 NPU:同一音色连生成 6 次,
  **基频极差 < 8 半音** —— 音色条件失效时基频在男女声间乱跳、极差
  翻倍;有效 2.8~5.7、失效 9.6~12.4)。

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

## 监控反查 (video-retrieval) page

`#page-video-retrieval`（卡片 id `video-retrieval`，`app/apps/video_retrieval/`）
是演示页第 4 项。

选一个本地视频文件 → 按时间窗切片 → 每片 embedding → 文本余弦检索出
命中片段 → 点命中卡片用真实帧流播放。状态机
`idle → extracting → ready`，`error` 是旁支终态。

* **Service** (`app/core/video/search.py`)：
  - `VideoSearchService` 单例（`get_service()`）。
  - **LLMSession 必须从 ComputeRegistry 取** —— `get_session("swcap_vision_embedding")`，
    复用 `qwen3-vl-embedding-2b` 实例；找不到 capability 抛
    `RuntimeError("功能未启用: ...")`，HTTP 503 透传。`start_extract`
    加预检 `_ensure_session()`，不用等 worker 探测才报错。
  - **进程级 `_llm_session_call_lock`**：`embed_text` / `embed_video` 的
    `reset() + respond` 类推理用该锁与 detector 串行化（embedding 的
    `embed_*` 也是 NPU 推理）。
  - **切片 embedding（gscore 动态切片）**：`start_extract()` 不再收切片
    参数 —— 每 1 秒采样一帧，交给 `self._judge`（`FrameJudge(mode=gscore,
    every_n=10, threshold=10, long_side=160)`）判定要不要；**按有效帧**
    累积成片（`app/core/video/slicing.py`：每片最多 `SLICE_MAX_FRAMES=4`
    个有效帧、片间重叠 `SLICE_OVERLAP_FRAMES=1` 个有效帧，
    `start=首个有效帧时间`、`end=最后一个有效帧时间+1`）。跳过的秒不产
    帧但**仍落在区间内** —— 片段时长会自然变长，所以「4s/1s」不再是
    准确的描述。每片帧缩放到 576×576，`embed_video` 得 2048 维 L2 归一化
    向量，存内存 `List[{video_path, start_time, end_time, chunk_idx, fps,
    vec}]`。逐片推进 `slices_embedded` 并 WS 广播 `progress`（另带
    `frames_evaluated` / `duration_s`，前端进度按「已分析秒数」算，因为
    片段总数在 gscore 下是动态的）。`_probe_video_meta` 用 PyAV 探测
    fps / 总帧数 / 时长。

    > **manual 模式的坑**：`FrameJudge.observe` 的 manual 历史语义是
    > `pos > 0 and pos % n == 0` —— 直接照搬会让短视频（< every_n 秒）
    > **一帧都不取、0 个片段**，而 `state` 还会变成 `ready`。所以
    > `iter_slices` 对**第一个采样帧无条件取**（自动模式本来就取首帧当
    > 参照帧），并在 `search._worker_loop` 把 `slices_total==0` 判成
    > `error`（「未能从视频中取到任何片段」）而不是假成功。

    **⚠️ `embed_video` 必须传 ndarray 帧列表，绝不能传 `/dev/shm` 路径
    字符串**：worker 端 `_base.py` 把 `payload["frames"]` 交给
    `decode_attachments`，而后者对 **str** 的语义是"能 base64 解码就是
    图片，否则当文本前缀" —— 文件路径不是 base64 → `TextAttachment`
    → 被 `isinstance(ImageAttachment)` 过滤 → `frames=[]` →
    `{"status":"error","error":"no decodable frame in payload"}` → 每个
    chunk 都失败 → **`0/N` 切片成功**。传 ndarray 时 wdndds client
    经 `_bgr_to_path` 写 raw-BGR 描述符、worker `decode_attachments` 的
    dict 分支 `np.frombuffer().reshape(h,w,3)` 正好接住，且在 RPC 返回后
    自动 unlink。**本页（`core/video/search.py`）与微信管线
    （`im_bridge/video_manager.py`）共用同一契约，改一处必须同步另一处。**
  - **检索**：`embed_text` 编码查询 → 遍历索引算余弦相似度 → 阈值
    过滤（固定 `FIXED_SIM_THRESHOLD = 0.40`）+ `top_k`（固定
    `FIXED_TOP_K = 10`）截断；同样由 `query()` 覆盖调用方传入值，
    页面与向导不暴露这两个参数。索引为空时直接返回空命中。
  - **`iter_segment_jpeg(source, start_s, end_s, paced=...)`**：按播放
    顺序生成区间内每帧 JPEG（PyAV seek 到 keyframe + 顺序 decode +
    cv2 编码）；`paced=True` 时按源视频 fps 节流（**绝对时钟对齐**，
    sleep 补偿编码耗时，帧间隔精确 = 1/fps，缺失回退 `fallback_fps=25`）。
* **API** (`app/apps/video_retrieval/api.py`，**扁平化** `/api/video-retrieval/*`)：
  - `GET  /` — 快照（state / 进度 / query / hits / fps / source_exists）。
  - `POST /open` `{source}` — 选单个视频文件。
  - `POST /extract` / `POST /extract/stop` — 开始 / 停止切片 embedding；
    body 无参数（切片由 gscore 判据 + `SLICE_*` 常量决定）；实例未启用 503。
  - `GET /judge` / `POST /judge` / `GET /judge/metrics` / `POST /judge/reset`
    — 判据配置（与三卡同契约；`metrics` 额外返回 `default_every_n=10`，
    因为 VR 的 `every_n` 单位是**秒级采样帧**而不是解码帧）。
  - `POST /query_batch` `{queries: [{text?, image_path?}], entries?, check_image_quality?, min_results?}`
    — 统一单组 / 1~5 组检索（阈值 0.40 / 上限 10 固化；传 `entries`
    走缓存多视频，否则读当前单视频索引）；`POST /query/clear` — 清空结果。
  - `GET /image/thumb?path=` — 查询图缩略图（长边 240 JPEG，只读）。
  - `GET  /cache/stats` / `GET /cache/list` / `DELETE /cache/entry` / `POST /cache/clear`
    — 检索缓存管理（只含检索条目）。**删除当前会话对应条目时必须
    `reset_session()`**（与 `/cache/clear` 一致，返回 `session_reset`）：
    否则持久缓存已删、单例仍 `ready` + 旧 `_index`，前端「开始 / 向导完成」
    会对 ready+同源短路，进度条直接 100%、有结果但不重新提取、也不写
    新缓存（用户实测）。前端删除按钮据 `session_reset` 清
    `VR.snap`/进度/旧结果；向导完成改用 `vrStart(true)` **强制走一次
    `start_extract`**（缓存命中秒回，缓存已删则重新提取并写缓存）。
    回归：`scripts/verify_vr_cache_entry_reset.py`（不占 NPU）。
  - `GET  /history` — 按 md5 聚合的历史（只含检索档位）。
  - `GET  /frame?source=&t=` — PyAV seek 到 `t` 秒附近取帧 JPEG。
  - `WS   /ws` — server→client：`hello`（连接即发快照）/ `progress` /
    `state`。
  - `WS   /play` — 播放命中片段：客户端首条 JSON `{source, start, end}`
    → 服务端用 `iter_segment_jpeg(..., paced=True)` 推二进制 JPEG 帧；
    `{"action":"pause"}` / `{"action":"resume"}` 控制；播完 `{"type":"eof"}`、
    出错 `{"type":"error"}`；断开连接即停线程。
  - 向导预览：`POST /upload`、`POST /source/{open,close,control}`、
    `GET /source`、`WS /source/ws`（复用共享 `VideoSession`）。
* **前端** (`app/apps/video_retrieval/static/app.js` `VR` 模块 + `#page-video-retrieval`)：
  - 左侧：视频源（"浏览…"复用 `VdFsModal._sourceTarget`）+ **判帧算法**
    （`#vr-judge-config[data-judge-layout="inline"]`，共享 `frame-judge.js`
    的 `configOnly` 模式）+ 检索问题 / 检索图片（浏览 / 上传 / 预览 /
    清除）/ 图片质量检查 / 宽松匹配 + 历史记录（只含检索缓存）+
    开始 / 停止 + 提取进度（`#vr-progress`，文案为「已提取 N 段 · 已分析
    X/Y 秒」）。算力就绪时自动可用，未就绪时检索子区置灰、输入框禁用、
    开始按钮禁用。
  - 右侧：播放器 + 命中结果列表（相似度降序，每条带开始/结束时间），
    点卡片 → `vrPlaySegment` 连 `WS /play` 真实帧播放：等比 contain 填满
    播放窗口（`vrSyncCanvasSize` 把 canvas 像素同步为 `.vd-stage` 显示
    尺寸）、进度条按已收帧数/总帧数实时推进、暂停/恢复/停止/播完重播。
  - 历史记录**一行 = 一条检索缓存**（md5 × 切片参数 × **判据口径**；后端
    `/history` 仍按 md5 聚合返回，前端展平）；gscore 条目展示「按变化切片」
    （不显示 4s/1s —— 跳过的秒不产帧，参数不再描述实际片段），旧固定切片
    条目保留参数。勾选 ≥1 → `/query_batch` 多视频检索（`entries[]` 带
    `judge_fp` / `slice_version`，按条目自身口径查）；按行删除 / 清空缓存 /
    清空检索结果。
  - `DemoGuard` 注册 `video-retrieval`（`active_state_keys: ["search.state","busy"]`），
    离开页面 confirm + 停提取 + `sendBeacon` 兜底。
  - **向导跑完自动播放视频**（`ImApp_video_retrieval.applyWizardSnapshot`）：
    与 vtd / vtl / vd 同口径 —— 向导「完成配置」后画面就在播。四张视频卡里
    原本**只有 vr 跑完是黑屏**（其余三张都是 open + 启动功能）。
    * **两条通路彼此独立**，别当一回事：预览 = 共享 `VideoSession`
      （MPP/RGA 解码，`/source/open` + `/source/ws`）；检索 =
      `VideoSearchService`（PyAV 切片 + NPU embedding，`/open` + `/extract`）。
      所以**预览打不开不阻断检索**（共享 session 正被别的卡占着、或某容器
      MPP 解不了而 PyAV 能切，检索照样能做）—— 只 warn，真问题（源不存在
      之类）由检索自己的错误横幅报。
    * 顺序：**先开画面再启动检索** —— 等能力就绪最多 20s
      （`WizardStart` 轮询上限），这期间画面已经在播，用户不用盯黑屏。
    * **预览在跑 = 后台在跑**：`vrReport()` 的 `busy` 必须含
      `previewPlaying`，否则离开页面不拦、停下也不走 stop_endpoints
      → 共享解码线程一直解下去（与 vtd / vtl / vd 的
      `session.state=playing` 同口径）。
    * **点命中片段时要把后端解码也停掉**：`vrClosePreviewWs()` 只关 WS，
      共享 `VideoSession` 的解码线程会继续解 + 继续编 JPEG（没有订阅者
      也照编，broadcast 只是遍历空列表）—— `vrPlaySegment` 额外 POST
      `/source/close`（只关预览那路，不碰检索侧）。
    * 向导挂载期间 page section 是 hidden 的 → canvas 被压成 2×2；向导
      卸载后 `vrDrawJpeg` 的尺寸漂移检测会自动把它同步回真实尺寸。
* **ComputeRegistry preset**（`app/core/compute/registry.py`）：
  - `MODEL_PRESETS["qwen3-vl-embedding-2b"]`：`capability_types=
    ["swcap_vision_embedding"]`，`memory_estimate_mb=2000`，
    `estimated_init_seconds=30`。`kind="embedding"` 的 probe /
    `_init_session_subprocess` 分支按 vlm 文件布局校验（rknn_llm +
    rknn_vision + embed + tokenizer）。
* **缓存**：LanceDB 表 `wechat_video_index`（表名 / 类名 `WechatVideoCache`
  保留，微信视频检索共用同一单例）。schema **只含检索列**
  （`md5 / chunk_idx / start_time / end_time / fps / vec / filename /
  source_path / duration_s / slices_total / slices_embedded /
  chunk_seconds / chunk_overlap_seconds / embedding_model / analyzed_at /
  meta_json / judge_fp / slice_version`），已删除 `kind` 与全部分析列。
  **`judge_fp` / `slice_version` 是切片口径隔离键**：VR 写
  `judge_fp="<mode>:<threshold>:<every_n>"`（默认 `gscore:25:10`）、
  `slice_version="gscore-v1"`；微信写 `judge_fp=""`、`slice_version="fixed"`。
  `lookup` / `save` / `list_videos` / `delete_video` 全部按这个键过滤 ——
  否则改一次阈值就会复用旧索引（时间窗与帧数都对不上）。升级时直接删除
  旧库 (`rm -rf app/uploads/lancedb`) 后重建；微信条目也会随之清空。
* **判据默认阈值独立于三卡**：VR 是 1 秒/帧采样，gscore 默认 **25**
  （`app/core/video/search.py: DEFAULT_JUDGE_THRESHOLD`）；三卡仍是 25fps
  逐帧口径的 10（`frame_judge.DEFAULT_THRESHOLD_BY_MODE`）。`/judge/metrics`
  返回 VR 自己的 `default_threshold`，`wizard.json` 的
  `vr-judge-threshold.default` 也是 25。`FrameJudge.reconfigure` 在
  “模式变化 + 未传 threshold”时会回落到共享的 10，所以
  `VideoSearchService.set_judge` 在切到 gscore 时补 25 —— 改这块时别把
  它删掉，否则 `gscore → manual → gscore` 会静默回 10。
* **资源预算**：qwen3-vl-embedding-2b 单实例 ~2 GB。检索单视频索引在内存，
  长视频（>1h，按「有效帧上限 4 / 重叠 1 帧」≈ 900~1200 片 × 2048 维 fp32
  ≈ 10MB 向量）可忽略。播放走 PyAV 顺序解码，不占 NPU。

### 图片检索

`POST /query_batch` 接受每组 `image_path`（绝对路径）—— 图片、
文字、图片+文字三种输入任意组合：

* **编码**：`VideoSearchService.encode_query(text, image_path, check_image_quality)`
  - 有图 → `session.embed_video([img], prompt=text or "")`（**单帧** = 图片
    embedding，与视频片段同一 2048 维空间）；
  - 纯文字 → `session.embed_text(text)`。
  - 读图后统一 `cv2.resize` 到 **576×576**（与 `_read_chunk_frames` 的索引帧
    预处理一致；不要给图片另设尺寸）。
* **默认检索策略与文本完全一致**：阈值 `FIXED_SIM_THRESHOLD=0.40`、
  `min_results=0`（严格；勾选「宽松匹配」才 top-10 兜底）。
* **图片质量检查**（`check_image_quality`，默认 **true**，页面/向导都有
  勾选）：在**原图**上判 `std < 3`（纯色）或 `Laplacian var > 20000`
  （随机噪声）→ 400。必须在缩放前判 —— 128×128 噪声放大到 576 的插值
  本身就是低通滤波，缩后判会漏（实测）。
* **路径校验**：绝对路径、存在、后缀 `.jpg/.jpeg/.png/.bmp/.webp`、≤20MB。
  上传复用 `POST /api/video-retrieval/upload`（任意文件，落 `uploads/`）。
* **缩略图**：`GET /image/thumb?path=`（长边 240 JPEG，只读、不占 NPU）。
* **向导**：`state.js` 支持字段级 `"browse": true`（复用 VdFsModal 选服务器
  文件）与 `"upload": true`（浏览器上传到 `cfg.upload_endpoint`，写回字段 +
  复用上传在途锁）；VR 的 search 步声明 `vr-search-image` +
  `vr-check-image-quality`。未声明这两个属性的卡行为零变化。

## Text chat (文本对话) page

`#page-text-chat` 暴露纯文本对话（`swcap_text_chat` capability，由
qwen3.5-vl-4b 提供），
流式逐字渲染 ` thinking...</think>` 与正式回答（思考过程由页面「深度
思考」开关控制，**默认关闭**；关闭时思考块整块隐藏，见下文「深度思考
开关」），单页实例串行推理，
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
  - **上下文跟随模型 spec**：`context_limit` 取当前 session 的
    `spec.max_context_len`；`max_new_tokens` 固定使用共享
    `chat_runner.MAX_NEW_TOKENS`（2048），不再读取
    `compute.text_chat.context_limit`。文本对话优先 9B，其次 4B。
  - WS broadcaster pattern：worker 线程 push 到 `asyncio.Queue` 列表，
    WS handler 在主 event loop 里 `await queue.get()` drain；observer
    也走同一条路径，避免第二条 channel。

* **Worker 端（v0.16 起）= `wdndds.serve_local_session`，无自研 chat 通道**：
  worker 只做三件事：把 `state["session"]`（懒建，首次 `load` 时
  `LLMLocalSession`）交给 `serve_local_session`、用 `extra_ops` 挂上
  wdnwebui 自己的生命周期动词、跑主循环。**没有 `chat_run` /
  `chat_poll` / `chat_stop` / `infer_handler`**。
  - `extra_ops`（`_build_extra_ops`）分两类：
    **新增** `load` / `unload` / `shutdown` / `count_tokens` /
    `set_thinking_mode`（后者是 thinking 开关的唯一真源，见下文「深度思考
    开关」）；
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

* **深度思考开关（`enable_thinking`）—— 动态切换要「reset + 重设模板」，
  状态必须放 worker 侧**：
  - 页面级开关「深度思考」（默认关，`localStorage["text_chat_thinking"]`），
    随 `POST /api/text-chat/send {enable_thinking}` 下发；
    `ChatRequest` → `run_chat_with_tools(enable_thinking=…)` →
    `respond(enable_thinking=…)`。**只有文本对话传 True**；微信桥接、
    音视频描述、电脑控制都不传（默认 False）。
  - **C 侧 thinking 是「开了就锁住」的**（真机 Qwen3.5-4B 实测）：一旦
    `enable_thinking=True` 跑过一轮，之后所有 `enable_thinking=False` 都
    被忽略 —— **`sess.reset()`（`clear_kvcache`）单独无效**，必须
    `reset()` **再 `set_chat_template(CHATML_SYSTEM/USER/ASSISTANT)`**
    才能切回非思考。实测同一 session：开启 2.2s / 输出完整思考；
    关闭 0.39s / 输出空的 ` thinking\n\n` 块；只 reset 不重设模板 → 仍是
    完整思考。重建 session 也能回到 OFF，但要 45s 模型加载，不可用。
  - 因此 **worker 提供 `set_thinking_mode` extra op**
    （`workers/_base.py`）：**幂等**（模式相同直接返回 `changed=False`），
    只在变化时做 reset + 重设模板，状态存在 `state["thinking_mode"]`。
    状态放 worker 而不是主进程，是因为 **C 会话才是唯一真源**：LAN peer
    adopt 同一 worker 时，两个主进程各自缓存会不一致。
  - **`proxy.make_rpc_session` 包装所有 session 的 `respond`**
    （`_wrap_thinking_mode`）：每次请求前在 per-worker 锁内先调
    `set_thinking_mode(desired)` 再推理。于是 **text_chat 的开关不会污染
    同 worker 的其它应用**（vd / usb_hid / 微信请求前都会被自动切回 OFF），
    而「真正 reset 只在模式改变时发生」由 worker 侧判断保证。锁按 worker
    隔离（不同模型互不串行）；worker 端本就串行执行 RPC，加锁不增加延迟。
  - **旧 worker 进程**（未重启、没有该 op）→ `call_op` 报 `unknown op`
    → 包装层记下并降级继续，**不影响推理**；worker 重启后自动获得该 op。
  - **已知边界（多板）**：LAN peer 借同一 worker 时，两边主进程在各自的
    锁外 → `set_thinking_mode` 与 `respond` 之间有极小窗口（对端刚切走
    模式）。**单板部署不受影响**；多板共用 worker 且同时混用
    thinking / 非 thinking 才会遇到。
  - **输出形态两种，`_ThinkingRouter` 必须都覆盖**（勿回退成
    `delta.find("</think>")` 的单 delta 写法）：
    * 开启：流里**没有** ` thinking` 开标签（C 侧把它注入 assistant
      前缀了），只有思考内容 + `</think>`；
    * 关闭：模型仍输出空的 ` thinking\n\n</think>` 块。
    标签会被 token 流切开（`<th` + `ink>`），所以 router 是**缓冲式状态机**
    （保留尾部 `len(tag)-1` 个字符），空思考段**不 emit**；结束时
    `flush()` 兜住尾部（无任何标签的纯答案按 answer 处理）。
    `runner.split_thinking` 另补一档：只有孤立 ` thinking` 开标签时从
    answer 中剥掉。前端 `<details class="text-chat-thinking">` 初始
    `hidden`，**只有非空 thinking 才显示**（关闭思考时整块消失，不再出现
    用户看到的「只有一个 ` thinking`」）。
  - 回归：`scripts/verify_thinking_router.py`（6 用例，纯逻辑，无需 NPU）。

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

* **Proxy**（`app/core/compute/proxy.py`，v0.16 起只剩装配 + 两条策略）：
  - `make_rpc_session(...)` → 返回 `wdndds.LLMRPCSession`，复用 registry
    已有的 `Hub` / `Service` / `Requester`（不再自建 participant）。
    `_INFER_TIMEOUT_MS=120_000`（够 768px VLM 帧的 ~35s，又要能抓住真卡
    死）、`_CONTROL_TIMEOUT_MS=15_000`（控制面毫秒级，不该继承推理预算）。
  - `count_tokens(sess, texts)`：带降级，控制面失败不让整轮对话失败。
  - `_wrap_thinking_mode(sess, service_name)`：包装 `respond`，在
    per-worker 锁内先调 worker 的 `set_thinking_mode` 再推理 —— 让
    `enable_thinking` 在「C 会话一旦开启就锁死」的前提下表现得像
    per-call 开关，且同一 worker 的其它调用方（vd / usb_hid / 微信）
    请求前会被自动切回它们期望的模式。详见「深度思考开关」小节。
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
    `capability_types=["swcap_vision_grounding"]`，
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
  ``requires_capability="swcap_text_to_speech"``。
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
        capability_types=["swcap_vision_grounding"]、`worker_name="ground"`（alias 6 字符，
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
     **有效帧上限 4 / 重叠 1 帧** embedding（`iter_slices(judge=None)`
     = 所有采样帧都算有效帧，行为与旧固定 4s/1s 逐片一致；复用 VR 底层函数 `_probe_video_meta`
     / `_read_chunk_frames` / `_write_frames_to_shm` / `_cosine_similarity`
     + `get_session("swcap_vision_embedding")`，SHM 帧传递 + timeout 重试
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
    `(md5, embedding_model, chunk_seconds, chunk_overlap_seconds,
    judge_fp, slice_version)` 定位条目 —— 不同切片口径的索引时间窗与
    帧数完全不同，混用会时间戳错位。微信管线固定
    `chunk=SLICE_MAX_FRAMES / overlap=SLICE_OVERLAP_FRAMES`、
    `judge_fp=""` / `slice_version="fixed"`；VR 写判据指纹
    `gscore:<threshold>:<every_n>` / `slice_version="gscore-v1"`。同视频
    不同参数 / 判据是**独立条目**（各自占 LRU 槽，命中互不干扰）。
* **VR 页接入缓存（v0.12.1）**：视频内容检索页与微信管线共享同一
  张表 + 同一单例：
  - `VideoSearchService.start_extract` 参数校验通过后算
    md5（本地文件直接流式算，无需下载）→ `lookup(...)` 命中即
    还原 index（`video_path` 换绑为当前 source basename，帧接口
    走当前源）→ state 秒级 READY（广播 state，前端进度条直接
    满格）；不命中走原有 worker。
  - `_worker_loop` 收尾 `slices_embedded >= slices_total`（全成功
    语义与微信一致）→ `cache.save(...)`；部分成功不存（与微信
    决策一致，重跑可补全）。
   - **"清空缓存"按钮**（`#vr-clear-cache`，缓存窗口 head）→
     `confirm()`（含 `cache/stats` 返回的条数提示）→
     `POST /api/video-retrieval/cache/clear` → 幂等
     `{videos_cleared}`；`GET .../cache/stats` 返回
     `{enabled, max_entries, videos, rows}` 给确认框展示。**同时作废
     当前会话的内存索引**（端点随后调 `get_service().reset_session()`：
     停提取 worker + `_state→idle` + 清 `_index` / `_md5` / 切片计数 +
     广播 idle）—— 否则持久缓存虽删、前端 `vrStartSearch` 仍会看到
     `state=ready` 短路跳过 `start_extract`，表现为"进度条直接满格 +
     历史无新条目"（用户实测）。保留 `_source`，可重新点开始提取。
     **注意**：清空 LanceDB 是全表删除（含微信条目），会话 reset 只
     针对 VR 单例（微信 `wechat_video_manager` 用自己的
     VideoEntry，不共用这个单例，不受影响）。
   - **"清空检索结果"按钮**（`#vr-results-clear`）→
     `POST /search/query/clear` → `clear_query()`：清 `_query` / `_hits`
     **并把 `_state` 从 `ready` 复位为 `idle`（清零切片计数）**，
     `_index` 保留供重查。原因：进度条完全由快照 `state`/`slices_*`
     渲染，`ready` 会在任何一次快照重放（WS 重连 / 卡片 JS 重载 /
     切页返回）时把进度条刷回 100%（用户实测："清空后切页再切回进度条
     复活"）。复位 idle 后前端进度条走 `prog.hidden=true`（**直接隐藏**，
     非停在 0%）。前端 `vrResetProgress()` 的 `_progressForceIdle`
     内存标志保留作即时反馈，且 `vrStartSearch` 的 `ready+同源` 短路
     额外加了 `!_progressForceIdle` 守卫（防旧 ready 快照刷回后误短路）。
  - **schema v0.12.2 加列 `source_path`**（入库时的本地视频路径）：
    微信路径固定存 `""` 占位（分析完删文件，无可取帧），VR 传
    `self._source`；`_migrate_add_source_path` 用 lancedb 0.34
    `add_columns({"source_path": "''"})`（SQL-dict 形状，持久化）
    幂等补列，失败兜底 drop + 新 schema 重建（缓存可再生，旧条目
    frame 置灰不影响检索）。
* **VR 勾选双路径（v0.12.2）**：缓存窗口让已分析视频可被直接勾选
  检索，无需重新上传/提取：
  - **窗口/数据**：`GET /api/video-retrieval/cache/list`
    （`cache.list_videos()`：`[{md5, filename, source_path, chunk/
    overlap, slices, duration_s, analyzed_at}]`，按 analyzed_at 真
    倒序）渲染 `#vr-cache-window`（max-height 滚动）—— 每行=
    (md5 × 切片参数) 三元键条目：checkbox + 文件名 + "Ns/Ms·N段" +
    行删除按钮（`DELETE .../cache/entry` 按行删，同 md5 其它参数档
    保留）。
  - **互斥**（前端 `vrApplyMutualExclusion`）：路径 text 非空 → 勾
    选框全 disabled；勾选 ≥1 → `#vr-source` / `#vr-browse` /
    `#vr-start` disabled。
  - **多选检索**：检索按钮在勾选 ≥1 时走
    `POST /query_batch {queries, min_results, entries[]}` —— 匹配阈值
    固定 0.40、检索上限固定 10（body 里传了也会被忽略）；`entries[]`
    里的 `chunk_seconds` / `chunk_overlap_seconds` / `judge_fp` /
    `slice_version` 是缓存条目主键，按条目自身记录的参数检索（历史条目
    可能是旧固定切片档位，也可能属于不同判据配置）。
    **embed_text 只算一次**、逐条目 LanceDB lookup
    向量做余弦（`_score_hits` 与单视频 query 共用）；返回
    `groups[{filename, source_path, chunk params, hits, top_score,
    frame_enabled, miss}]`，前端 `vrRenderMultiGroups` 按视频小节
    分组渲染（命中卡点击播放 → `vrPlaySegment(idx, srcOverride)`
    用条目 source；`frame_enabled=false` 时卡片置灰"取帧失败"，
    点击提示）。**无状态批量语义** —— 不污染单例的 `_index` /
    `_source` / 状态机。
  - **宽松匹配全局化（v0.12.4）**：缓存批量查询改**两阶段**评分
    —— 阶段 1 每组严格检索（`min_results=0`，阈值过滤 + 组内
    top_k）累计全组合计达标数；阶段 2 仅当**合计零达标**且
    lenient 时，收集各组低于阈值候选**全局降序**取 top
    `min_results` 条按来源组落位（共享一份配额，标 below）——
    此前每组各自带 min_results 兜底（N 视频全无命中兜出 N×top_k
    条，"每视频各一份"与"一次总的"语义不符，用户决策改为全局
    判定：任一视频有达标命中则全部不兜底）。单视频 `query` 路径
    与微信 `VideoSearch` 工具的兜底不受影响（各自单组语义不变）。
  - **布局（v0.12.4）**：`#vr-cache-window` 从"视频源"栏移出为
    独立 section（位于"操作"与"检索问题"之间），"清空缓存"按钮
    移入窗口 head（`.vr-cache-actions` 右侧按钮组，警示 hover）；
    提取完成（state 首次 ready，`_cacheWinOnReady` 防抖块）自动
    清空 `#vr-source` 路径文本框（仅成功清空，error/stopped 保留
    便于重试）并刷新缓存窗口 —— 引导检索走勾选路径。
  - **source_path 语义**：缓存条目存的入库时路径仅当**该文件仍在
    本机**才允许取帧/播放（`frame_enabled`，端点侧
    `os.path.isfile` 校验）—— 移走/删除的旧文件条目仍可纯文本
    检索（时间段 + 相似度）。
  - **渠道隔离（v0.12.3）**：`source_path` 双语义兼作渠道判别 ——
    微信管线固定 `""` 占位、web（VR）恒存真实本地路径（均不随
    文件存亡漂移）。`cache/list`（web 窗口/勾选/行删除的数据源）
    调 `list_videos(channel="web")` → 只出 `source_path != ''` 条目；
    **微信条目不进 web 界面**。`lookup`/命中短路/LRU/`clear`（全清
    含微信条目，用户决策）不变 —— 跨入口命中复用保持（表共享、
    展示隔离）。同 md5 同参数两渠道互覆（save 幂等语义，检索等价）。
* **VideoSearch 工具**（`app/core/tools/video_search.py`，
  `requires_capability="swcap_vision_embedding"`）：**无调用时机限制**
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
  先 `get_session("swcap_vision_embedding")` —— 未加载则**不入队**
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

### 微信图片检索（v0.28）

微信消息现在支持图片输入（与视频索引同一 embedding 空间）：

* **分流**（`llm.py::handle_incoming`，图片走独立 image worker，不阻塞 SDK 回调）：
  - 纯图片 → `embed_video([img])`；
  - 图片 + 同消息文字（caption）→ `embed_video([img], prompt=caption)`
    —— 但微信客户端**不支持图片与文字同发**（实际是两条消息），
    所以 caption 分支基本恒为空；保留它只为协议将来支持时直接生效。
  - 后续任何文字/图片都是**独立重新检索** —— 不做 TTL / pending /
    跨消息自动绑定。
* **结果**：命中片段作为 `_system_note` 写入该用户 history，由
  `swcap_text_chat` 组织回答（`tool_names=[]`：结果已给出，不让模型重复
  检索）。text_chat 未启用时回「文本对话模型未启用，无法总结检索结果」
  + 原始命中时间段/相似度。
* **质量检查**：默认开（与 VR 同一 `encode_query`：`std<3` 纯色 /
  `Laplacian>20000` 噪声 → 直接回错误文案，不进入 LLM）。
* **缓存**：每用户只保留**最近一张**图片（`video_manager._last_images` +
  `uploads/wechat_images/<user>/`）；新图覆盖旧图并删除旧文件；服务启动
  在 `app/system/__init__.py::register_all` 显式调 `clear_image_cache()`
  清空（不依赖 video_manager 的 lazy 初始化）。
* **“文字是否基于刚发的图”由模型自行判断**（不做固定规则）：
  `VideoSearch.use_last_image` 仅当当前文字是在描述/追问**刚发送的那张
  图片**时置 true → 用最近一张 + 文字做图文检索；独立问题保持 false。
  图片检索后写入 history 的系统注记里也会提示模型区分“追问这张图”
  与“独立新问题”。无缓存图时工具返回 `no_image`。
* **引用图片：协议层不支持** —— 实测微信引用消息的
  `ref_msg.message_item` 只有 `msg_id` / `create_time_ms` / `type=0`
  等元数据，**没有 image_item / media / url，也没有 text_item**；
  引用图片与引用文字的结构逐字段一致（无法区分）。SDK 也没有按
  `msg_id` 拉取原消息的 API。因此：
  - 引用消息一律按**普通文字**处理，不下载、不猜最近一张；
  - 模型若判断这条引用文字是在追问刚发的图，可自行
    `VideoSearch(use_last_image=true)` 使用最近一张；
  - 残余风险：引用的是更早的图片时无法取到，只能拿到最近一张
    （这是微信协议限制，不是实现缺陷）。
* **检索口径**：仍走 `video_manager.search` 的阈值 0.40 / top-5 / 3 条兜底；
  返回 `query_type` 标记 `image` / `image+text`。
* **回归**：`scripts/verify_wechat_image_search.py`（mock，26 项：caption /
  search 编码、最近一张覆盖与清空、`use_last_image` 描述与无图状态、
  成功 / 质量不合格 / 无视频 / text_chat 未启用、图片与引用消息分流）。


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

### NPU 温度读不到 / C 层 ERROR 刷屏（v0.30 修）

**症状**：`journalctl -u wdnwebui` 里每 3 秒一屏 ——

```
[ERROR] [nsmi_pcie.c:651] Receive error, command: 12     ← 设备级温度
[ERROR] [rk_nsmi.c:1143] rknnsmi receive failed, ret=-1
[WARNING] system_monitor: rknnsmi get_device_temperature(0) failed: ...
[ERROR] [nsmi_pcie.c:651] Receive error, command: 7      ← 设备级功耗
[WARN]  [rk_nsmi.c:906] rknnsmi receive failed, ret=-1
[INFO]  [rk_nsmi.c:923] Device[0] get device power from hwmon node, power: 5143 mW
```

而且**卡片上温度恒为空**（前端 `temp_c` 一直是 null → 不渲染 °C）。

**根因**：RK1828（RK3588 + PCIe AI 协处理器板）的 **设备级** SMI 命令要过
PCIe 通道问 EP 固件要遥测，这块固件**不应答** —— rknn-smi 先打
"Receive error, command: N"，`rknnsmi_get_device_temperature()` 于是返回
-1（GENERIC）。实测（2026-10-24，本机双 RK1828，pcie-rkep 3.3.1）：

| 调用 | 结果 |
|---|---|
| `rknnsmi_get_device_temperature(0)` | **必失败**（GENERIC -1） |
| `rknnsmi_get_chip_temperature(0,0)` | **成功**，raw = 43 / 45 |
| `rknn-smi info` CLI | 正常，Temp(C) 40 / 42（CLI 走的就是 chip 级） |
| `rknnsmi_get_device_power(0)` | PCIe 失败，但**库内部**回退到板级 INA226（hwmon）→ 仍返回 ~5300 mW |

**两级温度单位不一样**：设备级是毫度（×1000），chip 级是**整度**。
wdnai 两级都按毫度 ÷1000 → chip 级的 43°C 会被算成 **0.043**。
（wdnai 那边这个换算是 bug，属 SDK 仓库；本仓只能自己兜。）

**修法**（`app/system/api/system_monitor.py`）：

* `_read_temp_c()`：**先试 chip 级**（RK1828 上唯一可用的路），失败再试
  设备级；用 `_TEMP_SOURCE_CACHE` 记住每张卡哪一级能用 —— 于是 **cmd 12
  根本不会被发起**，C 层那行 "Receive error, command: 12" 消失。
* `_normalize_temp_c()`：把 `0 < v < 1` 判成「被误除了 1000 的毫度」并乘回
  1000。两种单位约定都能出正确值，将来 wdnai 修好换算也不会被反着乘。
* `_read_power_mw()`：功耗照旧调（值是对的，来自库内部的 hwmon 回退），
  但加 30s TTL 缓存 —— 功耗是慢变量，压掉 cmd 7 的噪音。
* `_smi_warn_once()`：同一个 (操作, 设备) 的失败最多每 60s 记一条 WARNING，
  其余降 DEBUG。PCIe 链路瞬态坏掉时（NPU 被 SIGTERM 打断、卡复位）会集体
  失败一轮，逐次记 WARNING 就是刷屏。

回归：`scripts/verify_npu_temperature_fallback.py`（15 断言，不占 NPU）——
chip 级归一 / 设备级命令**一次不被调用** / 两级都失败不抛 / 设备级可用时
也能出温度 / 功耗 TTL / WARNING 限流。**修复前是 5 通过 8 失败**。

> **旁证（排查 SMI 的坑）**：`/tmp/rknnsmi.lock` 被 webui 进程持有 → 同一
> 时刻另起 `rknn-smi info` CLI 会直接 `Failed to initialize rknnsmi`；
> 非 root 进程还会因为锁文件属主不对而 `errno 13`（cannot open lock file）。
> 所以**排查 SMI 要么停服务，要么用私有 /tmp**：
> `sudo unshare -m bash -c 'mount -t tmpfs tmpfs /tmp && ...'`
> （mount namespace 里 /tmp 是新的 → flock 落在另一个 inode → 不冲突）。

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
  （VR / wechat_video_manager）用 `/dev/shm` path-list 传帧，
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
> 曾一度误判「单路同样受影响、影响 vd/vtl/vr 三张卡」。用打桩 gscore 复现
> 两种时序才看清：不 reset 时第 0 轮之后每轮放行 0 次，reset 时每轮都有放行。
> 附带结论 —— reset 后每轮那 1 次放行来自**首帧无条件送检**，不是兜底；
> 看 `sampled` 计数容易误判成「兜底已触发」。

### 端点挂本卡，不共用 /api/video/judge

`/api/video/judge` 操作的是单例 `VideoSession`，vtd / vtl / vd 三张多路卡走
`MultiChannelSession`，所以判据端点分别挂在
`/api/video-target-detection/judge{,/metrics,/reset}`、
`/api/video-target-localization/judge*`、
`/api/video-detection/judge*`。
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

## 音轨理解多路化（1~32 路，v0.25）

`app/apps/audio_analysis/` 是音轨分析的**后端 + hidden 卡片**；真正在用的
UI 在 vd 卡（音视频描述）的「音频理解」面板里。

### 架构：取音频与送推理两级解耦

```
N 个采集线程   每路一个 AudioSource（app/core/audio/source.py）
                 文件源：顺序惰性解码 → 有界队列（深 2，满了阻塞）
                 RTSP源：常驻解码 → 环形缓冲（8s）→ 取尾部最新一段
  1 个轮转线程  app/core/audio/multi_session.py
                 严格 ch0→ch1→…→chN-1，每路取一段送推理（阻塞）
  1 个 Omni     单实例串行
```

**为什么不能一套缓冲糊过去**：文件源的段**不能丢**（丢了内容永久缺失），
RTSP 源的段**必须丢**（不丢就滞后越来越大）。所以接口统一
（`next_chunk()`），**语义分实现**：文件源 = 顺序取下一段，RTSP 源 = 取
最新一段。

**「就绪」与「放完」是两件事**。`next_chunk()` 返回 None 有两种含义：
还没准备好（本轮跳过，下轮再来）vs **放完了**（`exhausted=True`，该路
标记完成，不再参与轮转）。用 `exhausted` 属性区分，不要用哨兵值。

**结束条件**：全是文件源 → 各自跑完 → 会话 `done`；**含 RTSP 源 → 永不
自动结束**，一直轮转到用户停止。混合源同理。

### 6s 是 RKNN 的编译期上限，不是运行时参数

Omni-3B 单次 `run_audio` 上限 **6.000 秒**。这个数字是**编译进 .rknn 文件
的**，三条独立证据：

| 证据 | 内容 |
|---|---|
| 模型输入张量 | `rknn3_query(INPUT_ATTR)` → `[1, 128, 600]`；600 帧 × HOP_LENGTH 160 ÷ SAMPLE_RATE 16000 = 6.000s |
| RKNN 驱动 | `rknn3_set_shape(shape_id=2)` → **`shape_id = 2 is invalid, valid range is [0, 2)!`** —— 只有 2 个 shape（600 帧 / 300 帧） |
| wdnai 代码 | `AUDIO_SHAPES = {0:{n_frame:600}, 1:{n_frame:300}}`；`select_audio_shape` 在需要 >600 帧时**兜底 return 0**，于是 `run_audio` 里 `mel[:, :600]` **静默截断** |

**喂 8s 给模型，它只听到前 6s，后 2s 悄悄消失** —— 不报错、不告警、返回
正常文本。这是最容易踩的坑。

想支持 7s/8s **必须重新导出 RKNN**（加一个 n_frame=700/800 的动态 shape）
并同步改 `AUDIO_SHAPES`。多切片是当前的正解（24s 切 4 段各 6s）。

**缩短窗口不省时间**：实测 1s 窗口 0.51s、3s 0.51s、6s 0.68s、12s 0.73s ——
0.7s 是**固定开销**（prompt + 生成 + RPC），不是音频长度带来的。所以别为了
「多轮几路」去用短窗口，代价是描述质量下降。

### NPU 互斥：Omni 用**专属锁**，别碰 get_session_call_lock()

`registry.get_session_call_lock()` 是**进程级**的 `threading.Lock()`，被
音频分析 / VR 视频描述 / VCS 视频检索 / 微信视频检索**共用**。

但 Omni 的 session 与 VLM 的 session 是**两个不同的对象**、两个不同的 worker
进程、两个不同的 NPU —— 它们之间根本不需要互斥。共用它的代价是实测出来
的：32 路音频轮转时每段持锁 700ms，期间 VR 的视频描述一把锁都拿不到。

→ `app/core/audio/multi_session.py` 用**模块级专属锁** `_omni_call_lock`。

> `registry.py` 自己的注释已经写了「v0.4 per-subprocess RPC refactor 后主
> 进程不再需要这把锁，worker 端已按实例串行化」—— 它现在只是历史包袱。

**vd 卡的视频描述走 detector（`detector_base`）自己的 slot 机制，不用这把
锁** —— 所以改完之后，音轨分析与画面描述**真并行**（不同 worker、不同
NPU、不同的锁）。

### npu 2 / 3 是空的（限制是配置性的，不是硬件性的）

本机 RK3588 有 **4 个 NPU**（npu_index 0~3），但 `config.jsonc` 的
`compute.autostart[]` 只声明了 npu 0 和 1，于是 `queue_length=3`（3 个模型
在排队）。「同时开不了两个 Omni」是**当前配置**的限制。

本应用（音视频描述）本身就要同时开 `[VLM]视觉描述v3` + `[Omni]多感知描述v2.5`，
所以**不做双 Omni 实例**是产品决策，不是技术限制。将来若嫌 22s 一圈太慢，
把第二个 Omni 绑到 npu 2 即可 —— `MultiAudioSession` 本身是 K 实例可扩展的
（当前 K=1），**不用改代码**，改配置就行。

### registry 探测 worker 的请求会被误当成一次文本推理

worker 日志里成片出现

```
WARNING shm side-file vanished before read: /dev/shm/wdndds_ai_audio_*.bin
WARNING serve[...]: respond raised RuntimeError: OmniBackend.run_text():
        pass AudioAttachment(wav_bytes) to LLMSession.respond()
```

**与音轨分析功能无关**。journalctl 证明那段时间**根本没有
`/api/audio-analysis/start` 请求**；这些错误与 registry 的
`in-place updated spawned` 事件（探测 / 采纳 worker）**逐条精确对应**。

根因：cshim 只把请求体塞进 IDL 的 `prompt` 字段，worker 侧靠
`_unwrap_payload` 从 `payload["prompt"]` 里把客户端 JSON 捞回来。探测用的
`op="info"` 请求**没有 attachments**，捞回来后 `attachments` 为空 →
`run_prompt(prompt, None)` → 路由到 `run_text()` → OmniBackend 报「你得传
AudioAttachment 才会走 run_audio」。

代价很小（错误在推理前抛出，8ms 一条），但它会**占用 worker 的串行
replier pump**。诊断时别被这批日志带偏 —— **先看有没有真的发过 start
请求**。

### 前端：一份实现，不两份拷贝

音轨分析面板的实现在 **`app/static/core/audio-analysis.js`**（共享模块）。

原来它在两处各有一份拷贝：
* `app/apps/video_detection/static/app.js` —— 真正在用的
* `app/apps/audio_analysis/static/app.js` —— vd 卡的**旧副本**，而且那个
  hidden 卡片的模板里根本没有 `vd-audio-*` 这些 DOM id，副本里连 `VD`
  对象都没定义 —— 是一份跑起来会 ReferenceError 的**死代码**

两份拷贝必然漂：多路改造只改一份，另一份就退回「只分析第 1 路」。

**共享模块的接口**：`mount({ apiPrefix, getSources, onSnapshot })`。源从哪
来由调用方给（core 不该知道具体控件）。`onSnapshot` 里要自己调
`DemoGuard.report()` —— 共享模块只管渲染，「页面在跑」的判断留给调用方。

### 真机实测（本机 RK3588，32 路带音轨视频文件）

素材：`/userdata/audio720p/aud-00..31.mp4`，各 30s，AAC 44.1k 立体声，
**音高各异**（220→561Hz）。每个文件解码后恰好 479987 样本 = 29.999s
→ 严格 5 段（30s ÷ 6s 窗口，尾巴 0.999s 归入第 5 段）。

| 项 | 结果 |
|---|---|
| 轮转顺序 | **严格 0→31**，5 轮，每轮完整 32 路 |
| 事件 | 160 条（32 路 × 5 段），**0 错误** |
| 单次推理 | 0.70s |
| 总耗时 | 165.6~166.3s（160 × 0.70s + 开轮转 3.3s） |
| 窗口 | 每路逐段 +6.0s 精确递增，`duration_s` = 6.0 |
| 结束判定 | 全部放完 → `state="done"` |

> **「32 路描述互不相同」这条要打折看**（2026-10 复核修正）：描述**唯一**的
> 程度取决于**提示词**，不取决于路由。实测同一批素材：
>
> | 提示词 | 32 路首段描述唯一数 |
> |---|---|
> | 「请转写音频内容并简要描述环境音。」（通用） | **15~19 / 32** |
> | 「请描述这段音频的音高、音色与节奏特征…」（强调音高） | **29 / 32** |
>
> 通用提示词下模型会把很多合成音调归成同一句（「音频内容是音乐，环境音描述为：
> 背景中有持续的电子嗡嗡声。」）—— 这是**模型描述粒度**问题，不是串源。
> **验证路由要靠逐字对拍**，不要靠「文本各不相同」：
> 「32 路多跑」与「逐个单跑同一文件」的首段描述逐字比对，全部一致才算路由
> 正确。

**对拍（验证「第 i 路分析的就是第 i 个文件」）**

做法：先跑一轮 32 路多路分析，取每路首段描述；再把 32 个文件**逐个**单路
跑一遍（每次 `stop` → `clear` → `start`），取各首段描述；两组逐字比对。

| | 修复前 | 修复后 |
|---|---|---|
| 逐字一致 | 24/32 | **32/32** |

修复前那 8 个不一致**不在多路侧**——是单跑那侧被**上一轮会话的迟到事件**
污染了（`single[i]` 拿到的是第 i-1 个文件的结果，表现为「整体错位一格」）。
这个偏差本身成了定位「会话串扰」缺陷的线索：因为如果是多路侧错位，应该是
**32 路全部**错位，而不是只有 8 路。

**开轮转 3.3s 是「起 32 个采集线程」的成本**：每个线程 `av.open` 一次容器
（~200ms）。**必须并行** —— 早先在主线程里串行探测 32 个容器，`open()`
要 6.5s。探测已经搬进生产者线程（见 `FileAudioSource.start`）。

### 文件源的「滞后」不是实时性滞后

事件里的 `lag_s` = 推理完成时刻 − 该段被**产出**的时刻。

* **RTSP 源**：`lag_s` ≈ 推理时间（0.7s），因为段是实时取的 —— 这就是
  真正的「描述滞后」。
* **文件源**：整条 30s 音轨在 **0.3s 内就解完了**（解码极快），所以
  `lag_s` 反映的是**分析进度**（这段音频排在队列第几位），实测最大 110s。

所以文件源的 `lag_s` **不能**当成「描述有多旧」来看。32 路文件源下，最后
一段的描述会在 165s 之后才出来 —— 这是「顺序扫完全部音轨」这个语义的
必然结果，不是缺陷。

> 顺带一个产品问题（本期不做）：文件源是**顺序扫完**，与视频的**实时播放
> 位置**并不同步。要让「音画对齐」，得让音频窗口跟着视频播放位置走
> （而不是顺序扫），那是另一套语义。

### 踩过的坑：stop 后立刻重开，旧会话的残留段会**渗进新会话**

**症状**：32 路跑到一半点「停止」，立刻换源再点「开始」—— 新会话的事件列表
里混进了**上一轮**的段：某几路出现 6 段（应为 5 段），`inferred` 计数比
实际段数多，一轮 32 路也比正常慢（两个线程抢同一个 Omni 锁）。

**实测（修复前）**：一轮跑满本该 160 段（32 路 × 5 段），实际 **171 段**，
11 路变成 6 段；耗时 182.5s（正常 ~165s）；而**跑在另一个会话前面的那一轮**
完全正常（160 段、id/ts 严格递增）—— 说明缺陷只在「上一轮被**中途**停掉」
时出现。

**根因是四个叠加的洞**（`app/core/audio/multi_session.py`）：

1. **`close()` 的 join 超时等于「假装停了」**。`t.join(timeout=3.0)`，但一次
   Omni 推理最长可跑 `INFER_TIMEOUT + 5s`（65s）。join 一超时，代码就把
   `self._rr_thread = None` 当作「线程没了」—— 线程其实**还活着**。
2. **`open()` 替换了 `self._stop_evt`**，于是旧线程下一轮看到的
   `self._stop_evt.is_set()` 读的是**新** Event（未 set）→ 旧线程的退出
   判据失效，它继续用旧 channel 列表把旧源队列里缓冲的段（队列深 2）
   一条条推理出来，**每条都推进新会话的事件列表**（`self._events` /
   `self._inferred` / `self._round` 都是会话级共享字段）。
3. **在途结果没有代际校验**。`_rr_gen` 只在 `while` 顶部查一次，
   `_infer` 内部一次都不查 —— 任何在途结果无条件投递。
4. **轮转线程的收尾块也没有代际校验**。`for` 循环跑完后的
   `self._round += 1` / `self._state = STATE_DONE` / `_notify_state()`
   三行都在收尾块里。旧线程跑到这里时 `_channels` 已被 `close()` 清空，
   于是它会把新会话的 `round` 多加一轮、把状态改成 **DONE**（用户点的是
   「停止」），日志还会打出 **`audio-rr: 全部 0 路分析完毕`** —— 「0 路」
   就是 `close()` 清空后的 `_channels` 长度。**这行日志是本缺陷的指纹**：
   正常结束只会在真正跑完时打一次「全部 32 路分析完毕」。

   > 实测指纹（修复前，journalctl）：连续 30 次「全部 0 路分析完毕」，
   > 跨 67 秒 —— 正好对应当时连续跑的 32 次单路会话（每次 ~2.2s 一次）。
   > 每一行都是「stop 之后旧线程的收尾块把状态改成了 done」。

还有一个更隐蔽的窗口：**`open()` 里换 `_channels`（新）和换 `_stop_evt`
（新）都发生在 `_rr_gen += 1`（新）之前**。旧线程正好落在这个窗口里，
会读到**新**的通道列表、而代号校验却通过 —— 它去消费新会话的源。

**修复（三处，缺一不可）**：

* `open()` 里**代号最先推进**：`self._rr_gen += 1` 放在换 `_channels` /
  `_stop_evt` **之前**，顺序反了就没有那个窗口；
* `_rr_loop` 的 `for ch in channels:` 里**每路都查**代号 —— 一轮 N 路 ×
  ~0.7s = 十几秒，只查 `while` 顶部的话旧线程能白跑完整轮
  （与视频侧 `mc-vlm-rr` 的既定做法一致）；
* `_infer()` 拿到结果后、**动任何状态之前**校验代号，过期就整段丢弃
  （不记事件、不记 `inferred/errors`、不改 `_current`）。

> 代号校验的位置很关键：必须在**改状态之前**。晚了就会把 `inferred`
> 计数和「当前通道」指针一起算歪 —— 事件列表看着正常，进度条却是错的。

`close()` 的 join 仍只等 3s —— **不能为了等一个卡住的推理把 stop() 拖成
65s**。正确性靠代号校验兜底：旧线程会在**当前这次推理结束**时自行退出，
它的结果被丢弃。join 超时时打一条 `LOG.info` 说明，不静默。

**这个坑与视频侧的 `mc-vlm-rr` 是同一类**（停一次立刻再起 → 线程身份 /
代号判据失效 → 两个线程一起轮转）。视频侧当时是「判据改成线程身份」，
音频侧这次用「代号 + 每路都查 + 结果丢弃」三件套 —— 后者更严，因为音频
的段是有序的，串一段就等于内容错乱。

**回归（修复后，真机 RK3588 + 真实 Omni 模型）**：

| 场景 | 事件数 | 每路段数 | id / ts | inferred | round | 备注 |
|---|---|---|---|---|---|---|
| 干净跑满 32 路 | **160** | 5（chunk_idx 0..4） | 严格递增 | 160 | 6 | 耗时 166.3s，0 错误 |
| 跑到一半 stop → 立刻重开 | **160** | 5（chunk_idx 0..4） | 严格递增 | 160 | 6 | 耗时 164.9s，0 错误 |

* stop 之后状态**保持 `stopped`**（修复前会被旧线程改成 `done`）。
* 日志里 `audio: 丢弃过期结果 ch14 段1（会话已换代）` 出现过一次 —— 正是
  旧线程在途结果被代际校验拦下的现场，说明守卫在真实时序下确实生效。
* 修复前后的指纹对比：`audio-rr: 全部 0 路分析完毕` **0 次**（修复前 30 次）。

兼容性回归同步通过（19 项）：单值 `source` 向后兼容（5 段、progress=[5,5]）、
无音轨路 `state=no_audio` 且 0 段而其余路不受影响、停止后不再产事件、
>32 路报 400、源不存在报 404、空 source 报 400。前端浏览器回归 10 项通过
（共享模块加载、32 路源标签、状态「分析中 6/160 段 · 第 1 轮 · 通道 6/32」、
事件带「通道 N」、三张视频卡无 pageerror）。

### 真机实测之二：真实带音轨素材（补上「合成音调不算真实验收」的缺口）

**素材现状**（先查清楚，别再问「用哪些带音轨的文件测的」）：

| 目录 | 带音轨 | 说明 |
|---|---|---|
| `/userdata/assets/720p/` | **0 / 32** | 32 个 `rk-*.mp4` **全部无音轨** |
| `/userdata/assets/uploads/` | **0 / 20** | 同样全部无音轨 |
| `/userdata/test/` | **34 个** | **真实监控视频**，带真实现场音（人群拥挤 / 停车进出 / 地铁 / 城市街道），总时长 2427.7s |
| `/userdata/audio720p/` | 32 个 | **自造**：视频轨取自 `rk-*.mp4`，音轨是**合成**的三谐波 + 噪声底（音高 220→561Hz 各异） |

→ **要做依赖真实音频的验收，素材在 `/userdata/test/`，不在 `assets/`。**

**32 路真实素材验收**（32 个真实文件，含 4 个 md5 相同的内容副本）：

| 项 | 结果 |
|---|---|
| 通道数 | 32 |
| 段数 | **399**（各路按自身时长：1 段 ~ 69 段） |
| 错误 | **0** |
| 轮数 | 70 = 最大段数 69 **+1**（最后一轮是「全部放完」的收尾轮） |
| 每路段数 vs 素材实际解码段数 | **逐路一致**（32/32） |
| 耗时 | 304.1s（399 × 0.71s + 开轮转） |
| 保留事件的 id / ts | 严格递增 |
| 每路窗口 | 逐段 +6.0s 连续 |

**跨通道确定性**：4 个 md5 相同的文件放在 4 个不同通道上，描述**逐字一致**
（`音频内容为：'哔哔哔。'，背景环境音是低沉的嗡嗡声和轻微的沙沙声。`）——
说明同输入必然同输出，通道之间互不串味。

**环境音描述与场景对得上**（真实素材的真实体现）：

| 素材 | 模型描述 |
|---|---|
| 驾驶员在车内出闸 | 「背景环境音为远处的汽车喇叭声和鸟鸣声」 |
| 4K 停车厂车辆出入 | 「环境音为汽车引擎怠速运转的声音」 |
| 绿色停车场景区（闸机） | 「'哔！哔！哔！…'，背景音是环境噪音」 |
| 城市夜晚人群拥挤路口 | 「环境音是一只鸟在鸣叫」 |

**但「转写」那一半基本是幻觉**：「you are」「嗯，我也是。」「你真可爱。」
—— 这些监控片段里**没有清晰人声**，模型把「转写」槽位填成了通顺的废话。
**环境音那一半可用，转写那一半不可信**；提示词里点名「没有清晰语音时只描述环境音」
会好一些，但那是模型侧的事，本期不做。

> **保留上限 200 条在长素材下会「挤掉」早期通道的事件**：399 段跑完只剩最后
> 200 条，最先跑完的通道事件已被裁掉。事件列表只保证**最近 200 条**，
> 不是全量归档。`per_channel[].chunks_done` 不受此影响，进度是准的。

### 踩过的坑：hidden 卡重复 mount → 事件框里「两套结果反复横跳」

**症状（用户实测）**：填 32 路带音轨的视频源，点「分析音频」，事件列表里
出现**两套结果来回横跳**——一套 27 条，另一套 180+ 条。

**根因**：`framework.js` 的 `_preloadCardScripts()` 在 boot 时把**所有卡片
（含 `hidden: true` 的）的 JS 都注入执行**。于是：

* `app/apps/video_detection/static/app.js` → `AudioAnalysisCore.mount(...)`（实例 A）
* `app/apps/audio_analysis/static/app.js`（hidden 卡）→ **也** `mount(...)`（实例 B）

而 hidden 卡的模板里**没有**音轨面板，它 mount 时
`document.getElementById("vd-audio-events")` 拿到的是**「音视频描述」卡那个
同名元素** → **两个渲染器写同一个 `<ol>`**。

**为什么两套长度还不一样**（27 vs 180+）：面板上的「分析音频」按钮只有
**最后挂载**的实例拥有 `onclick`，也只有它的 `onStart` 会把本地事件列表
清空；另一个实例的列表**留着上一轮的旧事件**继续 append。于是 DOM 在
「本轮 N 条」和「上一轮 180+ 条」之间来回跳 —— 用户看到的就是「两套结果
反复横跳」。

> 诊断抓手：`page.add_init_script` 里包一层 `WebSocket` 数连接数。
> 实测修复前 **2 条** `ws://.../api/audio-analysis/ws`，修复后 **1 条**。

**修复（三层）**：

1. **hidden 卡不再 mount**（`app/apps/audio_analysis/static/app.js` 改成
   空实现 + 注释说明）。面板归「音视频描述」卡所有 —— 只有那里才有真正的
   DOM、`readVdSources()` 和 `DemoGuard.report()`（离开页面时停后台）。
   hidden 卡只负责后端 API（manifest 的 `register_api`）。
2. **共享模块幂等**：`mount()` 用 `ids.events` 作键做 `_instances` 去重，
   同一套 id 重复 mount 直接返回旧实例（并 `console.warn`）。将来任何
   重复挂载都不会再污染 DOM。
3. **键用 id 字符串而不是元素对象**：元素被重新渲染后换了对象，但旧实例
   通过 `document.getElementById` 动态解析、仍会写进新元素 —— 所以
   「同一个 id 只允许一个实例」才是对的语义。

**回归**：浏览器实测 mount 1 次 / WS 1 条，事件条目数序列
`3→5→7→…→38` **单调不回退**（修复前在两套长度间来回跳），计数标签与 DOM
条目数始终一致。

### 踩过的坑：worker 进程没了但 registry 还报 `running` → 每段都超 60s

**症状**：Omni worker 进程消失后，音轨分析**照样能点开始**（start 返回
200），但每一段推理都要等满 60s 才失败，界面上只看到一条条「推理失败」，
399 段就是好几个小时。

**根因**（`app/core/compute/registry.py`）：

* `_probe_instance` 的退出条件里有「worker 报 `running` → probe 不再需要」
  → worker 起来之后**就没有人再盯它的存活**了；
* 失联判定（`inst.status = "error"`）**只对 `_source == "adopted"`（局域网
  远端 peer）生效**，本地 worker（`spawned` / `adopted_local`）进程死了
  没人管 → `status` 永远停在 `running`；
* 机器内存只有 7.9GB，`free` 常年只剩 ~470MB，worker 被清掉并不奇怪
  （实测 `compute_worker for qwen2.5-omni-3b exited rc=0`，且同期
  `172.16.9.11` 这台局域网另一台机器在访问本服务并 adopt 远端实例）。

**修法（在音频侧熔断，不动共享 registry）**：
`MultiAudioSession` 加 `CONSEC_FAIL_LIMIT = 3` —— **连续 3 段推理失败**就
熔断整个会话，置 `state="error"`，错误文案「连续 3 段推理失败（Omni worker
可能已失联），会话已停止」，并经 service 透传到前端（原来 `_on_session_state`
把会话的 error 吞了，前端只看到「错误: 」后面是空的）。

> 「连续 3 段」而不是「任意 1 段」：正常路径下偶发单段失败是可能的
> （音频太短等），但连续 3 段**不同通道**都失败基本只有一个原因 ——
> worker 没了 / RPC 通道断了。熔断也要**排除「结果作废」**（会话换代时
> 丢弃的在途结果不算失败，否则 stop→重开会被误熔断）。

**实测**：杀掉 Omni worker 后，第 3 次连续失败时
`state=error error='连续 3 段推理失败（Omni worker 可能已失联），会话已停止'`；
第 3 次的原始错误正是「功能未启用: swcap_audio_chat」——registry
这时才发现 worker 没了（晚了 3 次）。

> **registry 层面的根治**（本地 worker 存活探测）**本期未做** —— 那是共享
> 组件，影响所有模型/应用，应该单独评估。当前熔断只保证音轨功能不会
> 无限空转。

## 视觉深度信息v3（Depth Anything V3）—— 本地算力的第三个「非 LLM」模型

「算力配置 → 本地算力」里新增了一个模型条目 **`[Depth]视觉深度信息v3`**
（worker manifest：`tools/worker_manifests/DepthAnythingV3.json`，随模型文件夹
一起分发到 `/userdata/models/Depth-Anything-V3/`）。

### 它为什么和别的模型不一样

前 7 个 worker 全是「文本生成器」（llm / vlm / tts / omni / asr / embedding），
走的是同一条路：`LLMLocalSession` → `respond()` → **文本**。
DA3 不是生成器 —— 它是**单目深度估计**：图像进，**逐像素深度图 + 置信度**出。

> **当前部署的是 1-view / 280×280 导出**（2026-11-02 重新导出）——
> `global` 段输入 `[1,401,1,768]`、`head` 段输出 `[1,1,280,280]`，
> **一张图即可出深度图 + 置信度**。
>
> **换 V 不用改代码** —— view 数是**导出时**参数（model-zoo 的 `--views`，
> 已验证 1-10），backend 从模型上读 V，重新导出三段 RKNN 即可。
> 实测（RK3588）：有效 view 数 10→5 只损失 **1.58%**、→1 损失 **4.14%**，
> 收益递减但未饱和；**顺序不重要**（逆序 3.98%）；**跨场景拼帧灾难性**
> （偏 62%，confidence 崩到 1/3）—— 实时流的多个 view 必须来自同一段连续视频。
>
> **多相机几何当前走不通**：需要相机位姿，而位姿靠官方模型的 **ray map**
> 隐式表示，model-zoo 的导出脚本只出 `depth`+`depth_conf`、**不支持 ray**；
> 且论文数据显示 DA3-Base 的位姿精度只有 DA3-Giant 的 1/4
> —— 位姿比深度更吃模型规模。详细推导见 wdnai 仓
> `examples/README.md` 的「view 数与实时流」一节。

所以它需要三处新的东西：

1. **新的能力词表** `swcap_vision_depth`
   （`wdnai/capability.py::SoftwareCapability.VISION_DEPTH`）——
   能力词表是 SDK 里的**封闭枚举**，manifest 里写没写过的能力会在
   `workers/__init__.py::_validate` 直接被判非法，模型**根本不会出现在
   本地算力列表里**（不是报错，是静默消失）。
2. **新的 backend kind** `"depth"`
   （`wdnai/backends/depth_anything.py`）—— `build_backend` 按 `kind`
   选实现，认不出来就 `ValueError`。
3. **新的 ModelSpec 路径字段** `depth_local_path` / `depth_global_path` /
   `depth_head_path` —— DA3 的 RKNN 导出是**三段**，不是一段。

### 三段式 + 共享显存：不共享就 MODEL_SETUP fail

```
local   images [1,280,280,3] uint8 NHWC  -> local_tokens [1,401,768]   fp16
global  tokens [1,401,1,768]             -> feat_5/7/9/11 [1,400,1,1536] fp16
head    feat_* [1,400,1536]              -> depth / depth_conf [1,1,280,280] fp16
```

> 上面这组形状是**当前部署的 1-view 导出**的实测值（从 model report 的
> `modelData` 里读出来的，不是从文件名推的）。多视图导出时 V 那一维变 10，
> `head` 的输出变成 `[1,10,280,280]`。

三段**共享**一份按 NPU 核分配的内部显存（C++ demo 的
`da3_init_internal_mem_share`，8 核合计 ~1.6 GB）。少这一步
（`config.user_mem_internal = 1` + `rknn3_set_internal_mem`）
→ `rknn3_model_init` 直接 **`MODEL_SETUP fail`**，表现为
「功能未启用: swcap_vision_depth」。

> **view 数是烤进导出的**，不是运行期参数：`global` 的输入形状就是
> `[V,L,1,D]`，V=1 的模型只吃 1 张图。送 3 张会被 backend 明确拒绝。

### 部署侧文件命名（跟 LocateAnything-3B 同一套约定）

模型文件**不再做软链** —— 直接从 model-zoo 的产物 copy 成真实文件，命名跟
`/userdata/models/LocateAnything-3B` 一致：

```
local_DepthAnythingV3.280x280.1view.1828.rknn   + local_DepthAnythingV3.280x280.1view.weight
global_DepthAnythingV3.280x280.1view.1828.rknn  + global_DepthAnythingV3.280x280.1view.weight
head_DepthAnythingV3.280x280.1view.1828.rknn    + head_DepthAnythingV3.280x280.1view.weight
```

即 `<role>_<Model>.<WxH>.<N>view.1828.rknn`，`.weight` 是**去掉 `.1828` 段**
的同名文件（不是把 `.rknn` 换成 `.weight`）。SDK 侧
`wdnai/backends/depth_anything.py::depth_weight_path()` 按
`.1828.rknn` → `.weight` 派生，老的「`.rknn` → `.weight`」只作兼容兜底。

> **`.280x280` / `.1view` 只是文档**：backend 从张量属性上读真实几何
> （分辨率 / view 数 / token 数），**从不解析文件名**。改了导出只需重发
> 文件 + 改 SDK 的 `_DEPTH_ANYTHING_V3_REQUIRED` / `_default_depth_anything_v3_paths`。
>
> **model-zoo 侧的 `da3_base_*.rknn` 保持原名** —— C++ 参考实现的
> `make_weight_path()` 只做「`.rknn` → `.weight`」，读不了新命名；
> `scripts/verify_da3_parity.py` 就是拿 model-zoo 的原始文件当 C++ 侧、
> 拿部署侧的新命名当 Python 侧，两边比的是**实现**不是权重。

### 像素侧：对着 C++ demo 逐位对齐（含三个坑）

参考实现是 `rknn3-model-zoo/examples/depth_anything_v3/cpp/main.cpp`，
wdnai 侧必须和它的输出**逐位相同**，否则深度图会差一两个 fp16 ULP。
三处不对齐就会翻车：

| 环节 | C++ 的实际行为 | 不对齐的后果 |
|---|---|---|
| 预处理 resize | GCC 把表达式**融合成 FMA**（`-ffp-contract=fast`） | 0.4 % 像素差 1 LSB → depth 均值差 ~5e-4 |
| fp16→fp32 | `float16.h` 的 `fp16_to_fp32` **不处理 fp16 非规格化数**（指数域 0 按 0 处理） | DA3 的 token 里确实有这类值 → 81 % 的 global 特征差 1 个 fp16 ULP |
| 统计均值 | `double sum` 累加 | numpy float32 `.mean()` 在第 6 位小数上偏一 |

回归防线：`python -m scripts.verify_da3_parity`（**占 NPU**）——
并排跑 C++ demo 与 `wdnai/examples/depth_anything_v3.py`，逐项比对
张量统计与热力图。

> **别在 NPU 调用中途 kill 进程**：SIGTERM 落在 `rknn3_run` 里会把 RK1820
> 的 PCIe 链路弄坏（`rknn3_startup` 之后报
> `ddr load addr request failed`，C++ demo 也跑不动），**只能整机重启恢复**。
> 排查时用 `py-spy dump --pid <pid>` 看栈，别急着 kill。

## 视频实时深度估计卡片（`video-depth`，插件仓 `wdnapps_video_analyzer`）

把 DA3 做成一张**多路实时**卡片：左半边播放多路视频流，右半边显示**每一路
对应的深度热力图**。卡片源码在插件仓
`wdnapps_video_analyzer/cards/video_depth/`，宿主侧只提供能力
（`app/core/depth/`）。

> **DA3 已按 V=1 重新导出**（2026-11-02）—— 上面那节写的「单路深度估计
> 尚未实现」是导出时的状态。单 view 的实测：稳态推理 ≈116 ms（local 14.8 +
> global 50.3 + head 51.5，≈8.6 FPS），比 V=10 快 11.5×。卡片按**单 view**
> 实现（送图张数必须 == 模型 view 数）。

### DepthDetector：为什么不继承 `WdnaiDetector`

`app/core/depth/depth_detector.py` 是**另写**一份，不是 `WdnaiDetector`
的子类。`WdnaiDetector.start()` 里全是 VLM 专属逻辑（image_size 校验、
工具注册表、prompt 必填、schema 解析），继承过来会剩一堆永不执行的死代码；
而且共享模块的改动要**真实测试所有受影响的后端**，违反「改动只触及目标
功能及其直接依赖」。

它只实现 `multi_api` / `multi_session` 需要的**鸭子类型契约**：
`slot_count` / `start` / `stop` / `snapshot` / `events` / `events_since` /
`latest` / `clear` / `diag_history` / `clear_diagnostics` /
`register_broadcaster` / `submit_frame` / `submit_frame_to` /
`wait_idle` / `wait_slot_idle`。

`snapshot()` 的**字段名必须与 `WdnaiDetector.snapshot()` 逐字对齐**
（`available` / `init_error` / `session_ready` / `model_loading` /
`slot_count` / `queue_size` / …）—— 前端状态条、`wizard-start.js` 的能力
探测、`model-pick.js` 的多卡判定都按那套字段名读，改名会让共享组件读到
`undefined`。

### turbo 调色板：宿主**自实现**，不能 import 上游

`wdnai.backends.depth_anything` 在模块顶层 import 了 `._common` →
`.._librknn` → `rknn3lite`。**宿主进程碰 NPU runtime 是越界的**（模型归
worker 所有），所以 `app/core/depth/palette.py` 自己实现了一份纯 numpy 的
turbo：5 次多项式 + `[0,1]` clamp + `(uint8_t)(c*255+0.5)` 舍入，与 C++
demo 的 `turbo_color` **逐位一致**。

回归防护在 `scripts/verify_video_depth.py`：**从上游源码里 `ast.unparse`
出三个通道的多项式表达式并直接求值**，与本地实现的输出逐字节比 ——
不 import 上游（import 就会拖起 rknn3lite），但比的是「多项式本身」。

> **显示量是 `1/depth`，越大越近**（红端）。方向搞反了画面**不会报错**，
> 只是把近处画成远处 —— 所以测试里钉死了这个方向。

### 深度图走**事件通道**，不走二进制帧通道

二进制帧通道是「视频流」：每路每帧一张显示尺寸 JPEG，由解码线程广播。
深度图不是视频流 —— 它是**推理产物**（频率低一个量级），而且必须与深度
数值（色标窗口、统计量）严格配对。走事件通道能顺带把 `stats` /
`infer_ms` / 通道号一起带上，前端画右栏时不必再猜「这张热力图是哪一路、
什么时候的」。

深度图**不进** WS 的二进制帧（32 路时带宽直接爆），也**不改**
`multi_api.py`（共享代码）—— 卡片自己的 `app.js` 消费事件即可。

worker 只回**原始深度数组**；turbo 着色、色标窗口、JPEG 编码都在主进程做
（符合「worker 是薄 RPC server」）。

### 色标窗口：跨路共享

`DepthWindow` 每路各自维护一档 EMA（alpha=0.3），`shared=True`（默认）
时取**跨路并集** —— 同一个窗口才能横向比较不同摄像头。渲染用「先更新本路
EMA、再取窗口」的两段式，避免同一轮里先到的路被后到的路立刻改色。

### 深度事件历史：从 UI 挪到 log，用 debug 开关控制

UI 上**不再有**「深度事件历史」列表（`vdp-event-list` / `vdp-events-count`
连同 `app.js` 里那一整套事件列表渲染代码一起删了）。原因有两个：页面里挂
200 条历史 + 每条一张整帧热力图是实打实的内存账（单条 JPEG 20~29 KB），
而**看历史**这件事本身是排障需求，不是日常操作。

历史改由 `core.depth` 的日志承担，**默认关**：

```python
LOG.info("[depth-event] id=%d ch=%d npu=%s pos=%d "
         "depth=[%.3f,%.3f] mean=%.3f window=[%.3f,%.3f] infer=%.0fms", ...)
```

开关有两个入口（`app/core/depth/depth_detector.py::depth_debug_enabled`）：

| 入口 | 取值 | 生效时机 |
|---|---|---|
| 环境变量 `WDNWEBUI_DEPTH_DEBUG` | `1/true/yes/on` 开，`0/false/no/off` 关 | **不用重启**（每次调用都读 `os.environ`） |
| `config.jsonc` 的 `debug.depth_event_log` | bool | 改完要重启服务 |

* **环境变量显式写了 `0/false` 时压过配置项** —— 否则运维想在这台机器上
  临时关掉日志却关不掉（配置项仍然是 true）。
* 读不到配置（异常 / 文件缺失）按**关**处理，不抛异常。
* `snapshot()` 里带 `debug_log` 字段 → 前端状态条会追加「· 事件日志:开」，
  页面上能直接看出当前这台机器是不是开着日志。
* 后端的 `GET /events` **仍然保留**（诊断 / 排障用），只是前端不再拉它 ——
  `refreshVdpDetector()` 改拉 `GET /prompt`（detector 快照），几百 KB 的
  事件历史不再每轮刷新都拖一遍。

真机验证（本机 RK3588，两路 720p）：开关关 → journal 里 0 条
`[depth-event]`；开关开 → 12 秒 44 条，字段齐全。

### 色标图例：说明文字放在色标条**下面**

原来「远 ← 1/深度 → 近」是**横排**在色标条右边的，读起来像右边那一段另有所指
（到底是「这一段是远」还是「这一段另有一个意思」）。改成**条在上、说明在下**
（`.vdp-legend` → `flex-direction: column`），压在这条色标下面才是「这条色标的
含义」。

浏览器端验证脚本会做**几何断言**（说明文字的顶边必须 ≥ 色标条的底边）——
只看「有没有这个元素」红不了这种排版回归。

### 送检尺寸：会话仍按源算，但**送检前在宿主侧缩到模型输入尺寸**

`MultiChannelSession` 的送检尺寸默认按源算（720p → 640 长边），深度页
**不**覆盖它（DA3 的输入是固定 280×280 的**拉伸**，没有「vision 变体」
可选，送检尺寸不决定精度档位）。

但**送检的那一帧在宿主侧会被缩到模型的真实输入尺寸**再发出去
（`app/core/depth/depth_detector.py::_rga_resize_for_model`）。原因：

* 上行负载 640×523 的 BGR = **981 KB**，而模型输入只有 280×280 = 230 KB；
* worker 侧那条 stretch（`preprocess_image_rgb`，numpy 实现，实测
  **17.9 ms**）塌缩成「源尺寸 == 目标尺寸」分支，只剩一个通道翻转。

**尺寸不写死**：worker 回的负载里带 `height` / `width`（模型真实输入
尺寸），第一帧送整帧、回包学到尺寸，之后每帧按它缩。`snapshot()` 的
`slots[].input_size` / `slots[].prescaled` 能看到学到没、缩了多少帧。

> **RGA 的三条硬约束**（踩过才知道，全部是 `librga` 报的）：
>
> 1. **目标缓冲的 width stride 必须 16 对齐**。DA3 的 280 不是 16 的倍数，
>    直接 `resize_bgr` 会被拒：
>    `dst unsupport width stride 280, bgr888 width stride should be 16`。
>    解法是**开一个 16 对齐 stride 的目标缓冲**（可见宽度仍是 280），
>    RGA 写进去之后把可见区域切出来 —— `wrap_virtual(..., wstride=288)`。
> 2. **不支持「一边缩一边放」**：x 缩 y 放会直接 `failed to generate task
>    req`。所以只做**纯缩小**；目标比源还大就保持原样（让 worker 自己拉）。
> 3. **bilinear 的缩小倍率上限约 4×**（288×157 → 72×72 行，64×64 就被拒）。
>    真机是 640×523 → 280×280（2.3× / 1.9×），在范围内。
>
> RGA 失败时**不缩**（原样送整帧）：worker 侧还会照常拉伸，结果依然正确，
> 只是慢一点。**不做软件缩放兜底**（项目规则）。

**代价：深度值会漂一点**。RGA 的插值核与 worker 侧那份 numpy 实现（照 C++
demo 逐行复刻的）不是同一个，实测：

| | 平均绝对差 | 相关系数 | 均值漂移 |
|---|---|---|---|
| 喂给模型的输入张量（280×280×3 RGB） | 6.5 / 255 | — | — |
| 深度图 | 0.165 m（占量程 5.9%） | **0.999** | 1.254 m → 1.418 m |

结构完全一致（相关 0.999），是**单目深度的绝对尺度**对重采样核敏感 ——
色标窗口是按分位自适应的，所以画面观感几乎不变。

深度图按**原帧**的宽高比回拉（长边 560）—— 注意用的是**原帧**尺寸，不是
预缩后的 280×280，否则会变成正方形、与左侧视频对不上。于是左右两栏覆盖
同一个视场（实测：视频 canvas 960×785 vs 深度 canvas 560×458，比例
1.2229 vs 1.2227）。

### 踩过的坑：卡片不在 `index.html.template` 里 = 卡片**不存在**

卡片模板**不是运行时 fetch 的** —— `app/core/html_assembly.py` 在拼
`index.html` 时就把每张卡的 `static/template.html` **内联**进去，占位符是
`{% card_slot "<card_id>" %}`。

漏了这一行的症状极其隐蔽：manifest 能发现、`/static/apps/<id>/template.html`
返回 200、API 全在、卡片也出现在应用网格里 —— 但**页面上根本没有这个卡片**
（`ROUTER.showPage` 遍历 `.page` 元素时找不到它，表现为「点了卡片没反应 /
白屏」）。`scripts/verify_video_depth.py` 现在会断言这个占位符存在。

### 踩过的坑：`swcap_to_model` 的 `npu_index` 决定向导的**就绪判定**

`wizard.json` 的 `swcap_to_model` 写 `npu_index: [0,1]` → 就绪要求
**那几张卡**上有 running 实例；写 `null` → 任意一张卡 running 即可（但
「一键开启」的 POST 会带 `npu_index: null`，后端会拒）。
深度页写 `[0, 1]`（与 vtl / vtd 同款）：实例跑在别的卡上时向导会显示 ✗，
用户会以为功能没启用。

### 踩过的坑：关掉浏览器标签会**停掉后端会话**

DemoGuard 的 `beforeunload` 钩子会 `sendBeacon` 打 `/close` + `/prompt/stop`。
所以「先在别处开好视频 → 再开浏览器验证」的流程必然看到**空画面** ——
不是卡片坏了，是上一个标签关掉时把会话停了。浏览器端验证脚本必须
**自己**拉链路（开路 → 启动检测 → 再看画面）。

### 真机实测（本机 RK3588，两路 704×576 源，单 slot 跑在 NPU0）

**宿主端预缩前 → 预缩后**（同一台机器、同样两路源、40 秒采样 150+ 次）：

| 项 | 预缩前（送 981 KB 整帧） | 预缩后（送 230 KB） |
|---|---|---|
| `infer_ms` p50 | 185.7 ms | **172.8 ms** |
| `infer_ms` p90 / max | 209.9 / 223.9 ms | **180.6 / 193.1 ms** |
| 单路深度事件率 | 1.82 /s | **1.97 /s** |
| 两路合计事件率 | 3.67 /s | **3.95 /s** |
| 同通道事件间隔 p50 | 539 ms | **504 ms** |
| WS 投递延迟 p50 | 14.2 ms | 16.9 ms（噪声） |
| WS 视频帧 | 24.9 fps/路 | 24.9 fps/路（不变） |

> **尾部收窄得比中位数明显**（p90 −14%、max −14%）—— worker 侧那条 numpy
> stretch 的耗时本来随源尺寸抖动，去掉之后整条链更稳。

**整链路的耗时分布**（一次轮转 = 271 ms，预缩前；预缩后 249 ms）：

| 段 | 预缩前 | 预缩后 |
|---|---|---|
| 轮转间隙（等解码线程下一次解码出送检帧 + base64 + 取路） | ~37 ms | ~37 ms |
| **`infer_ms`**（RPC 上行 + 模型 + 回传 + 解析） | **185.7 ms** | **172.8 ms** |
| 宿主后处理（解码负载 0.7 + `1/d` 0.5 + 色标窗口 1.5 + turbo/JPEG 25.0） | 27.7 ms | 27.7 ms |
| slot 释放 + 轮询 + GIL 竞争 | ~20 ms | ~20 ms |

**`infer_ms` 里面装了什么**（模型侧另起进程在 NPU1 上单测，V=1，30 次）：

| 段 | p50 |
|---|---|
| 预处理 stretch 640×523→280×280（**预缩后这段归零**） | 17.9 ms |
| 有效掩码 | 2.1 ms |
| local 段 | 20.9 ms |
| global 段 | 51.9 ms |
| head 段 | 56.0 ms |
| 负载组装 base64+json（408 KB） | 9.9 ms |
| **模型侧合计** | **163.8 ms** |

> **三段模型 128.8 ms 是物理下限**（global + head 占 108 ms）。186 ms 里真正
> 跑模型只有 129 ms，**57 ms 花在搬运和编解码上**。

**其它**：单次深度推理（含 RPC + 着色 + JPEG）172.8 ms；深度热力图 JPEG
20~47 KB（base64 后 p50 34 KB）；事件历史只留最近 12 条带图；色标窗口跨路
共享（两路事件用同一档窗口）；**端到端（帧 → 深度图上屏）≈ 250 ms**。

> **两个还没动的浪费**（要动得改共享代码或 SDK，先记着）：
>
> 1. **`frame_b64` 深度路径根本不用** —— `depth_detector.py` 里它只在签名
>    出现，从未进 event、也没参与推理。但每次推理仍在 `multi_session` 里
>    base64 编码一张 90 KB 显示 JPEG（→120 KB）并走 RPC 上行。
> 2. **下行 408 KB 里有一半是置信度图** —— `confidence_b64` 主进程从不
>    解析（`DepthResult.text` 在 `include_maps=True` 时把两张图一起
>    base64，wdnai 没有单独的开关）。

**本机 NPU 现状（2026-11-02）**：`pcie_upgrade_tool ld` 只枚举到
`0003:31:00.0` 与 `0004:41:00.0` 两张卡；**`0004:41:00.0`（卡号 2）上
DA3 的 `rknn3_init` 会挂死**，最终报 `rknn_local init_runtime failed`
（同机的 LocateAnything 在卡 1 上正常）。**卡 0 / 卡 1 上 DA3 正常**。
排查用 `py-spy dump --pid <pid>`（栈会停在 `wdnai/_librknn.py` 的
`rknn3_init`），**别 kill** —— 见上文「别在 NPU 调用中途 kill 进程」。

### 面板动态预览：`preview_video` 与素材落位

首页应用网格里每张卡的缩略图**不是静态图**，而是
`manifest.preview_video` 指向的一段 mp4 —— `framework.js:renderAppPanel`
把它渲染成 `<video class="app-card-video" autoplay muted playsinline
preload="metadata" loop>`（`app/static/core/framework.css` 的
`.app-card-video` 用 `object-fit: cover` 铺满 16:9 的占位框）。

`video-depth` 这张卡以前 `preview_video` 是空的 → 面板里那块位置是**空的**
（不报错，只是没东西）。现在指向
`cards/video_depth/static/preview.mp4`（608×352 / 24fps / 3.0s，左半边原画、
右半边 turbo 深度热力图），与 `video-detection` / `vtd` / `vtl` 同款。

三条容易踩的：

* **素材必须落在卡片自己的 `static/` 目录**（`cards/<card>/static/preview.mp4`）。
  服务把 `/static/apps/<id>/` 挂到**卡片目录**上，不是仓库根 —— 把 mp4 丢在
  仓库根不会被任何卡片看到。
* **改 `preview_video` 不用重启服务**：`index` 路由每次请求都重新
  `assemble_index_html(discover_cards())`（`app/main.py:index`），所以 manifest
  的改动下一次刷新就生效。但**新增卡片**要重启 —— `/static/apps/<id>/` 的
  挂载和 API 注册都是启动时做的。
* **预览要能真的播**：文件得是浏览器能解的 mp4（H.264 + AAC），而且必须
  **faststart**（`moov` 排在 `mdat` 前面）—— 否则浏览器要**下完整个文件**
  才起播，面格里先黑一阵。

#### faststart：`scripts/mp4_faststart.py`（无损改容器，不重编码）

`moov`（索引）在文件尾 = 浏览器必须先把整个文件拉完才知道怎么播。本机
**没有 ffmpeg**（也没有 ffprobe / PIL / cairosvg），所以这个脚本是纯
Python 实现：搬 box（`ftyp` → `moov` → `mdat`）+ 修偏移表。

搬 `moov` 会让 `mdat` 的载荷整体后移 `delta` 字节，于是 `stbl` 里
`stco`（32-bit）/ `co64`（64-bit）的**绝对偏移**必须跟着加 `delta` ——
漏了就是「能播但画面是花的 / 直接解不出帧」。脚本**只改容器**，像素数据
一个字节都不动，落地前会**逐帧哈希比对**（OpenCV 解码，改写前后必须
逐帧一致）才替换原文件。

出现 `sidx` / `saio` / `iloc` / `mfra` 这类本脚本不理解的偏移机制时，
**拒绝处理**并报错 —— 那些表的语义不同，瞎改会得到「能播但坏」的文件。

```bash
.venv/bin/python scripts/mp4_faststart.py --check <mp4>...   # 只体检
.venv/bin/python scripts/mp4_faststart.py --fix  <mp4>...    # 就地改写
```

> 本机曾有两张卡的素材是 `moov` 在尾：`tts`（`wdnapps_audio_chat` 仓）
> 与 `video-target-localization`。已用该脚本改成 faststart —— 改写后
> 视频轨 73 个样本、音频轨 97 个样本**逐字节哈希不变**。

回归：`scripts/verify_app_panel_preview.py` —— 对**所有**声明了
`preview_video` 的卡断言：文件在磁盘上存在且是**合法且 faststart 的**
mp4 → HTTP 200 + `Content-Type: video/*` → 浏览器里那张 `<video>`
解出画面（`videoWidth > 0`）、**在动**（`currentTime` 推进，注意 loop
短视频会回卷）、且画到 canvas 上**不是黑屏**（只看 DOM 存在红不了
黑屏/不播）。

```bash
.venv/bin/python scripts/verify_app_panel_preview.py            # 默认打 8000
.venv/bin/python scripts/verify_app_panel_preview.py --base http://127.0.0.1:8000
```

### favicon：`/favicon.ico` 的 404

页面没声明图标时，浏览器会**默认去请求 `/favicon.ico`** —— 根路径本来没
这个文件，于是每次访问都留一条 404（console 里是一条 `Failed to load
resource`，access log 里是一条 404）。

修法两处，缺一不可：

* `app/static/index.html.template` 的 `<head>` 里声明图标（**下次刷新就
  生效**，因为 `/` 每次请求都重新拼 HTML）：
  ```html
  <link rel="icon" href="/static/core/favicon.ico" sizes="32x32" />
  <link rel="icon" href="/static/core/favicon.svg" type="image/svg+xml" />
  ```
  ICO 在前、SVG 在后：认 SVG 的浏览器（Chrome / Edge / Firefox）用 SVG，
  不认的（Safari / iOS）自动退回 ICO。
* `app/main.py` 里加一条 `/favicon.ico` 路由兜底（Safari / iOS / 书签
  / 爬虫**即使页面声明了图标也可能直接请求根路径**）。**路由是启动时
  注册的** → 这条要**重启服务**才生效；页面里那两条 `<link>` 不用重启。

图标本体由 `scripts/make_favicon.py` 生成（SVG + 16/32/48 三档 ICO，
**从同一份描述**产出，不会走样）：图形沿用侧栏「应用面板」那个
`#i-app-list`（蓝底 + 三条横线 + 右端三点），底色取产品的 `--accent`
`#2563eb`。本机没有 PIL / cairosvg，ICO 是 numpy + OpenCV 画 4 倍尺寸
再降采样（抗锯齿），编成 PNG 后按 ICO 容器格式打包。

```bash
.venv/bin/python scripts/make_favicon.py     # 重新生成两个图标
```

## 算力配置页卡「加载中」：两条根因（2026-11-03 实测）

用户报「浏览器打开 `#compute-config` 一直卡在加载中」。**不是**模型缺失、
也不是 NPU 没起来 —— 是两条各自独立的缺陷叠在一起。

### 根因 1：远程（ETH）NPU 的 SMI 命令**永不返回**

`/api/system-monitor` 60s 超时都不返回（curl exit=28、HTTP 000、0 bytes）。
`py-spy dump --pid <pid>` 抓到 6 个线程全停在同一处：

    get_device_info (wdnai/_librknnsmi.py:583)   ← C 层，无内部超时
    device_id = 2

SMI 设备表是 `[0:pcie, 1:pcie, 2:eth]` —— device_id=2 是**经网口接入的
另一台** RK 板（`172.16.9.x:11900`）。SMI 的设备级命令（`get_device_info`
/ `get_device_temperature` / `get_device_power` …）要过 PCIe/ETH 通道去问
对端 EP 固件要遥测，对端不保证应答，而 **C 层没有超时**。

> **这条会拖垮全站，不止这一个页**：这些调用跑在 `asyncio.to_thread` 里
> —— 用的是 **event loop 的默认线程池**（上限 `min(32, cpu+4)` = 12）。
> 前端每 3s 轮询一次 → 每轮漏一个**永久卡死**的线程 → 约 36s 内默认
> 线程池被占满 → 全站所有 `asyncio.to_thread` 一起排队阻塞。

修法（代码在 `app/system/api/system_monitor.py`）：ETH 设备一个 SMI 命令都
不发；采集走**专用线程池**（不占全站默认池）+ 整体超时；超时后熔断；
NPU 遥测加 10s TTL 缓存。超时后**不要**再走 CLI fallback —— CLI 问的是同
一批设备，只会再 fork 一个注定超时的子进程、白等 5s。

### 根因 2：模型目录里的**大 JSON** 被当 manifest 解析

`/api/compute` 每秒轮询，`discover()` 会把模型目录里**每个** `*.json` 都
`json.loads` 解析一遍 —— 包括 `tokenizer.json`：

    /userdata/models/Qwen3-TTS/tokenizer.json   11,423,986 B (11.4 MB)

单次全量解析 ~2s。`discover()` 在**请求路径**（`/api/compute` 每秒轮询）
和**后台路径**（`_model_rescan_loop` 每 2s 重扫）各跑一次 → event loop
常年被堵 + GIL 被抢。

`py-spy record` 采样（14s、60Hz、853 个样本）：**671 个样本停在
`json.loads`** —— 占 80%。

修法：只解析 `< MAX_MANIFEST_BYTES`（64KB）的 json。canonical 名
（`worker.json`）超限时**仍然解析**，好让它走「解析失败 → `status=fatal`」
的既有分支，而不是静默消失。

> **教训**：模型目录**不是**「只有 manifest」的地方。往里放 tokenizer /
> config 这类大 json 是**正常**的 —— 扫描端必须假设目录里有任意大小的
> json，不能靠文件名猜。

### 实测

| | 修复前 | 修复后 |
|---|---|---|
| `/api/system-monitor` | 60s 不返回 | **0.13s** |
| `/api/compute` | 2.36s | **0.015s** |
| `discover()` | 340ms | **0.7ms** |
| 首屏并行拉两端点 | — | **0.15s** |
| 15s 内 20 次轮询 | — | 0 失败，最慢 0.401s |
| 卡死线程 | 6（且持续泄漏） | **0** |

### 诊断手法（下次照样用）

1. **`py-spy dump --pid <pid> --locals`** —— 看卡死线程停在哪一行、局部
   变量是什么（本次靠 `device_id: 2` + `devs` 里的 `"mode": "eth"` 定位到
   远程设备）。
2. **`py-spy record --pid <pid> --duration N --format raw`** —— 采样
   profiler，按栈聚类看时间花在哪（本次 80% 落在 `json.loads`）。
3. **访问日志里没有这一条 = 请求发出去了但没返回**，而不是没发出 ——
   uvicorn 在**请求结束时**才记访问日志。

### 回归

- `python -m scripts.verify_smi_eth_guard`（17 条，不占 NPU）
- `python -m scripts.verify_manifest_scan_cost`（8 条，不占 NPU）

### 补充（2026-11-03 晚）：`mode` 字段不可靠 + EP 哑了 39 小时

上面那条「ETH 设备靠 `mode` 认」的修法有个前提：**`get_device_list()` 的
`mode` 字段跟这个库的版本对不上**。本机实测的设备表：

```
slot 0  mode=0x0      → 0001:11:00.0   （PCIe，本机）
slot 1  mode=0x10001  → 0003:31:00.0   （PCIe，本机）
slot 2  mode=0x20002  → 0004:41:00.0   （PCIe，本机）
slot 3  mode=0x3      → ETH peer      ← **不是本机卡**
```

* PCIe 设备填的是 `0` / `0x10001` / `0x20002`，**不是** `CommMode.PCIE == 0`；
* 那个 ETH 槽位**有时**填 `2`（`"eth"`，上游能认出）、**有时**填 `0x3`
  （`"unknown(3)"`，**上游认不出**）—— 同一台机器、不同的设备表状态。

认不出时的后果（实测）：对端不应答 → `nsmi_eth.c` 的 socket 没有超时 →
**永久阻塞**；对端应答 → 回的是**垃圾**（`received error status 344100`、
`Data size exceeds max allowed size: 28 > 4`）→ C 层每轮刷 10 行 ERROR
（实测 69 行 / 37 秒），偶尔还被当成温度读出来（页面显示成 59℃）。

**补的判据：槽位规则** —— 只探前 N 个槽位，N = 本机 `/dev/pcie-rkep-*`
节点数（N 和 NSMI 的 PCIe 槽位数一致 —— 同一套 PCIe 枚举）。
实测：69 行 / 37s → **4 行 / 36s**，温度照常（45/42/45℃），
对端机器仍出现在结果里、标记 `status="Remote"`。

**另一件独立的事：EP 哑了 39 小时。** 2026-11-01 12:00~12:02 连续三次
`rknn3_startup stop/start`（每次都 `pcie_upgrade_tool uf
/lib/firmware/rknn3_rk1820.img -reset` 重刷并复位 EP）之后，EP 的 NSMI
遥测全哑 —— cmd 2/4/6/9/11/13 超时、cmd 7/12 有应答但回 error、
`rknn-smi info` 全 NA，持续到 **2026-11-03 03:40**（约 39 小时）。

排除了三种解释：固件被降级（镜像 md5 与 10-24 那份一致 `f7244237…`）、
PCIe 链路坏（`pcie_err` 全 0、5GT/s x1 无 AER）、存在旁路（hwmon 里
**没有**协卡温度 —— `hwmon6=npu_thermal` 是 RK3588 自带的 NPU，
`hwmon8=ina226` 是功耗传感器）。恢复动作：`rknn3_startup restart`
（重刷 + 复位）之后 EP 在几小时内恢复应答（chip 温度 40/44/44℃、
固件 1.1.0、`Total EP device num: 3`）。→ **「EP 不应答」是可恢复的状态，
不是固件能力缺失。**

这个状态下**每条命令是「超时失败」而不是「挂死」**（110ms 一条），
所以上游那条「超时才熔断」的机制**不会触发** —— 探针能跑完，但每轮
（10s TTL 缓存）刷十几行 ERROR。补三条「**不发注定失败的命令**」：

1. **chip 列表为空 → 跳过全部 chip 级命令**（npu 占用 / 显存 / health）。
   原来 fallback 到 `chip_id = 0` 照发不误 → 每卡每轮白发 3 条。
2. **温度这条路不通 → TTL 内不再发温度命令**（`_TEMP_STATE`，120s）。
   SDK 的回退只在 **chip 级成功** 时才记住「设备级不通」，chip 级也失败
   时它每次都重发 cmd 12 → C 层每轮每卡刷一行。
3. **PCIe 相关命令全灭 → 静默期**（`_SMI_QUIET_UNTIL`，60s）：期间
   **一个 SMI 命令都不发**（没有调用 = 没有 C 层 ERROR），并把
   「链路无响应」如实带给前端。

### 回归（补充部分）

- `python -m scripts.verify_smi_local_slot_guard`（13 条，不占 NPU）
- `python -m scripts.verify_compute_npu_telemetry_note`（8 条，起浏览器）

## 算力卡号（`npu_index`）的稳定口径（2026-11-03 晚，v0.30）

### 现象

`config.jsonc` 的 `compute.autostart[].npu_index` 是用户写死的**稳定句柄**
（「这个模型跑在卡 1」）。但某天开机后，深度实例**少起了一个** —— 而
`config.jsonc` 一个字没改。

### 根因：驱动枚举列表里混着「对端机器」

`RKNN3Lite.get_devices_id()` 返回的列表里混着三类设备：

| 类别 | device_id 形态 | 真机实例 |
|---|---|---|
| 本机 PCIe EP 协卡 | PCI BDF | `0001:11:00.0` / `0003:31:00.0` / `0004:41:00.0` |
| **mdns 发现的对端机器** | `IP:端口` | `172.16.9.139:11900`（后变 `.159`） |
| 本机 RGA / VPU 设备 | USB 序列号 | `180B6D6C…` |

对端机器**随时会上下线**（实验室里另一台板子），而它一旦在，就占住列表
**最前面**的下标 —— 本机协卡的下标整体后移 1：

```
对端离线：[0001:11:00.0, 0003:31:00.0, 0004:41:00.0, …]   → 卡号 0/1/2
对端在线：[172.16.9.139:11900, 0001:11:00.0, 0003:31:00.0, …] → 驱动下标 1/2/3
```

旧实现直接拿**驱动枚举下标**当卡号，于是：

1. `config` 里的 `npu_index: 0` 在对端上线后**不再指向本机卡 #0**；
   `_crop_autostart_for_cards` 判定「该卡不在算力卡位内」→ **静默丢弃**
   那条 autostart（只留一行 `LOG.info`）→ 少起一个实例，UI 上完全无感。
2. 更危险的是主进程与 worker **各枚举各的**：两次枚举结果不一致时
   （主进程枚举时对端离线、worker 枚举时对端上线），同一个卡号在两边
   指向**不同的物理卡** —— 模型会被加载到另一张卡上。

### 修复：卡号 = 本机 EP 协卡按 PCI BDF 排序后的序号

规则只有**一处实现**（`app/core/compute/cards.py`），三个使用者共用：

| 使用者 | 怎么用 |
|---|---|
| `registry.list_cards()` | 枚举一次、编号一次；`cards` **只含本机 EP 协卡**，`npu_index` = 卡号 |
| `workers/_base._build_session()` | 自己再枚举一次，用**同一套规则**把卡号解析成真实设备 |
| `system_monitor._wdnai_canonical_device_index()` | `bus_id` → 同一个卡号，系统信息页与算力页的 `#N` 对齐 |

三层保障：

1. **卡号稳定** —— 对端在不在，本机卡的卡号都不变（`card_no → device_id`
   映射恒定）。
2. **按物理身份选卡** —— 主进程把卡号对应的 **PCI BDF**（`device_id`）随
   load RPC / `--device-id` 一起发给 worker，worker 用
   `LLMLocalSession(model=spec, device_id=…)` 选卡 —— **与枚举顺序完全
   无关**。wdnai 的 `npu=` 是位置语义，只有 `device_id=` 才是物理身份。
   worker 侧仍保留「自己枚举 + 同一套规则换算」作为兜底（老主进程没发
   `device_id` 时）。
3. **失败不再静默** —— autostart 条目被裁掉时：
   * 日志级别从 `INFO` 提到 **`WARNING`**；
   * 条目记进 `registry._autostart_skipped`，经 `snapshot["autostart_skipped"]`
     暴露，算力页顶部渲染一条**黄色横幅**（「N 条开机自启配置未生效」）。

> **不要用驱动枚举下标当卡号。** 它会被 mdns 对端机器挤动。
> 新增代码里凡是要表达「哪张卡」，一律走 `app/core/compute/cards.py`。

### 为什么按 BDF 排序而不是按出现顺序

驱动枚举顺序**不保证稳定**（对端插队、PCIe 重枚举都可能改变）。BDF 是
物理拓扑，排序后是确定的 —— 而且真机实测驱动本来就是按 BDF 递增给出的
（`0001` < `0003` < `0004`），所以换成 BDF 排序**不改变对端离线时的既有
卡号**，只是把「对端在线时的漂移」消掉了。

### 回归

- `python -m scripts.verify_card_number_stability`（39 条，不占 NPU）
  —— A 卡号规则 / B registry 编号 / C autostart 不被丢弃且留痕 /
  D worker 按卡号解析到同一张物理卡 / E 监控侧 `bus_id → 卡号` 对齐。

## Qwen3-ASR 卡：拖动进度条 → 识别跟过去（v0.33）

用户报的 bug：播放含音轨的视频（例：98 分钟的《圆桌派》）时，**在 UI 拖动
进度条**，字幕**不显示拖到的那个位置**的语音内容 —— 画面在播第 40 分钟的
声音，字幕还停在第 5 分钟。

### 根因：识别是「从头顺序」的，拖到后面 → 那里**还没识别**

服务端一直是从文件开头**顺序**识别（`iter_media_pcm` 边解边喂）。识别速度
（RTF ≈ 0.18）比播放快 5~6 倍，所以**顺着播**时字幕总是有内容；但用户
把进度条**拖到后面**，那个位置压根还没轮到识别 → 没有字幕可显示。

实测（98 分钟《圆桌派》）：点开始 12 秒后已识别到 97 s；此时把播放头拖到
2400 s → 那边一个字都没有。

### 修法：让识别**跟到拖动位置**

| 层 | 改动 | 关键点 |
|---|---|---|
| `app/core/audio/decode.py` | `iter_media_pcm(..., start_sec=, end_sec=)` | PyAV 先 `container.seek(...)` 再解；WAV 按采样数跳 |
| `app/apps/qwen3_asr/service.py` | `transcribe_stream(..., append=, start_sec=)` | `append` 保留已有轮次；`start_sec` 把时间轴起点对到绝对位置 |
| `app/apps/qwen3_asr/api.py` | `POST /transcribe` 收 `start_sec` / `append` | 表单字段 |
| `app/apps/qwen3_asr/static/app.js` | `_subRanges` / `_maybeFollowSeek` / `_followSeek` | 拖动 → 停掉当前轮 → 从新位置重开 |

**时间戳语义不变**：轮次的 `chunk_start_sec` 仍是**绝对**音频时间（音轨
相对起点）→ 前端的时间轴算法不用改。

> ⚠️ **seek 的定位精度**（实测 98 分钟 mp4）：PyAV 的
> `container.seek(off, stream=stream, backward=True, any_frame=True)` 落点
> 误差 **±20 ms**、耗时 **0.04 s**（跳到 5000 s 也一样快）。
> 落点不一定正好等于请求的位置 → 解码器把**第一块样本的容器时间**
> 报给调用方（`MediaPcmChunk.media_start_sec`），服务端用它把
> `audio_sec` 对到绝对位置 → 字幕不会错位。

### 前端的三个要点

1. **已识别区间**（`_subRanges`）：每来一条 round 帧就记一段
   `[chunk_start_sec, chunk_end_sec]`，相邻的合并成一片。拖动时靠它判断
   「这个位置识别过没有」。
2. **引导（lead-in）= 8 秒**：新会话的头 4 轮**不允许提交**（SDK
   `asr.py:1067` 的 `chunk_idx < rollback_rounds`）→ 从拖动点**正好**开始
   的话，前 4 秒 committed 恒为空 → 字幕在那里是空的。往前多带 8 秒，
   模型就有上下文了（不会「零上下文 + 半个句子」→ 碎片），而且这 8 秒正好
   把热身期吃掉 → **拖动点本身就有字幕**。
3. **一次只能跑一个识别**（RPC session 只有一条 delta 通道，见
   `service.py::_ASR_RPC_LOCK`）→ 所以是「**先停再开**」：
   `POST /stop` → 等状态离开 `running` → `POST /transcribe`（带
   `start_sec` + `append=true`）。

### 多段识别的时间轴怎么拼

轮次来自**多段**识别（每次拖动重开就是新的一段）→ epoch 的时间**不一定
按顺序来**（用户往回拖过一次就会倒序）。时间轴是按「最后一个
`start ≤ 播放头` 的行」找当前行的 → 必须先按**绝对时间排序**。

重叠区（两段识别到了同一段音频）保留**先开始**的那一段 —— 它是在有上下文
的时候转写的（后一段从中间开始，起点是零上下文）。

### 实测（板端真实素材，98 分钟《圆桌派》）

```
拖之前: rounds=97 已识别到 97.0s 区间=[[0, 97]]
拖动 → 2400.0 s
  [ 0.0s] 识别已跟到 2395.4s（rounds=101）
  已识别区间 : [[0, 99], [2393, 2464]]
  时间轴     : 48 行，覆盖到 2463.4 s
  当前行     : 「但我觉得这个爽感一直是这样的。」   ← 拖动位置的内容 ✓
```

### ⚠️ 已知取舍

* **转写文本会缺一段**：拖动会**中断**当前那一轮 → 被跳过的那段
  （旧识别末尾到拖动点之间）**没有文本**。想补全就再拖回去（会自愈：
  落在没识别过的位置 → 又会重开识别）。
* **拖动会打断正在跑的整段识别**：所以大文件的「识别完成」提示会来得更晚。
* **本机上传的视频**每次重开都要**重新上传**一遍（服务端不缓存上传的
  文件）→ 但上传上限只有 **32 MB**（`_MAX_UPLOAD_BYTES`），所以长视频必然
  走**设备端路径**（不重传）→ 实际影响很小。

### 回归

* `scripts/verify_asr_wizard.py` 的 **U17**（6 条断言）：
  * 已识别区间被合并成连续的一段 / 区间内判定「识别过」、区间外「没识别」；
  * 多段识别（时间倒序）→ 时间轴按绝对时间排序；两段都被保留；
  * 拖到没识别过的位置 → 前端重开识别（起点 = 拖动点 − 8 s）；
  * 重开后字幕仍显示拖动位置的内容。

## Qwen3-ASR 卡：YouTube 式节奏字幕（v0.31）

`app/apps/qwen3_asr/static/app.js` 的视频来源（**含音轨的视频，识别走它的
音轨**）现在有**跟播放节奏走的实时字幕**：一个字一个字地点亮、正在说的
那个词高亮、说完的行淡出上移。

### 为什么字幕是「算」出来的，不是「推」出来的

服务端对文件/视频是**整文件快转**（`iter_media_pcm` 边解边喂，1 秒一块
`online_chunk_seconds=1.0`），**不与播放同步**。识别结果在用户按下播放
之前就已经全部到手了。

所以「实时字幕」= **一张「字符 → 音频时间」的对照表 + 播放头位置**。
渲染由**播放头**驱动（`video.currentTime`），不是由新帧到达驱动。
这条路径**零后端改动** —— worker / SDK 契约一个字没动。

### 数据链路（round 帧）

服务端每 1 秒给一帧（`app/apps/qwen3_asr/service.py::_on_delta`）：

| 字段 | 含义 |
|---|---|
| `committed` | **本会话内累计**的已确认文本（只增不减，**会被 60 秒切会话清零**） |
| `unfixed` | 本轮未确认的尾巴（**随时会被回滚推翻**） |
| `chunk_start_sec` / `chunk_end_sec` | 这一秒块在**音轨**上的起止（相对音轨起点） |
| `audio_origin_sec` | 音轨在**容器**里的起点（只有 \|origin\|>1e-6 才带） |

播放头换算：`t = video.currentTime - _audioOriginSec`（chunk 时间戳也是
音轨相对时间，二者可直接比较）。

### epoch（SDK 会话）必须分段处理

服务端每 60 秒切一个新会话（`_FILE_SESSION_MAX_SEC = 60.0`）。切的时候
`committed` **归零**、`round` 编号**回到 1**，但**音频时间轴是连续的**
（实测：epoch0 结束于 55.0s，epoch1 的第一轮 `chunk=[55.0, 56.0]`）。

> ⚠️ **committed 不能跨段拼** —— 它是**段内**累积的。跨段直接拼会错位。
> 前端按「`round` 编号回退」或「`committed` 长度变短」认 epoch 边界，
> **每段单独建时间轴**，再按时间把各段的行拼起来。

### 时间映射模型：`lcp` 锚点（实测选出来的）

一个 epoch 内要回答的问题是：**「最终字幕里的第 i 个字，大概在第几秒被
说出来？」**

锚点取 **(本轮假设文本与最终文本的最长公共前缀长度, 该轮 chunk 终点)**：

```js
const hyp = (f.committed || "") + (f.unfixed || "");
raw.push([_lcp(hyp, finalText), Number(f.chunk_end_sec)]);
```

* `committed` **一定是**最终文本 F 的前缀（只增不减）→ 公共前缀 ≥ committed 长度；
* `unfixed` **不一定**是（会被回滚、会改标点 —— 第 1 轮在英文音频上先猜了
  个中文填充词「嗯哼。」，第 2 轮拿到上下文后整个推翻）→ 所以要取公共前缀，
  不能直接用「假设文本的长度」；
* 计数走**高水位线**（回滚时计数不许倒退），否则插值会出现负斜率。

**选型实测**（15 秒英文样本 + 90 秒样本，各按音频能量包络打分；
指标 =「浊音覆盖」= 每个词的时间窗有多少落在真实语音段里，1.0 = 完全对上）：

| 模型 | 浊音覆盖（15 s / 90 s） | 标点误差（90 s） |
|---|---|---|
| 假设长度 `sorted` | 0.728 / 0.689 | 0.549 |
| 假设长度 + 高水位 | 0.728 / 0.689 | — |
| 假设长度 + 起点锚 | 0.737 / 0.712 | 0.255 |
| **公共前缀 `lcp`** | **0.750 / 0.717** | 0.273 |
| 公共前缀 + 起点锚 | 0.720 / 0.722 | 0.269 |

> 工具在 `/userdata/asr_probe/align_probe2.py`（抓 round 帧 + PyAV 能量包络
> + 六种模型打分）。

### 渲染规则（YouTube 那套）

* 当前行 = 最后一个「开始时间 ≤ 播放头」的行；
* 行内**逐词点亮**（还没说到的词**占位但不可见** —— `opacity: 0`，
  整行宽度稳定，字幕不会随着每个词出现而左右抖）；
* 正在说的那个词高亮（`.asr-sub-current`，金色 + 加粗）；
* 上一行保留 `SUB_PREV_HOLD_SEC`（3.2 s）后淡出，画面上最多 3 行；
* 分行：句末标点必断、句中标点够长（≥10 列）就断、超过 42 列断、
  超过 7 秒断；CJK **逐字**成词（中文没空格），拉丁按空格。

### 踩过的坑

* **`timeupdate` 只有 4 Hz** —— 撑不起「逐词点亮」，高亮会一跳一跳。
  播放期间挂 **rAF 循环**（`_startSubLoop` / `_stopSubLoop`），每帧算一次；
  DOM 只在**签名变了**时才动（`_subSig`），否则 60 fps 白折腾 DOM。
* **原生控件条会盖住字幕** —— Chrome 的播放条高约 42 px。字幕层
  `bottom: 42px` 让开它，否则第二行直接被控件吃掉（实测）。
* **模型卡顿时字幕会「闪」过去** —— 同一段音频反复转写后，模型会在一秒里
  把积压的一大段一次性提交（实测 90 秒重复音频：committed 从 480 跳到 548，
  于是 7 行挤进 1 秒）。`_spaceOutLines()` 给每行兜一个
  `SUB_MIN_LINE_SEC`（0.5 s）下限：把后面几行的开始时间往后推，行内的词
  整体平移（保持「词的时间 = 行的时间」）。
* **`unfixed` 绝不能显示** —— 那是会被回滚的瞬时猜测（「嗯哼。」的来源），
  最终文本里根本没有这句。字幕只用**最终确认文本**派生的时间轴。
* **来源切换要清字幕 + 清时间轴缓存**（`_onSourceChanged` → `_subInvalidate`），
  否则会把上一来源的文字画到当前画面上。`_roundsSource` 是第二道保险。

### epoch 边界碎片：**已用「切口对齐说话停顿」修复**（v0.32，方案 A）

症状：长时间视频的**中途**（每 60 秒一次）会冒出一句语法不通的话，或
一段和音频对不上的外语。实测（英文 CS229 讲座 132 s / 中文《圆桌派》
150 s）：

| 位置 | 实际音频 | 修复前的模型输出（epoch 首句） |
|---|---|---|
| 英文 epoch2 @58 s | "…and of the context." | **"In the past, so of the context."** |
| 英文 epoch3 @118 s | "…you give an ID for each token." | **"Then, and then you give an ID for each token."** |
| 中文 epoch1 @41 s | 接上文的半句 | **"要缩短在一分钟以内。"** |
| 中文 epoch2 @121 s | 接上文的半句 | **"老婆说话。基本上不管他说什么…"** |

**三条根因（都在识别层，不在字幕层）**：

1. **每 60 秒切一次 SDK 会话，切口落在句子中间。**
   `service.py::_FILE_SESSION_MAX_SEC = 60.0` + `_rotate_epoch()` → 新的
   `asr_stream_id` + 新的 `asr_stream_origin_sec` → SDK 侧
   `_new_stream_state()` → **decoder 重置 + 音频历史清零**。于是新会话的
   第一轮模型是**零上下文**，而喂给它的音频是**半个句子** → 只能转写出
   碎片。碎片一旦被确认（committed 只增不减）就**原样进最终文本**
   （`service.py:669` 的 `" ".join(segments)`）→ 字幕忠实地显示它。
2. **新会话的头 4 秒不允许提交任何东西。**
   SDK `asr.py:1067`：`elif state.chunk_idx < state.rollback_rounds:
   stable_count = 0`，而 `ONLINE_ROLLBACK_ROUNDS_DEFAULT = 4` → 每个
   epoch 的头 4 秒 committed 恒为空，屏幕上只有模型的**纯猜测**。
   实测英文样本 epoch1 第 1 轮的 unfixed 就是 **`"language"`** —— 模型
   吐出的语种头 `language English[PAD151704]`，那一轮分隔符还没落下来，
   SDK 的剥离（`asr.py:1349-1371` 的 `find(sep)`）没生效 → 原始语种头
   露在 unfixed 里。同类现象：英文音频第 1 轮猜出过中文填充词「嗯哼。」。
   （字幕层只用 committed，**不显示 unfixed**，所以没进字幕。）
3. **模型卡死也会切会话（`reason=stall`），切口位置随机** → 所以症状是
   「偶尔」而不是「每 60 秒」。实测英文样本 epoch0 只跑了 1 秒就被切掉
   （`epoch rotated: rid=78 epoch=1 reason=stall audio_sec=1.0`）。

**字幕层是无辜的**：字幕文本 100% 来自各 epoch 的 committed（最终确认
文本），不含 unfixed；上面那些碎片在**最终转写文本里也有**（主文本区
显示的是同一份东西）。字幕唯一相关的现象是 **epoch 边界有 1~7 秒空档**
（实测 1.0 / 7.4 / 1.0 s）—— 新会话要 4 秒热身才能提交，时间轴锚点也要
等 committed 出现。

#### 修法：把切口对齐到「说话停顿」

60 s 上限**不能突破**（C++ demo 的 `ONLINE_MAX_AUDIO_SECONDS`，每轮都要把
整段历史重新 prefill），所以做法不是「拉长会话」，而是**「在 40~60 s 这个
窗口里挑一个停顿点切」**。两个独立判据，任一命中即视为停顿：

| 判据 | 实现 | 口径 |
|---|---|---|
| ① **自适应声学** | `app/core/audio/endpoint.py::AdaptivePauseDetector` | 当前块电平 < 「最近 30 s 电平**中位数** − 12 dB」 |
| ② **模型侧** | `service.py::_on_delta` | 连续 2 轮「假设文本**内容完全相同**」 |

**① 为什么不能用绝对门限**：真实视频的底噪差异极大 —— 实测《圆桌派》
片段（**有背景音乐**）每秒 RMS 中位数 **-23.6 dBFS**、最低才 -58.7，
150 秒里低于 -30 dBFS 的只有 2 秒；英文讲座的中位数是 -21.1 dBFS。
绝对门限（-40 dBFS）要么切不出、要么误切。**相对**门限能同时覆盖两种
内容：实测真停顿处的电平是 -35.3 / -42.1 / -45.5 / -47.3 / -58.7 dBFS
（比中位数低 **12~35 dB**），而说话中的秒基本都在中位数 **±3 dB** 之内
→ 12 dB 的余量分得很开。用**中位数**而不是平均数：停顿段是少数，平均值
会被拖下来。

**② 判据必须是「内容」，不是「长度」**（v0.32 实测修正）：
`unfixed` 是**当前轮输出的尾巴**（SDK `asr.py:1076`：
`new_unfixed = round_tokens[stable_count:]`，即回退末尾那 5 个 token），
不是累积的假设。人在说话 → 前沿那 5 个 token 每轮都变；人停下 → 模型
没新东西可说，把同一段再吐一遍 → 尾巴**一字不差**。
最初实现比的是「**长度**」没变 → 实测中文《圆桌派》150 秒里这样的「等长」
出现 **21 次**，但只有 **1 次**内容也相同（t=124，真静音，dB=-58.7），
其余 **20 次全是模型在改写**（内容变了、长度恰好一样）→ 在 41 秒处误切
一刀，切口落在句子中间 → 新会话开头就是残句「要缩短在一分钟以内。」。
改成比「**内容完全相同**」之后误报全部消失。

**③ 跨请求的计数器泄漏**（已修）：`_stall_rounds` / `_last_committed_len`
原来只在 `_rotate_epoch()` 里复位 → 上一请求以 stall 收尾时，下一请求的
第一个块就触发切会话 → 首段只有 1 秒 → 开头成碎片。`_run_stream()` 开头
也必须复位。

#### 修复效果（实测，板端真实素材）

| 样本 | 修复前 | 修复后 |
|---|---|---|
| 英文 CS229 132 s | 切点 58 / 118 s，首句 "In the past, so of the context." / "Then, and then…" | 切点 **52 / 96 s**（均 `reason=silence`），三段首句全是**完整句子**："The reason why we say language models are generative models…" / "More concisely, you can write it as a product of p's." / "Great. So autoregressive language models at a high level…" |
| 中文《圆桌派》150 s | 切点 41 / 81 / 121 s（**误判**），epoch1 首句残句「要缩短在一分钟以内。」 | 切点 **60 / 120 s**（`reason=length`），误切那刀消失；epoch1 首句变成完整分句「甚至可以剪接，把我五分钟、十分钟的话减成一分钟。」 |

**整片验证（104 分钟英文讲座，实视频文件，不是切片）**：

| 指标 | 修复后 |
|---|---|
| 音频长度 / 墙钟 | 6271 s / **1105 s**（RTF ≈ 0.18） |
| epoch 段数 | **141** |
| 切点理由 | `silence` **103**（73%）/ `stall` 22（16%）/ `length` 15（11%） |
| 首句短于 12 字的 epoch | **11 / 141（7.8%）** —— 逐条看过，全是正常短句（"Great." / "Okay." / "Um." / "Ah, what?" / "Evil."），**没有一条是语法不通的碎片** |

修复前这个片子有 **141 个**切口，每个都可能产出碎片；修复后只有 11 个 epoch
的**第一句**偏短，而且逐条核过都是正常口语短句。

**字幕层的时间轴**（用真实前端代码渲染）：epoch 边界从「半句 → 碎片」变成
「完整句 → 1~1.5 s 空档 → 完整句」。例如：

```
[ 50.17- 52.00] So slightly                                  ← 修复前这里是 "In the past, so of the context."
[ 53.00- 53.00] More concisely,                              ← 修复后：完整句开头
[ 93.71- 95.50] So I'm going to talk about this one.          ← 完整句结尾
[ 97.00- 97.00] Great.                                       ← 完整句开头
```

⚠️ 顺带发现一个**既有缺陷（未修）**：`language English` 语种头偶尔会**粘进
committed**（模型吐出「language English」时，那个分隔符 token 落在回退区 →
SDK 的 `find(sep)` 找不到 → 语种头被当成正文提交）。实测 **141 个 epoch 里
只有 1 个**（且在 t=0，文件开头），最终文本是干净的（SDK 的 `final_text()`
按 151704 切过）→ **只污染 round 帧**，会漏进字幕第一行。

正确的修法在 **SDK**（`wdnai/backends/asr.py::_OnlineDecoder.add`）：分隔符
没落下时**不要把语种头提交成正文**，先挂起等分隔符。**主进程**去猜「这串
文本是不是语种头」有误伤风险（正文里本来就有 "language models"）。

#### ⚠️ 方案 A **没有**覆盖的情况（残留问题）

**连续讲话（访谈 / 播客）里根本找不到停顿** → 切口只能落在 60 s 硬上限上
→ **仍然会切在句子中间**。实测中文《圆桌派》150 秒只有 **1 处**真静音
（t=124，dB=-58.7），而它在第一个 epoch 的 60 s 窗口之外 → 三个 epoch
全是 `reason=length`。

这不是判据不够好，是**数学上无解**：60 s 到了就必须切，而这段内容在这
60 s 内没有可切的点。彻底解决要靠 **B 回看（look-back）**：切会话时把
上一段末尾 5~6 s 音频**再喂一遍**给新会话，产出后按公共前缀砍掉重复
文本。难点是字符↔秒换算不准 + chunk 时间戳要补偿 + 前端时间轴要处理
重叠区间。

**余量很大（v0.32 实测）**：60 s 上限其实绑得很松 —— 104 分钟的整片识别只用了
**1105 s** 墙钟（**RTF ≈ 0.18**），而模型的 `max_context_len = 1024*24 =
24576` token 远没到。所以「把上限放宽」在**性能上**是可行的（但那会
偏离 C++ demo 的口径，需要单独决策）。

> 实测整片（104 分钟）的切点理由分布：`silence` 73% / `stall` 16% /
> `length` 11% —— 说明**有停顿的内容（讲座、演讲、绝大多数纪录片）**里，
> 方案 A 基本都能把切口落在停顿处；剩下的 `stall`（模型自锁）切口位置
> 由内容决定，是残留问题。

#### 其它方案（未实施）

| 方案 | 做法 | 代价 |
|---|---|---|
| **B 回看（look-back）** | 切会话时把上一段末尾 5~6 s 音频再喂一遍，产出后砍掉对应前缀 | 字符↔秒换算不准；chunk 时间戳要补偿；改动面大（service + SDK origin 语义 + 前端时间轴） |
| C 纯显示层规避 | epoch 开头 5 s 内不显示字幕 | 真实内容也一起被藏；只在 app.js，最快零风险 |
| D 打标记 | 字幕里标出 epoch 边界 | 不解决问题，只是让用户知道 |

> ⚠️ 抓 round 帧时**不要按单引号切**：`cap_rounds.py` 用 `%r` 打印，
> 文本里含撇号时 repr 会换成**双引号** → 按单引号解析会**静默丢掉**那些
> 轮次（看起来像"中间少了一大片"，其实是解析错了）。用
> `/userdata/asr_probe/parse_rounds.py`（正则 + `eval`）。
>
> ⚠️ `cap_rounds.py` 的收帧循环只有 **60 s** 墙钟上限 → 识别超过 60 s 墙钟
> 的文件（长视频）会**截断**。长文件用 `/userdata/asr_probe/capture.py`
> （不限时长，直接落 JSON）。
>
> ⚠️ **浏览器探针会打断正在跑的识别**：只要有页面打开 ASR 卡（哪怕只是
> 导航过去看时间轴），卡片前端就会发 `POST /api/qwen3-asr/stop` →
> 服务端 `_cancel = True` → 识别**当场结束**（实测：104 分钟的全片识别在
> 645 s 处被自己的探针打断，日志里是 `POST /api/qwen3-asr/stop`）。
> 跑长片识别期间**不要**开浏览器探针；要看字幕时间轴，先把识别跑完。


### 回归

* `WDNWEBUI_BASE="http://127.0.0.1:8000" .venv/bin/python scripts/verify_asr_wizard.py`
  —— **116 通过 / 0 失败**。其中：
  * **U14**（字幕随播放头变化）：字幕随播放头变、字幕每个词都在最终文本里、
    累计点亮的字数单调不减、当前词有高亮；
  * **U16**（时间轴结构）：分了多行、行内词的时间递增、行的时间递增、
    相邻行不重叠、没说的词占位但不可见；
  * **U17**（v0.33 拖动进度条）：已识别区间被合并成连续的一段 / 区间内外
    的「识别过」判定 / 多段识别（时间倒序）→ 时间轴按绝对时间排序且两段
    都保留 / 拖到没识别过的位置 → 前端重开识别（起点 = 拖动点 − 8 s）/
    重开后字幕仍显示拖动位置的内容；
  * **U18**（v0.34 文字转写框）：见下面那节。
* `scripts/verify_asr_live.py` —— **49 通过 / 0 失败**（不需要 NPU，也不需要
  服务在跑）。其中 v0.32 新增：
  * **L13**（模型侧停顿状态机）：连续 2 轮「假设文本**内容**相同」→ 计数
    达标；**长度相同但内容变了** → 归零（模型在改写）；假设文本为空 → 归零；
  * **L14**（自适应声学）：持续说话 → 一次都不判；响 20 秒后接 1 秒安静 →
    只命中那一秒；**持续安静 40 秒 → 不会一直判停顿**（滑动中位数跟着降）；
    历史不足 5 秒 → 不判。
* `scripts/verify_asr_long_audio.py`、`scripts/verify_asr_wizard.py` 三个都要跑
  —— 前两个不需要 NPU 也能跑（`verify_asr_live.py` 不占 NPU）。

> ⚠️ `verify_asr_wizard.py` 的 **V6c** 段有个**采样/字节单位**的坑：
> `_best_correlation()` 返回的是**采样**数，而 PCM 是 s16le（1 采样 = 2 字节）——
> 拿它直接切字节会只平移一半；`lag0` 是奇数时切出来的字节数是奇数，
> `np.frombuffer(..., '<i2')` 直接抛「buffer size must be a multiple of
> element size」，**整轮回归中断**（后面的断言全跑不到）。必须切
> `browser_pcm[lag0 * 2:]`。

## Qwen3-ASR 卡：文字显示框 = 字幕同款转写框（v0.34）

用户要求：「含音轨的视频」来源下，主「文字显示框」也按**实时字幕的格式**
输出 —— 同样的分行、同样的逐词点亮、同样的当前词高亮 —— 而且**历史全部
保留**，框可以**上拉回看**。

### 和画面字幕的关系：**同一个渲染，不同的范围**

| | 画面字幕（`.asr-video-sub`） | 文字转写框（`#asr-transcript`） |
|---|---|---|
| 范围 | 当前那 **3** 行，说完淡出 | **整条时间轴**（几千行），一行不丢 |
| 历史 | 不留 | **全留**，可上拉回看 |
| 宿主 | 压在画面上 | 页面上的「文字显示框」（`#asr-live` 里） |

两者**共用同一份时间轴**（`_buildSubTimeline()`）和**同一个词状态算法**
（`_revealState()`）—— 「显示效果一样」不是「看起来像」，是**同一份代码
算出来的**。两边各写一份的话，早晚会对不上（高亮差一帧这种最难查）。

DOM 结构上，转写框**长在**「文字显示框」里面（`#asr-live` > `#asr-transcript`
> `#asr-transcript-body`），所以它占的还是原来那个位置。

* **只在「含音轨的视频」来源下生效** —— 音频文件 / 麦克风来源下，
  `#asr-live` 仍然是原来的单行「已确认文本 + 蓝色未确认尾巴」。
* **只在「实时结果」模式下可见** —— 「识别过程」模式下那块地方让给逐轮明细
  （`#asr-live` 整体隐藏），这是模式本身的契约。
* `#asr-live-committed` / `#asr-live-unfixed` 这两个 span **继续更新**，
  只是被 CSS 藏起来当「镜像」：回归脚本从 `#asr-live-committed` 读最终文本
  （U14 / U15 都靠它），清掉就会把既有用例打挂。

### 三种行状态

| 状态 | class | 含义 |
|---|---|---|
| 过去 | `asr-tr-past` | 播放头已经过了 → 整行可见、正常色 |
| 当前 | `asr-tr-current` | 播放头正在这一行 → 左侧高亮条 + 浅底，**词逐个点亮** |
| 未来 | `asr-tr-future` | 还没播到 → **文字都在**（压暗 0.55） |

> 「未来」的行**文字是可见的**（只是压暗）—— 这是**逐字稿**，不是画面字幕：
> 画面上不能提前剧透，但文字框的用途就是「看全文 + 回看」。
> 只有**当前行**内部才套用字幕那套「还没说到的词占位但不可见」。

### 增量维护（几千行也不能卡）

98 分钟的片子时间轴约 **2900 行**（≈4 万个词）。每帧重建整个 DOM 是不可接受的，
所以：

1. **行按 id 增量维护**（`_transcriptSync`）：时间轴是「往后长」的 → **公共前缀
   直接复用**，只重建变掉的后缀。拖动重开识别导致时间轴**重排**时，重建的是
   重排点之后的部分，顺序自然对齐。
2. **每帧只更新当前行的词状态**（几十个 span）。行的「过去/当前/未来」状态也
   只在**状态变化的那一段**上刷（当前行挪一步就改两行）。
3. 时间轴**重排过**时（`full=true`）才全量刷一遍状态 —— 否则会出现「已经说过的
   行还标着未来（压暗）」的错位。

**实测成本**（板端 headless Chromium，合成 3000 轮 / 550 行 / 11250 个词，
≈98 分钟片子的量级；工具 `/userdata/asr_probe/tr_scale.py`）：

| 场景 | 成本 |
|---|---|
| 一次性建 550 行 DOM | **78~140 ms**（一次性） |
| 识别推进一格（+60 轮 ≈ +11 行） | 中位 **1.2 ms** / P95 3.3 ms / 最大 6.3 ms |
| 播放中每帧（播放头推进 + 重画） | 中位 **0.9 ms** / P95 1.5 ms |
| 当前行每帧都变（跳着看） | 1.3~2.3 ms |

> 有一帧会到 **~85 ms** —— 那是「**首次整表布局**」的一次性开销（DOM 建完
> 之后的第一次 `offsetTop` 读取会强制一次全量布局）。之后的布局都是增量的。
> 想看每一帧的拆分（paint / scroll / 总）用 `/tmp/asr_ref/frame_prof.py`
> 那套写法：`scroll` 那一项就是被 `offsetTop` 逼出来的**强制布局**。

### 滚动跟随：用户上翻时**不把人拽回来**

* `_transcriptScroll`：当前行**看不见**时才滚动到它；已经在视野里 → 一点不动。
* `_onTranscriptScroll`：用户**手动**滚动时，若当前行离开视野 → **暂停跟随**
  （`_trFollow = false`）；滚回来 → 恢复跟随。
* 「自己滚的」和「用户滚的」靠 `_trAutoAt`（上次自动滚动的时间戳）区分 ——
  程序化滚动也会触发 `scroll` 事件。

### 踩过的坑：行的 id 必须覆盖**文本**

`_epochLines()` 原本的行 id 是 `epoch + 行号 + 词数 + 首词时间`。看起来够用，
其实**漏了文本**：还在长的那个 epoch 里，模型可能给某个词**补上标点或空格**
（词数不变、时间也不变）→ id 不变 → 那一行**永远停在旧文本上**。

实测症状（15 秒英文样本）：转写框里的第 4 行是 `him, but in his solo music,`
（少了词尾的空格），而时间轴是 `him, but in his solo music, ` → 框里的正文
和最终文本**差一个空格**。

修法：id 追加**首尾时间 + 本行全文**：

```js
id: epochIndex + ":" + lines.length + ":" + cur.length + ":"
    + cur[0].start.toFixed(3) + ":"
    + cur[cur.length - 1].end.toFixed(3) + ":"
    + cur.map((w) => w.text).join("")
```

> 这个坑**画面字幕也中过招**（槽位重建同样靠 id）—— 修在 `_epochLines()` 里，
> 两边一起好。

### 踩过的坑：`white-space: pre-wrap` 会把**模板缩进**渲染成空行

`.asr-live` 是 `white-space: pre-wrap`（单行模式要保留模型给的换行）。但转写框
**长在它里面**，模板里那些换行 + 缩进就变成了**排版空白** —— pre-wrap 会把
它们**渲染成实打实的空行**。

实测：框顶部凭空多出 **79 px** 空白，字幕和第一行之间还夹着一行空气
（`#asr-live` 高 731 px，其中 133 px 是空行）。

修法：视频来源下（`.asr-live.asr-live-video`）和 `.asr-transcript` 本身都改成
`white-space: normal`（框里只有转写框，没有需要保留的换行）。

> 通用教训：给某个容器加子容器时，先看一眼**父容器的 `white-space`** ——
> `pre-wrap` / `pre-line` 下，HTML 里的缩进是**可见内容**，不是空白。

### 回归

`scripts/verify_asr_wizard.py` 新增 **U18**（12 条断言）：

* 视频来源下文字框变成字幕同款转写框（**可见**，不是 hidden）；
* 转写框保留**全部**历史行（DOM 行数 == 时间轴行数）；
* 转写框**逐行**与时间轴一致（没有停在旧文本上 → 守住上面那个 id 坑）；
* 当前行被标出、当前正在说的词高亮；
* 转写框可滚动（`overflow-y: auto` 且内容超出框高）；
* 「单行实时结果」那两个 span 仍保留最终文本（镜像契约）；
* 用户上翻回看时**不被拽回**（跟随暂停）；
* 画面字幕与文字转写框的**当前行**逐字一致、**当前词**一致；
* 「识别过程」模式下文字框让位给逐轮明细；
* 逐轮明细**增量追加** → 行数与内容都和整表重建一致；
* 切回音频文件来源 → 转写框收起、单行结果恢复显示。

> ⚠️ U18 用例**必须先切到「实时结果」模式**：向导用例跑完会把显示模式留在
> 「识别过程」，而 `#asr-live` 在那个模式下是 hidden → 转写框**没有布局高度**
> （`clientHeight = 0`）→ 滚动相关的断言会莫名其妙地挂掉。

> ⚠️ 「上翻回看」的判据是「**当前行离开视野**」，不是「scrollTop 变了」：
> 当前行还在视野里时跟随本来就该继续（用户没走远），只有当前行不见了才算
> 「去回看历史了」。把滚动位置设成 0 而当前行恰好在顶部 → 跟随不会暂停 →
> 断言会误判。

### 实测：98 分钟真实视频（【圆桌派】EP01，177 MB）

浏览器里点「开始识别」，跑到 done，再逐个播放头位置检查转写框：

| 指标 | 值 |
|---|---|
| 识别墙钟 | **1077 s**（5869 轮，RTF ≈ 0.18） |
| 时间轴行数 | **2921 行** |
| 转写框 DOM 行数 | **2921 行**（与时间轴**逐行完全一致**） |
| 框内可滚动高度 | 90552 px（框高 384 px） |
| 播放头 30 s | 当前行 #4「有风。」 |
| 播放头 3000 s | 当前行 #1499「后来翻译英文，」，当前词「来」 |
| 播放头 5000 s | 当前行 #2565「然后那个母亲，」，当前词「后」 |
| 自动滚动 | 随播放头 121 → 46120 → 79166 px |

**画面字幕与文字框的「当前行 / 当前词」逐字一致**（9 个播放头位置实测，
不一致 **0** 次）：

```
@  3.0s  字幕='Oh yeah, yeah!'            转写='Oh yeah, yeah!'            词 'yeah!' vs 'yeah!'
@  7.5s  字幕='him, but in his solo music,' 转写='him, but in his solo music,' 词 'solo' vs 'solo'
@ 12.0s  字幕='but he did very well when…'  转写='but he did very well when…' 词 'very' vs 'very'
@ 22.0s  字幕="He wasn't even that big…"    转写="He wasn't even that big…"   词 'when' vs 'when'
@ 33.0s  字幕='people.'                    转写='people.'                    词 'people.' vs 'people.'
@ 45.0s  字幕="didn't do overly well,"      转写="didn't do overly well,"      词 'do' vs 'do'
@ 58.0s  字幕="He wasn't even that big…"    转写="He wasn't even that big…"   词 'big' vs 'big'
@ 70.0s  字幕='Uh huh, oh yeah,'           转写='Uh huh, oh yeah,'           词 'Uh' vs 'Uh'
@ 82.0s  字幕="didn't do overly well,"      转写="didn't do overly well,"      词 'well,' vs 'well,'
```

工具：`/userdata/asr_probe/tr_98min.py`（**必须从浏览器里发起**识别 ——
round 帧是 WS 推的，浏览器中途才连上就拿不到前面的帧）。

## 「识别过程」面板：每帧重建整张表 → 拖慢识别本身（v0.34 顺带修）

上面那次 98 分钟的识别**开着页面跑了 1077 s**，而同样片子不开页面只要约
**480 s**。差的那一倍不是错觉，是前端在抢 CPU。

### 根因

`_renderRounds()` 原本是「**每来一条 round 帧就把整张逐轮表重建一遍**」：
`box.innerHTML = ""` + 5869 行 × 4 个元素 ≈ **2.3 万个 DOM 节点**。

实测（板端 headless Chromium，5869 轮）：

| 调用 | 每帧成本 |
|---|---|
| `_renderRounds()`（重建整表） | **中位 59 ms / 最大 74 ms** |
| `_buildSubTimeline()`（缓存命中后） | 中位 1.3 ms / 最大 3.5 ms |
| `_renderTranscript()`（增量） | 中位 1.2 ms |

一轮 = 一秒音频 → 每秒来好几帧 → 每帧几十毫秒 → **浏览器主线程被占死**。

### 修法

1. **面板看不见的时候不重建** —— 「识别过程」模式默认是关着的，数据还在
   `this.rounds` 里，切回该模式时再补（`_applyMode()` 里补调一次
   `_renderRounds()`）。
2. **面板可见时只追加新增的轮次**（`_roundsShown` / `_roundsPrev` /
   `_roundsDirty` 三个记账字段），不重画老行。

改完：**每帧 59 ms → 0 ms**（面板隐藏时）。面板可见时每帧只多一行。

> ⚠️ 「本轮增量」的算法不能改：committed 是**段内累计**的，显示的是
> `committed` 相对上一轮的增量；跨 epoch（60 秒切会话）时 committed 归零，
> 新的一段整段都要显示。增量追加要**跨行携带** `prev`（`_roundsPrev`），
> 否则跨 epoch 的那几行会显示成空。

## Qwen3-ASR 卡：刷新页面后转写框内容丢失 + 不同来源的历史串线（v0.35）

### 现象

用户在「含音轨的视频」来源下识别完，**刷新页面** → 转写框（字幕同款）
整个空了，连「识别过程」的逐轮明细也没了 → 像是白识别一场。

### 根因：轮次只存在**服务端的内存**里

`service.py::_rounds` 是**进程内**的列表。浏览器一刷新，`this.rounds`
归零 → `_renderTranscript()` 画不出任何行 → `_transcriptClear()` 把框清空。

后端其实一直在（`GET /api/qwen3-asr/rounds` 能拿到轮次），但**前端从来没
在刷新后拉过它** —— 那个端点之前只是「谁要用谁用」的工具。

### 修法：恢复，而且**归属由服务端说了算**

```
service.py::_rounds_source   ← 这批轮次归属的来源（kind / name / path）
        ↓
GET /api/qwen3-asr/rounds → {"rounds": [...], "source": {...}}
        ↓
app.js::_restoreFromServer() → 轮次 + 来源页签 + 路径 + 单行文本
```

| 恢复什么 | 谁决定 | 为什么 |
|---|---|---|
| 轮次内容 | 服务端（`_rounds`） | 唯一的事实来源 |
| 归属（视频还是音频） | 服务端（`_rounds_source`） | 见下 |
| 设备端路径 | 服务端记下的 `path` | 播放器要靠它重载 |
| 浏览器上传的文件 | **恢复不了** | `File` 对象刷新后就没了 |

⚠️ **归属必须由后端给，不能让前端猜**：前端只知道「当前停在哪个页签」，
而刷新后那个页签是**默认值**（音频文件）→ 猜出来的归属会把**视频**的轮次
画到音频页签下（或反过来）= 串台。服务端知道这批轮次是用什么发起的
（`api.py::_source_kind` 按**扩展名**判 video / audio）。

服务端侧的口径：

* **不是 append**（新的一批）→ 覆盖归属；
* **append**（拖动后重开，还是同一段媒体）→ **保持原来的**（拖动重开的
  是同一个视频，来源当然不变）；
* `reset()` → 归属一起清。

### 与 WS 帧的去重合并

刷新可能发生在识别**跑到一半**时 → 服务端快照与 WS 帧有时间差。

* 服务端那份是**同一批轮次的前缀**（服务端先把轮次记进 `_rounds`，
  **再**广播给 WS → 浏览器收到的每一条服务端都已经有了）；
* 本地多出来的那几条是「拉取的那一瞬间」新到的 → **追加**在后面。

去重靠 `chunk_start_sec`（**绝对音频时间**，跨 append 也单调）：
⚠️ **不能靠 `round` 序号** —— 拖动重开（append）会开新 SDK 会话，序号从
0 重来，拿序号去重会把新的一段认成旧的。

### 串线守卫：`asr-live-notfile`

单行文字框（`#asr-live-committed`）显示的是「**最近一批**」的文本。切到
**实时来源**（麦克风）时，那行字是**文件批次**的历史，而麦克风的文字在
它自己的框里（`#asr-rt-committed`）→ 藏起来（`asr-live-notfile`），别让
两段文字并排出现。

> ⚠️ **文件 ↔ 视频之间不藏**：那两栏是同一批结果的两种视图（视频来源
> 多一个「字幕同款转写框」）—— 藏了的话，视频的识别结果在切回文件页签
> 后就**彻底看不见**了（U18 的契约，实测踩过：守卫写宽了，U18 直接挂
> 「切回音频文件来源 → 转写框收起、单行结果恢复显示」）。
>
> 真正要挡住的是「**画到画面上**」的那两处：视频字幕 / 转写框 —— 它们靠
> `_roundsSource` 把关（见 `_renderVideoSubtitle`）。**守卫要挡在
> 「画到别人的画面上」，不是「界面上不许出现」。**

麦克风来源本来就不串：实时帧带 `stream_id` → 走 `#asr-rt-committed`
（实时区自己的框），**不进** `this.rounds`，也不进单行文字框。

### 踩过的坑

* **刷新后向导会重挂，把卡片页藏起来** → 回归脚本里点页签会报
  `element is not visible`。用户侧没这问题（走过向导 = `completed=1`
  → 不重挂），但**脚本**必须先把 `wdnwebui.wizard.<app>.completed` 置 1
  （清掉反而更糟 —— 清空了等于「没走过向导」，`tryMount` 照样会挂）。
* **拉取期间用户又点了「开始识别」** → 恢复会把旧轮次盖掉新轮次。
  ⇒ `this._roundGen` 计数器：发起新识别 / 清空就 +1，拉取前后对不上就
  **丢弃这次恢复**。
* **`_switchSource` 会清字幕** → 恢复时**先切页签、再摆轮次**，顺序反了
  会被 `_switchSource` 里的 `_renderTranscript()` 覆盖掉。

### 回归

`scripts/verify_asr_wizard.py` 新增 **U19**（20 条断言）：

* 音频文件识别 → 刷新后：**轮次数量**、**文本逐字一致**、来源页签、
  设备端路径、批次归属全部恢复；
* 切到「含音轨的视频」页签 → 音频文件的轮次**没被画进转写框**、画面字幕
  是空的，但音频文件的文本**没被删**（切回去还能看）；
* 视频识别 → 刷新后：转写框**行数与文本**与刷新前一致、页签回到
  「含音轨的视频」、批次归属是 `video`（不是上一次的 `file`）、路径回填；
* 切到「浏览器麦克风」页签 → 文件那批的单行文本被藏起来、实时区没有
  文件的历史文本；
* API 层：`/rounds` 一起返回归属（`kind` / `path`）、新批次替换旧批次。

### 实测

板端真浏览器（headless Chromium，127.0.0.1）跑 U19：**20 通过 / 0 失败**；
整套 `scripts/verify_asr_wizard.py`：**136 通过 / 0 失败**（原 116 + U19 20）。

刷新前后的对照（`asr_en.wav`，15 s / 16 轮）：

| | 刷新前 | 刷新后 |
|---|---|---|
| 轮次 | 16 | **16** |
| 单行文本 | `Uh huh. Oh yeah, yeah! …` | **逐字一致** |
| 来源页签 | file | **file** |
| 设备端路径 | `/…/asr_en.wav` | **回填** |

视频（`asr_real_audio.mp4` → `/userdata/assets/uploads/_asr_restore_test.mp4`）：

| | 刷新前 | 刷新后 |
|---|---|---|
| 轮次 | 16 | **16** |
| 转写框行数 | 7 | **7** |
| 转写框文本 | 7 行 | **逐字一致** |
| 来源页签 | video | **video** |
| 设备端路径 | `/…/_asr_restore_test.mp4` | **回填**（播放器能重载） |

## Qwen3-ASR 卡：点「停止」后**字幕还在滚动**（v0.36）

### 现象

在「流式语音识别」卡里点「停止」→ 徽标变了，但**画面字幕还在跟着
视频往下滚** → 用户以为识别根本没停。

实测（板端，90 秒样本）：

```
点停止前：state=running  rounds=33  视频在播，字幕跟着播放头推进
点停止后：state=error    error='识别失败: LLMRPCSession[...]: generation stopped'
```

### 根因：**两层**，都不是「字幕画错了」

**第一层：字幕的驱动源是「播放头」，不是识别进度。**

画面字幕是 YouTube 式的 —— `_renderVideoSubtitle()` 按
`video.currentTime` 取「当前行」，每帧由 rAF 循环重画
（`_startSubLoop`，挂在 video 的 `play` 事件上）。

而「停止」原来**只停后端识别**，**没碰视频** → 视频还在播 → rAF 循环
还在跑 → 字幕继续按播放头滚动。**识别早停了，画面没停** ⇒ 字幕看起来
「还在识别」。

> 这跟字幕算法本身无关：字幕**没有算错**，它忠实地反映了「视频还在
> 播」。错的是「停止」没有把**画面**也停下来。

**第二层：后端把「用户取消」当成了「识别失败」。**

`stop()` 会 `session.stop()` → 在飞的那次 RPC 抛
`generation stopped` → 异常从 `_stream_main()` 冒到 `_run_stream()` 的
`except Exception` → 走 `_fail()` → 状态落成 **`error`**（徽标「出错」），
还把 RPC 原文抛给用户。

⚠️ `STATE_STOPPED` 这个常量**定义了却从来没有被赋值过** —— 「已停止」
这个状态在代码里是**死的**。

### 修法

**后端**（`service.py`）—— 把「取消」从「失败」里摘出来：

* `_stream_main()` 记住 `cancelled` 标志：块循环里 `if self._cancel: break`
  时置位；`_submit()` 因为取消而抛异常时**就地吞掉**（取消是预期内的
  收尾，不是失败），跳出循环；
* `_close_epoch()` 在取消时**直接返回**（不再向已停的会话提交收尾调用，
  也不计入 `epoch_errors` —— 那不是 worker 的错）；
* 新增 `_stopped()`：状态置 `STATE_STOPPED`、**保留已识别的文本**
  （`last_result.text`）、广播 `stopped` 帧 + `state` 帧；
* `stop()` **立刻**把状态置成 `stopped` 并广播 —— 不等后台线程收尾
  （那个线程随后还会再广播一次，带上文本）；
* `_run_stream()` 的异常分支也判一次 `self._cancel`（兜底：取消从别的
  路径冒出来时同样按「已停止」收场，而不是「出错」）。

**前端**（`app.js`）—— 「停止」= 识别 + 画面**一起**停下来：

* 新增 `_freezePlayback()`：`video.pause()` + `_stopSubLoop()`；
  播放头不动 ⇒ 字幕自然停住（转写框的跟随滚动也跟着停）；
* `stop()` 成功后调它；
* WS 新增 `stopped` 帧：把已识别的文本交回单行结果 + 提示语说清楚
  「已停止」。

### 设计取舍：「停止」停画面，「完成」不停

| | 识别 | 视频 | 字幕 |
|---|---|---|---|
| **点「停止」** | 停 | **停**（暂停） | 冻结在最后一帧 |
| **自然跑完（done）** | 完 | **继续播** | 继续跟着画面走 |

「完成」时视频继续播、字幕继续跟 —— 那是**设计**（YouTube 式：识别
比播放快 5~6 倍，识别早就跑完，用户是一边看一边读字幕）。「停止」是
用户**喊停**，语义是「都停下来」；想继续看可以自己按播放键，那之后字幕
会跟着**已识别**的那部分继续走（正常）。

> ⚠️ 反过来做（停识别但让视频继续播）会让画面跑到「还没识别到」的位置
> —— 那里没有字幕，画面和字幕对不上。

### 回归

`scripts/verify_asr_wizard.py` 新增 **U20**（12 条断言）：

* 停止前：字幕确实在画面上、rAF 循环在跑（**能复现用户的场景**）；
* 停止后：后端状态是 `stopped`（不是 `error` / `done`）、没有把 RPC 的
  异常当成识别失败、徽标显示「已停止」；
* 停止后：视频已暂停、字幕 rAF 已停、**字幕内容不再变化**、播放头不再
  前进（这三条是用户报的 bug 本身）；
* 停止后：已识别的内容保留（转写框有行 + 单行结果非空）、提示语说明
  「已停止」。

### 实测

板端真浏览器（headless Chromium，127.0.0.1）：

```
停止前：字幕='Uh huh.'  行数=5  播放头=1.44s  暂停=False  rAF=104
停止后：字幕='Uh huh. Oh yeah, yeah!'  播放头=2.82s  暂停=True  rAF=0  徽标='已停止'
```

* U20 单跑：**13 通过 / 0 失败**（12 条断言 + 「页面没有 JS 报错」）；
* 整套 `scripts/verify_asr_wizard.py`：**148 通过 / 0 失败**（原 136 + U20 12）。

API 层（90 秒样本，第 5 秒 stop）：

| | 修复前 | 修复后 |
|---|---|---|
| stop 响应 | `state=running` | **`state=stopped`** |
| stop 后状态 | `error` | **`stopped`** |
| `error` 字段 | `识别失败: LLMRPCSession[...]: generation stopped` | **空** |
| `last_result.text` | `''`（丢了） | **已识别的那部分**（`stopped=True`） |

其余回归：`scripts/verify_asr_live.py` 49/0 · `scripts/verify_asr_long_audio.py`
全过 · `scripts.verify_static_delivery` 53/0 · `verify_capability_groups` /
`verify_model_packaging` 全过。

### 边界：谁发的「停止」

`stopped` 帧比用户点按晚约 1 秒（后台线程收尾才广播）。若用户在这 1 秒里
已经自己按了播放，就**不该**再被冻住 ⇒ 用 `_stopInitiated` 标记「这次停止
是不是本页面发起的」：

* 本页面点的 → 画面在 click 那一刻就冻了 → `stopped` 帧**不再冻**；
* 别的页面 / 别的标签页发的 → 本页面没冻过 → **要冻**（否则那边停了这边
  字幕还在滚 —— 同一个 bug 换个页面）。

新一轮识别开始时 `_stopInitiated` 归零（「谁停的」重新算）。

### ⚠️ 同一个「停止」有两种语义 —— 必须由**调用方**说（v0.36 补丁）

上面那条修复上线后，用户立刻报了新问题：

> 「拖动进度条到目标位置后，视频播放和字幕都暂停了，这是不合理的」

**根因**：拖动进度条会触发「重开识别」（`_maybeFollowSeek` → `_followSeek`），
它的第一步是**停掉正在跑的那一轮**（一次只能跑一个识别）：

```js
await fetch(API_PREFIX + "/qwen3-asr/stop", { method: "POST" });
```

这次 stop 是**前端内部**的（为重开识别），**不是用户喊停**。而我加的
「stopped 帧 → 冻画面」**不区分来源** → 拖一下进度条，视频就被暂停了，
字幕也跟着停 —— 正好把「拖动进度条 → 识别跟过去」这条功能废掉。

⚠️ **后端看这两种 stop 长得一模一样**（都是「停掉正在跑的识别」），
**猜不出来** ⇒ 必须由调用方说清楚：

| `reason` | 谁发的 | 前端怎么做 |
|---|---|---|
| `user` | 用户点了「停止」按钮 | **画面停**（视频暂停 + 字幕 rAF 停） |
| `internal` | 前端为了「拖动进度条 → 重开识别」而停 | **画面继续**（用户在拖进度条看视频，字幕还要跟着播放头走） |

落地：

* `service.stop(reason="user")` → 记下 reason → `stopped` 帧带上
  `reason`（`api.py::post_asr_stop` 用 query 参数收，默认 `user`）；
* 前端 `_followSeek` 的那次 stop 改成 `?reason=internal`；
* 前端 `stopped` 帧处理：`internal` → **不冻画面**、也不报「已停止」
  （那不是用户干的）；`user` → 冻画面（但本页面点的那次在 click 那一刻
  就冻过了 ⇒ 用 `_stopInitiated` 标记，避免 1 秒后到达的帧把用户刚按的
  播放又按掉）；
* `_updateHint()` 在 `_seekBusy`（正在重开识别）期间不说「已停止」。

> 这条是**同一个症状（画面被冻）对应两个相反需求**的典型：
> 「停掉识别」≠「停掉画面」。判据在**调用方**，不在被调方。

### 回归（补丁部分）

`scripts/verify_asr_wizard.py` 新增 **U21**（7 条断言）：

* 拖动前：视频在播、字幕在画面上（能复现用户的场景）；
* 拖到**没识别过**的位置（30 s）→ **视频没有被暂停**（这条就是用户报
  的 bug）、播放头继续前进、字幕 rAF 仍在跑、字幕跟着新位置（不是空的）、
  识别确实重开了（状态 running）、轮次继续增长。

实测（板端真浏览器）：

```
拖动前：字幕='Uh huh.'  播放头=1.41s  暂停=False  轮次=9
拖动后：字幕='but he did very well when he s'  播放头=32.83s  暂停=False  rAF=274  状态='running'
```

U20（用户点「停止」→ 画面停）**同时复跑仍全绿** —— 两条路径互不干扰。

> ⚠️ **回归脚本的用例必须互相「让出 NPU」**：后端**一次只能跑一个识别**
> （``transcribe_stream`` 撞上 running 会抛「上一条识别还在进行中」）。
> U21 结束时识别**还在跑**（拖动后重开的那一轮）→ 后面 live 用例的
> 「文件路径参考」拿不到识别（**409**），而且它取的「参考文本」是
> **上一条识别的残留** → V3/V4/V5c 一串假失败（实测踩过）。
> ⇒ 每个可能留活儿的用例在 ``finally`` 里调 ``release_npu()``。

## Qwen3-ASR 卡：「清空」按钮搬家 + 「清空」语义修正（v0.37）

### 用户诉求

> 「识别 已停止 语种 清空」那一行，请放在「停止」按钮左侧，或者放在 UI
> 更符合使用者直观感受看到的地方，以及统一方便操作到的地方。

原来的布局是**两个地方各放一个动作按钮**：

| 位置 | 控件 |
|---|---|
| 卡片头（右上角） | 「识别」标题 + 状态徽标 + 语种 + **「清空」**（mini 小按钮） |
| 卡片体底部（运行控制区） | 显示模式下拉 + **「停止」** + **「开始识别」** |

问题：「清空」离「停止 / 开始识别」很远，操作时手要来回跑；而且它是
`vd-btn-mini`（小一号），跟旁边两个常规按钮**不是一个量级**，看着不像
同一组操作。

### 改法

「清空」移到**运行控制区**，排在「停止」**左边**，并从 mini 改成常规
`vd-btn`：

```
显示模式 [实时结果]      …      [清空] [停止] [开始识别]
```

三个动作**一组**、一眼扫得到，顺序也顺（清空 → 停止 → 开始识别，主操作
在最右最显眼）。卡片头只剩「识别 / 状态 / 语种」——那是**状态**，
不是操作。

> `text_chat` 卡是同样的老布局（「清空对话」在卡头、「停止 / 发送」在
> 底部）。本次**只改 ASR 卡**（用户点名的那张）；要不要对齐，等用户
> 发话。

### 连带修掉的一个 bug：「清空」清不干净

按钮挪到显眼位置、离「停止」只隔一个键 ⇒ **识别进行中点「清空」是很
自然的手滑**。而原来的后端 `reset()` 只清结果、**不停正在跑的那一轮**
→ 那个线程继续推轮次 → 结果**又长回来**：

```
识别中：32 轮
点清空 +0.5s：3 轮      ← 清掉了
点清空 +4.5s：26 轮     ← 又长回来了（「清空」等于没点）
```

修法三处：

1. **`service.reset()` 停掉正在跑的那一轮**（`_cancel` + `session.stop()`）
   —— 否则它会继续推轮次；
2. **`service._superseded(rid)`**：用 `_current_request_id` 判「这次识别
   已经被作废」（被「清空」置空，或被新的一批顶替）→ 作废的那一轮收尾
   时**什么都不做**（不写 `last_result`、不改状态、不广播）——
   否则线程收尾会把「清空」撤销掉（文本又冒出来、状态又变「已停止」）；
3. **前端本地先清、不等服务器**：`POST /reset` 要等在飞的那次 RPC 被打断
   才返回（实测 ~1 s）→ 等它才清，用户会觉得「点了没反应」。清空后前端
   用 `_ignoreRounds` **忽略迟到的轮次帧**（清空前广播出去的那几帧）。

> ⚠️ **守卫别写成「当前没有请求」** —— 顺手写成
> ``if self._current_request_id is None: return`` 会**误伤单元测试**：
> 停滞 / 停顿检测的测试（L12 / L13）是**直接调** ``mgr._on_delta()`` 的
> （没有请求上下文）→ 帧被全丢掉 → 计数永远不涨 → L12 / L13 直接挂
> （实测踩过：live 段 46/49）。
> ⇒ 用一个**只在 ``reset()`` 时置位**的专用标志 ``_drop_frames``
> （新的一批开始时归零）。

另外：识别中点「清空」= 停止 + 清空 ⇒ **画面也要停**（视频暂停），
否则视频还在播、字幕还在滚，而识别已经被清空了 —— 那正是「点停止后
字幕还在滚」那个 bug 的翻版。

### 回归

`scripts/verify_asr_wizard.py` 新增 **U22**（7 条断言）：

* 清空前：识别在跑（有轮次）；
* 点清空后：本地轮次**立刻**清零、转写框空、单行结果空、视频暂停；
* **清空 5 秒后轮次没有长回来**（真清干净 —— 这条就是那个 bug 本身）；
* 后端状态回到 idle、轮次为 0。

实测（板端真浏览器，90 秒样本）：

```
清空前：rounds=7  state=running  paused=False  rows=4
清空 +0.6s：rounds=0  paused=True  rows=0  live=0
清空 +5.6s：rounds=0  state=idle  busy=False  rows=0  live=0
```

## Qwen3-ASR 卡：状态徽标挪到结果文本栏正上方（v0.38）

### 用户诉求

> 「"识别 识别中  语种：Chinese"这样的状态，请调整挪动到识别结果文本栏的
> 上方，"清空"按钮的左侧，具体布局请根据合理性自己调整。」

v0.37 之后，状态还挂在**卡片头右上角** —— 离结果文本框隔着大半个卡片。
用户盯着下面那段文字时，视线根本够不到右上角那个小徽标 ⇒ 「现在到底跑到
第几轮 / 用的什么语种」这件事，和它描述的内容**分离**了。

### 改法

**状态组（徽标 + 语种）从卡片头搬进运行控制行**，排在「清空」**左边**；
提示语（`.asr-hint`）挪到操作行**上方**，让操作行**紧贴**结果文本栏：

```
卡片头:  [识别]                      ← 只剩卡片名

提示语:  已停止 —— 已识别的内容保留在下方转写框里；…
操作行:  显示模式 [实时结果]   [已停止] 语种：Chinese │ [清空] [停止] [开始识别]
                                  ↑ 状态组          ↑ 竖线分隔
结果框:  ┌─────────────────────────────────────────┐
         │ 字幕文本 · 与画面字幕同步，可上拉回看      │
```

读法变成「提示 → 状态 + 操作 → 结果」：**状态描述的就是下面那块内容**。

### 为什么「识别」标题留在卡片头

它是这张卡的**名字**（框架里每张卡都有卡头标题），不是随时会变的运行
状态。徽标和语种才属于「状态」。两者混在一起时，卡头那行
「识别 识别中 语种：Chinese」读起来像三个并列的状态，其实只有一个是。

### 两个约束同时成立

上一轮定的「清空在停止左侧、三个动作成一组」和这一轮定的「状态在清空
左侧」**不冲突** —— 因为状态组插在**按钮组整体左边**，不是插在按钮
**中间**：

| 断言 | 值 |
|---|---|
| `badgeLeftOfReset` | True |
| `langLeftOfReset` | True |
| `badgeAboveLive` | True |
| `ctrlTouchesLive` | True（间距 ≤ 20px） |
| `resetNextToStop` | True |
| `stopNextToStart` | True |
| `headHasBadge` / `headHasLang` | False |
| `jsErrors` | `[]` |

### 实现要点

* **JS 零改动** —— 前端一直按 **id** 取元素（`#asr-state-badge` /
  `#asr-language` / `#asr-reset-btn`），位置无关 ⇒ 搬 DOM 不影响逻辑，
  回归脚本那几处 `getElementById` 也不用动。
* **CSS 新增 `.asr-status-group`**（`inline-flex` + `border-right`）：
  状态组内部两条元素挨在一起，跟右边的按钮组用一条竖线分开 ——
  免得「识别中」看着像是在给「清空」按钮做说明。
* **不用重启服务**：`html_assembly.assemble_index_html()` **每次请求**都
  重读 `template.html`，卡片 CSS 走 `?v=<mtime>` 版本戳 ⇒ 改前端即时生效。
  （改**后端**才要 `systemctl restart wdnwebui.service`。）

## Qwen3-ASR 卡：删掉「识别」卡头（v0.39）

### 用户诉求

> 「把"输入来源"顶上的"识别"那栏 UI 布局给去掉，因为单独显示比较突兀。」

v0.38 之后，卡头只剩「识别」两个字（状态徽标和语种已经挪到结果文本栏
上方了）。一条**只有标题**的灰条夹在「输入来源」上方，确实突兀：

* 页面头部已经有「流式语音识别」这个卡名，卡内再挂一条同名标题 = 重复；
* 它把用户第一眼该看的东西（**选来源**）往下挤；
* 空标题条只剩装饰作用。

### 改法

**删掉 `.imb-card-head` 整块**（模板里不再有那个 div），并把**外层卡片
的框削平**：

```css
#page-qwen3-asr .imb-card {
  background: transparent;
  border: 0;
  box-shadow: none;
}
#page-qwen3-asr .imb-card-body {
  padding: 0;   /* 外层不画框了 ⇒ 别再缩 16px */
}
```

**为什么必须连带削平外框**：卡头是外层 `.imb-card` 的**上边框 + 灰底**。
删掉卡头而留着外框 ⇒ 外层那圈边框就只剩「包住」输入来源那个框 ⇒
**框套框**，比原来那栏还难看。

削平后，框由**里面的块自己带**：

```
┌─ 输入来源块（自带边框+圆角+阴影）────────┐
└──────────────────────────────────────────┘
提示语
显示模式[▾]        [已停止] 语种：English │ [清空][停止][开始识别]
┌─ 结果框（自带边框+圆角+阴影）──────────┐
```

`padding: 0` 是配套的：外层不画框了还缩 16px，里面的块会比页面标题
（「流式语音识别」）往里缩一截，看着没对齐。削平后实测
**`.asr-block` 与 `.page-header h2` 左边缘都是 x=104**，齐平。

### 影响面

* **JS 零改动** —— 前端只按 id 取元素。
* **`.imb-card` 的高度覆盖保持不变**（`height: auto` +
  `min-height: calc(100vh - 220px)`，见 app.css 既有注释）—— 那是修
  「逐轮明细压住完整 JSON」（W8）的，跟卡头无关。
* 只影响 `#page-qwen3-asr` 作用域，**framework.css 一行没动**
  （`.imb-card` / `.imb-card-head` 是其它卡共用的）。

## Qwen3-ASR 卡：向导「音频源」步 —— 硬件能力 + 视频源式预览（v0.40）

### 用户诉求（三条）

1. 「流式语音识别快速配置」里，**硬件能力**要有 `✓ hwcap_video_input`、
   `✓ hwcap_audio_input`、`✓ hwcap_audio_output` 这三个；
2. 「音频源」步增加**类似「视频实时深度估计」卡「视频源」步**的逻辑，
   但**不要** gscore 那部分 UI 与逻辑；
3. **音频文件**或**含音轨的视频文件**都要能在**向导界面**里预览播放。

### 1. 硬件能力：只改 manifest

`manifest.json` 的 `hardware_capabilities` 加三个 → 框架 env 步的
「硬件能力」卡按 `app.hardware_capabilities` **逐行列出**
（`state.js::_renderEnvStep`）。`hwMissing` 恒为空 ⇒ 每行都带 `✓`。

> ⚠️ 这是**声明式**的：框架不强求板子真有这些硬件（`state.js` 注释：
> 「严格 HW 探测留给具体应用在 applyWizardSnapshot 内 assert」）。
> 与 `video_detection` 卡声明的是同一组。

### 2. 「音频源」改走**源字段**路径

框架判定源字段：`state.js::_isSourceFieldId(id)` = `/-sources?$/`
⇒ **字段 id 必须以 `-source` / `-sources` 结尾**。
⇒ wizard.json 的字段 id 从 `asr-audio` 改成 `asr-source`。

两条路径**互斥**（`state.js::_renderFormStep`）：

| 路径 | 触发条件 | 控件 |
|---|---|---|
| 通用文件字段 | `field.browse \|\| field.upload` | 浏览… + 上传… |
| **源字段** | id 匹配 `-source(s)` | 浏览… + **预览播放 / 关闭** + **拖放上传区** + **画面搬进向导** |

⇒ 音频源步现在跟视频源步同款。

#### ⚠️ 源字段 id 必须与**应用页元素同名**

框架点「预览播放」时执行：

```js
const srcEl = document.getElementById(field.id);   // ← field.id
if (srcEl) srcEl.value = el.value || "";
```

向导自己的 input **没有 `id`**（只有 `data-field-id`）⇒ 这个 id 命中的是
**应用页**的元素。⇒ 应用页的 `#asr-path` 改名成 `#asr-source`
（app.js 5 处 + 回归脚本 21 处）。

> 同一条链路还藏着第二个坑：框架写值**不派发 `input` 事件** ⇒ 卡片的
> 内部状态（`_path`）还是旧的。向导里选了 A、预览了 A、跳过向导 ⇒
> 页面输入框显示 A、内部却是旧值 B ⇒ 点「开始识别」识别的是 B。
> ⇒ `_previewOpen()` 里补一次状态同步。

### 3. 预览播放：独立播放器 + 收尾守卫

wizard.json 声明 `open_source_button: "asr-open"`、
`canvas_id: "asr-preview-wrap"` ⇒ 框架把该元素**搬进向导面板**
（点「预览播放」时搬、`unmount()` 时按原位搬回）。

应用页给一个**独立的**预览播放器（`<audio>` + `<video>`，按后缀切换）：

> ⚠️ **不复用页面的 `#asr-video`** —— 那个绑着字幕渲染循环（字幕条
> `#asr-video-sub` 是它的**兄弟节点**），把它搬进向导会留下一个没有
> 字幕条的孤儿视频，而页面自己也没画面了。

> ⚠️ **搬回页面 ≠ 停了** —— `<video>` / `<audio>` 还在播，声音会在后台
> 继续放（卡片页那层 holder 是 hidden 的 ⇒ 用户只看到页面、听到声音）。
> ⇒ 两个收尾点都停：**向导卸载**（框架 dispatch `wizard:unmounted`）
> + **离开这张卡**（hashchange）。

媒体源用**同一个端点** `/api/qwen3-asr/media?path=`（Range 支持），
只放行 `<assets>/uploads` / `<assets>/tts` 两个根 ⇒ 预览的文件必须在
这两个目录里（回归脚本会先复制一份进去）。

### 4. 框架侧新增三个可配项（向后兼容，默认行为不变）

| 配置项 | 默认值 | 用途 |
|---|---|---|
| `cfg.open_source_label` | 「播放视频」 | 源字段的预览按钮文案 |
| `cfg.drop_hint` | 「或点击「浏览」选择本地视频」 | 拖放区提示 |
| `cfg.drop_icon` | 「📥」 | 拖放区图标（**HTML 片段**） |

> ⚠️ **`drop_icon` 传空串 = 不要图标** —— 板端**没有 emoji 字体**，
> 「📥」实测渲染成**豆腐块 □**（AGENTS.md 有记）。ASR 传的是**内联
> SVG**（波形图标），不是 emoji。

### 回归

`scripts/verify_asr_wizard.py` 新增 **U23**（约 30 条断言）：

* env 步三条硬件能力**逐条列出且带 ✓**；
* 音频源步有「预览播放 / 关闭 / 拖放上传区」、拖放区文案说音频或视频、
  图标是 SVG 不是 emoji、**没有** gscore / 判帧 / 阈值 / 帧间隔字段；
* 预览**音频**：`<audio>` 被搬进向导面板、走 `/media` 端点、**播放头
  在走**、无解码错误；
* 预览**视频**：`<video>` 同上；
* 「关闭」后预览器**搬回卡片页**、**停住了**、源被清掉。

⚠️ **预览素材必须在媒体端点放行的目录里** —— 回归脚本把参考素材复制到
`<assets>/uploads/` 再用（那个端点没有鉴权，只放行 `uploads` / `tts`
两个根），用完删掉。

### ⚠️ 踩坑：「藏起来」≠「停了」—— `display:none` 的媒体照样播

用户实测报的 bug：**先预览视频、再预览音频 ⇒ 上一次的视频还在放声音**，
两个叠着放。

```
预览视频后   video{hidden:false, paused:false, t:1.49}
再预览音频后 video{hidden:true,  paused:false, t:3.11}   ← 藏了，但还在播！
再预览音频后 video{hidden:true,  paused:false, t:4.33}   ← 播放头照走
```

**根因**：``<video>`` / ``<audio>`` 的**播放与元素可见性无关** ——
``display:none`` 只是不渲染，浏览器**不会**因此暂停它。原来切换预览源时
只做了 ``other.classList.add("hidden")``（藏起来），没停。

⇒ 换源预览必须**藏 + 停**两件事都做（`_stopPreviewEl()`：
``pause()`` + ``removeAttribute("src")`` + ``load()`` + 加 hidden）。

> 同一类坑还有：向导关闭时元素被**搬回**卡片页 —— **搬回去也不等于停了**
> （卡片页那层 holder 是 hidden 的 ⇒ 用户只看到页面、听到声音）。
> ⇒ 向导卸载（`wizard:unmounted`）+ 离开卡片（hashchange）两个收尾点
> 都要停。

> 顺带：同一个源重复点「预览播放」**不要**重头再来（会打断正在播的）
> ⇒ 只有换了 src 才 `load()`；`ended` 了才归零重播。

> ⚠️ **实测发现（设备级）**：ASR 模型在 **npu 0** 上 `init_runtime` 会失败
> （`wdnai/backends/asr.py` 的 `audio init_runtime failed`，RKNN 里等约
> **3 分钟**才报），而同样的模型在 npu 1 / npu 2 上正常。
> 本卡向导的 `swcap_to_model.swcap_audio_asr.npu_index` 写的是 **[0]** ⇒
> 环境步会显示 ⚠（能力可用但配置没配齐）而不是 ✓。
> ⇒ **别把这个 ⚠ 当成 bug**：它是「模型实际跑在别的卡上」的真实反映。
> （同日早些时候 ASR 在 npu 0 上是能跑的 ⇒ 更像是 NPU 核心被反复
> 杀 worker 搞脏了，**重启板子**能恢复。）

## Qwen3-ASR 卡：向导选视频 ⇒ 功能页必须切成视频来源（v0.41）

### 用户诉求

「流式语音识别快速配置」向导里，**音频源**选的是含音轨的视频；点「完成
配置」进功能页 ⇒ **页签还是「音频文件」**，**视频也不播**。

### 根因：向导里**没有**「输入来源」页签

「输入来源」页签（音频文件 / 含音轨的视频 / 板载麦 / 浏览器麦）是**功能页**
的东西。向导只有「音频源」一个文本字段 ⇒ **没有任何 UI 能表达「这是个视频」**。

`applyWizardSnapshot` 回填路径后只调了 `_syncSourceUi()`（刷新按钮可用性），
**从没设置过 `_source`** ⇒ 三个后果：

| 位置 | 判据 | 结果 |
|---|---|---|
| 页签 | `_switchSource()` 从没被调 | 停在默认的「音频文件」 |
| 视频播放器 | `_syncVideoPlayer()`：`this._source === "video"` 才显示 | 不显示 |
| 视频播放 | `_playVideoIfAny()`：同上 | 不播 |

而后端 `api.py::_source_kind`（按**后缀**判）返回的是 `"video"` ⇒
**前后端口径不一致**（前端按音频、后端按视频记轮次归属）。

### 修法：按路径后缀推断来源类型

新增两个 helper（`app.js`）：

* `_kindFromPath(path)` —— 按**后缀**判 `"video"` / `"file"`，与后端
  `_source_kind` **同口径**（同一份 `VIDEO_EXTS` 扩展名表）；
* `_applyKindFromPath(path)` —— 推断结果与当前页签**不同**时才
  `_switchSource(kind)`（同类型重复切会触发 `_onSourceChanged()` 清字幕，
  打字过程中反复触发会把正在看的字幕清掉）。

调用点三处：

1. `applyWizardSnapshot`（**向导完成** —— 本次 bug 的正主）；
2. `#asr-source` 的 `input` 处理器（手填路径 / 「浏览…」弹窗写回 ——
   `FSModal._commit` 会补发标准 `input` 事件，所以这条覆盖手填 + 浏览）；
3. `_previewOpen()`（向导内「预览播放」）—— 因为向导的 `skip()`
   **不调** `applyWizardSnapshot` ⇒ 走「跳过向导」那条路时只有这里能
   把页签纠正过来。

### 回归

`scripts/verify_asr_wizard.py` 新增 **U24**（9 条断言）：向导选 mp4 →
「完成配置」⇒ 页签是 `video`、卡片内部 `_source` 是 `video`、批次归属
`roundsSource` 是 `video`、播放器容器**可见**、src 指向 `/media` 端点、
**视频真的在播**（播放头在走）、无解码错误、有实际尺寸、无 JS 报错。

### ⚠️ 踩坑：页签会**恢复**，所以「默认」那条断言必须验在干净状态上

页面会按服务端的 `rounds_source.kind` **恢复**上一批次的来源
（`_restoreFromServer` → `_switchSource(kind)`，这是有意的，U19 有断言）。
⇒ 上一段（W7 视频向导）留下的 video 轮次，会让后面「默认选中『音频文件』」
的断言量到的是「恢复」而不是「默认」。

⇒ `verify_sources` 现在**先 `POST /api/qwen3-asr/reset` + 刷新页面**再验
U1。

### ⚠️ 环境级：NPU 驱动超时会让长音频识别**提前结束**（不是代码 bug）

实测偶发：`verify_asr_wizard.py` 的 U7（长音频不被 60 s 截断）红，
`audio_sec=48.0`（应该是 90）。

服务日志里同一时刻有：

```
[ERROR] [nsmi_pcie.c:644] Receive timeout, command: 4
[ERROR] [rk_nsmi.c:727] rknnsmi receive failed, ret=-7
[INFO] qwen3_asr: asr stream epoch rotated: rid=52 epoch=1 reason=silence audio_sec=48.0
```

⇒ NPU 通信超时 → 模型输出不再变化 → **模型侧停顿检测**命中 →
在 48 s 处切会话（`reason=silence`）→ 新会话建不起来 → 这一轮只交回
前 48 s 的结果。

**这不是前端/后端逻辑问题**：同一个请求单独调 API 能跑满 90 s
（`audio_sec=90.0`、90 轮、996 字）。⇒ 遇到 U7 偶发红，先看服务日志里
有没有 `rknnsmi receive failed`，有就是**板子的 NPU 状态**问题
（重启板子 / 重启服务能清），不要去改代码。

### ⚠️ 回归素材：**视频轨必须用 H.264**，不能用 MPEG-4 Part 2

`scripts/make_asr_ref_video.py` 早期用 `out.add_stream("mpeg4", ...)` ——
那是 **MPEG-4 Part 2**，**Chromium 早就不支持了**（Chrome 只解
H.264 / VP8 / VP9 / AV1）。

症状**极其隐蔽**：音频轨正常（AAC 解得出来、时长也对、识别和字幕全都
正常），但**视频轨解不出来** ⇒ 浏览器里 `videoWidth === 0`
（而 `readyState` 却是 4）⇒ 「设备端视频能真正解码出画面」那条断言红。

⇒ 看起来像产品 bug（用户会以为「视频播不了」），其实是**素材编码格式
不对**。已改成 `libx264`（板端 Chromium 能解 H.264 —— 用户素材
`rk-0.mp4` 就是 H.264，实测 704×576 解得好好的）。

自检（浏览器里量 `videoWidth`）：

```
asr_real_audio.mp4（自造·旧）: FAIL {'w': 0, 'h': 0, 'dur': 15.051, 'ready': 4}
asr_long_90s.mp4（自造·旧）  : FAIL {'w': 0, 'h': 0, 'dur': 90,   'ready': 4}
rk-0.mp4（用户素材）          : OK   {'w': 704, 'h': 576, 'dur': 19, 'ready': 4}
```

### ⚠️ 回归素材：uploads 里的**测试草稿**会插队当样本

`_pick_sample_mp4()` 取的是「uploads 里最小的 mp4」。而各个用例会往
uploads 复制 `_asr_*.mp4` 草稿、用完删 —— **板子重启 / 用例崩了就留下
残骸**，残骸往往更小 ⇒ **插队成为样本** ⇒ U12 测的其实是另一个文件。

⇒ `_pick_sample_mp4()` 现在**排除 `_` / `.` 开头的文件**（只认用户素材）。

## 「切到向导模式」必须先停掉在跑的识别（v0.42）

### 用户诉求

「流式语音识别」**从功能界面正在识别**的时候，切到向导模式，或者返回首页，
或者切到其它界面，识别应该停止才合理 —— 要不推理一直会占着 NPU。

### 三个「离开」的路径，各走各的机制

| 场景 | 机制 | 改之前 |
|---|---|---|
| 点「返回首页」 | 框架 `injectBackHomeButtons` → `DemoGuard.stopAndClear` | ✓ 本来就行 |
| 切到其它卡 / 浏览器前进后退 | `onHashChange` → `DemoGuard.stopSync`（sendBeacon） | ✓ 本来就行 |
| **点「向导模式」** | `wireWizardModeButtons` → `enterWizardMode()` | ❌ **完全没停** |

「向导模式」按钮（`<button class="wizard-mode-btn" data-wizard-mode
data-page-id="<id>">`）的处理函数只调 `window.enterWizardMode(appId)` ——
**一行停止逻辑都没有** ⇒ 识别继续跑。

实测（`/tmp/asr_ref/guard_probe.py`）：点「向导模式」后后端仍
`busy=True state='running'`，且页面**根本没发 stop 请求**。

### 两条理由（缺一不可）

1. **算力**：识别是长任务（整文件快转，几十秒起步），不停就一直占着 NPU
   —— 而用户已经离开这张卡了，没人看结果。
2. **功能**：向导走完会 `applyWizardSnapshot` → **直接发起新的识别**；而后端
   「一次只能跑一个识别」（RPC 只有一条 delta 通道，`service.py::
   _ASR_RPC_LOCK`）⇒ 上一条还在跑的话，新请求直接 **409「上一条识别还在
   进行中」** ⇒ 向导显示完成、识别却没跑起来。

### 改法（框架级，只动「向导模式」按钮这一处）

`app/static/core/framework.js` 的 `wireWizardModeButtons`：

```js
      // 只在**真的在跑**时才停（DemoGuard.isActive）⇒ 空闲的卡点这个
      // 按钮行为零变化。stopAndClear 走 card manifest 声明的
      // stop_endpoints（ASR = POST /api/qwen3-asr/stop）。
      if (DemoGuard.isActive(appId)) {
        try { await DemoGuard.stopAndClear(appId); } catch (_) {}
      }
      if (typeof window.enterWizardMode === "function") {
        window.enterWizardMode(appId);
      }
```

* **只在 `isActive` 时才停** ⇒ 空闲卡行为零变化；
* 走 card manifest 的 `stop_endpoints` ⇒ **不给 ASR 硬编码端点**，其它卡
  自动生效（text-chat / tts / video-* 都有各自的 stop 端点）；
* `service.stop()` 是**非阻塞**的（立刻置 `stopped` + 广播，不等后台线程
  收尾）⇒ `await` 不会卡住界面。

后端 stop 会广播 `stopped` 帧（带 `reason`）⇒ 前端据此 `_freezePlayback()`
⇒ 画面也停（`reason=user`），不会「后台继续放声音」。

### 回归

`scripts/verify_asr_wizard.py` 新增 **U25**（4 条断言）：A 点「向导模式」/
B 点「返回首页」/ C 切到其它卡 ⇒ 三种离开方式识别都停；外加「页面确实
发了 stop 请求」。

## 服务重启卡死 90 秒 → SIGKILL 整组（v0.43）

### 症状

`sudo systemctl restart wdnwebui` 要等**整整 90 秒**，日志：

```
State 'stop-sigterm' timed out. Killing.
Main process exited, code=killed, status=9/KILL
Failed with result 'timeout'.
```

⇒ systemd **强杀整个 cgroup** ⇒ `compute_worker` **连坐**被杀（AGENTS.md
开头就警告过这条）。worker 不是自己退的，是被 SIGKILL 拖走的。

### 根因链（三层，一层一层剥）

**① 谁卡住了 uvicorn**

uvicorn 停机顺序：先 `connection.shutdown()` 关掉 WS transport → 再等
`server_state.tasks` 清空 → 最后才发 lifespan shutdown。日志里那行
`Waiting for background tasks to complete.` 就是它 —— **挂住的是卡在
`await` 上的 WS handler**。

**② WS handler 为什么卡**

`app/system/api/system_logs.py` 的 `_drain()` 阻塞在
`await stream.readline()`（等 `journalctl -f` 出声）。**服务端主动关闭
transport 时，ASGI 层收不到 `websocket.disconnect`** —— 那个事件只在
**客户端**断开时才有 ⇒ handler 根本不知道 socket 没了 ⇒ 永远不退出。

> 同类问题在插件卡 `audio_analysis`
> （`.venv/src/wdnapps-video-analyzer/.../audio_analysis/api.py:123`，
> 卡在 `await queue.get()`）—— **外部仓库**，本仓改不了，只能靠 uvicorn
> 的优雅超时兜底。

**③ uvicorn 的超时上限没生效**

`--timeout-graceful-shutdown` 会给 `_wait_tasks_to_complete()` 加上限，
超时后 cancel 所有 task（日志 `Cancel N running task(s), timeout graceful
shutdown exceeded`）。**原 ExecStart 裸跑 uvicorn ⇒ 没有这个上限 ⇒
无限等 ⇒ 只能等 systemd 的 `TimeoutStopSec=90s` 把整组 SIGKILL 掉。**

### 修法

**单元（`install_autostart.sh` 是单元文件的仓库源头 —— 改它，别只改板子上的 `/etc`）**

```
ExecStart=... uvicorn app.main:app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 8
TimeoutStopSec=25
```

> ⚠️ `TimeoutStopSec` 必须 **>** graceful timeout，否则 systemd 会在
> uvicorn 还没收尾完时就 SIGKILL —— 又回到老问题。

**代码（`app/system/api/system_logs.py`）**

1. `_drain()` 改成「读一行」与「等客户端断开」**竞速**
   （`asyncio.wait({read, disconnect}, FIRST_COMPLETED)`）⇒ 客户端一断
   就收尾；
2. `finally` 里 **`except asyncio.CancelledError` 也要 kill journalctl**
   —— CancelledError 继承自 **BaseException**，`except Exception` 捕不到；
   而 shutdown 恰恰是 cancel 触发的 ⇒ 漏这一句就留下**孤儿
   `journalctl -f`**（它会继续占着 cgroup，下次 stop 又超时）；
3. `_ping` 间隔 **15s → 3s**：服务端主动关闭时 ASGI 层收不到 disconnect，
   **「发不出去」是唯一的信号** ⇒ 心跳短一点，发现得早一点。

### 效果（实测，板子）

| | 改之前 | 改之后 |
|---|---|---|
| `systemctl stop` | 90s（超时） | **8.4s** |
| 主进程退出方式 | `status=9/KILL`（被强杀） | 正常退出 |
| `compute_worker` 退出码 | 连坐被杀 | **rc=0**（自己优雅退） |
| 孤儿 `journalctl -f` | 有（能挂 6 个月） | 无 |

### ⚠️ 排查这类问题的通用手法

* `Waiting for background tasks to complete.` ⇒ 就是它；
* 有 `Cancel N running task(s), timeout graceful shutdown exceeded` ⇒
  优雅超时**生效了**（没这行 = 参数没传到进程）；
* 紧跟着的 `Exception in ASGI application` + traceback ⇒ 那是**被 cancel
  的 handler 的收尾**抛出来的，不是新 bug（但会刷屏）；
* **`systemctl restart` 比 `stop` + `start` 慢**：restart 的停止阶段还要
  等 cgroup 里 worker 排空（worker 卸载 RKNN 模型要 ~6s）⇒ 别拿
  restart 的耗时当停止耗时，**分开测**（`stop` 8.4s / `start` 0.2s，
  restart 却 15.4s）。

### 附带：流式 ASR 冒烟脚本

`scripts/verify_asr_live_smoke.py`（新）—— 活体链路几十秒冒烟：
`live/start` → 37×500ms `live/chunk` → `live/finish`，8 条断言。
全量回归仍是 `scripts/verify_asr_live.py`（49 条）。

> ⚠️ 两个判据容易写错：
> * `round` 是**本段内**已凑满 1 秒的推理轮次。500ms 一块 ⇒ 同一轮的
>   两块共享同一个 round 号（`0,0,1,1,2,2…`）⇒ 判据是「**段内**单调
>   不减」，不是严格递增；
> * VAD 静音断句（`silence_ms=1200`）后**开新的一段**，round 归零重数
>   —— 设计行为，不是异常。
>
> ⚠️ 素材在 `/tmp`（tmpfs），板子重启就没了 ⇒ 缺素材时脚本**报错退出**，
> 不静默 SKIP。

## WebSocket handler **必须能感知断开**（v0.43 附则，跨卡通用）

### 为什么单独立一节

v0.43 修「重启卡死 90 秒」时挖出一条**跨卡通用**的坑：WebSocket handler
只要「只写不读」或「读不到断开」，就会**永久占住一个 ASGI task**。

后果有两条，都很隐蔽：

1. **泄漏** —— 客户端每次断开都漏一个 handler（连同它持有的队列 / 线程）；
2. **关停卡死** —— `uvicorn` 的 `Server.shutdown()` 等
   `server_state.tasks` 清空 ⇒ 永远等不到 ⇒ systemd 只能 SIGKILL 整个
   cgroup ⇒ **compute_worker 连坐被杀**。

### 根因：uvicorn **关停时不会**给「只写」的协程投递 disconnect

`websocket.disconnect` 这个 ASGI 事件只在**客户端断开**时才有。
**服务端主动关闭**（`connection.shutdown()` → `transport.close()`）时，
只写不读的协程**永远看不到任何信号** ⇒ 它会一直等下去。

⇒ 于是有两种死法：

* **只写不读**（`while True: await queue.get()`）：客户端断开也不报错 ⇒
  纯泄漏；
* **写了但状态不变就不写**（`if changed: await ws.send_json(...)`）：
  客户端断开后没有新事件 ⇒ `send` 永远不被调用 ⇒ 也发现不了死连接。

### 四条规则

1. **必须有「只读」协程**：`await ws.receive_text()` / `receive_json()`
   循环 —— 客户端断开时它抛 `WebSocketDisconnect`。
2. **推送协程与只读协程赛跑**：
   `await asyncio.wait({push, watch}, return_when=asyncio.FIRST_COMPLETED)`
   —— 谁先结束都收尾，**finally 里 cancel 掉另一个**。
3. **`send` 必须加超时**：`await asyncio.wait_for(ws.send_json(p),
   timeout=5.0)` —— 对端「消失但不干净关闭」时 socket 停在 CLOSE-WAIT
   且写缓冲排不空 ⇒ `send` **永远不返回** ⇒ 协程卡死。
4. **要有心跳**：即使状态没变也定期推一次 —— 否则「死连接但没关闭」
   永远等不到一次 `send` ⇒ 也永远发现不了它死了。

> ⚠️ `except WebSocketDisconnect:` 写在 `await queue.get()` 外面是
> **死代码** —— `queue.get()` 永远不会抛它。这种 handler 看起来「有断开
> 处理」，实则一次都没触发过。

### 本仓 6 个 + 插件仓 6 个 WS 端点的现状

| 端点 | 写法 | 断开感知 |
|---|---|---|
| `system-logs/ws`（本仓） | `readline` × `ws.receive()` 竞速 + 3s 心跳 + send 超时 | ✅ v0.43 修 |
| `qwen3-asr/ws`（本仓） | reader task（`receive_text`）→ finally 取消 relay | ✅ |
| `text_chat/ws` / `im_bridge/ws`（本仓） | 同上，同构 | ✅ |
| `core/video/multi_api.py`（本仓） | 50ms 轮询，天然能感知 | ✅ |
| `coprocessor/ws`（本仓） | 一次性任务，跑完即止 | ✅ |
| `image_editor/ws`（media-generator） | FIRST_COMPLETED 竞速 + 10s 心跳 + send 超时 | ✅ 2026-11-04 已修 |
| `media_generator/ws`（media-generator） | 同上 | ✅ |
| `video_retrieval/ws`（video-analyzer） | `wait_for(queue.get(), timeout=1.0)` 轮询 | ✅ |
| `video_retrieval/play`（video-analyzer） | 双向 + `receive_json` 超时 | ✅ |
| `audio_analysis/ws`（video-analyzer） | 纯 `queue.get()` + 死代码 except | ❌ **v0.43 已修** |
| `tts/ws`（audio-chat） | `audio_q.get()` + 主循环空转 | ❌ **v0.43 已修** |

> **同仓的 `image_editor` 早就踩过这个坑并留下了模板**（2026-11-04，
> 注释里写得很清楚）—— 加新卡时**直接抄它**，别自己发明写法。

### 排查手法

* 停机日志 `Cancel N running task(s)` 的 **N 越大 ⇒ 泄漏越多**（N 里也含
  正在进行的 HTTP 请求 task，所以不是纯 WS 数，但趋势有意义）；
* 对照实验：挂一个 WS → 停机记 N₁；断开后再停机记 N₂。**N₂ < N₁ ⇒
  有泄漏**（客户端断开没让服务端收尾）；
* 想直接看某个 WS 有没有泄漏：连上 → 断开 → 看服务端的
  `journalctl -f` / 队列 / 线程有没有跟着消失。

### ⚠️ 本板子进程会**继承 uvicorn 的 fd**（SDK 没设 FD_CLOEXEC）

实测：`journalctl -f` 子进程的 fd 里有 `/dev/rga`、`/tmp/rknnsmi.lock`、
`/dev/shm/fastdds_*` ⇒ **FastDDS / RGA / RKNNS 打开的 fd 都没设
`FD_CLOEXEC`** ⇒ fork 时被子进程继承。

⇒ 一个**没退出的**日志跟随进程，会一直占着 RGA 设备 fd 和 DDS 的共享内存
（它卡在 `sock_alloc_send_pskb` 时尤其明显：stdout 管道写缓冲排不空，
进程既不读也不退）。

> ⚠️ **别用 `preexec_fn` 解决** —— 本服务跑在 **uvloop** 上，`preexec_fn`
> 会让 uvloop 的 `UVProcess._after_fork` 抛
> `OSError: [Errno 9] Bad file descriptor` ⇒ **子进程根本起不来**
> （journalctl 立刻死、WS 连接被异常关闭、`ConnectionClosedError`）。
> 实测踩过，别再试。
>
> ⇒ 正确的解法在 **SDK 侧**（打开 fd 时设 `FD_CLOEXEC`），或**接受现状但
> 确保日志进程及时死**（见下）。

### `journalctl -f` 可能卡在写缓冲 ⇒ 收尾要 `kill` 兜底

stdout 管道没人读时它会阻塞在写上 ⇒ **收到 SIGTERM 后还要好几秒才退出**
⇒ 收尾时 **terminate 后只给 0.5s 宽限，超时直接 `kill()`**（日志跟随进程
不需要优雅退出）。宽限给到 2s 时，实测能看到「客户端已断开、journalctl
还挂着」的窗口。

⇒ 收尾再打一行 `system-logs ws closed` 日志 —— 之前日志跟随进程的收尾
**完全静默**，泄漏了也看不出来（两个插件仓的 WS 也各加了一行同款日志）。

### 已知限制：偶发的「日志跟随进程残留」

修复**之前**实测中见过 `journalctl -f` **残留**：客户端断开后它还活了
**1~2 分钟**才自己退出（服务端 TCP 连接早已消失，`established` 为 0）。
**成因未复现**。

修复**之后**用 `scripts/verify_ws_disconnect.py` 回归过 **4 端点 × 3 种
断开方式 = 12 种组合，全部通过**（含上面那次没复现的场景）⇒ 结论：
残留来自**修复前的代码**（旧进程），现已消失。

> ⚠️ 别把它和「在线客户端」混淆：`journalctl` 存活 ⇏ 有活跃 WS 连接 ——
> 它的 stdout 是**管道**不是 TCP。判据要看**服务端的关闭日志**
> （`system-logs ws closed`）有没有对应上。
>
> 另外注意：直连 127.0.0.1 与**经 TLS relay(8443)** 的两条路径都验证过
> 能及时收尾 ⇒ relay 不是元凶。

已验证能正常收尾的路径（`b0d70e9` 之后的代码）：

| 断开方式 | 结果 |
|---|---|
| 客户端正常关闭 | journalctl **≤1s** 归零 |
| 客户端进程被 **SIGKILL**（模拟浏览器被杀） | **≤2s** 归零 |
| 服务端停机（`--timeout-graceful-shutdown` 到点 cancel） | task 被 cancel ⇒ `finally` 里 kill 兜底 |

⇒ 推测是「客户端已消失、但内核没把 FIN/RST 送到服务端」这种 TCP 半死
场景：此时 `send` 仍会成功（内核照收），心跳察觉不到，服务端**在应用层
无从得知**对端已走。

⇒ 若要根治，方向是**在应用层之外查 TCP 状态**（如按
`scope["server"]` 的端口去读 `/proc/net/tcp` 看 socket 还在不在
ESTABLISHED），或**依赖内核 keepalive**（默认 2 小时，太慢）。
本轮**未实现** —— 收益有限、风险（/proc 解析、IPv6、netns）不低。

⇒ 危害有限：残留的只是一个 `journalctl`（它**持有** `/dev/rga`、
`/tmp/rknnsmi.lock`、FastDDS 共享内存的 **fd**，但**不持有文件锁** ——
锁随进程退出释放），不影响服务正确性，只是让那些 fd 多活一会儿。

### 回归：`scripts/verify_ws_disconnect.py`（12 条）

对 **4 个端点**（`system-logs` / `audio_analysis` / `tts` / `qwen3-asr`）
× **3 种断开方式**（正常关闭 / 客户端进程被 **SIGKILL** / 经 **TLS
relay(8443)**）断言「服务端的关闭标志出现了」。

⇒ 关停链路的修复**跨三个仓**（wdnwebui / wdnapps-video-analyzer /
wdnapps-audio-chat）⇒ 光靠单仓回归测不出来 ⇒ 这个脚本是唯一的
「一处跑、三个仓一起验」的守门人。

⇒ 每个端点都要有一行**关闭日志**（`system-logs ws closed` /
`audio_analysis ws closed` / `ws[..] closed` / `qwen3_asr ws closed`）——
没有它就没法判定 handler 有没有走到 `finally`。⇒ **加 WS 端点时顺手加
这行日志**，否则回归脚本覆盖不到。

### ⚠️ 向导类回归失败的第一嫌疑：`compute.autostart` 被清空了

症状（看着像脚本 bug，其实是环境没准备好）：

```
playwright._impl._errors.TimeoutError: Page.click: Timeout 30000ms exceeded.
  - waiting for locator("#wd-wizard-root-qwen3-asr #wd-wizard-next-btn")
  -     - locator resolved to <button disabled ...>下一步</button>
```

⇒ **「下一步」按钮 disabled** ⇒ 向导的**环境步**没满足 ⇒ 软件能力没就绪
⇒ **ASR 实例没在跑**。

根因链：`DELETE /api/compute/active-app`（**前端离开卡片时就会发**）会
**连带清空** `compute.autostart`（`clear_active_app`）⇒ 板子重启后
**没有任何模型自启** ⇒ 跑回归时实例不在。

> ✅ **这是产品语义，不是 bug**：「离开那张卡 = 取消那张卡的开机自启」。
> 回归脚本踩到的只是它的**副作用** —— 板子恰好处于「用户已离开所有卡」
> 的状态，于是没有模型可跑。

⇒ 跑回归**前**先确认实例在跑：

```bash
curl -s http://127.0.0.1:8000/api/compute | grep -o '"status": "[a-z]*"'
```

没有实例就恢复 `config.jsonc` 的 `compute.autostart`（设备级配置，不进
git）再重启：

```jsonc
"compute": {
  "autostart": [ { "model": "qwen3-asr", "npu_index": 0 } ],
  "card_count": null,
}
```

> ⚠️ 这是**产品语义**（「离开那张卡 = 取消那张卡的开机自启」），**不是
> bug** —— 只是回归脚本会踩到它的副作用：板子恰好停在「用户已离开所有卡」
> 的状态 ⇒ 没有模型自启 ⇒ 回归失败。⇒ 回归前把 `autostart` 恢复好即可
> （设备级配置，不进 git）。

## Qwen3-ASR 卡迁到 `wdnapps_audio_chat` 插件包（v0.44）

「流式语音识别」卡（`qwen3-asr`）从宿主 `app/apps/qwen3_asr/` 迁到插件包
`.venv/src/wdnapps-audio-chat/src/wdnapps_audio_chat/cards/qwen3_asr/` ——
与 `tts` 卡同仓。宿主 `app/apps/` 现在只剩 `text_chat` 一张内置卡。

> 迁移动机与 `tts`（commit `95fce52`）完全一致：**卡片是可独立分发的
> 功能单元**，音频类两张卡（生成 + 识别）同仓 ⇒ 装一个包得两张卡。

### 改了什么

| 位置 | 改动 |
|---|---|
| `cards/qwen3_asr/manifest.json` | `backend.module`：`app.apps.qwen3_asr` → `wdnapps_audio_chat.cards.qwen3_asr` |
| `cards/qwen3_asr/__init__.py` | `from app.apps.qwen3_asr.api` → `from wdnapps_audio_chat.cards.qwen3_asr.api` |
| `cards/qwen3_asr/api.py` | `board_mic` / `service` 两个 import 换成插件路径 |
| `cards/qwen3_asr/service.py` | **零改动** —— 它只 import `app.core.audio.*` / `app.core.config` |
| `cards/qwen3_asr/static/*` | **零改动** —— 逐字拷贝（URL / wizard 字段 id 一个没动） |
| `pyproject.toml` | 新增 entry point `qwen3-asr` + `package-data` 一条 |
| `scripts/verify_asr_live.py` | import 路径跟着改（4 处） |
| `app/core/audio/capture.py`、`scripts/verify_asr_long_audio.py` | 仅 docstring 里的路径引用 |

> **`backend.module` 是唯一需要改的「身份」字段** —— 路由前缀
> `/api/qwen3-asr`、静态挂载 `/static/apps/qwen3-asr`、wizard 字段 id
> 全都跟着 **卡 id** 走，与卡住在哪个仓库无关。

### ⚠️ 坑一：插件化后「向上寻址找宿主文件」会失效

`board_mic.py` 要 spawn 板载采集进程 `tools/asr_mic_agent.py`。原实现是
「从本文件所在目录逐级向上找 `tools/asr_mic_agent.py`」。

搬进插件后，向上寻址的**命中结果取决于插件装在哪儿**：

* editable 装在 `.venv/src/wdnapps-audio-chat/`（本机现状）⇒ 向上走
  恰好能摸到 `/userdata/wdnwebui/tools/asr_mic_agent.py` ⇒ **碰巧能用**；
* 真 wheel 装进 site-packages ⇒ 向上走摸不到宿主 ⇒ **板载麦克风静默失效**。

⇒ 改成**从 `app.core.__file__` 反推宿主仓库根**（插件本来就依赖
`app.core`，这条依赖是允许的方向）：

```python
for parent in Path(core_file).resolve().parents:
    if parent.name == "app":
        return parent.parent          # ← 宿主仓库根
```

> ⚠️ **别写死 `parents[1]`** —— `app/core/__init__.py` 的
> `parents[1]` 是 `<host>/app`（**少一层**），`parents[2]` 才是根。
> 本次就是先写成了 `parents[1]`，靠「向上寻址」兜底才没暴露。
> ⇒ **按目录名（`app`）找，不按索引找。**

> `tools/asr_mic_agent.py` **本身不搬**：它 import `app.core.audio.alsa`
> ⇒ 是**宿主**的工具，留在宿主 `tools/` 下。插件只是**用**它。

### ⚠️ 坑二：加了 entry point 必须重装，宿主才认

`pyproject.toml` 里加了 `[project.entry-points."wdnwebui.cards"]` 的新条目
**不会**自动生效 —— editable 装法的 entry point 存在
`site-packages/wdnapps_audio_chat-0.1.0.dist-info/entry_points.txt` 里，
是**装的时候**生成的。

```bash
.venv/bin/pip install -e .venv/src/wdnapps-audio-chat --no-deps --no-build-isolation
```

不重装 ⇒ 宿主扫不到这张卡 ⇒ **`/health` 里没有 `qwen3-asr`，应用网格里也没有**，
而且**没有报错**（只是「卡不存在」）。⇒ 改完 `pyproject.toml` 一定要重装再验证。

### ⚠️ 坑三：删掉 `app/apps/qwen3_asr/` 后，跑着的服务是「半坏」的

宿主服务是**常驻进程**：`manifest.json` / 路由 / 静态挂载都在**启动时**读一次。
删掉目录后，**不重启**的话：

* 路由还在（模块已 import 进内存）⇒ `POST /api/qwen3-asr/transcribe` 还通；
* 静态挂载指向**已不存在的目录** ⇒ `/static/apps/qwen3-asr/app.js` **404**
  ⇒ 前端白屏，且**后端毫无异常**。

⇒ **迁移完成后必须重启服务**（`sudo systemctl restart wdnwebui`）。
⚠️ 重启会连坐杀掉 `compute_worker`（`KillMode=control-group`）⇒ 确认
`config.jsonc` 的 `compute.autostart` 里有条目，否则重启后板子没有算力。

### 验证结果（本机 RK3588）

| 脚本 | 结果 |
|---|---|
| `scripts/check_plugin_entrypoints.py` | 11 张插件卡全 OK（含 `qwen3-asr`） |
| `scripts/verify_card_slots.py` | 12 张卡全有卡位占位符，渲染出 page section |
| `scripts/verify_apps_isolation.py` | A/B/C 全 PASS（静态扫描 + 可达性 + 故障隔离） |
| `scripts/verify_plugin_cards.py` | VERDICT: OK —— `qwen3-asr` 归类为 **plugin**（原 builtin），pageerror 空 |
| `scripts/verify_asr_live.py` | 49 通过 / 0 失败 |
| `/health` | 12 张卡全 `status=ok` |
| 静态资源 | `app.js` / `app.css` / `template.html` / `wizard.json` / `capture-worklet.js` / `preview.mp4` 全 200 |

## 清理 `app/apps/tts/` 残留：插件化后的「假源码点」（v0.44 附则）

TTS 卡拆到插件包后，宿主 `app/apps/tts/` 只剩一个 `static/app.js` ——
**它一行都不会被用到**，但看起来完全像源码。

### 为什么它不生效（三条独立原因，每条都足以让它失效）

1. **没有 `manifest.json`** ⇒ 内置卡扫描直接 `[skip] tts: no manifest.json`
   ⇒ 不生成 `CardManifest` ⇒ 压根不进 `manifests` 列表。
2. **`/static/apps/tts` 挂的是插件那份** —— `app/main.py` 用 `m.static_dir`
   挂载，而 `app/core/manifest.py` 里 `static_dir = card_dir / "static"`，
   插件卡的 `card_dir` 是 **entry point 模块所在目录**（插件包的
   `cards/tts/`）。实测：`/static/apps/tts/app.js` 返回 3268 行（插件版），
   树内那份 2574 行**从未被发出去过**。
3. **宿主没有「树内覆盖插件」机制** —— 宿主的 `/static` 挂的是
   `app/static/`（框架级），`app/apps/` 从未被挂载。两边同时有
   `manifest.json` 反而会撞 `[FATAL] duplicate card id`。

### 它是怎么回来的（**别再让它回来**）

```
10-29 01:56  95fce52  wade.weng  refactor(tts): 语音生成卡拆到 wdnapps_audio_chat 插件包
10-29 03:26  7844dfd  LiYuekai   feat(text-chat): 完善文本对话功能与 9B 模型的对接…
```

迁移提交把整张卡删干净了（父提交里已无此文件）。**1.5 小时后，一个
text-chat 提交把 `app/apps/tts/static/app.js` 又加了回来**（2574 行全新
文件，内容是迁移前的老版本）。

树内那份**缺**的正是后来加的功能：音色试听（`_updatePreviewButton` /
`previewVoice`）、用户音色克隆（`_droppedFile` / `uploadVoice`）、向导内选
音色（`_wizardVoice` / `_catalogPromise`）、新音色表（`news_female` /
`baby_girl` / `cool_lady` / `male_announcer` / `uncle_bass`）。

⇒ **风险不是「残留」，是「假源码点」**：按目录名直觉去改它，改动会被
**静默丢弃** —— 没有报错、没有告警，页面照旧跑插件那份。

⇒ 已删除。`app/apps/` 现在只剩 `text_chat` 一张内置卡。

> 同批清掉的还有 5 个只剩 `__pycache__/*.pyc` 的空壳目录
> （`audio_analysis` / `video_analysis_search` / `video_detection` /
> `video_target_detection` / `video_target_localization`）—— 它们在
> 内置卡扫描里各刷一条 `[skip] …: no manifest.json`，删掉后启动日志干净了。

### ⚠️ 回归脚本会改**生产**配置：`active-app` 必须 mock

`DELETE /api/compute/active-app` 会**连带清空** `compute.autostart`
（`app/core/compute/registry.py::clear_active_app`）—— 产品语义是
「退出应用 = 不想让这些算力开机自启」。

`PUT /api/compute/active-app` 则把承载该卡 caps 的 `(model, npu_index)`
**增量追加**进 autostart。

⇒ **任何用浏览器驱动前端、并在卡之间切换的回归脚本，都会改到设备级的
`config.jsonc`。** 2026-11-10 实测：跑完浏览器回归后 `compute.autostart`
变成 `[]` ⇒ 下次重启板子**没有任何算力自启**。

⇒ 已加 **`scripts/active_app_guard.py`**：在 Playwright 层拦下
`/api/compute/active-app` 的**写方法**（PUT/POST/PATCH/DELETE），
直接回 200 —— 前端不报 pageerror，请求根本没到服务端，生产配置原样不动。
**`GET` 必须放行**（`bootRestore()` 靠它读 `active_app` 决定开机跳哪张卡）。

已接入：`verify_plugin_cards.py` / `verify_asr_wizard.py` /
`verify_tts_voice_catalog.py`。其余 ~50 个浏览器脚本仍待接入 —— 新写
浏览器脚本时**一律**先 `install_on_page(page)`。

> ⚠️ Playwright 1.49 的 **`Browser` 没有 `route()` 方法**（只有
> `BrowserContext.route` 和 `Page.route`）。`browser.new_page()` 隐式建
> context、拿不到它 ⇒ 这种脚本只能**给 page 装**（`install_on_page`），
> 且**每个 page 都要单独装**。
