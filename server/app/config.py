"""server 配置：.env 加载 + 定价模型系数 + 外部服务开关。

所有外部服务（Supabase/Resend/Telegram）未配置密钥时自动降级为本地模拟，
保证开箱即用与验收可跑通。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent  # .../server/app
SERVER_ROOT = SERVER_DIR.parent               # .../server
PROJECT_ROOT = SERVER_ROOT.parent             # .../pr-gen（项目根）

DEFAULT_ENV = {
    # 基础
    "APP_NAME": "pr-gen",
    "PORT": "8000",
    "DATA_DIR": str(SERVER_ROOT / "data"),
    "MAIL_OUT_DIR": str(SERVER_ROOT / "mail_out"),
    # GitHub Marketplace / Webhook
    "GITHUB_WEBHOOK_SECRET": "dev-webhook-secret",
    "GITHUB_APP_ID": "",
    "GITHUB_APP_PRIVATE_KEY": "",  # PEM 内容或文件路径
    # 定价（美元）
    "PLAN_PRICE_PRO": "9",
    "PLAN_PRICE_TEAM": "29",
    # 报价公式系数（美元/年）
    "QUOTE_BASE_PRICE": "99",
    "QUOTE_PER_DEV": "12",
    "QUOTE_MIN_DEVS": "10",
    "QUOTE_DEPLOYMENT_RATE": "0.25",
    "QUOTE_CUSTOM_RATE": "0.40",
    "QUOTE_VALID_DAYS": "30",
    # 外部服务
    "SUPABASE_URL": "",
    "SUPABASE_KEY": "",
    "RESEND_API_KEY": "",
    "MAIL_FROM": "pr-gen <sales@example.com>",
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
    # 国内支付网关（FAST易支付/YPay 风格免签）
    "PAY_GATEWAY_URL": "",
    "PAY_MERCHANT_ID": "",
    "PAY_SECRET_KEY": "dev-pay-secret",
    "PAY_CALLBACK_URL": "https://your-domain.com/api/payment/callback",
    # PayPal
    "PAYPAL_CLIENT_ID": "",
    "PAYPAL_SECRET": "",
    "PAYPAL_WEBHOOK_ID": "",
    "PAYPAL_SANDBOX": "true",
    # 定价配置
    "PRICING_CONFIG_PATH": "",
    # 安全
    "DATA_ENCRYPTION_KEY": "",  # 空则自动生成并持久化到 data/ 下
    "USAGE_API_KEY": "",  # 非空时 /api/usage 与 /cron/downgrade 要求 X-API-Key 头
    # 监控
    "COST_ALERT_THRESHOLD": "5",
}


def load_env_file(path: Path | None = None) -> dict[str, str]:
    """加载 .env 文件（KEY=VALUE，忽略注释），返回覆盖字典。"""
    path = path or SERVER_DIR / ".env"
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        line = line.removeprefix("export ").strip()
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


@dataclass
class Settings:
    app_name: str
    data_dir: Path
    mail_out_dir: Path
    github_webhook_secret: str
    github_app_id: str
    github_app_private_key: str
    plan_price: dict[str, float] = field(default_factory=dict)  # {pro: 9, team: 29}
    quote: dict[str, float] = field(default_factory=dict)
    quote_valid_days: int = 30
    supabase_url: str = ""
    supabase_key: str = ""
    resend_api_key: str = ""
    mail_from: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    data_encryption_key: str = ""
    cost_alert_threshold: float = 5.0
    usage_api_key: str = ""
    # 支付渠道
    pay_gateway_url: str = ""
    pay_merchant_id: str = ""
    pay_secret_key: str = "dev-pay-secret"
    pay_callback_url: str = ""
    paypal_client_id: str = ""
    paypal_secret: str = ""
    paypal_webhook_id: str = ""
    paypal_sandbox: bool = True
    pricing_config_path: str = ""

    @property
    def is_local_mode(self) -> bool:
        """未配置任何外部服务 → 本地模拟模式。"""
        return not (self.supabase_url or self.resend_api_key or self.telegram_bot_token)


def _f(val: str, default: float) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def load_settings(env_overrides: dict[str, str] | None = None) -> Settings:
    """组装 Settings：env_overrides > 环境变量 > .env 文件 > 默认值。"""
    merged = dict(DEFAULT_ENV)
    merged.update(load_env_file())
    # 环境变量（仅采纳已知 key），支持 Railway/Fly.io 等平台注入
    known_env = {k: v for k, v in os.environ.items() if k in merged}
    merged.update(known_env)
    if env_overrides:
        merged.update(env_overrides)

    data_dir = Path(merged["DATA_DIR"])
    data_dir.mkdir(parents=True, exist_ok=True)
    mail_out = Path(merged["MAIL_OUT_DIR"])
    mail_out.mkdir(parents=True, exist_ok=True)

    enc_key = merged["DATA_ENCRYPTION_KEY"]
    if not enc_key:
        # 本地模式：自动生成 Fernet 密钥并持久化（生产应显式配置）
        from cryptography.fernet import Fernet

        key_file = data_dir / "encryption.key"
        if key_file.exists():
            enc_key = key_file.read_text(encoding="utf-8").strip()
        else:
            enc_key = Fernet.generate_key().decode("utf-8")
            key_file.write_text(enc_key, encoding="utf-8")
            try:
                os.chmod(key_file, 0o600)  # 仅所有者可读（Windows 上为 no-op）
            except OSError:
                pass

    return Settings(
        app_name=merged["APP_NAME"],
        data_dir=data_dir,
        mail_out_dir=mail_out,
        github_webhook_secret=merged["GITHUB_WEBHOOK_SECRET"],
        github_app_id=merged["GITHUB_APP_ID"],
        github_app_private_key=merged["GITHUB_APP_PRIVATE_KEY"],
        plan_price={
            "pro": _f(merged["PLAN_PRICE_PRO"], 9.0),
            "team": _f(merged["PLAN_PRICE_TEAM"], 29.0),
        },
        quote={
            "base": _f(merged["QUOTE_BASE_PRICE"], 99.0),
            "per_dev": _f(merged["QUOTE_PER_DEV"], 12.0),
            "min_devs": _f(merged["QUOTE_MIN_DEVS"], 10.0),
            "deployment_rate": _f(merged["QUOTE_DEPLOYMENT_RATE"], 0.25),
            "custom_rate": _f(merged["QUOTE_CUSTOM_RATE"], 0.40),
        },
        quote_valid_days=int(_f(merged["QUOTE_VALID_DAYS"], 30.0)),
        supabase_url=merged["SUPABASE_URL"],
        supabase_key=merged["SUPABASE_KEY"],
        resend_api_key=merged["RESEND_API_KEY"],
        mail_from=merged["MAIL_FROM"],
        telegram_bot_token=merged["TELEGRAM_BOT_TOKEN"],
        telegram_chat_id=merged["TELEGRAM_CHAT_ID"],
        data_encryption_key=enc_key,
        cost_alert_threshold=_f(merged["COST_ALERT_THRESHOLD"], 5.0),
        usage_api_key=merged["USAGE_API_KEY"],
        pay_gateway_url=merged["PAY_GATEWAY_URL"],
        pay_merchant_id=merged["PAY_MERCHANT_ID"],
        pay_secret_key=merged["PAY_SECRET_KEY"],
        pay_callback_url=merged["PAY_CALLBACK_URL"],
        paypal_client_id=merged["PAYPAL_CLIENT_ID"],
        paypal_secret=merged["PAYPAL_SECRET"],
        paypal_webhook_id=merged["PAYPAL_WEBHOOK_ID"],
        paypal_sandbox=str(merged["PAYPAL_SANDBOX"]).lower() in ("true", "1", "yes"),
        pricing_config_path=merged["PRICING_CONFIG_PATH"],
    )
