# 浏览器麦克风要 HTTPS —— 客户端信任指引

## 为什么需要这个

浏览器只在**安全上下文**里暴露 `navigator.mediaDevices`（麦克风 API）：

| 访问地址 | 安全上下文 | `navigator.mediaDevices` | 浏览器麦克风 |
|---|---|---|---|
| `https://<板子IP>:8000` | ✅ | ✅ 存在 | **可用** |
| `https://127.0.0.1:8000` | ✅ | ✅ 存在 | 可用（只能本机访问） |
| `http://<板子IP>:8000` | ❌ | ❌ **undefined** | **打不开**（明文已被切断） |

> ⚠️ **2026-11-16 起，8000 已改为 uvicorn 原生 TLS**（`--ssl-certfile` /
> `--ssl-keyfile`），**8443 的 TLS 中继已退役**。访问地址从
> `https://<板子IP>:8443` 变成 `https://<板子IP>:8000`。
>
> * 客户端信任的**根证书没变**（信任的是 CA 根，与端口无关）⇒ **不用重装 CA**
> * 服务端证书的 **SAN 不含端口** ⇒ **不用重签**
> * 明文 `http://…:8000` **已不可用**（curl 返回 exit=52 / Empty reply）

本板部署是**纯 HTTPS**（`0.0.0.0:8000`，uvicorn 原生 TLS），所以从任何机器
用 `https://172.16.9.65:8000` 访问都是安全上下文 —— 浏览器**正常提供**麦克风 API。

> 实测（同一服务、同一浏览器，2026-11-16）：
> `https://127.0.0.1:8000` 与 `https://172.16.9.65:8000` 均
> `isSecureContext=true`、`getUserMedia()` 返回 live 音轨（48 kHz / 单声道）。

## 现在的部署长什么样

```
浏览器  ──https://172.16.9.65:8000──▶  uvicorn（原生 TLS，0.0.0.0:8000）
                                            │  scope["scheme"] 天然是 https
                                            ▼
                                      FastAPI 应用
```

* **TLS 在 uvicorn 进程内终结** —— 不再有中间中继，少一跳、少一个进程。
* **不再需要 `X-Forwarded-Proto`** —— scheme 由 uvicorn 直接给出
  （`app/core/forwarded.py` 的中间件保留但不再命中，详见该文件的说明）。
* **前端不用改** —— WebSocket 地址按 `location.protocol` 自动选 `ws://` / `wss://`。
* **证书缺失 ⇒ 服务不启动** —— systemd 单元里的 `ConditionPathExists`（**必须在
  `[Unit]` 段**）会挡住启动，避免 uvicorn 抛异常后被 `Restart=on-failure`
  无限重启。每日巡检由 `wdnwebui-cert-check.timer` 负责。

相关文件：

| 文件 | 作用 |
|---|---|
| `/etc/systemd/system/wdnwebui.service` | 主服务单元（带 `--ssl-certfile` / `--ssl-keyfile`） |
| `scripts/gen_tls_ca.sh` | **私有 CA** + 签发服务端证书（本文档用的就是它） |
| `scripts/gen_tls_cert.sh` | 另一条路：直接自签一张叶子证书（要点「继续前往」） |
| `scripts/verify_tls_scheme.py` | 回归：转发头 scheme 处理 + 请求体不被搞坏 |
| `scripts/verify_tls_handshake_log.py` | 回归：握手失败落明文 ERROR 日志（asyncio + uvloop） |
| `scripts/verify_run_py_tls_guard.py` | 回归：`run.py` 不许在 8000 上以明文启动 |
| `app/core/tls_logging.py` | 握手失败的明文日志钩子（与 event loop 无关） |
| `/etc/systemd/system/wdnwebui-cert-check.{service,timer}` | 证书存在性 / 有效期每日巡检 |

## 证书在哪

```
/home/rockchip/.config/wdnwebui/tls/
├── ca.pem        ← CA 根证书（自签）—— **发给客户端的就是这个**
├── ca-key.pem    ← CA 私钥（只在签发时用，可挪走离线保管）
├── cert.pem      ← 服务端证书（由 CA 签发）
└── key.pem       ← 服务端私钥
```

