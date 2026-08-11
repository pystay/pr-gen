import hashlib
import hmac


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def verify_token(token: str, username: str, secret: str) -> bool:
    """校验 token 是否由 secret 为 username 签发（常量时间比较）。"""
    return hmac.compare_digest(token, sign_token(username, secret))


def sign_token(username: str, secret: str) -> str:
    """用 secret 对 username 签名，生成登录 token。"""
    return hmac.new(secret.encode("utf-8"), username.encode("utf-8"),
                    hashlib.sha256).hexdigest()
