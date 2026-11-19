# huggingface.co 访问：DNS 污染 + SNI 阻断的诊断与修复

> 日期：2026-11-17 · 板卡：RK3588（`172.16.9.65`）· 网络：境内出口
>
> **一句话**：本板访问 `huggingface.co` 有**两层**封锁 —— DNS 应答被污染，
> 且 TLS ClientHello 里的 SNI 被 RST。**只修 DNS 是不够的**，
> 必须再叠一层 SNI 绕行代理。本文档是这两层的实测记录与用法。
>
> **本文范围**：网络诊断与代理本身。**「怎么把模型发布到 HF」是另一件事**
> —— 元数据刷新、两条推送路线、复核方式都在
> [`docs/MODEL_PUBLISH_GUIDE.md`](MODEL_PUBLISH_GUIDE.md)。

---

## 1. 症状

```bash
$ curl https://huggingface.co/
curl: (35) SSL connect error ...        # 改完 hosts 之后
$ curl https://huggingface.co/         # 改之前
curl: (7) Failed to connect ... Connection refused
```

板子上的**下载**侧一直正常 —— 因为 `app/core/compute/hf_transfer.py` 默认
就�� `HF_ENDPOINT = https://hf-mirror.com`（镜像站可达）。**只有上传 /
建仓这条路是断的**。

## 2. 诊断：两层封锁

### 2.1 第一层 —— DNS 污染（按域名定向，不是全局）

同一个问题域，不同上游解析器给出的假 IP **各不相同**，但全部落在
Facebook / Twitter / Dropbox 段：

| 上游 | `huggingface.co` 的应答 |
|---|---|
| 8.8.8.8 | 162.125.32.15（Dropbox 段） |
| 1.1.1.1 | 157.240.0.18（Facebook） |
| 9.9.9.9 | 185.60.216.50（Facebook） |
| 223.5.5.5 | 199.16.158.190（Twitter） |
| 119.29.29.29 | 157.240.7.8（Facebook） |
| 114.114.114.114 | 66.220.149.18（Facebook） |

AAAA 一律是 `2a03:2880:...:face:b00c:...`（Facebook）。

**对照实验**（同一个解析器、同一次查询）：

```
www.baidu.com    -> 157.148.69.186   ✅ 真
github.com       -> 20.247.166.64    ✅ 真
hf-mirror.com    -> 104.250.143.18   ✅ 真
www.google.com   -> 104.244.42.197   ❌ 假（Twitter 段）
huggingface.co   -> 104.244.46.21    ❌ 假（Twitter 段）
```

⇒ 污染**只针对被封锁的域名**，不是本板 DNS 配置坏了。**换解析器无用**
（连 DoH 也一样：alidns / doh.pub / 223.5.5.5 的 DoH 同样返回假地址）。

### 2.2 第二层 —— SNI 阻断（这才是关键）

用境外 DoH 拿到**真实 IP**（`dns.digitale-gesellschaft.ch`，见 §4）后直连：

```
$ curl --resolve huggingface.co:443:52.222.136.89 https://huggingface.co/
curl: (35) SSL connect error          # 仍然是 TLS 失败
```

**决定性对照** —— 同一批 IP，只换 SNI：

```
$ openssl s_client -connect 104.18.32.47:443 -servername cloudflare.com
subject=CN = cloudflare.com                          ✅ 握手成功
$ openssl s_client -connect 104.18.32.47:443 -servername huggingface.co
（无证书返回 —— ClientHello 被 RST）                  ❌ 握手被掐
```

⇒ **GFW 按 SNI 匹配并掐断连接**。`/etc/hosts` 只能修好解析，
修不好握手。

### 2.3 被掐的到底有哪几个主机

逐个探测后，**只有 `huggingface.co` 本体**被 SNI 封锁：

| 主机 | 正常 SNI | 备注 |
|---|---|---|
| `huggingface.co` | ❌ 被掐 | Hub API + 网页 |
| `transfer.xethub.hf.co` | ✅ 通 | Xet 数据面（`hf_xet` 已装 ⇒ 上传走这里） |
| `cas-bridge.xethub.hf.co` | ✅ 通 | Xet CAS |
| `cdn-lfs-us-1.hf.co` | ✅ 通 | LFS CDN |
| `cdn-lfs-eu-1.hf.co` | ✅ 通 | LFS CDN |

`cas-server.huggingface.co` / `cdn-lfs.huggingface.co` 已**不再有 DNS 记录**
（Hub 改用 Xet 了），不用管。

⇒ **只要打通 `huggingface.co` 一个主机，整条上传链路就通了** ——
数据面本来就通。

## 3. 修复

