# 流式语音识别卡「同声传译」—— 实施方案（v1）

> 状态：**待审核**（先出方案，不实施）
> 配套：`docs/SIMULTANEOUS_INTERPRETATION_PLAN.md` = **为什么**（市面对比 / 硬件实测 / 产能推算）；本文 = **怎么做**（文件级改动、接口契约、实施顺序）。
> **部署形态（已定，本文的硬前提）**：
>
> | 卡 | 跑的模型 | 说明 |
> |---|---|---|
> | **卡 0** | `qwen3-asr` + `hy-mt-1-5` | ASR 与翻译同卡（两个 llm_mode 8 核上下文 ⇒ 时间片轮转，见 §9 风险 R1） |
> | **卡 1** | `qwen3-tts` | **单实例**（不做第二份） |
> | ~~卡 2~~ | — | **部署禁区，不碰** |
>
> ⇒ **不重新转模型**；产能上限 = **单卡 TTS 1.02×**（实测）⇒ 方案必须靠「流水线 + tempo + 允许滞后增长」兜住原则①。

---

## 0. 一页速览

### 0.1 产能账（决定了方案的形状）

| 环节 | 速率 | 来源 |
|---|---|---|
| TTS 合成（卡 1，单实例） | **1.02×** 实时 | 实测（4.19 s 合成 / 4.28 s 音频） |
| TTS 首帧 | 2.8–3.3 s | 实测 |
| TTS PCM 块 | **4 s 一块** | SDK 行为 |
| 翻译（卡 0，单实例 hy-mt-1-5） | `avg_latency_ms ≈ 263`（实测），一次性定稿 | 实测 |
| ASR `unfixed` 滞后 | ~1 s | 结构性 |
| ASR `committed` 滞后 | 4–5 s | 结构性回看窗口 |
| 板载播放 | 1.00×（可 `tempo` 到 1.15） | sox 14.4.2 `tempo`（不变调） |

**语言对膨胀比 k（译音时长 ÷ 源语时长）⇒ 两种结局**：

| 语言对 | k（估） | 单卡 1.02× 的后果 | 方案动作 |
|---|---|---|---|
| 英→中 / 日→中 / 德→中 | 0.8–1.0 | **够 ⇒ 滞后恒定** | 无需特殊处理（**产品主场景**） |
| 中→英 | 1.15–1.30 | **不够 ⇒ 滞后按 (k−1.02)×讲话时长 增长** | `tempo` 自适应 + 如实显示滞后 |
| 中→日 | ~1.4 | 不够 | 同上 |

⇒ **k 表必须先实测**（M1），否则 `tempo` 阈值是拍脑袋。

### 0.2 改动地图

| 仓库 | 文件 | 动作 | 规模 |
|---|---|---|---|
| 插件 `wdnapps_audio_chat` | `cards/tts/queue.py` | **新增**：合成队列（单实例串行 + 变速 + 滞后记账） | 新文件 ~260 行 |
| 插件 | `cards/tts/service.py` | 改：`_run_safe` 支持 `tempo` / 不落 WAV；`AplayWriter` 支持 `sox play` 变速 | ~120 行 |
| 插件 | `cards/tts/api.py` | 改：新增队列相关 4 个路由 | ~80 行 |
| 插件 | `cards/qwen3_asr/interp.py` | **新增**：同传引擎（切句 / 翻译编排 / 复核 / 指标） | 新文件 ~420 行 |
| 插件 | `cards/qwen3_asr/service.py` | 改：`_on_delta` 挂钩子 + endpoint 钩子 | ~60 行 |
| 插件 | `cards/qwen3_asr/api.py` | 改：新增同传 4 个路由 | ~70 行 |
| 插件 | `cards/qwen3_asr/static/template.html` | 改：新增「同声传译」面板 | ~70 行 |
| 插件 | `cards/qwen3_asr/static/app.js` | 改：面板交互 + WS 帧处理 + 麦缓冲放宽 | ~180 行 |
| 宿主 `wdnwebui` | `app/core/audio/tts_bridge.py` | 改：加 `submit_queued` / `queue_status` / `queue_clear`（**跨卡唯一门**） | ~60 行 |

⇒ **不动 SDK（`wdnai`）、不动 `compute_worker`、不动 TTS 卡页面、不新建卡片。**

### 0.3 四步走

| 步 | 内容 | 可独立验收 | 依赖 |
|---|---|---|---|
| **S0** | **只测不写**：M6 同卡 ASR+MT 争用 / M7 sox `play`+tempo 在板载 codec 上可用 / M8 麦上行丢块基线 / M1 k 表 | 三张数据表 | — |
| **S1** | **TTS 队列 + 变速播放**（插件 TTS 卡，自成闭环） | 队列深度、tempo 生效、播放不阻塞 worker | S0-M7 |
| **S2** | **同传引擎 + 稳档**（ASR 卡） | 端到端一句话：麦 → 译 → 板载出声 | S1 |
| **S3** | **快档 + 播前复核 + 前端面板 + 滞后指标** | 两档可切、指标可见 | S2 |

---

## 1. 现状盘点：能直接拿的 vs 必须改的

### 1.1 ✅ 可直接复用（不动）

