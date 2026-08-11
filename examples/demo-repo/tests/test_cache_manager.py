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
