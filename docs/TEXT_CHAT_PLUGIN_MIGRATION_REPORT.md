# 把 `app/apps/text_chat` 迁到插件包：合理性评估报告（v2 · 代码更新后重评）

> 评估日期：2026-11-12
> 宿主仓库：`/userdata/wdnwebui`（`master` @ **`4154749`**，较上版 `4175400` 有 8 个新提交）
> 目标插件：`.venv/src/wdnapps-audio-chat/`（独立仓 `wardner/wdnapps_audio_chat.git` @ **`d2bd58e`，本次未变动**）
> 关联：[`TEXT_CHAT_USAGE_REPORT.md`](./TEXT_CHAT_USAGE_REPORT.md)（text_chat 使用点全景）

---

## 0. 结论速览

| 维度 | 判定 | 较上版变化 |
|---|---|---|
| **技术可行性** | ✅ **完全可行** —— 插件机制对卡片是通用的，宿主**一行 core 代码都不用改** | 不变，且**更强**：text_chat 的依赖面 100% 落在 `app.core.*`（新增 `app.core.thinking`），插件全部合法 |
| **语义归属** | ❌ **不合理** —— `wdnapps-audio-chat` 是**音频**插件，而 text_chat 是**纯文本 LLM 对话**卡（`swcap_text_chat`，文本→文本） | 不变，插件仓本次未动，三处自述仍写「音频类」「2 张卡」 |
| **基础设施代价** | 🟢 **零** —— 加进现有插件包不需要动 `update_rkwhl.sh`、不需要新 git 仓 | 不变 |
| **推荐动作** | **不建议放进 `wdnapps-audio-chat`**。若目标是「清空 `app/apps/`、让内置通道退役」，应新建 `wdnapps-chat` 插件包 | **理由更强了**：宿主已新增一类「非卡片的宿主级功能」（界面翻译），卡片插件化已是常态，内置通道只剩 text_chat 一张卡 |

**一句话**：「能不能」不是问题；「该不该」是问题 —— 按现有生态的**按模态分包**
惯例，文本对话卡不属于音频插件包。

### 本次更新带来的三个新事实

| # | 新事实 | 对本评估的影响 |
|---|---|---|
| 1 | **`app/core/thinking.py` 诞生** —— 应用层「思考模式」总闸（`THINKING_ENABLED = False`），四个执行点强制生效（proxy / worker / text_chat / …） | ✅ text_chat 新增依赖 `app.core.thinking` → **仍是 core**，插件合法 |
| 2 | **界面翻译落地** —— `app/core/translate/` + `app/system/api/translate.py` + `app/static/core/translate.{js,css}`，走板端代理转外部 MT（MyMemory），**不是卡片**（无 `card_slot`，挂在 `/api/translate`） | 🟢 宿主现在有**两类非卡片功能**（算力页、翻译页）+ **一张内置卡** → 「内置通道只剩 text_chat」已成事实 |
| 3 | **`swcap_text_chat` 现在有 2 个 provider** —— `qwen3.5-vl-4b` **和** `qwen3.5-vl-9b` 都声明了它（AGENTS.md 仍写「只有 4b 一个」，已过时） | 🟡 9B 是 text_chat 的 `PREFERRED_MODELS[0]`，走多卡 VLM 后端 → 模块级依赖 `jinja2`（本次才补进 `requirements.txt`）。这条依赖链**完全在宿主侧**，迁卡不迁它 |

---

## 1. 事实基线（已逐条核实 · 基于 `4154749`）

### 1.1 目标插件的现状（**本次无变化**）

```
.venv/src/wdnapps-audio-chat/           ← 独立 git 仓（.venv/ 在宿主 .gitignore 里）
├── pyproject.toml   description = "wdnwebui 音频类应用插件：语音生成 + 流式语音识别"
├── README_CN.md     "音频类应用插件包…会出现这两张卡"
└── src/wdnapps_audio_chat/
    ├── __init__.py  "提供 2 张卡片"   (__version__ = "0.1.1")
    └── cards/
        ├── tts/          category=audio   swcap_text_to_speech   app.css 374 行
        └── qwen3_asr/    category=audio   swcap_audio_asr         app.css 536 行
```

