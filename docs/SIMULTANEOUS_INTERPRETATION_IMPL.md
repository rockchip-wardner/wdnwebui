# 流式语音识别卡「同声传译」—— 实施方案（v2）

> 状态：**待审核**（先出方案，不实施）
> 配套：`docs/SIMULTANEOUS_INTERPRETATION_PLAN.md` = **为什么**（市面对比 / 硬件实测 / 产能推算）；本文 = **怎么做**。
> **部署形态（已定）**：卡 0 = `qwen3-asr` + `hy-mt-1-5`；卡 1 = **单实例** `qwen3-tts`；**npu2 禁区**；**不重新转模型**。
> **输入源（v2 新增）**：**音频文件** / **带音频的视频文件** / **浏览器麦**（外加卡片已有的**板载麦**）都要能同传。
>
> **v2 相对 v1 改了什么**
>
> 1. **输入源从「仅麦克风」扩到四源**（file / video / board / browser）⇒ 引入**统一抽象 `InterpSegment`** + **两条正交轴**（切句档位 / 播放时机 `now`·`follow`）；
> 2. **两种滞后口径**（实时流「说完→听到」/ 跟播「译文落后画面」）⇒ 跟播模式下不能用流式口径（会把「用户暂停」误报成滞后）；
> 3. **播放头走 REST 上报**（`/interp/playhead`，250 ms 一次，带 `stream_id`）⇒ ASR 卡的 `/ws` 是单向广播，反向通道只能走 POST；
> 4. ⚠️ **订正 v1 的一处事实错误**：v1 把 `#asr-preview-audio` 当成音频文件源的播放头 —— **它是向导的预览播放器**（只在点「预览播放」时加载、只吃设备端路径、还会被向导框架搬来搬去）⇒ **音频文件源今天根本没有播放头** ⇒ 方案里新增「卡片页自有的 `#asr-audio-player`」（§1.4）；
> 5. **文件流的快档口径**改成「强制切句长度 60 → 24」（不给 `_SentenceSplitter` 的全局常量动刀，避免影响字幕翻译的切句）；
> 6. **不丢内容**的路径写实：队列满 ⇒ 合并队尾（不丢）；复核不过 ⇒ 作废重译；重试 3 次 ⇒ 才放弃（唯一允许的损失）。

---

## 0. 一页速览

### 0.1 产能账（决定方案的形状）

| 环节 | 速率 | 来源 |
|---|---|---|
| TTS 合成（卡 1，单实例） | **1.02×** 实时 | 实测（4.19 s 合成 / 4.28 s 音频） |
| TTS 首帧 | 2.8–3.3 s | 实测 |
| TTS PCM 块 | **4 s 一块** | SDK 行为 |
| 翻译（卡 0，单实例 hy-mt-1-5） | `avg_latency_ms ≈ 263`，**一次性定稿** | 实测 |
| ASR `unfixed` 滞后 | ~1 s | 结构性 |
| ASR `committed` 滞后 | 4–5 s | 结构性回看窗口 |

**语言对膨胀比 k（译音时长 ÷ 源语时长）**：

| 语言对 | k（估） | 单卡 1.02× 的后果 |
|---|---|---|
| 英→中 / 日→中 / 德→中 | 0.8–1.0 | **够 ⇒ 滞后恒定**（产品主场景） |
| 中→英 | 1.15–1.30 | **不够 ⇒ 滞后随讲话时长增长** |
| 中→日 | ~1.4 | 不够 |

⇒ **k 表必须先实测**（M1），否则 `tempo` 阈值是拍脑袋。

### 0.2 输入源矩阵（**v2 的核心**）

卡片已有 **4 个来源 tab**，它们的**切句信号、文本语义、播放头**三者各不相同 ⇒ 同传引擎必须把它们**归一**。

| 来源 tab | 音频怎么进来 | 服务端链路 | 切句信号 | 公开文本会回滚吗 | 有播放头吗 |
|---|---|---|---|---|---|
| **file**（音频文件 / 设备端路径） | 服务端解码 | `POST /transcribe` → `_AsrManager._stream_main` | **标点断句**（`_SentenceSplitter`） | ❌ 不回滚（`committed` 只会增长） | ⚠️ **今天没有** ⇒ 要**新增**卡片页 `<audio id="asr-audio-player">`（§1.4） |
| **video**（含音轨的视频） | 服务端解码 | 同上 | **标点断句** | ❌ 不回滚 | ✅ `#asr-video`（`<video controls>`） |
| **board**（板载麦） | 板载 ALSA → 采集进程 → HTTP | `board_mic` → `/live/start` + `/live/chunk` ⇒ **同一条实时链路** | **endpoint**（静音 1200 ms） | ✅ 会（在线回看 5 token） | ❌ |
| **browser**（浏览器麦） | `getUserMedia` → 上传 | `/live/start` + `/live/chunk` | **endpoint** | ✅ 会 | ❌ |

**⇒ 归一后的两条轴**：

```
                    ┌── 实时流（board / browser）──▶ 切句：endpoint 或 稳定前缀
                    │                                  文本：会回滚 ⇒ 播前复核有意义
  输入源 ───────────┤
                    └── 文件流（file / video）──────▶ 切句：标点 / 强制长度
                                                       文本：不回滚 ⇒ 复核几乎必过
```

⇒ **两种「播放时机」**（正交于切句档位）：

| 时机 | 含义 | 适用源 | 默认 |
|---|---|---|---|
| **`now`（不等播放）** | 句子一识别完就译、就播 | **全部四种源** | board / browser |
| **`follow`（跟播放）** | 译文按**播放头**放行 ⇒ 与画面/音频同步 | **仅 file / video** | file / video |

### 0.3 改动地图

