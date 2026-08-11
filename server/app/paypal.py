"""PayPal 支付（API v2 / Orders API + Webhook）。

流程：
1. POST /api/payment/paypal/create → 创建 PayPal Order，返回 approval_url
   （本地模拟返回模拟链接；配置 PAYPAL_CLIENT_ID/SECRET 后走真实 API v2）
2. 用户跳转 PayPal 完成支付
3. PayPal 异步发送 PAYMENT.CAPTURE.COMPLETED Webhook
4. POST /api/payment/paypal/webhook → 验签 → 激活订阅

安全：
- 真实模式：校验 PayPal-Transmission-* 头并调用 verify-webhook-signature API
  （依赖 PAYPAL_WEBHOOK_ID）
- 模拟模式：X-Sim-Signature = HMAC-SHA256(event_id + PAY_SECRET_KEY)，常量时间比较

事件解析：真实 PAYMENT.CAPTURE.COMPLETED 的 resource 是 capture 对象，
订单号通过创建订单时设置的 custom_id（reference_id 之外的可靠载体）传递。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
from urllib import error as urlerror
from urllib import request as urlreq

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .config import Settings
from .db import Database, now
from .payment import activate_subscription, create_order_no, handle_successful_payment
from .pricing import Pricing

logger = logging.getLogger("prgen.paypal")

router = APIRouter(prefix="/api/payment/paypal", tags=["paypal"])

PAYPAL_API = {
    "live": "https://api-m.paypal.com",
    "sandbox": "https://api-m.sandbox.paypal.com",
}


def _api_base(settings: Settings) -> str:
    env = "sandbox" if settings.paypal_sandbox else "live"
    return PAYPAL_API[env]


def _sim_signature(event_id: str, secret: str) -> str:
    """本地模拟模式的事件签名（HMAC-SHA256，与安全模块同风格）。"""
    digest = hmac.new(secret.encode("utf-8"), event_id.encode("utf-8"),
                      hashlib.sha256).hexdigest()
    return digest


def _verify_paypal_webhook(request: Request, settings: Settings, body: bytes) -> bool:
    """PayPal Webhook 验签。

    - 真实模式（配置了 client_id/secret）：校验 transmission 头 + 调用
      verify-webhook-signature API（webhook_id 参与验签）
    - 本地模拟：X-Sim-Signature 与 HMAC(body, PAY_SECRET_KEY) 常量时间比较
    """
    if settings.paypal_client_id and settings.paypal_secret:
        transmission_id = request.headers.get("PayPal-Transmission-Id", "")
        transmission_sig = request.headers.get("PayPal-Transmission-Sig", "")
        transmission_time = request.headers.get("PayPal-Transmission-Time", "")
        webhook_id = request.headers.get("PayPal-Transmission-Webhook-Id", "")
        if not (transmission_id and transmission_sig and transmission_time):
            return False
        if not settings.paypal_webhook_id or webhook_id != settings.paypal_webhook_id:
            return False
        return _verify_with_paypal_api(settings, {
            "auth_algo": request.headers.get("PayPal-Auth-Algo", ""),
            "cert_url": request.headers.get("PayPal-Cert-Url", ""),
            "transmission_id": transmission_id,
            "transmission_sig": transmission_sig,
            "transmission_time": transmission_time,
            "webhook_id": webhook_id,
            "webhook_event": _safe_load_json(body),
        })
    # 本地模拟：HMAC 常量时间比较
    provided = request.headers.get("X-Sim-Signature", "")
    try:
        event_id = json.loads(body).get("id", "")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    expected = _sim_signature(event_id, settings.pay_secret_key)
    return hmac.compare_digest(provided, expected)


def _safe_load_json(body: bytes) -> dict:
    """安全解析 Webhook body（失败返回空 dict，避免 500）。"""
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _verify_with_paypal_api(settings: Settings, payload: dict) -> bool:
    """调用 PayPal verify-webhook-signature API 完成验签。"""
    try:
        req = urlreq.Request(
            f"{_api_base(settings)}/v1/notifications/verify-webhook-signature",
            data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {_token(settings)}",
                     "Content-Type": "application/json"},
        )
        with urlreq.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        return result.get("verification_status") == "SUCCESS"
    except (urlerror.URLError, urlerror.HTTPError, OSError, ValueError) as exc:
        logger.warning("PayPal 验签调用失败: %s", exc)
        return False


@router.post("/create")
async def paypal_create(request: Request):
    """创建 PayPal Order 并返回 approval_url。body: {user_id, tier, cycle}"""
    settings: Settings = request.app.state.settings
    db: Database = request.app.state.db
    pricing: Pricing = request.app.state.pricing

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid json"})

    user_id = str(payload.get("user_id", ""))
    tier = str(payload.get("tier", "pro"))
    cycle = str(payload.get("cycle", "monthly"))
    if tier == "free":
        return JSONResponse(status_code=422, content={"error": "free 层级无需支付"})
    if not (user_id.isdigit() and db.get_user(int(user_id))):
        return JSONResponse(status_code=404, content={"error": "用户不存在"})

    sub = db.get_subscription(user_id)
    locked = bool(sub and sub.get("price_locked") and sub.get("locked_price") is not None)
    try:
        quote = pricing.quote(tier, cycle, "USD", locked=locked,
                              locked_price=sub.get("locked_price") if sub else None,
                              locked_currency=sub.get("locked_currency") if sub else None)
    except Exception as exc:
        return JSONResponse(status_code=422, content={"error": str(exc)})

    order_no = create_order_no()
    db.create_payment({
        "order_no": order_no,
        "user_id": int(user_id),
        "tier": tier,
        "cycle": cycle,
        "amount": quote.amount,
        "currency": "USD",
        "channel": "paypal",
        "status": "pending",
        "raw_callback": "",
        "created_at": now(),
        "updated_at": now(),
    })

    if settings.paypal_client_id and settings.paypal_secret:
        pay_url = _create_real_order(settings, order_no, quote.amount, tier)
    else:
        pay_url = (f"https://www.paypal.com/checkoutnow?token=sim-{order_no}"
                   "（本地模拟 approval URL）")

    return {
        "ok": True,
        "order_no": order_no,
        "amount": round(quote.amount, 2),
        "currency": "USD",
        "approval_url": pay_url,
        "quote": quote.to_dict(),
    }


def _create_real_order(settings: Settings, order_no: str, amount: float,
                       tier: str) -> str:
    """真实 PayPal Orders API v2。custom_id 携带内部订单号供 Webhook 关联。"""
    auth = base64.b64encode(
        f"{settings.paypal_client_id}:{settings.paypal_secret}".encode()
    ).decode()
    body = json.dumps({
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": order_no,
            "custom_id": order_no,  # capture 事件中可读取的订单号载体
            "description": f"pr-gen {tier} 订阅",
            "amount": {"currency_code": "USD", "value": f"{amount:.2f}"},
        }],
        "application_context": {"brand_name": "pr-gen",
                                "user_action": "PAY_NOW"},
    }).encode("utf-8")

    req = urlreq.Request(
        f"{_api_base(settings)}/v2/checkout/orders", data=body, method="POST",
        headers={"Authorization": f"Bearer {_token(settings)}",
                 "Content-Type": "application/json"},
    )
    try:
        with urlreq.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urlerror.URLError, urlerror.HTTPError, OSError) as exc:
        raise RuntimeError(f"PayPal 创建订单失败: {exc}") from exc
    for link in data.get("links", []):
        if link.get("rel") == "approve":
            return link["href"]
    raise RuntimeError(f"PayPal 响应缺少 approval_url: {data}")


def _token(settings: Settings) -> str:
    """获取 PayPal OAuth token（真实模式）。"""
    auth = base64.b64encode(
        f"{settings.paypal_client_id}:{settings.paypal_secret}".encode()
    ).decode()
    req = urlreq.Request(
        f"{_api_base(settings)}/v1/oauth2/token", data=b"grant_type=client_credentials",
        method="POST",
        headers={"Authorization": f"Basic {auth}",
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urlreq.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))["access_token"]
    except (urlerror.URLError, urlerror.HTTPError, OSError) as exc:
        raise RuntimeError(f"PayPal 获取 token 失败: {exc}") from exc


@router.post("/webhook")
async def paypal_webhook(request: Request):
    """PayPal Webhook：PAYMENT.CAPTURE.COMPLETED → 激活订阅。"""
    settings: Settings = request.app.state.settings
    db: Database = request.app.state.db
    body = await request.body()

    if not _verify_paypal_webhook(request, settings, body):
        return JSONResponse(status_code=401, content={"error": "invalid signature"})

    try:
        event = json.loads(body)
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"error": "invalid json"})

    if event.get("event_type") != "PAYMENT.CAPTURE.COMPLETED":
        return {"ok": True, "ignored": event.get("event_type")}

    # 事件幂等：record_event 原子插入（INSERT OR IGNORE + rowcount 闸门）
    event_id = event.get("id", "")
    if event_id and not db.record_event("paypal:" + event_id,
                                        "PAYMENT.CAPTURE.COMPLETED", event):
        return {"ok": True, "duplicate": True}

    # 真实 capture 事件：订单号在 custom_id（reference_id 仅存在于 order 事件）
    resource = event.get("resource") or {}
    order_no = (resource.get("custom_id") or ""
                or (resource.get("purchase_units") or [{}])[0].get("reference_id", ""))
    amount = float((resource.get("amount") or {}).get("value", 0))

    if not order_no:
        return JSONResponse(status_code=400, content={"error": "缺少订单号"})

    result = handle_successful_payment(
        db, order_no, "paypal",
        {"event_id": event_id, "amount": amount, **event}, settings,
    )
    if not result.get("ok"):
        return JSONResponse(status_code=400, content={"error": result.get("error")})
    return {"ok": True, "activated": result.get("activated")}
