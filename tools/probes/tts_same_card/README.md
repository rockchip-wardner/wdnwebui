# 同卡双 TTS 实例 —— 实测探针集

> 用途：复现 `docs/SIMULTANEOUS_INTERPRETATION_PLAN.md` §2.3 实验 1/4/5/7/8 的数据。
> 这些脚本**会占用 NPU 上下文**（会真的把卡占满），跑完进程退出即释放。

## 前置

* 必须是**能访问 NPU** 的板端环境；`rknnsmi` 采样那部分需要 root（脚本内部用 `sudo -n` 子进程）。
* 模型目录默认 `/userdata/models/Qwen3-TTS/`（可用 `WDNAI_QWEN3_TTS_DIR` 覆盖）。
* ⚠️ **跑之前先看 `GET /api/compute`**：脚本默认打**卡 1**（`PROBE_CARD=1`），
  卡上有业务实例时会互相干扰 ⇒ **选一张空闲卡**。
* ⚠️ **不要在生产业务时段跑**。

## 脚本与对应实验

| 脚本 | 对应实验 | 作用 |
|---|---|---|
| `scan_core_mask_models.py <card>` | §2.3 实验 4 | 用 4 个真实子模型逐个试 `core_mask` ⇒ 得「**掩码置位数 == 模型编译核数**」规则 |
| `scan_one.py <rknn> <weight> [llm]` | §2.3 实验 11 | 单个模型探「编译核数」（用驱动的 `CORE_NUMBER` 查询交叉验证）；逐个模型跑可列出全板的核数 |
| `util_probe.py <card> <rounds>` | §2.3 实验 10 | 合成期间每 50 ms 读 `rknnsmi` 的 `util_rate_pct` ⇒ **单实例到底吃满几个核**（需 root） |
| `ladder2.py "<role>:<mask>:<count>"` | §2.3 实验 5 | 在空卡上连续建 N 个裸 runtime ⇒ 测每卡配额（每个 spec 用逗号分隔可组合） |
| `session_quota.py contexts <n>` | §2.3 实验 5/8 | N 个 llm runtime，**每个建 1 个 LLM session** ⇒ 测 session 数量是否受配额 |
| `session_quota.py per_ctx <n>` | §2.3 实验 8 | **1 个 runtime 上建 N 个 session** ⇒ 测驱动是否允许同 context 多 session |
| `combo_probe.py` | §2.3 实验 8 | 裸组合：2×(tp+sdec+cp+talker) = 8 个上下文，**不建 session** |
| `combo_probe2.py` | §2.3 实验 8 | 同上，**但建 LLM session + 输出张量**（复刻真实实例构造顺序） |
| `probe_two_instances_card.py` | §2.3 实验 1 | **真实 SDK 路径**连建两个 `qwen3-tts` 会话（`_build_session`）⇒ 复现 talker 的 `MODEL_SETUP fail` |
| `mt_probe.py <out.json> <gate> <n>` | §2.3 实验 7 | 同卡**双实例并行性**：两个独立进程各建一个 hy-mt-1-5 会话，用同一闸门同时开跑 ⇒ 得「单请求延迟 / 聚合吞吐」 |

## 关键坑（别重试）

1. **`RKNN3Lite.init_runtime()` 不抛异常，只返回 `-1`** ⇒ 探针里**必须检查返回值**，
   否则会把失败当成功（本仓就踩过一次，导致误判「双实例的裸 runtime 组合能共存」）。
2. **`device_id` 必须传 `bytes`**（例如 `b"0003:31:00.0"`），传 `str` 会
   `TypeError: bytes or integer address expected`。
3. **`get_registry()` 在第二个进程里会永久挂住**（DDS/peer 发现）⇒ 这些探针都**绕开 registry**，
   直接用 `app.core.compute.workers._base._build_session`。
4. **裸 runtime 不会自动释放** ⇒ 阶梯脚本靠进程退出回收；同一进程里想继续占槽就**别调 `release()`**。
5. **驱动日志走 stderr、且不换行** ⇒ 和 stdout 混在一起会看不清 ⇒ 跑的时候把 stderr 重定向到单独文件。
6. `rknnsmi` 需要 root（`/dev/pcie-rkep-*` 权限）⇒ `util_probe.py` 要用 `sudo -n` 跑。
7. ⚠️ **`device_id` 传错类型 ⇒ 段错误**：`device_id` 必须是 **bytes**（如 `b"0003:31:00.0"`）。
   若写成 `BDF[CARD]` 而 `BDF` 本身已经是 bytes ⇒ 会把**字节串当字典索引**得到一个整数 ⇒ 驱动 `rknn3_init` 直接 **SIGSEGV**（不是报错，是段错误，很难查）。

## 卡号 ↔ device_id

| 卡号 | PCI BDF（`device_id`，bytes） |
|---|---|
| 0 | `0001:11:00.0` |
| 1 | `0003:31:00.0` |
| 2 | `0004:41:00.0` |

（口径见 `app/core/compute/cards.py::card_no_to_device_id`。）
