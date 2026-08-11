"""GitHub Marketplace Webhook：订阅激活 / 取消 / 变更 / 退款。

事件流（GitHub marketplace_purchase）：
- purchased       → 创建/更新订阅为 active，记录订单
- cancelled       → 标记 cancelled，expiry_date = effective_date，到期后自动降级 Free
- pending_change  → 记录待生效变更（日志）
- changed         → 计划立即变更（如升级 Pro → Team）
- refunded        → 记录退款通知（订阅状态由 GitHub 后续 cancelled 事件收敛）

幂等：events 表按 event_id 唯一，重复投递直接忽略。
到期降级：懒检查（读取订阅时判断）+ /api/cron/downgrade 定时任务双保险。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .config import Settings
from .db import Database, now
from .security import verify_webhook_signature

logger = logging.getLogger("prgen.webhook")

router = APIRouter(prefix="/api/github", tags=["github"])

ACTION_ACTIVATE = "purchased"
ACTION_CANCELLED = "cancelled"
ACTION_CHANGED = "changed"
ACTION_PENDING_CHANGE = "pending_change"
ACTION_REFUNDED = "refunded"


def _parse_date(value: str | None) -> float | None:
    """ISO8601 → epoch 秒；解析失败返回 None。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _map_plan(payload: dict, settings: Settings) -> str:
    """GitHub plan → 内部计划（free/pro/team）。默认按名称关键词匹配，Pro 兜底。"""
    plan = (payload.get("marketplace_purchase") or {}).get("plan") or {}
    name = str(plan.get("name", "")).lower()
    price = plan.get("unit_price") or plan.get("price") or 0
    if "team" in name:
        return "team"
    if "pro" in name or float(price) > 0:
        return "pro"
    return "free"


def _extract_account(payload: dict) -> tuple[str, str, str]:
    """返回 (account_id, account_type, account_login)。"""
    sender = payload.get("sender") or {}
    acct = (payload.get("marketplace_purchase") or {}).get("account") or {}
    account_id = str(acct.get("id") or sender.get("id") or "")
    account_type = str(acct.get("type") or sender.get("type") or "user").lower()
    login = str(acct.get("login") or sender.get("login") or "")
    return account_id, account_type, login


def handle_marketplace_event(payload: dict, db: Database, settings: Settings) -> dict:
    """处理一个 marketplace_purchase 事件（原子幂等）。返回处理结果摘要。

    并发安全：先 INSERT OR IGNORE events（主键去重），rowcount=0 即为重复事件，
    直接返回；只有抢到插入权的一个请求继续执行业务逻辑。
    """
    event_id = str(payload.get("marketplace_purchase", {}).get("id")
                   or f"{payload.get('sender', {}).get('id')}-{payload.get('effective_date')}")
    action = str(payload.get("action", ""))
    account_id, account_type, login = _extract_account(payload)

    if not db.record_event(event_id, f"marketplace_purchase:{action}", payload):
        return {"event_id": event_id, "action": action, "duplicate": True}

    effective = _parse_date(payload.get("effective_date")) or now()
    seats = int((payload.get("marketplace_purchase") or {}).get("unit_count") or 1)
    plan = _map_plan(payload, settings)

    sub = db.get_subscription(account_id)
    summary: dict = {"event_id": event_id, "action": action, "account_id": account_id}

    if action == ACTION_ACTIVATE:
        db.upsert_subscription({
            "account_id": account_id,
            "account_type": account_type,
            "account_login": login,
            "plan": plan,
            "status": "active",
            "seats": seats,
            "billing_model": "per_seat",
            "effective_date": effective,
            "expiry_date": None,
            "created_at": now(),
            "updated_at": now(),
        })
        unit_price = settings.plan_price.get(plan, 0.0)
        db.create_order({
            "event_id": event_id,
            "account_id": account_id,
            "plan": plan,
            "seats": seats,
            "amount": round(unit_price * seats, 2),
            "currency": "USD",
            "created_at": now(),
        })
        summary.update({"plan": plan, "seats": seats, "activated": True})

    elif action == ACTION_CANCELLED:
        # GitHub：cancelled 后订阅仍有效至 effective_date
        db.upsert_subscription({
            "account_id": account_id,
            "account_type": account_type,
            "account_login": login,
            "plan": plan if sub else "free",
            "status": "cancelled",
            "seats": seats if sub else 1,
            "billing_model": "per_seat",
            "effective_date": effective,
            "expiry_date": effective,
            "created_at": now(),
            "updated_at": now(),
        })
        summary.update({"cancelled": True, "expiry_date": effective})

    elif action == ACTION_CHANGED:
        db.upsert_subscription({
            "account_id": account_id,
            "account_type": account_type,
            "account_login": login,
            "plan": plan,
            "status": "active" if not (sub and sub.get("status") == "cancelled") else "cancelled",
            "seats": seats,
            "billing_model": "per_seat",
            "effective_date": effective,
            "expiry_date": sub.get("expiry_date") if sub else None,
            "created_at": now(),
            "updated_at": now(),
        })
        summary.update({"plan": plan, "changed": True})

    elif action == ACTION_PENDING_CHANGE:
        logger.info("pending_change 已记录（生效日 %s）: %s", payload.get("effective_date"), account_id)
        db.add_notification("log", f"pending_change: {account_id} → {plan} 于 {payload.get('effective_date')}")
        summary.update({"plan": plan, "pending_change": True})

    elif action == ACTION_REFUNDED:
        db.add_notification("log", f"refunded: {account_id} ({plan})")
        summary.update({"refunded": True})

    return summary


