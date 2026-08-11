import threading
import time


class CacheManager:
    """线程安全的带锁缓存，使用双重检查防止缓存击穿。"""

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