| 仓库 | 文件 | 动作 | 规模 |
|---|---|---|---|
| 插件 `wdnapps_audio_chat` | `cards/tts/queue.py` | **新增**：合成队列（串行 + 变速 + 滞后记账） | 新文件 ~260 行 |
| 插件 | `cards/tts/service.py` | 改：`tempo` 变速 + 不落 WAV | ~120 行 |
| 插件 | `cards/tts/api.py` | 改：队列 4 个路由 | ~80 行 |
| 插件 | `cards/qwen3_asr/interp.py` | **新增**：同传引擎（归一 + 放行 + 复核 + 指标） | 新文件 ~480 行 |
| 插件 | `cards/qwen3_asr/service.py` | 改：**两处**钩子（实时 endpoint / 文件 `_tr_emit`）+ 文件路径快档切句长度 | ~90 行 |
| 插件 | `cards/qwen3_asr/api.py` | 改：同传 4 个路由 + **播放头上报**路由 | ~100 行 |
| 插件 | `cards/qwen3_asr/static/template.html` | 改：同传面板（**按源变形态**）+ **新增音频播放器 `#asr-audio-player`** | ~110 行 |
| 插件 | `cards/qwen3_asr/static/app.js` | 改：面板 + 播放头上报 + WS 帧 + 麦缓冲 2→20 + **新增 `_audioUrl()` / 播放器同步** | ~260 行 |
| 宿主 `wdnwebui` | `app/core/audio/tts_bridge.py` | 改：加 `submit_queued` / `queue_status` / `queue_clear`（**跨卡唯一门**） | ~60 行 |

⇒ **不动 SDK（`wdnai`）、不动 `compute_worker`、不动 TTS 卡自己的页面、不新建卡片。**

### 0.4 四步走

| 步 | 内容 | 可独立验收 |
|---|---|---|
| **S0** | **只测不写**：M6 同卡 ASR+MT 争用 / M7 sox `play`+tempo / M8 麦上行丢块 / M1 k 表 | 四张数据表 |
| **S1** | **TTS 队列 + 变速**（插件 TTS 卡自成闭环） | 排队、变速、不落盘 |
| **S2** | **同传引擎 + 实时源**（board / browser） | 一句话端到端（麦 → 译 → 板载出声） |
| **S3** | **文件 / 视频源**（播放头放行 + 跟播） | 视频放 ⇒ 译文跟画面同步 |

---

## 1. 三种输入源的机制差异（读代码得出的事实）

### 1.1 四个来源的实际链路

| tab | 前端 | 服务端 | 关键代码 |
|---|---|---|---|
| file | `#asr-source`（设备端路径）→ `POST /transcribe` | 解码 → 逐块喂 ASR worker | `service.py::_AsrManager._stream_main` |
| video | 上传/路径 → `POST /transcribe`（含 ffmpeg 取音轨） | 同上，**额外**给前端 `audio_origin_sec` | `service.py:1983` |
| board | `POST /live/board/start` → 板载采集进程 → `POST /live/chunk` | 与 browser **同一条** | `board_mic.py:12` |
| browser | `getUserMedia` → `POST /live/chunk` | 同上 | `service.py::_LiveSession` |

### 1.2 两种切句信号

**① 实时流（board / browser）：endpoint 帧**

- 端点检测：`AdaptivePauseDetector`（`app/core/audio/endpoint.py:307`）
  - `silence_threshold_dbfs = -45`、`endpoint_silence_ms = 1200`、`min_speech_sec = 0.3`
- 命中 ⇒ `_LiveSession._finalize(reason="endpoint")` ⇒ 广播 `endpoint` 帧 ⇒ **紧接着调用 `_submit_translation()`**（`service.py:3093`）⇒ 字幕翻译已经在跑。
- ⇒ **稳档的切句点现成**，同传只需在旁边加一个钩子。

**② 文件流（file / video）：标点断句**

- `_SentenceSplitter`（`service.py:531`）：句末标点 `。！？!?`（`_TR_SENTENCE_END`）为主，
  **强制长度** `_TR_MAX_SENTENCE_CHARS = 60` 为兜底，epoch 切换再兜底一次。
- 切出的句子由 `_tr_emit`（`service.py:1266`）送去翻译，**带 `start_sec` / `end_sec`**（音轨时间，由 `_tr_time_at(c0)` 换算）。

### 1.3 文本语义：会不会回滚（决定「播前复核」有没有用）

| 源 | 公开文本 | 回滚？ | 复核价值 |
|---|---|---|---|
| 实时流 | `unfixed`（前沿 5 token，**每轮在滚**）+ `committed` | ✅ 会 | **高**（快档必须复核） |
| 文件流 | `_epoch_text`（`committed` 剥掉回看） | ❌ 不会 | 低（稳档几乎必过） |

### 1.4 播放头：有的有、有的没有、有的**要新建**

| 源 | 播放头元素 | 换算 | 现状 |
|---|---|---|---|
| video | `#asr-video`（`<video controls>`，卡片页上） | `currentTime − audio_origin_sec`（音轨起点） | ✅ 已有 |
| file | **要新增** `#asr-audio-player`（`<audio controls>`，卡片页上） | `currentTime`（纯音频 ⇒ 音轨起点 = 0） | ❌ **今天没有** |
| board / browser | — | — | — |

⚠️ **`#asr-preview-audio` 不能拿来当播放头**（这是个很容易踩的坑）：

* 它是**向导**的预览播放器 —— 只有用户点「预览播放」时 `_previewOpen()` 才加载它（`app.js:881`）；
* 它只吃**设备端路径**（读 `#asr-source` 的值），**上传的本机音频它根本没有源**；
* 向导框架会把 `#asr-preview-wrap` **搬进 / 搬出**向导面板 ⇒ 元素位置会变、还可能被 `hidden`。

⇒ **必须新增一个卡片页自己的音频播放器**：

