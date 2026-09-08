# AGENTS.md

Compact guide for OpenCode sessions in this repo.

## What this repo is

Python web service (`wdnwebui`) exposing RK-platform hardware / model SDKs
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
实例集合在 算力配置 页配置，存到 `config.jsonc["compute"]["instances"]`，
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

## 修改守则

改动某个功能的时候，不影响其它功能；如果需要改动到公共功能，需要自动
跑真实测试每个关联的功能项是否都正常。

- 改动只触及目标功能及其直接依赖，不顺手 refactor 无关代码。
- 公共功能改动（`app/services/compute_registry.py`、LLM 会话栈、
  `app/services/detector.py` / `video_session.py` / `surveillance.py`
  等共享 service、`wdndds` cshim、`wdnai._librknn` shim 等被多个
  调用方共享的模块）必须跑所有关联功能的真实测试（不止单元测试），
  不允许"测试跳过"或"代码改对了就行"。
- 失败 → 修到全通过再交；提交前在 PR/commit message 列出跑了哪些
  feature 的 smoke test + 结果。

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

* **VLM 图像传输：raw BGR via `/dev/shm` path-list**：
  `RpcSessionProxy._encode_attachments()` 把每个
  `Attachment.images` / `Attachment.frames` BGR ndarray 走
  `_bgr_ndarray_to_path()`：
  1. 主进程 `arr.tofile()` 写到 `/dev/shm/wdnwebui_vd_<uuid>.bin`
     （tmpfs RAM，板上 4 GB 充裕）
  2. wire 上只传 `{"data_path": ..., "shape": [h,w,3], "dtype": "uint8"}`
     （~150 字节，远小于 FastDDS builtin per-message 限额）
  3. worker `np.fromfile().reshape()` 还原 byte-exact 帧，立刻 `os.unlink()`
  4. boot + atexit 各扫一次 `/dev/shm/wdnwebui_vd_*.bin` 防泄漏
     （boot sweep 默认 `max_age=1h`，atexit 用 `max_age=0` 全清）

  为什么走 path-list 而不是 base64 inline（之前是 JPEG q=85 + base64）：
  * **无损**：480×640 单帧 max 像素差从 JPEG q=85 的 96/255 → 0。
    VLM 4B 类模型对像素抖动敏感，JPEG 块状 artifact 在 1080p + 768
    variant 上让模型误判 / 输出退化（实测报"泰文 / 乱码"）。
  * **LAN peer 兜底**：内置 FastDDS transport（`use_builtin_transports=true`，
    见 `cshim_src/wdndds_cshim.cpp:312`）自动 SHM（同机）+ UDPv4/TCPv4（跨机）。
    跨机时 proxy 收 `ModelInstance._source == "adopted"` 自动设
    `is_remote=True` → 切回 `_bgr_ndarray_to_b64`（JPEG+base64）。
    path-list 在 caller 自己的 /dev/shm 上才能被同机 worker 读，
    LAN peer 拿不到 → 自动 fallback。
  * **wire 体积 + DDS 开销**：wire 不带图本身，每帧省 6.2 MB DDS
    serialize / 反序列化（builtin TCPv4 兜底虽能塞但每帧一遍不便宜）。

  用途覆盖：`WdnaiDetector`（音视频描述 / 视频目标检测）+ 视频文件分析
  共用同一 `RpcSessionProxy`，都走 path-list。视频内容检索（embed_video）
  走自己的 `wdndds_vcs_*` path-list（基于 JPEG，嵌入模型对像素抖动不敏感）。
  音频走 base64（PCM bytes 不是图）。
  串行化同一 instance 的并发请求。
