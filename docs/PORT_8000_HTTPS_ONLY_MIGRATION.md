# 8000 单端口 HTTPS 化 —— 实施报告（决策已定）

> 生成时间：2026-11-16
> 板子：`debian`（RK3588），IP `172.16.9.65`
> 仓库：`/userdata/wdnwebui`（commit `2912003`） + **wdnapps-audio-chat**（editable 装在 `.venv/src/`）
> 关联文档：[`PORT_8000_VS_8443.md`](PORT_8000_VS_8443.md)、[`BROWSER_MIC_HTTPS_IMPLEMENTATION.md`](BROWSER_MIC_HTTPS_IMPLEMENTATION.md)

---

## 一、决策

> **只保留 8000 端口，且 8000 只提供 HTTPS。**
> **8443 TLS 中继退役，明文 8000 取消。**

本报告是这套配置的**实施定稿**：变更清单、可直接套用的补丁、迁移顺序、验收与回滚。

---

## 二、目标架构

```
        ┌────────────────────────────────────────────┐
        │  浏览器 / 板载 agent / 回归脚本              │
        └───────────────────┬────────────────────────┘
                            │ https://  （唯一入口）
                            ▼
   ┌────────────────────────────────────────────────────┐
   │ uvicorn app.main:app                                │
   │   --host 0.0.0.0 --port 8000                        │
   │   --ssl-certfile .../cert.pem                       │
   │   --ssl-keyfile  .../key.pem                        │
   │                                                     │
   │  ★ TLS 在 uvicorn 进程内终结                        │
   │  ★ scope["scheme"] 天然是 https —— 不需要            │
   │    X-Forwarded-Proto 注入 / 中间件                  │
   │  ★ wss:// 由同一个 TLS 上下文承载                    │
   └───────────────────┬────────────────────────────────┘
                       ▼
              compute_worker 子进程 × N
              （同一进程组，systemd 统一管）
```

**被移除的东西**：

| 移除项 | 位置 |
|---|---|
| TLS 中继进程 | `scripts/tls_relay.py`（随本方案退役，文件保留备查） |
| relay 的 systemd 单元 | `/etc/systemd/system/wdnwebui-tls-relay.service` |
| `X-Forwarded-Proto` 注入逻辑 | `scripts/tls_relay.py` 的 `_RequestRewriter`（整个文件） |
| `XForwardedProtoMiddleware` | `app/core/forwarded.py` —— **建议保留**（见 §三·0） |

**不变的东西**：

* 证书体系**完全不动** —— 还是同一套私有 CA（`wdnwebui Local CA`）+ 叶子证书（SAN 含 `IP:172.16.9.65` / `DNS:debian` / `DNS:localhost` / `IP:127.0.0.1`），10 年有效期
* 客户端信任**完全不动** —— 客户端已经信任了根证书，换端口不影响
* 前端**零改动** —— WebSocket 地址按 `location.protocol` 自动选 `ws://` / `wss://`
* `app/` 生产代码**零改动** —— 实测 `app/` 里 0 处硬编码明文 8000

---

## 三、变更清单

### 0. `app/core/forwarded.py` —— 建议**保留但标注**

改后 uvicorn 原生把 `scope["scheme"]` 设成 `https`，这个中间件**永远不会再命中**。

* **保留**：零成本；万一将来前面又加一层代理（nginx / caddy），它能兜住
* **保留的理由要写进注释**，否则下一个人会以为是死代码删掉

```python
# app/core/forwarded.py 顶部注释追加
#
# 注（2026-11-16 起）：8000 已改为 uvicorn 原生 TLS（--ssl-certfile），
# scheme 由 uvicorn 直接给出，本中间件**不会**再命中。保留是因为：
# 若将来前面再加一层 TLS 代理，它能立刻兜住 scheme 传递，不必重写。
# 回归入口：python -m scripts.verify_tls_scheme
```

### 1. `/etc/systemd/system/wdnwebui.service`

