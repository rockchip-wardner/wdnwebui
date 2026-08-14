# AGENTS.md

Compact guide for OpenCode sessions in this repo.

## What this repo is

Python web service (`pyrkaiweb`) exposing RK-platform hardware / model SDKs
(`wdnav`, `wdnai`) over HTTP via FastAPI. Brand-new repo (no commits yet at
creation); everything below is verified against the current tree.

## Stack at a glance

- **Web framework**: FastAPI + Uvicorn (`run.py` boots `app.main:app` on
  `0.0.0.0:8000` with `reload=True`).
- **Internal SDKs** (installed editable from private SSH git, **not** PyPI):
  - `wdnav` — Rockchip MPP / RGA2 / V4L2 video bindings.
    Native libs (`librockchip_mpp`, `librga`) must be installed on the host;
    `import wdnav` only — `import wdnav.video` is what touches the hardware.
  - `wdnai` — RK-chip LLM / VLM SDK (transformers + opencv-python).
  See `.venv/src/wdnav/AGENTS.md` and `.venv/src/wdnai/pyproject.toml` for the
  SDK-side gotchas (they're external code, not in this repo, but the
  constraints propagate here).

## 设计原则

1. 必须通过wdnai的LLMSession调用rknn，不得直接调用
2. 必须通过wdnav调用mpp和rga，做编解码和图片缩放、格式转换等操作，不得直接调用或者通过其他库进行软编解和格式转换、resize等操作。
3. 必须通过wdndds做发现和注册服务。
4. **LLMSession 必须从 `ComputeRegistry` 取，禁止各业务模块自建**。视频检测
   / 语音生成 / 视频监控等服务只能通过
   `get_registry().get_session("<capability>")` 拿到 session；
   找不到对应 capability 就抛 `RuntimeError("功能未启用: <capability>")`，
   由调用方决定如何向上层报告（视频监控选择静默 error 返回，不阻塞 16 路
   解码；视频检测 / 语音生成选择 raise，由 HTTP 层 502 透传给前端）。
实例集合在 算力配置 页配置，存到 `config.json["compute"]["instances"]`，
    `run.py` 启动时**逐个 spawn 独立 compute_worker 子进程**（不阻塞 UI）。
5. **所有演示页（音视频描述 / 视频目标检测 / 视频内容检索 / 语音生成）
   离开时必须主动关闭后台运行**。`app/static/js/app.js` 的 `DemoGuard`
   统一负责：
   * in-app 导航（点击 sidebar）：若当前页后台在跑（解码
     `state ∈ {opening, playing, paused, eof}` 或 detector `enabled=true`，
     TTS `busy=true`），先弹 `confirm()`，用户确认后 await 后端
     `/close` + `/prompt/stop`（或 `/tts/stop`）再放行；用户取消则
    `preventDefault()` 阻止跳转。
   * `beforeunload`（关 tab / 刷新 / 关闭浏览器）：同步触发原生警告
     对话框，并通过 `navigator.sendBeacon` 或 `fetch({keepalive:true})`
     异步发停请求，best-effort 兜底。
   * `hashchange`（浏览器前进/后退）：静默 sendBeacon 兜底停掉所有
     在跑演示页的后端，避免路径绕过 click handler 时残留。
   * 判定由各页模块主动 push：`setVdMeta` / `updateVdDetectorStatus` /
     `setVtdMeta` / `updateVtdDetectorStatus` / `TTS.render` 在更新
     DOM 的同时 `DemoGuard.report(<page>, { session, detector })`。
   **浏览器限制**：beforeunload 期间 JS 不能保证请求送达（用户强杀、
   浏览器崩溃、某些移动浏览器会丢弃 beacon）；AGENTS 不做超出此限制
   的承诺。若可靠性要求更高，需要服务端按 session TTL / 心跳
   自动回收 —— 不在本期范围。

## 算力注册中心 — per-model-subprocess RPC (v0.4)

v0.4 重大重构：每个 (npu_index, model_id) 实例现在跑在**独立
compute_worker 子进程**里，跨进程通过 `wdndds` cshim RPC
（Request/Reply over RELIABLE Pub-Sub）通信。架构改造的关键点：

* **`wdndds 0.3` = C++ cshim + ctypes Python wrapper**。
  抛弃了 v0.2 的 bundled python-fastdds SWIG 绑定；
  C++ 源码在 `wdndds/cshim_src/`，在 `pip install wdndds` 时通过
  `wdndds/_build.py` 的 setuptools `build_py` 子类编译成
  `libwdndds_cshim.so`。运行时链接系统 `libfastrtps2.9` /
  `libfastcdr1`（apt install libfastrtps-dev libfastcdr-dev）。
* **每个 model = 1 个 compute_worker 进程**。`ComputeRegistry.start
`     ` 在主进程 spawn 一个 `python app/services/compute_worker.py
       --model <id> --npu <k>` 子进程（**子进程默认不加载模型** —
       等首次 RPC 到达时再 lazy `LLMSession(model=spec, npu=k)`）。
* **主进程持 RpcClient**：`get_registry().get_session(cap)` 返回
  `app/services/rpc_session_proxy.RpcSessionProxy`，内部转发
  `reset()` / `respond()` 到 worker 的 `wdndds/rpc/<model_id>` service
  topic，再从 `<service_name>/reply` shared reply topic 拿回 JSON reply。
  业务侧（detector / tts / vfa / text_chat）调用方式**不变**
  —— `RpcSessionProxy` 保持 `wdnai.LLMSession` 的 `.reset()` /
  `await .respond(prompt, attachment, ...)` 表面。
* **服务端 lock**：worker 进程内一个 cshim pump 线程串行消费请求，
  处理完 reply 完毕后才接下一个；多个 worker 并行是允许的
  （不同 NPU / 不同 model 实例各跑各的）。跨进程互斥锁在 v0.4 不需要
  —— 主进程侧 `RpcSessionProxy.respond` 内部 1 个 ThreadPoolExecutor
  串行化同一 instance 的并发请求。
* **`boot_compute_services` 在 boot 默认开启**。`app/main.py`
  `create_app` 调用一次，遍历 `config.compute.services`
  （典型 `["qwen3-vl-4b", "qwen3-tts", "qwen3-8b"]`）逐条做
  probe → adopt-or-spawn。worker 起来后**只**注册 DDS endpoint，
  不 build LLMSession；npu_index 由用户在算力配置页选 NPU 后通过
  RPC `{"action":"load","npu_index":N}` 传入。
  `_probe_instance` daemon 线程周期性 RPC `info` 同步 worker
  状态（`not_loaded` / `loading` / `running` / `error`）。
* **`get_session_call_lock()` 留作 no-op 兼容旧调用点**
  （仅 `video_file_analysis.py` 还在 import）。

### 已知约束 / 待完善

* **本机 FastDDS 2.9 bug**：在 Debian bookworm 板上的系统
  libfastrtps2.9 包 `DataWriter::enable()` 会 SIGSEGV
  （在 `get_payload_pool()` 里），所以当前主机跨进程 DDS 收发都
  跑不通。这是系统库的问题，**不是 wdndds 架构的问题**——
  改用 SRIO / 共享内存 / Unix socket 等替代传输时，进程间通信照常
  跑得通。架构在协议层是干净的：每个 worker 一个 service，每个
  service 一个 request topic + 一个 shared reply topic。
* **流式 vs 一次性**：`RpcClient.call` 首版只支持一次性 reply
  (`{"ok": True, "data": {...}}`)。API 层面 `call_streaming`
  已经预留签名但未实现；如果加回来需要 cshim 多一条 streaming
  topic 通路。
* **多 NPU 跨子组件**：当前 `ModelInstance.npu_index: int` 单值；
  后续 vision@NPU0 + llm@NPU1 这种「一个 model 跨多 NPU」配置要
  等 wdnai SDK 改 spec + pyrkaiweb 解析 `npu_assignments: dict[str, int]`
  —— 当前每个 model 一个实例，所以 service_name = `wdndds/rpc
`     <model_id>`（**不带 npu 后缀**），单 model 多 npu 也在子进程
  内部决策。
* **同 model 不允许重复实例**：`compute_registry.start_one` 校验
  `(npu_index, model_id)` 不重复；UI  已经在加「instance_name」
  之前按 (npu, model) 唯一。

## TL;DR commands

```bash
# activate (do NOT use system pip — Debian PEP 668 blocks it)
source .venv/bin/activate

# run
python run.py                  # 0.0.0.0:8000, reload on

# sanity
curl http://127.0.0.1:8000/health
```

Endpoints wired so far: `GET /` (version), `GET /health`.

## One-time setup that an agent will get wrong

Both internal SDKs live behind SSH on `git@10.10.10.10`. They are **not** in
`requirements.txt` (intentional — the host SSH key + network decide whether
the install succeeds). Run them by hand after cloning:

```bash
.venv/bin/pip install -e "git+ssh://git@10.10.10.10/wardner/wdnav.git#egg=wdnav"
.venv/bin/pip install -e "git+ssh://git@10.10.10.10/wardner/wdnai.git#egg=wdnai"
```

If `git clone` fails, the host either lacks SSH access to `10.10.10.10` or
the deploy key isn't loaded. Do **not** fall back to a public mirror — these
SDKs aren't published.

## Layout

```
pyrkaiweb/
├── run.py                # entry point — uvicorn.run("app.main:app", ...)
├── app/
│   ├── __init__.py       # __version__ = "0.3.0"
│   ├── main.py           # create_app() factory + module-level `app`
│   ├── api/
│   │   ├── __init__.py   # imports submodules so include_router sees them
│   │   ├── power.py
│   │   ├── surveillance.py     # 16-channel REST + WS endpoints
│   │   ├── system_logs.py
│   │   ├── system_monitor.py   # CPU/mem/NPU + sysfs thermal zones → 系统信息 page
│   │   └── video_detection.py   # REST + WebSocket frame stream + detector
│   ├── services/
│   │   ├── surveillance.py      # 16 channels sharing ONE LLMSession via a worker thread
│   │   ├── video_session.py     # VideoCapture + decode thread + JPEG queue
│   │   ├── detector.py          # wdnai VLM wrapper + worker thread
│   │   └── uploads/             # local-file upload staging (gitignored)
│   └── static/
│       ├── index.html           # single-page UI
│       ├── css/style.css
│       └── js/app.js            # WebSocket + Canvas + event UI
├── requirements.txt      # web deps + opencv-python-headless + python-multipart
├── .gitignore            # excludes .venv/, __pycache__/, .env, uploads/, etc.
└── .venv/                # editable installs of wdnav, wdnai live under .venv/src/
```

`app/__init__.py` owns the single version string; bump it there when the
service version changes.

## Video detection page

The `#page-video-detection` section is wired to:

- **Backend** (`app/api/video_detection.py`):
  - `POST /api/video-detection/upload` — multipart file upload; saves to
    `app/services/uploads/<uuid><ext>` and returns `{upload_id, path, …}`.
  - `POST /api/video-detection/open` `{source: str}` — opens via
    `wdnav.VideoCapture`, starts decode thread, returns snapshot.
  - `POST /api/video-detection/control` `{action: "play"|"pause"|"stop"}`.
  - `POST /api/video-detection/close`.
  - `GET  /api/video-detection` — current snapshot.
  - `GET  /api/video-detection/fs?path=…` — server-side directory
    listing (used by the in-page file picker modal). Constrained to
    `VD_FS_ROOT` (env var, default `/`) and skips `/proc`, `/sys`,
    `/dev`, `/run`, `/snap`, `/usr/share/{doc,locale,man}`, etc.
  - `POST /api/video-detection/prompt` `{prompt, every_n}` — start VLM
    detection with the user's prompt; `every_n` controls how often
    (in frames) the worker feeds a frame to the model.
  - `POST /api/video-detection/prompt/stop` — stop detection (worker
    thread exits, session stays loaded for next start).
  - `GET  /api/video-detection/events` — snapshot + history list + latest.
  - `POST /api/video-detection/events/clear` — clear history.
  - `WS   /api/video-detection/ws` — server-pushes binary JPEG frames;
    interleaves JSON `{"type":"status", detector:{…}}` frames at <=1 Hz;
    emits `{"type":"eof"}` once when a file source reaches end;
    emits `{"type":"event", id, frame_pos, ts, prompt, text}` for each
    VLM detection result.
- **Service** (`app/services/video_session.py`):
  - One `VideoSession` singleton, one `wdnav.VideoCapture`, one decode
    `Thread`. Latest-frame-only queue (`maxsize=1`) — drops oldest on
    overflow to keep latency low.
  - Frame sinks: registered callbacks are called on each decoded BGR
    frame (used by the detector). Sinks must not raise.
  - Lazy `from wdnav import VideoCapture` at module top (still behind the
    `try/except` from `wdnav/__init__.py`); never imports `wdnav.video`
    explicitly.
