# 开发计划书：worker 配置文件随模型文件夹分发

> 状态：**已实施**（2026-11-02 按本文档执行完毕，实施差异见文末「实施结果」）
> 版本基线：wdnwebui v0.9.2
> 日期：2026-11-02

---

## 一、目标与非目标

### 1.1 目标

1. **worker 配置文件从代码仓移到模型目录**：每个 worker 的 manifest 落到
   `/userdata/models/<ModelDir>/` 内，与模型权重文件同住。
2. **发现逻辑改为「按模型文件夹发现」**：模型文件夹不在本板 → 该 worker
   在**算力配置页的「本地算力」里不出现**。
3. **为 HuggingFace 分发做准备**：每个模型文件夹 = 一个 HF repo，
   `git clone` 下来即同时获得权重与 worker 配置，无需额外安装步骤。
4. **clone 完即可用**：模型文件夹出现后，算力配置页能（尽量快）看到对应
   worker 卡片，不要求用户重启服务。

### 1.2 非目标（本期不做）

- 不改 wdnai SDK 的模型实现（除 D3-a 需要补 2 个 env 覆盖，见 §4.3）。
- 不做 HF 上传脚本本身（只准备可上传的目录结构）。
- 不改模型权重格式、不改 RKNN 导出流程。
- 不做在线下载/自动更新（用户自己 `git clone`）。

---

## 二、现状事实（改造前必须知道的事实清单）

以下每条都带 `file:line`，是本计划的依据。

### 2.1 两条独立的路径

| 东西 | 现在在哪 | 是否进版本库 |
|---|---|---|
| worker manifest（身份/能力/UI 元数据） | `app/core/compute/workers/<id>.json` | ✅ 是 |
| 模型权重（rknn/weight/tokenizer/embed） | `/userdata/models/<ModelDir>/` | ❌ 否 |
| 两者之间的桥 | manifest 里的 `model_dir_name` 字段 | ✅ 是 |

**`model_dir_name` 是唯一的桥**，且它有两份同名字面量：

- manifest 侧：`app/core/compute/workers/qwen3-vl-4b.json:10` → `"Qwen3-VL-4B"`
- SDK 侧：`.venv/src/wdnai/src/wdnai/model.py:498` → `_models_root() / "Qwen3-VL-4B"`

两份必须永远一致，但没有任何机制保证它们一致（见 §8 风险 R1）。

### 2.2 发现机制（要改的核心）

`app/core/compute/workers/__init__.py:269-329`：

```python
WORKERS_DIR = Path(__file__).resolve().parent      # :61 —— 写死在包目录

def discover() -> List[WorkerManifest]:
    if not WORKERS_DIR.is_dir():
        return found
    for path in sorted(WORKERS_DIR.glob(f"*{MANIFEST_SUFFIX}")):
        ...                                     # 扫包目录下的 *.json
```

`model_presets()`（`:340-352`）过滤掉 `status != "ok"` 和 `hidden` 的条目。

### 2.3 `MODEL_PRESETS` 是**导入时冻结**的模块常量

`app/core/compute/registry.py:529`：

```python
MODEL_PRESETS: dict[str, dict] = _model_presets()
```

全仓 **66 处**引用它（`app/` 下 58 处 + `scripts/` 下 8 处），
**全部是只读**（无一处写入）。这意味着：

- 只要保持「同一个 dict 对象、原地 mutate」，**零调用点改动**就能支持动态刷新。
- 反过来，运行期新增/删除 manifest 对当前进程**完全不可见**。

> 注：`app/core/compute/workers/__init__.py:39` 的模块 docstring 写的是
> 「~54 call sites」—— 实测 66 处，docstring 已过期（不影响功能）。

### 2.4 worker 进程侧

`app/core/compute/workers/_base.py:262-279`：

```python
HERE = os.path.dirname(os.path.abspath(__file__))

def load_manifest(model_id: str) -> dict:
    path = os.path.join(HERE, f"{model_id}.json")
    ...  # FileNotFoundError → RuntimeError("no manifest for model ...")
```

模型目录在 `_base.py:1391-1401` 拼出来：

```python
base_dir = os.environ.get("WDNAI_MODELS_DIR", f"{default_models_root()}/")
dir_name = manifest.get("model_dir_name")
if dir_name:
    model_dir = os.path.join(base_dir, dir_name)
```