| 件 | 位置 | 为什么能复用 |
|---|---|---|
| **实时路径的 endpoint 切句** | `cards/qwen3_asr/service.py::_LiveSession`（`AdaptivePauseDetector`，静音 1200 ms / `min_speech` 0.3 s） | **稳档要的切句点已经存在** —— endpoint 帧一广播，`_submit_translation()` 就被调用（`service.py:3093`） |
| **翻译服务**（队列 / `pick_session` / 一次性定稿 / 最多 2 条措辞） | `cards/qwen3_asr/translation.py` | `submit()` 已经是非阻塞、**绝不抛异常**、按 `gen` 作废旧句 |
| **翻译广播帧** | `type: "translation"`（含 `source` / `text` / `status` / `latency_ms`） | 同传只需要在这个帧上**多接一路**（送去 TTS） |
| **`AplayWriter`**（独立 drain 线程 ⇒ 不阻塞 SDK worker） | `cards/tts/service.py:51` | 板载出声的异步骨架现成；**只需加 tempo** |
| **TTS 的 PCM 回传通道**（UDS） | `cards/tts/service.py::_run_safe` | **每次 run 都重建 socket** ⇒ 单实例串行下**天然无交错** ⇒ **不需要**改成 per-request |
| **`play_to=both` ⇒ `no_play=True`** | `cards/tts/service.py:956` | SDK **不自己播** ⇒ 不会阻塞 worker ⇒ 板载出声必须走这条 |
| **回看不广播 / 退化时 `unfixed=""`** | `service.py::_on_delta` | 同传**免费继承**这两个不变量（不会播回看内容、不会播模型胡话） |
| **宿主跨卡门** | `app/core/audio/tts_bridge.py` | 已按「候选路径列表」解耦插件缺失 ⇒ 同传按同一模式接 |

### 1.2 ⚠️ 必须改造

| 件 | 现状 | 为什么必须改 |
|---|---|---|
| **TTS 合成入口** | `TtsSession.start()` 是**单发**语义（运行中抛 `RuntimeError("TTS 正在运行或正在清理")`） | 同传要连续合成多句 ⇒ 必须**队列化** |
| **TTS 落盘** | `_run_safe` 每次把 PCM 拼成 WAV 写盘 | 同传只播不存 ⇒ 写盘会把 eMMC 灌满（C14） |
| **TTS 播放速度** | `aplay` 定速 | 产能不足时要 `tempo` 吸收（不变调） |
| **ASR → TTS 的编排** | 无 | 需要一个引擎把「切句 → 翻译 → 合成 → 播放 → 记账」串起来 |
| **快档切句** | 只有 endpoint（静音 1200 ms） | 快档要「不等静音就成句」⇒ 需要稳定前缀切句器 |
| **播前复核** | 无 | 译文回来时源文可能已被改写 ⇒ 需要作废重译 |
| **浏览器麦缓冲** | 前端 L1 = **2 块**（丢最旧）、服务端 L3 = **3 块** | 丢音频 = 违反原则① ⇒ 放宽到 20 块并把丢块数暴露出来 |
| **滞后指标** | 无 | 原则① 的诚实兑现：产能不足时必须让用户看见滞后 |

---

## 2. 端到端数据流

### 2.1 帧序列（快档）

```
浏览器麦 ──L1 采集(20块)──▶ L2 串行上传 ──▶ 服务端 L3(20块) ──▶ ASR worker
                                                                      │
        ┌─────────────────────────────────────────────────────────────┘
        ▼  每轮 asr_round {committed, unfixed, language}
   ┌─────────────────┐
   │ 快档切句器      │  LCP(unfixed) 连续 2 轮不变 ⇒ 成句
   │ (interp.py)     │  每句记 src_text / src_ts / 段号
   └────────┬────────┘
            ▼  submit(独立小队列，不占字幕翻译队列)
   ┌─────────────────┐
   │ 翻译服务        │  一次性定稿 ⇒ 译文不再变
   └────────┬────────┘
            ▼  译文回来 → **播前复核源文**（比对 ASR 最新文本）
            ├─ 不一致 ⇒ 作废重译（内容不丢，只是版本升级）
            └─ 一致 ⇒ 提交 TTS 队列
                     ▼
              ┌──────────────────┐   PCM(4s/块, UDS)   ┌─────────────┐
              │ TTS 队列(单实例) │ ──────────────────▶ │ AplayWriter │ ─▶ 板载扬声器
              │ 深度/滞后记账    │                     │ +tempo 变速 │   (sox play)
              └──────────────────┘                     └─────────────┘
```

**稳档**只改一处：切句点从「稳定前缀」换成 **endpoint 帧**（静音 1200 ms）⇒ 其余链路完全一样。

### 2.2 时序预算（单句，中→英）

| 阶段 | 时长 | 备注 |
|---|---|---|
| 麦 → 服务端（上行） | ≤ 0.2 s | 1 s 一块，20 块缓冲 |
| ASR 出 `unfixed` | ~1.0 s | 结构性 |
| 快档成句（稳定窗口） | +1–2 s | 连续 2 轮不变 |
| 翻译（卡 0） | 0.26 s | 实测均值 |
| 播前复核 | ~0 | 纯文本比对 |
| TTS 排队等待 | 0（无积压时） | 有积压时 = 积压深度 |
| TTS 首帧 | 2.8–3.3 s | 实测 |
| **合计（用户感知的首声延迟）** | **≈ 5.3–6.8 s** | 与报告的 5–7 s 一致 |
| 稳档（等 committed + endpoint） | ≈ 9–11 s | 报告的稳档口径 |

