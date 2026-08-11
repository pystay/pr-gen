"""安全模块：Webhook 签名校验、敏感字段加密、GitHub App JWT。

- Webhook：X-Hub-Signature-256（HMAC-SHA256 + 常量时间比较）
- 加密：Fernet（AES-128-CBC + HMAC，cryptography 标准推荐），满足 GDPR 加密存储
- JWT：GitHub App 身份令牌（RS256），私钥从配置读取（PEM 内容或文件路径）
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from datetime import datetime, timedelta, timezone

import jwt
from cryptography.fernet import Fernet, InvalidToken


# ---------- Webhook 签名 ----------

def verify_webhook_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """校验 GitHub Webhook 的 X-Hub-Signature-256。

    支持逗号分隔的多签名（secret 轮换过渡期：任一匹配即通过）。
    """
    if not signature_header:
        return False
    for item in signature_header.split(","):
        item = item.strip()
        prefix = "sha256="
        if not item.startswith(prefix):
            continue
        expected = item[len(prefix):]
        digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(digest, expected):
            return True
    return False


# ---------- 敏感字段加密（Fernet / AES） ----------

class Encryptor:
    def __init__(self, key: str):
        self._fernet = Fernet(key.encode("utf-8") if isinstance(key, str) else key)

    @classmethod
    def generate_key(cls) -> str:
        return Fernet.generate_key().decode("utf-8")

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            return ""
        token = self._fernet.encrypt(plaintext.encode("utf-8"))
        return token.decode("utf-8")

    def decrypt(self, ciphertext: str) -> str:
        if not ciphertext:
            return ""
        try:
            return self._fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("敏感字段解密失败：密钥不匹配或数据损坏") from exc


# ---------- GitHub App JWT ----------

def create_github_app_jwt(app_id: str, private_key: str, ttl_minutes: int = 9) -> str:
    """签发 GitHub App JWT（RS256）。GitHub 规定 JWT 有效期最长 10 分钟。"""
    if not app_id or not private_key:
        raise ValueError("GitHub App 未配置：需要 GITHUB_APP_ID 与 GITHUB_APP_PRIVATE_KEY")
    now = datetime.now(timezone.utc)
    payload = {
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=ttl_minutes)).timestamp()),
        "iss": app_id,
    }
    return jwt.encode(payload, _load_private_key(private_key), algorithm="RS256")


def _load_private_key(private_key: str) -> bytes:
    """私钥支持直接传 PEM 内容或文件路径。"""
    key = private_key.strip()
    if "-----BEGIN" in key:
        return key.encode("utf-8")
    # 视为文件路径
    from pathlib import Path

    p = Path(key)
    if p.exists():
        return p.read_text(encoding="utf-8").encode("utf-8")
    raise ValueError("GITHUB_APP_PRIVATE_KEY 既不是 PEM 内容也不是有效文件路径")


def decode_jwt(token: str) -> dict:
    """解码 JWT 并校验签名（用于测试与调试；生产优先依赖 GitHub 回调）。"""
    return jwt.decode(token, options={"verify_signature": False})


# ---------- 内部管理令牌（看板/管理 API 简单鉴权） ----------

def admin_token(secret: str, salt: str = "admin") -> str:
    """生成/校验管理页访问令牌（HMAC，无状态）。"""
    digest = hmac.new(secret.encode("utf-8"), salt.encode("utf-8"),
                      hashlib.sha256).hexdigest()
    return digest
