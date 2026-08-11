"""看板测试：用户数、用量、成本告警。"""

import tempfile
import unittest
from pathlib import Path

from app.db import Database
from tests.conftest import make_client


class TestAdmin(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="prgen-test-"))
        self.client = make_client(self.tmp)
        self.db = Database(self.tmp / "data" / "prgen.db")

    def tearDown(self):
        self.client.close()
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _register(self, email: str) -> int:
        return self.client.post("/api/auth/register",
                                json={"email": email}).json()["user_id"]

    def test_stats_after_register(self):
        self._register("a@example.com")
        self._register("b@example.com")
        stats = self.client.get("/api/admin/stats").json()
        self.assertEqual(stats["users_total"], 2)
        self.assertEqual(stats["users_active"], 2)

    def test_usage_recorded(self):
        uid = self._register("a@example.com")
        self.client.post("/api/usage", json={
            "account_id": str(uid), "plan": "free", "calls": 3, "cost": 0.42,
        })
        stats = self.client.get("/api/admin/stats").json()
        self.assertEqual(stats["today_cost"], 0.42)
        self.assertEqual(stats["usage"][0]["calls"], 3)

    def test_admin_page_served(self):
        resp = self.client.get("/admin")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("运营看板", resp.text)
        self.assertIn("运营看板", resp.text)

    def test_cost_alert_threshold(self):
        self.db.record_usage("u1", "free", calls=100, cost=6.0)  # 超过 $5 阈值
        resp = self.client.post("/api/usage", json={
            "account_id": "u2", "plan": "free", "calls": 1, "cost": 0.5,
        })
        self.assertEqual(resp.status_code, 200)
        alert = resp.json()["alert"]
        self.assertIsNotNone(alert)
        self.assertIn("成本告警", alert["message"])
        stats = self.client.get("/api/admin/stats").json()
        self.assertEqual(stats["today_cost"], 6.5)


class TestCron(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="prgen-test-"))
        self.client = make_client(self.tmp)
        self.db = Database(self.tmp / "data" / "prgen.db")

    def tearDown(self):
        self.client.close()
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_free_usage_reset(self):
        self.db.record_usage("1", "free", calls=3, cost=0.0)
        resp = self.client.post("/api/github/cron/reset-usage")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["cleared"], 1)


if __name__ == "__main__":
    unittest.main()