---

## 3. 模块改动清单（文件级）

### 3.1 插件 TTS 卡 —— 队列化 + 变速播放

#### 3.1.1 新增 `cards/tts/queue.py`

**职责**：把「多个待合成句」排成**串行**队列，算积压深度，给每个 job 记滞后。

```python
#: 队列深度（段）。同传只播不存 ⇒ 内存里只有文本，很便宜。
QUEUE_MAX = 128

#: 变速上限（sox tempo）。超过 1.15 音质开始明显变紧，不往上走。
TEMPO_MAX = 1.15
TEMPO_MIN = 1.0

@dataclass
class TTSJob:
    seq: int
    text: str
    language: str = "auto"
    speaker: str = ""
    volume: float = 0.1
    tempo: float = 1.0
    play_to: str = PLAY_BOTH
    stream_id: str = ""
    src_end_ts: float = 0.0      # 源文说完的**服务端**时刻（算滞后用）
    created_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    #: 估算音频时长（秒）—— 按「字符数 ÷ 该语种语速」估，只用于算积压。
    est_audio_sec: float = 0.0
```

**关键设计**：

1. **串行消费**：一个后台线程 `while True: job = q.get(); session.run_queued(job)` ⇒ **天然按提交顺序播放** ⇒ 不需要「乱序到达再排序」那套（单实例下没有并行）。
2. **`tempo` 可动态改**：已**取走**的 job 改不了（正在合成）；**还在队列里**的可以改 ⇒
   `set_tempo(f)` 遍历未开始的 job 更新 ⇒ 积压一降就自动回落到 1.0。
3. **不丢内容**：队列满时**合并队尾两段**（把文本拼成一段重新合成），并计 `merged` 计数 ⇒ **内容一个不丢**，代价只是那句更长、延迟更大。
   （报告 §DD-4：允许滞后增长，不许丢内容。）
4. **滞后记账**：`lag_sec = job.finished_at − job.src_end_ts`（**播完**相对**源话说完**）⇒ 落进 `snapshot()` 供 UI 显示。
5. **积压估算**：`queued_audio_sec = Σ est_audio_sec(未开始)` ⇒ tempo 决策的输入。

> ⚠️ **为什么单实例下不需要「per-request PCM socket」**：`_run_safe` **每次 run 都新建 UDS socket**（`service.py:1008`）⇒ 串行消费时天然一请求一 socket ⇒ 报告 §DD-5 里那条「socket 是实例级、要改成 per-request」的改造 **P0 不需要做**（留给将来多实例）。

#### 3.1.2 改 `cards/tts/service.py`

**(a) `TTSRequest` 加两个字段**（宿主 `tts_bridge.py` 的 `TTSRequest` 也要同步加）：

| 字段 | 默认 | 作用 |
|---|---|---|
| `save_wav: bool` | `True` | `False` ⇒ 同传**不落 WAV**（C14：不把 eMMC 灌满） |
| `tempo: float` | `1.0` | 播放变速（>1.0 时生效） |

**(b) `AplayWriter` 支持变速**（`service.py:51`）：

```python
def __init__(self, ..., tempo: float = 1.0):
    if tempo > 1.001:
        # sox 的 play：一次搞定「读 raw f32 + 变速 + 送设备」
        cmd = ["play", "-q", "-t", "raw", "-f", "FLOAT_LE",
               "-r", str(sample_rate), "-c", str(channels)]
        if device: cmd += ["-D", device]
        cmd += ["tempo", "%.3f" % tempo, "-"]
    else:
        cmd = ["aplay", "-t", "raw", "-f", "FLOAT_LE", ...]   # 原路径，一字不改
```

> ⚠️ **必须保留 `aplay` 分支**：sox `play` 在板载 codec 上的行为**未实测**（M7）⇒ 默认路径零风险，只在 `tempo > 1` 时才走 sox。
> ⚠️ `play` 的 `-D` 设备名格式与 aplay 相同（ALSA 设备串，如 `plughw:1,0`）⇒ M7 要验证两件事：① 能出声；② `tempo` 生效且**不变调**。

**(c) `_run_safe` 支持「不落盘」**：`req.save_wav == False` ⇒ 跳过 `_assemble_wav` + 写盘，但**仍然**走 `_run_pcm_lock` 累积（要用来算播放时长/统计）。

**(d) 新增 `submit_queued(req, meta) -> dict`**：
- 校验 `req.text` 非空、**目标语在可发声集合内**（不在 ⇒ 抛 `RuntimeError("目标语不支持语音")`，由调用方降级为仅字幕）
- 入队（带 `tempo` 自适应值）
- 返回 `{"seq":…, "queued": depth, "est_audio_sec": …}`

**(e) 新增 `run_queued(job)`**：把 `TTSJob` 转成 `TTSRequest` 后调 `_run_safe(req)` ⇒ 完成后回调队列记 `finished_at`。

#### 3.1.3 改 `cards/tts/api.py` —— 4 个新路由