```diff
 [Unit]
 Description=wdnwebui service
 After=network-online.target
 Wants=network-online.target
+# ★ 证书缺失时**不启动**（而不是让 uvicorn 抛异常后无限重启）。
+#   ⚠️ ConditionPathExists 必须写在 [Unit] 段！写进 [Service] 会被 systemd
+#      静默忽略（日志里只有一行 "Unknown key 'ConditionPathExists'"），
+#      等于没有保护 —— 已实测。
+ConditionPathExists=/home/rockchip/.config/wdnwebui/tls/cert.pem
+ConditionPathExists=/home/rockchip/.config/wdnwebui/tls/key.pem
 
 [Service]
 Type=simple
 User=root
 WorkingDirectory=/userdata/wdnwebui
 Environment=WDNWEBUI_CONFIG=/userdata/wdnwebui/config.jsonc
@@
-ExecStart=/userdata/wdnwebui/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 5
+ExecStart=/userdata/wdnwebui/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 \
+  --ssl-certfile /home/rockchip/.config/wdnwebui/tls/cert.pem \
+  --ssl-keyfile /home/rockchip/.config/wdnwebui/tls/key.pem \
+  --timeout-graceful-shutdown 5
 Restart=on-failure
 RestartSec=5
 TimeoutStopSec=20
```

**uvicorn 的参数语义**（`uvicorn/config.py:118-135` 已核实）：`certfile` 是**必填**，`keyfile` 可选但**必须与证书同文件**才能省略。我们的私钥是独立文件 ⇒ **`--ssl-certfile` 和 `--ssl-keyfile` 都要给**。

### 2. `install_autostart.sh` —— 单元模板同步

改 `write_web_unit()` 里的 `ExecStart`（与 §1 同），并把 `ConditionPathExists` 一并写进 `[Unit]` 段。

> ⚠️ 证书路径**不能用 `$SCRIPT_DIR`** —— 证书不在仓库里，固定写 `/home/rockchip/.config/wdnwebui/tls/`。

### 3. `run.py` —— 开发模式要与生产一致

否则 `python run.py` 起的仍是明文 8000，还会**把生产实例顶掉**（`run.py` 的 `_kill_stale_wdnwebui_processes` 会杀掉占着 8000 的 uvicorn）。

```diff
@@ argparse @@
     parser.add_argument(
         "--port",
         type=int,
         default=None,
@@
              "webui serving another port is left alone.",
     )
+    parser.add_argument(
+        "--ssl-certfile",
+        default=os.environ.get("WDNWEBUI_SSL_CERTFILE") or None,
+        help="TLS 证书路径。给了就启用 HTTPS（生产 systemd 单元用的就是它）。"
+             "默认取环境变量 WDNWEBUI_SSL_CERTFILE。",
+    )
+    parser.add_argument(
+        "--ssl-keyfile",
+        default=os.environ.get("WDNWEBUI_SSL_KEYFILE") or None,
+        help="TLS 私钥路径。默认取环境变量 WDNWEBUI_SSL_KEYFILE。",
+    )

@@ uvicorn.run @@
-    uvicorn.run(
+    _ssl = {}
+    if args.ssl_certfile and args.ssl_keyfile:
+        _ssl["ssl_certfile"] = args.ssl_certfile
+        _ssl["ssl_keyfile"] = args.ssl_keyfile
+    uvicorn.run(
         "app.main:app",
         host="0.0.0.0",
         port=port,
         reload=True,
+        **_ssl,
```

配套：开发时用 `WDNWEBUI_SSL_CERTFILE=... WDNWEBUI_SSL_KEYFILE=... python run.py`，或者**用 `--port 8001` 另起一个明文开发实例**（推荐，避免和生产抢端口）。

> ⚠️ 注意 `run.py` 用的是 `parse_known_args()`（`run.py:617`），未知参数会被静默忽略 ⇒ **写错参数名不会报错，只会静默不生效**。加完参数后用 `python run.py --help | grep ssl` 确认。

### 4. `tools/asr_mic_agent.py`（wdnwebui 仓）

