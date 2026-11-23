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
- `tempo ≤ 1.15` ⇒ 覆盖 k ≈ 1.17（**S0-M1 实测的最坏值就是意大利语 1.17** ⇒ 刚好压住）。
- **不变调**（sox `tempo` 是时间域算法，保持音高 —— S0-M7 已用 FFT 峰值 440 Hz 验证）。
- 每次变速都记进指标，UI 如实显示「当前提速 1.05×」。
- ⚠️ 变速**必须走 `sox → aplay` 两段管道**（sox 14.4.2 的 `play` 没法选设备 ⇒ 见 §9.1 M7）。
- ⚠️ **tempo 是「队列级」设置** ⇒ `submit_queued(tempo=None)` **继承队列当前变速**，
  显式给数值才覆盖这一句。
  ⇒ 之前新句一律用调用方传的默认 1.0 ⇒ 「设了 tempo 1.15，新句还是原速播」
  ⇒ **自动提速等于没提速**（实测：journal 里 `aplay-writer` 没带 `tempo=1.150`）。
  ⇒ 已修（宿主桥 / 插件 API / 队列三处都改成 `tempo=None` = 继承）。

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

用 M1 **实测**的 k 表（§9.1），在用户选目标语时**预先提示**：

> 「中译英的译音比原话长约 15–30%，当前单卡产能 1.02× ⇒ 译音会**逐渐落后**（内容不会丢）。」

实测 k 表（S0-M1，源句 5.77 s 英文）⇒ 提示按**目标语**给，别用一句话概括：

| k 区间 | 目标语 | 提示 |
|---|---|---|
| ≤ 0.96 | german 0.61 / japanese 0.90 / chinese 0.93 / french 0.95 | 「译音比原话**短**，不会积压」 |
| 0.97 ~ 1.06 | portuguese 0.98 / korean 1.05 | 「译音与原话**差不多**，不会有明显滞后」 |
| > 1.06 | spanish 1.10 / **italian 1.17** | 「译音比原话长 **10–17%** ⇒ 必要时提速到 1.15×（内容不会丢）」 |

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
| **R1** | **卡 0 上 ASR 与 MT 同卡争用**（两个 llm_mode 8 核上下文 ⇒ 时间片轮转） | MT 翻译拖慢 ASR 轮次 ⇒ 实时源滞后变大；文件源识别变慢 ⇒ 译文备得晚 | ✅ **S0-M6 已测：同卡可撑**（见 §9.1）⇒ 不需要给 MT 限速 |
| **R2** | sox `play` 在板载 ES8388 上未验证 | 变速路径可能不出声 | ✅ **S0-M7 已测：出声正常** ⇒ 但**必须用 `sox → aplay` 两段管道**（见 §9.1） |
| **R3** | **file 源今天没有播放器**（要新增 `#asr-audio-player`）；且播放头可能不动 | `follow` 模式不出声 | ✅ S3 已加播放器（不复用向导的 `#asr-preview-audio`）；§7.3 五条缓解 |
| **R4** | 回声自激 | 用户听到自己被重复 | 提示 + 建议远置麦/耳机 |
| **R5** | 麦缓冲 2→20 块 | 前端内存 + 上行压力 | 10 s × 16 kHz Int16 ≈ **320 KB**，可接受 |
| **R6** | 单卡 1.02× ⇒ 中译英必然滞后增长 | 用户体验 | 原则①要求不丢 ⇒ 滞后是允许的代价；`tempo` 覆盖 k ≤ 1.17，其余如实显示 |
| **R7** | k 表未实测 | tempo 阈值是猜的 | ✅ **S0-M1 已测：k ≈ 0.61~1.17**（见 §9.1）⇒ `tempo=1.15` 全压得住 |
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

## 9.1 S0 实测结果（2026-11-21，板端实测）

> 三项已测完，**结论比方案里的估算乐观**；M8 并入浏览器端到端回归一起跑。

### M6 —— 卡 0 同卡争用：✅ **通过（同卡可撑，部署形态成立）**

两轮都跑在同一张卡 0（`core_cnt=8`）上，**实时路径**（浏览器麦上行 → 实时识别）：

| 阶段 | 轮次数 | 识别 lag 中位 | 识别 lag 最大 | **丢块** | 并发 MT 请求数 |
|---|---|---|---|---|---|
| 基线（只有 ASR） | 23 | 195 ms | 395 ms | **0** | 0 |
| 争用（ASR + 灌满 MT 队列） | 23 | 196 ms | 365 ms | **0** | **3918** |

* 灌了 3918 个 MT 请求（把翻译队列压到爆）⇒ **识别 lag 一点没涨、丢块仍是 0**。
* ⇒ **R1 关闭**：不需要给 MT 限速，「卡 0 = ASR + MT」的部署形态成立。
* 探针：`tools/probes/interp/m6_same_card_contention.py`、`m6b_live_contention.py`
  （结果：`/userdata/assets/uploads/.m6_result.json`、`.m6b_result.json`）

### M7 —— sox 变速 + 板载 codec：✅ **出声正常（但管道形态要改）**

* `tempo` 精度：倍率 1.04 → 2.885 s、1.08 → 2.778 s、1.15 → 2.609 s（源 3.0 s）
  ⇒ **实测时长 = 源长 × 1/倍率，偏差 < 0.01%**；
* **音高不变**：FFT 峰值仍是 440 Hz（`tempo` 只改时长不改编调）；
* **确实打开板载 codec**：`/proc/asound/card1/pcm0p/sub0/hw_params`
  = `MMAP_INTERLEAVED` / `S24_LE` / 48 kHz / 2ch ⇒ 走的是 ES8323 声卡。

> ⚠️ **方案里的命令形态作废**：sox 14.4.2 的 `-D` 是 **`--no-dither`**（不是选设备）
> ⇒ `sox play` 只能用**默认设备** ⇒ 变速**必须**走 **`sox → aplay` 两段管道**：
>
> ```
> sox -t raw -e floating-point -b 32 -L -r 24000 -c 1 <in> -t raw -e floating-point -b 32 -L -r 24000 -c 1 - tempo 1.15 | aplay -q -t raw -f FLOAT_LE -r 24000 -c 1
> ```
>
> ⚠️ sox 的**效果参数放在输出文件之后**（`sox in.wav ... - tempo 1.1`）；
> 读 stdin 写 stdout 时**输入输出各要一份格式参数**。
> ⇒ `cards/tts/service.py` 的 `AplayWriter` 因此分两条路：
> `tempo=1.0` 走原 aplay 路径（**一字不改**），`tempo>1.001` 才走两段管道。
> 探针：`tools/probes/interp/m7_sox_tempo.py`

### M1 —— 语言对膨胀比 k 表：✅ **k ≈ 0.61~1.17（`tempo=1.15` 全压得住）**
源句取同一句英文（5.77 s，最长句）⇒ 翻成 8 种目标语后量译音时长：

| 目标语 | chinese | german | italian | portuguese | spanish | japanese | korean | french |
|---|---|---|---|---|---|---|---|---|
| **k** | 0.93 | **0.61** | **1.17** | 0.98 | 1.10 | 0.90 | 1.05 | 0.95 |

* english = 同语透传（不测）；russian 偶发翻译失败（未取样）。
* ⇒ **比方案里的估算乐观**：最坏 1.17（意大利语）也**刚好被 `tempo=1.15` 压住**
  （1.17 / 1.15 ≈ 1.017 ⇒ 播放端仍略慢于源 ⇒ 滞后会缓慢累积，但**不丢句**）。
* ⇒ `tempo` 阶梯（1.0 / 1.04 / 1.08 / 1.15）**够用**；
  语言对预判文案按上表写（德语/日语/中文/法语 ≤ 0.96 ⇒ 无需提速）。
* 探针：`tools/probes/interp/m1_lang_expansion.py`
  （结果：`/userdata/assets/uploads/.m1_k_table.json`）

> ⚠️ M1 探针的坑：翻译帧按 `seq` 匹配 ⇒ 旧帧会被误取 ⇒ **必须用高基数唯一 seq**；
> 翻译是异步的 ⇒ 收到 `done` 帧后还要**等翻译队列排空**再收帧；
> 模型偶发退化会把英文音频转成中文 ⇒ 源句要**含 ASCII 字母**校验，并取**最长**的句子。

### M8 —— 麦上行丢块基线：✅ **0 丢块（基线与满载争用都是 0）**

M6b 的实时探针走的就是**浏览器麦的那条上行**（`POST /api/qwen3-asr/live/chunk`，
响应里带 `dropped_chunks`）⇒ 顺带拿到 M8 的基线：

| 阶段 | 送入块数 | **丢块** | 识别 lag 中位 | lag 最大 |
|---|---|---|---|---|
| 基线 | 25 | **0** | 195 ms | 395 ms |
| 争用（同时灌 3918 个 MT 请求） | 25 | **0** | 196 ms | 365 ms |

⇒ **正常语速下 `dropped_src = 0`** ⇒ 上行不丢块（`dropped_src` 计数在同传面板上可见）。
探针：`tools/probes/interp/m6b_live_contention.py`（结果 `.m6b_result.json`）

### M9 —— **浏览器麦上行 → 同传** 端到端：✅ 两个档位都通

S3 的端到端只验过**文件源**；用户最关心的是**实时流** ⇒ 走麦上行那条链路
（`POST /api/qwen3-asr/live/chunk`）把真实音频灌进去：

| 档位 | 切句 | 翻译 | 进 TTS 队列 | 上行丢块 | 识别 lag 最大 |
|---|---|---|---|---|---|
| **快档**（`unfixed` 稳定前缀成句） | 1 | 1 | 1 | **0** | 273 ms |
| **稳档**（endpoint 成句） | 1 | 1 | 1 | **0** | 225 ms |

* ⚠️ 探针里特意用「**说完就停**」（`live/finish`，**不产生 endpoint**）收尾
  ⇒ 验的就是「最后一句也不丢」⇒ **稳档的收尾钩子必须补这一下**
  （不补 ⇒ 那句话永远不成句 ⇒ 直接违反「一句话都不能丢」）。
* 探针：`tools/probes/interp/m9_live_interp.py`

### M10 —— **TTS 卡的变速管道**真机：✅ 倍率 1.148 / 板载 codec 打开

M7 只验了 sox 本身 ⇒ M10 验 **TTS 卡里真正的接线**
（`AplayWriter(tempo=1.15)` ⇒ `sox → aplay` 两段管道）：

* 播放时长用**两种长度各测一次、拟合斜率**（把进程启动的固定开销消掉）：
  **实测倍率 1.1481（期望 1.15，偏差 0.2%）**；
* 播放期间轮询 `/proc/asound/card1/pcm0p/sub0/hw_params`
  = `MMAP_INTERLEAVED / S24_LE / 2ch / 48000` ⇒ **板载 ES8328 codec 真的被打开**；
* 音高不变（sox `tempo` 是时间域算法；M7 已用 FFT 峰值 440 Hz 验过）。
* 探针：`tools/probes/interp/m10_tempo_pipeline.py`

### M11 / M12 —— **稳档实时流的滞后时间线**（用户报「滞后 18.5 秒」的第一现场）

探针：`tools/probes/interp/m11_steady_lag.py`（文件源）/
`tools/probes/interp/m12_live_lag.py`（实时流）。

**结论：18.5 秒不是「一个原因」，是三个原因叠在一起**（见下表）：

| # | 现象 | 真因 | 修法 |
|---|---|---|---|
| ① | **稳档在连续讲话时永远不成句** | 稳档把「一句话结束」押在 **endpoint（静音 1200 ms）**上 ⇒ 连续讲话时**永不触发** ⇒ 译文要等整段结束 | 稳档改**标点断句**（`_SentenceSplitter`，实时流与文件源同一套）；endpoint / finish 只 flush **尾巴**，不交整段 |
| ② | **文件源的滞后恒显示 18.48 秒** | 文件源提交的 job `src_end_ts=0.0` ⇒ 队列**不记滞后** ⇒ 面板显示的是**上一轮的残留值** | `src_end_ts` 一律用 `seg.created_ts` |
| ③ | **实时流越播越落后** | TTS 队列吞吐只有 **0.67× 实时**（见 M13） | **两段流水线**（见 M14） |

> ⚠️ ① 的隐蔽性：endpoint 分支只在**静音 1200 ms** 后触发 ⇒ 讲 presentations
> 时一句都切不出来，而日志里**没有任何异常** ⇒ 看起来像「翻译慢」。

### M13 —— **TTS 队列的真实吞吐**：⚠️ **0.67× 实时**（落后 ASR+翻译的根因）

探针：`tools/probes/interp/m13_tts_throughput.py`
（4 句 · 估算音频 26.6 s · 墙钟 39.3 s ⇒ **0.67×**）。

拆开看（`tts-timing` 日志 + worker 侧 `[talker+decoder]`）：

| 环节 | 实测 | 判读 |
|---|---|---|
| 合成（worker 侧） | `84 frames, 6.67s of audio in 6.20s` | **RTF ≈ 0.93** ⇒ 合成不是瓶颈 |
| 播放（aplay） | 与合成**重叠** | 1× 实时 |
| **每句的固定开销** | `infer_done 6.36s` → `audio_drained 10.29s` | **3.93 s 白等** |

**3.93 秒从哪来？** ⇒ **Qwen3-TTS 的首窗延迟**：

* decoder 的**首窗要 50 帧**（`kWindowLen = kChunkSize + kLeftContextSize = 50`
  ⇒ 50 × 1920 / 24000 = **4 秒音频**）⇒ talker 得先吐 50 个语义 token
  才出第一块 PCM ⇒ **首窗延迟 ≈ 3.7 秒**（实测 `first_pcm=3.7~4.0s`）；
* 而原来的队列是「**合成 + 播放焊死在一句**」（`PLAY_BOTH`：PCM 一到就进
  aplay）⇒ **每句**都要付这个 3.7 秒 ⇒ 吞吐 = 6.6 / (6.6+3.7) = **0.64×**。

⇒ **这就是「稳档非常慢、越播越落后」的物理原因**（不是翻译慢）。

### M14 —— **两段流水线**（合成线程 + 播放线程）：✅ **吞吐 0.67× → 0.93×，滞后不再增长**

改法（`cards/tts/queue.py` + `cards/tts/service.py` + 宿主桥）：

* 新增 `play_to="queue"`（宿主桥 `PLAY_QUEUE`）：**会话只产出 PCM，不自己播**；
* 队列拆成**两个线程**：
  * **合成线程**：一次一句，串行；每合成完一句就交给播放线程；
  * **播放线程**：按 FIFO 播；**第一块 PCM 到了就起 aplay**（不等整句合成完），
    后面几句的 PCM 先缓存在自己的缓冲里；
* ⇒ 下一句的合成与这一句的播放**重叠** ⇒ **首窗延迟从「每句」摊成「每轮一次」**。

实测（M13 重测 + M12 重测）：

| 指标 | 改前 | 改后 |
|---|---|---|
| TTS 队列吞吐 | **0.67×** | **0.93×**（首窗那 3.7 s 只付一次） |
| 句子之间的空档 | 每句 ~3.7 s | **0 s**（journal：连续 `aplay-writer` 之间无空档） |
| 实时流 25 s 音频的滞后 | 8.2 → 13.3 → 16.4 → **23.2 s（持续增长）** | 7.8 → 9.4 → 9.1 → **12.6 s（收敛）** |
| 队列积压 | 越积越多（`qdepth` 2~3） | **0~1**（播完即清） |

> ⚠️ 剩下的 **12.6 秒**是**结构性的**、不是积压：
> ASR 切句 ~1 s + 翻译 ~1 s + **TTS 首窗 3.7 s** + 说话本身 ~4.6 s。
> 其中只有「TTS 首窗 3.7 s」可以再压（要改 **wdnai** 的 decoder 首窗，
> 属于另一个仓 ⇒ 本期**不动**）。
>
> ⇒ 想再快就得**提速**（`tempo`）：队列有积压时自动提速（`TEMPO_STEPS`
> ⇒ 最高 1.15），或者手动 `POST /api/tts/queue/tempo`。

### M15 —— **音色不生效**（用户报「每句话音色都不同」）

