# 网页打不开 —— 首屏交付性能排查与修复

> 起因：用户报「网页打不开」，并贴了服务端的访问日志
> （`GET / HTTP/1.1" 200 OK`）。日志里明明是 200，页面却打不开。

## 一句话结论

服务端**毫秒级响应、页面渲染完全正常**，问题出在**首屏交付的字节量**：
一个首页要传 **4.29 MB**，其中 **3.2 MB 是应用网格的预览视频**。在慢链路
客户端上，这就是 76 秒。

三个放大器，都在服务端：

| # | 放大器 | 修复前 | 修复后 |
|---|---|---|---|
| 1 | **完全没有压缩** | HTML 116 KB / CSS 140 KB / JS 64 KB … 全明文 | gzip → 约 1/4 |
| 2 | **静态资源一律 `no-cache`** | 每次刷新重下 42 个文件（各带一次 ETag 回校验） | 带 `?v=` 的 → `immutable` 长期缓存 |
| 3 | **卡片预览视频 `autoplay`** | 首屏整段下载 9 个 mp4 = **3.2 MB** | 首屏只显示 16 KB 的首帧海报，悬停才加载视频 |

实测（同一台板子、同一浏览器、同一链路）：

| | 修复前 | 修复后 | 变化 |
|---|---|---|---|
| 冷加载真实过线字节 | 4.29 MB | **784 KB** | **−82%** |
| 热加载（刷新）真实过线字节 | 4.29 MB | **190 KB** | **−96%** |
| 热加载时服务端实收静态请求 | 42 个 | **0 个** | 全部命中浏览器缓存 |

## 排查过程（可复现的证据链）

### 1. 先排除服务端

| 检查项 | 结果 |
|---|---|
| `wdnwebui.service` / `wdnwebui-tls-relay.service` | 都 active |
| `curl http://127.0.0.1:8000/` | 200，116691 B，46 ms |
| `curl https://127.0.0.1:8443/`（`--cacert`） | 200，证书校验通过 |
| LAN 侧 `http://172.16.9.65:8000/` / `https://…:8443/` | 200，46~68 ms |
| 16 并发请求 × 8 个路径（明文 / TLS 各一轮） | 明文 90 ms，TLS 193 ms |
| 无头 Chromium 三个入口 | 全部 200，**页面渲染正常**（9 张卡片、侧边栏、向导首屏），无 pageerror |

**结论**：服务端和页面本身都是健康的，慢在**客户端链路**。

### 2. 定位到客户端

* 用户浏览器的请求在 uvicorn 日志里来源全是 `127.0.0.1` → 经**本机 TLS
  中继**（`scripts/tls_relay.py`，pid 1875）转发进来，不是直连。
* `ss -tn` 抓到客户端 **`172.16.9.18`**（同网段另一台机器），此刻正挂着
  7 条 `:8000` 直连 + 8 条 `:8443`。
* 用户那次加载的时间线：**11:30:30 HTML → 11:31:44 全部 API + WS 全通 =
  76 秒**，期间用户重载了 3 次。
* `ping` 不可用（容器缺 `cap_net_raw`），但 socket 计时器显示 RTO 正常
  （0.2 s）——**不是丢包重传**，是纯粹的带宽/往返问题。

### 3. 抓出真正的字节大头

无头 Chromium 量首屏响应体：

```
586446 B  /static/apps/video-depth/preview.mp4
434046 B  /static/apps/video-target-detection/preview.mp4
430447 B  /static/apps/video-target-localization/preview.mp4
...
总 116 个响应 / 4,399,677 B
mp4: 9 个 / 2,956,197 B (67.2%)     ← 七个成三的元凶
```

**9 个预览视频占了首屏字节的 67%。**

## 三个放大器，逐个拆

### 放大器 1：没有压缩

`app/main.py` 之前**没有任何压缩中间件**。首屏要传几百 KB 明文。

**修法**：`app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=6)`。

用 Starlette 自带的而不是自己写，是因为它**显式跳过 206 Partial Content**
（`partial_response` 判断）—— 视频的 Range 请求压了就坏；它默认排除
`video/*` / `audio/*` / `image/*` / `text/event-stream`；而且
`scope["type"] != "http"` 时原样放行 —— **WebSocket 不受影响**。

`compresslevel` 用 6 而不是默认的 9：RK3588 上 level 9 编 116 KB HTML 的
CPU 代价明显更大，体积差距只有百分之几。

实测：

```
HTML      117081 B → 23333 B (20%)
core.css  140628 B → 30357 B (22%)
core.js    64529 B → 21097 B (33%)
```

### 放大器 2：静态资源一律 `no-cache`

`/static/core` 与 `/static/apps/<id>` 由 `_NoCacheStaticFiles` 强制
`Cache-Control: no-cache`（为了"改了前端不用硬刷新"）。但 `no-cache`
**不是** `no-store`：浏览器仍会缓存，只是每次都带 ETag 回校验 —— 于是
**每次刷新都要跑满 42 个往返**。

版本戳早就有了（`html_assembly.py::_ASSET_RE` 给 `/static/core|system/**`
加 `?v=<mtime>`），但**卡片资源不在其中**。

**修法**（`app/main.py::_cache_control`）：

* 带 `?v=` → `public, max-age=31536000, immutable`
  —— URL 本身含版本，内容变 → mtime 变 → URL 变，既能长期缓存又不会陈旧；
* 不带 `?v=` → 维持 `no-cache`。

卡片资源也版本戳化（`html_assembly.py::_card_asset_stamp`）。**关键点**：
版本戳直接加在 **manifest JSON 的 `frontend.js` / `frontend.css`** 里，
于是 `framework.js` 的 `s.src = url` **一行都不用改**就吃到了 immutable
缓存 —— 改动边界最小，也不碰 `_loadedApps` / `_wizardMounted` 那套分离语义。