### 2.5 wdnai SDK 已经支持 per-model 目录覆盖

`wdnai/model.py` 里 7/9 个模型有各自的 env 覆盖：

| 模型 | 覆盖 env | SDK 硬编码目录名 |
|---|---|---|
| Qwen3-VL-4B | `WDNAI_QWEN3_VL_4B_DIR` | `Qwen3-VL-4B` |
| Qwen3-4B | `WDNAI_QWEN3_4B_DIR` | `Qwen3-4B` |
| Qwen3.5-4B | `WDNAI_QWEN3_5_4B_VL_DIR` | `Qwen3.5-4B` |
| Qwen3.5-VL-9B | `WDNAI_QWEN3_5_VL_9B_DIR` | `Qwen3.5-VL-9B` |
| Qwen2.5-Omni-3B | `WDNAI_QWEN_2_5_OMNI_3B_DIR` | `Qwen2.5-Omni-3B` |
| LocateAnything-3B | `WDNAI_LOCATE_ANYTHING_DIR` | `LocateAnything-3B` |
| Depth-Anything-V3 | `WDNAI_DEPTH_ANYTHING_V3_DIR` | `Depth-Anything-V3` |
| **Qwen3-VL-Embedding-2B** | ❌ **无** | `Qwen3-VL-Embedding-2B` |
| **Qwen3-TTS** | ❌ **无** | `Qwen3-TTS` |

后两个的目录是 `_models_root() / "<名字>"` 硬拼（`model.py:739`、`model.py:1202`）。

### 2.6 本机现状

```
/userdata/models/
├── LocateAnything-3B/        ├── Qwen3-TTS/          ├── Qwen3-4B/   ← 有权重但无 worker
├── Qwen2.5-Omni-3B/          ├── Qwen3-VL-4B/        ├── Qwen3.5-4B/
└── Qwen3-VL-Embedding-2B/
缺失: Qwen3.5-VL-9B、Depth-Anything-V3
```

现有 8 个 worker manifest 在 `app/core/compute/workers/`。

### 2.7 三个「模型不在本板就会连累别人」的隐藏依赖 ⚠️

这是本次改造**必须先拆掉的**，否则「模型文件夹缺失」会引发比「少一张卡片」严重得多的问题。

**(a) 卡片 manifest 的能力词表从 worker 清单反推**

`app/core/manifest.py:481-496`：

```python
def _known_software_capabilities() -> set[str]:
    from app.core.compute.registry import MODEL_PRESETS
    caps = set()
    for preset in MODEL_PRESETS.values():
        for cap in preset.get("capability_types") or []:
            caps.add(cap)
    return caps
```

`app/core/manifest.py:351-355` 用它校验卡片 manifest，不认识的能力 → `FATAL` →
`manifest.py:186-187` 直接 `continue` → **该卡片从应用网格消失、API 路由不注册**。

> 讽刺的是 `app/core/compute/workers/__init__.py:408` 的 `known_capability_types()`
> （返回 `wdnai.SoftwareCapability` 封闭枚举）**docstring 明写「Used by
> `app.core.manifest._known_software_capabilities`」，但全仓没有任何调用点** ——
> 它是死代码。设计意图和实现已经脱节。

**(b) `set_active_app` 的 capability 校验**

`app/core/compute/registry.py:4847-4863`：capability 不在 `MODEL_PRESETS` 并集里 →
`ValueError` → `PUT /api/compute/active-app` 422。

**(c) autostart 校验：一条坏 → 整份作废**

`app/core/compute/registry.py:661-665` raise，`:3837-3841` catch 之后
`entries = []` —— **整份 `compute.autostart` 失效**，其他模型也不开机自启。
`_autostart_npu_map()`（`:704-713`）同样吞异常返回 `{}`，所有模型的
「开机自启」徽标一起消失。

---

## 三、目标目录布局