```diff
-DEFAULT_URL = "http://127.0.0.1:8000"
+# 8000 已改为原生 TLS。用标准库 urllib 的默认 SSL 上下文即可验签通过：
+#   ① 板子系统信任库已装 wdnwebui Local CA
+#   ② 叶子证书 SAN 含 IP:127.0.0.1 ⇒ 主机名校验也过
+# 实测：urllib.request.urlopen('https://127.0.0.1:8000/health') -> 200
+DEFAULT_URL = "https://127.0.0.1:8000"
```

### 5. `cards/qwen3_asr/board_mic.py`（**wdnapps-audio-chat 仓**）

```diff
-    def __init__(self, *, base_url: str = "http://127.0.0.1:8000",
+    def __init__(self, *, base_url: str = "https://127.0.0.1:8000",
                  agent_path: Optional[Path] = None) -> None:
```

这个 `base_url` 会以 `--url` 传给 agent（`board_mic.py:233`）⇒ **板载麦克风这条生产路径有两个硬编码，分处两个仓**：

| 位置 | 仓 | 改法 |
|---|---|---|
| `tools/asr_mic_agent.py:65` | wdnwebui | `DEFAULT_URL` 改 https |
| `cards/qwen3_asr/board_mic.py:140` | **wdnapps-audio-chat** | `base_url` 默认值改 https |

⇒ **两个仓都要发版**。

### 6. `scripts/` × 67 —— 回归脚本

**先分档**（实测）：

| 档 | 数量 | 改法 |
|---|---|---|
| ✅ 已支持 `WDNWEBUI_BASE` 环境变量 | **6** | **零代码改动**，跑之前设环境变量即可 |
| 🔧 需要改代码 | **61** | 一条 sed 机械替换（见下） |

已支持环境变量的 6 个（改环境变量即可）：
`verify_boot_restore_live.py` / `verify_autostart_badge_ui.py` / `verify_asr_wizard.py` / `verify_model_download_card.py` / `verify_static_delivery.py` / `verify_wizard_cap_states.py`

**61 个的机械改法**（一条命令）：

```bash
cd /userdata/wdnwebui
# 只改 scheme，端口不动。改完逐个验证。
grep -rlE "(http|ws)://(127\.0\.0\.1|localhost):8000" --include="*.py" scripts/ \
  | xargs sed -i -E 's#(http|ws)://(127\.0\.0\.1|localhost):8000#\1s://\2:8000#g'
```

改完**必须**逐个检查两类例外：

| 例外 | 文件 | 处理 |
|---|---|---|
| `ws://` → `wss://` | `scripts/probe_video_depth_ws.py:80` | sed 已一并处理，但要确认 `websockets.connect` 的 `ssl` 参数用的是默认上下文（实测 ✅ 默认就走系统信任库） |
| **`requests`（certifi）** | `verify_ws_disconnect.py`、`verify_asr_live_smoke.py` | ⚠️ **必须额外处理**，见下 |

#### ⚠️ `requests` 的 certifi 陷阱（实测确认）

`requests` 用自己打包的 certifi，**不读**系统信任库 ⇒ 换 https 后会直接失败：

```
requests 默认           -> FAIL SSLError ... SSLCertVerificationError
requests + verify=CA   -> 200
requests + REQUESTS_CA_BUNDLE 环境变量 -> 200
```

**推荐修法（改动最小）**：在这 2 个脚本里把 `requests.get(...)` 加上 `verify=`：

```python
CA = "/home/rockchip/.config/wdnwebui/tls/ca.pem"
requests.get(url, verify=CA, timeout=...)
```

或**一次性环境变量**（适合 CI / 批量跑）：

```bash
export REQUESTS_CA_BUNDLE=/home/rockchip/.config/wdnwebui/tls/ca.pem
```

#### 各客户端库的 HTTPS 可用性（全部实测）

| 库 | 用它的脚本数 | 默认行为 | 结论 |
|---|---|---|---|
| `playwright` | 57 | 读 `~/.pki/nssdb` 共享库 | ✅ **无需改动**（实测 200） |
| `urllib`（标准库） | 28 | `ssl.create_default_context()` → 系统信任库 | ✅ **无需改动**（实测 200） |
| `websockets` | 5 | 同上 | ✅ **无需改动**（实测首帧 278 B） |
| `http.client` | 2 | 同上 | ✅ **无需改动**（实测 200） |
| **`requests`** | **2** | **certifi** | ❌ **需加 `verify=` 或环境变量** |