### 放大器 3：预览视频 `autoplay`（最大的一条）

`framework.js` 渲染应用网格时给每张卡挂：

```html
<video class="app-card-video" autoplay muted playsinline preload="metadata" loop src="...">
```

`autoplay` 会让浏览器**整段下载**每个视频。而 `preload="metadata"` 在
`autoplay` 面前**不起作用**。

浏览器侧绕不开 —— 只要 `src` 在，Chrome 就发**开区间** Range
（`bytes 0-`），服务端照直把整个文件发过去。实测四种组合的过线字节：

| 属性组合 | 过线字节 |
|---|---|
| `autoplay` + `preload="metadata"`（原样） | **3.2 MB** |
| 无 `autoplay` + `preload="metadata"` | 3.4 MB |
| `preload="none"`（仍带 `src`） | 2.5 MB |
| **完全不设 `src`** | **0 B** |

> 判据是**服务端 loopback 计数器**（`/proc/net/dev`），不是 Playwright 的
> `response.body()` —— 后者在响应被客户端中止时拿不到body，量不准。

**修法**：

1. **首屏不设 `src`**：`<video>` 只带 `poster`（服务端抽的首帧 JPEG），
   鼠标悬停到那张卡才补 `src` 并播放。
2. **海报由服务端现抽**：`GET /preview/<card_id>.jpg?v=<mtime>`
   （`app/core/preview_poster.py`）。用 **PyAV** 解第一帧 + **cv2** 缩到
   480 宽编码 JPEG，~16 KB/张，9 张合计 ~144 KB —— 比 3.2 MB 少 23 倍。
   * 解码走 PyAV 是**音频同款例外**：`wdnav` 只有 MPP / RGA / V4L2 的
     **视频**能力，没有解码器（与 `app/core/audio/decode.py` 同理）。
   * `av` / `cv2` **惰性导入**：重 native 库在模块 import 期加载会在 pytest
     收集期引发段错误。
3. **视觉不打折**：海报就是视频的第一帧，和原来动态预览的画面**肉眼一致**
   （只是不動了）；悬停即恢复播放。

## 顺带修掉的

* **`qwen3-asr/preview.mp4` 不是 faststart**（moov 在文件尾）——
  `scripts/mp4_faststart.py --fix` 无损改容器（73 帧逐帧哈希一致）。
  这也是**既有状态**，与本次改动无关；修了之后 `verify_app_panel_preview.py`
  的 faststart 断言才过，悬停起播也更快。

## 回归

新增 `scripts/verify_static_delivery.py`（**53 / 53**）：

* A. gzip 生效且**解压后与明文逐字节相同**；
* A2. 首屏三个大文件压缩后不到原来的一半；
* B. 视频不带 `Content-Encoding`；**206 也不带**，且 `Content-Range` 与
  实际字节跟源文件区间一致；
* C. 带 `?v=` → `immutable`；不带 → `no-cache`；
* D. 卡片 CSS link 与 manifest 的 `frontend.js` 都带 `?v=`；
* F. 海报端点 200 / `image/jpeg` / 合法 JPEG / 比视频小 5 倍以上；
* F2. **首屏没有任何 `<video>` 带 `src`**，也**没发出任何 `.mp4` 请求**；
* E. WebSocket 握手仍 101，且无 `Content-Encoding`。

受影响的两处既有断言已同步更新（`preview_video` 多了版本戳、视频改成懒加载）：

| 套件 | 结果 |
|---|---|
| `verify_static_delivery.py`（新增） | **53 / 0** |
| `verify_asr_wizard.py` | **89 / 0**（W9 拆成"首屏不加载"+"悬停后能解码"两条） |
| `verify_app_panel_preview.py` | ✅ 全部通过 |
| `verify_asr_live.py` | 41 / 0 |
| `verify_asr_long_audio.py` | 7 / 0 |
| `verify_model_download_card.py` | 20 / 0 |
| `verify_config_userdata_fallback.py` | 21 / 0 |
| `verify_capability_groups` / `verify_model_packaging` / `verify_tls_scheme` | ✅ 全部通过 |

## 教训

1. **"服务端返回 200" 不等于"用户看到页面"。** 这次日志里每一条都是 200，
   但用户等了 76 秒。**要看的是字节数，不是状态码。**
2. **量字节要用服务端计数，不要信客户端 SDK 的 `body()`。** 响应被中止时
   `body()` 拿不到数据，而那恰恰是关键场景。
3. **`autoplay` 会覆盖 `preload` 提示。** 想省流量不能只改 `preload` ——
   要**真的不设 `src`**。
4. **浏览器侧的"少下载"在开区间 Range 下不存在。** `Range: bytes=0-` 的
   语义就是"到文件尾"，服务端照直发完整个文件；客户端中止与否取决于
   socket 缓冲，不取决于你写了什么 `preload`。
5. **版本戳要在"资源被引用的地方"加。** 加在 manifest JSON 里，
   下游 `s.src = url` 就自动受益 —— 比改注入逻辑更小、更稳。

## 遗留

* 用户链路的**绝对带宽**仍然是瓶颈（冷加载 784 KB 也要几秒）。上面的
  修复把服务端能做的都做了，剩下的要看客户端网络。
* 卡片 `wizard.json` 仍是 `no-cache`（每次回校验）—— 它是**运行时配置**，
  必须保证新鲜，1 个请求不值得为它牺牲正确性。
* 应用网格在**触屏设备**上没有 hover，预览视频不会自动播放（只显示海报）。
  这是可接受的降级（点了就进功能页），但如果要做触屏优化需要另设方案。