```
/userdata/models/                          ← 根目录 = config base.model_path = WDNAI_MODELS_DIR
├── Qwen3-VL-4B/                           ← 一个模型文件夹 = 一个 HF repo
│   ├── qwen3-vl-4b.json                   ← worker manifest（新增，随模型分发）
│   ├── llm_Qwen3-VL-4B.1828.rknn
│   ├── vision_Qwen3-VL-4B.384x384.1828.rknn
│   ├── Qwen3-VL-4B.tokenizer.gguf
│   ├── Qwen3-VL-4B.embed.bin
│   └── ...
├── Qwen3-TTS/
│   ├── qwen3-tts.json
│   └── ...
└── ...

# 仓库侧：worker 入口脚本仍然留在代码里（不是模型的一部分）
app/core/compute/workers/
├── __init__.py          # 发现逻辑（本次大改）
├── _base.py             # worker 主体（本次小改）
└── compute_worker.py    # 唯一入口，文件名/--model 参数不可动
```

**关键点**：`compute_worker.py` 入口脚本**留在仓库**，不进模型文件夹。
理由见 §8 风险 R2（`/proc/<pid>/cmdline` 的 needle 依赖它的路径）。

---

## 四、待确认的设计决策

以下 9 项需要拍板。**D3 / D5 / D6 是必答题**，其余有推荐默认值。

### D1. 模型文件夹里的 manifest 文件名

| 选项 | 布局 | 评价 |
|---|---|---|
| **A（推荐）** | `<ModelDir>/<model_id>.json` | 保留现有「id == 文件名主干」不变量（`_validate` 现在就在强制），一眼能看出是哪个模型；HF repo 里文件名与 model_id 一致 |
| B | `<ModelDir>/worker.json` | 每个 repo 统一名字，但 id 必须从 manifest 的 `id` 字段读，破坏了现有「id 默认取文件名主干」的规则 |

**推荐 A。** 并且允许 A 和 B 同时支持（A 优先），降低迁移摩擦。

### D2. 模型根目录从哪来

沿用现有机制：`config.jsonc["base"]["model_path"]` → boot 时写进
`WDNAI_MODELS_DIR` → worker 继承。

**但有两个已知缺陷需要在本次一并处理**（否则 HF 场景直接踩坑）：

- **坑 A（已确认，用户已表示不修）**：运行时改 `model_path` 不生效，
  必须重启服务。→ 本期**仍然不修**，但文档里必须写明「HF clone 请 clone 到
  默认根目录 `/userdata/models/`，或改完配置后重启服务」。
- **坑 B**：`app/core/compute/registry.py:1680-1682` 的 `list_supported_models()`
  用「config → env → `default_models_root()`」优先级，SDK 用「env → 硬编码
  `/userdata/models`」，两套优先级不一致。→ 本期建议统一成「env → config →
  default」，但这属于坑 A 的范畴，需一并决定。

### D3. 模型目录怎么确定（SDK 路径注入方式）⭐ 必答

新布局下「manifest 所在目录 = 模型目录」，但 SDK 算 rknn 路径时用的是
**它自己硬编码的目录名**（§2.1 的第二份字面量）。两种解法：

| 选项 | 做法 | 优点 | 代价 |
|---|---|---|---|
| **A（推荐）** | worker 在建 spec 前，把该模型的 per-model env 覆盖指向**实际目录**（manifest 所在目录） | 模型文件夹可任意命名 → HF repo 名自由；彻底消灭「两份目录名字面量」 | 需给 wdnai 补 2 个 env 覆盖（embedding + TTS）—— 各 3 行，与现有 7 个完全同构 |
| B | 模型文件夹名**必须**等于 SDK 硬编码名 | 零 SDK 改动 | HF repo 名被 SDK 内部命名绑死；`model_dir_name` 仍需保留做一致性校验；换 SDK 版本改目录名就断 |

**推荐 A。** 理由：HF repo 名是**长期对外契约**，不该被 SDK 内部常量绑死；
而且 A 顺手消灭了 §8 的 R1 风险。

> A 的具体实现：worker 启动时读到自己的 manifest 路径 → 推出 `model_dir` →
> 按 manifest 自己声明的 `model_dir_env` 写入 env（如
> `WDNAI_QWEN3_VL_4B_DIR=<model_dir>`）→ 再调 `build_model_spec()`。
>
> **实施时调整**：原计划在 webui 侧维护一张「model_id → env 名」的查表
> （`_LEGACY_MODEL_DIR_ENV`）。落地时**删掉了这张表** —— env 名改由
> manifest 的 `model_dir_env` 字段自带，一张模型带一张 HF repo 走，
> 表就永远是第二真源，正是这次要消灭的东西。manifest 没声明该字段时，
> 路径断言会立刻报错（而不是静默用 SDK 的默认目录）。
>
> 需要 wdnai 补的两个覆盖（对称于现有 7 个）：
> - `WDNAI_QWEN3_VL_EMBEDDING_2B_DIR`（`model.py:738-739`）
> - `WDNAI_QWEN3_TTS_DIR`（`model.py:1202`）

