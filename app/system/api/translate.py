"""板端翻译代理 —— 前端唯一的翻译入口。

``POST /api/translate``
    入参 ``{"segments": ["系统运行正常", ...], "source": "zh-CN", "target": "en"}``
    出参 ``{"translations": [...], "provider": "mymemory", "cached": 12, ...}``
    —— ``translations`` 与入参**同序等长**。

``GET /api/translate/status``
    当前 provider、缓存条目数、前端该用哪个源语言。给前端启动时读一次
    (顺带在界面上显示「由谁在翻」),也方便运维排查。

为什么是同步 def 而不是 async
-----------------------------
翻译走的是阻塞的 HTTP 调用(provider 内部用标准库 urllib)。FastAPI 会把
同步端点丢到线程池里跑,所以这里直接写 ``def``,不用手写线程池,也不会
堵住事件循环。
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.core.translate import (
    PROVIDER_LABELS,
    QuotaExceeded,
    TranslationError,
    get_translate_service,
)

LOG = logging.getLogger("api.translate")

router = APIRouter(tags=["translate"])

# 单次请求的条数 / 长度上限 —— 这是**对外**的护栏,防止有人拿这个端点
# 当通用翻译机使,把板子的网络和配额打爆。
MAX_SEGMENTS = 400
MAX_SEGMENT_CHARS = 2000
MAX_TOTAL_CHARS = 40000

# 语言码:ISO 639-1 / RFC3066(``zh-CN`` / ``en`` / ``ja`` …)
_LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$")


class TranslateRequest(BaseModel):
    """一次翻译请求。"""

    segments: list[str] = Field(..., min_length=1, max_length=MAX_SEGMENTS)
    source: str = "zh-CN"
    target: str

    @field_validator("source", "target")
    @classmethod
    def _check_lang(cls, v: str) -> str:
        v = (v or "").strip()
        if not _LANG_RE.match(v):
            raise ValueError(f"语言代码不合法: {v!r}")
        return v

    @field_validator("segments")
    @classmethod
    def _check_segments(cls, v: list[str]) -> list[str]:
        total = 0
        for item in v:
            if not isinstance(item, str):
                raise ValueError("segments 必须是字符串数组")
            if len(item) > MAX_SEGMENT_CHARS:
                raise ValueError(f"单条文案过长(上限 {MAX_SEGMENT_CHARS} 字)")
            total += len(item)
        if total > MAX_TOTAL_CHARS:
            raise ValueError(f"一次请求的总长度超限(上限 {MAX_TOTAL_CHARS} 字)")
        return v


@router.post("/translate")
def translate(req: TranslateRequest) -> dict:
    """把一批文案翻成目标语言。"""
    service = get_translate_service()
    try:
        translations, stats = service.translate_segments(
            req.segments, req.source, req.target
        )
    except QuotaExceeded as exc:
        # 429:配额用尽 —— 跟「服务挂了」(502)要分开,前端提示也不一样
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except TranslationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # 兜底:provider 内部抛了别的异常也不能 500 裸奔
        LOG.exception("translate: 未预期的异常")
        raise HTTPException(status_code=502, detail=f"翻译失败: {exc}") from exc
    return {"translations": translations, **stats}


@router.get("/translate/status")
def translate_status() -> dict:
    """当前 provider 链与缓存状态。"""
    service = get_translate_service()
    names = service.provider_names
    return {
        "provider": service.provider_name,
        "providers": names,
        # 每家的中文显示名 —— 前端直接用,不用自己维护一张表
        "provider_labels": {
            name: PROVIDER_LABELS.get(name, name) for name in names
        },
        # 还在冷却的 provider 及剩余秒数(429 之后会看到它在里面)
        "cooldown": service.cooldown_state(),
        "cache_size": service.cache_size(),
        "source": "zh-CN",
    }
