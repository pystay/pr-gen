"""定价与支付模块验收测试。

验收标准映射：
  A1. 国内支付回调 5 秒内激活 Pro          → test_domestic_payment_activates_under_5s
  A2. PayPal Webhook 10 秒内激活订阅      → test_paypal_webhook_activates_under_10s
  A3. 注册自动插入 free 订阅（+30 天）     → test_register_creates_free_subscription
  A4. 提价后老用户锁定价续费、新用户新价   → test_price_lock_after_tier_change
  A5. 每日任务过期 → expired              → test_daily_expiry_task
"""

import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from app.config import load_settings
from app.db import Database, now
from app.payment import sign_payload
from app.pricing import Pricing
from tests.conftest import SECRET, make_client

PAY_SECRET = "dev-pay-secret"


def _pricing_yaml(pro_monthly: float = 9.9) -> Path:
    """生成临时定价配置（含促销窗口）。"""
    doc = {
        "tiers": {
            "free": {"price_monthly": 0, "price_yearly": 0, "quota_monthly": 5,
                     "features": ["每月 5 次免费生成"]},
            "pro": {"price_monthly": pro_monthly, "price_yearly": 99,
                    "currency": "CNY", "features": ["无限次数生成"]},
            "team": {"price_monthly": 39, "price_yearly": 399,
                     "currency": "CNY", "features": ["团队协作"]},
        },
        "international_pricing": {
            "pro": {"price_monthly": 2.99, "price_yearly": 29.99},
            "team": {"price_monthly": 9.99, "price_yearly": 99.99},
        },
        "promotion": {
            "pro_price_monthly": 9.9,
            "start_date": "2026-08-01",
            "end_date": "2099-12-31",
            "note": "公测特惠价 ¥9.9/月",
        },
    }
    p = Path(tempfile.mkdtemp(prefix="prgen-price-")) / "pricing_config.yaml"
    p.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    return p


class PaymentTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="prgen-pay-"))
        self.price_yaml = _pricing_yaml()
        self.client = make_client(
            self.tmp,
            PRICING_CONFIG_PATH=str(self.price_yaml),
            PAY_SECRET_KEY=PAY_SECRET,
        )
        self.db = Database(self.tmp / "data" / "prgen.db")

    def tearDown(self):
        self.client.close()
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def register(self, email: str = "user@example.com") -> dict:
        resp = self.client.post("/api/auth/register", json={"email": email})
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def create_order(self, user_id: int, tier: str = "pro", cycle: str = "monthly",
                     channel: str = "alipay", currency: str = "CNY") -> dict:
        resp = self.client.post("/api/payment/create", json={
            "user_id": user_id, "tier": tier, "cycle": cycle,
            "channel": channel, "currency": currency,
        })
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def domestic_callback(self, order_no: str, amount: float, status: str = "success"):
        """构造带签名的国内支付回调。"""
        params = {
            "pid": "1001",
            "out_trade_no": order_no,
            "trade_status": status,
            "money": f"{amount:.2f}",
            "trade_no": f"TRADE-{order_no}",
        }
        params["sign"] = sign_payload(params, PAY_SECRET)
        return self.client.post("/api/payment/callback", json=params)

    def paypal_webhook(self, order_no: str, amount: float, event_id: str = "WH-1"):
        event = {
            "id": event_id,
            "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {
                "custom_id": order_no,  # 真实 capture 事件的订单号载体
                "purchase_units": [{"reference_id": order_no}],
                "amount": {"value": f"{amount:.2f}", "currency_code": "USD"},
            },
        }
        # 本地模拟签名：HMAC(event_id, PAY_SECRET_KEY)
        import hashlib
        import hmac as hmac_mod

        sig = hmac_mod.new(PAY_SECRET.encode(), event_id.encode(),
                           hashlib.sha256).hexdigest()
        return self.client.post(
            "/api/payment/paypal/webhook", json=event,
            headers={"X-Sim-Signature": sig},
        )


class TestRegistration(PaymentTestBase):
    """验收 A3：注册自动插入 free 订阅（end_date = +30 天）。"""

    def test_register_creates_free_subscription(self):
        data = self.register("alice@example.com")
        self.assertFalse(data["already_registered"])
        sub = data["subscription"]
        self.assertEqual(sub["plan"], "free")
        self.assertEqual(sub["status"], "active")
        # end_date ≈ 注册时间 + 30 天
        diff = sub["expiry_date"] - sub["effective_date"]
        self.assertAlmostEqual(diff, 30 * 86400, delta=60)

    def test_register_idempotent_by_email(self):
        self.register("same@example.com")
        data = self.register("same@example.com")
        self.assertTrue(data["already_registered"])
        self.assertEqual(len(self.db.list_subscriptions()), 1)

    def test_register_rejects_bad_email(self):
        resp = self.client.post("/api/auth/register", json={"email": "not-an-email"})
        self.assertEqual(resp.status_code, 422)