### D4. 发现时机：动态重扫 vs 启动时静态

用户流程是「clone → 用」，静态发现要求重启服务，体验差。

| 选项 | 做法 | 评价 |
|---|---|---|
| **A（推荐）** | 后台线程每 ~2s 扫一次模型根目录；发现集合变化时**原地 mutate** `MODEL_PRESETS` | clone 完 ~2s 内卡片出现；配合 §5.4 的手动刷新按钮兜底 |
| B | 只在启动时扫一次 | 最简单，但 clone 后必须重启 |
| C | 只在用户点「刷新模型」时扫 | 显式、无后台线程，但要用户点 |

**推荐 A + C 组合**：后台 2s 轮询（用 `os.scandir` 只看目录名+mtime，很便宜）
+ 算力配置页加一个「刷新模型」按钮（调 `POST /api/compute/refresh-models`）。

**动态化的实现要点**：
- `MODEL_PRESETS` 保持**同一个 dict 对象**，只做 `clear()` + `update()`，
  45 处只读调用点零改动（§2.3）。
- 必须在 `self._lock` 保护下 mutate，避免与 `list_with_source()` 并发读。
- `preset_order`（`registry.py:1829-1833`）每次 snapshot 重算 —— 它已经是
  snapshot 内计算的，无需额外处理。

### D5. 卡片能力词表改用 SDK 封闭枚举 ⭐ 必答

`app/core/manifest.py:481-496` 改成：

```python
def _known_software_capabilities() -> set[str]:
    from app.core.compute.workers import known_capability_types
    return known_capability_types()
```

这一行改动同时修好三个问题：

1. 卡片 manifest 不再因为「本板没装对应模型」而 FATAL 消失；
2. 删掉模型文件夹不再连累别的卡片（§2.7a）；
3. 兑现 `known_capability_types()` docstring 里早就写好的设计意图。

**顺带必须改**：`app/core/compute/registry.py:4847-4863` 的
`set_active_app` 校验同样换成 `known_capability_types()`，
否则 §2.7b 会让缺模型的卡片连「激活」都做不了。

> ⚠️ 注意 `app/core/manifest.py` 只允许 import `app.core.*`（零耦合规则），
> `app.core.compute.workers` 是 `app.core.*` → 合规。
> 但 `app/core/compute/workers/__init__.py` 的模块 docstring 明确禁止
> import `app.core.compute.registry`（会成环）→ 保持不变，
> `known_capability_types()` 放在 workers 包里正好。

### D6. autostart 校验：跳过坏条目 instead of 整份作废 ⭐ 必答

`app/core/compute/registry.py:661-665` 改成**跳过 + 警告**，不再 raise：

```
compute.autostart[0].model='qwen3-vl-4b' 未安装（/userdata/models/Qwen3-VL-4B 不存在）→ 跳过
```

必须同时改 `_parse_autostart_config` 的两个调用点：
- `:3837-3841`（boot）—— 现状是 raise → 整份作废
- `:704-713`（`_autostart_npu_map`）—— 现状是 raise → 徽标全没

**不改的后果**：用户 clone 前板子上还留着旧 autostart 配置 → 整份 autostart
永远不生效 → 「模型装了但开机不自启」，且日志里只有一行 ValueError，很难查。

### D7. `model_dir_name` 字段的处置

| 选项 | 说明 |
|---|---|
| **A（推荐）** | 保留为**可选**字段：解析但基本不用；若存在且与所在目录名不一致 → `LOG.warning`。为迁移期的旧 manifest 兜底。 |
| B | 彻底删除字段 | 更干净，但迁移期旧 manifest 会报「未知字段」警告（不会致命，`extra` 会收走） |

**推荐 A。** D3-a 落地后它就是纯 advisory。