### 3.1 第一层：把真 IP 钉进 `/etc/hosts`

```bash
sudo tee -a /etc/hosts > /dev/null <<'EOF'
52.222.136.89	huggingface.co
52.222.136.92	huggingface.co
52.222.136.117	huggingface.co
52.222.136.38	huggingface.co
2600:9000:2204:1c00:17:b174:6d00:93a1	huggingface.co
EOF
```

**AAAA 也必须钉**：本板没有全局 IPv6 路由（只有 `fe80::` link-local），
但 glibc 仍会解析出 IPv6 —— 不钉的话 `getaddrinfo` 会把假 AAAA 一并返回。

> 板子没有 IPv6 出口，IPv4 优先级本来就对（glibc 默认 RFC 6724 把
> IPv4-mapped 排在 IPv6 前面），**不需要改 `/etc/gai.conf`**。

改完 `getaddrinfo('huggingface.co', 443)` 首选就是 `52.222.136.89` ✅。

### 3.2 第二层：SNI 绕行代理

**原理**：GFW 的 SNI 匹配串里**没有尾点**。把 ClientHello 的 SNI 发成
`huggingface.co.`（带尾点）→ 握手放行；而 HTTP 的 `Host` 头保持
`huggingface.co`（不带尾点）→ 服务端照常路由，证书也仍是
`CN=huggingface.co` ⇒ **证书校验依然成立，没有降级成「关校验」**。

实现见 `tools/hf_sni_proxy.py`（纯 stdlib + `curl`，无新依赖）：

```bash
# 起代理（已装成 systemd 单元，开机自启）
sudo systemctl start hf-sni-proxy.service

# 让 hf CLI 走它
export HF_ENDPOINT=http://127.0.0.1:8787
hf auth whoami
hf upload wardner/Qwen3-ASR /userdata/models/Qwen3-ASR
```

代理只做 **TCP 层的 TLS 换腿**：请求体/响应体**按字节原样转发**
（chunked 帧保持不变），所以 1 GB 级权重上传**不会被缓冲进内存**。

### 3.3 代理实现里踩到的三个坑

1. **`makefile()` 会吞字节** —— `BufferedReader` 预读的头部后面那截
   请求体，在 `makefile` 对象被关掉时就丢了。必须**读头部和读正文
   共用同一个缓冲流**。
2. **`Expect: 100-continue` 会互等** —— 客户端发完头就等 100，代理若
   把它透传给上游，就是「代理等请求体 / 客户端等 100」的双向死等。
   解法：**剥掉 `Expect`**，上游看不到它就不会回中间态。
3. **`100 Continue` 是临时响应** —— 当成最终响应转发，客户端会以为
   收完了（`curl` 直接报 `http=100` 就停）。必须转发后**继续读**真正的响应。

另：上游可能**边收边拒**（路由不存在时直接回 404 并关连接，而请求体还在发）。
此时 `send()` 抛 `SSLEOFError` —— 不能中断，要继续把上游已经给出的
响应转给客户端，否则客户端只看到 `http=000`，丢掉真实状态码。

### 3.4 第三层：请求体里的关键词也会被掐（2026-11-17 补）

改 README 时撞上的：**把含 `huggingface.co` 字样的文本 POST 上去，会被间歇性
注入 RST**（实测 3 次里挂 1 次）。

```bash
# 同一个请求连发 3 次
第1次: http=200
第2次: http=000  t=30.001582s      ← 被掐
第3次: http=200
```

客户端看到的是：

```
requests.exceptions.ConnectionError: (ProtocolError('Connection aborted.',
RemoteDisconnected('Remote end closed connection without response')))
```

**为什么只有某些请求会中招**：

| 请求 | 请求体形态 | 受不受影响 |
|---|---|---|
| `POST /api/validate-yaml` | README **原文**（明文域名） | ❌ 会挂 |
| `POST /api/models/.../commit/main` | 文件内容 **base64** | ✅ 不受影响 |

⇒ 注入的是 RST，**和真实响应赛跑** —— 客户端先收到 RST 就报「连接被掐」，
先收到真响应就正常。**重试即可**（单请求重试 6 次 × 整轮重试 4 轮，实测必过）。

`tools/publish_hf_model.py` 已把这条重试固化进去
（`_install_retry()` + `upload_folder_with_retry()`）。

> 顺带说明：这个坑**只影响带 README 的上传**。纯权重上传（commit 载荷里
> 全是 base64）从来没事 —— 所以之前 1.1 GB 整目录上传一次就过了。


### 3.5 huggingface_hub 在**自定义端点**下的两个坑

用的是 `HF_ENDPOINT=http://127.0.0.1:8787`（不是官方 huggingface.co），
SDK 有两处没考虑自定义端点：

