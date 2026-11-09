# Qwen3-ASR 官方 vLLM 流式实现分析报告

> **分析对象**：`/userdata/Qwen3-ASR/examples/example_qwen3_asr_vllm_streaming.py`（及它依赖的
> `qwen_asr` 包内实现）
> **报告日期**：2026-11-06
> **报告性质**：纯代码分析（只读勘察），**未改动任何仓库代码**
> **分析人**：MiniMax Code @ wdnwebui

---

## 0. 一句话结论

官方 vLLM 的"实时流"**不是** token 级增量推理，而是
**「增量喂入 → 缓冲切块 → 每块全量重算 → 前缀回退」** 的四件套：
每一轮都是一次**完整 forward**（全量音频 + 大部分已确认文本），靠"前缀回退"把已说出口的
文字钉住，靠"全量重算"保证每轮结果都对，代价是**单轮耗时随流长线性增长**。

它的流式能力与 C++ RKNN demo、以及我们板端 `wdnai` 的实现**在策略层面同源**
（全量重喂 + 前缀回退 + 预热块），但有三处关键差异值得注意：
① 回退在 **token 文本层**做（多一次 encode/decode 往返，带 `\ufffd` 半字符保护）；
② 官方**内置了复读修复** `detect_and_fix_repetitions`（C++ demo 和我们板端都**没有**）；
③ 官方流式路径**没有 60 秒时长上限**（`MAX_ASR_INPUT_SECONDS` 只管离线路径）。

---

## 1. 分析边界：这是两套完全不同的技术路线

| | 官方 `qwen-asr` 包 | 我们板端 `wdnai` + RKNN |
|---|---|---|
| 推理硬件 | NVIDIA GPU（vLLM 引擎） | Rockchip NPU（PCIe） |
| 后端 | vLLM（流式**仅**支持 vLLM）/ transformers | RKNN Runtime |
| 模型权重 | `Qwen/Qwen3-ASR-1.7B`（HuggingFace，FP16/BF16） | `llm_*.rknn` + `encoder*.rknn`（RKNN 量化） |
| 音频编码器 | vLLM 原生多模态插件（`Qwen3ASRAudioEncoder`） | 独立 `encoder_online.1828.rknn` |
| 协议形态 | 进程内 Python 调用 + Flask HTTP demo | `ComputeRegistry` → `wdndds` RPC → worker 进程 |

因此本报告的价值**不在于"能不能照搬"**，而在于：
官方实现是同一套模型/算法的**参考实现**，它把「流式应该怎么做」讲清楚了，
包括一些 C++ demo 和我们板端**都缺**的细节。

---

## 2. 官方流式的三层 API

官方把流式拆成三个显式方法（`qwen_asr/inference/qwen3_asr.py`）：

```python
# 1) 建状态（:584-655）—— 只允许 vLLM 后端
state = asr.init_streaming_state(
    context="",           # 可选上下文（system prompt）
    language=None,        # 可选强制语言
    unfixed_chunk_num=2,  # 前 N 块不拿上一轮输出当前缀
    unfixed_token_num=5,  # 之后每轮回退末尾 K 个 token
    chunk_size_sec=2.0,   # 推理块长（秒）
)

# 2) 喂音（:657-765）—— 接受任意长度的 16 kHz 单声道 float PCM
asr.streaming_transcribe(seg, state)
print(state.language, state.text)   # 每轮都是「全量」结果

# 3) 收尾（:767-830）—— 把尾部不足一块的音频也送进去
asr.finish_streaming_transcribe(state)
```

状态对象 `ASRStreamingState`（`:78-128`）是一个可变 dataclass，字段就是这套算法的全部记忆：

| 字段 | 作用 |
|---|---|
| `buffer` | 还没凑满一块的音频尾巴 |
| `audio_accum` | **从流开始到当前的全部音频**（每轮全量重喂） |
| `prompt_raw` | chat template 生成的固定前缀（不含上一轮文本） |
| `_raw_decoded` | 上一轮的**原始**模型输出（含 `language X<asr_text>` 头），用于回退 |
| `chunk_id` | 已推理的块计数（0 起） |
| `language` / `text` | 对外暴露的最新解析结果 |

---

## 3. 核心机制逐层拆解

### 3.1 最关键的一点：**喂入节奏 ≠ 推理块长**

这是官方实现里最容易被误读的设计。

`example_qwen3_asr_vllm_streaming.py:100`：

```python
for step_ms in [500, 1000, 2000, 4000]:
    run_streaming_case(asr, wav16k, step_ms)
```

而 `run_streaming_case()`（`:64-85`）里：

```python
step = int(round(step_ms / 1000.0 * sr))          # 喂入步长（调用方决定）
state = asr.init_streaming_state(..., chunk_size_sec=2.0)   # 推理块长（SDK 决定）
while pos < wav16k.shape[0]:
    seg = wav16k[pos : pos + step]
    asr.streaming_transcribe(seg, state)           # 内部可能触发 0 次或多次推理
    print(f"[call {call_id:03d}] language={state.language!r} text={state.text!r}")
```

`streaming_transcribe()` 内部（`:715-728`）：

```python
state.buffer = np.concatenate([state.buffer, x], axis=0)     # 先进缓冲区
while state.buffer.shape[0] >= state.chunk_size_samples:      # 凑满一块才推理
    chunk = state.buffer[: state.chunk_size_samples]
    state.buffer = state.buffer[state.chunk_size_samples:]
    state.audio_accum = np.concatenate([state.audio_accum, chunk], axis=0)
    ...  # ← 一次完整推理
```

**所以：一次 `streaming_transcribe()` 调用可能触发 0 次、1 次或多次推理。**
`print` 出来的 `[call NNN]` 是**喂入调用**序号，不是推理轮次。

