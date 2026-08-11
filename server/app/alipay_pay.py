"""支付宝 AI 网页应用收款（alipay.trade.page.pay）真实通道集成。

基于支付宝官方 alipay-sdk-python（3.7.x）实现，对接现有订阅激活体系：

- 下单：`page_execute()` 返回 HTML 支付表单（前端渲染自动提交），禁止用 execute()
- 异步通知：POST 表单 → SDK 验签 → 业务字段校验 → notify_id 幂等 → 激活订阅 → 纯文本 success
- 管理接口：交易查询 / 退款 / 退款查询 / 关闭交易（对账与售后）
- 同步回跳页：不信任回跳参数判定支付成功（以通知/查询为准）

配置（.env）：
    ALIPAY_APP_ID / ALIPAY_APP_PRIVATE_KEY(PKCS#1) / ALIPAY_PUBLIC_KEY
    ALIPAY_SANDBOX=true|false / ALIPAY_NOTIFY_URL / ALIPAY_RETURN_URL
未配置凭证时接口明确失败（不 fallback 占位密钥，符合官方 SDK 红线）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from .config import Settings
from .db import Database, now
from .payment import activate_subscription, create_order_no, handle_successful_payment
from .pricing import Pricing

logger = logging.getLogger("prgen.alipay")

router = APIRouter(prefix="/api/payment/alipay", tags=["alipay"])

GATEWAY = {
    "production": "https://openapi.alipay.com/gateway.do",
    "sandbox": "https://openapi-sandbox.dl.alipaydev.com/gateway.do",
}
PAID_STATUSES = ("TRADE_SUCCESS", "TRADE_FINISHED")


class AlipayConfigError(RuntimeError):
    pass


# ---------- 配置与客户端 ----------

def _alipay_configured(settings: Settings) -> bool:
    return bool(settings.alipay_app_id and settings.alipay_app_private_key
                and settings.alipay_public_key)


def build_alipay_client(settings: Settings):
    """构建支付宝 SDK 客户端（Python 使用 PKCS#1 格式私钥）。"""
    if not _alipay_configured(settings):
        raise AlipayConfigError(
            "支付宝未配置：需要 ALIPAY_APP_ID / ALIPAY_APP_PRIVATE_KEY / "
            "ALIPAY_PUBLIC_KEY（开放平台应用凭证）"
        )
    from alipay.aop.api.AlipayClientConfig import AlipayClientConfig
    from alipay.aop.api.DefaultAlipayClient import DefaultAlipayClient

    config = AlipayClientConfig()
    config.server_url = GATEWAY["sandbox" if settings.alipay_sandbox else "production"]
    config.app_id = settings.alipay_app_id
    config.app_private_key = settings.alipay_app_private_key
    config.alipay_public_key = settings.alipay_public_key
    config.charset = "utf-8"
    config.sign_type = "RSA2"
    return DefaultAlipayClient(alipay_client_config=config)


def verify_notify_signature(settings: Settings, params: dict) -> bool:
    """支付宝异步通知验签：排除 sign/sign_type 后按字典序拼接，RSA2 验签。"""
    sign = params.get("sign", "")
    if not sign:
        return False
    from alipay.aop.api.util import SignatureUtils

    to_verify = {k: v for k, v in params.items()
                 if k not in ("sign", "sign_type")}
    content = SignatureUtils.get_sign_content(to_verify)
    try:
        # SDK verify_with_rsa 不编码 message（与 sign_with_rsa2 的 encode 对应），需传 bytes
        return SignatureUtils.verify_with_rsa(
            settings.alipay_public_key, content.encode("utf-8"), sign
        )
    except Exception as exc:
        logger.warning("支付宝通知验签异常: %s", exc)
        return False


def _normalize_amount(value: Any) -> str | None:
    """金额字符串规范化（"9.9" → "9.90"），避免浮点比较。"""
    import re

    if value is None:
        return None
    m = re.fullmatch(r"(\d+)(?:\.(\d{1,2}))?", str(value).strip())
    if not m:
        return None
    return f"{int(m.group(1))}.{(m.group(2) or '').ljust(2, '0')}"


def _is_paid_notification(params: dict) -> bool:
    """仅付款成功状态且非退款/关单/分账事件才算付款成功。"""
    return (params.get("trade_status") in PAID_STATUSES
            and not params.get("out_biz_no")
            and not params.get("gmt_refund")
            and not params.get("refund_fee"))


