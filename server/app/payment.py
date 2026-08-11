"""国内支付（支付宝/微信，FAST易支付/YPay 风格免签网关）。

流程：
1. POST /api/payment/create  → 创建订单（金额来自 pricing 模块，含促销/锁定价）
   - 本地模拟：返回模拟收款二维码（文本 + data URL）
   - 真实网关：调用 PAY_GATEWAY_URL 下单并返回跳转/二维码信息
2. 用户扫码支付
3. 支付平台异步回调 POST /api/payment/callback
   - 验签（md5 按 key 排序拼接 + PAY_SECRET_KEY）
   - 幂等（order_no 唯一 + 状态机：pending → success 只允许一次）
   - 激活订阅（首次付费锁定价格，续费延长 end_date）
   - 返回 {"code": 0, "msg": "success"}

安全：回调必须验签；金额与订单一致；原始回调留痕；日志保留 ≥90 天。
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
import urllib.error
import urllib.request
import urllib.parse
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .config import Settings
from .db import Database, now
from .leads import html_escape
from .pricing import Pricing

logger = logging.getLogger("prgen.payment")

router = APIRouter(prefix="/api/payment", tags=["payment"])

CYCLE_DAYS = {"monthly": 30, "yearly": 365}
PAYMENT_RETENTION_DAYS = 90  # 支付日志保留期（对账/纠纷）


# ---------- 签名（FAST易支付风格：md5(排序参数拼接 + secret)） ----------

def sign_payload(params: dict, secret: str) -> str:
    """按 key 排序拼接 key=value，追加 secret，md5。与网关约定一致。"""
    ordered = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return hashlib.md5((ordered + secret).encode("utf-8")).hexdigest()


def verify_callback(params: dict, secret: str) -> bool:
    """回调验签：校验 sign 字段。"""
    provided = str(params.get("sign", ""))
    if not provided:
        return False
    to_check = {k: v for k, v in params.items() if k != "sign"}
    return secrets.compare_digest(sign_payload(to_check, secret), provided)


# ---------- 订单创建 ----------

def create_order_no() -> str:
    return f"{datetime.now(timezone.utc):%Y%m%d%H%M%S}{secrets.randbelow(100000):05d}"


def _mock_qrcode(order_no: str, amount: float) -> dict:
    """本地模拟收款二维码（真实网关返回二维码图片/跳转 URL）。"""
    text = f"prgen://pay?order={order_no}&amount={amount:.2f}"
    return {
        "mode": "mock",
        "qr_text": text,
        "qr_data_url": f"data:image/png;base64,{secrets.token_hex(8)}",
        "note": "本地模拟二维码：扫码支付后调用 /api/payment/callback 模拟通知",
    }


@router.post("/create")
async def create_payment(request: Request):
    """创建支付订单。body: {user_id, tier, cycle, channel, currency}"""
    settings: Settings = request.app.state.settings
    db: Database = request.app.state.db
    pricing: Pricing = request.app.state.pricing

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid json"})

    user_id = str(payload.get("user_id", ""))
    tier = str(payload.get("tier", ""))
    cycle = str(payload.get("cycle", "monthly"))
    channel = str(payload.get("channel", "alipay"))
    currency = str(payload.get("currency", "CNY"))

    if tier == "free":
        return JSONResponse(status_code=422, content={"error": "free 层级无需支付，直接注册即可"})
    if not (user_id.isdigit() and db.get_user(int(user_id))):
        return JSONResponse(status_code=404, content={"error": "用户不存在"})
    if channel not in ("alipay", "wechat"):
        return JSONResponse(status_code=422, content={"error": "国内支付渠道仅支持 alipay/wechat"})

    # 锁定老价格判断：已有付费订阅且 price_locked
    sub = db.get_subscription(user_id)
    locked = bool(sub and sub.get("price_locked") and sub.get("locked_price") is not None)

    try:
        quote = pricing.quote(
            tier, cycle, currency,
            locked=locked,
            locked_price=sub.get("locked_price") if sub else None,
            locked_currency=sub.get("locked_currency") if sub else None,
        )
    except Exception as exc:
        return JSONResponse(status_code=422, content={"error": str(exc)})

    order_no = create_order_no()
    db.create_payment({
        "order_no": order_no,
        "user_id": int(user_id),
        "tier": tier,
        "cycle": cycle,
        "amount": quote.amount,
        "currency": quote.currency,
        "channel": channel,
        "status": "pending",
        "raw_callback": "",
        "created_at": now(),
        "updated_at": now(),
    })

    result = {
        "ok": True,
        "order_no": order_no,
        "amount": round(quote.amount, 2),
        "currency": quote.currency,
        "channel": channel,
        "quote": quote.to_dict(),
    }

    # 真实网关：下单并返回支付信息；本地模拟：直接返回模拟二维码
    if settings.pay_gateway_url:
        result["payment"] = _gateway_submit(settings, order_no, quote.amount,
                                            quote.currency, tier)
    else:
        result["payment"] = _mock_qrcode(order_no, quote.amount)
    return result


def _gateway_submit(settings: Settings, order_no: str, amount: float,
                    currency: str, tier: str) -> dict:
    """调用 FAST易支付/YPay 网关下单（按网关文档调整字段）。"""
    params = {
        "pid": settings.pay_merchant_id,
        "type": "alipay",
        "out_trade_no": order_no,
        "notify_url": settings.pay_callback_url,
        "return_url": settings.pay_callback_url,
        "name": f"pr-gen {tier} 订阅",
        "money": f"{amount:.2f}",
    }
    params["sign"] = sign_payload(params, settings.pay_secret_key)
    params["sign_type"] = "MD5"
    url = f"{settings.pay_gateway_url}?{urllib.parse.urlencode(params)}"
    return {"mode": "gateway", "pay_url": url, "note": "跳转支付网关完成支付"}


# ---------- 订阅激活（共享逻辑：国内回调 + PayPal Webhook） ----------

def activate_subscription(db: Database, user_id: str, tier: str, cycle: str,
                          channel: str, amount: float, currency: str,
                          first_purchase: bool = False) -> dict:
    """支付成功后激活/延长订阅。

    - 首次付费订阅：price_locked=true，locked_price=月度价格点（本次支付折算），
      锁定币种=本次支付币种（老用户跨周期续费按该价格点 × 周期月数）
    - 续费：延长 end_date（max(now, 当前到期日) + 周期）
    """
    sub = db.get_subscription(user_id)
    base = max(now(), sub["expiry_date"] or now()) if sub else now()
    new_expiry = base + CYCLE_DAYS.get(cycle, 30) * 86400

    cycle_months = 12 if cycle == "yearly" else 1
    price_locked = 1 if (first_purchase or (sub and sub.get("price_locked"))) else 0
    locked_price = (amount / cycle_months) if price_locked else (sub.get("locked_price") if sub else None)
    locked_currency = currency if price_locked else (sub.get("locked_currency") if sub else "CNY")

    db.upsert_subscription({
        "account_id": user_id,
        "account_type": "user",
        "account_login": "",
        "plan": tier,
        "status": "active",
        "seats": 1,
        "billing_model": "per_user",
        "effective_date": now(),
        "expiry_date": new_expiry,
        "price_locked": price_locked,
        "locked_price": locked_price,
        "locked_currency": locked_currency,
        "payment_channel": channel,
        "billing_cycle": cycle,
        "created_at": now(),
        "updated_at": now(),
    })
    return {"plan": tier, "expiry_date": new_expiry, "price_locked": price_locked,
            "locked_price": locked_price, "locked_currency": locked_currency}


def handle_successful_payment(db: Database, order_no: str, channel: str,
                              raw_callback: dict, settings: Settings) -> dict:
    """订单支付成功处理（原子幂等：mark_payment_if_pending 为唯一闸门）。"""
    order = db.get_payment(order_no)
    if not order:
        return {"ok": False, "error": "订单不存在"}

    # 金额校验（回调金额必须与订单一致，防篡改）
    cb_amount = float(raw_callback.get("amount", raw_callback.get("money", 0)))
    if abs(cb_amount - order["amount"]) > 0.005:
        return {"ok": False, "error": "金额不一致"}

    # 原子闸门：pending → success 只允许一次（并发重试/重复回调不会二次生效）
    if not db.mark_payment_if_pending(order_no, "success",
                                      json.dumps(raw_callback, ensure_ascii=False)):
        return {"ok": True, "duplicate": True}

    sub = db.get_subscription(str(order["user_id"]))
    first_purchase = not (sub and sub.get("plan") in ("pro", "team") and
                          sub.get("price_locked"))
    result = activate_subscription(
        db, str(order["user_id"]), order["tier"], order["cycle"], channel,
        order["amount"], order["currency"], first_purchase=first_purchase,
    )
    db.add_notification("log",
                        f"payment success: {order_no} {order['tier']} "
                        f"{order['amount']}{order['currency']}")
    return {"ok": True, "activated": result}


# ---------- 国内回调端点 ----------

@router.post("/callback")
async def payment_callback(request: Request):
    """支付平台异步通知（表单/JSON）。验签 → 幂等 → 激活 → 返回网关约定格式。"""
    settings: Settings = request.app.state.settings
    db: Database = request.app.state.db

    content_type = request.headers.get("content-type", "")
    if "json" in content_type:
        params = dict(await request.json())
    else:
        form = await request.form()
        params = {k: v for k, v in form.items()}

    if not verify_callback(params, settings.pay_secret_key):
        logger.warning("支付回调验签失败: %s", params.get("out_trade_no"))
        return JSONResponse(status_code=400, content={"code": 1, "msg": "bad sign"})

    order_no = str(params.get("out_trade_no", ""))
    trade_status = str(params.get("trade_status", "success"))
    if trade_status != "success":
        # 仅 pending → failed（不覆盖已 success 的订单）
        db.mark_payment_if_pending(order_no, "failed",
                                   json.dumps(params, ensure_ascii=False))
        return {"code": 0, "msg": "success"}

    order = db.get_payment(order_no)
    channel = order["channel"] if order else "alipay"  # 渠道以订单为准
    result = handle_successful_payment(db, order_no, channel, params, settings)
    if not result.get("ok"):
        logger.warning("支付回调处理失败: %s %s", order_no, result.get("error"))
        return JSONResponse(status_code=400, content={"code": 1, "msg": result.get("error", "failed")})
    return {"code": 0, "msg": "success"}
