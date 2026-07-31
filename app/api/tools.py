"""工具列表页 (sidebar “工具列表”) 的只读 API。

``GET /api/tools`` 返回所有内置工具的元数据 + 能力可用性。
"""
from __future__ import annotations

from fastapi import APIRouter

from app.services.compute_registry import get_registry
from app.services.tools import get_tool_registry


router = APIRouter(tags=["tools"])


@router.get("/tools")
async def list_tools() -> dict:
    reg = get_tool_registry()
    out = []
    for spec in reg.list_specs():
        available = True
        reason = ""
        if spec.requires_capability:
            try:
                get_registry().get_session(spec.requires_capability)
            except RuntimeError as e:
                available = False
                reason = str(e)
        out.append({
            "key":                 spec.key,
            "name":                spec.name,
            "description":         spec.description,
            "requires_capability": spec.requires_capability,
            "available":           available,
            "reason":              reason,
        })
    return {"tools": out}