| 项 | 值 |
|---|---|
| 元素 | `<audio id="asr-audio-player" controls preload="metadata">`（卡片页，**不进向导**） |
| 本机上传的音频 | `this._fileBlobUrl`（`_setFileBlob()` 已经建好了 blob URL） |
| 设备端路径的音频 | `/api/qwen3-asr/media?path=…`（与 `_videoUrl()` 同款） |
| 时间轴 | 纯音频 ⇒ 音轨起点 = 0 ⇒ `currentTime` 直接与 `src_start_sec` / `src_end_sec` 同轴 ✓ |
| 与视频的差别 | 视频要减 `audio_origin_sec`（音轨在容器里的起点），音频不用 |

⚠️ **上传的音频在服务端没有落盘路径** ⇒ 播放头只能来自浏览器的 blob ⇒ 但服务端解码的是**同一份字节** ⇒ 两侧时间轴**精确同轴** ✓（不是估算）。

⚠️ **file 源的播放头有个坑**：新加的 `#asr-audio-player` 是**独立**的元素，用户可能压根没点播放 ⇒ 播放头不动 ⇒ `follow` 模式下译文永远不放行 ⇒ 用户以为坏了。
⇒ **缓解**（§7.3）：file 源在「开始识别」时**自动播放**（与 video 源的 `_playVideoIfAny()` 对称）+ 面板提示「点播放键让译文跟上」+ 一键切「不等播放」。

### 1.5 ⇒ 统一抽象

> **同传引擎只认一种东西：`InterpSegment`（一句源文 + 它在时间轴上的位置）。**
> 四种源各自负责把自己的原生信号**翻译成** `InterpSegment`；引擎只管「放行 → 翻译 → 复核 → 合成 → 记账」。

---

## 2. 核心抽象

### 2.1 `InterpSegment` —— 一句源文

```python
@dataclass
class InterpSegment:
    index: int              # 全局递增（跨源、跨 epoch）
    text: str               # 源文
    lang: str               # 源语
    stream_id: str          # 哪条流（file-3#1 / live-xxx）
    mode: str               # "fast" | "steady"
    #: 源文在**音频时间轴**上的起止（秒）。实时流没有 ⇒ -1.0
    src_start_sec: float = -1.0
    src_end_sec: float = -1.0
    #: 服务端收到这句的时刻（实时流算滞后用）
    created_ts: float = 0.0
    #: 来源标签（"live" | "file"）—— 决定用哪种滞后口径
    origin: str = "live"
```

### 2.2 放行器（`now` / `follow`）

```python
class Gate:
    """决定「这句现在能不能进 TTS 队列」。"""

    def __init__(self, mode: str = "now"):        # "now" | "follow"
        self._mode = mode
        self._playhead: dict[str, float] = {}     # stream_id → 播放头（音频时间轴）
        self._playing: dict[str, bool] = {}

    def admit(self, seg: InterpSegment) -> bool:
        if self._mode == "now":
            return True
        # follow：等播放头到达（提前 lead_sec 放行，给 TTS 留出首帧时间）
        ph = self._playhead.get(seg.stream_id)
        if ph is None:
            return False                          # 还没有播放头 ⇒ 等
        return ph >= (seg.src_start_sec - FOLLOW_LEAD_SEC)
```

- `FOLLOW_LEAD_SEC = 1.0`（提前 1 s 放行 ⇒ TTS 有时间做首帧 ⇒ 译文不会明显晚于画面）。
- **播放头由前端上报**（见 §4.3）：`POST /qwen3-asr/interp/playhead` `{stream_id, playhead_sec, playing}`，**250 ms 一次**。

**seek 处理**：播放头**回退**超过 `SEEK_BACK_TOLERANCE = 2.0` s ⇒ 判定为 seek ⇒
**清掉「播放头之前还没播的」待播句**（用户自己跳过的内容，不算丢）⇒ 计 `skipped_seek`，面板显示。

### 2.3 播前复核（实时流才有意义）

翻译是**一次性定稿** ⇒ 译文不会变；真正的风险是**译文回来前源文被改写**。

```python
def _recheck(self, seg: InterpSegment, latest: str) -> bool:
    return seg.text in latest        # 仍是最新文本的连续一段
```

| 情况 | 动作 | 计数 |
|---|---|---|
| 通过 | 提交 TTS | — |
| 不通过 | 用**最新文本**重送翻译 | `retried` |
| 重试 3 次仍不一致 | 放弃该段 + UI 标注 | `abandoned`（**唯一允许的内容损失**） |

> **文件流**：`committed` 不回滚 ⇒ 复核几乎必过 ⇒ 只在「强制切句边界被后续标点修正」这种极小概率下触发。

### 2.4 滞后指标（**两种口径，按源分**）

| 口径 | 适用 | 定义 | 含义 |
|---|---|---|---|
| **流式滞后** | board / browser（`now`） | `播完时刻 − 源话说完时刻` | 用户说完后多久听到译文 |
| **跟播滞后** | file / video（`follow`） | `已播句的 src_end_sec − 当前播放头` | 译文落后画面多少秒 |

> **为什么要两种**：跟播模式下「译文落后画面」才是用户能感知的东西；用流式口径会把「用户暂停播放」也算成滞后 ⇒ 误报。

---

## 3. 端到端数据流

### 3.1 实时流（board / browser）

```
麦 → L1(20块) → L2 串行上传 → 服务端 L3(20块) → ASR worker
                                                   │ asr_round{committed,unfixed,language}
        ┌──────────────────────────────────────────┘
        ▼
   ① 稳档：endpoint 帧（静音 1200 ms）→ InterpSegment
     快档：LCP(unfixed) 连续 2 轮不变     → InterpSegment
        ▼
   ② Gate(now) → 直接放行
        ▼
   ③ 翻译（卡 0）→ 一次性定稿
        ▼
   ④ 播前复核（实时流才有意义）
        ▼
   ⑤ TTS 队列（卡 1，单实例串行）→ PCM(4s/块, UDS) → AplayWriter(+tempo) → 板载扬声器
        ▼
   ⑥ 记账：lag_sec / queue_depth / tempo → interp_status 帧 → 面板
```