* 根因：本板（Base 权重）**只有 6 个参考音色**（`kind: "ref"`），
  必须走 SDK 的 **`ref_speaker`** 参数；而当时一路透传到了 **`speaker`**
  （内置音色 SPK_ID）⇒ worker 日志刷
  `[speaker] 'baby_girl' not in SPK_ID, skipping` ⇒ **没有音色条件**
  ⇒ 模型每次自由发挥 ⇒ **每句话音色都不一样**；
* 修法：**参考 / 内置分流** —— `kind=="ref"` ⇒ `ref_speaker`，
  `kind=="builtin"` ⇒ `speaker`，**绝不可混用**；
  （队列 `TTSJob.ref_speaker` → 宿主桥 → 插件 API → 引擎 `_ref_speaker`
  → 前端下拉按 `dataset.kind` 分流）；
* 验证：worker 日志变成
  `[speaker] using voice-clone ref_speaker='news_female'` ✓；
  `verify_tts_voices_real`（真机 F0 稳定性）**18/18** ✓。

### M16 —— **「开始同传」会把已选音色抹掉**（M15 的兄弟坑）

* 现象：先用面板选了音色（`POST /interp/settings`），
  再点「开始同传」（`POST /interp/start`）⇒ **音色变回空** ⇒
  worker 日志里**没有** `[speaker] using voice-clone` 那行 ⇒
  每句都自由发挥；
* 根因：`interp/start` 写的是 `ref_speaker=data.ref_speaker or ""`
  ⇒ **payload 没带这个字段时 = 清空**（把「没传」当成了「清空」）；
* 修法：`start` 的音色 / 音量改成 **`Optional`**，**传了才覆盖**
  ⇒ start 的语义是「按面板当前状态起」，**不是**「重置成默认」；
* 回归：`verify_interp_api` 加了 4 条断言（**49 → 53**）。

### M17 —— **同传对「本机 MT 实例」的硬依赖**（冷启动会整段哑掉）

* 现象：字幕翻译的开关（`POST /api/qwen3-asr/translate/disable`）会
  **停掉本机 MT 实例** ⇒ 板上只剩 **peer 的** MT ⇒ 同传立刻报
  「NPU 翻译未启用（swcap_translate 没有正在运行的实例）」⇒
  **一句都译不出来**；
* 根因：`npu_manager.sessions()` 用 `list_sessions(include_remote=False)`
  ⇒ **只认本机实例**（对端 MT 不算 —— 「每块板子只管自己的算力」）；
* 修法（同传自己兜底）：
  * `interp/start` ⇒ **后台**调 `npu_manager.ensure()`（**本机** worker，
    `allow_adopt=False`）⇒ 不挡 HTTP 响应（模型加载 ~30 s）；
  * 翻译那一侧（`_handle`）**等 MT 就绪**（≤45 s）再翻 ⇒ 冷启动那 30 s
    里产生的句子**不丢**（不等 ⇒ 立刻失败 ⇒ 整段同传哑掉）；
  * 翻译中途 MT 被停掉 ⇒ 识别「翻译未启用」⇒ 重新拉起 + 重试这一句；
* 验证：手动停掉本机 MT 后跑文件源 ⇒ **4 句全部译出并播完**
  （`done=4 failed=0`），冷启动期间**零丢句**。

### M18 —— **「有翻译，但是没有播放」**（用户实测，2026-11-21）

用户反馈：**浏览器里跑音频文件源，译文出得来，但板子一声不响。**

#### 现场证据（服务端日志 + 状态）

| 观测 | 数值 | 说明 |
|---|---|---|
| `segments` / `translated` | 16 / 16 | 切句 + 翻译**都正常** |
| `submitted` | **0**（用户那轮） | **一句都没进 TTS 队列** ⇒ 队列根本没收到东西 |
| `pending` | **8** | 8 句**卡在放行器**里 |
| `interp/playhead` 请求 | 只上报过 **1 次** | 播放头**再没动过** |
| `/api/qwen3-asr/media` 请求 | 用户那轮**一次都没有** | 媒体元素要么没源，要么用 blob URL |

⇒ **播放头不动 ⇒ 「跟播放」把句子全部压住 ⇒ 一声不响。**

#### 三层根因（都修了）

1. **播放头的 `stream_id` 对不上**（**主因**）
   * 前端上报播放头时带的是 `this._interp.streamId` ⇒ **这个字段从来没被
     赋值**（一直是 `""`）⇒ 播放头存进了 `""` 这个键；
   * 而句子用的是服务端的 `file-{rid}#0` ⇒ **配不上** ⇒
     `Gate.admit()` 的回退分支 `if ph is None and self._last_stream:` 里
     `self._last_stream` 也是 `""`（**假值**）⇒ **回退根本没触发**；
   * ⇒ 修法：放行器记一份「**最近一次上报的播放头**」（不分流）⇒
     句子配不上流 id 时用它兜底；前端也改成上报**引擎当前的流 id**
     （`/interp/status` 新增 `stream_id`）。

2. **自动播放发生在 `await fetch()` 之后** ⇒ 用户手势（transient
   activation）已经失效 ⇒ `el.play()` 被浏览器策略**拒绝** ⇒ 媒体一直
   暂停 ⇒ 播放头永远 0；
   * ⇒ 修法：`_interpAutoPlay()` 挪到 `_interpStart()` 的**最前面
     （同步执行）** ⇒ 落在「开始同传」这个**点击手势**的同一个调用栈里；
   * 再补一次「`loadeddata`/`canplay` 后重试」（媒体后到时也能播）。

3. **放行器会无限期压住句子**
   * ⇒ 修法：**卡住兜底** —— 媒体**报的是暂停** + 播放头**从没真正走过**
     （自动播放被拦的指纹）+ 卡住 > `FOLLOW_STALL_SEC`（10 s）⇒ 放行；
   * ⚠️ **区分「用户主动暂停」**：播放头**曾经走过** ⇒ 译文跟着停是对的
     ⇒ **不兜底**；媒体**还在播**（音轨晚开始）⇒ 播放头迟早会动 ⇒ **不兜底**；
   * 另加 `playhead_stalled` 状态 ⇒ 面板**显性提示**「媒体没在放，N 段压着
     没播 · 点播放键，或改成「不等播放」」。

> ⚠️ **既有回归的盲区**：``verify_interp_browser.py`` 原来用 headless
> Chromium 的**默认自动播放策略**（宽松）⇒ 媒体**总能**自动播 ⇒
> 这条路径**测不出来**。⇒ 现在回归改用
> ``--autoplay-policy=user-gesture-required``（真实桌面浏览器的默认行为），
> 并新增两条断言：「开始同传 ⇒ 媒体真的开始播了」+「播放头在走」。

#### 修完的实测

| 项 | 修前 | 修后 |
|---|---|---|
| 浏览器回归「有句子送进 TTS 队列」 | ❌ `submitted=0`（pending=4） | ✅ **`submitted=2`** |
| 自动播放（严格策略下） | 媒体不动 | ✅ 媒体在播、`currentTime` 在涨 |
| 播放头 | 只上报 1 次 | 持续上报 |



### M19 —— **「有翻译，但是没有播放」的第四层**：`Gate` 记账**跨会话残留**（2026-11-21）

M18 的三层根因修完后，「兜底放行」这条**安全网**在真机上**测不出来**
⇒ 顺着探针挖出了两个新问题。

#### 探针本身的设计缺陷（先排除「是测试的问题」）

`m14_browser_media.py` 第五步「模拟媒体一直暂停」：

```
t=20s currentTime=0.00 paused=True | stalled=True pending=0 sub=3 兜底=0
  ⇒ ❌ 兜底没生效（句子还被压着）
```

⇒ `stalled=True`（告警正常）但 `pending=0`、`submitted` 没涨 ⇒
**根本没有句子** ⇒ 无从放行。

> ⚠️ **根因是探针没跑识别**：它只点了「开始同传」+ 把媒体按停，
> 页面**从没跑过 `/transcribe`** ⇒ 没有句子产生 ⇒
> `Gate.admit()` 一次都没被调用 ⇒ 兜底当然不触发。
> ⇒ 探针补上「开始识别」，并改成**两场**（手势能播 / 自动播放被拦）。

⇒ **教训**：验证「放行器」的兜底，必须先**造出句子**。

#### 真 bug ①：`Gate._ph_moved` 跨会话残留 ⇒ 兜底**永不触发**

`_ph_moved`（播放头**曾经真的走过**）是区分

* 「自动播放被拦」（媒体**从没播过**），
* 「用户主动暂停」（播过再停）

的分界线。而 M18 的兜底条件是
`never_played and playing_now is False and 卡住 >= 10 s`。

⇒ **跨会话残留**的后果：

| 上一轮 | 这一轮 | 结果 |
|---|---|---|
| 媒体**播过**（`_ph_moved=True`） | 自动播放被拦（媒体从没播过） | `never_played=False` ⇒ **兜底永不触发** ⇒ 句子被**永久压住** ⇒ **有翻译、一声不响** |
| 媒体**从没播过** | 用户**主动暂停** | 兜底**误触发** ⇒ 用户按了暂停，译文还在念 |

⇒ **用户报的现象在「先正常播过一次、再被拦一次」之后必然复现** ——
而这正是**真实使用序列**（试一次能播 ⇒ 换个浏览器/换��页 ⇒ 被拦）。

⇒ **修法**：`Gate.reset()`（清 `_playhead` / `_playing` / `_last_ph` /
`_ph_at` / `_ph_moved` / `_stalled_released`），在
**每次起新会话**（`InterpEngine.start()` 且原先不在跑）时调用。

#### 真 bug ②：「一次播放头都**没上报过**」⇒ 兜底也不触发

`playing_now` 之前是 `self._last_ph[1] if self._last_ph else None`
⇒ **没上报过** ⇒ `None` ⇒ 兜底条件里的 `playing_now is False` 不成立
⇒ 而 `_ph_at` 是 0 ⇒ 「卡住多久」永远是 0 ⇒ **两个条件都不成立**
⇒ 句子被**无限期**压住，且面板**一声不响**（`stalled()` 之前要求
`playing is False` ⇒ `None` 不算 ⇒ 连告警都不报）。

⇒ **修法**：

* 新增 `_armed_at`（跟播计时的起点）⇒ **没上报过**时用它当「卡住多久」
  的基准；
* `stalled()` / `admit()` 的判据从 `playing is False` 放宽成
  **`playing is not True`**（`None` = 「没在放」）——
  ⚠️ 「媒体还在播（音轨晚开始）」仍是 `True` ⇒ **不兜底**，语义不变。

#### 顺带把「为什么没声音」说得更清楚（前端）

`_interpAutoPlay()` 里 `play()` 被拒时，除了写一句提示，还记一个
**旗标** `autoplayBlocked` ⇒ 面板每秒被 `_interpSyncUi()` 刷新的提示
**不再把它冲掉** ⇒ 用户一直能看到「浏览器拦了自动播放 —— 点媒体的播放键，
译文才会跟着走」；媒体一旦真的播起来，旗标自动清掉。

#### 修完的实测（真机浏览器，两场）

| 场景 | 修前 | 修后 |
|---|---|---|
| 第一场：有用户手势 ⇒ 媒体播起来 | — | ✅ `currentTime` 在涨，句子照播（`submitted=2`） |
| 第二场：**自动播放被拦** ⇒ 媒体不动 | ❌ 兜底永不触发（句子永久压住） | ✅ **10 s 后兜底放行** ⇒ `stalled_released` 涨、`submitted` 涨 ⇒ **译文仍然出声** |
| 面板提示 | 「译文跟着播放头走」（泛泛） | ✅ 「浏览器拦了自动播放 —— 点媒体的播放键，译文才会跟着走」 |
| 会话切换 | `_ph_moved` 残留 | ✅ `reset()` 清零（`playhead_moved` 状态可见） |

> ⚠️ **Playwright 的 `click()` 自带用户手势** ⇒ 即便开
> `--autoplay-policy=user-gesture-required`，`play()` 也会成功 ⇒
> 「自动播放被拦」这条路径在 headless 里**测不出来**
> ⇒ 回归第二场**直接把 `HTMLMediaElement.prototype.play` 改成拒绝**
> （等价于浏览器策略拦截）。

#### 顺带挖出的**第三个盲区**：计数是**累计**的 ⇒ 断言会**假通过**

`/interp/status` 的 `segments` / `translated` / `submitted` /
`stalled_released` 是**引擎生命周期内累计**的（跨会话不清零）⇒

* 回归里写 `check("有句子送进 TTS 队列", submitted > 0)` ⇒
  **上一轮跑过之后，这一轮哪怕一句都没送进去也会「通过」**；
* 探针同理 ⇒ m14 第一场打印的 `submitted=6` 其实全是**上一轮**的残留
  ⇒ 差点把「这一轮没送进去」当成正常。

⇒ **所有计数类断言都必须取基线、比增量**（回归与探针都已改）。
⇒ 顺带把 `segments` / `translated` 也改成增量断言。



### M20 —— **「有翻译，但是没有播放」的另一条线：默认音量 10% ⇒ 实际 **−24~−32 dBFS**（听不见）

M18 / M19 把**放行**这一层修通之后，「句子确实进了 TTS 队列」——
但**声音小到听不见**，用户还是会说「没有播放」。

#### 关键事实：`volume` 是**振幅倍率**，不是硬件音量

`AplayWriter.write()` 里就是一行：

```python
chunk = np.clip(np.asarray(audio, dtype=np.float32) * self._volume, -1.0, 1.0)
```

⚠️ 板载 codec 那边**已经是满档**了：

| 硬件 | 状态 |
|---|---|
| `amixer -c 1 sget PCM` | `192 [100%] [0.00dB]` |
| `amixer -c 1 sget Speaker` | `Playback [on]`（PA 已开） |

⇒ **唯一的衰减就是软件倍率**。

#### 板端实测（电平探针）

给 `AplayWriter` 加了**电平探针**（缩放前的原始峰值 + 缩放后真正送进
aplay 的峰值）⇒ 队列快照里多两个字段：`last_peak` / `last_raw_peak`。

| `volume` | 模型原始峰值 | **真正出声的峰值** | 电平 |
|---|---|---|---|
| 1.0 | 0.396 | 0.396 | −8 dBFS |
| 0.5（TTS 卡的默认） | 0.396 / 0.150 | **0.198 / 0.075** | **−14 dBFS** |
| **0.1（同传原来的默认）** | 0.247 | **0.025** | **−32 dBFS** ❌ |
| 0.0 | 0.150 | 0.000 | 静音 ✔ |

⇒ **同传面板的默认 10% ⇒ −32 dBFS** ⇒ 板载小喇叭上**基本听不见**
⇒ 和「没播」在用户耳朵里**没区别**。

#### 修法

1. **默认音量 0.1 → 0.5**（与 TTS 卡自己的默认值对齐 ⇒ 板上已验证过的
   响度档）；同传引擎 / 宿主桥 / TTS 队列 / API 的默认值**统一**成 0.5；
2. **电平探针进面板**：状态行在峰值过低时显式写「（音量偏小：出声峰值
   0.02）」⇒ 用户能区分「**没播**」和「**播了但太小**」；
3. 顺手修掉 **`volume=0` 被当成 0.1** 的坑
   （`float(payload.get("volume", 0.1) or 0.1)` ⇒ 滑块拉到 0 静音会变成
   0.1）⇒ 改成显式的 `None` 判断。

> ⚠️ **模型自己的输出电平不归一化**（同一段文本峰值在 0.15~0.40 之间
> 波动）⇒ 固定倍率下不同句子的响度会有差异 ⇒ 这是**模型侧**的现状，
> 不在同传这一层解决（真要解决得做响度归一化，那是另一件事）。



### M21 —— **「只播放了第一句」**（用户实测，2026-11-22）

用户反馈：浏览器里跑音频文件源，**只听到第一句**，后面的译文没了。

#### 现场证据（探针 m16 / m17 的逐秒记录）

```
媒体: 15.1⏸⏹（播完了）  ph=15.05  stalled=true
切句 34 · 翻译 34 · **送进队列 28** · **待放行 6** ← 6 句卡住
（停机时 pending 被清掉 ⇒ 用户永远听不到这 6 句）
```

⇒ 句子**切出来了、也翻出来了**，卡在**放行器**这一环。

