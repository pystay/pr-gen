"""运维 cron 端点（开源免费版）。

保留：每月用量重置、成本告警检查由 admin 侧触发。
移除：GitHub Marketplace 订阅事件、订阅过期降级（无付费订阅概念）。
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .config import Settings
from .db import Database

router = APIRouter(prefix="/api/github", tags=["cron"])


def _cron_allowed(request: Request, settings: Settings) -> bool:
    """cron 端点鉴权：配置了 USAGE_API_KEY 时必须匹配；未配置仅本地模拟放行。"""
    if settings.usage_api_key:
        return hmac.compare_digest(request.headers.get("X-API-Key", ""),
                                   settings.usage_api_key)
    return settings.is_local_mode


@router.post("/cron/reset-usage")
async def cron_reset_usage(request: Request):
    """每月 1 日用量重置（cron/调度器调用）。"""
    settings: Settings = request.app.state.settings
    if not _cron_allowed(request, settings):
        return JSONResponse(status_code=403, content={"error": "forbidden"})
    db: Database = request.app.state.db
    count = db.reset_free_usage()
    db.add_notification("log", f"free usage reset: {count} rows cleared")
    return {"ok": True, "cleared": count}
