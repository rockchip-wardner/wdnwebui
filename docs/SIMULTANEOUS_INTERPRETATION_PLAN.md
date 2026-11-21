# 流式语音识别卡引入 TTS 做同声传译 —— 方案报告（v4）

> 状态：**待审核**（用户要求先出报告，不实施）
> 版本：v4（2026-11-21）。v2 按用户三条原则重写；v3 补上**双 TTS 实例的实测评估**；**v4 在用户追问「npu2 禁用后，同卡双 TTS 实例还有没有戏」后重做了硬件侧验证** —— 纠正 v3 的两处结论（`core_mask` 规则、每卡 runtime 上限），并**把「同卡不并行」从推论变成实测**。
> 涉及仓库：宿主 `wdnwebui`（`app/core/audio/`、`app/static/core/`）+ 插件 `wdnapps_audio_chat`（`cards/qwen3_asr/`、`cards/tts/`）
> 适用卡片：`qwen3-asr`（流式语音识别）新增一个与「实时翻译」并列的**同声传译模式**

---

## 0. 一页结论（v4）

### 0.1 三条原则 → 设计后果

| 用户原则 | 对设计的强制后果 | 关键手段 |
|---|---|---|
| **① 每句话都要翻译和播报，一句话都不能丢** | **取消「丢最旧」策略**。实时性不能靠丢内容维持，只能靠**产能** | 流水线化（合成与播放并行）+ **轻微倍速**（sox tempo，不变调）+ **允许滞后增长** + 如实显示滞后。（⚠️ v4：**npu2 是禁区 ⇒ 双实例不可用 ⇒ 产能天花板 = 单卡 1.02×**） |
| **② 浏览器麦输入 + 板载扬声器输出** | 输入侧现有「丢最旧」会**丢音频**（违反原则①）⇒ 必须放宽缓冲；输出侧要走**异步** aplay，否则 SDK worker 被堵 3 s | 放宽浏览器麦 L1/L3 缓冲并暴露丢块数；`play_to=both` + 前端**不订阅** PCM ⇒ 只走板载扬声器且不阻塞推理 |
| **③ 等翻译稳定后再播报** | 翻译是**一次性定稿**（实测：`translate_one_fast` → `session.respond` 返回完整文本）⇒ 译文返回即稳定；真正的风险是**源文在翻译期间被改写** | 三重稳定判据：① ASR 稳定窗口 → ② 翻译一次性定稿 → ③ **播前复核源文**，不一致则作废重译（内容不丢，只是版本升级） |

### 0.2 本轮实测的决定性结论（v4）

> ⚠️ **v3 有两处结论已被本轮实测推翻**，v4 更正：①「`llm_mode` 只接受 `0xFF`」的真正规则是
> **「`core_mask` 的置位数必须等于模型编译时的核数」**（普通 runtime 也一样，且**跨 3 张卡一致**）；
> ②「每卡上限 ~7 个上下文」实际是**按模型分类的配额**，不是一个统一总数。

| 实验 | 做法 | 结果 |
|---|---|---|
| **同卡双 TTS 实例** | 真实 SDK 路径（`_build_session`）在卡 1 连建两个 `qwen3-tts` 会话 | ❌ **失败**：第 2 个实例的 **talker** 在 `rknn3_model_init` 被设备拒绝（`MODEL_SETUP fail, ack = ACK_FAIL` → `init talker runtime failed`，`tts.py:1136`）；失败时卡上 **free 2362 MB**（≫ 需要的 862 MB）⇒ **不是显存** |
| **`core_mask` 的真实规则** | 4 个真实子模型 × 18 种掩码 × 3 张卡全量实测 | **掩码置位数必须 == 模型编译核数**：text_projection（编译 1 核）只认单核（0x01–0x80 全通；0x03/0xFF 被拒「npu core number **1**」）；speech_decoder / talker / code_predictor（编译 8 核）只认 **0xFF**（0x01/0x0F 被拒「npu core number **8**」）⇒ **要改核数只能重新转模型**，改掩码无效 |
| **每卡 runtime 配额** | 逐个加裸 runtime 直到 `MODEL_SETUP fail` | **分类配额，不是统一总数**：普通 **1 核**模型 **24 个**；普通 **8 核**模型 **4 个**；**llm 8 核**模型 **≥ 8 个**（带 LLM session 也 ≥8）；**同一 context 上可建 6 个 LLM session** |
| **双实例到底卡在哪** | 逐段复刻两个实例的真实构造顺序 | 裸的 8 个上下文（2×tp + 2×sdec + 2×cp + 2×talker）**全部共存**（free 剩 1733 MB）⇒ **上下文总数不是瓶颈**；**加上 LLM session 后**，第 2 个 talker 的 model setup 被拒 ⇒ **LLM session 会挤占设备端 model-setup 配额** |
| **同卡双实例能不能并行**（关键） | 卡 0 上两个**独立进程**各跑一个 hy-mt-1-5（用户参照的模型），同一闸门同时放行 | 单请求 **0.93 s** → 并发 **1.54 s**（1.65×）；**聚合吞吐 1.08 → 1.30 req/s，仅 1.20×** ⇒ **同卡两实例是时间片轮转，不是真并行**（v3 这里只是推论，现已实测） |
| **跨卡双实例** | 在 npu2（空闲）起第二个 qwen3-tts | ✅ 成功（30 s 内 running）；但 **npu2 是部署禁区** ⇒ 不可用 |
| **跨卡并发吞吐** | 卡1（运行中 worker）‖ 卡2（本地直连会话），同时各合成 3 句 | 单卡 **1.02×**；双卡 **≈ 2.07×**；单卡延迟仅膨胀 **6%** ⇒ **真并行，不是共享算力** |
| **TTS 每实例占几个 runtime** | 统计 TTS 的 5 个子模型 | **4 个**（speaker_encoder on_demand 不建 NPU）：**3 个 8 核上下文**（talker + code_predictor + speech_decoder）+ **1 个单核**（text_projection） |
| **npu2 是部署禁区** | 用户 2026-11-21 确认 | ⇒ 产能天花板 = **单卡 1.02×**（详见 §10.2） |

### 0.3 结论

**能做。** 但「一句都不能丢」把方案从「丢内容保实时」改成了「堆产能保实时」，而**堆产能的形态是「第二个 TTS 实例」**（v4 实测：同卡不行、只有跨卡才有 2.07× —— 而 npu2 禁用 ⇒ 当前只能单卡 + 兜底）。

**v4 更新的结论**：同卡双 TTS 实例**两条路都堵死**，且都不需要重转模型就能判断：

1. **起不来**：第 2 个实例的 talker 被设备端 model-setup 配额拒绝（`MODEL_SETUP fail`，失败时显存充足 ⇒ 非显存问题）。
2. **即便起得来也不提产能**：卡 0 上两个 hy-mt-1-5 实例并发实测，**聚合吞吐只有 1.20×**（不是 2×）——两个 `core_mask=0xFF` 的实例抢同一批 8 个核，只能时间片轮转。
   ⇒ 所以「重新转模型让同卡双实例跑起来」这条路**收益上限约 1.2×，且要改 SDK + 重测质量/速度，投入产出不成正比 ⇒ 不做**（用户问「是否需要重新转模型」的答案：**不需要**）。

⇒ **npu2 禁区的最终形态 = 单卡 + 1.02× 产能 + `tempo` 自适应 + 允许滞后增长 + 如实显示滞后**（§10.2 给了按语言对分解的结论）。

**总延迟**：典型 **5–7 s**（快档）/ **9–11 s**（稳档）—— 与真人同传 3–8 s 同量级（稳档偏慢但一个词都不会错）。

**吞吐**（原则①的成败点）：

```
译音总时长 = 源语总时长 × 语言对膨胀比（中→英 ≈ 1.15–1.30，英→中 ≈ 0.8–1.0）
需求产能  = 膨胀比（1.0 ~ 1.30）
单实例产能 = 1.02×（实测）  ⇒  中→英单实例【绝对不够】，必然越积越多
双实例产能 = 2.07×（实测，跨卡）  ⇒  覆盖 1.30 膨胀比 + 抖动余量，【够】
                  ⚠️ 但 npu2 是部署禁区 ⇒ 本次实施按【单实例 1.02×】设计
剩余差额 ⇒ sox tempo 1.05–1.15 轻微加速吸收（不变调）+ 允许滞后增长
```

**硬件事实（已澄清，v4 更新）**：本板 `GET /api/compute` 实测 **`card_count=3`** / `model_cards=[0,1,2]`，`lspci` 显示 **3 个独立 PCI NPU 端点**（`0001:11:00.0` / `0003:31:00.0` / `0004:41:00.0`）⇒ **npu2 在硬件上真实存在且空闲**，跨卡双实例技术上完全可行（实测 2.07×）。

**但用户 2026-11-21 明确：npu2 是部署禁区，不能用。** ⇒ 这不是硬件限制，而是**部署约定** ⇒
**「一句都不丢」在当前部署下只能靠 tempo + 允许滞后增长兜底**（你已选的策略②）。
⇒ **好消息**：按语言对分解，**外语 → 中文（k ≈ 0.8–1.0）在单卡 1.02× 下即可做到「恒定滞后 + 一句不丢」**（§10.2 表）⇒ 产品的核心场景成立。
⇒ **保留项**：多实例改造（per-request PCM socket、跨卡轮转）**代码层面先按可扩展写**（socket 走 per-request 参数），等哪天拿到第二张可用卡，**只差一个 `POST /compute/instances`，不用重写**。

**最大的风险不是延迟，是回声**：板载扬声器放译文 ⇒ 板子附近若有拾音点会自激。用户选「浏览器麦 + 板载扬声器」是**缓解版**（麦克风在远端笔记本，衰减大），但仍需提示。

---

## 1. 市面方案对比

### 1.1 Azure Speech（标准 API + Live Interpreter）

来源：<https://learn.microsoft.com/en-us/azure/ai-services/speech-service/speech-translation>（已读取）

* **标准实时语音翻译**：speech-to-text / speech-to-speech，输出 interim + final 两类结果，可直接输出合成语音。
* **新的 Live Interpreter（LLM 驱动）**：
  * **免指定源语** —— 持续做语种识别（对应本板 ASR 的 `language` 字段会随内容变）；
  * **会话中途换语**；
  * **BYO voice / personal voice**（保留说话人风格语气）；
  * 单次调用**最多 2 个目标语种**；
  * 计费按 **intermediate 结果放大**（interim 多 ⇒ 贵）。

**对本板的启示**

1. 「interim（假设）+ final（确认）」两路结果并存是行业标准做法 ⇒ 本板的 `unfixed` / `committed` 双轨**正好对应**，同传走 unfixed 是符合行业惯例的，不是权宜之计。
2. 免指定源语 ⇒ 本板 ASR 已有的 `language` 随内容更新即可用；但**不要**用退化轮次的语种（第一轮修复已建立该不变量）。
3. 「最多 2 个目标语种」是**云端算力**下的限制；本板双 TTS 实例已经全用在一个目标语种上 ⇒ **P0 只做 1 个目标语种**。

### 1.2 Qwen3-Omni（端到端）

来源：<https://raw.githubusercontent.com/QwenLM/Qwen3-Omni/main/README.md>（已读取）