以示例音频 `asr_en.wav`（15.05 s，48 kHz 单声道 24-bit）为例，`chunk_size_sec=2.0` 时：

| `step_ms`（喂入） | 喂入调用次数 | 实际推理块数 | 每调用平均推理次数 |
|---|---|---|---|
| 500 | 91 | 23 | 0.25 |
| 1000 | 46 | 23 | 0.50 |
| 2000 | 23 | 23 | 1.00 |
| 4000 | 12 | 23 | 1.92 |

**四种步长下，推理次数完全相同（23 块），变的只是"多久才看到一次新结果"。**
这正是流式设计的核心取舍：喂得再快，模型的更新频率上限也被 `chunk_size_sec` 卡死。

### 3.2 全量重喂（`audio_accum`）

每一轮推理，送进模型的音频是**从流开始到现在的全部音频**：

```python
inp = {"prompt": prompt, "multi_modal_data": {"audio": [state.audio_accum]}}
outputs = self.model.generate([inp], sampling_params=self.sampling_params, use_tqdm=False)
```

没有 padding、没有分段拼接的滑窗 —— 就是整段重算。

后果：
- **正确性**：每轮看到的都是完整音频，不会出现"只听到后半段"的问题；
- **代价**：音频编码器每轮都要处理全量音频（vLLM 侧是否命中多模态哈希缓存取决于版本，
  官方代码本身没有做任何 encoder 级缓存），**单轮耗时随流长增长**。

C++ demo 用 `audio_embed_history` 做的是同一件事（把历史音频 embedding 拼起来重喂），
我们板端 SDK 同理（`asr.py:664`：`np.concatenate(audio_embed_history, axis=0)`）。
**三边一致，这是算法的固有代价，不是实现缺陷。**

### 3.3 前缀回退 —— 官方独有的"文本层"实现

回退的动机：模型每轮都会**重写**已确认的文字（因为它看到的是完整音频），
所以上一轮的输出不能直接用，得**砍掉尾巴几个 token** 再作为 prompt 前缀喂回去。

官方实现在 **token 文本层**做（`:731-746`）：

```python
prefix = ""
if state.chunk_id < state.unfixed_chunk_num:
    prefix = ""                              # 预热阶段不挂前缀
else:
    cur_ids = self.processor.tokenizer.encode(state._raw_decoded)
    k = int(state.unfixed_token_num)
    while True:
        end_idx = max(0, len(cur_ids) - k)
        prefix = self.processor.tokenizer.decode(cur_ids[:end_idx]) if end_idx > 0 else ""
        if '\ufffd' not in prefix:
            break                            # 正常：前缀是合法字符
        else:
            if end_idx == 0:
                prefix = ""; break
            k += 1                           # 半截 UTF-8 字符 → 多砍一个 token
```

三个细节值得记：

1. **回退源是 `_raw_decoded`（原始输出），不是解析后的 `state.text`** ——
   原始输出里带着 `language English<asr_text>` 这种格式头，回退时会被一起带上，
   所以下一轮 prompt 天然还是合法的 chat 格式。
2. **`\ufffd` 保护**：BPE 的 `decode()` 可能切在多字节字符中间，产生替换符 `�`。
   官方检测到就多砍一个 token，直到前缀能干净解码 —— **C++ demo 和我们板端都没有这一步**，
   因为它们在 **token id 层**回退，根本不存在编解码往返。
3. **回退是"滚动"的**：`k` 在单轮内只增不减（不会重新变小），所以一轮里最多退到空。

**收尾阶段有一处细微不一致**（`:815`）：

```python
end_idx = max(1, len(cur_ids) - int(state.unfixed_token_num))   # 至少保留 1 个 token
```

主循环是 `max(0, ...)`（可以退到空），收尾是 `max(1, ...)`（至少留 1 个）。
且收尾**没有** `\ufffd` 保护循环。属于边角不一致，不影响主干。

### 3.4 输出解析与**复读修复**

每轮推理完（`:757-761`）：

```python
state._raw_decoded = (prefix + gen_text) if prefix is not None else gen_text
lang, txt = parse_asr_output(state._raw_decoded, user_language=state.force_language)
state.language = lang
state.text = txt
```

`parse_asr_output()`（`qwen_asr/inference/utils.py:403-470`）第一步就是
**`detect_and_fix_repetitions(s)`**（`:432`）。

`detect_and_fix_repetitions()`（`utils.py:335-400`）做两级折叠：

| 级别 | 规则 |
|---|---|
| `fix_char_repeats` | 同一字符连续出现 > 20 次 → 折叠成 1 个 |
| `fix_pattern_repeats` | 长度 1~20 的模式重复 ≥ 20 次 → 折叠成 1 次 |

> **这直接命中我们上一轮在板端实测到的"复读恶性循环"**：
> 重复语音 8 遍的 120 s 音频，模型输出膨胀到 13943 字符（正常同样内容只有 755 字符），
> 单轮耗时从 0.1 s 涨到 2.05 s，RTF 0.768。
> **官方 Python 有这层防护，C++ demo 没有 —— 我们板端目前也没有。**
> 这是本报告最有直接借鉴价值的一处（详见 §6.1）。

解析规则本身：

- `language Chinese<asr_text>...` → 有 tag → 拆出语言 + 正文
- `language Chinese\n...` → 逐行找 `language ` 前缀
- 无 tag → 整段当纯文本（语言未知）
- `language None<asr_text>` + 空正文 → 空音频 → `("", "")`
- 强制语言时 → 整段当纯文本，语言直接用用户指定值

### 3.5 采样参数

`Qwen3ASRModel.LLM()`（`:226-288`）里：

```python
sampling_params = SamplingParams(**({"temperature": 0.0, "max_tokens": max_new_tokens}))
```

