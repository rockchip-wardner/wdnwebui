# 同声传译功能卡 —— 方案设计

> 状态：方案稿（未实施）
> 目标板：RK1828 × 3 协卡（`0001:11:00.0` / `0003:31:00.0` / `0004:41:00.0`，各 5120 MB）
> 依据：本仓库实测数据 + 现场 `/api/compute` 探针（2026-11-18）

---

## 0. 结论先行

**新增一张 `live-interpreter` 卡，把仓库里已有的三颗模型串成一条流水线：**

```
Qwen3-ASR（听）→ HY-MT1.5（翻）→ Qwen3-TTS（说，可选）
```

核心产出是**目标语言字幕**（原文 / 译文双语对照），语音播报作为可关的开关。

- **不新增模型**、**不新增 worker 入口**、**不改 SDK**、**不碰 `compute_worker.py`**
- 三张卡（`qwen3-asr` / `web-translate` / `tts`）的模型**全部已装在 `/userdata/models/`**，能力齐备
- 新增代码量集中在两处：**一段编排逻辑**（宿主 `app/core/interpret/`）+ **一张薄壳卡**（插件 `wdnapps-audio-chat/cards/live_interpreter/`）

**一句话的取舍：** 同传的产品价值在**字幕**（准确、可读、可导出），不在**语音**（延迟高、串行、会啸叫）。所以本方案把 TTS 做成**可选项**，让它当"加分项"而不是"必经环节"。

---

## 1. 现状盘点 —— 能复用什么

### 1.1 三颗模型（全部已安装）

| 环节 | model_id | capability | 显存估算 | 初始化 | 现有卡片 | worker manifest |
|---|---|---|---|---|---|---|
| ASR | `qwen3-asr` | `swcap_audio_asr` | 1600 MB | 25 s | `qwen3-asr` 卡 | `tools/worker_manifests/qwen3-asr.json` |
| MT | `hy-mt-1-5` | `swcap_translate` | 2200 MB（**同卡最多 2 份**） | 30 s | `web-translate` 卡 | `tools/worker_manifests/hy-mt-1-5.json` |
| TTS | `qwen3-tts` | `swcap_text_to_speech` | 1900 MB | 30 s | `tts` 卡 | `tools/worker_manifests/qwen3-tts.json` |

### 1.2 可直接复用的宿主模块

| 能力 | 位置 | 说明 |
|---|---|---|
| 取模型会话 | `app/core/compute/registry.py:5157` `get_session(capability, npu_index=None)` | **本机优先**；找不到就抛 `RuntimeError("功能未启用: <cap>")` |
| 翻译内核 | `app/core/translate/npu_provider.py:447` `NpuTranslateProvider` | 单句 0.11~0.50 s；语言码表 `:159`；按语言定制的措辞顺序 `:335` |
| 翻译服务层 | `app/core/translate/service.py:147` `translate_segments` | 批 + 缓存 + 失败隔离（**同步批接口，同传用不上**） |
| 端点检测 | `app/core/audio/endpoint.py:160` `EndpointConfig` / `:213` `EndpointDetector` | 静音 1200 ms 触发端点、`min_speech_sec=0.3` |
| PCM 缓冲 | `app/core/audio/capture.py:270` `PcmFeedBuffer` | 留住最后 1 个采样，收尾必带得上音频 |
| 采集 | `app/core/audio/alsa.py:193` `AlsaCapture` / `app/core/audio/source.py:476` `make_audio_source` | 板载麦 / 文件 / **RTSP** 都现成 |
| TTS 请求模型 | `app/core/audio/tts_bridge.py:51` `TTSRequest` | 含 `language` / `speaker` / `play_to` |
| 卡片装配 | `app/core/html_assembly.py:41` `assemble_index_html` | `{% card_slot "<id>" %}` 注入 |

### 1.3 可直接复用的插件资产（`wdnapps-audio-chat`）

| 资产 | 位置 | 复用价值 |
|---|---|---|
| 实时 ASR 会话 | `cards/qwen3_asr/service.py:1351` `_LiveAsrSession` | **1 s 一块增量解码 + 端点切句 + committed/unfixed 双态** —— 同传最核心的一块 |
| 实时帧协议 | 同上 `:1752` `_on_delta` / `:1820` `endpoint` 帧 | `round`（committed+unfixed+language+时间戳）/ `endpoint`（一句完成）/ `gap`（丢块告警） |
| 板载麦采集 | `cards/qwen3_asr/board_mic.py:137` `BoardMicController` | 独立进程采 ALSA（`snd_pcm_readi` 是阻塞 I/O，不能进 webui 进程） |
| 浏览器采音 | `cards/qwen3_asr/static/capture-worklet.js` | AudioWorklet → PCM 帧 → `/live/chunk` |
| TTS 音频流 | `cards/tts/api.py:798` WS `subscribe_audio` | 二进制帧（12 字节头 + float32 PCM），**首包即可播** |
| TTS 音色/语种表 | `cards/tts/service.py:1224` `voice_catalog` | 前端下拉数据源 |
| 翻译模型启停 | `cards/web_translate/service.py:240` `WebTranslateManager` | 卡位冲突检查、槽位规划、多实例起停的现成逻辑 |

### 1.4 三条"硬约束"（来自 AGENTS.md + 插件约定，方案必须遵守）