`git -C .venv/src/wdnapps-audio-chat status` → **干净**；`git log` 仍停在
`d2bd58e`。插件包本次**完全没动**，所以「语义错位」这条判断原样成立。

### 1.2 宿主侧本次新增了什么（`4175400..4154749`）

```
AGENTS.md                               | 279 ++++   （界面翻译 + 思考总闸两节）
app/apps/text_chat/service.py           |  14 +-     （改用 effective_thinking）
app/apps/text_chat/static/template.html |  10 +-     （思考开关置 disabled）
app/apps/text_chat/static/wizard.json   |   3 +-     （删掉 enableThinking 字段）
app/core/compute/proxy.py               | 148 +++    （思考总闸 + 推理超时自愈）
app/core/compute/registry.py            |  69 ++     （recover_wedged_instance）
app/core/compute/workers/_base.py       |  71 ++     （worker 侧思考总闸）
app/core/thinking.py                    |  52 ++  ★  （思考模式总闸，新模块）
app/core/translate/{__init__,providers,service}.py | 509 ++ ★（界面翻译，新子系统）
app/static/core/translate.{css,js}      | 655 ++  ★ （右上角语言切换）
app/system/api/translate.py             | 103 ++  ★
app/static/index.html.template          |   4 +      （translate.css/js）
app/system/__init__.py                  |   5 +
requirements.txt                        |  24 +      （jinja2 —— 多卡 VLM 后端）
```

★ = 新增模块。

### 1.3 插件卡机制（对卡片通用，未变）

`app/core/manifest.py::discover()` 两条发现通道并成同一条校验管线：

```
1. 内置 —— 扫 app/apps/*/manifest.json（磁盘）
2. 插件 —— 扫 wdnwebui.cards entry point 组（已安装的 wheel）
   entry point 名 = 卡 id（连字符，须与 manifest "id" 逐字相同）
   值 = 可 import 的卡模块；宿主据此定位 <模块目录>/manifest.json + /static/
```

⇒ 插件包**不需要任何特殊支持**，目录结构与在树时逐字一致即可，
**所有 URL / 路由 / wizard 字段 id 一个都不用改**（`AGENTS.md:1705-1708`）。

### 1.4 生态的分组逻辑是**按模态**

| 插件包 | 模态 | 卡 |
|---|---|---|
| `wdnapps_video_analyzer` | 视频 ↔ 文本 | video-detection / video-retrieval / video-target-* / audio-analysis |
| `wdnapps_audio_chat` | 音频 ↔ 文本/语音 | tts（文本→语音）、qwen3-asr（音频→文本） |
| `wdnapps_media_generator` | 文本 → 图像 | media-generator / image-editor |
| `wdnapps_hdmi_processor` | 视频 → HID 动作 | usb-hid |
| **（缺）** | **文本 → 文本** | **text_chat 仍内置** |

---

## 2. 技术可行性评估：✅ 可行，且本次更新后**更干净**

### 2.1 宿主侧不用改 core

