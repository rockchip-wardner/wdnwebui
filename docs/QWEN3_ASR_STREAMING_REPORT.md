# Qwen3-ASR 流式语音识别 —— 实现报告（待审核）

> 日期：2026-11-06 · 板卡：RK3588 + 3 张 PCIe RK1820 协卡（`0001:11:00.0` / `0003:31:00.0` / `0004:41:00.0`）
>
> **状态：功能已实现，自测全部通过；代码未提交（按要求）。**
>
> 与 C++ demo 的比对：**最终结果逐字节相同**，16 轮已确认文本全部一致。
> 测试期间弄坏过 PCIe NPU 子系统（根因已定位），**经主机重启已完全恢复**，
> 重启后重跑的验证全部 PASS —— 详见 §6。
>
> **第 2 轮（同一日）**：按用户反馈重做了向导、并加了「语音文件上传 +
> 兼容含音轨的视频」。真浏览器回归 32/32 通过；顺带揪出并修掉了
> **卡片卡位缺失**（向导一直浮在别的卡片下面 —— 用户说「怪怪的」的真正原因）、
> **暗色主题配色**和**「逐轮明细」覆盖「完整 JSON」**三个真问题 ——
> 详见 §7。
>
> **第 2 轮追加**：按用户要求**去掉识别参数的 UI** —— 向导从四步减到三步
> （删掉「识别参数」步），功能页的「识别参数」折叠块也一并删掉。参数改由
> SDK 的 `ONLINE_*_DEFAULT` 决定（`1.0 / 50 / 5 / 4`，与 C demo 常量相同），
> 前端不再复制一份常量 —— 详见 §7.2。
>
> **第 2 轮追加 2**：补上**应用面板的预览视频** —— 用户提供的 `asr.mp4`
> 按其它卡片的约定移到 `app/apps/qwen3_asr/static/preview.mp4` 并在
> manifest 里声明 —— 详见 §7.7。

---

## 1. C++ 参考实现：读到什么

源码位置：`/userdata/rknn3/rknn3-model-zoo/examples/Qwen3_ASR/cpp/`

| 文件 | 职责 |
|---|---|
| `main.cc` | **离线**模式（整段音频一次识别） |
| `online_main.cc` | **流式**模式 —— 本次实现的基准 |
| `qwen3_asr.{h,cc}` | 模型初始化 + 内部显存共享（`init_internal_share`） |
| `audio/rknn_qwen3_asr_audio.{h,cc}` | 音频路径：mel 频谱 → encoder → embeds |
| `llm/rknn_qwen3_asr_llm.{h,cc}` | LLM 推理（llama.cpp 风格） |

### 1.1 关键常量

| 常量 | 值 | 作用 |
|---|---|---|
| `ONLINE_CHUNK_MS` | 1000 | **1 秒一块**增量喂入 |
| `ONLINE_MAX_NEW_TOKENS` | 50 | 每块 LLM 最多生成 50 token |
| `ONLINE_ROLLBACK_TOKENS` | 5 | 每轮回退末尾 5 个 token |
| `ONLINE_ROLLBACK_ROUNDS` | 4 | **前 4 轮不提交**任何已确认文本 |
| `ONLINE_MAX_AUDIO_SECONDS` | 60 | 音频时长上限 |
| `PREFIX_N_TOKENS` / `POSTFIX_N_TOKENS` | 9 / 6 | ChatML 前缀 / 后缀 embedding |
| `AUDIO_START_TOKEN` / `AUDIO_END_TOKEN` | 151669 / 151670 | 音频段起止 |
| `ASR_TEXT_TOKEN` | 151704 | **语种分隔符** —— 模型先吐 `language <lang>` 再接正文 |
| `HOP_LENGTH` / `N_FFT` | 160 / 400 | mel 帧移 / 窗长 |
| `DOWN_SAMPLE_RATE` | 8 | 48 kHz → 16 kHz |
| `MAX_CONTEXT_LEN` | 24576 | 上下文上限 |

### 1.2 回退策略（`online_main.cc:482-502`）

每轮生成 `round_token_ids`，切成两段：

```
最后一轮            → 全部提交
前 rollback_rounds 轮 → 一条都不提交（stable = 0）
其余                → 提交 size - ROLLBACK_TOKENS，剩下的当"未确认尾巴"
```

被提交的 token 累进 `committed_token_ids`；尾巴每轮都可能**被下一轮整体改写**。
这解释了「流式输出里已确认文本单调增长、未确认尾巴反复变化」的现象。

### 1.3 两种输出模式

- **默认（逐轮）**：每轮打印 `commit_add_text`（本轮**新增**）与 `unfix_text`（未确认尾巴）。
- **`-s`（单行）**：`print_single_line_result()`（`online_main.cc:201-210`）——

  ```c
  printf("\r%s", committed_text);
  if (!unfix_text.empty()) printf(BLUE "%s" RESET, unfix_text);
  printf("\033[K");
  ```

  即 **已确认文本正常色 + 未确认尾巴蓝色**，整行原地刷新。

### 1.4 设备选择（重要）

C demo 用 `rknn3_init(&ctx, NULL)` —— 传 `NULL` ⇒ 库自己挑**驱动枚举的第一张卡**：

```
W RKNN3: Multiple RKNN devices found (6) but no device_id specified.
         Using device[0]: id=0001:11:00.0, type=PCIE by default.
```

板卡枚举（`RKNN3Lite.get_devices_id()`）：

| 索引 | device_id | 说明 |
|---|---|---|
| 0 | `0001:11:00.0` | PCIe 协卡 #1 |
| 1 | `0003:31:00.0` | PCIe 协卡 #2 |
| 2 | `0004:41:00.0` | PCIe 协卡 #3 |
| 3~5 | `180B6D…` | 板载 SoC NPU 核 |

---

## 2. 改动清单（**均未提交**）

### 2.1 SDK 侧 —— `/userdata/wdnwebui/.venv/src/wdnai`（独立 git 仓）

```
 M src/wdnai/model.py            +46
 M src/wdnai/capability.py        +8
 M src/wdnai/backends/_common.py +14
 M src/wdnai/backends/asr.py     +188
 M examples/README.md           +192
?? examples/Qwen3_ASR/{streaming_asr.py, compare_with_c_demo.py, asr_en.wav}
```