### D8. 仓库里现有的 8 个 manifest 怎么处置

| 选项 | 说明 |
|---|---|
| **A（推荐）** | 移到一个**不被自动发现**的暂存目录（如 `tools/worker_manifests/`），配一个迁移脚本把它铺到本机模型目录；HF repo 建好后删掉暂存目录 |
| B | 直接 `git rm`，靠手工/脚本从别处恢复 | 没有回退路径 |
| C | 保留在 `app/core/compute/workers/` 作为**降级兜底**（模型目录找不到时回退） | 与「缺模型 → 不显示卡片」的目标**直接冲突** —— 违背需求，排除 |

**推荐 A。** 迁移脚本 `scripts/migrate_worker_manifests.py`：
读暂存目录的 manifest → 按 `model_dir_name` 找到模型目录 → 写入
`<ModelDir>/<model_id>.json` → 报告哪些模型目录不存在（跳过，不报错）。

### D9. 新模型出现后，DDS 服务能不能运行期注册 ⚠️ 技术风险

`app/core/compute/registry.py:1213-1215`：

```python
service_names = [f"wdndds/rpc/{mid}" for mid in MODEL_PRESETS.keys()]
```

Hub 的 service 列表是**启动时一次性**按 `MODEL_PRESETS` 生成的。D3-A 动态化后，
新 clone 进来的模型不在这个列表里 → 主进程能否正常 RPC 到它的 worker？

**待验证**（Phase 0 的调研任务，不改代码）：
- worker 侧注册的是 per-PID 服务 `wdndds/rpc/<model_id>/worker-<pid>`，
  主进程 `_setup_rpc_client`（`:3330-3354`）也是按这个拼的；
- 需要确认 cshim Hub 是否允许运行期新增 service，或主进程是否根本不需要
  预注册就能 `RpcClient.call()` 到 per-PID 服务。

**如果答案是「必须预注册」**，则 D4 的动态化需要给 Hub 加一个
`add_service()` 调用路径 —— 这是 D4 的**前置依赖**，必须在 Phase 0 定论。

---

## 五、改造清单（按模块）

### 5.1 `app/core/compute/workers/__init__.py`（发现层，主体改动）

| 改动 | 说明 |
|---|---|
| `WORKERS_DIR` → `models_root()` | 根目录来自 `WDNAI_MODELS_DIR`（fallback `default_models_root()`）。**注意本包禁止 import registry，但可以 import `app.core.config`**（零耦合规则允许 `app.core.*`） |
| `discover()` 改扫描模型根 | 遍历根下子目录 → 每个子目录找 `<model_id>.json` / `worker.json`；**manifest 的 `id` 默认取文件名主干**（D1-A） |
| `WorkerManifest` 加字段 | `model_dir: str`（绝对路径，manifest 所在目录）、`source: str`（`"model_dir"` / `"fallback"`） |
| `model_presets()` 同步 | 输出 preset 里加 `model_dir` |
| 新增 `rescan()` | 返回 `(presets, changed: bool)`；供 registry 动态刷新调用 |
| `known_capability_types()` | 保持不变（Phase 0 把它接上） |

### 5.2 `app/core/compute/workers/_base.py`（worker 进程侧）

| 改动 | 说明 |
|---|---|
| `load_manifest(model_id)` 改写 | 不再从 `HERE/<id>.json` 读；改为在模型根下扫 `<ModelDir>/<id>.json`。启动时扫一次并缓存 |
| `model_dir` 计算 | 直接用 manifest 所在目录（不再拼 `model_dir_name`） |
| **新增**：写 per-model env 覆盖 | 在建 spec **之前**把 `WDNAI_<MODEL>_DIR` 指向实际目录（D3-a）。查表放 webui 侧 |
| `build_model_spec` 调用顺序 | 必须「先设 env → 再 `factory()`」，否则 spec 里的路径还是旧目录 |
| `--model` 参数 / 入口文件名 | **不动**（§8 R2） |

### 5.3 `app/core/compute/registry.py`（注册中心）

