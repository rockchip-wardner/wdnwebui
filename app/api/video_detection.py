"""Video detection placeholder endpoints."""
from fastapi import APIRouter

router = APIRouter(tags=["video-detection"])


@router.get("/video-detection")
async def get_video_detection() -> dict:
    return {
        "placeholder": True,
        "section": "video-detection",
        "sources": [],
        "active_model": None,
        "note": "占位接口：视频检测逻辑接入后在此返回",
    }