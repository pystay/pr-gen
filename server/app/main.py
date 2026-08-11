"""FastAPI 入口：组装路由、共享状态（settings/db/encryptor）。

启动：uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__
from .admin import router as admin_router
from .alipay_pay import router as alipay_router
from .auth import router as auth_router
from .config import Settings, load_settings
from .db import Database
from .leads import router as leads_router
from .payment import router as payment_router
from .paypal import router as paypal_router
from .pricing import DEFAULT_CONFIG_PATH, Pricing
from .security import Encryptor
from .webhook import router as github_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("prgen")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    db = Database(settings.data_dir / "prgen.db")
    enc = Encryptor(settings.data_encryption_key)
    pricing = Pricing(Path(settings.pricing_config_path) if settings.pricing_config_path
                      else DEFAULT_CONFIG_PATH)
    app.state.settings = settings
    app.state.db = db
    app.state.encryptor = enc
    app.state.pricing = pricing
    mode = "本地模拟" if settings.is_local_mode else "真实服务"
    logger.info("pr-gen server v%s 启动（%s）｜定价: %s", __version__, mode,
                pricing.config_path_str)
    if not settings.is_local_mode:
        # 生产模式安全基线检查（fail-fast：默认密钥直接拒绝启动）
        failures = []
        if settings.github_webhook_secret == "dev-webhook-secret":
            failures.append("GITHUB_WEBHOOK_SECRET 仍为默认值（Webhook 签名可被伪造）")
        if settings.pay_secret_key == "dev-pay-secret":
            failures.append("PAY_SECRET_KEY 仍为默认值（国内支付回调可被伪造）")
        if not settings.usage_api_key:
            failures.append("未配置 USAGE_API_KEY（内部 API 无鉴权）")
        if not settings.data_encryption_key:
            failures.append("DATA_ENCRYPTION_KEY 未显式配置（使用自动生成的本地密钥）")
        if failures:
            raise RuntimeError(
                "生产模式安全基线检查未通过，拒绝启动：\n  - " + "\n  - ".join(failures)
            )
    yield


app = FastAPI(
    title="pr-gen 商业化服务",
    version=__version__,
    lifespan=lifespan,
)

app.include_router(github_router)
app.include_router(leads_router)
app.include_router(admin_router)
app.include_router(auth_router)
app.include_router(payment_router)
app.include_router(paypal_router)
app.include_router(alipay_router)


@app.get("/api/pricing")
async def get_pricing(request: Request):
    """对外定价查询（前端展示用，含促销与功能列表）。"""
    pricing: Pricing = request.app.state.pricing
    tiers = {}
    for tier in ("free", "pro", "team"):
        tiers[tier] = {
            "features": pricing.tier_features(tier),
            "cn": pricing.quote(tier, "monthly", "CNY").to_dict()
            if tier != "free" else pricing.quote(tier, "monthly").to_dict(),
            "cn_yearly": pricing.quote(tier, "yearly", "CNY").to_dict()
            if tier != "free" else pricing.quote(tier, "yearly").to_dict(),
            "usd": pricing.quote(tier, "monthly", "USD").to_dict()
            if tier != "free" else pricing.quote(tier, "monthly", "USD").to_dict(),
            "usd_yearly": pricing.quote(tier, "yearly", "USD").to_dict()
            if tier != "free" else pricing.quote(tier, "yearly", "USD").to_dict(),
        }
    promo = pricing._promotion_window()
    return {"tiers": tiers, "free_quota": pricing.free_quota(),
            "promotion": {"active": promo.active, "note": promo.note or None}}


@app.get("/healthz")
async def healthz():
    return {"ok": True, "version": __version__}


def _require_api_key(request: Request, settings: Settings) -> bool:
    """内部 API 鉴权：配置了 USAGE_API_KEY 时必须匹配 X-API-Key；
    未配置时仅本地模拟模式放行（生产 fail-closed）。"""
    if settings.usage_api_key:
        import hmac

        return hmac.compare_digest(request.headers.get("X-API-Key", ""),
                                   settings.usage_api_key)
    return settings.is_local_mode


@app.post("/api/usage")
async def record_usage(request: Request):
    """pr-gen CLI 生成成功后上报用量（account_id 与成本由调用方提供）。"""
    settings: Settings = request.app.state.settings
    db: Database = request.app.state.db
    if not _require_api_key(request, settings):
        return JSONResponse(status_code=403, content={"error": "forbidden"})
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid json"})
    account_id = str(payload.get("account_id", "anonymous"))
    plan = str(payload.get("plan", "free"))
    calls = int(payload.get("calls", 1))
    cost = float(payload.get("cost", 0.0))
    db.record_usage(account_id, plan, calls, cost)

    # 成本告警（单日成本超阈值）
    from .admin import check_cost_alert

    alert = check_cost_alert(settings, db)
    return {"ok": True, "alert": alert}


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("未处理异常: %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"error": "internal error"})