* **原生端到端** omni 模型，MoE **Thinker–Talker** 架构 + 多码本设计；
* **119 文本语种 / 19 语音输入 / 10 语音输出**；
* 官方 cookbook 有 **Speech Translation（Speech-to-Text / Speech-to-Speech）**；
* 实时流式（文本 + 语音），自然 turn-taking；
* 官方 **Cross-Lingual Speech Generation** 指标（与 CosyVoice2/3 同表对比）：`en-to-zh 5.37`、`ja-to-zh 3.32`、`zh-to-en 2.76`、`zh-to-ja 8.29` 等；
* 官方**默认提示词**明确区分任务：
  * ASR（中文）：`请将这段中文语音转换为纯文本。`
  * Speech-to-Text Translation：`Listen to the provided <source_language> speech and produce a translation in <target_language> text.`

**对本板的启示（关键）**

1. **板端 TTS 支持的 10 种语音输出语言**（chinese/english/german/italian/portuguese/spanish/japanese/korean/french/russian）与 **Qwen3-Omni 的语音输出语言集合完全一致** ⇒ 板端 TTS 与 Omni 同源 ⇒ 未来若引入 Omni 做端到端，**语言集、音色接口、评测口径都不用重做**。
2. 官方默认提示词说明：**ASR 任务需要显式指令**。这与本仓第一轮修复的发现一致（本仓 Qwen3-ASR 空 system prompt 是模型退化根因之一）⇒ 同传链路上的每个模型都必须给对指令，不能裸跑。
3. **板端跑不动 30B-A3B** ⇒ 端到端不是本期的路。README 另有 `Qwen3-Omni-Flash-Instruct`（更小的版本）⇒ 列为 **P2 评估项**，不在 P0。
4. 端到端模型天然没有「翻译产物要等稳定」的问题（一次生成到底）⇒ 端到端路线能天然满足原则③，代价是板端跑不动。

### 1.3 板端现有三段式（本方案）

```
浏览器麦/板载麦/文件/视频
      → ASR（qwen3-asr）→ committed + unfixed
      → 翻译（hy-mt-1-5）→ 译文（一次性定稿）
      → TTS（qwen3-tts，**1 实例**；双实例需跨卡，npu2 禁 ⇒ 当前 1 个）→ PCM（4 s 一块）→ 板载扬声器
```

**这就是 Azure 标准 API / Riva 那类经典同传管线的同构实现**，不是新发明。优势：三个模型都已在本板跑通、显存够、**实测跨卡双 TTS 实例可用且产能 2.07×**（⚠️ 但 npu2 是部署禁区 ⇒ 当前按单实例实施，见 §10.2）。

### 1.4 对比结论

| 维度 | Azure 标准 | Azure Live Interpreter | Qwen3-Omni | **板端三段式（本方案）** |
|---|---|---|---|---|
| 架构 | 三段 | LLM 驱动端到端 | 端到端 | 三段 |
| 源语 | 需指定（部分场景可自动） | 免指定 | 免指定 | ASR 已有 `language` 字段（随内容更新） |
| 目标语数 | 1 | ≤ 2 | 多 | **1**（P0） |
| 文本稳定性 | interim + final 两路 | LLM 内部处理 | 一次生成到底 | **快/稳两档可切换** + 播前复核 |
| 板端可行性 | 不适用（云） | 不适用（云） | 30B 跑不动；Flash 待评估 | **已跑通**；跨卡双实例实测 2.07×，但 **npu2 禁 ⇒ 实施时单实例 + 兜底** |
| 预期滞后 | 秒级（云） | 更低 | 最低 | **5–7 s（快）/ 9–11 s（稳）** |
| 内容可否丢 | 可按策略丢 | — | — | **本方案：一律不丢**（靠产能 / tempo / 允许滞后） |
| 音色 | 标准音色 | BYO / personal voice | 3 个 speaker | 6 个参考音色（无克隆） |

**决策：P0 走三段式 + 不丢内容；产能按「单实例 + tempo + 允许滞后增长」实施（多实例能力保留可扩展性）；端到端列入 P2 评估。**

### 1.5 取证边界（诚实声明）

* **已读取**：Azure Speech 实时翻译文档、Qwen3-Omni 官方 README。
* **抓取失败**：NVIDIA Riva 文档页（404）、Google Cloud 实时翻译文档页（网络失败）⇒ **本报告不引用这两家的任何具体指标或接口细节**，只按上文 1.4 的抽象分类讨论。
* 板端所有延迟/吞吐数据均为**本机实测**（脚本 `/tmp/measure_tts.py`、`/tmp/measure_tts_crosscard.py`、实时 API 探测），非厂商宣传值。

---

## 2. 板端现有能力盘点

### 2.1 三段能力的接口与实测数据

| 环节 | 代码位置 | 实测 | 口径 |
|---|---|---|---|
| ASR 已确认文本 | `wdnai/src/wdnai/backends/asr.py:85-86`（`ONLINE_ROLLBACK_TOKENS_DEFAULT=5` / `ONLINE_ROLLBACK_ROUNDS_DEFAULT=4`） | 滞后 4–5 s | 结构性回滚窗口 |
| ASR 假设文本 | 同上（unfixed 字段） | 滞后 ~1 s | 轮间隔 1 s |
| **翻译** | `app/core/translate/npu_manager.py::translate_sentence` → `npu_provider.py::translate_one_fast` | `avg_latency_ms ≈ 263`；**一次性定稿**（`session.respond` 返回完整文本，无增量部分）；最多试 2 条措辞 ⇒ 最坏 ~1 s | `GET /api/qwen3-asr/translate/status` |
| 翻译实例数 | `GET /api/compute` + `GET /api/qwen3-asr/translate/status` | 实测时 **2 个** `hy-mt-1-5`（npu0，本地）`pick_session()` 轮转 ⇒ 可并行；⚠️ **v4 复核时板端上本地实例数为 0**（当时列出的两个实例都是**对端 adopt 来的**服务，`translate/status` 报 `count: 0`）⇒ **实施前必须确认本地 `hy-mt-1-5` 已拉起**（`compute.autostart` 里有 `hy-mt-1-5@0`，重启会自动起） | 同上 |
| TTS 首帧 | `cards/tts/service.py::_infer_one` | 中文短句 3.27 s / 长句 3.57 s；英文 news_female 2.95 s；英文 male_announcer 2.82 s | `/tmp/measure_tts.py`（`play_to=browser`，不出声） |
| TTS 整句 | 同上 | 中文短句 3.39 s（音频 3.68 s）/ 长句 4.30 s（4.46 s）；英文 news_female 3.08 s（3.28 s） | 同上 |
| **TTS 合成速率（单卡）** | 由上两行推算 + 跨卡实验复核 | **1.02×**（4.19 s 合成 / 4.28 s 音频）⇒ **无明显固定开销**，长句不吃亏 | `/tmp/measure_tts_crosscard.py` |
| **TTS 合成速率（双卡并发）** | 同上 | **≈ 2.07×**；单卡延迟膨胀 **6%** | 同上 |
| TTS PCM 块 | `cards/tts/service.py:55-62` 注释 | **4 s 一块** | SDK `AudioPlayer.play_chunk` 的 64 KB OS pipe 填满会阻塞 worker ~3 s |
| 翻译语种 | `GET /api/qwen3-asr/translate/languages` | **37 种** | hy-mt-1-5 |
| TTS 语种 | `GET /api/tts/voices` | **11 个 id**（auto + 10） | 见 §2.4 |

### 2.2 硬件与算力（`GET /api/compute` + `lspci` 实测）

| 项 | 值 |
|---|---|
| NPU 卡 | **3 张**（`npu_index` 0 / 1 / 2，`card_count=3`，`model_cards=[0,1,2]`） |
| PCI 端点 | `0001:11:00.0` / `0003:31:00.0` / `0004:41:00.0`（3 个独立的 `Device 182a`） |
| 当前占用 | 卡 0：embedding + ASR + 2×MT（3800 MB）；卡 1：TTS（1900 MB）；**卡 2：空** |
| worker 实际 RSS | qwen3-tts **944 MB** / hy-mt-1-5 317 MB / qwen3-asr 266 MB（账面 `memory_estimate_mb` 1900 偏大） |
| 整机内存 | 7.9 GB 总；起第二个 TTS 实例后可用 2865 → 1866 MB（**可接受**） |
| 启动耗时 | TTS ≈ 30 s（`estimated_init_seconds_per_model`） |

### 2.3 双 TTS 实例的实测评估（v4 完整重做，v3 的核心）

#### 实验 1：同卡双实例 —— ❌ 不可行（根因：**设备端 model-setup 配额**，不是内存、也不是核掩码选错）

* **做法（第一轮）**：临时把 `/userdata/models/Qwen3-TTS/qwen3-tts.json` 的 `max_replicas_per_card` 改成 2 → `POST /api/compute/instances {slots:[{npu_index:1, replica:1}]}`。
* **结果**：worker 起来了（RSS 965 MB）但 **bind 失败** ⇒
  `AssertionError: init talker runtime failed`
  （`wdnai/src/wdnai/backends/tts.py:1136`；底层 C 库日志：`MODEL_SETUP fail, ack = ACK_FAIL` → `rknn3_model_init` ret=-3）。
* **做法（v4 复现，去掉 registry 干扰）**：在**同一个进程**里用真实 SDK 路径
  `_build_session("qwen3-tts", 1, None)` 连建两个会话（卡 1），并在每步之间用 `rknnsmi` 采样卡上内存。
* **结果**：**第 1 个实例成功、第 2 个实例在 talker 处失败**，与第一轮完全一致 ⇒ 排除 registry / worker 进程的因素。
* **清理**：删除失败实例 + 恢复 manifest ⇒ **基线已复原**（manifest 已还原成缺省值，autostart 未动）。

**根因取证（v4 修正）**：

1. **SDK 吞异常**：`RKNN3Lite.init_runtime` 里 `except Exception: return -1`（`_librknn.py:2114`）把真实原因吃掉了。
   ⚠️ **v4 补一条踩坑**：这个方法**不抛异常、只返回 -1** ⇒ 探针脚本里**必须检查返回值**，
   否则会把失败当成成功（本轮就踩过一次，导致误判「双实例的裸 runtime 组合能共存」）。
2. **不是卡上内存不够**（`rknnsmi.get_memory_info` 实测，每卡 total = **5120 MB**）：

   | 卡 1 状态 | used | free |
   |---|---|---|
   | 空载 | 36 MB | 5084 MB |
   | 1 个完整 TTS 实例 | **1942 MB** | 3178 MB |
   | 第 2 个实例**失败时**（已建 tp/sdec/cp） | 2758 MB | **2362 MB** |

   ⇒ 单实例占 **1942 MB**（与 manifest 的 `memory_estimate_mb: 1900` 吻合）；第二个实例失败时**卡上还剩 2362 MB**，比它需要的 862 MB 还多 ⇒ **不是总量不够**。