#### 根因：**媒体播完之后，句子被永久压住**

媒体播完 ⇒ `<audio>` 变成 `paused=true, ended=true, currentTime=停在时长`
⇒ 前端上报的 `playing` 变成 **false** ⇒ 放行器那条

```
if ph is not None and playing is not False and seg.src_start_sec >= 0:
    if ph >= (seg.src_start_sec - FOLLOW_LEAD_SEC): return True
```

**不再成立**（`playing is False`）⇒ 而兜底放行要求
「播放头**从没真正走过**」（自动播放被拦的指纹）⇒ 媒体**播过**
⇒ `never_played=False` ⇒ **兜底也不触发** ⇒

⇒ **媒体播完之后才识别出来的句子，被无限期压住。**

#### 为什么文件源必然踩到

| | 时长 |
|---|---|
| 源（媒体） | 15 s（浏览器按 1× 播） |
| ASR + 翻译出齐 | 十几秒（**板端 ASR ≈ 0.5~1× 实时**） |

⇒ 媒体 15 s 播完时，**大部分句子才刚识别出来** ⇒ 全部落在「媒体已结束」
之后 ⇒ 全部卡住 ⇒ **只有媒体还在播时出的那一句被播了** ⇒
**用户听到的只有第一句**。

（实时流没有这个问题：没有媒体、没有播放头，`gate` 恒为「不等播放」。）

#### 修法：**让「源结束了」这件事能被服务端知道**

1. **前端**上报播放头时**多带两个标志**：`ended`（`<audio>.ended`）与
   `failed`（媒体加载失败）⇒ 并且把它们纳入「变化才上报」的判据
   （否则「结束」这个状态可能不单独上报一次）；
2. **API**：`InterpPlayheadPayload` 加 `ended` / `failed`；
3. **放行器**：`Gate.update_playhead(..., ended, failed)` 记下
   「源不会再往前走」⇒ `admit()` **第一条判据**就是它 ⇒
   **直接放行**（源都结束了，没什么可等的）；
4. **状态**：新增 `source_done` ⇒ 面板提示「源已播完 · 剩下的译文正在
   补播（识别比播放慢时会晚一点）」。

⚠️ **「用户主动暂停」仍然压着**（`ended=false, failed=false`）⇒
译文跟着字幕停，这是**设计如此**（面板会提示「媒体没在放，N 段压着没播
· 点播放键，或改成「不等播放」」）。

#### 修完的实测（探针 m16，模拟「媒体播完 + 源已结束」）

```
媒体播完 ⇒ 4 句全部识别出来 ⇒ submitted 4 / segments 4 / pending 0
（且 stalled_released=0 ⇒ 走的是**正常通道**，不是兜底）
```



### M22 —— **「只播放了第一句」的现场**：媒体**声称在播**、播放头却**一动不动**（2026-11-22）

M21 修的是「媒体播完」。但用户那轮跑完还是只听到第一句 ⇒ 顺着
**服务端的访问日志**（用户的浏览器 IP 是 `172.16.9.18`）又挖出一层。

#### 现场证据（journal，不是猜的）

```
172.16.9.18  POST /api/qwen3-asr/interp/start     × 1
172.16.9.18  POST /api/qwen3-asr/interp/playhead   × 1   ← ★ 只有一次！
172.16.9.18  POST /api/qwen3-asr/transcribe         × 3
172.16.9.18  GET  /api/qwen3-asr/interp/status     × 很多（面板在刷）
```

⇒ 面板在刷（引擎在跑），但**播放头只上报过一次** ⇒
**媒体从头到尾没动过**（`currentTime` 一直是 0）。

而那一次上报的内容是 `(playhead_sec=0, playing=true)` ——
**媒体声称在播，播放头却冻在 0**。

#### 为什么会「只播第一句」

放行器当时的两条判据**都不成立**：

| 判据 | 用户那轮 | 结果 |
|---|---|---|
| 「播放头到达 ``src_start - 1 s``」 | 播放头永远是 0 ⇒ 只有 `src_start ≤ 1 s` 的**那一句**满足 | ⇒ **只放行了第一句** |
| 兜底「媒体报的是**暂停** + 从没走过 + 卡住 10 s」 | 媒体报的是 **playing=true** ⇒ 不成立 | ⇒ **兜底也不触发** |

⇒ 剩下的句子被**永久压住**，而且**面板一声不响**
（告警也是同一个错判据 ⇒ 不报）⇒ 用户只看到「就播了一句」。

#### 根因：判据用错了信号

放行器拿**媒体自己声称的状态**（`playing`）当健康信号 ⇒ 而**媒体会说谎**
（卡在缓冲 / 解码不动 / 服务端没喂上数据 ⇒ `paused=false` 但时间不走）。

⇒ **健康信号应该是「播放头有没有在动」**——那才是放行器真正依赖的东西。

#### 修法

1. **兜底放行**的判据改成：**播放头从没走过 + 冻住 ≥ `FOLLOW_STALL_SEC`**
   ⇒ **不再看 `playing`** ⇒ 媒体说在播也没用；
2. **`stalled()` 告警**同样改成「**播放头冻住**」⇒ 面板会明确提示
   「播放头卡住（媒体没在放 / 卡在缓冲），N 段译文压着没播」；
3. ⚠️ **「用户主动暂停」仍然压着**（播放头**曾经走过** ⇒ `moved=True`
   ⇒ 不兜底）——译文跟着字幕停，是设计如此；
4. ⚠️ **「音轨比画面晚开始」不受影响**：视频源的播放头就是
   `video.currentTime` ⇒ 它会往前走 ⇒ `moved=True` ⇒ 不兜底。

#### 顺带修掉的一个「时间倒流」

`app.js` 里点「开始识别」时会 `a.play()`（想借用户手势让媒体出声），
但 **HTML5 规定媒体 `ended` 之后再 `play()` 会从头重播** ⇒
播放头回到 0 ⇒「源已结束」被清掉 ⇒ 跟播放又得从头等 ⇒
**后面识别出来的句子又被压住**（第三场回归就是被它绊住的）。

⇒ 音频 / 视频两处都加了 `if (el.ended) return;`。
⇒ 源已经播完了 ⇒ 剩下的译文靠「源已结束 ⇒ 直接补播」那条路，
**不需要**把媒体倒回去重播。



### M23 —— **「只播放了第一句」的第三层**：本机**没有 TTS 实例** ⇒ 语音走了**对端板子**（2026-11-22）

前两层（M21 / M22）修完，用户那轮的现场仍然对不上 ⇒ 继续翻 journal，
发现了**配置层面**的一条链。

#### 现场证据

```
01:50:32  172.16.9.18  "DELETE /api/compute/active-app HTTP/1.1" 200 OK
01:59:59  compute_registry: boot: compute.autostart 声明了 1 个模型: ['qwen3-asr']
```

⇒ 01:50 用户浏览器点了一次「**回首页**」⇒
`DELETE /api/compute/active-app` ⇒
:meth:`ComputeRegistry.clear_active_app` **连 ``compute.autostart`` 一起清空**
（这是**设计如此**：autostart 只增不减会把 NPU 撑爆）。

⇒ 之后用户重新进卡，``PUT /api/compute/active-app`` 把**那张卡需要的模型**
增量写回 autostart ⇒ 加回了 `qwen3-asr` + `hy-mt-1-5`
⇒ **但没加回 `qwen3-tts`**（用户没进过 TTS 卡）⇒

⇒ 01:59 重启时 autostart **只剩 1 条** ⇒ **本机没有 TTS worker**
⇒ registry 只能 **auto-adopt 对端**：

```
compute_registry: probe: qwen3-tts unreachable after 3 attempts; giving up
compute_registry: auto-adopt peer qwen3-tts: retry 5/5 in 32.0s
```

⇒ 同传的语音**从对端板子走网络回来**（PCM 回传）⇒ 延迟大、还可能失败
⇒ 听起来就是「**只播了一句**」。

#### 修法（两层）

1. **运行态**：把 `qwen3-tts` 写回 ``compute.autostart``（卡 1）并拉起本机
   实例 ⇒ 重启后三个 worker 都回来了。
2. **代码**：同传 ``start`` 时**顺手确保本机 TTS**（跟 MT 一个套路）——
   `InterpEngine._ensure_tts_model()`：
   * 本机已有能出声的实例 ⇒ 够了；
   * 本机有一个**正在加载** ⇒ 等它（别再起一份抢显存）；
   * 都没有 ⇒ 挑一张**不是感知链那张**的卡，起一个**本机** worker
     （``allow_adopt=False`` ⇒ 不许拿对端的）；
   * 状态里新增 `tts_local_ready` / `tts_waiting` / `tts_error`。
   ⇒ **「回首页」清掉 autostart 之后，同传依然能自己把本机 TTS 拉起来。**

⚠️ **没有动 `clear_active_app` 的语义** —— 它是有意为之（autostart 只增不减
会把 NPU 撑爆，见该方法的 docstring）。⇒ 修的是「**同传不依赖 autostart
里有没有 TTS**」。

---

### M24 —— **「ASR+翻译都结束了，同声传译才播放出声音，显示滞后 15 秒」**（2026-11-22）

> 用户原话：「ASR+翻译都结束了，同声传译才播放出声音，显示滞后了15s，请分析链路上
> 每部分的时间明细，并输出报告给我，其它仓库如果涉及到也可以允许改动」

#### 0. 结论先说

| # | 根因 | 性质 | 状态 |
|---|---|---|---|
| ① | **重复点「开始识别」⇒ 同一批句子被反复入队** —— 每一轮识别都把**同一个文件再转写一遍**、**同一批句子再灌一次 TTS 队列**（前端清了显示，后端没清队列）⇒ 队列里堆了 **3.5 倍**的音频 ⇒ **译文越播越落后**，面板显示「滞后 13.9 秒」 | **真 bug（用户这轮的直接原因）** | ✅ 已修 |
| ② | **面板的「滞后」在文件源下量错了基准** —— 拿「离线转写完成时刻」当「源话说完时刻」⇒ 数值**随句子数单调虚涨**（6.4→10.4→14.6→**17.8 s**），而用户实际听到的错位只有 ~6 s | **指标 bug**（面板在骗人） | ✅ 已修 |
| ③ | **MT 冷启动 24 s** —— 「回首页」清空 `compute.autostart` ⇒ MT/TTS 不在里面 ⇒ 重启后本机没有 TTS ⇒ 语音走对端板子；MT 要重新加载 ~22 s | **真 bug** | ✅ 已修 |
| ④ | **TTS 合成首窗 3.2 s** —— 上游实现首个解码窗口硬等满 50 帧（= 4 s 音频）⇒ 同传链路上最大的一段固定延迟 | **性能** | ✅ 已修（3.2 → 2.0 s） |

**修完的端到端（真机、文件源、跟播放、热态）**：面板滞后 **5.1 ~ 5.7 s**（**平的，
不再增长**），修前是 **6.4 → 17.8 s**（虚涨）/ **13.9 s**（重复识别时的真实积压）。

#### 1. 怎么量的（埋点）

光靠「滞后」一个总数看不出花在哪 ⇒ 沿链路埋了**分段计时**：

| 埋点 | 位置 | 字段 |
|---|---|---|
| 同传侧 | `interp.py::InterpSegment` / `InterpEngine._timings`（环形 60 句） | `t_seg` `t_deq` `t_tr` `t_submit` `wait_mt` `infer` `attempts` |
| TTS 侧 | `tts/queue.py::TTSJob` / `TTSQueue._played_timings` | `t_submit` `t_synth_start` `t_first_pcm` `t_play` `t_end` `lag` `tempo` `backlog` |

拆出来的段：

```
ASR出句 ──[翻译队列排队]── [等MT就绪] ──[纯推理]── [放行等待] ──[队列排队] ──[合成首窗] ──[出声启动]── [播完]
```

⇒ 探针：`tools/probes/interp/m19_latency_breakdown.py`（**默认先热身**，否则测的是
「冷启动」不是「链路」）。

#### 2. 冷态实测（第一轮，MT 刚被 autostart 削掉）

```
翻译（ASR 出句 → 译文回来）  平均 24.10 s   ← 几乎全是「等 MT 就绪」
放行等待                      0.00 s
队列排队                      5.42 s
合成首窗                      3.28 s
出声启动                      0.23 s
端到端滞后                   36.77 s
```

⇒ **第一眼看到的是「翻译 24 s」，其实绝大部分不是翻译，是等 MT 加载** ——
这就是为什么必须把「等 MT 就绪」和「纯推理」分开记。

#### 3. 热态实测（模型都在跑）—— 真正的链路

```
翻译排队 0.00 · 等MT 0.00 · 纯推理 0.28 · 放行等待 2.42
队列排队 2.58 · 合成首窗 3.23 · 出声启动 0.31 · 播完 4.05
```

⇒ **翻译只占 0.28 s**（MT 推理很快）⇒ 瓶颈**根本不在翻译**。

#### 4. 根因 ①：**重复点「开始识别」⇒ 同一批句子被反复入队**（用户这轮的直接原因）

**现场证据**（服务端 journal，用户在浏览器里反复点「开始识别」）：

```
03:19:43 POST /api/qwen3-asr/transcribe 200
03:19:53 POST /api/qwen3-asr/transcribe 200
03:20:04 POST /api/qwen3-asr/transcribe 200
03:20:28 live session live-5fa5a42f8da1 started (source=board …)
03:20:37 POST /api/qwen3-asr/transcribe 200      ← 又一次
```

⇒ **同一个文件被转写了 3~4 遍**。

**现场证据**（同传侧的计时，一轮 4 句的文件）：

```
# 1 src[1.0~2.06]  'Uh huh. Oh yeah, yeah!'
# 2 src[2.06~6.5]  "He wasn't even that big when I started l"
# 3 src[6.33~12.1] "t in his solo music, didn't do overly we"
# 4 src[12.1~15.0] 'well when he started writing for other p'
# 5 src[1.0~2.06]  'Uh huh. Oh yeah, yeah!'      ← 重复
# 6 src[1.0~2.06]  'Uh huh. Oh yeah, yeah!'      ← 重复
# 7 src[2.06~6.5]  …                             ← 重复
…
#14 src[12.1~15.0] …                             ← 重复
```

⇒ **14 句里有 10 句是重复的**（4 句 × 3~4 轮）⇒ TTS 队列里堆了
**35.6 s 的音频**（源话只有 14 s ⇒ **2.54×**）⇒ 队列必然积压 ⇒
面板显示「**滞后 13.9 秒**」，而用户看到的正是「ASR+翻译都结束了，
同声传译才播放出声音」。

**为什么没被挡住**：前端 `start()` **清了显示**（`_tr.items = []`、
`_sentClear(...)`），但**没清后端的队列** ⇒ 后端照旧把每一轮都灌进去。

**修法**（后端为主）：

1. `InterpEngine.on_new_stream(stream_id)` —— **新一轮识别（新 `stream_id`）**
   ⇒ 作废上一轮的：待播（放行器压着的）+ 翻译队列（没翻完的）+
   **TTS 队列（已排进去的译文）** + 放行器**换流**。
2. `service.py::transcribe_stream` 在**新的一次请求**（新 `rid`）时调用它。
3. ⚠️ **放行器换流（`Gate.reset_stream()`）≠ 新会话（`Gate.reset()`）**：
   换流**保留**「源已结束」这个**媒体状态**（媒体还是那个媒体 —— 文件源
   用的是同一个 `<audio>` 元素）⇒ 清掉的话 ⇒ 新一轮的句子会因为「还在
   等播放头」被**压住** ⇒ 回归第三场（「源结束后开始识别 ⇒ 剩下的译文照样
   全播」）**失败**。⇒ 换流只清：播放头映射 / 「播放头曾经走过」/
   媒体零点 / 兜底计数。

⚠️ **只在「新的一次请求」时复位**，**不在「换段（epoch）」时复位** ——
换段是 ASR **内部**原因（长度上限 / 卡住 / 停顿 / 退化）触发的，
不是用户操作 ⇒ 换段就清队列会把「已经念到一半的内容」抹掉。

⚠️ **前端「开始识别」不把媒体倒回 0**（试过，**回退了**）：倒带会清掉
「源已结束」⇒ 跟播放又从头等 ⇒ 回归第三场失败 ⇒ 保持「源播完了就别再
play()」的既有行为。