| 机制 | 是否需要改 | 依据 |
|---|---|---|
| 卡片发现 | ❌ 不用改 | `manifest.py::_discover_from_entry_points()` 通用；entry point 名 = 卡 id |
| 路由挂载 | ❌ 不用改 | 卡片自己的 `register_api()` → `include_router(prefix="/api/text-chat")` |
| 静态挂载 | ❌ 不用改 | `app/main.py:257` 按 manifest 的 `static_dir` 挂 `/static/apps/<id>`，与来源无关 |
| 首页卡位 | ❌ **不用删** | `index.html.template:624` 的 `{% card_slot "text-chat" %}` **保留** |
| 前端资源加载 | ❌ 不用改 | `framework.js::_loadCardJS` 按 `manifest.frontend.js` 注入，与来源无关 |
| CSS 注入 | ❌ 不用改 | `html_assembly.py` 按 `manifest.frontend.css` 生成 `<link>` |
| 向导 | ❌ 不用改 | `manifest.wizard_config` → `wizard.json`，机制一致 |
| DemoGuard | ❌ 不用改 | `manifest.demo_guard` 自动注册 |
| 能力词表 | ❌ 不用改 | `swcap_text_chat` 由 **worker manifest**（宿主 `tools/worker_manifests/`）声明，卡片只是消费者 |
| 共享 chat 子系统 | ❌ 不用改 | `app/core/chat/*` 留在宿主，插件 `from app.core.*` import —— 合法（插件 → core 单向） |
| **思考总闸** | ❌ 不用改 | `app/core/thinking.py::effective_thinking` —— 仍是 core，插件合法 |

**关于 `{% card_slot %}`**：宿主模板里 13 个卡位占位符**全部保留**，包括 5 张
插件卡的（`qwen3-asr` / `tts` / `media-generator` / `image-editor` / `usb-hid`）。
插件没装时 `_drop_unmatched_card_slots()`（`html_assembly.py:131-156`）把该行
换成 HTML 注释，用户看不到。⇒ **插件卡同样需要宿主模板里留一行占位符**。

### 2.2 text_chat 的依赖面（**全部落在 `app.core.*`**）

| 依赖 | 用途 | 插件合法性 |
|---|---|---|
| `app.core.chat.runner` | `MAX_NEW_TOKENS`、`split_thinking` | ✅ core |
| `app.core.chat.agent` | `run_chat_with_tools`（工具循环、上下文裁剪） | ✅ core |
| `app.core.chat.serial` | `get_chat_run_lock()`（与微信桥接互斥） | ✅ core |
| `app.core.compute.registry` | `get_registry()` → `get_session_for_model()` | ✅ core |
| `app.core.tools.registry` | `resolve_available_tool_names(channel="chat_web")` | ✅ core |
| **`app.core.thinking`** | **`effective_thinking()`（本次新增）** | ✅ core |

`app/core/chat/` 内部**不 import 任何 `app.apps.*` / `app.system.*`**（已核实）
⇒ 无循环依赖风险。

### 2.3 插件侧要做的（若放进现有插件包）

```
src/wdnapps_audio_chat/cards/text_chat/     ← 从 app/apps/text_chat/ 整目录拷过去
├── __init__.py      register_shutdown() 里 "app.apps.text_chat" → 改插件命名空间
├── api.py           后端零改动（只 import app.core.*）
├── service.py       后端零改动（只 import app.core.*）
├── thinking_9b.py   零改动
├── manifest.json    "backend.module" → "wdnapps_audio_chat.cards.text_chat"
└── static/
    ├── template.html / app.js / preview.mp4 / wizard.json   零改动
    └── app.css      ⚠️ 见 §5.1 —— 必须从宿主 framework.css 搬 145 行进来
```

`pyproject.toml` 两处：

```toml
[project.entry-points."wdnwebui.cards"]
"text-chat" = "wdnapps_audio_chat.cards.text_chat"     # 连字符 id ↔ 下划线模块，同 qwen3-asr 的写法

[tool.setuptools.package-data]
"wdnapps_audio_chat.cards.text_chat" = ["manifest.json", "static/*"]
```

**`package-data` 漏了是最阴的坑**（插件 README 原话）：卡片仍会被发现、
后端路由正常，但前端 404 —— 「卡片在、页面空白」。

### 2.4 基础设施代价：零

放进**现有**插件包的话：

- ❌ 不用新建 git 仓（局域网 `wardner/wdnapps_audio_chat.git` / GitHub 镜像都现成）
- ❌ 不用改 `update_rkwhl.sh`（4 个插件包名在 224 / 558-564 / 576 行硬编码，
  加第 5 个包要改 4 处）