1. **卡片只准 import `app.core.*` 与 SDK，禁止跨卡 import**
   —— `cards/qwen3_asr/__init__.py` 的 docstring 明写 *"No cross-card imports."*
   ⇒ 同传卡**不能** `import wdnapps_audio_chat.cards.qwen3_asr.service`。共享逻辑必须落在**宿主** `app/core/`。
2. **LLMSession 只能从 `ComputeRegistry` 取**，业务模块不得自建（AGENTS 设计原则 4）。
3. **板端没有 emoji / 符号字体** —— 模板里只能用内联 SVG 或 `× ● ■ ▶` 这类字符。

---

## 2. 总体架构

```
        ┌───────────── 音频源（4 选 1 + RTSP）─────────────┐
        │  板载麦(ALSA 子进程) │ 浏览器麦(Worklet) │ 文件/视频 │ RTSP │
        └───────────────────────┬──────────────────────────┘
                                │ PCM 16k 单声道
                    ┌───────────▼────────────┐
                    │ ① ASR 段  Qwen3-ASR   │  swcap_audio_asr
                    │  1s 一块增量 + 端点切句 │  （ASR 卡同款会话）
                    └───────────┬────────────┘
                                │ endpoint 帧：一句完成（含语种）
                    ┌───────────▼────────────┐
                    │ ② 翻译段  HY-MT1.5    │  swcap_translate
                    │  单次措辞 · 低延迟通道   │  （NpuTranslateProvider）
                    └───────────┬────────────┘
                                │ 目标语言文本
                    ┌───────────▼────────────┐
                    │ ③ 字幕聚合（宿主新模块）  │  → WS → 浏览器
                    │  Segment 列表 + 状态机   │     双语对照 + SRT/VTT 导出
                    └───────────┬────────────┘
                                │ （可选）
                    ┌───────────▼────────────┐
                    │ ④ 播报段  Qwen3-TTS    │  swcap_text_to_speech
                    │  单 session 串行排队     │  → 板载喇叭 / 浏览器
                    └────────────────────────┘
```

**每段独立可降级**（这是本方案的核心鲁棒性设计）：

| 段 | 缺失时 | 表现 |
|---|---|---|
| ① ASR | 整卡不可用 | 报"功能未启用: swcap_audio_asr"（502），不白屏 |
| ② MT | 只出**原文**字幕 | 字幕区标"未翻译"，其余功能不受影响 |
| ③ 字幕 | —— | 始终存在（纯文本拼接） |
| ④ TTS | 只出字幕 | 播报开关置灰，提示"语音播报未启用" |

---

## 3. 关键设计决策

### D1 —— 翻译单元 = **ASR 端点（句级）**，不做逐块增量翻译

**依据**：实时 ASR 的端点检测（`app/core/audio/endpoint.py:160`，静音 1200 ms）已经在做"句子切分"，且 `endpoint` 帧直接给出**完整一句 + 语种**（`cards/qwen3_asr/service.py:1820`）。

**为什么不逐块翻**：`round` 帧里 `unfixed` 是**还会改的假设文本**，翻它等于翻一句可能会变的话 → 字幕反复重写、抖动，且每块都要烧一次 MT 推理（一句通常 3~5 块 → 3~5 倍推理量）。

**延迟账**（句级）：
```
端点静音门限            1.2 s   （可降到 0.6 s）
ASR 末块收尾            0.3~1.0 s
MT 单句                 0.11~0.50 s   （实测，见 npu_provider.py:12）
─────────────────────────────────────
字幕可见延迟            ≈ 1.6 ~ 2.7 s
```
会议同传的商业可接受线通常是 2~5 s ⇒ **达标**。

**代价**：一句说完后要等 1.2 s 静音才出译文 ⇒ "抢话"场景体感偏慢。
**缓解**：端点门限做成可调（0.6 / 1.2 / 2.0 s 三档），并提供**字数强制触发**（当前句超过 N 字符且距上次翻译 > 1.5 s 就先翻一版，标为"预览"）。

### D2 —— 翻译走**低延迟通道**：**混搭措辞 + 1 条兜底措辞**（最多 2 次推理）

**依据**：现有 `NpuTranslateProvider._translate_one`（`npu_provider.py:685`）会按 `PROMPT_ORDER`（`:335`）**最多试 5 条措辞**，每条都是一次完整推理。界面翻译里这是优点（不漏译），同传里这是**致命的**（5 × 0.5 s = 2.5 s，字幕会滞后一句话）。

**方案**：在 `npu_provider.py` 增加一个**低延迟入口**（与界面翻译共用同一个 provider 实例与同一套语言码表，但只试 1~2 条措辞）：

```python
def translate_one_fast(self, text: str, source: str, target: str) -> tuple[str, dict]:
    """低延迟通道 · 供流式场景使用。

    第 1 条：**混搭措辞**（源用语言码、目标用语言名）—— 实测两个方向都全中。
    第 2 条：**兜底**（PROMPT_ORDER[target][0]，写法与第 1 条不同）。
    两条都失败 ⇒ 返回原文 + failed 标记，由上层降级。
    """
```

#### 为什么是「混搭措辞」：实测数据（2026-11-18，HY-MT1.5-1.8B 贪心）

10 句英文 + 10 句中文，「源/目标用名还是用码」的四种组合：

