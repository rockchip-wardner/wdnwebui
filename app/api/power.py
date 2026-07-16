"""Power control placeholder endpoints."""
from fastapi import APIRouter

router = APIRouter(tags=["power"])


@router.get("/power")
async def get_power() -> dict:
    return {
        "placeholder": True,
        "section": "power",
        "state": "unknown",
        "voltage_v": None,
        "note": "占位接口：电源控制逻辑接入后在此返回",
    }