# 浏览器麦克风要 HTTPS —— 客户端信任指引

## 为什么需要这个

浏览器只在**安全上下文**里暴露 `navigator.mediaDevices`（麦克风 API）：

| 访问地址 | 安全上下文 | `navigator.mediaDevices` | 浏览器麦克风 |
|---|---|---|---|
| `https://<板子IP>:8443` | ✅ | ✅ 存在 | **可用** |
| `http://127.0.0.1:8000` | ✅ | ✅ 存在 | 可用（但只能本机访问） |
| `http://<板子IP>:8000` | ❌ | ❌ **undefined** | **打不开** |

本板部署是**纯 HTTP**（`0.0.0.0:8000`，无 TLS），所以从别的机器用
`http://172.16.9.65:8000` 访问时，浏览器**根本不提供**麦克风 API ——
不是 webui 的 bug，是浏览器的硬性限制。

> 实测（同一服务、同一浏览器）：
> `http://127.0.0.1:8000` → `isSecureContext=true`、麦克风可用；
> `http://172.16.9.65:8000` → `isSecureContext=false`、`mediaDevices` 不存在。

## 现在的部署长什么样

```
浏览器  ──https://172.16.9.65:8443──▶  tls_relay（TLS 终结，8443）
                                            │  注入 X-Forwarded-Proto: https
                                            ▼
                                      uvicorn（明文，127.0.0.1:8000）
```

* **`8000` 一行没改** —— 板子上别的人还在用 `http://…:8000`，照常可用；
  内部调用方（板载麦克风 agent、回归脚本）也**不受影响**。
* **`8443` 是新增的** —— TLS 在 relay 里终结，转成明文再给后端。
  后端通过 `X-Forwarded-Proto` 知道原始 scheme（否则 307 重定向会写出
  `http://` 的 Location，https 页面按 mixed content 拦掉 → `Failed to fetch`）。
* **前端不用改** —— WebSocket 地址按 `location.protocol` 自动选 `ws://` / `wss://`。

相关文件：

| 文件 | 作用 |
|---|---|
| `scripts/tls_relay.py` | TLS 终结 + 转发（8443 → 8000） |
| `scripts/gen_tls_ca.sh` | **私有 CA** + 签发服务端证书（本文档用的就是它） |
| `scripts/gen_tls_cert.sh` | 另一条路：直接自签一张叶子证书（要点「继续前往」） |
| `scripts/verify_tls_scheme.py` | 回归：scheme 必须传给后端 + 请求体不能被搞坏 |
| `/etc/systemd/system/wdnwebui-tls-relay.service` | relay 的 systemd 单元（开机自启） |

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
     https://172.16.9.65:8443/
#   期望：200 tls_verify=0
```

浏览器打开 `https://172.16.9.65:8443/` —— **地址栏不该再出现「不是私密连接」**，
然后进「流式语音识别」→ 选「浏览器麦克风」→ 点「开始实时识别」。

## 板子上已经装好的部分

本机（板子自己）已经配好，**不用重复做**：

| 位置 | 内容 |
|---|---|
| `/usr/local/share/ca-certificates/wdnwebui-local-ca.crt` | CA 根（`update-ca-certificates` 已跑过）→ `curl` / Python / Firefox 都认 |
| `~/.pki/nssdb` | CA 根（NSS）→ 本机的 Chromium 认 |
| `wdnwebui-tls-relay.service` | relay（`enabled`，开机自启） |

## 续签 / 换 IP

叶子证书换了**不用**再动客户端 —— 客户端信任的是根：

```bash
# 重签（保留 CA）
bash scripts/gen_tls_ca.sh 172.16.9.65 --force-leaf
sudo systemctl restart wdnwebui-tls-relay.service
```

换 IP 时把新 IP 加进 SAN 再重签：

```bash
bash scripts/gen_tls_ca.sh <新IP> --force-leaf
```

## 排查

| 症状 | 多半是 |
|---|---|
| 浏览器还报「不是私密连接」 | CA 没装进**这个浏览器**用的那个库（Chrome 读 NSS，不读系统库） |
| `ERR_CERT_AUTHORITY_INVALID` | 同上；或证书过期（`openssl x509 -enddate` 看） |
| `ERR_CERT_COMMON_NAME_INVALID` | 访问地址不在 SAN 里 → 用 `https://172.16.9.65:8443`（SAN 含 IP + `debian` + `localhost`） |
| 页面能开但 `Failed to fetch` | 后端没收到 `X-Forwarded-Proto` → 跑 `python -m scripts.verify_tls_scheme` 查 |
| 8443 连不上 | `systemctl status wdnwebui-tls-relay`；证书不在 → `bash scripts/gen_tls_ca.sh` |
| 麦克风还是打不开 | 看浏览器控制台；**必须**是 `https://` 或 `localhost` |

```bash
systemctl status wdnwebui-tls-relay.service
sudo journalctl -u wdnwebui-tls-relay.service -n 50
sudo ss -ltnp | grep 8443
```