### 3.2 文件流（file / video）

```
媒体文件 → 服务端解码 → ASR worker（**跑在播放头前面**，实测「识别前沿比播放头快好几倍」）
                                                   │ round{sentences:[{text,start,end}]}
        ┌──────────────────────────────────────────┘
        ▼
   ① 标点断句（`_SentenceSplitter`）→ InterpSegment（带 src_start/end）
        ▼
   ② 翻译（卡 0）→ 提前备好译文（不等播放头）
        ▼
   ③ Gate(follow)：播放头到达 src_start − 1 s ⇒ 放行进 TTS 队列
        ▼
   ④ TTS 队列（串行）→ 板载扬声器
        ▼
   ⑤ 记账：跟播滞后 = 已播句 src_end − 播放头
```

> ⚠️ **识别跑在播放头前面 ⇒ 译文通常提前备好** ⇒ `follow` 模式下 TTS 队列里通常只有 0–2 句 ⇒ 跟播滞后 ≈ **TTS 首帧 + 合成 ≈ 3–4 s，恒定**。
> ⚠️ **若 k > 1.02**：TTS 产能跟不上播放消耗 ⇒ 队列变长 ⇒ 跟播滞后**持续增长** ⇒ 靠 `tempo` 压 k。

> **file 源要额外加一个播放器**（§1.4）：今天卡片页上的音频文件源**没有播放器** ⇒ `follow` 模式要用，得先加 `#asr-audio-player`（视频源有现成的 `#asr-video`）。
> ⇒ **S3 的第一件事就是加这个播放器**，否则 file 源只能走「不等播放」。

### 3.3 时序预算

| 场景 | 首声延迟 | 说明 |
|---|---|---|
| 实时流 · 稳档 | **≈ 9–11 s** | endpoint(1.2 s) + committed(4–5 s) + 翻译(0.3 s) + TTS 首帧(3 s) |
| 实时流 · 快档 | **≈ 5.3–6.8 s** | 稳定窗口(1–2 s) + 翻译 + TTS 首帧 |
| 文件 / 视频 · 跟播 | **≈ 3–4 s（恒定）** | 译文提前备好 ⇒ 只剩 TTS 首帧 + 合成 |

---

## 4. 模块改动清单

### 4.1 插件 ASR —— 新增 `cards/qwen3_asr/interp.py`

**职责**：归一（源 → `InterpSegment`）→ 放行 → 翻译 → 复核 → 提交 TTS → 记账 → 广播。

```python
INTERP_STABLE_ROUNDS = 2      # 快档：LCP 连续 N 轮不变才成句（实时流）
INTERP_MIN_CHARS = 8          # 低于此不切
INTERP_MAX_CHARS = 40         # 单句上限（实时流快档）
FOLLOW_LEAD_SEC = 1.0         # 跟播：提前放行量
SEEK_BACK_TOLERANCE = 2.0     # 播放头回退多少秒算 seek
INTERP_QUEUE_MAX = 64         # 待播句上限（超了合并队尾，不丢内容）
RETRY_LIMIT = 3               # 复核重试上限
```

**① 快档切句器（仅实时流）**

```python
class StablePrefixSegmenter:
    """从 unfixed 求「最长公共前缀」，连续 N 轮不变 ⇒ 成句。"""
    def feed(self, unfixed: str) -> Optional[str]:
        # 1) 与上一轮求 LCP
        # 2) LCP 变长 ⇒ 候选更新，稳定计数清零
        # 3) LCP 连续 N 轮「长度与内容都不变」⇒ 成句
        # 4) LCP 变短（模型改写）⇒ **作废候选**，重新开始
```

> **为什么不用 `committed` 增量**：文件流的 `committed` 只增长、不回滚 ⇒ 「说话中」与「停顿」分不开 ⇒ 快档判据在文件流上失效 ⇒ **文件流的快档改用「更短的强制切」**（见 §4.2）。

**② 翻译编排**
- **不复用字幕翻译队列**（那是「历史可回看」，深度 256、丢最旧）⇒ 同传用**独立的小队列**（深度 64、不丢）。
- 但**复用同一个 MT 实例**（卡 0 的 hy-mt-1-5）⇒ ⚠️ 同卡争用风险 **R1**（§9）。
- 调用方式复用 `app.core.translate.npu_manager.translate_sentence`（与 `translation.py::_handle` 同款）。

**③ 提交 TTS**（经宿主桥）

```python
from app.core.audio.tts_bridge import submit_queued, TTSUnavailable
submit_queued(text=译文, language=目标语, speaker=音色, volume=音量,
              tempo=当前tempo, src_end_ts=seg.src_end_sec, stream_id=…)
```

- 桥缺失 / TTS 没启用 ⇒ 抛 `TTSUnavailable` ⇒ 同传**降级为仅字幕**（UI 明说）。

**④ 指标**（`status()`）

| 指标 | 含义 |
|---|---|
| `mode` / `gate` | 切句档位 / 播放时机（`now` / `follow`） |
| `source` | `live` / `file`（决定滞后口径） |
| `segments` / `translated` / `retried` / `abandoned` | 切句 / 翻译成功 / 重译 / 放弃 |
| `submitted` / `played` | 已送 TTS / 已播完 |
| `lag_sec` / `lag_avg_sec` | **当前滞后**（按源选口径） |
| `queue_depth` / `queued_audio_sec` | TTS 队列深度 / 积压音频秒数 |
| `tempo` | 当前变速 |
| `playhead_sec` | 当前播放头（follow 模式） |
| `skipped_seek` | 因 seek 跳过的句数 |

**⑤ 广播帧**（新增 `type`，复用 ASR 卡的 `/ws`）

| 帧 | 时机 | 字段 |
|---|---|---|
| `interp_segment` | 成句 | `index`, `source`, `text`, `lang`, `mode`, `start_sec`, `end_sec` |
| `interp_spoken` | 播完 | `index`, `lag_sec`, `tempo` |
| `interp_status` | 1 s 一次 | 上表全部指标 |
| `interp_error` | 出错 | `reason` |

