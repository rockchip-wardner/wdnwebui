# 板端实时麦克风 ASR 落地清单

> **来源**：`docs/QWEN3_ASR_VLLM_STREAMING_ANALYSIS.md`（官方 vLLM 流式分析）+ 本仓现有 ASR 实现勘察
> **日期**：2026-11-08
> **性质**：落地规格书（**本轮只出清单，不实施**）
> **约束基线**：`AGENTS.md` —— worker 薄 RPC、`options` 透传、per-model 差异写进 `wdnai`、不改 framework 注入逻辑

---

## 0. 已定决策（本轮拍板）

| 决策项 | 结论 |
|---|---|
| **采集端形态** | **统一采集契约 + 两个实现**：浏览器 `getUserMedia`、板载 ALSA（`/dev/snd`） |
| **流式档位** | **两阶段**：阶段一 = SDK 流式接口 + 单测；阶段二 = 采集端与传输层接入 |
| **静音/端点** | **服务端端点检测**（主进程侧，不改模型行为、不引入 `webrtcvad`） |
| **文件 / 视频实时**（追加） | **与麦克风共用一个转写流内核**，靠「时间轴 + 游标推进器」区分三种模式（详见 §8） |

---

## 1. 目标与非目标

### 目标

1. **真流式**：麦克风音频**增量**喂进模型，每 1 秒音频产出一轮结果，已确认文本逐段稳定增长。
2. **与现有文件流式共享同一套模型侧语义**（全量重喂 + 前缀回退），文本质量不因输入方式改变。
3. **跨端一致**：浏览器采集与板载采集，同一段音频的识别文本**逐字节相同**。
4. **实时性有界**：推理跟不上采集时**延迟有上限**（靠丢块 + 上报），不静默恶化。
5. **（追加）三种实时模式统一**：麦克风 / 音频文件 / 视频（含音轨）共用同一内核，
   文件模式额外支持 **seek + 缓存 + 字幕时间轴**（§8）。

### 非目标

- 不做强制对齐 / 时间戳（官方 vLLM 流式也不支持）。
- 不做多路并发实时流（当前 worker 串行 pump，一卡一模型）。
- 不改 `wdndds` 传输层，不改 `compute_worker.py` 入口（见 §2）。

---

## 2. 关键可行性结论（决定了改造面）

### 2.1 ✅ 不用改 `wdndds`，不用改 worker

链路实测（源码级）：

| 位置 | 事实 |
|---|---|
| `wdnai/src/wdnai/session.py:1018-1030` | `overrides` **全量透传**给 `backend.run_audio(pcm, ..., **overrides)` |
| `wdndds/src/wdndds/ai_session.py:592` | wire 上 `"options": dict(overrides)` —— **自由 dict，不校验字段** |
| `wdndds/.../ai_session.py:1041-1094` | `serve_local_session` 的 replier 薄 handler，原样透传 `{prompt, attachments, options}` |

**推论**：主进程 `session.respond(attachment=[...], asr_stream_id="s1")` 里的
`asr_stream_id` 会**原样穿到 worker 侧** `ASRBackend.run_audio(..., asr_stream_id="s1")`。

→ **worker 保持"只透传 `{prompt, attachments, options}`"的薄 RPC 语义不变（AGENTS.md 合规）**，
   **零协议改动，零跨仓依赖**。

### 2.2 ⚠️ 必须改 SDK（不可回避）

`ASRBackend._run_online`（`asr.py:615-723`）里的流式状态全是**局部变量**：

```python
audio_embed_history: List[np.ndarray] = []      # :641
committed_token_ids: List[int] = []             # :642
decoder = _OnlineDecoder(self.tokenizer)        # :640
```

一次 `run_audio` = 一次完整会话（内部 while 切块跑 N 轮）。
**每次 `respond()` 都会重置这些状态** → 每块单独提交会得到「每块独立识别、文本重复」。

→ 阶段一必须让这些状态**跨 request 存活**。

### 2.3 ⚠️ 增量模式与文件模式必须逐字节等价

增量 N 块 ≡ 文件 N 块，成立的前提（阶段一的**核心不变量**）：

| 维度 | 文件模式 | 增量模式 | 必须一致 |
|---|---|---|---|
| 块边界 | 从流起点按 `samples_per_chunk` 对齐 | 同（服务端按累计样本数对齐） | ✓ |
| 每块 mel | 块内独立算，块间不连续 | 同（**不要**改成跨块连续 mel） | ✓ |
| encoder | `encoder_online` 逐块 | 同 | ✓ |
| embeds 拼接 | `prefix + 全部 audio history + postfix + committed` | 同 | ✓ |
| 回退预热判据 | `chunk_idx < rollback_rounds` | 用**全局** `chunk_idx` | ✓ |
| `is_last` | 整段最后一块 | 仅 `final=True` 那次调用的最后一块 | ✓ |
| `max_new_tokens` | `ONLINE_MAX_NEW_TOKENS_DEFAULT=50` | 同 | ✓ |

**这是阶段一的第一验收项**，也是最省事的回归手段：复用
`examples/Qwen3_ASR/compare_with_c_demo.py` 的比对方法论。

### 2.4 ⚠️ 文件 / 视频模式的额外缺口：**时间戳**

现有 `asr_round` 载荷（`asr.py:775-789`）= `{type, round, committed, unfixed, language}`，
**没有任何时间信息**。麦克风模式不需要（时间就是现在），但**文件模式必须要** ——
否则字幕无法与播放头对齐。

扩展是**纯加法**：主进程 `service.py:249-254` 用 `payload.get(...)` 带默认值，
新老任意组合都能跑。详见 §8.2。

---

## 3. 目标架构

```
┌── 采集端（两个实现，同一契约） ────────────────────────────────┐
│                                                                  │
│  ① 浏览器（app.js）                                             │
│     getUserMedia → AudioWorklet/ScriptProcessor(4096)            │
│     → 线性插值重采样 16k（**整块重采样**，非逐帧）               │
│     → 攒块 500ms → POST /live/chunk → await（串行 = 背压）      │
│                                                                  │
│  ② 板载（tools/asr_mic_agent.py，独立进程）                      │
│     ctypes → libasound.so.2 → snd_pcm_readi(pcmC1D0c)            │
│     → 48k→16k 线性插值 → 攒块 500ms → POST /live/chunk           │
└───────────────────────────┬──────────────────────────────────────┘
                            │  PCM 块 {pcm, seq, stream_id}
                            ▼
┌── webui 主进程（app/apps/qwen3_asr/） ──────────────────────────┐
│  api.py     POST /live/start  /live/chunk  /live/finish /live/stop │
│  service.py _LiveAsrSession：                                  │
│              · 按 16000 样本对齐攒够 1s → 提交                  │
│              · **单线程串行**调 session.respond()（背压）       │
│              · 端点检测（能量门限）→ final=True → 重置会话      │
│  endpoint.py 能量 RMS 门限 + 连续静音时长 → 端点判定            │
└───────────────────────────┬──────────────────────────────────────┘
                            │ respond(attachment=[1s PCM],
                            │         options={asr_stream_id, asr_stream_final})
                            ▼
┌── RPC（options 自由 dict，零协议改动） ────────────────────────┐
│  wdndds  {"action":"infer","op":"respond","prompt","attachments",│
│           "options":{"asr_stream_id":"s1"}, "stream_id":"..."}  │
│  worker  compute_worker.py —— **零改动**（只透传）             │
└───────────────────────────┬──────────────────────────────────────┘
                            ▼
┌── worker 进程内 ASRBackend（wdnai） ────────────────────────────┐
│  _online_streams: Dict[str, _OnlineStreamState]  ← 阶段一新增   │
│    · audio_embed_history / committed_token_ids / decoder        │
│    · 跨 request 存活，TTL 自动回收                             │
│  每块：mel → encoder → 拼 embeds → session_run → 回退 →        │
│         sink.publish(round payload)                            │
└───────────────────────────┬──────────────────────────────────────┘
                            │ RELIABLE delta 旁路（**已通**）
                            ▼
                    主进程 on_delta → WS "round" 帧 → 浏览器
```

---

## 4. 阶段一：SDK 流式会话接口（`wdnai`）

### 4.1 需改造的文件

| 文件 | 动作 | 说明 |
|---|---|---|
| `.venv/src/wdnai/src/wdnai/backends/asr.py` | **改** | 主体：`_OnlineStreamState`、会话表、增量路径 |
| `.venv/src/wdnai/src/wdnai/model.py` | **小改** | `qwen3_asr()` spec 新增 `asr_stream_ttl_sec` 默认值 |
| `.venv/src/wdnai/src/wdnai/capability.py` | **不改** | `swcap_audio_asr` 已覆盖；实时只是新的输入方式，不新增能力 |
| `.venv/src/wdnai/src/wdnai/session.py` | **不改** | `overrides` 已全量透传（`:1018-1030`），无需动路由 |
| `.venv/src/wdnai/tests/test_asr_stream.py` | **新增** | 阶段一单测（pytest） |
| `.venv/src/wdnai/examples/Qwen3_ASR/streaming_session.py` | **新增** | 增量接口的可跑样例 |

> `wdnai` 是**独立 git 仓**（`.venv/src/wdnai`），按既有约定**不提交**。

### 4.2 接口定义

#### 4.2.1 传输层契约（走 `options`，零协议改动）

主进程侧：

```python
result = await session.respond(
    attachment=[AudioAttachment(pcm_1s, mime_type="audio/pcm")],
    sample_rate=16000, n_channels=1, sample_width=2,
    on_delta=self._on_delta,
    asr_stream_id="live-<uuid>",     # ← 新增：会话 id
    asr_stream_final=False,          # ← 新增：本块是否为最后一块
    # online_* 参数沿用现有 4 个键，不变
)
```

wire 上自动变成：

```json
{"action":"infer","op":"respond","v":1,"prompt":"",
 "attachments":[...],"options":{"asr_stream_id":"live-...","asr_stream_final":false}}
```

worker 侧 `run_audio` 直接收到 `asr_stream_id` / `asr_stream_final` 两个 kwargs。

#### 4.2.1.1 ⚠️ 增量路径必须自带**流式重采样**

文件路径在 `run_audio` 开头对**整段**做一次 `_resample_linear`；增量路径**不能**
逐块各做一次 —— 那会让每块的相位从 0 重新开始，与整段重采样**错位一个块**，
token 会翻转。

正确做法：**带 carry 的流式重采样器**（`_StreamResampler`）
- 输出样本 `j` 用**全局**输入索引 `(j × src) / dst` 插值；
- 保留「未消费的输入尾巴」作为 carry；
- 只在「右邻居是真实样本」时产出，把离线路径最后一次外插推迟到 `flush()`
  （那时才知道总长）。

这样**分块喂**与**整段喂**的输出**逐样本相同** —— 这是「增量 ≡ 文件」的前提，
已由单元测试锁死（3 种源采样率 × 4 种分块大小）。

#### 4.2.1.2 ⚠️ 「最后一块」只有一个

`asr_stream_final=True` 的那次调用里，**最后一个被执行的块**才是最后一块。
若该次调用既有整块又有不足一块的尾巴，**尾巴才是最后一块**（整块仍要砍尾）——
这与文件路径的切分完全一致（最后一块永远是短的那块）。

⚠️ 实现初版把两者都标成了「最后一块」，导致整块多确认 5 个 token、转写多出一句。
**已被实机测试抓出并修复**。

#### 4.2.2 SDK 侧新增/变更的符号

```python
# asr.py —— 模块级常量
ASR_STREAM_ID_KEY    = "asr_stream_id"
ASR_STREAM_FINAL_KEY = "asr_stream_final"

# asr.py —— 新增 dataclass（模块私有）
@dataclass
class _OnlineStreamState:
    stream_id: str
    samples_per_chunk: int
    max_new_tokens: int
    rollback_tokens: int
    rollback_rounds: int
    audio_embed_history: List[np.ndarray]
    committed_token_ids: List[int]
    decoder: "_OnlineDecoder"
    chunk_idx: int
    created_at: float
    last_seen: float

# asr.py —— ASRBackend 新增
def _run_online_stream(self, audio, stream_id, final, on_asr_commit, overrides) -> str
def _get_or_create_stream(self, stream_id, overrides) -> _OnlineStreamState
def _gc_streams(self) -> None                       # TTL 回收
def reset_stream(self, stream_id: str) -> None       # 显式丢弃某会话
```

#### 4.2.3 `run_audio` 的分叉点（`asr.py:540-576`）

```python
def run_audio(self, pcm_bytes, sample_rate=TARGET_SAMPLE_RATE,
              n_channels=1, sample_width=2, on_asr_commit=None, **overrides):
    self.sink.reset()
    audio = _pcm_to_mono_float32(pcm_bytes, n_channels, sample_width)
    if sample_rate != TARGET_SAMPLE_RATE:
        audio = _resample_linear(audio, sample_rate, TARGET_SAMPLE_RATE)
    if len(audio) < N_FFT:
        raise RuntimeError(...)

    stream_id = overrides.pop(ASR_STREAM_ID_KEY, None)
    if stream_id:
        final = bool(overrides.pop(ASR_STREAM_FINAL_KEY, False))
        return self._run_online_stream(audio, str(stream_id), final,
                                       on_asr_commit, overrides)
    if self._use_online:
        return self._run_online(audio, on_asr_commit, overrides)   # ← 原路径不动
    return self._run_offline(audio, overrides)
```

**`asr_stream_id` 存在时优先级高于 `use_online_encoder`** —— 增量路径隐含使用在线编码器。

### 4.3 字段

#### 4.3.1 SDK 侧 options 新键

| 键 | 类型 | 必填 | 默认 | 语义 |
|---|---|---|---|---|
| `asr_stream_id` | `str` | 增量模式必填 | 无 | 流式会话 id；同一 id 共享音频历史与已确认 token |
| `asr_stream_final` | `bool` | 否 | `false` | 本次调用是否为该会话的最后一块（收尾，全文确认） |
| `asr_stream_origin_sec` | `float` | 否 | `0.0` | **会话**在调用方时间轴上的起点（秒）。会话创建时冻结；每块时间戳由它推导 |

#### 4.3.2 每轮 delta 载荷（**沿用现有 schema，不改**）

```json
{"type":"asr_round","round":7,"committed":"...","unfixed":"...","language":"English"}
```

`_round_payload`（`asr.py:711`）已产出这个结构；`asr_final` 载荷同样不变。

#### 4.3.3 `model.py` spec 新字段

| 字段 | 类型 | 默认 | 语义 |
|---|---|---|---|
| `asr_stream_ttl_sec` | `float` | `120.0` | 流式会话空闲回收时间（秒） |

### 4.4 会话状态机与生命周期

```
              run_audio(asr_stream_id=s)          run_audio(..., final=True)
                     │                                      │
                     ▼                                      ▼
   (无) ──► [ACTIVE] ──────────────────────────────► [CLOSED] ──► 删除 state
              │  ▲                                        
              │  │  run_audio(s) 续用                  
              ▼  │                                        
          TTL 到期 (idle > 120s) ──► 删除 state            
                     │
                     └─► reset_stream(s) 显式丢弃
```

