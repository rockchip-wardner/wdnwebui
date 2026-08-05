# AGENTS.md

Compact guide for OpenCode sessions in this repo.

## What this repo is

Python web service (`pyrkaiweb`) exposing RK-platform hardware / model SDKs
(`rkv`, `rkai`) over HTTP via FastAPI. Brand-new repo (no commits yet at
creation); everything below is verified against the current tree.

## Stack at a glance

- **Web framework**: FastAPI + Uvicorn (`run.py` boots `app.main:app` on
  `0.0.0.0:8000` with `reload=True`).
- **Internal SDKs** (installed editable from private SSH git, **not** PyPI):
  - `rkv` — Rockchip MPP / RGA2 / V4L2 video bindings.
    Native libs (`librockchip_mpp`, `librga`) must be installed on the host;
    `import rkv` only — `import rkv.video` is what touches the hardware.
  - `rkai` — RK-chip LLM / VLM SDK (transformers + opencv-python).
  See `.venv/src/rkv/AGENTS.md` and `.venv/src/rkai/pyproject.toml` for the
  SDK-side gotchas (they're external code, not in this repo, but the
  constraints propagate here).

## 设计原则

1. 必须通过rkai的LLMSession调用rknn，不得直接调用
2. 必须通过rkv调用mpp和rga，做编解码和图片缩放、格式转换等操作，不得直接调用或者通过其他库进行软编解和格式转换、resize等操作。
3. 必须通过wdiot做发现和注册服务。
4. **LLMSession 必须从 `ComputeRegistry` 取，禁止各业务模块自建**。视频检测
   / 语音生成 / 视频监控等服务只能通过
   `get_registry().get_session("<capability>")` 拿到 session；
   找不到对应 capability 就抛 `RuntimeError("功能未启用: <capability>")`，
   由调用方决定如何向上层报告（视频监控选择静默 error 返回，不阻塞 16 路
   解码；视频检测 / 语音生成选择 raise，由 HTTP 层 502 透传给前端）。
实例集合在 算力配置 页配置，存到 `config.json["compute"]["instances"]`，
    `run.py` 启动时后台线程串行加载，不阻塞 UI。
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
.venv/bin/pip install -e "git+ssh://git@10.10.10.10/pythonsdk/pyrkv.git#egg=rkv"
.venv/bin/pip install -e "git+ssh://git@10.10.10.10/pythonsdk/pyrkai.git#egg=rkai"
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
│   │   ├── detector.py          # rkai VLM wrapper + worker thread
│   │   └── uploads/             # local-file upload staging (gitignored)
│   └── static/
│       ├── index.html           # single-page UI
│       ├── css/style.css
│       └── js/app.js            # WebSocket + Canvas + event UI
├── requirements.txt      # web deps + opencv-python-headless + python-multipart
├── .gitignore            # excludes .venv/, __pycache__/, .env, uploads/, etc.
└── .venv/                # editable installs of rkv, rkai live under .venv/src/
```

`app/__init__.py` owns the single version string; bump it there when the
service version changes.

## Video detection page

The `#page-video-detection` section is wired to:

- **Backend** (`app/api/video_detection.py`):
  - `POST /api/video-detection/upload` — multipart file upload; saves to
    `app/services/uploads/<uuid><ext>` and returns `{upload_id, path, …}`.
  - `POST /api/video-detection/open` `{source: str}` — opens via
    `rkv.VideoCapture`, starts decode thread, returns snapshot.
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
  - One `VideoSession` singleton, one `rkv.VideoCapture`, one decode
    `Thread`. Latest-frame-only queue (`maxsize=1`) — drops oldest on
    overflow to keep latency low.
  - Frame sinks: registered callbacks are called on each decoded BGR
    frame (used by the detector). Sinks must not raise.
  - Lazy `from rkv import VideoCapture` at module top (still behind the
    `try/except` from `rkv/__init__.py`); never imports `rkv.video`
    explicitly.