> **不新增 PCM 帧**：板载出声 ⇒ 浏览器**不订阅** TTS 的 PCM。

### 4.2 插件 ASR —— 改 `cards/qwen3_asr/service.py`

| 位置 | 改动 |
|---|---|
| `_LiveSession._finalize(reason="endpoint")`（`service.py:3093` 附近） | 调 `interp.on_endpoint(text, lang, stream_id, ts)`（**稳档**）。⚠️ 现有 `_submit_translation()` **保留不动**（字幕翻译继续走它） |
| `_LiveSession._on_delta`（拿到 `unfixed` 的地方） | 调 `interp.on_unfixed(unfixed, lang, stream_id, ts)`（**快档**）。⚠️ 必须在**回看轮次过滤之后**、退化时 `unfixed=""` 之后 ⇒ 自动继承「不播回看 / 不播胡话」 |
| `_AsrManager._tr_emit`（`service.py:1266`） | 调 `interp.on_file_segment(text, seq, start_sec, end_sec, stream_id, ts)`（**文件流**，两种档位共用） |
| `_LiveSession` / `_AsrManager` 增加 `current_text(stream_id)` | 返回当前公开文本（`committed + unfixed`）⇒ 供播前复核 |
| `_TR_MAX_SENTENCE_CHARS`（`service.py:427`，现 60） | **不全局改**（会影响字幕翻译的切句）⇒ 在 `_SentenceSplitter` 上加一个**可选**的 `max_chars` 参数，同传快档用 24、字幕翻译仍用 60 |
| 新增 `get_interp_engine()` 单例 | 与 `get_translation_service()` 同款（懒加载 + 单例） |

> ⚠️ **文件流的快档口径**：把「强制切句长度」从 60 降到 **24** ⇒ 句子更碎、**更早**出句 ⇒ 翻译更早开始 ⇒ 跟播滞后更小（代价：句子更碎、语义边界更差）。
> ⚠️ **文件流的稳档口径**：维持现状（等标点，最长 60）。

### 4.3 插件 ASR —— 改 `cards/qwen3_asr/api.py`

| 路由 | 方法 | 说明 |
|---|---|---|
| `/qwen3-asr/interp/start` | POST | `{mode, gate, target_lang, speaker, volume}` ⇒ `{ok, mode, gate, source, tts}` |
| `/qwen3-asr/interp/stop` | POST | 停引擎 + 清 TTS 队列（**正在合成的那句跑完**） |
| `/qwen3-asr/interp/status` | GET | §4.1 ④ 的全部指标 |
| `/qwen3-asr/interp/settings` | POST | 改档位 / 播放时机 / 目标语 / 音色 / 音量（**不重启引擎**） |
| **`/qwen3-asr/interp/playhead`** | POST | `{stream_id, playhead_sec, playing}` —— **前端每 250 ms 上报**（见 §4.4） |

> **为什么播放头走 REST 而不是 WS**：ASR 卡的 `/ws` 是**服务端 → 浏览器**的单向广播；播放头是**浏览器 → 服务端** ⇒ 走 POST 最省事（4 req/s，可忽略）。
> ⚠️ 上报要带 `stream_id`（文件流是 `file-<rid>#<epoch>`）⇒ 服务端按流维护播放头。

### 4.4 插件 ASR —— 改 `static/app.js` + `template.html`

**面板**（放在实时翻译块 `#asr-tr-block` 下方）：

```
┌ 同声传译 ─────────────────────────────────────────────────┐
│ 切句 [快 ●][稳 ○]   播放 [跟播放 ●][不等播放 ○]            │
│ 目标语言 [中文 ▾]   音色 [news_female ▾]   音量 [——●——]    │
│ [ 开始同传 ]  [ 停止 ]                                     │
│ 状态：正在播报 · 滞后 3.2 秒 · 队列 2 段 · 变速 1.00        │
│ 提示：当前来源「音频文件」，跟播放模式下译文会跟着播放头走  │
└───────────────────────────────────────────────────────────┘
```

- ⚠️ **板端没有 emoji / 符号字体** ⇒ 只用普通汉字 + 内联 SVG（可用 `× ● ■ ▶ ← → ↑ ↓`）。
- ⚠️ **「播放时机」对 board / browser 置灰**（没有播放头）⇒ 自动显示 `now`。

**app.js 改动**

| 项 | 改动 |
|---|---|
| **播放头上报** | 新增 `_startPlayheadReporter()`：每 250 ms 读播放头并 POST |
| 播放头来源 | video ⇒ `#asr-video`.currentTime − `_audioOriginSec`；file ⇒ **新增** `#asr-audio-player`.currentTime；board/browser ⇒ **不上报** |
| 播放器事件 | `seeking` / `seeked` ⇒ 立即上报一次（让服务端尽快察觉跳转） |
| `pause` / `play` | 上报 `playing=false/true`（暂停 ⇒ 译文也该停） |
| **新增音频播放器** | template 加 `<audio id="asr-audio-player" controls>`；app.js 加 `_audioUrl()`（上传 ⇒ `_fileBlobUrl`；路径 ⇒ `/media?path=`，与 `_videoUrl()` 同款）+ `_syncAudioPlayer()`（换源时同步 `src`、换源时停掉旧播放器） |
| **file 源自动播放** | 「开始识别」时调 **新增**的 `#asr-audio-player.play()`（与 video 源的 `_playVideoIfAny()` 对称）⇒ `follow` 模式可用；`play()` 被浏览器拦 ⇒ 捕获并提示 |
| `LIVE_MAX_PENDING` | **2 → 20**（L1 采集缓冲；`LIVE_CHUNK_MS = 500` ⇒ 1 s → **10 s**）⇒ 不丢音频（原则①） |
| 丢块计数 | 暴露到 UI：「上行丢块 N」 |
| WS 帧处理 | 新增 `interp_segment` / `interp_spoken` / `interp_status` / `interp_error` |
| 离开卡片 | `DemoGuard` 注册同传停止（停引擎 + 清队列），≤2 s 内停止播放 |