**TTL 是必须的**：worker 是长驻进程，`audio_embed_history` 单调增长；
没有回收，泄漏会持续吃内存。每会话带 `created_at` / `last_seen`，
每次 `run_audio` 入口跑一次 `_gc_streams()`。

**`close()` 必须清空 `_online_streams`**（`asr.py:742` 已有 `close()`，补一行）。

### 4.5 不变量（必须保持的性质）

1. **不传 `asr_stream_id` 时，行为与改造前逐字节一致**（文件驱动路径零回归）。
2. 增量 N 块 ≡ 文件 N 块：**committed 文本逐字节相同**（§2.3 的六维对齐）。
3. `final=True` 的那一次，其最后一块 `is_last=True`（不砍尾，全文确认）。
4. `final=True` 之后该 `stream_id` 被删除；再次用同 id 提交 ≡ 新建会话（**不报错**）。
5. 单个 `stream_id` 的块索引 `chunk_idx` **全局单调递增**，跨 request 延续。
6. 回退预热判据用**全局** `chunk_idx`（不是「本次调用内的块序号」）。
7. **「最后一块」只有一个**：带 `asr_stream_final=True` 的那次调用里，
   **最后一个被执行的块**才是最后一块。若该次调用既有整块又有不足一块的尾巴，
   **尾巴才是最后一块**（整块仍要砍尾）—— 这与文件路径的切分一致。
   ⚠️ 实现初版把两者都标成了「最后一块」，导致整块多确认 5 个 token、
   转写多出一句。**已被实机测试抓出并修复**（回归断言见下）。
8. **重采样必须逐样本等价**：非 16 kHz 输入走**带 carry 的流式重采样**
   （全局输出索引），不能逐块独立重采样 —— 后者每块相位从 0 重来，
   与整段重采样错位一个块，token 会翻转。

### 4.6 单元测试（`.venv/src/wdnai/tests/test_asr_stream.py`）

| # | 用例 | 断言 |
|---|---|---|
| T1 | 增量 16 块 vs 文件 16 块 | committed 文本**逐字节相同**（核心） |
| T2 | 不传 `asr_stream_id` | 行为与改造前一致（golden 对比） |
| T3 | `final=True` 后 state 删除 | `_online_streams` 为空 |
| T4 | 同 id 再次提交 | ≡ 新建会话，不抛异常 |
| T5 | 空闲 120 s 后 | state 被 TTL 回收（可用可注入时钟） |
| T6 | 100 块连续提交 | 不崩；`chunk_idx` == 100 |
| T7 | `asr_stream_final` 缺省 | 视为 `False`（不收尾） |
| T8 | `close()` | `_online_streams` 清空 |

> **无 NPU 环境下的可测性**：T1/T2 需要真模型。测试分两档：
> **契约档**（mock backend，验状态机/TTL/参数透传）+ **实机档**（需 NPU，
> 与 `compare_with_c_demo.py` 同款，标记 `@pytest.mark.requires_npu`）。
> 仓库既有 `test_mock_e2e.py` 可作 mock 档样板。

---

## 5. 阶段二：webui 采集与传输层

### 5.1 统一采集契约

**服务端消费的最小单位 = PCM 块**：

| 字段 | 类型 | 约束 |
|---|---|---|
| `pcm` | `bytes` | `s16le` / **单声道** / **16 kHz**（重采样在采集端完成） |
| `seq` | `int` | 从 0 单调递增；**缺号 = 丢包** |
| `stream_id` | `str` | 会话标识 |

采集端只负责「产生符合契约的块」；两个实现互不感知。

### 5.2 需改造的文件

| 文件 | 动作 | 说明 |
|---|---|---|
| `app/core/audio/capture.py` | **新增** | 采集契约 + 块累积器（`ChunkAccumulator`） |
| `app/core/audio/alsa.py` | **新增** | 板载 ALSA 采集（ctypes → `libasound.so.2`，**零 pip 依赖**） |
| `app/core/audio/endpoint.py` | **新增** | 端点检测（能量门限，纯 numpy） |
| `tools/asr_mic_agent.py` | **新增** | 板载采集**独立进程** CLI |
| `app/apps/qwen3_asr/service.py` | **改** | 新增 `_LiveAsrSession` 管理器（现有 `_AsrManager` 不动） |
| `app/apps/qwen3_asr/api.py` | **改** | 新增 4 个 live 端点（现有端点不动） |
| `app/apps/qwen3_asr/static/app.js` | **改** | 浏览器采集 + 上行 + 渲染 |
| `app/apps/qwen3_asr/static/template.html` | **改** | 实时识别区块（麦克风按钮、电平表、状态） |
| `app/apps/qwen3_asr/static/app.css` | **改** | 电平表 / 丢包提示样式 |
| `app/apps/qwen3_asr/manifest.json` | **可能改** | 能力描述补充实时模式 |
| `app/core/config.py` + `config.jsonc` | **改** | **feature flag** `asr.live.enabled`（默认 `false`） |
| `scripts/verify_asr_wizard.py` | **改** | 新增实时用例（现有 32 条不动） |

**阶段二-b 追加**（四源 UI + 去 60 s 截断）：

| 文件 | 动作 | 说明 |
|---|---|---|
| `app/core/audio/decode.py` | **改** | 新增 `iter_media_pcm()`（**流式**解码，不落全量、不截断）；`MediaPcmChunk.origin_sec` —— 音轨在容器里的起点（字幕对齐要用）；`decode_media()`（全量）保留不动 |
| `app/apps/qwen3_asr/board_mic.py` | **新增** | `BoardMicController` —— 板载采集子进程的起停与状态（§8.11.2） |
| `app/apps/qwen3_asr/static/app.js` | **改** | 四个来源页签 + 板载面板；去掉「60 秒」文案与 `truncated` 分支；**视频播放**（blob / 媒体端点）+ 播放头字幕同步 |
| `app/apps/qwen3_asr/static/template.html` | **改** | 四个来源页签 + 板载面板（设备下拉 / 开始 / 停止 / 状态 / 日志）；**`<video>` 播放器 + 字幕层** |
| `app/apps/qwen3_asr/static/app.css` | **改** | 来源页签 / 面板 / 播放器与字幕层样式 |
| `app/apps/qwen3_asr/manifest.json` | **改** | `demo_guard.stop_endpoints` 增加 `POST /api/qwen3-asr/live/board/stop` |

> ⚠️ **AGENTS.md 禁令**：`app/apps/*/static/app.js` **不得**改动 framework 注入逻辑
> （`_loadedApps` / `_wizardMounted`）。本方案只动本卡自己的 `app.js`，不碰 `framework.js`。

#### 5.2.1 实施时**追加**的文件（清单初稿没列，但缺了跑不起来）

| 文件 | 动作 | 为什么必须有 |
|---|---|---|
| `app/apps/qwen3_asr/static/capture-worklet.js` | **新增** | `AudioWorkletProcessor` 必须在 **AudioWorkletGlobalScope** 里注册 —— 那个作用域只能通过 `audioWorklet.addModule(url)` 加载，**不能**用 `document.head.appendChild`（那是主线程作用域）。所以 worklet 必须是**单独一个文件**。 |
| `scripts/verify_asr_live.py` | **新增** | 实时链路的**契约级**回归：用假 SDK session 驱动 `_LiveAsrSession`，把「收尾必须带最后一块」「块边界 ≡ 文件路径」「背压 / 丢包 / 熔断 / 端点」等**协议契约**钉死。**不需要 NPU、不需要服务在跑** → 能进 CI。 |
| `scripts/make_asr_live_wav.py` | **新增** | 生成实时链路的测试音频（16 kHz / s16le / 单声道，末尾补静音）。为什么必须预处理见 §5.2.2。 |
| `scripts/make_asr_ref_video.py` | **新增** | 生成 `/tmp/asr_ref/asr_real_audio.mp4`（**原始 `asr_en.wav`** → AAC 音轨 + 320×240 黑帧）。W7（视频路径文本 ≡ C demo）/ U14（字幕随播放头）用例的素材。⚠️ `/tmp` 是 tmpfs，**板子重启即丢** → 丢了只会让用例**静默 SKIP**，不报错（实测：板子重启后 88 通过 → 81 通过 / 3 失败） |

#### 5.2.2 测试音频：**两个采样率**，同源生成

采集契约要求 16 kHz（§5.1），但三条路径的**输入采样率**不一样：

| 路径 | 输入 | 说明 |
|---|---|---|
| 文件路径（`POST /transcribe`） | **16 kHz** | 契约要求 |
| 板载路径（`asr_mic_agent.py --file`） | **48 kHz** | 模拟 ALSA 设备（ES8323 原生 48 kHz），采集端线性插值降到 16 kHz |
| 浏览器路径（`getUserMedia`） | **48 kHz** | ⚠️ Chromium 的假麦克风**固定以 48 kHz 播放**（实测），与文件本身的采样率无关 |

所以 `scripts/make_asr_live_wav.py` 生成**两份**同源资产：

```
/tmp/asr_ref/asr_48k_mono_s16.wav   ← 浏览器假麦克风 + 板载 --file
/tmp/asr_ref/asr_16k_mono_s16.wav   ← 文件路径（参考）
```

16 kHz 那份是用**与服务端同一套**线性插值
（`app.core.audio.capture.resample_s16_linear`）从 48 kHz 降下来的 →
它与「板载路径把 48 kHz 文件经 `StreamResampler` 降到 16 kHz」的输出
**逐字节相同**（已验证）→ 两条路径的参考文本必然一致。

> ⚠️ **初稿这里写的是「预先降到 16 kHz，浏览器与板载就一致了」——
> 那是错的**：Chromium 的假设备以 48 kHz 播放，浏览器再降采样回 16 kHz
> 时用的是 **WebAudio 自己的**重采样器（带滤波和延迟），与我们的线性
> 插值**不是同一个算法** → 两端 PCM 不可能逐字节相同。
> 实测：WebAudio 重采样后的 PCM 与源文件在第 78 个字节后就开始发散。

#### 5.2.3 「跨端一致」到底能断言到哪一步（实测结论）

阶段二实施时用探针把 Chromium 的采集链路量化了一遍：

| 事实 | 实测值 |
|---|---|
| 假麦克风的播放采样率 | **48 kHz**（与文件采样率无关） |
| 采集链路的**固定延迟** | ≈ **480 个 16 kHz 采样（30 ms）** |
| 采样被**调理**的幅度 | 16 kHz 域下最大 **8 LSB**（≈ 0.02%） |
| 负载高时**偶发丢帧** | 10 ms（= 1 个采集 buffer），实测出现过 2 次 |

于是 B3「同一段录音，浏览器采集与板载采集的最终文本**逐字节相同**」
被拆成**三条可判定**的命题（见 §9.2.1）：

* **V6a 算法一致** —— 两端用**同一套**线性插值（Python 的
  `StreamResampler` 与 JS 的 `_resampleTo16k`）→ 同一段输入 PCM 出来的
  16 kHz PCM **逐字节相同**。这条是**产品代码的性质**，与测试环境无关。
* **V6b 结果一致** —— **同一段 PCM**（浏览器实际上传的那段）从两条
  链路进来 → 文本**逐字节相同**。
* **V6c 素材一致** —— 两端拿到的确实是同一段录音（包络相关 + 逐段
  细结构相关 + 偏移不跑飞）。

**实测结论**：浏览器路径的最终文本与文件路径的参考文本**逐字节相同**
（不是「基本相同」）—— 因为那 30 ms 的固定延迟与 8 LSB 的调理量，
在 1 秒一块的 mel 切分下不改变 token。

末尾还要补 **3 秒静音**：浏览器端的「录音」不会在源文件结束时停
（Chromium 假麦克风放完文件会继续输出静音），板载路径喂完就收尾 ——
两边音频不等长，末尾 token 会分叉。补足静音后**两条路径在同一点命中
端点**，切出的句子完全一致。

> 浏览器侧还有一个细节：WebAudio 把 s16 采样除以 32768 变成 float，
> 前端必须**乘回 32768**（不是 32767）才能逐位还原 —— 否则每个采样被
> 压低约 3e-5，幅度上看不出来，但 mel 频谱末位比特会变、token 可能翻转。
> 另外**量化必须向零截断**（numpy `astype("<i2")` 的语义），不能四舍五入 ——
> 差 1 LSB 就会让「跨端逐字节一致」不成立。

### 5.3 HTTP 接口与字段

#### `POST /api/qwen3-asr/live/start`

请求体（JSON）：

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `source` | `"browser" \| "board"` | 是 | — | 采集端来源，仅用于日志与 UI 标识 |
| `sample_rate` | `int` | 是 | — | 必须是 `16000`（否则 400） |
| `n_channels` | `int` | 否 | `1` | 只支持 `1` |
| `sample_width` | `int` | 否 | `2` | 只支持 `2`（s16le） |
| `chunk_ms` | `int` | 否 | `500` | 采集端攒块粒度 |

响应：

```json
{"ok": true, "stream_id": "live-<hex>", "session_ttl_sec": 600,
 "chunk_seconds": 1.0, "endpoint": {"silence_ms": 1200, "threshold_dbfs": -45}}
```

#### `POST /api/qwen3-asr/live/chunk`

| 位置 | 字段 | 类型 | 说明 |
|---|---|---|---|
| header | `X-Asr-Stream-Id` | `str` | 会话 id；缺失/过期 → `400` / `404` |
| header | `X-Asr-Seq` | `int` | 块序号；**缺号 → 记 `dropped` 并上报** |
| body | — | `application/octet-stream` | 裸 s16le PCM（**无 WAV 头**） |

响应：

```json
{"ok": true, "seq": 12, "round": 7, "committed": "...", "unfixed": "...",
 "language": "English", "lag_ms": 143, "buffered_chunks": 0, "dropped_chunks": 0}
```

> `round` 为 `0` 表示本块**未凑满 1 秒**（只缓冲，尚未推理）。
> 逐轮增量仍走**现有 WS `round` 帧**，此处不重复携带文本。

#### `POST /api/qwen3-asr/live/finish`

| 位置 | 字段 | 类型 |
|---|---|---|
| header | `X-Asr-Stream-Id` | `str` |

响应：

```json
{"ok": true, "language": "English", "text": "final transcript",
 "elapsed_sec": 12.3, "endpoint_hit": false}
```

#### `DELETE /api/qwen3-asr/live/stop`

响应：`{"ok": true, "dropped_chunks": 0}` —— 立即中止、释放 SDK 会话。

### 5.4 WS 帧扩展（沿用现有 `/ws`）

现有帧类型 `snapshot` / `state` / `round` / `done` / `error` **不变**，新增两个：

| 帧 | 字段 | 语义 |
|---|---|---|
| `gap` | `{"dropped_chunks": n, "from_seq": a, "to_seq": b}` | 音频有丢失，识别结果**可能有缺口** |
| `endpoint` | `{"language": "...", "text": "...", "index": k}` | 一句话结束（端点检测命中），前端可分段展示 |