- **贪心**（temperature = 0）
- example 与 demo_streaming 都用 **`max_new_tokens=32`**，
  example 的注释写得很直白：`# set a small value for streaming`

为什么流式要小？因为每轮模型的**增量输出**本就很短（大部分内容已经在 prefix 里），
`max_new_tokens` 只是单轮上限，用来防止复读把一轮撑爆。
对比 C++ demo 的 `ONLINE_MAX_NEW_TOKENS = 50`。

> **旁证**：C demo 的 `online_main.cc:37` 注释写着
> 「结合回退修正，单轮最长输出对应约 6 秒音频，**通常不会超过 35 个 token**」。
> 官方流式取 `32`、C demo 取 `50`，两者数量级一致，且**官方更贴近实测值**。
> 我们板端目前用 `ONLINE_MAX_NEW_TOKENS_DEFAULT = 50`（照搬 C demo），偏宽松。

### 3.6 时长上限：**流式路径没有**

`utils.py:33-36`：

```python
SAMPLE_RATE = 16000
MAX_ASR_INPUT_SECONDS = 1200        # ← 20 分钟
MAX_FORCE_ALIGN_INPUT_SECONDS = 180
MIN_ASR_INPUT_SECONDS = 0.5
```

但 `MAX_ASR_INPUT_SECONDS` **只在离线 `transcribe()` 路径用于分块**
（`qwen3_asr.py:366`：`max_chunk_sec = MAX_FORCE_ALIGN_INPUT_SECONDS if return_time_stamps else MAX_ASR_INPUT_SECONDS`）。

**流式的 `streaming_transcribe()` / `finish_streaming_transcribe()` 里完全没有时长检查。**
唯一的自然约束是 vLLM 的 `max_model_len` —— 音频 token + 文本 token 撑爆时由 vLLM 报错。

> 顺带印证：官方**离线**上限 1200 s = 20 min，与 C++ demo 的
> `OFFLINE_MAX_AUDIO_SECONDS (20 * 60)` **完全一致**。
> 官方与 C demo 在离线侧是对齐的。

---

## 4. 端到端时序（以 `step_ms=1000, chunk_size_sec=2.0` 为例）

```
调用方                SDK 内部                                         模型
  │                     │                                                │
  ├─ init_streaming_state(unfixed=2, token=5, chunk=2.0s) ──────────────▶│ 建 state
  │                     │  chunk_size_samples = 32000                    │
  │                     │  prompt_raw = chat_template(context)          │
  │                     │                                                │
  ├─ call 1 (1.0s) ───▶ │ buffer=16000 < 32000 → 不推理                 │
  │  print call 001     │  state.text 仍是上一次的（空）                   │
  ├─ call 2 (2.0s) ───▶ │ buffer=32000 ≥ 32000 → 推理 #1                  │
  │                     │   audio_accum = 0~2s                           │
  │                     │   chunk_id=0 < unfixed(2) → prefix=""            │
  │                     │   prompt = prompt_raw                          │
  │                     │   ────────────────────────────▶ generate       │
  │                     │   ◀──────── "language English<asr_text>He"      │
  │                     │   _raw_decoded = "language English<asr_text>He" │
  │                     │   parse → (English, "He")                      │
  │  print call 002     │                                                │
  ├─ call 3 (3.0s) ───▶ │ buffer=16000 → 不推理                          │
  │  print call 003     │  text 不变                                     │
  ├─ call 4 (4.0s) ───▶ │ buffer=32000 → 推理 #2                          │
  │                     │   audio_accum = 0~4s                           │
  │                     │   chunk_id=1 < unfixed(2) → prefix=""  ← 仍在预热│
  │                     │   ────────────────────────────▶ generate       │
  │  print call 004     │                                                │
  ├─ call 5 (5.0s) ───▶ │ buffer=16000 → 不推理                          │
  ├─ call 6 (6.0s) ───▶ │ buffer=32000 → 推理 #3                          │
  │                     │   chunk_id=2 ≥ unfixed(2) → 开始用前缀           │
  │                     │   cur_ids = encode(_raw_decoded)                │
  │                     │   prefix = decode(cur_ids[:-5])  ← 砍尾 5 token │
  │                     │   prompt = prompt_raw + prefix                 │
  │                     │   ────────────────────────────▶ generate       │
  │                     │   _raw_decoded = prefix + gen_text              │
  │  print call 006     │                                                │
  ...                    │                                                │
  ├─ finish_streaming_transcribe(state)                                   │
  │                     │  buffer 剩余 (<32000) 也送进去（**不 padding**） │
  │                     │  end_idx = max(1, len-k)  ← 与主循环略有不同     │
  │  print final       │                                                │
```

**要点**：
- `unfixed_chunk_num` 期间（这里 2 块 = 4 秒）**完全不用**上一轮输出，
  因为最初几轮模型的语言判定和边界都很不稳定；
- 之后每轮都把「上一轮输出砍掉尾部 5 token」作为 prompt 前缀，
  已确认的文字因此被**钉住**，只有最后几个 token 允许改写（这就是我们板端
  "已确认文本 + 未确认尾巴" 里的**未确认尾巴**）；
- 每轮 `state.text` 都是**全量文本**，不是增量 —— 前端要整体刷新。

---

## 5. 官方 Flask 流式 demo（真实麦克风链路）

`qwen_asr/cli/demo_streaming.py` 是官方配套的最小实时 demo，
它把上面的 API 包装成 **HTTP 拉取式**协议（不是 WebSocket）。

### 5.1 协议