| 写法 | en→zh | zh→en |
|---|---|---|
| 源=名 目标=名 | 10/10 | 6/10 |
| 源=名 目标=码 | 0/10 | 2/10 |
| **源=码 目标=名** | **10/10** | **10/10** |
| 源=码 目标=码 | 4/10 | 10/10 |

**只有「源=码、目标=名」在两个方向都全中**。横扫验证：en→{zh,ja,ko,fr,de,es,it,ru,ar,th,hi} 全 10/10；zh→{en,ja,fr,de} 全 10/10。

**失败的样子永远是「复读原文」** —— 模型没听懂指令，就照着原文写一遍（还把 `'` 写成 `'`）。所以「没翻出来」= 指令没被认出来，**换一种写法往往就中了**：这正是留第 2 条兜底措辞的理由（实测同一句口语长句，第 1 条复读、第 2 条就翻对）。

⚠️ **只改流式通道**：`PROMPT_ORDER` 是 2026-11-16 为**界面翻译**逐语言实测出来的（24 条界面文案 × 5 条措辞），改成混搭要**整套重测**，不在本期范围 ⇒ 界面翻译（`_translate_one`）**一行未动**。

**代价**：延迟上界从 1 次推理（~0.6 s）涨到 2 次（~1 s），但只在第 1 条失败时才发生；换来的是「整句作废」几乎不再出现。

### D3 —— 字幕模型 = **Segment 列表 + 状态机**（不是简单字符串追加）

每句一个 Segment：

```python
@dataclass
class SubtitleSegment:
    id: int                 # 单调递增
    source_text: str        # 源语言（ASR 原文）
    target_text: str        # 目标语言（译文，可能为空）
    lang_in: str            # ASR 自动检测的语种（"zh"/"en"/…）
    lang_out: str           # 目标语种
    t_start: float          # 音频时间轴起点（秒）
    t_end: float            # 终点
    status: str             # pending | translating | done | failed | skipped
    preview: bool           # 是否为"预览译文"（非定稿）
```

**状态机**（前端据此渲染）：
```
pending ──► translating ──► done
   │             │
   │             └─────────► failed（降级显示原文）
   └───────────────────────► skipped（源语==目标语，透传）
```

**为什么必须有时间轴**：① 会议字幕要能对齐音视频；② **SRT/VTT 导出**是刚需（会后纪要）；③ 迟到/乱序的翻译帧要能按 id 定位覆盖，而不是 append。

### D4 —— 三段解耦、**每段独立线程 + 独立队列**

- ASR 线程 → 端点句 → **翻译队列**（有界，丢最旧）→ 翻译线程池 → 字幕队列 → WS 广播
- 翻译**不能阻塞** ASR：一句翻译要 0.5 s，ASR 每秒来一块，串行必然积压
- 有界队列（默认 32 句）+ **满时丢最旧并计数**（同传场景下"丢掉 10 秒前的翻译"比"延迟 10 秒"可接受）

**并发单位是"实例"不是"线程"**：`LLMRPCSession` 的 RPC 执行器是 `ThreadPoolExecutor(max_workers=1)`（`npu_provider.py:524-534` 实测）⇒ **同一个 session 同时只能一个推理在飞**。所以翻译并发 = **MT 实例数**（worker 进程数），不是线程数。
⇒ 翻译线程池里的每个 worker 必须**各自绑定一个 MT session**（`registry.get_session("swcap_translate", npu_index=卡号)`），并在**实例级**做串行。

### D5 —— 卡位规划（3 卡 × 5120 MB）

MT 一份 2200 MB、ASR 1600 MB、TTS 1900 MB。

| 卡 | 放什么 | 占用 | 余量 | 作用 |
|---|---|---|---|---|
| 卡 0 | ASR ×1 + MT ×1 | 3800 MB | 1320 MB | 听 + 翻（主） |
| 卡 1 | MT ×1 + TTS ×1 | 4100 MB | 1020 MB | 并行翻第 2 句 + 播报 |
| 卡 2 | 空 / 留给别的卡 | — | — | 弹性 |

**依据**：同卡加第 2 份只 +21%（抢同一张卡的算力），加到第二张卡直接 +85%（`web_translate/service.py:48-58` 实测）⇒ **跨卡分配优先于同卡堆叠**。

**注意 `max_replicas_per_card`**：只有 `hy-mt-1-5` 是 2，其余模型是 1 ⇒ ASR/TTS 同卡开 2 份**直接不允许**（显存也不够）。

**✅ 前置修复（M0）已完成（2026-11-18）**：`registry.snapshot()` 的 `per_card_memory_mb` **把 peer 的显存记到了本板**（`registry.py` 没过滤 `_source == "adopted"`）。现场实测：

```
修复前 GET /api/compute → per_card_memory_mb: {"0": 3000}
而 instances 里那条 DepthAnythingV3 的 _source = "adopted"（别的板子）
⇒ 本板 3 张卡其实全空，却显示卡 0 被占 3000 MB

修复后 GET /api/compute → per_card_memory_mb: {}   total_memory_mb: 0
                        remote_memory_mb: 3000   （peer 的数字单独记，不占本板卡）
```

⇒ `registry.py::_local_memory_usage()`：只算本机实例（`spawned` / `adopted_local`），
peer 的显存单独进新字段 `remote_memory_mb`；多卡实例（9B / qwenimage21）的
显存**平摊**到它占的每一张卡上。
回归：`scripts/verify_registry_memory_accounting.py`（31 条断言，含反向验证）。