| 路由 | 方法 | 说明 |
|---|---|---|
| `/api/tts/queue/submit` | POST | body：`text` / `language` / `speaker` / `volume` / `tempo` / `src_end_ts` / `stream_id`；返回 `{seq, queued, est_audio_sec}` |
| `/api/tts/queue/status` | GET | `{depth, queued_audio_sec, playing, playing_seq, last_lag_sec, avg_lag_sec, merged, done, tempo}` |
| `/api/tts/queue/clear` | POST | 清空未开始的 job（**已在合成的跑完**）⇒ 返回清掉几段 |
| `/api/tts/queue/tempo` | POST | `{tempo}` ⇒ 改未开始 job 的变速 |

> ⚠️ 这 4 个路由**只给同传用**；TTS 卡自己的页面**不用**（它继续走 `/start` 单发）。同传是**另一个卡片**在调 ⇒ **必须经宿主 `tts_bridge`**，不能让 ASR 卡直接 import TTS 卡（AGENTS 铁律：卡片只能 import `app.core.*`）。

### 3.2 插件 ASR 卡 —— 同传引擎

#### 3.2.1 新增 `cards/qwen3_asr/interp.py`

**职责**：切句 → 翻译编排 → 播前复核 → 提交 TTS → 记账 → 广播。

```python
INTERP_STABLE_ROUNDS = 2      # 快档：LCP 连续 N 轮不变才成句（≈1–2 s）
INTERP_MAX_CHARS = 40         # 单句上限（超了强制切）
INTERP_MIN_CHARS = 8          # 低于此不切（避免碎句）
INTERP_QUEUE_MAX = 64         # 同传自己的小队列（**独立于字幕翻译队列**）
```

**① 快档切句器**（报告 DD-2 的实现）：

```python
class StablePrefixSegmenter:
    """从 unfixed 里求「最长公共前缀」，连续 N 轮不变 ⇒ 成句。"""
    def feed(self, unfixed: str) -> Optional[str]:
        # 1) 与上一轮求 LCP
        # 2) LCP 长度 > 上一轮的稳定长度 ⇒ 有新内容 ⇒ 稳定计数清零、更新候选
        # 3) LCP 连续 N 轮长度与内容都不变 ⇒ 返回该前缀作为一句
        # 4) LCP 变短（模型改写）⇒ **作废当前候选**，重新开始
```

> **为什么用 LCP 而不是「文本相同」**：`unfixed` 每轮会「往前滚」（在线回看窗口 5 个 token）⇒ 文本每轮都在变，只有**公共前缀**才代表「已经定型的那部分」。
> **为什么不用 `committed` 增量**：`committed` 只会增长、不回滚 ⇒ 它在**说话中**每轮都变 ⇒ 无法区分「在说新内容」和「停顿」⇒ 仍要靠静音/停顿判据 ⇒ 那是稳档的路子。

**② 播前复核**（报告 DD-3）：

```python
def _recheck(self, seg) -> bool:
    """译文回来时，源文还在吗？"""
    latest = self._service.current_text(stream_id)   # committed + unfixed
    return seg.src_text in latest        # 仍是最新文本的**连续一段**
```

- **一致** ⇒ 提交 TTS。
- **不一致** ⇒ **作废**，用**最新的文本**重送翻译（内容不丢，只是版本升级）⇒ 计 `retried`。
- 重试上限 **3 次**；超了 ⇒ 放弃这一段并在 UI 标注（**唯一允许的内容损失，必须计数上报**）。

> **稳档下复核几乎总是通过**（committed 不回滚）⇒ 重试主要发生在快档。

**③ 翻译编排**：
- **不复用字幕翻译队列**（那是「历史可回看」，深度 256、丢最旧）⇒ 同传要**独立的小队列**（深度 64，不丢）。
- 但**复用同一个 MT 实例**（卡 0 的 hy-mt-1-5）⇒ ⚠️ **同卡争用风险 R1**（见 §9）。
- 调用方式复用 `app.core.translate.npu_manager.translate_sentence`（与 `translation.py::_handle` 同款）。

**④ 提交 TTS**（经宿主桥）：
```python
from app.core.audio.tts_bridge import submit_queued, TTSUnavailable
submit_queued(text=译文, language=目标语, speaker=音色, volume=音量,
              tempo=当前tempo, src_end_ts=seg.src_end_ts, stream_id=…)
```
- 桥不存在 / TTS 没启用 ⇒ 抛 `TTSUnavailable` ⇒ 同传**降级为仅字幕**（UI 明说）。

**⑤ 指标**（`status()`）：
| 指标 | 含义 |
|---|---|
| `segments` | 已切句数 |
| `translated` / `retried` / `abandoned` | 翻译成功 / 重译 / 放弃（重试超限） |
| `submitted` | 已送 TTS 的句数 |
| `played` | 已播完的句数 |
| `lag_sec` | **当前滞后**（最后播完句的 `finished − src_end`） |
| `lag_avg_sec` | 近 20 句平均滞后 |
| `queue_depth` / `queued_audio_sec` | TTS 队列深度 / 积压音频秒数 |
| `tempo` | 当前变速 |
| `mode` | `off` / `fast` / `steady` |

**⑥ 广播帧**（新增 `type`）：

| 帧 | 时机 | 字段 |
|---|---|---|
| `interp_segment` | 成句 | `index`, `source`, `lang`, `mode`, `ts` |
| `interp_spoken` | 播完 | `index`, `lag_sec`, `tempo` |
| `interp_status` | 1 s 一次 | 上表全部指标 |
| `interp_error` | 出错 | `reason`（例如「目标语不支持语音」） |