| 端点 | 请求 | 响应 |
|---|---|---|
| `POST /api/start` | — | `{"session_id": "..."}`（`:417-427`） |
| `POST /api/chunk?session_id=` | `Content-Type: application/octet-stream`，body = **float32 裸字节**（长度必须 4 的倍数） | `{"language": "...", "text": "..."}`（**全量**）（`:430-453`） |
| `POST /api/finish?session_id=` | — | `{"language": "...", "text": "..."}`，随后删除 session（`:456-469`） |

服务端 `api_chunk` 的核心就三行：

```python
raw = request.get_data(cache=False)
if len(raw) % 4 != 0:  return jsonify({"error": "float32 bytes length not multiple of 4"}), 400
wav = np.frombuffer(raw, dtype=np.float32).reshape(-1)
asr.streaming_transcribe(wav, s.state)
```

会话管理：`SESSION_TTL_SEC = 10 * 60`（`:53`），`_gc_sessions()`（`:56-64`）
在超时后调 `finish_streaming_transcribe` 再丢弃 session。
默认参数（`:479-481`）：

```
--unfixed-chunk-num 4   --unfixed-token-num 5   --chunk-size-sec 1.0
```

> 这三个默认值与 C++ demo 的
> `ONLINE_ROLLBACK_ROUNDS=4` / `ONLINE_ROLLBACK_TOKENS=5` / `ONLINE_CHUNK_MS=1000`
> **逐个相同**。官方 Python 与 C++ demo 在这些策略常量上是刻意对齐的。
> （只有 `examples/example_qwen3_asr_vllm_streaming.py` 用了不同的
> `unfixed_chunk_num=2` / `chunk_size_sec=2.0`，那是为了演示"参数可调"。）

### 5.2 浏览器端（`:217-405`）

```js
const CHUNK_MS = 500;          // :222
const TARGET_SR = 16000;       // :223
processor = audioCtx.createScriptProcessor(4096, 1, 1);      // :329
const chunkSamples = Math.round(TARGET_SR * (CHUNK_MS/1000)); // :330 → 8000
processor.onaudioprocess = (e) => {
  const resampled = resampleLinear(input, audioCtx.sampleRate, TARGET_SR);  // :335
  buf = concatFloat32(buf, resampled);
  if (!pushing) pump();
};
```

`pump()`（`:356-378`）**串行**发送：`await apiPushChunk(chunk)` 没回来不发下一个。

所以真实链路上：**浏览器 500 ms 推一次 → 服务端 `chunk_size_sec=1.0` → 每 2 次推送才真正推理一次**。
这正是 §3.1「喂入节奏与推理块长解耦」在产品里的落地形态。

浏览器端用的是**线性插值重采样**（`resampleLinear`，`:253`），
和我们板端 SDK 的选择一致（`wdnai/backends/asr.py` 的 `_resample`，线性插值，
理由是保持与 C demo 的 miniaudio 结果**逐字节一致**）。

---

### 5.3 实时语音输入：完整细节清单

> 本节回答「官方**实时语音输入**（麦克风）是怎么处理的」。
> 先定位清楚：**整个 `qwen_asr` 包里只有 `cli/demo_streaming.py` 这一条真·实时链路**。
> `cli/demo.py`（Gradio）的音频组件是 `gr.Audio(label="Audio Input (上传音频)", type="numpy")`
> —— **纯文件上传，没有 `sources=["microphone"]`**；
> `cli/serve.py` 只是 vLLM OpenAI 兼容 server 的壳，走 vLLM 自己的协议。

#### 5.3.1 采集端（浏览器）—— 7 个关键决策

| # | 决策 | 代码 | 说明 |
|---|---|---|---|
| 1 | 麦克风约束 | `:316-324` | `channelCount:1` + **`echoCancellation` / `noiseSuppression` / `autoGainControl` 全开** —— 三项浏览器端预处理，是识别质量的第一道关口 |
| 2 | 音频图 | `:326-329` | `AudioContext` → `createMediaStreamSource` → `createScriptProcessor(4096, 1, 1)` |
| 3 | **回调粒度** | `:329` | `4096` 帧/回调。@48 kHz ≈ **85.3 ms** 一次（@44.1 kHz ≈ 92.9 ms） |
| 4 | **必须连 destination** | `:341` | `processor.connect(audioCtx.destination)` —— Web Audio 的老坑：不连到输出端，ScriptProcessor **根本不会被调度**。副作用是麦克风声音被回放（没做 gain=0） |
| 5 | **重采样** | `:253-266`, `:335` | 每帧做一次**线性插值** 44.1k/48k → 16k |
| 6 | **攒包节奏** | `:222`, `:330`, `:360` | `CHUNK_MS = 500` → `chunkSamples = 16000×0.5 = 8000` 样本 = **32 000 字节**/包 |
| 7 | **串行推送** | `:356-378` | `await apiPushChunk(chunk)` —— **后一包必须等前一包的 HTTP 响应回来**。`pushing` 标志防重入 |

补充两点：

- **`createScriptProcessor` 已被 Web Audio 规范标记为 deprecated**（Chrome/Firefox 仍可用，
  现代替代方案是 `AudioWorklet`）。官方 demo 选它是因为代码量最小。
- **重采样是「每帧独立」的**：每次 `onaudioprocess` 只在**本帧内**做线性插值，
  **帧与帧之间不做相位对齐**。这会引入轻微的边界不连续 —— 与我们板端 SDK 的
  `_resample`（整段重采样）是同类问题，但官方是**逐帧**做的。

#### 5.3.2 传输协议 —— HTTP 拉取式，不是 WebSocket

```
POST /api/start                      → { "session_id": "<hex32>" }
POST /api/chunk?session_id=<id>      ← body = float32 裸字节（8000 样本 = 32000 B）
                                    → { "language": "...", "text": "..." }   ← 全量文本
POST /api/finish?session_id=<id>     → { "language": "...", "text": "..." }   ← 并删除 session
```

