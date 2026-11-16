# 浏览器麦克风 + 保留 8000 端口 —— 实施方案

> 生成时间：2026-11-16
> 板子：`debian`（RK3588），IP `172.16.9.65`
> 仓库：`/userdata/wdnwebui`（commit `2912003`）
> 配套文档：[`PORT_8000_VS_8443.md`](PORT_8000_VS_8443.md)（两端口差异分析）

> ⚠️ **状态更新（2026-11-16 晚）**：本文档描述的「8443 TLS 中继」方案**已退役**。
> 8000 现在**直接由 uvicorn 提供 TLS**（`--ssl-certfile` / `--ssl-keyfile`），
> 8443 端口不再监听。**§四 的「安装 relay 单元」步骤已作废**；
> §六 的验收命令把 `:8443` 换成 `:8000` 即可。
> 现行部署见 [`PORT_8000_HTTPS_ONLY_MIGRATION.md`](PORT_8000_HTTPS_ONLY_MIGRATION.md)。

---

## 一、需求与硬约束

**目标**：① 浏览器麦克风可用；② 保留 8000 端口访问。

**硬约束（浏览器侧，不可绕过）**：`navigator.mediaDevices` **只在安全上下文**里存在。安全上下文 = `https://` **或** `http://localhost` / `http://127.0.0.1`。

### 实测矩阵（真实 Chromium，非推断）

| 访问地址 | `isSecureContext` | `mediaDevices` | `getUserMedia()` 实调 |
|---|---|---|---|
| `http://127.0.0.1:8000` | ✅ `true` | ✅ 存在 | ✅ 返回 **live** 音轨（48000 Hz / 单声道） |
| `http://172.16.9.65:8000` | ❌ `false` | ❌ **undefined** | ❌ `TypeError: Cannot read properties of undefined` |
| `https://172.16.9.65:8443`（私有 CA 已信任） | ✅ `true` | ✅ 存在 | ✅ 返回 **live** 音轨（48000 Hz / 单声道） |
| `https://172.16.9.65:8443`（自签叶子 + 忽略证书错误） | ✅ `true` | ✅ 存在 | ✅ 返回 **live** 音轨 |

**三条关键推论**：

1. **「保留 8000」与「麦克风可用」不冲突** —— 因为两者可以是**两个入口**，不必是同一个端口。
2. `http://<LAN-IP>:8000` 永远拿不到麦克风，**这不是 webui 的 bug**，是浏览器的硬性限制。
3. `http://127.0.0.1:8000` 是安全上下文 —— 所以**在板子本机**用 8000 调试麦克风是可以的，只是别人从别的机器访问不行。

---

## 二、结论

**采用「TLS 中继」架构：在 8443 端口终结 TLS，明文转给 127.0.0.1:8000。8000 一行不改。**

**这块板子上已经部署并运行中**（`wdnwebui-tls-relay.service`，enabled，已稳定运行 6 个月 19 天）。本报告给出的是**可复制到其他板子的完整落地步骤**，以及**实测验收结果**和**现有部署的缺口清单**。

```
        ┌──────────────────────────────────────────────┐
        │  浏览器                                       │
        └───┬───────────────────────┬──────────────────┘
            │ http://                │ https://
            │  ┌───────────────┐     │  ┌──────────────────────┐
            ├─→│ 现有用法照旧  │     ├─→│ 新增：麦克风可用      │
            │  │ 无证书烦恼    │     │  │ 需要客户端信任 CA     │
            │  └───────────────┘     │  └──────────┬───────────┘
            ▼                        ▼             │
   ┌──────────────────┐   ┌─────────────────────────────┐
   │ uvicorn          │   │ tls_relay                    │
   │ 0.0.0.0:8000     │   │ 0.0.0.0:8443  (TLS 1.3)      │
   │ 明文 HTTP        │◀──│ 注入 X-Forwarded-Proto: https │
   │ 真正的业务       │   │ 字节透传（body / WS 原样）     │
   └──────────────────┘   └─────────────────────────────┘
```