> **不新增 PCM 帧**：板载出声 ⇒ 浏览器**不订阅** TTS 的 PCM（报告 DD-6）。

#### 3.2.2 改 `cards/qwen3_asr/service.py` —— **只加钩子，不改既有逻辑**

| 位置 | 改动 |
|---|---|
| `_LiveSession._on_delta`（拿到 `unfixed` 的地方） | 调 `interp.on_unfixed(unfixed, language, stream_id, ts)`（**快档**）。⚠️ 必须在**回看轮次过滤之后**、退化时 `unfixed=""` 之后 ⇒ 同传自动继承「不播回看 / 不播胡话」 |
| `_LiveSession._finalize`（`reason == "endpoint"` 分支，`service.py:3093`） | 调 `interp.on_endpoint(text, language, stream_id, ts)`（**稳档**）。⚠️ 现有 `_submit_translation()` **保留不动**（字幕翻译继续走它） |
| `_LiveSession` 增加 `current_text()` | 返回 `committed + unfixed` 拼接（供播前复核用） |
| 新增 `get_interp_engine()` 单例 | 与 `get_translation_service()` 同款（懒加载 + 单例） |

> ⚠️ **同传与字幕翻译的关系**：两者**同时**开时，endpoint 句会**既**进字幕翻译队列**又**进同传引擎 ⇒ **两次翻译**（卡 0 上多一次推理）。
> ⇒ **决策**：同传开启时，**字幕翻译自动让路**（同传优先，报告 DD-10）⇒ 字幕译文晚一点出现，但板载出声优先。

#### 3.2.3 改 `cards/qwen3_asr/api.py` —— 4 个新路由

| 路由 | 方法 | 说明 |
|---|---|---|
| `/qwen3-asr/interp/start` | POST | body：`mode`(`fast`/`steady`) / `target_lang` / `speaker` / `volume`；返回 `{ok, mode, target_lang}` |
| `/qwen3-asr/interp/stop` | POST | 停同传 + 清 TTS 队列（**正在合成的那句跑完**） |
| `/qwen3-asr/interp/status` | GET | §3.2.1 ⑤ 的全部指标 |
| `/qwen3-asr/interp/settings` | POST | 改档位 / 目标语 / 音色 / 音量（**不重启引擎**） |

#### 3.2.4 改 `cards/qwen3_asr/static/template.html` + `app.js`

**面板**（放在实时翻译块 `#asr-tr-block` **下方**，因为它面向麦克风实时路径）：

```
┌ 同声传译 ──────────────────────────────────────────────┐
│ 档位  [快 ●] [稳 ○]     目标语言 [中文 ▾]                │
│ 音色  [news_female ▾]   音量 [——●——]                    │
│ [ 开始同传 ]  [ 停止 ]                                   │
│ 状态：正在播报　滞后 3.2 秒　队列 2 段　变速 1.00　　　　│
│ 提示：中译英语速超过译音产能，译音会逐渐落后（内容不丢） │
└──────────────────────────────────────────────────────────┘
```

- ⚠️ **板端没有 emoji / 符号字体** ⇒ 只用普通汉字 + 内联 SVG（可用 `× ● ■ ▶ ← → ↑ ↓`）。
- ⚠️ 音量滑块用原生 `<input type=range>`（不自己画）。
- 「开始同传」按钮 ⇒ **必须在用户手势里** `resume()` 浏览器 AudioContext（自动播放策略，C10）。

**app.js 改动**：

| 项 | 改动 |
|---|---|
| `LIVE_MAX_PENDING` | **2 → 20**（L1 采集缓冲，≈20 s）⇒ 不丢音频（原则①） |
| 丢块计数 | 暴露到 UI：「上行丢块 N」 |
| WS 帧处理 | 新增 `interp_segment` / `interp_spoken` / `interp_status` / `interp_error` |
| 面板状态刷新 | 1 s 拉一次 `/interp/status`（或用 WS 推的 `interp_status`） |
| 离开卡片 | `DemoGuard` 已有「离开前关后台」机制 ⇒ 同传要注册进去（停止同传 + 清 TTS 队列） |

### 3.3 宿主 `wdnwebui` —— 跨卡门

#### 3.3.1 改 `app/core/audio/tts_bridge.py`

新增（沿用现有「候选路径 + 插件缺失抛错」的模式）：

```python
class TTSUnavailable(RuntimeError):
    """TTS 能力不可用（插件没装 / 实例没跑 / 队列不存在）。"""

def submit_queued(text, language="auto", speaker="", volume=0.1,
                  tempo=1.0, src_end_ts=0.0, stream_id="") -> dict:
    """把一句待播文本丢进 TTS 队列。插件缺失 ⇒ 抛 TTSUnavailable。"""

def queue_status() -> dict: ...
def queue_clear() -> dict: ...
```

- **必须走候选路径解析**（`_TTS_SESSION_MODULES`）⇒ 插件没装时**只影响「同传」这一个功能**，不影响别的卡（AGENTS 铁律）。
- `TTSRequest` 同步加 `save_wav` / `tempo` 两个字段（与插件侧一致）。

#### 3.3.2 `app/static/core/audio-stream-player.js`（**P0 不做**）

