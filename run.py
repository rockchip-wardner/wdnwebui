"""Entry point for pyrkaiweb service.

Usage:
    python run.py
    python run.py --config /etc/pyrkaiweb/config.json
    # or
    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
import argparse
import os
from pathlib import Path

import uvicorn

from app import __version__


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pyrkaiweb",
        description=f"pyrkaiweb service v{__version__}",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parent / "config.json",
        help="Path to the service config.json "
             "(default: <repo>/config.json). Propagated to the app via "
             "the PYRKAIWEB_CONFIG env var.",
    )
    args, _unknown = parser.parse_known_args()

    config_path = args.config.expanduser()
    if not config_path.is_absolute():
        config_path = (Path(__file__).resolve().parent / config_path).resolve()
    else:
        config_path = config_path.resolve()

    # Propagate to the app via env so app.services.config picks it up
    # before any FastAPI route is hit.
    os.environ["PYRKAIWEB_CONFIG"] = str(config_path)

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )


if __name__ == "__main__":
    main()