---

## 三、方案对比

| # | 方案 | 8000 保留 | 麦克风可用 | 改动面 | 评价 |
|---|---|---|---|---|---|
| **A** | **TLS 中继 8443 → 8000** | ✅ | ✅ | **1 个新进程 + 1 个新单元** | ✅ **推荐，已部署** |
| B | uvicorn 直接加 `--ssl-keyfile` 上 8000 | ❌ **明文没了** | ✅ | 改 1 行 ExecStart | ❌ 违反约束 ②：现有 `http://…:8000` 的调用方全废 |
| C | 第二个 uvicorn 实例带 TLS 跑 8443 | ✅ | ✅ | 复制整个应用 | ❌ **静默破坏算力发现**（见下） |
| D | nginx / caddy 反代 | ✅ | ✅ | 引入新依赖 | ⚠️ 板子上**没装** nginx/caddy；收益仅限 HTTP/2 |
| E | 8000 端口同时嗅探 HTTP 与 TLS | ⚠️ 需按首字节分流 | ✅ | 重写 relay | ❌ 复杂度高、无收益 |
| F | 客户端 Chrome flag | ✅ | ✅（仅该浏览器） | 无服务端改动 | ❌ 不可部署：需每个客户端加启动参数 |

### 为什么排除 B

uvicorn 的 `--ssl-keyfile` 让该端口**只**说 HTTPS。现有 `http://<板子IP>:8000` 的使用者（板载 agent、回归脚本、局域网别的人）会直接握手失败。约束 ②「保留 8000 明文访问」被破坏。

### 为什么排除 C（这是最危险的一个）

第二个 uvicorn 会**再起一份 ComputeRegistry**。而 DDS 的 participant 名是：

```python
# app/core/compute/registry.py:1318
participant_name=f"wdnwebui-main-{device_name}",   # device_name 来自 config，不是 PID
```

同一个 domain 里出现**同名 participant** 会**静默破坏发现**。代码里已经把这个坑写死了（`registry.py:1453-1457`）：

> *"participant names must be unique within a domain, and a duplicate name corrupts discovery **silently** — the second worker's RPCs stop routing while the process still answers, so the symptom is 'this model never finishes loading' rather than anything that points at DDS."*

⇒ 症状是「模型一直加载不出来」，但没有任何日志指向 DDS。**这是最难查的一类故障。**

### 为什么 D 不是最优

nginx/caddy 能做，而且比裸 relay 多两个能力：**HTTP/2**、**更成熟的连接管理**。但：

* 板子上**没装** nginx/caddy（已确认 `which nginx caddy` 为空），引入它意味着多一个要维护的依赖；
* 当前 uvicorn 走 h11，本身就是 **HTTP/1.1**，所以 8443 现在也是 HTTP/1.1 —— **对现有前端没有功能差异**；
* 裸 relay 已经在板上跑了 6 个多月，**零故障**。

⇒ 若将来确实需要 HTTP/2 或访问控制，再换 D 也不迟，且届时可以保持 8000 明文不变。

---

## 四、落地步骤（复制到一块新板子）

### 步骤 1：签发证书（私有 CA 方案）

```bash
cd /userdata/wdnwebui
bash scripts/gen_tls_ca.sh $(hostname -I | awk '{print $1}')
```

产物（**全部在仓库外**，保持 `git status` 干净）：

```
/home/rockchip/.config/wdnwebui/tls/
├── ca.pem        ← CA 根（自签）—— 发给客户端的就是这个
├── ca-key.pem    ← CA 私钥（只在签发时用，建议签完挪走离线保管）
├── cert.pem      ← 服务端叶子证书（由 CA 签发）—— relay 加载它
└── key.pem       ← 服务端私钥
```

**为什么用私有 CA 而不是自签叶子**：自签叶子只有两条路 —— 每次访问点「高级 → 继续前往」，或手工装进信任库（且每次续签/换 IP 都要重新分发）。私有 CA 下客户端只装**一个**根证书（永久有效），叶子可随时续签/换 IP 而**不用再动客户端**。

