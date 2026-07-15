"""Entry point for pyrkaiweb service.

Usage:
    python run.py
    # or
    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
import uvicorn

from app import __version__


def main() -> None:
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )


if __name__ == "__main__":
    main()