### 5.5 浏览器实现要点

| 项 | 做法 | 注意 |
|---|---|---|
| 采集 | `getUserMedia({audio:{channelCount:1, echoCancellation:true, noiseSuppression:true, autoGainControl:true}})` | 与官方 demo 同款三件套 |
| 音频图 | **优先 `AudioWorklet`**，`ScriptProcessor` 仅作回退 | 官方 demo 用的 `ScriptProcessor` **已废弃**；worklet 攒够 4096 采样再发（逐 quantum 发就是每秒 375 条 `postMessage`） |
| 上下文采样率 | **原生采样率**（`new AudioContext()`，通常 44.1/48 kHz） | ⚠️ **不要**用 `new AudioContext({sampleRate:16000})` —— 那会让 **WebAudio 自己的**重采样器介入，与板载的线性插值不是同一个算法 → 跨端文本分叉（§5.2.3） |
| 重采样 | **前端自己做**整块线性插值到 16 kHz（`_resampleTo16k`） | ⚠️ **不要**照抄官方 demo 的「逐帧独立插值」——帧边界相位不连续会累积漂移（报告 §5.3.6 第 7 条）；也**不要**用四舍五入量化（要与 numpy `astype("<i2")` 一致：向零截断） |
| 攒块 | `chunkSamples = 16000 × chunk_ms/1000` = 8000 | |
| 发送 | `await fetch('/live/chunk', ...)` | **串行 await = 天然背压**（官方 demo 同款） |
| 丢块 | 发送队列满 → 丢**最旧**块 + 计数 | 保实时性优先 |
| 收尾 | 停设备前先 `flush` 重采样器 + **等队列排空**再 `/live/finish` | 否则最后 1~2 个采样与最后半秒音频会丢（文本末尾可能少词） |
| 渲染 | 复用现有 `round` 帧处理逻辑 | 实时模式复用「实时结果 / 识别过程」两种显示 |
| 离开页面 | `DemoGuard` 触发 `DELETE /live/stop` | ⚠️ 必须在 `beforeunload` 兜底 |

### 5.6 板载 ALSA 实现要点

**依赖现状（已核实）**：`sounddevice` / `pyaudio` / `webrtcvad` **均未安装**；
`libasound.so.2` **存在**（`/usr/lib/aarch64-linux-gnu/libasound.so.2`）。

→ **用 `ctypes` 直接调 ALSA，零新增 pip 依赖**（不污染 `pyproject.toml`）。

| 项 | 做法 |
|---|---|
| 设备 | 默认 `hw:1,0`（ES8323 板载麦，`pcmC1D0c`）；HDMI-in 为 `hw:0,0`（`pcmC0D0c`）。两者都验证过存在 |
| 权限 | `/dev/snd/pcmC1D0c` 是 `root:audio` `rw-rw----` → 采集进程须在 **`audio` 组**（或以 root 跑，与 systemd 一致） |
| 独占 | ALSA 默认独占；采集进程**独占**设备，webui 不碰声卡 |
| 采样率 | ES8323 原生多为 48 kHz → 采集端线性插值到 16 kHz（与 SDK `_resample_linear` 同款，保持一致） |
| 阻塞 | `snd_pcm_readi` 阻塞 → 必须放**独立进程**（`tools/asr_mic_agent.py`），不放 webui 进程 |
| 进程间 | 采集进程 → HTTP `POST /live/chunk`（带 `X-Asr-Seq`），与浏览器走**同一套接口** |

**为什么独立进程**：
1. ALSA 读是阻塞 I/O，塞进 webui 会占死线程；
2. 设备独占：webui 还要提供其它服务，不该被声卡绑住；
3. 采集进程可独立重启 / 独立做成 systemd 单元，webui 重启不影响它。

---

## 6. 背压策略（三层）

> 官方 demo **完全没有背压**（报告 §5.3.6 第 3 条）——推理慢于推送时 `state.buffer`
> 无限增长、延迟单调恶化。**这是我们必须自己补的第一件事。**

### L1 采集层（浏览器）

- 发送队列上限 `MAX_PENDING = 2` 块（1 秒音频）。
- 队列满 → **丢弃最旧的块**，`dropped_chunks += 1`，继续发最新的。
- **理由**：实时场景下「晚到的旧音频」价值低于「最新的音频」；宁可丢字，不可延迟膨胀。

### L2 传输层

- 客户端**串行** `await` 响应 → 后一包的发送被前一包的响应阻塞。
- 这与官方 demo 一致（`demo_streaming.py:356-378` 的 `pump()`）。
- 效果：**客户端发送速率自动被服务端推理速率钳住**。

### L3 服务端（`_LiveAsrSession`）

| 机制 | 参数 | 行为 |
|---|---|---|
| **串行提交** | 单线程 | 保证同一会话内 `chunk_idx` 严格有序，`respond()` 不并发 |
| **有界缓冲** | `MAX_BUFFER_CHUNKS = 3` | 缓冲积压超过 3 块（3 秒）→ 丢最旧 + 计数 |
| **丢包上报** | — | 丢块后立即广播 WS `gap` 帧；前端显示「⚠ 有音频丢失」 |
| **连续丢失熔断** | `MAX_CONSECUTIVE_DROPS = 5` | 连续丢 5 块 → 主动结束本轮并报错（状态不可信了） |
| **会话上限** | `MAX_SESSION_SEC = 300` | 实时流**不套用 60 s 截断**（报告 §3.6：官方流式无时长上限），改用 300 s 强制重置会话，防止上下文无限增长 |

### 时延预算（目标）

| 环节 | 预算 |
|---|---|
| 采集缓冲（500 ms 块） | 0~500 ms |
| 服务端攒满 1 s | 0~1000 ms |
| RPC 往返 + 单轮推理 | 板端实测 70~150 ms（card 1） |
| **合计** | **≈ 0.6 ~ 1.7 s**（含一轮推理） |

> 注：板端实测 RTF 会随音频变长而上升（120 s 正常语音 RTF 0.144），
> 长会话的单轮耗时会比上表大 —— 这是**全量重喂**的固有代价，不是背压能消除的。

### 喂送策略：**留住最后 1 个采样**（阶段二实施时定下的关键决策）

SDK 的在线路径有一条硬契约（§4.2）：**带 ``asr_stream_final=True`` 的
那次调用必须携带本会话的最后一块**。最后一块不打回滚，所以最后几个
token 才会被确认 —— 若收尾时没有**新**音频可带，SDK 会警告「最后 N 个
token 未确认」，最终文本会**丢掉最后几个词**。

三种收尾策略的对比（总长 16.0 s / 16.5 s）：

| 方案 | 收尾调用携带 | 与文件路径 |
|---|---|---|
| **留住最后 1 个采样**（2 字节） | 1.0 s / 0.5 s（= 末块） | **逐字节相同** ✅ |
| 多留一整块（1 s 前瞻） | 空 / 空（→ 丢末尾几个词） | **不一致** ❌ |
| 不留（直接收尾） | 空 / 空 | **不一致** ❌ |

**为什么留住 1 个采样就够**：SDK 内部按**累计采样数**切 1 秒块，与调用方
怎么切块无关。收尾时把这 1 个采样补上，累计数与「一次性喂完整段」
**完全一致** → 切出来的块边界、每块内容、最后一块的打标全都一致 →
与文件路径**逐字节等价**。代价是 1 个采样 = 62.5 µs，**零可感知延迟**；
而「多留一整块」的方案要付整整 1 秒（首字延迟直接翻倍）。

落地位置：`app/core/audio/capture.py::PcmFeedBuffer`。

> ⚠️ 由此推出两条实现约束：
> 1. 服务端**不能**把 500 ms 的采集块直接当 1 秒块喂 —— 必须保留
>    「不足一块的尾巴」，收尾时一起交出去。
> 2. 端点命中时，**触发端点的那一块也要喂给模型**（§7.2 约束 1），
>    端点只决定「何时收尾」，不决定「识别什么」。

### 丢块记账：**在入队时**判定，不在 worker 里

采集端丢的块只有「下一个块的序号」能证明 —— 而那个序号在**入队时**就
看到了。若等到 worker 处理时再判定，会和服务端「缓冲满丢最旧」**重复
计数**（同一块既被算成「缓冲丢的」又被算成「序号缺号」），熔断阈值
也会被提前触发。

所以：`_classify_seq()` 在 `submit()` 里调用（推进 `_expected_seq`），
服务端丢块只**记一次**。

---

## 7. 端点检测策略（服务端）

### 7.1 判定

**纯能量门限**（不引入 `webrtcvad` 依赖）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `silence_threshold_dbfs` | `-45` | RMS 低于此值算静音 |
| `endpoint_silence_ms` | `1200` | 连续静音超过此时长 → 端点 |
| `max_silence_sec` | `30` | 纯静音（从未识别到语音）超过此时长 → 强制结束会话 |
| `min_speech_sec` | `0.3` | 单块能量峰值需超过此值才计入「有语音」 |

判定逻辑（主进程侧，`app/core/audio/endpoint.py`）：

```
每收到一块 PCM:
    rms_dbfs = 20*log10(sqrt(mean(x^2)))
    if rms_dbfs > silence_threshold_dbfs:
        voiced = True;  silence_run = 0
    else:
        silence_run += chunk_ms
    if voiced and silence_run >= endpoint_silence_ms:
        → 端点：提交 final=True → 关闭 SDK 会话 → 广播 endpoint 帧
          → 自动开新会话（连续对话模式）
    if not voiced and total_silence >= max_silence_sec:
        → 强制结束会话（不产 endpoint 帧）
```

### 7.2 三条硬约束

1. **静音仍然要喂给模型。** 端点检测**不改变**「全量重喂」语义 —— 静音照样进 `audio_embed_history`。
   理由：模型的音频上下文里留着静音，与 C demo 行为一致；剪掉静音会切碎上下文，
   反而伤害跨静音的识别连续性（报告 §7.1 表格中「静音段不喂模型」一栏被标为不推荐）。
2. **端点只决定「何时收尾并重置会话」，不决定「识别什么」。**
3. **端点后开新会话 = 新 `asr_stream_id`。** 已确认文本在 SDK 侧清零，
   前端负责把多句拼起来（用 `endpoint.index` 编号）。

### 7.3 与 `60 s` 上限的关系

- 文件路径：`app/core/audio/decode.py:49` `MAX_AUDIO_SECONDS = 60.0`（**截断**，与 C demo 一致）—— **不动**。
- 实时路径：**不套用** 60 s。改用 §6 的 `MAX_SESSION_SEC = 300` 强制重置。
  依据：报告 §3.6 —— 官方 Python 流式路径**完全不做时长检查**，
  1200 s 的上限只管离线路径。

---

## 8. 文件 / 视频实时模式：方案优化

> **本节回答**：除麦克风外，还要兼容「**音频文件实时 ASR 输出**」与
> 「**视频文件（含音轨）实时 ASR 输出**」时，上面的方案要怎么改。

### 8.1 结论先行：不需要三套方案，只要一个内核 + 三种游标推进器

**音频文件与「视频含音轨」在 ASR 侧完全同构** —— 两者都走
`app/core/audio/decode.py:decode_media` → PyAV 取**第一条音轨** → 单声道 s16le PCM。
视频的画面部分对 ASR **完全无关**，只是前端要额外做「字幕与画面对齐」。

所以三种"实时"其实是同一个东西：**一条带时间轴的转写流**。差别只在**游标由谁推进**：

| 模式 | 音频从哪来 | 时间轴 | 可 seek | 能否丢块 | VAD 的作用 |
|---|---|---|---|---|---|
| **麦克风实时** | 采集端推流 | 真实时间 | ❌ | ✅ 丢旧块 | 决定端点 → 收尾重置 |
| **音频文件实时** | PyAV 流式解码 | 文件时间 | ✅ | ❌ **绝不丢** | 仅分段显示 |
| **视频（含音轨）实时** | 同上（第一条音轨） | 文件时间 | ✅ | ❌ **绝不丢** | 仅分段显示 + 画面对齐 |

⚠️ **背压与 VAD 的语义在文件模式下是反的**（§8.6 / §8.7）—— 这是本次扩展最需要小心的地方。

### 8.2 SDK 侧唯一缺口：**时间戳**

现状：`asr_round` 载荷 = `{type, round, committed, unfixed, language}`
（`asr.py:775-789`）—— **没有任何时间信息**。

文件实时输出必须知道「这段文字对应文件里第几秒」，否则字幕无法与播放头对齐（必然漂移）。

**扩展方式（纯加法，零回归）**：

```python
# SDK 侧 _round_payload 加两个字段
{"type": "asr_round", "round": 7,
 "chunk_start_sec": 6.0,     # ← 新增
 "chunk_samples": 16000,     # ← 新增（16 kHz 基准的实际样本数）
 "committed": "...", "unfixed": "...", "language": "English"}
```

- **向后兼容**：主进程 `service.py:249-254` 用 `payload.get(...)` 带默认值 →
  老 worker / 新主进程 / 新 worker / 老主进程**任意组合都能跑**。
- `chunk_end_sec` **不在 SDK 里算**，由主进程算：`chunk_start_sec + chunk_samples / 16000`。
  理由：**时间轴是主进程的业务事实**（只有它知道文件的时间轴），
  SDK 只回报"我处理了多少样本"这个物理事实 —— 符合 AGENTS.md
  「worker 不理解业务语义」的分工。

**起点由主进程给，不让 SDK 自己累加**：

| 键 | 方向 | 说明 |
|---|---|---|
| `options["asr_stream_origin_sec"]` | 主 → worker | **会话**在调用方时间轴上的起点（秒，float）。**缺省 0.0**。会话创建时冻结 |
| `payload["chunk_start_sec"]` | worker → 主 | 本块在时间轴上的起始秒 |
| `payload["chunk_samples"]` | worker → 主 | 本块实际样本数（16 kHz 基准） |

> ⚠️ **实现时改过一次语义**：原方案写的是「每次调用传本块的起始秒」
> （`asr_chunk_start_sec`）。实现下来那个设计**太容易出错** —— 调用方必须自己
> 跟踪位置，一旦漏传就静默错位。改成**会话级原点**后，SDK 自己按
> `origin + chunk_idx × 1s` 推算每一块，调用方只管在**建会话时**给一次原点。
> **seek = 换新会话（新 id）+ 新原点**，语义天然正确。
>
> 键名因此改为 `asr_stream_origin_sec`（避免"chunk 起始"与"会话原点"混淆）。

**为什么不让 SDK 累加**：seek 时主进程知道真实位置；SDK 内部累加会在跳块后错位
（SDK 不知道被跳过了多少）。主进程给起点 = 唯一正确的做法。

### 8.3 统一内核：`ChunkScheduler`

新增 `app/core/audio/stream.py`：