- ❌ 不用改 `verify_plugin_cards` / `check_plugin_entrypoints.py` 的包名列表
- ✅ 只需要：插件版本 0.1.1 → 0.2.0 + 一次 `pip install -e` 重装

---

## 3. 语义归属评估：❌ 不合理

### 3.1 能力词表不属于音频域

`AGENTS.md` 的能力命名轴是 **`<输入模态>_<输出形态或功能>`**：

| 能力 | 含义 | provider | 域 |
|---|---|---|---|
| `swcap_text_chat` | **文本→文本**（多轮/流式） | qwen3.5-vl-4b **/ 9b** | **文本** |
| `swcap_text_to_speech` | 文本→语音 | qwen3-tts | 音频 |
| `swcap_audio_asr` | 音频→文本 | qwen3-asr | 音频 |
| `swcap_audio_chat` | 音频→文本 | qwen2.5-omni-3b | 音频 |

`swcap_text_chat` 的输入**和**输出都是文本 —— 它不碰任何音频路径。
放进一个「音频类应用插件包」在能力语义上是错位的。

### 3.2 插件的自述边界会被打破

塞进去以后，插件包的三处自述全部要改：

| 文件 | 现状 | 塞入 text_chat 后 |
|---|---|---|
| `pyproject.toml` | "音频类应用插件：语音生成 + 流式语音识别" | 得写成"…+ 文本对话"，标题与内容不符 |
| `README_CN.md` / `README.md` | "音频类应用插件包…这两张卡" | "这三张卡"，且要解释为什么文本卡在音频包里 |
| `__init__.py` docstring | "提供 2 张卡片" | "提供 3 张卡片"，列举里要加一条非音频的 |

**插件的用户预期被打破**：装 `wdnapps-audio-chat` 是为了 TTS/ASR，却多出一张
文本对话卡。反过来，想用文本对话的人得装「音频插件」—— 命名误导。

### 3.3 一个值得认真对待的反方论点

「语音对话」完整链路 = ASR（音频→文本）+ **LLM（文本→文本）** + TTS（文本→语音）。
按这个读法，text_chat 放进 `audio_chat` 确实能凑成一条完整的语音助手回路。

**但这个论点有三个问题**：

1. **插件的既有边界是「音频 I/O」，不是「对话大脑」**。它自己的 docstring 写
   「卡片后端 import 宿主的服务（`app.core.audio.tts_bridge` /
   `app.core.audio.capture` / `app.core.audio.endpoint`）」—— 全是音频基建。
   text_chat 一个都不用，它用的是 `app.core.chat.*` + `app.core.compute.registry`
   + `app.core.thinking`。2. **「对话大脑」是所有模态共用的**。usb-hid 卡的 AI 任务循环也用
   `swcap_vision_chat` + `swcap_tool_call`，走同一个 `app/core/chat/agent.py`。
   把「LLM 大脑」塞进某个模态插件，等于把跨模态的公共件绑死在一个插件里。
3. **能力词表的 provider 在宿主**（`tools/worker_manifests/qwen3.5-vl-*.json`），
   不在插件。插件只是消费者。把它和音频 I/O 绑在一个可独立安装的包里，
   没有技术收益。

### 3.4 本次更新反而**加强**了「thinking 属 core」的论证

`app/core/thinking.py` 的文档明确写着，这个总闸覆盖
**「文本对话 / 电脑控制 / 音视频描述 / 微信桥接」**四个执行点 —— 它是
**平台级**的横切关注点，不是某张卡的私有开关。

⇒ 「思考模式」已经在宿主 core 里了；text_chat 只是它的一个消费者。
把 text_chat 挪走时，`app/core/thinking.py` 留在宿主、插件 import 它 ——
这正是「插件 → core 单向依赖」的标准形态，不构成任何额外阻力。

---