### 步骤 2：安装 relay 的 systemd 单元

```bash
sudo tee /etc/systemd/system/wdnwebui-tls-relay.service >/dev/null <<'EOF'
[Unit]
Description=wdnwebui TLS relay (https://<board-ip>:8443 -> http://127.0.0.1:8000)
Documentation=file:/userdata/wdnwebui/scripts/tls_relay.py
After=network-online.target wdnwebui.service
Wants=network-online.target

[Service]
Type=simple
# 用普通用户跑：relay 只需要读证书 + 连本机 8000，不需要 root。
User=rockchip
Group=rockchip
WorkingDirectory=/userdata/wdnwebui
ExecStart=/userdata/wdnwebui/.venv/bin/python -u scripts/tls_relay.py
Environment=PYTHONUNBUFFERED=1
Environment=WDNAI_TLS_LISTEN_HOST=0.0.0.0
Environment=WDNAI_TLS_LISTEN_PORT=8443
Environment=WDNAI_TLS_TARGET_HOST=127.0.0.1
Environment=WDNAI_TLS_TARGET_PORT=8000
Restart=on-failure
RestartSec=5
# 证书不在仓库里，缺了就直接报错退出（别静默降级成明文）。
RestartPreventExitStatus=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now wdnwebui-tls-relay.service
```

> **两个关键点**：
> * `User=rockchip` —— relay 只需读证书 + 连本机 8000，不需要 root。
> * `RestartPreventExitStatus=1` —— relay 缺证书时 `return 1` 退出，systemd **不会**反复拉起它。这是刻意的「宁可不提供 HTTPS，也不静默降级成明文」。

### 步骤 3：后端中间件

**已就位，无需改动**。`app/main.py:188` 已挂：

```python
app.add_middleware(XForwardedProtoMiddleware)
```

它把 relay 注入的 `X-Forwarded-Proto: https` 落到 `scope["scheme"]`，WebSocket 相应映射成 `ws` / `wss`。

**只信任回环对端**（`app/core/forwarded.py:25`）：

```python
TRUSTED_PEERS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})
```

因为 8000 对整个局域网开放，任意客户端都能自己塞 `X-Forwarded-Proto` 进来 —— 非回环塞了不认。

### 步骤 4：前端

**零改动**。WebSocket 地址按 `location.protocol` 自动选 `ws://` / `wss://`：

| 位置 | 代码 |
|---|---|
| `app/system/static/system.js:693` | `const proto = location.protocol === "https:" ? "wss:" : "ws:";` |
| `app/system/static/system.js:1734` | 同上（coprocessor/ws） |
| `app/system/static/im_bridge/app.js:93` | 同上（wechat/ws） |
| `app/static/core/audio-analysis.js:244` | 同上（audio-analysis/ws） |

### 步骤 5：客户端信任 CA

```bash
# 在板子上把根证书取出来
scp rockchip@172.16.9.65:/home/rockchip/.config/wdnwebui/tls/ca.pem .
```

| 客户端 | 装法 |
|---|---|
| Chrome/Edge (Linux) | Chrome 读 **NSS 用户库，不读** `/etc/ssl/certs` → `certutil -A -d sql:$HOME/.pki/nssdb -t "C,," -n "wdnwebui Local CA" -i ca.pem` |
| Chrome/Edge (Windows) | 双击 `ca.pem` → 本地计算机 → 受信任的根证书颁发机构 |
| macOS | 钥匙串访问 → 拖进「系统」钥匙串 → 「始终信任」 |
| Firefox | 设置 → 隐私与安全 → 证书 → 查看证书 → 颁发机构 → 导入 → 勾「信任此 CA 来标识网站」 |
| Android | 设置 → 安全 → 加密与凭据 → 安装证书 → CA 证书 |
| iOS | 装成描述文件 → 通用 → 关于本机 → 证书信任设置 → **手动打开信任开关**（iOS 13+ 对自建 CA 的额外要求） |