**坑 ① `CommitInfo` 构造 `RepoUrl` 时不传 endpoint**

```python
# huggingface_hub/hf_api.py
class CommitInfo:
    def __post_init__(self):
        self.repo_url = RepoUrl(self.commit_url.split("/commit/")[0])
                                                   ↑ 没传 endpoint

class RepoUrl:
    def __init__(self, url, endpoint=None):
        self.endpoint = endpoint or constants.ENDPOINT   # ← 默认 huggingface.co
```

⇒ 端点是 `http://127.0.0.1:8787` 时，`RepoUrl` 却拿**默认的 huggingface.co**
去判定「这是不是 HF URL」→ 判定失败 →

```
ValueError: Unable to retrieve user and repo ID from the passed HF ID:
            http://127.0.0.1:8787/wardner/Qwen3-ASR
```

**修法**（`tools/publish_hf_model.py::_patch_repo_url_endpoint`）：给
`RepoUrl.__new__/__init__` 打个补丁，让它默认用我们自己的 endpoint。

**坑 ② `validate-yaml` 把 README 原文裸传** —— 见 §3.4。

> **为什么坑 ① 是间歇性的**：它取决于 `create_commit` 走哪个返回分支
> （「无改动提前返回」那条分支也会构造 `CommitInfo`）。实测同一条命令
> 有时过、有时炸 —— 所以**别把偶发失败当成偶发网络抖动**，要看栈。

## 4. 真实 IP 从哪来

**境内解析器一律不可信**（连 DoH 也返回假答案）。本板可达且返回真答案的
境外 DoH：

```bash
# dns.digitale-gesellschaft.ch —— 瑞士，要求 HTTP/2（RFC 8484 §5.2）
curl -s --http2 -X POST https://dns.digitale-gesellschaft.ch/dns-query \
     -H 'content-type: application/dns-message' \
     --data-binary @query.bin -o resp.bin
```

代理内置了这份地址，并每 6 小时用它刷新一次（`--refresh-hours`），
HF 换 IP 也不会失联。`tools/hf_sni_proxy.py --print-hosts` 可以直接
打印要写进 `/etc/hosts` 的行。

> 其它境外 DoH（1.1.1.1 / 8.8.8.8 / 9.9.9.9 / Quad9 / OpenDNS /
> AdGuard / Mullvad / NextDNS）在本板**全部被封**（IP 段被阻或 SNI 被阻）。

## 5. 验证记录

| 检查项 | 结果 |
|---|---|
| `getaddrinfo('huggingface.co')` 首选 | `52.222.136.89` ✅ |
| `GET /api/models?author=wardner` | 200，返回真实仓列表 ✅ |
| `GET /api/whoami-v2`（无 token） | 401 `{"error":"Invalid username or password."}` ✅ |
| `GET /` | 200，175 898 B ✅ |
| `hf download wardner/Qwen3-TTS --include README.md` | 下载成功，3675 B ✅ |
| `hf upload …`（无 token） | 401，**请求确实送达 Hub 鉴权层** ✅ |
| 45 MB 下载带宽 | **9.4 MB/s**（4.8 s）⇒ 1.1 GB 上传约 2 分钟 ✅ |
| 响应头指纹 | `X-Powered-By: huggingface-moon`、`X-Request-Id: Root=1-…`、`Via: …cloudfront.net` ✅ 确为真 Hub |

## 6. 代理已被真实验证过（2026-11-17）

代理搭起来之后，**`wardner/Qwen3-ASR` 就是通过它发布成功的**（公开仓，
`license: apache-2.0`，15 个文件 = 6 个 LFS + 9 个普通 blob，
`.gitattributes` 被服务端采纳）—— 这既是发布记录，也是「代理确实打通了
`huggingface.co`」的端到端证据（§5 的响应头指纹
`X-Powered-By: huggingface-moon`、`Via: …cloudfront.net` 确为真 Hub）。

**完整的发布流程不在本文** —— 见
[`docs/MODEL_PUBLISH_GUIDE.md`](MODEL_PUBLISH_GUIDE.md)。

## 7. 相关文档

* [`docs/MODEL_PUBLISH_GUIDE.md`](MODEL_PUBLISH_GUIDE.md) ——
  维护者：怎么把模型发布到 HuggingFace
* [`docs/MODEL_INSTALL_GUIDE.md`](MODEL_INSTALL_GUIDE.md) ——
  用户：clone 下来怎么装
* `tools/hf_sni_proxy.py` —— 本文描述的 SNI 绕行代理
* `tools/publish_hf_model.py` —— 建仓 + 上传 + 前后校验一条龙