### 7. `.github/workflows/regression.yml:131`

注释里的 `http://127.0.0.1:8000` 要改。**但要先确认 CI 到底连的是谁** —— GitHub Actions 的 runner 不是这块板子，若它真的依赖本板服务，需要另配可达性方案。

### 8. 文档

| 文件 | 要改什么 |
|---|---|
| `README.md:225,228` / `README_CN.md:198,201` | curl 示例 → `https://127.0.0.1:8000` |
| `docs/TLS_CLIENT_TRUST.md` | **整篇重写**（8443 不存在了；信任步骤本身不变） |
| `docs/WEB_DELIVERY_PERFORMANCE.md:34-37` | 实测表格重跑 |
| `docs/PORT_8000_VS_8443.md` | 顶部加「**历史架构**，已由 8000 原生 TLS 取代」 |
| `docs/BROWSER_MIC_HTTPS_IMPLEMENTATION.md` | §四 步骤 2（中继单元安装）作废 |
| `AGENTS.md` | 「生产 = systemd 单元 `wdnwebui.service`」那段补 TLS 参数 |
| `docs/QWEN3_ASR_*_CHECKLIST.md` | 里面的 `:8443` 引用要全量替换 |

---

## 四、风险与缓解

### 🔴 风险 1：证书从「可降级」变成「全站生死线」

**现状**：证书出问题 → relay 退出 → **只掉 8443**，8000 明文照常可用。

**改后**：证书出问题 → uvicorn 在 `Config.load()` 阶段抛 `FileNotFoundError` → **端口 8000 根本没人监听** → `Restart=on-failure` + `RestartSec=5` → **每 5 秒重启一轮，永不停歇**。

实测证据：

```
$ uvicorn trivial:app --ssl-keyfile /tmp/nope.pem --ssl-certfile /tmp/nope.crt
FileNotFoundError: [Errno 2] No such file or directory
  uvicorn/config.py:129 → ctx.load_cert_chain(...)
退出码 = 1
```

#### 缓解 1：`ConditionPathExists`（实测有效）

写在 **`[Unit]` 段**（见 §三·1 的坑）。实测行为：

```
start 后：ActiveState=inactive  SubState=dead  Result=success  NRestarts=0
```

⇒ **证书缺失时单元被「跳过」而不是重启循环**，日志里一行 systemd 消息，`systemctl status` 显示 `○`（空心圆 = 条件未满足）。**诊断路径清晰，不会刷屏。**

#### 缓解 2：证书巡检定时器

```ini
# /etc/systemd/system/wdnwebui-cert-check.service
[Unit]
Description=wdnwebui TLS 证书巡检（存在性 + 有效期）

[Service]
Type=oneshot
ExecStart=/bin/sh -c '/usr/bin/openssl x509 -checkend 2592000 -noout \
  -in /home/rockchip/.config/wdnwebui/tls/cert.pem \
  && /usr/bin/test -r /home/rockchip/.config/wdnwebui/tls/key.pem \
  || echo "WARN: wdnwebui 证书缺失或 30 天内过期 —— 服务将无法启动"'

# /etc/systemd/system/wdnwebui-cert-check.timer
[Unit]
Description=每日巡检 wdnwebui TLS 证书

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl enable --now wdnwebui-cert-check.timer
systemctl list-timers wdnwebui-cert-check.timer
```

#### 缓解 3：证书路径的稳定性（已核实）

* 证书目录 `/home/rockchip/.config/wdnwebui/tls/` 在 **`/dev/root`** 上（`/userdata` 才是独立挂载）⇒ **不受挂载顺序影响**，开机即可读
* `key.pem` 是 `-rw------- rockchip:rockchip`，主服务以 **root** 运行 ⇒ **可读**（已核实）
* 有效期到 **2036-11-05**（剩余 3641 天）⇒ 十年内不用操心轮换

