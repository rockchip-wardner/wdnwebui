# text_chat → wdnapps-audio-chat 迁移：实施清单与回归验证

> **✅ 已执行（2026-11-13）** —— 实施 + L1–L4 回归全部完成，结果见文末「执行结果」。
>
> 执行基线：宿主仓 `wdnwebui` @ `4154749`；插件仓 `wdnapps_audio_chat` @ `d2bd58e`
> 评估依据：[`TEXT_CHAT_PLUGIN_MIGRATION_REPORT.md`](./TEXT_CHAT_PLUGIN_MIGRATION_REPORT.md)
> 插件侧执行文档：[`wdnapps_audio_chat/docs/TEXT_CHAT_MIGRATION.md`](https://github.com/rockchip-wardner/wdnapps_audio_chat)
>
> ⚠️ **本迁移是「暂存」性质的**：卡片先落进 `wdnapps_audio_chat`（音频插件包），
> 但按生态「按模态分包」的惯例，纯文本对话卡最终应独立成 `wdnapps-chat` 包。
> 详见插件仓的暂存说明。

---

## 0. 迁移全景

```
迁移前                                     迁移后
──────────────────────────────────         ──────────────────────────────────
宿主仓 wdnwebui                            宿主仓 wdnwebui
├─ app/apps/text_chat/  ← 卡片源码          ├─ app/apps/           (空目录，保留)
│   ├─ manifest.json                         └─ app/core/…         (不变)
│   ├─ api.py / service.py                 插件仓 wdnapps_audio_chat
│   ├─ thinking_9b.py                       └─ src/wdnapps_audio_chat/cards/
│   └─ static/                                ├─ tts/          (不变)
│       ├─ template.html / app.js               ├─ qwen3_asr/    (不变)
│       ├─ wizard.json / preview.mp4            └─ text_chat/    ★ 新增（暂存）
│       └─ app.css  (空)                                ├─ manifest.json
├─ app/static/core/framework.css                        ├─ api.py / service.py
│   └─ 145 行 #page-text-chat 样式 ← 要搬走            ├─ thinking_9b.py
└─ .venv/src/wdnapps-audio-chat/                       └─ static/ (app.css 补齐)
    └─ cards/{tts,qwen3_asr}/
```

**不变的契约**（迁移全程必须保持）：

| 契约 | 值 |
|---|---|
| 卡 id | `text-chat`（连字符） |
| API 前缀 | `/api/text-chat` |
| 静态路径 | `/static/apps/text-chat/*` |
| 向导全局名 | `window.ImApp_text_chat.applyWizardSnapshot` |
| capability | `swcap_text_chat`（provider 仍在宿主 `tools/worker_manifests/`） |

---

## 1. 实施清单

### 阶段 0 · 前置（两侧各一次）

- [ ] 宿主仓 `git status` 干净（除 `docs/` 下未跟踪的报告文件）
- [ ] 插件仓 `git status` 干净；`git -C .venv/src/wdnapps-audio-chat log --oneline -1` 记下基线 commit
- [ ] 记录基线，便于回滚：
      ```bash
      git rev-parse HEAD                                  # 宿主
      git -C .venv/src/wdnapps-audio-chat rev-parse HEAD  # 插件
      ```
- [ ] 确认服务状态：`systemctl status wdnwebui.service`（**不要**与手动 `run.py` 同时跑 ——
      systemd 单元 `KillMode=control-group` 会连带 SIGTERM 掉 `compute_worker`）

### 阶段 1 · 宿主侧改动（宿主仓 `wdnwebui`）

#### 1.1 CSS 归位 🔴 最高危项

`app/static/core/framework.css` 的 **4287–4434 行**（从
`/* ---------- text-chat (文本对话) page ---------- */` 到那个空的
`@media (max-width: 700px) { }`）整段是 text_chat 专属样式：

- `#page-text-chat .imb-card` / `.imb-card-body--chat` / `.imb-conv-list` / `.imb-composer`
- `.text-chat-msg-block` / `.text-chat-thinking` / `.text-chat-thinking-body` /
  `.text-chat-thinking-toggle` / `.text-chat-cursor` / `.text-chat-error` /
  `.text-chat-stopped` / `.text-chat-tool-badge` / `#text-chat-tokens`
- `@keyframes text-chat-blink`

⚠️ **注意**：这段里**不全是 `#page-text-chat` 前缀** —— `.text-chat-*` 是无作用域的
全局类名，**必须整段一起搬**，不能只搬带 `#page-` 的那几条。

- [ ] 把 4287–4434 行**整段**剪切到卡片自己的 `static/app.css`（当前是 1 行空文件）
- [ ] 搬完后宿主 `framework.css` 里 **不应再出现 `text-chat` 字样**：
      ```bash
      grep -n "text-chat" app/static/core/framework.css   # 期望：无输出
      ```
- [ ] 搬过去的 CSS 依赖的共享类（`.imb-card` / `.imb-conv` / `.imb-msg--in/out` /
      `.imb-composer` / `var(--mono)`）**留在宿主** —— 插件卡已在用这些共享类
      （见 `tts/static/app.css`），不用搬
- [ ] 边界确认：4435 行的 `.vr-error-banner` 属于**视频检索卡**，**不要动**

#### 1.2 清理死代码

- [ ] `app/system/static/system.js:1258-1263` —— 删除 `if (name === "text-chat") { TextChat.init(); ... }`
- [ ] `app/system/static/system.js:1289-1291` —— 删除 `if (prev === "text-chat" ...) { TextChat.closeWs(); }`
      > 这 4 行引用的是一个**从来不是全局变量**的 `TextChat`（真实定义在
      > `app/apps/text_chat/static/app.js:5` 的 IIFE 作用域内）。之所以至今没炸，
      > 是因为 `system.js` 的 `showPage()`（1278 行）**没有任何调用者** ——
      > 框架走的是 `framework.js:1158` 的 `ROUTER.showPage()`。
      > 迁移后 `TextChat` 连 IIFE 都不在宿主了，这段引用必须删。
- [ ] `app/apps/text_chat/static/app.js` 的思考链路（**建议在搬进插件前清**）：
      - `:34-41`（复选框读 + localStorage 写）、`:396`、`:539`、`:680-684`
      > 思考模式已在三个层面同时关闭：`app/core/thinking.py:42`
      > `THINKING_ENABLED = False`（总闸）、`template.html` 的复选框 `disabled`、
      > `wizard.json` 的 `enableThinking` 字段已删 ⇒ `:680` 永远读到 `undefined`。
      > 死代码别搬进公开发布的插件包。

#### 1.3 CI

- [ ] `.github/workflows/regression.yml:60` —— 从 `py_compile` 列表里删掉
      `app/apps/text_chat/api.py`（文件将不存在于本仓 → **CI job 硬失败**）
- [ ] `.github/workflows/regression.yml:56` —— 注释已过时：
      「The cards still living in-tree are text_chat / tts」→ **tts 早已外移**，
      改成迁移后的实际状态

#### 1.4 回归脚本适配

- [ ] `scripts/verify_thinking_router.py:25` ——
      `from app.apps.text_chat.service import _ThinkingRouter`
      → 改为 `from wdnapps_audio_chat.cards.text_chat.service import _ThinkingRouter`
      （或把这条回归整体迁进插件仓）
      > ⚠️ `scripts/` 下**没有** `__init__.py`，`python -m scripts.X` 走的是
      > 命名空间包，能工作；但**插件包必须先装上**，否则 import 失败。

- [ ] `scripts/card_dirs.py:5-13` docstring —— 插件包名列表要补上 text-chat 所在的位置说明

#### 1.5 目录与文档

- [ ] `app/apps/text_chat/` 整目录删除
- [ ] `app/apps/__init__.py` **保留**（空文件）—— 删掉整个 `app/apps/` 目录会触发
      `app/core/manifest.py:148` 的 `apps/ directory not found` 告警
- [ ] `AGENTS.md:1722` 卡片清单表 —— `text-chat` 来源「内置」→ `wdnapps_audio_chat`；
      后端入口 → `wdnapps_audio_chat.cards.text_chat`
- [ ] `AGENTS.md:1713-1718` —— 「7 张卡在插件包」→ 8 张
- [ ] `AGENTS.md:7378` —— 「`loadPageData("text-chat")` 调 `TextChat.init() + refresh() + ensureWs()`」
      **过时**（该路径是死代码），改成实际入口「卡片自己的 `app.js:661 TextChat.init()`」
- [ ] `AGENTS.md:7383` —— 「`swcap_text_chat` 现在**只有 qwen3.5-vl-4b 一个 provider**」
      **过时**（9B 也声明了它）
- [ ] `app/core/manifest.py:11-13` docstring ——
      「After the v0.25 split this holds only ``text_chat``」→ 无内置卡了

### 阶段 2 · 插件侧改动（插件仓 `wdnapps_audio_chat`）

详细步骤见插件仓 `docs/TEXT_CHAT_MIGRATION.md`。摘要：

- [ ] `cards/text_chat/` 整目录拷入（从宿主 `app/apps/text_chat/`）
- [ ] `cards/text_chat/manifest.json` —— `"backend.module"` →
      `"wdnapps_audio_chat.cards.text_chat"`
- [ ] `cards/text_chat/__init__.py` —— `register_shutdown()` 里
      `from app.apps.text_chat import service` → `from wdnapps_audio_chat.cards.text_chat import service`；
      **顺带删掉** `"bot", "llm", "video_cache", "video_manager", "service_wechat"`
      这 5 个子模块探测 —— 那是 im_bridge 的遗留，text_chat 根本没有这些模块
- [ ] `cards/text_chat/static/app.css` —— 填入从宿主搬来的 CSS
- [ ] `pyproject.toml` —— entry point + package-data + description + version
      （见插件仓文档）
- [ ] 包级 `__init__.py` docstring —— 「提供 2 张卡片」→ 3 张
- [ ] `README.md` / `README_CN.md` / `AGENTS.md` —— 卡片表 + 暂存说明

### 阶段 3 · 安装与装载

- [ ] 重装插件（**不要**手动 pip，见插件 README）：
      ```bash
      cd /userdata/wdnwebui
      ./update_rkwhl.sh
      ```
      或强制全量：`UPDATE_RKWHL_FORCE_REBUILD=1 ./update_rkwhl.sh`
- [ ] 校验 entry point：
      ```bash
      .venv/bin/python scripts/check_plugin_entrypoints.py wdnapps_audio_chat
      ```
      期望 **3 行** `OK <id> (<dir>)`（`tts` / `qwen3-asr` / `text-chat`），
      无 `BAD:` 行
- [ ] 重启服务并看日志：
      ```bash
      sudo systemctl restart wdnwebui
      journalctl -u wdnwebui -n 80 | grep -i "plugin card\|text-chat\|FATAL"
      ```
      期望：`loaded 3 plugin card(s) from wdnwebui.cards: ..., text-chat`
- [ ] ⚠️ **重启会连坐杀掉 `compute_worker`**（systemd `KillMode=control-group`）——
      重启后确认 `compute.autostart` 有值，否则板子没有任何算力，
      需手工 `POST /api/compute/instances` 把模型拉回来

---

## 2. 回归验证步骤

分四层。**L1/L2 不占 NPU**，可在任何时候跑；L3 需要浏览器；L4 需要真机 + 模型已加载。

### L1 · 静态与装载层（不需要服务、不需要 NPU）

| # | 命令 | 期望 |
|---|---|---|
| 1 | `python -m scripts.verify_card_slots` | 每张已发现的卡都有卡位占位符；渲染出的首页含 `id="page-text-chat"` |
| 2 | `.venv/bin/python scripts/check_plugin_entrypoints.py wdnapps_audio_chat` | 3 行 `OK`，无 `BAD:` |
| 3 | `.venv/bin/python scripts/card_dirs.py` | JSON 里 `text-chat` 的 `ns` = `wdnapps_audio_chat.cards`、`dir` 指向插件仓 |
| 4 | `python -m scripts.verify_apps_isolation` | 跨卡 import 检查通过（text_chat 只 import `app.core.*`） |
| 5 | `python -m scripts.verify_thinking_router` | `_ThinkingRouter` 分频 + `<answer>` 标签剥离仍绿（**改了 import 路径**） |

> #5 必须在插件包装上之后跑 —— 它 import 的是插件命名空间。

### L2 · HTTP 层（需要服务在跑，不需要 NPU）

服务起来后逐条打：

| # | 请求 | 期望 |
|---|---|---|
| 6 | `GET /api/text-chat/`（**带尾斜杠**） | 200，`{"state": "idle", "busy": false, ...}` |
| 7 | `GET /api/text-chat/history` | 200，`{"messages": []}` |
| 8 | `POST /api/text-chat/send` `{"text": ""}` | **422** ⚠️ 不是 400 —— pydantic 的 `Field(min_length=1)` 在到达 `api.py` 的 `ValueError` 分支**之前**就拒了。实测 422 才是正确行为 |
| 8b | `POST /api/text-chat/send` `{"text": "x"}`（模型未加载） | 200（异步接受），**随后** snapshot 转 `error`，`error` = `功能未启用: swcap_text_chat` |
| 9 | `POST /api/text-chat/stop` | 200，`{"ok": true, ...}` |
| 10 | `POST /api/text-chat/reset` | 200，`{"ok": true, ...}` |
| 11 | `GET /static/apps/text-chat/app.js` | 200 |
| 12 | `GET /static/apps/text-chat/app.css` | 200 **且 size > 0** ⚠️ **CSS 漏搬的探针**（迁移前该文件是 1 字节空文件） |
| 13 | `GET /static/apps/text-chat/template.html` | 200 |
| 14 | `GET /static/apps/text-chat/wizard.json` | 200 |
| 15 | `GET /` | 200，HTML 里含 `id="page-text-chat"` |

> #12 是**最重要的一条**：`app.css` 漏搬时，页面能开、能发消息，但**全是裸样式**
> —— 不会报任何错，只能靠这个探针或肉眼发现。
>
> 一键跑：
> ```bash
> for u in /api/text-chat /api/text-chat/history /static/apps/text-chat/app.js \
>          /static/apps/text-chat/app.css /static/apps/text-chat/template.html \
>          /static/apps/text-chat/wizard.json; do
>   printf "%-46s %s\n" "$u" "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000$u)"
> done
> ```

### L3 · 浏览器层（需要 playwright + 服务在跑）

| # | 命令 / 动作 | 期望 |
|---|---|---|
| 16 | `python scripts/verify_plugin_cards.py` | 侧栏出现「文本对话」→ 点进去页面渲染 → JS/CSS/wizard.json 全部 200 → **无 pageerror** |
| 17 | `python -m scripts.smoke_wizard_all` | 8 张带向导的卡逐个 mount 一遍，text_chat 在列 |
| 18 | `python scripts/verify_single_card_ui.py` | text_chat 单卡流程正常（模型 POST 到 NPU0） |
| 19 | 手测 | 输一句话 → user 气泡 → assistant 气泡 → 上下文计数器「N / M tokens」有值 |
| 20 | 手测 | 切到别的卡再切回 → **气泡还在、WS 重连正常**（验卡片 IIFE 只跑一次的假设） |

> #16 的 `pageerror` 判据价值最高：卡片 `app.js` 是 IIFE，它引用的共享模块 404
> 时报错发生在 IIFE 执行期 —— 截图和 DOM 断言都可能「看着像没事」，只有 pageerror 能抓到。

### L4 · 真机 NPU 层（需要模型已 running）

| # | 命令 | 期望 |
|---|---|---|
| 21 | `sudo .venv/bin/python scripts/verify_stage3_smoke.py --group chat` | `swcap_text_chat` 纯文本轮 + 工具轮通过 |
| 22 | `sudo .venv/bin/python scripts/verify_9b_thinking_tool_long.py` | 9B 长思考 + 工具调用（⚠️ 思考总闸已关，行为与预期可能不同） |
| 23 | 手测 | 发送 → 流式出字 → **点「停止」能真中断**（rknn3 侧中止并释放 NPU） |
| 24 | 手测 | 开启 `swcap_text_chat` 实例 → 微信发一句话 → 自动回复（验证**能力**没被迁移影响） |

> #24 验证的是 `swcap_text_chat` **能力**仍然可用 —— 微信桥接（`app/system/im_bridge/`）
> 用的是能力、不是卡片，迁移**不应该**影响它。迁移后如果微信桥接坏了，
> 说明 provider 声明被误动了。

> ⚠️ **#21/#22 必须用 `sudo` 跑，而且跑之前要腾空 NPU**：
>
> 1. **权限**：worker 的日志写在 `/tmp/wdnwebui-worker/` —— 那目录是 **root**
>    建的（systemd 服务以 root 跑）。普通用户跑脚本会
>    `spawn failed: [Errno 13] Permission denied: '/tmp/wdnwebui-worker/<model>.log'`
>    → spawn 失败 → 后面只剩一句误导人的 `load RPC timed out`。
> 2. **NPU 冲突**：`verify_stage3_smoke.py` 自带 registry，`boot()` 会
>    **自己 spawn 一个 worker**。服务侧若已在同一张卡上加载了同一个模型，
>    两个 worker 抢同一个 NPU → load RPC 超时。
>    ⇒ 跑之前先 `DELETE /api/compute/instances` 把服务侧的实例卸掉，
>    跑完再从算力页装回来。
>
> 这两条与迁移无关，是这个 standalone harness 的既有约束 —— 但第一次
> 跑的人一定会踩，记在这里。

---

## 3. 回滚

迁移涉及**两个仓库**，回滚也要两侧一起做：

```bash
# 插件侧
git -C .venv/src/wdnapps-audio-chat reset --hard <基线 commit>
rm -rf .venv/src/wdnapps-audio-chat/src/wdnapps_audio_chat/cards/text_chat
cd /userdata/wdnwebui && git reset --hard <基线 commit>
./update_rkwhl.sh          # 重装插件
sudo systemctl restart wdnwebui
```

⚠️ 回滚后同样要确认 `compute.autostart` 有值（重启连坐杀 worker）。

---

## 4. 完成判据

全部满足才算迁移完成：

- [ ] `app/apps/` 下没有卡片目录（`__init__.py` 保留）
- [ ] `grep -rn "text-chat" app/static/core/framework.css` 无输出
- [ ] `grep -rn "text_chat" app/system/static/system.js` 无输出
- [ ] `grep -rn "app\.apps\.text_chat" .` 无输出（宿主内不再引用旧命名空间）
- [ ] `check_plugin_entrypoints.py wdnapps_audio_chat` → 3 行 OK
- [ ] 宿主日志：`loaded 3 plugin card(s) from wdnwebui.cards: ..., text-chat`
- [ ] L1–L4 全部通过
- [ ] 插件仓 `README*.md` / `AGENTS.md` 已写明**暂存**状态与未来拆包计划
- [ ] 两侧 commit message **均为英文**（Conventional Commits）
- [ ] 推送顺序：先 `git push origin master`（局域网主仓库），再 `git push github master`（GitHub）；**Gitee 一概不碰**

---

## 5. 执行结果（2026-11-13）

### 5.1 改动清单（实际落地）

**宿主仓 `wdnwebui`**

| 文件 | 改动 |
|---|---|
| `app/apps/text_chat/` | **整目录删除**（10 个文件） |
| `app/static/core/framework.css` | 删掉 4287–4434 行的 text-chat 专属样式（145 行） |
| `app/system/static/system.js` | 删掉 2 段引用不存在全局 `TextChat` 的死代码 |
| `app/core/manifest.py` | docstring 更新（内置通道已无卡） |
| `app/core/thinking.py` | docstring 里的卡片路径更新 |
| `scripts/verify_thinking_router.py` | 改为**插件路径优先 + 旧路径兜底** |
| `scripts/verify_tool_channel_filter.py` | 卡片目录改走 `card_dirs.card_dir("text-chat")`（否则 FileNotFoundError） |
| `scripts/verify_text_chat_plugin_card.py` | **新增** —— text-chat 插件卡专项浏览器回归（19 条断言） |
| `scripts/card_dirs.py` | docstring 更新（11 张卡在插件包） |
| `.github/workflows/regression.yml` | 删掉 `app/apps/text_chat/api.py` 的 py_compile；修掉过时注释 |
| `AGENTS.md` | 卡片清单表 / 路径 / 2 处过时表述 |

**插件仓 `wdnapps_audio_chat`**（`d2bd58e` → 工作区）

| 文件 | 改动 |
|---|---|
| `cards/text_chat/`（新增，10 文件） | 从宿主整目录拷入 |
| `cards/text_chat/manifest.json` | `backend.module` → `wdnapps_audio_chat.cards.text_chat` |
| `cards/text_chat/__init__.py` | import 路径改插件命名空间；删掉 im_bridge 遗留的 5 个子模块探测 |
| `cards/text_chat/api.py` | `from app.apps.text_chat.service` → 插件命名空间 |
| `cards/text_chat/service.py` | `from app.apps.text_chat.thinking_9b` → 插件命名空间 |
| `cards/text_chat/static/app.js` | 清理已死的思考链路（复选框 localStorage 同步 / 向导 `enableThinking`） |
| `cards/text_chat/static/app.css` | 从 1 行空文件 → **159 行**（宿主搬来的 145 行 + 说明头） |
| `pyproject.toml` | entry point + package-data + version 0.1.1→0.2.0 + description |
| `src/wdnapps_audio_chat/__init__.py` | version + docstring（2 张卡 → 3 张） |
| `AGENTS.md` / `README.md` / `README_CN.md` | 卡片表 + **暂存说明** |
| `docs/TEXT_CHAT_MIGRATION.md`（新增） | 暂存说明 + 迁入步骤 + 未来拆包步骤 |

### 5.2 回归结果

| 层 | 项 | 结果 |
|---|---|---|
| **L1** | `verify_card_slots` | ✅ 12 张卡全有卡位占位符 + page section |
| L1 | `check_plugin_entrypoints.py wdnapps_audio_chat` | ✅ 3 行 OK（tts / qwen3-asr / text-chat） |
| L1 | `card_dirs.py text-chat` | ✅ 指向插件包路径 |
| L1 | `verify_apps_isolation` | ✅ PASS（12 张卡，A/B/C 三组全过） |
| L1 | `verify_thinking_router` | ✅ 9/9（改路径后仍绿） |
| L1 | `verify_tool_channel_filter` | ✅ 11/11（新增：改走 card_dirs） |
| **L2** | 5 个静态资源 | ✅ 全 200；`app.css` **4328 bytes**（迁移前 1 byte 空文件） |
| L2 | `GET /api/text-chat/` / `history` / `POST stop` / `POST reset` | ✅ 全 200 |
| L2 | `POST /send` 空 text | ✅ **422**（pydantic `min_length=1` 先拒，非 400） |
| L2 | `POST /send` 正常文本（无模型） | ✅ 200 → 异步转 `error`：`功能未启用: swcap_text_chat` |
| L2 | `GET /` 含 `id="page-text-chat"` | ✅ |
| **L3** | `verify_plugin_cards.py` | ✅ VERDICT: OK —— text-chat `plugin ok wizard=yes` |
| L3 | `smoke_wizard_all` | ✅ 8/8（text-chat: sections=1, dots=3, `ImApp_text_chat.applyWizardSnapshot=True`） |
| L3 | **`verify_text_chat_plugin_card.py`（新增）** | ✅ **19/19** —— 渲染 / CSS 生效 / 跳过向导 / 切页往返 / 无 pageerror |
| **L4** | `verify_stage3_smoke.py --group chat` | ✅ **5/5** —— worker 启动 / agent 循环 / 256 个流式 delta / 无错误 / 工具轮 |
| L4 | 迁移后卡片真实推理（走服务） | ✅ answer=`你好，我是 Qwen3.5…`，`tool_call=TtsAlert` 成功解析 |
| L4 | 「停止」真中断 | ✅ `state=running → stopped`，`busy=false`，**被停轮未写历史**（history_count 不变） |

**L4 工具轮细节**：smoke 自带的 registry 没装 `qwen3-tts`，`TtsAlert` 报
「功能未启用: swcap_text_to_speech」—— 但 agent **把失败回喂给模型**、模型给出
面向用户的自然语言答复，判定 PASS。这正是 `app/core/chat/agent.py` 设计的
「与结果型同构，无特殊分支」。

### 5.3 执行中发现的 3 个额外问题（已修）

1. **`scripts/verify_tool_channel_filter.py` 会 FileNotFoundError** —— 它硬编码读
   `app/apps/text_chat/service.py`。清单里漏了这一条（我原稿只查了
   `verify_thinking_router.py`）。已改为走 `card_dirs.card_dir("text-chat")`。
2. **插件卡内部有 2 处旧命名空间自引用** —— `api.py` 的
   `from app.apps.text_chat.service` 和 `service.py` 的
   `from app.apps.text_chat.thinking_9b`。不改成插件命名空间，卡片 import 时
   会直接 ImportError。已修。
3. **`verify_stage3_smoke.py` 的两个既有约束**（与迁移无关，但第一次跑必踩）：
   - 必须 `sudo` 跑 —— worker 日志写在 `/tmp/wdnwebui-worker/`，那目录是
     **root** 建的；普通用户跑会 `spawn failed: [Errno 13] Permission denied`，
     然后只剩一句误导人的 `load RPC timed out`；
   - 跑之前要腾空 NPU —— 脚本自带 registry 会**自己 spawn 一个 worker**，
     服务侧若已在同一张卡上加载了同一个模型就会抢卡。

### 5.4 板子状态（最终）

| 模型 | NPU | 说明 |
|---|---|---|
| DepthAnythingV3 | 0, 1 | **与迁移前一致** |
| qwen3.5-vl-4b | 2 | **新增** —— 见下 |

⚠️ **本次顺手补齐了 4b 的 worker manifest**（一项超出迁移范围、但 L4 必需的改动）：
`/userdata/models/Qwen3.5-4B/` 里**权重齐全**（4.3 GB）却**没有 worker
manifest**，所以 `discover()` 扫不到它 —— 本板此前**根本没有 `swcap_text_chat`
provider**（这正是 L4 无法跑的原因）。已按发布载荷部署：

```bash
sudo cp tools/worker_manifests/qwen3.5-vl-4b.json \
        /userdata/models/Qwen3.5-4B/qwen3.5-vl-4b.json
```

manifest 的 `model_dir_name` 正好是 `Qwen3.5-4B`，与目录名逐字相同。

⇒ 迁移后 text-chat 卡**开箱即用**（算力页可一键启用），而迁移前它在本板是
「无 provider」状态。这不是迁移带来的能力，是把**早已存在于板上的权重**补上
了它缺失的那份描述文件。

### 5.5 未完成 / 未验证

- **两侧均未 commit、未 push** —— 按 AGENTS.md 的规范（**英文** Conventional
  Commits；先 `origin` 后 `github`；Gitee 不碰）提交时需另行确认。
- `verify_single_card_ui.py` **在步骤 4 超时**（不是步骤 5 的 text-chat）：
  它要 `qwen2.5-omni-3b` 和 `qwen3-vl-4b`，**本板都没装**，向导的
  「一键开启所需模型」按钮因此是 disabled（title=`本板未安装所需模型，无法启用`）。
  ⇒ **板子状态限制，与迁移无关**。text-chat 的部分由新增的
  `verify_text_chat_plugin_card.py` 覆盖并全绿。
- 9B 路径（`qwen3.5-vl-9b`）**本板未安装**，未验证。
- 微信桥接（L4-24）未手测 —— 桥接开关需要 `swcap_text_chat` 实例 running，
  本板有条件但未实际发微信消息。