3. **不是 `core_mask` 选错**：0xFF 对 talker 是**合法**的（实测 talker 用 0xFF 能正常起来，见实验 5）⇒ 失败发生在模型 setup 阶段，**掩码没问题**。
4. **TTS 的 runtime 构成**（`wdnai/src/wdnai/backends/tts.py`，v4 补全 `core_mask`）：

   | 子模型 | runtime 类型 | core_mask | 编译核数 | 卡上占用（实测） |
   |---|---|---|---|---|
   | talker | `RKNN3Lite(llm_mode=True)` | **0xFF** | 8 | **862 MB** |
   | code_predictor | `llm_mode=True`（传了 `llm_args` ⇒ 建 LLM session） | **0xFF** | 8 | **194 MB** |
   | speech_decoder | 普通 runtime | **0xFF** | 8 | **592 MB** |
   | text_projection | 普通 runtime | 0x01 | 1 | 24 MB |
   | ~~speaker_encoder~~ | 普通 runtime | 0x02 | 1 | on_demand ⇒ **不建** |

   ⇒ **一个 TTS 实例 = 3 个 8 核上下文 + 1 个单核上下文**。
5. **裸 talker 能共存，但没意义**：在「1 个完整实例」之上再加 2 个裸 talker（各 +862 MB）都能建好 ⇒ 同核多 context 本身允许；但它们**共用同一批 8 核** ⇒ 只能时间片轮转 ⇒ **不可能真并行** ⇒ 总产能 ≈ 1×（跨卡实测 2.07×）。
   （顺带说明：仓里「算力页每卡两个复选框」那套 `max_replicas_per_card` 机制**确实可用** —— 唯一开了 2 份的是 `hy-mt-1-5`（纯 LLM，一个 runtime）。**TTS 不适用是因为它一个实例含 3 个 8 核上下文 + 2 个 LLM session，不是机制本身的问题。**）

#### 实验 2：跨卡双实例 —— ✅ 可行

* **做法**：`POST /api/compute/instances {model:"qwen3-tts", npu_index:2}`。
* **结果**：**30 s 内 status=running**；整机可用内存 2865 → 1866 MB。
* **结论**：**跨卡双 TTS 实例完全可行**（前提是第三张卡可用）。

#### 实验 3：跨卡并发吞吐 —— ✅ 真并行，2.07×

* **做法**：卡 1 = 运行中服务里的 worker（用 `/api/tts/start` 驱动）；卡 2 = 独立进程里直连 `LLMLocalSession`（`_build_session("qwen3-tts", 2, None)`）⇒ 两进程、两张卡、同时合成。

| 场景 | 每句耗时 | 产出音频 | 速率 |
|---|---|---|---|
| 卡 1 单独（基线） | 4.19 s | 4.28 s | **1.02×** |
| 卡 1 并发时 | 4.45 s（膨胀 6%） | 4.65 s | 1.04× |
| 卡 2 并发时 | 4.52 s | 4.70 / 4.86 / 4.62 s | 1.04–1.08× |
| **双卡合计** | — | — | **≈ 2.07×**（保守按卡1 膨胀计 ≈ 1.92×） |

* **结论**：两张卡**几乎互不干扰**（单卡延迟只涨 6%）⇒ **跨卡双实例 = 真并行**，不是共享算力。

#### 实验 4：`core_mask` 的真实规则 —— **置位数必须 == 模型编译核数**（v4 新增，纠正 v3）

* **做法**：把 TTS 的 4 个真实子模型当探针，逐一试 18 种掩码（0xFF / 单核 8 种 / 双核 / 四核 / 0xF0 / 0xFC / 0x33 非连续…），**三张卡各跑一遍**，用 SDK 原路径 `RKNN3Lite.init_runtime(target, core_mask, device_id)`。
* **结果**（卡 0/1/2 三卡**完全一致**）：

  | 子模型 | 编译核数 | 接受的掩码 | 被拒的典型 | 驱动原文 |
  |---|---|---|---|---|
  | text_projection | **1** | 0x01 / 0x02 / 0x04 / 0x08 / 0x10 / 0x20 / 0x40 / 0x80（任意单核） | 0x03、0x0F、0xFF、0xFC、0x33 | `core_mask ff is not match with npu core number 1!` |
  | speech_decoder | **8** | **仅 0xFF** | 0x01、0x0F、0xF0 | `core_mask 1 is not match with npu core number 8!` |
  | talker（llm_mode） | **8** | **仅 0xFF** | 0x01、0x0F | `core_mask 1 is not match with npu core number 8!` |
  | code_predictor（llm_mode） | **8** | **仅 0xFF** | 0x01 | `core_mask 1 is not match with npu core number 8!` |

* **结论（v3 的说法要改）**：
  * ✅ 规则是 **「掩码里置位的核数 == 该 `.rknn` 编译时用的核数」**，**不是**「llm_mode 必须占满 8 核」。
    普通 runtime 同样受这条约束（speech_decoder 也只认 0xFF；text_projection 只认单核）。
  * ⚠️ **非连续掩码另有独立限制**：`0x33` 报 `core mask 0x00000033 is not contiguous, bits must be consecutive`。
  * ⇒ **想少占核 ⇒ 必须重新转模型**（转成 4 核版本），**改掩码是死路**。
  * ⇒ 顺带解释了 SDK 里 `TEXT_PROJECTION_CORE_MASK=0x01` / `SPEAKER_ENCODER_CORE_MASK=0x02` 为什么是单核：**这两个模型就是按 1 核编译的**。

#### 实验 5：每张卡的 runtime 配额 —— **按模型分类，不是统一总数**（v4 重做，纠正 v3 的「上限 7 个」）

* **做法**：在**空卡**上逐个加裸 runtime（`init_runtime` 返回 0 即算存活），每步采样卡上内存，直到 `MODEL_SETUP fail`。
* **结果**：

  | 探针 | 类型 / 掩码 | 单个卡上占用 | **可共存数** | 失败时卡上 free | 失败原因 |
  |---|---|---|---|---|---|
  | text_projection | 普通 / 0x01 | 28 MB | **24**（第 25 个 fail） | 4429 MB | 配额（非显存） |
  | speech_decoder | 普通 / 0xFF | 592 MB | **4**（第 5 个 fail） | 2713 MB | 配额（非显存） |
  | talker | llm / 0xFF | 862 MB | **≥5**（第 6 个 fail） | **774 MB** | **显存不足**（774 < 862） |
  | code_predictor | llm / 0xFF | 194 MB | **≥8**（未撞顶，主动停） | — | — |
  | code_predictor + LLM session | llm / 0xFF | 196 MB | **≥8**（8 个全过） | 3521 MB | — |
  | **20×text_projection + 1×talker** | 混合 | — | **talker 起不来** | **4538 MB** | 配额被小模型挤占 |

* **结论**：
  * 每卡的 model-setup **配额是按模型分类的**，**不是** v3 说的「统一 ~7 个上下文」。
  * **配额互相挤占**：20 个小模型（只占 582 MB）就能把一个 talker 挤掉 ⇒ **不是「上下文总数」，也不是显存，而是设备端的 model-setup 资源池**。
  * 驱动内部的精确计费规则**未逆向出来**（只拿到边界值）—— 这不影响结论，因为**两条路都堵死**（见实验 7、8）。

#### 实验 6：TTS 每个实例到底占几个 runtime —— **4 个**

`init_speaker_encoder` 在 **on_demand** 模式下「只加载 mel 滤波器组（几百 KB），真正提取时才开 NPU 上下文」⇒ **speaker_encoder 不占 NPU runtime**（用内置音色时）。

| runtime | 类型 | core_mask | 编译核数 | 权重（主机侧 .weight） | 卡上占用（实测） |
|---|---|---|---|---|---|
| talker | **llm_mode** | 0xFF | 8 | 784 MB | **862 MB** |
| code_predictor | **llm_mode** | 0xFF | 8 | 144 MB | **194 MB** |
| speech_decoder | 普通 | 0xFF | 8 | 218 MB | **592 MB** |
| text_projection | 普通 | 0x01 | 1 | 16 MB | 24 MB |
| ~~speaker_encoder~~ | 普通 | 0x02 | 1 | 23 MB | on_demand ⇒ **不建** |

⇒ **每实例 4 个 NPU runtime（3 个 8 核 + 1 个单核）+ 2 个 LLM session（talker、code_predictor 各一个）。**

#### 实验 7：同卡双实例**到底能不能并行** —— ❌ 不能（**v4 实测，把 v3 的推论变成数据**）

* **做法**：卡 0 上跑两个**独立进程**，各建一个 hy-mt-1-5 会话（纯 LLM = 每实例 1 个 llm 8 核 runtime，正是用户参照的「一张卡两个实例」场景）。两个进程都加载完模型后，用**同一个闸门文件**同时开跑，跑 5 次同样的翻译请求（长段落，单请求 ≈ 0.93 s）。
* **结果**：

  | 场景 | 单请求延迟（去掉首条预热） | 聚合吞吐 |
  |---|---|---|
  | 单实例独占 | **0.93 s** | 1 / 0.93 = **1.08 req/s** |
  | **同卡两实例并发** | **1.536 s**（A、B 几乎完全对称） | 2 / 1.536 = **1.30 req/s** |

  ⇒ 并发时单请求延迟涨到 **1.65×**，但**聚合吞吐只到 1.20×**（不是 2×）。
  ⇒ **同卡两实例是时间片轮转，不是真并行**：两个实例都用 `core_mask=0xFF`（全部 8 核）⇒ 抢同一批核。
  ⇒ 有效 NPU 占用率 ≈ 2×0.93/1.536 = **1.21**（约 21% 损耗在切换/准备间隙）。

* **对照**：跨卡双实例实测 **2.07×**（实验 3）⇒ **只有第二张卡才是真并行**。

#### 实验 8：同卡双 TTS 实例的阻塞点定位 —— **LLM session 挤占配额**（v4 新增）

* **做法**：逐段复刻两个实例的真实构造顺序（`ensure_loaded`：tp → sdec → cp → talker），分两组对照。
* **结果**：

  | 组合 | 上下文构成 | 结果 |
  |---|---|---|
  | **裸 runtime**（不建 LLM session） | 2×tp + 2×sdec + 2×cp + 2×talker = **8 个**（6 个 8 核 + 2 个单核） | ✅ **全部共存成功**（used 3387 MB，**free 剩 1733 MB**）⇒ **上下文总数不是瓶颈** |
  | **加上 LLM session**（真实路径） | 同上 + A.talker 的 output tensors/session、A.cp/B.cp 的 session | ❌ **B.talker 的 model setup 被拒**（`MODEL_SETUP fail`），此时 free = 2362 MB |
  | 8×code_predictor（各带 session） | 8 个 llm 8 核 + 8 个 session | ✅ 全过 ⇒ **session 数量本身不是配额** |
  | 1 个 runtime 上建 6 个 session | 同一 context | ✅ 全过 ⇒ 驱动**允许同一 context 多 session** |

* **结论**：阻塞项是 **LLM session 与 model setup 共享设备端资源池**（或等价地说：带 session 的 llm 上下文组合会挤占配额）。
  ⇒ 但**这不影响最终结论**：即使把配额问题绕过去（同卡双实例能起来），**产能也只有 ≈1.2×**（实验 7 实测），远不如跨卡 2.07×。

#### 实验 9：能不能把 runtime 拆开 / 减掉？—— **只能靠重新转模型，且不值得**