> ⚠️ **CA 私钥泄露 = 任何人都能伪造一个本板认可的证书。**
> 签发完建议把 `ca-key.pem` 挪到离线介质，需要续签时再放回来。

## 第一步：把 CA 根证书弄到客户端上

```bash
# 在板子上
scp rockchip@172.16.9.65:/home/rockchip/.config/wdnwebui/tls/ca.pem .
```

## 第二步：装进客户端的信任库

### Chrome / Edge（Linux）

Chrome 在 Linux 上读 **NSS 用户库**，**不读** `/etc/ssl/certs`：

```bash
sudo apt-get install -y libnss3-tools     # 提供 certutil
certutil -A -d sql:$HOME/.pki/nssdb -t "C,," -n "wdnwebui Local CA" -i ca.pem
```

（桌面版 Chrome 也可用 GUI：设置 → 隐私和安全 → 管理证书 → 证书颁发机构 → 导入。）

### Chrome / Edge（Windows）

双击 `ca.pem` → 「安装证书」→ 选 **「本地计算机」** → 选
**「将所有证书放入下列存储」** → **「受信任的根证书颁发机构」** → 完成。

### Chrome / Edge（macOS）

双击 `ca.pem` → 钥匙串访问 → 把 `wdnwebui Local CA` 拖进 **「系统」** 钥匙串 →
双击它 → **「信任」** → 选 **「始终信任」**。

### Firefox

设置 → 隐私与安全 → 证书 → 查看证书 → **「颁发机构」** → 导入 → 选 `ca.pem` →
勾选「信任此 CA 来标识网站」。

### Android

设置 → 安全 → 加密与凭据 → **安装证书** → **CA 证书** → 选 `ca.pem` 文件。

### iOS / iPadOS

用「文件」App 或 AirDrop 把 `ca.pem` 装成描述文件 →
设置 → 通用 → 关于本机 → **证书信任设置** → 打开 `wdnwebui Local CA` 的开关。

> ⚠️ iOS 对**自建 CA** 有额外限制：iOS 13+ 要求 CA 必须有
> `basicConstraints=CA:TRUE` **且带 SAN**（本 CA 已带），并且安装后要在
> 「证书信任设置」里**手动打开信任开关**，否则系统级信任不会生效。

## 第三步：验证

```bash
# 证书链对不对（应该 OK）
openssl verify -CAfile ca.pem /home/rockchip/.config/wdnwebui/tls/cert.pem

# HTTPS 能不能通（不加 -k，不加 --cacert —— 说明系统信任库已经认它）
curl -sS -o /dev/null -w "%{http_code} tls_verify=%{ssl_verify_result}\n" \
     https://172.16.9.65:8000/
#   期望：200 tls_verify=0
```

浏览器打开 `https://172.16.9.65:8000/` —— **地址栏不该再出现「不是私密连接」**，
然后进「流式语音识别」→ 选「浏览器麦克风」→ 点「开始实时识别」。

## 板子上已经装好的部分

本机（板子自己）已经配好，**不用重复做**：

| 位置 | 内容 |
|---|---|
| `/usr/local/share/ca-certificates/wdnwebui-local-ca.crt` | CA 根（`update-ca-certificates` 已跑过）→ `curl` / Python / Firefox 都认 |
| `~/.pki/nssdb` | CA 根（NSS）→ 本机的 Chromium 认（**Playwright 的浏览器页也认**） |
| `wdnwebui.service` | 主服务（`--ssl-certfile` / `--ssl-keyfile`，开机自启） |
| `wdnwebui-cert-check.timer` | 证书存在性 / 有效期每日巡检 |

> ⚠️ **Playwright 的 `page.request`（APIRequestContext）走 Node.js 的 TLS 栈**，
> **既不读 NSS 也不读系统信任库** ⇒ 会报
> `unable to verify the first certificate`。需要它的脚本必须设
> `NODE_EXTRA_CA_CERTS=/home/rockchip/.config/wdnwebui/tls/ca.pem`
> （`scripts/verify_plugin_cards.py` 已经这么做了）。

## 续签 / 换 IP

叶子证书换了**不用**再动客户端 —— 客户端信任的是根：