**实测**（连跑 3 轮识别）：

```
第1轮 → 队列 已播9 深度2 在途1
第2轮 → 队列 已播11 深度2 在途1     ← 深度**不叠加**
第3轮 → 队列 已播13 深度1 在途2
```

（修前：深度会一路涨到 4 / 8 / 12。）

#### 5. 根因 ②：文件源的滞后基准错了（面板在骗人）

**文件源**的句子是**离线一次性转写完**的 ⇒ 所有句子的 `created_ts`
（= ASR 切出这句的时刻）**几乎相同**（都等于「转写完成那一刻」）⇒
而滞后被算成 `播完时刻 − created_ts` ⇒ **每多一句，基准不变、终点后移**
⇒ 数值**单调虚涨**：

```
修前：lag = 6.34 → 10.39 → 14.59 → 17.76 s      ← 虚涨
修后：lag = 5.09 → 5.40 → 5.12 → 5.69 s          ← 平的（≈ 真实错位）
```

**修法**：文件源改用**音频时间轴**。播放头上报里已经带了「播放头在第几秒」，
反推一下就知道「媒体时间 0」对应哪个墙上时刻：

```
媒体零点 = 上报时刻 − playhead_sec          （Gate.media_anchor()）
源话说完时刻 = 媒体零点 + src_end_sec
```

⚠️ **只在「媒体真的在播」时更新零点** —— 播放前播放头恒为 0，那时算出来的
零点会**一直往后滑** ⇒ 滞后恒为 0。

⚠️ **实时流不变**（仍用 `created_ts`）—— 实时流里「ASR 出句」≈「用户说完」。

回归：`scripts/verify_interp_latency.py`（17 条）。

#### 6. 根因 ③：MT/TTS 每次都要冷启动（24 s）

**现场证据**（journal）：

```
02:24:51 boot: compute.autostart 声明了 3 个模型: ['hy-mt-1-5','qwen3-asr','qwen3-tts']
02:42:34 boot: compute.autostart 声明了 1 个模型: ['qwen3-asr']      ← 中间被削了
```

**怎么被削的**：`DELETE /api/compute/active-app`（用户点「回首页」）⇒
`clear_active_app()` 把 `compute.autostart` **整个清空**（这是**有意为之**，
防止只增不减撑爆 NPU）；再进 ASR 卡 ⇒ `set_active_app()` 只按**该卡声明的
capability** 加回 `qwen3-asr`（ASR 卡只声明 `swcap_audio_asr`）⇒
**MT 和 TTS 都不在里面了**。

⇒ 重启后：本机没有 TTS ⇒ **语音走了对端板子**（PCM 走网络）；MT 要重新加载
~22 s ⇒ **同传第一句干等 24 s**。

**修法**：同传是**唯一**同时需要 MT 和 TTS 的功能 ⇒ 由它补记：

1. `InterpEngine._record_dep_autostart()` —— **每次 `interp/start` 都同步补记**
   正在跑的本机 MT / TTS 到 `compute.autostart`；
   * ⚠️ **必须每次都记**：`_ensure_*_model()` 在「模型已经在跑」时会提前返回
     ⇒ 只在「把模型拉起来的那一次」记是不够的（实测：第一次记了，第二次因为
     `_tts_ready=True` 提前返回 ⇒ **没记回去**）；
   * ⚠️ **只记本机实例**（`source` 必须是 `spawned`/`adopted_local`）——
     peer 的实例记进来等于「下次开机等一个空头」。
2. `_ensure_tts_model()` 起完本机 TTS 后也记一笔（覆盖「同传把它拉起来」这条路）。

⇒ 实测：削成 2 条 → 起同传 → **TTS 自动记回**（且扛得住浏览器每秒的
`PUT /active-app`，因为 `set_active_app` 是**增量合并**、不删别人的条目）。

#### 7. 根因 ④：TTS 合成首窗 3.2 s（同传链路上最大的一段固定延迟）

**TTS 其实是流式的**（talker 边生成 codec 帧、decoder 边解码成 PCM），
但**首个解码窗口硬等满 50 帧**（50 × 1920 / 24000 Hz = **4 s 音频**）⇒
实测「合成首窗」≈ **3.2 s**。

**凭什么能缩短**：`speech_decoder` 是**因果**的 —— 它的分块解码设计就是
「左上下文 25 帧 + 新 25 帧」（`_decode_window_with_context`），**后续窗口
本来就只攒 25 帧**；而**首个窗口没有左上下文可用** ⇒ **未来的帧对已生成
的部分没有任何影响** ⇒ 攒够 25 帧就能开声，帧覆盖仍精确：

```
首窗吃 0~24（prev_context = 0~24） → 下一窗吃 25~49 → 不重不漏
```

**改法**：`wdnai.backends.tts` 加 `FIRST_WINDOW_FRAMES`（默认 25）+ 可选参数
`first_window_frames`（0 = 用默认）；插件侧 `TTSRequest` / `TTSJob` /
宿主桥 `submit_queued` 逐层透传；**只有同传**显式给 25。

**A/B 验证**（`tools/probes/interp/m20_tts_first_window.py`）：

⚠️ TTS 是**采样生成**的（`temperature=0.9`）⇒ **两次合成同一句话本来就不一样**
⇒ 直接比两次 WAV 的相似度**没有意义**。⇒ 改成**每种配置各跑两次**：

| | 同配置两次（噪声基线） | 跨配置 |
|---|---|---|
| 归一化互相关 | 25 帧 0.0175 / 50 帧 −0.0251 | −0.0246 ~ 0.0100 |
| 前 2 s RMS | 50 帧 0.0428~0.0435 | 25 帧 0.0417~0.0531（**在同量级**） |

⇒ 跨配置差异**落在采样噪声底内** ⇒ 没有把音频搞坏。

**边界连续性**（2.0 s 处正是首窗/次窗的接缝）：接缝处相邻样本差
**0.08× 全段平均** ⇒ **没有断裂/爆音** ⇒ 帧账目精确。

**效果**：合成首窗 **3.23 s → 2.03 s**（−37%）。

⚠️ **没有改变总合成时间**（首窗只影响「首声何时出现」，不影响 talker 生成
全部帧的耗时）⇒ 队列吞吐不变。

#### 8. 修完的实测（真机 · 文件源 · 跟播放 · 热态）

⚠️ **测量要在「安静窗口」做** —— 用户的浏览器每点一次「开始识别」就会多
一轮 ⇒ **计数是累计的** ⇒ 汇总值会被前几轮污染（实测过一次并发轮 ⇒
「切句 18 / 译文 29.5 s」，那不是单轮的数）。下面的数字取自**只有探针
自己那一轮**的窗口。

| 段 | 修前 | 修后 |
|---|---|---|
| **重复识别导致的重复入队** | 一轮 4 句 → 实测收到 **14 句**（3.5 倍音频） | **每轮都复位 ⇒ 只剩当轮的 4 句**（实测连跑 3 轮，队列深度 1~2，不叠加） |
| 等 MT 就绪 | **24.10 s**（冷启动） | **0.00 s** |
| 纯翻译推理 | （混在上面） | **0.26~0.28 s** |
| 放行等待（跟播节奏，设计如此） | — | 2.42 s |
| 队列排队 | 5.42 s | 2.58 s |
| **合成首窗** | 3.23 s | **2.01~2.03 s** |
| 出声启动 | 0.23 s | 0.31 s |
| **面板端到端滞后** | 6.34 → **17.76 s**（虚涨） | **5.09 → 5.69 s**（平） |

**面板读数序列**（每秒采样，「跟播滞后」= 源话比最后一句已播译文前进了多少）：

```
修前（有积压时）：13.9 s（一直挂着）
修后（有积压时）：13.4 → 3.9 → 2.6 → 1.4 → 6.0 → … → 0.0   ← 会**排空**
```

⇒ 关键区别不是「峰值多高」，而是**会不会自己排空**。

产能核对（安静窗口）：源话 14.0 s vs 译文 16.2 s（= **1.157×**）⇒ 译文比源话
长 16% ⇒ 靠**自动变速**（实测 1.04~1.08）补回来 ⇒ 滞后**不再累积**。

#### 9. 遗留与边界（诚实说明）

* **跟播（follow）模式的「放行等待 2.4 s」是设计行为**，不是 bug ——
  放行器**故意**等播放头走到那句的位置才让译文进队列（否则译文会抢在源话
  前面播）。⇒ 它不进「滞后」数字（基准是源话结束时刻）。
* **实时流**的滞后基准仍是 `created_ts`（≈ 用户说完的那一刻）—— 语义正确，
  但它**不含**「ASR 收尾推理」那一小段（实测 ~0.3~1.7 s，视切句档位）。
* **产能仍是硬约束**：TTS 合成约 **1.0× 实时**（实测 16.2 s 音频 / 约 15 s
  合成），源话也是 1.0× ⇒ **没有余量** ⇒ 任何抖动（模型抢卡、磁盘慢、
  句子变长）都会变成积压。⇒ 靠自动变速吸收（≤1.15）。
  ⇒ **再往下压只有两条路**：① TTS 流式化到**真·增量**（当前首窗已是
  因果流式，瓶颈在 talker 生成速度）；② 允许**丢弃过期句子**（牺牲完整性
  换实时性）—— 属于产品决策，本期**没做**。
* **首窗 25 帧是 SDK 的全局默认**（`FIRST_WINDOW_FRAMES`）—— 对其它 TTS
  调用方也是提速（首声更早）；A/B 只验了**电平统计 + 边界连续性**，
  **没有做听感评测**（板子上做不了）⇒ 若用户觉得开头有杂音，把
  `FIRST_WINDOW_FRAMES` 调回 50 即可（一个常量）。
* **面板的「滞后」在跟播模式下是另一个口径** —— `lag_sec` = 「源话比
  **最后一句已播译文**前进了多少秒」（不是端到端的「播完 − 源话说完」）。
  端到端那个在 `tts_status.last_lag_sec` / `avg_lag_sec` 里（修完是
  **5.1~5.7 s**）。两者语义不同、都会显示，**别混着看**。
* **重复识别的复位会清 TTS 队列** —— 用户连点两次「开始识别」⇒ 第一轮
  **还没播完**的译文会被丢掉（只保留当轮的）。这是**有意的**
  （否则重复内容会堆着播），但意味着「快速连点两次」会听到第一轮被截断。

---

### M25 —— **「播放还是滞后很多」+「播放中途会有卡顿断音」**（2026-11-22）

#### 0. 结论先说

这一轮用户报了两件事，**性质完全不同**：

|  complaint | 结论 | 状态 |
|---|---|---|
| **播放中途卡顿断音** | **根因找全了**（四层），其中**一层是我自己埋点的 bug**（报出来的「断音」大半是假的） | ✅ **已修**（句内饿死 0.00 s、句间间隔 ≤ 0.2 s） |
| **播放还是滞后很多** | 非结构性原因已修 3 处；**剩下的，是一个物理上限** | ⚠️ **结构性上限已量化**（译文最快只能跑到源话的 **83~90%**） |

**一句话的结论**：这条链路在**本板（RK3588 级 NPU）**上**做不到完全跟平** ——
实测 TTS 的**边际产能只有 1.03~1.09× 实时**，而**译文比源话长 20~25%** ⇒
**产能缺口 10~17%** ⇒ 译文每秒必然落后 0.10~0.17 秒 ⇒ **会随讲话时长线性累积**。
⇒ 要真正解决，只能在 §6 的**三条出路**里选一条（**属产品决策，本轮没做**）。

#### 1. 怎么量的（新增埋点 + 两个探针）

上一轮（M24）已经把「ASR → 翻译 → 放行 → 合成 → 播放」逐段埋点了，但**播放侧
还是黑盒**：只知道「一句播完了」，不知道**中间断没断、断多久**。这一轮补齐：

**队列的每句埋点**（`cards/tts/queue.py::_finish_job` ⇒ `timings[]`）：

| 字段 | 口径 | 为什么要有 |
|---|---|---|
| `synth_lat` | 开始合成 → 出声 | **跟播提前量该按它算**（见 §3.2） |
| `pipe_lat` | 入队 → 出声 | 端到端管线延迟 |
| `gap` | **句间间隔**（管道空着等下一句，精确到 ms） | **断音的直接度量** |
| `starved` | **句内饿死**（管道空着等合成） | 句**内部**的断音 |
| `stock` | 开口时管道里的存货（秒） | 缓冲够不够的直接证据 |
| `m_rate` | **边际生产速率**（首窗之后的音频秒/墙钟秒） | **「产能」的真值** |
| `o_rate` | 整体速率（含首窗延迟） | 只作诊断 |
| `a_lag` | **可听滞后** = 译文出声时刻 − 源话开口时刻 | **用户真正感受到的那一口** |
| `k` | 译文长度比 = Σ译文音频 ÷ Σ源话 | **变速该瞄的目标**（见 §4） |

**两个新探针**：

* `tools/probes/interp/m21_pipeline_metrics.py` —— **播放侧体检**：
  起同传 → 识别文件 → 每 250 ms 采一次队列状态 ⇒ 打印逐句表格 +
  **长度比 k** + **产能缺口**。⚠️ 它**自带「模拟浏览器播放头」**
  （`--gate follow`）⇒ **不用开浏览器**就能量跟播的真实表现。
* `tools/probes/interp/m22_aplay_rate.py` —— **aplay 到底是不是按 1.0×
  实时消费**：往管道灌 5 s 静音，看存货怎么衰减。
  ⚠️ **这个模型必须验** —— 后面 §2.3 会看到，**不验它，报出来的「断音」
  全是假的**。

#### 2. 「断音」的四层根因（其中一层是我自己的 bug）

##### 2.1 第一层：**每句都新建 + 关闭一个 aplay**（上轮已修）

⇒ 每句要 `amixer` un-mute（fork+exec）→ 起 aplay/sox → **探活 0.12 s**
⇒ 句间静音 **0.13~0.28 s**。
⇒ **上轮已修**：`AplayWriter` **跨句复用**（空闲 15 s 才关、变速变了才重开）。

##### 2.2 第二层：**「一句一缓冲」的结构** ⇒ 每句之间再断一次（本轮修）

原来播放线程是「**取一句 → 播完这句 → 再取下一句**」：

```
播放线程：  [等句1的首窗] → 写句1 → 写句1 → … → [等句2的首窗] → …
                                        ↑ 这里管道是空的 ↑
```

⇒ **下一句的首窗（~2 s）期间管道里没货** ⇒ **每句之间断一次**。
⇒ 本轮改成**共享 FIFO**：合成线程把 PCM **直接推进一个队列**，播放线程连续取
⇒ **下一句的音频在上一句还在播的时候就已经在 FIFO 里等着** ⇒ 句间零缝隙。

⇒ 实测：句间间隔 **0.28~0.85 s → 0.031~0.059 s**（见 §5）—— **听不出来**。

##### 2.3 第三层：**我自己的记账 bug** ⇒ 报出来的「断音」大半是假的（本轮修）

`AplayWriter` 用「已写入 − 已消费」估算管道存货，而「已消费」按墙钟推：

```python
def _tick(self, now):
    dt = now - self._last_write_ts      # ⚠️ 每次都从「最后一次写入」起算
    consumed = min(self._backlog_sec, dt * self._tempo)
```

⇒ **每次 tick 都把「上次写入到现在的整段时间」再扣一遍** ⇒
**1.0 s 的墙钟扣掉了 1.5 s 的音频** ⇒ 管道被**误判成空了** ⇒
**报出来的「每句断 1.4 s」其实一秒都没断**。

⇒ 探针 m22 实测（修正后）：**消费速率精确 1.000×**（每 0.5 s 稳定扣 0.5 s）
⇒ **aplay 确实是实时消费的，模型没问题**。
⇒ 修法：`_tick` 从「**上一次 tick**」起算（新增 `_last_tick_ts`）。

> ⚠️ **教训**：**度量本身要先被度量**。这一层如果没挖出来，后面的所有
> 「断音」结论都会建立在错的数上（本轮前两版结论就是这么错的）。

##### 2.4 第四层：**空文本请求也会起 aplay 抢声卡**（SDK 侧，本轮修）

worker 日志里每合成完一句就有 3 次：