| 尝试 | 结果 |
|---|---|
| 改 `core_mask` 让 llm_mode 少占核（0x0F / 0xF0 / 0x0C / 0x03 / 0xFC） | ❌ 驱动全拒（ret=-2）⇒ 规则是「置位数 == 编译核数」（实验 4）⇒ **改掩码无效** |
| 改 `core_mask` 让普通 runtime 少占核 | ❌ 同样被拒；speech_decoder 就是 8 核编译的 |
| 去掉 speaker_encoder | ✅ 已经天然不建（on_demand）⇒ 每实例 4 个 |
| 去掉 code_predictor | ❌ 它是 **自回归小 LLM**（vocab 2048、ctx 32、每帧出 15 个 codec token，`CODEC_VOCAB_SIZE=2048`）⇒ **不是查表**，不能直接删；要么搬到 CPU（太慢），要么**改写成普通 runtime（需重新转模型 + 手写采样循环）** |
| 去掉 speech_decoder | ❌ 声码器必需；218 MB 权重可换更小的（**要重新转**），但质量/速度要重测 |
| 去掉 text_projection | ❌ 必需（16 MB，占单核，代价最小） |
| **「1 组模型 + 2 个 session」**（同一 runtime 上开两个 session 来当两个实例） | ⚠️ 驱动**允许**（实验 8：同 context 建 6 个 session 成功）⇒ 技术上可做，但 ① 要改 SDK 的 talker 状态管理（`past_hidden` / KV cache / codec 状态是**每实例成员变量**，两路并发会串扰）；② **同一 context 上的两个 session 是串行的 ⇒ 产能仍是 1×** ⇒ **对本方案没有价值** |

⇒ **同卡双 TTS 实例的唯一硬杠杆 = 重新转模型（把 8 核子模型转成 4 核，腾出配额）**，
但**收益上限约 1.2×**（实验 7）而成本是「重新转模型 + 改 wdnai SDK + 重测质量与速度」⇒ **不划算 ⇒ 不做**。

#### ⚠️ 但这样做**救不了「一句都不丢」**

同卡两个实例**共用同一批 8 个 NPU 核** ⇒ 只能时间片轮转 ⇒ **不可能真并行** ⇒ 聚合产能实测 **仅 1.20×**（实验 7），跨卡则是 **2.07×**（实验 3）。

> **v4 状态**：v3 里「同卡并发不并行」只是**基于核共享的推断**（因为同卡双完整实例起不来，没法直接测）；
> v4 用**两个 hy-mt-1-5 实例在卡 0 上并发**把这个推断**实测成了数据**（1.20×）。
> 同卡双 TTS 实例仍然**起不来**（实验 1、8），但**这条路已经不值得走**了 —— 收益上限 1.2×，代价是重新转模型 + 改 SDK。

### 2.4 语言集合与映射表（实测数据）

**翻译**（37 种，`GET /api/qwen3-asr/translate/languages`）：
zh-CN, en, fr, pt, es, ja, tr, ru, ar, ko, th, it, de, vi, ms, id, tl, hi, pl, cs, nl, km, my, fa, gu, ur, te, mr, he, bn, ta, uk, bo, kk, mn, ug

**TTS**（`GET /api/tts/voices` ⇒ `languages`）：
auto, chinese, english, german, italian, portuguese, spanish, japanese, korean, french, russian

**可同传出声的 10 种**（翻译 id → TTS id）：

| 翻译 id | TTS id | 翻译 id | TTS id |
|---|---|---|---|
| zh-CN | chinese | ja | japanese |
| en | english | ko | korean |
| fr | french | it | italian |
| pt | portuguese | de | german |
| es | spanish | ru | russian |

**只能出字幕的 27 种**：tr, ar, th, vi, ms, id, tl, hi, pl, cs, nl, km, my, fa, gu, ur, te, mr, he, bn, ta, uk, bo, kk, mn, ug。

> 附带发现：板端 TTS 的这 10 种语音输出语言与 **Qwen3-Omni 的语音输出语言集合完全一致** ⇒ 同源，为 P2 的端到端路线留好接口。

### 2.5 浏览器麦输入链路现状（原则②的上游）

| 环节 | 现状 | 对同传的影响 |
|---|---|---|
| 采集 | `getUserMedia` → 500 ms 一块（`chunk_ms=500`），裸 s16le 上传 `POST /live/chunk` | ✅ 可直接用于同传 |
| **L1 前端队列** | 上限 **2 块**，满了**丢最旧**（`app.js:31,3426`） | ❌ **违反原则①** ⇒ 同传模式必须放宽 |
| **L2 传输** | 串行 `await fetch` ⇒ **上传速率被推理速率钳住** | ⚠️ 推理慢于实时 ⇒ 输入侧积压（服务端 `lag_ms` / `buffered_chunks` 可见） |
| **L3 服务端缓冲** | `_LIVE_MAX_BUFFER_CHUNKS = 3` 块（1.5 s）；连续丢 5 块熔断 | 同传模式应放宽，并暴露丢块计数 |
| 端点检测 | `_LIVE_ENDPOINT_CONFIG`：静音阈值 −45 dBFS、**静音 1200 ms 判句末**、最短语音 0.3 s | ✅ 同传切句可直接挂 endpoint（实时路径已有 `_submit_translation`） |
| 停顿检测 | `_PAUSE_ROUNDS_LIMIT = 2`（按**内容**判停顿，不按长度） | ✅ 复用做稳定窗口（原则③） |

### 2.6 已经有的、可直接复用的件

| 能力 | 位置 | 复用方式 |
|---|---|---|
| ASR 双轨文本（committed / unfixed） | `cards/qwen3_asr/service.py::_on_delta`（`service.py:2155-2160`） | 同传消费 `frame["unfixed"]` |
| 回看/退化不变量 | `_on_delta` 里 `is_lookback` / `_degenerate` ⇒ 退化时 `frame["unfixed"]=""` | **同传在退化期自动静默**（不会念出胡话），零额外成本 |
| 停顿判据（按内容） | `_pause_rounds` / `_last_unfixed` | 复用做「稳定窗口」（原则③） |
| 实时路径端点 | `_LIVE_ENDPOINT_CONFIG` + `endpoint` 帧 + `_submit_translation()` | 同传切句直接挂 endpoint |
| 翻译服务（队列 + 统计） | `cards/qwen3_asr/translation.py` | 复用 `submit()` / `_broadcast()`；同传用**独立小队列**（DD-3） |
| 译文帧 | 同上，`{"type":"translation", ...}` | 字幕译文沿同一条 WS 推给前端 |
| TTS 流式 PCM 通道 | `cards/tts/service.py::_on_audio_chunk` / `subscribe_audio` / `_finish_audio_stream` | PCM 已能以「12 字节头 + float32」逐块推送 |
| 浏览器端逐块无缝播放 | `cards/tts/static/app.js`（`audioScheduledUntil` 排期、`ws.binaryType="arraybuffer"`） | 抽到宿主 `app/static/core/` 共享给两张卡 |
| **异步板载播放** | `cards/tts/service.py::AplayWriter`（独立 drain 线程写 aplay 的 stdin ⇒ **不阻塞 SDK worker**） | 板载扬声器输出走它（`play_to=both`） |
| 播放目标 | `app/core/audio/tts_bridge.py`（`PLAY_DEVICE/PLAY_BROWSER/PLAY_BOTH`）+ `TTSRequest` | 沿用；`TTSRequest` 已有 `non_streaming_mode`（默认流式）、`ref_speaker`、`volume`、`language` |
| 多实例取用 | `app/core/compute/registry.py::get_session(capability, npu_index=...)` + `list_sessions()` | 双 TTS 实例：按 `npu_index` 各取一个，轮转分配 |
| 板载 un-mute | `AplayWriter.__init__` 里的 `amixer -c 1 sset Speaker on` | 已有 |
| **无变调倍速** | 板端已装 **sox 14.4.2**，带 `tempo` 效果 | 产能不足时轻微加速吸收差额（不变调） |
| 跨卡能力面的既有做法 | `app/core/audio/tts_bridge.py` 的**候选路径解析** | 同传新增能力一律走这个模式 |

### 2.7 硬约束清单（v4）

* **C1 / C2**：ASR committed 4–5 s、unfixed ~1 s。
* **C3**：TTS PCM 4 s 一块 ⇒ 最小音频延迟 4 s。
* **C4**：TTS 合成速率 ≈ **1.02×**（单卡实测）⇒ 单实例产能不足（原则①的关键约束）。
* **C5（v4 重写）**：**同卡跑不了两个完整 TTS 实例，且同卡双实例没有产能价值**。
  * **起不来**：第 2 个实例的 **talker** 在 `rknn3_model_init` 被设备拒绝（`MODEL_SETUP fail, ack = ACK_FAIL`）⇒ 设备端 **model-setup 配额**不够。**不是显存**（失败时卡上还剩 **2362 MB**），**也不是 `core_mask` 选错**（0xFF 对 talker 合法）。
  * **改掩码救不了**：规则是「**掩码置位数 == 模型编译核数**」（speech_decoder/talker/code_predictor 编译 8 核 ⇒ 只认 0xFF；text_projection 编译 1 核 ⇒ 只认单核）⇒ **要少占核只能重新转模型**。
  * **塞进去也没用**：**实测**同卡两实例并发 ⇒ 单请求 0.93 s → **1.54 s**，**聚合吞吐仅 1.20×**（跨卡 2.07×）⇒ 两个 `core_mask=0xFF` 的实例抢同一批 8 核，只能时间片轮转。
  * ⇒ **双实例必须跨卡**；而 **npu2 是禁区 ⇒ 本方案按单实例 + 兜底（DD-4）实施**（DD-5）。
* **C6**：`TtsSession.start()` 当前是**单发**语义（运行中再调抛 `RuntimeError("TTS 正在运行或正在清理")`）⇒ 同传需要**队列化**。
* **C7**：板端**没有 emoji / 符号字体**；界面文案只用普通汉字，图标用内联 SVG（可用 `× ● ■ ▶ ← → ↑ ↓`）。
* **C8**：重启会连坐杀掉 `compute_worker`（systemd `KillMode=control-group`）⇒ `compute.autostart` 必须有值。
* **C9**：宿主 core **不得**硬依赖某个插件包路径；卡片**只能** import `app.core.*`。
* **C10**：浏览器 `AudioContext` 受自动播放策略限制 ⇒ 必须在用户点击开关的同一手势里 `resume()`。
* **C11**：**浏览器麦 L1 丢最旧（2 块 = 1 s）** ⇒ 同传模式下会丢音频，违反原则①。
* **C12**：**浏览器麦 L2 串行 await** ⇒ 上传速率被推理速率钳住。
* **C13**：**SDK 自带 aplay 会阻塞 worker ~3 s/4 s 块** ⇒ 板载输出**必须**走 `AplayWriter`（`play_to=both` 路径），不能用 `play_to=device`。
* **C14**：`_run_safe` 每次都把 PCM 拼成 WAV 落盘 ⇒ 同传（只播不存）应跳过写盘，否则 eMMC 写入被同传灌满。

---

## 3. 目标架构

### 3.1 模块与依赖方向