#### 缓解 4：应急恢复路径（不需要浏览器）

证书坏了 → 网站打不开 → **用 SSH 上板**（不是浏览器）：

```bash
bash /userdata/wdnwebui/scripts/gen_tls_ca.sh $(hostname -I | awk '{print $1}') --force-leaf
sudo systemctl restart wdnwebui
```

⇒ 恢复路径不依赖 WebUI 本身，这是「全站生死线」可接受的前提。

> **必须接受的代价**：**明文旁路永久消失**。现在 `http://<LAN-IP>:8000` 是一个不依赖证书的旁路，证书坏了还能用它打开页面排查问题；改后没有。而另一个 uvicorn 实例做明文旁路**不可行** —— DDS participant 名是 `wdnwebui-main-{device_name}`（基于 config，非 PID），同名 participant 会**静默破坏发现**（`registry.py:1453-1457`）。

### 🟠 风险 2：迁移必然停机，且会连带重载模型

**零停机不可行**（同上，DDS 约束）。只能停旧起新：

* 服务重启本身 ≈ 5 s
* ⚠️ **systemd 的 `KillMode=control-group` 会把 cgroup 里的 `compute_worker` 一起 SIGTERM 掉**
* 当前 `compute.autostart` 只有 `qwen3.5-vl-4b`（npu 2）—— 它会自动回来
* 另外两个 **DepthAnythingV3**（npu 0 / npu 1）是按需起的、**不在 autostart 里** ⇒ **重启后要手工拉回**（每个 worker 加载约 33 s 量级）

⇒ **总停机 ≈ 1~2 分钟**（含模型重载）。建议**低峰期执行**。

### 🟡 风险 3：开发模式与生产抢端口

`run.py` 的 `_kill_stale_wdnwebui_processes` 会杀掉占着目标端口的 uvicorn ⇒ 改后如果开发还用明文 8000，**一跑 `python run.py` 就会把生产的 TLS 实例顶掉**。

⇒ 开发一律 `--port 8001`，或给开发实例也带 TLS 参数。

### 🟡 风险 4：客户端证书信任不受影响，但访问地址变了

客户端之前访问 `https://<IP>:8443`，现在要改成 `https://<IP>:8000`。

* **根证书不用重装**（信任的是 CA 根，端口无关）
* **但书签 / 文档 / 部署脚本里的 `:8443` 要全部替换**
* 服务端证书的 **SAN 不含端口**，所以换端口不需要重签 ✅

---

## 五、迁移执行顺序（Runbook）

### 阶段 A：改代码（**不切流量，可随时中止**）

```bash
cd /userdata/wdnwebui

# A1. 改仓库内的脚本（61 个）
grep -rlE "(http|ws)://(127\.0\.0\.1|localhost):8000" --include="*.py" scripts/ \
  | xargs sed -i -E 's#(http|ws)://(127\.0\.0\.1|localhost):8000#\1s://\2:8000#g'

# A2. 手工补 2 个 requests 脚本的 verify=
#     verify_ws_disconnect.py / verify_asr_live_smoke.py

# A3. 板载麦克风（两个仓）
sed -i 's#http://127.0.0.1:8000#https://127.0.0.1:8000#' tools/asr_mic_agent.py
sed -i 's#http://127.0.0.1:8000#https://127.0.0.1:8000#' \
  .venv/src/wdnapps-audio-chat/src/wdnapps_audio_chat/cards/qwen3_asr/board_mic.py

# A4. run.py 加 --ssl-certfile/--ssl-keyfile（见 §三·3）

# A5. install_autostart.sh 的 ExecStart（见 §三·2）
```

### 阶段 B：切流量（**有停机**）

```bash
# B1. 停 relay（否则它把明文发给已变 TLS 的 8000）
sudo systemctl stop wdnwebui-tls-relay.service

# B2. 备份并改主服务单元
sudo cp /etc/systemd/system/wdnwebui.service \
        /etc/systemd/system/wdnwebui.service.bak-$(date +%F)
sudo vi /etc/systemd/system/wdnwebui.service      # 按 §三·1 改
sudo systemctl daemon-reload

# B3. 重启（⚠️ 会连带杀掉 compute_worker）
sudo systemctl restart wdnwebui.service

# B4. 立刻确认 TLS 起来了（这一步过了才算成功）
curl -sS -o /dev/null -w "%{http_code} verify=%{ssl_verify_result}\n" https://127.0.0.1:8000/health
#   期望：200 verify=0
```