- **Service** (`app/services/detector.py`):
  - `RkaiDetector` singleton. Wraps `rkai.LLMSession` with
    `SystemLanguageModel.qwen3_vl_4b()` (configurable via
    `RKAI_MODELS_DIR`).
  - **rkai is imported lazily** at `start()` time — not at module load.
    Loading the RKNN native libs eagerly at app startup caused a
    segfault on this dev box (RKNN3 init races with rkv's mpp
    bring-up). Don't move the import back to module top.
  - One daemon worker thread consumes a `queue.Queue(maxsize=1)`
    (latest-only); the decode-loop sink drops whatever's pending and
    replaces it with the freshest frame so the detector always sees
    the most recent state.
  - Each inference calls `session.reset()` (clears history + KV cache)
    and `session.respond(prompt=prompt, attachment=[Attachment(frame)])` — free-text
    response, no schema. ~1s after model warm-up.
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
`rkv.VideoCapture` per thread, one `rkv.VideoWriter` per thread
(zero-copy — the encoder consumes the decoder's DMA-BUF via
`rkv+ws://`). The VLM interpreter is **shared across all 16 channels**:
the ``SurveillanceManager`` owns ONE ``rkai.LLMSession`` (the qwen3-vl-4b
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
    rkv.VideoCapture.read0(detect_w, detect_h, fmt="RGB")
        |
        v
    _submit_vlm(...)                  # non-blocking: enqueue on the
        |                              # shared ``LLMSession`` queue
        v
    SurveillanceManager._InferenceWorker (single daemon thread, persistent asyncio loop)
        |   session.reset()  +  await session.respond(prompt, attachment=[Attachment(frame)])
        |   (always goes through rkai — never direct rknn3lite / rknntoolkit)
        v
    Future resolves; channel._on_inference_done records the event async
        |
        v
    rkv.VideoWriter("rkv+ws://127.0.0.1:8000/api/surveillance/<id>/ingest", codec="h264")
        |   (zero-copy: writer.write(frame0) hands the decoder's
        |    DMA-BUF to MPP's encoder)
        v
    /api/surveillance/<id>/ingest  (server-side WS, rkv encoder connects as client)
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
  - rkai is imported lazily (``_load_rkai()``) inside the inference
    worker — the same module-level cache as
    :mod:`app.services.detector` so the rkai import only happens once
    across the whole process. rkv is imported lazily for the same
    reason (non-Rockchip dev boxes still boot).
  - Packets use the rkv wire format
    ``<type_byte:u8><pts_us:u64 LE><payload>`` with
    ``0x00 DELTA | 0x01 KEY | 0x02 HEADER (avcC) | 0xFF EOS`` (matches
    ``rkv.encoder._WebSocketSink`` and ``app/static/js/app.js``'s
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
  - ``WS     /api/surveillance/{id}/ingest``  — server-side ingest; the rkv
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
  ``VideoCapture`` and the ``VideoWriter`` to ``rkv+ws://...``)
  and ``detection_enabled`` (gates the per-frame submission to the
  shared VLM worker). Playback can run without detection, so you
  can monitor 16 streams without ever touching the NPU. Toggling
  detection alone doesn't restart the decode thread — it's only
  consulted inside the loop.
* **Resource budget** — the manager owns ONE shared ``rkai.LLMSession``
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
- **`RKAI_MODELS_DIR` propagation** — `app/main.py` reads
  `base.model_path` at `create_app()` time and writes it into
  `os.environ["RKAI_MODELS_DIR"]` before any `LLMSession` is built.
  rkai's `_models_root()` reads that env var on first call, so the
  SDK picks up the configured root automatically. Changing the
  field via the UI after the app has booted only takes effect on
  the next uvicorn reload (i.e. after a restart in production) —
  an already-loaded `LLMSession` was bound to the old path.
- **Frontend modules** — `ConfigMenu` (popover), `Base` (form page);
  `PowerMenu` was untouched and is mutually exclusive with
  `ConfigMenu` via the `_wrapOpenToCloseOther` helper.
- **算力配置 (`ComputeConfigPage`) + 算力注册中心 (`ComputeRegistry`)**
  —— 用户在算力配置页启用/禁用实例；每条实例绑定到一张 NPU 卡（由
  `RKNN3Lite().get_devices_id()` 枚举）+ 一个 model preset。同一 NPU 上
  多个不同 model 允许（按内存预算自我约束），但同 NPU 重复同 model 拒绝
  （pydantic 校验 + 注册中心运行时校验双层防护）。内存估算硬编码：
  Qwen3-VL-4B ≈ 3500 MB / Qwen3-TTS ≈ 1900 MB / Qwen3-ASR ≈ 2500 MB。
  启动 **不阻塞 UI**：`apply_boot_config_async()` 起一个 daemon thread
  串行加载；UI 通过 `GET /api/compute` 每秒轮询状态，queued / loading
  实例显示预估倒计时（`ESTIMATED_INIT_SECONDS_PER_MODEL = 8s`）。
  Capability 概念作为内部 key 使用（`get_session("swcap_video_describe")` /
  `"swcap_audio_tts"`），**这一轮不做** wdiot 真实注册发现；discovery
  页面只读 registry 内存状态。

## Discovery page (注册发现)

`#page-discovery` 内部按从上到下三个 pane 排布：

1. **局域网设备 (LAN)** —— 新增，详见下文。
2. **硬件能力 (HW)** —— 本机硬件能力开关，源自 `HARDWARE_CAPABILITIES` 静态表 +
   `config.json["compute"]["hardware_capabilities"]` 启用状态。checkbox 修改后
   点"保存修改"才落盘，避免误触。
3. **软件能力 (SW)** —— 当前 running 实例暴露的能力（每条来自一个 RKNN 模型实例）。

### 局域网设备 (LAN) 子面板

LAN 子面板（`data-pane="lan"`）做同网段 pyrkaiweb 互相识别：

* **开关 "在网注册"** —— 开启后：
  * 后台线程（`lan-discovery-worker` daemon）周期性扫描本机所有 IPv4 私有 /24 子网；
  * 对每个候选 host 做 TCP connect（端口 = `config.port`，默认 8000）+ HTTP
    `GET /api/lan-discovery/announce`；
  * 响应成功的设备写入内存表：`(ip, port) → LanDevice(device_name,
    sw_capabilities, hw_capabilities, last_seen, last_rtt_ms, last_error)`；
  * TTL 5 分钟 —— 连续 5 分钟没再扫到自动剔除，避免僵尸节点堆积。
* **`PUT /api/lan-discovery`** `{enabled, port, scan_interval_seconds}`：
  * `enabled` 一旦切到 `true`，内存里的旧设备列表会被清空，并立刻触发首轮扫描；
  * 切到 `false` 后停止扫描，但内存里的设备保留供用户回顾；
  * `port` 默认 8000，`scan_interval_seconds` 范围 5–600（默认 30）。
* **`POST /api/lan-discovery/scan`** —— "立即扫描"按钮：不等 interval 到点，
  立刻唤醒 worker；即使 `enabled=false` 也允许手动跑一次（用于临时探测）。
* **`DELETE /api/lan-discovery/devices`** —— "清空列表"按钮，仅清内存，不动
  enabled 开关与 config.json。
* **`GET /api/lan-discovery/announce`** —— **永远可用**的对端探测接口，跟
  `enabled` 无关。对端 pyrkaiweb 扫描时打这个 URL，本机返回
  `{device_name, ip, sw_capabilities, hw_capabilities, timestamp,
  protocol_version}`。device_name 来自 `config.json["base"]["device_name"]`，
  capability 来自 `ComputeRegistry.list_active_capabilities()` +
  `list_hardware_capabilities()`。
* **`GET /api/lan-discovery`** —— Discovery 页每秒轮询的完整 snapshot：
  `config / enabled / scanning / last_scan_started / last_scan_finished /
  local_ips / subnets / hosts_per_subnet / devices[] / ttl_seconds /
  probe_timeout_seconds / default_port`。
* **持久化** —— `enabled / port / scan_interval_seconds` 写到
  `config.json["compute"]["lan_discovery"]`，重启后保留 `enabled` 状态但**不会**
  自动启动扫描线程（与 compute instances 一样，遵循
  "boot 不自动加载"原则 —— 用户必须手动打开开关）。
* **实现细节**：
  * 子网枚举走 `psutil.net_if_addrs()`（已有依赖），过滤 loopback / link-local /
    公网 IP；同一 /24 出现多次合并；本机 IP 自身从候选列表剔除。
  * 并行探测用 `ThreadPoolExecutor(max_workers=64)`，单 host 超时 0.7s；
    一轮 /24（253 主机）≈ 2.8s。
  * TCP connect 失败直接返回（不浪费 HTTP 超时）；成功才发 HTTP。
  * 失败的探测**不更新** `last_seen` —— 设备掉线后自然在 TTL 到期后从表里
    剔除；`last_error` 字段保留最近一次的失败原因供 UI 展示。

### Discovery 页前端模块拆分

`app/static/js/app.js` 里 Discovery 相关代码分两个独立模块：

* **`Discovery`** —— 注册发现页主入口。`load()` 拉 `/api/compute/capabilities`
  渲染 HW + SW pane（一次拉取，不轮询）；HW pane 的 checkbox 修改是 dirty
  状态，点"保存修改"才 PUT 到 `/api/compute/hardware-capabilities`。
* **`DiscoveryLan`** —— LAN 子面板独立模块。`load()` 拉
  `/api/lan-discovery`；进入页面后 `startTicking()` 起 1Hz 轮询，让
  `scanning / last_seen / devices` 实时刷新；离开页面 `stopTicking()`
  释放定时器。
  * `onToggle()` 处理"在网注册"开关：PUT 时设 `_toggleInflight=true`
    防止 1Hz 轮询在 PUT 完成前把乐观 UI 覆盖回旧值；失败回滚 checkbox。
  * `scanNow()` / `clear()` 触发对应 POST / DELETE，失败写
    `discovery-lan-summary` 红字提示。
  * `render()` 把 snapshot 渲成 4 段 UI：toggle 状态 + hint（在网/总设备数）
    + summary（扫描中 / 已发现 / 未启用）+ meta（本机 IP / 端口 / 间隔 / 子网）
    + device 卡片（device_name + ip:port + 在线/离线 + RTT + 最近探测 +
    SW/HW capability chips）。

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
  live there. Editing rkv/rkai source via Uvicorn reload is fine; the
  installed metadata is in `.venv/lib/python3.11/site-packages/`.
- **`run.py` defaults to `reload=True` and port 8000** — fine for dev, but
  disable reload (`uvicorn.run(..., reload=False)`) before any production
  command, or use `uvicorn app.main:app` directly.
- **rkv hardware path requires a Rockchip target.** If `import rkv.video`
  raises on this machine, it's expected — the SDK falls back gracefully for
  bare `import rkv`. Don't suppress the fallback.
- **Imports should stay lazy** for `rkv.video` / `rkai.*` so a non-target
  dev box can still boot the web shell.
- **`RkaiDetector._load_rkai()` must stay lazy**. The module-top import of
  `rkai.Attachment / LLMSession / SystemLanguageModel` will pull in
  RKNN3 native libs at app startup, racing with rkv's mpp init and
  segfaulting on this dev box. Keep the import behind `_load_rkai()`,
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
- **`ComputeRegistry` 启动顺序**：`app.main.create_app()` 必须先
  `include_router(compute.router)`，**再**（可选）调
  `get_registry().apply_boot_config_async()`，**再**注册 detector 的
  frame sink。
  
  **当前 boot 不自动加载**：`apply_boot_config_async()` 在 `app/main.py`
  里被注释掉。ComputeRegistry worker 线程在第一次 `POST /api/compute/instances`
  时由 `start_one` → `_wake_worker` → `_ensure_worker` 按需起 daemon thread。
  config.json["compute"]["instances"] 里的条目仍会被
  `POST /api/compute/instances` / `start_one` 写到 `inst.model`，
  重启后配置保留，但**不会**自动启动 —— 用户必须从算力配置页手动启用。
  Detector 等消费方在第一次 inference 时才调
  `get_registry().get_session("swcap_video_describe")`—— registry 还是空就会抛
  "功能未启用"，这是预期行为（不是 bug）。
- **`apply_boot_config_async` 的后台线程只在 reload 模式下被杀掉**：
  uvicorn `reload=True` 重启父进程时 daemon thread 跟着挂掉；
  production（`reload=False`）下要靠 `create_app` 的
  `on_event("shutdown")` 调 `get_registry().close_all()`，会 join
  worker thread（最长 2s）。正在 loading 的模型被中断后会留在
  `status='error'`，下次启动时由 `apply_boot_config_async` 重新尝试。

## TTS (语音生成) page

The `#page-tts` section drives Qwen3-TTS through the SDK's
``TTSBackend`` (defined under
``.venv/src/rkai/src/rkai/backends/tts.py`). The full RKNN3 driver
(four ``.1828.rknn`` models, embed tables, prefill construction,
talker/code_predictor/speech_decoder pipeline, ``AudioPlayer``
streaming) is inlined into the same file as ``Qwen3TTSDemo`` —
vendored in-tree so the SDK is self-contained and no longer has to
find ``qwen3_tts_demo.py`` anywhere on the host filesystem. The
model layout, .1828.rknn naming, and AudioPlayer streaming path
are all documented in
``.venv/src/rkai/src/rkai/models/README.md`` and
``.venv/src/rkai/src/rkai/backends/tts.py``.

* **Service** (`app/services/tts_session.py`):
  - ``TtsSession`` singleton, lazy-imports rkai at first use so a
    non-RK dev box still boots (just reports ``state="unavailable"``).
  - State machine mirrors ``VideoSession``/``RkaiDetector``:
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
  ``_BUNDLED_SPEAKER_EMBEDDINGS_B64`` (``.venv/src/rkai/src/rkai/backends/tts.py:209``)
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
  - **rkai 导入懒加载** —— 模块顶层只 import `os / queue / re / threading / asyncio`；
    `_install_patches()` 在第一次构造实例时才 import `rkai.backends._common`。
  - **流式回调 = monkey-patch**：模块加载时把
    `rkai.backends._common.make_result_callback` 替换成包装版。包装版
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

`app/services/tools/` 装的是给音视频检测页 (RkaiDetector) 用的
rkai ``@Callable`` 工具集合，**不**进入视频监控 (16 路) — 那条
通路有自己的 surveillance manager，工具语义不同，这一轮不动。

* **三个内置工具**：
  - ``crop_region`` — 目标剪裁：模型给出 0–1000 归一化 bbox，
    后端按 bbox 切出 BGR，JPEG 编码（quality 85）后 **base64
    内嵌到事件 JSON 的 ``crop_b64`` / ``crop_w`` / ``crop_h`` 字段**，不
    落盘、不二次送 VLM 推理。前端在画布上叠红框（5 秒淡出），
    同时在右下角弹一个浮动窗展示剪裁图（5 秒淡出），事件历史
    列表里挂小缩略图。无独立 capability 门控（detector 启动时
    的 ``swcap_video_describe`` 校验已兜底，工具本身不重复卡）。
  - ``annotate_roi`` — 目标标注：模型给出 bbox，前端在 canvas
    上叠红色矩形（5 秒淡出）。无 capability 依赖。
  - ``tts_alert`` — 语音提醒：模型给出 ``text`` / ``language``
    / ``ref_speaker``，``TtsSession.start`` 触发本机播放。
    能力门控 ``requires_capability="swcap_audio_tts"``。
* **页面归属**：
  - ``crop_region`` + ``annotate_roi`` 只在 **视频目标检测页**
    暴露（``ALLOWED_TOOLS = ("annotate_roi", "crop_region")`` 在
    ``app/api/video_target_detection.py``）。
  - ``tts_alert`` 只在 **音视频描述页** 暴露
    （``ALLOWED_TOOLS = ("tts_alert",)`` 在
    ``app/api/video_detection.py``）。
  - 算力配置页 RK3588 列则只读展示全部三个工具的状态。
* **能力门控（双层防护）**：
  - UI：``GET /api/video-detection/tools`` /
    ``GET /api/video-target-detection/tools`` 列表里每条带
    ``available`` / ``reason``；不可用时 checkbox 置灰、显示
    原因。
  - 后端：``RkaiDetector.start()`` 校验每个 ``enabled_tools``
    的 capability (``get_registry().get_session(spec.requires_capability)``)，
    未启用抛 503。
* **会话级，不持久化**：用户在检测页勾选的 tools 集合只
  存在内存里 (``RkaiDetector._enabled_tools``)；重启 / 关闭页
  面 / 停止检测都清空，不写 ``config.json``。``crop_region``
  推的 ``crop_b64`` 同样在 ``DetectorEvent`` deque 里活，刷新
  页面 / 重启就丢；不写盘、不另开缓存。
* **bbox 渲染**：后端把 bbox 通过 ``DetectorEvent.bbox`` 字段推到
  WS（事件类型仍是 ``type:"event"``，仅追加字段），前端在
  ``#vtd-canvas`` 之上叠 ``<svg viewBox="0 0 1000 1000">``，
  ``rect`` 5 秒后淡出。``crop_region`` 复用同一渲染路径（再
  加 ``showVtdCropPopup`` 浮动窗 + 历史缩略图）。
* **工具列表已并入算力配置页 RK3588 列**：原 ``#page-tools`` 页面与
  ``GET /api/tools`` 端点都已移除；工具元数据现通过
  ``GET /api/compute`` snapshot 的 ``tools`` 字段返回（懒导入
  ``app.services.tools``，避免在 compute 模块加载时触发 rkai 装饰器）。
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
   * 4 个模型文件（rknn / weight / embed / tokenizer）齐全且非空
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
但调用入口是 `.venv/src/wdiot/src/wdiot/wechat.py` 里 `wdiot.Wechat` 的
**同步门面**（自带 daemon-thread asyncio loop），不是直接 `await` SDK。

* **服务** (`app/services/wechat.py`)：
  - 单例 `_WechatManager`（lazy，``get_wechat()``），内部持一个 `wdiot.Wechat`。
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
  wdiot.Wechat._qr_url set, login_state=awaiting_scan
      ↓ 1Hz 轮询 + 回调即推
  _WechatManager._push_state_diff → WS broadcast {type:"state", state:{login_state:..., qr_url:...}}
      ↓ 前端 _renderQr() 用 qrcode-generator 把 URL 渲染成 <svg>
  [用户在手机点确认]
      ↓ SDK 回调 on_scanned → on_confirmed → bot.start() 自动起 long-poll
  login_state=confirmed + started=True
      ↓ [用户A 发消息]
  wechatbot bot._dispatch(msg) → wdiot._on_incoming → _WechatManager._handle_incoming
      ↓ _push_state_diff → WS broadcast {type:"message", message:{direction:"in", ...}}
      ↓ 前端 _recordMessage + _renderChat → 追加到列表
  [用户点发送]
      ↓ POST /api/wechat/send
  wdiot.Wechat.send_text → wechatbot.bot.send(user_id, text)
      ↓ SDK _send_content + _send_text
      ↓ wdiot._record_outbound → _push_state_diff → WS broadcast {type:"message", direction:"out"}
  ```

* **凭证路径** = `~/.wechatbot/credentials.json`（`wdiot.wechat.DEFAULT_CRED_PATH`），
  也可设 `WDIOT_WECHAT_CRED_PATH` 环境变量覆盖。这是 SDK 默认路径，
  多 pyrkaiweb 实例不会冲突。

* **资源预算** —— `wdiot.Wechat` 单实例长期驻留一个 daemon thread + 一个
  aiohttp 连接池；未登录时 SDK 不会建任何连接（`bot.login()` 之前
  `_api` 只在 `poll_qr_status` 时拉 HTTP）。登录失败 / QR 过期 → SDK
  内部走 `MAX_QR_REFRESH_COUNT=3` 轮后抛 `AuthError`，wrapper 把
  `login_state` 切到 `error`，UI 看到红色徽章 + 点 登录 重试。