class TestDomesticPayment(PaymentTestBase):
    """验收 A1：扫码支付回调 5 秒内激活 Pro。"""

    def test_domestic_payment_activates_under_5s(self):
        uid = self.register()["user_id"]
        order = self.create_order(uid, tier="pro", cycle="monthly", channel="alipay")
        self.assertEqual(order["quote"]["amount"], 9.9)  # 促销价/定价 9.9
        self.assertIn("qr_text", order["payment"])       # 收款二维码

        start = time.monotonic()
        resp = self.domestic_callback(order["order_no"], amount=9.9)
        elapsed = time.monotonic() - start

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"code": 0, "msg": "success"})  # 网关约定格式
        self.assertLess(elapsed, 5, f"回调激活耗时 {elapsed:.2f}s")

        sub = self.db.get_subscription(str(uid))
        self.assertEqual(sub["plan"], "pro")
        self.assertEqual(sub["status"], "active")
        self.assertEqual(sub["payment_channel"], "alipay")
        self.assertEqual(sub["price_locked"], 1)
        self.assertEqual(sub["locked_price"], 9.9)  # 首次订阅锁定价格

        # 支付流水已记录
        logs = self.db.list_payments()
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["status"], "success")
        self.assertIn("TRADE-", logs[0]["raw_callback"])  # 回调留痕

    def test_callback_idempotent(self):
        uid = self.register()["user_id"]
        order = self.create_order(uid)
        self.domestic_callback(order["order_no"], amount=9.9)
        resp = self.domestic_callback(order["order_no"], amount=9.9)  # 重复回调
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.db.list_payments()[0]["status"], "success")
        # 续费逻辑不应重复叠加：第二次回调不延长到期日
        sub = self.db.get_subscription(str(uid))
        self.assertEqual(sub["plan"], "pro")

    def test_callback_bad_signature_rejected(self):
        uid = self.register()["user_id"]
        order = self.create_order(uid)
        params = {"pid": "1001", "out_trade_no": order["order_no"],
                  "trade_status": "success", "money": "9.90", "sign": "forged"}
        resp = self.client.post("/api/payment/callback", json=params)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.db.get_payment(order["order_no"])["status"], "pending")

    def test_callback_amount_mismatch_rejected(self):
        uid = self.register()["user_id"]
        order = self.create_order(uid)
        resp = self.domestic_callback(order["order_no"], amount=0.01)  # 金额不符
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.db.get_payment(order["order_no"])["status"], "pending")

    def test_wechat_channel(self):
        uid = self.register()["user_id"]
        order = self.create_order(uid, channel="wechat")
        self.assertEqual(order["channel"], "wechat")
        self.domestic_callback(order["order_no"], amount=9.9)
        sub = self.db.get_subscription(str(uid))
        self.assertEqual(sub["payment_channel"], "wechat")  # 渠道以订单为准

    def test_yearly_cycle_extends_365_days(self):
        uid = self.register()["user_id"]
        order = self.create_order(uid, cycle="yearly")
        self.assertEqual(order["quote"]["amount"], 99.0)
        self.domestic_callback(order["order_no"], amount=99.0)
        sub = self.db.get_subscription(str(uid))
        # 注册时已有 30 天试用期，年付续费在其上延长 365 天（试用不浪费）
        diff = sub["expiry_date"] - sub["effective_date"]
        self.assertAlmostEqual(diff, (30 + 365) * 86400, delta=120)