**body 是裸 float32，不是 WAV** —— 没有文件头，服务端直接
`np.frombuffer(raw, dtype=np.float32).reshape(-1)` 零拷贝解析（`:444`）。
唯一校验是**长度必须是 4 的倍数**（float32 对齐），否则 400（`:441-442`）。
Content-Type 必须是 `application/octet-stream`（`:437-438`）。

**响应里的 `text` 是全量文本，不是增量**。前端每次 `textEl.textContent = j.text` **整体覆盖**（`:369`）。

#### 5.3.3 服务端 —— 会话与生命周期

| 机制 | 代码 | 细节 |
|---|---|---|
| 会话表 | `:52` | `SESSIONS: Dict[str, Session]` —— **进程内全局 dict**，非持久、非跨进程 |
| session id | `:419` | `uuid.uuid4().hex` |
| **TTL** | `:53` | `SESSION_TTL_SEC = 10 * 60`（10 分钟） |
| GC 触发 | `:56-72` | `_get_session()` **每次调用都先 `_gc_sessions()`**（惰性 GC，不是定时器） |
| GC 动作 | `:60-64` | 超时 session 先调 `finish_streaming_transcribe()`（冲掉尾巴音频）再丢弃 |
| 模型实例 | `:47`, `:497` | `global asr` —— **全局单例**，所有流共享一个 vLLM `LLM` |
| Flask 并发 | `:503` | `threaded=True` |

#### 5.3.4 时间尺度对齐（本链路最关键的一张表）

| 层级 | 参数 | 值 | 位置 |
|---|---|---|---|
| 浏览器采集回调 | ScriptProcessor bufferSize | 4096 帧 ≈ 85 ms | `:329` |
| 浏览器攒包 | `CHUNK_MS` | **500 ms** | `:222` |
| HTTP 请求 | 每次一包 | 32 000 B | `:330` |
| **服务端推理块长** | `--chunk-size-sec` | **1.0 s** | `:481` |
| 模型单轮输出上限 | `max_new_tokens` | 32 | `:500` |
| 回退预热轮数 | `--unfixed-chunk-num` | 4 | `:479` |
| 回退 token 数 | `--unfixed-token-num` | 5 | `:480` |
| 会话超时 | `SESSION_TTL_SEC` | 600 s | `:53` |

**推论**：

- 浏览器 **500 ms 推一次**，服务端 **1 秒推理一次** → **每 2 个 HTTP 请求才有 1 次真正的模型推理**。
  中间那个请求照样返回（返回的是上一次的结果），前端照样刷新 —— 看起来"实时"，其实一半是重复值。
- 前 **4 轮**（= 4 秒音频）**不挂前缀**（`unfixed_chunk_num=4`），纯靠模型从零开始；
  **第 5 秒起**才出现"被钉住"的稳定文本。
- `chunk_size_sec=1.0` 与 C++ demo 的 `ONLINE_CHUNK_MS=1000` **完全一致** —— 这是官方
  与 C demo 刻意对齐的默认值（`unfixed-chunk-num 4` / `unfixed-token-num 5` 同样一致）。

#### 5.3.5 端到端时延预算（非模型部分）

从"人说完"到"屏幕更新"：

```
采集缓冲        0 ~ 85 ms      (等下一次 onaudioprocess)
攒够 500ms      0 ~ 500 ms     (平均 250)
HTTP 传输       ~ 1 ~ 10 ms    (本地)
服务端凑满 1s   0 ~ 1000 ms    (平均 500)
模型推理        随流长增长
────────────────────────────────
非模型延迟      ≈ 0.6 ~ 1.6 s  (平均 ~0.85 s)  + 推理时间
```

**注意这个平均值比很多产品高**：因为 500 ms 推送 + 1 s 推理两个窗口**串联**，
而不是并行滚动。要压低只能减小 `chunk_size_sec`（代价是每轮重算的音频更少、结果更抖）
或减小 `CHUNK_MS`（代价是 HTTP 请求数翻倍）。

#### 5.3.6 这条链路的 7 个已知短板（官方 demo 级的粗糙之处）

我在 `qwen_asr/` 全目录做过精确定位搜索，确认**以下机制一个都没有**：

| 缺失项 | 后果 |
|---|---|
| **无 VAD / 端点检测** | 静音照样累积进 `audio_accum` 并参与每轮重算 —— 安静时的 CPU/GPU 开销和"说话时"一样大 |
| **无 chunk 序号** | `/api/chunk` 只带 `session_id`。丢一个包 = 音频缺一段 = **模型不知道**（没有序号可校验），识别直接错一段且无法察觉 |
| **无背压 / 无丢弃策略** | 若模型推理慢于客户端推送，`state.buffer` **持续增长**，推理轮次永远追不上 → **延迟单调增长，无上限保护** |
| **非 WebSocket** | HTTP 拉取式，每包一次往返 + 每包一个 HTTP 头开销；对比 WS 帧开销明显更大 |
| **`createScriptProcessor` 已废弃** | 主线程回调，在低端设备上容易造成 UI 卡顿（现代应换 `AudioWorklet`） |
| **全局单例模型 + `threaded=True`** | 多用户并发时，Flask 线程会**串行排队**在同步的 `model.generate()` 上 —— 官方 demo 实质上是**单流服务** |
| **逐帧独立重采样** | 帧边界相位不连续，长时间累积有轻微漂移（我们板端是整段重采样，没这问题） |

> **结论**：官方 demo 是**功能演示级**的最小实现，展示了"麦克风 → 16 kHz → 增量推送 →
> 块级推理 → 全量刷新"这条完整链路，但**没有**任何生产化的实时音视频工程要素。
> 如果我们要做真·麦克风 ASR，**背压、序号、VAD 三件事必须自己补**。

---


