## Summary
新增登录接口与基于 HMAC 的 Token 签发/校验，并引入带锁 TTL 缓存，修复用户信息查询的缓存击穿问题。

## Changes

### 登录认证（feature / security）
- `app/auth.py`：新增 `sign_token(username, secret)` 与 `verify_token(token, username, secret)`，使用 HMAC-SHA256 对用户名签名；`verify_token` 通过 `hmac.compare_digest` 做常量时间比较，防止时序侧信道攻击。
- `app/api.py`：新增 `login(username, password, secret)` 接口，密码校验通过后签发 token，并在返回前自检（sign → verify）确保 token 可用。

### 用户缓存（bugfix / performance）
- `app/cache_manager.py`：新增 `CacheManager` 类，核心方法 `get_or_load(key, loader)` 采用双重检查锁，同一 key 并发未命中时只允许一个请求执行 `loader`，其余请求等待后直接命中，防止缓存击穿打到数据库。
- `app/api.py`：`get_user` 改为优先通过 `_cache.get_or_load(f"user:{user_id}", _load)` 读取，未命中时才查库并回填；`CACHE_ENABLED` 为 False 时保留原有直查行为。
- `app/config.py`：`CACHE_ENABLED` 默认开启（True），`CACHE_TTL` 从 0 调整为 300 秒。

### 测试
- `tests/test_auth.py`：新增 `test_verify_token`，覆盖正确 token、错误用户名、篡改 token 三种场景。
- `tests/test_cache_manager.py`：新增 `test_hit_after_load`、`test_expiry`、`test_invalidate`，覆盖缓存命中、过期与主动失效。

## Test Plan

1. 运行全部单元测试：
   ```bash
   python -m unittest discover -s tests -v
   ```
   重点用例：
   - `TestAuth.test_verify_token`：错误用户名与篡改 token 必须返回 `False`；
   - `TestCacheManager.test_hit_after_load`：第二次 `get_or_load` 不触发 loader；
   - `TestCacheManager.test_expiry`：TTL=0 时立即过期并重新加载；
   - `TestCacheManager.test_invalidate`：`invalidate` 后重新加载。

2. 手工验证登录接口：
   ```bash
   python -c "from app.api import login; print(login('alice','password','secret'))"
   ```
   应返回 64 位 hex 字符串；错误密码应抛 `PermissionError`。

3. 手工验证缓存击穿修复：
   并发调用 `get_user(1)`，确认 `query_db` 只执行一次：
   ```bash
   python -c "from app.api import _cache, get_user; from threading import Thread; get_user(1); ts=[Thread(target=lambda: get_user(1)) for _ in range(5)]; [t.start() for t in ts]; [t.join() for t in ts]; print(_cache._store)"
   ```

## Impact

- 无破坏性变更：`get_user` 签名与调用方式保持兼容。
- 行为变更：
  - `CACHE_ENABLED` 默认值从 False 改为 True，`get_user` 默认使用缓存，用户信息变更后最多 5 分钟才可见，业务侧可调用 `_cache.invalidate(f"user:{user_id}")` 主动失效；
  - 新增公开接口 `login(username, password, secret)`，当前 token 不含过期时间，后续若需要会话过期机制需另行设计。
- 新增依赖：无（仅使用标准库 `threading`、`hmac`、`hashlib`）。

## Related Issues
- JIRA-107
- #42