```
┌──────────────────── 宿主 wdnwebui ────────────────────┐
│                                                       │
│  app/core/audio/tts_bridge.py   ← 扩展（跨卡能力面）   │
│     + submit()  + queue_snapshot()  + queue_clear()   │
│     + subscribe_chunks()  （代理 TTS 卡的 subscribe_audio）│
│     + pick_tts_session()   （双实例轮转，按 npu_index）   │
│                                                       │
│  app/static/core/audio-stream-player.js  ← 新增（共享）  │
│     WebAudio 逐块无缝播放（TTS 卡改用它，行为不变）      │
│                                                       │
│  app/static/core/framework.js / translate.js …（既有）  │
└───────────────────────────────────────────────────────┘
             ▲ import app.core.*            ▲ import app.core.*
             │                              │
┌────────────────┐                  ┌────────────────────────────┐
│ 插件 wdnapps_   │                  │ 插件 wdnapps_audio_chat     │
│ audio_chat     │                  │  cards/qwen3_asr/           │
│  cards/tts/    │                  │    service.py  （+ hook）    │
│   service.py   │                  │    interp.py     ← 新增     │
│   queue.py ← 新增（合成队列）        │    translation.py（复用）   │
│                                │    api.py         （+ 端点）   │
│                                │    static/app.js  （+ 面板）   │
│                                │    static/template.html（+ 面板）│
│                                └────────────────────────────┘
```

**依赖方向规则（遵守 C9）**

* ASR 卡**不 import** TTS 卡 ⇒ 一律走 `app.core.audio.tts_bridge`；
* `tts_bridge` 用**候选路径**解析 TTS 卡（沿用现有 `_TTS_SESSION_MODULES` 模式），插件没装时抛 `RuntimeError("TTS 不可用")` 而非 `ImportError` 崩服务；
* TTS 卡的合成队列**放在插件内**（它要用 `_run_safe` / `_on_audio_chunk` 等私有实现），宿主只暴露**代理接口**。

### 3.2 数据流（浏览器麦 + 板载扬声器，同传模式）

```
浏览器麦克风 ──500 ms/块 s16le──► POST /live/chunk
                                      │ ② L1/L3 缓冲（同传模式放宽，不丢）
                                      ▼
                            ASR worker（qwen3-asr）
                                      │ ③ 每秒一轮：{committed, unfixed, language}
                                      ▼
                    ┌───────────────────────────────────────────┐
                    │ InterpEngine（cards/qwen3_asr/interp.py）  │
                    │  ④ 稳定窗口：快档 = unfixed 稳定前缀        │
                    │           稳档 = 等 committed 确认          │
                    │  ⑤ 切段：endpoint / 标点 / 停顿 / 长度上限   │
                    │  ⑥ 送翻译（独立小队列）                      │
                    │  ⑦ 播前复核：源文是否仍与 ASR 最新文本一致    │
                    │     └ 不一致 ⇒ 作废本条译文，重新等稳定 ⇒ 重译 │
                    └───────────────┬───────────────────────────┘
                                    │ ⑧ 译文（一次性定稿，~0.3 s）
                                    ▼
                    ┌───────────────────────────────────────────┐
                    │ TTS 合成队列（cards/tts/queue.py）          │
                    │  · 双实例并行（npu1 / npu2），轮转分配       │
                    │  · play_to=both：AplayWriter 异步播板载     │
                    │    ⇒ SDK 不自己播 ⇒ worker 不被堵           │
                    │  · sox tempo 按积压深度自适应（1.0–1.15）    │
                    │  · **队列不丢内容**（只丢作废版本）          │
                    └───────────────┬───────────────────────────┘
                                    │ ⑨ PCM（4 s 一块，float32）
                                    ▼
                    AplayWriter（独立线程）──► aplay ──► 板载 ES8388
                    （前端不订阅 PCM ⇒ 浏览器只出字幕，不出声）
```

### 3.3 帧协议

| 帧 | 方向 | 载荷 | 说明 |
|---|---|---|---|
| `interp` | 服务端 → 浏览器 | `{type:"interp", state, mode, lag_sec, pending, spoken, retried, dropped_src, tempo, queue_depth}` | 同传状态与滞后指标（1 s 一帧足够） |
| `interp_speak` | 服务端 → 浏览器 | `{type:"interp_speak", seq, source, text, lang, state}` | 「这句将被播报」/「这句因源文被改写而重译」 |
| `translation` | 服务端 → 浏览器 | 既有帧 | 字幕译文照旧（与译音两条时间线） |
| 二进制 PCM | 服务端 → 浏览器 | 12 字节头（sr, ch, n）+ float32 | **同传默认不推**（前端不订阅）⇒ 仅板载出声 |

---

## 4. 延迟预算与吞吐预算

### 4.1 延迟（原则③ 的代价）

以「人说完这句话的第 0 秒」为原点：

| 环节 | 耗时 | 备注 |
|---|---|---|
| ① 浏览器麦采集 + 上传 | 0.1–0.5 s | 500 ms 一块；L2 串行 ⇒ 推理慢时排队（`lag_ms` 可见） |
| ② 音频凑满 1 s + ASR 推理 | ~1.0 s | 1 秒一块增量解码 |
| ③ 文本可用 | 快档 ~1.0 s（unfixed）/ 稳档 4–5 s（committed） | C1 / C2 |
| ④ **稳定窗口** | 快档 1–2 s / 稳档 0 s（committed 已确认） | **可调**，DD-2（原则③） |
| ⑤ 翻译（一次性定稿） | 0.3 s（最坏 ~1 s 重试） | |
| ⑥ **播前复核** | ~0 s | 只是字符串比对 |
| ⑦ TTS 首帧 | 2.8–3.3 s | |
| ⑧ 首块音频出声 | 0 s | 首帧即含可播音频 |
| **合计（快档）** | **5.2–6.5 s** | |
| **合计（稳档）** | **9.5–11 s** | 一个词都不会错，但慢 |

### 4.2 吞吐（原则① 的成败点，v4 实测）

设源语总时长 `T_src`，译音总时长 `T_out = T_src × k`（k = 语言对膨胀比）。

| 环节 | 速率 |
|---|---|
| TTS 合成（**单卡**，实测） | **1.02×** 实时 |
| TTS 合成（**同卡两实例**，实测） | **仅 1.20×**（时间片轮转，不是真并行）⇒ **不作为提产能手段** |
| TTS 合成（**双卡**，实测） | **≈ 2.07×** 实时（保守 1.92×）—— ⚠️ **但 npu2 是部署禁区 ⇒ 不可用** |
| 板载播放（aplay） | 1.00× 实时（受 `tempo` 可调） |
| 翻译（多实例轮转） | ~3 句/秒 ⇒ 相对 1 句/3 s 的语速，余量充足（**前提：本地 hy-mt-1-5 实例在跑**，见 §2.1 注） |

| 语言对 | 膨胀比 k（估） | 单卡 1.02×（**当前部署形态**） | 双卡 2.07×（npu2 禁 ⇒ 不可用） |
|---|---|---|---|
| 英→中 / 日→中 / 德→中 | 0.8–1.0 | **够（余量 2–25%）⇒ 滞后恒定** | 够（余量 >100%） |
| 中→英 | 1.15–1.30 | **不够 ⇒ 必然越积越多** | 够（余量 60–80%） |
| 中→日 | ~1.4（估算，未实测） | **不够** | 临界 ⇒ 需 `tempo` |
| 任意 + 快速连续讲话 | k × 语速 | 不够 | 靠 `tempo` 与排队兜底 |

⇒ **策略（三层，已按用户决策落定）**：

1. **常态（当前部署）**：**单实例 1.02×** ⇒ **外语 → 中文方向滞后恒定、内容全播**；**中译英方向必然越积越多**（物理必然，非实现缺陷）。
   ⚠️ **npu2 是部署禁区（用户 2026-11-21 确认）⇒ 双卡这一层不可用** ⇒ 降级方案见 §10.2。
2. **产能仍不足时**（中译英 / 极端语速）：`sox tempo` **按积压深度自适应**（1.0 → 1.05 → 1.15，**不变调**，sox 14.4.2 已装）⇒ 吸收 0–15% 差额。
3. **仍不足时**：**不丢内容**，滞后缓慢增长，UI 实时显示「译音落后 N 秒（语速超过译音产能）」并提示用户放慢语速或暂停。**这是唯一允许的降级，且不违反原则①。**

> ⚠️ **膨胀比 k 未实测**（不同语言对的译音时长比）。P0 第一件事就是实测：对 10 种可发声语言各取 3 句，实测「源句时长 vs 译音时长」⇒ 得到 k 表 ⇒ 决定 `tempo` 的触发阈值**以及哪些语言对在单卡下能做到恒定滞后**。**不要拍脑袋填。**

---

## 5. 关键设计决策（v4）

### DD-1　同传只吃 `unfixed`（快档）/ `committed`（稳档）

* **理由**：committed 滞后 4–5 s（C1），用它做同传总延迟 9.5–11 s；unfixed 只滞后 ~1 s（C2）。且「interim 先播、final 校正」是 Azure 标准 API 的行业做法。
* **两档可切换**（用户决策）：**快档** = unfixed + 稳定窗口（~6 s 滞后）；**稳档** = 等 committed 确认（~10 s 滞后，一个词都不会错）。
* **不变量复用**：回看轮次不广播、退化时 `unfixed=""` ⇒ **同传自动不会播回看内容、不会播模型胡话**（稳档同理：committed 是剔除回看后的公开文本）。

### DD-2　稳定窗口（快档）：unfixed 的**最长稳定前缀**连续 N 轮不变才成段

* **做法**：逐轮记录 `unfixed`；求它与上一轮的**最长公共前缀**；该前缀连续 N 轮（N 默认 2）**内容不变** ⇒ 认为这段文本已定型 ⇒ 切点落在它末尾。
* **理由**：ASR 已有同款思路且已验证 —— `_pause_rounds`（按**内容**判停顿，不按长度）在 150 秒素材里把误切从 21 次降到 1 次。同传复用这套判据，等于「白拿」一次消错。
* **参数**：`INTERP_STABLE_ROUNDS = 2`（≈1–2 s）、`INTERP_MAX_CHARS = 40`、`INTERP_MIN_CHARS = 8`。

### DD-3　**播前复核源文**；不一致则作废重译（原则③ 的落点）

* **背景**：翻译是**一次性定稿**（`_infer` → `session.respond` 返回完整文本，无增量部分）⇒ **译文返回即稳定**。真正的风险是：翻译这 0.3–1 s 里，ASR 把源文改了 ⇒ 译文对应的是**旧版本**。
* **做法**：译文到达、提交 TTS **之前**，比对当前 ASR 最新文本与本段源文：
  * 一致 ⇒ 译文有效 ⇒ 入 TTS 队列；
  * 不一致 ⇒ **本次译文作废**（不是「丢内容」—— 源文被改写后会有新版本顶上）⇒ 回到 DD-2 重新等稳定 ⇒ 重译。
* **稳档**：committed 路径下源文基本不会变，复核多为「通过」⇒ 重试次数应显著少于快档。
* **UI**：作废次数计入 `interp.retried`，明示「这句被重译」；同一段最多重试 N 次（默认 3），超过则放弃该段并在字幕标注（极端情况下的**唯一**内容损失，必须计数上报）。