`web_translate/service.py:173-192` 的注释里已经点名了这个坑（"那边要一起修"），**至今没修**。同传卡的槽位规划**必须绕开它**（自算本机占用，参照 `_local_card_occupancy`），否则会误判"卡 0 放不下"。

### D6 —— TTS 播报：**串行排队 + 强制耳机提示**（**第一期不做**，2026-11-18 拍板）

> **2026-11-18 用户拍板**：TTS 语音播报**不进第一期**，先把「实时路径的文字
> 译文」这条链路跑稳。本节留作后续（第二期或第三期）的设计输入。

**依据**：`TtsSession` 是**单 session**，`start()` 有 busy 态（`cards/tts/service.py:322`）⇒ 同传要一句接一句排队，队列深度建议 ≤ 3（排太久用户已经忘了在等什么）。

**两个必须处理的物理问题**：
1. **回声/啸叫**：麦克风采音 + 板载喇叭放译文 = 啸叫回路。⇒ UI 必须提示"语音播报请戴耳机"，并默认**关闭**播报（字幕默认开）。
2. **播报延迟**（TTS 首包）**未知**，需要实测（见 M1 的验收项）。若首包 > 1.5 s，播报就只适合"会后成稿"而不是"同步传译"。

### D7 —— 目标语言下拉 = **HY-MT ∩ Qwen3-TTS 的交集**

- HY-MT1.5 支持 **36 种语言**（`npu_provider.py:159` `LANG_CODES`，含 `zh-TW` 别名共 37 条）
- Qwen3-TTS 只支持 **10 种 + 2 方言**（`wdnai/backends/tts.py:180` `CODEC_LANGUAGE_ID`：chinese / english / german / italian / portuguese / spanish / japanese / korean / french / russian + beijing_dialect / sichuan_dialect）

⇒ **只要字幕**（不播报）：可用 36 种语言。
⇒ **要播报**：只有 **10 种**能用（zh-CN/en/fr/de/it/pt/es/ja/ko/ru）。
⇒ UI 应给**两个下拉**：目标语言（36 种）+ 播报音色（仅当目标语言落在 10 种交集内才可选）。

### D8 —— 共享逻辑放**宿主 `app/core/interpret/`**，卡片只做壳

插件约定禁止跨卡 import（§1.4），而同传需要 ASR 的**实时会话**能力 ⇒ 两条路：

| 方案 | 做法 | 风险 | 评价 |
|---|---|---|---|
| **A** | 把 `_LiveAsrSession` / `_LiveAsrManager` 从插件**搬到宿主** `app/core/asr/live.py`，ASR 卡改为 import 宿主 | 动了已上线、已验收的 ASR 卡 | ✅ **推荐**（单一实现，长期正确） |
| B | 同传卡自己再写一份 live 会话 | 两份近似副本，正是本仓 AGENTS 明令要消灭的东西 | ❌ |
| C | 同传卡 import ASR 卡模块 | 违反插件约定 | ❌ |

**推荐 A，但分两步走**（M1 先落地宿主模块 + 同卡 PoC，M4 再把 ASR 卡切过来），避免一次性动大。

### D9 —— 源语 == 目标语时**透传**，不翻

ASR 自动检测语种（`round` 帧的 `language` 字段）已可用。若 `lang_in == lang_out` ⇒ 直接把原文当译文（`status=skipped`），省一次推理，字幕也更自然（不引入翻译腔）。

### D10 —— 停止语义 & DemoGuard

沿用 ASR 卡的既有语义（`cards/qwen3_asr/service.py:452` `stop(reason)`）：
- 用户点"停止" ⇒ 状态落 **`stopped`**，**不是 `error`**（取消会让在飞的 RPC 抛 `generation stopped`，落成 error 会让用户以为坏了）
- 停止时**保留已产出的字幕**
- 卡片 `manifest.json` 的 `demo_guard` 要登记 `/api/live-interpreter/stop`（+ `/live/stop`），离开页面时停掉后台（AGENTS 设计原则 5）

---

## 4. 接口设计

### 4.1 HTTP

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/live-interpreter/` | 状态快照（各段是否就绪、当前源、目标语、统计） |
| GET | `/api/live-interpreter/capabilities` | 三个 capability 的就绪情况（前端据此置灰按钮） |
| POST | `/api/live-interpreter/start` | 开始（body：`source`/`device`/`target_lang`/`speak`/`silence_ms`） |
| POST | `/api/live-interpreter/stop` | 停止（保留字幕） |
| POST | `/api/live-interpreter/clear` | 清空字幕（**不**清模型） |
| GET | `/api/live-interpreter/subtitles` | 全量字幕（刷新页面后恢复） |
| GET | `/api/live-interpreter/export?format=srt\|vtt` | 导出字幕文件 |
| GET | `/api/live-interpreter/languages` | 目标语言表（36 种）+ 标记哪些能播报 |
| POST | `/api/live-interpreter/source/chunk` | 浏览器麦 PCM 分片（与 ASR 卡同款） |
| WS | `/api/live-interpreter/ws` | 实时帧（字幕 / 状态 / 统计 / 丢块告警） |

### 4.2 WebSocket 帧

```jsonc
// ① 新的源语句（ASR 端点到达，翻译还没回来）
{"type":"segment", "segment":{
   "id": 42, "source_text":"...", "lang_in":"zh",
   "target_text":"", "status":"translating",
   "t_start": 12.3, "t_end": 14.1 }}

