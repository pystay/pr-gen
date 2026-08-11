"""生成验收测试仓库 examples/demo-repo。

包含：main 分支初始代码 + feature 分支 5 个文件变更
（新增 CacheManager 类、认证 API 逻辑修改、配置变更、新增测试），
提交信息含 Issue 引用（#42、JIRA-107），覆盖 bugfix + feature 两类语义。

可重复执行：先删除已有目录再重建。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent / "examples" / "demo-repo"

GIT_ENV = ["-c", "user.name=Demo", "-c", "user.email=demo@example.com"]


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *GIT_ENV, *args], cwd=cwd, check=True,
                   capture_output=True, text=True,
                   encoding="utf-8", errors="replace")


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main() -> None:
    if REPO.exists():
        shutil.rmtree(REPO, ignore_errors=True)
    REPO.mkdir(parents=True)

    git("init", "-b", "main", cwd=REPO)

    # ---------- main 分支初始代码 ----------
    write(REPO / "app/__init__.py", '"""Demo application."""\n')
    write(REPO / "app/config.py", """\
CACHE_ENABLED = False
CACHE_TTL = 0
""")
    write(REPO / "app/auth.py", """\
import hashlib


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()
""")
    write(REPO / "app/api.py", """\
def get_user(user_id: int):
    \"\"\"从数据库查询用户信息（每次直接查询）。\"\"\"
    row = query_db("SELECT * FROM users WHERE id = ?", (user_id,))
    return row


def query_db(sql: str, params: tuple) -> dict | None:
    \"\"\"模拟数据库查询。\"\"\"
    return {"id": params[0], "name": "alice"}
""")
    write(REPO / "tests/test_auth.py", """\
import unittest
from app.auth import hash_password


class TestAuth(unittest.TestCase):
    def test_hash_password(self):
        self.assertEqual(len(hash_password("x")), 64)
""")
    write(REPO / "requirements.txt", "flask==3.0.0\n")
    git("add", ".", cwd=REPO)
    git("commit", "-m", "init: 用户服务骨架", cwd=REPO)

    # ---------- feature 分支：修复缓存击穿 + 新增认证 API ----------
    git("checkout", "-b", "feature/user-auth-and-cache", cwd=REPO)

    write(REPO / "app/cache_manager.py", """\
import threading
import time


class CacheManager:
    \"\"\"线程安全的带锁缓存，使用双重检查防止缓存击穿。\"\"\"

    def __init__(self, ttl: int = 300):
        self._ttl = ttl
        self._store: dict[str, tuple[float, object]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()

    def get_or_load(self, key: str, loader):
        now = time.time()
        entry = self._store.get(key)
        if entry and entry[0] > now:
            return entry[1]
        lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            entry = self._store.get(key)
            if entry and entry[0] > now:
                return entry[1]
            value = loader()
            self._store[key] = (now + self._ttl, value)
            return value

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)
""")
    write(REPO / "app/config.py", """\
CACHE_ENABLED = True
CACHE_TTL = 300
""")
    write(REPO / "app/auth.py", """\
import hashlib
import hmac


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def verify_token(token: str, username: str, secret: str) -> bool:
    \"\"\"校验 token 是否由 secret 为 username 签发（常量时间比较）。\"\"\"
    return hmac.compare_digest(token, sign_token(username, secret))


def sign_token(username: str, secret: str) -> str:
    \"\"\"用 secret 对 username 签名，生成登录 token。\"\"\"
    return hmac.new(secret.encode("utf-8"), username.encode("utf-8"),
                    hashlib.sha256).hexdigest()
""")
    write(REPO / "app/api.py", """\
from app.cache_manager import CacheManager
from app.config import CACHE_ENABLED, CACHE_TTL

_cache = CacheManager(ttl=CACHE_TTL)


def get_user(user_id: int):
    \"\"\"从缓存读取用户信息；未命中时查库并回填，防止缓存击穿。\"\"\"
    if not CACHE_ENABLED:
        return query_db("SELECT * FROM users WHERE id = ?", (user_id,))

    def _load():
        return query_db("SELECT * FROM users WHERE id = ?", (user_id,))

    return _cache.get_or_load(f"user:{user_id}", _load)


def login(username: str, password: str, secret: str) -> str:
    \"\"\"登录：校验密码后签发 token。\"\"\"
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
    \"\"\"模拟数据库查询。\"\"\"
    return {"id": params[0], "name": "alice"}
""")
    write(REPO / "tests/test_cache_manager.py", """\
import time
import unittest

from app.cache_manager import CacheManager


class TestCacheManager(unittest.TestCase):
    def test_hit_after_load(self):
        cache = CacheManager(ttl=60)
        calls = []

        def loader():
            calls.append(1)
            return "value"

        self.assertEqual(cache.get_or_load("k", loader), "value")
        self.assertEqual(cache.get_or_load("k", loader), "value")
        self.assertEqual(len(calls), 1)  # 第二次命中缓存

    def test_expiry(self):
        cache = CacheManager(ttl=0)
        cache.get_or_load("k", lambda: "v")
        time.sleep(0.01)
        self.assertEqual(cache.get_or_load("k", lambda: "v2"), "v2")

    def test_invalidate(self):
        cache = CacheManager(ttl=60)
        cache.get_or_load("k", lambda: "v")
        cache.invalidate("k")
        self.assertEqual(cache.get_or_load("k", lambda: "v2"), "v2")
""")
    git("add", ".", cwd=REPO)
    git("commit", "-m", "fix: 修复用户信息查询缓存击穿问题 (#42)", cwd=REPO)
    write(REPO / "tests/test_auth.py", """\
import unittest
from app.auth import hash_password, sign_token, verify_token


class TestAuth(unittest.TestCase):
    def test_hash_password(self):
        self.assertEqual(len(hash_password("x")), 64)

    def test_verify_token(self):
        token = sign_token("alice", "secret")
        self.assertTrue(verify_token(token, "alice", "secret"))
        self.assertFalse(verify_token(token, "bob", "secret"))
        self.assertFalse(verify_token("tampered", "alice", "secret"))
""")
    git("add", ".", cwd=REPO)
    git("commit", "-m", "feat: 新增登录接口与 token 校验 (JIRA-107)", cwd=REPO)

    print(f"demo-repo ready: {REPO}")
    subprocess.run(["git", "log", "--oneline", "--all"], cwd=REPO, check=True,
                   encoding="utf-8", errors="replace")


if __name__ == "__main__":
    sys.exit(main())
