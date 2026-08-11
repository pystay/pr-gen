"""验收 3：运营看板 —— 最近 Pro 订单详情与总 MRR；成本告警。"""

import tempfile
import unittest
from pathlib import Path

from tests.conftest import make_client, purchase_payload


class TestAdminAcceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="prgen-test-"))
        self.client = make_client(self.tmp)

    def tearDown(self):
        self.client.close()
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _purchase(self, plan: str = "Pro Plan", unit_count: int = 5,
                  account_id: int = 184, login: str = "acme-corp"):
        from tests.conftest import signed_payload

        sp = signed_payload(purchase_payload(
            action="purchased", plan_name=plan, unit_count=unit_count,
            account_id=account_id, login=login, event_id=account_id + 9000,
        ))
        return self.client.post(
            "/api/github/webhook", content=sp["body"],
            headers={"X-Hub-Signature-256": sp["signature"],
                     "X-GitHub-Event": "marketplace_purchase"},
        )

    def test_stats_after_pro_purchase(self):
        resp = self._purchase(plan="Pro Plan", unit_count=5)
        self.assertEqual(resp.status_code, 200)

        stats = self.client.get("/api/admin/stats").json()
        # MRR = 5 席 × $9 = $45
        self.assertEqual(stats["mrr"], 45.0)
        self.assertEqual(stats["arr"], 540.0)
        # 活跃用户统计
        self.assertEqual(stats["active_total"], 1)
        self.assertEqual(stats["active_users"]["pro"], 1)
        self.assertEqual(stats["active_users"]["free"], 0)
        # 最近订单详情
        self.assertEqual(len(stats["orders"]), 1)
        order = stats["orders"][0]
        self.assertEqual(order["plan"], "pro")
        self.assertEqual(order["plan_name"], "Pro")
        self.assertEqual(order["seats"], 5)
        self.assertEqual(order["amount"], 45.0)
        self.assertEqual(order["account_login"], "acme-corp")

    def test_mrr_multiple_subscriptions(self):
        self._purchase(plan="Pro Plan", unit_count=5, account_id=184)
        self._purchase(plan="Team Plan", unit_count=2, account_id=777,
                       login="bigcorp")
        stats = self.client.get("/api/admin/stats").json()
        # 45 + 2×29 = 103
        self.assertEqual(stats["mrr"], 103.0)
        self.assertEqual(stats["active_users"]["team"], 1)

    def test_cancelled_subscription_excluded_from_mrr(self):
        from tests.conftest import signed_payload

        self._purchase(plan="Pro Plan", unit_count=5)
        sp = signed_payload(purchase_payload(
            action="cancelled", effective="2099-01-01T00:00:00+00:00",
            account_id=184, event_id=99999,
        ))
        self.client.post(
            "/api/github/webhook", content=sp["body"],
            headers={"X-Hub-Signature-256": sp["signature"],
                     "X-GitHub-Event": "marketplace_purchase"},
        )
        stats = self.client.get("/api/admin/stats").json()
        self.assertEqual(stats["mrr"], 0.0)  # 无 active 订阅
        self.assertEqual(stats["active_total"], 0)

    def test_admin_page_served(self):
        resp = self.client.get("/admin")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("运营看板", resp.text)

    def test_cost_alert_threshold(self):
        from app.db import Database

        db = Database(self.tmp / "data" / "prgen.db")
        db.record_usage("u1", "pro", calls=100, cost=6.0)  # 超过 $5 阈值
        resp = self.client.post("/api/usage", json={
            "account_id": "u2", "plan": "pro", "calls": 1, "cost": 0.5,
        })
        self.assertEqual(resp.status_code, 200)
        alert = resp.json()["alert"]
        self.assertIsNotNone(alert)
        self.assertIn("成本告警", alert["message"])
        # 看板显示今日成本
        stats = self.client.get("/api/admin/stats").json()
        self.assertEqual(stats["today_cost"], 6.5)


if __name__ == "__main__":
    unittest.main()
