[English](README.md) | [简体中文](README_CN.md)
# wdnwebui

一个 Python web 服务，通过 FastAPI 以 HTTP 方式对外暴露 Rockchip 平台 SDK
（[`wdnav`](https://github.com/rockchip-wardner/wdnav) —— MPP / RGA2 / V4L2 视频，
[`wdnai`](https://github.com/rockchip-wardner/wdnai) —— RKNN LLM / VLM）。
单页 UI 在 `http://<host>:8000/`，四个板块：系统监控、视频检测、系统日志、
电源控制（关机 / 重启）。

## 环境要求

- **Python 3.11+**（3.11.2 已验证可用）
- **Debian / Ubuntu** 主机（Debian 的 PEP 668 会拦掉系统级 pip —— 我们用 venv）
- **Git + SSH** 访问 `git@github.com` 才能拉 SDK
  （`git clone` 失败就找运维要部署密钥）
- **电源控制**（关机 / 重启 按钮）要求服务以 root 运行，否则这两个按钮会报 EPERM

在真实的 Rockchip 目标板上跑视频检测页，主机需要 BSP 里的 `librockchip_mpp` 和
`librga`。`import wdnav` 在任何主机上都能用；`import wdnav.video` 只在目标板上能用。

## 1. 克隆

```bash
git clone <this-repo-url> wdnwebui
cd wdnwebui
```

## 2. 创建虚拟环境

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip          # optional but recommended
```

## 3. 安装 web 依赖

```bash
pip install -r requirements.txt
```

这会拉进 FastAPI、Uvicorn、Pydantic、psutil、opencv-python-headless、
python-multipart、**`gguf`**、**`transformers`**、**`tokenizers`** 和
**`v4l2`**。

> **注**：这里单独列出这三个（`gguf` / `transformers` / `tokenizers`），是因为
> SDK 是用 `--no-deps` 装的（§4），自己的依赖永远不会被带进来。
> `scripts/check_sdk_deps.py`（§4c）审的就是这件事 —— 每次装完或更新完 SDK
> 都跑一遍。

- `gguf` —— `wdnai.gguf_tokenizer` 用它解析每个 RKNN 模型自带的
  `*.tokenizer.gguf` 文件。`wdnai` SDK import 了它但没声明成依赖，
  所以在这里钉住。
- `transformers` / `tokenizers` —— `wdnai.gguf_tokenizer` 通过它们加载每个
  RKNN 模型的 `*.tokenizer.gguf`，少了这两个模型连*启动*都做不到
  （`ModuleNotFoundError: No module named 'tokenizers'` → 算力页显示
  「启动失败」）。`wdnai` SDK 在自己的 `pyproject.toml` 里声明了这两个包，
  但它是用 `pip install --no-deps -e` 装的（§4b），完全跳过依赖解析 ——
  所以必须在这里列出来。不需要 PyTorch：只用到 `transformers` 里的
  tokenizer 部分。
- `v4l2` —— 第三方 PyPI 包（纯 ctypes 的 Video4L2 绑定），
  `wdnav/src/wdnav/v4l2.py` 在模块级 import 它。
  `wdnav` SDK 声明了它，但同样是用 `pip install --no-deps -e` 装的（§4b），
  所以它自己永远进不了 venv。缺了它，每次 HDMI-IN 快照请求都会失败：
  `ModuleNotFoundError: No module named 'v4l2'` → 503。

  > **警告**：上游 `v4l2` 0.2（2014）里还留着 Python 2 写法
  > `range(...) + [...]`，在 Python 3 上**import 阶段**就抛：
  > `TypeError: unsupported operand type(s) for +: 'range' and 'list'`。
  > 这**不是** `ImportError`，所以直接 `pip install v4l2` 会把本来干净的
  > 503 变成未处理的 500。装完跑一次补丁脚本：
  >
  > ```bash
  > .venv/bin/python scripts/fix_v4l2_py3.py
  > ```
  >
  > 脚本是幂等的，自带校验；`update_rkwhl.sh` 和
  > `bootstrap_wdndds_native.sh` 已经会替你调用。
- `wechatbot-sdk` —— 微信 iLink bot 的异步客户端；驱动 im-bridge 页面的
  同步封装在 `app/services/wechat_bot.py`（0.5+ 起从 `wdndds.Wechat`
  内联过来）。

> **注**：从 `wdnai ≥ 0.4.0` 起，`rknn3-toolkit-lite` wheel **不再**是
> wdnwebui 的依赖。`wdnai` 自带了 vendored 的 ctypes shim（`wdnai._librknn`），
> 直接驱动 `/usr/lib/librknn3_api.so`。内核态驱动（`/dev/rknn3`）和用户态
> 库（`librknn3_api.so` + 辅助二进制 `rknn3_startup`、`rknn3_model_test` 等）
> 仍然来自 BSP 镜像，主机上必须存在。在全新的目标板上要先跑一遍 BSP 安装器；
> Rockchip 标准镜像里已经预装好了。Web 服务没有它们照样能启动，但系统监控的
> NPU 区块和 VLM 检测器会报 "offline" / 加载失败。

## 4. 安装 SDK

这两个包**不在** `requirements.txt` 里，因为它们不在 PyPI 上。
SSH key 配好之后手动装：

```bash
pip install -e "git+ssh://git@github.com/rockchip-wardner/wdnav.git#egg=wdnav"
pip install -e "git+ssh://git@github.com/rockchip-wardner/wdnai.git#egg=wdnai"
pip install -e "git+ssh://git@github.com/rockchip-wardner/wdndds.git#egg=wdndds"
```

命令报 `Permission denied (publickey)`，说明主机要么连不上 `github.com`，
要么 SSH key 没加载。

> **注**：在没网的隔离板子上，把这些地址指向内部镜像
> （`git+ssh://git@10.10.10.10/wardner/<pkg>.git`）—— `update_rkwhl.sh`（§4b）
> 做现场部署时用的就是它。

装完之后它们在 `.venv/src/wdnav/` 和 `.venv/src/wdnai/`。

## 4b. 保持 SDK 最新

拉完上游改动（或例行同步）之后跑：

```bash
./update_rkwhl.sh
```

它对 `.venv/src/` 下每个 editable 包执行 `git pull` —— 四个 SDK
（wdnav / wdnai / wdndds / wdnhid）和两个卡片插件
（wdnapps_video_analyzer / wdnapps_hdmi_processor）—— 然后对每个包跑
`pip install --no-deps -e`。wdndds 和 wdnav 的 `.so` 重建钩子就是这么触发的。
HEAD 前进 → 重建（每个 SDK 约 5–20s）。HEAD 没动的包不动。

这两个插件包没有 `.so` 可断言，脚本改成检查每张卡是否真的注册上了：每个
`wdnwebui.cards` entry point 必须能解析到一个卡片目录，该目录的
`manifest.json` 里 `"id"` 要和 entry point 名字一致，`static/` 也要存在。
这样能抓到 pip 装成功、但宿主启动时把卡全丢了的那种静默失败。手动同步单个
包的最小做法是 `pip install --no-deps -e .venv/src/<pkg>`；在全新主机上，
apt 依赖和 native lib 内联仍然要靠 `sudo ./bootstrap_wdndds_native.sh`。

所有包处理完之后，脚本会审一遍 SDK 声明的依赖（§4c），有缺失或无法 import
就让整轮失败 —— 否则 `--no-deps` 装的代价只是等到有人打开受影响的页面时才发现。

## 4c. 审查 SDK 依赖

SDK 是用 `pip install --no-deps -e` 装的，所以**它们自己声明的依赖永远进不了
venv**。这是故意的 —— 这些私有包不在 PyPI 上（`wdndds` → `wdnai`，卡片插件 →
`wdnhid` / `wdnav`），让 pip 去解析会直接失败；就算能解析，还会把
`opencv-python`（GUI 版）跟钉住的 `opencv-python-headless` 一起拉进来，
两个抢同一个 `cv2` 包；`av` 需要手工挑版本；目标板也常常根本没有 PyPI 访问权限。

真正的坑是：缺依赖只有用到那个功能时才会暴露，而症状离原因十万八千里：

| 缺什么 | 症状 |
|---|---|
| `v4l2` | 每次 HDMI-IN 快照 → 503 `No module named 'v4l2'` |
| `tokenizers` / `transformers` | 算力页显示「模型启动失败」 |

所以装完或更新完 SDK 之后跑一次审查：

```bash
.venv/bin/python scripts/check_sdk_deps.py
```

它读 `.venv/src/` 下每个 SDK 的 `[project].dependencies`，逐条对照**已安装**的
发行包（`importlib.metadata` —— 不查 index，所以离线可用），校验版本约束，
然后**真的 import** 一下模块。最后这一步很关键：「装了但是坏的」在这里是真实
存在的一类 —— 上游 `v4l2` 0.2 在 Python 3 下 import 时就抛 `TypeError`（§3），
而 `importlib.util.find_spec` 看不出来。

退出码就是契约：

| 退出码 | 含义 |
|---|---|
| 0 | 所有声明的依赖都已安装且可 import |
| 1 | 有缺失 / 版本不匹配 / 无法 import |
| 2 | 找不到 SDK 源码树或 venv |

`update_rkwhl.sh` 在所有包处理完之后跑它，报告有问题就让整轮失败；
`bootstrap_wdndds_native.sh` 也跑，但只是大声警告（不中断 —— 它自己的活，
wdndds 的 `.so`，到那一步已经做完了）。每条报出来的依赖都附带着修复它的命令。

## 5. 跑服务

开发模式（Python 改动自动重载）：

```bash
python run.py
# → Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```

电源控制要真正能用，得用 sudo 跑（shutdown / reboot 系统调用需要 root）：

```bash
sudo .venv/bin/python run.py
```

生产方式（不自动重载，单 worker）：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

## 6. 验证

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok"}

curl http://127.0.0.1:8000/api/power
# {"uptime_seconds": ..., "uptime_human": "...", "is_root": true}
```

`is_root` 是 `false` 的话，Web UI 顶部会挂一条黄色警告横幅 —— 用 sudo 重启之前，
电源操作是禁用的。

浏览器打开 `http://<host>:8000/` 看完整 UI。开发模式下静态资源带
`Cache-Control: no-cache`，改完 JS / CSS 硬刷新就能看到，不用重启。

## 接口一览

| Method | Path                                | 作用                                      |
| ------ | ----------------------------------- | ----------------------------------------- |
| GET    | `/`                                 | 单页 UI                                   |
| GET    | `/health`                           | 存活检查                                  |
| GET    | `/api/system-monitor`               | CPU / 内存 / NPU 快照                     |
| GET    | `/api/system-logs?unit=&priority=&lines=` | 最近 N 条 journal 条目             |
| WS     | `/api/system-logs/ws?unit=&priority=` | 实时 journal 尾巴                    |
| POST   | `/api/video-detection/upload`       | 暂存本地文件给文件选择器                  |
| POST   | `/api/video-detection/open`         | 打开 RTSP / RTMP / HLS / 文件源           |
| POST   | `/api/video-detection/control`      | `play` / `pause` / `stop`                 |
| POST   | `/api/video-detection/close`        | 关闭当前源                                |
| POST   | `/api/video-detection/prompt`       | 对实时帧启动 VLM 检测                     |
| POST   | `/api/video-detection/prompt/stop`  | 停掉 VLM 检测器                           |
| GET    | `/api/video-detection/events`       | 检测事件历史                              |
| WS     | `/api/video-detection/ws`           | JPEG 帧流 + 状态 + 事件                   |
| GET    | `/api/video-detection/fs?path=`     | 服务端目录列表                            |
| POST   | `/api/tts/start`                   | 起一次 TTS 合成（`{text, ref_speaker, language, volume, ...}`） |
| POST   | `/api/tts/stop`                    | 停掉当前这次 TTS                          |
| GET    | `/api/tts`                         | TTS 状态、帧、audio_sec、elapsed            |
| GET    | `/api/tts/history`                 | 最近的输出 WAV                            |
| WS     | `/api/tts/ws`                      | 每次状态变化推一次快照                    |
| GET    | `/api/power`                        | 运行时长 + `is_root`                      |
| POST   | `/api/power` `{action:"shutdown"\|"reboot"}` | 执行宿主关机 / 重启           |

## 目录结构

```
wdnwebui/
├── run.py                  # uvicorn entrypoint
├── app/
│   ├── __init__.py         # __version__ string
│   ├── main.py             # create_app() factory
│   ├── api/                # routers (power, system_monitor, system_logs, video_detection)
│   ├── services/           # video_session, detector, uploads/
│   └── static/             # index.html + css + js
├── requirements.txt
└── .venv/                  # editable installs of wdnav, wdnai live under .venv/src/
```

## 故障排查

- 点关机时报 **`shutdown: command not found`** —— 二进制在 `/sbin/`，但服务
  的 PATH 里没有。用 `sudo` 起让 /sbin 进 PATH，或者
  `PATH=$PATH:/sbin:/usr/sbin python run.py`。
- 在非 RK 开发机上**视频页报 `wdnav` / `RKNN` import 错误** —— 预期之内；
  SDK 会优雅降级，UI 其余部分照常工作。
- **`.venv/src/*` 的 .pyc 文件报 `Permission denied`** —— venv 是另一个 uid
  建的（比如之前用 sudo 跑过）。用 `python -B run.py` 跳过字节码写入，或者
  `chown -R $USER .venv`。
- **`create_app()` / router 404** —— 确认 `app/api/__init__.py` import 了
  每个子模块（`from . import power, ...`）；少了它，package 对象是空的，
  路由会静默消失。

## License

Apache-2.0。见 [LICENSE](LICENSE)。