def downgrade_expired(db: Database) -> int:
    """每日过期检查：把已过期订阅降级/置为 expired（返回处理数量）。

    覆盖两类：
    - cancelled 且已到到期日（GitHub 取消订阅）→ expired + free
    - active 且已过到期日（自营订阅试用/付费到期）→ expired
    """
    n = 0
    for sub in db.list_subscriptions():
        if sub["status"] in ("cancelled", "active") and sub.get("expiry_date") and now() >= sub["expiry_date"]:
            db.upsert_subscription({
                **sub,
                "plan": "free" if sub["status"] == "cancelled" else sub["plan"],
                "status": "expired",
                "updated_at": now(),
            })
            db.add_notification("log", f"subscription expired: {sub['account_id']}")
            n += 1
    return n


@router.post("/webhook")
async def github_webhook(request: Request):
    """接收 GitHub Marketplace Webhook 事件。"""
    settings: Settings = request.app.state.settings
    db: Database = request.app.state.db
    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256")
    event = request.headers.get("X-GitHub-Event", "")

    if not verify_webhook_signature(settings.github_webhook_secret, body, sig):
        return JSONResponse(status_code=401, content={"error": "invalid signature"})

    import json

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"error": "invalid json"})

    if event == "marketplace_purchase":
        result = handle_marketplace_event(payload, db, settings)
        return {"ok": True, **result}

    # ping / 其他事件：确认接收
    if event == "ping":
        return {"ok": True, "event": "ping"}
    return JSONResponse(status_code=202, content={"ok": True, "event": event, "ignored": True})


@router.post("/cron/downgrade")
async def cron_downgrade(request: Request):
    """定时任务端点（cron/外部调度器调用）：处理到期降级。"""
    settings: Settings = request.app.state.settings
    if not _cron_allowed(request, settings):
        return JSONResponse(status_code=403, content={"error": "forbidden"})
    db: Database = request.app.state.db
    n = downgrade_expired(db)
    return {"ok": True, "downgraded": n}


@router.post("/cron/reset-usage")
async def cron_reset_usage(request: Request):
    """每月 1 日用量重置（仅 Free 用户 usage 归零；cron/调度器调用）。"""
    settings: Settings = request.app.state.settings
    if not _cron_allowed(request, settings):
        return JSONResponse(status_code=403, content={"error": "forbidden"})
    db: Database = request.app.state.db
    count = db.reset_free_usage()
    db.add_notification("log", f"free usage reset: {count} rows cleared")
    return {"ok": True, "cleared": count}


@router.post("/cron/cleanup-payments")
async def cron_cleanup_payments(request: Request):
    """支付日志清理：删除超过保留期（默认 90 天）的非成功记录。"""
    settings: Settings = request.app.state.settings
    if not _cron_allowed(request, settings):
        return JSONResponse(status_code=403, content={"error": "forbidden"})
    db: Database = request.app.state.db
    n = db.cleanup_payments(keep_days=90)
    db.add_notification("log", f"payment log cleanup: {n} rows removed")
    return {"ok": True, "cleaned": n}


def _cron_allowed(request: Request, settings: Settings) -> bool:
    """cron 端点鉴权：配置了 USAGE_API_KEY 时必须匹配；未配置仅本地模拟放行。"""
    if settings.usage_api_key:
        import hmac

        return hmac.compare_digest(request.headers.get("X-API-Key", ""),
                                   settings.usage_api_key)
    return settings.is_local_mode


def get_effective_subscription(db: Database, account_id: str) -> dict | None:
    """读取订阅（懒降级：已到期自动降为 free）。"""
    sub = db.get_subscription(account_id)
    if not sub:
        return None
    if sub["status"] == "cancelled" and sub.get("expiry_date") and now() >= sub["expiry_date"]:
        downgrade_expired(db)
        sub = db.get_subscription(account_id)
    return sub
