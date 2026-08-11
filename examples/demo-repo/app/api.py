from app.cache_manager import CacheManager
from app.config import CACHE_ENABLED, CACHE_TTL

_cache = CacheManager(ttl=CACHE_TTL)


def get_user(user_id: int):
    """从缓存读取用户信息；未命中时查库并回填，防止缓存击穿。"""
    if not CACHE_ENABLED:
        return query_db("SELECT * FROM users WHERE id = ?", (user_id,))

    def _load():
        return query_db("SELECT * FROM users WHERE id = ?", (user_id,))

    return _cache.get_or_load(f"user:{user_id}", _load)


def login(username: str, password: str, secret: str) -> str:
    """登录：校验密码后签发 token。"""
    import hmac

    from app.auth import hash_password, sign_token, verify_token

    stored = "5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8"  # sha256("password")
    if not hmac.compare_digest(hash_password(password), stored):
        raise PermissionError("invalid credentials")
    token = sign_token(username, secret)
    if not verify_token(token, username, secret):
        raise RuntimeError("token signing failed")
    return token


def query_db(sql: str, params: tuple) -> dict | None:
    """模拟数据库查询。"""
    return {"id": params[0], "name": "alice"}
