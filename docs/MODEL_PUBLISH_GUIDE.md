# 模型发布指南（推到 HuggingFace）

> 面向**维护者**：怎么把一个模型发布到 HuggingFace，让别人 `git clone` 下来就能用。
>
> * 用户侧怎么安装 → [`docs/MODEL_INSTALL_GUIDE.md`](MODEL_INSTALL_GUIDE.md)
> * 这块板子为什么连不上 `huggingface.co`、SNI 绕行代理怎么用 → [`docs/HF_SNI_PROXY.md`](HF_SNI_PROXY.md)

## 一句话

一个模型 = 一个文件夹 = 一个 HF repo = **一个分发单元**。
文件夹里有权重，**也有**描述它的 worker 配置 —— 所以 clone 下来就是完整安装包，
不需要额外的安装步骤。

## 1. 一个分发单元长什么样

以 `wardner/Qwen3.5-VL-9B` 为例：

```
/userdata/models/Qwen3.5-VL-9B/
├── qwen3.5-vl-9b.json       ← worker 配置（model_id = 文件名），纯文本，不进 LFS
├── Qwen3.5-9B-llm_seg0.1828.rknn / .weight
├── Qwen3.5-9B-llm_seg1.1828.rknn / .weight
├── Qwen3.5-9B-vision.1828.rknn / .weight
├── Qwen3.5-9B-llm.embed.bin
├── Qwen3.5-VL-9B.tokenizer.gguf
├── SHA256SUMS               ← 逐文件摘要（唯一权威）
├── config.json              ← 自描述：files[] + worker 段
├── README.md / README_CN.md
├── LICENSE / NOTICE
└── .gitattributes           ← 哪些文件走 LFS
```

两条铁律：

* **worker 配置必须保持纯文本** —— 它是这个分发单元的「说明书」，要能 diff、
  要能审阅、也要能在 Hub 网页上直接预览 ⇒ 绝不能进 `.gitattributes` 的 LFS 规则。
* **`SHA256SUMS` 与磁盘必须一致** —— 见 §2。