```python
class ChunkScheduler:
    """把 PCM 流切成固定长度的块并逐块推进转写。

    三种实时模式共用同一个内核，只有「游标推进器」不同：
      - AppendCursor   麦克风：只能往前（append-only）
      - TimelineCursor 文件：可 seek，靠 chunk 缓存回退
    """
    chunk_samples: int = 16000      # 1 秒 @16 kHz

    def feed(self, pcm: bytes) -> None          # 追加到 pending
    @property
    def pending_samples(self) -> int
    def pop_chunk(self) -> Optional[bytes]      # 取下一个整块（不足则留 buffer）
    def reset(self) -> None
```

- 切块边界**由 SDK 唯一决定**（SDK 的 `samples_per_chunk`）→ 但主进程也要按同一
  规则对齐，否则 `chunk_start_sec` 算不准。
  → **约定**：主进程与 SDK 都用 `16000` 样本 = 1 秒，且主进程按累计样本数对齐切，
  SDK 收到的每块恰好 1 秒（最后一块可能不足 1 秒 → `chunk_samples` 回报实际值）。

### 8.4 流式解码（避免长文件全量入内存）

`decode.py` 现有 `decode_media()` 是**全量**解码（长视频会吃爆内存，且必须先解完才能开跑）。
文件实时需要**边解边喂**：

| 新增 | 位置 | 说明 |
|---|---|---|
| `iter_media_pcm(source, *, chunk_samples)` | `app/core/audio/decode.py` | **generator**，逐块 yield PCM |
| WAV 分支 | — | `wave.Wave_read.readframes(n)` 天然支持流式读 |
| 容器分支 | — | 复用 `_decode_with_pyav` 的逐 frame 逻辑，改成 generator 版本 |

- **`decode_media()` 保持不变**（全量路径零回归，现有 `/transcribe` 继续用）。
- 容器分支现在**边解边截**（`decode.py:158-160`），generator 版本改为
  `max_seconds=None` 时**不截断**、由调用方控制何时停。
- `DecodedAudio` 需**加一个字段** `origin_sec: float = 0.0` —— 音频流在容器内的
  起始秒（§8.9 的时间轴原点），流式解码器也要一并返回。

### 8.5 文件模式的背压语义 —— 与麦克风**相反**

| | 麦克风模式 | 文件模式 |
|---|---|---|
| 转写跟不上 | **丢旧块**，保实时性 | **绝不丢**（丢 = 永久丢内容） |
| 动作 | 丢最旧块 + `dropped_chunks++` + 推 `gap` 帧 | 标记 `behind_sec`，**等**或**让前端等** |
| 用户感知 | 文字有缺口 | 字幕滞后于画面，但**内容完整** |

**文件模式的正确背压**：

1. 转写游标落后播放头 → `behind_sec = playhead - cursor`；
2. `behind_sec > 2.0` → WS 推 `{"type":"behind","behind_sec":X}`；
3. 前端据此**暂停播放**（或提示"转写追赶中"）—— 由用户选择；
4. 缓冲上限 `MAX_BUFFER_CHUNKS` 在文件模式下**改为阻塞等待**而非丢弃。

⚠️ **这两套背压必须分开实现**，不能共用一套丢弃逻辑 —— 共用会导致文件模式静默丢内容。

### 8.6 文件模式的 VAD 语义 —— 只观察，不动作

| | 麦克风模式 | 文件模式 |
|---|---|---|
| VAD 判定 | 静音持续 `endpoint_silence_ms` | 同左 |
| VAD 动作 | **收尾（final=True）+ 关会话 + 开新会话** | **仅在字幕里断句**（不重置会话、不影响转写） |
| 对转写连续性的影响 | 切断了上下文 | **零影响** |

→ 端点检测模块要能「**只观察不动作**」：加一个 `action_on_endpoint: bool` 开关
（麦克风模式 `True`，文件模式 `False`）。

**为什么文件模式不靠 VAD 断句**：文件已有确定边界（一句话 = 一个语义段落），
按 VAD 切会与模型自己的断句打架；VAD 在这里的唯一价值是**给字幕 nicer 的换行**。

### 8.7 seek 与 chunk 缓存

文件实时与麦克风实时的**根本差异**：文件可以来回拖。

**缓存**：

```python
# chunk_index → {committed, unfixed, language, chunk_start_sec, chunk_end_sec}
self._chunk_cache: OrderedDict   # LRU，默认 600 块 = 10 分钟
```

**seek 流程**（`target_sec`）：

```
1. 命中缓存？ → 直接返回该区间的 chunks（零推理，零延迟）
2. 未命中     → 找到 ≤ target_sec 的最近缓存点
              → **重建 SDK 会话**（新 asr_stream_id）
              → 从该点往后重新转写
```

⚠️ **seek 必须重建 SDK 会话** —— `audio_embed_history` 是**累积**的，
跳过的块补不回来；不重建的话，新位置的上下文里会残留旧位置的音频。

**幂等性**：同一 chunk 重复转写结果**应当一致**（贪心采样 + 相同上下文）。
若因浮点/边界差异产生 ±1 token 的偏差，属可接受范围（与 C demo 的
「未确认尾巴可改写」同理），但**已确认文本（committed）必须一致**。

### 8.8 长文件与 `60 s` 上限

现有的 `app/core/audio/decode.py:49` `MAX_AUDIO_SECONDS = 60.0` 是**硬截断**，
文件模式**不能沿用**（30 分钟视频会被砍成 1 分钟）。

**好消息**：阶段一的**流式会话机制让长文件不再需要分段** ——
`audio_embed_history` 与 `committed_token_ids` 跨 request 存活，
所以可以**连续喂 30 分钟而不切断上下文**。

**坏消息**：上下文线性增长，会撞 `max_context_len = 24576` 的墙。
按上一轮实测的 13 audio token/秒：

| 音频长度 | 上下文占用 |
|---|---|
| 60 s | 4.1% |
| 120 s | 8.1% |
| 600 s | 40.3% |
| ~1200 s | ~80%（逼近硬墙） |

**分级策略**（**已被 §8.8.3 修正**，见下）：

| 条件 | 策略 | 代价 |
|---|---|---|
| 时长 ≤ `MAX_SESSION_SEC`（文件模式 ~~900 s = 15 min~~ → **60 s**，见 §8.8.3） | **单会话连续喂**，上下文完整 | 无 |
| 时长 > 上限 | **切段**，每段新会话 | 段间上下文丢失；UI 标注「分段转写」 |

⚠️ **文件模式的会话上限比麦克风模式（300 s）短得多** ——
见 §8.8.3：**单个会话超过 ~60 s 后模型的增量回退策略会自锁**
（`stable_count` 恒为 0 → 已确认文本不再增长 → 永久卡死）。
文件模式的正确做法是**分段会话**，而不是「一个超长会话」。
麦克风模式的 300 s 上限是另一回事（端点检测切句），两者**不能共用常量**。

> 长音频有**两个**独立的坑：§8.8.3 是「跑到一半卡死」（自锁），
> §8.8.4 是「跑到最后一段崩掉」（末块太短 → mel 反射填充越界）。
> 两个都修完，97 分钟视频才能从第一秒跑到最后一秒。

#### 8.8.1 ✅ 实施结果：60 s 硬截断**已移除**（2026-11）

清单初稿只写了「文件模式不能沿用 60 s」，没定具体做法。实施时选了
**改走增量会话**，而不是「把上限调大」：

| 做法 | 代价 | 取舍 |
|---|---|---|
| 调大 `MAX_AUDIO_SECONDS` | 音频越长 RTF 越差（C demo 的离线在线编码器把**整段**反复重喂），长录音会慢到不可用 | ✗ |
| **改走增量会话**（采用） | 每 1 秒一块喂给 SDK，历史单调增长 → 代价**线性** | ✓ |

落地：

| 位置 | 内容 |
|---|---|
| `app/core/audio/decode.py::iter_media_pcm()` | **流式解码**（WAV 按帧切块；容器用 PyAV 增量解码，**不落全量**）。原 `decode_media()`（全量）**保留不动** |
| `app/apps/qwen3_asr/service.py::transcribe_stream()` | 增量会话；`PcmFeedBuffer` 留住末采样；`_FILE_SESSION_MAX_SEC = 60`（超了切新 `asr_stream_id`，文本分段拼接）—— **值已被 §8.8.3 修正** |
| `app/apps/qwen3_asr/api.py::POST /transcribe` | 响应**去掉 `truncated` / `duration_sec`**，加 `source_name`；`last_result` 加 `audio_sec` / `segments` |

#### 8.8.2 ⚠️ 主进程**不要**重采样 —— 重采样归 SDK

第一版实现里，主进程侧用 `StreamResampler` 把音频降到 16 kHz 再喂 SDK。
**这是错的**，踩了两个坑：

1. **24-bit WAV 直接崩** —— 主进程的 `StreamResampler` 只认 s16le
   （`np.frombuffer(..., dtype="<i2")`），而 C demo 的 `asr_en.wav` 是
   **s24le**（PCM 长度 2167377 = **奇数**）→
   `ValueError: buffer size must be a multiple of element size`。
   SDK 侧的 `_pcm_to_mono_float32` 本来就支持 2/3/4 字节。
2. **多一次量化** —— 主进程把 float 夹回 s16 再交给 SDK，多一次 ±1 LSB
   的误差机会。

SDK 的增量会话**自己负责重采样与切块**（`asr.py::_run_online_stream`，
注释原文：*「The incremental path owns the resampling」*）：

* 它的 `_StreamResampler` **按会话保持状态** → 跨块相位对齐；
* 它在 **16 kHz 网格**上切块 → 与「整段一次性」的切块边界**完全一致**
  （这正是 SDK 承诺的「两条路径文本一致」的地基）；
* 它在 `asr_stream_final=True` 时 flush 自己的重采样尾巴。

所以主进程只负责**按帧对齐地切块 + 透传源格式**。分块喂与整段喂的
逐字节等价性靠两条不变式保证：

* 块边界按**帧**对齐（`PcmFeedBuffer.frame_bytes`）→ SDK 的逐采样转换
  （s24le / s16le → f32）分块做与整段做结果相同；
* `PcmFeedBuffer` **留住最后 1 个采样** → 收尾那次调用一定带得下音频
  → SDK 能把末块标成 `is_last`（末块不回滚，与 C demo 一致）。

> **教训**：SDK 已经把「增量路径的重采样 + 切块」做完了。主进程再插一脚
> 不是「更保险」，而是**多一个出错的层** —— 而且这个层一插，24-bit 支持
> 就没了。跨进程契约要看 **SDK 那一侧的注释**，别凭直觉补一层。

#### 8.8.3 ❌→✅ 长音频中途卡死 —— 增量会话的「回退自锁」与分段会话（2026-11）

##### 现象

用 97 分钟的真人访谈视频（`【圆桌派 第八季】EP01`，含音轨，5868 s）
走「文件 / 视频」路径：**前面一段正常识别，后面逐渐出错** ——

* t=0~25 s 正常（「本节目由蚂蚁财富黑卡独家冠名播出…」），
* t=26 s 起**文本完全不动**，持续 **395 秒**零输出，
* t=420 s 突然开始输出，但**每轮 +700 字**（远超语速 5 字/秒），
  且内容是 **t=80~120 s** 的音频 —— 整体**滞后 340 秒**，
* t=460 s 起退化成 `<小红帽>《小红帽》<小红帽>…` 的无限重复。

##### 实测证据链

| # | 实验 | 结果 | 排除了什么 |
|---|---|---|---|
| 1 | PyAV 整条音轨解码 | 252714 帧 / 5868.01 s，pts **零跳变**，采样率 44100/立体声**全程恒定**，每 30 s 段都有声 | **不是解码侧问题** |
| 2 | 逐 30 s 段能量/频谱 | t=26~40 s 高频占比 40%（音乐），t=60~70 s **数字静音** | 这些段本来就无语音 |
| 3 | **单独**识别 t=80~120 s | ✅ 完全正确（「富有见地，所见不同。本节目由蚂蚁财富黑卡独家冠名播出…」） | **模型能识别这段** |
| 4 | **单独**识别 t=400~460 s | ✅ 完全正确（372 字对话） | 同上 |
| 5 | 连续流式跑同一段 | ❌ t=26 s 起冻结 395 s | **是增量会话的状态问题** |
| 6 | SDK 内部插桩（`_run_online_block`） | 见下 | — |

插桩数据（第 6 步）—— 每轮的内部状态：

```
chunk= 17 gen= 7 stable=2 ids= 44 audio_tok=234 txt= 54  unf='眼镜赞助播出。'
chunk= 18 gen= 5 stable=0 ids= 44 audio_tok=247 txt= 54  unf='眼镜赞助播出。'   ← stable=0
chunk= 25 gen= 8 stable=3 ids= 47 audio_tok=338 txt= 60  unf='。有风。'
chunk= 26 gen= 5 stable=0 ids= 47 audio_tok=351 txt= 60  unf='。有风。'         ← 从这里起永久 stable=0
...（400 轮完全一样，ids / txt 一动不动，audio_tok 一路涨到 780）
```

`audio_tok` 在涨（音频确实喂进去了），但 `ids`（已确认 token）**冻在 47**。

##### 根因：回退阈值把「无新内容」变成永久自锁

流式会话的推进规则（C++ `online_main.cc:482-497`、SDK
`asr.py::_run_online_block`，两者**逐条一致**）：

```
stable_count = len(round_tokens) - ONLINE_ROLLBACK_TOKENS      # 回退末尾 5 个
```

模型每轮**至少**要重述这 5 个回退 token。于是当某一轮「这一秒音频没有
新内容」（静音 / 纯音乐 / 人声间隙）时：

1. `len(round_tokens) == 5` → `stable_count == 0` → **一个 token 都不提交**；
2. `committed_token_ids` 不变 → 下一轮模型的「已确认文本」输入**完全相同**；
3. 输出不变 → 又只吐 5 个回退 token → **永久自锁**。

**触发是偶然的，但后果是永久的** —— 实测三个样本的冻结点各不相同
（t=25 / t=26 / t=40 s），一旦触发就再也出不来。

> 之所以能观测到「模型其实还在接收音频却不产出」：解锁只发生在某一轮模型
> 偶然多吐了几个 token（`stable > 0`）——此时它会把**积压的内容一次性
> 倒出来**（每轮 +700 字），于是文本与音频彻底脱节，最后 degeneration。

##### 与参考实现的对照：当前实现是否合理？

| 参考 | 关键约束 | 我们的实现 |
|---|---|---|
| **C++ `online_main.cc`** | `ONLINE_MAX_AUDIO_SECONDS = 60`，注释「音频过长会导致RTF降低」—— **硬截断** | ❌ 原先 900 s 单会话，**没对齐** |
| **C++ `online_main.cc`** | `build_llm_input()` 里 `MAX_CONTEXT_LEN` 检查，超了直接 `-1` | ❌ SDK 侧**没有**这条检查 |
| **C++ `online_main.cc`** | 回退策略、输入构造（`[prefix, audio, postfix, committed]`）、`keep_history=0`、`max_new_tokens=50` | ✅ **逐条一致** |
| **官方 Python `demo_streaming.py`** | `unfixed_chunk_num=4` / `unfixed_token_num=5` / `chunk_size_sec=1.0`；前缀回退取**累积文本**的尾部 K 个 token | ✅ 语义等价 |