## 6. 三方对照：官方 vLLM ↔ C++ RKNN demo ↔ 我们板端 SDK

### 6.1 策略参数对照

| 维度 | 官方 `qwen_asr`（vLLM） | C++ RKNN demo | 我们板端 `wdnai` |
|---|---|---|---|
| 推理块长 | `chunk_size_sec`（example 2.0 / demo 1.0） | `ONLINE_CHUNK_MS = 1000` | `online_chunk_seconds = 1.0` |
| **喂入节奏** | **调用方任意**（example 500/1000/2000/4000 ms；demo 500 ms） | 固定 1 s 一块 | 固定 1 s 一块（喂入即推理） |
| 全量重喂 | `audio_accum` 全量，无 padding | `audio_embed_history` 全量 | `audio_embed_history` 全量 |
| 前缀回退**层级** | **token 文本层**（encode→砍尾→decode） | token id 层 | token id 层 |
| 半字符保护 | ✅ `\ufffd` 循环多砍 token | ➖ 不需要 | ➖ 不需要 |
| 回退量 | `unfixed_token_num = 5` | `ONLINE_ROLLBACK_TOKENS = 5` | `online_rollback_tokens = 5` |
| 预热块数 | `unfixed_chunk_num`（example 2 / demo **4**） | `ONLINE_ROLLBACK_ROUNDS = 4` | `online_rollback_rounds = 4` |
| 单轮输出上限 | `max_new_tokens = 32` | `ONLINE_MAX_NEW_TOKENS = 50` | `online_max_new_tokens = 50` |
| 采样 | 贪心（temperature=0.0） | — | top_k=1, temperature=1.0, repeat_penalty=1.2 |
| **复读修复** | ✅ `detect_and_fix_repetitions` | ❌ | ❌ |
| **时长上限** | ❌ 流式无限制 | 60 s（截断） | 60 s（截断，webui 层 `decode.py`） |
| 每轮输出语义 | 全量 `(language, text)` | committed + unfixed 两段 | committed + unfixed 两段 |
| 时间戳 | ❌ | ❌ | ❌ |
| 批处理 | ❌（流式单流） | — | — |

### 6.2 三处关键差异的解读

**① 前缀回退层级：官方更"通用"，C++/我们更"精确"**

官方必须做 encode→decode 往返，是因为 vLLM 的 prompt 是**字符串**接口。
C++ demo 与我们板端走 RKNN Runtime，prompt 是**token id 数组**，可以直接砍尾，
**不存在编解码往返，也就不存在 `\ufffd` 问题**。我们板端在这一项上**优于**官方实现。

**② 复读修复：官方有，我们缺 —— 最值得借鉴的一处**

`detect_and_fix_repetitions` 直接对应我们上轮实测到的复读膨胀问题。
我们板端目前的处理方式是"靠回退策略吸收"，但**没有文本层兜底**。

**②′ 命名对照：C demo 的两条宏注释写反了**

读 C++ 源码时容易被带偏，这里明确一下**语义**（以使用点为准，不以注释为准）：

```c
// online_main.cc:40-41  ← 注释写反了
#define ONLINE_ROLLBACK_TOKENS 5        // 回退策略，首5轮输入回退     ← 实际是「砍尾 5 个 token」
#define ONLINE_ROLLBACK_ROUNDS 4        // 回退策略，最后4个token回退   ← 实际是「预热 4 轮」
```

实际使用点证明名字是对的、注释是错的：

- `online_main.cc:302` `remaining_rollback_rounds = ONLINE_ROLLBACK_ROUNDS;`
  → 当**轮次计数器**用（预热轮数）→ 对应官方 `unfixed_chunk_num`
- `online_main.cc:488-492` 预热期内 `stable_token_count = 0`（整轮都不挂前缀）
- `online_main.cc:493-495` 预热后 `stable_token_count = size - ONLINE_ROLLBACK_TOKENS`
  → 砍**末尾 5 个 token** → 对应官方 `unfixed_token_num`

所以三方的命名对应关系是：

| 语义 | 官方 Python | C++ demo | 我们板端 |
|---|---|---|---|
| 预热轮数（前 N 轮不挂前缀） | `unfixed_chunk_num` | `ONLINE_ROLLBACK_ROUNDS` | `online_rollback_rounds` |
| 回退 token 数（砍尾） | `unfixed_token_num` | `ONLINE_ROLLBACK_TOKENS` | `online_rollback_tokens` |

**官方的命名（`unfixed_chunk_num` / `unfixed_token_num`）明显比 C demo 的更不容易误解。**

**③ 时长上限：官方流式无限制，再次印证 60 s 是 C demo 的保守经验值**

官方流式路径不做任何时长检查；C++ demo 的 `ONLINE_MAX_AUDIO_SECONDS 60`
是**截断**；我们 webui 层 `app/core/audio/decode.py` 的 `MAX_AUDIO_SECONDS = 60.0`
照搬了这个值。这与上一轮报告的结论一致：
**60 s 不是硬件/显存/上下文限制，只是 C demo 的经验值**。

---

## 7. 对我们的实际启示

### 7.1 值得借鉴（可选做，未实施）

| 项 | 说明 | 优先级 |
|---|---|---|
| **复读修复** | 把 `detect_and_fix_repetitions` 的两级折叠（>20 同字符 / ≥20 次模式重复）移植到 `wdnai/backends/asr.py` 的文本解析层 | ★★★ 直接命中已实测的复读膨胀 |
| **真正的麦克风流式** | 官方是"喂入即缓冲、按 `chunk_size_sec` 推理"的两级节奏；我们的 webui 是"整段文件上传 → 按 1 s 切块"，两者不是一回事。若接真麦克风，需要 SDK 层提供 `streaming_transcribe` 式的增量入口 | ★★ 看需求 |
| **流式无 60 s 上限** | 官方不设限；我们当前 60 s 是照搬 C demo。若要放宽，官方这条是佐证 | ★★ 上一轮已留给用户决策 |
| **采样参数对齐** | 官方流式 `max_new_tokens=32`，我们用 50；流式下 32 更紧 | ★ 影响极小 |