| 改动 | 说明 |
|---|---|
| `MODEL_PRESETS` 动态刷新 | 新增 `refresh_model_presets()`：调 `workers.rescan()` → 原地 mutate `MODEL_PRESETS`（锁保护） |
| 后台重扫线程 | ~2s 一次，只在集合变化时 mutate（避免每次都重建 dict 引起前端重渲染抖动） |
| `_parse_autostart_config` | 缺模型 → 跳过该条 + warning（D6） |
| `set_active_app` 的 capability 校验 | 改用 `known_capability_types()`（D5） |
| `list_supported_models()` | `exists` 语义改为「模型目录存在」；优先级统一（D2） |
| `_cleanup_unregistered_workers` | `scan_targets` 用动态 `MODEL_PRESETS`（自动生效） |
| Hub service 列表 | 依 D9 结论决定是否需要运行期新增 |

### 5.4 `app/core/manifest.py`（卡片校验）

| 改动 | 说明 |
|---|---|
| `_known_software_capabilities()` | 改用 `known_capability_types()`（D5）—— **本次改造的前置项** |

### 5.5 `app/system/api/compute.py` + 前端

| 改动 | 说明 |
|---|---|
| `POST /api/compute/refresh-models` | 新端点，触发一次重扫（D4-C 的手动按钮） |
| `system.js` 算力配置页 | 「刷新模型」按钮；空态文案从「未声明任何 compute service —— 检查 MODEL_PRESETS 配置」改为「本机未安装任何模型 —— 把模型文件夹放到 <root>」 |
| 向导 env 步 | `swcap_to_model` 指向的 model 未安装时，显示「缺少模型 <name>（需要 <ModelDir> 文件夹）」而不是笼统的「未就绪」 |
| `swcap_to_model` 缺模型 | `wizard/state.js::_modelSlots` 对未知 model_id 给出明确文案 |

### 5.6 脚本与文档

| 文件 | 动作 |
|---|---|
| `scripts/migrate_worker_manifests.py` | 新增：把仓库暂存的 manifest 铺到模型目录（D8-A） |
| `scripts/verify_model_packaging.py` | 新增：断言「缺模型文件夹 → 不出现在本地算力」「manifest 在模型目录内也能发现」 |
| `docs/WORKER_MODEL_PACKAGING.md` | 新增：给最终用户的《模型安装指南》（clone 命令 + 目录要求） |
| `AGENTS.md` | 更新「worker 进程是薄 RPC server」章节（§5.1/5.2 的新契约）+ 新增「模型分发」章节 |
| `app/core/compute/workers/*.json` | 移到 `tools/worker_manifests/` |

---

## 六、实施阶段与验收标准

### Phase 0 —— 调研与前置解耦（不改布局）

**任务**
1. 验证 D9（Hub 是否支持运行期新增 service）。
2. 落地 D5（卡片能力词表改封闭枚举）+ D6（autostart 跳过坏条目）。
3. 补 `known_capability_types()` 的死代码接线。

**验收**
- `python -m scripts.verify_capability_groups` 通过。
- 手工验证：临时把 `app/core/compute/workers/qwen3-tts.json` 移走 → 重启 →
  **tts 卡片仍在应用网格**（只是激活时报「功能未启用」），其他卡片不受影响；
  `compute.autostart` 里其余条目仍正常自启。

> 这一阶段单独提交，因为它是「删除模型不再连累别人」的保障，
> 也是后续所有阶段的安全网。

### Phase 1 —— 发现层改造

**任务**：§5.1 + §5.2（worker 侧 `load_manifest` / `model_dir` / env 注入）。

**验收**
- 新增 `scripts/verify_model_packaging.py`：
  - 把 manifest 放到模型目录 → `discover()` 能发现；
  - 模型目录不存在 → `discover()` 不返回该模型；
  - `build_model_spec()` 返回的路径**指向实际模型目录**（不是 SDK 硬编码名）。
- `python -m scripts.verify_grounding_prompt` 通过（spec.kind 仍能解析）。

### Phase 2 —— 注册中心动态化

**任务**：§5.3 + §5.4（已在前置做）+ §5.5 后端端点。

**验收**
- 本机实测：`mv /userdata/models/Qwen3-TTS /tmp/` → 2~5s 内算力配置页
  **本地算力**列表少一张 TTS 卡 → `mv` 回去 → 卡片回来。
- `compute.autostart` 不因为缺模型而整份失效。
- 动态新增的模型能被正常 spawn / RPC 调通（依 D9 结论可能需要额外工作）。