### 步骤 6：自检

```bash
CA=/home/rockchip/.config/wdnwebui/tls/ca.pem

systemctl status wdnwebui.service wdnwebui-tls-relay.service --no-pager
ss -ltnp | grep -E '8000|8443'

curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/
curl -sS --cacert $CA -o /dev/null -w "%{http_code} verify=%{ssl_verify_result}\n" https://127.0.0.1:8443/

# scheme 传递：两个 Location 必须各自带 http / https
curl -sSI http://127.0.0.1:8000/api/tts        | grep -i location
curl -sSI --cacert $CA https://127.0.0.1:8443/api/tts | grep -i location

python -m scripts.verify_tls_scheme    # 8/8
```

---

## 五、当前板子的现状与缺口

### 5.1 已就位（实测确认）

| 项 | 状态 |
|---|---|
| `wdnwebui-tls-relay.service` | ✅ enabled + active，pid 1868，已跑 **6 个月 19 天** |
| relay 进程 | ✅ `User=rockchip` 降权，RSS **44.8 MB**（对比 uvicorn 2.4 GB） |
| 证书链 | ✅ 私有 CA 签发，`CN=172.16.9.65`，SAN 含 IP/debian/localhost，有效期至 **2036-11-05** |
| 板子自身信任 | ✅ `/usr/local/share/ca-certificates/` + `~/.pki/nssdb` 均已装根证书 |
| 不加 `--cacert` 直连 | ✅ `https://172.16.9.65:8443/` → `code=200 verify=0` |
| `XForwardedProtoMiddleware` | ✅ 已挂载，307 Location 正确区分 http/https |
| relay 的 TLS 段回归 | ✅ `scripts/verify_tls_scheme` **8/8 通过**（含 256 KB 二进制体在 Content-Length / chunked 两种模式下完整送达） |

### 5.2 缺口 1（**最重要**）：relay 单元不在任何安装脚本里

* `install_autostart.sh` 只写 `wdnwebui.service`（`WEB_UNIT` 变量，**没有任何 relay 相关代码**）
* `update_rkwhl.sh` 只更新 SDK 与 app 包，**完全不碰 systemd**
* 当前板子上的 relay 单元是**手工安装**的

⇒ **一块全新板子按现有脚本装完，是没有 8443 的**，也就没有浏览器麦克风。

**修法**：在 `install_autostart.sh` 里加一个 `write_relay_unit()`（内容即 §四·步骤 2 的单元文件），并在 `write_web_unit` 后调用；`--uninstall` 分支里一并 `disable --now` + `rm -f`。

### 5.3 缺口 2：证书签发也不在安装脚本里

`scripts/gen_tls_ca.sh` 需要人工执行，且 `gen_tls_ca.sh` 会**自动取本机 IP** 做 SAN。装板脚本可以：

* 在首次安装时自动签发一次（IP 变了可重签，CA 不变 → **客户端不用重装**）
* 或至少在 `install_autostart.sh` 末尾打印一句提示

### 5.4 缺口 3：两个端口都没有访问控制

`app/main.py` 只有 `XForwardedProtoMiddleware` + `GZipMiddleware`，**没有 auth、没有 CORS**。TLS 解决的是「加密 + 安全上下文」，**不解决访问控制**。任何局域网设备都能打开算力配置页、启停模型。

若要收紧，建议**在 relay 层加 IP 白名单**（而不是动 8000）—— 8000 保留给内网板载调用。

### 5.5 缺口 4：ASR 能力当前未启用（与端口无关，但影响麦克风验收）

`config.jsonc` 的 `compute.autostart` 只有 `qwen3.5-vl-4b`；`qwen3-asr` 模型**已安装**（`/userdata/models/Qwen3-ASR/`）但**未运行** ⇒ `swcap_audio_asr` 能力缺失。

