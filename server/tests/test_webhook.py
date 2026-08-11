"""验收 1：GitHub Marketplace Webhook —— 激活、幂等、取消、到期降级。"""

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import Database
from app.webhook import get_effective_subscription
from tests.conftest import make_client, purchase_payload, signed_payload

PAST = "2020-01-01T00:00:00+00:00"
FUTURE = "2099-01-01T00:00:00+00:00"


class TestWebhookAcceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="prgen-test-"))
        self.client: TestClient = make_client(self.tmp)
        self.db = Database(self.tmp / "data" / "prgen.db")

    def tearDown(self):
        self.client.close()
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _post(self, payload: dict):
        sp = signed_payload(payload)
        return self.client.post(
            "/api/github/webhook", content=sp["body"],
            headers={
                "X-Hub-Signature-256": sp["signature"],
                "X-GitHub-Event": "marketplace_purchase",
            },
        )

    # ---------- 验收 1a：purchased → 创建用户并激活 Pro ----------

    def test_purchase_activates_pro(self):
        resp = self._post(purchase_payload(action="purchased", plan_name="Pro Plan"))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["activated"])
        self.assertEqual(data["plan"], "pro")

        sub = self.db.get_subscription("184")
        self.assertIsNotNone(sub)
        self.assertEqual(sub["plan"], "pro")
        self.assertEqual(sub["status"], "active")
        self.assertEqual(sub["seats"], 5)

        # 订单已记录（5 席 × $9 = $45）
        orders = self.db.list_orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["amount"], 45.0)
        self.assertEqual(orders[0]["plan"], "pro")

    def test_purchase_team_plan(self):
        resp = self._post(purchase_payload(action="purchased", plan_name="Team Plan",
                                           unit_count=2))
        self.assertEqual(resp.json()["plan"], "team")
        sub = self.db.get_subscription("184")
        self.assertEqual(sub["plan"], "team")

    # ---------- 幂等：重复事件不重复处理 ----------

    def test_duplicate_event_idempotent(self):
        payload = purchase_payload()
        self._post(payload)
        self._post(payload)  # 重复投递
        orders = self.db.list_orders()
        self.assertEqual(len(orders), 1, "重复事件不应生成重复订单")

    # ---------- 签名校验 ----------

    def test_bad_signature_rejected(self):
        body = json.dumps(purchase_payload()).encode("utf-8")
        resp = self.client.post(
            "/api/github/webhook", content=body,
            headers={"X-Hub-Signature-256": "sha256=deadbeef",
                     "X-GitHub-Event": "marketplace_purchase"},
        )
        self.assertEqual(resp.status_code, 401)

    # ---------- 验收 1b：cancelled → 到期日自动降级 ----------

    def test_cancelled_downgrades_after_expiry(self):
        # 先激活 Pro
        self._post(purchase_payload(action="purchased"))
        self.assertEqual(self.db.get_subscription("184")["plan"], "pro")

        # cancelled：生效日在过去 → 已到期
        resp = self._post(purchase_payload(action="cancelled", effective=PAST,
                                           event_id=9002))
        self.assertTrue(resp.json()["cancelled"])
        sub = self.db.get_subscription("184")
        self.assertEqual(sub["status"], "cancelled")

        # 懒读取自动降级
        eff = get_effective_subscription(self.db, "184")
        self.assertEqual(eff["status"], "expired")
        self.assertEqual(eff["plan"], "free")

    def test_cancelled_not_downgraded_before_expiry(self):
        self._post(purchase_payload(action="purchased"))
        self._post(purchase_payload(action="cancelled", effective=FUTURE, event_id=9002))
        eff = get_effective_subscription(self.db, "184")
        self.assertEqual(eff["status"], "cancelled")  # 未到期，仍是 cancelled
        self.assertEqual(eff["plan"], "pro")

    def test_cron_downgrade_endpoint(self):
        self._post(purchase_payload(action="purchased"))
        self._post(purchase_payload(action="cancelled", effective=PAST, event_id=9002))
        resp = self.client.post("/api/github/cron/downgrade")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["downgraded"], 1)
        sub = self.db.get_subscription("184")
        self.assertEqual(sub["status"], "expired")
        self.assertEqual(sub["plan"], "free")

    # ---------- changed / pending_change / refunded ----------

    def test_changed_plan(self):
        self._post(purchase_payload(action="purchased"))
        resp = self._post(purchase_payload(action="changed", plan_name="Team Plan",
                                           event_id=9003))
        self.assertEqual(resp.json()["plan"], "team")
        self.assertEqual(self.db.get_subscription("184")["plan"], "team")

    def test_pending_change_and_refunded(self):
        r1 = self._post(purchase_payload(action="pending_change", event_id=9004))
        self.assertTrue(r1.json()["pending_change"])
        r2 = self._post(purchase_payload(action="refunded", event_id=9005))
        self.assertTrue(r2.json()["refunded"])
        self.assertGreaterEqual(len(self.db.list_notifications()), 2)

    def test_ping_event(self):
        sp = signed_payload({"zen": "keep it simple"})
        resp = self.client.post(
            "/api/github/webhook", content=sp["body"],
            headers={"X-Hub-Signature-256": sp["signature"], "X-GitHub-Event": "ping"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["event"], "ping")


if __name__ == "__main__":
    unittest.main()