## 4. 收益与代价总账

### 收益（迁出能拿到什么）

| 收益 | 评价 |
|---|---|
| `app/apps/` 清空 → 内置发现通道退役 | 🟡 只剩 1 张卡时收益很小；`discover()` 对空目录只返回空列表、不告警（`manifest.py:148` 只在**目录不存在**时告警） |
| text_chat 独立发版节奏 | 🟡 真实收益，但 text_chat 与宿主耦合极低（只依赖 `app.core.*`），宿主改动频率低 |
| 卡片样式归位（`app.css` 自带） | 🟢 **真实且必要** —— 顺带修掉宿主 `framework.css` 泄漏卡片规则的边界问题 |
| 插件包统一管理 | 🔴 **不成立** —— 只有 1 张卡时，管理成本 > 收益 |

### 代价（迁出会付出什么）

| 代价 | 严重度 |
|---|---|
| **改宿主 CI**：`regression.yml:60` 的 `py_compile app/apps/text_chat/api.py` → 文件不存在 → **CI job 硬失败** | 🔴 必须改 |
| **`verify_thinking_router.py:25` import 断裂** | 🔴 必须改 |
| **`framework.css` 145 行 CSS 不搬 → 卡片全裸** | 🔴 最阴的坑 |
| 跨仓协同：改 text_chat 要开两个 PR（宿主 + 插件） | 🟡 可接受（其它 10 张卡已如此） |
| 插件包语义边界被打破（§3.2） | 🟡 设计债 |

---

## 5. 迁移工作清单（若决定做）

### 5.1 🔴 宿主侧必须改（5 处）

| # | 位置 | 动作 |
|---|---|---|
| 1 | `app/apps/text_chat/` | 整目录删除（→ 拷进插件）。`app/apps/` 保留空目录 + `__init__.py`（删目录会触发 `manifest.py:148` 的告警） |
| 2 | `app/static/core/framework.css:4287-4431` | **把 6 条 `#page-text-chat` 规则（145 行）搬进卡片的 `static/app.css`**。参考：`tts` 374 行 / `qwen3-asr` 536 行 / `usb-hid` 340 行 —— 插件卡都自带 CSS。⚠️ 本次更新**未改动**这 145 行，仍是宿主里唯一泄漏的卡片样式之一 |
| 3 | `.github/workflows/regression.yml:60` | 从 py_compile 列表里删掉 `app/apps/text_chat/api.py`（插件不在本仓 CI checkout 里 → 只能删）。顺带修掉第 56 行那句已过时的注释「The cards still living in-tree are text_chat / tts」—— **tts 早已外移** |
| 4 | `scripts/verify_thinking_router.py:25` | `from app.apps.text_chat.service import _ThinkingRouter` → 改为从插件命名空间 import，或**把这条回归迁进插件仓** |
| 5 | `app/system/static/system.js:1258-1263, 1289-1291` | 删掉死代码 `TextChat.init()` / `refresh()` / `ensureWs()` / `closeWs()`（`TextChat` 从来不是全局变量；`system.js` 的 `showPage()` 无调用者） |

### 5.2 🟡 文档同步（**本次新增了 3 处过时表述**）

| 位置 | 动作 |
|---|---|
| `AGENTS.md:1722` 卡片清单表 | `text-chat` 来源「内置」→ 插件包；后端入口 → `<pkg>.cards.text_chat` |
| `AGENTS.md:1713-1718` | 「7 张卡在插件包」→ 8 张 |
| `app/core/manifest.py:11-13` docstring | "After the v0.25 split this holds only ``text_chat``" → 无内置卡了 |
| `scripts/card_dirs.py:5-13` docstring | 列出插件包名的注释要加 text-chat 所在的包 |
| **`AGENTS.md:7378`** ★ | 「`loadPageData("text-chat")` 调 `TextChat.init() + refresh() + ensureWs()`」—— **过时**：该路径是死代码（`loadPageData` 唯一调用点在 `system.js:1313`，其所在的 `showPage()` 无调用者）。实际入口是卡片自己的 `app.js:661 TextChat.init()` |
| **`AGENTS.md:7383`** ★ | 「`swcap_text_chat` 现在**只有 qwen3.5-vl-4b 一个 provider**」—— **过时**：9B 也声明了它（`tools/worker_manifests/qwen3.5-vl-9b.json`） |
| **`app/apps/text_chat/static/app.js`** ★ | 「深度思考」整条前端链路已成死代码（见 §6.1）—— 迁移前应先清理，否则把死代码搬进插件仓 |