### Phase 3 —— 迁移与仓库清理

**任务**：`scripts/migrate_worker_manifests.py`；把 8 个 manifest 移到
`tools/worker_manifests/`；本机执行迁移。

**验收**
- 本机 8 个模型全部仍能在算力配置页看到卡片、仍能正常加载推理。
- `git status` 显示 manifest 已移出 `app/core/compute/workers/`。

### Phase 4 —— 前端与文案

**任务**：§5.5 前端。

**验收**
- 空态文案正确。
- 向导 env 步对缺模型给出可操作提示（点名缺哪个文件夹、去哪 clone）。
- 算力配置页有「刷新模型」按钮且生效。

### Phase 5 —— 回归与文档

**任务**：跑全量 `scripts/verify_*.py`；写 `docs/WORKER_MODEL_PACKAGING.md`；
更新 `AGENTS.md`。

**验收**：见 §7 回归矩阵全绿。

---

## 七、回归测试矩阵

| 脚本 | 覆盖点 | 本次是否受影响 |
|---|---|---|
| `verify_capability_groups.py` | 能力枚举 / 能力组 / autostart 校验 | ⚠️ 直接受影响（D5/D6） |
| `verify_grounding_prompt.py` | `build_model_spec` 的 kind 解析 | ⚠️ 直接受影响（spec 路径改） |
| `verify_autostart_badge.py` | autostart 徽章 | ⚠️ 直接受影响（D6） |
| `verify_autostart_boot_plan.py` | boot 自启计划 | ⚠️ 直接受影响（D6） |
| `verify_worker_split.py` | worker 单入口 / manifest 结构 | ⚠️ 直接受影响 |
| `verify_dual_card_smoke.py` | 双卡 adoption | ⚠️ 间接 |
| `verify_snapshot_per_source_counts.py` | 快照 services 计数 | ⚠️ 间接 |
| `verify_llm_session_parity.py` | 主/worker session 一致 | ⚠️ 间接 |
| `verify_probe_stale_error.py` | probe 状态 | ⚠️ 间接 |
| `verify_grounding_live.py` | 真机 grounding（占 NPU） | 需真机跑 |
| `scripts/verify_model_packaging.py`（新增） | 本次核心 | — |

---

## 八、风险登记

| ID | 风险 | 影响 | 缓解 |
|---|---|---|---|
| **R1** | manifest 的 `model_dir_name` 与 SDK 硬编码目录名脱节 | 「文件明明在，模型报 missing_model_dir」 | D3-a 让 SDK 跟随实际目录，**根治** |
| **R2** | `compute_worker.py` 的路径/文件名变动 | `registry._find_workers_by_name` 与 `system_monitor` 的 `/proc/<pid>/cmdline` needle（`compute_worker.py\x00--model\x00<id>\x00`）失配 → **孤儿检测与去重静默失效** | 入口脚本**留在原位**，文件名与 `--model` 参数**一字不改**（§3） |
| **R3** | `MODEL_PRESETS` 动态 mutate 与并发读 | 快照半新半旧 | mutate 必须在 registry 的 `_lock` 内；先构造完整新 dict 再原子替换内容 |
| **R4** | 前端每 2s 收到一次 services 列表 | 无变化也重渲染 → 闪烁 | 重扫只在集合**真变了**时 mutate `MODEL_PRESETS`；前端已有 `sig` 比对机制（`system.js` 的 sig 变化才重建） |
| **R5** | HF 上传体积 | 权重是大文件 | HF 用 LFS；每个模型一个 repo；`.gitattributes` 或 HF 默认 LFS 规则 |
| **R6** | 用户 clone 到非默认根目录 | 找不到模型 | 文档明确「clone 到 `base.model_path` 指定目录」；或提供 `WDNAI_MODELS_DIR` 说明（注意坑 A：改 env 要重启） |
| **R7** | 部分模型文件夹已存在但缺 manifest（`/userdata/models/Qwen3-4B/`） | 该模型在 UI 里看不到（预期行为） | 可选：若要启用 `Qwen3-4B`，补一个 `qwen3-4b.json` manifest（SDK 有 `qwen3_4b()` factory） |
| **R8** | `manifest.py` 能力校验改动影响面 | 某些卡片 manifest 里写了枚举外的能力，现在会 FATAL | 改前先跑一遍全量卡片清单 vs 封闭枚举的差集，确认无遗漏 |

