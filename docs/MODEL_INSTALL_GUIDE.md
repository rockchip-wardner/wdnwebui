# 模型安装指南

> 面向**最终用户**：怎么把模型装到板子上，怎么让它出现在算力配置页。

## 一句话

一个模型 = 一个文件夹 = 一个 HuggingFace repo。文件夹里有权重，**也有**
描述这个模型的 worker 配置。`git clone` 下来就装好了，不需要额外的安装步骤。

```
/userdata/models/Qwen3-VL-4B/          ← clone 下来的就是这个文件夹
├── qwen3-vl-4b.json                   ← worker 配置（随模型一起分发）
├── llm_Qwen3-VL-4B.1828.rknn
├── vision_Qwen3-VL-4B.384x384.1828.rknn
├── Qwen3-VL-4B.tokenizer.gguf
└── Qwen3-VL-4B.embed.bin
```

## 安装步骤

```bash
# 1. clone 到模型根目录
git clone https://huggingface.co/<org>/Qwen3-VL-4B /userdata/models/Qwen3-VL-4B

# 2. 刷新（可选）—— 后台每 ~2s 自动扫一次，一般等一下就出来了
curl -X POST http://<板子IP>:8000/api/compute/refresh-models
```

然后打开**算力配置**页，「本地算力」栏里就有这张模型卡片了，点「启用」即可。

> **目录名可以任意**。SDK 跟随的是「worker 配置所在的那个文件夹」，
> 不要求文件夹名等于模型内部的硬编码名字。所以 HF repo 叫什么都行。

## 几个要点

### 没装模型 = 看不到卡片

算力配置页的「本地算力」只列出**本板已安装**的模型。模型文件夹不在，
卡片就不在 —— 这是设计如此，不是故障。

反过来说：装了模型但**没启用**，卡片**在**（显示为「未启用」），
可以直接点启用。

### 不用重启服务

服务每 ~2 秒扫一次模型根目录。clone 完最多等 2 秒卡片就出现；
想立刻生效就点算力配置页右上角的**「刷新模型」**按钮。

### 改了模型根目录要重启

「基础配置」页的 `base.model_path` 决定模型根目录。这个值在服务启动时
写入环境变量，**运行期改它不生效** —— 改完需要重启服务。

（这是已知的既有行为，本次改造未改动它。）

### 模型根目录在哪

默认 `/userdata/models`，也可以是 `/mnt/oem/models` 或 `~/models`
（按此顺序取第一个存在的）。实际生效的值见「基础配置」页，或算力配置页
空状态提示里显示的路径。

## 常见问题

### 卡片出现了，但启用时报「模型目录不存在」

worker 配置在，但权重文件不在同一个文件夹里。检查：

```bash
ls /userdata/models/<模型目录>/
```

应该能看到 `.rknn` / `.weight` / `.tokenizer.gguf` 等文件。
clone 时注意别漏了 LFS 文件（`git lfs install` 后重新 clone）。

### 卡片出现了，但启用时报「模型文件缺失」

SDK 在这个模型目录里找不到它期望的那些具体文件名。每个模型的必需文件
清单在 `wdnai` 的 `model.py` 里（`*_REQUIRED` 常量），报错的文件名就是清单。

### 想装一个还没发布到 HuggingFace 的模型

把权重和 worker 配置放进同一个文件夹即可 —— worker 配置的格式见
`tools/worker_manifests/` 里的样板。

### worker 配置里那些字段是干嘛的

| 字段 | 作用 |
|---|---|
| `id` | 模型 id，全系统唯一（`--model` 参数、DDS 服务名都用它） |
| `label` | 算力配置页上显示的名字 |
| `capability_types` | 这个模型提供哪些能力（`swcap_*`，词表由 SDK 封闭枚举定义） |
| `spec_factory` | `wdnai.SystemLanguageModel` 上的工厂方法名 |
| `worker_name` | 进程 comm 别名（≤15 字节） |
| `model_dir_name` | 建议的文件夹名（迁移/校验用；实际以所在目录为准） |
| `model_dir_env` | SDK 用来定位该模型文件的 per-model 环境变量名 |
| `requires_cards` | 需要几张算力卡 |
| `estimated_init_seconds` | 首次 load 的经验秒数（算力页「加载中」倒计时的起点） |
| `infer_timeout_s` | **一次推理的预算（秒）**，可选，缺省 60。长耗时模型（扩散/文生图）必须声明，否则会被 60 s 砍掉 |
| `display_order` | 算力配置页里的显示顺序 |

完整字段表见 `app/core/compute/workers/__init__.py::WorkerManifest`。

## 维护者：从仓库迁移到模型目录

仓库里保留了一份出厂清单 `tools/worker_manifests/`（用于上传到
HuggingFace）。把它铺到本机：

```bash
python -m scripts.migrate_worker_manifests --dry-run   # 先看计划
python -m scripts.migrate_worker_manifests             # 实际写入
```

模型目录不存在的会被跳过（那个模型没装，正常状态）。clone 完模型后
重跑一次即可补上它的 worker 配置。