### DD-4　**不丢内容**：靠产能而不是丢弃（原则①）

* **取消** v1 的「队列满丢最旧」。合成队列**只丢作废版本**（DD-3 的重译旧版），**绝不丢有效句**。
* **产能三件套**：
  1. **流水线化**：板载播放走 `AplayWriter`（异步线程）⇒ 合成不等人听完 ⇒ 句 N 播着，句 N+1 已在合成。
  2. **双 TTS 实例（跨卡）**：实测 2.07×（同卡起不来、且只有 1.20× ⇒ 必须跨卡，DD-5）。
     ⚠️ **但 npu2 是部署禁区 ⇒ 这一条当前不可用** ⇒ 正式形态退化为「单实例 + 下面第 3 条 + 允许滞后增长」。
  3. **`sox tempo` 自适应**：按积压深度 1.0 → 1.15 吸收剩余差额。
* **绝不丢** ⇒ 队列**无界或大深度**（例如 64 段 ≈ 3 分钟音频）+ 明确的「积压深度」指标；极端情况下让滞后增长，不让内容消失。
* **与字幕翻译的关系**：字幕那套翻译队列（深度 256，丢最旧）**保持不变** —— 字幕是「历史可回看」，同传是「当下不可缺」，两者语义不同，不能混用（DD-3 的独立小队列）。

### DD-5　双 TTS 实例必须**跨卡**（v4 实测重写）

* **同卡为什么不行**（v4 完整根因，详见 §2.3 实验 1/4/5/8）：
  * 表面错误：`AssertionError: init talker runtime failed`（`wdnai/src/wdnai/backends/tts.py:1136`，底层 `MODEL_SETUP fail, ack = ACK_FAIL`）。
  * **不是卡上内存不够**：单个完整 TTS 实例在卡上占 **1942 MB**（`rknnsmi` 实测，5 GB 卡）；第二个实例失败时卡上**还剩 2362 MB**。
  * **不是 `core_mask` 选错**：0xFF 对 talker **合法**（实测能起来）⇒ 失败在**设备端 model-setup 配额**。
  * **改掩码救不了**：规则是「掩码置位数 == 模型编译核数」⇒ speech_decoder / talker / code_predictor（8 核编译）只认 0xFF ⇒ **要少占核必须重新转模型**。
  * **顺带澄清**：`max_replicas_per_card`「每卡两份」机制**本身可用**（`hy-mt-1-5` 纯 LLM，一个 runtime）⇒ **TTS 不适用是因为它一个实例 = 3 个 8 核上下文 + 2 个 LLM session，不是机制缺陷**。
* **同卡即便塞进去也没用**（v4 从推论变成实测）：卡 0 上两个 hy-mt-1-5 实例并发 ⇒ 单请求 **0.93 s → 1.54 s**，**聚合吞吐 1.08 → 1.30 req/s（仅 1.20×）** ⇒ 两个 `core_mask=0xFF` 的实例**共用同一批 8 核**，只能时间片轮转 ⇒ 对「一句都不丢」帮助有限（跨卡实测 **2.07×**）。
* **跨卡可行**：`POST /api/compute/instances {model:"qwen3-tts", npu_index:2}` ⇒ 30 s 内 running；并发总产能 **2.07×**，单卡延迟只膨胀 6%。
* ⚠️ **npu2 是部署禁区 ⇒ 跨卡这条路当前不可用** ⇒ 实施时按「单实例 + DD-4 的 tempo/滞后兜底」跑；下面的多实例改造（per-request socket、播放串行化）**先不做**，等有第二张可用卡再上。
* **接入方式（将来启用时）**：`registry.get_session(capability, npu_index=...)` / `list_sessions()` 可按卡号取 ⇒ 队列 worker **轮转**分配到两个 session（翻译侧 `pick_session()` 是同款模式，直接照抄）。
* **必要重构（将来启用时）**：PCM 的 AF_UNIX socket 现在是**实例级**（`self._pcm_sock_path`）⇒ 两个 session 并行时会串。必须改成 **per-request 参数**（`_infer_one(req, pcm_sock_path)`），socket 随请求创建/销毁。
* **播放串行化（将来启用时）**：两个实例可以**并行合成**，但 PCM 必须**按提交顺序**送进同一个 `AplayWriter` ⇒ 队列里维护「下一句该播的序号」，乱序到达的先存着。
* **降级**：只有 1 个实例时（**npu2 不可用** / 被别的功能占了）⇒ 单实例降级运行，UI 提示「当前只有 1 个 TTS 实例，中译英场景可能逐渐滞后」。

### DD-6　板载扬声器输出：走 `play_to=both` + 前端不订阅 PCM

* **为什么不能用 `play_to=device`**：那会让 SDK 自己 aplay，**64 KB OS pipe 填满会阻塞 worker ~3 s/4 s 块**（C13，`cards/tts/service.py:55-62` 记录了这个坑）⇒ 合成被播放阻塞 ⇒ 产能掉到 0.5×。
* **做法**：`play_to=both` ⇒ `no_play=True`（SDK 不自己播）+ `AplayWriter`（独立线程）异步写 aplay ⇒ **合成与播放真正并行**。
* **只出声在板载**：`_on_audio_chunk` 只推给**订阅者** ⇒ 同传时前端**不订阅** PCM ⇒ 浏览器不响，只有板载扬声器出声（也避免浏览器与板载双路叠加造成回声叠加）。
* **落盘**：`play_to` 含 device 时跳过 WAV 写盘（C14）⇒ 不灌 eMMC。

### DD-7　浏览器麦输入：放宽缓冲 + 暴露丢块（原则① 的上游）

* **L1（前端）**：`LIVE_MAX_PENDING = 2` 块（1 s）⇒ 推理稍慢就丢最旧 ⇒ **同传模式下放宽到 20 块（10 s）**，并把丢块数显示出来。
* **L3（服务端）**：`_LIVE_MAX_BUFFER_CHUNKS = 3` ⇒ 同传模式放宽到 20 块。
* **熔断**：`_LIVE_MAX_CONSECUTIVE_DROPS = 5` 保持（这是**故障保护**，不是正常丢块）。
* **L2 串行**：无法改成并行（SDK 会话串行），但放宽缓冲后，推理的抖动会被缓冲吸收 ⇒ `lag_ms` 反映真实滞后。
* **UI**：同传面板显示「上行滞后 xxx ms」「丢块 N」。

### DD-8　只做 1 个目标语种

* **理由**：双实例已经全用在「同一目标语种的两个并行 worker」上 ⇒ 再加目标语种需要第 3 个 TTS 实例。
* Azure Live Interpreter 的「2 个目标」是云端算力下的能力，本板不具备。

### DD-9　目标语必须在「可发声子集」内，否则自动降级为仅字幕

* 翻译支持 **37 种**，TTS 支持 **10 种可发声**（§2.4）⇒ 交集只有 **10 种**。
* 不在交集 ⇒ **不入 TTS 队列**，字幕译文照常出，UI 标注「该语言不支持语音输出，仅显示字幕」。

### DD-10　同传与「语音生成」卡互斥，冲突时同传优先

* 同一个 TTS session 池 ⇒ 手动合成与同传抢实例。
* 做法：手动合成请求**入同一个队列**，同传句**优先**（插队 + 丢最旧的 UI 请求）；UI 侧提示「同传进行中，手动合成已排队」。

### DD-11　实时翻译开关与同传开关的关系

* **同传开启 ⇒ 自动打开实时翻译**（同传是实时翻译的超集：字幕译文 + 译音）。
* **关闭实时翻译 ⇒ 同传自动降级为仅字幕**（字幕译文也没了 ⇒ 等于关掉）。

---

## 6. 边界问题清单（v4）

每条给「问题 → 后果 → 对策」。**粗体**的是不做就会出故障或违反用户原则的。

### A. 稳定性（原则③）

| # | 问题 | 后果 | 对策 |
|---|---|---|---|
| A1 | **假设文本会被改写** | 译音把话说错，且**音频已播出无法撤回** | DD-2 稳定窗口 + DD-3 播前复核；UI 标注「译音基于实时识别」 |
| A2 | 译文本身会变吗 | —— | **不会**：翻译是一次性定稿（实测）⇒ 稳定问题只在源文侧 |
| A3 | **模型退化** | 念出「human rights violations」这类胡话 | **复用既有不变量**：退化期 `frame["unfixed"]=""` ⇒ 同传自动静默 |
| A4 | **回看重放** | 把刚说过的话再念一遍 | **复用既有不变量**：回看轮次不广播 ⇒ 同传收不到 |
| A5 | 重译导致同一段出现两次 | 观众听到重复 | DD-3 的作废译文**不入队** ⇒ 只有最终版本会播 |
| A6 | 源语中途切换 | 翻译参数错 | 源语跟随 ASR 的 `language`（**只在非退化轮更新**）；目标语不变 |
| A7 | 源语 == 目标语 | 翻译服务返回 `skipped` | 默认**不朗读**；可选「原文朗读」 |

### B. 产能与「一句都不能丢」（原则①）

| # | 问题 | 后果 | 对策 |
|---|---|---|---|
| B1 | **TTS 单卡合成速率只有 1.02×** | 中译英等语言对**必然**越积越多 | **跨卡双实例**（DD-5）⇒ 实测 2.07×；⚠️ **npu2 禁 ⇒ 当前按单实例实施**，靠 `tempo` + 允许滞后增长兜底 |
| B2 | **同卡跑不了两个完整 TTS 实例**（设备端 model-setup 配额；且改掩码救不了 ⇒ 要重转模型） | 「同卡双份」这条路既起不来、也没收益（**实测聚合吞吐仅 1.20×**） | 实测已证伪（§2.3 实验 1/4/5/7/8）；**不重转 model**（Q8） |
| B3 | **播放阻塞合成**（SDK 自带 aplay） | 产能掉到 ~0.5× | DD-6：`play_to=both` + AplayWriter 异步 |
| B4 | 译音时长 > 源语时长 | 积压 | 单实例下用 `tempo` 自适应（覆盖到 k ≈ 1.17）+ 允许滞后增长 |
| B5 | 产能仍不足 | 滞后增长 | **不丢内容**，滞后增长 + UI 提示（唯一允许的降级） |
| B6 | 句子太碎 | 每句都要一次 TTS 往返，首帧 3 s 占比高 | 实时路径用 **endpoint**（静音 1.2 s）切句 ⇒ 天然是完整句 |
| B7 | 翻译重试（最多 2 条措辞） | 某句翻译耗时翻倍 | 翻译侧有 2 个实例轮转 ⇒ 不阻塞其它句 |
| B8 | 队列无界 | 极端情况下内存涨 | 队列深度上限 64 段（≈3 min 音频）+ 明确的积压指标 |

### C. 输入侧（浏览器麦，原则②）

