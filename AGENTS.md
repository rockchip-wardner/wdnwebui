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
│   ├── __init__.py       # __version__ = "0.1.0"
│   └── main.py           # create_app() factory + module-level `app`
├── requirements.txt      # web deps only; SDKs come from private git
├── .gitignore            # excludes .venv/, __pycache__/, .env, etc.
└── .venv/                # editable installs of rkv, rkai live under .venv/src/
```

`app/__init__.py` owns the single version string; bump it there when the
service version changes.

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

## When you change things, also update

| You changed…                          | Then update…                                              |
| ------------------------------------- | --------------------------------------------------------- |
| Service version                       | `app/__init__.py` `__version__` (single source of truth)  |
| Default bind host / port              | `run.py` `main()`                                        |
| Public SDK dependency                 | Re-run the two `pip install -e` lines above              |
| Anything that needs a new endpoint    | Add a route inside `create_app()` in `app/main.py`        |