---

## 九、HF 分发与最终用户流程（目标态）

### 9.1 维护者侧

```bash
# 每个模型一个 HF repo
huggingface-cli repo create Qwen3-VL-4B --repo-type model
cd /userdata/models/Qwen3-VL-4B
git init && git lfs install
git add .            # 权重 + worker.json 一起
git commit -m "Qwen3-VL-4B rk3588 + worker manifest"
git push https://huggingface.co/<org>/Qwen3-VL-4B main
```

### 9.2 用户侧

```bash
git clone https://huggingface.co/<org>/Qwen3-VL-4B /userdata/models/Qwen3-VL-4B
# 刷新网页（或等 2s 自动刷新）→ 算力配置页出现该模型卡片 → 启用
```

### 9.3 目录命名约束

- **D3-a 落地后**：模型文件夹名**任意**（HF repo 名自由）。
- **D3-b 落地后**：文件夹名必须等于 SDK 硬编码名。

---

## 十、附录：本次改造触及的「硬约束」清单

开发过程中**不得破坏**以下既有约定（AGENTS.md 明确要求）：

1. `compute_worker.py` 的**文件名**与 `--model` 参数**不可改**（R2）。
2. `--model <id>` 里的 `<id>` 仍是 `MODEL_PRESETS` 的 key，仍是 DDS 服务名
   `wdndds/rpc/<model_id>` 的组成部分。
3. worker 进程仍只做「透传 prompt / 回原始文本」，不做任何 schema 语义解析。
4. `app/core/compute/workers/` 包**禁止 import `app.core.compute.registry`**
   （会成环）。
5. `app/core/compute/workers/` 包**禁止 import `app.apps.*` / `app.system.*`**。
6. `app/core/manifest.py` 禁止 import `app.apps.*` / `app.system.*`。
7. per-model 的推理行为差异仍写在 wdnai，不写在 manifest。

---

## 十一、待用户确认的问题清单

| # | 问题 | 我的推荐 |
|---|---|---|
| Q1 | D1 manifest 文件名 | `<model_id>.json`（兼容 `worker.json`） |
| Q2 | **D3 SDK 路径注入** | **A：worker 注入 per-model env（需给 wdnai 补 2 个覆盖）** |
| Q3 | **D5 卡片能力词表** | **必须改：改用 `known_capability_types()` 封闭枚举** |
| Q4 | **D6 autostart 校验** | **必须改：缺模型跳过该条，不整份作废** |
| Q5 | D4 发现时机 | A：后台 2s 重扫 + 手动刷新按钮 |
| Q6 | D8 仓库 manifest 处置 | A：移到 `tools/worker_manifests/` + 迁移脚本 |
| Q7 | D7 `model_dir_name` | A：保留为可选 + 不一致时告警 |
| Q8 | D2 根目录优先级统一 | 是否与坑 A 一并修（用户此前说坑 A 不修） |
| Q9 | 是否同时产出「缺模型」的向导提示文案 | 是（Phase 4） |

---

## 实施结果（2026-11-02）

全部 9 项决策按推荐执行，Phase 0~5 一次做完。落地时的三处调整：

1. **删掉了 `_LEGACY_MODEL_DIR_ENV` 查表**（见 §4 D3）—— env 名由 manifest
   自带，查表就永远是第二真源。
2. **`model_dir_name` 不再参与路径解析**（§4 D7 原写「不一致时告警」）——
   模型文件夹名本来就自由（这是特性），告警只会是噪音。字段保留，迁移
   脚本要用。
3. **卡片能力词表接入的是 `known_capability_types()`**（§4 D5）—— 它早写
   好了但一直没有调用点，这次终于接上。

额外发现并修掉两处**重写时丢掉的既有检查**（`workers/__init__.py`）：

* `capability_types` 不能为空（原 `_validate` 有，重写时漏了）；
* 入口脚本 `compute_worker.py` 缺失时该 worker 标 fatal（原来有）。

回归：`python -m scripts.verify_model_packaging` 21 条断言全通。

---

*本文档记录设计决策与实施结果。*