**结论**：**SDK 层是合理的**（与 C++ demo 逐条对齐，问题不在 SDK）。
不合理的是**主进程这一层**：C++ demo 用 60 s 硬截断把「会话过长」这个
前提挡掉了，而 webui 去掉截断时只做了「改走增量会话」，**没把 60 s 的
会话上限带过来** —— 于是把 C++ 明确规避掉的场景（长会话）放大了 100 倍。

官方 Python 实现给了一个更根本的提示：它每轮是**整段重算**
（`_raw_decoded = prefix + gen_text`），不存在「提交 / 不提交」的二值状态，
所以天然没有这个自锁。RKNN 版是**增量提交**，必须显式兜底。

##### 修复（`app/apps/qwen3_asr/service.py`）

| 改动 | 内容 |
|---|---|
| **会话分段** | `_FILE_SESSION_MAX_SEC` 900 → **60**（对齐 C++ demo 的 `ONLINE_MAX_AUDIO_SECONDS`）。长音频靠**分段会话**而不是单个超长会话；段边界**不丢内容**（收尾那次带 `asr_stream_final=True`，末块不砍尾 → 对齐 C demo 的 `is_last`） |
| **停滞检测** | `_on_delta` 里统计「已确认文本连续 N 轮长度不变」（SDK 侧等价于 `stable_count == 0`）。连续 **8 轮**（8 秒）不变 → **提前切 epoch**（换新 `asr_stream_id`，音频历史清零）→ 自锁立即解开 |
| **收尾解析修正** | SDK 的 `final_text()` 在「committed 里还没出现语言分隔符 `[PAD151704]`」时**原样返回**（形状是 `language None`，**无换行**）。`_split_language()` 早先把它**当正文**并进转写 —— 切 epoch 后一个静音段的收尾就会往文本里塞一句 `language None`（实测 120 s 样本里混进 6 处）。已修 |

> **为什么停滞检测阈值是 8 轮**：SDK 的前 4 轮本来就不提交
> （C++ 的 `remaining_rollback_rounds = 4`），要跳过这个冷启动窗口；
> 又不至于让正常的几秒沉默误触发。**误触发也无害** —— 切 epoch 不丢
> 内容，只是多开一个会话。

##### 回归

`scripts/verify_asr_long_audio.py` —— 两个用例（详见 §8.8.4 的用例表）：

| 用例 | 样本 | 断言 |
|---|---|---|
| A | 10 s 静音 + 15 s 真实语音 | 语音被识别出来（修复前**完全识别不到**）/ 没有 `language None` / 没有重复退化 / `epochs ≥ 2`（确实分了会话） |
| B | 6 s 语音 + **100 采样**的尾巴 | 状态是 `done`（不是 `error`）/ 语音仍被识别 / 没有 `could not broadcast` |

停滞检测的**机制本身**（状态机）另有确定性单测：`scripts/verify_asr_live.py`
的 **L12**（不需要 NPU、不需要服务、不依赖模型行为）—— 端到端能不能触发
自锁取决于模型在静音段会不会幻觉出文本（**概率性**），所以机制必须单独
钉死。

##### 教训

* **去掉截断 ≠ 把上限调大**。长音频的正确形态是**分段会话**，不是
  「一个超长会话」—— 参考实现（C++ demo）用 60 s 硬截断，恰恰是因为
  它知道单会话撑不住。
* **增量提交的算法必须显式处理「本轮没有新增」**。「不提交」在正常情况
  下是**精度机制**（回退待确认），在异常情况下会变成**死锁**。
  「连续 N 轮没进展」就是一个必须兜底的状态 —— 这类状态在**增量 /
  状态机 / 队列**类实现里是通病，值得在设计时就留出口。
* **回归要用「必然触发」而不是「碰巧触发」的样本**。这个 bug 在 15 s 的
  C demo 样本上根本不会出现（样本太短、且没有静音段）；必须专门造一段
  「静音 + 语音」才能稳定复现。


#### 8.8.4 ❌→✅ 长音频**最后一段**崩掉 —— 末块太短把 worker 打崩（2026-11）

##### 现象

§8.8.3 的分段 + 停滞检测上线后，97 分钟视频能跑到 **100%**（5868 轮全部
完成，文本持续均匀增长 ≈ 5 字/秒，12 段各贡献 300~500 字，无退化），
**但最终状态是 `error`**：

```
识别失败: LLMRPCSession[wdndds/rpc/qwen3-asr/worker-5266] worker error:
ValueError: could not broadcast input array from shape (160,) into shape (200,)
```

**17 分钟算力 + 前面所有文本一起丢** —— 只因为**最后一块**。

##### 根因：末块的长度 = 音频总长 mod 块长

增量会话的块是 **SDK 自己切的**（`asr.py::_run_online_stream`，16 kHz 网格）。
最后一块的长度 = 总采样数 mod 16000 —— **只要总长没落在网格上，末块就可能
只有几十个采样**（本片：196 采样 = 12 ms）。

SDK 的 mel 前端按 C 的语义把音频裁到 `n_frame * hop_length`
（1 帧 → **160 采样**），再做**反射填充**（需要 `n_fft // 2` = **200** 采样）。
C 的 `reflect_pad`（`utils/audio_utils.cc:111`）在这里是**越界读**（UB），
numpy 版把同一个表达式翻译过来就是一次**硬失败**：

```python
out[:pad_width] = audio[:pad_width][::-1]   # audio 只有 160 个采样
→ ValueError: could not broadcast input array from shape (160,) into shape (200,)
```

实测（纯 numpy，不需要 NPU）：

| 块长（16 kHz 采样） | 修复前 | 修复后 |
|---|---|---|
| 1 / 100 / 400 / 559 | ❌ `ValueError` | ✅ mel (128, 1) |
| 560 / 800 | ✅ | ✅ |
| 16000（正常块） | ✅ mel (128, 98) | ✅ **逐字节不变** |

> **为什么两条参考路径都没暴露它**
>
> * **C++ demo**：对 < `N_FFT` 的块，`audio_preprocess` 会算出**负的**
>   `n_frame`（`floor((100-400)/160)+1 = -1`）→ 负长度的 `std::vector` →
>   **更早的 UB**。C demo 的输入都是整秒的 WAV，踩不到。
> * **官方 Python `demo_streaming.py`**：`/api/finish` **直接丢弃**缓冲区里
>   的尾巴 —— 它**从不**喂短块。
>
> 两条参考路径都绕开了这个输入，而我们的实现**必须自己兜住**：末块是
> **契约的一部分**（C demo 的 `is_last` 语义就靠它确认最后几个 token）。

##### 修复

| 位置 | 改动 |
|---|---|
| `wdnai/src/wdnai/backends/asr.py::_reflect_pad` | 反射**只用实际存在的采样**（`w = min(pad_width, n)`），越界处补 0 —— 既不越界、也不抛错。C 那边是 UB，这边是**确定性行为** |
| `wdnai/src/wdnai/backends/asr.py::_run_online_block` | 块长下限从 `N_FFT` 提到 `N_FFT + HOP_LENGTH`（560）—— 保证能凑出 **2 帧** mel，裁剪后仍 ≥ 反射填充需要的 200 |
| `app/apps/qwen3_asr/service.py::_close_epoch` | **单段收尾失败不再毁全场**：记日志 + 丢掉这一段的尾巴（≤ 1 s）+ 继续下一段。连续 2 段都失败（worker 整体坏了）或整场一段文本都没产出 → 仍然报错 |

> **为什么容错放在主进程**：SDK 修的是**根因**（末块不再崩），但一段收尾
> 失败就报废整场识别，对 97 分钟的输入代价太大。**能续跑就续跑**，续不了
> （连续失败）再报错 —— 这是「长任务不该一错全没」的通用形态。

##### 回归

`scripts/verify_asr_long_audio.py` 的用例 B（**100 采样**的尾巴）：

| 断言 | 补丁在 | 补丁撤掉 |
|---|---|---|
| B1 状态是 `done` 不是 `error` | ✅ | ❌ `state=error` |
| B2 语音仍被识别 | ✅ | ❌ 文本为空 |
| B3 没有 `could not broadcast` | ✅ | ❌ 报错原文 |

（「补丁撤掉」= 临时把 SDK 的补丁回退再跑 —— 用**备份文件**做，不动 git；
实测 B1/B2/B3 如期 FAIL，恢复后全绿。）

##### 教训

* **「最后一块」是契约，不是边角料**。C demo 的 `is_last` 语义、官方
  Python demo 的「丢弃尾巴」都说明：**末块的长度是实现细节，调用方必须
  能处理任意长度**。numpy 移植把 C 的 UB 翻译成了**硬失败** —— 这不是
  numpy 的错，是**移植把「没说清楚的地方」暴露了出来**。
* **UB → 硬失败 是一次进步，但报错的位置得对**。C 那边是「读到垃圾还能
  跑」，我们这边是「明确报错」。可**该保证不越界的是块切割方（SDK）**，
  而不是让调用方去猜块有多长。
* **长任务不能「一错全没」**。97 分钟的识别里，任何一块的失败都不该让
  整场报废。分段 + 容错 = 把「一次失败」降级成「丢 1 秒尾巴」。


#### 8.8.5 ⚠️ 已知残留：模型偶发的**重复爆发**（非本实现缺陷，2026-11）

97 分钟视频跑通后（`state=done`，129 段 / 32025 字 / `epoch_errors=0`），
全文里有一处**局部重复**：

| 项 | 值 |
|---|---|
| 位置 | t=2291~2316 s（**一个 epoch 内**的 25 秒） |
| 形态 | 同一段中文（约 180 字）连续重复 **6~8 次** |
| 规模 | 约 940 字 / 全文 32025 字 ≈ **3%** |
| 该段的增速 | 已确认文本 **26 字/秒**（正常语速 ~5 字/秒）→ 模型在「倒带」 |

##### 三步排除：不是音频、不是模型能力，是**增量会话内**的状态

| 实验 | 结果 | 排除了什么 |
|---|---|---|
| 该段音频逐秒块的余弦相似度 | ≈ **0**（每秒都不同，是正常连续说话） | **不是音频重复** |
| 该段**单独**识别（35 s，新会话） | ✅ **完全正常**，内容与音频一致，无重复 | **不是模型识别不了** |
| 同一 epoch 内 committed 增速 | 26 字/秒（正常 5 字/秒） | 模型在复读自己上一轮的输出 |

##### 为什么**不在主进程做「文本去重」**

因为**分不清「模型循环」和「真人复读」**—— 而且本片里就有**真实重复**：

* `Human Rights International (HRI) is a non-profit international
  organization …`（节目插播的英文 PSA）在全片出现**多次**；
* `What problem with this country, what is it that we are seeing in
  China today?` 在 350 字内重复 **4 次**（PSA 自己的排比修辞）。

按「同一子串出现 ≥ 3 次就当退化删掉」的规则，会把上面这些**真实内容**
一起删掉 —— 那是**更糟**的错误。

C++ demo 用**同一款模型 + 同一套回退策略**，同样会出现这种复读（参考
实现没有这层保护）。所以它是**模型层的重复倾向**，不是「中途卡死 /
崩溃」那类实现缺陷；60 s 切段限制了它的**最长**长度（最坏一整个
epoch）。

> **如果以后要做**，判据必须同时满足：① 同一块在**一个 epoch 内**重复
> ≥ 4 次；② 重复块的**长度** ≫ 真人会复读的长度；③ 重复期间该 epoch
> 的文本增速远超语速。三条都中才截断 —— 单靠「子串重复次数」会误伤
> 上面那种真实的 PSA 排比。


### 8.9 视频同步（画面 vs 字幕）

| 项 | 做法 |
|---|---|
| 显示条件 | `chunk_end_sec <= currentTime + lead`（`lead` 默认 **0.0**） |
| 天然滞后 | 转写本身有延迟 —— 播到 `t` 时，`t+1s` 的音频可能刚转写完 → **字幕落后画面约 1~2 s** |
| 驱动 | `<video>` 的 `timeupdate`（~4 Hz）或 `requestAnimationFrame` 轮询 `currentTime` |
| 播放头落后转写 | 用户拖快于转写 → `behind` 帧 → 前端暂停 |
| 字幕导出 | 从 chunk 缓存直接生成 **SRT / VTT** —— **副产品**，几乎零成本 |

> **音频文件实时**用同一套机制，只是播放器是 `<audio>`，没有画面对齐这一层。

**时间轴原点**：字幕对齐的基准是**音频流在容器内的位置**，不是容器起点。
`decode.py:142` 现在只读了 `stream.rate`，**没读 `stream.start_time`** —— 需补一个字段
（`DecodedAudio` 加 `origin_sec`），前端播放头的 `currentTime` 要减去同一个原点。
实测本板 `app/apps/qwen3_asr/static/preview.mp4` 的 `container.start_time` / 视频 / 音频三者均为 `0`，暂无偏移；
但**不能假设所有文件都如此**（音轨晚于画面起点的文件很常见）。

#### 8.9.1 ✅ 实施结果：视频在 UI 上播，字幕跟着画面走

「含音轨的视频」这个来源**不只是**「拿它的音轨去识别」——**画面本身也在
UI 上播**，识别结果按播放头位置叠加在画面底部。

| 环节 | 落地 |
|---|---|
| 播放器 | `<video controls preload=metadata playsinline>` + 绝对定位的字幕层（压在画面底部） |
| 画面来源 | 本机上传 → **`blob:` URL**（浏览器本地，不经服务端）；设备端路径 → **`GET /api/qwen3-asr/media`** |
| 拖进度条 | 媒体端点用 `FileResponse` —— Starlette 的 `FileResponse` **支持 Range**（实测 206 + `content-range`），没有它浏览器只能从头播 |
| 字幕驱动 | `<video>` 的 `timeupdate`（~4 Hz）+ 每轮帧到达时各刷一次 |
| 时间轴换算 | 字幕时间 = `currentTime` − `audio_origin_sec`；`audio_origin_sec` 由解码层给出（`MediaPcmChunk.origin_sec`），挂进每轮帧（`round.audio_origin_sec`）与 snapshot |
| 字幕内容 | 已确认文本（`committed`，**累积**）+ 该轮未确认尾巴（`unfixed`，蓝色） |
| 自动播放 | 点「开始识别」时起播 —— 那是**用户手势**，浏览器允许带声音自动播 |

**新增端点 `GET /api/qwen3-asr/media?path=<绝对路径>`**

⚠️ 这个 webui **没有鉴权** → 该端点等于「任意文件读取」入口
（`?path=/etc/shadow` 能把 shadow 读出来）。所以**三道闸**全都要有：

| 闸 | 做法 | 实测 |
|---|---|---|
| 根目录白名单 | 只允许 `<assets>/uploads` 与 `<assets>/tts` | `/tmp/x.mp4` → **403** |
| 扩展名白名单 | 只允许 `MEDIA_EXTS`（音视频） | `/etc/passwd` → **400** |
| resolve 后仍在根内 | `resolve()` 规范化 `..` 与软链接 | 指向根外的软链接 → **403**；软链接到 `/etc/shadow` → **400** |