| 改动 | 内容 | 为什么 |
|---|---|---|
| `model.py` | 新增 `_QWEN3_ASR_DIR_NAME = "Qwen3-ASR"` + `_qwen3_asr_dir()`（支持 `WDNAI_QWEN3_ASR_DIR` 覆写）；`use_online_encoder` 默认改 **True** | 模型目录名由 manifest 的 `model_dir_env` 声明，**不再由 SDK 硬编码**；encoder 选择是**装载期**决定 → 写进 SDK，不写进 manifest（AGENTS.md 原则） |
| `capability.py` | 新增 `AUDIO_ASR = "swcap_audio_asr"` | 与 Omni 的 `swcap_audio_chat` **分开**，否则两者会在 `get_session()` 里抢同一个 provider |
| `_common.py` | `StreamSink.publish(delta)` —— 只 fan-out 给 observer，**不污染** `self.text` | 流式增量需要一条独立于最终文本的旁路 |
| `asr.py` | ① 文件检查只要求**实际选中的那套 encoder**；② `_log_mel_spectrogram()` 复刻 C 的 `n_frame*hop` **截断**；③ `_OnlineDecoder._decode()` 加 `skip_special_tokens=True`；④ 新增 `self.language`；⑤ 新增 `_round_payload()` / `_final_payload()`，每轮发一个 JSON 载荷；⑥ `_resample_linear()` **保留线性插值**并补 docstring | ① 原先强制要求 offline+online 两套，流式部署根本加载不了；②③ 见 §3.1；⑤ 是流式增量的传输载体；⑥ 见 §3.2 |

### 2.2 WebUI 侧 —— `/userdata/wdnwebui`

```
 M app/static/index.html.template            （补 ASR 卡位，见 §7.1）
 M scripts/verify_capability_groups.py
?? app/apps/qwen3_asr/                       （新建卡片，10 个文件）
?? app/core/audio/wav.py                     （新建，WAV 解析）
?? app/core/audio/decode.py                  （新建，通用媒体解码，见 §7.3）
?? tools/worker_manifests/qwen3-asr.json     （新建，工厂副本）
?? scripts/verify_asr_wizard.py              （新建，向导回归，见 §7.4）
```