```bash
# 重签（保留 CA）
bash scripts/gen_tls_ca.sh 172.16.9.65 --force-leaf
sudo systemctl restart wdnwebui.service
```

> ⚠️ 现在 8000 就是 TLS 端口，**重签后必须重启主服务**（不再是只重启 relay）。
> 证书文件缺失时主服务**不会启动**（`ConditionPathExists`）—— 应急恢复走 SSH：
> `bash scripts/gen_tls_ca.sh <IP> --force-leaf && sudo systemctl restart wdnwebui`。

换 IP 时把新 IP 加进 SAN 再重签：

```bash
bash scripts/gen_tls_ca.sh <新IP> --force-leaf
sudo systemctl restart wdnwebui.service
```

## 排查

| 症状 | 多半是 |
|---|---|
| 浏览器还报「不是私密连接」 | CA 没装进**这个浏览器**用的那个库（Chrome 读 NSS，不读系统库） |
| `ERR_CERT_AUTHORITY_INVALID` | 同上；或证书过期（`openssl x509 -enddate` 看） |
| `ERR_CERT_COMMON_NAME_INVALID` | 访问地址不在 SAN 里 → 用 `https://172.16.9.65:8000`（SAN 含 IP + `debian` + `localhost` + `127.0.0.1`） |
| **服务整个起不来**（8000 连不上） | 证书缺失 / 私钥不可读 → `ConditionPathExists` 挡住了启动。查 `journalctl -u wdnwebui` 与 `ls -l /home/rockchip/.config/wdnwebui/tls/` |
| `curl` 报 exit=52（Empty reply） | 用明文 http 访问了 8000 —— 8000 **只说 HTTPS**。服务端会留一行明文日志：`journalctl -u wdnwebui.service \| grep '\[TLS\]'`（见下） |
| 页面能开但 `Failed to fetch` | 查 `curl -sSI https://127.0.0.1:8000/api/tts \| grep -i location` 是否为 `https://` |
| 麦克风还是打不开 | 看浏览器控制台；**必须**是 `https://`（`http://localhost` 在非回环地址上不算） |

### 「Empty reply from server」怎么定位

8000 只说 HTTPS ⇒ 客户端用 `http://` 撞上来时，服务端会**掐掉连接**，
客户端只看到 `curl: (52) Empty reply from server`，**看不出原因**。

服务端现在会留**明文**日志（`app/core/tls_logging.py`）：

```bash
journalctl -u wdnwebui.service | grep '\[TLS\]'
```

```
[ERROR] [TLS] handshake failed  SSLError: [SSL: HTTP_REQUEST] http request (_ssl.c:992)
[ERROR] [TLS] hint: looks like a plaintext client hitting the HTTPS port: the client used http:// instead of https://.
                   The server serves HTTPS only on port 8000 — the client must use https://<host>:8000
```

`[SSL: HTTP_REQUEST] http request` 是 OpenSSL 直接认出来的「对面发的是
一个 HTTP 请求」⇒ 结论就是**客户端把地址写成了 `http://`**。

其它可能的原文：

| 原文 | 含义 |
|---|---|
| `[SSL: HTTP_REQUEST] http request` | 明文客户端撞 HTTPS 端口 |
| `[SSL: WRONG_VERSION_NUMBER] wrong version number` | 同上（老一点 OpenSSL 的措辞） |
| `CERTIFICATE_VERIFY_FAILED` | 客户端**不信任**本板 CA ⇒ 走上面的「装 CA」流程 |
| `The operation did not complete (read)` | 握手超时（网络不通 / 中间设备拦截） |

> ⚠️ 纯 asyncio 下这行还会带 `peer=<ip>:<port>`（对端地址）。
> **生产跑的是 uvloop** ⇒ uvloop 的 SSL 类型不可打补丁 ⇒ **拿不到 peer**。
> 钩子本体走 `ssl.SSLObject.do_handshake`，与 event loop 无关 ⇒
> uvloop 下照样能记，只是没有对端地址。

```bash
systemctl status wdnwebui.service
sudo journalctl -u wdnwebui.service -n 50
sudo ss -ltnp | grep 8000
systemctl list-timers wdnwebui-cert-check.timer
```