后果：ASR 卡的实时区块虽然可见，但点「开始实时识别」会拿到：

```
POST /api/qwen3-asr/live/start → 502 {"detail":"功能未启用: swcap_audio_asr"}
前端状态栏：无法开始：功能未启用: swcap_audio_asr
```

**这是算力问题，不是端口问题。** 要跑通麦克风 E2E，需要先在 NPU 上加载 `qwen3-asr`。

> ⚠️ 当前 3 张 NPU 卡**全部占满**（npu 0/1 = DepthAnythingV3，npu 2 = qwen3.5-vl-4b）。要腾出一张卡给 ASR，得先停掉一个实例。

---

## 六、验收结果

### 6.1 已验证 ✅

| # | 验收项 | 方法 | 结果 |
|---|---|---|---|
| 1 | 安全上下文矩阵（4 场景） | 真实 Chromium + `getUserMedia()` 实调 | ✅ 见 §一 |
| 2 | scheme 传递 | 307 Location 对比 | ✅ `http://…:8000` vs `https://…:8443` |
| 3 | HTTP 语义等价 | `/live/start` + `/live/chunk` 两条链路对照 | ✅ **状态码与响应体逐字节一致** |
| 4 | WS 升级等价 | `ws://` vs `wss://` 握手 + 首帧 | ✅ **101 established，首帧均 278 B 且内容逐字节一致** |
| 5 | 二进制体完整性 | `verify_tls_scheme` 256 KB / Content-Length + chunked | ✅ 原样送达 |
| 6 | 浏览器卡片页（8443） | 真实 Chromium 走 router → 卸向导 → 切「浏览器麦克风」页签 | ✅ 页签可见、实时区块展开、`#asr-mic-btn` **可用**、安全上下文提示条**正确隐藏** |
| 7 | 浏览器实际走 HTTPS | 捕获前端请求 | ✅ `POST https://172.16.9.65:8443/api/qwen3-asr/live/start`（87 B JSON）确实发出并抵达后端 |

### 6.2 未验证 ⚠️

| # | 验收项 | 阻塞原因 |
|---|---|---|
| 8 | 麦克风采集 → 音频块上行 → WS 推送 → 文本产出（端到端） | `swcap_audio_asr` 未启用；且需先打开麦克风，而前端在 `/live/start` 失败时**提前 return**，不会打开麦克风（`app.js:1942-1957`） |

> 第 8 项的 TLS 相关环节已被第 3/4/5/7 项覆盖；剩下的是**模型与算力**问题。
> 要补齐：停掉一个 DepthAnythingV3 实例腾出 NPU 卡 → `POST /api/compute/instances {"model":"qwen3-asr","npu_index":1}` → 重跑 `WDNWEBUI_BASE=https://172.16.9.65:8443 python scripts/verify_asr_wizard.py`。

### 6.3 验收矩阵（同一份脚本覆盖两条路径）

```bash
# 安全上下文 → 88 条全过（含浏览器麦用例 V5/V6a/V6b/V6c/V5c）
WDNWEBUI_BASE=https://172.16.9.65:8443 python scripts/verify_asr_wizard.py

# 明文 LAN IP → 72 条过，16 条浏览器麦用例**按设计跳过**
#   （非安全上下文里浏览器根本不提供 mediaDevices —— 功能没坏，是这个访问方式用不了浏览器麦）
WDNWEBUI_BASE=http://172.16.9.65:8000 python scripts/verify_asr_wizard.py
```

---

## 七、故障排查对照表

