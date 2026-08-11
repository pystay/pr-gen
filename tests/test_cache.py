"""cache 模块单元测试。"""

import unittest

from pr_gen import cache


class TestCache(unittest.TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_put_get(self):
        cache.put("k1", "generation", "hello")
        self.assertEqual(cache.get("k1"), "hello")

    def test_miss(self):
        self.assertIsNone(cache.get("nope"))

    def test_overwrite(self):
        cache.put("k1", "generation", "v1")
        cache.put("k1", "generation", "v2")
        self.assertEqual(cache.get("k1"), "v2")

    def test_fingerprint_stable(self):
        a = cache.fingerprint({"b": 1, "a": [1, 2], "c": "中文"})
        b = cache.fingerprint({"c": "中文", "a": [1, 2], "b": 1})
        self.assertEqual(a, b)
        c = cache.fingerprint({"b": 1, "a": [1, 2], "c": "中文!"})
        self.assertNotEqual(a, c)

    def test_stats(self):
        cache.put("k1", "analysis", "x")
        cache.put("k2", "generation", "y")
        cache.get("k1")
        st = cache.stats()
        self.assertEqual(st["entries"], 2)
        self.assertEqual(st["by_kind"]["analysis"], 1)
        self.assertGreaterEqual(st["total_hits"], 1)


if __name__ == "__main__":
    unittest.main()