> ⚠️ **暂停时的行为**：`follow` 模式下用户暂停 ⇒ 播放头不动 ⇒ 新句不再放行 ⇒ **译文自动跟着停**（自然达成，无需额外逻辑）✓
> ⚠️ `now` 模式（实时流）没有播放头 ⇒ 用户暂停浏览器麦 ⇒ 没有新句子 ⇒ 也不会有新译文 ✓

### 4.5 插件 TTS —— 队列化 + 变速

#### 新增 `cards/tts/queue.py`

```python
QUEUE_MAX = 128        # 队列深度（段）。只播不存 ⇒ 内存里只有文本，很便宜。
TEMPO_MAX = 1.15       # 变速上限（sox tempo）。超过音质明显变紧。
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
    src_end_ts: float = 0.0
    created_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0
    est_audio_sec: float = 0.0     # 估算音频时长（算积压用）
```

**关键设计**

1. **串行消费**：一个后台线程 `while True: job = q.get(); session.run_queued(job)` ⇒ **天然按提交顺序播放**。
2. **`tempo` 可动态改**：**已经被取走开始合成的**改不了；**还在队列里**的可以 ⇒ `set_tempo(f)` 遍历未开始的 job 更新 ⇒ 积压一降就自动回落 1.0。
3. **不丢内容**：队列满时**合并队尾两段**（文本拼成一段重新合成），计 `merged` ⇒ **内容一个不丢**，代价只是那句更长。
4. **滞后记账**：`lag_sec = job.finished_at − job.src_end_ts` ⇒ 落进 `snapshot()`。
5. **积压估算**：`queued_audio_sec = Σ est_audio_sec(未开始)` ⇒ tempo 决策的输入。

> ⚠️ **单实例下不需要「per-request PCM socket」**：`_run_safe`（`service.py:994`）**每次 run 都新建 UDS socket**（`service.py:1016`）⇒ 串行消费时天然一请求一 socket。

#### 改 `cards/tts/service.py`

**(a) `TTSRequest` 加两个字段**（宿主 `tts_bridge.py` 同步加）：

| 字段 | 默认 | 作用 |
|---|---|---|
| `save_wav: bool` | `True` | `False` ⇒ 同传**不落 WAV**（防 eMMC 被灌满） |
| `tempo: float` | `1.0` | 播放变速（>1.0 生效） |

**(b) `AplayWriter` 支持变速**（`service.py:51`）

```python
if tempo > 1.001:
    cmd = ["play", "-q", "-t", "raw", "-f", "FLOAT_LE",
           "-r", str(sample_rate), "-c", str(channels)]
    if device: cmd += ["-D", device]
    cmd += ["tempo", "%.3f" % tempo, "-"]
else:
    cmd = ["aplay", "-t", "raw", "-f", "FLOAT_LE", ...]   # 原路径一字不改
```

> ⚠️ **必须保留 `aplay` 分支**：sox `play` 在板载 codec 上的行为**未实测**（M7）⇒ 默认路径零风险，只在 `tempo > 1` 时才走 sox。

**(c) `_run_safe` 支持「不落盘」**：`req.save_wav == False` ⇒ 跳过 `_assemble_wav` + 写盘（但仍累积 PCM 用于统计播放时长）。

**(d) 新增 `submit_queued(req, meta)`**：校验文本非空、**目标语在可发声集合内**（不在 ⇒ 抛 `RuntimeError`，由调用方降级为仅字幕）⇒ 入队 ⇒ 返回 `{seq, queued, est_audio_sec}`。

**(e) 新增 `run_queued(job)`**：转成 `TTSRequest` 后调 `_run_safe(req)`，完成后回调队列记 `finished_at`。

#### 改 `cards/tts/api.py` —— 4 个新路由

| 路由 | 方法 | 说明 |
|---|---|---|
| `/api/tts/queue/submit` | POST | `{text, language, speaker, volume, tempo, src_end_ts, stream_id}` ⇒ `{seq, queued, est_audio_sec}` |
| `/api/tts/queue/status` | GET | `{depth, queued_audio_sec, playing, playing_seq, last_lag_sec, avg_lag_sec, merged, done, tempo}` |
| `/api/tts/queue/clear` | POST | 清空未开始的 job（**已在合成的跑完**）⇒ `{cleared}` |
| `/api/tts/queue/tempo` | POST | `{tempo}` ⇒ 改未开始 job 的变速 ⇒ `{tempo, applied}` |

> ⚠️ 这 4 个路由**只给同传用**；TTS 卡自己的页面**不用**（继续走 `/start` 单发）。同传是**另一个卡片**在调 ⇒ **必须经宿主 `tts_bridge`**，不能让 ASR 卡直接 import TTS 卡（AGENTS 铁律：卡片只能 import `app.core.*`）。

### 4.6 宿主 —— 改 `app/core/audio/tts_bridge.py`

```python
class TTSUnavailable(RuntimeError):
    """TTS 能力不可用（插件没装 / 实例没跑 / 队列不存在）。"""

def submit_queued(text, language="auto", speaker="", volume=0.1,
                  tempo=1.0, src_end_ts=0.0, stream_id="") -> dict: ...
def queue_status() -> dict: ...
def queue_clear() -> dict: ...
```

- **沿用现有「候选路径 + 插件缺失抛错」模式** ⇒ 插件没装时**只影响「同传」这一个功能**。
- `TTSRequest` 同步加 `save_wav` / `tempo`。

> ❌ **P0 不做** `app/static/core/audio-stream-player.js`：同传不订阅浏览器 PCM ⇒ 根本不需要 ⇒ 将来做「浏览器也出声」时再抽。