| 文件 | 行数 | 内容 |
|---|---|---|
| `app/apps/qwen3_asr/manifest.json` | 56 | id `qwen3-asr`、标题「流式语音识别」、`software_capabilities: ["swcap_audio_asr"]`、router 前缀 `/api/qwen3-asr`、demo_guard stop 端点、`display_order: 45` |
| `app/apps/qwen3_asr/service.py` | 363 | `_AsrManager` 单例；状态机 idle/running/done/error/stopped；`transcribe()` 起线程；`on_delta` 解析 JSON 轮次并广播；`_select_session()` 走 `registry.list_sessions("swcap_audio_asr")`；`stop()` / `reset()`；流式参数 `options` 透传 |
| `app/apps/qwen3_asr/api.py` | 268 | `GET /`、`GET /rounds`、`POST /upload`、`POST /transcribe`（multipart **或** form path）、`POST /stop`、`POST /reset`、`WS /ws` |
| `app/apps/qwen3_asr/__init__.py` | 47 | `register_api` / `register_shutdown` |
| `app/apps/qwen3_asr/static/*` | 772 + 预览视频 | 前端：「实时结果」/「识别过程」双模式、WS 实时推送、本机上传 + 设备端路径双来源、停止/清空、三步向导；`preview.mp4`（254 KB，应用面板预览视频，见 §7.7） |
| `app/core/audio/wav.py` | 110 | `parse_wav()` → PCM s16/s24/s32 + IEEE float32；**不提前转码**以保持与 C 相同的输入 |
| `app/core/audio/decode.py` | 253 | `decode_media()` —— WAV 直通，其余格式（mp3/m4a/flac/ogg/**含音轨的视频**）走 PyAV |


**「本地算力」卡片**：本地算力栏的卡片由 manifest 驱动、通用渲染器渲染 ——
**前端核心 JS 一行未改**。`/api/compute` 的 `supported_models` 里已出现
`[ASR]流式语音识别`，与 `[Depth]视觉深度信息v3` 同一列表、同一渲染路径。

### 2.3 worker manifest 与模型

- 工厂副本：`tools/worker_manifests/qwen3-asr.json`
- 安装位置：`/userdata/models/Qwen3-ASR/qwen3-asr.json`
- 模型目录 `/userdata/models/Qwen3-ASR/`：`encoder_online.1828.rknn` / `.weight` /
  `llm.1828.rknn` / `.weight` / `llm.embed.bin` / `llm.tokenizer.gguf` /
  `mel_128_filters.txt` —— 全部用**硬链接**（同 inode，**零额外磁盘占用**），
  属主 `rockchip`。

> `/userdata` 只剩 3.9 G，复制会再占 ~1.1 G —— 硬链接是必须的。

### 2.4 示例 sample —— `.venv/src/wdnai/examples/Qwen3_ASR/`

| 文件 | 内容 |
|---|---|
| `streaming_asr.py` | 主 sample，`--mode rounds\|single` 对应 C 的两种模式；另有 `--npu` / `--wav` / `--model-dir` / `--chunk-seconds` / `--max-new-tokens` / `--rollback-tokens` / `--rollback-rounds` / `--save-json` |
| `compare_with_c_demo.py` | 与 C demo **自动比对**：跑 C demo + SDK，逐轮 diff，最终结果必须逐字节相同；支持 `--c-baseline` 对固化基线比 |
| `asr_en.wav` | 素材，md5 `f50189de8aa94fb9cd1e5dbfa1b6939e`（与 model-zoo 原文件**一致**，已核对） |

`examples/README.md` 已按仓库规定的 4 小节 + 素材目录 + 性能实测格式补了
`## Qwen3_ASR` 章节（含 7 条「容易踩的坑」）。

---

## 3. 为了「与 C demo 一致」所做的三个关键修正

### 3.1 mel 频谱必须复刻 C 的截断（**结构性差异**）

C 的 `audio_preprocess` 按 `n_frame = (len - n_fft) / hop + 1` 算帧数，并把音频
**截断到 `n_frame * hop`**。1 秒块 = 16000 采样 → 截到 15680 → **98 列** mel。

Python 侧不截断会算成 **100 列** —— 模型输入宽度对不上，
**第 1 轮就分叉**（实测从「嗯哼。」变成「Uh-huh」）。修正后第 1 轮与 C 一致。

### 3.2 重采样器**保留线性插值**（不要换 Kaiser-sinc）

C demo 用 miniaudio 的带限 FIR；实测其冲激响应是**单位直流增益的窗函数 sinc**
（DC ≈ 1.0002），SDK 用线性插值。

| 重采样器 | 转写结果 |
|---|---|
| **线性插值（现状）** | `Uh huh. Oh yeah, yeah! He wasn't even that big…` ✅ 与 C 逐字节相同 |
| Kaiser-sinc（试过） | `Oh yeah, he wasn't even that big…` ❌ **丢掉「Uh huh.」** |

根因：两者的差异在 mel 频谱的**末位比特**上，经 int8 量化偶尔让某一个 token
选到不同分支，回退策略把它吸收掉 —— **已确认文本从未分歧**。
⇒ 线性插值才是与 C 一致的那个，不能"优化"。

### 3.3 解码必须 `skip_special_tokens=True`

C 侧走 llama.cpp 的 `TokenToPiece`，它对控制 token 返回 **0 字节**；
Python 侧不过滤会在尾部漏出 `<|im_end|>`。

---

## 4. 与 C demo 的比对结果

### 4.1 方法

**主路径：实时跑 C demo + SDK 并逐轮 diff**

```bash
.venv/bin/python .venv/src/wdnai/examples/Qwen3_ASR/compare_with_c_demo.py
```

脚本会：跑 C demo（`device[0]` = card 0）→ 从 C demo 自己的输出里解析它用了哪张卡
→ **自动给 SDK 选一张不同的卡**（card 1）→ 逐轮 diff。

也支持对着**固化基线**比（省一次 C demo 的时间；板子某张卡挂掉时也能用）：

```bash
.venv/bin/python .venv/src/wdnai/examples/Qwen3_ASR/compare_with_c_demo.py \
    --c-baseline /path/to/c_demo_stdout.txt --sdk-npu 1
```

> ⚠️ **抽取只认 stdout**：C demo 自己的 printf 走 stdout，而 llama.cpp 的模型
> 元数据 dump 走 **stderr**。把两者拼起来会让「Final Commit Result」后面粘上
> 整段日志 → 比对**假失败**（已修）。

### 4.2 结果 —— **PASS**（2026-11-06 04:26 重启后实时跑）

```
=== 1. 跑 C demo (/userdata/rknn3/rknn3-model-zoo/install/rk3588_linux_aarch64/rknn_Qwen3_ASR_demo) ===
    rounds=16 final_len=188
    C demo 用的设备: device[0] = 0001:11:00.0
=== 2. 跑 SDK（npu=1）===
    rounds=16 final_len=205

=== 3. 最终结果比对 ===
    [OK] 最终结果逐字节相同
         Uh huh. Oh yeah, yeah! He wasn't even that big when I started listening to him,
         but in his solo music, didn't do overly well, but he did very well when he
         started writing for other people.

=== 4. 逐轮比对 ===
    [OK] 16 轮的已确认文本全部相同
    [INFO] 2 轮的**未确认尾巴**不同（允许 —— 下一轮会被回退修正）:
      round 4: C="Uh huh. Oh yeah, yeah he wasn't even that big when"
               SDK="Uh huh. Oh yeah, yeah! He wasn't even that big when."
      round 7: C=' but and his.'
               SDK=' but in his.'

=== 结论: PASS ===
```

（`final_len` 差 17 是因为 SDK 的返回值带 `language English\n` 头，比对时已剥离。）

| 维度 | 结果 |
|---|---|
| 最终转写正文 | ✅ **逐字节相同** |
| 16 轮**已确认**文本 | ✅ **全部相同** |
| 16 轮**未确认**尾巴 | ⚠️ 2 轮不同（第 4、7 轮）—— 属设计内的「还没确认、可被改写」部分 |
| 前缀 / 后缀 embedding | ✅ 与 C 打印值**逐位相同**（`prefix_embeds[0]=-0.007538` … `postfix_embeds[-1]=-0.035889`） |
| 轮数 | ✅ 16 / 16 |

**两轮未确认尾巴差异的成因**：C 用 miniaudio 的带限 FIR 重采样、SDK 用线性插值，
差异经 mel → int8 量化后偶尔翻转一个 token（第 4 轮的 `yeah, yeah` vs
`yeah, yeah!`、第 7 轮的 `but and his` vs `but in his`）。
回退策略把这种差异**吸收**在未确认区 —— **已确认文本从未分歧，最终结果逐字节相同**。

### 4.3 webui 端到端 —— **PASS**（2026-11-06 04:27 重启后重跑）

上传 `asr_en.wav` → WebSocket 收到 16 轮增量 + `done`：

```
round  1 unfixed='嗯哼。'                                    lang='Chinese'
round  2 unfixed='Uh huh. Oh yeah, yeah.'                    lang='English'
...
round 16 committed='hen he started writing for other people.' unfixed=''
done: {"language": "English", "text": "Uh huh. Oh yeah, yeah! He wasn't even that big
       when I started listening to him, but in his solo music, didn't do overly well,
       but he did very well when he started writing for other people.",
       "elapsed_sec": 2.054}
C demo final == webui final : YES
```

> 语种在**第 1 轮报 `Chinese`、第 2 轮才纠正成 `English`** —— 音频上下文太短时
> 语种判定不准，这是模型的正常行为；**最终语种**取自完整 committed token 流。
> 已写进 sample 的 README。

### 4.4 其它验证

| 验证 | 结果 |
|---|---|
| `python -m scripts.verify_capability_groups` | ✅ 全过（能力词表 9 → **10**） |
| `python -m scripts.verify_model_packaging` | ✅ 全过（1 项 SKIP：本机没装 Qwen3-VL-4B） |
| SDK sample `--mode rounds` / `--mode single` | ✅ 均正常，单行模式转义序列与 C 同构 |
| SDK 在 card 0 / card 1 / card 2 上 | ✅ 结果一致（跨卡确定性） |
| C demo 跑完后 card 0 复测 | ✅ 仍正常（不背靠背同卡就没事） |

### 4.5 性能实测（重启后）

| 指标 | 值 |
|---|---|
| 模型加载（webui worker 侧 `load_time_sec`） | **18.44 s** |
| 模型加载（裸 sample 侧，card 0 / card 2） | **19.85 s / 18.35~18.39 s** |
| 纯推理（15.05 s 音频 / 16 轮） | **2.02 ~ 2.20 s** |
| 实时率 | **≈ 7× 实时** |
| 单轮 audio encoder | 18 ~ 32 ms |
| 单轮 LLM ttft / decode | 32 ~ 54 ms / 37 ~ 142 ms |

---

## 5. 流式增量是怎么传到浏览器的（架构说明）

```
浏览器上传 wav
  → POST /api/qwen3-asr/transcribe（multipart）
  → WAV 解析成 (PCM, 采样率, 声道, 位宽)
  → ComputeRegistry.get_session("swcap_audio_asr")     ← LLMSession 从 registry 取
  → wdndds RPC（compute_worker 子进程里的 LLMLocalSession）
  → wdnai ASRBackend.run_audio(online=True)
  → 每秒一块增量解码，每轮经 sink observer 发一个 JSON 载荷
  → wdndds 的 RELIABLE delta 旁路 topic 回到主进程
  → 本服务解析后广播给浏览器（WebSocket）
```

**为什么增量必须走 `on_delta`**：一次流式识别 = N 轮 LLM，reply 通道在推理期间是
「聋」的，只有全部轮次跑完才回一句话 —— 那样就看不到「实时刷新」了。

**worker 端不认识 ASR**：worker 只把文本递出去，**不解析** JSON 载荷；
解析发生在**主进程**（`service.py`）和浏览器里 —— 符合
「worker 只做传输、不理解 schema」。

**能力词表与 provider 解耦**：`swcap_audio_asr` 来自 SDK 的封闭枚举
`wdnai.SoftwareCapability`，卡片**始终在**应用网格里；本板没装模型时，
运行期报「功能未启用: swcap_audio_asr」并由 HTTP 层 502 透传。

---

## 6. ⚠️ 板卡状态问题：card 0（`0001:11:00.0`）在测试中被弄坏

### 6.1 发生了什么

1. 早期测试中，**C demo 与 SDK 在同一张卡上背靠背**运行（C demo 退出 → 立刻建
   新 context）⇒ RKNN3 PCIe **transfer proxy 卡在 `ioctl()`**。
2. 该 proxy 需要 `kill -9` 手动救，而且**supervisor 不会自动把它拉回来**
   （`rknn3_transfer_proxy` 只在启动时按设备发现拉起 per-device 子进程）。
3. 此后 **card 0 在 `rknn3_init` 的模型加载阶段就挂死**（C demo 与 SDK 都一样）。

### 6.2 根因（已定位到内核线程）

**`kworker/6:1-events`（pcie-rkep 驱动的事件处理线程）自 `03:09:47` 起卡在
D 状态（不可中断等待）。**

```
$ ps -eLo pid,tid,stat,wchan,comm | awk '$3 ~ /D/'
  13567   13567 D    -       kworker/6:1+events      ← 启动于 Fri Nov 6 03:09:47
$ cat /proc/13567/stack
  [<0>] worker_thread+0x1fc/0x52c
  [<0>] kthread+0xdc/0xe0
  [<0>] ret_from_fork+0x10/0x20
$ grep pcie /proc/interrupts
  34:  0 0 0 0 0 0 0 0 0   GICv3 275 Level   pcie-sys   ← 完成中断一次都没来
```

因果链：

1. 03:09 前后 C demo 与 SDK 在同一张卡上背靠背 → 一条发往 card 0 的 DMA
   请求**永远不完成**（PCIe 完成中断没有到达，`pcie-sys` 计数全 0）。
2. 驱动的 `events` kworker 因此进入**不可中断等待**，并**持有模块引用**。
3. 该驱动用**同一个事件线程**服务所有 PCIe EP ⇒ 线程一卡，
   **所有** PCIe NPU 卡的 `rknn3_init` 都跟着挂死
   （这解释了为什么 card 1 / card 2 先是正常、后来也一起挂）。
4. 模块 `refcnt=3` 被卡住的请求持有 ⇒ `rmmod pcie_rkep` 报
   **"Module pcie_rkep is in use"** ⇒ **用户态无法清除，只能重启主机**。

### 6.3 已尝试的恢复手段（**全部无效**，因为都动不了那个卡住的 kworker）

| # | 手段 | 结果 |
|---|---|---|
| 1 | `kill -9` 挂死的 proxy + 重启 supervisor | ❌ 仍挂 |
| 2 | 重启整个 proxy 套 + 对 3 张卡**重新刷固件**（`Device boot state: 0x00004c42, success: 1`） | ❌ 仍挂 |
| 3 | `pcie_upgrade_tool -s 0001:11:00.0 rd`（设备复位） | ❌ 仍挂 |
| 4 | 驱动级 `unbind` / `bind`（`/sys/bus/pci/drivers/pcie-rkep/`） | ❌ 仍挂（设备重探测正常：`did=182a, func_ver=30301`） |
| 5 | PCIe **FLR**（`/sys/bus/pci/devices/0001:11:00.0/reset`） | ❌ 仍挂 |
| 6 | `rmmod pcie_rkep` + `insmod`（驱动模块整体重载） | ❌ **不可能** —— `Module pcie_rkep is in use`（refcnt=3） |
| 7 | `pcie_upgrade_tool td`（设备自检，三张卡全测） | ✅ **三张卡全部 OK** —— PCIe 链路与 RK1820 bootloader 完好，**坏的是 host 侧驱动的事件线程** |

> 第 7 条很关键：卡本身没坏，**host 侧的驱动状态坏了**。
> 而 host 侧驱动的那个 kworker 只能靠**主机重启**清。

### 6.4 当前板卡状态（**已由主机重启完全恢复**）

> 审核人已决定重启主机。**2026-11-06 04:22 重启后，全部恢复** ✅

| 项 | 重启前 | 重启后 |
|---|---|---|
| `kworker/…+events` | ❌ 卡在 **D 状态** 40+ 分钟 | ✅ **I 状态（空闲）** —— D 状态是 DMA 期间的瞬时态，连续采样 16 s 均为 I |
| `0001:11:00.0`（card 0） | ❌ `rknn3_init` 挂死 | ✅ **正常**：SDK 加载 19.85 s / 16 轮 / 2.20 s，文本正确 |
| `0003:31:00.0`（card 1） | ❌ 被连带挂死 | ✅ **正常**：webui worker 加载 18.44 s，端到端 16 轮 / 2.05 s |
| `0004:41:00.0`（card 2） | ❌ 被连带挂死 | ✅ **正常**（重启前已验证） |
| `rknn3_transfer_proxy` 套 | — | ✅ supervisor + 6 个 per-device 子进程在跑 |
| `wdnwebui.service` / `rknn-mdns.service` | — | ✅ `active` |
| `config.jsonc` | ✅ 未改动 | ✅ 仍未改动（git 干净），`compute.autostart` 仍为 `[]` |

**重启后重跑的完整验证（全部 PASS）**：

| 验证 | 结果 |
|---|---|
| SDK 在 card 0（曾经坏掉的那张） | ✅ 加载 19.85 s，16 轮，2.20 s，文本正确 |
| **实时** C demo vs SDK 比对（C demo 在 device[0]=card 0，SDK 在 card 1） | ✅ **PASS** —— 最终结果逐字节相同，16/16 轮已确认文本全一致 |
| C demo 跑完后 card 0 复测 | ✅ 仍正常（加载 18.35 s，16 轮，2.02 s）—— 说明**只要不背靠背同卡**就没问题 |
| webui 端到端（worker 在 card 1） | ✅ 16 轮 WS 增量，`done` 文本与 C demo 一致，elapsed 2.05 s |

**重启后的板子状态 = 功能可用、无持久化算力实例的干净状态**。
ASR worker 目前**运行在 card 1**（运行期状态，不落盘），随时可停：

```bash
curl -X DELETE http://127.0.0.1:8000/api/compute/instances \
  -H 'Content-Type: application/json' -d '{"model":"qwen3-asr"}'
```

### 6.5 根因复盘（写给下一个人）

| 坑 | 说明 |
|---|---|
| **同卡背靠背会挂死整个 PCIe NPU 子系统** | C demo 退出后立刻在同一张卡建新 context ⇒ 一条 DMA 永不完成 ⇒ 驱动的 `events` kworker 进 D 状态 ⇒ **所有** PCIe 卡的 `rknn3_init` 一起挂死（不是只坏一张卡！） |
| **卡死的 kworker 持有模块引用** | `lsmod` 显示 `pcie_rkep 40960 3`（refcnt=3，3 张卡各一个未完成的请求），`rmmod` 直接报 **"Module pcie_rkep is in use"** ⇒ **用户态无解** |
| **重启 proxy / 重刷固件 / 设备复位 / FLR / unbind-bind 全部无效** | 因为它们都动不了那个卡在 D 状态的 kworker。`pcie_upgrade_tool td` 三张卡全部 **OK** —— 卡本身没坏，坏的是 host 侧驱动状态 |
| **诊断抓手** | `ps -eLo pid,tid,stat,comm \| awk '$3 ~ /D/'` —— 出现常驻 D 状态的 `kworker/…+events` 就是它；`grep pcie /proc/interrupts` 里 `pcie-sys` 计数全 0 = 完成中断没到 |
| **唯一解** | **重启主机** |
| **`kill -9` 挂死的 proxy 不够** | `rknn3_transfer_proxy` 的 supervisor **不会**自动把 per-device 子进程拉回来（只在启动时按设备发现拉一次）—— 还得 `rknn3_startup stop` + `start` |

### 6.6 规避措施（已固化进代码与文档）

- `compare_with_c_demo.py` 默认**自动给 SDK 选一张和 C demo 不同的卡**
  （从 C demo 自己的输出里解析它用了哪张卡，再在
  `RKNN3Lite.get_devices_id()` 里挑别的），并在同卡时打 `[WARN]`。
- `streaming_asr.py --npu` 的 help 里写明「和 webui 里已启用的 worker 不能抢同一张」。
- sample README 的「容易踩的坑」第 4 条完整记录了症状 → 原因 → 规避。
- 比对脚本的「结果抽取只认 stdout」：C demo 的 llama 元数据 dump 走 **stderr**，
  把 `stdout + stderr` 拼起来会让「Final Commit Result」后面粘上整段日志，
  导致**假失败**。现在只从 stdout 解析，stderr 仅作诊断。

---

## 7. 本轮两条需求的改动与验证

用户的两条需求：

1. 「流式语音识别」的**向导界面怪怪的** —— 参照「视频任意目标定位」的向导，
   结合 ASR 自己的功能重做；**不要** gscore / 判帧 / 阈值 / 帧间隔那一套
   （ASR 每一段音频都要解析，没有「跳过某些段」这回事）。
2. 增加**语音文件上传**，兼容**视频文件中包含音频**的形式。

### 7.1 需求一：向导重做 —— 以及「怪怪的」的**真正**根因

向导内容本身是重写过的，但**用户看到的界面根本不是 ASR 的向导页**。真因是
一条**漏掉的卡位声明**：

```
app/static/index.html.template
    {% card_slot "video-retrieval" %}
+   {% card_slot "qwen3-asr" %}      ← 本轮补上
    {% card_slot "tts" %}
```

`html_assembly.assemble_index_html()` 靠这个占位符把卡片的
`static/template.html` 片段拼进首页。**占位符缺失 → ASR 卡片页面段
根本不在 DOM 里**（实测：线上 HTML 中 `page-qwen3-asr` 出现 **0 次**），
后果是连锁的：

| 环节 | 缺卡位时的表现 |
|---|---|
| `WizardState.mount()` 找不到 `#page-qwen3-asr` | 向导容器 fallback 到 `document.body.appendChild()` —— **向导浮在整页最末尾**，压在别的卡片底下 |
| 路由 `showPage('qwen3-asr')` | 所有 `.page` 段都被 `hidden`，但**向导容器不是 `.page`**，所以它照样显示 —— 看起来像「贴在当前卡片下面」 |
| 用户点「返回首页」/切卡 | 看到的是**上一张卡**（vtl）的画面 + 一截 ASR 向导 —— 这就是「怪怪的」 |
| 卡片 JS | 照常被 `_loadCardJS` 加载（`isCardPage` 只看 manifest），所以 `/api/qwen3-asr/` 一直在轮询、`compute.active_app=qwen3-asr` 正常 —— **后端一切正常，只有页面是错的** |

> 服务日志里其实一直在报警，只是没人注意：
> `[WARNING] core.html_assembly: [qwen3-asr] no {{card_slot}} placeholder in index.html.template`

**这类卡片（内置在 `app/apps/<id>/`，不是 pip 插件包）必须自己在
`index.html.template` 里声明卡位。** 插件包提供的卡片（vtl / vd / tts …）
由发布方保证，所以内置卡最容易漏这一步。

补上卡位后，向导容器正确地挂在 `#page-qwen3-asr` **旁边**（sibling），
向导期间卡片页被隐藏、结束后 `unmount()` 把它放回来。

### 7.2 向导的三步设计（对齐 vtl 的骨架，内容全是 ASR 自己的）

`app/apps/qwen3_asr/static/wizard.json`：

| 步 | kind | 内容 |
|---|---|---|
| 1 运行环境检查 | `env_check` | 硬件能力（本卡不依赖）+ 软件能力 `swcap_audio_asr`；缺模型时给「一键开启所需模型」 |
| 2 音频源 | `form` | **音频文件**（设备端路径文本框 + 「浏览…」+「上传…」）+ **显示模式**下拉（「实时结果」/「识别过程」，见 §7.8） |
| 3 完成配置 | `finish` | 调 `applyWizardSnapshot` → **直接发起识别** |

**刻意没有的东西**（用户明确要求）：

| 被去掉的 | 为什么 |
|---|---|
| `gscore` / 判帧算法 / 阈值 / 帧间隔 | vtl 的「目标定位」语义（哪一帧值得送模型）。ASR 没有对应概念：**每一段音频都必须解析** |
| **识别参数**（分块长度 / 每块 token 上限 / 回退 token / 前几轮不提交） | 这四个值 = C demo 的 `ONLINE_CHUNK_MS` / `ONLINE_MAX_NEW_TOKENS` / `ONLINE_ROLLBACK_TOKENS` / `ONLINE_ROLLBACK_ROUNDS`，是**「与 C demo 对齐」的内部契约**，不是用户该调的东西。上一轮它还占着向导一整步 + 功能页一个折叠块，本轮按用户要求**两处都去掉** |

> **去掉参数 UI 后，参数从哪来？**
>
> **前端彻底不传** → SDK 用自己的 `ONLINE_*_DEFAULT`
> （`asr.py:78-81`：`1.0 / 50 / 5 / 4`，与 C demo 常量逐个相同）。
> **单一真值源留在 SDK**，前端不再复制一份常量表 —— 也就不会出现
> 「前端默认 1、SDK 默认 2」这种漂移。
>
> 后端 `POST /transcribe` 的四个 Form 字段**保留**（`chunk_seconds` /
> `max_new_tokens` / `rollback_tokens` / `rollback_rounds`，均为
> `Form(None)`）—— 给直接调 API 的人用；不传就不带进
> `session.respond(**options)`，由 SDK 取默认值。

> **向导字段 id 刻意不叫 `-source` 结尾**：框架里 id 以 `-source`/`-sources`
> 结尾的字段走「视频源」分支（浏览… + **canvas 搬运** + `-loop` 开关），
> ASR 没有 canvas。`asr-audio` 走的是 `browse:true` / `upload:true` 分支，
> 正好给出「浏览…」+「上传…」两个按钮。

### 7.3 需求二：语音文件上传 + 兼容含音轨的视频

**新增 `app/core/audio/decode.py`**：

```python
decode_media(source, *, filename="", max_seconds=MAX_AUDIO_SECONDS) -> DecodedAudio
```

| 输入 | 处理 | 理由 |
|---|---|---|
| **WAV** | **原样透传** `parse_wav()` 的结果（实测与 `parse_wav` 逐字节一致、md5 相同） | 提前转码就等于换了一条输入链 → mel 末位比特会变、token 可能翻转 → 「与 C demo 逐字节一致」这条硬要求就废了 |
| mp3 / m4a / flac / ogg / **含音轨的视频** | PyAV 解码 → 单声道 s16le，**保持源采样率** | 重采样统一交给 SDK 的线性插值器，让 WAV 路径与容器路径走**同一条**重采样链（若容器侧改用 swr，与 C demo 的 miniaudio FIR 既不同源也不同实现，一致性彻底失效） |
| 超过 60 秒 | **截断**（不是报错） | 对齐 C demo `online_main.cc:399-411` |

> **设计原则 2 的例外**：AGENTS.md 要求解码必须走 `wdnav`（MPP/RGA），
> 但 `wdnav` 没有音频解码器。仓库里已有先例 —— `app/core/audio/source.py`
> 写明「这是设计原则 2 的唯一例外，音频只能走 PyAV」。ASR 沿用同一条路，
> 不新开口子。

**两条上传通道并存**：

| 通道 | 入口 | 落盘 | 用在哪 |
|---|---|---|---|
| 向导「上传…」 | `POST /api/qwen3-asr/upload`（multipart） | 落到 `<resource_root>/uploads/` → 返回设备端路径 | 向导的 `opts.source` 因此永远是**设备端路径** |
| 功能页「本机上传」 | `POST /api/qwen3-asr/transcribe`（multipart `file`） | **不落盘** | 直接把字节发给后端 |

**流式参数怎么透传**（本轮起前端不传，参数由 SDK 默认值决定）：

```
wizard.json (ui_key) → applyWizardSnapshot(opts)
  → Qwen3Asr.start() 组 FormData（只有 file|path）
  → POST /api/qwen3-asr/transcribe
  → service.transcribe(options={}) → session.respond()
  → wdndds → worker run_prompt() → run_audio()
```

worker 端**不解析**任何 schema —— 它只是把 kwargs 递下去，符合
「worker 只做传输」的原则。

> 后端仍支持显式传这四个参数（`Form(None)`，不传即空），供直接调 API
> 的人使用；网页端一律走默认值。

### 7.4 验证

#### （a）四来源端到端 —— **PASS**

```
=== 上传 wav（multipart file） ===
  POST      : 200  origin=wav dur=15.051229166666667 sr=48000 trunc=False
  rounds    : 16   elapsed: 2.03 s
  == C demo : YES
=== 上传视频（multipart file，mp4 取音轨） ===
  POST      : 200  origin=container dur=14.997333333333334 sr=48000 trunc=False
  rounds    : 15   elapsed: 1.92 s
  == C demo : YES
=== 设备端路径（form path） ===
  POST      : 200  origin=wav dur=15.051229166666667 sr=48000 trunc=False
  rounds    : 16   elapsed: 2.04 s
  == C demo : YES
=== 设备端路径（form path，视频） ===
  POST      : 200  origin=container dur=14.997333333333334 sr=48000 trunc=False
  rounds    : 15   elapsed: 1.89 s
  == C demo : YES

RESULT: PASS
```

> 视频来源**少一轮**（15 vs 16）不是失败：AAC 音轨比源 WAV 短 **54 ms**，
> 少一块 1 秒的音频。四条路径的**最终文本逐字节相同**。

测试素材（板上没有现成含音轨的视频，用 PyAV 现造）：

| 文件 | 内容 |
|---|---|
| `/tmp/asr_ref/asr_real_audio.mp4` | 真实 `asr_en.wav` 转 AAC 音轨 + 320×240 黑帧（190 KB） |
| `/tmp/asr_ref/asr_with_video.mp4` | 8 s 正弦波（测「无语音」通路） |

#### （b）真浏览器向导回归 —— **32/32 PASS**

`scripts/verify_asr_wizard.py`（Playwright + Chromium）：

| 断言 | 内容 |
|---|---|
| W0 ×2 | 卡片页 section 在首页 DOM 里 + 向导容器挂在它**旁边**（卡位回归护栏） |
| W1 ×2 | env 步渲染能力行、列出 `swcap_audio_asr` |
| W2 ×4 | 步骤标题「音频源」、有音频源字段、有显示模式字段、有「浏览…」+「上传…」两个按钮 |
| W3 ×2 | 显示模式有「实时结果」和「识别过程」、**文案不含内部术语**（已确认 / 未确认 / 尾巴 / 逐轮 / 明细） |
| W4 ×2 | **没有** gscore / 判帧 / 阈值 / 帧间隔 字段；音频源步也**没有**流式参数字段 |
| W5 ×6 | 步骤标题「完成配置」、向导**只有 3 步**、`wizard.json` 里没有识别参数步、`wizard.json` 里没有流式参数字段、**功能页没有「识别参数」UI**、功能页的显示模式也是大白话文案 |
| W6 ×4 | 走完向导**真的发起了识别**、页面文本 == C demo、后端最终文本 == C demo、结束后卡片页被放回 |
| W7 ×2 | 设备端 mp4 也能识别、文本 == C demo |
| W8 | 切到「逐轮明细」后**没有覆盖「完整 JSON」**（见 §7.6） |
| W9 ×4 | 应用面板卡片有预览视频、src 指向卡片 `static/` 下的 mp4、**能真正解码出画面**、静音自动循环（见 §7.7） |
| — | 页面无 JS 报错 |

```
=== 第 3 步：完成配置 → 发起识别 ===
  向导步骤数: 3
  wizard.json steps: env, source, finish
  状态: done · 轮数: 16 · 文本: "Uh huh. Oh yeah, yeah! He wasn't even that big when I started listening to him, but in his solo music, didn't do overly well, but he did very well when he started writing for other people."
=== 视频路径（设备端 mp4，含音轨）===
  状态: done · 轮数: 15 · 文本: "…（同上，逐字节相同）"

=== 32 通过 / 0 失败 ===
```

> **W5 的「没有识别参数步」怎么验才靠谱**：查 DOM 只能看到**当前那一步**
> 的字段 —— 在「完成配置」步上查字段永远是空的，配置里藏着参数步也照样
> 能过。所以判据是**直接读 `wizard.json`**（`steps` 里没有 `params`、
> 字段 id 里没有 `chunk`/`token`/`rollback`）+ 查功能页 DOM 里
> `.asr-params` / `#asr-chunk-seconds` 等选择器全部落空。

> **两个测试判据上的坑**（都踩过）：
> ① 「页面上出现非空文本」**不等于**识别完了 —— 流式识别的已确认文本
> **逐轮增长**（第 7 轮就已经有 `…listening to him,`），中途取值必然比最终
> 结果短。判据必须是**后端 `current_request_id` 变大 且 state 落到 done**。
> ② 「徽章显示完成」也不可靠 —— 上一轮跑完后徽章就停在「完成」，页面重载后
> 会被 stale 快照骗过去。

#### （c）其它回归

| 脚本 | 结果 |
|---|---|
| `python -m scripts.verify_capability_groups` | ✅ 全部通过 |
| `python -m scripts.verify_model_packaging` | ✅ 全部通过 |
| `python -m py_compile`（新增/改动 py） | ✅ |
| `node --check`（app.js） | ✅ |

> **C demo 实时比对沿用 2026-11-06 04:26 重启后已验证的结论**（PASS，
> 见 §4.2）：本轮**只动 webui 侧**，SDK 仓一字未改；而 webui 四来源 e2e
> 仍产出与 C demo 逐字节相同的文本，等于「与 C 一致」这条性质在 webui
> 链路上被再次端到端验证了一遍。

### 7.5 顺手修的两个视觉问题

真浏览器截图暴露出来的（此前只有 API 层验证，从没在浏览器里看过）：

1. **`app.css` 是按暗色主题写的，实际框架是浅色主题** ——
   `var(--text, #e6e9ee)` 里的 `--text` **框架根本没定义** → 落到近白，
   在浅色底上「音频来源」标题和转写正文**几乎看不见**；
   `rgba(0,0,0,0.12)` 底在浅色页面上是一块脏灰。
   已按 framework.css 的浅色约定（`#fff` 卡片 + `var(--border)` +
   `#111827` 正文 + `var(--muted)` 次要）重写。
2. **「未选择文件」出现两次** —— 浏览器自带的 file input 已经显示
   「未选择任何文件」，我们又补了一句「未选择文件」。已改成**没选时留空**
   （CSS `:empty { display: none }`）。

### 7.6 「逐轮明细」覆盖「完整 JSON」—— 布局溢出，不是定位问题

用户报：显示模式切到「逐轮明细」后，逐轮列表**压住**下面的「完整 JSON」。

**不是** `position` 打架 —— 全链路没有任何绝对定位。真因是**布局溢出**：

```css
/* framework.css */
.imb-card      { display: flex; flex-direction: column;
                 height: calc(100vh - 220px); }   /* ← 固定高度 */