报告里提过「把播放模块抽到宿主共享层」⇒ **同传不订阅浏览器 PCM ⇒ 根本不需要这个模块** ⇒ **P0 砍掉**，将来做「浏览器也出声」时再抽。

---

## 4. API 契约（汇总）

### 4.1 TTS 队列（插件 TTS 卡，经宿主桥调用）

```http
POST /api/tts/queue/submit
{ "text": "你好", "language": "chinese", "speaker": "news_female",
  "volume": 0.1, "tempo": 1.05, "src_end_ts": 1795250000.123, "stream_id": "s1" }
→ 200 { "seq": 42, "queued": 2, "est_audio_sec": 1.8 }

GET  /api/tts/queue/status
→ { "depth": 2, "queued_audio_sec": 3.6, "playing": true, "playing_seq": 41,
    "last_lag_sec": 3.2, "avg_lag_sec": 2.9, "merged": 0, "done": 40, "tempo": 1.05 }

POST /api/tts/queue/clear   → { "cleared": 2 }
POST /api/tts/queue/tempo   { "tempo": 1.15 } → { "tempo": 1.15, "applied": 2 }
```

### 4.2 同传（插件 ASR 卡）

```http
POST /qwen3-asr/interp/start
{ "mode": "fast", "target_lang": "zh-CN", "speaker": "news_female", "volume": 0.1 }
→ { "ok": true, "mode": "fast", "target_lang": "zh-CN", "tts": true }

POST /qwen3-asr/interp/stop      → { "ok": true, "cleared": 1 }
GET  /qwen3-asr/interp/status    → { "mode": "fast", "segments": 12, "translated": 12,
                                    "retried": 1, "abandoned": 0, "submitted": 12,
                                    "played": 9, "lag_sec": 3.4, "lag_avg_sec": 3.1,
                                    "queue_depth": 3, "queued_audio_sec": 5.4,
                                    "tempo": 1.05, "tts": true }
POST /qwen3-asr/interp/settings
{ "mode": "steady" }              → { "mode": "steady" }
```

### 4.3 WS 帧（复用 ASR 卡的 `/ws`）

```json
{"type":"interp_segment","index":7,"source":"今天天气很好","lang":"zh-CN","mode":"fast","ts":1795250000.1}
{"type":"interp_spoken","index":7,"lag_sec":3.2,"tempo":1.0}
{"type":"interp_status","segments":12,"played":9,"lag_sec":3.4,"queue_depth":3,"tempo":1.05}
{"type":"interp_error","reason":"目标语不支持语音"}
```

---

## 5. 产能与滞后治理（原则① 的落点）

### 5.1 滞后怎么定义

| 指标 | 定义 | 用途 |
|---|---|---|
| `lag_sec` | **播完**时刻 − **源话说完**时刻 | 主指标（用户看到「译音落后 N 秒」） |
| `lag_start_sec` | 首帧播放时刻 − 源话说完时刻 | 内部诊断（首帧 vs 排队） |
| `queued_audio_sec` | 队列里未开始 job 的估算音频总长 | tempo 决策输入 |

> **为什么用「播完」而不是「开始」**：同传是「一句一句对齐」的体验 ⇒ 整句播完才算追上 ⇒ 用 `lag_sec` 更贴近用户。

### 5.2 tempo 自适应（只在产能不足时启用）

```python
def _auto_tempo(queued_audio_sec: float) -> float:
    if   queued_audio_sec > 8.0: return 1.15
    elif queued_audio_sec > 4.0: return 1.08
    elif queued_audio_sec > 2.0: return 1.04
    else:                        return 1.0
```

- **只在积压时提速** ⇒ 常态下**音质完全不受影响**。
- `tempo ≤ 1.15` ⇒ 覆盖 k ≈ 1.17（中→英 1.15–1.30 只能覆盖下沿）。
- **不变调**（sox `tempo` 是时间域算法，保持音高）⇒ 不影响音色辨识。
- 每次变速都记进指标（`tempo` / `tempo_applied`），UI 如实显示「当前提速 1.05×」。

### 5.3 不丢内容

| 场景 | 动作 |
|---|---|
| TTS 队列未满 | 正常排队 |
| TTS 队列满（128 段） | **合并队尾两段**（内容不丢）⇒ 计 `merged` |
| 翻译失败 | 显示原文 + 标「未翻译」（**不算丢**，是翻译质量问题） |
| 播前复核不通过 | **作废重译**（不是丢弃）⇒ 计 `retried` |
| 重试 3 次仍不一致 | 放弃该段 + UI 标注 ⇒ 计 `abandoned`（**唯一允许的内容损失**，必须计数上报） |
| 上行音频丢块 | **暴露计数**（不再静默丢）⇒ 熔断（连续丢 5 块）保持 |

### 5.4 语言对预判（选目标语时提示）

用 M1 实测的 k 表，在用户选目标语时**预先提示**：

> 「中译英的译音比原话长约 15–30%，当前单卡产能 1.02× ⇒ 译音会**逐渐落后**（内容不会丢）。」

⇒ 用户的预期被管理好，而不是播了两分钟才发现落后。

---

## 6. 播前复核（原则③ 的落点）详细