### 7.1.1 若要做真·麦克风 ASR，官方 demo 缺的必须自己补

官方 `demo_streaming.py` 是**功能演示级**实现（§5.3.6），我们不能照抄它的短板：

| 必须补 | 原因（官方的缺失） |
|---|---|
| **背压 / 丢弃策略** | 官方无背压：推理慢于推送时 `state.buffer` 无上限增长，延迟单调恶化。我们板端 RTF 在长音频上会涨（实测 120 s 语音 RTF 0.144），**这条一定会触发** |
| **chunk 序号 + 校验** | 官方 `/api/chunk` 无序号，丢包无法察觉，识别静默出错 |
| **VAD / 端点检测** | 官方无 VAD，静音也参与每轮全量重算，纯烧算力 |
| **WebSocket 或长连接** | 官方 HTTP 拉取式，每包一次往返 + HTTP 头开销 |
| **AudioWorklet** | 官方用已废弃的 `createScriptProcessor`，主线程回调，低端设备易卡 UI |
| **整段重采样而非逐帧** | 官方逐帧独立线性插值，帧边界相位不连续会累积漂移；我们 SDK 已是整段重采样，**别改回逐帧** |

> 我们 webui 侧已具备的条件：`app/core/audio/decode.py` 的解码、`wdnai` 的
> `run_audio(..., online_*=...)` 分轮接口、以及卡片现有的逐轮明细（`/api/qwen3-asr/rounds`）
> —— 逐轮明细这个能力**正好就是实时流式需要的**（每轮一个稳定/未确认两段文本）。
> 缺的主要是**采集端 + 传输层 + 背压/序号/VAD**。

### 7.2 不需要借鉴

- **token 文本层回退 + `\ufffd` 保护**：我们在 token id 层回退，天然规避该问题。
  只有将来改成"文本级 prefix"才需要它。
- **vLLM prefix caching 之类**：那是 GPU 引擎特性，RKNN 路线无对应物。

---

## 8. 局限与注意事项

1. **未运行官方示例**。本报告是**纯静态代码分析** —— 官方流式仅支持 vLLM 后端，
   需要 NVIDIA GPU + `pip install qwen-asr[vllm]`，本板无 GPU 环境，无法实机复现。
   所有"推理次数""RTF 随流长增长"等结论均来自代码逻辑推导，
   其中推理次数已用**等效的 15.05 s 音频**做了算术验证（§3.1 表格）。
2. **官方 `asr_en.wav` 未逐字节比对**。报告中的时长（15.05 s / 48 kHz / 单声道 / 24-bit）
   来自我们板端 SDK example 目录下的**同名**文件，与官方 example 下载的 URL 同名；
   若两者实际不是同一文件，§3.1 的具体次数会有出入，但**比例关系与结论不变**。
3. **vLLM 内部行为未逐行验证**。多模态哈希缓存、prefix caching 的实际命中情况
   取决于 vLLM 版本与配置，官方代码本身未做 encoder 级缓存，
   因此"每轮重算全量音频"是**必然**的，最坏情况确实线性。
4. **§5.3 的实时链路细节来自静态阅读**。浏览器 `ScriptProcessor` 的实际回调间隔、
   `getUserMedia` 的实际采样率、端到端时延预算均为**按代码推导**，
   未在真实浏览器 + 麦克风下实测。
5. **未改动任何代码**。本轮为只读分析；`wdnai` 与 `wdnwebui` 的工作区状态与上一轮交付时一致。

---

## 9. 关键文件与行号索引

### 官方仓库 `/userdata/Qwen3-ASR/`

| 文件:行 | 内容 |
|---|---|
| `examples/example_qwen3_asr_vllm_streaming.py:64-85` | `run_streaming_case()` —— 喂入/推理双节奏 |
| `examples/example_qwen3_asr_vllm_streaming.py:50-61` | `_resample_to_16k()` —— 线性插值 |
| `examples/example_qwen3_asr_vllm_streaming.py:90-94` | `LLM(..., gpu_memory_utilization=0.8, max_new_tokens=32)` |
| `qwen_asr/inference/qwen3_asr.py:78-128` | `ASRStreamingState` |
| `qwen_asr/inference/qwen3_asr.py:145-161` | `__init__`（`sampling_params` / `max_new_tokens`） |
| `qwen_asr/inference/qwen3_asr.py:226-288` | `LLM()` classmethod；`:272` `SamplingParams(temperature=0.0, …)` |
| `qwen_asr/inference/qwen3_asr.py:366` | 离线分块（`MAX_ASR_INPUT_SECONDS` 唯一使用点） |
| `qwen_asr/inference/qwen3_asr.py:454-465` | `_build_text_prompt()` |
| `qwen_asr/inference/qwen3_asr.py:584-655` | `init_streaming_state()` |
| `qwen_asr/inference/qwen3_asr.py:657-765` | **`streaming_transcribe()`** |
| `qwen_asr/inference/qwen3_asr.py:731-746` | 前缀回退 + `\ufffd` 保护 |
| `qwen_asr/inference/qwen3_asr.py:767-830` | `finish_streaming_transcribe()`（`:815` `max(1, …)` 差异） |
| `qwen_asr/inference/utils.py:33-36` | `SAMPLE_RATE` / `MAX_ASR_INPUT_SECONDS=1200` |
| `qwen_asr/inference/utils.py:335-400` | **`detect_and_fix_repetitions()`** |
| `qwen_asr/inference/utils.py:403-470` | `parse_asr_output()`（`:432` 调用复读修复） |
| `qwen_asr/cli/demo_streaming.py:53-64` | `SESSION_TTL_SEC=600` / `_gc_sessions()` |
| `qwen_asr/cli/demo_streaming.py:222-223` | 浏览器 `CHUNK_MS=500` / `TARGET_SR=16000` |
| `qwen_asr/cli/demo_streaming.py:253-266` | `resampleLinear()` —— 逐帧线性插值 |
| `qwen_asr/cli/demo_streaming.py:316-324` | `getUserMedia` 约束（单声道 + AEC + 降噪 + AGC） |
| `qwen_asr/cli/demo_streaming.py:329-341` | `createScriptProcessor(4096,1,1)` + `onaudioprocess` + 连 destination |
| `qwen_asr/cli/demo_streaming.py:356-378` | `pump()` 串行推送 |
| `qwen_asr/cli/demo_streaming.py:417-469` | `/api/start` `/api/chunk` `/api/finish` |
| `qwen_asr/cli/demo_streaming.py:437-444` | octet-stream 校验 + `np.frombuffer` 零拷贝解析 |
| `qwen_asr/cli/demo_streaming.py:479-481` | 默认 4 / 5 / 1.0 |
| `qwen_asr/cli/demo_streaming.py:503` | `threaded=True` + 全局单例 `asr` |
| `qwen_asr/cli/demo.py:374` | Gradio 的 `gr.Audio(type="numpy")` —— **纯上传，无麦克风** |
| `qwen_asr/cli/serve.py` | vLLM OpenAI 兼容 server 壳（非官方自研流式协议） |
| `qwen_asr/core/vllm_backend/qwen3_asr.py` | vLLM 原生多模态插件（音频编码器 + 数据解析） |