```
[audio] streaming via aplay (aplay -t raw -f FLOAT_LE -r 24000 -c 1 -D plughw:1,0 -)
serve[wdndds/rpc/qwen3-tts]: respond raised ValueError: input text is empty
```

⇒ SDK 的 `run_with_progress` 是「**先建 AudioPlayer（= fork 一个 aplay
去开 `plughw:1,0`）→ 再算 prefill（这一步才因空文本抛错）**」
⇒ **每一次空文本请求都起一个 aplay 去抢声卡**。
⇒ 声卡是**独占**的 ⇒ 它要么抢在同传队列前面把设备占住（⇒ 队列的 aplay
起不来 ⇒ **整句没声音**），要么自己「Device or resource busy」秒退。
⇒ 修法（`wdnai/backends/tts.py`）：**空文本在建 AudioPlayer 之前就拒**
⇒ 一个 aplay 都不起。

##### 2.5 顺带删掉的：**每句开头「攒 3 s 缓冲」**

上一轮为了压句内饿死，给每句加了个「攒够 3 s 才开口」——
⇒ **句与句之间**会白等一个首窗 ⇒ **反而制造了句间断音**。
⇒ 删掉，改由**共享 FIFO 当地震缓冲**（合成比播放快时它自动蓄水）。

#### 3. 「滞后」的根因

##### 3.1 非结构性原因 ①：**提前量是「管线延迟」** ⇒ **正反馈跑飞**

放行器在「播放头到达 `句首 − lead`」时放行。原实现让
`lead = 实测的管线延迟（入队 → 出声）`：

* 管线延迟 = **排队等待** + 生产延迟；
* 而**排队等待恰恰是提前量造成的**（提前放行 ⇒ 句子早进队列 ⇒ 排更久）
⇒ 提前量 ↑ ⇒ 管线延迟 ↑ ⇒ 提前量 ↑ ⇒ **正反馈**
⇒ 实测提前量一路顶到上限 **6 s** ⇒ **译文比源话晚 ~5 s**，面板的滞后**卡在 5 s 下不来**。

⇒ 修法：提前量改按**生产延迟**（`synth_lat`，实测稳定 **1.94~2.20 s**）
⇒ **跟队列深度无关** ⇒ 不跑飞。

##### 3.2 非结构性原因 ②：**播放没有真的「跟」源话** ⇒ 改用**播放时钟**

原来「跟播」= 放行器按播放头放行 + 播放**不等**任何东西 ⇒ 译文出声的时刻
= `句首 − lead + 管线延迟` —— 靠一个**静态偏移**去凑对齐，
而管线延迟本身在变 ⇒ 对不齐。

⇒ 本轮把**两件事拆开**：

| 环节 | 由谁决定 | 作用 |
|---|---|---|
| **合成什么时候开始** | 放行器（提前量 = 生产延迟 + 余量 1 s） | 让合成**提前跑**，管线有缓冲 |
| **播放什么时候出声** | **播放时钟**（源话的播放头） | 译文**跟源话对齐** |

实现：队列的 FIFO 带上每块的**生产时刻**，播放线程在写进管道之前问一句
「源话的播放头到这句了吗」⇒ 没到就**放回 FIFO 头部等着**（不丢）。
⚠️ **时钟卡住 8 s 就放弃等待**（源暂停 / 卡住 / 没上报 ⇒ 不能把译文永久压住）。

##### 3.3 结构性原因 ③：**产能缺口**（**这个修不掉，是物理上限**）

实测（安静窗口 · 文件源 · 跟播）：

```
边际产能 m_rate  = 0.985 ~ 1.090 × 实时  （中位 ~1.05；首窗之后的产量）
译文长度比   k   = 1.200                 （源话 14.17 s → 译文 17.00 s）
产能缺口        = 1 − m/k = 0.105 ~ 0.127（另一轮实测 k=1.247 ⇒ 缺口 0.14~0.17）
```

⇒ **译文最快只能跑到源话的 83~90%**（本轮实测 **89.5%**）⇒ **每秒落后 0.10~0.17 秒**
⇒ 讲话越长越落后（15 s 的文件 ≈ 落后 2 s；3 分钟 ≈ 落后 20~30 s）。

> ⚠️ **为什么变速救不了**（这一版的推理之前是**反的**）：
> sox 的 tempo 在**播放器上游** ⇒ 变速越大，播放器从合成流里**拉得越快**
> ⇒ **播放速度 = min(tempo, 产能)** ⇒ **tempo > 产能只会更快地吃空缓冲**，
> **不会让译文更快**。⇒ 变速的**上限是产能**（不是 `1/产能`）。

#### 4. 变速该怎么用（方向纠正 + 两条纪律）

| 判据 | 改前 | 改后 | 理由 |
|---|---|---|---|
| **目标值** | 「积压 > 2/4/8 s」 | **长度比 k**（译文比源话长多少 ⇒ 就该快多少） | 跟播模式积压在**放行器**那边，队列一直是空的 ⇒ 旧判据永远不启动 |
| **上限** | `1/产能`（**方向反了**） | **≤ 产能** | 见 §3.3 |
| **变化时机** | 随时 | **棘轮：一轮同传内只升不降** + 死区 0.02 + 最小间隔 8 s | 每次改变速 = **重开 aplay 管道**（sox 的 tempo 是 spawn 时定死的）= **一次断音**；实测产能估计在阈值附近抖 ⇒ 变速 1.00↔1.04 来回切 ⇒ 反复断音 |

⇒ 死区踩过坑：**0.05 会把整整一档（1.00 → 1.04）挡在门外** ⇒ 变速永远升不上去。

#### 5. 修完的实测（真机 · 文件源 · 跟播 · 热态）

| 指标 | 修前 | 修后 |
|---|---|---|
| **句间间隔** `gap` | 0.28 ~ 0.85 s | **0.031 ~ 0.059 s**（精确到 ms；听阈 ≈ 100 ms ⇒ **听不出来**） |
| **句内饿死** `starved` | 1.2 ~ 5.6 s | **0.00 s** |
| **开口时管道存货** `stock` | 0.00 s（开口即断） | 0.00 → 0.22 s（**在长** ⇒ 缓冲在建立） |
| **提前量** | 顶到上限 6 s（正反馈） | **生产延迟 1.96~2.20 s + 余量 1 s**（稳定） |
| **变速** | 卡在 1.00（死区挡了一整档） | 按 **k** 瞄、受产能封顶、棘轮只升不降（实测升到 1.04） |
| **可听滞后** `a_lag` | 5 ~ 8 s 且**单调增长** | **2.61 / 4.83 / 5.61 / 5.03 s**，增长速率 = 产能缺口（0.10~0.17 /s）；**面板改为优先显示这个口径** |
| **面板滞后** | 5 s 卡住下不来 | 随源话推进**平**（不再被提前量带偏） |
| **端到端**（播完 − 源话说完） | 4.7 → 18.1 s（now 模式串行） | 跟播下 **≈ 产能缺口 × 源话时长** |

#### 6. 结构性结论：实时同传**物理上无法完全跟平** ⇒ 三条出路

**缺口 = 1 − 产能/长度比 ≈ 10~17%**（本板实测）。要真正做到「译文跟源话同时」，
只有三条路（**都是产品决策，本轮没做**）：

1. **让译文更短**（把长度比 k 压到 ≤ 1.0）
   ⇒ 直接把负载降下来。HY-MT1.5 是**专用翻译模型**（没有 prompt 可调）
   ⇒ 只能**后处理**（去冗余 / 换更简的目标语风格）⇒ 收益有限，
   且**会损失信息量**。
2. **丢弃过期句子**（滞后超过阈值 ⇒ 跳过源话已经说过的句子）
   ⇒ 经典同传的「省译」策略：牺牲完整性换实时性。
   ⚠️ **与本仓文档里的「不丢内容」原则冲突**（`queue.py` 队满时的策略是
   「合并队尾，不丢」）⇒ **必须先有产品决策** ⇒ 本轮**没做**。
3. **接受一个固定滞后**（面板如实显示，让用户知道译文会慢慢落后）
   ⇒ 本轮已经做到「**如实显示 + 不再乱涨**」。

⇒ **另外两条工程侧的路**（不解决缺口，但能少疼一点）：
* **TTS 提到真·增量**（当前首窗已是因果流式，瓶颈在 **talker 的生成速度**：
  实测 ~12.5 语义 token/s ≈ 1.0× 实时）⇒ 换更小的 talker / 更快的 NPU 核心配置。
* **给 TTS 腾出独占的 NPU 核**（本板 TTS 在 npu1，ASR+MT 在 npu0 ⇒ 已隔离）。

#### 7. 回归（本轮改动涉及 3 个仓）

| 脚本 | 结果 |
|---|---|
| `scripts/verify_tts_queue.py` | **19 / 0** |
| `scripts/verify_interp_gate.py` | **45 / 0** |
| `scripts/verify_interp_segmenter.py` | **11 / 0** |
| `scripts/verify_interp_api.py` | **53 / 0** |
| `scripts/verify_interp_latency.py` | **22 / 0** |
| `scripts/verify_interp_browser.py` | **50 / 0** |
| `scripts/verify_asr_translation.py` | **77 / 0** |
| `scripts/verify_asr_translate_toggle.py` | **16 / 0** |
| `scripts/verify_tts_voice_catalog.py` | **47 / 47** |
| `scripts/verify_asr_wizard.py`（既有） | **173 / 0** |

#### 8. 遗留与边界（诚实说明）

* **产能缺口没有解决**（§6）—— 这是**物理上限**，不是 bug。
  ⇒ 面板上的滞后会**随讲话时长线性增长**（0.10~0.17 /s）⇒ 长会议必然
  「越听越滞后」。**这一条需要产品决策**（要不要省译 / 要不要降质量）。
* **播放时钟依赖前端的播放头上报** ⇒ 浏览器不报 / 报错 ⇒ 播放头不动 ⇒
  8 s 后**放弃等待**（宁可不准，也不卡住）。
* **「now」模式没有播放时钟** ⇒ 译文一合成就播 ⇒ 队列串行 ⇒ 端到端滞后
  = 队列深度（实测 now 模式 4 句：4.7 → 18.1 s）。**跟播模式才是正解**。
* **文件源的第一句天然晚 1~2 s**（ASR + MT 要先把音频转出来）⇒
  第一句的 `a_lag` 偏大是正常的。
* **`m_rate` 的样本量小**（每句一个数）⇒ 单句会抖（实测 0.965~1.094）
  ⇒ 队列用 **EMA** ⇒ 变速用**阶梯 + 棘轮** ⇒ 不抖。
* **变速上限仍是 1.15**（音质上限）⇒ 当 k > 1.15 时**光靠变速补不回来**
  （实测 k = 1.20~1.25 ⇒ 差 0.05~0.10 ⇒ 补不满 ⇒ 缺口里有一部分是
  **变速补不了的**）。
* **共享 FIFO 是无界的**（理论上）⇒ 正常场景（几秒的句子）没问题；
  长时间「生产 >> 消费」会攒内存 ⇒ 本轮**没加上限**（TTS 单实例，
  产能≈消费 ⇒ 攒不起来）。
* **空文本请求的来源还没定位到具体调用方**（只堵住了它的副作用：
  不再起 aplay 抢声卡）⇒ worker 日志里仍会看到那几行 WARNING（无害）。

---

### M26 —— **「多次拖动进度条，没和字幕的翻译同声播放出来」**（2026-11-22）

用户原话：

> 「『含音轨的视频』播放的时候，字幕在实时翻译，但是多次拖动进度条，
> 没有和字幕的翻译同声播放出来」

#### 0. 结论先说

| 层次 | 结论 | 状态 |
|---|---|---|
| **度量** | 「同没同声」第一次变成**一个可测量的数**（`err = 出声时的播放头 − 源话位置`） | ✅ 新增探针 `m23_seek_sync.py` + `m24_browser_seek.py` |
| **根因** | **四层**，其中一层是**队列自己的死锁**（被作废的句子堵住整条 FIFO ⇒ 到点整队一起炸出来） | ✅ **四层全修** |
| **效果** | 修前 **17 句里 15 句**错位（**+27~+35 s / −20~−31 s**）⇒ 修后 **23 句里 4 句**（剩下 4~8 s 是 M25 的**结构性产能缺口**，不是 seek 引入的） | ✅ **已验证**（API 层 + 真浏览器各一遍） |

**一句话**：拖进度条之后译文不同步，**不是「跟播没对齐」这一个 bug**，
而是 **① 前跳压根没被当成 seek ② 被跳过的句子照播 ③ 队列里的陈旧音频
没清 ④ 被作废的句子把整条播放 FIFO 堵死** 四件事叠在一起。

#### 1. 怎么量的（新增埋点 + 新探针）

**新增埋点**（`cards/tts/queue.py::_finish_job` ⇒ `timings[]`）：

| 字段 | 口径 | 为什么要有 |
|---|---|---|
| `src_start_sec` | 这句译文对应的**源话在媒体时间轴上的位置**（秒） | **同步度量的另一半**（另一半是播放头） |

**新探针** `tools/probes/interp/m23_seek_sync.py`：

* 识别**含音轨的视频**（`asr_zh_seek_test.mp4`，144.7 s）⇒
  句子**带媒体位置**；
* **等 ASR 追上播放头**（识别比实时快 ⇒ ASR 跑在前面 ⇒ 用户拖到的位置
  **大多已经识别过了** ⇒ 这才是用户遇到的现场）；
* 每 250 ms 上报一次**模拟的播放头**，按剧本拖两次
  （**前跳** 0 → 60 s，**后跳** 60 → 20 s）；
* 每 250 ms 采一次队列状态 ⇒ 逐句算 **`err = 出声时的播放头 − 源话位置`**。

⇒ **`err ≈ 0` 才是「同声」**；`|err|` 很大 = 译文在讲**别的位置**的内容。

#### 2. 修前的实测（复现）

```
seq   t_play    ph        src       err      a_lag   text
1     ...419.64  4.29      1.06      3.23     3.46    习惯的书写，我每天想：爸爸，那全世界人类怎样走过
2     ...429.28 14.03     13.73      0.30     0.45    但是，今天的教育，我的教育是：做人。
4     ...436.71 63.51     60.06      3.45     3.54    我怎么做，自身的力量，一个价值。
5     ...440.36 67.04     66.06      0.98     1.19    ，一个下午。
6     ...442.54 69.19     66.06      3.13     3.37    我对于人的理解，怎么样？
7     ...447.15 73.98     73.73      0.25     0.31    您呢？
8     ...448.95 27.94     48.31    -20.37    34.87    啊，不，是，就是和我。
...
17    ...467.12 46.19     77.06    -30.87    27.16    看。

同步误差 |err| > 5 s 的句子: 15 / 17
```

⇒ **前跳之后**：播放头在 61~74 s，播的却是 **29~47 s** 的内容（**+27~+35 s**）。
⇒ **后跳之后**：播放头在 27~46 s，播的却是 **48~77 s** 的内容（**−20~−31 s**）。
⇒ `skipped_seek = 0` ⇒ **seek 一次都没被识别**（前跳不算、后跳也没丢到东西）。

#### 3. 四层根因

##### 3.1 第一层：**前跳压根没被当成 seek**

`Gate.update_playhead` 只判**回退**（`now < prev − SEEK_BACK_TOLERANCE`）⇒
**前跳完全不被识别** ⇒ 用户跳过的那些句子**还在待播队列里** ⇒ 放行器一看
播放头已经越过它们 ⇒ **全部放行** ⇒ TTS 队列里灌进几十秒的**陈旧译文**
⇒ 接下来讲的全是**用户已经跳过的内容**。

**修法**：**双向**判定。前跳的判据不是「跳了多少」而是
「**比两次上报之间的真实时长多跳了多少**」：

```python
dt = now_ts - self._ph_ts          # 两次上报之间真实过去多久
if now - prev > dt + SEEK_FWD_TOLERANCE:   # 多出来的才算 seek
```

⇒ 媒体**卡住 / 缓冲**造成的一次补跳（`now − prev ≈ dt`）**不会**被误判；
⇒ 用户**暂停很久**再恢复（`dt` 很大、`now − prev` 很小）**不会**被误判。