// ② 译文回填（按 id 覆盖，不 append）
{"type":"segment_update", "segment":{
   "id": 42, "target_text":"...", "status":"done",
   "latency_ms": 187 }}

// ③ 翻译失败 → 降级显示原文
{"type":"segment_update", "segment":{
   "id": 42, "target_text":"...", "status":"failed",
   "reason":"NPU 这条没翻出来（已降级显示原文）"}}

// ④ 状态 / 统计 / 告警
{"type":"state",   "state":{ ... }}
{"type":"stats",   "segments": 42, "dropped": 0, "avg_latency_ms": 210,
                "asr_rt": 0.42, "mt_rt": 0.18}
{"type":"gap",     "detail":"有音频丢失，识别结果可能有缺口"}
```

**为什么按 id 覆盖而不是追加**：翻译是**乱序完成**的（多实例并行），append 会让字幕顺序乱掉。

---

## 5. 前端

### 5.1 布局

```
┌───────────────────────────────────────────────────────────┐
│  同声传译            [源: 板载麦▾] [目标: English▾] [●播报]  │
├───────────────────────────┬───────────────────────────────┤
│  原文（源语言）            │  译文（目标语言）              │
│                           │                               │
│  …会议开始的时候…          │  …when the meeting begins…     │  ← 最新一句高亮
│  第二个议题是预算。        │  The second topic is budget.  │
│                           │                               │
├───────────────────────────┴───────────────────────────────┤
│  [开始] [停止] [清空] [导出SRT]   状态：运行中 · 延迟 0.21s │
└───────────────────────────────────────────────────────────┘
```

### 5.2 复用与注意

- **样式**沿用 `app/static/core/framework.css` 的卡片类名（`vd-btn` / `tts-select` / `asr-block`）
- **图标一律内联 SVG**（板端无 emoji 字体，AGENTS 明令）
- **DemoGuard**：卡片 JS 要 `DemoGuard.report("live-interpreter", {session:{busy:true}})`，并在 `manifest.json` 的 `demo_guard.stop_endpoints` 登记停止端点
- **`_loadedApps` / `_wizardMounted` 两个集合解耦**（AGENTS 设计原则 5 后半段）—— 新卡片的向导要遵守，别让 `wizard:unmounted` 删 `_loadedApps`
- **卡位占位符**：`app/static/index.html.template` 必须加一行
  `{% card_slot "live-interpreter" %}`（漏了不报错，只会**静默白屏** —— AGENTS 反复强调的坑）

### 5.3 向导（`static/wizard.json`）

```jsonc
{
  "enabled": true,
  "wizard_title": "同声传译快速配置",
  "swcap_to_model": {
    "swcap_audio_asr":       { "model_id": "qwen3-asr",   "npu_index": [0] },
    "swcap_translate":       { "model_id": "hy-mt-1-5",   "npu_index": [0, 1],
                              "instance_slots": [ {"npu_index":0,"replica":0},
                                                 {"npu_index":1,"replica":0} ] },
    "swcap_text_to_speech":  { "model_id": "qwen3-tts",   "npu_index": [1] }
  },
  "steps": [
    { "id":"env",    "title":"运行环境检查", "kind":"env_check" },
    { "id":"form",   "title":"传译设置",     "kind":"form",
      "fields":[
        {"id":"li-target","ui_key":"target_lang","label":"目标语言","type":"select","required":true},
        {"id":"li-speak", "ui_key":"speak","label":"语音播报","type":"checkbox"},
        {"id":"li-silence","ui_key":"silence_ms","label":"断句灵敏度","type":"select",
         "options":[{"value":"600","label":"快（600ms）"},
                    {"value":"1200","label":"标准（1200ms）","selected":true},
                    {"value":"2000","label":"稳（2000ms）"}]}
      ]},
    { "id":"finish", "title":"完成配置",     "kind":"finish" }
  ]
}
```

⚠️ 三个 capability 一起出现在 `swcap_to_model` 里时，**向导的实例槽位钳制**（`scripts/verify_wizard_replica_clamp.py`）要一起验证 —— 三个模型按 D5 的方案占 2 张卡，向导默认会按"每个 capability 都想要 1 份"来算。

---

## 6. 实施路线

| 里程碑 | 内容 | 验收 |
|---|---|---|
| **M0** | ✅ **已完成（2026-11-18）** —— 修 `registry.snapshot()` 的 `per_card_memory_mb` peer 污染（新增 `_local_memory_usage()` + `remote_memory_mb`） | ✅ `GET /api/compute` 返回 `per_card_memory_mb: {}` / `remote_memory_mb: 3000`；`scripts/verify_registry_memory_accounting.py` 31 条断言全过 |
| **M1** | 宿主 `app/core/interpret/`：Segment 模型 + 状态机 + 低延迟翻译入口（`npu_provider.translate_one_fast`）+ 翻译线程池（实例级串行） | 单测：喂 10 句 → 断言 10 条译文、顺序按 id、延迟统计正确；**实测 TTS 首包延迟** |
| **M2** | 宿主 `app/core/asr/live.py`：把 `_LiveAsrSession` / `_LiveAsrManager` 搬过来（从插件 → 宿主） | ASR 卡仍全部功能正常（`scripts/verify_*` 回归）；新模块被同传卡复用 |
| **M3** | 插件卡骨架：`wdnapps_audio_chat/cards/live_interpreter/`（manifest / api / service / static）+ `pyproject.toml` 入口点 + **卡位占位符** | `python -m scripts.verify_card_slots` 过；`python -m scripts.smoke_wizard_all` 过 |
| **M4** | 前端：WS 订阅 + 双语字幕区 + 状态/统计 + SRT 导出 | 真实浏览器验收（参考 `scripts/verify_image_editor_ui`）：起同传 → 注入音频 → 断言目标语言字幕出现且正确 |
| **M5** | TTS 播报（可开关）+ 队列 + 耳机提示 | 播报开关关掉时零影响；开时音频按句播放、不重叠 |
| **M6** | ASR 卡切到宿主模块（删插件里的副本） | ASR 卡回归全过；`grep` 确认插件里不再有第二份 live 会话实现 |

**M1 的实测项（不能拍脑袋）**：
1. Qwen3-TTS 首包延迟（决定播报能不能叫"同步"）
2. MT 在"会议长句"（> 40 字）上的真实延迟（界面文案是短句，同传是长句）
3. 长句是否会被 HY-MT 截断（`max_new_tokens=512`，`npu_provider.py:927`）

---

## 7. 风险与已知坑

| # | 坑 | 依据 | 处置 |
|---|---|---|---|
| 1 | ~~**`per_card_memory_mb` 把 peer 记到本板**~~ | 现场实测 `{"0":3000}` 而该实例 `_source=adopted` | ✅ **M0 已修**（`remote_memory_mb` 单独记 peer，不占本板卡）；同传卡仍建议自算本机占用 |
| 2 | **跨卡 import 被禁** | 插件 docstring 明写 | 共享逻辑进宿主 `app/core/` |
| 3 | **同一 session 只能一个推理在飞** | `LLMRPCSession` 单线程执行器 | 并发单位 = 实例；翻译线程各自绑一个 MT session |
| 4 | **MT 会 5 次重试** | `npu_provider.py:685` | 新增 `translate_one_fast`（单次措辞） |
| 5 | **ASR 的 `_ASR_RPC_LOCK` 是进程级** | `cards/qwen3_asr/service.py:221` | 同传卡与 ASR 卡**不能同时**跑实时识别（会互相等锁）⇒ UI 要提示 |
| 6 | **TTS 串行 + 麦克风啸叫** | `TtsSession` busy 态 | 播报默认关 + 队列 ≤ 3 + 强制耳机提示 |
| 7 | **板端无 emoji 字体** | AGENTS 明令 | 只用内联 SVG |
| 8 | **漏卡位占位符 = 静默白屏** | `html_assembly.py:65` 只打 WARNING | M3 用 `verify_card_slots` 卡住 |
| 9 | **MT 翻不出来（复读原文）** | `npu_provider.py` 质量校验一节的实测 | 降级显示原文 + 标"未翻译"，不重试 |
| 10 | **长句延迟/截断** | 未知，需实测 | M1 实测；必要时按标点切分后再翻 |
| 11 | **HY-MT 模板不是 ChatML** | `tools/worker_manifests/hy-mt-1-5.json` 的 `notes` | 复用 provider 的提示词构造，**不要**自己拼 ChatML |
| 12 | **繁简同码** | `npu_provider.py:77` | 目标语言列表里不给 `zh-TW` |

---

## 8. 备选方案对比（"有没有更好的实现方式"）

| 方案 | 描述 | 优点 | 缺点 | 结论 |
|---|---|---|---|---|
| **A. 三段串联（本方案）** | ASR → HY-MT → TTS，全用现成模型 | 零新增模型；字幕质量有 MT 专用模型兜底；延迟可控 | 三段各自要占显存（2 张卡）；翻译有延迟 | ✅ **推荐** |
| B. **端到端语音翻译模型** | 一个模型直接出译文语音 | 延迟最低、语调自然 | 板子上**没有**这种 RKNN 权重；要新导出（周期长、风险高）；字幕只能靠附带 ASR | ⏳ 长期方向，**本期不做** |
| C. 用 `qwen2.5-omni-3b`（`swcap_audio_chat`）翻译 | 拿 Omni 当翻译器 | 一颗模型搞定 | Omni 那条通路是"音频理解问答"，**不是翻译**；质量/延迟都更差；与 HY-MT 重复 | ❌ |
| D. 用本地 VLM（`qwen3.5-vl-4b`）翻译 | 文本进文本出 | 通用 | 3500 MB、init 60 s、生成速度远低于专用 MT；质量不如 HY-MT | ❌ |
| E. **接外网翻译 API** | 走 MyMemory/有道等 | 不占 NPU | 国内网络抽风、限流、延迟 1~3 s（AGENTS 明确记录三家都不稳） | ⚠️ 仅作 **MT 失败时的兜底**（现有 provider 链已具备） |
| F. 在 ASR 卡上加"翻译"开关 | 不新增卡 | 改动小 | ASR 卡语义膨胀；两卡共享一个 live 会话（`_ASR_RPC_LOCK`）⇒ 同传跑到一半切回 ASR 卡会打架 | ❌ 但可作 **PoC 形态**（1 天验证链路） |
| G. 新增卡 + ASR 卡切宿主共享模块（A 方案的完整形态） | 见 D8 | 单一实现、职责清晰 | 要动 ASR 卡（需回归） | ✅ **终态** |

**结论**：本期做 **A+G**，PoC 阶段可先用 **F**（快，但只作为一次性验证，不落仓）。

---

## 9. 需要拍板的三件事（**2026-11-18 已全部拍板**）

1. **播报（④）是否进本期**？ → **不进**（用户拍板）。本期只交付**双语字幕**；
   TTS 语音播报留到后续批次（设计见 §D6）。
2. **翻译失败时的降级策略** → **显示原文并标「未翻译」**（已实现，见 §11.3
   的降级表）。不回退外网 API（实时路径上外网延迟不可控）。
3. **目标语言范围** → **36 种全给**（只出字幕，不播报 ⇒ 没有交集限制）。
   实测：en→11 种目标语言、zh→4 种目标语言，混搭措辞全部 10/10。

---

## 10. 附：现场实测基线（2026-11-18）

```
本机 EP 协卡：3 张（0001:11:00.0 / 0003:31:00.0 / 0004:41:00.0），各 5120 MB
已装模型：DepthAnythingV3 / qwen3-vl-embedding-2b（均 peer，adopted）
本机 worker：无 ⇒ 3 张卡全空
per_card_memory_mb：{}   ← ✅ M0 修复后（peer 的 3000MB 单独记在 remote_memory_mb）