* **`boot_compute_services` 在 boot 默认开启**。`app/main.py`
  `create_app` 把整个调用挪到 daemon thread
  (``threading.Thread(target=..., name="compute-boot", daemon=True).start()``)，
  防止冷启动时 ``_probe_service`` 的 3 × 5s race-recovery timeout
  把 ``create_app()`` 阻塞几十秒、uvicorn bind 8000 等不到 —— 浏览器
  GET / 立即出，probe/adopt/spawn 在后台慢慢跑（前端 1Hz 轮询
  ``/api/compute`` 自动反映）。函数本体在 v0.10 重写：以
  **DDS RPC `info` 探测 + `/proc` 先行** 为真源，对
  ``MODEL_PRESETS`` 全集做四步走——
  1. **`_find_workers_by_name(model_id)`** 先扫 ``/proc/*/comm``
     看本机有没有 ``worker:<alias>`` comm 的进程；
     **未命中 → 跳过 probe**（避免冷启动 5 × 17s ≈ 85s 的纯
     timeout 等待），按 config 走 spawn / skip；
     **命中 → 走 probe + adopt**（拿 hostname / npu / status）。
  2. 探测 + adopt：``_adopt_existing_worker`` 内部走
     ``_probe_service`` 的 3 × 5s race-recovery 路径，只对真的有
     活 worker 的 model 才会被探测到。
  3. 本机无 worker + config 里有 → ``_spawn_compute_worker``；
     本机无 worker + config 里没 → skip（UI ``unavailable``）。
  4. 本机有 worker + probe 失败 + config 里有 → fall through
     ``_spawn_compute_worker``，它的 dedup 拒绝 spawn sibling 并
     登 errored inst（避免 NPU 显存吃双份）。
  **LAN peer 不在 `/proc` 里** —— boot 不主动探测远端 worker；
  兜底走运行时首次请求 ``_ensure_worker_for_model`` → probe → 若
  peer 响应则 adopt；否则本机 spawn。单板部署无影响，多板 LAN 的
  peer boot-time discover 退化到 runtime discover。worker 起来后
  只注册 DDS endpoint，不 build LLMSession；npu_index 由用户在算力
  配置页选 NPU 后通过 RPC `{"action":"load","npu_index":N}` 传入。
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
  等 wdnai SDK 改 spec + wdnwebui 解析 `npu_assignments: dict[str, int]`
  —— 当前每个 model 一个实例，所以 service_name = `wdndds/rpc
`     <model_id>`（**不带 npu 后缀**），单 model 多 npu 也在子进程
  内部决策。
* **同 model 不允许重复实例**：`compute_registry.start_one` 校验
  `(npu_index, model_id)` 不重复；UI  已经在加「instance_name」
  之前按 (npu, model) 唯一。

### 内存模型：主进程 RSS 稳态 ~300 MB（修复后）

主进程 RSS 由三部分组成（`/proc/PID/smaps_rollup` 看）：

```
RSS = python_heap (~140 MB)
    + shared_libs  (~50 MB, fastdds.so/cv2/libmali/...)
    + anon rw      (~200 MB, FastDDS TopicPayloadPool + glibc arena)
    + shm files    (~12 MB, /dev/shm/fastdds_*)
```

**anon rw 段是 FastDDS 内部 TopicPayloadPool**（不是 Python 对象），
每个 `DataWriter`/`DataReader` 创建时按
`max_serialized_type_size × allocated_samples` 预分配 payload pool，
delete entity 后池不释放（topic 留在 `Participant::req_topics` map 里，
要等 `wdndds_participant_destroy()` 才整批 free）。

**两层防御把稳态 RSS 从 ~1.35 GB 压到 ~300 MB**：

1. **Service 生命周期**（`compute_registry.stop_instance_local`）—
   `hub.destroy_service(name)` 把 Service 从 `hub._services` 删掉，
   触发 `Service.destroy()` 清掉所有 repliers/requesters + C 侧
   `wdndds_service_destroy`。修复前每次 enable→disable 循环 +80 MB
   主进程 RSS。
2. **resource_limits 上限封顶**（`cshim_src/wdndds_cshim.cpp:213`
   `apply_bounded_resource_limits`）— 每个 writer/Reader 创建时设
   `allocated_samples=8, max_samples=16`（reliable）/
   `4/8`（best_effort），把单 entity pool 从 ~63 MB 压到 ~18 MB。

**剩余 ~200 MB anon rw 不能压**：
* ~265 MB 单段（malloc arena，没回 OS）
* 18 MB × N entities（bounded pool 自身）
* 65 MB + 35 MB × few（FastDDS discovery builtin endpoint cache，
  改不了）

**verifier**：连续 15+ 次 restart cycle 后 RSS 应稳定在 ±50 MB 范围内。
若 RSS 单调上涨，说明 Service 没被 destroy（看 `hub._services` 大小
或 `_instances` 的 `inst._svc` 是不是 None）；若 RSS 上限变高，说明
QoS 上限被改宽（看 `apply_bounded_resource_limits` 调用点）。

## TL;DR commands

```bash
# activate (do NOT use system pip — Debian PEP 668 blocks it)
source .venv/bin/activate

# one-time wdndds native libs (apt + stub + pip) — idempotent, run again is <1s no-op
sudo ./bootstrap_wdndds_native.sh

# run (auto-cleans stale uvicorn / compute_workers from previous unclean shutdowns)
sudo .venv/bin/python run.py             # 0.0.0.0:8000, reload on
sudo .venv/bin/python run.py --no-kill-stale   # opt out of stale cleanup

# sanity
curl http://127.0.0.1:8000/health
```

Endpoints wired so far: `GET /` (version), `GET /health`.

**Stale-process cleanup on startup.** `run.py` reaps leftover
**MAIN** processes from a previous unclean shutdown before binding
port 8000: uvicorn worker holding port 8000 (via `ss -tlnp`), the
`python run.py` ancestor itself, and uvicorn-reloader's
`multiprocessing.spawn` / `resource_tracker` children (defunct
zombies that inherited the LISTEN socket). SIGTERM with 3s grace,
then SIGKILL.

**Orphan `compute_worker.py` subprocesses are intentionally NOT
killed** — ComputeRegistry spawns them with
`Popen(start_new_session=True)` so they detach from the run.py
process group and survive it, and the new main process adopts them
on the next boot via `boot_compute_services()` →
`_adopt_existing_worker()` (DDS RPC `info` probe). Killing them
would force a 60s+ model reload on every restart. Detected orphans
are reported on stderr ("detected N orphan compute_worker
process(es) — preserved, will be adopted") so the user has
visibility.

Enabled by default; opt out via `--no-kill-stale` CLI flag or
`WDNWEBUI_KILL_STALE_ON_BOOT=0` env var. When disabled, both the
MAIN-process kill and the orphan-worker log are skipped (single
gate). Per `install_autostart.sh`, the production systemd unit
invokes `uvicorn` directly (not `run.py`), so the kill-stale
behaviour never runs under systemd.

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

**wdndds also needs native libs built before `run.py` will boot.** Unlike
wdnav/wdnai, `pip install wdndds` does not ship pre-built `.so` files —
the `wdndds/_build.py` `build_py` hook is supposed to compile
`libwdndds_cshim.so` + `libwdndds_idl.so` from `cshim_src/` + `idl/`
into `src/wdndds/dds/_native/` at install time. The hook needs a
FastDDS 3.6 dev install with `libfoonathan_memory.so.0.7.2` reachable.

`run.py` ships a fail-fast preflight (`_ensure_wdndds_built`) that
checks for the two `.so` files at module-import time and `SystemExit(1)`
with a pointer to `bootstrap_wdndds_native.sh` if they're missing —
the web service refuses to come up rather than silently failing in
the daemon child process. Bypass with
`WDNWEBUI_SKIP_WDNDDS_BOOTSTRAP=1` for production images that
pre-stage the .so files out-of-band.

* Symptom when missing — `run.py` aborts with
  `wdndds native libraries not built. Run once on this host:
  sudo ./bootstrap_wdndds_native.sh`. Quick check:

  ```bash
  .venv/bin/python -c "from wdndds.dds._cshim import check_runtime; print(check_runtime())"
  # expect (True, None)
  ```

* `bootstrap_wdndds_native.sh` is the recommended one-shot setup.
  It installs the apt build deps (`libfoonathan-memory-dev`,
  `libfoonathan-memory0.7.2`, `patchelf`), writes a stub for
  `native/bin/nodesize_dbg` (partial native-install workaround for
  foonathan's cmake IMPORTED-target existence check), and runs
  `pip install --force-reinstall --no-deps .venv/src/wdndds` to
  trigger `_build.py` end-to-end. The script refuses to fall back to
  `apt install libfastrtps-dev` — that's FastDDS 2.x and incompatible
  with the v0.5 cshim ABI.

* `native/lib/libfastdds.so.3.6.2.0` + `libfastcdr.so.2.3.6` already
  vendored (typical partial build) — the bootstrap script handles
  this case automatically. Manual equivalent:

  ```bash
  sudo apt install libfoonathan-memory-dev libfoonathan-memory0.7.2 patchelf
  sudo .venv/bin/pip install --force-reinstall --no-deps .venv/src/wdndds
  ls .venv/src/wdndds/src/wdndds/dds/_native/libwdndds_*.so
  ```

  Note: `pip install -e ...` (editable) silently swallows `_build.py`
  hook failures on setuptools ≥64 — so always run the bootstrap
  script (or the manual non-editable `pip install` form) when
  rebuilding. The `WDNDDS_FASTDDS_PREFIX` env var (read by
  `_build.py`) is auto-detected from `native/`, no need to set it.

* **v0.6.1 unconditional rebuild** — both `_build.py::build_py.run()`
  (`.venv/src/wdndds/src/wdndds/_build.py`) and
  `bootstrap_wdndds_native.sh` now unconditionally rebuild on every
  invocation, no fast-path skip. The hook `unlink()`s the two
  `libwdndds_*.so` files at the start of every `pip install` /
  `pip install -e`; bootstrap no longer exits 0 when the `.so` files
  exist. Cost is the cmake build itself (~20s on this dev box).

  **Why**: the previous "if both .so files exist, skip pip" fast path
  in bootstrap was a silent correctness trap. After `git pull` of
  wdndds source that added new bindings to `_cshim.py` (commit
  `871d00f feat(wdndds): 新增 create_topic_writer/reader 流式分片助手`
  — added `wdndds_topic_writer_create` / `wdndds_topic_reader_create`
  + friends), the pre-existing `.so` would still satisfy the
  existence check, bootstrap would exit 0, and the user would later
  hit
  `AttributeError: .../libwdndds_cshim.so: undefined symbol: wdndds_topic_writer_create`
  when `ComputeRegistry._ensure_hub()` tried to instantiate
  `wdndds.dds._rpc.Hub`. End-user symptom was every compute card on
  the 算力配置 page failing with
  `RpcClient: wdndds Hub not available (fastdds init failed)`.

  **Workaround if you want the old behaviour back** — for production
  images that pre-stage the `.so` files out-of-band and don't want to
  pay the ~20s rebuild, gate the bootstrap with
  `WDNWEBUI_SKIP_WDNDDS_BOOTSTRAP=1` (see `run.py`'s
  `_ensure_wdndds_built`). Don't try to bypass bootstrap from inside
  the script itself; the hook in `_build.py` is always invoked by
  `pip install` and there's no environment knob to skip just the
  build step without bypassing the whole bootstrap.

* `native/` empty (fresh clone after `git clean -fd`, or new board) —
  run `bash .venv/src/wdndds/build_native.sh` first (20-30 min,
  builds FastDDS 3.6 + FastCDR 2.3 from source), then the bootstrap
  script.

The wdndds-owned `.so` files (`libwdndds_cshim.so`,
`libwdndds_idl.so`) are **never in git** — gitignored at both
`native/lib/` and `src/wdndds/dds/_native/`, built fresh per install.
A `git clean -fd` that wipes `native/lib/` is recoverable via the
bootstrap script; a `git clean -fdx` that also wipes
`idl/build/` + `cshim_src/build/` needs the full `build_native.sh` +
bootstrap dance.

## SDK native-`.so` rebuild policy — all three `.venv/src/` repos (since v0.3.1)

All three internal SDKs live behind SSH at
`git@10.10.10.10:wardner/{wdnav,wdnai,wdndds}.git`. Two of them
ship in-tree C/C++ that's linked into a `.so` and **dynamically
bound at import time** via ctypes (`_lib.<sym>.argtypes = […]` at
module top). The previous build hooks checked only C-source mtime,
which silently missed the case where a Python wrapper added a new
ctypes binding while the C source was unchanged — leaving a stale
`.so` that `import <sdk>` would crash on at startup with
`AttributeError: undefined symbol`. Two separate instances so far:

| SDK    | hook location                                   | stale symbol that crashed                                | fix commit | rebuild cost |
|--------|-------------------------------------------------|----------------------------------------------------------|------------|--------------|
| wdndds | `.venv/src/wdndds/src/wdndds/_build.py`         | `wdndds_topic_writer_create` (after `git pull` of 871d00f)| v0.6.1     | ~20s cmake   |
| wdnav  | `.venv/src/wdnav/src/wdnav/_build_wdnav.py`     | any new `_lib.rkv_xxx.argtypes` binding                  | v0.3.1     | ~5s gcc      |
| wdnai  | (no build hook — pure Python, no in-tree `.so`) | n/a                                                      | n/a        | 0            |

Both hooks now unconditionally `os.unlink()` the pre-existing
`.so` at the start of every `pip install` / `pip install -e` and
rebuild from scratch. Opt-outs:

* **wdndds** — `WDNWEBUI_SKIP_WDNDDS_BOOTSTRAP=1` gates the whole
  bootstrap script (the only path that triggers a rebuild for
  wdndds is the bootstrap, not the user's own `pip install`).
* **wdnav** — `--skip-wdnav-video-build` CLI flag or
  `SKIP_WDNAV_VIDEO_BUILD=1` env var (CI pre-built images).

The hard rule for users of these SDKs: **after `git pull` on
`.venv/src/<sdk>`, run `./update_rkwhl.sh`.** It `git pull`s all three
SDKs and forces `pip install -e --force-reinstall --no-deps` on each,
which is what actually triggers the rebuild hooks. The previous
heuristic of "only reinstall when `pyproject.toml` changed" missed
wrapper-only commits — root cause of the wdndds
`undefined symbol: wdndds_topic_writer_create` incident after commit
871d00f (`_cshim.py` gained new ctypes bindings, `pyproject.toml`
untouched). `update_rkwhl.sh` now rebuilds unconditionally on any HEAD
advance. For one-off sync of a single SDK, `pip install -e
--force-reinstall --no-deps .venv/src/<sdk>` is the minimum; on a
fresh host, `bootstrap_wdndds_native.sh` is still required for apt
deps + native lib vendoring.

## Layout

```
wdnwebui/
├── run.py                          # entry point — uvicorn.run("app.main:app", ...)
├── bootstrap_wdndds_native.sh      # wdndds .so build (apt + stub + pip; unconditional rebuild since v0.6.1)
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

## Audio understanding (音频理解) — 嵌入音视频描述页

`#page-video-detection` 在视觉检测区下方新加 `#vd-audio-block` 子区块：
独立的 `audio prompt` textarea + "分析音频" / "停止" 按钮 + 段落事件列表。
复用上方视频源（`#vd-source` 的 value），不另起 file picker；与
`WdnaiDetector` 完全解耦，可与视觉检测同时运行。

### Omni-3B 单次推理上限（必须切片）

`wdnai/backends/omni.py:58-70` 把 audio encoder 的动态 shape 写死两档：

```python
AUDIO_SHAPES = {
    0: {"n_frame": 600, "embeds_dim0": 150},   # ≤ 6.0 s
    1: {"n_frame": 300, "embeds_dim0":  75},   # ≤ 3.0 s
}
```

`omni.py:287-291` 的截断逻辑：

```python
if mel.shape[1] >= n_frame:
    mel_padded = mel[:, :n_frame].astype(np.float16)   # 直接切到 600 帧
```

**结论**：超过 6.0 秒的音频被**静默截断**到前 6 秒；`Attachment` 串联
(`session.py:247` 的 `b"".join(...)`) 也救不了 —— 后端仍按 600 帧切。
所以 60 秒音频必须先切片。`audio_analysis.CHUNK_SECONDS = 6.0` 是硬上限。

### 切片策略（硬切片 + 时间戳列表）

`app/services/audio_analysis.py`：

* **PyAV 抽音**：从当前视频源 `av.open` → `container.streams.audio[0]` →
  `av.AudioResampler(format="s16", layout="mono", rate=16000)` →
  `b"".join(pcm_chunks)`（16 kHz mono s16le bytes）。
* **硬切片**：每 6 秒一段（`samples_per_chunk = 6.0 * 16000 = 96000`），
  无重叠；总段数 `n_chunks = ceil(total_samples / samples_per_chunk)`。
* **逐段推理**：`with get_session_call_lock(): sess.reset() + await sess.respond(prompt, attachment=[Attachment(pcm, kind="audio")], sample_rate=16000, n_channels=1, sample_width=2)`。
* **失败隔离**：单段 SDK / NPU 异常写 `AudioEvent(level="error")` 继续下一段。
* **状态机**：`idle → extracting → done / stopped / error`。

### AudioEvent 形状

```python
@dataclass
class AudioEvent:
    id:        int
    chunk_idx: int       # 0-based 段序号
    start_s:   float     # 段起点秒
    end_s:     float     # 段终点秒
    ts:        float     # 推理完成 wall-clock
    prompt:    str
    text:      str       # omni 输出（已经过 sanitize）
    level:     Optional[str] = None   # "error" 时表示该段失败
```

前端渲染为 `[00:00–00:06] 描述...` 段落时间线（`fmtTime` 把秒数格式化为
`MM:SS`）。

### Capability 与 ComputeRegistry 集成

* `MODEL_PRESETS["qwen2.5-omni-3b"]`：memory_estimate_mb=2500 /
  estimated_init_seconds=50 / capability_types=["swcap_audio_understand"] /
  worker_name="audio-3b"（comm="worker:audio-3b", 13 字符）。
* `compute_worker.py:_build_session` / `compute_worker_lazy.py:_preset_for`
  都加了 `model_id == "qwen2.5-omni-3b"` 分支（`SLM.qwen_2_5_omni_3b()`）。
* `_LEGACY_CAPABILITY_TYPE_RENAMES` 加 `"audio_understand" → "swcap_audio_understand"`，
  兼容老 config.jsonc。

### API 端点（`app/api/audio_analysis.py`）

| Method | Path | 用途 |
|---|---|---|
| `GET`  | `/api/audio-analysis`               | 快照 `{state, error, source, prompt, progress, events_count, available}` |
| `GET`  | `/api/audio-analysis/events`        | `{snapshot, events: [AudioEvent...]}` |
| `POST` | `/api/audio-analysis/start` `{source, prompt}` | 启动一次；404 source 不存在 / 400 prompt 空 / 502 capability 未启用 |
| `POST` | `/api/audio-analysis/stop`          | 中途停止（不动 session） |
| `POST` | `/api/audio-analysis/events/clear`  | 清空内存事件 |
| `WS`   | `/api/audio-analysis/ws`            | server-push `{type:"hello"|"event"|"progress"|"state"}` |

错误码与 detector / VFA 对齐：502 + `RuntimeError("功能未启用: swcap_audio_understand")`。

### 前端模块（`app/static/js/app.js`）

* `AudioAnalysis` 模块（仿 `VDDetector` 写法）：
  * `init()` 绑定 start / stop / clear；`ensureWs()` 指数退避重连（上限 30s）。
  * `onStart()` 校验 source ≠ 空（alert）+ prompt ≠ 空。
  * `onWsMessage()` 分发 hello / state / progress / event 四种帧；
    state 终态（done/error/stopped）时主动 `hydrateEvents()` 拉全量列表。
* `setVdAudioMeta(snap)`：把快照写进 `VD.audioSnap`，同时刷新
  `#vd-audio-status` / `#vd-audio-source` / 按钮 enable 状态 / 进度条。
* `setVdMeta` / `updateVdDetectorStatus` 同时把 `audio: VD.audioSnap` 推
  进 `DemoGuard.report("video-detection", ...)`。

### DemoGuard 集成

`video-detection` 已有 DemoGuard。audio 加进去需要：

* `_ACTIVE_STATES` 判定条件追加 `audio.state === "extracting"`。
* `stop()` + `stopSync()` 追加 `POST /audio-analysis/stop`（sendBeacon
  兜底）。
* `stopAndClear` 把 cache 清成 `{session: null, detector: null, audio: null}`。

`beforeunload` / `hashchange` 走已有兜底链路，自动覆盖 audio（sendBeacon
已发 stop）。

### 资源预算

| 项 | 值 | 备注 |
|---|---|---|
| omni 内存 | ~ 2500 MB | audio encoder + llm + embed；按 VL-4B 同档估；真机测后回填 |
| omni init | ~ 50s | 两个 rknn + embed；倒计时同其它 preset |
| 60s 音频切片 | 10 段 × ~ 1s | 单线程串行 ≈ 10s 总耗时 |
| 与 qwen3-vl-4b 共存 | ~ 6 GB | OK；与 qwen3-4b 同时跑超 8 GB |
| NPU 互斥 | 进程级 `_llm_session_call_lock` 串行 | 与 detector / VFA / text_chat 共享同一把锁 |

`_check_npu_memory_budget()`（`compute_registry.py`）自动纳入 omni preset
校验；同卡内存超限在 `boot_compute_services` 时被拒绝。

### 已知限制

* **音频源必须含音轨**：PyAV `streams.audio` 为空 → worker 进入 `_STATE_ERROR`，
  前端 `alert("视频无音轨")`。
* **极短音频**（< 6s）：整段算一段，shape 1 (n=300) 或 shape 0 (n=600)
  补零处理（omni 内部行为）。
* **超时熔断**：单段 SDK 卡死没有 timeout；上层若需要可包一层
  `asyncio.wait_for(...)`，但当前按 detector / VFA 现有风格不做。
* **不做二次聚合**：60 秒以上音频不会自动生成"综述"；如未来需要，
  加一段独立的 LLM 调用（建议复用 `swcap_text_chat` 的 qwen3-4b）。

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
  16-channel array back to ``config.jsonc`` under
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
  latency, sharding channels across multiple ``wdnwebui`` instances
  (each with its own LLMSession) is the answer; the shared-session
  design here makes that a horizontal scaling decision rather than a
  per-channel one.

## Configuration

The service reads a single JSONC file `config.jsonc` from disk and
holds it in memory. The file is seeded with a `base` section at
`{}` + future sections stacked alongside it.

- **JSONC, not JSON** — the on-disk file is JSONC (JSON with `//`
  line comments and `/* ... */` block comments), parsed via the
  ``json5`` library through ``app/services/jsonc.py``. **User-written
  comments are accepted on read but discarded on write**: any save
  through ``PUT /api/config/...`` or ``POST /api/config/import``
  emits a freshly formatted JSONC file with no comments. Hand-edited
  comments stay intact as long as nothing triggers a backend save;
  once any save fires, they're gone. Rationale: round-tripping
  comments faithfully would need a token-level rebuild that's brittle
  to key reordering / nested array comments / multi-line block comments;
  the simpler contract is "comments are for humans reading the file,
  the backend rewrites it cleanly on save".
- **Where it lives** — resolved from `WDNWEBUI_CONFIG` env var when
  `run.py` starts (set by `--config /path/to/config.jsonc`); if unset,
  defaults to `<repo>/config.jsonc`. Always absolute.
- **Read-write** — `app/services/config.py` `ConfigStore` is a lazy
  singleton (`get_config()`) with an in-memory dict guarded by an
  `RLock`. Missing files / non-object payloads all collapse to `{}`.
- **Atomic writes** — `replace()` / `set_section()` write a sibling
  temp file and `os.replace()`-s into place so a crash mid-write
  can't corrupt the file. Output goes through
  :func:`app.services.jsonc.dump_jsonc` (quoted keys, trailing commas,
  UTF-8 native).
- **Whole-file endpoints** (`app/api/config.py`):
  - `GET  /api/config`        — `{path, data}` (current snapshot).
  - `GET  /api/config/export` — `FileResponse` of the on-disk file
    (downloads as `config.jsonc`). If the file doesn't exist yet,
    the endpoint writes `{}` first so the download always succeeds.
  - `POST /api/config/import` — `multipart/form-data` upload (field
    name `file`). Validates UTF-8, JSONC parse, top-level object —
    rejects empty bodies, lists, scalars with HTTP 400. Uploaded
    JSONC with comments is accepted; the persisted file drops the
    comments on write (see "JSONC, not JSON" above).
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
    via `secrets.choice`) and persisted to `config.jsonc` so the value
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
  16-channel array; the same `config.jsonc` powers the dedicated
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
  硬编码：Qwen3-VL-4B ≈ 3500 MB / Qwen3-TTS ≈ 1900 MB / Qwen3-4B ≈
  4950 MB。Boot 流程：`boot_compute_services()` 遍历
  `config.compute.services` 逐条 probe → adopt-or-spawn；worker 起来
  后**不** build LLMSession，等用户在算力配置页选 NPU 触发 RPC load
  才 instantiate（55-90s）。UI 通过 `GET /api/compute` 每秒轮询状态，
  loading 实例显示预估倒计时（`ESTIMATED_INIT_SECONDS_PER_MODEL`）。
  Capability 概念作为内部 key 使用（`get_session("swcap_video_describe")` /
  `"swcap_audio_tts"`）；capability 路由仅走同进程 `ComputeRegistry`，
  跨机器发现走 FastDDS SIMPLE multicast（v0.6+），`ComputeRegistry`
  的 `Hub` 服务名 `wdndds/rpc/<model_id>` 自动暴露给 peer。

## App 提示词模板 (app.prompts)

`config.jsonc["app"]["prompts"]` 是一个 3 层 schema 驱动的提示词模板
配置 —— 给 wdnai 的 `@Generable` schema class (`VLMDoc` / `TargetDoc`)
的每个 `Field(description=...)` 提供用户可编辑的中文说明。模型根据
`description` 推断字段语义，描述越具体，返回字段越稳。

v0.11 起从 `compute.app_prompts` 挪到独立 `app` section（顶层
`base / app / compute` 三层结构更清晰 —— prompts 跟硬件能力 / 算力
实例是不同抽象层）。

* **路径** — `app.prompts.<app_key>.<schema_cls>.<field>`，
  三段都是字面 key。例如 `app.prompts.video_detection.VLMDoc.text`
  写到 `text: str` 这一个 field 的 description 上。
* **当前可配字段 (3 项)**：
  - `video_detection.VLMDoc.text` — 音视频描述页 `text` 字段描述。
  - `video_detection.VLMDoc.level` — 严重程度标签字段描述。
  - `video_target_detection.TargetDoc.desc` — 视频目标检测页
    `desc` 字段描述。
* **`TargetDoc.x1/y1/x2/y2` 刻意不做配置** —— 4 个 bbox 坐标字段的
  description 写死在 `_build_target_doc_class()` (`app/services/detector.py`)
  函数体里，是协议层 0–1000 归一化坐标格式说明，与模型对「画哪里」
  的理解无关，开 UI 编辑只会让用户误改协议格式。`_SCHEMA_CACHE`
  的 cache key 也只覆盖可配字段 (`desc`) 的 description，硬编码坐标
  字面量不进 cache（也不在 `_load_app_prompts` 返回值里）。
* **默认值在 `app/services/detector.py` 模块顶层常量**
  (`DEFAULT_VLM_DOC_TEXT_DESC` / `DEFAULT_VLM_DOC_LEVEL_DESC` /
  `DEFAULT_TARGET_DOC_DESC_DESC`)，简体中文一句话。
* **空值兜底** —— 配置里字段缺失或空字符串 fallback 到默认常量，
  不写盘空值；`PUT /api/config/app-prompts` 由
  `AppPromptsConfig.field_validator` 兜底拒绝空字段 (422)。
* **修改提示词后重启检测后生效** —— detector 实例的 schema class 在
  `start()` 时 build 一次，cached 在模块级 `_SCHEMA_CACHE`
  (`app/services/detector.py`)；UI 上方有 hint `#bc-prompts-hint`
  提示用户保存后必须停止 + 重新启动检测循环。
