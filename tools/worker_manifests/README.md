# worker manifest 出厂清单

这个目录是 **worker 配置的出厂副本**，用于上传到 HuggingFace。

worker 配置现在**随模型文件夹一起分发** —— 一个模型文件夹 = 权重 +
描述它的 manifest = 一个分发单元。板子上的运行时**不读这个目录**，
它只扫模型根目录（`/userdata/models`）下的模型文件夹。

## 为什么要留一份在仓库里

* **上传到 HuggingFace 的素材**：每个模型一个 repo，repo 里放权重 +
  对应的 manifest，用户 `git clone` 下来就是完整安装包。
* **迁移工具的输入**：`python -m scripts.migrate_worker_manifests`
  把这里的 manifest 铺到本机的模型目录里。
* **回归对照**：`scripts/verify_capability_groups.py` 用它校验
  「出厂清单声明的能力都在 SDK 封闭词表内」。

> HF repo 建好后这个目录就可以删了 —— 届时 manifest 的唯一副本在
> 各模型的 repo 里。

## 文件命名

两种约定都支持：

* `<model_id>.json`（**出厂清单用的就是这种**）—— 文件名即模型 id。
  沿用了长期以来的「id == 文件名主干」规则，改一处就能看出是哪个模型。
* `worker.json` —— 每个文件夹固定名字，id 从 manifest body 的 `id` 字段读。

模型文件夹里可以有**其它 json**（tokenizer config 之类）—— 只有带
`spec_factory` 字段的文件会被当成 worker 配置。

## 字段

见 `docs/MODEL_INSTALL_GUIDE.md` 的字段表，或
`app/core/compute/workers/__init__.py::WorkerManifest`（权威定义）。

新增字段时记得同步：`_validate` 的 `known` 集合（决定哪些键进 `extra`）。