worker 配置各字段的含义见
[`docs/MODEL_INSTALL_GUIDE.md`](MODEL_INSTALL_GUIDE.md#worker-配置里那些字段是干嘛的)。

## 2. 发布前：把元数据刷对

模型文件被**手工替换**（转换工具重新导出、从别的板子拷过来）之后，
`SHA256SUMS` 和 `config.json` 就过期了 —— 摘要对不上、size 对不上。

两个后果：

1. `tools/publish_hf_model.py` 的**传前校验**会判定「本地文件坏了」而**拒绝上传**
   （传上去的是坏文件，比不传更糟）；
2. 已经推上去的仓里 `SHA256SUMS` 与实际内容对不上 ⇒ 下载方按摘要校验会报
   「校验和不匹配」。

⇒ **先刷元数据，再发布**：

```bash
# 干跑：只报出会改什么
python tools/refresh_model_metadata.py --dir /userdata/models/Qwen3.5-VL-9B --dry-run

# 实际写入（模型目录通常是 root 的，要 sudo）
sudo python tools/refresh_model_metadata.py --dir /userdata/models/Qwen3.5-VL-9B
```

它重算 `config.json` 的 `files[]`（size + sha256）、重写 `SHA256SUMS`，
并把 `config.json` 的 `worker` 段**投影**自模型目录里的 worker manifest
（标签 / 显存估算只有一个真源，描述文件不再手抄第二份）。

### worker 配置也要同步

仓里的 `tools/worker_manifests/` 是**出厂副本**，发布前用迁移脚本铺进模型目录：

```bash
python -m scripts.migrate_worker_manifests --dry-run   # 先看计划
python -m scripts.migrate_worker_manifests             # 实际写入
```

模型目录不存在的会跳过（那个模型没装，正常状态）。

### README 里的 size 表

README 的「文件说明」表里写着每个文件的大小 —— 换完权重后这些数字会过期，
**手工对齐**（脚本只管 `SHA256SUMS` 和 `config.json`，不管 Markdown）。

## 3. 两条推送路线

| | 路线 A：Hub API | 路线 B：SSH + git-lfs |
|---|---|---|
| 凭据 | write token（`HF_TOKEN` / `hf auth login`） | **SSH 公钥**（账号里注册过） |
| 本机前提 | `huggingface_hub` 已装 | `git-lfs` + `~/.ssh/config` 钉死密钥 |
| 网络前提 | 要能通 `huggingface.co`（或其代理 / 镜像） | SSH 22 端口通 + LFS batch API 通 |
| 传什么 | 整个目录（服务端按 sha256 判重） | git commit + LFS 对象（按 oid 判重） |
| 适合 | 通用板子、手上有 token | **这块板子**（没 token，且直连 huggingface.co 不通） |

### 3.1 路线 A：Hub API（要 write token）

发布工具 `tools/publish_hf_model.py` 是**建仓 + 上传 + 前后校验一条龙**：

```bash
export HF_ENDPOINT=https://hf-mirror.com     # 或本机 SNI 代理，见 HF_SNI_PROXY.md
export HF_TOKEN=hf_xxx                       # write token
python tools/publish_hf_model.py \
    --repo wardner/Qwen3.5-VL-9B \
    --dir /userdata/models/Qwen3.5-VL-9B \
    --endpoint https://hf-mirror.com
```

它做四件事，任一环不过就**停下不传**：

1. **传前**：本地 `SHA256SUMS` 逐文件重算 + `config.json` 与磁盘对账；
2. **建仓**（`--exist-ok`，已存在就跳过）；
3. **上传**；
4. **传后**：从 Hub 把 `SHA256SUMS` 拉回来逐条比对，再核对文件清单。

token 只从环境变量 `HF_TOKEN` 或 `hf auth login` 的标准落点
（`~/.cache/huggingface/token`）读，**不接受命令行传参**（会进 shell history
与进程列表）。

`--dry-run` 只做本地校验 + 网络探测，不建仓不上传。

#### 踩到的坑

- **`hf upload` 第一次失败是瞬时的** —— commit API 的 POST 偶发
  `RemoteDisconnected`（CloudFront 侧断连）。重跑即可；仓库已建时脚本会跳过建仓
  直接续传。
- **相对路径会踩坑** —— `hf upload <repo> SHA256SUMS` 的相对路径按
  **当前工作目录**解析，不是按 `--local-dir`；传目录时用绝对路径。
- **`.cache/` 不能进仓** —— Hub 的 commit API **硬拒** `.cache/` 下的任何路径
  （`_validate_path_in_repo` 直接抛 `ValueError`），而 huggingface_hub 自带的
  `DEFAULT_IGNORE_PATTERNS` 只挡 `.cache/huggingface/*` ⇒ 脚本已显式排除
  `.cache/*`。
- **自定义端点 + 请求体关键词** —— 见
  [`HF_SNI_PROXY.md` §3.4 / §3.5](HF_SNI_PROXY.md#34-第三层请求体里的关键词也会被掐2026-11-17-补)。

### 3.2 路线 B：SSH + git-lfs（不要 token）

模型文件推 HF **不一定需要 token** —— 账号注册了 SSH 公钥就能推。

#### 前置条件

| 条件 | 本板状态 |
|---|---|
| HF 账号已注册本机 SSH 公钥 | ✅ `ssh -T git@hf.co` → `Hi andornott, welcome to Hugging Face.` |
| `git-lfs` | ✅ `sudo apt-get install -y git-lfs`（Debian 源可达） |
| 主机密钥 | ✅ `ssh-keyscan` / `StrictHostKeyChecking=accept-new` |

> ⚠️ **ssh 默认先试 `id_rsa`** —— 如果那把没注册，HF 认到未注册的 key
> 就回 `Hi anonymous`，**后面已注册的 `id_ed25519` 根本没机会被认**。
> ⇒ 用 `~/.ssh/config` 钉死顺序：
>
> ```
> Host hf.co
>     HostName hf.co
>     User git
>     IdentityFile ~/.ssh/id_ed25519
>     IdentitiesOnly yes
> ```

#### 坑 1：HF 不支持 pure-SSH 的 LFS 协议

```
trace git-lfs: attempting pure SSH protocol connection
trace git-lfs: pure SSH protocol connection failed: Unable to negotiate version with remote side: EOF
```

git-lfs 会回退到 legacy 的 `git-lfs-authenticate`（`ssh git@hf.co
git-lfs-authenticate <repo>.git upload`），拿到 `{"header":{"Authorization":
"Basic ..."}}` —— 一份**短时效**（实测 `expires_in` ≈ 3500 秒）的 LFS 凭据。

#### 坑 2：回退之后，batch API 打到了 huggingface.co

本板 `huggingface.co` 是 DNS 污染 + SNI 阻断 ⇒ git-lfs 默认的
`https://huggingface.co/<repo>/info/lfs` 必然超时。

⇒ 把 LFS 端点指到镜像：

```bash
git config lfs.url https://hf-mirror.com/wardner/Qwen3.5-VL-9B.git/info/lfs
```

**但端点一旦被 `lfs.url` 接管，git-lfs 就不再走 SSH 认证那条路了** ——
它只在「端点由 SSH 远端推导」时才去跑 `git-lfs-authenticate`；否则改问
git 要用户名密码 ⇒ 终端弹出 `Username for 'https://hf-mirror.com':`。

#### 桥接：凭据助手

`tools/hf_lfs_credential_helper.py` 把两段接起来 —— 每次要凭据就现取一份
（因为它是短时效的）：

```bash
sudo install -m 755 tools/hf_lfs_credential_helper.py /usr/local/bin/hf-lfs-cred
git config credential.helper /usr/local/bin/hf-lfs-cred
git config lfs.url https://hf-mirror.com/wardner/Qwen3.5-VL-9B.git/info/lfs
```

git 拿到 `username` + 明文口令后会**重新编码成一模一样的 Basic 头**，
镜像把它转发给真 Hub —— 实测 batch API 返回 200，并给出
`hf-hub-lfs-us-east-1.s3-accelerate.amazonaws.com` 的 presigned PUT。
**数据面直连 S3，不经镜像** ⇒ 带宽比走镜像的 HTTP 上传快得多。

#### 坑 3：verify 这一步的地址是「真 Hub」

镜像在 batch 响应里回的是**真 Hub 的绝对地址**
（`https://huggingface.co/<repo>/info/lfs/objects/verify`）⇒ 推送在收尾时
报 `dial tcp ...: connect: connection refused`。
**但那 4 个对象其实已经传上去了**（PUT 直连 S3 成功）。

⇒ 补两步：verify 走镜像补一遍，再 `--no-verify` 把提交推上去。

#### 完整流程

```bash
cd /userdata/models/Qwen3.5-VL-9B
git init -b main
git remote add origin git@hf.co:wardner/Qwen3.5-VL-9B.git
git fetch origin main
git reset --mixed origin/main          # 索引对齐远端，工作区保留新文件
git config user.name  "wade.weng"
git config user.email "wade.weng@rock-chips.com"
printf '.cache/\n' >> .git/info/exclude # 下载日志不要进仓
git add -A
git commit -m "fix(model): refresh ..."
git push origin main
```

磁盘代价：`git-lfs` 会把权重在本地再存一份到 `.git/lfs/objects`
（6.9 GB）。推完可以 `rm -rf .git` 收回。

#### 一条命令版本

`tools/push_hf_over_ssh.py` 把上面全部固化（含前置检查、幂等续传、收尾复核）：

```bash
sudo install -m 755 tools/hf_lfs_credential_helper.py /usr/local/bin/hf-lfs-cred
python tools/push_hf_over_ssh.py --repo wardner/Qwen3.5-VL-9B \
    --dir /userdata/models/Qwen3.5-VL-9B
```

它会自己提权到 root —— 模型目录属 root，git 的「可疑所有权」保护会拒掉
普通用户（`git config --get` **静默**返回空、`git status` 才报错，很容易
误判成「配置没写」）。

#### 让普通用户也能在模型目录里用 git

```bash
sudo git config --system --add safe.directory /userdata/models/Qwen3.5-VL-9B
```

⚠️ `safe.directory` 的值是**正则**：`--unset-all` 时带 `*` 会把精确路径
那条一起删掉；而且 `/userdata/models/*` 这种 glob **不生效**（git 2.39），
只能逐个目录登记。

### 3.3 两条路线都是增量推送

Hub 的 preupload 按 **sha256** 判重：远端已有的同摘要文件直接跳过，
只传真正变了的那几个。⇒ 换几个文件就是几百 MB，不是整个 6.9 GB。

## 4. 复核

推完**必须**核对，否则只有用户下载时才发现问题：

| 路线 | 怎么验 |
|---|---|
| A（API） | 脚本自带「传后回读校验」：从 Hub 拉回 `SHA256SUMS` 逐条比对 + 核对文件清单 |
| B（SSH） | `git ls-remote origin main` 看 HEAD 是否是新 commit；再用 LFS batch API 查对象是否存在 |

路线 B 的对象级复核（**LFS oid 就是内容的 sha256**，所以 oid 对上了内容就对了）：

```bash
# batch API 用 operation=download 问「远端有没有这些对象」
# → 有 download href = 对象在；没有 = 缺
```

想要字节级确认就回拉一个文件算 sha256（大文件用 HTTP Range 取首尾各 1 MB
即可，不必整份下载）。

## 5. 已发布过的仓

| 仓 | 状态 | 备注 |
|---|---|---|
| `wardner/Qwen3-ASR` | ✅ 已发布（11-17） | 15 个文件（6 LFS + 9 普通 blob），走 SNI 代理 + token |
| `wardner/Qwen3.5-VL-9B` | ✅ 已发布（11-19） | 16 个文件 / 6.89 GB，走 SSH + git-lfs，无 token |
| `wardner/Intern_Decision-4B` | ✅ 已发布 | RKNN 部署包（带 `*_report/` 子目录），走镜像逐文件上传 |
| `wardner/Intern_Decision-0.8B` | ⬜ 未发布 | 仓里已有 manifest，出厂清单已就位 |

`.gitattributes` **会被 Hub 服务端采纳** —— 权重走 LFS，README / LICENSE /
NOTICE / SHA256SUMS / config.json / worker manifest 都是普通 git blob
（可直接在网页上预览和 diff）。

## 6. 相关文档

* [`docs/MODEL_INSTALL_GUIDE.md`](MODEL_INSTALL_GUIDE.md) —— 用户侧：clone 下来怎么装
* [`docs/HF_SNI_PROXY.md`](HF_SNI_PROXY.md) —— 网络侧：`huggingface.co` 的两层封锁与 SNI 绕行代理
* `tools/publish_hf_model.py` —— 路线 A 的发布工具（建仓 + 上传 + 前后校验）
* `tools/push_hf_over_ssh.py` —— 路线 B 的一条命令版
* `tools/hf_lfs_credential_helper.py` —— 路线 B 的 git 凭据助手
* `tools/refresh_model_metadata.py` —— 发布前刷新 `SHA256SUMS` / `config.json`
* `scripts/migrate_worker_manifests.py` —— 把出厂 manifest 铺进模型目录
* `scripts/verify_hf_repo_matches_local.py` —— 逐文件拉回来对账（目前硬编码 4B 仓）