> **「resolve 之后仍在根内」这条是软链接的克星** —— 只做字符串前缀比较
> 的话，`uploads/x.mp4 -> /etc/shadow` 这种会漏过去。

#### 8.9.2 🐛 换来源后字幕串台 + 字幕显示「会被回滚的猜测」

**症状**（用户实测）：「流式语音识别」卡，从「音频文件」识别结束切换到
「含音轨的视频」后，**画面下方凭空多出一句「嗯哼。」** —— 而最终识别结果里
根本没有这句，且那个视频**还没开始识别**。

##### 根因（两层，都在前端）

**第一层：串台。** 字幕是按播放头去 `rounds` 里取轮的，而 `rounds` 在
「切换来源」时**不会**被清空 —— 切到视频后，字幕拿**上一来源**（音频文件）
的轮次来渲染。

**第二层：取错了字段。** 字幕取的是那一轮的 `unfixed`，而 `unfixed` 是
「本轮还没确认、**随时会被回滚推翻**」的**猜测**。

第 1 轮只有 1 秒音频，模型只能瞎猜 —— 实测它在英文音频上先吐了个中文
填充词「嗯哼。」，第 2 轮拿到更多上下文后**整个推翻了**：

| 轮次 | `committed` | `unfixed` |
|---|---|---|
| #1 | `''` | `'嗯哼。'` ← **从没进过 `committed`** |
| #2 | `''` | `'Uh huh. Oh yeah, yeah.'` ← 「嗯哼。」被丢弃 |
| #3~#4 | `''` | 继续累积英文 |
| #5 | `'Uh huh. Oh yeah, yeah! He wasn't even that big when'` | `' I started listening.'` |
| #16 | 全文 | — |

最终文本 = `Uh huh. Oh yeah, yeah! He wasn't even that big when I started
listening to him, ...` —— **「嗯哼。」从来就不存在**。

> 顺带确认：**不是视频解码多解了东西**。同一段音频分别走「音频文件」与
> 「含音轨的视频」两条路径，最终文本**逐字节相同**（实测两次都是 188 字）。
> mp4 音轨与源 wav 只差 38 ms 前导静音（AAC 编码器延迟）+ 485 个尾样本，
> 不影响转写。

##### 修法

1. **轮次绑定来源**：新增 `Qwen3Asr._roundsSource`，记下这批轮次来自哪个
   来源；`_renderVideoSubtitle()` 里加一道守卫 —— 轮次不是来自 `video`
   就不渲染字幕（**双保险**：即使识别中途切来源也不会串台）。
   `start()` 写入、`reset()` 清空；WS 推来轮次而本地没走过 `start()`
   （刷新页面时识别正在跑）时按当前来源认领。
2. **切来源时清字幕**（新增 `_onSourceChanged()`）：字幕是**视频专属**的
   展示位（音频文件来源下压根没有这个元素），切过去的那一刻它会「凭空
   冒出来」。同时把 `_audioOriginSec` 归零 —— 那是**上一个文件**的音轨起点，
   不归零的话播放头换算会取到错误的轮次。
3. **字幕改用「最终文本的前缀」**：长度 = 已确认部分 + 播放头所在轮的增量
   长度，取**最新一轮的 `committed`**（= 回滚后的最终文本）的前缀。
   于是字幕里出现的每个字都在最终结果里，而长度仍随播放头增长
   （U14 的两条断言「字幕随播放头变长」「后段包含前段」都还成立）。

> ⚠️ **别把字幕改成「只显示 `committed`」** —— 第 1~4 轮的 `committed`
> 都是空的（模型要 5 秒上下文才提交），U14 在 `t=2s` 处断言字幕非空会
> 直接失败。

##### 回归

`scripts/verify_asr_wizard.py` 新增 **U15**（3 条断言）：

| 断言 | 抓什么 |
|---|---|
| 音频文件识别出了文本 | 前置条件（没有文本就没法验串台） |
| 切到视频来源后字幕是空的 | 第一层（串台） |
| 字幕内容是最终文本的前缀 | 第二层（回滚的猜测） |

实测：回退修复后 U15 如期 **2 条 FAIL**（字幕 = `'嗯哼。'`），修复后
**92 通过 / 0 失败**。

### 8.10 接口增量

现有 4 个 live 端点**全部保留**，增量如下：

| 端点 | 变更 | 说明 |
|---|---|---|
| `POST /live/start` | **加字段** | `mode: "mic" \| "file"`；`file` 模式可带 `media_path`（设备端文件）或不带（前端分块喂） |
| `POST /live/chunk` | **响应加字段** | `chunk_start_sec` / `chunk_end_sec` / `cursor_sec` / `behind_sec` |
| `POST /live/seek` | **新增** | body `{"target_sec": 12.5}` → 重置会话 + 游标跳转；响应含命中的缓存 chunks |
| `GET /live/chunks` | **新增** | `?from_sec=&to_sec=` → 取缓存的字幕区间（供前端补齐 / 导出 SRT） |
| `POST /live/finish` | 不变 | 但响应加 `segments`（分段信息，长文件时 >1 段） |
| WS 帧 | **加 1 个** | `behind`：`{"behind_sec": X, "cursor_sec": Y}` |

`asr_round` WS 帧**加两个字段**（`chunk_start_sec` / `chunk_end_sec`），
现有前端逻辑（`committed` / `unfixed` 渲染）**不用改**。

### 8.11 前端增量

| 元素 | 说明 |
|---|---|
| **四个来源页签** | 音频文件 / 含音轨的视频 / 板载麦克风 / 浏览器麦克风（**互斥**，见 §8.11.1） |
| 播放器 | `<audio>` / `<video>` + 字幕叠加层 |
| 字幕层 | 按 `chunk_start_sec/end_sec` 定位；已确认（committed）与未确认（unfixed）**分色**（沿用现有蓝色尾巴约定） |
| 进度指示 | 显示「转写游标 vs 播放头」双进度条（`behind` 时高亮） |
| 拖动（seek） | 拖动 → `POST /live/seek` → 用返回的缓存立即渲染 → 后台补算 |
| 两种显示模式 | 复用现有「实时结果 / 识别过程」，**不新增** |

⚠️ 仍受 AGENTS.md 约束：**不动 `framework.js` 的 `_loadedApps` / `_wizardMounted`**。

#### 8.11.1 ✅ 实施结果：四个来源页签（互斥）

| 来源 | 控件 | 说明 |
|---|---|---|
| `file` | 上传 + 设备端路径 + 浏览… | `accept="audio/*,…"` |
| `video` | **与 file 共用同一套控件** | 只换 `accept="video/*,…"` + 一句提示语 |
| `board` | 设备下拉 + 开始采集 / 停止 + 状态 + 日志路径 | 设备端 ALSA（§8.11.2） |
| `browser` | 开始实时识别 / 中止 + 电平表 + 状态 | `getUserMedia` |

两条不变式：

* **页签互斥** —— 切到谁就只显示谁的面板；实时区块的显隐 =
  **feature flag ∧ 当前来源是实时来源**；
* **文件 / 视频不重置实时状态** —— 切页签只是换控件，不碰后端会话。

> ⚠️ **「浏览器麦克风」来源有个前提**：浏览器只在**安全上下文**
> （https 或 localhost）里暴露 `navigator.mediaDevices`。用局域网 IP 走
> 明文 `http://` 时它是 `undefined` —— 点「开始实时识别」**静默失败**。
>
> 解法是给服务加 HTTPS（`scripts/tls_relay.py` 在 8443 端口终结 TLS，
> 8000 一行不改），客户端信任步骤见
> **[docs/TLS_CLIENT_TRUST.md](TLS_CLIENT_TRUST.md)**。
>
> 前端也已经**主动提示**：非安全上下文时浏览器麦面板直接写明
> 「当前地址不是安全上下文…请改用 https://…:8443 访问，或改用板载麦克风」
> （回归用例 U9 / U10）。

#### 8.11.2 板载采集：独立进程 + ⚠️ 停止路径的死锁

板载采集跑在**独立进程**（`tools/asr_mic_agent.py`）里 —— `snd_pcm_readi`
是**阻塞 I/O**，放进 webui 会占死线程、拖垮整个 Web 服务；声卡还是
**独占**的。agent 自己开实时会话、自己推流、自己收尾，webui 只管
起停与状态（`app/apps/qwen3_asr/board_mic.py::BoardMicController`）。

> ⚠️ **踩过的坑：停止路径把事件循环堵死 → 互等死锁**
>
> ```
> webui: ctl.stop() 等 agent 退出 ─┐
> agent: /live/finish 等 webui 处理 ─┘  → agent 被 SIGKILL，
> ```
>
> * **症状**：stop 端点耗时 **5055 ms**（= grace 秒数）、agent 日志停在
>   「收到信号 15，准备收尾」、webui 日志在这 5 秒里**一条请求都没有**、
>   agent 退出码 **-9**（SIGKILL）、live 会话**残留**到 600 s TTL。
> * **根因**：`ctl.stop()` 是**同步阻塞**的（等 agent 退出），却写在
>   `async def` 端点里 → **把事件循环堵死**。而 agent 收到 SIGTERM 后
>   要 POST `/live/finish` 收尾，那个请求正好排在被堵死的事件循环后面
>   → **互相等** → webui 等满 grace → SIGKILL → `/live/finish` 永远丢。
> * **修法**（两道）：
>   1. `stop` 丢进线程跑（`await asyncio.to_thread(ctl.stop)`）——
>      事件循环保持响应，`/live/finish` 才处理得掉；
>   2. **安全网**：进程退出后**主动收掉 `source == "board"` 的 live 会话**
>      （`_close_board_live_sessions()`）—— 万一 agent 被 SIGKILL
>      （没走完 finish），会话也不会占着 SDK 到 600 s TTL。
>
> **实测**：修复后 stop 端点 **745 ms**、退出码 **0**、无残留会话；
> 故意 SIGKILL agent 后再 stop → `closed_sessions: 1`，会话被收干净。

> **通用教训**：**任何「同步阻塞」的等待都不能写在 `async def` 里**。
> 事件循环一堵，**所有**在途请求（包括子进程回调自己的收尾请求）都会
> 被排队卡死 —— 死锁的另一半往往在**另一个进程**里。

### 8.12 阶段划分调整

| 阶段 | 内容 | 变化 |
|---|---|---|
| **阶段一（SDK）** | 流式会话 + **时间戳字段** | ➕ 加 `asr_chunk_start_sec` option、加 `chunk_start_sec` / `chunk_samples` 载荷字段 |
| **阶段二（麦克风）** | 采集端 + 背压 + VAD | 不变 |
| **🆕 阶段二-b（文件/视频）** | 流式解码 + timeline 游标 + 缓存 + seek + 字幕 | 新增 |

**阶段二-b 的依赖**：阶段一必须先完成（时间戳与流式会话是共同底座）。
但阶段二-b 与阶段二**可并行开发**（不同文件，不同人/不同轮次）。

### 8.13 新增验收项

| # | 验收项 | 判定 |
|---|---|---|
| D1 | 时间戳连续性 | 连续 100 块，`chunk_start_sec` **严格递增**且步长 == 1.0 s（±1 样本） |
| D2 | 最后一块 | 末块 `chunk_samples < 16000` 时，`chunk_end_sec` 正确反映实际长度 |
| D3 | **文件实时 vs 一次性转写等价** | 同一音频文件，实时模式逐块累积的 committed 文本 ≡ 现有 `/transcribe` 一次性转写的文本（**逐字节相同**） |
| D4 | **视频 ≡ 音频** | 同一段音频，包在视频里 vs 纯音频文件 → 文本**逐字节相同** |
| D5 | seek | 拖到任意位置 → 立即出结果（缓存命中）→ 后续内容继续正确推进 |
| D6 | seek 幂等 | 同一位置 seek 两次 → 结果**逐字节相同** |
| D7 | **文件模式不丢内容** | 长文件转写中，`dropped_chunks` **恒为 0**；`behind_sec` 有值但内容完整 |
| D8 | 字幕对齐 | 字幕不早于画面（`chunk_end_sec > currentTime` 的不显示） |
| D9 | 视频不早于画面 | 同上 |
| D10 | 长文件分段 | > 900 s 的文件被切成 ≥2 段，UI 标注分段，段间无异常 |
| D11 | SRT 导出 | 从缓存导出的 SRT 时间轴正确、可被播放器加载 |
| D12 | 回归 | 现有 `/transcribe` 路径（文件模式）行为**完全不变**；32/32 仍全绿 |

---

## 9. 验收标准

### 9.1 阶段一（SDK）

| # | 验收项 | 判定 |
|---|---|---|
| A1 | 增量 16 块 vs 文件 16 块 | **committed 文本逐字节相同** |
| A2 | 不传 `asr_stream_id` | 与改造前 golden 输出逐字节相同（**零回归**） |
| A3 | `final=True` 后 | SDK 侧 `_online_streams` 中该 id 消失 |
| A4 | 同 id 再次提交 | ≡ 新建会话，不抛异常 |
| A5 | 空闲 > `asr_stream_ttl_sec` | state 被回收（内存不泄漏） |
| A6 | 连续 100 块 | 不崩，`chunk_idx == 100` |
| A7 | `pytest .venv/src/wdnai/tests/` | 全绿（含新 `test_asr_stream.py`） |
| A8 | 与 C demo 比对 | 用同一段 `asr_en.wav` 走增量路径，最终文本与 C demo **逐字节相同** |

### 9.2 阶段二（webui）

| # | 验收项 | 判定 |
|---|---|---|
| B1 | 浏览器麦克风 | 说话时 **≤ 1.7 s** 内出现首个已确认片段（§6 时延预算） |
| B2 | 板载 ALSA | 同场景可识别；设备选择可配（`hw:1,0` / `hw:0,0`） |
| B3 | **跨端一致** | 同一段录音，浏览器采集与板载采集的最终文本**逐字节相同** |
| B4 | 背压 | 人为拖慢推理（长音频）→ 客户端不崩、延迟有界、`dropped_chunks` 计数正确、WS 推 `gap` 帧 |
| B5 | 丢包熔断 | 连续丢 5 块 → 会话自动结束并报错，不静默产出错误文本 |
| B6 | 端点检测 | 静音 1.2 s 后自动收尾 → WS 推 `endpoint` 帧 → 自动开新会话 |
| B7 | 纯静音 | 30 s 纯静音 → 强制结束，不产生 `endpoint` 帧 |
| B8 | 会话上限 | 300 s → 强制重置，不 OOM |
| B9 | DemoGuard | 离开页面 / 刷新 / 切卡 → 后端实时流**被关闭**（`DELETE /live/stop` 生效） |
| B10 | 端点续传 | 刷新页面后 `GET /rounds` 能恢复逐轮明细（与现有行为一致） |
| B11 | feature flag | `asr.live.enabled=false` 时新端点返回 404，**旧路径完全不受影响** |
| B12 | 回归 | `scripts/verify_asr_wizard.py` 现有 **32/32 仍全绿** + 新增实时用例通过 |
| B13 | 回归 | `python -m scripts.verify_capability_groups`、`verify_model_packaging` 全绿 |
| B14 | 板卡健康 | 实时会话期间 `pcie_rkep` refcnt 仍为 **2**、无常驻 D 状态线程 |
| B15 | 文件 / 视频模式 | 见 §8.13 的 **D1~D12**（时间戳 / seek / 字幕对齐 / 视频≡音频 / 不丢内容） |