* **API** (`app/api/config.py`)：
  - `GET  /api/config/app-prompts` — 返回当前生效值（缺字段用默认补）。
  - `PUT  /api/config/app-prompts` — 全量替换；`AppPromptsConfig`
    校验每个字段非空 + strip 后落盘。
* **UI 入口** —— 系统配置页 (`#base-config`) 下新增
  `#bc-section-app-prompts` 子区块（`app/static/index.html`）：
  音视频描述 (VLMDoc) 两条 textarea + 视频目标检测 (TargetDoc) 一条
  textarea + 保存/放弃修改按钮。`Base` 模块 (`app/static/js/app.js`)
  的 `loadPrompts()` / `savePrompts()` / `resetPrompts()` 负责
  GET/PUT 三件套。
* **添加新 app 的提示词步骤**：
  1. `app/api/config.py` 里定义 `<Schema>Prompts(BaseModel)`，加 field
     description；`AppPromptsConfig` 加同名嵌套字段并配
     `field_validator`。
  2. `app/services/detector.py` `_load_app_prompts()` 的 `defaults`
     映射表追加 `(<app_key>, <schema_cls>)` 条目 + 默认常量。
  3. `app/static/index.html` 的 `#bc-section-app-prompts` 内追加
     一组 `<h4>` + `<textarea>` 控件。
  4. `app/static/js/app.js` `Base.loadPrompts()` /
     `savePrompts()` 里追加对应字段读写 + `addEventListener`
     绑定。
* **Schema cache 行为** — `_SCHEMA_CACHE: dict[tuple, type] = {}`
  以 `(schema_name, tuple(sorted(field_descs)))` 为 key：
  description 变了 → 新 key → rebuild；description 没变 → 命中旧
  key → 复用。一般用户每次点"保存"最多触发 1 次重建，不在每次
  inference 重建。

## Discovery page (注册发现)