### 阶段 C：把算力拉回来

```bash
# autostart 的 qwen3.5-vl-4b（npu 2）会自动回来，确认一下
curl -sS https://127.0.0.1:8000/api/compute | python -m json.tool | grep -A5 capabilities

# DepthAnythingV3（npu 0 / 1）不在 autostart 里，手工拉回
for n in 0 1; do
  curl -sS -X POST https://127.0.0.1:8000/api/compute/instances \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"DepthAnythingV3\",\"npu_index\":$n}" | head -c 200; echo
done
```

### 阶段 D：退役 relay

```bash
# 确认 8000 的 HTTPS 稳定运行一段时间（建议 ≥ 1 天）后再退役
sudo systemctl disable --now wdnwebui-tls-relay.service
sudo rm -f /etc/systemd/system/wdnwebui-tls-relay.service
sudo systemctl daemon-reload
```

### 阶段 E：文档与 CI

按 §三·8 更新文档；`regression.yml` 的 CI 可达性单独确认。

---

## 六、验收清单

```bash
CA=/home/rockchip/.config/wdnwebui/tls/ca.pem

# 1) 8000 只说 HTTPS
curl -sS -o /dev/null -w "https -> %{http_code} verify=%{ssl_verify_result}\n" https://127.0.0.1:8000/health
#   期望：200 verify=0

# 2) 明文确实没了（**绝不能**是 200）
curl -sS --max-time 5 http://127.0.0.1:8000/health; echo "exit=$?"
curl -sS --max-time 5 http://172.16.9.65:8000/health; echo "exit=$?"
#   期望：握手失败 / Connection reset / exit≠0

# 3) scheme 正确（不再需要 X-Forwarded-Proto）
curl -sSI https://127.0.0.1:8000/api/tts | grep -i location
#   期望：location: https://127.0.0.1:8000/api/tts/

# 4) WebSocket 走 wss://
python - <<'EOF'
import asyncio, websockets
async def go():
    async with websockets.connect("wss://127.0.0.1:8000/api/qwen3-asr/ws", open_timeout=10) as ws:
        m = await asyncio.wait_for(ws.recv(), timeout=10)
        print("  wss 首帧", len(m), "字节")
asyncio.run(go())
EOF

# 5) 内部调用方（标准库）
python -c "import urllib.request; print(urllib.request.urlopen('https://127.0.0.1:8000/health',timeout=5).status)"
#   期望：200

# 6) 浏览器麦克风（真实 Chromium，8443 已废，改用 8000）
WDNWEBUI_BASE=https://172.16.9.65:8000 python scripts/verify_asr_wizard.py
#   期望：88 通过 / 0 失败（浏览器麦用例 V5/V6a/V6b/V6c/V5c 全跑，不再跳过）

# 7) 回归脚本批量（注意 requests 的两个要带 REQUESTS_CA_BUNDLE）
export REQUESTS_CA_BUNDLE=$CA
for f in scripts/verify_card_slots.py scripts/verify_static_delivery.py \
         scripts/verify_asr_wizard.py scripts/verify_ws_disconnect.py; do
  .venv/bin/python "$f" > /tmp/$(basename $f).log 2>&1 \
    && echo "OK   $f" || echo "FAIL $f  (见 /tmp/$(basename $f).log)"
done

# 8) 8443 确认退役
ss -ltn | grep 8443 || echo "8443 已关闭"
```

---

## 七、回滚