def _order_amount(order: dict) -> str:
    return f"{order['amount']:.2f}"


# ---------- 下单 ----------

@router.post("/create")
async def alipay_create(request: Request):
    """创建支付宝支付订单并返回 HTML 支付表单（页面自动提交跳转支付宝收银台）。"""
    settings: Settings = request.app.state.settings
    db: Database = request.app.state.db
    pricing: Pricing = request.app.state.pricing

    try:
        client = build_alipay_client(settings)
    except AlipayConfigError as exc:
        return JSONResponse(status_code=422, content={"error": str(exc)})

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

    # 锁定老价格（与现有支付渠道一致）
    sub = db.get_subscription(user_id)
    locked = bool(sub and sub.get("price_locked") and sub.get("locked_price") is not None)
    try:
        quote = pricing.quote(tier, cycle, "CNY", locked=locked,
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
        "currency": "CNY",
        "channel": "alipay",
        "status": "pending",
        "raw_callback": "",
        "created_at": now(),
        "updated_at": now(),
    })

    # 下单并生成支付表单（网站支付固定 FAST_INSTANT_TRADE_PAY + page_execute）
    from alipay.aop.api.domain.AlipayTradePagePayModel import AlipayTradePagePayModel
    from alipay.aop.api.request.AlipayTradePagePayRequest import AlipayTradePagePayRequest

    request = AlipayTradePagePayRequest()
    if settings.alipay_notify_url:
        request.notify_url = settings.alipay_notify_url
    if settings.alipay_return_url:
        request.return_url = settings.alipay_return_url
    model = AlipayTradePagePayModel()
    model.out_trade_no = order_no
    model.total_amount = _order_amount({"amount": quote.amount})
    model.subject = f"pr-gen {tier} 订阅（{cycle}）"
    model.product_code = "FAST_INSTANT_TRADE_PAY"
    request.biz_model = model

    try:
        form_html = client.page_execute(request)
    except Exception as exc:
        logger.exception("支付宝下单失败")
        db.mark_payment_if_pending(order_no, "failed", json.dumps({"err": str(exc)}))
        return JSONResponse(status_code=502, content={"error": f"支付宝下单失败: {exc}"})

    return {
        "ok": True,
        "order_no": order_no,
        "amount": round(quote.amount, 2),
        "currency": "CNY",
        "mode": "alipay_page",
        "form_html": form_html,
        "notify_url": settings.alipay_notify_url or None,
        "return_url": settings.alipay_return_url or None,
    }


# ---------- 异步通知 ----------

@router.post("/notify")
async def alipay_notify(request: Request):
    """支付宝异步通知：POST 表单（非 JSON）→ 验签 → 业务校验 → 幂等 → 激活。

    返回纯文本 success/fail（支付宝约定）。
    """
    settings: Settings = request.app.state.settings
    db: Database = request.app.state.db

    try:
        form = await request.form()
        params = {k: v for k, v in form.items()}
    except Exception:
        return PlainTextResponse("fail")

    if not verify_notify_signature(settings, params):
        logger.warning("支付宝通知验签失败: out_trade_no=%s",
                       params.get("out_trade_no"))
        return PlainTextResponse("fail")

    order_no = str(params.get("out_trade_no", ""))
    order = db.get_payment(order_no)
    if order is None:
        logger.warning("支付宝通知订单不存在: %s", order_no)
        return PlainTextResponse("fail")

    # 业务字段校验：app_id / 金额 / 卖家（seller_id 配置存在时校验）
    if params.get("app_id") != settings.alipay_app_id:
        return PlainTextResponse("fail")
    if _normalize_amount(params.get("total_amount")) != _normalize_amount(_order_amount(order)):
        return PlainTextResponse("fail")
    if settings.alipay_seller_id and params.get("seller_id") != settings.alipay_seller_id:
        return PlainTextResponse("fail")

    # notify_id 幂等（事件表唯一）
    notify_id = str(params.get("notify_id", ""))
    if notify_id and not db.record_event("alipay_notify:" + notify_id,
                                         "alipay.trade.notify", params):
        return PlainTextResponse("success")  # 重复通知，已处理过

    if not _is_paid_notification(params):
        db.add_notification("log", f"alipay notify non-paid: {order_no} {params.get('trade_status')}")
        return PlainTextResponse("success")

    # 付款成功：原子闸门激活（复用现有幂等逻辑）
    # 支付宝金额字段为 total_amount，映射为 amount 供通用校验读取
    result = handle_successful_payment(
        db, order_no, "alipay",
        {"amount": params.get("total_amount"), **params}, settings,
    )
    if not result.get("ok"):
        logger.warning("支付宝通知处理失败: %s %s", order_no, result.get("error"))
        return PlainTextResponse("fail")
    return PlainTextResponse("success")