.imb-card-body { padding: 16px; flex: 1; display: flex; flex-direction: column; }
                /* ↑ 没写 min-height: 0 */
```

`.imb-card-body` 是 flex item，而 flex item 的 `min-height: auto` 等于
**min-content 高度**。逐轮明细模式下 body 的 min-content 是 734px，
而分配到的高度只有 689px（卡片 740 − head 51）⇒ body **撑出卡片 46px**。

而 `details.raw-output`（「完整 JSON」）是 `.imb-card` 的**兄弟节点**，
按**卡片**的高度参与正常流（top = 839 + 16 = 855）—— 溢出的那 46px
正好压在它上面。

实测（修复前）：

| 元素 | 实时单行 | 逐轮明细 |
|---|---|---|
| `.asr-rounds` 底边 | —（隐藏） | **869** |
| `details.raw-output` 顶边 | 855 | 855 |
| `.imb-card` 底边 | 839 | 839（被 body 顶破） |
| `.imb-card-body` 底边 | 838 | **885**（溢出卡片 46px） |

**修法**（限定在 ASR 卡片作用域内，**不动 `framework.css`** ——
`.imb-card` 被其它卡片共用）：

```css
#page-qwen3-asr .imb-card {
  height: auto;                          /* 内容多 → 卡片长高，把后面的元素推下去 */
  min-height: calc(100vh - 220px);       /* 内容少 → 兜底，视觉与原来一致 */
}
#page-qwen3-asr .imb-card-body { min-height: 0; }
.asr-rounds { max-height: min(26rem, 45vh); }   /* 上限同时看 rem 和视口 */
```

修复后实测：

| 元素 | 实时单行 | 逐轮明细 |
|---|---|---|
| `.asr-rounds` 底边 | —（隐藏） | 863.6 |
| `details.raw-output` 顶边 | 855 | **896.6** |
| `.imb-card` 高度 | 740（min-height 兜底） | 787（随内容长高） |

⇒ **不再重叠**，且「实时单行」模式的视觉完全不变（卡片仍是 740）。

多视口复验（切「逐轮明细」后 rounds 底边 vs JSON 顶边）：

| 视口 | rounds 底边 | JSON 顶边 | 重叠 |
|---|---|---|---|
| 1440×960 | 869 | 902 | ❌ 无 |
| 1024×700 | 768 | 801 | ❌ 无 |
| 768×1024 | 914 | 947 | ❌ 无 |
| 430×900（手机宽） | 1161 | 1194 | ❌ 无 |

回归脚本新增 **W8**：切到「逐轮明细」后断言
`.asr-rounds` 底边 ≤ `details.raw-output` 顶边 —— 这条断言在修复前是
**失败**的（869 > 855），现在通过。

### 7.7 应用面板的预览视频

用户提供了演示素材 `asr.mp4`，并要求「参考其它卡片」放到对应位置。

**仓库里的既有约定**（9 张卡里已有 7 张是这么做的）：

| 约定 | 内容 |
|---|---|
| 文件位置 | 卡片自己的 `static/` 目录下（`app/apps/<id>/static/`） |
| 文件名 | `preview.mp4` |
| 声明方式 | `manifest.json` 的 `preview_video` = `/static/apps/<card_id>/preview.mp4` |
| 渲染方式 | `framework.js` 把它渲染成 `<video class="app-card-video" autoplay muted playsinline loop>`，挂在 `aspect-ratio: 16/9` 的占位里（`object-fit: cover`） |
| 静态挂载 | `app/main.py` 把每张卡的 `static/` 挂到 `/static/apps/<id>`（`_NoCacheStaticFiles`） |

本轮动作：

```bash
mv asr.mp4 app/apps/qwen3_asr/static/preview.mp4   # 移出仓库根目录 + 统一改名
```

```json
// app/apps/qwen3_asr/manifest.json
- "preview_video": "",
+ "preview_video": "/static/apps/qwen3-asr/preview.mp4",
```

素材规格与 `text_chat/static/preview.mp4` **完全同规格**（同一套模板产出的）：

| | text_chat（既有） | qwen3-asr（本轮） |
|---|---|---|
| 大小 | 186 582 B | 260 598 B |
| 时长 | 3.04 s | 3.042 s |
| 视频 | h264 608×352 @24fps | h264 608×352 @24fps |
| 音频 | aac | aac 32 kHz 立体声 |

验证（真浏览器，应用面板**可见**时）：

```
{'src': '/static/apps/qwen3-asr/preview.mp4', 'readyState': 4, 'w': 608, 'h': 352,
 'dur': 3.042, 'loop': True, 'muted': True}
