# wdnwebui

A Python web service that exposes the Rockchip platform SDKs
([`wdnav`](https://10.10.10.10/wardner/wdnav) — MPP / RGA2 / V4L2 video,
[`wdnai`](https://10.10.10.10/wardner/wdnai) — RKNN LLM / VLM) over
HTTP via FastAPI. Single-page UI at `http://<host>:8000/` with four
sections: system monitor, video detection, system logs, and power
control (shutdown / reboot).

## Requirements

- **Python 3.11+** (3.11.2 confirmed working)
- **Debian / Ubuntu** host (Debian PEP 668 blocks system pip — we use a venv)
- **Git + SSH** access to `git@10.10.10.10` for the internal SDKs
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
python-multipart, **`rknn3-toolkit-lite`**, and **`gguf`**.

- `rknn3-toolkit-lite` — public Rockchip PyPI package; provides the
  Python `rknn3lite` module and the `.so` bindings used by
  `app/services/detector.py` for VLM inference.
- `gguf` — used by `wdnai.gguf_tokenizer` to parse the
  `*.tokenizer.gguf` files that ship with each RKNN model. The
  `wdnai` SDK imports it but doesn't declare it as a dependency, so
  we pin it here.

> ⚠️  `rknn3-toolkit-lite` only ships the **Python** half of the
> runtime. The kernel-mode driver (`/dev/rknn3`), the userspace libs
> (`/usr/lib/librknnrt.so`, `librknn3_api.so`), and the helper
> binaries (`/usr/bin/rknn3_startup`, `rknn3_model_test`, …) come
> from the BSP image and must already be on the host. On a fresh
> target that means running the BSP installer first; on the standard
> Rockchip images they're pre-installed. The web service will boot
> fine without them, but the NPU section of the system monitor and
> the VLM detector will report "offline" / fail to load.

## 4. Install the internal SDKs

These are **not** in `requirements.txt` because they're behind SSH and
not on PyPI. Install them by hand after the SSH key is set up:

```bash
pip install -e "git+ssh://git@10.10.10.10/wardner/wdnav.git#egg=wdnav"
pip install -e "git+ssh://git@10.10.10.10/wardner/wdnai.git#egg=wdnai"
pip install -e "git+ssh://git@10.10.10.10/wardner/wdndds.git#egg=wdndds"
```

If either command fails with `Permission denied (publickey)`, the host
either can't reach `10.10.10.10` or the SSH key isn't loaded. There
is no public fallback — these SDKs aren't published.

After install you'll find them at `.venv/src/wdnav/` and `.venv/src/wdnai/`.

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