### 5.3 🟢 插件侧（7 处）

1. `cards/text_chat/` 整目录拷入
2. `manifest.json`：`"backend.module"` → `"wdnapps_audio_chat.cards.text_chat"`
3. `pyproject.toml`：entry point `"text-chat" = "wdnapps_audio_chat.cards.text_chat"`
4. `pyproject.toml`：`package-data` 加 `"wdnapps_audio_chat.cards.text_chat" = ["manifest.json", "static/*"]`
5. `cards/text_chat/__init__.py::register_shutdown`：`from app.apps.text_chat import service` → `from wdnapps_audio_chat.cards.text_chat import service`；
   **顺带删掉残留的 `"bot", "llm", "video_cache", "video_manager", "service_wechat"` 子模块探测** —— 那是 im_bridge 的遗留，text_chat 根本没有这些模块
6. `static/app.css`：填入从宿主搬来的 145 行
7. 插件版本 0.1.1 → 0.2.0；`pyproject.toml` description / `README*.md` / `__init__.py` docstring 三处自述改成「3 张卡」

### 5.4 ✅ 确认**不受影响**的部分（可放心）

- `app/static/index.html.template:624` `{% card_slot "text-chat" %}` —— **保留**
- `app/core/chat/{runner,agent,serial}.py` —— 留在宿主，插件 import 它（合法）
- **`app/core/thinking.py`** —— 留在宿主，插件 import 它（合法）
- `app/core/tools/registry.py::resolve_available_tool_names` —— 宿主 API，不动
- `app/system/im_bridge/*`（微信桥接）—— 它用的是**能力**不是卡片，不受影响
- `tools/worker_manifests/qwen3.5-vl-{4b,9b}.json` —— `swcap_text_chat` 的 provider 声明留在宿主
- `app/core/uploads.py` —— `uploads_dir` 从 manifest 读；text_chat 声明了但实际没用上传
- `app/core/translate/*` + `app/system/api/translate.py` —— **全新的宿主级功能，与卡片无关**（走外部 MT，不碰 `swcap_text_chat`）
- `scripts/smoke_wizard_all.py:14`、`scripts/verify_single_card_ui.py:366-391` —— 按**卡 id** 引用，插件卡照样命中
- `scripts/verify_apps_isolation.py` —— 走 `scripts/card_dirs.py` 双通道解析；text_chat 只 import `app.core.*` → 无跨卡违规
- `app/core/compute/workers/_base.py` —— worker 侧工具注册 + 思考总闸，与卡片位置无关

---

## 6. 本次更新新发现的 3 个问题

### 6.1 🔴 `app.js` 的「深度思考」链路已完全死掉，但代码还在

本次更新把思考开关**在三个层面同时关掉**了：

| 层 | 位置 | 状态 |
|---|---|---|
| 应用层总闸 | `app/core/thinking.py:42` `THINKING_ENABLED = False` | 关闭 |
| UI 控件 | `static/template.html` `<input type="checkbox" id="text-chat-thinking" disabled />` | 禁用 |
| 向导字段 | `static/wizard.json` `enableThinking` 字段 | **已删除** |

但 `app/apps/text_chat/static/app.js` **一行没改**（本次 diff 未触及），仍在：