| # | 问题 | 后果 | 对策 |
|---|---|---|---|
| C1 | **L1 前端队列丢最旧（2 块）** | **丢音频 ⇒ 丢内容**（违反原则①） | DD-7：放宽到 20 块 + 显示丢块数 |
| C2 | L2 串行 await | 上传被推理钳住 ⇒ 输入侧积压 | 放宽缓冲吸收抖动；`lag_ms` 可见 |
| C3 | 服务端 L3 缓冲 3 块 | 推理抖动即丢块 | DD-7：放宽到 20 块 |
| C4 | 熔断（连续丢 5 块） | 会话中断 | 保持（故障保护，不是正常丢块） |
| C5 | 浏览器麦克风权限/设备切换 | 没声音 | 沿用现有 UI 的权限与设备处理 |
| C6 | 采样率/编码不一致 | 识别乱码 | 沿用现有 500 ms s16le 通道（已验证） |

### D. 输出侧（板载扬声器，原则②）

| # | 问题 | 后果 | 对策 |
|---|---|---|---|
| D1 | **回声自激** | 正反馈自激，设备自说自话 | 浏览器麦在远端 ⇒ 衰减大（比板载麦好）；仍需提示「让麦克风远离板子 / 用耳机」 |
| D2 | 板载 codec 未开 | 没声音 | `amixer -c 1 sset Speaker on`（`AplayWriter` 已有） |
| D3 | aplay 写 stdin 阻塞 | 播放卡顿 | `AplayWriter` 的独立 drain 线程（非阻塞） |
| D4 | 浏览器自动播放策略 | 将来若加浏览器出声会被拦 | 开关点击的同一手势里 `resume()` |
| D5 | 每次合成都落 WAV | eMMC 被同传写满 | DD-6：device 模式跳过写盘 |
| D6 | 译音与原声重叠 | 观众听不清 | 滞后是同传的固有属性；不做 ducking（板端无混音） |

### E. 资源与并发

| # | 问题 | 后果 | 对策 |
|---|---|---|---|
| E1 | 第二个 TTS 实例吃 ~965 MB RSS | 整机可用内存 2865 → 1866 MB | 可接受；必要时先停 embedding 实例 |
| E2 | **npu2 不可用**（若「只能用两张卡」是硬约束） | 双实例方案落空 | 降级为单实例 + `tempo` 自适应 + 允许滞后增长 + UI 提示 |
| E3 | 服务重启 | 同传中断、队列丢失 | best-effort；**重启前确认 `compute.autostart` 有值**（C8） |
| E4 | 浏览器离开卡片 | 后台继续烧 NPU / 继续出声 | 复用 `DemoGuard`（切卡 / `beforeunload` / `hashchange`）⇒ 调 `POST /api/qwen3-asr/interp/stop` |
| E5 | 同传与「语音生成」卡抢实例 | 互相报错 | DD-10：统一队列，同传优先 |

### F. 产品、交互与合规

| # | 问题 | 后果 | 对策 |
|---|---|---|---|
| F1 | 用户预期 | 以为「实时」＝ 零延迟 | UI 常驻「译音滞后约 6 秒（快档）/ 10 秒（稳档）」+ 实时滞后秒数 |
| F2 | 录音隐私 | 会议同传涉敏感信息 | 卡片说明写清「音频在板端本地处理，不上云」；不新增网络出口 |
| F3 | 译音与最终字幕不一致 | 用户以为识别错了 | 标注「译音基于实时识别结果，可能与最终字幕略有出入」 |
| F4 | 目标语不支持语音 | 点了没声音 | DD-9 自动降级 + UI 说明 |
| F5 | 板端字体 | 文案变豆腐块 | 只用普通汉字 + 内联 SVG（C7） |
| F6 | 「不丢句」与「实时」冲突时 | 用户以为坏了 | 滞后增长时明确显示「译音落后 N 秒（语速超过译音产能）」，**不静默丢内容** |
| F7 | 重译 | 观众觉得卡顿 | `interp.retried` 计数可见；重试次数超限才放弃（并计数） |

---

## 7. 实施计划（v4）

> 横跨**两个仓**：宿主 `wdnwebui` 与插件 `wdnapps_audio_chat` ⇒ 提交/推送按 AGENTS.md 分仓走（commit message 用英文；push 先问；先局域网主仓库后 GitHub；Gitee 不碰）。

### P0-0　先实测几件事（**不写代码，先出数据**）

| # | 实测 | 状态 |
|---|---|---|
| M1 | **各语言对膨胀比 k**（源句时长 vs 译音时长） | ⬜ 待做：对 10 种可发声语言各取 3 句 ⇒ 得 k 表 ⇒ 定 `tempo` 触发阈值 |
| M2 | **跨卡双 TTS 实例可行性 + 并发产能** | ✅ **已完成**（§2.3）：同卡 ❌（起不来 + 只有 1.20×）/ 跨卡 ✅ 2.07× |
| M3 | **npu2 在本机是否允许使用** | ✅ **已确认（用户 2026-11-21）：部署禁区，不能用** ⇒ 产能按单实例 1.02× 设计 |
| M4 | **同卡双实例到底能不能并行** | ✅ **已完成**（§2.3 实验 7）：同卡两实例聚合吞吐 **1.20×**（不是 2×）⇒ 不为同卡双实例重转 model |

### P0-1　最小可用闭环

| # | 仓库 | 文件 | 改动 |
|---|---|---|---|
| 1 | 宿主 | `app/core/audio/tts_bridge.py` | 扩展能力面：`submit()` / `queue_snapshot()` / `queue_clear()` / `subscribe_chunks()` / **`pick_tts_session()`（按 `npu_index` 取；npu2 禁 ⇒ 当前只有 1 个，将来加卡即可多实例轮转）**；沿用候选路径解析，插件缺失时抛 `RuntimeError("TTS 不可用")` |
| 2 | 插件 | `cards/tts/queue.py`（新） | 合成队列：**不丢内容**；**per-request PCM socket**（为将来多实例并行预留）；乱序到达按序号串行播放；`tempo` 参数透传（自适应调节） |
| 3 | 插件 | `cards/tts/service.py` | `start()` 由「单发抛错」改为「入队」；`_infer_one(req, pcm_sock_path)` 去掉实例级 socket；`play_to` 含 device 时跳过 WAV 落盘；`_on_audio_chunk` 支持「只播设备」标记 |
| 4 | 插件 | `cards/qwen3_asr/interp.py`（新） | `InterpEngine`：**快/稳两档**（DD-1/DD-2）、**播前复核**（DD-3）、独立小翻译队列、TTS 提交、重译/放弃计数、积压深度指标 |
| 5 | 插件 | `cards/qwen3_asr/service.py` | `_on_delta` 中把 `frame["unfixed"]` + `language` + `degenerate` 喂给 `InterpEngine`（**只加钩子，不改既有切句/翻译逻辑**）；实时路径在 `endpoint` 处触发同传切段 |
| 6 | 插件 | `cards/qwen3_asr/api.py` | 新增 `/interp/status` `/interp/settings` `/interp/start` `/interp/stop`；WS 增加 PCM 二进制帧通道（默认不订阅） |
| 7 | 宿主 | `app/static/core/audio-stream-player.js`（新） | 共享 WebAudio 逐块无缝播放器（TTS 卡改用它，行为不变） |
| 8 | 插件 | `cards/tts/static/app.js` | 改用共享播放器（**纯重构，行为不变**） |
| 9 | 插件 | `cards/qwen3_asr/static/app.js` | 浏览器麦缓冲放宽（DD-7）+ 同传面板交互 + 滞后/重译/丢块/tempo 指标 |
| 10 | 插件 | `cards/qwen3_asr/static/template.html` | 新增「同声传译」面板：开关、**档位（快/稳）**、目标语（复用 `#asr-tr-target`）、音色（`GET /api/tts/voices`）、播放目标（板载/浏览器）、滞后与重试计数 |
| 11 | 宿主 | `config.jsonc` | ⚠️ **npu2 是部署禁区 ⇒ 本次不加 `{"model":"qwen3-tts","npu_index":2}`**（将来解禁时再加；队列/pick_tts_session 按多实例写好即可） |

**P0 明确不做**：不动 SDK、不动既有字幕/译文渲染结构、不新建卡片、不做多目标语种、不做浏览器出声（同传只走板载）、**不为同卡双实例重转模型**（Q8）。

### P1　打磨

* 滞后指示器（实时秒数）与「译音落后 N 秒」告警；
* `tempo` 自动调节（按积压深度自适应 1.0–1.15，已在 P0 埋钩子）；
* 重译原因可视化（源文被改写 / 模型退化 / 翻译重试）；
* 语种自动跟随的可见化；
* 「仅字幕」模式（目标语不支持语音时自动进入）。

### P2　端到端路线评估

* 评估 `Qwen3-Omni-Flash` 在本板的可行性（内存、RTK 版本、SDK 适配）；
* 端到端天然满足原则③（一次生成到底，不需要稳定窗口与复核）；
* 板端 TTS 与 Omni 同源（语言集完全一致）⇒ 接口不用重做。

---

## 8. 验收标准与回归

### 8.1 功能验收（真机，浏览器麦 + 板载扬声器）

| # | 验收项 | 判据 |
|---|---|---|
| V1 | 打开同传开关（快档） | ≤ 8 s 内听到第一句译文 |
| V2 | 打开同传开关（稳档） | ≤ 12 s 内听到第一句译文 |
| V3 | **连续讲话 60 s**（**外语 → 中文**方向） | **滞后不增长**（内容全播，无丢弃）；快档滞后 ≤ 8 s |
| V4 | **连续讲话 300 s** | 滞后稳定或缓慢增长但**内容零丢弃**；`dropped_content = 0` |
| V5 | 中→英（膨胀比最大的方向） | ⚠️ **单卡 1.02× 下必然越积越多** ⇒ 验收判据改为「**内容零丢弃 + 滞后如实显示 + tempo 生效**」，**不要求滞后恒定**（恒定需双实例 ⇒ npu2 禁） |
| V6 | 源文被改写（人为触发自我更正） | 旧译文**不播**；新译文播出；`retried` 计数 +1 |
| V7 | 模型退化注入 | 退化期间**没有**译文音频播出 |
| V8 | 拖动进度条（触发回看） | 不重复播已播内容 |
| V9 | 目标语选泰语 | 自动降级为仅字幕 + UI 说明 |
| V10 | 离开卡片 | ≤ 2 s 停止播放，TTS 队列清空 |
| V11 | 板载外放 + 浏览器麦 | 无自激（人工确认：连续听 1 分钟不出现自说自话） |
| V12 | 长时间（10 分钟） | 无内存增长、无 WS 帧堆积、无 worker 崩溃 |
| V13 | **上行丢块** | 正常推理速度下 `dropped_src = 0`（DD-7 生效） |
| V14 | **TTS 实例数为 1**（当前部署的常态：npu2 禁 ⇒ 只有 1 个） | UI 如实显示「当前 1 个 TTS 实例」；功能仍可用；中译英方向允许滞后增长但**内容零丢弃** |
| V15 | 将来解禁第二张卡后 | 自动识别到 2 个实例 ⇒ 轮转分配 ⇒ 滞后回到恒定（验收判据同 V3） |

### 8.2 回归脚本（新增）