##### 3.2 第二层：**队列里的陈旧音频没人清**

即使认出了 seek，队列侧也**没有「按位置丢陈旧音频」的接口**：
`clear()` 是**全清**（连在队的内容也作废），而「只丢 FIFO 里陈旧 PCM、
保留待提交句」这种**半清**做不到。

**修法**：新增 `TTSQueue.flush_by_position(pos_sec, direction)` ——
① 丢待合成的 job；② 丢 FIFO 里陈旧的 PCM；③ **把正在合成的那句也作废**
（它的 PCM 之后还会进 FIFO ⇒ 不作废的话**陈旧译文照样播出来**）；
④ **复位播放时钟**（后跳 ⇒ 播放头变小 ⇒ 时钟的「停滞判定」永远不成立
⇒ 8 秒后兜底放行 ⇒ 播的还是旧内容）。

宿主桥新增 `queue_flush_position()`（`app/core/audio/tts_bridge.py`）。

##### 3.3 第三层：**拖回去之后，那些句子「再也说不出来」**

用户**往回拖**时，新位置对应的句子**早就播过了**（放行器放行过 ⇒
不在 `_pending` 里了）⇒ 没有缓存就**再也说不出来** ⇒
用户看到的字幕**有译文、却一声不响** —— 这正是用户报的
「没有和字幕的翻译同声播放出来」。

**修法**：引擎加**重播缓存**（`_cache`：所有已译好、且**带媒体位置**的句子）
⇒ seek 之后把「新位置之后」的句子**重新武装**（放回 `_pending`）⇒
放行器按**播放头的节奏**重新放出来 ⇒ **译文跟得上字幕**。
⇒ 已经播过的内容**会被重播一遍**（这是「拖回去重看」应有的行为）。

##### 3.4 第四层：**队列自己的死锁**（最隐蔽的一层）

播放线程里的判定顺序是「**先看播放时钟 ⇒ 再看 job 是否已取消**」：

```python
if chunk is not None and not self._clock_ready(job):   # ← 先判时钟
    self._fifo_unpop(item); time.sleep(0.05); continue
if job.cancelled:                                      # ← 后判取消
    ...; continue
```

⇒ 被 seek 作废的那句（`job.cancelled`）**还会继续吐 PCM**（合成线程已经在
跑了，停不掉）⇒ 它的分片继续进 FIFO ⇒ 它的位置在**未来**（比如 47 s，
而播放头在 20 s）⇒ 时钟说「还没到」⇒ 分片被**放回 FIFO 头部**
⇒ **整条 FIFO 被它堵住**（FIFO 是顺序的 ⇒ 队头不动 ⇒ 后面全不动）
⇒ 这 30 秒里**一句都没播** ⇒ 播放头走到 47 s 才放开 ⇒
**整队一起进管道**（实测 6 句的 `t_play` **完全相同**）⇒
**一串译文一起炸出来**。

**修法**：**先判「已取消」，再判时钟** —— 取消的 job 直接丢弃，
它本来就不该播，更不该**挡住后面的**。

#### 3.5 第五步：**浏览器**那条路也验过了（`m24_browser_seek.py`）

M23 是在 **API 层**模拟播放头（绕过浏览器）⇒ 它验的是「服务端收到跳变之后
会怎么做」。**用户那条路**是「在浏览器里**真的拖进度条**」⇒ 另有一支
`m24_browser_seek.py` 验前半段（真浏览器 + 真改 `video.currentTime`）：

```
== ① 前跳（拖到 40 s）==
  ok   服务端认出了**前跳**
  ok   前跳位置对得上（≈40 s）
== ② 等重开后的识别覆盖到 40 s 附近 ==
  ok   重开后的识别覆盖到 40 s 附近
== ③ 后跳（拖回 36 s，已识别过 ⇒ 前端不重开）==
  ok   服务端认出了**后跳**
  ok   后跳位置对得上（≈36 s）
  ok   后跳之后**重播**了（新位置之后的句子被重新武装）
  ok   面板提示「已跟随进度条重新对齐」
  ok   全程无 JS 错误

通过 12 / 失败 0
```

⚠️ **视频源有两条恢复路径，各管一段**（实测都成立）：

| 拖到的位置 | 前端行为 | 谁来把译文「追回来」 |
|---|---|---|
| **还没识别过** | **重开识别**（从拖到的位置往前 8 s）⇒ 服务端 `on_new_stream`（队列/缓存全清） | **新的识别轮次**重新产出那些句子 ⇒ 照说 |
| **已经识别过** | **不重开**（字幕本来就有） | **本轮的 seek 处理**（丢被跳过的 + **重播缓存**把新位置之后的句子重新武装）⇒ 照说 |

⇒ ⇒ 两条路**互补** ⇒ 不管拖到哪，译文都会重新跟上来。

#### 4. 修法汇总

| 层 | 改动 | 文件 |
|---|---|---|
| ① | `Gate.update_playhead` **双向** seek 判定 + `last_seek()` | 插件 `cards/qwen3_asr/interp.py` |
| ② | `TTSQueue.flush_by_position()`（丢 job / 丢 PCM / 作废在合成 / 复位时钟） | 插件 `cards/tts/queue.py` |
| ③ | 引擎 `_resync_seek()`：丢被跳过的待播句 + **重播缓存**重新武装 + 调 ② | 插件 `cards/qwen3_asr/interp.py` |
| ④ | 播放线程：**先判「已取消」再判时钟** | 插件 `cards/tts/queue.py` |
| 桥 | `queue_flush_position()` | 宿主 `app/core/audio/tts_bridge.py` |
| 面板 | seek 后 20 秒内显示「**已跟随进度条重新对齐**（重播 N 句）」 | 插件 `static/app.js` |

#### 5. 实测（修后）

同一支探针、同一段视频、同一个剧本（**前跳 0 → 60 s**、**后跳 60 → 20 s**）：

```
seq   t_play    ph        src       err      a_lag   text
1     ...789.99  1.95      1.06      0.89     0.00    习惯的书写，我每天想：爸爸，那全世界人类怎样走过
2     ...801.93 13.93     13.73      0.20     0.00    但是，今天的教育，我的教育是：做人。
15    ...809.92 63.81     60.06      3.75     3.89    我怎么做，自身的力量，一个价值。
18    ...816.05 69.92     66.06      3.86     4.03    我对于人的理解，怎么样？
19    ...819.78 73.80     73.73      0.07     0.09    您呢？
21    ...823.65 22.66     13.73      8.93     8.96    但是，今天的教育，我的教育是：做人。
22    ...830.08 29.09     29.06      0.03     0.05    是做人。
23    ...830.80 29.63     29.40      0.23     0.44    哦，人类叫臭小子，坏人。
24    ...834.73 33.60     32.06      1.54     1.70    我日。
25    ...837.07 36.08     35.06      1.02     1.04    我们现在只是玩，只是赛跑，只是简述机器。
26    ...848.24 47.26     47.06      0.20     0.21    我少，不定是多脆弱。
...
37    ...887.07 86.08     86.06      0.02     0.03    看。
38    ...917.20 116.07   116.06      0.01     0.17    老练，你可以很谦。

同步误差 |err| > 5 s 的句子: 4 / 23
interp: {'translated': 19, 'submitted': 38, 'skipped_seek': 0, 'replayed': 19,
         'played': 23}
```

**对照表**：

| | 修前 | 修后 |
|---|---|---|
| \|err\| > 5 s 的句子 | **15 / 17** | **4 / 23** |
| 前跳之后的错位 | **+27 ~ +35 s** | **0 ~ +4 s** |
| 后跳之后的错位 | **−20 ~ −31 s** | **0 ~ +4 s**（第一句 +8.9 s） |
| `t_play` 是否「整队同时」 | 是（6 句完全相同 ⇒ 一次炸出来） | **否**（每句各自出声） |
| 重播（拖回去后再说一遍） | 无此机制 | `replayed = 19` |

**剩下的 4~8 s 是什么**（**不是 seek 引入的**）：

* **后跳后的第一句（+8.9 s）** —— 那句要**重新合成**（TTS 合成过一次的内容
  不会缓存）⇒ 约 4 s 的首窗 + 放行等待 ⇒ **这是「拖回去重听」的固有代价**；
* **其余 5~8 s 的那几句** —— 是 M25 已量化的**结构性产能缺口**
  （译文比源话长 20%、边际产能 1.05× ⇒ 每秒落后 0.10~0.17 s）
  ⇒ **正常播放段落里也有**（同一份数据里 seq 29~34 的 3.7~7.5 s）。

⇒ **seek 引入的错位（+27~+35 / −20~−31 s）已经消失**；
⇒ 剩下的是 M25 那条**结构性上限**，属产品决策（§6 的三条出路），本轮没动。

#### 6. 遗留 / 边界

* **后跳时正在播的那一句放完**（aplay 管道里的音频没法「撤回」）⇒
  用户会听到**一句**旧内容，之后全是新位置的 ⇒ 可接受。
* **重播要重新合成**（TTS 合成过一次的内容不会缓存）⇒ 后跳后第一句约
  **2~4 秒**才出声 ⇒ 可接受。
* **seek 之后的 `a_lag` 口径会变得奇怪**（它算的是「出声时刻 − 源话开口
  时刻」，而重播的是**过去**的内容）⇒ **同步误差要看 `err`（本节的口径），
  不要看 `a_lag`**。
* **TTS worker 偶发 60 s 推理超时**（worker 日志里
  `respond raised TimeoutError: inference exceeded 60.0s`，实测 1 次 /
  上万行）⇒ worker 被杀后自愈（registry 会重拉）⇒ **与本轮改动无关**
  （ASR 在 NPU 0、TTS 在 NPU 1），但**值得单独查**。
* ⚠️ **板端基础设施坑（实测踩到，值得记一笔）**：worker 被 `SIGTERM` 杀掉
  之后，它占着的 **NPU 传输通道**会留在**坏状态** ⇒ 之后任何 worker 在
  **同一张卡**上 `rknn3_init` 都会失败：
  ```
  E NPUTransfer: socket read fd = 46, n = -1: Resource temporarily unavailable
  E RKNNAPI: rknn3_init, recv(MsgInitAck) fail, -9(ERROR_PIPE) != 256!
  load qwen3-tts @ npu=1 init failed but token stale; discarding
  ```
  ⇒ 表现是「**TTS 永远起不来**（功能未启用: swcap_text_to_speech）」，
  而 `registry` **不会重试** ⇒ 只能手工干预。
  ⇒ **手工恢复办法**（父进程 `rknn3_transfer_proxy` **不会**自动重启子进程）：
  ```bash
  # ① 找到该卡的那个子进程（名字 = rknn3_transfer_proxy_<device_hash>）
  ps -ef | grep rknn3_transfer_proxy
  # ② 杀掉它，并按同样的 argv[0] 约定拉起来
  sudo kill <pid>
  sudo bash -c 'exec -a rknn3_transfer_proxy_237c40f1 \
      /usr/bin/rknn3_transfer_proxy -s 0003:31:00.0 &'
  # ③ 再让 registry 重新拉起模型实例（DELETE + POST /api/compute/instances）
  ```
  ⇒ 修好之后 TTS 模型加载成功（实测 npu=1）。**根治要改 rknn3 的守护逻辑**
  （或让 worker 退出前 `rknn3_destroy`）—— **不在本轮范围**。

---

### M27 —— 「TTS 播出声音**太久了**」+「中间还是会有**顿挫**」（2026-11-23）

用户报（2026-11-23 00:13）：

> 1、ASR+翻译之后，TTS 播出声音**太久了**
> 2、中间还是会有**顿挫**
> 3、测试素材 = `/userdata/assets/uploads/asr_en.wav`（英文，15.05 s）

#### 0. 结论先说

**这两条抱怨是同一个根因的两副面孔：TTS 的产能喂不饱译文。**

| | 修前 | 修后 |
|---|---|---|
| **播报总时长**（首句出声 → 末句播完） | **17.20 s**（= 源时长 1.14×） | **15.12 / 15.09 s**（两轮复测，= **1.00×**） |
| **句间间隔 gap**（管道空着等下一句） | 0 / 0.29 / 0.50 / **0.73 s**（合计 **1.52 s**，逐句变大） | **0 / 0 / 0 / 0**（合计 **0.000 s**） |
| 句内饿死 starved | 0 | 0 |
| 变速 tempo | 1.08（**锁死**，超过真实产能） | 1.15（= 聚合产能上限） |
| 本机 TTS 实例 | 1（产能 ~0.95× 实时） | **2**（聚合产能 ~1.94×） |

⇒ **两个症状同时消失**：播报时长回到源文件长度（不再拖尾），句间零静音
（不再顿挫）。

#### 1. 怎么量的

探针：`tools/probes/interp/m27_en2zh_e2e.py`（端到端）+ `m27_tts_capacity.py`
（产能体检）。

⚠️ **口径**：队列的 `timings` 是**累计**的（跨会话不清零）⇒ 探针先取
**基线 max(seq)**，之后只统计**新句子** ⇒ 否则「播报总时长」会把上一轮
（甚至用户浏览器那一轮）算进去（实测第一次跑出来是 **46207 s** 的垃圾数）。

⚠️ **「每一段的耗时」从三个来源取**（各自口径不同，别混）：

| 段 | 来源 | 字段 |
|---|---|---|
| ASR 切句 → 出队 | 引擎 | `t_seg` → `t_deq` |
| 等 MT 就绪 / 翻译推理 | 引擎 | `wait_mt` / `infer` |
| 翻译完 → 入队（放行器压了多久） | 引擎 | `t_tr` → `t_submit` |
| 入队 → 开始合成 / 合成首窗 / 首窗→出声 / 出声→播完 | 队列 | `t_submit`/`t_synth_start`/`t_first_pcm`/`t_play`/`t_end` |

#### 2. 修前实测（单卡）

素材 `asr_en.wav`：**15.05 s / 48 kHz / 单声道**；ASR 切成 **4 句**
（源话合计 **14.17 s**）：

| # | 源文 | 源区间 | 译文 | 译文音频 |
|---|---|---|---|---|
| 1 | Uh huh. Oh yeah, yeah! | 1.00–2.06 | 嗯嗯。哦对了，当然了！ | 2.80 s |
| 2 | He wasn't even that big when I started l… | 2.06–6.50 | 当他开始给我听的时候，他甚至还没有那么大呢。 | 3.44 s |
| 3 | t in his solo music, didn't do overly we… | 6.33–12.10 | 在他的独奏作品中，表现并不出色，但他的表现还是不错的。 | 6.27 s |
| 4 | well when he started writing for other p… | 12.10–15.00 | 嗯，当他开始为其他人写作的时候。 | 4.14 s |

⇒ **长度比 k = 16.65 / 14.17 = 1.175**（译文念出来比源话长 17.5%）。

**产能体检**（`m27_tts_capacity.py`，直接把 4 句灌进 TTS 队列）：

```
合计：音频 15.32 s / 合成 15.51 s ⇒ 产能 r = 0.988
解码器占比 16.8%  talker 占比 83.2%
解码器：2.61 s / 8 次调用 ⇒ 326 ms/次
墙钟（提交→播完）= 16.29 s
```

⇒ **TTS 模型本身只有 ~1.0× 实时**（talker 自回归，一步一帧 = 80 ms 音频；
主机侧回调是 µs 级 ⇒ **不是 CPU 瓶颈，是 NPU 瓶颈**）。

⇒ **k = 1.175 > r = 0.95（单卡可持续速率）** ⇒ **产能天生不够**
⇒ 译文既跟不上源话，管道又会被吃空 ⇒ **顿挫**。

**顿挫是怎么长出来的**（逐句 gap：0 → 0.29 → 0.50 → 0.73 s，**单调变大**）：

```
gap_i ≈ A_i × (1/r − 1/tempo) + ε
       = 译文秒 × (1/0.95 − 1/1.08) + prefill(0.2 s)
```

⇒ **tempo = 1.08 > r = 0.95** ⇒ 每句边界都暴露一次「产能欠账」⇒
**越播越断**。

#### 3. 四层根因