---

## 5. API 契约（汇总）

### 5.1 TTS 队列（插件 TTS 卡，经宿主桥调用）

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

### 5.2 同传（插件 ASR 卡）

```http
POST /qwen3-asr/interp/start
{ "mode": "fast", "gate": "follow", "target_lang": "zh-CN",
  "speaker": "news_female", "volume": 0.1 }
→ { "ok": true, "mode": "fast", "gate": "follow", "source": "file", "tts": true }

POST /qwen3-asr/interp/stop   → { "ok": true, "cleared": 1 }
GET  /qwen3-asr/interp/status
→ { "mode": "fast", "gate": "follow", "source": "file", "segments": 12,
    "translated": 12, "retried": 0, "abandoned": 0, "submitted": 12, "played": 9,
    "lag_sec": 3.4, "lag_avg_sec": 3.1, "queue_depth": 3, "queued_audio_sec": 5.4,
    "tempo": 1.05, "playhead_sec": 41.2, "skipped_seek": 0, "tts": true }

POST /qwen3-asr/interp/settings  { "mode": "steady" }  → { "mode": "steady" }

POST /qwen3-asr/interp/playhead
{ "stream_id": "file-3#1", "playhead_sec": 41.2, "playing": true } → { "ok": true }
```

### 5.3 WS 帧（复用 ASR 卡的 `/ws`）

```json
{"type":"interp_segment","index":7,"source":"file","text":"今天天气很好","lang":"zh-CN",
 "mode":"steady","start_sec":12.4,"end_sec":14.1}
{"type":"interp_spoken","index":7,"lag_sec":3.2,"tempo":1.0}
{"type":"interp_status","segments":12,"played":9,"lag_sec":3.4,"queue_depth":3,"tempo":1.05}
{"type":"interp_error","reason":"目标语不支持语音"}
```

---

## 6. 产能与滞后治理（原则① 的落点）

### 6.1 tempo 自适应（只在产能不足时启用）

```python
def _auto_tempo(queued_audio_sec: float) -> float:
    if   queued_audio_sec > 8.0: return 1.15
    elif queued_audio_sec > 4.0: return 1.08
    elif queued_audio_sec > 2.0: return 1.04
    else:                        return 1.0
```

- **只在积压时提速** ⇒ 常态下音质完全不受影响。
- `tempo ≤ 1.15` ⇒ 覆盖 k ≈ 1.17（中→英 1.15–1.30 只覆盖下沿）。
- **不变调**（sox `tempo` 是时间域算法，保持音高）。
- 每次变速都记进指标，UI 如实显示「当前提速 1.05×」。

### 6.2 不丢内容

| 场景 | 动作 |
|---|---|
| TTS 队列未满 | 正常排队 |
| TTS 队列满（128 段） | **合并队尾两段**（内容不丢）⇒ 计 `merged` |
| 翻译失败 | 显示原文 + 标「未翻译」（**不算丢**） |
| 播前复核不通过 | **作废重译**（不是丢弃）⇒ 计 `retried` |
| 重试 3 次仍不一致 | 放弃该段 + UI 标注 ⇒ 计 `abandoned`（**唯一允许的内容损失**） |
| 用户 seek 跳过 | 丢弃待播句（**用户自己跳过的**）⇒ 计 `skipped_seek` |
| 上行音频丢块 | **暴露计数**（不再静默丢）；熔断（连续丢 5 块）保持 |

### 6.3 语言对预判（选目标语时提示）

用 M1 实测的 k 表，在用户选目标语时**预先提示**：

> 「中译英的译音比原话长约 15–30%，当前单卡产能 1.02× ⇒ 译音会**逐渐落后**（内容不会丢）。」

---

## 7. 前端交互（按源变化）

### 7.1 面板

| 元素 | 行为 |
|---|---|
| 切句档位 | 快 / 稳，**随时可切** |
| 播放时机 | 跟播放 / 不等播放 —— **board / browser 置灰**（无播放头） |
| 目标语言 | 复用 `#asr-tr-target` ⇒ **不可出声的 27 种自动降级为仅字幕** + 面板说明 |
| 音色 | 从 `GET /api/tts/voices` 取（6 个参考音色） |
| 音量 | 原生 `<input type=range>` |
| 状态行 | `正在播报 · 滞后 3.2 秒 · 队列 2 段 · 变速 1.05` |

### 7.2 各源的默认与提示

| 源 | 默认播放时机 | 面板提示 |
|---|---|---|
| board | `now` | 「实时流：说完一句即译即播」 |
| browser | `now` | 同上 |
| file | `follow` | 「译文跟着音频播放头走；若未播放，译文不会出声（可切「不等播放」）」 |
| video | `follow` | 「译文跟着画面走」 |

### 7.3 file 源的播放头坑（缓解）

1. 「开始识别」时**自动播放**音频（新增的 `#asr-audio-player`，与 video 的 `_playVideoIfAny()` 对称）；
2. 播放头 3 s 不动 ⇒ 面板提示「点播放键让译文跟上」+ 一键「不等播放」；
3. 用户暂停 ⇒ 译文自动停（`follow` 下播放头不动 ⇒ 不放行）；
4. **浏览器自动播放策略**：自动播放可能被拦（`play()` 返回 rejected）⇒ 捕获并提示「点播放键开始」；
5. **兜底**：file 源也允许直接选「不等播放」⇒ 此时译文**不依赖播放头**（等价实时流的 `now`）⇒ 播放头缺失不再是死结。

### 7.4 与「实时翻译」的关系

- 两个开关**独立**；**同传开启时字幕翻译让路**（卡 0 只有一个 MT 实例，DD-10）。
- 同传关闭 ⇒ 字幕翻译恢复原行为。

---

## 8. 边界与错误处理