* `scripts/verify_asr_interpretation.py` —— **离线单元**（不占 NPU）：
  稳定前缀算法（连续 N 轮不变才成段）、最小/最大段长、**播前复核（源文被改写 ⇒ 作废重译）**、重译次数上限、**队列不丢内容**（灌 100 条 ⇒ 全部送达）、语言映射表（10 可发声 / 27 降级）、退化期静默、回看期静默、代数作废（清空后旧句不播）、`tempo` 参数透传、快/稳两档切换。
* `scripts/verify_asr_interp_live.py` —— **真机**：浏览器麦或文件 ⇒ 断言有译音帧、滞后在预算内、`dropped_content = 0`、重译计数正确。

### 8.3 既有回归必须全绿（改插件 `.py` 后要 `sudo systemctl restart wdnwebui`）

`verify_asr_translation`（77）/ `verify_asr_video_dual_subtitle`（24）/ `verify_asr_file_translate_ui`（19）/ `verify_asr_file_translation_e2e`（18）/ `verify_asr_seek_subtitle`（15）/ `verify_asr_wizard_translate`（28）/ `verify_asr_translate_lang_labels`（16）/ `verify_asr_live_smoke`（8）/ `verify_asr_model_degeneration`（34）/ `verify_asr_translate_toggle`（16）

⚠️ **回归纪律**（既有踩坑）：不要并发跑；`DELETE /api/compute/active-app` 会连带清空 `compute.autostart` ⇒ 用 `scripts/active_app_guard.install(ctx)` + `install_active_app_spoof(ctx)`；`verify_asr_wizard_translate` 结尾会停掉翻译模型、`verify_asr_translate_lang_labels` 会把 `enabled` 设回 false ⇒ 跑完要复原。

---

## 9. 明确不做的事（防跑偏）

* **不新建卡片** —— 同传是 ASR 卡的一个模式（与「实时翻译」并列）。
* **不改 SDK**（`wdnai`）—— 4 秒块、回滚窗口、同卡单 talker runtime 都是既有行为，方案在应用层绕开。
* **不做端到端模型** —— 30B-A3B 板端跑不动；Flash 评估放 P2。
* **不做多目标语种同时播** —— 产能优先给一个目标语种（当前单实例 ⇒ 更要集中）。
* **不做 AEC / 降噪 / 会议级混音** —— 板端没有这些能力。
* **不碰既有字幕与译文渲染结构** —— 同传是叠加层。
* **不改 `compute.autostart` 语义** —— 只增加一条实例条目，不改机制。
* **不做浏览器出声的同传**（P0）—— 用户明确要板载扬声器输出。

---

## 10. 决策状态

### 10.1 已落定（用户 2026-11-21 拍板）

| # | 决策 | 结论 |
|---|---|---|
| Q1 | 第二个 TTS 实例 | **跨卡启用**（同卡实测超限，见 §2.3 实验 4–9） |
| Q2 | 稳定口径 | **两档都做，界面可切换**：快档（稳定窗口 ~6 s）/ 稳档（等 committed ~10 s） |
| Q3 | 产能兜底 | **两者都要，按积压深度自动切换**：`tempo` 1.0→1.15 加速，不够则允许滞后增长（内容仍不丢） |
| **Q4** | **npu2 是部署禁区** | ✅ **已确认（用户 2026-11-21）** ⇒ 跨卡双实例**不可用** ⇒ 产能天花板 = **单卡 1.02×** ⇒ 见 §10.2 |
| **Q9** | **npu2 禁用后，同卡双 TTS 实例还有没有戏**（用户 2026-11-21 追问） | ❌ **没有**。两条路都堵死：① **起不来**（第 2 个实例的 talker 被设备端 model-setup 配额拒绝，失败时显存充足）；② **即便起得来也不提产能** —— 卡 0 上两实例并发实测，**聚合吞吐仅 1.20×**（单请求 0.93 s → 1.54 s），跨卡是 2.07×。⇒ 见 §2.3 实验 7–9 |

### 10.2 npu2 禁区下的产能结论（v4 更新）

**硬事实**：

* 单卡产能 **1.02×**（实测）⇒ 译音总时长若 > 源语总时长（k > 1.02），滞后必然随讲话时长线性增长。
* 同卡双实例**起不来**（第 2 个实例的 talker 被设备端 model-setup 配额拒绝，失败时卡上 free 2362 MB ⇒ 非显存）。
* 同卡双实例**即便重转 model 做成也不提产能** —— **实测**（§2.3 实验 7）：卡 0 上两个 hy-mt-1-5 实例并发，单请求 0.93 s → 1.54 s，**聚合吞吐 1.08 → 1.30 req/s，仅 1.20×**（跨卡是 2.07×）⇒ 两个 `core_mask=0xFF` 的实例抢同一批 8 个核，只能时间片轮转。
* ⇒ **单卡 + 不丢内容 = 滞后可能缓慢增长**，这是物理必然，不是实现问题。

**产能天花板对照表**：

| 方案 | 可行性 | 聚合产能 | 结论 |
|---|---|---|---|
| **单卡 1 实例** | ✅ | **1.02×** | **采用**（npu2 禁区的唯一可行形态） |
| 同卡 2 实例（现状模型） | ❌ 起不来 | — | 设备端 model-setup 配额挡住 |
| 同卡 2 实例（重转成 4 核） | ⚠️ 需重转 model + 改 SDK | **≈1.0–1.2×**（核被切开，单请求变慢） | **不划算 ⇒ 不做** |
| 跨卡 2 实例 | ✅ | **2.07×** | **npu2 禁 ⇒ 不可用** |

**按语言对分解（k = 译音时长 ÷ 源语时长，待 M1 实测校准）**：

| 语言对 | k | 单卡 1.02× | 后果 |
|---|---|---|---|
| 英→中 / 日→中 / 德→中 | 0.8–1.0 | **够** | **滞后恒定** ✔ 完全满足「不丢句 + 实时」 |
| 中→英 | 1.15–1.30 | 不够 | 滞后按 (k−1.02) × 讲话时长增长；10 分钟连续讲话 ≈ 滞后 +100 s |
| 中→日 | ~1.4 | 不够 | 更明显 |

**降级方案（npu2 禁区的正式对策）**：

1. **内容零丢弃**（原则① 保持）：队列不丢有效句，只丢作废版本。
2. **`tempo` 自适应**：积压深时按 1.0 → 1.05 → 1.15 加速（不变调）⇒ 吸收 0–15%；`tempo` 上限 1.15 时可覆盖 k ≈ 1.17。
3. **滞后如实显示**：UI 常驻「译音落后 N 秒」+ 语速超产能提示（原则① 的诚实兑现）。
4. **语言对预判**：按 M1 实测的 k 表，在选目标语种时**预先提示**：「该语言对译音更长，译音会逐渐落后（内容不会丢）」。
5. **可考虑的方向**：若主要场景是**外语 → 中文**（k ≤ 1.0），单卡即可做到**恒定滞后 + 一句不丢** ⇒ 产品的核心卖点成立。

### 10.3 其余待定（按报告默认值实施，不阻塞）

| # | 决策 | 默认值 |
|---|---|---|
| Q5 | 板载输出时浏览器是否也出声 | **只板载**（符合你的要求，也减少回声叠加） |
| Q6 | 同语种是否朗读 | **不朗读** |
| Q7 | 不可出声的 27 种语种 | **标注**（不隐藏，避免「这板不支持泰语」的误解） |
| **Q8** | **是否为「同卡双实例」重新转 model**（用户 2026-11-21 追问） | **不做** —— 理由是**两条**，不是一条：① 要让实例**起得来**确实必须重转（`core_mask` 规则 = 编译核数，改不了掩码）；② 但**产能不增**：同卡两实例实测聚合吞吐只有 **1.20×**（实验 7），且重转成 4 核后单请求会变慢，收益上限 ≈1.0–1.2×。⇒ **投入产出不成正比 ⇒ 不转、不改 SDK** |

---

## 11. 未取证 / 待确认

* **NVIDIA Riva / Google Cloud 实时翻译**的文档页抓取失败（404 / 网络失败）⇒ 报告未引用其具体指标；若需要对比这两家，需要用户侧提供可访问链接或允许换检索路径。
* **语言对膨胀比 k 未实测**（§4.2）⇒ P0-0 的 M1 必须先做，否则「`tempo` 阈值」是估算而非结论。
* **TTS 4 秒块**是 SDK 行为 ⇒ 若 SDK 后续把块变小，首帧延迟可能降到 ~1 s，延迟预算要重算（产能不变）。
* **板载 codec（ES8388）在同传场景的底噪与拾音**未实测 ⇒ D1 的自激风险用「浏览器麦在远端 + 提示」规避，不靠实测调参。
* **npu2 是部署禁区**（用户 2026-11-21 确认）⇒ 跨卡双实例不可用；同卡双实例**实测起不来**（设备端 model-setup 配额），且**即便重转 model 做成也只到 1.20×**（实验 7）⇒ 结论与对策见 §10.2。
* **驱动内部 model-setup 配额的精确计费规则未逆向出来** —— 只拿到边界值（普通 1 核 24 个 / 普通 8 核 4 个 / llm 8 核 ≥8 个 / 小模型会挤占大模型）。这不影响结论（两条路都堵死），但若将来要复用这张卡的余量，得再逆向一次。
* **`init_llm_session` 同一 context 多 session** 驱动允许（实测 6 个）⇒ 若将来需要「1 组模型服务多路请求」，这是一个**不用重转模型**的技术点；但同一 context 上的多 session 是**串行**的，**不解决产能问题**。

---

## 12. 本轮（v4）硬件验证的复现方式

§2.3 的 9 组实验全部有脚本，**已固化进仓**（`/tmp` 是 tmpfs，重启即失）：

```
tools/probes/tts_same_card/
├── README.md                  # 用法 + 踩过的坑（必读）
├── scan_core_mask_models.py   # 实验 4：core_mask 合法值全量扫描
├── ladder2.py                 # 实验 5：每卡 runtime 配额阶梯
├── session_quota.py           # 实验 5/8：LLM session 配额 + 同 context 多 session
├── combo_probe.py             # 实验 8：裸 runtime 组合（无 session）
├── combo_probe2.py            # 实验 8：带 session + 输出张量的真实构造顺序
├── probe_two_instances_card.py# 实验 1：真实 SDK 路径复现双实例失败
└── mt_probe.py                # 实验 7：同卡双实例并行性（hy-mt-1-5 ×2）
```

**跑之前必看 `tools/probes/tts_same_card/README.md`**：
① 选一张**空闲**卡（脚本默认卡 1，`PROBE_CARD=1` 可改）；
② `init_runtime()` **只返回 -1 不抛异常** ⇒ 必须检查返回值；
③ `device_id` 必须传 **bytes**；
④ **不要在生产业务时段跑**（脚本会真的把卡占满）。

---

*附：本报告所有板端数字均来自本机实测（`tools/probes/tts_same_card/`、`/tmp/measure_tts.py`、`/tmp/measure_tts_crosscard.py`、实时 API 探测、`/api/compute` 快照、`lspci`），非厂商宣传值。同卡双实例实验对模型 manifest 做了临时改动（`max_replicas_per_card: 2`），**实验后已还原**；v4 的探针实验**未改动任何生产配置**（实例、`compute.autostart`、manifest 全部原样，实验后三张卡均回到 36 MB）。*