MT 实测（npu_provider.py:12, 48-58）
  单句 0.11~0.50 s；1 实例 24 条 3.30 s；4 实例 1.45 s（2.31×）
  加速的单位是"卡"不是"实例"：同卡加一份只 +21%，跨卡 +85%
  端到端 POST /api/translate：1 实例 5.52 s / 4 实例 2.78 s

ASR 端点参数（app/core/audio/endpoint.py:160）
  静音门限 -45 dBFS、端点 1200 ms、最小语音 0.3 s、纯静音超时 30 s
```

---

## 11. 方案修订（2026-11-18 决策）：**合并进「流式语音识别」卡**

> 本节是对 §0~§6 的**修订**。§1~§5 的盘点、延迟账、卡位账、协议设计
> **全部继续有效**；变的是**落地形态**：不新开 `live-interpreter` 卡，
> 而是把「实时翻译字幕」做成 **ASR 卡的一个可选功能**。

### 11.1 为什么改：实时 ASR 流是**单例**的

`_LiveAsrManager` 看似允许多个会话（`cards/qwen3_asr/service.py:1969`），
但每个会话的 `select_session()` 取的都是 registry 里**同一个** ASR
worker 的 session（`service.py:1449-1460`）：

```python
handles = registry.list_sessions(self.CAPABILITY, include_remote=True)
chosen = handles[0]
self._session = chosen.session      # ← 所有 live 会话共用一个增量解码器
```

⇒ **两张卡各开一条实时流会把同一个解码器搅在一起**。独立同传卡要么
跨卡 HTTP 调 ASR 卡的 `/live/*`（违反插件「禁止跨卡依赖」约定），要么
自己开一条（搅在一起）。合并则：翻译挂在 ASR **已有**的 `endpoint` 帧上，
**零新增音频通道**。

| 维度 | 合并进 ASR 卡 | 独立 `live-interpreter` 卡 |
|---|---|---|
| 实时 ASR 流 | 复用，零新增 | 跨卡 HTTP 或自开一条（搅在一起）|
| WebSocket | **1 条**（live 帧统一走 `broadcast()`，`service.py:1951`）| 2 条，前端要按 id 合并帧 |
| DemoGuard / 停止语义 | 已有 3 个 stop 端点；ASR 一停翻译就停 | 另做一套 |
| 插件重构 | **零** | 要把 `_LiveAsrSession` 搬进宿主（原 M2）|
| 卡位 | ASR 1600 + MT 2200 = **3800 ≤ 5120，同卡放得下** | 要占 2 张卡 |
| ASR 回归面 | 变大（开关默认关 + 断言守住 manifest）| 不变 |
| 卡片语义 | 识别卡多一个**可选**翻译功能 | 语义更纯 |

### 11.2 三条硬约束（不做就是回归）

**约束 1（最致命）：`software_capabilities` 保持只有 `swcap_audio_asr`。**

`app/static/core/framework.js:205-224` 的 `_capsAllLocalRunning()` 是
`caps.every(...)` —— 声明的**每一个** cap 都得本机 running，卡片才会被
写进 `compute.active_app`、cap 才会增量写进 `compute.autostart`。

⇒ 加了 `swcap_translate`：用户只跑 ASR 时判定为 false ⇒ **ASR 不再进
autostart**（重启不自动起）+ **active_app 不落盘**（开机不恢复向导）。
另 `wizard/state.js:931-933` 会把没跑的 cap 列为「缺能力」，向导弹
"翻译能力未就绪"。

⇒ 翻译是**页内可选功能**，不是卡片声明的能力。

**约束 2：翻译做成页内开关，默认关闭**，且**不动** `wizard.json` 的
`swcap_to_model`（那是「一键开启所需模型」的依据；加了会让只想识别的人
也白起一个 2200 MB 的翻译模型）。⇒ 现有 ASR 用户零行为变化。

**约束 3：MT 实例的启停放宿主共享模块。** 插件明令禁止跨卡 import
（`cards/qwen3_asr/__init__.py`："No cross-card imports."），所以 ASR 卡
不能 import web-translate 卡的 `WebTranslateManager`；但在 ASR 卡里
**照抄**一份 = 又一份近似副本（AGENTS 明令消灭的东西）。

⇒ 新增宿主 `app/core/translate/npu_manager.py`，只管「起/停 MT 实例 +
选 session + 卡位检查」，**不含**界面翻译的 provider 链（那是 web-translate
卡的私事）。web-translate 卡后续可切过去。

### 11.3 实现要点

**后端**

1. **宿主** `app/core/translate/npu_manager.py`（新）
   —— `ensure()` / `stop_all()` / `pick_session()` / `status()`；
   起之前用 §M0 修好的 `_local_memory_usage()` 查本机占用。
2. **宿主** `app/core/translate/npu_provider.py` —— 加
   `translate_one_fast(text, src, tgt)`：**混搭措辞（源=码/目标=名）+
   1 条兜底措辞**，最多 2 次推理（现有 `_translate_one` 最多试 5 条措辞，
   同传里是致命的 5×0.5 s）；
   复用现成的 `LANG_CODES` / `LANG_NAMES` / `PROMPT_ORDER` /
   `_looks_translated` 文种校验。实测表见 §D2。
3. **ASR 卡** `service.py` —— `endpoint` 分支挂「送去翻译」的钩子 →
   有界队列（满时丢最旧）→ 翻译线程池，**每个 MT 实例一个 session、
   实例内串行**（`LLMRPCSession` 执行器 `max_workers=1` ⇒ 并发单位是
   **实例**不是线程）。
4. **ASR 卡** `api.py` —— `POST /api/qwen3-asr/translate/settings`
   （目标语种 / 开关）、`/translate/enable`、`/translate/disable`、
   `GET /translate/status`。
5. **WS 新帧**（搭 `broadcast()` 同一班车）——
   `{"type":"translation","stream_id":…,"index":…,"source":…,"text":…,
   "status":…,"latency_ms":…}`。

**前端**

- `template.html` —— 实时区加一列译文 + 目标语言下拉 + 「实时翻译」开关
  + 「启用翻译模型」按钮。
- `app.js` —— 新增 `case "translation"` 分支；`_liveRender()`
  （`app.js:1861`）扩成「原文行 + 译文行」；`endpoint` case
  （`app.js:2838`）里 `L.segments.push(text)` 时建立「句序号 ↔ 译文」对应。
- 板端无 emoji 字体 ⇒ 只用内联 SVG。

**只做实时路径（第一期）**

文件/视频路径**没有句级信号** —— 它的 `round` 帧只有 committed/unfixed，
收尾只有一个 `done`（`service.py:1107-1123`）⇒ 要出双语字幕得自己按
标点/静音断句。⇒ 第一期只做**麦克风 / 板载麦克风**；「视频批量双语字幕」
是另一条批处理管线，将来单独开卡更合适。

**降级**

| 情况 | 表现 |
|---|---|
| MT 没启用 | 只出原文，译文区标「翻译未启用」 |
| 翻不出来（复读原文）| 显示原文 + 标「未翻译」（两条措辞都失败才降级）|
| 源语 == 目标语 | 透传（`status=skipped`）|
| ASR 停止 | 翻译队列清空，状态落 `stopped`（**不是** error）|
| 翻译出错 | **不影响** ASR（完全解耦）|

**SRT 导出的前提** —— `endpoint` 帧只有 `index` 与 `elapsed_sec`（句子
**结束**时刻），没有句子起止 ⇒ 要用 `round` 帧的 `chunk_start_sec`
（`service.py:1781-1789`）记下每句起点。

### 11.4 回归

* 现有 ASR 回归必须全过：`scripts/verify_asr_live.py` /
  `verify_asr_live_smoke.py` / `verify_asr_long_audio.py` /
  `verify_asr_wizard.py`；
* 新增 `scripts/verify_asr_translation.py` —— 假 MT session 断言
  endpoint→翻译→回填的时序与各条降级；**外加一条守住 manifest 的断言**：
  `software_capabilities` 必须仍然只有 `swcap_audio_asr`（防止后人
  「顺手」加上去把 autostart 打断）；
* 新增 `scripts/verify_asr_translation_e2e.py` —— **真机**端到端：
  喂样本 WAV 当麦克风 → 收 WS 帧 → 断言「至少一句 `status=done`」
  （2026-11-18 实测：译文 604 ms，混搭措辞第一条直接命中）。

**现场实测（2026-11-18，板上真机）**

| 项 | 值 |
|---|---|
| ASR + MT 同卡 | 卡 0：1600 + 2200 = 3800 MB ≤ 5120 MB |
| `POST /translate/enable` | 200，约 28 s 起完模型 |
| 端到端 | 18 round + 1 endpoint + 1 translation + 1 live_done |
| 单句翻译延迟 | 604 ms（1 次推理，未触发兜底）|
| 译文 | 「嗯嗯。哦对了，没错！我开始听他的音乐的时候……」|
