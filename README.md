# wdnwebui

A Python web service that exposes the Rockchip platform SDKs
([`wdnav`](https://github.com/rockchip-wardner/wdnav) — MPP / RGA2 / V4L2 video,
[`wdnai`](https://github.com/rockchip-wardner/wdnai) — RKNN LLM / VLM) over
HTTP via FastAPI. Single-page UI at `http://<host>:8000/` with four
sections: system monitor, video detection, system logs, and power
control (shutdown / reboot).

## Requirements

- **Python 3.11+** (3.11.2 confirmed working)
- **Debian / Ubuntu** host (Debian PEP 668 blocks system pip — we use a venv)
- **Git + SSH** access to `git@github.com` for the SDKs
  (ask ops for the deploy key if `git clone` fails)
- For **power control** (关机 / 重启 buttons) the service needs to run as
  root, otherwise those buttons will error out with EPERM

For the video detection page on a real Rockchip target, the host
needs `librockchip_mpp` and `librga` from the BSP at runtime.
`import wdnav` works on any host; `import wdnav.video` only works on the
target.

## 1. Clone

```bash
git clone <this-repo-url> wdnwebui
cd wdnwebui
```

## 2. Create the virtualenv

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip          # optional but recommended
```

## 3. Install web dependencies

```bash
pip install -r requirements.txt
```

That pulls FastAPI, Uvicorn, Pydantic, psutil, opencv-python-headless,
python-multipart, **`gguf`**, **`transformers`**, **`tokenizers`** and
**`v4l2`**.

> ℹ️  These three (`gguf` / `transformers` / `tokenizers`) are listed here
>  because the SDKs are installed with `--no-deps` (§4) and therefore never
>  pull their own dependencies in. `scripts/check_sdk_deps.py` (§4c) audits
>  exactly this — run it after any SDK install or update.

- `gguf` — used by `wdnai.gguf_tokenizer` to parse the
  `*.tokenizer.gguf` files that ship with each RKNN model. The
  `wdnai` SDK imports it but doesn't declare it as a dependency, so
  we pin it here.
- `transformers` / `tokenizers` — `wdnai.gguf_tokenizer` loads every
  RKNN model's `*.tokenizer.gguf` through them, so a model can't even
  *start* without them (`ModuleNotFoundError: No module named
  'tokenizers'` → 「启动失败」 on the compute page). The `wdnai` SDK
  declares both in its `pyproject.toml`, but it is installed with
  `pip install --no-deps -e` (§4b), which skips dependency resolution
  entirely — so they have to be listed here. No PyTorch needed: only
  the tokenizer half of `transformers` is used.
- `v4l2` — the third-party PyPI package (pure-ctypes Video4L2
  bindings) that `wdnav/src/wdnav/v4l2.py` imports at module level.
  The `wdnav` SDK declares it, but is installed with
  `pip install --no-deps -e` (§4b), so it never lands in the venv on
  its own. Without it every HDMI-IN snapshot request fails with
  `ModuleNotFoundError: No module named 'v4l2'` → 503.

  > ⚠️ Upstream `v4l2` 0.2 (2014) still contains the Python-2 idiom
  > `range(...) + [...]`, which raises **at import time** on Python 3:
  > `TypeError: unsupported operand type(s) for +: 'range' and 'list'`.
  > That is *not* an `ImportError`, so a plain `pip install v4l2`
  > turns the clean 503 into an unhandled 500. Run the patcher once
  > after installing:
  >
  > ```bash
  > .venv/bin/python scripts/fix_v4l2_py3.py
  > ```
  >
  > It is idempotent and self-verifying; `update_rkwhl.sh` and
  > `bootstrap_wdndds_native.sh` already call it for you.
- `wechatbot-sdk` — async client for the 微信 iLink bot; the synchronous
  wrapper that drives the im-bridge page lives in
  `app/services/wechat_bot.py` (inlined from `wdndds.Wechat` in 0.5+).

> ℹ️  As of `wdnai ≥ 0.4.0`, the `rknn3-toolkit-lite` wheel is **no
> longer** a wdnwebui dependency. `wdnai` ships its own vendored
> ctypes shim (`wdnai._librknn`) that drives
> `/usr/lib/librknn3_api.so` directly. The kernel-mode driver
> (`/dev/rknn3`) and the userspace libs (`librknn3_api.so` + helper
> binaries `rknn3_startup`, `rknn3_model_test`, …) still come from
> the BSP image and must be present on the host. On a fresh target
> that means running the BSP installer first; on the standard
> Rockchip images they're pre-installed. The web service will boot
> fine without them, but the NPU section of the system monitor and
> the VLM detector will report "offline" / fail to load.

## 4. Install the SDKs

These are **not** in `requirements.txt` because they aren't on PyPI.
Install them by hand after the SSH key is set up:

```bash
pip install -e "git+ssh://git@github.com/rockchip-wardner/wdnav.git#egg=wdnav"
pip install -e "git+ssh://git@github.com/rockchip-wardner/wdnai.git#egg=wdnai"
pip install -e "git+ssh://git@github.com/rockchip-wardner/wdndds.git#egg=wdndds"
```

If a command fails with `Permission denied (publickey)`, the host either
can't reach `github.com` or the SSH key isn't loaded.

> ℹ️  On an isolated board with no internet access, point these at the
> internal mirror instead (`git+ssh://git@10.10.10.10/wardner/<pkg>.git`) —
> that's what `update_rkwhl.sh` (§4b) uses for field deployments.

After install you'll find them at `.venv/src/wdnav/` and `.venv/src/wdnai/`.

## 4b. Keep the SDKs up to date

After pulling upstream changes (or for routine sync), run:

```bash
./update_rkwhl.sh
```

It `git pull`s every editable package under `.venv/src/` — the four SDKs
(wdnav / wdnai / wdndds / wdnhid) and the two card plugins
(wdnapps_video_analyzer / wdnapps_hdmi_processor) — and runs
`pip install --no-deps -e` on each. That is what actually triggers
the `.so` rebuild hooks for wdndds and wdnav. HEAD advances → rebuild
(~5–20s per SDK). Packages whose HEAD didn't move are left untouched.

For the two plugin packages there are no `.so` to assert, so the script
instead checks that every card is really registered: each
`wdnwebui.cards` entry point must resolve to a card directory whose
`manifest.json` `"id"` matches the entry-point name and whose `static/`
exists. That catches the silent failure where pip succeeds but the host
drops every card at boot. For manual single-package sync,
`pip install --no-deps -e .venv/src/<pkg>` is the minimum; on a fresh
host, `sudo ./bootstrap_wdndds_native.sh` is still required for apt
deps + native lib vendoring.

Once every package has been processed, the script audits the SDKs'
declared dependencies (§4c) and fails the run if any of them is missing or
unimportable — otherwise a `--no-deps` install would only reveal the gap
when someone opens the affected page.

## 4c. Audit the SDK dependencies

The SDKs are installed with `pip install --no-deps -e`, so **their own
declared dependencies never land in the venv**. That is deliberate — the
private packages aren't on PyPI (`wdndds` → `wdnai`, the card plugins →
`wdnhid` / `wdnav`), letting pip resolve them fails outright; resolving
would also pull `opencv-python` (the GUI build) in next to the pinned
`opencv-python-headless`, and the two fight over the same `cv2` package;
`av` needs a hand-picked build; and the target board often has no PyPI
access at all.

The sharp edge is that a missing dependency is only discovered when the
feature that needs it is used, and the symptom is nowhere near the cause:

| missing | symptom |
|---|---|
| `v4l2` | every HDMI-IN snapshot → 503 `No module named 'v4l2'` |
| `tokenizers` / `transformers` | compute page says 「模型启动失败」 |

So run the audit after installing or updating the SDKs:

```bash
.venv/bin/python scripts/check_sdk_deps.py
```

It reads `[project].dependencies` from every SDK under `.venv/src/`, checks
each entry against the **installed** distributions (`importlib.metadata` —
never the index, so it works offline), verifies the version constraint, and
then **really imports** the module. That last step matters: "installed but
broken" is a real class here — upstream `v4l2` 0.2 raises `TypeError` at
import time under Python 3 (§3), which `importlib.util.find_spec` cannot
see.

The exit code is the contract:

| exit | meaning |
|---|---|
| 0 | every declared dependency is installed and importable |
| 1 | something is missing / version-mismatched / unimportable |
| 2 | the SDK source tree or the venv could not be located |

`update_rkwhl.sh` runs it after all packages are processed and fails the
whole run if it reports a problem; `bootstrap_wdndds_native.sh` runs it too
and warns loudly (it doesn't abort — its own job, the wdndds `.so`, is
already done by then). Each reported dependency comes with the command that
fixes it.

## 5. Run the service

Development (auto-reload on Python edits):

```bash
python run.py
# → Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```

For power control to actually work, run with sudo (the shutdown /
reboot syscalls need root):

```bash
sudo .venv/bin/python run.py
```

Production-style (no auto-reload, single worker):

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

## 6. Verify

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok"}

curl http://127.0.0.1:8000/api/power
# {"uptime_seconds": ..., "uptime_human": "...", "is_root": true}
```

If `is_root` is `false`, the web UI will show a yellow warning banner
at the top — power actions are disabled until you restart with sudo.

Open `http://<host>:8000/` in a browser for the full UI. The static
assets are served with `Cache-Control: no-cache` in dev so JS / CSS
edits show up on hard-refresh without restart.

## Endpoints at a glance

| Method | Path                                | What it does                              |
| ------ | ----------------------------------- | ----------------------------------------- |
| GET    | `/`                                 | Single-page UI                            |
| GET    | `/health`                           | Liveness check                            |
| GET    | `/api/system-monitor`               | CPU / memory / NPU snapshot               |
| GET    | `/api/system-logs?unit=&priority=&lines=` | Last N journal entries              |
| WS     | `/api/system-logs/ws?unit=&priority=` | Live journal tail                       |
| POST   | `/api/video-detection/upload`       | Stage a local file for the file picker    |
| POST   | `/api/video-detection/open`         | Open an RTSP / RTMP / HLS / file source   |
| POST   | `/api/video-detection/control`      | `play` / `pause` / `stop`                 |
| POST   | `/api/video-detection/close`        | Close the current source                  |
| POST   | `/api/video-detection/prompt`       | Start VLM detection on the live frames    |
| POST   | `/api/video-detection/prompt/stop`  | Stop the VLM detector                     |
| GET    | `/api/video-detection/events`       | Detection event history                   |
| WS     | `/api/video-detection/ws`           | JPEG frame stream + status + events       |
| GET    | `/api/video-detection/fs?path=`     | Server-side directory listing             |
| POST   | `/api/tts/start`                   | Kick off a TTS synthesis (`{text, ref_speaker, language, volume, ...}`) |
| POST   | `/api/tts/stop`                    | Stop the current TTS run                   |
| GET    | `/api/tts`                         | TTS state, frames, audio_sec, elapsed      |
| GET    | `/api/tts/history`                 | Recent output WAVs                         |
| WS     | `/api/tts/ws`                      | Snapshot pushed on every state change      |
| GET    | `/api/power`                        | Uptime + `is_root`                        |
| POST   | `/api/power` `{action:"shutdown"\|"reboot"}` | Run host shutdown / reboot       |

## Layout

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

## Troubleshooting

- **`shutdown: command not found`** when clicking 关机 — the binary is
  in `/sbin/` but the service's PATH doesn't include it. Start with
  `sudo` so /sbin is in PATH, or `PATH=$PATH:/sbin:/usr/sbin python run.py`.
- **Video page errors with `wdnav` / `RKNN` import errors** on a non-RK
  dev box — expected; the SDKs fall back gracefully and the rest of
  the UI keeps working.
- **`Permission denied` on `.venv/src/*` .pyc files** — the venv was
  created by a different uid (e.g. once ran with sudo). Run with
  `python -B run.py` to skip bytecode writes, or `chown -R $USER .venv`.
- **`create_app()` / router 404** — make sure `app/api/__init__.py`
  imports every submodule (`from . import power, ...`); without it
  the package object is empty and the routes are silently missing.