**问题**：翻译是**一次性定稿**（实测：`translate_one_fast` → `session.respond` 返回完整文本）⇒ 译文不会变。
**真正的风险**：译文**回来之前**，ASR 把源文**改写了**（自我更正 / 模型回看）⇒ 播的是旧版本的译文。

**复核判据**：译文回来时，拿 ASR **当前**的 `committed + unfixed`，检查切句时的 `src_text` 是否**仍是其中连续的一段**。

| 情况 | 判定 | 动作 |
|---|---|---|
| `src_text` 仍在最新文本里 | 通过 | 提交 TTS |
| 不在（模型改写了这段） | 不通过 | 用**最新文本**重送翻译（计 `retried`） |
| 重试仍不一致 | 放弃 | 计 `abandoned`，UI 标注 |

> **稳档下几乎总是通过**（`committed` 不回滚）⇒ 重试集中在快档。
> **为什么值得做**：宁可多 0.3 s 等一次重译，也不要播一句「已经不认了」的译文。

---

## 7. 前端交互

| 元素 | 行为 |
|---|---|
| 「开始同传」 | ① 用户手势里 `resume()` AudioContext；② `POST /interp/start`；③ 面板切到运行态 |
| 「停止」 | `POST /interp/stop` ⇒ 停引擎 + 清 TTS 队列（**正在合成的那句跑完**，不打断） |
| 档位切换 | 快 / 稳，**随时可切**（切了不影响在播的那句） |
| 目标语言 | 复用 `#asr-tr-target`（同一份语言表）⇒ **不可出声的 27 种自动降级为仅字幕** + 面板说明 |
| 音色 | 从 `GET /api/tts/voices` 取（6 个参考音色） |
| 状态行 | `正在播报 · 滞后 3.2 秒 · 队列 2 段 · 变速 1.05` |
| 离开卡片 | `DemoGuard` 触发 ⇒ 停同传 + 清队列（**≤2 s 内停止播放**） |

⚠️ **「实时翻译」与「同声传译」的关系**（报告 DD-11）：
- 两个开关**独立**，但**同传开启时字幕翻译让路**（卡 0 只有一个 MT 实例）。
- 同传关闭 ⇒ 字幕翻译恢复原行为。

---

## 8. 边界与错误处理

| 情况 | 表现 |
|---|---|
| TTS 实例没跑 / 插件没装 | 同传**不可用**：面板显示「TTS 未启用」+ 一个「启用 TTS」按钮（调 `POST /api/compute/instances`） |
| 目标语不可出声（泰语等 27 种） | **自动降级为仅字幕** + 面板说明「本板没有该语言的语音模型」 |
| 源语 == 目标语 | 默认**不朗读**（可选项「朗读原文」） |
| 翻译模型没跑 | 同传**降级为仅字幕**（原文照常显示）+ 明确提示 |
| MT 卡住（>20 s） | 翻译超时 ⇒ 该句标「未翻译」⇒ **不阻塞后续句**（`SENTENCE_TIMEOUT_S = 20`） |
| 板载扬声器被占 | `AplayWriter` 起不来 ⇒ 日志告警 + 面板提示「板载输出不可用」 |
| 回声自激 | 浏览器麦在远端 ⇒ 衰减大；面板**常驻提示**「板载外放 + 近场拾音会自激」 |
| 队列积压 > 60 s | 面板显示「译音落后 N 秒」+ 建议放慢语速 |

---

## 9. 风险与未决项

| # | 风险 | 影响 | 应对 |
|---|---|---|---|
| **R1** | **卡 0 上 ASR 与 MT 同卡争用**（两个 llm_mode 8 核上下文 ⇒ 时间片轮转） | MT 翻译时可能拖慢 ASR 的轮次 ⇒ `unfixed` 滞后变大 | **S0-M6 必测**（见下）；若劣化明显 ⇒ 把 MT 限速（每 500 ms 最多 1 句）**给 ASR 让路** |
| **R2** | sox `play` 在板载 ES8388 上的行为未验证 | 变速路径可能不出声 | S0-M7 实测；保留 `aplay` 分支作回退；变速失败 ⇒ 降级到 1.0 并提示 |
| **R3** | 板载 codec 底噪 + 扬声器→麦自激 | 用户听到自己被重复 | 提示 + 建议用耳机/远置麦 |
| **R4** | 麦缓冲放宽到 20 块 ⇒ 前端内存 + 上行压力 | 低（20 s × 16 kHz f32 ≈ 1.2 MB） | 可接受 |
| **R5** | 单卡 1.02× ⇒ 中译英必然滞后增长 | 用户体验 | 原则①要求「不丢」⇒ 滞后是**允许的代价**；靠 tempo 覆盖 k ≤ 1.17，其余如实显示 |
| **R6** | k 表未实测 | tempo 阈值是猜的 | S0-M1 |
| **R7** | 同传引擎与字幕翻译共用一个 MT 实例 | 互相拖慢 | R1 的应对；或同传时**暂停**字幕翻译 |

### S0 的四项实测（不写业务代码）