### 本板参考实现

| 文件:行 | 内容 |
|---|---|
| `/userdata/rknn3/rknn3-model-zoo/examples/Qwen3_ASR/cpp/online_main.cc:35` | `ONLINE_MAX_AUDIO_SECONDS 60` |
| `.../cpp/online_main.cc:36-37` | `ONLINE_CHUNK_MS 1000` / `ONLINE_MAX_NEW_TOKENS 50`（注释：单轮通常 <35 token） |
| `.../cpp/online_main.cc:40-41` | `ONLINE_ROLLBACK_TOKENS 5` / `ONLINE_ROLLBACK_ROUNDS 4`（**注释写反，见 §6.2 ②′**） |
| `.../cpp/online_main.cc:302` | `remaining_rollback_rounds` —— 证明 ROUNDS 是"预热轮数" |
| `.../cpp/online_main.cc:475` | 单轮 `ONLINE_MAX_NEW_TOKENS` 传入 LLM 推理 |
| `.../cpp/online_main.cc:482-502` | 回退策略（`:488-492` 预热 / `:493-495` 砍尾） |
| `/userdata/rknn3/rknn3-model-zoo/examples/Qwen3_ASR/cpp/main.cc:30` | `OFFLINE_MAX_AUDIO_SECONDS (20*60)` |
| `wdnai/src/wdnai/backends/asr.py:78-81` | `ONLINE_*_DEFAULT`（1.0 / 50 / 5 / 4） |
| `wdnai/src/wdnai/backends/asr.py:664` | `audio_embed_history` 全量拼接 |
| `wdnai/src/wdnai/backends/asr.py:683-692` | 回退策略（token id 层） |
| `app/core/audio/decode.py:49` | `MAX_AUDIO_SECONDS = 60.0`（webui 层截断） |

---

## 10. 结论

1. 官方 vLLM 的"实时流"= **增量喂入 + 缓冲切块 + 全量重算 + 前缀回退**，
   **不是** token 级增量推理；每轮都是完整 forward。
2. **喂入节奏与推理块长是解耦的两个参数** —— 这是官方实现最值得学的一点，
   也是我们板端"喂入即推理"和它最大的架构差异。
3. 官方的回退在 **token 文本层**做（多一次编解码往返 + `\ufffd` 保护），
   我们在 **token id 层**做，**这一项我们更精确**。
4. 官方**内置复读修复**（`detect_and_fix_repetitions`），C++ demo 和我们板端**都没有** ——
   这是本报告最有借鉴价值的发现，正好命中我们已实测的复读膨胀问题。
5. 官方**流式路径没有时长上限**（1200 s 只管离线），
   再次印证我们当前 60 s 是照搬 C demo 的保守经验值。
6. 除以上四点外，官方与 C demo 在策略常量上（回退 5 token / 预热 4 块 / 1 s 块长）
   **刻意对齐**，我们的实现也与之逐个一致 —— 三边在算法主干上是同源的。
7. **实时语音输入只有一条链路**：`cli/demo_streaming.py`（Flask + 浏览器麦克风）。
   Gradio demo 是纯上传，`serve.py` 只是 vLLM server 壳。
   链路是 **HTTP 拉取式**（非 WebSocket）：浏览器 4096 帧回调 → 逐帧线性插值重采样到 16 kHz
   → 攒 500 ms 打包 float32 → 串行 `await` 推送 → 服务端凑满 1 s 推理一次 → 返回**全量**文本 → 前端整体覆盖。
   官方默认参数（4 / 5 / 1.0）与 C++ demo **逐个相同**。
8. **但这条链路是演示级**：无 VAD、无 chunk 序号、无背压、无 WebSocket、
   用已废弃的 `createScriptProcessor`、逐帧重采样有相位漂移、
   全局单例模型 + `threaded=True` 实质是单流服务。
   **我们要做真·麦克风 ASR，背压 / 序号 / VAD 三件必须自己补**（§7.1.1）。

---

*报告结束 · 本轮未改动任何代码*
