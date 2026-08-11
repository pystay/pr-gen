"""开源免费版用户注册：创建用户 + 永久免费订阅（全部功能开放）。

邮箱 Fernet 加密存储 + pepper 哈希查询键；重复邮箱返回已有用户（幂等）。
"""

from __future__ import annotations

import hashlib
import hmac
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .config import Settings
from .db import Database, now
from .security import Encryptor

router = APIRouter(prefix="/api/auth", tags=["auth"])

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def email_hash(email: str, pepper: str) -> str:
    """邮箱确定性哈希（查询键；原文仍走 Fernet 加密存储）。"""
    return hashlib.sha256((email + pepper).encode("utf-8")).hexdigest()


def _pepper(settings: Settings) -> str:
    """从 DATA_ENCRYPTION_KEY 派生 pepper（确定性，无需额外配置）。"""
    return hashlib.sha256(settings.data_encryption_key.encode("utf-8")).hexdigest()[:32]


def _require_key(request: Request, settings: Settings) -> bool:
    """内部端点鉴权：配置了 USAGE_API_KEY 时必须匹配；未配置时仅本地模式放行。"""
    key = request.headers.get("X-API-Key", "")
    if settings.usage_api_key:
        return hmac.compare_digest(key, settings.usage_api_key)
    return settings.is_local_mode


@router.post("/register")
async def register(request: Request):
    """注册用户：创建用户记录并激活永久免费订阅（全部功能开放）。"""
    settings: Settings = request.app.state.settings
    db: Database = request.app.state.db
    enc: Encryptor = request.app.state.encryptor

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid json"})

    email = str(payload.get("email", "")).strip().lower()
    if not EMAIL_RE.fullmatch(email):
        return JSONResponse(status_code=422, content={"error": "邮箱格式不正确"})

    existing = db.get_user_by_email(email_hash(email, _pepper(settings)))
    if existing:
        return {
            "ok": True,
            "user_id": existing["id"],
            "already_registered": True,
            "subscription": db.get_subscription(str(existing["id"])),
        }

    user_id = db.create_user(email_hash(email, _pepper(settings)), enc.encrypt(email))
    sub = db.ensure_free_subscription(user_id)
    return {"ok": True, "user_id": user_id, "already_registered": False,
            "subscription": sub}


@router.delete("/me/{user_id}")
async def delete_user(user_id: int, request: Request):
    """GDPR/个保法：删除用户、订阅与用量记录（需内部 API key）。"""
    settings: Settings = request.app.state.settings
    if not _require_key(request, settings):
        return JSONResponse(status_code=403, content={"error": "forbidden"})
    db: Database = request.app.state.db
    if not db.get_user(user_id):
        return JSONResponse(status_code=404, content={"error": "用户不存在"})
    db.delete_user(user_id)
    return {"ok": True, "deleted": True}