# ---------- 同步回跳页（不信任回跳参数） ----------

@router.get("/return", response_class=HTMLResponse)
async def alipay_return(request: Request):
    """支付宝支付完成后的同步回跳页。

    仅展示提示：支付结果以异步通知与交易查询为准，不信任回跳参数。
    """
    out_trade_no = request.query_params.get("out_trade_no", "")
    return HTMLResponse(f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>支付结果</title></head><body style="font-family:sans-serif;text-align:center;margin-top:80px">
<h2>支付处理中…</h2>
<p>订单号：{out_trade_no}</p>
<p>支付结果请以站内订单状态为准（通常 1~5 秒内自动更新）。</p>
<p><a href="/">返回首页</a></p>
</body></html>""")


# ---------- 管理接口：查询 / 退款 / 退款查询 / 关闭 ----------

async def _admin_call(request: Request, api_method: str, biz: dict):
    """统一执行支付宝管理类接口（查询/退款/关闭）。"""
    settings: Settings = request.app.state.settings
    try:
        client = build_alipay_client(settings)
    except AlipayConfigError as exc:
        return JSONResponse(status_code=422, content={"error": str(exc)})

    from alipay.aop.api.request.AlipayTradeQueryRequest import AlipayTradeQueryRequest
    from alipay.aop.api.request.AlipayTradeRefundRequest import AlipayTradeRefundRequest
    from alipay.aop.api.request.AlipayTradeFastpayRefundQueryRequest import AlipayTradeFastpayRefundQueryRequest
    from alipay.aop.api.request.AlipayTradeCloseRequest import AlipayTradeCloseRequest

    req_map = {
        "alipay.trade.query": AlipayTradeQueryRequest,
        "alipay.trade.refund": AlipayTradeRefundRequest,
        "alipay.trade.fastpay.refund.query": AlipayTradeFastpayRefundQueryRequest,
        "alipay.trade.close": AlipayTradeCloseRequest,
    }
    req_cls = req_map.get(api_method)
    if req_cls is None:
        return JSONResponse(status_code=400, content={"error": f"未知接口: {api_method}"})

    req = req_cls()
    req.biz_model = biz
    try:
        resp = client.execute(req)
    except Exception as exc:
        logger.exception("支付宝接口调用失败: %s", api_method)
        return JSONResponse(status_code=502, content={"error": str(exc)})
    return {"ok": True, "method": api_method, "response": resp}


@router.post("/query")
async def alipay_query(request: Request):
    """交易查询（对账/支付结果兜底确认）。body: {out_trade_no}"""
    body = await request.json()
    return await _admin_call(request, "alipay.trade.query",
                             {"out_trade_no": str(body.get("out_trade_no", ""))})


@router.post("/refund")
async def alipay_refund(request: Request):
    """退款。body: {out_trade_no, refund_amount}"""
    body = await request.json()
    return await _admin_call(request, "alipay.trade.refund", {
        "out_trade_no": str(body.get("out_trade_no", "")),
        "refund_amount": f"{float(body.get('refund_amount', 0)):.2f}",
    })


@router.post("/refund/query")
async def alipay_refund_query(request: Request):
    """退款查询。body: {out_trade_no, out_request_no}"""
    body = await request.json()
    return await _admin_call(request, "alipay.trade.fastpay.refund.query", {
        "out_trade_no": str(body.get("out_trade_no", "")),
        "out_request_no": str(body.get("out_request_no", "")),
    })


@router.post("/close")
async def alipay_close(request: Request):
    """关闭交易（未支付订单）。body: {out_trade_no}"""
    body = await request.json()
    return await _admin_call(request, "alipay.trade.close",
                             {"out_trade_no": str(body.get("out_trade_no", ""))})