- **Service** (`app/services/detector.py`):
  - `WdnaiDetector` singleton. Wraps `wdnai.LLMSession` with
    `SystemLanguageModel.qwen3_vl_4b()` (configurable via
    `WDNAI_MODELS_DIR`).
  - **wdnai is imported lazily** at `start()` time — not at module load.
    Loading the RKNN native libs eagerly at app startup caused a
    segfault on this dev box (RKNN3 init races with wdnav.s mpp
    bring-up). Don't move the import back to module top.
  - One daemon worker thread consumes a `queue.Queue(maxsize=1)`
    (latest-only); the decode-loop sink drops whatever's pending and
    replaces it with the freshest frame so the detector always sees
    the most recent state.
  - **单帧多轮工具循环（agent-loop）**：`_infer_sync` 现在对一帧跑最多
    `compute.tool_call_max_iters`（默认 5，`0` = 无限，受
    `WdnaiDetector._ABSOLUTE_MAX_ITERS = 200` 硬上限兜底）次
    `sess.respond`；每轮若模型返回 `@Callable` 实例就执行副作用
    并把"调用 + 返回"作为**文本 attachment** 喂回下一轮 prompt，
    直到模型返回 `VLMDoc` 或循环上限命中。`sess.reset()` 每帧开头
    调一次（保留帧间 KV 隔离）；循环内不 reset。
    * 工具抛异常 → 直接发 error 事件跳出循环（不再喂回 prompt）。
    * 工具成功 → 写 transcript 继续循环。
    * `tool_call_max_iters=0` → 视为无限；`min(cfg, _ABSOLUTE_MAX_ITERS)`
      兜底，永远不会让 worker 单帧跑超过 200 次（防失控模型锁死）。
    * `_infer_sync` 返回 `List[DetectorEvent]`（一帧可多个事件），
      `worker_loop` 依次分配连续 id + 广播；前端 WS 协议不变。
    * transcript 格式刻意用 prose（不是 JSON），避免模型把 transcript
      里的旧 JSON 抄回去当成自己的工具调用重发。
  - History is an in-memory deque (max 200). Events carry `id`, `frame_pos`,
    `ts`, `prompt`, `text`.
  - On app shutdown, `close()` releases the RKNN session handle.
- **Frontend** (`app/static/js/app.js` + `#page-video-detection` markup):
  - Address input + "浏览设备…" button (opens server-side file picker
    modal) + "打开" / "关闭" buttons. The picker modal drives
    `/api/video-detection/fs` and writes the **device's** absolute path
    back into the address input — paths come from the server's
    filesystem, not the user's laptop.
  - `<canvas>` rendered from binary WS frames; auto-reconnecting WS with
    exponential backoff.
  - Play / Pause / Stop / Replay, status line, animated progress bar for
    live streams.
  - Below the video: prompt textarea + "每隔 N 帧检测一次" input + 启动/停止
    buttons + status text + 历史事件列表. Each new event also pops
    a `vd-event-badge` overlay on top of the canvas (auto-dismiss
    after 6s).

`app/api/__init__.py` must import each submodule (`from . import power, …`)
so `app.include_router(...)` in `main.py` actually sees them — without it
the package object is empty and the routes are silently missing.

## Surveillance page (视频监控)

