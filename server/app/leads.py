"""企业报价 API：询价表单 → 报价计算 → PDF → 邮件 → leads 入库 → 人工介入通知。

- 敏感字段（公司名/邮箱/备注）AES-256（Fernet）加密后入库
- 开发者数量 > 50 触发人工介入通知（配置 TELEGRAM_BOT_TOKEN 走真实 Telegram，
  否则落盘 notifications 表）
"""

from __future__ import annotations

import html
import json
import logging
import time
import urllib.error
import urllib.request

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .config import Settings
from .db import Database, now
from .mailer import send_quote_email
from .pdf import generate_quote_pdf
from .quoting import QuoteRequest, compute_quote
from .security import Encryptor

logger = logging.getLogger("prgen.leads")

router = APIRouter(prefix="/api/enterprise", tags=["enterprise"])

HUMAN_INTERVENTION_THRESHOLD = 50

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def html_escape(value: str) -> str:
    """邮件/PDF 中使用的 HTML 转义。"""
    return html.escape(value, quote=True)


def _validation_fields(exc: Exception) -> list[str]:
    """从 Pydantic ValidationError 提取失败字段名（不暴露内部细节）。"""
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return ["unknown"]
    try:
        return [str(e.get("loc", ["unknown"])[-1]) for e in errors()]
    except Exception:
        return ["unknown"]


def notify_human_intervention(settings: Settings, db: Database, message: str) -> dict:
    """开发者数量超过阈值时通知人工介入（Telegram 或落盘）。"""
    if settings.telegram_bot_token and settings.telegram_chat_id:
        url = TELEGRAM_API.format(token=settings.telegram_bot_token)
        body = json.dumps({
            "chat_id": settings.telegram_chat_id,
            "text": message,
        }).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
            db.add_notification("telegram", message)
            return {"channel": "telegram"}
        except (urllib.error.URLError, OSError) as exc:
            logger.warning("Telegram 通知失败，落盘: %s", exc)
    db.add_notification("log", message)
    return {"channel": "log"}


@router.post("/quote")
async def create_quote(request: Request):
    """提交企业询价表单，生成 PDF 报价单并邮件发送。"""
    settings: Settings = request.app.state.settings
    db: Database = request.app.state.db
    enc: Encryptor = request.app.state.encryptor

    try:
        raw = await request.json()
        req = QuoteRequest(**raw)
    except Exception as exc:
        # 只回显字段级错误位置，不暴露内部实现细节
        return JSONResponse(status_code=422, content={
            "error": "表单校验失败",
            "fields": _validation_fields(exc),
        })

    quote = compute_quote(req, settings)
    pdf_path = generate_quote_pdf(settings, req, quote, settings.data_dir / "quotes")

    company_html = html_escape(req.company)
    subject = f"{settings.app_name} 企业版报价单 - {company_html}"
    html = (
        f"<p>您好 {company_html}，</p>"
        f"<p>感谢您对 {settings.app_name} 企业私有部署版感兴趣。"
        f"根据您提交的信息，报价如下：</p>"
        f"<ul>"
        f"<li>基础价格：${quote.base_price:,.2f}/年</li>"
        f"<li>部署附加费：${quote.deployment_fee:,.2f}/年</li>"
        f"<li>定制开发附加费：${quote.custom_fee:,.2f}/年</li>"
        f"<li><b>最终报价：${quote.total:,.2f} 美元/年</b></li>"
        f"</ul>"
        f"<p>详细报价单见附件（有效期 {settings.quote_valid_days} 天）。"
        f"如有任何问题，欢迎直接回复本邮件。</p>"
    )
    mail = send_quote_email(settings, req.contact_email, subject, html, pdf_path)

    lead_id = db.create_lead({
        "company_enc": enc.encrypt(req.company),
        "contact_email_enc": enc.encrypt(req.contact_email),
        "dev_count": req.dev_count,
        "environment": req.environment.value,
        "needs_custom": 1 if req.needs_custom else 0,
        "special_notes_enc": enc.encrypt(req.special_notes),
        "quote_amount": quote.total,
        "quote_currency": quote.currency,
        "quote_pdf_path": str(pdf_path),
        "status": "已报价",
        "created_at": now(),
    })

    intervention = None
    if req.dev_count > HUMAN_INTERVENTION_THRESHOLD:
        intervention = notify_human_intervention(
            settings, db,
            f"[人工介入] {req.company} 询价：{req.dev_count} 名开发者，"
            f"环境 {req.environment.value}，报价 ${quote.total:,.2f}/年，"
            f"预计高价值客户，请尽快联系 {req.contact_email}",
        )

    return {
        "ok": True,
        "lead_id": lead_id,
        "quote": quote.breakdown(),
        "pdf": str(pdf_path),
        "mail": mail,
        "intervention": intervention,
    }