| # | 实测 | 方法 | 判据 |
|---|---|---|---|
| **M6** | **卡 0 上 ASR + MT 同卡争用** | 卡 0 起 ASR + hy-mt-1-5；① ASR 单独跑（喂真实音频）记轮间隔；② 同时打 MT 请求，再记轮间隔 | 轮间隔膨胀 > 30% ⇒ 必须给 MT 限速 |
| **M7** | **sox `play` + `tempo` 在板载 codec 上** | `play -q -t raw -f FLOAT_LE -r 24000 -c 1 -D plughw:1,0 tempo 1.1 -` 喂一段 PCM | 出声 ✓、时长缩短 ~9% ✓、音高不变 ✓ |
| **M8** | **麦上行丢块基线** | 浏览器麦 → `/live/chunk`，统计 `dropped_src` | 正常语速下应为 0 |
| **M1** | **语言对膨胀比 k 表** | 10 种可发声语言各 3 句：源句时长 vs 译音时长 | 得 k 表 ⇒ 定 tempo 阈值 + 语言对预判文案 |

> **M6 是本方案最大的未知** —— 它决定「ASR + 翻译同卡」这个部署形态能不能撑住实时性。

---

## 10. 实施顺序与验收

### S0（只测不写）→ 交付：三张数据表
- M6 / M7 / M8 / M1 的结果 + 结论 ⇒ 若 M6 不过 ⇒ 部署形态要改（**回到用户**重新决策）

### S1（TTS 队列 + 变速）→ 交付：可排队、可变速、不落盘
| 验收 | 判据 |
|---|---|
| 提交 5 句 | 依次播完，顺序不乱 |
| 队列深度 | `status` 里深度/积压秒数正确 |
| `tempo=1.15` | 播放时长缩短 ~13%，音高不变（M7 已验） |
| `save_wav=false` | `/userdata/test/tts` **不新增文件** |
| 播放不阻塞合成 | 合成线程不因 aplay 停顿（看 `on_step` 进度是否连续） |

### S2（同传引擎 + 稳档）→ 交付：一句话端到端
| 验收 | 判据 |
|---|---|
| 麦 → 板载出声 | ≤ 12 s 内听到第一句（稳档） |
| 一句不丢 | 连续讲 60 s ⇒ 面板 `played` 句数 == 切句数 |
| 停止 | ≤ 2 s 停止播放 |

### S3（快档 + 复核 + 面板 + 指标）
| 验收 | 判据 |
|---|---|
| 快档首声 | ≤ 8 s |
| 复核 | 人为触发自我更正 ⇒ 旧译文**不播**，`retried` +1 |
| 滞后显示 | 面板显示滞后秒数，与实测一致 |
| tempo | 人为拉长语速 ⇒ 积压 ⇒ tempo 自动升 ⇒ 滞后不再涨 |

---

## 11. 回归

**新增**（放 `scripts/`，命名对齐现有 `verify_asr_*`）：

| 脚本 | 断言 |
|---|---|
| `verify_interp_segmenter.py` | 快档切句器：LCP 连续 N 轮不变才成句；模型改写 ⇒ 作废候选；长度上下限 |
| `verify_interp_recheck.py` | 播前复核：一致⇒提交；不一致⇒重译；重试超限⇒放弃并计数 |
| `verify_tts_queue.py` | 队列：顺序、深度、合并、clear、tempo 动态改（未开始的才改） |
| `verify_interp_api.py` | 4 个路由的入参校验、状态字段、降级路径（无 TTS / 不可出声语种） |

⚠️ **回归必须 mock `PUT`/`DELETE /api/compute/active-app`**（用 `scripts/active_app_guard.install(ctx)` + `install_active_app_spoof(ctx)`，且**须在 `ctx.new_page()` 之前**）⇒ 服务端配置一个字节都不动。
⚠️ **别并发跑回归脚本**。

**既有必须全绿**（改插件 `.py` 后要 `sudo systemctl restart wdnwebui`）：
`verify_asr_translate_toggle.py` / `verify_asr_translate_lang_labels.py` / `verify_asr_wizard_translate.py` / `verify_asr_video_dual_subtitle.py` / `verify_asr_file_translate_ui.py` / `verify_asr_translation_e2e.py` / `verify_asr_seek_subtitle.py` / `verify_asr_live.py` / `verify_asr_model_degeneration.py`。
⚠️ `verify_asr_wizard_translate` 结尾会停掉翻译模型；`verify_asr_translate_lang_labels` 结尾把 `enabled` 设回 false ⇒ **跑完要复原**。
⚠️ **重启会连坐杀 `compute_worker`** ⇒ 重启前确认 `compute.autostart` 有值。

---

## 12. 交付物清单

| # | 交付物 | 位置 |
|---|---|---|
| 1 | 实施方案（本文） | `docs/SIMULTANEOUS_INTERPRETATION_IMPL.md` |
| 2 | 可行性报告（硬件实测 / 市面对比 / 产能推算） | `docs/SIMULTANEOUS_INTERPRETATION_PLAN.md` |
| 3 | 硬件验证探针 | `tools/probes/tts_same_card/` |
| 4 | 插件改动 | `.venv/src/wdnapps-audio-chat/`（P1 起） |
| 5 | 宿主改动 | `app/core/audio/tts_bridge.py`（P1 起） |
| 6 | 回归脚本 | `scripts/verify_interp_*.py`、`scripts/verify_tts_queue.py` |

---

*附：本文的所有板端数字来自本机实测（`tools/probes/tts_same_card/`、`/api/compute`、`rknnsmi` 遥测），非厂商宣传值。**M1 / M6 / M7 / M8 尚未实测**，方案里凡依赖它们的数值都标了「待实测」。*