The `#page-surveillance` section (sits **above** 视频检测 in the sidebar)
shows 16 monitoring cells in a 4x4 grid. Each cell runs its own
decoder / encoder pipeline — one daemon thread per channel, one
`wdnav.VideoCapture` per thread, one `wdnav.VideoWriter` per thread
(zero-copy — the encoder consumes the decoder's DMA-BUF via
`wdnav+ws://`). The VLM interpreter is **shared across all 16 channels**:
the ``SurveillanceManager`` owns ONE ``wdnai.LLMSession`` (the qwen3-vl-4b
RKNN contexts total ~3.5 GB resident; spinning up 16 would need
~56 GB of unified memory, which a single RK3588 board can't supply).
A single ``_InferenceWorker`` thread serialises all VLM calls into the
shared session — see "Resource budget" below.

* **Service** (`app/services/surveillance.py`):
  - ``SurveillanceManager`` singleton (lazy), owns 16 ``Channel``
    instances plus the shared ``_InferenceWorker``.
  - Each ``Channel`` carries its own config (name / source / prompt /
    detect_w / detect_h / playback_enabled / detection_enabled), runtime
    state, decode thread, H.264 packet ring buffer, and JPEG
    thumbnail ring buffer. The detection size is fixed to one of
    two presets — ``DETECT_SIZE_CHOICES = (384, 768)`` — chosen to
    match the qwen3-vl-4b model's native input (384) and a
    higher-fidelity option (768). Anything else in stored configs
    snaps back to 384 during ``_merge_default``. The
    ``ChannelConfigModel`` Pydantic schema rejects out-of-set
    values with HTTP 422 and ``detect_w != detect_h`` with HTTP
    422, so the API won't accept non-square or out-of-set sizes.
  - Decode loop (per-channel thread):
    ```text
    wdnav.VideoCapture.read0(detect_w, detect_h, fmt="RGB")
        |
        v
    _submit_vlm(...)                  # non-blocking: enqueue on the
        |                              # shared ``LLMSession`` queue
        v
    SurveillanceManager._InferenceWorker (single daemon thread, persistent asyncio loop)
        |   session.reset()  +  await session.respond(prompt, attachment=[Attachment(frame)])
        |   (always goes through wdnai — never direct rknn3lite / rknntoolkit)
        v
    Future resolves; channel._on_inference_done records the event async
        |
        v
    wdnav.VideoWriter("wdnav+ws://127.0.0.1:8000/api/surveillance/<id>/ingest", codec="h264")
        |   (zero-copy: writer.write(frame0) hands the decoder's
        |    DMA-BUF to MPP's encoder)
        v
    /api/surveillance/<id>/ingest  (server-side WS, wdnav encoder connects as client)
        |   fan-out to per-subscriber queue
        v
    /api/surveillance/<id}/stream  (browser-facing WS, raw H.264 wire format)
        |   WebCodecs (Chrome 94+ / Edge 94+ / Safari 17+ / Firefox 130+)
        v
    <canvas> in the enlarge modal
    ```
  - Thumbnail path runs in parallel: the decode thread downscales the
    decoded BGR frame to 240x135 via OpenCV, JPEG-encodes at quality
    70, and pushes the bytes to all registered
    ``/api/surveillance/<id>/thumb`` WS subscribers at ~5 fps.
  - wdnai is imported lazily (``_load_wdnai()``) inside the inference
    worker — the same module-level cache as
    :mod:`app.services.detector` so the wdnai import only happens once
    across the whole process. wdnav is imported lazily for the same
    reason (non-Rockchip dev boxes still boot).
  - Packets use the wdnav wire format
    ``<type_byte:u8><pts_us:u64 LE><payload>`` with
    ``0x00 DELTA | 0x01 KEY | 0x02 HEADER (avcC) | 0xFF EOS`` (matches
    ``wdnav.encoder._WebSocketSink`` and ``app/static/js/app.js``'s
    ``avccCodecString()``).
  - Each ``Channel`` exposes a thread-safe subscriber queue +
    ``threading.Event`` pair for the H.264 stream and another for the
    thumbnail. Subscribers are added/dropped from the asyncio side via
    ``register_stream_subscriber()`` / ``unregister_stream_subscriber()``;
    the asyncio handler drains via ``loop.run_in_executor``.
* **Config persistence** — every per-channel edit (PATCH
  ``/api/surveillance/config/{id}``) calls
  ``SurveillanceManager.update_config()`` which writes the full
  16-channel array back to ``config.json`` under
  ``surveillance.channels[]``. Reset (POST
  ``/api/surveillance/reset``) restores the 16 default entries.
  Missing fields are back-filled with defaults so older configs keep
  working.
* **API** (`app/api/surveillance.py`):
  - ``GET    /api/surveillance``              — per-channel runtime snapshot + config.
  - ``GET    /api/surveillance/config``       — full 16-channel config.
  - ``PUT    /api/surveillance/config``       — replace the whole list (validated by Pydantic).
  - ``PATCH  /api/surveillance/config/{id}``  — partial update of one channel.
  - ``POST   /api/surveillance/reset``        — restore defaults.
  - ``GET    /api/surveillance/{id}/events``  — VLM event history + snapshot.
  - ``POST   /api/surveillance/{id}/events/clear`` — clear history.
  - ``WS     /api/surveillance/{id}/ingest``  — server-side ingest; the wdnav
    encoder connects here as a client (hand-rolled RFC 6455 handshake,
    uvicorn accepts it as a vanilla client WS upgrade).
  - ``WS     /api/surveillance/{id}/stream``  — server pushes the H.264 wire
    format to the browser; the WebCodecs ``VideoDecoder`` (in
    ``app/static/js/app.js``) consumes the same payload directly.
  - ``WS     /api/surveillance/{id}/thumb``   — server pushes JPEG thumbnails
    at ~5 fps.
* **Frontend** (`app/static/js/app.js` ``Surveillance`` module +
  ``#page-surveillance`` markup):
  - 4x4 grid of ``<canvas>`` cells; each subscribes to
    ``/api/surveillance/<id>/thumb`` while the channel is running.
  - Click any cell → opens the enlarge modal
    (``#surv-modal``) showing:
    * a ``<canvas>`` fed by WebCodecs (``VideoDecoder``) using the H.264
      wire format from ``/api/surveillance/<id>/stream``;
    * a per-channel config form (name / source / prompt / detect size
      / enable toggle) with dirty-state tracking and an
      "放弃修改" button that re-fetches the saved config;
    * a live VLM event history list (incremental updates on every
      ``/events`` poll).
  - The "浏览…" button reuses the video-detection file picker modal
    (``VdFsModal``) — it writes the picked path into the surveillance
    source field when the surveillance modal is open, otherwise back
    into the video-detection source field as before.
  - The "恢复默认" toolbar button POSTs
    ``/api/surveillance/reset`` after a ``confirm()``.
  - WebCodecs is the **preferred** path for the enlarged view, but if
    the browser lacks ``window.VideoDecoder`` (older Chromium, or
    builds with WebCodecs disabled by flag), the modal falls back
    to the per-channel thumbnail WS (``/api/surveillance/{id}/thumb``)
    — JPEG @ ~5 fps painted to ``<canvas>``. Smoother motion needs a
    browser with WebCodecs; the grid cells continue to work either
    way.
  - Browser detection checks ``VideoDecoder`` + ``EncodedVideoChunk``
    + ``DecodedVideoChunk`` so we don't accept a build that exposes
    one without the others.

* **Two independent toggles per channel** — the per-channel config
  exposes ``playback_enabled`` (decodes + encodes the stream, opens
  ``VideoCapture`` and the ``VideoWriter`` to ``wdnav+ws://...``)
  and ``detection_enabled`` (gates the per-frame submission to the
  shared VLM worker). Playback can run without detection, so you
  can monitor 16 streams without ever touching the NPU. Toggling
  detection alone doesn't restart the decode thread — it's only
  consulted inside the loop.
* **Resource budget** — the manager owns ONE shared ``wdnai.LLMSession``
  (~3.5 GB resident: 2.32 GB LLM weights + 0.74 GB embed table +
  0.23 GB vision weights + a few hundred MB KV cache, all on RK3588's
  8 GB unified LPDDR5 — no dedicated VRAM, the NPU DMA reads from the
  same system RAM). All 16 channels share that one interpreter via
  the serialised ``_InferenceWorker`` queue, so the total resident
  footprint stays at ~3.5 GB regardless of how many channels are
  enabled. The trade-off is throughput: with N channels each wanting
  one inference every ``every_n`` frames at 25 fps and ~1 s per
  inference (typical qwen3-vl-4b warm call), the queue depth grows
  as ``N * 1 s * fps / every_n`` — at ``every_n=30`` that's
  ``N * 0.83``, so 16 channels queue ~13 frames deep on average. The
  events timestamp reflects when the inference actually completed,
  not when the frame was captured. If you need faster end-to-end
  latency, sharding channels across multiple ``pyrkaiweb`` instances
  (each with its own LLMSession) is the answer; the shared-session
  design here makes that a horizontal scaling decision rather than a
  per-channel one.

## Configuration

The service reads a single JSON-object `config.json` from disk and
holds it in memory. The file is seeded with a `base` section at
`{}` + future sections stacked alongside it.

- **Where it lives** — resolved from `PYRKAIWEB_CONFIG` env var when
  `run.py` starts (set by `--config /path/to/config.json`); if unset,
  defaults to `<repo>/config.json`. Always absolute.
- **Read-write** — `app/services/config.py` `ConfigStore` is a lazy
  singleton (`get_config()`) with an in-memory dict guarded by an
  `RLock`. Missing files / non-object payloads all collapse to `{}`.
- **Atomic writes** — `replace()` / `set_section()` write a sibling
  temp file and `os.replace()`-s into place so a crash mid-write
  can't corrupt the file.
- **Whole-file endpoints** (`app/api/config.py`):
  - `GET  /api/config`        — `{path, data}` (current snapshot).
  - `GET  /api/config/export` — `FileResponse` of the on-disk file
    (downloads as `config.json`). If the file doesn't exist yet,
    the endpoint writes `{}` first so the download always succeeds.
  - `POST /api/config/import` — `multipart/form-data` upload (field
    name `file`). Validates UTF-8, JSON parse, top-level object —
    rejects empty bodies, lists, scalars with HTTP 400.
- **Section endpoints** — `GET /api/config/{section}` reads a
  top-level key (e.g. `base`) and fills in missing fields with
  Pydantic defaults; `PUT /api/config/{section}` replaces just that
  key without disturbing siblings. Other sections (e.g. a future
  `power`) stay intact across `base` edits.
- **`base` section** (Pydantic model `BaseConfig` in
  `app/api/config.py`) currently has two fields:
  - `device_name` — human-readable device identifier. The default is
    generated lazily on the first `GET /api/config/base` after a
    missing/empty value (random 8-char alphanumeric, mixed case + digits,
    via `secrets.choice`) and persisted to `config.json` so the value
    stays stable across reads and reboots. Validated as non-empty
    (whitespace stripped).
  - `model_path` — absolute directory where RKNN model weights live
    (default `/userdata/models/`). Validated as non-empty +
    starts-with-`/`; the GET/PUT responses also include a
    `model_path_exists` boolean for the UI to surface a soft warning
    (the save doesn't create the directory).
- **UI** — gear button (`#config-btn`) sits next to the power button
  at the bottom-left of the sidebar. Opens a popover with 导出配置 /
  导入配置 (export / import) and the live config file path at the
  bottom. Import fires a hidden `<input type="file">`; export
  fetches the file as a blob and triggers a download. Popover and
  power popover are mutually exclusive (opening one closes the other
  — they share the same anchor and would otherwise stack).
- **Base config page** — second sidebar entry (`#base-config`),
  driven by the `Base` module in `app/static/js/app.js`. GETs the
  `base` section on entry, PUTs on submit; "放弃修改" re-fetches.
  Services downstream (e.g. the detector's model loader) read
  `get_config().get_section("base")["model_path"]` lazily, not at
- **视频监控 section on the base-config page** — same page also
  carries a sticky-header table of all 16 channels under
  `bc-section-surveillance` (`#bc-surv-tbody`). Each row exposes
  名称 / 视频源 (with "浏览…" reusing the existing server-side
  picker) / 检测提示词 / 检测尺寸 (384 / 768 单选下拉) / 播放 /
  检测 toggles.
  Save button does `PUT /api/surveillance/config` with the full
  16-channel array; the same `config.json` powers the dedicated
  视频监控 sidebar page, so edits round-trip through the shared
  store. The browse button sets `VdFsModal._sourceTarget` to the
  row's source input before opening the modal; the
  `VdFsModal.pickSelectedFile` / `pickCurrentDir` override at module
  load checks this target before falling back to the surveillance-
  modal / video-detection paths. The `Base.init()` call wires both
  the device-name form and the surveillance table; navigating to
  `基础配置` calls `Base.load()` (GET `/api/config/base`) **and**
  `Base.loadSurveillance()` (GET `/api/surveillance/config`); each
  section has its own save button that hits its own endpoint.
- **`WDNAI_MODELS_DIR` propagation** — `app/main.py` reads
  `base.model_path` at `create_app()` time and writes it into
  `os.environ["WDNAI_MODELS_DIR"]` before any `LLMSession` is built.
  wdnai's `_models_root()` reads that env var on first call, so the
  SDK picks up the configured root automatically. Changing the
  field via the UI after the app has booted only takes effect on
  the next uvicorn reload (i.e. after a restart in production) —
  an already-loaded `LLMSession` was bound to the old path.
- **Frontend modules** — `ConfigMenu` (popover), `Base` (form page);
  `PowerMenu` was untouched and is mutually exclusive with
  `ConfigMenu` via the `_wrapOpenToCloseOther` helper.
- **算力配置 (`ComputeConfigPage`) + 算力注册中心 (`ComputeRegistry`)**
  —— 用户在算力配置页启用/禁用/重启实例；每条实例绑定到一张 NPU 卡
  （由 `RKNN3Lite().get_devices_id()` 枚举）+ 一个 model preset。同一
  NPU 上多个不同 model 允许（按内存预算自我约束），但同 NPU 重复同
  model 拒绝（pydantic 校验 + 注册中心运行时校验双层防护）。内存估算
  硬编码：Qwen3-VL-4B ≈ 3500 MB / Qwen3-TTS ≈ 1900 MB / Qwen3-8B ≈
  4950 MB。Boot 流程：`boot_compute_services()` 遍历
  `config.compute.services` 逐条 probe → adopt-or-spawn；worker 起来
  后**不** build LLMSession，等用户在算力配置页选 NPU 触发 RPC load
  才 instantiate（55-90s）。UI 通过 `GET /api/compute` 每秒轮询状态，
  loading 实例显示预估倒计时（`ESTIMATED_INIT_SECONDS_PER_MODEL`）。
  Capability 概念作为内部 key 使用（`get_session("swcap_video_describe")` /
  `"swcap_audio_tts"`），注册发现的 wire 侧由 :mod:`app.services.dds_discovery`
  负责把当前活跃实例的能力转换成 wdndds ``Capability`` 实例广播到 DDS。

## Discovery page (注册发现)

`#page-discovery` 内部按从上到下三个 pane 排布：

1. **在网注册 (DDS)** —— wdndds + FastDDS 广播本机能力 + 接收对端，详见下文。
2. **硬件能力 (HW)** —— 本机硬件能力开关，源自 `HARDWARE_CAPABILITIES` 静态表 +
   `config.json["compute"]["hardware_capabilities"]` 启用状态。checkbox 修改后
   点"保存修改"才落盘，避免误触。
3. **软件能力 (SW)** —— 当前 running 实例暴露的能力（每条来自一个 RKNN 模型实例）。

### 在网注册 (DDS) 子面板

DDS 子面板（`data-pane="dds"`）通过 wdndds 调 FastDDS 把本机能力广播给
同网段其它 FastDDS participant，并显示对端列表。**不再走 HTTP / TCP 探测**
—— FastDDS 的 SPDP / SEDP 走 UDPv4 + 组播 239.255.0.1 / discovery port
7400+ 即可。

* **FastDDS 版本** —— 当前 bundled 的是 **FastDDS 2.6.12**（FastRTPS
  命名）+ **Fast-DDS-Python 1.1.x**（输出 flat ``fastdds.py``）+
  **FastCDR 1.0.24**（``libfastcdr.so.1``）。3.6.x master 配套的
  ``Fast-DDS-Python 2.6.x`` 在本机环境下 ``DataWriter::enable()`` 会
  在 ``EDP::new_writer_proxy_data`` 挂死（实测 30s watchdog 触发仍
  收不到返回），无 patch 可绕；切回 2.6.12 后 ``create_writer``
  0.00s 返回。
  - WDnddsStringTopicDataType（`wdndds_cxx/`）按 2.6.x API 重写：
    ``getKey(void*, InstanceHandle_t*, bool)`` 全指针，
    ``eprosima::fastrtps::rtps`` 命名空间，
    ``SerializedPayload_t::reserve`` (非 resize)，``setName(const char*)``
    (非 set_name)。
  - wdndds bootstrap (`wdndds/dds/_fastdds.py`) 同时认 3.x 包形式
    （``fastdds/__init__.py`` + ``fastdds/_fastdds_python.so``）和
    1.x flat 形式（``fastdds.py`` + ``_fastdds_python.so``）；
    preloader 列表里两种 SONAME 都列着，谁在 ``lib/`` 下谁被
    ``RTLD_GLOBAL``-load。切 FastDDS 主版本 = 跑
    ``FASTDDDS_BRANCH=origin/2.14.x build_native.sh`` 重 build。
  - 1.x 与 2.x 在 Python API 上有两处差异，bootstrap 已兜底：
    * ``fastdds.RETCODE_OK`` —— 1.x 是 ``ReturnCode_t.RETCODE_OK``
      类属性，bootstrap 注到模块级（值不变 = 0）。
    * ``DataWriter.write(void*)`` —— 1.x 要求 SwigPyObject 指向
      ``std::string*``，不接 Python ``str``；新 helper
      ``wdndds_create_string_data_with(value)`` 在 C 端建
      ``std::string`` 给 Python 喂。``publisher.Writer.write``
      内部对 ``str`` payload 自动走这个 fallback，非 str 透传。

* **开关 "在网注册"** —— 开启后：
  * 后端调 ``wdndds.connect()`` 起 DomainParticipant（domain_id 默认为 0）；
  * 调 ``wdndds.register(device_name, capabilities)``：device_name 来自
    `config.json["base"]["device_name"]`，capabilities 由
    `_build_capabilities` 根据 `ComputeRegistry.list_active_capabilities()`（SW,
    只看 status=running 的实例）+ `list_hardware_capabilities()`（HW,
    只看 enabled=True 的项）现拼 wdndds 叶子类（`SWVideoDescribe` /
    `HWVideoInput` / …）；
  * 调 ``wdndds.start_scan(callback)`` 订阅对端 ``wdndds/registry`` topic，
    维护内存 `discovered: {name -> {name, capabilities, first_seen, last_seen}}`。
  * 关 toggle → ``stop_scan`` + ``Hub.close()`` + 重置 wdndds 内部 ``_hub``
    全局变量；对端通过 SPDP participant 失活自然从它们的列表里移除。
* **FastDDS native bindings 未装时** → ``POST /start`` 直接返回 **503**，
  ``detail`` 为 dict（含 ``reason: "fastdds_unavailable"`` + 中文 message +
  ``dds_runtime``），前端把这条错误渲到 ``discovery-dds-runtime-banner``
  红色条 + 禁用 toggle，避免空操作。
* **`GET /api/discovery/dds`** —— Discovery 页每秒轮询的完整 snapshot：
  `state`（uninitialized / unavailable / starting / ready / stopping / error）
  / `available` / `dds_runtime` / `device_name` / `discovered[]` /
  `discovered_count` / `last_error` / `started_at` / `stopped_at` /
  `last_updated`。
* **`GET /api/discovery/dds/runtime`** —— 只读 FastDDS 可用性快照
  （调 ``wdndds.dds.check_runtime()`` 但不 connect），UI 进页面就拉一次
  渲 banner。
* **`POST /api/discovery/dds/start`** —— 幂等（state 已经是 ready 时直接返 200
  + 当前 snapshot）。**不持久化 enabled 状态** —— 跟 ComputeRegistry 一样遵
  循 "boot 不自动加载" 原则：重启后状态归零，用户必须重新打开。
* **`POST /api/discovery/dds/stop`** —— 关闭 Hub；state 已经是
  uninitialized / unavailable 时直接 200 + 当前 snapshot（幂等）。
* **错误码**：
  * **503** —— fastdds 不可用（detail 为 dict，UI 直接渲染）
  * **409** —— 切换中（STARTING / STOPPING）又来一次 POST
  * **502** —— ``connect / register / start_scan / close`` 流程任一步抛异常
* **能力 → wdndds 类映射**（`app/services/dds_discovery.py` 内
  `_CAPABILITY_FACTORIES`）：
  ```
  hwcap_video_input  → HWVideoInput    swcap_video_query    → SWVideoQuery
  hwcap_audio_input  → HWAudioInput    swcap_video_describe → SWVideoDescribe
  hwcap_audio_output → HWAudioOutput   swcap_video_analyse  → SWVideoAnalyse
  hwcap_hdmi_display → HWHDMIDisplay   swcap_audio_tts      → SWAudioTTS
                                      swcap_text_chat      → SWTextChat
  ```
  未知 ``capability_type`` 静默跳过（不让单个坏数据把整个 register 拖挂）。
* **没有 invoke handler**：本轮只做"广播自己"，cap 实例不传 ``callback``，
  对端 invoke 我们就走 ``_default_handle`` 回一个默认应答。
* **关闭** —— app shutdown 时 ``close_dds_discovery()`` 调
  ``DdsDiscoveryManager.close()`` → ``stop_scan`` + ``Hub.close()`` +
  重置 wdndds registry 状态；无 manager 实例（用户从没进过 Discovery 页）时
  是 no-op。
* **资源预算** —— 一个 ``wdndds.Hub`` 单实例长期驻留一个 daemon pump 线程
  + 少量后台 publish/subscribe 协程；不启用 toggle 时**完全空闲**（不连接
  不发 SPDP）。开启 toggle 时 Groupcast / Discovery port 由 FastDDS 默认
  0.0.0.0:7400 起动态分配；UDP 组播 239.255.0.1 必须可达。

### Discovery 页前端模块拆分

`app/static/js/app.js` 里 Discovery 相关代码分两个独立模块：

* **`Discovery`** —— 注册发现页主入口。`load()` 拉 `/api/compute/capabilities`
  渲染 HW + SW pane（一次拉取，不轮询）；HW pane 的 checkbox 修改是 dirty
  状态，点"保存修改"才 PUT 到 `/api/compute/hardware-capabilities`。
* **`DiscoveryDds`** —— DDS 子面板独立模块。`load()` 并行拉
  `/api/discovery/dds/runtime`（banner 状态）+ `/api/discovery/dds`
  （snapshot）；进入页面后 `startTicking()` 起 1Hz 轮询，让
  `state / discovered / last_error` 实时刷新；离开页面 `stopTicking()`
  释放定时器。
  * `onToggle()` 处理"在网注册"开关：根据 `_runtime.available` 预先
    拦截无 FastDDS 的点击；PUT 时设 `_toggleInflight=true` 防止 1Hz 轮询
    在 PUT 完成前把乐观 UI 覆盖回旧值；503 时把 `detail.dds_runtime`
    写回 `_runtime` 让下次 toggle 仍然被拦截。

## System logs page

The `#page-system-logs` section is wired to `app/api/system_logs.py`,
which shells out to `journalctl` and pipes JSON-lines back:

- `GET /api/system-logs/units` — service list (running / failed /
  exited / activating) + priority whitelist. Populates the unit
  dropdown so the UI never hardcodes a name.
- `GET /api/system-logs?unit=&priority=&lines=` — last N entries.
  `unit` is matched against a strict regex (`[A-Za-z0-9_.@:-]{1,128}`),
  `priority` is whitelisted, `lines` is clamped to 2000 — none of these
  reach a shell.
- `WS /api/system-logs/ws?unit=&priority=` — `journalctl -n 100 -f`
  driven by `asyncio.create_subprocess_exec`. Lines stream as
  `{"type":"line","entry":{...}}`; auto-terminates after 30 minutes
  per connection; pings every 15 s so idle proxies don't reap.

Frontend (`app/static/js/app.js` `Logs` module): filter toolbar (unit +
  priority + lines + follow-tail checkbox + clear), `<ol>` list of color-coded
  log rows on a dark stage, auto-scroll only when the user is parked at
  the bottom (so scrolling up to inspect an older line doesn't get yanked).
  Journalctl outputs `MESSAGE` as a list-of-bytes for some services
  (rknn3, etc.); `_normalize()` decodes with `errors="replace"` so the
  page never 500s on a binary blob.

## Gotchas

- **No lint / typecheck / test / CI config exists yet.** Do not invent
  `ruff`/`mypy`/`pytest` invocations — there is nothing to run, and adding
  config requires a decision the user hasn't made.
- **Reload mode picks up `.venv/src/` edits** because the editable installs
  live there. Editing wdnav/wdnai source via Uvicorn reload is fine; the
  installed metadata is in `.venv/lib/python3.11/site-packages/`.
- **`run.py` defaults to `reload=True` and port 8000** — fine for dev, but
  disable reload (`uvicorn.run(..., reload=False)`) before any production
  command, or use `uvicorn app.main:app` directly.
- **wdnav hardware path requires a Rockchip target.** If `import wdnav.video`
  raises on this machine, it's expected — the SDK falls back gracefully for
  bare `import wdnav`. Don't suppress the fallback.
- **Imports should stay lazy** for `wdnav.video` / `wdnai.*` so a non-target
  dev box can still boot the web shell.
- **`WdnaiDetector._load_wdnai()` must stay lazy**. The module-top import of
  `wdnai.Attachment / LLMSession / SystemLanguageModel` will pull in
  RKNN3 native libs at app startup, racing with wdnav's mpp init and
  segfaulting on this dev box. Keep the import behind `_load_wdnai()`,
  which is called from `start()` / `_ensure_session()`.
- **`VideoSession.open()` event ordering matters.** Always `_teardown()`
  before clearing `_stop_event` / setting `_play_event` — teardown flips
  both into the "stopped" state, so doing it the other way leaves the new
  decode thread seeing `_stop_event.is_set() == True` and exiting on
  iteration 1 (silent: state stays "playing", `frame_pos` stays 0, no
  frames ever appear on the WS).
- **RTSP/RTMP/HLS work via the same `/open` endpoint** — `VideoCapture`
  routes anything containing `://` through PyAV. The frontend just shows
  the URL in the address bar; no client-side protocol branching needed.
- **Surveillance sources loop automatically.** When a channel's source
  is a regular file the decode loop wraps around to the start when
  EOF is hit; when it's a directory the loop enumerates video files
  (``.mp4 .mkv .avi .mov .webm .flv .ts .m4v .3gp .wmv .mpg .mpeg
  .rmvb .h264 .h265 .hevc .m2ts .mts``, dotfiles skipped) sorted
  alphabetically and plays them in sequence, looping back to the
  first file when the last one ends. The folder is re-scanned on
  every wrap so newly-dropped files join the loop without a restart.
  Live streams (``rtsp://``, ``rtmp://``, ``/dev/video*``) are
  detected as ``"live"`` and never looped — they shouldn't hit EOF
  anyway. The currently-playing file is exposed in the snapshot as
  ``current_file`` and surfaces in the cell status + modal meta
  strip so users can see which clip is on during a folder loop.
- **Seek/scrub is intentionally not implemented yet.** `VideoCapture`
  doesn't expose a `set(1, frame_pos)` API; plumbing one in means
  touching `self._container` internals. The progress bar is currently
  read-only and animates only for live sources.
- **`ComputeRegistry` 启动顺序 (v0.7)**：`app.main.create_app()` 必须先
  `include_router(compute.router)`，**再**调
  `get_registry().boot_compute_services()`，**再**注册 detector 的
  frame sink。

  **`boot_compute_services()`**：遍历 `config.compute.services`
  列表（典型：``["qwen3-vl-4b", "qwen3-tts", "qwen3-8b"]``），逐条
  做 probe → adopt-or-spawn：
   - **probe**：对 `wdndds/rpc/<model_id>` 走短超时 RPC ``info``（~1.5s）
   - **命中**（LAN peer 已响应或上轮孤儿 worker 还在）→ ``_adopt_existing_worker``，
     `proc=None` + `_source` 根据 probe info 的 ``hostname`` 字段决定：
     同 hostname → ``"adopted_local"``（本机孤儿 / reload 残留），
     不同 hostname → ``"adopted"``（真 LAN peer），**不** spawn 新进程
   - **未命中** → ``_spawn_compute_worker`` 拉新子进程
     （`python compute_worker.py --model <id>`，**不传 --npu**）

  **v0.8 dedup**：spawn 之前再扫一次 `/proc/*/cmdline` 找同 model 的现存
  worker 进程（`_find_orphan_worker_pid`），命中就走 adopt 接管 —— 防
  reload 后 Popen(start_new_session=True) 的子进程脱离父进程组、reload
  SIGTERM 杀不掉导致的 NPU 内存累积泄漏。详情见 "Orphan worker 处理"
  段落。

  worker 启动后**只**起 Hub + Service + Replier，不 build LLMSession。
  状态停在 ``not_loaded``；用户在算力配置页选 NPU 后触发
  ``POST /api/compute/instances {model, npu_index}`` →
  ``bind_instance`` 走 RPC ``load{npu_index}`` 才 instantiate
  ``LLMSession(model=spec, npu=N)``。这条 boot 流程**不**自动 bind
  任何 NPU —— 完全解耦 web 与 worker：web shutdown 不 kill worker，
  worker 独立进程生命周期由 systemd / 手动 / 跨主机发现共同管理。

- **单帧工具循环硬上限**：`WdnaiDetector._ABSOLUTE_MAX_ITERS = 200`
  是兜底，不论 `config.json["compute"]["tool_call_max_iters"]` 是 0
  （无限）还是用户显式填了超大值，单帧推理都不会超过 200 次
  `sess.respond`。200 次 × 1s/inference ≈ 3 分钟单帧最坏延迟 -
  — 失控模型可以跑很久但不会锁死整个 worker 线程 / 队列。
  配置热生效：每次推理前 `_get_tool_call_max_iters()` 重读 config.json。
- **probe 线程（per-instance daemon）**：每个 spawned / adopted
  ModelInstance 在创建时起一个 daemon ``_probe_instance`` 线程，1Hz
  轮询 ``info`` RPC，把 worker 的 ``not_loaded / loading / running / error``
  同步到 registry。worker 进程意外退出时 probe 把 inst 翻 ``error``
  并捕获 stderr。Reload 模式下 uvicorn 重启父进程会杀掉 daemon thread；
  production 下靠 `close_all`（但 v0.7 起 `close_all` 不杀 worker）。
- **`ModelInstance._source` 4 个取值**（v0.8 起）：
  - ``"spawned"``        —— 本次 boot 由本机 Popen 起的 worker
                            （`proc` 句柄在 registry 里，PID 来自 `proc.pid`）。
  - ``"adopted_local"``  —— uvicorn reload 留下的孤儿 worker：probe 命中
                            的 responder 跟主进程同 hostname。proc 句柄
                            **不在** registry 里（被 SIGTERM 干掉的是父
                            进程而不是 worker），但 worker 进程还在跑、
                            能响应 RPC。`_peer_pid` 字段填 worker 的真实
                            PID，让用户能 `kill <pid>` 手动清理。
  - ``"adopted"``        —— LAN 上另一台机子的 worker 提供的 service。
                            只有本机 _setup_rpc_client 路径，没有 proc；
                            bind / 启停 / 重启全部 403。
  - ``"unavailable"``    —— `MODEL_PRESETS` 里有这个 model，但 daemon
                            全 LAN 都没回应（既没本地孤儿也没 peer）。
  UI 渲染：local 列容纳 spawned + adopted_local（按 PID 来源不同在
  `_makeLocalCard` 区分"自管 PID" 与 "孤儿 PID"，后者 重启/禁用 按钮
  disable 并提示 `kill <pid>`）；peer 列容纳 adopted + unavailable
  （`adopted` 卡片标题为 `<model> @ <device_name>`，device_name 来自
  worker `info_handler` 的 device_name 字段）。
- **Local-only operations**：算力配置页的 "启用 / 禁用 / 重启" 按钮
  可见性：
  - **spawed**：启用 / 重启 / 禁用 三按钮全开
  - **adopted_local**：只 启用 可点（走 RPC `load` 同 spawned）；重启/
    禁用 disable 提示 `kill <pid>` 手动清理
  - **adopted**：三按钮全 disable（远端机子，本机无权操作）
  - **unavailable**：只有 启用 可点（spawn 新进程）
  后端 `_require_owned(inst, action)` 对 ``_source == "adopted"`` 拦
  403，对 spawned / adopted_local 放行。
- **Orphan worker 处理（v0.8 dedup → v0.9 强化）**：uvicorn reload 路径下
  `Popen(start_new_session=True)` 起的 worker 脱离原进程组，reload
  SIGTERM 不会传过来 —— 每次 reload 都会留一个孤儿。`_spawn_compute_worker`
  在 spawn 前先扫 worker 进程，命中就走 `_adopt_existing_worker` 接管
  而不 spawn，避免 NPU 内存被无意义地消耗。`_adopt_existing_worker`
  拿到 info 后对比 `info.hostname == socket.gethostname()` 区分本机
  孤儿 vs 远端 peer。Worker 端 `info_handler` (v0.8) 返回
  `pid` / `hostname` / `device_name` 字段供主进程判断。

  v0.9 修了一个老 dedup 的洞：`_find_orphan_worker_pid` 只返**第一个**
  匹配 PID，剩下的孤儿永远没人收。新实现
  `_find_workers_by_name(model_id)` 返 `List[int]`，扫出**所有**同 model
  worker；`_adopt_existing_worker` 接管 1 个后 SIGTERM 其余（5s 宽限 →
  SIGKILL 兜底）；`boot_compute_services` 末尾再跑一次
  `_cleanup_unregistered_workers` 兜底扫
  （同时清掉 config.compute.services 移除后残留的孤儿 + proc 文件
  异常权限扫不到的边界情况）。

  **Worker 进程命名**：worker 启动后通过 ``prctl(PR_SET_NAME)`` 把
  自己的 comm（`/proc/<pid>/comm` / `ps -c`）设为 ``worker:<alias>``，
  alias 来自 ``MODEL_PRESETS[model_id]["worker_name"]``。预设：
  - qwen3-vl-4b → `worker:vlm-4b`（13 字符）
  - qwen3-tts → `worker:tts`（10 字符）
  - qwen3-8b → `worker:llm-8b`（12 字符）
  - qwen3-vl-embedding-2b → `worker:emb-2b`（12 字符）

  受 TASK_COMM_LEN=16 限制 alias 必须在 8 字符内（15 - len("worker:")）。
  命名放在 worker 端 main() 里调 —— preexec_fn 里调 prctl 会被紧随的
  exec() 用新 argv[0] basename 覆盖。`_find_workers_by_name` 同时
  fallback 扫 cmdline 兼容老 worker（reload 后的第一次清理会识别
  comm="python" 的历史 worker，全清之后 cmdline 退化为 no-op）。
- **FastDDS participant_name 命名约定 (v0.8)**：每个 DomainParticipant
  的名字都带 `config.json["base"]["device_name"]` 后缀，方便 LAN daemon
  的 `/peers` 端点和 DDS 调试日志一眼区分板子：
   - 主进程：`pyrkaiweb-main-{device_name}`
   - discovery Hub（注册发现页）：`pyrkaiweb-discovery-{device_name}`
   - compute worker：`compute_worker-{device_name}-{model_id}`
  worker 的 `info_handler` 返回的 `device_name` 字段是上述 participant
  name 后缀的来源；主进程 probe 后写到 `ModelInstance._peer_name`，
  算力配置页"网络发现"卡片标题渲染为 `<model> @ <device_name>`。
  device_name 缺失时 fallback 到 `socket.gethostname()`，保证
  participant_name 永远非空。
- **重启 4 步 (Q3)**：`POST /api/compute/restart {model}` →
  ``stop_instance_local(model)`` (unload → RPC shutdown → wait 3s →
  kill -9) → ``ensure_worker_for_model(model)``（probe → adopt /
  spawn）。**不**自动 bind —— 重启后 worker 回到 ``not_loaded``，
  用户需在算力配置页再点"启用" + 选 NPU 才完成 reload。

## TTS (语音生成) page

The `#page-tts` section drives Qwen3-TTS through the SDK's
``TTSBackend`` (defined under
``.venv/src/wdnai/src/wdnai/backends/tts.py`). The full RKNN3 driver
(four ``.1828.rknn`` models, embed tables, prefill construction,
talker/code_predictor/speech_decoder pipeline, ``AudioPlayer``
streaming) is inlined into the same file as ``Qwen3TTSDemo`` —
vendored in-tree so the SDK is self-contained and no longer has to
find ``qwen3_tts_demo.py`` anywhere on the host filesystem. The
model layout, .1828.rknn naming, and AudioPlayer streaming path
are all documented in
``.venv/src/wdnai/src/wdnai/models/README.md`` and
``.venv/src/wdnai/src/wdnai/backends/tts.py``.

* **Service** (`app/services/tts_session.py`):
  - ``TtsSession`` singleton, lazy-imports wdnai at first use so a
    non-RK dev box still boots (just reports ``state="unavailable"``).
  - State machine mirrors ``VideoSession``/``WdnaiDetector``:
    ``idle → start() → loading → running → done/error/stopped``.
    The state transitions are broadcast over ``/api/tts/ws``.
  - One daemon worker thread per request. The worker forwards
    ``stop_event`` to the SDK so a stop issued during prefill or
    decode unwinds the RKNN sessions cleanly; ``stop()`` flips the
    state immediately so the UI is responsive while the worker
    thread is still draining.
  - Output history is an in-memory deque (max 50). Each entry:
    ``{ts, text, language, speaker, output, size, audio_sec, frames}``.
* **Endpoints** (`app/api/tts.py`):
  - ``POST /api/tts/start`` ``{text, language, speaker, ref_speaker,
    volume, instruct, no_play, non_streaming_mode, output}`` — kicks
    off a synthesis. Refuses with 503 if a run is in flight.
  - ``POST /api/tts/stop`` — flips state to ``stopped`` immediately
    and propagates ``stop_event`` to the SDK.
  - ``GET  /api/tts`` — current snapshot (state / available / frames
    / audio_sec / elapsed / output / error / request / history_count).
  - ``GET  /api/tts/history`` — list of recent output WAVs.
  - ``WS   /api/tts/ws`` — pushes the snapshot on every state /
    frames / error change. Auto-reconnect with exponential backoff
    is handled on the client side (``app/static/js/app.js``
    ``TTS.ensureWs``).
* **Frontend** (``app/static/index.html`` + ``#page-tts`` markup):
  - Sidebar entry ``语音生成`` sits between ``视频检测`` and
    ``系统日志`` (mutually exclusive with other pages — the sidebar
    just toggles the ``.hidden`` class on each ``.page``).
  - Textarea + speaker/language selectors + volume slider (default
    30%, range 0–100, step 5; the label mirrors the slider value
    live via the ``input`` event).
  - Start / Stop buttons, status line, history list below. Cmd/Ctrl
    + Enter inside the textarea fires 开始生成.

* **VoiceClone speaker embeddings are bundled, not filesystem-loaded.**
  The upstream C++ demo's ``qwen3tts_speaker_embed.h`` is a static
  ``std::unordered_map<std::string, std::vector<float>>`` holding the
  reference vectors for ``girl_base`` / ``ahu`` (each 2048-dim fp32).
  That file ships only with the rknn3 dev tree and is never installed
  on a clean board. To keep the SDK self-contained, both vectors are
  encoded inline as zlib-compressed fp16 blobs in
  ``_BUNDLED_SPEAKER_EMBEDDINGS_B64`` (``.venv/src/wdnai/src/wdnai/backends/tts.py:209``)
  and decoded by ``_get_bundled_speaker_embeddings()`` on first use.
  ``Qwen3TTSDriver._load_speaker_embeddings()`` (line 635) returns the
  bundled dict directly — there is no runtime walk-up filesystem
  search anymore, so ``ref_speaker="girl_base"`` / ``"ahu"`` always
  resolves correctly regardless of the host's rknn3 layout.

**Volume semantics** — the slider value is passed straight through to
the SDK as the linear amplitude scale (``chunk *= volume`` on the
decoded float32 PCM). A slider of 0.5 is 0.5 amplitude, no
perceptual curve. The web default is 0.1 so the played PCM starts
at ~10% on hardware whose mixers default to 100% — crank the slider
to taste.

**Output WAV format** — 24 kHz, mono, 32-bit IEEE float (format tag
3, matching the C++ demo). Python's stdlib ``wave`` can't read
format-3; for inspection, use ``soundfile.read(...)`` or parse the
RIFF header by hand.

## Video file analysis (视频文件分析) page

`#page-video-file-analysis` 是演示页第 4 项（`⏏` 图标），提供整段视频的
批量 VLM 分析 —— 给定本地文件 / RTSP / RTMP / 摄像头，VLM 按 `every_n`
帧采样输出结构化 `SceneFrame`，文件走完后跑一次 Apollo 三段式综述。

* **Service** (`app/services/video_file_analysis.py`):
  - `VideoFileAnalysisService` 单例（`get_service()`）。状态机
    `idle → opening → running → summarizing → done`，`stopped` / `error` 是
    旁支终态。
  - **独立 `wdnav.VideoCapture`** —— 不挂 `VideoSession.add_frame_sink`，避免
    抢占音视频描述 / 视频监控的同一源。
  - **LLMSession 必须从 ComputeRegistry 取** —— `get_session("swcap_video_describe")`，
    复用 `qwen3-vl-4b` 实例；找不到 capability 抛 `RuntimeError("功能未启用: ...")`，
    HTTP 502 透传。
  - **进程级 `_llm_session_call_lock`**：detector 与 VFA 共享同一 LLMSession；
    `reset() + respond(...)` 不是线程安全的，必须 `with lock:` 包住。锁不绑
    capability —— VLM 端所有消费者串行化，TTS 用另一组 session 互不干扰。
    `app.services.compute_registry.get_session_call_lock()` 暴露锁对象。
  - **decode / infer pipeline 反压**：`_decode_loop` 用 `queue.Queue(maxsize=1)`
    + `threading.Event` 做 backpressure —— 采样时等 `_worker_ready`，worker
    拿走样本立即 set；避免新样本覆盖尚未处理的旧样本。
  - **per-frame SceneFrame schema**：用 wdnai `@Generable SceneFrame(LMOutput)`
    让模型输出 5 字段结构化 JSON（`text` / `scene` / `actors` / `action` /
    `objects`）；SDK 解析失败时 `_split_scene_frame_result` 五分支兜底
    （dict 归一化 → JSON parse → markdown regex → raw str）。
  - **Apollo 三段式综述**：场景检测（cv2 HSV 直方图 + Bhattacharyya 距离）
    → per-scene chunk 小结（每段 ≤120 字，`max_new_tokens=200`）→ 最终综述
    （`max_new_tokens=400`）。每完成一个 chunk 广播 `state` 进度，
    前端用 `summary_progress: [done, total]` 显示 N/M。
  - **持久化**：每次 `start()` 分配新 `run_id`（uuid4 hex），结束 / 停止 /
    出错都原子写到 `app/services/vfa_runs/<run_id>.json`（mp temp + os.replace）。
    JSON 含 `events[]` + `summary` + `fps` + `total_frames`。**老 run 没
    `fps` 字段时** `list_runs` 走 PyAV 探测自愈并回填。
* **API** (`app/api/video_file_analysis.py`):
  - `GET  /api/video-file-analysis`             — 当前快照（state / 总帧 /
    进度 / 综述 / available / init_error）。
  - `GET  /api/video-file-analysis/events`      — per-frame events + summary。
  - `POST /api/video-file-analysis/open` `{source, prompt, every_n}` —
    启动一次；400 source/prompt/every_n 校验失败；503 wdnai 不可用 / 功能未启用。
  - `POST /api/video-file-analysis/stop`        — 用户中途停止（不出综述）。
  - `POST /api/video-file-analysis/close`       — 彻底关闭。
  - `POST /api/video-file-analysis/events/clear` — 清空内存 events + summary。
  - `GET  /api/video-file-analysis/runs`        — 历史 run 列表（按 finished_at 倒序）。
  - `GET  /api/video-file-analysis/runs/{run_id}` — 加载历史 run 到内存。
  - `DELETE /api/video-file-analysis/runs/{run_id}` — 删盘上 JSON。
  - `GET  /api/video-file-analysis/runs/{run_id}/facets` — scene/actor/object/action
    去重集合（前端反查 chip 候选）。
  - `GET  /api/video-file-analysis/runs/{run_id}/query?scene=&actor=&object=&action=&exact=&q=&semantic=&frame_min=&frame_max=&limit=` —
    反查：多维 AND 过滤 + 三种搜索模式（`semantic` > `exact` > `q` 互斥）。
  - `GET  /api/video-file-analysis/runs/{run_id}/frame/{frame_pos}` —
    从原始视频取指定帧的 JPEG（PyAV v4l2m2m seek + wdnav read0 fallback）。
  - `WS   /api/video-file-analysis/ws`          — server-push `{type:"event"|"summary"|"state"|"hello"}`。
* **前端** (`app/static/js/app.js` `VFA` 模块 + `#page-video-file-analysis`):
  - 顶部进度条 + 阶段标签（`未启动 / 正在打开 / 分析中 / 生成综述中 / 已完成 /
    已停止 / 错误`），状态色随 `data-state` 切换；分析中显示"已读 N/M · 已
    分析 X/Y · LLM 队列 Z"。
  - 左侧表单：视频源（"浏览…"复用 `VdFsModal._sourceTarget = sourceEl` 机制，
    current 项目已有，写路径回到 `#vfa-source`）+ 分析要求 textarea + 隔 N
    帧采样输入 + 开始 / 停止 / 清空记录按钮。
  - 右侧 `vd-side vfa-result-col`：综述框 + 历史分析列表（每条带状态徽章 +
    `加载 / 反查 / 删除` 三个 `vda-action-btn` 横排，删除用 `margin-left:auto`
    推到最右避免误点）。
  - 下方 `<details open>` 逐帧分析列表（默认折叠，新事件抵达时只 push、
    不自动滚动；run 终态自动折叠腾出空间给综述）。
  - **反查 modal** (`#vfa-query`)：场景下拉 + 人物/物体/动作 chip 多选 +
    精准匹配 / 模糊搜索 / 语义搜索 三选一 + 时间范围 `HH:MM:SS`（on-blur 校验，
    超总时长自动截断 + 提示）。结果 list 显示 frame_pos / 时间戳 / 结构化 tag
    / 高亮 `<mark>`。点击结果 → **取帧 modal** (`#vfa-frame-modal`)：从原
    视频取对应帧 JPEG + 显示 frame_pos / fps / 该帧时间戳 / 画面描述 / 结构化。
  - 取帧路径：PyAV v4l2m2m `avformat_seek_file` 跳到 keyframe + 顺序 decode 到
    target（实测比 wdnav read0 从头顺序读快 ~150x），失败 fallback 到 wdnav。
* **DemoGuard 注册**：VFA 是演示页第 4 项，离开时弹 confirm 并 POST `/stop`；
  `active()` 判定 `state ∈ {opening, running, summarizing}`；`stopSync` 走
  `sendBeacon(/stop, POST)` 兜底。`beforeunload` 期间 JS 不能保证送达，最
  终兜底依赖服务端的 `get_vfa_service().close()` on shutdown。
* **资源预算**：与 detector 共用同一 qwen3-vl-4b 实例（~3.5 GB），典型一次
  VFA 分析 200 帧约 200s（every_n=60 × 1s/inference）。长视频（>10 分钟）
  建议 every_n ≥ 60。
* **依赖**：`requirements.txt` 加 `av>=11.0.0`（PyAV，硬取帧走 `v4l2m2m` 后端
  + `avformat_seek_file`）；`wdnav.VideoCapture` 已在内部 SDK 提供。

## Video content search (视频内容检索) page

`#page-video-content-search`（侧栏演示项，`⌕` 图标）暴露
Qwen3-VL-Embedding-2B 检索 embedding：选一个本地视频文件 → 按时间窗
切片 → 每片 embedding → 文本余弦检索出命中片段 → 点命中卡片用真实
帧流播放。状态机 `idle → extracting → ready`，`stopped` / `error` 是
旁支终态。

* **Service** (`app/services/video_content_search.py`)：
  - `VideoContentSearchService` 单例（`get_service()`）。
  - **LLMSession 必须从 ComputeRegistry 取** —— `get_session("swcap_video_retrieval")`，
    复用 qwen3-vl-embedding-2b 实例；找不到 capability 抛
    `RuntimeError("功能未启用: ...")`，HTTP 503 透传。`start_extract`
    加预检 `_ensure_session()`，不用等 worker 探测才报错。
  - **进程级 `_llm_session_call_lock`**：与 detector / VFA 共用同一套锁，
    `embed_text` / `embed_video` 的 `reset() + respond` 类推理必须
    `with lock:` 包住（embedding 的 `embed_*` 也是 NPU 推理，同样要
    与 VLM 端串行化）。
  - **切片 embedding**：PyAV 按 `[start, start+chunk_seconds)` 时间窗
    推进（步进 = chunk_seconds - overlap），每片取 `chunk_seconds` 个
    等距帧（1 秒/帧）缩放到 576×576，`embed_video` 得 2048 维 L2
    归一化向量，存内存 `List[{video_path, start_time, end_time,
    chunk_idx, fps, vec}]`。逐片推进 `slices_embedded` 并 WS 广播
    `progress`。`_probe_video_meta` 用 PyAV 探测 fps / 总帧数 / 时长。
  - **检索**：`embed_text` 编码查询 → 遍历索引算余弦相似度 → 阈值
    过滤（默认 `sim_threshold=0.5`）+ `top_k`（默认 10）截断。索引为
    空时直接返回空命中。
  - **`iter_segment_jpeg(source, start_s, end_s, paced=...)`**：按播放
    顺序生成区间内每帧 JPEG（PyAV seek 到 keyframe + 顺序 decode +
    cv2 编码）；`paced=True` 时按源视频 fps 节流（**绝对时钟对齐**，
    sleep 补偿编码耗时，帧间隔精确 = 1/fps，缺失回退 `fallback_fps=25`）。
* **API** (`app/api/video_content_search.py`)：
  - `GET  /api/video-content-search` — 快照（state / 进度 / query /
    hits / fps / source_exists）。
  - `POST /api/video-content-search/open` `{source}` — 选单个视频文件。
  - `POST /api/video-content-search/extract` `{chunk_seconds,
    chunk_overlap_seconds}` — 开始切片 embedding；chunk_seconds ∈
    {3,4,5,6}、overlap ∈ {1,2}（Pydantic/service 校验，非法 400）；
    实例未启用 503。
  - `POST /api/video-content-search/extract/stop` — 中途停止（退回 idle）。
  - `POST /api/video-content-search/query` `{text, sim_threshold,
    top_k}` — 文本检索；`embed_text` 同步 NPU 推理挪
    `run_in_executor` + **35s 超时**（SDK 卡死返 504，不阻塞 event
    loop；对齐 VFA 做法）。
  - `WS   /api/video-content-search/ws` — server→client：`hello`
    （连接即发快照）/ `progress`（embedded, total）/ `state`。
  - `WS   /api/video-content-search/play` — 播放命中片段：客户端首条
    JSON `{source, start, end}` → 服务端 daemon 线程跑
    `iter_segment_jpeg(..., paced=True)` 推二进制 JPEG 帧；客户端
    `{"action":"pause"}` / `{"action":"resume"}` 控制（
    `threading.Event`，pause 时阻塞 reader）；播完 `{"type":"eof"}`、
    出错 `{"type":"error"}`；断开连接即停线程。首条参数 10s 超时，
    非法 source / `end<=start` 直接 1008。
  - `GET  /api/video-content-search/frame?source=&t=` — PyAV seek 到
    `t` 秒附近取帧 JPEG（命中卡片缩略图），失败 404。
* **前端** (`app/static/js/app.js` `VCS` 模块 + `#page-video-content-search`)：
  - 左侧表单：视频源（"浏览…"复用 `VdFsModal._sourceTarget` 机制）+
    切片档位（3/4/5/6s，重叠 1/2s）+ 提取按钮；中间进度条 + 阶段标签。
  - 右侧命中结果列表（相似度降序，每条带开始/结束时间），点卡片 →
    `vcsPlaySegment` 连 play WS 真实帧播放：等比 contain 填满播放
    窗口（`vcsSyncCanvasSize` 把 canvas 像素同步为 `.vd-stage` 显示
    尺寸，消除元素级黑边）、进度条按已收帧数/总帧数实时推进
    （`VCS.snap.fps` 算总帧数）、暂停/恢复/停止/播完重播。
  - `DemoGuard` 注册 `video-content-search`，离开页面 confirm + 停
    play WS + sendBeacon 兜底，与其它演示页一致。
* **ComputeRegistry preset**（`app/services/compute_registry.py`）：
  - `MODEL_PRESETS["qwen3-vl-embedding-2b"]`：`capability_types=
    ["swcap_video_retrieval"]`，`memory_estimate_mb=2000`，
    `estimated_init_seconds=30`。`kind="embedding"` 的 probe /
    `_init_session_subprocess` 分支按 vlm 文件布局校验（rknn_llm +
    rknn_vision + embed + tokenizer）。
* **资源预算**：qwen3-vl-embedding-2b 单实例 ~2 GB（LLM + vision 权重
  + 622MB embed 表）。检索单视频索引在内存，长视频（>1h @ 4s 片 /
  1s 重叠 ≈ 1200 片 × 2048 维 fp32 ≈ 10MB 向量）可忽略。播放走 PyAV
  顺序解码，不占 NPU。

## Text chat (文本对话) page

`#page-text-chat` 暴露 Qwen3-8B 纯文本对话（`swcap_text_chat` capability），
流式逐字渲染 `<think>...</think>` 与正式回答，单页实例串行推理，
多轮上下文由 app 端消息 deque + ChatML 拼接维持。

* **Backend** (`app/api/text_chat.py`):
  - `GET /api/text-chat` — snapshot：state / busy / error / history_count /
    history_tokens / context_limit / max_new_tokens / current_request_id /
    last_result。
  - `POST /api/text-chat/send` `{text}` — 发起一轮推理；返回
    `{request_id, snapshot}`。400 text 为空 / 409 还在生成中 /
    502 capability 未启用。
  - `POST /api/text-chat/stop` — 立即调 `session._backend.rknn.session_stop()`
    中断 C 端正在跑的推理；当前轮丢弃，不写历史。
  - `POST /api/text-chat/reset` — 清空 history + history_tokens，回到 idle。
  - `WS /api/text-chat/ws` — server-push 流式 token + 状态变更；连接后立即
    推 `{type:"snapshot", snapshot:...}` hydrate。

* **Service** (`app/services/text_chat.py`):
  - `_TextChatManager` 单例（`get_chat_service()`，lazy）；状态机
    `idle → loading → running → done/error/stopped`，与 pyrkaiweb
    其它服务风格一致。
  - **LLMSession 必须从 ComputeRegistry 取**（`get_session("swcap_text_chat")`）；
    找不到抛 `RuntimeError("功能未启用: swcap_text_chat")`，HTTP 502 透传。
  - **wdnai 导入懒加载** —— 模块顶层只 import `os / queue / re / threading / asyncio`；
    `_install_patches()` 在第一次构造实例时才 import `wdnai.backends._common`。
  - **流式回调 = monkey-patch**：模块加载时把
    `wdnai.backends._common.make_result_callback` 替换成包装版。包装版
    在原 callback 完成后读 `sink.text` 算增量，调用 `register_observer`
    注册的 callable（key = `id(sink)`）。未注册的 sink 走原 callback，
    零开销（其它业务 detector / tts 不受影响）。
  - **`_ThinkingRouter`**：observer 每次拿到 delta 增量时按 `</think>` 切频道
    推到 `thinking` / `answer` 两条流；`done` 时再正则
    `<think>(.*?)</think>` 兜底切一次，处理 token 边界切碎等极端情况。
  - 单 daemon worker thread（`text-chat-worker`）串行消费 `request_queue`；
    `session_run` 是同步阻塞，worker 在 `loop.get(timeout=1.0)` 期间
    监听 `_worker_stop_event`，stop 事件置位即调
    `session._backend.rknn.session_stop()`。
  - **32K token 限制**：每收到 `done` 用 `tokenizer.encode(content)` 累加
    `history_tokens`；超过 `context_limit - max_new_tokens` 时拒绝发新
    请求（HTTP 400 + 中文提示）。预留输出 = `max_new_tokens = 2048`，
    spec `max_context_len = 32768`。
  - **多轮上下文**：app 端消息 deque，每轮 `session.reset()` 清 C 端 KV，
    再用 ChatML（`<|im_start|>system\n...<|im_end|>\n<|im_start|>user\n...`
    系列）拼整段 prompt 喂进 `session._backend.run(prompt, keep_history=0)`。
    **历史中只写 `content:answer`，不写 thinking** —— 下轮 prompt 看不到
    thinking，保证模型下一轮从干净的对话记忆开始。spec.options 的
    `keep_history=1` 是 init runtime 默认值（按用户要求保留），但
    per-call override 设为 0，所以 KV 不跨 run 续接。
  - WS broadcaster pattern：worker 线程 push 到 `asyncio.Queue` 列表，
    WS handler 在主 event loop 里 `await queue.get()` drain；observer
    也走同一条路径，避免第二条 channel。

* **ComputeRegistry** preset（`app/services/compute_registry.py:136-164`）：
  - 新增 `MODEL_PRESETS["qwen3-8b"]`：`capability_types=["swcap_text_chat"]`，
    `memory_estimate_mb=5500`，`estimated_init_seconds=90`（实测待校准）。
  - `_qwen3_8b_spec()` 工厂用 `dataclasses.replace(spec, max_context_len=32768)`
    + `with_options(max_new_tokens=2048, keep_history=1, enable_thinking=True)`。
    注意 `max_context_len` 是 `ModelSpec` 的 dataclass 字段（被
    `_common.build_llm_args` 直接读），**不能用 `with_options` 设**，必须
    `replace`。
  - `_init_session_subprocess` 子进程硬编码分支加 `elif model_id == 'qwen3-8b'`。

* **Frontend** (`app/static/index.html` + `app/static/js/app.js` + `style.css`)：
  - 左栏 demo 区第 5 项 `<a data-page="text-chat" data-icon="✎">文本对话</a>`，
    排在 `#tts` 之后。
  - `<section id="page-text-chat">`：复用 `.imb-card` / `.imb-conv` /
    `.imb-composer` / `.imb-msg--in/out` 等现有样式；新增
    `.text-chat-thinking`（折叠块，初始折叠，有内容时自动展开）、
    `.text-chat-cursor`（打字机光标 blink 动画）、`.text-chat-stopped` /
    `.text-chat-error`。
  - JS 模块 `TextChat`（仿 `ImBridge` 模式）：
    - `init()` 绑定 send / stop / reset / Cmd+Enter；
    - `refresh()` 拉 snapshot；
    - `ensureWs()` / `closeWs()` 指数退避重连（`1 << (wsRetry-1)` ms，
      上限 30s）；
    - `_onWsMessage(msg)` 分发 `snapshot` / `state` / `user_echo` /
      `thinking` / `answer` / `done` / `stopped` / `error` 八种帧；
    - `pending: Map<request_id, ...>` 维护每轮推理的"用户气泡 →
      assistant 气泡"三段渲染状态；
    - `_scrollToBottom()` 仅在用户靠近底部时自动滚动（避免抢焦点）。
  - `loadPageData("text-chat")` 调 `TextChat.init() + refresh() + ensureWs()`；
    `showPage()` 离开时调 `TextChat.closeWs()`。

* **资源预算** —— 单实例常驻 ~5.5 GB（Qwen3-8B 量化权重 + embed table +
  KV cache 200×2K token）。板上 8 GB 统一内存比较紧张，但与 Qwen3-VL-4B
  单实例不冲突（2 个同时跑超 9 GB）。多用户并发首版不支持 —— worker
  串行；如需横向扩展，在算力配置页加第 2 个 Qwen3-8B 实例（多卡环境）。

* **限制 / 已知问题**：
  - 32K 上下文硬上限写死；改需同步 `spec.max_context_len` + 服务端
    `CONTEXT_LIMIT` 常量 + 前端 token 计数器。
  - thinking 不写历史 —— 用户"接着问"时下一轮 prompt 看不到上轮思考；
    想要延续 thinking 行为需把 `thinking` 也 push 到 history 的 assistant
    content 字段，但代价是污染 KV 且长对话下 token 增长更快。
  - 重生成按钮首版不做（plan §十三已确认）。
  - 真机首次初始化 ~90s（按经验值，需真机 page-cache-dropped 实测校准）。
  - 取消正在跑的轮次靠 `rknn3lite.session_stop()`；C 库收到 stop 信号后
    回调走 FINISH state=3，`session_run` 返回。当前 `sink.text` 丢弃，
    不写历史。

## When you change things, also update

| You changed…                          | Then update…                                              |
| ------------------------------------- | --------------------------------------------------------- |
| Service version                       | `app/__init__.py` `__version__` (single source of truth)  |
| Default bind host / port              | `run.py` `main()`                                        |
| Public SDK dependency                 | Re-run the two `pip install -e` lines above              |
| Anything that needs a new endpoint    | Add a route inside `create_app()` in `app/main.py`        |
| Added/removed a model preset          | `MODEL_PRESETS` in `app/services/compute_registry.py` + `register` the new capability in the same table; update `app/static/index.html` and `app/static/js/app.js` only if the UI needs new model cards |
| `ComputeRegistry` config schema       | `ComputeConfigPayload` in `app/api/compute.py` + `MODEL_PRESETS` keys; reboot-flush config (`PUT /api/compute/config`) carries Pydantic validation |
## Tools (音视频检测页工具)

`app/services/tools/` 装的是给音视频描述页 (`WdnaiDetector` 走
`VLMDoc` schema 时) 用的 wdnai ``@Callable`` 工具集合，**不**进入
视频监控 (16 路) — 那条通路有自己的 surveillance manager，工具
语义不同，这一轮不动。

* **唯一保留的内置工具** — ``tts_alert``：语音提醒，模型给出
  ``text`` / ``language`` / ``ref_speaker``，``TtsSession.start``
  触发本机播放。能力门控
  ``requires_capability="swcap_audio_tts"``。
  工具注册中心 ``app/services/tools/registry.py`` 中
  ``get_tool_registry()`` 启动时只挂载 ``tts_alert`` 一条。

* **页面归属**：
  - ``tts_alert`` 只在 **音视频描述页** 暴露
    （``ALLOWED_TOOLS = ("tts_alert",)`` 在
    ``app/api/video_detection.py``）。
  - 算力配置页 RK3588 列只读展示全部工具的状态（当前只有
    ``tts_alert`` 一条）。

* **历史下线 — ``target_detection`` 工具**：原本是给 **视频目标
  检测页** 用的 ``@Callable``，模型给出 0–1000 归一化 bbox + ROI
  描述，后端裁切 + JPEG 编码 + base64 内嵌。本版本已**完全下线**：
  - 工具文件 ``app/services/tools/target_detection.py`` 已删除；
  - 注册表不再有 ``target_detection`` 项；
  - ``app/api/video_target_detection.py`` 不再有
    ``ALLOWED_TOOLS`` 常量与 ``/video-target-detection/tools``
    端点（视频目标检测页没有"工具集合"概念）；
  - 替代实现见下方 "TargetDoc 结构化输出"段。

* **能力门控（双层防护）**：
  - UI：``GET /api/video-detection/tools`` 列表里每条带
    ``available`` / ``reason``；不可用时 checkbox 置灰、显示
    原因。
  - 后端：``WdnaiDetector.start()`` 校验每个 ``enabled_tools``
    的 capability (``get_registry().get_session(spec.requires_capability)``)，
    未启用抛 503。

* **会话级，不持久化**：用户在检测页勾选的 tools 集合只
  存在内存里 (``WdnaiDetector._enabled_tools``)；重启 / 关闭页
  面 / 停止检测都清空，不写 ``config.json``。``tts_alert`` 触发
  的副作用（已播放 / 失败等）只在 ``DetectorEvent`` deque 里活，
  刷新页面 / 重启就丢；不写盘、不另开缓存。

* **单帧多轮工具调用（agent-loop）**：详见上文 detector 段。
  配置入口 ``config.json["compute"]["tool_call_max_iters"``（默认
  5，``0`` = 无限，缺失 / 负值 / 类型错 → 回退默认 5）；任何路径
  都被 ``WdnaiDetector._ABSOLUTE_MAX_ITERS = 200`` 硬上限兜底。
  工具调用 + 返回按 prose 格式写进下一轮 prompt（不是 JSON，避免
  模型复读），由 detector 内部维护。视频目标检测页无工具，循环
  天然只跑一轮就跳出。

## TargetDoc 结构化输出 (视频目标检测页)

取代旧的 ``target_detection`` ``@Callable`` 工具 —— 模型在生成
结构化输出时**直接**给出 ROI 坐标 + 描述文本，不需要再跑工具
链路。

* **Schema 定义** (``app/services/detector.py``)：
  ```python
  @Generable
  class TargetDoc(BaseModel):
      x1: float   # 0–1000 归一化
      y1: float   # 0–1000 归一化
      x2: float   # 0–1000 归一化
      y2: float   # 0–1000 归一化
      desc: str   # 对 ROI 框出内容的描述
  ```
  由 ``_get_target_doc_cls()`` 懒加载（首次 start() 时 build 一
  次），存到模块级 ``_TARGET_DOC_CLS`` 缓存。同 ``VLMDoc`` 一样
  继承 ``pydantic.BaseModel`` 而非 ``LMOutput``，避免
  ``attachment: List[Attachment]`` 字段污染 ``model_json_schema``。

* **Detector 实例绑定 schema** — ``WdnaiDetector.__init__`` 接
  ``generating_cls`` 参数；工厂函数按页面分别绑定：
  - ``get_detector()``         → ``generating_cls`` 留空 → ``VLMDoc``
  - ``get_target_detector()``  → ``generating_cls=_get_target_doc_cls``
    → ``TargetDoc``

* **推理分支** (``WdnaiDetector._infer_sync``) — 跑的多轮工具循
  环不变，但 ``generating=`` 按实例配置；拿到结果后三路分发：
  1. ``@Callable`` 工具实例 → ``_run_tool_side_effect``
     （当前只有 ``tts_alert``）；
  2. ``TargetDoc`` 实例      → ``_build_target_event``
     （视频目标检测页走这条）；
  3. 其他（``VLMDoc`` 等）  → ``text`` / ``level`` 兜底分支
     （音视频描述页走这条）。

* **TargetDoc → DetectorEvent 构造**
  (``WdnaiDetector._build_target_event``)：
  - 从 ``x1, y1, x2, y2`` 拼 ``bbox: List[float]``；
  - ``desc`` 过 sanitize（防 chatml token / 控制字符 / 半截 JSON
    漏出）→ 复制到事件 ``text`` 字段（沿用历史展示路径）；
  - 按 bbox 裁原图 + JPEG 编码 (quality 85) + base64 内嵌到
    ``crop_b64``，事件含 ``crop_w`` / ``crop_h``；
  - ``tool_call='target_detection'`` 字段仍然写入事件 JSON —— 作
    为"TargetDoc 事件"的判别字段，前端
    (``ev.tool_call === "target_detection"``) 用来定位带 bbox /
    crop 的目标检测事件。语义上不再是 "工具被调用"，而是
    "TargetDoc schema 产生了事件"；与 ``tts_alert`` 的真实工具
    调用路径区分。
  - 裁切为空时 ``crop_b64=""``，事件仍写出，``level='error'``。

* **bbox 渲染**：后端把 bbox 通过 ``DetectorEvent.bbox`` 字段推到
  WS（事件类型仍是 ``type:"event"``，仅追加字段）。前端**不**
  在画布上绘制矩形框 —— bbox 只在事件历史列表里记录。浮动窗 /
  历史缩略图仍展示裁切图本身。
  ``target_detection`` 在浮动窗 + 历史缩略图里展示裁剪图本身，浮动窗
  5 秒后淡出。
* **工具列表已并入算力配置页 RK3588 列**：原 ``#page-tools`` 页面与
  ``GET /api/tools`` 端点都已移除；工具元数据现通过
  ``GET /api/compute`` snapshot 的 ``tools`` 字段返回（懒导入
  ``app.services.tools``，避免在 compute 模块加载时触发 wdnai 装饰器）。
  RK3588 列只读展示：默认全部"已启用"badge，依赖 capability
  未启用时切到"不可用"红色 badge；该列不允许手动 toggle 工具，
  仅作为状态展示。音视频描述页 / 视频目标检测页各自的 per-detector
  工具勾选（``/api/video-detection/tools`` /
  ``/api/video-target-detection/tools``）不受影响。


## LLMSession init 轻量预检（v0.5.0 起的优化）

`ComputeRegistry._run_one` 启动一条实例的流程：

1. **轻量预检** (`_probe_session_setup`，<1s)：
   * model_id 在 preset 白名单
   * preset 工厂能正常构造 `ModelSpec`（不调 rknn3）
   * **模型文件**按 `spec.kind` 分支校验（v0.5.x 起）：
     - `kind="llm"`：`rknn_path` + `weight_path` + `embed_path` + `tokenizer_path`
     - `kind="vlm"`：`rknn_llm_path` + `rknn_vision_path` + `embed_path` +
       `tokenizer_path`（`weight_llm_path` / `weight_vision_path` 由
       `.1828.rknn` 路径自动 derive，probe 不单独校验）
- `kind="tts"`：`tts_model_dir` 目录存在即可；具体
        `.1828.rknn` / `.weight` / `.fp16.bin` 清单交给 backend 真实 init
        阶段报更精确错误
    * `RKNN3Lite().get_devices_id()` 枚举得到 ≥ `npu_index + 1` 张卡
2. **父进程完整 init** (`LLMSession(model=spec, npu=npu_index)`，~55s)：
   * `try/except` 包住；非 std::terminate 错误都进 `inst.error`
3. **持久化** + LOG 显示 `probe=Xs init=Ys total=Zs`

**历史**：v0.5.0 之前是先在**子进程**里做完整 init 当探针（55s），
父进程再完整 init 一次（55s）—— 总耗时 ~110s，是 demo `summarize_text.py`
（55s）的两倍。优化后实测 56s（probe 1s + init 55s），与 demo 一致。

**权衡**：失去"rknn3 native abort 子进程隔离"保护 —— 但 AGENTS.md
里记载的 `std::terminate()` 现象**仅在非 RK 开发板 / 算力配置错的板
子**上见过，真机 RK3588 极少发生。旧版 `_init_session_subprocess`
完整函数**保留**在 `app/services/compute_registry.py:756+`（当前不
调用），将来真机若出现 abort，可以把 `_run_one` 改回原路径（恢复
双 init）或加 fallback 策略。

**实例配置**（`MODEL_PRESETS["qwen3-8b"]`）：

| 字段 | 值 | 备注 |
|---|---|---|
| `memory_estimate_mb` | **4950** | 真机 4 卡板 RK1828 实际占用 |
| `estimated_init_seconds` | **60** | 实测 ~55s + 5s buffer |

## Im-bridge page (应用对接 — 微信 iLink)

`#page-im-bridge` 拆成两块：左侧登录面板 + 右侧调试收发对话
（user_id 下拉 + 文本框 + 消息列表）。底层走 [`wechatbot-sdk` 0.3.0](https://www.wechatbot.dev/zh/python)，
但调用入口是 `.venv/src/wdndds/src/wdndds/wechat.py` 里 `wdndds.Wechat` 的
**同步门面**（自带 daemon-thread asyncio loop），不是直接 `await` SDK。

* **服务** (`app/services/wechat.py`)：
  - 单例 `_WechatManager`（lazy，``get_wechat()``），内部持一个 `wdndds.Wechat`。
  - 后台线程 1Hz 轮询 `login_status()`，状态变化时 push 给 WS 订阅者；
    新入站消息通过 `register_message_handler` 立即触发推送，不等下一轮。
  - 内存里维护最近 200 条消息（in/out 各保留）+ 已知 user_id 集合。
  - **不自动 start()** —— 必须由 UI 点 登录 才进入 long-poll。凭证存在
    也只存不启动，决策与 `ComputeRegistry` "boot 不自动加载" 一致。
  - ``close_wechat()`` 用于 app shutdown；如果 manager 从未被创建过
    （用户从来没进过 im-bridge 页面）则是 no-op，避免空跑 daemon loop。

* **API** (`app/api/wechat.py`)：

  | Method | Path | 用途 |
  |---|---|---|
  | `GET`  | `/api/wechat`              | snapshot：state + qr_url + recent + known_users |
  | `GET`  | `/api/wechat/users`        | 已知 user_id 列表 |
  | `GET`  | `/api/wechat/runtime`      | SDK 版本 + available 标志（前端 "SDK" 行展示） |
  | `POST` | `/api/wechat/login` `{force?: bool}` | 触发 QR 登录 |
  | `POST` | `/api/wechat/cancel-login` | 取消正在进行的 QR 流程 |
  | `POST` | `/api/wechat/logout`       | 删凭证 + 停 bot |
  | `POST` | `/api/wechat/send` `{user_id, text}` | 发文本；`NoContextError` → 400 |
  | `WS`   | `/api/wechat/ws`           | 推 `snapshot` / `state` (diff) / `message` 帧 |

  - send 端点对 `wechatbot.NoContextError` 返回 400 并附中文提示（"该用户
    尚未向你发送过消息，context_token 未建立"）；其他错误 502。
  - WS 握手后**立刻**推一帧 `{"type":"snapshot", "snapshot":...}` 让客户端
    无需额外 GET 也能 hydrate。

* **前端** (`app/static/js/app.js` `ImBridge` 模块 + `#page-im-bridge`):
  - 左侧：登录按钮 / 取消 / 退出 + 凭证路径 + user_id + long-poll 状态
    + SDK 版本 + QR `<svg>` 渲染（vendored Kazuhiko Arase
    `qrcode-generator` 1.4.4，MIT，文件 `app/static/js/qrcode.js`，**不**
    引外部 CDN 也不增加 Python 依赖）。
  - 右侧：user_id `<datalist>` 下拉 + 文本框 + 发送按钮（Cmd/Ctrl+Enter
    触发）；消息列表按 user_id 过滤，in/out 分色（`imb-msg--in` /
    `imb-msg--out`）。
  - WS 自动重连 + 指数退避（与 TTS / VD 一致）。
  - 状态徽章按 `login_state` 上色：未登录（灰）/ 等待扫码（黄）/ 已扫码
    （蓝）/ 已登录（绿）/ 过期 / 错误（红）。

* **消息流**：
  ```text
  [微信用户扫码]
      ↓ SDK 推送 QR URL → on_qr_url
  wdndds.Wechat._qr_url set, login_state=awaiting_scan
      ↓ 1Hz 轮询 + 回调即推
  _WechatManager._push_state_diff → WS broadcast {type:"state", state:{login_state:..., qr_url:...}}
      ↓ 前端 _renderQr() 用 qrcode-generator 把 URL 渲染成 <svg>
  [用户在手机点确认]
      ↓ SDK 回调 on_scanned → on_confirmed → bot.start() 自动起 long-poll
  login_state=confirmed + started=True
      ↓ [用户A 发消息]
  wechatbot bot._dispatch(msg) → wdndds._on_incoming → _WechatManager._handle_incoming
      ↓ _push_state_diff → WS broadcast {type:"message", message:{direction:"in", ...}}
      ↓ 前端 _recordMessage + _renderChat → 追加到列表
  [用户点发送]
      ↓ POST /api/wechat/send
  wdndds.Wechat.send_text → wechatbot.bot.send(user_id, text)
      ↓ SDK _send_content + _send_text
      ↓ wdndds._record_outbound → _push_state_diff → WS broadcast {type:"message", direction:"out"}
  ```

* **凭证路径** = `~/.wechatbot/credentials.json`（`wdndds.wechat.DEFAULT_CRED_PATH`），
  也可设 `WDNDDS_WECHAT_CRED_PATH` 环境变量覆盖。这是 SDK 默认路径，
  多 pyrkaiweb 实例不会冲突。

* **资源预算** —— `wdndds.Wechat` 单实例长期驻留一个 daemon thread + 一个
  aiohttp 连接池；未登录时 SDK 不会建任何连接（`bot.login()` 之前
  `_api` 只在 `poll_qr_status` 时拉 HTTP）。登录失败 / QR 过期 → SDK
  内部走 `MAX_QR_REFRESH_COUNT=3` 轮后抛 `AuthError`，wrapper 把
  `login_state` 切到 `error`，UI 看到红色徽章 + 点 登录 重试。


## wdndds_daemon — FastDDS Discovery Server (v0.5)

v0.5 在 v0.4 的 per-model-subprocess RPC 之上加了**集中式发现 daemon**，
绕开 FastDDS 2.9 在本机的 SPDP multicast 发现 bug（**改用 SERVER/CLIENT
协议 + UDP 单播**）。

### 架构

```
       ┌───────────────────────┐
       │   wdndds_daemon       │   唯一一个进程
       │   (FastDDS SERVER     │   默认端口：UDP 7411 / HTTP 7412
       │    discovery protocol)│   (可在 config.json 里改)
       └───────────────────────┘
                 │  (UDP 单播：peers 互相发现)
       ┌─────────┼─────────┼─────────┐
       ▼         ▼         ▼         ▼
   pyrkaiweb  worker   worker   worker     (CLIENT protocol,
     main     qwen3   qwen3      qwen8      discovery_servers 指向 daemon)
```

* 每个 LAN 跑一个 `wdndds_daemon` 进程。**没有 fallback**（多播 / SIMPLE
  在嵌入式板不可靠；server 故障 = 整个 LAN 不通）。
* 所有 pyrkaiweb 进程（main / compute_worker / im-bridge / 任意）
  通过 `config.json["compute"]["discovery_servers"]` 指向 daemon 的
  `host:port`。
* daemon 同时服务于「compute RPC 跨机器发现」和「Discovery 页在网注册」
  —— **同一个 daemon，两个用途**。

### wdndds_daemon 启动方式

**1) 主进程随 pyrkaiweb 一起启动（dev / 小部署）**

`config.json["compute"]["auto_start_daemon"]: true` 时，`app/main.py`
`create_app()` 会 `subprocess.Popen` 拉起 daemon（默认端口可改）。

**2) 手动启动（生产 / systemd）**

```bash
# 前台
.venv/bin/python -m app.services.wdndds_daemon --udp-port 7411 --http-port 7412

# 通过 systemd unit（生产推荐）
[Unit]
Description=wdndds discovery daemon
After=network-online.target

[Service]
Type=simple
ExecStart=/userdata/pyrkaiweb/.venv/bin/python -m app.services.wdndds_daemon     --udp-port 7411 --http-port 7412
WorkingDirectory=/userdata/pyrkaiweb
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

**3) HTTP API（前端 / 调试用）**

```
GET  /status   {"running", "started_at", "uptime_sec", "udp_port", "guid", "participant_count"}
GET  /peers    {"peers": [{"guid", "name"}, ...]}   (基于 DomainParticipantListener)
GET  /health   {"ok": true}
```

### pyrkaiweb 端配置

`config.json["compute"]` 字段：

```json
{
  "instances": [{"npu_index": 0, "model": "qwen3-vl-4b"}],
  "discovery_servers": ["127.0.0.1:7411"],   // 主进程 / worker 都读这个
  "discovery_server_port": 7411,            // 仅 auto_start_daemon 用
  "daemon_http_port": 7412,                 // 同上
  "auto_start_daemon": false                // 默认 false（生产用 systemd）
}
```

`compute_registry._persist()` **保留** `discovery_servers` / `ports` /
`auto_start_daemon` 字段（v0.5.1 修复：之前会覆盖整个 compute section）。

### 跨进程 RPC：发现 + 数据平面

* **发现**：daemon 跑 SERVER 协议，所有 client 跑 CLIENT 协议 →
  daemon 监听 UDP 7411 → 每个 client 把自己的 participant + endpoint
  注册到 daemon → daemon 把 endpoint 信息分发给所有 client。
* **数据平面**：compute_worker 用 `SHM + UDPv4 + TCPv4` 三种 transport；
  SHM 走同机零拷贝，UDPv4 走同 LAN，TCPv4 走跨子网。
* **共享 reply topic**：request / reply 两个 topic 复用 shared reply。
  请求 ID 在 ComputeRequest/ComputeReply 里匹配（参见
  `wdndds/idl/compute.idl`）。
* **首次调用 race**：daemon 刚启动时 endpoint 还没匹配，第一次
  `RpcClient.call()` 会 timeout（约 5s），第二次起成功。pyrkaiweb
  probe 自动 retry（每 1s 一次，5s timeout）。

### 注意事项

* **本机 FastDDS 3.6.2 编译 + 源码安装**（参见
  `.venv/src/wdndds/build_native.sh`）：libfastdds / libfastcdr /
  libwdndds_idl / libwdndds_cshim 全部安装到 `wdndds/native/`，不依赖
  系统 apt 包。
* **FastDDS 3.6 的 rrpc API 还是 stub 状态**：DomainParticipant::create_service
  等返回 `RETCODE_UNSUPPORTED`（参见 Fast-DDS `DomainParticipantImpl.cpp`）。
  wdndds_cshim 直接用 raw `DataReader` / `DataWriter` 模拟 Request/Reply
  —— 等 FastDDS 真实现 rrpc 后可以简化。
* **disable_builtin_multicast=true** + **avoid_builtin_multicast=true**：
  cshim 在 SERVER/CLIENT 模式下禁用 multicast，单纯走 UDP 单播。
  这是 FastDDS 3.6 的官方推荐（embedded board 不保证有 multicast 路由）。


## wdndds_daemon — FastDDS Discovery Server (v0.5)

v0.5 在 v0.4 的 per-model-subprocess RPC 之上加了**集中式发现 daemon**，
绕开 FastDDS 2.9 在本机的 SPDP multicast 发现 bug（**改用 SERVER/CLIENT
协议 + UDP 单播**）。

### 架构

```
       ┌───────────────────────┐
       │   wdndds_daemon       │   唯一一个进程
       │   (FastDDS SERVER     │   默认端口：UDP 7411 / HTTP 7412
       │    discovery protocol)│   (可在 config.json 里改)
       └───────────────────────┘
                 │  (UDP 单播：peers 互相发现)
       ┌─────────┼─────────┼─────────┐
       ▼         ▼         ▼         ▼
   pyrkaiweb  worker   worker   worker     (CLIENT protocol,
     main     qwen3   qwen3      qwen8      discovery_servers 指向 daemon)
```

* 每个 LAN 跑一个 `wdndds_daemon` 进程。**没有 fallback**（多播 / SIMPLE
  在嵌入式板不可靠；server 故障 = 整个 LAN 不通）。
* 所有 pyrkaiweb 进程（main / compute_worker / im-bridge / 任意）
  通过 `config.json["compute"]["discovery_servers"]` 指向 daemon 的
  `host:port`。
* daemon 同时服务于「compute RPC 跨机器发现」和「Discovery 页在网注册」
  —— **同一个 daemon，两个用途**。

### wdndds_daemon 启动方式

**1) 主进程随 pyrkaiweb 一起启动（dev / 小部署）**

`config.json["compute"]["auto_start_daemon"]: true` 时，`app/main.py`
`create_app()` 会 `subprocess.Popen` 拉起 daemon（默认端口可改）。

**2) 手动启动（生产 / systemd）**

```bash
# 前台
.venv/bin/python -m app.services.wdndds_daemon --udp-port 7411 --http-port 7412

# 通过 systemd unit（生产推荐）
[Unit]
Description=wdndds discovery daemon
After=network-online.target

[Service]
Type=simple
ExecStart=/userdata/pyrkaiweb/.venv/bin/python -m app.services.wdndds_daemon \
    --udp-port 7411 --http-port 7412
WorkingDirectory=/userdata/pyrkaiweb
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

**3) HTTP API（前端 / 调试用）**

```
GET  /status   {"running", "started_at", "uptime_sec", "udp_port", "guid", "participant_count"}
GET  /peers    {"peers": [{"guid", "name"}, ...]}   (基于 DomainParticipantListener)
GET  /health   {"ok": true}
```

### pyrkaiweb 端配置

`config.json["compute"]` 字段：

```json
{
  "instances": [{"npu_index": 0, "model": "qwen3-vl-4b"}],
  "discovery_servers": ["127.0.0.1:7411"],   // 主进程 / worker 都读这个
  "discovery_server_port": 7411,            // 仅 auto_start_daemon 用
  "daemon_http_port": 7412,                 // 同上
  "auto_start_daemon": false                // 默认 false（生产用 systemd）
}
```

`compute_registry._persist()` **保留** `discovery_servers` / `ports` /
`auto_start_daemon` 字段（v0.5.1 修复：之前会覆盖整个 compute section）。

### 跨进程 RPC：发现 + 数据平面

* **发现**：daemon 跑 SERVER 协议，所有 client 跑 CLIENT 协议 →
  daemon 监听 UDP 7411 → 每个 client 把自己的 participant + endpoint
  注册到 daemon → daemon 把 endpoint 信息分发给所有 client。
* **数据平面**：compute_worker 用 `SHM + UDPv4 + TCPv4` 三种 transport；
  SHM 走同机零拷贝，UDPv4 走同 LAN，TCPv4 走跨子网。
* **共享 reply topic**：request / reply 两个 topic 复用 shared reply。
  请求 ID 在 ComputeRequest/ComputeReply 里匹配（参见
  `wdndds/idl/compute.idl`）。
* **首次调用 race**：daemon 刚启动时 endpoint 还没匹配，第一次
  `RpcClient.call()` 会 timeout（约 5s），第二次起成功。pyrkaiweb
  probe 自动 retry（每 1s 一次，5s timeout）。

### 注意事项

* **本机 FastDDS 3.6.2 编译 + 源码安装**（参见
  `.venv/src/wdndds/build_native.sh`）：libfastdds / libfastcdr /
  libwdndds_idl / libwdndds_cshim 全部安装到 `wdndds/native/`，不依赖
  系统 apt 包。
* **FastDDS 3.6 的 rrpc API 还是 stub 状态**：DomainParticipant::create_service
  等返回 `RETCODE_UNSUPPORTED`（参见 Fast-DDS `DomainParticipantImpl.cpp`）。
  wdndds_cshim 直接用 raw `DataReader` / `DataWriter` 模拟 Request/Reply
  —— 等 FastDDS 真实现 rrpc 后可以简化。
* **disable_builtin_multicast=true** + **avoid_builtin_multicast=true**：
  cshim 在 SERVER/CLIENT 模式下禁用 multicast，单纯走 UDP 单播。
  这是 FastDDS 3.6 的官方推荐（embedded board 不保证有 multicast 路由）。