| # | 根因 | 证据 | 修法 |
|---|---|---|---|
| **C1** | **产能天花板**：TTS 是 ~1.0× 实时的模型 | talker 占 83%、解码器 17%；主机侧不是瓶颈 | **同一个模型跑两张卡**（npu1+npu2）⇒ 聚合 ~1.94× |
| **C2** | **变速上限用错了口径**：`need_tempo` 用**边际速率**（把首窗+prefill 排除 ⇒ 系统性高估） | 上限算出 1.0，实际却按边际速率放到 1.08 | 上限改用**可持续速率**（整体速率）× **实例数** |
| **C3** | **棘轮锁死**：一轮内「只升不降」⇒ 变速一旦冲到上限就下不来 | 变速锁在 1.08，`need_tempo` 早已回落到 1.0 | 改成**升得快、降得慢**（死区 0.02 / 0.06） |
| **C4** | **每句的 prefill 死时间**（~0.2 s）暴露在句边界上 | 公式里的 ε 项 | **被多卡产能吸收**（管道有存货 ⇒ 不暴露） |

⇒ **注意**：变速是**零和**旋钮 —— 产能是瓶颈时，**变速变快 = 把语音换成
静音**（实测 tempo 1.08 时：语音 15.45 s + 静音 1.52 s = 17.0 s；
tempo 1.0 时：语音 16.65 s + 静音 0.68 s = 17.3 s —— 几乎一样）。
⇒ **只有把产能提上去，才能同时拿到「不拖尾」和「不断音」。**

#### 4. 修法

**① 多卡并行合成**（队列分片 + **收集器保序**）

* `TTSRequest` 加 `npu_index` ⇒ 会话**按卡位**解析
  （`registry.get_session(capability, npu_index=…)` —— registry 本来就支持
  同一个模型跑多张卡）。
* 插件 `service.py` 加 `get_session_for_npu(npu)`：**每个卡位一个会话对象**
  （共用一个对象的话第二个会被判「忙」⇒ 并行度退回 1）。
* 队列 `cards/tts/queue.py`：
  * N 个合成 worker（`_WORKER_COUNT = 3`），**租约**制 —— 每张卡同时只跑一句，
    起了几张实例就并行几张；
  * ⚠️ **分片 ⇒ 产出顺序 ≠ 提交顺序** ⇒ 直接压共享 FIFO 会让**后面的句子
    先播** ⇒ **收集器**（`_collect_loop`）按**提交顺序**（`_push_order`）
    把各卡产出的分片压进 FIFO ⇒ **FIFO 的「顺序 = 播放顺序」不变式原样
    保留** ⇒ **播放线程一行都没改**。
  * `clear()` 必须把**待合成**的句子从 `_push_order` 里去掉（否则收集器会
    等一个永远不会被合成的句子 ⇒ 后面全部卡住）。

**② 变速上限 = 聚合产能**

```python
capacity = self._sustain_rate * self._tts_instance_count()   # 0.95 × 2 = 1.9
need_tempo = max(t for t in TEMPO_LADDER if t <= capacity)     # = 1.15
```

⇒ 单卡时上限 = 1.0（老实话：单卡跑不动）；双卡时上限 = 1.15（够抵消 k）。

**③ 棘轮改成「升得快、降得慢」**（`TEMPO_DEADBAND_DOWN = 0.06`）⇒ 第二个
实例被停掉时变速能回落。

**④ autostart 加第二条 TTS**（`config.jsonc`）：
`qwen3-tts@1` **+** `qwen3-tts@2` ⇒ 板子重启后并行能力还在。

#### 5. 修后实测（双卡）

**跟播模式（follow）**：

```
句数              : 4
源媒体时长        : 15.05 s      源话音频合计 : 14.17 s
译文音频合计      : 15.96 s      长度比 k    : 1.126
播报总时长        : 15.12 s   ⇒ 播报/源 = 1.00×
（复测一轮 15.09 s / gap 依旧 0.000 s ⇒ **可复现**）
首句出声时刻      : +2.19 s（首窗延迟）
变速 tempo        : 1.15      need_tempo : 1.15
本机 TTS 实例     : 2 ⇒ 聚合产能 ≈ 1.94×
句间间隔 gap      : max 0.000 s / 合计 0.000 s / >0.1 s 的句数 0
句内饿死 starved  : max 0.00 s / 合计 0.00 s
```

**逐句时间线（并行证据）**（t=0 = 第一句提交）：

| seq | 提交 | 开始合成 | 首窗 PCM | 出声 | 播完 | 音频 | gap | 开口存货 |
|---|---|---|---|---|---|---|---|---|
| 1 | 0.00 | 0.00 | 2.05 | 2.19 | 5.39 | 3.20 | 0.000 | 0.00 |
| 2 | **0.45** | 0.45 | 2.49 | 3.41 | 9.37 | 3.98 | 0.000 | 1.99 |
| 3 | 3.69 | 3.69 | 5.72 | 9.68 | 14.67 | 5.74 | 0.000 | 0.00 |
| 4 | 6.90 | 6.90 | 8.89 | 11.04 | 17.31 | 3.04 | 0.000 | 4.18 |

⇒ 句 1 与句 2 **分别在两张卡上同时合成**（0.00 / 0.45 s 提交，各 2.0 s
后出首窗）⇒ **产能确实翻倍**；句 3/句 4 开口时管道里已有 **4.18 s 存货**
⇒ 句间零静音。

**now 模式**（不等播放头）：播报 16.38 s（1.09×）、gap 0、可听滞后 0.0 s。

#### 6. 每一段耗时（修后，跟播模式）

| 段 | 平均 | 最大 |
|---|---|---|
| ASR 切句 → 出队 | 0.00 s | 0.00 s |
| 等 MT 就绪 | 0.00 s | 0.00 s |
| **翻译推理** | **0.29 s** | 0.35 s |
| 翻译完 → 入队（放行器压了多久） | 3.72 s | 5.24 s |
| 入队 → 开始合成 | 0.00 s | 0.00 s |
| **合成首窗** | **2.03 s** | 2.05 s |
| 首窗 → 出声（跟播等播放头） | 1.79 s | 3.96 s |
| 出声 → 播完 | 5.11 s | 6.27 s |

⇒ **链路上最大的两段固定开销是「合成首窗 2.0 s」和「翻译完→入队」**
（后者是跟播语义的必然：译文要等播放头点到那一句）。

#### 7. 遗留与边界

* **首窗 2.0 s 还在**（每句第一块 PCM 要等 25 帧 = 2 s 音频产出）。
  再降到 12 帧能省 ~1 s，但会动到模型解码的上下文边界 ⇒ **音质风险**，
  本轮没动（面板上的「首句出声 +2.2 s」就是这个）。
* **k ≈ 1.13 降不下去**：译文天然比源话长 13%（中文的信息量/语速决定）
  ⇒ 变速 1.15 正好抵消 ⇒ **跟播时译文与源话基本同步**。
* **要两张卡**：单卡板的同传仍是「~1.0× 实时」⇒ 会回到 M25 的老问题
  （断音 + 拖尾）。**这不是代码能解决的** —— 是模型速度。
* **显存**：两个 TTS 实例 ≈ 2 × 1900 MB；本板 8 GB 内存，实测够用。

---

### M28 —— **把「每句的固定开销」摊薄**（2026-11-23 凌晨）

M27 的结论是「k ≈ 1.13 降不下去 ⇒ 变速 1.15 正好抵消」。本轮先把
**「每句的固定开销」**这层剥掉（内容层面的证据见 M29 的 A/B）。

| 改动 | 位置 | 效果（实测） |
|---|---|---|
| **合并窗口**：放行器把「几乎同时到」的几句合成**一段**送进 TTS | `interp.py::_gate_worker` | 一句 16 字的译文，一半时间是开销 ⇒ 合并后开销只付一次 |
| **首窗 12 帧**（上游硬等 50 帧 = 4 s 音频） | `wdnai/backends/tts.py::FIRST_WINDOW_FRAMES` + `interp.py::INTERP_FIRST_WINDOW_FRAMES` | 首声提前 ~1 s（25 帧 2.07 s → 12 帧 1.05 s） |
| **变速阶梯**含 < 1.0 的档（0.92 / 0.96） | `interp.py::TEMPO_LADDER` / `queue.py::TEMPO_LADDER` | 让「播放速度 ≤ 可持续产能」⇒ 存货不塌 ⇒ 收尾不断音 |

⚠️ **M28 的「降速防断音」后来被 M29 推翻**（它把播放拖到 0.96 ⇒ 译文
落后得**更快**）⇒ 见 M29 的改动 B。

---

### M29 —— **「ASR+翻译后，TTS 播放滞后非常多」**（用户报，2026-11-23 06:12）

#### 0. 结论先说

**滞后不是「某一段慢」，是三个量凑出来的**：

```
可听滞后(t) ≈ L0 + (k ÷ 产能 − 1) × t
```

* **L0** —— 起播延迟（ASR 出第一句 + 翻译 + TTS 首窗）≈ 3~4 s；
* **k** —— **长度比**（译文念出来比源话长多少倍）；
* **产能** —— TTS 每秒能产出多少秒音频（**每批**都有固定开销！）。

改前：**k = 1.104**、产能 **~0.81**（3 批 × ~1.3 s 合成死时间）
⇒ 增长 **0.23 s/源话秒** ⇒ 5 分钟素材落后 **~60 秒** ⇒ 「滞后非常多」。

#### 1. 怎么量的（探针 `tools/probes/interp/m29_lag_floor.py`）

量的是**用户真正感受到的那一口**：

```
可听滞后 a_lag = 译文「出声」的时刻 − 源话「开口」的时刻
```

⚠️ **不能用面板上的 `lag_sec`**（那是「播完 − 源话说完」另一个口径），
⚠️ 更不能用 `created_ts`（文件源是**离线一次性转写**的 ⇒ 所有句子的
`created_ts` 几乎相同 ⇒ 面板会算出「随句子数单调增长」的假数）。

探针**忠实复刻浏览器**：每 250 ms 上报播放头、**带 `stream_id`**
（带空 id ⇒ 服务端配不上 ⇒ 放行器走「卡住兜底」⇒ 整条链路晚 10 s 起跳）、
音量 0.03（自测要小声）。

#### 2. 改前的实测（15 s 英文素材，热态）

```
批次 3（4 句译文合并成 3 段）
k = 1.104（译文比源话长 10.4%）
边际产能 1.06 / 1.02 / 0.96   整体产能 0.99 / 0.80 / 0.89
变速 0.96（⚠️ 被 need_tempo 压到了 1.0 以下）
可听滞后 2.63 → 4.89 → 4.86 s（每秒源话 +0.20 s）
```

#### 3. 四层根因（每一层都实测过，不是猜的）

**① 标点停顿 —— 纯浪费的 2.2~4.2 秒**（A/B 实测，`m29_tts_punct.py`）

模型为**每一个标点**留一段静音，中文 TTS 实测**每个标点 0.25~1.0 秒**
（三段译文的标点停顿合计 2.2~4.2 秒，占音频 9%~30%）。
去掉标点：16.65 s → 14.01 s（**省 15.9%**）⇒ **k 1.104 → 0.989**。

**② 每批的合成死时间（~1.3 s）—— 批次越碎，产能越低**

首窗要等 12 帧才出第一块 PCM ⇒ **每批白等 ~1.3 s**。
3 批 = 3.9 s 死时间（占 15 s 素材的 26%）⇒ 产能只有 **0.81×**
⇒ 译文每秒落后 0.23 s。

**③ 变速被压到 1.0 以下 —— 「变慢」让滞后**更快**

`need_tempo`（= 实测可持续速率 0.96）把变速压到 0.96 ⇒
播放速度 = `min(tempo, 产能)` ⇒ **译文前进速度 = 0.96 ÷ 1.104 = 0.87×**
⇒ 每秒落后 **0.15 s**（比 tempo=1.0 的 0.10 s/s **多落后 50%**）。

⚠️ M28 的注释写「宁可滞后，也不要断音」—— **方向反了**：变慢 ⇒ 译文
落后得更快；断音只是**一次性**的空洞，滞后是**累积**的。

**④ 每段音频开头的静音（0.35~0.65 s）—— 直接加在可听滞后上**

模型每段开头都带一段静音 ⇒ 声音要等它走完才出来。

#### 4. 改法（4 处）

| # | 改动 | 位置 |
|---|---|---|
| **A** | **送进 TTS 的文本去掉 CJK 停顿标点**（字幕仍用原文 ⇒ 字幕不变） | `interp.py::_tts_text` |
| **B** | **变速下限锁 1.0**（`TEMPO_FLOOR`）—— 只用来「抵消长度比」，不用来放慢 | `interp.py::_refresh_status` |
| **C** | **文件 / 视频源按「内容量」攒批**（`MERGE_MIN_AUDIO_SEC = 18` 秒音频）⇒ 摊薄每批的固定开销 | `interp.py::_batch_ready` |
| **D** | **裁掉每段音频开头的静音**（`trim_head_silence`） | `tts/queue.py` |

⚠️ **C 的前提**：跟播模式的**对齐由「播放时钟」负责**（队列侧
`_clock_ready`）⇒ 放行器**不再等播放头** ⇒ 合成可以提前跑 ⇒
① 批次能攒大（摊薄开销）② 管道有余量（不断音）③ 攒批的延迟被
播放时钟吸收 ⇒ **攒得越久，播得越准**。

⚠️ **C 的分档按「源」不按「播放时机」**：文件 / 视频源是**离线转写**
（整个素材一次性转完）⇒ 攒批只多花「首句」的等待 ⇒ **两种播放时机
（now / follow）都按内容量攒**；**实时流（人在说话）低延迟优先** ⇒
仍按 1 秒的合并窗口攒（延迟 ≤ 1 s）。

⚠️ **C 的兜底上限只有 4 秒**（`MERGE_MAX_HOLD_SEC`）—— 实测踩过：
**ASR 服务被别的识别占用**（`current_request_id` 对不上 ⇒ 判不出
「这一轮跑完了」）⇒ 攒批只能等兜底 ⇒ 上限给 10 s 时实测**起播延迟
17 秒** ⇒ 兜底只保证「不无限期压着」，不承担攒批的职责。

⚠️ **「源已读完」只认当前这一轮**（`_batch_is_final` 比对
`current_request_id` 与流 id 里的 rid）—— 否则**上一轮**跑完留下的
`done` 会让**这一轮**的句子一翻译完就单独放行（攒批失效 ⇒ 每句都
付一次固定开销 ⇒ 产能上不去 ⇒ 滞后照旧）。

#### 5. 改后的实测（同一素材、同一探针）

| 指标 | 改前 | 改后 |
|---|---|---|
| 批次 | 3 | **1** |
| 长度比 k | 1.104 | **0.920**（译文比源话**短** 8%） |
| 整体产能 | 0.99 / 0.80 / 0.89 | **0.977** |
| 变速 | 0.96 | **1.00** |
| 播报总时长（源 15.05 s） | 17.92 s | **14.39 s** |
| **可听滞后** | 2.63 → 4.89 → 4.86 s（**+0.20 s/s**） | **3.74 s（恒定）** |

**60 秒素材（同一文件首尾相接 4 遍）—— 决定性的一测**：

```
可听滞后 6.60 → 2.48 → 0.09 s   ⇒ 每秒源话 −0.129 s（**在收敛**）
边际产能 1.074 / 1.081 / 1.062   整体 1.040 / 1.054 / 0.951
句间间隔 gap = 0     句内饿死合计 1.45 s（只有首句的起播）
```

⇒ 改前同一素材会涨到 **~18 s**，改后**收敛到 0.09 s**。

#### 6. 遗留与边界（诚实说明）

* **L0 ≈ 3.7 s 起播延迟还在**（ASR 出第一句 + 翻译 + 首窗 + 攒批）。
  这部分是「同声传译」的**物理下限** —— 真人同传也要慢 2~5 秒。
* **攒批的代价**：跟播模式第一批要攒够 18 秒音频才送进 TTS ⇒
  长素材的**首句**会晚 ~5 s（60 s 素材实测 6.6 s），之后**收敛**。
  ⇒ 阈值是「延迟」与「产能」的取舍（`MERGE_MIN_AUDIO_SEC`）。
* **实时流仍是 1 秒合并窗口** ⇒ 产能偏低（每批都付固定开销）
  ⇒ 长篇连续说话的**滞后仍会缓慢增长** —— 这是「低延迟」与「产能」的
  取舍，**没有两全**（真同传也是「越说越落后」）。