| 症状 | 判据 | 原因 | 处理 |
|---|---|---|---|
| 浏览器报「不是私密连接」 | `openssl x509 -noout -issuer` | 客户端不认 CA | 按 §四·步骤 5 装 `ca.pem`（Chrome 用 NSS 库） |
| `ERR_CERT_COMMON_NAME_INVALID` | `openssl x509 -noout -ext subjectAltName` | 访问地址不在 SAN 里 | 用 SAN 里的地址，或重签叶子 |
| 页面能开但 `Failed to fetch` | `curl -sSI https://…:8443/api/tts \| grep -i location` | 后端没收到 `X-Forwarded-Proto` → Location 写成 `http://` → mixed content | 查 `app/main.py:188` 挂载 + relay 是否注入 |
| 8443 连不上 | `systemctl status wdnwebui-tls-relay` | 单元没装 / 缺证书 | 装单元；`bash scripts/gen_tls_ca.sh` |
| 麦克风面板打不开 | `window.isSecureContext` | 用了 `http://<LAN-IP>:8000` | 改用 `https://…:8443` |
| 麦克风按钮点了没反应 | `navigator.mediaDevices` | 同上 | 同上 |
| 实时区块整个不出现 | 快照的 `live_enabled` | 后端 feature flag 关了 | 查 `config.jsonc["asr"]["live"]["enabled"]` |
| 「无法开始：功能未启用: swcap_audio_asr」 | `GET /api/compute` | ASR 模型没加载 | 在算力配置页启用 `qwen3-asr` |
| 音频块上传失败 / 卡顿 | relay 日志 | chunked 请求体被降级成一连接一请求（刻意） | 正常设计，二进制体不丢 |

---

## 八、回滚

```bash
# 1) 停掉 HTTPS 入口（8000 明文不受影响）
sudo systemctl disable --now wdnwebui-tls-relay.service
sudo rm -f /etc/systemd/system/wdnwebui-tls-relay.service
sudo systemctl daemon-reload

# 2) 确认 8000 照常
curl -sS -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/
```

**回滚不影响 8000** —— 这是本方案最重要的性质：TLS 是**加法**，不是改造。

若将来把 relay 单元纳入 `install_autostart.sh`，`--uninstall` 分支必须一并清理该单元，否则会留下一个指向已删目录的僵尸单元。

---

## 附录：本次实测的原始数据

```jsonc
// 安全上下文矩阵（真实 Chromium）
A http://127.0.0.1:8000     → isSecureContext=true,  mediaDevices=true,  getUserMedia ok (48000Hz/1ch/live)
B http://172.16.9.65:8000   → isSecureContext=false, mediaDevices=false, getUserMedia TypeError
C https://172.16.9.65:8443  → isSecureContext=true,  mediaDevices=true,  getUserMedia ok (48000Hz/1ch/live)
D https://…:8443 (忽略证书错误) → isSecureContext=true, mediaDevices=true, getUserMedia ok

// TLS 段语义等价（明文 vs TLS，响应逐字节一致）
POST /api/qwen3-asr/live/start  → 8000: 502 {"detail":"功能未启用: swcap_audio_asr"}
                                → 8443: 502 {"detail":"功能未启用: swcap_audio_asr"}   ✅ 一致
POST /api/qwen3-asr/live/chunk  → 8000: 404 {"detail":"会话不存在或已过期"}
                                → 8443: 404 {"detail":"会话不存在或已过期"}             ✅ 一致（32000 B 裸 PCM 送达）
WS   /api/qwen3-asr/ws          → ws:// : 101, 首帧 278 B
                                → wss://: 101, 首帧 278 B，内容逐字节一致              ✅ 一致

// 浏览器侧（8443，真实 Chromium + 假麦克风设备）
isSecureContext      : true
浏览器麦页签可见      : true
实时区块 hidden       : false
#asr-mic-btn disabled : false
安全上下文提示条隐藏  : true          ← 安全上下文下不该显示，正确
点击后实际请求        : POST https://172.16.9.65:8443/api/qwen3-asr/live/start (87 B, application/json)
后端响应              : 502 功能未启用: swcap_audio_asr   ← 算力问题，非 TLS 问题
pageerror             : 无
```

**实测方法说明**：麦克风用 Chromium 的 `--use-fake-device-for-media-stream` 假设备（固定 48 kHz），**没有用真人麦克风**；`getUserMedia()` 返回的是真实 `live` 音轨（`track.readyState === "live"`）。