```bash
# 7.1 恢复主服务单元
sudo cp /etc/systemd/system/wdnwebui.service.bak-$(date +%F) /etc/systemd/system/wdnwebui.service
sudo systemctl daemon-reload
sudo systemctl restart wdnwebui.service

# 7.2 relay 想恢复随时可以（它连的是明文 8000，恢复后立刻可用）
sudo systemctl enable --now wdnwebui-tls-relay.service

# 7.3 算力拉回（同阶段 C）

# 7.4 代码回滚
git -C /userdata/wdnwebui checkout -- scripts/ tools/asr_mic_agent.py run.py install_autostart.sh
```

**回滚代价**：又一次服务重启 ⇒ 又一次模型重载（约 1~2 分钟）。

---

## 八、附录：实测数据

### 8.1 延迟对照（N=60，keep-alive 单连接）

```
A trivial 明文 :8445          median 0.23 ms   p95 0.76 ms   min 0.18 ms
B trivial 原生TLS :8444       median 0.86 ms   p95 0.92 ms   min 0.63 ms
C 真实 uvicorn 明文 :8000     median 15.95 ms  p95 16.81 ms  min 15.2 ms
D 真实+中继 TLS :8443         median 17.73 ms  p95 19.23 ms  min 17.05 ms
```

⇒ **省掉中继一跳 ≈ 1.15 ms/请求**；TLS 握手本身 ≈ 0.63 ms（省不掉）。

### 8.2 证书缺失的行为

```
uvicorn 缺证书 → FileNotFoundError (uvicorn/config.py:129) → 退出码 1
ConditionPathExists 在 [Unit] 段 → 单元被跳过，NRestarts=0，无重启循环
ConditionPathExists 误放 [Service] 段 → systemd 日志 "Unknown key ... ignoring"，
                                      等于没有保护（实测）
```

### 8.3 各客户端库在 HTTPS 下的实测

```
curl 系统信任库                → 200 verify=0
urllib.request.urlopen         → 200（默认 ctx 加载 149 张 CA）
playwright 默认 profile        → 200（Chromium 读 ~/.pki/nssdb 共享库）
playwright ignore_https_errors → 200
websockets.connect(wss://)     → 首帧 278 字节
requests 默认（certifi）         → FAIL SSLCertVerificationError
requests + verify=CA           → 200
requests + REQUESTS_CA_BUNDLE  → 200
```

### 8.4 受影响文件分类（不含 `docs/` `.venv/`）

```
scripts/ : 67   （其中 6 个已支持 WDNWEBUI_BASE ⇒ 零代码改动；61 个需 sed）
tools/   :  2   （asr_mic_agent.py [生产] + rknn2_vae/e2e_topo.sh）
.github/ :  1   （regression.yml）
app/     :  0   ← 应用代码与端口协议无关
合计     : 70
```

**另一个仓**（wdnapps-audio-chat）：

```
wdnapps_audio_chat/cards/qwen3_asr/board_mic.py:140
    def __init__(self, *, base_url: str = "http://127.0.0.1:8000", ...)
    → 以 --url 传给 agent（board_mic.py:233）
```

### 8.5 环境事实

```
uvicorn 0.52.4 | httptools ✅ | uvloop ✅ | websockets ✅ | h2 ❌（→ 无 HTTP/2）
OpenSSL 3.0.20
证书 CN=172.16.9.65, O=wdnwebui | issuer CN=wdnwebui Local CA
SAN: IP:172.16.9.65, DNS:debian, DNS:localhost, IP:127.0.0.1
有效期: 2026-11-08 → 2036-11-05（剩余 3641 天）
证书目录在 /dev/root（非 /userdata 独立挂载）⇒ 不受挂载顺序影响
key.pem -rw------- rockchip:rockchip（主服务以 root 运行 ⇒ 可读）
```

---

## 九、实施中必须记住的三件事

1. **`ConditionPathExists` 必须写在 `[Unit]` 段** —— 放 `[Service]` 段会被 systemd 静默忽略，保护等于零（实测）。
2. **`requests` 是唯一不读系统信任库的客户端** —— 2 个脚本要加 `verify=` 或 `REQUESTS_CA_BUNDLE`（实测）。
3. **迁移跨两个仓** —— `wdnwebui` + `wdnapps-audio-chat`，只改一个仓，板载麦克风会连不上。