* **尾部静音**没裁（每段 ~0.1~0.45 s）—— 裁它要「扣住最后一块 PCM」
  ⇒ 收益小、风险大，暂不做。

#### 7. 回归

* `scripts/verify_interp_lag_floor.py`（22 条）—— 本轮的 4 个契约；
* `scripts/verify_interp_gate.py`（45 条）、`scripts/verify_tts_queue.py`（20 条）、
  `scripts/verify_interp_segmenter.py`（11 条）—— 既有契约**未破**；
* 探针：`tools/probes/interp/m29_lag_floor.py`（端到端）、
  `tools/probes/interp/m29_tts_punct.py`（标点 A/B）。

---

---

---

## 10. 实施顺序与验收





### S0（只测不写）→ 四张数据表
M6 / M7 / M8 / M1 的结果 + 结论 ⇒ 若 M6 不过 ⇒ 部署形态要改（**回到用户**重新决策）。

> ✅ **2026-11-21 已测**：M6 / M7 / M1 / M8 全部通过（数据见 §9.1）⇒
> **部署形态不变**（卡 0 = ASR + MT、卡 1 = 单实例 TTS、npu2 禁区）。

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

| 脚本 | 断言 | 结果 |
|---|---|---|
| `verify_interp_segmenter.py` | 快档切句器：LCP 连续 N 轮不变才成句；模型改写 ⇒ 作废候选；长度上下限 | **11/11** |
| `verify_interp_gate.py` | 放行器：`now` 直放；`follow` 等播放头；提前量；**seek 回退 ⇒ 清待播**；**播放头 stream_id 对不上 ⇒ 兜底回退**；**卡住兜底**（自动播放被拦 ⇒ 不再永久压住句子）且**区分「用户主动暂停」**；`stalled` 告警标志；**一次播放头都没上报过 ⇒ 同样兜底**；**会话切换清记账**（`reset()`）；**源播完 / 媒体失败 ⇒ 直接放行**；**媒体声称在播但播放头冻住 ⇒ 兜底** | **45/45** |
| `verify_tts_queue.py` | 队列：顺序、深度、合并、clear、tempo 动态改（未开始的才改）、**播放顺序 = 提交顺序**、**PCM 分片出口已挂上**；**M27 新增**：**并行合成（两张卡）⇒ 播放顺序仍是提交顺序**（收集器保序）+ **确实并发** | **22/22** |
| `verify_interp_api.py` | 5 个同传路由 + 4 个 TTS 队列路由：入参校验、状态字段、降级路径（无 TTS / 不可出声语种）、播放头按 `stream_id` 维护、**start 不带音色 ⇒ 保留已选音色** | **55/55** |
| `verify_interp_browser.py` | 真浏览器（**严格自动播放策略**）：面板控件、目标语只列 10 种可出声语言、音色、各源的播放时机可用性、file 源播放器、**端到端（切句→翻译→进 TTS 队列→播报）**、**开始同传 ⇒ 媒体真的开始播**、停止后清队列、全程无 JS 错误；**第二场：自动播放被拦 ⇒ 兜底放行 ⇒ 译文仍然出声**；**第三场：媒体先播完 ⇒ 剩下的译文照样全播**（`source_done`）；**第四场：媒体声称在播但播放头冻住 ⇒ 一句都不能丢** | **50/50** |
| `verify_interp_latency.py` | **滞后口径 + 首窗优化 + 重复识别复位**（M24）：媒体零点反推（只在**真在播**时更新 / `reset()` 清零）；**文件源**的基准 = 媒体零点 + `src_end_sec`（**不是** `created_ts`）；**实时流**仍用 `created_ts`；首窗帧数一路透传到 `TTSJob`；宿主桥形参；**本机 TTS 补记 autostart**（含「peer 的实例不记」）；**新一轮识别 ⇒ 待播/翻译队列/TTS 队列复位 + 放行器换流（但保留「源已结束」这个媒体事实）** | **22/22** |
| `verify_interp_seek.py` | **seek（拖进度条）处理**（M26）：**双向**判定（前跳按「比两次上报的真实时长多跳了多少」⇒ 媒体**缓冲补跳**不算、**暂停后恢复**不算）；引擎重同步（**新位置之前的待播句被丢** + **新位置之后的句子被重新武装/重播** + 待播队列**按位置排序**）；队列 `flush_by_position`（丢 job / 丢陈旧 PCM / **作废在合成的那句** / **复位播放时钟**）；**被作废的句子不能堵住 FIFO**（⚠️ 用**假 writer**，不出声；实测这条断言在旧顺序下**真的失败** ⇒ 回归有效） | **32/32** |

> 📌 原计划里的 `verify_interp_recheck.py` / `verify_interp_playhead.py`
> **没有单独出脚本** —— 复核与播放头的断言分别并进 `verify_interp_api.py`
> 和 `verify_interp_gate.py`（避免同一套语义散在两个文件里漂移）。

**真机探针**（`tools/probes/interp/`，一次跑出多张数据表）：

| 探针 | 验什么 | 结果 |
|---|---|---|
| `m1_lang_expansion.py` | 语言对膨胀比 k 表 | k ≈ 0.61~1.17（8 种语） |
| `m6_same_card_contention.py` | 卡 0 上 ASR + MT 同卡争用（文件路径） | 轮间隔不膨胀 |
| `m6b_live_contention.py` | 卡 0 争用（**实时**路径）+ **M8** 麦上行丢块基线 | lag 不膨胀 / 丢块 0 |
| `m7_sox_tempo.py` | sox `tempo` 精度 / 音高 / 板载 codec | 偏差 <0.01% / 440 Hz 不变 |
| `m9_live_interp.py` | **浏览器麦上行 → 同传** 端到端（快/稳两档） | 切句→翻译→播报全通 |
| `m10_tempo_pipeline.py` | **TTS 卡里**的变速管道（sox→aplay）真机 | 倍率 1.148 / codec 打开 |
| `m11_steady_lag.py` | **文件源 + 稳档**的滞后时间线（切句/翻译/入队/播完逐秒） | 冷启动零丢句 / 4 句全播完 |
| `m12_live_lag.py` | **实时流 + 稳档**的滞后时间线（决定性实验） | 滞后收敛在 5.8~13.3 s（改前 8.2→**23.2 s 持续增长**） |
| `m13_tts_throughput.py` | **TTS 队列的真实吞吐**（单句 + 批量） | 0.67× → **0.93× 实时** |
| `m14_browser_media.py` | **浏览器里「有翻译没声音」**的诊断（媒体元素 + 播放头 + 放行三处一起看）；**两场**：手势能播 / **自动播放被拦 ⇒ 兜底放行** | 定位到播放头 `stream_id` 对不上 + **会话记账残留** |
| `m15_tts_level.py` | **TTS 实际出声电平**（同一句话 × 4 档音量，量真正送进 aplay 的峰值） | 0.1 ⇒ **−32 dBFS（听不见）** / 0.5 ⇒ −14 dBFS |
| `m16_file_follow.py` | **文件源 + 跟播放**的逐秒时序（模拟浏览器播放头 + **媒体播完后的冻结状态**） | 抓到「切了 34 句、只送出 28 句」⇒ 6 句卡在放行器 ⇒ **M21 的现场** |
| `m17_browser_first_sentence.py` | **浏览器里按用户操作序列**（填路径 → 开始同传 → 开始识别）逐秒复现 | 定位到「媒体播完后句子被永久压住」 |
| `m19_latency_breakdown.py` | **全链路分段耗时明细**（M24）：翻译排队 / **等 MT 就绪** / **纯推理** / 放行等待 / 队列排队 / **合成首窗** / 出声启动 / 播完 / 端到端滞后 + 产能比；**默认先热身**（否则测的是冷启动） | 冷态：等 MT 24.1 s；热态：等 MT 0.00 / 推理 0.28 / 首窗 3.23→**2.03** / 滞后 **5.1~5.7 s（平）** |
| `m20_tts_first_window.py` | **TTS 首窗 A/B**（25 帧 vs 50 帧）：⚠️ 采样生成 ⇒ **同配置两次**取噪声基线，再比跨配置；边界连续性 | 跨配置差异**落在噪声底内** + 接缝 0.08× ⇒ 音频未劣化 |
| `m21_pipeline_metrics.py` | **播放侧体检**（M25）：逐句 `gap`/`starved`/`stock`/`synth_lat`/`a_lag`/`m_rate` + **长度比 k** + **产能缺口**；⚠️ **自带「模拟浏览器播放头」** ⇒ 不用开浏览器就能量跟播 | 句间 **0.03~0.06 s** / 句内饿死 **0.00 s** / 边际产能 **1.047×** / k **1.200** / 缺口 **12.7%** |
| `m22_aplay_rate.py` | **aplay 到底是不是按 1.0× 实时消费**（M25）—— ⚠️ **度量本身要先被度量**（不验它，报出来的「断音」全是假的） | 消费速率 **精确 1.000×**（修正 `_tick` 之后） |
| `m23_seek_sync.py` | **seek 场景的同步体检**（M26）：识别**含音轨的视频** ⇒ 等 ASR 追上播放头 ⇒ 模拟播放头按剧本**前跳 / 后跳** ⇒ 逐句算 **`err = 出声时的播放头 − 源话位置`**（≈0 才算「同声」） | 修前 **17 句里 15 句**错位（**+27~+35 s / −20~−31 s**）⇒ 修后 **4/23**（剩下的 4~8 s 是 M25 的**结构性产能缺口**，不是 seek 引入的） |
| `m24_browser_seek.py` | **浏览器里真拖进度条**（M26）：**真浏览器**打开 ASR 卡的「含音轨的视频」⇒ 起同传（跟播放）⇒ 开始识别 ⇒ 真的改 ``video.currentTime`` ⇒ 断言**服务端认出了前跳 / 后跳**（`status().last_seek.dir`）+ **位置对得上** + **后跳之后重播了**（`replayed` 增量）+ **面板出现「已跟随进度条重新对齐」** + 无 JS 错误 | **12/12**（前跳 `fwd` / 后跳 `back` / 位置误差 < 4 s / 重播 > 0 / 面板回执 ✓） |
| `m27_tts_capacity.py` | **TTS 产能体检**（M27）：把已知文本灌进队列 ⇒ 从 worker 日志解析 **[talker+decoder] / [decoder]** ⇒ **产能 r** + **talker vs 解码器的耗时拆分**（判定「能不能提产能」）| 音频 15.32 s / 合成 15.51 s ⇒ **r = 0.988**；**talker 83% / 解码器 17%** ⇒ 瓶颈是模型自回归 ⇒ 只能靠**多卡** |
| `m27_en2zh_e2e.py` | **EN→ZH 端到端体检**（M27）：**基线增量**口径（队列 timings 累计 ⇒ 先取 max(seq)）⇒ 播报总时长 / **k** / **gap** / **starved** / 产能 / **每一段耗时**（切句→出队 / 等MT / 推理 / 放行等待 / 排队 / 首窗 / 出声 / 播完）| 修前 17.20 s（1.14×）+ gap 1.52 s ⇒ 修后 **15.12 s（1.00×）+ gap 0.000 s** |

⚠️ **回归必须 mock `PUT`/`DELETE /api/compute/active-app`**（用 `scripts/active_app_guard.install(ctx)` + `install_active_app_spoof(ctx)`，且**须在 `ctx.new_page()` 之前**）⇒ 服务端配置一个字节都不动。
⚠️ **别并发跑回归脚本**。

**既有必须全绿**（改插件 `.py` 后要 `sudo systemctl restart wdnwebui`）：
`verify_asr_translation.py` / `verify_asr_video_dual_subtitle.py` / `verify_asr_file_translate_ui.py` / `verify_asr_file_translation_e2e.py` / `verify_asr_seek_subtitle.py` / `verify_asr_wizard.py` / `verify_asr_translate_lang_labels.py` / `verify_asr_live.py` / `verify_asr_model_degeneration.py`。
⚠️ `verify_asr_wizard_translate` 结尾会停掉翻译模型；`verify_asr_translate_lang_labels` 结尾把 `enabled` 设回 false ⇒ **跑完要复原**。
⚠️ **重启会连坐杀 `compute_worker`** ⇒ 重启前确认 `compute.autostart` 有值。

> 🐛 **踩到的坑（已修在脚本里）**：`verify_asr_wizard.py` 的 `click_to_finish()`
> 原来用 `document.getElementById('… #wd-wizard-finish-btn')` 判断「到完成步了」——
> **getElementById 只吃纯 id**（不吃 CSS 选择器）⇒ 恒为 null ⇒ 它永远点「下一步」
> ⇒ 走到「完成配置」步（那一页**没有**「下一步」按钮）时 `page.click` 等 30 s 超时
> ⇒ **整个向导回归在视频路径前就崩了**（症状看着像「向导卡住」，其实是脚本写错了）。
> ⇒ 已改成 `querySelector`。

---

## 12. 交付物清单

| # | 交付物 | 位置 |
|---|---|---|
| 1 | 实施方案（本文） | `docs/SIMULTANEOUS_INTERPRETATION_IMPL.md` |
| 2 | 可行性报告（硬件实测 / 市面对比 / 产能推算） | `docs/SIMULTANEOUS_INTERPRETATION_PLAN.md` |
| 3 | 硬件验证探针 | `tools/probes/interp/`（M1 / M6 / M6b / M7） |
| 4 | 插件改动 | `.venv/src/wdnapps-audio-chat/`（S1 起） |
| 5 | 宿主改动 | `app/core/audio/tts_bridge.py`（S1 起） |
| 6 | 回归脚本 | `scripts/verify_interp_*.py`、`scripts/verify_tts_queue.py` |
| 7 | **分段耗时探针**（M24） | `tools/probes/interp/m19_latency_breakdown.py`、`m20_tts_first_window.py` |
| 8 | **SDK 改动**（首窗 25 帧，M24；**空文本不再起 aplay**，M25） | `.venv/src/wdnai/src/wdnai/backends/tts.py`（`FIRST_WINDOW_FRAMES` + `first_window_frames` + 空文本前置校验） |
| 9 | **播放侧体检探针**（M25） | `tools/probes/interp/m21_pipeline_metrics.py`、`m22_aplay_rate.py` |
| 10 | **共享 FIFO + 播放时钟**（M25） | `.venv/src/wdnapps-audio-chat/src/wdnapps_audio_chat/cards/tts/queue.py`（`_fifo_*` / `set_clock`） |
| 11 | **seek 同步探针**（M26：API 层模拟播放头 + 真浏览器拖进度条） | `tools/probes/interp/m23_seek_sync.py`、`m24_browser_seek.py` || 12 | **seek 处理**（M26：双向判定 / 冲掉陈旧音频 / 重播缓存 / FIFO 死锁） | 插件 `cards/qwen3_asr/interp.py`（`Gate.update_playhead` / `_resync_seek`）、插件 `cards/tts/queue.py`（`flush_by_position` / 播放线程判定顺序）、宿主 `app/core/audio/tts_bridge.py`（`queue_flush_position`） |
| 13 | **seek 回归**（M26） | `scripts/verify_interp_seek.py`（32/32） |
| 14 | **产能体检探针**（M27） | `tools/probes/interp/m27_tts_capacity.py`（talker vs 解码器拆分） |
| 15 | **EN→ZH 端到端探针**（M27） | `tools/probes/interp/m27_en2zh_e2e.py`（基线增量口径 + 每一段耗时） |
| 16 | **多卡并行合成**（M27） | 插件 `cards/tts/queue.py`（`_lease_npu` / `_collect_loop` 收集器保序 / `_push_order`）、插件 `cards/tts/service.py`（`get_session_for_npu`）、宿主 `app/core/audio/tts_bridge.py`（`TTSRequest.npu_index`） |
| 17 | **第二个 TTS 实例**（M27） | `config.jsonc` 的 `compute.autostart` 加 `qwen3-tts@2` ⇒ 板子重启后并行能力还在 |

---

*附：本文的所有板端数字来自本机实测（`tools/probes/interp/`、`/api/compute`、`rknnsmi` 遥测），非厂商宣传值。**M1 / M6 / M7 / M8 已实测（2026-11-21，数据见 §9.1）**。*