`#page-discovery` 内部按从上到下两个 pane 排布：

1. **硬件能力 (HW)** —— 本机硬件能力开关，源自 `HARDWARE_CAPABILITIES` 静态表 +
   `config.jsonc["base"]["hardware_capabilities"]` 启用状态。checkbox 修改后
   点"保存修改"才落盘，避免误触。
2. **软件能力 (SW)** —— 当前 running 实例暴露的能力（每条来自一个 RKNN 模型实例）。

**v0.10 起下线**：原"在网注册 (DDS)"子面板（含开关 + FastDDS 能力广播 +
对端列表）整个删除 —— `DdsDiscoveryManager` 之前只起一个 placeholder
`RpcHub`，既不广播 capability、也不订阅对端列表（`_discovered` 永远为
0），toggle 是个空壳。跨机器发现改由 FastDDS SIMPLE multicast（v0.6+）
承担，「系统信息 → 在网设备」section 展示主进程 Hub 的真实 peer 列表。

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
- **`ComputeRegistry` 启动顺序 (v0.7→v0.10)**：`app.main.create_app()` 必须先
  `include_router(compute.router)`，**再**fire ``compute-boot`` daemon
  thread 调 `get_registry().boot_compute_services()`，**再**注册 detector 的
  frame sink。daemon-thread 化（v0.10）是为了让 ``create_app()`` 同步返回 →
  uvicorn 立即 bind 8000；probe / adopt / spawn 的耗时挪到后台。

  **`boot_compute_services()` (v0.10 /proc 先行)**：遍历
  ``MODEL_PRESETS`` 全集，逐条先扫 ``/proc/*/comm``：
   - **`_find_workers_by_name(model_id)` 命中**（本机有 worker comm）
     → 走 RPC ``info`` probe（``_probe_service`` 3 × 5s race-recovery）+ 
     ``_adopt_existing_worker``。probe 命中 → ``_source`` 按 info
     ``hostname`` 决定：同 hostname → ``"adopted_local"``（本机孤儿 /
     reload 残留），不同 hostname → ``"adopted"``（真 LAN peer）；
     probe 失败 + config 里有 → fall through ``_spawn_compute_worker``
     让它的 dedup 拒 spawn sibling 登 errored inst；probe 失败 + config
     里没 → log warning + skip。
   - **`_find_workers_by_name(model_id)` 未命中**（冷启动 / 全新板）
     → **跳过 probe**（避免冷启动 5 × 17s ≈ 85s 纯 timeout 阻塞），
     config 里有 → ``_spawn_compute_worker`` 拉新子进程（``python
     compute_worker.py --model <id>``，**不传 --npu**），config 里没
     → skip（UI ``unavailable``）。

  **为什么 `/proc` 先行**：v0.10 之前的 boot 对每个 preset 都跑一遍
  probe；冷启动无 worker 时 5 × 17s timeout 浪费且阻塞 create_app。
  ``/proc`` 先行后冷启动 probe 数 = 0，后台总耗时 < 100ms。

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
  是兜底，不论 `config.jsonc["compute"]["tool_call_max_iters"]` 是 0
  （无限）还是用户显式填了超大值，单帧推理都不会超过 200 次
  `sess.respond`。200 次 × 1s/inference ≈ 3 分钟单帧最坏延迟 -
  — 失控模型可以跑很久但不会锁死整个 worker 线程 / 队列。
  配置热生效：每次推理前 `_get_tool_call_max_iters()` 重读 config.jsonc。
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
  可见性 / 行为：
  - **spawed**：启用 / 重启 / 禁用 三按钮全开；禁用 = RPC `unload`
    （worker 进程保留，status=not_loaded，可秒级重启用）。
  - **adopted_local**：启用 / 禁用 两按钮可点（重启 disable 提示
    `kill <pid>` 手动清理）；禁用走 RPC `unload` 同 spawned。
  - **adopted**：启用 / 禁用 两按钮可点（重启 disable）；禁用走
    RPC `unload` 让对端 peer 释放 LLMSession，本机 registry 同步
    not_loaded，但**不**碰 peer 进程。
  - **unavailable**：只有 启用 可点（spawn 新进程）。
  - "完全关 worker 进程" 唯一入口：重启服务按钮（Q3 4 步：unload →
    shutdown → wait/kill → 走 boot 流程重 spawn）。孤儿 worker
    由下次 boot 的 `_find_workers_by_name` 兜底。
  后端 `_require_owned` 仅对 **重启** 仍拦 adopted → 403（restart
  要 kill 进程）；禁用走 `unload_instance()` 对三类来源都允许。
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
  - qwen3-4b → `worker:llm-4b`（12 字符）
  - qwen3-vl-embedding-2b → `worker:emb-2b`（12 字符）
  - qwen3-5-vl-4b → `worker:q5-vl`（12 字符；多模态 LLM+vision）

  受 TASK_COMM_LEN=16 限制 alias 必须在 8 字符内（15 - len("worker:")）。
  命名放在 worker 端 main() 里调 —— preexec_fn 里调 prctl 会被紧随的
  exec() 用新 argv[0] basename 覆盖。`_find_workers_by_name` 同时
  fallback 扫 cmdline 兼容老 worker（reload 后的第一次清理会识别
  comm="python" 的历史 worker，全清之后 cmdline 退化为 no-op）。
- **FastDDS participant_name 命名约定 (v0.8)**：每个 DomainParticipant
  的名字都带 `config.jsonc["base"]["device_name"]` 后缀，方便 LAN daemon
  的 `/peers` 端点和 DDS 调试日志一眼区分板子：
   - 主进程：`wdnwebui-main-{device_name}`
   - discovery Hub（注册发现页）：`wdnwebui-discovery-{device_name}`
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
  - `GET  /api/video-file-analysis/runs/{run_id}/query?scene=&actor=&object=&action=&exact=&q=&frame_min=&frame_max=&limit=` —
    反查：多维 AND 过滤 + 两种搜索模式（`exact` > `q` 互斥）。
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
    精准匹配 / 模糊搜索 二选一 + 时间范围 `HH:MM:SS`（on-blur 校验，
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

`#page-text-chat` 暴露 Qwen3-4B 纯文本对话（`swcap_text_chat` capability），
流式逐字渲染 ` thinking... response` 与正式回答，单页实例串行推理，
多轮上下文由 worker 端滚动裁剪 + ChatML 拼接维持。

**推理走 compute_worker 子进程（RPC）**。`get_session("swcap_text_chat")`
返回 `RpcSessionProxy`，`session.chat_run(...)` 通过 wdndds RPC 把
`{history, text, context_limit, max_new_tokens}` 发给 worker；worker 后台
线程用真实 tokenizer 精确裁剪历史、拼 ChatML、跑 `_backend.run`，边跑边把
`sink.text` 增量收进 chat state。主进程 `chat_poll` 轮询拉增量（~150ms
粒度），`on_delta` 喂给 `_ThinkingRouter` 流式切分 thinking / answer。

* **Backend** (`app/api/text_chat.py`):
  - `GET /api/text-chat` — snapshot：state / busy / error / history_count /
    history_tokens / context_limit / max_new_tokens / current_request_id /
    last_result。
  - `POST /api/text-chat/send` `{text}` — 发起一轮推理；返回
    `{request_id, snapshot}`。400 text 为空 / 409 还在生成中 /
    502 capability 未启用。
  - `POST /api/text-chat/stop` — 置 `_worker_stop_event`；proxy 的
    `chat_run` 轮询循环感知后调 `chat_stop` RPC（软停：worker 跑完返回
    stopped，主进程丢弃本轮，不写历史）。
  - `POST /api/text-chat/reset` — 清空 history + history_tokens，回到 idle。
  - `WS /api/text-chat/ws` — server-push 流式 token + 状态变更；连接后立即
    推 `{type:"snapshot", snapshot:...}` hydrate。

* **Service** (`app/services/text_chat.py`):
  - `_TextChatManager` 单例（`get_chat_service()`，lazy）；状态机
    `idle → loading → running → done/error/stopped`，与 wdnwebui
    其它服务风格一致。
  - **LLMSession 必须从 ComputeRegistry 取**（`get_session("swcap_text_chat")`）；
    找不到抛 `RuntimeError("功能未启用: swcap_text_chat")`，HTTP 502 透传。
    拿到的是 `RpcSessionProxy` —— 本模块**不再访问 `session._backend`**，
    wdnai 的 monkey-patch（`make_result_callback` 包装）已整体移除。
  - **`_ThinkingRouter`**：`chat_run` 的 `on_delta` 每次拿到增量时按
    ` response` 切频道推到 `thinking` / `answer` 两条流；`done` 时再用
    `chat_runner.split_thinking` 兜底切一次（处理 token 边界切碎等极端情况）。
  - 单 daemon worker thread（`text-chat-worker`）串行消费 `request_queue`；
    `chat_run` 同步阻塞在 RPC 轮询里，每轮检查 `_worker_stop_event`。
  - **上下文管理在 worker 端**（`app/services/chat_runner.py`）：
    `trim_history` 用真实 tokenizer 从最新往回累计每条消息 token，超
    `context_limit - reserve` 就裁掉更早的（滚动窗口）；reserve =
    `min(max_new_tokens, context_limit // 8)`（自适应输出预留，小 context
    测试如 2048 不会误伤）。仅单条用户消息超限才报"单条消息过长"。
  - **多轮上下文**：app 端消息 deque 全量发给 worker；worker 每轮
    `sess.reset()` 清 C 端 KV 后 `_backend.run(prompt, keep_history=0)`。
    **历史中只写 `content:answer`，不写 thinking** —— 下轮 prompt 看不到
    thinking，保证模型下一轮从干净的对话记忆开始。
  - **history_tokens 语义**：每次 done 后覆盖为 worker 返回的
    `prompt_tokens`（最近一次实际喂给模型的 prompt token 数），前端据此
    显示上下文占用。
  - **context_limit 可配置**：`config.jsonc["compute"]["text_chat"]["context_limit"]`，
    默认 16384；每次推理前热读（`_resolve_context_limit`）。测试多轮裁剪
    可设 2048。
  - WS broadcaster pattern：worker 线程 push 到 `asyncio.Queue` 列表，
    WS handler 在主 event loop 里 `await queue.get()` drain；observer
    也走同一条路径，避免第二条 channel。

* **Worker 端 chat 通道**（`app/services/compute_worker.py`）：
  - `chat_run` action：校验 session loaded + 无并发 chat；把 history/text/
    context_limit/max_new_tokens 存进 `state["chat"]`，起后台线程
    `_chat_run_loop`，pump 线程立即返回 `{"status":"chat_started"}`（对齐
    `load_handler` 的后台线程模式，避免长推理占住 cshim pump 线程）。
  - `_chat_run_loop`：`chat_runner.trim_history` 精确裁剪 → 拼 ChatML →
    `sess.reset()` → **注册 C 回调 observer**（`app/services/chat_stream.py`，
    见下）把 `sink.text` 增量 append 到 `state["chat"]["deltas"]` →
    `_backend.run(prompt, keep_history=0, enable_thinking=True)` → done
    （text / elapsed_sec / prompt_tokens / answer_tokens）。
  - **工具 schema 注入与解析（worker 端只解析不执行）**：payload 带
    `tools`（class 名列表）时，`_resolve_tool_classes` 从工具 registry
    枚举真实 class（无手工映射表），`render_tools_block` +
    `_TOOL_CALL_INSTRUCTION` 注入 prompt；done 后仅对 answer 段跑
    `_parse_qwen_tool_call`（`<tool_call>` 标签优先 + 倒序 JSON 扫描
    兜底——真机主力路径是 markdown ```json 围栏）。result 附
    `tool_call` / `tool_call_args`。副作用执行在主进程侧，见
    "Chat 工具调用 (chat-agent)" 章节。
  - `chat_poll` action：按 offset 返回新 delta + status（running / done /
    error / idle），done 时附 result。
  - `chat_stop` action：置 `state["chat"]["stop"]`（软停标志）。
  - `_build_session` 已支持 qwen3-4b（`max_context_len=16384` +
    `with_options(max_new_tokens=2048, keep_history=1, enable_thinking=True)`）。

* **流式采集 = C 回调注入**（`app/services/chat_stream.py`）：
  - **不能靠独立线程轮询 `sink.text`** —— `_backend.run` 的 `session_run`
    是同步阻塞的 C 调用，解码回调持有 GIL 逐批执行，并行轮询线程抢不到
    GIL，`deltas` 恒为空（真机实测：状态走到 done 但前端无任何回复）。
  - `install_patch()`：幂等包装 wdnai 的结果回调工厂（在原 callback
    之后按 `id(sink)` 查全局 observer，算 `sink.text` 增量并调用它；
    回调本身在 C 层持有 GIL 时执行，采集可靠。**必须显式
    `return ret`**，漏 return 会让 ctypes/shim 把返回值吞掉并导致
    原 callback 副作用失效）。**patch 点随 wdnai 上游重构演进**
    （f61dddd 把 backends 从 rknn3lite 迁到 vendored
    `wdnai._librknn` shim 时把工厂 `make_result_callback` 改名为
    `_make_sink_result_cb`，2026-09 事故：真机算力配置页所有模型
    加载报 `AttributeError: ... no attribute 'make_result_callback'`
    —— wrapper 依赖 SDK 私有符号，SDK 一重构就断）。现行探测顺序：
    `_make_sink_result_cb`（当前 SDK）→ `make_result_callback`
    （旧 SDK 回滚兼容）→ 都缺则 **fail-soft**：LOG.error 后放弃
    patch —— chat 退化为无流式（done 一次性输出全文），
    **不阻断模型加载**。`compute_worker._load_wdnai()` 里对
    install_patch 再包一层 try/except（双保险，patch 内部任何
    意外都不阻断 worker 加载模型）。**pull wdnai 后 chat 若无流式，
    先查 worker 日志里 chat_stream 的 patch 行**。
  - shim 调用契约（`wdnai._librknn._result_trampoline`）：
    `user_cb(py_obj, tokens, state)` 3 实参，`py_obj` 取
    `getattr(cb, "__self__", cb)`（free closure 无 `__self__` →
    pin closure 本身）—— wrapper 用 free closure 3 参签名直接兼容，
    无需 bound-method 改造；`_LLMCallState` 数值
    NORMAL=0 / FINISH 族 (2,3,4) / ERROR=5 与 `chat_stream.py`
    顶部常量一致。
  - 在 `compute_worker._load_wdnai()` 里安装（早于任何 `_build_session`
    构造 LLMSession，patch 才能绑进 `init_runtime`）。未注册 observer 的
    sink（detector / vlm / tts）走原 callback 零开销。
  - `register_observer(sink, cb)` / `unregister_observer(sink)`：全局 dict
    key=`id(sink)`，`_chat_run_loop` 在 run 前后注册/注销。

* **Proxy**（`app/services/rpc_session_proxy.py`）：
  - `chat_run(history, text, *, context_limit, max_new_tokens, on_delta,
    stop_event, poll_interval_ms=150, timeout_ms=300000,
    tool_names=None)` — 同步阻塞，发
    `chat_run` 后循环 `chat_poll` 拉增量调 `on_delta`；`stop_event` 置位即
    调 `chat_stop`；done 返回 result dict（含 thinking / answer /
    tool_call / tool_call_args）；error 抛 `RuntimeError`；超时抛
    `TimeoutError`。
  - `chat_stop()` — RPC `chat_stop`。

* **ComputeRegistry** preset（`app/services/compute_registry.py:136-164`）：
  - 新增 `MODEL_PRESETS["qwen3-4b"]`：`capability_types=["swcap_text_chat"]`，
    `memory_estimate_mb=4950`，`estimated_init_seconds=60`。
  - `_qwen3_4b_spec()` 工厂用 `dataclasses.replace(spec, max_context_len=16384)`
    + `with_options(max_new_tokens=2048, keep_history=1, enable_thinking=True)`。
    注意 `max_context_len` 是 `ModelSpec` 的 dataclass 字段（被
    `_common.build_llm_args` 直接读），**不能用 `with_options` 设**，必须
    `replace`。
  - `_init_session_subprocess` 子进程硬编码分支加 `elif model_id == 'qwen3-4b'`。
* **ComputeRegistry 配置 — `locate-anything` grounding preset**
  （`app/services/compute_registry.py`）：纯图像输入 + 文本输出（0–1000
  归一化 bbox + 描述），与 VLM 共用同款文件布局（`rknn_llm_path` +
  `rknn_vision_path` + `embed_path` + `tokenizer_path`），但绑定到
  `LocateAnything` backend（`wdnai.backends.locate_anything.LocateAnythingBackend`）。
  - `MODEL_PRESETS["locate-anything"]`：
    `kind="grounding"`（probe 按 vlm 文件布局校验），
    `capability_types=["swcap_grounding"]`，
    `memory_estimate_mb=5500`，
    `estimated_init_seconds=60`，
    `worker_name="ground"`（comm = `worker:ground`，13 字符；alias 6 字符
    在 TASK_COMM_LEN=16 的 8 字符硬限内）。
  - `_locate_anything_spec()` 工厂直接调
    `wdnai.SystemLanguageModel.locate_anything()`，文件缺失时抛
    `FileNotFoundError`；`_locate_anything_spec_template()` 跳过路径校验
    返回裸 spec，仅供算力配置页 / 启动预检的形态占位用，真实
    `LLMSession(model=spec, npu=K)` init 仍会报缺文件。

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
      assistant 气泡"三段渲染状态；`_finalizeBubble` 优先用 done 帧的完整
      thinking/answer 覆盖流式 buffer（一次性结果也能完整渲染）；
    - `_scrollToBottom()` 仅在用户靠近底部时自动滚动（避免抢焦点）。
  - `loadPageData("text-chat")` 调 `TextChat.init() + refresh() + ensureWs()`；
    `showPage()` 离开时调 `TextChat.closeWs()`。

* **资源预算** —— 单实例常驻 ~5.5 GB（Qwen3-4B 量化权重 + embed table +
  KV cache 200×2K token）。板上 8 GB 统一内存比较紧张，但与 Qwen3-VL-4B
  单实例不冲突（2 个同时跑超 9 GB）。多用户并发首版不支持 —— worker
  串行；如需横向扩展，在算力配置页加第 2 个 Qwen3-4B 实例（多卡环境）。

* **限制 / 已知问题**：
  - stop 为**软停**：`chat_stop` 只置 worker 标志，`_backend.run` 同步阻塞
    跑完才返回，主进程丢弃本轮。跨线程 `session_stop()` 硬中断（C 库
    FINISH state=3）尚未打通，真机验证后再加。
  - `history` 全量每次 RPC 传输（deque 200 条上限）：同机 SHM 无碍；跨机
    UDP 传输受 FastDDS 单包大小限制，长对话跨机场景需评估。
  - thinking 不写历史 —— 用户"接着问"时下一轮 prompt 看不到上轮思考；
    想要延续 thinking 行为需把 `thinking` 也 push 到 history 的 assistant
    content 字段，但代价是污染 KV 且长对话下 token 增长更快。
  - 重生成按钮首版不做（plan §十三已确认）。
  - 真机首次初始化 ~90s（按经验值，需真机 page-cache-dropped 实测校准）。

## When you change things, also update

| You changed…                          | Then update…                                              |
| ------------------------------------- | --------------------------------------------------------- |
| Service version                       | `app/__init__.py` `__version__` (single source of truth)  |
| Default bind host / port              | `run.py` `main()`                                        |
| Public SDK dependency                 | Re-run the two `pip install -e` lines above              |
| Anything that needs a new endpoint    | Add a route inside `create_app()` in `app/main.py`        |
| Added/removed a model preset          | `MODEL_PRESETS` in `app/services/compute_registry.py` + `register` the new capability in the same table; update `app/static/index.html` and `app/static/js/app.js` only if the UI needs new model cards |
| `ComputeRegistry` config schema       | `ComputeConfigPayload` in `app/api/compute.py` + `MODEL_PRESETS` keys; reboot-flush config (`PUT /api/compute/config`) carries Pydantic validation |
| Schema hint prompts (`VLMDoc.text`/`level`, `TargetDoc.desc`) | `app.prompts.<app_key>.<schema_cls>.<field>` in `config.jsonc`; UI under 系统配置 → App 提示词模板 section. `TargetDoc.x1/y1/x2/y2` stay hardcoded in `_build_target_doc_class` (`app/services/detector.py`) — protocol-level 0–1000 归一化坐标格式说明，刻意不开 UI 编辑 |
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
  - ``tts_alert`` 暴露面：**音视频描述页**（``ALLOWED_TOOLS =
    ("tts_alert",)`` 在 ``app/api/video_detection.py``）+ **chat 链路**
    （web 文本对话 / 微信桥接，经 capability 过滤后自动纳入，
    见下方 "Chat 工具调用 (chat-agent)" 章节）。
  - ``video_search`` 暴露面：仅 **chat 链路**（微信视频检索场景，
    见 "微信视频检索" 章节）；音视频描述页不使用。
  - 算力配置页 RK3588 列只读展示全部工具的状态（当前有
    ``tts_alert`` / ``video_search`` 两条）。

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
  面 / 停止检测都清空，不写 ``config.jsonc``。``tts_alert`` 触发
  的副作用（已播放 / 失败等）只在 ``DetectorEvent`` deque 里活，
  刷新页面 / 重启就丢；不写盘、不另开缓存。

* **单帧多轮工具调用（agent-loop）**：详见上文 detector 段。
  配置入口 ``config.jsonc["compute"]["tool_call_max_iters"``（默认
  5，``0`` = 无限，缺失 / 负值 / 类型错 → 回退默认 5）；任何路径
  都被 ``WdnaiDetector._ABSOLUTE_MAX_ITERS = 200`` 硬上限兜底。
  工具调用 + 返回按 prose 格式写进下一轮 prompt（不是 JSON，避免
  模型复读），由 detector 内部维护。视频目标检测页无工具，循环
  天然只跑一轮就跳出。

## Chat 工具调用 (chat-agent)

web 文本对话与微信桥接共用 ``run_chat_with_tools()`
（``app/services/chat_agent.py``）：模型发起 tool_call 后在**主进程**
实例化并执行 @Callable，wrap-up / 回喂继续推理，直到给出自然语言
回答或达到循环上限。done 帧带最终 ``tool_call`` / ``tool_call_args``
+ ``steps[]``（每次执行的 args/outcome/error 明细）；
``TextChat`` 前端在气泡 meta 渲染 ``🔧 已调用 <工具名>`` 徽章
（`.text-chat-tool-badge`）。

* **为什么循环放主进程**：工具副作用依赖主进程资源（TtsSession /
  registry / 未来设备控制等）；compute_worker 是独立子进程，只能做
  schema 注入与输出解析（见 Text chat 章节 worker 端描述），执行
  必须回到主进程。
* **循环分流**：
  - side-effect 型（``__call__`` 返回 dict 带 ``consume_iteration:
    True``，如 tts_alert）→ 成功后跑一轮 wrap-up（``tools=[]``，
    输入"请一句话向用户确认"）生成自然语言确认语 → 出口；
    wrap-up RPC 失败回退固定文案。
  - 结果型（无该标志）→ 结果 JSON 转 prose transcript 作为下一轮
    `current_text` 继续推理，可链式再次 tool_call。
  - 执行失败 / 抛错 → **失败 transcript 回喂**（与结果型同构）：
    `[系统] 工具 X 调用失败：<原因>。请勿提及技术细节，自然语言告知
    用户暂时无法完成` → 模型生成解释后出口；连续失败由 max_iters
    兜底收敛。无特殊分支、无固定文案出口。失败明细写入 steps[].error。
* **capability 前置校验**：``_build_instance`` 对 ``requires_
  capability`` 未加载的工具直接抛 ``ToolExecutionError`` —— 保证
  "已注册但不可用的工具被调用必然返回错误"，未来新工具无需各自在
  ``__call__`` 里重复检查（tts_alert 内部仍保留自己的检查）。
* **history 写回**：调用方只写首条 user 条目，然后 extend agent 返回的
  `history_append` —— 含**自然语言** assistant 占位
  （如"（我已通过语音提醒完成操作：「请喝水」。）"，取 registry 的
  `spec.name` 中文名/英文名 + 结果摘要；语言按本轮用户输入 CJK 占比
  自适应中英）、user 系统指令条目、最终 assistant 回答。占位刻意不
  带 class 名 / JSON 参数等机器痕迹 —— 卸载工具模型后模型凭历史记忆
  复述出的也是无害人话，而非 `[已调用工具 X(参数)]` 机器腔。
  dict 上仍保留 `tool_call` / `tool_call_args` 字段供程序追溯。
  wrap-up / 失败回喂指令统一用展示名且要求模型"语言与用户当前对话
  保持一致"（英文用户场景不再强制中文回复）。
* **max_iters**：复用 ``compute.tool_call_max_iters``，chat 场景默认
  3（detector 默认 5），0 = 无限、200 硬上限兜底。
* **capability 过滤**：``resolve_available_tool_names()``
  （`tools/registry.py`）——``requires_capability`` 对应模型未加载的
  工具直接从 schema 排除，模型看不到就调不到。text_chat /
  wechat_llm 的 `_resolve_tool_names` 均委托它。
* **worker 端解析与注入解耦**：schema 注入按 capability 过滤后的
  `tool_names`；tool_call 解析则**始终按 registry 全量** —— 工具模型
  卸载后 prompt 无该 schema，但模型凭历史记忆自发输出的调用仍被
  识别并返回主进程执行（capability 校验必然抛错 → 失败回喂自然告知）。
  完全编造的工具名 dispatch 校验失败，当纯文本。
* **新增一个工具的三步流程（零框架改动）**：
  1. ``app/services/tools/<tool>.py`` 定义 ``@WdnaiCallable`` 类 +
     ``__call__(self, frame_bgr, ctx)``（纯文本场景 frame 传 None）；
  2. ``get_tool_registry()`` 里 ``register(...)`` 一行（需 NPU 算力的
     标注 ``requires_capability``）;
  3. 完成 —— schema 注入 / 解析 / capability 过滤 / chat-agent 循环 /
     detector 页勾选列表全部自动生效。worker 端无需任何改动
     （`_TOOL_TABLE` 手工映射表已删除，改枚举 registry）。
* **已知边界**：TtsAlert busy 时返回 silent skip dict（不带
  consume_iteration）→ 会走结果型回喂分支，模型将向用户转述
  "因繁忙未播"；语义上更理想是静默终止 + wrap-up，真机接入 TTS
  后按体验决定是否调整（`_execute_tool` 后加 silent 判定约 3 行）。
* **工具模型卸载后的"历史诱导调用"**：工具调用成功会在 history 留下
  `[已调用工具 X(...)]` 语义化占位；对应模型（如 qwen3-tts）被卸载后，
  新一轮 prompt 无该 schema —— **绝不会误执行**（capability 前置
  校验必然抛错）；模型可能凭历史记忆继续发起调用，走标准失败回喂
  路径：`[系统] 工具 X 调用失败：功能未启用…` → 模型自然语言告知用户，
  与其它运行时失败同构，无特殊分支。

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
      - `kind="grounding"`：`rknn_llm_path` + `rknn_vision_path` +
        `embed_path` + `tokenizer_path`（与 `kind="vlm"` 同布局，
        LocateAnything backend `wdnai.backends.locate_anything.LocateAnythingBackend`
        走 RKNN 视觉 encoder + RKNN LLM decoder；输出 0–1000 归一化 bbox + 文本）
        — `MODEL_PRESETS["locate-anything"]` memory_estimate_mb=5500、
        capability_types=["swcap_grounding"]、`worker_name="ground"`（alias 6 字符，
        在 TASK_COMM_LEN=16 的 8 字符硬限内）。
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

**实例配置**（`MODEL_PRESETS["qwen3-4b"]`）：

| 字段 | 值 | 备注 |
|---|---|---|
| `memory_estimate_mb` | **4950** | 真机 4 卡板 RK1828 实际占用 |
| `estimated_init_seconds` | **60** | 实测 ~55s + 5s buffer |

## Im-bridge page (应用对接 — 微信 iLink)

`#page-im-bridge` 拆成两块：左侧登录面板 + 右侧调试收发对话
（user_id 下拉 + 文本框 + 消息列表）。底层走 [`wechatbot-sdk` 0.3.0](https://www.wechatbot.dev/zh/python)，
但调用入口是 `app/services/wechat_bot.py` 里本地 `Wechat` 类（v0.5+
从 `wdndds.Wechat` 内联过来）的**同步门面**（自带 daemon-thread
asyncio loop），不是直接 `await` SDK。`wechatbot-sdk` 是 wdnwebui
的**直接**依赖（`requirements.txt`），不再是 wdndds 的 transitive
依赖 —— wdndds 0.4 已移除 `Wechat` 模块。

* **服务** (`app/services/wechat.py` + `app/services/wechat_bot.py`)：
  - `Wechat` 类（`app/services/wechat_bot.py`）—— 同步门面，持有
    `wechatbot.WeChatBot` + 私有 asyncio loop + daemon thread。
    暴露与原 `wdndds.Wechat` 一致的 API（`start_login` / `cancel_login` /
    `logout` / `send_text` / `register_message_handler` /
    `login_status` / `recent_messages` / `known_users` /
    `check_runtime`）。
  - 单例 `_WechatManager`（`app/services/wechat.py`，lazy，
    ``get_wechat()``），内部持一个 `wechat_bot.Wechat` 实例。
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
  wechat_bot.Wechat._qr_url set, login_state=awaiting_scan
      ↓ 1Hz 轮询 + 回调即推
  _WechatManager._push_state_diff → WS broadcast {type:"state", state:{login_state:..., qr_url:...}}
      ↓ 前端 _renderQr() 用 qrcode-generator 把 URL 渲染成 <svg>
  [用户在手机点确认]
      ↓ SDK 回调 on_scanned → on_confirmed → bot.start() 自动起 long-poll
  login_state=confirmed + started=True
      ↓ [用户A 发消息]
  wechatbot bot._dispatch(msg) → wechat_bot._on_incoming → _WechatManager._handle_incoming
      ↓ _push_state_diff → WS broadcast {type:"message", message:{direction:"in", ...}}
      ↓ 前端 _recordMessage + _renderChat → 追加到列表
  [用户点发送]
      ↓ POST /api/wechat/send
  wechat_bot.Wechat.send_text → wechatbot.bot.send(user_id, text)
      ↓ SDK _send_content + _send_text
      ↓ wechat_bot._record_outbound → _push_state_diff → WS broadcast {type:"message", direction:"out"}
  ```

* **凭证路径** = `~/.wechatbot/credentials.json`（`wechat_bot.DEFAULT_CRED_PATH`），
  也可设 `WDNDDS_WECHAT_CRED_PATH` 环境变量覆盖（向后兼容命名）。
  这是 SDK 默认路径，多 wdnwebui 实例不会冲突。

* **资源预算** —— `wechat_bot.Wechat` 单实例长期驻留一个 daemon thread + 一个
  aiohttp 连接池；未登录时 SDK 不会建任何连接（`bot.login()` 之前
  `_api` 只在 `poll_qr_status` 时拉 HTTP）。登录失败 / QR 过期 → SDK
  内部走 `MAX_QR_REFRESH_COUNT=3` 轮后抛 `AuthError`，wrapper 把
  `login_state` 切到 `error`，UI 看到红色徽章 + 点 登录 重试。

## 微信对接文字对话模型（LLM Bridge）

im-bridge 页「调试收发」卡片顶部新增一个「对接文字对话模型」开关。开启后
微信用户发来的**文字**消息自动进入桥接队列 → 调 `swcap_text_chat`
（qwen3-4b）推理 → 把回答（只回 answer，不含 thinking）通过
`Wechat.send_text` 发回给该微信用户，实现微信上的多轮文本对话。

* **Service** (`app/services/wechat_llm.py`)：
  - `_LlmBridge` 单例（`get_llm_bridge()`），由 `_WechatManager` 持有并
    在 `_handle_incoming` 里转发 `handle_incoming(msg)`。
  - **开关内存态**：`_enabled` 不落盘（重启即关）。`available()` 通过
    `get_registry().get_session("swcap_text_chat")` 判断 qwen3-4b 是否
    已加载；未加载时 `set_enabled(True)` 抛 `RuntimeError`（前端置灰 +
    后端 409 双层防护）。
  - **per-user 上下文隔离**：`histories: dict[user_id, deque(maxlen=200)]`
    各自独立，与 web 文本对话的全局历史完全无关；服务重启即清空。
  - **串行保证**：桥接 worker 线程串行消费 `queue.Queue`；`chat_run` 用
    `chat_serial.get_chat_run_lock()`（进程级 `RLock`）与 text_chat worker
    互斥 —— 同一时刻只有一个 `chat_run` 发往 worker，避免并发轮询/停止
    冲突。`text_chat._run_one` 的 `chat_run` 同样包这把锁。
  - **裁剪在 worker 端**：主进程只透传 `history`，`compute_worker`
    `_chat_run_loop` 用真实 tokenizer 裁剪 + 拼 ChatML；主进程侧
    `run_chat_with_tools()` 完成工具执行与回喂循环（成功 wrap-up /
    失败回喂自然告知）后，把最终 answer 直接发微信。
  - **发送复用 `wechat_bot`**：回复走 `Wechat.send_text`，自动出现在
    "调试收发"消息列表（out 方向），与手动发送共用同一发送通道、线程安全。

* **与调试收发的关系**：两者数据流正交、可共存。用户消息（in）和 LLM
  自动回复（out）都显示在调试收发列表。**开关开启时手动发送被禁止**：
  - 前端：`imb-send-btn` 固定置灰（输入框仍可输入）；
  - 后端：`POST /api/wechat/send` 在 `llm_bridge.enabled == true` 时返回
    409「对接文字对话模型已开启，手动发送已禁用」。
  语义：开启时消息流完全交给「用户微信消息 → LLM → 自动回复」闭环，避免
  手动消息未进模型上下文造成上下文错乱。

* **API**（`app/api/wechat.py`）：
  - `POST /api/wechat/llm-bridge` `{enabled}` —— 开/关桥接；未加载 LLM 时
    开启 → 409；返回 `{ok, snapshot, llm_bridge}`。
  - `GET /api/wechat` snapshot 增加 `llm_bridge: {enabled, available,
    busy, active_sessions}`（`available` 供前端置灰）。

* **前端**（`app/static/index.html` + `app.js` `ImBridge`）：
  - 调试收发卡片顶部一行：checkbox + 提示。`available=false` → checkbox
    disabled + 红字提示「文本对话模型未启用，请在算力配置页加载 qwen3-4b」；
    `enabled` + `busy` → 「已对接，生成中…」。
  - `_toggleLlmBridge` 调 `POST /api/wechat/llm-bridge`；409 时 revert +
    提示。`send()` 顶部也兜底拦截（按钮已置灰，防 Cmd+Enter 绕过）。

* **限制**：LLM 单实例串行，多人同时发需排队（每条推理约 40-60s）；只发
  answer 不发 thinking；非文字消息（图/音/文件）回复「当前仅支持文字对话」。
  工具调用经 chat-agent 循环（见 "Chat 工具调用 (chat-agent)" 章节），
  微信用户同样可用。视频消息是例外 —— 分流到微信视频管理器（见下节）。


## 微信视频检索（wechat-video-manager + VideoSearch）

用户通过微信发送视频并提问（如"视频里有消费员吗"），LLM 调用
``VideoSearch`` 工具对该视频做语义检索并结合命中时间段回答。

* **架构分层**：提取不走 LLM 工具链路 —— "收到视频 → 提取"是确定性
  协议行为。``wechat_llm.handle_incoming`` 对 video 消息毫秒级分流到
  ``wechat_video_manager.enqueue_video()``（纯内存入队，CDN 下载在
  后台线程，不阻塞回调）；LLM 只暴露同步查询型工具。
* **管理器**（`app/services/wechat_video_manager.py` 单例）：
  - **单槽注册表** `{user_id: VideoEntry}` —— 每用户仅保留最新 1 个
    视频，新视频到达即覆盖旧的（删落盘文件 + 释放旧索引）；串行队列
    中被覆盖的任务按 seq 判定自动跳过。
   - 管线：SDK CDN 下载（`Wechat.download_message()` 同步透传，120s
     上限）→ 落盘 `uploads/wechat_videos/<uid>/` → PyAV 探测 → 逐片
     **6s 片 / 1s 重叠** embedding（复用 VCS 底层函数 `_probe_video_meta`
     / `_read_chunk_frames` / `_write_frames_to_shm` / `_cosine_similarity`
     + `get_session("swcap_video_retrieval")`，SHM 帧传递 + timeout 重试
     与 UI 页同款）→ ready。
   - **md5 缓存命中短路**（下载落盘后、PyAV 探测前）：流式算本地
     md5 → `get_video_cache().lookup(md5, model_id)` 命中即把
     LanceDB 里的切片索引恢复进 entry（秒级 ready，跳过全部
     embedding），播报与正常分析完成**完全一致**（用户无感知）；
     随后删刚下载的文件。所有消息形态（video / file）统一走
     "下载 → 本地 md5 → 查缓存"；file 形态自带的服务端 md5 不使用
     （与实际下发文件存在极小概率不一致，本地复核是唯一真源）。
   - **进度感知**：推送侧节点播报固定文案（📥 收到 / ⏳ 开始分析
     N 段 / ✅ 完成 / ⚠️ 失败）；拉取侧工具返回实时 progress。不做周期
     百分比消息（个人号高频发消息有风控风险，且协议无编辑消息能力、
     无法做原地刷新的进度条）。
   - **全成功（failures==0 且段数满）**：分析结果写入 LanceDB 缓存
     （见下节）+ 删本地视频文件（检索只回时间段，文件无用途；
     200MB/个，板上磁盘稀缺）；**部分成功**：⚠️ 警告播报带失败数 +
     首个失败原因，让用户明确感知不完整；不入缓存、不删文件（重发
     可重试失败段）。播报格式对命中 / 全成功完全一致。
   - shutdown hook `close_wechat_video_manager()` 在 main.py 注册。
* **LanceDB 分析结果缓存**（`app/services/wechat_video_cache.py`，
  v0.12）：key = 视频文件本地 md5；value = 切片 embedding 索引，
  **一行一片**（表 `wechat_video_index`：md5 / chunk_idx / 时间窗 /
  `vec` FixedSizeList(2048,float32) / filename / duration / 切片参数 /
  embedding_model / analyzed_at）。数据目录
  `app/services/uploads/lancedb/`（uploads/ 已 gitignore）。要点：
  - **embedding_model 隔离**：每行记录产生向量的模型 id；模型换代后
    旧缓存自动失配（lookup 按 md5 + embedding_model 过滤查不到），
    不会拿旧向量硬算余弦。
  - **仅全成功的分析入库**；部分成功只驻留内存。save 前 delete 同
    md5 再 add（幂等覆盖）。
  - **LRU 淘汰**：按视频条数（distinct md5）计，超上限删
    `analyzed_at` 最旧条目；`config.compute.wechat_video`
    热读 `cache_max_entries`（默认 100，≈1MB / 10min 视频）与
    `cache_enabled`（默认 true；false 时旁路 → 全部退回无缓存行为）。
  - **故障兜底**：lookup/save 任何异常（lancedb 不可用 / 表损坏 /
    维度不匹配）只 LOG warning 降级 —— 缓存层任何异常都不阻塞
    分析管线；`get_video_cache()` 返回 None 表示旁路。
  - **切片参数进缓存键（v0.12.1）**：`lookup/save` 均以
    `(md5, embedding_model, chunk_seconds, chunk_overlap_seconds)`
    定位条目 —— UI 检索页的 chunk/overlap 是用户可选的
    （3-6s / 1-2s），不同参数的索引时间窗与帧数完全不同，混用会
    时间戳错位。微信管线传固定 6/1；VCS 传用户选值；同视频不同
    参数是**独立条目**（各自占 LRU 槽，命中互不干扰）。
* **VCS 页接入缓存（v0.12.1）**：视频内容检索页与微信管线共享同一
  张表 + 同一单例：
  - `VideoContentSearchService.start_extract` 参数校验通过后算
    md5（本地文件直接流式算，无需下载）→ `lookup(...)` 命中即
    还原 index（`video_path` 换绑为当前 source basename，帧接口
    走当前源）→ state 秒级 READY（广播 state，前端进度条直接
    满格）；不命中走原有 worker。
  - `_worker_loop` 收尾 `slices_embedded >= slices_total`（全成功
    语义与微信一致）→ `cache.save(...)`；部分成功不存（与微信
    决策一致，重跑可补全）。
  - **$json "清空缓存"按钮**（`#vcs-clear-cache`，视频源 input 行）→
    `confirm()`（含 `cache/stats` 返回的条数提示）→
    `POST /api/video-content-search/cache/clear` → 幂等
    `{videos_cleared}`；`GET .../cache/stats` 返回
    `{enabled, max_entries, videos, rows}` 给确认框展示。内存中
    当前索引不受影响（缓存是持久备份层）。
  - **schema v0.12.2 加列 `source_path`**（入库时的本地视频路径）：
    微信路径固定存 `""` 占位（分析完删文件，无可取帧），VCS 传
    `self._source`；`_migrate_add_source_path` 用 lancedb 0.34
    `add_columns({"source_path": "''"})`（SQL-dict 形状，持久化）
    幂等补列，失败兜底 drop + 新 schema 重建（缓存可再生，旧条目
    frame 置灰不影响检索）。
* **VCS 勾选双路径（v0.12.2）**：缓存窗口让已分析视频可被直接勾选
  检索，无需重新上传/提取：
  - **窗口/数据**：`GET /api/video-content-search/cache/list`
    （`cache.list_videos()`：`[{md5, filename, source_path, chunk/
    overlap, slices, duration_s, analyzed_at}]`，按 analyzed_at 真
    倒序）渲染 `#vcs-cache-window`（max-height 滚动）—— 每行=
    (md5 × 切片参数) 三元键条目：checkbox + 文件名 + "Ns/Ms·N段" +
    行删除按钮（`DELETE .../cache/entry` 按行删，同 md5 其它参数档
    保留）。
  - **互斥**（前端 `vcsApplyMutualExclusion`）：路径 text 非空 → 勾
    选框全 disabled；勾选 ≥1 → `#vcs-source` / `#vcs-browse` /
    `#vcs-start` disabled。
  - **多选检索**：检索按钮在勾选 ≥1 时分叉
    `POST /query_multi {text, sim_threshold, top_k, min_results,
    entries[]}` —— **embed_text 只算一次**、逐条目 LanceDB lookup
    向量做余弦（`_score_hits` 与单视频 query 共用）；返回
    `groups[{filename, source_path, chunk params, hits, top_score,
    frame_enabled, miss}]`，前端 `vcsRenderMultiGroups` 按视频小节
    分组渲染（命中卡点击播放 → `vcsPlaySegment(idx, srcOverride)`
    用条目 source；`frame_enabled=false` 时卡片置灰"取帧失败"，
    点击提示）。**无状态批量语义** —— 不污染单例的 `_index` /
    `_source` / 状态机。
  - **宽松匹配全局化（v0.12.4）**：`query_multi` 改**两阶段**评分
    —— 阶段 1 每组严格检索（`min_results=0`，阈值过滤 + 组内
    top_k）累计全组合计达标数；阶段 2 仅当**合计零达标**且
    lenient 时，收集各组低于阈值候选**全局降序**取 top
    `min_results` 条按来源组落位（共享一份配额，标 below）——
    此前每组各自带 min_results 兜底（N 视频全无命中兜出 N×top_k
    条，"每视频各一份"与"一次总的"语义不符，用户决策改为全局
    判定：任一视频有达标命中则全部不兜底）。单视频 `query` 路径
    与微信 `VideoSearch` 工具的兜底不受影响（各自单组语义不变）。
  - **布局（v0.12.4）**：`#vcs-cache-window` 从"视频源"栏移出为
    独立 section（位于"操作"与"检索问题"之间），"清空缓存"按钮
    移入窗口 head（`.vcs-cache-actions` 右侧按钮组，警示 hover）；
    提取完成（state 首次 ready，`_cacheWinOnReady` 防抖块）自动
    清空 `#vcs-source` 路径文本框（仅成功清空，error/stopped 保留
    便于重试）并刷新缓存窗口 —— 引导检索走勾选路径。
  - **source_path 语义**：缓存条目存的入库时路径仅当**该文件仍在
    本机**才允许取帧/播放（`frame_enabled`，端点侧
    `os.path.isfile` 校验）—— 移走/删除的旧文件条目仍可纯文本
    检索（时间段 + 相似度）。
  - **渠道隔离（v0.12.3）**：`source_path` 双语义兼作渠道判别 ——
    微信管线固定 `""` 占位、web（VCS）恒存真实本地路径（均不随
    文件存亡漂移）。`cache/list`（web 窗口/勾选/行删除的数据源）
    调 `list_videos(channel="web")` → 只出 `source_path != ''` 条目；
    **微信条目不进 web 界面**。`lookup`/命中短路/LRU/`clear`（全清
    含微信条目，用户决策）不变 —— 跨入口命中复用保持（表共享、
    展示隔离）。同 md5 同参数两渠道互覆（save 幂等语义，检索等价）。
* **VideoSearch 工具**（`app/services/tools/video_search.py`，
  `requires_capability="swcap_video_retrieval"`）：**无调用时机限制**
  的四态结果型返回 —— `ok`（命中片段）/ `extracting`（附实时进度）/
  `downloading` / `no_video`，状态即合法业务结果而非错误，模型自由
  组织回复。真正的执行失败（会话缺失等）抛错走失败回喂。
  检索参数：query 内部截断 50 字；top_k=5（模型可调 1-10）；
  sim_threshold 固定 0.5 **不暴露给模型**，`min_results=3` 兜底启用
  —— 全低于阈值时返回最接近片段 + `all_below_threshold` / `hint`
  质量信号，由模型谨慎表述"未找到高度匹配的内容"。阈值不暴露的
  原因：模型对余弦分数无语义感知（盲调），且兜底已保证非空。
* **来源透传**：`ToolContext` 新增 `user_id` / `channel` 字段，
  `run_chat_with_tools()` 对应形参由 wechat_llm 传真实值（web 侧
  channel="web"、user_id 空 → 工具返回 no_video 引导走微信渠道）。
* **资源上限（可配置）**：`config.jsonc["compute"]["wechat_video"]`
  的 `max_size_mb`（默认 200）与 `max_duration_min`（默认 10），热读
  即时生效、非法值回退默认。三道校验：下载前按 SDK 元数据预检
  （video 形态 duration_ms / file 形态 size，互补覆盖）；落盘后大小
  复核；PyAV probe 后时长复核 —— 统一 raise → error 态 + ⚠️ 微信
  通知，超限文件不残留。
* **capability 预检（未启用整体不可用）**：`handle_incoming` 分流时
  先 `get_session("swcap_video_retrieval")` —— 未加载则**不入队**
  （加载后需重发视频），回固定文案「视频检索模型功能未被启用，需在
  算力配置页中加载启用后重新发送」，并向该用户 history 注入系统通知
  （此场景 VideoSearch 不在 schema 中，模型无工具可调，注入后才能
  准确解释原因而非含糊其辞；语言自适应）。manager `_process` 内部
  再取一次 session 作为最终防线（防预检后下载期间被卸载的 TOCTOU
  窗口），位置在 CDN 下载之前 —— 未启用时连流量都不浪费。
* **已知边界**：UI 页"视频内容检索"的单例服务与本管理器完全独立
  （互不复用索引）；真机 SDK CDN 下载稳定性需实际微信验证。
* **新视频抢占（协作式取消）**：分析进行中收到同用户的新视频时，
  `enqueue_video` 单槽覆盖即 `old.cancel.set()` + 删落盘文件；正在
  执行的旧管线在四处检查点静默退出（下载重试边界 / 下载完成后 /
  embedding 每片开头 / 终态写回前）—— 不发任何通知、不写终态，
  新视频随即开始分析。SDK 的 `download_message` 同步阻塞无法中途
  打断，最长滞后 = 单次 CDN 传输时长。


## FastDDS discovery — SIMPLE multicast (v0.6+)

v0.5 引入了 wdndds_daemon 走 SERVER/CLIENT 协议绕开 FastDDS 2.9 SPDP
multicast bug。v0.6 改成 **FastDDS SIMPLE discovery** 直接走组播
239.255.0.1:7400 —— 既然 wdnwebui 现已自带源码编译的 FastDDS 3.6.2
(v0.5 时代的 bug 不存在),daemon 这一层就成了纯运维负担,没有功能收益。

### 架构

```
        ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐
        │  main    │  │ worker   │  │ worker   │  │ worker   │  (FastDDS
        │ (wdnwebui│  │ qwen3-vl │  │ qwen3-tts│  │ qwen3-4b │   SIMPLE)
        │  main)   │  │  -4b     │  │          │  │          │
        └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘
             │              │             │             │
             └──────────────┴──── UDP multicast 239.255.0.1:7400 ───────
                                          │
                            (LAN: 任意 L2 节点互通)
```

* **零配置**：每台板子启 `wdnwebui` 即自动被同网段其它板子看到；
  不需要配 daemon 地址、不需要 systemd 单元、不需要端口管理。
* **数据平面不变**：仍走 cshim 的 SHM（同机）+ UDPv4（LAN）+ TCPv4（跨子网）
  builtin transports。
* **请求 / 回复主题**：`wdndds/rpc/req/<model_id>` +
  `wdndds/rpc/rep/<model_id>`，IDL 见 `wdndds/idl/compute.idl`。
* **participant 命名约定**：主进程 = `wdnwebui-main-<device_name>`，
  worker = `compute_worker-<device_name>-<model_id>`，
  comm = `worker:<alias>`（见 `MODEL_PRESETS[model_id]["worker_name"]`）。
  v0.8 起 `device_name` 缺省 fallback 到 `socket.gethostname()`，
  participant_name 永远非空。

### 跨子网 / 组播被屏蔽时的 fallback

消费级 WiFi（AP 隔离、访客网络）、Docker bridge、严格 VLAN 经常丢组播。
每个 wdnwebui 进程读 `WDNDDS_INITIAL_PEERS` 环境变量（逗号分隔的 IP
列表），由 cshim 的 `_install_default_dds_profile()` 写入
FastDDS XML profile 的 `initialPeersList`，作为 unicast fallback：

```bash
# 在每台板子设上对端 IP（v0.6 新增，无需 daemon）
WDNDDS_INITIAL_PEERS="192.168.1.10,192.168.1.11,192.168.1.12" \
    .venv/bin/python run.py
```

XML profile 路径：`$XDG_RUNTIME_DIR/wdndds_default_profile.xml`
（找不到时 fallback 到 `/tmp/`）。除 `initialPeersList` 外,profile 还
显式设了:
* `<discoveryProtocol>SIMPLE</discoveryProtocol>`
* `<initialAnnouncements><count>10</count><period>100ms</period></initialAnnouncements>`
  — 比 FastDDS 默认（5×100ms）更激进,冷启动发现更早收敛。
* `<leaseDuration_announcementperiod><sec>1</sec>…</leaseDuration_announcementperiod>`
  — 比 FastDDS 默认（3s）更短,周期发现滞后更小。
* 默认 `initialPeersList` 永远含 `127.0.0.1:7410` loopback,同机进程
  即使 multicast 坏掉也能互相发现。

### 调试 / 监控

* **「系统信息 → 在网设备」**：前端拉 `GET /api/system-monitor` 中的
  `discovery` 字段（`{protocol, multicast_group, own_guid, peer_count,
  peers:[{guid, name, discovered_at}], wdndds_initial_peers}`）。
  数据源是 `ComputeRegistry.list_peers()`,由 `_ensure_hub()` 启动时
  给 Hub 装的 `set_peer_listener()` 实时回调维护。
* **verify 脚本** (`scripts/verify_simple_discovery.py`)：
  跨机 / 同机纯 SIMPLE 端到端检查,带退出码（0=peer+RPC OK,
  1=peer OK 但 RPC 失败, 2=无 peer, 3=cshim 不可用）。
* **无 daemon HTTP API**：v0.5 时代 daemon 的 `/status` / `/peers`
  /start /stop 端点全部下线。

### 已知限制

* 跨机 cold-start 时延：从 SPDP 首次通告到 peer 可见 < 1s（实测本机
  0.20s / 同 LAN 跨机 0.20s）。lease announcement period = 1s 意味着
  peer 失联最迟 ~3s（lease 20s 之内）后被探测到。
* FastDDS 3.6 rrpc API 仍 stub（参见 Fast-DDS `DomainParticipantImpl.cpp`）：
  wdndds_cshim 仍走 raw `DataReader` / `DataWriter` 模拟 Request/Reply。
* **冷启动 NIC 时序坑（真机实测）**：systemd autostart 拉起的
  compute_worker 若早于网卡就绪启动，cshim 的 interfaceWhiteList pin
  会绑错 interface → 该 worker 进程活着但 DDS 数据面完全不可达
  （probe / bind 全 timeout，worker 日志零请求痕迹）。判别方法：
  手动 kill 后重新 spawn（NIC 已就绪）即恢复。现有防线：① 主进程
  `_ensure_hub` 的 `wait_for_network_ready` / socket fallback；
  ② `bind_instance._run_load` 前调 `_refresh_rpc_clients()` recreate
  handle（对 spawn→bind 的时序 race 兜底，destroy 拿 requester._lock
  防 probe 线程 UAF）。待办：worker systemd 单元加
  `After=network-online.target`。
* **get_session 本机优先（v0.8.30）**：`ComputeRegistry.get_session`
  同 capability 多实例 running 时按 `spawned` / `adopted_local` 优先
  于 `adopted`（LAN peer）选择。原因：视频类消费方
  （VCS / wechat_video_manager）用 `/dev/shm` path-list 传帧，
  **远端 worker 读不到本机 tmpfs** —— 命中远端实例会逐片
  `cv2.imread exists=False` read操作全灭；
  `rpc_session_proxy.embed_video` 的 path-list分支**没有**
  is_remote 守卫（ndarray 分支有 base64 fallback，
  path-list 分支跨机就是不可用）。无本机 running 时才退回
  LAN peer。每次调用打 `get_session(cap): LOCAL/REMOTE` 日志。
* **主进程 fd 防线（v0.8.30）**：`run.py main()` 开头
  `setrlimit(RLIMIT_NOFILE soft→hard)`。reboot 后手动 shell /
  systemd 拉起的主进程默认 soft=1024，而主进程长寿命持有 FastDDS
  endpoint、uvicorn 连接、journalctl WS 等 —— reload 的
  multiprocessing.spawn 子进程与 Popen 出的 compute_worker 都继承
  本 limit（worker main() 另有自己的 setrlimit 兜底，见
  "compute_worker.py" 段）。**本仓库无生产 systemd unit**
  （install_autostart.sh 未部署的主机 reboot 后 run.py 由手动
  shell 拉起），部署环境重启后务必确认主进程 `Max open files`
  为 524288。
* **shim 写端 fail-loud**（`_write_frames_to_shm`，v0.8.30）：每个
  帧文件写后立即 `os.stat` 校验（size>0），失败 raise
  `RuntimeError("/dev/shm 写入失败…/立即消失…")` → 走早前加的
  失败聚合 → 微信用户直接收到真因。此前"写端静默、worker imread
  None"的三层吞错即使修复后仍难以诊断"文件消失"类环境事故。
* **cshim Service 对象必须显式销毁**：`_probe_service` 曾只 destroy
  requester 留下 svc —— boot auto-adopt 期累积约 75 个 FastDDS
  endpoint 集合。已修复为双销毁；新增 probe/临时 requester 路径时
  注意同样问题。
* wdndds_daemon 完全删除；旧版 systemd unit
  `wdndds-daemon.service` 会因 unit 文件不存在报 `Unit not found`，
  `install_autostart.sh --uninstall` 会一并清掉。