```js
:34   const thinkEl = $("text-chat-thinking");
:37   thinkEl.checked = localStorage.getItem("text_chat_thinking") === "1";
:41   localStorage.setItem("text_chat_thinking", ...)
:396  try { return localStorage.getItem("text_chat_thinking") !== "1"; }
:539  const enabled = !!document.getElementById("text-chat-thinking")?.checked;
:680  const wantThinking = opts.enableThinking === true;      // 向导已不再传这个字段
:684  localStorage.setItem("text_chat_thinking", wantThinking ? "1" : "0");
```

`:680` 永远读到 `undefined` → `wantThinking` 恒为 `false`。`:539` 读的是
`disabled` 复选框（`.checked` 仍可被程序改写，但用户改不了）。

⇒ **整条 per-call 思考链路是死代码**。这与 `app/core/thinking.py` 的设计
意图一致（「应用层不指望每个调用点都记得传对标志，而是直接把闸拉下来」），
但**前端这一层没跟上**。

⇒ 迁移前应先清理，否则把一段死代码搬进插件仓（而且插件仓是公开发布的）。

### 6.2 🟡 `AGENTS.md` 里 text-chat 章节有 2 处已过时的表述

- `:7378` 说前端入口是 `loadPageData("text-chat")` —— 实际是死代码路径
- `:7383` 说 `swcap_text_chat` 只有 4b 一个 provider —— 9B 也声明了

两处都在「文本对话卡」专章（AGENTS.md:6999 起）里。

### 6.3 🟡 9B 的 `jinja2` 依赖链全在宿主侧，迁卡不迁它

本次 `requirements.txt` 新增 `jinja2>=3.1.0,<4`，原因是
`wdnai/backends/vlm_multicard.py` 在**模块级**写死 `from jinja2 import Environment`
—— 而 **Qwen3.5-VL-9B 正是 text_chat 的 `PREFERRED_MODELS[0]`**（`service.py:115`）。

这条链是：`宿主 requirements.txt` → `pip install --no-deps -e` 跳过依赖解析
→ `wdnai` 没声明 jinja2 → `scripts/check_sdk_deps.py` 也看不见（只审计**声明过**的依赖）。

⇒ 插件卡的 `pyproject.toml` **不该**声明 jinja2（它不是插件的依赖，是宿主/SDK 的）。
但这也说明：**text_chat 的旗舰模型路径横跨宿主 requirements + wdnai 后端 +
worker 清单三层**，迁走卡片**不会**迁走这条链。§3.4 的结论依然成立：LLM 大脑
是平台级资产，不属于某个模态插件。

---

## 7. 三个方案对比

### 方案 A（推荐）：新建 `wdnapps-chat` 插件包

| 项 | 评价 |
|---|---|
| 语义 | ✅ 文本→文本，自成一个模态插件，与生态的「按模态分包」一致 |
| 生态 | ✅ 补上「文本」这一格；将来任何纯文本卡（prompt 模板卡、翻译卡）有家可归 |
| 本次更新的额外利好 | ✅ 界面翻译（`app/core/translate/`）证明宿主还会继续长出**宿主级非卡片功能**；文本类需求在增加，独立的文本插件包有长期价值 |
| 代价 | 🟡 新建 git 仓（局域网 + GitHub 镜像 + Gitee 自动同步）+ `update_rkwhl.sh` **改 4 处**（224 行 case、558-564 行 run_pkg、576 行完成信息、3-4 行头注释）+ 新包要过 `check_plugin_entrypoints.py` |
| 风险 | 🟡 首次装机路径是新链路，要真机验一遍 |

### 方案 B：塞进 `wdnapps-audio-chat`

| 项 | 评价 |
|---|---|
| 语义 | ❌ 音频插件装文本卡，三处自述都要改 |
| 生态 | ❌ 破坏「按模态分包」惯例 |
| 代价 | 🟢 零基础设施改动，1~2 小时可完成 |
| 风险 | 🟢 机械风险低；🟡 设计债 |