#### 9.2.1 阶段二实施后的**验收落点**（谁验的、怎么验的）

| # | 落点 | 说明 |
|---|---|---|
| B1 | `verify_asr_wizard.py` **V5** | 浏览器假麦克风；从「第一块音频发出去」到首个片段的延迟（**不含** getUserMedia / AudioContext / worklet 下载的启动开销） |
| B2 | `verify_asr_wizard.py` **V4** + `tools/asr_mic_agent.py --device` | 板载路径（`--file` 走同一套上行接口；`--device hw:1,0` / `hw:0,0` 可选） |
| B3 | `verify_asr_wizard.py` **V6a / V6b / V6c** | 拆成三条（见 §5.2.3）：**V6a** 两端重采样**逐字节**相同（算法一致）；**V6b** 同一段 PCM 经两条链路 → 文本**逐字节**相同；**V6c** 两端确实是同一段录音（包络 + 逐段细结构 + 偏移不跑飞） |
| B4 | `verify_asr_live.py` **L3 / L4** + `verify_asr_wizard.py` **V7** | 服务端有界缓冲 / 客户端丢块 → `gap` 帧 + 计数 |
| B5 | `verify_asr_live.py` **L5** + `verify_asr_wizard.py` **V8** | 连续丢 5 块 → 409 + `error` 帧 |
| B6 | `verify_asr_live.py` **L6** + `verify_asr_wizard.py` **V10** | 端点 → `endpoint` 帧 + 自动开新 `asr_stream_id`（**服务端计数与前端收帧都要查** —— 状态行会被下一条 chunk 的响应立刻覆盖） |
| B7 | `verify_asr_live.py` **L7** | 30 s 纯静音 → 强制结束、**不**产 `endpoint` 帧 |
| B8 | `verify_asr_live.py` **L8** | 300 s 上限 → 强制重置（测试里把上限调成 2 s） |
| B9 | `verify_asr_wizard.py` **V9** | 切走卡片 → 后端 `live.active` 变 false（**要在会话还在跑的时候查快照** —— 会话可能已被 30 s 纯静音规则收掉） |
| B10 | `verify_asr_wizard.py` 存量 **W6** | 文件路径的 `GET /rounds` 恢复（实时路径的逐轮明细在 WS 帧里） |
| B11 | `verify_asr_wizard.py` **V1** | flag 关 → 404 |
| B12 | `verify_asr_wizard.py` | 存量 32 条 + 新增实时用例 |
| B13 | `scripts/verify_capability_groups.py`、`scripts/verify_model_packaging.py` | — |
| B14 | `lsmod \| grep pcie_rkep` | 实时会话前后 refcnt 不变 |
| **U1~U8** | `verify_asr_wizard.py::verify_sources` | **四种输入源 + 不再 60 s 截断 + 板载停止不死锁**（见 §9.2.1.2） |
| **U9** | `verify_asr_long_audio.py` | **长音频不中途卡死 + 末块不崩**：用例 A（静音之后的语音仍被识别 / 正文无 `language None` / 无重复退化 / `epochs ≥ 2`）+ 用例 B（极短末块 → 状态 `done`、无 `could not broadcast`）（见 §8.8.3 / §8.8.4） |
| **U10** | `verify_asr_live.py::t_l12_stall_detector` | **停滞检测状态机**（确定性，不需要 NPU / 服务 / 模型行为）：连续 8 轮文本不变 → 计数达标；文本增长或变短 → 归零（见 §8.8.3） |

> **B7 / B8 不放在浏览器 e2e 里**：一个要跑 30 秒、一个要跑 300 秒。
> 它们由**契约档**（`verify_asr_live.py`，假 SDK session，秒级）覆盖 ——
> 契约不依赖真实时钟，也不依赖 NPU。

> **V9 要单独开一个页面**：切走卡片会**停掉麦克风**，所以「等端点」
> （V10）与「离开页面」（V9）不能共用同一个会话 —— V9 用**第二个页面**
> 单独跑一遍。

#### 9.2.1.1 实时用例一览（`verify_asr_wizard.py::verify_live`）

| # | 断言 |
|---|---|
| V1 | flag 关 → live 端点 404 |
| V2 | flag 开 → `/live/start` 返回 stream_id + 端点参数 |
| V3 | 文件路径（16 kHz）→ 参考文本非空 |
| V4 | 板载路径（48 kHz 文件 → 采集端降采样）→ 文本 ≡ 参考文本 ≡ C++ demo 文本 |
| V5 | 浏览器路径 → 出文本；「第一块发出 → 首个片段」≤ 1.7 s |
| V5c | 浏览器文本 vs 参考文本相似度 ≥ 0.75（实测**逐字节相同**） |
| V6a | 浏览器重采样 ≡ 板载重采样（**逐字节**） |
| V6b | 同一段 PCM：板载路径文本 ≡ 浏览器路径文本（**逐字节**） |
| V6c | 包络相关 ≥ 0.95 + 逐段细结构相关中位数 ≥ 0.999 + 偏移极差 ≤ 2000 采样 |
| V7 | seq 缺号 → 服务端记丢块 |
| V8 | 连续丢 5 块 → 熔断 409 + 明确报错 |
| V9 | 会话在跑时快照能看到 live；切走卡片 → `live.active` 变 false |
| V10 | 服务端 `endpoint_index ≥ 1` **且**前端收到 `endpoint` 帧 |

#### 9.2.1.2 四种输入源用例一览（`verify_asr_wizard.py::verify_sources`）

| # | 断言 |
|---|---|
| U1 | 四个来源页签齐全（文件 / 视频 / 板载麦 / 浏览器麦），默认选中「音频文件」 |
| U2 | 切到视频 → `accept` 收视频类型、提示语说「视频」；切回文件 → `accept` 收音频类型 |
| U3 | 四个面板**互斥**（切到谁只显示谁）；浏览器面板有开始/中止 + 电平表 |
| U4 | 板载面板列出了采集设备（`/live/board/devices` 通了）+ 开始/停止按钮 |
| U5 | 页面**不再**出现「60 秒 / 最长处理」字样 |
| U6 | `/transcribe` 的响应里**没有** `truncated` 字段 |
| U7 | **> 60 s 的音频不被截断**（`audio_sec > 60`）且识别出了文本 |
| U8 | 板载 stop **不堵死事件循环**（远快于 grace）+ agent 优雅退出（退出码 0，不是 -9）+ **无残留**板载实时会话 |
| U9 | **非安全上下文**（http:// + 局域网 IP）时浏览器麦面板给出提示条并写明 https 地址；**安全上下文**下不显示提示条 |
| U10 | 非安全上下文点「开始实时识别」给出**可读**错误（不是被「已停止」盖掉的静默失败） |
| U11 | 视频来源下**播放器可见**，切回音频文件后隐藏 |
| U12 | 设备端视频**真的能解码出画面**（`videoWidth > 0`）+ `src` 指向媒体端点 |
| U13 | 媒体端点**拒绝**白名单外的路径与路径逃逸（403 / 400）—— 无鉴权服务的底线 |
| U14 | 拖播放头 → **字幕跟着变长**（真同步，不是把全文糊在画面上）；后段字幕包含前段 |

> **U9 / U10 是分支判据** —— 同一份脚本跑两遍就覆盖两条：
> `WDNWEBUI_BASE=http://<板子IP>:8000`（非安全）与
> `WDNWEBUI_BASE=https://<板子IP>:8443`（安全）。
>
> ⚠️ **非安全上下文时 V5 / V6a / V6b / V6c / V5c 自动跳过**（不是失败）——
> 它们测的是「浏览器采到的音频」，而浏览器在非安全上下文里**根本不提供**
> `navigator.mediaDevices`。功能没坏，是这个访问方式用不了浏览器麦。

> **U8 是回归护栏**：旧实现把「等 agent 退出」的同步等待写在
> `async def` 端点里 → 事件循环被堵 5 s → agent 的 `/live/finish`
> 处理不了 → SIGKILL + 会话残留（详见 §8.11.2）。

#### 9.2.2 阶段二实施时**顺带修掉**的既有缺陷

**「刚重启服务时，向导走完却什么都没跑」**（与实时链路无关，是被
服务重启暴露出来的）。

* **症状**：向导第 3 步「完成配置」点了没反应，等 20 s 后向导报
  「模型还没就绪」；`/api/qwen3-asr/transcribe` **一次都没发出去**。
* **根因**：向导的「能力就绪」判据是 ``snapshot.model_id``
  （`static/app.js` 的 ``isReady: (s) => !!(s && s.model_id)``），而
  ``model_id`` 只在 ``_AsrManager._select_session()`` 里被填 —— 那要等
  **一次识别真的跑起来**。于是形成死结：没识别过 → 没有 model_id →
  向导等不到就绪 → 识别永远起不来。
  上线后的第一台设备、以及**每次重启服务之后**，都会撞上这个死结。
* **修法**：`snapshot()` 里的 ``model_id`` 改成「**当前谁在提供
  ``swcap_audio_asr`` 能力**」（从 registry 查），而不是「上一次识别
  用了哪个模型」。语义也更对 —— 前端那个「模型未启用」横幅要表达的是
  「能力不可用」，不是「还没识别过」。
* **附带**：为了不让「每 5 秒轮询一次快照」把整个 registry 拉起来，
  registry 模块新增了 :func:`peek_registry`（**不**触发初始化）。

#### 9.2.3 阶段二**实施记录**（2026-11 落地）

**落地形态**

| 位置 | 内容 |
|---|---|
| `app/core/audio/capture.py` | 采集契约 + `ChunkAccumulator` + `PcmFeedBuffer` + `StreamResampler` |
| `app/core/audio/endpoint.py` | 能量门限端点检测（纯 numpy，无 `webrtcvad`） |
| `app/core/audio/alsa.py` | `ctypes` → `libasound.so.2`（零 pip 依赖） |
| `app/apps/qwen3_asr/service.py` | `_LiveAsrSession` / `_LiveAsrManager`；模块级 `_ASR_RPC_LOCK` 把文件路径与实时路径**串行化** |
| `app/apps/qwen3_asr/api.py` | 4 个 live 端点（`/live/start` `/live/chunk` `/live/finish` `/live/stop`），feature flag 门控 |
| `app/apps/qwen3_asr/static/app.js` | 浏览器采集（原生采样率 + 自研线性插值重采样）+ L1/L2 背压 + WS 渲染 |
| `app/apps/qwen3_asr/static/capture-worklet.js` | `AudioWorkletProcessor`（**必须**独立文件） |
| `tools/asr_mic_agent.py` | 板载采集 CLI（`--device` / `--file` / `--pcm` / `--list-devices`） |
| `scripts/verify_asr_live.py` | 契约级回归 **41/41**（假 SDK session，不需要 NPU / 服务）—— L1~L12 |
| `scripts/verify_asr_wizard.py` | 新增实时用例 **V1~V10**（含 V6a/V6b/V6c） |
| `scripts/verify_asr_long_audio.py` | **新增** —— 长音频回归：用例 A（自造「静音 + 语音」→ 自锁）+ 用例 B（极短末块 → worker 崩），需要服务在跑（见 §8.8.3 / §8.8.4） |
| `scripts/make_asr_ref_video.py` | **新增** —— 生成 `/tmp/asr_ref/asr_real_audio.mp4`（**原始 `asr_en.wav`** → AAC 音轨 + 黑帧）。W7 / U14 / **U15** 用例的素材；`/tmp` 是 tmpfs，**板子重启就没了** → 素材丢了只会让用例**静默 SKIP**（板子重启后实测：88 通过 → 81 通过 / 3 失败） |

**回归结果（本板实测）**

| 脚本 | 结果 |
|---|---|
| `scripts/verify_asr_wizard.py` | **92 通过 / 0 失败**（存量 32 + 实时 26 + 四源 30 + **U15 3**，见 §8.9.2） |
| `scripts/verify_asr_live.py` | **41 通过 / 0 失败**（L1~L12） |
| `scripts/verify_asr_long_audio.py` | **7 通过 / 0 失败**（用例 A 自锁 + 用例 B 末块越界） |
| `python -m scripts.verify_capability_groups` | ✅ 全绿 |
| `python -m scripts.verify_model_packaging` | ✅ 全绿 |
| SDK（`.venv/src/wdnai`）pytest | **153 passed, 2 skipped** |
| 板卡健康（B14） | `pcie_rkep` refcnt **3**（上一阶段遗留，基线 2）；**无常驻** D 状态线程 |

**实施中撞到的坑（都已写进代码注释）**

1. **「重采样交给浏览器」是个陷阱** —— `AudioContext({sampleRate:16000})`
   会让 **WebAudio 自己的**重采样器介入，与板载的线性插值不是同一个
   算法 → 跨端文本分叉。改成「原生采样率 + 前端自己做线性插值」后，
   两端输出**逐字节相同**（V6a）。
2. **量化必须向零截断** —— numpy `astype("<i2")` 是截断，JS 侧原本用
   `Math.round` → 差 1 LSB 就破坏「逐字节相同」。
3. **Python 的 `round()` 是银行家舍入** —— `Math.round` 是「半数向上」，
   极少数采样上差 1 → JS 侧补了 `_pyRoundToEven` / `_pyFloorDiv`。
4. **收尾必须带末块** —— 服务端用 `PcmFeedBuffer` **留住最后 1 个采样**
   （2 字节，62.5 µs），收尾时补上 → 与文件路径**逐字节等价**。
5. **丢块记账在入队时** —— `_classify_seq()` 在 `submit()` 里推进
   `_expected_seq`，避免与服务端缓冲丢最旧**重复计数**。
6. **模型在纯静音上会吐 `language None`** → `_valid_language()` 过滤 +
   `_session_had_speech` 守卫（否则纯静音会话会往文本里塞
   「…for other people. language None」）。
7. **前端收尾要先 flush 重采样器 + 等队列排空** —— 否则最后 1~2 个采样
   和最后半秒音频会丢（文本末尾可能少词）。
8. **状态行会被下一条 chunk 的响应立刻覆盖** → 端点检测的判据必须抓
   **WS 帧**或**服务端计数**，不能最后再读一次状态行。
9. **「会话在跑时」要趁早查快照** —— 纯静音 30 s 规则会把会话收掉，
   查晚了就看不到 `live.active`。
10. **V9 要单独开一个页面** —— 切走卡片会停麦克风，与「等端点」冲突。

#### 9.2.3.1 阶段二-b 扩展实施记录（2026-11，四源 UI + 去 60 s 截断）

**落地形态**