| 情况 | 表现 |
|---|---|
| TTS 实例没跑 / 插件没装 | 同传**不可用**：面板显示「TTS 未启用」+ 「启用 TTS」按钮 |
| 目标语不可出声（泰语等 27 种） | **自动降级为仅字幕** + 面板说明 |
| 源语 == 目标语 | 默认**不朗读**（可选项「朗读原文」） |
| 翻译模型没跑 | 同传**降级为仅字幕** + 明确提示 |
| MT 卡住（>20 s） | 翻译超时 ⇒ 该句标「未翻译」⇒ **不阻塞后续句**（`SENTENCE_TIMEOUT_S = 20`） |
| 播放头长时间不动（file 源） | 面板提示 + 提供「不等播放」 |
| 用户 seek | 清掉跳过的待播句 ⇒ 计 `skipped_seek` |
| 板载扬声器被占 | `AplayWriter` 起不来 ⇒ 日志告警 + 面板提示 |
| 回声自激 | 浏览器麦在远端 ⇒ 衰减大；面板**常驻提示** |

---

## 9. 风险与未决项

| # | 风险 | 影响 | 应对 |
|---|---|---|---|
| **R1** | **卡 0 上 ASR 与 MT 同卡争用**（两个 llm_mode 8 核上下文 ⇒ 时间片轮转） | MT 翻译拖慢 ASR 轮次 ⇒ 实时源滞后变大；文件源识别变慢 ⇒ 译文备得晚 | **S0-M6 必测**；劣化明显 ⇒ 给 MT 限速给 ASR 让路 |
| **R2** | sox `play` 在板载 ES8388 上未验证 | 变速路径可能不出声 | S0-M7；保留 `aplay` 分支回退 |
| **R3** | **file 源今天没有播放器**（要新增 `#asr-audio-player`）；且播放头可能不动 | `follow` 模式不出声 | S3 第一件事加播放器；§7.3 五条缓解 |
| **R4** | 回声自激 | 用户听到自己被重复 | 提示 + 建议远置麦/耳机 |
| **R5** | 麦缓冲 2→20 块 | 前端内存 + 上行压力 | 10 s × 16 kHz Int16 ≈ **320 KB**，可接受 |
| **R6** | 单卡 1.02× ⇒ 中译英必然滞后增长 | 用户体验 | 原则①要求不丢 ⇒ 滞后是允许的代价；`tempo` 覆盖 k ≤ 1.17，其余如实显示 |
| **R7** | k 表未实测 | tempo 阈值是猜的 | S0-M1 |
| **R8** | 同传与字幕翻译共用一个 MT 实例 | 互相拖慢 | R1 的应对；必要时同传时暂停字幕翻译 |

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

### S0（只测不写）→ 四张数据表
M6 / M7 / M8 / M1 的结果 + 结论 ⇒ 若 M6 不过 ⇒ 部署形态要改（**回到用户**重新决策）。

### S1（TTS 队列 + 变速）
| 验收 | 判据 |
|---|---|
| 提交 5 句 | 依次播完，顺序不乱 |
| 队列深度 | `status` 的深度/积压秒数正确 |
| `tempo=1.15` | 播放时长缩短 ~13%，音高不变（M7 已验） |
| `save_wav=false` | `/userdata/test/tts` **不新增文件** |
| 播放不阻塞合成 | 合成线程不因 aplay 停顿 |

### S2（同传引擎 + 实时源 board / browser）
| 验收 | 判据 |
|---|---|
| 麦 → 板载出声 | 快档 ≤ 8 s / 稳档 ≤ 12 s 听到第一句 |
| 一句不丢 | 连续讲 60 s ⇒ `played` 句数 == 切句数 |
| 停止 | ≤ 2 s 停止播放 |
| 上行丢块 | 正常语速下 `dropped_src = 0` |

### S3（文件 / 视频源）
| 验收 | 判据 |
|---|---|
| 视频跟播 | 放视频 ⇒ 译文跟着画面走，**不跑到画面前面** |
| 跟播滞后 | 稳定在 3–4 s（恒定），不随时间增长 |
| seek | 拖动进度条 ⇒ 跳过的那段不播，`skipped_seek` 计数正确 |
| 暂停 | 暂停视频 ⇒ 译文自动停；继续 ⇒ 恢复 |
| file 源 | **新增播放器**播放时译文同步出声；不播时给出提示；「不等播放」也可用 |
| 快/稳对比 | 快档句子更早出声（滞后更小）、稳档句子更完整 |

---

## 11. 回归

**新增**（放 `scripts/`，命名对齐现有 `verify_asr_*`）：

| 脚本 | 断言 |
|---|---|
| `verify_interp_segmenter.py` | 快档切句器：LCP 连续 N 轮不变才成句；模型改写 ⇒ 作废候选；长度上下限 |
| `verify_interp_gate.py` | 放行器：`now` 直放；`follow` 等播放头；提前量；**seek 回退 ⇒ 清待播** |
| `verify_interp_recheck.py` | 播前复核：一致⇒提交；不一致⇒重译；重试超限⇒放弃并计数 |
| `verify_interp_playhead.py` | 播放头上报路由：按 `stream_id` 维护；`playing=false` 不放行 |
| `verify_tts_queue.py` | 队列：顺序、深度、合并、clear、tempo 动态改（未开始的才改） |
| `verify_interp_api.py` | 5 个路由的入参校验、状态字段、降级路径（无 TTS / 不可出声语种） |

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
| 4 | 插件改动 | `.venv/src/wdnapps-audio-chat/`（S1 起） |
| 5 | 宿主改动 | `app/core/audio/tts_bridge.py`（S1 起） |
| 6 | 回归脚本 | `scripts/verify_interp_*.py`、`scripts/verify_tts_queue.py` |

---

*附：本文的所有板端数字来自本机实测（`tools/probes/tts_same_card/`、`/api/compute`、`rknnsmi` 遥测），非厂商宣传值。**M1 / M6 / M7 / M8 尚未实测**，方案里凡依赖它们的数值都标了「待实测」。*
