"""System logs placeholder endpoints."""
from fastapi import APIRouter

router = APIRouter(tags=["system-logs"])


@router.get("/system-logs")
async def get_system_logs() -> dict:
    return {
        "placeholder": True,
        "section": "system-logs",
        "entries": [],
        "note": "占位接口：日志拉取/流式接口接入后在此返回",
    }