| 位置 | 内容 |
|---|---|
| `app/core/audio/decode.py` | `iter_media_pcm()` —— **流式解码**（不落全量、不截断）；`decode_media()`（全量）保留不动 |
| `app/apps/qwen3_asr/service.py` | `transcribe_stream()` / `_stream_main()` —— 增量会话；`_FILE_SESSION_MAX_SEC = 900` |
| `app/apps/qwen3_asr/board_mic.py` | `BoardMicController` —— 板载采集子进程（spawn / stop / status / shutdown） |
| `app/apps/qwen3_asr/api.py` | `/live/board/{devices,start,stop,status}` 四个端点；`/live/finish` 收尾后收 worker 协程 |
| `app/apps/qwen3_asr/static/template.html` / `app.js` / `app.css` | 四个来源页签 + 板载面板；去掉「60 秒」文案与 `truncated` 分支 |
| `app/apps/qwen3_asr/manifest.json` | `demo_guard` 增加 `POST /api/qwen3-asr/live/board/stop` |
| `tools/asr_mic_agent.py` | 板载采集 CLI（收尾请求超时收紧到 15 s） |

**回归结果（本板实测）**

| 脚本 | 结果 |
|---|---|
| `scripts/verify_asr_wizard.py` | **88 通过 / 0 失败**（存量 W 31 + 实时 V 25 + 四源/视频新增 U 28 + 杂项 4） |
| `scripts/verify_asr_wizard.py`（`WDNWEBUI_BASE=https://<板子IP>:8443`） | **88 通过 / 0 失败**（同上一行，**走 TLS**：`wss://` 也在内） |
| `scripts/verify_asr_wizard.py`（`WDNWEBUI_BASE=http://<板子IP>:8000`） | **72 通过 / 0 失败**（非安全上下文 → 浏览器麦用例按设计**跳过**，见 U9 / U10） |
| `python -m scripts.verify_tls_scheme` | ✅ 8/8（scheme 传递 + 请求体没被搞坏） |
| `scripts/verify_asr_live.py` | **38 通过 / 0 失败** |
| `python -m scripts.verify_capability_groups` | ✅ 全绿 |
| `python -m scripts.verify_model_packaging` | ✅ 全绿 |
| SDK（`.venv/src/wdnai`）pytest | **153 passed, 2 skipped** |
| 板卡健康（B14） | `pcie_rkep` refcnt **3**（上一阶段遗留，基线 2）；**无常驻** D 状态线程 |

> ⚠️ **V6c 是「会抖」的那一条** —— 它测的是**测试环境**（Chromium 假麦克风
> 采到的音频 vs 板载喂的音频是不是同一段录音），不是产品代码。
>
> * **机制**：Chromium 假设备是**一次性播放**（`%noloop`），且实测在负载高
>   时会**偶发丢 10 ms buffer**。采集启动若赶上文件已放完，首段就是静音 →
>   `_best_correlation` 的相关性搜索落在噪声上 → `argmax` 给出 0 偏移 →
>   分段细结构相关全部失真。
> * **实测**：某轮 HTTPS 回归里 V6c 中位数掉到 0.452932（**整体偏移 0 采样**，
>   而真实固定延迟约 320 采样）；**同一轮**里确定性的 V6a（重采样逐字节）、
>   V6b（文本逐字节相同）、V5c（文本相似度 1.000）**全部通过** →
>   产品链路没问题，是**测量**这一步失效。原样重跑即恢复
>   （偏移 320、包络相关 1.000000、中位数 0.999996）。
> * **判定口诀**：V6c 失败时先看 **V6a / V6b / V5c** —— 它们过了就说明是
>   环境抖动，重跑即可；它们没过才是真问题。

**本轮撞到的坑**

11. **「同步阻塞」写进 `async def` = 死锁** —— 板载 stop 的等待堵死事件循环，
    agent 的 `/live/finish` 排在后面 → 互等 → SIGKILL + 会话残留。
    详见 §8.11.2。**任何同步等待都不能写在事件循环线程里。**
12. **别在主进程侧重采样** —— SDK 的增量路径自己负责重采样与切块；
    主进程再插一脚会**打断 24-bit WAV 支持**（`np.frombuffer` 奇数长度）。
    详见 §8.8.2。
13. **`/live/finish` 之后要收 worker 协程** —— 否则会话被 drop 了、协程还挂着
    → GC 时抛 "Task was destroyed but it is pending"。TTL 回收同理。
14. **UI 文案里的「60 秒」不是装饰** —— 它照抄的是 C demo 的
    `ONLINE_MAX_AUDIO_SECONDS`。去掉限制之前先问「这条限制是谁定的、
    为什么」—— 答案决定了该**放宽上限**还是该**换实现方式**（这里是换）。
15. ⚠️ **外部改 config.jsonc 会被服务端的内存副本吃掉** —— 算力配置的
    自动归一化跑在**模型重扫循环**里（每 ~2 秒一次），走
    `ConfigStore.set_section()` → `_persist()` 把**内存里的整份配置**写回
    磁盘。内存副本还是开机时读的 → 运维热切的 feature flag
    （`asr.live.enabled`）**几秒内就被悄悄改回去**。
    * **症状**：回归里「flag 关掉 → live 端点 404」这条**间歇性失败**
      （实测 `st=200`）；直接改文件后 8 秒内被写回 `true`。
    * **修法**：`set_section()` 落盘前先**重读磁盘**做基底
      （`ConfigStore._merge_disk()`）→ 外部改动被保住。
    * **通用教训**：**「读-改-写」整个文件时，写之前要重新读一遍** ——
      尤其当**另一个进程/线程也在周期性地写同一个文件**时。

### 9.3 文档与可观测性

| # | 验收项 |
|---|---|
| C1 | `docs/` 更新：实时链路说明 + 与官方 vLLM 的差异表 |
| C2 | 服务日志能定位一次实时会话的完整生命周期（start → N 块 → finish/stop） |
| C3 | 端点、丢块、熔断三类事件都有日志 |

---

## 10. 风险与回滚

| 风险 | 影响 | 缓解 |
|---|---|---|
| **SDK 增量路径与文件路径不一致** | 实时结果与文件结果不同 | A1/A2 双向 golden 断言；文件路径**代码不动** |
| **worker 内存泄漏**（session 未回收） | 长跑 OOM | TTL + `close()` 清理 + A5 |
| **NPU 长会话 RTF 恶化**（全量重喂） | 延迟随时间上升 | 300 s 强制重置；端点检测自然切分句子 |
| **板卡 D 状态**（历史事故） | 全卡不可用 | 会话结束**必须**释放 RKNN context；会话上限 300 s 强制换 context；跑 SDK sample 前先停 webui 的 ASR worker（AGENTS.md 明令） |
| **ALSA 设备被占** | 板载采集失败 | 采集进程独占，webui 不碰声卡；失败要清晰报错而不是静默 |
| **`/userdata` 空间紧张**（仅剩 ~3.9 G） | — | 实时路径**不落盘**，PCM 只在内存 |
| **改动 `wdnai` 未提交** | 丢失 | 按既有约定**不提交**；回滚 = `git -C .venv/src/wdnai checkout src/wdnai/backends/asr.py` |

### 10.1 文件 / 视频模式追加的风险（§8）

| 风险 | 影响 | 缓解 |
|---|---|---|
| **背压逻辑共用** | 文件模式误用「丢弃」策略 → **永久丢内容** | 两种背压**分开实现**；D7 断言文件模式 `dropped_chunks` 恒为 0 |
| **seek 后上下文错位** | 跳过位置的音频残留在 `audio_embed_history` 里 → 识别混乱 | seek **必须重建 SDK 会话**（新 `asr_stream_id`） |
| **长文件撑爆内存** | OOM | 流式解码（`iter_media_pcm`）不落全量；会话上限 900 s 切段 |
| **上下文撞墙**（>20 min） | 越界或质量劣化 | 900 s 切段 + UI 标注分段；按实测 13 token/s 预留余量 |
| **字幕超前于画面** | 用户体验割裂 | 显示条件 `chunk_end_sec <= currentTime + lead`；D8/D9 断言 |
| **时间戳字段破坏兼容** | 老 worker / 新主进程组合报错 | 加法式扩展 + `payload.get()` 默认值；任意新老组合都能跑 |
| **视频音轨与画面起点偏移** | 字幕整体偏移几帧～几秒 | 时间轴原点取**音频流在容器内的 `start_time`**（`decode.py` 现只取 `rate`，未取 `start_time`，需补）；前端播放头的 `currentTime` 需减去同一原点。实测本板 `preview.mp4` 两者均为 0，无偏移 |

### 回滚开关

`config.jsonc` → `asr.live.enabled`（默认 `false`）：

- `false` → live 端点返回 **404**，前端隐藏实时区块，**所有现有功能零影响**。
- 这是**第一道**回滚闸门；SDK 侧的改动即使不合预期，也不会影响文件路径。

---

## 11. 任务分解与依赖顺序

```
阶段一（SDK，可独立完成并验证）
  T-1  asr.py：_OnlineStreamState + _online_streams + _run_online_stream
  T-2  asr.py：run_audio 分叉 + TTL 回收 + close() 清理
  T-3  model.py：asr_stream_ttl_sec 默认值
  T-4  tests/test_asr_stream.py：契约档（T2/T3/T4/T5/T6/T7/T8）
  T-5  examples/Qwen3_ASR/streaming_session.py：可跑样例
  T-6  实机档测试 + A1/A8 与 C demo 比对
        ▼  （A1/A8 不过则不进入阶段二）

阶段一-b（SDK 时间戳扩展，文件/视频的共同底座）
  T-6b asr.py：options 加 asr_chunk_start_sec；_round_payload 加
               chunk_start_sec / chunk_samples（**纯加法**）
  T-6c tests：D1/D2 时间戳连续性断言
        ▼

阶段二（webui —— 麦克风）✅ **已实施**
  T-7  core/audio/capture.py：契约 + ChunkAccumulator + PcmFeedBuffer
       （+ StreamResampler：板载 48k→16k 的流式重采样）
  T-8  core/audio/endpoint.py：能量门限端点检测（action_on_endpoint 开关）
  T-9  service.py：_LiveAsrSession（串行提交 + 背压 + 端点联动）
  T-10 api.py：4 个 live 端点（feature flag 门控）
  T-11 static/app.js：浏览器采集 + 上行 + 渲染
  T-12 static/template.html + app.css：实时区块 UI
  T-13 core/audio/alsa.py：ctypes ALSA 采集
  T-14 tools/asr_mic_agent.py：板载采集进程
  T-15 config feature flag + manifest 文案
  T-16 scripts/verify_asr_wizard.py：新增实时用例（B1~B12）
        （V1~V10，其中 B3 拆成 V6a/V6b/V6c —— 见 §5.2.3）
  T-16b static/capture-worklet.js：AudioWorklet processor（**必须**是独立文件，
        见 §5.2.1）
  T-16c scripts/verify_asr_live.py：实时链路的**契约级**回归（假 SDK session，
        不需要 NPU / 不需要服务在跑）
  T-16d scripts/make_asr_live_wav.py：实时链路的测试音频生成器
        （**两个**采样率：48 kHz + 16 kHz，同源 —— 见 §5.2.2）
  T-16e app.js：前端重采样与 Python 的 StreamResampler **逐样本等价**
        （V6a 的可执行判据：window.__asrLiveResample）
        ▼

阶段二-b（webui —— 文件 / 视频；与阶段二可并行）
  T-19 core/audio/stream.py：ChunkScheduler + Append/Timeline 两种游标
  T-20 core/audio/decode.py：iter_media_pcm() 流式解码（decode_media 保留不动）
  T-21 service.py：timeline 模式 + chunk LRU 缓存 + seek 重建会话
  T-22 api.py：/live/seek + /live/chunks；/live/chunk 响应加时间戳字段
  T-23 static/app.js + template.html：播放器 + 字幕层 + 双进度条 + seek 交互
  T-24 文件模式的背压（behind 帧）与 VAD（只观察不动作）分流
  T-25 scripts/verify_asr_wizard.py：文件/视频用例（D1~D12）
        ▼
阶段三（收口）
  T-17 文档更新 + 日志与可观测性（C1~C3）
  T-18 全量回归（32/32 + 两个 verify 脚本 + 板卡健康 B14）
```

**关键路径**：`T-1 → T-2 → T-4 → T-6`（阶段一的成败全在 T-6 的逐字节比对）。
浏览器实现（T-11）与板载实现（T-13/T-14）**可并行**，但都要等 T-9/T-10 就位。

**阶段二-b 的关键路径**：`T-6b → T-6c → T-19 → T-21 → T-25`。
T-20（流式解码）与 T-23（前端播放器）可与 T-21 并行。
**阶段二-b 必须等阶段一的时间戳扩展（T-6b）落地**，否则字幕无法对齐。

---

## 12. 附：本清单依据的关键源码位置

| 结论 | 出处 |
|---|---|
| `overrides` 全量透传给 `run_audio` | `wdnai/src/wdnai/session.py:1018-1030` |
| wire `options` 是自由 dict | `wdndds/src/wdndds/ai_session.py:585-594` |
| worker 薄 handler（零改动可行） | `wdndds/src/wdndds/ai_session.py:1041-1094` |
| 流式状态是局部变量（必须改 SDK） | `wdnai/src/wdnai/backends/asr.py:640-642` |
| 回退策略（增量模式需复用） | `wdnai/src/wdnai/backends/asr.py:680-695`；C demo `online_main.cc:482-502` |
| 每轮 delta 载荷（schema 加法式扩展） | `wdnai/src/wdnai/backends/asr.py:775-789` `_round_payload` |
| 现有 delta 解析用 `payload.get()` 带默认 → 加字段零回归 | `app/apps/qwen3_asr/service.py:249-254` |
| 视频取第一条音轨（与音频文件同构） | `app/core/audio/decode.py:137-148` |
| 现有全量解码（保留不动，新增流式版） | `app/core/audio/decode.py:123-188` `_decode_with_pyav` |
| 现有 60 s 截断（文件模式不沿用） | `app/core/audio/decode.py:49`、`:158-160`、`:172` |
| 上下文占用实测（13 token/s） | 上一轮报告 §「上下文占用」表 |
| 现有 WS 帧类型 | `app/apps/qwen3_asr/api.py:18-27` |
| 现有文件路径全量透传 | `app/apps/qwen3_asr/service.py:279-291` |
| 文件路径 60 s 截断 | `app/core/audio/decode.py:49` |
| 官方 demo 串行 `pump()` | `qwen_asr/cli/demo_streaming.py:356-378` |
| 官方 demo 的 7 个短板（背压/VAD/序号…） | `qwen_asr/cli/demo_streaming.py` 全量；见分析报告 §5.3.6 |
| 板载声卡与设备节点 | `/proc/asound/cards`、`arecord -l`、`/dev/snd`（已核实） |
| ALSA 用户态库存在 | `/usr/lib/aarch64-linux-gnu/libasound.so.2`（已核实） |
| `sounddevice`/`pyaudio`/`webrtcvad` 缺失 | `.venv` 实测（已核实） |

---

*清单结束 · 本轮只出规格，未实施任何代码改动，未提交 git*