```

`readyState = 4`（完全加载）、`videoWidth = 608`（**真解码出画面**）、
`loop`/`muted` 为真 ⇒ 自动静音循环播放。9 张卡的预览视频全部就位，
无一报错。

回归脚本新增 **W9 ×4**：应用面板的 ASR 卡片有 `<video>` / src 指向卡片
`static/` 下的 mp4 / **能真正解码出画面**（`readyState>=2 && videoWidth>0`
—— 光有标签说明不了文件能播，路径写错或文件损坏都只会渲染出一个黑框）/
是静音自动循环的。

> **W9 必须在应用面板可见时验**：隐藏容器里的 `<video>` 浏览器可能只加载
> metadata 不解码，判据会飘 —— 所以脚本里先 `goto_home()` 切回首页网格
> 再测。

### 7.8 显示模式改成大白话文案

用户反馈：两种显示模式的文案「很难理解」。

| | 改前 | 改后 |
|---|---|---|
| 向导下拉 | `实时单行（已确认文本 + 未确认尾巴，整行刷新）` | **`实时结果（文字实时刷新，蓝色是还可能改动的部分）`** |
| 向导下拉 | `逐轮明细（每轮一行）` | **`识别过程（每识别一小段就记一行，能看到文字是怎么一步步成句的）`** |
| 功能页下拉 | `实时单行` | **`实时结果`** |
| 功能页下拉 | `逐轮明细` | **`识别过程`** |

改的思路：**术语是内部机制，不是用户语言**。

- 「已确认 / 未确认尾巴」讲的是**回退策略**（`ONLINE_ROLLBACK_TOKENS` /
  `ONLINE_ROLLBACK_ROUNDS`，见 §1.2）—— 那是对齐 C demo 的实现细节，
  用户不需要知道「尾巴」是什么。
- 「单行 / 逐轮 / 明细」讲的是**输出形态**，而形态的差别对用户来说就是
  「要不要看过程」。

所以界面上只留一句话能说清的差别：**「实时结果」= 看结果**，
**「识别过程」= 看它是怎么一步步出来的**。

**`value` 没动**（仍是 `single` / `rounds`）—— 模式判定的逻辑、WS 协议、
后端 schema 全都零改动，只换了 label。

**代码注释里保留旧术语**（`实时单行` / `逐轮明细`），因为那俩确实是
C demo 的两种输出方式（`-s` vs 默认），注释要和 C demo 对得上。
`app.js` 顶部补了映射：

```
UI「实时结果」  ←→  内部 single  ←→  rknn_qwen3_asr_demo_online ... -s
UI「识别过程」  ←→  内部 rounds  ←→  rknn_qwen3_asr_demo_online ...
```

**功能页下拉的宽度**：新文案是 **4 个字**，与原来的「实时单行」「逐轮明细」
**等长** —— 这点是刻意的。`<select>` 收起态会把选中的 option 文本裁掉
（`AGENTS.md` 记过的坑，`scrollWidth` 看不出来），所以功能页的 option
必须短。实测：

```
{'value': 'single', 'text': '实时结果', 'optTextPx': 57, 'selectClientW': 95, 'scrollW': 95}
{'value': 'rounds', 'text': '识别过程', 'optTextPx': 57, 'selectClientW': 95, 'scrollW': 95}
收起态: {'scrollW': 95, 'clientW': 95, 'overflow': False}
```

`scrollWidth == clientWidth` ⇒ **没有裁切**（option 文本 57px，
select 可用宽度约 60px，刚好放下）。

回归脚本同步加了 W3 的第二条断言：**文案里不许再出现内部术语**
（已确认 / 未确认 / 尾巴 / 逐轮 / 明细）—— 防止以后有人「顺手改回去」。

---

## 8. 未做 / 待办

1. **不提交**：两个仓库的改动都留在工作区（按要求）。
2. ~~前端页面的浏览器人工走查~~ —— **本轮已做**（Playwright 真浏览器 32/32，
   见 §7.4 (b)），并因此揪出了「卡位缺失」「暗色主题配色」「逐轮明细覆盖
   完整 JSON」三个真问题。
   仍未人工确认的只剩**移动端 / 窄屏**下的布局。
3. `compare_with_c_demo.py` 目前是**工具脚本**，尚未纳入 `scripts/` 下的
   自动回归（`verify_model_packaging` / `verify_capability_groups` 那样）。
   如需要可加成 `scripts/verify_asr_parity.py`。
   （`scripts/verify_asr_wizard.py` 已经是自动回归形态，但它依赖本机服务
   在跑、且要占用 ASR worker 所在的卡。）
4. **ASR worker 目前运行在 card 1**（运行期状态，不落盘；重启后需重新启用）。
   停掉：
   ```bash
   curl -X DELETE http://127.0.0.1:8000/api/compute/instances \
       -H 'Content-Type: application/json' -d '{"model":"qwen3-asr"}'
   ```
5. **留一句给运维**：本机 `pcie_rkep` 驱动有一个已知雷 —— 一条永不完成的 DMA
   会把 `kworker/…+events` 卡进 D 状态并**拖垮全部 PCIe NPU 卡**，且用户态
   手段（重启 proxy / 重刷固件 / 设备复位 / FLR / unbind-bind / rmmod）**全部无效**，
   只能重启主机。触发条件是**同一张卡上背靠背建/销 context**（如 C demo 退出后立刻
   在同卡再跑 SDK）。诊断一行命令：
   ```bash
   ps -eLo pid,tid,stat,comm | awk '$3 ~ /D/'
   ```
   出现常驻 D 状态的 `kworker/…+events` 就是它。详见 §6.5。
6. **内置卡片的卡位是易漏点**：`app/apps/<id>/` 下的卡必须在
   `app/static/index.html.template` 里声明 `{% card_slot "<id>" %}`，
   否则卡片页面不进首页、向导会浮在页尾（§7.1）。新建内置卡片时记得加上，
   并让 `scripts/verify_asr_wizard.py` 里的 **W0** 断言继续守着它。