class TestPayPal(PaymentTestBase):
    """验收 A2：PayPal Webhook 10 秒内接收并激活订阅。"""

    def test_paypal_webhook_activates_under_10s(self):
        uid = self.register()["user_id"]
        resp = self.client.post("/api/payment/paypal/create", json={
            "user_id": uid, "tier": "pro", "cycle": "monthly",
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["amount"], 2.99)          # 海外 USD 定价
        self.assertIn("checkoutnow", data["approval_url"])

        start = time.monotonic()
        wb = self.paypal_webhook(data["order_no"], amount=2.99)
        elapsed = time.monotonic() - start
        self.assertEqual(wb.status_code, 200)
        self.assertTrue(wb.json()["ok"])
        self.assertLess(elapsed, 10, f"PayPal 激活耗时 {elapsed:.2f}s")

        sub = self.db.get_subscription(str(uid))
        self.assertEqual(sub["plan"], "pro")
        self.assertEqual(sub["payment_channel"], "paypal")

    def test_paypal_webhook_idempotent(self):
        uid = self.register()["user_id"]
        data = self.client.post("/api/payment/paypal/create", json={
            "user_id": uid, "tier": "pro", "cycle": "monthly",
        }).json()
        self.paypal_webhook(data["order_no"], amount=2.99)
        r2 = self.paypal_webhook(data["order_no"], amount=2.99)  # 重复事件
        self.assertTrue(r2.json()["ok"])

    def test_paypal_bad_signature_rejected(self):
        uid = self.register()["user_id"]
        data = self.client.post("/api/payment/paypal/create", json={
            "user_id": uid, "tier": "pro", "cycle": "monthly",
        }).json()
        resp = self.client.post("/api/payment/paypal/webhook", json={
            "id": "WH-X", "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {"purchase_units": [{"reference_id": data["order_no"]}],
                         "amount": {"value": "2.99"}},
        }, headers={"X-Sim-Signature": "wrong"})
        self.assertEqual(resp.status_code, 401)


class TestPriceLock(PaymentTestBase):
    """验收 A4：提价后老用户锁定价续费，新用户按新价。"""

    def test_price_lock_after_tier_change(self):
        # 用户 A：9.9 定价下首次订阅 → 锁定 9.9
        uid_a = self.register("old@example.com")["user_id"]
        order_a = self.create_order(uid_a, tier="pro")
        self.domestic_callback(order_a["order_no"], amount=9.9)

        # 提价：9.9 → 19.9（改配置文件，无需重启）
        self.price_yaml.write_text(
            yaml.safe_dump({"tiers": {
                "free": {"price_monthly": 0, "price_yearly": 0},
                "pro": {"price_monthly": 19.9, "price_yearly": 199,
                        "currency": "CNY"},
                "team": {"price_monthly": 39, "price_yearly": 399,
                         "currency": "CNY"},
            }, "international_pricing": {
                "pro": {"price_monthly": 4.99, "price_yearly": 49.99},
            }}, allow_unicode=True), encoding="utf-8")
        self.client.app.state.pricing.reload()

        # 老用户 A 续费 → 仍按 9.9（locked_price）
        order_a2 = self.create_order(uid_a, tier="pro")
        self.assertEqual(order_a2["quote"]["amount"], 9.9)
        self.assertTrue(order_a2["quote"]["locked"])
        self.assertEqual(order_a2["quote"]["locked_price"], 9.9)

        # 新用户 B → 按新价 19.9
        uid_b = self.register("new@example.com")["user_id"]
        order_b = self.create_order(uid_b, tier="pro")
        self.assertEqual(order_b["quote"]["amount"], 19.9)
        self.assertFalse(order_b["quote"]["locked"])

    def test_quote_module_promotion_and_currency(self):
        pricing = Pricing(self.price_yaml)
        # 促销窗口内（2026-08-01 ~ 2099-12-31）：pro 月付 9.9
        q = pricing.quote("pro", "monthly", "CNY")
        self.assertTrue(q.promotion_active)
        self.assertEqual(q.amount, 9.9)
        # 海外 USD
        q_usd = pricing.quote("pro", "monthly", "USD")
        self.assertEqual(q_usd.amount, 2.99)
        self.assertEqual(q_usd.currency, "USD")
        # free 恒 0
        self.assertEqual(pricing.quote("free", "monthly").amount, 0.0)
        # 年付
        self.assertEqual(pricing.quote("pro", "yearly", "CNY").amount, 99.0)

    def test_price_lock_cross_cycle(self):
        """锁定价为月度价格点：月付锁定 9.9 后，年付续费 = 9.9×12 = 118.8。"""
        pricing = Pricing(self.price_yaml)
        # 月付锁定 9.9
        q = pricing.quote("pro", "monthly", "CNY", locked=True, locked_price=9.9,
                          locked_currency="CNY")
        self.assertEqual(q.amount, 9.9)
        # 年付续费按锁定月价 × 12
        qy = pricing.quote("pro", "yearly", "CNY", locked=True, locked_price=9.9,
                           locked_currency="CNY")
        self.assertEqual(qy.amount, 118.8)
        self.assertTrue(qy.locked)
        # 币种不匹配：锁定价不生效（回退新价）
        q_usd = pricing.quote("pro", "monthly", "USD", locked=True,
                              locked_price=9.9, locked_currency="CNY")
        self.assertFalse(q_usd.locked)
        self.assertEqual(q_usd.amount, 2.99)

    def test_price_lock_end_to_end_cross_cycle(self):
        """端到端：月付支付锁定 9.9 → 提价后年付续费仍按 9.9×12。"""
        uid = self.register("lock@example.com")["user_id"]
        order = self.create_order(uid, tier="pro", cycle="monthly")
        self.domestic_callback(order["order_no"], amount=9.9)
        sub = self.db.get_subscription(str(uid))
        self.assertEqual(sub["locked_price"], 9.9)  # 月度价格点
        self.assertEqual(sub["locked_currency"], "CNY")

        # 提价后年付续费
        self.price_yaml.write_text(
            yaml.safe_dump({"tiers": {
                "free": {"price_monthly": 0, "price_yearly": 0},
                "pro": {"price_monthly": 19.9, "price_yearly": 199,
                        "currency": "CNY"},
                "team": {"price_monthly": 39, "price_yearly": 399,
                         "currency": "CNY"},
            }}, allow_unicode=True), encoding="utf-8")
        self.client.app.state.pricing.reload()

        order2 = self.create_order(uid, tier="pro", cycle="yearly")
        self.assertEqual(order2["quote"]["amount"], 118.8)  # 9.9 × 12（锁定价）


