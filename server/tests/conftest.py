"""共享测试设施：独立临时数据目录 + TestClient 工厂。"""

from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

SECRET = "test-webhook-secret"


def make_client(tmp: Path, **env_overrides: str) -> TestClient:
    """用临时数据目录启动隔离的应用实例；env_overrides 可覆盖默认环境。"""
    import os

    env = {
        "GITHUB_WEBHOOK_SECRET": SECRET,
        "DATA_DIR": str(tmp / "data"),
        "MAIL_OUT_DIR": str(tmp / "mail"),
        "RESEND_API_KEY": "",
        "TELEGRAM_BOT_TOKEN": "",
        "DATA_ENCRYPTION_KEY": "",
        "USAGE_API_KEY": "",
    }
    env.update(env_overrides)
    for k, v in env.items():
        os.environ[k] = v
    client = TestClient(app)
    # 进入/退出 lifespan，初始化 app.state（settings/db/encryptor）
    with client:
        pass
    return client


def signed_payload(payload: dict, secret: str = SECRET) -> dict:
    """计算 X-Hub-Signature-256 头。"""
    body = json.dumps(payload).encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {"body": body, "signature": f"sha256={digest}"}


def purchase_payload(action: str = "purchased", account_id: int = 184,
                     login: str = "acme-corp", plan_name: str = "Pro Plan",
                     unit_count: int = 5, effective: str = "2026-08-01T00:00:00+00:00",
                     event_id: int = 9001) -> dict:
    """构造 GitHub marketplace_purchase 事件 payload。"""
    return {
        "action": action,
        "effective_date": effective,
        "sender": {"login": "octocat", "id": 12345, "type": "User"},
        "marketplace_purchase": {
            "id": event_id,
            "account": {"type": "Organization", "id": account_id, "login": login,
                        "organization_billing_email": "billing@example.com"},
            "billing_cycle": "monthly",
            "unit_count": unit_count,
            "plan": {"id": 435, "name": plan_name, "price_model": "per-unit",
                     "unit_price": 9},
        },
    }