### 方案 C：留在内置（维持现状）

| 项 | 评价 |
|---|---|
| 语义 | ✅ 正确 —— 宿主保留一张「旗舰」卡（9B 是双卡旗舰模型） |
| 代价 | 🟢 零改动 |
| 遗留问题 | 🟡 `framework.css` 145 行卡片 CSS 泄漏；`app.js` 死代码；AGENTS.md 2 处过时表述 |

---

## 8. 最终建议

**如果目标是「让 `app/apps/` 退役、内置通道清空」** → 走**方案 A**，新建
`wdnapps-chat`。理由：这件事的价值在于**架构收口**（卡片全部插件化、
宿主只留 core + system 页），而不在于 text_chat 这一张卡本身。既然要做，
就一次做对 —— 按模态分包，文本对话卡该有自己的包。

**如果目标只是「给 text_chat 独立的发版节奏」** → 走**方案 C**，留在内置。
text_chat 与宿主耦合极低（只依赖 `app.core.*`），拆出去省不了多少维护成本，
却要付出跨仓协同 + CI 改动的成本。

**不建议方案 B**。它把「能跑」当成了「合理」：省掉的是半小时，欠下的是
一个命名与语义长期不一致的插件包 —— 而插件包是**公开发布**的产物
（GitHub 镜像国际可见），README 里的「音频类插件包…这三张卡：语音生成、
流式语音识别、文本对话」会长期摆在外面。

**无论选哪个方案，都建议顺手做掉这三件事**（与迁移决策无关，是独立缺陷）：

1. 把 `framework.css` 的 145 行 `#page-text-chat` 规则搬回卡片自己的
   `app.css` —— 插件卡全部自带 CSS，text_chat 是仅有的两个例外之一
   （另一个是 `video-detection` / `audio_analysis`）。
2. 清理 `app/apps/text_chat/static/app.js` 里已死的「深度思考」链路
   （`:34-41` / `:396` / `:539` / `:680-684`）—— 三个层面的开关都已关闭。
3. 删掉 `system.js` 里那 4 行引用不存在全局 `TextChat` 的死代码，
   并修正 `AGENTS.md:7378` 对它的描述。

---

## 附：本次评估用到的检索命令

```bash
# 本次更新了什么
git log --oneline -8
git diff --stat 4175400..HEAD
git diff 4175400..HEAD -- app/apps/text_chat/

# 插件包有无变动
git -C .venv/src/wdnapps-audio-chat log --oneline -5
git -C .venv/src/wdnapps-audio-chat status --short

# 宿主对插件卡的通用支持
grep -n "card_slot\|no {{card_slot}}" app/core/html_assembly.py
grep -n "entry_point\|_ENTRY_POINT_GROUP" app/core/manifest.py
grep -n -A 10 "Mount per-card static" app/main.py

# 卡片清单与插件化设计
sed -n '1698,1800p' AGENTS.md

# CSS 归属统计
for c in text-chat tts qwen3-asr media-generator image-editor usb-hid video-detection; do
  grep -c "#page-$c" app/static/core/framework.css; done

# 思考总闸的四个执行点
grep -rn "effective_thinking\|THINKING_ENABLED" app/

# 新建插件包的流水线改动点
grep -n "wdnapps_" update_rkwhl.sh
```

**未验证项**（需真机 / 运行环境）：

- 迁移后真机 boot 是否仍能发现并挂载 `text-chat` 卡（未实际执行迁移）；
- `pip install -e` 重装插件后 `check_plugin_entrypoints.py` 的实际输出；
- `qwen3.5-vl-9b` 双卡模型在插件卡路径下的端到端对话与 NPU 释放时序
  （本次新增 `recover_wedged_instance` 自愈路径后的行为）；
- `app/core/thinking.py` 总闸关闭后，9B 非思考模式「复读 JSON Schema 外壳」
  （AGENTS.md:7174 记录的实测问题）是否仍复现。