class TestLifecycle(PaymentTestBase):
    """验收 A5：每日任务把过期订阅置为 expired；用量重置。"""

    def test_daily_expiry_task(self):
        uid = self.register()["user_id"]
        # 手动把到期日改到过去
        sub = self.db.get_subscription(str(uid))
        self.db.upsert_subscription({**sub, "expiry_date": now() - 86400})
        resp = self.client.post("/api/github/cron/downgrade")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["downgraded"], 1)
        self.assertEqual(self.db.get_subscription(str(uid))["status"], "expired")

    def test_active_paid_subscription_expires(self):
        uid = self.register()["user_id"]
        order = self.create_order(uid)
        self.domestic_callback(order["order_no"], amount=9.9)
        sub = self.db.get_subscription(str(uid))
        self.db.upsert_subscription({**sub, "expiry_date": now() - 60})
        resp = self.client.post("/api/github/cron/downgrade")
        self.assertEqual(resp.json()["downgraded"], 1)
        self.assertEqual(self.db.get_subscription(str(uid))["status"], "expired")

    def test_free_usage_reset(self):
        self.db.record_usage("1", "free", calls=3, cost=0.0)
        self.db.record_usage("2", "pro", calls=5, cost=1.2)
        resp = self.client.post("/api/github/cron/reset-usage")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["cleared"], 1)
        # pro 用户用量保留
        self.assertEqual(self.db.usage_series()[0]["calls"], 5)

    def test_gdpr_anonymization(self):
        uid = self.register("gdpr@example.com")["user_id"]
        order = self.create_order(uid)
        self.domestic_callback(order["order_no"], amount=9.9)
        resp = self.client.delete(f"/api/auth/me/{uid}")
        self.assertEqual(resp.status_code, 200)
        # 用户与订阅已删除，支付记录匿名化（user_id=-1，raw_callback 清空）
        self.assertIsNone(self.db.get_user(uid))
        self.assertIsNone(self.db.get_subscription(str(uid)))
        logs = self.db.list_payments()
        self.assertEqual(logs[0]["user_id"], -1)
        self.assertEqual(logs[0]["raw_callback"], "")


class TestPricingEndpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="prgen-price-api-"))
        self.client = make_client(self.tmp, PRICING_CONFIG_PATH=str(_pricing_yaml()))

    def tearDown(self):
        self.client.close()
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pricing_endpoint(self):
        resp = self.client.get("/api/pricing")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(set(data["tiers"]), {"free", "pro", "team"})
        self.assertEqual(data["tiers"]["pro"]["cn"]["amount"], 9.9)
        self.assertEqual(data["tiers"]["pro"]["usd"]["amount"], 2.99)
        self.assertEqual(data["tiers"]["team"]["cn_yearly"]["amount"], 399.0)
        self.assertEqual(data["free_quota"], 5)
        self.assertTrue(data["promotion"]["active"])


if __name__ == "__main__":
    unittest.main()
